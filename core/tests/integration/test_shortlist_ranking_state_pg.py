"""Integration tests — ``jobs.shortlist_state = 'ranking'`` (real Postgres,
testcontainers): fix/regenerate-shortlist-no-feedback.

Root cause, measured live on the running stack: clicking "Regenerate
shortlist" enqueues real work and the worker really runs (0.3s POST, 78-119s
worker run, rows really written) — but the UI never updates, because
``shortlist_cards.html`` infers "a run is in flight" from ``not entries``,
which is only true on a first Generate. The fix records the fact instead of
inferring it: a real ``'ranking'`` value on ``jobs.shortlist_state`` (widened
CHECK — see ``test_schema.py``'s dedicated section), set by the API route
BEFORE it enqueues, and cleared/overwritten by the worker on every terminal
path.

None of the following exist yet — every test below fails either at the first
``jobs.shortlist_state = 'ranking'`` write (``asyncpg.CheckViolationError``
against the still-narrow real constraint) or at collection
(``shortlist_routes.router`` not yet setting the state before enqueue, no
staleness bound on ``get_shortlist_state``). RED half of the TDD cycle.

What a REAL Postgres proves that a mocked-conn unit test cannot: the widened
CHECK constraint actually accepts ``'ranking'`` end to end through the real
route + real worker + real advisory lock, and the staleness-bound read is a
real ``now() - shortlist_state_at`` comparison against a real timestamptz
column, not a mock told what to return.

Mirrors the fixture/helper patterns already established for this state
column: ``test_shortlist_fail_closed_pg.py`` (worker-against-real-Postgres,
fake Neo4j, ``_worker_ctx``), ``test_api_jobs_pg.py`` (real ASGI app + real
``pg_pool`` + a fake ``ArqRedis``), and ``test_job_lock_pg.py`` (the real
``pg_try_advisory_lock`` "already_running" proof, held on a SEPARATE
connection acquired directly from the pool).
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
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from testcontainers.postgres import PostgresContainer

from src.api.deps import Role, get_arq, resolve_role
from src.api.routes import shortlist as shortlist_routes
from src.errors import AppError
from src.models.ddl import init_schema
from src.models.pool import get_db
from src.pipeline.llm import LLMOutputInvalidError
from src.schemas.matching import EvidenceObjectIngest

# ── fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def pg_dsn() -> Iterator[str]:
    with PostgresContainer("postgres:16-alpine") as pg:
        yield re.sub(r"\+\w+", "", pg.get_connection_url(), count=1)


@pytest.fixture
async def pg_pool(pg_dsn: str) -> AsyncIterator[asyncpg.Pool]:
    pool = await asyncpg.create_pool(dsn=pg_dsn, min_size=4, max_size=8)
    await init_schema(pool)
    async with pool.acquire() as conn:
        await conn.execute("TRUNCATE jobs, resumes, outbox CASCADE")
    try:
        yield pool
    finally:
        await pool.close()


# ── seeding helpers (mirrors test_shortlist_fail_closed_pg.py) ────────────


async def _insert_job(pool: asyncpg.Pool, *, parsed: bool = True) -> UUID:
    """``parsed=True`` (the default) seeds a parsed JD; ``parsed=False`` seeds
    a job with ``description_parsed IS NULL`` (the not-yet-parsed case)."""
    parsed_json = '{"required_skills": [{"name": "Python"}]}' if parsed else None
    async with pool.acquire() as conn:
        job_id: UUID = await conn.fetchval(
            "INSERT INTO jobs (title, description_raw, description_parsed) "
            "VALUES ($1, $2, $3::jsonb) RETURNING id",
            "Senior Backend Engineer",
            "raw jd text (irrelevant to these tests)",
            parsed_json,
        )
    return job_id


async def _insert_resume_with_chunks(pool: asyncpg.Pool, job_id: UUID) -> UUID:
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


def _fake_neo4j(resume_id: UUID | None) -> MagicMock:
    """``resume_id=None`` -> stage 1 always yields zero candidates (the
    'empty' outcome). Otherwise mirrors
    ``test_shortlist_fail_closed_pg.py``'s ``_fake_neo4j``: stage 1's vector
    query (identified by the ``vec_score`` column it returns, NOT by Cypher
    text — see that file's docstring for why) yields exactly ``resume_id``."""
    session = MagicMock(name="session")

    async def _run(query: str, **_kwargs: Any) -> _AsyncRows:
        if "vec_score" in query and resume_id is not None:
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


async def _set_ranking(pool: asyncpg.Pool, job_id: UUID) -> None:
    """Stand-in for the route's own write (tested separately below) — used to
    seed the "a run is already in flight" precondition the worker-side tests
    need before invoking ``shortlist_job`` directly."""
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE jobs SET shortlist_state = 'ranking', "
            "shortlist_state_reason = NULL, shortlist_state_at = now() "
            "WHERE id = $1",
            job_id,
        )


