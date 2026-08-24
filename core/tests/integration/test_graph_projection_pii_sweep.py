"""THE GRAPH-WIDE PII SWEEP — the merge-blocking artifact for Phase 4b.

Seeds a ``resumes`` row whose ``parsed`` jsonb holds a header chunk carrying
a synthetic email/phone/name (from the reviewed corpus allowlist —
``casey.rivera@example.test`` / ``555-0101`` / ``Casey Rivera``, the same
literals ``core/tests/evals/fixtures/resumes/r01_casey_rivera.json`` uses),
enqueues the REAL identity-free outbox payload (built via
``ResumeParsed.model_dump(exclude=_OUTBOX_PARSED_EXCLUDE)`` — the SAME named
constant ``test_evals_outbox_fixture.py`` pins, not a hand-rolled re-
implementation of the exclude clause), drains it through the real
``project_to_graph``, and then walks EVERY NODE AND EVERY RELATIONSHIP
PROPERTY IN THE WHOLE DATABASE for the three markers.

A per-field check ("does the Resume node's ``total_years_experience``
contain the email?") is not good enough — R1's whole point is that
``text_preview``/chunk text/whatever future field could carry it, so the
guard has to be structurally blind to WHICH property, walking the graph the
same way a real PIPEDA/FIPPA audit would.

F1 (security re-audit, HIGH) — the sweep only hunts the identity triple in
whatever the fixture happens to carry, and previously the fixture's
``experience[].bullets[].text``/``experience[].company``/
``education[].institution`` were marker-free strings ("Shipped REST APIs.",
"Nimbus Analytics Inc") — an N1 field (ADR-007 permits these on the outbox
ONLY because the projection never writes them to the graph) that would sail
through the sweep even if a future regression started projecting them,
simply because the fixture text carried no PII substring to trip on. Every
one of those fields (PLUS ``skills[].name`` — see F3) now carries a marker.
The email/phone/name assertions are also now CASE-INSENSITIVE:
``_basic_normalise`` (``src/pipeline/skills.py``) unconditionally lowercases
every skill/canonical name, so a real leak via ``Skill.canonical_name`` lands
as ``'casey rivera'`` — the ORIGINAL case-sensitive ``CANDIDATE_NAME not in
all_values`` check would never see it.

Real Postgres + real Neo4j via testcontainers. LLM/embedder are mocked (no
Ollama in gates — see CLAUDE.md); the embedder returns deterministic 768-d
vectors so the real ``vector.dimensions`` Neo4j contract is respected.

F3c (security re-audit round 2) — the sweep previously built ``skills[]`` as
a raw, already-canonicalised-shaped dict, bypassing
``src.worker.resume_tasks._extract_skills_merged`` entirely; that is exactly
why round 1's F3 fix (a shape reject inside ``skills_graph._resolve_one``)
stayed green after it was defeated on the real production path (F3b) — the
fixture never exercised the function the defeat lives in.
``_skills_via_real_extraction`` below routes every skill name through the
REAL ``_extract_skills_merged``, and the two candidate-block states
(``candidate_empty`` parametrization) reproduce F3a's exact defeat condition
alongside it.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest
from neo4j import AsyncDriver, AsyncGraphDatabase
from testcontainers.neo4j import Neo4jContainer
from testcontainers.postgres import PostgresContainer

from src.models.ddl import init_schema
from src.schemas.resumes import (
    ResumeChunk,
    ResumeParsed,
    ResumeSkillDetail,
    ResumeSkillDetails,
)
from src.services import outbox_service
from src.settings import get_settings
from src.worker.graph_tasks import project_to_graph
from src.worker.neo4j_bootstrap import bootstrap_neo4j_schema
from src.worker.resume_tasks import _OUTBOX_PARSED_EXCLUDE, _extract_skills_merged

CANDIDATE_NAME = "Casey Rivera"
CANDIDATE_EMAIL = "casey.rivera@example.test"
CANDIDATE_PHONE = "555-0101"
HEADER_CHUNK_TEXT = (
    f"{CANDIDATE_NAME}\n{CANDIDATE_EMAIL} | {CANDIDATE_PHONE}\nVancouver, BC"
)

# F3c (security re-audit round 2) — security's exact round-2 reproduction
# table: every one of these RAW LLM-hallucinated "skill" names must be
# rejected by `_extract_skills_merged` (F3b's fix) BEFORE it is ever
# canonicalised, embedded, or written to Neo4j — with NO candidate context
# involved (this whole list is run through the real production function
# below, not hand-authored as a pre-canonicalised dict — see
# `_skills_via_real_extraction`, which is F3c's actual fix: the OLD fixture
# built `skills[]` as a raw dict, bypassing `_extract_skills_merged`
# entirely, which is exactly why the sweep stayed green after round 1's fix
# was defeated).
MALICIOUS_RAW_SKILL_NAMES = [
    CANDIDATE_NAME,  # the ORIGINAL F3 finding, verbatim
    CANDIDATE_EMAIL,  # F3b: email net, dead post-canonicalisation
    "Rivera, Casey",  # comma-reordered
    "Casey M. Rivera",  # middle initial
    "Casey-Rivera",  # hyphenated
    "Rivera",  # bare surname
    "John Smith",  # a referee, not even the candidate
    # ── round-3 security re-audit widening (S1-S6) ──────────────────────
    "RIVERA, CASEY",  # S1: all-caps, comma-reordered
    "CASEY RIVERA",  # S1: all-caps
    "casey rivera",  # S1: all-lowercase
    "Sean McDonald",  # S3: Mc-internal-caps surname, not even the candidate
    "John O'Brien",  # S3: apostrophe-joined surname
    "Maria del Carmen Rivera Lopez",  # S4: 5-token, connector particle
    "Ana van der Berg",  # S4: 4-token, two connector particles
    "Casey Rivera 2",  # S2: stray trailing standalone digit token
    "Casey Rivera+",  # S2: stray trailing glued '+'
    "Casey Rivera#",  # S2: stray trailing glued '#'
    "Casey.Rivera",  # S2: dot-joined (not a technical '.')
    "Кейси Ривера",  # S5: Cyrillic
    "李伟",  # S5: CJK, caseless script
    "casey.rivera (at) example.test",  # S6: '(at)' obfuscation + whitespace
]

_DIM = get_settings().llm_embedding_dim


def _vec(seed: int) -> list[float]:
    return [((i * 13 + seed * 7) % 971) / 990.0 + 0.01 for i in range(_DIM)]


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
    pool = await asyncpg.create_pool(dsn=pg_dsn, min_size=1, max_size=4)
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
    out = MagicMock(match=None)
    return MagicMock(chat_json=AsyncMock(return_value=out))


def _make_embedder() -> MagicMock:
    async def _embed(texts: Any) -> list[list[float]]:
        return [_vec(len(t)) for t in texts]

    return MagicMock(embed=AsyncMock(side_effect=_embed))


async def _insert_job(pool: asyncpg.Pool) -> uuid.UUID:
    async with pool.acquire() as conn:
        job_id: uuid.UUID = await conn.fetchval(
            "INSERT INTO jobs (title, description_raw) VALUES ($1, $2) RETURNING id",
            "Senior Backend Engineer",
            "Build and operate REST APIs and data pipelines.",
        )
    return job_id


async def _skills_via_real_extraction() -> list[dict[str, Any]]:
    """F3c fix: builds ``skills[]`` through the REAL
    ``src.worker.resume_tasks._extract_skills_merged`` — never a hand-rolled
    dict. The OLD fixture built ``skills[]`` directly as
    ``[{"name": "python", ...}, {"name": CANDIDATE_EMAIL}]``, which handed
    ``skills_graph._resolve_one`` the email name RAW (with its "@" intact) —
    a shape production never emits, because the real pipeline always
    canonicalises a skill name (via ``canonicalize_skill_names``, which
    strips "@") before it ever reaches that module. Routing every name in
    ``MALICIOUS_RAW_SKILL_NAMES`` through the real ``_extract_skills_merged``
    exercises F3b's actual fix (the raw-name shape+vocab reject that runs
    BEFORE canonicalisation) and proves none of them survive into the
    persisted/outbox skill list — with "python" included so the sweep also
    pins that a LEGITIMATE skill is not collaterally dropped (Decision B).
    """
    chunks = [ResumeChunk(id="c_001", section="header", page=1, text=HEADER_CHUNK_TEXT)]
    llm = MagicMock(
        chat_json=AsyncMock(
            return_value=ResumeSkillDetails(
                skills=[
                    ResumeSkillDetail(name="python"),
                    *[ResumeSkillDetail(name=n) for n in MALICIOUS_RAW_SKILL_NAMES],
                ]
            )
        )
    )
    merged, _reason = await _extract_skills_merged(llm, chunks, "sweep-resume")
    return [s.model_dump() for s in merged]


async def _insert_resume_with_pii_header_chunk(
    pool: asyncpg.Pool, job_id: uuid.UUID, *, candidate_empty: bool = False
) -> tuple[uuid.UUID, ResumeParsed]:
    """``candidate_empty=True`` reproduces F3a's exact defeat condition: layer
    2 (``_redact_skill_names_pii``, candidate-context-aware) has NOTHING to
    redact against when the parsed ``candidate`` block is empty — the F3b
    shape+vocab reject (layer 0, upstream of any candidate context) must
    catch every row of ``MALICIOUS_RAW_SKILL_NAMES`` regardless."""
    candidate_block = (
        {}
        if candidate_empty
        else {
            "name": CANDIDATE_NAME,
            "email": CANDIDATE_EMAIL,
            "phone": CANDIDATE_PHONE,
            "location": "Vancouver, BC",
        }
    )
    skills = await _skills_via_real_extraction()
    parsed = ResumeParsed.model_validate(
        {
            "candidate": candidate_block,
            "summary": (
                f"Backend engineer {CANDIDATE_NAME} with 6 years of "
                "Python experience."
            ),
            "total_years_experience": 6,
            "skills": skills,
            "experience": [
                {
                    # F1: an N1 field — permitted on the outbox ONLY because
                    # this module never writes it to the graph (R8). Carries
                    # a marker so a future regression that starts writing it
                    # gets caught here instead of sailing through on
                    # marker-free fixture text.
                    "company": f"{CANDIDATE_NAME} Consulting",
                    "title": "Senior Backend Engineer",
                    "start": "2022-01",
                    "is_current": True,
                    "bullets": [
                        {"text": "Shipped REST APIs.", "chunk_id": "c_001"},
                        {
                            "text": (
                                f"Reach {CANDIDATE_NAME} at {CANDIDATE_EMAIL} "
                                f"or {CANDIDATE_PHONE}."
                            ),
                            "chunk_id": "c_001",
                        },
                    ],
                }
            ],
            "education": [
                {
                    "degree": "BSc Computer Science",
                    # F1: same N1-field rationale as `company` above.
                    "institution": f"University of {CANDIDATE_NAME}",
                    "year": 2016,
                }
            ],
            "chunks": [
                {
                    "id": "c_001",
                    "section": "header",
                    "page": 1,
                    # The canonical R1 leak shape: a résumé header chunk
                    # carrying the candidate's contact block verbatim.
                    "text": HEADER_CHUNK_TEXT,
                }
            ],
            "cover_letter_chunks": [],
        }
    )
    async with pool.acquire() as conn:
        resume_id: uuid.UUID = await conn.fetchval(
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
            json.dumps(parsed.model_dump()),
        )
    return resume_id, parsed


async def _enqueue_real_outbox_payload(
    pool: asyncpg.Pool, *, resume_id: uuid.UUID, job_id: uuid.UUID, parsed: ResumeParsed
) -> None:
    """The REAL identity-free payload — built the same way
    ``src.worker.resume_tasks.parse_resume`` builds it, via the shared
    ``_OUTBOX_PARSED_EXCLUDE`` constant (not a hand-rolled re-implementation
    of the exclude clause)."""
    outbox_payload = {
        "parsed": parsed.model_dump(exclude=_OUTBOX_PARSED_EXCLUDE),
        "summary_emb": _vec(1),
        "chunk_embs": {c.id: _vec(hash(c.id) % 97) for c in parsed.chunks},
        "prompt_version": "resume_core_v1+resume_skills_v2",
        "job_id": str(job_id),
    }
    async with pool.acquire() as conn:
        await outbox_service.enqueue_outbox(
            conn,
            aggregate="resume",
            aggregate_id=resume_id,
            event_type="resume.parsed",
            payload=outbox_payload,
        )


async def _walk_every_property_value(driver: AsyncDriver) -> list[str]:
    """Every node property AND every relationship property, in the WHOLE
    database, stringified. Structurally blind to which label/property
    carries a leak — that's the point."""
    values: list[str] = []
    async with driver.session() as session:
        node_result = await session.run("MATCH (n) RETURN properties(n) AS props")
        async for record in node_result:
            values.extend(str(v) for v in record["props"].values())
        rel_result = await session.run("MATCH ()-[r]->() RETURN properties(r) AS props")
        async for record in rel_result:
            values.extend(str(v) for v in record["props"].values())
    return values


