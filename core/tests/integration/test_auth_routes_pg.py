"""Integration tests — FU-5 slice 6's CAS routes against a REAL Postgres
(testcontainers) via a real ASGI app (``app.dependency_overrides[get_db]``
routed to a real pooled ``asyncpg`` connection), the real
``session_service``/``user_service`` from slice 5, and the real ``users`` /
``sessions`` DDL from slices 1/3. The one boundary mocked is CAS's own
network call — ``auth_routes.cas_service.validate_ticket`` is monkeypatched
to an ``AsyncMock`` returning a canned username, standing in for the real
round trip to ``cas.sfu.ca``.

Nothing here exists yet (``core/src/api/routes/auth.py``,
``core/src/api/deps.py::resolve_user``), so every test below fails at
collection or the first request. RED half of the TDD cycle.

What a REAL Postgres proves that ``test_route_auth.py``'s mocked-conn route
tests structurally cannot:

* ``GET /auth/cas/validate``'s happy path REALLY inserts a ``users`` row
  (role picked per ADR-019 §10a's default-admin allowlist) and a REAL
  ``sessions`` row satisfying the FK — not just that the route called some
  function with plausible arguments,
* the ``Set-Cookie`` response header really carries an opaque session id that
  really resolves, end-to-end, back through ``resolve_user`` to that same
  real ``users`` row,
* ``GET /auth/cas/logout`` really flips ``sessions.revoked_at`` so a REAL
  subsequent ``get_active_session`` lookup returns ``None`` — a mocked
  connection cannot observe a revoke actually taking effect against the
  ``WHERE revoked_at IS NULL AND expires_at > now()`` predicate.
* (FU-5 slice 12, ADR-019 §10 step 5) the sliding-window refresh REALLY
  extends a near-expiry ``sessions.expires_at`` row when the request path
  resolves it — through both ``deps.resolve_user`` and the ``GET
  /auth/cas/user`` status route — and REALLY leaves a fresh session's row
  untouched. This is the reviewer-flagged gap: ``session_service.
  refresh_if_needed`` (slice 5) was unit-tested in isolation but nothing on
  the request path called it, so ``settings.session_idle_refresh_hours`` was
  unread and sessions hard-expired instead of sliding. A mocked connection
  cannot prove a real ``UPDATE ... SET expires_at`` actually lands (or does
  not land) against a real row — only a real Postgres can.
* (FU-6 slice 10, ADR-020 §7) the ``role`` reported by ``GET /auth/cas/user``
  REALLY comes from the real ``users.role`` column via the real session
  resolution chain — not just that the route echoed a field on a mocked
  row. A mocked connection would let ``role`` be a hardcoded literal that
  never actually reads the DB; only a real Postgres proves the endpoint's
  ``role`` and the row's ``users.role`` cannot drift apart.

**user-admin-roles slice 2 — human-directed reversal of ADR-019 §10a/§2
(2026-07-25).** A first CAS login for a username other than the configured
default-admin now captures NO role through the real HTTP route, not the
old ``'recruiter'`` default. The default-admin (``asalah``) case is
UNCHANGED — still provisioned ``'admin'`` on first login. Tests below that
previously asserted ``role == "recruiter"`` through the real CAS-validate
route are rewritten to assert ``role is None`` — this is the authorized
reversal target the coordinator cited, not a "modify tests to pass"
violation. Slice 3 adds the fail-closed gate; a no-role user reaching
``/auth/cas/user`` without yet being blocked is this slice's intentional,
known intermediate state.

Follows the exact asyncpg/testcontainers fixture wiring already used in
``tests/integration/test_api_jobs_pg.py`` and
``tests/integration/test_session_user_service_pg.py`` — no new harness.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
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
from src.errors import AppError
from src.models.ddl import init_schema
from src.models.pool import get_db
from src.schemas.auth import User
from src.services import session_service, user_service
from src.settings import Settings

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
        "skill_hash_salt": "test-salt",
        "pii_key": "test-key",
    }
    base.update(overrides)
    return Settings(**base)


def _unique_username(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _mock_validate_ticket(username: str) -> AsyncMock:
    """Stands in for the real ``httpx`` round trip to the CAS server — the
    one genuinely-external boundary these tests mock."""
    return AsyncMock(return_value=username)


def _build_app(pool: asyncpg.Pool) -> FastAPI:
    app = FastAPI()
    app.include_router(auth_routes.router)

    async def _get_db_override() -> AsyncIterator[Any]:
        async with pool.acquire() as conn:
            yield conn

    app.dependency_overrides[get_db] = _get_db_override

    @app.exception_handler(AppError)
    async def _app_error_handler(_request: Any, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status, content={"detail": exc.message, "code": exc.code}
        )

    return app


async def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# ── GET /auth/cas/validate — happy path against real Postgres ─────────────


@pytest.mark.asyncio
async def test_cas_validate_happy_path_creates_a_real_admin_user_and_session(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings()
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    monkeypatch.setattr(
        auth_routes.cas_service,
        "validate_ticket",
        _mock_validate_ticket(DEFAULT_ADMIN),
    )
    app = _build_app(pg_pool)

    async with await _client(app) as client:
        resp = await client.get(
            "/auth/cas/validate", params={"ticket": "any-ticket", "next": "/jobs"}
        )

    assert resp.status_code == 302
    assert resp.headers["location"] == "/jobs"
    set_cookie = resp.headers.get("set-cookie", "")
    assert settings.session_cookie_name in set_cookie
    assert "httponly" in set_cookie.lower()

    sid = resp.cookies.get(settings.session_cookie_name)
    assert sid is not None

    async with pg_pool.acquire() as conn:
        user_row = await conn.fetchrow(
            "SELECT id, role FROM users WHERE cas_username = $1", DEFAULT_ADMIN
        )
    assert user_row is not None
    assert user_row["role"] == "admin", "asalah is the ADR-019 §10a default admin"

    async with pg_pool.acquire() as conn:
        session_row = await conn.fetchrow(
            "SELECT user_id, revoked_at FROM sessions WHERE id = $1", sid
        )
    assert session_row is not None
    assert session_row["user_id"] == user_row["id"]
    assert session_row["revoked_at"] is None


@pytest.mark.asyncio
async def test_cas_validate_for_a_non_default_username_creates_a_no_role_row(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """user-admin-roles slice 2 (ADR-019 §10a/§2 reversal, human directive
    2026-07-25) through the real route — a username other than the
    configured default admin is now provisioned with NO role at all,
    previously ``'recruiter'``, superseded here."""
    settings = _settings()
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    username = _unique_username("erin")
    monkeypatch.setattr(
        auth_routes.cas_service, "validate_ticket", _mock_validate_ticket(username)
    )
    app = _build_app(pg_pool)

    async with await _client(app) as client:
        resp = await client.get("/auth/cas/validate", params={"ticket": "any-ticket"})

    assert resp.status_code == 302
    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT role FROM users WHERE cas_username = $1", username
        )
    assert row is not None
    assert row["role"] is None


# ── cookie -> session -> user, end-to-end through resolve_user ────────────


@pytest.mark.asyncio
async def test_resolve_user_resolves_the_real_user_from_the_validate_response_cookie(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """user-admin-roles slice 2 — the resolved ``User.role`` for a
    non-default-admin first login is now ``None`` end-to-end through
    ``resolve_user``, not ``'recruiter'``."""
    settings = _settings()
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    username = _unique_username("carol")
    monkeypatch.setattr(
        auth_routes.cas_service, "validate_ticket", _mock_validate_ticket(username)
    )
    app = _build_app(pg_pool)

    async with await _client(app) as client:
        resp = await client.get("/auth/cas/validate", params={"ticket": "any-ticket"})
    sid = resp.cookies.get(settings.session_cookie_name)
    assert sid is not None

    async with pg_pool.acquire() as conn:
        request = MagicMock()
        user = await deps.resolve_user(request=request, db=conn, ra_session=sid)

    assert user is not None
    assert user.cas_username == username
    assert user.role is None


@pytest.mark.asyncio
async def test_cas_user_endpoint_reflects_the_real_session_after_validate(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same chain, proved through the public ``/auth/cas/user`` status route
    instead of calling the dependency directly — belt and suspenders."""
    settings = _settings()
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    username = _unique_username("dana")
    monkeypatch.setattr(
        auth_routes.cas_service, "validate_ticket", _mock_validate_ticket(username)
    )
    app = _build_app(pg_pool)

    async with await _client(app) as client:
        await client.get("/auth/cas/validate", params={"ticket": "any-ticket"})
        status_resp = await client.get("/auth/cas/user")

    assert status_resp.status_code == 200
    body = status_resp.json()
    assert body["authenticated"] is True
    assert body["username"] == username


