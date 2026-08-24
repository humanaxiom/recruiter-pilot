"""Unit tests for ``src.services.resume_service`` withdrawal-lifecycle
functions — ``withdraw_resume`` / ``reinstate_resume`` / ``status_breakdown``
(ADR-026 decisions 1-3/5, FU-8, exclude-and-retain slice).

All I/O mocked (``conn.execute``/``conn.fetchval``/``conn.fetch``/
``conn.transaction``); the real Postgres round trip — the idempotent UPDATE
guard, the atomic audit/outbox write, and the replay-payload equality — is
covered by ``tests/integration/test_resume_withdraw_pg.py``.

**Two locked decisions this file bakes in** (see the FU-8 planning brief /
ADR-026):

1. **Reinstate REPLAYS the stored embeddings; it never re-embeds.**
   ``reinstate_resume`` re-enters the candidate into the pool by re-enqueuing
   an outbox ``resume.parsed`` event whose payload is the LAST DELIVERED
   ``resume.parsed`` payload for that résumé — no LLM/embedder call, and the
   function signature itself has no ``llm``/``embedder`` parameter (the same
   architectural guarantee ``project_resume`` carries for ADR-008).
2. **Withdraw enqueues a ``resume.withdrawn`` outbox event**, the signal the
   drainer (``src.worker.graph_tasks.project_to_graph``) uses to un-project
   the résumé from Neo4j (``src.worker.resume_tasks.unproject_resume``) —
   see ``test_worker_project_to_graph.py`` / ``test_worker_project_resume.py``
   for that half.

``src.services.resume_service.withdraw_resume`` / ``reinstate_resume`` /
``status_breakdown`` do not exist yet — RED half of the TDD cycle: every test
below fails at import or with an ``AttributeError``.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from src.errors import NotFoundError


def _flat_call_args(mock_call: Any) -> list[Any]:
    return list(mock_call.args) + list(mock_call.kwargs.values())


def _acm(return_value: Any = None) -> MagicMock:
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=return_value)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _mock_conn(
    *,
    exists: bool = True,
    execute_result: str = "UPDATE 1",
    fetchval_result: Any = None,
    fetch_result: list[Any] | None = None,
) -> MagicMock:
    """A mocked ``DbConn``. ``fetchval`` answers the existence probe with
    ``exists``'s truthiness UNLESS a caller-supplied ``fetchval_result`` is
    given (used by the reinstate-replay tests, which need ``fetchval`` to
    answer a DIFFERENT query — the last-delivered-payload lookup — after the
    existence probe already ran)."""
    conn = MagicMock(name="conn")
    conn.execute = AsyncMock(return_value=execute_result)
    conn.transaction = MagicMock(return_value=_acm())
    conn.fetch = AsyncMock(return_value=fetch_result or [])

    calls: list[Any] = []

    async def _fetchval(_query: str, *args: Any) -> Any:
        calls.append(args)
        # First fetchval call is always the existence probe.
        if len(calls) == 1:
            return uuid4() if exists else None
        return fetchval_result

    conn.fetchval = AsyncMock(side_effect=_fetchval)
    return conn


# ── withdraw_resume: existence guard ────────────────────────────────────────


@pytest.mark.asyncio
async def test_withdraw_resume_raises_not_found_for_a_missing_resume() -> None:
    from src.services.resume_service import withdraw_resume

    conn = _mock_conn(exists=False)

    with pytest.raises(NotFoundError):
        await withdraw_resume(
            conn,
            uuid4(),
            reason=None,
            actor_kind="user",
            actor_user_id=uuid4(),
            actor_service=None,
        )
    conn.execute.assert_not_called()


# ── withdraw_resume: happy path ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_withdraw_resume_applies_update_writes_audit_and_enqueues_outbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.services.resume_service as resume_service_mod
    from src.services.resume_service import withdraw_resume

    resume_id = uuid4()
    actor_id = uuid4()
    conn = _mock_conn(exists=True, execute_result="UPDATE 1")
    audit = AsyncMock(return_value=None)
    enqueue = AsyncMock(return_value=None)
    monkeypatch.setattr(resume_service_mod.audit_service, "record_audit", audit)
    monkeypatch.setattr(resume_service_mod.outbox_service, "enqueue_outbox", enqueue)

    await withdraw_resume(
        conn,
        resume_id,
        reason="accepted another offer",
        actor_kind="user",
        actor_user_id=actor_id,
        actor_service=None,
    )

    conn.execute.assert_awaited_once()
    update_args = _flat_call_args(conn.execute.await_args)
    assert resume_id in update_args
    assert "accepted another offer" in update_args

    audit.assert_awaited_once()
    audit_kwargs = audit.await_args.kwargs
    assert audit_kwargs["subject_type"] == "resume"
    assert audit_kwargs["subject_id"] == resume_id
    assert audit_kwargs["actor_kind"] == "user"
    assert audit_kwargs["actor_user_id"] == actor_id

    enqueue.assert_awaited_once()
    enqueue_kwargs = enqueue.await_args.kwargs
    assert enqueue_kwargs["aggregate"] == "resume"
    assert enqueue_kwargs["aggregate_id"] == resume_id
    assert enqueue_kwargs["event_type"] == "resume.withdrawn"


@pytest.mark.asyncio
async def test_withdraw_resume_allows_a_null_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.services.resume_service as resume_service_mod
    from src.services.resume_service import withdraw_resume

    conn = _mock_conn(exists=True, execute_result="UPDATE 1")
    monkeypatch.setattr(
        resume_service_mod.audit_service, "record_audit", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        resume_service_mod.outbox_service,
        "enqueue_outbox",
        AsyncMock(return_value=None),
    )

    await withdraw_resume(
        conn,
        uuid4(),
        reason=None,
        actor_kind="service",
        actor_user_id=None,
        actor_service="dev-anonymous",
    )

    update_args = _flat_call_args(conn.execute.await_args)
    assert None in update_args


@pytest.mark.asyncio
async def test_withdraw_resume_runs_update_audit_and_outbox_in_one_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Structural pin for atomicity — the real rollback-on-failure proof is
    integration-only (``test_resume_withdraw_pg.py``), but at the unit level
    we can at least pin that exactly one ``conn.transaction()`` context is
    opened around the whole withdraw (a mutant that opens per-statement
    transactions, or none at all, changes this call count)."""
    import src.services.resume_service as resume_service_mod
    from src.services.resume_service import withdraw_resume

    conn = _mock_conn(exists=True, execute_result="UPDATE 1")
    monkeypatch.setattr(
        resume_service_mod.audit_service, "record_audit", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        resume_service_mod.outbox_service,
        "enqueue_outbox",
        AsyncMock(return_value=None),
    )

    await withdraw_resume(
        conn,
        uuid4(),
        reason="x",
        actor_kind="user",
        actor_user_id=uuid4(),
        actor_service=None,
    )

    assert conn.transaction.call_count == 1


