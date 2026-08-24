"""Integration tests — FU-5 slice 8 (ADR-019 §6/§7/§10b): the reveal ->
``audit_log`` cutover and the human-attribution gate, against a REAL Postgres
(testcontainers) via a real ASGI app.

What a REAL Postgres proves that ``test_route_reveal.py``'s mocked-conn route
tests structurally cannot:

* a successful reveal really inserts EXACTLY ONE ``audit_log`` row satisfying
  the real ``audit_log_actor_identity`` CHECK constraint (built at slice 1,
  first exercised by application code here) — not just that the route called
  some function with plausible arguments,
* the cutover itself: EXACTLY ZERO new ``reveal_audit`` rows are written on
  the reveal path any more, proven by a real row count against a real table,
* the ADR-016 ordering guarantee (audit write BEFORE decrypt, restated
  ADR-019 §7) really holds against a real connection — a decrypt failure
  AFTER a real, committed audit write does not roll the audit row back,
* ``reveal_audit`` (the table) and ``src.services.reveal_service`` (the
  module) are still present — slice 8 stops WRITING new rows to the sink, it
  does not delete it (ADR-019 §6's read-only migration posture).

Nothing new here exists yet (``src.services.audit_service``, and the route's
wiring of it), so every test below is expected to fail — most immediately at
`AttributeError`/`ImportError`, some (case 2's 403 gate) at an assertion on
today's still-200 fallback behaviour. RED half of the TDD cycle.

Follows the exact CAS-session/testcontainers wiring already used in
``tests/integration/test_auth_routes_pg.py`` and
``tests/integration/test_identity_created_by_pg.py`` (real session cookie via
the slice-6 validate flow, with only CAS's own network call —
``auth_routes.cas_service.validate_ticket`` — mocked), plus the encrypted-PII
seeding helper from ``tests/integration/test_resume_read_pg.py``. No new
harness.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator, Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import asyncpg
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from testcontainers.postgres import PostgresContainer

from src.api import deps
from src.api.deps import Role, resolve_role
from src.api.routes import auth as auth_routes
from src.api.routes import resumes as resumes_routes
from src.errors import AppError
from src.models.ddl import init_schema
from src.models.pool import get_db
from src.services.pii import encrypt as pii_encrypt
from src.services.pii import set_pii_key
from src.settings import Settings

TEST_PII_KEY = "integration-test-pii-key-do-not-use-in-prod"
DEFAULT_ADMIN = "asalah"


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
    fake_settings = SimpleNamespace(pii_key=TEST_PII_KEY)
    with patch("src.services.pii.get_settings", return_value=fake_settings):
        yield


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "cas_enabled": True,
        "cas_server_url": "https://cas.example.edu/cas",
        "session_cookie_name": "ra_session",
        "session_ttl_hours": 8,
        "default_admin_cas_username": DEFAULT_ADMIN,
        "skill_hash_salt": "test-salt",
        "pii_key": TEST_PII_KEY,
    }
    base.update(overrides)
    return Settings(**base)


def _unique_username(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _mock_validate_ticket(username: str) -> AsyncMock:
    """Stands in for the real ``httpx`` round trip to the CAS server — the
    one genuinely-external boundary these tests mock."""
    return AsyncMock(return_value=username)


async def _insert_user_with_role(
    pool: asyncpg.Pool, *, cas_username: str, role: str = "recruiter"
) -> None:
    """Seed a ``users`` row with an EXPLICIT role BEFORE the CAS login for
    the SAME username, so ``provision_or_get``'s ``ON CONFLICT`` path (which
    deliberately never touches ``role``, see ``user_service.py``) leaves it
    in place. Needed as of `fix/session-role-on-writes` (ADR-033):
    ``require_session_role(*_REVEALERS)`` (admin, recruiter) now 403s a real
    session whose role is neither — these two tests need reveal to actually
    SUCCEED so the audit-actor/ordering plumbing can be observed; neither was
    ever testing authorization itself (see the reveal-403 tests elsewhere in
    this file, and ``test_route_resumes_session_gate.py``)."""
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (cas_username, display_name, email, role) "
            "VALUES ($1, $2, $3, $4)",
            cas_username,
            cas_username,
            None,
            role,
        )


def _build_app(pool: asyncpg.Pool) -> FastAPI:
    app = FastAPI()
    app.include_router(auth_routes.router)
    app.include_router(resumes_routes.router)

    async def _get_db_override() -> AsyncIterator[Any]:
        async with pool.acquire() as conn:
            yield conn

    app.dependency_overrides[get_db] = _get_db_override
    # RBAC is orthogonal to this slice's identity/audit-sink switch.
    app.dependency_overrides[resolve_role] = lambda: Role.ADMIN

    @app.exception_handler(AppError)
    async def _app_error_handler(_request: Any, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status, content={"detail": exc.message, "code": exc.code}
        )

    return app


async def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _login_and_get_sid(
    client: AsyncClient, settings: Settings, username: str
) -> str:
    resp = await client.get("/auth/cas/validate", params={"ticket": "any-ticket"})
    assert resp.status_code == 302
    sid = resp.cookies.get(settings.session_cookie_name)
    assert sid is not None
    return sid


async def _insert_job(pool: asyncpg.Pool) -> uuid.UUID:
    async with pool.acquire() as conn:
        job_id: uuid.UUID = await conn.fetchval(
            "INSERT INTO jobs (title, description_raw) VALUES ($1, $2) RETURNING id",
            "Senior Backend Engineer",
            "raw jd text " * 10,
        )
    return job_id


async def _insert_resume(
    pool: asyncpg.Pool, job_id: uuid.UUID, *, name: str = "Casey Rivera"
) -> uuid.UUID:
    email = f"{name.split()[0].lower()}@example.test"
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


async def _audit_log_reveal_rows(
    pool: asyncpg.Pool, resume_id: uuid.UUID
) -> list[asyncpg.Record]:
    async with pool.acquire() as conn:
        return await conn.fetch(
            "SELECT actor_kind, actor_user_id, actor_service, action, "
            "subject_type, subject_id FROM audit_log "
            "WHERE subject_id = $1 AND action = 'reveal'",
            resume_id,
        )


async def _reveal_audit_row_count(pool: asyncpg.Pool, resume_id: uuid.UUID) -> int:
    async with pool.acquire() as conn:
        count: int = await conn.fetchval(
            "SELECT count(*) FROM reveal_audit WHERE resume_id = $1", resume_id
        )
    return count


# ── Case 1: a real human session -> one audit_log row, zero reveal_audit ──


@pytest.mark.asyncio
async def test_reveal_case1_human_session_writes_one_audit_log_row_and_no_reveal_audit(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(cas_enabled=True)
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    username = _unique_username("priya")
    monkeypatch.setattr(
        auth_routes.cas_service, "validate_ticket", _mock_validate_ticket(username)
    )
    await _insert_user_with_role(pg_pool, cas_username=username)
    app = _build_app(pg_pool)
    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume(pg_pool, job_id, name="Jane Smith")

    async with await _client(app) as client:
        sid = await _login_and_get_sid(client, settings, username)
        resp = await client.post(
            f"/resumes/{resume_id}/reveal",
            cookies={settings.session_cookie_name: sid},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["candidate"]["name"] == "Jane Smith"

    async with pg_pool.acquire() as conn:
        user_id = await conn.fetchval(
            "SELECT id FROM users WHERE cas_username = $1", username
        )
    assert user_id is not None

    rows = await _audit_log_reveal_rows(pg_pool, resume_id)
    assert len(rows) == 1, "exactly one audit_log row for this reveal"
    row = rows[0]
    assert row["actor_kind"] == "user"
    assert row["actor_user_id"] == user_id
    assert row["actor_service"] is None
    assert row["subject_type"] == "resume"
    assert row["subject_id"] == resume_id

    # The reveal -> audit_log cutover: NOT ONE new reveal_audit row.
    assert await _reveal_audit_row_count(pg_pool, resume_id) == 0


# ── Case 3: CAS disabled -> service-actor audit_log row ────────────────────


@pytest.mark.asyncio
async def test_reveal_case3_cas_disabled_writes_service_actor_audit_log_row(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(cas_enabled=False)
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    app = _build_app(pg_pool)
    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume(pg_pool, job_id, name="Jordan Lee")

    async with await _client(app) as client:
        resp = await client.post(f"/resumes/{resume_id}/reveal")

    assert resp.status_code == 200
    assert resp.json()["candidate"]["name"] == "Jordan Lee"

    rows = await _audit_log_reveal_rows(pg_pool, resume_id)
    assert len(rows) == 1
    row = rows[0]
    assert row["actor_kind"] == "service"
    assert row["actor_service"] == "dev-anonymous"
    assert row["actor_user_id"] is None

    assert await _reveal_audit_row_count(pg_pool, resume_id) == 0


# ── Case 2: CAS enabled, no session -> 403, no audit row at all ───────────


@pytest.mark.asyncio
async def test_reveal_case2_cas_enabled_no_session_403s_and_writes_no_audit_row(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(cas_enabled=True)
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    app = _build_app(pg_pool)
    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume(pg_pool, job_id, name="Alex Chen")

    async with await _client(app) as client:
        resp = await client.post(f"/resumes/{resume_id}/reveal")

    assert resp.status_code == 403
    assert await _reveal_audit_row_count(pg_pool, resume_id) == 0
    rows = await _audit_log_reveal_rows(pg_pool, resume_id)
    assert rows == []


# ── Ordering guarantee against a REAL connection ────────────────────────


@pytest.mark.asyncio
async def test_reveal_audit_row_survives_a_decrypt_failure_after_it_commits(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-016's ordering guarantee, restated ADR-019 §7: the audit write
    happens BEFORE decryption, so a crash during reveal does not leave an
    audit gap. Proved here against a REAL connection (unprovable by a mocked
    one): patch ONLY the decrypt/read step (``resume_service.get_one``) to
    raise AFTER the real, unpatched ``audit_service.record_audit`` has
    already executed and committed its INSERT — the row must still be there
    once the exception has propagated."""
    settings = _settings(cas_enabled=True)
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    username = _unique_username("morgan")
    monkeypatch.setattr(
        auth_routes.cas_service, "validate_ticket", _mock_validate_ticket(username)
    )
    await _insert_user_with_role(pg_pool, cas_username=username)
    monkeypatch.setattr(
        resumes_routes.resume_service,
        "get_one",
        AsyncMock(side_effect=RuntimeError("simulated decrypt crash")),
    )
    app = _build_app(pg_pool)
    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume(pg_pool, job_id, name="Sam Okafor")

    async with await _client(app) as client:
        sid = await _login_and_get_sid(client, settings, username)
        with pytest.raises(RuntimeError):
            await client.post(
                f"/resumes/{resume_id}/reveal",
                cookies={settings.session_cookie_name: sid},
            )

    rows = await _audit_log_reveal_rows(pg_pool, resume_id)
    assert len(rows) == 1, "the audit row must survive a crash in the decrypt step"


# ── reveal_service / reveal_audit are KEPT, not deleted (ADR-019 §6) ──────


@pytest.mark.asyncio
async def test_reveal_audit_table_still_exists(pg_pool: asyncpg.Pool) -> None:
    async with pg_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        )
    tables = {row["tablename"] for row in rows}
    assert "reveal_audit" in tables


@pytest.mark.asyncio
async def test_reveal_service_module_still_importable_and_writes_a_row_directly(
    pg_pool: asyncpg.Pool,
) -> None:
    """Slice 8 stops the REVEAL ROUTE from calling ``record_reveal`` — it does
    not delete the module or the table. Calling it directly must still work
    against the real schema."""
    from src.services.reveal_service import record_reveal

    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume(pg_pool, job_id, name="Legacy Writer")

    async with pg_pool.acquire() as conn:
        audit_id = await record_reveal(
            conn, resume_id=resume_id, actor="direct-call", context="legacy path"
        )

    assert await _reveal_audit_row_count(pg_pool, resume_id) == 1
    async with pg_pool.acquire() as conn:
        stored_id = await conn.fetchval(
            "SELECT id FROM reveal_audit WHERE resume_id = $1", resume_id
        )
    assert stored_id == audit_id
