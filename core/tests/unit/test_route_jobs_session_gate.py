"""RED pin — Phase 1.1 of the pilot-readiness plan (fix/session-role-on-writes).

**The defect.** Every write route on ``src.api.routes.jobs`` authorizes the
**API-key** role only, via ``require_role(...)`` -> ``resolve_role``
(``src/api/deps.py:129-147``). It never consults the CAS **session** role.
The Flask BFF attaches ONE shared ``recruiter`` API key to every browser
request (``core/frontend/api_client.py:118-119``), so a signed-in
hiring_manager or auditor performs writes on this router indistinguishably
from a recruiter — the codebase already admits this at ``deps.py:294-295``:
"``require_role`` only judges the KEY, never the session."

This file pins the production combination for every write route on this
router: **key role = ``Role.RECRUITER``** (the one shared Flask key) with a
**real hiring_manager/auditor SESSION** — asserting 403 and that the
underlying mutation never ran. Today every one of these tests reaches the
route and gets a 2xx (the route body runs), because nothing here has ever
consulted ``resolve_user().role``.

**Target fix (not implemented by this file — tests only; UPDATED, security
finding fix/auth-boundary-fails-open).** ``require_session_role(*allowed)``
in ``src.api.deps`` mirrors the already-correct
``src.api.routes.users._require_admin_session`` pattern with ONE reversal
from its original design: a REAL session (``resolve_user()`` is not
``None``) whose ``role`` is not in ``allowed`` -> 403 (plain
``fastapi.HTTPException``, not ``AppError``); ``user is None`` (a bare
service-key caller, no CAS session at all) -> **403 too, not PASS**. The
original design let ``user is None`` pass, on the theory that this gate
"never judges the ABSENCE of a session" — a live audit against real
Postgres proved that theory wrong: combined with this deploy's real config
(zero ``API_KEY_*`` configured, so ``resolve_role`` trivially resolves
``Role.ADMIN`` for every caller), a cookie-less, key-less request could
reach every write route on this router with no credential whatsoever,
proved end-to-end with a bare ``PATCH /jobs/{id} {"blind_review": false}``
that returned 200 and really flipped the column. The human decision: a
valid API key is NOT sufficient on its own — every write route now
requires a REAL, resolvable human CAS session. The CAS-disabled
dev-anonymous sentinel (``role="admin"``) still -> PASS, because that path
resolves a real (synthetic-but-non-``None``) ``User``, not an absent one.

Follows each route's own ``require_role(...)``-bypass idiom
(``app.dependency_overrides[resolve_role]``) from ``test_route_jobs.py`` /
``test_route_jobs_bulk.py``, plus ``app.dependency_overrides[resolve_user]``
for the session identity — the shared ``resolve_role``/``resolve_user``
functions are the correct override points (never the per-route
``require_role(...)`` closure — see
``test_api_deps.py::test_require_role_composes_with_resolve_role_via_dependency_override``).
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

from src.api.deps import Role, get_arq, resolve_role, resolve_user
from src.api.routes import jobs as jobs_routes
from src.errors import AppError
from src.models.pool import get_db
from src.schemas.auth import User
from src.schemas.jobs import JobOut

_NOW = dt.datetime(2026, 8, 7, tzinfo=dt.UTC)

# The production combination: the ONE shared Flask key, paired with each
# non-writer SESSION role. "admin"/"recruiter" sessions are the positive
# cases below, never parametrized here.
_NON_WRITER_SESSION_ROLES: tuple[str, ...] = ("hiring_manager", "auditor")


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


def _job_out(job_id: UUID | None = None) -> JobOut:
    return JobOut(
        id=job_id or uuid4(),
        title="Senior Backend Engineer",
        department="Engineering",
        location="Remote",
        employment_type="full_time",
        seniority="senior",
        min_years=5,
        description_raw="We need a senior backend engineer. " * 3,
        description_parsed=None,
        status="draft",
        retention_days=180,
        shortlist_top_percent=100,
        blind_review=True,
        failure_reason=None,
        created_by="api",
        created_at=_NOW,
        updated_at=_NOW,
        parsed_at=None,
        closed_at=None,
    )


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
    app.include_router(jobs_routes.router)

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


def _job_create_payload() -> dict[str, Any]:
    return {
        "title": "Senior Backend Engineer",
        "description_raw": "We need a senior backend engineer. " * 3,
    }


# ── POST /jobs (jobs.py:67) ──────────────────────────────────────────────


@pytest.mark.parametrize("session_role", _NON_WRITER_SESSION_ROLES)
@pytest.mark.asyncio
async def test_create_job_403s_for_recruiter_key_with_non_writer_session(
    session_role: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_job = AsyncMock(return_value=_job_out())
    monkeypatch.setattr(jobs_routes.job_service, "create_job", create_job)
    arq = MagicMock(enqueue_job=AsyncMock())
    app = _build_app(_mock_conn(), arq=arq, key_role=Role.RECRUITER)
    app.dependency_overrides[resolve_user] = lambda: _session_user(role=session_role)

    async with await _client(app) as client:
        resp = await client.post("/jobs", json=_job_create_payload())

    assert resp.status_code == 403
    create_job.assert_not_awaited()
    arq.enqueue_job.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_job_recruiter_key_and_recruiter_session_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create_job = AsyncMock(return_value=_job_out())
    monkeypatch.setattr(jobs_routes.job_service, "create_job", create_job)
    app = _build_app(_mock_conn(), key_role=Role.RECRUITER)
    app.dependency_overrides[resolve_user] = lambda: _session_user(role="recruiter")

    async with await _client(app) as client:
        resp = await client.post("/jobs", json=_job_create_payload())

    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_create_job_recruiter_key_and_admin_session_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create_job = AsyncMock(return_value=_job_out())
    monkeypatch.setattr(jobs_routes.job_service, "create_job", create_job)
    app = _build_app(_mock_conn(), key_role=Role.RECRUITER)
    app.dependency_overrides[resolve_user] = lambda: _session_user(role="admin")

    async with await _client(app) as client:
        resp = await client.post("/jobs", json=_job_create_payload())

    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_create_job_403s_for_recruiter_key_with_no_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REVERSED (security finding, fix/auth-boundary-fails-open) — this test
    used to be docstringed "the case most likely to be broken by an
    over-eager fix" and asserted PASS for a bare recruiter-key caller with
    NO CAS session at all: "this gate must never judge the ABSENCE of a
    session". That framing is now wrong — it protected exactly the
    vulnerability this slice closes. A live audit against real Postgres
    proved that with this deploy's real config (zero ``API_KEY_*``
    configured, so ``resolve_role`` trivially resolves ``Role.ADMIN`` for
    every caller) a cookie-less, key-less request could reach every write
    route on this router with no credential whatsoever — proved end-to-end
    with a bare ``PATCH /jobs/{id} {"blind_review": false}`` that returned
    200 and really flipped the column, audited to an actor nobody could
    trace to a person. The human decision: a valid API key is NOT
    sufficient on its own — every write route now requires a REAL,
    resolvable CAS session, so ``resolve_user`` -> ``None`` must 403 here
    too, the same as an out-of-allowed-set session role."""
    create_job = AsyncMock(return_value=_job_out())
    monkeypatch.setattr(jobs_routes.job_service, "create_job", create_job)
    app = _build_app(_mock_conn(), key_role=Role.RECRUITER)
    app.dependency_overrides[resolve_user] = lambda: None

    async with await _client(app) as client:
        resp = await client.post("/jobs", json=_job_create_payload())

    assert resp.status_code == 403
    create_job.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_job_recruiter_key_and_ambient_dev_anonymous_sentinel_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ambient default (no ``resolve_user`` override): CAS disabled resolves
    the synthetic dev-anonymous admin sentinel — must PASS."""
    create_job = AsyncMock(return_value=_job_out())
    monkeypatch.setattr(jobs_routes.job_service, "create_job", create_job)
    app = _build_app(_mock_conn(), key_role=Role.RECRUITER)

    async with await _client(app) as client:
        resp = await client.post("/jobs", json=_job_create_payload())

    assert resp.status_code == 201


# ── POST /jobs/jd-extract (jobs.py:91) ───────────────────────────────────


@pytest.mark.parametrize("session_role", _NON_WRITER_SESSION_ROLES)
@pytest.mark.asyncio
async def test_jd_extract_403s_for_recruiter_key_with_non_writer_session(
    session_role: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    extract = MagicMock(return_value="extracted text")
    monkeypatch.setattr(jobs_routes.jd_import_service, "extract_jd_text", extract)
    app = _build_app(_mock_conn(), key_role=Role.RECRUITER)
    app.dependency_overrides[resolve_user] = lambda: _session_user(role=session_role)

    async with await _client(app) as client:
        resp = await client.post(
            "/jobs/jd-extract",
            files={"file": ("jd.txt", b"We are hiring an engineer.", "text/plain")},
        )

    assert resp.status_code == 403
    extract.assert_not_called()


@pytest.mark.asyncio
async def test_jd_extract_recruiter_key_and_recruiter_session_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _build_app(_mock_conn(), key_role=Role.RECRUITER)
    app.dependency_overrides[resolve_user] = lambda: _session_user(role="recruiter")

    async with await _client(app) as client:
        resp = await client.post(
            "/jobs/jd-extract",
            files={"file": ("jd.txt", b"We are hiring an engineer.", "text/plain")},
        )

    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_jd_extract_403s_for_recruiter_key_with_no_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REVERSED (security finding, fix/auth-boundary-fails-open) — this test
    used to assert PASS for a bare recruiter-key caller with NO CAS session
    at all. That was the bug this whole slice closes: a live audit against
    real Postgres proved that with this deploy's real config (zero
    ``API_KEY_*`` configured, so ``resolve_role`` trivially resolves
    ``Role.ADMIN`` for every caller) a cookie-less, key-less request could
    reach every write route on this router with no credential whatsoever.
    The human decision: a valid API key is NOT sufficient on its own —
    every write route now requires a REAL, resolvable CAS session, so
    ``resolve_user`` -> ``None`` must 403 here too."""
    extract = MagicMock(return_value="extracted text")
    monkeypatch.setattr(jobs_routes.jd_import_service, "extract_jd_text", extract)
    app = _build_app(_mock_conn(), key_role=Role.RECRUITER)
    app.dependency_overrides[resolve_user] = lambda: None

    async with await _client(app) as client:
        resp = await client.post(
            "/jobs/jd-extract",
            files={"file": ("jd.txt", b"We are hiring an engineer.", "text/plain")},
        )

    assert resp.status_code == 403
    extract.assert_not_called()


