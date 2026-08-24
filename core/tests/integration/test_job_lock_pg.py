"""Integration tests — REAL Postgres (testcontainers) proof of the
``pg_try_advisory_lock`` / ``pg_advisory_unlock`` SESSION-scoped semantics
that dedupe concurrent ``shortlist_job`` / ``reverse_match_job`` runs
(ADR-010 §1 residual: "No advisory lock on concurrent duplicate enqueues").

This file is MANDATORY, not a nice-to-have: whether a second, concurrent
connection actually gets blocked by ``pg_try_advisory_lock``, and whether
releasing on one connection actually frees the lock for another, is real
Postgres backend/session behaviour. A mocked ``conn.fetchval`` (see
``tests/unit/test_worker_job_lock.py``) can only prove which SQL string and
which two ints get sent — it returns whatever the mock is told to return, so
it CANNOT show that concurrent duplicates are actually serialized. Only a
real server with two live connections held open at once can.

Neither ``src.worker.job_lock`` nor its use inside ``src.worker.matching_tasks``
exist yet — every test below fails at collection or at the first ``import``,
either with ``ModuleNotFoundError`` or ``ImportError``. RED half of the TDD
cycle.

Mirrors the ``pg_dsn``/``pg_pool`` fixture pattern in
``test_shortlist_persistence_pg.py`` / ``test_matching_orchestrator.py``, but
uses ``pool.acquire()``/``pool.release()`` directly (not ``async with``) in
several tests, because advisory locks are tied to the PHYSICAL backend
session — two concurrently-open connections are required to prove blocking,
and a connection returned to the pool without an explicit
``pg_advisory_unlock`` keeps holding the lock (the whole reason the worker
code must release it in a ``finally``, not just let the connection go back to
the pool).
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import asyncpg
import pytest
from testcontainers.postgres import PostgresContainer

from src.models.ddl import init_schema
from src.pipeline.matching.orchestrator import ShortlistResult, ShortlistResultEntry
from src.schemas.matching import ScoreBreakdown

# ── fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def pg_dsn() -> Iterator[str]:
    with PostgresContainer("postgres:16-alpine") as pg:
        yield re.sub(r"\+\w+", "", pg.get_connection_url(), count=1)


@pytest.fixture
async def pg_pool(pg_dsn: str) -> AsyncIterator[asyncpg.Pool]:
    # min_size/max_size >= 4: several tests need TWO physical connections
    # held open concurrently at once to prove cross-session lock blocking.
    pool = await asyncpg.create_pool(dsn=pg_dsn, min_size=4, max_size=8)
    await init_schema(pool)
    async with pool.acquire() as conn:
        await conn.execute("TRUNCATE jobs, resumes, outbox CASCADE")
    try:
        yield pool
    finally:
        await pool.close()


# ── seeding helpers ──────────────────────────────────────────────────────


async def _insert_job(
    pool: asyncpg.Pool, *, description_parsed: dict[str, Any] | None = None
) -> UUID:
    async with pool.acquire() as conn:
        job_id: UUID = await conn.fetchval(
            "INSERT INTO jobs (title, description_raw, description_parsed) "
            "VALUES ($1, $2, $3::jsonb) RETURNING id",
            "Senior Backend Engineer",
            "raw jd text (irrelevant to these tests)",
            json.dumps(description_parsed) if description_parsed is not None else None,
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


def _shortlist_entry(resume_id: UUID, *, rank: int) -> ShortlistResultEntry:
    return ShortlistResultEntry(
        resume_id=resume_id,
        rank=rank,
        score_final=0.9,
        score_structured=0.8,
        score_evidence=0.7,
        breakdown=_breakdown(),
        evidence=None,
    )


# ── try_job_lock / release_job_lock: real cross-session semantics ─────────


@pytest.mark.asyncio
async def test_try_job_lock_blocks_a_concurrent_duplicate_on_a_different_connection(
    pg_pool: asyncpg.Pool,
) -> None:
    from src.worker.job_lock import release_job_lock, try_job_lock

    entity_id = uuid4()
    conn1 = await pg_pool.acquire()
    conn2 = await pg_pool.acquire()
    try:
        first = await try_job_lock(conn1, "shortlist", entity_id)
        second = await try_job_lock(conn2, "shortlist", entity_id)

        assert first is True
        assert second is False, (
            "a second connection trying the SAME (kind, entity_id) while the "
            "first still holds it must be refused — this is the concurrent-"
            "duplicate-run dedup the whole fix exists for"
        )
    finally:
        await release_job_lock(conn1, "shortlist", entity_id)
        await pg_pool.release(conn1)
        await pg_pool.release(conn2)


@pytest.mark.asyncio
async def test_release_then_a_fresh_acquire_succeeds_sequential_rerun_allowed(
    pg_pool: asyncpg.Pool,
) -> None:
    """A legitimate re-run AFTER the first finishes must NOT be blocked —
    this is the property that rules out an arq ``_job_id``/keep_result
    approach (which would block re-runs for the whole result TTL)."""
    from src.worker.job_lock import release_job_lock, try_job_lock

    entity_id = uuid4()
    conn1 = await pg_pool.acquire()
    first = await try_job_lock(conn1, "shortlist", entity_id)
    assert first is True
    await release_job_lock(conn1, "shortlist", entity_id)
    await pg_pool.release(conn1)

    conn2 = await pg_pool.acquire()
    try:
        second = await try_job_lock(conn2, "shortlist", entity_id)
        assert (
            second is True
        ), "after the holder released, a legitimate re-run must succeed"
    finally:
        await release_job_lock(conn2, "shortlist", entity_id)
        await pg_pool.release(conn2)


@pytest.mark.asyncio
async def test_different_kind_same_entity_id_do_not_block_each_other(
    pg_pool: asyncpg.Pool,
) -> None:
    """``shortlist`` and ``reverse_match`` are distinct advisory-lock
    namespaces (different classids) — a job id and a résumé id that happen to
    collide on the low 32 bits must not fight over the same key."""
    from src.worker.job_lock import release_job_lock, try_job_lock

    entity_id = uuid4()
    conn1 = await pg_pool.acquire()
    conn2 = await pg_pool.acquire()
    try:
        shortlist_held = await try_job_lock(conn1, "shortlist", entity_id)
        reverse_held = await try_job_lock(conn2, "reverse_match", entity_id)

        assert shortlist_held is True
        assert reverse_held is True
    finally:
        await release_job_lock(conn1, "shortlist", entity_id)
        await release_job_lock(conn2, "reverse_match", entity_id)
        await pg_pool.release(conn1)
        await pg_pool.release(conn2)


@pytest.mark.asyncio
async def test_different_entity_ids_of_the_same_kind_do_not_block_each_other(
    pg_pool: asyncpg.Pool,
) -> None:
    from src.worker.job_lock import release_job_lock, try_job_lock

    id_a, id_b = uuid4(), uuid4()
    conn1 = await pg_pool.acquire()
    conn2 = await pg_pool.acquire()
    try:
        held_a = await try_job_lock(conn1, "shortlist", id_a)
        held_b = await try_job_lock(conn2, "shortlist", id_b)

        assert held_a is True
        assert held_b is True
    finally:
        await release_job_lock(conn1, "shortlist", id_a)
        await release_job_lock(conn2, "shortlist", id_b)
        await pg_pool.release(conn1)
        await pg_pool.release(conn2)


@pytest.mark.asyncio
async def test_a_pending_holder_still_blocks_across_many_distinct_challengers(
    pg_pool: asyncpg.Pool,
) -> None:
    """Not just one challenger — repeated attempts from fresh connections all
    fail while the lock is held, ruling out a "only blocks the very next
    caller" off-by-one."""
    from src.worker.job_lock import release_job_lock, try_job_lock

    entity_id = uuid4()
    holder = await pg_pool.acquire()
    try:
        assert await try_job_lock(holder, "reverse_match", entity_id) is True

        for _ in range(3):
            challenger = await pg_pool.acquire()
            try:
                assert (
                    await try_job_lock(challenger, "reverse_match", entity_id) is False
                )
            finally:
                await pg_pool.release(challenger)
    finally:
        await release_job_lock(holder, "reverse_match", entity_id)
        await pg_pool.release(holder)


