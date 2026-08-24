"""Matching / ranking schemas — ported from hris
``packages/schemas/src/schemas/matching.py``, with the review workflow CUT.

The 2nd-review pipeline (``PipelineStage``, ``DispositionReason``,
``ShortlistDecision*``, ``StageTransition*``, ``DecisionKind``,
``TERMINAL_STAGES``) is not part of recruiter-assistant and is deliberately
absent — ``ShortlistEntry`` keeps only the blind-review fields
(``blinded`` / ``display_label``), not ``current_decision`` / ``current_stage``.

``MatchWeights`` is the ranking contract: its defaults and the
``_sums_close_to_one`` validator encode the plan's algorithm
(``0.6·structured + 0.3·evidence + 0.1·motivation``; skill/exp/edu/sen/vector
sub-weights; the ``0.85`` anti-fabrication fuzz threshold).
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

EvidenceStatus = Literal["met", "partial", "missing"]

# ADR-022 follow-up #2 — C0 controls (plus DEL) that must never reach the
# ``json.dumps`` in ``persist_shortlist``. Postgres rejects the JSON escape
# for U+0000 outright ("unsupported Unicode escape sequence"), which kills the
# whole shortlist transaction; the rest are junk in a human-facing quote and
# go under the same rule. TAB (0x09), LF (0x0a) and CR (0x0d) carry real
# formatting in a multi-line quote and are deliberately NOT in the class.
#
# This generalises the NUL-strip idiom already established on the parse path
# (``src/pipeline/parsing/extract.py::_sanitize``) rather than inventing a
# second one. It lives at the SCHEMA boundary, not in ``verify_evidence``:
# the verifier only ever rewrites the two ``evidence`` fields, so
# ``requirement`` / ``overall_summary`` / ``overall_motivation`` would still
# reach Postgres unscrubbed.
#
# It is also not redundant with ``pipeline/llm/client.py::_strip_nuls``, which
# ALREADY recursively strips U+0000 from parsed LLM JSON before validation —
# so the specific byte Postgres rejects is, on the ``chat_json`` path, handled
# twice. Stating the justification that way makes it stronger, not weaker:
# this layer earns its place by being wider on BOTH axes. It removes the other
# C0 controls (invisible junk in a human-facing quote, which ``_strip_nuls``
# leaves alone), and it holds for every caller that never goes through the LLM
# client at all — read-path revalidation, tests, and any future non-LLM
# producer of an evidence object. Defence in depth with the outer layer
# strictly containing the inner one, not two copies of the same check.
# security FINDING 1 — the class also covers Unicode FORMAT (Cf) characters.
#
# C0 + DEL is not the whole of "invisible junk in a human-facing quote". Two
# sub-classes of Cf are a live FABRICATION vector, because the anti-fabrication
# verifier scores the codepoints and the recruiter reads the RENDERING:
#
#   * BIDI OVERRIDES AND ISOLATES (U+202A-202E, U+2066-2069). MEASURED against
#     r01's real 148-char chunk c_001, the quote
#     ``chunk[:100] + U+202E + "detacirbaf"`` is 111 characters, clears both
#     the minimum-quote-length floor and the length guard, scores 0.948 and
#     VERIFIES — then renders to the reviewer as the English word "fabricated",
#     because U+202E reverses the display order of everything after it. The
#     reviewer reads a word the résumé does not contain, inside a quote the
#     verifier has just certified.
#   * ZERO-WIDTH / INVISIBLE FORMAT CHARACTERS (ZWSP U+200B, WJ U+2060,
#     BOM U+FEFF, SHY U+00AD, MVS U+180E). No text, no visibility, and no way
#     for a reviewer to see them in the quote they are being asked to trust.
#
# The BIDI MARKS (U+200E/200F) are in the class too. The security finding
# enumerated only the overrides and isolates, but the marks are the same shape
# of defect — invisible Cf codepoints whose only effect is to reorder the
# display of neighbouring neutral text — so leaving them out would leave an
# adjacent hole of identical character. The class is a strict SUPERSET of the
# finding, never a subset.
#
# ZWNJ (U+200C) and ZWJ (U+200D) are deliberately EXCLUDED: they are
# script-meaningful in Persian/Arabic/Devanagari and inside emoji sequences, so
# stripping them would corrupt genuine résumé text. NBSP, U+2028/2029 and
# U+3000 are excluded for a different reason — they are real whitespace, and
# ``stages._collapse_whitespace`` already normalises them SYMMETRICALLY on both
# sides of the fuzzy match. Removing them here would be a second, asymmetric
# normalisation applied to the needle alone.
#
# WHAT THIS DOES AND DOES NOT CLOSE, stated honestly. Stripping U+202E does
# NOT lower the attack's fuzzy score (measured 0.948 -> 0.952: the appended
# "detacirbaf" is 10 visible characters either way, and a short append onto a
# long chunk sits inside ``partial_ratio``'s existing tolerance at the 0.85
# bar — a separate, known property). What it removes is the ability to make
# appended text RENDER as plausible prose. The quote a reviewer sees is now
# the quote that was scored.
#
# INTERACTION WITH THE LENGTH GUARD — verified, not assumed. Python's ``\s``
# collapses NBSP/U+2028/U+3000 but NOT ZWSP/BOM/WJ/SHY, so before this change
# those inflated the needle's collapsed length and the length guard ("a span
# cannot be longer than the chunk it spans") rejected it: fail-CLOSED.
# Stripping them removes that inflation. It opens no window, because the scrub
# touches no VISIBLE character: after it, the verdict on a padded quote is by
# construction the verdict on the same quote unpadded — a string the producer
# could always have submitted directly. Appended visible fabrication is still
# visible, and still trips the length guard. In one direction the scrub is
# strictly stronger: ``"API"`` + 40x U+200B is 43 characters and cleared even
# the old 32-char floor on length, surviving only on ``partial_ratio``;
# scrubbed it is 3 characters and the floor rejects it outright. Pinned in
# ``tests/unit/test_matching_stages.py``'s FINDING 1 block.
#
# Every non-ASCII codepoint below is written as a HEX INTEGER and materialised
# with ``chr()``, never as the literal character. ADR-022 records that its own
# first draft embedded a literal NUL and git classified the file as binary; a
# literal U+202E in this source would be worse still, since it would reorder
# the display of the very code that defines the class. The tests follow the
# same rule.
_INVISIBLE_FORMAT_CODEPOINTS: tuple[int | tuple[int, int], ...] = (
    0x00AD,  # SOFT HYPHEN
    0x180E,  # MONGOLIAN VOWEL SEPARATOR
    0x200B,  # ZERO WIDTH SPACE
    (0x200E, 0x200F),  # LEFT-TO-RIGHT / RIGHT-TO-LEFT MARK
    (0x202A, 0x202E),  # bidi EMBEDDING / OVERRIDE / POP DIRECTIONAL FORMATTING
    0x2060,  # WORD JOINER
    (0x2066, 0x2069),  # bidi ISOLATE / FIRST STRONG ISOLATE / POP
    0xFEFF,  # ZERO WIDTH NO-BREAK SPACE (BOM)
)


def _char_class_body(codepoints: tuple[int | tuple[int, int], ...]) -> str:
    """Render single codepoints and inclusive ranges as regex class members."""
    parts: list[str] = []
    for item in codepoints:
        if isinstance(item, tuple):
            lo, hi = item
            parts.append(f"{chr(lo)}-{chr(hi)}")
        else:
            parts.append(chr(item))
    return "".join(parts)


_INVISIBLE_CONTROLS = re.compile(
    # C0 controls (keeping TAB 0x09 / LF 0x0a / CR 0x0d) and DEL, then the Cf
    # class above.
    r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f"
    + _char_class_body(_INVISIBLE_FORMAT_CODEPOINTS)
    + "]"
)


def _strip_control_chars(value: str) -> str:
    """Drop C0 controls, DEL and the invisible/bidi Cf class above, keeping
    TAB / LF / CR and real whitespace. No-op on clean text."""
    return _INVISIBLE_CONTROLS.sub("", value)


# Every LLM-authored free-text field on the evidence models carries this, so
# the scrub holds on ``__init__`` and ``model_validate`` alike and is a fixed
# point across a dump/validate roundtrip.
CleanText = Annotated[str, AfterValidator(_strip_control_chars)]

# ── ADR-022 follow-up #3 — evidence size caps ───────────────────────────────
#
# HUMAN DECISION (reviewer round 2): the READ path is TOLERANT, INGEST is
# STRICT. A cap prevents a bad WRITE; once the bytes are on disk it buys no
# protection whatsoever and only breaks retrieval. This project has no
# migration framework, and ``shortlist_service`` validates stored JSONB with
# an uncaught ``model_validate``, so a cap on the read model turns any
# pre-existing over-cap row into a 500 for the whole job — making exactly the
# pathological output the cap targets PERMANENTLY UNREADABLE.
#
# So the three evidence models below carry NO length constraints and are what
# the DTOs, the read path and ``verify_evidence`` use. The ``*Ingest``
# subclasses at the bottom of this module carry the caps, and are wired at
# exactly one place: the ``chat_json`` call in
# ``pipeline/matching/orchestrator.py::_stage3_per_candidate``.
#
# The ingest caps SCRUB rather than raise, for the same reason
# ``verify_evidence`` scrubs per-requirement instead of rejecting wholesale:
# one over-long quote must not cost a candidate every OTHER requirement's
# evidence, which is what a raising cap did (the object failed validation, so
# ``_stage3_per_candidate`` returned ``None``).
MAX_EVIDENCE_QUOTE_CHARS = 2000
MAX_REQUIREMENT_CHARS = 500
MAX_EVIDENCE_CHUNK_IDS = 8
MAX_REQUIREMENTS = 64
MAX_OVERALL_TEXT_CHARS = 1000

# Confidence ceiling applied to any requirement/theme whose quote was scrubbed.
# Shared with ``pipeline/matching/stages.py::verify_evidence`` so the ingest
# drop and the anti-fabrication scrub cannot drift apart.
SCRUBBED_CONFIDENCE_CAP = 0.3

# A skill family/category label. Free-form string, resolved against the
# config-driven ontology vocabulary at scoring time (not an enum).
SkillCategory = str


class MatchWeights(BaseModel):
    """Top-level + sub-score weights. Sub-weights must sum to ~1.0."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    structured: float = Field(default=0.6, ge=0, le=1)
    evidence: float = Field(default=0.3, ge=0, le=1)
    motivation: float = Field(default=0.1, ge=0, le=1)
    skill: float = Field(default=0.40, ge=0, le=1)
    experience: float = Field(default=0.25, ge=0, le=1)
    education: float = Field(default=0.10, ge=0, le=1)
    seniority: float = Field(default=0.15, ge=0, le=1)
    vector: float = Field(default=0.10, ge=0, le=1)
    must_have_miss_penalty: float = Field(default=0.5, ge=0, le=1)
    implied_experience_relief: float = Field(default=0.75, ge=0, le=1)
    recency_recent_years: int = Field(default=2, ge=0)
    recency_mid_years: int = Field(default=5, ge=0)
    recency_recent: float = Field(default=1.0, ge=0, le=1)
    recency_mid: float = Field(default=0.7, ge=0, le=1)
    recency_old: float = Field(default=0.4, ge=0, le=1)
    overqual_ratio: float = Field(default=2.0, ge=1)
    overqual_slope: float = Field(default=0.1, ge=0)
    overqual_floor: float = Field(default=0.8, ge=0, le=1)
    education_partial: float = Field(default=0.5, ge=0, le=1)
    # Field-of-study fuzzy-match bar (token_set_ratio/100). A qualifying-level
    # degree whose field clears this against any jd.education.fields entry keeps
    # full education credit; otherwise capped at education_partial. ADR-028.
    # A THRESHOLD, not a weight — deliberately NOT part of either sum the
    # ``_sums_close_to_one`` validator enforces (sibling to evidence_verify_fuzz).
    education_field_fuzz: float = Field(default=0.85, ge=0, le=1)
    seniority_floor: float = Field(default=0.5, ge=0, lt=1)
    implied_seniority_factor: float = Field(default=1.5, ge=1)
    implied_min_coverage: float = Field(default=0.5, ge=0, le=1)
    evidence_met_confidence: float = Field(default=0.7, ge=0, le=1)
    evidence_partial_weight: float = Field(default=0.5, ge=0, le=1)
    evidence_verify_fuzz: float = Field(default=0.85, ge=0, le=1)
    # ADR-022 follow-up #4: a quote shorter than this many characters is not
    # evidence of anything — "API" matches any chunk containing it at 1.000.
    #
    # LOWERED 32 -> 16 BY HUMAN DECISION (security FINDING 4). At 32 the floor
    # was ALSO scrubbing genuine short evidence and demoting it met ->
    # missing, indistinguishably from a fabrication. MEASURED as blanked at
    # 32: "PhD in Computer Science" (23), "AWS Solutions Architect" (23),
    # "Postgres schema migrations" (26). 16 still rejects every degenerate
    # case the floor exists for — "API" (3), "SQL" (3), "Kubernetes" (10).
    #
    # The eval corpus could not see the defect: its shortest gold anchor was
    # 71 characters, so any floor up to 71 passed the ranking gate untouched.
    # That is closed in the same change — labels.json carries a genuinely
    # short anchor and run_evals.py scores every anchor through the real
    # ``_fuzz_ratio``, floor included.
    evidence_min_quote_chars: int = Field(default=16, ge=0)
    motivation_min_confidence: float = Field(default=0.7, ge=0, le=1)

    @model_validator(mode="after")
    def _sums_close_to_one(self) -> MatchWeights:
        top = self.structured + self.evidence + self.motivation
        sub = (
            self.skill + self.experience + self.education + self.seniority + self.vector
        )
        if abs(top - 1.0) > 0.01:
            raise ValueError(
                f"structured+evidence+motivation must sum to 1.0 (got {top:.3f})"
            )
        if abs(sub - 1.0) > 0.01:
            raise ValueError(
                "skill+experience+education+seniority+vector must sum to 1.0 "
                f"(got {sub:.3f})"
            )
        if self.implied_experience_relief < self.must_have_miss_penalty:
            raise ValueError(
                "implied_experience_relief must be >= must_have_miss_penalty "
                "(relief is a softer penalty): got "
                f"relief={self.implied_experience_relief:.3f} < "
                f"penalty={self.must_have_miss_penalty:.3f}"
            )
        return self


