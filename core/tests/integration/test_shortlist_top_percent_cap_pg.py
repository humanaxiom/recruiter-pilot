"""Integration tests -- the top-P% shortlist cap, end to end against REAL
Postgres + REAL Neo4j (testcontainers), mirroring the fixture wiring in
``test_matching_orchestrator.py`` (``generate_shortlist``) and
``test_shortlist_persistence_pg.py`` (the ``shortlist_job`` worker task).

Slice A (already GREEN on this branch) added ``jobs.shortlist_top_percent``
as a schema-only column. Slice B wires it into the ranking engine
(``generate_shortlist`` gains a ``top_percent`` keyword) and the worker
(``shortlist_job`` reads the job's own column and forwards it). Every test
below is expected to FAIL (RED) until both land:

* the ``generate_shortlist(..., top_percent=...)`` calls fail with
  ``TypeError: unexpected keyword argument 'top_percent'`` -- the parameter
  does not exist yet.
* the ``shortlist_job`` end-to-end tests fail because ALL 10 candidates are
  persisted regardless of the job's ``shortlist_top_percent`` value -- there
  is no cap wired into the worker path at all yet.

**Deterministic ranking, without depending on the real scoring formula's
internals.** Ten candidates are seeded against a job that requires 9 skills;
candidate ``i`` (0-indexed, i in 0..9) is HAS_SKILL-linked to the first
``min(i, 9)`` of them, so candidate 9 matches all 9 required skills and
candidate 0 matches none -- more matched must-have skills can only ever
raise (never lower) the structured skill sub-score, so candidates 9..0 is a
STRICT descending order regardless of the exact arithmetic. Every other
sub-score is held uniform across all ten candidates (no ``min_years``/
``education`` requirement on the job, identical summary embeddings, no
recent-role title, and an LLM stub that returns the same empty
``EvidenceObject`` for everyone) so nothing else can perturb that order.
This is the same seeding technique
``test_generate_shortlist_end_to_end_orders_candidates_and_shapes_breakdown``
uses for its strong/weak pair, generalised to ten candidates.
"""

from __future__ import annotations

import json
import re
import uuid
import zlib
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import asyncpg
import pytest
from neo4j import AsyncDriver, AsyncGraphDatabase
from testcontainers.neo4j import Neo4jContainer
from testcontainers.postgres import PostgresContainer

from src.models.ddl import init_schema
from src.pipeline.matching.orchestrator import (
    MatchingContext,
    generate_shortlist,
    match_resume_to_jobs,
)
from src.prompts import RenderedPrompt
from src.schemas.matching import EvidenceObject
from src.settings import get_settings
from src.worker.neo4j_bootstrap import bootstrap_neo4j_schema

_DIM = get_settings().llm_embedding_dim
_N_CANDIDATES = 10
_N_SKILLS = 9  # candidate i matches min(i, 9) of these, i in 0..9


def _vec(seed: int) -> list[float]:
    import random

    rng = random.Random(seed)
    return [rng.uniform(-1.0, 1.0) for _ in range(_DIM)]


# ── fixtures (same wiring as test_matching_orchestrator.py) ─────────────────


@pytest.fixture(scope="session")
def pg_dsn() -> Iterator[str]:
    with PostgresContainer("postgres:16-alpine") as pg:
        yield re.sub(r"\+\w+", "", pg.get_connection_url(), count=1)


@pytest.fixture(scope="session")
def neo4j_container() -> Iterator[Neo4jContainer]:
    with Neo4jContainer("neo4j:5-community") as container:
        yield container


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


@pytest.fixture
async def neo4j_driver(neo4j_container: Neo4jContainer) -> AsyncIterator[AsyncDriver]:
    driver = AsyncGraphDatabase.driver(
        neo4j_container.get_connection_url(), auth=("neo4j", neo4j_container.password)
    )
    await bootstrap_neo4j_schema(driver)
    async with driver.session() as session:
        await session.run("MATCH (n) DETACH DELETE n")
    try:
        yield driver
    finally:
        await driver.close()


# ── seeding helpers ──────────────────────────────────────────────────────


