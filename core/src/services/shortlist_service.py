"""Shortlist + reverse-match write path (Phase 4d).

Write-only persistence for the 4c orchestrator's results, raw asyncpg + hand-
written SQL (SQLAlchemy is dropped), mirroring ``job_service`` /
``resume_service`` / ``outbox_service``: every function takes an open
connection and leaves transaction scoping to the caller (the worker wraps each
persist in ONE ``conn.transaction()`` so the DELETE + re-INSERT-per-run commit
atomically).

Both functions are DELETE-first (clearing any stale prior run BEFORE inserting,
even when the new result is empty — a rerun that now yields nothing must still
clear the previous shortlist). The two paths are deliberate MIRROR IMAGES of
each other, dictated by the two tables' shapes (verified against
``src/models/ddl.py``):

* ``shortlist_entries`` has NO dedicated ``score_structured`` /
  ``score_evidence`` columns and ``evidence JSONB NOT NULL`` ->
  ``persist_shortlist`` FOLDS ``score_structured`` / ``score_evidence`` into the
  ``score_breakdown`` jsonb (losing them would be a data-loss bug) and coerces
  ``evidence=None`` to the JSON literal ``{}`` (never SQL NULL).
* ``reverse_match_entries`` HAS dedicated ``score_structured`` /
  ``score_evidence`` columns and a NULLABLE ``evidence JSONB`` ->
  ``persist_reverse_match`` writes those as their OWN SQL args (NOT folded into
  the breakdown jsonb) and passes ``evidence=None`` through as SQL NULL.

``pipeline_meta`` jsonb carries the full ``MatchWeights`` + ``git_sha`` on both
paths — it is serialised straight from the orchestrator's ``PipelineMeta``,
which already stamps both (Phase 4c wired ``git_sha`` through ``Settings`` ->
``MatchingContext`` -> ``PipelineMeta``).

No new PII redaction here (ADR-007 §6): stage-3 evidence quotes are written
verbatim, exactly as extracted from ``resumes.parsed`` chunk text. Any future
redaction is Phase 5's display-only concern and must not touch this at-rest
write path silently.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import logging
from collections.abc import Callable
from typing import Any
from uuid import UUID

from pydantic import ValidationError

from src.errors import NotFoundError
from src.pipeline.matching.orchestrator import JobMatchResult, ShortlistResult
from src.schemas.matching import (
    DEFAULT_WEIGHTS,
    EvidenceObject,
    JobMatchEntry,
    JobMatchResultOut,
    PipelineMeta,
    ScoreBreakdown,
    ShortlistEntry,
    ShortlistStateOut,
)
from src.schemas.resumes import ResumeParsed
from src.services import DbConn
from src.services.pii import set_pii_key
from src.services.redaction import (
    blind_label_map,
    pseudonym,
    redact_text,
    redacted_filename,
)
from src.settings import get_settings

logger = logging.getLogger(__name__)

_DELETE_SHORTLIST_SQL = "DELETE FROM shortlist_entries WHERE job_id = $1"

_INSERT_SHORTLIST_SQL = """
INSERT INTO shortlist_entries (
    job_id, resume_id, rank, score_final, score_breakdown, evidence, pipeline_meta
) VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb, $7::jsonb)
"""

_DELETE_REVERSE_SQL = "DELETE FROM reverse_match_entries WHERE resume_id = $1"

_INSERT_REVERSE_SQL = """
INSERT INTO reverse_match_entries (
    resume_id, job_id, rank, score_final, score_structured, score_evidence,
    score_breakdown, evidence, requirement_count, must_have_count, pipeline_meta
) VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8::jsonb, $9, $10, $11::jsonb)
"""


def _meta_json(meta: PipelineMeta | None) -> str:
    """Serialise the reproducibility stamp for the ``pipeline_meta`` jsonb
    column (``NOT NULL`` on both tables). ``model_dump_json`` handles the
    nested ``MatchWeights`` and the ``generated_at`` datetime; the rare
    ``None`` case (a job/résumé that vanished mid-run before the orchestrator
    stamped meta) still writes a valid, non-null object carrying the default
    weights so the ``NOT NULL`` constraint holds."""
    if meta is None:
        return json.dumps({"weights": DEFAULT_WEIGHTS.model_dump(mode="json")})
    return meta.model_dump_json()


async def persist_shortlist(conn: DbConn, result: ShortlistResult) -> int:
    """Replace the shortlist for ``result.job_id`` (DELETE-first, then one
    INSERT per ranked entry) and return the number of rows inserted."""
    await conn.execute(_DELETE_SHORTLIST_SQL, result.job_id)
    meta_json = _meta_json(result.pipeline_meta)
    for entry in result.entries:
        # shortlist_entries has no dedicated structured/evidence columns —
        # fold them into the breakdown jsonb so they are not lost.
        breakdown = entry.breakdown.model_dump()
        breakdown["score_structured"] = entry.score_structured
        breakdown["score_evidence"] = entry.score_evidence
        # ROADMAP A4 (evidence cliff): folded for the same reason and by the
        # same mechanism as the two sub-scores above — the table has no column
        # for it either. A row written before this slice simply has no key,
        # which unfolds to None ("we do not know whether stage 3 ran"), which
        # is exactly the third state the panel needs.
        breakdown["evidence_evaluated"] = entry.evidence_evaluated
        # evidence JSONB NOT NULL: None -> the empty-object literal, never NULL.
        evidence_json = (
            json.dumps(entry.evidence.model_dump())
            if entry.evidence is not None
            else "{}"
        )
        await conn.execute(
            _INSERT_SHORTLIST_SQL,
            result.job_id,
            entry.resume_id,
            entry.rank,
            entry.score_final,
            json.dumps(breakdown),
            evidence_json,
            meta_json,
        )
    return len(result.entries)


async def persist_reverse_match(conn: DbConn, result: JobMatchResult) -> int:
    """Replace the reverse-match result for ``result.resume_id`` (DELETE-first,
    then one INSERT per ranked entry) and return the number of rows inserted."""
    await conn.execute(_DELETE_REVERSE_SQL, result.resume_id)
    meta_json = _meta_json(result.pipeline_meta)
    for entry in result.entries:
        # reverse_match_entries HAS dedicated structured/evidence columns — keep
        # them out of the breakdown jsonb (mirror-image of the shortlist path).
        breakdown_json = json.dumps(entry.breakdown.model_dump())
        # evidence JSONB is nullable here: None passes through as SQL NULL.
        evidence_json = (
            json.dumps(entry.evidence.model_dump())
            if entry.evidence is not None
            else None
        )
        await conn.execute(
            _INSERT_REVERSE_SQL,
            result.resume_id,
            entry.job_id,
            entry.rank,
            entry.score_final,
            entry.score_structured,
            entry.score_evidence,
            breakdown_json,
            evidence_json,
            entry.requirement_count,
            entry.must_have_count,
            meta_json,
        )
    return len(result.entries)


# ── fail-closed shortlist state (FU-7 §2, ADR-021 §2 / ADR-029) ─────────────
#
# A shortlist run that fails closed (a Mode A/B LLM failure during ranking —
# ``RankingUnavailableError``) records a visible ``awaiting_llm`` state on the
# job's dedicated nullable columns instead of persisting a silently-degraded
# shortlist. The worker sets it before an arq retry and clears it on the next
# successful run; the status route surfaces it so the UI can distinguish
# "awaiting AI" from "still generating".

_SET_SHORTLIST_STATE_SQL = (
    "UPDATE jobs SET shortlist_state = 'awaiting_llm', "
    "shortlist_state_reason = $2, shortlist_state_at = now() WHERE id = $1"
)
# fix/regenerate-shortlist-no-feedback — a run genuinely in flight (set by the
# API route BEFORE it enqueues, so the very first poll after the POST returns
# already sees it true; overwritten/cleared by the worker on every terminal
# path — see matching_tasks.py).
_SET_SHORTLIST_RANKING_SQL = (
    "UPDATE jobs SET shortlist_state = 'ranking', "
    "shortlist_state_reason = NULL, shortlist_state_at = now() WHERE id = $1"
)
_CLEAR_SHORTLIST_STATE_SQL = (
    "UPDATE jobs SET shortlist_state = NULL, shortlist_state_reason = NULL, "
    "shortlist_state_at = NULL WHERE id = $1"
)
_GET_SHORTLIST_STATE_SQL = (
    "SELECT shortlist_state, shortlist_state_reason, shortlist_state_at "
    "FROM jobs WHERE id = $1"
)
# FU-6/ADR-020 §3 single-entity row-scoping predicate, mirroring
# ``job_service._JOB_ASSIGNEE_EXISTS_SQL`` VERBATIM — keyed on ``jobs.id`` (the
# read below is on the ``jobs`` table itself). This is deliberately NOT the
# sibling ``_SHORTLIST_ASSIGNEE_EXISTS_SQL`` above, which is keyed on
# ``{table}.job_id`` for reads OFF the shortlist tables (``jobs`` has no
# ``job_id`` column). A local constant keeps this off a cross-service import.
_JOBS_ASSIGNEE_EXISTS_SQL = (
    "EXISTS (SELECT 1 FROM job_assignees "
    "WHERE job_assignees.job_id = jobs.id "
    "AND job_assignees.user_id = $2)"
)


async def set_shortlist_awaiting_llm(
    conn: DbConn, job_id: UUID, *, reason: str
) -> None:
    """Record the fail-closed ``awaiting_llm`` state (with its reason + a
    ``now()`` timestamp) on ``job_id``. Clearing/overwriting is idempotent —
    a later run either clears it or re-sets it."""
    await conn.execute(_SET_SHORTLIST_STATE_SQL, job_id, reason)


async def set_shortlist_ranking(conn: DbConn, job_id: UUID) -> None:
    """Record the ``'ranking'`` state (a run genuinely in flight) with a
    ``now()`` timestamp on ``job_id``, clearing any stale ``reason``.
    ``fix/regenerate-shortlist-no-feedback`` — called by the API route
    synchronously, BEFORE ``arq.enqueue_job``, so the state is already true
    the instant the POST returns and the frontend's very first poll sees
    it."""
    await conn.execute(_SET_SHORTLIST_RANKING_SQL, job_id)


async def clear_shortlist_state(conn: DbConn, job_id: UUID) -> None:
    """Null out every ``shortlist_state*`` column for ``job_id``. A no-op on a
    job that carries no state (the columns are already NULL) — never raises."""
    await conn.execute(_CLEAR_SHORTLIST_STATE_SQL, job_id)


async def get_shortlist_state(
    conn: DbConn, job_id: UUID, *, user_id: UUID | None = None
) -> ShortlistStateOut | None:
    """Read the visible ranking state for ``job_id`` — either the fail-closed
    ``awaiting_llm`` state (FU-7 §2) or the in-flight ``ranking`` state
    (``fix/regenerate-shortlist-no-feedback``).

    Raises ``NotFoundError`` when the job does not resolve (mirroring every
    other single-entity read here — ``get_one`` / ``job_service.get_job``);
    returns ``None`` when the job resolves but carries no state at all, OR
    when it carries ``'ranking'`` whose ``shortlist_state_at`` is older than
    ``settings.shortlist_ranking_stale_after_s`` — a row that old cannot
    still be genuinely in flight (the worker would have been killed by its
    own job timeout), so a crashed worker must not permanently pin the UI on
    "Regenerating…". The stale row itself is left untouched — reported as
    not-ranking, never silently cleared.

    FU-6/ADR-020 §3/§5 (IDOR fix) — ``user_id`` row-scopes the read to a
    hiring_manager SESSION's assigned jobs via an ``EXISTS`` predicate against
    ``job_assignees``, keyed on ``jobs.id`` exactly like
    ``job_service.get_job``. A job that exists but is NOT assigned to
    ``user_id`` is filtered out at the SQL layer (0 rows) and surfaces as
    ``NotFoundError`` — observationally IDENTICAL to a genuinely nonexistent
    job id, so a scoped reader gains no 404-vs-200 existence oracle and cannot
    read another job's ranking state. ``None`` (the default, for
    admin/recruiter/auditor) leaves the query byte-identical to the unscoped
    read — no predicate, no extra bind arg."""
    if user_id is None:
        row = await conn.fetchrow(_GET_SHORTLIST_STATE_SQL, job_id)
    else:
        query = f"{_GET_SHORTLIST_STATE_SQL} AND {_JOBS_ASSIGNEE_EXISTS_SQL}"
        row = await conn.fetchrow(query, job_id, user_id)
    if row is None:
        raise NotFoundError(f"job {job_id} not found", job_id=str(job_id))
    if row["shortlist_state"] is None:
        return None
    if row["shortlist_state"] == "ranking":
        age = dt.datetime.now(dt.UTC) - row["shortlist_state_at"]
        if age.total_seconds() > get_settings().shortlist_ranking_stale_after_s:
            return None
    return ShortlistStateOut(
        state=row["shortlist_state"],
        reason=row["shortlist_state_reason"],
        at=row["shortlist_state_at"],
    )


# ── reverse-match read (Phase 6) ────────────────────────────────────────────
#
# Placed alongside persist_reverse_match for symmetry: the write and read
# sides of the SAME table live together, mirroring list_for_job/get_one below
# for shortlist_entries.

_REVERSE_MATCH_COLS = (
    "rm.job_id, j.title, j.department, rm.rank, rm.score_final, "
    "rm.score_structured, rm.score_evidence, rm.score_breakdown, rm.evidence, "
    "rm.requirement_count, rm.must_have_count, rm.pipeline_meta, rm.generated_at"
)
# ADR-026 residual fix (FU-8 withdrawal), mirror of PR #43's shortlist read
# filter for the reverse-match (candidate -> jobs) read. The write path
# (``reverse_match_job``) already skips a withdrawn résumé and persists
# nothing, but rows written BEFORE withdrawal survive untouched — only the READ
# must hide them. This read is keyed on a SINGLE ``resume_id`` and JOINs
# ``jobs`` (not ``resumes``), so — like ``_NOT_WITHDRAWN_SQL`` for the non-blind
# shortlist read — the guard is a correlated NOT EXISTS on the entry's own
# ``rm.resume_id``: a withdrawn candidate's whole reverse-match result collapses
# to the empty shape (``get_reverse_match_result``'s "never matched" contract),
# and reinstatement restores the same persisted rows.
_REVERSE_NOT_WITHDRAWN_SQL = (
    "NOT EXISTS (SELECT 1 FROM resumes r WHERE r.id = rm.resume_id "
    "AND r.withdrawn_at IS NOT NULL)"
)
_REVERSE_MATCH_QUERY = (
    f"SELECT {_REVERSE_MATCH_COLS} FROM reverse_match_entries rm "
    "JOIN jobs j ON j.id = rm.job_id "
    f"WHERE rm.resume_id = $1 AND {_REVERSE_NOT_WITHDRAWN_SQL} "
    "ORDER BY rm.rank ASC"
)


def _row_to_job_match_entry(row: Any) -> JobMatchEntry:
    raw = dict(row)
    sb = raw["score_breakdown"]
    if isinstance(sb, str):
        sb = json.loads(sb)
    ev = raw["evidence"]
    if isinstance(ev, str):
        ev = json.loads(ev)
    return JobMatchEntry(
        job_id=raw["job_id"],
        title=raw["title"],
        department=raw["department"],
        rank=raw["rank"],
        score_final=raw["score_final"],
        score_structured=raw["score_structured"],
        score_evidence=raw["score_evidence"],
        # No fold-pop here (the mirror image of the shortlist read path): this
        # table never folded score_structured/score_evidence into the jsonb,
        # so ScoreBreakdown.model_validate runs on the jsonb AS-IS.
        score_breakdown=ScoreBreakdown.model_validate(sb),
        # TOLERANT model, deliberately (ADR-022 follow-up #3). This validate is
        # uncaught and runs on bytes already committed to JSONB, so a size cap
        # here is a 500 on the whole reverse-match response for the résumé —
        # and there is no migration framework to clean the row up with. A cap
        # prevents a bad WRITE; it buys nothing on READ. Never swap in
        # EvidenceObjectIngest.
        evidence=EvidenceObject.model_validate(ev) if ev is not None else None,
        requirement_count=raw["requirement_count"],
        must_have_count=raw["must_have_count"],
    )


async def get_reverse_match_result(conn: DbConn, resume_id: UUID) -> JobMatchResultOut:
    """All ``reverse_match_entries`` rows for ``resume_id``, ordered by rank,
    joined to ``jobs`` for title/department.

    A résumé that has never been reverse-matched (zero rows — not a missing
    résumé; that is the route's job) returns the empty shape. ``pipeline_meta``
    / ``generated_at`` are per-row on the table but singular on the DTO — every
    row from one run carries the identical value, so they are taken from the
    first row.
    """
    rows = await conn.fetch(_REVERSE_MATCH_QUERY, resume_id)
    if not rows:
        return JobMatchResultOut(
            resume_id=resume_id, entries=[], pipeline_meta=None, generated_at=None
        )
    entries = [_row_to_job_match_entry(r) for r in rows]
    first = dict(rows[0])
    meta_raw = first["pipeline_meta"]
    if isinstance(meta_raw, str):
        meta_raw = json.loads(meta_raw)
    return JobMatchResultOut(
        resume_id=resume_id,
        entries=entries,
        pipeline_meta=(
            PipelineMeta.model_validate(meta_raw) if meta_raw is not None else None
        ),
        generated_at=first["generated_at"],
    )


# ── read (Phase 5) ─────────────────────────────────────────────────────────

# The review-workflow joins (shortlist_decisions / stage_transitions / users)
# are CUT — those tables do not exist in this project. The read layer surfaces
# only the ranking row + blind-review masking.
_ENTRY_COLS = (
    "id, job_id, resume_id, rank, score_final, "
    "score_breakdown, evidence, pipeline_meta, generated_at"
)
# ADR-026 residual fix (FU-8 withdrawal) — a withdrawn candidate's persisted
# shortlist_entries row survives the withdrawal untouched (write path is
# unchanged); only the READ must hide it. Neither query below joins resumes,
# so the guard is a correlated NOT EXISTS keyed on the entry's own
# resume_id. Appended AFTER the "WHERE job_id = $1" / "WHERE id = $1" anchor
# so list_for_job's FU-6 `.replace("WHERE job_id = $1", ...)` still finds
# that exact substring verbatim and inserts its scoping predicate before
# this clause.
_NOT_WITHDRAWN_SQL = (
    "NOT EXISTS (SELECT 1 FROM resumes r WHERE r.id = shortlist_entries.resume_id "
    "AND r.withdrawn_at IS NOT NULL)"
)
_LIST_QUERY = (
    f"SELECT {_ENTRY_COLS} FROM shortlist_entries "
    f"WHERE job_id = $1 AND {_NOT_WITHDRAWN_SQL} ORDER BY rank ASC"
)
_GET_QUERY = (
    f"SELECT {_ENTRY_COLS} FROM shortlist_entries "
    f"WHERE id = $1 AND {_NOT_WITHDRAWN_SQL}"
)

# Blind-review queries: same columns (aliased `se`, since the JOIN to resumes
# makes a bare `id` ambiguous) plus the candidate's decrypted name/contact and
# the résumé's parsed json — appended ONLY so we can redact them out of the
# evidence server-side. Those `_c_*` columns are popped before the response is
# built and never reach the client. Caller need not pre-`set_pii_key`; the
# read function opens its own transaction and sets it.
_BLIND_COLS = (
    "se.id, se.job_id, se.resume_id, se.rank, se.score_final, "
    "se.score_breakdown, se.evidence, se.pipeline_meta, se.generated_at, "
    "pgp_sym_decrypt(r.candidate_name, current_setting('app.pii_key')) "
    "AS _c_name, "
    "pgp_sym_decrypt(r.candidate_email, current_setting('app.pii_key')) "
    "AS _c_email, "
    "pgp_sym_decrypt(r.candidate_phone, current_setting('app.pii_key')) "
    "AS _c_phone, "
    "r.parsed AS _c_parsed"
)
# ADR-026 residual fix (FU-8 withdrawal) — these already JOIN resumes, so a plain
# `r.withdrawn_at IS NULL` predicate suffices. Appended AFTER the
# "WHERE se.job_id = $1" / "WHERE se.id = $1" anchor so the FU-6
# `.replace("WHERE se.job_id = $1", ...)` scoping in list_for_job still finds
# that exact substring verbatim.
_BLIND_LIST_QUERY = (
    f"SELECT {_BLIND_COLS} FROM shortlist_entries se "
    "JOIN resumes r ON r.id = se.resume_id "
    "WHERE se.job_id = $1 AND r.withdrawn_at IS NULL ORDER BY se.rank ASC"
)
_BLIND_GET_QUERY = (
    f"SELECT {_BLIND_COLS} FROM shortlist_entries se "
    "JOIN resumes r ON r.id = se.resume_id "
    "WHERE se.id = $1 AND r.withdrawn_at IS NULL"
)
_BLIND_CHECK_QUERY = (
    "SELECT j.blind_review FROM jobs j "
    "JOIN shortlist_entries se ON se.job_id = j.id WHERE se.id = $1"
)

# FU-6 slice 7 (ADR-020 §3) — the row-scoping predicate, mirroring
# ``job_service._JOB_ASSIGNEE_EXISTS_SQL`` / ``resume_service.
# _RESUME_ASSIGNEE_EXISTS_SQL``. Shortlist entries carry no assignee column
# of their own; scoping rides the job they belong to via their own
# ``job_id`` column (unscoped-column queries alias the table as
# ``shortlist_entries``, blind-review queries as ``se`` — ``{table}`` picks
# the right prefix). ``{n}`` is always ``2`` at every call site here: the
# scoping arg is bound immediately after the existing single positional
# arg ($1 = job_id/entry_id).
_SHORTLIST_ASSIGNEE_EXISTS_SQL = (
    "EXISTS (SELECT 1 FROM job_assignees "
    "WHERE job_assignees.job_id = {table}.job_id "
    "AND job_assignees.user_id = ${n})"
)


async def list_for_job(
    conn: DbConn, *, job_id: UUID, user_id: UUID | None = None
) -> list[ShortlistEntry]:
    """Ranked shortlist for a job. Under blind review, identity is masked and a
    rank-based ``display_label`` is applied before any DTO is built.

    FU-6 slice 7 (ADR-020 §3/§4) — ``user_id`` row-scopes the list to a
    hiring_manager's assigned jobs via an ``EXISTS`` predicate against
    ``job_assignees``, keyed on the shortlist entry's own ``job_id``.
    ``None`` (the default) leaves the SQL byte-identical to before this
    slice for unscoped callers (admin/recruiter/auditor). An
    unassigned/nonexistent ``job_id`` yields an empty list — never a 404 —
    matching this route's pre-existing behaviour for a genuinely
    nonexistent job (ADR-020 §5).
    """
    if await conn.fetchval("SELECT blind_review FROM jobs WHERE id = $1", job_id):
        async with conn.transaction():
            await set_pii_key(conn)
            if user_id is None:
                rows = await conn.fetch(_BLIND_LIST_QUERY, job_id)
            else:
                query = _BLIND_LIST_QUERY.replace(
                    "WHERE se.job_id = $1",
                    "WHERE se.job_id = $1 AND "
                    + _SHORTLIST_ASSIGNEE_EXISTS_SQL.format(table="se", n=2),
                )
                rows = await conn.fetch(query, job_id, user_id)
        return [_row_to_blind_entry(r) for r in rows]
    if user_id is None:
        rows = await conn.fetch(_LIST_QUERY, job_id)
    else:
        query = _LIST_QUERY.replace(
            "WHERE job_id = $1",
            "WHERE job_id = $1 AND "
            + _SHORTLIST_ASSIGNEE_EXISTS_SQL.format(table="shortlist_entries", n=2),
        )
        rows = await conn.fetch(query, job_id, user_id)
    return [_row_to_entry(r) for r in rows]


async def get_one(
    conn: DbConn, entry_id: UUID, *, user_id: UUID | None = None
) -> ShortlistEntry:
    """One shortlist entry by id, blind-masked when its job is blind.

    FU-6 slice 7 (ADR-020 §3/§5) — ``user_id`` row-scopes the read via an
    ``EXISTS`` predicate against ``job_assignees``, keyed on the entry's own
    ``job_id``. An entry that exists but whose job is not assigned to
    ``user_id`` is filtered out at the SQL layer (0 rows), which this
    function surfaces as ``NotFoundError`` — observationally identical to a
    genuinely nonexistent entry id (ADR-020 §5: 404, never 403). ``None``
    (the default, unscoped callers) leaves the SQL byte-identical to before
    this slice — no predicate, no extra bind arg.
    """
    if await conn.fetchval(_BLIND_CHECK_QUERY, entry_id):
        async with conn.transaction():
            await set_pii_key(conn)
            if user_id is None:
                row = await conn.fetchrow(_BLIND_GET_QUERY, entry_id)
            else:
                query = (
                    f"{_BLIND_GET_QUERY} AND "
                    f"{_SHORTLIST_ASSIGNEE_EXISTS_SQL.format(table='se', n=2)}"
                )
                row = await conn.fetchrow(query, entry_id, user_id)
        if row is None:
            raise NotFoundError(
                f"shortlist entry {entry_id} not found", entry_id=str(entry_id)
            )
        return _row_to_blind_entry(row)
    if user_id is None:
        row = await conn.fetchrow(_GET_QUERY, entry_id)
    else:
        query = (
            f"{_GET_QUERY} AND "
            f"{_SHORTLIST_ASSIGNEE_EXISTS_SQL.format(table='shortlist_entries', n=2)}"
        )
        row = await conn.fetchrow(query, entry_id, user_id)
    if row is None:
        raise NotFoundError(
            f"shortlist entry {entry_id} not found", entry_id=str(entry_id)
        )
    return _row_to_entry(row)


def _folded_subscore(value: Any, *, key: str, entry_id: Any) -> float | None:
    """One folded sub-score out of the ``score_breakdown`` jsonb, or ``None``.

    ``float(value)`` is NOT safe to run bare here: a corrupted/legacy jsonb
    value raises ``ValueError``/``TypeError``, NEITHER of which is a
    ``ValidationError``, so it would escape every tolerance on this read path
    and 500 the whole shortlist PERMANENTLY (nothing can rewrite the stored
    row). Degrading to ``None`` — "not recorded" — matches how
    ``_parse_pipeline_meta`` treats an unreadable stamp."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        logger.warning(
            "shortlist entry %s: score_breakdown.%s is not numeric (%s); "
            "reporting it as not recorded",
            entry_id,
            key,
            type(value).__name__,
        )
        return None