DEFAULT_WEIGHTS = MatchWeights()


class SkillContribution(BaseModel):
    """One row in the skill breakdown: per required skill."""

    model_config = ConfigDict(extra="forbid")

    skill: str
    score: float = Field(ge=0, le=1)
    years: int | None = None
    recency: float | None = None
    ontology_weight: float | None = None
    is_must_have: bool = False
    reason: str | None = None  # "missing", "ontology-fallback", etc.


class ScoreBreakdown(BaseModel):
    """All sub-scores for one candidate. Stored verbatim in
    shortlist_entries.score_breakdown (jsonb)."""

    model_config = ConfigDict(extra="forbid")

    skill: float = Field(ge=0, le=1)
    experience: float = Field(ge=0, le=1)
    education: float = Field(ge=0, le=1)
    seniority: float = Field(ge=0, le=1)
    vector: float = Field(ge=0, le=1)
    structured: float = Field(ge=0, le=1)
    motivation: float = Field(default=0.0, ge=0, le=1)
    implied_experience: bool = False
    skill_contributions: list[SkillContribution] = Field(default_factory=list)
    # ROADMAP A6 (D1/D2). THREE states each, mirroring ``evidence_evaluated``
    # above:
    #
    #   True  — the comparison ran. The stored value is a real measurement.
    #   False — no comparison was possible; the stored value came from a
    #           fallback default, not from merit.
    #   None  — the row predates these markers. Assert neither.
    #
    # Answers "did the computation happen", never "what does the number
    # mean" — never inferred from the score itself (that is the exact mutant
    # this pair of markers exists to prevent). Unlike ``evidence_evaluated``,
    # these live INSIDE ``ScoreBreakdown`` (which is persisted verbatim), so
    # no fold/pop path is needed for them to round-trip.
    seniority_measured: bool | None = None
    vector_discriminating: bool | None = None
    # ROADMAP A6 siblings (docs/adr/041-sub-score-measurement-markers.md,
    # "Three siblings found while writing this"). Same THREE-state contract
    # as the pair above, one dimension over each:
    #
    #   experience_bar_stated -- did the JD state a minimum-years bar?
    #   education_bar_stated  -- did the JD state a minimum education level?
    #   education_readable    -- did the résumé yield >= 1 readable degree
    #                            level at all (independent of whether it
    #                            meets the bar)?
    #
    # Same names on both sides of the read path (deliberate deviation from
    # ADR-041's seniority_measured -> seniority_assessed rename -- see
    # ADR-041 addendum): a rename buys nothing and is one more place two
    # copies can drift.
    experience_bar_stated: bool | None = None
    education_bar_stated: bool | None = None
    education_readable: bool | None = None


