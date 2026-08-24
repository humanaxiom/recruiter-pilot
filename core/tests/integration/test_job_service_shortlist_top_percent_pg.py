"""Integration tests -- ``src.services.job_service.create_job`` /
``update_job`` actually persisting ``jobs.shortlist_top_percent`` against a
REAL Postgres (testcontainers), mirroring the fixture wiring in
``test_shortlist_persistence_pg.py`` / ``test_job_assignees_pg.py``.

Slice A (already GREEN on this branch) added the column + its 1-100 CHECK to
the DDL, and added ``shortlist_top_percent`` to ``JobCreate`` / ``JobUpdate``
/ the ``_JOB_COLS`` SELECT list -- but ``_insert_job`` never binds
``payload.shortlist_top_percent`` (the INSERT column list simply omits it,
relying entirely on the DB DEFAULT of 100) and ``_UPDATABLE_JOB_COLUMNS``
omits it too (a PATCH touching only this field is filtered to an empty
``fields`` dict and silently short-circuits to a plain read).

What a REAL Postgres proves that the mocked-conn unit tests
(``test_services_job_write.py``) cannot: the value that actually lands in
the ROW after a real INSERT/UPDATE round trip through asyncpg -- a mocked
``conn.fetchrow`` can be handed ANY canned row regardless of what SQL text or
bind args were actually sent, so only a real database closes the loop between
"the args looked right" and "the column is actually 30 now".

Every test below is expected to FAIL (RED) until slice B lands: the
create-with-30 test fails because the persisted row still reads 100 (the
DEFAULT, since the value was silently dropped); the PATCH-to-50 test fails
because the row is untouched (still whatever it was created with).
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Iterator
from uuid import UUID

import asyncpg
import pytest
from testcontainers.postgres import PostgresContainer

from src.models.ddl import init_schema
from src.schemas.jobs import JobCreate, JobUpdate

_ACTOR_KWARGS = {
    "actor_kind": "service",
    "actor_user_id": None,
    "actor_service": "test-harness",
}


@pytest.fixture(scope="session")
def pg_dsn() -> Iterator[str]:
    with PostgresContainer("postgres:16-alpine") as pg:
        yield re.sub(r"\+\w+", "", pg.get_connection_url(), count=1)


@pytest.fixture
async def pg_pool(pg_dsn: str) -> AsyncIterator[asyncpg.Pool]:
    pool = await asyncpg.create_pool(dsn=pg_dsn, min_size=2, max_size=8)
    await init_schema(pool)
    async with pool.acquire() as conn:
        await conn.execute("TRUNCATE jobs, outbox CASCADE")
    try:
        yield pool
    finally:
        await pool.close()


def _payload(**overrides: object) -> JobCreate:
    base: dict[str, object] = {
        "title": "Senior Backend Engineer",
        "description_raw": "We are looking for a senior backend engineer " * 3,
    }
    base.update(overrides)
    return JobCreate.model_validate(base)


async def _read_shortlist_top_percent(pool: asyncpg.Pool, job_id: UUID) -> int:
    async with pool.acquire() as conn:
        value: int = await conn.fetchval(
            "SELECT shortlist_top_percent FROM jobs WHERE id = $1", job_id
        )
    return value


# ── create_job persists the caller's chosen value ───────────────────────────


@pytest.mark.asyncio
async def test_create_job_with_custom_percent_persists_30_not_the_default(
    pg_pool: asyncpg.Pool,
) -> None:
    from src.services import job_service

    async with pg_pool.acquire() as conn:
        out = await job_service.create_job(
            conn, _payload(shortlist_top_percent=30), created_by="alice"
        )

    assert out.shortlist_top_percent == 30
    persisted = await _read_shortlist_top_percent(pg_pool, out.id)
    assert persisted == 30, (
        "the real row must read 30, not the column DEFAULT of 100 -- "
        f"got {persisted}"
    )


@pytest.mark.asyncio
async def test_create_job_omitting_the_field_persists_the_default_100(
    pg_pool: asyncpg.Pool,
) -> None:
    from src.services import job_service

    async with pg_pool.acquire() as conn:
        out = await job_service.create_job(conn, _payload(), created_by="alice")

    assert out.shortlist_top_percent == 100
    persisted = await _read_shortlist_top_percent(pg_pool, out.id)
    assert persisted == 100


# ── update_job actually writes the new value ────────────────────────────────


@pytest.mark.asyncio
async def test_update_job_patch_changes_the_real_row_to_50(
    pg_pool: asyncpg.Pool,
) -> None:
    from src.services import job_service

    async with pg_pool.acquire() as conn:
        created = await job_service.create_job(conn, _payload(), created_by="alice")
    assert created.shortlist_top_percent == 100

    async with pg_pool.acquire() as conn:
        out = await job_service.update_job(
            conn,
            created.id,
            JobUpdate.model_validate({"shortlist_top_percent": 50}),
            **_ACTOR_KWARGS,
        )

    assert out.shortlist_top_percent == 50
    persisted = await _read_shortlist_top_percent(pg_pool, created.id)
    assert persisted == 50, (
        "PATCH {shortlist_top_percent: 50} must actually UPDATE the row -- "
        f"got {persisted} (a filtered-out PATCH silently short-circuits to "
        "a no-op read and leaves the row at its prior value)"
    )


@pytest.mark.asyncio
async def test_update_job_omitting_the_field_leaves_the_real_row_unchanged(
    pg_pool: asyncpg.Pool,
) -> None:
    from src.services import job_service

    async with pg_pool.acquire() as conn:
        created = await job_service.create_job(
            conn, _payload(shortlist_top_percent=42), created_by="alice"
        )

    async with pg_pool.acquire() as conn:
        out = await job_service.update_job(
            conn,
            created.id,
            JobUpdate.model_validate({"title": "New Title"}),
            **_ACTOR_KWARGS,
        )

    assert out.shortlist_top_percent == 42
    persisted = await _read_shortlist_top_percent(pg_pool, created.id)
    assert persisted == 42


@pytest.mark.asyncio
async def test_update_job_patch_to_101_is_rejected_by_the_real_check_constraint(
    pg_pool: asyncpg.Pool,
) -> None:
    """The pydantic ``JobUpdate`` schema already rejects >100 at the API
    boundary (``le=100``) -- this test proves the REAL Postgres CHECK is
    still there as a second line of defence by going around pydantic and
    issuing the UPDATE directly, mirroring
    ``test_shortlist_top_percent_pg.py``'s equivalent raw-SQL CHECK test."""
    from src.services import job_service

    async with pg_pool.acquire() as conn:
        created = await job_service.create_job(conn, _payload(), created_by="alice")

    async with pg_pool.acquire() as conn:
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "UPDATE jobs SET shortlist_top_percent = $2 WHERE id = $1",
                created.id,
                101,
            )