def _folded_evidence_evaluated(value: Any, *, entry_id: Any) -> bool | None:
    """The folded ``evidence_evaluated`` marker, or ``None`` for "unknown".

    STRICT about the type, unlike a bare ``bool(value)``. ``"yes"``, ``1.5``
    and ``[]``-vs-``[0]`` are all truthy or falsy in Python without being a
    recorded boolean, and reporting a corrupted value as an affirmative
    "assessed" is precisely the invented fact this marker exists to prevent.
    Only a real ``bool`` counts; anything else degrades to "we do not know".

    Never raises, for the same reason as :func:`_folded_subscore`: this runs on
    bytes already committed to a NOT NULL column that nothing can rewrite, so a
    raise would 500 the whole shortlist permanently.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    logger.warning(
        "shortlist entry %s: score_breakdown.evidence_evaluated is not a "
        "boolean (%s); reporting it as not recorded",
        entry_id,
        type(value).__name__,
    )
    return None


def _parse_entry_jsonb(
    raw: dict[str, Any],
) -> tuple[float | None, float | None, bool | None]:
    """In-place: coerce the jsonb score_breakdown/evidence columns into their
    pydantic models. RETURNS the two UNFOLDED sub-scores
    ``(score_structured, score_evidence)`` for the caller to put on the DTO.

    ``persist_shortlist`` (4d) FOLDS ``score_structured``/``score_evidence``
    into the ``score_breakdown`` jsonb (the table has no dedicated columns).
    ``ScoreBreakdown`` is ``extra="forbid"``, so those two folded keys MUST be
    stripped before validating or every 4d row raises ValidationError. They are
    still popped here for exactly that reason — but they are no longer
    DISCARDED once popped: the "Why this rank?" panel explains the score
    composition, which needs both. ``None`` stands for "the row never recorded
    one" (a pre-4d row, or one whose folded value was unreadable) and renders
    as "not recorded"; it is deliberately NOT ``0.0``, which would render as an
    affirmative "0% contribution" the row does not support.

    Same rule for the evidence jsonb: it is validated with the TOLERANT
    ``EvidenceObject``, never an ingest variant. Both validates below are
    uncaught and run on already-stored bytes, so any length cap reached from
    here 500s the entire shortlist for the job — permanently, since nothing
    can rewrite the row."""
    entry_id = raw.get("id")
    sb = raw["score_breakdown"]
    if isinstance(sb, str):
        sb = json.loads(sb)
    sb = dict(sb)
    folded_structured = sb.pop("score_structured", None)
    folded_evidence = sb.pop("score_evidence", None)
    # Popped for the same hard reason as the two above: ScoreBreakdown is
    # extra="forbid", so a folded key left in place raises on EVERY row.
    folded_evaluated = sb.pop("evidence_evaluated", None)
    raw["score_breakdown"] = ScoreBreakdown.model_validate(sb)
    ev = raw["evidence"]
    if isinstance(ev, str):
        ev = json.loads(ev)
    raw["evidence"] = EvidenceObject.model_validate(ev) if ev is not None else None
    return (
        _folded_subscore(folded_structured, key="score_structured", entry_id=entry_id),
        _folded_subscore(folded_evidence, key="score_evidence", entry_id=entry_id),
        _folded_evidence_evaluated(folded_evaluated, entry_id=entry_id),
    )


def _parse_pipeline_meta(raw_meta: Any, *, entry_id: Any = None) -> PipelineMeta | None:
    """The ``pipeline_meta`` jsonb -> ``PipelineMeta``, or ``None``.

    TOLERANT on purpose, by the same rule as the evidence validate above: this
    runs on bytes already committed to a ``NOT NULL`` column that nothing can
    rewrite. ``_meta_json(None)`` legitimately writes a DEGENERATE stamp
    (``{"weights": ...}`` only) for a job that vanished mid-run, and pre-4d
    rows can carry anything at all; a raising validate would turn either into a
    permanent 500 on the whole shortlist read. ``None`` is the honest answer —
    the explanation panel reports "weights unavailable" for it and never
    substitutes ``DEFAULT_WEIGHTS``, so a malformed stamp can never be
    mistaken for the weights that actually produced the score.

    That last sentence is NOT enforced by this comment. It is enforced by the
    ``_parse_pipeline_meta`` tests in
    ``tests/unit/test_services_shortlist_read.py``, which kill the mutant that
    resurrects a failed stamp as ``DEFAULT_WEIGHTS``.

    The two refusals are LOGGED (WARNING, entry id only — never candidate
    fields): the explanation panel is a compliance artifact, so genuine
    corruption must not be indistinguishable from an ordinary legacy row."""
    if raw_meta is None:
        return None
    if isinstance(raw_meta, str):
        try:
            raw_meta = json.loads(raw_meta)
        except (ValueError, TypeError):
            logger.warning(
                "shortlist entry %s: pipeline_meta is not valid JSON; "
                "reporting weights as unavailable",
                entry_id,
            )
            return None
    try:
        return PipelineMeta.model_validate(raw_meta)
    except ValidationError as exc:
        logger.warning(
            "shortlist entry %s: pipeline_meta failed validation (%d error(s), "
            "first at %s); reporting weights as unavailable rather than "
            "substituting today's defaults",
            entry_id,
            exc.error_count(),
            ".".join(str(p) for p in (exc.errors()[0]["loc"] if exc.errors() else ())),
        )
        return None


def _row_to_entry(row: Any) -> ShortlistEntry:
    raw = dict(row)
    (
        raw["score_structured"],
        raw["score_evidence"],
        raw["evidence_evaluated"],
    ) = _parse_entry_jsonb(raw)
    raw["pipeline_meta"] = _parse_pipeline_meta(
        raw.get("pipeline_meta"), entry_id=raw.get("id")
    )
    return ShortlistEntry.model_validate(raw)


def _redact_evidence(
    ev: EvidenceObject | None,
    *,
    name: str | None,
    email: str | None,
    phone: str | None,
    term_map: dict[str, str] | None = None,
    location: str | None = None,
    redact_locations: bool = False,
) -> EvidenceObject | None:
    """Scrub identity out of the LLM evidence. Closes the hris gap: BOTH the
    requirement evidence + overall_summary AND the cover-letter evidence +
    overall_motivation are redacted (a cover-letter quote can carry the
    candidate's own name)."""
    if ev is None:
        return None

    def _r(text: str) -> str:
        return redact_text(
            text,
            name=name,
            email=email,
            phone=phone,
            term_map=term_map,
            location=location,
            redact_locations=redact_locations,
        )

    reqs = [r.model_copy(update={"evidence": _r(r.evidence)}) for r in ev.requirements]
    cover = [
        c.model_copy(update={"evidence": _r(c.evidence)})
        for c in ev.cover_letter_evidence
    ]
    return ev.model_copy(
        update={
            "requirements": reqs,
            "overall_summary": _r(ev.overall_summary),
            "cover_letter_evidence": cover,
            "overall_motivation": _r(ev.overall_motivation),
        }
    )


def _attach_source_context_model(
    ev: EvidenceObject | None,
    *,
    raw_parsed: Any,
    redactor: Callable[[str], str] | None,
) -> EvidenceObject | None:
    """FU-2 (read path): populate each requirement's ``source_context`` by
    resolving its ``evidence_chunk_ids`` against ``raw_parsed`` chunk text.

    ``redactor`` is an optional ``str -> str`` callable applied to the resolved
    text; under blind review it is the SAME scrub used for the evidence quote,
    so a header/summary chunk carrying the candidate's name never leaks. Empty
    resolutions leave the field ``None``."""
    if ev is None:
        return None
    reqs = []
    for r in ev.requirements:
        text = _resolve_chunk_context(raw_parsed, r.evidence_chunk_ids)
        if text and redactor is not None:
            text = redactor(text)
        reqs.append(r.model_copy(update={"source_context": text or None}))
    return ev.model_copy(update={"requirements": reqs})


def labels_from_parsed(raw_parsed: Any) -> dict[str, str]:
    """Employer/school label map from a résumé's stored parse JSON, so evidence
    quotes that name an employer/school get the same labels as the résumé
    detail. Empty when the résumé isn't parsed. Shared with the export path."""
    if raw_parsed is None:
        return {}
    if isinstance(raw_parsed, str):
        raw_parsed = json.loads(raw_parsed)
    parsed = ResumeParsed.model_validate(raw_parsed)
    return blind_label_map(
        employers=[e.company for e in parsed.experience],
        institutions=[ed.institution for ed in parsed.education],
    )


def location_from_parsed(raw_parsed: Any) -> str | None:
    """The candidate's structured location from a stored parse, so evidence +
    export text scrub the same city as the résumé detail. Shared with export."""
    if raw_parsed is None:
        return None
    if isinstance(raw_parsed, str):
        raw_parsed = json.loads(raw_parsed)
    return ResumeParsed.model_validate(raw_parsed).candidate.location


def _resolve_chunk_context(raw_parsed: Any, chunk_ids: list[str]) -> str:
    """FU-2 pure resolver: expand a requirement's ``evidence_chunk_ids`` to the
    ACTUAL source text behind them, drawn from the résumé's stored parse.

    Chunk text lives in ``parsed["chunks"]`` (``c_NNN`` ids) and
    ``parsed["cover_letter_chunks"]`` (``cl_NNN`` ids, a parallel id space). The
    two are merged into one ``{id: text}`` map, then the requested ids are
    joined IN ORDER, deduped, skipping any id with no matching chunk. Returns
    ``""`` when there is nothing to resolve (no ids / no parse / no matches).

    RAW text only — this helper does NO redaction; every call site is
    responsible for scrubbing the result to match its ``reveal`` state (see
    ``_attach_export_context`` / ``_row_to_blind_entry``)."""
    if not chunk_ids or raw_parsed is None:
        return ""
    if isinstance(raw_parsed, str):
        try:
            raw_parsed = json.loads(raw_parsed)
        except (ValueError, TypeError):
            return ""
    if not isinstance(raw_parsed, dict):
        return ""
    text_by_id: dict[str, str] = {}
    for key in ("chunks", "cover_letter_chunks"):
        items = raw_parsed.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            cid = item.get("id")
            ctext = item.get("text")
            if not (isinstance(cid, str) and isinstance(ctext, str)):
                continue
            if cid not in text_by_id:
                text_by_id[cid] = ctext
    seen: set[str] = set()
    parts: list[str] = []
    for raw_id in chunk_ids:
        cid = str(raw_id)
        if cid in seen:
            continue
        seen.add(cid)
        txt = text_by_id.get(cid)
        if txt:
            parts.append(txt)
    return "\n\n".join(parts)


def _row_to_blind_entry(row: Any) -> ShortlistEntry:
    raw = dict(row)
    name = raw.pop("_c_name", None)
    email = raw.pop("_c_email", None)
    phone = raw.pop("_c_phone", None)
    parsed_raw = raw.pop("_c_parsed", None)
    labels = labels_from_parsed(parsed_raw)
    location = location_from_parsed(parsed_raw)
    (
        raw["score_structured"],
        raw["score_evidence"],
        raw["evidence_evaluated"],
    ) = _parse_entry_jsonb(raw)
    raw["pipeline_meta"] = _parse_pipeline_meta(
        raw.get("pipeline_meta"), entry_id=raw.get("id")
    )
    # Redaction happens BEFORE the DTO is built (ADR-006 §4): no decrypted PII
    # ever reaches ShortlistEntry.
    raw["evidence"] = _redact_evidence(
        raw["evidence"],
        name=name,
        email=email,
        phone=phone,
        term_map=labels,
        location=location,
        redact_locations=True,
    )

    # FU-2: expand each requirement's cited chunk ids to their source text. The
    # read path IS blind, so the resolved text is scrubbed with the SAME
    # name/label/location redaction just applied to the evidence quote — a chunk
    # can carry the candidate's own name/email/phone.
    def _blind_redactor(text: str) -> str:
        return redact_text(
            text,
            name=name,
            email=email,
            phone=phone,
            term_map=labels,
            location=location,
            redact_locations=True,
        )

    raw["evidence"] = _attach_source_context_model(
        raw["evidence"], raw_parsed=parsed_raw, redactor=_blind_redactor
    )
    raw["blinded"] = True
    raw["display_label"] = pseudonym(int(raw["rank"]))
    return ShortlistEntry.model_validate(raw)


# ── export (Phase 5) ───────────────────────────────────────────────────────

_EXPORT_QUERY = """
SELECT
    s.rank,
    s.resume_id,
    s.score_final,
    s.score_breakdown,
    s.evidence,
    s.pipeline_meta,
    s.generated_at,
    j.title AS job_title,
    j.department AS job_department,
    r.original_filename,
    pgp_sym_decrypt(r.candidate_name, current_setting('app.pii_key'))
        AS candidate_name,
    pgp_sym_decrypt(r.candidate_email, current_setting('app.pii_key'))
        AS candidate_email,
    pgp_sym_decrypt(r.candidate_phone, current_setting('app.pii_key'))
        AS candidate_phone,
    r.parsed AS candidate_parsed
FROM shortlist_entries s
JOIN jobs j ON j.id = s.job_id
JOIN resumes r ON r.id = s.resume_id
WHERE s.job_id = $1 AND r.withdrawn_at IS NULL
ORDER BY s.rank ASC
"""
# ADR-026 residual fix (FU-8 withdrawal) — the export is a shortlist view too,
# so it must hide withdrawn candidates like list_for_job/get_one. The clause is
# appended AFTER the "WHERE s.job_id = $1" anchor so export_rows' FU-6
# `.replace("WHERE s.job_id = $1", ...)` row-scoping still finds it verbatim.


def _as_obj(v: object) -> dict[str, Any]:
    """jsonb columns arrive as a dict (JSON codec) or a JSON string depending on
    the codec; normalise either to a plain dict."""
    if isinstance(v, str):
        loaded = json.loads(v)
        return loaded if isinstance(loaded, dict) else {}
    if isinstance(v, dict):
        return v
    return {}


def _redact_evidence_dict(
    ev: object,
    *,
    name: str | None,
    term_map: dict[str, str],
    location: str | None = None,
    redact_locations: bool = False,
) -> dict[str, Any]:
    """Scrub name/contact + label employer/school (and, when requested, foreign
    geography) out of an export row's evidence dict — requirement quotes +
    overall_summary AND cover-letter quotes + overall_motivation (gap closed)."""
    obj = _as_obj(ev)
    if not obj:
        return obj

    def _r(text: str) -> str:
        return redact_text(
            text,
            name=name,
            term_map=term_map,
            location=location,
            redact_locations=redact_locations,
        )

    for key in ("requirements", "cover_letter_evidence"):
        items = obj.get(key)
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict) and isinstance(item.get("evidence"), str):
                    item["evidence"] = _r(item["evidence"])
    for key in ("overall_summary", "overall_motivation"):
        if isinstance(obj.get(key), str):
            obj[key] = _r(obj[key])
    return obj


