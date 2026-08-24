"""Unit tests for the Neo4j schema bootstrap (``src/worker/neo4j_bootstrap.py``).

Two regressions are pinned here, both ported from hris's source comments:

1. **The 768-d contract.** The ``vector.dimensions`` literal in every vector
   index is parsed out of the Cypher and compared to
   ``get_settings().llm_embedding_dim``. Neither side is hardcoded twice — if
   they ever drift apart, this fails.
2. **The ResumeChunk collision.** Chunk ids (``c_001``, ``c_002``, …) are
   deterministic per-resume and intentionally COLLIDE across resumes. A
   uniqueness constraint on ``ResumeChunk.id`` makes the second resume's
   projection fail, which silently caps every shortlist at ONE candidate.
   ``chunk_id_unique`` must therefore be DROPPED, never created. Do not "fix"
   this back.

Contract with the implementation: ``bootstrap_neo4j_schema`` opens one
``driver.session()`` and awaits ``session.run(stmt)`` per statement, in order.
"""

from __future__ import annotations

import inspect
import re
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.settings import get_settings
from src.worker.neo4j_bootstrap import _STATEMENTS, bootstrap_neo4j_schema

VECTOR_INDEXES: tuple[str, ...] = (
    "resume_summary_idx",
    "job_summary_idx",
    "skill_emb_idx",
    "chunk_emb_idx",
)

# Statement order is load-bearing: each DROP must precede its replacement.
#
# F2 (security re-audit round 2): "skill_name_unique" now appears ONLY as a
# DROP (its CREATE counterpart is "skill_canonical_key_unique", a genuinely
# new name) — see ``_statement_for``'s uniqueness-of-match assertion below,
# which is exactly what would catch a regression back to reusing the old
# name for the CREATE.
EXPECTED_ORDER: tuple[str, ...] = (
    "job_id_unique",
    "resume_id_unique",
    "skill_name_unique",  # DROP (F2) — the stale, wrong-property constraint
    "skill_canonical_key_unique",
    "company_name_unique",
    "institution_name_unique",
    "chunk_id_unique",  # DROP
    "chunk_resume_id_idx",
    "resume_job_id_idx",
    "resume_summary_idx",
    "job_summary_idx",
    "skill_emb_idx",
    "chunk_emb_idx",
)

DIM_RE = re.compile(r"`vector\.dimensions`\s*:\s*(\d+)")
SIMILARITY_RE = re.compile(r"`vector\.similarity_function`\s*:\s*'(\w+)'")


# ── Helpers ────────────────────────────────────────────────────────────────


def _squash(cypher: str) -> str:
    return " ".join(cypher.split())


def _statement_for(name: str) -> str:
    matches = [_squash(s) for s in _STATEMENTS if re.search(rf"\b{name}\b", s)]
    assert len(matches) == 1, f"expected exactly one statement naming {name}"
    return matches[0]


def _fake_driver() -> tuple[Any, Any]:
    """AsyncDriver double supporting ``async with driver.session() as s``."""
    session = MagicMock()
    session.run = AsyncMock()
    driver = MagicMock()
    driver.session.return_value.__aenter__.return_value = session
    driver.session.return_value.__aexit__.return_value = None
    return driver, session


def _ran(session: Any) -> list[str]:
    return [_squash(call.args[0]) for call in session.run.await_args_list]


# ── The 12 statements, in order ────────────────────────────────────────────


def test_exactly_thirteen_statements() -> None:
    assert isinstance(_STATEMENTS, tuple)
    assert len(_STATEMENTS) == 13


def test_statements_are_in_the_expected_order() -> None:
    for stmt, name in zip(_STATEMENTS, EXPECTED_ORDER, strict=True):
        assert re.search(rf"\b{name}\b", stmt), f"expected {name} in: {stmt}"


def test_drop_precedes_the_replacement_chunk_index() -> None:
    names = [
        next(n for n in EXPECTED_ORDER if re.search(rf"\b{n}\b", s))
        for s in _STATEMENTS
    ]
    assert names.index("chunk_id_unique") < names.index("chunk_resume_id_idx")


