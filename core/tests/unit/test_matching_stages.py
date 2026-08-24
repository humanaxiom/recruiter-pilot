"""Unit tests for the Phase 4c matching **stages** — pure scoring functions
ported from hris ``packages/pipeline/src/pipeline/matching/stages.py``.

RED half of the TDD cycle: ``src/pipeline/matching/stages.py`` (and the
``src.pipeline.matching`` package itself) does not exist yet, so every test
below fails at collection time with ``ModuleNotFoundError``. Once 4c lands
``stages.py``, these pin the intended, CORRECTED behavior — not a literal
copy of hris's known-buggy behavior — for the blockers recorded in the 4c
resume-point handoff:

* blocker #1 — ``score_skill_breakdown``'s must-have-miss detection must key
  off a row's ``ontology_weight == 0`` (genuinely no ontology/family credit
  at all), NOT ``score == 0.0`` (which a present-but-insufficient-tenure row
  can also hit, wrongly labelling it a miss).
* blocker #6 (carry-forward from Phase 4a round 5's F1 finding, "hris's own
  shipped ``_fuzz_substring`` verifies all four fabricated anchors") —
  ``verify_evidence`` must use a rapidfuzz metric that REJECTS every
  fabricated quote in ``tests/evals/fixtures/labels.json``'s
  ``negative_evidence`` while ACCEPTING every ``gold_evidence`` anchor; the
  landmine test below documents, with real fixture text, exactly why
  ``fuzz.WRatio``/``fuzz.ratio`` are the wrong choice and
  ``partial_ratio``/``token_set_ratio`` are required.

Also covers the remaining pure functions plainly, against the read hris
source: ``is_senior_candidate`` (a YEARS-based boolean gate for the
implied-experience relief — NOT the cosine-title ``seniority`` sub-score,
which orchestrator.py computes separately with a live embedder and is
therefore out of scope for this pure-function file), ``score_experience``,
``score_education`` (degree LEVEL, now extended to fold in
``jd.education.fields`` fuzzy field-of-study relevance — ADR-009 §7's open
decision is RESOLVED by ADR-028: a qualifying-level degree in a non-allowed
field is capped at ``education_partial``), ``normalise_vector_scores``,
``stage4_combine``, ``_evidence_completeness``, ``_motivation_score``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from src.pipeline.matching.stages import (
    _collapse_whitespace,
    _CombineInput,
    _evidence_completeness,
    _fuzz_ratio,
    _motivation_score,
    _SkillRowFromCypher,
    is_senior_candidate,
    normalise_vector_scores,
    score_education,
    score_experience,
    score_skill_breakdown,
    stage4_combine,
    verify_evidence,
)
from src.schemas.matching import (
    DEFAULT_WEIGHTS,
    CoverLetterEvidence,
    EvidenceObject,
    MatchWeights,
    RequirementEvidence,
    ScoreBreakdown,
    _strip_control_chars,
)

# ── Evals-corpus fixture loading (read-only — mirrors test_evals_corpus.py) ──

_EVALS_DIR = Path(__file__).resolve().parent.parent / "evals"
_FIXTURES_DIR = _EVALS_DIR / "fixtures"
_LABELS_PATH = _FIXTURES_DIR / "labels.json"


def _load_labels() -> dict[str, Any]:
    with _LABELS_PATH.open(encoding="utf-8") as fh:
        result: dict[str, Any] = json.load(fh)
        return result


def _load_resume_raw(resume_id: str) -> dict[str, Any]:
    labels = _load_labels()
    fixture = labels["resumes"][resume_id]["fixture"]
    with (_FIXTURES_DIR / fixture).open(encoding="utf-8") as fh:
        result: dict[str, Any] = json.load(fh)
        return result


def _chunks_by_id(resume_raw: dict[str, Any]) -> dict[str, str]:
    return {c["id"]: c["text"] for c in resume_raw["chunks"]}


def _skill_chunk_id(resume_raw: dict[str, Any], skill_name: str) -> str:
    for skill in resume_raw["skills"]:
        if skill["name"] == skill_name:
            ids = skill["evidence_chunk_ids"]
            assert ids, f"{skill_name} has no evidence_chunk_ids in fixture"
            return str(ids[0])
    raise AssertionError(f"skill {skill_name!r} not found in resume fixture")


_LABELS = _load_labels()

_NEGATIVE_EVIDENCE_CASES: list[tuple[str, str, str]] = [
    (resume_id, chunk_id, fabricated_quote)
    for resume_id, entry in _LABELS["resumes"].items()
    for chunk_id, fabricated_quote in entry.get("negative_evidence", {}).items()
]

_GOLD_EVIDENCE_CASES: list[tuple[str, str, str]] = [
    (resume_id, skill_name, quote)
    for resume_id, entry in _LABELS["resumes"].items()
    for skill_name, quote in entry.get("gold_evidence", {}).items()
]


def test_labels_json_has_exactly_four_negative_evidence_fabrications() -> None:
    """Pins labels.json's falsifiability invariant (see its ``_comment``):
    four fabricated quotes across r01/r02/r09 must all fail verification."""
    assert len(_NEGATIVE_EVIDENCE_CASES) == 4


def test_labels_json_has_gold_evidence_anchors_to_verify() -> None:
    assert len(_GOLD_EVIDENCE_CASES) >= 2


# ── is_senior_candidate: years-based boolean gate for implied-experience ────
# (NOT the cosine-title `seniority` sub-score — that's computed with a live
# embedder in orchestrator.py and is out of scope for this pure-function file.)


@pytest.mark.parametrize(
    "total_years, jd_min_years, expected",
    [
        (None, 5, False),
        (10, None, False),
        (10, 0, False),  # falsy jd_min_years
        (0, 5, False),  # falsy total_years
        (7, 5, False),  # 7 < 5 * 1.5 = 7.5 -> just below the relief threshold
        (8, 5, True),  # 8 >= 7.5
        (6, 4, True),  # boundary: 6 >= 4 * 1.5 = 6.0 exactly (>=)
        (5, 4, False),  # boundary: 5 < 6.0
    ],
)
def test_is_senior_candidate(
    total_years: int | None, jd_min_years: int | None, expected: bool
) -> None:
    assert (
        is_senior_candidate(total_years, jd_min_years, weights=DEFAULT_WEIGHTS)
        is expected
    )


# ── score_skill_breakdown: general behavior ─────────────────────────────────


def _row(**overrides: Any) -> _SkillRowFromCypher:
    base: dict[str, Any] = {
        "skill": "Python",
        "req_years": 3,
        "is_must_have": False,
        "years": None,
        "last_used_year": 2026,
        "ontology_weight": 1.0,
    }
    base.update(overrides)
    return _SkillRowFromCypher(**base)


def test_score_skill_breakdown_empty_rows_returns_zero_and_empty_list() -> None:
    overall, contributions = score_skill_breakdown(
        [], weights=DEFAULT_WEIGHTS, today_year=2026
    )
    assert overall == 0.0
    assert contributions == []


def test_score_skill_breakdown_unknown_years_gets_full_years_credit() -> None:
    """Résumé extraction frequently omits per-skill years; treating that as
    zero would unfairly zero the sub-score for almost every candidate."""
    row = _row(years=None, ontology_weight=1.0, last_used_year=2026)
    overall, contributions = score_skill_breakdown(
        [row], weights=DEFAULT_WEIGHTS, today_year=2026
    )
    [contrib] = contributions
    assert contrib.score == 1.0
    assert contrib.years is None
    assert overall == 1.0


@pytest.mark.parametrize(
    "last_used_year, expected_recency",
    [
        (2026, 1.0),  # delta 0 <= recency_recent_years (2)
        (2024, 1.0),  # delta 2
        (2023, 0.7),  # delta 3 <= recency_mid_years (5)
        (2021, 0.7),  # delta 5
        (2020, 0.4),  # delta 6 > recency_mid_years
    ],
)
def test_score_skill_breakdown_recency_buckets(
    last_used_year: int, expected_recency: float
) -> None:
    row = _row(years=None, ontology_weight=1.0, last_used_year=last_used_year)
    _, contributions = score_skill_breakdown(
        [row], weights=DEFAULT_WEIGHTS, today_year=2026
    )
    [contrib] = contributions
    assert contrib.recency == expected_recency
    assert contrib.score == pytest.approx(expected_recency)


def test_score_skill_breakdown_reason_is_ontology_fallback_when_indirect() -> None:
    row = _row(ontology_weight=0.6, last_used_year=2026)
    _, contributions = score_skill_breakdown(
        [row], weights=DEFAULT_WEIGHTS, today_year=2026
    )
    [contrib] = contributions
    assert contrib.reason == "ontology-fallback"


def test_score_skill_breakdown_reason_is_none_when_direct_match() -> None:
    row = _row(ontology_weight=1.0, last_used_year=2026)
    _, contributions = score_skill_breakdown(
        [row], weights=DEFAULT_WEIGHTS, today_year=2026
    )
    [contrib] = contributions
    assert contrib.reason is None


def test_score_skill_breakdown_years_score_caps_at_req_years() -> None:
    row = _row(years=10, req_years=5, ontology_weight=1.0, last_used_year=2026)
    _, contributions = score_skill_breakdown(
        [row], weights=DEFAULT_WEIGHTS, today_year=2026
    )
    [contrib] = contributions
    assert contrib.score == 1.0  # min(1.0, 10/5) capped, not 2.0


# ── blocker #1: missing_must keys off ontology_weight == 0, NOT score==0.0 ──


def test_family_credited_present_must_have_alone_is_not_penalized() -> None:
    """``ontology_weight=0.5`` means the candidate DOES hold this must-have
    via an ontology/family match — it is present, not missing — so as the
    sole must-have it must not trigger ``must_have_miss_penalty``."""
    row = _row(
        skill="Kubernetes",
        is_must_have=True,
        ontology_weight=0.5,
        years=None,
        last_used_year=2026,
    )
    overall, contributions = score_skill_breakdown(
        [row], weights=DEFAULT_WEIGHTS, today_year=2026
    )
    [contrib] = contributions
    assert contrib.score == 0.5
    assert overall == 0.5  # NOT halved


def test_missing_must_have_is_not_masked_by_a_present_family_credited_sibling() -> None:
    """A must-have that is genuinely missing (``ontology_weight=0``) must
    still trigger ``must_have_miss_penalty`` even when scored alongside
    ANOTHER must-have that is present via family credit
    (``ontology_weight=0.5``, ``score=0.5``) — the nonzero sibling must not
    mask the real miss."""
    missing = _row(
        skill="Terraform",
        is_must_have=True,
        ontology_weight=0.0,
        years=None,
        last_used_year=None,
    )
    present = _row(
        skill="Kubernetes",
        is_must_have=True,
        ontology_weight=0.5,
        years=None,
        last_used_year=2026,
    )
    overall, contributions = score_skill_breakdown(
        [missing, present], weights=DEFAULT_WEIGHTS, today_year=2026
    )
    assert {c.skill: c.score for c in contributions} == {
        "Terraform": 0.0,
        "Kubernetes": 0.5,
    }
    # (0.0 + 0.5) / 2 = 0.25, halved by must_have_miss_penalty (0.5) -> 0.125
    assert overall == pytest.approx(0.125)


def test_truly_missing_must_have_fires_penalty() -> None:
    missing = _row(
        skill="Terraform",
        is_must_have=True,
        ontology_weight=0.0,
        years=None,
        last_used_year=None,
    )
    held = _row(
        skill="Python",
        is_must_have=True,
        ontology_weight=1.0,
        years=None,
        last_used_year=2026,
    )
    overall, _ = score_skill_breakdown(
        [missing, held], weights=DEFAULT_WEIGHTS, today_year=2026
    )
    # (0.0 + 1.0) / 2 = 0.5, halved by must_have_miss_penalty (0.5) -> 0.25
    assert overall == pytest.approx(0.25)


def test_fully_held_must_have_does_not_fire_penalty() -> None:
    row = _row(
        skill="Python",
        is_must_have=True,
        ontology_weight=1.0,
        years=None,
        last_used_year=2026,
    )
    overall, _ = score_skill_breakdown([row], weights=DEFAULT_WEIGHTS, today_year=2026)
    assert overall == 1.0


def test_missing_must_keys_off_ontology_weight_not_score_mutant() -> None:
    """The must-have-miss filter must key off ``ontology_weight == 0`` (the
    row genuinely lacks any ontology/family credit), NOT ``score == 0.0``.

    A present-but-insufficient-tenure row (``ontology_weight=0.5`` — the
    candidate DOES hold the skill via family credit — but ``years=0``
    against a positive ``req_years`` zeroes its numeric score) must NOT be
    treated as missing. A mutant that keys the filter off ``score == 0.0``
    instead would wrongly classify this row as a miss and apply
    ``must_have_miss_penalty``, producing 0.25 instead of the correct 0.5 —
    this assertion is sharp enough to kill that mutant.
    """
    held = _row(
        skill="Python",
        is_must_have=True,
        ontology_weight=1.0,
        years=None,
        last_used_year=2026,
    )
    present_zero_score = _row(
        skill="Docker",
        is_must_have=True,
        ontology_weight=0.5,
        years=0,
        req_years=5,
        last_used_year=2026,
    )
    overall, contributions = score_skill_breakdown(
        [held, present_zero_score], weights=DEFAULT_WEIGHTS, today_year=2026
    )
    scores = {c.skill: c.score for c in contributions}
    assert scores == {"Python": 1.0, "Docker": 0.0}
    # (1.0 + 0.0) / 2 = 0.5. Neither row has ontology_weight == 0, so no
    # penalty should apply. A score==0.0-keyed mutant would (wrongly) apply
    # the 0.5x must_have_miss_penalty here, producing 0.25 instead.
    assert overall == pytest.approx(0.5)


def test_senior_implied_experience_relief_softens_penalty_and_flags_reason() -> None:
    missing = _row(
        skill="Terraform",
        is_must_have=True,
        ontology_weight=0.0,
        years=None,
        last_used_year=None,
    )
    held = _row(
        skill="Python",
        is_must_have=True,
        ontology_weight=1.0,
        years=None,
        last_used_year=2026,
    )
    overall, contributions = score_skill_breakdown(
        [missing, held], weights=DEFAULT_WEIGHTS, senior=True, today_year=2026
    )
    # matched_coverage = 1 - 1/2 = 0.5 >= implied_min_coverage (0.5) -> relief.
    # (0.0 + 1.0) / 2 = 0.5, softened by implied_experience_relief (0.75) -> 0.375
    assert overall == pytest.approx(0.375)
    missing_contrib = next(c for c in contributions if c.skill == "Terraform")
    assert missing_contrib.reason == "implied-experience"


# ── score_experience ─────────────────────────────────────────────────────────


def test_score_experience_no_min_years_is_perfect() -> None:
    assert score_experience(3, None, weights=DEFAULT_WEIGHTS) == 1.0
    assert score_experience(None, None, weights=DEFAULT_WEIGHTS) == 1.0


def test_score_experience_none_total_years_treated_as_zero() -> None:
    assert score_experience(None, 5, weights=DEFAULT_WEIGHTS) == 0.0


def test_score_experience_under_minimum_is_raw_ratio() -> None:
    assert score_experience(2, 4, weights=DEFAULT_WEIGHTS) == pytest.approx(0.5)


def test_score_experience_meets_minimum_exactly() -> None:
    assert score_experience(5, 5, weights=DEFAULT_WEIGHTS) == 1.0


def test_score_experience_moderately_over_minimum_stays_perfect() -> None:
    """raw <= overqual_ratio (2.0) does not increase past 1.0 either."""
    assert score_experience(8, 5, weights=DEFAULT_WEIGHTS) == 1.0  # raw = 1.6


def test_score_experience_overqualified_past_ratio_mildly_drops() -> None:
    # raw = 15/5 = 3.0 -> 1.0 - (3.0 - 2.0) * 0.1 = 0.9
    assert score_experience(15, 5, weights=DEFAULT_WEIGHTS) == pytest.approx(0.9)


def test_score_experience_far_overqualified_hits_floor() -> None:
    # raw = 100/5 = 20 -> 1.0 - (20-2)*0.1 = -0.8 -> clamped at overqual_floor 0.8
    assert score_experience(100, 5, weights=DEFAULT_WEIGHTS) == pytest.approx(0.8)


# ── score_education: degree LEVEL + field-of-study relevance (ADR-028) ────


def test_score_education_no_min_level_is_perfect() -> None:
    assert score_education(["bachelors"], None, weights=DEFAULT_WEIGHTS) == 1.0


def test_score_education_no_candidate_levels_is_zero() -> None:
    assert score_education([], "bachelors", weights=DEFAULT_WEIGHTS) == 0.0


def test_score_education_all_none_levels_is_zero() -> None:
    assert score_education([None, None], "bachelors", weights=DEFAULT_WEIGHTS) == 0.0


def test_score_education_meets_required_level_is_perfect() -> None:
    assert score_education(["bachelors"], "bachelors", weights=DEFAULT_WEIGHTS) == 1.0


def test_score_education_exceeds_required_level_is_perfect() -> None:
    assert (
        score_education(["masters", "associate"], "bachelors", weights=DEFAULT_WEIGHTS)
        == 1.0
    )


def test_score_education_below_required_level_gets_partial_credit() -> None:
    # associate(2) < bachelors(3) -> education_partial (0.5) * 2/3
    assert score_education(
        ["associate"], "bachelors", weights=DEFAULT_WEIGHTS
    ) == pytest.approx(0.5 * (2 / 3))


def test_score_education_unrecognized_candidate_level_scores_as_zero_floor() -> None:
    assert score_education(
        ["not_a_real_degree_level"], "bachelors", weights=DEFAULT_WEIGHTS
    ) == pytest.approx(0.0)


def test_score_education_unknown_jd_level_defaults_to_full_credit() -> None:
    """An unrecognized JD requirement maps to level 0 via _LEVEL_ORDER.get
    default; any recognized candidate level clears that trivially."""
    assert (
        score_education(["bachelors"], "not_a_real_jd_level", weights=DEFAULT_WEIGHTS)
        == 1.0
    )


# ── score_education: field-of-study relevance extension (ADR-028) ─────────
#
# ``jd.education.fields`` is optional per-JD, fuzzy-matched via
# ``rapidfuzz.fuzz.token_set_ratio`` at ``weights.education_field_fuzz``
# (default 0.85). A candidate who MEETS the level bar but whose qualifying
# degree is in a field NOT on the JD's allowed list is capped at
# ``education_partial`` instead of getting full credit. A JD with no
# ``jd_fields`` stays level-only — today's (pre-ADR-028) behavior, unchanged.
# Below-level candidates are UNAFFECTED by field: the field axis is never
# consulted below the level bar (ADR-009 §7 / ADR-028).

_ALLOWED_TECH_FIELDS = ["Computer Science", "Software Engineering", "Data Engineering"]


def test_score_education_backward_compat_empty_jd_fields_stays_level_only() -> None:
    """Explicit empty ``jd_fields`` (the new keyword's default) must behave
    EXACTLY like the pre-ADR-028 level-only scorer, even when the candidate's
    field would not have matched anything — an empty allowed list means the
    JD asked for no particular field at all."""
    assert (
        score_education(
            ["bachelors"],
            "bachelors",
            candidate_fields=["Communications"],
            jd_fields=(),
            weights=DEFAULT_WEIGHTS,
        )
        == 1.0
    )


def test_score_education_meets_level_field_in_allowed_list_is_perfect() -> None:
    assert (
        score_education(
            ["bachelors"],
            "bachelors",
            candidate_fields=["Computer Science"],
            jd_fields=_ALLOWED_TECH_FIELDS,
            weights=DEFAULT_WEIGHTS,
        )
        == 1.0
    )


def test_score_education_meets_level_field_not_in_allowed_list_is_capped() -> None:
    """MUTATION GUARD: the pre-ADR-028 level-only scorer returns 1.0 here (the
    candidate meets the level bar) — an implementation that ignores
    ``jd_fields`` entirely would wrongly pass this. Only a scorer that
    actually consults the field axis returns ``education_partial`` (0.5)."""
    result = score_education(
        ["bachelors"],
        "bachelors",
        candidate_fields=["Communications"],
        jd_fields=_ALLOWED_TECH_FIELDS,
        weights=DEFAULT_WEIGHTS,
    )
    assert result == DEFAULT_WEIGHTS.education_partial
    assert result != 1.0, (
        "a level-only scorer that never reads jd_fields would (wrongly) "
        "return 1.0 here"
    )


def test_score_education_fuzzy_field_variation_within_tolerance_still_matches() -> None:
    """A genuine variation ("Computer Sciences", plural) that is NOT byte-equal
    to the allowed entry after normalisation (lowercase + whitespace collapse
    only — no stemming), but clears the default 0.85
    ``rapidfuzz.fuzz.token_set_ratio`` bar. MEASURED:
    ``token_set_ratio("computer sciences", "computer science") == 96.97``,
    i.e. 0.9697 >= 0.85."""
    assert (
        score_education(
            ["bachelors"],
            "bachelors",
            candidate_fields=["Computer Sciences"],
            jd_fields=["Computer Science"],
            weights=DEFAULT_WEIGHTS,
        )
        == 1.0
    )


def test_score_education_field_fuzz_threshold_is_configurable_and_flips_the_verdict() -> (  # noqa: E501
    None
):
    """``weights.education_field_fuzz`` is a real, load-bearing knob, not a
    documented-but-unused constant. MEASURED:
    ``token_set_ratio("computer science, b.s.", "computer science") == 84.21``
    (0.8421) — BELOW the default 0.85 bar (capped), but ABOVE a lowered 0.80
    bar (full credit). The same candidate field flips verdict purely on the
    threshold, proving the knob actually reaches the scorer."""
    candidate_fields = ["Computer Science, B.S."]
    jd_fields = ["Computer Science"]

    default_result = score_education(
        ["bachelors"],
        "bachelors",
        candidate_fields=candidate_fields,
        jd_fields=jd_fields,
        weights=DEFAULT_WEIGHTS,
    )
    assert default_result == DEFAULT_WEIGHTS.education_partial

    lenient_weights = MatchWeights(education_field_fuzz=0.80)
    lenient_result = score_education(
        ["bachelors"],
        "bachelors",
        candidate_fields=candidate_fields,
        jd_fields=jd_fields,
        weights=lenient_weights,
    )
    assert lenient_result == 1.0


def test_score_education_unknown_candidate_field_meets_level_is_capped() -> None:
    """Documented decision: an unparsed/``None`` candidate field counts as NO
    match when the JD lists allowed fields — full credit is awarded only when
    a qualifying-level degree's field can be CONFIRMED against the allowed
    list. (Counter-risk, noted in ADR-028: an unparsed field over-penalizes a
    candidate who may in fact hold an allowed-field degree.)"""
    assert (
        score_education(
            ["bachelors"],
            "bachelors",
            candidate_fields=[None],
            jd_fields=_ALLOWED_TECH_FIELDS,
            weights=DEFAULT_WEIGHTS,
        )
        == DEFAULT_WEIGHTS.education_partial
    )


def test_score_education_below_level_non_allowed_field_still_gets_partial_credit() -> (
    None
):
    """Field is NEVER consulted below the level bar. associate(2) <
    bachelors(3) -> education_partial * 2/3, identical to the pre-ADR-028
    level-only result, even though "Communications" is not on the allowed
    list — the field axis never runs."""
    assert score_education(
        ["associate"],
        "bachelors",
        candidate_fields=["Communications"],
        jd_fields=_ALLOWED_TECH_FIELDS,
        weights=DEFAULT_WEIGHTS,
    ) == pytest.approx(0.5 * (2 / 3))


def test_score_education_below_level_allowed_field_is_still_only_the_below_level_partial() -> (  # noqa: E501
    None
):
    """The r14/r11-style corpus twin: an associate-level candidate whose field
    IS on the allowed list must still get the below-level partial, NOT full
    credit — meeting the field bar can never substitute for meeting the level
    bar. (r11 is the below-level half of that twin; r14, a bachelors-level CS
    candidate, gets full credit via the "meets level" tests above.)"""
    assert score_education(
        ["associate"],
        "bachelors",
        candidate_fields=["Computer Science"],
        jd_fields=_ALLOWED_TECH_FIELDS,
        weights=DEFAULT_WEIGHTS,
    ) == pytest.approx(0.5 * (2 / 3))


def test_score_education_qualifying_wrong_field_pairs_with_below_level_right_field_is_capped() -> (  # noqa: E501
    None
):
    """Two degrees: a QUALIFYING-level one in the WRONG field, and a
    BELOW-level one in the RIGHT field. The right-field degree never meets the
    bar, so it cannot contribute full credit — only the qualifying (wrong-
    field) degree is eligible, and it fails the field check. Also pins the
    ``zip_longest`` index alignment: degree i's level pairs with degree i's
    field, not any cross product."""
    assert (
        score_education(
            ["bachelors", "associate"],
            "bachelors",
            candidate_fields=["Communications", "Computer Science"],
            jd_fields=["Computer Science"],
            weights=DEFAULT_WEIGHTS,
        )
        == DEFAULT_WEIGHTS.education_partial
    )


def test_score_education_qualifying_right_field_pairs_with_extra_below_level_wrong_field_is_perfect() -> (  # noqa: E501
    None
):
    """The mirror of the above, sanity-checking the pairing is not merely
    "any field matches": the QUALIFYING degree is in the right field (full
    credit), and an extra below-level wrong-field degree must not drag it
    down."""
    assert (
        score_education(
            ["bachelors", "associate"],
            "bachelors",
            candidate_fields=["Computer Science", "Communications"],
            jd_fields=["Computer Science"],
            weights=DEFAULT_WEIGHTS,
        )
        == 1.0
    )


# ── normalise_vector_scores ───────────────────────────────────────────────


def test_normalise_vector_scores_empty_list() -> None:
    assert normalise_vector_scores([]) == []


def test_normalise_vector_scores_single_element_is_degenerate() -> None:
    assert normalise_vector_scores([5.0]) == [1.0]


def test_normalise_vector_scores_all_identical_is_degenerate() -> None:
    assert normalise_vector_scores([3.0, 3.0, 3.0]) == [1.0, 1.0, 1.0]


def test_normalise_vector_scores_min_max_scales_linearly() -> None:
    assert normalise_vector_scores([1.0, 2.0, 3.0]) == pytest.approx([0.0, 0.5, 1.0])


def test_normalise_vector_scores_handles_negative_values() -> None:
    assert normalise_vector_scores([-5.0, 5.0]) == pytest.approx([0.0, 1.0])


# ── verify_evidence: rapidfuzz anti-fabrication (blocker #6) ────────────────


@pytest.mark.parametrize(
    "resume_id, chunk_id, fabricated_quote", _NEGATIVE_EVIDENCE_CASES
)
def test_verify_evidence_rejects_every_negative_evidence_fabrication(
    resume_id: str, chunk_id: str, fabricated_quote: str
) -> None:
    resume_raw = _load_resume_raw(resume_id)
    chunks_by_id = _chunks_by_id(resume_raw)
    assert chunk_id in chunks_by_id  # sanity: the fixture's chunk really exists
    req = RequirementEvidence(
        requirement="whatever the JD asked for",
        status="met",
        evidence=fabricated_quote,
        evidence_chunk_ids=[chunk_id],
        confidence=0.9,
    )
    evidence = EvidenceObject(requirements=[req])
    cleaned = verify_evidence(evidence, chunks_by_id, weights=DEFAULT_WEIGHTS)
    result = cleaned.requirements[0]
    assert result.evidence == "", (
        f"fabricated quote {fabricated_quote!r} against real chunk "
        f"{chunks_by_id[chunk_id]!r} must be blanked, not leaked"
    )
    assert result.status == "missing"
    assert result.confidence <= 0.3


@pytest.mark.parametrize("resume_id, skill_name, quote", _GOLD_EVIDENCE_CASES)
def test_verify_evidence_accepts_every_gold_evidence_anchor(
    resume_id: str, skill_name: str, quote: str
) -> None:
    resume_raw = _load_resume_raw(resume_id)
    chunks_by_id = _chunks_by_id(resume_raw)
    chunk_id = _skill_chunk_id(resume_raw, skill_name)
    assert quote.lower() in chunks_by_id[chunk_id].lower()  # sanity: real substring
    req = RequirementEvidence(
        requirement=skill_name,
        status="met",
        evidence=quote,
        evidence_chunk_ids=[chunk_id],
        confidence=0.9,
    )
    evidence = EvidenceObject(requirements=[req])
    cleaned = verify_evidence(evidence, chunks_by_id, weights=DEFAULT_WEIGHTS)
    result = cleaned.requirements[0]
    assert result.evidence == quote
    assert result.status == "met"
    assert result.confidence == 0.9


def test_verify_evidence_strips_hallucinated_chunk_ids() -> None:
    req = RequirementEvidence(
        requirement="Python",
        status="met",
        evidence="",
        evidence_chunk_ids=["c_999_does_not_exist"],
        confidence=0.9,
    )
    evidence = EvidenceObject(requirements=[req])
    cleaned = verify_evidence(evidence, {}, weights=DEFAULT_WEIGHTS)
    assert cleaned.requirements[0].evidence_chunk_ids == []


def test_verify_evidence_blanks_and_demotes_an_uncited_quote() -> None:
    """A quote with NO surviving citation gets the SAME scrub as a quote that
    fails to match its cited chunk: evidence blanked, ``met`` demoted to
    ``missing``, confidence capped. No citation is strictly less evidence than
    a bad citation, so it cannot warrant a weaker response."""
    req = RequirementEvidence(
        requirement="Python",
        status="met",
        evidence="some quote with no valid citation",
        evidence_chunk_ids=["c_999_does_not_exist"],
        confidence=0.95,
    )
    evidence = EvidenceObject(requirements=[req])
    cleaned = verify_evidence(evidence, {}, weights=DEFAULT_WEIGHTS)
    result = cleaned.requirements[0]
    assert result.evidence == ""
    assert result.status == "missing"
    assert result.confidence <= 0.3


def test_verify_evidence_passes_through_requirement_with_no_quote() -> None:
    req = RequirementEvidence(
        requirement="Docker", status="missing", evidence="", confidence=0.0
    )
    evidence = EvidenceObject(requirements=[req])
    cleaned = verify_evidence(evidence, {}, weights=DEFAULT_WEIGHTS)
    assert cleaned.requirements[0].requirement == "Docker"
    assert cleaned.requirements[0].status == "missing"


def test_verify_evidence_does_not_mutate_input() -> None:
    req = RequirementEvidence(
        requirement="Python",
        status="met",
        evidence="junk",
        evidence_chunk_ids=["c1"],
        confidence=0.9,
    )
    evidence = EvidenceObject(requirements=[req])
    chunks_by_id = {"c1": "totally unrelated text with no overlap at all"}
    verify_evidence(evidence, chunks_by_id, weights=DEFAULT_WEIGHTS)
    assert evidence.requirements[0].evidence == "junk"


def test_verify_evidence_scrubs_fabricated_cover_letter_quote() -> None:
    cle = CoverLetterEvidence(
        theme="motivation",
        evidence="I have always dreamed of astrophysics since childhood",
        evidence_chunk_ids=["cl_1"],
        confidence=0.9,
    )
    evidence = EvidenceObject(cover_letter_evidence=[cle])
    chunks_by_id = {
        "cl_1": "I am excited to apply my backend engineering skills to this role."
    }
    cleaned = verify_evidence(evidence, chunks_by_id, weights=DEFAULT_WEIGHTS)
    result = cleaned.cover_letter_evidence[0]
    assert result.evidence == ""
    assert result.confidence <= 0.3


def test_landmine_wratio_leaks_a_fabrication_and_ratio_rejects_gold_evidence() -> None:
    """Documents WHY verify_evidence's chosen metric must be
    ``partial_ratio`` or ``token_set_ratio`` — measured against this
    corpus's real fixture text (see ``labels.json``'s negative_evidence /
    gold_evidence anchors):

    * ``fuzz.WRatio`` scores r02's ``c_002`` fabrication at 0.855 — it would
      LEAK past the 0.85 ``evidence_verify_fuzz`` bar.
    * ``fuzz.ratio`` scores r01's Python/PostgreSQL gold anchors at
      0.648/0.796 — both would be WRONGLY REJECTED, even though they are
      verbatim substrings of their cited chunks.
    """
    pytest.importorskip("rapidfuzz")
    from rapidfuzz import fuzz

    r02 = _load_resume_raw("r02_jordan_kim")
    r02_chunks = _chunks_by_id(r02)
    fabricated = _LABELS["resumes"]["r02_jordan_kim"]["negative_evidence"]["c_002"]
    haystack = r02_chunks["c_002"].lower()
    needle = fabricated.lower()

    wratio = fuzz.WRatio(needle, haystack) / 100
    assert wratio == pytest.approx(0.855, abs=0.005), (
        "landmine: WRatio scores this FABRICATED quote high enough to leak "
        "past the 0.85 evidence_verify_fuzz bar"
    )
    assert wratio >= DEFAULT_WEIGHTS.evidence_verify_fuzz  # would (wrongly) verify

    r01 = _load_resume_raw("r01_casey_rivera")
    r01_chunks = _chunks_by_id(r01)
    gold = _LABELS["resumes"]["r01_casey_rivera"]["gold_evidence"]
    py_chunk_id = _skill_chunk_id(r01, "Python")
    pg_chunk_id = _skill_chunk_id(r01, "PostgreSQL")

    py_ratio = fuzz.ratio(gold["Python"].lower(), r01_chunks[py_chunk_id].lower()) / 100
    pg_ratio = (
        fuzz.ratio(gold["PostgreSQL"].lower(), r01_chunks[pg_chunk_id].lower()) / 100
    )
    assert py_ratio == pytest.approx(0.648, abs=0.005), (
        "landmine: plain ratio() scores this GOLD (verbatim substring) quote "
        "below the 0.85 bar -- would wrongly reject valid evidence"
    )
    assert pg_ratio == pytest.approx(0.796, abs=0.005)
    assert py_ratio < DEFAULT_WEIGHTS.evidence_verify_fuzz
    assert pg_ratio < DEFAULT_WEIGHTS.evidence_verify_fuzz

    # The metric verify_evidence must actually use clears BOTH traps at once:
    # rejects the fabrication, accepts both gold anchors.
    for metric in (fuzz.partial_ratio, fuzz.token_set_ratio):
        assert metric(needle, haystack) / 100 < DEFAULT_WEIGHTS.evidence_verify_fuzz
        assert (
            metric(gold["Python"].lower(), r01_chunks[py_chunk_id].lower()) / 100
            >= DEFAULT_WEIGHTS.evidence_verify_fuzz
        )
        assert (
            metric(gold["PostgreSQL"].lower(), r01_chunks[pg_chunk_id].lower()) / 100
            >= DEFAULT_WEIGHTS.evidence_verify_fuzz
        )


# ── _evidence_completeness ──────────────────────────────────────────────────


def test_evidence_completeness_none_evidence_is_zero() -> None:
    assert _evidence_completeness(None, weights=DEFAULT_WEIGHTS) == 0.0


def test_evidence_completeness_empty_requirements_is_zero() -> None:
    assert (
        _evidence_completeness(EvidenceObject(requirements=[]), weights=DEFAULT_WEIGHTS)
        == 0.0
    )


def test_evidence_completeness_all_met_high_confidence_is_one() -> None:
    ev = EvidenceObject(
        requirements=[
            RequirementEvidence(requirement="Python", status="met", confidence=0.9),
            RequirementEvidence(requirement="SQL", status="met", confidence=0.8),
        ]
    )
    assert _evidence_completeness(ev, weights=DEFAULT_WEIGHTS) == 1.0


def test_evidence_completeness_met_below_confidence_threshold_does_not_count() -> None:
    ev = EvidenceObject(
        requirements=[
            RequirementEvidence(requirement="Python", status="met", confidence=0.5),
            RequirementEvidence(requirement="SQL", status="missing", confidence=0.0),
        ]
    )
    assert _evidence_completeness(ev, weights=DEFAULT_WEIGHTS) == 0.0


def test_evidence_completeness_partial_counts_at_partial_weight() -> None:
    ev = EvidenceObject(
        requirements=[
            RequirementEvidence(requirement="Python", status="partial", confidence=0.6),
            RequirementEvidence(requirement="SQL", status="missing", confidence=0.0),
        ]
    )
    # (0 met + 0.5 * 1 partial) / 2 = 0.25
    assert _evidence_completeness(ev, weights=DEFAULT_WEIGHTS) == pytest.approx(0.25)


# ── _motivation_score ────────────────────────────────────────────────────────


def test_motivation_score_none_evidence_is_zero() -> None:
    assert _motivation_score(None, weights=DEFAULT_WEIGHTS) == 0.0


def test_motivation_score_empty_cover_letter_evidence_is_zero() -> None:
    assert (
        _motivation_score(
            EvidenceObject(cover_letter_evidence=[]), weights=DEFAULT_WEIGHTS
        )
        == 0.0
    )


def test_motivation_score_all_verified_is_mean_confidence() -> None:
    ev = EvidenceObject(
        cover_letter_evidence=[
            CoverLetterEvidence(
                theme="motivation", confidence=0.9, evidence_chunk_ids=["cl_1"]
            ),
            CoverLetterEvidence(
                theme="growth", confidence=0.7, evidence_chunk_ids=["cl_2"]
            ),
        ]
    )
    assert _motivation_score(ev, weights=DEFAULT_WEIGHTS) == pytest.approx(0.8)


def test_motivation_score_unverified_theme_drags_score_down() -> None:
    """Denominator is ALL emitted themes, not just verified ones — an
    unverified/uncited theme drags the score down rather than being
    excluded outright."""
    ev = EvidenceObject(
        cover_letter_evidence=[
            CoverLetterEvidence(
                theme="motivation", confidence=0.9, evidence_chunk_ids=["cl_1"]
            ),
            CoverLetterEvidence(
                theme="cultural_fit", confidence=0.9, evidence_chunk_ids=[]
            ),
        ]
    )
    assert _motivation_score(ev, weights=DEFAULT_WEIGHTS) == pytest.approx(
        0.45
    )  # 0.9 / 2


def test_motivation_score_all_unverified_is_zero() -> None:
    ev = EvidenceObject(
        cover_letter_evidence=[
            CoverLetterEvidence(
                theme="motivation", confidence=0.5, evidence_chunk_ids=["cl_1"]
            ),
        ]
    )
    assert _motivation_score(ev, weights=DEFAULT_WEIGHTS) == 0.0


# ── stage4_combine ───────────────────────────────────────────────────────────


def _breakdown(**overrides: Any) -> ScoreBreakdown:
    base: dict[str, Any] = {
        "skill": 0.8,
        "experience": 0.8,
        "education": 0.8,
        "seniority": 0.8,
        "vector": 0.8,
        "structured": 0.8,
    }
    base.update(overrides)
    return ScoreBreakdown(**base)


def test_stage4_combine_empty_input_returns_empty_list() -> None:
    assert stage4_combine([], DEFAULT_WEIGHTS) == []


def test_stage4_combine_ranks_descending_by_final_score() -> None:
    low_id, high_id = uuid4(), uuid4()
    low = _CombineInput(
        resume_id=low_id,
        structured=0.3,
        breakdown=_breakdown(structured=0.3),
        evidence=None,
    )
    high = _CombineInput(
        resume_id=high_id,
        structured=0.9,
        breakdown=_breakdown(structured=0.9),
        evidence=None,
    )
    entries = stage4_combine([low, high], DEFAULT_WEIGHTS)
    assert entries[0].resume_id == high_id
    assert entries[0].rank == 1
    assert entries[1].resume_id == low_id
    assert entries[1].rank == 2


def test_stage4_combine_final_score_is_weighted_sum() -> None:
    rid = uuid4()
    ev = EvidenceObject(
        requirements=[
            RequirementEvidence(requirement="Python", status="met", confidence=0.9)
        ]
    )
    combine_in = _CombineInput(
        resume_id=rid, structured=0.8, breakdown=_breakdown(structured=0.8), evidence=ev
    )
    entries = stage4_combine([combine_in], DEFAULT_WEIGHTS)
    [entry] = entries
    expected = (
        DEFAULT_WEIGHTS.structured * 0.8
        + DEFAULT_WEIGHTS.evidence * 1.0
        + DEFAULT_WEIGHTS.motivation * 0.0
    )
    assert entry.score_final == pytest.approx(expected)
    assert entry.score_structured == 0.8
    assert entry.score_evidence == 1.0


def test_stage4_combine_writes_computed_motivation_into_breakdown() -> None:
    """The deterministic motivation sub-score is surfaced back onto
    ``breakdown.motivation`` so the cover-letter contribution is auditable."""
    rid = uuid4()
    ev = EvidenceObject(
        cover_letter_evidence=[
            CoverLetterEvidence(
                theme="motivation", confidence=0.9, evidence_chunk_ids=["cl_1"]
            )
        ]
    )
    combine_in = _CombineInput(
        resume_id=rid,
        structured=0.5,
        breakdown=_breakdown(structured=0.5, motivation=0.0),
        evidence=ev,
    )
    entries = stage4_combine([combine_in], DEFAULT_WEIGHTS)
    [entry] = entries
    assert entry.breakdown.motivation == pytest.approx(0.9)


# ═══════════════════════════════════════════════════════════════════════════
# ADR-022 follow-up hardening (RED) — superset bypass, cross-chunk quotes,
# minimum quote length.
#
# ADR-022 closed the WEAKER of two bypasses in ``verify_evidence`` (the lenient
# uncited arm). Its "Follow-up items" list records the larger one, still open:
#
#   #1 HIGH   — ``partial_ratio`` returns 1.000 when the quote CONTAINS the
#               whole cited chunk verbatim plus arbitrary appended fabrication.
#   #4 MEDIUM — no minimum quote length; ``"API"`` scores 1.000 against any
#               chunk containing it.
#
# Human decisions encoded here (settled, not re-litigated):
#
#   * A quote must be a span of EXACTLY ONE cited chunk. A quote spanning two
#     cited chunks concatenated is REJECTED, not accepted.
#   * Minimum quote length floor = 16 characters (LOWERED from 32 by human
#     decision — see the FINDING 4 block further down for the measurements
#     that forced it).
#   * The length guard is FULLY CLOSED, not a ratio: reject when the
#     whitespace-collapsed quote is longer than the whitespace-collapsed chunk
#     AT ALL. There is no tunable ``k``.
#
# WHY THE RATIO WAS ABANDONED (this block previously recorded ``k = 1.05`` as
# the settled decision; that framing is superseded, and the shipped guard is
# the closed one). A ratio leaves a small-append window permanently open, and
# the corpus measures how small: appending ONE fabricated character to r01's
# 148-char Python chunk yields a length ratio of ~1.007, and to r02's 131-char
# chunk ~1.008. ``partial_ratio`` still scores both 1.000. k = 1.05 does not
# catch either, so every "+1" case below would ride straight through a
# ratio-based fix. A quote is a SPAN of one chunk, and a span cannot be longer
# than the thing it spans — so the structural rule ("longer at all => not a
# span") is both simpler and strictly stronger than any k.
#
# The ``plus1`` parameter case and ``test_superset_bypass_at_plus_one_...``
# below are KEPT as regression pins: the arithmetic they state is real and is
# exactly why the ratio is gone. Only their framing changed.
#
# The ANTI-OVER-REJECTION arm at the bottom is load-bearing: without it a
# ``_fuzz_ratio`` that simply ``return 0.0`` would satisfy every test above.
# ═══════════════════════════════════════════════════════════════════════════

# The rejected ratio constant. Retained ONLY so the tests that document why a
# ratio cannot work can state it; the implementation has no such constant.
_REJECTED_LENGTH_RATIO_K = 1.05
# Minimum quote length floor, in characters, fixed by human decision.
# Lowered 32 -> 16 (security FINDING 4). Kept as a module constant so every
# boundary case below moves together with MatchWeights.
_MIN_QUOTE_CHARS = 16
# The floor as originally shipped. Retained ONLY so the FINDING 4 cases can
# state which genuine credentials the old value erased.
_PRE_FINDING_4_FLOOR = 32

# Deterministic fabrication filler. Starts with a non-space character so that
# an append of length 1 is a real character rather than trailing whitespace
# that ``.strip()`` would silently erase.
_FABRICATION_FILLER = (
    "led a team of twelve engineers migrating the billing monolith to "
    "kubernetes on google cloud and owned the dbt transformation layer "
) * 24


def _superset_quote(chunk_text: str, append_len: int) -> str:
    """``chunk_text`` verbatim followed by ``append_len`` fabricated chars."""
    assert len(_FABRICATION_FILLER) >= append_len
    return chunk_text + _FABRICATION_FILLER[:append_len]


def _r01_python_chunk() -> tuple[str, str]:
    """(chunk_id, chunk_text) for r01's real Python-evidence chunk."""
    resume_raw = _load_resume_raw("r01_casey_rivera")
    chunk_id = _skill_chunk_id(resume_raw, "Python")
    return chunk_id, _chunks_by_id(resume_raw)[chunk_id]