def _attach_export_context(rows: list[dict[str, Any]], *, reveal: bool) -> None:
    """FU-2 (export path): for each requirement, resolve ``evidence_chunk_ids``
    to the actual source text and stash it on ``req["source_context"]``.

    Mutates the evidence dicts in place and MUST run while ``candidate_parsed``
    and the REAL ``candidate_name`` are still present (i.e. BEFORE
    ``_apply_reveal`` swaps in the pseudonym). Under ``reveal=False`` the
    resolved text is scrubbed with the same name / employer-school labels /
    foreign-location redaction ``_apply_reveal`` applies to the evidence quote,
    so no chunk-borne PII escapes the anonymized export."""
    for r in rows:
        ev = r.get("evidence")
        if not isinstance(ev, dict):
            continue
        reqs = ev.get("requirements")
        if not isinstance(reqs, list):
            continue
        parsed = r.get("candidate_parsed")
        # Redaction params are the SAME ones _apply_reveal derives for the quote
        # (real name still present at this point); unused when reveal=True.
        name = r.get("candidate_name")
        labels = labels_from_parsed(parsed)
        location = location_from_parsed(parsed)
        for req in reqs:
            if not isinstance(req, dict):
                continue
            ids = req.get("evidence_chunk_ids")
            id_list = [str(c) for c in ids] if isinstance(ids, list) else []
            text = _resolve_chunk_context(parsed, id_list)
            if text and not reveal:
                text = redact_text(
                    text,
                    name=name,
                    term_map=labels,
                    location=location,
                    redact_locations=True,
                )
            req["source_context"] = text


