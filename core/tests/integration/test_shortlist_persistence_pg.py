"""Integration tests — Phase 4d's write-only persistence
(``src.services.shortlist_service.persist_shortlist`` /
``persist_reverse_match``) and the two worker tasks
(``src.worker.matching_tasks.shortlist_job`` / ``reverse_match_job``) against
a REAL Postgres (testcontainers), mirroring
``test_matching_orchestrator.py``'s ``pg_dsn``/``pg_pool`` fixtures.

Neither ``src.services.shortlist_service`` nor ``src.worker.matching_tasks``
exist yet — every test below fails at collection time with
``ModuleNotFoundError``. RED half of the TDD cycle.

What a REAL Postgres proves that the mocked-conn unit tests
(``test_services_shortlist_persist.py``) cannot:

* the DELETE-first contract actually satisfies the real
  ``UNIQUE (job_id, resume_id)`` / ``UNIQUE (resume_id, job_id)`` indexes on
  a RERUN — a persist function that inserts without deleting first would
  raise ``asyncpg.exceptions.UniqueViolationError`` on the second run against
  the same (job_id, resume_id) pair, not merely "not delete stale rows".
* ``evidence=None`` -> ``{}`` on the shortlist path actually satisfies the
  real ``evidence JSONB NOT NULL`` constraint — writing raw SQL NULL there
  would raise ``asyncpg.exceptions.NotNullViolationError``.
* Requirement 1 (ADR-009 carried obligation), proven through a REAL round
  trip: a worker task invoked against a NON-default ``Settings`` persists a
  ``pipeline_meta`` whose ``weights`` match ``weights_from_settings(settings)``
  when read back from the table — not merely captured by a mock.
* "Why this rank?" defense pack, slice 1 (added below): the READ side
  (``get_one``) must surface ``score_structured``/``score_evidence``/
  ``pipeline_meta.weights`` back onto the DTO after a REAL asyncpg jsonb
  round trip — floats coming back out of a real jsonb codec, not merely
  captured by a mock connection, which is what the unit suite can prove.
"""

from __future__ import annotations

import datetime as dt
import json
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
from src.pipeline.matching.orchestrator import (
    JobMatchResult,
    JobMatchResultEntry,
    ShortlistResult,
    ShortlistResultEntry,
)
from src.schemas.matching import (
    DEFAULT_WEIGHTS,
    MatchWeights,
    PipelineMeta,
    ScoreBreakdown,
)
from src.settings import Settings, weights_from_settings

# ── fixtures ─────────────────────────────────────────────────────────────


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


# ── seeding helpers ──────────────────────────────────────────────────────


