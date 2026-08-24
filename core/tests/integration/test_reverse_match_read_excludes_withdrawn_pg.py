"""Integration tests — RED for the reverse-match READ residual (branch
``fix/reverse-match-read-excludes-withdrawn``): a withdrawn candidate
(FU-8, ``resumes.withdrawn_at IS NOT NULL``) still has its persisted
reverse-match rows returned because ``src.services.shortlist_service``'s
reverse-match read query (``_REVERSE_MATCH_QUERY``, consumed by
``get_reverse_match_result``) does not filter on ``withdrawn_at``.

PR #43 closed exactly this residual for the four *shortlist* reads
(``_LIST_QUERY`` / ``_GET_QUERY`` / ``_BLIND_LIST_QUERY`` / ``_BLIND_GET_QUERY``
+ the export) but left the reverse-match persisted read (candidate -> jobs)
out of scope. The write path (``reverse_match_job``) already skips a withdrawn
résumé and persists nothing — but rows written BEFORE withdrawal survive
untouched, and only the READ is supposed to hide them.

The reverse-match read is keyed on a SINGLE ``resume_id`` (one candidate,
ranked against many jobs), so withdrawing that candidate must hide the WHOLE
result — the empty shape, mirroring ``get_reverse_match_result``'s existing
"never reverse-matched" contract. Reinstatement restores the same persisted
rows (no regenerate).

Against a REAL Postgres (testcontainers) because the defect is entirely in the
SQL text — a mocked-conn unit test can only prove the query STRING
contains/doesn't-contain a WHERE fragment; it cannot prove the clause actually
filters real joined ``reverse_match_entries`` / ``jobs`` / ``resumes`` rows.

Every test below is expected to FAIL today: the withdrawn candidate's rows are
still returned. RED half of the TDD cycle — no implementation change here.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from collections.abc import AsyncIterator, Iterator
from uuid import UUID

import asyncpg
import pytest
from testcontainers.postgres import PostgresContainer

from src.models.ddl import init_schema
from src.pipeline.matching.orchestrator import JobMatchResult, JobMatchResultEntry
from src.schemas.matching import DEFAULT_WEIGHTS, PipelineMeta, ScoreBreakdown
from src.services.shortlist_service import (
    get_reverse_match_result,
    persist_reverse_match,
)

_JD = "We need a senior backend engineer. " * 3


@pytest.fixture(scope="session")
def pg_dsn() -> Iterator[str]:
    with PostgresContainer("postgres:16-alpine") as pg:
        yield re.sub(r"\+\w+", "", pg.get_connection_url(), count=1)


@pytest.fixture
async def pg_pool(pg_dsn: str) -> AsyncIterator[asyncpg.Pool]:
    pool = await asyncpg.create_pool(dsn=pg_dsn, min_size=2, max_size=8)
    await init_schema(pool)
    async with pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE job_assignees, users, jobs, resumes, "
            "reverse_match_entries, outbox CASCADE"
        )
    try:
        yield pool
    finally:
        await pool.close()


# ── seeding helpers ─────────────────────────────────────────────────────────


async def _insert_job(pool: asyncpg.Pool, *, title: str) -> UUID:
    """A parsed job — the reverse-match read JOINs ``jobs`` for title/dept."""
    async with pool.acquire() as conn:
        job_id: UUID = await conn.fetchval(
            "INSERT INTO jobs (title, department, description_raw, blind_review) "
            "VALUES ($1, 'Engineering', $2, TRUE) RETURNING id",
            title,
            _JD,
        )
    return job_id


async def _insert_resume(
    pool: asyncpg.Pool, *, owning_job_id: UUID, name: str = "Real Candidate"
) -> UUID:
    """A résumé row (unencrypted-PII columns left NULL — no test here asserts on
    decrypted identity, only on which rows the read layer returns). ``resumes.
    job_id`` is ``NOT NULL`` (the job the candidate applied to), independent of
    the OTHER jobs reverse-match ranks them against via
    ``reverse_match_entries.job_id``."""
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
            owning_job_id,
            f"resumes/{uuid.uuid4().hex}.pdf",
            uuid.uuid4().hex,
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
        prompt_versions={"shortlist_evidence": "shortlist_evidence_v1"},
        weights=DEFAULT_WEIGHTS,
        git_sha="abc123",
        generated_at=dt.datetime.now(dt.UTC),
        timings_ms={},
    )


async def _seed_reverse_match(
    pool: asyncpg.Pool, *, resume_id: UUID, jobs_ranked: list[tuple[UUID, str]]
) -> None:
    """Persist one ranked reverse-match row per job in ``jobs_ranked``
    (rank = 1-based position) via ONE real ``persist_reverse_match`` call —
    DELETE-first per ``resume_id``, so all rows for one candidate must be
    seeded in a single call."""
    entries = [
        JobMatchResultEntry(
            job_id=job_id,
            title=title,
            rank=rank,
            score_final=0.9,
            score_structured=0.8,
            score_evidence=0.7,
            breakdown=_breakdown(),
            evidence=None,
            requirement_count=5,
            must_have_count=2,
        )
        for rank, (job_id, title) in enumerate(jobs_ranked, start=1)
    ]
    result = JobMatchResult(resume_id=resume_id, entries=entries, pipeline_meta=_meta())
    async with pool.acquire() as conn:
        async with conn.transaction():
            await persist_reverse_match(conn, result)


async def _withdraw(pool: asyncpg.Pool, resume_id: UUID) -> None:
    """Flip ``withdrawn_at`` directly via SQL — a pure read-layer probe. The
    withdraw/reinstate FLIP itself (audit_log + outbox side effects) is already
    pinned in ``test_resume_withdraw_pg.py``; these tests exercise ONLY the
    reverse-match read filter."""
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE resumes SET withdrawn_at = now(), "
            "withdrawal_reason = 'test withdrawal' "
            "WHERE id = $1 AND withdrawn_at IS NULL",
            resume_id,
        )
    assert result.endswith(" 1"), "the seeded résumé must not already be withdrawn"


async def _reinstate(pool: asyncpg.Pool, resume_id: UUID) -> None:
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE resumes SET withdrawn_at = NULL, withdrawal_reason = NULL "
            "WHERE id = $1 AND withdrawn_at IS NOT NULL",
            resume_id,
        )
    assert result.endswith(" 1"), "the seeded résumé must have been withdrawn first"


# ── 1. a withdrawn candidate's reverse-match read is empty ───────────────────


@pytest.mark.asyncio
async def test_reverse_match_read_empty_for_withdrawn_candidate(
    pg_pool: asyncpg.Pool,
) -> None:
    job_a = await _insert_job(pg_pool, title="Senior Backend Engineer")
    job_b = await _insert_job(pg_pool, title="Staff Platform Engineer")
    resume_id = await _insert_resume(
        pg_pool, owning_job_id=job_a, name="Withdrawn Candidate Zyzzyva"
    )
    await _seed_reverse_match(
        pg_pool,
        resume_id=resume_id,
        jobs_ranked=[(job_a, "Senior Backend Engineer"), (job_b, "Staff Platform")],
    )

    async with pg_pool.acquire() as conn:
        before = await get_reverse_match_result(conn, resume_id)
    assert len(before.entries) == 2, "sanity: rows are visible before withdrawal"

    await _withdraw(pg_pool, resume_id)

    async with pg_pool.acquire() as conn:
        after = await get_reverse_match_result(conn, resume_id)

    assert after.entries == [], (
        "a withdrawn candidate must not have its persisted reverse-match rows "
        "returned by get_reverse_match_result"
    )
    assert after.pipeline_meta is None
    assert after.generated_at is None
    assert after.resume_id == resume_id


# ── 2. reinstatement restores the SAME persisted rows (no regenerate) ────────


@pytest.mark.asyncio
async def test_reinstate_restores_reverse_match_from_same_rows(
    pg_pool: asyncpg.Pool,
) -> None:
    job_a = await _insert_job(pg_pool, title="Senior Backend Engineer")
    resume_id = await _insert_resume(pg_pool, owning_job_id=job_a)
    await _seed_reverse_match(
        pg_pool,
        resume_id=resume_id,
        jobs_ranked=[(job_a, "Senior Backend Engineer")],
    )

    await _withdraw(pg_pool, resume_id)
    async with pg_pool.acquire() as conn:
        while_withdrawn = await get_reverse_match_result(conn, resume_id)
    assert while_withdrawn.entries == []

    await _reinstate(pg_pool, resume_id)
    async with pg_pool.acquire() as conn:
        after = await get_reverse_match_result(conn, resume_id)

    assert len(after.entries) == 1, (
        "reinstatement must surface the SAME persisted reverse_match_entries "
        "rows -- no regenerate"
    )
    assert after.entries[0].job_id == job_a


# ── 3. the filter is correlated on THIS candidate, not a blanket filter ──────


@pytest.mark.asyncio
async def test_reverse_match_of_another_candidate_is_unaffected(
    pg_pool: asyncpg.Pool,
) -> None:
    """Withdrawing candidate A must not hide candidate B's reverse-match rows —
    proves the NOT-EXISTS guard is correlated on the read's own ``resume_id``,
    not a blanket predicate that empties every reverse-match read."""
    job_a = await _insert_job(pg_pool, title="Senior Backend Engineer")
    withdrawn = await _insert_resume(pg_pool, owning_job_id=job_a, name="Withdrawn A")
    active = await _insert_resume(pg_pool, owning_job_id=job_a, name="Active B")
    await _seed_reverse_match(
        pg_pool, resume_id=withdrawn, jobs_ranked=[(job_a, "Senior Backend Engineer")]
    )
    await _seed_reverse_match(
        pg_pool, resume_id=active, jobs_ranked=[(job_a, "Senior Backend Engineer")]
    )

    await _withdraw(pg_pool, withdrawn)

    async with pg_pool.acquire() as conn:
        withdrawn_result = await get_reverse_match_result(conn, withdrawn)
        active_result = await get_reverse_match_result(conn, active)

    assert withdrawn_result.entries == []
    assert len(active_result.entries) == 1, (
        "an active candidate's reverse-match read must be untouched when a "
        "DIFFERENT candidate is withdrawn"
    )
    assert active_result.entries[0].job_id == job_a