def _apply_reveal(rows: list[dict[str, Any]], *, reveal: bool) -> None:
    """When ``reveal`` is False, strip decrypted identity and substitute the
    rank-based pseudonym, mask the identifying résumé filename, and scrub
    name/contact + employer/school labels out of the evidence quote text (using
    the REAL name still present here BEFORE it is swapped). Mutates rows in
    place."""
    if reveal:
        return
    for r in rows:
        labels = labels_from_parsed(r.get("candidate_parsed"))
        location = location_from_parsed(r.get("candidate_parsed"))
        r["evidence"] = _redact_evidence_dict(
            r.get("evidence"),
            name=r.get("candidate_name"),
            term_map=labels,
            location=location,
            redact_locations=True,
        )
        r["candidate_name"] = pseudonym(int(r["rank"]))
        r["candidate_email"] = ""
        r["candidate_phone"] = ""
        # A résumé named after its candidate leaks identity through the export
        # (the resume_file column) — mask it, keeping only its extension.
        r["original_filename"] = redacted_filename(r.get("original_filename"))


async def export_rows(
    conn: DbConn, *, job_id: UUID, reveal: bool, user_id: UUID | None = None
) -> list[dict[str, Any]]:
    """Flat rows for CSV / JSON export, already reveal-applied. The caller MUST
    have run ``pii_service.set_pii_key(conn)`` inside an open transaction — the
    raw-SQL ``pgp_sym_decrypt`` fails loud otherwise (it is NOT called here).

    FU-6 slice 7 (ADR-020 §3/§4) — ``user_id`` row-scopes the export to a
    hiring_manager's assigned jobs via an ``EXISTS`` predicate against
    ``job_assignees``, keyed on the shortlist entry's ``job_id`` (aliased
    ``s`` in this query). ``None`` (the default) leaves the SQL byte-
    identical to before this slice for unscoped callers (admin/recruiter/
    auditor). An unassigned/nonexistent ``job_id`` yields an empty export —
    never a 404 — matching the sibling by-job list route's behaviour
    (ADR-020 §5).
    """
    if user_id is None:
        rows = await conn.fetch(_EXPORT_QUERY, job_id)
    else:
        query = _EXPORT_QUERY.replace(
            "WHERE s.job_id = $1",
            "WHERE s.job_id = $1 AND "
            + _SHORTLIST_ASSIGNEE_EXISTS_SQL.format(table="s", n=2),
        )
        rows = await conn.fetch(query, job_id, user_id)
    out: list[dict[str, Any]] = []
    for row in rows:
        d = dict(row)
        d["score_breakdown"] = _as_obj(d.get("score_breakdown"))
        d["evidence"] = _as_obj(d.get("evidence"))
        d["pipeline_meta"] = _as_obj(d.get("pipeline_meta"))
        out.append(d)
    # FU-2: resolve cited chunk ids -> source text BEFORE _apply_reveal swaps
    # the real name for a pseudonym, so blind redaction uses the real identity.
    _attach_export_context(out, reveal=reveal)
    _apply_reveal(out, reveal=reveal)
    # candidate_parsed was only needed to derive labels/location for redaction;
    # it carries the raw name and must never reach the exported payload.
    for d in out:
        d.pop("candidate_parsed", None)
    return out


