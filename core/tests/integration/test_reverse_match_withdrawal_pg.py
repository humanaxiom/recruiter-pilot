"""Integration test — ``src.worker.matching_tasks.reverse_match_job``
excludes a withdrawn résumé, against a REAL Postgres (testcontainers).

ADR-026 decision 3: reverse-match already filters ``status == 'parsed'`` in
Postgres; this extends that same pure-Postgres check to also require
``withdrawn_at IS NULL``. What a REAL Postgres proves that the mocked-conn
unit tests (``test_worker_reverse_match_job.py``) structurally cannot: a
withdrawn résumé's reverse-match run writes ZERO rows into
``reverse_match_entries`` — a real row-count query, not a mocked
``persist_reverse_match`` call-count assertion. A mutant that returns the
string ``"withdrawn"`` but still lets a stray write slip through (e.g. a
previous run's rows never cleared, or a code path that persists before the
withdrawn check) fails this test where a call-count-only assertion would not.

Neo4j is a bare ``MagicMock`` here — the orchestrator (``match_resume_to_jobs``)
is patched out entirely (a spy), so no real graph query ever needs to run;
this test's job is purely the Postgres-side exclusion + persistence guarantee.

``src.worker.matching_tasks.reverse_match_job`` does not check
``withdrawn_at`` yet — every test below is expected to fail. RED half of the
TDD cycle.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import asyncpg
import pytest
from testcontainers.postgres import PostgresContainer

from src.models.ddl import init_schema
from src.pipeline.matching.orchestrator import JobMatchResult, JobMatchResultEntry
from src.schemas.matching import ScoreBreakdown


@pytest.fixture(scope="session")
def pg_dsn() -> Iterator[str]:
    with PostgresContainer("postgres:16-alpine") as pg:
        yield re.sub(r"\+\w+", "", pg.get_connection_url(), count=1)


@pytest.fixture
async def pg_pool(pg_dsn: str) -> AsyncIterator[asyncpg.Pool]:
    pool = await asyncpg.create_pool(dsn=pg_dsn, min_size=2, max_size=8)
    await init_schema(pool)
    async with pool.acquire() as conn:
        await conn.execute("TRUNCATE jobs, resumes, outbox, audit_log CASCADE")
    try:
        yield pool
    finally:
        await pool.close()


async def _insert_job(pool: asyncpg.Pool) -> UUID:
    async with pool.acquire() as conn:
        job_id: UUID = await conn.fetchval(
            "INSERT INTO jobs (title, description_raw) VALUES ($1, $2) RETURNING id",
            "Senior Backend Engineer",
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
            f"resumes/{uuid.uuid4().hex}.pdf",
            uuid.uuid4().hex,
        )
    return resume_id


def _ctx(pool: asyncpg.Pool) -> dict[str, Any]:
    return {
        "pg_pool": pool,
        "neo4j": MagicMock(name="neo4j_driver"),
        "llm": MagicMock(name="llm"),
        "embedder": MagicMock(name="embedder"),
    }


def _breakdown() -> ScoreBreakdown:
    return ScoreBreakdown(
        skill=0.8,
        experience=0.6,
        education=0.4,
        seniority=0.5,
        vector=0.3,
        structured=0.55,
    )


async def _reverse_match_entry_count(pool: asyncpg.Pool, resume_id: UUID) -> int:
    async with pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT count(*) FROM reverse_match_entries WHERE resume_id = $1",
            resume_id,
        )
    return int(count)


@pytest.mark.asyncio
async def test_withdrawn_resume_returns_withdrawn_and_writes_zero_entries(
    pg_pool: asyncpg.Pool,
) -> None:
    from src.worker.matching_tasks import reverse_match_job

    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume(pg_pool, job_id)
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "UPDATE resumes SET withdrawn_at = now() WHERE id = $1", resume_id
        )

    ctx = _ctx(pg_pool)
    with (
        patch(
            "src.worker.matching_tasks.match_resume_to_jobs", new_callable=AsyncMock
        ) as match_jobs,
        patch(
            "src.worker.matching_tasks.persist_reverse_match", new_callable=AsyncMock
        ) as persist,
    ):
        result = await reverse_match_job(ctx, str(resume_id))

    assert result == "withdrawn"
    match_jobs.assert_not_called()
    persist.assert_not_called()

    assert await _reverse_match_entry_count(pg_pool, resume_id) == 0


@pytest.mark.asyncio
async def test_withdrawn_resume_leaves_a_prior_run_result_undisturbed(
    pg_pool: asyncpg.Pool,
) -> None:
    """A résumé withdrawn AFTER an earlier successful reverse-match run keeps
    its persisted history (ADR-026's accepted residual: historical results
    are not retroactively scrubbed) — the row count must be UNCHANGED by the
    withdrawn run, not zeroed out."""
    from src.services.shortlist_service import persist_reverse_match
    from src.worker.matching_tasks import reverse_match_job

    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume(pg_pool, job_id)

    prior_result = JobMatchResult(
        resume_id=resume_id,
        entries=[
            JobMatchResultEntry(
                job_id=job_id,
                title="Senior Backend Engineer",
                rank=1,
                score_final=0.8,
                score_structured=0.7,
                score_evidence=0.6,
                breakdown=_breakdown(),
                evidence=None,
                requirement_count=4,
                must_have_count=2,
            )
        ],
    )
    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await persist_reverse_match(conn, prior_result)

    assert await _reverse_match_entry_count(pg_pool, resume_id) == 1

    async with pg_pool.acquire() as conn:
        await conn.execute(
            "UPDATE resumes SET withdrawn_at = now() WHERE id = $1", resume_id
        )

    ctx = _ctx(pg_pool)
    with patch(
        "src.worker.matching_tasks.match_resume_to_jobs", new_callable=AsyncMock
    ) as match_jobs:
        result = await reverse_match_job(ctx, str(resume_id))

    assert result == "withdrawn"
    match_jobs.assert_not_called()
    # Unchanged — still exactly the one prior row, never zeroed nor added to.
    assert await _reverse_match_entry_count(pg_pool, resume_id) == 1


@pytest.mark.asyncio
async def test_regression_twin_non_withdrawn_parsed_resume_reaches_the_orchestrator(
    pg_pool: asyncpg.Pool,
) -> None:
    """The pairing test: an ordinary parsed, non-withdrawn résumé must still
    reach the orchestrator — a mutant that excludes EVERY résumé (not just
    withdrawn ones) would pass the withdrawn tests above but fail this one."""
    from src.worker.matching_tasks import reverse_match_job

    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume(pg_pool, job_id)

    ctx = _ctx(pg_pool)
    with patch(
        "src.worker.matching_tasks.match_resume_to_jobs",
        new_callable=AsyncMock,
        return_value=JobMatchResult(resume_id=resume_id, entries=[]),
    ) as match_jobs:
        result = await reverse_match_job(ctx, str(resume_id))

    assert result != "withdrawn"
    match_jobs.assert_awaited_once()
