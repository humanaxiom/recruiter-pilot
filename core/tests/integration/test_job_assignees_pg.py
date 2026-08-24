"""Integration tests — the ``job_assignees`` table (ADR-020 §1, FU-6 slice 1)
against a REAL Postgres (testcontainers).

Schema-only slice: ``src/models/ddl.py`` does not declare ``job_assignees``
yet, so every test below is expected to FAIL (RED) until that lands — most as
an ``asyncpg.UndefinedTableError`` (or the same surfacing through the ``conn``
fixture's ``TRUNCATE``), since the table does not exist at all.

These tests prove, against a live database, what
``tests/unit/test_ddl.py``'s string-matching structurally cannot:

* ``init_schema`` stays idempotent with ``job_assignees`` added,
* the composite PRIMARY KEY ``(job_id, user_id)`` really rejects a duplicate
  assignment, while two different users assigned to the same job coexist,
* a ``job_assignees`` row REQUIRES a real ``jobs`` row — the FK on ``job_id``
  is enforced by Postgres, not just declared in SQL text,
* ``ON DELETE CASCADE`` on ``job_id`` really removes a job's assignments when
  the job is deleted,
* ``ON DELETE CASCADE`` on ``user_id`` really removes a user's assignments
  when the assigned user is deleted,
* ``ON DELETE RESTRICT`` on ``assigned_by`` really REJECTS deleting a user
  who is the assigner of a live assignment — ADR-020 §1's specific safety
  guard against accidental admin-account cleanup erasing delegation records,
  which a string-match test cannot observe firing,
* ``assigned_at`` defaults to ``now()``.

Follows the exact asyncpg/testcontainers fixture wiring already used in
``tests/integration/test_users_audit_pg.py`` and
``tests/integration/test_sessions_pg.py`` — no new harness.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime

import asyncpg
import pytest
from testcontainers.postgres import PostgresContainer

from src.models.ddl import init_schema

INSERT_USER = """
INSERT INTO users (cas_username, display_name, email, role)
VALUES ($1, $2, $3, $4)
RETURNING id
"""

INSERT_JOB = """
INSERT INTO jobs (title, description_raw)
VALUES ($1, $2)
RETURNING id
"""

INSERT_ASSIGNEE = """
INSERT INTO job_assignees (job_id, user_id, assigned_by)
VALUES ($1, $2, $3)
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
    await connection.execute(
        "TRUNCATE job_assignees, sessions, users, audit_log, jobs, outbox CASCADE"
    )
    try:
        yield connection
    finally:
        await connection.close()


async def _insert_user(
    conn: asyncpg.Connection,
    cas_username: str | None = None,
    role: str = "recruiter",
) -> uuid.UUID:
    user_id: uuid.UUID = await conn.fetchval(
        INSERT_USER,
        cas_username or f"user-{uuid.uuid4().hex}",
        "Ada Lovelace",
        "ada@example.org",
        role,
    )
    return user_id


async def _insert_job(
    conn: asyncpg.Connection, title: str = "Senior Engineer"
) -> uuid.UUID:
    job_id: uuid.UUID = await conn.fetchval(
        INSERT_JOB, title, "Build the ranking pipeline."
    )
    return job_id


# ── Idempotency, for real ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_init_schema_twice_in_a_row_succeeds_with_job_assignees(
    pg_dsn: str,
) -> None:
    """``init_schema`` re-runs on every boot (no migration framework) — the
    new table must not break that guarantee."""
    connection = await asyncpg.connect(pg_dsn)
    try:
        await init_schema(connection)
        await init_schema(connection)
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_job_assignees_table_exists(conn: asyncpg.Connection) -> None:
    rows = await conn.fetch(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
    )
    tables = {row["tablename"] for row in rows}
    assert "job_assignees" in tables


@pytest.mark.asyncio
async def test_job_assignees_user_idx_exists(conn: asyncpg.Connection) -> None:
    rows = await conn.fetch("""
        SELECT indexname FROM pg_indexes
         WHERE schemaname = 'public' AND tablename = 'job_assignees'
        """)
    names = {row["indexname"] for row in rows}
    assert "job_assignees_user_idx" in names