# ── export formatters (pure) ───────────────────────────────────────────────


def _skill_summary(breakdown: dict[str, Any]) -> dict[str, Any]:
    """Flatten score_breakdown.skill_contributions into recruiter-facing
    columns: which required skills matched vs. are missing, and which of the
    MISSING ones are must-haves."""
    contribs = breakdown.get("skill_contributions")
    matched: list[str] = []
    missing: list[str] = []
    must_missing: list[str] = []
    must_total = 0
    for c in contribs if isinstance(contribs, list) else []:
        if not isinstance(c, dict):
            continue
        cname = str(c.get("skill") or "")
        is_must = bool(c.get("is_must_have"))
        must_total += int(is_must)
        if c.get("reason") == "missing":
            missing.append(cname)
            if is_must:
                must_missing.append(cname)
        else:
            matched.append(cname)
    return {
        "skills_total": len(matched) + len(missing),
        "skills_matched_count": len(matched),
        "skills_matched": "; ".join(matched),
        "skills_missing": "; ".join(missing),
        "must_have_count": must_total,
        "must_have_missing": "; ".join(must_missing),
    }


def _evidence_coverage(evidence: dict[str, Any]) -> dict[str, Any]:
    """Per-requirement coverage counts + the evidence-completeness fraction,
    computed with the same formula the scorer uses:
    (#met-with-confidence>=0.7 + 0.5*#partial) / #requirements."""
    reqs = evidence.get("requirements")
    met = partial = missing = strong_met = 0
    for r in reqs if isinstance(reqs, list) else []:
        if not isinstance(r, dict):
            continue
        status = r.get("status")
        conf = r.get("confidence")
        if status == "met":
            met += 1
            if isinstance(conf, (int, float)) and conf >= 0.7:
                strong_met += 1
        elif status == "partial":
            partial += 1
        elif status == "missing":
            missing += 1
    total = met + partial + missing
    completeness = round((strong_met + 0.5 * partial) / total, 4) if total else 0.0
    summary = str(evidence.get("overall_summary") or "").replace("\n", " ")
    return {
        "requirement_count": total,
        "requirements_met": met,
        "requirements_partial": partial,
        "requirements_missing": missing,
        "evidence_completeness": completeness,
        "evidence_summary": summary,
    }