@pytest.mark.parametrize("stmt", _STATEMENTS, ids=range(len(_STATEMENTS)))
def test_every_statement_is_idempotent(stmt: str) -> None:
    sql = _squash(stmt)
    if sql.upper().startswith("DROP"):
        assert re.search(r"IF\s+EXISTS", sql, re.IGNORECASE), sql
    else:
        assert re.search(r"IF\s+NOT\s+EXISTS", sql, re.IGNORECASE), sql


# ── The ResumeChunk collision regression ───────────────────────────────────


def test_no_constraint_is_created_on_resumechunk() -> None:
    """A uniqueness constraint here caps every shortlist at one candidate."""
    offenders = [
        s
        for s in _STATEMENTS
        if s.upper().lstrip().startswith("CREATE CONSTRAINT") and "ResumeChunk" in s
    ]
    assert offenders == []


def test_chunk_id_unique_is_dropped() -> None:
    stmt = _statement_for("chunk_id_unique")
    assert re.fullmatch(
        r"DROP\s+CONSTRAINT\s+chunk_id_unique\s+IF\s+EXISTS", stmt, re.IGNORECASE
    )


def test_chunk_id_unique_is_never_created() -> None:
    assert not re.search(
        r"CREATE\s+CONSTRAINT\s+chunk_id_unique", " ".join(_STATEMENTS), re.IGNORECASE
    )


# ── F2 (security re-audit round 2): skill_name_unique rename regression ────
#
# A same-named `CREATE CONSTRAINT skill_name_unique IF NOT EXISTS` with a
# CHANGED `REQUIRE` clause is a Neo4j no-op keyed on the constraint NAME —
# every install that ever ran the Phase-3 statement kept its OLD constraint
# (on the abandoned `canonical_name` property) and got NO uniqueness
# constraint on `canonical_key` at all. Fixed by DROPping the old name and
# creating a genuinely NEW one (`skill_canonical_key_unique`), mirroring the
# `chunk_id_unique` pattern already pinned above.


def test_skill_name_unique_is_dropped() -> None:
    stmt = _statement_for("skill_name_unique")
    assert re.fullmatch(
        r"DROP\s+CONSTRAINT\s+skill_name_unique\s+IF\s+EXISTS", stmt, re.IGNORECASE
    )


def test_skill_name_unique_is_never_created_as_a_constraint() -> None:
    assert not re.search(
        r"CREATE\s+CONSTRAINT\s+skill_name_unique\b",
        " ".join(_STATEMENTS),
        re.IGNORECASE,
    )


def test_skill_canonical_key_unique_is_a_new_name_not_a_rename_in_place() -> None:
    """The replacement constraint must be a DIFFERENT name from the dropped
    one — reusing `skill_name_unique` for the CREATE (even with the correct
    REQUIRE clause) would silently no-op on every install that already has
    the Phase-3 constraint under that name."""
    stmt = _statement_for("skill_canonical_key_unique")
    assert re.search(
        r"CREATE\s+CONSTRAINT\s+skill_canonical_key_unique\s+IF\s+NOT\s+EXISTS",
        stmt,
        re.IGNORECASE,
    )
    assert "skill_name_unique" not in stmt


def test_skill_canonical_key_unique_drop_precedes_the_create() -> None:
    names = [
        next(n for n in EXPECTED_ORDER if re.search(rf"\b{n}\b", s))
        for s in _STATEMENTS
    ]
    assert names.index("skill_name_unique") < names.index("skill_canonical_key_unique")


def test_chunk_lookup_uses_a_plain_composite_index() -> None:
    stmt = _statement_for("chunk_resume_id_idx")
    assert "CONSTRAINT" not in stmt.upper()
    assert re.search(
        r"FOR\s*\(\s*c:ResumeChunk\s*\)\s*ON\s*\(\s*c\.resume_id\s*,\s*c\.id\s*\)", stmt
    )


def test_resume_job_id_index_exists_for_per_job_scoping() -> None:
    """Without it, other jobs' candidates leak into a shortlist."""
    stmt = _statement_for("resume_job_id_idx")
    assert re.search(r"FOR\s*\(\s*r:Resume\s*\)\s*ON\s+r\.job_id", stmt)


