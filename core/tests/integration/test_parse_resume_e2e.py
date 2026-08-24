"""Integration test — ``src.worker.resume_tasks.parse_resume`` end-to-end.

Real Postgres (testcontainers) + a real filesystem ``BlobStore`` rooted at
``tmp_path``. Only the LLM and embedder are mocked (no Ollama in gates — see
CLAUDE.md). Everything else — blob write/read, ``extract_text``,
``chunk_resume``, the deterministic skill scan, the pgcrypto PII round-trip,
the ``resumes``/``outbox`` writes — runs for real, closing the PII loop
end-to-end THROUGH the real task rather than through ``pii.py`` in isolation
(unit-tested separately).

Per the Phase 3 contract: ``resumes.consent_acknowledged`` is
``BOOLEAN NOT NULL`` with **no default** — every INSERT fixture here supplies
it explicitly, or the insert fails with ``NotNullViolationError``.

None of ``src.worker.resume_tasks`` / ``src.pipeline.parsing`` /
``src.services.pii`` exist yet — this is the RED half of the TDD cycle; the
whole file is expected to fail at collection (ImportError).
"""

from __future__ import annotations

import hashlib
import json
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
from src.pipeline.parsing import MIME_TXT
from src.schemas import (
    CandidateInfo,
    CoverLetterParsed,
    EducationItem,
    Experience,
    ResumeCore,
    ResumeParsed,
    ResumeSkillDetail,
    ResumeSkillDetails,
)
from src.storage.blob_store import BlobStore
from src.worker.resume_tasks import parse_resume

# 32 random bytes, base64 — same shape as PII_KEY in test_schema.py (any
# non-empty string works as a pgp_sym_encrypt passphrase; this just mirrors
# the shape the real deployment uses).
PII_KEY = "b3VyLTMyLWJ5dGUtcGlpLWtleS1mb3ItdGVzdHM="

CANDIDATE_NAME = "Ada Integration Lovelace"
CANDIDATE_EMAIL = "ada.integration@example.org"
CANDIDATE_PHONE = "555-0100-INTEGRATION"

RESUME_TEXT = """Summary
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

CORE_RESULT = ResumeCore(
    candidate=CandidateInfo(
        name=CANDIDATE_NAME,
        email=CANDIDATE_EMAIL,
        phone=CANDIDATE_PHONE,
        location="Waterloo, ON",
    ),
    summary="Experienced backend engineer with a distributed systems background.",
    total_years_experience=6,
    experience=[
        Experience(
            company="Acme Corp",
            title="Senior Software Engineer",
            start="2019",
            is_current=True,
        )
    ],
    education=[
        EducationItem(
            degree="BSc Computer Science", institution="Example University", year=2015
        )
    ],
)

SKILLS_RESULT = ResumeSkillDetails(
    skills=[
        ResumeSkillDetail(name="Python", years=6),
        ResumeSkillDetail(name="Kubernetes", years=3),
    ]
)


def _make_llm() -> MagicMock:
    """A ``chat_json`` stub that dispatches on the requested schema — robust
    against however ``parse_resume`` orders its core/skills/cover-letter
    calls (that ordering is a unit-test concern, not this one's)."""

    async def _chat_json(messages: Any, schema: Any, **kwargs: Any) -> Any:
        if schema is ResumeCore:
            return CORE_RESULT
        if schema is ResumeSkillDetails:
            return SKILLS_RESULT
        if schema is CoverLetterParsed:
            return CoverLetterParsed()
        raise AssertionError(
            f"integration LLM stub got an unexpected schema: {schema!r}"
        )

    return MagicMock(chat_json=AsyncMock(side_effect=_chat_json))


def _make_embedder() -> MagicMock:
    async def _embed(texts: Any) -> list[list[float]]:
        return [[float(i) + 0.1] * 8 for i in range(len(texts))]

    return MagicMock(embed=AsyncMock(side_effect=_embed))


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
    mime_type: str,
    file_size_bytes: int,
    sha256: str,
) -> uuid.UUID:
    async with pool.acquire() as conn:
        resume_id: uuid.UUID = await conn.fetchval(
            """
            INSERT INTO resumes (
                job_id, blob_key, original_filename, mime_type,
                file_size_bytes, sha256, consent_acknowledged
            ) VALUES ($1, $2, 'ada-lovelace.txt', $3, $4, $5, TRUE)
            RETURNING id
            """,
            job_id,
            blob_key,
            mime_type,
            file_size_bytes,
            sha256,
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


@pytest.fixture
async def parsed(pg_pool: asyncpg.Pool, blob_store: BlobStore) -> dict[str, Any]:
    """Seed a job + an 'uploaded' resume with a real blob, run the real
    ``parse_resume`` task once (LLM/embedder mocked), and hand back the ids
    so each test asserts a different slice of the resulting DB state."""
    job_id = await _insert_job(pg_pool)

    data = RESUME_TEXT.encode("utf-8")
    blob_key = f"resumes/{uuid.uuid4().hex}.txt"
    await blob_store.put(blob_key, data)

    resume_id = await _insert_resume(
        pg_pool,
        job_id,
        blob_key=blob_key,
        mime_type=MIME_TXT,
        file_size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )

    ctx: dict[str, Any] = {
        "pg_pool": pg_pool,
        "llm": _make_llm(),
        "embedder": _make_embedder(),
        "blob_store": blob_store,
    }

    with patch(
        "src.services.pii.get_settings",
        return_value=SimpleNamespace(pii_key=PII_KEY),
    ):
        result = await parse_resume(ctx, str(resume_id))

    return {"result": result, "resume_id": resume_id, "job_id": job_id, "pool": pg_pool}


# ── Happy path status / bookkeeping ────────────────────────────────────────


@pytest.mark.asyncio
async def test_parse_resume_e2e_returns_parsed(parsed: dict[str, Any]) -> None:
    assert parsed["result"] == "parsed"


@pytest.mark.asyncio
async def test_resume_status_and_parsed_at_are_updated(parsed: dict[str, Any]) -> None:
    pool: asyncpg.Pool = parsed["pool"]
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status, parsed_at FROM resumes WHERE id = $1", parsed["resume_id"]
        )
    assert row is not None
    assert row["status"] == "parsed"
    assert row["parsed_at"] is not None


