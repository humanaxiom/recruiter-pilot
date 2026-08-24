"""Integration tests — the skill-display-names bug (branch
``fix/skill-display-names-and-corpus-gap``).

ADR-008 hashes any skill outside the curated vocabulary
(``src.pipeline.skills_graph._canonical_key_for_normalised``): a vocab hit
returns the cleartext normalised string, a non-vocab name returns
``h:<hash>``. The JD side already writes a cleartext ``Skill.display_name``
(``src.worker.tasks._job_projection_tx``) — but until this fix, NOTHING in
``src/`` ever READS it: Stage 2's Cypher
(``src.pipeline.matching.orchestrator._stage2_skill_rows``) returns
``reqSkill.canonical_key AS skill`` directly, so a hashed non-vocab skill
renders as ``h:6fe660ff...`` in the UI/CSV export, and a vocab-hit skill
renders as its lowercased matching key (``python``) instead of the JD's own
wording (``Python``).

Target implementation this file pins:

1. ``_job_projection_tx`` also sets the JD's raw skill wording on the
   REQUIRES/NICE_TO_HAVE **relationship** (``r.display_name``), not just the
   Skill node — so each JOB renders its OWN wording instead of a single,
   global, last-writer-wins node property.
2. ``_stage2_skill_rows`` reads a TWO-way fallback: the relationship's
   ``display_name``, else the bare ``canonical_key``. The node-level
   ``Skill.display_name`` is deliberately NOT a rung in that chain — see the
   security note below.

**Security note (merge-blocking security gate, finding S2 — MEDIUM, a real
cross-job leak).** An earlier revision of this branch read a THREE-way
fallback (``coalesce(req.display_name, reqSkill.display_name,
reqSkill.canonical_key)``). ``Skill.display_name`` is written GLOBALLY and
last-writer-wins (``src.worker.tasks._job_projection_tx``:
``MATCH (s:Skill {canonical_key: $cname}) SET s.display_name = $display``),
so the middle rung rendered **another job's JD wording** to this job's
assignees for every ``REQUIRES`` edge projected before this branch landed —
i.e. the entire pre-existing graph until a re-parse — crossing the per-job
authorization boundary that ``_SHORTLIST_ASSIGNEE_EXISTS_SQL`` enforces, and
compounding ADR-008 residual #4 (résumé text pasted into a JD skill field by
job B's author would surface under job A). The middle rung is GONE: a stale
edge now renders the safe, opaque ``canonical_key``, and a re-parse restores
readability — the same stale-row posture already accepted on this branch for
pre-existing ``shortlist_entries`` (they keep hashed labels until
regenerated). ``test_stale_edge_falls_back_to_canonical_key_not_node_wording``
and ``test_two_jobs_cannot_read_each_others_wording_via_node_property``
below pin the SECURE behaviour; the former REPLACED an earlier
``test_fallback_to_node_display_name_when_relationship_property_absent``
that pinned exactly the behaviour the security gate ordered removed.

**Security note (finding S1 — MEDIUM, an unpinned invariant).** Security
mutated the Cypher to ``coalesce(has.evidence_chunk_id, req.display_name,
...)`` — promoting a **résumé-side** property (ADR-008 residual #3: a pointer
back to a résumé region) to the highest-priority source of the rendered skill
label — and the FULL gate stayed green; only a non-string control mutation
(``has.years``) was caught, and only by pydantic's ``str`` type. The label
must be **JD-authored only**.
``test_resume_side_property_is_never_the_rendered_skill_label`` is the
regression pin, backed by the cheap source-level assertion in
``tests/unit/test_stage2_skill_label_source.py``.

These tests seed EXCLUSIVELY through the REAL projection path
(``src.worker.graph_tasks.project_to_graph`` -> ``src.worker.tasks.
_project_job`` -> ``_job_projection_tx``, via a real ``job.parsed`` outbox
event — and, for the résumé side, ``src.worker.resume_tasks.project_resume``
-> ``_resume_projection_tx`` via a real ``resume.parsed`` event), never a
hand-rolled ``MERGE`` — unlike ``test_matching_orchestrator.py``'s
``_seed_requires`` helper (a bare ``MERGE (s:Skill {canonical_key: $key})``),
which is exactly why NO Skill node anywhere in that file's suite ever carries
a ``display_name`` and why that suite was structurally blind to this bug.

RED status (verified against current ``src/``, ``./scripts/verify.sh
integration``): the original 4 of 5 tests failed for the right reason (Stage 2
returned the bare/lowercased ``canonical_key`` where a display name is
expected). ``test_fallback_to_canonical_key_when_neither_display_name_present``
already passed against unfixed ``src/`` — see its own docstring for why that
is a genuine, not a weak, test. The three security-driven tests named above
were RED against the three-way-fallback revision of ``src/``.
"""

