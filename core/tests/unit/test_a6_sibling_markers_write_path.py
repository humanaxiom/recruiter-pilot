"""RED pin — ROADMAP A6 siblings, WRITE path: ``_stage2_per_candidate``
(``src.pipeline.matching.orchestrator``) must mark, for each of the three
remaining fallback branches, whether the underlying comparison actually ran.

Structured identically to ``test_a6_sub_score_markers_write_path.py`` (which
pins ``seniority_measured``/``vector_discriminating``): every I/O dependency
mocked/faked, no testcontainers -- this file proves ONLY
``_stage2_per_candidate``'s own branching.

    experience_bar_stated  -- set from `score_experience`'s own
                               `if not jd_min_years:` branch (via
                               ``jd_states_experience_bar``).
    education_bar_stated   -- set from `score_education`'s own
                               `if not jd_min_level:` branch (via
                               ``jd_states_education_bar``).
    education_readable     -- set from `score_education`'s own
                               `if not ranked:` branch (via
                               ``education_levels_readable``).

Per the spec: NO arithmetic changes anywhere in this file. Every assertion on
``breakdown.experience``/``breakdown.education`` below is a PIN of the
existing value, not a new expectation.
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
    """Zero-row async iterator -- every fixture below seeds a job with no
    required skills, so the skill sub-score is irrelevant to what this file
    pins."""

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
    def session(self) -> _FakeNeo4jSession:
        return _FakeNeo4jSession()


def _job(
    *,
    min_years: int | None = None,
    education_min_level: str | None = None,
    education_fields: tuple[str, ...] = (),
) -> JobView:
    return JobView(
        id=uuid4(),
        title="Senior Backend Engineer",
        min_years=min_years,
        education_min_level=education_min_level,
        education_fields=education_fields,
        required_skills=(),
        nice_to_have_skills=(),
    )


def _titled_embedder() -> MagicMock:
    async def _embed(texts: list[str]) -> list[list[float]]:
        # Identical vector for every text -> the seniority comparison
        # succeeds trivially; seniority is not what this file pins.
        return [[1.0, 0.0] for _ in texts]

    return MagicMock(embed=AsyncMock(side_effect=_embed))


def _ctx(parsed: dict[str, Any]) -> MatchingContext:
    db = MagicMock(fetchrow=AsyncMock(return_value={"parsed": json.dumps(parsed)}))
    return MatchingContext(
        db=db,
        neo4j=_FakeNeo4jDriver(),
        llm=MagicMock(name="llm"),
        embedder=_titled_embedder(),
        model_gen="test-gen",
        model_emb="test-emb",
    )


def _parsed(
    *,
    total_years: int | None,
    degree: str | None,
) -> dict[str, Any]:
    education = [{"degree": degree, "field": None}] if degree is not None else []
    return {
        "total_years_experience": total_years,
        "education": education,
        "experience": [{"title": "Senior Backend Engineer", "is_current": True}],
    }


async def _run(parsed: dict[str, Any], job: JobView) -> Any:
    ctx = _ctx(parsed)
    return await _stage2_per_candidate(
        ctx,
        job,
        Stage1Candidate(resume_id=uuid4(), vec_score=0.9),
        vec_normalised=1.0,
        vec_discriminating=True,
        weights=DEFAULT_WEIGHTS,
    )


# ── baseline wiring: both bars unstated, education readable ────────────────


async def test_no_bars_stated_and_readable_education_marks_all_three_correctly() -> (
    None
):
    """Baseline: a JD with neither bar and a résumé with a recognised degree.
    Both fallback ``1.0``s fire, and the marker for the readability axis is
    independently True."""
    parsed = _parsed(total_years=5, degree="Bachelor of Science")
    job = _job(min_years=None, education_min_level=None)

    result = await _run(parsed, job)

    assert result.breakdown.experience == 1.0
    assert result.breakdown.education == 1.0
    assert result.breakdown.experience_bar_stated is False
    assert result.breakdown.education_bar_stated is False
    assert result.breakdown.education_readable is True


# ── non-re-derivation guards (spec's REQUIRED list) ─────────────────────────


async def test_education_meets_bar_by_merit_marks_bar_stated_and_readable_true() -> (
    None
):
    """Education scoring a genuine ``1.0`` by merit (meets the level bar, no
    field restriction) -> both education markers must read True."""
    parsed = _parsed(total_years=5, degree="Bachelor of Science")
    job = _job(education_min_level="bachelors")

    result = await _run(parsed, job)

    assert result.breakdown.education == 1.0
    assert result.breakdown.education_bar_stated is True, (
        "a JD that DID state an education bar must be marked as having "
        "stated one, even though the candidate's score happens to be 1.0"
    )
    assert result.breakdown.education_readable is True


async def test_education_below_bar_partial_credit_marks_readable_true() -> None:
    """A genuinely below-the-bar candidate earns non-zero partial credit --
    the readability marker must be True, distinguishing this from the
    'nothing parsed at all' case that scores a harder 0.0."""
    parsed = _parsed(total_years=5, degree="High School Diploma")
    job = _job(education_min_level="bachelors")

    result = await _run(parsed, job)

    assert 0.0 < result.breakdown.education < 1.0, (
        "fixture drift -- this test needs a genuine below-the-bar PARTIAL "
        "credit score to distinguish it from the unreadable-education case"
    )
    assert (
        result.breakdown.education_readable is True
    ), "a readable (if below-bar) degree level must be marked readable"


async def test_unreadable_education_and_no_jd_bar_scores_full_marks_but_marks_both_false() -> (  # noqa: E501
    None
):
    """THE anti-re-derivation pin for D-education-readable: an unparsed
    education section AND a JD stating no education bar. ``score_education``
    hits its `if not jd_min_level: return 1.0` branch BEFORE it ever looks at
    the (empty) levels list, so the stored value is a full-marks fallback --
    but neither axis was actually assessed.

    This is the exact case that kills
    ``education_readable = (education != 0.0)``: that mutant would derive
    True from a 1.0 score, when in fact nothing was read or compared."""
    parsed = _parsed(total_years=5, degree=None)
    job = _job(education_min_level=None)

    result = await _run(parsed, job)

    assert result.breakdown.education == 1.0, (
        "fixture drift -- score_education must take the no-bar fallback "
        "branch, not the unreadable-education 0.0 branch, for this test to "
        "distinguish the two markers from the score"
    )
    assert result.breakdown.education_readable is False, (
        "no degree level was readable on this résumé -- marking it "
        "readable=True (derived from the 1.0 score) would be exactly the "
        "fabrication this marker exists to prevent"
    )
    assert result.breakdown.education_bar_stated is False, (
        "the JD stated no education bar -- this axis must also read False, "
        "independent of the readability axis"
    )


async def test_experience_genuinely_scoring_zero_marks_bar_stated_true() -> None:
    """``total_years=0`` against a REAL, stated minimum of 5: the comparison
    ran and genuinely produced the same ``0.0`` a fallback would -- the
    marker must still read True, because a real comparison happened."""
    parsed = _parsed(total_years=0, degree="Bachelor of Science")
    job = _job(min_years=5, education_min_level=None)

    result = await _run(parsed, job)

    assert result.breakdown.experience == 0.0, (
        "fixture drift -- this test needs a genuine zero from a real "
        "comparison, not the no-bar fallback"
    )
    assert result.breakdown.experience_bar_stated is True, (
        "a JD that stated a real years bar must be marked stated, even "
        "though this candidate's own comparison genuinely scored zero"
    )


async def test_experience_genuinely_scoring_one_by_merit_marks_bar_stated_true() -> (
    None
):
    """THE anti-re-derivation pin for D-experience: a candidate who exactly
    meets a REAL, stated minimum scores a genuine ``1.0`` by merit -- kills
    ``experience_bar_stated = (experience != 1.0)``, which would derive False
    here even though the JD plainly stated a bar and a real comparison ran."""
    parsed = _parsed(total_years=5, degree="Bachelor of Science")
    job = _job(min_years=5, education_min_level=None)

    result = await _run(parsed, job)

    assert result.breakdown.experience == 1.0, (
        "fixture drift -- this test needs a genuine merit-based 1.0, not "
        "the no-bar fallback"
    )
    assert result.breakdown.experience_bar_stated is True, (
        "a JD that stated a real years bar must be marked stated -- a "
        "re-derivation mutant (`experience != 1.0`) would wrongly read this "
        "as unstated because the merit score happens to equal the fallback "
        "value"
    )


# ── independence: the three markers must not leak into each other ──────────


async def test_the_three_markers_are_set_independently_of_each_other() -> None:
    """A JD that states an experience bar but NOT an education bar, against a
    résumé with an unreadable education section -- each marker must reflect
    only its own axis."""
    parsed = _parsed(total_years=5, degree=None)
    job = _job(min_years=5, education_min_level=None)

    result = await _run(parsed, job)

    assert result.breakdown.experience_bar_stated is True
    assert result.breakdown.education_bar_stated is False
    assert result.breakdown.education_readable is False


# ── real corpus shape (merge-blocking review, Gap 1): a JD that STATES an
# ── education bar against a résumé whose only degree string is one
# ── `_level_from_degree` cannot map to any level at all -- confirmed against
# ── the eval corpus itself (r07 "Certificate, Full-Stack Web Development",
# ── r08 "Diploma, Business Administration"), not a hypothetical shape ───────


async def test_stated_bachelor_bar_against_an_unmapped_degree_string_is_unreadable_not_unstated() -> (  # noqa: E501
    None
):
    """The production quadrant the anti-re-derivation tests above never hit:
    ``education_bar_stated`` and ``education_readable`` are True/False on
    DIFFERENT axes at once. The JD genuinely states a bachelors-level bar
    (``education_bar_stated`` must read True), but the candidate's only
    degree string ("Certificate, Full-Stack Web Development", in the spirit
    of the corpus's own r07 row) matches none of ``_DEGREE_KEYWORDS``, so
    ``_level_from_degree`` returns ``None`` and the résumé contributes no
    readable level at all (``education_readable`` must read False) --
    ``score_education`` takes its unreadable-``0.0`` branch, not a
    below-the-bar partial-credit branch."""
    parsed = _parsed(total_years=5, degree="Certificate, Full-Stack Web Development")
    job = _job(education_min_level="bachelors")

    result = await _run(parsed, job)

    assert result.breakdown.education == 0.0, (
        "fixture drift -- an unmapped degree string against a stated bar "
        "must take the unreadable-education 0.0 branch, not a partial-"
        "credit below-the-bar score"
    )
    assert result.breakdown.education_bar_stated is True, (
        "the JD plainly stated a bachelors-level bar -- this axis must read "
        "True even though the candidate's own level could not be read"
    )
    assert result.breakdown.education_readable is False, (
        "no degree level was readable from this candidate's résumé -- "
        "marking it readable would fabricate a comparison that never ran"
    )