async def _insert_job(
    pool: asyncpg.Pool,
    *,
    description_parsed: dict[str, Any] | None = None,
    blind_review: bool | None = None,
) -> UUID:
    """``blind_review=None`` leaves the column at its table DEFAULT (TRUE), so
    every pre-existing caller is byte-unchanged. Pass it explicitly to reach
    the NON-blind read path (``_row_to_entry``), which the default hides."""
    async with pool.acquire() as conn:
        if blind_review is None:
            job_id: UUID = await conn.fetchval(
                "INSERT INTO jobs (title, description_raw, description_parsed) "
                "VALUES ($1, $2, $3::jsonb) RETURNING id",
                "Senior Backend Engineer",
                "raw jd text (irrelevant to these tests)",
                (
                    json.dumps(description_parsed)
                    if description_parsed is not None
                    else None
                ),
            )
            return job_id
        job_id = await conn.fetchval(
            "INSERT INTO jobs (title, description_raw, description_parsed, "
            "blind_review) VALUES ($1, $2, $3::jsonb, $4) RETURNING id",
            "Senior Backend Engineer",
            "raw jd text (irrelevant to these tests)",
            json.dumps(description_parsed) if description_parsed is not None else None,
            blind_review,
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


def _breakdown() -> ScoreBreakdown:
    return ScoreBreakdown(
        skill=0.8,
        experience=0.6,
        education=0.4,
        seniority=0.5,
        vector=0.3,
        structured=0.55,
    )


def _meta(
    weights: MatchWeights = DEFAULT_WEIGHTS, git_sha: str | None = None
) -> PipelineMeta:
    return PipelineMeta(
        model_gen="test-gen",
        model_emb="test-emb",
        prompt_versions={"shortlist_evidence": "shortlist_evidence_v1"},
        weights=weights,
        git_sha=git_sha,
        generated_at=dt.datetime.now(dt.UTC),
        timings_ms={},
    )


def _shortlist_entry(
    resume_id: UUID, *, rank: int, evidence: Any = None
) -> ShortlistResultEntry:
    return ShortlistResultEntry(
        resume_id=resume_id,
        rank=rank,
        score_final=0.9,
        score_structured=0.8,
        score_evidence=0.7,
        breakdown=_breakdown(),
        evidence=evidence,
    )


def _job_match_entry(job_id: UUID, *, rank: int) -> JobMatchResultEntry:
    return JobMatchResultEntry(
        job_id=job_id,
        title="Some Job",
        rank=rank,
        score_final=0.9,
        score_structured=0.8,
        score_evidence=0.7,
        breakdown=_breakdown(),
        evidence=None,
        requirement_count=5,
        must_have_count=3,
    )


# ── persist_shortlist: rerun-replaces against the real UNIQUE constraint ───


@pytest.mark.asyncio
async def test_persist_shortlist_rerun_replaces_prior_run(
    pg_pool: asyncpg.Pool,
) -> None:
    from src.services.shortlist_service import persist_shortlist

    job_id = await _insert_job(pg_pool)
    resume1 = await _insert_resume(pg_pool, job_id)
    resume2 = await _insert_resume(pg_pool, job_id)
    resume3 = await _insert_resume(pg_pool, job_id)

    async with pg_pool.acquire() as conn:
        await persist_shortlist(
            conn,
            ShortlistResult(
                job_id=job_id,
                entries=[
                    _shortlist_entry(resume1, rank=1),
                    _shortlist_entry(resume2, rank=2),
                ],
                pipeline_meta=_meta(),
            ),
        )

    async with pg_pool.acquire() as conn:
        await persist_shortlist(
            conn,
            ShortlistResult(
                job_id=job_id,
                entries=[_shortlist_entry(resume3, rank=1)],
                pipeline_meta=_meta(),
            ),
        )

    async with pg_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT resume_id FROM shortlist_entries WHERE job_id = $1", job_id
        )
    assert {r["resume_id"] for r in rows} == {resume3}


@pytest.mark.asyncio
async def test_persist_shortlist_rerun_same_resume_new_rank_ok(
    pg_pool: asyncpg.Pool,
) -> None:
    """Without a DELETE-first, re-inserting the SAME (job_id, resume_id) pair
    on a rerun would raise ``asyncpg.exceptions.UniqueViolationError`` against
    the real ``UNIQUE (job_id, resume_id)`` index."""
    from src.services.shortlist_service import persist_shortlist

    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume(pg_pool, job_id)

    async with pg_pool.acquire() as conn:
        await persist_shortlist(
            conn,
            ShortlistResult(
                job_id=job_id,
                entries=[_shortlist_entry(resume_id, rank=1)],
                pipeline_meta=_meta(),
            ),
        )
        await persist_shortlist(
            conn,
            ShortlistResult(
                job_id=job_id,
                entries=[_shortlist_entry(resume_id, rank=1)],
                pipeline_meta=_meta(),
            ),
        )

    async with pg_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT rank FROM shortlist_entries WHERE job_id = $1", job_id
        )
    assert [r["rank"] for r in rows] == [1]


@pytest.mark.asyncio
async def test_persist_shortlist_none_evidence_satisfies_not_null_constraint(
    pg_pool: asyncpg.Pool,
) -> None:
    """``shortlist_entries.evidence`` is ``JSONB NOT NULL`` — writing raw SQL
    NULL would raise ``asyncpg.exceptions.NotNullViolationError``. This proves
    the ``evidence=None`` -> ``{}`` coercion against the real constraint."""
    from src.services.shortlist_service import persist_shortlist

    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume(pg_pool, job_id)

    async with pg_pool.acquire() as conn:
        await persist_shortlist(
            conn,
            ShortlistResult(
                job_id=job_id,
                entries=[_shortlist_entry(resume_id, rank=1, evidence=None)],
                pipeline_meta=_meta(),
            ),
        )

    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT evidence FROM shortlist_entries WHERE job_id = $1", job_id
        )
    assert row is not None
    evidence = row["evidence"]
    if isinstance(evidence, str):
        evidence = json.loads(evidence)
    assert evidence == {}