# ── follow-up #1: the partial_ratio superset bypass ─────────────────────────


@pytest.mark.parametrize("append_len", [1, 8, 50, 1120], ids=lambda n: f"plus{n}")
def test_fuzz_ratio_rejects_a_quote_that_is_the_whole_chunk_plus_appended_text(
    append_len: int,
) -> None:
    """ADR-022 follow-up #1. ``fuzz.partial_ratio`` scores the best-matching
    WINDOW of the longer string against the shorter one, so a quote that
    CONTAINS the cited chunk verbatim scores 1.000 no matter how much invented
    text is bolted on. Measured at 1120 appended characters, surfacing as
    ``met`` @ 0.95.

    A quote must be a SPAN of the chunk. Text the chunk does not contain is
    never a span of it, at ANY append length — including 1 character. The
    ``plus1`` case is the one that fails a length-ratio-only fix: one extra
    character on r01's 148-char chunk is a ratio of ~1.007, comfortably under
    any usable k, while ``partial_ratio`` still returns 1.000. That is why the
    shipped guard rejects on "longer at all" rather than on a ratio.
    """
    _, chunk_text = _r01_python_chunk()
    quote = _superset_quote(chunk_text, append_len)
    assert len(quote) == len(chunk_text) + append_len
    assert chunk_text.lower() in quote.lower()  # the bypass shape, by construction

    score = _fuzz_ratio(quote.lower(), chunk_text.lower())
    assert score < DEFAULT_WEIGHTS.evidence_verify_fuzz, (
        f"quote = chunk verbatim + {append_len} fabricated chars scored "
        f"{score:.3f} >= {DEFAULT_WEIGHTS.evidence_verify_fuzz}: a superset of "
        "the chunk is not a span of it and must not verify"
    )


