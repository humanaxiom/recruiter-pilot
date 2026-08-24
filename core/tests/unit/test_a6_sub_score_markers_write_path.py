"""RED pin — ROADMAP A6 write path, forward direction:
``_stage2_per_candidate`` (``src.pipeline.matching.orchestrator``) must MARK
whether each of the two sub-scores it computes was a real comparison, not
just compute the number.

Mirrors ``test_evidence_evaluated_write_path.py``'s level (the write-path
mechanism, not the rendering) but for the two markers that live INSIDE
``ScoreBreakdown`` rather than folded by hand.

Every I/O dependency is mocked/faked here (Postgres via ``ctx.db.fetchrow``,
Neo4j via a tiny fake driver/session, the embedder via ``AsyncMock``) — no
testcontainers needed to prove ``_stage2_per_candidate``'s OWN branching.
The reverse-match CALL SITE (``match_resume_to_jobs``, which threads its own
independently-computed ``vec_discriminating`` through the same function) is
pinned separately, against real Postgres/Neo4j, in
``tests/integration/test_matching_orchestrator.py`` — reverse match's stage-1
query is Cypher this repo has no other real-driver coverage for
(``docs/ROADMAP.md``'s own open residual), so a mock there would prove
nothing a fake couldn't already.

Per the spec's explicit constraint: the no-title branch must NOT call
``ctx.embedder`` at all (``test_shortlist_fail_closed_pg.py`` already relies
on exactly this — its worker context's embedder is a bare ``MagicMock``, and
``await MagicMock()`` raises ``TypeError``, which would escape stage 2
uncaught). Every embedder here is therefore a plain ``MagicMock`` (never an
``AsyncMock``) unless a test explicitly expects it to be awaited.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from src.pipeline.matching.orchestrator import (
    JobView,
    MatchingContext,
    Stage1Candidate,
    _stage2_per_candidate,
)
from src.schemas.matching import DEFAULT_WEIGHTS


class _FakeNeo4jResult:
    """Zero-row async iterator. Every fixture below seeds a job with no
    required skills, so an empty skill-row set is always the correct
    response regardless of the Cypher text — the skill sub-score is
    irrelevant to what this file pins."""

    def __aiter__(self) -> Any:
        return self._empty()

    async def _empty(self) -> Any:
        return
        yield  # pragma: no cover -- makes this an async generator with 0 items


class _FakeNeo4jSession:
    async def run(self, *_a: Any, **_k: Any) -> _FakeNeo4jResult:
        return _FakeNeo4jResult()

    async def __aenter__(self) -> _FakeNeo4jSession:
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        return None


class _FakeNeo4jDriver:
    """Stands in for the real ``AsyncDriver`` — ``_stage2_skill_rows`` is the
    only caller inside ``_stage2_per_candidate`` that touches it."""

    def session(self) -> _FakeNeo4jSession:
        return _FakeNeo4jSession()


def _job(*, title: str = "Senior Backend Engineer") -> JobView:
    return JobView(
        id=uuid4(),
        title=title,
        min_years=None,
        education_min_level=None,
        education_fields=(),
        required_skills=(),
        nice_to_have_skills=(),
    )


def _ctx(
    parsed: dict[str, Any], *, embedder: MagicMock | None = None
) -> MatchingContext:
    db = MagicMock(fetchrow=AsyncMock(return_value={"parsed": json.dumps(parsed)}))
    return MatchingContext(
        db=db,
        neo4j=_FakeNeo4jDriver(),
        llm=MagicMock(name="llm"),
        embedder=embedder if embedder is not None else MagicMock(name="embedder"),
        model_gen="test-gen",
        model_emb="test-emb",
    )


def _titled_embedder() -> MagicMock:
    async def _embed(texts: list[str]) -> list[list[float]]:
        # Identical vector for every text -> cosine(x, x) == 1.0 regardless
        # of embedder internals, so the seniority VALUE is not what this file
        # is pinning -- only whether the comparison ran at all.
        return [[1.0, 0.0] for _ in texts]

    return MagicMock(embed=AsyncMock(side_effect=_embed))


def _orthogonal_embedder() -> MagicMock:
    """Distinct orthogonal unit vectors -> ``_cosine`` is exactly 0.0, and the
    ``[seniority_floor, 1.0] -> [0, 1]`` rescale then clamps to exactly 0.0.

    This is the ONLY way to build a candidate whose seniority comparison
    genuinely RAN and genuinely scored zero -- which is what makes the
    re-derivation mutant below killable."""

    async def _embed(texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0], [0.0, 1.0]][: len(texts)]

    return MagicMock(embed=AsyncMock(side_effect=_embed))


async def test_normal_call_marks_both_measured_and_discriminating_true() -> None:
    """A readable title and a caller-supplied ``vec_discriminating=True`` (a
    pool with real spread) must both be recorded as measured."""
    parsed = {
        "total_years_experience": 5,
        "education": [],
        "experience": [{"title": "Senior Backend Engineer", "is_current": True}],
    }
    embedder = _titled_embedder()
    ctx = _ctx(parsed, embedder=embedder)

    result = await _stage2_per_candidate(
        ctx,
        _job(),
        Stage1Candidate(resume_id=uuid4(), vec_score=0.9),
        vec_normalised=1.0,
        vec_discriminating=True,
        weights=DEFAULT_WEIGHTS,
    )

    assert result.breakdown.seniority_measured is True, (
        "a readable, comparable title must be marked measured, not left "
        "at its no-title fallback"
    )
    assert result.breakdown.vector_discriminating is True, (
        "a pool with real spread must be marked discriminating, not "
        "collapsed to the degenerate case"
    )
    embedder.embed.assert_awaited()


async def test_no_title_marks_seniority_measured_false_and_skips_embedder() -> None:
    """No experience at all -> ``_most_recent_title`` returns None ->
    ``seniority = 0.0`` from a policy fallback, not a comparison. The
    embedder must never be awaited on this path."""
    parsed = {"total_years_experience": 3, "education": [], "experience": []}
    embedder = MagicMock(name="embedder-must-not-be-called")
    ctx = _ctx(parsed, embedder=embedder)

    result = await _stage2_per_candidate(
        ctx,
        _job(),
        Stage1Candidate(resume_id=uuid4(), vec_score=0.9),
        vec_normalised=1.0,
        vec_discriminating=True,
        weights=DEFAULT_WEIGHTS,
    )

    assert result.breakdown.seniority == 0.0
    assert result.breakdown.seniority_measured is False, (
        "no title was readable anywhere on this résumé -- the 0.0 is a "
        "policy fallback and must be marked unmeasured, not a real 0% "
        "seniority match"
    )
    embedder.embed.assert_not_called()


async def test_degenerate_pool_flag_threads_through_independent_of_seniority() -> None:
    """``vec_discriminating`` is a caller-supplied fact about the WHOLE pool,
    computed once outside this function — it must land on the breakdown
    unchanged, independent of what the seniority branch decided."""
    parsed = {
        "total_years_experience": 5,
        "education": [],
        "experience": [{"title": "Senior Backend Engineer", "is_current": True}],
    }
    ctx = _ctx(parsed, embedder=_titled_embedder())

    result = await _stage2_per_candidate(
        ctx,
        _job(),
        Stage1Candidate(resume_id=uuid4(), vec_score=0.9),
        vec_normalised=1.0,
        vec_discriminating=False,
        weights=DEFAULT_WEIGHTS,
    )

    assert result.breakdown.vector_discriminating is False, (
        "a degenerate pool (e.g. this résumé's only candidate job) must be "
        "marked, even though this candidate's OWN title was readable"
    )
    assert result.breakdown.seniority_measured is True, (
        "the two markers are independent axes -- a degenerate vector pool "
        "must not suppress a genuinely measured seniority comparison"
    )


async def test_seniority_measured_true_via_previous_role_fallback() -> None:
    """ROADMAP A6 (4)'s fallback fix, exercised through the write path: a
    blank CURRENT title with a titled PREVIOUS role must still count as
    measured — the fallback widens which title is read, not whether a
    comparison happened."""
    parsed = {
        "total_years_experience": 6,
        "education": [],
        "experience": [
            {"title": "", "is_current": True},
            {"title": "Senior Backend Engineer", "is_current": False},
        ],
    }
    embedder = _titled_embedder()
    ctx = _ctx(parsed, embedder=embedder)

    result = await _stage2_per_candidate(
        ctx,
        _job(),
        Stage1Candidate(resume_id=uuid4(), vec_score=0.9),
        vec_normalised=1.0,
        vec_discriminating=True,
        weights=DEFAULT_WEIGHTS,
    )

    assert result.breakdown.seniority_measured is True, (
        "the previous role's readable title must be used via the fallback, "
        "and the resulting comparison marked measured -- not silently "
        "demoted to the no-title 0.0 fallback"
    )
    embedder.embed.assert_awaited()


# ── the markers must not be RE-DERIVED from the numbers they annotate ───────


async def test_seniority_measured_is_not_re_derived_from_a_zero_score() -> None:
    """THE anti-re-derivation pin for D2, and the whole point of the marker.

    A readable title that scores a genuine 0.0 (title cosine at or below
    ``seniority_floor``) and a missing title that falls back to 0.0 produce
    the SAME number. If the marker is computed from that number
    (``seniority_measured = seniority != 0.0``) rather than from the branch
    actually taken, the two collapse -- and the panel goes back to saying
    "not assessed" about a candidate whose title WAS read and compared and
    genuinely did not match. That mutant passes every other test in this
    file, because every one of them pairs measured=True with a 1.0 score and
    measured=False with a 0.0 score."""
    parsed = {
        "total_years_experience": 5,
        "education": [],
        "experience": [{"title": "Store Clerk", "is_current": True}],
    }
    embedder = _orthogonal_embedder()
    ctx = _ctx(parsed, embedder=embedder)

    result = await _stage2_per_candidate(
        ctx,
        _job(title="Senior Backend Engineer"),
        Stage1Candidate(resume_id=uuid4(), vec_score=0.9),
        vec_normalised=1.0,
        vec_discriminating=True,
        weights=DEFAULT_WEIGHTS,
    )

    assert result.breakdown.seniority == 0.0, (
        "fixture drift -- this test needs a title comparison that genuinely "
        "scores zero, otherwise it cannot distinguish the two cases"
    )
    embedder.embed.assert_awaited()
    assert result.breakdown.seniority_measured is True, (
        "a title WAS read and compared and honestly scored 0.0 -- marking it "
        "unmeasured would tell the recruiter nothing was assessed when in "
        "fact this candidate was assessed and did not match"
    )


async def test_an_unsupplied_pool_opinion_is_unknown_never_an_affirmative_claim() -> (
    None
):
    """A caller that supplies no ``vec_discriminating`` has no opinion about
    the pool -- it may not have a pool at all. The recorded marker must then
    be ``None`` ("we do not know"), never an affirmative ``True`` asserting
    that a real comparison happened.

    This is the parameter-default direction of the same defect: ADR-040's
    ``ShortlistResultEntry.evidence_evaluated`` defaults to ``False`` and so
    structurally cannot express "unknown", meaning any future construction
    site that forgets the kwarg silently persists a claim about a real
    person. Pointed the other way here, a ``True`` default would manufacture
    the affirmative claim instead."""
    parsed = {
        "total_years_experience": 5,
        "education": [],
        "experience": [{"title": "Senior Backend Engineer", "is_current": True}],
    }
    ctx = _ctx(parsed, embedder=_titled_embedder())

    result = await _stage2_per_candidate(
        ctx,
        _job(),
        Stage1Candidate(resume_id=uuid4(), vec_score=0.9),
        vec_normalised=1.0,
        weights=DEFAULT_WEIGHTS,
    )

    assert result.breakdown.vector_discriminating is None, (
        "an unsupplied pool opinion became an affirmative 'this pool "
        "discriminated' claim -- the same invented fact the marker exists "
        "to prevent, pointed the other way"
    )