async def _insert_job(
    pool: asyncpg.Pool,
    *,
    required_skills: list[dict[str, Any]],
    shortlist_top_percent: int | None = None,
) -> UUID:
    description_parsed = {
        "required_skills": required_skills,
        "nice_to_have_skills": [],
        "min_years_experience": None,
        "education": {"min_level": None, "fields": []},
        "location": None,
        "remote_policy": None,
        "responsibilities": [],
    }
    async with pool.acquire() as conn:
        if shortlist_top_percent is None:
            job_id: UUID = await conn.fetchval(
                "INSERT INTO jobs (title, description_raw, description_parsed) "
                "VALUES ($1, $2, $3::jsonb) RETURNING id",
                "Senior Backend Engineer",
                "raw jd text (irrelevant to these tests)",
                json.dumps(description_parsed),
            )
        else:
            job_id = await conn.fetchval(
                "INSERT INTO jobs (title, description_raw, description_parsed, "
                "shortlist_top_percent) VALUES ($1, $2, $3::jsonb, $4) RETURNING id",
                "Senior Backend Engineer",
                "raw jd text (irrelevant to these tests)",
                json.dumps(description_parsed),
                shortlist_top_percent,
            )
    return job_id


async def _insert_resume(pool: asyncpg.Pool, job_id: UUID) -> UUID:
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
            f"resumes/{uuid.uuid4().hex}.pdf",
            uuid.uuid4().hex,
            json.dumps(
                {
                    "total_years_experience": None,
                    "education": [],
                    "experience": [],
                    "chunks": [],
                    "cover_letter_chunks": [],
                }
            ),
        )
    return resume_id


async def _seed_job_node(
    driver: AsyncDriver, job_id: UUID, *, emb: list[float]
) -> None:
    async with driver.session() as session:
        await session.run(
            "MERGE (j:Job {id: $jid}) SET j.summary_embedding = $emb",
            jid=str(job_id),
            emb=emb,
        )


async def _seed_resume_node(
    driver: AsyncDriver, resume_id: UUID, job_id: UUID, *, emb: list[float]
) -> None:
    async with driver.session() as session:
        await session.run(
            "MERGE (r:Resume {id: $rid}) "
            "SET r.summary_embedding = $emb, r.job_id = $jid",
            rid=str(resume_id),
            jid=str(job_id),
            emb=emb,
        )


async def _seed_requires(driver: AsyncDriver, job_id: UUID, canonical_key: str) -> None:
    async with driver.session() as session:
        await session.run(
            """
            MERGE (s:Skill {canonical_key: $key})
            WITH s
            MATCH (j:Job {id: $jid})
            MERGE (j)-[r:REQUIRES]->(s)
            SET r.min_years = 1, r.is_must_have = true
            """,
            key=canonical_key,
            jid=str(job_id),
        )


async def _seed_has_skill(
    driver: AsyncDriver, resume_id: UUID, canonical_key: str, *, current_year: int
) -> None:
    async with driver.session() as session:
        await session.run(
            """
            MERGE (s:Skill {canonical_key: $key})
            WITH s
            MERGE (r:Resume {id: $rid})
            MERGE (r)-[h:HAS_SKILL]->(s)
            SET h.years = 5, h.last_used_year = $year
            """,
            key=canonical_key,
            rid=str(resume_id),
            year=current_year,
        )


def _make_embedder() -> MagicMock:
    async def _embed(texts: Any) -> list[list[float]]:
        return [_vec(zlib.crc32(t.encode())) for t in texts]

    return MagicMock(embed=AsyncMock(side_effect=_embed))


def _make_llm() -> MagicMock:
    return MagicMock(chat_json=AsyncMock(return_value=EvidenceObject()))


def _ctx(
    conn: asyncpg.pool.PoolConnectionProxy, neo4j: AsyncDriver, **overrides: Any
) -> MatchingContext:
    kwargs: dict[str, Any] = {
        "db": conn,
        "neo4j": neo4j,
        "llm": _make_llm(),
        "embedder": _make_embedder(),
        "model_gen": "test-gen",
        "model_emb": "test-emb",
    }
    kwargs.update(overrides)
    return MatchingContext(**kwargs)


_STUBBED_PROMPT = RenderedPrompt(version="test-stub", system="system", user="user")

_SKILL_KEYS = [f"topcap-skill-{i}" for i in range(_N_SKILLS)]


async def _seed_monotonic_pool(
    pg_pool: asyncpg.Pool,
    neo4j_driver: AsyncDriver,
    *,
    shortlist_top_percent: int | None = None,
) -> tuple[UUID, list[UUID]]:
    """Seeds one job requiring ``_N_SKILLS`` skills and ``_N_CANDIDATES``
    résumés, where résumé index ``i`` matches exactly ``i`` of them. Returns
    ``(job_id, resume_ids)`` where ``resume_ids[i]`` is the résumé matching
    ``i`` skills -- so the expected best-to-worst order is
    ``list(reversed(resume_ids))``."""
    required_skills = [{"name": key, "min_years": None} for key in _SKILL_KEYS]
    job_id = await _insert_job(
        pg_pool,
        required_skills=required_skills,
        shortlist_top_percent=shortlist_top_percent,
    )
    shared_emb = _vec(999)
    await _seed_job_node(neo4j_driver, job_id, emb=shared_emb)
    for key in _SKILL_KEYS:
        await _seed_requires(neo4j_driver, job_id, key)

    current_year = 2026
    resume_ids: list[UUID] = []
    for i in range(_N_CANDIDATES):
        resume_id = await _insert_resume(pg_pool, job_id)
        await _seed_resume_node(neo4j_driver, resume_id, job_id, emb=shared_emb)
        for key in _SKILL_KEYS[:i]:
            await _seed_has_skill(
                neo4j_driver, resume_id, key, current_year=current_year
            )
        resume_ids.append(resume_id)
    return job_id, resume_ids


