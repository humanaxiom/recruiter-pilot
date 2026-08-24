"""Integration tests — ``jobs.shortlist_top_percent`` against a REAL Postgres
(testcontainers). Slice A (schema foundation) of "per-job configurable
shortlist size": today the shortlist persists ALL ranked candidates up to
``match_coarse_k=50`` — no fixed cap. ``shortlist_top_percent`` (1-100,
default 100 = today's "keep all") will cap the persisted shortlist to the
top P% of the ranked pool once slice B lands; this slice only adds the
column + its CHECK.

Schema-only slice: ``src/models/ddl.py`` does not declare the column yet, so
every test below is expected to FAIL (RED) until that lands — most as an
``asyncpg.UndefinedColumnError`` (the column is not there at all), the
CHECK-violation tests failing because no CHECK exists to violate (the INSERT
just succeeds).

These tests prove, against a live database, what
``tests/unit/test_ddl.py``'s string-matching structurally cannot:

* a ``jobs`` row inserted with no ``shortlist_top_percent`` really defaults
  to 100 at the database level (DEFAULT, not just declared in SQL text),
* the 1-100 CHECK really rejects 0 and 101 with a real
  ``asyncpg.CheckViolationError``, not merely "the DDL text contains the
  word CHECK",
* 1 and 100 (the inclusive bounds) really succeed,
* ``init_schema`` stays idempotent with the new column added — the
  canonical FU-3 ``description_sha256`` risk: ``CREATE TABLE IF NOT EXISTS``
  is a no-op against an already-migrated dev/CI volume, so only a real
  second ``init_schema`` run against a database that already has the column
  proves the ``ADD COLUMN IF NOT EXISTS`` clause is actually safe to
  re-run.

Follows the exact asyncpg/testcontainers fixture wiring already used in
``tests/integration/test_schema.py`` and ``tests/integration/test_job_assignees_pg.py``
— no new harness.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Iterator

import asyncpg
import pytest
from testcontainers.postgres import PostgresContainer

from src.models.ddl import init_schema

INSERT_JOB_DEFAULT = """
INSERT INTO jobs (title, description_raw)
VALUES ($1, $2)
RETURNING id, shortlist_top_percent
"""

INSERT_JOB_WITH_PERCENT = """
INSERT INTO jobs (title, description_raw, shortlist_top_percent)
VALUES ($1, $2, $3)
RETURNING id, shortlist_top_percent
"""


@pytest.fixture(scope="session")
def pg_dsn() -> Iterator[str]:
    with PostgresContainer("postgres:16-alpine") as pg:
        # asyncpg needs the raw DSN, not SQLAlchemy's postgresql+driver:// form.
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


# ── Idempotency, for real ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_init_schema_twice_in_a_row_succeeds_with_shortlist_top_percent(
    pg_dsn: str,
) -> None:
    """``init_schema`` re-runs on every boot (no migration framework) — the
    new column must not break that guarantee, whether the column already
    exists (second run here) or the table itself pre-dates the column
    (proven implicitly: the first run is exactly the "existing volume
    without the column yet" case)."""
    connection = await asyncpg.connect(pg_dsn)
    try:
        await init_schema(connection)
        await init_schema(connection)
    finally:
        await connection.close()


# ── The column exists and defaults correctly ───────────────────────────────


@pytest.mark.asyncio
async def test_job_insert_without_shortlist_top_percent_defaults_to_100(
    conn: asyncpg.Connection,
) -> None:
    row = await conn.fetchrow(
        INSERT_JOB_DEFAULT, "Senior Python Engineer", "Build the ranking pipeline."
    )
    assert row is not None
    assert row["shortlist_top_percent"] == 100


@pytest.mark.asyncio
async def test_shortlist_top_percent_column_is_integer(
    conn: asyncpg.Connection,
) -> None:
    data_type = await conn.fetchval("""
        SELECT data_type FROM information_schema.columns
        WHERE table_name = 'jobs' AND column_name = 'shortlist_top_percent'
        """)
    assert data_type == "integer"


# ── The 1-100 CHECK, enforced for real ──────────────────────────────────────


@pytest.mark.asyncio
async def test_job_insert_rejects_shortlist_top_percent_zero(
    conn: asyncpg.Connection,
) -> None:
    with pytest.raises(asyncpg.CheckViolationError):
        await conn.execute(
            INSERT_JOB_WITH_PERCENT,
            "Senior Python Engineer",
            "Build the ranking pipeline.",
            0,
        )


@pytest.mark.asyncio
async def test_job_insert_rejects_shortlist_top_percent_101(
    conn: asyncpg.Connection,
) -> None:
    with pytest.raises(asyncpg.CheckViolationError):
        await conn.execute(
            INSERT_JOB_WITH_PERCENT,
            "Senior Python Engineer",
            "Build the ranking pipeline.",
            101,
        )


@pytest.mark.asyncio
async def test_job_insert_accepts_shortlist_top_percent_lower_bound(
    conn: asyncpg.Connection,
) -> None:
    row = await conn.fetchrow(
        INSERT_JOB_WITH_PERCENT, "Senior Python Engineer", "Build the pipeline.", 1
    )
    assert row is not None
    assert row["shortlist_top_percent"] == 1


@pytest.mark.asyncio
async def test_job_insert_accepts_shortlist_top_percent_upper_bound(
    conn: asyncpg.Connection,
) -> None:
    row = await conn.fetchrow(
        INSERT_JOB_WITH_PERCENT, "Senior Python Engineer", "Build the pipeline.", 100
    )
    assert row is not None
    assert row["shortlist_top_percent"] == 100


@pytest.mark.asyncio
async def test_job_insert_accepts_a_mid_range_value(
    conn: asyncpg.Connection,
) -> None:
    row = await conn.fetchrow(
        INSERT_JOB_WITH_PERCENT, "Senior Python Engineer", "Build the pipeline.", 25
    )
    assert row is not None
    assert row["shortlist_top_percent"] == 25


@pytest.mark.asyncio
async def test_job_update_to_an_out_of_range_value_is_rejected(
    conn: asyncpg.Connection,
) -> None:
    """The CHECK fires on UPDATE too, not just INSERT."""
    row = await conn.fetchrow(
        INSERT_JOB_DEFAULT, "Senior Python Engineer", "Build the pipeline."
    )
    assert row is not None
    with pytest.raises(asyncpg.CheckViolationError):
        await conn.execute(
            "UPDATE jobs SET shortlist_top_percent = $2 WHERE id = $1",
            row["id"],
            0,
        )