def test_superset_bypass_at_plus_one_would_survive_any_length_ratio_guard() -> None:
    """Pins WHY the ``plus1`` case above is not redundant with ``plus1120``,
    and why the shipped guard is fully closed rather than ratio-based.

    This test does NOT describe the implementation — there is no ``k`` in
    ``stages.py``. It records the measurement that ruled a ratio out: a 1-char
    append to r01's 148-char chunk measures 1.007, so the candidate constant
    k = 1.05 misses it by a wide margin while ``partial_ratio`` still returns
    1.000. Any k > 1.0 leaves the same window open; the only ratio that closes
    it is 1.0, which is the closed guard stated as a ratio.
    """
    _, chunk_text = _r01_python_chunk()
    plus_one = _superset_quote(chunk_text, 1)
    plus_1120 = _superset_quote(chunk_text, 1120)

    ratio_plus_one = len(plus_one) / len(chunk_text)
    assert ratio_plus_one == pytest.approx(1.007, abs=0.001)
    assert ratio_plus_one <= _REJECTED_LENGTH_RATIO_K, (
        "a 1-char append is NOT caught by a k=1.05 length-ratio guard — which "
        "is why the shipped guard rejects on 'longer at all' instead"
    )
    # ...while the guard that DID ship rejects it, because it is longer at all.
    assert len(plus_one) > len(chunk_text)
    assert len(plus_1120) > len(chunk_text) * _REJECTED_LENGTH_RATIO_K


