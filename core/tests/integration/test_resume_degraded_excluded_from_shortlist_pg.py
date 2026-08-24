"""Integration tests -- FU-7 §4 / ADR-030, ranking exclusion, against REAL
Postgres + REAL Neo4j (testcontainers).

Mirrors ``test_graph_projection_e2e.py`` (the ``parse_resume`` ->
``project_to_graph`` real pipeline) and
``test_matching_orchestrator.py``'s ``generate_shortlist`` happy path
(``_seed_job_node``/``_seed_requires``/``_ctx`` helpers) and
``test_parse_resume_withdrawn_race_pg.py``'s ``parse_resume`` harness
(``BlobStore`` + a schema-dispatching ``llm.chat_json`` stub).

Proves the load-bearing exclusion end to end, via the REAL production path
for BOTH résumés (never a hand-rolled Neo4j seed for the résumé under test):
skills extraction "degrading" (the ``resume_skills_v2``
``LLMOutputInvalidError`` catch) must make ``parse_resume`` skip its
``resume.parsed`` outbox enqueue -- so the degraded résumé never gets a
``Resume`` node when the SAME real ``project_to_graph`` drain runs, and is
therefore absent from stage-1 recall / any shortlist ``generate_shortlist``
produces for that job. A non-degraded résumé, parsed and drained through the
exact same real pipeline, is projected and ranks normally.

None of the ``degraded`` plumbing exists yet -- today ``_extract_skills_merged``
swallows ``LLMOutputInvalidError`` and returns the deterministic-scan skills
with NO signal, so ``parse_resume`` treats BOTH résumés as an ordinary clean
parse: both enqueue ``resume.parsed``, both get a ``Resume`` node, both rank.
Every test below is expected to FAIL today. RED half of the TDD cycle.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import uuid
import zlib
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import asyncpg
import pytest
from neo4j import AsyncDriver, AsyncGraphDatabase
from testcontainers.neo4j import Neo4jContainer
from testcontainers.postgres import PostgresContainer

from src.models.ddl import init_schema
from src.pipeline.matching.orchestrator import MatchingContext, generate_shortlist
from src.pipeline.parsing import MIME_TXT
from src.prompts import RenderedPrompt
from src.schemas import (
    CandidateInfo,
    CoverLetterParsed,
    EvidenceObject,
    RequirementEvidence,
    ResumeCore,
    ResumeSkillDetail,
    ResumeSkillDetails,
)
from src.settings import get_settings
from src.storage.blob_store import BlobStore
from src.worker.graph_tasks import project_to_graph
from src.worker.neo4j_bootstrap import bootstrap_neo4j_schema
from src.worker.resume_tasks import parse_resume

PII_KEY = "b3VyLTMyLWJ5dGUtcGlpLWtleS1mb3ItdGVzdHM="
_DIM = get_settings().llm_embedding_dim

GOOD_RESUME_TEXT = """Summary
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

DEGRADED_RESUME_TEXT = """Summary
Backend engineer with hands-on Python delivery experience.

Experience
Backend Engineer at Widget Co
2018 - Present
Delivered Python services backed by PostgreSQL.

Education
BSc Computer Science, Another University, 2014

Skills
Python, PostgreSQL
"""

GOOD_CORE = ResumeCore(
    candidate=CandidateInfo(name="Good Candidate", email="good@example.org"),
    summary="Experienced backend engineer.",
    total_years_experience=6,
)
DEGRADED_CORE = ResumeCore(
    candidate=CandidateInfo(name="Degraded Candidate", email="degraded@example.org"),
    summary="Backend engineer with Python delivery experience.",
    total_years_experience=4,
)
GOOD_SKILLS_RESULT = ResumeSkillDetails(
    skills=[
        ResumeSkillDetail(name="Python", years=6),
        ResumeSkillDetail(name="PostgreSQL", years=5),
    ]
)


def _vec(seed: int) -> list[float]:
    """Deterministic near-orthogonal vector -- same technique as
    ``test_graph_projection_e2e.py``/``test_matching_orchestrator.py``."""
    rng = random.Random(seed)
    return [rng.uniform(-1.0, 1.0) for _ in range(_DIM)]


def _make_parse_llm(*, skills_fail: bool) -> MagicMock:
    """Schema-dispatching ``llm.chat_json`` stub for ``parse_resume`` --
    mirrors ``test_parse_resume_withdrawn_race_pg.py``'s ``_make_llm``. When
    ``skills_fail`` is True, the ``resume_skills_v2`` sub-call (schema
    ``ResumeSkillDetails``) raises ``LLMOutputInvalidError`` -- the EXACT
    catch this whole slice is about (``resume_tasks.py:164``)."""
    from src.pipeline.llm import LLMOutputInvalidError

    core = DEGRADED_CORE if skills_fail else GOOD_CORE

    async def _chat_json(messages: Any, schema: Any, **kwargs: Any) -> Any:
        if schema is ResumeCore:
            return core
        if schema is ResumeSkillDetails:
            if skills_fail:
                raise LLMOutputInvalidError("skills sub-call produced invalid json")
            return GOOD_SKILLS_RESULT
        if schema is CoverLetterParsed:
            return CoverLetterParsed()
        raise AssertionError(f"unexpected schema: {schema!r}")

    return MagicMock(chat_json=AsyncMock(side_effect=_chat_json))