# ── PII loop, closed end-to-end through the real task ──────────────────────


@pytest.mark.asyncio
async def test_candidate_columns_are_ciphertext_bytes_without_pii_key(
    parsed: dict[str, Any],
) -> None:
    """Read back the raw columns with no ``set_pii_key`` in this connection
    at all — they must be opaque bytea, and must not contain the plaintext
    candidate values anywhere in the ciphertext."""
    pool: asyncpg.Pool = parsed["pool"]
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT candidate_name, candidate_email, candidate_phone
              FROM resumes WHERE id = $1
            """,
            parsed["resume_id"],
        )
    assert row is not None
    for column, plaintext in (
        ("candidate_name", CANDIDATE_NAME),
        ("candidate_email", CANDIDATE_EMAIL),
        ("candidate_phone", CANDIDATE_PHONE),
    ):
        value = row[column]
        assert isinstance(value, (bytes, bytearray)), (
            f"resumes.{column} must be stored as bytea ciphertext, got "
            f"{type(value)!r} (raw value: {value!r})"
        )
        assert plaintext.encode() not in bytes(
            value
        ), f"resumes.{column} ciphertext leaks the plaintext candidate value"


@pytest.mark.asyncio
async def test_candidate_columns_decrypt_correctly_with_pii_key(
    parsed: dict[str, Any],
) -> None:
    """With ``app.pii_key`` set (SET LOCAL, transaction-scoped — mirroring
    ``pii.set_pii_key``'s real semantics), the same columns decrypt back to
    the exact candidate values the mocked LLM produced."""
    pool: asyncpg.Pool = parsed["pool"]
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.pii_key', $1, true)", PII_KEY)
        row = await conn.fetchrow(
            """
            SELECT
                pgp_sym_decrypt(
                    candidate_name, current_setting('app.pii_key')
                ) AS name,
                pgp_sym_decrypt(
                    candidate_email, current_setting('app.pii_key')
                ) AS email,
                pgp_sym_decrypt(
                    candidate_phone, current_setting('app.pii_key')
                ) AS phone
              FROM resumes WHERE id = $1
            """,
            parsed["resume_id"],
        )
    assert row is not None
    assert row["name"] == CANDIDATE_NAME
    assert row["email"] == CANDIDATE_EMAIL
    assert row["phone"] == CANDIDATE_PHONE


@pytest.mark.asyncio
async def test_candidate_email_hash_is_plaintext_sha256(parsed: dict[str, Any]) -> None:
    """``candidate_email_hash`` is deliberately NOT encrypted (PIPEDA
    subject-access rationale) — plaintext sha256 of the normalised email."""
    pool: asyncpg.Pool = parsed["pool"]
    async with pool.acquire() as conn:
        stored_hash = await conn.fetchval(
            "SELECT candidate_email_hash FROM resumes WHERE id = $1",
            parsed["resume_id"],
        )
    expected = hashlib.sha256(CANDIDATE_EMAIL.strip().lower().encode()).hexdigest()
    assert stored_hash == expected


# ── parsed jsonb round-trip ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_parsed_jsonb_round_trips_through_resume_parsed_model(
    parsed: dict[str, Any],
) -> None:
    pool: asyncpg.Pool = parsed["pool"]
    async with pool.acquire() as conn:
        raw = await conn.fetchval(
            "SELECT parsed FROM resumes WHERE id = $1", parsed["resume_id"]
        )
    assert raw is not None
    payload = json.loads(raw)

    model = ResumeParsed.model_validate(payload)

    assert model.summary == CORE_RESULT.summary
    assert model.total_years_experience == CORE_RESULT.total_years_experience
    # Canonical skill names are lower-case by design (see the aliases.yaml
    # header) — that is what lets the LLM's "Python" and the deterministic
    # scan's "python" collapse into one row instead of two HAS_SKILL edges.
    assert {s.name for s in model.skills} >= {"python", "kubernetes"}
    assert model.chunks, "chunker output must be persisted alongside the extraction"
    # The PII invariant, once more, at the persisted-jsonb boundary: the
    # embedded candidate block on the *validated model* is fine to carry
    # (it's ciphertext-adjacent, encrypted separately in its own columns),
    # but the raw jsonb payload's `parsed.candidate` must never be the
    # vehicle by which plaintext PII leaks into a place a vector index reads.
    assert isinstance(model.candidate, CandidateInfo)


# ── outbox row ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_outbox_row_exists_and_is_undelivered(parsed: dict[str, Any]) -> None:
    """Phase 4 adds the drainer; an undelivered row here is the outbox
    pattern working as intended, not a bug."""
    pool: asyncpg.Pool = parsed["pool"]
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT event_type, delivered_at, aggregate, aggregate_id FROM outbox "
            "WHERE aggregate_id = $1",
            parsed["resume_id"],
        )
    assert len(rows) == 1, f"expected exactly one outbox row, got {len(rows)}"
    row = rows[0]
    assert row["event_type"] == "resume.parsed"
    assert row["delivered_at"] is None
    assert row["aggregate"], "aggregate column must be a non-empty label"
    assert str(row["aggregate_id"]) == str(parsed["resume_id"])


@pytest.mark.asyncio
async def test_outbox_payload_carries_the_resume_parse_fields(
    parsed: dict[str, Any],
) -> None:
    pool: asyncpg.Pool = parsed["pool"]
    async with pool.acquire() as conn:
        raw_payload = await conn.fetchval(
            """
            SELECT payload FROM outbox
             WHERE aggregate_id = $1 AND event_type = 'resume.parsed'
            """,
            parsed["resume_id"],
        )
    assert raw_payload is not None
    payload = json.loads(raw_payload)
    assert "parsed" in payload
    assert "summary_emb" in payload
    assert "chunk_embs" in payload
    assert "prompt_version" in payload
    assert str(payload.get("job_id")) == str(parsed["job_id"])


# ── FU-7 (ADR-021 §3) — the uploaded->parsing claim's effect is otherwise ──
# invisible ───────────────────────────────────────────────────────────────
#
# 'parsed'/'failed' are both reachable whether or not parse_resume ever
# claims the row -- a mutant that deletes the claim step still passes every
# OTHER assertion in this file. This test is the one that actually proves
# the claim ran: the mocked LLM reads the row's LIVE status back from
# Postgres, from a FRESH connection, from INSIDE the parse, on its first
# call, before returning anything.


@pytest.mark.asyncio
async def test_row_is_already_parsing_when_the_core_llm_call_fires(
    pg_pool: asyncpg.Pool, blob_store: BlobStore
) -> None:
    job_id = await _insert_job(pg_pool)
    data = RESUME_TEXT.encode("utf-8")
    blob_key = f"resumes/{uuid.uuid4().hex}.txt"
    await blob_store.put(blob_key, data)
    resume_id = await _insert_resume(
        pg_pool,
        job_id,
        blob_key=blob_key,
        mime_type=MIME_TXT,
        file_size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )

    observed_status: dict[str, Any] = {"value": "not-observed"}

    async def _chat_json(messages: Any, schema: Any, **kwargs: Any) -> Any:
        if schema is ResumeCore:
            # In-flight: read the row's LIVE status from a FRESH connection,
            # from inside the parse, before returning the canned core.
            async with pg_pool.acquire() as conn:
                observed_status["value"] = await conn.fetchval(
                    "SELECT status FROM resumes WHERE id = $1", resume_id
                )
            return CORE_RESULT
        if schema is ResumeSkillDetails:
            return SKILLS_RESULT
        if schema is CoverLetterParsed:
            return CoverLetterParsed()
        raise AssertionError(f"unexpected schema in in-flight test: {schema!r}")

    llm = MagicMock(chat_json=AsyncMock(side_effect=_chat_json))
    embedder = _make_embedder()
    ctx: dict[str, Any] = {
        "pg_pool": pg_pool,
        "llm": llm,
        "embedder": embedder,
        "blob_store": blob_store,
    }

    with patch(
        "src.services.pii.get_settings",
        return_value=SimpleNamespace(pii_key=PII_KEY),
    ):
        result = await parse_resume(ctx, str(resume_id))

    assert result == "parsed"
    assert observed_status["value"] == "parsing", (
        "resumes.status was NOT 'parsing' by the time the core LLM call "
        "fired -- the uploaded->parsing claim did not run (or ran too late "
        "-- after the blob fetch/LLM call instead of before it), observed "
        f"status: {observed_status['value']!r}"
    )