async def _job_state_row(pool: asyncpg.Pool, job_id: UUID) -> Any:
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT shortlist_state, shortlist_state_reason, shortlist_state_at "
            "FROM jobs WHERE id = $1",
            job_id,
        )


# ── API route: sets 'ranking' BEFORE the worker ever runs ─────────────────


def _build_app(pool: asyncpg.Pool, *, arq: MagicMock) -> FastAPI:
    app = FastAPI()
    app.include_router(shortlist_routes.router)

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
async def test_generate_shortlist_route_sets_ranking_state_before_worker_runs(
    pg_pool: asyncpg.Pool,
) -> None:
    """``arq.enqueue_job`` is faked (no real worker in this test — the route
    itself is what's under test), so by construction the state written here
    is written before ANY worker could possibly have run: proving the route
    writes 'ranking' itself, synchronously, before it ever hands the job to
    the queue."""
    job_id = await _insert_job(pg_pool)
    arq = MagicMock(enqueue_job=AsyncMock())
    app = _build_app(pg_pool, arq=arq)

    async with await _client(app) as client:
        resp = await client.post(f"/jobs/{job_id}/shortlist")

    assert resp.status_code == 202
    arq.enqueue_job.assert_awaited_once_with("shortlist_job", str(job_id))

    row = await _job_state_row(pg_pool, job_id)
    assert row is not None
    assert row["shortlist_state"] == "ranking", (
        "POST /jobs/{id}/shortlist must record shortlist_state='ranking' "
        "before returning — the frontend's very first poll must already see "
        "it true, not race the worker to set it"
    )
    assert row["shortlist_state_at"] is not None


# ── worker: clears 'ranking' on every non-already_running terminal path ───


@pytest.mark.asyncio
async def test_shortlist_job_clears_ranking_state_on_persisted(
    pg_pool: asyncpg.Pool,
) -> None:
    from src.worker.matching_tasks import shortlist_job

    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume_with_chunks(pg_pool, job_id)
    await _set_ranking(pg_pool, job_id)

    ctx = _worker_ctx(pg_pool, _fake_neo4j(resume_id), _working_llm())
    result = await shortlist_job(ctx, str(job_id))

    assert result == "persisted"
    row = await _job_state_row(pg_pool, job_id)
    assert row is not None
    assert row["shortlist_state"] is None


@pytest.mark.asyncio
async def test_shortlist_job_clears_ranking_state_on_empty(
    pg_pool: asyncpg.Pool,
) -> None:
    from src.worker.matching_tasks import shortlist_job

    job_id = await _insert_job(pg_pool)
    await _set_ranking(pg_pool, job_id)

    # No candidate résumé at all -> stage 1 yields zero rows -> "empty".
    ctx = _worker_ctx(pg_pool, _fake_neo4j(None), _working_llm())
    result = await shortlist_job(ctx, str(job_id))

    assert result == "empty"
    row = await _job_state_row(pg_pool, job_id)
    assert row is not None
    assert row["shortlist_state"] is None


@pytest.mark.asyncio
async def test_shortlist_job_clears_ranking_state_on_not_parsed(
    pg_pool: asyncpg.Pool,
) -> None:
    from src.worker.matching_tasks import shortlist_job

    job_id = await _insert_job(pg_pool, parsed=False)
    await _set_ranking(pg_pool, job_id)

    ctx = _worker_ctx(pg_pool, _fake_neo4j(None), _working_llm())
    result = await shortlist_job(ctx, str(job_id))

    assert result == "not_parsed"
    row = await _job_state_row(pg_pool, job_id)
    assert row is not None
    assert row["shortlist_state"] is None


@pytest.mark.asyncio
async def test_shortlist_job_missing_row_does_not_raise_while_clearing_state(
    pg_pool: asyncpg.Pool,
) -> None:
    """A job deleted between enqueue and the worker picking it up: the
    'clear' UPDATE affects zero rows and must not raise."""
    from src.worker.matching_tasks import shortlist_job

    job_id = await _insert_job(pg_pool)
    async with pg_pool.acquire() as conn:
        await conn.execute("DELETE FROM jobs WHERE id = $1", job_id)

    ctx = _worker_ctx(pg_pool, _fake_neo4j(None), _working_llm())
    result = await shortlist_job(ctx, str(job_id))

    assert result == "missing"


# ── worker: already_running LEAVES 'ranking' set (another run owns it) ────