# ── withdraw_resume: idempotency — the guarded UPDATE applied zero rows ────


@pytest.mark.asyncio
async def test_withdraw_resume_already_withdrawn_writes_no_audit_or_outbox_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Idempotency: an already-withdrawn résumé's guarded UPDATE (``WHERE
    withdrawn_at IS NULL`` or equivalent) applies ZERO rows — simulated here
    by a mocked ``execute`` returning ``"UPDATE 0"``. The function must be a
    quiet no-op: no new ``audit_log`` row, no new outbox row. A mutant that
    writes audit/outbox unconditionally (ignoring the UPDATE's rowcount)
    fails this test."""
    import src.services.resume_service as resume_service_mod
    from src.services.resume_service import withdraw_resume

    conn = _mock_conn(exists=True, execute_result="UPDATE 0")
    audit = AsyncMock(return_value=None)
    enqueue = AsyncMock(return_value=None)
    monkeypatch.setattr(resume_service_mod.audit_service, "record_audit", audit)
    monkeypatch.setattr(resume_service_mod.outbox_service, "enqueue_outbox", enqueue)

    await withdraw_resume(
        conn,
        uuid4(),
        reason="second attempt",
        actor_kind="user",
        actor_user_id=uuid4(),
        actor_service=None,
    )

    audit.assert_not_awaited()
    enqueue.assert_not_awaited()


@pytest.mark.asyncio
async def test_withdraw_resume_already_withdrawn_does_not_raise() -> None:
    """Idempotent success, not a 409 — per ADR-026 §Decision 2, withdrawing an
    already-withdrawn résumé is a no-op success."""
    from src.services.resume_service import withdraw_resume

    conn = _mock_conn(exists=True, execute_result="UPDATE 0")

    await withdraw_resume(
        conn,
        uuid4(),
        reason=None,
        actor_kind="user",
        actor_user_id=uuid4(),
        actor_service=None,
    )  # must not raise


# ── reinstate_resume: existence guard ───────────────────────────────────────


@pytest.mark.asyncio
async def test_reinstate_resume_raises_not_found_for_a_missing_resume() -> None:
    from src.services.resume_service import reinstate_resume

    conn = _mock_conn(exists=False)

    with pytest.raises(NotFoundError):
        await reinstate_resume(
            conn,
            uuid4(),
            actor_kind="user",
            actor_user_id=uuid4(),
            actor_service=None,
        )
    conn.execute.assert_not_called()


# ── reinstate_resume: happy path — clears columns, audits, replays ─────────


def _last_payload() -> dict[str, Any]:
    return {
        "parsed": {"total_years_experience": 5, "skills": [], "chunks": []},
        "summary_emb": [0.1, 0.2, 0.3],
        "chunk_embs": {},
        "prompt_version": "resume_core_v1+resume_skills_v2",
        "job_id": str(uuid4()),
    }


@pytest.mark.asyncio
async def test_reinstate_resume_clears_columns_and_writes_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.services.resume_service as resume_service_mod
    from src.services.resume_service import reinstate_resume

    resume_id = uuid4()
    payload = _last_payload()
    conn = _mock_conn(
        exists=True, execute_result="UPDATE 1", fetchval_result=json.dumps(payload)
    )
    audit = AsyncMock(return_value=None)
    monkeypatch.setattr(resume_service_mod.audit_service, "record_audit", audit)
    monkeypatch.setattr(
        resume_service_mod.outbox_service,
        "enqueue_outbox",
        AsyncMock(return_value=None),
    )

    await reinstate_resume(
        conn,
        resume_id,
        actor_kind="user",
        actor_user_id=uuid4(),
        actor_service=None,
    )

    conn.execute.assert_awaited_once()
    assert resume_id in _flat_call_args(conn.execute.await_args)

    audit.assert_awaited_once()
    kwargs = audit.await_args.kwargs
    assert kwargs["subject_type"] == "resume"
    assert kwargs["subject_id"] == resume_id


@pytest.mark.asyncio
async def test_reinstate_resume_enqueues_resume_parsed_with_last_delivered_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The load-bearing replay contract: reinstate must enqueue a
    ``resume.parsed`` outbox event whose payload is EXACTLY the last
    delivered ``resume.parsed`` payload — proving embeddings are replayed,
    never regenerated."""
    import src.services.resume_service as resume_service_mod
    from src.services.resume_service import reinstate_resume

    resume_id = uuid4()
    payload = _last_payload()
    conn = _mock_conn(
        exists=True, execute_result="UPDATE 1", fetchval_result=json.dumps(payload)
    )
    monkeypatch.setattr(
        resume_service_mod.audit_service, "record_audit", AsyncMock(return_value=None)
    )
    enqueue = AsyncMock(return_value=None)
    monkeypatch.setattr(resume_service_mod.outbox_service, "enqueue_outbox", enqueue)

    await reinstate_resume(
        conn,
        resume_id,
        actor_kind="user",
        actor_user_id=uuid4(),
        actor_service=None,
    )

    enqueue.assert_awaited_once()
    kwargs = enqueue.await_args.kwargs
    assert kwargs["aggregate"] == "resume"
    assert kwargs["aggregate_id"] == resume_id
    assert kwargs["event_type"] == "resume.parsed"
    assert kwargs["payload"] == payload


@pytest.mark.asyncio
async def test_reinstate_resume_accepts_an_already_parsed_dict_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """asyncpg may hand back JSONB as a pre-parsed ``dict`` (depending on the
    codec) rather than a raw JSON ``str`` — the function must not assume one
    shape and crash on the other."""
    import src.services.resume_service as resume_service_mod
    from src.services.resume_service import reinstate_resume

    resume_id = uuid4()
    payload = _last_payload()
    conn = _mock_conn(exists=True, execute_result="UPDATE 1", fetchval_result=payload)
    monkeypatch.setattr(
        resume_service_mod.audit_service, "record_audit", AsyncMock(return_value=None)
    )
    enqueue = AsyncMock(return_value=None)
    monkeypatch.setattr(resume_service_mod.outbox_service, "enqueue_outbox", enqueue)

    await reinstate_resume(
        conn,
        resume_id,
        actor_kind="user",
        actor_user_id=uuid4(),
        actor_service=None,
    )

    enqueue.assert_awaited_once()
    assert enqueue.await_args.kwargs["payload"] == payload


@pytest.mark.asyncio
async def test_reinstate_resume_with_no_prior_delivered_payload_enqueues_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A résumé withdrawn before it was ever successfully parsed has nothing
    to replay — reinstate must still clear the columns and audit, but must
    NOT enqueue a ``resume.parsed`` event with no payload to send."""
    import src.services.resume_service as resume_service_mod
    from src.services.resume_service import reinstate_resume

    resume_id = uuid4()
    conn = _mock_conn(exists=True, execute_result="UPDATE 1", fetchval_result=None)
    monkeypatch.setattr(
        resume_service_mod.audit_service, "record_audit", AsyncMock(return_value=None)
    )
    enqueue = AsyncMock(return_value=None)
    monkeypatch.setattr(resume_service_mod.outbox_service, "enqueue_outbox", enqueue)

    await reinstate_resume(
        conn,
        resume_id,
        actor_kind="user",
        actor_user_id=uuid4(),
        actor_service=None,
    )

    enqueue.assert_not_awaited()


# ── reinstate_resume: idempotency ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_reinstate_resume_not_withdrawn_writes_no_audit_or_outbox_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.services.resume_service as resume_service_mod
    from src.services.resume_service import reinstate_resume

    conn = _mock_conn(exists=True, execute_result="UPDATE 0")
    audit = AsyncMock(return_value=None)
    enqueue = AsyncMock(return_value=None)
    monkeypatch.setattr(resume_service_mod.audit_service, "record_audit", audit)
    monkeypatch.setattr(resume_service_mod.outbox_service, "enqueue_outbox", enqueue)

    await reinstate_resume(
        conn,
        uuid4(),
        actor_kind="user",
        actor_user_id=uuid4(),
        actor_service=None,
    )

    audit.assert_not_awaited()
    enqueue.assert_not_awaited()


# ── reinstate_resume: architectural — no LLM/embedder call, ever ──────────


def test_reinstate_resume_signature_has_no_llm_or_embedder_parameter() -> None:
    """The load-bearing architectural guarantee: reinstate REPLAYS the last
    delivered payload — it cannot even be called with an ``llm``/``embedder``,
    mirroring ``project_resume``'s own ADR-008 guarantee."""
    import inspect

    from src.services.resume_service import reinstate_resume

    params = set(inspect.signature(reinstate_resume).parameters)
    assert not params & {"llm", "embedder"}


# ── status_breakdown ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_status_breakdown_zero_resume_job_is_all_zero_buckets() -> None:
    from src.schemas.resumes import ResumeStatusBreakdown
    from src.services.resume_service import status_breakdown

    conn = MagicMock(name="conn")
    conn.fetch = AsyncMock(return_value=[])

    result = await status_breakdown(conn, uuid4())

    assert isinstance(result, ResumeStatusBreakdown)
    assert result.uploaded == 0
    assert result.parsing == 0
    assert result.parsed == 0
    assert result.failed == 0
    assert result.withdrawn == 0
    assert result.degraded == 0  # ADR-030: sub-count, zero too


@pytest.mark.asyncio
async def test_status_breakdown_maps_bucket_rows_onto_the_five_fields() -> None:
    from src.services.resume_service import status_breakdown

    class _Row(dict[str, Any]):
        def __getitem__(self, key: str) -> Any:
            return dict.get(self, key)

    rows = [
        _Row({"bucket": "uploaded", "n": 2}),
        _Row({"bucket": "parsing", "n": 1}),
        _Row({"bucket": "parsed", "n": 7}),
        _Row({"bucket": "failed", "n": 3}),
        _Row({"bucket": "withdrawn", "n": 4}),
    ]
    conn = MagicMock(name="conn")
    conn.fetch = AsyncMock(return_value=rows)

    result = await status_breakdown(conn, uuid4())

    assert result.uploaded == 2
    assert result.parsing == 1
    assert result.parsed == 7
    assert result.failed == 3
    assert result.withdrawn == 4


@pytest.mark.asyncio
async def test_status_breakdown_maps_degraded_bucket_row_as_a_sub_count() -> None:
    """ADR-030: `degraded` rides ALONGSIDE `parsed` in the same bucket-row
    query shape (a 6th `bucket='degraded'` row), never replacing it."""
    from src.services.resume_service import status_breakdown

    class _Row(dict[str, Any]):
        def __getitem__(self, key: str) -> Any:
            return dict.get(self, key)

    rows = [
        _Row({"bucket": "uploaded", "n": 2}),
        _Row({"bucket": "parsing", "n": 1}),
        _Row({"bucket": "parsed", "n": 7}),
        _Row({"bucket": "failed", "n": 3}),
        _Row({"bucket": "withdrawn", "n": 4}),
        _Row({"bucket": "degraded", "n": 2}),
    ]
    conn = MagicMock(name="conn")
    conn.fetch = AsyncMock(return_value=rows)

    result = await status_breakdown(conn, uuid4())

    assert result.parsed == 7, "parsed must stay the FULL parsed count"
    assert result.degraded == 2, "degraded is an ADDITIONAL sub-count of parsed"


@pytest.mark.asyncio
async def test_status_breakdown_filters_by_the_given_job_id() -> None:
    from src.services.resume_service import status_breakdown

    conn = MagicMock(name="conn")
    conn.fetch = AsyncMock(return_value=[])
    job_id: UUID = uuid4()

    await status_breakdown(conn, job_id)

    conn.fetch.assert_awaited_once()
    assert job_id in _flat_call_args(conn.fetch.await_args)
