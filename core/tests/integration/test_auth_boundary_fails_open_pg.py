"""Integration tests — fix/auth-boundary-fails-open — the exploit chain a
live security audit proved end-to-end against a real, running stack, closed
here against a REAL Postgres (testcontainers) via a real ASGI app mounting
the real ``jobs`` router exactly as ``src.api.main`` wires it.

**The proven defect.** No ``API_KEY_*`` is configured anywhere in a real
deploy of this codebase (0 matches in ``docker-compose.yml``, ``compose.cas
.yml``, ``.env.example``, or the running container). So
``Settings.auth_enabled`` is ``False`` and ``src.api.deps.resolve_role``
resolves ``Role.ADMIN`` for EVERY request, key or no key
(``deps.py``). ``require_role(*_JOB_WRITERS)`` therefore always passes.
The SECOND gate on every write route, ``require_session_role(*_JOB_WRITERS)``
(ADR-033), was ADDITIONALLY designed to pass whenever ``resolve_user()``
resolves ``None`` — "this gate never judges the ABSENCE of a session" — on
the theory that some write routes (like this one) are not human-only.
Combined, a cookie-less, key-less caller sailed through BOTH gates with no
credential whatsoever. Proved end-to-end against a real running stack: a
bare ``PATCH /jobs/{id} {"blind_review": false}`` returned 200 and really
flipped the column, audited to ``actor_kind='service'`` /
``actor_service='api'`` — unattributable to any person. The same anonymous
caller could then read ``candidate_name`` and the un-redacted filename
through any legitimate auditor/recruiter session, because the column really
flipped.

**The human decision this slice enforces.** A valid API key (or the absence
of one, under auth-disabled local dev) is NEVER sufficient on its own —
every write route now requires a REAL, resolvable CAS session.
``require_session_role`` must 403 on ``resolve_user() is None``, the same as
an out-of-allowed-set session role.

Deliberately reproduces the REAL vulnerable configuration rather than a
synthetic one: ``resolve_role`` is exercised for REAL (not overridden to a
fixed role, unlike most other integration harnesses in this directory) with
EVERY ``api_key_*`` field left empty — exactly what ships today — so a
missing fix here would 200 for the exact reason the live audit found, not a
different one. The original probe script that first proved this live is not
part of this repo (it was a throwaway audit tool against a running
container); this file is the durable, from-scratch regression test for the
same chain.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from testcontainers.postgres import PostgresContainer

from src.api import deps
from src.api.routes import auth as auth_routes
from src.api.routes import jobs as jobs_routes
from src.errors import AppError
from src.models.ddl import init_schema
from src.models.pool import get_db
from src.settings import Settings

_JD = "We need a senior backend engineer. " * 3


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


def _vulnerable_settings(**overrides: Any) -> Settings:
    """The REAL shipped shape: CAS enabled (a real deploy, not single-
    operator local dev) and every ``api_key_*`` field left at its EMPTY
    default — ``Settings()``'s own default, not a test convenience. Do NOT
    add an ``api_key_*`` override here; that would stop reproducing the
    live finding."""
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


def _mock_validate_ticket(username: str) -> AsyncMock:
    return AsyncMock(return_value=username)


async def _insert_user_with_role(
    pool: asyncpg.Pool, *, cas_username: str, role: str = "recruiter"
) -> None:
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
    """Mounts the REAL ``jobs``/``auth`` routers with NO ``resolve_role`` or
    ``resolve_user`` dependency override — every gate on the write route
    runs for real against whatever ``Settings`` the test installs via
    ``deps.get_settings``/``auth_routes.get_settings``, exactly as
    ``src.api.main`` wires the same router."""
    app = FastAPI()
    app.include_router(auth_routes.router)
    app.include_router(jobs_routes.router)

    async def _get_db_override() -> AsyncIterator[Any]:
        async with pool.acquire() as conn:
            yield conn

    app.dependency_overrides[get_db] = _get_db_override
    app.dependency_overrides[deps.get_arq] = lambda: MagicMock(enqueue_job=AsyncMock())

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


async def _insert_job(pool: asyncpg.Pool, *, blind_review: bool = True) -> uuid.UUID:
    async with pool.acquire() as conn:
        job_id: uuid.UUID = await conn.fetchval(
            "INSERT INTO jobs (title, description_raw, blind_review) "
            "VALUES ($1, $2, $3) RETURNING id",
            "Senior Backend Engineer",
            _JD,
            blind_review,
        )
    return job_id


async def _job_blind_review(pool: asyncpg.Pool, job_id: uuid.UUID) -> bool:
    async with pool.acquire() as conn:
        value: bool = await conn.fetchval(
            "SELECT blind_review FROM jobs WHERE id = $1", job_id
        )
    return value


async def _audit_row_count(pool: asyncpg.Pool, job_id: uuid.UUID) -> int:
    async with pool.acquire() as conn:
        count: int = await conn.fetchval(
            "SELECT count(*) FROM audit_log WHERE subject_id = $1 "
            "AND action = 'blind_review_toggled'",
            job_id,
        )
    return count


# ── THE exploit chain: cookie-less + key-less must NOT flip the column ────


@pytest.mark.asyncio
async def test_patch_job_blind_review_with_no_cookie_and_no_api_key_does_not_flip(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE proof this vulnerability is closed, not merely gated differently.
    Reproduces the live audit's probe exactly: no ``X-API-Key`` header, no
    session cookie, against the REAL shipped ``Settings`` shape (CAS
    enabled, every ``api_key_*`` empty). Must 403 and the column must stay
    exactly as seeded — no partial/silent mutation."""
    settings = _vulnerable_settings()
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    app = _build_app(pg_pool)
    job_id = await _insert_job(pg_pool, blind_review=True)

    async with await _client(app) as client:
        resp = await client.patch(f"/jobs/{job_id}", json={"blind_review": False})

    assert resp.status_code == 403
    assert await _job_blind_review(pg_pool, job_id) is True
    assert await _audit_row_count(pg_pool, job_id) == 0