@pytest.mark.asyncio
async def test_shortlist_job_leaves_ranking_state_set_on_already_running(
    pg_pool: asyncpg.Pool,
) -> None:
    """Mirrors ``test_job_lock_pg.py``'s real-advisory-lock 'already_running'
    proof, but starting from a job that already carries 'ranking' (set by a
    concurrent duplicate's own enqueue) — the SECOND run's early return must
    not touch the state at all, since the FIRST run is still genuinely
    ranking."""
    from src.worker.job_lock import release_job_lock, try_job_lock
    from src.worker.matching_tasks import shortlist_job

    job_id = await _insert_job(pg_pool)
    await _set_ranking(pg_pool, job_id)

    holder = await pg_pool.acquire()
    assert await try_job_lock(holder, "shortlist", job_id) is True

    ctx = _worker_ctx(pg_pool, _fake_neo4j(None), _working_llm())
    try:
        result = await shortlist_job(ctx, str(job_id))
    finally:
        await release_job_lock(holder, "shortlist", job_id)
        await pg_pool.release(holder)

    assert result == "already_running"
    row = await _job_state_row(pg_pool, job_id)
    assert row is not None
    assert row["shortlist_state"] == "ranking", (
        "a concurrent duplicate's early return must leave the genuinely "
        "in-flight run's state untouched — clearing it here would blank the "
        "UI mid-run"
    )


# ── worker: fail-closed path ends at 'awaiting_llm', never 'ranking' ──────


@pytest.mark.asyncio
async def test_shortlist_job_awaiting_llm_path_overwrites_ranking_not_leaves_it(
    pg_pool: asyncpg.Pool,
) -> None:
    """Starting state is 'ranking' (as the route would have set it at
    enqueue) — after a Mode B LLM failure the row must read 'awaiting_llm',
    never still 'ranking'."""
    from src.pipeline.matching.orchestrator import RankingUnavailableError
    from src.worker.matching_tasks import shortlist_job

    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume_with_chunks(pg_pool, job_id)
    await _set_ranking(pg_pool, job_id)

    llm = _failing_llm(LLMOutputInvalidError("response content was empty"))
    ctx = _worker_ctx(pg_pool, _fake_neo4j(resume_id), llm)

    with pytest.raises(Retry) as exc_info:
        await shortlist_job(ctx, str(job_id))

    assert isinstance(exc_info.value.__cause__, RankingUnavailableError)
    row = await _job_state_row(pg_pool, job_id)
    assert row is not None
    assert row["shortlist_state"] == "awaiting_llm"
    assert row["shortlist_state"] != "ranking"


# ── get_shortlist_state: the staleness bound (crashed-worker backstop) ────


@pytest.mark.asyncio
async def test_get_shortlist_state_reports_a_stale_ranking_row_as_not_ranking(
    pg_pool: asyncpg.Pool,
) -> None:
    """A 'ranking' row whose ``shortlist_state_at`` is well past the default
    staleness bound (``Settings().shortlist_ranking_stale_after_s`` — 3600s)
    cannot still be genuinely in flight; the worker would have been killed by
    its own job timeout. ``get_shortlist_state`` must report NOT ranking —
    but must NOT clear the row (no silent mutation of the record)."""
    from src.services.shortlist_service import get_shortlist_state

    job_id = await _insert_job(pg_pool)
    stale_at = dt.datetime.now(dt.UTC) - dt.timedelta(hours=2)
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "UPDATE jobs SET shortlist_state = 'ranking', "
            "shortlist_state_reason = NULL, shortlist_state_at = $2 "
            "WHERE id = $1",
            job_id,
            stale_at,
        )
        state = await get_shortlist_state(conn, job_id)

    assert state is None, (
        "a 'ranking' row older than the staleness bound must read back as "
        "NOT ranking, or a crashed worker permanently pins the UI on "
        "'Regenerating…'"
    )

    # The record itself must be left alone — reported as not-ranking, never
    # silently cleared (the spec's explicit "do not silently clear" rule).
    row = await _job_state_row(pg_pool, job_id)
    assert row is not None
    assert row["shortlist_state"] == "ranking"


@pytest.mark.asyncio
async def test_get_shortlist_state_reports_a_fresh_ranking_row_as_ranking(
    pg_pool: asyncpg.Pool,
) -> None:
    """The positive complement: a 'ranking' row well within the staleness
    bound must read back as genuinely ranking."""
    from src.services.shortlist_service import get_shortlist_state

    job_id = await _insert_job(pg_pool)
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "UPDATE jobs SET shortlist_state = 'ranking', "
            "shortlist_state_reason = NULL, shortlist_state_at = now() "
            "WHERE id = $1",
            job_id,
        )
        state = await get_shortlist_state(conn, job_id)

    assert state is not None
    assert state.state == "ranking"
