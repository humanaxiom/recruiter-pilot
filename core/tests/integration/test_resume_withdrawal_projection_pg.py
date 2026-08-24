"""Integration tests — the résumé-withdrawal un-project/reinstate-replay
lifecycle (ADR-026 decisions 1-3, FU-8) against REAL Postgres + REAL Neo4j
(testcontainers), driven through the real outbox drainer
(``src.worker.graph_tasks.project_to_graph``).

What a REAL Postgres + REAL Neo4j prove that the mocked unit tests
(``test_worker_project_resume.py``'s ``unproject_resume`` tests,
``test_worker_project_to_graph.py``'s dispatch tests,
``test_services_resume_withdraw.py``) structurally cannot:

* **The exclusion is real, not merely "a delete statement was issued".**
  After withdraw + drain, ``stage1_coarse`` — the SAME function
  ``generate_shortlist`` calls — no longer returns the résumé, AND a direct
  ``resume_summary_idx`` vector query doesn't either. A no-op
  ``unproject_resume`` mutant (or one that deletes the wrong node) passes
  every mocked-Cypher unit assertion but fails THIS test, because the real
  Neo4j vector index still has the node in it.
* **Reinstate really replays, never re-embeds.** After reinstate + drain, the
  résumé is back in the recall set with the EXACT SAME
  ``summary_embedding`` it had before withdrawal — proving byte-identical
  recall restoration, not merely "some vector was written."

None of ``unproject_resume``/the drainer's ``resume.withdrawn`` branch/
``withdraw_resume``/``reinstate_resume`` exist yet — every test below fails
at import or at the first call. RED half of the TDD cycle.
"""

from __future__ import annotations

import random
import re
import uuid
import zlib
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest
from neo4j import AsyncDriver, AsyncGraphDatabase
from testcontainers.neo4j import Neo4jContainer
from testcontainers.postgres import PostgresContainer

from src.models.ddl import init_schema
from src.pipeline.matching.orchestrator import stage1_coarse
from src.services import outbox_service
from src.settings import get_settings
from src.worker.graph_tasks import project_to_graph
from src.worker.neo4j_bootstrap import bootstrap_neo4j_schema

_DIM = get_settings().llm_embedding_dim

# A real ``users`` row backing the ``actor_kind='user'`` audit rows the
# withdraw/reinstate calls below write. ``audit_log.actor_user_id`` carries a
# FK to ``users`` (FU-5, ADR-019 §6); in production the route always supplies a
# real CAS-session ``user.id``. At the service layer these tests must seed the
# referenced row for the FK to hold — a precondition only, no assertion change.
_ACTOR_ID = uuid.UUID("a11ce000-0000-4000-8000-000000000002")


def _vec(seed: int) -> list[float]:
    """Deterministic near-orthogonal 768-d vector — same technique as
    ``test_graph_projection_e2e.py``'s ``_vec`` (see that module's docstring
    for why a per-seed ``random.Random`` draw, not a linear ramp, is used)."""
    rng = random.Random(seed)
    return [rng.uniform(-1.0, 1.0) for _ in range(_DIM)]


# ── fixtures ─────────────────────────────────────────────────────────────


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
        await conn.execute("TRUNCATE jobs, resumes, outbox, audit_log CASCADE")
        await conn.execute(
            "INSERT INTO users (id, cas_username, role) "
            "VALUES ($1, 'withdrawal-projection-test-actor', 'recruiter') "
            "ON CONFLICT (id) DO NOTHING",
            _ACTOR_ID,
        )
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


def _make_llm() -> MagicMock:
    out = MagicMock(match=None)
    return MagicMock(chat_json=AsyncMock(return_value=out))


def _make_embedder() -> MagicMock:
    async def _embed(texts: Any) -> list[list[float]]:
        return [_vec(zlib.crc32(t.encode())) for t in texts]

    return MagicMock(embed=AsyncMock(side_effect=_embed))


def _ctx(pg_pool: asyncpg.Pool, neo4j_driver: AsyncDriver) -> dict[str, Any]:
    return {
        "pg_pool": pg_pool,
        "neo4j": neo4j_driver,
        "llm": _make_llm(),
        "embedder": _make_embedder(),
    }


# ── Postgres seeding helpers ─────────────────────────────────────────────


