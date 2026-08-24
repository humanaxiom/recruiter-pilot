"""Integration tests — user-admin-roles slice 3, ``require_role_assigned``
against a REAL Postgres (testcontainers) via real ASGI apps mounting the
real business routers exactly the way ``src.api.main`` must wire them:
``app.include_router(<business router>, dependencies=[Depends(
deps.require_role_assigned)])`` on every one of ``jobs``/``resumes``/
``shortlist``/``job_assignees``/``audit`` — deliberately NOT on ``auth``.

**Why this closes the hole slice 2 opened.** Slice 2 (ADR-019 §10a/§2
reversal) stopped defaulting a non-default-admin CAS login to
``role='recruiter'`` — a first login now captures ``role=None``. Nothing
in slice 2 BLOCKS that no-role user, though: the Flask viewer always
presents the ONE shared ``recruiter`` API key for every browser session
(``core/frontend/api_client.py``), so ``require_role`` passes on the KEY
alone regardless of who is actually signed in, and
``scoped_user_id_or_403`` resolves ``None`` (UNSCOPED) for any
non-hiring_manager SESSION role — including ``role=None`` — so a genuinely
no-role human ends up with full recruiter-equivalent, company-wide access.
This is provable ONLY against a real Postgres + a real cookie + a real
shared key, through real FastAPI router-level dependency resolution — a
mocked ``resolve_user`` (as in ``tests/unit/test_api_deps.py``) can pin the
gate's pure LOGIC but cannot prove the router-level WIRING actually
intercepts a live request before the route body runs.

``src.api.deps.require_role_assigned`` does not exist yet — every test
below fails at ``_build_app`` (``AttributeError: module 'src.api.deps' has
no attribute 'require_role_assigned'``), the RED half of the TDD cycle,
proving the hole is still open: a no-role session presenting the shared
recruiter key currently gets 200 from ``GET /jobs``, not 403.

Follows the exact asyncpg/testcontainers/real-CAS-validate-route fixture
wiring already used in ``tests/integration/test_auth_routes_pg.py`` — no new
harness — plus the local-app-per-router-set convention every other
``tests/integration/test_*_pg.py`` file in this repo already uses (e.g.
``test_auditor_read_logging_pg.py`` mounts jobs+resumes+shortlist together)
rather than importing the lifespan-heavy ``src.api.main.app`` directly.
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
from src.errors import AppError
from src.models.ddl import init_schema
from src.models.pool import get_db
from src.settings import Settings

DEFAULT_ADMIN = "asalah"
RECRUITER_KEY = "shared-recruiter-key-value-slice3"


@pytest.fixture(scope="session")
def pg_dsn() -> Iterator[str]:
    with PostgresContainer("postgres:16-alpine") as pg:
        yield re.sub(r"\+\w+", "", pg.get_connection_url(), count=1)


@pytest.fixture
async def pg_pool(pg_dsn: str) -> AsyncIterator[asyncpg.Pool]:
    pool = await asyncpg.create_pool(dsn=pg_dsn, min_size=2, max_size=8)
    await init_schema(pool)
    async with pool.acquire() as conn:
        await conn.execute("TRUNCATE sessions, users, audit_log, jobs, outbox CASCADE")
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
        "default_admin_cas_username": DEFAULT_ADMIN,
        "api_key_recruiter": RECRUITER_KEY,
        "skill_hash_salt": "test-salt",
        "pii_key": "test-key",
    }
    base.update(overrides)
    return Settings(**base)


def _unique_username(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _mock_validate_ticket(username: str) -> AsyncMock:
    """Stands in for the real ``httpx`` round trip to the CAS server — the
    one genuinely-external boundary these tests mock (matching
    ``test_auth_routes_pg.py``'s own discipline)."""
    return AsyncMock(return_value=username)


def _build_app(pool: asyncpg.Pool) -> FastAPI:
    """Mount the auth router UNGATED, and every business router with
    ``require_role_assigned`` wired at the ROUTER level — this is the exact
    shape ``src.api.main`` must adopt (see the task/ADR context): the auth
    router stays reachable for a no-role user to see their own status or log
    out; every other router 403s a real no-role session before any route
    body runs.
    """
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

    async def _get_db_override() -> AsyncIterator[Any]:
        async with pool.acquire() as conn:
            yield conn

    app.dependency_overrides[get_db] = _get_db_override
    # No real arq/Redis needed for anything exercised here — a harmless fake
    # in case a 200-path test below ever reaches a route that enqueues.
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
    """Drive the REAL ``/auth/cas/validate`` route (mocked ticket only) to
    provision/login ``username`` and return the resulting session cookie."""
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


# ── the hole: a no-role SESSION + the shared recruiter KEY currently 200s ──


@pytest.mark.asyncio
async def test_no_role_session_403s_get_jobs(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE core regression this slice closes: before this gate, a real
    no-role human session presenting the shared recruiter key got 200 from
    ``GET /jobs`` — full recruiter-equivalent, company-wide read access."""
    settings = _settings()
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    username = _unique_username("noel")
    app = _build_app(pg_pool)

    async with await _client(app) as client:
        sid = await _login_via_cas(client, settings, monkeypatch, username)

        resp = await client.get(
            "/jobs",
            headers={"X-API-Key": RECRUITER_KEY},
            cookies={settings.session_cookie_name: sid},
        )

    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_no_role_session_403s_post_jobs_create(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate must block WRITES too, not just reads — and a blocked write
    must never actually land the row."""
    settings = _settings()
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    username = _unique_username("odell")
    app = _build_app(pg_pool)

    async with await _client(app) as client:
        sid = await _login_via_cas(client, settings, monkeypatch, username)

        resp = await client.post(
            "/jobs",
            headers={"X-API-Key": RECRUITER_KEY},
            cookies={settings.session_cookie_name: sid},
            json={
                "title": "Should Never Be Created",
                "description_raw": "blocked by the fail-closed gate. " * 3,
            },
        )

    assert resp.status_code == 403
    async with pg_pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT count(*) FROM jobs WHERE title = $1", "Should Never Be Created"
        )
    assert count == 0, "a 403'd request must never actually create the job row"


@pytest.mark.asyncio
async def test_no_role_session_403s_get_job_shortlist(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Proves the gate is wired on the SHORTLIST router too, not just jobs —
    ``require_role_assigned`` must be added once at every business
    ``include_router`` call site, not hand-copied per router."""
    settings = _settings()
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    username = _unique_username("penny")
    app = _build_app(pg_pool)

    async with await _client(app) as client:
        sid = await _login_via_cas(client, settings, monkeypatch, username)

        resp = await client.get(
            f"/jobs/{uuid.uuid4()}/shortlist",
            headers={"X-API-Key": RECRUITER_KEY},
            cookies={settings.session_cookie_name: sid},
        )

    assert resp.status_code == 403


# ── the auth router stays reachable — a no-role user must still see status ─


@pytest.mark.asyncio
async def test_no_role_session_still_reaches_auth_cas_user_200(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The auth router is deliberately NOT gated: a no-role human must still
    be able to see their own status (and log out) — otherwise they could
    never discover why every business route now 403s them."""
    settings = _settings()
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    username = _unique_username("quinn")
    app = _build_app(pg_pool)

    async with await _client(app) as client:
        sid = await _login_via_cas(client, settings, monkeypatch, username)

        resp = await client.get(
            "/auth/cas/user",
            headers={"X-API-Key": RECRUITER_KEY},
            cookies={settings.session_cookie_name: sid},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["authenticated"] is True
    assert body["role"] is None


# ── REVERSED 2026-08-19 (D2 = option B): a bare recruiter key, no session at
#    all, used to be unaffected here -- it now 403s too, see below ──


@pytest.mark.asyncio
async def test_bare_recruiter_key_with_no_session_cookie_now_403s_get_jobs(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REVERSED 2026-08-19 -- D2 = option B, ``docs/OPEN_DECISIONS.md`` §D2,
    answered by product; the question ADR-034's "Accepted residuals" carried
    forward as undecided. Until today this test asserted the OPPOSITE (see
    its old name, ``..._still_200s_get_jobs``): a caller presenting only the
    shared recruiter API key and NO session cookie at all was governed by
    ``require_role`` alone, and ``require_role_assigned`` PASSED a
    ``user is None`` resolution. Product decided reads must be symmetric
    with writes (ADR-034 F1a already 403s ``user is None`` for
    ``require_session_role``) -- a bare key with no session now needs a
    real principal for reads too, same as writes. Full router-by-router
    coverage of this reversal lives in
    ``tests/integration/test_close_unscoped_reads_pg.py``; this test stays
    to keep the original regression location honest about what changed."""
    settings = _settings()
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    app = _build_app(pg_pool)

    async with await _client(app) as client:
        resp = await client.get("/jobs", headers={"X-API-Key": RECRUITER_KEY})

    assert resp.status_code == 403


# ── the gate only blocks role=None — any real assigned role still 200s ──


@pytest.mark.parametrize("role", ["admin", "recruiter", "hiring_manager", "auditor"])
@pytest.mark.asyncio
async def test_session_with_an_assigned_role_still_200s_get_jobs(
    role: str, pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate only blocks a genuinely no-role session — every real
    assigned role must keep working exactly as before. A non-admin role can
    only arrive via an explicit UPDATE post slice-2 (``provision_or_get`` no
    longer assigns any non-admin default), exactly as
    ``test_auth_routes_pg.py``'s own hiring_manager coverage already does."""
    settings = _settings()
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    username = _unique_username(f"role-{role}")
    app = _build_app(pg_pool)

    async with await _client(app) as client:
        sid = await _login_via_cas(client, settings, monkeypatch, username)
        await _seed_role(pg_pool, username, role)

        resp = await client.get(
            "/jobs",
            headers={"X-API-Key": RECRUITER_KEY},
            cookies={settings.session_cookie_name: sid},
        )

    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_default_admin_first_login_is_never_blocked(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Belt-and-suspenders: the configured default-admin CAS username
    (ADR-019 §10a) is provisioned ``role='admin'`` on first login (slice 2
    leaves this path unchanged) and must sail through the new gate exactly
    as before -- this slice adds a BLOCK for no-role sessions, it must never
    add a NEW block for the one identity that has always had a role."""
    settings = _settings()
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    app = _build_app(pg_pool)

    async with await _client(app) as client:
        sid = await _login_via_cas(client, settings, monkeypatch, DEFAULT_ADMIN)

        resp = await client.get(
            "/jobs",
            headers={"X-API-Key": RECRUITER_KEY},
            cookies={settings.session_cookie_name: sid},
        )

    assert resp.status_code == 200
