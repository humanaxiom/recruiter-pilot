"""Route-level tests for FU-5 slice 10 (ADR-019 §6 / §9.4) —
``GET /audit/reveals-legacy``, a read-only paginated view of the FROZEN
``reveal_audit`` table.

``src.api.routes.audit`` does not exist yet — the whole file fails at
collection (``ModuleNotFoundError``). RED half of the TDD cycle.

**What this file locks (the coder's implementation MUST match):**

* ``src.api.routes.audit`` exposes an ``APIRouter`` named ``router`` with an
  ABSOLUTE path ``GET /audit/reveals-legacy`` (no router-level ``prefix``),
  mirroring every other Phase-6/FU-5 route module. ``src.api.main`` mounts it
  with ``app.include_router(audit.router)``.
* **Role gate — admin AND auditor** (ADR-019 §9.4: "Four points this ADR left
  silent... `GET /audit/reveals-legacy` is admin + auditor... this becomes
  the auditor's first real capability"). Recruiter and hiring_manager are
  REJECTED with 403. This is the auditor role's first endpoint anywhere in
  the codebase — do not default to admin-only.
* ``limit``/``offset`` are validated ``Query`` params, following the
  established convention in ``src.api.routes.resumes.list_resumes``
  (``limit: int = Query(default=100, ge=1, le=500)``,
  ``offset: int = Query(default=0, ge=0)``) and
  ``src.api.routes.jobs.list_jobs`` — an out-of-bounds value is a 422 from
  FastAPI's own validation, not clamped or silently accepted.
* The route reads via the injected DB connection (mocked here at the
  ``conn.fetch``/``get_db`` boundary, exactly as ``test_route_jobs.py`` and
  ``test_route_resumes.py`` do) — no assertion here couples to a specific
  service module name (``reveal_service`` vs. a new module is the coder's
  call, per the tester's report).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

from src.api.deps import Role, resolve_role
from src.api.routes import audit as audit_routes
from src.errors import AppError
from src.models.pool import get_db

_NOW = dt.datetime(2026, 7, 22, tzinfo=dt.UTC)

_NON_AUDIT_READER_ROLES: tuple[Role, ...] = (Role.RECRUITER, Role.HIRING_MANAGER)
_AUDIT_READER_ROLES: tuple[Role, ...] = (Role.ADMIN, Role.AUDITOR)


class _Row(dict[str, Any]):
    def __getitem__(self, key: str) -> Any:
        return dict.get(self, key)


def _reveal_audit_row(
    *,
    row_id: UUID | None = None,
    resume_id: UUID | None = None,
    job_id: UUID | None = None,
    actor: str | None = "priya",
    context: str | None = "reviewing shortlist",
    revealed_at: dt.datetime = _NOW,
) -> _Row:
    return _Row(
        {
            "id": row_id or uuid4(),
            "resume_id": resume_id or uuid4(),
            "job_id": job_id or uuid4(),
            "actor": actor,
            "context": context,
            "revealed_at": revealed_at,
        }
    )


def _mock_conn(*, fetch: list[_Row] | None = None) -> MagicMock:
    conn = MagicMock(name="conn")
    conn.fetch = AsyncMock(return_value=fetch or [])
    conn.fetchrow = AsyncMock(return_value=None)
    conn.fetchval = AsyncMock(return_value=len(fetch or []))
    conn.execute = AsyncMock(return_value="SELECT 1")
    return conn


def _build_app(conn: MagicMock, *, role: Role = Role.ADMIN) -> FastAPI:
    app = FastAPI()
    app.include_router(audit_routes.router)

    async def _get_db_override() -> AsyncIterator[Any]:
        yield conn

    app.dependency_overrides[get_db] = _get_db_override
    app.dependency_overrides[resolve_role] = lambda: role

    @app.exception_handler(AppError)
    async def _app_error_handler(_request: Any, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status, content={"detail": exc.message, "code": exc.code}
        )

    return app


async def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# ── Role gate: admin + auditor only ─────────────────────────────────────


@pytest.mark.asyncio
async def test_admin_can_list_reveals_legacy() -> None:
    conn = _mock_conn(fetch=[_reveal_audit_row()])
    app = _build_app(conn, role=Role.ADMIN)
    async with await _client(app) as client:
        resp = await client.get("/audit/reveals-legacy")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_auditor_can_list_reveals_legacy() -> None:
    """ADR-019 §9.4 — the auditor role's first real capability."""
    conn = _mock_conn(fetch=[_reveal_audit_row()])
    app = _build_app(conn, role=Role.AUDITOR)
    async with await _client(app) as client:
        resp = await client.get("/audit/reveals-legacy")
    assert resp.status_code == 200