from __future__ import annotations

import random
import re
import zlib
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import asyncpg
import pytest
from neo4j import AsyncDriver, AsyncGraphDatabase
from testcontainers.neo4j import Neo4jContainer
from testcontainers.postgres import PostgresContainer

from src.models.ddl import init_schema
from src.pipeline.matching.orchestrator import _stage2_skill_rows
from src.services import outbox_service
from src.settings import get_settings
from src.worker.graph_tasks import project_to_graph
from src.worker.neo4j_bootstrap import bootstrap_neo4j_schema

_DIM = get_settings().llm_embedding_dim


def _vec(seed: int) -> list[float]:
    """Deterministic near-orthogonal 768-d vector — same technique as
    ``test_graph_projection_e2e.py``'s ``_vec`` (a per-seed ``random.Random``
    draw, not a linear ramp, so distinct skill names don't accidentally land
    inside the vector auto-merge/tiebreaker band)."""
    rng = random.Random(seed)
    return [rng.uniform(-1.0, 1.0) for _ in range(_DIM)]


# ── fixtures (same shape as test_graph_projection_e2e.py / test_matching_
# orchestrator.py) ──────────────────────────────────────────────────────────


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


def _make_llm() -> MagicMock:
    return MagicMock(chat_json=AsyncMock(return_value=MagicMock(match=None)))


def _make_embedder() -> MagicMock:
    async def _embed(texts: Any) -> list[list[float]]:
        # Deterministic per TEXT: identical strings embed identically, and
        # distinct skill names land near-orthogonal (see ``_vec``'s
        # docstring) — the vector auto-merge branch never fires by accident
        # for any of the skill names these tests use.
        return [_vec(zlib.crc32(t.encode())) for t in texts]

    return MagicMock(embed=AsyncMock(side_effect=_embed))


def _ctx(pg_pool: asyncpg.Pool, neo4j_driver: AsyncDriver) -> dict[str, Any]:
    return {
        "pg_pool": pg_pool,
        "neo4j": neo4j_driver,
        "llm": _make_llm(),
        "embedder": _make_embedder(),
    }


# ── Postgres/outbox seeding — the REAL projection path ─────────────────────


async def _insert_job(pool: asyncpg.Pool, *, title: str = "Staff Engineer") -> UUID:
    async with pool.acquire() as conn:
        job_id: UUID = await conn.fetchval(
            "INSERT INTO jobs (title, description_raw) VALUES ($1, $2) RETURNING id",
            title,
            "raw jd text (irrelevant to these tests)",
        )
    return job_id


