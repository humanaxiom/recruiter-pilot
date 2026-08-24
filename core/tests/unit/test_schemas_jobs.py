"""Unit tests for ``src/schemas/jobs.py`` — the jobs-domain pydantic contracts.

Phase 2 ports hris ``packages/schemas/src/schemas/jobs.py`` trimmed to
recruiter-assistant's scope. These tests pin, as merge-blocking contracts:

* the KEEP set is importable from the module AND re-exported by ``src.schemas``,
* the deliberate DEVIATIONS from hris (drop ``approval_required_2nd_review``;
  ``JobOut.created_by`` is a nullable ``str`` actor label, not a required UUID;
  ``JobCreate.blind_review`` defaults ``True`` per decision 4 / the Phase 0 DDL;
  ``JobListItem`` drops the cut JD-comment/Taleo columns),
* field constraints reject bad input and ``extra="forbid"``/``"ignore"`` behave
  per model.

These modules do not exist yet — this is the RED half of the TDD cycle.
"""

from __future__ import annotations

import datetime as dt
from uuid import uuid4

import pytest
from pydantic import ValidationError

import src.schemas as schemas_pkg
import src.schemas.jobs as jobs_mod
from src.schemas.jobs import (
    BulkJobResult,
    Education,
    JDExtracted,
    JDExtractText,
    JobAssigneeCreate,
    JobCreate,
    JobDeleteOut,
    JobListItem,
    JobOut,
    JobTransition,
    JobUpdate,
    Skill,
)

# A description long enough to clear the ``min_length=50`` bar (81 chars).
_DESC = "A detailed job description of the role. " * 3
_TS = dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.UTC)

# The full public surface the module and the package must expose.
JOBS_PUBLIC: tuple[str, ...] = (
    "JobStatus",
    "EmploymentType",
    "Seniority",
    "RemotePolicy",
    "JobCreate",
    "JobUpdate",
    "JobTransition",
    "JobOut",
    "JobDeleteOut",
    "JobListItem",
    "JDExtractText",
    "BulkJobResult",
    "Skill",
    "Education",
    "JDExtracted",
)

