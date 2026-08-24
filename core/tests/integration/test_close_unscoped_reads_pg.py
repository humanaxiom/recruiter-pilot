"""Integration tests -- D2 = option B (``docs/OPEN_DECISIONS.md`` §D2,
answered by product 2026-08-19), against a REAL Postgres (testcontainers),
proving the router-level WIRING interaction a mocked ``resolve_user`` in
``tests/unit/test_api_deps_d2_close_unscoped_reads.py`` cannot: a real
request carrying only an ``X-API-Key`` and NO session cookie, through the
real business routers, mounted exactly the way ``src.api.main`` must wire
them (``require_role_assigned`` at ``include_router(...)`` time on
``audit``/``job_assignees``/``jobs``/``resumes``/``shortlist``).

**The decision.** ``require_role_assigned`` currently PASSES on
``user is None`` -- a bare API key with no CAS session gets unscoped reads
everywhere. Writes were already closed by ADR-034's ``require_session_role``.
Product decided reads must be symmetric: ``user is None`` now 403s there
too. The sharpest instance, and the one this file leads with, is a bare
ADMIN key reading ``GET /audit/reveals-legacy`` -- an unattributable read of
the audit log itself.

**What this file must NOT break, pinned as hard as the new rule itself:**

* the CAS-disabled dev boot (``resolve_user`` returns the synthetic
  ``dev-anonymous`` admin sentinel BEFORE the ``if not ra_session: return
  None`` branch even runs, so ``user`` is never ``None`` on that path --
  this is the behaviour most likely to be broken silently by a careless
  ``user is None`` check placed too early or duplicated elsewhere), and
* the Flask viewer's key+session pattern (``core/frontend/api_client.py``
  forwards the browser's ``ra_session`` cookie alongside the fixed
  recruiter key -- a keyed request that DOES carry a session must keep
  reading exactly as before).

Also pins that ``/auth/*`` and ``/users`` are unaffected -- neither is wired
behind ``require_role_assigned`` (``src.api.main``:88/96), and this decision
does not change that; a future session must not "fix" the asymmetry between
them without a separate decision.

Follows the exact fixture/harness shape already established in
``tests/integration/test_no_role_fail_closed_pg.py`` (asyncpg/testcontainers,
a local per-file ``_build_app`` mounting the real routers rather than
importing the lifespan-heavy ``src.api.main.app`` directly, and the real
``/auth/cas/validate`` route -- ticket verification mocked -- to provision a
real session cookie) -- no new harness, plus an ``api_key_admin`` addition
this file needs that the slice-3 file didn't, to reach the admin+auditor-only
``/audit/reveals-legacy`` route.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest
from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from testcontainers.postgres import PostgresContainer

from src.api import deps
from src.api.routes import audit as audit_routes
from src.api.routes import auth as auth_routes
from src.api.routes import job_assignees as job_assignees_routes
from src.api.routes import jobs as jobs_routes
from src.api.routes import resumes as resumes_routes
from src.api.routes import shortlist as shortlist_routes
from src.api.routes import users as users_routes
from src.errors import AppError
from src.models.ddl import init_schema
from src.models.pool import get_db
from src.settings import Settings

RECRUITER_KEY = "shared-recruiter-key-value-d2"
ADMIN_KEY = "admin-key-value-d2"


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
            "TRUNCATE sessions, users, audit_log, reveal_audit, jobs, outbox CASCADE"
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
        "default_admin_cas_username": "nobody-uses-this-default",
        "api_key_recruiter": RECRUITER_KEY,
        "api_key_admin": ADMIN_KEY,
        "skill_hash_salt": "test-salt",
        "pii_key": "test-key",
    }
    base.update(overrides)
    return Settings(**base)


def _unique_username(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _mock_validate_ticket(username: str) -> AsyncMock:
    return AsyncMock(return_value=username)


def _build_app(pool: asyncpg.Pool) -> FastAPI:
    """Mount every business router with ``require_role_assigned`` wired at
    the ROUTER level (the exact shape ``src.api.main`` uses), plus ``auth``
    and ``users`` UNGATED by it -- pinning that both stay reachable exactly
    as ``src.api.main`` wires them."""
    app = FastAPI()
    app.include_router(auth_routes.router)
    app.include_router(
        jobs_routes.router, dependencies=[Depends(deps.require_role_assigned)]
    )
    app.include_router(
        resumes_routes.router, dependencies=[Depends(deps.require_role_assigned)]
    )
    app.include_router(
        shortlist_routes.router, dependencies=[Depends(deps.require_role_assigned)]
    )
    app.include_router(
        job_assignees_routes.router,
        dependencies=[Depends(deps.require_role_assigned)],
    )
    app.include_router(
        audit_routes.router, dependencies=[Depends(deps.require_role_assigned)]
    )
    app.include_router(users_routes.router)

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


async def _login_via_cas(
    client: AsyncClient,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    username: str,
) -> str:
    monkeypatch.setattr(
        auth_routes.cas_service, "validate_ticket", _mock_validate_ticket(username)
    )
    resp = await client.get("/auth/cas/validate", params={"ticket": "any-ticket"})
    sid = resp.cookies.get(settings.session_cookie_name)
    assert sid is not None, "expected a session cookie from the real CAS-validate route"
    return sid


async def _seed_role(pool: asyncpg.Pool, username: str, role: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET role = $1 WHERE cas_username = $2", role, username
        )


# ── the sharp instance: a bare admin key reads the audit log, unattributably ─


@pytest.mark.asyncio
async def test_bare_admin_key_with_no_session_403s_audit_reveals_legacy(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE instance the decision is really about: before D2, an admin key
    with NO session read ``GET /audit/reveals-legacy`` -- an unattributable
    read of the audit trail itself, the same shape ADR-036 exists to
    prevent -- and got 200. It must now 403."""
    settings = _settings()
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    app = _build_app(pg_pool)

    async with await _client(app) as client:
        resp = await client.get(
            "/audit/reveals-legacy", headers={"X-API-Key": ADMIN_KEY}
        )

    assert resp.status_code == 403


