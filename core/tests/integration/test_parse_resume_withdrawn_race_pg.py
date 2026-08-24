"""Integration test — the withdrawn-during-parse race (ADR-026, FU-8).

A résumé withdrawn WHILE it is still ``uploaded``/``parsing`` (a candidate
clicks "withdraw" moments before the worker picks up the parse job) must not
have its parse silently re-enter it into the candidate pool: ``parse_resume``
must still run to completion — the row's ``resume_status`` machine (ADR-021)
is untouched, so it still lands at ``status='parsed'`` — but the
``resume.parsed`` outbox enqueue (the event that drives graph projection,
i.e. re-entering the ranking pool) must be SKIPPED whenever
``withdrawn_at IS NOT NULL`` at the point the write-back transaction runs.

This is a REAL Postgres proof (testcontainers) — the race is a property of
two REAL transactions interleaving on the SAME row, not something a mocked
connection can demonstrate. Only the LLM and embedder are mocked (no Ollama
in gates — see CLAUDE.md); blob I/O, extraction, chunking, the pgcrypto PII
write-back, and the outbox INSERT/absence all run for real, mirroring
``test_parse_resume_e2e.py``'s harness.

``src.worker.resume_tasks.parse_resume`` does not yet check
``resumes.withdrawn_at`` before its outbox enqueue — every test below is
expected to fail (the "regression twin" passes today; the withdrawn-race
test does not, because the row currently gets a live ``resume.parsed`` row
regardless of ``withdrawn_at``). RED half of the TDD cycle.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest
from testcontainers.postgres import PostgresContainer

from src.models.ddl import init_schema
from src.pipeline.parsing import MIME_TXT
from src.schemas import (
    CandidateInfo,
    CoverLetterParsed,
    ResumeCore,
    ResumeSkillDetail,
    ResumeSkillDetails,
)
from src.storage.blob_store import BlobStore
from src.worker.resume_tasks import parse_resume

PII_KEY = "b3VyLTMyLWJ5dGUtcGlpLWtleS1mb3ItdGVzdHM="

RESUME_TEXT = """Summary
Experienced backend engineer with a distributed systems background.

Experience
Senior Software Engineer at Acme Corp
2019 - Present
Built distributed systems in Python and Kubernetes.

Education
BSc Computer Science, Example University, 2015

