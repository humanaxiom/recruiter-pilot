"""Integration tests — FU-7 §2 fail-closed ranking (ADR-021 §2 / ADR-029)
against a REAL Postgres (testcontainers), mirroring
``test_shortlist_persistence_pg.py``'s ``pg_dsn``/``pg_pool`` fixtures and
``test_matching_orchestrator.py``'s pattern of mocking Neo4j directly (no
Neo4j testcontainer needed here — only the REAL orchestrator's Postgres
reads/writes matter for this slice; the graph half is faked deterministically
so ``generate_shortlist`` runs for REAL end to end, including its stage-2/
stage-3 wiring, without a live Neo4j).

None of the following exist yet — every test below fails, either at
collection (``ModuleNotFoundError`` on the new imports) or at the first
``jobs.shortlist_state`` column read (``asyncpg.exceptions.UndefinedColumnError``
against the real schema). RED half of the TDD cycle.

* ``src.pipeline.matching.orchestrator.RankingUnavailableError``
* ``src.services.shortlist_service.set_shortlist_awaiting_llm`` /
  ``clear_shortlist_state`` / ``get_shortlist_state``
* ``jobs.shortlist_state`` / ``shortlist_state_reason`` / ``shortlist_state_at``
  columns (``src/models/ddl.py``)
* ``settings.shortlist_max_tries`` / ``shortlist_retry_defer_s``

What a REAL Postgres proves that a mocked-conn unit test
(``test_worker_shortlist_job.py``) cannot:

* the ``jobs.shortlist_state`` CHECK constraint / column shape actually
  exists and round-trips through a real ``UPDATE`` + ``SELECT``;
* ``shortlist_job``, run against a REAL orchestrator (``generate_shortlist``
  is NOT mocked here — only its Neo4j/LLM dependencies are), fails CLOSED:
  an LLM failure ANYWHERE in the stage-2/stage-3 LLM/embedder path leaves
  ZERO rows in ``shortlist_entries`` — not a partial, silently-degraded
  shortlist (ADR-009 residual / explainer register item 11, the whole point
  of this slice);
* a subsequent SUCCESSFUL run both persists rows AND clears the state,
  through the real DELETE-first + UPDATE path.
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
from arq import Retry
from testcontainers.postgres import PostgresContainer

from src.models.ddl import init_schema
from src.pipeline.llm import LLMOutputInvalidError, LLMUnavailableError
from src.schemas.matching import EvidenceObjectIngest

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


async def _insert_job(pool: asyncpg.Pool) -> UUID:
    async with pool.acquire() as conn:
        job_id: UUID = await conn.fetchval(
            "INSERT INTO jobs (title, description_raw, description_parsed) "
            "VALUES ($1, $2, $3::jsonb) RETURNING id",
            "Senior Backend Engineer",
            "raw jd text (irrelevant to these tests)",
            '{"required_skills": [{"name": "Python"}]}',
        )
    return job_id


async def _insert_resume_with_chunks(pool: asyncpg.Pool, job_id: UUID) -> UUID:
    """A 'parsed' résumé carrying at least one chunk and NO ``experience``
    entries (so stage-2's seniority cosine short-circuits to 0.0 without
    ever calling the embedder — this slice's fail-closed path is driven
    entirely through ``ctx.llm``, not ``ctx.embedder``)."""
    parsed = (
        '{"chunks": [{"id": "c_001", "section": "summary", "page": 0, '
        '"text": "Six years of backend experience with Python."}]}'
    )
    async with pool.acquire() as conn:
        resume_id: UUID = await conn.fetchval(
            """
            INSERT INTO resumes (
                job_id, blob_key, original_filename, mime_type,
                file_size_bytes, sha256, consent_acknowledged, status, parsed
            ) VALUES ($1, $2, 'resume.pdf', 'application/pdf', 1024, $3, TRUE,
                       'parsed', $4::jsonb)
            RETURNING id
            """,
            job_id,
            f"resumes/{uuid4().hex}.pdf",
            uuid4().hex,
            parsed,
        )
    return resume_id


class _AsyncRows:
    """An async-iterable Neo4j result yielding pre-baked dict rows —
    ``dict(row)`` (the real orchestrator's own decoding step) is a no-op on
    an already-plain dict."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def __aiter__(self) -> _AsyncRows:
        self._iter = iter(self._rows)
        return self

    async def __anext__(self) -> dict[str, Any]:
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration from None


def _acm(return_value: Any = None) -> MagicMock:
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=return_value)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _fake_neo4j(resume_id: UUID) -> MagicMock:
    """Stage-1's vector query yields exactly ``resume_id``; stage-2's
    skill-rows query yields no ``REQUIRES`` edges (no graph seeded) — neither
    matters for THIS slice, whose failure is driven entirely by ``ctx.llm``
    during stage 3.

    The stage-1 query is recognised by the ``vec_score`` column it RETURNS,
    not by the Cypher it uses to get there. It was previously matched on
    ``"queryNodes" in query``: when ROADMAP A4 M2 replaced the global vector
    index with a job-scoped cosine, that discriminator stopped matching and
    this stub silently returned NO candidates — so stage 3 never ran, the LLM
    never failed, and four fail-closed tests reported ``shortlist_state
    ='empty'`` instead of ``'awaiting_llm'``. A stub keyed on an
    implementation detail fails loudly only if you are lucky; keying it on the
    column stage 1 must produce ties it to the contract instead.
    """
    session = MagicMock(name="session")

    async def _run(query: str, **_kwargs: Any) -> _AsyncRows:
        if "vec_score" in query:
            return _AsyncRows([{"resume_id": str(resume_id), "vec_score": 0.9}])
        return _AsyncRows([])

    session.run = AsyncMock(side_effect=_run)
    neo4j = MagicMock(name="neo4j")
    neo4j.session = MagicMock(return_value=_acm(session))
    return neo4j


