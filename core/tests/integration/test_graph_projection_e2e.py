"""Integration tests — ``src.worker.graph_tasks.project_to_graph`` against
REAL Postgres + REAL Neo4j (testcontainers).

Proves for real what the unit tests (``test_worker_project_to_graph.py`` /
``test_worker_project_resume.py`` / ``test_worker_project_job.py`` /
``test_pipeline_skills_graph.py``) prove by mocked-call capture:

* ``job.parsed`` -> ``Job`` node + ``REQUIRES``(``is_must_have``,
  ``min_years``) + ``NICE_TO_HAVE`` edges.
* ``resume.parsed`` -> ``Resume`` / ``HAS_CHUNK`` / ``ResumeChunk`` /
  ``HAS_SKILL`` / ``Skill.categories`` non-empty.
* **R8 — the pinned label set.** The whole database, after both projections,
  carries only ``{Resume, ResumeChunk, Skill, Job}`` — never ``Company``/
  ``Institution``, even though the bootstrap already declares constraints for
  those labels (an inviting trap for a verbatim hris port).
* **Idempotency** — draining the SAME event twice (a re-parse) produces
  identical node/edge counts and no duplicate skill aliases (R6).
* **R7 — concurrency.** Two drainers racing against the same outbox rows
  each deliver a disjoint set exactly once; requires ``FOR UPDATE SKIP
  LOCKED``.
* **Decision 2 — poison row.** A row whose projection always raises
  increments ``delivery_attempts`` on every drain, never gets
  ``delivered_at``, does NOT block its batch-mates, and stops being selected
  once it hits ``settings.outbox_max_delivery_attempts`` (dead-lettered).

None of ``src.worker.graph_tasks`` / ``src.worker.resume_tasks.project_resume``
/ ``src.worker.tasks._project_job`` / ``src.pipeline.skills_graph`` exist yet
— this file fails at collection (``ImportError``). RED half of the TDD cycle.
"""

from __future__ import annotations

import asyncio
import random
import re
import uuid
import zlib
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest
from neo4j import AsyncDriver, AsyncGraphDatabase
from testcontainers.neo4j import Neo4jContainer
from testcontainers.postgres import PostgresContainer

from src.models.ddl import init_schema
from src.pipeline import skills_graph
from src.services import outbox_service
from src.settings import Settings, get_settings
from src.worker.graph_tasks import project_to_graph
from src.worker.neo4j_bootstrap import bootstrap_neo4j_schema

_DIM = get_settings().llm_embedding_dim

PINNED_LABEL_SET = frozenset({"Resume", "ResumeChunk", "Skill", "Job"})


def _vec(seed: int) -> list[float]:
    # DELIBERATE FIX (data-pipeline coder, Phase 4b, not part of the original
    # RED commit): the original formula (``((i*13+seed*7) % 971) / 990 +
    # 0.01``) is a phase-shifted linear ramp — every seed produces a vector
    # that is highly cosine-correlated with every OTHER seed's vector
    # (measured 0.65-0.98 between distinct skill names), because the "signal"
    # is just a constant offset riding the same underlying ramp. That is
    # fine for the vast majority of this file's uses (determinism + a valid
    # 768-d float vector is all a Resume/Job/Chunk embedding needs here), but
    # it makes ``resolve_canonical_names``'s real cosine-similarity skill
    # matching FLAKY: distinct skills ("python" vs "postgresql") could land
    # above ``skill_auto_merge_threshold``/``skill_tiebreaker_threshold``
    # purely by chance of which seeds this run happened to hash to, silently
    # merging two different skills into one Skill node and failing
    # ``test_resume_parsed_projects_resume_chunks_and_skills`` /
    # ``test_job_parsed_projects_job_node_and_requires_edges`` nondeterministically.
    # A per-seed ``random.Random`` draw gives the SAME determinism (same seed
    # -> byte-identical vector, needed for the idempotency test) but distinct
    # seeds land NEAR-ORTHOGONAL in 768-d (measured -0.08..0.06 across every
    # skill-name pair actually used in this file) — the property the vector
    # index's similarity search actually needs to behave correctly.
    rng = random.Random(seed)
    return [rng.uniform(-1.0, 1.0) for _ in range(_DIM)]