class RequirementEvidence(BaseModel):
    """TOLERANT read/DTO model. Deliberately carries no length caps — see the
    ingest/read note above. ``RequirementEvidenceIngest`` is the strict one.

    CAVEAT — ``model_copy`` / ``model_construct`` BYPASS ``CleanText``
    (security FINDING 6). Pydantic does not re-run validators for either, so
    ``model_copy(update={"requirement": ...})`` can put a live control
    character back onto an already-validated instance; MEASURED reaching
    ``json.dumps`` as the escaped form Postgres rejects. There is no live
    exploit today — ``verify_evidence`` is the only ``model_copy`` caller on
    this model and it only ever updates ``evidence`` to ``""`` — so the
    response is this caveat rather than a runtime guard. Any future
    ``model_copy`` that writes ATTACKER- OR MODEL-AUTHORED text into a field
    must scrub it itself, or go through ``model_validate`` instead.
    """

    model_config = ConfigDict(extra="ignore")

    requirement: CleanText
    status: EvidenceStatus = "missing"
    evidence: CleanText = ""
    evidence_chunk_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0, le=1)
    # FU-2 (display-only): the resolved source text behind ``evidence_chunk_ids``,
    # expanded from ``resumes.parsed`` at read/export time. Redacted under blind
    # review, exactly like ``evidence``. Populated only on the display paths; the
    # LLM never emits it, so at write time it is always ``None`` and persists as
    # JSONB ``null`` — a pure display expansion, never a stored value.
    #
    # ``CleanText``, not bare ``str`` (security FINDING 6). It was the one
    # free-text field on these models without the scrub. The text it carries is
    # fed from ``resumes.parsed``, which the parse path already NUL-strips in
    # ``pipeline/parsing/extract.py::_sanitize``, so there is no live gap —
    # but that is a property of the CURRENT producer, not of this model, and
    # the scrub class is wider than ``_sanitize``'s anyway (bidi and
    # zero-width characters in an expanded source context render to a reviewer
    # exactly as they would in the quote itself).
    source_context: CleanText | None = None


