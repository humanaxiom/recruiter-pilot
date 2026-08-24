"""Evidence-integrity RED tests: an UNCITED quote must be scrubbed exactly like
an UNVERIFIABLE one.

``src/pipeline/matching/stages.py::verify_evidence`` has two asymmetric arms:

* the STRICT arm (``evidence and good_ids``) — the quote fuzzy-matches no cited
  chunk at ``weights.evidence_verify_fuzz`` — blanks ``evidence``, demotes
  ``status`` ``met`` -> ``missing``, and caps ``confidence`` at 0.3;
* the LENIENT arm (``evidence and not good_ids``) — the quote has NO surviving
  citation — caps ``confidence`` at 0.3 and does nothing else. The quote TEXT
  survives and ``status="met"`` survives, and the quote is never text-matched
  against anything at all.

``good_ids`` is empty whenever EVERY cited id is hallucinated. So the most
suspicious path there is — a model that invents a chunk id — is the one arm
that escapes the anti-fabrication scrub, and its quote reaches a human reviewer
still labelled "met" through the shortlist read path
(``src/services/shortlist_service.py`` passes ``confidence`` through untouched).

SCOPE — this is a DISPLAY / data-integrity defect, NOT a scoring defect, at
every weighting the ranking-evals gate exercises. The 0.3 confidence cap
already keeps these requirements out of ``_evidence_completeness`` (which needs
``status == "met"`` AND ``confidence >= weights.evidence_met_confidence`` =
0.7), and ``_motivation_score`` already requires a surviving ``cl_`` citation.
The scoring-invariance tests at the bottom of this file pin that.

The invariance is CONDITIONAL on ``evidence_met_confidence > 0.3`` — true of
the 0.7 default and so of the whole corpus, but ``match_evidence_met_confidence``
is env-settable, and below the cap the fix genuinely does move completeness
(1.0 -> 0.0). That boundary is pinned explicitly as a CHANGE at the bottom of
this file rather than left implied. Motivation is unconditionally unchanged:
``_motivation_score`` gates on ``evidence_chunk_ids``, which is empty by
definition in the lenient arm.

NOTE FOR THE IMPLEMENTER — ``test_matching_stages.py::
test_verify_evidence_downgrades_confidence_for_uncited_quote_but_keeps_it``
PINS the defective behaviour ("downgraded, not blanked"). It is provably wrong
under this file and must be updated, not deleted, when the lenient arm is made
strict.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from src.pipeline.matching.stages import (
    _evidence_completeness,
    _motivation_score,
    verify_evidence,
)
from src.schemas.matching import (
    DEFAULT_WEIGHTS,
    CoverLetterEvidence,
    EvidenceObject,
    RequirementEvidence,
)

# ── evals-corpus fixture loading (read-only; mirrors test_matching_stages.py) ─

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

_GOLD_EVIDENCE_CASES: list[tuple[str, str, str]] = [
    (resume_id, skill_name, quote)
    for resume_id, entry in _LABELS["resumes"].items()
    for skill_name, quote in entry.get("gold_evidence", {}).items()
]

# A real chunk + a quote that is genuinely a span of it. Used as the "properly
# cited" control against which the uncited cases are compared.
_REAL_CHUNK_ID = "c_real"
_REAL_CHUNK_TEXT = (
    "Designed and shipped Python REST APIs consumed by 40+ internal services, "
    "using FastAPI with typed request/response contracts."
)
_REAL_QUOTE = "shipped Python REST APIs consumed by 40+ internal services"

_UNCITED_QUOTE = "Led the migration of the billing platform to Kubernetes"


def _one_requirement(evidence_chunk_ids: list[str]) -> RequirementEvidence:
    """A ``met``, high-confidence requirement carrying a quote and the given
    (deliberately unresolvable) citation list."""
    return RequirementEvidence(
        requirement="Python",
        status="met",
        evidence=_UNCITED_QUOTE,
        evidence_chunk_ids=evidence_chunk_ids,
        confidence=0.95,
    )


# ── requirement evidence: the uncited arm must scrub like the fabrication arm ─


@pytest.mark.parametrize(
    "evidence_chunk_ids, case",
    [
        ([], "no citation at all"),
        (["c_999"], "single hallucinated citation"),
        (["c_999", "c_998"], "every citation hallucinated"),
    ],
    ids=["no_citations", "one_fabricated_id", "all_fabricated_ids"],
)
def test_uncited_quote_is_blanked_not_merely_downgraded(
    evidence_chunk_ids: list[str], case: str
) -> None:
    """A quote with no surviving citation was never text-matched against
    anything, so it carries exactly zero verified support and must not be
    surfaced."""
    evidence = EvidenceObject(requirements=[_one_requirement(evidence_chunk_ids)])
    cleaned = verify_evidence(
        evidence, {_REAL_CHUNK_ID: _REAL_CHUNK_TEXT}, weights=DEFAULT_WEIGHTS
    )
    result = cleaned.requirements[0]
    assert result.evidence == "", (
        f"{case}: an unverified quote leaked to the display path "
        f"({result.evidence!r}) — it must be blanked like a fabricated quote"
    )
    assert result.status == "missing", (
        f"{case}: requirement is still labelled {result.status!r}; a quote with "
        "no valid citation cannot substantiate a 'met' claim to a reviewer"
    )
    assert result.confidence <= 0.3
    assert result.evidence_chunk_ids == []


def test_fabricated_citation_id_does_not_escape_the_anti_fabrication_scrub() -> None:
    """The sharpest form of the defect: citing a chunk id that does not exist
    empties ``good_ids``, which routes the MOST suspicious input into the
    LENIENT arm."""
    evidence = EvidenceObject(requirements=[_one_requirement(["c_999_not_a_chunk"])])
    cleaned = verify_evidence(
        evidence, {_REAL_CHUNK_ID: _REAL_CHUNK_TEXT}, weights=DEFAULT_WEIGHTS
    )
    result = cleaned.requirements[0]
    assert (result.evidence, result.status) == ("", "missing")


def test_uncited_and_unverifiable_quotes_reach_the_same_state() -> None:
    """SYMMETRY GUARD — this is the test that stops the asymmetry reappearing.

    Compares ``(evidence, status, confidence)`` only: the two cases legitimately
    differ in ``evidence_chunk_ids`` (the fabricated-quote case keeps its real,
    resolvable citation; the fabricated-citation case has none to keep)."""
    chunks = {_REAL_CHUNK_ID: _REAL_CHUNK_TEXT}

    fabricated_citation = verify_evidence(
        EvidenceObject(requirements=[_one_requirement(["c_999_not_a_chunk"])]),
        chunks,
        weights=DEFAULT_WEIGHTS,
    ).requirements[0]

    fabricated_quote = verify_evidence(
        EvidenceObject(
            requirements=[
                RequirementEvidence(
                    requirement="Python",
                    status="met",
                    evidence=_UNCITED_QUOTE,
                    evidence_chunk_ids=[_REAL_CHUNK_ID],
                    confidence=0.95,
                )
            ]
        ),
        chunks,
        weights=DEFAULT_WEIGHTS,
    ).requirements[0]

    assert (
        fabricated_citation.evidence,
        fabricated_citation.status,
        fabricated_citation.confidence,
    ) == (
        fabricated_quote.evidence,
        fabricated_quote.status,
        fabricated_quote.confidence,
    ), (
        "a fabricated CITATION and a fabricated QUOTE must end in the same "
        "state; any asymmetry rewards the model for inventing chunk ids"
    )


@pytest.mark.parametrize(
    "status", ["met", "partial", "missing"], ids=["met", "partial", "missing"]
)
def test_uncited_quote_is_blanked_whatever_the_claimed_status(status: str) -> None:
    """The scrub is about the QUOTE, not the label: an unsupported quote must
    never survive, and a non-``met`` status is left alone (only ``met`` is
    demoted, matching the fabrication arm)."""
    evidence = EvidenceObject(
        requirements=[
            RequirementEvidence(
                requirement="Python",
                status=status,  # type: ignore[arg-type]
                evidence=_UNCITED_QUOTE,
                evidence_chunk_ids=["c_999"],
                confidence=0.9,
            )
        ]
    )
    result = verify_evidence(evidence, {}, weights=DEFAULT_WEIGHTS).requirements[0]
    assert result.evidence == ""
    assert result.status == ("missing" if status == "met" else status)


# ── cover-letter evidence: the same lenient arm exists at stages.py:315-318 ──


@pytest.mark.parametrize(
    "evidence_chunk_ids",
    [[], ["cl_999"]],
    ids=["no_citations", "fabricated_id"],
)
def test_uncited_cover_letter_quote_is_blanked(
    evidence_chunk_ids: list[str],
) -> None:
    evidence = EvidenceObject(
        cover_letter_evidence=[
            CoverLetterEvidence(
                theme="motivation",
                evidence="I have followed this company since its founding",
                evidence_chunk_ids=evidence_chunk_ids,
                confidence=0.9,
            )
        ]
    )
    result = verify_evidence(
        evidence, {"cl_1": "I am excited to apply."}, weights=DEFAULT_WEIGHTS
    ).cover_letter_evidence[0]
    assert result.evidence == "", (
        "an uncited cover-letter quote leaked to the display path; the "
        "cover-letter loop carries the same asymmetry as the requirement loop"
    )
    assert result.confidence <= 0.3


def test_uncited_and_unverifiable_cover_letter_quotes_reach_the_same_state() -> None:
    chunks = {"cl_1": "I am excited to apply my backend engineering skills here."}
    theme_text = "I have always dreamed of astrophysics since childhood"

    def _run(ids: list[str]) -> CoverLetterEvidence:
        out = verify_evidence(
            EvidenceObject(
                cover_letter_evidence=[
                    CoverLetterEvidence(
                        theme="motivation",
                        evidence=theme_text,
                        evidence_chunk_ids=ids,
                        confidence=0.9,
                    )
                ]
            ),
            chunks,
            weights=DEFAULT_WEIGHTS,
        )
        result: CoverLetterEvidence = out.cover_letter_evidence[0]
        return result

    uncited = _run(["cl_999"])
    unverifiable = _run(["cl_1"])
    assert (uncited.evidence, uncited.confidence) == (
        unverifiable.evidence,
        unverifiable.confidence,
    )


# ── post-condition invariant: no surviving quote is ever uncited ─────────────


def test_verifier_output_never_carries_a_quote_without_a_citation() -> None:
    """The whole-object invariant the fix establishes, and the one the evals
    harness's verification-rate loop currently cannot rely on."""
    evidence = EvidenceObject(
        requirements=[
            _one_requirement([]),
            _one_requirement(["c_999"]),
            RequirementEvidence(
                requirement="FastAPI",
                status="met",
                evidence=_REAL_QUOTE,
                evidence_chunk_ids=[_REAL_CHUNK_ID],
                confidence=0.9,
            ),
        ],
        cover_letter_evidence=[
            CoverLetterEvidence(
                theme="growth",
                evidence="I want to grow into a staff role",
                evidence_chunk_ids=["cl_999"],
                confidence=0.8,
            )
        ],
    )
    cleaned = verify_evidence(
        evidence, {_REAL_CHUNK_ID: _REAL_CHUNK_TEXT}, weights=DEFAULT_WEIGHTS
    )
    offenders = [
        r.requirement
        for r in cleaned.requirements
        if r.evidence and not r.evidence_chunk_ids
    ] + [
        c.theme
        for c in cleaned.cover_letter_evidence
        if c.evidence and not c.evidence_chunk_ids
    ]
    assert not offenders, (
        f"verify_evidence returned surfaced-but-uncited quotes for {offenders}: "
        "every surviving quote must carry at least one resolvable citation"
    )