def _make_parse_embedder() -> MagicMock:
    async def _embed(texts: Any) -> list[list[float]]:
        return [_vec(zlib.crc32(t.encode())) for t in texts]

    return MagicMock(embed=AsyncMock(side_effect=_embed))


def _make_shortlist_embedder() -> MagicMock:
    async def _embed(texts: Any) -> list[list[float]]:
        return [_vec(zlib.crc32(t.encode())) for t in texts]

    return MagicMock(embed=AsyncMock(side_effect=_embed))


def _make_shortlist_llm(evidence: EvidenceObject | None = None) -> MagicMock:
    return MagicMock(chat_json=AsyncMock(return_value=evidence or EvidenceObject()))


_STUBBED_PROMPT = RenderedPrompt(version="test-stub", system="system", user="user")


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


@pytest.fixture
def blob_store(tmp_path: Path) -> BlobStore:
    return BlobStore(tmp_path / "blobs")


async def _insert_job(pool: asyncpg.Pool) -> UUID:
    description_parsed = {
        "required_skills": [{"name": "Python", "min_years": 3}],
        "nice_to_have_skills": [],
        "min_years_experience": 3,
        "education": {"min_level": None, "fields": []},
        "location": None,
        "remote_policy": None,
        "responsibilities": [],
    }
    async with pool.acquire() as conn:
        job_id: UUID = await conn.fetchval(
            "INSERT INTO jobs (title, description_raw, description_parsed) "
            "VALUES ($1, $2, $3::jsonb) RETURNING id",
            "Senior Backend Engineer",
            "Build and operate REST APIs and data pipelines on PostgreSQL.",
            json.dumps(description_parsed),
        )
    return job_id


async def _seed_job_graph(driver: AsyncDriver, job_id: UUID) -> None:
    async with driver.session() as session:
        await session.run(
            "MERGE (j:Job {id: $jid}) SET j.summary_embedding = $emb",
            jid=str(job_id),
            emb=_vec(1),
        )
        await session.run(
            """
            MERGE (s:Skill {canonical_key: 'python'})
            WITH s
            MATCH (j:Job {id: $jid})
            MERGE (j)-[r:REQUIRES]->(s)
            SET r.min_years = 3, r.is_must_have = true
            """,
            jid=str(job_id),
        )


async def _insert_uploaded_resume(
    pool: asyncpg.Pool, job_id: UUID, *, blob_key: str, sha256: str, size: int
) -> UUID:
    async with pool.acquire() as conn:
        resume_id: UUID = await conn.fetchval(
            """
            INSERT INTO resumes (
                job_id, blob_key, original_filename, mime_type,
                file_size_bytes, sha256, consent_acknowledged
            ) VALUES ($1, $2, 'candidate.txt', $3, $4, $5, TRUE)
            RETURNING id
            """,
            job_id,
            blob_key,
            MIME_TXT,
            size,
            sha256,
        )
    return resume_id


async def _seed_and_parse_via_real_path(
    pg_pool: asyncpg.Pool,
    blob_store: BlobStore,
    job_id: UUID,
    *,
    text: str,
    skills_fail: bool,
) -> UUID:
    """Runs the REAL ``parse_resume`` worker task end to end (blob I/O, real
    text extraction/chunking, real ``_extract_skills_merged`` -- only
    ``llm.chat_json``/``embedder.embed`` are stubbed, exactly like
    ``test_parse_resume_withdrawn_race_pg.py``)."""
    data = text.encode("utf-8")
    blob_key = f"resumes/{uuid.uuid4().hex}.txt"
    await blob_store.put(blob_key, data)
    resume_id = await _insert_uploaded_resume(
        pg_pool,
        job_id,
        blob_key=blob_key,
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
    )
    ctx: dict[str, Any] = {
        "pg_pool": pg_pool,
        "llm": _make_parse_llm(skills_fail=skills_fail),
        "embedder": _make_parse_embedder(),
        "blob_store": blob_store,
    }
    with patch(
        "src.services.pii.get_settings", return_value=SimpleNamespace(pii_key=PII_KEY)
    ):
        result = await parse_resume(ctx, str(resume_id))
    assert result == "parsed"
    return resume_id


