"""Integration tests — FU-7 §4 / ADR-030 degraded-parse visibility, read side,
against a REAL Postgres (testcontainers). Mirrors ``test_resume_read_pg.py``'s
fixtures/helpers and ``test_resume_withdraw_pg.py``'s
``status_breakdown``-mutual-exclusion style.

Scope: skills-extraction fell back to the deterministic keyword scan (the
``resume_skills_v2`` ``LLMOutputInvalidError`` catch in
``src/worker/resume_tasks.py::_extract_skills_merged``). ``ResumeParsed``
carries ``degraded``/``degradation_reason`` inside the existing
``resumes.parsed`` jsonb — no DDL change — so these tests seed the flag
directly in that column, exactly as a real (degraded) parse would leave it.

What a REAL Postgres proves that a mocked-conn unit test cannot: the
``(parsed->>'degraded')::bool`` jsonb-boolean cast used by both
``status_breakdown``'s FILTER aggregate and ``list_for_job``'s
``COALESCE`` projection resolves against a REAL jsonb column (a JSON string
on the wire, not a pre-parsed dict), and the blind-review redaction path
(``get_one``) really does leave this NON-PII flag untouched across a real
``pgp_sym_decrypt`` + blind ``jobs`` join.

``ResumeParsed.degraded``/``degradation_reason`` and
``ResumeStatusBreakdown.degraded`` do not exist yet — every test below fails
at collection (``AttributeError``/``ValidationError``) or at the first
assertion. RED half of the TDD cycle.
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

from src.models.ddl import init_schema
from src.services.pii import encrypt as pii_encrypt
from src.services.pii import set_pii_key

TEST_PII_KEY = "integration-test-pii-key-do-not-use-in-prod"

DEGRADED_REASON = "skills extraction failed (AI); using keyword-scan fallback"


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


def _parsed_payload(*, degraded: bool, reason: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "candidate": {"name": "Jane Smith", "email": "jane.smith@example.test"},
        "summary": "Jane Smith is a senior engineer.",
        "total_years_experience": 8,
        "skills": [{"name": "python"}],
        "experience": [],
        "education": [],
        "chunks": [],
        "cover_letter_chunks": [],
        "degraded": degraded,
    }
    if reason is not None:
        payload["degradation_reason"] = reason
    return payload


async def _insert_job(pool: asyncpg.Pool, *, blind_review: bool = False) -> UUID:
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
    status: str = "parsed",
    parsed: dict[str, Any] | None = None,
    name: str = "Jane Smith",
) -> UUID:
    async with pool.acquire() as conn:
        resume_id: UUID = await conn.fetchval(
            """
            INSERT INTO resumes (
                job_id, blob_key, original_filename, mime_type,
                file_size_bytes, sha256, consent_acknowledged, status, parsed
            ) VALUES ($1, $2, 'resume.pdf', 'application/pdf', 1024, $3, TRUE,
                       $4, $5::jsonb)
            RETURNING id
            """,
            job_id,
            f"resumes/{uuid.uuid4().hex}.pdf",
            uuid.uuid4().hex,
            status,
            json.dumps(parsed) if parsed is not None else None,
        )
        async with conn.transaction():
            await set_pii_key(conn)
            name_enc = await pii_encrypt(conn, name)
            await conn.execute(
                "UPDATE resumes SET candidate_name = $2 WHERE id = $1",
                resume_id,
                name_enc,
            )
    return resume_id


# ── status_breakdown: degraded is a sub-count of parsed, not a peer bucket ──


@pytest.mark.asyncio
async def test_status_breakdown_counts_degraded_as_sub_count_of_parsed(
    pg_pool: asyncpg.Pool,
) -> None:
    from src.services.resume_service import status_breakdown

    job_id = await _insert_job(pg_pool)
    await _insert_resume(
        pg_pool, job_id, status="parsed", parsed=_parsed_payload(degraded=False)
    )
    await _insert_resume(
        pg_pool,
        job_id,
        status="parsed",
        parsed=_parsed_payload(degraded=True, reason=DEGRADED_REASON),
    )
    await _insert_resume(
        pg_pool,
        job_id,
        status="parsed",
        parsed=_parsed_payload(degraded=True, reason=DEGRADED_REASON),
    )
    await _insert_resume(pg_pool, job_id, status="uploaded", parsed=None)

    async with pg_pool.acquire() as conn:
        breakdown = await status_breakdown(conn, job_id)

    assert breakdown.parsed == 3, "parsed counts ALL parsed rows, degraded included"
    assert breakdown.degraded == 2, "degraded is a sub-count of parsed, not a peer"
    assert breakdown.uploaded == 1


@pytest.mark.asyncio
async def test_status_breakdown_zero_degraded_when_none_fell_back(
    pg_pool: asyncpg.Pool,
) -> None:
    from src.services.resume_service import status_breakdown

    job_id = await _insert_job(pg_pool)
    await _insert_resume(
        pg_pool, job_id, status="parsed", parsed=_parsed_payload(degraded=False)
    )

    async with pg_pool.acquire() as conn:
        breakdown = await status_breakdown(conn, job_id)

    assert breakdown.parsed == 1
    assert breakdown.degraded == 0


@pytest.mark.asyncio
async def test_status_breakdown_old_row_null_parsed_does_not_count_as_degraded(
    pg_pool: asyncpg.Pool,
) -> None:
    """A row with ``parsed IS NULL`` (e.g. ``status='failed'``, never parsed)
    must not blow up the ``(parsed->>'degraded')::bool`` cast or be counted."""
    from src.services.resume_service import status_breakdown

    job_id = await _insert_job(pg_pool)
    await _insert_resume(pg_pool, job_id, status="failed", parsed=None)

    async with pg_pool.acquire() as conn:
        breakdown = await status_breakdown(conn, job_id)

    assert breakdown.failed == 1
    assert breakdown.degraded == 0
    assert breakdown.parsed == 0


# ── list_for_job: degraded surfaces on the list row itself ─────────────────


@pytest.mark.asyncio
async def test_list_for_job_surfaces_degraded_true_for_a_degraded_row(
    pg_pool: asyncpg.Pool,
) -> None:
    from src.services.resume_service import list_for_job

    job_id = await _insert_job(pg_pool, blind_review=False)
    await _insert_resume(
        pg_pool,
        job_id,
        status="parsed",
        parsed=_parsed_payload(degraded=True, reason=DEGRADED_REASON),
    )

    async with pg_pool.acquire() as conn:
        items = await list_for_job(conn, job_id=job_id)

    assert len(items) == 1
    assert items[0].degraded is True


@pytest.mark.asyncio
async def test_list_for_job_surfaces_degraded_false_for_a_clean_parse(
    pg_pool: asyncpg.Pool,
) -> None:
    from src.services.resume_service import list_for_job

    job_id = await _insert_job(pg_pool, blind_review=False)
    await _insert_resume(
        pg_pool, job_id, status="parsed", parsed=_parsed_payload(degraded=False)
    )

    async with pg_pool.acquire() as conn:
        items = await list_for_job(conn, job_id=job_id)

    assert items[0].degraded is False


@pytest.mark.asyncio
async def test_list_for_job_old_row_null_parsed_surfaces_degraded_false(
    pg_pool: asyncpg.Pool,
) -> None:
    """A pre-feature row (``parsed IS NULL``, e.g. still ``uploaded``) must
    not crash the list read and must read back as non-degraded."""
    from src.services.resume_service import list_for_job

    job_id = await _insert_job(pg_pool, blind_review=False)
    await _insert_resume(pg_pool, job_id, status="uploaded", parsed=None)

    async with pg_pool.acquire() as conn:
        items = await list_for_job(conn, job_id=job_id)

    assert len(items) == 1
    assert items[0].degraded is False


@pytest.mark.asyncio
async def test_list_for_job_blind_still_surfaces_degraded_non_pii_flag(
    pg_pool: asyncpg.Pool,
) -> None:
    """``degraded`` is NOT PII — it must survive blind-review masking, unlike
    ``candidate_name``."""
    from src.services.resume_service import list_for_job

    job_id = await _insert_job(pg_pool, blind_review=True)
    await _insert_resume(
        pg_pool,
        job_id,
        status="parsed",
        parsed=_parsed_payload(degraded=True, reason=DEGRADED_REASON),
    )

    async with pg_pool.acquire() as conn:
        items = await list_for_job(conn, job_id=job_id)

    assert items[0].candidate_name is None  # PII masked
    assert items[0].degraded is True  # non-PII flag survives


# ── get_one: degraded/degradation_reason carried under BOTH blind + reveal ──


@pytest.mark.asyncio
async def test_get_one_non_blind_carries_degraded_and_reason(
    pg_pool: asyncpg.Pool,
) -> None:
    from src.services.resume_service import get_one

    job_id = await _insert_job(pg_pool, blind_review=False)
    resume_id = await _insert_resume(
        pg_pool,
        job_id,
        status="parsed",
        parsed=_parsed_payload(degraded=True, reason=DEGRADED_REASON),
    )

    async with pg_pool.acquire() as conn:
        out = await get_one(conn, resume_id)

    assert out.blinded is False
    assert out.parsed is not None
    assert out.parsed.degraded is True
    assert out.parsed.degradation_reason == DEGRADED_REASON


@pytest.mark.asyncio
async def test_get_one_blind_still_carries_degraded_and_reason(
    pg_pool: asyncpg.Pool,
) -> None:
    """Blind masking hides identity (name/email/employers/schools/grad
    years) — ``degraded``/``degradation_reason`` are NOT PII and must survive
    untouched, exactly like ``withdrawn_at``/``withdrawal_reason``."""
    from src.services.resume_service import get_one

    job_id = await _insert_job(pg_pool, blind_review=True)
    resume_id = await _insert_resume(
        pg_pool,
        job_id,
        status="parsed",
        parsed=_parsed_payload(degraded=True, reason=DEGRADED_REASON),
    )

    async with pg_pool.acquire() as conn:
        out = await get_one(conn, resume_id)

    assert out.blinded is True
    assert out.candidate.name is None  # PII still masked
    assert out.parsed is not None
    assert out.parsed.degraded is True
    assert out.parsed.degradation_reason == DEGRADED_REASON


@pytest.mark.asyncio
async def test_get_one_reveal_true_carries_degraded_and_reason(
    pg_pool: asyncpg.Pool,
) -> None:
    from src.services.resume_service import get_one

    job_id = await _insert_job(pg_pool, blind_review=True)
    resume_id = await _insert_resume(
        pg_pool,
        job_id,
        status="parsed",
        parsed=_parsed_payload(degraded=True, reason=DEGRADED_REASON),
    )

    async with pg_pool.acquire() as conn:
        out = await get_one(conn, resume_id, reveal=True)

    assert out.blinded is False
    assert out.parsed is not None
    assert out.parsed.degraded is True
    assert out.parsed.degradation_reason == DEGRADED_REASON


@pytest.mark.asyncio
async def test_get_one_non_degraded_row_carries_degraded_false(
    pg_pool: asyncpg.Pool,
) -> None:
    from src.services.resume_service import get_one

    job_id = await _insert_job(pg_pool, blind_review=False)
    resume_id = await _insert_resume(
        pg_pool, job_id, status="parsed", parsed=_parsed_payload(degraded=False)
    )

    async with pg_pool.acquire() as conn:
        out = await get_one(conn, resume_id)

    assert out.parsed is not None
    assert out.parsed.degraded is False
    assert out.parsed.degradation_reason is None
