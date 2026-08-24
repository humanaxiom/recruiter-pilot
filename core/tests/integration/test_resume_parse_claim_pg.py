"""Integration test — resume_service.claim_parsing + the retries-exhausted
end state (FU-7, ADR-021 §3).

Real Postgres (testcontainers), same conventions as
``test_parse_resume_e2e.py``: only the LLM/embedder are mocked.

``src.services.resume_service.claim_parsing`` and the ``LLMUnavailableError``
retry/fail boundary inside ``parse_resume`` do not exist yet — this is the
RED half of the TDD cycle. Every test below either calls the not-yet-existing
``claim_parsing`` directly (``AttributeError``) or drives ``parse_resume``
into a code path that today lets ``LLMUnavailableError`` propagate uncaught
instead of resolving to a real ``status='failed'`` row.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest
from testcontainers.postgres import PostgresContainer

from src.models.ddl import init_schema
from src.pipeline.llm import LLMUnavailableError
from src.pipeline.parsing import MIME_TXT
from src.services import resume_service
from src.settings import Settings
from src.storage.blob_store import BlobStore
from src.worker.resume_tasks import parse_resume

# Same shape as PII_KEY in test_parse_resume_e2e.py / test_schema.py — any
# non-empty string works as a pgp_sym_encrypt passphrase.
PII_KEY = "b3VyLTMyLWJ5dGUtcGlpLWtleS1mb3ItdGVzdHM="

RESUME_TEXT = "Summary\nExperienced backend engineer.\n"

# A résumé body that actually produces chunks — REQUIRED for any test that must
# reach the LLM/embed boundary (the short RESUME_TEXT above yields 0 chunks, so
# parse_resume returns "failed" at the `if not chunks:` guard BEFORE the LLM is
# ever called; a test using it cannot prove the retries-exhausted transition).
# Mirrors test_parse_resume_e2e.py's chunk-producing body.
_CHUNKING_RESUME_TEXT = """Summary
Experienced backend engineer with a distributed systems background, six
years building services that stay up at 3am so nobody has to page anyone.

Experience
Senior Software Engineer at Acme Corp
2019 - Present
Built distributed systems in Python and Kubernetes, ran the on-call
rotation, and mentored two junior engineers onto the platform team.

Education
BSc Computer Science, Example University, 2015

