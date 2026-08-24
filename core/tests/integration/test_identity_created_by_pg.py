"""Integration tests — FU-5 slice 7 (ADR-019 §8.3/§9.2): ``jobs.created_by``
and ``resumes.uploaded_by`` sourced from the REAL CAS session identity
against a REAL Postgres (testcontainers), not the retired ``X-Actor-Name``
header.

What a REAL Postgres proves that the mocked-conn route tests
(``test_route_jobs.py`` / ``test_route_resumes.py``) structurally cannot: the
NULL-vs-``cas_username`` distinction in an ACTUAL committed row. A mocked
``conn.fetchrow``/``conn.execute`` can only prove which Python value the
route PASSED to the SQL call; it cannot prove that value round-trips through
a real ``TEXT`` column as either a real ``NULL`` or a real string, nor that
the DDL's nullable ``created_by``/``uploaded_by`` columns actually accept
both. This file wires the real ``auth`` router (from slice 6) alongside the
real ``jobs``/``resumes`` routers on ONE app so a session minted by
``GET /auth/cas/validate`` really flows through ``resolve_user`` into a
really-committed job/résumé row — the exact chain
``test_auth_routes_pg.py`` already proves for ``resolve_user`` alone, and
``test_api_jobs_pg.py``/``test_api_resumes_pg.py`` already prove for
job/résumé creation alone. This file is the first to prove them TOGETHER.

The one boundary mocked, mirroring ``test_auth_routes_pg.py``: CAS's own
network call (``auth_routes.cas_service.validate_ticket``), standing in for
the real round trip to ``cas.sfu.ca``. ``resolve_role`` is bypassed to
``Role.ADMIN`` throughout — the key→role RBAC layer is orthogonal to this
slice's identity-SOURCE switch and is exercised elsewhere.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from testcontainers.postgres import PostgresContainer

from src.api import deps
from src.api.deps import Role, get_arq, resolve_role
from src.api.routes import auth as auth_routes
from src.api.routes import jobs as jobs_routes
from src.api.routes import resumes as resumes_routes
from src.errors import AppError
from src.models.ddl import init_schema
from src.models.pool import get_db
from src.settings import Settings
from src.storage.blob_store import BlobStore, get_blob_store

_JD = "We need a senior backend engineer. " * 3
_PDF_MAGIC = b"%PDF-1.4\nresume content\n" + b"x" * 500


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
            "TRUNCATE sessions, users, audit_log, jobs, resumes, outbox CASCADE"
        )
    try:
        yield pool
    finally:
        await pool.close()


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "cas_enabled": True,
        "cas_server_url": "https://cas.example.edu/cas",
        "session_cookie_name": "ra_session",
        "session_ttl_hours": 8,
        "default_admin_cas_username": "asalah",
        "skill_hash_salt": "test-salt",
        "pii_key": "test-key",
    }
    base.update(overrides)
    return Settings(**base)


def _unique_username(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


async def _insert_user_with_role(
    pool: asyncpg.Pool, *, cas_username: str, role: str = "recruiter"
) -> None:
    """Seed a ``users`` row with an EXPLICIT role BEFORE the CAS login for
    the SAME username, so ``provision_or_get``'s ``ON CONFLICT`` path (which
    deliberately never touches ``role``, see ``user_service.py``) leaves it
    in place. Needed as of `fix/session-role-on-writes` (ADR-033):
    ``require_session_role`` now 403s a real session whose role is not
    admin/recruiter, and this file's whole point — proving ``created_by``/
    ``uploaded_by`` are sourced from the resolved session identity — needs
    the write to actually succeed so the identity plumbing can be observed;
    it was never testing authorization itself (see
    ``test_route_{jobs,resumes}_session_gate.py`` for that)."""
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (cas_username, display_name, email, role) "
            "VALUES ($1, $2, $3, $4)",
            cas_username,
            cas_username,
            None,
            role,
        )


def _mock_validate_ticket(username: str) -> AsyncMock:
    """Stands in for the real ``httpx`` round trip to the CAS server — the
    one genuinely-external boundary these tests mock."""
    return AsyncMock(return_value=username)


def _build_app(
    pool: asyncpg.Pool, *, store: BlobStore, arq: MagicMock | None = None
) -> FastAPI:
    app = FastAPI()
    app.include_router(auth_routes.router)
    app.include_router(jobs_routes.router)
    app.include_router(resumes_routes.router)

    async def _get_db_override() -> AsyncIterator[Any]:
        async with pool.acquire() as conn:
            yield conn

    app.dependency_overrides[get_db] = _get_db_override
    app.dependency_overrides[get_blob_store] = lambda: store
    app.dependency_overrides[get_arq] = lambda: arq or MagicMock(
        enqueue_job=AsyncMock()
    )
    # RBAC is orthogonal to this slice's identity-source switch.
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


# ── jobs.created_by ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_job_with_a_real_session_sets_created_by_to_the_cas_username(
    pg_pool: asyncpg.Pool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings()
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    username = _unique_username("erin")
    monkeypatch.setattr(
        auth_routes.cas_service, "validate_ticket", _mock_validate_ticket(username)
    )
    await _insert_user_with_role(pg_pool, cas_username=username)
    store = BlobStore(str(tmp_path))
    app = _build_app(pg_pool, store=store)

    async with await _client(app) as client:
        sid = await _login_and_get_sid(client, settings, username)
        create_resp = await client.post(
            "/jobs",
            json={"title": "Senior Backend Engineer", "description_raw": _JD},
            cookies={settings.session_cookie_name: sid},
        )

    assert create_resp.status_code == 201
    job_id = create_resp.json()["id"]
    async with pg_pool.acquire() as conn:
        created_by = await conn.fetchval(
            "SELECT created_by FROM jobs WHERE id = $1", uuid.UUID(job_id)
        )
    assert created_by == username


@pytest.mark.asyncio
async def test_create_job_with_no_session_403s_and_creates_no_row(
    pg_pool: asyncpg.Pool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REVERSED (security finding, fix/auth-boundary-fails-open) — this test
    used to pin ``created_by = NULL`` on a 201 for CAS enabled with no
    cookie presented at all (a bare service-key caller, ``resolve_user`` ->
    ``None``). A live audit against real Postgres proved that combination
    (this deploy's real config ships with zero ``API_KEY_*``, so
    ``resolve_role`` trivially resolves ``Role.ADMIN`` for every caller) let
    a cookie-less, key-less request reach every write route with no
    credential whatsoever — end-to-end proof is
    ``tests/integration/test_auth_boundary_fails_open_pg.py``. The human
    decision: a valid API key is NOT sufficient on its own — every write
    route now requires a REAL, resolvable CAS session, so this must 403 and
    create NO row at all, not a NULL-``created_by`` row."""
    settings = _settings()
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    store = BlobStore(str(tmp_path))
    app = _build_app(pg_pool, store=store)

    async with await _client(app) as client:
        create_resp = await client.post(
            "/jobs",
            json={"title": "Staff Engineer", "description_raw": _JD},
        )

    assert create_resp.status_code == 403
    async with pg_pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT count(*) FROM jobs WHERE title = $1", "Staff Engineer"
        )
    assert count == 0


