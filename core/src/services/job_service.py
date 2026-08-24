"""Job write-back — the worker's half of the job lifecycle.

Phase 3 ports ONLY the two worker-side mutations from hris
``apps/api/src/api/services/job_service.py``; the CRUD / status state machine
lands with the routes in Phase 6.

Two deliberate cuts from the hris source:

* **No ``refine_title`` branch.** hris had a second UPDATE that also wrote
  ``title = $4, title_autofilled = FALSE`` so a bulk-ingest job whose title was
  derived from a filename could be corrected once by the LLM. Bulk ingest is
  cut, and ``jobs.title_autofilled`` does not exist in ``src/models/ddl.py`` —
  porting the branch would be an ``UndefinedColumnError`` waiting to happen.
  ``record_parsed`` never touches the ``title`` column at all.
* **No ``'failed'`` status.** ``job_status`` is ('draft','open','closed',
  'archived') — there is no failed state for a job, so a parse failure only
  surfaces on ``failure_reason`` and the row stays in 'draft' for a retry.

``record_parsed`` carries an optimistic-concurrency guard: the UPDATE applies
only while the row is still ``status = 'draft'``. If a concurrent transition
moved it out from under us, 0 rows apply and we return ``False`` — the caller
(``parse_job``) turns that into ``"stale"`` and MUST NOT enqueue an outbox row,
because a projection event for a write that never landed would corrupt the
graph.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
from typing import Any
from uuid import UUID

from pydantic import ValidationError

from src.errors import NotFoundError
from src.schemas.jobs import (
    BulkJobResult,
    JDExtracted,
    JobCreate,
    JobListItem,
    JobOut,
    JobUpdate,
)
from src.services import DbConn, audit_service
from src.services.bulk_ingest_service import (
    JobManifestRow,
    basename_lower,
    title_from_filename,
)
from src.services.jd_import_service import JDImportError, extract_jd_text

logger = logging.getLogger(__name__)

_MAX_REASON_CHARS = 1000

# JobCreate's ``description_raw`` floor — a JD shorter than this can't create a
# job, so it becomes a per-file ``failed`` outcome rather than aborting the batch.
_MIN_DESCRIPTION_CHARS = 50

_RECORD_PARSED_SQL = """
UPDATE jobs SET
    description_parsed = $2::jsonb,
    parsed_at = $3,
    failure_reason = NULL,
    updated_at = now()
WHERE id = $1 AND status = 'draft'
"""

_RECORD_FAILURE_SQL = """
UPDATE jobs SET
    failure_reason = $2,
    updated_at = now()
WHERE id = $1 AND status = 'draft'
"""


async def record_parsed(
    conn: DbConn,
    job_id: UUID,
    extracted: JDExtracted,
    parsed_at: dt.datetime,
) -> bool:
    """Write the LLM extraction back onto the job row.

    Returns ``True`` when the UPDATE applied (the job was still 'draft'), and
    ``False`` when a concurrent transition already moved it — the worker treats
    ``False`` as "drop it on the floor, do not retry, do not enqueue".
    """
    result = await conn.execute(
        _RECORD_PARSED_SQL,
        job_id,
        json.dumps(extracted.model_dump()),
        parsed_at,
    )
    applied = result.endswith(" 1")
    if not applied:
        logger.info("job.record_parsed.stale job_id=%s", job_id)
    return applied


async def record_parse_failure(conn: DbConn, job_id: UUID, reason: str) -> None:
    """Surface a parse failure on the job row. Status stays 'draft'."""
    await conn.execute(_RECORD_FAILURE_SQL, job_id, reason[:_MAX_REASON_CHARS])
    logger.warning("job.parse_failed job_id=%s reason=%s", job_id, reason[:200])


_CLEAR_FAILURE_SQL = """
UPDATE jobs SET
    failure_reason = NULL,
    updated_at = now()