CoverLetterTheme = Literal["motivation", "role_alignment", "cultural_fit", "growth"]


class CoverLetterEvidence(BaseModel):
    """One cover-letter theme with a cited quote (Feature 1).

    TOLERANT read/DTO model — no length caps, by the same decision as
    ``RequirementEvidence``."""

    model_config = ConfigDict(extra="ignore")

    theme: CoverLetterTheme
    evidence: CleanText = ""
    evidence_chunk_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0, le=1)


class EvidenceObject(BaseModel):
    """Evidence for one candidate: the READ/DTO shape.

    This is what ``shortlist_service`` validates stored JSONB with, what the
    ``ShortlistEntry`` / ``JobMatchEntry`` DTOs hold, and what
    ``verify_evidence`` takes and returns. It must accept anything ever
    written, so it declares no length caps. The LLM ingest boundary uses
    ``EvidenceObjectIngest``."""

    model_config = ConfigDict(extra="ignore")

    requirements: list[RequirementEvidence] = Field(default_factory=list)
    overall_summary: CleanText = ""
    cover_letter_presence: bool = False
    cover_letter_evidence: list[CoverLetterEvidence] = Field(default_factory=list)
    overall_motivation: CleanText = ""


# ── STRICT ingest variants — LLM boundary ONLY ──────────────────────────────
#
# Never use these on a read path. They subclass their tolerant counterparts so
# variance runs one way only: an ingest instance is accepted everywhere a read
# instance is (``verify_evidence``, the DTOs, ``persist_shortlist``), and a
# read instance can never stand in for an ingest one.


