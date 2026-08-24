"""Shared pydantic schemas for the jobs domain.

These are the contracts that cross package boundaries:
  - JobCreate / JobUpdate / JobOut / JobListItem — API request/response
  - JobStatus enum — must match the postgres role_enum
  - JDExtracted — LLM output (passed to LLMClient.chat_json)

If you add a field here, you almost certainly need a follow-up DDL change
and a form change. Treat this file as the API contract.

Ported from hris ``packages/schemas/src/schemas/jobs.py`` and trimmed to
recruiter-assistant's scope (review workflow + Taleo/JD-comments cut) and
aligned to the Phase 0 DDL. Deviations from the hris source are commented
inline with "DEVIATION".
"""

from __future__ import annotations

import datetime as dt
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

JobStatus = Literal["draft", "open", "closed", "archived"]
EmploymentType = Literal["full_time", "part_time", "contract", "intern"]
Seniority = Literal["junior", "mid", "senior", "staff", "principal", "director", "vp"]
RemotePolicy = Literal["onsite", "hybrid", "remote"]


# ---------- HTTP request bodies ----------


class JobCreate(BaseModel):
    """POST /api/v1/jobs body."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=2, max_length=200)
    department: str | None = Field(default=None, max_length=100)
    location: str | None = Field(default=None, max_length=200)
    employment_type: EmploymentType | None = None
    seniority: Seniority | None = None
    min_years: int | None = Field(default=None, ge=0, le=50)
    description_raw: str = Field(min_length=50, max_length=200_000)
    retention_days: int = Field(default=180, ge=30, le=730)
    # Per-job configurable shortlist cap (slice A, schema foundation only —
    # the engine cap and the form land in slices B/C). 1-100, default 100 ==
    # today's "keep all ranked candidates" behaviour.
    shortlist_top_percent: int = Field(default=100, ge=1, le=100)
    # DEVIATION: dropped hris ``approval_required_2nd_review`` — the 2nd-review
    # workflow is cut and the Phase 0 DDL has no such column.
    # When true, the review surfaces hide candidate identity (blind review).
    # DEVIATION: hris defaulted this False; flipped to True to match the Phase 0
    # DDL default (blind-by-default, decision 4).
    blind_review: bool = True


class JobUpdate(BaseModel):
    """PATCH /api/v1/jobs/{id} body — every field optional."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=2, max_length=200)
    department: str | None = Field(default=None, max_length=100)
    location: str | None = Field(default=None, max_length=200)
    employment_type: EmploymentType | None = None
    seniority: Seniority | None = None
    min_years: int | None = Field(default=None, ge=0, le=50)
    description_raw: str | None = Field(default=None, min_length=50, max_length=200_000)
    retention_days: int | None = Field(default=None, ge=30, le=730)
    # PATCH omit ⇒ "unchanged", same convention as every other JobUpdate field.
    shortlist_top_percent: int | None = Field(default=None, ge=1, le=100)
    # DEVIATION: dropped hris ``approval_required_2nd_review`` — review workflow cut.
    # A PATCH omit (None) means "unchanged".
    blind_review: bool | None = None


class JobTransition(BaseModel):
    """POST /api/v1/jobs/{id}/transition body."""

    model_config = ConfigDict(extra="forbid")

    to: JobStatus


class JobAssigneeCreate(BaseModel):
    """POST /jobs/{job_id}/assignees body (ADR-020 §2, FU-6 slice 3).

    ``user_id`` names the user being assigned to the job; ``note`` is an
    optional free-text context string forwarded verbatim into the
    ``assign_job`` audit row's ``details`` (never persisted anywhere else —
    ``job_assignees`` itself has no note column).
    """

    model_config = ConfigDict(extra="forbid")

    user_id: UUID
    # Capped like the other free-text job fields (department/location) — ``note``
    # rides verbatim into the ``assign_job`` audit_log.details JSONB, so an
    # unbounded value is an at-rest storage-growth vector (reviewer + security,
    # FU-6 gates). 200 chars is ample for operational context.
    note: str | None = Field(default=None, max_length=200)


# ---------- HTTP responses ----------


class JobOut(BaseModel):
    """GET /api/v1/jobs/{id} — single job (includes the full parsed JSON)."""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    title: str
    department: str | None
    location: str | None
    employment_type: EmploymentType | None
    seniority: Seniority | None
    min_years: int | None
    description_raw: str
    description_parsed: JDExtracted | None
    status: JobStatus
    retention_days: int
    # Required (no default) — every read must carry the persisted value.
    shortlist_top_percent: int
    # DEVIATION: dropped hris ``approval_required_2nd_review`` — review workflow cut.
    blind_review: bool = False
    failure_reason: str | None
    # DEVIATION: hris typed this ``UUID`` (FK to a users table). The Phase 0 DDL
    # made ``created_by`` a nullable TEXT actor label (no users table / CAS in v1).
    created_by: str | None
    created_at: dt.datetime
    updated_at: dt.datetime
    parsed_at: dt.datetime | None
    closed_at: dt.datetime | None