# ── CSV formula injection (security gate finding S3) ──────────────────────
#
# Excel / Google Sheets EXECUTE a cell whose FIRST character is one of
# ``= + - @ TAB CR`` as a formula when the file is opened — the classic
# ``=cmd|'/c calc'!A1`` DDE payload turns a recruiter's shortlist download
# into code execution. Neither writer neutralized anything.
#
# The exposure is pre-existing via ``candidate_name`` / ``evidence_summary``
# / ``quote`` / ``requirement`` / ``resume_file``, and the per-job
# display-name work WIDENED it: ``skills_matched`` / ``skills_missing`` /
# ``must_have_missing`` used to be a closed set (curated vocabulary, or an
# opaque ``h:<32 hex>`` ADR-008 key) and are now arbitrary JD text.
# ``skills_graph.reject_reason_for_skill_name`` screens only length, token
# count, and email/phone SHAPE — a skill named ``=cmd|'/c calc'!A1`` passes
# it untouched.
#
# The guard is therefore applied file-wide, to EVERY string cell in BOTH
# writers (``_csv_row``), not per-column: special-casing the skills columns
# would leave the identical bug on the other six text fields, and on the
# next column anyone adds.
#
# NUL (0x00) is deliberately absent: writing that escape materialises a real
# NUL byte that breaks pytest collection and git. TAB/CR are built with
# ``chr()`` for the same reason.
_CSV_FORMULA_LEADERS = frozenset({"=", "+", "-", "@", chr(9), chr(13)})


