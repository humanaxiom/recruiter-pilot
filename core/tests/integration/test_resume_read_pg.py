"""Integration tests — Phase 5's résumé read
(``src.services.resume_service.list_for_job`` / ``get_one``) against a REAL
Postgres (testcontainers), mirroring ``test_shortlist_persistence_pg.py``'s
``pg_dsn``/``pg_pool`` fixtures and ``test_pii_encryption.py``'s
``patched_pii_key`` pattern.

Neither ``list_for_job`` nor ``get_one`` exist yet on
``src.services.resume_service`` (that module currently ships only the
worker write-back trio — see its Phase-3 docstring, which explicitly defers
the read side to Phase 5). Every test below fails at collection time with
``ModuleNotFoundError``. RED half of the TDD cycle.

What a REAL Postgres proves that the mocked-conn unit tests
(``test_services_resume_read.py``) cannot: the blind-review branch's real SQL
(join to ``jobs``, real ``pgp_sym_decrypt`` on ``candidate_name`` /
``candidate_email`` / ``candidate_phone`` / ``cover_letter_text``) resolves
against real encrypted PII columns, and ``ResumeParsed`` really does
round-trip through the real jsonb codec (a JSON string, not a pre-parsed
dict — no custom codec is registered anywhere in this project).
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import AsyncIterator, Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch
from uuid import UUID

import asyncpg
import pytest
from testcontainers.postgres import PostgresContainer

from src.errors import NotFoundError
from src.models.ddl import init_schema
from src.services.pii import encrypt as pii_encrypt
from src.services.pii import set_pii_key

TEST_PII_KEY = "integration-test-pii-key-do-not-use-in-prod"


@pytest.fixture(scope="session")
def pg_dsn() -> Iterator[str]:
    with PostgresContainer("postgres:16-alpine") as pg:
        yield re.sub(r"\+\w+", "", pg.get_connection_url(), count=1)


@pytest.fixture
async def pg_pool(pg_dsn: str) -> AsyncIterator[asyncpg.Pool]:
    pool = await asyncpg.create_pool(dsn=pg_dsn, min_size=2, max_size=8)
    await init_schema(pool)
    async with pool.acquire() as conn:
        await conn.execute("TRUNCATE jobs, resumes, outbox CASCADE")
    try:
        yield pool
    finally:
        await pool.close()


@pytest.fixture(autouse=True)
def patched_pii_key() -> Iterator[None]:
    fake_settings = SimpleNamespace(pii_key=TEST_PII_KEY)
    with patch("src.services.pii.get_settings", return_value=fake_settings):
        yield


def _full_parsed(*, name: str) -> dict[str, Any]:
    return {
        "candidate": {
            "name": name,
            "email": "jane.smith@example.test",
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


async def _insert_job(pool: asyncpg.Pool, *, blind_review: bool) -> UUID:
    async with pool.acquire() as conn:
        job_id: UUID = await conn.fetchval(
            "INSERT INTO jobs (title, description_raw, blind_review) "
            "VALUES ($1, $2, $3) RETURNING id",
            "Senior Backend Engineer",
            "raw jd text (irrelevant to these tests)",
            blind_review,
        )
    return job_id


async def _insert_resume(
    pool: asyncpg.Pool,
    job_id: UUID,
    *,
    name: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    cover_letter_text: str | None = None,
    parsed: dict[str, Any] | None = None,
    status: str = "parsed",
    original_filename: str = "resume.pdf",
) -> UUID:
    async with pool.acquire() as conn:
        resume_id: UUID = await conn.fetchval(
            """
            INSERT INTO resumes (
                job_id, blob_key, original_filename, mime_type,
                file_size_bytes, sha256, consent_acknowledged, status, parsed
            ) VALUES ($1, $2, $3, 'application/pdf', 1024, $4, TRUE,
                       $5, $6::jsonb)
            RETURNING id
            """,
            job_id,
            f"resumes/{uuid.uuid4().hex}.pdf",
            original_filename,
            uuid.uuid4().hex,
            status,
            json.dumps(parsed) if parsed is not None else None,
        )
        async with conn.transaction():
            await set_pii_key(conn)
            name_enc = await pii_encrypt(conn, name)
            email_enc = await pii_encrypt(conn, email)
            phone_enc = await pii_encrypt(conn, phone)
            cover_enc = await pii_encrypt(conn, cover_letter_text)
            await conn.execute(
                "UPDATE resumes SET candidate_name = $2, candidate_email = $3, "
                "candidate_phone = $4, cover_letter_text = $5 WHERE id = $1",
                resume_id,
                name_enc,
                email_enc,
                phone_enc,
                cover_enc,
            )
    return resume_id


# ── list_for_job: blind masks candidate_name, non-blind shows it ───────────


@pytest.mark.asyncio
async def test_list_for_job_blind_job_masks_candidate_name_against_real_pii(
    pg_pool: asyncpg.Pool,
) -> None:
    from src.services.resume_service import list_for_job

    job_id = await _insert_job(pg_pool, blind_review=True)
    await _insert_resume(pg_pool, job_id, name="Jane Smith")

    async with pg_pool.acquire() as conn:
        items = await list_for_job(conn, job_id=job_id)

    assert len(items) == 1
    assert items[0].candidate_name is None


@pytest.mark.asyncio
async def test_list_for_job_non_blind_job_shows_real_candidate_name(
    pg_pool: asyncpg.Pool,
) -> None:
    from src.services.resume_service import list_for_job

    job_id = await _insert_job(pg_pool, blind_review=False)
    await _insert_resume(pg_pool, job_id, name="Jane Smith")

    async with pg_pool.acquire() as conn:
        items = await list_for_job(conn, job_id=job_id)

    assert items[0].candidate_name == "Jane Smith"


@pytest.mark.asyncio
async def test_list_for_job_empty_job_returns_empty_list(pg_pool: asyncpg.Pool) -> None:
    from src.services.resume_service import list_for_job

    job_id = await _insert_job(pg_pool, blind_review=False)

    async with pg_pool.acquire() as conn:
        items = await list_for_job(conn, job_id=job_id)

    assert items == []


# ── get_one round trip against real encrypted PII ───────────────────────


@pytest.mark.asyncio
async def test_get_one_non_blind_round_trips_real_encrypted_pii_and_parsed(
    pg_pool: asyncpg.Pool,
) -> None:
    from src.services.resume_service import get_one

    job_id = await _insert_job(pg_pool, blind_review=False)
    parsed = _full_parsed(name="Jane Smith")
    resume_id = await _insert_resume(
        pg_pool,
        job_id,
        name="Jane Smith",
        email="jane.smith@example.test",
        phone="604-555-0192",
        cover_letter_text="Dear hiring team, Jane Smith here.",
        parsed=parsed,
    )

    async with pg_pool.acquire() as conn:
        out = await get_one(conn, resume_id)

    assert out.blinded is False
    assert out.candidate.name == "Jane Smith"
    assert out.candidate.email == "jane.smith@example.test"
    assert out.candidate.phone == "604-555-0192"
    assert out.cover_letter_text == "Dear hiring team, Jane Smith here."
    assert out.parsed is not None
    assert out.parsed.experience[0].company == "Acme Corp"


@pytest.mark.asyncio
async def test_get_one_blind_round_trips_and_masks_real_encrypted_pii(
    pg_pool: asyncpg.Pool,
) -> None:
    from src.services.resume_service import get_one

    job_id = await _insert_job(pg_pool, blind_review=True)
    parsed = _full_parsed(name="Jane Smith")
    resume_id = await _insert_resume(
        pg_pool,
        job_id,
        name="Jane Smith",
        email="jane.smith@example.test",
        phone="604-555-0192",
        cover_letter_text="Dear hiring team, Jane Smith here.",
        parsed=parsed,
    )

    async with pg_pool.acquire() as conn:
        out = await get_one(conn, resume_id)

    assert out.blinded is True
    assert out.candidate.name is None
    assert out.candidate.email is None
    assert out.candidate.phone is None
    assert out.cover_letter_text is None
    assert out.parsed is not None
    assert "Jane Smith" not in out.parsed.summary
    assert out.parsed.experience[0].company == "Employer A"

    dumped = out.model_dump_json()
    assert "Jane Smith" not in dumped
    assert "jane.smith@example.test" not in dumped


@pytest.mark.asyncio
async def test_get_one_blind_redacts_cover_letter_chunks_against_real_pii(
    pg_pool: asyncpg.Pool,
) -> None:
    """Regression: cover-letter chunks carry the candidate's OWN identity in
    their text (letterhead). Round-tripped through real jsonb + a real blind
    ``jobs`` join, the serialised ``ResumeOut`` must not leak the raw
    name/email/phone via ``parsed.cover_letter_chunks``."""
    from src.services.resume_service import get_one

    job_id = await _insert_job(pg_pool, blind_review=True)
    name = "Zzyzxqrst Wibblesworth"
    email = "zzyzxqrst.wibblesworth@example.test"
    phone = "778-555-0199"
    parsed = _full_parsed(name=name)
    parsed["candidate"]["email"] = email
    parsed["candidate"]["phone"] = phone
    parsed["cover_letter_chunks"] = [
        {
            "id": "cl_001",
            "section": "cover_letter",
            "page": 0,
            "text": f"{name}\n{email}\n{phone}\nDear hiring team,",
        }
    ]
    resume_id = await _insert_resume(
        pg_pool,
        job_id,
        name=name,
        email=email,
        phone=phone,
        cover_letter_text=f"Sincerely, {name}.",
        parsed=parsed,
    )

    async with pg_pool.acquire() as conn:
        out = await get_one(conn, resume_id)

    assert out.blinded is True
    assert out.parsed is not None
    assert out.parsed.cover_letter_chunks

    dumped = out.model_dump_json()
    assert name not in dumped
    assert email not in dumped
    assert phone not in dumped


@pytest.mark.asyncio
async def test_get_one_blind_redacts_identifying_filename_against_real_row(
    pg_pool: asyncpg.Pool,
) -> None:
    """A résumé named after its candidate (``Zzyzxqrst_Wibblesworth_CV.pdf``)
    leaks identity verbatim through the blind surface — round-tripped through a
    real blind ``jobs`` join, the filename must be masked to ``resume.pdf``."""
    from src.services.resume_service import get_one

    job_id = await _insert_job(pg_pool, blind_review=True)
    resume_id = await _insert_resume(
        pg_pool,
        job_id,
        name="Jane Smith",
        original_filename="Zzyzxqrst_Wibblesworth_CV.pdf",
    )

    async with pg_pool.acquire() as conn:
        out = await get_one(conn, resume_id)

    assert out.original_filename == "resume.pdf"
    assert "Wibblesworth" not in out.model_dump_json()


@pytest.mark.asyncio
async def test_list_for_job_blind_redacts_identifying_filename_against_real_row(
    pg_pool: asyncpg.Pool,
) -> None:
    from src.services.resume_service import list_for_job

    job_id = await _insert_job(pg_pool, blind_review=True)
    await _insert_resume(
        pg_pool,
        job_id,
        name="Jane Smith",
        original_filename="Zzyzxqrst_Wibblesworth_CV.pdf",
    )

    async with pg_pool.acquire() as conn:
        items = await list_for_job(conn, job_id=job_id)

    assert items[0].original_filename == "resume.pdf"


@pytest.mark.asyncio
async def test_get_one_reveal_true_bypasses_masking_against_real_blind_job(
    pg_pool: asyncpg.Pool,
) -> None:
    from src.services.resume_service import get_one

    job_id = await _insert_job(pg_pool, blind_review=True)
    resume_id = await _insert_resume(
        pg_pool,
        job_id,
        name="Jane Smith",
        email="jane.smith@example.test",
    )

    async with pg_pool.acquire() as conn:
        out = await get_one(conn, resume_id, reveal=True)

    assert out.blinded is False
    assert out.candidate.name == "Jane Smith"


@pytest.mark.asyncio
async def test_get_one_nonexistent_resume_raises_not_found(
    pg_pool: asyncpg.Pool,
) -> None:
    from src.services.resume_service import get_one

    async with pg_pool.acquire() as conn:
        with pytest.raises(NotFoundError):
            await get_one(conn, uuid.uuid4())