@pytest.mark.parametrize("append_len", [1, 1120], ids=["plus1", "plus1120"])
def test_verify_evidence_scrubs_a_superset_quote_like_a_fabrication(
    append_len: int,
) -> None:
    """The requirements loop must give the superset quote the SAME scrub as
    the fabrication arm: evidence blanked, ``met`` demoted to ``missing``,
    confidence capped at 0.3."""
    resume_raw = _load_resume_raw("r01_casey_rivera")
    chunks_by_id = _chunks_by_id(resume_raw)
    chunk_id, chunk_text = _r01_python_chunk()
    req = RequirementEvidence(
        requirement="Python",
        status="met",
        evidence=_superset_quote(chunk_text, append_len),
        evidence_chunk_ids=[chunk_id],
        confidence=0.95,
    )
    cleaned = verify_evidence(
        EvidenceObject(requirements=[req]), chunks_by_id, weights=DEFAULT_WEIGHTS
    )
    result = cleaned.requirements[0]
    assert result.evidence == "", (
        "a quote containing the whole cited chunk plus fabricated text must be "
        "blanked, not surfaced to a human reviewer"
    )
    assert result.status == "missing"
    assert result.confidence <= 0.3


@pytest.mark.parametrize("append_len", [1, 1120], ids=["plus1", "plus1120"])
def test_verify_evidence_scrubs_a_superset_cover_letter_quote(
    append_len: int,
) -> None:
    """The cover-letter loop has the same shape as the requirements loop and
    must get the same fix — ADR-022's own defect recurred in both loops."""
    chunk_text = (
        "I am excited to apply my backend engineering skills to this role and "
        "to keep growing the data platform I have spent four years building."
    )
    cle = CoverLetterEvidence(
        theme="motivation",
        evidence=_superset_quote(chunk_text, append_len),
        evidence_chunk_ids=["cl_1"],
        confidence=0.9,
    )
    cleaned = verify_evidence(
        EvidenceObject(cover_letter_evidence=[cle]),
        {"cl_1": chunk_text},
        weights=DEFAULT_WEIGHTS,
    )
    result = cleaned.cover_letter_evidence[0]
    assert result.evidence == ""
    assert result.confidence <= 0.3


