"""asyncpg connection-pool lifecycle + the per-request connection dependency.

The pool is process-global: one per uvicorn worker, parked on ``app.state``.
Each request takes a single connection for its duration; transactions are
scoped by the caller (``async with conn.transaction(): ...``).

Ported from hris ``apps/api/src/api/db/session.py``. Raw asyncpg — the DSN in
settings is a plain ``postgresql://`` URL, never SQLAlchemy's
``postgresql+asyncpg://`` form, which ``asyncpg.create_pool`` cannot parse.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Annotated, Any

import asyncpg
from fastapi import Depends, FastAPI, Request

from src.settings import Settings, get_settings

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from asyncpg import Record
    from asyncpg.pool import PoolConnectionProxy

    # mypy --strict's disallow_any_generics wants the real parametrised type.
    _ConnT = PoolConnectionProxy[Record]
else:
    # PoolConnectionProxy is generic only in the stubs — subscripting the REAL
    # runtime class raises. `from __future__ import annotations` (this
    # module's own PEP 563) stringifies every annotation, and FastAPI's
    # dependant-graph builder introspects `get_db` (as a `Depends(get_db)`
    # sub-dependency) with `inspect.signature(eval_str=True)`, which `eval()`s
    # that string — so a real `PoolConnectionProxy[Record]` reference
    # anywhere in `get_db`'s signature (params OR return) blows up the FIRST
    # time any route actually uses it (nothing did before Phase 6). `Any`
    # here is what that `eval()` actually resolves at runtime; mypy never
    # sees this branch.
    _ConnT = Any


async def init_pool(
    app: FastAPI, settings: Settings | None = None
) -> asyncpg.Pool[Record]:
    """Create the asyncpg pool and attach it to ``app.state.pg_pool``."""
    s = settings or get_settings()
    pool = await asyncpg.create_pool(
        dsn=s.postgres_dsn,
        min_size=s.postgres_pool_min,
        max_size=s.postgres_pool_max,
        command_timeout=30,
    )
    app.state.pg_pool = pool
    logger.info("pg.pool.init min=%s max=%s", s.postgres_pool_min, s.postgres_pool_max)
    return pool


async def close_pool(app: FastAPI) -> None:
    """Close the pool if it was ever opened. A no-op otherwise."""
    pool: asyncpg.Pool[Record] | None = getattr(app.state, "pg_pool", None)
    if pool is not None:
        await pool.close()
        app.state.pg_pool = None
        logger.info("pg.pool.close")


async def get_db(request: Request) -> AsyncIterator[_ConnT]:
    """FastAPI dep — one connection per request, released back to the pool.

    Return-typed via ``_ConnT`` (not a direct ``PoolConnectionProxy[Record]``
    reference) — see the module-level comment: this signature IS introspected
    by FastAPI (as a ``Depends(get_db)`` sub-dependency), so the real
    un-subscriptable runtime class can never appear here directly.
    """
    pool: asyncpg.Pool[Record] | None = getattr(request.app.state, "pg_pool", None)
    if pool is None:
        raise RuntimeError("Postgres pool not initialised (lifespan not running)")
    async with pool.acquire() as conn:
        yield conn


Db = Annotated[_ConnT, Depends(get_db)]