# ── POST /jobs/bulk (jobs.py:106) ────────────────────────────────────────

_JD_TEXT = "Backend role. We are hiring a senior backend engineer. " * 3


@pytest.mark.parametrize("session_role", _NON_WRITER_SESSION_ROLES)
@pytest.mark.asyncio
async def test_bulk_create_jobs_403s_for_recruiter_key_with_non_writer_session(
    session_role: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_bulk = AsyncMock(return_value=[])
    monkeypatch.setattr(jobs_routes.job_service, "create_jobs_bulk", create_bulk)
    arq = MagicMock(enqueue_job=AsyncMock())
    app = _build_app(_mock_conn(), arq=arq, key_role=Role.RECRUITER)
    app.dependency_overrides[resolve_user] = lambda: _session_user(role=session_role)
    files = [("files", ("Backend.txt", _JD_TEXT.encode(), "text/plain"))]

    async with await _client(app) as client:
        resp = await client.post("/jobs/bulk", files=files)

    assert resp.status_code == 403
    create_bulk.assert_not_awaited()
    arq.enqueue_job.assert_not_awaited()


@pytest.mark.asyncio
async def test_bulk_create_jobs_recruiter_key_and_recruiter_session_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create_bulk = AsyncMock(return_value=[])
    monkeypatch.setattr(jobs_routes.job_service, "create_jobs_bulk", create_bulk)
    app = _build_app(_mock_conn(), key_role=Role.RECRUITER)
    app.dependency_overrides[resolve_user] = lambda: _session_user(role="recruiter")
    files = [("files", ("Backend.txt", _JD_TEXT.encode(), "text/plain"))]

    async with await _client(app) as client:
        resp = await client.post("/jobs/bulk", files=files)

    assert resp.status_code == 202


@pytest.mark.asyncio
async def test_bulk_create_jobs_403s_for_recruiter_key_with_no_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REVERSED (security finding, fix/auth-boundary-fails-open) — this test
    used to assert PASS for a bare recruiter-key caller with NO CAS session
    at all. That was the bug this whole slice closes: a live audit against
    real Postgres proved that with this deploy's real config (zero
    ``API_KEY_*`` configured, so ``resolve_role`` trivially resolves
    ``Role.ADMIN`` for every caller) a cookie-less, key-less request could
    reach every write route on this router with no credential whatsoever.
    The human decision: a valid API key is NOT sufficient on its own —
    every write route now requires a REAL, resolvable CAS session, so
    ``resolve_user`` -> ``None`` must 403 here too."""
    create_bulk = AsyncMock(return_value=[])
    monkeypatch.setattr(jobs_routes.job_service, "create_jobs_bulk", create_bulk)
    app = _build_app(_mock_conn(), key_role=Role.RECRUITER)
    app.dependency_overrides[resolve_user] = lambda: None
    files = [("files", ("Backend.txt", _JD_TEXT.encode(), "text/plain"))]

    async with await _client(app) as client:
        resp = await client.post("/jobs/bulk", files=files)

    assert resp.status_code == 403
    create_bulk.assert_not_awaited()


# ── PATCH /jobs/{job_id} (jobs.py:243) ───────────────────────────────────


@pytest.mark.parametrize("session_role", _NON_WRITER_SESSION_ROLES)
@pytest.mark.asyncio
async def test_update_job_403s_for_recruiter_key_with_non_writer_session(
    session_role: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_id = uuid4()
    update_job = AsyncMock(return_value=_job_out(job_id=job_id))
    monkeypatch.setattr(jobs_routes.job_service, "update_job", update_job)
    app = _build_app(_mock_conn(), key_role=Role.RECRUITER)
    app.dependency_overrides[resolve_user] = lambda: _session_user(role=session_role)

    async with await _client(app) as client:
        resp = await client.patch(f"/jobs/{job_id}", json={"title": "New Title"})

    assert resp.status_code == 403
    update_job.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_job_recruiter_key_and_recruiter_session_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = uuid4()
    update_job = AsyncMock(return_value=_job_out(job_id=job_id))
    monkeypatch.setattr(jobs_routes.job_service, "update_job", update_job)
    app = _build_app(_mock_conn(), key_role=Role.RECRUITER)
    app.dependency_overrides[resolve_user] = lambda: _session_user(role="recruiter")

    async with await _client(app) as client:
        resp = await client.patch(f"/jobs/{job_id}", json={"title": "New Title"})

    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_update_job_403s_for_recruiter_key_with_no_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REVERSED (security finding, fix/auth-boundary-fails-open) — this test
    used to be docstringed "the case most likely to be broken by an
    over-eager fix" and asserted PASS for a bare recruiter-key caller with
    NO CAS session at all. That was the bug: a live audit against real
    Postgres proved exactly this route, exploited end-to-end — a
    cookie-less, key-less ``PATCH /jobs/{id} {"blind_review": false}``
    returned 200 and really flipped the column, audited to an actor nobody
    could trace to a person, because this deploy's real config carries zero
    ``API_KEY_*`` (so ``resolve_role`` trivially resolves ``Role.ADMIN`` for
    everyone). The human decision: a valid API key is NOT sufficient on its
    own — every write route now requires a REAL, resolvable CAS session, so
    ``resolve_user`` -> ``None`` must 403 here too."""
    job_id = uuid4()
    update_job = AsyncMock(return_value=_job_out(job_id=job_id))
    monkeypatch.setattr(jobs_routes.job_service, "update_job", update_job)
    app = _build_app(_mock_conn(), key_role=Role.RECRUITER)
    app.dependency_overrides[resolve_user] = lambda: None

    async with await _client(app) as client:
        resp = await client.patch(f"/jobs/{job_id}", json={"title": "New Title"})

    assert resp.status_code == 403
    update_job.assert_not_awaited()


