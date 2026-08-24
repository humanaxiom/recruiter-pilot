"""Shared pydantic schemas for the resumes domain.

- ResumeChunk   — deterministic text chunk with stable id (c_NNN)
- Experience / Bullet / EducationItem / CandidateInfo — sub-shapes
- ResumeParsed  — LLM output schema (chat_json target). extra='ignore'
- ResumeOut / ResumeListItem — API responses (with PII flag, never
  the encrypted bytes)
- ResumeUploadResult — per-file outcome from the bulk-upload endpoint

Ported from hris ``packages/schemas/src/schemas/resumes.py``; the ``Skill``
import is retargeted to ``src.schemas.jobs``. Deviations from the hris source
are commented inline with "DEVIATION".
"""

from __future__ import annotations

import datetime as dt
from typing import Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from src.schemas.jobs import Skill  # reused — same shape

ResumeStatus = Literal["uploaded", "parsing", "parsed", "failed"]


# Year coercion shared between ResumeSkill.last_used_year and
# EducationItem.year. Small models (qwen2.5:3b in particular) sometimes
# emit two-digit years ("worked with Loop in '20") which would otherwise
# fail `ge=1900`. We apply the same windowing convention python's
# `datetime.strptime("%y")` and Excel use: 0-68 -> 20xx, 69-99 -> 19xx.
# Anything still out of range returns None so the whole parse doesn't
# fail over one bad cell — the field is optional anyway.
_TWO_DIGIT_PIVOT = 69


def _coerce_year(value: object) -> int | None:
    if isinstance(value, str):
        try:
            value = int(value.strip())
        except ValueError:
            return None
    # bool is an int subclass; explicitly reject so True/False don't slip through.
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    if 1900 <= value <= 2100:
        return value
    if 0 <= value < 100:
        return 2000 + value if value < _TWO_DIGIT_PIVOT else 1900 + value
    return None


# ---------------- LLM / parsed shapes ----------------


class ResumeChunk(BaseModel):
    """One chunk of resume text (built by `chunker`, NOT the LLM).

    The id is a deterministic ``c_NNN`` token; the LLM cites these ids
    in `evidence_chunk_ids` to anchor its claims. Citations that don't
    exist in the chunk set are scrubbed before persistence.
    """

    model_config = ConfigDict(extra="ignore")

    id: str = Field(min_length=2, max_length=20)
    section: str = Field(default="other")
    page: int = Field(default=0, ge=0)
    text: str = Field(min_length=1, max_length=4000)


class Bullet(BaseModel):
    model_config = ConfigDict(extra="ignore")

    text: str = Field(max_length=1000)
    # A chunk id is a ``c_NNN``/``cl_NNN`` token — same cap as ResumeChunk.id.
    chunk_id: str | None = Field(default=None, max_length=20)


class Experience(BaseModel):
    model_config = ConfigDict(extra="ignore")

    company: str = Field(max_length=300)
    title: str = Field(max_length=300)
    start: str | None = Field(default=None, max_length=50)  # "YYYY-MM" or "YYYY"
    end: str | None = Field(default=None, max_length=50)
    is_current: bool = False
    bullets: list[Bullet] = Field(default_factory=list, max_length=20)


class EducationItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    degree: str = Field(max_length=300)
    institution: str = Field(max_length=300)
    field: str | None = Field(default=None, max_length=300)
    year: int | None = Field(default=None, ge=1900, le=2100)

    @field_validator("year", mode="before")
    @classmethod
    def _coerce_year(cls, v: object) -> int | None:
        return _coerce_year(v)


class CandidateInfo(BaseModel):
    """The PII subset of the parsed resume. Never embedded; encrypted at rest.

    Every field is capped: these strings flow straight from an untrusted local
    LLM response into a ``pgp_sym_encrypt`` BYTEA column, so a looping small
    model must not be able to push an unbounded blob through the encrypt path.
    """

    model_config = ConfigDict(extra="ignore")

    name: str | None = Field(default=None, max_length=300)
    email: str | None = Field(default=None, max_length=300)
    phone: str | None = Field(default=None, max_length=300)
    location: str | None = Field(default=None, max_length=300)


