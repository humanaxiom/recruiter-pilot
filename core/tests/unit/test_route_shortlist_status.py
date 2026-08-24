"""Route-level tests for FU-7 §2's new status route — ``GET
/jobs/{job_id}/shortlist/status`` (ADR-021 §2 / ADR-029).

``src.api.routes.shortlist`` exists already (this is an ADDITIVE route on an
existing module), but the route itself, ``shortlist_service.get_shortlist_state``,
and the ``ShortlistStateOut`` DTO do not exist yet — every test below either
404s against FastAPI's own unmatched-route handler (the route isn't
registered yet) or fails at ``monkeypatch.setattr`` with an ``AttributeError``
(the service function doesn't exist). RED half of the TDD cycle.

**Tester's resolution of a spec ambiguity** (flagged per the standing
contract — no ADR authorizes this, it is a documentation gap in the
approved spec, not a contradicted existing test): the spec's service-layer
docstring says ``get_shortlist_state`` returns ``None`` for BOTH "no such
job" and "job exists but state is NULL" — but the route must return 404 for
a missing job and 200 for "job exists, no state", which are observationally
different outcomes. This file pins the more implementable contract:
``get_shortlist_state`` raises ``NotFoundError`` when the job does not
exist (mirroring every other single-entity read in this codebase —
``shortlist_service.get_one`` / ``job_service.get_job`` — see
``test_get_shortlist_entry_404_when_missing`` in ``test_route_shortlist.py``
for the established precedent) and returns ``None`` ONLY when the job
exists but carries no ``awaiting_llm`` state. If the coder's read of the
spec differs, this decision should be revisited together, not silently
overridden.

**Response shape**: ``{"job_id": <uuid>, "state": "awaiting_llm" | null,
"reason": str | null, "at": iso-datetime | null}``.

**RBAC**: same readers as ``list_shortlist`` (all four roles; the spec
explicitly says "reuse its dependency") — mirrors
``test_list_shortlist_is_readable_by_every_role`` in ``test_route_shortlist.py``,
including the ADR-020 §3 fail-closed hiring_manager-needs-a-real-session fix.

**IDOR fix (security + reviewer finding, post-GREEN follow-up)**: the route
already resolves a scoped ``user_id`` via ``scoped_user_id_or_403`` but
discards it — ``get_shortlist_state`` is called with NO ``user_id``, unlike
its sibling ``list_shortlist`` (which forwards ``user_id`` to
``list_for_job``). An unassigned ``hiring_manager`` could therefore read
ANOTHER job's ranking state, and the 404-vs-200 split doubled as a
job-existence oracle. The fix mirrors ``job_service.get_job``'s
single-entity precedent (``job_service.py:402``,
``_JOB_ASSIGNEE_EXISTS_SQL``): the route must forward the resolved
``user_id`` into ``get_shortlist_state(db, job_id, user_id=...)``, exactly
like ``list_shortlist`` does today — the "route forwards user_id" tests
below pin THAT wiring; the real Postgres row-scoping proof (an unassigned
hiring_manager's read actually collapses to 0 rows / ``NotFoundError``) is
``test_shortlist_fail_closed_pg.py``'s job, mandatory, not optional.
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
from src.errors import AppError, NotFoundError
from src.models.pool import get_db
from src.schemas.auth import User

_NOW = dt.datetime(2026, 8, 1, tzinfo=dt.UTC)

_ALL_ROLES: tuple[Role, ...] = tuple(Role)
_UNSCOPED_KEY_ROLES: tuple[tuple[Role, str], ...] = (
    (Role.ADMIN, "admin"),
    (Role.RECRUITER, "recruiter"),
    (Role.AUDITOR, "auditor"),
)


def _acm() -> MagicMock:
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=None)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _mock_conn() -> MagicMock:
    conn = MagicMock(name="conn")
    conn.fetchrow = AsyncMock(return_value=None)
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchval = AsyncMock(return_value=None)
    conn.execute = AsyncMock(return_value="UPDATE 1")
    conn.transaction = MagicMock(return_value=_acm())
    return conn


def _build_app(conn: MagicMock, *, role: Role = Role.ADMIN) -> FastAPI:
    app = FastAPI()
    app.include_router(shortlist_routes.router)

    async def _get_db_override() -> AsyncIterator[Any]:
        yield conn

    app.dependency_overrides[get_db] = _get_db_override
    app.dependency_overrides[get_arq] = lambda: MagicMock(enqueue_job=AsyncMock())
    app.dependency_overrides[resolve_role] = lambda: role

    @app.exception_handler(AppError)
    async def _app_error_handler(_request: Any, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status, content={"detail": exc.message, "code": exc.code}
        )

    return app


async def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _identity_user(*, cas_username: str = "alice", role: str = "recruiter") -> User:
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


# ── shape: no state set ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_status_returns_null_state_when_job_has_no_awaiting_llm_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = uuid4()
    conn = _mock_conn()
    app = _build_app(conn)
    get_state = AsyncMock(return_value=None)
    monkeypatch.setattr(
        shortlist_routes.shortlist_service, "get_shortlist_state", get_state
    )

    async with await _client(app) as client:
        resp = await client.get(f"/jobs/{job_id}/shortlist/status")

    assert resp.status_code == 200
    body = resp.json()
    assert body["job_id"] == str(job_id)
    assert body["state"] is None
    assert body["reason"] is None
    assert body["at"] is None


# ── shape: awaiting_llm set ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_status_returns_awaiting_llm_state_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = uuid4()
    conn = _mock_conn()
    app = _build_app(conn)

    class _State:
        state = "awaiting_llm"
        reason = "llm output invalid: empty response"
        at = _NOW

    get_state = AsyncMock(return_value=_State())
    monkeypatch.setattr(
        shortlist_routes.shortlist_service, "get_shortlist_state", get_state
    )

    async with await _client(app) as client:
        resp = await client.get(f"/jobs/{job_id}/shortlist/status")

    assert resp.status_code == 200
    body = resp.json()
    assert body["job_id"] == str(job_id)
    assert body["state"] == "awaiting_llm"
    assert body["reason"] == "llm output invalid: empty response"
    assert body["at"] is not None
    get_state.assert_awaited_once()
    assert job_id in list(get_state.await_args.args) + list(
        get_state.await_args.kwargs.values()
    )


# ── 404 on a missing job ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_status_404s_when_job_does_not_exist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = uuid4()
    conn = _mock_conn()
    app = _build_app(conn)
    get_state = AsyncMock(
        side_effect=NotFoundError(f"job {job_id} not found", job_id=str(job_id))
    )
    monkeypatch.setattr(
        shortlist_routes.shortlist_service, "get_shortlist_state", get_state
    )

    async with await _client(app) as client:
        resp = await client.get(f"/jobs/{job_id}/shortlist/status")

    assert resp.status_code == 404


# ── RBAC parity with list_shortlist (all four roles readable) ─────────────


@pytest.mark.parametrize("role", _ALL_ROLES)
@pytest.mark.asyncio
async def test_status_is_readable_by_every_role(
    role: Role, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_id = uuid4()
    conn = _mock_conn()
    app = _build_app(conn, role=role)
    if role == Role.HIRING_MANAGER:
        # ADR-020 §3, fail-closed: mirrors test_route_shortlist.py's identical
        # fix for list_shortlist / get_shortlist_entry / export.
        app.dependency_overrides[resolve_user] = lambda: _identity_user(
            cas_username="hm", role="hiring_manager"
        )
    # The service is mocked here — this test is about ROLE-gating (does the
    # dependency chain let the request through at all), not about row-level
    # assignment scoping (that's the dedicated "passes_user_id" tests below,
    # and the real filtering proof is test_shortlist_fail_closed_pg.py).
    get_state = AsyncMock(return_value=None)
    monkeypatch.setattr(
        shortlist_routes.shortlist_service, "get_shortlist_state", get_state
    )

    async with await _client(app) as client:
        resp = await client.get(f"/jobs/{job_id}/shortlist/status")

    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_status_writers_can_also_read_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """The status route is a read (no side effects) — the two WRITER-only
    roles from generate_shortlist (admin/recruiter) must not be blocked
    either; already covered by the parametrized all-roles test above, but
    pinned explicitly since this route sits right next to the write-only
    generate route in the same module."""
    job_id = uuid4()
    conn = _mock_conn()
    app = _build_app(conn, role=Role.RECRUITER)
    get_state = AsyncMock(return_value=None)
    monkeypatch.setattr(
        shortlist_routes.shortlist_service, "get_shortlist_state", get_state
    )

    async with await _client(app) as client:
        resp = await client.get(f"/jobs/{job_id}/shortlist/status")

    assert resp.status_code != 403


# ── route actually exists (not a 404-because-unregistered false pass) ─────


@pytest.mark.asyncio
async def test_status_route_is_registered_not_a_plain_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Distinguishes "the route doesn't exist" (would 404 with no service
    call at all) from a genuine NotFoundError-driven 404 above — this test
    proves the service function IS reached on the happy path."""
    job_id = uuid4()
    conn = _mock_conn()
    app = _build_app(conn)
    get_state = AsyncMock(return_value=None)
    monkeypatch.setattr(
        shortlist_routes.shortlist_service, "get_shortlist_state", get_state
    )

    async with await _client(app) as client:
        await client.get(f"/jobs/{job_id}/shortlist/status")

    get_state.assert_awaited_once()


