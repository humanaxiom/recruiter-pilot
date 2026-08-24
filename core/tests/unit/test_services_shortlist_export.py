"""Unit tests for ``src.services.shortlist_service.export_rows`` + the CSV /
JSON export formatters (Phase 5). All I/O is mocked; the real Postgres round
trip (incl. the "decrypt needs ``set_pii_key`` first" fail-loud proof) lives
in ``tests/integration/test_shortlist_read_export_pg.py``.

API this file drives the coder toward (mirrors hris's
``routes/shortlist.py`` export helpers, ported into the SERVICE layer since
this project has no Phase-6 route yet and Phase 5 is explicitly scoped to
ship ``export_rows``):

* ``export_rows(conn, *, job_id, reveal) -> list[dict[str, Any]]`` — one
  flat dict per candidate, already reveal-applied (identity pseudonymized +
  evidence redacted when ``reveal=False``). ``score_breakdown`` / ``evidence``
  / ``pipeline_meta`` are already-parsed ``dict``s in the returned rows (not
  JSON-encoded strings) — the caller MUST have already run
  ``pii_service.set_pii_key(conn)`` inside an open transaction (this function
  does not call it itself: the raw-SQL ``pgp_sym_decrypt`` fails loud
  otherwise — see the integration test).
* ``shortlist_csv(rows) -> str`` — the one-row-per-candidate working file.
* ``shortlist_evidence_csv(rows) -> str`` — one row per (candidate,
  requirement); zero-requirement candidates still get a placeholder row.
* ``shortlist_json(rows) -> str`` — full nested JSON, Decimal/datetime
  normalised.

None of these exist yet — RED half of the TDD cycle; every test below fails,
either at collection or at the first call.

THE LOAD-BEARING TEST IN THIS FILE:
``test_shortlist_csv_header_excludes_cut_review_columns`` asserts the CSV
header as a POSITIVE set-equality, not a substring check, so a reintroduced
``current_stage``/``current_decision``/... column (the cut review workflow)
fails loud even if it's added alongside other legitimate new columns.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from src.schemas.matching import ScoreBreakdown


class _Row(dict[str, Any]):
    def __getitem__(self, key: str) -> Any:
        return dict.get(self, key)


def _breakdown_dict() -> dict[str, Any]:
    return ScoreBreakdown(
        skill=0.7,
        experience=0.6,
        education=0.5,
        seniority=0.5,
        vector=0.4,
        structured=0.6,
    ).model_dump()


# ---------------- export_rows: raw SQL-shaped rows (pre-processing) --------


def _raw_export_row(
    *,
    rank: int = 1,
    resume_id: UUID | None = None,
    score_final: float = 0.9,
    score_breakdown: dict[str, Any] | None = None,
    evidence: dict[str, Any] | None = None,
    pipeline_meta: dict[str, Any] | None = None,
    generated_at: dt.datetime | None = None,
    job_title: str = "Senior Engineer",
    job_department: str | None = "Platform",
    original_filename: str = "resume.pdf",
    candidate_name: str | None = None,
    candidate_email: str | None = None,
    candidate_phone: str | None = None,
    candidate_parsed: dict[str, Any] | None = None,
) -> _Row:
    ev = evidence if evidence is not None else {"requirements": []}
    meta = pipeline_meta or {"git_sha": None, "weights": {}}
    return _Row(
        {
            "rank": rank,
            "resume_id": resume_id or uuid4(),
            "score_final": score_final,
            "score_breakdown": json.dumps(score_breakdown or _breakdown_dict()),
            "evidence": json.dumps(ev),
            "pipeline_meta": json.dumps(meta),
            "generated_at": generated_at or dt.datetime(2026, 7, 15, tzinfo=dt.UTC),
            "job_title": job_title,
            "job_department": job_department,
            "original_filename": original_filename,
            "candidate_name": candidate_name,
            "candidate_email": candidate_email,
            "candidate_phone": candidate_phone,
            "candidate_parsed": (
                json.dumps(candidate_parsed) if candidate_parsed is not None else None
            ),
        }
    )


def _mock_export_conn(*, rows: list[_Row]) -> MagicMock:
    conn = MagicMock(name="conn")
    conn.fetch = AsyncMock(return_value=rows)
    return conn


# ---------------- formatter helpers: already-processed rows ----------------


def _export_row(
    *,
    rank: int = 1,
    resume_id: UUID | None = None,
    score_final: float | Decimal = 0.9,
    score_breakdown: dict[str, Any] | None = None,
    evidence: dict[str, Any] | None = None,
    pipeline_meta: dict[str, Any] | None = None,
    generated_at: dt.datetime | None = None,
    job_title: str = "Senior Engineer",
    job_department: str | None = "Platform",
    original_filename: str = "resume.pdf",
    candidate_name: str | None = "Candidate A",
    candidate_email: str | None = None,
    candidate_phone: str | None = None,
) -> dict[str, Any]:
    return {
        "rank": rank,
        "resume_id": resume_id or uuid4(),
        "score_final": score_final,
        "score_breakdown": score_breakdown or _breakdown_dict(),
        "evidence": evidence if evidence is not None else {"requirements": []},
        "pipeline_meta": pipeline_meta or {"git_sha": None, "weights": {}},
        "generated_at": generated_at or dt.datetime(2026, 7, 15, tzinfo=dt.UTC),
        "job_title": job_title,
        "job_department": job_department,
        "original_filename": original_filename,
        "candidate_name": candidate_name,
        "candidate_email": candidate_email,
        "candidate_phone": candidate_phone,
    }


# ── export_rows: reveal=True / reveal=False ─────────────────────────────


@pytest.mark.asyncio
async def test_export_rows_reveal_true_shows_real_identity() -> None:
    from src.services.shortlist_service import export_rows

    job_id = uuid4()
    raw_row = _raw_export_row(
        candidate_name="Jane Smith",
        candidate_email="jane.smith@example.test",
        candidate_phone="604-555-0192",
        evidence={
            "requirements": [
                {
                    "requirement": "Python",
                    "status": "met",
                    "evidence": "Jane Smith did great work.",
                    "evidence_chunk_ids": [],
                    "confidence": 0.9,
                }
            ]
        },
    )
    conn = _mock_export_conn(rows=[raw_row])

    rows = await export_rows(conn, job_id=job_id, reveal=True)

    assert rows[0]["candidate_name"] == "Jane Smith"
    assert rows[0]["candidate_email"] == "jane.smith@example.test"
    assert rows[0]["candidate_phone"] == "604-555-0192"
    assert "Jane Smith" in rows[0]["evidence"]["requirements"][0]["evidence"]


@pytest.mark.asyncio
async def test_export_rows_reveal_false_pseudonymizes_and_redacts() -> None:
    from src.services.shortlist_service import export_rows

    job_id = uuid4()
    raw_row = _raw_export_row(
        rank=1,
        candidate_name="Jane Smith",
        candidate_email="jane.smith@example.test",
        candidate_phone="604-555-0192",
        candidate_parsed={
            "candidate": {"name": "Jane Smith"},
            "experience": [{"company": "Acme Corp", "title": "Eng", "bullets": []}],
            "education": [],
        },
        evidence={
            "requirements": [
                {
                    "requirement": "Python",
                    "status": "met",
                    "evidence": "Jane Smith did great work at Acme Corp.",
                    "evidence_chunk_ids": [],
                    "confidence": 0.9,
                }
            ],
            "cover_letter_evidence": [
                {
                    "theme": "motivation",
                    "evidence": "I, Jane Smith, care deeply.",
                    "evidence_chunk_ids": [],
                    "confidence": 0.7,
                }
            ],
            "overall_motivation": "Jane Smith is motivated.",
        },
    )
    conn = _mock_export_conn(rows=[raw_row])

    rows = await export_rows(conn, job_id=job_id, reveal=False)

    row = rows[0]
    assert row["candidate_name"] == "Candidate A"
    assert row["candidate_email"] in (None, "")
    assert row["candidate_phone"] in (None, "")
    ev = row["evidence"]
    assert "Jane Smith" not in ev["requirements"][0]["evidence"]
    assert "Acme Corp" not in ev["requirements"][0]["evidence"]
    assert "Jane Smith" not in ev["cover_letter_evidence"][0]["evidence"]
    assert "Jane Smith" not in ev["overall_motivation"]


@pytest.mark.asyncio
async def test_export_rows_reveal_false_redacts_identifying_filename() -> None:
    """The résumé filename (e.g. ``Jane_Smith_Resume.pdf``) leaks identity
    verbatim through the blind export surface — mask it to ``resume<ext>``."""
    from src.services.shortlist_service import export_rows

    job_id = uuid4()
    raw_row = _raw_export_row(
        rank=1,
        candidate_name="Jane Smith",
        original_filename="Jane_Smith_Resume.pdf",
    )
    conn = _mock_export_conn(rows=[raw_row])

    rows = await export_rows(conn, job_id=job_id, reveal=False)

    assert rows[0]["original_filename"] == "resume.pdf"
    assert "Jane_Smith" not in rows[0]["original_filename"]


@pytest.mark.asyncio
async def test_export_rows_reveal_true_keeps_real_filename() -> None:
    from src.services.shortlist_service import export_rows

    job_id = uuid4()
    raw_row = _raw_export_row(
        rank=1,
        candidate_name="Jane Smith",
        original_filename="Jane_Smith_Resume.pdf",
    )
    conn = _mock_export_conn(rows=[raw_row])

    rows = await export_rows(conn, job_id=job_id, reveal=True)

    assert rows[0]["original_filename"] == "Jane_Smith_Resume.pdf"


@pytest.mark.asyncio
async def test_shortlist_csv_resume_file_redacted_under_blind_export() -> None:
    """End-to-end: a blind export feeds ``shortlist_csv`` — the ``resume_file``
    column must carry the placeholder, never the identifying filename."""
    from src.services.shortlist_service import export_rows, shortlist_csv

    job_id = uuid4()
    raw_row = _raw_export_row(
        rank=1,
        candidate_name="Jane Smith",
        original_filename="Jane_Smith_Resume.pdf",
    )
    conn = _mock_export_conn(rows=[raw_row])

    rows = await export_rows(conn, job_id=job_id, reveal=False)
    csv_text = shortlist_csv(rows)
    data = next(csv.DictReader(io.StringIO(csv_text)))

    assert data["resume_file"] == "resume.pdf"
    assert "Jane_Smith" not in data["resume_file"]


@pytest.mark.asyncio
async def test_export_rows_reveal_false_pseudonym_keyed_on_rank() -> None:
    from src.services.shortlist_service import export_rows

    job_id = uuid4()
    row1 = _raw_export_row(rank=1, candidate_name="Jane Smith")
    row2 = _raw_export_row(rank=2, candidate_name="Bob Lee")
    conn = _mock_export_conn(rows=[row1, row2])

    rows = await export_rows(conn, job_id=job_id, reveal=False)

    assert rows[0]["candidate_name"] == "Candidate A"
    assert rows[1]["candidate_name"] == "Candidate B"


@pytest.mark.asyncio
async def test_black_box_scan_no_pii_in_export_row_reveal_false() -> None:
    from src.services.shortlist_service import export_rows

    job_id = uuid4()
    real_name = "Zzyzxqrst Wibblesworth"
    real_email = "zzyzxqrst.wibblesworth@example.test"
    real_phone = "778-555-0199"
    identity_filename = "Zzyzxqrst_Wibblesworth_CV.pdf"
    raw_row = _raw_export_row(
        rank=1,
        candidate_name=real_name,
        candidate_email=real_email,
        candidate_phone=real_phone,
        original_filename=identity_filename,
        candidate_parsed={
            "candidate": {"name": real_name},
            "experience": [],
            "education": [],
        },
        evidence={
            "requirements": [
                {
                    "requirement": "X",
                    "status": "met",
                    "evidence": f"{real_name} built it.",
                    "evidence_chunk_ids": [],
                    "confidence": 0.9,
                }
            ],
            "overall_summary": f"{real_name} is great.",
        },
    )
    conn = _mock_export_conn(rows=[raw_row])

    rows = await export_rows(conn, job_id=job_id, reveal=False)
    dumped = json.dumps(rows, default=str)

    assert real_name not in dumped
    assert real_email not in dumped
    assert real_phone not in dumped
    assert identity_filename not in dumped


# ── shortlist_csv: exact header set (the load-bearing test) ────────────────


def test_shortlist_csv_header_excludes_cut_review_columns() -> None:
    from src.services.shortlist_service import shortlist_csv

    csv_text = shortlist_csv([_export_row()])
    reader = csv.reader(io.StringIO(csv_text))
    fieldnames = set(next(reader))

    forbidden = {
        "current_stage",
        "current_decision",
        "decision_actor",
        "decision_reason",
        "decision_reason_code",
        "decision_at",
    }
    leaked = fieldnames & forbidden
    assert not leaked, f"the cut review workflow reappeared in the header: {leaked}"

    expected = {
        "rank",
        "candidate_name",
        "candidate_email",
        "candidate_phone",
        "resume_file",
        "job_title",
        "job_department",
        "score_final",
        "must_have_missing",
        "skills_missing",
        "skills_matched_count",
        "skills_total",
        "must_have_count",
        "score_structured",
        "score_evidence_completeness",
        "score_motivation",
        "skill",
        "experience",
        "education",
        "seniority",
        "vector",
        "requirement_count",
        "requirements_met",
        "requirements_partial",
        "requirements_missing",
        "evidence_summary",
        "skills_matched",
        "generated_at",
        "pipeline_version",
        "resume_id",
    }
    assert fieldnames == expected


def test_shortlist_csv_has_exactly_one_data_row_per_input_row() -> None:
    from src.services.shortlist_service import shortlist_csv

    rows = [_export_row(rank=1), _export_row(rank=2)]
    csv_text = shortlist_csv(rows)
    reader = csv.DictReader(io.StringIO(csv_text))
    data = list(reader)
    assert len(data) == 2
    assert data[0]["rank"] == "1"
    assert data[1]["rank"] == "2"


def test_shortlist_csv_empty_rows_yields_header_only() -> None:
    from src.services.shortlist_service import shortlist_csv

    csv_text = shortlist_csv([])
    reader = csv.reader(io.StringIO(csv_text))
    lines = list(reader)
    assert len(lines) == 1  # header only, no trailing blank data row


# ── shortlist_csv: pipeline_version = git_sha[:12] (or empty) ──────────────


def test_shortlist_csv_pipeline_version_is_git_sha_first_12_chars() -> None:
    from src.services.shortlist_service import shortlist_csv

    row = _export_row(pipeline_meta={"git_sha": "abcdef0123456789", "weights": {}})
    csv_text = shortlist_csv([row])
    reader = csv.DictReader(io.StringIO(csv_text))
    data = next(reader)
    assert data["pipeline_version"] == "abcdef012345"


def test_shortlist_csv_pipeline_version_empty_when_git_sha_none() -> None:
    from src.services.shortlist_service import shortlist_csv

    row = _export_row(pipeline_meta={"git_sha": None, "weights": {}})
    csv_text = shortlist_csv([row])
    reader = csv.DictReader(io.StringIO(csv_text))
    data = next(reader)
    assert data["pipeline_version"] == ""


def test_shortlist_csv_pipeline_version_empty_when_git_sha_key_missing() -> None:
    from src.services.shortlist_service import shortlist_csv

    row = _export_row(pipeline_meta={"weights": {}})
    csv_text = shortlist_csv([row])
    reader = csv.DictReader(io.StringIO(csv_text))
    data = next(reader)
    assert data["pipeline_version"] == ""


# ── shortlist_evidence_csv: one row per (candidate, requirement) ───────────


def test_shortlist_evidence_csv_one_row_per_candidate_requirement() -> None:
    from src.services.shortlist_service import shortlist_evidence_csv

    row_with_reqs = _export_row(
        rank=1,
        candidate_name="Candidate A",
        evidence={
            "requirements": [
                {
                    "requirement": "Python",
                    "status": "met",
                    "evidence": "quote 1",
                    "evidence_chunk_ids": ["c_001"],
                    "confidence": 0.9,
                },
                {
                    "requirement": "SQL",
                    "status": "partial",
                    "evidence": "quote 2",
                    "evidence_chunk_ids": [],
                    "confidence": 0.5,
                },
            ]
        },
    )
    row_no_reqs = _export_row(
        rank=2, candidate_name="Candidate B", evidence={"requirements": []}
    )

    csv_text = shortlist_evidence_csv([row_with_reqs, row_no_reqs])
    reader = csv.DictReader(io.StringIO(csv_text))
    data_rows = list(reader)

    assert len(data_rows) == 3  # 2 requirement rows + 1 placeholder row
    a_rows = [r for r in data_rows if r["candidate_name"] == "Candidate A"]
    assert len(a_rows) == 2
    b_rows = [r for r in data_rows if r["candidate_name"] == "Candidate B"]
    assert len(b_rows) == 1
    assert b_rows[0]["requirement"] == "(no evidence generated)"


def test_shortlist_evidence_csv_header_fields() -> None:
    from src.services.shortlist_service import shortlist_evidence_csv

    csv_text = shortlist_evidence_csv([_export_row()])
    reader = csv.reader(io.StringIO(csv_text))
    fieldnames = set(next(reader))
    assert fieldnames == {
        "rank",
        "candidate_name",
        "resume_file",
        "job_title",
        "requirement",
        "status",
        "confidence",
        "quote",
        "evidence_chunk_ids",
        # FU-2: the resolved source text behind the cited chunk ids. The ids
        # column is kept alongside it for traceability.
        "evidence_context",
    }


def test_shortlist_evidence_csv_quote_matches_evidence_text() -> None:
    from src.services.shortlist_service import shortlist_evidence_csv

    row = _export_row(
        evidence={
            "requirements": [
                {
                    "requirement": "Python",
                    "status": "met",
                    "evidence": "a verbatim quote",
                    "evidence_chunk_ids": ["c_001"],
                    "confidence": 0.9,
                }
            ]
        }
    )
    csv_text = shortlist_evidence_csv([row])
    reader = csv.DictReader(io.StringIO(csv_text))
    data = next(reader)
    assert data["quote"] == "a verbatim quote"
    assert data["evidence_chunk_ids"] == "c_001"


# ── shortlist_json: nested objects stay nested; types normalise ────────────


def test_shortlist_json_normalises_decimal_and_datetime() -> None:
    from src.services.shortlist_service import shortlist_json

    row = _export_row(
        score_final=Decimal("0.6420"),
        generated_at=dt.datetime(2026, 7, 15, 12, 30, tzinfo=dt.UTC),
    )
    json_text = shortlist_json([row])
    parsed = json.loads(json_text)

    assert isinstance(parsed, list)
    assert isinstance(parsed[0]["score_breakdown"], dict)
    assert isinstance(parsed[0]["evidence"], dict)
    assert isinstance(parsed[0]["score_final"], float)
    assert parsed[0]["score_final"] == pytest.approx(0.642)
    assert isinstance(parsed[0]["generated_at"], str)
    # must be a real ISO-8601 string, not just any string
    dt.datetime.fromisoformat(parsed[0]["generated_at"])


def test_shortlist_json_preserves_requirement_list_structure() -> None:
    from src.services.shortlist_service import shortlist_json

    row = _export_row(
        evidence={
            "requirements": [
                {
                    "requirement": "Python",
                    "status": "met",
                    "evidence": "quote",
                    "evidence_chunk_ids": ["c_001"],
                    "confidence": 0.9,
                }
            ]
        }
    )
    parsed = json.loads(shortlist_json([row]))
    reqs = parsed[0]["evidence"]["requirements"]
    assert isinstance(reqs, list)
    assert reqs[0]["requirement"] == "Python"


def test_shortlist_json_empty_rows_yields_empty_json_array() -> None:
    from src.services.shortlist_service import shortlist_json

    assert json.loads(shortlist_json([])) == []


# ── CSV formula injection (merge-blocking security gate, finding S3) ───────
#
# Excel/Sheets EXECUTE a cell whose first character is one of = + - @ TAB CR
# as a formula on open (``=cmd|'/c calc'!A1`` is the classic RCE-via-DDE
# payload). Neither writer neutralized anything. The exposure is pre-existing
# via candidate_name / evidence_summary / quote / requirement / resume_file,
# and the display-name branch WIDENED it: skills_matched / skills_missing /
# must_have_missing moved from a closed set (curated vocab, or an opaque
# ``h:<32 hex>``) to arbitrary JD text, and ``reject_reason_for_skill_name``
# only screens length / token count / email / phone SHAPE — a skill literally
# named ``=cmd|'/c calc'!A1`` passes it and reaches the export.
#
# The fix is file-wide (one helper applied to EVERY string cell in both
# writers), not per-column: special-casing the skills columns would leave the
# identical bug on the five other text fields.
#
# NUL is deliberately NOT in this list: a real 0x00 byte in a source file
# breaks pytest collection and git. TAB/CR are built with ``chr()`` for the
# same reason (see the repo memory note on NUL propagation).
_DANGEROUS_LEADING = ["=", "+", "-", "@", chr(9), chr(13)]


def _contribution(name: str, *, reason: str | None = None) -> dict[str, Any]:
    return {
        "skill": name,
        "score": 0.0 if reason == "missing" else 1.0,
        "years": None,
        "recency": None,
        "ontology_weight": 0.0 if reason == "missing" else 1.0,
        "is_must_have": True,
        "reason": reason,
    }


@pytest.mark.parametrize("lead", _DANGEROUS_LEADING)
def test_shortlist_csv_neutralises_formula_injection_in_text_columns(
    lead: str,
) -> None:
    from src.services.shortlist_service import shortlist_csv

    payload = f"{lead}cmd|'/c calc'!A1"
    breakdown = _breakdown_dict()
    breakdown["skill_contributions"] = [
        _contribution(payload),
        _contribution(payload, reason="missing"),
    ]
    row = _export_row(
        candidate_name=payload,
        candidate_email=payload,
        candidate_phone=payload,
        original_filename=payload,
        job_title=payload,
        job_department=payload,
        score_breakdown=breakdown,
        evidence={
            "requirements": [
                {
                    "requirement": payload,
                    "status": "met",
                    "evidence": payload,
                    "evidence_chunk_ids": [],
                    "confidence": 0.9,
                }
            ],
            "overall_summary": payload,
        },
    )

    data = next(csv.DictReader(io.StringIO(shortlist_csv([row]))))

    for column in (
        "candidate_name",
        "candidate_email",
        "candidate_phone",
        "resume_file",
        "job_title",
        "job_department",
        "evidence_summary",
        "skills_matched",
        "skills_missing",
        "must_have_missing",
    ):
        assert data[column].startswith("'"), (
            f"shortlist_csv column {column!r} passed a formula-injection "
            f"payload through unescaped: {data[column]!r} (leading "
            f"{lead!r}) -- security finding S3"
        )
        assert payload in data[column], (
            f"column {column!r} lost the original text instead of merely "
            f"prefixing it: {data[column]!r}"
        )


@pytest.mark.parametrize("lead", _DANGEROUS_LEADING)
def test_shortlist_evidence_csv_neutralises_formula_injection(lead: str) -> None:
    from src.services.shortlist_service import shortlist_evidence_csv

    payload = f"{lead}cmd|'/c calc'!A1"
    row = _export_row(
        candidate_name=payload,
        original_filename=payload,
        job_title=payload,
        evidence={
            "requirements": [
                {
                    "requirement": payload,
                    "status": "met",
                    "evidence": payload,
                    "evidence_chunk_ids": [payload],
                    "confidence": 0.9,
                    "source_context": payload,
                }
            ]
        },
    )

    data = next(csv.DictReader(io.StringIO(shortlist_evidence_csv([row]))))

    for column in (
        "candidate_name",
        "resume_file",
        "job_title",
        "requirement",
        "quote",
        "evidence_chunk_ids",
        "evidence_context",
    ):
        assert data[column].startswith("'"), (
            f"shortlist_evidence_csv column {column!r} passed a "
            f"formula-injection payload through unescaped: {data[column]!r} "
            f"(leading {lead!r}) -- security finding S3"
        )


@pytest.mark.parametrize("lead", _DANGEROUS_LEADING)
def test_shortlist_evidence_csv_neutralises_placeholder_row_columns(
    lead: str,
) -> None:
    """The zero-requirement candidate still gets a row — its identity cells
    come from the same ``base`` dict and must be neutralised too."""
    from src.services.shortlist_service import shortlist_evidence_csv

    payload = f'{lead}HYPERLINK("http://evil.test")'
    row = _export_row(
        candidate_name=payload,
        original_filename=payload,
        job_title=payload,
        evidence={"requirements": []},
    )

    data = next(csv.DictReader(io.StringIO(shortlist_evidence_csv([row]))))

    assert data["requirement"] == "(no evidence generated)"
    for column in ("candidate_name", "resume_file", "job_title"):
        assert data[column].startswith("'"), (
            f"placeholder row leaked an unescaped payload in {column!r}: "
            f"{data[column]!r} -- security finding S3"
        )


def test_shortlist_csv_does_not_mangle_legitimate_values() -> None:
    """The neutralisation must be surgical: only a DANGEROUS leading
    character gets the apostrophe. Ordinary skill names, filenames and every
    NUMERIC cell (including a negative number, whose ``-`` would be dangerous
    if it were a string) must round-trip byte-identically."""
    from src.services.shortlist_service import shortlist_csv

    breakdown = _breakdown_dict()
    breakdown["skill_contributions"] = [
        _contribution("Python"),
        _contribution("C++"),
        _contribution("Distributed Systems Design", reason="missing"),
    ]
    row = _export_row(
        candidate_name="Jane Smith",
        original_filename="resume.pdf",
        job_title="Senior Engineer",
        job_department="Platform",
        score_final=-0.5,  # numeric: not a string cell, must NOT be prefixed
        score_breakdown=breakdown,
    )

    data = next(csv.DictReader(io.StringIO(shortlist_csv([row]))))

    assert data["candidate_name"] == "Jane Smith"
    assert data["resume_file"] == "resume.pdf"
    assert data["job_title"] == "Senior Engineer"
    assert data["job_department"] == "Platform"
    assert data["skills_matched"] == "Python; C++"
    assert data["skills_missing"] == "Distributed Systems Design"
    assert (
        data["score_final"] == "-0.5"
    ), "a numeric cell was mangled -- only STRING cells may be prefixed"


def test_shortlist_csv_empty_string_cells_are_untouched() -> None:
    from src.services.shortlist_service import shortlist_csv

    row = _export_row(candidate_name=None, candidate_email=None, candidate_phone=None)

    data = next(csv.DictReader(io.StringIO(shortlist_csv([row]))))

    assert data["candidate_name"] == ""
    assert data["candidate_email"] == ""
    assert data["candidate_phone"] == ""