Skills
Python, Kubernetes, PostgreSQL, Redis
"""

CORE_RESULT = ResumeCore(
    candidate=CandidateInfo(
        name="Withdrawn Race Candidate",
        email="withdrawn.race@example.org",
        phone="555-0100",
        location="Waterloo, ON",
    ),
    summary="Experienced backend engineer.",
    total_years_experience=6,
)

SKILLS_RESULT = ResumeSkillDetails(skills=[ResumeSkillDetail(name="Python", years=6)])


def _make_llm() -> MagicMock:
    async def _chat_json(messages: Any, schema: Any, **kwargs: Any) -> Any:
        if schema is ResumeCore:
            return CORE_RESULT
        if schema is ResumeSkillDetails:
            return SKILLS_RESULT
        if schema is CoverLetterParsed:
            return CoverLetterParsed()
        raise AssertionError(f"unexpected schema: {schema!r}")

    return MagicMock(chat_json=AsyncMock(side_effect=_chat_json))


def _make_embedder() -> MagicMock:
    async def _embed(texts: Any) -> list[list[float]]:
        return [[float(i) + 0.1] * 8 for i in range(len(texts))]

    return MagicMock(embed=AsyncMock(side_effect=_embed))


@pytest.fixture(scope="session")
def pg_dsn() -> Iterator[str]:
    with PostgresContainer("postgres:16-alpine") as pg:
        yield re.sub(r"\+\w+", "", pg.get_connection_url(), count=1)


@pytest.fixture
async def pg_pool(pg_dsn: str) -> AsyncIterator[asyncpg.Pool]:
    pool = await asyncpg.create_pool(dsn=pg_dsn, min_size=1, max_size=5)
    await init_schema(pool)
    async with pool.acquire() as conn:
        await conn.execute("TRUNCATE jobs, outbox CASCADE")
    try:
        yield pool
    finally:
        await pool.close()


@pytest.fixture
def blob_store(tmp_path: Path) -> BlobStore:
    return BlobStore(tmp_path / "blobs")


async def _insert_job(pool: asyncpg.Pool) -> uuid.UUID:
    async with pool.acquire() as conn:
        job_id: uuid.UUID = await conn.fetchval(
            "INSERT INTO jobs (title, description_raw) VALUES ($1, $2) RETURNING id",
            "Senior Backend Engineer",
            "Build the ranking pipeline end to end.",
        )
    return job_id


async def _insert_resume(
    pool: asyncpg.Pool,
    job_id: uuid.UUID,
    *,
    blob_key: str,
    mime_type: str,
    file_size_bytes: int,
    sha256: str,
) -> uuid.UUID:
    async with pool.acquire() as conn:
        resume_id: uuid.UUID = await conn.fetchval(
            """
            INSERT INTO resumes (
                job_id, blob_key, original_filename, mime_type,
                file_size_bytes, sha256, consent_acknowledged
            ) VALUES ($1, $2, 'race-candidate.txt', $3, $4, $5, TRUE)
            RETURNING id
            """,
            job_id,
            blob_key,
            mime_type,
            file_size_bytes,
            sha256,
        )
    return resume_id


async def _mark_withdrawn(pool: asyncpg.Pool, resume_id: uuid.UUID) -> None:
    """Simulate the race directly: the candidate withdrew while the résumé
    was still ``uploaded``, i.e. BEFORE ``parse_resume`` ever ran."""
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE resumes SET withdrawn_at = now(), withdrawal_reason = $2 "
            "WHERE id = $1",
            resume_id,
            "withdrew mid-parse",
        )


async def _seed_and_parse(
    pg_pool: asyncpg.Pool, blob_store: BlobStore, *, withdrawn: bool
) -> dict[str, Any]:
    job_id = await _insert_job(pg_pool)
    data = RESUME_TEXT.encode("utf-8")
    blob_key = f"resumes/{uuid.uuid4().hex}.txt"
    await blob_store.put(blob_key, data)
    resume_id = await _insert_resume(
        pg_pool,
        job_id,
        blob_key=blob_key,
        mime_type=MIME_TXT,
        file_size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )
    if withdrawn:
        await _mark_withdrawn(pg_pool, resume_id)

    ctx: dict[str, Any] = {
        "pg_pool": pg_pool,
        "llm": _make_llm(),
        "embedder": _make_embedder(),
        "blob_store": blob_store,
    }
    with patch(
        "src.services.pii.get_settings",
        return_value=SimpleNamespace(pii_key=PII_KEY),
    ):
        result = await parse_resume(ctx, str(resume_id))

    return {"result": result, "resume_id": resume_id, "job_id": job_id}


# ── the race: withdrawn mid-parse ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_withdrawn_mid_parse_still_reaches_status_parsed(
    pg_pool: asyncpg.Pool, blob_store: BlobStore
) -> None:
    """ADR-026 must not introduce a second parse-state machine (ADR-021 stays
    the single source of truth) — parsing itself completes normally even
    though the row was withdrawn under it."""
    outcome = await _seed_and_parse(pg_pool, blob_store, withdrawn=True)
    assert outcome["result"] == "parsed"

    async with pg_pool.acquire() as conn:
        status = await conn.fetchval(
            "SELECT status FROM resumes WHERE id = $1", outcome["resume_id"]
        )
    assert status == "parsed"


@pytest.mark.asyncio
async def test_withdrawn_mid_parse_enqueues_no_resume_parsed_outbox_row(
    pg_pool: asyncpg.Pool, blob_store: BlobStore
) -> None:
    """The load-bearing fix: the projection-triggering outbox enqueue is
    SKIPPED — a withdrawn-mid-parse résumé must never re-enter the ranking
    pool via a live projection event."""
    outcome = await _seed_and_parse(pg_pool, blob_store, withdrawn=True)

    async with pg_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id FROM outbox WHERE aggregate_id = $1 AND event_type = "
            "'resume.parsed'",
            outcome["resume_id"],
        )
    assert rows == [], (
        "a withdrawn-mid-parse résumé must not enqueue a resume.parsed " "outbox row"
    )


@pytest.mark.asyncio
async def test_withdrawn_mid_parse_preserves_withdrawn_at_and_reason(
    pg_pool: asyncpg.Pool, blob_store: BlobStore
) -> None:
    """``parse_resume``'s write-back must not clobber the withdrawal columns
    it is deliberately deferring to."""
    outcome = await _seed_and_parse(pg_pool, blob_store, withdrawn=True)

    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT withdrawn_at, withdrawal_reason FROM resumes WHERE id = $1",
            outcome["resume_id"],
        )
    assert row is not None
    assert row["withdrawn_at"] is not None
    assert row["withdrawal_reason"] == "withdrew mid-parse"


# ── regression twin: NOT withdrawn — outbox row IS inserted, as before ─────


@pytest.mark.asyncio
async def test_regression_twin_not_withdrawn_still_enqueues_resume_parsed(
    pg_pool: asyncpg.Pool, blob_store: BlobStore
) -> None:
    """The pairing test: an ordinary (never-withdrawn) parse must be
    UNCHANGED by this fix — the outbox row still lands. A builder that
    accidentally skips the enqueue unconditionally (rather than gating on
    ``withdrawn_at``) passes the race test above but fails this one."""
    outcome = await _seed_and_parse(pg_pool, blob_store, withdrawn=False)
    assert outcome["result"] == "parsed"

    async with pg_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, delivered_at FROM outbox WHERE aggregate_id = $1 "
            "AND event_type = 'resume.parsed'",
            outcome["resume_id"],
        )
    assert len(rows) == 1
    assert rows[0]["delivered_at"] is None
