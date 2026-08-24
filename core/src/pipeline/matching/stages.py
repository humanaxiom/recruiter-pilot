"""Pure-function helpers for the 4-stage matching pipeline.

These are the parts that can be unit-tested without a DB or LLM —
sub-score arithmetic + the ``verify_evidence`` anti-fabrication check +
the final combine. The DB-heavy stage1/stage2_per_candidate /
LLM-heavy stage3 live in ``orchestrator.py`` (they need ctx).

Ported from hris ``packages/pipeline/src/pipeline/matching/stages.py`` with
two corrected behaviours pinned by the Phase 4c RED tests:

* **blocker #1** — ``score_skill_breakdown``'s must-have-miss detection keys
  off a row's ``ontology_weight == 0`` (equivalently ``reason == "missing"``),
  NOT the built ``SkillContribution.score == 0.0``. A present-but-insufficient
  -tenure or family-credited row can score ``0.0`` yet is NOT a genuine miss;
  and family credit makes a genuine miss score ``0.0`` under the built
  contribution's default ``ontology_weight=None`` (``None == 0`` is ``False``),
  so the original ``score == 0.0`` check silently mis-fired.
* **blocker #6** — ``verify_evidence`` uses ``rapidfuzz.fuzz.partial_ratio``
  at the ``evidence_verify_fuzz`` bar. hris's hand-rolled ``_fuzz_substring``
  (a char-SET overlap ratio) verified all four fabricated corpus quotes;
  ``fuzz.WRatio`` leaks r02's fabrication at 0.855 and ``fuzz.ratio`` rejects
  the gold anchors at 0.648/0.796 — ``partial_ratio`` clears both traps.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from itertools import zip_longest
from typing import Any, Generic, TypeVar
from uuid import UUID

from rapidfuzz import fuzz

from src.schemas.matching import (
    DEFAULT_WEIGHTS,
    SCRUBBED_CONFIDENCE_CAP,
    EvidenceObject,
    MatchWeights,
    ScoreBreakdown,
    SkillContribution,
    _strip_control_chars,
)

# security FINDING 5 — the tolerant/strict evidence split must be STRUCTURAL,
# not a convention. ``EvidenceObjectIngest`` is the strict LLM-boundary model
# and ``EvidenceObject`` the tolerant read/DTO one; before this, everything
# downstream of the single ``chat_json`` call was annotated with the TOLERANT
# model, so an uncapped instance was TYPE-LEGAL all the way into
# ``persist_shortlist``'s ``json.dumps``. Nothing but the accident of who
# builds it kept one out.
#
# ``verify_evidence`` and ``stage4_combine`` are therefore generic in the
# evidence type. Both round-trip through ``model_copy``, which preserves the
# class, so the guarantee is real at runtime as well as in mypy: what goes in
# strict comes out strict, and the write boundary can demand strict without
# forcing a cast.
_E = TypeVar("_E", bound=EvidenceObject)

_LEVEL_ORDER: Mapping[str, int] = {
    "high_school": 1,
    "associate": 2,
    "bachelors": 3,
    "masters": 4,
    "phd": 5,
}


@dataclass(frozen=True)
class _SkillRowFromCypher:
    """Shape of one row from the score_skill cypher query."""

    skill: str
    req_years: int | None
    is_must_have: bool
    years: int | None
    last_used_year: int | None
    ontology_weight: float  # 1.0 if direct, < 1.0 if via ontology, 0 if missing


# ---------------- per-skill scoring ----------------


def is_senior_candidate(
    total_years: int | None,
    jd_min_years: int | None,
    *,
    weights: MatchWeights = DEFAULT_WEIGHTS,
) -> bool:
    """Deterministic seniority gate for the implied-experience relief (ADR 0027).

    True only when the JD states a years bar AND the candidate comfortably
    exceeds it (>= ``weights.implied_seniority_factor`` x the minimum). No bar
    or no candidate years → False: we won't grant relief we can't justify from
    the JD's own stated requirement.

    NOTE: this is a YEARS-based boolean gate, NOT the cosine-title ``seniority``
    sub-score (which orchestrator.py computes separately with a live embedder).
    """
    if not jd_min_years or not total_years:
        return False
    return total_years >= jd_min_years * weights.implied_seniority_factor


def score_skill_breakdown(
    rows: Iterable[_SkillRowFromCypher],
    *,
    weights: MatchWeights,
    senior: bool = False,
    today_year: int | None = None,
) -> tuple[float, list[SkillContribution]]:
    """Compute the skill sub-score from already-fetched cypher rows.

    Each row encodes (a) the JD requirement and (b) the candidate's matching
    skill (direct or via ontology) — or null if missing. Returns
    ``(overall, contributions)``.

    ``senior`` (from :func:`is_senior_candidate`) enables the
    implied-experience relief: when a senior candidate matches most of the
    must-have skills but is missing one, the must-have-miss multiplier is
    softened and the missing must-have contribution is flagged
    ``reason="implied-experience"`` instead of ``"missing"``.
    """
    year = today_year or dt.datetime.now(dt.UTC).year
    contributions: list[SkillContribution] = []
    for row in rows:
        # Only ontology_weight == 0 means the candidate genuinely lacks this
        # required skill (it's missing). A skill the candidate HAS must NOT be
        # scored as missing just because its per-skill year count is unknown —
        # résumé extraction frequently omits years, so almost every HAS_SKILL
        # edge has years=None. When years is unknown we give full credit on the
        # years axis; the skill still earns its recency x ontology weight.
        if row.ontology_weight == 0:
            contributions.append(
                SkillContribution(
                    skill=row.skill,
                    score=0.0,
                    is_must_have=row.is_must_have,
                    reason="missing",
                )
            )
            continue
        years: int | None
        if row.years is None:
            years = None
            years_score = 1.0
        else:
            years = max(0, row.years)
            req_y = max(1, row.req_years or 1)
            years_score = min(1.0, years / req_y)
        last = row.last_used_year or year
        delta = year - last
        if delta <= weights.recency_recent_years:
            recency = weights.recency_recent
        elif delta <= weights.recency_mid_years:
            recency = weights.recency_mid
        else:
            recency = weights.recency_old
        score = max(0.0, min(1.0, years_score * recency * row.ontology_weight))
        contributions.append(
            SkillContribution(
                skill=row.skill,
                score=score,
                years=years,
                recency=recency,
                ontology_weight=row.ontology_weight,
                is_must_have=row.is_must_have,
                reason=None if row.ontology_weight == 1.0 else "ontology-fallback",
            )
        )

    if not contributions:
        return 0.0, contributions

    overall = sum(c.score for c in contributions) / len(contributions)

    # Must-have miss penalty (ADR 0008), with implied-experience relief
    # (ADR 0027). BLOCKER #1: key the miss off ``reason == "missing"`` (set only
    # when the ROW's ontology_weight == 0), NEVER the built contribution's
    # ``score == 0.0`` — a present-but-zero-scored row (family credit + years=0,
    # or a genuine miss whose ontology_weight defaults to None on the built
    # object) must not be mis-classified.
    must = [c for c in contributions if c.is_must_have]
    missing_must = [c for c in must if c.reason == "missing"]
    if must and missing_must:
        matched_coverage = 1.0 - len(missing_must) / len(must)
        if senior and matched_coverage >= weights.implied_min_coverage:
            for c in missing_must:
                c.reason = "implied-experience"
            overall *= weights.implied_experience_relief
        else:
            overall *= weights.must_have_miss_penalty

    return overall, contributions


# ---------------- experience / education ----------------


def jd_states_experience_bar(jd_min_years: int | None) -> bool:
    """True iff the JD stated a minimum-years bar.

    ROADMAP A6 sibling of ``vector_pool_is_degenerate``: ``score_experience``
    below CALLS this (rather than restating ``if not jd_min_years:``
    independently), so exactly one comparison against the boundary set
    (``None`` and ``0`` are both "no bar" today) exists in the codebase --
    the same F3 remediation shape ``normalise_vector_scores`` uses for
    ``vector_pool_is_degenerate``.
    """
    return bool(jd_min_years)


def score_experience(
    total_years: int | None,
    jd_min_years: int | None,
    *,
    weights: MatchWeights = DEFAULT_WEIGHTS,
) -> float:
    """Over-qualified does NOT increase past 1.0 and beyond ``overqual_ratio``x
    mildly drops."""
    if not jd_states_experience_bar(jd_min_years):
        return 1.0
    # mypy narrowing only -- `jd_states_experience_bar` already made the
    # actual "is there a bar" decision above; this restates nothing about
    # that decision, it just tells the checker what the predicate proved.
    assert jd_min_years is not None
    total = total_years or 0
    raw = total / jd_min_years
    if raw <= 1.0:
        return raw
    if raw <= weights.overqual_ratio:
        return 1.0
    return max(
        weights.overqual_floor,
        1.0 - (raw - weights.overqual_ratio) * weights.overqual_slope,
    )


def _normalise_field(value: str) -> str:
    """Lowercase + strip control chars + collapse whitespace for field compare.

    Kept deliberately simple — ``token_set_ratio`` handles word order and
    partial token overlap, so no stemming/synonym expansion here. "Computer
    Science" vs "Computer Science" -> 100/100; "Communications" vs the tech
    fields -> well below the bar.
    """
    return _collapse_whitespace(_strip_control_chars(value)).lower()


def _field_matches(
    field: str | None, allowed_normalised: Iterable[str], *, threshold: float
) -> bool:
    """True iff ``field`` fuzzy-matches any allowed (already-normalised) field.

    Unknown/blank candidate field counts as NO match (documented decision,
    ADR-028): the JD asked for a field, so we award full credit only when a
    qualifying-level degree in an allowed field can be CONFIRMED. The
    counter-risk (an unparsed field over-penalizes a candidate who may hold an
    allowed-field degree) is recorded in the ADR.
    """
    if not field or not field.strip():
        return False
    f = _normalise_field(field)
    return any(
        fuzz.token_set_ratio(f, a) / 100.0 >= threshold for a in allowed_normalised
    )


def jd_states_education_bar(jd_min_level: str | None) -> bool:
    """True iff the JD stated a minimum education level.

    ROADMAP A6 sibling of ``jd_states_experience_bar`` /
    ``vector_pool_is_degenerate``: ``score_education`` below CALLS this
    (rather than restating ``if not jd_min_level:`` independently), so
    exactly one comparison against the boundary set (``None`` and ``""`` are
    both "no bar" today) exists in the codebase.
    """
    return bool(jd_min_level)


def education_levels_readable(candidate_levels: Iterable[str | None]) -> bool:
    """True iff at least one level string in ``candidate_levels`` is truthy.

    ROADMAP A6 sibling of the two predicates above: ``score_education`` below
    CALLS this to decide its ``if not ranked: return 0.0`` branch, so exactly
    one "did anything parse" check exists. Deliberately Python-truthiness on
    the STRING (``if lvl``), NOT on ``_LEVEL_ORDER.get(lvl, 0)`` -- a
    whitespace-only string (``"   "``) or an unrecognised level string both
    survive here, because readability means "we parsed A level", not "we
    recognised it". An unrecognised level still contributes a `0`-ranked
    pair to ``ranked`` in ``score_education``, so it is readable but may
    still lose on the level comparison -- a different branch entirely.
    """
    return any(lvl for lvl in candidate_levels)


def score_education(
    candidate_levels: Iterable[str | None],
    jd_min_level: str | None,
    *,
    candidate_fields: Iterable[str | None] = (),
    jd_fields: Iterable[str] = (),
    weights: MatchWeights = DEFAULT_WEIGHTS,
) -> float:
    """Degree LEVEL score, extended with field-of-study relevance (ADR-028).

    When the JD lists ``education.fields`` AND the candidate meets the level
    bar but their qualifying degree is in a NON-allowed field, education is
    capped at ``weights.education_partial`` instead of 1.0. A JD with no
    ``fields`` stays level-only (pre-ADR-028 behaviour). Below-level candidates
    are unaffected by field — the field axis is never consulted below the bar.
    """
    if not jd_states_education_bar(jd_min_level):
        return 1.0
    # mypy narrowing only -- see the identical comment in `score_experience`.
    assert jd_min_level is not None
    req = _LEVEL_ORDER.get(jd_min_level, 0)
    # Materialised once so the SAME sequence backs both
    # ``education_levels_readable`` (the branch predicate) and the
    # ``ranked`` build below (the pairing/scoring) -- consuming
    # ``candidate_levels`` twice from a one-shot iterable would silently
    # desync the two.
    levels = list(candidate_levels)
    if not education_levels_readable(levels):
        return 0.0
    # Pair levels with fields preserving order (index i's level <-> field).
    pairs = zip_longest(levels, candidate_fields, fillvalue=None)
    ranked = [(_LEVEL_ORDER.get(lvl, 0), fld) for lvl, fld in pairs if lvl]
    best = max(rank for rank, _ in ranked)
    if best < req:
        # Field is irrelevant below the level bar.
        return weights.education_partial * (best / req) if req else 0.0
    # best >= req (meets level).
    allowed = [_normalise_field(f) for f in jd_fields if f and f.strip()]
    if not allowed:
        # JD lists no fields -> level-only (unchanged behaviour).
        return 1.0
    qualifying = [fld for rank, fld in ranked if rank >= req]
    if any(
        _field_matches(fld, allowed, threshold=weights.education_field_fuzz)
        for fld in qualifying
    ):
        return 1.0
    # Meets level, wrong/unknown field -> capped.
    return weights.education_partial


# ---------------- vector normalisation ----------------

# ROADMAP A6 (D1). The min-max collapse threshold below, shared by
# ``normalise_vector_scores`` and ``vector_pool_is_degenerate`` so the two
# callers cannot drift on the boundary -- a second independently-written
# ``1e-9`` literal is precisely the A7 defect shape (a duplicated threshold
# constant) this branch's own spec warns against.
_DEGENERATE_POOL_EPS = 1e-9


def vector_pool_is_degenerate(scores: list[float]) -> bool:
    """True iff ``scores`` has no real spread (``hi - lo < _DEGENERATE_POOL_EPS``).

    A degenerate pool -- every single-candidate pool included -- is the case
    ``normalise_vector_scores`` collapses to ``1.0`` for everyone below. This
    predicate lets the write path MARK that no genuine comparison happened,
    without changing ``normalise_vector_scores``'s own return value at all.

    An EMPTY pool (F4, remediation) has no spread at all either, so it reads
    as degenerate too -- ``False`` here would be the affirmative "a real
    comparison happened" claim ADR-041 argues against, for a pool with
    nothing in it to compare.
    """
    if not scores:
        return True
    lo, hi = min(scores), max(scores)
    return (hi - lo) < _DEGENERATE_POOL_EPS


def normalise_vector_scores(scores: list[float]) -> list[float]:
    """Min-max scale a stage-1 vec_score pool to [0, 1].

    With a degenerate pool (all identical), returns 1.0 for every entry rather
    than dividing by zero.

    F3 (remediation): delegates the degeneracy check to
    ``vector_pool_is_degenerate`` so exactly one ``<`` comparison against
    ``_DEGENERATE_POOL_EPS`` exists in the codebase -- two independently
    written comparisons is the drift risk the shared constant was meant to
    remove. The empty-pool short-circuit stays first so this function's
    return type (``[]``, not ``[1.0] * 0`` -- observably identical, but kept
    explicit) and behaviour are unchanged.
    """
    if not scores:
        return []
    if vector_pool_is_degenerate(scores):
        return [1.0] * len(scores)
    lo, hi = min(scores), max(scores)
    return [(s - lo) / (hi - lo) for s in scores]


# ---------------- evidence verification ----------------


_WHITESPACE = re.compile(r"\s+")


def _collapse_whitespace(text: str) -> str:
    """Normalise runs of whitespace to a single space, then strip.

    Applied SYMMETRICALLY to the needle and the haystack, and to every stage
    of ``_fuzz_ratio`` — the length floor, the length guard AND the string
    handed to ``partial_ratio`` — exactly like ``.lower()`` already is. It is
    a normalisation, not a relaxation: it cannot turn a non-span into a span,
    only let a genuine span score as one.

    Applying it to the guard alone (the original shape) was not enough, and
    the corpus says so. Measured on r01's real 148-char Python chunk, with
    quotes that collapse to a verbatim span in every case, ``partial_ratio``
    on the RAW strings scored: whole chunk triple-spaced 0.797 and the gold
    anchor with 4-space column padding 0.826 — both BELOW the 0.85 bar, i.e.
    real evidence scrubbed as fabrication — and the newline-wrapped anchor
    0.859, nine thousandths from the same fate. Collapsed on both sides all
    six re-renders score 1.000.
    """
    return _WHITESPACE.sub(" ", text).strip()


def _fuzz_ratio(
    needle: str,
    haystack: str,
    *,
    min_chars: int = DEFAULT_WEIGHTS.evidence_min_quote_chars,
) -> float:
    """Fuzzy substring score in [0, 1] via ``rapidfuzz.fuzz.partial_ratio``.

    ``partial_ratio`` scores the best-matching WINDOW of ``haystack`` against
    the whole ``needle`` — the right shape for "is this quote a span of the
    cited chunk". BLOCKER #6: it rejects every fabricated corpus quote while
    accepting the gold anchors, unlike ``fuzz.WRatio`` (leaks r02's fabrication
    at 0.855) or ``fuzz.ratio`` (rejects gold anchors at 0.648/0.796).

    Both sides are whitespace-collapsed first (see ``_collapse_whitespace``),
    and every check below — including ``partial_ratio`` itself — runs on the
    collapsed forms.

    Two guards run BEFORE ``partial_ratio``, because it is blind to both
    (ADR-022 follow-up items #1 and #4):

    * **Minimum quote length** (#4). Anything under ``min_chars`` scores 0.0.
      ``"API"`` otherwise scores 1.000 against every chunk containing it, so a
      bare token is indistinguishable from real evidence. Measured on the
      COLLAPSED needle, not the raw one: ``"API"`` followed by 30 spaces is 33
      raw characters and cleared a raw floor while carrying three characters
      of evidence. ``verify_evidence`` happens to strip before calling, but a
      guard that only holds because of what its caller does first is not a
      guard — this function's contract stands on its own.
    * **Length guard** (#1). ``partial_ratio`` scores the best-matching WINDOW
      of the longer string, so a quote CONTAINING the cited chunk verbatim
      plus arbitrary appended fabrication scores 1.000 at any append length.
      A quote is a SPAN of exactly one cited chunk, and a span cannot be
      longer than the thing it spans — so a quote longer than the chunk is
      rejected outright, at any excess. This is a structural invariant, NOT a
      tunable ratio: a threshold like ``k = 1.05`` leaves a small-append window
      open (a +1 char append on r01's 148-char chunk lands at 1.007) and the
      fabrication rides through it. Cross-chunk concatenation is rejected as a
      consequence, which is intended.

    Both sides are also scrubbed of the invisible/bidi class
    (``schemas.matching._strip_control_chars``) before any of the above, and
    SYMMETRY there is load-bearing. The QUOTE is already scrubbed at the schema
    boundary; the CHUNK is not — it comes from ``resumes.parsed``, and
    ``parsing/extract.py::_sanitize`` strips NULs and nothing else. So a résumé
    whose extracted text carries SOFT HYPHENS (what a PDF emits at its
    line-break points) would give a haystack full of U+00AD and a needle with
    every one removed. MEASURED on a 148-char chunk, needle = the same text
    scrubbed: SHY every 60 chars scores 0.986, every 20 chars 0.956, every 8
    chars 0.892 — but every 5 chars 0.838 and every 2 chars 0.671, i.e. the
    chunk's own text rejected as a fabrication. Scrubbing both sides is a
    normalisation exactly like ``.lower()`` and ``_collapse_whitespace``: it
    removes only invisible characters, from needle and haystack alike, so it
    cannot turn a non-span into a span. Appended VISIBLE fabrication survives
    the scrub on the needle and still trips the length guard.
    """
    needle = _collapse_whitespace(_strip_control_chars(needle))
    haystack = _collapse_whitespace(_strip_control_chars(haystack))
    if not needle:
        return 0.0
    if len(needle) < min_chars:
        return 0.0
    if len(needle) > len(haystack):
        return 0.0
    return float(fuzz.partial_ratio(needle, haystack)) / 100.0


def verify_evidence(
    evidence: _E,
    chunks_by_id: Mapping[str, str],
    *,
    weights: MatchWeights = DEFAULT_WEIGHTS,
) -> _E:
    """Strip hallucinated chunk_ids; downgrade requirements whose ``evidence``
    quote doesn't actually appear in any cited chunk (fuzzy-match threshold
    ``weights.evidence_verify_fuzz``).

    Returns a new evidence object of the SAME CLASS it was given — never
    mutates input. Generic rather than widened to ``EvidenceObject`` so an
    ``EvidenceObjectIngest`` survives verification as an ingest instance and
    the write boundary can be typed strictly (security FINDING 5).
    """
    quote_threshold = weights.evidence_verify_fuzz
    min_chars = weights.evidence_min_quote_chars
    cleaned: list[Any] = []
    for req in evidence.requirements:
        good_ids = [cid for cid in req.evidence_chunk_ids if cid in chunks_by_id]
        new_req = req.model_copy(update={"evidence_chunk_ids": good_ids})
        if new_req.evidence and good_ids:
            quoted = new_req.evidence.lower().strip()
            if not any(
                _fuzz_ratio(quoted, chunks_by_id[cid].lower(), min_chars=min_chars)
                >= quote_threshold
                for cid in good_ids
            ):
                # Quote not found in any cited chunk — fabrication likely.
                new_req = new_req.model_copy(
                    update={
                        "evidence": "",
                        "status": (
                            "missing" if new_req.status == "met" else new_req.status
                        ),
                        "confidence": min(new_req.confidence, SCRUBBED_CONFIDENCE_CAP),
                    }
                )
        elif new_req.evidence and not good_ids:
            # Has a quote but NO surviving citation — strictly less evidence
            # than a bad citation, so it gets the same scrub, not a weaker one.
            new_req = new_req.model_copy(
                update={
                    "evidence": "",
                    "status": (
                        "missing" if new_req.status == "met" else new_req.status
                    ),
                    "confidence": min(new_req.confidence, SCRUBBED_CONFIDENCE_CAP),
                }
            )
        cleaned.append(new_req)

    # Same anti-fabrication scrub for cover-letter evidence (Feature 1): a theme
    # citing a fabricated cl_ id (or quoting text not in any cited cl_ chunk) is
    # stripped/downgraded, so the deterministic motivation score only ever
    # counts VERIFIED, cited themes.
    cleaned_cover: list[Any] = []
    for cle in evidence.cover_letter_evidence:
        good_ids = [cid for cid in cle.evidence_chunk_ids if cid in chunks_by_id]
        new_cle = cle.model_copy(update={"evidence_chunk_ids": good_ids})
        if new_cle.evidence and good_ids:
            quoted = new_cle.evidence.lower().strip()
            if not any(
                _fuzz_ratio(quoted, chunks_by_id[cid].lower(), min_chars=min_chars)
                >= quote_threshold
                for cid in good_ids
            ):
                new_cle = new_cle.model_copy(
                    update={
                        "evidence": "",
                        "confidence": min(new_cle.confidence, SCRUBBED_CONFIDENCE_CAP),
                    }
                )
        elif new_cle.evidence and not good_ids:
            new_cle = new_cle.model_copy(
                update={
                    "evidence": "",
                    "confidence": min(new_cle.confidence, SCRUBBED_CONFIDENCE_CAP),
                }
            )
        cleaned_cover.append(new_cle)

    return evidence.model_copy(
        update={"requirements": cleaned, "cover_letter_evidence": cleaned_cover}
    )


# ---------------- stage 4: combine ----------------


@dataclass(frozen=True)
class _CombineInput(Generic[_E]):
    resume_id: UUID
    structured: float
    breakdown: ScoreBreakdown
    evidence: _E | None


@dataclass(frozen=True)
class _CombineEntry(Generic[_E]):
    resume_id: UUID
    rank: int
    score_final: float
    score_structured: float
    score_evidence: float
    breakdown: ScoreBreakdown
    evidence: _E | None


def stage4_combine(
    candidates: Iterable[_CombineInput[_E]], weights: MatchWeights
) -> list[_CombineEntry[_E]]:
    """Combine structured + evidence into a final score and rank descending."""
    entries: list[_CombineEntry[_E]] = []
    for c in candidates:
        evidence_completeness = _evidence_completeness(c.evidence, weights=weights)
        motivation = _motivation_score(c.evidence, weights=weights)
        final = (
            weights.structured * c.structured
            + weights.evidence * evidence_completeness
            + weights.motivation * motivation
        )
        # Surface the deterministic motivation sub-score in the breakdown so the
        # cover-letter contribution is auditable (0.0 when no cover letter /
        # weight off — leaves the rank unchanged).
        breakdown = c.breakdown.model_copy(update={"motivation": motivation})
        entries.append(
            _CombineEntry(
                resume_id=c.resume_id,
                rank=0,  # filled below
                score_final=final,
                score_structured=c.structured,
                score_evidence=evidence_completeness,
                breakdown=breakdown,
                evidence=c.evidence,
            )
        )

    entries.sort(key=lambda e: e.score_final, reverse=True)
    return [
        _CombineEntry(
            resume_id=e.resume_id,
            rank=i + 1,
            score_final=e.score_final,
            score_structured=e.score_structured,
            score_evidence=e.score_evidence,
            breakdown=e.breakdown,
            evidence=e.evidence,
        )
        for i, e in enumerate(entries)
    ]


def _evidence_completeness(
    evidence: EvidenceObject | None, *, weights: MatchWeights = DEFAULT_WEIGHTS
) -> float:
    if evidence is None or not evidence.requirements:
        return 0.0
    met = sum(
        1
        for r in evidence.requirements
        if r.status == "met" and r.confidence >= weights.evidence_met_confidence
    )
    partial = sum(1 for r in evidence.requirements if r.status == "partial")
    return (met + weights.evidence_partial_weight * partial) / len(
        evidence.requirements
    )


def _motivation_score(
    evidence: EvidenceObject | None, *, weights: MatchWeights = DEFAULT_WEIGHTS
) -> float:
    """Deterministic cover-letter motivation in [0, 1] (Feature 1).

    The mean confidence of the VERIFIED cover-letter themes (confidence ≥
    ``motivation_min_confidence`` AND at least one surviving ``cl_`` citation),
    normalised by the number of themes the LLM emitted — so unverified/uncited
    themes drag the score down. 0.0 when there's no cover-letter evidence.
    """
    if evidence is None or not evidence.cover_letter_evidence:
        return 0.0
    verified = [
        e
        for e in evidence.cover_letter_evidence
        if e.confidence >= weights.motivation_min_confidence and e.evidence_chunk_ids
    ]
    if not verified:
        return 0.0
    return sum(e.confidence for e in verified) / len(evidence.cover_letter_evidence)