async def _insert_job(pool: asyncpg.Pool) -> uuid.UUID:
    async with pool.acquire() as conn:
        job_id: uuid.UUID = await conn.fetchval(
            "INSERT INTO jobs (title, description_raw) VALUES ($1, $2) RETURNING id",
            "Senior Backend Engineer",
            "Build and operate REST APIs and data pipelines on PostgreSQL.",
        )
    return job_id


async def _enqueue_job_parsed(pool: asyncpg.Pool, job_id: uuid.UUID) -> None:
    payload = {
        "embedding": _vec(2),
        "extracted": {
            "title": "Senior Backend Engineer",
            "required_skills": [{"name": "python", "min_years": 4}],
            "nice_to_have_skills": [],
            "min_years_experience": 4,
            "education": {"min_level": "bachelors", "fields": []},
            "location": None,
            "remote_policy": None,
            "responsibilities": [],
        },
        "prompt_version": "jd_extract_v1",
    }
    async with pool.acquire() as conn:
        await outbox_service.enqueue_outbox(
            conn,
            aggregate="job",
            aggregate_id=job_id,
            event_type="job.parsed",
            payload=payload,
        )


async def _insert_resume(pool: asyncpg.Pool, job_id: uuid.UUID) -> uuid.UUID:
    async with pool.acquire() as conn:
        resume_id: uuid.UUID = await conn.fetchval(
            """
            INSERT INTO resumes (
                job_id, blob_key, original_filename, mime_type,
                file_size_bytes, sha256, consent_acknowledged, status
            ) VALUES ($1, $2, 'resume.pdf', 'application/pdf', 1024, $3, TRUE, 'parsed')
            RETURNING id
            """,
            job_id,
            f"resumes/{uuid.uuid4().hex}.pdf",
            uuid.uuid4().hex,
        )
    return resume_id


async def _enqueue_resume_parsed(
    pool: asyncpg.Pool, *, resume_id: uuid.UUID, job_id: uuid.UUID, summary_seed: int
) -> dict[str, Any]:
    payload = {
        "parsed": {
            "total_years_experience": 6,
            "skills": [
                {
                    "name": "python",
                    "years": 6,
                    "last_used_year": 2026,
                    "evidence_chunk_ids": ["c_001"],
                },
            ],
            "experience": [],
            "education": [],
            "chunks": [{"id": "c_001", "section": "experience", "page": 1}],
            "cover_letter_chunks": [],
        },
        "summary_emb": _vec(summary_seed),
        "chunk_embs": {"c_001": _vec(summary_seed + 1000)},
        "prompt_version": "resume_core_v1+resume_skills_v2",
        "job_id": str(job_id),
    }
    async with pool.acquire() as conn:
        await outbox_service.enqueue_outbox(
            conn,
            aggregate="resume",
            aggregate_id=resume_id,
            event_type="resume.parsed",
            payload=payload,
        )
    return payload


async def _mark_all_delivered(pool: asyncpg.Pool, resume_id: uuid.UUID) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE outbox SET delivered_at = now() "
            "WHERE aggregate_id = $1 AND delivered_at IS NULL",
            resume_id,
        )


async def _resume_summary_embedding(
    driver: AsyncDriver, resume_id: uuid.UUID
) -> list[float] | None:
    async with driver.session() as session:
        result = await session.run(
            "MATCH (r:Resume {id: $rid}) RETURN r.summary_embedding AS emb",
            rid=str(resume_id),
        )
        record = await result.single()
    if record is None:
        return None
    emb = record["emb"]
    return list(emb) if emb is not None else None


async def _direct_vector_query_hits(
    driver: AsyncDriver, *, query_vec: list[float], resume_id: uuid.UUID
) -> bool:
    """A raw ``resume_summary_idx`` query — proves the node is (or isn't) in
    the vector index at all, independent of how the orchestrator retrieves.

    Still a meaningful ADR-026 assertion after ROADMAP A4 M2, but for a
    narrower reason than when it was written: ``stage1_coarse`` no longer reads
    this index at all (it now scores a job-scoped MATCH directly), so this is
    no longer "the same query with the WHERE removed". It is now an independent
    check that un-projection really removed the NODE — if it had merely been
    detached from the job, ``stage1_coarse`` would stop returning it while the
    résumé's embedding sat in the index indefinitely."""
    async with driver.session() as session:
        result = await session.run(
            """
            CALL db.index.vector.queryNodes('resume_summary_idx', 20, $vec)
            YIELD node
            RETURN node.id AS id
            """,
            vec=query_vec,
        )
        ids = {record["id"] async for record in result}
    return str(resume_id) in ids


