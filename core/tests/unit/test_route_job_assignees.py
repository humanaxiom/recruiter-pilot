"""Route-level tests for FU-6 slice 3's assign/unassign routes —
``src.api.routes.job_assignees`` (new module; does not exist yet). The whole
file fails at collection (``ModuleNotFoundError``) — RED half of the TDD
cycle.

Mirrors ``test_route_jobs.py``/``test_route_reveal.py``'s conventions: a
fresh ``FastAPI()`` (not the whole ``src.api.main`` app), mocked ``asyncpg``
connection via ``app.dependency_overrides[get_db]``, ``resolve_role``
overridden to bypass key->role resolution while the real per-route
``require_role(...)`` check still runs, and ``resolve_user`` overridden per
test to control the resolved session identity.

**ADR-020 §2 — who may assign (the contract this file pins):**

* ``POST /jobs/{job_id}/assignees`` and ``DELETE
  /jobs/{job_id}/assignees/{user_id}`` are gated ``require_role(Role.ADMIN,
  Role.RECRUITER)`` — auditor and hiring_manager get 403 on BOTH routes.
  "Auditor credentials may never assign; assignment is an operational act,
  not an audit act." (ADR-020 §2, quoted verbatim in the named negative test
  below.)
* ``action='assign_job'`` / ``action='unassign_job'`` are written to
  ``audit_log`` via ``audit_service.record_audit``.

**The FU-5-forced wrinkle this file also pins (see the task's design note):**
``job_assignees.assigned_by`` is a NOT-NULL FK to ``users(id)``. The
CAS-disabled synthetic dev-admin (``resolve_user``'s
``_DEV_ADMIN_SENTINEL_ID``) is not a real ``users`` row, so it can be neither
``assigned_by`` (FK violation) nor a real audit ``actor_user_id``. Both
routes therefore ALSO require a real, attributable ``resolve_user`` result —
non-``None`` and not the sentinel id — or they 403, exactly like reveal's
human-attribution gate, and BEFORE either the assign/unassign service call or
the audit write.

Two service calls are monkeypatched at the route module boundary
(``job_assignees_routes.job_assignee_service.{assign,unassign}`` and
``job_assignees_routes.audit_service.record_audit``) — the same technique
``test_route_jobs.py`` uses for ``job_service.audit_service.record_audit`` —
so this isolates the ROUTE's role/identity-gating and pass-through contract
from each service's own internals (covered by
``test_job_assignee_service_pg.py`` / ``test_audit_service.py``).
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

from src.api.deps import _DEV_ADMIN_SENTINEL_ID, Role, resolve_role, resolve_user
from src.errors import AppError
from src.models.pool import get_db
from src.schemas.auth import User

_NOW = dt.datetime(2026, 7, 22, tzinfo=dt.UTC)

# ADR-020 §2 — the disallowed roles for BOTH routes in this file.
_NON_ASSIGNER_ROLES: tuple[Role, ...] = (Role.HIRING_MANAGER, Role.AUDITOR)
_ASSIGNER_ROLES: tuple[Role, ...] = (Role.ADMIN, Role.RECRUITER)


def _mock_conn() -> MagicMock:
    conn = MagicMock(name="conn")
    conn.execute = AsyncMock(return_value="DELETE 1")
    conn.fetchrow = AsyncMock(return_value=None)
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchval = AsyncMock(return_value=None)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=None)
    cm.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=cm)
    return conn


def _real_user(*, cas_username: str = "priya", role: str = "admin") -> User:
    return User(
        id=uuid4(),
        cas_username=cas_username,
        display_name=cas_username,
        email=None,
        role=role,
        active=True,
        created_at=_NOW,
        last_seen_at=_NOW,
    )


def _sentinel_dev_admin_user() -> User:
    """The CAS-disabled synthetic identity ``resolve_user`` returns when
    ``cas_enabled=False`` — NOT a real ``users`` row (see module docstring)."""
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


def _build_app(conn: MagicMock, *, role: Role = Role.ADMIN) -> FastAPI:
    from src.api.routes import job_assignees as job_assignees_routes

    app = FastAPI()
    app.include_router(job_assignees_routes.router)

    async def _get_db_override() -> AsyncIterator[Any]:
        yield conn

    app.dependency_overrides[get_db] = _get_db_override
    app.dependency_overrides[resolve_role] = lambda: role
    # Ambient default for every test in this file: a REAL, attributable
    # session identity — individual tests override this to prove the
    # None/sentinel 403 gate.
    app.dependency_overrides[resolve_user] = lambda: _real_user(
        role=role.value if hasattr(role, "value") else str(role)
    )

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
    assign_result: None = None,
    unassign_result: bool = True,
) -> tuple[AsyncMock, AsyncMock, AsyncMock]:
    from src.api.routes import job_assignees as job_assignees_routes

    assign = AsyncMock(return_value=assign_result)
    unassign = AsyncMock(return_value=unassign_result)
    audit = AsyncMock(return_value=None)
    monkeypatch.setattr(job_assignees_routes.job_assignee_service, "assign", assign)
    monkeypatch.setattr(job_assignees_routes.job_assignee_service, "unassign", unassign)
    monkeypatch.setattr(job_assignees_routes.audit_service, "record_audit", audit)
    return assign, unassign, audit


# ── POST /jobs/{job_id}/assignees — happy path ─────────────────────────────


@pytest.mark.asyncio
async def test_post_assignee_as_admin_with_real_user_returns_201(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = uuid4()
    target_user_id = uuid4()
    _patch_services(monkeypatch)
    app = _build_app(_mock_conn(), role=Role.ADMIN)
    async with await _client(app) as client:
        resp = await client.post(
            f"/jobs/{job_id}/assignees", json={"user_id": str(target_user_id)}
        )
    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_post_assignee_as_recruiter_with_real_user_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = uuid4()
    target_user_id = uuid4()
    _patch_services(monkeypatch)
    app = _build_app(_mock_conn(), role=Role.RECRUITER)
    async with await _client(app) as client:
        resp = await client.post(
            f"/jobs/{job_id}/assignees", json={"user_id": str(target_user_id)}
        )
    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_post_assignee_calls_assign_service_with_real_assigner_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = uuid4()
    target_user_id = uuid4()
    actor = _real_user(role="admin")
    assign, _unassign, _audit = _patch_services(monkeypatch)
    app = _build_app(_mock_conn(), role=Role.ADMIN)
    app.dependency_overrides[resolve_user] = lambda: actor
    async with await _client(app) as client:
        resp = await client.post(
            f"/jobs/{job_id}/assignees",
            json={"user_id": str(target_user_id), "note": "backfill"},
        )
    assert resp.status_code == 201
    assign.assert_awaited_once()
    args = assign.await_args.args
    kwargs = assign.await_args.kwargs
    assert job_id in args
    assert target_user_id in args
    assert kwargs["assigned_by"] == actor.id


@pytest.mark.asyncio
async def test_post_assignee_writes_assign_job_audit_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = uuid4()
    target_user_id = uuid4()
    actor = _real_user(role="recruiter")
    _assign, _unassign, audit = _patch_services(monkeypatch)
    app = _build_app(_mock_conn(), role=Role.RECRUITER)
    app.dependency_overrides[resolve_user] = lambda: actor
    async with await _client(app) as client:
        resp = await client.post(
            f"/jobs/{job_id}/assignees",
            json={"user_id": str(target_user_id), "note": "backfill"},
        )
    assert resp.status_code == 201
    audit.assert_awaited_once()
    kwargs = audit.await_args.kwargs
    assert kwargs["actor_kind"] == "user"
    assert kwargs["actor_user_id"] == actor.id
    assert kwargs.get("actor_service") is None
    assert kwargs["action"] == "assign_job"
    assert kwargs["subject_type"] == "job"
    assert kwargs["subject_id"] == job_id
    assert kwargs["job_id"] == job_id
    assert kwargs["details"] == {"user_id": str(target_user_id), "note": "backfill"}


@pytest.mark.asyncio
async def test_post_assignee_assign_runs_before_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The assign + audit write are meant to be atomic (ADR-020 §2, mirroring
    the blind_review-flip pattern) — proved fully only against a real
    Postgres (see the companion integration file), but the call ORDER is
    provable here: assign must run before the audit write records it."""
    job_id = uuid4()
    target_user_id = uuid4()
    order: list[str] = []

    from src.api.routes import job_assignees as job_assignees_routes

    async def _assign_side_effect(*_a: Any, **_kw: Any) -> None:
        order.append("assign")

    async def _audit_side_effect(*_a: Any, **_kw: Any) -> None:
        order.append("audit")

    monkeypatch.setattr(
        job_assignees_routes.job_assignee_service,
        "assign",
        AsyncMock(side_effect=_assign_side_effect),
    )
    monkeypatch.setattr(
        job_assignees_routes.audit_service,
        "record_audit",
        AsyncMock(side_effect=_audit_side_effect),
    )

    app = _build_app(_mock_conn(), role=Role.ADMIN)
    async with await _client(app) as client:
        resp = await client.post(
            f"/jobs/{job_id}/assignees", json={"user_id": str(target_user_id)}
        )
    assert resp.status_code == 201
    assert order == ["assign", "audit"]