# ── Uniqueness constraints ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("name", "label", "prop"),
    [
        ("job_id_unique", "Job", "id"),
        ("resume_id_unique", "Resume", "id"),
        ("skill_canonical_key_unique", "Skill", "canonical_key"),
        ("company_name_unique", "Company", "canonical_name"),
        ("institution_name_unique", "Institution", "canonical_name"),
    ],
)
def test_uniqueness_constraint(name: str, label: str, prop: str) -> None:
    stmt = _statement_for(name)
    assert re.search(rf"CREATE\s+CONSTRAINT\s+{name}\s+IF\s+NOT\s+EXISTS", stmt, re.I)
    assert re.search(rf"FOR\s*\(\s*\w+:{label}\s*\)", stmt)
    assert re.search(rf"REQUIRE\s+\w+\.{prop}\s+IS\s+UNIQUE", stmt, re.IGNORECASE)


def test_no_relationship_constraints_are_declared() -> None:
    """HAS_CHUNK/HAS_SKILL/REQUIRES/SHORTLISTED are MERGE-created, not DDL."""
    joined = " ".join(_STATEMENTS)
    assert "-[" not in joined
    for rel in ("HAS_CHUNK", "HAS_SKILL", "REQUIRES", "SHORTLISTED"):
        assert rel not in joined


# ── The 768-d contract ─────────────────────────────────────────────────────


def test_there_are_exactly_four_vector_indexes() -> None:
    vector_stmts = [s for s in _STATEMENTS if "VECTOR INDEX" in s.upper()]
    assert len(vector_stmts) == 4
    for name in VECTOR_INDEXES:
        assert any(re.search(rf"\b{name}\b", s) for s in vector_stmts)


@pytest.mark.parametrize("name", VECTOR_INDEXES)
def test_vector_index_dimension_matches_settings(name: str) -> None:
    """CONTRACT: the Cypher literal and llm_embedding_dim must never drift.

    Both sides are read at runtime — nothing is hardcoded twice.
    """
    dims = DIM_RE.findall(_statement_for(name))
    assert len(dims) == 1, f"{name} must declare `vector.dimensions` exactly once"
    assert int(dims[0]) == get_settings().llm_embedding_dim


@pytest.mark.parametrize("name", VECTOR_INDEXES)
def test_vector_index_similarity_is_cosine(name: str) -> None:
    assert SIMILARITY_RE.findall(_statement_for(name)) == ["cosine"]


@pytest.mark.parametrize(
    ("name", "target"),
    [
        ("resume_summary_idx", "(r:Resume) ON r.summary_embedding"),
        ("job_summary_idx", "(j:Job) ON j.summary_embedding"),
        ("skill_emb_idx", "(s:Skill) ON s.embedding"),
        ("chunk_emb_idx", "(c:ResumeChunk) ON c.embedding"),
    ],
)
def test_vector_index_targets_the_right_label_and_property(
    name: str, target: str
) -> None:
    assert target in _statement_for(name)


def test_all_vector_indexes_agree_on_one_dimension() -> None:
    dims = {int(d) for s in _STATEMENTS for d in DIM_RE.findall(s)}
    assert dims == {get_settings().llm_embedding_dim}


# ── bootstrap_neo4j_schema ─────────────────────────────────────────────────


def test_bootstrap_is_async() -> None:
    assert inspect.iscoroutinefunction(bootstrap_neo4j_schema)


@pytest.mark.asyncio
async def test_bootstrap_runs_every_statement_in_order() -> None:
    driver, session = _fake_driver()
    await bootstrap_neo4j_schema(driver)
    assert _ran(session) == [_squash(s) for s in _STATEMENTS]


@pytest.mark.asyncio
async def test_bootstrap_is_idempotent_when_run_twice() -> None:
    driver, session = _fake_driver()
    await bootstrap_neo4j_schema(driver)
    await bootstrap_neo4j_schema(driver)
    expected = [_squash(s) for s in _STATEMENTS]
    assert _ran(session) == expected + expected


@pytest.mark.asyncio
async def test_bootstrap_propagates_driver_errors() -> None:
    driver, session = _fake_driver()
    session.run = AsyncMock(side_effect=RuntimeError("neo4j unavailable"))
    with pytest.raises(RuntimeError, match="neo4j unavailable"):
        await bootstrap_neo4j_schema(driver)
