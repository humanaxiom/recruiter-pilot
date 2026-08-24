"""Re-queue résumé parses that lost their job — the cron `extract.py` assumed.

**The durability gap this closes.** A `resumes` row is the source of truth for
"this candidate still needs parsing", but the WORK ITEM lives only in Redis.
Lose the queue — a flush, a worker crash mid-job, a purge, an arq job that
expires — and the row is stranded non-terminal forever with nothing left to
retry it. Until now `WorkerSettings.cron_jobs` held exactly one entry
(`project_to_graph`) and no reconciler existed, while
`pipeline/parsing/extract.py:126` described "the reconcile cron re-queues it
forever" as a present-tense property of the system. ROADMAP A7 again: a
mechanism relied on in reasoning and implemented nowhere.

**Why it matters here specifically.** The GPU peer is shared with other systems
and its capacity comes and goes. Without this, every window of contention
permanently strands whatever was in flight — `scripts/doctor.sh` found 20
résumés stuck since July, and three more were stranded in a single afternoon.
Work has to survive the periods when the model is unavailable and resume when it
returns; that is the entire requirement, and no timeout value delivers it.

**The bound matters as much as the retry.** `extract.py`'s warning is real: a
row that crashes the pipeline *before* reaching a terminal state would be
re-queued forever, burning an expensive LLM pass every time — on the very peer
that is already contended. So attempts are counted and capped, and a row past
the cap is failed with a reason a human can act on rather than looped.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

#: The ctx key holding the arq queue client. `worker/main.py::startup` sets
#: a dedicated ``ctx["arq"]``. The first version of this module read that key
#: before `main.py` set it: it found nothing, returned a bland "skipped", and did
#: precisely nothing on every tick from the moment it was deployed, while its
#: unit tests passed because they built the ctx the implementation expected
#: rather than the one the worker produces. That is ROADMAP A7 (18), "the test
#: that plays both parts", committed by the very session that named it.
#:
#: Named as a constant so the tests can pin it against main.py's real startup
#: instead of inventing it a second time.
_QUEUE_CTX_KEY = "arq"

#: How long a résumé may sit non-terminal before it is considered stranded
#: rather than busy. Generous on purpose: a real parse on a contended peer can
#: take many minutes, and a reconciler that fires while the original job is
#: still working would double the load on the resource already struggling.
_STALLED_AFTER = "30 minutes"

#: Rows re-queued per tick. A backlog of 500 stranded résumés must not become
#: 500 simultaneous LLM jobs the moment capacity returns — that would recreate
#: the contention that stranded them.
_BATCH = 10

#: Give-up threshold, honouring `extract.py`'s "re-queues it forever" warning.
#: A row that cannot reach a terminal state in this many reconciles is failing
#: for a reason retrying will not fix (the NUL-byte crash is the documented
#: example), and burning further LLM passes on it is pure waste.
_MAX_RECONCILE_ATTEMPTS = 3

#: Non-terminal statuses. `parsed` and `failed` are terminal: re-queueing either
#: would redo work already done, or resurrect a row a human was told had failed.
_SELECT_STALLED = f"""
SELECT id, status, uploaded_at, COALESCE(reconcile_attempts, 0) AS attempts
FROM resumes
WHERE status IN ('uploaded', 'parsing')
  AND withdrawn_at IS NULL
  AND uploaded_at < now() - interval '{_STALLED_AFTER}'
ORDER BY uploaded_at ASC
LIMIT {_BATCH}
"""

_BUMP_ATTEMPTS = """
UPDATE resumes
SET reconcile_attempts = COALESCE(reconcile_attempts, 0) + 1
WHERE id = $1
RETURNING COALESCE(reconcile_attempts, 0)
"""

_GIVE_UP = """
UPDATE resumes
SET status = 'failed', failure_reason = $2
WHERE id = $1 AND status IN ('uploaded', 'parsing')
"""


async def reconcile_stalled_parses(ctx: dict[str, Any]) -> str:
    """One reconcile tick. Returns a short status string for the worker log.

    **Never raises.** This runs as a cron inside the worker process, and an
    exception escaping it would kill scheduled execution for everything else
    in that process, graph projection included. A reconciler that takes the
    worker down is worse than no reconciler.
    """
    pool = ctx.get("pg_pool")
    arq = ctx.get(_QUEUE_CTX_KEY)
    if pool is None or arq is None:
        # LOUD, not a bland "skipped". The silent version of this line hid a
        # completely inert cron for hours.
        log.warning(
            "reconcile_stalled_parses.unavailable pg_pool=%s %s=%s — the cron "
            "cannot run and stranded résumés will NOT be recovered",
            pool is not None,
            _QUEUE_CTX_KEY,
            arq is not None,
        )
        return "unavailable"

    requeued = 0
    abandoned = 0
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(_SELECT_STALLED)
            for row in rows:
                resume_id = row["id"]
                attempts = await conn.fetchval(_BUMP_ATTEMPTS, resume_id)
                if attempts is not None and int(attempts) > _MAX_RECONCILE_ATTEMPTS:
                    await conn.execute(
                        _GIVE_UP,
                        resume_id,
                        (
                            f"abandoned after {_MAX_RECONCILE_ATTEMPTS} reconcile "
                            "attempts — the parse never reached a terminal state"
                        ),
                    )
                    abandoned += 1
                    continue
                # A DETERMINISTIC job id makes this idempotent for free: arq
                # refuses a job whose id is already queued or running, so a
                # tick that overlaps a legitimately in-flight parse is a no-op
                # rather than a duplicate LLM pass.
                await arq.enqueue_job(
                    "parse_resume",
                    str(resume_id),
                    _job_id=f"reconcile-parse-{resume_id}",
                )
                requeued += 1
    except Exception as exc:  # noqa: BLE001 - a cron must not kill the worker
        log.warning("reconcile_stalled_parses.failed exc=%s", exc)
        return "error"

    if requeued or abandoned:
        log.info(
            "reconcile_stalled_parses requeued=%d abandoned=%d", requeued, abandoned
        )
    return f"requeued={requeued} abandoned={abandoned}"


__all__ = ["reconcile_stalled_parses"]