@pytest.mark.parametrize("role", _NON_AUDIT_READER_ROLES)
@pytest.mark.asyncio
async def test_recruiter_and_hiring_manager_403(role: Role) -> None:
    conn = _mock_conn(fetch=[_reveal_audit_row()])
    app = _build_app(conn, role=role)
    async with await _client(app) as client:
        resp = await client.get("/audit/reveals-legacy")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_unauthenticated_or_unresolvable_role_is_not_admitted_by_default() -> (
    None
):
    """A role outside the allowed set (belt-and-suspenders on the parametrized
    403 tests above) — every role except admin/auditor must be rejected, not
    just the two spot-checked."""
    conn = _mock_conn(fetch=[])
    for role in Role:
        app = _build_app(conn, role=role)
        async with await _client(app) as client:
            resp = await client.get("/audit/reveals-legacy")
        if role in _AUDIT_READER_ROLES:
            assert resp.status_code == 200, role
        else:
            assert resp.status_code == 403, role


# ── Response shape — bare list of reveal_audit's own columns ──────────────


@pytest.mark.asyncio
async def test_response_is_a_list_of_reveal_audit_rows() -> None:
    resume_id = uuid4()
    job_id = uuid4()
    row = _reveal_audit_row(
        resume_id=resume_id,
        job_id=job_id,
        actor="morgan",
        context="second look",
    )
    conn = _mock_conn(fetch=[row])
    app = _build_app(conn, role=Role.ADMIN)
    async with await _client(app) as client:
        resp = await client.get("/audit/reveals-legacy")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    assert len(body) == 1
    item = body[0]
    assert item["resume_id"] == str(resume_id)
    assert item["job_id"] == str(job_id)
    assert item["actor"] == "morgan"
    assert item["context"] == "second look"
    assert "revealed_at" in item
    # No candidate PII column — reveal_audit itself carries none, and the
    # legacy endpoint must never join against resumes to add any.
    assert "candidate_name" not in item
    assert "candidate_email" not in item


@pytest.mark.asyncio
async def test_empty_reveal_audit_returns_empty_list() -> None:
    conn = _mock_conn(fetch=[])
    app = _build_app(conn, role=Role.AUDITOR)
    async with await _client(app) as client:
        resp = await client.get("/audit/reveals-legacy")
    assert resp.status_code == 200
    assert resp.json() == []


# ── limit/offset validation — 422, matching the resumes/jobs convention ──


@pytest.mark.parametrize("bad_limit", [0, -1, 501, 10_000])
@pytest.mark.asyncio
async def test_limit_out_of_bounds_is_422(bad_limit: int) -> None:
    conn = _mock_conn(fetch=[])
    app = _build_app(conn, role=Role.ADMIN)
    async with await _client(app) as client:
        resp = await client.get("/audit/reveals-legacy", params={"limit": bad_limit})
    assert resp.status_code == 422


@pytest.mark.parametrize("bad_offset", [-1, -100])
@pytest.mark.asyncio
async def test_offset_negative_is_422(bad_offset: int) -> None:
    conn = _mock_conn(fetch=[])
    app = _build_app(conn, role=Role.ADMIN)
    async with await _client(app) as client:
        resp = await client.get("/audit/reveals-legacy", params={"offset": bad_offset})
    assert resp.status_code == 422


@pytest.mark.parametrize("limit,offset", [(1, 0), (100, 0), (500, 1000)])
@pytest.mark.asyncio
async def test_in_bounds_limit_and_offset_are_accepted(limit: int, offset: int) -> None:
    conn = _mock_conn(fetch=[])
    app = _build_app(conn, role=Role.ADMIN)
    async with await _client(app) as client:
        resp = await client.get(
            "/audit/reveals-legacy", params={"limit": limit, "offset": offset}
        )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_default_limit_and_offset_used_when_omitted() -> None:
    """No query params at all must still 200 (defaults apply), not 422."""
    conn = _mock_conn(fetch=[])
    app = _build_app(conn, role=Role.ADMIN)
    async with await _client(app) as client:
        resp = await client.get("/audit/reveals-legacy")
    assert resp.status_code == 200
