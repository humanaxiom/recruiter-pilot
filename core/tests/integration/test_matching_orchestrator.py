"""Integration tests -- Phase 4c's matching **orchestrator**
(``src.pipeline.matching.orchestrator``) against REAL Postgres + REAL Neo4j
(testcontainers).

``src/pipeline/matching/orchestrator.py`` does NOT exist yet -- every test
below fails at collection time with ``ModuleNotFoundError``. RED half of the
TDD cycle. Once 4c lands the module, these pin the intended, CORRECTED
behavior recorded in the 4b->4c resume-point handoff -- read against hris
``packages/pipeline/src/pipeline/matching/orchestrator.py`` but adapted to
THIS repo's real Neo4j/Postgres schema (``src/worker/tasks.py``,
``src/worker/resume_tasks.py``, ``src/worker/neo4j_bootstrap.py``,
ADR-008):

* blocker #4 -- ``_stage2_skill_rows``' Cypher must read
  ``reqSkill.canonical_key`` (the property this graph's Skill nodes actually
  carry, per ADR-008 and ``neo4j_bootstrap.py``'s ``skill_canonical_key_unique``
  constraint), NEVER ``reqSkill.canonical_name`` (a property that has never
  existed on a Skill node since Phase 3 -- see ``neo4j_bootstrap.py``'s own
  docstring on the ``skill_name_unique`` rename). A verbatim hris port reading
  ``canonical_name`` returns ``skill=None`` for every row against a real
  Neo4j -- ``test_stage2_skill_rows_reads_canonical_key_not_canonical_name``
  kills it.
* blocker #5 -- NICE_TO_HAVE skills feed stage-3's evidence prompt
  (``requirements = required_skills + nice_to_have_skills``) but the
  structured stage-2 skill sub-score is built ONLY from ``REQUIRES`` edges
  (``_stage2_skill_rows``' Cypher matches ``(j)-[:REQUIRES]->(:Skill)``
  exclusively) -- a candidate who matches every nice-to-have and zero
  required skills must score a structured skill sub-score of exactly 0.0.
  This is hris's SHIPPED behavior, pinned verbatim (not "fixed").
* blocker #7 -- ADR-007 stripped chunk TEXT from the outbox; stage-3's
  citation verification must resolve real chunk text from ``resumes.parsed``
  in Postgres. The test seeds no outbox row at all and still gets a verified
  quote.
* blocker #10 -- this repo has no synchronous reverse-match endpoint (there
  is nothing on a proxied request path to protect from LLM fan-out), so the
  worker path must call ``match_resume_to_jobs`` with
  ``settings.match_reverse_evidence_k`` (10, per the settings sibling's RED
  test) -- not orchestrator's pre-ADR-0023 synchronous-endpoint default of 0,
  which would leave every entry's evidence permanently ``None``.
* A happy-path ``generate_shortlist`` end-to-end against a small seeded
  job+candidates graph, asserting ordering + ``ScoreBreakdown`` shape.

Every LLM call is stubbed deterministically (``ctx.llm.chat_json`` ->
a canned ``EvidenceObject``) and ``load_prompt`` is patched to a fixed
``RenderedPrompt`` -- ``shortlist_evidence_v1`` has no template under
``src/prompts/templates/`` yet (Phase 4's prompt, not Phase 3's, per
``src/prompts/__init__.py``'s own docstring), and these tests target the
orchestrator's CONTRACT (chunk-text sourcing, skill-row Cypher, evidence
wiring), not prompt rendering. The embedder is a crc32-seeded deterministic
stub (same technique as ``test_graph_projection_e2e.py``'s ``_make_embedder``)
so the seniority title-cosine sub-score never depends on a live Ollama.
"""

from __future__ import annotations

import json
import random
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
    Stage1Candidate,
    Stage2Candidate,
    _stage2_per_candidate,
    _stage2_skill_rows,
    _stage3_per_candidate,
    generate_shortlist,
    load_job_view,
    match_resume_to_jobs,
)
from src.prompts import RenderedPrompt
from src.schemas.matching import (
    DEFAULT_WEIGHTS,
    EvidenceObject,
    RequirementEvidence,
    ScoreBreakdown,
)
from src.settings import get_settings
from src.worker.neo4j_bootstrap import bootstrap_neo4j_schema

_DIM = get_settings().llm_embedding_dim


def _vec(seed: int) -> list[float]:
    """Deterministic near-orthogonal 768-d vector (same technique as
    ``test_graph_projection_e2e.py``'s ``_vec`` -- see its docstring for why
    a per-seed ``random.Random`` draw, not a linear ramp, is used)."""
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