@pytest.mark.asyncio
@pytest.mark.parametrize("candidate_empty", [False, True], ids=["perfect", "empty"])
async def test_no_pii_marker_survives_anywhere_in_the_projected_graph(
    pg_pool: asyncpg.Pool, neo4j_driver: AsyncDriver, candidate_empty: bool
) -> None:
    """F3c: parametrized over BOTH candidate-block states security's
    reproduction covers. ``candidate_empty=True`` is F3a's exact defeat
    condition — layer 2 (candidate-context-aware) has nothing to redact
    against, so this direction proves the F3b shape+vocab reject (layer 0)
    alone is sufficient, with NO help from candidate context."""
    job_id = await _insert_job(pg_pool)
    resume_id, parsed = await _insert_resume_with_pii_header_chunk(
        pg_pool, job_id, candidate_empty=candidate_empty
    )
    await _enqueue_real_outbox_payload(
        pg_pool, resume_id=resume_id, job_id=job_id, parsed=parsed
    )

    ctx = {
        "pg_pool": pg_pool,
        "neo4j": neo4j_driver,
        "llm": _make_llm(),
        "embedder": _make_embedder(),
    }
    delivered = await project_to_graph(ctx, batch=10)
    assert delivered == 1

    # F1: case-insensitive. `_basic_normalise` (src/pipeline/skills.py)
    # unconditionally lowercases every skill/canonical name, so a real leak
    # via `Skill.canonical_name` lands as 'casey rivera' — a case-SENSITIVE
    # check here would never see it (this is exactly what let F3 through).
    all_values_lower = " \n ".join(
        await _walk_every_property_value(neo4j_driver)
    ).lower()

    assert CANDIDATE_EMAIL.lower() not in all_values_lower, (
        "candidate email survived somewhere in the projected graph (R1 — the "
        "PII sweep is structurally blind to which node/property carried it)"
    )
    assert (
        CANDIDATE_PHONE.lower() not in all_values_lower
    ), "candidate phone survived somewhere in the projected graph (R1)"
    assert (
        CANDIDATE_NAME.lower() not in all_values_lower
    ), "candidate name survived somewhere in the projected graph (R1)"
    # Belt-and-braces: the header chunk's RAW text must not survive either,
    # even a truncated preview of it (decision 1 — no chunk text, ever).
    assert HEADER_CHUNK_TEXT.lower() not in all_values_lower

    # F3c: TOKEN-level checks, not just the exact "casey rivera" phrase —
    # every comma-reordered / hyphenated / middle-initialled / bare-surname
    # variant in `MALICIOUS_RAW_SKILL_NAMES` still carries these two bare
    # tokens, so this catches a regression in ANY of those shapes, not only
    # the original verbatim-phrase finding.
    assert "casey" not in all_values_lower
    assert "rivera" not in all_values_lower
    assert "smith" not in all_values_lower

    # Round-3 widening (S1-S6): token-level checks for every NEW shape added
    # to `MALICIOUS_RAW_SKILL_NAMES` above.
    assert "mcdonald" not in all_values_lower
    assert "obrien" not in all_values_lower  # apostrophe stripped by _basic_normalise
    assert "maria" not in all_values_lower
    assert "carmen" not in all_values_lower
    assert "lopez" not in all_values_lower
    assert "berg" not in all_values_lower
    assert "кейси" not in all_values_lower
    assert "ривера" not in all_values_lower
    assert "李伟" not in all_values_lower

    # Decision B recall pin, right alongside the PII assertions: the filter
    # closing F3 must not collaterally eat a LEGITIMATE skill — "python" must
    # have made it all the way to a real Skill node.
    assert "python" in all_values_lower


@pytest.mark.asyncio
async def test_the_resume_node_and_chunk_carry_no_text_preview_property(
    pg_pool: asyncpg.Pool, neo4j_driver: AsyncDriver
) -> None:
    """Belt-and-braces alongside the sweep above: pins decision 1's exact
    shape (no ``text_preview`` KEY at all) so a future re-introduction of a
    "harmless-looking preview" is caught even before it could carry PII."""
    job_id = await _insert_job(pg_pool)
    resume_id, parsed = await _insert_resume_with_pii_header_chunk(pg_pool, job_id)
    await _enqueue_real_outbox_payload(
        pg_pool, resume_id=resume_id, job_id=job_id, parsed=parsed
    )
    ctx = {
        "pg_pool": pg_pool,
        "neo4j": neo4j_driver,
        "llm": _make_llm(),
        "embedder": _make_embedder(),
    }
    await project_to_graph(ctx, batch=10)

    async with neo4j_driver.session() as session:
        result = await session.run("MATCH (c:ResumeChunk) RETURN keys(c) AS ks")
        rows = [record["ks"] async for record in result]
    assert rows, "expected at least one ResumeChunk node"
    for keys in rows:
        assert "text_preview" not in keys
        assert "preview" not in keys
        assert "text" not in keys