# ── fixtures ───────────────────────────────────────────────────────────────


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


def _make_llm(match: str | None = None) -> MagicMock:
    out = MagicMock(match=match)
    return MagicMock(chat_json=AsyncMock(return_value=out))


def _make_embedder() -> MagicMock:
    async def _embed(texts: Any) -> list[list[float]]:
        # DELIBERATE FIX (data-pipeline coder, Phase 4b): Python's builtin
        # ``hash()`` on ``str`` is PER-PROCESS RANDOMISED (PEP 456) unless
        # ``PYTHONHASHSEED`` is pinned — this call's seed, and therefore
        # whether two skill names' vectors land inside the same-skill
        # auto-merge/tiebreaker band, changed from one test run (one process)
        # to the next. ``zlib.crc32`` is a real deterministic hash: the same
        # text always seeds the same vector, in this run and every other one.
        return [_vec(zlib.crc32(t.encode())) for t in texts]

    return MagicMock(embed=AsyncMock(side_effect=_embed))


def _ctx(pg_pool: asyncpg.Pool, neo4j_driver: AsyncDriver) -> dict[str, Any]:
    return {
        "pg_pool": pg_pool,
        "neo4j": neo4j_driver,
        "llm": _make_llm(),
        "embedder": _make_embedder(),
    }


def _make_embedder_forcing_near_duplicate(text_a: str, text_b: str) -> MagicMock:
    """F1 (security re-audit round 2): every OTHER fixture in this file uses
    ``_make_embedder`` above, whose ``_vec``-per-crc32-seed vectors are
    DELIBERATELY near-orthogonal across distinct skill names (see ``_vec``'s
    own docstring) — which means the real Neo4j vector-search auto-merge
    branch of ``skills_graph._resolve_one`` never actually fires in ANY
    other test in this file (security's report: "the e2e `_vec` helper was
    deliberately made near-orthogonal so the vector branch never fires").

    This helper is used ONLY by the vector-auto-merge test below: it forces
    ``text_a`` and ``text_b`` — and ONLY those two exact strings — to embed
    to the IDENTICAL vector, so a real ``score >= skill_auto_merge_threshold``
    hit happens against the REAL Neo4j vector index. Every other text still
    goes through the normal crc32-seeded, near-orthogonal path, so this
    doesn't change determinism/collision behaviour for anything else.
    """
    shared_seed = zlib.crc32(text_a.encode())

    async def _embed(texts: Any) -> list[list[float]]:
        out = []
        for t in texts:
            seed = shared_seed if t in (text_a, text_b) else zlib.crc32(t.encode())
            out.append(_vec(seed))
        return out

    return MagicMock(embed=AsyncMock(side_effect=_embed))


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
            "required_skills": [
                {"name": "python", "min_years": 4},
                {"name": "postgresql", "min_years": 3},
            ],
            "nice_to_have_skills": [{"name": "kubernetes", "min_years": None}],
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
    pool: asyncpg.Pool, *, resume_id: uuid.UUID, job_id: uuid.UUID
) -> None:
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
                {
                    "name": "postgresql",
                    "years": 5,
                    "last_used_year": 2026,
                    "evidence_chunk_ids": ["c_002"],
                },
            ],
            # F2 (security re-audit): non-empty on purpose. R8's whole claim
            # is "this module never writes Company/Institution" — an EMPTY
            # experience/education list makes that assertion vacuously true
            # (there is nothing TO write either way). Real data here means
            # `test_pinned_label_set_after_both_projections` actually
            # exercises the guard: a verbatim-hris-style regression that
            # started writing a Company/Institution node from this data
            # would now fail it.
            "experience": [
                {
                    "company": "Nimbus Analytics Inc",
                    "title": "Senior Backend Engineer",
                    "start": "2022-01",
                    "is_current": True,
                    "bullets": [{"text": "Shipped REST APIs.", "chunk_id": "c_001"}],
                }
            ],
            "education": [
                {
                    "degree": "BSc Computer Science",
                    "institution": "State University",
                    "year": 2016,
                }
            ],
            "chunks": [
                {"id": "c_001", "section": "experience", "page": 1},
                {"id": "c_002", "section": "experience", "page": 1},
            ],
            "cover_letter_chunks": [],
        },
        "summary_emb": _vec(3),
        "chunk_embs": {"c_001": _vec(4), "c_002": _vec(5)},
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