class ResumeSkill(BaseModel):
    """Skill as the LLM extracts it from a resume.

    `name` is generous (200 chars) for the same reason JD `Skill.name`
    is: small models emit short phrases ("Microsoft Loop: advanced
    document workflows") rather than canonical tokens. skill_normalize
    canonicalises + dedupes downstream.

    `last_used_year` runs through `_coerce_year` first so the common
    "20" → 2020 case doesn't fail the whole resume.
    """

    model_config = ConfigDict(extra="ignore")

    name: str = Field(min_length=1, max_length=200)
    years: int | None = Field(default=None, ge=0, le=80)
    last_used_year: int | None = Field(default=None, ge=1900, le=2100)
    evidence_chunk_ids: list[str] = Field(default_factory=list, max_length=8)
    # ROADMAP A2 Phase 3.3 (skill-family classifier, slice 1): families a
    # PARSE-TIME classifier assigned to an out-of-vocab skill name
    # (`src.pipeline.skill_classifier.classify_families`). Absent (`None`),
    # NOT `[]`, when the classifier gave no confident answer or was never run
    # (in-vocab skill, classifier failure) — that is what keeps a résumé
    # parsed before this feature shipped byte-identical to one parsed after
    # it, for any skill the classifier declines to touch. Capped at
    # `skill_classifier._MAX_FAMILIES_PER_SKILL` (2) as a second line of
    # defence: family credit is transitive across the whole résumé
    # (`orchestrator.py`), so this schema ceiling holds even if a future
    # classifier bug tries to over-assign.
    categories: list[str] | None = Field(default=None, max_length=2)

    @field_validator("last_used_year", mode="before")
    @classmethod
    def _coerce_year(cls, v: object) -> int | None:
        return _coerce_year(v)


CoverLetterSentiment = Literal["positive", "neutral", "negative"]


class CoverLetterParsed(BaseModel):
    """LLM output for cover_letter_v1 (Feature 1). Stored in
    resumes.cover_letter_parsed (jsonb). Lightweight: the raw text plus
    extracted themes + key claims. PII in `raw_text` is encrypted at the
    storage layer and redacted at the display layer — never trusted here.
    """

    model_config = ConfigDict(extra="ignore")

    raw_text: str = Field(default="", max_length=20000)
    themes: list[str] = Field(default_factory=list, max_length=20)
    key_claims: list[str] = Field(default_factory=list, max_length=20)
    sentiment: CoverLetterSentiment | None = None


class ResumeParsed(BaseModel):
    """LLM output schema. Stored verbatim in resumes.parsed (jsonb).

    Lossy mode for list rows: `skills`, `experience`, and `education`
    are filtered through a model-level pre-validator that drops rows
    failing their own sub-schema. This is deliberate. The alternative —
    failing the whole resume on one malformed entry — surfaces as
    `status='failed'` for the recruiter, who then can't see the other
    20 perfectly good skills the model extracted. We log the dropped
    rows so quality regressions are visible without holding the parse
    hostage.
    """

    model_config = ConfigDict(extra="ignore")

    candidate: CandidateInfo = Field(default_factory=CandidateInfo)
    summary: str = Field(default="", max_length=2000)
    total_years_experience: int = Field(default=0, ge=0, le=80)
    # Bounded at 400, not the curated vocabulary's current 306 canonicals: a
    # cap at/below the vocabulary size would silently truncate skills the
    # deterministic scan trustworthily found verbatim as the vocabulary
    # grows (see `_MAX_SKILLS` in `worker/resume_tasks.py`, which mirrors
    # this and MUST stay in sync).
    skills: list[ResumeSkill] = Field(default_factory=list, max_length=400)
    experience: list[Experience] = Field(default_factory=list, max_length=30)
    education: list[EducationItem] = Field(default_factory=list, max_length=10)
    # Chunks are populated by the chunker (not the LLM); included here
    # so the persisted parsed json carries the chunk text alongside the
    # extraction, for evidence display + verifier audit.
    chunks: list[ResumeChunk] = Field(default_factory=list, max_length=200)
    # Cover-letter chunks (Feature 1) live in a PARALLEL `cl_NNN` id space so
    # evidence citations never collide with résumé `c_NNN`. Empty when there's
    # no cover letter.
    cover_letter_chunks: list[ResumeChunk] = Field(default_factory=list, max_length=100)
    # FU-7 §4 / ADR-030: degraded-parse visibility. Set True when skills
    # extraction fell back to the deterministic keyword scan (the
    # `resume_skills_v2` `LLMOutputInvalidError` catch in the worker). Rides the
    # existing `resumes.parsed` jsonb verbatim — NO DDL change. `extra="ignore"`
    # + these defaults mean pre-feature rows (no keys) read back non-degraded.
    # NOT PII — flows through `ResumeOut` unredacted. A degraded parse is
    # persisted + visible but NOT projected to Neo4j → excluded from ranking.
    degraded: bool = False
    degradation_reason: str | None = Field(default=None, max_length=200)

    @model_validator(mode="before")
    @classmethod
    def _drop_invalid_rows(cls, data: object) -> object:
        """Filter out individual list rows that won't validate.

        Runs BEFORE field validation, so a malformed skill row is
        silently dropped rather than failing the whole resume. We
        validate each candidate row through its own sub-schema
        (which itself runs the `_coerce_year` / name-truncation fixes
        first), and only keep the survivors.

        An ALREADY-VALIDATED sub-model instance passes straight through.
        Without that branch this filter drops every row of
        ``ResumeParsed(skills=[ResumeSkill(...)])`` on the floor — the
        rows are not dicts, so the "is it a dict?" guard rejects them and
        the caller silently gets an empty list. hris only ever fed this
        validator raw dicts, so the hole never showed there.
        """
        if not isinstance(data, dict):
            return data
        for key, sub in (
            ("skills", ResumeSkill),
            ("experience", Experience),
            ("education", EducationItem),
        ):
            rows = data.get(key)
            if not isinstance(rows, list):
                continue
            kept: list[object] = []
            for row in rows:
                if isinstance(row, sub):
                    kept.append(row)
                    continue
                if not isinstance(row, dict):
                    continue
                try:
                    sub.model_validate(row)
                except ValidationError:
                    continue
                kept.append(row)
            data[key] = kept
        return data


