"""RED pin — ROADMAP A6: two sub-scores render as affirmative measurements
when nothing was measured, one layer down from ADR-040's evidence cliff.

**D1 — ``normalise_vector_scores`` degenerate pool.** On ``hi - lo < 1e-9``
(every single-candidate pool, which reverse match hits routinely) it returns
``1.0`` for everyone. The panel renders ``vector | 10 | 100 | 10`` — a
perfect semantic match — when no comparison was possible at all.

**D2 — seniority fallback.** When ``_most_recent_title(parsed)`` is falsy,
``seniority = 0.0``, byte-identical to a genuinely poor title match.

**The fix, mirrored from ADR-040.** Mark it on the write path, disclose it on
the read path, leave the arithmetic alone. Both markers live INSIDE
``ScoreBreakdown`` (not folded into the jsonb by hand like
``evidence_evaluated`` — ``ScoreBreakdown`` is stored verbatim, so they
round-trip for free), defaulted to ``None`` so every existing construction
site across ~25 test files keeps working and a pre-existing jsonb row (no
key) validates to the legacy "unknown" state.

This file pins the SCHEMA layer only:
  * both markers default to ``None`` and accept explicit ``True``/``False``;
  * a legacy dict with neither key validates cleanly to ``None``/``None``;
  * the free round trip (no fold/pop path — spec explicitly forbids adding
    one);
  * ``vector_pool_is_degenerate`` shares the EXACT ``_DEGENERATE_POOL_EPS``
    constant ``normalise_vector_scores`` uses, so the two callers cannot
    drift on the threshold (a second literal ``1e-9`` is precisely the A7
    defect shape);
  * identity, not truthiness (spec item 7): a non-bool marker value must
    raise, not silently become an affirmative measurement, and pydantic's
    own lax bool aliases (``0``/``1``/``0.0``/``1.0``) still coerce to a
    REAL, identity-comparable ``True``/``False`` singleton.

REMEDIATION ROUND (reviewer CHANGES-REQUIRED on `fix/a6-fabricated-sub-scores`):
  * F3 — the existing boundary probes above (``just_inside``/``just_outside``)
    sit strictly either side of ``_DEGENERATE_POOL_EPS`` and cannot see a
    mutant that flips the shared comparison's `<` to `<=`: both operators
    agree at every point strictly inside or outside the boundary, and only
    disagree AT the boundary itself. The remediation also makes
    ``normalise_vector_scores`` delegate to ``vector_pool_is_degenerate`` so
    exactly one comparison exists in the codebase (F3's structural fix) — the
    tests below pin the OBSERVABLE behaviour of that one comparison at the
    exact boundary value, not its internal structure, so they hold regardless
    of which function textually owns the comparison.
  * F4 — ``vector_pool_is_degenerate([])`` currently returns ``False``, the
    AFFIRMATIVE direction ("a real comparison happened") for a pool that by
    definition has no spread at all. Unreachable in production today (an
    empty stage-1 pool short-circuits before any breakdown is built) but
    still the wrong default for the same reason ADR-041 exists, and nothing
    pinned the branch — pinned below.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from src.pipeline.matching.stages import (
    _DEGENERATE_POOL_EPS,
    normalise_vector_scores,
    vector_pool_is_degenerate,
)
from src.schemas.matching import ScoreBreakdown


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


def test_score_breakdown_defaults_both_new_markers_to_none() -> None:
    """The ~25 existing test files that construct ``ScoreBreakdown`` without
    either marker must keep working, unchanged — and their value, once built,
    must be the genuine "unknown" state, not a guessed default."""
    breakdown = ScoreBreakdown(**_breakdown_kwargs())
    assert breakdown.seniority_measured is None
    assert breakdown.vector_discriminating is None


def test_score_breakdown_accepts_explicit_true_and_false() -> None:
    breakdown = ScoreBreakdown(
        **_breakdown_kwargs(), seniority_measured=True, vector_discriminating=False
    )
    assert breakdown.seniority_measured is True
    assert breakdown.vector_discriminating is False


def test_a_legacy_jsonb_dict_with_neither_key_validates_to_the_unknown_state() -> None:
    """A ``shortlist_entries.score_breakdown`` row written before this slice
    has neither key in its stored jsonb at all. It must validate cleanly —
    never raise — and land on ``None``/``None``, never a guessed True/False."""
    legacy = _breakdown_kwargs()
    assert "seniority_measured" not in legacy
    assert "vector_discriminating" not in legacy
    breakdown = ScoreBreakdown.model_validate(legacy)
    assert breakdown.seniority_measured is None
    assert breakdown.vector_discriminating is None


def test_the_markers_round_trip_with_no_fold_pop() -> None:
    """Unlike ``evidence_evaluated`` (folded into the jsonb BY HAND because
    ``ShortlistEntry`` has no column for it), these two markers live INSIDE
    ``ScoreBreakdown`` itself, which is persisted verbatim — so they must
    round-trip for free, with no fold/pop path at all."""
    breakdown = ScoreBreakdown(
        **_breakdown_kwargs(), seniority_measured=False, vector_discriminating=True
    )
    dumped = breakdown.model_dump()
    assert dumped["seniority_measured"] is False
    assert dumped["vector_discriminating"] is True

    restored = ScoreBreakdown.model_validate(dumped)
    assert restored.seniority_measured is False
    assert restored.vector_discriminating is True


# ── identity, not truthiness (spec item 7) ──────────────────────────────────


@pytest.mark.parametrize("junk", [{}, [], "maybe", "purple", 2, 3.5])
def test_a_non_bool_marker_value_raises_rather_than_becoming_a_silent_measurement(
    junk: Any,
) -> None:
    """A value that is neither a real bool nor one of pydantic's lax bool
    aliases must raise, not silently coerce into an affirmative "this was
    measured" claim about a candidate."""
    with pytest.raises(ValidationError):
        ScoreBreakdown(**_breakdown_kwargs(), seniority_measured=junk)
    with pytest.raises(ValidationError):
        ScoreBreakdown(**_breakdown_kwargs(), vector_discriminating=junk)