# ── shortlist_job / reverse_match_job wiring against a REAL held lock ─────


@pytest.mark.asyncio
async def test_shortlist_job_returns_already_running_and_writes_nothing(
    pg_pool: asyncpg.Pool,
) -> None:
    """Seeds a real job + résumé, holds the REAL advisory lock for that job
    on a separate connection, then invokes ``shortlist_job`` with a real
    ``pg_pool``. The orchestrator functions are patched only so we can assert
    they were NEVER called — proving the early return happens before any
    ranking/persist work, backed by a real lock actually being held."""
    from src.worker.job_lock import release_job_lock, try_job_lock
    from src.worker.matching_tasks import shortlist_job

    job_id = await _insert_job(pg_pool, description_parsed={"required_skills": []})
    await _insert_resume(pg_pool, job_id)

    holder = await pg_pool.acquire()
    assert await try_job_lock(holder, "shortlist", job_id) is True

    ctx: dict[str, Any] = {
        "pg_pool": pg_pool,
        "neo4j": MagicMock(),
        "llm": MagicMock(),
        "embedder": MagicMock(),
    }

    try:
        with (
            patch(
                "src.worker.matching_tasks.generate_shortlist", new_callable=AsyncMock
            ) as generate,
            patch(
                "src.worker.matching_tasks.persist_shortlist", new_callable=AsyncMock
            ) as persist,
        ):
            result = await shortlist_job(ctx, str(job_id))
    finally:
        await release_job_lock(holder, "shortlist", job_id)
        await pg_pool.release(holder)

    assert result == "already_running"
    generate.assert_not_called()
    persist.assert_not_called()

    async with pg_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM shortlist_entries WHERE job_id = $1", job_id
        )
    assert rows == [], "no shortlist row may be written while the lock is held"


