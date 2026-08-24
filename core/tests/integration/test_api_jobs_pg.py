"""Integration tests — Phase 6's job routes against a REAL Postgres
(testcontainers) via the real ASGI app (``app.state.pg_pool``), a real
``BlobStore`` rooted at a temp dir, and a FAKE ``ArqRedis`` recording
``enqueue_job`` calls (no real Redis/arq — nothing here needs the broker
itself, only proof that the route calls ``enqueue_job`` with the right
arguments once a job row is REALLY committed).

``src.api.routes.jobs`` does not exist yet — every test below fails at
collection or the first request. RED half of the TDD cycle.

What a REAL Postgres proves that the mocked-conn route tests
(``test_route_jobs.py``) cannot: ``create_job``'s INSERT actually satisfies
the DDL's ``job_status`` enum / ``blind_review BOOLEAN NOT NULL DEFAULT
TRUE`` / ``retention_days`` CHECK, and ``transition_status``'s UPDATE
resolves against a row that REALLY started life in 'draft'.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from testcontainers.postgres import PostgresContainer

from src.api.deps import Role, get_arq, resolve_role
from src.api.routes import jobs as jobs_routes
from src.errors import AppError
from src.models.ddl import init_schema
from src.models.pool import get_db


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


def _build_app(pool: asyncpg.Pool, *, arq: MagicMock) -> FastAPI:
    app = FastAPI()
    app.include_router(jobs_routes.router)

    async def _get_db_override() -> AsyncIterator[Any]:
        async with pool.acquire() as conn:
            yield conn

    app.dependency_overrides[get_db] = _get_db_override
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


@pytest.mark.asyncio
async def test_create_and_get_job_round_trips_through_real_postgres(
    pg_pool: asyncpg.Pool,
) -> None:
    arq = MagicMock(enqueue_job=AsyncMock())
    app = _build_app(pg_pool, arq=arq)
    async with await _client(app) as client:
        create_resp = await client.post(
            "/jobs",
            json={
                "title": "Senior Backend Engineer",
                "description_raw": "We need a senior backend engineer. " * 3,
            },
        )
        assert create_resp.status_code == 201
        job_id = create_resp.json()["id"]

        get_resp = await client.get(f"/jobs/{job_id}")
    assert get_resp.status_code == 200
    assert get_resp.json()["title"] == "Senior Backend Engineer"
    assert get_resp.json()["status"] == "draft"
    arq.enqueue_job.assert_awaited_once_with("parse_job", job_id)


@pytest.mark.asyncio
async def test_create_job_blind_review_defaults_true_against_real_ddl(
    pg_pool: asyncpg.Pool,
) -> None:
    arq = MagicMock(enqueue_job=AsyncMock())
    app = _build_app(pg_pool, arq=arq)
    async with await _client(app) as client:
        resp = await client.post(
            "/jobs",
            json={
                "title": "Staff Engineer",
                "description_raw": "We need a staff engineer. " * 3,
            },
        )
    assert resp.json()["blind_review"] is True


@pytest.mark.asyncio
async def test_transition_status_draft_to_open_persists_against_real_postgres(
    pg_pool: asyncpg.Pool,
) -> None:
    arq = MagicMock(enqueue_job=AsyncMock())
    app = _build_app(pg_pool, arq=arq)
    async with await _client(app) as client:
        create_resp = await client.post(
            "/jobs",
            json={
                "title": "Product Manager",
                "description_raw": "We need a product manager. " * 3,
            },
        )
        job_id = create_resp.json()["id"]
        patch_resp = await client.patch(f"/jobs/{job_id}/status", json={"to": "open"})
        get_resp = await client.get(f"/jobs/{job_id}")
    assert patch_resp.status_code == 200
    assert get_resp.json()["status"] == "open"


@pytest.mark.asyncio
async def test_get_job_404_for_a_real_missing_uuid(pg_pool: asyncpg.Pool) -> None:
    from uuid import uuid4

    arq = MagicMock(enqueue_job=AsyncMock())
    app = _build_app(pg_pool, arq=arq)
    async with await _client(app) as client:
        resp = await client.get(f"/jobs/{uuid4()}")
    assert resp.status_code == 404
