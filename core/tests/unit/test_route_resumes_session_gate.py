"""RED pin — Phase 1.1 of the pilot-readiness plan (fix/session-role-on-writes).

See ``test_route_jobs_session_gate.py``'s module docstring for the full
defect description; this file pins the identical production combination
(key role = ``Role.RECRUITER``, session role = ``hiring_manager``/``auditor``)
against every write route on ``src.api.routes.resumes``:

* ``POST /jobs/{job_id}/resumes`` (upload, resumes.py:89)
* ``POST /resumes/{resume_id}/reveal`` (resumes.py:290)
* ``POST /resumes/{resume_id}/withdraw`` (resumes.py:394)
* ``POST /resumes/{resume_id}/reinstate`` (resumes.py:421)
* ``POST /resumes/{resume_id}/match-jobs`` (resumes.py:461)

Every negative test asserts BOTH the 403 status AND that the underlying
mutation (the monkeypatched service call, or ``arq.enqueue_job``) never ran —
a role check that runs after the mutation would still surface a 403 to the
HTTP caller while having already performed the very write it exists to
prevent (mirrors ``test_route_reveal.py``'s own discipline).
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
from src.api.routes import resumes as resumes_routes
from src.errors import AppError
from src.models.pool import get_db
from src.schemas.auth import User
from src.schemas.resumes import CandidateInfo, ResumeOut
from src.storage.blob_store import get_blob_store

_NOW = dt.datetime(2026, 8, 7, tzinfo=dt.UTC)
_PDF_MAGIC = b"%PDF-1.4\nresume content\n" + b"x" * 500

_NON_WRITER_SESSION_ROLES: tuple[str, ...] = ("hiring_manager", "auditor")


def _acm() -> MagicMock:
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=None)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _mock_conn(*, exists: bool = True) -> MagicMock:
    conn = MagicMock(name="conn")
    conn.fetchrow = AsyncMock(return_value=None)
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchval = AsyncMock(return_value=(uuid4() if exists else None))
    conn.execute = AsyncMock(return_value="UPDATE 1")
    conn.transaction = MagicMock(return_value=_acm())
    return conn


def _mock_blob_store() -> MagicMock:
    store = MagicMock(name="blob_store")
    store.put = AsyncMock(return_value=None)
    return store


def _resume_out(
    resume_id: UUID | None = None,
    *,
    withdrawn_at: dt.datetime | None = None,
) -> ResumeOut:
    return ResumeOut(
        id=resume_id or uuid4(),
        job_id=uuid4(),
        original_filename="resume.pdf",
        mime_type="application/pdf",
        file_size_bytes=1234,
        sha256="a" * 64,
        candidate=CandidateInfo(name=None, email=None, phone=None, location=None),
        candidate_email_hash=None,
        parsed=None,
        status="parsed",
        uploaded_by="api",
        uploaded_at=_NOW,
        parsed_at=_NOW,
        failure_reason=None,
        consent_acknowledged=True,
        blinded=True,
        withdrawn_at=withdrawn_at,
        withdrawal_reason=None,
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
    conn: MagicMock,
    *,
    arq: MagicMock | None = None,
    store: MagicMock | None = None,
    key_role: Role = Role.RECRUITER,
) -> FastAPI:
    app = FastAPI()
    app.include_router(resumes_routes.router)

    async def _get_db_override() -> AsyncIterator[Any]:
        yield conn

    app.dependency_overrides[get_db] = _get_db_override
    app.dependency_overrides[get_blob_store] = lambda: store or _mock_blob_store()
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


# ── POST /jobs/{job_id}/resumes — upload (resumes.py:89) ────────────────


@pytest.mark.parametrize("session_role", _NON_WRITER_SESSION_ROLES)
@pytest.mark.asyncio
async def test_upload_resumes_403s_for_recruiter_key_with_non_writer_session(
    session_role: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    upload = AsyncMock(return_value=[])
    monkeypatch.setattr(resumes_routes.resume_service, "upload_resumes", upload)
    arq = MagicMock(enqueue_job=AsyncMock())
    store = _mock_blob_store()
    app = _build_app(_mock_conn(), arq=arq, store=store, key_role=Role.RECRUITER)
    app.dependency_overrides[resolve_user] = lambda: _session_user(role=session_role)

    async with await _client(app) as client:
        resp = await client.post(
            f"/jobs/{uuid4()}/resumes",
            files=[("files", ("a.pdf", _PDF_MAGIC, "application/pdf"))],
            data={"consent_acknowledged": "true"},
        )

    assert resp.status_code == 403
    upload.assert_not_awaited()
    arq.enqueue_job.assert_not_awaited()
    store.put.assert_not_called()


@pytest.mark.asyncio
async def test_upload_resumes_recruiter_key_and_recruiter_session_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upload = AsyncMock(return_value=[])
    monkeypatch.setattr(resumes_routes.resume_service, "upload_resumes", upload)
    app = _build_app(_mock_conn(), key_role=Role.RECRUITER)
    app.dependency_overrides[resolve_user] = lambda: _session_user(role="recruiter")

    async with await _client(app) as client:
        resp = await client.post(
            f"/jobs/{uuid4()}/resumes",
            files=[("files", ("a.pdf", _PDF_MAGIC, "application/pdf"))],
            data={"consent_acknowledged": "true"},
        )

    assert resp.status_code == 202


@pytest.mark.asyncio
async def test_upload_resumes_403s_for_recruiter_key_with_no_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REVERSED (security finding, fix/auth-boundary-fails-open) — this test
    used to be docstringed "the case most likely to be broken by an
    over-eager fix" and asserted PASS for ``user is None`` (a bare
    service-key caller with NO CAS session at all): "must never be judged
    by this gate". That framing is now wrong — it protected exactly the
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
    upload = AsyncMock(return_value=[])
    monkeypatch.setattr(resumes_routes.resume_service, "upload_resumes", upload)
    arq = MagicMock(enqueue_job=AsyncMock())
    store = _mock_blob_store()
    app = _build_app(_mock_conn(), arq=arq, store=store, key_role=Role.RECRUITER)
    app.dependency_overrides[resolve_user] = lambda: None

    async with await _client(app) as client:
        resp = await client.post(
            f"/jobs/{uuid4()}/resumes",
            files=[("files", ("a.pdf", _PDF_MAGIC, "application/pdf"))],
            data={"consent_acknowledged": "true"},
        )

    assert resp.status_code == 403
    upload.assert_not_awaited()
    arq.enqueue_job.assert_not_awaited()
    store.put.assert_not_called()


# ── POST /resumes/{resume_id}/reveal (resumes.py:290) ────────────────────


@pytest.mark.parametrize("session_role", _NON_WRITER_SESSION_ROLES)
@pytest.mark.asyncio
async def test_reveal_403s_for_recruiter_key_with_non_writer_session(
    session_role: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    resume_id = uuid4()
    audit = AsyncMock(return_value=None)
    get_one = AsyncMock(return_value=_resume_out(resume_id=resume_id))
    monkeypatch.setattr(resumes_routes.audit_service, "record_audit", audit)
    monkeypatch.setattr(resumes_routes.resume_service, "get_one", get_one)
    app = _build_app(_mock_conn(exists=True), key_role=Role.RECRUITER)
    app.dependency_overrides[resolve_user] = lambda: _session_user(role=session_role)

    async with await _client(app) as client:
        resp = await client.post(f"/resumes/{resume_id}/reveal")

    assert resp.status_code == 403
    audit.assert_not_awaited()
    get_one.assert_not_awaited()


@pytest.mark.asyncio
async def test_reveal_recruiter_key_and_recruiter_session_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resume_id = uuid4()
    monkeypatch.setattr(
        resumes_routes.audit_service, "record_audit", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        resumes_routes.resume_service,
        "get_one",
        AsyncMock(return_value=_resume_out(resume_id=resume_id)),
    )
    app = _build_app(_mock_conn(exists=True), key_role=Role.RECRUITER)
    app.dependency_overrides[resolve_user] = lambda: _session_user(role="recruiter")

    async with await _client(app) as client:
        resp = await client.post(f"/resumes/{resume_id}/reveal")

    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_reveal_recruiter_key_and_admin_session_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resume_id = uuid4()
    monkeypatch.setattr(
        resumes_routes.audit_service, "record_audit", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        resumes_routes.resume_service,
        "get_one",
        AsyncMock(return_value=_resume_out(resume_id=resume_id)),
    )
    app = _build_app(_mock_conn(exists=True), key_role=Role.RECRUITER)
    app.dependency_overrides[resolve_user] = lambda: _session_user(role="admin")

    async with await _client(app) as client:
        resp = await client.post(f"/resumes/{resume_id}/reveal")

    assert resp.status_code == 200


# Note: reveal already 403s on `user is None` for an UNRELATED, pre-existing
# reason (its own human-attribution gate, resumes.py:350-354 — "reveal
# requires an attributable human session"). That gate is orthogonal to the
# session-ROLE gate this file pins, so `resolve_user -> None` is not usable
# as a positive case here (it is already, correctly, a 403 today).


# ── POST /resumes/{resume_id}/withdraw (resumes.py:394) ──────────────────


@pytest.mark.parametrize("session_role", _NON_WRITER_SESSION_ROLES)
@pytest.mark.asyncio
async def test_withdraw_403s_for_recruiter_key_with_non_writer_session(
    session_role: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    resume_id = uuid4()
    withdraw = AsyncMock(return_value=None)
    get_one = AsyncMock(
        return_value=_resume_out(resume_id=resume_id, withdrawn_at=_NOW)
    )
    monkeypatch.setattr(resumes_routes.resume_service, "withdraw_resume", withdraw)
    monkeypatch.setattr(resumes_routes.resume_service, "get_one", get_one)
    app = _build_app(_mock_conn(), key_role=Role.RECRUITER)
    app.dependency_overrides[resolve_user] = lambda: _session_user(role=session_role)

    async with await _client(app) as client:
        resp = await client.post(f"/resumes/{resume_id}/withdraw", json={})

    assert resp.status_code == 403
    withdraw.assert_not_awaited()
    get_one.assert_not_awaited()


@pytest.mark.asyncio
async def test_withdraw_recruiter_key_and_recruiter_session_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resume_id = uuid4()
    monkeypatch.setattr(
        resumes_routes.resume_service, "withdraw_resume", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        resumes_routes.resume_service,
        "get_one",
        AsyncMock(return_value=_resume_out(resume_id=resume_id, withdrawn_at=_NOW)),
    )
    app = _build_app(_mock_conn(), key_role=Role.RECRUITER)
    app.dependency_overrides[resolve_user] = lambda: _session_user(role="recruiter")

    async with await _client(app) as client:
        resp = await client.post(f"/resumes/{resume_id}/withdraw", json={})

    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_withdraw_403s_for_recruiter_key_with_no_session(
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
    resume_id = uuid4()
    withdraw = AsyncMock(return_value=None)
    get_one = AsyncMock(
        return_value=_resume_out(resume_id=resume_id, withdrawn_at=_NOW)
    )
    monkeypatch.setattr(resumes_routes.resume_service, "withdraw_resume", withdraw)
    monkeypatch.setattr(resumes_routes.resume_service, "get_one", get_one)
    app = _build_app(_mock_conn(), key_role=Role.RECRUITER)
    app.dependency_overrides[resolve_user] = lambda: None

    async with await _client(app) as client:
        resp = await client.post(f"/resumes/{resume_id}/withdraw", json={})

    assert resp.status_code == 403
    withdraw.assert_not_awaited()
    get_one.assert_not_awaited()


# ── POST /resumes/{resume_id}/reinstate (resumes.py:421) ─────────────────


@pytest.mark.parametrize("session_role", _NON_WRITER_SESSION_ROLES)
@pytest.mark.asyncio
async def test_reinstate_403s_for_recruiter_key_with_non_writer_session(
    session_role: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    resume_id = uuid4()
    reinstate = AsyncMock(return_value=None)
    get_one = AsyncMock(return_value=_resume_out(resume_id=resume_id))
    monkeypatch.setattr(resumes_routes.resume_service, "reinstate_resume", reinstate)
    monkeypatch.setattr(resumes_routes.resume_service, "get_one", get_one)
    app = _build_app(_mock_conn(), key_role=Role.RECRUITER)
    app.dependency_overrides[resolve_user] = lambda: _session_user(role=session_role)

    async with await _client(app) as client:
        resp = await client.post(f"/resumes/{resume_id}/reinstate")

    assert resp.status_code == 403
    reinstate.assert_not_awaited()
    get_one.assert_not_awaited()


@pytest.mark.asyncio
async def test_reinstate_recruiter_key_and_recruiter_session_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resume_id = uuid4()
    monkeypatch.setattr(
        resumes_routes.resume_service, "reinstate_resume", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        resumes_routes.resume_service,
        "get_one",
        AsyncMock(return_value=_resume_out(resume_id=resume_id)),
    )
    app = _build_app(_mock_conn(), key_role=Role.RECRUITER)
    app.dependency_overrides[resolve_user] = lambda: _session_user(role="recruiter")

    async with await _client(app) as client:
        resp = await client.post(f"/resumes/{resume_id}/reinstate")

    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_reinstate_403s_for_recruiter_key_with_no_session(
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
    resume_id = uuid4()
    reinstate = AsyncMock(return_value=None)
    get_one = AsyncMock(return_value=_resume_out(resume_id=resume_id))
    monkeypatch.setattr(resumes_routes.resume_service, "reinstate_resume", reinstate)
    monkeypatch.setattr(resumes_routes.resume_service, "get_one", get_one)
    app = _build_app(_mock_conn(), key_role=Role.RECRUITER)
    app.dependency_overrides[resolve_user] = lambda: None

    async with await _client(app) as client:
        resp = await client.post(f"/resumes/{resume_id}/reinstate")

    assert resp.status_code == 403
    reinstate.assert_not_awaited()
    get_one.assert_not_awaited()


# ── POST /resumes/{resume_id}/match-jobs (resumes.py:461) ────────────────


@pytest.mark.parametrize("session_role", _NON_WRITER_SESSION_ROLES)
@pytest.mark.asyncio
async def test_trigger_reverse_match_403s_for_recruiter_key_with_non_writer_session(
    session_role: str,
) -> None:
    resume_id = uuid4()
    arq = MagicMock(enqueue_job=AsyncMock())
    app = _build_app(_mock_conn(exists=True), arq=arq, key_role=Role.RECRUITER)
    app.dependency_overrides[resolve_user] = lambda: _session_user(role=session_role)

    async with await _client(app) as client:
        resp = await client.post(f"/resumes/{resume_id}/match-jobs")

    assert resp.status_code == 403
    arq.enqueue_job.assert_not_awaited()


@pytest.mark.asyncio
async def test_trigger_reverse_match_recruiter_key_and_recruiter_session_passes() -> (
    None
):
    resume_id = uuid4()
    app = _build_app(_mock_conn(exists=True), key_role=Role.RECRUITER)
    app.dependency_overrides[resolve_user] = lambda: _session_user(role="recruiter")

    async with await _client(app) as client:
        resp = await client.post(f"/resumes/{resume_id}/match-jobs")

    assert resp.status_code == 202


@pytest.mark.asyncio
async def test_trigger_reverse_match_403s_for_recruiter_key_with_no_session() -> None:
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
    resume_id = uuid4()
    arq = MagicMock(enqueue_job=AsyncMock())
    app = _build_app(_mock_conn(exists=True), arq=arq, key_role=Role.RECRUITER)
    app.dependency_overrides[resolve_user] = lambda: None

    async with await _client(app) as client:
        resp = await client.post(f"/resumes/{resume_id}/match-jobs")

    assert resp.status_code == 403
    arq.enqueue_job.assert_not_awaited()


__all__: list[str] = []
