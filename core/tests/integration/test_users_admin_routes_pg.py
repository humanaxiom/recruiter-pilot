"""Integration tests — user-admin-roles slice 5's ``GET /users`` against a
REAL Postgres (testcontainers), through a real ASGI app and real CAS-session
cookies obtained via the real ``/auth/cas/validate`` route (the same
technique ``test_auth_routes_pg.py``/``test_no_role_fail_closed_pg.py``
already use — the one boundary mocked is CAS's own network call,
``auth_routes.cas_service.validate_ticket``).

``core/src/api/routes/users.py`` and ``src.services.user_service.list_users``
do not exist yet — every test below fails at collection
(``ModuleNotFoundError``) or the first request. RED half of the TDD cycle.

**What a real Postgres + real dependency resolution proves that
``test_route_users.py``'s mocked-conn route tests structurally cannot**
(CLAUDE.md's ``offline`` caveat — admin authz + a DB list + the key-vs-session
distinction through real dependency resolution):

* the admin listing REALLY reads every row of a real ``users`` table (not
  just that the route called some function with plausible arguments), and a
  REAL no-role row (``role IS NULL``, exactly what a non-default-admin first
  login captures per the slice-2 reversal) really surfaces with
  ``"role": null``, not silently dropped or coerced to a string,
* the admin gate is REALLY keyed off the CAS session's ``users.role`` column,
  read through the real cookie -> sessions -> users chain — not a hardcoded
  literal that never actually touches the database,
* **the key-vs-session gap (planner decision #3, the point of this slice):**
  a caller presenting ONLY the shared ``recruiter`` API key, with NO session
  cookie at all, really gets 403 from a REAL ``require_role``-style key
  check being ABSENT from this route's gate — proving the route consults
  ``resolve_user``'s real session resolution and never falls back to
  ``resolve_role``/the key. A mocked ``resolve_user`` override (as in
  ``test_route_users.py``) can pin the gate's pure LOGIC for a given
  resolution, but cannot prove that a REAL unauthenticated-by-cookie request
  actually resolves ``user is None`` through the real dependency chain
  instead of leaking through some other path.

Follows the exact asyncpg/testcontainers/real-CAS-validate-route fixture
wiring already used in ``tests/integration/test_auth_routes_pg.py`` and
``tests/integration/test_no_role_fail_closed_pg.py`` — no new harness — plus
``tests/integration/test_users_audit_pg.py``'s direct-SQL user-seeding
convention for building the listing's fixture data.

**user-admin-roles slice 6 addendum (the merge-blocking payoff slice) —
``PATCH /users/{user_id}/role``.** Everything below the ``GET /users``
sections above is new: the role-assignment route, its ``role_changed`` audit
row, and the last-admin-lockout guard. None of ``RoleAssignment``,
``user_service.set_role``/``count_active_admins``, ``errors.ConflictError``,
or the route itself exist yet — every test below fails too, most
immediately at ``AttributeError``/404 (route not registered) or import.
What only a REAL Postgres can prove here (the reason this slice is
integration-MANDATORY per CLAUDE.md's ``offline`` caveat, restated in the
task): the role ``UPDATE`` actually lands in a real row, EXACTLY one real
``audit_log`` row is written per real role change (satisfying the real
``audit_log_actor_identity`` CHECK constraint), the admin-count lockout
arithmetic runs against a REAL ``COUNT(*) WHERE role='admin' AND active``
query (not a mocked stand-in that could trivially be wired to always return
1 or always return 2), and the write + audit are REALLY atomic across a
crash (mirroring ``test_blind_review_audit_pg.py``'s own crash-injection
technique).
"""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock

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
from src.settings import Settings

DEFAULT_ADMIN = "asalah"
RECRUITER_KEY = "shared-recruiter-key-value-users-slice5"

INSERT_USER = """
INSERT INTO users (cas_username, display_name, email, role)
VALUES ($1, $2, $3, $4)
RETURNING id
"""


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
    """Mounts the auth router (ungated — needed to obtain real session
    cookies via ``/auth/cas/validate``) alongside the new ``users`` router,
    exactly the shape the task's design note calls for: ``/users`` is gated
    by its own ``_require_admin_session`` (the CAS session role), never by
    ``require_role``/an API key."""
    from src.api.routes import users as users_routes

    app = FastAPI()
    app.include_router(auth_routes.router)
    app.include_router(users_routes.router)

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