async def _all_labels(driver: AsyncDriver) -> set[str]:
    # DELIBERATE FIX (data-pipeline coder, Phase 4b, not part of the original
    # RED commit): ``CALL db.labels()`` lists every label TOKEN Neo4j has ever
    # registered — including one created by a bare ``CREATE CONSTRAINT ... FOR
    # (c:Company) ...`` with ZERO ``Company`` nodes ever written. Verified
    # empirically against a real neo4j:5-community container: an empty
    # database that only runs the (out-of-scope, "NO CHANGE") bootstrap's
    # Company/Institution uniqueness constraints already reports
    # ``db.labels() == {"Company", "Institution"}`` before this module writes
    # a single node. That makes the ORIGINAL query here unsatisfiable by ANY
    # correct implementation once the bootstrap (which 4b must not touch —
    # see the target file map) declares those constraints, and it is not what
    # this test's own docstring/assertion message describe wanting to check
    # ("the whole database ... carries only {...}" — i.e. labels actually
    # present on NODES, not labels merely declared in the schema). Rewritten
    # to enumerate labels actually carried by nodes; the assertion, the
    # pinned label set, and the failure message are all unchanged.
    async with driver.session() as session:
        result = await session.run(
            "MATCH (n) UNWIND labels(n) AS label RETURN DISTINCT label"
        )
        return {record["label"] async for record in result}


async def _count(driver: AsyncDriver, cypher: str) -> int:
    async with driver.session() as session:
        result = await session.run(cypher)
        record = await result.single()
        assert record is not None
        return int(record["n"])


async def _all_aliases(driver: AsyncDriver) -> list[str]:
    async with driver.session() as session:
        result = await session.run(
            "MATCH (s:Skill) UNWIND coalesce(s.aliases, []) AS a RETURN a"
        )
        return [record["a"] async for record in result]


# ── job.parsed projection ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_job_parsed_projects_job_node_and_requires_edges(
    pg_pool: asyncpg.Pool, neo4j_driver: AsyncDriver
) -> None:
    job_id = await _insert_job(pg_pool)
    await _enqueue_job_parsed(pg_pool, job_id)

    delivered = await project_to_graph(_ctx(pg_pool, neo4j_driver), batch=10)
    assert delivered == 1

    assert await _count(neo4j_driver, "MATCH (j:Job) RETURN count(j) AS n") == 1
    assert (
        await _count(
            neo4j_driver,
            "MATCH (:Job)-[r:REQUIRES]->(:Skill) "
            "WHERE r.is_must_have = true RETURN count(r) AS n",
        )
        == 2
    )
    assert (
        await _count(
            neo4j_driver, "MATCH (:Job)-[r:NICE_TO_HAVE]->(:Skill) RETURN count(r) AS n"
        )
        == 1
    )


@pytest.mark.asyncio
async def test_job_reprojection_typed_delete_does_not_touch_a_foreign_job_skill_edge(
    pg_pool: asyncpg.Pool, neo4j_driver: AsyncDriver
) -> None:
    """F4 (security re-audit, MEDIUM), behavioural proof. A future phase
    (4c/4d) may introduce another Job->Skill edge type; the old-edge cleanup
    in ``_job_projection_tx`` must never delete it on a re-parse. The
    pre-fix wildcard ``-[r]->(:Skill) DELETE r`` destroys this edge on the
    very next ``job.parsed`` projection for the SAME job — this test would
    have gone RED against that code."""
    job_id = await _insert_job(pg_pool)
    await _enqueue_job_parsed(pg_pool, job_id)
    await project_to_graph(_ctx(pg_pool, neo4j_driver), batch=10)

    # Seed a FOREIGN Job->Skill edge type this module never creates itself.
    async with neo4j_driver.session() as session:
        await session.run(
            "MATCH (j:Job {id: $jid}) "
            "MERGE (s:Skill {canonical_key: 'rust'}) "
            "MERGE (j)-[:MENTIONS]->(s)",
            jid=str(job_id),
        )

    # Re-project (simulates a JD re-parse) — must not touch the foreign edge.
    await _enqueue_job_parsed(pg_pool, job_id)
    delivered = await project_to_graph(_ctx(pg_pool, neo4j_driver), batch=10)
    assert delivered == 1

    assert (
        await _count(
            neo4j_driver,
            f"MATCH (:Job {{id: '{job_id}'}})-[:MENTIONS]->"
            "(:Skill {canonical_key: 'rust'}) RETURN count(*) AS n",
        )
        == 1
    ), "typed DELETE removed a foreign Job->Skill edge type"


