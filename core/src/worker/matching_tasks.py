"""arq tasks for the ranking write path (Phase 4d).

``shortlist_job(ctx, job_id_str)``  — rank a job's résumé pool -> shortlist_entries.
``reverse_match_job(ctx, resume_id_str)`` — rank a résumé against parsed jobs
-> reverse_match_entries.

Mirrors ``worker/tasks.py::parse_job`` / ``resume_tasks.py::parse_resume``:
deps resolve off ``ctx`` (``pg_pool`` / ``neo4j`` / ``llm`` / ``embedder``), the
task returns a short status string, and the persist runs inside ONE
``conn.transaction()``.

ADR-009 REQUIREMENT 1 lands here: ``MatchingContext`` is built via
``matching_context_from_settings(get_settings(), ...)`` and ``MatchWeights`` via
``weights_from_settings(get_settings())`` — never the orchestrator's in-code /
``DEFAULT_WEIGHTS`` fallbacks. These are the real construction sites the 4c
settings bridge was proven for.

Status strings:

* ``"missing"``     — the job/résumé row is absent; the orchestrator is NEVER
  called.
* ``"not_parsed"``  — the row exists but isn't parsed (job
  ``description_parsed IS NULL`` / résumé ``status != 'parsed'``); the
  orchestrator is NEVER called.
* ``"persisted"``   — a non-empty result was ranked and written.
* ``"empty"``       — zero candidates ranked; STILL persisted (DELETE-first
  clears any stale prior run), but a distinct status so "ranked, zero results"
  is told apart from "ranked, wrote N rows".
* ``"already_running"`` — a concurrent duplicate run for the same entity
  already holds the Postgres advisory lock (``src.worker.job_lock``,
  ADR-010 §1 residual); the row is never even fetched and the orchestrator is
  never called.
* ``awaiting_llm`` — FU-7 §2 (ADR-021 §2 / ADR-029): ranking failed CLOSED (a
  Mode A/B LLM failure, ``RankingUnavailableError``) and the arq retry ceiling
  (``settings.shortlist_max_tries``) has been reached, so the run gives up
  WITHOUT persisting a degraded shortlist. ``jobs.shortlist_state`` is left set
  to ``'awaiting_llm'`` (visible; the user can re-Generate). Below the ceiling
  the same failure raises ``arq.Retry`` instead of returning a status.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from arq import Retry

from src.pipeline.matching.orchestrator import (
    RankingUnavailableError,
    generate_shortlist,
    match_resume_to_jobs,
    matching_context_from_settings,
)
from src.services.shortlist_service import (
    clear_shortlist_state,
    persist_reverse_match,
    persist_shortlist,
    set_shortlist_awaiting_llm,
)
from src.settings import get_settings, weights_from_settings
from src.worker.job_lock import release_job_lock, try_job_lock

log = logging.getLogger(__name__)

_JOB_META_SQL = (
    "SELECT description_parsed, shortlist_top_percent FROM jobs WHERE id = $1"
)
_RESUME_STATUS_SQL = "SELECT status, withdrawn_at FROM resumes WHERE id = $1"
_PARSED_JOB_IDS_SQL = "SELECT id FROM jobs WHERE description_parsed IS NOT NULL"


_ELIGIBLE_SQL = """
SELECT count(*) FROM resumes
WHERE job_id = $1 AND status = 'parsed' AND withdrawn_at IS NULL
"""

_PROJECTED_CYPHER = "MATCH (r:Resume {job_id: $jid}) RETURN count(r) AS n"


async def ensure_projection_caught_up(
    conn: Any,
    neo4j: Any,
    *,
    job_id: UUID,
    job_try: int,
    max_tries: int,
    defer_s: float,
) -> None:
    """Defer ranking while the graph is behind Postgres. Found by smoke, 2026-08-22.

    ``resumes.status`` becomes ``parsed`` the instant the LLM pipeline finishes,
    but ranking reads Neo4j and projection is an async cron. In that window a
    résumé is *parsed* and *unrankable*: Stage 1 recall (``MATCH (r:Resume
    {job_id: $jid})``) does not see it, so it is dropped from the shortlist with
    no error and no trace. Three résumés parsed; two were ranked; the third —
    the one with the most skills — reappeared on a re-run minutes later with
    nothing else changed.

    **Only a graph BEHIND the eligible set defers.** Projection legitimately
    retains nodes Postgres no longer counts (a withdrawn résumé keeps its node),
    and deferring on that direction would wedge ranking permanently.

    **Bounded, because a failed projection is never coming.** Past
    ``max_tries`` the run proceeds and logs that it did: ranking a subset is the
    status quo, and doing it *silently* is the defect. Turning one broken row
    into a job that can never be ranked would be worse than the bug.

    **Fails OPEN on an unreadable graph**, uniquely in this repo. If Neo4j
    cannot be counted, the ranking immediately after will fail on its own and
    report properly; converting an unreadable count into an infinite defer would
    replace a loud failure with a silent one.
    """
    try:
        eligible = int(await conn.fetchval(_ELIGIBLE_SQL, job_id) or 0)
        if eligible == 0:
            return
        async with neo4j.session() as session:
            record = await (
                await session.run(_PROJECTED_CYPHER, jid=str(job_id))
            ).single()
        projected = int((record or {}).get("n") or 0)
    except Exception as exc:  # noqa: BLE001 - see "fails OPEN" above
        log.warning(
            "shortlist_job.projection_check_failed job_id=%s exc=%s", job_id, exc
        )
        return

    if projected >= eligible:
        return
    if job_try < max_tries:
        log.info(
            "shortlist_job.awaiting_projection job_id=%s projected=%d "
            "eligible=%d try=%d",
            job_id,
            projected,
            eligible,
            job_try,
        )
        raise Retry(defer=defer_s)
    log.warning(
        "shortlist_job.ranking_incomplete job_id=%s projected=%d eligible=%d "
        "— ranking a SUBSET after %d tries; a projection is stuck",
        job_id,
        projected,
        eligible,
        job_try,
    )


async def shortlist_job(ctx: dict[str, Any], job_id_str: str) -> str:
    """Generate + persist the shortlist for one job."""
    pool = ctx["pg_pool"]
    job_id = UUID(job_id_str)
    settings = get_settings()

    async with pool.acquire() as conn:
        if not await try_job_lock(conn, "shortlist", job_id):
            # fix/regenerate-shortlist-no-feedback: a concurrent duplicate run
            # genuinely holds the lock and is in flight — deliberately do NOT
            # touch shortlist_state here. It was set to 'ranking' by that
            # OTHER run's own enqueue (or is about to be), and clearing it
            # from this early return would blank the UI mid-run.
            log.info("shortlist_job.already_running job_id=%s", job_id_str)
            return "already_running"

        # fix/regenerate-shortlist-no-feedback: an unexpected exception
        # anywhere below (not caught as RankingUnavailableError) propagates
        # past this function with shortlist_state left at 'ranking' — there is
        # no blanket except/finally here to clear it, since arq's own retry
        # may still legitimately be in flight. The staleness bound
        # (settings.shortlist_ranking_stale_after_s, read by
        # get_shortlist_state) is the backstop: a 'ranking' row past that age
        # reads back as NOT ranking even though nothing wrote NULL over it.
        try:
            row = await conn.fetchrow(_JOB_META_SQL, job_id)
            if row is None:
                # fix/regenerate-shortlist-no-feedback: the row itself is gone
                # (deleted between enqueue and pickup), so this UPDATE affects
                # zero rows and must not raise — mirrors clear_shortlist_state's
                # own no-op-on-no-match contract.
                await clear_shortlist_state(conn, job_id)
                log.warning("shortlist_job.missing job_id=%s", job_id_str)
                return "missing"
            if row["description_parsed"] is None:
                # fix/regenerate-shortlist-no-feedback: no run is happening —
                # clear any 'ranking' the route set at enqueue so the UI stops
                # polling instead of being pinned on "Regenerating…" forever.
                await clear_shortlist_state(conn, job_id)
                log.info("shortlist_job.not_parsed job_id=%s", job_id_str)
                return "not_parsed"

            # Found by scripts/smoke.sh, 2026-08-22: three résumés parsed, two
            # were ranked. Graph projection is an async cron, so a résumé can be
            # `parsed` in Postgres and invisible to Stage 1 recall. Deferring
            # here costs seconds; not deferring silently drops a candidate.
            await ensure_projection_caught_up(
                conn,
                ctx["neo4j"],
                job_id=job_id,
                job_try=ctx.get("job_try", 1),
                max_tries=settings.shortlist_max_tries,
                defer_s=settings.shortlist_retry_defer_s,
            )

            mc = matching_context_from_settings(
                settings,
                db=conn,
                neo4j=ctx["neo4j"],
                llm=ctx["llm"],
                embedder=ctx["embedder"],
            )
            try:
                result = await generate_shortlist(
                    job_id,
                    ctx=mc,
                    weights=weights_from_settings(settings),
                    coarse_k=settings.match_coarse_k,
                    evidence_k=settings.match_evidence_k,
                    top_percent=row["shortlist_top_percent"],
                )
            except RankingUnavailableError as exc:
                # FU-7 §2 (ADR-021 §2 / ADR-029): fail CLOSED. Record the
                # visible ``awaiting_llm`` state (its own transaction, so it
                # commits even as the run bails out) and either let arq retry
                # (below the ceiling) or give up leaving the state set. The
                # ``finally`` below releases the advisory lock BEFORE the Retry
                # propagates, so the re-run can re-acquire it.
                async with conn.transaction():
                    await set_shortlist_awaiting_llm(conn, job_id, reason=str(exc))
                job_try = ctx.get("job_try", 1)
                if job_try < settings.shortlist_max_tries:
                    log.warning(
                        "shortlist_job.awaiting_llm job_id=%s try=%s reason=%s",
                        job_id_str,
                        job_try,
                        exc,
                    )
                    raise Retry(defer=settings.shortlist_retry_defer_s) from exc
                log.warning(
                    "shortlist_job.awaiting_llm_exhausted job_id=%s tries=%s",
                    job_id_str,
                    job_try,
                )
                return "awaiting_llm"

            async with conn.transaction():
                await persist_shortlist(conn, result)
                # A successful run clears any prior awaiting_llm flag (including
                # the "empty result" case) in the SAME transaction as the write.
                await clear_shortlist_state(conn, job_id)
        finally:
            await release_job_lock(conn, "shortlist", job_id)

    persisted = bool(result.entries)
    log.info(
        "shortlist_job.done job_id=%s entries=%d",
        job_id_str,
        len(result.entries),
    )
    return "persisted" if persisted else "empty"


async def reverse_match_job(ctx: dict[str, Any], resume_id_str: str) -> str:
    """Generate + persist the reverse match (résumé -> jobs) for one résumé.

    ``fix/regenerate-shortlist-no-feedback`` — checked and deliberately left
    alone: this task has no state column of its own (``jobs.shortlist_state``
    is keyed on a job, not a résumé) and no equivalent "regenerate shows a
    stale list" report exists for it. It relies solely on
    ``try_job_lock``/``already_running`` for in-flight detection, same as
    before this fix. Widening it is out of this branch's scope."""
    pool = ctx["pg_pool"]
    resume_id = UUID(resume_id_str)
    settings = get_settings()

    async with pool.acquire() as conn:
        if not await try_job_lock(conn, "reverse_match", resume_id):
            log.info("reverse_match_job.already_running resume_id=%s", resume_id_str)
            return "already_running"

        try:
            row = await conn.fetchrow(_RESUME_STATUS_SQL, resume_id)
            if row is None:
                log.warning("reverse_match_job.missing resume_id=%s", resume_id_str)
                return "missing"
            # ADR-026 (FU-8): a withdrawn résumé is excluded from ranking — a
            # DISTINCT status from "not_parsed" (checked FIRST so a withdrawn
            # row that also happens to be unparsed is reported as withdrawn),
            # the orchestrator is never called, and nothing is persisted.
            if row["withdrawn_at"] is not None:
                log.info("reverse_match_job.withdrawn resume_id=%s", resume_id_str)
                return "withdrawn"
            if row["status"] != "parsed":
                log.info("reverse_match_job.not_parsed resume_id=%s", resume_id_str)
                return "not_parsed"

            # Reverse match must never rank a résumé against an UNPARSED JD (no
            # required/nice-to-have skills to score against). Scope to parsed
            # jobs and pass an explicit set — never None (which means "no
            # filter" to match_resume_to_jobs).
            job_rows = await conn.fetch(_PARSED_JOB_IDS_SQL)
            allowed_job_ids: set[UUID] = {r["id"] for r in job_rows}

            mc = matching_context_from_settings(
                settings,
                db=conn,
                neo4j=ctx["neo4j"],
                llm=ctx["llm"],
                embedder=ctx["embedder"],
            )
            result = await match_resume_to_jobs(
                resume_id,
                ctx=mc,
                allowed_job_ids=allowed_job_ids,
                weights=weights_from_settings(settings),
                coarse_k=settings.match_coarse_k,
                evidence_k=settings.match_reverse_evidence_k,
            )

            async with conn.transaction():
                await persist_reverse_match(conn, result)
        finally:
            await release_job_lock(conn, "reverse_match", resume_id)

    persisted = bool(result.entries)
    log.info(
        "reverse_match_job.done resume_id=%s entries=%d",
        resume_id_str,
        len(result.entries),
    )
    return "persisted" if persisted else "empty"
