"""RED — Slice 1 of "Why this rank?" (deterministic score-composition +
verified-evidence panel). Pins the target shape of a NEW pure, display-only
function, ``src.services.explanation.shortlist_entry_explanation``, which
does not exist yet — every test below fails at collection with
``ModuleNotFoundError`` until the module is created.

Design choices this file locks in (the coder must implement exactly this,
not something merely compatible with it):

* Module home: ``src/services/explanation.py`` (new file) — a pure function,
  no DB, no LLM, no I/O, mirroring the "pure formatter" shape of
  ``shortlist_service._skill_summary`` / ``_evidence_coverage`` but as its
  own module since it is display-only and has no business being imported by
  the persistence/read path.
* Return shape: ``ShortlistExplanation``, a pydantic model with EXPLICIT
  named fields (not a generic label+list) for full ``mypy --strict``
  friendliness:
    - ``structured`` / ``evidence`` / ``motivation``: top-level
      ``ContributionRow`` (weight, score, contribution = weight*score).
    - ``skill`` / ``experience`` / ``education`` / ``seniority`` / ``vector``:
      the structured sub-score ``ContributionRow``s.
    - ``requirements``: list[RequirementRow] (requirement/status/evidence/
      confidence/evidence_chunk_ids), copied faithfully off
      ``entry.evidence.requirements`` — this function does NOT re-derive
      anti-fabrication verdicts, it only explains what is already on the DTO.
    - ``weights_available: bool`` — False exactly when
      ``entry.pipeline_meta is None``.

* THE HONESTY GUARD (explicit human decision, stated in the task and pinned
  here): weights come from ``entry.pipeline_meta.weights`` — the weights
  ACTUALLY USED to generate this score — never from
  ``src.schemas.matching.DEFAULT_WEIGHTS`` or any "current settings" value.
  Explaining a historical score with today's weights would be dishonest.

* THE FALLBACK (explicit human decision, stated in the task and pinned
  here): when ``entry.pipeline_meta is None`` (a legacy row written before
  this slice, or a blind row that never got one), every ``ContributionRow``
  carries ``weight=None`` and ``contribution=None`` — NEVER a value silently
  borrowed from ``DEFAULT_WEIGHTS``. ``score`` (the raw, un-weighted
  sub-score) is still shown, since that number came from the row itself and
  is not in question. The UI can then say "weights unavailable" instead of
  presenting a number that looks authoritative but is fabricated.

Every arithmetic assertion below is checked against an INDEPENDENTLY
hand-computed expected number (worked out by hand in this file's comments,
not re-derived from the same formula the implementation will use), so a
reviewer corrupting a single weight or sub-score in the implementation
cannot leave these tests green.
"""

from __future__ import annotations

import datetime as dt
from uuid import uuid4

import pytest

from src.schemas.matching import (
    DEFAULT_WEIGHTS,
    SCRUBBED_CONFIDENCE_CAP,
    EvidenceObject,
    MatchWeights,
    PipelineMeta,
    RequirementEvidence,
    ScoreBreakdown,
    ShortlistEntry,
)

# ── hand-computed fixture ────────────────────────────────────────────────
#
# weights: structured=0.5, evidence=0.4, motivation=0.1 (sums to 1.0)
#          skill=0.5, experience=0.2, education=0.1, seniority=0.1, vector=0.1
#          (sums to 1.0) — DELIBERATELY different from DEFAULT_WEIGHTS
#          (0.6/0.3/0.1 top; 0.40/0.25/0.10/0.15/0.10 sub) so the honesty
#          guard test has something to distinguish.
#
# structured sub-scores: skill=0.8, experience=0.6, education=0.4,
#                         seniority=0.5, vector=0.7
#   skill:      0.5 * 0.8 = 0.40
#   experience: 0.2 * 0.6 = 0.12
#   education:  0.1 * 0.4 = 0.04
#   seniority:  0.1 * 0.5 = 0.05
#   vector:     0.1 * 0.7 = 0.07
#   sum = 0.68  == score_breakdown.structured
#
# top-level: structured score=0.68, evidence score=0.75,
#            motivation score=0.20 (score_breakdown.motivation)
#   structured: 0.5 * 0.68 = 0.34
#   evidence:   0.4 * 0.75 = 0.30
#   motivation: 0.1 * 0.20 = 0.02
#   sum = 0.66  == score_final


def _custom_weights() -> MatchWeights:
    return MatchWeights(
        structured=0.5,
        evidence=0.4,
        motivation=0.1,
        skill=0.5,
        experience=0.2,
        education=0.1,
        seniority=0.1,
        vector=0.1,
    )