# ── over-scrub guard: genuinely cited evidence must be untouched ─────────────


@pytest.mark.parametrize("resume_id, skill_name, quote", _GOLD_EVIDENCE_CASES)
def test_gold_cited_evidence_survives_the_stricter_scrub(
    resume_id: str, skill_name: str, quote: str
) -> None:
    """Real gold anchors from the evals corpus: the fix must tighten the
    UNCITED arm only, never the properly-cited one."""
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


def test_partially_hallucinated_citations_keep_the_verified_quote() -> None:
    """One real id + one invented id: ``good_ids`` is non-empty, the quote
    verifies, so only the dead id is stripped."""
    cleaned = verify_evidence(
        EvidenceObject(
            requirements=[
                RequirementEvidence(
                    requirement="Python",
                    status="met",
                    evidence=_REAL_QUOTE,
                    evidence_chunk_ids=[_REAL_CHUNK_ID, "c_999"],
                    confidence=0.9,
                )
            ]
        ),
        {_REAL_CHUNK_ID: _REAL_CHUNK_TEXT},
        weights=DEFAULT_WEIGHTS,
    )
    result = cleaned.requirements[0]
    assert result.evidence == _REAL_QUOTE
    assert result.status == "met"
    assert result.evidence_chunk_ids == [_REAL_CHUNK_ID]