# ── resume.parsed projection ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resume_parsed_projects_resume_chunks_and_skills(
    pg_pool: asyncpg.Pool, neo4j_driver: AsyncDriver
) -> None:
    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume(pg_pool, job_id)
    await _enqueue_resume_parsed(pg_pool, resume_id=resume_id, job_id=job_id)

    delivered = await project_to_graph(_ctx(pg_pool, neo4j_driver), batch=10)
    assert delivered == 1

    assert await _count(neo4j_driver, "MATCH (r:Resume) RETURN count(r) AS n") == 1
    assert (
        await _count(
            neo4j_driver,
            "MATCH (:Resume)-[:HAS_CHUNK]->(:ResumeChunk) RETURN count(*) AS n",
        )
        == 2
    )
    assert (
        await _count(
            neo4j_driver, "MATCH (:Resume)-[:HAS_SKILL]->(:Skill) RETURN count(*) AS n"
        )
        == 2
    )


@pytest.mark.asyncio
async def test_python_skill_node_has_non_empty_categories(
    pg_pool: asyncpg.Pool, neo4j_driver: AsyncDriver
) -> None:
    """R4, end to end: if ``categories.yaml`` fails to resolve, this is the
    test that catches it (a projected Skill node's ``.categories`` stays
    empty)."""
    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume(pg_pool, job_id)
    await _enqueue_resume_parsed(pg_pool, resume_id=resume_id, job_id=job_id)
    await project_to_graph(_ctx(pg_pool, neo4j_driver), batch=10)

    async with neo4j_driver.session() as session:
        result = await session.run(
            "MATCH (s:Skill {canonical_key: 'python'}) RETURN s.categories AS cats"
        )
        record = await result.single()
    assert record is not None
    assert record["cats"], "python Skill node has empty .categories (R4)"


# ── ADR-008: canonical-key hashing, display_name, and the recall round-trip


@pytest.mark.asyncio
async def test_job_side_skill_node_gets_a_cleartext_display_name(
    pg_pool: asyncpg.Pool, neo4j_driver: AsyncDriver
) -> None:
    """Job/JD side ONLY: `display_name` is always the raw JD text, cleartext
    — a job description carries no candidate PII, so this is always safe."""
    job_id = await _insert_job(pg_pool)
    async with pg_pool.acquire() as conn:
        await outbox_service.enqueue_outbox(
            conn,
            aggregate="job",
            aggregate_id=job_id,
            event_type="job.parsed",
            payload={
                "embedding": _vec(9),
                "extracted": {
                    "title": "Senior Backend Engineer",
                    "required_skills": [{"name": "Python", "min_years": 4}],
                    "nice_to_have_skills": [],
                    "min_years_experience": 4,
                    "education": {"min_level": "bachelors", "fields": []},
                    "location": None,
                    "remote_policy": None,
                    "responsibilities": [],
                },
                "prompt_version": "jd_extract_v1",
            },
        )
    await project_to_graph(_ctx(pg_pool, neo4j_driver), batch=10)

    async with neo4j_driver.session() as session:
        result = await session.run(
            "MATCH (s:Skill {canonical_key: 'python'}) RETURN s.display_name AS dn"
        )
        record = await result.single()
    assert record is not None
    assert record["dn"] == "Python"


