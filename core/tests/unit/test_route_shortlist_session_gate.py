"""RED pin — Phase 1.1 of the pilot-readiness plan (fix/session-role-on-writes).

See ``test_route_jobs_session_gate.py``'s module docstring for the full
defect description; this file pins the identical production combination
against the one write route on ``src.api.routes.shortlist``:

* ``POST /jobs/{job_id}/shortlist`` (generate, shortlist.py:43)
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

from src.api.deps import Role, get_arq, resolve_role, resolve_user
from src.api.routes import shortlist as shortlist_routes
from src.errors import AppError
from src.models.pool import get_db
from src.schemas.auth import User

_NOW = dt.datetime(2026, 8, 7, tzinfo=dt.UTC)

_NON_WRITER_SESSION_ROLES: tuple[str, ...] = ("hiring_manager", "auditor")


def _mock_conn() -> MagicMock:
    conn = MagicMock(name="conn")
    conn.fetchrow = AsyncMock(return_value=None)
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchval = AsyncMock(return_value=None)
    conn.execute = AsyncMock(return_value="UPDATE 1")
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


def _build_app(
    conn: MagicMock, *, arq: MagicMock | None = None, key_role: Role = Role.RECRUITER
) -> FastAPI:
    app = FastAPI()
    app.include_router(shortlist_routes.router)

    async def _get_db_override() -> AsyncIterator[Any]:
        yield conn

    app.dependency_overrides[get_db] = _get_db_override
    app.dependency_overrides[get_arq] = lambda: arq or MagicMock(
        enqueue_job=AsyncMock()
    )
    app.dependency_overrides[resolve_role] = lambda: key_role

    @app.exception_handler(AppError)
    async def _app_error_handler(_request: Any, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status, content={"detail": exc.message, "code": exc.code}
        )

    return app


async def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# ── POST /jobs/{job_id}/shortlist (shortlist.py:43) ──────────────────────


@pytest.mark.parametrize("session_role", _NON_WRITER_SESSION_ROLES)
@pytest.mark.asyncio
async def test_generate_shortlist_403s_for_recruiter_key_with_non_writer_session(
    session_role: str,
) -> None:
    job_id = uuid4()
    arq = MagicMock(enqueue_job=AsyncMock())
    app = _build_app(_mock_conn(), arq=arq, key_role=Role.RECRUITER)
    app.dependency_overrides[resolve_user] = lambda: _session_user(role=session_role)

    async with await _client(app) as client:
        resp = await client.post(f"/jobs/{job_id}/shortlist")

    assert resp.status_code == 403
    arq.enqueue_job.assert_not_awaited()


@pytest.mark.asyncio
async def test_generate_shortlist_recruiter_key_and_recruiter_session_passes() -> None:
    job_id = uuid4()
    app = _build_app(_mock_conn(), key_role=Role.RECRUITER)
    app.dependency_overrides[resolve_user] = lambda: _session_user(role="recruiter")

    async with await _client(app) as client:
        resp = await client.post(f"/jobs/{job_id}/shortlist")

    assert resp.status_code == 202


@pytest.mark.asyncio
async def test_generate_shortlist_recruiter_key_and_admin_session_passes() -> None:
    job_id = uuid4()
    app = _build_app(_mock_conn(), key_role=Role.RECRUITER)
    app.dependency_overrides[resolve_user] = lambda: _session_user(role="admin")

    async with await _client(app) as client:
        resp = await client.post(f"/jobs/{job_id}/shortlist")

    assert resp.status_code == 202


@pytest.mark.asyncio
async def test_generate_shortlist_403s_for_recruiter_key_with_no_session() -> None:
    """REVERSED (security finding, fix/auth-boundary-fails-open) — this test
    used to be docstringed "the case most likely to be broken by an
    over-eager fix" and asserted PASS for ``user is None`` (a bare
    service-key caller with NO CAS session at all): "must never be judged
    by this gate". That framing is now wrong — it protected exactly the
    vulnerability this slice closes. A live audit against real Postgres
    proved that with this deploy's real config (zero ``API_KEY_*``
    configured, so ``resolve_role`` trivially resolves ``Role.ADMIN`` for
    every caller) a cookie-less, key-less request could reach every write
    route with no credential whatsoever — proved end-to-end with a bare
    ``PATCH /jobs/{id} {"blind_review": false}`` that returned 200 and
    really flipped the column, audited to an actor nobody could trace to a
    person. The human decision: a valid API key is NOT sufficient on its
    own — every write route now requires a REAL, resolvable CAS session, so
    ``resolve_user`` -> ``None`` must 403 here too, the same as an
    out-of-allowed-set session role."""
    job_id = uuid4()
    arq = MagicMock(enqueue_job=AsyncMock())
    app = _build_app(_mock_conn(), arq=arq, key_role=Role.RECRUITER)
    app.dependency_overrides[resolve_user] = lambda: None

    async with await _client(app) as client:
        resp = await client.post(f"/jobs/{job_id}/shortlist")

    assert resp.status_code == 403
    arq.enqueue_job.assert_not_awaited()


@pytest.mark.asyncio
async def test_generate_shortlist_recruiter_key_and_ambient_dev_anonymous_sentinel_passes() -> (  # noqa: E501
    None
):
    """Ambient default (no ``resolve_user`` override): CAS disabled resolves
    the synthetic dev-anonymous admin sentinel — must PASS."""
    job_id = uuid4()
    app = _build_app(_mock_conn(), key_role=Role.RECRUITER)

    async with await _client(app) as client:
        resp = await client.post(f"/jobs/{job_id}/shortlist")

    assert resp.status_code == 202


__all__: list[str] = []