@pytest.mark.asyncio
async def test_create_job_with_no_session_403s_even_with_an_x_actor_name_header(
    pg_pool: asyncpg.Pool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REVERSED (security finding, fix/auth-boundary-fails-open) — this test
    used to prove a leftover ``X-Actor-Name`` header could not populate
    ``created_by`` on an otherwise-succeeding no-session create. That create
    no longer succeeds at all (see the sibling test above), so this now
    proves the stronger property: the header cannot be used to sneak past
    the new session gate either — still 403, still no row, regardless of
    what the header claims."""
    settings = _settings()
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    store = BlobStore(str(tmp_path))
    app = _build_app(pg_pool, store=store)

    async with await _client(app) as client:
        create_resp = await client.post(
            "/jobs",
            json={"title": "Product Manager", "description_raw": _JD},
            headers={"X-Actor-Name": "totally-ignored@example.test"},
        )

    assert create_resp.status_code == 403
    async with pg_pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT count(*) FROM jobs WHERE title = $1", "Product Manager"
        )
    assert count == 0


# ── resumes.uploaded_by ─────────────────────────────────────────────────


async def _insert_job(pool: asyncpg.Pool) -> uuid.UUID:
    async with pool.acquire() as conn:
        job_id: uuid.UUID = await conn.fetchval(
            "INSERT INTO jobs (title, description_raw) VALUES ($1, $2) RETURNING id",
            "Senior Backend Engineer",
            "raw jd text " * 10,
        )
    return job_id


@pytest.mark.asyncio
async def test_upload_resume_with_a_real_session_sets_uploaded_by_to_the_cas_username(
    pg_pool: asyncpg.Pool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings()
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    username = _unique_username("dana")
    monkeypatch.setattr(
        auth_routes.cas_service, "validate_ticket", _mock_validate_ticket(username)
    )
    await _insert_user_with_role(pg_pool, cas_username=username)
    store = BlobStore(str(tmp_path))
    app = _build_app(pg_pool, store=store)
    job_id = await _insert_job(pg_pool)

    async with await _client(app) as client:
        sid = await _login_and_get_sid(client, settings, username)
        upload_resp = await client.post(
            f"/jobs/{job_id}/resumes",
            files=[("files", ("resume.pdf", _PDF_MAGIC, "application/pdf"))],
            data={"consent_acknowledged": "true"},
            cookies={settings.session_cookie_name: sid},
        )

    assert upload_resp.status_code == 202
    resume_id = upload_resp.json()[0]["resume_id"]
    async with pg_pool.acquire() as conn:
        uploaded_by = await conn.fetchval(
            "SELECT uploaded_by FROM resumes WHERE id = $1", uuid.UUID(resume_id)
        )
    assert uploaded_by == username


@pytest.mark.asyncio
async def test_upload_resume_with_no_session_403s_and_creates_no_row(
    pg_pool: asyncpg.Pool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REVERSED (security finding, fix/auth-boundary-fails-open) — this test
    used to pin ``uploaded_by = NULL`` on a 202 for CAS enabled with no
    session cookie (a bare service-key caller, ``resolve_user`` -> ``None``).
    A live audit against real Postgres proved that combination (this
    deploy's real config ships with zero ``API_KEY_*``, so ``resolve_role``
    trivially resolves ``Role.ADMIN`` for every caller) let a cookie-less,
    key-less request reach every write route with no credential whatsoever
    — end-to-end proof is
    ``tests/integration/test_auth_boundary_fails_open_pg.py``. The human
    decision: a valid API key is NOT sufficient on its own — every write
    route now requires a REAL, resolvable CAS session, so this must 403 and
    create NO résumé row at all, not a NULL-``uploaded_by`` row."""
    settings = _settings()
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    store = BlobStore(str(tmp_path))
    app = _build_app(pg_pool, store=store)
    job_id = await _insert_job(pg_pool)

    async with await _client(app) as client:
        upload_resp = await client.post(
            f"/jobs/{job_id}/resumes",
            files=[("files", ("resume.pdf", _PDF_MAGIC, "application/pdf"))],
            data={"consent_acknowledged": "true"},
        )

    assert upload_resp.status_code == 403
    async with pg_pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT count(*) FROM resumes WHERE job_id = $1", job_id
        )
    assert count == 0
