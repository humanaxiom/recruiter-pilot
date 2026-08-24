"""Unit tests for ``src.services.job_assignee_service`` (ADR-020 §1/§2, FU-6
slice 2).

Nothing here exists yet (``core/src/services/job_assignee_service.py``), so
every test is expected to FAIL (RED) as an ``ImportError``/
``ModuleNotFoundError`` at collection.

Deliberately narrow scope, per CLAUDE.md's ``offline`` caveat: whether
``assign``'s ``INSERT ... ON CONFLICT DO UPDATE`` truly upserts instead of
raising a unique violation, and whether ``list_assigned_job_ids`` truly comes
back ``ORDER BY assigned_at DESC``, are real-Postgres behaviour a mocked
connection cannot prove — that is
``tests/integration/test_job_assignee_service_pg.py``'s job.

What IS genuinely unit-provable without a database:

* the three functions call the connection with ``job_id``/``user_id``/
  ``assigned_by`` passed as BOUND PARAMETERS (not interpolated into the SQL
  string) — captured via the bound call args, never by asserting on SQL text
  itself (SQL text is an implementation detail; a coder should be free to
  reformat it),
* ``assign`` returns ``None``,
* ``unassign`` turns a "row(s) affected" execute status into the correct
  ``bool``,
* ``list_assigned_job_ids`` turns fetched rows into a plain ``list[UUID]``
  and returns ``[]`` for no rows, without inventing rows that were not
  returned by the connection.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest

from src.services import job_assignee_service


class _FakeConn:
    """Records every call so tests can assert on bound parameters, never on
    SQL text."""

    def __init__(
        self,
        *,
        execute_result: str = "INSERT 0 1",
        fetch_rows: list[dict[str, Any]] | None = None,
    ) -> None:
        self._execute_result = execute_result
        self._fetch_rows = fetch_rows if fetch_rows is not None else []
        self.execute_calls: list[tuple[Any, ...]] = []
        self.fetch_calls: list[tuple[Any, ...]] = []

    async def execute(self, query: str, *args: Any) -> str:
        self.execute_calls.append(args)
        return self._execute_result

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        self.fetch_calls.append(args)
        return self._fetch_rows


# ── assign — bound parameters, return value ─────────────────────────────────


@pytest.mark.asyncio
async def test_assign_passes_job_id_user_id_and_assigned_by_as_bound_parameters() -> (
    None
):
    job_id = uuid4()
    user_id = uuid4()
    assigned_by = uuid4()
    conn = _FakeConn()

    await job_assignee_service.assign(conn, job_id, user_id, assigned_by=assigned_by)

    assert len(conn.execute_calls) == 1
    args = conn.execute_calls[0]
    assert job_id in args
    assert user_id in args
    assert assigned_by in args


@pytest.mark.asyncio
async def test_assign_returns_none() -> None:
    conn = _FakeConn()

    result = await job_assignee_service.assign(
        conn, uuid4(), uuid4(), assigned_by=uuid4()
    )

    assert result is None


@pytest.mark.asyncio
async def test_assign_requires_assigned_by_as_keyword_argument() -> None:
    """Mirrors ``user_service.provision_or_get``'s keyword-only convention —
    a positional ``assigned_by`` is a TypeError, not silently accepted."""
    conn = _FakeConn()

    with pytest.raises(TypeError):
        await job_assignee_service.assign(  # type: ignore[call-arg, misc]
            conn, uuid4(), uuid4(), uuid4()
        )


# ── unassign — execute status -> bool ───────────────────────────────────────


@pytest.mark.asyncio
async def test_unassign_returns_true_when_a_row_was_deleted() -> None:
    conn = _FakeConn(execute_result="DELETE 1")

    removed = await job_assignee_service.unassign(conn, uuid4(), uuid4())

    assert removed is True


@pytest.mark.asyncio
async def test_unassign_returns_false_when_no_row_was_deleted() -> None:
    conn = _FakeConn(execute_result="DELETE 0")

    removed = await job_assignee_service.unassign(conn, uuid4(), uuid4())

    assert removed is False


@pytest.mark.asyncio
async def test_unassign_passes_job_id_and_user_id_as_bound_parameters() -> None:
    job_id = uuid4()
    user_id = uuid4()
    conn = _FakeConn(execute_result="DELETE 1")

    await job_assignee_service.unassign(conn, job_id, user_id)

    assert len(conn.execute_calls) == 1
    args = conn.execute_calls[0]
    assert job_id in args
    assert user_id in args


# ── list_assigned_job_ids — rows -> list[UUID] ──────────────────────────────


@pytest.mark.asyncio
async def test_list_assigned_job_ids_returns_uuids_from_fetched_rows() -> None:
    job_a = uuid4()
    job_b = uuid4()
    conn = _FakeConn(fetch_rows=[{"job_id": job_a}, {"job_id": job_b}])

    result = await job_assignee_service.list_assigned_job_ids(conn, uuid4())

    assert result == [job_a, job_b]
    assert all(isinstance(job_id, UUID) for job_id in result)


@pytest.mark.asyncio
async def test_list_assigned_job_ids_returns_empty_list_when_no_rows_fetched() -> None:
    conn = _FakeConn(fetch_rows=[])

    result = await job_assignee_service.list_assigned_job_ids(conn, uuid4())

    assert result == []


@pytest.mark.asyncio
async def test_list_assigned_job_ids_passes_user_id_as_a_bound_parameter() -> None:
    user_id = uuid4()
    conn = _FakeConn(fetch_rows=[])

    await job_assignee_service.list_assigned_job_ids(conn, user_id)

    assert len(conn.fetch_calls) == 1
    assert user_id in conn.fetch_calls[0]


@pytest.mark.asyncio
async def test_list_assigned_job_ids_does_not_invent_rows() -> None:
    """The service must not silently pad/truncate — the returned list must
    be exactly what the connection handed back, in the same order."""
    job_ids = [uuid4(), uuid4(), uuid4()]
    conn = _FakeConn(fetch_rows=[{"job_id": jid} for jid in job_ids])

    result = await job_assignee_service.list_assigned_job_ids(conn, uuid4())

    assert result == job_ids
