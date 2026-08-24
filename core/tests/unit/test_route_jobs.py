"""Route-level tests for Phase 6's job routes — ``src.api.routes.jobs``
(new module; does not exist yet). The whole file fails at collection
(``ModuleNotFoundError``) — RED half of the TDD cycle.

Uses a real ASGI app (a fresh ``FastAPI()``, not the whole ``src.api.main``
app, to stay independent of lifespan wiring) with ``httpx.AsyncClient`` +
``ASGITransport``, exercised through ``app.dependency_overrides`` for
``get_db`` / ``get_arq`` / ``resolve_role`` — mirroring the tester
mandate: mock db/arq/blob_store, let the real service layer run against a
mocked ``asyncpg`` connection (no service-internals monkeypatching).

**Ambiguities this file locks (the coder's implementation MUST match):**

* ``src.api.routes.jobs`` exposes an ``APIRouter`` named ``router`` with
  ABSOLUTE paths (no router-level ``prefix``): ``POST /jobs``,
  ``GET /jobs``, ``GET /jobs/{job_id}``, ``PATCH /jobs/{job_id}/status``,
  ``POST /jobs/jd-extract``. ``src.api.main`` mounts it with
  ``app.include_router(jobs.router)``.
* ``POST /jobs`` — body is ``JobCreate`` JSON. Returns **201** with the
  created ``JobOut`` and enqueues ``arq.enqueue_job("parse_job",
  str(job.id))`` with the NEWLY CREATED id (not a client-supplied one — jobs
  have no client-chosen id).
* ``GET /jobs/{job_id}`` — 404 (via a global ``AppError`` exception handler
  mapping ``NotFoundError.status == 404``) when the id does not resolve.
  **``blind_review`` fail-open guard (ADR-006 / carried every phase since
  Phase 2 security)**: tested BOTH directions — a job stored
  ``blind_review=True`` in the DB returns ``"blind_review": true`` in the
  JSON body, one stored ``False`` returns ``false``. This is the load-bearing
  pair; a route that hardcodes either literal fails one of the two.
* ``GET /jobs`` — a TRIMMED ``JobListItem`` shape: exactly
  ``{id, title, department, status, created_at, parsed_at}`` per row — no
  ``description_raw``/``blind_review``/other full-detail fields leak into
  the list view.
* ``PATCH /jobs/{job_id}/status`` — body ``JobTransition`` (``{"to":
  "open"}``). A valid forward transition (draft -> open) returns **200**
  with the updated ``JobOut``. An invalid transition (e.g. draft -> archived,
  skipping open/closed) returns **409** (a business-rule conflict, distinct
  from the 422 a syntactically-invalid ``JobStatus`` enum member would
  already get from pydantic).
* ``POST /jobs/jd-extract`` — ``multipart/form-data`` with a ``file`` field.
  Returns **200** with a ``JDExtractText``-shaped body
  (``filename``/``text``/``chars``) and performs **NO** database write at
  all — ``db.execute``/``db.fetchrow``/``db.fetch`` are never called for
  this route.
* **FU-5 slice 7 (ADR-019 §8.3/§9.2, REVERSES the Phase-6 line this replaces
  — the old ``X-Actor-Name`` header no longer exists anywhere).**
  ``created_by`` on job create is sourced from ``src.api.deps.resolve_user``:
  the resolved user's ``cas_username`` when a human/dev-anonymous identity
  resolves, ``None`` when no identity resolves (a bare service-key caller).
  A leftover ``X-Actor-Name`` header on the request is read nowhere and has
  no effect. See the dedicated section near the end of this file.
* **FU-5 slice 9 (ADR-019 §1.4/§6) — ``PATCH /jobs/{job_id}`` gains
  attributable audit for the ``blind_review`` flip.** The route now also
  depends on ``resolve_user`` and forwards a three-case-derived actor
  identity into ``job_service.update_job`` (mirroring, but NOT identical to,
  the reveal route's own actor derivation — see the dedicated section near
  the end of this file for why blind_review has a THIRD actor case reveal
  does not).
* **FU-6 slice 9 (ADR-020 §7)** — ``GET /my/jobs`` is a NEW route on this
  same router: the caller's own assigned job set, for ANY role. It does
  NOT reuse ``scoped_user_id_or_403`` (that helper returns ``None`` —
  "all jobs" — for admin/recruiter/auditor sessions); instead it calls
  ``job_service.list_jobs(user_id=<the resolved session user's own id>)``
  directly, for every role including admin/recruiter/auditor. A caller
  with no resolvable session (``resolve_user`` -> ``None``) gets 403 ("a
  'my' view needs a 'me'"); the CAS-disabled dev-anonymous synthetic
  admin is NOT "no session" (it resolves the fixed sentinel id) and gets
  200 + ``[]`` instead. See the dedicated section near the end of this
  file.

**FU-4 (RBAC) — route→role table for this file, per the locked decisions:**

| Route                        | Method | Allowed roles                  |
|-------------------------------|--------|---------------------------------|
| ``/jobs``                     | POST   | admin, recruiter                |
| ``/jobs/jd-extract``          | POST   | admin, recruiter                |
| ``/jobs/bulk``                | POST   | admin, recruiter (see below)    |
| ``/jobs``                     | GET    | all four                        |
| ``/jobs/{id}``                | GET    | all four                        |
| ``/jobs/{id}``                | PATCH  | admin, recruiter ONLY (D5)      |
| ``/jobs/{id}/status``         | PATCH  | admin, recruiter                |
| ``/my/jobs``                  | GET    | all four (session-scoped to self)|

``/jobs/bulk`` is covered in ``test_route_jobs_bulk.py``. ``PATCH
/jobs/{id}`` gets the most explicit coverage in this file (D5): that PATCH can
set ``blind_review: false``, permanently un-blinding every résumé/shortlist
under a job with NO audit row.

Every route now goes through a per-route ``Depends(require_role(...))``
(replacing the Phase-6 uniform router-level ``require_api_key``), which in
turn depends on the shared ``resolve_role``. Tests bypass auth for the
non-RBAC-focused tests by overriding ``resolve_role`` to always resolve
``Role.ADMIN`` (admin is allowed everywhere in the table above) — the actual
per-route ``require_role(...)`` check still runs for real against that
resolved role, it is never itself overridden (see
``test_api_deps.py::test_require_role_composes_with_resolve_role_via_dependency_override``
for why that is the correct override point).
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

from src.api.deps import (
    _DEV_ADMIN_SENTINEL_ID,
    Role,
    get_arq,
    resolve_role,
    resolve_user,
)
from src.api.routes import jobs as jobs_routes
from src.errors import AppError, NotFoundError
from src.models.pool import get_db
from src.schemas.auth import User
from src.schemas.jobs import JobOut
from src.services import audit_service

_NOW = dt.datetime(2026, 7, 16, tzinfo=dt.UTC)

# The disallowed roles for the admin/recruiter-only routes in this file.
_NON_JOB_WRITER_ROLES: tuple[Role, ...] = (Role.HIRING_MANAGER, Role.AUDITOR)
_ALL_ROLES: tuple[Role, ...] = tuple(Role)


class _Row(dict[str, Any]):
    def __getitem__(self, key: str) -> Any:
        return dict.get(self, key)


def _job_row(
    *,
    job_id: UUID | None = None,
    title: str = "Senior Backend Engineer",
    status: str = "draft",
    blind_review: bool = True,
    department: str | None = "Engineering",
    created_by: str | None = "api",
) -> _Row:
    return _Row(
        {
            "id": job_id or uuid4(),
            "title": title,
            "department": department,
            "location": "Remote",
            "employment_type": "full_time",
            "seniority": "senior",
            "min_years": 5,
            "description_raw": "We need a senior backend engineer. " * 3,
            "description_parsed": None,
            "status": status,
            "retention_days": 180,
            "shortlist_top_percent": 100,
            "blind_review": blind_review,
            "failure_reason": None,
            "created_by": created_by,
            "created_at": _NOW,
            "updated_at": _NOW,
            "parsed_at": None,
            "closed_at": None,
        }
    )


def _mock_conn(
    *,
    fetchrow: _Row | None = None,
    fetch: list[_Row] | None = None,
    fetchval: Any = None,
) -> MagicMock:
    conn = MagicMock(name="conn")
    conn.fetchrow = AsyncMock(return_value=fetchrow)
    conn.fetch = AsyncMock(return_value=fetch or [])
    conn.fetchval = AsyncMock(return_value=fetchval)
    conn.execute = AsyncMock(return_value="UPDATE 1")
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=None)
    cm.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=cm)
    return conn


def _build_app(
    conn: MagicMock, *, arq: MagicMock | None = None, role: Role = Role.ADMIN
) -> FastAPI:
    app = FastAPI()
    app.include_router(jobs_routes.router)

    async def _get_db_override() -> AsyncIterator[Any]:
        yield conn

    app.dependency_overrides[get_db] = _get_db_override
    app.dependency_overrides[get_arq] = lambda: arq or MagicMock(
        enqueue_job=AsyncMock()
    )
    # FU-4: bypass key→role resolution with a fixed resolved role — the real
    # per-route require_role(...) check still runs against it for real.
    app.dependency_overrides[resolve_role] = lambda: role

    @app.exception_handler(AppError)
    async def _app_error_handler(_request: Any, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status, content={"detail": exc.message, "code": exc.code}
        )

    return app


async def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# ── POST /jobs ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_job_returns_201() -> None:
    conn = _mock_conn(fetchrow=_job_row())
    app = _build_app(conn)
    async with await _client(app) as client:
        resp = await client.post(
            "/jobs",
            json={
                "title": "Senior Backend Engineer",
                "description_raw": "We need a senior backend engineer. " * 3,
            },
        )
    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_create_job_enqueues_parse_job_with_the_new_id() -> None:
    job_id = uuid4()
    conn = _mock_conn(fetchrow=_job_row(job_id=job_id))
    arq = MagicMock(enqueue_job=AsyncMock())
    app = _build_app(conn, arq=arq)
    async with await _client(app) as client:
        await client.post(
            "/jobs",
            json={
                "title": "Senior Backend Engineer",
                "description_raw": "We need a senior backend engineer. " * 3,
            },
        )
    arq.enqueue_job.assert_awaited_once_with("parse_job", str(job_id))


@pytest.mark.asyncio
async def test_create_job_as_recruiter_succeeds() -> None:
    """Admin is not the ONLY writer role — recruiter must also be able to
    create jobs (proves the allowed set is {admin, recruiter}, not just
    admin-only)."""
    conn = _mock_conn(fetchrow=_job_row())
    app = _build_app(conn, role=Role.RECRUITER)
    async with await _client(app) as client:
        resp = await client.post(
            "/jobs",
            json={
                "title": "Senior Backend Engineer",
                "description_raw": "We need a senior backend engineer. " * 3,
            },
        )
    assert resp.status_code == 201


@pytest.mark.parametrize("role", _NON_JOB_WRITER_ROLES)
@pytest.mark.asyncio
async def test_create_job_403s_for_hiring_manager_and_auditor(role: Role) -> None:
    conn = _mock_conn(fetchrow=_job_row())
    app = _build_app(conn, role=role)
    async with await _client(app) as client:
        resp = await client.post(
            "/jobs",
            json={
                "title": "Senior Backend Engineer",
                "description_raw": "We need a senior backend engineer. " * 3,
            },
        )
    assert resp.status_code == 403


# ── GET /jobs/{id} ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_job_404_via_app_error_handler() -> None:
    conn = _mock_conn(fetchrow=None)
    app = _build_app(conn)
    async with await _client(app) as client:
        resp = await client.get(f"/jobs/{uuid4()}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_job_blind_review_true_from_a_true_row() -> None:
    conn = _mock_conn(fetchrow=_job_row(blind_review=True))
    app = _build_app(conn)
    async with await _client(app) as client:
        resp = await client.get(f"/jobs/{uuid4()}")
    assert resp.status_code == 200
    assert resp.json()["blind_review"] is True


@pytest.mark.asyncio
async def test_get_job_blind_review_false_from_a_false_row() -> None:
    """The pairing test — a route hardcoding True instead of reading the row
    would pass the test above but fail this one."""
    conn = _mock_conn(fetchrow=_job_row(blind_review=False))
    app = _build_app(conn)
    async with await _client(app) as client:
        resp = await client.get(f"/jobs/{uuid4()}")
    assert resp.status_code == 200
    assert resp.json()["blind_review"] is False


@pytest.mark.parametrize("role", _ALL_ROLES)
@pytest.mark.asyncio
async def test_get_job_is_readable_by_every_role(role: Role) -> None:
    """FU-6 slice 5 (ADR-020 §3, fail-closed): a ``hiring_manager`` KEY needs
    a verifiable ``hiring_manager`` SESSION to be scoped-and-served — with no
    session override it now 403s (see the dedicated slice-5 section below),
    so this "every role can read" case supplies one for that role only. The
    other three roles are unaffected by row-scoping (ADR-020 §4) and keep
    relying on the ambient dev-anonymous-admin ``resolve_user`` default."""
    conn = _mock_conn(fetchrow=_job_row())
    app = _build_app(conn, role=role)
    if role == Role.HIRING_MANAGER:
        app.dependency_overrides[resolve_user] = lambda: _user(
            cas_username="hm", role="hiring_manager"
        )
    async with await _client(app) as client:
        resp = await client.get(f"/jobs/{uuid4()}")
    assert resp.status_code == 200


# ── GET /jobs (list) ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_jobs_returns_the_trimmed_joblistitem_shape() -> None:
    conn = _mock_conn(fetch=[_job_row(title="A"), _job_row(title="B")])
    app = _build_app(conn)
    async with await _client(app) as client:
        resp = await client.get("/jobs")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 2
    expected_keys = {"id", "title", "department", "status", "created_at", "parsed_at"}
    assert set(body[0].keys()) == expected_keys


@pytest.mark.parametrize("role", _ALL_ROLES)
@pytest.mark.asyncio
async def test_list_jobs_is_readable_by_every_role(role: Role) -> None:
    """FU-6 slice 5 (ADR-020 §3, fail-closed): see the matching docstring on
    ``test_get_job_is_readable_by_every_role`` — a ``hiring_manager`` KEY
    needs a verifiable session to be served; supply one here so this case
    keeps proving "every role, once properly authorized, can read"."""
    conn = _mock_conn(fetch=[_job_row()])
    app = _build_app(conn, role=role)
    if role == Role.HIRING_MANAGER:
        app.dependency_overrides[resolve_user] = lambda: _user(
            cas_username="hm", role="hiring_manager"
        )
    async with await _client(app) as client:
        resp = await client.get("/jobs")
    assert resp.status_code == 200


# ── FU-6 slice 5 (ADR-020 §3/§5) — row-scoping WIRING for GET /jobs and
#    GET /jobs/{job_id} ───────────────────────────────────────────────────
#
# These tests prove WIRING only: that the route resolves the caller's CAS
# session identity (``resolve_user``) and key role (``require_role``'s
# resolved ``Role``) and forwards the correct ``user_id`` to
# ``job_service.list_jobs`` / ``job_service.get_job`` via
# ``scoped_user_id_or_403`` (already unit-tested standalone in
# ``test_api_deps.py``). They mock ``job_service`` entirely, so they cannot
# prove the EXISTS predicate actually FILTERS rows — that structural proof
# is ``test_job_scoping_pg.py``'s job (real Postgres, ADR-020 §3, mandatory).

_UNSCOPED_KEY_ROLES: tuple[tuple[Role, str], ...] = (
    (Role.ADMIN, "admin"),
    (Role.RECRUITER, "recruiter"),
    (Role.AUDITOR, "auditor"),
)


def _job_out(job_id: UUID | None = None) -> JobOut:
    """A valid ``JobOut`` the mocked ``job_service.get_job`` can return —
    the route's return-type annotation doubles as its response_model, so a
    mocked return value must satisfy the real schema or FastAPI 500s before
    the wiring assertion below ever runs."""
    return JobOut(**_job_row(job_id=job_id))


@pytest.mark.asyncio
async def test_list_jobs_passes_user_id_for_a_hiring_manager_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _mock_conn()
    app = _build_app(conn, role=Role.HIRING_MANAGER)
    hm = _user(cas_username="hm", role="hiring_manager")
    app.dependency_overrides[resolve_user] = lambda: hm
    list_jobs_mock = AsyncMock(return_value=[])
    monkeypatch.setattr(jobs_routes.job_service, "list_jobs", list_jobs_mock)

    async with await _client(app) as client:
        resp = await client.get("/jobs")

    assert resp.status_code == 200
    list_jobs_mock.assert_awaited_once()
    assert list_jobs_mock.await_args.kwargs.get("user_id") == hm.id


@pytest.mark.asyncio
async def test_list_jobs_passes_user_id_for_a_hiring_manager_even_with_the_shared_recruiter_key(  # noqa: E501
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The trap ADR-020 §4/§3 calls out explicitly: the Flask viewer's
    browser session presents the ONE shared ``recruiter`` API key for every
    human, so ``key_role`` resolves to ``Role.RECRUITER`` even when the
    signed-in human is a hiring_manager. Scoping must key off the SESSION
    identity, not the key role."""
    conn = _mock_conn()
    app = _build_app(conn, role=Role.RECRUITER)
    hm = _user(cas_username="hm", role="hiring_manager")
    app.dependency_overrides[resolve_user] = lambda: hm
    list_jobs_mock = AsyncMock(return_value=[])
    monkeypatch.setattr(jobs_routes.job_service, "list_jobs", list_jobs_mock)

    async with await _client(app) as client:
        resp = await client.get("/jobs")

    assert resp.status_code == 200
    list_jobs_mock.assert_awaited_once()
    assert list_jobs_mock.await_args.kwargs.get("user_id") == hm.id


