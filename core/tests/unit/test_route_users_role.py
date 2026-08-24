"""Route-level tests for user-admin-roles slice 6 — ``PATCH
/users/{user_id}/role``, the merge-blocking payoff slice: an admin assigns a
user's role, with a ``role_changed`` audit row and a last-admin-lockout
guard.

``src.api.routes.users`` does not have this route yet — every test below
fails at ``AttributeError``/404 (route not registered) or import, RED half of
the TDD cycle.

Mirrors ``test_route_users.py``'s (slice 5) and
``test_route_job_assignees.py``'s (FU-6 slice 3) conventions: a fresh
``FastAPI()``, a mocked ``asyncpg`` connection (with a working
``.transaction()`` async-context-manager stub, since this route's write +
audit must run inside one), ``resolve_user`` overridden per test to control
the acting admin's session identity, and the three service calls
(``user_service.get_by_id``/``set_role``/``count_active_admins``,
``audit_service.record_audit``) monkeypatched at the route module boundary —
isolating the route's gating/orchestration contract from each service's own
SQL (covered separately in ``test_user_service.py`` and, against real
Postgres, in ``test_users_admin_routes_pg.py``).

**THE SURFACE this file pins (task's design note, verbatim logic):**

1. Fetch the target user (``get_by_id``); 404 if missing.
2. LAST-ADMIN LOCKOUT: if the target is currently ``role='admin'`` AND the
   new role is not ``'admin'`` AND ``count_active_admins() == 1`` -> 409
   (``ConflictError``), and NO audit row is written (mirrors
   ``job_assignees``'s "a no-op gets no audit row").
3. Otherwise: ``set_role`` + ``record_audit`` (action ``'role_changed'``),
   both inside the SAME ``conn.transaction()``.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
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


def _mock_conn() -> MagicMock:
    conn = MagicMock(name="conn")
    conn.execute = AsyncMock(return_value="UPDATE 1")
    conn.fetchrow = AsyncMock(return_value=None)
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchval = AsyncMock(return_value=None)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=None)
    cm.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=cm)
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


def _patch_services(
    monkeypatch: pytest.MonkeyPatch,
    *,
    target_user: User | None,
    active_admin_count: int = 2,
) -> tuple[AsyncMock, AsyncMock, AsyncMock, AsyncMock]:
    from src.api.routes import users as users_routes

    get_by_id = AsyncMock(return_value=target_user)
    set_role = AsyncMock(return_value=None)
    count_active_admins = AsyncMock(return_value=active_admin_count)
    record_audit = AsyncMock(return_value=None)
    monkeypatch.setattr(users_routes.user_service, "get_by_id", get_by_id)
    monkeypatch.setattr(users_routes.user_service, "set_role", set_role)
    monkeypatch.setattr(
        users_routes.user_service, "count_active_admins", count_active_admins
    )
    monkeypatch.setattr(users_routes.audit_service, "record_audit", record_audit)
    return get_by_id, set_role, count_active_admins, record_audit


# ── the admin-session gate (reuses _require_admin_session) ────────────────


@pytest.mark.asyncio
async def test_patch_role_403s_when_no_session_resolves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _real_user(cas_username="target", role="recruiter")
    get_by_id, set_role, _count, audit = _patch_services(
        monkeypatch, target_user=target
    )
    app = _build_app(_mock_conn(), user=None)
    async with await _client(app) as client:
        resp = await client.patch(
            f"/users/{target.id}/role", json={"role": "hiring_manager"}
        )
    assert resp.status_code == 403
    get_by_id.assert_not_awaited()
    set_role.assert_not_awaited()
    audit.assert_not_awaited()


@pytest.mark.parametrize("role", _NON_ADMIN_ROLES)
@pytest.mark.asyncio
async def test_patch_role_403s_every_non_admin_session_role(
    role: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _real_user(cas_username="target", role="recruiter")
    get_by_id, set_role, _count, audit = _patch_services(
        monkeypatch, target_user=target
    )
    app = _build_app(_mock_conn(), user=_real_user(role=role))
    async with await _client(app) as client:
        resp = await client.patch(
            f"/users/{target.id}/role", json={"role": "hiring_manager"}
        )
    assert resp.status_code == 403
    get_by_id.assert_not_awaited()
    set_role.assert_not_awaited()
    audit.assert_not_awaited()


@pytest.mark.asyncio
async def test_patch_role_403s_a_real_session_with_no_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _real_user(cas_username="target", role="recruiter")
    get_by_id, set_role, _count, audit = _patch_services(
        monkeypatch, target_user=target
    )
    app = _build_app(_mock_conn(), user=_real_user(role=None))
    async with await _client(app) as client:
        resp = await client.patch(
            f"/users/{target.id}/role", json={"role": "hiring_manager"}
        )
    assert resp.status_code == 403
    get_by_id.assert_not_awaited()
    set_role.assert_not_awaited()
    audit.assert_not_awaited()


# ── happy path — a valid admin assigns a role ──────────────────────────────


@pytest.mark.asyncio
async def test_patch_role_by_admin_returns_200(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _real_user(cas_username="target", role="recruiter")
    _patch_services(monkeypatch, target_user=target, active_admin_count=2)
    app = _build_app(_mock_conn(), user=_real_user(role="admin"))
    async with await _client(app) as client:
        resp = await client.patch(
            f"/users/{target.id}/role", json={"role": "hiring_manager"}
        )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_patch_role_by_admin_calls_set_role_with_target_and_new_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _real_user(cas_username="target", role="recruiter")
    _get_by_id, set_role, _count, _audit = _patch_services(
        monkeypatch, target_user=target, active_admin_count=2
    )
    app = _build_app(_mock_conn(), user=_real_user(role="admin"))
    async with await _client(app) as client:
        resp = await client.patch(
            f"/users/{target.id}/role", json={"role": "hiring_manager"}
        )
    assert resp.status_code == 200
    set_role.assert_awaited_once()
    args = set_role.await_args.args
    assert target.id in args
    assert "hiring_manager" in args


@pytest.mark.asyncio
async def test_patch_role_by_admin_writes_role_changed_audit_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _real_user(cas_username="target", role="recruiter")
    actor = _real_user(cas_username="acting-admin", role="admin")
    _get_by_id, _set_role, _count, audit = _patch_services(
        monkeypatch, target_user=target, active_admin_count=2
    )
    app = _build_app(_mock_conn(), user=actor)
    async with await _client(app) as client:
        resp = await client.patch(
            f"/users/{target.id}/role", json={"role": "hiring_manager"}
        )
    assert resp.status_code == 200
    audit.assert_awaited_once()
    kwargs = audit.await_args.kwargs
    assert kwargs["actor_kind"] == "user"
    assert kwargs["actor_user_id"] == actor.id
    assert kwargs.get("actor_service") is None
    assert kwargs["action"] == "role_changed"
    assert kwargs["subject_type"] == "user"
    assert kwargs["subject_id"] == target.id
    assert kwargs["details"] == {"old_role": "recruiter", "new_role": "hiring_manager"}


@pytest.mark.asyncio
async def test_patch_role_from_null_role_records_old_role_null_in_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A first-login no-role user (slice 2's captured state) being assigned
    their first real role — ``old_role`` must be ``None`` in the audit
    details, not dropped or coerced to a string."""
    target = _real_user(cas_username="no-role-target", role=None)
    _get_by_id, _set_role, _count, audit = _patch_services(
        monkeypatch, target_user=target, active_admin_count=2
    )
    app = _build_app(_mock_conn(), user=_real_user(role="admin"))
    async with await _client(app) as client:
        resp = await client.patch(
            f"/users/{target.id}/role", json={"role": "recruiter"}
        )
    assert resp.status_code == 200
    kwargs = audit.await_args.kwargs
    assert kwargs["details"] == {"old_role": None, "new_role": "recruiter"}


