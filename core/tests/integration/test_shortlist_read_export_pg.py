"""Integration tests — Phase 5's shortlist read + export
(``src.services.shortlist_service.list_for_job`` / ``get_one`` /
``export_rows``) against a REAL Postgres (testcontainers), mirroring
``test_shortlist_persistence_pg.py``'s ``pg_dsn``/``pg_pool`` fixtures and
``test_pii_encryption.py``'s ``patched_pii_key`` pattern.

None of ``list_for_job`` / ``get_one`` / ``export_rows`` exist yet — every
test below fails at collection time with ``ModuleNotFoundError``. RED half
of the TDD cycle.

What a REAL Postgres proves that the mocked-conn unit tests
(``test_services_shortlist_read.py`` / ``test_services_shortlist_export.py``)
cannot:

* the ScoreBreakdown fold fix works against the REAL jsonb codec (asyncpg
  hands back a JSON *string* for a jsonb column by default — no custom codec
  is registered anywhere in this project, see ``src/models/pool.py``) —
  round-tripping through a real INSERT via ``persist_shortlist`` (4d) is the
  only way to prove the read layer parses what Postgres ACTUALLY returns,
  not what a hand-built mock assumes it returns.
* the blind-review branch's real SQL (join to ``resumes``, real
  ``pgp_sym_decrypt``) resolves the right rows and the right redaction inputs
  against real encrypted PII.
* ``export_rows`` genuinely NEEDS ``set_pii_key`` to have been called first —
  a real ``current_setting('app.pii_key')`` STRICT read raises
  ``asyncpg.PostgresError`` when it wasn't, which no mock can prove.
"""

from __future__ import annotations

import datetime as dt
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
from src.pipeline.matching.orchestrator import ShortlistResult, ShortlistResultEntry
from src.schemas.matching import (
    DEFAULT_WEIGHTS,
    EvidenceObject,
    PipelineMeta,
    ScoreBreakdown,
)
from src.services.pii import encrypt as pii_encrypt
from src.services.pii import set_pii_key

TEST_PII_KEY = "integration-test-pii-key-do-not-use-in-prod"

_INSERT_JOB_SQL = (
    "INSERT INTO jobs (title, description_raw, description_parsed, blind_review) "
    "VALUES ($1, $2, $3::jsonb, $4) RETURNING id"
)


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
    """Point every module's ``get_settings`` used by ``src.services.pii`` at a
    fixed test key, so PII encrypt/decrypt is deterministic without depending
    on an env-supplied ``PII_KEY``."""
    fake_settings = SimpleNamespace(pii_key=TEST_PII_KEY)
    with patch("src.services.pii.get_settings", return_value=fake_settings):
        yield


# ── seeding helpers ──────────────────────────────────────────────────────


async def _insert_job(
    pool: asyncpg.Pool,
    *,
    blind_review: bool = True,
    description_parsed: dict[str, Any] | None = None,
) -> UUID:
    parsed_json = (
        json.dumps(description_parsed) if description_parsed is not None else None
    )
    async with pool.acquire() as conn:
        job_id: UUID = await conn.fetchval(
            _INSERT_JOB_SQL,
            "Senior Backend Engineer",
            "raw jd text (irrelevant to these tests)",
            parsed_json,
            blind_review,
        )
    return job_id


async def _insert_resume_with_pii(
    pool: asyncpg.Pool,
    job_id: UUID,
    *,
    name: str | None,
    email: str | None = None,
    phone: str | None = None,
    parsed: dict[str, Any] | None = None,
    original_filename: str = "resume.pdf",
) -> UUID:
    async with pool.acquire() as conn:
        resume_id: UUID = await conn.fetchval(
            """
            INSERT INTO resumes (
                job_id, blob_key, original_filename, mime_type,
                file_size_bytes, sha256, consent_acknowledged, status, parsed
            ) VALUES ($1, $2, $3, 'application/pdf', 1024, $4, TRUE,
                       'parsed', $5::jsonb)
            RETURNING id
            """,
            job_id,
            f"resumes/{uuid.uuid4().hex}.pdf",
            original_filename,
            uuid.uuid4().hex,
            json.dumps(parsed) if parsed is not None else None,
        )
        async with conn.transaction():
            await set_pii_key(conn)
            name_enc = await pii_encrypt(conn, name)
            email_enc = await pii_encrypt(conn, email)
            phone_enc = await pii_encrypt(conn, phone)
            await conn.execute(
                "UPDATE resumes SET candidate_name = $2, candidate_email = $3, "
                "candidate_phone = $4 WHERE id = $1",
                resume_id,
                name_enc,
                email_enc,
                phone_enc,
            )
    return resume_id