# ── GET /auth/cas/user — real `role` (FU-6 slice 10, ADR-020 §7) ──────────
#
# The Flask viewer's `/my/jobs` default-view switch needs the logged-in
# user's ROLE, sourced from the real `users.role` row via the real
# cookie -> session -> user chain — not a hardcoded literal on the route.


@pytest.mark.asyncio
async def test_cas_user_endpoint_reports_no_role_for_a_non_default_login(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """user-admin-roles slice 2 (ADR-019 §10a/§2 reversal, human directive
    2026-07-25) — a non-default-admin first login now reports ``role: null``
    through ``GET /auth/cas/user``, previously ``'recruiter'``, superseded
    here."""
    settings = _settings()
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    username = _unique_username("nina")
    monkeypatch.setattr(
        auth_routes.cas_service, "validate_ticket", _mock_validate_ticket(username)
    )
    app = _build_app(pg_pool)

    async with await _client(app) as client:
        await client.get("/auth/cas/validate", params={"ticket": "any-ticket"})
        status_resp = await client.get("/auth/cas/user")

    assert status_resp.status_code == 200
    body = status_resp.json()
    assert body["authenticated"] is True
    assert body["role"] is None

    async with pg_pool.acquire() as conn:
        db_role = await conn.fetchval(
            "SELECT role FROM users WHERE cas_username = $1", username
        )
    assert body["role"] == db_role, "endpoint role must match the real users.role row"
    assert db_role is None


@pytest.mark.asyncio
async def test_cas_user_endpoint_reports_a_seeded_hiring_manager_role(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-020 §7 — the Flask viewer's `/my/jobs` default-view switch needs
    a real hiring_manager session's role surfaced end-to-end. Seed the role
    directly (``user_service.provision_or_get`` no longer assigns any
    non-admin default role — see the slice-2 reversal — so a non-admin role
    can only arrive via an explicit UPDATE, as here), then re-read it
    through the real session chain."""
    settings = _settings()
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    username = _unique_username("oscar")
    monkeypatch.setattr(
        auth_routes.cas_service, "validate_ticket", _mock_validate_ticket(username)
    )
    app = _build_app(pg_pool)

    async with await _client(app) as client:
        await client.get("/auth/cas/validate", params={"ticket": "any-ticket"})

        async with pg_pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET role = 'hiring_manager' WHERE cas_username = $1",
                username,
            )

        status_resp = await client.get("/auth/cas/user")

    assert status_resp.status_code == 200
    body = status_resp.json()
    assert body["authenticated"] is True
    assert body["role"] == "hiring_manager"


@pytest.mark.asyncio
async def test_cas_user_endpoint_role_is_null_with_no_cookie(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings()
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    app = _build_app(pg_pool)

    async with await _client(app) as client:
        resp = await client.get("/auth/cas/user")

    assert resp.status_code == 200
    body = resp.json()
    assert body["authenticated"] is False
    assert body["role"] is None


# ── GET /auth/cas/logout — revokes the real session row ───────────────────


@pytest.mark.asyncio
async def test_logout_revokes_the_session_so_it_no_longer_resolves(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings()
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    username = _unique_username("frank")
    monkeypatch.setattr(
        auth_routes.cas_service, "validate_ticket", _mock_validate_ticket(username)
    )
    app = _build_app(pg_pool)

    async with await _client(app) as client:
        validate_resp = await client.get(
            "/auth/cas/validate", params={"ticket": "any-ticket"}
        )
        sid = validate_resp.cookies.get(settings.session_cookie_name)
        assert sid is not None

        logout_resp = await client.get(
            "/auth/cas/logout", cookies={settings.session_cookie_name: sid}
        )

    assert logout_resp.status_code in (200, 302, 303, 307)

    async with pg_pool.acquire() as conn:
        got = await session_service.get_active_session(conn, sid)
    assert got is None, "a logged-out session must never resolve as active again"

    async with pg_pool.acquire() as conn:
        revoked_at = await conn.fetchval(
            "SELECT revoked_at FROM sessions WHERE id = $1", sid
        )
    assert revoked_at is not None


# ── sliding-window refresh wiring (FU-5 slice 12, ADR-019 §10 step 5) ─────
#
# The reviewer finding this closes: ``refresh_if_needed`` (slice 5) was
# unit-tested in isolation but nothing on the request path called it —
# ``resolve_user`` and ``GET /auth/cas/user`` both called ONLY
# ``get_active_session``, so sessions hard-expired at the fixed TTL instead
# of sliding. ``session_idle_refresh_hours`` was unread config. These tests
# build a real session, force its ``expires_at`` to a known point (near or
# far from expiry) with a direct SQL ``UPDATE`` — exactly as a real clock
# tick would eventually do — then drive the real request path and re-read
# the row.

REFRESH_TTL_HOURS = 8
REFRESH_IDLE_HOURS = 1


def _refresh_settings(**overrides: Any) -> Settings:
    return _settings(
        session_ttl_hours=REFRESH_TTL_HOURS,
        session_idle_refresh_hours=REFRESH_IDLE_HOURS,
        **overrides,
    )


async def _provision_and_login(
    pg_pool: asyncpg.Pool, username: str
) -> tuple[User, str]:
    """Provision a user + real session row directly against the pool (no
    HTTP round trip through /auth/cas/validate), returning (user, sid).
    The session is created with the full configured TTL — callers push it
    near expiry themselves via ``_force_expires_at`` where the test needs
    that."""
    async with pg_pool.acquire() as conn:
        user = await user_service.provision_or_get(
            conn, cas_username=username, default_admin_cas_username=DEFAULT_ADMIN
        )
        session = await session_service.create_session(
            conn, user_id=user.id, ttl_seconds=REFRESH_TTL_HOURS * 3600
        )
    return user, session.id


async def _force_expires_at(
    pg_pool: asyncpg.Pool, session_id: str, delta: timedelta
) -> None:
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "UPDATE sessions SET expires_at = $1 WHERE id = $2",
            datetime.now(UTC) + delta,
            session_id,
        )


async def _read_expires_at(pg_pool: asyncpg.Pool, session_id: str) -> datetime:
    async with pg_pool.acquire() as conn:
        value: datetime = await conn.fetchval(
            "SELECT expires_at FROM sessions WHERE id = $1", session_id
        )
    return value


@pytest.mark.asyncio
async def test_resolve_user_extends_a_near_expiry_session_expires_at(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _refresh_settings()
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    _user, sid = await _provision_and_login(pg_pool, _unique_username("ivy"))
    # Push it inside the 1-hour idle-refresh window.
    await _force_expires_at(pg_pool, sid, timedelta(minutes=2))
    before = await _read_expires_at(pg_pool, sid)

    async with pg_pool.acquire() as conn:
        resolved = await deps.resolve_user(request=MagicMock(), db=conn, ra_session=sid)

    assert resolved is not None, "a near-expiry but still-live session must resolve"

    after = await _read_expires_at(pg_pool, sid)
    assert after > before, "a near-expiry session must be pushed further out"
    assert after >= datetime.now(UTC) + timedelta(hours=REFRESH_TTL_HOURS - 1), (
        "the extension must be roughly a full ttl_seconds from now, not a " "token bump"
    )


@pytest.mark.asyncio
async def test_resolve_user_leaves_a_fresh_session_expires_at_unchanged(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _refresh_settings()
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    # Session created with the full 8h ttl — far outside the 1h idle window.
    _user, sid = await _provision_and_login(pg_pool, _unique_username("jack"))
    before = await _read_expires_at(pg_pool, sid)

    async with pg_pool.acquire() as conn:
        resolved = await deps.resolve_user(request=MagicMock(), db=conn, ra_session=sid)

    assert resolved is not None

    after = await _read_expires_at(pg_pool, sid)
    assert after == before, "a session far from expiry must not be rewritten"


@pytest.mark.asyncio
async def test_cas_user_route_extends_a_near_expiry_session_expires_at(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same behaviour, proved through the public ``/auth/cas/user`` status
    route — which resolves cookie -> session -> user WITHOUT going through
    ``deps.resolve_user`` at all, so the wiring must be added there too."""
    settings = _refresh_settings()
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    _user, sid = await _provision_and_login(pg_pool, _unique_username("kim"))
    await _force_expires_at(pg_pool, sid, timedelta(minutes=2))
    before = await _read_expires_at(pg_pool, sid)
    app = _build_app(pg_pool)

    async with await _client(app) as client:
        resp = await client.get(
            "/auth/cas/user", cookies={settings.session_cookie_name: sid}
        )

    assert resp.status_code == 200
    assert resp.json()["authenticated"] is True

    after = await _read_expires_at(pg_pool, sid)
    assert (
        after > before
    ), "a near-expiry session must be extended by /auth/cas/user too"


@pytest.mark.asyncio
async def test_cas_user_route_leaves_a_fresh_session_expires_at_unchanged(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _refresh_settings()
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    _user, sid = await _provision_and_login(pg_pool, _unique_username("liam"))
    before = await _read_expires_at(pg_pool, sid)
    app = _build_app(pg_pool)

    async with await _client(app) as client:
        resp = await client.get(
            "/auth/cas/user", cookies={settings.session_cookie_name: sid}
        )

    assert resp.status_code == 200

    after = await _read_expires_at(pg_pool, sid)
    assert after == before


@pytest.mark.asyncio
async def test_resolve_user_with_a_revoked_near_expiry_session_still_returns_none(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A revoked session must resolve to ``None`` — never refreshed, even if
    it also happens to be near expiry."""
    settings = _refresh_settings()
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    _user, sid = await _provision_and_login(pg_pool, _unique_username("mia"))
    await _force_expires_at(pg_pool, sid, timedelta(minutes=2))

    async with pg_pool.acquire() as conn:
        await session_service.revoke_session(conn, sid)
    before = await _read_expires_at(pg_pool, sid)

    async with pg_pool.acquire() as conn:
        resolved = await deps.resolve_user(request=MagicMock(), db=conn, ra_session=sid)

    assert resolved is None, "a revoked session must never resolve to a user"

    after = await _read_expires_at(pg_pool, sid)
    assert after == before, "a revoked session's expires_at must never be touched"