WHERE id = $1 AND status = 'draft'
"""


async def clear_parse_failure(conn: DbConn, job_id: UUID) -> None:
    """Wipe a stale ``failure_reason`` so a queued retry reads as in-flight.

    Called by ``POST /jobs/{id}/reparse`` BEFORE it enqueues, never after. The
    reverse order races a fast worker: it could finish, write its own fresh
    ``failure_reason``, and then have this clear it — leaving a row that failed
    with nothing at all to show for it, which is the silently-stranded shape
    the re-parse route exists to end rather than create.

    Deliberately not merged into ``record_parsed``'s own ``failure_reason =
    NULL``: that one fires on SUCCESS, whereas this fires on INTENT, and the
    two must stay independently truthful.
    """
    await conn.execute(_CLEAR_FAILURE_SQL, job_id)


# ── CRUD / status state machine (Phase 6) ───────────────────────────────────

_JOB_COLS = (
    "id, title, department, location, employment_type, seniority, min_years, "
    "description_raw, description_parsed, status, retention_days, "
    "shortlist_top_percent, "
    "blind_review, failure_reason, created_by, created_at, updated_at, "
    "parsed_at, closed_at"
)

_INSERT_JOB_SQL = f"""
INSERT INTO jobs (
    title, department, location, employment_type, seniority, min_years,
    description_raw, retention_days, shortlist_top_percent, blind_review,
    created_by, description_sha256
) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
RETURNING {_JOB_COLS}
"""

# Dedup probe for bulk create: is there already a job with this exact JD text?
# GLOBAL scope — jobs have no parent aggregate to scope a duplicate to.
_JOB_BY_SHA_SQL = "SELECT id FROM jobs WHERE description_sha256 = $1 LIMIT 1"


def _description_sha256(description_raw: str) -> str:
    """The dedup key: SHA-256 of the JD text, hex. Two jobs with byte-identical
    ``description_raw`` share a hash (and are treated as duplicates)."""
    return hashlib.sha256(description_raw.encode("utf-8")).hexdigest()


async def _insert_job(
    conn: DbConn,
    payload: JobCreate,
    *,
    created_by: str | None,
    description_sha256: str,
) -> Any:
    """THE single INSERT both ``create_job`` and ``create_jobs_bulk`` write
    through, so the column list stays in one place."""
    return await conn.fetchrow(
        _INSERT_JOB_SQL,
        payload.title,
        payload.department,
        payload.location,
        payload.employment_type,
        payload.seniority,
        payload.min_years,
        payload.description_raw,
        payload.retention_days,
        payload.shortlist_top_percent,
        payload.blind_review,
        created_by,
        description_sha256,
    )


_GET_JOB_SQL = f"SELECT {_JOB_COLS} FROM jobs WHERE id = $1"

_LIST_JOBS_BASE_SQL = (
    "SELECT id, title, department, status, created_at, parsed_at FROM jobs"
)

_UPDATE_STATUS_SQL = f"""
UPDATE jobs SET status = $2, updated_at = now() WHERE id = $1
RETURNING {_JOB_COLS}
"""

# Strictly-forward state graph: draft -> open -> closed -> archived. No
# skipping, no same-state no-op, no backward move — anything not in this map
# (as the CURRENT status's single allowed next state) is rejected.
_FORWARD_TRANSITIONS: dict[str, str] = {
    "draft": "open",
    "open": "closed",
    "closed": "archived",
}

# Columns PATCH /jobs/{id} is allowed to touch. ``JobUpdate`` (src/schemas/
# jobs.py) has no ``status`` field at all — status changes are exclusively
# the job of ``transition_status``'s state-machine guard — so this allowlist
# is a defensive belt-and-suspenders filter in case a future field is ever
# added to ``JobUpdate`` without updating this set.
_UPDATABLE_JOB_COLUMNS: frozenset[str] = frozenset(
    {
        "title",
        "department",
        "location",
        "employment_type",
        "seniority",
        "min_years",
        "description_raw",
        "retention_days",
        "shortlist_top_percent",
        "blind_review",
    }
)


def _row_to_jobout(row: Any) -> JobOut:
    """THE single place that builds a ``JobOut`` from a raw DB row.

    ADR-006 / Phase 2 security "low": ``JobOut.blind_review`` defaults to
    ``False`` (fail-OPEN) if a builder ever omits it — this function MUST
    read ``row["blind_review"]`` explicitly, never rely on the pydantic
    default.
    """
    raw = dict(row)
    desc_parsed_raw = raw["description_parsed"]
    if isinstance(desc_parsed_raw, str):
        desc_parsed_raw = json.loads(desc_parsed_raw)
    description_parsed = (
        JDExtracted.model_validate(desc_parsed_raw)
        if desc_parsed_raw is not None
        else None
    )
    return JobOut(
        id=raw["id"],
        title=raw["title"],
        department=raw["department"],
        location=raw["location"],
        employment_type=raw["employment_type"],
        seniority=raw["seniority"],
        min_years=raw["min_years"],
        description_raw=raw["description_raw"],
        description_parsed=description_parsed,
        status=raw["status"],
        retention_days=raw["retention_days"],
        shortlist_top_percent=raw["shortlist_top_percent"],
        blind_review=raw["blind_review"],
        failure_reason=raw["failure_reason"],
        created_by=raw["created_by"],
        created_at=raw["created_at"],
        updated_at=raw["updated_at"],
        parsed_at=raw["parsed_at"],
        closed_at=raw["closed_at"],
    )


async def create_job(
    conn: DbConn, payload: JobCreate, *, created_by: str | None
) -> JobOut:
    """Insert a new job row (status='draft', the DDL default) and return the
    full ``JobOut`` built from the inserted row."""
    row = await _insert_job(
        conn,
        payload,
        created_by=created_by,
        description_sha256=_description_sha256(payload.description_raw),
    )
    return _row_to_jobout(row)


def _payload_from_manifest(
    *, description_raw: str, title: str, row: JobManifestRow | None
) -> JobCreate:
    """Build a ``JobCreate`` for one bulk file: a manifest row's non-empty
    metadata overrides the derived title and fills the optional columns; absent
    fields fall back to ``JobCreate``'s own defaults. May raise
    ``pydantic.ValidationError`` (e.g. a too-short JD, or an out-of-range
    manifest ``retention_days``) — the caller turns that into a ``failed`` row."""
    kwargs: dict[str, Any] = {"title": title, "description_raw": description_raw}
    if row is not None:
        if row.title:
            kwargs["title"] = row.title
        if row.department is not None:
            kwargs["department"] = row.department
        if row.location is not None:
            kwargs["location"] = row.location
        if row.employment_type is not None:
            kwargs["employment_type"] = row.employment_type
        if row.seniority is not None:
            kwargs["seniority"] = row.seniority
        if row.min_years is not None:
            kwargs["min_years"] = row.min_years
        if row.retention_days is not None:
            kwargs["retention_days"] = row.retention_days
        if row.blind_review is not None:
            kwargs["blind_review"] = row.blind_review
    return JobCreate(**kwargs)


async def create_jobs_bulk(
    conn: DbConn,
    *,
    files: list[tuple[str, bytes]],
    manifest: dict[str, JobManifestRow] | None,
    created_by: str | None,
) -> list[BulkJobResult]:
    """Create ONE draft job per uploaded JD file, returning a ``BulkJobResult``
    per file so the caller can enqueue ``parse_job`` once per *created* job.

    Per file: extract JD text (an unreadable/empty file → ``failed``), dedup on
    ``description_sha256`` (an identical JD already in ``jobs`` — or earlier in
    THIS batch — → ``duplicate``, no insert), apply the manifest row's overrides
    (or the filename-stem title), then insert. A JD below the 50-char floor (or
    any other ``JobCreate`` validation failure) becomes a ``failed`` row WITHOUT
    aborting the rest of the batch. Dedup scope is GLOBAL — jobs have no parent
    aggregate to scope a duplicate to.

    Transaction scoping is the caller's job (mirrors the rest of this module);
    each inserted row is visible to the next file's dedup probe via the in-batch
    ``created_in_batch`` map, so two identical JDs in one request don't both
    insert even before the first commits.
    """
    results: list[BulkJobResult] = []
    created_in_batch: dict[str, UUID] = {}  # sha -> job id created this batch

    for filename, content in files:
        try:
            description_raw = extract_jd_text(content, filename=filename)
        except JDImportError as exc:
            results.append(
                BulkJobResult(
                    original_filename=filename,
                    outcome="failed",
                    reason=exc.message[:_MAX_REASON_CHARS],
                )
            )
            continue

        # Floor check BEFORE the dedup probe: a sub-floor JD is never valid, so
        # fail it outright rather than possibly reporting it as a "duplicate".
        if len(description_raw) < _MIN_DESCRIPTION_CHARS:
            results.append(
                BulkJobResult(
                    original_filename=filename,
                    outcome="failed",
                    reason=(
                        f"extracted description is too short "
                        f"({len(description_raw)} chars; min "
                        f"{_MIN_DESCRIPTION_CHARS})"
                    ),
                )
            )
            continue

        sha = _description_sha256(description_raw)
        existing = await conn.fetchval(_JOB_BY_SHA_SQL, sha) or created_in_batch.get(
            sha
        )
        if existing is not None:
            results.append(
                BulkJobResult(
                    original_filename=filename,
                    outcome="duplicate",
                    job_id=(
                        existing if isinstance(existing, UUID) else UUID(str(existing))
                    ),
                    reason="a job with an identical description already exists",
                )
            )
            continue

        row = manifest.get(basename_lower(filename)) if manifest else None
        try:
            payload = _payload_from_manifest(
                description_raw=description_raw,
                title=title_from_filename(filename),
                row=row,
            )
        except ValidationError as exc:
            results.append(
                BulkJobResult(
                    original_filename=filename,
                    outcome="failed",
                    reason=str(exc)[:_MAX_REASON_CHARS],
                )
            )
            continue

        inserted = await _insert_job(
            conn, payload, created_by=created_by, description_sha256=sha
        )
        job = _row_to_jobout(inserted)
        created_in_batch[sha] = job.id
        results.append(
            BulkJobResult(
                original_filename=filename,
                outcome="created",
                job_id=job.id,
                title=job.title,
            )
        )
    return results


_JOB_ASSIGNEE_EXISTS_SQL = (
    "EXISTS (SELECT 1 FROM job_assignees "
    "WHERE job_assignees.job_id = jobs.id "
    "AND job_assignees.user_id = ${n})"
)


async def get_job(conn: DbConn, job_id: UUID, *, user_id: UUID | None = None) -> JobOut:
    """Fetch one job by id. Raises ``NotFoundError`` when it does not resolve.

    FU-6 slice 5 (ADR-020 §3/§5) — ``user_id`` row-scopes the read. When
    supplied (a hiring_manager session), the query gains an ``EXISTS``
    predicate against ``job_assignees``; a job that exists but is not
    assigned to ``user_id`` is filtered out at the SQL layer (0 rows), which
    this function surfaces as ``NotFoundError`` — observationally identical
    to a genuinely nonexistent job id (ADR-020 §5: 404, never 403). ``None``
    (the default, unscoped callers) leaves the SQL byte-identical to before
    this slice — no predicate, no extra bind arg.
    """
    if user_id is None:
        row = await conn.fetchrow(_GET_JOB_SQL, job_id)
    else:
        query = f"{_GET_JOB_SQL} AND {_JOB_ASSIGNEE_EXISTS_SQL.format(n=2)}"
        row = await conn.fetchrow(query, job_id, user_id)
    if row is None:
        raise NotFoundError(f"job {job_id} not found", job_id=str(job_id))
    return _row_to_jobout(row)


async def list_jobs(
    conn: DbConn,
    *,
    limit: int = 50,
    offset: int = 0,
    status: str | None = None,
    user_id: UUID | None = None,
) -> list[JobListItem]:
    """Light rows for list views. ``status`` omitted means "every status" —
    never silently scoped to one status when the filter is absent.

    FU-6 slice 5 (ADR-020 §3/§4) — ``user_id`` row-scopes the list to a
    hiring_manager's assigned jobs via an ``EXISTS`` predicate against
    ``job_assignees``. ``None`` (the default) leaves the SQL byte-identical
    to before this slice for unscoped callers (admin/recruiter/auditor).
    """
    args: list[Any] = []
    where_clauses: list[str] = []
    if status is not None:
        args.append(status)
        where_clauses.append(f"status = ${len(args)}")
    if user_id is not None:
        args.append(user_id)
        where_clauses.append(_JOB_ASSIGNEE_EXISTS_SQL.format(n=len(args)))

    query = _LIST_JOBS_BASE_SQL
    if where_clauses:
        query += " WHERE " + " AND ".join(where_clauses)
    args.append(limit)
    query += f" ORDER BY created_at DESC LIMIT ${len(args)}"
    args.append(offset)
    query += f" OFFSET ${len(args)}"
    rows = await conn.fetch(query, *args)
    # Explicit field-by-field projection (not `dict(r)`) — the real SQL only
    # selects these six columns, but a test double (or a future query that
    # widens its SELECT) may hand back a row carrying extra keys; JobListItem
    # is extra="forbid", so trusting the row's own key set would be fragile.
    return [
        JobListItem(
            id=r["id"],
            title=r["title"],
            department=r["department"],
            status=r["status"],
            created_at=r["created_at"],
            parsed_at=r["parsed_at"],
        )
        for r in rows
    ]


async def transition_status(conn: DbConn, job_id: UUID, to: str) -> JobOut:
    """Validate ``to`` against the strictly-forward state graph and apply it.

    Raises ``NotFoundError`` when the job doesn't exist, and ``ValueError``
    (the route's 409 material) for any transition not in the graph —
    including a same-state no-op and a backward move.
    """
    current = await conn.fetchval("SELECT status FROM jobs WHERE id = $1", job_id)
    if current is None:
        raise NotFoundError(f"job {job_id} not found", job_id=str(job_id))
    if _FORWARD_TRANSITIONS.get(current) != to:
        raise ValueError(f"invalid status transition: {current!r} -> {to!r}")
    row = await conn.fetchrow(_UPDATE_STATUS_SQL, job_id, to)
    return _row_to_jobout(row)


_JOB_BLIND_REVIEW_SQL = "SELECT blind_review FROM jobs WHERE id = $1"


async def update_job(
    conn: DbConn,
    job_id: UUID,
    payload: JobUpdate,
    *,
    actor_kind: str,
    actor_user_id: UUID | None,
    actor_service: str | None,
) -> JobOut:
    """General partial update for ``PATCH /jobs/{id}``.

    Builds the SQL SET clause from ``payload.model_dump(exclude_unset=True)``
    — NOT from the model's full field set — because a PATCH omit must mean
    "unchanged", not "set to null". This matters most for ``blind_review``:
    it is a plain ``bool``, so ``False`` is a legitimate value a client sends
    on purpose and must stay distinguishable from "the client didn't send
    this field at all".

    ``status`` is deliberately never writable here — ``JobUpdate`` carries no
    ``status`` field (status has its own state-machine-guarded transition
    endpoint, ``transition_status``) and ``_UPDATABLE_JOB_COLUMNS`` filters
    anything outside the known-safe column set as a defensive second layer.

    Raises ``NotFoundError`` when the job id does not resolve. An empty
    payload (nothing sent, or everything filtered out) is a no-op that still
    returns the current row — it never issues an UPDATE with an empty SET
    clause, which is invalid SQL.

    **FU-5 slice 9 (ADR-019 §1.4/§6) — attributable audit for the
    ``blind_review`` flip.** ``actor_kind``/``actor_user_id``/
    ``actor_service`` are the caller's (the route's) already-resolved audit
    identity — this function forwards them verbatim into
    ``audit_service.record_audit``, never inventing or overriding them. When
    (and only when) the payload actually touches ``blind_review``, the
    PRE-update value is read via ``fetchval`` and compared against the new
    value; a real flip writes exactly one ``audit_log`` row
    (``action='blind_review_toggled'``), a same-value resend or an omitted
    field writes none. The UPDATE and the conditional audit INSERT run
    inside the SAME ``conn.transaction()`` — SECURITY-CRITICAL: a crash
    partway through must never leave the flip applied without an audit row
    (the opposite ordering concern from reveal's audit-before-decrypt, whose
    autocommit-first write must survive a crash in the *other* direction).
    """
    fields = {
        col: val
        for col, val in payload.model_dump(exclude_unset=True).items()
        if col in _UPDATABLE_JOB_COLUMNS
    }
    if not fields:
        return await get_job(conn, job_id)

    blind_review_touched = "blind_review" in fields

    set_clauses = []
    args: list[Any] = [job_id]
    for col, val in fields.items():
        args.append(val)
        set_clauses.append(f"{col} = ${len(args)}")
    query = (
        f"UPDATE jobs SET {', '.join(set_clauses)}, updated_at = now() "
        f"WHERE id = $1 RETURNING {_JOB_COLS}"
    )

    async with conn.transaction():
        old_blind_review: bool | None = None
        if blind_review_touched:
            old_blind_review = await conn.fetchval(_JOB_BLIND_REVIEW_SQL, job_id)

        row = await conn.fetchrow(query, *args)
        if row is None:
            raise NotFoundError(f"job {job_id} not found", job_id=str(job_id))

        if blind_review_touched:
            new_blind_review = fields["blind_review"]
            if old_blind_review != new_blind_review:
                await audit_service.record_audit(
                    conn,
                    actor_kind=actor_kind,
                    actor_user_id=actor_user_id,
                    actor_service=actor_service,
                    action="blind_review_toggled",
                    subject_type="job",
                    subject_id=job_id,
                    job_id=job_id,
                    details={"old": old_blind_review, "new": new_blind_review},
                )

    return _row_to_jobout(row)