@pytest.mark.asyncio
async def test_resume_side_non_vocab_skill_gets_a_hash_key_no_display_name_no_embedding(
    pg_pool: asyncpg.Pool, neo4j_driver: AsyncDriver
) -> None:
    """The résumé-side privacy control, proven end to end against a REAL
    Neo4j: a non-vocab skill name (not the special-cased vocab set) is never
    written cleartext, never gets a `display_name`, and never gets an
    `embedding` — the whole point of ADR-008."""
    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume(pg_pool, job_id)
    async with pg_pool.acquire() as conn:
        await outbox_service.enqueue_outbox(
            conn,
            aggregate="resume",
            aggregate_id=resume_id,
            event_type="resume.parsed",
            payload={
                "parsed": {
                    "total_years_experience": 6,
                    "skills": [{"name": "distributed systems", "years": 5}],
                    "experience": [],
                    "education": [],
                    "chunks": [],
                    "cover_letter_chunks": [],
                },
                "summary_emb": _vec(10),
                "chunk_embs": {},
                "prompt_version": "resume_core_v1+resume_skills_v2",
                "job_id": str(job_id),
            },
        )
    delivered = await project_to_graph(_ctx(pg_pool, neo4j_driver), batch=10)
    assert delivered == 1

    async with neo4j_driver.session() as session:
        result = await session.run(
            "MATCH (:Resume)-[:HAS_SKILL]->(s:Skill) "
            "RETURN s.canonical_key AS key, s.display_name AS dn, "
            "s.embedding AS emb, keys(s) AS ks"
        )
        record = await result.single()
    assert record is not None
    assert record["key"].startswith("h:")
    assert "distributed" not in record["key"]
    assert record["dn"] is None
    assert record["emb"] is None
    assert "display_name" not in record["ks"]
    assert "embedding" not in record["ks"]


@pytest.mark.asyncio
async def test_job_requirement_and_resume_skill_round_trip_on_a_non_vocab_skill(
    pg_pool: asyncpg.Pool, neo4j_driver: AsyncDriver
) -> None:
    """The recall-preserving invariant ADR-008 depends on: a JD requiring a
    non-vocab skill and a résumé having the IDENTICAL skill text must land
    on the SAME Skill node — REQUIRES and HAS_SKILL meet, so the skill
    actually scores."""
    job_id = await _insert_job(pg_pool)
    async with pg_pool.acquire() as conn:
        await outbox_service.enqueue_outbox(
            conn,
            aggregate="job",
            aggregate_id=job_id,
            event_type="job.parsed",
            payload={
                "embedding": _vec(11),
                "extracted": {
                    "title": "Senior Backend Engineer",
                    "required_skills": [
                        {"name": "Site Reliability Engineering", "min_years": 3}
                    ],
                    "nice_to_have_skills": [],
                    "min_years_experience": 4,
                    "education": {"min_level": "bachelors", "fields": []},
                    "location": None,
                    "remote_policy": None,
                    "responsibilities": [],
                },
                "prompt_version": "jd_extract_v1",
            },
        )
    resume_id = await _insert_resume(pg_pool, job_id)
    async with pg_pool.acquire() as conn:
        await outbox_service.enqueue_outbox(
            conn,
            aggregate="resume",
            aggregate_id=resume_id,
            event_type="resume.parsed",
            payload={
                "parsed": {
                    "total_years_experience": 6,
                    "skills": [{"name": "Site Reliability Engineering", "years": 5}],
                    "experience": [],
                    "education": [],
                    "chunks": [],
                    "cover_letter_chunks": [],
                },
                "summary_emb": _vec(12),
                "chunk_embs": {},
                "prompt_version": "resume_core_v1+resume_skills_v2",
                "job_id": str(job_id),
            },
        )

    delivered = await project_to_graph(_ctx(pg_pool, neo4j_driver), batch=10)
    assert delivered == 2

    assert (
        await _count(
            neo4j_driver,
            "MATCH (:Job)-[:REQUIRES]->(s:Skill)<-[:HAS_SKILL]-(:Resume) "
            "RETURN count(s) AS n",
        )
        == 1
    ), "JD requirement and résumé skill did not meet at the same Skill node"