# ── Postgres seeding helpers ─────────────────────────────────────────────


async def _insert_job(
    pool: asyncpg.Pool,
    *,
    title: str = "Senior Backend Engineer",
    min_years: int | None = 3,
    required_skills: list[dict[str, Any]] | None = None,
    nice_to_have_skills: list[dict[str, Any]] | None = None,
    education_min_level: str | None = None,
    education_fields: list[str] | None = None,
) -> UUID:
    description_parsed = {
        "required_skills": required_skills if required_skills is not None else [],
        "nice_to_have_skills": (
            nice_to_have_skills if nice_to_have_skills is not None else []
        ),
        "min_years_experience": min_years,
        "education": {
            "min_level": education_min_level,
            "fields": education_fields if education_fields is not None else [],
        },
        "location": None,
        "remote_policy": None,
        "responsibilities": [],
    }
    async with pool.acquire() as conn:
        job_id: UUID = await conn.fetchval(
            "INSERT INTO jobs (title, description_raw, description_parsed, min_years) "
            "VALUES ($1, $2, $3::jsonb, $4) RETURNING id",
            title,
            "raw jd text (irrelevant to these tests)",
            json.dumps(description_parsed),
            min_years,
        )
    return job_id


async def _insert_resume(
    pool: asyncpg.Pool, job_id: UUID, *, parsed: dict[str, Any] | None = None
) -> UUID:
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
            json.dumps(parsed or {}),
        )
    return resume_id


# ── Neo4j seeding helpers ────────────────────────────────────────────────


async def _seed_job_node(
    driver: AsyncDriver, job_id: UUID, *, summary_emb: list[float]
) -> None:
    async with driver.session() as session:
        await session.run(
            "MERGE (j:Job {id: $jid}) SET j.summary_embedding = $emb",
            jid=str(job_id),
            emb=summary_emb,
        )


async def _seed_resume_node(
    driver: AsyncDriver, resume_id: UUID, job_id: UUID, *, summary_emb: list[float]
) -> None:
    async with driver.session() as session:
        await session.run(
            "MERGE (r:Resume {id: $rid}) "
            "SET r.summary_embedding = $emb, r.job_id = $jid",
            rid=str(resume_id),
            jid=str(job_id),
            emb=summary_emb,
        )


async def _seed_requires(
    driver: AsyncDriver,
    job_id: UUID,
    canonical_key: str,
    *,
    min_years: int | None = None,
    is_must_have: bool = True,
) -> None:
    async with driver.session() as session:
        await session.run(
            """
            MERGE (s:Skill {canonical_key: $key})
            WITH s
            MATCH (j:Job {id: $jid})
            MERGE (j)-[r:REQUIRES]->(s)
            SET r.min_years = $miny, r.is_must_have = $must
            """,
            key=canonical_key,
            jid=str(job_id),
            miny=min_years,
            must=is_must_have,
        )


async def _seed_nice_to_have(
    driver: AsyncDriver,
    job_id: UUID,
    canonical_key: str,
    *,
    min_years: int | None = None,
) -> None:
    async with driver.session() as session:
        await session.run(
            """
            MERGE (s:Skill {canonical_key: $key})
            WITH s
            MATCH (j:Job {id: $jid})
            MERGE (j)-[r:NICE_TO_HAVE]->(s)
            SET r.min_years = $miny
            """,
            key=canonical_key,
            jid=str(job_id),
            miny=min_years,
        )


async def _seed_has_skill(
    driver: AsyncDriver,
    resume_id: UUID,
    canonical_key: str,
    *,
    years: int | None = None,
    last_used_year: int | None = None,
) -> None:
    async with driver.session() as session:
        await session.run(
            """
            MERGE (s:Skill {canonical_key: $key})
            WITH s
            MERGE (r:Resume {id: $rid})
            MERGE (r)-[h:HAS_SKILL]->(s)
            SET h.years = $years, h.last_used_year = $luy
            """,
            key=canonical_key,
            rid=str(resume_id),
            years=years,
            luy=last_used_year,
        )


# ── LLM / embedder stubs (no live Ollama) ────────────────────────────────


def _make_embedder() -> MagicMock:
    async def _embed(texts: Any) -> list[list[float]]:
        # Deterministic per TEXT (not per call): identical strings (e.g. a
        # candidate's most-recent title matching the JD title verbatim)
        # embed to the identical vector, giving seniority's title-cosine a
        # real, reproducible signal without a live embedder.
        return [_vec(zlib.crc32(t.encode())) for t in texts]

    return MagicMock(embed=AsyncMock(side_effect=_embed))