# ── cross-chunk quotes: a quote must be a span of EXACTLY ONE cited chunk ───


def _two_real_chunks() -> tuple[str, str, str, str]:
    """(id_a, text_a, id_b, text_b) — two real r01 chunks."""
    resume_raw = _load_resume_raw("r01_casey_rivera")
    chunks_by_id = _chunks_by_id(resume_raw)
    ids = sorted(chunks_by_id)
    assert len(ids) >= 2, "fixture must carry at least two chunks"
    return ids[0], chunks_by_id[ids[0]], ids[1], chunks_by_id[ids[1]]


def test_fuzz_ratio_rejects_a_cross_chunk_concatenation_against_each_chunk() -> None:
    """Human decision: a quote must be a span of EXACTLY ONE cited chunk.

    Two real chunks concatenated is a quote no single chunk contains, so it
    must fail against BOTH of them — not "pass because each half matches
    something". ``partial_ratio`` accepts it against either half today.
    """
    id_a, text_a, id_b, text_b = _two_real_chunks()
    quote = f"{text_a} {text_b}"

    for cid, text in ((id_a, text_a), (id_b, text_b)):
        score = _fuzz_ratio(quote.lower(), text.lower())
        assert score < DEFAULT_WEIGHTS.evidence_verify_fuzz, (
            f"cross-chunk concatenation scored {score:.3f} against {cid}: a "
            "quote spanning two chunks is a span of neither"
        )


def test_verify_evidence_rejects_a_quote_spanning_two_cited_chunks() -> None:
    """Citing BOTH ids does not legitimise the concatenation: the quote is
    still not a span of any ONE cited chunk, so it is scrubbed."""
    id_a, text_a, id_b, text_b = _two_real_chunks()
    resume_raw = _load_resume_raw("r01_casey_rivera")
    chunks_by_id = _chunks_by_id(resume_raw)
    req = RequirementEvidence(
        requirement="Python",
        status="met",
        evidence=f"{text_a} {text_b}",
        evidence_chunk_ids=[id_a, id_b],
        confidence=0.9,
    )
    cleaned = verify_evidence(
        EvidenceObject(requirements=[req]), chunks_by_id, weights=DEFAULT_WEIGHTS
    )
    result = cleaned.requirements[0]
    assert result.evidence == "", (
        "a quote stitched from two cited chunks must be rejected against every "
        "cited id, not accepted because each half matches one of them"
    )
    assert result.status == "missing"
    assert result.confidence <= 0.3


# ── follow-up #4: minimum quote length floor (16 chars) ─────────────────────
#
# SECURITY FINDING 4 — THE FLOOR WAS LOWERED 32 -> 16 BY HUMAN DECISION.
#
# At 32 the floor was scrubbing GENUINE evidence and demoting it met ->
# missing, indistinguishably from a fabrication. Measured, all blanked at 32:
#
#     "PhD in Computer Science"       23 chars
#     "AWS Solutions Architect"       23 chars
#     "Postgres schema migrations"    26 chars
#
# The eval corpus could not catch this — its shortest gold anchor was 71
# characters, so nothing in the corpus lived anywhere near the boundary. (That
# gap is now closed: labels.json carries a genuinely short gold anchor, and
# ``test_gold_anchors_sit_well_inside_both_new_guards`` pins that at least one
# anchor sits below the OLD floor, so reverting the floor fails the corpus.)
#
# 16 still rejects every degenerate case the floor exists for — "API" (3),
# "SQL" (3), "ETL" (3), "Kubernetes" (10) — while preserving short credentials
# and skill phrases that are real evidence. Both sides of the new boundary are
# pinned below (15 rejected / 16 accepted), exactly as they were at 32.
#
# The floor is ALSO load-bearing for security FINDING 1: a bare token padded
# with zero-width characters used to clear a 32-char floor on raw length
# (measured: "API" + 40x U+200B = 43 characters, floor cleared, rejected only
# by ``partial_ratio``). Lowering the floor makes that padding cheaper, which
# is precisely why the invisible-stripping half of FINDING 1 lands in the same
# change — after the scrub the same needle is 3 characters and the floor
# rejects it outright. See ``test_stripping_invisibles_...`` below.

_FLOOR_CHUNK = (
    "Built API gateways on Kubernetes and shipped typed FastAPI services "
    "for the platform team across three regions."
)
# Exactly 16 characters, and a verbatim span of _FLOOR_CHUNK.
_FLOOR_SPAN_16 = "Built API gatewa"
# Exactly 15 characters, and ALSO a verbatim span — the only thing separating
# it from the case above is the floor itself.
_FLOOR_SPAN_15 = "Built API gatew"
# A verbatim span that the OLD 32-char floor erased and the new one keeps.
_FLOOR_SPAN_UNDER_OLD_FLOOR = "Built API gateways on"


@pytest.mark.parametrize(
    "short_quote",
    ["API", "SQL", "ETL", "Kubernetes", "FastAPI", _FLOOR_SPAN_15],
    ids=[
        "api",
        "sql",
        "etl",
        "kubernetes",
        "fastapi",
        "one_char_under_floor",
    ],
)
def test_fuzz_ratio_is_zero_for_a_quote_under_the_16_char_floor(
    short_quote: str,
) -> None:
    """ADR-022 follow-up #4. ``"API"`` scores 1.000 against any chunk
    containing it, which makes a bare token indistinguishable from real
    evidence. Anything below the 16-character floor scores 0.0 outright.

    ``one_char_under_floor`` is the important case: it IS a genuine verbatim
    span, so only the floor can reject it. A fix that keys off "is this a real
    substring" instead of length will not fail it.

    "SQL" and "ETL" are the degenerate cases the human decision named as still
    having to be rejected at the lowered floor.
    """
    assert len(short_quote) < _MIN_QUOTE_CHARS
    if short_quote != "SQL" and short_quote != "ETL":
        assert short_quote.lower() in _FLOOR_CHUNK.lower()  # genuinely present
    assert _fuzz_ratio(short_quote.lower(), _FLOOR_CHUNK.lower()) == 0.0, (
        f"{short_quote!r} is under the {_MIN_QUOTE_CHARS}-char floor and must "
        "score 0.0 however well it matches"
    )


def test_fuzz_ratio_accepts_a_real_span_exactly_at_the_16_char_floor() -> None:
    """The other side of the boundary. The floor is ``< 16 rejects``, NOT
    ``<= 16 rejects``: a 16-character verbatim span is real evidence and must
    still verify at 1.000. Pins the off-by-one in the direction of
    over-rejection."""
    assert len(_FLOOR_SPAN_16) == _MIN_QUOTE_CHARS
    assert _FLOOR_SPAN_16.lower() in _FLOOR_CHUNK.lower()
    assert _fuzz_ratio(_FLOOR_SPAN_16.lower(), _FLOOR_CHUNK.lower()) == 1.0


# The genuine short credentials FINDING 4 measured being blanked at 32, each
# embedded in a chunk that really contains them.
_SHORT_CREDENTIAL_CASES: tuple[tuple[str, str], ...] = (
    (
        "PhD in Computer Science",
        "PhD in Computer Science, University of British Columbia, 2019.",
    ),
    (
        "AWS Solutions Architect",
        "Certifications: AWS Solutions Architect (Professional), renewed 2025.",
    ),
    (
        "Postgres schema migrations",
        "Owned Postgres schema migrations for the billing service end to end.",
    ),
)