# ── the full lifecycle: project -> withdraw+drain -> reinstate+drain ──────


@pytest.mark.asyncio
async def test_withdraw_then_drain_excludes_the_resume_from_stage1_coarse(
    pg_pool: asyncpg.Pool, neo4j_driver: AsyncDriver
) -> None:
    from src.services.resume_service import withdraw_resume

    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume(pg_pool, job_id)
    await _enqueue_job_parsed(pg_pool, job_id)
    await _enqueue_resume_parsed(
        pg_pool, resume_id=resume_id, job_id=job_id, summary_seed=101
    )

    ctx = _ctx(pg_pool, neo4j_driver)
    delivered = await project_to_graph(ctx, batch=10)
    assert delivered == 2

    before = await stage1_coarse(neo4j_driver, job_id, k=10)
    assert resume_id in {c.resume_id for c in before}

    async with pg_pool.acquire() as conn:
        await withdraw_resume(
            conn,
            resume_id,
            reason="withdrew before shortlist",
            actor_kind="user",
            actor_user_id=_ACTOR_ID,
            actor_service=None,
        )

    delivered = await project_to_graph(ctx, batch=10)
    assert delivered == 1, "the resume.withdrawn outbox row must be drained"

    after = await stage1_coarse(neo4j_driver, job_id, k=10)
    assert resume_id not in {
        c.resume_id for c in after
    }, "a withdrawn+drained résumé must no longer appear in stage1_coarse"


@pytest.mark.asyncio
async def test_withdraw_then_drain_removes_the_node_from_the_real_vector_index(
    pg_pool: asyncpg.Pool, neo4j_driver: AsyncDriver
) -> None:
    """Mutation-proof against a mutant ``unproject_resume`` that no-ops (or
    deletes the wrong node): a DIRECT ``resume_summary_idx`` query, bypassing
    ``stage1_coarse``'s job-scoping WHERE entirely, must also stop returning
    this résumé's id."""
    from src.services.resume_service import withdraw_resume

    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume(pg_pool, job_id)
    summary_seed = 202
    await _enqueue_resume_parsed(
        pg_pool, resume_id=resume_id, job_id=job_id, summary_seed=summary_seed
    )

    ctx = _ctx(pg_pool, neo4j_driver)
    await project_to_graph(ctx, batch=10)

    query_vec = _vec(summary_seed)
    assert await _direct_vector_query_hits(
        neo4j_driver, query_vec=query_vec, resume_id=resume_id
    )

    async with pg_pool.acquire() as conn:
        await withdraw_resume(
            conn,
            resume_id,
            reason=None,
            actor_kind="user",
            actor_user_id=_ACTOR_ID,
            actor_service=None,
        )
    await project_to_graph(ctx, batch=10)

    assert not await _direct_vector_query_hits(
        neo4j_driver, query_vec=query_vec, resume_id=resume_id
    ), "the résumé node must no longer be findable via the real vector index"


@pytest.mark.asyncio
async def test_unproject_never_deletes_the_shared_skill_node(
    pg_pool: asyncpg.Pool, neo4j_driver: AsyncDriver
) -> None:
    """A ``Skill`` node is shared across every résumé that has ever claimed
    it — un-projecting one candidate must never remove the vocabulary node,
    or a batch-mate résumé's HAS_SKILL edge would dangle."""
    from src.services.resume_service import withdraw_resume

    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume(pg_pool, job_id)
    await _enqueue_resume_parsed(
        pg_pool, resume_id=resume_id, job_id=job_id, summary_seed=303
    )

    ctx = _ctx(pg_pool, neo4j_driver)
    await project_to_graph(ctx, batch=10)

    async with neo4j_driver.session() as session:
        result = await session.run(
            "MATCH (s:Skill {canonical_key: 'python'}) RETURN count(s) AS n"
        )
        record = await result.single()
    assert record is not None
    assert record["n"] == 1

    async with pg_pool.acquire() as conn:
        await withdraw_resume(
            conn,
            resume_id,
            reason=None,
            actor_kind="user",
            actor_user_id=_ACTOR_ID,
            actor_service=None,
        )
    await project_to_graph(ctx, batch=10)

    async with neo4j_driver.session() as session:
        result = await session.run(
            "MATCH (s:Skill {canonical_key: 'python'}) RETURN count(s) AS n"
        )
        record = await result.single()
    assert record is not None
    assert record["n"] == 1, "the shared Skill node must survive un-projection"