class RequirementEvidenceIngest(RequirementEvidence):
    """``RequirementEvidence`` with the size caps enforced by scrubbing."""

    @model_validator(mode="after")
    def _enforce_ingest_caps(self) -> RequirementEvidenceIngest:
        if len(self.requirement) > MAX_REQUIREMENT_CHARS:
            # The label is the row's KEY, not evidence of anything, so it is
            # trimmed rather than dropped — blanking it would orphan the row.
            self.requirement = self.requirement[:MAX_REQUIREMENT_CHARS]
        if len(self.evidence_chunk_ids) > MAX_EVIDENCE_CHUNK_IDS:
            self.evidence_chunk_ids = self.evidence_chunk_ids[:MAX_EVIDENCE_CHUNK_IDS]
        if len(self.evidence) > MAX_EVIDENCE_QUOTE_CHARS:
            # DROP the quote, never truncate it. A truncated superset-bypass
            # quote can still contain the cited chunk verbatim in its prefix
            # and would then verify at 1.000 — trimming would manufacture the
            # fabrication the guard exists to catch. Demote exactly as
            # ``verify_evidence`` does: a blanked-but-still-``met`` row would
            # keep full credit in ``_evidence_completeness``, which scores on
            # status and confidence and never looks at the quote text.
            self.evidence = ""
            if self.status == "met":
                self.status = "missing"
            self.confidence = min(self.confidence, SCRUBBED_CONFIDENCE_CAP)
        return self


