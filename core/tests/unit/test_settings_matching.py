"""Unit tests for Phase 4c's matching-config settings wiring (ADR 0021 port).

Ports hris's ``pipeline.config.Settings`` ``match_*`` block and
``pipeline.matching.config.weights_from_settings`` bridge (see
``hris/packages/pipeline/src/pipeline/config.py`` lines 99-159 and
``hris/packages/pipeline/src/pipeline/matching/config.py``) into
recruiter-assistant's single ``src/settings.py``. Neither the fields nor the
builder function exist yet in this repo — this file is RED by construction:
every test below fails at collection/import time with
``ImportError: cannot import name 'weights_from_settings' from 'src.settings'``
until the green step adds them.

Two things this file pins deliberately DIFFERENT from a naive hris copy:

* ``weights_from_settings`` lives in ``src/settings.py`` itself (recruiter-
  assistant has no separate ``matching/config.py`` module — CLAUDE.md's "config
  only via src/settings.py" applies here), not a sibling module the way hris
  splits ``pipeline.config`` from ``pipeline.matching.config``.
* ``match_reverse_evidence_k`` defaults to hris's WORKER-path value (10), not
  the old synchronous-endpoint value (0). hris itself already made this switch
  in ADR 0023 ("reverse match is now computed by the WORKER off the request
  path ... default > 0"; the pre-0023 default was ``_REVERSE_EVIDENCE_K = 0``,
  see ADR 0023 sec. "Context"). recruiter-assistant has no synchronous
  reverse-match endpoint at all (there is nothing to protect from LLM fan-out
  on a proxied request), so it must inherit hris's CURRENT (worker-path, >0)
  default, not hris's now-superseded synchronous-endpoint default of 0 —
  blocker #10.
"""

from __future__ import annotations

import pytest
from pytest import MonkeyPatch

from src.schemas.matching import DEFAULT_WEIGHTS, MatchWeights
from src.settings import Settings, weights_from_settings

# ── 1. match_* fields exist with hris-identical defaults ────────────────────
# Every default below is copied verbatim from
# hris/packages/pipeline/src/pipeline/config.py lines 99-159.


def test_match_top_blend_defaults() -> None:
    s = Settings()
    assert s.match_structured == 0.6
    assert s.match_evidence == 0.3
    assert s.match_motivation == 0.1


def test_match_sub_weight_defaults() -> None:
    s = Settings()
    assert s.match_skill == 0.40
    assert s.match_experience == 0.25
    assert s.match_education == 0.10
    assert s.match_seniority == 0.15
    assert s.match_vector == 0.10


def test_match_must_have_and_relief_defaults() -> None:
    s = Settings()
    assert s.match_must_have_miss_penalty == 0.5
    assert s.match_implied_experience_relief == 0.75


def test_match_recency_defaults() -> None:
    s = Settings()
    assert s.match_recency_recent_years == 2
    assert s.match_recency_mid_years == 5
    assert s.match_recency_recent == 1.0
    assert s.match_recency_mid == 0.7
    assert s.match_recency_old == 0.4


def test_match_overqual_defaults() -> None:
    s = Settings()
    assert s.match_overqual_ratio == 2.0
    assert s.match_overqual_slope == 0.1
    assert s.match_overqual_floor == 0.8


def test_match_education_and_seniority_curve_defaults() -> None:
    s = Settings()
    assert s.match_education_partial == 0.5
    assert s.match_seniority_floor == 0.5
    assert s.match_implied_seniority_factor == 1.5
    assert s.match_implied_min_coverage == 0.5
    assert s.match_education_field_fuzz == 0.85


def test_match_evidence_and_motivation_confidence_defaults() -> None:
    s = Settings()
    assert s.match_evidence_met_confidence == 0.7
    assert s.match_evidence_partial_weight == 0.5
    assert s.match_evidence_verify_fuzz == 0.85
    # Lowered 32 -> 16 by human decision (security FINDING 4): at 32 the floor
    # blanked genuine short credentials ("PhD in Computer Science", 23) and
    # demoted them met -> missing, indistinguishably from fabrication.
    assert s.match_evidence_min_quote_chars == 16
    assert s.match_motivation_min_confidence == 0.7


def test_match_family_and_non_matchable_families_defaults() -> None:
    s = Settings()
    assert s.match_family_weight == 0.5
    assert s.match_non_matchable_families == "other,domain"


