"""Integration tests — Phase 6's shortlist export route
(``GET /jobs/{job_id}/shortlist/export``) against a REAL Postgres
(testcontainers), reading a shortlist row that was ACTUALLY WRITTEN by
``src.services.shortlist_service.persist_shortlist`` (the Phase 4d write
path), plus a real ``pgp_sym_encrypt``-backed candidate so the export's
``pgp_sym_decrypt`` really runs.

``src.api.routes.shortlist`` does not exist yet — every test below fails at
collection or the first request. RED half of the TDD cycle.

What this proves that the mocked route tests
(``test_route_shortlist.py``) cannot: the ``ScoreBreakdown`` FOLD-POP guard
(ADR-011 §2) actually holds end to end through the HTTP layer — a REAL
``persist_shortlist`` write folds ``score_structured``/``score_evidence``
into ``score_breakdown`` jsonb (ADR-010 §2), and the export route must still
render a correct CSV/JSON row without ``ValidationError``ing on that exact
jsonb shape (only a mocked-conn test could accidentally paper over a missing
pop by handing back an already-correct-shaped fixture row).
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import re
from collections.abc import AsyncIterator, Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch
from uuid import UUID

import asyncpg
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from testcontainers.postgres import PostgresContainer

from src.api.deps import Role, resolve_role
from src.api.routes import shortlist as shortlist_routes
from src.errors import AppError
from src.models.ddl import init_schema
from src.models.pool import get_db
from src.pipeline.matching.orchestrator import ShortlistResult, ShortlistResultEntry
from src.schemas.matching import DEFAULT_WEIGHTS, PipelineMeta, ScoreBreakdown
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


async def _insert_job(pool: asyncpg.Pool, *, blind_review: bool = False) -> UUID:
    async with pool.acquire() as conn:
        job_id: UUID = await conn.fetchval(
            "INSERT INTO jobs (title, description_raw, blind_review) "
            "VALUES ($1, $2, $3) RETURNING id",
            "Senior Backend Engineer",
            "raw jd text (irrelevant to this test)",
            blind_review,
        )
    return job_id


async def _insert_resume(
    pool: asyncpg.Pool, job_id: UUID, *, name: str = "Jane Smith"
) -> UUID:
    async with pool.acquire() as conn:
        async with conn.transaction():
            await set_pii_key(conn)
            name_enc = await pii_encrypt(conn, name)
            resume_id: UUID = await conn.fetchval(
                """
                INSERT INTO resumes (
                    job_id, blob_key, original_filename, mime_type,
                    file_size_bytes, sha256, consent_acknowledged, status,
                    candidate_name
                ) VALUES ($1, $2, 'resume.pdf', 'application/pdf', 1024, $3,
                           TRUE, 'parsed', $4)
                RETURNING id
                """,
                job_id,
                f"resumes/{resume_id_hex()}.pdf",
                sha_for(name),
                name_enc,
            )
    return resume_id


def resume_id_hex() -> str:
    import uuid

    return uuid.uuid4().hex


def sha_for(seed: str) -> str:
    import hashlib

    return hashlib.sha256(seed.encode()).hexdigest()


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
        git_sha="abc123",
        generated_at=dt.datetime.now(dt.UTC),
        timings_ms={},
    )


def _build_app(pool: asyncpg.Pool) -> FastAPI:
    app = FastAPI()
    app.include_router(shortlist_routes.router)

    async def _get_db_override() -> AsyncIterator[Any]:
        async with pool.acquire() as conn:
            yield conn

    app.dependency_overrides[get_db] = _get_db_override
    app.dependency_overrides[resolve_role] = lambda: Role.ADMIN

    @app.exception_handler(AppError)
    async def _app_error_handler(_request: Any, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status, content={"detail": exc.message, "code": exc.code}
        )

    return app


async def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_export_csv_reads_a_row_actually_written_by_persist_shortlist(
    pg_pool: asyncpg.Pool,
) -> None:
    """The ScoreBreakdown fold-pop end-to-end proof (ADR-011 §2 through the
    HTTP layer): persist_shortlist folds score_structured/score_evidence
    into score_breakdown; the export route must still render a row without
    ValidationError-ing on that exact shape."""
    from src.services.shortlist_service import persist_shortlist

    job_id = await _insert_job(pg_pool, blind_review=False)
    resume_id = await _insert_resume(pg_pool, job_id, name="Jane Smith")

    result = ShortlistResult(
        job_id=job_id,
        entries=[
            ShortlistResultEntry(
                resume_id=resume_id,
                rank=1,
                score_final=0.91,
                score_structured=0.8,
                score_evidence=0.7,
                breakdown=_breakdown(),
                evidence=None,
            )
        ],
        pipeline_meta=_meta(),
    )
    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await persist_shortlist(conn, result)

    app = _build_app(pg_pool)
    async with await _client(app) as client:
        resp = await client.get(f"/jobs/{job_id}/shortlist/export?format=csv")
    assert resp.status_code == 200
    reader = csv.DictReader(io.StringIO(resp.text))
    rows = list(reader)
    assert len(rows) == 1
    # FU-4/D3: exports are blind-only; reveal parameter is retired
    assert "Jane Smith" not in resp.text
    # The row round-tripped through persist_shortlist with its real name
    # ("Jane Smith") intact, then got blind-redacted to the rank-1 pseudonym
    # by the export route — proving the round trip on candidate_name itself,
    # not just on score_final.
    assert rows[0]["candidate_name"] == "Candidate A"
    assert float(rows[0]["score_final"]) == pytest.approx(0.91)


@pytest.mark.asyncio
async def test_export_json_reveal_false_masks_the_real_candidate_name(
    pg_pool: asyncpg.Pool,
) -> None:
    from src.services.shortlist_service import persist_shortlist

    job_id = await _insert_job(pg_pool, blind_review=True)
    resume_id = await _insert_resume(pg_pool, job_id, name="Jane Smith")
    result = ShortlistResult(
        job_id=job_id,
        entries=[
            ShortlistResultEntry(
                resume_id=resume_id,
                rank=1,
                score_final=0.91,
                score_structured=0.8,
                score_evidence=0.7,
                breakdown=_breakdown(),
                evidence=None,
            )
        ],
        pipeline_meta=_meta(),
    )
    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await persist_shortlist(conn, result)

    app = _build_app(pg_pool)
    async with await _client(app) as client:
        resp = await client.get(f"/jobs/{job_id}/shortlist/export?format=json")
    assert resp.status_code == 200
    assert "Jane" not in resp.text
    assert "Smith" not in resp.text