def _breakdown() -> ScoreBreakdown:
    return ScoreBreakdown(
        skill=0.8,
        experience=0.6,
        education=0.4,
        seniority=0.5,
        vector=0.3,
        structured=0.55,
    )


def _meta() -> PipelineMeta:
    return PipelineMeta(
        model_gen="test-gen",
        model_emb="test-emb",
        prompt_versions={"shortlist_evidence": "shortlist_evidence_v1"},
        weights=DEFAULT_WEIGHTS,
        git_sha="abcdef0123456789",
        generated_at=dt.datetime.now(dt.UTC),
        timings_ms={},
    )


def _real_evidence(name: str) -> EvidenceObject:
    return EvidenceObject(
        requirements=[
            {
                "requirement": "Python",
                "status": "met",
                "evidence": f"{name} led the platform migration.",
                "evidence_chunk_ids": ["c_001"],
                "confidence": 0.9,
            }
        ],
        overall_summary=f"{name} is a strong candidate.",
    )


# ── test 26: full round trip through the real ScoreBreakdown fold ──────────


@pytest.mark.asyncio
async def test_score_breakdown_fold_survives_a_real_round_trip(
    pg_pool: asyncpg.Pool,
) -> None:
    """persist_shortlist (4d) folds score_structured/score_evidence INTO the
    real jsonb column; list_for_job must read it back without a
    ValidationError against the REAL Postgres jsonb codec (a str, not a
    pre-parsed dict — see the module docstring)."""
    from src.services.shortlist_service import list_for_job, persist_shortlist

    job_id = await _insert_job(pg_pool, blind_review=False)
    resume_id = await _insert_resume_with_pii(pg_pool, job_id, name=None)

    entry = ShortlistResultEntry(
        resume_id=resume_id,
        rank=1,
        score_final=0.85,
        score_structured=0.8,
        score_evidence=0.7,
        breakdown=_breakdown(),
        evidence=_real_evidence("the candidate"),
    )
    async with pg_pool.acquire() as conn:
        await persist_shortlist(
            conn, ShortlistResult(job_id=job_id, entries=[entry], pipeline_meta=_meta())
        )

    async with pg_pool.acquire() as conn:
        entries = await list_for_job(conn, job_id=job_id)

    assert len(entries) == 1
    got = entries[0]
    assert isinstance(got.score_breakdown, ScoreBreakdown)
    assert got.score_breakdown.skill == pytest.approx(0.8)
    assert got.score_breakdown.structured == pytest.approx(0.55)
    assert got.evidence is not None
    assert "the candidate" in got.evidence.requirements[0].evidence


@pytest.mark.asyncio
async def test_score_breakdown_fold_survives_get_one_round_trip(
    pg_pool: asyncpg.Pool,
) -> None:
    from src.services.shortlist_service import get_one, list_for_job, persist_shortlist

    job_id = await _insert_job(pg_pool, blind_review=False)
    resume_id = await _insert_resume_with_pii(pg_pool, job_id, name=None)

    entry = ShortlistResultEntry(
        resume_id=resume_id,
        rank=1,
        score_final=0.85,
        score_structured=0.8,
        score_evidence=0.7,
        breakdown=_breakdown(),
        evidence=None,
    )
    async with pg_pool.acquire() as conn:
        await persist_shortlist(
            conn, ShortlistResult(job_id=job_id, entries=[entry], pipeline_meta=_meta())
        )

    async with pg_pool.acquire() as conn:
        [listed] = await list_for_job(conn, job_id=job_id)

    async with pg_pool.acquire() as conn:
        got = await get_one(conn, listed.id)

    assert isinstance(got.score_breakdown, ScoreBreakdown)
    assert got.score_breakdown.skill == pytest.approx(0.8)
    # evidence=None on the write path -> {} on read (ADR-010 §2 residual)
    assert got.evidence is not None
    assert got.evidence.requirements == []