@pytest.mark.parametrize(
    "raw,expected",
    [
        (True, True),
        (False, False),
        (None, None),
        (0, False),
        (1, True),
        (0.0, False),
        (1.0, True),
    ],
)
def test_marker_values_coerce_to_a_real_identity_comparable_bool_or_none(
    raw: Any, expected: bool | None
) -> None:
    """pydantic's lax bool aliases (``0``/``1``/``0.0``/``1.0``) still
    coerce to the REAL ``True``/``False`` singleton — asserted with ``is``,
    never ``==``, so this test cannot be satisfied by a mutant that merely
    checks equality."""
    breakdown = ScoreBreakdown(**_breakdown_kwargs(), seniority_measured=raw)
    if expected is None:
        assert breakdown.seniority_measured is None
    elif expected is True:
        assert breakdown.seniority_measured is True
    else:
        assert breakdown.seniority_measured is False


# ── vector_pool_is_degenerate: the shared epsilon (D1's write-path helper) ──


def test_vector_pool_is_degenerate_true_for_a_single_element_pool() -> None:
    """Every reverse-match résumé with exactly one candidate job hits this —
    the routine case the defect targets, not an edge case."""
    assert vector_pool_is_degenerate([5.0]) is True


def test_vector_pool_is_degenerate_true_for_all_identical_values() -> None:
    assert vector_pool_is_degenerate([3.0, 3.0, 3.0]) is True


def test_vector_pool_is_degenerate_false_for_a_pool_with_spread() -> None:
    assert vector_pool_is_degenerate([1.0, 2.0, 3.0]) is False


def test_vector_pool_is_degenerate_false_for_negative_value_spread() -> None:
    assert vector_pool_is_degenerate([-5.0, 5.0]) is False


def test_degenerate_pool_eps_is_1e_minus_9() -> None:
    assert _DEGENERATE_POOL_EPS == 1e-9