# ── persist_reverse_match: rerun-replaces keyed on resume_id ───────────────


@pytest.mark.asyncio
async def test_persist_reverse_match_rerun_replaces_prior_run(
    pg_pool: asyncpg.Pool,
) -> None:
    from src.services.shortlist_service import persist_reverse_match

    job1 = await _insert_job(pg_pool)
    job2 = await _insert_job(pg_pool)
    job3 = await _insert_job(pg_pool)
    resume_id = await _insert_resume(pg_pool, job1)

    async with pg_pool.acquire() as conn:
        await persist_reverse_match(
            conn,
            JobMatchResult(
                resume_id=resume_id,
                entries=[
                    _job_match_entry(job1, rank=1),
                    _job_match_entry(job2, rank=2),
                ],
                pipeline_meta=_meta(),
            ),
        )

    async with pg_pool.acquire() as conn:
        await persist_reverse_match(
            conn,
            JobMatchResult(
                resume_id=resume_id,
                entries=[_job_match_entry(job3, rank=1)],
                pipeline_meta=_meta(),
            ),
        )

    async with pg_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT job_id FROM reverse_match_entries WHERE resume_id = $1", resume_id
        )
    assert {r["job_id"] for r in rows} == {job3}


# ── Requirement 1 end-to-end: real worker task -> real persist -> real ────
#    SELECT-back proves pipeline_meta.weights came from Settings ───────────


@pytest.mark.asyncio
async def test_shortlist_job_end_to_end_persists_pipeline_meta_weights_from_settings(
    pg_pool: asyncpg.Pool,
) -> None:
    from src.worker.matching_tasks import shortlist_job

    job_id = await _insert_job(pg_pool, description_parsed={"required_skills": []})
    resume_id = await _insert_resume(pg_pool, job_id)

    custom_settings = Settings(
        match_skill=0.5,
        match_experience=0.15,
        match_education=0.10,
        match_seniority=0.15,
        match_vector=0.10,
    )
    expected_weights = weights_from_settings(custom_settings)
    assert expected_weights != DEFAULT_WEIGHTS

    canned_result = ShortlistResult(
        job_id=job_id,
        entries=[_shortlist_entry(resume_id, rank=1)],
        pipeline_meta=_meta(weights=expected_weights, git_sha=None),
    )

    ctx: dict[str, Any] = {
        "pg_pool": pg_pool,
        "neo4j": MagicMock(),
        "llm": MagicMock(),
        "embedder": MagicMock(),
    }

    with (
        patch("src.worker.matching_tasks.get_settings", return_value=custom_settings),
        patch(
            "src.worker.matching_tasks.generate_shortlist",
            new_callable=AsyncMock,
            return_value=canned_result,
        ),
    ):
        result = await shortlist_job(ctx, str(job_id))

    assert result == "persisted"

    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT pipeline_meta FROM shortlist_entries WHERE job_id = $1", job_id
        )
    assert row is not None
    meta = row["pipeline_meta"]
    if isinstance(meta, str):
        meta = json.loads(meta)
    assert meta["weights"] == json.loads(expected_weights.model_dump_json())
    assert meta["weights"]["skill"] == pytest.approx(0.5)
    assert meta["weights"]["experience"] == pytest.approx(0.15)


