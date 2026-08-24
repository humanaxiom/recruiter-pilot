"""Why this rank? — the deterministic score-composition explanation for one
already-ranked, already-persisted shortlist entry.

PURE AND DISPLAY-ONLY. No DB, no LLM, no I/O, no clock, no randomness, and no
import from ``src.pipeline`` — nothing here computes or re-computes a score.
Every number this module emits is either read verbatim off the DTO or is the
product of two numbers read verbatim off the DTO. The ranking math lives in
``src/pipeline/matching/`` and is untouched by this file; if the two ever
disagree, this file is wrong.

It lives in its own module rather than in ``shortlist_service`` (whose
``_skill_summary`` / ``_evidence_coverage`` it otherwise resembles) precisely
because it is display-only: the persistence/read path has no business
importing it, and it has no business importing them.

THE HONESTY GUARD. The weights come from ``entry.pipeline_meta.weights`` — the
weights ACTUALLY IN FORCE when this row was generated — never from
``DEFAULT_WEIGHTS`` or any "current settings" value. Weights are tunable, so
today's defaults may differ from the ones that produced a shortlist ranked
last month; explaining a historical score with today's weights would present a
fabricated arithmetic as an audit trail.

THE FALLBACK. When ``entry.pipeline_meta`` is ``None`` (a legacy row, or one
whose reproducibility stamp could not be read back) there is no honest weight
to show, so every ``ContributionRow`` carries ``weight=None`` and
``contribution=None`` and ``weights_available`` is ``False``. The raw
sub-SCORES are still shown — those came off the row itself and are not in
question. The UI says "weights unavailable" instead of rendering a number that
looks authoritative but was invented.

THE SECOND FALLBACK, told the same way on purpose. When the ROW never recorded
a composed sub-score (``entry.score_structured`` / ``entry.score_evidence`` is
``None`` — a pre-4d row, or one whose folded value was unreadable), that row's
``score`` AND ``contribution`` are ``None`` and ``scores_available`` is
``False``. It is deliberately NOT reported as ``0.0``: "this candidate scored
0% on structured" is a positive false claim, whereas "not recorded" is true.
An absent number and a zero are different statements and the panel makes only
the one it can support — the same rule as the weights fallback above.

THE PII BOUNDARY. This function consumes an ALREADY-REDACTED
``ShortlistEntry``. Under blind review the redaction has already happened
inside ``shortlist_service._row_to_blind_entry``, BEFORE the DTO was built
(ADR-006 §4). Nothing here may open a connection, re-read ``resumes.parsed``,
or otherwise reach around the DTO for raw text — doing so would re-introduce
exactly the identity leak that boundary exists to prevent.

ANTI-FABRICATION. The per-requirement rows are copied FAITHFULLY off
``entry.evidence.requirements``; this module never re-derives a verdict. A
requirement whose quote failed ``verify_evidence``'s fuzzy match was already
blanked, demoted (``met`` -> ``missing``) and confidence-capped at
``SCRUBBED_CONFIDENCE_CAP`` at scoring time, so copying faithfully is what
guarantees it can never resurface here as "met".
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from src.schemas.matching import (
    EvidenceStatus,
    MatchWeights,
    RequirementEvidence,
    ShortlistEntry,
)


class ContributionRow(BaseModel):
    """One line of the score-composition table: ``weight x score =
    contribution``.

    ``weight``/``contribution`` are ``None`` — never a borrowed default —
    when the entry carries no generation-time weights (see THE FALLBACK).
    ``score`` is ``None`` when the ROW never recorded that sub-score (see THE
    SECOND FALLBACK), which likewise zeroes out ``contribution``: there is no
    honest product of a known weight and an unknown score."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    weight: float | None = None
    score: float | None = None
    contribution: float | None = None


