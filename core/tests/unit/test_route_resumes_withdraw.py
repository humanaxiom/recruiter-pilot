"""Route tests for ADR-026 (FU-8)'s résumé-withdrawal lifecycle routes on
``src.api.routes.resumes``:

* ``POST /resumes/{id}/withdraw`` — admin/recruiter only, optional
  ``{"reason": "..."}`` body (capped 500 chars), idempotent.
* ``POST /resumes/{id}/reinstate`` — admin/recruiter only, symmetric.
* ``GET /jobs/{id}/resume-status`` — open to all four roles, the per-job
  status breakdown (ADR-026 decision 5).

Mirrors ``test_route_reveal.py``'s convention of monkeypatching the SERVICE
layer (``resume_service.withdraw_resume``/``reinstate_resume``/
``status_breakdown``/``get_one``) so this file isolates the ROUTE contract
(RBAC, status codes, request/response shape) — the service's own behaviour
(idempotency, atomicity, audit/outbox writes) is covered by
``test_services_resume_withdraw.py`` and the real-Postgres
``test_resume_withdraw_pg.py`` / ``test_api_resumes_withdraw_pg.py``.

None of these routes exist yet — every test below fails at the first request
(404 "not found" from FastAPI's own router, or an ``AttributeError`` on the
monkeypatch target). RED half of the TDD cycle.
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
from src.api.routes import resumes as resumes_routes
from src.errors import AppError, NotFoundError
from src.models.pool import get_db
from src.schemas.auth import User
from src.schemas.resumes import CandidateInfo, ResumeOut, ResumeStatusBreakdown

_NOW = dt.datetime(2026, 7, 28, tzinfo=dt.UTC)

_NON_WRITER_ROLES: tuple[Role, ...] = (Role.HIRING_MANAGER, Role.AUDITOR)
_ALL_ROLES: tuple[Role, ...] = tuple(Role)


def _resume_out(
    resume_id: UUID | None = None,
    *,
    withdrawn_at: dt.datetime | None = None,
    withdrawal_reason: str | None = None,
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
        withdrawal_reason=withdrawal_reason,
    )


def _mock_conn() -> MagicMock:
    conn = MagicMock(name="conn")
    conn.execute = AsyncMock(return_value="INSERT 0 1")
    conn.fetchval = AsyncMock(return_value=uuid4())
    return conn


def _build_app(conn: MagicMock, *, role: Role = Role.ADMIN) -> FastAPI:
    app = FastAPI()
    app.include_router(resumes_routes.router)

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


# ── POST /resumes/{id}/withdraw ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_withdraw_as_admin_succeeds_and_returns_the_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resume_id = uuid4()
    withdraw = AsyncMock(return_value=None)
    get_one = AsyncMock(
        return_value=_resume_out(resume_id=resume_id, withdrawn_at=_NOW)
    )
    monkeypatch.setattr(resumes_routes.resume_service, "withdraw_resume", withdraw)
    monkeypatch.setattr(resumes_routes.resume_service, "get_one", get_one)

    app = _build_app(_mock_conn(), role=Role.ADMIN)
    async with await _client(app) as client:
        resp = await client.post(
            f"/resumes/{resume_id}/withdraw", json={"reason": "accepted elsewhere"}
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["withdrawn_at"] is not None

    withdraw.assert_awaited_once()
    kwargs = withdraw.await_args.kwargs
    assert kwargs.get("reason") == "accepted elsewhere"


@pytest.mark.asyncio
async def test_withdraw_as_recruiter_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    resume_id = uuid4()
    monkeypatch.setattr(
        resumes_routes.resume_service, "withdraw_resume", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        resumes_routes.resume_service,
        "get_one",
        AsyncMock(return_value=_resume_out(resume_id=resume_id, withdrawn_at=_NOW)),
    )

    app = _build_app(_mock_conn(), role=Role.RECRUITER)
    async with await _client(app) as client:
        resp = await client.post(f"/resumes/{resume_id}/withdraw", json={})

    assert resp.status_code == 200


@pytest.mark.parametrize("role", _NON_WRITER_ROLES)
@pytest.mark.asyncio
async def test_withdraw_403s_for_hiring_manager_and_auditor(
    role: Role, monkeypatch: pytest.MonkeyPatch
) -> None:
    resume_id = uuid4()
    withdraw = AsyncMock(return_value=None)
    monkeypatch.setattr(resumes_routes.resume_service, "withdraw_resume", withdraw)

    app = _build_app(_mock_conn(), role=role)
    async with await _client(app) as client:
        resp = await client.post(f"/resumes/{resume_id}/withdraw", json={})

    assert resp.status_code == 403
    withdraw.assert_not_awaited()


@pytest.mark.asyncio
async def test_withdraw_404_for_a_nonexistent_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resume_id = uuid4()
    withdraw = AsyncMock(
        side_effect=NotFoundError(
            f"resume {resume_id} not found", resume_id=str(resume_id)
        )
    )
    monkeypatch.setattr(resumes_routes.resume_service, "withdraw_resume", withdraw)

    app = _build_app(_mock_conn(), role=Role.ADMIN)
    async with await _client(app) as client:
        resp = await client.post(f"/resumes/{resume_id}/withdraw", json={})

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_withdraw_422_for_an_oversize_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resume_id = uuid4()
    withdraw = AsyncMock(return_value=None)
    monkeypatch.setattr(resumes_routes.resume_service, "withdraw_resume", withdraw)

    app = _build_app(_mock_conn(), role=Role.ADMIN)
    async with await _client(app) as client:
        resp = await client.post(
            f"/resumes/{resume_id}/withdraw", json={"reason": "x" * 501}
        )

    assert resp.status_code == 422
    withdraw.assert_not_awaited()


@pytest.mark.asyncio
async def test_withdraw_accepts_a_reason_at_exactly_500_chars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resume_id = uuid4()
    withdraw = AsyncMock(return_value=None)
    monkeypatch.setattr(resumes_routes.resume_service, "withdraw_resume", withdraw)
    monkeypatch.setattr(
        resumes_routes.resume_service,
        "get_one",
        AsyncMock(return_value=_resume_out(resume_id=resume_id, withdrawn_at=_NOW)),
    )

    app = _build_app(_mock_conn(), role=Role.ADMIN)
    async with await _client(app) as client:
        resp = await client.post(
            f"/resumes/{resume_id}/withdraw", json={"reason": "x" * 500}
        )

    assert resp.status_code == 200
    withdraw.assert_awaited_once()


@pytest.mark.asyncio
async def test_withdraw_double_call_is_idempotent_200_both_times(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wiring pin: the ROUTE never special-cases a second call — it simply
    calls ``withdraw_resume`` again and returns 200 with the (unchanged)
    résumé, deferring the actual idempotency guarantee to the service layer
    (proven directly in ``test_services_resume_withdraw.py`` /
    ``test_resume_withdraw_pg.py``)."""
    resume_id = uuid4()
    withdraw = AsyncMock(return_value=None)
    monkeypatch.setattr(resumes_routes.resume_service, "withdraw_resume", withdraw)
    monkeypatch.setattr(
        resumes_routes.resume_service,
        "get_one",
        AsyncMock(return_value=_resume_out(resume_id=resume_id, withdrawn_at=_NOW)),
    )

    app = _build_app(_mock_conn(), role=Role.ADMIN)
    async with await _client(app) as client:
        first = await client.post(f"/resumes/{resume_id}/withdraw", json={})
        second = await client.post(f"/resumes/{resume_id}/withdraw", json={})

    assert first.status_code == 200
    assert second.status_code == 200
    assert withdraw.await_count == 2


