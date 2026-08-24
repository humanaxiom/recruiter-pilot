"""Route-level tests for Phase 6's reverse-match subresource routes —
``POST /resumes/{id}/match-jobs`` / ``GET /resumes/{id}/match-results`` on
``src.api.routes.resumes`` (new module; does not exist yet). The whole file
fails at collection (``ModuleNotFoundError``) — RED half of the TDD cycle.

Per the plan-of-record default: reverse-match is a subresource of
``routes/resumes.py`` (not a standalone ``routes/matching.py``); the read
route (``match-results``) applies NO redaction — the caller already owns the
résumé, so there is no blind-review boundary to enforce on this path (unlike
``GET /resumes/{id}`` itself).

**Ambiguities this file locks:**

* ``POST /resumes/{resume_id}/match-jobs`` checks the résumé EXISTS (a
  lightweight existence probe, not a full ``resume_service.get_one`` PII
  decrypt) BEFORE enqueueing anything — a nonexistent id returns **404**
  and ``arq.enqueue_job`` is never called. An existing résumé returns
  **202** and enqueues ``arq.enqueue_job("reverse_match_job",
  str(resume_id))``.
* ``GET /resumes/{resume_id}/match-results`` returns
  ``JobMatchResultOut`` (via ``shortlist_service.get_reverse_match_result``).
  A résumé that has NEVER been reverse-matched returns **200** with the
  EMPTY shape (``entries: []``) — NOT a 404. (A résumé id that doesn't exist
  at all is out of scope for this file's "never-matched" test — the route
  MAY 404 on a nonexistent résumé id here too, but that is not what "empty
  shape, not 404" pins: it pins that HAVING ZERO REVERSE-MATCH ROWS for a
  real résumé is not confused with "not found".)

**FU-4 (RBAC) — route→role table for this file:**

| Route                              | Method | Allowed roles |
|--------------------------------------|--------|-----------------|
| ``/resumes/{id}/match-jobs``         | POST   | admin, recruiter |
| ``/resumes/{id}/match-results``      | GET    | admin, recruiter |

``GET /resumes/{id}/match-results`` is **NOT all four**: hiring_manager and
auditor get 403 here even though they can read the blind résumé itself. This
route is narrower than the general résumé-read route, so it is NOT reasonable
to assume it inherits the "all four" pattern most GETs in this router follow.
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

from src.api.deps import Role, get_arq, resolve_role
from src.api.routes import resumes as resumes_routes
from src.errors import AppError
from src.models.pool import get_db

_NOW = dt.datetime(2026, 7, 16, tzinfo=dt.UTC)

_NON_MATCH_ROLES: tuple[Role, ...] = (Role.HIRING_MANAGER, Role.AUDITOR)


def _acm() -> MagicMock:
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=None)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _mock_conn(*, exists: bool, fetch: list[Any] | None = None) -> MagicMock:
    conn = MagicMock(name="conn")
    conn.fetchval = AsyncMock(return_value=(uuid4() if exists else None))
    conn.fetch = AsyncMock(return_value=fetch or [])
    conn.fetchrow = AsyncMock(return_value=None)
    conn.execute = AsyncMock(return_value="UPDATE 1")
    conn.transaction = MagicMock(return_value=_acm())
    return conn


def _build_app(
    conn: MagicMock, *, arq: MagicMock | None = None, role: Role = Role.ADMIN
) -> FastAPI:
    app = FastAPI()
    app.include_router(resumes_routes.router)

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


# ── POST /resumes/{id}/match-jobs ────────────────────────────────────────


@pytest.mark.asyncio
async def test_trigger_reverse_match_404_on_nonexistent_resume_before_enqueue() -> None:
    resume_id = uuid4()
    conn = _mock_conn(exists=False)
    arq = MagicMock(enqueue_job=AsyncMock())
    app = _build_app(conn, arq=arq)
    async with await _client(app) as client:
        resp = await client.post(f"/resumes/{resume_id}/match-jobs")
    assert resp.status_code == 404
    arq.enqueue_job.assert_not_awaited()


@pytest.mark.asyncio
async def test_trigger_reverse_match_202_and_enqueues_reverse_match_job() -> None:
    resume_id = uuid4()
    conn = _mock_conn(exists=True)
    arq = MagicMock(enqueue_job=AsyncMock())
    app = _build_app(conn, arq=arq)
    async with await _client(app) as client:
        resp = await client.post(f"/resumes/{resume_id}/match-jobs")
    assert resp.status_code == 202
    arq.enqueue_job.assert_awaited_once_with("reverse_match_job", str(resume_id))


# ── GET /resumes/{id}/match-results ──────────────────────────────────────


@pytest.mark.asyncio
async def test_get_match_results_empty_shape_for_never_matched_resume() -> None:
    resume_id = uuid4()
    conn = _mock_conn(exists=True, fetch=[])
    app = _build_app(conn)
    async with await _client(app) as client:
        resp = await client.get(f"/resumes/{resume_id}/match-results")
    assert resp.status_code == 200
    body = resp.json()
    assert body["entries"] == []
    assert body["resume_id"] == str(resume_id)


@pytest.mark.asyncio
async def test_get_match_results_returns_persisted_entries() -> None:
    import json

    resume_id = uuid4()
    job_id = uuid4()
    row = {
        "job_id": job_id,
        "title": "Staff Engineer",
        "department": "Platform",
        "rank": 1,
        "score_final": 0.9,
        "score_structured": 0.8,
        "score_evidence": 0.85,
        "score_breakdown": json.dumps(
            {
                "skill": 0.8,
                "experience": 0.6,
                "education": 0.4,
                "seniority": 0.5,
                "vector": 0.3,
                "structured": 0.55,
                "motivation": 0.0,
                "implied_experience": False,
                "skill_contributions": [],
            }
        ),
        "evidence": None,
        "requirement_count": 5,
        "must_have_count": 2,
        "pipeline_meta": json.dumps(
            {
                "model_gen": "g",
                "model_emb": "e",
                "prompt_versions": {},
                "weights": {},
                "git_sha": None,
                "generated_at": _NOW.isoformat(),
                "timings_ms": {},
            }
        ),
        "generated_at": _NOW,
    }
    conn = _mock_conn(exists=True, fetch=[row])
    app = _build_app(conn)
    async with await _client(app) as client:
        resp = await client.get(f"/resumes/{resume_id}/match-results")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["entries"]) == 1
    assert body["entries"][0]["job_id"] == str(job_id)
    assert body["entries"][0]["title"] == "Staff Engineer"


# ── FU-4 (RBAC): admin/recruiter ONLY on BOTH routes ─────────────────────


@pytest.mark.asyncio
async def test_trigger_reverse_match_as_recruiter_succeeds() -> None:
    resume_id = uuid4()
    conn = _mock_conn(exists=True)
    app = _build_app(conn, role=Role.RECRUITER)
    async with await _client(app) as client:
        resp = await client.post(f"/resumes/{resume_id}/match-jobs")
    assert resp.status_code == 202


@pytest.mark.parametrize("role", _NON_MATCH_ROLES)
@pytest.mark.asyncio
async def test_trigger_reverse_match_403s_for_hiring_manager_and_auditor(
    role: Role,
) -> None:
    resume_id = uuid4()
    conn = _mock_conn(exists=True)
    arq = MagicMock(enqueue_job=AsyncMock())
    app = _build_app(conn, arq=arq, role=role)
    async with await _client(app) as client:
        resp = await client.post(f"/resumes/{resume_id}/match-jobs")
    assert resp.status_code == 403
    arq.enqueue_job.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_match_results_as_recruiter_succeeds() -> None:
    resume_id = uuid4()
    conn = _mock_conn(exists=True, fetch=[])
    app = _build_app(conn, role=Role.RECRUITER)
    async with await _client(app) as client:
        resp = await client.get(f"/resumes/{resume_id}/match-results")
    assert resp.status_code == 200


@pytest.mark.parametrize("role", _NON_MATCH_ROLES)
@pytest.mark.asyncio
async def test_get_match_results_403s_for_hiring_manager_and_auditor(
    role: Role,
) -> None:
    """Narrower than the general "all four can GET" pattern elsewhere on this
    router — asserted explicitly per the route→role table rather than
    assumed from ``GET /resumes/{id}``'s own (wider) allowed set."""
    resume_id = uuid4()
    conn = _mock_conn(exists=True, fetch=[])
    app = _build_app(conn, role=role)
    async with await _client(app) as client:
        resp = await client.get(f"/resumes/{resume_id}/match-results")
    assert resp.status_code == 403
