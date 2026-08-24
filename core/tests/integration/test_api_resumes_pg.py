"""Integration tests — Phase 6's résumé upload route against a REAL Postgres
(testcontainers), a REAL filesystem ``BlobStore`` rooted at a temp dir, and a
FAKE ``ArqRedis`` recording ``enqueue_job`` calls.

``src.api.routes.resumes`` does not exist yet — every test below fails at
collection or the first request. RED half of the TDD cycle.

What a REAL Postgres + REAL BlobStore prove that the mocked route tests
(``test_route_resumes.py``) cannot:

* the pasted ``cover_letter_text`` round-trips through REAL
  ``pgp_sym_encrypt``/``pgp_sym_decrypt`` under ``app.pii_key`` — not just
  "encrypt was called".
* a real zip upload writes N real blob files to disk, each under a
  SERVER-GENERATED key (never the zip entry's own name), and the DDL's
  ``UNIQUE (job_id, sha256)`` constraint is what a real duplicate-file zip
  entry collides against.
* a résumé uploaded against a ``job_id`` with no matching ``jobs`` row is
  rejected with a clean 404, not left to surface the real
  ``asyncpg.exceptions.ForeignKeyViolationError`` the ``resumes_job_id_fkey``
  constraint raises on the INSERT — diagnosed live, see
  ``test_upload_to_nonexistent_job_returns_404_not_500`` below. A mocked
  connection (``test_route_resumes.py``) cannot reproduce the FK violation
  itself, only whatever pre-check a fix chooses to add; only a real Postgres
  proves the crash this test pins is actually gone.
"""

from __future__ import annotations

import re
import zipfile
from collections.abc import AsyncIterator, Iterator
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import asyncpg
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from testcontainers.postgres import PostgresContainer

from src.api.deps import Role, get_arq, resolve_role
from src.api.routes import resumes as resumes_routes
from src.errors import AppError
from src.models.ddl import init_schema
from src.models.pool import get_db
from src.services.pii import decrypt as pii_decrypt
from src.services.pii import set_pii_key
from src.storage.blob_store import BlobStore, get_blob_store

TEST_PII_KEY = "integration-test-pii-key-do-not-use-in-prod"

_PDF_MAGIC = b"%PDF-1.4\nresume content\n" + b"x" * 500


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


async def _insert_job(pool: asyncpg.Pool) -> str:
    async with pool.acquire() as conn:
        job_id = await conn.fetchval(
            "INSERT INTO jobs (title, description_raw) VALUES ($1, $2) RETURNING id",
            "Senior Backend Engineer",
            "raw jd text",
        )
    return str(job_id)


def _build_app(pool: asyncpg.Pool, store: BlobStore, *, arq: MagicMock) -> FastAPI:
    app = FastAPI()
    app.include_router(resumes_routes.router)

    async def _get_db_override() -> AsyncIterator[Any]:
        async with pool.acquire() as conn:
            yield conn

    app.dependency_overrides[get_db] = _get_db_override
    app.dependency_overrides[get_blob_store] = lambda: store
    app.dependency_overrides[get_arq] = lambda: arq
    app.dependency_overrides[resolve_role] = lambda: Role.ADMIN

    @app.exception_handler(AppError)
    async def _app_error_handler(_request: Any, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status, content={"detail": exc.message, "code": exc.code}
        )

    return app


async def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _zip_bytes(entries: list[tuple[str, bytes]]) -> bytes:
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
        for name, content in entries:
            zf.writestr(zipfile.ZipInfo(filename=name), content)
    return buf.getvalue()


@pytest.mark.asyncio
async def test_pasted_cover_letter_round_trips_through_real_pgcrypto(
    pg_pool: asyncpg.Pool, tmp_path: Path
) -> None:
    job_id = await _insert_job(pg_pool)
    store = BlobStore(str(tmp_path))
    arq = MagicMock(enqueue_job=AsyncMock())
    app = _build_app(pg_pool, store, arq=arq)
    cover_text = "Dear hiring manager, I am thrilled to apply for this role."

    async with await _client(app) as client:
        resp = await client.post(
            f"/jobs/{job_id}/resumes",
            files=[("files", ("resume.pdf", _PDF_MAGIC, "application/pdf"))],
            data={
                "consent_acknowledged": "true",
                "cover_letter_text": cover_text,
            },
        )
    assert resp.status_code == 202
    resume_id = resp.json()[0]["resume_id"]

    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await set_pii_key(conn)
            ciphertext = await conn.fetchval(
                "SELECT cover_letter_text FROM resumes WHERE id = $1", resume_id
            )
            assert ciphertext is not None
            assert cover_text.encode() not in ciphertext
            decrypted = await pii_decrypt(conn, ciphertext)
    assert decrypted == cover_text