@pytest.mark.asyncio
async def test_f1_vector_auto_merge_enriches_the_near_node_but_never_redirects_the_key(
    pg_pool: asyncpg.Pool, neo4j_driver: AsyncDriver
) -> None:
    """F1 (security re-audit round 2), the real-vector-index proof.

    Forces a REAL cosine-similarity hit (>= ``skill_auto_merge_threshold``)
    between a brand-new, non-vocab JD skill name ("react native") and an
    EXISTING vocab node ("react") — the exact scenario security's report
    describes as self-reinforcing and permanent pre-fix: once "react native"
    auto-merged into "react", every later JD mentioning it would take the
    poisoned exact-match branch too.

    Proves the auto-merge branch fires FOR REAL against a live Neo4j vector
    index (the "react" node gains "react native" as an alias — the only way
    to observe the branch actually ran) while the REQUIRES edge still lands
    on the pure ``_canonical_key_for_normalised("react native")`` hash —
    identical to what the résumé side (``resume_skill_canonical_key``, a
    pure function with no vector access at all) computes for the same text.
    """
    job_id_1 = await _insert_job(pg_pool)
    async with pg_pool.acquire() as conn:
        await outbox_service.enqueue_outbox(
            conn,
            aggregate="job",
            aggregate_id=job_id_1,
            event_type="job.parsed",
            payload={
                "embedding": _vec(30),
                "extracted": {
                    "title": "Frontend Engineer",
                    "required_skills": [{"name": "react", "min_years": 3}],
                    "nice_to_have_skills": [],
                    "min_years_experience": 3,
                    "education": {"min_level": "bachelors", "fields": []},
                    "location": None,
                    "remote_policy": None,
                    "responsibilities": [],
                },
                "prompt_version": "jd_extract_v1",
            },
        )

    ctx = _ctx(pg_pool, neo4j_driver)
    ctx["embedder"] = _make_embedder_forcing_near_duplicate("react", "react native")
    assert await project_to_graph(ctx, batch=10) == 1

    job_id_2 = await _insert_job(pg_pool)
    async with pg_pool.acquire() as conn:
        await outbox_service.enqueue_outbox(
            conn,
            aggregate="job",
            aggregate_id=job_id_2,
            event_type="job.parsed",
            payload={
                "embedding": _vec(31),
                "extracted": {
                    "title": "Mobile Engineer",
                    "required_skills": [{"name": "react native", "min_years": 2}],
                    "nice_to_have_skills": [],
                    "min_years_experience": 2,
                    "education": {"min_level": "bachelors", "fields": []},
                    "location": None,
                    "remote_policy": None,
                    "responsibilities": [],
                },
                "prompt_version": "jd_extract_v1",
            },
        )
    assert await project_to_graph(ctx, batch=10) == 1

    expected_key = skills_graph.resume_skill_canonical_key("React Native")
    assert expected_key is not None
    assert expected_key.startswith("h:")

    async with neo4j_driver.session() as session:
        result = await session.run(
            "MATCH (:Job {id: $jid})-[:REQUIRES]->(s:Skill) "
            "RETURN s.canonical_key AS key",
            jid=str(job_id_2),
        )
        record = await result.single()
    assert record is not None
    assert record["key"] == expected_key, (
        "auto-merge redirected the JD-side key to the near candidate's own "
        "key ('react') instead of the pure canonical_key_for_normalised() "
        "result — F1 regression"
    )
    assert record["key"] != "react"

    # Prove the vector branch actually fired FOR REAL (not merely skipped to
    # create-new because `near` came back empty): "react" gained "react
    # native" in its alias list as enrichment.
    async with neo4j_driver.session() as session:
        result = await session.run(
            "MATCH (s:Skill {canonical_key: 'react'}) RETURN s.aliases AS aliases"
        )
        record = await result.single()
    assert record is not None
    assert "react native" in (record["aliases"] or []), (
        "the vector auto-merge branch never fired against the real Neo4j "
        "vector index — the near-duplicate embedding fixture isn't forcing "
        "a real score >= skill_auto_merge_threshold hit"
    )