# ── Composite PRIMARY KEY (job_id, user_id) ─────────────────────────────────


@pytest.mark.asyncio
async def test_duplicate_job_user_assignment_is_rejected(
    conn: asyncpg.Connection,
) -> None:
    """ADR-020 §1 — a user may be assigned to a given job at most once; the
    PK is the (job_id, user_id) pair."""
    job_id = await _insert_job(conn)
    assigner = await _insert_user(conn, role="admin")
    assignee = await _insert_user(conn, role="hiring_manager")
    await conn.execute(INSERT_ASSIGNEE, job_id, assignee, assigner)

    with pytest.raises(asyncpg.UniqueViolationError):
        await conn.execute(INSERT_ASSIGNEE, job_id, assignee, assigner)


@pytest.mark.asyncio
async def test_two_different_users_can_be_assigned_to_the_same_job(
    conn: asyncpg.Connection,
) -> None:
    job_id = await _insert_job(conn)
    assigner = await _insert_user(conn, role="admin")
    first_assignee = await _insert_user(conn, role="hiring_manager")
    second_assignee = await _insert_user(conn, role="hiring_manager")

    await conn.execute(INSERT_ASSIGNEE, job_id, first_assignee, assigner)
    await conn.execute(INSERT_ASSIGNEE, job_id, second_assignee, assigner)

    count = await conn.fetchval(
        "SELECT count(*) FROM job_assignees WHERE job_id = $1", job_id
    )
    assert count == 2


# ── FK: job_id requires a real job ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_assignee_insert_requires_an_existing_job(
    conn: asyncpg.Connection,
) -> None:
    """An assignment for a job_id with no matching jobs row must be rejected
    by the database FK, not merely by application discipline."""
    assigner = await _insert_user(conn, role="admin")
    assignee = await _insert_user(conn, role="hiring_manager")

    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await conn.execute(
            INSERT_ASSIGNEE,
            uuid.uuid4(),  # no such job
            assignee,
            assigner,
        )


@pytest.mark.asyncio
async def test_assignee_insert_requires_an_existing_user(
    conn: asyncpg.Connection,
) -> None:
    """Symmetric FK on user_id — mirrors the job_id FK proof above."""
    job_id = await _insert_job(conn)
    assigner = await _insert_user(conn, role="admin")

    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await conn.execute(
            INSERT_ASSIGNEE,
            job_id,
            uuid.uuid4(),  # no such user
            assigner,
        )


@pytest.mark.asyncio
async def test_assignee_insert_requires_an_existing_assigner(
    conn: asyncpg.Connection,
) -> None:
    """assigned_by is also a real FK to users(id)."""
    job_id = await _insert_job(conn)
    assignee = await _insert_user(conn, role="hiring_manager")

    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await conn.execute(
            INSERT_ASSIGNEE,
            job_id,
            assignee,
            uuid.uuid4(),  # no such assigner
        )


# ── ON DELETE CASCADE on job_id ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_deleting_a_job_cascades_to_its_assignments(
    conn: asyncpg.Connection,
) -> None:
    """ADR-020 §1 — 'a job has no hiring managers if it doesn't exist.'
    Unprovable by string-matching the DDL — only a real DELETE against a real
    FK proves the CASCADE actually fires."""
    job_id = await _insert_job(conn)
    assigner = await _insert_user(conn, role="admin")
    assignee = await _insert_user(conn, role="hiring_manager")
    await conn.execute(INSERT_ASSIGNEE, job_id, assignee, assigner)

    await conn.execute("DELETE FROM jobs WHERE id = $1", job_id)

    remaining = await conn.fetchval(
        "SELECT count(*) FROM job_assignees WHERE job_id = $1", job_id
    )
    assert remaining == 0


