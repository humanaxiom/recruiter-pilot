"""Unit tests for ``src/schemas/matching.py`` — the ranking-pipeline contracts.

Phase 2 ports the KEEP set of hris ``packages/schemas/src/schemas/matching.py``
and DELETES the review workflow. These tests pin, as merge-blocking contracts:

* the CUT set (review/2nd-review types) is NOT importable — a guard against the
  review workflow creeping back in,
* ``ShortlistEntry`` loses ``current_decision`` / ``current_stage`` but keeps the
  blind-review fields ``blinded`` / ``display_label``,
* the ``MatchWeights`` ranking contract: exact defaults, the ``_sums_close_to_one``
  validator (top trio + sub-five each sum to 1.0; relief ≥ penalty), and frozen,
* the jsonb-stored shapes (``ScoreBreakdown`` / ``EvidenceObject`` / ``PipelineMeta``)
  round-trip faithfully,
* ``extra="forbid"``/``"ignore"`` behave per model.

These modules do not exist yet — this is the RED half of the TDD cycle.
"""

from __future__ import annotations

import datetime as dt
import json
from uuid import uuid4

import pytest
from pydantic import BaseModel, ValidationError

import src.schemas as schemas_pkg
import src.schemas.matching as matching_mod
from src.schemas.matching import (
    DEFAULT_WEIGHTS,
    CleanText,
    CoverLetterEvidence,
    EvidenceObject,
    JobMatchEntry,
    JobMatchResultOut,
    MatchWeights,
    PipelineMeta,
    RequirementEvidence,
    ScoreBreakdown,
    ShortlistEntry,
    SkillContribution,
)

_TS = dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.UTC)

# The KEEP public surface the module and package must expose.
MATCHING_PUBLIC: tuple[str, ...] = (
    "EvidenceStatus",
    "SkillCategory",
    "CoverLetterTheme",
    "MatchWeights",
    "DEFAULT_WEIGHTS",
    "SkillContribution",
    "ScoreBreakdown",
    "RequirementEvidence",
    "CoverLetterEvidence",
    "EvidenceObject",
    "PipelineMeta",
    "ShortlistEntry",
    "JobMatchEntry",
    "JobMatchResultOut",
)

# The review workflow — CUT entirely. None of these may be importable from the
# module OR re-exported by the package (merge-blocking against review creep).
CUT_MATCHING: tuple[str, ...] = (
    "PipelineStage",
    "TERMINAL_STAGES",
    "DispositionReason",
    "DecisionKind",
    "ShortlistDecisionCreate",
    "ShortlistDecisionOut",
    "StageTransitionCreate",
    "StageTransitionOut",
)