def _make_llm(evidence: EvidenceObject | None = None) -> MagicMock:
    return MagicMock(chat_json=AsyncMock(return_value=evidence or EvidenceObject()))


def _ctx(
    conn: asyncpg.pool.PoolConnectionProxy,
    neo4j: AsyncDriver,
    *,
    llm: MagicMock | None = None,
    embedder: MagicMock | None = None,
) -> MatchingContext:
    return MatchingContext(
        db=conn,
        neo4j=neo4j,
        llm=llm or _make_llm(),
        embedder=embedder or _make_embedder(),
        model_gen="test-gen",
        model_emb="test-emb",
    )


_STUBBED_PROMPT = RenderedPrompt(version="test-stub", system="system", user="user")


# ── blocker #4: canonical_key, not canonical_name ────────────────────────


@pytest.mark.asyncio
async def test_stage2_skill_rows_reads_canonical_key_not_canonical_name(
    pg_pool: asyncpg.Pool, neo4j_driver: AsyncDriver
) -> None:
    """A verbatim hris port reads ``reqSkill.canonical_name`` -- a property
    this graph's Skill nodes never carry (ADR-008 keys every Skill node on
    ``canonical_key``; ``neo4j_bootstrap.py`` enforces uniqueness on
    ``canonical_key``, never ``canonical_name``). Against a REAL Neo4j, that
    port returns ``skill=None`` for every row. This test kills it."""
    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume(pg_pool, job_id)
    # NOTE (Phase 4c GREEN): a Neo4j Job node must exist first -- `_seed_requires`
    # does `MATCH (j:Job {id})` then `MERGE (j)-[:REQUIRES]->(s)`, so without the
    # node the MATCH yields no rows and NO edge is created (verified empirically:
    # 0 REQUIRES edges, one orphan Skill). The other REQUIRES-seeding tests
    # (`_seed_job_node` in reverse_match / generate_shortlist) already do this;
    # this one omitted it, so it asserted 1 row from an edge that never existed.
    # Adding the missing setup does not touch the behavioral assertion below
    # (skill == canonical_key, not canonical_name).
    await _seed_job_node(neo4j_driver, job_id, summary_emb=_vec(1))
    await _seed_requires(neo4j_driver, job_id, "python", min_years=3, is_must_have=True)
    async with neo4j_driver.session() as session:
        await session.run("MERGE (:Resume {id: $rid})", rid=str(resume_id))

    rows = await _stage2_skill_rows(neo4j_driver, job_id, resume_id)

    assert len(rows) == 1
    assert rows[0].skill == "python", (
        f"expected skill='python', got {rows[0].skill!r} -- the Cypher "
        "likely reads reqSkill.canonical_name (a property this graph never "
        "sets) instead of reqSkill.canonical_key"
    )
    assert rows[0].req_years == 3
    assert rows[0].is_must_have is True


# ── blocker #5: NICE_TO_HAVE feeds evidence text, not the structured score ──


@pytest.mark.asyncio
async def test_nice_to_have_skills_feed_evidence_not_stage2_skill_score(
    pg_pool: asyncpg.Pool, neo4j_driver: AsyncDriver
) -> None:
    """A candidate matching every nice-to-have and zero required skills must
    score structured skill=0.0 -- ``_stage2_skill_rows``' Cypher matches only
    ``(j)-[:REQUIRES]->(:Skill)``, never ``NICE_TO_HAVE``. The evidence
    prompt's requirements list (``required + nice_to_have``) still surfaces
    the nice-to-have, which is why ``JobView.nice_to_have_skills`` must stay
    populated even though it never feeds the structured sub-score."""
    job_id = await _insert_job(
        pg_pool,
        required_skills=[],
        nice_to_have_skills=[{"name": "Docker", "min_years": None}],
    )
    resume_id = await _insert_resume(
        pg_pool,
        job_id,
        parsed={"total_years_experience": 5, "education": [], "experience": []},
    )
    await _seed_nice_to_have(neo4j_driver, job_id, "docker")
    await _seed_has_skill(
        neo4j_driver, resume_id, "docker", years=5, last_used_year=2026
    )

    async with pg_pool.acquire() as conn:
        job = await load_job_view(conn, job_id)
    assert job is not None
    assert job.required_skills == ()
    assert job.nice_to_have_skills == ("Docker",), (
        "nice-to-have skills must still be visible on JobView -- stage-3's "
        "evidence prompt builds `requirements` from required + nice-to-have"
    )

    async with pg_pool.acquire() as conn:
        ctx = _ctx(conn, neo4j_driver)
        candidate = Stage1Candidate(resume_id=resume_id, vec_score=1.0)
        result = await _stage2_per_candidate(ctx, job, candidate, 1.0, DEFAULT_WEIGHTS)

    assert result.breakdown.skill == 0.0, (
        "matching every nice-to-have and zero required skills must not "
        "produce a nonzero structured skill sub-score -- nice-to-haves must "
        "never feed _stage2_skill_rows"
    )


