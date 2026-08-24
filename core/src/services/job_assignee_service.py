"""Per-job assignment CRUD (ADR-020 §1/§2, FU-6 slice 2).

Wraps the ``job_assignees`` table (``core/src/models/ddl.py``) created in
slice 1. ``assign`` is an upsert — re-assigning the same ``(job_id,
user_id)`` pair updates ``assigned_at``/``assigned_by`` in place rather than
raising a unique violation (ADR-020 §1: "re-assigning a user to the same job
is an update ... not a duplicate row"). ``unassign`` reports whether a row
was actually removed by parsing asyncpg's ``"DELETE <n>"`` execute status.
``list_assigned_job_ids`` powers the "all jobs for this user" query that
ADR-020 §3/§7 use to derive a scoped user's visible job set.
"""

from __future__ import annotations

from uuid import UUID

from src.services import DbConn

_ASSIGN_SQL = """
INSERT INTO job_assignees (job_id, user_id, assigned_by)
VALUES ($1, $2, $3)
ON CONFLICT (job_id, user_id) DO UPDATE
    SET assigned_at = now(),
        assigned_by = EXCLUDED.assigned_by
"""

_UNASSIGN_SQL = "DELETE FROM job_assignees WHERE job_id = $1 AND user_id = $2"

_LIST_ASSIGNED_JOB_IDS_SQL = """
SELECT job_id FROM job_assignees WHERE user_id = $1 ORDER BY assigned_at DESC
"""


async def assign(
    conn: DbConn, job_id: UUID, user_id: UUID, *, assigned_by: UUID
) -> None:
    """Assign ``user_id`` to ``job_id``, recording ``assigned_by``.

    Idempotent: a second call for the same ``(job_id, user_id)`` pair
    updates ``assigned_at``/``assigned_by`` in place rather than raising a
    unique violation or creating a duplicate row.
    """
    await conn.execute(_ASSIGN_SQL, job_id, user_id, assigned_by)


async def unassign(conn: DbConn, job_id: UUID, user_id: UUID) -> bool:
    """Remove the ``(job_id, user_id)`` assignment, if any.

    Returns whether a row was actually deleted, parsed from asyncpg's
    ``"DELETE <n>"`` execute status.
    """
    status = await conn.execute(_UNASSIGN_SQL, job_id, user_id)
    deleted_count = int(status.split()[-1])
    return deleted_count > 0


async def list_assigned_job_ids(conn: DbConn, user_id: UUID) -> list[UUID]:
    """Return every job id ``user_id`` is assigned to, most-recent first."""
    rows = await conn.fetch(_LIST_ASSIGNED_JOB_IDS_SQL, user_id)
    return [row["job_id"] for row in rows]
