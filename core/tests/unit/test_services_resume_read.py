"""Unit tests for ``src.services.resume_service.list_for_job`` / ``get_one``
(new in Phase 5) — the résumé-side blind-review redaction boundary. All I/O
is mocked; the real Postgres round trip lives in
``tests/integration/test_resume_read_pg.py``.

``src.services.resume_service`` currently only ships the worker write-back
trio (``encrypt_pii_via_session`` / ``record_parsed`` / ``record_parse_failure``
— see the module's Phase-3 docstring, which explicitly defers the read side to
Phase 5). ``list_for_job`` / ``get_one`` do not exist yet — RED half of the
TDD cycle; every test below fails, either at collection or at the first
``await``.

Column-alias contract these mocks assume for ``get_one`` (ported from hris
``resume_service.get_one``): the SQL row carries the decrypted PII / cover
letter under ``c_name`` / ``c_email`` / ``c_phone`` / ``cl_text`` so the read
layer can redact them out of the parsed text before they are ever assigned
onto a response field.

Locked decision pinned by
``test_list_for_job_masks_candidate_name_when_job_is_blind`` (human, this
session): under blind review, ``ResumeListItem.candidate_name`` is ``None``
— NOT a pseudonym — because a résumé LIST has no rank to build one from
(unlike the shortlist, which always carries a rank). This closes a real gap
in the hris source (``resume_service.list_for_job`` there decrypts and
returns the candidate name unconditionally, with no blind check at all —
security-flagged in this project's ADR-006 §4).
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from src.errors import NotFoundError

# A distinctive résumé filename that leaks candidate identity verbatim — the
# de-anonymization leak this phase closes (a résumé named after its candidate).
_IDENTITY_FILENAME = "Zzyzxqrst_Wibblesworth_CV.pdf"


class _Row(dict[str, Any]):
    def __getitem__(self, key: str) -> Any:
        return dict.get(self, key)


def _acm(return_value: Any = None) -> MagicMock:
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=return_value)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _list_row(
    *,
    resume_id: UUID | None = None,
    candidate_name: str | None = None,
    has_cover_letter: bool = False,
    original_filename: str = "resume.pdf",
) -> _Row:
    return _Row(
        {
            "id": resume_id or uuid4(),
            "original_filename": original_filename,
            "status": "parsed",
            "uploaded_at": dt.datetime(2026, 7, 15, tzinfo=dt.UTC),
            "parsed_at": dt.datetime(2026, 7, 15, tzinfo=dt.UTC),
            "candidate_name": candidate_name,
            "has_cover_letter": has_cover_letter,
        }
    )


def _get_row(
    *,
    resume_id: UUID,
    job_id: UUID | None = None,
    parsed: dict[str, Any] | None = None,
    candidate_name: str | None = None,
    candidate_email: str | None = None,
    candidate_phone: str | None = None,
    cover_letter_text: str | None = None,
    cover_letter_parsed: dict[str, Any] | None = None,
    status: str = "parsed",
    original_filename: str = "resume.pdf",
) -> _Row:
    cl_parsed = (
        json.dumps(cover_letter_parsed) if cover_letter_parsed is not None else None
    )
    return _Row(
        {
            "id": resume_id,
            "job_id": job_id or uuid4(),
            "original_filename": original_filename,
            "mime_type": "application/pdf",
            "file_size_bytes": 12345,
            "sha256": "deadbeef",
            "c_name": candidate_name,
            "c_email": candidate_email,
            "c_phone": candidate_phone,
            "cl_text": cover_letter_text,
            "cover_letter_parsed": cl_parsed,
            "candidate_email_hash": None,
            "parsed": json.dumps(parsed) if parsed is not None else None,
            "status": status,
            "uploaded_by": None,
            "uploaded_at": dt.datetime(2026, 7, 15, tzinfo=dt.UTC),
            "parsed_at": dt.datetime(2026, 7, 15, tzinfo=dt.UTC),
            "failure_reason": None,
            "consent_acknowledged": True,
        }
    )


def _mock_conn(
    *,
    blind: bool,
    row: _Row | None = None,
    rows: list[_Row] | None = None,
) -> MagicMock:
    conn = MagicMock(name="conn")
    conn.fetchrow = AsyncMock(return_value=row)
    conn.fetch = AsyncMock(return_value=rows or [])
    conn.fetchval = AsyncMock(return_value=blind)
    conn.execute = AsyncMock()
    conn.transaction = MagicMock(return_value=_acm())
    return conn


def _full_parsed(*, name: str) -> dict[str, Any]:
    first = name.split()[0].lower()
    return {
        "candidate": {
            "name": name,
            "email": f"{first}@example.test",
            "phone": "604-555-0192",
            "location": "Toronto, Ontario",
        },
        "summary": f"{name} is a senior engineer.",
        "total_years_experience": 8,
        "skills": [],
        "experience": [
            {
                "company": "Acme Corp",
                "title": "Engineer",
                "start": "2019",
                "end": None,
                "is_current": True,
                "bullets": [
                    {
                        "text": f"{name} led the platform migration.",
                        "chunk_id": "c_001",
                    }
                ],
            }
        ],
        "education": [
            {
                "degree": "BSc",
                "institution": "State University",
                "field": "CS",
                "year": 2015,
            }
        ],
        "chunks": [
            {
                "id": "c_001",
                "section": "experience",
                "page": 0,
                "text": f"{name} led the platform migration.",
            }
        ],
        "cover_letter_chunks": [],
    }


# ── list_for_job: locked decision — blind candidate_name is None ───────────


@pytest.mark.asyncio
async def test_list_for_job_masks_candidate_name_when_job_is_blind() -> None:
    from src.services.resume_service import list_for_job

    job_id = uuid4()
    row = _list_row(candidate_name="Jane Smith")
    conn = _mock_conn(blind=True, rows=[row])

    items = await list_for_job(conn, job_id=job_id)

    assert items[0].candidate_name is None


@pytest.mark.asyncio
async def test_list_for_job_shows_real_candidate_name_when_not_blind() -> None:
    from src.services.resume_service import list_for_job

    job_id = uuid4()
    row = _list_row(candidate_name="Jane Smith")
    conn = _mock_conn(blind=False, rows=[row])

    items = await list_for_job(conn, job_id=job_id)

    assert items[0].candidate_name == "Jane Smith"


@pytest.mark.asyncio
async def test_list_for_job_masks_candidate_name_even_when_not_yet_parsed() -> None:
    """A row whose decrypted name is None already (unparsed résumé) under a
    blind job must still resolve to None, not error."""
    from src.services.resume_service import list_for_job

    job_id = uuid4()
    row = _list_row(candidate_name=None)
    conn = _mock_conn(blind=True, rows=[row])

    items = await list_for_job(conn, job_id=job_id)

    assert items[0].candidate_name is None


@pytest.mark.asyncio
async def test_list_for_job_empty_job_returns_empty_list() -> None:
    from src.services.resume_service import list_for_job

    job_id = uuid4()
    conn = _mock_conn(blind=False, rows=[])

    items = await list_for_job(conn, job_id=job_id)

    assert items == []


@pytest.mark.asyncio
async def test_list_for_job_preserves_has_cover_letter_flag() -> None:
    from src.services.resume_service import list_for_job

    job_id = uuid4()
    row = _list_row(candidate_name="Jane Smith", has_cover_letter=True)
    conn = _mock_conn(blind=False, rows=[row])

    items = await list_for_job(conn, job_id=job_id)

    assert items[0].has_cover_letter is True


# ── filename redaction: the résumé-name de-anonymization leak ──────────────


@pytest.mark.asyncio
async def test_list_for_job_blind_redacts_identifying_filename() -> None:
    from src.services.resume_service import list_for_job

    job_id = uuid4()
    row = _list_row(candidate_name="Jane Smith", original_filename=_IDENTITY_FILENAME)
    conn = _mock_conn(blind=True, rows=[row])

    items = await list_for_job(conn, job_id=job_id)

    assert items[0].original_filename == "resume.pdf"
    assert "Wibblesworth" not in items[0].original_filename


@pytest.mark.asyncio
async def test_list_for_job_non_blind_keeps_real_filename() -> None:
    from src.services.resume_service import list_for_job

    job_id = uuid4()
    row = _list_row(candidate_name="Jane Smith", original_filename=_IDENTITY_FILENAME)
    conn = _mock_conn(blind=False, rows=[row])

    items = await list_for_job(conn, job_id=job_id)

    assert items[0].original_filename == _IDENTITY_FILENAME


# ── get_one: blind masking ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_one_blind_masks_candidate_and_redacts_parsed_fields() -> None:
    from src.services.resume_service import get_one

    resume_id = uuid4()
    parsed = _full_parsed(name="Jane Smith")
    row = _get_row(
        resume_id=resume_id,
        parsed=parsed,
        candidate_name="Jane Smith",
        candidate_email="jane@example.test",
        candidate_phone="604-555-0192",
        cover_letter_text="Dear hiring team, Jane Smith here.",
    )
    conn = _mock_conn(blind=True, row=row)

    out = await get_one(conn, resume_id)

    assert out.blinded is True
    assert out.candidate.name is None
    assert out.candidate.email is None
    assert out.candidate.phone is None
    assert out.cover_letter_text is None
    assert out.parsed is not None
    assert "Jane Smith" not in out.parsed.summary
    assert "Jane Smith" not in out.parsed.chunks[0].text
    assert "Jane Smith" not in out.parsed.experience[0].bullets[0].text
    assert out.parsed.experience[0].company == "Employer A"
    assert out.parsed.education[0].institution == "Institution A"
    assert out.parsed.education[0].year is None


@pytest.mark.asyncio
async def test_get_one_blind_redacts_identifying_filename() -> None:
    from src.services.resume_service import get_one

    resume_id = uuid4()
    row = _get_row(
        resume_id=resume_id,
        candidate_name="Jane Smith",
        original_filename=_IDENTITY_FILENAME,
    )
    conn = _mock_conn(blind=True, row=row)

    out = await get_one(conn, resume_id)

    assert out.original_filename == "resume.pdf"
    assert "Wibblesworth" not in out.model_dump_json()


@pytest.mark.asyncio
async def test_get_one_reveal_true_keeps_real_filename() -> None:
    from src.services.resume_service import get_one

    resume_id = uuid4()
    row = _get_row(
        resume_id=resume_id,
        candidate_name="Jane Smith",
        original_filename=_IDENTITY_FILENAME,
    )
    conn = _mock_conn(blind=True, row=row)

    out = await get_one(conn, resume_id, reveal=True)

    assert out.original_filename == _IDENTITY_FILENAME


@pytest.mark.asyncio
async def test_get_one_non_blind_keeps_real_filename() -> None:
    from src.services.resume_service import get_one

    resume_id = uuid4()
    row = _get_row(
        resume_id=resume_id,
        candidate_name="Jane Smith",
        original_filename=_IDENTITY_FILENAME,
    )
    conn = _mock_conn(blind=False, row=row)

    out = await get_one(conn, resume_id)

    assert out.original_filename == _IDENTITY_FILENAME


@pytest.mark.asyncio
async def test_get_one_blind_canadian_location_stays_visible() -> None:
    from src.services.resume_service import get_one

    resume_id = uuid4()
    parsed = _full_parsed(name="Jane Smith")  # location: Toronto, Ontario
    row = _get_row(resume_id=resume_id, parsed=parsed, candidate_name="Jane Smith")
    conn = _mock_conn(blind=True, row=row)

    out = await get_one(conn, resume_id)

    assert out.candidate.location == "Toronto, Ontario"


@pytest.mark.asyncio
async def test_get_one_blind_foreign_location_is_masked() -> None:
    from src.services.resume_service import get_one

    resume_id = uuid4()
    parsed = _full_parsed(name="Jane Smith")
    parsed["candidate"]["location"] = "Austin, Texas"
    row = _get_row(resume_id=resume_id, parsed=parsed, candidate_name="Jane Smith")
    conn = _mock_conn(blind=True, row=row)

    out = await get_one(conn, resume_id)

    assert out.candidate.location is None


@pytest.mark.asyncio
async def test_get_one_non_blind_returns_real_candidate_and_parsed() -> None:
    from src.services.resume_service import get_one

    resume_id = uuid4()
    parsed = _full_parsed(name="Jane Smith")
    row = _get_row(
        resume_id=resume_id,
        parsed=parsed,
        candidate_name="Jane Smith",
        candidate_email="jane@example.test",
        candidate_phone="604-555-0192",
        cover_letter_text="Dear hiring team, Jane Smith here.",
    )
    conn = _mock_conn(blind=False, row=row)

    out = await get_one(conn, resume_id)

    assert out.blinded is False
    assert out.candidate.name == "Jane Smith"
    assert out.cover_letter_text == "Dear hiring team, Jane Smith here."
    assert out.parsed is not None
    assert "Jane Smith" in out.parsed.summary
    assert out.parsed.experience[0].company == "Acme Corp"


@pytest.mark.asyncio
async def test_get_one_reveal_true_bypasses_masking() -> None:
    from src.services.resume_service import get_one

    resume_id = uuid4()
    parsed = _full_parsed(name="Jane Smith")
    row = _get_row(
        resume_id=resume_id,
        parsed=parsed,
        candidate_name="Jane Smith",
        candidate_email="jane@example.test",
        candidate_phone="604-555-0192",
    )
    conn = _mock_conn(blind=True, row=row)

    out = await get_one(conn, resume_id, reveal=True)

    assert out.blinded is False
    assert out.candidate.name == "Jane Smith"
    assert out.parsed is not None
    assert "Jane Smith" in out.parsed.summary


@pytest.mark.asyncio
async def test_get_one_nonexistent_resume_raises_not_found() -> None:
    from src.services.resume_service import get_one

    conn = _mock_conn(blind=False, row=None)

    with pytest.raises(NotFoundError):
        await get_one(conn, uuid4())


# ── black-box scan: the redaction-boundary guard (landmine) ────────────────


@pytest.mark.asyncio
async def test_black_box_scan_no_pii_in_blind_resume_out() -> None:
    """Fails if masking is applied AFTER DTO construction (only some fields
    nulled) rather than before — serialises the REAL ``get_one`` output (not
    a hand-built DTO) to JSON and greps for the raw candidate identity
    strings anywhere in the byte stream, including nested ``parsed.*``
    fields the individually-checked assertions above don't enumerate one by
    one."""
    from src.services.resume_service import get_one

    resume_id = uuid4()
    real_name = "Zzyzxqrst Wibblesworth"
    real_email = "zzyzxqrst.wibblesworth@example.test"
    real_phone = "778-555-0199"
    parsed = _full_parsed(name=real_name)
    parsed["candidate"]["email"] = real_email
    parsed["candidate"]["phone"] = real_phone
    cover_letter = f"Sincerely, {real_name}. Reach {real_email} or {real_phone}."
    row = _get_row(
        resume_id=resume_id,
        parsed=parsed,
        candidate_name=real_name,
        candidate_email=real_email,
        candidate_phone=real_phone,
        cover_letter_text=cover_letter,
        original_filename=_IDENTITY_FILENAME,
    )
    conn = _mock_conn(blind=True, row=row)

    out = await get_one(conn, resume_id)
    dumped = out.model_dump_json()

    assert real_name not in dumped
    assert real_email not in dumped
    assert real_phone not in dumped
    # The résumé's own filename leaks identity verbatim too (a landmine the
    # field-by-field asserts miss): the raw filename must be absent.
    assert _IDENTITY_FILENAME not in dumped


@pytest.mark.asyncio
async def test_black_box_scan_no_pii_in_blind_cover_letter_chunks() -> None:
    """Regression: cover-letter chunks carry the candidate's OWN
    name/email/phone (the first ~200 chars of a cover letter are the
    letterhead — see ``worker/resume_tasks.py``). The prior scan left
    ``cover_letter_chunks`` EMPTY, so ``_blind_parsed`` omitting that field
    slipped through. Make the chunks NON-empty and assert the raw identity is
    absent from the serialised blind ``ResumeOut``."""
    from src.services.resume_service import get_one

    resume_id = uuid4()
    real_name = "Zzyzxqrst Wibblesworth"
    real_email = "zzyzxqrst.wibblesworth@example.test"
    real_phone = "778-555-0199"
    parsed = _full_parsed(name=real_name)
    parsed["candidate"]["email"] = real_email
    parsed["candidate"]["phone"] = real_phone
    parsed["cover_letter_chunks"] = [
        {
            "id": "cl_001",
            "section": "cover_letter",
            "page": 0,
            "text": f"{real_name}\n{real_email}\n{real_phone}\nDear hiring team,",
        }
    ]
    row = _get_row(
        resume_id=resume_id,
        parsed=parsed,
        candidate_name=real_name,
        candidate_email=real_email,
        candidate_phone=real_phone,
        cover_letter_text=f"Sincerely, {real_name}.",
    )
    conn = _mock_conn(blind=True, row=row)

    out = await get_one(conn, resume_id)
    assert out.parsed is not None
    # The chunk must survive redaction (redacted text, not dropped).
    assert out.parsed.cover_letter_chunks
    dumped = out.model_dump_json()

    assert real_name not in dumped
    assert real_email not in dumped
    assert real_phone not in dumped