@pytest.mark.asyncio
async def test_patch_role_by_the_dev_anonymous_sentinel_records_service_actor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CAS-disabled synthetic dev-admin acting as the admin session must
    be attributed ``actor_kind='service'``/``actor_service='dev-anonymous'``
    (``actor_fields_from_user``'s case 2) — never ``actor_kind='user'``
    against the sentinel id, which is not a real ``users`` row and would
    violate ``audit_log.actor_user_id``'s FK."""
    target = _real_user(cas_username="target", role="recruiter")
    _get_by_id, _set_role, _count, audit = _patch_services(
        monkeypatch, target_user=target, active_admin_count=2
    )
    app = _build_app(_mock_conn(), user=_dev_anonymous_admin())
    async with await _client(app) as client:
        resp = await client.patch(
            f"/users/{target.id}/role", json={"role": "hiring_manager"}
        )
    assert resp.status_code == 200
    kwargs = audit.await_args.kwargs
    assert kwargs["actor_kind"] == "service"
    assert kwargs["actor_user_id"] is None
    assert kwargs["actor_service"] == "dev-anonymous"


@pytest.mark.asyncio
async def test_patch_role_set_role_runs_before_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The write + audit are meant to be atomic (mirrors ``blind_review``'s
    flip / ``job_assignees``'s assign pattern) — proved fully only against a
    real Postgres (the companion integration file), but the call ORDER is
    provable here."""
    target = _real_user(cas_username="target", role="recruiter")
    order: list[str] = []

    from src.api.routes import users as users_routes

    async def _get_by_id_side_effect(*_a: Any, **_kw: Any) -> User:
        return target

    async def _set_role_side_effect(*_a: Any, **_kw: Any) -> None:
        order.append("set_role")

    async def _count_side_effect(*_a: Any, **_kw: Any) -> int:
        return 2

    async def _audit_side_effect(*_a: Any, **_kw: Any) -> None:
        order.append("audit")

    monkeypatch.setattr(
        users_routes.user_service,
        "get_by_id",
        AsyncMock(side_effect=_get_by_id_side_effect),
    )
    monkeypatch.setattr(
        users_routes.user_service,
        "set_role",
        AsyncMock(side_effect=_set_role_side_effect),
    )
    monkeypatch.setattr(
        users_routes.user_service,
        "count_active_admins",
        AsyncMock(side_effect=_count_side_effect),
    )
    monkeypatch.setattr(
        users_routes.audit_service,
        "record_audit",
        AsyncMock(side_effect=_audit_side_effect),
    )

    app = _build_app(_mock_conn(), user=_real_user(role="admin"))
    async with await _client(app) as client:
        resp = await client.patch(
            f"/users/{target.id}/role", json={"role": "hiring_manager"}
        )
    assert resp.status_code == 200
    assert order == ["set_role", "audit"]


# ── 404 — target user does not exist ───────────────────────────────────────


@pytest.mark.asyncio
async def test_patch_role_404s_when_target_user_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing_id = uuid4()
    get_by_id, set_role, _count, audit = _patch_services(monkeypatch, target_user=None)
    app = _build_app(_mock_conn(), user=_real_user(role="admin"))
    async with await _client(app) as client:
        resp = await client.patch(
            f"/users/{missing_id}/role", json={"role": "recruiter"}
        )
    assert resp.status_code == 404
    set_role.assert_not_awaited()
    audit.assert_not_awaited()


# ── the last-admin lockout guard (409, no audit row) ───────────────────────


@pytest.mark.asyncio
async def test_patch_role_409s_when_demoting_the_last_active_admin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _real_user(cas_username="lone-admin", role="admin")
    get_by_id, set_role, count_active_admins, audit = _patch_services(
        monkeypatch, target_user=target, active_admin_count=1
    )
    app = _build_app(_mock_conn(), user=_real_user(role="admin"))
    async with await _client(app) as client:
        resp = await client.patch(
            f"/users/{target.id}/role", json={"role": "recruiter"}
        )
    assert resp.status_code == 409
    set_role.assert_not_awaited()
    audit.assert_not_awaited()
    count_active_admins.assert_awaited()


@pytest.mark.asyncio
async def test_patch_role_allows_demotion_when_a_second_admin_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lockout guard's negative case: exactly the same target/new-role
    shape as the 409 test above, but a SECOND active admin exists — the
    demotion must succeed."""
    target = _real_user(cas_username="one-of-two-admins", role="admin")
    _get_by_id, set_role, _count, audit = _patch_services(
        monkeypatch, target_user=target, active_admin_count=2
    )
    app = _build_app(_mock_conn(), user=_real_user(role="admin"))
    async with await _client(app) as client:
        resp = await client.patch(
            f"/users/{target.id}/role", json={"role": "recruiter"}
        )
    assert resp.status_code == 200
    set_role.assert_awaited_once()
    audit.assert_awaited_once()


@pytest.mark.asyncio
async def test_patch_role_lockout_does_not_trigger_when_new_role_is_still_admin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-assigning the sole admin to 'admin' again is not a demotion at all
    — the lockout guard must not fire (``new_role != 'admin'`` is part of
    its condition)."""
    target = _real_user(cas_username="lone-admin", role="admin")
    _get_by_id, set_role, _count, audit = _patch_services(
        monkeypatch, target_user=target, active_admin_count=1
    )
    app = _build_app(_mock_conn(), user=_real_user(role="admin"))
    async with await _client(app) as client:
        resp = await client.patch(f"/users/{target.id}/role", json={"role": "admin"})
    assert resp.status_code == 200
    set_role.assert_awaited_once()
    audit.assert_awaited_once()


@pytest.mark.asyncio
async def test_patch_role_lockout_does_not_trigger_for_a_non_admin_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lockout guard only ever applies to a target CURRENTLY role='admin'
    — changing a recruiter's role must never consult/care about the active
    admin count."""
    target = _real_user(cas_username="a-recruiter", role="recruiter")
    _get_by_id, set_role, count_active_admins, audit = _patch_services(
        monkeypatch, target_user=target, active_admin_count=1
    )
    app = _build_app(_mock_conn(), user=_real_user(role="admin"))
    async with await _client(app) as client:
        resp = await client.patch(f"/users/{target.id}/role", json={"role": "auditor"})
    assert resp.status_code == 200
    set_role.assert_awaited_once()
    audit.assert_awaited_once()


# ── malformed body ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_patch_role_422_on_an_unknown_role_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _real_user(cas_username="target", role="recruiter")
    get_by_id, set_role, _count, audit = _patch_services(
        monkeypatch, target_user=target
    )
    app = _build_app(_mock_conn(), user=_real_user(role="admin"))
    async with await _client(app) as client:
        resp = await client.patch(
            f"/users/{target.id}/role", json={"role": "superuser"}
        )
    assert resp.status_code == 422
    get_by_id.assert_not_awaited()
    set_role.assert_not_awaited()
    audit.assert_not_awaited()


@pytest.mark.asyncio
async def test_patch_role_422_on_null_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """This endpoint only ASSIGNS a real role — ``{"role": null}`` must never
    clear a role via this route."""
    target = _real_user(cas_username="target", role="recruiter")
    get_by_id, set_role, _count, audit = _patch_services(
        monkeypatch, target_user=target
    )
    app = _build_app(_mock_conn(), user=_real_user(role="admin"))
    async with await _client(app) as client:
        resp = await client.patch(f"/users/{target.id}/role", json={"role": None})
    assert resp.status_code == 422
    get_by_id.assert_not_awaited()
    set_role.assert_not_awaited()
    audit.assert_not_awaited()


@pytest.mark.asyncio
async def test_patch_role_422_on_missing_role_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _real_user(cas_username="target", role="recruiter")
    _patch_services(monkeypatch, target_user=target)
    app = _build_app(_mock_conn(), user=_real_user(role="admin"))
    async with await _client(app) as client:
        resp = await client.patch(f"/users/{target.id}/role", json={})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_patch_role_422_on_non_uuid_user_id_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_services(monkeypatch, target_user=_real_user(role="recruiter"))
    app = _build_app(_mock_conn(), user=_real_user(role="admin"))
    async with await _client(app) as client:
        resp = await client.patch("/users/not-a-uuid/role", json={"role": "recruiter"})
    assert resp.status_code == 422


__all__: list[str] = []