# ── scoring-invariance pins: display-only at evidence_met_confidence > 0.3 ───
#
# These pass BOTH before and after the fix, deliberately. They are the guard
# that the fix does not move the ranking-evals gate: an uncited quote already
# scores 0 (via the 0.3 confidence cap vs evidence_met_confidence = 0.7), and it
# must still score 0 once the quote is blanked and the status demoted.
#
# The invariance is CONDITIONAL, and the boundary is worth stating exactly.
# It holds for any evidence_met_confidence > 0.3, which covers the 0.7 default
# and therefore the whole ranking-evals corpus. At <= 0.3 the confidence cap no
# longer excludes the requirement, so blanking it genuinely does move
# completeness (1.0 -> 0.0) — see the explicit test below, which pins that
# CHANGE rather than an invariance. match_evidence_met_confidence is env-settable
# (settings.py), so that configuration is reachable; the movement is toward
# correctness and matches what the fabrication arm already did at those weights.


@pytest.mark.parametrize(
    "evidence_chunk_ids", [[], ["c_999"]], ids=["no_citations", "fabricated_id"]
)
def test_uncited_quote_scores_zero_completeness_before_and_after(
    evidence_chunk_ids: list[str],
) -> None:
    cleaned = verify_evidence(
        EvidenceObject(requirements=[_one_requirement(evidence_chunk_ids)]),
        {_REAL_CHUNK_ID: _REAL_CHUNK_TEXT},
        weights=DEFAULT_WEIGHTS,
    )
    assert _evidence_completeness(cleaned, weights=DEFAULT_WEIGHTS) == 0.0, (
        "REGRESSION PIN: an uncited quote must contribute nothing to "
        "evidence completeness — it did not before the fix and must not after"
    )