def _graph_ctx(pg_pool: asyncpg.Pool, neo4j_driver: AsyncDriver) -> dict[str, Any]:
    return {
        "pg_pool": pg_pool,
        "neo4j": neo4j_driver,
        "llm": MagicMock(),
        "embedder": MagicMock(),
    }


async def _resume_node_exists(driver: AsyncDriver, resume_id: UUID) -> bool:
    async with driver.session() as session:
        result = await session.run(
            "MATCH (r:Resume {id: $rid}) RETURN count(r) AS n", rid=str(resume_id)
        )
        record = await result.single()
    assert record is not None
    return bool(record["n"])


# ── the exclusion, at the graph-projection level ────────────────────────────


@pytest.mark.asyncio
async def test_degraded_resume_gets_no_resume_node_after_the_real_drain(
    pg_pool: asyncpg.Pool, neo4j_driver: AsyncDriver, blob_store: BlobStore
) -> None:
    job_id = await _insert_job(pg_pool)
    await _seed_job_graph(neo4j_driver, job_id)

    good_id = await _seed_and_parse_via_real_path(
        pg_pool, blob_store, job_id, text=GOOD_RESUME_TEXT, skills_fail=False
    )
    degraded_id = await _seed_and_parse_via_real_path(
        pg_pool, blob_store, job_id, text=DEGRADED_RESUME_TEXT, skills_fail=True
    )

    await project_to_graph(_graph_ctx(pg_pool, neo4j_driver), batch=10)

    assert await _resume_node_exists(neo4j_driver, good_id) is True, (
        "the non-degraded résumé must be projected -- a REAL Resume node "
        "after the real drain"
    )
    assert await _resume_node_exists(neo4j_driver, degraded_id) is False, (
        "a degraded résumé must NOT enqueue resume.parsed -- no Resume node "
        "should exist after the real drain (ADR-030 exclusion)"
    )


@pytest.mark.asyncio
async def test_degraded_resume_outbox_row_never_enqueued_so_nothing_to_drain(
    pg_pool: asyncpg.Pool, neo4j_driver: AsyncDriver, blob_store: BlobStore
) -> None:
    job_id = await _insert_job(pg_pool)

    degraded_id = await _seed_and_parse_via_real_path(
        pg_pool, blob_store, job_id, text=DEGRADED_RESUME_TEXT, skills_fail=True
    )

    async with pg_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id FROM outbox WHERE aggregate_id = $1 AND event_type = "
            "'resume.parsed'",
            degraded_id,
        )
    assert rows == [], (
        "a degraded résumé must not enqueue a resume.parsed outbox row -- "
        "there is nothing for project_to_graph to ever drain"
    )


# ── the exclusion, proven all the way through a generated shortlist ────────


@pytest.mark.asyncio
async def test_degraded_resume_is_absent_from_a_generated_shortlist(
    pg_pool: asyncpg.Pool, neo4j_driver: AsyncDriver, blob_store: BlobStore
) -> None:
    """The full ADR-030 promise: a degraded résumé, parsed and drained
    through the exact same real pipeline as a good one, never appears in a
    ``generate_shortlist`` result for that job -- it was never recalled at
    stage 1 because it was never projected."""
    job_id = await _insert_job(pg_pool)
    await _seed_job_graph(neo4j_driver, job_id)

    good_id = await _seed_and_parse_via_real_path(
        pg_pool, blob_store, job_id, text=GOOD_RESUME_TEXT, skills_fail=False
    )
    degraded_id = await _seed_and_parse_via_real_path(
        pg_pool, blob_store, job_id, text=DEGRADED_RESUME_TEXT, skills_fail=True
    )

    await project_to_graph(_graph_ctx(pg_pool, neo4j_driver), batch=10)

    stubbed_evidence = EvidenceObject(
        requirements=[
            RequirementEvidence(
                requirement="Python",
                status="met",
                evidence="Built distributed systems in Python and Kubernetes.",
                evidence_chunk_ids=[],
                confidence=0.9,
            )
        ]
    )

    async with pg_pool.acquire() as conn:
        matching_ctx = MatchingContext(
            db=conn,
            neo4j=neo4j_driver,
            llm=_make_shortlist_llm(stubbed_evidence),
            embedder=_make_shortlist_embedder(),
            model_gen="test-gen",
            model_emb="test-emb",
        )
        with patch(
            "src.pipeline.matching.orchestrator.load_prompt",
            return_value=_STUBBED_PROMPT,
        ):
            result = await generate_shortlist(job_id, matching_ctx)

    ranked_ids = {e.resume_id for e in result.entries}
    assert good_id in ranked_ids, "the non-degraded résumé must rank"
    assert degraded_id not in ranked_ids, (
        "a degraded résumé must be ABSENT from the generated shortlist -- "
        "never merely ranked lower -- because it was never recalled at "
        "stage 1 (no Resume node to find)"
    )