# hris fields cut from JobListItem: JD-comments (Feature 3) + Taleo ingest.
CUT_JOB_LIST_FIELDS: tuple[str, ...] = (
    "comment_count",
    "source",
    "external_last_seen_at",
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _job_out_kwargs(**over: object) -> dict[str, object]:
    """A complete, valid ``JobOut`` payload (every non-defaulted field set)."""
    base: dict[str, object] = {
        "id": uuid4(),
        "title": "Staff Engineer",
        "department": None,
        "location": None,
        "employment_type": None,
        "seniority": None,
        "min_years": None,
        "description_raw": _DESC,
        "description_parsed": None,
        "status": "open",
        "retention_days": 180,
        "shortlist_top_percent": 100,
        "blind_review": True,
        "failure_reason": None,
        "created_by": "recruiter@sfu.ca",
        "created_at": _TS,
        "updated_at": _TS,
        "parsed_at": None,
        "closed_at": None,
    }
    base.update(over)
    return base


# ── Public surface / re-export ───────────────────────────────────────────────


@pytest.mark.parametrize("name", JOBS_PUBLIC)
def test_module_exposes_keep_name(name: str) -> None:
    assert hasattr(jobs_mod, name)


@pytest.mark.parametrize("name", JOBS_PUBLIC)
def test_package_reexports_keep_name(name: str) -> None:
    assert hasattr(schemas_pkg, name)


def test_package_reexport_is_the_same_object() -> None:
    assert schemas_pkg.JobCreate is jobs_mod.JobCreate


# ── Happy-path validation ────────────────────────────────────────────────────


def test_job_create_minimal_valid() -> None:
    job = JobCreate(title="Dev", description_raw=_DESC)
    assert job.title == "Dev"
    assert job.retention_days == 180  # default


def test_job_update_all_fields_optional() -> None:
    upd = JobUpdate()
    assert upd.title is None
    assert upd.blind_review is None  # PATCH omit ⇒ "unchanged"


def test_job_transition_roundtrips() -> None:
    assert JobTransition(to="closed").to == "closed"


def test_job_out_minimal_valid() -> None:
    job = JobOut(**_job_out_kwargs())
    assert job.status == "open"


def test_job_delete_out_defaults_empty_warnings() -> None:
    out = JobDeleteOut(id=uuid4(), deleted=True, resume_count=3)
    assert out.cleanup_warnings == []


def test_job_list_item_minimal_valid() -> None:
    item = JobListItem(
        id=uuid4(),
        title="Dev",
        department=None,
        status="open",
        created_at=_TS,
        parsed_at=None,
    )
    assert item.title == "Dev"


def test_jd_extract_text_roundtrips() -> None:
    ex = JDExtractText(filename="jd.pdf", text="hello", chars=5)
    assert ex.chars == 5


def test_bulk_job_result_minimal_valid() -> None:
    res = BulkJobResult(original_filename="jd.pdf", outcome="created")
    assert res.job_id is None


def test_jd_extracted_roundtrips_faithfully() -> None:
    payload = {
        "title": "Backend Engineer",
        "required_skills": [{"name": "Python", "min_years": 3}],
        "nice_to_have_skills": [{"name": "Go"}],
        "min_years_experience": 5,
        "education": {"min_level": "bachelors", "fields": ["CS"]},
        "location": "Vancouver",
        "remote_policy": "hybrid",
        "responsibilities": ["Ship features"],
    }
    parsed = JDExtracted.model_validate(payload)
    again = JDExtracted.model_validate(parsed.model_dump())
    assert again.model_dump() == parsed.model_dump()


# ── DEVIATION 1: approval_required_2nd_review is dropped everywhere ───────────


@pytest.mark.parametrize("model", [JobCreate, JobUpdate, JobOut])
def test_approval_required_2nd_review_is_gone(model: type) -> None:
    """Review workflow is cut — the field must not exist on any job model."""
    assert "approval_required_2nd_review" not in model.model_fields


# ── DEVIATION 2: JobOut.created_by is a nullable str actor label, not a UUID ──


def test_job_out_created_by_accepts_a_plain_string() -> None:
    job = JobOut(**_job_out_kwargs(created_by="recruiter@sfu.ca"))
    assert job.created_by == "recruiter@sfu.ca"  # not coerced to a UUID


def test_job_out_created_by_accepts_none() -> None:
    job = JobOut(**_job_out_kwargs(created_by=None))
    assert job.created_by is None


def test_job_out_created_by_rejects_non_string() -> None:
    """A ``str | None`` field must reject a non-string, non-None value."""
    with pytest.raises(ValidationError):
        JobOut(**_job_out_kwargs(created_by=123))


# ── DEVIATION 3: blind_review defaults True on JobCreate (decision 4) ─────────


def test_job_create_blind_review_defaults_true() -> None:
    """hris defaulted this False; the port flips it to match the DDL default."""
    job = JobCreate(title="Dev", description_raw=_DESC)
    assert job.blind_review is True


# ── DEVIATION 4: JobListItem drops the JD-comment / Taleo columns ─────────────


@pytest.mark.parametrize("field", CUT_JOB_LIST_FIELDS)
def test_job_list_item_drops_cut_columns(field: str) -> None:
    assert field not in JobListItem.model_fields


# ── Field constraints ────────────────────────────────────────────────────────


def test_job_create_title_min_length() -> None:
    with pytest.raises(ValidationError):
        JobCreate(title="A", description_raw=_DESC)


def test_job_create_description_min_length_50() -> None:
    with pytest.raises(ValidationError):
        JobCreate(title="Dev", description_raw="x" * 49)


@pytest.mark.parametrize("bad", [29, 731])
def test_job_create_retention_days_bounds(bad: int) -> None:
    with pytest.raises(ValidationError):
        JobCreate(title="Dev", description_raw=_DESC, retention_days=bad)


@pytest.mark.parametrize("bad", [-1, 51])
def test_job_create_min_years_bounds(bad: int) -> None:
    with pytest.raises(ValidationError):
        JobCreate(title="Dev", description_raw=_DESC, min_years=bad)


@pytest.mark.parametrize("bad", [-1, 51])
def test_skill_min_years_bounds(bad: int) -> None:
    with pytest.raises(ValidationError):
        Skill(name="Python", min_years=bad)


def test_skill_name_min_length() -> None:
    with pytest.raises(ValidationError):
        Skill(name="")


# ── extra="forbid" vs extra="ignore" ─────────────────────────────────────────


def test_job_create_forbids_unknown_key() -> None:
    with pytest.raises(ValidationError):
        JobCreate.model_validate({"title": "Dev", "description_raw": _DESC, "bogus": 1})


def test_skill_ignores_unknown_key() -> None:
    skill = Skill.model_validate({"name": "Python", "bogus": 1})
    assert "bogus" not in skill.model_dump()


def test_education_ignores_unknown_key() -> None:
    edu = Education.model_validate({"min_level": "bachelors", "bogus": 1})
    assert "bogus" not in edu.model_dump()


def test_jd_extracted_ignores_unknown_key() -> None:
    jd = JDExtracted.model_validate({"title": "Dev", "bogus": 1})
    assert "bogus" not in jd.model_dump()


# ── FU-6 hardening: JobAssigneeCreate.note is length-capped ──────────────
# Reviewer + security both flagged that ``note`` was uncapped while flowing
# verbatim into audit_log.details JSONB (ADR-020 §Accepted residuals).


def test_job_assignee_create_note_accepts_a_normal_value() -> None:
    m = JobAssigneeCreate(user_id=uuid4(), note="reviewing for the EMEA req")
    assert m.note == "reviewing for the EMEA req"


def test_job_assignee_create_note_allows_none() -> None:
    assert JobAssigneeCreate(user_id=uuid4()).note is None


def test_job_assignee_create_note_rejects_an_overlong_value() -> None:
    with pytest.raises(ValidationError):
        JobAssigneeCreate(user_id=uuid4(), note="x" * 201)


# ── shortlist_top_percent (per-job configurable shortlist cap, slice A) ─────
# Schema-foundation slice: engine cap (slice B) and form (slice C) come later.
# 1-100, default 100 == today's "keep all ranked candidates" behaviour.


def test_job_create_shortlist_top_percent_defaults_100() -> None:
    job = JobCreate(title="Dev", description_raw=_DESC)
    assert job.shortlist_top_percent == 100


@pytest.mark.parametrize("value", [1, 100])
def test_job_create_shortlist_top_percent_accepts_bounds(value: int) -> None:
    job = JobCreate(title="Dev", description_raw=_DESC, shortlist_top_percent=value)
    assert job.shortlist_top_percent == value


@pytest.mark.parametrize("bad", [0, 101])
def test_job_create_shortlist_top_percent_rejects_out_of_range(bad: int) -> None:
    with pytest.raises(ValidationError):
        JobCreate(title="Dev", description_raw=_DESC, shortlist_top_percent=bad)


def test_job_update_shortlist_top_percent_defaults_none() -> None:
    """PATCH omit ⇒ 'unchanged', same convention as every other JobUpdate field."""
    upd = JobUpdate()
    assert upd.shortlist_top_percent is None


def test_job_update_shortlist_top_percent_accepts_a_valid_value() -> None:
    upd = JobUpdate(shortlist_top_percent=42)
    assert upd.shortlist_top_percent == 42


@pytest.mark.parametrize("bad", [0, 101])
def test_job_update_shortlist_top_percent_rejects_out_of_range(bad: int) -> None:
    with pytest.raises(ValidationError):
        JobUpdate(shortlist_top_percent=bad)


def test_job_out_shortlist_top_percent_round_trips() -> None:
    job = JobOut(**_job_out_kwargs(shortlist_top_percent=55))
    assert job.shortlist_top_percent == 55


def test_job_out_shortlist_top_percent_is_required() -> None:
    """No default on JobOut — every read must carry the persisted value."""
    kwargs = _job_out_kwargs()
    del kwargs["shortlist_top_percent"]
    with pytest.raises(ValidationError):
        JobOut(**kwargs)
