"""Unit tests -- ROADMAP A2, Phase 3.3 skill-family classifier, slice 2 (the
scoring-safety follow-up, this decision memo, 2026-08-19): the classifier's
output (``Skill.classified_categories``, written at projection) must NOT
move ranking until ``settings.match_use_classified_families`` is switched on.
Live measurement against the real tailnet ``gpt-oss:20b`` found the
classifier stably mis-families domain-expert phrases ("expert scientific
knowledge of MRI and MEG research methods" -> ``bi``) -- and family credit is
transitive across a WHOLE résumé, so granting credit on that misfire tells a
recruiter a candidate holds a qualification they don't. Record the fact,
defer the scoring change (ADR-040/ADR-041 precedent).

This file pins the WIRING half at the unit level (no real Neo4j): a new
keyword-only ``use_classified_families`` on ``_stage2_skill_rows``, a new
``MatchingContext.use_classified_families`` field defaulting ``False``, and
that ``_stage2_per_candidate`` forwards ``ctx.use_classified_families`` into
the Cypher-calling function's kwarg -- NEVER a hardcoded literal, and never
read ad hoc from ``get_settings()`` inside the per-candidate loop (the
existing ``family_weight``/``non_matchable_families`` convention this
mirrors, ``orchestrator.py:499-504``).

The actual SCORING behaviour of the flag (does a classified family really
earn/deny family credit against a real Cypher `EXISTS {...}` clause) can only
be proven against a real Neo4j -- see
``tests/integration/test_skill_family_classifier_projection_e2e.py``'s
flag-off/flag-on/non-matchable-exclusion/ranking-neutrality tests. Nothing in
THIS file exercises real Cypher.

Every test below is RED against the current source: neither
``MatchingContext.use_classified_families`` nor
``_stage2_skill_rows``'s/``_stage2_per_candidate``'s new keyword exist yet.
"""

from __future__ import annotations

import inspect
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from src.pipeline.matching.orchestrator import (
    JobView,
    MatchingContext,
    Stage1Candidate,
    _stage2_per_candidate,
    _stage2_skill_rows,
)
from src.schemas.matching import DEFAULT_WEIGHTS

# ── 1. MatchingContext.use_classified_families defaults to False ───────────


def _ctx(**overrides: Any) -> MatchingContext:
    base: dict[str, Any] = {
        "db": MagicMock(name="db"),
        "neo4j": MagicMock(name="neo4j"),
        "llm": MagicMock(name="llm"),
        "embedder": MagicMock(name="embedder"),
        "model_gen": "gen-model",
        "model_emb": "emb-model",
    }
    base.update(overrides)
    return MatchingContext(**base)


def test_matching_context_use_classified_families_defaults_to_false() -> None:
    ctx = _ctx()
    assert ctx.use_classified_families is False


def test_matching_context_use_classified_families_can_be_set_true() -> None:
    ctx = _ctx(use_classified_families=True)
    assert ctx.use_classified_families is True


# ── 2. _stage2_skill_rows accepts a use_classified_families keyword, ───────
#       defaulting False, mirroring family_weight / non_matchable_families ──


def test_stage2_skill_rows_signature_has_use_classified_families_keyword() -> None:
    sig = inspect.signature(_stage2_skill_rows)
    assert "use_classified_families" in sig.parameters, (
        "_stage2_skill_rows must accept a use_classified_families keyword -- "
        "the flag the family-credit arm honours (Stage 2 spec, this decision "
        "memo)"
    )
    param = sig.parameters["use_classified_families"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY, (
        "use_classified_families must be keyword-only, matching the "
        "existing family_weight / non_matchable_families convention"
    )
    assert param.default is False, (
        "use_classified_families must default to False -- classifier "
        "output must never move ranking unless a caller opts in explicitly"
    )


# ── 3. _stage2_per_candidate forwards ctx.use_classified_families into ─────
#       _stage2_skill_rows -- never a hardcoded literal, never read ad hoc ──


def _job() -> JobView:
    return JobView(
        id=uuid4(),
        title="Senior Backend Engineer",
        min_years=2,
        education_min_level=None,
        education_fields=(),
        required_skills=("python",),
        nice_to_have_skills=(),
    )


def _candidate() -> Stage1Candidate:
    return Stage1Candidate(resume_id=uuid4(), vec_score=0.8)


def _db_with_empty_parsed() -> MagicMock:
    db = MagicMock(name="db")
    db.fetchrow = AsyncMock(return_value={"parsed": {}})
    return db


@pytest.mark.asyncio
async def test_stage2_per_candidate_forwards_use_classified_families_true() -> None:
    ctx = _ctx(db=_db_with_empty_parsed(), use_classified_families=True)

    with patch(
        "src.pipeline.matching.orchestrator._stage2_skill_rows",
        new_callable=AsyncMock,
        return_value=[],
    ) as skill_rows:
        await _stage2_per_candidate(
            ctx, _job(), _candidate(), vec_normalised=1.0, weights=DEFAULT_WEIGHTS
        )

    skill_rows.assert_awaited_once()
    assert skill_rows.await_args.kwargs["use_classified_families"] is True


@pytest.mark.asyncio
async def test_stage2_per_candidate_forwards_use_classified_families_false_default() -> (  # noqa: E501
    None
):
    """The default-off path must ALSO explicitly thread ctx's value (False),
    not merely happen to omit the kwarg -- a caller reading it ad hoc from
    settings inside the loop, or defaulting the kwarg independently, would
    silently diverge from ctx.use_classified_families the moment a future
    caller sets it True on a MatchingContext built some other way."""
    ctx = _ctx(db=_db_with_empty_parsed(), use_classified_families=False)

    with patch(
        "src.pipeline.matching.orchestrator._stage2_skill_rows",
        new_callable=AsyncMock,
        return_value=[],
    ) as skill_rows:
        await _stage2_per_candidate(
            ctx, _job(), _candidate(), vec_normalised=1.0, weights=DEFAULT_WEIGHTS
        )

    skill_rows.assert_awaited_once()
    assert skill_rows.await_args.kwargs["use_classified_families"] is False


@pytest.mark.asyncio
async def test_stage2_per_candidate_also_still_forwards_family_weight_and_non_matchable() -> (  # noqa: E501
    None
):
    """Guard against a fix that adds use_classified_families but drops the
    two existing kwargs it sits beside (orchestrator.py:499-504)."""
    ctx = _ctx(
        db=_db_with_empty_parsed(),
        family_weight=0.42,
        non_matchable_families=("junk",),
        use_classified_families=True,
    )

    with patch(
        "src.pipeline.matching.orchestrator._stage2_skill_rows",
        new_callable=AsyncMock,
        return_value=[],
    ) as skill_rows:
        await _stage2_per_candidate(
            ctx, _job(), _candidate(), vec_normalised=1.0, weights=DEFAULT_WEIGHTS
        )

    kwargs = skill_rows.await_args.kwargs
    assert kwargs["family_weight"] == pytest.approx(0.42)
    assert kwargs["non_matchable_families"] == ("junk",)
    assert kwargs["use_classified_families"] is True
