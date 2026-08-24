"""RED pin — the reconcile cron that `extract.py` already documents as existing.

**The gap, and it is a durability one.** A résumé row is the source of truth for
"this candidate needs parsing", but the WORK ITEM lives only in Redis. Lose the
queue — a flush, a worker crash mid-job, a purge — and the row is stranded
non-terminal forever with nothing left to retry it. `WorkerSettings.cron_jobs`
contains exactly one entry (`project_to_graph`); no reconciler has ever existed.

`pipeline/parsing/extract.py:126` nonetheless describes the hazard of "the
reconcile cron re-queues it forever (burning an LLM pass each time)" as a
present-tense property of the system. Another ROADMAP A7 instance: a mechanism
described in prose, relied on in reasoning, implemented nowhere.

**Why it matters now rather than in general.** The GPU peer is shared with other
systems and its capacity comes and goes. Every window of contention strands
whatever was in flight, permanently — `scripts/doctor.sh` found 20 résumés stuck
since July, and three more were stranded today. Work that must survive the
periods when the model is unavailable, and resume when it returns, is the whole
requirement.

**The bound is as important as the retry.** `extract.py`'s warning is real: a
row that crashes the pipeline before reaching a terminal state would be
re-queued forever, burning an expensive LLM pass every time. So the reconciler
gives up and marks the résumé failed rather than looping, and says so in the
failure reason.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from src.worker import reconcile


class _Row(dict[str, Any]):
    def __getitem__(self, key: str) -> Any:
        return dict.get(self, key)


def _stalled(**over: Any) -> _Row:
    base: dict[str, Any] = {
        "id": uuid4(),
        "status": "parsing",
        "uploaded_at": dt.datetime(2026, 7, 1, tzinfo=dt.UTC),
    }
    base.update(over)
    return _Row(base)


def _ctx(rows: list[_Row]) -> tuple[dict[str, Any], MagicMock, MagicMock]:
    conn = MagicMock(name="conn")
    conn.fetch = AsyncMock(return_value=rows)
    conn.execute = AsyncMock(return_value="UPDATE 1")
    conn.fetchval = AsyncMock(return_value=0)
    acq = MagicMock()
    acq.__aenter__ = AsyncMock(return_value=conn)
    acq.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acq)
    arq = MagicMock(name="arq")
    arq.enqueue_job = AsyncMock(return_value=MagicMock())
    # Keyed by the SAME constant the implementation reads, so this fixture can
    # never again describe a ctx the worker does not build.
    return ({"pg_pool": pool, reconcile._QUEUE_CTX_KEY: arq}, conn, arq)


async def test_a_stranded_resume_is_re_enqueued() -> None:
    """The whole point: a row with no job left behind it gets one."""
    row = _stalled()
    ctx, _conn, arq = _ctx([row])
    await reconcile.reconcile_stalled_parses(ctx)
    arq.enqueue_job.assert_awaited()
    assert str(row["id"]) in str(arq.enqueue_job.await_args)


async def test_nothing_is_enqueued_when_nothing_is_stalled() -> None:
    """A cron that does work every tick on a healthy system is a cron people
    disable."""
    ctx, _conn, arq = _ctx([])
    await reconcile.reconcile_stalled_parses(ctx)
    assert not arq.enqueue_job.await_count


async def test_the_enqueue_is_deduped_by_a_deterministic_job_id() -> None:
    """Idempotence for free. arq refuses a job whose id is already queued or
    running, so a reconciler firing every minute cannot pile up duplicate
    parses for the same résumé while one is legitimately in flight — which
    would multiply load on the very peer that is already struggling."""
    row = _stalled()
    ctx, _conn, arq = _ctx([row])
    await reconcile.reconcile_stalled_parses(ctx)
    kwargs = arq.enqueue_job.await_args.kwargs
    assert kwargs.get("_job_id"), "no deterministic job id — duplicates can pile up"
    assert str(row["id"]) in kwargs["_job_id"]


async def test_the_sql_only_selects_non_terminal_rows() -> None:
    """`parsed` and `failed` are terminal. Re-queueing either would re-run an
    expensive LLM pass over work already done, or resurrect a row a human was
    told had failed."""
    ctx, conn, _arq = _ctx([])
    await reconcile.reconcile_stalled_parses(ctx)
    sql = str(conn.fetch.await_args.args[0]).lower()
    assert "'uploaded'" in sql and "'parsing'" in sql
    assert "'parsed'" not in sql and "'failed'" not in sql


async def test_only_rows_older_than_the_grace_period_are_touched() -> None:
    """A résumé uploaded ten seconds ago is not stalled, it is BUSY. Without a
    grace period the reconciler would fight the parse that is already running
    and double the load on a contended GPU."""
    ctx, conn, _arq = _ctx([])
    await reconcile.reconcile_stalled_parses(ctx)
    sql = str(conn.fetch.await_args.args[0]).lower()
    assert "interval" in sql or "now()" in sql


async def test_the_last_permitted_attempt_still_runs() -> None:
    """The boundary, stated explicitly because it is an off-by-one waiting to
    happen. ``reconcile_attempts`` counts attempts ALREADY MADE INCLUDING this
    one, so a row whose counter has just reached the cap is making its final
    permitted attempt — giving up here would silently allow only N-1."""
    ctx, conn, arq = _ctx([_stalled()])
    conn.fetchval = AsyncMock(return_value=reconcile._MAX_RECONCILE_ATTEMPTS)
    await reconcile.reconcile_stalled_parses(ctx)
    assert arq.enqueue_job.await_count == 1


async def test_a_row_past_the_attempt_cap_is_failed_not_re_enqueued() -> None:
    """`extract.py`'s warning, honoured: a row that crashes the pipeline before
    reaching a terminal state would otherwise be re-queued forever, burning an
    LLM pass each time on a peer shared with other systems."""
    row = _stalled()
    ctx, conn, arq = _ctx([row])
    conn.fetchval = AsyncMock(return_value=reconcile._MAX_RECONCILE_ATTEMPTS + 1)
    await reconcile.reconcile_stalled_parses(ctx)
    assert not arq.enqueue_job.await_count, "re-queued a row past its attempt cap"
    assert conn.execute.await_count, "gave up without marking the row failed"


async def test_the_failure_reason_says_why_a_human_should_not_wait() -> None:
    """A résumé stuck with a NULL reason is indistinguishable from one still
    working — which is exactly how 20 of them sat unnoticed since July."""
    row = _stalled()
    ctx, conn, _arq = _ctx([row])
    conn.fetchval = AsyncMock(return_value=reconcile._MAX_RECONCILE_ATTEMPTS + 1)
    await reconcile.reconcile_stalled_parses(ctx)
    written = " ".join(str(c) for c in conn.execute.await_args_list).lower()
    assert "reconcil" in written or "attempt" in written


async def test_one_tick_is_bounded() -> None:
    """A backlog of 500 stranded résumés must not become 500 simultaneous LLM
    jobs the instant capacity returns."""
    ctx, conn, _arq = _ctx([])
    await reconcile.reconcile_stalled_parses(ctx)
    sql = str(conn.fetch.await_args.args[0]).lower()
    assert "limit" in sql


@pytest.mark.parametrize("boom", [RuntimeError("pg down"), OSError("redis gone")])
async def test_a_failing_tick_never_takes_the_worker_down(boom: Exception) -> None:
    """This runs on a cron inside the worker process. An exception escaping it
    would kill scheduled execution for everything else, including graph
    projection."""
    ctx, _conn, arq = _ctx([_stalled()])
    arq.enqueue_job = AsyncMock(side_effect=boom)
    await reconcile.reconcile_stalled_parses(ctx)


# ── the contract must match the REAL worker, not one the test invented ───
#
# This file originally built its ctx as {"pg_pool": ..., "arq": ...} and every
# test passed. The worker sets ctx["redis"], never ctx["arq"], so the reconciler
# returned "skipped" on every tick from the moment it was deployed and did
# nothing at all — green tests, inert feature.
#
# That is ROADMAP A7 instance (18), "the test that plays both parts", committed
# by the same session that named it: a test which supplies the input the system
# was supposed to supply can never notice that the system does not.
#
# So the key is pinned against `worker/main.py`'s ACTUAL startup, by reading it.


def test_the_queue_ctx_key_is_one_the_worker_actually_sets() -> None:
    from pathlib import Path

    startup = (
        Path(__file__).resolve().parents[2] / "src" / "worker" / "main.py"
    ).read_text(encoding="utf-8")
    assert f'ctx["{reconcile._QUEUE_CTX_KEY}"] =' in startup, (
        f'reconcile reads ctx["{reconcile._QUEUE_CTX_KEY}"] but worker/main.py '
        "never assigns it — the cron will silently no-op forever"
    )


async def test_a_missing_queue_dependency_is_loud_not_silent() -> None:
    """Returning a bland "skipped" is how this went unnoticed. A cron that
    cannot do its job must say so in a word an operator would grep for."""
    ctx, _conn, _arq = _ctx([_stalled()])
    del ctx[reconcile._QUEUE_CTX_KEY]
    assert await reconcile.reconcile_stalled_parses(ctx) == "unavailable"