# ── IDOR fix — GET /jobs/{job_id}/shortlist/status forwards user_id ────────
#
# Mirrors test_route_shortlist.py's identically-shaped
# "list_shortlist_passes_user_id_for_a_hiring_manager_session" /
# "..._passes_user_id_none_for_unscoped_roles" / "..._403s_and_never_calls..."
# trio. Currently RED: the route calls
# ``shortlist_service.get_shortlist_state(db, job_id)`` with NO ``user_id``
# kwarg at all, so ``get_state.await_args.kwargs.get("user_id")`` is always
# ``None`` — including for a real hiring_manager session, where it must be
# that session's own id.


@pytest.mark.asyncio
async def test_status_passes_user_id_for_a_hiring_manager_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = uuid4()
    conn = _mock_conn()
    app = _build_app(conn, role=Role.HIRING_MANAGER)
    hm = _identity_user(cas_username="hm", role="hiring_manager")
    app.dependency_overrides[resolve_user] = lambda: hm
    get_state = AsyncMock(return_value=None)
    monkeypatch.setattr(
        shortlist_routes.shortlist_service, "get_shortlist_state", get_state
    )

    async with await _client(app) as client:
        resp = await client.get(f"/jobs/{job_id}/shortlist/status")

    assert resp.status_code == 200
    get_state.assert_awaited_once()
    assert get_state.await_args.kwargs.get("user_id") == hm.id