@pytest.mark.asyncio
async def test_reverse_match_job_returns_already_running_and_writes_nothing(
    pg_pool: asyncpg.Pool,
) -> None:
    from src.worker.job_lock import release_job_lock, try_job_lock
    from src.worker.matching_tasks import reverse_match_job

    job_id = await _insert_job(pg_pool, description_parsed={"required_skills": []})
    resume_id = await _insert_resume(pg_pool, job_id)

    holder = await pg_pool.acquire()
    assert await try_job_lock(holder, "reverse_match", resume_id) is True

    ctx: dict[str, Any] = {
        "pg_pool": pg_pool,
        "neo4j": MagicMock(),
        "llm": MagicMock(),
        "embedder": MagicMock(),
    }

    try:
        with (
            patch(
                "src.worker.matching_tasks.match_resume_to_jobs",
                new_callable=AsyncMock,
            ) as match_jobs,
            patch(
                "src.worker.matching_tasks.persist_reverse_match",
                new_callable=AsyncMock,
            ) as persist,
        ):
            result = await reverse_match_job(ctx, str(resume_id))
    finally:
        await release_job_lock(holder, "reverse_match", resume_id)
        await pg_pool.release(holder)

    assert result == "already_running"
    match_jobs.assert_not_called()
    persist.assert_not_called()

    async with pg_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM reverse_match_entries WHERE resume_id = $1", resume_id
        )
    assert rows == [], "no reverse-match row may be written while the lock is held"


@pytest.mark.asyncio
async def test_shortlist_job_normal_run_releases_the_lock_allowing_immediate_rerun(
    pg_pool: asyncpg.Pool,
) -> None:
    """A completed run must free the lock (via the ``finally`` release) so a
    legitimate re-run right after is NOT blocked."""
    from src.worker.job_lock import release_job_lock, try_job_lock
    from src.worker.matching_tasks import shortlist_job

    job_id = await _insert_job(pg_pool, description_parsed={"required_skills": []})
    resume_id = await _insert_resume(pg_pool, job_id)

    canned_result = ShortlistResult(
        job_id=job_id, entries=[_shortlist_entry(resume_id, rank=1)]
    )
    ctx: dict[str, Any] = {
        "pg_pool": pg_pool,
        "neo4j": MagicMock(),
        "llm": MagicMock(),
        "embedder": MagicMock(),
    }

    with patch(
        "src.worker.matching_tasks.generate_shortlist",
        new_callable=AsyncMock,
        return_value=canned_result,
    ):
        result = await shortlist_job(ctx, str(job_id))

    assert result == "persisted"

    conn2 = await pg_pool.acquire()
    try:
        reacquired = await try_job_lock(conn2, "shortlist", job_id)
        assert reacquired is True, (
            "the lock must have been released when shortlist_job finished; a "
            "fresh acquire immediately after must succeed"
        )
    finally:
        await release_job_lock(conn2, "shortlist", job_id)
        await pg_pool.release(conn2)