@pytest.mark.asyncio
async def test_reverse_match_job_e2e_persists_pipeline_meta_weights(
    pg_pool: asyncpg.Pool,
) -> None:
    from src.worker.matching_tasks import reverse_match_job

    job_id = await _insert_job(pg_pool, description_parsed={"required_skills": []})
    resume_id = await _insert_resume(pg_pool, job_id)

    custom_settings = Settings(match_reverse_evidence_k=3)
    expected_weights = weights_from_settings(custom_settings)

    canned_result = JobMatchResult(
        resume_id=resume_id,
        entries=[_job_match_entry(job_id, rank=1)],
        pipeline_meta=_meta(weights=expected_weights, git_sha="rev-sha-1"),
    )

    ctx: dict[str, Any] = {
        "pg_pool": pg_pool,
        "neo4j": MagicMock(),
        "llm": MagicMock(),
        "embedder": MagicMock(),
    }

    with (
        patch("src.worker.matching_tasks.get_settings", return_value=custom_settings),
        patch(
            "src.worker.matching_tasks.match_resume_to_jobs",
            new_callable=AsyncMock,
            return_value=canned_result,
        ),
    ):
        result = await reverse_match_job(ctx, str(resume_id))

    assert result == "persisted"

    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT pipeline_meta FROM reverse_match_entries WHERE resume_id = $1",
            resume_id,
        )
    assert row is not None
    meta = row["pipeline_meta"]
    if isinstance(meta, str):
        meta = json.loads(meta)
    assert meta["git_sha"] == "rev-sha-1"
    assert meta["weights"] == json.loads(expected_weights.model_dump_json())


# ── "Why this rank?" defense pack, slice 1 — score_structured/score_evidence/
#    pipeline_meta round-trip through a REAL Postgres jsonb column ─────────


@pytest.mark.parametrize("blind_review", [True, False])
@pytest.mark.asyncio
async def test_persist_then_get_one_round_trips_structured_evidence_and_weights(
    pg_pool: asyncpg.Pool, blind_review: bool
) -> None:
    """``persist_shortlist`` folds ``score_structured``/``score_evidence``
    into the ``score_breakdown`` jsonb at write time (ADR-010 §2);
    ``get_one`` must surface them back onto the DTO — and
    ``pipeline_meta.weights`` (the GENERATION-TIME weights, not whatever
    Settings default to today) must round-trip byte-for-byte through
    asyncpg's real jsonb float codec. The mocked-conn unit suite
    (``test_services_shortlist_read.py``) hands python floats straight
    through a fake connection and so cannot prove this; only a real
    Postgres round trip can.

    PARAMETRIZED over ``jobs.blind_review`` because ``get_one`` forks into two
    ENTIRELY SEPARATE row-to-DTO functions on it (``_row_to_blind_entry`` vs
    ``_row_to_entry``), and the column DEFAULTS to TRUE — so the unparametrized
    version of this test exercised only the blind branch and left the
    non-blind unfold with no real-Postgres coverage at all."""
    from src.services.shortlist_service import get_one, persist_shortlist

    job_id = await _insert_job(pg_pool, blind_review=blind_review)
    resume_id = await _insert_resume(pg_pool, job_id)
    custom_weights = MatchWeights(
        structured=0.5,
        evidence=0.4,
        motivation=0.1,
        skill=0.55,
        experience=0.15,
        education=0.10,
        seniority=0.10,
        vector=0.10,
    )
    assert custom_weights.structured != DEFAULT_WEIGHTS.structured
    assert custom_weights.skill != DEFAULT_WEIGHTS.skill

    async with pg_pool.acquire() as conn:
        await persist_shortlist(
            conn,
            ShortlistResult(
                job_id=job_id,
                entries=[_shortlist_entry(resume_id, rank=1)],
                pipeline_meta=_meta(weights=custom_weights, git_sha="abc123def"),
            ),
        )

    async with pg_pool.acquire() as conn:
        entry_id = await conn.fetchval(
            "SELECT id FROM shortlist_entries WHERE job_id = $1", job_id
        )
        entry = await get_one(conn, entry_id)

    # The two row-to-DTO paths really were exercised separately.
    assert entry.blinded is blind_review
    if blind_review:
        assert entry.display_label == "Candidate A"
    else:
        assert entry.display_label is None

    # score_structured=0.8 / score_evidence=0.7 come from _shortlist_entry().
    assert entry.score_structured == pytest.approx(0.8)
    assert entry.score_evidence == pytest.approx(0.7)
    assert entry.pipeline_meta is not None
    assert entry.pipeline_meta.weights.structured == pytest.approx(0.5)
    assert entry.pipeline_meta.weights.skill == pytest.approx(0.55)
    # The honesty guard, proven against a real round trip: the weights that
    # come back are NOT today's defaults.
    assert entry.pipeline_meta.weights.structured != pytest.approx(
        DEFAULT_WEIGHTS.structured
    )
    assert entry.pipeline_meta.weights.skill != pytest.approx(DEFAULT_WEIGHTS.skill)


