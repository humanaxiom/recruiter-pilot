"""Integration tests — ``job_assignee_service`` (ADR-020 §1/§2, FU-6 slice 2)
against a REAL Postgres (testcontainers).

Slice 1 (``core/tests/integration/test_job_assignees_pg.py``) proved the
``job_assignees`` table's constraints directly against raw SQL. This file
proves the SERVICE LAYER wrapping that table — ``src/services/
job_assignee_service.py`` does not exist yet, so every test below is
expected to FAIL (RED) as an ``ImportError``/``ModuleNotFoundError`` at
collection.

The load-bearing behaviour here is real-Postgres-only and cannot be proven by
mocking a connection:

* ``assign``'s ``INSERT ... ON CONFLICT (job_id, user_id) DO UPDATE`` —
  re-assigning the same (job, user) pair must leave exactly ONE row and must
  UPDATE ``assigned_at``/``assigned_by`` in place, never insert a duplicate
  or raise a unique-violation (ADR-020 §1: "re-assigning a user to the same
  job is an update ... not a duplicate row").
* ``list_assigned_job_ids``'s ``ORDER BY assigned_at DESC`` — the "all jobs
  for this user" query that powers row-level scoping (ADR-020 §3) and
  ``GET /my/jobs`` (ADR-020 §7). Ordering is a property of the database, not
  of the Python that calls it.
* ``unassign``'s "was a row actually removed" signal, which only a real
  DELETE against a real row (vs. no matching row) can distinguish.

Follows the exact asyncpg/testcontainers fixture wiring already used in
``tests/integration/test_job_assignees_pg.py`` — no new harness.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime

import asyncpg
import pytest
from testcontainers.postgres import PostgresContainer

from src.models.ddl import init_schema
from src.services import job_assignee_service

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


# ── assign / list_assigned_job_ids — the happy path ─────────────────────────


@pytest.mark.asyncio
async def test_assign_creates_a_row_visible_via_list_assigned_job_ids(
    conn: asyncpg.Connection,
) -> None:
    job_id = await _insert_job(conn)
    assigner = await _insert_user(conn, role="admin")
    assignee = await _insert_user(conn, role="hiring_manager")

    result = await job_assignee_service.assign(
        conn, job_id, assignee, assigned_by=assigner
    )
    assert result is None

    job_ids = await job_assignee_service.list_assigned_job_ids(conn, assignee)
    assert job_ids == [job_id]

    count = await conn.fetchval(
        "SELECT count(*) FROM job_assignees WHERE job_id = $1 AND user_id = $2",
        job_id,
        assignee,
    )
    assert count == 1


@pytest.mark.asyncio
async def test_list_assigned_job_ids_returns_empty_list_for_user_with_no_assignments(
    conn: asyncpg.Connection,
) -> None:
    unassigned_user = await _insert_user(conn, role="hiring_manager")

    job_ids = await job_assignee_service.list_assigned_job_ids(conn, unassigned_user)

    assert job_ids == []


# ── re-assign idempotency — ADR-020 §1's ON CONFLICT DO UPDATE ─────────────


@pytest.mark.asyncio
async def test_reassigning_the_same_job_and_user_updates_in_place_not_a_duplicate(
    conn: asyncpg.Connection,
) -> None:
    """Calling ``assign`` twice for the same (job, user) must leave exactly
    ONE row in ``job_assignees``, and must update ``assigned_at`` (strictly
    later) and ``assigned_by`` (to the new assigner) — proving the
    ``ON CONFLICT (job_id, user_id) DO UPDATE`` semantics that a mocked
    connection cannot demonstrate."""
    job_id = await _insert_job(conn)
    first_assigner = await _insert_user(conn, role="admin")
    second_assigner = await _insert_user(conn, role="recruiter")
    assignee = await _insert_user(conn, role="hiring_manager")

    await job_assignee_service.assign(
        conn, job_id, assignee, assigned_by=first_assigner
    )
    first_assigned_at = await conn.fetchval(
        "SELECT assigned_at FROM job_assignees WHERE job_id = $1 AND user_id = $2",
        job_id,
        assignee,
    )

    await asyncio.sleep(0.05)  # ensure a distinguishable assigned_at tick

    await job_assignee_service.assign(
        conn, job_id, assignee, assigned_by=second_assigner
    )

    rows = await conn.fetch(
        "SELECT assigned_at, assigned_by FROM job_assignees "
        "WHERE job_id = $1 AND user_id = $2",
        job_id,
        assignee,
    )
    assert len(rows) == 1, "re-assignment must not create a duplicate row"
    assert rows[0]["assigned_at"] > first_assigned_at
    assert rows[0]["assigned_by"] == second_assigner

    total = await conn.fetchval(
        "SELECT count(*) FROM job_assignees WHERE job_id = $1", job_id
    )
    assert total == 1


@pytest.mark.asyncio
async def test_reassigning_does_not_raise_a_unique_violation(
    conn: asyncpg.Connection,
) -> None:
    """A naive plain INSERT would raise ``UniqueViolationError`` on the
    (job_id, user_id) PK for a second call — the service must not."""
    job_id = await _insert_job(conn)
    assigner = await _insert_user(conn, role="admin")
    assignee = await _insert_user(conn, role="hiring_manager")

    await job_assignee_service.assign(conn, job_id, assignee, assigned_by=assigner)
    # Must not raise.
    await job_assignee_service.assign(conn, job_id, assignee, assigned_by=assigner)


# ── list_assigned_job_ids — multiple jobs, ORDER BY assigned_at DESC ───────


@pytest.mark.asyncio
async def test_list_assigned_job_ids_orders_multiple_jobs_by_assigned_at_desc(
    conn: asyncpg.Connection,
) -> None:
    assigner = await _insert_user(conn, role="admin")
    assignee = await _insert_user(conn, role="hiring_manager")

    oldest_job = await _insert_job(conn, title="Oldest")
    await job_assignee_service.assign(conn, oldest_job, assignee, assigned_by=assigner)
    await asyncio.sleep(0.05)

    middle_job = await _insert_job(conn, title="Middle")
    await job_assignee_service.assign(conn, middle_job, assignee, assigned_by=assigner)
    await asyncio.sleep(0.05)

    newest_job = await _insert_job(conn, title="Newest")
    await job_assignee_service.assign(conn, newest_job, assignee, assigned_by=assigner)

    job_ids = await job_assignee_service.list_assigned_job_ids(conn, assignee)

    assert job_ids == [newest_job, middle_job, oldest_job]


@pytest.mark.asyncio
async def test_list_assigned_job_ids_only_returns_the_requested_users_jobs(
    conn: asyncpg.Connection,
) -> None:
    """A second user's assignments must not leak into the first user's list."""
    assigner = await _insert_user(conn, role="admin")
    user_a = await _insert_user(conn, role="hiring_manager")
    user_b = await _insert_user(conn, role="hiring_manager")

    job_for_a = await _insert_job(conn, title="For A")
    job_for_b = await _insert_job(conn, title="For B")
    await job_assignee_service.assign(conn, job_for_a, user_a, assigned_by=assigner)
    await job_assignee_service.assign(conn, job_for_b, user_b, assigned_by=assigner)

    assert await job_assignee_service.list_assigned_job_ids(conn, user_a) == [job_for_a]
    assert await job_assignee_service.list_assigned_job_ids(conn, user_b) == [job_for_b]


# ── unassign — deletes and reports whether a row was actually removed ─────


@pytest.mark.asyncio
async def test_unassign_removes_the_row_and_returns_true(
    conn: asyncpg.Connection,
) -> None:
    job_id = await _insert_job(conn)
    assigner = await _insert_user(conn, role="admin")
    assignee = await _insert_user(conn, role="hiring_manager")
    await job_assignee_service.assign(conn, job_id, assignee, assigned_by=assigner)

    removed = await job_assignee_service.unassign(conn, job_id, assignee)

    assert removed is True
    count = await conn.fetchval(
        "SELECT count(*) FROM job_assignees WHERE job_id = $1 AND user_id = $2",
        job_id,
        assignee,
    )
    assert count == 0


@pytest.mark.asyncio
async def test_unassign_a_second_time_returns_false(
    conn: asyncpg.Connection,
) -> None:
    job_id = await _insert_job(conn)
    assigner = await _insert_user(conn, role="admin")
    assignee = await _insert_user(conn, role="hiring_manager")
    await job_assignee_service.assign(conn, job_id, assignee, assigned_by=assigner)

    first = await job_assignee_service.unassign(conn, job_id, assignee)
    second = await job_assignee_service.unassign(conn, job_id, assignee)

    assert first is True
    assert second is False, "nothing left to remove on the second call"


@pytest.mark.asyncio
async def test_unassign_of_a_never_assigned_pair_returns_false(
    conn: asyncpg.Connection,
) -> None:
    job_id = await _insert_job(conn)
    never_assigned_user = await _insert_user(conn, role="hiring_manager")

    removed = await job_assignee_service.unassign(conn, job_id, never_assigned_user)

    assert removed is False


@pytest.mark.asyncio
async def test_unassign_removes_it_from_list_assigned_job_ids(
    conn: asyncpg.Connection,
) -> None:
    assigner = await _insert_user(conn, role="admin")
    assignee = await _insert_user(conn, role="hiring_manager")
    kept_job = await _insert_job(conn, title="Kept")
    doomed_job = await _insert_job(conn, title="Doomed")
    await job_assignee_service.assign(conn, kept_job, assignee, assigned_by=assigner)
    await job_assignee_service.assign(conn, doomed_job, assignee, assigned_by=assigner)

    await job_assignee_service.unassign(conn, doomed_job, assignee)

    job_ids = await job_assignee_service.list_assigned_job_ids(conn, assignee)
    assert job_ids == [kept_job]


@pytest.mark.asyncio
async def test_unassign_only_removes_the_targeted_pair(
    conn: asyncpg.Connection,
) -> None:
    """Unassigning (job, user_a) must not touch (job, user_b)."""
    assigner = await _insert_user(conn, role="admin")
    job_id = await _insert_job(conn)
    user_a = await _insert_user(conn, role="hiring_manager")
    user_b = await _insert_user(conn, role="hiring_manager")
    await job_assignee_service.assign(conn, job_id, user_a, assigned_by=assigner)
    await job_assignee_service.assign(conn, job_id, user_b, assigned_by=assigner)

    await job_assignee_service.unassign(conn, job_id, user_a)

    survivor = await conn.fetchval(
        "SELECT user_id FROM job_assignees WHERE job_id = $1", job_id
    )
    assert survivor == user_b


# ── assign — FK enforcement flows through the service, not just raw SQL ────


@pytest.mark.asyncio
async def test_assign_to_a_nonexistent_job_raises_foreign_key_violation(
    conn: asyncpg.Connection,
) -> None:
    assigner = await _insert_user(conn, role="admin")
    assignee = await _insert_user(conn, role="hiring_manager")

    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await job_assignee_service.assign(
            conn, uuid.uuid4(), assignee, assigned_by=assigner
        )


@pytest.mark.asyncio
async def test_assign_return_type_and_assigned_at_is_set(
    conn: asyncpg.Connection,
) -> None:
    job_id = await _insert_job(conn)
    assigner = await _insert_user(conn, role="admin")
    assignee = await _insert_user(conn, role="hiring_manager")

    before = datetime.now(UTC)
    await job_assignee_service.assign(conn, job_id, assignee, assigned_by=assigner)
    after = datetime.now(UTC)

    assigned_at = await conn.fetchval(
        "SELECT assigned_at FROM job_assignees WHERE job_id = $1 AND user_id = $2",
        job_id,
        assignee,
    )
    assert before <= assigned_at <= after