@pytest.mark.asyncio
async def test_real_zip_upload_writes_n_blobs_with_server_generated_keys(
    pg_pool: asyncpg.Pool, tmp_path: Path
) -> None:
    job_id = await _insert_job(pg_pool)
    store = BlobStore(str(tmp_path))
    arq = MagicMock(enqueue_job=AsyncMock())
    app = _build_app(pg_pool, store, arq=arq)
    archive = _zip_bytes(
        [
            ("alice_resume.pdf", _PDF_MAGIC + b"1"),
            ("bob_resume.pdf", _PDF_MAGIC + b"2"),
            ("carol_resume.pdf", _PDF_MAGIC + b"3"),
        ]
    )

    async with await _client(app) as client:
        resp = await client.post(
            f"/jobs/{job_id}/resumes",
            files=[("files", ("batch.zip", archive, "application/zip"))],
            data={"consent_acknowledged": "true"},
        )
    assert resp.status_code == 202
    assert len(resp.json()) == 3

    keys = await store.list_keys()
    assert len(keys) == 3
    for key in keys:
        assert "alice" not in key.lower()
        assert "bob" not in key.lower()
        assert "carol" not in key.lower()
        assert (tmp_path / key).exists()

    async with pg_pool.acquire() as conn:
        db_blob_keys = {
            r["blob_key"]
            for r in await conn.fetch(
                "SELECT blob_key FROM resumes WHERE job_id = $1", job_id
            )
        }
    assert db_blob_keys == set(keys)


# ── backend-robustness fix: upload against a nonexistent job ────────────
#
# Diagnosed live: POST /jobs/{job_id}/resumes with a valid file + consent but
# a job_id that does not exist currently crashes on the résumé INSERT with
# asyncpg.exceptions.ForeignKeyViolationError ("resumes_job_id_fkey"),
# surfacing as an unhandled 500. It must be a clean 404 instead, consistent
# with every other job-scoped route (e.g. GET /jobs/{job_id}).


@pytest.mark.asyncio
async def test_upload_to_nonexistent_job_returns_404_not_500(
    pg_pool: asyncpg.Pool, tmp_path: Path
) -> None:
    """The load-bearing test for this fix: only a REAL Postgres can raise the
    real FK violation this guards against — a mocked connection cannot
    reproduce it, only whatever pre-check a fix chooses to add."""
    missing_job_id = str(uuid4())
    store = BlobStore(str(tmp_path))
    arq = MagicMock(enqueue_job=AsyncMock())
    app = _build_app(pg_pool, store, arq=arq)

    async with await _client(app) as client:
        resp = await client.post(
            f"/jobs/{missing_job_id}/resumes",
            files=[("files", ("resume.pdf", _PDF_MAGIC, "application/pdf"))],
            data={"consent_acknowledged": "true"},
        )

    assert resp.status_code == 404
    assert resp.json()["code"] == "resource.not_found"
    arq.enqueue_job.assert_not_awaited()

    async with pg_pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT count(*) FROM resumes WHERE job_id = $1", missing_job_id
        )
    assert count == 0


@pytest.mark.asyncio
async def test_upload_to_nonexistent_job_writes_no_blob(
    pg_pool: asyncpg.Pool, tmp_path: Path
) -> None:
    """No orphaned blob file either — the whole request is rejected before
    any I/O against the blob store, not merely before the DB commit."""
    missing_job_id = str(uuid4())
    store = BlobStore(str(tmp_path))
    arq = MagicMock(enqueue_job=AsyncMock())
    app = _build_app(pg_pool, store, arq=arq)

    async with await _client(app) as client:
        await client.post(
            f"/jobs/{missing_job_id}/resumes",
            files=[("files", ("resume.pdf", _PDF_MAGIC, "application/pdf"))],
            data={"consent_acknowledged": "true"},
        )

    keys = await store.list_keys()
    assert keys == []


@pytest.mark.asyncio
async def test_upload_to_a_real_job_is_unaffected_by_the_existence_check(
    pg_pool: asyncpg.Pool, tmp_path: Path
) -> None:
    """Regression guard alongside the fix: a real job's upload must still
    succeed exactly as before (guards against the existence check being
    implemented backwards, e.g. always rejecting or querying the wrong id)."""
    job_id = await _insert_job(pg_pool)
    store = BlobStore(str(tmp_path))
    arq = MagicMock(enqueue_job=AsyncMock())
    app = _build_app(pg_pool, store, arq=arq)

    async with await _client(app) as client:
        resp = await client.post(
            f"/jobs/{job_id}/resumes",
            files=[("files", ("resume.pdf", _PDF_MAGIC, "application/pdf"))],
            data={"consent_acknowledged": "true"},
        )

    assert resp.status_code == 202
    assert resp.json()[0]["outcome"] == "accepted"
    arq.enqueue_job.assert_awaited_once()

    async with pg_pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT count(*) FROM resumes WHERE job_id = $1", job_id
        )
    assert count == 1