# ── ROADMAP A6: the two new ScoreBreakdown markers round-trip through a
#    REAL Postgres jsonb column — no fold/pop path, unlike evidence_evaluated
#    (spec item 6; CLAUDE.md's "`offline` is not always enough" applies to
#    anything touching `models/`/`services/`) ───────────────────────────────


def _breakdown_with_markers(
    *, seniority_measured: bool | None, vector_discriminating: bool | None
) -> ScoreBreakdown:
    return ScoreBreakdown(
        skill=0.8,
        experience=0.6,
        education=0.4,
        seniority=0.5,
        vector=0.3,
        structured=0.55,
        seniority_measured=seniority_measured,
        vector_discriminating=vector_discriminating,
    )


@pytest.mark.asyncio
async def test_persist_then_get_one_round_trips_both_new_markers(
    pg_pool: asyncpg.Pool,
) -> None:
    """ADR-040's own round-trip test hand-builds a dict and calls
    ``_parse_entry_jsonb`` directly, because ``score_structured``/
    ``score_evidence``/``evidence_evaluated`` are folded into the jsonb BY
    HAND. These two markers need no such workaround — they live INSIDE
    ``ScoreBreakdown``, which ``persist_shortlist`` already stores verbatim
    as the ``score_breakdown`` jsonb — so a REAL asyncpg jsonb round trip
    through ``persist_shortlist`` / ``get_one`` is the whole test."""
    from src.services.shortlist_service import get_one, persist_shortlist

    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume(pg_pool, job_id)

    entry = ShortlistResultEntry(
        resume_id=resume_id,
        rank=1,
        score_final=0.9,
        score_structured=0.8,
        score_evidence=0.7,
        breakdown=_breakdown_with_markers(
            seniority_measured=False, vector_discriminating=True
        ),
        evidence=None,
    )

    async with pg_pool.acquire() as conn:
        await persist_shortlist(
            conn,
            ShortlistResult(job_id=job_id, entries=[entry], pipeline_meta=_meta()),
        )

    async with pg_pool.acquire() as conn:
        entry_id = await conn.fetchval(
            "SELECT id FROM shortlist_entries WHERE job_id = $1", job_id
        )
        read_back = await get_one(conn, entry_id)

    # Both markers must survive a REAL Postgres jsonb round trip byte-for-
    # byte -- a candidate whose seniority came from the no-title fallback
    # must not read back looking measured.
    assert read_back.score_breakdown.seniority_measured is False
    assert read_back.score_breakdown.vector_discriminating is True


@pytest.mark.asyncio
async def test_persist_then_get_one_a_legacy_row_with_neither_key_reads_back_unknown(
    pg_pool: asyncpg.Pool,
) -> None:
    """A row written by CODE THAT PREDATES THIS SLICE never wrote either key
    into the jsonb at all. Simulated here with a direct INSERT (bypassing
    ``ScoreBreakdown``'s own serialisation, which would now always include
    both keys) — proving the READ side treats a genuinely absent key as
    unknown, never inventing True/False."""
    from src.services.shortlist_service import get_one

    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume(pg_pool, job_id)
    legacy_breakdown = {
        "skill": 0.8,
        "experience": 0.6,
        "education": 0.4,
        "seniority": 0.5,
        "vector": 0.3,
        "structured": 0.55,
        "motivation": 0.0,
        "implied_experience": False,
        "skill_contributions": [],
        # NOTE: no seniority_measured / vector_discriminating key at all —
        # this is what every ROW WRITTEN BEFORE THIS SLICE looks like.
    }

    async with pg_pool.acquire() as conn:
        entry_id = await conn.fetchval(
            """
            INSERT INTO shortlist_entries (
                job_id, resume_id, rank, score_final, score_breakdown,
                evidence, pipeline_meta
            ) VALUES ($1, $2, 1, 0.9, $3::jsonb, '{}'::jsonb, $4::jsonb)
            RETURNING id
            """,
            job_id,
            resume_id,
            json.dumps(legacy_breakdown),
            _meta().model_dump_json(),
        )
        read_back = await get_one(conn, entry_id)

    # A genuinely absent key must read back None, never a guessed True/False.
    assert read_back.score_breakdown.seniority_measured is None
    assert read_back.score_breakdown.vector_discriminating is None