# ── test 27: blind_review branch against real rows ──────────────────────


@pytest.mark.asyncio
async def test_blind_review_masks_identity_against_real_encrypted_pii(
    pg_pool: asyncpg.Pool,
) -> None:
    from src.services.shortlist_service import list_for_job, persist_shortlist

    job_id = await _insert_job(pg_pool, blind_review=True)
    parsed = {
        "candidate": {
            "name": "Jane Smith",
            "email": "jane.smith@example.test",
            "phone": "604-555-0192",
            "location": None,
        },
        "summary": "",
        "total_years_experience": 0,
        "skills": [],
        "experience": [{"company": "Acme Corp", "title": "Engineer", "bullets": []}],
        "education": [],
        "chunks": [],
        "cover_letter_chunks": [],
    }
    resume_id = await _insert_resume_with_pii(
        pg_pool,
        job_id,
        name="Jane Smith",
        email="jane.smith@example.test",
        phone="604-555-0192",
        parsed=parsed,
    )

    entry = ShortlistResultEntry(
        resume_id=resume_id,
        rank=1,
        score_final=0.85,
        score_structured=0.8,
        score_evidence=0.7,
        breakdown=_breakdown(),
        evidence=_real_evidence("Jane Smith"),
    )
    async with pg_pool.acquire() as conn:
        await persist_shortlist(
            conn, ShortlistResult(job_id=job_id, entries=[entry], pipeline_meta=_meta())
        )

    async with pg_pool.acquire() as conn:
        entries = await list_for_job(conn, job_id=job_id)

    assert len(entries) == 1
    got = entries[0]
    assert got.blinded is True
    assert got.display_label == "Candidate A"
    assert got.evidence is not None
    assert "Jane Smith" not in got.evidence.requirements[0].evidence
    assert "Acme Corp" not in got.evidence.requirements[0].evidence


@pytest.mark.asyncio
async def test_non_blind_review_shows_real_evidence_against_real_rows(
    pg_pool: asyncpg.Pool,
) -> None:
    from src.services.shortlist_service import list_for_job, persist_shortlist

    job_id = await _insert_job(pg_pool, blind_review=False)
    resume_id = await _insert_resume_with_pii(
        pg_pool, job_id, name="Jane Smith", email="jane.smith@example.test"
    )

    entry = ShortlistResultEntry(
        resume_id=resume_id,
        rank=1,
        score_final=0.85,
        score_structured=0.8,
        score_evidence=0.7,
        breakdown=_breakdown(),
        evidence=_real_evidence("Jane Smith"),
    )
    async with pg_pool.acquire() as conn:
        await persist_shortlist(
            conn, ShortlistResult(job_id=job_id, entries=[entry], pipeline_meta=_meta())
        )

    async with pg_pool.acquire() as conn:
        entries = await list_for_job(conn, job_id=job_id)

    assert entries[0].blinded is False
    assert entries[0].display_label is None
    got_evidence = entries[0].evidence
    assert got_evidence is not None
    assert "Jane Smith" in got_evidence.requirements[0].evidence


# ── test 28: export_rows needs set_pii_key first, fails loud otherwise ─────


@pytest.mark.asyncio
async def test_export_rows_without_set_pii_key_raises_postgres_error(
    pg_pool: asyncpg.Pool,
) -> None:
    """The mutation-detecting proof: export_rows's SQL decrypts candidate PII
    inline via pgp_sym_decrypt(..., current_setting('app.pii_key')) — a
    STRICT read with no missing_ok. Calling it on a fresh connection/
    transaction that never called set_pii_key MUST raise
    asyncpg.PostgresError, never silently return NULL/empty identity."""
    from src.services.shortlist_service import export_rows, persist_shortlist

    job_id = await _insert_job(pg_pool, blind_review=False)
    resume_id = await _insert_resume_with_pii(pg_pool, job_id, name="Jane Smith")
    entry = ShortlistResultEntry(
        resume_id=resume_id,
        rank=1,
        score_final=0.85,
        score_structured=0.8,
        score_evidence=0.7,
        breakdown=_breakdown(),
        evidence=None,
    )
    async with pg_pool.acquire() as conn:
        await persist_shortlist(
            conn, ShortlistResult(job_id=job_id, entries=[entry], pipeline_meta=_meta())
        )

    with pytest.raises(asyncpg.PostgresError):
        async with pg_pool.acquire() as conn:
            await export_rows(conn, job_id=job_id, reveal=True)