@pytest.mark.parametrize(
    "quote, chunk",
    _SHORT_CREDENTIAL_CASES,
    ids=["phd", "aws_cert", "postgres_migrations"],
)
def test_genuine_short_credentials_survive_the_lowered_floor(
    quote: str, chunk: str
) -> None:
    """FINDING 4, stated as the over-rejection it was. Each of these is a real,
    unambiguous span of its chunk that the 32-char floor scored 0.0 and blanked
    as if it were invented."""
    assert quote in chunk
    assert len(quote) < _PRE_FINDING_4_FLOOR, "otherwise this case pins nothing"
    assert (
        _fuzz_ratio(quote.lower(), chunk.lower(), min_chars=_PRE_FINDING_4_FLOOR) == 0.0
    ), "the old floor erased it — this is the defect"
    assert _fuzz_ratio(quote.lower(), chunk.lower()) == 1.0


@pytest.mark.parametrize(
    "quote, chunk",
    _SHORT_CREDENTIAL_CASES,
    ids=["phd", "aws_cert", "postgres_migrations"],
)
def test_verify_evidence_keeps_genuine_short_credentials(
    quote: str, chunk: str
) -> None:
    """The same three, end-to-end: at 32 they came back blank and demoted
    ``met`` -> ``missing``, which is the exact signal a recruiter reads as
    "the model made this up"."""
    req = RequirementEvidence(
        requirement="Credential",
        status="met",
        evidence=quote,
        evidence_chunk_ids=["c_1"],
        confidence=0.9,
    )
    cleaned = verify_evidence(
        EvidenceObject(requirements=[req]), {"c_1": chunk}, weights=DEFAULT_WEIGHTS
    )
    result = cleaned.requirements[0]
    assert result.evidence == quote
    assert result.status == "met"
    assert result.confidence == 0.9


def test_verify_evidence_scrubs_a_bare_token_quote() -> None:
    """End-to-end shape of follow-up #4 through the verifier itself."""
    req = RequirementEvidence(
        requirement="API design",
        status="met",
        evidence="API",
        evidence_chunk_ids=["c_1"],
        confidence=0.9,
    )
    cleaned = verify_evidence(
        EvidenceObject(requirements=[req]),
        {"c_1": _FLOOR_CHUNK},
        weights=DEFAULT_WEIGHTS,
    )
    result = cleaned.requirements[0]
    assert result.evidence == ""
    assert result.status == "missing"
    assert result.confidence <= 0.3


def test_verify_evidence_keeps_a_real_span_exactly_at_the_floor() -> None:
    """Over-rejection guard for the floor, through the verifier."""
    req = RequirementEvidence(
        requirement="Kubernetes",
        status="met",
        evidence=_FLOOR_SPAN_16,
        evidence_chunk_ids=["c_1"],
        confidence=0.9,
    )
    cleaned = verify_evidence(
        EvidenceObject(requirements=[req]),
        {"c_1": _FLOOR_CHUNK},
        weights=DEFAULT_WEIGHTS,
    )
    result = cleaned.requirements[0]
    assert result.evidence == _FLOOR_SPAN_16
    assert result.status == "met"
    assert result.confidence == 0.9


# ── regression pin: the empty-needle guard (mutant closed in 1e1776c) ───────


@pytest.mark.parametrize(
    "blank_needle",
    ["", "   ", "\t\n ", " "],
    ids=["empty", "spaces", "tabs_newline", "single_space"],
)
def test_fuzz_ratio_is_zero_for_a_blank_needle(blank_needle: str) -> None:
    """ADR-022 records ``_fuzz_ratio``'s empty-needle guard as a mutation that
    SURVIVED review until it was closed in ``1e1776c``. Flipping the guard to
    return 1.0 would let an empty quote "verify" against any chunk and surface
    as ``met``. This pin must not regress while the function is rewritten.

    The whitespace-only variants extend it: ``verify_evidence`` strips before
    calling, but ``_fuzz_ratio`` is a contract in its own right and a
    whitespace run is not evidence of anything either.
    """
    assert _fuzz_ratio(blank_needle, _FLOOR_CHUNK.lower()) == 0.0


# ── ANTI-OVER-REJECTION ARM ─────────────────────────────────────────────────
#
# Load-bearing. Every test above is satisfied by ``_fuzz_ratio`` returning 0.0
# unconditionally; the tests below are what make that stub fail. The four gold
# anchors are real corpus evidence and must survive the hardened verifier
# untouched.


@pytest.mark.parametrize("resume_id, skill_name, quote", _GOLD_EVIDENCE_CASES)
def test_gold_anchor_still_scores_exactly_one_after_hardening(
    resume_id: str, skill_name: str, quote: str
) -> None:
    """All four 4a gold anchors are verbatim substrings of their cited chunk
    and must still score 1.000 — not merely "above the bar"."""
    resume_raw = _load_resume_raw(resume_id)
    chunks_by_id = _chunks_by_id(resume_raw)
    chunk_text = chunks_by_id[_skill_chunk_id(resume_raw, skill_name)]
    assert quote.lower() in chunk_text.lower()
    assert _fuzz_ratio(quote.lower(), chunk_text.lower()) == 1.0, (
        f"{resume_id}/{skill_name} is a verbatim span of its cited chunk; the "
        "hardening must not cost it a single point"
    )


@pytest.mark.parametrize("resume_id, skill_name, quote", _GOLD_EVIDENCE_CASES)
def test_gold_anchor_survives_verify_evidence_completely_intact(
    resume_id: str, skill_name: str, quote: str
) -> None:
    """Quote text, ``met`` status, confidence and citation all preserved."""
    resume_raw = _load_resume_raw(resume_id)
    chunks_by_id = _chunks_by_id(resume_raw)
    chunk_id = _skill_chunk_id(resume_raw, skill_name)
    req = RequirementEvidence(
        requirement=skill_name,
        status="met",
        evidence=quote,
        evidence_chunk_ids=[chunk_id],
        confidence=0.9,
    )
    cleaned = verify_evidence(
        EvidenceObject(requirements=[req]), chunks_by_id, weights=DEFAULT_WEIGHTS
    )
    result = cleaned.requirements[0]
    assert result.evidence == quote
    assert result.status == "met"
    assert result.confidence == 0.9
    assert result.evidence_chunk_ids == [chunk_id]


def test_gold_anchors_sit_well_inside_both_new_guards() -> None:
    """Feasibility pin for the two guards, measured against real corpus text:
    every gold anchor clears the minimum-quote-length floor, and none is longer
    than its own chunk.

    The bound asserted is ``<= 1.0``, NOT ``< 1.05``. The implemented
    invariant is "a span is never longer than its chunk", so a future fixture
    landing at ratio 1.02 would pass a 1.05 assertion here and then be
    silently rejected by the guard — the precise failure this pin advertises
    catching. Asserting the real invariant is what makes it catch it.
    """
    ratios: list[float] = []
    for resume_id, skill_name, quote in _GOLD_EVIDENCE_CASES:
        resume_raw = _load_resume_raw(resume_id)
        chunk_text = _chunks_by_id(resume_raw)[_skill_chunk_id(resume_raw, skill_name)]
        assert len(quote) >= _MIN_QUOTE_CHARS, (
            f"{resume_id}/{skill_name} is {len(quote)} chars, under the "
            f"{_MIN_QUOTE_CHARS}-char floor — the floor would erase real evidence"
        )
        ratios.append(len(quote) / len(chunk_text))

    assert len(ratios) == 5, "the corpus must carry exactly five gold anchors"
    assert sorted(ratios) == pytest.approx(
        [0.276, 0.480, 0.661, 0.823, 0.836], abs=0.005
    )
    assert max(ratios) <= 1.0, (
        "a gold anchor longer than its own chunk is not a span of it and the "
        "shipped guard will reject it — fix the fixture, not the guard"
    )


def test_the_corpus_carries_a_gold_anchor_that_defends_the_lowered_floor() -> None:
    """FINDING 4's second half. The floor was lowered on the strength of three
    MEASURED credentials ("PhD in Computer Science" and friends) that the
    corpus itself could not see: its shortest gold anchor was 71 characters,
    more than twice the old floor, so a revert to 32 — or to anything up to 71
    — passed the entire ranking-evals gate untouched.

    labels.json now carries a genuinely SHORT anchor, a real span of a real
    résumé chunk. This is the pin that makes the floor's value falsifiable by
    the corpus rather than by nothing: raise the floor above the shortest
    anchor and this fails, and so does the gate.
    """
    lengths = sorted(len(quote) for _, _, quote in _GOLD_EVIDENCE_CASES)
    assert lengths[0] < _PRE_FINDING_4_FLOOR, (
        f"shortest gold anchor is {lengths[0]} chars; with no anchor below the "
        f"old {_PRE_FINDING_4_FLOOR}-char floor the corpus cannot detect a "
        "revert of the FINDING 4 decision"
    )
    assert lengths[0] >= _MIN_QUOTE_CHARS


@pytest.mark.parametrize("resume_id, skill_name, quote", _GOLD_EVIDENCE_CASES)
def test_every_gold_anchor_survives_the_real_verifier_at_the_shipped_floor(
    resume_id: str, skill_name: str, quote: str
) -> None:
    """The short anchor's defence, run through ``_fuzz_ratio`` itself rather
    than through a length assertion. At a floor of 32 the short anchor scores
    0.0 here and is blanked; at 16 it scores 1.000."""
    resume_raw = _load_resume_raw(resume_id)
    chunk_text = _chunks_by_id(resume_raw)[_skill_chunk_id(resume_raw, skill_name)]
    assert _fuzz_ratio(quote.lower(), chunk_text.lower()) == 1.0


# ── WHITESPACE NORMALISATION (reviewer round 2, MINOR 6 + NIT 7) ────────────
#
# ``_collapse_whitespace`` was introduced to stop the length guard punishing
# honest whitespace variation (a re-wrapped quote, double spaces after a
# period, PDF column padding). Two problems were found and MEASURED:
#
# 1. Its comparison was unpinned — swapping the collapsed length comparison
#    for a raw ``len(needle) > len(haystack)`` passed the entire suite, so the
#    helper's whole documented purpose was untested.
# 2. Worse, the claimed tolerance did not actually MATERIALISE. The guard
#    compared COLLAPSED lengths but handed the RAW strings to
#    ``partial_ratio``, so a quote that survived the guard could still be
#    scrubbed by the 0.85 bar. Measured against r01's real 148-char Python
#    chunk, with the quote collapsing to a verbatim span in every case:
#
#      quote form                              raw pr    collapsed pr
#      whole chunk, double-spaced              0.885     1.000
#      whole chunk, triple-spaced              0.797 ✗   1.000
#      gold anchor, double-spaced              0.934     1.000
#      gold anchor, triple-spaced              0.877     1.000
#      gold anchor, newline-wrapped            0.859     1.000
#      gold anchor, 4-space column padding     0.826 ✗   1.000
#
#    Two of six legitimate re-renders scored BELOW 0.85 and were scrubbed as
#    fabrications, and one more sat 0.009 above the bar. The tolerance was
#    nominal.
#
# RESOLUTION (not a paper-over): the collapse now applies to BOTH the guard
# and the ``partial_ratio`` input, exactly like ``.lower()`` already does.
# It is a normalisation applied symmetrically to needle and haystack, so it
# opens no fabrication window — it cannot turn a non-span into a span, only
# make a genuine span score as one. Every row above becomes 1.000.
#
# ``_collapse_whitespace`` was NOT dropped: the measurements show the raw
# comparison over-rejects real evidence, which is the thing the helper exists
# to prevent. Corpus impact is nil — exactly one chunk in the 20-résumé corpus
# has any whitespace slack at all (r17_harper_nakamura/c_003, 1 character) —
# so the ranking is unchanged; the guard is for input the corpus does not
# contain yet.

# r01's real Python chunk collapses to itself (no internal whitespace runs),
# which is what lets these cases isolate the re-render from the text.
_WS_CASES: list[tuple[str, str]] = [
    ("double_spaced", "  "),
    ("triple_spaced", "   "),
    ("column_padded", "    "),
]


