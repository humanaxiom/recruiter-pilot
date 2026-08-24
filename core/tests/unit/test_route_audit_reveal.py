"""RED pin — D1 = option C: ``POST /audit/log/{audit_id}/reveal``, the
separately-audited reveal of a withheld ``audit_log.details`` value.

**What decision this implements.** ``audit_log.details`` is the only free-text
column in the table, and ``withdraw_resume``'s ``reason`` is operator-typed
prose about a named, identifiable candidate. ``redact_audit_details`` withholds
it (fail-closed allowlist), which leaves an auditor investigating a
wrongful-withdrawal complaint unable to do the job without an engineer running
SQL by hand — the exact unaudited-read problem ADR-036 closed elsewhere.

The product owner answered this on 2026-08-19 with **option C**: reveal on
request, each read separately audited. This file pins that shape, and the shape
is deliberately the one the codebase already defends for candidate PII
(``POST /resumes/{id}/reveal``): an explicit POST, gated on an attributable
human session, writing its audit row BEFORE the value is disclosed.

**What this file locks:**

* ``POST /audit/log/{audit_id}/reveal`` on the existing ``audit`` router.
* **Session-gated admin + auditor**, matching ``/audit/log``'s own gate and for
  the same two reasons (attributability, and browser reachability behind the
  BFF's one fixed ``recruiter`` key).
* **A keyed caller with no CAS session is refused** — the D2 = option B
  symmetry. The whole value of C over B is that the read is *attributable*; a
  reveal that could be performed by a bare service key would be worth strictly
  less than the status quo, because it would launder an unattributable read
  through a route that claims to record one.
* **Fail-closed on the action.** Only actions on an explicit revealable
  allowlist can be revealed. A row whose action is not on it is refused with no
  audit row and no disclosure — the same posture ``redact_audit_details`` takes,
  so a future writer inventing a new details key does not get a reveal path for
  free.
* **Audit-before-disclose ordering**, restating ADR-016/ADR-019 §7: the row is
  written and autocommitted before the value leaves the service, so a crash
  mid-response cannot produce an un-audited reveal.
* **A refused reveal writes NOTHING**, matching ``reveal_resume``'s discipline
  for a scope-blocked reveal — an audit trail that records reads that never
  happened is a trail that cannot be relied on.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

from src.api.deps import _DEV_ADMIN_SENTINEL_ID, Role, resolve_role, resolve_user
from src.api.routes import audit as audit_routes
from src.errors import AppError
from src.models.pool import get_db
from src.services.audit_service import redact_audit_details

_NOW = dt.datetime(2026, 8, 20, tzinfo=dt.UTC)
_REASON = "candidate asked to be removed after accepting another offer"

_AUDIT_READER_ROLES: tuple[Role, ...] = (Role.ADMIN, Role.AUDITOR)
_NON_AUDIT_READER_ROLES: tuple[Role, ...] = (Role.RECRUITER, Role.HIRING_MANAGER)

#: Distinguishes "caller did not pass details" from "caller passed None".
#: `None` is a real jsonb value this file must be able to exercise.
_UNSET = object()


class _Row(dict[str, Any]):
    def __getitem__(self, key: str) -> Any:
        return dict.get(self, key)


def _detail_row(
    *,
    audit_id: UUID,
    action: str = "withdraw_resume",
    details: Any = _UNSET,
) -> _Row:
    return _Row(
        {
            "id": audit_id,
            "action": action,
            "details": {"reason": _REASON} if details is _UNSET else details,
            "occurred_at": _NOW,
        }
    )


def _mock_conn(row: _Row | None) -> MagicMock:
    conn = MagicMock(name="conn")
    conn.fetchrow = AsyncMock(return_value=row)
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchval = AsyncMock(return_value=None)
    conn.execute = AsyncMock(return_value="INSERT 0 1")
    return conn


def _session_user(role: Role, *, user_id: UUID | None = None) -> Any:
    return MagicMock(
        id=user_id or uuid4(),
        role=str(role),
        active=True,
        cas_username="dev-anonymous" if user_id == _DEV_ADMIN_SENTINEL_ID else "priya",
    )


def _build_app(
    conn: MagicMock,
    *,
    role: Role = Role.ADMIN,
    session_role: Role | None = None,
    no_session: bool = False,
    user: Any = None,
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
        resolved = user or _session_user(session_role or role)
        app.dependency_overrides[resolve_user] = lambda: resolved

    @app.exception_handler(AppError)
    async def _app_error_handler(_request: Any, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status, content={"detail": exc.message, "code": exc.code}
        )

    return app


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _audit_writes(conn: MagicMock) -> list[Any]:
    """Every ``record_audit`` INSERT issued on this connection."""
    return [
        call
        for call in conn.execute.await_args_list
        if "INSERT INTO audit_log" in str(call.args[0])
    ]


@pytest.mark.parametrize("role", _AUDIT_READER_ROLES)
async def test_an_auditor_or_admin_gets_the_withheld_reason(role: Role) -> None:
    """The capability D1 = C exists to grant: the prose, on request."""
    audit_id = uuid4()
    conn = _mock_conn(_detail_row(audit_id=audit_id))
    async with _client(_build_app(conn, role=role)) as client:
        resp = await client.post(f"/audit/log/{audit_id}/reveal")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["action"] == "withdraw_resume"
    assert body["details"] == {"reason": _REASON}
    assert body["id"] == str(audit_id)


@pytest.mark.parametrize("role", _AUDIT_READER_ROLES)
async def test_the_reveal_is_itself_audited_and_attributable(role: Role) -> None:
    """C differs from B (blanket disclosure) in exactly one way: the read
    leaves a record naming who performed it. That record is the feature."""
    audit_id = uuid4()
    actor_id = uuid4()
    conn = _mock_conn(_detail_row(audit_id=audit_id))
    app = _build_app(conn, role=role, user=_session_user(role, user_id=actor_id))
    async with _client(app) as client:
        resp = await client.post(f"/audit/log/{audit_id}/reveal?context=complaint")
    assert resp.status_code == 200, resp.text

    writes = _audit_writes(conn)
    assert len(writes) == 1, f"expected exactly one audit row, got {len(writes)}"
    args = writes[0].args
    assert args[1] == "user", "the reveal was not attributed to a person"
    assert args[2] == actor_id
    assert args[3] is None, "a real human session must not be recorded as a service"
    assert args[4] == "reveal_audit_detail"
    assert args[5] == "audit_log"
    assert args[6] == audit_id
    assert args[8] == "complaint"


async def test_the_audit_row_names_which_action_was_revealed() -> None:
    """An auditor reading the trail of reveals must be able to tell WHAT was
    revealed without re-revealing it. The action is enum-shaped and non-PII, so
    it is recorded in ``details`` and disclosed by the allowlist."""
    audit_id = uuid4()
    conn = _mock_conn(_detail_row(audit_id=audit_id))
    async with _client(_build_app(conn)) as client:
        resp = await client.post(f"/audit/log/{audit_id}/reveal")
    assert resp.status_code == 200, resp.text

    details = json.loads(_audit_writes(conn)[0].args[9])
    assert details == {"revealed_action": "withdraw_resume"}
    assert redact_audit_details("reveal_audit_detail", details) == details, (
        "the reveal trail redacts its own non-PII marker, so an auditor reading "
        "the record of reveals cannot tell what was revealed"
    )


@pytest.mark.parametrize("role", _NON_AUDIT_READER_ROLES)
async def test_a_recruiter_or_hiring_manager_session_is_refused(role: Role) -> None:
    conn = _mock_conn(_detail_row(audit_id=uuid4()))
    async with _client(_build_app(conn, role=role)) as client:
        resp = await client.post(f"/audit/log/{uuid4()}/reveal")
    assert resp.status_code == 403, resp.text
    assert not conn.fetchrow.await_count, "the row was read before the gate refused"
    assert not _audit_writes(conn)


async def test_a_keyed_caller_with_no_cas_session_is_refused() -> None:
    """D2 = option B's symmetry, applied here. A reveal performed by a bare
    service key would record an unattributable read through a route whose only
    justification is that the read is attributable — strictly worse than
    withholding."""
    conn = _mock_conn(_detail_row(audit_id=uuid4()))
    app = _build_app(conn, role=Role.ADMIN, no_session=True)
    async with _client(app) as client:
        resp = await client.post(f"/audit/log/{uuid4()}/reveal")
    assert resp.status_code == 403, resp.text
    assert not conn.fetchrow.await_count
    assert not _audit_writes(conn)


@pytest.mark.parametrize("session_role", _AUDIT_READER_ROLES)
async def test_the_bff_recruiter_key_with_a_reader_session_is_allowed(
    session_role: Role,
) -> None:
    """The reachability pin. The Flask BFF presents ONE fixed ``recruiter`` key
    for every browser (FU-4/D6); a keyed role gate here would make the button
    unclickable for exactly the person it was built for, while every unit test
    that set the KEY role to admin passed. ``/audit/log`` learned this already."""
    audit_id = uuid4()
    conn = _mock_conn(_detail_row(audit_id=audit_id))
    app = _build_app(conn, role=Role.RECRUITER, session_role=session_role)
    async with _client(app) as client:
        resp = await client.post(f"/audit/log/{audit_id}/reveal")
    assert resp.status_code == 200, (
        "the BFF's fixed recruiter key blocked a real auditor session — this "
        "route is unreachable from the browser"
    )


@pytest.mark.parametrize("action", ["role_changed", "reveal", "assign_job"])
async def test_an_action_off_the_revealable_allowlist_is_refused(action: str) -> None:
    """Fail-closed, mirroring ``redact_audit_details``. The decision the owner
    answered was about withdrawal reasons specifically; this route must not
    become a general un-redactor that a future ``details`` writer inherits."""
    audit_id = uuid4()
    conn = _mock_conn(_detail_row(audit_id=audit_id, action=action, details={"x": "y"}))
    async with _client(_build_app(conn)) as client:
        resp = await client.post(f"/audit/log/{audit_id}/reveal")
    assert resp.status_code == 403, resp.text
    assert "y" not in resp.text, "the refused route disclosed the value anyway"
    assert not _audit_writes(conn), "a refused reveal wrote an audit row"


async def test_a_nonexistent_audit_row_is_a_404_and_writes_nothing() -> None:
    conn = _mock_conn(None)
    async with _client(_build_app(conn)) as client:
        resp = await client.post(f"/audit/log/{uuid4()}/reveal")
    assert resp.status_code == 404, resp.text
    assert not _audit_writes(conn)


@pytest.mark.parametrize("details", [None, "a legacy scalar", ["a", "list"], {}])
async def test_a_row_with_no_revealable_object_is_refused(details: Any) -> None:
    """``details`` is ``jsonb``: a legacy or hand-written row may hold a scalar,
    a list, or nothing. There is no withheld value to reveal in any of those
    cases, so the route must refuse rather than hand back whatever it found —
    and must not manufacture an audit row claiming a reveal happened."""
    audit_id = uuid4()
    conn = _mock_conn(_detail_row(audit_id=audit_id, details=details))
    async with _client(_build_app(conn)) as client:
        resp = await client.post(f"/audit/log/{audit_id}/reveal")
    assert resp.status_code in (403, 404), resp.text
    assert not _audit_writes(conn)


async def test_the_dev_anonymous_sentinel_is_recorded_as_a_service() -> None:
    """ADR-019 §10b — the synthetic CAS-disabled identity is not a ``users``
    row, so recording it as ``actor_kind='user'`` would violate
    ``audit_log.actor_user_id``'s FK at the database, not in Python."""
    audit_id = uuid4()
    conn = _mock_conn(_detail_row(audit_id=audit_id))
    app = _build_app(
        conn, user=_session_user(Role.ADMIN, user_id=_DEV_ADMIN_SENTINEL_ID)
    )
    async with _client(app) as client:
        resp = await client.post(f"/audit/log/{audit_id}/reveal")
    assert resp.status_code == 200, resp.text
    args = _audit_writes(conn)[0].args
    assert args[1] == "service"
    assert args[2] is None
    assert args[3] == "dev-anonymous"


async def test_the_audit_row_is_written_before_the_value_is_returned() -> None:
    """ADR-016 / ADR-019 §7's ordering guarantee, restated for this route: a
    crash between the two steps must leave a record of an attempted reveal, not
    a disclosure with no record."""
    audit_id = uuid4()
    order: list[str] = []

    conn = MagicMock(name="conn")

    async def _fetchrow(*_a: Any, **_k: Any) -> _Row:
        order.append("read")
        return _detail_row(audit_id=audit_id)

    async def _execute(*a: Any, **_k: Any) -> str:
        if "INSERT INTO audit_log" in str(a[0]):
            order.append("audit")
        return "INSERT 0 1"

    conn.fetchrow = AsyncMock(side_effect=_fetchrow)
    conn.execute = AsyncMock(side_effect=_execute)
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchval = AsyncMock(return_value=None)

    async with _client(_build_app(conn)) as client:
        resp = await client.post(f"/audit/log/{audit_id}/reveal")
    assert resp.status_code == 200, resp.text
    assert order == ["read", "audit"], order