def test_uncited_cover_letter_quote_scores_zero_motivation_before_and_after() -> None:
    cleaned = verify_evidence(
        EvidenceObject(
            cover_letter_evidence=[
                CoverLetterEvidence(
                    theme="motivation",
                    evidence="I have followed this company since its founding",
                    evidence_chunk_ids=["cl_999"],
                    confidence=0.9,
                )
            ]
        ),
        {"cl_1": "I am excited to apply."},
        weights=DEFAULT_WEIGHTS,
    )
    assert _motivation_score(cleaned, weights=DEFAULT_WEIGHTS) == 0.0


def test_mixed_evidence_completeness_is_identical_before_and_after_the_fix() -> None:
    """A realistic evidence object (one verified quote, one uncited quote, one
    genuinely missing requirement) scores 1/3 — the uncited requirement neither
    counts now nor after it is demoted to ``missing``."""
    cleaned = verify_evidence(
        EvidenceObject(
            requirements=[
                RequirementEvidence(
                    requirement="FastAPI",
                    status="met",
                    evidence=_REAL_QUOTE,
                    evidence_chunk_ids=[_REAL_CHUNK_ID],
                    confidence=0.9,
                ),
                _one_requirement(["c_999"]),
                RequirementEvidence(requirement="Kafka", status="missing"),
            ]
        ),
        {_REAL_CHUNK_ID: _REAL_CHUNK_TEXT},
        weights=DEFAULT_WEIGHTS,
    )
    assert _evidence_completeness(cleaned, weights=DEFAULT_WEIGHTS) == pytest.approx(
        1 / 3
    )


# ── the boundary of the invariance, pinned as a CHANGE not an invariance ─────


def test_uncited_quote_does_move_completeness_below_the_confidence_cap() -> None:
    """At ``evidence_met_confidence <= 0.3`` the fix DOES move a score.

    Everything above pins invariance, which holds only because the pre-existing
    0.3 confidence cap sits below the 0.7 default. Lower the bar under the cap
    and the cap stops excluding the requirement, so blanking it genuinely drops
    completeness 1.0 -> 0.0. That configuration is reachable
    (``match_evidence_met_confidence`` is env-settable), so pin it explicitly
    rather than letting the file's headline claim imply it cannot happen.

    The movement is toward correctness: an uncited quote ceasing to count as
    ``met`` is the intended semantics, and it is what the fabrication arm
    already did at these weights.
    """
    low_bar = DEFAULT_WEIGHTS.model_copy(update={"evidence_met_confidence": 0.25})
    cleaned = verify_evidence(
        EvidenceObject(requirements=[_one_requirement(["c_999"])]),
        {_REAL_CHUNK_ID: _REAL_CHUNK_TEXT},
        weights=low_bar,
    )
    assert _evidence_completeness(cleaned, weights=low_bar) == 0.0, (
        "below the confidence cap the uncited quote must stop counting as met "
        "— this is the intended change, not a regression"
    )


# ── an empty needle must never verify (kills the _fuzz_ratio guard mutation) ──


@pytest.mark.parametrize(
    "blank_quote", ["   ", "\t\n ", " "], ids=["spaces", "tabs_newline", "nbsp"]
)
def test_whitespace_only_quote_with_a_valid_citation_is_blanked(
    blank_quote: str,
) -> None:
    """A whitespace-only quote carrying a REAL citation must still be scrubbed.

    This lands in the strict arm (the citation resolves), so the only thing
    stopping it is ``_fuzz_ratio``'s empty-needle guard returning 0.0. Flipping
    that guard to 1.0 survived the entire suite under review mutation testing —
    an empty quote would then "verify" against any chunk and surface as ``met``.
    """
    cleaned = verify_evidence(
        EvidenceObject(
            requirements=[
                RequirementEvidence(
                    requirement="Python",
                    status="met",
                    evidence=blank_quote,
                    evidence_chunk_ids=[_REAL_CHUNK_ID],
                    confidence=0.95,
                )
            ]
        ),
        {_REAL_CHUNK_ID: _REAL_CHUNK_TEXT},
        weights=DEFAULT_WEIGHTS,
    )
    result = cleaned.requirements[0]
    assert result.evidence == ""
    assert result.status == "missing"
    assert result.confidence <= 0.3