class RequirementRow(BaseModel):
    """One requirement of the verified-evidence panel, copied verbatim off the
    DTO's ``EvidenceObject`` — no verdict is re-derived here."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    requirement: str
    status: EvidenceStatus
    evidence: str
    confidence: float
    evidence_chunk_ids: list[str] = Field(default_factory=list)
    source_context: str | None = None


class ShortlistExplanation(BaseModel):
    """The full "Why this rank?" payload for one entry.

    Explicit named fields rather than a generic label+list so ``mypy --strict``
    (and the template) can see the shape."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # Top level: 0.6*structured + 0.3*evidence + 0.1*motivation, at the
    # weights this row was generated with.
    structured: ContributionRow
    evidence: ContributionRow
    motivation: ContributionRow
    # The structured sub-scores, which compose score_breakdown.structured.
    skill: ContributionRow
    experience: ContributionRow
    education: ContributionRow
    seniority: ContributionRow
    vector: ContributionRow
    requirements: list[RequirementRow] = Field(default_factory=list)
    weights_available: bool = False
    # False when the row never recorded the two COMPOSED sub-scores
    # (``score_structured``/``score_evidence``). Sibling of
    # ``weights_available`` and computed here for the same reason: the UI must
    # not have to re-derive "is this number available", or the two copies of
    # that rule drift.
    scores_available: bool = False
    # ROADMAP A4 (evidence cliff). Did stage 3 actually run for this candidate?
    # ``None`` = the row never recorded it (legacy), so we assert neither.
    #
    # A DIFFERENT QUESTION from ``scores_available``, and collapsing the two is
    # the obvious shortcut that silently re-opens the defect: a past-the-cliff
    # row stores a perfectly readable ``0.0``, so ``scores_available`` is True
    # while this is False. One asks "did the row store a number", the other
    # asks "does that number mean anything".
    evidence_assessed: bool | None = None
    # ROADMAP A6 (D1/D2). Two more different-question markers, same pattern
    # as ``evidence_assessed`` immediately above: a past-the-fallback row
    # stores a perfectly readable ``0.0``/``1.0``, so ``scores_available`` can
    # be True while these are False. Copied FAITHFULLY off
    # ``score_breakdown``'s own markers below -- never re-derived from the
    # score value itself.
    seniority_assessed: bool | None = None
    vector_comparable: bool | None = None
    # ROADMAP A6 siblings (docs/adr/041-sub-score-measurement-markers.md
    # addendum). Same different-question pattern as the pair above, one
    # dimension over each. Deliberately kept under the SAME name as
    # ``score_breakdown``'s own field (unlike the seniority/vector pair's
    # rename) -- a rename buys nothing here and is one more place the two
    # copies could drift.
    experience_bar_stated: bool | None = None
    education_bar_stated: bool | None = None
    education_readable: bool | None = None


def _row(score: float | None, weight: float | None) -> ContributionRow:
    """One table line. ``weight is None`` (no generation-time stamp) or
    ``score is None`` (the row never recorded that sub-score) both mean no
    contribution can be stated honestly, so none is."""
    return ContributionRow(
        weight=weight,
        score=score,
        contribution=None if weight is None or score is None else weight * score,
    )


def shortlist_entry_explanation(entry: ShortlistEntry) -> ShortlistExplanation:
    """Explain ``entry``'s final score as a deterministic contribution table
    plus its per-requirement evidence, using the weights that GENERATED it."""
    weights: MatchWeights | None = (
        entry.pipeline_meta.weights if entry.pipeline_meta is not None else None
    )
    # Read once, explicitly, so the "no stamp -> no weight" rule is visible on
    # every single row rather than hidden behind a lookup helper.
    w_structured = weights.structured if weights is not None else None
    w_evidence = weights.evidence if weights is not None else None
    w_motivation = weights.motivation if weights is not None else None
    w_skill = weights.skill if weights is not None else None
    w_experience = weights.experience if weights is not None else None
    w_education = weights.education if weights is not None else None
    w_seniority = weights.seniority if weights is not None else None
    w_vector = weights.vector if weights is not None else None

    breakdown = entry.score_breakdown
    reqs: list[RequirementEvidence] = (
        entry.evidence.requirements if entry.evidence is not None else []
    )
    return ShortlistExplanation(
        structured=_row(entry.score_structured, w_structured),
        evidence=_row(entry.score_evidence, w_evidence),
        motivation=_row(breakdown.motivation, w_motivation),
        skill=_row(breakdown.skill, w_skill),
        experience=_row(breakdown.experience, w_experience),
        education=_row(breakdown.education, w_education),
        seniority=_row(breakdown.seniority, w_seniority),
        vector=_row(breakdown.vector, w_vector),
        requirements=[
            RequirementRow(
                requirement=r.requirement,
                status=r.status,
                evidence=r.evidence,
                confidence=r.confidence,
                evidence_chunk_ids=list(r.evidence_chunk_ids),
                source_context=r.source_context,
            )
            for r in reqs
        ],
        weights_available=weights is not None,
        scores_available=(
            entry.score_structured is not None and entry.score_evidence is not None
        ),
        # Copied faithfully off the stored row, never re-derived — the same
        # rule ADR-031 §4 applies to the anti-fabrication verdicts. The write
        # path is the only place that knows whether stage 3 saw this candidate.
        evidence_assessed=entry.evidence_evaluated,
        # Copied faithfully off ``score_breakdown``'s own markers (ADR-040 §5
        # discipline extended to the two ROADMAP A6 markers) — the write path
        # is the only place that knows whether either comparison actually ran.
        seniority_assessed=breakdown.seniority_measured,
        vector_comparable=breakdown.vector_discriminating,
        # Copied faithfully off ``score_breakdown``'s own markers, same
        # discipline, same names both sides (spec deviation from the
        # seniority/vector rename, recorded in the ADR-041 addendum).
        experience_bar_stated=breakdown.experience_bar_stated,
        education_bar_stated=breakdown.education_bar_stated,
        education_readable=breakdown.education_readable,
    )


__all__ = [
    "ContributionRow",
    "RequirementRow",
    "ShortlistExplanation",
    "shortlist_entry_explanation",
]