@pytest.mark.parametrize("key_role,session_role", _UNSCOPED_KEY_ROLES)
@pytest.mark.asyncio
async def test_list_jobs_passes_user_id_none_for_unscoped_roles(
    key_role: Role, session_role: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = _mock_conn()
    app = _build_app(conn, role=key_role)
    session_user = _user(cas_username="someone", role=session_role)
    app.dependency_overrides[resolve_user] = lambda: session_user
    list_jobs_mock = AsyncMock(return_value=[])
    monkeypatch.setattr(jobs_routes.job_service, "list_jobs", list_jobs_mock)

    async with await _client(app) as client:
        resp = await client.get("/jobs")

    assert resp.status_code == 200
    list_jobs_mock.assert_awaited_once()
    assert list_jobs_mock.await_args.kwargs.get("user_id") is None


@pytest.mark.asyncio
async def test_list_jobs_passes_user_id_none_for_dev_anonymous_admin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ambient default (no ``resolve_user`` override, ``cas_enabled=False``):
    the synthetic dev-anonymous admin identity resolves. Must stay
    unscoped."""
    conn = _mock_conn()
    app = _build_app(conn, role=Role.ADMIN)
    list_jobs_mock = AsyncMock(return_value=[])
    monkeypatch.setattr(jobs_routes.job_service, "list_jobs", list_jobs_mock)

    async with await _client(app) as client:
        resp = await client.get("/jobs")

    assert resp.status_code == 200
    list_jobs_mock.assert_awaited_once()
    assert list_jobs_mock.await_args.kwargs.get("user_id") is None


@pytest.mark.asyncio
async def test_list_jobs_403s_and_never_calls_the_service_for_hiring_manager_key_with_no_session(  # noqa: E501
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail-closed (ADR-020 §3 final paragraph): a hiring_manager key with no
    verifiable session identity must 403 and must NEVER reach the service
    layer unscoped."""
    conn = _mock_conn()
    app = _build_app(conn, role=Role.HIRING_MANAGER)
    app.dependency_overrides[resolve_user] = lambda: None
    list_jobs_mock = AsyncMock(return_value=[])
    monkeypatch.setattr(jobs_routes.job_service, "list_jobs", list_jobs_mock)

    async with await _client(app) as client:
        resp = await client.get("/jobs")

    assert resp.status_code == 403
    list_jobs_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_job_passes_user_id_for_a_hiring_manager_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = uuid4()
    conn = _mock_conn()
    app = _build_app(conn, role=Role.HIRING_MANAGER)
    hm = _user(cas_username="hm", role="hiring_manager")
    app.dependency_overrides[resolve_user] = lambda: hm
    get_job_mock = AsyncMock(return_value=_job_out(job_id=job_id))
    monkeypatch.setattr(jobs_routes.job_service, "get_job", get_job_mock)

    async with await _client(app) as client:
        resp = await client.get(f"/jobs/{job_id}")

    assert resp.status_code == 200
    get_job_mock.assert_awaited_once()
    assert get_job_mock.await_args.kwargs.get("user_id") == hm.id


@pytest.mark.parametrize("key_role,session_role", _UNSCOPED_KEY_ROLES)
@pytest.mark.asyncio
async def test_get_job_passes_user_id_none_for_unscoped_roles(
    key_role: Role, session_role: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_id = uuid4()
    conn = _mock_conn()
    app = _build_app(conn, role=key_role)
    session_user = _user(cas_username="someone", role=session_role)
    app.dependency_overrides[resolve_user] = lambda: session_user
    get_job_mock = AsyncMock(return_value=_job_out(job_id=job_id))
    monkeypatch.setattr(jobs_routes.job_service, "get_job", get_job_mock)

    async with await _client(app) as client:
        resp = await client.get(f"/jobs/{job_id}")

    assert resp.status_code == 200
    get_job_mock.assert_awaited_once()
    assert get_job_mock.await_args.kwargs.get("user_id") is None


@pytest.mark.asyncio
async def test_get_job_passes_user_id_none_for_dev_anonymous_admin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = uuid4()
    conn = _mock_conn()
    app = _build_app(conn, role=Role.ADMIN)
    get_job_mock = AsyncMock(return_value=_job_out(job_id=job_id))
    monkeypatch.setattr(jobs_routes.job_service, "get_job", get_job_mock)

    async with await _client(app) as client:
        resp = await client.get(f"/jobs/{job_id}")

    assert resp.status_code == 200
    get_job_mock.assert_awaited_once()
    assert get_job_mock.await_args.kwargs.get("user_id") is None


@pytest.mark.asyncio
async def test_get_job_403s_and_never_calls_the_service_for_hiring_manager_key_with_no_session(  # noqa: E501
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = uuid4()
    conn = _mock_conn()
    app = _build_app(conn, role=Role.HIRING_MANAGER)
    app.dependency_overrides[resolve_user] = lambda: None
    get_job_mock = AsyncMock(return_value=_job_out(job_id=job_id))
    monkeypatch.setattr(jobs_routes.job_service, "get_job", get_job_mock)

    async with await _client(app) as client:
        resp = await client.get(f"/jobs/{job_id}")

    assert resp.status_code == 403
    get_job_mock.assert_not_awaited()


# ── PATCH /jobs/{id}/status ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_patch_job_status_draft_to_open_succeeds() -> None:
    job_id = uuid4()
    conn = _mock_conn(fetchrow=_job_row(job_id=job_id, status="open"), fetchval="draft")
    app = _build_app(conn)
    async with await _client(app) as client:
        resp = await client.patch(f"/jobs/{job_id}/status", json={"to": "open"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "open"


@pytest.mark.asyncio
async def test_patch_job_status_rejects_an_invalid_transition() -> None:
    job_id = uuid4()
    conn = _mock_conn(
        fetchrow=_job_row(job_id=job_id, status="draft"), fetchval="draft"
    )
    app = _build_app(conn)
    async with await _client(app) as client:
        resp = await client.patch(f"/jobs/{job_id}/status", json={"to": "archived"})
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_patch_job_status_as_recruiter_succeeds() -> None:
    job_id = uuid4()
    conn = _mock_conn(fetchrow=_job_row(job_id=job_id, status="open"), fetchval="draft")
    app = _build_app(conn, role=Role.RECRUITER)
    async with await _client(app) as client:
        resp = await client.patch(f"/jobs/{job_id}/status", json={"to": "open"})
    assert resp.status_code == 200


@pytest.mark.parametrize("role", _NON_JOB_WRITER_ROLES)
@pytest.mark.asyncio
async def test_patch_job_status_403s_for_hiring_manager_and_auditor(
    role: Role,
) -> None:
    job_id = uuid4()
    conn = _mock_conn(
        fetchrow=_job_row(job_id=job_id, status="draft"), fetchval="draft"
    )
    app = _build_app(conn, role=role)
    async with await _client(app) as client:
        resp = await client.patch(f"/jobs/{job_id}/status", json={"to": "open"})
    assert resp.status_code == 403


# ── PATCH /jobs/{id} — general partial update ────────────────────────────


@pytest.mark.asyncio
async def test_patch_job_returns_200_with_updated_jobout() -> None:
    job_id = uuid4()
    conn = _mock_conn(fetchrow=_job_row(job_id=job_id, title="Updated Title"))
    app = _build_app(conn)
    async with await _client(app) as client:
        resp = await client.patch(f"/jobs/{job_id}", json={"title": "Updated Title"})
    assert resp.status_code == 200
    assert resp.json()["title"] == "Updated Title"


@pytest.mark.asyncio
async def test_patch_job_404_for_missing_job() -> None:
    conn = _mock_conn(fetchrow=None)
    app = _build_app(conn)
    async with await _client(app) as client:
        resp = await client.patch(f"/jobs/{uuid4()}", json={"title": "New Title"})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_patch_job_422_rejects_status_field() -> None:
    """``status`` has its own state-machine-guarded transition endpoint —
    it must not be smuggled through the general PATCH. ``JobUpdate`` has no
    ``status`` field at all, so pydantic's ``extra="forbid"`` rejects it with
    a 422 before the route body is ever handled."""
    conn = _mock_conn(fetchrow=_job_row())
    app = _build_app(conn)
    async with await _client(app) as client:
        resp = await client.patch(f"/jobs/{uuid4()}", json={"status": "open"})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_patch_job_422_rejects_approval_required_2nd_review_field() -> None:
    """A cut hris column must not be smuggled in through extra="forbid"."""
    conn = _mock_conn(fetchrow=_job_row())
    app = _build_app(conn)
    async with await _client(app) as client:
        resp = await client.patch(
            f"/jobs/{uuid4()}", json={"approval_required_2nd_review": True}
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_patch_job_requires_auth_like_sibling_routes() -> None:
    """Every route on this router still passes through ``resolve_role`` —
    remove the bypass override and a 401 must surface (FU-4: the auth-switch
    failure mode moved from ``require_api_key`` to ``resolve_role``, but the
    invariant that this route is not accidentally exempt from auth entirely
    is unchanged)."""
    from fastapi import HTTPException

    job_id = uuid4()
    conn = _mock_conn(fetchrow=_job_row(job_id=job_id))
    app = _build_app(conn)

    def _deny() -> None:
        raise HTTPException(status_code=401, detail="missing api key")

    app.dependency_overrides[resolve_role] = _deny
    async with await _client(app) as client:
        resp = await client.patch(f"/jobs/{job_id}", json={"title": "X"})
    assert resp.status_code == 401


# ── PATCH /jobs/{id} — FU-4 D5: admin/recruiter ONLY ─────────────────────
#
# The MOST EXPLICIT coverage in this file per the FU-4 decisions doc: this
# PATCH can set ``blind_review: false``, and every redaction key gates off
# ``jobs.blind_review`` — so this route permanently un-blinds every résumé
# and shortlist under a job, with NO audit row at all. A wider blast radius
# than the audited ``POST /resumes/{id}/reveal`` path, hence the tighter,
# explicitly-pinned role set (admin/recruiter ONLY — narrower than
# ``PATCH /jobs/{id}/status``'s otherwise-identical allowed set, which is
# coincidence, not derived from it; each route's role set is independently
# asserted here).


@pytest.mark.asyncio
async def test_patch_job_admin_can_flip_blind_review_off() -> None:
    job_id = uuid4()
    conn = _mock_conn(fetchrow=_job_row(job_id=job_id, blind_review=False))
    app = _build_app(conn, role=Role.ADMIN)
    async with await _client(app) as client:
        resp = await client.patch(f"/jobs/{job_id}", json={"blind_review": False})
    assert resp.status_code == 200
    assert resp.json()["blind_review"] is False


@pytest.mark.asyncio
async def test_patch_job_recruiter_can_flip_blind_review_off() -> None:
    job_id = uuid4()
    conn = _mock_conn(fetchrow=_job_row(job_id=job_id, blind_review=False))
    app = _build_app(conn, role=Role.RECRUITER)
    async with await _client(app) as client:
        resp = await client.patch(f"/jobs/{job_id}", json={"blind_review": False})
    assert resp.status_code == 200


@pytest.mark.parametrize("role", _NON_JOB_WRITER_ROLES)
@pytest.mark.asyncio
async def test_patch_job_403s_for_hiring_manager_and_auditor_d5(role: Role) -> None:
    """D5 (planner finding): a hiring-manager or auditor key must NEVER be
    able to flip ``blind_review`` off for a whole job — that is a wider,
    unaudited de-anonymization blast radius than even the audited reveal
    endpoint."""
    job_id = uuid4()
    conn = _mock_conn(fetchrow=_job_row(job_id=job_id))
    app = _build_app(conn, role=role)
    async with await _client(app) as client:
        resp = await client.patch(f"/jobs/{job_id}", json={"blind_review": False})
    assert resp.status_code == 403


@pytest.mark.parametrize("role", _NON_JOB_WRITER_ROLES)
@pytest.mark.asyncio
async def test_patch_job_403s_hiring_manager_and_auditor_even_for_a_benign_field(
    role: Role,
) -> None:
    """The 403 applies to the WHOLE route, not just blind_review-touching
    payloads — a hiring-manager/auditor key cannot PATCH a job's title
    either. Pins that the restriction is per-route (D5), not per-field."""
    job_id = uuid4()
    conn = _mock_conn(fetchrow=_job_row(job_id=job_id))
    app = _build_app(conn, role=role)
    async with await _client(app) as client:
        resp = await client.patch(f"/jobs/{job_id}", json={"title": "New Title"})
    assert resp.status_code == 403


@pytest.mark.parametrize("role", _NON_JOB_WRITER_ROLES)
@pytest.mark.asyncio
async def test_patch_job_403_happens_before_any_db_write(role: Role) -> None:
    """The role check must reject BEFORE the service layer ever touches the
    database — a disallowed caller must never cause a partial/attempted
    write, even one that would ultimately be rolled back."""
    job_id = uuid4()
    conn = _mock_conn(fetchrow=_job_row(job_id=job_id))
    app = _build_app(conn, role=role)
    async with await _client(app) as client:
        resp = await client.patch(f"/jobs/{job_id}", json={"blind_review": False})
    assert resp.status_code == 403
    conn.execute.assert_not_called()


# ── POST /jobs/jd-extract ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_jd_extract_returns_extracted_text() -> None:
    conn = _mock_conn()
    app = _build_app(conn)
    content = b"We are hiring a senior backend engineer with Python experience."
    async with await _client(app) as client:
        resp = await client.post(
            "/jobs/jd-extract",
            files={"file": ("jd.txt", content, "text/plain")},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert "senior backend engineer" in body["text"].lower()
    assert body["filename"] == "jd.txt"
    assert body["chars"] == len(body["text"])


@pytest.mark.asyncio
async def test_jd_extract_performs_no_database_write() -> None:
    conn = _mock_conn()
    app = _build_app(conn)
    content = b"We are hiring a senior backend engineer."
    async with await _client(app) as client:
        await client.post(
            "/jobs/jd-extract",
            files={"file": ("jd.txt", content, "text/plain")},
        )
    conn.execute.assert_not_called()
    conn.fetchrow.assert_not_called()
    conn.fetch.assert_not_called()


@pytest.mark.parametrize("role", _NON_JOB_WRITER_ROLES)
@pytest.mark.asyncio
async def test_jd_extract_403s_for_hiring_manager_and_auditor(role: Role) -> None:
    conn = _mock_conn()
    app = _build_app(conn, role=role)
    content = b"We are hiring a senior backend engineer."
    async with await _client(app) as client:
        resp = await client.post(
            "/jobs/jd-extract",
            files={"file": ("jd.txt", content, "text/plain")},
        )
    assert resp.status_code == 403


# ── FU-5 slice 7 (ADR-019 §8.3/§9.2): created_by sourced from resolve_user,
#    NOT the deleted X-Actor-Name header ──────────────────────────────────
#
# ``resolve_actor``/``X-Actor-Name`` no longer exist (see ``test_api_deps.py``
# for the deletion pin). ``created_by`` now comes from
# ``src.api.deps.resolve_user`` — the session/dev-anonymous identity's
# ``cas_username`` when a user resolves, ``None`` when it does not (a bare
# service-key caller). These tests inspect the raw asyncpg call args (the
# same technique ``test_services_job_write.py`` uses) rather than the
# response body, because the mocked ``fetchrow`` always returns a canned row
# regardless of what was inserted — the response body alone cannot prove
# which value the route actually passed to the service layer.


def _user(*, cas_username: str = "alice", role: str = "recruiter") -> User:
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


def _fully_populated_job_payload() -> dict[str, Any]:
    """Every optional ``JobCreate`` field is non-None, so the raw asyncpg
    ``fetchrow`` args tuple contains exactly ONE ``None`` when ``created_by``
    is unresolved (a bare service-key caller) — a plain ``None in args``
    assertion would otherwise false-pass against ``department``/
    ``location``/etc. being left at their own ``None`` default."""
    return {
        "title": "Senior Backend Engineer",
        "department": "Engineering",
        "location": "Remote",
        "employment_type": "full_time",
        "seniority": "senior",
        "min_years": 5,
        "description_raw": "We need a senior backend engineer. " * 3,
    }


@pytest.mark.asyncio
async def test_create_job_created_by_is_dev_anonymous_when_cas_disabled() -> None:
    """ADR-019 §10b/§9.2: CAS disabled is this test suite's ambient default
    (``Settings().cas_enabled is False``) — no ``resolve_user`` override is
    installed, so the REAL dependency resolves the synthetic dev-anonymous
    identity, and the newly created job's ``created_by`` is
    ``"dev-anonymous"`` — never the retired ``"api"`` constant."""
    conn = _mock_conn(fetchrow=_job_row())
    app = _build_app(conn)
    async with await _client(app) as client:
        resp = await client.post("/jobs", json=_fully_populated_job_payload())
    assert resp.status_code == 201
    assert "dev-anonymous" in conn.fetchrow.await_args.args


@pytest.mark.asyncio
async def test_create_job_ignores_an_x_actor_name_header_entirely() -> None:
    """A leftover ``X-Actor-Name`` header must be read nowhere — the
    identity source is exclusively ``resolve_user``. Sending the header must
    NOT change ``created_by`` at all: it stays ``"dev-anonymous"``, not the
    header's value."""
    conn = _mock_conn(fetchrow=_job_row())
    app = _build_app(conn)
    async with await _client(app) as client:
        resp = await client.post(
            "/jobs",
            json=_fully_populated_job_payload(),
            headers={"X-Actor-Name": "totally-ignored@example.test"},
        )
    assert resp.status_code == 201
    assert "totally-ignored@example.test" not in conn.fetchrow.await_args.args
    assert "dev-anonymous" in conn.fetchrow.await_args.args


@pytest.mark.asyncio
async def test_create_job_created_by_is_the_resolved_users_cas_username() -> None:
    """A human session (or any non-``None`` ``resolve_user`` resolution)
    populates ``created_by`` with THAT user's ``cas_username`` — not a fixed
    label."""
    conn = _mock_conn(fetchrow=_job_row())
    app = _build_app(conn)
    app.dependency_overrides[resolve_user] = lambda: _user(cas_username="priya")
    async with await _client(app) as client:
        resp = await client.post("/jobs", json=_fully_populated_job_payload())
    assert resp.status_code == 201
    assert "priya" in conn.fetchrow.await_args.args


@pytest.mark.asyncio
async def test_create_job_403s_when_no_user_resolves() -> None:
    """REVERSED (security finding, fix/auth-boundary-fails-open) — this test
    used to pin ``created_by = NULL`` on a 201 for a bare service-key caller
    with NO CAS session at all. A live audit against real Postgres proved
    this exact combination — zero ``API_KEY_*`` configured (so
    ``resolve_role`` trivially resolves ``Role.ADMIN`` for anyone) plus a
    cookie-less caller (``resolve_user`` -> ``None``) — let ANY caller with
    no credential whatsoever flip ``PATCH /jobs/{id}
    {"blind_review": false}`` and get away with an unattributable
    ``actor_kind='service'`` audit row. The human decision: a valid API key
    (or no key at all, when auth is disabled) is NEVER sufficient on its
    own — every write route now requires a REAL human CAS session, so
    ``resolve_user`` -> ``None`` must 403 BEFORE the insert ever runs, not
    fall through to a null-``created_by`` success."""
    conn = _mock_conn(fetchrow=_job_row(created_by=None))
    app = _build_app(conn)
    app.dependency_overrides[resolve_user] = lambda: None
    async with await _client(app) as client:
        resp = await client.post("/jobs", json=_fully_populated_job_payload())
    assert resp.status_code == 403
    conn.fetchrow.assert_not_awaited()


# ── FU-5 slice 9 (ADR-019 §1.4/§6): PATCH /jobs/{id} blind_review flip audit
#
# ``blind_review`` is ADR-019 §1.4's "widest-blast-radius unaudited action" —
# this PATCH permanently un-blinds every résumé/shortlist under a job. Every
# test below runs through the REAL ``job_service.update_job`` against a
# mocked ``asyncpg`` connection (never mocking ``job_service`` itself), and
# monkeypatches only ``job_service.audit_service.record_audit`` — the same
# boundary ``test_route_reveal.py`` mocks for the reveal route, and the same
# reason: this isolates the ROUTE's actor-derivation and pass-through
# contract from ``record_audit``'s own INSERT shape (covered by
# ``test_audit_service.py``) and from the flip-detection logic inside
# ``update_job`` itself (covered by ``test_services_job_write.py``).
#
# Unlike reveal, blind_review is NOT human-only (ADR-019 §1.4) — a bare
# service-key caller may flip it too. This gives a THIRD actor case reveal's
# 403 doesn't have:
#   Case 1: a real human session resolves -> actor_kind='user',
#           actor_user_id=user.id.
#   Case 2: the CAS-disabled synthetic dev-anonymous identity resolves
#           (this suite's ambient default, no resolve_user override) ->
#           actor_kind='service', actor_service='dev-anonymous'.
#   Case 3: no session resolves at all (resolve_user -> None, e.g. CAS
#           enabled with no session) -> actor_kind='service',
#           actor_service='api'. Reveal 403s here; this route must NOT.


@pytest.mark.asyncio
async def test_patch_job_blind_review_flip_writes_audit_row_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = uuid4()
    conn = _mock_conn(
        fetchrow=_job_row(job_id=job_id, blind_review=False), fetchval=True
    )
    audit = AsyncMock(return_value=None)
    monkeypatch.setattr(jobs_routes.job_service.audit_service, "record_audit", audit)

    app = _build_app(conn)
    async with await _client(app) as client:
        resp = await client.patch(f"/jobs/{job_id}", json={"blind_review": False})

    assert resp.status_code == 200
    audit.assert_awaited_once()
    kwargs = audit.await_args.kwargs
    assert kwargs["action"] == "blind_review_toggled"
    assert kwargs["subject_type"] == "job"
    assert kwargs["subject_id"] == job_id
    assert kwargs["job_id"] == job_id
    assert kwargs["details"] == {"old": True, "new": False}


@pytest.mark.asyncio
async def test_patch_job_no_blind_review_change_writes_no_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A client resending the CURRENT value (True -> True) is not a flip."""
    job_id = uuid4()
    conn = _mock_conn(
        fetchrow=_job_row(job_id=job_id, blind_review=True), fetchval=True
    )
    audit = AsyncMock(return_value=None)
    monkeypatch.setattr(jobs_routes.job_service.audit_service, "record_audit", audit)

    app = _build_app(conn)
    async with await _client(app) as client:
        resp = await client.patch(f"/jobs/{job_id}", json={"blind_review": True})

    assert resp.status_code == 200
    audit.assert_not_awaited()


@pytest.mark.asyncio
async def test_patch_job_blind_review_absent_from_payload_writes_no_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = uuid4()
    conn = _mock_conn(fetchrow=_job_row(job_id=job_id, title="New Title"))
    audit = AsyncMock(return_value=None)
    monkeypatch.setattr(jobs_routes.job_service.audit_service, "record_audit", audit)

    app = _build_app(conn)
    async with await _client(app) as client:
        resp = await client.patch(f"/jobs/{job_id}", json={"title": "New Title"})

    assert resp.status_code == 200
    audit.assert_not_awaited()


@pytest.mark.asyncio
async def test_patch_job_actor_case1_human_session_writes_user_actor_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = uuid4()
    user = _user(cas_username="priya")
    conn = _mock_conn(
        fetchrow=_job_row(job_id=job_id, blind_review=False), fetchval=True
    )
    audit = AsyncMock(return_value=None)
    monkeypatch.setattr(jobs_routes.job_service.audit_service, "record_audit", audit)

    app = _build_app(conn)
    app.dependency_overrides[resolve_user] = lambda: user
    async with await _client(app) as client:
        resp = await client.patch(f"/jobs/{job_id}", json={"blind_review": False})

    assert resp.status_code == 200
    kwargs = audit.await_args.kwargs
    assert kwargs["actor_kind"] == "user"
    assert kwargs["actor_user_id"] == user.id
    assert kwargs.get("actor_service") is None


@pytest.mark.asyncio
async def test_patch_job_actor_case2_dev_anonymous_writes_service_actor_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ambient default (no ``resolve_user`` override): CAS disabled resolves
    the synthetic dev-anonymous identity -> ``actor_kind='service'`` /
    ``actor_service='dev-anonymous'`` (never ``actor_kind='user'`` against
    the sentinel id — see ``test_route_reveal.py``'s identical rule)."""
    job_id = uuid4()
    conn = _mock_conn(
        fetchrow=_job_row(job_id=job_id, blind_review=False), fetchval=True
    )
    audit = AsyncMock(return_value=None)
    monkeypatch.setattr(jobs_routes.job_service.audit_service, "record_audit", audit)

    app = _build_app(conn)
    async with await _client(app) as client:
        resp = await client.patch(f"/jobs/{job_id}", json={"blind_review": False})

    assert resp.status_code == 200
    kwargs = audit.await_args.kwargs
    assert kwargs["actor_kind"] == "service"
    assert kwargs["actor_service"] == "dev-anonymous"
    assert kwargs.get("actor_user_id") is None


@pytest.mark.asyncio
async def test_patch_job_actor_case3_no_session_403s_and_never_flips_blind_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REVERSED (security finding, fix/auth-boundary-fails-open) — this test
    used to pin a 200 plus an unattributable ``actor_kind='service'`` /
    ``actor_service='api'`` audit row for a bare service-key caller (no CAS
    session at all) flipping ``blind_review``. A live audit against real
    Postgres proved this exact gap: with no ``API_KEY_*`` configured at all
    (``resolve_role`` trivially resolves ``Role.ADMIN`` for anyone) a
    cookie-less, key-less caller could ``PATCH /jobs/{id}
    {"blind_review": false}`` and permanently un-blind every résumé/
    shortlist under the job, audited to an actor nobody could trace to a
    person. ``blind_review`` is no longer an exception to the human-session
    requirement — EVERY write route now needs a real, resolvable CAS
    session; a bare service-key caller (``resolve_user`` -> ``None``) must
    403 before ``job_service.update_job``/the audit write ever run."""
    job_id = uuid4()
    conn = _mock_conn(
        fetchrow=_job_row(job_id=job_id, blind_review=False), fetchval=True
    )
    audit = AsyncMock(return_value=None)
    monkeypatch.setattr(jobs_routes.job_service.audit_service, "record_audit", audit)

    app = _build_app(conn)
    app.dependency_overrides[resolve_user] = lambda: None
    async with await _client(app) as client:
        resp = await client.patch(f"/jobs/{job_id}", json={"blind_review": False})

    assert resp.status_code == 403
    audit.assert_not_awaited()


@pytest.mark.asyncio
async def test_patch_job_blind_review_flip_ignores_x_actor_name_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A leftover ``X-Actor-Name`` header (retired since slice 7) must be
    read nowhere for the audit actor either: the ambient dev-anonymous
    identity is used regardless of the header's value."""
    job_id = uuid4()
    conn = _mock_conn(
        fetchrow=_job_row(job_id=job_id, blind_review=False), fetchval=True
    )
    audit = AsyncMock(return_value=None)
    monkeypatch.setattr(jobs_routes.job_service.audit_service, "record_audit", audit)

    app = _build_app(conn)
    async with await _client(app) as client:
        resp = await client.patch(
            f"/jobs/{job_id}",
            json={"blind_review": False},
            headers={"X-Actor-Name": "alice@example.test"},
        )

    assert resp.status_code == 200
    kwargs = audit.await_args.kwargs
    assert kwargs["actor_kind"] == "service"
    assert kwargs["actor_service"] == "dev-anonymous"


# ── FU-6 slice 8 (ADR-020 §6) — auditor read-logging: "the watchers are
#    watched" ───────────────────────────────────────────────────────────────
#
# Auditors keep GLOBAL, unscoped read access (ADR-020 §4) — the compensating
# control is that every auditor READ is itself logged to ``audit_log``
# (ADR-020 §6). Scope decision (a coordinator judgment, pinned here): only
# the DELIBERATE single-subject read ``GET /jobs/{id}`` is logged — the
# polled list-index route ``GET /jobs`` (hit by the Flask frontend every
# ~3s, carrying no specific subject) is deliberately NOT logged; logging
# every poll would flood ``audit_log`` with noise that names no particular
# job. See ``test_list_jobs_never_writes_a_read_log_even_for_an_auditor``
# below for the negative pin.
#
# The read-log fires ONLY for a REAL auditor session — ``user is not None
# and user.role == "auditor"`` AND ``user.id != _DEV_ADMIN_SENTINEL_ID`` (the
# ambient CAS-disabled default resolves a SYNTHETIC identity with
# ``role="admin"``, never ``"auditor"``, so it is excluded by the role check
# alone — a dedicated test below pins it explicitly anyway). Written only on
# a SUCCESSFUL (200) read — a 404 means nothing was read, so no row.
#
# These tests patch the SHARED ``src.services.audit_service`` module object
# directly (imported fresh at the top of this file), not a
# ``jobs_routes``-local alias — this makes the pin agnostic to whether the
# coder's implementation imports it as ``from src.services import
# audit_service`` directly into ``jobs.py``, or reuses ``job_service``'s
# existing import (``jobs_routes.job_service.audit_service`` is the SAME
# module object, per Python's module cache, as already proven by the
# ``blind_review`` audit tests above patching it that exact way). Either
# implementation choice is intercepted identically by patching the shared
# module.


def _auditor_session_user(*, cas_username: str = "audrey") -> User:
    return _user(cas_username=cas_username, role="auditor")


@pytest.mark.asyncio
async def test_get_job_real_auditor_session_writes_exactly_one_read_job_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = uuid4()
    conn = _mock_conn()
    app = _build_app(conn, role=Role.AUDITOR)
    auditor = _auditor_session_user()
    app.dependency_overrides[resolve_user] = lambda: auditor
    get_job_mock = AsyncMock(return_value=_job_out(job_id=job_id))
    monkeypatch.setattr(jobs_routes.job_service, "get_job", get_job_mock)
    audit = AsyncMock(return_value=None)
    monkeypatch.setattr(audit_service, "record_audit", audit)

    async with await _client(app) as client:
        resp = await client.get(f"/jobs/{job_id}")

    assert resp.status_code == 200
    audit.assert_awaited_once()
    kwargs = audit.await_args.kwargs
    assert kwargs["action"] == "read_job"
    assert kwargs["actor_kind"] == "user"
    assert kwargs["actor_user_id"] == auditor.id
    assert kwargs["subject_type"] == "job"
    assert kwargs["subject_id"] == job_id


@pytest.mark.parametrize(
    "key_role,session_role",
    [
        (Role.ADMIN, "admin"),
        (Role.RECRUITER, "recruiter"),
        (Role.HIRING_MANAGER, "hiring_manager"),
    ],
)
@pytest.mark.asyncio
async def test_get_job_non_auditor_session_writes_no_read_log(
    key_role: Role, session_role: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_id = uuid4()
    conn = _mock_conn()
    app = _build_app(conn, role=key_role)
    session_user = _user(cas_username="someone", role=session_role)
    app.dependency_overrides[resolve_user] = lambda: session_user
    get_job_mock = AsyncMock(return_value=_job_out(job_id=job_id))
    monkeypatch.setattr(jobs_routes.job_service, "get_job", get_job_mock)
    audit = AsyncMock(return_value=None)
    monkeypatch.setattr(audit_service, "record_audit", audit)

    async with await _client(app) as client:
        resp = await client.get(f"/jobs/{job_id}")

    assert resp.status_code == 200
    audit.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_job_dev_anonymous_admin_writes_no_read_log(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ambient default (no ``resolve_user`` override, CAS disabled): the
    synthetic dev-anonymous identity resolves with ``role="admin"``, never
    ``"auditor"`` — must never write a read-log row."""
    job_id = uuid4()
    conn = _mock_conn()
    app = _build_app(conn, role=Role.ADMIN)
    get_job_mock = AsyncMock(return_value=_job_out(job_id=job_id))
    monkeypatch.setattr(jobs_routes.job_service, "get_job", get_job_mock)
    audit = AsyncMock(return_value=None)
    monkeypatch.setattr(audit_service, "record_audit", audit)

    async with await _client(app) as client:
        resp = await client.get(f"/jobs/{job_id}")

    assert resp.status_code == 200
    audit.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_job_404_writes_no_read_log_even_for_a_real_auditor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 404 means nothing was read — no audit row, even for a genuine
    auditor session. (Auditor is unscoped per ADR-020 §4, so in practice this
    fires only for a genuinely nonexistent job id.)"""
    job_id = uuid4()
    conn = _mock_conn()
    app = _build_app(conn, role=Role.AUDITOR)
    auditor = _auditor_session_user()
    app.dependency_overrides[resolve_user] = lambda: auditor
    get_job_mock = AsyncMock(
        side_effect=NotFoundError(f"job {job_id} not found", job_id=str(job_id))
    )
    monkeypatch.setattr(jobs_routes.job_service, "get_job", get_job_mock)
    audit = AsyncMock(return_value=None)
    monkeypatch.setattr(audit_service, "record_audit", audit)

    async with await _client(app) as client:
        resp = await client.get(f"/jobs/{job_id}")

    assert resp.status_code == 404
    audit.assert_not_awaited()


@pytest.mark.asyncio
async def test_list_jobs_never_writes_a_read_log_even_for_an_auditor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scope decision pinned above: the polled list-index route ``GET
    /jobs`` is NEVER read-logged, even for a real auditor session — it
    carries no specific subject and the frontend polls it every ~3s."""
    conn = _mock_conn()
    app = _build_app(conn, role=Role.AUDITOR)
    auditor = _auditor_session_user()
    app.dependency_overrides[resolve_user] = lambda: auditor
    list_jobs_mock = AsyncMock(return_value=[])
    monkeypatch.setattr(jobs_routes.job_service, "list_jobs", list_jobs_mock)
    audit = AsyncMock(return_value=None)
    monkeypatch.setattr(audit_service, "record_audit", audit)

    async with await _client(app) as client:
        resp = await client.get("/jobs")

    assert resp.status_code == 200
    audit.assert_not_awaited()


# ── FU-6 slice 9 (ADR-020 §7) — GET /my/jobs: the caller's own assigned set,
#    for ANY role ─────────────────────────────────────────────────────────
#
# THIS DIFFERS FROM THE SCOPED READ ROUTES ABOVE. ``GET /jobs``/``GET
# /jobs/{id}`` use ``scoped_user_id_or_403``, which returns ``None`` — "all
# jobs" — for admin/recruiter/auditor sessions (ADR-020 §4's global-read
# roles). ``GET /my/jobs`` is a PERSONAL "my assignments" view: it must scope
# to the calling session user's OWN id directly, regardless of role — an
# admin session here still gets back only THAT admin's own assignments
# (typically none), never every job in the company. A route that reuses
# ``scoped_user_id_or_403`` would fail every "non-hiring_manager role still
# scopes to self" test below (they would all get ``user_id=None``, i.e. "all
# jobs", instead of the caller's own id).
#
# Identity is REQUIRED: no session (``resolve_user`` -> ``None``, the
# CAS-enabled-no-cookie case) -> 403, service never called ("a 'my' view
# needs a 'me'"). The CAS-disabled dev-anonymous synthetic admin is
# DIFFERENT: it resolves a real (sentinel) id via ``resolve_user``, so it is
# NOT "no session" — it scopes to the sentinel id (an empty result set,
# since the sentinel is never a real assignee) and returns 200 + [], not
# 403.


@pytest.mark.asyncio
async def test_my_jobs_route_exists_and_returns_the_trimmed_joblistitem_shape() -> None:
    conn = _mock_conn(fetch=[_job_row(title="A"), _job_row(title="B")])
    app = _build_app(conn)
    async with await _client(app) as client:
        resp = await client.get("/my/jobs")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 2
    expected_keys = {"id", "title", "department", "status", "created_at", "parsed_at"}
    assert set(body[0].keys()) == expected_keys


@pytest.mark.asyncio
async def test_my_jobs_passes_user_id_for_a_hiring_manager_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _mock_conn()
    app = _build_app(conn, role=Role.HIRING_MANAGER)
    hm = _user(cas_username="hm", role="hiring_manager")
    app.dependency_overrides[resolve_user] = lambda: hm
    list_jobs_mock = AsyncMock(return_value=[])
    monkeypatch.setattr(jobs_routes.job_service, "list_jobs", list_jobs_mock)

    async with await _client(app) as client:
        resp = await client.get("/my/jobs")

    assert resp.status_code == 200
    list_jobs_mock.assert_awaited_once()
    assert list_jobs_mock.await_args.kwargs.get("user_id") == hm.id


@pytest.mark.parametrize("key_role,session_role", _UNSCOPED_KEY_ROLES)
@pytest.mark.asyncio
async def test_my_jobs_passes_the_session_users_own_id_for_admin_recruiter_and_auditor(
    key_role: Role, session_role: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The load-bearing negative pin against ``scoped_user_id_or_403`` reuse:
    for THESE roles, ``GET /jobs`` passes ``user_id=None`` (see
    ``test_list_jobs_passes_user_id_none_for_unscoped_roles`` above) —
    ``GET /my/jobs`` must NOT: it passes the CALLER'S OWN id, proving it
    scopes to the session user directly rather than delegating to the
    all-jobs-for-unscoped-roles helper."""
    conn = _mock_conn()
    app = _build_app(conn, role=key_role)
    session_user = _user(cas_username="someone", role=session_role)
    app.dependency_overrides[resolve_user] = lambda: session_user
    list_jobs_mock = AsyncMock(return_value=[])
    monkeypatch.setattr(jobs_routes.job_service, "list_jobs", list_jobs_mock)

    async with await _client(app) as client:
        resp = await client.get("/my/jobs")

    assert resp.status_code == 200
    list_jobs_mock.assert_awaited_once()
    assert list_jobs_mock.await_args.kwargs.get("user_id") == session_user.id
    assert list_jobs_mock.await_args.kwargs.get("user_id") is not None


@pytest.mark.asyncio
async def test_my_jobs_403s_and_never_calls_the_service_when_no_session_resolves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 'my' view needs a 'me' — a bare service-key caller with no
    verifiable session (``resolve_user`` -> ``None``, e.g. CAS enabled with
    no cookie) gets 403 and the service is never reached."""
    conn = _mock_conn()
    app = _build_app(conn, role=Role.ADMIN)
    app.dependency_overrides[resolve_user] = lambda: None
    list_jobs_mock = AsyncMock(return_value=[])
    monkeypatch.setattr(jobs_routes.job_service, "list_jobs", list_jobs_mock)

    async with await _client(app) as client:
        resp = await client.get("/my/jobs")

    assert resp.status_code == 403
    list_jobs_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_my_jobs_dev_anonymous_scopes_to_the_sentinel_id_and_returns_200(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CAS-disabled synthetic dev-admin identity is NOT 'no session' — it
    IS a resolved (sentinel) identity, so ``resolve_user`` never returns
    ``None`` for it. It must scope to the sentinel id (an empty result,
    since the sentinel is never a real assignee) and return 200 + [], never
    403. Ambient default: no ``resolve_user`` override needed."""
    conn = _mock_conn()
    app = _build_app(conn, role=Role.ADMIN)
    list_jobs_mock = AsyncMock(return_value=[])
    monkeypatch.setattr(jobs_routes.job_service, "list_jobs", list_jobs_mock)

    async with await _client(app) as client:
        resp = await client.get("/my/jobs")

    assert resp.status_code == 200
    assert resp.json() == []
    list_jobs_mock.assert_awaited_once()
    assert list_jobs_mock.await_args.kwargs.get("user_id") == _DEV_ADMIN_SENTINEL_ID


@pytest.mark.asyncio
async def test_my_jobs_requires_auth_like_sibling_routes() -> None:
    """The standard RBAC key gate still applies (``require_role``) —
    removing the ``resolve_role`` bypass override must surface a 401,
    exactly like the other job routes."""
    from fastapi import HTTPException

    conn = _mock_conn()
    app = _build_app(conn)

    def _deny() -> None:
        raise HTTPException(status_code=401, detail="missing api key")

    app.dependency_overrides[resolve_role] = _deny
    async with await _client(app) as client:
        resp = await client.get("/my/jobs")
    assert resp.status_code == 401
