"""arq task implementations — the JD half of the parse pipeline.

Ported from hris ``apps/worker/src/worker/tasks.py`` (see
``phase3-source-dossier.md`` §8), trimmed to ``parse_job`` in Phase 3; Phase 4b
adds ``_project_job``/``_job_projection_tx``, the JD half of graph projection.

Tasks resolve their dependencies from ``ctx``, which ``src/worker/main.py::
startup`` builds:

  - ``ctx["pg_pool"]``  asyncpg.Pool   (NOT hris's ``ctx["pool"]``)
  - ``ctx["llm"]``      src.pipeline.llm.LLMClient
  - ``ctx["embedder"]`` src.pipeline.llm.CachedEmbedder
  - ``ctx["blob_store"]`` src.storage.blob_store.BlobStore
  - ``ctx["neo4j"]``    neo4j.AsyncDriver

Phase 3 stops at: parse -> write Postgres -> enqueue an ``outbox`` row. Phase
4b's drainer (``src.worker.graph_tasks.project_to_graph``) consumes those rows
and calls ``_project_job`` below for every ``job.parsed`` event.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any
from uuid import UUID

from neo4j import AsyncDriver

from src.pipeline import skills_graph
from src.pipeline.llm import (
    REASONING_JSON_MIN_TOKENS,
    CachedEmbedder,
    LLMClient,
    LLMOutputInvalidError,
)
from src.pipeline.skills import build_summary_text
from src.prompts import load_prompt
from src.schemas import JDExtracted
from src.services import job_service, outbox_service

log = logging.getLogger(__name__)

_MAX_REASON_CHARS = 1000

_JOB_META_SQL = "SELECT description_raw, status FROM jobs WHERE id = $1"


async def parse_job(ctx: dict[str, Any], job_id_str: str) -> str:
    """Pull the JD, run LLM extraction, embed the summary, write back, and
    enqueue the outbox event Phase 4's drainer will project.

    Returns one of: ``"parsed"``, ``"missing"``, ``"stale"``, ``"failed"``.

    Idempotent: re-running against a job that already left 'draft' is a no-op
    (``job_service.record_parsed`` only updates rows still in 'draft').
    """
    pool = ctx["pg_pool"]
    llm: LLMClient = ctx["llm"]
    embedder: CachedEmbedder = ctx["embedder"]
    job_id = UUID(job_id_str)

    async with pool.acquire() as conn:
        row = await conn.fetchrow(_JOB_META_SQL, job_id)
        if row is None:
            log.warning("parse_job.missing job_id=%s", job_id_str)
            return "missing"
        if row["status"] != "draft":
            log.info(
                "parse_job.skipped job_id=%s status=%s reason=not-in-draft",
                job_id_str,
                row["status"],
            )
            return "stale"

        # Two distinct except branches on purpose: an invalid LLM payload is an
        # EXPECTED outcome with a small local model (logged at error, no stack),
        # whereas anything else is a genuine bug and gets a traceback. Both
        # record the failure on the row and return "failed" — and neither
        # enqueues an outbox row, because nothing was written to project.
        try:
            prompt = load_prompt("jd_extract_v1", jd_text=row["description_raw"])
            extracted = await llm.chat_json(
                prompt.messages,
                JDExtracted,
                max_tokens=REASONING_JSON_MIN_TOKENS,
                max_retries=1,
            )
        except LLMOutputInvalidError as exc:
            log.error("parse_job.llm_invalid job_id=%s error=%s", job_id_str, exc)
            await job_service.record_parse_failure(
                conn,
                job_id=job_id,
                reason=f"llm output invalid: {exc}"[:_MAX_REASON_CHARS],
            )
            return "failed"
        except Exception as exc:  # noqa: BLE001 — recorded on the row, not swallowed
            log.exception("parse_job.unexpected job_id=%s", job_id_str)
            await job_service.record_parse_failure(
                conn,
                job_id=job_id,
                reason=f"{type(exc).__name__}: {exc}"[:_MAX_REASON_CHARS],
            )
            return "failed"

        # The summary embed has the same failure split as the core LLM call:
        # a PERMANENT LLMOutputInvalidError (dim/count mismatch) is recorded on
        # the row and returns "failed" with NO outbox row — otherwise it escapes
        # uncaught, stranding the 'draft' row and re-burning the JD extraction on
        # every arq retry. A TRANSIENT LLMUnavailableError deliberately escapes
        # so arq retries the genuine Ollama outage. (No PII scrub here: the JD
        # summary carries no candidate identity.)
        summary_text = build_summary_text(extracted)
        try:
            [embedding] = await embedder.embed([summary_text])
        except LLMOutputInvalidError as exc:
            log.error("parse_job.embed_invalid job_id=%s error=%s", job_id_str, exc)
            await job_service.record_parse_failure(
                conn,
                job_id=job_id,
                reason=f"embedding failed: {exc}"[:_MAX_REASON_CHARS],
            )
            return "failed"

        # The write-back and its outbox row commit together or not at all.
        async with conn.transaction():
            applied = await job_service.record_parsed(
                conn,
                job_id=job_id,
                extracted=extracted,
                parsed_at=dt.datetime.now(dt.UTC),
            )
            if not applied:
                log.info(
                    "parse_job.race job_id=%s note=%s",
                    job_id_str,
                    "row left draft state mid-parse; dropping result",
                )
                return "stale"

            await outbox_service.enqueue_outbox(
                conn,
                aggregate="job",
                aggregate_id=job_id,
                event_type="job.parsed",
                payload={
                    "embedding": embedding,
                    "extracted": extracted.model_dump(),
                    "prompt_version": prompt.version,
                },
            )

    log.info(
        "parse_job.ok job_id=%s required_skills=%d nice_to_have=%d",
        job_id_str,
        len(extracted.required_skills),
        len(extracted.nice_to_have_skills),
    )
    return "parsed"


# ---------------- job.parsed graph projection (Phase 4b) -------------------
#
# Ported behaviourally from hris ``apps/worker/src/worker/tasks.py::
# _project_job`` / ``_job_projection_tx``, with the same Decision-3 (resolve
# skill names OUTSIDE the write transaction) and R8 (pinned label set — no
# Company/Institution) pins as the résumé side
# (``src.worker.resume_tasks.project_resume``).


async def _project_job(
    driver: AsyncDriver,
    job_id: Any,
    payload: dict[str, Any],
    *,
    llm: Any,
    embedder: Any,
) -> None:
    """Project one ``job.parsed`` outbox payload into Neo4j: the Job node +
    its REQUIRES/NICE_TO_HAVE skill edges."""
    extracted = payload["extracted"]
    embedding = payload["embedding"]

    skill_names = [s["name"] for s in extracted.get("required_skills", [])] + [
        s["name"] for s in extracted.get("nice_to_have_skills", [])
    ]

    async with driver.session() as session:
        # Decision 3: every embed()/chat_json() round trip happens HERE, on a
        # plain auto-commit session, before any write transaction opens.
        resolved_skills = await skills_graph.resolve_canonical_names(
            session, skill_names, llm=llm, embedder=embedder
        )
        await session.execute_write(
            _job_projection_tx,
            str(job_id),
            extracted,
            embedding,
            resolved_skills,
        )


async def _job_projection_tx(
    tx: Any,
    job_id: str,
    extracted: dict[str, Any],
    embedding: list[float],
    resolved_skills: dict[str, str | None],
) -> None:
    """The write-transaction callback. Architecturally cannot call an LLM or
    embedder — neither parameter exists on this signature."""
    # Upsert the Job node + summary embedding.
    await tx.run(
        """
        MERGE (j:Job {id: $jid})
        SET j.title = $title,
            j.summary_embedding = $emb,
            j.updated_at = datetime()
        """,
        jid=job_id,
        title=extracted["title"],
        emb=embedding,
    )

    # Drop old skill edges so re-parses don't accumulate cruft. F4 (security
    # re-audit): this MUST be a typed delete — the earlier untyped
    # ``-[r]->(:Skill) DELETE r`` deletes EVERY outgoing Job->Skill edge type,
    # which silently destroys any future Job->Skill edge type (e.g. a 4c/4d
    # addition) the moment one gets introduced. This module only ever CREATES
    # REQUIRES/NICE_TO_HAVE, so naming exactly those two here is not a
    # regression risk, and IS what stops the silent-data-loss class.
    await tx.run(
        "MATCH (:Job {id: $jid})-[r:REQUIRES|NICE_TO_HAVE]->(:Skill) DELETE r",
        jid=job_id,
    )

    for skill in extracted.get("required_skills", []):
        raw_name = skill["name"]
        # F6 (security re-audit): fail loud on a name resolve_canonical_names
        # was never asked to resolve — see the resume-side comment in
        # resume_tasks.py::_resume_projection_tx for the full rationale.
        if raw_name not in resolved_skills:
            raise skills_graph.UnresolvedSkillNameError(
                "job required-skill name has no resolution entry"
            )
        canonical = resolved_skills[raw_name]
        if canonical is None:
            # F3: shape-rejected as PII — drop this skill/edge silently.
            continue
        await skills_graph.ensure_categories(tx, canonical)
        # ADR-008: `display_name` is written ONLY from the job/JD side, in a
        # dedicated statement — a job description carries no candidate
        # identity, so stamping the RAW (cleartext) skill name here is always
        # safe, even when `canonical` is an opaque `h:<hash>` key for a
        # non-vocab skill. Kept as its own statement (not folded into the
        # REQUIRES MERGE below) so the edge write's own params never carry
        # the raw name.
        await tx.run(
            "MATCH (s:Skill {canonical_key: $cname}) SET s.display_name = $display",
            cname=canonical,
            display=raw_name,
        )
        await tx.run(
            """
            MATCH (j:Job {id: $jid}), (s:Skill {canonical_key: $cname})
            MERGE (j)-[r:REQUIRES]->(s)
            SET r.min_years = $miny, r.is_must_have = $must
            """,
            jid=job_id,
            cname=canonical,
            miny=skill.get("min_years"),
            must=True,
        )
        # Per-job display name (fix/skill-display-names): the node-level
        # property above is global and last-writer-wins — two jobs requiring
        # the same canonical skill with different wording ("ReactJS" vs
        # "React JS") would stomp each other. Stamping the JD's own wording on
        # THIS job's edge lets Stage 2 render each job's own text. Same
        # ADR-008 posture as the node write (JD text only, never résumé
        # text), and likewise its own statement so the edge MERGE's params
        # never carry the raw name.
        await tx.run(
            """
            MATCH (:Job {id: $jid})-[r:REQUIRES]->(:Skill {canonical_key: $cname})
            SET r.display_name = $display
            """,
            jid=job_id,
            cname=canonical,
            display=raw_name,
        )

    for skill in extracted.get("nice_to_have_skills", []):
        raw_name = skill["name"]
        if raw_name not in resolved_skills:
            raise skills_graph.UnresolvedSkillNameError(
                "job nice-to-have-skill name has no resolution entry"
            )
        canonical = resolved_skills[raw_name]
        if canonical is None:
            continue
        await skills_graph.ensure_categories(tx, canonical)
        await tx.run(
            "MATCH (s:Skill {canonical_key: $cname}) SET s.display_name = $display",
            cname=canonical,
            display=raw_name,
        )
        await tx.run(
            """
            MATCH (j:Job {id: $jid}), (s:Skill {canonical_key: $cname})
            MERGE (j)-[r:NICE_TO_HAVE]->(s)
            SET r.min_years = $miny
            """,
            jid=job_id,
            cname=canonical,
            miny=skill.get("min_years"),
        )
        # Per-job display name — see the REQUIRES branch above.
        await tx.run(
            """
            MATCH (:Job {id: $jid})-[r:NICE_TO_HAVE]->(:Skill {canonical_key: $cname})
            SET r.display_name = $display
            """,
            jid=job_id,
            cname=canonical,
            display=raw_name,
        )

    # R8: no Company/Institution writes from this module.