def _worker_ctx(
    pg_pool: asyncpg.Pool, neo4j: MagicMock, llm: MagicMock
) -> dict[str, Any]:
    return {
        "pg_pool": pg_pool,
        "neo4j": neo4j,
        "llm": llm,
        "embedder": MagicMock(name="embedder"),
    }


def _failing_llm(exc: Exception) -> MagicMock:
    return MagicMock(chat_json=AsyncMock(side_effect=exc))


def _working_llm() -> MagicMock:
    return MagicMock(chat_json=AsyncMock(return_value=EvidenceObjectIngest()))


async def _job_state_row(pool: asyncpg.Pool, job_id: UUID) -> Any:
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT shortlist_state, shortlist_state_reason, shortlist_state_at "
            "FROM jobs WHERE id = $1",
            job_id,
        )


async def _shortlist_entry_count(pool: asyncpg.Pool, job_id: UUID) -> int:
    async with pool.acquire() as conn:
        count: int = await conn.fetchval(
            "SELECT count(*) FROM shortlist_entries WHERE job_id = $1", job_id
        )
    return count


# ── fail-closed: Mode B (LLMOutputInvalidError) ─────────────────────────────


@pytest.mark.asyncio
async def test_shortlist_job_fails_closed_on_llm_output_invalid_error(
    pg_pool: asyncpg.Pool,
) -> None:
    from src.pipeline.matching.orchestrator import RankingUnavailableError
    from src.worker.matching_tasks import shortlist_job

    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume_with_chunks(pg_pool, job_id)
    neo4j = _fake_neo4j(resume_id)
    llm = _failing_llm(LLMOutputInvalidError("response content was empty"))
    ctx = _worker_ctx(pg_pool, neo4j, llm)

    with pytest.raises(Retry) as exc_info:
        await shortlist_job(ctx, str(job_id))

    assert isinstance(exc_info.value.__cause__, RankingUnavailableError)

    assert await _shortlist_entry_count(pg_pool, job_id) == 0, (
        "a fail-closed LLM failure must persist NO shortlist_entries rows — "
        "not a silently-degraded partial shortlist (ADR-021 §2)"
    )
    row = await _job_state_row(pg_pool, job_id)
    assert row is not None
    assert row["shortlist_state"] == "awaiting_llm"
    assert row["shortlist_state_reason"] is not None
    assert row["shortlist_state_at"] is not None