def _breakdown(*, motivation: float = 0.20) -> ScoreBreakdown:
    return ScoreBreakdown(
        skill=0.8,
        experience=0.6,
        education=0.4,
        seniority=0.5,
        vector=0.7,
        structured=0.68,
        motivation=motivation,
    )


def _requirement_rows() -> list[RequirementEvidence]:
    return [
        RequirementEvidence(
            requirement="Python",
            status="met",
            evidence="Built the payments service in Python.",
            evidence_chunk_ids=["c_001"],
            confidence=0.9,
        ),
        RequirementEvidence(
            requirement="Kubernetes",
            status="partial",
            evidence="Some exposure to Helm charts.",
            evidence_chunk_ids=["c_002"],
            confidence=0.5,
        ),
        # Demoted by verify_evidence (anti-fabrication scrub): the quote
        # failed fuzzy verification against the chunk it cited, so it was
        # blanked, status demoted met -> missing, and confidence capped at
        # SCRUBBED_CONFIDENCE_CAP. The panel must show this as UNVERIFIED,
        # never as met.
        RequirementEvidence(
            requirement="AWS certification",
            status="missing",
            evidence="",
            evidence_chunk_ids=["c_003"],
            confidence=SCRUBBED_CONFIDENCE_CAP,
        ),
    ]


def _evidence() -> EvidenceObject:
    return EvidenceObject(
        requirements=_requirement_rows(),
        overall_summary="Strong candidate overall.",
    )


def _meta(weights: MatchWeights) -> PipelineMeta:
    return PipelineMeta(
        model_gen="gpt-oss:20b",
        model_emb="nomic-embed-text",
        prompt_versions={"shortlist_evidence": "shortlist_evidence_v1"},
        weights=weights,
        git_sha="abc123def",
        generated_at=dt.datetime(2026, 7, 1, tzinfo=dt.UTC),
        timings_ms={},
    )


class _Unset:
    """Sentinel so ``evidence=None`` can mean "this entry genuinely has no
    evidence object" rather than "caller did not pass evidence"."""


_UNSET = _Unset()


def _entry(
    *,
    weights: MatchWeights | None,
    score_final: float = 0.66,
    score_structured: float | None = 0.68,
    score_evidence: float | None = 0.75,
    breakdown: ScoreBreakdown | None = None,
    evidence: EvidenceObject | None | _Unset = _UNSET,
) -> ShortlistEntry:
    return ShortlistEntry(
        id=uuid4(),
        job_id=uuid4(),
        resume_id=uuid4(),
        rank=1,
        score_final=score_final,
        score_structured=score_structured,
        score_evidence=score_evidence,
        score_breakdown=breakdown or _breakdown(),
        evidence=_evidence() if isinstance(evidence, _Unset) else evidence,
        pipeline_meta=_meta(weights) if weights is not None else None,
        generated_at=dt.datetime(2026, 7, 1, tzinfo=dt.UTC),
    )


# ── top-level contribution arithmetic ───────────────────────────────────


def test_top_level_contributions_sum_to_score_final() -> None:
    from src.services.explanation import shortlist_entry_explanation

    entry = _entry(weights=_custom_weights())
    explanation = shortlist_entry_explanation(entry)

    total = (
        explanation.structured.contribution
        + explanation.evidence.contribution
        + explanation.motivation.contribution
    )
    assert total == pytest.approx(entry.score_final, abs=1e-6)


def test_top_level_contribution_values_match_hand_computed_expectations() -> None:
    """Per-row check against the independently hand-computed numbers in the
    module docstring — mutation-provable: corrupting a single weight or
    sub-score breaks exactly one of these, not merely the aggregate sum."""
    from src.services.explanation import shortlist_entry_explanation

    entry = _entry(weights=_custom_weights())
    explanation = shortlist_entry_explanation(entry)

    assert explanation.structured.weight == pytest.approx(0.5)
    assert explanation.structured.score == pytest.approx(0.68)
    assert explanation.structured.contribution == pytest.approx(0.34, abs=1e-9)

    assert explanation.evidence.weight == pytest.approx(0.4)
    assert explanation.evidence.score == pytest.approx(0.75)
    assert explanation.evidence.contribution == pytest.approx(0.30, abs=1e-9)

    assert explanation.motivation.weight == pytest.approx(0.1)
    assert explanation.motivation.score == pytest.approx(0.20)
    assert explanation.motivation.contribution == pytest.approx(0.02, abs=1e-9)


