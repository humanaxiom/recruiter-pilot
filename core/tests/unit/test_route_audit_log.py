"""RED pin — Phase 1.4 slice 1: ``GET /audit/log``, the auditor's read of the
LIVE ``audit_log`` table.

Distinct from ``test_route_audit.py``, which covers ``GET
/audit/reveals-legacy`` — the read of the FROZEN ``reveal_audit`` table. That
route returns only pre-FU-5-slice-8 history; every audit event since the
cutover lives in ``audit_log``, which **nothing reads**. This route is that
read.

**What this file locks:**

* ``GET /audit/log`` on the existing ``src.api.routes.audit`` router, absolute
  path, no router-level prefix — same convention as its sibling.
* **Role gate: admin + auditor**, matching ``_AUDIT_READERS``. Recruiter and
  hiring_manager get 403. This is the auditor's second capability and the first
  one that shows them anything current.
* **A REAL CAS SESSION IS REQUIRED**, not merely a valid API key — the route
  carries ``require_session_role(*_AUDIT_READERS)`` in addition to the keyed
  ``require_role``. This is deliberately STRICTER than its
  ``/audit/reveals-legacy`` sibling, and is not an answer to ADR-034's carried
  question about machine readers in general: it is a judgement about this
  surface specifically. The audit log is the most sensitive read in the
  application — it names who looked at whom, and it is the record an auditor
  would rely on to detect misuse. Reading it should itself be attributable to a
  person, and a shared service key is by construction not a person. ADR-036
  records this rather than leaving it to be inferred from the code.
* ``limit``/``offset`` follow the established ``Query`` convention
  (``ge=1, le=500`` / ``ge=0``); out-of-bounds is a 422 from FastAPI's own
  validation, never clamped.
* Optional ``action``/``subject_type``/``job_id`` filters.
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

from src.api.deps import Role, resolve_role, resolve_user
from src.api.routes import audit as audit_routes
from src.errors import AppError
from src.models.pool import get_db

_NOW = dt.datetime(2026, 8, 13, tzinfo=dt.UTC)

_NON_AUDIT_READER_ROLES: tuple[Role, ...] = (Role.RECRUITER, Role.HIRING_MANAGER)
_AUDIT_READER_ROLES: tuple[Role, ...] = (Role.ADMIN, Role.AUDITOR)


class _Row(dict[str, Any]):
    def __getitem__(self, key: str) -> Any:
        return dict.get(self, key)


def _audit_row(
    *,
    action: str = "reveal",
    actor_kind: str = "user",
    actor_username: str | None = "priya",
    actor_service: str | None = None,
    details: Any = None,
) -> _Row:
    return _Row(
        {
            "id": uuid4(),
            "actor_kind": actor_kind,
            "actor_user_id": uuid4() if actor_kind == "user" else None,
            "actor_username": actor_username,
            "actor_service": actor_service,
            "action": action,
            "subject_type": "resume",
            "subject_id": uuid4(),
            "job_id": uuid4(),
            "context": "reviewing shortlist",
            "details": details,
            "occurred_at": _NOW,
        }
    )


def _mock_conn(*, fetch: list[_Row] | None = None) -> MagicMock:
    conn = MagicMock(name="conn")
    conn.fetch = AsyncMock(return_value=fetch or [])
    conn.fetchrow = AsyncMock(return_value=None)
    conn.fetchval = AsyncMock(return_value=len(fetch or []))
    conn.execute = AsyncMock(return_value="SELECT 1")
    return conn


def _session_user(role: Role) -> Any:
    """A resolved, active CAS session with ``role``."""
    return MagicMock(id=uuid4(), role=str(role), active=True, cas_username="someone")


def _build_app(
    conn: MagicMock,
    *,
    role: Role = Role.ADMIN,
    session_role: Role | None = None,
    no_session: bool = False,
) -> FastAPI:
    app = FastAPI()
    app.include_router(audit_routes.router)

    async def _get_db_override() -> AsyncIterator[Any]:
        yield conn

    app.dependency_overrides[get_db] = _get_db_override
    app.dependency_overrides[resolve_role] = lambda: role
    if no_session:
        app.dependency_overrides[resolve_user] = lambda: None
    else:
        user = _session_user(session_role or role)
        app.dependency_overrides[resolve_user] = lambda: user

    @app.exception_handler(AppError)
    async def _app_error_handler(_request: Any, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status, content={"detail": exc.message, "code": exc.code}
        )

    return app


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.parametrize("role", _AUDIT_READER_ROLES)
async def test_admin_and_auditor_can_read_the_live_audit_log(role: Role) -> None:
    conn = _mock_conn(fetch=[_audit_row()])
    async with _client(_build_app(conn, role=role)) as client:
        resp = await client.get("/audit/log")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body) == 1
    assert body[0]["action"] == "reveal"
    assert body[0]["actor_username"] == "priya"


@pytest.mark.parametrize("role", _NON_AUDIT_READER_ROLES)
async def test_recruiter_and_hiring_manager_are_refused(role: Role) -> None:
    conn = _mock_conn(fetch=[_audit_row()])
    async with _client(_build_app(conn, role=role)) as client:
        resp = await client.get("/audit/log")
    assert resp.status_code == 403, resp.text
    assert not conn.fetch.await_count, "the query ran before the role gate refused"


async def test_a_keyed_caller_with_no_cas_session_is_refused() -> None:
    """The audit log names who looked at whom. Reading it must itself be
    attributable to a person, and a shared service key is not a person.

    Stricter than ``/audit/reveals-legacy`` on purpose — see the module
    docstring and ADR-036. This is NOT an answer to ADR-034's carried question
    about machine readers generally.
    """
    conn = _mock_conn(fetch=[_audit_row()])
    async with _client(_build_app(conn, role=Role.ADMIN, no_session=True)) as client:
        resp = await client.get("/audit/log")
    assert resp.status_code == 403, resp.text
    assert not conn.fetch.await_count


@pytest.mark.parametrize("session_role", _AUDIT_READER_ROLES)
async def test_the_bff_recruiter_key_with_a_reader_session_is_allowed(
    session_role: Role,
) -> None:
    """THE reachability pin, and the one that nearly shipped broken.

    The Flask BFF presents ONE fixed ``recruiter`` key for every browser it
    serves (FU-4/D6) while forwarding the real user's session cookie. A keyed
    ``require_role(ADMIN, AUDITOR)`` gate on this route would therefore 403
    every real auditor at the only door they will ever use — the page would be
    unreachable for exactly the person it was built for, while every unit test
    that set the KEY role to admin passed.

    So this route gates on the SESSION role alone, matching
    ``users.py::_require_admin_session``, and this test pins the combination the
    browser actually produces: recruiter key, auditor (or admin) session.
    """
    conn = _mock_conn(fetch=[_audit_row()])
    app = _build_app(conn, role=Role.RECRUITER, session_role=session_role)
    async with _client(app) as client:
        resp = await client.get("/audit/log")
    assert resp.status_code == 200, (
        "the BFF's fixed recruiter key blocked a real auditor session — this "
        "route is unreachable from the browser"
    )


async def test_a_valid_key_with_a_non_reader_session_role_is_refused() -> None:
    """The intersection, not the union: an admin KEY carried by a recruiter's
    real session does not open the audit log."""
    conn = _mock_conn(fetch=[_audit_row()])
    app = _build_app(conn, role=Role.ADMIN, session_role=Role.RECRUITER)
    async with _client(app) as client:
        resp = await client.get("/audit/log")
    assert resp.status_code == 403, resp.text


async def test_a_service_actor_row_serialises_without_a_username() -> None:
    """The unattributable events are the ones an auditor most needs; they must
    render, not error, with a null username."""
    conn = _mock_conn(
        fetch=[
            _audit_row(
                actor_kind="service",
                actor_username=None,
                actor_service="api",
                action="blind_review_changed",
            )
        ]
    )
    async with _client(_build_app(conn)) as client:
        resp = await client.get("/audit/log")
    assert resp.status_code == 200, resp.text
    assert resp.json()[0]["actor_service"] == "api"
    assert resp.json()[0]["actor_username"] is None


async def test_the_withdrawal_reason_is_withheld_in_the_response() -> None:
    """The disclosure boundary holds at the ROUTE, not just in the service."""
    secret = "Jane Q Candidate asked us to delete her file"
    conn = _mock_conn(
        fetch=[_audit_row(action="withdraw_resume", details={"reason": secret})]
    )
    async with _client(_build_app(conn)) as client:
        resp = await client.get("/audit/log")
    assert resp.status_code == 200, resp.text
    assert secret not in resp.text
    assert "Jane" not in resp.text


@pytest.mark.parametrize(
    ("query", "expected"),
    [("limit=0", 422), ("limit=501", 422), ("offset=-1", 422), ("limit=500", 200)],
)
async def test_pagination_bounds_are_validated_not_clamped(
    query: str, expected: int
) -> None:
    conn = _mock_conn(fetch=[])
    async with _client(_build_app(conn)) as client:
        resp = await client.get(f"/audit/log?{query}")
    assert resp.status_code == expected, resp.text


async def test_filters_are_forwarded_to_the_query() -> None:
    conn = _mock_conn(fetch=[])
    job_id: UUID = uuid4()
    async with _client(_build_app(conn)) as client:
        resp = await client.get(
            f"/audit/log?action=reveal&subject_type=resume&job_id={job_id}"
        )
    assert resp.status_code == 200, resp.text
    assert conn.fetch.await_count == 1
    forwarded = conn.fetch.await_args[0]
    assert "reveal" in forwarded
    assert "resume" in forwarded
    assert job_id in forwarded