@pytest.mark.asyncio
async def test_export_rows_with_set_pii_key_decrypts_real_identity(
    pg_pool: asyncpg.Pool,
) -> None:
    from src.services.shortlist_service import export_rows, persist_shortlist

    job_id = await _insert_job(pg_pool, blind_review=False)
    resume_id = await _insert_resume_with_pii(
        pg_pool,
        job_id,
        name="Jane Smith",
        email="jane.smith@example.test",
        phone="604-555-0192",
    )
    entry = ShortlistResultEntry(
        resume_id=resume_id,
        rank=1,
        score_final=0.85,
        score_structured=0.8,
        score_evidence=0.7,
        breakdown=_breakdown(),
        evidence=_real_evidence("Jane Smith"),
    )
    async with pg_pool.acquire() as conn:
        await persist_shortlist(
            conn, ShortlistResult(job_id=job_id, entries=[entry], pipeline_meta=_meta())
        )

    async with pg_pool.acquire() as conn, conn.transaction():
        await set_pii_key(conn)
        rows = await export_rows(conn, job_id=job_id, reveal=True)

    assert len(rows) == 1
    assert rows[0]["candidate_name"] == "Jane Smith"
    assert rows[0]["candidate_email"] == "jane.smith@example.test"
    assert rows[0]["candidate_phone"] == "604-555-0192"


@pytest.mark.asyncio
async def test_export_rows_reveal_false_pseudonymizes_against_real_rows(
    pg_pool: asyncpg.Pool,
) -> None:
    from src.services.shortlist_service import export_rows, persist_shortlist

    job_id = await _insert_job(pg_pool, blind_review=False)
    resume_id = await _insert_resume_with_pii(pg_pool, job_id, name="Jane Smith")
    entry = ShortlistResultEntry(
        resume_id=resume_id,
        rank=1,
        score_final=0.85,
        score_structured=0.8,
        score_evidence=0.7,
        breakdown=_breakdown(),
        evidence=_real_evidence("Jane Smith"),
    )
    async with pg_pool.acquire() as conn:
        await persist_shortlist(
            conn, ShortlistResult(job_id=job_id, entries=[entry], pipeline_meta=_meta())
        )

    async with pg_pool.acquire() as conn, conn.transaction():
        await set_pii_key(conn)
        rows = await export_rows(conn, job_id=job_id, reveal=False)

    assert rows[0]["candidate_name"] == "Candidate A"
    assert "Jane Smith" not in rows[0]["evidence"]["requirements"][0]["evidence"]


@pytest.mark.asyncio
async def test_export_rows_reveal_false_redacts_identifying_filename_real_rows(
    pg_pool: asyncpg.Pool,
) -> None:
    """A résumé named after its candidate leaks identity through the blind
    export — reveal=False must mask ``original_filename`` to ``resume.pdf``."""
    from src.services.shortlist_service import export_rows, persist_shortlist

    job_id = await _insert_job(pg_pool, blind_review=False)
    resume_id = await _insert_resume_with_pii(
        pg_pool,
        job_id,
        name="Jane Smith",
        original_filename="Zzyzxqrst_Wibblesworth_CV.pdf",
    )
    entry = ShortlistResultEntry(
        resume_id=resume_id,
        rank=1,
        score_final=0.85,
        score_structured=0.8,
        score_evidence=0.7,
        breakdown=_breakdown(),
        evidence=_real_evidence("Jane Smith"),
    )
    async with pg_pool.acquire() as conn:
        await persist_shortlist(
            conn, ShortlistResult(job_id=job_id, entries=[entry], pipeline_meta=_meta())
        )

    async with pg_pool.acquire() as conn, conn.transaction():
        await set_pii_key(conn)
        rows = await export_rows(conn, job_id=job_id, reveal=False)

    assert rows[0]["original_filename"] == "resume.pdf"