@pytest.mark.asyncio
async def test_withdraw_response_goes_through_get_one_redaction_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The response is built via ``resume_service.get_one`` — the same
    redaction boundary every other read goes through (ADR-006 §4) — never a
    raw re-query that could bypass blind-review masking."""
    resume_id = uuid4()
    monkeypatch.setattr(
        resumes_routes.resume_service, "withdraw_resume", AsyncMock(return_value=None)
    )
    get_one = AsyncMock(
        return_value=_resume_out(resume_id=resume_id, withdrawn_at=_NOW)
    )
    monkeypatch.setattr(resumes_routes.resume_service, "get_one", get_one)

    app = _build_app(_mock_conn(), role=Role.ADMIN)
    async with await _client(app) as client:
        resp = await client.post(f"/resumes/{resume_id}/withdraw", json={})

    assert resp.status_code == 200
    get_one.assert_awaited_once()


# ── POST /resumes/{id}/reinstate ────────────────────────────────────────


@pytest.mark.asyncio
async def test_reinstate_as_admin_succeeds_and_returns_the_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resume_id = uuid4()
    reinstate = AsyncMock(return_value=None)
    get_one = AsyncMock(return_value=_resume_out(resume_id=resume_id))
    monkeypatch.setattr(resumes_routes.resume_service, "reinstate_resume", reinstate)
    monkeypatch.setattr(resumes_routes.resume_service, "get_one", get_one)

    app = _build_app(_mock_conn(), role=Role.ADMIN)
    async with await _client(app) as client:
        resp = await client.post(f"/resumes/{resume_id}/reinstate")

    assert resp.status_code == 200
    body = resp.json()
    assert body["withdrawn_at"] is None
    reinstate.assert_awaited_once()


@pytest.mark.parametrize("role", _NON_WRITER_ROLES)
@pytest.mark.asyncio
async def test_reinstate_403s_for_hiring_manager_and_auditor(
    role: Role, monkeypatch: pytest.MonkeyPatch
) -> None:
    resume_id = uuid4()
    reinstate = AsyncMock(return_value=None)
    monkeypatch.setattr(resumes_routes.resume_service, "reinstate_resume", reinstate)

    app = _build_app(_mock_conn(), role=role)
    async with await _client(app) as client:
        resp = await client.post(f"/resumes/{resume_id}/reinstate")

    assert resp.status_code == 403
    reinstate.assert_not_awaited()


@pytest.mark.asyncio
async def test_reinstate_404_for_a_nonexistent_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resume_id = uuid4()
    reinstate = AsyncMock(
        side_effect=NotFoundError(
            f"resume {resume_id} not found", resume_id=str(resume_id)
        )
    )
    monkeypatch.setattr(resumes_routes.resume_service, "reinstate_resume", reinstate)

    app = _build_app(_mock_conn(), role=Role.ADMIN)
    async with await _client(app) as client:
        resp = await client.post(f"/resumes/{resume_id}/reinstate")

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_reinstate_double_call_is_idempotent_200_both_times(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resume_id = uuid4()
    reinstate = AsyncMock(return_value=None)
    monkeypatch.setattr(resumes_routes.resume_service, "reinstate_resume", reinstate)
    monkeypatch.setattr(
        resumes_routes.resume_service,
        "get_one",
        AsyncMock(return_value=_resume_out(resume_id=resume_id)),
    )

    app = _build_app(_mock_conn(), role=Role.ADMIN)
    async with await _client(app) as client:
        first = await client.post(f"/resumes/{resume_id}/reinstate")
        second = await client.post(f"/resumes/{resume_id}/reinstate")

    assert first.status_code == 200
    assert second.status_code == 200
    assert reinstate.await_count == 2


# ── GET /jobs/{id}/resume-status ────────────────────────────────────────


@pytest.mark.parametrize("role", _ALL_ROLES)
@pytest.mark.asyncio
async def test_resume_status_breakdown_is_readable_by_every_role(
    role: Role, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_id = uuid4()
    breakdown = ResumeStatusBreakdown(
        uploaded=0, parsing=0, parsed=0, failed=0, withdrawn=0, degraded=0
    )
    status_mock = AsyncMock(return_value=breakdown)
    monkeypatch.setattr(resumes_routes.resume_service, "status_breakdown", status_mock)

    app = _build_app(_mock_conn(), role=role)
    if role == Role.HIRING_MANAGER:
        app.dependency_overrides[resolve_user] = lambda: _identity_user(
            cas_username="hm", role="hiring_manager"
        )
    async with await _client(app) as client:
        resp = await client.get(f"/jobs/{job_id}/resume-status")

    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_resume_status_breakdown_zero_resume_job_returns_the_five_bucket_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = uuid4()
    breakdown = ResumeStatusBreakdown(
        uploaded=0, parsing=0, parsed=0, failed=0, withdrawn=0, degraded=0
    )
    monkeypatch.setattr(
        resumes_routes.resume_service,
        "status_breakdown",
        AsyncMock(return_value=breakdown),
    )

    app = _build_app(_mock_conn(), role=Role.ADMIN)
    async with await _client(app) as client:
        resp = await client.get(f"/jobs/{job_id}/resume-status")

    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "uploaded": 0,
        "parsing": 0,
        "parsed": 0,
        "failed": 0,
        "withdrawn": 0,
        "degraded": 0,
    }


@pytest.mark.asyncio
async def test_resume_status_breakdown_forwards_the_job_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = uuid4()
    status_mock = AsyncMock(
        return_value=ResumeStatusBreakdown(
            uploaded=1, parsing=0, parsed=2, failed=0, withdrawn=1, degraded=0
        )
    )
    monkeypatch.setattr(resumes_routes.resume_service, "status_breakdown", status_mock)

    app = _build_app(_mock_conn(), role=Role.ADMIN)
    async with await _client(app) as client:
        resp = await client.get(f"/jobs/{job_id}/resume-status")

    assert resp.status_code == 200
    status_mock.assert_awaited_once()
    flat = list(status_mock.await_args.args) + list(
        status_mock.await_args.kwargs.values()
    )
    assert job_id in flat