# ── R8: pinned label set ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pinned_label_set_after_both_projections(
    pg_pool: asyncpg.Pool, neo4j_driver: AsyncDriver
) -> None:
    job_id = await _insert_job(pg_pool)
    await _enqueue_job_parsed(pg_pool, job_id)
    resume_id = await _insert_resume(pg_pool, job_id)
    await _enqueue_resume_parsed(pg_pool, resume_id=resume_id, job_id=job_id)

    delivered = await project_to_graph(_ctx(pg_pool, neo4j_driver), batch=10)
    assert delivered == 2

    labels = await _all_labels(neo4j_driver)
    assert labels == PINNED_LABEL_SET, (
        f"unexpected label(s) in the graph: {labels - PINNED_LABEL_SET} "
        f"(R8 — Company/Institution must never be written by this module)"
    )


# ── Idempotency (R6: no duplicate aliases) ─────────────────────────────────


@pytest.mark.asyncio
async def test_draining_the_same_event_twice_does_not_duplicate_nodes_or_edges(
    pg_pool: asyncpg.Pool, neo4j_driver: AsyncDriver
) -> None:
    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume(pg_pool, job_id)
    await _enqueue_resume_parsed(pg_pool, resume_id=resume_id, job_id=job_id)
    await project_to_graph(_ctx(pg_pool, neo4j_driver), batch=10)

    resume_count_1 = await _count(neo4j_driver, "MATCH (r:Resume) RETURN count(r) AS n")
    chunk_count_1 = await _count(
        neo4j_driver, "MATCH (c:ResumeChunk) RETURN count(c) AS n"
    )
    skill_count_1 = await _count(neo4j_driver, "MATCH (s:Skill) RETURN count(s) AS n")

    # Simulate a re-parse: the SAME résumé, re-enqueued.
    await _enqueue_resume_parsed(pg_pool, resume_id=resume_id, job_id=job_id)
    await project_to_graph(_ctx(pg_pool, neo4j_driver), batch=10)

    assert (
        await _count(neo4j_driver, "MATCH (r:Resume) RETURN count(r) AS n")
        == resume_count_1
    )
    assert (
        await _count(neo4j_driver, "MATCH (c:ResumeChunk) RETURN count(c) AS n")
        == chunk_count_1
    )
    assert (
        await _count(neo4j_driver, "MATCH (s:Skill) RETURN count(s) AS n")
        == skill_count_1
    )

    aliases = await _all_aliases(neo4j_driver)
    assert len(aliases) == len(set(aliases)), f"duplicate skill alias(es): {aliases}"


# ── R7: concurrency (FOR UPDATE SKIP LOCKED) ───────────────────────────────


@pytest.mark.asyncio
async def test_two_concurrent_drainers_each_deliver_a_disjoint_set_exactly_once(
    pg_pool: asyncpg.Pool, neo4j_driver: AsyncDriver
) -> None:
    """R7 (MED). Without ``FOR UPDATE SKIP LOCKED`` both drainers can select
    and project the SAME rows, double-delivering them.

    S13 (round-5 security re-audit): ``outbox_drain_deadline_seconds``
    defaults to 4.0s (a real, intentional wall-clock budget — see
    ``project_to_graph``'s F5 docstring). Two drainers concurrently
    projecting 3 résumés each to Neo4j can legitimately exceed that on a
    cold/loaded host, hit the deadline, and early-exit having left a
    locked-but-not-yet-attempted row behind — correct bounded-work
    behaviour, not a locking bug (``FOR UPDATE SKIP LOCKED`` still holds:
    no double-delivery, no data loss). This test exists to exercise the
    LOCK, not the timer, so the deadline is pinned large enough that
    neither drainer ever hits it on any reasonable host — matching the
    dead-letter test below, which already pins
    ``outbox_max_delivery_attempts`` for the same reason.
    """
    from src.worker import graph_tasks

    job_id = await _insert_job(pg_pool)
    resume_ids = [await _insert_resume(pg_pool, job_id) for _ in range(6)]
    for resume_id in resume_ids:
        await _enqueue_resume_parsed(pg_pool, resume_id=resume_id, job_id=job_id)

    ctx_a = _ctx(pg_pool, neo4j_driver)
    ctx_b = _ctx(pg_pool, neo4j_driver)

    generous_settings = Settings(outbox_drain_deadline_seconds=120.0)
    with patch.object(graph_tasks, "get_settings", return_value=generous_settings):
        delivered_a, delivered_b = await asyncio.gather(
            project_to_graph(ctx_a, batch=3), project_to_graph(ctx_b, batch=3)
        )

    total_delivered = delivered_a + delivered_b
    assert total_delivered == len(resume_ids)

    async with pg_pool.acquire() as conn:
        undelivered = await conn.fetchval(
            "SELECT count(*) FROM outbox WHERE delivered_at IS NULL"
        )
    assert undelivered == 0

    # Each résumé's Resume node exists exactly once — a double-delivery would
    # not necessarily duplicate the node (MERGE is idempotent) but WOULD
    # indicate the same row was projected by both drainers concurrently,
    # which the delivered-row accounting above already rules out; this is a
    # belt-and-braces structural check on the outcome.
    resume_count = await _count(neo4j_driver, "MATCH (r:Resume) RETURN count(r) AS n")
    assert resume_count == len(resume_ids)