# ── generate_shortlist: the cap, exercised against the real pipeline ───────


@pytest.mark.asyncio
async def test_top_percent_100_keeps_all_ten_and_matches_the_omitted_default(
    pg_pool: asyncpg.Pool, neo4j_driver: AsyncDriver
) -> None:
    """Eval-preservation guard: explicitly passing ``top_percent=100`` (the
    schema's own default) must be a complete no-op -- identical entries, in
    identical order, to calling with the parameter omitted entirely."""
    job_id, resume_ids = await _seed_monotonic_pool(pg_pool, neo4j_driver)
    expected_order = list(reversed(resume_ids))

    async with pg_pool.acquire() as conn:
        ctx = _ctx(conn, neo4j_driver)
        with patch(
            "src.pipeline.matching.orchestrator.load_prompt",
            return_value=_STUBBED_PROMPT,
        ):
            result_omitted = await generate_shortlist(job_id, ctx)

    async with pg_pool.acquire() as conn:
        ctx = _ctx(conn, neo4j_driver)
        with patch(
            "src.pipeline.matching.orchestrator.load_prompt",
            return_value=_STUBBED_PROMPT,
        ):
            result_100 = await generate_shortlist(job_id, ctx, top_percent=100)

    assert len(result_omitted.entries) == _N_CANDIDATES
    assert [e.resume_id for e in result_omitted.entries] == expected_order
    assert [e.resume_id for e in result_100.entries] == [
        e.resume_id for e in result_omitted.entries
    ]


@pytest.mark.asyncio
async def test_top_percent_20_keeps_exactly_the_top_two_of_ten(
    pg_pool: asyncpg.Pool, neo4j_driver: AsyncDriver
) -> None:
    job_id, resume_ids = await _seed_monotonic_pool(pg_pool, neo4j_driver)
    expected_top_two = list(reversed(resume_ids))[:2]

    async with pg_pool.acquire() as conn:
        ctx = _ctx(conn, neo4j_driver)
        with patch(
            "src.pipeline.matching.orchestrator.load_prompt",
            return_value=_STUBBED_PROMPT,
        ):
            result = await generate_shortlist(job_id, ctx, top_percent=20)

    assert (
        len(result.entries) == 2
    ), f"ceil(10 * 20 / 100) = 2 -- got {len(result.entries)} entries"
    assert [e.resume_id for e in result.entries] == expected_top_two
    assert [e.rank for e in result.entries] == [1, 2]


@pytest.mark.asyncio
async def test_top_percent_50_keeps_exactly_the_top_five_of_ten(
    pg_pool: asyncpg.Pool, neo4j_driver: AsyncDriver
) -> None:
    job_id, resume_ids = await _seed_monotonic_pool(pg_pool, neo4j_driver)
    expected_top_five = list(reversed(resume_ids))[:5]

    async with pg_pool.acquire() as conn:
        ctx = _ctx(conn, neo4j_driver)
        with patch(
            "src.pipeline.matching.orchestrator.load_prompt",
            return_value=_STUBBED_PROMPT,
        ):
            result = await generate_shortlist(job_id, ctx, top_percent=50)

    assert len(result.entries) == 5
    assert [e.resume_id for e in result.entries] == expected_top_five


@pytest.mark.asyncio
async def test_top_percent_1_keeps_exactly_the_single_best_candidate(
    pg_pool: asyncpg.Pool, neo4j_driver: AsyncDriver
) -> None:
    """The min-1 floor: ceil(10 * 1 / 100) = ceil(0.1) = 1."""
    job_id, resume_ids = await _seed_monotonic_pool(pg_pool, neo4j_driver)
    best = list(reversed(resume_ids))[0]

    async with pg_pool.acquire() as conn:
        ctx = _ctx(conn, neo4j_driver)
        with patch(
            "src.pipeline.matching.orchestrator.load_prompt",
            return_value=_STUBBED_PROMPT,
        ):
            result = await generate_shortlist(job_id, ctx, top_percent=1)

    assert len(result.entries) == 1
    assert result.entries[0].resume_id == best
    assert result.entries[0].rank == 1