# ── one representative read per gated router now 403s a bare key/no session ─


@pytest.mark.asyncio
async def test_bare_key_with_no_session_403s_get_jobs(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings()
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    app = _build_app(pg_pool)

    async with await _client(app) as client:
        resp = await client.get("/jobs", headers={"X-API-Key": RECRUITER_KEY})

    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_bare_key_with_no_session_403s_list_resumes_for_job(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``GET /jobs/{job_id}/resumes`` is a LIST subresource that 200s + an
    empty list even for a genuinely nonexistent ``job_id`` (pre-existing
    behaviour, unrelated to this decision) -- so this proves the gate fires
    BEFORE that leniency, not that the job happens to exist."""
    settings = _settings()
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    app = _build_app(pg_pool)

    async with await _client(app) as client:
        resp = await client.get(
            f"/jobs/{uuid.uuid4()}/resumes", headers={"X-API-Key": RECRUITER_KEY}
        )

    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_bare_key_with_no_session_403s_get_job_shortlist(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings()
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    app = _build_app(pg_pool)

    async with await _client(app) as client:
        resp = await client.get(
            f"/jobs/{uuid.uuid4()}/shortlist", headers={"X-API-Key": RECRUITER_KEY}
        )

    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_bare_key_with_no_session_403s_post_job_assignees(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Included for completeness (every gated router, per the task) -- but
    NOT a new-behaviour pin: ``POST /jobs/{id}/assignees`` already 403s a
    bare key with no session today, via its OWN per-route
    ``require_session_role(*_ASSIGNERS)`` decorator dependency (ADR-034 F1a,
    writes closed already). This test passes unchanged before and after
    D2 -- it documents that ``job_assignees`` is still correctly behind
    ``require_role_assigned`` too (defence in depth), not that D2 changed
    anything observable here."""
    settings = _settings()
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    app = _build_app(pg_pool)

    async with await _client(app) as client:
        resp = await client.post(
            f"/jobs/{uuid.uuid4()}/assignees",
            headers={"X-API-Key": RECRUITER_KEY},
            json={"user_id": str(uuid.uuid4()), "note": None},
        )

    assert resp.status_code == 403


# ── must not break: the CAS-off dev boot ────────────────────────────────


@pytest.mark.asyncio
async def test_cas_disabled_dev_boot_still_200s_get_jobs_with_no_session_cookie(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The behaviour most likely to be broken silently by a careless
    implementation of D2. With CAS disabled, ``resolve_user`` returns the
    synthetic ``dev-anonymous`` admin sentinel BEFORE the
    ``if not ra_session: return None`` branch ever runs -- so ``user`` is
    NEVER ``None`` on this path regardless of whether a session cookie is
    presented. A bare key with no cookie at all, CAS disabled, must keep
    reading exactly as it does today."""
    settings = _settings(cas_enabled=False)
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    app = _build_app(pg_pool)

    async with await _client(app) as client:
        resp = await client.get("/jobs", headers={"X-API-Key": RECRUITER_KEY})

    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_cas_disabled_dev_boot_still_200s_audit_reveals_legacy(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same pin as above, on the sharpest route -- the CAS-off dev sentinel
    must reach even ``/audit/reveals-legacy`` unaffected, since it is a real
    (synthetic) admin identity, not an absent one."""
    settings = _settings(cas_enabled=False)
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    app = _build_app(pg_pool)

    async with await _client(app) as client:
        resp = await client.get(
            "/audit/reveals-legacy", headers={"X-API-Key": ADMIN_KEY}
        )

    assert resp.status_code == 200


# ── must not break: the Flask viewer's key+session pattern ─────────────────


@pytest.mark.asyncio
async def test_keyed_request_with_a_real_session_still_200s_get_jobs(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``core/frontend/api_client.py`` forwards the browser's ``ra_session``
    cookie ALONGSIDE the fixed recruiter API key on every request -- so its
    reads always carry a real session and must be entirely unaffected by
    D2. This is the exact key+cookie combination the Flask viewer sends."""
    settings = _settings()
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    username = _unique_username("viewer")
    app = _build_app(pg_pool)

    async with await _client(app) as client:
        sid = await _login_via_cas(client, settings, monkeypatch, username)
        await _seed_role(pg_pool, username, "recruiter")

        resp = await client.get(
            "/jobs",
            headers={"X-API-Key": RECRUITER_KEY},
            cookies={settings.session_cookie_name: sid},
        )

    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_keyed_admin_request_with_a_real_session_still_200s_audit_reveals_legacy(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same key+session pattern, on the sharpest route: an admin key
    presented ALONGSIDE a real admin session must keep reading the audit
    log -- D2 closes the UNATTRIBUTABLE case, not this attributable one."""
    settings = _settings()
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    username = _unique_username("realadmin")
    app = _build_app(pg_pool)

    async with await _client(app) as client:
        sid = await _login_via_cas(client, settings, monkeypatch, username)
        await _seed_role(pg_pool, username, "admin")

        resp = await client.get(
            "/audit/reveals-legacy",
            headers={"X-API-Key": ADMIN_KEY},
            cookies={settings.session_cookie_name: sid},
        )

    assert resp.status_code == 200


# ── /auth/* and /users are deliberately NOT behind this gate ───────────────


@pytest.mark.asyncio
async def test_auth_cas_user_reachable_with_no_key_and_no_session(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``/auth`` is never wired behind ``require_role_assigned`` -- a caller
    with NEITHER a key NOR a session must still get the (public, never-401)
    status endpoint, unaffected by D2 in either direction."""
    settings = _settings()
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    app = _build_app(pg_pool)

    async with await _client(app) as client:
        resp = await client.get("/auth/cas/user")

    assert resp.status_code == 200
    body = resp.json()
    assert body["authenticated"] is False


@pytest.mark.asyncio
async def test_users_still_403s_with_no_key_and_no_session_via_its_own_gate(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``/users`` is gated by its own, stricter, session-only
    ``_require_admin_session`` -- already 403ing ``user is None`` long
    before D2. This must be unaffected: still 403, for the same reason as
    always, not a new reason introduced by this decision. Pins the black-box
    behaviour; ``test_api_deps_d2_close_unscoped_reads.py``'s wiring guard
    pins the WHY (it is not wired behind ``require_role_assigned`` at all)."""
    settings = _settings()
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    app = _build_app(pg_pool)

    async with await _client(app) as client:
        resp = await client.get("/users", headers={"X-API-Key": ADMIN_KEY})

    assert resp.status_code == 403