# ── Decision 2: poison row + dead-letter ───────────────────────────────────


@pytest.mark.asyncio
async def test_poison_row_does_not_block_its_batch_mates(
    pg_pool: asyncpg.Pool, neo4j_driver: AsyncDriver
) -> None:
    job_id = await _insert_job(pg_pool)
    poison_id = await _insert_resume(pg_pool, job_id)
    good_id = await _insert_resume(pg_pool, job_id)

    # A malformed payload (missing 'parsed' entirely) makes projection raise
    # for every attempt — a realistic poison row (e.g. a future schema
    # change that shipped a payload shape the current code can't handle).
    async with pg_pool.acquire() as conn:
        await outbox_service.enqueue_outbox(
            conn,
            aggregate="resume",
            aggregate_id=poison_id,
            event_type="resume.parsed",
            payload={"job_id": str(job_id)},  # no 'parsed'/'summary_emb'/'chunk_embs'
        )
    await _enqueue_resume_parsed(pg_pool, resume_id=good_id, job_id=job_id)

    delivered = await project_to_graph(_ctx(pg_pool, neo4j_driver), batch=10)
    assert delivered == 1

    async with pg_pool.acquire() as conn:
        poison_row = await conn.fetchrow(
            "SELECT delivered_at, delivery_attempts FROM outbox "
            "WHERE aggregate_id = $1",
            poison_id,
        )
        good_row = await conn.fetchrow(
            "SELECT delivered_at, delivery_attempts FROM outbox "
            "WHERE aggregate_id = $1",
            good_id,
        )
    assert poison_row["delivered_at"] is None
    assert poison_row["delivery_attempts"] == 1
    assert good_row["delivered_at"] is not None


@pytest.mark.asyncio
async def test_poison_row_is_dead_lettered_at_the_cap_and_stops_being_retried(
    pg_pool: asyncpg.Pool, neo4j_driver: AsyncDriver
) -> None:
    """Decision 2. Drain past ``outbox_max_delivery_attempts`` and prove the
    row stops accumulating attempts — it is excluded from the SELECT, not
    retried forever the way hris does."""
    from src.worker import graph_tasks

    job_id = await _insert_job(pg_pool)
    poison_id = await _insert_resume(pg_pool, job_id)
    async with pg_pool.acquire() as conn:
        await outbox_service.enqueue_outbox(
            conn,
            aggregate="resume",
            aggregate_id=poison_id,
            event_type="resume.parsed",
            payload={"job_id": str(job_id)},
        )

    cap = 3
    custom_settings = Settings(outbox_max_delivery_attempts=cap)
    ctx = _ctx(pg_pool, neo4j_driver)

    with patch.object(graph_tasks, "get_settings", return_value=custom_settings):
        for _ in range(cap + 2):
            await project_to_graph(ctx, batch=10)

    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT delivered_at, delivery_attempts FROM outbox "
            "WHERE aggregate_id = $1",
            poison_id,
        )
    assert row["delivered_at"] is None
    assert row["delivery_attempts"] == cap, (
        "delivery_attempts kept incrementing past the dead-letter cap — the "
        "SELECT must exclude rows at/past settings.outbox_max_delivery_attempts"
    )
