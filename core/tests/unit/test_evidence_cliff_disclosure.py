"""RED pin — ROADMAP A4's evidence cliff: a never-computed evidence score must
not render as an affirmative zero.

**The defect.** ``evidence_k = 15``, but **all** of ``candidates_s2`` goes to
``stage4_combine``. A candidate ranked 16th is never submitted to stage 3, so
``_evidence_completeness`` maps their absent evidence to ``0.0`` and
``_motivation_score`` likewise — 40% of ``score_final`` — **from compute
placement, not from merit**. ``shortlist_top_percent`` defaults to 100, so this
is live on any job with more than 15 recalled candidates.

Rank order is provably unaffected (``0.6·s_i + (≥0) ≥ 0.6·s_j``), which is why
this is a *disclosure* defect rather than a ranking one. The harm is on screen:
``stage4_combine`` produces a **real ``0.0`` float**, so ``explanation.py``
sets ``scores_available=True`` and the panel renders

    Evidence · 30% · 0% · 0.00

**affirmatively** — indistinguishable from a candidate whose evidence *was*
evaluated and found to support nothing. On a page whose entire purpose is
defending a ranking decision, that is a positive false claim about a person.

**ADR-031's "not recorded" guard does not cover this.** That guard protects an
*unreadable* row — one whose stored sub-score could not be parsed. This is a
*never-computed* one, which parses perfectly well as `0.0`. Different fact,
same rendering, and only one of them is honest.

**Both directions are pinned here, deliberately.** ADR-031 records that the fix
for "an unrecorded sub-score must not render 0%" *introduced* the mirror-image
mutant — a genuine ``0.0`` rendering as "not recorded" — because
``_motivation_score`` returns ``0.0`` for every candidate with no cover letter,
so real zeros are the common case rather than an edge case. The same trap
applies here: a candidate who WAS evaluated and genuinely met nothing must
still render ``0``, never "not assessed".

**Do not infer this from ``requirements == []`` in the template.** That is
inferring pipeline state from a display artifact — a candidate evaluated
against a JD with no requirements also has an empty list. The marker is
persisted on the write path, where the fact is actually known.
"""

from __future__ import annotations

import datetime as dt
from uuid import uuid4

from src.schemas.matching import EvidenceObject, ScoreBreakdown, ShortlistEntry
from src.services.explanation import shortlist_entry_explanation


def _breakdown() -> ScoreBreakdown:
    return ScoreBreakdown(
        skill=0.8,
        experience=0.7,
        education=0.6,
        seniority=0.5,
        vector=0.4,
        structured=0.65,
    )


def _entry(
    *,
    evidence_evaluated: bool | None,
    score_evidence: float | None = 0.0,
) -> ShortlistEntry:
    return ShortlistEntry.model_validate(
        {
            "id": uuid4(),
            "job_id": uuid4(),
            "resume_id": uuid4(),
            "rank": 16,
            "score_final": 0.39,
            "score_structured": 0.65,
            "score_evidence": score_evidence,
            "score_breakdown": _breakdown(),
            "evidence": EvidenceObject(),
            # None exercises the "weights unavailable" path, which is
            # orthogonal to what this file pins and keeps the fixture minimal.
            "pipeline_meta": None,
            "generated_at": dt.datetime(2026, 8, 13, tzinfo=dt.UTC),
            "evidence_evaluated": evidence_evaluated,
        }
    )


def test_a_past_the_cliff_candidate_is_marked_not_assessed() -> None:
    """THE pin. Stage 3 never ran for this candidate, so the panel must not
    state a measured zero."""
    explanation = shortlist_entry_explanation(_entry(evidence_evaluated=False))

    assert explanation.evidence_assessed is False, (
        "a candidate past the evidence cliff is being presented as having an "
        "evaluated evidence score of 0 — a positive false claim about a person "
        "on the page that defends the ranking"
    )


def test_an_evaluated_candidate_with_a_genuine_zero_still_reports_zero() -> None:
    """The mirror-image mutant ADR-031 records: the fix for "don't show a
    fabricated 0" must not start hiding REAL zeros.

    A candidate who was evaluated and met nothing has a true, defensible score
    of 0. Rendering that as "not assessed" would be the same lie pointing the
    other way — and this is the common case, not an edge case.
    """
    explanation = shortlist_entry_explanation(_entry(evidence_evaluated=True))

    assert explanation.evidence_assessed is True
    assert explanation.evidence.score == 0.0, (
        "a genuinely-evaluated zero must survive as 0 — hiding it would be the "
        "mirror image of the defect being fixed"
    )


def test_a_legacy_row_that_never_recorded_the_marker_is_not_claimed_either_way() -> (
    None
):
    """``None`` means the row predates the marker. We do not know whether stage
    3 ran for it, so the panel must not assert that it did.

    This follows ADR-031's ``pipeline_meta=None`` -> "weights unavailable"
    discipline exactly: refuse to state what the row does not support, rather
    than substituting the convenient default in either direction.
    """
    explanation = shortlist_entry_explanation(_entry(evidence_evaluated=None))

    assert explanation.evidence_assessed is None, (
        "a legacy row's unknown assessment state must stay unknown — claiming "
        "either 'assessed' or 'not assessed' invents a fact"
    )


def test_the_marker_is_independent_of_the_score_being_recorded() -> None:
    """``evidence_assessed`` answers "did stage 3 run", ``scores_available``
    answers "did the row store a number". They are different questions and must
    not be collapsed: a past-the-cliff row records a perfectly readable 0.0, so
    ``scores_available`` is True while ``evidence_assessed`` is False.

    Collapsing them is the obvious shortcut and would silently re-open the
    defect for exactly the rows it was built for.
    """
    past_cliff = shortlist_entry_explanation(_entry(evidence_evaluated=False))
    assert past_cliff.scores_available is True
    assert past_cliff.evidence_assessed is False

    unreadable = shortlist_entry_explanation(
        _entry(evidence_evaluated=True, score_evidence=None)
    )
    assert unreadable.scores_available is False
    assert unreadable.evidence_assessed is True