Skills
Python, Kubernetes, PostgreSQL, Redis
"""


async def _insert_job(pool: asyncpg.Pool) -> uuid.UUID:
    async with pool.acquire() as conn:
        job_id: uuid.UUID = await conn.fetchval(
            "INSERT INTO jobs (title, description_raw) VALUES ($1, $2) RETURNING id",
            "Senior Backend Engineer",
            "Build the ranking pipeline end to end.",
        )
    return job_id


async def _insert_resume(
    pool: asyncpg.Pool,
    job_id: uuid.UUID,
    *,
    blob_key: str,
    status: str = "uploaded",
) -> uuid.UUID:
    data = RESUME_TEXT.encode("utf-8")
    async with pool.acquire() as conn:
        resume_id: uuid.UUID = await conn.fetchval(
            """
            INSERT INTO resumes (
                job_id, blob_key, original_filename, mime_type,
                file_size_bytes, sha256, consent_acknowledged, status
            ) VALUES ($1, $2, 'ada-lovelace.txt', $3, $4, $5, TRUE, $6)
            RETURNING id
            """,
            job_id,
            blob_key,
            MIME_TXT,
            len(data),
            hashlib.sha256(data).hexdigest(),
            status,
        )
    return resume_id


@pytest.fixture(scope="session")
def pg_dsn() -> Iterator[str]:
    with PostgresContainer("postgres:16-alpine") as pg:
        # asyncpg needs the raw DSN, not SQLAlchemy's postgresql+driver:// form.
        yield re.sub(r"\+\w+", "", pg.get_connection_url(), count=1)


@pytest.fixture
async def pg_pool(pg_dsn: str) -> AsyncIterator[asyncpg.Pool]:
    pool = await asyncpg.create_pool(dsn=pg_dsn, min_size=1, max_size=5)
    await init_schema(pool)
    async with pool.acquire() as conn:
        await conn.execute("TRUNCATE jobs, outbox CASCADE")
    try:
        yield pool
    finally:
        await pool.close()


@pytest.fixture
def blob_store(tmp_path: Path) -> BlobStore:
    return BlobStore(tmp_path / "blobs")


# ── Decision 1: claim_parsing at a real Postgres level ──────────────────────


@pytest.mark.asyncio
async def test_claim_parsing_flips_uploaded_row_to_parsing(
    pg_pool: asyncpg.Pool,
) -> None:
    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume(
        pg_pool, job_id, blob_key="resumes/a.txt", status="uploaded"
    )

    async with pg_pool.acquire() as conn:
        claimed = await resume_service.claim_parsing(conn, resume_id)

    assert claimed is True
    async with pg_pool.acquire() as conn:
        status = await conn.fetchval(
            "SELECT status FROM resumes WHERE id = $1", resume_id
        )
    assert status == "parsing"


@pytest.mark.asyncio
async def test_claim_parsing_second_call_returns_false_status_stays_parsing(
    pg_pool: asyncpg.Pool,
) -> None:
    """The retry contract: a SECOND claim against an already-'parsing' row
    returns False (0 rows affected) and must not disturb the row at all."""
    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume(
        pg_pool, job_id, blob_key="resumes/b.txt", status="uploaded"
    )

    async with pg_pool.acquire() as conn:
        first = await resume_service.claim_parsing(conn, resume_id)
    async with pg_pool.acquire() as conn:
        second = await resume_service.claim_parsing(conn, resume_id)

    assert first is True
    assert second is False
    async with pg_pool.acquire() as conn:
        status = await conn.fetchval(
            "SELECT status FROM resumes WHERE id = $1", resume_id
        )
    assert status == "parsing"


@pytest.mark.asyncio
async def test_claim_parsing_does_not_reopen_a_terminal_row(
    pg_pool: asyncpg.Pool,
) -> None:
    """The guarded UPDATE's WHERE clause is ``status = 'uploaded'`` only — a
    row that already reached a TERMINAL state ('parsed') must never be
    silently reopened by a stray/duplicate enqueue."""
    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume(
        pg_pool, job_id, blob_key="resumes/c.txt", status="parsed"
    )

    async with pg_pool.acquire() as conn:
        claimed = await resume_service.claim_parsing(conn, resume_id)

    assert claimed is False
    async with pg_pool.acquire() as conn:
        status = await conn.fetchval(
            "SELECT status FROM resumes WHERE id = $1", resume_id
        )
    assert status == "parsed"


# ── Decision 2: retries-exhausted -> failed, real Postgres end state ────────


@pytest.mark.asyncio
async def test_llm_unavailable_exhausted_retries_ends_failed_with_reason(
    pg_pool: asyncpg.Pool, blob_store: BlobStore
) -> None:
    """A résumé whose LLM calls ALWAYS raise ``LLMUnavailableError``, run on
    the LAST configured try (``ctx["job_try"] == resume_parse_max_tries``),
    must end as a REAL ``status='failed'`` Postgres row with a non-empty
    ``failure_reason`` — not stranded 'parsing'/'uploaded' forever, and not
    an uncaught exception that crashes the worker process."""
    job_id = await _insert_job(pg_pool)
    # Chunk-producing body so parse_resume REACHES the LLM boundary (the short
    # RESUME_TEXT would return "failed" at the no-chunks guard first, never
    # exercising the retries-exhausted transition this test exists to prove).
    data = _CHUNKING_RESUME_TEXT.encode("utf-8")
    blob_key = f"resumes/{uuid.uuid4().hex}.txt"
    await blob_store.put(blob_key, data)
    resume_id = await _insert_resume(
        pg_pool, job_id, blob_key=blob_key, status="uploaded"
    )

    llm = MagicMock(chat_json=AsyncMock(side_effect=LLMUnavailableError("ollama down")))
    embedder = MagicMock(embed=AsyncMock())
    ctx: dict[str, Any] = {
        "pg_pool": pg_pool,
        "llm": llm,
        "embedder": embedder,
        "blob_store": blob_store,
        # The configured max — the LAST try, so parse_resume must give up
        # rather than raise arq.Retry.
        "job_try": 5,
    }

    with (
        patch(
            "src.services.pii.get_settings",
            return_value=SimpleNamespace(pii_key=PII_KEY),
        ),
        patch(
            "src.worker.resume_tasks.get_settings",
            return_value=Settings(resume_parse_max_tries=5),
        ),
    ):
        result = await parse_resume(ctx, str(resume_id))

    assert result == "failed"
    # The LLM boundary was actually REACHED (guards against the vacuous
    # no-chunks path that would return "failed" before any LLM call).
    assert llm.chat_json.await_count > 0, (
        "the test must exercise the LLMUnavailableError boundary — if the LLM "
        "was never called, parse_resume failed for some OTHER reason and this "
        "test proves nothing about the retries-exhausted transition"
    )
    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status, failure_reason FROM resumes WHERE id = $1", resume_id
        )
    assert row is not None
    assert row["status"] == "failed"
    assert row["failure_reason"], (
        "a real row left status='failed' after exhausted retries must carry "
        "a non-empty failure_reason — a NULL reason here is the exact "
        "'stranded row, no diagnostic' regression this feature fixes"
    )
    # ...and the reason must be the RETRIES-EXHAUSTED one, not some other
    # failure — pins that we came through the LLMUnavailableError last-try path.
    assert "retr" in row["failure_reason"].lower(), (
        f"failure_reason should name the exhausted-retries cause, got: "
        f"{row['failure_reason']!r}"
    )