async def _insert_user(
    pool: asyncpg.Pool,
    *,
    cas_username: str,
    role: str | None,
    display_name: str | None = "Seeded Person",
    email: str | None = "seeded@example.org",
) -> uuid.UUID:
    async with pool.acquire() as conn:
        user_id: uuid.UUID = await conn.fetchval(
            INSERT_USER, cas_username, display_name, email, role
        )
    return user_id


# ── an ADMIN session lists every user, incl. a real no-role row ──────────


@pytest.mark.asyncio
async def test_admin_session_lists_every_seeded_user_pg(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings()
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    monkeypatch.setattr(deps, "get_settings", lambda: settings)

    recruiter_name = _unique_username("rae-recruiter")
    no_role_name = _unique_username("nia-no-role")
    hm_name = _unique_username("hank-hm")
    await _insert_user(pg_pool, cas_username=recruiter_name, role="recruiter")
    await _insert_user(pg_pool, cas_username=no_role_name, role=None)
    await _insert_user(pg_pool, cas_username=hm_name, role="hiring_manager")

    app = _build_app(pg_pool)
    async with await _client(app) as client:
        sid = await _login_via_cas(client, settings, monkeypatch, DEFAULT_ADMIN)
        resp = await client.get("/users", cookies={settings.session_cookie_name: sid})

    assert resp.status_code == 200
    body = resp.json()
    by_username = {u["cas_username"]: u for u in body}

    # The default-admin's own login row, plus the three seeded rows.
    assert DEFAULT_ADMIN in by_username
    assert by_username[DEFAULT_ADMIN]["role"] == "admin"
    assert by_username[recruiter_name]["role"] == "recruiter"
    assert by_username[hm_name]["role"] == "hiring_manager"
    assert by_username[no_role_name]["role"] is None


@pytest.mark.asyncio
async def test_admin_session_listing_count_matches_the_real_users_table_pg(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings()
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    monkeypatch.setattr(deps, "get_settings", lambda: settings)

    await _insert_user(pg_pool, cas_username=_unique_username("one"), role="auditor")
    await _insert_user(pg_pool, cas_username=_unique_username("two"), role=None)

    app = _build_app(pg_pool)
    async with await _client(app) as client:
        sid = await _login_via_cas(client, settings, monkeypatch, DEFAULT_ADMIN)
        resp = await client.get("/users", cookies={settings.session_cookie_name: sid})

    assert resp.status_code == 200
    body = resp.json()

    async with pg_pool.acquire() as conn:
        real_count = await conn.fetchval("SELECT count(*) FROM users")
    assert len(body) == real_count, (
        "the endpoint's listing must match the real users table exactly, "
        "not a partial/paginated/stale view"
    )


# ── exact response shape — an admin surface, pin it precisely ────────────


@pytest.mark.asyncio
async def test_admin_session_response_exposes_exactly_the_expected_fields_pg(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings()
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    monkeypatch.setattr(deps, "get_settings", lambda: settings)

    username = _unique_username("gabby")
    await _insert_user(
        pg_pool,
        cas_username=username,
        role="recruiter",
        display_name="Gabby Recruiter",
        email="gabby@example.org",
    )

    app = _build_app(pg_pool)
    async with await _client(app) as client:
        sid = await _login_via_cas(client, settings, monkeypatch, DEFAULT_ADMIN)
        resp = await client.get("/users", cookies={settings.session_cookie_name: sid})

    assert resp.status_code == 200
    body = resp.json()
    item = next(u for u in body if u["cas_username"] == username)
    assert set(item.keys()) == {
        "id",
        "cas_username",
        "display_name",
        "email",
        "role",
        "active",
        "created_at",
        "last_seen_at",
    }
    assert item["cas_username"] == username
    assert item["email"] == "gabby@example.org"
    assert item["role"] == "recruiter"
    assert item["active"] is True


# ── a RECRUITER session (a real assigned non-admin role) 403s ─────────────


@pytest.mark.asyncio
async def test_recruiter_session_403s_get_users_pg(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings()
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    username = _unique_username("rex-recruiter")

    app = _build_app(pg_pool)
    async with await _client(app) as client:
        sid = await _login_via_cas(client, settings, monkeypatch, username)
        await _seed_role(pg_pool, username, "recruiter")

        resp = await client.get("/users", cookies={settings.session_cookie_name: sid})

    assert resp.status_code == 403


@pytest.mark.parametrize("role", ["hiring_manager", "auditor"])
@pytest.mark.asyncio
async def test_other_non_admin_roles_403_get_users_pg(
    role: str, pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings()
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    username = _unique_username(f"role-{role}")

    app = _build_app(pg_pool)
    async with await _client(app) as client:
        sid = await _login_via_cas(client, settings, monkeypatch, username)
        await _seed_role(pg_pool, username, role)

        resp = await client.get("/users", cookies={settings.session_cookie_name: sid})

    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_a_real_no_role_session_403s_get_users_pg(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact hole this slice closes for the admin surface: a genuinely
    no-role human session (slice 2's first-login capture, still unpromoted)
    must never see the user listing either."""
    settings = _settings()
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    username = _unique_username("nolan-no-role")

    app = _build_app(pg_pool)
    async with await _client(app) as client:
        sid = await _login_via_cas(client, settings, monkeypatch, username)

        resp = await client.get("/users", cookies={settings.session_cookie_name: sid})

    assert resp.status_code == 403


# ── the key-vs-session gap (planner decision #3) ──────────────────────────


@pytest.mark.asyncio
async def test_bare_recruiter_key_with_no_session_cookie_403s_get_users_pg(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE regression this slice's gate design closes: a caller presenting
    ONLY the shared ``recruiter`` API key (exactly what the Flask viewer
    sends for every browser user) and NO session cookie at all must be
    403'd — a key-role gate would have let this through as
    ``Role.RECRUITER`` (or ``Role.ADMIN`` with auth disabled entirely), which
    is precisely the "looks like a real human but isn't" gap
    ``job_assignees._require_real_assigner`` left open by checking only "is
    a real human session", never that session's role. ``_require_admin_
    session`` must 403 on ``user is None`` regardless of any header
    presented."""
    settings = _settings()
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    app = _build_app(pg_pool)

    async with await _client(app) as client:
        resp = await client.get("/users", headers={"X-API-Key": RECRUITER_KEY})

    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_no_cookie_and_no_key_at_all_403s_get_users_pg(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings()
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    app = _build_app(pg_pool)

    async with await _client(app) as client:
        resp = await client.get("/users")

    assert resp.status_code == 403


# ── the default-admin's own first login always sees the listing ──────────


@pytest.mark.asyncio
async def test_default_admin_first_login_sees_the_listing_pg(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings()
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    app = _build_app(pg_pool)

    async with await _client(app) as client:
        sid = await _login_via_cas(client, settings, monkeypatch, DEFAULT_ADMIN)
        resp = await client.get("/users", cookies={settings.session_cookie_name: sid})

    assert resp.status_code == 200
    body = resp.json()
    assert any(u["cas_username"] == DEFAULT_ADMIN for u in body)


# ═══════════════════════════════════════════════════════════════════════════
# user-admin-roles slice 6 — PATCH /users/{user_id}/role (the merge-blocking
# payoff slice): role assignment, its role_changed audit row, and the
# last-admin-lockout guard. See the module docstring addendum above.
# ═══════════════════════════════════════════════════════════════════════════


async def _get_role(pool: asyncpg.Pool, user_id: uuid.UUID) -> str | None:
    async with pool.acquire() as conn:
        value: str | None = await conn.fetchval(
            "SELECT role FROM users WHERE id = $1", user_id
        )
    return value


async def _role_changed_audit_rows(
    pool: asyncpg.Pool, user_id: uuid.UUID
) -> list[asyncpg.Record]:
    async with pool.acquire() as conn:
        return await conn.fetch(
            "SELECT actor_kind, actor_user_id, actor_service, action, "
            "subject_type, subject_id, details FROM audit_log "
            "WHERE subject_id = $1 AND action = 'role_changed'",
            user_id,
        )


def _details(row: asyncpg.Record) -> Any:
    import json

    raw = row["details"]
    return json.loads(raw) if isinstance(raw, str) else raw


async def _user_id_by_username(pool: asyncpg.Pool, username: str) -> uuid.UUID:
    async with pool.acquire() as conn:
        value: uuid.UUID = await conn.fetchval(
            "SELECT id FROM users WHERE cas_username = $1", username
        )
    return value


# ── happy path — a real admin session assigns a real role ────────────────


@pytest.mark.asyncio
async def test_admin_sets_a_no_role_users_role_to_recruiter_pg(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings()
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    monkeypatch.setattr(deps, "get_settings", lambda: settings)

    target_name = _unique_username("nia-no-role")
    target_id = await _insert_user(pg_pool, cas_username=target_name, role=None)

    app = _build_app(pg_pool)
    async with await _client(app) as client:
        sid = await _login_via_cas(client, settings, monkeypatch, DEFAULT_ADMIN)
        resp = await client.patch(
            f"/users/{target_id}/role",
            json={"role": "recruiter"},
            cookies={settings.session_cookie_name: sid},
        )

    assert resp.status_code == 200
    assert await _get_role(pg_pool, target_id) == "recruiter"

    rows = await _role_changed_audit_rows(pg_pool, target_id)
    assert len(rows) == 1, "exactly one audit_log row for this role change"
    row = rows[0]
    admin_id = await _user_id_by_username(pg_pool, DEFAULT_ADMIN)
    assert row["actor_kind"] == "user"
    assert row["actor_user_id"] == admin_id
    assert row["actor_service"] is None
    assert row["subject_type"] == "user"
    assert row["subject_id"] == target_id
    assert _details(row) == {"old_role": None, "new_role": "recruiter"}


@pytest.mark.asyncio
async def test_a_recruiter_session_403s_patch_role_pg(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings()
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    monkeypatch.setattr(deps, "get_settings", lambda: settings)

    recruiter_name = _unique_username("rex-recruiter")
    target_name = _unique_username("target-user")
    target_id = await _insert_user(pg_pool, cas_username=target_name, role=None)

    app = _build_app(pg_pool)
    async with await _client(app) as client:
        sid = await _login_via_cas(client, settings, monkeypatch, recruiter_name)
        await _seed_role(pg_pool, recruiter_name, "recruiter")

        resp = await client.patch(
            f"/users/{target_id}/role",
            json={"role": "hiring_manager"},
            cookies={settings.session_cookie_name: sid},
        )

    assert resp.status_code == 403
    assert await _get_role(pg_pool, target_id) is None
    assert await _role_changed_audit_rows(pg_pool, target_id) == []


@pytest.mark.asyncio
async def test_patch_role_404s_for_a_nonexistent_user_pg(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings()
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    missing_id = uuid.uuid4()

    app = _build_app(pg_pool)
    async with await _client(app) as client:
        sid = await _login_via_cas(client, settings, monkeypatch, DEFAULT_ADMIN)
        resp = await client.patch(
            f"/users/{missing_id}/role",
            json={"role": "recruiter"},
            cookies={settings.session_cookie_name: sid},
        )

    assert resp.status_code == 404
    # Strengthens the bare status check: FastAPI's own unmatched-route 404
    # ({"detail": "Not Found"}, no "code" key) would otherwise pass this
    # assertion vacuously before the route even exists. The route's real
    # NotFoundError -> _app_error_handler envelope always carries "code";
    # assert on it so this test only goes green once the route genuinely
    # resolves the target-user 404 itself.
    assert resp.json().get("code") == "resource.not_found"


# ── malformed body ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_patch_role_422s_on_an_unknown_role_string_pg(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings()
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    monkeypatch.setattr(deps, "get_settings", lambda: settings)

    target_id = await _insert_user(
        pg_pool, cas_username=_unique_username("target"), role="recruiter"
    )

    app = _build_app(pg_pool)
    async with await _client(app) as client:
        sid = await _login_via_cas(client, settings, monkeypatch, DEFAULT_ADMIN)
        resp = await client.patch(
            f"/users/{target_id}/role",
            json={"role": "bogus"},
            cookies={settings.session_cookie_name: sid},
        )

    assert resp.status_code == 422
    assert await _get_role(pg_pool, target_id) == "recruiter"


@pytest.mark.asyncio
async def test_patch_role_422s_on_null_role_pg(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This endpoint only ASSIGNS a real role — ``{"role": null}`` must never
    clear a role via this route, even for an admin session."""
    settings = _settings()
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    monkeypatch.setattr(deps, "get_settings", lambda: settings)

    target_id = await _insert_user(
        pg_pool, cas_username=_unique_username("target"), role="recruiter"
    )

    app = _build_app(pg_pool)
    async with await _client(app) as client:
        sid = await _login_via_cas(client, settings, monkeypatch, DEFAULT_ADMIN)
        resp = await client.patch(
            f"/users/{target_id}/role",
            json={"role": None},
            cookies={settings.session_cookie_name: sid},
        )

    assert resp.status_code == 422
    assert await _get_role(pg_pool, target_id) == "recruiter"


# ── the last-admin-lockout guard ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_last_admin_cannot_demote_themselves_pg(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With exactly ONE active admin (the default-admin's own first login,
    and nothing else in this freshly-truncated table), that admin PATCHing
    their OWN role away from 'admin' must 409 — the row stays 'admin' and
    NO new audit row is written."""
    settings = _settings()
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    monkeypatch.setattr(deps, "get_settings", lambda: settings)

    app = _build_app(pg_pool)
    async with await _client(app) as client:
        sid = await _login_via_cas(client, settings, monkeypatch, DEFAULT_ADMIN)
        admin_id = await _user_id_by_username(pg_pool, DEFAULT_ADMIN)

        async with pg_pool.acquire() as conn:
            admin_count = await conn.fetchval(
                "SELECT count(*) FROM users WHERE role = 'admin' AND active"
            )
        assert admin_count == 1, "fixture assumption: exactly one active admin"

        resp = await client.patch(
            f"/users/{admin_id}/role",
            json={"role": "recruiter"},
            cookies={settings.session_cookie_name: sid},
        )

    assert resp.status_code == 409
    assert await _get_role(pg_pool, admin_id) == "admin"
    assert await _role_changed_audit_rows(pg_pool, admin_id) == []


@pytest.mark.asyncio
async def test_self_demotion_succeeds_when_a_second_admin_exists_pg(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lockout guard's negative case, against real Postgres: seed a
    SECOND active admin, then have the default admin demote THEMSELVES — the
    real ``COUNT(*) WHERE role='admin' AND active`` is now 2, so the
    demotion must succeed (200), the row changes, and exactly one audit row
    is written."""
    settings = _settings()
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    monkeypatch.setattr(deps, "get_settings", lambda: settings)

    await _insert_user(
        pg_pool, cas_username=_unique_username("second-admin"), role="admin"
    )

    app = _build_app(pg_pool)
    async with await _client(app) as client:
        sid = await _login_via_cas(client, settings, monkeypatch, DEFAULT_ADMIN)
        admin_id = await _user_id_by_username(pg_pool, DEFAULT_ADMIN)

        resp = await client.patch(
            f"/users/{admin_id}/role",
            json={"role": "recruiter"},
            cookies={settings.session_cookie_name: sid},
        )

    assert resp.status_code == 200
    assert await _get_role(pg_pool, admin_id) == "recruiter"

    rows = await _role_changed_audit_rows(pg_pool, admin_id)
    assert len(rows) == 1
    assert _details(rows[0]) == {"old_role": "admin", "new_role": "recruiter"}


@pytest.mark.asyncio
async def test_lockout_does_not_block_demoting_a_non_last_admin_pg(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Symmetric check: with two active admins, demoting the OTHER admin
    (not the acting admin themselves) must also succeed — the guard is about
    the real admin count, not specifically about self-demotion."""
    settings = _settings()
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    monkeypatch.setattr(deps, "get_settings", lambda: settings)

    second_admin_id = await _insert_user(
        pg_pool, cas_username=_unique_username("second-admin"), role="admin"
    )

    app = _build_app(pg_pool)
    async with await _client(app) as client:
        sid = await _login_via_cas(client, settings, monkeypatch, DEFAULT_ADMIN)

        resp = await client.patch(
            f"/users/{second_admin_id}/role",
            json={"role": "auditor"},
            cookies={settings.session_cookie_name: sid},
        )

    assert resp.status_code == 200
    assert await _get_role(pg_pool, second_admin_id) == "auditor"
    assert len(await _role_changed_audit_rows(pg_pool, second_admin_id)) == 1


# ── SECURITY-CRITICAL: the role UPDATE and the audit write are atomic ─────


@pytest.mark.asyncio
async def test_audit_write_failure_rolls_back_the_role_change_too(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mirrors ``test_blind_review_audit_pg.py``'s own crash-injection
    technique: force the real ``audit_service.record_audit`` call to raise
    AFTER the real UPDATE would have applied, and assert the row is
    UNCHANGED once the exception has propagated — a role-changed-but-
    unaudited row must never be observable."""
    from src.api.routes import users as users_routes

    settings = _settings()
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    monkeypatch.setattr(
        users_routes.audit_service,
        "record_audit",
        AsyncMock(side_effect=RuntimeError("simulated audit-write crash")),
    )

    target_id = await _insert_user(
        pg_pool, cas_username=_unique_username("target"), role="recruiter"
    )

    app = _build_app(pg_pool)
    async with await _client(app) as client:
        sid = await _login_via_cas(client, settings, monkeypatch, DEFAULT_ADMIN)
        with pytest.raises(RuntimeError):
            await client.patch(
                f"/users/{target_id}/role",
                json={"role": "hiring_manager"},
                cookies={settings.session_cookie_name: sid},
            )

    # The role change must NOT have applied — proves atomicity, not merely
    # "an error surfaced somewhere".
    assert await _get_role(pg_pool, target_id) == "recruiter"
    assert await _role_changed_audit_rows(pg_pool, target_id) == []