# ── structured sub-contribution arithmetic ──────────────────────────────


def test_structured_sub_contributions_sum_to_structured_score() -> None:
    from src.services.explanation import shortlist_entry_explanation

    entry = _entry(weights=_custom_weights())
    explanation = shortlist_entry_explanation(entry)

    total = (
        explanation.skill.contribution
        + explanation.experience.contribution
        + explanation.education.contribution
        + explanation.seniority.contribution
        + explanation.vector.contribution
    )
    assert total == pytest.approx(entry.score_breakdown.structured, abs=1e-6)


def test_structured_sub_contribution_values_match_hand_computed_expectations() -> None:
    from src.services.explanation import shortlist_entry_explanation

    entry = _entry(weights=_custom_weights())
    explanation = shortlist_entry_explanation(entry)

    assert explanation.skill.weight == pytest.approx(0.5)
    assert explanation.skill.score == pytest.approx(0.8)
    assert explanation.skill.contribution == pytest.approx(0.40, abs=1e-9)

    assert explanation.experience.weight == pytest.approx(0.2)
    assert explanation.experience.contribution == pytest.approx(0.12, abs=1e-9)

    assert explanation.education.weight == pytest.approx(0.1)
    assert explanation.education.contribution == pytest.approx(0.04, abs=1e-9)

    assert explanation.seniority.weight == pytest.approx(0.1)
    assert explanation.seniority.contribution == pytest.approx(0.05, abs=1e-9)

    assert explanation.vector.weight == pytest.approx(0.1)
    assert explanation.vector.score == pytest.approx(0.7)
    assert explanation.vector.contribution == pytest.approx(0.07, abs=1e-9)


# ── the honesty guard ────────────────────────────────────────────────────


def test_explanation_uses_generation_time_weights_not_current_settings() -> None:
    """An entry whose pipeline_meta.weights differ from DEFAULT_WEIGHTS must
    be explained with ITS OWN weights, never today's defaults. This is the
    load-bearing guard: explaining a historical score with today's weights
    would be dishonest."""
    from src.services.explanation import shortlist_entry_explanation

    weights = _custom_weights()
    assert weights.structured != pytest.approx(DEFAULT_WEIGHTS.structured)
    assert weights.skill != pytest.approx(DEFAULT_WEIGHTS.skill)

    entry = _entry(weights=weights)
    explanation = shortlist_entry_explanation(entry)

    assert explanation.structured.weight == pytest.approx(weights.structured)
    assert explanation.structured.weight != pytest.approx(DEFAULT_WEIGHTS.structured)
    assert explanation.evidence.weight == pytest.approx(weights.evidence)
    assert explanation.motivation.weight == pytest.approx(weights.motivation)
    assert explanation.skill.weight == pytest.approx(weights.skill)
    assert explanation.skill.weight != pytest.approx(DEFAULT_WEIGHTS.skill)
    assert explanation.vector.weight == pytest.approx(weights.vector)


# ── the graceful fallback ────────────────────────────────────────────────


def test_explanation_without_pipeline_meta_does_not_fabricate_weights() -> None:
    """Legacy rows written before this slice (and any row that reaches this
    function without a pipeline_meta) MUST NOT have a weight silently
    borrowed from DEFAULT_WEIGHTS or any other "current" source — every
    weight/contribution comes back None, and weights_available is False, so
    the UI can say "weights unavailable" instead of lying."""
    from src.services.explanation import shortlist_entry_explanation

    entry = _entry(weights=None)
    assert entry.pipeline_meta is None

    explanation = shortlist_entry_explanation(entry)

    assert explanation.weights_available is False
    for row in (
        explanation.structured,
        explanation.evidence,
        explanation.motivation,
        explanation.skill,
        explanation.experience,
        explanation.education,
        explanation.seniority,
        explanation.vector,
    ):
        assert row.weight is None
        assert row.contribution is None
    # The raw sub-scores are NOT fabricated — they came from the row itself,
    # not from a weight — so they are still shown.
    assert explanation.skill.score == pytest.approx(0.8)
    assert explanation.structured.score == pytest.approx(entry.score_structured)


def test_explanation_with_pipeline_meta_has_weights_available_true() -> None:
    from src.services.explanation import shortlist_entry_explanation

    entry = _entry(weights=_custom_weights())
    explanation = shortlist_entry_explanation(entry)
    assert explanation.weights_available is True


# ── per-requirement fidelity ─────────────────────────────────────────────


