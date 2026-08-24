"""Route-level tests for user-admin-roles slice 5 — ``GET /users``, an
admin-only listing of every ``users`` row, the surface slice 6 will use to
pick a target for a role change.

``src.api.routes.users`` does not exist yet — the whole file fails at
collection (``ModuleNotFoundError``). RED half of the TDD cycle.

**The core design decision this file pins (planner decision #3, task
context).** The gate is on the CAS SESSION role
(``resolve_user().role == "admin"``), NOT the API-key role
(``resolve_role``/``require_role``). The Flask viewer sends the ONE shared
``recruiter`` API key for every browser user
(``core/frontend/api_client.py``) — a key-role gate would 403 every real
admin browsing through that shared key, and would ALSO let a bare
service/admin KEY with no verifiable session list every user, which is
exactly the "is a real human" gap ``job_assignees._require_real_assigner``
(ADR-020 §2, FU-6 slice 3) left open by only checking "a real human session
resolves" and never checking that session's ROLE. This module's
``_require_admin_session`` closes both gaps in one gate: ``user is not None
and user.role == "admin"`` — no key is consulted at all.

Three ``resolve_user`` resolutions to distinguish, mirroring
``test_api_deps.py``'s ``require_role_assigned`` decision table and
``test_route_job_assignees.py``'s real-assigner-gate pattern:

* ``user is None`` (no session at all, e.g. a bare API-key caller) -> 403.
* a real session whose ``role`` is anything other than ``"admin"``
  (``recruiter``/``hiring_manager``/``auditor``/``None``) -> 403.
* a real session with ``role == "admin"``, OR the CAS-disabled synthetic
  dev-anonymous sentinel (``role="admin"`` — see ``deps.
  _DEV_ADMIN_SENTINEL_ID``) -> 200, the list.

Mirrors ``test_route_job_assignees.py``/``test_route_audit.py``'s
conventions: a fresh ``FastAPI()`` (not the whole ``src.api.main`` app), a
mocked ``asyncpg`` connection via ``app.dependency_overrides[get_db]``, and
``resolve_user`` overridden per test to control the resolved session
identity. ``resolve_role``/the API key are NEVER touched here — proving the
route does not consult them at all is the point of this file.

``user_service.list_users`` is monkeypatched at the route module boundary
(``users_routes.user_service.list_users``), the same technique
``test_route_audit.py`` uses for its DB-boundary mock — isolating the
route's admin-session gate and pass-through contract from the service's own
SQL (covered separately in ``test_user_service.py`` and, against real
Postgres, in ``test_users_admin_routes_pg.py``).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

from src.api.deps import _DEV_ADMIN_SENTINEL_ID, resolve_user
from src.errors import AppError
from src.models.pool import get_db
from src.schemas.auth import User

_NOW = dt.datetime(2026, 7, 25, tzinfo=dt.UTC)

_NON_ADMIN_ROLES: tuple[str, ...] = ("recruiter", "hiring_manager", "auditor")


def _real_user(*, cas_username: str = "priya", role: str | None = "admin") -> User:
    return User(
        id=uuid4(),
        cas_username=cas_username,
        display_name=cas_username,
        email=f"{cas_username}@example.org",
        role=role,
        active=True,
        created_at=_NOW,
        last_seen_at=_NOW,
    )


def _dev_anonymous_admin() -> User:
    """The exact synthetic identity ``resolve_user`` returns when
    ``cas_enabled=False`` — not a real ``users`` row, but ``role="admin"``,
    so it must PASS this gate (mirrors ``require_role_assigned``'s and
    ``scoped_user_id_or_403``'s own treatment of the sentinel)."""
    return User(
        id=_DEV_ADMIN_SENTINEL_ID,
        cas_username="dev-anonymous",
        display_name="Dev Anonymous (auth disabled)",
        email=None,
        role="admin",
        active=True,
        created_at=_NOW,
        last_seen_at=_NOW,
    )


def _seeded_users() -> list[User]:
    return [
        _real_user(cas_username="alice", role="admin"),
        _real_user(cas_username="bob", role="recruiter"),
        _real_user(cas_username="carol", role=None),
    ]


def _mock_conn() -> MagicMock:
    conn = MagicMock(name="conn")
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchrow = AsyncMock(return_value=None)
    conn.fetchval = AsyncMock(return_value=None)
    return conn


def _build_app(conn: MagicMock, *, user: User | None) -> FastAPI:
    from src.api.routes import users as users_routes

    app = FastAPI()
    app.include_router(users_routes.router)

    async def _get_db_override() -> AsyncIterator[Any]:
        yield conn

    app.dependency_overrides[get_db] = _get_db_override
    app.dependency_overrides[resolve_user] = lambda: user

    @app.exception_handler(AppError)
    async def _app_error_handler(_request: Any, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status, content={"detail": exc.message, "code": exc.code}
        )

    return app


async def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _patch_list_users(monkeypatch: pytest.MonkeyPatch, users: list[User]) -> AsyncMock:
    from src.api.routes import users as users_routes

    mock = AsyncMock(return_value=users)
    monkeypatch.setattr(users_routes.user_service, "list_users", mock)
    return mock


# ── _require_admin_session — pure gate logic ─────────────────────────────


@pytest.mark.asyncio
async def test_require_admin_session_403s_when_no_session_resolves() -> None:
    from src.api.routes.users import _require_admin_session

    with pytest.raises(HTTPException) as excinfo:
        await _require_admin_session(user=None)
    assert excinfo.value.status_code == 403


@pytest.mark.parametrize("role", _NON_ADMIN_ROLES)
@pytest.mark.asyncio
async def test_require_admin_session_403s_every_non_admin_role(role: str) -> None:
    from src.api.routes.users import _require_admin_session

    user = _real_user(role=role)
    with pytest.raises(HTTPException) as excinfo:
        await _require_admin_session(user=user)
    assert excinfo.value.status_code == 403


@pytest.mark.asyncio
async def test_require_admin_session_403s_a_real_session_with_no_role() -> None:
    from src.api.routes.users import _require_admin_session

    user = _real_user(role=None)
    with pytest.raises(HTTPException) as excinfo:
        await _require_admin_session(user=user)
    assert excinfo.value.status_code == 403


@pytest.mark.asyncio
async def test_require_admin_session_passes_a_real_admin_session() -> None:
    from src.api.routes.users import _require_admin_session

    user = _real_user(role="admin")
    result = await _require_admin_session(user=user)
    assert result is user


@pytest.mark.asyncio
async def test_require_admin_session_passes_the_dev_anonymous_sentinel() -> None:
    """The CAS-disabled synthetic dev-admin identity (``role='admin'``) is
    not a no-role human and not a real ``users`` row, but it legitimately
    resolves ``role='admin'`` — must PASS, exactly like
    ``require_role_assigned``/``scoped_user_id_or_403`` treat it."""
    from src.api.routes.users import _require_admin_session

    user = _dev_anonymous_admin()
    result = await _require_admin_session(user=user)
    assert result is user


@pytest.mark.asyncio
async def test_require_admin_session_403_has_a_non_empty_detail() -> None:
    from src.api.routes.users import _require_admin_session

    with pytest.raises(HTTPException) as excinfo:
        await _require_admin_session(user=None)
    assert excinfo.value.detail


@pytest.mark.asyncio
async def test_require_admin_session_403_matches_the_reveal_403_mechanism() -> None:
    """Same plain ``fastapi.HTTPException(status_code=403, ...)`` mechanism
    every other 403 in this API uses — not a bespoke ``AppError`` subclass."""
    from src.api.routes.users import _require_admin_session

    with pytest.raises(HTTPException) as excinfo:
        await _require_admin_session(user=_real_user(role="recruiter"))
    assert isinstance(excinfo.value, HTTPException)
    assert excinfo.value.status_code == 403


# ── GET /users — role gate, through the real route ───────────────────────


@pytest.mark.asyncio
async def test_get_users_403s_when_no_session_at_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    list_users = _patch_list_users(monkeypatch, _seeded_users())
    app = _build_app(_mock_conn(), user=None)
    async with await _client(app) as client:
        resp = await client.get("/users")
    assert resp.status_code == 403
    list_users.assert_not_awaited()


@pytest.mark.parametrize("role", _NON_ADMIN_ROLES)
@pytest.mark.asyncio
async def test_get_users_403s_every_non_admin_session_role(
    role: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    list_users = _patch_list_users(monkeypatch, _seeded_users())
    app = _build_app(_mock_conn(), user=_real_user(role=role))
    async with await _client(app) as client:
        resp = await client.get("/users")
    assert resp.status_code == 403
    list_users.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_users_403s_a_real_session_with_no_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact hole this slice closes: a genuinely no-role human session
    (slice 2's first-login capture) must never see the admin user listing,
    regardless of which shared API key it also presents."""
    list_users = _patch_list_users(monkeypatch, _seeded_users())
    app = _build_app(_mock_conn(), user=_real_user(role=None))
    async with await _client(app) as client:
        resp = await client.get("/users")
    assert resp.status_code == 403
    list_users.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_users_200s_for_a_real_admin_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    users = _seeded_users()
    _patch_list_users(monkeypatch, users)
    app = _build_app(_mock_conn(), user=_real_user(role="admin"))
    async with await _client(app) as client:
        resp = await client.get("/users")
    assert resp.status_code == 200
    assert len(resp.json()) == len(users)


@pytest.mark.asyncio
async def test_get_users_200s_for_the_dev_anonymous_sentinel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    users = _seeded_users()
    _patch_list_users(monkeypatch, users)
    app = _build_app(_mock_conn(), user=_dev_anonymous_admin())
    async with await _client(app) as client:
        resp = await client.get("/users")
    assert resp.status_code == 200
    assert len(resp.json()) == len(users)


# ── GET /users — response shape ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_users_returns_a_bare_list_not_a_wrapped_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    users = _seeded_users()
    _patch_list_users(monkeypatch, users)
    app = _build_app(_mock_conn(), user=_real_user(role="admin"))
    async with await _client(app) as client:
        resp = await client.get("/users")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)


@pytest.mark.asyncio
async def test_get_users_exposes_exactly_the_expected_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An admin surface — cas_username/email/role are INTENDED to be
    visible — but pins EXACTLY the field set, so nothing extra (a password
    hash, a session token, an internal column) ever leaks through it."""
    users = [_real_user(cas_username="dana", role="hiring_manager")]
    _patch_list_users(monkeypatch, users)
    app = _build_app(_mock_conn(), user=_real_user(role="admin"))
    async with await _client(app) as client:
        resp = await client.get("/users")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    item = body[0]
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
    assert item["cas_username"] == "dana"
    assert item["role"] == "hiring_manager"


@pytest.mark.asyncio
async def test_get_users_shows_a_no_role_user_with_a_null_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    users = [_real_user(cas_username="erin", role=None)]
    _patch_list_users(monkeypatch, users)
    app = _build_app(_mock_conn(), user=_real_user(role="admin"))
    async with await _client(app) as client:
        resp = await client.get("/users")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["cas_username"] == "erin"
    assert body[0]["role"] is None


@pytest.mark.asyncio
async def test_get_users_empty_list_when_no_users_exist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_list_users(monkeypatch, [])
    app = _build_app(_mock_conn(), user=_real_user(role="admin"))
    async with await _client(app) as client:
        resp = await client.get("/users")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_get_users_calls_list_users_with_the_injected_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The route must pass through the request-scoped DB connection to the
    service, not open a fresh one / use a module-global pool."""
    conn = _mock_conn()
    list_users = _patch_list_users(monkeypatch, [])
    app = _build_app(conn, user=_real_user(role="admin"))
    async with await _client(app) as client:
        resp = await client.get("/users")
    assert resp.status_code == 200
    list_users.assert_awaited_once()
    assert list_users.await_args.args[0] is conn


__all__: list[str] = []
