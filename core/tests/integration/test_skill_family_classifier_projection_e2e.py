"""Integration -- ROADMAP A2, Phase 3.3 skill-family classifier.

**Slice 1** (original) proved: a hashed (out-of-vocab) skill whose parse-time-
assigned ``categories`` ride the ``resume.parsed`` outbox payload really do
land on the projected ``Skill`` node, and a JD requiring an in-vocab skill in
that SAME family really does earn stage-2 family credit it would otherwise
score 0.0 / ``reason="missing"`` for.

**Slice 2** (this update, 2026-08-19 decision memo): live measurement against
the real tailnet ``gpt-oss:20b`` found the classifier stably mis-families
domain-expert phrases ("expert scientific knowledge of MRI and MEG research
methods" -> ``bi``). Family credit is transitive across the WHOLE résumé, so
granting credit on a misfire tells a recruiter a candidate holds a
qualification they do not. The decision (ADR-040/ADR-041 precedent -- record
the fact, defer the scoring change) is implemented as TWO changes this file
now pins, CHANGING two of slice 1's own tests:

1. **A separate graph property.** The classifier's assignment now lands on
   ``Skill.classified_categories``, NEVER on ``Skill.categories`` (curated,
   ``ensure_categories``-only, forever). CHANGED:
   ``test_hashed_skill_node_gets_classifier_categories_from_the_payload``
   used to assert ``s.categories == ["backend"]`` for the hashed node; it now
   asserts ``s.classified_categories == ["backend"]`` AND ``s.categories IS
   NULL`` for that same node.
2. **A new flag, ``match_use_classified_families`` (default ``False``),
   threaded into ``MatchingContext.use_classified_families`` and honoured by
   ``_stage2_skill_rows``.** With it off (the default), the family-credit arm
   must ignore ``classified_categories`` entirely. CHANGED:
   ``test_hashed_skill_family_credit_where_it_previously_scored_missing``
   used to call ``_stage2_skill_rows(neo4j_driver, job_id, resume_id)`` with
   no flag and expect credit; that was slice 1's WHOLE point but is exactly
   the behaviour slice 2 must now gate behind an explicit opt-in, so this
   test now passes ``use_classified_families=True`` explicitly. The new
   ``test_hashed_skill_classified_categories_earn_nothing_with_flag_off_default``
   below pins the mirror-image claim (same graph, default call, ``missing``)
   and ``test_ranking_neutral_stage2_rows_identical_with_and_without_classifier_data``
   is the load-bearing safety proof: a REAL comparison of a graph WITH
   classifier data against one WITHOUT any, both scored with the flag off,
   asserting the stage-2 rows and ``score_skill_breakdown`` outputs are
   BYTE-IDENTICAL -- not an assertion that one number looks right.

The classifier itself is NOT exercised here -- it runs at PARSE time, calls
an LLM, and this slice's entire design claim is that PROJECTION gains no LLM
call at all (see ADR-044's "The design" section, and the four
ADR-008 tests in ``test_worker_project_resume.py`` this slice must leave
green). This file seeds the outbox payload's ``categories`` field directly,
exactly as ``_extract_skills_merged`` would already have attached it before
``parse_resume`` ever enqueues the row.

RED, by construction, against the current source: ``Skill.classified_categories``
does not exist yet (the current uncommitted projection code writes a hashed
skill's payload categories onto ``Skill.categories``, which is exactly the
provenance conflation this slice forbids), and neither
``MatchingContext.use_classified_families`` nor ``_stage2_skill_rows``'s
``use_classified_families`` keyword exist yet either.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Iterator
from typing import Any

import asyncpg
import pytest
from neo4j import AsyncDriver, AsyncGraphDatabase
from testcontainers.neo4j import Neo4jContainer
from testcontainers.postgres import PostgresContainer

from src.models.ddl import init_schema
from src.pipeline.matching.orchestrator import _stage2_skill_rows
from src.pipeline.matching.stages import score_skill_breakdown
from src.schemas.matching import DEFAULT_WEIGHTS
from src.services import outbox_service
from src.worker.graph_tasks import project_to_graph
from src.worker.neo4j_bootstrap import bootstrap_neo4j_schema

# Reuse, not a second ad hoc copy: `tests/integration/` is a real package
# (`tests/__init__.py` + `tests/integration/__init__.py` both exist), so the
# sibling `test_graph_projection_e2e.py`'s helpers ARE importable, and the
# TESTER-agent instruction for this file was explicit: import if importable,
# only mirror otherwise. `_vec`'s near-orthogonality across distinct seeds
# (see that module's own docstring on `_vec`) is exactly what keeps the two
# tests below off the vector auto-merge/tiebreaker enrichment branches --
# duplicating the formula here would risk a silent drift from that property.
from tests.integration.test_graph_projection_e2e import _make_embedder, _make_llm, _vec

# ── fixtures (same technique as test_graph_projection_e2e.py) ─────────────


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


# NOTE (originally a DATA-PIPELINE-CODER fix, replaced with an import above
# by the TESTER agent): the job side's projection (`_project_job` ->
# `resolve_canonical_names` -> `_resolve_one`) still runs a REAL
# vector-recall round trip for a brand-new Skill node (this fixture DETACH
# DELETEs the whole graph per test, so even an in-vocab name like "python"
# has no existing node on its first write) -- see `skills_graph.py`'s
# `_resolve_one` step 2, `[emb] = await embedder.embed([normalised])`. A
# bare, unconfigured `AsyncMock()` (as this file originally had) returns a
# `MagicMock()` whose default `__iter__` yields nothing, so that unpack
# raises `ValueError` and the job.parsed row dead-letters -- entirely
# unrelated to anything this file's classifier scenario is actually testing
# (which never touches the job/LLM/embedder path at all; see the module
# docstring). `_make_llm`/`_make_embedder`/`_vec` are imported from the
# sibling `test_graph_projection_e2e.py` above rather than duplicated here.
def _ctx(pg_pool: asyncpg.Pool, neo4j_driver: AsyncDriver) -> dict[str, Any]:
    return {
        "pg_pool": pg_pool,
        "neo4j": neo4j_driver,
        "llm": _make_llm(),
        "embedder": _make_embedder(),
    }


async def _insert_job(pool: asyncpg.Pool) -> Any:
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "INSERT INTO jobs (title, description_raw) VALUES ($1, $2) RETURNING id",
            "Senior Backend Engineer",
            "Build and operate REST APIs and data pipelines on Python.",
        )


async def _insert_resume(pool: asyncpg.Pool, job_id: Any) -> Any:
    import uuid

    async with pool.acquire() as conn:
        return await conn.fetchval(
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


async def _enqueue_job_requiring_python(pool: asyncpg.Pool, job_id: Any) -> None:
    payload = {
        "embedding": _vec(1),
        "extracted": {
            "title": "Senior Backend Engineer",
            "required_skills": [{"name": "python", "min_years": 2}],
            "nice_to_have_skills": [],
            "min_years_experience": 2,
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


async def _enqueue_resume_with_one_skill(
    pool: asyncpg.Pool,
    *,
    resume_id: Any,
    job_id: Any,
    skill_name: str,
    categories: list[str] | None,
) -> None:
    skill: dict[str, Any] = {"name": skill_name, "years": 4, "evidence_chunk_ids": []}
    if categories is not None:
        skill["categories"] = categories
    payload = {
        "parsed": {
            "total_years_experience": 4,
            "skills": [skill],
            "experience": [],
            "education": [],
            "chunks": [],
            "cover_letter_chunks": [],
        },
        "summary_emb": _vec(2),
        "chunk_embs": {},
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


# ── the actual feature, end to end ────────────────────────────────────────


@pytest.mark.asyncio
async def test_hashed_skill_node_gets_classifier_categories_from_the_payload(
    pg_pool: asyncpg.Pool, neo4j_driver: AsyncDriver
) -> None:
    """UPDATED (slice 2, 2026-08-19 decision memo): the classifier's
    assignment now lands on the SEPARATE ``classified_categories`` property,
    NEVER on ``categories`` (curated, ``ensure_categories``-only, forever) --
    a curated family and an inferred one must remain distinguishable in the
    graph. CHANGED from slice 1, which asserted ``s.categories ==
    ["backend"]`` for the hashed node; that write target moved."""
    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume(pg_pool, job_id)
    await _enqueue_resume_with_one_skill(
        pg_pool,
        resume_id=resume_id,
        job_id=job_id,
        skill_name="bespoke internal erp customisation",
        categories=["backend"],
    )

    delivered = await project_to_graph(_ctx(pg_pool, neo4j_driver), batch=10)
    assert delivered == 1

    async with neo4j_driver.session() as session:
        result = await session.run(
            "MATCH (:Resume)-[:HAS_SKILL]->(s:Skill) "
            "WHERE s.canonical_key STARTS WITH 'h:' "
            "RETURN s.canonical_key AS key, s.categories AS cats, "
            "s.classified_categories AS classified"
        )
        record = await result.single()
    assert record is not None
    assert record["key"].startswith("h:")
    assert record["classified"] == ["backend"], (
        "the parse-time classifier's payload categories did not reach the "
        "hashed Skill node -- projection must write "
        "s.classified_categories from the payload for a hashed skill"
    )
    assert record["cats"] is None, (
        "a classifier-assigned family must NEVER be written to the curated "
        "s.categories property -- curated and inferred provenance must stay "
        "distinguishable in the graph forever"
    )


@pytest.mark.asyncio
async def test_hashed_skill_family_credit_when_flag_on(
    pg_pool: asyncpg.Pool, neo4j_driver: AsyncDriver
) -> None:
    """UPDATED (slice 2): slice 1's whole point -- a JD requiring an
    in-vocab skill (``python``, curated family ``backend`` per
    ``categories.yaml``) and a résumé whose ONLY skill is an out-of-vocab
    phrase the parser hashed, with a parse-time-assigned ``backend`` family,
    earns stage-2 family credit instead of scoring 0.0 with
    ``reason="missing"`` -- is now GATED behind ``use_classified_families``.
    CHANGED from slice 1: that flag is passed explicitly True here (slice 1
    called ``_stage2_skill_rows`` with no flag and expected credit by
    default; slice 2 forbids that default). This "on" path must still be
    tested even though it ships disabled."""
    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume(pg_pool, job_id)
    await _enqueue_job_requiring_python(pg_pool, job_id)
    await _enqueue_resume_with_one_skill(
        pg_pool,
        resume_id=resume_id,
        job_id=job_id,
        skill_name="bespoke internal erp customisation",
        categories=["backend"],
    )

    delivered = await project_to_graph(_ctx(pg_pool, neo4j_driver), batch=10)
    assert delivered == 2

    rows = await _stage2_skill_rows(
        neo4j_driver, job_id, resume_id, use_classified_families=True
    )
    assert len(rows) == 1
    overall, contributions = score_skill_breakdown(rows, weights=DEFAULT_WEIGHTS)

    assert contributions[0].reason != "missing", (
        "with use_classified_families=True, a résumé skill sharing python's "
        "curated family via its classified family must not score as a "
        "genuine miss"
    )
    assert contributions[0].score > 0.0
    assert overall > 0.0


@pytest.mark.asyncio
async def test_hashed_skill_classified_categories_earn_nothing_with_flag_off_default(
    pg_pool: asyncpg.Pool, neo4j_driver: AsyncDriver
) -> None:
    """NEW (slice 2): the mirror image of the "on" test above, and the
    minimal direct proof of the slice's safety claim -- the EXACT SAME graph
    (classifier data genuinely present on the Skill node), scored with NO
    flag argument (``_stage2_skill_rows``'s own default), must still score
    0.0 / ``reason="missing"``. The classifier's record must never move
    ranking unless a caller opts in explicitly."""
    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume(pg_pool, job_id)
    await _enqueue_job_requiring_python(pg_pool, job_id)
    await _enqueue_resume_with_one_skill(
        pg_pool,
        resume_id=resume_id,
        job_id=job_id,
        skill_name="bespoke internal erp customisation",
        categories=["backend"],
    )

    delivered = await project_to_graph(_ctx(pg_pool, neo4j_driver), batch=10)
    assert delivered == 2

    rows = await _stage2_skill_rows(neo4j_driver, job_id, resume_id)
    assert len(rows) == 1
    overall, contributions = score_skill_breakdown(rows, weights=DEFAULT_WEIGHTS)

    assert contributions[0].reason == "missing", (
        "classified_categories must be ignored entirely when "
        "use_classified_families is not explicitly True, even though this "
        "graph genuinely carries a classifier-assigned family"
    )
    assert contributions[0].score == 0.0
    assert overall == 0.0


@pytest.mark.asyncio
async def test_ranking_neutral_stage2_rows_identical_with_and_without_classifier_data(
    pg_pool: asyncpg.Pool, neo4j_driver: AsyncDriver
) -> None:
    """THE load-bearing safety proof of this slice: with the flag off (the
    default), a graph containing classifier-assigned ``classified_categories``
    must produce stage-2 rows AND ``score_skill_breakdown`` results IDENTICAL
    to the same scenario with no classifier data at all -- a real comparison
    of both scenarios, not an assertion that one number looks right.

    Two independent job/résumé pairs (Skill nodes are global/shared --
    MERGEd on canonical_key -- so the two scenarios use DIFFERENT
    out-of-vocab phrases; reusing one phrase would collide on a single
    shared node and let one scenario's write leak into the other's read)."""
    job_with = await _insert_job(pg_pool)
    resume_with = await _insert_resume(pg_pool, job_with)
    await _enqueue_job_requiring_python(pg_pool, job_with)
    await _enqueue_resume_with_one_skill(
        pg_pool,
        resume_id=resume_with,
        job_id=job_with,
        skill_name="bespoke internal erp customisation",
        categories=["backend"],
    )

    job_without = await _insert_job(pg_pool)
    resume_without = await _insert_resume(pg_pool, job_without)
    await _enqueue_job_requiring_python(pg_pool, job_without)
    await _enqueue_resume_with_one_skill(
        pg_pool,
        resume_id=resume_without,
        job_id=job_without,
        skill_name="artisanal cheese affinage consulting",
        categories=None,  # no classifier data at all -- the "before" graph
    )

    delivered = await project_to_graph(_ctx(pg_pool, neo4j_driver), batch=10)
    assert delivered == 4

    rows_with = await _stage2_skill_rows(neo4j_driver, job_with, resume_with)
    rows_without = await _stage2_skill_rows(neo4j_driver, job_without, resume_without)
    assert rows_with == rows_without, (
        "stage-2 rows differ between a graph WITH classifier-assigned "
        "classified_categories and one WITHOUT any, both scored with the "
        "flag off (default) -- the classifier's record must not move "
        "ranking until match_use_classified_families is explicitly on"
    )

    overall_with, contribs_with = score_skill_breakdown(
        rows_with, weights=DEFAULT_WEIGHTS
    )
    overall_without, contribs_without = score_skill_breakdown(
        rows_without, weights=DEFAULT_WEIGHTS
    )
    assert overall_with == overall_without
    assert contribs_with == contribs_without


@pytest.mark.asyncio
@pytest.mark.parametrize("non_matchable_family", ["other", "domain"])
async def test_hashed_skill_non_matchable_classified_family_earns_nothing_with_flag_on(
    pg_pool: asyncpg.Pool, neo4j_driver: AsyncDriver, non_matchable_family: str
) -> None:
    """The non-matchable exclusion (``match_non_matchable_families``,
    default ``other,domain``) still applies on the "on" path: a classifier
    assignment of a non-matchable, catch-all family must earn ZERO family
    credit even with ``use_classified_families=True`` -- ``other``/``domain``
    are junk/ambiguous buckets, never a relatedness signal, on either the
    curated or the classified side."""
    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume(pg_pool, job_id)
    await _enqueue_job_requiring_python(pg_pool, job_id)
    await _enqueue_resume_with_one_skill(
        pg_pool,
        resume_id=resume_id,
        job_id=job_id,
        skill_name="bespoke internal erp customisation",
        categories=[non_matchable_family],
    )

    delivered = await project_to_graph(_ctx(pg_pool, neo4j_driver), batch=10)
    assert delivered == 2

    rows = await _stage2_skill_rows(
        neo4j_driver, job_id, resume_id, use_classified_families=True
    )
    assert len(rows) == 1
    overall, contributions = score_skill_breakdown(rows, weights=DEFAULT_WEIGHTS)

    assert contributions[0].reason == "missing", (
        f"a classified family of {non_matchable_family!r} must earn no "
        "family credit even with use_classified_families=True -- it is a "
        "non-matchable catch-all, not a relatedness signal"
    )
    assert contributions[0].score == 0.0
    assert overall == 0.0


@pytest.mark.asyncio
async def test_baseline_without_categories_still_scores_missing_today(
    pg_pool: asyncpg.Pool, neo4j_driver: AsyncDriver
) -> None:
    """The "before" comparison point the spec asks for: the SAME scenario,
    minus the classifier's category assignment, still scores exactly as it
    did before this feature existed -- 0.0, ``reason="missing"``. Expected
    to hold both BEFORE and AFTER this slice lands (the strictly-additive
    property ADR-044's design section claims by construction)."""
    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume(pg_pool, job_id)
    await _enqueue_job_requiring_python(pg_pool, job_id)
    await _enqueue_resume_with_one_skill(
        pg_pool,
        resume_id=resume_id,
        job_id=job_id,
        skill_name="bespoke internal erp customisation",
        categories=None,  # no classifier answer -- today's baseline
    )

    delivered = await project_to_graph(_ctx(pg_pool, neo4j_driver), batch=10)
    assert delivered == 2

    rows = await _stage2_skill_rows(neo4j_driver, job_id, resume_id)
    assert len(rows) == 1
    overall, contributions = score_skill_breakdown(rows, weights=DEFAULT_WEIGHTS)

    assert contributions[0].reason == "missing"
    assert contributions[0].score == 0.0
    assert overall == 0.0


@pytest.mark.asyncio
async def test_curated_in_vocab_skill_categories_are_not_overridden_by_a_payload_value(
    pg_pool: asyncpg.Pool, neo4j_driver: AsyncDriver
) -> None:
    """The résumé side's OWN copy of an in-vocab skill (e.g. it also lists
    "python" directly) must keep its curated ``categories.yaml`` families
    even if some future/buggy producer attached a stray ``categories`` value
    to that skill's outbox row -- curated wins, per ``ensure_categories``,
    never a classifier/payload value, for an in-vocab canonical.

    M9 mutation-review addition (2026-08-19): this is exactly the payload
    shape ``_redact_skill_names_pii`` can genuinely produce (pinned at the
    unit level by
    ``test_classifier_category_survives_redaction_into_an_in_vocab_name`` in
    ``test_worker_parse_resume.py``) when an out-of-vocab compound name
    carrying a classifier-assigned family gets rewritten down to a bare
    in-vocab remainder AFTER classification but BEFORE the outbox is
    enqueued -- not a purely hypothetical "buggy producer". The projection
    write guard (``resume_tasks.py``, ``if
    canonical.startswith(skills_graph._HASH_KEY_PREFIX)``) is what stops a
    stray ``categories`` value from landing on ``Skill.classified_categories``
    for a curated node; deleting that guard changes no OTHER assertion in
    this file (the original version of this test asserted only on
    ``s.categories``, never on ``s.classified_categories`` -- a survived
    mutant), so the new assertion below is the direct pin."""
    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume(pg_pool, job_id)
    await _enqueue_resume_with_one_skill(
        pg_pool,
        resume_id=resume_id,
        job_id=job_id,
        skill_name="python",
        categories=["not_a_real_family_a_buggy_producer_sent"],
    )

    delivered = await project_to_graph(_ctx(pg_pool, neo4j_driver), batch=10)
    assert delivered == 1

    async with neo4j_driver.session() as session:
        result = await session.run(
            "MATCH (s:Skill {canonical_key: 'python'}) "
            "RETURN s.categories AS cats, s.classified_categories AS classified"
        )
        record = await result.single()
    assert record is not None
    assert "not_a_real_family_a_buggy_producer_sent" not in (record["cats"] or [])
    assert record["cats"], "python's curated categories must still be written"
    assert record["classified"] is None, (
        "a payload categories value attached to a CURATED (in-vocab) skill "
        "must never reach s.classified_categories either -- that property "
        "is reserved for a genuinely hashed/out-of-vocab canonical, and "
        "this in-vocab node must come back with it entirely absent"
    )