class ResumeCore(BaseModel):
    """The narrative half of a resume — everything EXCEPT skills.

    Resume extraction is split into a bounded "core" LLM call (this) and
    a separate skills step, because small local models reliably produce
    this much but loop/over-generate on the open-ended skills enumeration.
    Same lossy row-dropping as ResumeParsed so one bad experience/education
    row doesn't fail the whole parse.
    """

    model_config = ConfigDict(extra="ignore")

    candidate: CandidateInfo = Field(default_factory=CandidateInfo)
    summary: str = Field(default="", max_length=2000)
    total_years_experience: int = Field(default=0, ge=0, le=80)
    experience: list[Experience] = Field(default_factory=list, max_length=30)
    education: list[EducationItem] = Field(default_factory=list, max_length=10)

    @model_validator(mode="before")
    @classmethod
    def _drop_invalid_rows(cls, data: object) -> object:
        # Same lossy filter as ResumeParsed, including the already-validated
        # sub-model pass-through — see the docstring there.
        if not isinstance(data, dict):
            return data
        for key, sub in (("experience", Experience), ("education", EducationItem)):
            rows = data.get(key)
            if not isinstance(rows, list):
                continue
            kept: list[object] = []
            for row in rows:
                if isinstance(row, sub):
                    kept.append(row)
                    continue
                if not isinstance(row, dict):
                    continue
                try:
                    sub.model_validate(row)
                except ValidationError:
                    continue
                kept.append(row)
            data[key] = kept
        return data


