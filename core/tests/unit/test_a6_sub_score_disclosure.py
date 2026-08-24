"""RED pin — ROADMAP A6: two sub-scores render as affirmative measurements
when nothing was measured. This is the same defect shape ADR-040 closed for
the evidence cliff, one layer down, and it is pinned the same way: this file
covers the READ path (``src.services.explanation.shortlist_entry_explanation``)
mirroring ``tests/unit/test_evidence_cliff_disclosure.py``'s structure.

**D1 — vector.** ``normalise_vector_scores`` returns ``1.0`` for every
candidate in a degenerate pool (``hi - lo < 1e-9`` — every single-candidate
pool). ``score_breakdown.vector_discriminating`` records whether that pool
actually had spread; ``explanation.vector_comparable`` copies it FAITHFULLY.

**D2 — seniority.** When ``_most_recent_title(parsed)`` is falsy,
``seniority = 0.0`` — a policy default, not a measurement.
``score_breakdown.seniority_measured`` records whether a title comparison
actually ran; ``explanation.seniority_assessed`` copies it faithfully.

**Both markers are a DIFFERENT question from ``scores_available``**, exactly
as ``evidence_assessed`` is a different question in the sibling file. A
past-the-fallback row stores a perfectly readable ``0.0``/``1.0``, so
``scores_available`` can be ``True`` while these are ``False``. Collapsing
them silently re-opens the defect.
"""

from __future__ import annotations

import datetime as dt
from uuid import uuid4

from src.schemas.matching import EvidenceObject, ScoreBreakdown, ShortlistEntry
from src.services.explanation import shortlist_entry_explanation


def _breakdown(
    *,
    seniority_measured: bool | None,
    vector_discriminating: bool | None,
    seniority: float = 0.5,
    vector: float = 0.4,
) -> ScoreBreakdown:
    return ScoreBreakdown(
        skill=0.8,
        experience=0.7,
        education=0.6,
        seniority=seniority,
        vector=vector,
        structured=0.65,
        seniority_measured=seniority_measured,
        vector_discriminating=vector_discriminating,
    )


def _entry(
    *,
    breakdown: ScoreBreakdown,
    score_structured: float | None = 0.65,
    score_evidence: float | None = 0.0,
) -> ShortlistEntry:
    return ShortlistEntry.model_validate(
        {
            "id": uuid4(),
            "job_id": uuid4(),
            "resume_id": uuid4(),
            "rank": 3,
            "score_final": 0.5,
            "score_structured": score_structured,
            "score_evidence": score_evidence,
            "score_breakdown": breakdown,
            "evidence": EvidenceObject(),
            "pipeline_meta": None,
            "generated_at": dt.datetime(2026, 8, 13, tzinfo=dt.UTC),
            "evidence_evaluated": None,
        }
    )


# ── D1: vector_comparable ────────────────────────────────────────────────


def test_a_degenerate_vector_pool_candidate_is_marked_not_comparable() -> None:
    """THE pin for D1. A single-candidate (or otherwise all-identical) pool
    scored a perfect 100% on semantic similarity for EVERY candidate in it —
    that reflects the pool, not the match, and the panel must not present it
    as a measured comparison."""
    breakdown = _breakdown(
        seniority_measured=None, vector_discriminating=False, vector=1.0
    )
    explanation = shortlist_entry_explanation(_entry(breakdown=breakdown))

    assert explanation.vector_comparable is False, (
        "a degenerate-pool candidate is being presented as having a measured "
        "100% semantic match — a positive false claim about a person on the "
        "page that defends the ranking"
    )


def test_a_pool_with_spread_reports_the_real_measured_vector_score() -> None:
    """The mirror-image mutant: a genuinely discriminating pool must keep
    rendering its real score, never 'not assessed'."""
    breakdown = _breakdown(
        seniority_measured=None, vector_discriminating=True, vector=0.42
    )
    explanation = shortlist_entry_explanation(_entry(breakdown=breakdown))

    assert explanation.vector_comparable is True
    assert explanation.vector.score == 0.42, (
        "a genuinely-measured vector score must survive as itself — hiding it "
        "would be the mirror image of the defect being fixed"
    )


# ── D2: seniority_assessed ───────────────────────────────────────────────


def test_seniority_not_measured_is_disclosed() -> None:
    """THE pin for D2. No readable title -> the stored 0.0 came from a
    fallback, not from a comparison, and the panel must not present it as a
    measured poor title match."""
    breakdown = _breakdown(seniority_measured=False, vector_discriminating=None)
    explanation = shortlist_entry_explanation(_entry(breakdown=breakdown))

    assert explanation.seniority_assessed is False, (
        "a candidate with no readable title is being presented as having a "
        "measured seniority score of 0 — indistinguishable from a genuinely "
        "poor title match"
    )


def test_a_readable_title_reports_the_real_measured_seniority_score() -> None:
    breakdown = _breakdown(
        seniority_measured=True, vector_discriminating=None, seniority=0.62
    )
    explanation = shortlist_entry_explanation(_entry(breakdown=breakdown))

    assert explanation.seniority_assessed is True
    assert explanation.seniority.score == 0.62


# ── legacy row: neither marker claimed ───────────────────────────────────


def test_a_legacy_row_with_neither_marker_makes_no_claim_either_way() -> None:
    """``None`` means the row predates both markers. We do not know whether
    either comparison ran for it, so the panel must not assert either way —
    following ADR-031's ``pipeline_meta=None`` -> "weights unavailable"
    discipline exactly."""
    breakdown = _breakdown(seniority_measured=None, vector_discriminating=None)
    explanation = shortlist_entry_explanation(_entry(breakdown=breakdown))

    assert explanation.seniority_assessed is None, (
        "a legacy row's unknown seniority-measurement state must stay "
        "unknown — claiming either 'measured' or 'not measured' invents a fact"
    )
    assert explanation.vector_comparable is None, (
        "a legacy row's unknown vector-comparability state must stay "
        "unknown — claiming either 'comparable' or 'not comparable' invents "
        "a fact"
    )


# ── independence from scores_available (spec item 5) ─────────────────────


def test_the_markers_are_independent_of_scores_available() -> None:
    """``scores_available`` answers "did the row store a composed number";
    these two markers answer "did the underlying comparison actually run".
    They are different questions and must not be collapsed: a row with
    BOTH markers False can still carry a fully recorded, readable
    ``score_structured``/``score_evidence`` pair."""
    breakdown = _breakdown(seniority_measured=False, vector_discriminating=False)
    explanation = shortlist_entry_explanation(
        _entry(breakdown=breakdown, score_structured=0.65, score_evidence=0.0)
    )

    assert explanation.scores_available is True
    assert explanation.seniority_assessed is False
    assert explanation.vector_comparable is False