def _csv_safe(v: str) -> str:
    """Neutralise a spreadsheet formula-injection payload by prefixing an
    apostrophe — the standard escape both Excel and Sheets honour: the cell
    renders as literal text and the apostrophe is not part of the value.
    Anything not starting with a dangerous character is returned unchanged
    (a legitimate skill name, filename or summary is never mangled)."""
    return "'" + v if v[:1] in _CSV_FORMULA_LEADERS else v


def _csv_row(row: dict[str, Any]) -> dict[str, Any]:
    """Apply ``_csv_safe`` to every STRING cell of one CSV row. Numeric cells
    (scores, counts, ranks, confidences) pass through untouched — a negative
    score must stay ``-0.5``, not ``'-0.5``."""
    return {k: _csv_safe(v) if isinstance(v, str) else v for k, v in row.items()}


_CSV_FIELDS = [
    "rank",
    "candidate_name",
    "candidate_email",
    "candidate_phone",
    "resume_file",
    "job_title",
    "job_department",
    "score_final",
    # Skill gap — the confidence headline, kept beside the score.
    "must_have_missing",
    "skills_missing",
    "skills_matched_count",
    "skills_total",
    "must_have_count",
    # Sub-scores that reconcile FINAL.
    "score_structured",
    "score_evidence_completeness",
    "score_motivation",
    # Structured axes (each 0..1).
    "skill",
    "experience",
    "education",
    "seniority",
    "vector",
    # LLM evidence coverage.
    "requirement_count",
    "requirements_met",
    "requirements_partial",
    "requirements_missing",
    "evidence_summary",
    "skills_matched",
    # Traceability. (The review-workflow columns — current_stage /
    # current_decision / decision_* — are CUT: no review pipeline here.)
    "generated_at",
    "pipeline_version",
    "resume_id",
]