class ResumeSkillNames(BaseModel):
    """Output of the skills-only LLM sub-call: a flat list of names.

    Deliberately the simplest possible shape (bare strings, no per-skill
    objects/citations) so the model can't loop on nested structure. A
    lenient pre-validator also accepts ``[{"name": ...}]`` in case the
    model emits objects anyway, and drops anything non-string.
    """

    model_config = ConfigDict(extra="ignore")

    skills: list[str] = Field(default_factory=list, max_length=200)

    @model_validator(mode="before")
    @classmethod
    def _coerce_names(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data
        rows = data.get("skills")
        if isinstance(rows, list):
            names: list[str] = []
            for s in rows:
                if isinstance(s, str):
                    names.append(s)
                elif isinstance(s, dict) and isinstance(s.get("name"), str):
                    names.append(s["name"])
            data["skills"] = names[:200]
        return data


class ResumeSkillDetail(BaseModel):
    """One skill from the resume_skills_v2 sub-call: a name plus OPTIONAL
    years / last_used_year (the model is told to include them only when the
    résumé states them explicitly). Both run through lenient coercion so a
    garbage value becomes ``None`` rather than failing the whole batch."""

    model_config = ConfigDict(extra="ignore")

    name: str = Field(min_length=1, max_length=200)
    years: int | None = Field(default=None, ge=0, le=80)
    last_used_year: int | None = Field(default=None, ge=1900, le=2100)

    @field_validator("years", mode="before")
    @classmethod
    def _coerce_years(cls, v: object) -> int | None:
        if v is None or isinstance(v, bool):
            return None
        try:
            n = int(v)  # type: ignore[call-overload]  # str/float/int → int; junk raises
        except (TypeError, ValueError):
            return None
        return n if 0 <= n <= 80 else None

    @field_validator("last_used_year", mode="before")
    @classmethod
    def _coerce_year(cls, v: object) -> int | None:
        return _coerce_year(v)


class ResumeSkillDetails(BaseModel):
    """Output of the resume_skills_v2 sub-call: bounded per-skill objects with
    optional years. Re-introducing per-skill years (the field, projection, and
    scorer already honour them) WITHOUT re-triggering the skills loop: still a
    flat, bounded list; the pre-validator accepts bare strings or objects and
    DROPS malformed entries, so a bad/looping LLM list never fails the parse —
    the caller keeps the deterministic name scan as the floor."""

    model_config = ConfigDict(extra="ignore")

    skills: list[ResumeSkillDetail] = Field(default_factory=list, max_length=200)

    @model_validator(mode="before")
    @classmethod
    def _coerce_rows(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data
        rows = data.get("skills")
        if isinstance(rows, list):
            cleaned: list[object] = []
            for s in rows:
                # An already-validated detail passes straight through: without
                # this branch ``ResumeSkillDetails(skills=[ResumeSkillDetail(
                # ...)])`` silently yields an empty list (the row is neither a
                # str nor a dict, so both coercion arms skip it).
                if isinstance(s, ResumeSkillDetail):
                    cleaned.append(s)
                elif isinstance(s, str) and s.strip():
                    cleaned.append({"name": s})
                elif isinstance(s, dict) and isinstance(s.get("name"), str):
                    cleaned.append(
                        {
                            "name": s["name"],
                            "years": s.get("years"),
                            "last_used_year": s.get("last_used_year"),
                        }
                    )
            data["skills"] = cleaned[:200]
        return data


# ---------------- HTTP request / response shapes ----------------


class ResumeUploadResult(BaseModel):
    """One row in the response to bulk POST /jobs/{id}/resumes."""

    model_config = ConfigDict(extra="forbid")

    original_filename: str
    outcome: Literal["accepted", "duplicate", "rejected"]
    resume_id: UUID | None = None
    reason: str | None = None
    # The cover letter (Feature 2) paired to this résumé via filename
    # convention or a manifest, when one was attached this upload.
    cover_letter_filename: str | None = None
    # Non-fatal pairing notes for this row (e.g. a cover-named file with no
    # matching résumé that was ingested as a résumé). Empty in the common case.
    warnings: list[str] = Field(default_factory=list)


class ResumeListItem(BaseModel):
    """Lightweight row for GET /jobs/{id}/resumes."""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    original_filename: str
    status: ResumeStatus
    uploaded_at: dt.datetime
    parsed_at: dt.datetime | None
    # The decrypted candidate name shows in lists; null until parsed
    # or if decryption fails (caller without app.pii_key set).
    candidate_name: str | None = None
    # Whether a cover letter is attached (Feature 2) — drives a list badge so a
    # recruiter can see at a glance which résumés carry one.
    has_cover_letter: bool = False
    # ADR-026 (FU-8): the résumé-withdrawal lifecycle pair. Both None for a
    # résumé that has never been withdrawn. NOT PII — no redaction applies.
    withdrawn_at: dt.datetime | None = None
    withdrawal_reason: str | None = None
    # FU-7 §4 / ADR-030: True when this résumé's skills extraction fell back to
    # the keyword scan. Read from the `resumes.parsed` jsonb
    # (`COALESCE((parsed->>'degraded')::bool, false)`). Surfaces the incident's
    # blind spot — a degraded résumé looked complete in the list. NOT PII.
    degraded: bool = False


class ResumeOut(BaseModel):
    """GET /resumes/{id} — full record. PII fields are the DECRYPTED
    plaintext (never the bytea). Decryption happens at the route layer
    after `SET app.pii_key`.
    """

    model_config = ConfigDict(extra="forbid")

    id: UUID
    job_id: UUID
    original_filename: str
    mime_type: str
    file_size_bytes: int
    sha256: str
    candidate: CandidateInfo
    candidate_email_hash: str | None
    parsed: ResumeParsed | None
    status: ResumeStatus
    # DEVIATION: hris typed this ``UUID`` (FK to a users table). The Phase 0 DDL
    # made ``uploaded_by`` a nullable TEXT actor label (no users table in v1).
    uploaded_by: str | None
    uploaded_at: dt.datetime
    parsed_at: dt.datetime | None
    failure_reason: str | None
    consent_acknowledged: bool
    # True when this response is blind-masked (identity + employers/schools/grad
    # years hidden); drives the UI's blind banner + reveal button.
    blinded: bool = False
    # Cover letter (Feature 1), optional. `cover_letter_text` is the DECRYPTED
    # plaintext (redacted under blind review); `cover_letter_parsed` is the
    # LLM extraction. Both None when the résumé has no cover letter.
    cover_letter_text: str | None = None
    cover_letter_parsed: CoverLetterParsed | None = None
    # ADR-026 (FU-8): the résumé-withdrawal lifecycle pair. Both None for a
    # résumé that has never been withdrawn. NOT PII — no redaction applies.
    withdrawn_at: dt.datetime | None = None
    withdrawal_reason: str | None = None


class WithdrawRequest(BaseModel):
    """POST /resumes/{id}/withdraw body (ADR-026 decision 2, FU-8).

    ``reason`` is an OPTIONAL free-text note (capped at 500 chars) recorded on
    the résumé + the audit row. Absent/``None`` is a legitimate withdrawal.
    """

    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=500)


class ResumeStatusBreakdown(BaseModel):
    """GET /jobs/{id}/resume-status response (ADR-026 decision 5, FU-8).

    A closed, five-bucket, non-negative-int shape — every bucket is required
    (no per-field default) so a service-layer bug that forgets one 500s loudly
    at construction rather than silently reporting zero. ``withdrawn`` is a
    peer bucket, counted from ``withdrawn_at IS NOT NULL`` rather than the
    ``resume_status`` enum (which has no ``withdrawn`` value).

    FU-7 §4 / ADR-030: ``degraded`` is a SUB-count of ``parsed`` (degraded ⊆
    parsed), NOT a disjoint peer bucket like ``withdrawn``. ``parsed`` still
    counts ALL parsed rows; ``degraded`` additionally reports how many of them
    fell back to the keyword scan. A job with 7 parsed résumés, 2 degraded,
    reports ``parsed=7, degraded=2`` — never double-count them out of
    ``parsed``. Required (no default), same fail-loud contract as the five
    lifecycle buckets."""

    model_config = ConfigDict(extra="forbid")

    uploaded: int = Field(ge=0)
    parsing: int = Field(ge=0)
    parsed: int = Field(ge=0)
    failed: int = Field(ge=0)
    withdrawn: int = Field(ge=0)
    degraded: int = Field(ge=0)


class ResumeDeleteOut(BaseModel):
    """DELETE /resumes/{id} — confirmation of a single-résumé cascade delete.

    ``cleanup_warnings`` is non-empty only when best-effort graph/blob cleanup
    hit a non-fatal error after the DB delete committed (the row + audit are
    already gone; reconcile catches drift) — same shape as ``JobDeleteOut``."""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    deleted: bool
    cleanup_warnings: list[str] = Field(default_factory=list)


__all__ = [
    "Bullet",
    "CandidateInfo",
    "EducationItem",
    "Experience",
    "ResumeChunk",
    "ResumeDeleteOut",
    "ResumeListItem",
    "ResumeOut",
    "ResumeParsed",
    "ResumeSkill",
    "ResumeStatus",
    "ResumeStatusBreakdown",
    "ResumeUploadResult",
    "Skill",
    "WithdrawRequest",
]