@pytest.mark.asyncio
async def test_reinstate_then_drain_restores_recall_with_the_same_embedding(
    pg_pool: asyncpg.Pool, neo4j_driver: AsyncDriver
) -> None:
    """The end-to-end replay proof: withdraw + drain (excluded), then
    reinstate + drain (included again) with the EXACT SAME
    ``summary_embedding`` — proving reinstate replays the stored vector
    rather than re-embedding (which would almost certainly produce a
    different vector even for identical text, and MUST NOT touch the
    embedder at all per the locked decision)."""
    from src.services.resume_service import reinstate_resume, withdraw_resume

    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume(pg_pool, job_id)
    await _enqueue_job_parsed(pg_pool, job_id)
    await _enqueue_resume_parsed(
        pg_pool, resume_id=resume_id, job_id=job_id, summary_seed=404
    )

    ctx = _ctx(pg_pool, neo4j_driver)
    await project_to_graph(ctx, batch=10)
    original_embedding = await _resume_summary_embedding(neo4j_driver, resume_id)
    assert original_embedding is not None

    async with pg_pool.acquire() as conn:
        await withdraw_resume(
            conn,
            resume_id,
            reason=None,
            actor_kind="user",
            actor_user_id=_ACTOR_ID,
            actor_service=None,
        )
    await project_to_graph(ctx, batch=10)

    excluded = await stage1_coarse(neo4j_driver, job_id, k=10)
    assert resume_id not in {c.resume_id for c in excluded}

    async with pg_pool.acquire() as conn:
        await reinstate_resume(
            conn,
            resume_id,
            actor_kind="user",
            actor_user_id=_ACTOR_ID,
            actor_service=None,
        )
    delivered = await project_to_graph(ctx, batch=10)
    assert delivered == 1, "the replayed resume.parsed row must be drained"

    included_again = await stage1_coarse(neo4j_driver, job_id, k=10)
    assert resume_id in {
        c.resume_id for c in included_again
    }, "the résumé must be back in the recall set after reinstate + drain"

    restored_embedding = await _resume_summary_embedding(neo4j_driver, resume_id)
    assert restored_embedding is not None
    assert restored_embedding == pytest.approx(original_embedding), (
        "reinstate must REPLAY the stored embedding byte-for-byte, never " "re-embed"
    )


@pytest.mark.asyncio
async def test_reinstate_never_calls_the_embedder(
    pg_pool: asyncpg.Pool, neo4j_driver: AsyncDriver
) -> None:
    """Belt-and-braces on the locked decision, at the real-drainer level: the
    embedder mock must record ZERO calls whose text originates from a
    reinstate-triggered replay (the drainer's ``embedder`` handle is passed
    straight through to ``ctx`` — a re-embedding regression would call it
    again for the résumé's summary text)."""
    from src.services.resume_service import reinstate_resume, withdraw_resume

    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume(pg_pool, job_id)
    await _enqueue_resume_parsed(
        pg_pool, resume_id=resume_id, job_id=job_id, summary_seed=505
    )

    embedder = _make_embedder()
    ctx: dict[str, Any] = {
        "pg_pool": pg_pool,
        "neo4j": neo4j_driver,
        "llm": _make_llm(),
        "embedder": embedder,
    }
    await project_to_graph(ctx, batch=10)
    calls_before_reinstate = embedder.embed.await_count

    async with pg_pool.acquire() as conn:
        await withdraw_resume(
            conn,
            resume_id,
            reason=None,
            actor_kind="user",
            actor_user_id=_ACTOR_ID,
            actor_service=None,
        )
    await project_to_graph(ctx, batch=10)

    async with pg_pool.acquire() as conn:
        await reinstate_resume(
            conn,
            resume_id,
            actor_kind="user",
            actor_user_id=_ACTOR_ID,
            actor_service=None,
        )
    await project_to_graph(ctx, batch=10)

    assert (
        embedder.embed.await_count == calls_before_reinstate
    ), "reinstate + drain must not invoke the embedder at all"