# ── fail-closed on a REGENERATE — prior entries survive untouched ──────────
#
# Reviewer finding (fix/regenerate-shortlist-no-feedback follow-up,
# 2026-08-18): every fail-closed test above starts from a job with NO prior
# shortlist. The reported no-feedback bug (and the frontend's
# ``awaiting_llm``-with-entries tests in ``test_frontend_shortlist.py``) both
# depend on a fact this file never actually drove through a real Postgres:
# that a Regenerate whose LLM call fails closed leaves a job's PRIOR run's
# ``shortlist_entries`` row completely untouched, because ``persist_shortlist``
# is never reached on this path (ADR-029's worker mechanism — the
# ``set_shortlist_awaiting_llm`` write and the ``raise Retry`` both happen
# before any delete/insert into ``shortlist_entries``). This is a real-DB fact
# (does the DELETE-first persist path get anywhere NEAR the table on this
# branch?), not something a template-level test can prove.


@pytest.mark.asyncio
async def test_shortlist_job_fail_closed_leaves_prior_entries_untouched(
    pg_pool: asyncpg.Pool,
) -> None:
    from src.pipeline.matching.orchestrator import RankingUnavailableError
    from src.worker.matching_tasks import shortlist_job

    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume_with_chunks(pg_pool, job_id)

    # Seed a PRIOR successful run's entry directly -- this file has no
    # dependency on shortlist_service.persist_shortlist, and only the row's
    # continued existence (not how it got there) matters for this test.
    prior_generated_at = dt.datetime(2026, 8, 1, tzinfo=dt.UTC)
    async with pg_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO shortlist_entries
                (job_id, resume_id, rank, score_final, score_breakdown,
                 evidence, pipeline_meta, generated_at)
            VALUES ($1, $2, 1, 0.75, '{}'::jsonb, '{}'::jsonb, '{}'::jsonb, $3)
            """,
            job_id,
            resume_id,
            prior_generated_at,
        )

    neo4j = _fake_neo4j(resume_id)
    llm = _failing_llm(LLMOutputInvalidError("response content was empty"))
    ctx = _worker_ctx(pg_pool, neo4j, llm)

    with pytest.raises(Retry) as exc_info:
        await shortlist_job(ctx, str(job_id))
    assert isinstance(exc_info.value.__cause__, RankingUnavailableError)

    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT rank, score_final, generated_at FROM shortlist_entries "
            "WHERE job_id = $1 AND resume_id = $2",
            job_id,
            resume_id,
        )
    assert row is not None, (
        "the prior run's entry must survive a fail-closed regenerate — "
        "persist_shortlist is never reached on this path"
    )
    assert row["rank"] == 1
    assert row["score_final"] == pytest.approx(0.75)
    assert row["generated_at"] == prior_generated_at

    state_row = await _job_state_row(pg_pool, job_id)
    assert state_row is not None
    assert state_row["shortlist_state"] == "awaiting_llm"


# ── fail-closed: Mode A (LLMUnavailableError) ───────────────────────────────


@pytest.mark.asyncio
async def test_shortlist_job_fails_closed_on_llm_unavailable_error(
    pg_pool: asyncpg.Pool,
) -> None:
    from src.pipeline.matching.orchestrator import RankingUnavailableError
    from src.worker.matching_tasks import shortlist_job

    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume_with_chunks(pg_pool, job_id)
    neo4j = _fake_neo4j(resume_id)
    llm = _failing_llm(LLMUnavailableError("circuit breaker open"))
    ctx = _worker_ctx(pg_pool, neo4j, llm)

    with pytest.raises(Retry) as exc_info:
        await shortlist_job(ctx, str(job_id))

    assert isinstance(exc_info.value.__cause__, RankingUnavailableError)

    assert await _shortlist_entry_count(pg_pool, job_id) == 0
    row = await _job_state_row(pg_pool, job_id)
    assert row is not None
    assert row["shortlist_state"] == "awaiting_llm"
    assert row["shortlist_state_reason"] is not None
    assert row["shortlist_state_at"] is not None


# ── retries exhausted: no more Retry, state stays visible ───────────────────


@pytest.mark.asyncio
async def test_shortlist_job_gives_up_at_max_tries_without_raising_retry(
    pg_pool: asyncpg.Pool,
) -> None:
    from unittest.mock import patch

    from src.settings import Settings
    from src.worker.matching_tasks import shortlist_job

    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume_with_chunks(pg_pool, job_id)
    neo4j = _fake_neo4j(resume_id)
    llm = _failing_llm(LLMOutputInvalidError("response content was empty"))
    ctx = _worker_ctx(pg_pool, neo4j, llm)
    ctx["job_try"] = 1

    with patch(
        "src.worker.matching_tasks.get_settings",
        return_value=Settings(shortlist_max_tries=1),
    ):
        result = await shortlist_job(ctx, str(job_id))

    assert result == "awaiting_llm"
    assert await _shortlist_entry_count(pg_pool, job_id) == 0
    row = await _job_state_row(pg_pool, job_id)
    assert row is not None
    assert row["shortlist_state"] == "awaiting_llm"


# ── follow-up success: persists AND clears the awaiting_llm state ──────────


@pytest.mark.asyncio
async def test_shortlist_job_followup_success_persists_and_clears_state(
    pg_pool: asyncpg.Pool,
) -> None:
    from src.worker.matching_tasks import shortlist_job

    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume_with_chunks(pg_pool, job_id)
    neo4j = _fake_neo4j(resume_id)

    failing_ctx = _worker_ctx(
        pg_pool, neo4j, _failing_llm(LLMOutputInvalidError("empty"))
    )
    with pytest.raises(Retry):
        await shortlist_job(failing_ctx, str(job_id))
    assert (await _job_state_row(pg_pool, job_id))["shortlist_state"] == "awaiting_llm"

    working_ctx = _worker_ctx(pg_pool, neo4j, _working_llm())
    result = await shortlist_job(working_ctx, str(job_id))

    assert result == "persisted"
    async with pg_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT resume_id FROM shortlist_entries WHERE job_id = $1", job_id
        )
    assert {r["resume_id"] for r in rows} == {resume_id}

    row = await _job_state_row(pg_pool, job_id)
    assert row is not None
    assert row["shortlist_state"] is None
    assert row["shortlist_state_reason"] is None
    assert row["shortlist_state_at"] is None


# ── shortlist_service: set/get/clear the awaiting_llm state directly ──────


@pytest.mark.asyncio
async def test_get_shortlist_state_returns_none_when_no_state_set(
    pg_pool: asyncpg.Pool,
) -> None:
    from src.services.shortlist_service import get_shortlist_state

    job_id = await _insert_job(pg_pool)

    async with pg_pool.acquire() as conn:
        state = await get_shortlist_state(conn, job_id)

    assert state is None


@pytest.mark.asyncio
async def test_set_shortlist_awaiting_llm_then_get_shortlist_state_round_trips(
    pg_pool: asyncpg.Pool,
) -> None:
    from src.services.shortlist_service import (
        get_shortlist_state,
        set_shortlist_awaiting_llm,
    )

    job_id = await _insert_job(pg_pool)

    async with pg_pool.acquire() as conn:
        await set_shortlist_awaiting_llm(
            conn, job_id, reason="llm output invalid: empty response"
        )
        state = await get_shortlist_state(conn, job_id)

    assert state is not None
    assert state.state == "awaiting_llm"
    assert state.reason == "llm output invalid: empty response"
    assert state.at is not None


@pytest.mark.asyncio
async def test_clear_shortlist_state_nulls_out_every_column(
    pg_pool: asyncpg.Pool,
) -> None:
    from src.services.shortlist_service import (
        clear_shortlist_state,
        get_shortlist_state,
        set_shortlist_awaiting_llm,
    )

    job_id = await _insert_job(pg_pool)

    async with pg_pool.acquire() as conn:
        await set_shortlist_awaiting_llm(conn, job_id, reason="boom")
        await clear_shortlist_state(conn, job_id)
        state = await get_shortlist_state(conn, job_id)

    assert state is None
    row = await _job_state_row(pg_pool, job_id)
    assert row is not None
    assert row["shortlist_state"] is None
    assert row["shortlist_state_reason"] is None
    assert row["shortlist_state_at"] is None


@pytest.mark.asyncio
async def test_clear_shortlist_state_on_an_already_clear_job_is_a_no_op(
    pg_pool: asyncpg.Pool,
) -> None:
    from src.services.shortlist_service import clear_shortlist_state

    job_id = await _insert_job(pg_pool)

    async with pg_pool.acquire() as conn:
        await clear_shortlist_state(conn, job_id)  # must not raise

    row = await _job_state_row(pg_pool, job_id)
    assert row is not None
    assert row["shortlist_state"] is None


# ── IDOR fix (security + reviewer finding) — get_shortlist_state row-scoping
#
# GET /jobs/{job_id}/shortlist/status resolves a scoped user_id but never
# forwarded it into get_shortlist_state, unlike its sibling list_for_job.
# An unassigned hiring_manager could read another job's ranking state, and
# the 404-vs-200 split doubled as a job-existence oracle. Mirrors
# job_service.get_job's precedent (job_service.py:402,
# _JOB_ASSIGNEE_EXISTS_SQL): a scoped user_id that is NOT assigned collapses
# to the SAME NotFoundError as a genuinely nonexistent job.
#
# get_shortlist_state(conn, job_id, user_id=...) does not exist yet -- every
# test below fails today with TypeError (unexpected keyword argument
# 'user_id'), a valid RED for the missing param.


async def _insert_user(pool: asyncpg.Pool, *, role: str) -> UUID:
    async with pool.acquire() as conn:
        user_id: UUID = await conn.fetchval(
            "INSERT INTO users (cas_username, display_name, email, role) "
            "VALUES ($1, $2, $3, $4) RETURNING id",
            f"{role}-{uuid4().hex}",
            role,
            None,
            role,
        )
    return user_id


async def _assign(
    pool: asyncpg.Pool, *, job_id: UUID, user_id: UUID, assigned_by: UUID
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO job_assignees (job_id, user_id, assigned_by) "
            "VALUES ($1, $2, $3)",
            job_id,
            user_id,
            assigned_by,
        )


@pytest.mark.asyncio
async def test_get_shortlist_state_assigned_hiring_manager_sees_the_state(
    pg_pool: asyncpg.Pool,
) -> None:
    from src.services.shortlist_service import (
        get_shortlist_state,
        set_shortlist_awaiting_llm,
    )

    job_id = await _insert_job(pg_pool)
    admin_id = await _insert_user(pg_pool, role="admin")
    hm_id = await _insert_user(pg_pool, role="hiring_manager")
    await _assign(pg_pool, job_id=job_id, user_id=hm_id, assigned_by=admin_id)

    async with pg_pool.acquire() as conn:
        await set_shortlist_awaiting_llm(conn, job_id, reason="empty response")
        state = await get_shortlist_state(conn, job_id, user_id=hm_id)

    assert state is not None
    assert state.state == "awaiting_llm"
    assert state.reason == "empty response"


@pytest.mark.asyncio
async def test_get_shortlist_state_unassigned_hiring_manager_gets_not_found(
    pg_pool: asyncpg.Pool,
) -> None:
    from src.errors import NotFoundError
    from src.services.shortlist_service import (
        get_shortlist_state,
        set_shortlist_awaiting_llm,
    )

    job_id = await _insert_job(pg_pool)
    unassigned_hm_id = await _insert_user(pg_pool, role="hiring_manager")
    # Deliberately NOT assigned -- no job_assignees row for this (job, user).

    async with pg_pool.acquire() as conn:
        await set_shortlist_awaiting_llm(conn, job_id, reason="empty response")
        with pytest.raises(NotFoundError):
            await get_shortlist_state(conn, job_id, user_id=unassigned_hm_id)


@pytest.mark.asyncio
async def test_get_shortlist_state_unscoped_user_id_none_is_unchanged(
    pg_pool: asyncpg.Pool,
) -> None:
    """admin/recruiter/auditor pass user_id=None -- today's unscoped
    behaviour must be byte-identical (no predicate, sees every job's
    state)."""
    from src.services.shortlist_service import (
        get_shortlist_state,
        set_shortlist_awaiting_llm,
    )

    job_id = await _insert_job(pg_pool)

    async with pg_pool.acquire() as conn:
        await set_shortlist_awaiting_llm(conn, job_id, reason="empty response")
        state = await get_shortlist_state(conn, job_id, user_id=None)

    assert state is not None
    assert state.state == "awaiting_llm"


@pytest.mark.asyncio
async def test_get_shortlist_state_scoped_nonexistent_job_is_not_found_same_as_unassigned(  # noqa: E501
    pg_pool: asyncpg.Pool,
) -> None:
    """The oracle this closes: a scoped hiring_manager must see IDENTICAL
    NotFoundError for "job exists but I'm not assigned" and "job doesn't
    exist at all" -- no observable difference either could reveal."""
    from src.errors import NotFoundError
    from src.services.shortlist_service import get_shortlist_state

    hm_id = await _insert_user(pg_pool, role="hiring_manager")

    async with pg_pool.acquire() as conn:
        with pytest.raises(NotFoundError):
            await get_shortlist_state(conn, uuid4(), user_id=hm_id)