@pytest.mark.asyncio
async def test_patch_job_blind_review_with_no_cookie_and_no_api_key_writes_no_audit_row(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Belt-and-suspenders on the same probe: not even an unattributable
    ``actor_kind='service'`` audit row may be written for a rejected
    request — a 403 must mean nothing happened at all."""
    settings = _vulnerable_settings()
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    app = _build_app(pg_pool)
    job_id = await _insert_job(pg_pool, blind_review=True)

    async with await _client(app) as client:
        await client.patch(f"/jobs/{job_id}", json={"blind_review": False})

    async with pg_pool.acquire() as conn:
        rows = await conn.fetch("SELECT 1 FROM audit_log WHERE subject_id = $1", job_id)
    assert rows == []


@pytest.mark.asyncio
async def test_patch_job_blind_review_with_a_bare_x_api_key_header_still_does_not_flip(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The human decision, pinned directly: presenting SOME ``X-API-Key``
    value (even a garbage one — auth is disabled, so ``resolve_role`` never
    even looks at it) is still not a session, and must not substitute for
    one."""
    settings = _vulnerable_settings()
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    app = _build_app(pg_pool)
    job_id = await _insert_job(pg_pool, blind_review=True)

    async with await _client(app) as client:
        resp = await client.patch(
            f"/jobs/{job_id}",
            json={"blind_review": False},
            headers={"X-API-Key": "whatever-anyone-typed-in"},
        )

    assert resp.status_code == 403
    assert await _job_blind_review(pg_pool, job_id) is True


# ── the fix must not break the two LEGITIMATE paths ────────────────────────


@pytest.mark.asyncio
async def test_patch_job_blind_review_with_a_real_recruiter_session_still_succeeds(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Positive control: this is not a blanket 403 — a REAL, resolvable CAS
    session with an allowed role still flips the column exactly as before."""
    settings = _vulnerable_settings()
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    username = _unique_username("priya")
    monkeypatch.setattr(
        auth_routes.cas_service, "validate_ticket", _mock_validate_ticket(username)
    )
    await _insert_user_with_role(pg_pool, cas_username=username, role="recruiter")
    app = _build_app(pg_pool)
    job_id = await _insert_job(pg_pool, blind_review=True)

    async with await _client(app) as client:
        sid = await _login_and_get_sid(client, settings, username)
        resp = await client.patch(
            f"/jobs/{job_id}",
            json={"blind_review": False},
            cookies={settings.session_cookie_name: sid},
        )

    assert resp.status_code == 200
    assert await _job_blind_review(pg_pool, job_id) is False
    assert await _audit_row_count(pg_pool, job_id) == 1


@pytest.mark.asyncio
async def test_patch_job_blind_review_cas_disabled_dev_anonymous_still_succeeds(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The single-operator local-dev boot (``cas_enabled=False``) must keep
    working exactly as before: the synthetic dev-anonymous sentinel always
    resolves a real (non-``None``) admin ``User``, so it is never subject to
    the ``user is None`` 403 this slice adds."""
    settings = _vulnerable_settings(cas_enabled=False)
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    app = _build_app(pg_pool)
    job_id = await _insert_job(pg_pool, blind_review=True)

    async with await _client(app) as client:
        resp = await client.patch(f"/jobs/{job_id}", json={"blind_review": False})

    assert resp.status_code == 200
    assert await _job_blind_review(pg_pool, job_id) is False


__all__: list[str] = []