# ── blocker #7: stage-3 chunk text sources from Postgres, not the outbox ──


@pytest.mark.asyncio
async def test_stage3_evidence_sources_chunk_text_from_resumes_parsed_not_outbox(
    pg_pool: asyncpg.Pool, neo4j_driver: AsyncDriver
) -> None:
    """ADR-007 stripped chunk TEXT from the outbox. Stage-3's citation
    verification must resolve real chunk text from ``resumes.parsed`` in
    Postgres -- no outbox row is enqueued anywhere in this test, and the
    assertion at the end proves none was needed."""
    job_id = await _insert_job(
        pg_pool, required_skills=[{"name": "Python", "min_years": 2}]
    )
    chunk_text = "Shipped production Python services handling 10k requests/sec."
    resume_id = await _insert_resume(
        pg_pool,
        job_id,
        parsed={
            "total_years_experience": 5,
            "education": [],
            "experience": [],
            "chunks": [
                {"id": "c_001", "section": "experience", "page": 1, "text": chunk_text}
            ],
            "cover_letter_chunks": [],
        },
    )

    async with pg_pool.acquire() as conn:
        job = await load_job_view(conn, job_id)
        assert job is not None
        parsed_row = await conn.fetchrow(
            "SELECT parsed FROM resumes WHERE id = $1", resume_id
        )
    assert parsed_row is not None
    parsed = parsed_row["parsed"]
    if isinstance(parsed, str):
        parsed = json.loads(parsed)

    stubbed_evidence = EvidenceObject(
        requirements=[
            RequirementEvidence(
                requirement="Python",
                status="met",
                evidence=chunk_text,
                evidence_chunk_ids=["c_001"],
                confidence=0.9,
            )
        ]
    )
    candidate = Stage2Candidate(
        resume_id=resume_id,
        vec_score=1.0,
        structured=1.0,
        breakdown=ScoreBreakdown(
            skill=1.0,
            experience=1.0,
            education=1.0,
            seniority=1.0,
            vector=1.0,
            structured=1.0,
        ),
    )

    async with pg_pool.acquire() as conn:
        ctx = _ctx(conn, neo4j_driver, llm=_make_llm(stubbed_evidence))
        with patch(
            "src.pipeline.matching.orchestrator.load_prompt",
            return_value=_STUBBED_PROMPT,
        ):
            evidence = await _stage3_per_candidate(ctx, job, candidate, parsed)

    assert evidence is not None
    result = evidence.requirements[0]
    assert result.evidence == chunk_text, (
        "verified evidence text did not match resumes.parsed's own chunk "
        "text -- stage-3 must resolve chunk text from Postgres, not an "
        "outbox-shaped payload"
    )
    assert result.status == "met"

    async with pg_pool.acquire() as conn:
        outbox_count = await conn.fetchval("SELECT count(*) FROM outbox")
    assert outbox_count == 0, (
        "this test seeded no outbox row at all -- chunk-text resolution "
        "must not depend on one"
    )


# ── blocker #10: reverse match runs evidence at the worker-path default ───


