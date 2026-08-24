"""RED pin — ROADMAP A6 siblings, READ path:
``src.services.explanation.shortlist_entry_explanation`` must copy the three
new markers FAITHFULLY off ``entry.score_breakdown``, never re-derive them.

Mirrors ``test_a6_sub_score_disclosure.py``'s structure exactly, for the
three markers added beside ``seniority_measured``/``vector_discriminating``:

    experience_bar_stated  -> ShortlistExplanation.experience_bar_stated
    education_bar_stated   -> ShortlistExplanation.education_bar_stated
    education_readable     -> ShortlistExplanation.education_readable

Deliberate deviation from ADR-041 (spec §4): the SAME field name is kept on
both sides (unlike ``seniority_measured`` -> ``seniority_assessed``) -- the
rename buys nothing and is one more place two copies can drift.
"""

from __future__ import annotations

import datetime as dt
from uuid import uuid4

from src.schemas.matching import EvidenceObject, ScoreBreakdown, ShortlistEntry
from src.services.explanation import shortlist_entry_explanation


def _breakdown(
    *,
    experience_bar_stated: bool | None = None,
    education_bar_stated: bool | None = None,
    education_readable: bool | None = None,
    experience: float = 0.5,
    education: float = 0.4,
) -> ScoreBreakdown:
    return ScoreBreakdown(
        skill=0.8,
        experience=experience,
        education=education,
        seniority=0.5,
        vector=0.4,
        structured=0.65,
        experience_bar_stated=experience_bar_stated,
        education_bar_stated=education_bar_stated,
        education_readable=education_readable,
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
            "generated_at": dt.datetime(2026, 8, 18, tzinfo=dt.UTC),
            "evidence_evaluated": None,
        }
    )


# ── copied faithfully, each of the three, each of True/False/None ──────────


def test_experience_bar_stated_true_is_copied() -> None:
    breakdown = _breakdown(experience_bar_stated=True)
    explanation = shortlist_entry_explanation(_entry(breakdown=breakdown))
    assert explanation.experience_bar_stated is True


def test_experience_bar_stated_false_is_copied() -> None:
    breakdown = _breakdown(experience_bar_stated=False)
    explanation = shortlist_entry_explanation(_entry(breakdown=breakdown))
    assert explanation.experience_bar_stated is False


def test_education_bar_stated_true_is_copied() -> None:
    breakdown = _breakdown(education_bar_stated=True)
    explanation = shortlist_entry_explanation(_entry(breakdown=breakdown))
    assert explanation.education_bar_stated is True


def test_education_bar_stated_false_is_copied() -> None:
    breakdown = _breakdown(education_bar_stated=False)
    explanation = shortlist_entry_explanation(_entry(breakdown=breakdown))
    assert explanation.education_bar_stated is False


def test_education_readable_true_is_copied() -> None:
    breakdown = _breakdown(education_readable=True)
    explanation = shortlist_entry_explanation(_entry(breakdown=breakdown))
    assert explanation.education_readable is True


def test_education_readable_false_is_copied() -> None:
    breakdown = _breakdown(education_readable=False)
    explanation = shortlist_entry_explanation(_entry(breakdown=breakdown))
    assert explanation.education_readable is False


def test_a_legacy_row_with_none_of_the_three_new_markers_makes_no_claim() -> None:
    """A row that predates all three markers must round-trip to ``None`` for
    every one of them -- the explanation must not assert either state."""
    breakdown = _breakdown(
        experience_bar_stated=None,
        education_bar_stated=None,
        education_readable=None,
    )
    explanation = shortlist_entry_explanation(_entry(breakdown=breakdown))

    assert explanation.experience_bar_stated is None
    assert explanation.education_bar_stated is None
    assert explanation.education_readable is None


# ── the non-re-derivation pin, carried through to the read path ────────────


def test_education_readable_false_survives_a_stored_education_score_of_one() -> None:
    """THE pin proving the read path COPIES rather than derives: a row whose
    ``education_readable`` is False (unreadable education, no JD bar stated
    -- the write-path fallback case) stores a stored ``education`` of
    ``1.0``. If ``shortlist_entry_explanation`` re-derived the marker from
    the score (e.g. ``education_readable = breakdown.education != 0.0``), a
    ``1.0`` would silently flip it back to True."""
    breakdown = _breakdown(
        education_bar_stated=False,
        education_readable=False,
        education=1.0,
    )
    explanation = shortlist_entry_explanation(_entry(breakdown=breakdown))

    assert explanation.education.score == 1.0, (
        "fixture drift -- the stored education score must be the genuine "
        "1.0 fallback value for this test to distinguish copy from derive"
    )
    assert explanation.education_readable is False, (
        "education_readable was re-derived from the score instead of "
        "copied from the stored marker -- a 1.0 education score flipped a "
        "recorded False back to an affirmative claim"
    )


def test_experience_bar_stated_false_survives_a_stored_experience_score_of_one() -> (
    None
):
    """Sibling pin for the experience marker: ``experience_bar_stated=False``
    (no bar stated) with a stored ``experience`` of ``1.0`` (the fallback
    value) must not be re-derived back to True."""
    breakdown = _breakdown(experience_bar_stated=False, experience=1.0)
    explanation = shortlist_entry_explanation(_entry(breakdown=breakdown))

    assert explanation.experience.score == 1.0
    assert explanation.experience_bar_stated is False


# ── independence from scores_available ──────────────────────────────────


def test_the_three_new_markers_are_independent_of_scores_available() -> None:
    breakdown = _breakdown(
        experience_bar_stated=False,
        education_bar_stated=False,
        education_readable=False,
    )
    explanation = shortlist_entry_explanation(
        _entry(breakdown=breakdown, score_structured=0.65, score_evidence=0.0)
    )

    assert explanation.scores_available is True
    assert explanation.experience_bar_stated is False
    assert explanation.education_bar_stated is False
    assert explanation.education_readable is False