class JobReparseOut(BaseModel):
    """POST /api/v1/jobs/{id}/reparse — the retry was accepted onto the queue.

    Deliberately NOT a ``JobOut``: the row is unchanged apart from a cleared
    ``failure_reason``, and returning the full job would invite a caller to
    read ``parsed_at`` from it as if the retry had already run. 202 plus this
    two-field acknowledgement says what actually happened — queued, not done.
    """

    model_config = ConfigDict(extra="forbid")

    id: UUID
    status: Literal["queued"]


class JobDeleteOut(BaseModel):
    """DELETE /api/v1/jobs/{id} — confirmation of a cascade delete.

    ``resume_count`` is how many resume records were removed alongside
    the job. ``cleanup_warnings`` is non-empty only when best-effort
    graph/blob cleanup hit a non-fatal error after the DB delete
    committed (the row + audit are already gone; reconcile catches drift).
    """

    model_config = ConfigDict(extra="forbid")

    id: UUID
    deleted: bool
    resume_count: int
    cleanup_warnings: list[str] = Field(default_factory=list)


class JobListItem(BaseModel):
    """GET /api/v1/jobs — light row for list views (no parsed JSON)."""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    title: str
    department: str | None
    status: JobStatus
    created_at: dt.datetime
    parsed_at: dt.datetime | None
    # DEVIATION: dropped hris ``comment_count`` (JD comments / Feature 3),
    # ``source`` and ``external_last_seen_at`` (Taleo ingest provenance) —
    # both features are cut and the Phase 0 DDL has none of these columns.


class JDExtractText(BaseModel):
    """POST /api/v1/jobs/jd-extract — plain text pulled from an uploaded
    JD file (txt/json/pdf/docx). The client pre-fills the description
    field with this; nothing is persisted server-side."""

    model_config = ConfigDict(extra="forbid")

    filename: str
    text: str
    chars: int


class BulkJobResult(BaseModel):
    """One row in the response to POST /api/v1/jobs/bulk.

    The endpoint returns 202 with an array of these — one per file fed
    in (after any ZIP is expanded). ``outcome`` is ``created`` (a draft
    job was inserted + parse enqueued), ``duplicate`` (an existing job
    already has identical JD text — nothing inserted; ``job_id`` points at
    it), or ``failed`` (couldn't extract JD text, too short, etc.); the UI
    surfaces the per-file ``reason``. ``title`` is the title the job was
    created with (filename stem, or a CSV-manifest value) — it may later be
    refined by the LLM parse.
    """

    model_config = ConfigDict(extra="forbid")

    original_filename: str
    outcome: Literal["created", "duplicate", "failed"]
    job_id: UUID | None = None
    title: str | None = None
    reason: str | None = None


# ---------- LLM output schema ----------


class Skill(BaseModel):
    """One skill as the LLM extracts it from a JD.

    `name` is generous (200 chars) because small LLMs (3B/7B) sometimes
    emit a short phrase ("CI/CD pipelines with Terraform") rather than
    a single canonical token. The downstream skill_normalize step
    canonicalises + dedupes, so loose names here are fine.
    """

    model_config = ConfigDict(extra="ignore")

    name: str = Field(min_length=1, max_length=200)
    min_years: int | None = Field(default=None, ge=0, le=50)


class Education(BaseModel):
    model_config = ConfigDict(extra="ignore")

    min_level: (
        Literal["high_school", "associate", "bachelors", "masters", "phd"] | None
    ) = None
    # Capped like every other LLM-emitted list on this shape: an uncapped list
    # is the same unbounded-JSONB hole as an uncapped string.
    fields: list[str] = Field(default_factory=list, max_length=20)


class JDExtracted(BaseModel):
    """Strict schema the LLM must return for a job description.

    Drives LLMClient.chat_json — extras dropped, types coerced where
    sensible. Stored verbatim in jobs.description_parsed (JSONB).
    """

    model_config = ConfigDict(extra="ignore")

    title: str = Field(min_length=1, max_length=200)
    required_skills: list[Skill] = Field(default_factory=list, max_length=50)
    nice_to_have_skills: list[Skill] = Field(default_factory=list, max_length=50)
    min_years_experience: int = Field(default=0, ge=0, le=50)
    education: Education | None = None
    location: str | None = Field(default=None, max_length=200)
    remote_policy: RemotePolicy | None = None
    responsibilities: list[str] = Field(default_factory=list, max_length=20)


# Resolve the forward reference now that JDExtracted is defined.
JobOut.model_rebuild()