@pytest.mark.asyncio
async def test_reverse_match_runs_stage3_evidence_at_the_worker_path_default(
    pg_pool: asyncpg.Pool, neo4j_driver: AsyncDriver
) -> None:
    """recruiter-assistant has no synchronous reverse-match endpoint, so the
    worker path must call ``match_resume_to_jobs`` with
    ``settings.match_reverse_evidence_k`` (10, per the settings sibling's RED
    test) -- not orchestrator's pre-ADR-0023 synchronous-endpoint default of
    0, which would leave every entry's evidence permanently ``None``."""
    reverse_evidence_k = get_settings().match_reverse_evidence_k
    assert reverse_evidence_k > 0, (
        "sanity: the settings-sourced worker-path default must be > 0 "
        "(blocker #10) -- got a synchronous-endpoint-style 0"
    )

    chunk_text = "Delivered Python microservices in production for four years."
    job_id = await _insert_job(
        pg_pool,
        title="Backend Engineer",
        required_skills=[{"name": "Python", "min_years": 2}],
    )
    resume_id = await _insert_resume(
        pg_pool,
        job_id,
        parsed={
            "total_years_experience": 5,
            "education": [],
            "experience": [{"title": "Backend Engineer", "is_current": True}],
            "chunks": [
                {"id": "c_001", "section": "experience", "page": 1, "text": chunk_text}
            ],
            "cover_letter_chunks": [],
        },
    )

    shared_emb = _vec(1)
    await _seed_job_node(neo4j_driver, job_id, summary_emb=shared_emb)
    await _seed_resume_node(neo4j_driver, resume_id, job_id, summary_emb=shared_emb)
    await _seed_requires(neo4j_driver, job_id, "python", min_years=2, is_must_have=True)
    await _seed_has_skill(
        neo4j_driver, resume_id, "python", years=5, last_used_year=2026
    )

    stubbed_evidence = EvidenceObject(
        requirements=[
            RequirementEvidence(
                requirement="Python",
                status="met",
                evidence=chunk_text,
                evidence_chunk_ids=["c_001"],
                confidence=0.9,
            )
        ]
    )

    async with pg_pool.acquire() as conn:
        ctx = _ctx(conn, neo4j_driver, llm=_make_llm(stubbed_evidence))
        with patch(
            "src.pipeline.matching.orchestrator.load_prompt",
            return_value=_STUBBED_PROMPT,
        ):
            result = await match_resume_to_jobs(
                resume_id, ctx, evidence_k=reverse_evidence_k
            )

    assert result.entries, "expected at least one ranked job"
    top = result.entries[0]
    assert top.evidence is not None, (
        "stage-3 evidence must run at the worker-path default "
        f"(match_reverse_evidence_k={reverse_evidence_k}) -- got "
        "evidence=None, the k=0 (synchronous-endpoint) behavior"
    )
    assert top.score_evidence > 0.0


# ── happy path: generate_shortlist end to end ────────────────────────────


@pytest.mark.asyncio
async def test_generate_shortlist_end_to_end_orders_candidates_and_shapes_breakdown(
    pg_pool: asyncpg.Pool, neo4j_driver: AsyncDriver
) -> None:
    job_id = await _insert_job(
        pg_pool,
        title="Senior Backend Engineer",
        min_years=4,
        required_skills=[
            {"name": "Python", "min_years": 3},
            {"name": "PostgreSQL", "min_years": 2},
        ],
    )
    await _seed_job_node(neo4j_driver, job_id, summary_emb=_vec(100))
    await _seed_requires(neo4j_driver, job_id, "python", min_years=3, is_must_have=True)
    await _seed_requires(
        neo4j_driver, job_id, "postgresql", min_years=2, is_must_have=True
    )

    strong_chunk = (
        "Built and scaled Python + PostgreSQL services for millions of users."
    )
    strong_id = await _insert_resume(
        pg_pool,
        job_id,
        parsed={
            "total_years_experience": 6,
            "education": [{"degree": "BSc Computer Science"}],
            "experience": [{"title": "Senior Backend Engineer", "is_current": True}],
            "chunks": [
                {
                    "id": "c_001",
                    "section": "experience",
                    "page": 1,
                    "text": strong_chunk,
                }
            ],
            "cover_letter_chunks": [],
        },
    )
    await _seed_resume_node(neo4j_driver, strong_id, job_id, summary_emb=_vec(101))
    await _seed_has_skill(
        neo4j_driver, strong_id, "python", years=6, last_used_year=2026
    )
    await _seed_has_skill(
        neo4j_driver, strong_id, "postgresql", years=5, last_used_year=2026
    )

    weak_id = await _insert_resume(
        pg_pool,
        job_id,
        parsed={
            "total_years_experience": 0,
            "education": [],
            "experience": [],
            "chunks": [],
            "cover_letter_chunks": [],
        },
    )
    await _seed_resume_node(neo4j_driver, weak_id, job_id, summary_emb=_vec(202))

    stubbed_evidence = EvidenceObject(
        requirements=[
            RequirementEvidence(
                requirement="Python",
                status="met",
                evidence=strong_chunk,
                evidence_chunk_ids=["c_001"],
                confidence=0.9,
            ),
            RequirementEvidence(
                requirement="PostgreSQL",
                status="met",
                evidence=strong_chunk,
                evidence_chunk_ids=["c_001"],
                confidence=0.9,
            ),
        ]
    )

    async with pg_pool.acquire() as conn:
        ctx = _ctx(conn, neo4j_driver, llm=_make_llm(stubbed_evidence))
        with patch(
            "src.pipeline.matching.orchestrator.load_prompt",
            return_value=_STUBBED_PROMPT,
        ):
            result = await generate_shortlist(job_id, ctx)

    assert [e.resume_id for e in result.entries] == [strong_id, weak_id], (
        "the strong candidate (both required skills, sufficient recent "
        "years, matching seniority) must rank above the weak candidate "
        "(zero skills, zero experience)"
    )
    assert result.entries[0].rank == 1
    assert result.entries[1].rank == 2

    for entry in result.entries:
        assert isinstance(entry.breakdown, ScoreBreakdown)
        assert 0.0 <= entry.score_final <= 1.0
        assert 0.0 <= entry.score_structured <= 1.0
        assert 0.0 <= entry.score_evidence <= 1.0

    assert result.entries[0].score_final > result.entries[1].score_final

    assert result.pipeline_meta is not None
    assert result.pipeline_meta.model_gen == "test-gen"
    assert result.pipeline_meta.model_emb == "test-emb"