# ── shortlist_job worker: reads the job's own column, persists the capped
#    count (not a canned/mocked orchestrator -- the REAL pipeline runs) ─────


@pytest.mark.asyncio
async def test_shortlist_job_persists_exactly_the_capped_rows_for_a_20_percent_job(
    pg_pool: asyncpg.Pool, neo4j_driver: AsyncDriver
) -> None:
    from src.worker.matching_tasks import shortlist_job

    job_id, resume_ids = await _seed_monotonic_pool(
        pg_pool, neo4j_driver, shortlist_top_percent=20
    )
    expected_top_two = set(list(reversed(resume_ids))[:2])

    ctx: dict[str, Any] = {
        "pg_pool": pg_pool,
        "neo4j": neo4j_driver,
        "llm": _make_llm(),
        "embedder": _make_embedder(),
    }
    with patch(
        "src.pipeline.matching.orchestrator.load_prompt",
        return_value=_STUBBED_PROMPT,
    ):
        result = await shortlist_job(ctx, str(job_id))

    assert result == "persisted"
    async with pg_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT resume_id FROM shortlist_entries WHERE job_id = $1", job_id
        )
    persisted_ids = {r["resume_id"] for r in rows}
    assert len(persisted_ids) == 2, (
        "a job with shortlist_top_percent=20 over a 10-candidate pool must "
        f"persist exactly ceil(10*0.2)=2 rows -- got {len(persisted_ids)}: "
        f"{persisted_ids!r}"
    )
    assert persisted_ids == expected_top_two


@pytest.mark.asyncio
async def test_shortlist_job_default_top_percent_persists_all_ten(
    pg_pool: asyncpg.Pool, neo4j_driver: AsyncDriver
) -> None:
    """The SAME pool, with the job's shortlist_top_percent left at its DDL
    default (100) -- proves the cap is really conditional on the column, not
    always-on, and that the default path stays byte-identical to today."""
    from src.worker.matching_tasks import shortlist_job

    job_id, resume_ids = await _seed_monotonic_pool(pg_pool, neo4j_driver)

    ctx: dict[str, Any] = {
        "pg_pool": pg_pool,
        "neo4j": neo4j_driver,
        "llm": _make_llm(),
        "embedder": _make_embedder(),
    }
    with patch(
        "src.pipeline.matching.orchestrator.load_prompt",
        return_value=_STUBBED_PROMPT,
    ):
        result = await shortlist_job(ctx, str(job_id))

    assert result == "persisted"
    async with pg_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT resume_id FROM shortlist_entries WHERE job_id = $1", job_id
        )
    assert {r["resume_id"] for r in rows} == set(resume_ids)


# ── reverse match must NEVER be capped by a job's shortlist_top_percent ────


@pytest.mark.asyncio
async def test_match_resume_to_jobs_ignores_every_jobs_shortlist_top_percent(
    pg_pool: asyncpg.Pool, neo4j_driver: AsyncDriver
) -> None:
    """Ten candidate JOBS (not résumés) for one résumé, EVERY job set to the
    most aggressive cap (1%) -- reverse match must still return all ten job
    entries. ``shortlist_top_percent`` is a forward-shortlist job property;
    it must never reach into (or truncate) the reverse-match result set."""
    job_ids: list[UUID] = []
    resume_emb = _vec(1234)
    owner_job_id = await _insert_job(pg_pool, required_skills=[])
    resume_id = await _insert_resume(pg_pool, owner_job_id)
    async with neo4j_driver.session() as session:
        await session.run(
            "MERGE (r:Resume {id: $rid}) SET r.summary_embedding = $emb",
            rid=str(resume_id),
            emb=resume_emb,
        )

    for _ in range(_N_CANDIDATES):
        job_id = await _insert_job(pg_pool, required_skills=[], shortlist_top_percent=1)
        await _seed_job_node(neo4j_driver, job_id, emb=resume_emb)
        job_ids.append(job_id)

    async with pg_pool.acquire() as conn:
        ctx = _ctx(conn, neo4j_driver)
        with patch(
            "src.pipeline.matching.orchestrator.load_prompt",
            return_value=_STUBBED_PROMPT,
        ):
            result = await match_resume_to_jobs(
                resume_id, ctx, allowed_job_ids=set(job_ids)
            )

    assert len(result.entries) == _N_CANDIDATES, (
        "reverse match must return every candidate job regardless of any "
        f"job's shortlist_top_percent -- got {len(result.entries)} of "
        f"{_N_CANDIDATES}"
    )
