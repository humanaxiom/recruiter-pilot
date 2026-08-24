"""Unit tests for ``src.worker.graph_tasks.project_to_graph`` — the outbox
drainer that projects ``job.parsed``/``resume.parsed`` events into Neo4j.

All I/O mocked: a fake asyncpg pool/connection (``fetch``/``execute``/
``transaction``) and patched ``_project_job``/``project_resume`` dispatch
targets. Ported behaviourally from hris ``apps/worker/src/worker/tasks.py::
project_to_graph`` (extended by ``resume_tasks.py``'s overload), with the
human-locked deviations pinned:

* **Decision 2 — poison rows are capped, not retried forever.** hris's
  ``SELECT ... WHERE delivered_at IS NULL ORDER BY id LIMIT $1`` has no upper
  bound on ``delivery_attempts`` — a permanently-broken row (bad payload
  shape, a Neo4j constraint violation that never clears) is retried on every
  single drain pass, forever, and can starve its batch-mates if it always
  sorts first. Here the SELECT itself excludes rows at or past
  ``settings.outbox_max_delivery_attempts`` — dead-lettered, not deleted, not
  retried.
* **R3 (HIGH) — ``outbox.last_error`` and every log record are PII-FREE.**
  ``last_error`` is a cleartext TEXT column, and N1 (ADR-007 §7) lets
  structured résumé fields (company names, bullet text) ride the outbox
  unscrubbed — so a Neo4j ``ClientError``/``CypherTypeError`` whose message
  echoes a parameter value can leak PII into ``last_error`` the same way
  Phase 3's ``validation_error_digest`` was built to stop pydantic errors
  doing. ``f"{type(exc).__name__}: {exc}"`` (hris's exact pattern) is
  unsafe; only the exception's CLASS NAME may be recorded.
* **R7 (MED) — no ``FOR UPDATE SKIP LOCKED`` -> two concurrent drainers
  double-deliver.** Unit-pinned here on the SQL text; proven behaviourally
  against a real Postgres in the integration suite.
* Batch limit and the dead-letter cap are both read from
  ``settings.outbox_drain_batch_size`` / ``settings.outbox_max_delivery_attempts``
  — never hard-coded (see ``test_settings.py``).
* **ADR-026 (FU-8) — a THIRD dispatch branch, ``resume.withdrawn``.** The
  résumé-withdrawal un-project exclusion point: on this event the drainer
  calls ``src.worker.resume_tasks.unproject_resume`` (imported into this
  module the same way ``project_resume``/``_project_job`` already are), not
  either of the parse-projection functions.

``src.worker.graph_tasks.project_to_graph`` does not exist yet — this whole
file fails at collection (``ImportError``). RED half of the TDD cycle.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from src.worker.graph_tasks import project_to_graph

# ── fakes ─────────────────────────────────────────────────────────────────


def _acm(return_value: Any = None) -> MagicMock:
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=return_value)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _row(
    *,
    row_id: int = 1,
    aggregate: str = "resume",
    aggregate_id: Any | None = None,
    event_type: str = "resume.parsed",
    payload: dict[str, Any] | None = None,
    delivery_attempts: int = 0,
) -> dict[str, Any]:
    return {
        "id": row_id,
        "aggregate": aggregate,
        "aggregate_id": aggregate_id if aggregate_id is not None else uuid4(),
        "event_type": event_type,
        "payload": json.dumps(payload if payload is not None else {}),
        "delivery_attempts": delivery_attempts,
    }


def _make_ctx(conn: MagicMock) -> dict[str, Any]:
    pool = MagicMock(name="pg_pool")
    pool.acquire = MagicMock(return_value=_acm(conn))
    return {
        "pg_pool": pool,
        "neo4j": MagicMock(name="neo4j_driver"),
        "llm": MagicMock(name="llm"),
        "embedder": MagicMock(name="embedder"),
    }


def _make_conn(rows: list[dict[str, Any]]) -> MagicMock:
    conn = MagicMock(name="conn")
    conn.fetch = AsyncMock(return_value=rows)
    conn.execute = AsyncMock(return_value="UPDATE 1")
    conn.transaction = MagicMock(return_value=_acm())
    return conn


def _execute_calls(conn: MagicMock) -> list[tuple[Any, ...]]:
    return [tuple(call.args) for call in conn.execute.await_args_list]


# ── dispatch ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dispatches_job_parsed_to_project_job() -> None:
    row = _row(
        aggregate="job",
        event_type="job.parsed",
        payload={
            "embedding": [0.1],
            "extracted": {"title": "Engineer"},
            "prompt_version": "v1",
        },
    )
    conn = _make_conn([row])
    ctx = _make_ctx(conn)

    with (
        patch(
            "src.worker.graph_tasks._project_job", new_callable=AsyncMock
        ) as project_job,
        patch(
            "src.worker.graph_tasks.project_resume", new_callable=AsyncMock
        ) as project_resume,
    ):
        delivered = await project_to_graph(ctx)

    assert delivered == 1
    project_job.assert_awaited_once()
    project_resume.assert_not_awaited()
    flat = list(project_job.await_args.args) + list(
        project_job.await_args.kwargs.values()
    )
    assert ctx["neo4j"] in flat
    assert row["aggregate_id"] in flat


@pytest.mark.asyncio
async def test_dispatches_resume_parsed_to_project_resume() -> None:
    row = _row(
        aggregate="resume",
        event_type="resume.parsed",
        payload={"parsed": {}, "summary_emb": [0.1], "chunk_embs": {}, "job_id": None},
    )
    conn = _make_conn([row])
    ctx = _make_ctx(conn)

    with (
        patch(
            "src.worker.graph_tasks._project_job", new_callable=AsyncMock
        ) as project_job,
        patch(
            "src.worker.graph_tasks.project_resume", new_callable=AsyncMock
        ) as project_resume,
    ):
        delivered = await project_to_graph(ctx)

    assert delivered == 1
    project_resume.assert_awaited_once()
    project_job.assert_not_awaited()
    kwargs = project_resume.await_args.kwargs
    assert kwargs.get("resume_id") == row["aggregate_id"]


# ── ADR-026 (FU-8): resume.withdrawn -> unproject_resume ──────────────────


@pytest.mark.asyncio
async def test_dispatches_resume_withdrawn_to_unproject_resume() -> None:
    row = _row(
        aggregate="resume",
        event_type="resume.withdrawn",
        payload={},
    )
    conn = _make_conn([row])
    ctx = _make_ctx(conn)

    with (
        patch(
            "src.worker.graph_tasks._project_job", new_callable=AsyncMock
        ) as project_job,
        patch(
            "src.worker.graph_tasks.project_resume", new_callable=AsyncMock
        ) as project_resume,
        patch(
            "src.worker.graph_tasks.unproject_resume", new_callable=AsyncMock
        ) as unproject_resume,
    ):
        delivered = await project_to_graph(ctx)

    assert delivered == 1
    unproject_resume.assert_awaited_once()
    project_job.assert_not_awaited()
    project_resume.assert_not_awaited()
    flat = list(unproject_resume.await_args.args) + list(
        unproject_resume.await_args.kwargs.values()
    )
    assert ctx["neo4j"] in flat
    assert row["aggregate_id"] in flat


@pytest.mark.asyncio
async def test_resume_withdrawn_row_marked_delivered_on_success() -> None:
    row = _row(aggregate="resume", event_type="resume.withdrawn", payload={})
    conn = _make_conn([row])
    ctx = _make_ctx(conn)

    with (
        patch("src.worker.graph_tasks.unproject_resume", new_callable=AsyncMock),
        patch("src.worker.graph_tasks.project_resume", new_callable=AsyncMock),
        patch("src.worker.graph_tasks._project_job", new_callable=AsyncMock),
    ):
        await project_to_graph(ctx)

    calls = _execute_calls(conn)
    assert any("delivered_at" in c[0] and row["id"] in c for c in calls)


@pytest.mark.asyncio
async def test_resume_withdrawn_failure_increments_delivery_attempts_not_delivered() -> (  # noqa: E501
    None
):
    row = _row(aggregate="resume", event_type="resume.withdrawn", payload={})
    conn = _make_conn([row])
    ctx = _make_ctx(conn)

    with patch(
        "src.worker.graph_tasks.unproject_resume",
        new_callable=AsyncMock,
        side_effect=RuntimeError("neo4j delete failed"),
    ):
        delivered = await project_to_graph(ctx)

    assert delivered == 0
    calls = _execute_calls(conn)
    assert any("delivery_attempts" in c[0] and row["id"] in c for c in calls)
    assert not any("delivered_at" in c[0] and row["id"] in c for c in calls)


@pytest.mark.asyncio
async def test_unknown_event_type_does_not_dispatch_but_is_still_marked_delivered() -> (
    None
):
    """Matches hris: an unrecognised ``event_type`` is logged and skipped,
    not retried — there is nothing a future retry could resolve."""
    row = _row(event_type="something.unexpected")
    conn = _make_conn([row])
    ctx = _make_ctx(conn)

    with (
        patch(
            "src.worker.graph_tasks._project_job", new_callable=AsyncMock
        ) as project_job,
        patch(
            "src.worker.graph_tasks.project_resume", new_callable=AsyncMock
        ) as project_resume,
    ):
        delivered = await project_to_graph(ctx)

    project_job.assert_not_awaited()
    project_resume.assert_not_awaited()
    assert delivered == 1
    delivered_calls = [c for c in _execute_calls(conn) if "delivered_at" in c[0]]
    assert delivered_calls


# ── delivered_at / delivery_attempts ──────────────────────────────────────


@pytest.mark.asyncio
async def test_delivered_at_stamped_only_on_success() -> None:
    row = _row()
    conn = _make_conn([row])
    ctx = _make_ctx(conn)

    with patch("src.worker.graph_tasks.project_resume", new_callable=AsyncMock):
        await project_to_graph(ctx)

    calls = _execute_calls(conn)
    assert any("delivered_at" in c[0] and row["id"] in c for c in calls)
    assert not any("delivery_attempts" in c[0] for c in calls)


@pytest.mark.asyncio
async def test_delivery_attempts_incremented_and_no_delivered_at_on_failure() -> None:
    row = _row()
    conn = _make_conn([row])
    ctx = _make_ctx(conn)

    with patch(
        "src.worker.graph_tasks.project_resume",
        new_callable=AsyncMock,
        side_effect=RuntimeError("neo4j write failed"),
    ):
        delivered = await project_to_graph(ctx)

    assert delivered == 0
    calls = _execute_calls(conn)
    assert any("delivery_attempts" in c[0] and row["id"] in c for c in calls)
    assert not any("delivered_at" in c[0] and row["id"] in c for c in calls)


@pytest.mark.asyncio
async def test_a_failing_row_does_not_block_its_batch_mates() -> None:
    bad = _row(row_id=1)
    good = _row(row_id=2)
    conn = _make_conn([bad, good])
    ctx = _make_ctx(conn)

    async def _resume_side_effect(*args: Any, **kwargs: Any) -> None:
        if kwargs.get("resume_id") == bad["aggregate_id"]:
            raise RuntimeError("boom")

    with patch(
        "src.worker.graph_tasks.project_resume",
        new_callable=AsyncMock,
        side_effect=_resume_side_effect,
    ):
        delivered = await project_to_graph(ctx)

    assert delivered == 1
    calls = _execute_calls(conn)
    assert any("delivered_at" in c[0] and good["id"] in c for c in calls)
    assert any("delivery_attempts" in c[0] and bad["id"] in c for c in calls)


@pytest.mark.asyncio
async def test_returns_the_count_of_successfully_delivered_rows() -> None:
    rows = [_row(row_id=i) for i in range(1, 4)]
    conn = _make_conn(rows)
    ctx = _make_ctx(conn)
    with patch("src.worker.graph_tasks.project_resume", new_callable=AsyncMock):
        delivered = await project_to_graph(ctx)
    assert delivered == 3


# ── R3: PII-free error recording ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_last_error_never_contains_the_raw_exception_message() -> None:
    """R3 (HIGH). Force an exception whose ``str()`` contains a synthetic
    email/phone/candidate-name — the kind of string a Neo4j
    ``ClientError``/``CypherTypeError`` echoes from its own Cypher params
    (N1 lets these ride the outbox unscrubbed). ``last_error`` must carry
    NONE of it — only diagnostic, PII-free information (e.g. the exception's
    class name)."""
    row = _row()
    conn = _make_conn([row])
    ctx = _make_ctx(conn)
    leaky_message = (
        "Cannot merge node using null property value for "
        "casey.rivera@example.test, phone 555-0101, name 'Casey Rivera'"
    )

    with patch(
        "src.worker.graph_tasks.project_resume",
        new_callable=AsyncMock,
        side_effect=RuntimeError(leaky_message),
    ):
        await project_to_graph(ctx)

    calls = _execute_calls(conn)
    error_calls = [c for c in calls if "delivery_attempts" in c[0]]
    assert error_calls
    for call in error_calls:
        joined = " ".join(str(a) for a in call)
        assert "casey.rivera@example.test" not in joined
        assert "555-0101" not in joined
        assert "Casey Rivera" not in joined


@pytest.mark.asyncio
async def test_caplog_never_contains_pii_on_projection_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """R3, the log half of the same invariant — a stack trace or an
    ``exc_info``-logged message must not leak the same PII either."""
    row = _row()
    conn = _make_conn([row])
    ctx = _make_ctx(conn)
    leaky_message = (
        "constraint violation for casey.rivera@example.test / 555-0101 / Casey Rivera"
    )

    with (
        caplog.at_level(logging.DEBUG),
        patch(
            "src.worker.graph_tasks.project_resume",
            new_callable=AsyncMock,
            side_effect=RuntimeError(leaky_message),
        ),
    ):
        await project_to_graph(ctx)

    all_text = "\n".join(record.getMessage() for record in caplog.records)
    assert "casey.rivera@example.test" not in all_text
    assert "555-0101" not in all_text
    assert "Casey Rivera" not in all_text


# ── batch limit + dead-letter cap read from settings ──────────────────────


@pytest.mark.asyncio
async def test_batch_limit_defaults_to_settings_outbox_drain_batch_size() -> None:
    from src.settings import Settings

    conn = _make_conn([])
    ctx = _make_ctx(conn)
    custom = Settings(outbox_drain_batch_size=7)

    with patch("src.worker.graph_tasks.get_settings", return_value=custom):
        await project_to_graph(ctx)

    args = conn.fetch.await_args.args
    assert 7 in args


@pytest.mark.asyncio
async def test_batch_limit_override_is_honored_over_settings() -> None:
    conn = _make_conn([])
    ctx = _make_ctx(conn)
    await project_to_graph(ctx, batch=13)
    args = conn.fetch.await_args.args
    assert 13 in args


@pytest.mark.asyncio
async def test_select_query_bounds_delivery_attempts_below_the_dead_letter_cap() -> (
    None
):
    """Decision 2. The SELECT itself must exclude rows at/past the cap — the
    dead-letter behaviour is "never selected again", not a separate flag."""
    from src.settings import Settings

    conn = _make_conn([])
    ctx = _make_ctx(conn)
    custom = Settings(outbox_max_delivery_attempts=42)

    with patch("src.worker.graph_tasks.get_settings", return_value=custom):
        await project_to_graph(ctx)

    sql, *params = conn.fetch.await_args.args
    assert re.search(r"delivery_attempts\s*<", sql, re.IGNORECASE)
    assert 42 in params


@pytest.mark.asyncio
async def test_select_query_uses_for_update_skip_locked() -> None:
    """R7 (MED) unit-level pin — proven behaviourally (two concurrent
    drainers, real Postgres) in the integration suite."""
    conn = _make_conn([])
    ctx = _make_ctx(conn)
    await project_to_graph(ctx)
    sql = conn.fetch.await_args.args[0]
    assert re.search(r"FOR\s+UPDATE\s+SKIP\s+LOCKED", sql, re.IGNORECASE)


@pytest.mark.asyncio
async def test_select_query_orders_by_id_ascending() -> None:
    conn = _make_conn([])
    ctx = _make_ctx(conn)
    await project_to_graph(ctx)
    sql = conn.fetch.await_args.args[0]
    assert re.search(r"ORDER\s+BY\s+id\s+ASC", sql, re.IGNORECASE)


@pytest.mark.asyncio
async def test_whole_batch_is_processed_under_one_transaction() -> None:
    """Holding one Postgres transaction (and thus the row locks) for the
    WHOLE batch is what makes ``FOR UPDATE SKIP LOCKED`` meaningful — a
    per-row transaction would release each lock before the next row's
    ``SELECT`` in a concurrent drainer even ran."""
    rows = [_row(row_id=i) for i in range(1, 4)]
    conn = _make_conn(rows)
    ctx = _make_ctx(conn)
    with patch("src.worker.graph_tasks.project_resume", new_callable=AsyncMock):
        await project_to_graph(ctx)
    assert conn.transaction.call_count == 1


# ── F5 (security re-audit): per-drain deadline + skill-resolution budget ──


def _skills_payload(n: int) -> dict[str, Any]:
    return {
        "parsed": {
            "skills": [{"name": f"skill-{i}"} for i in range(n)],
            "experience": [],
            "education": [],
            "chunks": [],
            "cover_letter_chunks": [],
        },
        "summary_emb": [0.1],
        "chunk_embs": {},
        "job_id": None,
    }


@pytest.mark.asyncio
async def test_skill_budget_estimate_tolerates_malformed_job_payload() -> None:
    """A poison ``job.parsed`` row whose ``extracted`` is not a dict (or is
    missing) must not crash the BUDGET ESTIMATE — the real error for a
    poison row surfaces from the projection call itself, inside the
    try/except, not from this pre-dispatch estimate."""
    row = _row(
        row_id=1,
        event_type="job.parsed",
        payload={"extracted": "not-a-dict"},
    )
    conn = _make_conn([row])
    ctx = _make_ctx(conn)
    with patch("src.worker.graph_tasks._project_job", new_callable=AsyncMock):
        delivered = await project_to_graph(ctx)
    assert delivered == 1


@pytest.mark.asyncio
async def test_skill_budget_estimate_tolerates_malformed_resume_payload() -> None:
    row = _row(
        row_id=1,
        event_type="resume.parsed",
        payload={"parsed": "not-a-dict"},
    )
    conn = _make_conn([row])
    ctx = _make_ctx(conn)
    with patch("src.worker.graph_tasks.project_resume", new_callable=AsyncMock):
        delivered = await project_to_graph(ctx)
    assert delivered == 1


@pytest.mark.asyncio
async def test_stops_dispatching_once_skill_budget_exhausted() -> None:
    """F5. A row whose skills would push the running total over
    ``settings.outbox_max_skill_resolutions_per_drain`` is left for the next
    drain tick — not attempted, not marked delivered, no attempts increment."""
    from src.settings import Settings

    row1 = _row(row_id=1, payload=_skills_payload(5))
    row2 = _row(row_id=2, payload=_skills_payload(5))
    conn = _make_conn([row1, row2])
    ctx = _make_ctx(conn)
    custom = Settings(outbox_max_skill_resolutions_per_drain=5)

    with (
        patch("src.worker.graph_tasks.get_settings", return_value=custom),
        patch("src.worker.graph_tasks.project_resume", new_callable=AsyncMock),
    ):
        delivered = await project_to_graph(ctx)

    assert delivered == 1
    calls = _execute_calls(conn)
    assert any("delivered_at" in c[0] and row1["id"] in c for c in calls)
    assert not any(row2["id"] in c for c in calls)  # untouched entirely


@pytest.mark.asyncio
async def test_always_attempts_at_least_one_row_even_over_skill_budget() -> None:
    """A single oversized row must not starve itself out of ever being
    processed — the budget only stops DISPATCHING FURTHER rows."""
    from src.settings import Settings

    row1 = _row(row_id=1, payload=_skills_payload(50))
    conn = _make_conn([row1])
    ctx = _make_ctx(conn)
    custom = Settings(outbox_max_skill_resolutions_per_drain=1)

    with (
        patch("src.worker.graph_tasks.get_settings", return_value=custom),
        patch(
            "src.worker.graph_tasks.project_resume", new_callable=AsyncMock
        ) as project_resume,
    ):
        delivered = await project_to_graph(ctx)

    assert delivered == 1
    project_resume.assert_awaited_once()


@pytest.mark.asyncio
async def test_stops_dispatching_once_deadline_exceeded() -> None:
    """F5. The second row is left untouched once the per-drain wall-clock
    deadline has passed."""
    row1 = _row(row_id=1, payload={})
    row2 = _row(row_id=2, payload={})
    conn = _make_conn([row1, row2])
    ctx = _make_ctx(conn)

    with (
        patch("src.worker.graph_tasks.time.monotonic", side_effect=[100.0, 999.0]),
        patch("src.worker.graph_tasks.project_resume", new_callable=AsyncMock),
    ):
        delivered = await project_to_graph(ctx)

    assert delivered == 1
    calls = _execute_calls(conn)
    assert any("delivered_at" in c[0] and row1["id"] in c for c in calls)
    assert not any(row2["id"] in c for c in calls)


@pytest.mark.asyncio
async def test_skill_budget_is_read_from_settings_not_hardcoded() -> None:
    from src.settings import Settings

    row1 = _row(row_id=1, payload=_skills_payload(2))
    row2 = _row(row_id=2, payload=_skills_payload(2))
    conn = _make_conn([row1, row2])
    ctx = _make_ctx(conn)
    # A generous default would let both rows through; a tight setting must
    # actually change the observed behaviour.
    custom = Settings(outbox_max_skill_resolutions_per_drain=2)

    with (
        patch("src.worker.graph_tasks.get_settings", return_value=custom),
        patch(
            "src.worker.graph_tasks.project_resume", new_callable=AsyncMock
        ) as project_resume,
    ):
        delivered = await project_to_graph(ctx)

    assert delivered == 1
    project_resume.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("deadline_seconds", "expected_delivered"),
    [
        # Generous deadline: the pinned 0.5s of "elapsed" wall-clock time
        # (below) is nowhere near it, so both rows are attempted.
        (100.0, 2),
        # Tight deadline: 0.5s has already blown past it, so row2 is left
        # for the next drain tick.
        (0.1, 1),
    ],
)
async def test_deadline_is_read_from_settings_not_hardcoded(
    deadline_seconds: float, expected_delivered: int
) -> None:
    """F5's per-drain wall-clock deadline must be genuinely READ from
    ``settings.outbox_drain_deadline_seconds`` on every call — not baked in
    as a literal (hris has no deadline at all; this module's own default
    happens to be ``4.0``, which is exactly the value a lazy hard-code could
    hide behind).

    ``time.monotonic`` is pinned to two calls exactly 0.5s apart: one for
    the initial ``deadline = time.monotonic() + settings...`` computation,
    one for row2's over-deadline check (row1 is always attempted
    unconditionally, so it never calls ``monotonic`` itself). A deadline
    setting of 100s comfortably absorbs that 0.5s elapse, so both rows are
    delivered; a deadline setting of 0.1s does not, so only row1 is.

    Mutation-proof: hard-coding ``deadline = time.monotonic() + 4.0`` at
    ``graph_tasks.py`` would absorb the 0.5s elapse in BOTH parametrised
    cases (0.5s < 4.0 regardless of the ``deadline_seconds`` setting), so
    the ``(0.1, 1)`` case would observe ``delivered == 2`` instead and fail.
    """
    from src.settings import Settings

    row1 = _row(row_id=1, payload=_skills_payload(2))
    row2 = _row(row_id=2, payload=_skills_payload(2))
    conn = _make_conn([row1, row2])
    ctx = _make_ctx(conn)
    custom = Settings(outbox_drain_deadline_seconds=deadline_seconds)

    with (
        patch("src.worker.graph_tasks.get_settings", return_value=custom),
        patch("src.worker.graph_tasks.time.monotonic", side_effect=[100.0, 100.5]),
        patch(
            "src.worker.graph_tasks.project_resume", new_callable=AsyncMock
        ) as project_resume,
    ):
        delivered = await project_to_graph(ctx)

    assert delivered == expected_delivered
    assert project_resume.await_count == expected_delivered


@pytest.mark.asyncio
async def test_no_rows_returns_zero_without_touching_neo4j() -> None:
    conn = _make_conn([])
    ctx = _make_ctx(conn)
    with (
        patch(
            "src.worker.graph_tasks.project_resume", new_callable=AsyncMock
        ) as project_resume,
        patch(
            "src.worker.graph_tasks._project_job", new_callable=AsyncMock
        ) as project_job,
    ):
        delivered = await project_to_graph(ctx)
    assert delivered == 0
    project_resume.assert_not_awaited()
    project_job.assert_not_awaited()