# ── ROADMAP A6: fabricated sub-scores are marked, not silently rendered ────
#
# D1/D2, REVERSE direction. Reverse match calls the identical
# `_stage2_per_candidate` (`orchestrator.py:964-966`) and its own
# `normalise_vector_scores` (`orchestrator.py:956`), so both defects are
# present here too — this pins the CALL SITE wiring (that
# `match_resume_to_jobs` actually threads its own computed
# `vec_discriminating` through), against a REAL Neo4j stage-1 query rather
# than a mock: reverse match's `stage1_coarse_jobs` uses
# `db.index.vector.queryNodes` on `job_summary_idx`, a code path this repo
# has no other real-driver coverage for (docs/ROADMAP.md's own open
# residual). The forward direction's unit-level equivalent lives in
# `tests/unit/test_a6_sub_score_markers_write_path.py`.


@pytest.mark.asyncio
async def test_reverse_match_marks_both_false_for_a_single_job_pool_no_title(
    pg_pool: asyncpg.Pool, neo4j_driver: AsyncDriver
) -> None:
    """A résumé with exactly one candidate job is a single-element stage-1
    pool — degenerate by definition, regardless of the real similarity value
    — and this résumé also carries no experience at all, so no title
    comparison is possible either. Both must be marked unmeasured."""
    job_id = await _insert_job(pg_pool, title="Backend Engineer")
    resume_id = await _insert_resume(
        pg_pool,
        job_id,
        parsed={"total_years_experience": 3, "education": [], "experience": []},
    )
    shared_emb = _vec(1)
    await _seed_job_node(neo4j_driver, job_id, summary_emb=shared_emb)
    await _seed_resume_node(neo4j_driver, resume_id, job_id, summary_emb=shared_emb)

    async with pg_pool.acquire() as conn:
        ctx = _ctx(conn, neo4j_driver)
        with patch(
            "src.pipeline.matching.orchestrator.load_prompt",
            return_value=_STUBBED_PROMPT,
        ):
            result = await match_resume_to_jobs(resume_id, ctx)

    assert result.entries, "expected exactly one ranked job"
    breakdown = result.entries[0].breakdown
    assert breakdown.vector_discriminating is False, (
        "a single-candidate-job pool is degenerate by definition — "
        "normalise_vector_scores returns 1.0 for it with no comparison "
        "possible, and the write path must mark it, not just compute it"
    )
    assert breakdown.seniority_measured is False, (
        "this résumé carries no experience at all, so no title comparison "
        "was possible — the fallback 0.0 must be marked, not measured"
    )