@pytest.mark.asyncio
async def test_status_passes_user_id_for_a_hiring_manager_even_with_the_shared_recruiter_key(  # noqa: E501
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The trap ADR-020 §4/§3 calls out explicitly: the Flask viewer's shared
    "recruiter" API key resolves ``key_role`` to ``Role.RECRUITER`` even when
    the signed-in human is a hiring_manager. Scoping must key off the SESSION
    identity, not the key role."""
    job_id = uuid4()
    conn = _mock_conn()
    app = _build_app(conn, role=Role.RECRUITER)
    hm = _identity_user(cas_username="hm", role="hiring_manager")
    app.dependency_overrides[resolve_user] = lambda: hm
    get_state = AsyncMock(return_value=None)
    monkeypatch.setattr(
        shortlist_routes.shortlist_service, "get_shortlist_state", get_state
    )

    async with await _client(app) as client:
        resp = await client.get(f"/jobs/{job_id}/shortlist/status")

    assert resp.status_code == 200
    get_state.assert_awaited_once()
    assert get_state.await_args.kwargs.get("user_id") == hm.id


@pytest.mark.parametrize("key_role,session_role", _UNSCOPED_KEY_ROLES)
@pytest.mark.asyncio
async def test_status_passes_user_id_none_for_unscoped_roles(
    key_role: Role, session_role: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_id = uuid4()
    conn = _mock_conn()
    app = _build_app(conn, role=key_role)
    session_user = _identity_user(cas_username="someone", role=session_role)
    app.dependency_overrides[resolve_user] = lambda: session_user
    get_state = AsyncMock(return_value=None)
    monkeypatch.setattr(
        shortlist_routes.shortlist_service, "get_shortlist_state", get_state
    )

    async with await _client(app) as client:
        resp = await client.get(f"/jobs/{job_id}/shortlist/status")

    assert resp.status_code == 200
    get_state.assert_awaited_once()
    assert get_state.await_args.kwargs.get("user_id") is None


@pytest.mark.asyncio
async def test_status_passes_user_id_none_for_dev_anonymous_admin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ambient default (no ``resolve_user`` override): the synthetic
    dev-anonymous admin identity resolves. Must stay unscoped."""
    job_id = uuid4()
    conn = _mock_conn()
    app = _build_app(conn, role=Role.ADMIN)
    get_state = AsyncMock(return_value=None)
    monkeypatch.setattr(
        shortlist_routes.shortlist_service, "get_shortlist_state", get_state
    )

    async with await _client(app) as client:
        resp = await client.get(f"/jobs/{job_id}/shortlist/status")

    assert resp.status_code == 200
    get_state.assert_awaited_once()
    assert get_state.await_args.kwargs.get("user_id") is None


@pytest.mark.asyncio
async def test_status_403s_and_never_calls_the_service_for_hiring_manager_key_with_no_session(  # noqa: E501
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail-closed (ADR-020 §3 final paragraph): a hiring_manager KEY with no
    verifiable session identity must 403 and must NEVER reach the service
    layer unscoped. Already true today (``scoped_user_id_or_403`` raises
    before the service call) — pinned here as a regression guard alongside
    the new user_id-forwarding tests."""
    job_id = uuid4()
    conn = _mock_conn()
    app = _build_app(conn, role=Role.HIRING_MANAGER)
    app.dependency_overrides[resolve_user] = lambda: None
    get_state = AsyncMock(return_value=None)
    monkeypatch.setattr(
        shortlist_routes.shortlist_service, "get_shortlist_state", get_state
    )

    async with await _client(app) as client:
        resp = await client.get(f"/jobs/{job_id}/shortlist/status")

    assert resp.status_code == 403
    get_state.assert_not_awaited()
