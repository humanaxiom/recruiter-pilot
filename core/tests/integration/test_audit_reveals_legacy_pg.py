"""Integration tests — FU-5 slice 10 (ADR-019 §6 / §9.4): ``GET
/audit/reveals-legacy``, the read-only paginated view of the FROZEN
``reveal_audit`` table, against a REAL Postgres (testcontainers).

What a real Postgres proves that a mocked-conn route test (``tests/unit/
test_route_audit.py``) structurally cannot:

* the ``ORDER BY revealed_at DESC`` guarantee actually holds against real
  rows with real timestamps, not whatever order a mock happened to return
  them in,
* the ``limit``/``offset`` window is a real SQL ``LIMIT``/``OFFSET`` pair —
  the correct slice of a real ordered result set, not just "the mock was
  called with these two integers",
* **the security-relevant guarantee**: the endpoint reads ``reveal_audit``
  ALONE. It must not join against ``resumes`` (or decrypt anything) to
  enrich the response — a route that accidentally joined in
  ``candidate_name``/``candidate_email`` would leak PII through an endpoint
  ADR-019 §6 says exists purely "for historical review" of the frozen audit
  sink. This is unprovable with a mocked connection (nothing to leak from).

``src.api.routes.audit`` does not exist yet, so every test below is expected
to fail — most immediately at ``ModuleNotFoundError``/``ImportError``. RED
half of the TDD cycle.

Follows the exact testcontainers/pg_pool wiring already used in
``tests/integration/test_reveal_audit_log_pg.py`` and the encrypted-PII
seeding helper from ``tests/integration/test_resume_read_pg.py``. No new
harness. Role gating (admin/auditor allowed, recruiter/hiring_manager
rejected, ``limit``/``offset`` 422 boundaries) is covered exhaustively by the
unit test file — this file bypasses ``resolve_role`` with a fixed role for
the ordering/pagination/no-PII-join tests and adds only a minimal role-gate
smoke check against the real app wiring.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import patch

import asyncpg
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from testcontainers.postgres import PostgresContainer

from src.api.deps import Role, resolve_role
from src.api.routes import audit as audit_routes
from src.errors import AppError
from src.models.ddl import init_schema
from src.models.pool import get_db
from src.services.pii import encrypt as pii_encrypt
from src.services.pii import set_pii_key

TEST_PII_KEY = "integration-test-pii-key-do-not-use-in-prod"


@pytest.fixture(scope="session")
def pg_dsn() -> Iterator[str]:
    with PostgresContainer("postgres:16-alpine") as pg:
        yield re.sub(r"\+\w+", "", pg.get_connection_url(), count=1)


@pytest.fixture
async def pg_pool(pg_dsn: str) -> AsyncIterator[asyncpg.Pool]:
    pool = await asyncpg.create_pool(dsn=pg_dsn, min_size=2, max_size=8)
    await init_schema(pool)
    async with pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE sessions, users, audit_log, reveal_audit, jobs, "
            "resumes, outbox CASCADE"
        )
    try:
        yield pool
    finally:
        await pool.close()


@pytest.fixture(autouse=True)
def patched_pii_key() -> Iterator[None]:
    from types import SimpleNamespace

    fake_settings = SimpleNamespace(pii_key=TEST_PII_KEY)
    with patch("src.services.pii.get_settings", return_value=fake_settings):
        yield


def _build_app(pool: asyncpg.Pool, *, role: Role = Role.ADMIN) -> FastAPI:
    app = FastAPI()
    app.include_router(audit_routes.router)

    async def _get_db_override() -> AsyncIterator[Any]:
        async with pool.acquire() as conn:
            yield conn

    app.dependency_overrides[get_db] = _get_db_override
    app.dependency_overrides[resolve_role] = lambda: role

    @app.exception_handler(AppError)
    async def _app_error_handler(_request: Any, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status, content={"detail": exc.message, "code": exc.code}
        )

    return app


async def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _insert_reveal_audit_row(
    pool: asyncpg.Pool,
    *,
    resume_id: uuid.UUID | None = None,
    job_id: uuid.UUID | None = None,
    actor: str | None = "priya",
    context: str | None = None,
    revealed_at: dt.datetime,
) -> uuid.UUID:
    row_id = uuid.uuid4()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO reveal_audit
                (id, resume_id, job_id, actor, context, revealed_at)
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            row_id,
            resume_id or uuid.uuid4(),
            job_id,
            actor,
            context,
            revealed_at,
        )
    return row_id


async def _insert_job(pool: asyncpg.Pool) -> uuid.UUID:
    async with pool.acquire() as conn:
        job_id: uuid.UUID = await conn.fetchval(
            "INSERT INTO jobs (title, description_raw) VALUES ($1, $2) RETURNING id",
            "Senior Backend Engineer",
            "raw jd text " * 10,
        )
    return job_id


async def _insert_resume_with_pii(
    pool: asyncpg.Pool, job_id: uuid.UUID, *, name: str, email: str
) -> uuid.UUID:
    async with pool.acquire() as conn:
        resume_id: uuid.UUID = await conn.fetchval(
            """
            INSERT INTO resumes (
                job_id, blob_key, original_filename, mime_type,
                file_size_bytes, sha256, consent_acknowledged, status
            ) VALUES ($1, $2, $3, 'application/pdf', 1024, $4, TRUE, 'parsed')
            RETURNING id
            """,
            job_id,
            f"resumes/{uuid.uuid4().hex}.pdf",
            "resume.pdf",
            uuid.uuid4().hex,
        )
        async with conn.transaction():
            await set_pii_key(conn)
            name_enc = await pii_encrypt(conn, name)
            email_enc = await pii_encrypt(conn, email)
            await conn.execute(
                "UPDATE resumes SET candidate_name = $2, candidate_email = $3 "
                "WHERE id = $1",
                resume_id,
                name_enc,
                email_enc,
            )
    return resume_id


# ── ORDER BY revealed_at DESC — real timestamps, real ordering ────────────


@pytest.mark.asyncio
async def test_rows_come_back_ordered_by_revealed_at_desc(
    pg_pool: asyncpg.Pool,
) -> None:
    base = dt.datetime(2026, 7, 1, 12, 0, 0, tzinfo=dt.UTC)
    oldest_id = await _insert_reveal_audit_row(pg_pool, actor="alice", revealed_at=base)
    middle_id = await _insert_reveal_audit_row(
        pg_pool, actor="bob", revealed_at=base + dt.timedelta(hours=1)
    )
    newest_id = await _insert_reveal_audit_row(
        pg_pool, actor="carol", revealed_at=base + dt.timedelta(hours=2)
    )

    app = _build_app(pg_pool, role=Role.ADMIN)
    async with await _client(app) as client:
        resp = await client.get("/audit/reveals-legacy")

    assert resp.status_code == 200
    body = resp.json()
    ids = [row["id"] for row in body]
    assert (
        ids.index(str(newest_id))
        < ids.index(str(middle_id))
        < ids.index(str(oldest_id))
    ), "expected newest-first (DESC) ordering by revealed_at"


@pytest.mark.asyncio
async def test_auditor_role_also_sees_ordered_rows(pg_pool: asyncpg.Pool) -> None:
    """ADR-019 §9.4 — auditor is a first-class reader of this endpoint, not
    just admin."""
    base = dt.datetime(2026, 7, 2, 9, 0, 0, tzinfo=dt.UTC)
    await _insert_reveal_audit_row(pg_pool, actor="dana", revealed_at=base)
    await _insert_reveal_audit_row(
        pg_pool, actor="erin", revealed_at=base + dt.timedelta(minutes=30)
    )

    app = _build_app(pg_pool, role=Role.AUDITOR)
    async with await _client(app) as client:
        resp = await client.get("/audit/reveals-legacy")

    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 2
    assert body[0]["actor"] == "erin"
    assert body[1]["actor"] == "dana"


# ── limit/offset pagination window — real LIMIT/OFFSET over a real order ──


@pytest.mark.asyncio
async def test_limit_and_offset_slice_the_real_ordered_result_set(
    pg_pool: asyncpg.Pool,
) -> None:
    base = dt.datetime(2026, 7, 5, 0, 0, 0, tzinfo=dt.UTC)
    actors = ["r0", "r1", "r2", "r3", "r4"]
    for i, actor in enumerate(actors):
        await _insert_reveal_audit_row(
            pg_pool, actor=actor, revealed_at=base + dt.timedelta(minutes=i)
        )
    # DESC order is r4, r3, r2, r1, r0. limit=2 offset=1 -> [r3, r2].
    app = _build_app(pg_pool, role=Role.ADMIN)
    async with await _client(app) as client:
        resp = await client.get(
            "/audit/reveals-legacy", params={"limit": 2, "offset": 1}
        )

    assert resp.status_code == 200
    body = resp.json()
    assert [row["actor"] for row in body] == ["r3", "r2"]


@pytest.mark.asyncio
async def test_offset_past_the_end_returns_empty_list(pg_pool: asyncpg.Pool) -> None:
    await _insert_reveal_audit_row(
        pg_pool,
        actor="solo",
        revealed_at=dt.datetime(2026, 7, 6, 0, 0, 0, tzinfo=dt.UTC),
    )
    app = _build_app(pg_pool, role=Role.ADMIN)
    async with await _client(app) as client:
        resp = await client.get(
            "/audit/reveals-legacy", params={"limit": 10, "offset": 50}
        )
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_default_pagination_returns_everything_under_the_cap(
    pg_pool: asyncpg.Pool,
) -> None:
    base = dt.datetime(2026, 7, 7, 0, 0, 0, tzinfo=dt.UTC)
    for i in range(3):
        await _insert_reveal_audit_row(
            pg_pool, actor=f"batch-{i}", revealed_at=base + dt.timedelta(minutes=i)
        )
    app = _build_app(pg_pool, role=Role.ADMIN)
    async with await _client(app) as client:
        resp = await client.get("/audit/reveals-legacy")
    assert resp.status_code == 200
    assert len(resp.json()) == 3


# ── Security-relevant: reads reveal_audit ALONE, no PII join ──────────────


@pytest.mark.asyncio
async def test_response_never_leaks_pii_via_a_resumes_join(
    pg_pool: asyncpg.Pool,
) -> None:
    """The core guarantee of ADR-019 §6's read-only historical viewer: it
    reads the frozen ``reveal_audit`` table alone. Seed a REAL resume with
    real (encrypted-at-rest) PII, point a ``reveal_audit`` row at it, and
    prove the endpoint's response contains neither a PII column key nor the
    plaintext candidate name/email anywhere in the raw response body."""
    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume_with_pii(
        pg_pool,
        job_id,
        name="Taylor Nakamura",
        email="taylor.nakamura@example.test",
    )
    await _insert_reveal_audit_row(
        pg_pool,
        resume_id=resume_id,
        job_id=job_id,
        actor="reviewer",
        context="checking for a duplicate",
        revealed_at=dt.datetime(2026, 7, 8, 0, 0, 0, tzinfo=dt.UTC),
    )

    app = _build_app(pg_pool, role=Role.ADMIN)
    async with await _client(app) as client:
        resp = await client.get("/audit/reveals-legacy")

    assert resp.status_code == 200
    raw_text = resp.text
    assert "Taylor Nakamura" not in raw_text
    assert "taylor.nakamura@example.test" not in raw_text

    body = resp.json()
    assert len(body) == 1
    row = body[0]
    assert row["resume_id"] == str(resume_id)
    assert row["actor"] == "reviewer"
    assert row["context"] == "checking for a duplicate"
    # Only reveal_audit's own columns — no PII column key leaks in even as a
    # null/empty value (which would still betray that a join happened).
    forbidden_keys = {
        "candidate_name",
        "candidate_email",
        "candidate_phone",
        "candidate_email_hash",
        "original_filename",
        "parsed",
    }
    assert forbidden_keys.isdisjoint(row.keys())


# ── Minimal role-gate smoke check against the real app wiring ─────────────


@pytest.mark.asyncio
async def test_recruiter_role_403s_against_the_real_route(
    pg_pool: asyncpg.Pool,
) -> None:
    await _insert_reveal_audit_row(
        pg_pool,
        actor="whoever",
        revealed_at=dt.datetime(2026, 7, 9, 0, 0, 0, tzinfo=dt.UTC),
    )
    app = _build_app(pg_pool, role=Role.RECRUITER)
    async with await _client(app) as client:
        resp = await client.get("/audit/reveals-legacy")
    assert resp.status_code == 403