@pytest.mark.asyncio
async def test_reverse_match_marks_both_true_for_a_pool_with_spread_and_title(
    pg_pool: asyncpg.Pool, neo4j_driver: AsyncDriver
) -> None:
    job1 = await _insert_job(pg_pool, title="Backend Engineer")
    job2 = await _insert_job(pg_pool, title="Data Engineer")
    resume_id = await _insert_resume(
        pg_pool,
        job1,
        parsed={
            "total_years_experience": 5,
            "education": [],
            "experience": [{"title": "Backend Engineer", "is_current": True}],
        },
    )
    await _seed_job_node(neo4j_driver, job1, summary_emb=_vec(11))
    await _seed_job_node(neo4j_driver, job2, summary_emb=_vec(22))
    await _seed_resume_node(neo4j_driver, resume_id, job1, summary_emb=_vec(33))

    async with pg_pool.acquire() as conn:
        ctx = _ctx(conn, neo4j_driver)
        with patch(
            "src.pipeline.matching.orchestrator.load_prompt",
            return_value=_STUBBED_PROMPT,
        ):
            result = await match_resume_to_jobs(resume_id, ctx)

    assert len(result.entries) == 2, "expected both candidate jobs to be scored"
    for entry in result.entries:
        assert entry.breakdown.vector_discriminating is True, (
            "a two-job pool with distinct embeddings has real spread — it "
            "must not be marked degenerate"
        )
    matched = [e for e in result.entries if e.job_id == job1]
    assert matched, "expected the résumé's own job to be among the ranked jobs"
    assert matched[0].breakdown.seniority_measured is True, (
        "this résumé's current role has a readable title — the comparison "
        "genuinely ran and must be marked measured"
    )


# ── ROADMAP A6 remediation F1: the FORWARD marker wiring is unenforced ─────
#
# The reverse-direction pair immediately above pins `match_resume_to_jobs`'s
# own computed `vec_discriminating`, threaded through `_stage2_per_candidate`.
# The FORWARD call site (`generate_shortlist`, `orchestrator.py:816`) has the
# IDENTICAL shape --
#
#     vec_discriminating = not vector_pool_is_degenerate(raw_vec_scores)
#
# -- but nothing drives it end to end: per the reviewer's own count, dropping
# the `not` there survives all 4459 unit tests AND all 56 integration tests
# that existed before this pin. A one-résumé job -- the ROUTINE case ADR-041
# names, not an edge case -- is a single-candidate stage-1 pool by
# definition, and this is the direction with the real rendering surface (the
# entry-detail panel via `generate_shortlist` -> `shortlist_entries`),
# unlike the reverse direction the two tests above cover. Driven against
# REAL Postgres + REAL Neo4j, exactly like the reverse pair, because
# `stage1_coarse`'s job-scoped Cypher (`orchestrator.py:355-372`) is itself
# untested by a mock.


@pytest.mark.asyncio
async def test_generate_shortlist_marks_vector_discriminating_false_single_pool(
    pg_pool: asyncpg.Pool, neo4j_driver: AsyncDriver
) -> None:
    """One résumé applied to this job -- stage 1's pool has exactly one
    element, degenerate by definition regardless of the real similarity
    value. `generate_shortlist` must persist `vector_discriminating=False`
    for it, not the affirmative default a dropped `not` at the forward call
    site would silently produce."""
    job_id = await _insert_job(pg_pool, title="Backend Engineer")
    resume_id = await _insert_resume(
        pg_pool,
        job_id,
        parsed={
            "total_years_experience": 3,
            "education": [],
            "experience": [],
            "chunks": [],
            "cover_letter_chunks": [],
        },
    )
    await _seed_job_node(neo4j_driver, job_id, summary_emb=_vec(500))
    await _seed_resume_node(neo4j_driver, resume_id, job_id, summary_emb=_vec(501))

    async with pg_pool.acquire() as conn:
        ctx = _ctx(conn, neo4j_driver)
        with patch(
            "src.pipeline.matching.orchestrator.load_prompt",
            return_value=_STUBBED_PROMPT,
        ):
            result = await generate_shortlist(job_id, ctx)

    assert len(result.entries) == 1, "expected exactly one ranked candidate"
    assert result.entries[0].breakdown.vector_discriminating is False, (
        "a single-candidate stage-1 pool is degenerate by definition -- the "
        "FORWARD call site (generate_shortlist) must mark it, not persist "
        "the affirmative default a dropped `not` would produce. This is the "
        "gap the reverse-direction tests above cannot see."
    )