def test_requirement_rows_carry_status_quote_confidence_and_chunk_ids_faithfully() -> (
    None
):
    from src.services.explanation import shortlist_entry_explanation

    entry = _entry(weights=_custom_weights())
    explanation = shortlist_entry_explanation(entry)

    assert len(explanation.requirements) == 3
    met = next(r for r in explanation.requirements if r.requirement == "Python")
    assert met.status == "met"
    assert met.evidence == "Built the payments service in Python."
    assert met.confidence == pytest.approx(0.9)
    assert met.evidence_chunk_ids == ["c_001"]

    partial = next(r for r in explanation.requirements if r.requirement == "Kubernetes")
    assert partial.status == "partial"
    assert partial.confidence == pytest.approx(0.5)


def test_demoted_requirement_never_shown_as_met() -> None:
    """The blanked-quote requirement (anti-fabrication scrub) must never
    surface as "met" — that would be exactly the fabricated-confidence
    failure mode this whole feature exists to prevent."""
    from src.services.explanation import shortlist_entry_explanation

    entry = _entry(weights=_custom_weights())
    explanation = shortlist_entry_explanation(entry)

    demoted = next(
        r for r in explanation.requirements if r.requirement == "AWS certification"
    )
    assert demoted.status == "missing"
    assert demoted.status != "met"
    assert demoted.evidence == ""
    assert demoted.confidence <= SCRUBBED_CONFIDENCE_CAP + 1e-9


def test_explanation_with_no_evidence_returns_empty_requirements() -> None:
    from src.services.explanation import shortlist_entry_explanation

    entry = _entry(weights=_custom_weights(), evidence=None)
    explanation = shortlist_entry_explanation(entry)
    assert explanation.requirements == []


# ── purity ────────────────────────────────────────────────────────────────


def test_explanation_is_pure_and_deterministic() -> None:
    """No DB, no LLM, no I/O, no hidden randomness/clock — calling twice on
    the same entry must yield an identical result."""
    from src.services.explanation import shortlist_entry_explanation

    entry = _entry(weights=_custom_weights())
    first = shortlist_entry_explanation(entry)
    second = shortlist_entry_explanation(entry)
    assert first.model_dump() == second.model_dump()


# ── unrecorded sub-scores: "not recorded", never an affirmative 0% ────────


def test_unrecorded_subscores_are_none_not_an_affirmative_zero() -> None:
    """A fold-less row (a pre-4d row, or one whose folded sub-scores were
    unreadable) must surface as "not recorded", NOT as ``0.0``.

    ``0.0`` renders as an affirmative "0% contribution" -- a POSITIVE FALSE
    CLAIM about a candidate, and asymmetric with the ``pipeline_meta=None``
    handling in the very same object, which already refuses to state a weight
    it does not know. Both unavailability stories must read the same way."""
    from src.services.explanation import shortlist_entry_explanation

    entry = _entry(
        weights=_custom_weights(), score_structured=None, score_evidence=None
    )
    explanation = shortlist_entry_explanation(entry)

    assert explanation.structured.score is None
    assert explanation.structured.contribution is None
    assert explanation.evidence.score is None
    assert explanation.evidence.contribution is None
    assert explanation.scores_available is False
    # The WEIGHTS are still known and still honest to show -- this fallback is
    # independent of the weights fallback, not a merged "everything unknown".
    assert explanation.weights_available is True
    assert explanation.structured.weight == pytest.approx(0.5)
    assert explanation.evidence.weight == pytest.approx(0.4)
    # Sub-scores that DID come off the row are unaffected.
    assert explanation.skill.score == pytest.approx(0.8)
    assert explanation.skill.contribution == pytest.approx(0.40, abs=1e-9)


def test_recorded_subscores_report_scores_available() -> None:
    """Positive control for the flag above: an ordinary row must NOT be
    flagged as missing sub-scores."""
    from src.services.explanation import shortlist_entry_explanation

    explanation = shortlist_entry_explanation(_entry(weights=_custom_weights()))

    assert explanation.scores_available is True
    assert explanation.structured.score == pytest.approx(0.68)


def test_unrecorded_subscore_and_missing_weights_compose() -> None:
    """Both fallbacks at once (a genuinely legacy row): no weight, no score,
    no contribution -- and still no substituted default anywhere."""
    from src.services.explanation import shortlist_entry_explanation

    entry = _entry(weights=None, score_structured=None, score_evidence=None)
    explanation = shortlist_entry_explanation(entry)

    assert explanation.weights_available is False
    assert explanation.scores_available is False
    assert explanation.structured.weight is None
    assert explanation.structured.score is None
    assert explanation.structured.contribution is None