def test_r01_python_chunk_has_no_internal_whitespace_slack() -> None:
    """Keeps the cases below honest: any slack in the source chunk would make
    the collapsed-length arithmetic mean something else."""
    _, chunk_text = _r01_python_chunk()
    assert " ".join(chunk_text.split()) == chunk_text


@pytest.mark.parametrize("case_id, sep", _WS_CASES, ids=[c for c, _ in _WS_CASES])
def test_fuzz_ratio_scores_a_re_rendered_whole_chunk_exactly_one(
    case_id: str, sep: str
) -> None:
    """MINOR 6. A quote that collapses to the chunk verbatim IS the chunk, so
    it must score 1.000 — not "somewhere above the bar", which is what the raw
    ``partial_ratio`` input gave (0.885 double-spaced, 0.797 triple-spaced).

    This is also the mutation pin for ``_collapse_whitespace``: every quote
    here is LONGER than the chunk in raw characters, so a guard that compares
    raw lengths returns 0.0 and fails this test.
    """
    _, chunk_text = _r01_python_chunk()
    quote = chunk_text.replace(" ", sep)

    assert len(quote) > len(chunk_text), "raw-longer is the point of the case"
    assert " ".join(quote.split()) == chunk_text, "collapses to the chunk verbatim"

    assert _fuzz_ratio(quote.lower(), chunk_text.lower()) == 1.0


@pytest.mark.parametrize("case_id, sep", _WS_CASES, ids=[c for c, _ in _WS_CASES])
def test_fuzz_ratio_scores_a_re_rendered_gold_anchor_exactly_one(
    case_id: str, sep: str
) -> None:
    """The same for a genuine SPAN rather than the whole chunk. The
    ``column_padded`` case is the one that was being scrubbed outright: it
    measured 0.826 against the raw chunk, below the 0.85 bar."""
    resume_raw = _load_resume_raw("r01_casey_rivera")
    chunk_text = _chunks_by_id(resume_raw)[_skill_chunk_id(resume_raw, "Python")]
    anchor = _LABELS["resumes"]["r01_casey_rivera"]["gold_evidence"]["Python"]
    quote = anchor.replace(" ", sep)

    assert anchor in chunk_text, "the anchor is a verbatim span, by construction"
    assert _fuzz_ratio(quote.lower(), chunk_text.lower()) == 1.0


def test_fuzz_ratio_scores_a_newline_wrapped_gold_anchor_exactly_one() -> None:
    """Line re-wrapping, the most common real cause: same character count, so
    the guard was never the problem — the raw ``partial_ratio`` was, at 0.859
    (0.009 above the bar, i.e. one more wrapped line from being scrubbed)."""
    resume_raw = _load_resume_raw("r01_casey_rivera")
    chunk_text = _chunks_by_id(resume_raw)[_skill_chunk_id(resume_raw, "Python")]
    anchor = _LABELS["resumes"]["r01_casey_rivera"]["gold_evidence"]["Python"]
    quote = anchor.replace(" ", "\n")

    assert len(quote) == len(anchor)
    assert _fuzz_ratio(quote.lower(), chunk_text.lower()) == 1.0


def test_verify_evidence_keeps_a_column_padded_quote_completely_intact() -> None:
    """End-to-end, through the verifier at the real 0.85 threshold: a
    legitimately re-rendered quote must survive with its text, ``met`` status
    and confidence untouched. Before the fix this quote was blanked and
    demoted exactly like a fabrication."""
    resume_raw = _load_resume_raw("r01_casey_rivera")
    chunks_by_id = _chunks_by_id(resume_raw)
    chunk_id = _skill_chunk_id(resume_raw, "Python")
    anchor = _LABELS["resumes"]["r01_casey_rivera"]["gold_evidence"]["Python"]
    quote = anchor.replace(" ", "    ")

    req = RequirementEvidence(
        requirement="Python",
        status="met",
        evidence=quote,
        evidence_chunk_ids=[chunk_id],
        confidence=0.9,
    )
    cleaned = verify_evidence(
        EvidenceObject(requirements=[req]), chunks_by_id, weights=DEFAULT_WEIGHTS
    )
    result = cleaned.requirements[0]
    assert result.evidence == quote, (
        "a quote whose only difference from a verbatim span is whitespace "
        "rendering is real evidence, not a fabrication"
    )
    assert result.status == "met"
    assert result.confidence == 0.9


def test_whitespace_normalisation_does_not_reopen_the_superset_bypass() -> None:
    """The collapse is a normalisation, not a relaxation. A quote that is the
    whole chunk plus fabricated WORDS is still longer after collapsing, so it
    is still rejected — collapsing only erases whitespace differences."""
    _, chunk_text = _r01_python_chunk()
    quote = chunk_text.replace(" ", "   ") + "   and led a team of twelve engineers"

    assert _fuzz_ratio(quote.lower(), chunk_text.lower()) == 0.0


# ── NIT 7: the floor is measured on the collapsed needle ────────────────────


def test_fuzz_ratio_floor_rejects_a_bare_token_padded_out_to_the_floor() -> None:
    """``_fuzz_ratio``'s docstring presents a standalone contract, and under a
    raw ``len(needle)`` floor ``"API"`` plus 30 spaces (33 raw characters)
    cleared the bar while carrying three characters of evidence.

    Unreachable through ``verify_evidence`` today, which strips first — but a
    guard whose contract only holds because of what its one caller happens to
    do first is not a guard. The floor is measured on the collapsed, stripped
    needle, which is also the string actually scored.
    """
    padded = "API" + " " * 30
    assert len(padded) > _MIN_QUOTE_CHARS, "clears the floor on raw length"
    assert len(" ".join(padded.split())) < _MIN_QUOTE_CHARS

    assert _fuzz_ratio(padded.lower(), _FLOOR_CHUNK.lower()) == 0.0


def test_fuzz_ratio_floor_rejects_an_interior_padded_bare_token() -> None:
    """The same hole with the padding inside the needle rather than trailing,
    so ``.strip()`` alone would not close it."""
    padded = "API" + " " * 25 + "gateways"
    assert len(padded) > _MIN_QUOTE_CHARS
    assert len(" ".join(padded.split())) < _MIN_QUOTE_CHARS

    assert _fuzz_ratio(padded.lower(), _FLOOR_CHUNK.lower()) == 0.0


def test_fuzz_ratio_floor_still_accepts_a_re_rendered_span_at_the_floor() -> None:
    """Over-rejection guard for the collapsed floor: the 16-char span rendered
    with double spaces collapses back to 16 characters and must still verify.
    A floor applied to the RAW needle would pass this too, so it is paired
    with the padded cases above, which it does not."""
    quote = _FLOOR_SPAN_16.replace(" ", "  ")
    assert len(" ".join(quote.split())) == _MIN_QUOTE_CHARS
    assert _fuzz_ratio(quote.lower(), _FLOOR_CHUNK.lower()) == 1.0


# ═══════════════════════════════════════════════════════════════════════════
# SECURITY FINDING 1 — the interaction between the invisible-character scrub
# and the guards it sits in front of.
#
# The schema-level fix lives in ``schemas/matching.py`` and is tested there.
# What belongs HERE is the question the security gate asked about it, because
# the fix could weaken the guard it is meant to strengthen:
#
#   Python's ``\s`` — and therefore ``_collapse_whitespace`` — collapses NBSP,
#   U+2028 and U+3000, but NOT ZWSP / BOM / WJ / SHY. Those non-collapsing
#   invisibles INFLATE the needle's collapsed length, and the length guard
#   ("a span cannot be longer than the chunk it spans") then rejects it. So
#   today the asymmetry is FAIL-CLOSED. MEASURED against r01's real 148-char
#   chunk c_001, before the scrub:
#
#       genuine 60-char span                  score 1.000  accepted
#       same span + 200x U+200B (260 chars)   score 0.000  REJECTED (guard)
#       "API" + 40x U+200B      ( 43 chars)   score 0.076  rejected (fuzz)
#
#   Once the invisibles are STRIPPED, that inflation is gone: the padded span
#   is 60 characters again and scores 1.000. The guard that was rejecting it
#   no longer fires.
#
# THAT IS NOT A NEW WINDOW, and the reason is exactly what the tests below
# assert. Stripping removes only zero-width and format characters, so the
# needle's VISIBLE content is unchanged. After the scrub, the verdict on a
# padded quote is by construction the verdict on the same quote with no
# padding at all — a string the attacker could always have submitted
# directly. The fail-closed asymmetry was rejecting quotes whose visible
# content was GENUINE (an over-rejection), never quotes whose visible content
# was fabricated: appended visible fabrication is still visible after the
# scrub, and still trips the length guard.
#
# In one direction the scrub is strictly STRONGER, and the floor is where:
# "API" + 40x ZWSP is 43 characters and CLEARS even the old 32-char floor,
# surviving only on ``partial_ratio``'s verdict. Scrubbed, it is 3 characters
# and the floor rejects it outright. At the lowered 16-char floor that matters
# more, not less — 13 invisible characters would have been enough.
# ═══════════════════════════════════════════════════════════════════════════

# The invisible characters the FINDING 1 class removes and ``\s`` does not.
_NON_COLLAPSING_INVISIBLES: tuple[str, ...] = (
    chr(0x200B),  # ZERO WIDTH SPACE
    chr(0xFEFF),  # BOM
    chr(0x2060),  # WORD JOINER
    chr(0x00AD),  # SOFT HYPHEN
    chr(0x180E),  # MONGOLIAN VOWEL SEPARATOR
    chr(0x202E),  # RIGHT-TO-LEFT OVERRIDE
)


def _scrub(text: str) -> str:
    """The shipped schema scrub, applied directly so the verdict tests below
    exercise the SAME class the models do (not a copy that can drift)."""
    return _strip_control_chars(text)


def test_python_whitespace_does_not_collapse_the_invisibles_the_scrub_removes() -> None:
    """The premise the whole interaction rests on. If ``\\s`` ever started
    collapsing these, the reasoning above would need redoing rather than
    silently continuing to hold."""
    for ch in _NON_COLLAPSING_INVISIBLES:
        assert _collapse_whitespace(f"a{ch}b") == f"a{ch}b", (
            f"U+{ord(ch):04X} is collapsed by \\s after all — the fail-closed "
            "inflation argument no longer describes this code"
        )
    for ch in (chr(0x00A0), chr(0x2028), chr(0x3000)):
        assert _collapse_whitespace(f"a{ch}b") == "a b"


@pytest.mark.parametrize(
    "visible, verifies",
    [
        (_FLOOR_CHUNK[:60], True),
        (_FLOOR_SPAN_16, True),
        (_FLOOR_CHUNK, True),
        (_FLOOR_CHUNK + " and led a team of twelve engineers", False),
        (_FLOOR_CHUNK[:60] + " and owned the dbt transformation layer", False),
        ("API", False),
        (_FLOOR_SPAN_15, False),
    ],
    ids=[
        "genuine_span",
        "span_at_floor",
        "whole_chunk",
        "chunk_plus_fabrication",
        "prefix_plus_fabrication",
        "bare_token",
        "one_under_floor",
    ],
)
@pytest.mark.parametrize(
    "invisible", _NON_COLLAPSING_INVISIBLES, ids=lambda c: f"U+{ord(c):04X}"
)
def test_stripping_invisibles_cannot_change_a_quotes_verdict(
    visible: str, verifies: bool, invisible: str
) -> None:
    """The window check FINDING 1 demands. Padding a quote with 200 invisible
    characters and then scrubbing must land on EXACTLY the verdict the
    unpadded quote gets — no more (no new acceptance) and no less (no new
    over-rejection). Anything else means the scrub is doing work of its own
    on the fuzzy match, which it must not."""
    padded = visible + invisible * 200
    plain_score = _fuzz_ratio(_scrub(visible).lower(), _FLOOR_CHUNK.lower())
    scrubbed_score = _fuzz_ratio(_scrub(padded).lower(), _FLOOR_CHUNK.lower())

    assert scrubbed_score == plain_score
    assert (scrubbed_score >= DEFAULT_WEIGHTS.evidence_verify_fuzz) is verifies