@pytest.mark.asyncio
async def test_generate_shortlist_marks_vector_discriminating_true_pool_spread(
    pg_pool: asyncpg.Pool, neo4j_driver: AsyncDriver
) -> None:
    """Two résumés with genuinely distinct summary embeddings -- real spread
    in the stage-1 `vec_score` pool -- must persist
    `vector_discriminating=True` for BOTH candidates via the forward call
    site."""
    job_id = await _insert_job(pg_pool, title="Backend Engineer")
    job_emb = _vec(600)
    resume_kwargs = {
        "total_years_experience": 3,
        "education": [],
        "experience": [],
        "chunks": [],
        "cover_letter_chunks": [],
    }
    close_id = await _insert_resume(pg_pool, job_id, parsed=dict(resume_kwargs))
    far_id = await _insert_resume(pg_pool, job_id, parsed=dict(resume_kwargs))
    await _seed_job_node(neo4j_driver, job_id, summary_emb=job_emb)
    await _seed_resume_node(neo4j_driver, close_id, job_id, summary_emb=job_emb)
    await _seed_resume_node(neo4j_driver, far_id, job_id, summary_emb=_vec(601))

    async with pg_pool.acquire() as conn:
        ctx = _ctx(conn, neo4j_driver)
        with patch(
            "src.pipeline.matching.orchestrator.load_prompt",
            return_value=_STUBBED_PROMPT,
        ):
            result = await generate_shortlist(job_id, ctx)

    assert len(result.entries) == 2, "expected both candidates to be scored"
    for entry in result.entries:
        assert entry.breakdown.vector_discriminating is True, (
            "a two-candidate pool with genuinely distinct embeddings has "
            "real spread -- the forward call site must not mark it "
            "degenerate"
        )


# ── ADR-028: load_job_view populates JobView.education_fields ───────────────
#
# DB-bound (``JobView`` is only ever built by ``load_job_view`` reading a real
# ``jobs.description_parsed`` jsonb column), so these live here rather than in
# the pure-function unit suite.


@pytest.mark.asyncio
async def test_load_job_view_populates_education_fields_from_description_parsed(
    pg_pool: asyncpg.Pool,
) -> None:
    job_id = await _insert_job(
        pg_pool,
        education_min_level="bachelors",
        education_fields=["Computer Science", "Software Engineering"],
    )

    async with pg_pool.acquire() as conn:
        job = await load_job_view(conn, job_id)

    assert job is not None
    assert job.education_fields == ("Computer Science", "Software Engineering")


@pytest.mark.asyncio
async def test_load_job_view_empty_fields_list_yields_empty_tuple(
    pg_pool: asyncpg.Pool,
) -> None:
    job_id = await _insert_job(
        pg_pool, education_min_level="bachelors", education_fields=[]
    )

    async with pg_pool.acquire() as conn:
        job = await load_job_view(conn, job_id)

    assert job is not None
    assert job.education_fields == ()


@pytest.mark.asyncio
async def test_load_job_view_missing_fields_key_defaults_to_empty_tuple(
    pg_pool: asyncpg.Pool,
) -> None:
    """The JD's ``education`` object may lack a ``fields`` key entirely (older
    parses, or a JD with no education requirement at all) — ``load_job_view``
    must not raise and must default to the empty tuple, not ``None`` or a
    ``KeyError``."""
    description_parsed = {
        "required_skills": [],
        "nice_to_have_skills": [],
        "min_years_experience": None,
        "education": {"min_level": None},  # no "fields" key at all
        "location": None,
        "remote_policy": None,
        "responsibilities": [],
    }
    async with pg_pool.acquire() as conn:
        job_id: UUID = await conn.fetchval(
            "INSERT INTO jobs (title, description_raw, description_parsed, min_years) "
            "VALUES ($1, $2, $3::jsonb, $4) RETURNING id",
            "Backend Engineer",
            "raw jd text (irrelevant to this test)",
            json.dumps(description_parsed),
            None,
        )
        job = await load_job_view(conn, job_id)

    assert job is not None
    assert job.education_fields == ()


@pytest.mark.asyncio
async def test_load_job_view_drops_non_string_and_blank_field_entries(
    pg_pool: asyncpg.Pool,
) -> None:
    """Mirrors ``required_skills``/``nice_to_have_skills``' own tolerance for a
    malformed LLM parse: a non-string or blank entry in ``education.fields``
    must be dropped, not raise or surface as a blank/garbage requirement."""
    description_parsed = {
        "required_skills": [],
        "nice_to_have_skills": [],
        "min_years_experience": None,
        "education": {
            "min_level": "bachelors",
            "fields": ["Computer Science", "", None, 42, "  "],
        },
        "location": None,
        "remote_policy": None,
        "responsibilities": [],
    }
    async with pg_pool.acquire() as conn:
        job_id: UUID = await conn.fetchval(
            "INSERT INTO jobs (title, description_raw, description_parsed, min_years) "
            "VALUES ($1, $2, $3::jsonb, $4) RETURNING id",
            "Backend Engineer",
            "raw jd text (irrelevant to this test)",
            json.dumps(description_parsed),
            None,
        )
        job = await load_job_view(conn, job_id)

    assert job is not None
    assert job.education_fields == ("Computer Science",)