def shortlist_csv(rows: list[dict[str, Any]]) -> str:
    """One row per candidate — the recruiter working file (Excel)."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_CSV_FIELDS, extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        sb = _as_obj(r["score_breakdown"])
        ev = _as_obj(r["evidence"])
        meta = _as_obj(r["pipeline_meta"])
        skills = _skill_summary(sb)
        coverage = _evidence_coverage(ev)
        # _csv_row: EVERY string cell is formula-injection-neutralised, so no
        # column can be forgotten here or in a future addition (finding S3).
        writer.writerow(
            _csv_row(
                {
                    "rank": r["rank"],
                    "candidate_name": r["candidate_name"] or "",
                    "candidate_email": r["candidate_email"] or "",
                    "candidate_phone": r["candidate_phone"] or "",
                    "resume_file": r["original_filename"] or "",
                    "job_title": r["job_title"] or "",
                    "job_department": r["job_department"] or "",
                    "score_final": round(float(r["score_final"]), 4),
                    "must_have_missing": skills["must_have_missing"],
                    "skills_missing": skills["skills_missing"],
                    "skills_matched_count": skills["skills_matched_count"],
                    "skills_total": skills["skills_total"],
                    "must_have_count": skills["must_have_count"],
                    "score_structured": sb.get("structured"),
                    "score_evidence_completeness": coverage["evidence_completeness"],
                    "score_motivation": sb.get("motivation"),
                    "skill": sb.get("skill"),
                    "experience": sb.get("experience"),
                    "education": sb.get("education"),
                    "seniority": sb.get("seniority"),
                    "vector": sb.get("vector"),
                    "requirement_count": coverage["requirement_count"],
                    "requirements_met": coverage["requirements_met"],
                    "requirements_partial": coverage["requirements_partial"],
                    "requirements_missing": coverage["requirements_missing"],
                    "evidence_summary": coverage["evidence_summary"],
                    "skills_matched": skills["skills_matched"],
                    "generated_at": r["generated_at"].isoformat(),
                    "pipeline_version": str(meta.get("git_sha") or "")[:12],
                    "resume_id": str(r["resume_id"]),
                }
            )
        )
    return buf.getvalue()


_EVIDENCE_CSV_FIELDS = [
    "rank",
    "candidate_name",
    "resume_file",
    "job_title",
    "requirement",
    "status",
    "confidence",
    "quote",
    "evidence_chunk_ids",
    # FU-2: the resolved source text behind the cited chunk ids (redacted under
    # a blind/anonymized export). The ids column is kept for traceability.
    "evidence_context",
]


def shortlist_evidence_csv(rows: list[dict[str, Any]]) -> str:
    """One row per (candidate, requirement): the per-quote audit detail.
    Zero-requirement candidates still get a single placeholder row."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_EVIDENCE_CSV_FIELDS, extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        base = {
            "rank": r["rank"],
            "candidate_name": r["candidate_name"] or "",
            "resume_file": r["original_filename"] or "",
            "job_title": r["job_title"] or "",
        }
        reqs = _as_obj(r["evidence"]).get("requirements")
        req_list = (
            [req for req in reqs if isinstance(req, dict)]
            if isinstance(reqs, list)
            else []
        )
        if not req_list:
            writer.writerow(
                _csv_row({**base, "requirement": "(no evidence generated)"})
            )
            continue
        for req in req_list:
            chunks = req.get("evidence_chunk_ids")
            # _csv_row: see the finding-S3 note above _CSV_FIELDS.
            writer.writerow(
                _csv_row(
                    {
                        **base,
                        "requirement": str(req.get("requirement") or ""),
                        "status": str(req.get("status") or ""),
                        "confidence": req.get("confidence"),
                        "quote": str(req.get("evidence") or "").replace("\n", " "),
                        "evidence_chunk_ids": (
                            "; ".join(str(c) for c in chunks)
                            if isinstance(chunks, list)
                            else ""
                        ),
                        "evidence_context": str(
                            req.get("source_context") or ""
                        ).replace("\n", " "),
                    }
                )
            )
    return buf.getvalue()


def _normalise_for_json(row: dict[str, Any]) -> dict[str, Any]:
    """asyncpg returns Decimal / datetime — json.dumps wants str. score_final's
    Decimal needs explicit float coercion so consumers don't get "0.6420" as a
    string; score_breakdown / evidence / pipeline_meta stay as JSON objects."""
    out = dict(row)
    for key in ("score_breakdown", "evidence", "pipeline_meta"):
        val = out.get(key)
        if isinstance(val, str):
            out[key] = json.loads(val)
    if out.get("score_final") is not None:
        out["score_final"] = float(out["score_final"])
    return out


def shortlist_json(rows: list[dict[str, Any]]) -> str:
    """Full nested JSON, Decimal/datetime normalised."""
    return json.dumps([_normalise_for_json(r) for r in rows], default=str)