async def _project_job_with_skills(
    pool: asyncpg.Pool,
    neo4j_driver: AsyncDriver,
    job_id: UUID,
    *,
    required_skills: list[dict[str, Any]] | None = None,
    nice_to_have_skills: list[dict[str, Any]] | None = None,
    embedding_seed: int,
) -> None:
    """Enqueue a REAL ``job.parsed`` outbox event and drain it through the
    REAL ``project_to_graph`` -> ``_project_job`` -> ``_job_projection_tx``
    path — never a hand-rolled ``MERGE``. This is the fixture-seeding
    discipline this file exists to correct (see module docstring)."""
    payload = {
        "embedding": _vec(embedding_seed),
        "extracted": {
            "title": "Staff Engineer",
            "required_skills": required_skills or [],
            "nice_to_have_skills": nice_to_have_skills or [],
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
    delivered = await project_to_graph(_ctx(pool, neo4j_driver), batch=10)
    assert delivered == 1, "the job.parsed outbox event did not drain — fixture bug"


async def _bare_resume_node(driver: AsyncDriver, resume_id: UUID) -> None:
    async with driver.session() as session:
        await session.run("MERGE (:Resume {id: $rid})", rid=str(resume_id))


async def _insert_resume(pool: asyncpg.Pool, job_id: UUID) -> UUID:
    async with pool.acquire() as conn:
        resume_id: UUID = await conn.fetchval(
            """
            INSERT INTO resumes (
                job_id, blob_key, original_filename, mime_type,
                file_size_bytes, sha256, consent_acknowledged, status
            ) VALUES ($1, $2, 'resume.pdf', 'application/pdf', 1024, $3, TRUE, 'parsed')
            RETURNING id
            """,
            job_id,
            f"resumes/{uuid4().hex}.pdf",
            uuid4().hex,
        )
    return resume_id


async def _project_resume_with_skill(
    pool: asyncpg.Pool,
    neo4j_driver: AsyncDriver,
    resume_id: UUID,
    job_id: UUID,
    *,
    skill_name: str,
    evidence_chunk_id: str,
    embedding_seed: int,
) -> None:
    """Drain a REAL ``resume.parsed`` event through the REAL résumé projection
    (``project_to_graph`` -> ``project_resume`` -> ``_resume_projection_tx``).
    That path is what actually writes ``h.evidence_chunk_id`` on the HAS_SKILL
    edge (``resume_tasks.py``'s ``SET h.years = $years, h.last_used_year =
    $luy, h.evidence_chunk_id = $first_ev``) — a hand-rolled MERGE here could
    invent a property shape production never produces."""
    payload = {
        "parsed": {
            "total_years_experience": 6,
            "skills": [
                {
                    "name": skill_name,
                    "years": 6,
                    "last_used_year": 2026,
                    "evidence_chunk_ids": [evidence_chunk_id],
                }
            ],
            "experience": [],
            "education": [],
            "chunks": [{"id": evidence_chunk_id, "section": "experience", "page": 1}],
            "cover_letter_chunks": [],
        },
        "summary_emb": _vec(embedding_seed),
        "chunk_embs": {evidence_chunk_id: _vec(embedding_seed + 1)},
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
    delivered = await project_to_graph(_ctx(pool, neo4j_driver), batch=10)
    assert delivered == 1, "the resume.parsed outbox event did not drain — fixture bug"


async def _has_skill_edge_property(
    driver: AsyncDriver, resume_id: UUID, prop: str
) -> Any:
    async with driver.session() as session:
        result = await session.run(
            f"MATCH (:Resume {{id: $rid}})-[h:HAS_SKILL]->(:Skill) "
            f"RETURN h.{prop} AS value",
            rid=str(resume_id),
        )
        record = await result.single()
    assert record is not None, "no HAS_SKILL edge was projected — fixture bug"
    return record["value"]


async def _strip_relationship_display_name(driver: AsyncDriver, job_id: UUID) -> None:
    """Simulates a PRE-FIX row: a REQUIRES edge with no ``display_name``
    property at all (every edge projected before this fix lands looks
    exactly like this). Scoped by job_id only -- every test that calls this
    projects exactly one required skill for that job."""
    async with driver.session() as session:
        await session.run(
            "MATCH (:Job {id: $jid})-[r:REQUIRES]->(:Skill) REMOVE r.display_name",
            jid=str(job_id),
        )


async def _strip_node_display_name(driver: AsyncDriver, job_id: UUID) -> None:
    async with driver.session() as session:
        await session.run(
            "MATCH (:Job {id: $jid})-[:REQUIRES]->(s:Skill) REMOVE s.display_name",
            jid=str(job_id),
        )


async def _job_required_skill_canonical_key(driver: AsyncDriver, job_id: UUID) -> str:
    """The REAL canonical_key Neo4j actually stored for a job's (single)
    required skill -- read back from the graph itself, never recomputed by
    calling a production hashing function directly, so this stays a
    black-box assertion of what Stage 2's fallback chain SHOULD terminate
    at."""
    async with driver.session() as session:
        result = await session.run(
            "MATCH (:Job {id: $jid})-[:REQUIRES]->(s:Skill) "
            "RETURN s.canonical_key AS key",
            jid=str(job_id),
        )
        record = await result.single()
    assert record is not None
    key: str = record["key"]
    return key


# ── 1. a non-vocab required skill renders its human-readable name ──────────


@pytest.mark.asyncio
async def test_non_vocab_required_skill_renders_display_name_not_hash(
    pg_pool: asyncpg.Pool, neo4j_driver: AsyncDriver
) -> None:
    job_id = await _insert_job(pg_pool)
    resume_id = uuid4()
    await _bare_resume_node(neo4j_driver, resume_id)
    await _project_job_with_skills(
        pg_pool,
        neo4j_driver,
        job_id,
        required_skills=[{"name": "Quantum Widgetry Ops", "min_years": 2}],
        embedding_seed=101,
    )

    rows = await _stage2_skill_rows(neo4j_driver, job_id, resume_id)

    assert len(rows) == 1
    assert rows[0].skill == "Quantum Widgetry Ops", (
        f"expected the JD's own human-readable wording, got {rows[0].skill!r} "
        "-- Stage 2 is still returning the bare (possibly hashed) "
        "canonical_key instead of reading a display name"
    )
    assert not rows[0].skill.startswith("h:")


# ── 2. a vocab-hit required skill renders the JD's OWN wording, not the
#       lowercased matching key ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_vocab_hit_required_skill_renders_jd_wording_not_lowercase_key(
    pg_pool: asyncpg.Pool, neo4j_driver: AsyncDriver
) -> None:
    job_id = await _insert_job(pg_pool)
    resume_id = uuid4()
    await _bare_resume_node(neo4j_driver, resume_id)
    await _project_job_with_skills(
        pg_pool,
        neo4j_driver,
        job_id,
        required_skills=[{"name": "Python", "min_years": 4}],
        embedding_seed=102,
    )

    rows = await _stage2_skill_rows(neo4j_driver, job_id, resume_id)

    assert len(rows) == 1
    assert rows[0].skill == "Python", (
        f"expected the JD's own wording 'Python', got {rows[0].skill!r} -- "
        "Stage 2 is returning the lowercased matching canonical_key "
        "('python') instead of a display name"
    )


# ── 3. fallback chain: relationship -> canonical_key (NO node rung) ────────


@pytest.mark.asyncio
async def test_stale_edge_falls_back_to_canonical_key_not_node_wording(
    pg_pool: asyncpg.Pool, neo4j_driver: AsyncDriver
) -> None:
    """A row that predates this fix has a node-level ``display_name``
    (ADR-008, pre-existing) but NO relationship-level one.

    **This test REPLACES ``test_fallback_to_node_display_name_when_
    relationship_property_absent``, which asserted the opposite** — that the
    node property was rendered. That behaviour was removed on the
    merge-blocking security gate's finding S2 (MEDIUM, cross-job leak):
    ``Skill.display_name`` is a single GLOBAL, last-writer-wins property, so
    reading it here renders whichever job projected LAST — another job's JD
    wording, shown to this job's assignees, across the per-job authorization
    boundary. See this module's docstring for the full rationale.

    The secure behaviour: fall all the way through to the opaque
    ``canonical_key``. Readability comes back on the next re-parse, which
    stamps the per-job relationship property."""
    job_id = await _insert_job(pg_pool)
    resume_id = uuid4()
    await _bare_resume_node(neo4j_driver, resume_id)
    await _project_job_with_skills(
        pg_pool,
        neo4j_driver,
        job_id,
        required_skills=[{"name": "Distributed Systems Design", "min_years": 3}],
        embedding_seed=103,
    )
    canonical_key = await _job_required_skill_canonical_key(neo4j_driver, job_id)
    await _strip_relationship_display_name(neo4j_driver, job_id)

    # Precondition: the node-level display_name IS still there — this test is
    # only meaningful if Stage 2 had something unsafe available to render.
    async with neo4j_driver.session() as session:
        result = await session.run(
            "MATCH (:Job {id: $jid})-[:REQUIRES]->(s:Skill) "
            "RETURN s.display_name AS dn",
            jid=str(job_id),
        )
        record = await result.single()
    assert record is not None and record["dn"] == "Distributed Systems Design", (
        "fixture precondition failed: the node-level display_name is not "
        "present, so this test could not observe the leak it guards"
    )

    rows = await _stage2_skill_rows(neo4j_driver, job_id, resume_id)

    assert len(rows) == 1
    assert rows[0].skill == canonical_key, (
        f"expected the stale edge to fall back to the opaque canonical_key "
        f"({canonical_key!r}), got {rows[0].skill!r}"
    )
    assert rows[0].skill != "Distributed Systems Design", (
        "Stage 2 rendered the GLOBAL, last-writer-wins node-level "
        "display_name — security finding S2: that property belongs to "
        "whichever job projected last, not to this one"
    )


@pytest.mark.asyncio
async def test_fallback_to_canonical_key_when_neither_display_name_present(
    pg_pool: asyncpg.Pool, neo4j_driver: AsyncDriver
) -> None:
    """No relationship display_name AND no node display_name (an edge case
    the fallback chain must still survive without crashing or returning an
    empty string) -- falls all the way back to the bare canonical_key.

    NOTE (RED-status honesty): this ONE test, unlike its four siblings in
    this file, already passes against current (unfixed) ``src/`` --
    ``_stage2_skill_rows`` today ALWAYS returns the bare ``canonical_key``
    (it doesn't read any display name at all yet), which happens to be
    EXACTLY the value this specific edge case expects. That is not a weak
    test: it pins a genuine, permanent invariant of the fallback chain
    ("never crash, never render empty on a totally undecorated row") that
    must hold both BEFORE and AFTER the fix -- there is no correct
    implementation of the fallback chain that could make this one
    observably different. The other tests in this file (vocab-hit wording,
    non-vocab hashing, the stale-edge fallback, per-job wording, and the two
    security pins) are what actually prove the fix landed.
    """
    job_id = await _insert_job(pg_pool)
    resume_id = uuid4()
    await _bare_resume_node(neo4j_driver, resume_id)
    await _project_job_with_skills(
        pg_pool,
        neo4j_driver,
        job_id,
        required_skills=[{"name": "Distributed Systems Design", "min_years": 3}],
        embedding_seed=104,
    )
    canonical_key = await _job_required_skill_canonical_key(neo4j_driver, job_id)
    await _strip_relationship_display_name(neo4j_driver, job_id)
    await _strip_node_display_name(neo4j_driver, job_id)

    rows = await _stage2_skill_rows(neo4j_driver, job_id, resume_id)

    assert len(rows) == 1
    assert rows[0].skill == canonical_key, (
        f"expected the final fallback to the bare canonical_key "
        f"({canonical_key!r}), got {rows[0].skill!r}"
    )
    assert rows[
        0
    ].skill, "fallback chain returned an empty string, not a crash-safe value"


# ── 4. per-job wording: two jobs, same underlying skill, different raw text


@pytest.mark.asyncio
async def test_two_jobs_same_skill_different_wording_render_own_wording(
    pg_pool: asyncpg.Pool, neo4j_driver: AsyncDriver
) -> None:
    """The whole point of the per-job (relationship) display name: a global,
    node-level, last-writer-wins property could only ever show ONE job's
    wording for a shared canonical skill. "ReactJS" and "React JS" are both
    aliases.yaml entries for the SAME canonical skill ('react') -- two
    different jobs requiring it with different raw wording must each render
    their OWN wording."""
    job_id_1 = await _insert_job(pg_pool, title="Frontend Engineer I")
    job_id_2 = await _insert_job(pg_pool, title="Frontend Engineer II")
    resume_id = uuid4()
    await _bare_resume_node(neo4j_driver, resume_id)

    await _project_job_with_skills(
        pg_pool,
        neo4j_driver,
        job_id_1,
        required_skills=[{"name": "ReactJS", "min_years": 2}],
        embedding_seed=105,
    )
    await _project_job_with_skills(
        pg_pool,
        neo4j_driver,
        job_id_2,
        required_skills=[{"name": "React JS", "min_years": 3}],
        embedding_seed=106,
    )

    rows_1 = await _stage2_skill_rows(neo4j_driver, job_id_1, resume_id)
    rows_2 = await _stage2_skill_rows(neo4j_driver, job_id_2, resume_id)

    assert len(rows_1) == 1
    assert len(rows_2) == 1
    assert rows_1[0].skill == "ReactJS", (
        f"job 1 must render its OWN JD wording 'ReactJS', got "
        f"{rows_1[0].skill!r} -- a global node-level display_name would "
        "show whichever job projected LAST for both jobs"
    )
    assert rows_2[0].skill == "React JS", (
        f"job 2 must render its OWN JD wording 'React JS', got " f"{rows_2[0].skill!r}"
    )
    assert rows_1[0].skill != rows_2[0].skill, (
        "both jobs rendered the SAME skill label -- the per-job "
        "(relationship-level) display name is not actually per-job"
    )


# ── 5. security S2: no cross-job wording leak through the GLOBAL node
#       property, even for a stale (pre-fix) edge ────────────────────────────


@pytest.mark.asyncio
async def test_two_jobs_cannot_read_each_others_wording_via_node_property(
    pg_pool: asyncpg.Pool, neo4j_driver: AsyncDriver
) -> None:
    """Security finding S2 (MEDIUM) — the cross-job leak, stated directly.

    ``Skill.display_name`` is written by ``_job_projection_tx`` with a
    GLOBALLY scoped ``MATCH (s:Skill {canonical_key: $cname})`` — no job in
    the pattern at all — so the LAST job to project a given canonical skill
    owns that property for every job. Here job 2 ("React JS") projects after
    job 1 ("ReactJS"), then job 1's edge is stripped of its relationship
    property to simulate a pre-fix row. Job 1's shortlist must NOT render job
    2's JD wording: it renders the opaque canonical key instead."""
    job_id_1 = await _insert_job(pg_pool, title="Frontend Engineer I")
    job_id_2 = await _insert_job(pg_pool, title="Frontend Engineer II")
    resume_id = uuid4()
    await _bare_resume_node(neo4j_driver, resume_id)

    await _project_job_with_skills(
        pg_pool,
        neo4j_driver,
        job_id_1,
        required_skills=[{"name": "ReactJS", "min_years": 2}],
        embedding_seed=107,
    )
    await _project_job_with_skills(
        pg_pool,
        neo4j_driver,
        job_id_2,
        required_skills=[{"name": "React JS", "min_years": 3}],
        embedding_seed=108,
    )
    canonical_key = await _job_required_skill_canonical_key(neo4j_driver, job_id_1)
    key_2 = await _job_required_skill_canonical_key(neo4j_driver, job_id_2)
    assert canonical_key == key_2, (
        "fixture precondition failed: the two jobs did not land on the SAME "
        "Skill node, so there is no shared node property to leak through"
    )

    # Job 1's edge now looks like every REQUIRES edge projected before this
    # branch: no relationship-level display_name at all.
    await _strip_relationship_display_name(neo4j_driver, job_id_1)

    rows_1 = await _stage2_skill_rows(neo4j_driver, job_id_1, resume_id)
    rows_2 = await _stage2_skill_rows(neo4j_driver, job_id_2, resume_id)

    assert len(rows_1) == 1
    assert len(rows_2) == 1
    assert rows_1[0].skill != "React JS", (
        "CROSS-JOB LEAK: job 1's shortlist rendered job 2's JD wording "
        "('React JS') through the global, last-writer-wins node-level "
        "Skill.display_name -- security finding S2"
    )
    assert rows_1[0].skill == canonical_key, (
        f"job 1's stale edge must render the opaque canonical_key "
        f"({canonical_key!r}), got {rows_1[0].skill!r}"
    )
    # Job 2 is unaffected: its own relationship property is intact.
    assert rows_2[0].skill == "React JS"


# ── 6. security S1: the label is JD-authored ONLY — no résumé-side property
#       may ever become the rendered skill name ─────────────────────────────


@pytest.mark.asyncio
async def test_resume_side_property_is_never_the_rendered_skill_label(
    pg_pool: asyncpg.Pool, neo4j_driver: AsyncDriver
) -> None:
    """Security finding S1 (MEDIUM) — an unpinned invariant.

    Security mutated Stage 2's Cypher to ``coalesce(has.evidence_chunk_id,
    req.display_name, ...)`` and the FULL gate stayed green: no test pinned
    that the rendered label comes from the **JD side only**. A HAS_SKILL edge
    property is résumé-derived (``evidence_chunk_id`` is ADR-008 residual #3,
    a pointer back to a résumé region) and must never reach the UI/CSV as a
    skill name.

    This seeds a real ``HAS_SKILL`` edge carrying a STRING résumé-side
    property (so pydantic's ``str`` type cannot be what catches the mutant —
    that is exactly how security's ``has.years`` control mutation died) on the
    same Skill the JD requires, and asserts the sentinel appears in NO
    returned label."""
    sentinel = "c_001_SENTINEL"
    job_id = await _insert_job(pg_pool)
    await _project_job_with_skills(
        pg_pool,
        neo4j_driver,
        job_id,
        required_skills=[{"name": "Python", "min_years": 4}],
        embedding_seed=109,
    )
    resume_id = await _insert_resume(pg_pool, job_id)
    await _project_resume_with_skill(
        pg_pool,
        neo4j_driver,
        resume_id,
        job_id,
        skill_name="Python",
        evidence_chunk_id=sentinel,
        embedding_seed=110,
    )

    # Precondition: the résumé-side property really is on the edge, and it is
    # a STRING — otherwise the mutant this test exists to kill would be caught
    # by the schema's type coercion instead of by this assertion.
    edge_value = await _has_skill_edge_property(
        neo4j_driver, resume_id, "evidence_chunk_id"
    )
    assert edge_value == sentinel and isinstance(edge_value, str), (
        "fixture precondition failed: HAS_SKILL carries no string "
        f"evidence_chunk_id (got {edge_value!r})"
    )

    rows = await _stage2_skill_rows(neo4j_driver, job_id, resume_id)

    assert len(rows) == 1
    # The edge really did join (this is not vacuously passing on a missing
    # HAS_SKILL match).
    assert rows[0].years == 6, (
        "fixture precondition failed: the HAS_SKILL edge did not join, so no "
        "résumé-side property was even reachable by the coalesce"
    )
    assert (
        rows[0].skill == "Python"
    ), f"expected the JD's own wording 'Python', got {rows[0].skill!r}"
    assert all(sentinel not in (r.skill or "") for r in rows), (
        "a RÉSUMÉ-side property reached the rendered skill label -- security "
        "finding S1: the label must be JD-authored only"
    )
