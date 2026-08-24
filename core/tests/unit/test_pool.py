"""Unit tests for the asyncpg pool lifecycle (``src/models/pool.py``).

No live Postgres: ``asyncpg.create_pool`` is mocked throughout.

Contract with the implementation (mirrors hris ``api/db/session.py``):

* the module does ``import asyncpg`` and calls
  ``asyncpg.create_pool(dsn=..., min_size=..., max_size=..., command_timeout=30)``,
* the pool lives on ``app.state.pg_pool``,
* ``get_db`` is a FastAPI dependency: an async generator yielding one
  connection acquired from the pool, raising if the pool is absent.
"""

from __future__ import annotations

import inspect
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI

from src.models.pool import close_pool, get_db, init_pool
from src.settings import Settings


@pytest.fixture
def app() -> FastAPI:
    return FastAPI()


@pytest.fixture
def settings() -> Settings:
    return Settings()


def _fake_pool() -> Any:
    pool = MagicMock()
    pool.close = AsyncMock()
    return pool


def _request_for(app: FastAPI) -> Any:
    request = MagicMock()
    request.app = app
    return request


# ── init_pool ──────────────────────────────────────────────────────────────


def test_pool_helpers_are_async() -> None:
    assert inspect.iscoroutinefunction(init_pool)
    assert inspect.iscoroutinefunction(close_pool)
    assert inspect.isasyncgenfunction(get_db)


@pytest.mark.asyncio
async def test_init_pool_attaches_pool_to_app_state(
    app: FastAPI, settings: Settings
) -> None:
    pool = _fake_pool()
    with patch(
        "src.models.pool.asyncpg.create_pool", AsyncMock(return_value=pool)
    ) as create:
        returned = await init_pool(app, settings)

    create.assert_awaited_once_with(
        dsn=settings.postgres_dsn,
        min_size=settings.postgres_pool_min,
        max_size=settings.postgres_pool_max,
        command_timeout=30,
    )
    assert app.state.pg_pool is pool
    assert returned is pool


@pytest.mark.asyncio
async def test_init_pool_uses_the_raw_dsn_from_settings(app: FastAPI) -> None:
    """asyncpg cannot parse SQLAlchemy's ``postgresql+asyncpg://`` form."""
    settings = Settings(postgres_dsn="postgresql://u:p@db:5432/recruiter")
    with patch(
        "src.models.pool.asyncpg.create_pool", AsyncMock(return_value=_fake_pool())
    ) as create:
        await init_pool(app, settings)

    dsn = create.await_args.kwargs["dsn"]
    assert dsn == "postgresql://u:p@db:5432/recruiter"
    assert "+asyncpg" not in dsn


@pytest.mark.asyncio
async def test_init_pool_falls_back_to_get_settings(app: FastAPI) -> None:
    override = Settings(postgres_dsn="postgresql://x:y@elsewhere:5432/recruiter")
    with (
        patch("src.models.pool.get_settings", return_value=override),
        patch(
            "src.models.pool.asyncpg.create_pool", AsyncMock(return_value=_fake_pool())
        ) as create,
    ):
        await init_pool(app)

    assert create.await_args.kwargs["dsn"] == override.postgres_dsn


@pytest.mark.asyncio
async def test_init_pool_propagates_connection_failure(
    app: FastAPI, settings: Settings
) -> None:
    with patch(
        "src.models.pool.asyncpg.create_pool",
        AsyncMock(side_effect=OSError("postgres unreachable")),
    ):
        with pytest.raises(OSError, match="postgres unreachable"):
            await init_pool(app, settings)


# ── close_pool ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_close_pool_closes_and_clears(app: FastAPI) -> None:
    pool = _fake_pool()
    app.state.pg_pool = pool

    await close_pool(app)

    pool.close.assert_awaited_once()
    assert getattr(app.state, "pg_pool", None) is None


@pytest.mark.asyncio
async def test_close_pool_is_a_noop_when_never_initialised(app: FastAPI) -> None:
    await close_pool(app)  # must not raise — lifespan may fail before init


# ── get_db ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_db_yields_a_connection_from_the_pool(app: FastAPI) -> None:
    conn = MagicMock()
    pool = _fake_pool()
    pool.acquire.return_value.__aenter__.return_value = conn
    pool.acquire.return_value.__aexit__.return_value = None
    app.state.pg_pool = pool

    agen = get_db(_request_for(app))
    yielded = await agen.__anext__()
    assert yielded is conn

    with pytest.raises(StopAsyncIteration):
        await agen.__anext__()
    pool.acquire.assert_called_once()


@pytest.mark.asyncio
async def test_get_db_raises_a_clear_error_when_the_pool_is_missing(
    app: FastAPI,
) -> None:
    with pytest.raises(RuntimeError, match="(?i)pool"):
        await get_db(_request_for(app)).__anext__()


@pytest.mark.asyncio
async def test_get_db_raises_when_the_pool_was_closed(app: FastAPI) -> None:
    app.state.pg_pool = _fake_pool()
    await close_pool(app)

    with pytest.raises(RuntimeError, match="(?i)pool"):
        await get_db(_request_for(app)).__anext__()
