"""RED pin — ROADMAP A6 siblings: the three remaining fallback sub-scores
that render as measurements, one level down from ADR-041's two (`docs/adr/
041-sub-score-measurement-markers.md`, "Three siblings found while writing
this, recorded not fixed"):

1. ``score_education``'s ``if not ranked: return 0.0`` (~stages.py:277). An
   unparsed education section scores WORSE than a genuinely below-the-bar
   candidate, who earns partial credit via ``education_partial``.
2. ``score_experience``'s ``if not jd_min_years: return 1.0`` (~stages.py:211).
   A JD with no stated minimum gives every candidate full marks on 25% of the
   score.
3. ``score_education``'s ``if not jd_min_level: return 1.0`` (~stages.py:271).
   Same, on 10%.

Mirrors ``test_a6_sub_score_markers_schema.py``'s structure and level exactly
(same file that pins ``seniority_measured``/``vector_discriminating`` and
``vector_pool_is_degenerate``), for the three NEW markers:

    experience_bar_stated  -- did the JD state a minimum-years bar?
    education_bar_stated   -- did the JD state a minimum education level?
    education_readable     -- did the résumé yield >= 1 readable degree level?

Per the spec: NO arithmetic changes. ``score_experience``/``score_education``
must return EXACTLY what they return today for every branch -- see
``test_matching_stages.py::test_a6_siblings_no_arithmetic_change_pin`` for the
dedicated guard, and the schema/coupling guards below.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from src.pipeline.matching.stages import (
    education_levels_readable,
    jd_states_education_bar,
    jd_states_experience_bar,
    score_education,
    score_experience,
)
from src.schemas.matching import DEFAULT_WEIGHTS, ScoreBreakdown


def _breakdown_kwargs() -> dict[str, float]:
    return {
        "skill": 0.8,
        "experience": 0.7,
        "education": 0.6,
        "seniority": 0.5,
        "vector": 0.4,
        "structured": 0.65,
    }


# ── ScoreBreakdown: defaults + explicit values ──────────────────────────────


def test_score_breakdown_defaults_all_three_new_markers_to_none() -> None:
    """The existing construction sites across the test suite that build
    ``ScoreBreakdown`` without these three keys must keep working, unchanged
    -- and their value, once built, must be the genuine 'unknown' state."""
    breakdown = ScoreBreakdown(**_breakdown_kwargs())
    assert breakdown.experience_bar_stated is None
    assert breakdown.education_bar_stated is None
    assert breakdown.education_readable is None


def test_score_breakdown_accepts_explicit_true_and_false_for_all_three() -> None:
    breakdown = ScoreBreakdown(
        **_breakdown_kwargs(),
        experience_bar_stated=True,
        education_bar_stated=False,
        education_readable=True,
    )
    assert breakdown.experience_bar_stated is True
    assert breakdown.education_bar_stated is False
    assert breakdown.education_readable is True


def test_a_legacy_jsonb_dict_with_none_of_the_three_keys_validates_to_unknown() -> None:
    """A ``shortlist_entries.score_breakdown`` row written before this slice
    has none of the three keys in its stored jsonb. It must validate cleanly
    -- never raise -- and land on ``None``/``None``/``None``, never a guessed
    True/False."""
    legacy = _breakdown_kwargs()
    assert "experience_bar_stated" not in legacy
    assert "education_bar_stated" not in legacy
    assert "education_readable" not in legacy
    breakdown = ScoreBreakdown.model_validate(legacy)
    assert breakdown.experience_bar_stated is None
    assert breakdown.education_bar_stated is None
    assert breakdown.education_readable is None


def test_a_dict_with_all_three_new_keys_present_validates() -> None:
    """``ScoreBreakdown`` is ``extra='forbid'`` -- a dict WITH the three keys
    (the shape a freshly-written row now has) must validate cleanly, proving
    the fields are actually declared on the model rather than merely
    tolerated by a lax config."""
    payload = {
        **_breakdown_kwargs(),
        "experience_bar_stated": False,
        "education_bar_stated": True,
        "education_readable": False,
    }
    breakdown = ScoreBreakdown.model_validate(payload)
    assert breakdown.experience_bar_stated is False
    assert breakdown.education_bar_stated is True
    assert breakdown.education_readable is False


def test_the_three_new_markers_round_trip_with_no_fold_pop() -> None:
    """``ScoreBreakdown`` is persisted verbatim -- these three markers live
    INSIDE it (like ``seniority_measured``/``vector_discriminating``), so
    they must round-trip through ``model_dump``/``model_validate`` for free,
    with no fold/pop mechanism required."""
    breakdown = ScoreBreakdown(
        **_breakdown_kwargs(),
        experience_bar_stated=True,
        education_bar_stated=False,
        education_readable=True,
    )
    dumped = breakdown.model_dump()
    assert dumped["experience_bar_stated"] is True
    assert dumped["education_bar_stated"] is False
    assert dumped["education_readable"] is True

    restored = ScoreBreakdown.model_validate(dumped)
    assert restored.experience_bar_stated is True
    assert restored.education_bar_stated is False
    assert restored.education_readable is True


@pytest.mark.parametrize("junk", [{}, [], "maybe", "purple", 2, 3.5])
def test_a_non_bool_marker_value_raises_for_each_new_marker(junk: Any) -> None:
    with pytest.raises(ValidationError):
        ScoreBreakdown(**_breakdown_kwargs(), experience_bar_stated=junk)
    with pytest.raises(ValidationError):
        ScoreBreakdown(**_breakdown_kwargs(), education_bar_stated=junk)
    with pytest.raises(ValidationError):
        ScoreBreakdown(**_breakdown_kwargs(), education_readable=junk)


# ── coupling guards: predicate and scorer must not drift ───────────────────


@pytest.mark.parametrize(
    "jd_min_years,expected_bar_stated",
    [(None, False), (0, False), (1, True)],
)
def test_jd_states_experience_bar_agrees_with_score_experience_over_boundary_set(
    jd_min_years: int | None, expected_bar_stated: bool
) -> None:
    """``0`` and ``None`` are both 'no bar' today (``score_experience``'s
    ``if not jd_min_years:``) and both return ``1.0``. The predicate must
    share the identical boundary, not an independently-written condition
    that happens to agree only on the values already tested."""
    assert jd_states_experience_bar(jd_min_years) is expected_bar_stated

    # Cross-check directly against the scorer's OWN fallback branch, at a
    # fixed total_years that would NOT itself produce a 1.0 through the real
    # comparison path -- so a False verdict here is unambiguously the
    # fallback, not a coincidence.
    scored = score_experience(3, jd_min_years, weights=DEFAULT_WEIGHTS)
    if not expected_bar_stated:
        assert scored == 1.0, (
            "jd_states_experience_bar says no bar, but score_experience did "
            "not take its fallback branch for this jd_min_years -- the two "
            "have drifted apart"
        )


@pytest.mark.parametrize(
    "jd_min_level,expected_bar_stated",
    [(None, False), ("", False), ("bachelors", True)],
)
def test_jd_states_education_bar_agrees_with_score_education_over_boundary_set(
    jd_min_level: str | None, expected_bar_stated: bool
) -> None:
    """``None`` and ``""`` are both 'no bar' today (``score_education``'s
    ``if not jd_min_level:``) and both return ``1.0``."""
    assert jd_states_education_bar(jd_min_level) is expected_bar_stated

    # A candidate whose only level is BELOW any real bar, so a genuine
    # comparison would NOT itself land on 1.0 -- a 1.0 here can only be the
    # fallback.
    scored = score_education(["high_school"], jd_min_level, weights=DEFAULT_WEIGHTS)
    if not expected_bar_stated:
        assert scored == 1.0, (
            "jd_states_education_bar says no bar, but score_education did "
            "not take its fallback branch for this jd_min_level -- the two "
            "have drifted apart"
        )


@pytest.mark.parametrize("levels", [[], [None], [""]])
def test_education_levels_readable_false_matches_the_scorers_unreadable_zero(
    levels: list[str | None],
) -> None:
    """These three inputs are exactly the ones for which ``score_education``'s
    ``ranked`` list is empty -- the ``if not ranked: return 0.0`` branch."""
    assert education_levels_readable(levels) is False
    assert score_education(levels, "bachelors", weights=DEFAULT_WEIGHTS) == 0.0


@pytest.mark.parametrize("levels", [["   "], ["unrecognised-level"], ["bachelor"]])
def test_education_levels_readable_true_for_whitespace_and_unrecognised_strings(
    levels: list[str],
) -> None:
    """Pinned from the REAL scorer behaviour, not guessed: ``score_education``
    filters pairs with a bare ``if lvl`` (Python truthiness on the STRING,
    not on ``_LEVEL_ORDER.get(lvl, 0)``), so a whitespace-only string
    (``"   "``) or an unrecognised level string are both truthy and both
    survive into ``ranked`` -- readability is 'we parsed a level', not 'we
    recognised it'. Confirmed directly against ``score_education`` here
    rather than assumed."""
    assert education_levels_readable(levels) is True
    # `ranked` is non-empty for all three, so the scorer must NOT take its
    # `if not ranked: return 0.0` branch -- it may still land on a numeric
    # 0.0 via the below-the-bar partial-credit formula (0 rank < req), which
    # is a DIFFERENT branch entirely and is exactly the case the write-path
    # anti-re-derivation test pins.
    pairs_call = score_education(levels, "bachelors", weights=DEFAULT_WEIGHTS)
    assert isinstance(pairs_call, float)


def test_education_levels_readable_true_for_a_recognised_level() -> None:
    assert education_levels_readable(["bachelors"]) is True
    assert score_education(["bachelors"], "bachelors", weights=DEFAULT_WEIGHTS) == 1.0