# The exact DEFAULT_WEIGHTS the plan's ranking algorithm depends on.
EXPECTED_DEFAULTS: tuple[tuple[str, float], ...] = (
    ("structured", 0.6),
    ("evidence", 0.3),
    ("motivation", 0.1),
    ("skill", 0.40),
    ("experience", 0.25),
    ("education", 0.10),
    ("seniority", 0.15),
    ("vector", 0.10),
    ("evidence_verify_fuzz", 0.85),
    ("education_field_fuzz", 0.85),
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _score_breakdown() -> ScoreBreakdown:
    return ScoreBreakdown(
        skill=0.5,
        experience=0.5,
        education=0.5,
        seniority=0.5,
        vector=0.5,
        structured=0.5,
    )


# ── Public surface / re-export ───────────────────────────────────────────────


@pytest.mark.parametrize("name", MATCHING_PUBLIC)
def test_module_exposes_keep_name(name: str) -> None:
    assert hasattr(matching_mod, name)


@pytest.mark.parametrize("name", MATCHING_PUBLIC)
def test_package_reexports_keep_name(name: str) -> None:
    assert hasattr(schemas_pkg, name)


# ── Cut-scope guard (merge-blocking against review-workflow creep) ───────────


@pytest.mark.parametrize("name", CUT_MATCHING)
def test_cut_review_type_is_not_importable_from_module(name: str) -> None:
    assert not hasattr(matching_mod, name)


@pytest.mark.parametrize("name", CUT_MATCHING)
def test_cut_review_type_is_not_reexported_by_package(name: str) -> None:
    assert not hasattr(schemas_pkg, name)


@pytest.mark.parametrize("name", CUT_MATCHING)
def test_cut_review_type_is_not_in_module_all(name: str) -> None:
    assert name not in getattr(matching_mod, "__all__", [])


@pytest.mark.parametrize("field", ["current_decision", "current_stage"])
def test_shortlist_entry_drops_review_fields(field: str) -> None:
    assert field not in ShortlistEntry.model_fields


@pytest.mark.parametrize("field", ["blinded", "display_label"])
def test_shortlist_entry_keeps_blind_review_fields(field: str) -> None:
    """Blind review is v1 scope — NOT the cut 2nd-review workflow."""
    assert field in ShortlistEntry.model_fields


# ── MatchWeights: the ranking contract ───────────────────────────────────────


def test_default_weights_is_a_valid_matchweights() -> None:
    assert isinstance(DEFAULT_WEIGHTS, MatchWeights)


@pytest.mark.parametrize("field, value", EXPECTED_DEFAULTS)
def test_default_weights_exact_values(field: str, value: float) -> None:
    assert getattr(DEFAULT_WEIGHTS, field) == value


def test_matchweights_rejects_top_trio_not_summing_to_one() -> None:
    """structured+evidence+motivation must sum to 1.0 — 0.7+0.3+0.1 = 1.1."""
    with pytest.raises(ValidationError):
        MatchWeights(structured=0.7, evidence=0.3, motivation=0.1)


def test_matchweights_rejects_sub_five_not_summing_to_one() -> None:
    """skill+experience+education+seniority+vector must sum to 1.0."""
    with pytest.raises(ValidationError):
        MatchWeights(skill=0.50)  # 0.50+0.25+0.10+0.15+0.10 = 1.10


def test_matchweights_rejects_relief_below_penalty() -> None:
    with pytest.raises(ValidationError):
        MatchWeights(implied_experience_relief=0.4, must_have_miss_penalty=0.5)


def test_matchweights_accepts_relief_equal_to_penalty() -> None:
    """== disables the relief while keeping the flag — must be allowed."""
    weights = MatchWeights(implied_experience_relief=0.5, must_have_miss_penalty=0.5)
    assert weights.implied_experience_relief == 0.5


@pytest.mark.parametrize("field, bad", [("structured", 1.5), ("skill", -0.1)])
def test_matchweights_field_bounds(field: str, bad: float) -> None:
    with pytest.raises(ValidationError):
        MatchWeights(**{field: bad})


def test_matchweights_is_frozen() -> None:
    weights = MatchWeights()
    with pytest.raises(ValidationError):
        weights.structured = 0.5  # type: ignore[misc]


# ── education_field_fuzz: field-of-study fuzzy-match threshold (ADR-028) ────
# A THRESHOLD knob, sibling to evidence_verify_fuzz — deliberately NOT part
# of either the top trio or the sub-five, so setting it must never perturb
# the _sums_close_to_one validator.


def test_matchweights_education_field_fuzz_default_is_085() -> None:
    assert MatchWeights().education_field_fuzz == 0.85


def test_matchweights_education_field_fuzz_is_configurable() -> None:
    weights = MatchWeights(education_field_fuzz=0.5)
    assert weights.education_field_fuzz == 0.5


def test_matchweights_education_field_fuzz_out_of_range_rejected() -> None:
    with pytest.raises(ValidationError):
        MatchWeights(education_field_fuzz=1.5)


def test_matchweights_education_field_fuzz_does_not_break_sums_to_one_validator() -> (
    None
):
    """It is a threshold, not a weight — it must not appear in either sum the
    ``_sums_close_to_one`` validator enforces, so an off-default value must
    still construct cleanly and the sums must be untouched."""
    weights = MatchWeights(education_field_fuzz=0.99)
    assert weights.education_field_fuzz == 0.99
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


# ── Sub-score / evidence models ──────────────────────────────────────────────


def test_skill_contribution_minimal_valid() -> None:
    contrib = SkillContribution(skill="Python", score=0.9)
    assert contrib.is_must_have is False


def test_score_breakdown_minimal_valid() -> None:
    sb = _score_breakdown()
    assert sb.motivation == 0.0  # default
    assert sb.implied_experience is False


def test_requirement_evidence_defaults() -> None:
    ev = RequirementEvidence(requirement="Python")
    assert ev.status == "missing"
    assert ev.confidence == 0.0


def test_cover_letter_evidence_minimal_valid() -> None:
    cle = CoverLetterEvidence(theme="motivation")
    assert cle.evidence == ""


def test_job_match_entry_minimal_valid() -> None:
    entry = JobMatchEntry(
        job_id=uuid4(),
        title="Backend Engineer",
        rank=1,
        score_final=0.8,
        score_structured=0.7,
        score_evidence=0.6,
        score_breakdown=_score_breakdown(),
        requirement_count=5,
        must_have_count=2,
    )
    assert entry.department is None


def test_job_match_result_out_defaults_empty() -> None:
    out = JobMatchResultOut(resume_id=uuid4())
    assert out.entries == []
    assert out.generated_at is None


def test_shortlist_entry_minimal_valid() -> None:
    entry = ShortlistEntry(
        id=uuid4(),
        job_id=uuid4(),
        resume_id=uuid4(),
        rank=1,
        score_final=0.9,
        score_breakdown=_score_breakdown(),
        evidence=None,
        generated_at=_TS,
    )
    assert entry.blinded is False
    assert entry.display_label is None


def _shortlist_entry_payload(field: str, value: float | None) -> dict[str, object]:
    return {
        "id": uuid4(),
        "job_id": uuid4(),
        "resume_id": uuid4(),
        "rank": 1,
        "score_final": 0.9,
        "score_breakdown": _score_breakdown(),
        "evidence": None,
        "generated_at": _TS,
        field: value,
    }


@pytest.mark.parametrize("field", ["score_structured", "score_evidence"])
@pytest.mark.parametrize("bad", [1.5, -0.1, float("inf"), float("-inf"), float("nan")])
def test_shortlist_entry_composed_subscore_bounds(field: str, bad: float) -> None:
    """The two composed sub-scores are bounded ``ge=0, le=1`` like every field
    on ``ScoreBreakdown``/``MatchWeights``.

    The load-bearing cases are ``inf``/``nan``. ``_folded_subscore`` degrades
    only NON-numeric jsonb, so a stored ``"Infinity"``/``"NaN"`` literal parses
    to a real float and rides onto this DTO unchallenged; the explanation
    panel's ``pct()`` macro (``(v * 100)|round|int``) then raises
    ``OverflowError``/``ValueError`` out of Jinja's ``int`` filter, which the
    filter does NOT catch -> an unhandled 500 on a compliance page. Rejecting
    the value here routes it through the entry-detail route's existing
    ``ValidationError`` fallback instead."""
    with pytest.raises(ValidationError):
        ShortlistEntry.model_validate(_shortlist_entry_payload(field, bad))


@pytest.mark.parametrize("field", ["score_structured", "score_evidence"])
@pytest.mark.parametrize("good", [0.0, 0.5, 1.0, None])
def test_shortlist_entry_composed_subscore_accepts_the_legal_range(
    field: str, good: float | None
) -> None:
    """POSITIVE CONTROL for the bound above — the endpoints and ``None`` ("not
    recorded") must all still be accepted, or the bound would have broken the
    honest-absence contract it sits beside."""
    entry = ShortlistEntry.model_validate(_shortlist_entry_payload(field, good))
    assert getattr(entry, field) == good


# ── jsonb round-trip fidelity (stored verbatim in Postgres jsonb) ────────────


def test_score_breakdown_roundtrips_faithfully() -> None:
    sb = ScoreBreakdown(
        skill=0.5,
        experience=0.4,
        education=0.3,
        seniority=0.2,
        vector=0.1,
        structured=0.6,
        motivation=0.1,
        implied_experience=True,
        skill_contributions=[
            SkillContribution(skill="Python", score=0.9, is_must_have=True)
        ],
    )
    again = ScoreBreakdown.model_validate(sb.model_dump())
    assert again.model_dump() == sb.model_dump()


def test_evidence_object_roundtrips_faithfully() -> None:
    ev = EvidenceObject(
        requirements=[
            RequirementEvidence(requirement="Python", status="met", confidence=0.9)
        ],
        overall_summary="Strong fit",
        cover_letter_presence=True,
        cover_letter_evidence=[CoverLetterEvidence(theme="motivation", confidence=0.8)],
        overall_motivation="Keen",
    )
    again = EvidenceObject.model_validate(ev.model_dump())
    assert again.model_dump() == ev.model_dump()


def test_pipeline_meta_roundtrips_faithfully() -> None:
    meta = PipelineMeta(
        model_gen="gpt-oss:20b",
        model_emb="nomic-embed-text",
        prompt_versions={"resume_core": "v1"},
        weights=MatchWeights(),
        generated_at=_TS,
        timings_ms={"structured": 12},
    )
    again = PipelineMeta.model_validate(meta.model_dump())
    assert again.model_dump() == meta.model_dump()


# ── extra="forbid" vs extra="ignore" ─────────────────────────────────────────


def test_score_breakdown_forbids_unknown_key() -> None:
    with pytest.raises(ValidationError):
        ScoreBreakdown.model_validate(
            {
                "skill": 0.5,
                "experience": 0.5,
                "education": 0.5,
                "seniority": 0.5,
                "vector": 0.5,
                "structured": 0.5,
                "bogus": 1,
            }
        )


def test_matchweights_forbids_unknown_key() -> None:
    with pytest.raises(ValidationError):
        MatchWeights.model_validate({"bogus": 1})


def test_evidence_object_ignores_unknown_key() -> None:
    ev = EvidenceObject.model_validate({"bogus": 1})
    assert "bogus" not in ev.model_dump()


def test_requirement_evidence_ignores_unknown_key() -> None:
    ev = RequirementEvidence.model_validate({"requirement": "Python", "bogus": 1})
    assert "bogus" not in ev.model_dump()


# ── ADR-022 follow-up #2 — C0 control sanitisation at the SCHEMA boundary ────
#
# The bug (ADR-022 "Follow-up items" #2): a NUL (U+0000) in an LLM-authored
# string survives the verifier, ``json.dumps`` emits it as a lowercase-u
# Unicode escape, and Postgres rejects that escape outright ("unsupported
# Unicode escape sequence ... cannot be converted to text"). The whole
# ``persist_shortlist`` transaction dies, so ONE malformed quote loses the
# ENTIRE shortlist. This is an availability bug, not a cosmetic one.
#
# WHY THESE TESTS TARGET THE SCHEMA AND NOT ``verify_evidence``:
#
#   The written spec (ADR-022 line 104; HANDOFF line 1003) says "Strip C0
#   controls (except newline and tab) in ``verify_evidence``". That instruction
#   is WRONG, and these tests are deliberately built so a spec-literal fix
#   still FAILS. ``verify_evidence`` only ever rewrites the two ``evidence``
#   fields (and only ever to blank them); it never touches ``requirement``,
#   ``overall_summary`` or ``overall_motivation``. All three of those reach
#   ``persist_shortlist`` unmodified, so the transaction still dies and the
#   shortlist is still lost.
#
#   Sanitising at the schema boundary closes all five fields at the single
#   point every LLM response must pass through, and it GENERALISES the
#   NUL-strip idiom already established on the parse path
#   (``src/pipeline/parsing/extract.py`` ``_sanitize``) rather than inventing a
#   second, narrower idiom beside it.
#
# NOTE ON THIS FILE'S OWN SOURCE: control characters are built with ``chr()``
# and never written as literal escapes. ADR-022 records that its first draft
# embedded a literal NUL while describing the bug and git classified the file
# as binary. The same thing happened to the first draft of this test module.
# The byte is easy to propagate by accident — which is the point.

NUL = chr(0x00)

# ADR-022's character class, as an explicit codepoint set. Equivalent to the
# regex the ADR names (C0 controls and DEL, excluding TAB 0x09, LF 0x0a and
# CR 0x0d), written without escapes for the reason above.
C0_STRIPPED_CODEPOINTS: frozenset[int] = frozenset(
    {*range(0x00, 0x09), 0x0B, 0x0C, *range(0x0E, 0x20), 0x7F}
)

# Every LLM-authored free-text field on the evidence models. All five must be
# sanitised — pinning only the two ``evidence`` fields would let the
# spec-literal ``verify_evidence`` fix pass while the bug stayed open.
C0_GUARDED_FIELDS: tuple[tuple[str, str], ...] = (
    ("RequirementEvidence", "evidence"),
    ("RequirementEvidence", "requirement"),
    ("CoverLetterEvidence", "evidence"),
    ("EvidenceObject", "overall_summary"),
    ("EvidenceObject", "overall_motivation"),
)

_C0_IDS = [f"{model}.{field}" for model, field in C0_GUARDED_FIELDS]

# Minimal valid payload per model, so each field can be exercised in isolation.
_C0_MODEL_BASE: dict[str, tuple[type[BaseModel], dict[str, object]]] = {
    "RequirementEvidence": (RequirementEvidence, {"requirement": "Python"}),
    "CoverLetterEvidence": (CoverLetterEvidence, {"theme": "motivation"}),
    "EvidenceObject": (EvidenceObject, {}),
}


def _validate_with(model_name: str, field: str, value: str) -> BaseModel:
    """Build ``model_name`` via ``model_validate`` with ``field`` set to
    ``value`` — the path an LLM JSON response actually takes."""
    model, base = _C0_MODEL_BASE[model_name]
    payload: dict[str, object] = {**base, field: value}
    return model.model_validate(payload)


def _construct_with(model_name: str, field: str, value: str) -> BaseModel:
    """Same, but via direct ``__init__`` — the path in-process callers take."""
    model, base = _C0_MODEL_BASE[model_name]
    kwargs: dict[str, object] = {**base, field: value}
    return model(**kwargs)


@pytest.mark.parametrize("model_name, field", C0_GUARDED_FIELDS, ids=_C0_IDS)
def test_nul_byte_is_stripped_from_every_llm_authored_evidence_field(
    model_name: str, field: str
) -> None:
    """The exact byte that kills the ``persist_shortlist`` transaction."""
    obj = _validate_with(model_name, field, f"before{NUL}after")
    assert getattr(obj, field) == "beforeafter"


@pytest.mark.parametrize("model_name, field", C0_GUARDED_FIELDS, ids=_C0_IDS)
def test_c0_controls_are_stripped_from_every_llm_authored_evidence_field(
    model_name: str, field: str
) -> None:
    """A spread across the class: NUL, BEL, VT, FF, ESC, DEL."""
    dirty = (
        f"a{chr(0x00)}b{chr(0x07)}c{chr(0x0B)}d" f"{chr(0x0C)}e{chr(0x1B)}f{chr(0x7F)}g"
    )
    obj = _validate_with(model_name, field, dirty)
    assert getattr(obj, field) == "abcdefg"


@pytest.mark.parametrize("model_name, field", C0_GUARDED_FIELDS, ids=_C0_IDS)
def test_c0_sanitisation_also_applies_on_direct_construction(
    model_name: str, field: str
) -> None:
    """``model_validate`` and ``__init__`` must not diverge: a field validator
    covers both, an ad-hoc scrub in one calling helper covers neither."""
    obj = _construct_with(model_name, field, f"x{NUL}y")
    assert getattr(obj, field) == "xy"


@pytest.mark.parametrize("model_name, field", C0_GUARDED_FIELDS, ids=_C0_IDS)
def test_newline_and_tab_survive_c0_sanitisation(model_name: str, field: str) -> None:
    """Multi-line quotes are legitimate — the scrub must not flatten them."""
    kept = f"line one{chr(0x0A)}line two{chr(0x09)}column two"
    obj = _validate_with(model_name, field, kept)
    assert getattr(obj, field) == kept


@pytest.mark.parametrize("model_name, field", C0_GUARDED_FIELDS, ids=_C0_IDS)
def test_clean_text_is_returned_unchanged_by_sanitisation(
    model_name: str, field: str
) -> None:
    """Anti-over-scrub: ordinary text, accents, CJK and punctuation the corpus
    actually contains must survive unchanged."""
    clean = "Built FastAPI services — café, 東京, 40+ teams; 99.9% uptime."
    obj = _validate_with(model_name, field, clean)
    assert getattr(obj, field) == clean


@pytest.mark.parametrize("codepoint", range(0x00, 0x21))
def test_every_low_codepoint_matches_the_adr_022_character_class(
    codepoint: int,
) -> None:
    """Exhaustive sweep of 0x00-0x20 against ADR-022's class: stripped iff the
    class contains it. Pins that TAB (0x09), LF (0x0a), CR (0x0d) and SPACE
    (0x20) are the ONLY survivors below 0x21."""
    char = chr(codepoint)
    ev = RequirementEvidence.model_validate(
        {"requirement": "Python", "evidence": f"a{char}b"}
    )
    expected = "ab" if codepoint in C0_STRIPPED_CODEPOINTS else f"a{char}b"
    assert ev.evidence == expected


def test_del_codepoint_is_stripped() -> None:
    """0x7f sits outside the 0x00-0x20 sweep and is inside the class."""
    ev = RequirementEvidence.model_validate(
        {"requirement": "Python", "evidence": f"a{chr(0x7F)}b"}
    )
    assert ev.evidence == "ab"


def test_nested_evidence_object_sanitises_its_child_models() -> None:
    """The real ingest shape: one ``model_validate`` of the whole LLM response.
    The nested ``RequirementEvidence`` / ``CoverLetterEvidence`` rows must be
    sanitised too, not just the two top-level ``overall_*`` strings."""
    ev = EvidenceObject.model_validate(
        {
            "requirements": [
                {
                    "requirement": f"Py{NUL}thon",
                    "status": "met",
                    "evidence": f"Shipped{NUL} Python APIs",
                    "confidence": 0.9,
                }
            ],
            "overall_summary": f"Strong{NUL} fit",
            "cover_letter_presence": True,
            "cover_letter_evidence": [
                {"theme": "motivation", "evidence": f"Eager{NUL} to join"}
            ],
            "overall_motivation": f"Keen{NUL}",
        }
    )
    assert ev.requirements[0].requirement == "Python"
    assert ev.requirements[0].evidence == "Shipped Python APIs"
    assert ev.overall_summary == "Strong fit"
    assert ev.cover_letter_evidence[0].evidence == "Eager to join"
    assert ev.overall_motivation == "Keen"


def test_serialised_evidence_carries_no_unicode_nul_escape() -> None:
    """The availability bug itself, at the boundary that causes it: whatever
    ``persist_shortlist`` hands to ``json.dumps`` must not contain the Unicode
    NUL escape, which Postgres rejects — taking the whole shortlist
    transaction down with it."""
    ev = EvidenceObject.model_validate(
        {
            "requirements": [
                {"requirement": f"Py{NUL}thon", "evidence": f"quote{NUL}here"}
            ],
            "cover_letter_evidence": [
                {"theme": "motivation", "evidence": f"cl{NUL}quote"}
            ],
            "overall_summary": f"sum{NUL}mary",
            "overall_motivation": f"moti{NUL}vation",
        }
    )
    dumped = json.dumps(ev.model_dump())
    nul_escape = chr(0x5C) + "u0000"  # backslash + u0000, the escape PG rejects
    assert nul_escape not in dumped
    assert NUL not in dumped


def test_c0_sanitisation_survives_the_dump_validate_roundtrip() -> None:
    """Sanitised output must be a fixed point: re-validating a dumped model
    changes nothing, so the shape stored in JSONB is stable."""
    ev = EvidenceObject.model_validate(
        {
            "requirements": [
                {"requirement": f"Py{NUL}thon", "evidence": f"a{chr(0x0B)}b"}
            ],
            "overall_summary": f"s{chr(0x1F)}um",
        }
    )
    again = EvidenceObject.model_validate(ev.model_dump())
    assert again.model_dump() == ev.model_dump()
    assert again.requirements[0].requirement == "Python"


# ── security FINDING 1 — Unicode format (Cf) controls in the scrub class ─────
#
# The C0 class above stops at DEL. It leaves every Unicode FORMAT character
# untouched, and two sub-classes of those are a live fabrication vector in a
# quote that a human reviewer is asked to trust:
#
#   * BIDI OVERRIDES AND ISOLATES (U+202A-202E, U+2066-2069). MEASURED against
#     r01's real 148-char chunk c_001: the quote
#     ``chunk[:100] + U+202E + "detacirbaf"`` is 111 characters, clears the
#     minimum-quote-length floor and the length guard, scores 0.948 against its
#     cited chunk and VERIFIES — and then RENDERS TO THE REVIEWER as the
#     English word "fabricated", because U+202E reverses the display order of
#     everything after it. The reviewer reads a word the résumé does not
#     contain, inside a quote the verifier has just certified.
#   * ZERO-WIDTH / INVISIBLE FORMAT CHARACTERS (ZWSP U+200B, WJ U+2060,
#     BOM U+FEFF, SHY U+00AD, MVS U+180E). These carry no text, cannot be seen,
#     and cannot be typed out of a quote by a reviewer who wants to check it.
#
# U+200E / U+200F (LRM / RLM) are in the class too. The security finding
# enumerated only the overrides and isolates, but the marks are the same shape
# of defect — invisible Cf codepoints whose only effect is to reorder the
# display of neighbouring neutral text — so excluding them would leave an
# adjacent hole of identical character. The class is therefore a strict
# SUPERSET of what was asked for, never a subset.
#
# ZWNJ (U+200C) and ZWJ (U+200D) are deliberately NOT stripped: they are
# script-meaningful in Persian/Arabic/Devanagari and inside emoji sequences, so
# removing them would corrupt genuine résumé text. That boundary is pinned
# below rather than left to convention.

# The Cf codepoints added to ADR-022's class by FINDING 1. Written as an
# explicit set, without escapes, for the same reason as C0_STRIPPED_CODEPOINTS.
FORMAT_STRIPPED_CODEPOINTS: frozenset[int] = frozenset(
    {
        0x00AD,  # SOFT HYPHEN
        0x180E,  # MONGOLIAN VOWEL SEPARATOR
        0x200B,  # ZERO WIDTH SPACE
        0x200E,  # LEFT-TO-RIGHT MARK
        0x200F,  # RIGHT-TO-LEFT MARK
        0x2060,  # WORD JOINER
        0x202A,  # LEFT-TO-RIGHT EMBEDDING
        0x202B,  # RIGHT-TO-LEFT EMBEDDING
        0x202C,  # POP DIRECTIONAL FORMATTING
        0x202D,  # LEFT-TO-RIGHT OVERRIDE
        0x202E,  # RIGHT-TO-LEFT OVERRIDE
        0x2066,  # LEFT-TO-RIGHT ISOLATE
        0x2067,  # RIGHT-TO-LEFT ISOLATE
        0x2068,  # FIRST STRONG ISOLATE
        0x2069,  # POP DIRECTIONAL ISOLATE
        0xFEFF,  # ZERO WIDTH NO-BREAK SPACE / BOM
    }
)

# Invisible-ish codepoints that must SURVIVE. The first two are script- and
# emoji-meaningful; the rest are real whitespace that ``_collapse_whitespace``
# already normalises symmetrically on both sides of the fuzzy match, so
# stripping them here would be a second, asymmetric normalisation.
FORMAT_PRESERVED_CODEPOINTS: tuple[int, ...] = (
    0x200C,  # ZERO WIDTH NON-JOINER
    0x200D,  # ZERO WIDTH JOINER
    0x00A0,  # NO-BREAK SPACE
    0x2028,  # LINE SEPARATOR
    0x2029,  # PARAGRAPH SEPARATOR
    0x3000,  # IDEOGRAPHIC SPACE
)

# ``source_context`` joins the guarded set under FINDING 6 — it is the only
# free-text evidence field that carried a bare ``str | None`` annotation.
FORMAT_GUARDED_FIELDS: tuple[tuple[str, str], ...] = (
    *C0_GUARDED_FIELDS,
    ("RequirementEvidence", "source_context"),
)

_FORMAT_IDS = [f"{model}.{field}" for model, field in FORMAT_GUARDED_FIELDS]


@pytest.mark.parametrize("model_name, field", FORMAT_GUARDED_FIELDS, ids=_FORMAT_IDS)
@pytest.mark.parametrize("codepoint", sorted(FORMAT_STRIPPED_CODEPOINTS))
def test_format_control_is_stripped_from_every_llm_authored_evidence_field(
    model_name: str, field: str, codepoint: int
) -> None:
    """Every Cf codepoint in the FINDING 1 class, on every guarded field."""
    obj = _validate_with(model_name, field, f"a{chr(codepoint)}b")
    assert getattr(obj, field) == "ab", (
        f"U+{codepoint:04X} survived on {model_name}.{field} — it is invisible "
        "to a reviewer and must never reach a stored quote"
    )


@pytest.mark.parametrize("codepoint", FORMAT_PRESERVED_CODEPOINTS)
def test_script_meaningful_and_whitespace_codepoints_survive_the_format_scrub(
    codepoint: int,
) -> None:
    """Anti-over-scrub, and the deliberate boundary of the class: ZWNJ/ZWJ are
    script- and emoji-meaningful, and the separators are genuine whitespace
    ``_collapse_whitespace`` already handles symmetrically."""
    kept = f"a{chr(codepoint)}b"
    ev = RequirementEvidence.model_validate({"requirement": "Python", "evidence": kept})
    assert ev.evidence == kept


def test_the_measured_bidi_override_fabrication_is_scrubbed_from_the_quote() -> None:
    """The exact string the security gate measured at 0.948-and-verifying.

    The scrub does not change the fuzzy SCORE (the visible characters are
    untouched, and 'detacirbaf' is still 10 characters of appended
    fabrication). What it removes is the RENDERING deception: with U+202E gone
    the reviewer sees the literal, obviously-junk 'detacirbaf' instead of the
    plausible English word 'fabricated'. That is the whole of FINDING 1 — see
    ``test_matching_stages.py`` for what the fuzzy guards do and do not close.
    """
    rlo = chr(0x202E)
    chunk = (
        "Designed and shipped Python REST APIs consumed by 40+ internal "
        "services at Nimbus Analytics Inc, using FastAPI and asyncio."
    )
    attack = chunk[:100] + rlo + "detacirbaf"
    ev = RequirementEvidence.model_validate(
        {"requirement": "Python", "evidence": attack, "evidence_chunk_ids": ["c_001"]}
    )
    assert rlo not in ev.evidence
    assert ev.evidence == chunk[:100] + "detacirbaf"


def test_serialised_evidence_carries_no_bidi_override() -> None:
    """Boundary that matters: what ``persist_shortlist`` hands to
    ``json.dumps`` — and therefore what any reader ever renders."""
    rlo = chr(0x202E)
    ev = EvidenceObject.model_validate(
        {
            "requirements": [
                {"requirement": f"Py{rlo}thon", "evidence": f"quote{rlo}here"}
            ],
            "cover_letter_evidence": [
                {"theme": "motivation", "evidence": f"cl{rlo}quote"}
            ],
            "overall_summary": f"sum{rlo}mary",
            "overall_motivation": f"moti{rlo}vation",
        }
    )
    dumped = json.dumps(ev.model_dump())
    assert rlo not in dumped
    assert chr(0x5C) + "u202e" not in dumped.lower()


def test_format_scrub_survives_the_dump_validate_roundtrip() -> None:
    """Same fixed-point guarantee the C0 class carries."""
    ev = EvidenceObject.model_validate(
        {
            "requirements": [
                {
                    "requirement": f"Py{chr(0x200B)}thon",
                    "evidence": f"a{chr(0x2066)}b",
                    "source_context": f"c{chr(0xFEFF)}d",
                }
            ],
            "overall_summary": f"s{chr(0x00AD)}um",
        }
    )
    again = EvidenceObject.model_validate(ev.model_dump())
    assert again.model_dump() == ev.model_dump()
    assert again.requirements[0].requirement == "Python"
    assert again.requirements[0].source_context == "cd"


# ── security FINDING 6 — ``source_context`` was the one unannotated field ────


def test_source_context_is_annotated_clean_text() -> None:
    """FINDING 6. ``source_context`` is expanded from ``resumes.parsed`` text
    on the display path; every OTHER free-text evidence field carries
    ``CleanText``. Annotating it structurally (rather than relying on the parse
    path's own NUL-strip) is what keeps the guarantee true for any future
    producer."""
    annotation = RequirementEvidence.model_fields["source_context"].annotation
    assert annotation == (CleanText | None), (
        "source_context must be CleanText | None, not str | None — otherwise "
        "the one display-only evidence field is the one that is not scrubbed"
    )


def test_source_context_is_scrubbed_on_validate_and_on_construction() -> None:
    ev = RequirementEvidence.model_validate(
        {"requirement": "Python", "source_context": f"a{NUL}b{chr(0x202E)}c"}
    )
    assert ev.source_context == "abc"
    direct = RequirementEvidence(
        requirement="Python", source_context=f"x{chr(0x200B)}y"
    )
    assert direct.source_context == "xy"


def test_model_copy_bypasses_clean_text_and_the_docstring_says_so() -> None:
    """FINDING 6's other half. ``model_copy`` / ``model_construct`` do NOT
    re-run validators, so an update dict can put a live control character back
    onto a validated model — measured reaching ``json.dumps`` as the escaped
    form Postgres rejects. There is no live exploit today (``verify_evidence``
    only ever updates ``evidence`` to ``""``), so the response is a documented
    caveat on the model rather than a runtime guard; this test is what stops
    the caveat from being deleted."""
    ev = RequirementEvidence(requirement="Python")
    bypassed = ev.model_copy(update={"requirement": f"x{NUL}y"})
    assert bypassed.requirement == f"x{NUL}y"  # the bypass is real
    doc = RequirementEvidence.__doc__ or ""
    assert "model_copy" in doc, (
        "RequirementEvidence's docstring must record the model_copy/"
        "model_construct CleanText bypass — it is the only mitigation"
    )
