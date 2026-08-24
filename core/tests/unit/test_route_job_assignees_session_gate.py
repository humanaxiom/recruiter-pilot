"""RED pin — Phase 1.1 of the pilot-readiness plan (fix/session-role-on-writes).

See ``test_route_jobs_session_gate.py``'s module docstring for the full
defect description; this file pins the identical production combination
against both write routes on ``src.api.routes.job_assignees``:

* ``POST /jobs/{job_id}/assignees`` (job_assignees.py:72)
* ``DELETE /jobs/{job_id}/assignees/{user_id}`` (job_assignees.py:108)

**Why this router is not tested with a ``resolve_user -> None`` positive
case (unlike its siblings).** Both routes already carry a PRE-EXISTING,
correct gate — ``_require_real_assigner`` (job_assignees.py:56-69) — that
403s whenever ``user is None`` or ``user.id == _DEV_ADMIN_SENTINEL_ID``,
for an orthogonal reason (neither is a legal ``assigned_by``/``actor_user_id``
FK target). A REAL hiring_manager/auditor session is NOT caught by that
gate (it is a real, non-sentinel user) — this file pins that this router's
GENUINE gap is exactly that a real hiring_manager/auditor session sails
through ``_require_real_assigner`` untouched today, because nothing on this
router has ever consulted the session's ROLE.
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

from src.api.deps import Role, resolve_role, resolve_user
from src.errors import AppError
from src.models.pool import get_db
from src.schemas.auth import User

_NOW = dt.datetime(2026, 8, 7, tzinfo=dt.UTC)

_NON_WRITER_SESSION_ROLES: tuple[str, ...] = ("hiring_manager", "auditor")


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


def _session_user(*, cas_username: str = "priya", role: str = "hiring_manager") -> User:
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


def _build_app(conn: MagicMock, *, key_role: Role = Role.RECRUITER) -> FastAPI:
    from src.api.routes import job_assignees as job_assignees_routes

    app = FastAPI()
    app.include_router(job_assignees_routes.router)

    async def _get_db_override() -> AsyncIterator[Any]:
        yield conn

    app.dependency_overrides[get_db] = _get_db_override
    app.dependency_overrides[resolve_role] = lambda: key_role

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


# ── POST /jobs/{job_id}/assignees (job_assignees.py:72) ──────────────────


@pytest.mark.parametrize("session_role", _NON_WRITER_SESSION_ROLES)
@pytest.mark.asyncio
async def test_create_assignee_403s_for_recruiter_key_with_non_writer_session(
    session_role: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_id = uuid4()
    target_user_id = uuid4()
    assign, _unassign, audit = _patch_services(monkeypatch)
    app = _build_app(_mock_conn(), key_role=Role.RECRUITER)
    app.dependency_overrides[resolve_user] = lambda: _session_user(role=session_role)

    async with await _client(app) as client:
        resp = await client.post(
            f"/jobs/{job_id}/assignees", json={"user_id": str(target_user_id)}
        )

    assert resp.status_code == 403
    assign.assert_not_awaited()
    audit.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_assignee_recruiter_key_and_recruiter_session_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = uuid4()
    target_user_id = uuid4()
    _patch_services(monkeypatch)
    app = _build_app(_mock_conn(), key_role=Role.RECRUITER)
    app.dependency_overrides[resolve_user] = lambda: _session_user(role="recruiter")

    async with await _client(app) as client:
        resp = await client.post(
            f"/jobs/{job_id}/assignees", json={"user_id": str(target_user_id)}
        )

    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_create_assignee_admin_key_and_admin_session_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = uuid4()
    target_user_id = uuid4()
    _patch_services(monkeypatch)
    app = _build_app(_mock_conn(), key_role=Role.ADMIN)
    app.dependency_overrides[resolve_user] = lambda: _session_user(role="admin")

    async with await _client(app) as client:
        resp = await client.post(
            f"/jobs/{job_id}/assignees", json={"user_id": str(target_user_id)}
        )

    assert resp.status_code == 201


# ── DELETE /jobs/{job_id}/assignees/{user_id} (job_assignees.py:108) ─────


@pytest.mark.parametrize("session_role", _NON_WRITER_SESSION_ROLES)
@pytest.mark.asyncio
async def test_delete_assignee_403s_for_recruiter_key_with_non_writer_session(
    session_role: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_id = uuid4()
    target_user_id = uuid4()
    _assign, unassign, audit = _patch_services(monkeypatch)
    app = _build_app(_mock_conn(), key_role=Role.RECRUITER)
    app.dependency_overrides[resolve_user] = lambda: _session_user(role=session_role)

    async with await _client(app) as client:
        resp = await client.delete(f"/jobs/{job_id}/assignees/{target_user_id}")

    assert resp.status_code == 403
    unassign.assert_not_awaited()
    audit.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_assignee_recruiter_key_and_recruiter_session_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = uuid4()
    target_user_id = uuid4()
    _patch_services(monkeypatch)
    app = _build_app(_mock_conn(), key_role=Role.RECRUITER)
    app.dependency_overrides[resolve_user] = lambda: _session_user(role="recruiter")

    async with await _client(app) as client:
        resp = await client.delete(f"/jobs/{job_id}/assignees/{target_user_id}")

    assert resp.status_code == 204


__all__: list[str] = []