def test_vector_pool_is_degenerate_shares_normalise_vector_scores_epsilon() -> None:
    """The two callers must not drift on the threshold — a second literal
    ``1e-9`` written independently in ``vector_pool_is_degenerate`` is
    precisely the A7 defect shape (a duplicated threshold constant) the
    branch's own spec warns against. Pin them against the SAME boundary."""
    just_inside = [3.0, 3.0 + _DEGENERATE_POOL_EPS / 2]
    just_outside = [3.0, 3.0 + _DEGENERATE_POOL_EPS * 10]

    assert vector_pool_is_degenerate(just_inside) is True
    assert normalise_vector_scores(just_inside) == [1.0, 1.0], (
        "vector_pool_is_degenerate says degenerate but normalise_vector_scores "
        "disagrees -- the two are reading different thresholds"
    )

    assert vector_pool_is_degenerate(just_outside) is False
    assert normalise_vector_scores(just_outside) != [1.0, 1.0], (
        "vector_pool_is_degenerate says NOT degenerate but "
        "normalise_vector_scores still collapsed it to [1.0, 1.0] -- the two "
        "are reading different thresholds"
    )


# ── F3 (remediation) — the boundary itself, not just either side of it ─────
#
# `just_inside`/`just_outside` above sit strictly either side of the
# boundary and cannot distinguish `<` from `<=`: both operators agree
# everywhere except AT `_DEGENERATE_POOL_EPS` exactly. `0.0` is used as the
# pool's low value (rather than e.g. `3.0`) so `hi - lo` is an EXACT float
# subtraction with no rounding error — `_DEGENERATE_POOL_EPS - 0.0 ==
# _DEGENERATE_POOL_EPS` bit-for-bit, so this pins the true boundary rather
# than a value merely close to it.


def test_vector_pool_is_degenerate_false_exactly_at_the_epsilon_boundary() -> None:
    """A spread of EXACTLY ``_DEGENERATE_POOL_EPS`` is NOT degenerate — the
    comparison is a strict ``<``. A mutant that flips it to ``<=`` marks this
    pool degenerate and this assertion catches it; the two probes further up
    this file (strictly inside/outside) cannot."""
    at_boundary = [0.0, _DEGENERATE_POOL_EPS]
    assert at_boundary[1] - at_boundary[0] == _DEGENERATE_POOL_EPS, (
        "test setup bug: this must be an EXACT float subtraction with no "
        "rounding, or the boundary pin is not actually pinning the boundary"
    )
    assert vector_pool_is_degenerate(at_boundary) is False


def test_normalise_vector_scores_does_not_collapse_at_the_epsilon_boundary() -> None:
    """Sibling of the pin directly above, through ``normalise_vector_scores``
    itself — after F3's structural fix (``normalise_vector_scores`` delegates
    to ``vector_pool_is_degenerate``) there is exactly one comparison in the
    codebase, so this and the test above must never disagree."""
    at_boundary = [0.0, _DEGENERATE_POOL_EPS]
    assert normalise_vector_scores(at_boundary) == [0.0, 1.0], (
        "a spread of exactly _DEGENERATE_POOL_EPS is real spread, not a "
        "degenerate pool -- it must be min-max scaled, not collapsed to "
        "[1.0, 1.0]"
    )


# ── F4 (remediation) — the empty pool must read as "no spread", not "real" ──


def test_vector_pool_is_degenerate_true_for_an_empty_pool() -> None:
    """An empty pool has no spread at all -- the AFFIRMATIVE "a real
    comparison happened" reading (``False``, the pre-remediation return
    value) is the wrong default for exactly the reason this whole marker
    exists: it would let a caller with nothing to compare persist
    ``vector_discriminating=True``. Unreachable in production today (an
    empty stage-1 pool short-circuits `generate_shortlist` / `match_resume_to_jobs`
    before any `ScoreBreakdown` is ever built) but the branch must still read
    the honest direction."""
    assert vector_pool_is_degenerate([]) is True