class CoverLetterEvidenceIngest(CoverLetterEvidence):
    """``CoverLetterEvidence`` with the size caps enforced by scrubbing."""

    @model_validator(mode="after")
    def _enforce_ingest_caps(self) -> CoverLetterEvidenceIngest:
        if len(self.evidence_chunk_ids) > MAX_EVIDENCE_CHUNK_IDS:
            self.evidence_chunk_ids = self.evidence_chunk_ids[:MAX_EVIDENCE_CHUNK_IDS]
        if len(self.evidence) > MAX_EVIDENCE_QUOTE_CHARS:
            self.evidence = ""
            self.confidence = min(self.confidence, SCRUBBED_CONFIDENCE_CAP)
        return self


class EvidenceObjectIngest(EvidenceObject):
    """LLM-output schema for shortlist_evidence_v1/v2 — the ``chat_json``
    target, and the ONLY place the evidence size caps are enforced."""

    @model_validator(mode="before")
    @classmethod
    def _bound_lists_before_item_validation(cls, data: Any) -> Any:
        """Slice the raw lists BEFORE pydantic validates their items.

        This is what actually bounds the DoS. The previous ``max_length``
        constraint did not: pydantic validates every item first and only then
        checks the length, so a 100,000-entry list of 2,000,000-char quotes
        ran the per-item scrub 100,000 times before raising.
        """
        if isinstance(data, dict):
            for key in ("requirements", "cover_letter_evidence"):
                value = data.get(key)
                if isinstance(value, list) and len(value) > MAX_REQUIREMENTS:
                    data = {**data, key: value[:MAX_REQUIREMENTS]}
        return data

    @model_validator(mode="after")
    def _enforce_ingest_caps(self) -> EvidenceObjectIngest:
        # Re-validate the children through their strict variants: the field
        # annotations are deliberately NOT narrowed (that would be an
        # invariant-list override), so the per-item caps are applied here.
        reqs: list[RequirementEvidence] = [
            RequirementEvidenceIngest.model_validate(r.model_dump())
            for r in self.requirements[:MAX_REQUIREMENTS]
        ]
        self.requirements = reqs
        themes: list[CoverLetterEvidence] = [
            CoverLetterEvidenceIngest.model_validate(c.model_dump())
            for c in self.cover_letter_evidence[:MAX_REQUIREMENTS]
        ]
        self.cover_letter_evidence = themes
        self.overall_summary = self.overall_summary[:MAX_OVERALL_TEXT_CHARS]
        self.overall_motivation = self.overall_motivation[:MAX_OVERALL_TEXT_CHARS]
        return self


