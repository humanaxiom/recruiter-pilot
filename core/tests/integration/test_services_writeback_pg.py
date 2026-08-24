"""Integration tests — ``src/services/job_service.py`` and
``src/services/resume_service.py`` write-back functions against a REAL
Postgres (testcontainers).

Mocked-connection proof of the SQL shape/ordering lives in
``tests/unit/test_services_writeback.py``; this file proves the SQL
actually *behaves*: the optimistic-concurrency WHERE clauses genuinely gate
the UPDATE (not just look right in a string), and the JSONB payloads
round-trip losslessly through the real ``json`` <-> ``jsonb`` boundary back
into the pydantic schemas that produced them.

NOTE: ``resumes.consent_acknowledged`` is ``BOOLEAN NOT NULL`` with NO
default (see ``src/models/ddl.py``) — every resume INSERT in this file
supplies it explicitly, or the insert itself fails before the code under
test ever runs.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import uuid
from collections.abc import AsyncIterator, Iterator

import asyncpg
import pytest
from testcontainers.postgres import PostgresContainer

from src.models.ddl import init_schema
from src.schemas.jobs import JDExtracted, Skill
from src.schemas.resumes import CoverLetterParsed, ResumeParsed
from src.services import job_service, resume_service

_NOW = dt.datetime(2026, 1, 1, 12, 0, 0, tzinfo=dt.UTC)

INSERT_RESUME = """
INSERT INTO resumes (
    job_id, blob_key, original_filename, mime_type,
    file_size_bytes, sha256, consent_acknowledged, status
) VALUES ($1, $2, 'a.pdf', 'application/pdf', 1024, $3, TRUE, $4)
RETURNING id
"""


@pytest.fixture(scope="session")
def pg_dsn() -> Iterator[str]:
    with PostgresContainer("postgres:16-alpine") as pg:
        yield re.sub(r"\+\w+", "", pg.get_connection_url(), count=1)


@pytest.fixture
async def conn(pg_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    connection: asyncpg.Connection = await asyncpg.connect(pg_dsn)
    await init_schema(connection)
    await connection.execute("TRUNCATE jobs, outbox CASCADE")
    try:
        yield connection
    finally:
        await connection.close()


async def _insert_job(conn: asyncpg.Connection, status: str = "draft") -> uuid.UUID:
    job_id: uuid.UUID = await conn.fetchval(
        """
        INSERT INTO jobs (title, description_raw, status)
        VALUES ($1, $2, $3) RETURNING id
        """,
        "Senior Python Engineer",
        "Build the ranking pipeline.",
        status,
    )
    return job_id


async def _insert_resume(
    conn: asyncpg.Connection, job_id: uuid.UUID, status: str = "uploaded"
) -> uuid.UUID:
    resume_id: uuid.UUID = await conn.fetchval(
        INSERT_RESUME,
        job_id,
        f"resumes/{uuid.uuid4().hex}.pdf",
        uuid.uuid4().hex,
        status,
    )
    return resume_id


def _pii_tuple() -> tuple[bytes | None, bytes | None, bytes | None, str | None]:
    return (b"enc-name-bytes", b"enc-email-bytes", b"enc-phone-bytes", "hash-abc123")


# ── job_service.record_parsed ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_job_service_record_parsed_applies_when_status_is_draft(
    conn: asyncpg.Connection,
) -> None:
    job_id = await _insert_job(conn, status="draft")
    extracted = JDExtracted(
        title="Senior Engineer", required_skills=[Skill(name="Python")]
    )

    result = await job_service.record_parsed(conn, job_id, extracted, _NOW)

    assert result is True
    row = await conn.fetchrow(
        "SELECT description_parsed, parsed_at, failure_reason FROM jobs WHERE id = $1",
        job_id,
    )
    assert row is not None
    assert row["failure_reason"] is None
    assert row["parsed_at"] is not None


@pytest.mark.asyncio
async def test_job_service_record_parsed_description_parsed_round_trips(
    conn: asyncpg.Connection,
) -> None:
    """description_parsed must round-trip as the EXACT JDExtracted.model_dump()
    shape — validate it back through JDExtracted.model_validate."""
    job_id = await _insert_job(conn, status="draft")
    extracted = JDExtracted(
        title="Senior Engineer",
        required_skills=[Skill(name="Python", min_years=3)],
        nice_to_have_skills=[Skill(name="Rust")],
        min_years_experience=5,
        location="Vancouver, BC",
        remote_policy="hybrid",
        responsibilities=["Ship the ranking pipeline"],
    )

    await job_service.record_parsed(conn, job_id, extracted, _NOW)

    raw = await conn.fetchval(
        "SELECT description_parsed FROM jobs WHERE id = $1", job_id
    )
    assert raw is not None
    stored = json.loads(raw)
    assert JDExtracted.model_validate(stored) == extracted


@pytest.mark.asyncio
async def test_job_record_parsed_false_and_row_unchanged_when_not_draft(
    conn: asyncpg.Connection,
) -> None:
    job_id = await _insert_job(conn, status="open")
    extracted = JDExtracted(title="Senior Engineer")

    result = await job_service.record_parsed(conn, job_id, extracted, _NOW)

    assert result is False
    row = await conn.fetchrow(
        "SELECT description_parsed, parsed_at FROM jobs WHERE id = $1", job_id
    )
    assert row is not None
    assert row["description_parsed"] is None
    assert row["parsed_at"] is None


@pytest.mark.asyncio
async def test_job_record_parse_failure_sets_reason_without_bad_status(
    conn: asyncpg.Connection,
) -> None:
    """job_status has NO 'failed' value — if record_parse_failure ever tried
    to write one, this would raise asyncpg.InvalidTextRepresentationError
    against the real enum. Proves the function completes and leaves status
    untouched."""
    job_id = await _insert_job(conn, status="draft")

    await job_service.record_parse_failure(conn, job_id, "LLM timeout")

    row = await conn.fetchrow(
        "SELECT status, failure_reason FROM jobs WHERE id = $1", job_id
    )
    assert row is not None
    assert row["failure_reason"] == "LLM timeout"
    assert row["status"] == "draft"


# ── resume_service.record_parsed ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resume_service_record_parsed_applies_when_status_is_uploaded(
    conn: asyncpg.Connection,
) -> None:
    job_id = await _insert_job(conn)
    resume_id = await _insert_resume(conn, job_id, status="uploaded")
    parsed = ResumeParsed(summary="Experienced backend engineer")

    result = await resume_service.record_parsed(
        conn, resume_id, parsed, _pii_tuple(), None, _NOW
    )

    assert result is True
    row = await conn.fetchrow(
        "SELECT status, parsed_at, failure_reason FROM resumes WHERE id = $1",
        resume_id,
    )
    assert row is not None
    assert row["status"] == "parsed"
    assert row["failure_reason"] is None
    assert row["parsed_at"] is not None


@pytest.mark.asyncio
async def test_resume_service_record_parsed_applies_when_status_is_parsing(
    conn: asyncpg.Connection,
) -> None:
    job_id = await _insert_job(conn)
    resume_id = await _insert_resume(conn, job_id, status="parsing")
    parsed = ResumeParsed()

    result = await resume_service.record_parsed(
        conn, resume_id, parsed, _pii_tuple(), None, _NOW
    )

    assert result is True


@pytest.mark.asyncio
async def test_resume_service_record_parsed_parsed_jsonb_round_trips(
    conn: asyncpg.Connection,
) -> None:
    """parsed jsonb must round-trip through ResumeParsed.model_validate."""
    job_id = await _insert_job(conn)
    resume_id = await _insert_resume(conn, job_id, status="uploaded")
    parsed = ResumeParsed(
        summary="Experienced backend engineer",
        total_years_experience=7,
    )

    await resume_service.record_parsed(
        conn, resume_id, parsed, _pii_tuple(), None, _NOW
    )

    raw = await conn.fetchval("SELECT parsed FROM resumes WHERE id = $1", resume_id)
    assert raw is not None
    stored = json.loads(raw)
    assert ResumeParsed.model_validate(stored) == parsed


@pytest.mark.asyncio
async def test_resume_record_parsed_false_and_row_unchanged_when_terminal(
    conn: asyncpg.Connection,
) -> None:
    job_id = await _insert_job(conn)
    resume_id = await _insert_resume(conn, job_id, status="failed")
    parsed = ResumeParsed(summary="should never be written")

    result = await resume_service.record_parsed(
        conn, resume_id, parsed, _pii_tuple(), None, _NOW
    )

    assert result is False
    row = await conn.fetchrow(
        "SELECT status, parsed, parsed_at FROM resumes WHERE id = $1", resume_id
    )
    assert row is not None
    assert row["status"] == "failed"
    assert row["parsed"] is None
    assert row["parsed_at"] is None


@pytest.mark.asyncio
async def test_resume_service_record_parsed_cover_letter_parsed_round_trips(
    conn: asyncpg.Connection,
) -> None:
    job_id = await _insert_job(conn)
    resume_id = await _insert_resume(conn, job_id, status="uploaded")
    cl = CoverLetterParsed(
        raw_text="Dear hiring manager, I am thrilled to apply.",
        themes=["enthusiasm", "fit"],
        sentiment="positive",
    )

    await resume_service.record_parsed(
        conn, resume_id, ResumeParsed(), _pii_tuple(), cl, _NOW
    )

    raw = await conn.fetchval(
        "SELECT cover_letter_parsed FROM resumes WHERE id = $1", resume_id
    )
    assert raw is not None
    stored = json.loads(raw)
    assert CoverLetterParsed.model_validate(stored) == cl


@pytest.mark.asyncio
async def test_resume_service_record_parsed_stores_pii_bytea_columns_and_email_hash(
    conn: asyncpg.Connection,
) -> None:
    job_id = await _insert_job(conn)
    resume_id = await _insert_resume(conn, job_id, status="uploaded")
    pii = _pii_tuple()

    await resume_service.record_parsed(conn, resume_id, ResumeParsed(), pii, None, _NOW)

    row = await conn.fetchrow(
        """
        SELECT candidate_name, candidate_email, candidate_phone,
               candidate_email_hash
          FROM resumes WHERE id = $1
        """,
        resume_id,
    )
    assert row is not None
    assert row["candidate_name"] == pii[0]
    assert row["candidate_email"] == pii[1]
    assert row["candidate_phone"] == pii[2]
    assert row["candidate_email_hash"] == pii[3]


# ── resume_service.record_parse_failure ──────────────────────────────────────


@pytest.mark.asyncio
async def test_resume_record_parse_failure_sets_status_failed_and_reason(
    conn: asyncpg.Connection,
) -> None:
    job_id = await _insert_job(conn)
    resume_id = await _insert_resume(conn, job_id, status="parsing")

    await resume_service.record_parse_failure(conn, resume_id, "corrupt pdf")

    row = await conn.fetchrow(
        "SELECT status, failure_reason FROM resumes WHERE id = $1", resume_id
    )
    assert row is not None
    assert row["status"] == "failed"
    assert row["failure_reason"] == "corrupt pdf"
