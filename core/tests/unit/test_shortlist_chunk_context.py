"""FU-2 — expand evidence chunk ids to their actual source text (redaction-aware).

Three surfaces, all proven here with mocked I/O:

1. ``_resolve_chunk_context`` — a pure helper that maps ``c_NNN`` + ``cl_NNN``
   ids to their chunk text from ``resumes.parsed`` and concatenates them.
2. Export path — ``export_rows`` resolves each requirement's chunk ids to source
   text (redaction consistent with the row's ``reveal`` flag) and
   ``shortlist_evidence_csv`` surfaces it in a new ``evidence_context`` column.
3. Read / UI path — ``RequirementEvidence.source_context`` is populated
   (redacted, because the read path is blind) by ``list_for_job`` and rendered
   in ``shortlist_cards.html``.

REDACTION-CRITICAL: résumé chunk text is RAW content that can carry the
candidate's real name/email/phone. Under blind review / anonymized export the
resolved text MUST be redacted exactly like the evidence quote already is.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from src.schemas.matching import RequirementEvidence, ScoreBreakdown


class _Row(dict[str, Any]):
    def __getitem__(self, key: str) -> Any:
        return dict.get(self, key)


def _acm(return_value: Any = None) -> MagicMock:
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=return_value)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _breakdown_dict() -> dict[str, Any]:
    return ScoreBreakdown(
        skill=0.7,
        experience=0.6,
        education=0.5,
        seniority=0.5,
        vector=0.4,
        structured=0.6,
    ).model_dump()


# ── 1. pure resolver ──────────────────────────────────────────────────────


def test_resolve_chunk_context_maps_resume_and_cover_letter_ids() -> None:
    from src.services.shortlist_service import _resolve_chunk_context

    parsed = {
        "chunks": [
            {"id": "c_001", "text": "Built the payments service."},
            {"id": "c_002", "text": "Led a team of six."},
        ],
        "cover_letter_chunks": [
            {"id": "cl_001", "text": "I am drawn to this mission."},
        ],
    }
    out = _resolve_chunk_context(parsed, ["c_002", "cl_001"])
    assert "Led a team of six." in out
    assert "I am drawn to this mission." in out
    # order preserved: c_002 before cl_001
    assert out.index("Led a team of six.") < out.index("I am drawn to this mission.")
    # only the requested ids
    assert "Built the payments service." not in out


def test_resolve_chunk_context_skips_unknown_ids() -> None:
    from src.services.shortlist_service import _resolve_chunk_context

    parsed = {"chunks": [{"id": "c_001", "text": "Known chunk."}]}
    out = _resolve_chunk_context(parsed, ["c_999", "c_001", "cl_404"])
    assert out == "Known chunk."


def test_resolve_chunk_context_dedupes_repeated_ids() -> None:
    from src.services.shortlist_service import _resolve_chunk_context

    parsed = {"chunks": [{"id": "c_001", "text": "Once."}]}
    out = _resolve_chunk_context(parsed, ["c_001", "c_001"])
    assert out.count("Once.") == 1


def test_resolve_chunk_context_handles_empty_and_missing() -> None:
    from src.services.shortlist_service import _resolve_chunk_context

    assert _resolve_chunk_context(None, ["c_001"]) == ""
    assert _resolve_chunk_context({}, ["c_001"]) == ""
    assert _resolve_chunk_context({"chunks": []}, []) == ""
    # parsed given as a JSON string (the export codec shape) still resolves
    parsed_str = json.dumps({"chunks": [{"id": "c_001", "text": "From a string."}]})
    assert _resolve_chunk_context(parsed_str, ["c_001"]) == "From a string."


# ── 2. export path ────────────────────────────────────────────────────────


def _raw_export_row(
    *,
    rank: int = 1,
    evidence: dict[str, Any] | None = None,
    candidate_name: str | None = None,
    candidate_email: str | None = None,
    candidate_phone: str | None = None,
    candidate_parsed: dict[str, Any] | None = None,
) -> _Row:
    ev = evidence if evidence is not None else {"requirements": []}
    return _Row(
        {
            "rank": rank,
            "resume_id": uuid4(),
            "score_final": 0.9,
            "score_breakdown": json.dumps(_breakdown_dict()),
            "evidence": json.dumps(ev),
            "pipeline_meta": json.dumps({"git_sha": None, "weights": {}}),
            "generated_at": dt.datetime(2026, 7, 15, tzinfo=dt.UTC),
            "job_title": "Senior Engineer",
            "job_department": "Platform",
            "original_filename": "resume.pdf",
            "candidate_name": candidate_name,
            "candidate_email": candidate_email,
            "candidate_phone": candidate_phone,
            "candidate_parsed": (
                json.dumps(candidate_parsed) if candidate_parsed is not None else None
            ),
        }
    )


def _mock_export_conn(rows: list[_Row]) -> MagicMock:
    conn = MagicMock(name="conn")
    conn.fetch = AsyncMock(return_value=rows)
    return conn


def _evidence_citing(chunk_id: str) -> dict[str, Any]:
    return {
        "requirements": [
            {
                "requirement": "Python",
                "status": "met",
                "evidence": "quote",
                "evidence_chunk_ids": [chunk_id],
                "confidence": 0.9,
            }
        ]
    }


@pytest.mark.asyncio
async def test_evidence_csv_has_evidence_context_column() -> None:
    from src.services.shortlist_service import shortlist_evidence_csv

    # already-processed export row (evidence dict carries source_context)
    row = {
        "rank": 1,
        "resume_id": uuid4(),
        "score_final": 0.9,
        "score_breakdown": _breakdown_dict(),
        "evidence": {
            "requirements": [
                {
                    "requirement": "Python",
                    "status": "met",
                    "evidence": "quote",
                    "evidence_chunk_ids": ["c_001"],
                    "confidence": 0.9,
                    "source_context": "Built the payments service in Python.",
                }
            ]
        },
        "pipeline_meta": {},
        "generated_at": dt.datetime(2026, 7, 15, tzinfo=dt.UTC),
        "job_title": "Senior Engineer",
        "job_department": "Platform",
        "original_filename": "resume.pdf",
        "candidate_name": "Candidate A",
    }
    csv_text = shortlist_evidence_csv([row])
    reader = csv.DictReader(io.StringIO(csv_text))
    fieldnames = reader.fieldnames or []
    assert "evidence_context" in fieldnames
    assert "evidence_chunk_ids" in fieldnames  # kept for traceability
    data = next(reader)
    assert data["evidence_context"] == "Built the payments service in Python."


@pytest.mark.asyncio
async def test_export_rows_reveal_true_resolves_full_chunk_text() -> None:
    from src.services.shortlist_service import export_rows, shortlist_evidence_csv

    parsed = {
        "candidate": {"name": "Jane Smith"},
        "experience": [],
        "education": [],
        "chunks": [{"id": "c_001", "text": "Jane Smith built the payments service."}],
        "cover_letter_chunks": [],
    }
    row = _raw_export_row(
        candidate_name="Jane Smith",
        candidate_parsed=parsed,
        evidence=_evidence_citing("c_001"),
    )
    conn = _mock_export_conn([row])

    rows = await export_rows(conn, job_id=uuid4(), reveal=True)
    ctx = rows[0]["evidence"]["requirements"][0]["source_context"]
    assert ctx == "Jane Smith built the payments service."
    csv_text = shortlist_evidence_csv(rows)
    data = next(csv.DictReader(io.StringIO(csv_text)))
    assert data["evidence_context"] == "Jane Smith built the payments service."


@pytest.mark.asyncio
async def test_export_rows_reveal_false_redacts_resolved_chunk_text() -> None:
    from src.services.shortlist_service import export_rows, shortlist_evidence_csv

    parsed = {
        "candidate": {"name": "Jane Smith"},
        "experience": [{"company": "Acme Corp", "title": "Eng", "bullets": []}],
        "education": [],
        "chunks": [
            {
                "id": "c_001",
                "text": "Jane Smith — jane.smith@example.test — 604-555-0192. "
                "Worked at Acme Corp.",
            }
        ],
        "cover_letter_chunks": [],
    }
    row = _raw_export_row(
        candidate_name="Jane Smith",
        candidate_email="jane.smith@example.test",
        candidate_phone="604-555-0192",
        candidate_parsed=parsed,
        evidence=_evidence_citing("c_001"),
    )
    conn = _mock_export_conn([row])

    rows = await export_rows(conn, job_id=uuid4(), reveal=False)
    ctx = rows[0]["evidence"]["requirements"][0]["source_context"]
    # resolved text is present but scrubbed of identity
    assert ctx  # not empty
    assert "Jane Smith" not in ctx
    assert "jane.smith@example.test" not in ctx
    assert "604-555-0192" not in ctx
    assert "Acme Corp" not in ctx

    csv_text = shortlist_evidence_csv(rows)
    blob = csv_text
    assert "Jane Smith" not in blob
    assert "jane.smith@example.test" not in blob
    assert "Acme Corp" not in blob


@pytest.mark.asyncio
async def test_export_rows_reveal_false_black_box_scan_resolved_context() -> None:
    from src.services.shortlist_service import export_rows

    real_name = "Zzyzxqrst Wibblesworth"
    real_email = "zzyzxqrst.wibblesworth@example.test"
    real_phone = "778-555-0199"
    parsed = {
        "candidate": {"name": real_name},
        "experience": [],
        "education": [],
        "chunks": [
            {
                "id": "c_001",
                "text": f"{real_name} {real_email} {real_phone} shipped it.",
            }
        ],
        "cover_letter_chunks": [],
    }
    row = _raw_export_row(
        candidate_name=real_name,
        candidate_email=real_email,
        candidate_phone=real_phone,
        candidate_parsed=parsed,
        evidence=_evidence_citing("c_001"),
    )
    conn = _mock_export_conn([row])

    rows = await export_rows(conn, job_id=uuid4(), reveal=False)
    dumped = json.dumps(rows, default=str)
    assert real_name not in dumped
    assert real_email not in dumped
    assert real_phone not in dumped


# ── 3. read path schema + service ─────────────────────────────────────────


def test_requirement_evidence_source_context_defaults_none() -> None:
    req = RequirementEvidence(requirement="Python")
    assert req.source_context is None


def _blind_row(
    *,
    job_id: UUID,
    rank: int = 1,
    evidence: dict[str, Any],
    parsed: dict[str, Any],
    name: str | None = "Jane Smith",
    email: str | None = "jane.smith@example.test",
    phone: str | None = "604-555-0192",
) -> _Row:
    return _Row(
        {
            "id": uuid4(),
            "job_id": job_id,
            "resume_id": uuid4(),
            "rank": rank,
            "score_final": 0.9,
            "score_breakdown": json.dumps(_breakdown_dict()),
            "evidence": json.dumps(evidence),
            "generated_at": dt.datetime(2026, 7, 15, tzinfo=dt.UTC),
            "_c_name": name,
            "_c_email": email,
            "_c_phone": phone,
            "_c_parsed": json.dumps(parsed),
        }
    )


def _mock_conn(*, blind: bool, rows: list[_Row]) -> MagicMock:
    conn = MagicMock(name="conn")
    conn.fetchval = AsyncMock(return_value=blind)
    conn.execute = AsyncMock()
    conn.transaction = MagicMock(return_value=_acm())
    conn.fetch = AsyncMock(return_value=rows)
    return conn


@pytest.mark.asyncio
async def test_blind_read_populates_redacted_source_context() -> None:
    from src.services.shortlist_service import list_for_job

    job_id = uuid4()
    evidence = {
        "requirements": [
            {
                "requirement": "Python",
                "status": "met",
                "evidence": "Led the migration.",
                "evidence_chunk_ids": ["c_001"],
                "confidence": 0.9,
            }
        ]
    }
    parsed = {
        "candidate": {"name": "Jane Smith"},
        "summary": "",
        "total_years_experience": 0,
        "skills": [],
        "experience": [{"company": "Acme Corp", "title": "Eng", "bullets": []}],
        "education": [],
        "chunks": [
            {
                "id": "c_001",
                "text": "Jane Smith led a Python migration at Acme Corp.",
            }
        ],
        "cover_letter_chunks": [],
    }
    row = _blind_row(job_id=job_id, evidence=evidence, parsed=parsed)
    conn = _mock_conn(blind=True, rows=[row])

    entries = await list_for_job(conn, job_id=job_id)
    ev = entries[0].evidence
    assert ev is not None
    ctx = ev.requirements[0].source_context
    assert ctx  # resolved
    assert "Jane Smith" not in ctx
    assert "Acme Corp" not in ctx
    assert "Python migration" in ctx  # non-identity content survives
    # full-DTO byte scan
    dumped = entries[0].model_dump_json()
    assert "Jane Smith" not in dumped
    assert "jane.smith@example.test" not in dumped


# ── 4. rendered template byte-scan ────────────────────────────────────────


def test_card_renders_redacted_source_context_no_pii() -> None:
    """End-to-end: the blind read path builds the entry (redacting resolved
    chunk text), the API serializes it, and the card renders it — the résumé
    owner's real name/email/phone are byte-absent from the HTML, but the
    non-identity chunk content is shown."""
    import asyncio

    from frontend import api_client
    from frontend.app import app
    from src.services.shortlist_service import list_for_job

    job_id = uuid4()
    real_name = "Zzyzxqrst Wibblesworth"
    real_email = "zzyzxqrst.wibblesworth@example.test"
    real_phone = "778-555-0199"
    evidence = {
        "requirements": [
            {
                "requirement": "Python",
                "status": "met",
                "evidence": "Shipped the service.",
                "evidence_chunk_ids": ["c_001"],
                "confidence": 0.9,
            }
        ]
    }
    parsed = {
        "candidate": {"name": real_name, "email": real_email, "phone": real_phone},
        "summary": "",
        "total_years_experience": 0,
        "skills": [],
        "experience": [],
        "education": [],
        "chunks": [
            {
                "id": "c_001",
                "text": f"{real_name} ({real_email}, {real_phone}) shipped the "
                "Kubernetes rollout on schedule.",
            }
        ],
        "cover_letter_chunks": [],
    }
    row = _blind_row(
        job_id=job_id,
        evidence=evidence,
        parsed=parsed,
        name=real_name,
        email=real_email,
        phone=real_phone,
    )
    conn = _mock_conn(blind=True, rows=[row])
    entries = asyncio.run(list_for_job(conn, job_id=job_id))
    entry_dict = entries[0].model_dump(mode="json")

    app.config.update(TESTING=True)
    client = app.test_client()

    orig = api_client.list_shortlist
    api_client.list_shortlist = MagicMock(return_value=[entry_dict])
    try:
        body = client.get(f"/jobs/{job_id}/shortlist-cards").get_data(as_text=True)
    finally:
        api_client.list_shortlist = orig

    assert real_name not in body
    assert real_email not in body
    assert real_phone not in body
    # the non-identity chunk content IS surfaced
    assert "Kubernetes rollout on schedule" in body