class PipelineMeta(BaseModel):
    """Reproducibility stamp written to every shortlist_entries row."""

    model_config = ConfigDict(extra="forbid")

    model_gen: str
    model_emb: str
    prompt_versions: dict[str, str]
    weights: MatchWeights
    git_sha: str | None = None
    generated_at: dt.datetime
    timings_ms: dict[str, int] = Field(default_factory=dict)


class ShortlistEntry(BaseModel):
    """One row in /jobs/{id}/shortlist.

    The review-workflow fields (``current_decision`` / ``current_stage``) are
    CUT; ``blinded`` / ``display_label`` are blind-review (v1 scope) and stay.
    """

    model_config = ConfigDict(extra="forbid")

    id: UUID
    job_id: UUID
    resume_id: UUID
    rank: int
    score_final: float
    # The two composed sub-scores, named exactly as on ``JobMatchEntry`` (the
    # reverse-match sibling) so one convention covers both directions. The
    # difference is purely where they live at rest: ``reverse_match_entries``
    # has dedicated columns, ``shortlist_entries`` does not, so
    # ``persist_shortlist`` FOLDS them into the ``score_breakdown`` jsonb and
    # ``shortlist_service._parse_entry_jsonb`` unfolds them back onto here.
    #
    # OPTIONAL (unlike ``JobMatchEntry``, where they are required, because that
    # table HAS dedicated NOT NULL columns for them) and defaulted to ``None``,
    # not ``0.0``. Every instance of this DTO is built by ``model_validate``
    # from a stored row — ``grep -rn "ShortlistEntry(" core/src core/frontend``
    # returns no direct construction site at all — and a pre-4d row simply has
    # no folded sub-scores to unfold.
    #
    # ``None`` means "this row never recorded one" and renders as "not
    # recorded". ``0.0`` would render as an affirmative "0% contribution": a
    # POSITIVE FALSE CLAIM about a candidate, and asymmetric with the
    # ``pipeline_meta=None`` -> "weights unavailable" handling immediately
    # below, which already refuses to state what it does not know. The two
    # unavailability stories are deliberately told the same way.
    #
    # BOUNDED ``ge=0, le=1`` like every field on ``ScoreBreakdown`` and
    # ``MatchWeights``. That bound is what keeps ``NaN``/``inf`` off this DTO:
    # ``_folded_subscore``'s ``float(value)`` degrades only NON-numeric jsonb
    # (``TypeError``/``ValueError``), so a stored ``"Infinity"``/``"NaN"``
    # string parses to a real ``inf``/``nan`` and would reach the explanation
    # panel, whose ``pct()`` macro (``(v * 100)|round|int``) raises
    # ``OverflowError``/``ValueError`` out of Jinja's ``int`` filter — an
    # UNHANDLED 500 on a compliance page. Not candidate-reachable today
    # (Postgres rejects bare ``NaN``/``Infinity`` JSON literals and
    # ``persist_shortlist`` writes pipeline floats), so this is defence in
    # depth. It only pays off on the frontend: the Flask route
    # (``core/frontend/app.py::shortlist_entry_detail``) wraps its own
    # ``ShortlistEntry.model_validate`` call in a ``try/except ValidationError``
    # and degrades to "explanation unavailable" instead of 500ing. The backend
    # API read path (``_row_to_entry``/``_row_to_blind_entry`` in
    # ``shortlist_service.py``) validates **uncaught** — a corrupt stored value
    # would raise a 500 out of the API route rather than degrade there —
    # matching ``_parse_entry_jsonb``'s own docstring, which states this same
    # caveat for the folded ``score_structured``/``score_evidence`` values it
    # reads back.
    score_structured: float | None = Field(default=None, ge=0, le=1)
    score_evidence: float | None = Field(default=None, ge=0, le=1)
    score_breakdown: ScoreBreakdown
    evidence: EvidenceObject | None
    # The reproducibility stamp in force WHEN THIS ROW WAS GENERATED, read back
    # off the ``pipeline_meta`` jsonb column. The explanation panel takes its
    # weights from here and NEVER from current settings / ``DEFAULT_WEIGHTS``:
    # explaining a historical score with today's weights would be dishonest.
    # ``None`` on a legacy row (or one whose stamp is unreadable), which the
    # panel must surface as "weights unavailable" rather than substituting
    # defaults.
    pipeline_meta: PipelineMeta | None = None
    # ROADMAP A4 (evidence cliff). THREE states, and the third is the point:
    #
    #   True  — stage 3 ran for this candidate. A ``score_evidence`` of 0.0 is
    #           then a real measurement and must render as 0.
    #   False — past the ``evidence_k`` cliff. Never evaluated, so the stored
    #           0.0 came from COMPUTE PLACEMENT, not merit, and the panel must
    #           say "not assessed" rather than state a measured zero.
    #   None  — the row predates this marker (or its folded value was
    #           unreadable). We do not know which of the above applies, so the
    #           panel asserts neither.
    #
    # Deliberately NOT inferred from ``evidence.requirements == []``: that is
    # reading pipeline state off a display artifact, and a candidate evaluated
    # against a JD with no requirements has an empty list too. It is folded
    # into the ``score_breakdown`` jsonb by ``persist_shortlist`` and unfolded
    # by ``_parse_entry_jsonb``, exactly like the two sub-scores above, because
    # ``shortlist_entries`` has no column for it either.
    evidence_evaluated: bool | None = None
    generated_at: dt.datetime
    blinded: bool = False
    display_label: str | None = None


