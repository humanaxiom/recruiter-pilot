"""Integration tests — Phase 6's reverse-match routes
(``POST /resumes/{id}/match-jobs`` / ``GET /resumes/{id}/match-results``)
against a REAL Postgres (testcontainers) with a FAKE ``ArqRedis``.

``src.api.routes.resumes`` does not exist yet — every test below fails at
collection or the first request. RED half of the TDD cycle.

What a REAL Postgres proves that the mocked route tests
(``test_route_reverse_match.py``) cannot: the trigger route's existence
check is a REAL query against a REAL résumé row (not a hand-fed mock
return value), and a real ``persist_reverse_match``-written row round-trips
through ``GET .../match-results`` using the DEDICATED
``score_structured``/``score_evidence`` columns (ADR-010 §2 mirror-image,
the OPPOSITE of the shortlist fold — see ``test_services_match_read.py``).
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import asyncpg
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from testcontainers.postgres import PostgresContainer

from src.api.deps import Role, get_arq, resolve_role
from src.api.routes import resumes as resumes_routes
from src.errors import AppError
from src.models.ddl import init_schema
from src.models.pool import get_db
from src.pipeline.matching.orchestrator import JobMatchResult, JobMatchResultEntry
from src.schemas.matching import DEFAULT_WEIGHTS, PipelineMeta, ScoreBreakdown


@pytest.fixture(scope="session")
def pg_dsn() -> Iterator[str]:
    with PostgresContainer("postgres:16-alpine") as pg:
        yield re.sub(r"\+\w+", "", pg.get_connection_url(), count=1)


@pytest.fixture
async def pg_pool(pg_dsn: str) -> AsyncIterator[asyncpg.Pool]:
    pool = await asyncpg.create_pool(dsn=pg_dsn, min_size=2, max_size=8)
    await init_schema(pool)
    async with pool.acquire() as conn:
        await conn.execute("TRUNCATE jobs, resumes, outbox CASCADE")
    try:
        yield pool
    finally:
        await pool.close()


async def _insert_job(
    pool: asyncpg.Pool, *, title: str = "Senior Backend Engineer"
) -> UUID:
    async with pool.acquire() as conn:
        job_id: UUID = await conn.fetchval(
            "INSERT INTO jobs (title, description_raw) VALUES ($1, $2) RETURNING id",
            title,
            "raw jd text",
        )
    return job_id


async def _insert_resume(pool: asyncpg.Pool, job_id: UUID) -> UUID:
    async with pool.acquire() as conn:
        resume_id: UUID = await conn.fetchval(
            """
            INSERT INTO resumes (
                job_id, blob_key, original_filename, mime_type,
                file_size_bytes, sha256, consent_acknowledged, status
            ) VALUES ($1, $2, 'resume.pdf', 'application/pdf', 1024, $3, TRUE,
                       'parsed')
            RETURNING id
            """,
            job_id,
            f"resumes/{uuid4().hex}.pdf",
            uuid4().hex,
        )
    return resume_id


def _breakdown() -> ScoreBreakdown:
    return ScoreBreakdown(
        skill=0.8,
        experience=0.6,
        education=0.4,
        seniority=0.5,
        vector=0.3,
        structured=0.55,
    )


def _meta() -> PipelineMeta:
    return PipelineMeta(
        model_gen="test-gen",
        model_emb="test-emb",
        prompt_versions={},
        weights=DEFAULT_WEIGHTS,
        git_sha=None,
        generated_at=dt.datetime.now(dt.UTC),
        timings_ms={},
    )


def _build_app(pool: asyncpg.Pool, *, arq: MagicMock) -> FastAPI:
    app = FastAPI()
    app.include_router(resumes_routes.router)

    async def _get_db_override() -> AsyncIterator[Any]:
        async with pool.acquire() as conn:
            yield conn

    app.dependency_overrides[get_db] = _get_db_override
    app.dependency_overrides[get_arq] = lambda: arq
    app.dependency_overrides[resolve_role] = lambda: Role.ADMIN

    @app.exception_handler(AppError)
    async def _app_error_handler(_request: Any, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status, content={"detail": exc.message, "code": exc.code}
        )

    return app


async def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_trigger_reverse_match_404_for_a_real_missing_resume(
    pg_pool: asyncpg.Pool,
) -> None:
    arq = MagicMock(enqueue_job=AsyncMock())
    app = _build_app(pg_pool, arq=arq)
    async with await _client(app) as client:
        resp = await client.post(f"/resumes/{uuid4()}/match-jobs")
    assert resp.status_code == 404
    arq.enqueue_job.assert_not_awaited()


@pytest.mark.asyncio
async def test_trigger_reverse_match_202_for_a_real_resume(
    pg_pool: asyncpg.Pool,
) -> None:
    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume(pg_pool, job_id)
    arq = MagicMock(enqueue_job=AsyncMock())
    app = _build_app(pg_pool, arq=arq)
    async with await _client(app) as client:
        resp = await client.post(f"/resumes/{resume_id}/match-jobs")
    assert resp.status_code == 202
    arq.enqueue_job.assert_awaited_once_with("reverse_match_job", str(resume_id))


@pytest.mark.asyncio
async def test_match_results_round_trips_a_real_persist_reverse_match_row(
    pg_pool: asyncpg.Pool,
) -> None:
    from src.services.shortlist_service import persist_reverse_match

    job_id = await _insert_job(pg_pool, title="Staff ML Engineer")
    resume_id = await _insert_resume(pg_pool, job_id)
    result = JobMatchResult(
        resume_id=resume_id,
        entries=[
            JobMatchResultEntry(
                job_id=job_id,
                title="Staff ML Engineer",
                rank=1,
                score_final=0.93,
                score_structured=0.81,
                score_evidence=0.76,
                breakdown=_breakdown(),
                evidence=None,
                requirement_count=6,
                must_have_count=3,
            )
        ],
        pipeline_meta=_meta(),
    )
    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await persist_reverse_match(conn, result)

    arq = MagicMock(enqueue_job=AsyncMock())
    app = _build_app(pg_pool, arq=arq)
    async with await _client(app) as client:
        resp = await client.get(f"/resumes/{resume_id}/match-results")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["entries"]) == 1
    entry = body["entries"][0]
    assert entry["job_id"] == str(job_id)
    assert entry["title"] == "Staff ML Engineer"
    assert entry["score_structured"] == pytest.approx(0.81)
    assert entry["score_evidence"] == pytest.approx(0.76)


@pytest.mark.asyncio
async def test_match_results_empty_shape_for_a_real_never_matched_resume(
    pg_pool: asyncpg.Pool,
) -> None:
    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume(pg_pool, job_id)
    arq = MagicMock(enqueue_job=AsyncMock())
    app = _build_app(pg_pool, arq=arq)
    async with await _client(app) as client:
        resp = await client.get(f"/resumes/{resume_id}/match-results")
    assert resp.status_code == 200
    assert resp.json()["entries"] == []