def test_match_use_classified_families_default_is_false() -> None:
    """ROADMAP A2, Phase 3.3 slice 2 (2026-08-19 decision memo): the
    parse-time classifier's output must never move ranking until this flag
    is explicitly switched on -- live measurement against the real tailnet
    gpt-oss:20b found stable mis-families ("expert scientific knowledge of
    MRI and MEG research methods" -> bi) and family credit is transitive
    across the whole résumé. Record the fact, defer the scoring change."""
    assert Settings().match_use_classified_families is False


def test_env_override_of_match_use_classified_families(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("MATCH_USE_CLASSIFIED_FAMILIES", "true")
    assert Settings().match_use_classified_families is True


def test_match_coarse_and_evidence_k_defaults() -> None:
    s = Settings()
    assert s.match_coarse_k == 50
    assert s.match_evidence_k == 15


def test_match_llm_concurrency_and_max_tokens_defaults() -> None:
    s = Settings()
    assert s.match_llm_concurrency == 4
    assert s.match_evidence_max_tokens == 2048


@pytest.mark.parametrize(
    "field_name",
    [
        "match_structured",
        "match_evidence",
        "match_motivation",
        "match_skill",
        "match_experience",
        "match_education",
        "match_seniority",
        "match_vector",
        "match_must_have_miss_penalty",
        "match_implied_experience_relief",
        "match_recency_recent_years",
        "match_recency_mid_years",
        "match_recency_recent",
        "match_recency_mid",
        "match_recency_old",
        "match_overqual_ratio",
        "match_overqual_slope",
        "match_overqual_floor",
        "match_education_partial",
        "match_education_field_fuzz",
        "match_seniority_floor",
        "match_implied_seniority_factor",
        "match_implied_min_coverage",
        "match_evidence_met_confidence",
        "match_evidence_partial_weight",
        "match_evidence_verify_fuzz",
        "match_evidence_min_quote_chars",
        "match_motivation_min_confidence",
        "match_family_weight",
        "match_non_matchable_families",
        "match_use_classified_families",
        "match_coarse_k",
        "match_evidence_k",
        "match_reverse_evidence_k",
        "match_llm_concurrency",
        "match_evidence_max_tokens",
    ],
)
def test_every_match_field_is_declared_on_settings(field_name: str) -> None:
    """Every hris ``match_*`` knob must exist as a declared pydantic field (not
    merely settable via extra="ignore" env passthrough)."""
    assert field_name in Settings.model_fields


# ── 2. weights_from_settings builds a valid MatchWeights ────────────────────


def test_weights_from_settings_returns_match_weights_instance() -> None:
    weights = weights_from_settings(Settings())
    assert isinstance(weights, MatchWeights)


def test_weights_from_settings_defaults_equal_default_weights() -> None:
    # An unconfigured deployment must rank identically to the hardcoded
    # DEFAULT_WEIGHTS that shipped before this settings bridge existed.
    weights = weights_from_settings(Settings())
    assert weights == DEFAULT_WEIGHTS


def test_weights_from_settings_fields_equal_settings_values() -> None:
    s = Settings()
    weights = weights_from_settings(s)
    assert weights.structured == s.match_structured
    assert weights.evidence == s.match_evidence
    assert weights.motivation == s.match_motivation
    assert weights.skill == s.match_skill
    assert weights.experience == s.match_experience
    assert weights.education == s.match_education
    assert weights.seniority == s.match_seniority
    assert weights.vector == s.match_vector
    assert weights.must_have_miss_penalty == s.match_must_have_miss_penalty
    assert weights.implied_experience_relief == s.match_implied_experience_relief
    assert weights.recency_recent_years == s.match_recency_recent_years
    assert weights.recency_mid_years == s.match_recency_mid_years
    assert weights.recency_recent == s.match_recency_recent
    assert weights.recency_mid == s.match_recency_mid
    assert weights.recency_old == s.match_recency_old
    assert weights.overqual_ratio == s.match_overqual_ratio
    assert weights.overqual_slope == s.match_overqual_slope
    assert weights.overqual_floor == s.match_overqual_floor
    assert weights.education_partial == s.match_education_partial
    assert weights.seniority_floor == s.match_seniority_floor
    assert weights.implied_seniority_factor == s.match_implied_seniority_factor
    assert weights.implied_min_coverage == s.match_implied_min_coverage
    assert weights.evidence_met_confidence == s.match_evidence_met_confidence
    assert weights.evidence_partial_weight == s.match_evidence_partial_weight
    assert weights.evidence_verify_fuzz == s.match_evidence_verify_fuzz
    assert weights.evidence_min_quote_chars == s.match_evidence_min_quote_chars
    assert weights.motivation_min_confidence == s.match_motivation_min_confidence
    assert weights.education_field_fuzz == s.match_education_field_fuzz


def test_weights_from_settings_passes_its_own_sums_to_one_validator() -> None:
    # MatchWeights' model_validator(mode="after") _sums_close_to_one runs on
    # construction; if weights_from_settings mis-maps a field this raises
    # instead of silently producing a skewed ranking.
    weights = weights_from_settings(Settings())
    top = weights.structured + weights.evidence + weights.motivation
    sub = (
        weights.skill
        + weights.experience
        + weights.education
        + weights.seniority
        + weights.vector
    )
    assert top == pytest.approx(1.0, abs=0.01)
    assert sub == pytest.approx(1.0, abs=0.01)


def test_weights_from_settings_rejects_unbalanced_top_weights() -> None:
    # 0.5 + 0.3 + 0.1 = 0.9 != 1.0 -> MatchWeights' validator must reject a
    # misconfigured .env at the start of a run, not absorb it silently.
    with pytest.raises(ValueError, match=r"must sum to 1.0"):
        weights_from_settings(Settings(match_structured=0.5))


def test_weights_from_settings_rejects_unbalanced_sub_weights() -> None:
    with pytest.raises(ValueError, match=r"must sum to 1.0"):
        weights_from_settings(Settings(match_skill=0.9))


def test_weights_from_settings_rejects_relief_below_penalty() -> None:
    with pytest.raises(ValueError, match=r"implied_experience_relief must be >="):
        weights_from_settings(
            Settings(
                match_must_have_miss_penalty=0.5,
                match_implied_experience_relief=0.3,
            )
        )


# ── 3. match_reverse_evidence_k: worker-path default, NOT the synchronous- ──
#        endpoint value (blocker #10) ────────────────────────────────────────


def test_match_reverse_evidence_k_default_is_worker_path_value() -> None:
    """recruiter-assistant has no synchronous reverse-match endpoint (there is
    nothing on the request path to protect from per-role LLM fan-out), so it
    must inherit hris's CURRENT worker-path default (> 0, pinned at 10 per
    hris/packages/pipeline/src/pipeline/config.py:156 and
    hris ADR 0023 "the configurable match_reverse_evidence_k ... now defaults
    > 0 (evidence on)"), never hris's now-superseded synchronous-endpoint
    default of 0 (pre-ADR-0023 `_REVERSE_EVIDENCE_K = 0`)."""
    assert Settings().match_reverse_evidence_k == 10


def test_match_reverse_evidence_k_default_is_positive() -> None:
    assert Settings().match_reverse_evidence_k > 0


# ── 4. Overrides (env / constructor) propagate through weights_from_settings ─


def test_constructor_override_of_top_blend_flows_through() -> None:
    weights = weights_from_settings(
        Settings(match_structured=0.7, match_motivation=0.0)
    )
    assert weights.structured == 0.7
    assert weights.motivation == 0.0
    assert weights.evidence == 0.3


def test_constructor_override_of_implied_experience_relief_flows_through() -> None:
    weights = weights_from_settings(Settings(match_implied_experience_relief=0.9))
    assert weights.implied_experience_relief == 0.9


def test_constructor_override_of_scoring_curve_tunables_flows_through() -> None:
    weights = weights_from_settings(
        Settings(
            match_recency_recent=0.9,
            match_recency_mid_years=4,
            match_overqual_floor=0.5,
            match_overqual_slope=0.2,
            match_education_partial=0.4,
            match_seniority_floor=0.3,
            match_implied_seniority_factor=2.0,
            match_implied_min_coverage=0.6,
            match_evidence_met_confidence=0.8,
            match_evidence_partial_weight=0.25,
            match_evidence_verify_fuzz=0.95,
            match_evidence_min_quote_chars=64,
            match_motivation_min_confidence=0.6,
        )
    )
    assert weights.recency_recent == 0.9
    assert weights.recency_mid_years == 4
    assert weights.overqual_floor == 0.5
    assert weights.overqual_slope == 0.2
    assert weights.education_partial == 0.4
    assert weights.seniority_floor == 0.3
    assert weights.implied_seniority_factor == 2.0
    assert weights.implied_min_coverage == 0.6
    assert weights.evidence_met_confidence == 0.8
    assert weights.evidence_partial_weight == 0.25
    assert weights.evidence_verify_fuzz == 0.95
    assert weights.evidence_min_quote_chars == 64
    assert weights.motivation_min_confidence == 0.6


def test_env_override_of_match_reverse_evidence_k(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("MATCH_REVERSE_EVIDENCE_K", "0")
    assert Settings().match_reverse_evidence_k == 0


def test_env_override_of_match_family_weight(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("MATCH_FAMILY_WEIGHT", "0.75")
    assert Settings().match_family_weight == pytest.approx(0.75)


def test_env_override_of_match_evidence_verify_fuzz_flows_into_weights(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("MATCH_EVIDENCE_VERIFY_FUZZ", "0.5")
    weights = weights_from_settings(Settings())
    assert weights.evidence_verify_fuzz == pytest.approx(0.5)


def test_env_override_of_match_education_field_fuzz_flows_into_weights(
    monkeypatch: MonkeyPatch,
) -> None:
    """ADR-028's field-of-study fuzzy-match threshold, wired the same way its
    sibling ``evidence_verify_fuzz`` is: a non-default env value must reach
    ``weights_from_settings(Settings()).education_field_fuzz``, not just the
    declared-but-unwired ``Settings`` field."""
    monkeypatch.setenv("MATCH_EDUCATION_FIELD_FUZZ", "0.6")
    weights = weights_from_settings(Settings())
    assert weights.education_field_fuzz == pytest.approx(0.6)


def test_env_override_of_match_evidence_min_quote_chars_flows_into_weights(
    monkeypatch: MonkeyPatch,
) -> None:
    """Mutation pin (reviewer round 2, MAJOR 1). Deleting
    ``evidence_min_quote_chars=settings.match_evidence_min_quote_chars`` from
    ``weights_from_settings`` used to pass the ENTIRE suite: the knob was
    declared, documented and defaulted, but nothing connected it to the
    ``MatchWeights`` the verifier actually reads, so ``MATCH_EVIDENCE_MIN_
    QUOTE_CHARS`` could silently become a no-op while looking configurable.

    Its two siblings — ``evidence_verify_fuzz`` above and
    ``evidence_met_confidence`` — were both pinned; this one was in none of
    the three field lists in this file either. The value chosen differs from
    the default (16) in BOTH directions from any plausible typo, so a mapping
    that reads the wrong setting cannot coincidentally pass.
    """
    monkeypatch.setenv("MATCH_EVIDENCE_MIN_QUOTE_CHARS", "77")
    weights = weights_from_settings(Settings())
    assert weights.evidence_min_quote_chars == 77


def test_env_override_of_match_evidence_min_quote_chars_reaches_the_verifier(
    monkeypatch: MonkeyPatch,
) -> None:
    """The knob is only real if the wired value changes behaviour. At a floor
    of 200 a genuine 71-char verbatim span must be scrubbed; at the default 16
    the same span verifies."""
    from src.pipeline.matching.stages import verify_evidence
    from src.schemas.matching import EvidenceObject, RequirementEvidence

    chunk = (
        "Designed and shipped Python REST APIs consumed by 40+ internal "
        "services at Nimbus Analytics Inc."
    )
    quote = "Designed and shipped Python REST APIs consumed by 40+ internal services"

    def _run(weights: MatchWeights) -> str:
        req = RequirementEvidence(
            requirement="Python",
            status="met",
            evidence=quote,
            evidence_chunk_ids=["c_001"],
            confidence=0.9,
        )
        cleaned = verify_evidence(
            EvidenceObject(requirements=[req]), {"c_001": chunk}, weights=weights
        )
        return cleaned.requirements[0].evidence

    assert _run(weights_from_settings(Settings())) == quote

    monkeypatch.setenv("MATCH_EVIDENCE_MIN_QUOTE_CHARS", "200")
    assert _run(weights_from_settings(Settings())) == ""