class JobMatchEntry(BaseModel):
    """One ranked job for a résumé in the reverse match (match-jobs)."""

    model_config = ConfigDict(extra="forbid")

    job_id: UUID
    title: str
    department: str | None = None
    rank: int
    score_final: float
    score_structured: float
    score_evidence: float
    score_breakdown: ScoreBreakdown
    evidence: EvidenceObject | None = None
    requirement_count: int
    must_have_count: int


class JobMatchResultOut(BaseModel):
    """Response for GET /resumes/{id}/match-results."""

    model_config = ConfigDict(extra="forbid")

    resume_id: UUID
    entries: list[JobMatchEntry] = Field(default_factory=list)
    pipeline_meta: PipelineMeta | None = None
    generated_at: dt.datetime | None = None


class ShortlistStateOut(BaseModel):
    """FU-7 §2 (ADR-021 §2 / ADR-029) — the fail-closed ranking state read
    off ``jobs`` by ``shortlist_service.get_shortlist_state``. Present only
    when a shortlist run failed closed and is awaiting a healthy LLM."""

    model_config = ConfigDict(extra="forbid")

    state: str
    reason: str | None
    at: dt.datetime


class ShortlistStatusResponse(BaseModel):
    """Response for GET /jobs/{id}/shortlist/status. ``state`` is ``None``
    (with ``reason``/``at`` also ``None``) when no run is in flight and the
    job carries no fail-closed ``awaiting_llm`` state. The other two legal
    values are ``'ranking'`` (a run is currently in flight) and
    ``'awaiting_llm'`` (a run failed closed and a retry is queued)."""

    model_config = ConfigDict(extra="forbid")

    job_id: UUID
    state: str | None = None
    reason: str | None = None
    at: dt.datetime | None = None


__all__ = [
    "CoverLetterEvidence",
    "CoverLetterEvidenceIngest",
    "CoverLetterTheme",
    "DEFAULT_WEIGHTS",
    "EvidenceObject",
    "EvidenceObjectIngest",
    "EvidenceStatus",
    "JobMatchEntry",
    "JobMatchResultOut",
    "MAX_EVIDENCE_CHUNK_IDS",
    "MAX_EVIDENCE_QUOTE_CHARS",
    "MAX_OVERALL_TEXT_CHARS",
    "MAX_REQUIREMENTS",
    "MAX_REQUIREMENT_CHARS",
    "MatchWeights",
    "PipelineMeta",
    "RequirementEvidence",
    "RequirementEvidenceIngest",
    "SCRUBBED_CONFIDENCE_CAP",
    "ScoreBreakdown",
    "ShortlistEntry",
    "ShortlistStateOut",
    "ShortlistStatusResponse",
    "SkillCategory",
    "SkillContribution",
]
