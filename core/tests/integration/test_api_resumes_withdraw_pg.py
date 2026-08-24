"""Integration tests — ADR-026 (FU-8) résumé-withdrawal lifecycle routes
against a REAL Postgres (testcontainers) via a real ASGI app:

* ``POST /resumes/{id}/withdraw``
* ``POST /resumes/{id}/reinstate``
* ``GET /jobs/{id}/resume-status``

What a REAL Postgres proves that the mocked-conn route tests
(``test_route_resumes_withdraw.py``) cannot: the 404 for a nonexistent
résumé comes from a REAL existence check (not a hand-fed mock), the 200
response really round-trips through ``resume_service.get_one`` against a
real row, and the status-breakdown route really executes a real
``GROUP BY`` against real seeded résumés.

None of these routes exist yet — every test below fails at the first
request (404 from FastAPI's own router, since the endpoint is not
registered) or an ``AttributeError``. RED half of the TDD cycle.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import asyncpg
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from testcontainers.postgres import PostgresContainer

from src.api.deps import Role, resolve_role
from src.api.routes import resumes as resumes_routes
from src.errors import AppError
from src.models.ddl import init_schema
from src.models.pool import get_db

_NON_WRITER_ROLES: tuple[Role, ...] = (Role.HIRING_MANAGER, Role.AUDITOR)
_ALL_ROLES: tuple[Role, ...] = tuple(Role)


@pytest.fixture(scope="session")
def pg_dsn() -> Iterator[str]:
    with PostgresContainer("postgres:16-alpine") as pg:
        yield re.sub(r"\+\w+", "", pg.get_connection_url(), count=1)


@pytest.fixture
async def pg_pool(pg_dsn: str) -> AsyncIterator[asyncpg.Pool]:
    pool = await asyncpg.create_pool(dsn=pg_dsn, min_size=2, max_size=8)
    await init_schema(pool)
    async with pool.acquire() as conn:
        await conn.execute("TRUNCATE jobs, resumes, outbox, audit_log CASCADE")
    try:
        yield pool
    finally:
        await pool.close()


async def _insert_job(pool: asyncpg.Pool) -> uuid.UUID:
    async with pool.acquire() as conn:
        job_id: uuid.UUID = await conn.fetchval(
            "INSERT INTO jobs (title, description_raw) VALUES ($1, $2) RETURNING id",
            "Senior Backend Engineer",
            "raw jd text",
        )
    return job_id


async def _insert_resume(
    pool: asyncpg.Pool, job_id: uuid.UUID, *, status: str = "parsed"
) -> uuid.UUID:
    async with pool.acquire() as conn:
        resume_id: uuid.UUID = await conn.fetchval(
            """
            INSERT INTO resumes (
                job_id, blob_key, original_filename, mime_type,
                file_size_bytes, sha256, consent_acknowledged, status
            ) VALUES ($1, $2, 'resume.pdf', 'application/pdf', 1024, $3, TRUE, $4)
            RETURNING id
            """,
            job_id,
            f"resumes/{uuid.uuid4().hex}.pdf",
            uuid.uuid4().hex,
            status,
        )
    return resume_id


def _build_app(pool: asyncpg.Pool, *, role: Role = Role.ADMIN) -> FastAPI:
    app = FastAPI()
    app.include_router(resumes_routes.router)

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


# ── POST /resumes/{id}/withdraw ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_withdraw_a_real_resume_returns_200_and_sets_withdrawn_at(
    pg_pool: asyncpg.Pool,
) -> None:
    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume(pg_pool, job_id)
    app = _build_app(pg_pool, role=Role.ADMIN)

    async with await _client(app) as client:
        resp = await client.post(
            f"/resumes/{resume_id}/withdraw", json={"reason": "took another offer"}
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["withdrawn_at"] is not None
    assert body["withdrawal_reason"] == "took another offer"

    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT withdrawn_at FROM resumes WHERE id = $1", resume_id
        )
    assert row is not None
    assert row["withdrawn_at"] is not None


@pytest.mark.parametrize("role", _NON_WRITER_ROLES)
@pytest.mark.asyncio
async def test_withdraw_403s_for_hiring_manager_and_auditor(
    role: Role, pg_pool: asyncpg.Pool
) -> None:
    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume(pg_pool, job_id)
    app = _build_app(pg_pool, role=role)

    async with await _client(app) as client:
        resp = await client.post(f"/resumes/{resume_id}/withdraw", json={})

    assert resp.status_code == 403

    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT withdrawn_at FROM resumes WHERE id = $1", resume_id
        )
    assert row is not None
    assert row["withdrawn_at"] is None, "a 403 must never apply the flip"


@pytest.mark.asyncio
async def test_withdraw_404_for_a_real_nonexistent_resume(
    pg_pool: asyncpg.Pool,
) -> None:
    app = _build_app(pg_pool, role=Role.ADMIN)

    async with await _client(app) as client:
        resp = await client.post(f"/resumes/{uuid.uuid4()}/withdraw", json={})

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_withdraw_422_for_an_oversize_reason(pg_pool: asyncpg.Pool) -> None:
    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume(pg_pool, job_id)
    app = _build_app(pg_pool, role=Role.ADMIN)

    async with await _client(app) as client:
        resp = await client.post(
            f"/resumes/{resume_id}/withdraw", json={"reason": "x" * 501}
        )

    assert resp.status_code == 422

    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT withdrawn_at FROM resumes WHERE id = $1", resume_id
        )
    assert row is not None
    assert row["withdrawn_at"] is None


@pytest.mark.asyncio
async def test_withdraw_twice_is_idempotent_both_200(pg_pool: asyncpg.Pool) -> None:
    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume(pg_pool, job_id)
    app = _build_app(pg_pool, role=Role.ADMIN)

    async with await _client(app) as client:
        first = await client.post(
            f"/resumes/{resume_id}/withdraw", json={"reason": "first"}
        )
        second = await client.post(
            f"/resumes/{resume_id}/withdraw", json={"reason": "second"}
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["withdrawn_at"] == second.json()["withdrawn_at"]
    assert (
        second.json()["withdrawal_reason"] == "first"
    ), "the second call must not overwrite the original reason"

    async with pg_pool.acquire() as conn:
        audit_count = await conn.fetchval(
            "SELECT count(*) FROM audit_log WHERE subject_id = $1 "
            "AND action = 'withdraw_resume'",
            resume_id,
        )
    assert audit_count == 1


# ── POST /resumes/{id}/reinstate ────────────────────────────────────────


@pytest.mark.asyncio
async def test_reinstate_a_withdrawn_resume_clears_the_columns(
    pg_pool: asyncpg.Pool,
) -> None:
    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume(pg_pool, job_id)
    app = _build_app(pg_pool, role=Role.ADMIN)

    async with await _client(app) as client:
        await client.post(f"/resumes/{resume_id}/withdraw", json={"reason": "oops"})
        resp = await client.post(f"/resumes/{resume_id}/reinstate")

    assert resp.status_code == 200
    body = resp.json()
    assert body["withdrawn_at"] is None
    assert body["withdrawal_reason"] is None


@pytest.mark.parametrize("role", _NON_WRITER_ROLES)
@pytest.mark.asyncio
async def test_reinstate_403s_for_hiring_manager_and_auditor(
    role: Role, pg_pool: asyncpg.Pool
) -> None:
    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume(pg_pool, job_id)

    admin_app = _build_app(pg_pool, role=Role.ADMIN)
    async with await _client(admin_app) as client:
        await client.post(f"/resumes/{resume_id}/withdraw", json={})

    app = _build_app(pg_pool, role=role)
    async with await _client(app) as client:
        resp = await client.post(f"/resumes/{resume_id}/reinstate")

    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_reinstate_404_for_a_real_nonexistent_resume(
    pg_pool: asyncpg.Pool,
) -> None:
    app = _build_app(pg_pool, role=Role.ADMIN)

    async with await _client(app) as client:
        resp = await client.post(f"/resumes/{uuid.uuid4()}/reinstate")

    assert resp.status_code == 404


# ── GET /jobs/{id}/resume-status ────────────────────────────────────────


@pytest.mark.parametrize("role", _ALL_ROLES)
@pytest.mark.asyncio
async def test_resume_status_breakdown_readable_by_every_role(
    role: Role, pg_pool: asyncpg.Pool
) -> None:
    job_id = await _insert_job(pg_pool)
    app = _build_app(pg_pool, role=role)

    async with await _client(app) as client:
        resp = await client.get(f"/jobs/{job_id}/resume-status")

    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_resume_status_breakdown_zero_resume_job_is_the_six_bucket_shape(
    pg_pool: asyncpg.Pool,
) -> None:
    job_id = await _insert_job(pg_pool)
    app = _build_app(pg_pool, role=Role.ADMIN)

    async with await _client(app) as client:
        resp = await client.get(f"/jobs/{job_id}/resume-status")

    assert resp.status_code == 200
    # FU-7 §4 (ADR-030): `degraded` is now a required sub-count of `parsed`.
    assert resp.json() == {
        "uploaded": 0,
        "parsing": 0,
        "parsed": 0,
        "failed": 0,
        "withdrawn": 0,
        "degraded": 0,
    }


@pytest.mark.asyncio
async def test_resume_status_breakdown_counts_real_seeded_rows(
    pg_pool: asyncpg.Pool,
) -> None:
    job_id = await _insert_job(pg_pool)
    await _insert_resume(pg_pool, job_id, status="uploaded")
    await _insert_resume(pg_pool, job_id, status="parsed")
    withdrawn_id = await _insert_resume(pg_pool, job_id, status="parsed")

    app = _build_app(pg_pool, role=Role.ADMIN)
    async with await _client(app) as client:
        await client.post(f"/resumes/{withdrawn_id}/withdraw", json={})
        resp = await client.get(f"/jobs/{job_id}/resume-status")

    assert resp.status_code == 200
    body = resp.json()
    assert body["uploaded"] == 1
    assert body["parsed"] == 1
    assert body["withdrawn"] == 1
