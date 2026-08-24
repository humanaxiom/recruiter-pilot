"""Unit tests for ``src.services.audit_service.record_audit`` (FU-5 slice 8,
ADR-019 §6/§7) — the generalized, append-only ``audit_log`` writer that
replaces ``reveal_service.record_reveal`` on the reveal path. The OLD
``reveal_audit`` writer is KEPT, read-only, per ADR-019 §6's migration
posture — see ``test_services_reveal.py``, unchanged by this slice.

``record_audit`` is a PURE insert, mirroring ``reveal_service.record_reveal``'s
own discipline (see that module's docstring): one INSERT into ``audit_log``,
no read-back, no decryption. All I/O is mocked here; the real Postgres round
trip — including the ``audit_log_actor_identity`` CHECK constraint actually
firing against application-written rows for the first time — is covered by
``tests/integration/test_reveal_audit_log_pg.py`` (the constraint itself was
already proven directly in ``tests/integration/test_users_audit_pg.py`` at
slice 1).

``src.services.audit_service`` does not exist yet — RED half of the TDD
cycle: every test below fails at import.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest


def _mock_conn() -> MagicMock:
    conn = MagicMock(name="conn")
    conn.execute = AsyncMock(return_value="INSERT 0 1")
    return conn


@pytest.mark.asyncio
async def test_record_audit_inserts_a_user_actor_row() -> None:
    from src.services.audit_service import record_audit

    conn = _mock_conn()
    user_id = uuid4()
    resume_id = uuid4()
    job_id = uuid4()

    result = await record_audit(
        conn,
        actor_kind="user",
        actor_user_id=user_id,
        actor_service=None,
        action="reveal",
        subject_type="resume",
        subject_id=resume_id,
        job_id=job_id,
        context="shortlist review",
        details={"note": "ok"},
    )

    assert result is None
    conn.execute.assert_awaited_once()
    args = conn.execute.await_args.args
    sql = args[0]
    assert "INSERT INTO audit_log" in sql
    assert "user" in args
    assert user_id in args
    assert "reveal" in args
    assert "resume" in args
    assert resume_id in args
    assert job_id in args


@pytest.mark.asyncio
async def test_record_audit_inserts_a_service_actor_row() -> None:
    from src.services.audit_service import record_audit

    conn = _mock_conn()
    resume_id = uuid4()

    await record_audit(
        conn,
        actor_kind="service",
        actor_user_id=None,
        actor_service="dev-anonymous",
        action="reveal",
        subject_type="resume",
        subject_id=resume_id,
    )

    args = conn.execute.await_args.args
    assert "service" in args
    assert "dev-anonymous" in args
    # actor_user_id must be bound as NULL alongside the service actor.
    assert None in args


@pytest.mark.asyncio
async def test_record_audit_defaults_job_id_context_details_to_none() -> None:
    from src.services.audit_service import record_audit

    conn = _mock_conn()
    resume_id = uuid4()

    await record_audit(
        conn,
        actor_kind="user",
        actor_user_id=uuid4(),
        actor_service=None,
        action="reveal",
        subject_type="resume",
        subject_id=resume_id,
    )

    args = conn.execute.await_args.args
    # job_id/context/details are optional and must default to a bound SQL
    # NULL, not be silently omitted from a positional INSERT (which would
    # misalign every column after them).
    assert None in args


@pytest.mark.asyncio
async def test_record_audit_is_pure_insert_no_readback() -> None:
    """Mirrors ``reveal_service.record_reveal``'s own no-readback discipline
    (``test_services_reveal.py::test_record_reveal_is_pure_insert_no_decrypt``)
    — the generalized writer must not read anything back either, and must
    never touch PII."""
    from src.services.audit_service import record_audit

    conn = _mock_conn()
    conn.fetchrow = AsyncMock()
    conn.fetchval = AsyncMock()
    conn.fetch = AsyncMock()

    await record_audit(
        conn,
        actor_kind="service",
        actor_user_id=None,
        actor_service="worker",
        action="blind_review_toggled",
        subject_type="job",
        subject_id=uuid4(),
    )

    conn.fetchrow.assert_not_awaited()
    conn.fetchval.assert_not_awaited()
    conn.fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_record_audit_returns_none() -> None:
    """Unlike ``record_reveal`` (which mints and returns a ``UUID`` because
    the reveal route needed it), ``record_audit`` has no caller that needs
    the minted id back — it returns ``None``."""
    from src.services.audit_service import record_audit

    conn = _mock_conn()

    result = await record_audit(
        conn,
        actor_kind="user",
        actor_user_id=uuid4(),
        actor_service=None,
        action="role_changed",
        subject_type="user",
        subject_id=uuid4(),
    )

    assert result is None


@pytest.mark.asyncio
async def test_record_audit_passes_through_illegal_actor_combo_unvalidated() -> None:
    """Light touch on the CHECK contract: ``record_audit`` is a pure
    passthrough (like ``record_reveal``) — it does not defensively validate
    that exactly one actor field is set. That invariant is enforced by the
    REAL ``audit_log_actor_identity`` CHECK constraint
    (``tests/integration/test_users_audit_pg.py``), and by the route always
    supplying exactly one (``tests/unit/test_route_reveal.py``'s
    ``test_reveal_route_always_supplies_exactly_one_actor_field``). A caller
    that violates the invariant here gets whatever it asked for passed
    straight through — not a Python-side exception masking what should be a
    real DB rejection."""
    from src.services.audit_service import record_audit

    conn = _mock_conn()
    resume_id = uuid4()

    # An illegal combination (both actor fields set) is NOT rejected here.
    await record_audit(
        conn,
        actor_kind="user",
        actor_user_id=uuid4(),
        actor_service="also-set",
        action="reveal",
        subject_type="resume",
        subject_id=resume_id,
    )

    conn.execute.assert_awaited_once()
    args = conn.execute.await_args.args
    assert "also-set" in args