@pytest.mark.parametrize(
    "invisible", _NON_COLLAPSING_INVISIBLES, ids=lambda c: f"U+{ord(c):04X}"
)
def test_invisible_padding_interleaved_through_a_fabrication_still_fails(
    invisible: str,
) -> None:
    """The adversarial shape the inflation argument was covering by accident:
    invisibles sprinkled BETWEEN the fabricated words, so that neither
    ``.strip()`` nor a trailing-padding heuristic would see them. The visible
    fabrication is still there after the scrub, so the length guard still
    rejects it."""
    fabrication = " and led a team of twelve engineers"
    quote = _FLOOR_CHUNK + invisible.join(fabrication)
    assert _fuzz_ratio(_scrub(quote).lower(), _FLOOR_CHUNK.lower()) == 0.0


@pytest.mark.parametrize(
    "invisible", _NON_COLLAPSING_INVISIBLES, ids=lambda c: f"U+{ord(c):04X}"
)
def test_the_scrub_makes_the_floor_strictly_harder_to_pad_past(
    invisible: str,
) -> None:
    """The one direction in which the scrub is stronger, not merely neutral.
    Unscrubbed, a bare token padded with invisibles clears the floor on
    length — measured at 43 characters against the OLD 32-char floor — and is
    left to ``partial_ratio`` alone. Scrubbed, the floor rejects it."""
    padded = "API" + invisible * 40
    assert len(_collapse_whitespace(padded)) > _PRE_FINDING_4_FLOOR
    assert len(_collapse_whitespace(_scrub(padded))) < _MIN_QUOTE_CHARS
    assert _fuzz_ratio(_scrub(padded).lower(), _FLOOR_CHUNK.lower()) == 0.0


# ── the scrub must be SYMMETRIC, or it becomes an over-rejection ────────────
#
# A defect the FINDING 1 fix INTRODUCES if the scrub is applied on one side
# only. The quote is scrubbed at the schema boundary; the CHUNK is not — it
# comes from ``resumes.parsed``, and ``parsing/extract.py::_sanitize`` strips
# NULs and nothing else. So a résumé whose extracted text carries SOFT HYPHENS
# (which is what a PDF emits at its line-break points) yields a haystack full
# of U+00AD and a needle with every one of them removed, and the two no longer
# match character-for-character.
#
# MEASURED on a 148-char chunk, needle = the same text scrubbed:
#
#     SHY every 60 chars (n=2)    0.986  verifies
#     SHY every 20 chars (n=7)    0.956  verifies
#     SHY every  8 chars (n=18)   0.892  verifies
#     SHY every  5 chars (n=29)   0.838  SCRUBBED AS FABRICATION
#     SHY every  2 chars (n=73)   0.671  SCRUBBED AS FABRICATION
#
# Real PDF hyphenation is far sparser than the break-even, so this is a narrow
# hole rather than a live corpus failure — but it is a hole of exactly the kind
# FINDING 4 was raised about (genuine evidence blanked and demoted, looking to
# a recruiter like a fabrication), and it did not exist before this change.
#
# The fix is to scrub BOTH sides inside ``_fuzz_ratio``, exactly as ``.lower()``
# and ``_collapse_whitespace`` already are. It is a normalisation, not a
# relaxation: it removes only invisible characters, from needle and haystack
# alike, so it cannot turn a non-span into a span — the anti-relaxation arm
# below is what holds that claim to account.


def _hyphenate(text: str, every: int) -> str:
    """Insert a SOFT HYPHEN every ``every`` characters — a PDF line-break
    artefact, rendered as nothing at all."""
    shy = chr(0x00AD)
    out: list[str] = []
    for i, ch in enumerate(text):
        out.append(ch)
        if i and i % every == 0:
            out.append(shy)
    return "".join(out)


@pytest.mark.parametrize("every", [60, 20, 8, 5, 2], ids=lambda n: f"every_{n}")
def test_a_scrubbed_quote_still_verifies_against_an_unscrubbed_chunk(
    every: int,
) -> None:
    """Over-rejection guard for the asymmetry. At every density — including the
    two that measured BELOW the 0.85 bar with a one-sided scrub — a quote that
    is the chunk itself must verify at 1.000."""
    dirty_chunk = _hyphenate(_FLOOR_CHUNK, every)
    quote = _scrub(dirty_chunk)
    assert quote != dirty_chunk, "otherwise this case pins nothing"
    assert _fuzz_ratio(quote.lower(), dirty_chunk.lower()) == 1.0


@pytest.mark.parametrize("every", [60, 8, 2], ids=lambda n: f"every_{n}")
def test_a_scrubbed_span_still_verifies_against_an_unscrubbed_chunk(
    every: int,
) -> None:
    """The same, for a genuine SPAN rather than the whole chunk — the shape a
    real evidence quote takes."""
    dirty_chunk = _hyphenate(_FLOOR_CHUNK, every)
    span = _scrub(_hyphenate(_FLOOR_CHUNK[:70], every))
    assert _fuzz_ratio(span.lower(), dirty_chunk.lower()) == 1.0


@pytest.mark.parametrize("every", [60, 8, 2], ids=lambda n: f"every_{n}")
def test_symmetric_scrubbing_does_not_relax_the_superset_guard(every: int) -> None:
    """The anti-relaxation arm. Normalising both sides must not let appended
    fabrication through: the visible fabrication survives the scrub on the
    needle, so the collapsed needle is still longer than the collapsed
    haystack and the length guard still rejects it at 0.0."""
    dirty_chunk = _hyphenate(_FLOOR_CHUNK, every)
    quote = _scrub(dirty_chunk) + " and led a team of twelve engineers"
    assert _fuzz_ratio(quote.lower(), dirty_chunk.lower()) == 0.0


def test_symmetric_scrubbing_does_not_relax_the_floor() -> None:
    """A haystack made ENTIRELY of invisibles scrubs to nothing, and a needle
    that scrubs to nothing hits the empty-needle guard. Neither may score."""
    invisible_only = chr(0x200B) * 200
    assert _fuzz_ratio("Built API gateways", invisible_only) == 0.0
    assert _fuzz_ratio(invisible_only, _FLOOR_CHUNK) == 0.0
    assert _fuzz_ratio(invisible_only, invisible_only) == 0.0


def test_verify_evidence_never_surfaces_a_bidi_override_in_a_quote() -> None:
    """End-to-end. The measured attack is
    ``chunk[:100] + U+202E + "detacirbaf"``: 111 characters, clears the floor
    and the length guard, scores 0.948 and VERIFIES — and renders to a human
    reviewer as the word "fabricated".

    The honest statement of what this fix does and does not do: the scrub does
    NOT drop the score (measured 0.948 -> 0.952 after stripping — the appended
    fabrication is 10 visible characters either way, and a short append onto a
    long chunk is inside ``partial_ratio``'s existing tolerance, which is a
    separate, known property of the 0.85 bar). What it removes is the ability
    to make that appended text RENDER as plausible English. The quote a
    reviewer sees is the quote that was scored.
    """
    rlo = chr(0x202E)
    chunk = _FLOOR_CHUNK
    quote = chunk[:60] + rlo + "detacirbaf"
    req = RequirementEvidence(
        requirement="API design",
        status="met",
        evidence=quote,
        evidence_chunk_ids=["c_1"],
        confidence=0.9,
    )
    cleaned = verify_evidence(
        EvidenceObject(requirements=[req]), {"c_1": chunk}, weights=DEFAULT_WEIGHTS
    )
    surfaced = cleaned.requirements[0].evidence
    assert rlo not in surfaced
    assert all(ch not in surfaced for ch in _NON_COLLAPSING_INVISIBLES)


# ── ROADMAP A6 siblings: marking the three fallback branches must not move
# ── any existing number (spec's explicit "no-arithmetic-change" guard) ─────


def test_a6_siblings_no_arithmetic_change_pin() -> None:
    """Adding the three write-path markers (``experience_bar_stated``,
    ``education_bar_stated``, ``education_readable``) must not change a
    single ``score_experience``/``score_education`` return value, on any
    branch. Every value pinned here is the SAME value the pre-existing,
    per-branch tests above already pin individually -- this is a single
    consolidated re-statement of them, the guard the spec calls for, so a
    change to either scorer's arithmetic (as opposed to its disclosure) is
    caught even if a future edit only touches this file."""
    # score_experience: the no-bar fallback, both None and 0.
    assert score_experience(5, None, weights=DEFAULT_WEIGHTS) == 1.0
    assert score_experience(5, 0, weights=DEFAULT_WEIGHTS) == 1.0
    # score_experience: real comparisons across every branch.
    assert score_experience(0, 5, weights=DEFAULT_WEIGHTS) == 0.0
    assert score_experience(5, 5, weights=DEFAULT_WEIGHTS) == 1.0
    assert score_experience(2, 4, weights=DEFAULT_WEIGHTS) == pytest.approx(0.5)
    assert score_experience(15, 5, weights=DEFAULT_WEIGHTS) == pytest.approx(0.9)
    assert score_experience(100, 5, weights=DEFAULT_WEIGHTS) == pytest.approx(0.8)

    # score_education: the no-bar fallback, both None and "".
    assert score_education(["bachelors"], None, weights=DEFAULT_WEIGHTS) == 1.0
    assert score_education(["bachelors"], "", weights=DEFAULT_WEIGHTS) == 1.0
    # score_education: the unreadable-education fallback -- the strongest of
    # the three siblings, and the one whose number this guard most needs to
    # keep honest.
    assert score_education([], "bachelors", weights=DEFAULT_WEIGHTS) == 0.0
    assert score_education([None, None], "bachelors", weights=DEFAULT_WEIGHTS) == 0.0
    # score_education: real comparisons across every branch.
    assert score_education(["bachelors"], "bachelors", weights=DEFAULT_WEIGHTS) == 1.0
    assert (
        score_education(["masters", "associate"], "bachelors", weights=DEFAULT_WEIGHTS)
        == 1.0
    )
    assert score_education(
        ["associate"], "bachelors", weights=DEFAULT_WEIGHTS
    ) == pytest.approx(0.5 * (2 / 3))


def test_a6_education_levels_materialised_before_readability_consumes_it() -> None:
    """Mutation guard (merge-blocking review, Gap 2): ``score_education``
    does ``levels = list(candidate_levels)`` BEFORE calling
    ``education_levels_readable(levels)``, specifically so a one-shot
    iterable (the ``Iterable[str | None]`` the signature actually promises,
    not just the lists every other test in this file happens to pass) is
    materialised once and backs BOTH the readability check and the
    ``ranked`` build below it.

    Delete that ``list(...)`` and this breaks: ``education_levels_readable``
    drains the generator during the readability check, so by the time
    ``ranked`` is built from the SAME (now-exhausted) generator there is
    nothing left, ``max()`` on the resulting empty sequence raises
    ``ValueError``, and a genuinely readable, above-the-bar candidate
    crashes instead of scoring ``1.0`` -- every other test in this module
    passes a list and cannot see this at all."""

    def _one_shot_bachelors() -> Any:
        yield "bachelors"

    generator_result = score_education(
        _one_shot_bachelors(), "bachelors", weights=DEFAULT_WEIGHTS
    )
    list_result = score_education(["bachelors"], "bachelors", weights=DEFAULT_WEIGHTS)
    assert generator_result == list_result == 1.0, (
        "a one-shot generator of levels must score identically to the "
        "equivalent list -- if it does not (or raises), `candidate_levels` "
        "is being consumed more than once"
    )