# ── PATCH /jobs/{job_id}/status (jobs.py:279) ────────────────────────────


@pytest.mark.parametrize("session_role", _NON_WRITER_SESSION_ROLES)
@pytest.mark.asyncio
async def test_patch_job_status_403s_for_recruiter_key_with_non_writer_session(
    session_role: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_id = uuid4()
    transition = AsyncMock(return_value=_job_out(job_id=job_id))
    monkeypatch.setattr(jobs_routes.job_service, "transition_status", transition)
    app = _build_app(_mock_conn(), key_role=Role.RECRUITER)
    app.dependency_overrides[resolve_user] = lambda: _session_user(role=session_role)

    async with await _client(app) as client:
        resp = await client.patch(f"/jobs/{job_id}/status", json={"to": "open"})

    assert resp.status_code == 403
    transition.assert_not_awaited()


@pytest.mark.asyncio
async def test_patch_job_status_recruiter_key_and_recruiter_session_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = uuid4()
    transition = AsyncMock(return_value=_job_out(job_id=job_id))
    monkeypatch.setattr(jobs_routes.job_service, "transition_status", transition)
    app = _build_app(_mock_conn(), key_role=Role.RECRUITER)
    app.dependency_overrides[resolve_user] = lambda: _session_user(role="recruiter")

    async with await _client(app) as client:
        resp = await client.patch(f"/jobs/{job_id}/status", json={"to": "open"})

    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_patch_job_status_403s_for_recruiter_key_with_no_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REVERSED (security finding, fix/auth-boundary-fails-open) — this test
    used to be docstringed "the case most likely to be broken by an
    over-eager fix" and asserted PASS for a bare recruiter-key caller with
    NO CAS session at all. That was the bug: a live audit against real
    Postgres proved that with this deploy's real config (zero ``API_KEY_*``
    configured, so ``resolve_role`` trivially resolves ``Role.ADMIN`` for
    every caller) a cookie-less, key-less request could reach this route
    with no credential whatsoever. The human decision: a valid API key is
    NOT sufficient on its own — every write route now requires a REAL,
    resolvable CAS session, so ``resolve_user`` -> ``None`` must 403 here
    too, the same as an out-of-allowed-set session role."""
    job_id = uuid4()
    transition = AsyncMock(return_value=_job_out(job_id=job_id))
    monkeypatch.setattr(jobs_routes.job_service, "transition_status", transition)
    app = _build_app(_mock_conn(), key_role=Role.RECRUITER)
    app.dependency_overrides[resolve_user] = lambda: None

    async with await _client(app) as client:
        resp = await client.patch(f"/jobs/{job_id}/status", json={"to": "open"})

    assert resp.status_code == 403
    transition.assert_not_awaited()


__all__: list[str] = []