# ── POST /jobs/{job_id}/assignees — ADR-020 §2 role gate ──────────────────


@pytest.mark.asyncio
async def test_post_assignee_403_for_auditor_never_assigns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Named negative test, ADR-020 §2 verbatim: "Auditor credentials may
    never assign; assignment is an operational act, not an audit act." No
    assign/audit call must happen either."""
    job_id = uuid4()
    target_user_id = uuid4()
    assign, _unassign, audit = _patch_services(monkeypatch)
    app = _build_app(_mock_conn(), role=Role.AUDITOR)
    async with await _client(app) as client:
        resp = await client.post(
            f"/jobs/{job_id}/assignees", json={"user_id": str(target_user_id)}
        )
    assert resp.status_code == 403
    assign.assert_not_awaited()
    audit.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_assignee_403_for_hiring_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = uuid4()
    target_user_id = uuid4()
    assign, _unassign, audit = _patch_services(monkeypatch)
    app = _build_app(_mock_conn(), role=Role.HIRING_MANAGER)
    async with await _client(app) as client:
        resp = await client.post(
            f"/jobs/{job_id}/assignees", json={"user_id": str(target_user_id)}
        )
    assert resp.status_code == 403
    assign.assert_not_awaited()
    audit.assert_not_awaited()


# ── POST /jobs/{job_id}/assignees — the real-assigner gate ────────────────


@pytest.mark.asyncio
async def test_post_assignee_403_when_no_identity_resolves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR-020's FK wrinkle: no session at all (``resolve_user`` -> ``None``,
    e.g. CAS enabled with no cookie) cannot be ``assigned_by`` — 403, and
    neither the assign nor the audit service is ever called."""
    job_id = uuid4()
    target_user_id = uuid4()
    assign, _unassign, audit = _patch_services(monkeypatch)
    app = _build_app(_mock_conn(), role=Role.ADMIN)
    app.dependency_overrides[resolve_user] = lambda: None
    async with await _client(app) as client:
        resp = await client.post(
            f"/jobs/{job_id}/assignees", json={"user_id": str(target_user_id)}
        )
    assert resp.status_code == 403
    assign.assert_not_awaited()
    audit.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_assignee_403_when_only_the_dev_admin_sentinel_resolves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CAS-disabled synthetic dev-admin identity is NOT a real ``users``
    row — using it as ``assigned_by`` would violate the FK. The route must
    403 rather than attempt the write."""
    job_id = uuid4()
    target_user_id = uuid4()
    assign, _unassign, audit = _patch_services(monkeypatch)
    app = _build_app(_mock_conn(), role=Role.ADMIN)
    app.dependency_overrides[resolve_user] = lambda: _sentinel_dev_admin_user()
    async with await _client(app) as client:
        resp = await client.post(
            f"/jobs/{job_id}/assignees", json={"user_id": str(target_user_id)}
        )
    assert resp.status_code == 403
    assign.assert_not_awaited()
    audit.assert_not_awaited()


# ── POST /jobs/{job_id}/assignees — malformed body ─────────────────────────


@pytest.mark.asyncio
async def test_post_assignee_422_on_missing_user_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = uuid4()
    _patch_services(monkeypatch)
    app = _build_app(_mock_conn(), role=Role.ADMIN)
    async with await _client(app) as client:
        resp = await client.post(f"/jobs/{job_id}/assignees", json={"note": "oops"})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_post_assignee_422_on_non_uuid_user_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = uuid4()
    _patch_services(monkeypatch)
    app = _build_app(_mock_conn(), role=Role.ADMIN)
    async with await _client(app) as client:
        resp = await client.post(
            f"/jobs/{job_id}/assignees", json={"user_id": "not-a-uuid"}
        )
    assert resp.status_code == 422


# ── DELETE /jobs/{job_id}/assignees/{user_id} — happy path ────────────────


@pytest.mark.asyncio
async def test_delete_assignee_as_admin_with_real_user_returns_204(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = uuid4()
    target_user_id = uuid4()
    _patch_services(monkeypatch, unassign_result=True)
    app = _build_app(_mock_conn(), role=Role.ADMIN)
    async with await _client(app) as client:
        resp = await client.delete(f"/jobs/{job_id}/assignees/{target_user_id}")
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_delete_assignee_as_recruiter_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = uuid4()
    target_user_id = uuid4()
    _patch_services(monkeypatch, unassign_result=True)
    app = _build_app(_mock_conn(), role=Role.RECRUITER)
    async with await _client(app) as client:
        resp = await client.delete(f"/jobs/{job_id}/assignees/{target_user_id}")
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_delete_assignee_returns_404_when_nothing_was_removed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Design call (tester's, see task report): ``unassign`` returning
    ``False`` (no matching row) surfaces as 404, distinct from a successful
    204 — mirroring the service layer's own true/false removal signal."""
    job_id = uuid4()
    target_user_id = uuid4()
    _patch_services(monkeypatch, unassign_result=False)
    app = _build_app(_mock_conn(), role=Role.ADMIN)
    async with await _client(app) as client:
        resp = await client.delete(f"/jobs/{job_id}/assignees/{target_user_id}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_assignee_writes_unassign_job_audit_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = uuid4()
    target_user_id = uuid4()
    actor = _real_user(role="admin")
    _assign, unassign, audit = _patch_services(monkeypatch, unassign_result=True)
    app = _build_app(_mock_conn(), role=Role.ADMIN)
    app.dependency_overrides[resolve_user] = lambda: actor
    async with await _client(app) as client:
        resp = await client.delete(f"/jobs/{job_id}/assignees/{target_user_id}")
    assert resp.status_code == 204
    unassign.assert_awaited_once()
    unassign_args = unassign.await_args.args
    assert job_id in unassign_args
    assert target_user_id in unassign_args
    audit.assert_awaited_once()
    kwargs = audit.await_args.kwargs
    assert kwargs["actor_kind"] == "user"
    assert kwargs["actor_user_id"] == actor.id
    assert kwargs["action"] == "unassign_job"
    assert kwargs["subject_type"] == "job"
    assert kwargs["subject_id"] == job_id
    assert kwargs["job_id"] == job_id


@pytest.mark.asyncio
async def test_delete_assignee_404_writes_no_audit_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing was actually unassigned — no ``unassign_job`` audit row for a
    no-op DELETE."""
    job_id = uuid4()
    target_user_id = uuid4()
    _assign, _unassign, audit = _patch_services(monkeypatch, unassign_result=False)
    app = _build_app(_mock_conn(), role=Role.ADMIN)
    async with await _client(app) as client:
        resp = await client.delete(f"/jobs/{job_id}/assignees/{target_user_id}")
    assert resp.status_code == 404
    audit.assert_not_awaited()


# ── DELETE /jobs/{job_id}/assignees/{user_id} — ADR-020 §2 role gate ──────


@pytest.mark.asyncio
async def test_delete_assignee_403_for_auditor_never_assigns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Named negative test, ADR-020 §2 verbatim: "Auditor credentials may
    never assign" — applies symmetrically to unassign."""
    job_id = uuid4()
    target_user_id = uuid4()
    _assign, unassign, audit = _patch_services(monkeypatch)
    app = _build_app(_mock_conn(), role=Role.AUDITOR)
    async with await _client(app) as client:
        resp = await client.delete(f"/jobs/{job_id}/assignees/{target_user_id}")
    assert resp.status_code == 403
    unassign.assert_not_awaited()
    audit.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_assignee_403_for_hiring_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = uuid4()
    target_user_id = uuid4()
    _assign, unassign, audit = _patch_services(monkeypatch)
    app = _build_app(_mock_conn(), role=Role.HIRING_MANAGER)
    async with await _client(app) as client:
        resp = await client.delete(f"/jobs/{job_id}/assignees/{target_user_id}")
    assert resp.status_code == 403
    unassign.assert_not_awaited()
    audit.assert_not_awaited()


# ── DELETE /jobs/{job_id}/assignees/{user_id} — the real-assigner gate ────


@pytest.mark.asyncio
async def test_delete_assignee_403_when_no_identity_resolves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = uuid4()
    target_user_id = uuid4()
    _assign, unassign, audit = _patch_services(monkeypatch)
    app = _build_app(_mock_conn(), role=Role.ADMIN)
    app.dependency_overrides[resolve_user] = lambda: None
    async with await _client(app) as client:
        resp = await client.delete(f"/jobs/{job_id}/assignees/{target_user_id}")
    assert resp.status_code == 403
    unassign.assert_not_awaited()
    audit.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_assignee_403_when_only_the_dev_admin_sentinel_resolves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = uuid4()
    target_user_id = uuid4()
    _assign, unassign, audit = _patch_services(monkeypatch)
    app = _build_app(_mock_conn(), role=Role.ADMIN)
    app.dependency_overrides[resolve_user] = lambda: _sentinel_dev_admin_user()
    async with await _client(app) as client:
        resp = await client.delete(f"/jobs/{job_id}/assignees/{target_user_id}")
    assert resp.status_code == 403
    unassign.assert_not_awaited()
    audit.assert_not_awaited()


# ── malformed path params ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_post_assignee_422_on_non_uuid_job_id_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_services(monkeypatch)
    app = _build_app(_mock_conn(), role=Role.ADMIN)
    async with await _client(app) as client:
        resp = await client.post(
            "/jobs/not-a-uuid/assignees", json={"user_id": str(uuid4())}
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_delete_assignee_422_on_non_uuid_user_id_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = uuid4()
    _patch_services(monkeypatch)
    app = _build_app(_mock_conn(), role=Role.ADMIN)
    async with await _client(app) as client:
        resp = await client.delete(f"/jobs/{job_id}/assignees/not-a-uuid")
    assert resp.status_code == 422


__all__: list[str] = []
