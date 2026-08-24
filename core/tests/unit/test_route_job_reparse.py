"""RED — ``POST /jobs/{job_id}/reparse``, the recovery path the design already
promised and never built.

**The incident this closes.** On 2026-08-21 a bulk upload of 20 JDs was parsed
while ``.env`` still carried ``LLM_TIMEOUT_S=120``. Four jobs exhausted their
three attempts on ``ReadTimeout``, that opened the circuit breaker, and the
remaining sixteen failed instantly with ``LLMUnavailableError: circuit breaker
open`` without ever reaching the model. The timeout was subsequently fixed. All
twenty rows were still dead a day later, because **nothing re-enqueues a failed
JD parse.**

``src.services.job_service``'s own module docstring says the row "stays in
'draft' for a retry", and ``_RECORD_PARSED_SQL`` nulls ``failure_reason`` on
success precisely so a retry can succeed cleanly. The retry was designed. It was
never given a caller — ROADMAP A7 again: an invariant stated in prose with
nothing enacting it.

**Why 'draft' is the gate rather than "has a failure_reason".** A job whose
worker died mid-parse has NO ``failure_reason`` and no ``parsed_at`` — the
silently-stranded shape ``doctor.sh`` already catches on the résumés table. Both
that row and an explicitly-failed row are recoverable, and both are exactly the
rows still sitting in 'draft'. Gating on the failure text would fix the loud case
and leave the silent one permanently dead, which is the worse of the two.
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

from src.api.deps import Role, get_arq, resolve_role
from src.api.routes import jobs as jobs_routes
from src.errors import AppError
from src.models.pool import get_db

_NOW = dt.datetime(2026, 8, 22, 12, 0, tzinfo=dt.UTC)


class _Row(dict[str, Any]):
    """asyncpg Record stand-in — mapping access is all the service layer uses."""


def _job_row(
    *,
    job_id: UUID | None = None,
    status: str = "draft",
    failure_reason: str | None = "LLMUnavailableError: circuit breaker open",
    parsed_at: dt.datetime | None = None,
) -> _Row:
    return _Row(
        {
            "id": job_id or uuid4(),
            "title": "Senior Backend Engineer",
            "department": "Engineering",
            "location": "Remote",
            "employment_type": "full_time",
            "seniority": "senior",
            "min_years": 5,
            "description_raw": "We need a senior backend engineer. " * 3,
            "description_parsed": None,
            "status": status,
            "retention_days": 180,
            "shortlist_top_percent": 100,
            "blind_review": True,
            "failure_reason": failure_reason,
            "created_by": "api",
            "created_at": _NOW,
            "updated_at": _NOW,
            "parsed_at": parsed_at,
            "closed_at": None,
        }
    )


def _mock_conn(*, fetchrow: _Row | None = None, execute: str = "UPDATE 1") -> MagicMock:
    conn = MagicMock(name="conn")
    conn.fetchrow = AsyncMock(return_value=fetchrow)
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchval = AsyncMock(return_value=None)
    conn.execute = AsyncMock(return_value=execute)
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
    app.dependency_overrides[resolve_role] = lambda: role

    @app.exception_handler(AppError)
    async def _app_error_handler(_request: Any, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status, content={"detail": exc.message, "code": exc.code}
        )

    return app


async def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_reparse_enqueues_parse_job_with_the_job_id() -> None:
    """The whole point: a failed JD gets another run at the model."""
    job_id = uuid4()
    conn = _mock_conn(fetchrow=_job_row(job_id=job_id))
    arq = MagicMock(enqueue_job=AsyncMock())
    app = _build_app(conn, arq=arq)
    async with await _client(app) as client:
        resp = await client.post(f"/jobs/{job_id}/reparse")
    assert resp.status_code == 202
    arq.enqueue_job.assert_awaited_once_with("parse_job", str(job_id))


@pytest.mark.asyncio
async def test_reparse_clears_the_stale_failure_reason_before_enqueueing() -> None:
    """Without this the page keeps showing yesterday's error for the whole of
    the new run, so a user cannot tell a retry-in-flight from the dead row they
    just clicked.

    Ordering matters too: clear FIRST, then enqueue. The reverse races a fast
    worker that finishes and writes its own outcome before the clear lands,
    which would wipe a genuinely fresh ``failure_reason`` and strand the row
    silently — the exact shape this whole route exists to end.
    """
    job_id = uuid4()
    conn = _mock_conn(fetchrow=_job_row(job_id=job_id))
    app = _build_app(conn)
    async with await _client(app) as client:
        await client.post(f"/jobs/{job_id}/reparse")
    sql = " ".join(str(call.args[0]) for call in conn.execute.await_args_list)
    assert "failure_reason" in sql
    assert "NULL" in sql.upper()


@pytest.mark.asyncio
async def test_reparse_404s_when_the_job_does_not_exist() -> None:
    conn = _mock_conn(fetchrow=None)
    arq = MagicMock(enqueue_job=AsyncMock())
    app = _build_app(conn, arq=arq)
    async with await _client(app) as client:
        resp = await client.post(f"/jobs/{uuid4()}/reparse")
    assert resp.status_code == 404
    arq.enqueue_job.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["open", "closed", "archived"])
async def test_reparse_409s_once_the_job_has_left_draft(status: str) -> None:
    """``parse_job`` returns "stale" and drops the work for any job past 'draft'
    (``record_parsed`` only updates draft rows), so enqueueing one burns a queue
    slot and reports success for work that will never happen. 409 says so."""
    job_id = uuid4()
    conn = _mock_conn(fetchrow=_job_row(job_id=job_id, status=status))
    arq = MagicMock(enqueue_job=AsyncMock())
    app = _build_app(conn, arq=arq)
    async with await _client(app) as client:
        resp = await client.post(f"/jobs/{job_id}/reparse")
    assert resp.status_code == 409
    arq.enqueue_job.assert_not_awaited()


@pytest.mark.asyncio
async def test_reparse_recovers_a_silently_stranded_job_with_no_failure_text() -> None:
    """The worker-died shape: still 'draft', never parsed, and NOTHING on
    ``failure_reason`` to show for it. Gating this route on the failure text
    would leave exactly these rows unrecoverable."""
    job_id = uuid4()
    conn = _mock_conn(fetchrow=_job_row(job_id=job_id, failure_reason=None))
    arq = MagicMock(enqueue_job=AsyncMock())
    app = _build_app(conn, arq=arq)
    async with await _client(app) as client:
        resp = await client.post(f"/jobs/{job_id}/reparse")
    assert resp.status_code == 202
    arq.enqueue_job.assert_awaited_once_with("parse_job", str(job_id))


@pytest.mark.asyncio
async def test_reparse_as_recruiter_succeeds() -> None:
    """Admin is not the only writer — a recruiter owns this workflow."""
    job_id = uuid4()
    conn = _mock_conn(fetchrow=_job_row(job_id=job_id))
    app = _build_app(conn, role=Role.RECRUITER)
    async with await _client(app) as client:
        resp = await client.post(f"/jobs/{job_id}/reparse")
    assert resp.status_code == 202


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [Role.HIRING_MANAGER, Role.AUDITOR])
async def test_reparse_403s_for_non_writers_and_never_enqueues(role: Role) -> None:
    """Re-parse spends GPU time and rewrites a row — a write, gated like one."""
    job_id = uuid4()
    conn = _mock_conn(fetchrow=_job_row(job_id=job_id))
    arq = MagicMock(enqueue_job=AsyncMock())
    app = _build_app(conn, arq=arq, role=role)
    async with await _client(app) as client:
        resp = await client.post(f"/jobs/{job_id}/reparse")
    assert resp.status_code == 403
    arq.enqueue_job.assert_not_awaited()