@pytest.mark.asyncio
async def test_deleting_an_unrelated_job_does_not_touch_other_assignments(
    conn: asyncpg.Connection,
) -> None:
    assigner = await _insert_user(conn, role="admin")
    assignee = await _insert_user(conn, role="hiring_manager")

    kept_job = await _insert_job(conn, title="Kept Job")
    await conn.execute(INSERT_ASSIGNEE, kept_job, assignee, assigner)

    doomed_job = await _insert_job(conn, title="Doomed Job")
    other_assignee = await _insert_user(conn, role="hiring_manager")
    await conn.execute(INSERT_ASSIGNEE, doomed_job, other_assignee, assigner)

    await conn.execute("DELETE FROM jobs WHERE id = $1", doomed_job)

    survivor = await conn.fetchval(
        "SELECT user_id FROM job_assignees WHERE job_id = $1", kept_job
    )
    assert survivor == assignee


# ── ON DELETE CASCADE on user_id ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_deleting_an_assigned_user_cascades_to_their_assignments(
    conn: asyncpg.Connection,
) -> None:
    """Deleting the *assignee* (user_id side) removes their own assignment
    rows — mirrors the sessions.user_id CASCADE convention."""
    job_id = await _insert_job(conn)
    assigner = await _insert_user(conn, role="admin")
    assignee = await _insert_user(conn, role="hiring_manager")
    await conn.execute(INSERT_ASSIGNEE, job_id, assignee, assigner)

    await conn.execute("DELETE FROM users WHERE id = $1", assignee)

    remaining = await conn.fetchval(
        "SELECT count(*) FROM job_assignees WHERE user_id = $1", assignee
    )
    assert remaining == 0


# ── ON DELETE RESTRICT on assigned_by — the ADR-020 §1 safety guard ─────────


@pytest.mark.asyncio
async def test_deleting_the_assigning_user_is_restricted_when_assignment_is_live(
    conn: asyncpg.Connection,
) -> None:
    """ADR-020 §1's specific safety guard: 'if the assigning user is deleted,
    orphaned assignments are not silently cascaded; instead, the DELETE fails
    and the assignment must be explicitly cleared by a different admin
    first.' A string-match test cannot see this constraint enforced — only a
    real DELETE against a real live assignment can."""
    job_id = await _insert_job(conn)
    assigner = await _insert_user(conn, role="admin")
    assignee = await _insert_user(conn, role="hiring_manager")
    await conn.execute(INSERT_ASSIGNEE, job_id, assignee, assigner)

    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await conn.execute("DELETE FROM users WHERE id = $1", assigner)

    # And the assignment record must still be there — REJECTED, not partially
    # applied.
    still_present = await conn.fetchval(
        "SELECT count(*) FROM job_assignees WHERE job_id = $1 AND user_id = $2",
        job_id,
        assignee,
    )
    assert still_present == 1


@pytest.mark.asyncio
async def test_deleting_the_assigning_user_succeeds_once_no_live_assignment_remains(
    conn: asyncpg.Connection,
) -> None:
    """The RESTRICT only blocks while a live assignment references the
    assigner; clearing the assignment first must let the DELETE through."""
    job_id = await _insert_job(conn)
    assigner = await _insert_user(conn, role="admin")
    assignee = await _insert_user(conn, role="hiring_manager")
    await conn.execute(INSERT_ASSIGNEE, job_id, assignee, assigner)

    await conn.execute(
        "DELETE FROM job_assignees WHERE job_id = $1 AND user_id = $2",
        job_id,
        assignee,
    )
    # No longer restricted — the FK no longer has a referencing row.
    await conn.execute("DELETE FROM users WHERE id = $1", assigner)

    remaining = await conn.fetchval(
        "SELECT count(*) FROM users WHERE id = $1", assigner
    )
    assert remaining == 0


# ── assigned_at default ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_assigned_at_defaults_to_now(conn: asyncpg.Connection) -> None:
    job_id = await _insert_job(conn)
    assigner = await _insert_user(conn, role="admin")
    assignee = await _insert_user(conn, role="hiring_manager")

    before = datetime.now(UTC)
    await conn.execute(INSERT_ASSIGNEE, job_id, assignee, assigner)
    after = datetime.now(UTC)

    assigned_at = await conn.fetchval(
        "SELECT assigned_at FROM job_assignees WHERE job_id = $1 AND user_id = $2",
        job_id,
        assignee,
    )
    assert assigned_at is not None
    assert before <= assigned_at <= after
