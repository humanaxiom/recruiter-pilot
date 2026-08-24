"""Unit tests for ``src/schemas/resumes.py`` — the resumes-domain contracts.

Phase 2 ports hris ``packages/schemas/src/schemas/resumes.py`` (import fixed to
``from src.schemas.jobs import Skill``). These tests pin:

* the KEEP set is importable from the module AND re-exported by ``src.schemas``,
* the ``_coerce_year`` two-digit-pivot / bool-rejection / range behaviour,
* ``ResumeParsed._drop_invalid_rows`` (lossy per-row filter) and the
  ``ResumeSkillNames`` / ``ResumeSkillDetails`` coercers (str-or-dict, cap 200),
* the DEVIATION ``ResumeOut.uploaded_by: str | None`` (nullable actor label),
* ``ResumeOut`` is the PII redaction boundary — it exposes ``candidate`` but
  never the ``candidate_email`` / ``blob_key`` ciphertext,
* ``extra="forbid"``/``"ignore"`` behave per model.
* ADR-026 (FU-8) résumé-withdrawal lifecycle contracts: ``ResumeOut`` /
  ``ResumeListItem`` gain ``withdrawn_at``/``withdrawal_reason``;
  ``WithdrawRequest`` caps ``reason`` at 500 chars; ``ResumeStatusBreakdown``
  is a closed, five-bucket, non-negative-int shape.

These modules do not exist yet — this is the RED half of the TDD cycle.
"""

from __future__ import annotations

import datetime as dt
from uuid import uuid4

import pytest
from pydantic import ValidationError

import src.schemas as schemas_pkg
import src.schemas.resumes as resumes_mod
from src.schemas.resumes import (
    CandidateInfo,
    CoverLetterParsed,
    EducationItem,
    Experience,
    ResumeChunk,
    ResumeCore,
    ResumeDeleteOut,
    ResumeListItem,
    ResumeOut,
    ResumeParsed,
    ResumeSkill,
    ResumeSkillDetail,
    ResumeSkillDetails,
    ResumeSkillNames,
    ResumeStatusBreakdown,
    ResumeUploadResult,
    WithdrawRequest,
    _coerce_year,
)

_TS = dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.UTC)

# The public surface the module and package must expose. ``Skill`` is re-used
# from jobs and re-exported by resumes; ``ResumeCore`` / ``ResumeSkill*`` are
# defined in the module even though hris omits them from ``__all__``.
# ADR-026 (FU-8): ``WithdrawRequest``/``ResumeStatusBreakdown`` are NEW —
# résumé-withdrawal lifecycle request/response shapes.
RESUMES_PUBLIC: tuple[str, ...] = (
    "ResumeStatus",
    "ResumeChunk",
    "Bullet",
    "Experience",
    "EducationItem",
    "CandidateInfo",
    "ResumeSkill",
    "CoverLetterSentiment",
    "CoverLetterParsed",
    "ResumeParsed",
    "ResumeCore",
    "ResumeSkillNames",
    "ResumeSkillDetail",
    "ResumeSkillDetails",
    "ResumeUploadResult",
    "ResumeListItem",
    "ResumeOut",
    "ResumeDeleteOut",
    "WithdrawRequest",
    "ResumeStatusBreakdown",
    "Skill",
)

# (raw value, coerced year) — the two-digit pivot is 69: <69 ⇒ 20xx, ≥69 ⇒ 19xx.
YEAR_CASES: tuple[tuple[object, int | None], ...] = (
    ("23", 2023),
    ("70", 1970),
    (1995, 1995),
    (True, None),  # bool is an int subclass — must be rejected
    (False, None),
    (2200, None),  # out of the 1900–2100 window and not a 2-digit year
    ("junk", None),
)

# (key, good_row, bad_row) for the lossy ``_drop_invalid_rows`` filter.
DROP_CASES: tuple[tuple[str, dict[str, object], dict[str, object]], ...] = (
    ("skills", {"name": "Python"}, {"name": ""}),  # empty name < min_length=1
    ("experience", {"company": "Acme", "title": "Dev"}, {"company": "Acme"}),
    ("education", {"degree": "BSc", "institution": "UBC"}, {"degree": "BSc"}),
)

# (raw value, coerced years) for ResumeSkillDetail._coerce_years (range 0–80).
YEARS_CASES: tuple[tuple[object, int | None], ...] = (
    ("3", 3),
    (3.0, 3),
    (3, 3),
    (True, None),
    (None, None),
    ("junk", None),
    (200, None),  # over the 80 cap
    (-1, None),  # under 0
)

