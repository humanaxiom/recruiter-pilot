"""F2 (major, PR review 2026-08-18) — the ``jobs_shortlist_state_check``
DROP+ADD pair races two booting containers.

``core/src/models/ddl.py:147-149`` runs

    ALTER TABLE jobs DROP CONSTRAINT IF EXISTS jobs_shortlist_state_check
    ALTER TABLE jobs ADD CONSTRAINT jobs_shortlist_state_check CHECK (...)

as two separate autocommit statements, unconditionally, on EVERY boot of
``init_schema`` — not just the first time the column is created. Both `api`
(``core/src/api/main.py:56``) and `worker` (``core/src/worker/main.py:81``)
call ``init_schema`` and start together, and neither has a ``restart:``
policy in ``docker-compose.yml``, so the loser of the ``ADD CONSTRAINT`` race
raises ``asyncpg`` error 42710 (``duplicate_object`` — you cannot have two
constraints with the same name on one table) and the container stays exited.
The reviewer reproduced this 25/25 against a real Postgres.

**What this file proves, and what it honestly cannot.** The test below
isolates the DROP+ADD pair as the ONLY non-idempotent-under-concurrency
statement in ``_STATEMENTS`` by first running ``init_schema`` once
sequentially (so every ``CREATE TABLE IF NOT EXISTS`` / ``ADD COLUMN IF NOT
EXISTS`` / ``DO $$ IF NOT EXISTS ... $$`` statement ahead of it is already a
true no-op on both connections — the exact "already-deployed volume" +
"next boot" pattern the sibling tests in ``test_schema.py`` already use),
then races two FRESH connections through a full ``init_schema`` call via
``asyncio.gather``. If both DROP+ADD attempts land close enough in wall-clock
time for Postgres's own constraint-name uniqueness check to see them
overlap, the loser raises. This is a genuine, unmocked race against a real
server — not a mocked timing assumption — but a race is, by its nature, not
guaranteed to land on every single run in every environment; local Docker on
this host measured it landing consistently (see the RED evidence in this
branch's test report), but a slower or faster container runtime could in
principle serialize the two ``ADD CONSTRAINT`` statements far enough apart
that neither observes the other's uncommitted DROP. If this ever starts
passing spuriously, that is exactly the ambiguity CLAUDE.md's "offline is
not always enough" note warns about: a green run here is evidence of ABSENCE
of the race on that one run, not proof the underlying pair is safe — the
structural fix (a single guarded transaction / advisory lock / catching and
swallowing 42710) is what actually closes it, not this test learning to
retry until it passes.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Iterator

import asyncpg
import pytest
from testcontainers.postgres import PostgresContainer

from src.models.ddl import init_schema


@pytest.fixture(scope="module")
def pg_dsn() -> Iterator[str]:
    with PostgresContainer("postgres:16-alpine") as pg:
        yield re.sub(r"\+\w+", "", pg.get_connection_url(), count=1)


async def _fresh_conn(pg_dsn: str) -> asyncpg.Connection:
    conn: asyncpg.Connection = await asyncpg.connect(pg_dsn)
    return conn


@pytest.mark.asyncio
async def test_two_concurrent_boots_do_not_race_on_the_widened_check_constraint(
    pg_dsn: str,
) -> None:
    """The behaviour the fix must deliver: ``api`` and ``worker`` booting at
    the same instant against an already-migrated volume must BOTH finish
    ``init_schema`` without either raising, and the table must be left with
    exactly one ``jobs_shortlist_state_check`` constraint, carrying the
    widened definition (both 'awaiting_llm' and 'ranking' legal).

    Fails against today's code: the second ``ADD CONSTRAINT`` to land loses
    to Postgres's own uniqueness check on the constraint name and raises
    ``asyncpg.DuplicateObjectError`` (sqlstate 42710) — exactly the failure
    mode that leaves one of the two containers exited on a real boot.
    """
    baseline = await _fresh_conn(pg_dsn)
    try:
        # Stand-in for "an already-deployed volume" — every statement ahead
        # of the DROP+ADD pair is now a genuine no-op on the race below, so
        # that pair is the ONLY non-idempotent-under-concurrency statement
        # left to race on.
        await init_schema(baseline)
    finally:
        await baseline.close()

    conn_a = await _fresh_conn(pg_dsn)
    conn_b = await _fresh_conn(pg_dsn)
    try:
        results = await asyncio.gather(
            init_schema(conn_a), init_schema(conn_b), return_exceptions=True
        )
    finally:
        await conn_a.close()
        await conn_b.close()

    errors = [r for r in results if isinstance(r, BaseException)]
    assert not errors, (
        "two concurrent init_schema() boots must both succeed -- got "
        f"{[repr(e) for e in errors]}; this is F2: the DROP+ADD CONSTRAINT "
        "pair at ddl.py:147-149 is not safe against two containers booting "
        "together"
    )

    check_conn = await _fresh_conn(pg_dsn)
    try:
        rows = await check_conn.fetch("""
            SELECT conname, pg_get_constraintdef(oid) AS def
              FROM pg_constraint
             WHERE conrelid = 'jobs'::regclass
               AND pg_get_constraintdef(oid) ILIKE '%shortlist_state%'
            """)
    finally:
        await check_conn.close()

    assert len(rows) == 1, (
        "expected exactly one shortlist_state CHECK constraint to survive "
        f"the race, found {len(rows)}: {[dict(r) for r in rows]}"
    )
    assert rows[0]["conname"] == "jobs_shortlist_state_check"
    assert "ranking" in rows[0]["def"]
    assert "awaiting_llm" in rows[0]["def"]


@pytest.mark.asyncio
async def test_two_concurrent_boots_from_a_genuinely_fresh_database_also_succeed(
    pg_dsn: str,
) -> None:
    """The stricter, more realistic sibling of the test above: a genuinely
    fresh Postgres volume (no baseline run at all) is exactly what a brand
    new ``docker compose up`` sees, and ``api``/``worker`` still start
    together against it. Uses its OWN container (not the module-scoped
    ``pg_dsn`` fixture, which the first test has already migrated) so this
    really is the very first ``init_schema`` call either connection has ever
    made against this database.

    This races every statement in ``_STATEMENTS``, not only the DROP+ADD
    pair -- so on its own it cannot pin the DROP+ADD pair specifically the
    way the isolated test above does (a failure here could in principle come
    from a different concurrently-unsafe statement). It is included because
    it is the actual first-boot scenario, and because the reviewer's 25/25
    reproduction was against a real dev stack's first boot, not a
    pre-migrated one.
    """
    with PostgresContainer("postgres:16-alpine") as pg:
        fresh_dsn = re.sub(r"\+\w+", "", pg.get_connection_url(), count=1)
        conn_a = await _fresh_conn(fresh_dsn)
        conn_b = await _fresh_conn(fresh_dsn)
        try:
            results = await asyncio.gather(
                init_schema(conn_a), init_schema(conn_b), return_exceptions=True
            )
        finally:
            await conn_a.close()
            await conn_b.close()

    errors = [r for r in results if isinstance(r, BaseException)]
    assert not errors, (
        "two containers booting together against a brand-new volume must "
        f"both succeed -- got {[repr(e) for e in errors]}"
    )


@pytest.mark.asyncio
async def test_three_concurrent_boots_from_a_genuinely_fresh_database_also_succeed(
    pg_dsn: str,
) -> None:
    """``init_schema``'s retry loop must not be a two-caller special case.

    Nothing about ``docker compose up`` guarantees exactly two concurrent
    callers forever -- an ``api`` replica, a test harness, or a future
    migration runner could make it three (or more). This races THREE fresh
    connections through ``init_schema`` against a genuinely fresh database
    (its own container, not the module-scoped ``pg_dsn`` fixture the earlier
    tests have already migrated) and asserts all three finish without error
    and the resulting schema is correct -- specifically that the widened
    ``jobs_shortlist_state_check`` constraint (the one statement in
    ``_STATEMENTS`` proven above to race under concurrency) survives exactly
    once, with both legal values present.
    """
    with PostgresContainer("postgres:16-alpine") as pg:
        fresh_dsn = re.sub(r"\+\w+", "", pg.get_connection_url(), count=1)
        conn_a = await _fresh_conn(fresh_dsn)
        conn_b = await _fresh_conn(fresh_dsn)
        conn_c = await _fresh_conn(fresh_dsn)
        try:
            results = await asyncio.gather(
                init_schema(conn_a),
                init_schema(conn_b),
                init_schema(conn_c),
                return_exceptions=True,
            )
        finally:
            await conn_a.close()
            await conn_b.close()
            await conn_c.close()

        errors = [r for r in results if isinstance(r, BaseException)]
        assert not errors, (
            "three concurrent init_schema() boots against a brand-new "
            f"volume must all succeed -- got {[repr(e) for e in errors]}"
        )

        check_conn = await _fresh_conn(fresh_dsn)
        try:
            rows = await check_conn.fetch("""
                SELECT conname, pg_get_constraintdef(oid) AS def
                  FROM pg_constraint
                 WHERE conrelid = 'jobs'::regclass
                   AND pg_get_constraintdef(oid) ILIKE '%shortlist_state%'
                """)
        finally:
            await check_conn.close()

    assert len(rows) == 1, (
        "expected exactly one shortlist_state CHECK constraint to survive "
        f"the three-way race, found {len(rows)}: {[dict(r) for r in rows]}"
    )
    assert rows[0]["conname"] == "jobs_shortlist_state_check"
    assert "ranking" in rows[0]["def"]
    assert "awaiting_llm" in rows[0]["def"]
