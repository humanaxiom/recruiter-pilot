"""Unit tests for ``src.services.reveal_service.record_reveal`` (FU-1, new) —
the append-only audit sink behind the AUDITED candidate-reveal action.

Reveal IS the de-anonymization action, so every reveal must leave an audit
trail. ``record_reveal`` is a PURE insert: it writes one ``reveal_audit`` row
and returns its id. No decryption happens here (that is ``get_one``'s job).

All I/O is mocked; the real Postgres round trip is covered by the DDL guard in
``test_ddl.py`` (the table is created idempotently on startup, no migration
framework). ``src.services.reveal_service`` does not exist yet — RED half of
the TDD cycle: every test below fails at import.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest


def _mock_conn() -> MagicMock:
    conn = MagicMock(name="conn")
    conn.execute = AsyncMock(return_value="INSERT 0 1")
    return conn


@pytest.mark.asyncio
async def test_record_reveal_inserts_one_row_and_returns_uuid() -> None:
    from src.services.reveal_service import record_reveal

    conn = _mock_conn()
    resume_id = uuid4()
    job_id = uuid4()

    audit_id = await record_reveal(
        conn,
        resume_id=resume_id,
        actor="alice@example.test",
        job_id=job_id,
        context="shortlist",
    )

    assert isinstance(audit_id, UUID)
    conn.execute.assert_awaited_once()
    args = conn.execute.await_args.args
    sql = args[0]
    assert "INSERT INTO reveal_audit" in sql
    # The minted id is the first bound arg AND the returned value (app-minted
    # UUID like the résumé-upload path — no RETURNING round trip needed).
    assert args[1] == audit_id
    assert resume_id in args
    assert job_id in args
    assert "alice@example.test" in args
    assert "shortlist" in args


@pytest.mark.asyncio
async def test_record_reveal_allows_null_actor_job_and_context() -> None:
    from src.services.reveal_service import record_reveal

    conn = _mock_conn()
    resume_id = uuid4()

    audit_id = await record_reveal(conn, resume_id=resume_id, actor=None)

    assert isinstance(audit_id, UUID)
    args = conn.execute.await_args.args
    # resume_id is mandatory; job_id / actor / context default to NULL.
    assert resume_id in args
    assert None in args


@pytest.mark.asyncio
async def test_record_reveal_is_pure_insert_no_decrypt(monkeypatch: Any) -> None:
    """The audit sink must never touch PII — it does no ``set_pii_key`` /
    ``pgp_sym_decrypt``. Assert it only issues the single INSERT and reads
    nothing back."""
    from src.services.reveal_service import record_reveal

    conn = _mock_conn()
    conn.fetchrow = AsyncMock()
    conn.fetchval = AsyncMock()
    conn.fetch = AsyncMock()

    await record_reveal(conn, resume_id=uuid4(), actor="api")

    conn.fetchrow.assert_not_awaited()
    conn.fetchval.assert_not_awaited()
    conn.fetch.assert_not_awaited()