# The five mutually-exclusive résumé-lifecycle buckets ADR-026 decision 5
# promises on the job status-breakdown surface.
STATUS_BREAKDOWN_FIELDS: tuple[str, ...] = (
    "uploaded",
    "parsing",
    "parsed",
    "failed",
    "withdrawn",
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _resume_out_kwargs(**over: object) -> dict[str, object]:
    """A complete, valid ``ResumeOut`` payload (every non-defaulted field set)."""
    base: dict[str, object] = {
        "id": uuid4(),
        "job_id": uuid4(),
        "original_filename": "jane.pdf",
        "mime_type": "application/pdf",
        "file_size_bytes": 1024,
        "sha256": "a" * 64,
        "candidate": CandidateInfo(name="Jane"),
        "candidate_email_hash": None,
        "parsed": None,
        "status": "parsed",
        "uploaded_by": "recruiter@sfu.ca",
        "uploaded_at": _TS,
        "parsed_at": None,
        "failure_reason": None,
        "consent_acknowledged": True,
    }
    base.update(over)
    return base


def _breakdown_kwargs(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "uploaded": 0,
        "parsing": 0,
        "parsed": 0,
        "failed": 0,
        "withdrawn": 0,
        "degraded": 0,
    }
    base.update(over)
    return base


# ── Public surface / re-export ───────────────────────────────────────────────


@pytest.mark.parametrize("name", RESUMES_PUBLIC)
def test_module_exposes_keep_name(name: str) -> None:
    assert hasattr(resumes_mod, name)


@pytest.mark.parametrize("name", RESUMES_PUBLIC)
def test_package_reexports_keep_name(name: str) -> None:
    assert hasattr(schemas_pkg, name)


def test_resumes_reexports_skill_from_jobs() -> None:
    """The import was retargeted to ``src.schemas.jobs`` — same object."""
    from src.schemas.jobs import Skill as JobsSkill

    assert resumes_mod.Skill is JobsSkill


# ── Happy-path validation ────────────────────────────────────────────────────


def test_resume_chunk_minimal_valid() -> None:
    chunk = ResumeChunk(id="c_1", text="hello")
    assert chunk.section == "other"  # default


def test_experience_minimal_valid() -> None:
    exp = Experience(company="Acme", title="Dev")
    assert exp.is_current is False


def test_candidate_info_all_optional() -> None:
    info = CandidateInfo()
    assert info.name is None


def test_cover_letter_parsed_defaults() -> None:
    cl = CoverLetterParsed()
    assert cl.raw_text == ""
    assert cl.themes == []


def test_resume_list_item_minimal_valid() -> None:
    item = ResumeListItem(
        id=uuid4(),
        original_filename="jane.pdf",
        status="parsed",
        uploaded_at=_TS,
        parsed_at=None,
    )
    assert item.has_cover_letter is False


def test_resume_upload_result_minimal_valid() -> None:
    res = ResumeUploadResult(original_filename="jane.pdf", outcome="accepted")
    assert res.warnings == []


def test_resume_delete_out_defaults_empty_warnings() -> None:
    out = ResumeDeleteOut(id=uuid4(), deleted=True)
    assert out.cleanup_warnings == []


def test_resume_core_minimal_valid() -> None:
    core = ResumeCore()
    assert core.total_years_experience == 0


def test_resume_out_minimal_valid() -> None:
    out = ResumeOut(**_resume_out_kwargs())
    assert out.candidate.name == "Jane"


def test_resume_parsed_roundtrips_faithfully() -> None:
    payload = {
        "candidate": {"name": "Jane", "email": "j@x.com"},
        "summary": "Experienced engineer",
        "total_years_experience": 5,
        "skills": [{"name": "Python", "years": 3, "last_used_year": 2023}],
        "experience": [
            {"company": "Acme", "title": "Dev", "bullets": [{"text": "did a thing"}]}
        ],
        "education": [{"degree": "BSc", "institution": "UBC", "year": 2015}],
        "chunks": [{"id": "c_1", "text": "hello world"}],
    }
    parsed = ResumeParsed.model_validate(payload)
    again = ResumeParsed.model_validate(parsed.model_dump())
    assert again.model_dump() == parsed.model_dump()


# ── _coerce_year (via the helper AND via both model fields) ───────────────────


@pytest.mark.parametrize("raw, expected", YEAR_CASES)
def test_coerce_year_helper(raw: object, expected: int | None) -> None:
    assert _coerce_year(raw) == expected


@pytest.mark.parametrize("raw, expected", YEAR_CASES)
def test_education_item_year_coercion(raw: object, expected: int | None) -> None:
    item = EducationItem(degree="BSc", institution="UBC", year=raw)
    assert item.year == expected


@pytest.mark.parametrize("raw, expected", YEAR_CASES)
def test_resume_skill_last_used_year_coercion(
    raw: object, expected: int | None
) -> None:
    skill = ResumeSkill(name="Python", last_used_year=raw)
    assert skill.last_used_year == expected


# ── ResumeSkill.categories (ROADMAP A2, Phase 3.3 skill-family classifier,
# slice 1) ──────────────────────────────────────────────────────────────


def test_resume_skill_categories_defaults_to_none() -> None:
    """Conservative default: absent input means ``categories`` is ``None``,
    not ``[]`` -- the same value a pre-feature parsed row (no key at all)
    reads back as, per ``extra="ignore"``'s forward-compatibility contract."""
    skill = ResumeSkill(name="python")
    assert skill.categories is None


def test_resume_skill_categories_accepts_a_list_of_strings() -> None:
    skill = ResumeSkill(name="mri methods", categories=["health_wellness"])
    assert skill.categories == ["health_wellness"]


def test_resume_skill_categories_accepts_two_families() -> None:
    skill = ResumeSkill(name="canvas", categories=["frontend", "edtech"])
    assert skill.categories == ["frontend", "edtech"]


def test_resume_skill_categories_rejects_more_than_the_per_skill_cap() -> None:
    """The classifier caps at 2 families per skill (ADR-044) --
    the schema itself enforces the same ceiling as a second line of
    defence, so a future classifier bug cannot silently inflate family
    credit past the reviewed cap."""
    with pytest.raises(ValidationError):
        ResumeSkill(name="x", categories=["a", "b", "c"])


def test_resume_skill_categories_old_row_missing_the_key_reads_back_none() -> None:
    """``extra="ignore"`` plus this default means a résumé parsed BEFORE
    this feature shipped (no ``categories`` key in the stored jsonb at all)
    reads back identically to a skill the classifier declined to answer for
    -- the strictly-additive property ADR-044 requires."""
    skill = ResumeSkill.model_validate({"name": "python", "years": 3})
    assert skill.categories is None


def test_resume_parsed_skill_categories_roundtrips_through_model_validate() -> None:
    parsed = ResumeParsed(
        skills=[ResumeSkill(name="mri methods", categories=["health_wellness"])]
    )
    again = ResumeParsed.model_validate(parsed.model_dump())
    assert again.skills[0].categories == ["health_wellness"]


# ── ResumeParsed._drop_invalid_rows (lossy per-row filter) ────────────────────


@pytest.mark.parametrize("key, good, bad", DROP_CASES)
def test_drop_invalid_rows_keeps_only_valid(
    key: str, good: dict[str, object], bad: dict[str, object]
) -> None:
    parsed = ResumeParsed.model_validate({key: [good, bad]})
    rows = getattr(parsed, key)
    assert len(rows) == 1


def test_drop_invalid_rows_passes_through_non_list_untouched() -> None:
    """A non-list ``skills`` is left untouched (not coerced to an empty list),
    so field validation then rejects it — proving the guard's ``continue``
    branch rather than a silent fix that would hide a malformed payload."""
    with pytest.raises(ValidationError):
        ResumeParsed.model_validate({"skills": {"name": "Python"}})


# ── ResumeSkillNames / ResumeSkillDetails coercers ───────────────────────────


def test_resume_skill_names_coerces_str_and_dict_rows() -> None:
    m = ResumeSkillNames.model_validate(
        {"skills": ["Python", {"name": "Go"}, 123, {"x": 1}, None]}
    )
    assert m.skills == ["Python", "Go"]


def test_resume_skill_names_caps_at_200() -> None:
    m = ResumeSkillNames.model_validate({"skills": [f"s{i}" for i in range(250)]})
    assert len(m.skills) == 200


def test_resume_skill_details_coerces_rows() -> None:
    m = ResumeSkillDetails.model_validate(
        {"skills": ["Python", {"name": "Go", "years": 3}, "", {"x": 1}, 5]}
    )
    assert [s.name for s in m.skills] == ["Python", "Go"]
    assert m.skills[1].years == 3


def test_resume_skill_details_caps_at_200() -> None:
    m = ResumeSkillDetails.model_validate({"skills": [f"s{i}" for i in range(250)]})
    assert len(m.skills) == 200


@pytest.mark.parametrize("raw, expected", YEARS_CASES)
def test_resume_skill_detail_years_coercion(raw: object, expected: int | None) -> None:
    detail = ResumeSkillDetail(name="Python", years=raw)
    assert detail.years == expected


# ── DEVIATION: ResumeOut.uploaded_by is a nullable str actor label ───────────


def test_resume_out_uploaded_by_accepts_a_plain_string() -> None:
    out = ResumeOut(**_resume_out_kwargs(uploaded_by="recruiter@sfu.ca"))
    assert out.uploaded_by == "recruiter@sfu.ca"  # not coerced to a UUID


def test_resume_out_uploaded_by_accepts_none() -> None:
    out = ResumeOut(**_resume_out_kwargs(uploaded_by=None))
    assert out.uploaded_by is None


def test_resume_out_uploaded_by_rejects_non_string() -> None:
    with pytest.raises(ValidationError):
        ResumeOut(**_resume_out_kwargs(uploaded_by=123))


# ── PII redaction boundary: candidate in, ciphertext out ─────────────────────


def test_resume_out_exposes_decrypted_candidate_view() -> None:
    assert "candidate" in ResumeOut.model_fields


@pytest.mark.parametrize("ciphertext", ["candidate_email", "blob_key"])
def test_resume_out_never_exposes_ciphertext(ciphertext: str) -> None:
    """The DTO is the redaction boundary — only the email *hash* is allowed."""
    assert ciphertext not in ResumeOut.model_fields


def test_resume_out_keeps_email_hash_not_ciphertext() -> None:
    assert "candidate_email_hash" in ResumeOut.model_fields


# ── extra="forbid" vs extra="ignore" ─────────────────────────────────────────


def test_resume_out_forbids_unknown_key() -> None:
    with pytest.raises(ValidationError):
        ResumeOut(**_resume_out_kwargs(bogus=1))


def test_resume_parsed_ignores_unknown_key() -> None:
    parsed = ResumeParsed.model_validate({"bogus": 1})
    assert "bogus" not in parsed.model_dump()


def test_resume_chunk_ignores_unknown_key() -> None:
    chunk = ResumeChunk.model_validate({"id": "c_1", "text": "hi", "bogus": 1})
    assert "bogus" not in chunk.model_dump()


# ── ADR-026 (FU-8): résumé withdrawal lifecycle — ResumeOut/ResumeListItem ──


def test_resume_out_has_withdrawn_at_and_withdrawal_reason_fields() -> None:
    assert "withdrawn_at" in ResumeOut.model_fields
    assert "withdrawal_reason" in ResumeOut.model_fields


def test_resume_out_withdrawal_fields_default_to_none() -> None:
    """A résumé that has never been withdrawn must not require the caller to
    supply these fields — they default to ``None``."""
    out = ResumeOut(**_resume_out_kwargs())
    assert out.withdrawn_at is None
    assert out.withdrawal_reason is None


def test_resume_out_carries_a_real_withdrawn_at_and_reason() -> None:
    out = ResumeOut(
        **_resume_out_kwargs(withdrawn_at=_TS, withdrawal_reason="took another offer")
    )
    assert out.withdrawn_at == _TS
    assert out.withdrawal_reason == "took another offer"


def test_resume_list_item_has_withdrawn_at_and_withdrawal_reason_fields() -> None:
    assert "withdrawn_at" in ResumeListItem.model_fields
    assert "withdrawal_reason" in ResumeListItem.model_fields


def test_resume_list_item_withdrawal_fields_default_to_none() -> None:
    item = ResumeListItem(
        id=uuid4(),
        original_filename="jane.pdf",
        status="parsed",
        uploaded_at=_TS,
        parsed_at=None,
    )
    assert item.withdrawn_at is None
    assert item.withdrawal_reason is None


def test_resume_list_item_carries_a_real_withdrawn_at() -> None:
    item = ResumeListItem(
        id=uuid4(),
        original_filename="jane.pdf",
        status="parsed",
        uploaded_at=_TS,
        parsed_at=None,
        withdrawn_at=_TS,
        withdrawal_reason="no longer interested",
    )
    assert item.withdrawn_at == _TS
    assert item.withdrawal_reason == "no longer interested"


# ── ADR-026 (FU-8): WithdrawRequest ─────────────────────────────────────────


def test_withdraw_request_reason_defaults_to_none() -> None:
    req = WithdrawRequest()
    assert req.reason is None


def test_withdraw_request_accepts_a_reason_up_to_500_chars() -> None:
    req = WithdrawRequest(reason="x" * 500)
    assert req.reason is not None
    assert len(req.reason) == 500


def test_withdraw_request_rejects_a_reason_over_500_chars() -> None:
    with pytest.raises(ValidationError):
        WithdrawRequest(reason="x" * 501)


def test_withdraw_request_forbids_unknown_key() -> None:
    with pytest.raises(ValidationError):
        WithdrawRequest(bogus=1)


def test_withdraw_request_accepts_none_reason_explicitly() -> None:
    req = WithdrawRequest(reason=None)
    assert req.reason is None


# ── ADR-026 (FU-8) decision 5: ResumeStatusBreakdown ────────────────────────


def test_resume_status_breakdown_accepts_all_five_buckets() -> None:
    b = ResumeStatusBreakdown(
        **_breakdown_kwargs(uploaded=1, parsing=2, parsed=3, failed=4, withdrawn=5)
    )
    assert b.uploaded == 1
    assert b.parsing == 2
    assert b.parsed == 3
    assert b.failed == 4
    assert b.withdrawn == 5


def test_resume_status_breakdown_zero_job_shape_is_all_zeros() -> None:
    """A job with no résumés at all must still yield the five-bucket shape,
    every count zero — never an omitted field."""
    b = ResumeStatusBreakdown(**_breakdown_kwargs())
    for field in STATUS_BREAKDOWN_FIELDS:
        assert getattr(b, field) == 0
    assert b.degraded == 0  # ADR-030: degraded is a sub-count, zero too


def test_resume_status_breakdown_forbids_unknown_key() -> None:
    with pytest.raises(ValidationError):
        ResumeStatusBreakdown(**_breakdown_kwargs(bogus=1))


@pytest.mark.parametrize("field", STATUS_BREAKDOWN_FIELDS)
def test_resume_status_breakdown_requires_every_bucket(field: str) -> None:
    """No default on any individual bucket — a caller must supply all five,
    so a service-layer bug that forgets one bucket 500s loudly at
    construction rather than silently reporting zero."""
    kwargs = _breakdown_kwargs()
    del kwargs[field]
    with pytest.raises(ValidationError):
        ResumeStatusBreakdown(**kwargs)


@pytest.mark.parametrize("field", STATUS_BREAKDOWN_FIELDS)
def test_resume_status_breakdown_rejects_a_negative_count(field: str) -> None:
    kwargs = _breakdown_kwargs(**{field: -1})
    with pytest.raises(ValidationError):
        ResumeStatusBreakdown(**kwargs)


@pytest.mark.parametrize("field", STATUS_BREAKDOWN_FIELDS)
def test_resume_status_breakdown_rejects_a_non_numeric_string_count(
    field: str,
) -> None:
    kwargs = _breakdown_kwargs(**{field: "not-a-number"})
    with pytest.raises(ValidationError):
        ResumeStatusBreakdown(**kwargs)


# ── FU-7 §4 / ADR-030: degraded-parse visibility ────────────────────────────
#
# When skills extraction falls back to the deterministic keyword scan (the
# `resume_skills_v2` `LLMOutputInvalidError` catch in
# `src/worker/resume_tasks.py::_extract_skills_merged`), the résumé must be
# flagged `degraded` end to end: persisted on `ResumeParsed` (rides the
# existing `resumes.parsed` jsonb — no DDL change), surfaced on the list read
# (`ResumeListItem.degraded`), and counted as a SUB-count of `parsed` on the
# per-job breakdown (`ResumeStatusBreakdown.degraded`). None of these fields
# exist yet — RED half of the TDD cycle.


def test_resume_parsed_degraded_defaults_false_and_reason_none() -> None:
    parsed = ResumeParsed()
    assert parsed.degraded is False
    assert parsed.degradation_reason is None


def test_resume_parsed_degraded_roundtrips_through_model_validate() -> None:
    payload = {
        "candidate": {"name": "Jane"},
        "summary": "Backend engineer",
        "degraded": True,
        "degradation_reason": (
            "skills extraction failed (AI); using keyword-scan fallback"
        ),
    }
    parsed = ResumeParsed.model_validate(payload)
    assert parsed.degraded is True
    assert parsed.degradation_reason == (
        "skills extraction failed (AI); using keyword-scan fallback"
    )

    again = ResumeParsed.model_validate(parsed.model_dump())
    assert again.degraded is True
    assert again.degradation_reason == parsed.degradation_reason
    assert again.model_dump() == parsed.model_dump()


def test_resume_parsed_degradation_reason_rejects_over_200_chars() -> None:
    with pytest.raises(ValidationError):
        ResumeParsed(degraded=True, degradation_reason="x" * 201)


def test_resume_parsed_degradation_reason_accepts_exactly_200_chars() -> None:
    parsed = ResumeParsed(degraded=True, degradation_reason="x" * 200)
    assert parsed.degradation_reason is not None
    assert len(parsed.degradation_reason) == 200


def test_resume_parsed_old_row_missing_degraded_keys_defaults_false() -> None:
    """`extra="ignore"` + defaults means a row persisted BEFORE this feature
    (no `degraded`/`degradation_reason` keys in its jsonb at all) reads back
    as a clean, non-degraded parse rather than raising."""
    old_row_payload = {
        "candidate": {"name": "Jane"},
        "summary": "Backend engineer",
        "skills": [{"name": "Python"}],
    }
    parsed = ResumeParsed.model_validate(old_row_payload)
    assert parsed.degraded is False
    assert parsed.degradation_reason is None


def test_resume_out_parsed_degraded_flows_through_unchanged() -> None:
    """``ResumeOut.parsed`` is a ``ResumeParsed`` — no separate redaction or
    remapping happens to `degraded`/`degradation_reason`; it is NOT PII."""
    parsed = ResumeParsed(degraded=True, degradation_reason="fallback used")
    out = ResumeOut(**_resume_out_kwargs(parsed=parsed))
    assert out.parsed is not None
    assert out.parsed.degraded is True
    assert out.parsed.degradation_reason == "fallback used"


# ── ResumeListItem.degraded ──────────────────────────────────────────────


def test_resume_list_item_degraded_defaults_false() -> None:
    item = ResumeListItem(
        id=uuid4(),
        original_filename="jane.pdf",
        status="parsed",
        uploaded_at=_TS,
        parsed_at=None,
    )
    assert item.degraded is False


def test_resume_list_item_carries_degraded_true() -> None:
    item = ResumeListItem(
        id=uuid4(),
        original_filename="jane.pdf",
        status="parsed",
        uploaded_at=_TS,
        parsed_at=None,
        degraded=True,
    )
    assert item.degraded is True


# ── ResumeStatusBreakdown.degraded (ADR-030) ────────────────────────────────
#
# Unlike the five mutually-exclusive lifecycle buckets, `degraded` is a
# SUB-count of `parsed` (degraded ⊆ parsed): a job with 7 parsed résumés, 2
# of them degraded, reports `parsed=7, degraded=2` — `parsed` never shrinks
# to exclude the degraded ones.


def test_resume_status_breakdown_accepts_a_degraded_count() -> None:
    b = ResumeStatusBreakdown(**_breakdown_kwargs(parsed=7, degraded=2))
    assert b.parsed == 7
    assert b.degraded == 2


def test_resume_status_breakdown_requires_the_degraded_field() -> None:
    """No default — a service-layer bug that forgets the bucket 500s loudly
    at construction rather than silently reporting zero, exactly like the
    five lifecycle buckets."""
    kwargs = _breakdown_kwargs()
    del kwargs["degraded"]
    with pytest.raises(ValidationError):
        ResumeStatusBreakdown(**kwargs)


def test_resume_status_breakdown_rejects_a_negative_degraded_count() -> None:
    with pytest.raises(ValidationError):
        ResumeStatusBreakdown(**_breakdown_kwargs(degraded=-1))


def test_resume_status_breakdown_rejects_a_non_numeric_string_degraded_count() -> None:
    with pytest.raises(ValidationError):
        ResumeStatusBreakdown(**_breakdown_kwargs(degraded="not-a-number"))


def test_resume_status_breakdown_degraded_does_not_reduce_parsed() -> None:
    """The sub-count relationship: `degraded` rides ALONGSIDE `parsed`, never
    in place of it — a résumé that is both `status='parsed'` and
    `degraded=True` is counted in BOTH numbers."""
    b = ResumeStatusBreakdown(
        **_breakdown_kwargs(uploaded=0, parsing=0, parsed=10, failed=0, degraded=10)
    )
    assert b.parsed == 10
    assert b.degraded == 10
