"""Résumé write-back — the worker's half of the résumé lifecycle.

Phase 3 ports ONLY the worker write-back trio from hris
``apps/api/src/api/services/resume_service.py``: ``encrypt_pii_via_session``,
``record_parsed`` and ``record_parse_failure``. Upload/validation
(``store_uploaded_blob``, ``detect_mime``, ``FileRejected``) is Phase 6 and the
read side (``list_for_job``, ``get_one``, ``reveal_identity``, ``_blind_parsed``)
is Phase 5 (redaction) — none of it is on the parse path.

``record_parsed`` carries the same optimistic-concurrency guard as the job
variant, widened to the two non-terminal résumé states: the UPDATE applies only
while the row is still ``status IN ('uploaded', 'parsing')``. A résumé that was
deleted, already parsed, or already failed under us yields 0 rows, we return
``False``, and the caller (``parse_resume``) returns ``"stale"`` WITHOUT
enqueueing an outbox row.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
from typing import Any
from uuid import UUID, uuid4

from src.errors import NotFoundError
from src.pipeline.parsing import (
    MIME_DOCX,
    MIME_PDF,
    MIME_RTF,
    MIME_TXT,
    UnsupportedMimeError,
)
from src.schemas.resumes import (
    CandidateInfo,
    CoverLetterParsed,
    ResumeListItem,
    ResumeOut,
    ResumeParsed,
    ResumeStatusBreakdown,
    ResumeUploadResult,
)
from src.services import DbConn, audit_service, outbox_service
from src.services import pii as pii_service
from src.services.bulk_ingest_service import basename_lower
from src.services.pii import set_pii_key
from src.services.redaction import (
    blind_label_map,
    is_foreign_location,
    redact_text,
    redacted_filename,
)
from src.storage.blob_store import BlobStore

logger = logging.getLogger(__name__)

_MAX_REASON_CHARS = 1000

# Matches src/pipeline/parsing/extract.py's per-document cap — the upload
# boundary must not accept anything the extractor would reject anyway.
_MAX_UPLOAD_BYTES = 10 * 1024 * 1024

_EXT_MIME: dict[str, str] = {
    "pdf": MIME_PDF,
    "docx": MIME_DOCX,
    "rtf": MIME_RTF,
    "txt": MIME_TXT,
}
_MIME_EXT: dict[str, str] = {v: k for k, v in _EXT_MIME.items()}

# (candidate_name_enc, candidate_email_enc, candidate_phone_enc, email_hash)
EncryptedPii = tuple[bytes | None, bytes | None, bytes | None, str | None]

_RECORD_PARSED_SQL = """
UPDATE resumes SET
    parsed = $2::jsonb,
    candidate_name = $3,
    candidate_email = $4,
    candidate_phone = $5,
    candidate_email_hash = $6,
    cover_letter_parsed = $7::jsonb,
    status = 'parsed',
    parsed_at = $8,
    failure_reason = NULL
WHERE id = $1 AND status IN ('uploaded', 'parsing')
"""

_RECORD_FAILURE_SQL = """
UPDATE resumes SET
    status = 'failed',
    failure_reason = $2
WHERE id = $1 AND status IN ('uploaded', 'parsing')
"""

# FU-7 (ADR-021 §3): the guarded WHERE clause is `status = 'uploaded'` ONLY —
# never `('uploaded', 'parsing')` — a row already 'parsing' (a retry) matches
# zero rows and is a normal, non-error outcome (see `claim_parsing`'s
# docstring); a TERMINAL row ('parsed'/'failed') must never be silently
# reopened by a stray/duplicate enqueue.
_CLAIM_PARSING_SQL = (
    "UPDATE resumes SET status = 'parsing' WHERE id = $1 AND status = 'uploaded'"
)


async def encrypt_pii_via_session(
    conn: DbConn, candidate: CandidateInfo
) -> EncryptedPii:
    """Encrypt the candidate's PII and derive the lookup hash.

    ``set_pii_key`` runs EXACTLY ONCE and STRICTLY FIRST: ``set_config`` is
    transaction-scoped, so it has to land before any ``current_setting()`` read
    in the same transaction — every ``encrypt`` below is such a read. The caller
    MUST already be inside ``async with conn.transaction():``.

    The email hash is computed in Python and stays PLAINTEXT — it is the
    subject-access lookup key, not a secret.
    """
    await pii_service.set_pii_key(conn)
    return (
        await pii_service.encrypt(conn, candidate.name),
        await pii_service.encrypt(conn, candidate.email),
        await pii_service.encrypt(conn, candidate.phone),
        pii_service.email_hash(candidate.email),
    )


async def record_parsed(
    conn: DbConn,
    resume_id: UUID,
    parsed: ResumeParsed,
    pii: EncryptedPii,
    cover_letter_parsed: CoverLetterParsed | None,
    parsed_at: dt.datetime,
) -> bool:
    """Write the LLM extraction + the encrypted PII back onto the résumé row.

    ``parsed`` already carries ``chunks`` and ``cover_letter_chunks``;
    ``cover_letter_parsed`` is the separate cover-letter extraction, or ``None``
    when there is no cover letter (or its parse failed — a non-fatal path).

    Returns ``True`` when the UPDATE applied, ``False`` when the row was no
    longer in ('uploaded', 'parsing') — see the module docstring.
    """
    name_enc, email_enc, phone_enc, email_hash = pii
    result = await conn.execute(
        _RECORD_PARSED_SQL,
        resume_id,
        json.dumps(parsed.model_dump()),
        name_enc,
        email_enc,
        phone_enc,
        email_hash,
        json.dumps(cover_letter_parsed.model_dump()) if cover_letter_parsed else None,
        parsed_at,
    )
    applied = result.endswith(" 1")
    if not applied:
        logger.info("resume.record_parsed.stale resume_id=%s", resume_id)
    return applied


async def claim_parsing(conn: DbConn, resume_id: UUID) -> bool:
    """Flip the résumé row ``uploaded`` -> ``parsing`` at task start (FU-7,
    ADR-021 §3) — BEFORE any blob I/O, so a worker crash mid-parse leaves the
    row observably ``'parsing'``, never silently stuck at ``'uploaded'``
    forever.

    Returns ``True`` iff THIS call flipped the row (a first entry). Returns
    ``False`` on a RETRY (the row is already ``'parsing'`` — 0 rows matched)
    or on a row already in a terminal state (``'parsed'``/``'failed'``); the
    caller must NOT branch on the result — it is for logging only, never
    control flow (a retry proceeds exactly like a fresh claim).
    """
    result = await conn.execute(_CLAIM_PARSING_SQL, resume_id)
    return result.endswith(" 1")


async def record_parse_failure(conn: DbConn, resume_id: UUID, reason: str) -> None:
    """Mark the résumé failed and surface why. Terminal — no retry."""
    await conn.execute(_RECORD_FAILURE_SQL, resume_id, reason[:_MAX_REASON_CHARS])
    logger.warning(
        "resume.parse_failed resume_id=%s reason=%s", resume_id, reason[:200]
    )


# ── upload / validation (Phase 6) ────────────────────────────────────────────


def detect_mime(filename: str, data: bytes) -> str:
    """Sniff the real mime type from MAGIC BYTES and cross-check it against
    the filename's extension. Raises ``UnsupportedMimeError`` (the SAME
    exception ``extract_text`` raises, so callers have one type to catch) on
    an unsupported extension OR a extension/content mismatch (a
    ``.pdf``-named file whose bytes are actually a DOCX zip)."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    expected = _EXT_MIME.get(ext)
    if expected is None:
        raise UnsupportedMimeError(f"unsupported file extension: {filename!r}")
    actual = _sniff_mime(data)
    if actual != expected:
        raise UnsupportedMimeError(
            f"file {filename!r} has extension {ext!r} but its content sniffs "
            f"as {actual!r}, not {expected!r}"
        )
    return expected


def _sniff_mime(data: bytes) -> str:
    if data.startswith(b"%PDF"):
        return MIME_PDF
    if data.startswith(b"PK\x03\x04"):
        return MIME_DOCX
    if data.lstrip().startswith(rb"{\rtf"):
        return MIME_RTF
    return MIME_TXT


_JOB_EXISTS_SQL = "SELECT id FROM jobs WHERE id = $1"

_DEDUP_SQL = "SELECT id FROM resumes WHERE job_id = $1 AND sha256 = $2"

_INSERT_UPLOADED_SQL = """
INSERT INTO resumes (
    id, job_id, blob_key, original_filename, mime_type, file_size_bytes,
    sha256, consent_acknowledged, uploaded_by, cover_letter_text,
    cover_letter_blob_key
) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
"""


def _validate_cover_letter_file(
    cover_letter_file: tuple[str, bytes],
) -> tuple[str, bytes, str]:
    """Validate a cover-letter file (empty / oversize / mime) and mint its
    server-generated ``cover_letters/{uuid4()}.{ext}`` blob key. Raises
    ``ValueError`` on any rejection so the caller fails loud (never swallowed
    into a "rejected" résumé row). Returns ``(blob_key, bytes, mime)`` — the
    blob write itself is deferred to INSIDE the caller's transaction."""
    cl_name, cl_bytes = cover_letter_file
    if len(cl_bytes) == 0:
        raise ValueError("cover letter file is empty")
    if len(cl_bytes) > _MAX_UPLOAD_BYTES:
        raise ValueError(
            f"cover letter file is {len(cl_bytes)} bytes; the cap is "
            f"{_MAX_UPLOAD_BYTES}"
        )
    try:
        cl_mime = detect_mime(cl_name, cl_bytes)
    except UnsupportedMimeError as exc:
        raise ValueError(f"unsupported cover letter file: {exc}") from exc
    return f"cover_letters/{uuid4()}.{_MIME_EXT[cl_mime]}", cl_bytes, cl_mime


async def upload_resumes(
    conn: DbConn,
    blob_store: BlobStore,
    *,
    job_id: UUID,
    files: list[tuple[str, bytes]],
    consent_acknowledged: bool,
    uploaded_by: str | None = None,
    cover_letter_file: tuple[str, bytes] | None = None,
    cover_letter_text: str | None = None,
    cover_letter_map: dict[str, tuple[str, bytes]] | None = None,
    warnings_map: dict[str, list[str]] | None = None,
) -> list[ResumeUploadResult]:
    """Validate + persist a batch of résumé files.

    ``consent_acknowledged=False`` raises ``ValueError`` immediately, before
    touching the blob store or the connection at all (one flag for the whole
    batch, not per file). Every other rejection (oversize, empty, unsupported
    mime, sha256 duplicate) yields a per-file ``ResumeUploadResult`` row and
    does not abort the rest of the batch.

    The blob key is ALWAYS server-generated (``resumes/{uuid4()}.{ext}``) —
    the same ``uuid4()`` also backs the inserted row's primary key, so a
    caller can enqueue the parse task without a second DB round trip. A
    pasted ``cover_letter_text`` is encrypted (via ``pii_service.set_pii_key``
    + ``pii_service.encrypt``, called through the ``pii_service`` module
    object so it stays monkeypatchable) inside the SAME transaction as the
    résumé row insert(s) — a failure there (e.g. an unset PII_KEY) propagates
    uncaught, never swallowed into a "rejected" row.

    ``cover_letter_map`` (FU-3 Slice 2, ADDITIVE) maps ``basename_lower(résumé
    filename)`` → the cover ``(filename, bytes)`` paired to THAT résumé; each
    such cover is validated + stored as its OWN distinct ``cover_letters/``
    blob keyed on that résumé's row (inside the transaction, per SEC#7). When
    the map is empty the singular ``cover_letter_file`` path applies unchanged.
    ``warnings_map`` (same key) carries non-fatal pairing notes surfaced on the
    result row.
    """
    if not consent_acknowledged:
        raise ValueError(
            "consent_acknowledged must be true before any résumé upload I/O"
        )

    # Backend-robustness fix (diagnosed live): a job_id with no matching
    # `jobs` row must be rejected here, BEFORE any blob write or résumé
    # INSERT is attempted — otherwise the INSERT's `resumes_job_id_fkey`
    # trips a raw `asyncpg.exceptions.ForeignKeyViolationError` that
    # surfaces as an unhandled 500 at the route layer. This mirrors the
    # existence-check convention already used elsewhere (job_service.get_job,
    # resumes.py's own `_EXISTS_SQL`) rather than catching the FK violation
    # itself, so the contract has no dependency on the driver's exception
    # type or message text.
    job_exists = await conn.fetchval(_JOB_EXISTS_SQL, job_id)
    if job_exists is None:
        raise NotFoundError(f"job {job_id} not found", job_id=str(job_id))

    cover_map = cover_letter_map or {}
    warn_map = warnings_map or {}

    # A file-attached cover letter is stored as a blob (like the résumé itself)
    # and its key set on the row; the worker's `_parse_cover_letter` extracts +
    # LLM-parses it at parse time from `cover_letter_blob_key` — the same
    # deferred path a pasted `cover_letter_text` takes, and non-fatal to the
    # résumé parse. Validated + size-capped here so a bad cover letter fails
    # loud at upload rather than silently later. Validate up front (fail fast,
    # before opening a transaction), but defer the blob write to INSIDE the
    # transaction below so its orphan-on-rollback window matches the résumé
    # blobs'.
    singular_cover_payload: tuple[str, bytes, str] | None = None
    if cover_letter_file is not None:
        singular_cover_payload = _validate_cover_letter_file(cover_letter_file)

    results: list[ResumeUploadResult] = []

    async with conn.transaction():
        singular_cover_key: str | None = None
        if singular_cover_payload is not None:
            k, b, m = singular_cover_payload
            await blob_store.put(k, b, content_type=m)
            singular_cover_key = k
        encrypted_cover: bytes | None = None
        if cover_letter_text is not None:
            await pii_service.set_pii_key(conn)
            encrypted_cover = await pii_service.encrypt(conn, cover_letter_text)

        for filename, data in files:
            row_key = basename_lower(filename)
            row_warnings = list(warn_map.get(row_key, []))
            if len(data) == 0:
                results.append(
                    ResumeUploadResult(
                        original_filename=filename,
                        outcome="rejected",
                        reason="file is empty",
                        warnings=row_warnings,
                    )
                )
                continue
            if len(data) > _MAX_UPLOAD_BYTES:
                results.append(
                    ResumeUploadResult(
                        original_filename=filename,
                        outcome="rejected",
                        reason=(
                            f"file is {len(data)} bytes; the cap is "
                            f"{_MAX_UPLOAD_BYTES}"
                        ),
                        warnings=row_warnings,
                    )
                )
                continue

            try:
                mime = detect_mime(filename, data)
            except UnsupportedMimeError as exc:
                results.append(
                    ResumeUploadResult(
                        original_filename=filename,
                        outcome="rejected",
                        reason=str(exc),
                        warnings=row_warnings,
                    )
                )
                continue

            sha = hashlib.sha256(data).hexdigest()
            dup_id = await conn.fetchval(_DEDUP_SQL, job_id, sha)
            if dup_id is not None:
                results.append(
                    ResumeUploadResult(
                        original_filename=filename,
                        outcome="duplicate",
                        resume_id=dup_id,
                        warnings=row_warnings,
                    )
                )
                continue

            # A per-résumé paired cover (FU-3) takes precedence over the batch
            # singular cover; each gets its OWN distinct blob key on THIS row.
            row_cover_key = singular_cover_key
            cover_letter_filename: str | None = None
            paired_cover = cover_map.get(row_key)
            if paired_cover is not None:
                cb_key, cb_bytes, cb_mime = _validate_cover_letter_file(paired_cover)
                await blob_store.put(cb_key, cb_bytes, content_type=cb_mime)
                row_cover_key = cb_key
                cover_letter_filename = paired_cover[0]

            resume_id = uuid4()
            blob_key = f"resumes/{resume_id}.{_MIME_EXT[mime]}"
            await blob_store.put(blob_key, data, content_type=mime)
            await conn.execute(
                _INSERT_UPLOADED_SQL,
                resume_id,
                job_id,
                blob_key,
                filename,
                mime,
                len(data),
                sha,
                consent_acknowledged,
                uploaded_by,
                encrypted_cover,
                row_cover_key,
            )
            results.append(
                ResumeUploadResult(
                    original_filename=filename,
                    outcome="accepted",
                    resume_id=resume_id,
                    cover_letter_filename=cover_letter_filename,
                    warnings=row_warnings,
                )
            )

    return results


# ── read (Phase 5) ─────────────────────────────────────────────────────────

_LIST_SQL_BASE = (
    "SELECT id, original_filename, status, uploaded_at, parsed_at, "
    "pgp_sym_decrypt(candidate_name, current_setting('app.pii_key')) "
    "AS candidate_name, "
    # Feature 2: surface whether a cover letter is attached (blob or pasted
    # text) so the list can badge it. Cheap boolean, no PII.
    "(cover_letter_blob_key IS NOT NULL OR cover_letter_text IS NOT NULL) "
    "AS has_cover_letter, "
    # ADR-026 (FU-8): the withdrawal lifecycle pair — NOT PII, no redaction.
    "withdrawn_at, withdrawal_reason, "
    # FU-7 §4 / ADR-030: the degraded flag, read from the parsed jsonb. A
    # pre-feature / never-parsed row (parsed IS NULL or no key) COALESCEs to
    # false. NOT PII — surfaces in the list even under blind review.
    "COALESCE((parsed->>'degraded')::bool, false) AS degraded "
    "FROM resumes WHERE job_id = $1"
)

# FU-6 slice 6 (ADR-020 §3) — the row-scoping predicate, keyed on
# ``resumes.job_id`` (résumés have no assignee column of their own; scoping
# rides the job they belong to). Mirrors ``job_service._JOB_ASSIGNEE_EXISTS_SQL``.
_RESUME_ASSIGNEE_EXISTS_SQL = (
    "EXISTS (SELECT 1 FROM job_assignees "
    "WHERE job_assignees.job_id = resumes.job_id "
    "AND job_assignees.user_id = ${n})"
)

_GET_SQL = """
SELECT id, job_id, original_filename, mime_type, file_size_bytes, sha256,
    pgp_sym_decrypt(candidate_name, current_setting('app.pii_key')) AS c_name,
    pgp_sym_decrypt(candidate_email, current_setting('app.pii_key')) AS c_email,
    pgp_sym_decrypt(candidate_phone, current_setting('app.pii_key')) AS c_phone,
    pgp_sym_decrypt(cover_letter_text, current_setting('app.pii_key')) AS cl_text,
    cover_letter_parsed,
    candidate_email_hash, parsed, status, uploaded_by, uploaded_at,
    parsed_at, failure_reason, consent_acknowledged,
    withdrawn_at, withdrawal_reason
FROM resumes WHERE id = $1
"""

_BLIND_CHECK_SQL = (
    "SELECT j.blind_review FROM jobs j "
    "JOIN resumes r ON r.job_id = j.id WHERE r.id = $1"
)


async def list_for_job(
    conn: DbConn,
    *,
    job_id: UUID,
    limit: int = 100,
    offset: int = 0,
    user_id: UUID | None = None,
) -> list[ResumeListItem]:
    """List résumés for a job. Under blind review the candidate name is masked
    to ``None`` — NOT a pseudonym: a résumé list has no rank to build one from
    (unlike the shortlist, which always carries a rank).

    FU-6 slice 6 (ADR-020 §3/§4) — ``user_id`` row-scopes the list to a
    hiring_manager's assigned jobs via an ``EXISTS`` predicate against
    ``job_assignees``, keyed on ``resumes.job_id`` (résumés carry no assignee
    column of their own). ``None`` (the default) leaves the SQL byte-
    identical to before this slice for unscoped callers (admin/recruiter/
    auditor). An unassigned/nonexistent ``job_id`` yields an empty list —
    never a 404 — matching this route's pre-existing behaviour for a
    genuinely nonexistent job (ADR-020 §5).
    """
    blind = await conn.fetchval("SELECT blind_review FROM jobs WHERE id = $1", job_id)
    args: list[Any] = [job_id]
    query = _LIST_SQL_BASE
    if user_id is not None:
        args.append(user_id)
        query += f" AND {_RESUME_ASSIGNEE_EXISTS_SQL.format(n=len(args))}"
    args.append(limit)
    query += f" ORDER BY uploaded_at DESC LIMIT ${len(args)}"
    args.append(offset)
    query += f" OFFSET ${len(args)}"
    async with conn.transaction():
        await set_pii_key(conn)
        rows = await conn.fetch(query, *args)
    return [
        ResumeListItem(
            id=r["id"],
            # A résumé named after its candidate leaks identity — mask the
            # filename under blind review, keeping only its extension.
            original_filename=(
                redacted_filename(r["original_filename"])
                if blind
                else r["original_filename"]
            ),
            status=r["status"],
            uploaded_at=r["uploaded_at"],
            parsed_at=r["parsed_at"],
            candidate_name=None if blind else r["candidate_name"],
            has_cover_letter=r["has_cover_letter"],
            withdrawn_at=r["withdrawn_at"],
            withdrawal_reason=r["withdrawal_reason"],
            # ``COALESCE(... , false)`` in the SQL guarantees a real bool from
            # a live row; ``bool(...)`` also folds a ``None`` (a pre-feature
            # row / a mocked Record without the key) to ``False``.
            degraded=bool(r["degraded"]),
        )
        for r in rows
    ]


async def get_one(
    conn: DbConn,
    resume_id: UUID,
    *,
    reveal: bool = False,
    user_id: UUID | None = None,
) -> ResumeOut:
    """Fetch + decrypt one résumé. Opens its own transaction and sets the PII
    key. ``reveal=True`` bypasses blind-review masking (the audited un-blind
    path). Under blind review, identity is redacted BEFORE the DTO is built
    (ADR-006 §4) — no decrypted PII ever reaches ``ResumeOut``.

    FU-6 slice 6 (ADR-020 §3/§5) — ``user_id`` row-scopes the read via an
    ``EXISTS`` predicate against ``job_assignees`` (keyed on
    ``resumes.job_id``). A résumé that exists but is not assigned to
    ``user_id`` is filtered out at the SQL layer (0 rows), which this
    function surfaces as ``NotFoundError`` — observationally identical to a
    genuinely nonexistent résumé id (ADR-020 §5: 404, never 403). ``None``
    (the default, unscoped callers) leaves the SQL byte-identical to before
    this slice — no predicate, no extra bind arg.
    """
    async with conn.transaction():
        await set_pii_key(conn)
        if user_id is None:
            row = await conn.fetchrow(_GET_SQL, resume_id)
        else:
            query = f"{_GET_SQL} AND {_RESUME_ASSIGNEE_EXISTS_SQL.format(n=2)}"
            row = await conn.fetchrow(query, resume_id, user_id)
    if row is None:
        raise NotFoundError(f"resume {resume_id} not found", resume_id=str(resume_id))

    parsed: ResumeParsed | None = None
    raw_parsed = row["parsed"]
    if raw_parsed is not None:
        if isinstance(raw_parsed, str):
            raw_parsed = json.loads(raw_parsed)
        parsed = ResumeParsed.model_validate(raw_parsed)

    cover_parsed = _parse_cover_letter(row["cover_letter_parsed"])

    blind = (not reveal) and await conn.fetchval(_BLIND_CHECK_SQL, resume_id)
    if blind:
        location = parsed.candidate.location if parsed else None
        shown_location = None if is_foreign_location(location) else location
        return ResumeOut(
            id=row["id"],
            job_id=row["job_id"],
            # A résumé named after its candidate ("Jane_Smith_Resume.pdf")
            # leaks identity verbatim — mask it, keeping only the extension.
            original_filename=redacted_filename(row["original_filename"]),
            mime_type=row["mime_type"],
            file_size_bytes=row["file_size_bytes"],
            sha256=row["sha256"],
            candidate=CandidateInfo(
                name=None, email=None, phone=None, location=shown_location
            ),
            candidate_email_hash=row["candidate_email_hash"],
            parsed=_blind_parsed(parsed, row["c_name"], row["c_email"], row["c_phone"]),
            status=row["status"],
            uploaded_by=row["uploaded_by"],
            uploaded_at=row["uploaded_at"],
            parsed_at=row["parsed_at"],
            failure_reason=row["failure_reason"],
            consent_acknowledged=row["consent_acknowledged"],
            blinded=True,
            # The cover letter carries PII (name/address) — withhold it under
            # blind review (an audited reveal surfaces it, like the name).
            cover_letter_text=None,
            cover_letter_parsed=None,
            # ADR-026 (FU-8): withdrawal state is not PII — surfaced under blind.
            withdrawn_at=row["withdrawn_at"],
            withdrawal_reason=row["withdrawal_reason"],
        )

    return ResumeOut(
        id=row["id"],
        job_id=row["job_id"],
        original_filename=row["original_filename"],
        mime_type=row["mime_type"],
        file_size_bytes=row["file_size_bytes"],
        sha256=row["sha256"],
        candidate=CandidateInfo(
            name=row["c_name"],
            email=row["c_email"],
            phone=row["c_phone"],
            location=parsed.candidate.location if parsed else None,
        ),
        candidate_email_hash=row["candidate_email_hash"],
        parsed=parsed,
        status=row["status"],
        uploaded_by=row["uploaded_by"],
        uploaded_at=row["uploaded_at"],
        parsed_at=row["parsed_at"],
        failure_reason=row["failure_reason"],
        consent_acknowledged=row["consent_acknowledged"],
        cover_letter_text=row["cl_text"],
        cover_letter_parsed=cover_parsed,
        withdrawn_at=row["withdrawn_at"],
        withdrawal_reason=row["withdrawal_reason"],
    )


def _parse_cover_letter(raw: Any) -> CoverLetterParsed | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        raw = json.loads(raw)
    return CoverLetterParsed.model_validate(raw)


def _blind_parsed(
    parsed: ResumeParsed | None,
    name: str | None,
    email: str | None,
    phone: str | None,
) -> ResumeParsed | None:
    """Mask the candidate for blind review: scrub name/contact from the text
    (summary, chunk text, experience bullets), replace employer + school names
    with stable labels in both the structured fields and the prose, drop
    graduation years, and mask **foreign** locations. Job titles, degree, field,
    employment dates, skills, and Canadian locations are left intact."""
    if parsed is None:
        return None

    labels = blind_label_map(
        employers=[e.company for e in parsed.experience],
        institutions=[ed.institution for ed in parsed.education],
    )
    location = parsed.candidate.location
    shown_location = None if is_foreign_location(location) else location

    def _r(text: str) -> str:
        return redact_text(
            text,
            name=name,
            email=email,
            phone=phone,
            term_map=labels,
            location=location,
            redact_locations=True,
        )

    return parsed.model_copy(
        update={
            "candidate": CandidateInfo(
                name=None, email=None, phone=None, location=shown_location
            ),
            "summary": _r(parsed.summary),
            "chunks": [
                c.model_copy(update={"text": _r(c.text)}) for c in parsed.chunks
            ],
            # Cover-letter chunk text carries the candidate's OWN letterhead
            # (name/email/phone in the first ~200 chars) — redact it exactly
            # like the résumé chunks so blind ResumeOut can't leak identity.
            "cover_letter_chunks": [
                c.model_copy(update={"text": _r(c.text)})
                for c in parsed.cover_letter_chunks
            ],
            "experience": [
                e.model_copy(
                    update={
                        "company": labels.get(e.company.strip(), e.company),
                        "bullets": [
                            b.model_copy(update={"text": _r(b.text)}) for b in e.bullets
                        ],
                    }
                )
                for e in parsed.experience
            ],
            "education": [
                ed.model_copy(
                    update={
                        "institution": labels.get(
                            ed.institution.strip(), ed.institution
                        ),
                        "year": None,  # graduation year is an age proxy
                    }
                )
                for ed in parsed.education
            ],
        }
    )


# ── withdrawal lifecycle (ADR-026, FU-8) ─────────────────────────────────────

_RESUME_EXISTS_SQL = "SELECT id FROM resumes WHERE id = $1"

# Guarded UPDATEs: the ``withdrawn_at`` predicate makes both operations
# idempotent — a second withdraw (or reinstate) matches zero rows, so no audit
# or outbox row is written.
_WITHDRAW_SQL = (
    "UPDATE resumes SET withdrawn_at = now(), withdrawal_reason = $2 "
    "WHERE id = $1 AND withdrawn_at IS NULL"
)
_REINSTATE_SQL = (
    "UPDATE resumes SET withdrawn_at = NULL, withdrawal_reason = NULL "
    "WHERE id = $1 AND withdrawn_at IS NOT NULL"
)

# The last DELIVERED ``resume.parsed`` payload for this résumé — the exact
# bytes reinstate REPLAYS (never re-embeds; ADR-026 decision 1).
_LAST_PARSED_PAYLOAD_SQL = (
    "SELECT payload FROM outbox "
    "WHERE aggregate_id = $1 AND event_type = 'resume.parsed' "
    "AND delivered_at IS NOT NULL "
    "ORDER BY delivered_at DESC, id DESC LIMIT 1"
)

# The five lifecycle buckets partition the job's résumés, PLUS a SIXTH
# ``'degraded'`` row (FU-7 §4 / ADR-030) UNIONed into the SAME result set — a
# SUB-count of ``parsed`` (degraded ⊆ parsed), never a peer bucket, so
# ``parsed`` keeps counting ALL parsed rows. The degraded row is emitted even
# at zero. A withdrawn-then-degraded row is excluded from the degraded row (it
# lands in the ``withdrawn`` peer bucket, so counting it here would break the
# ⊆-parsed invariant); a ``parsed IS NULL`` row COALESCEs to false.
_STATUS_BREAKDOWN_SQL = (
    "SELECT CASE WHEN withdrawn_at IS NOT NULL THEN 'withdrawn' "
    "ELSE status::text END AS bucket, count(*) AS n "
    "FROM resumes WHERE job_id = $1 GROUP BY 1 "
    "UNION ALL "
    "SELECT 'degraded' AS bucket, count(*) AS n FROM resumes "
    "WHERE job_id = $1 AND withdrawn_at IS NULL AND status = 'parsed' "
    "AND COALESCE((parsed->>'degraded')::bool, false) IS TRUE"
)


async def withdraw_resume(
    conn: DbConn,
    resume_id: UUID,
    *,
    reason: str | None,
    actor_kind: str,
    actor_user_id: UUID | None,
    actor_service: str | None,
) -> bool:
    """Exclude a résumé from ranking without deleting it (ADR-026 decision 2).

    Idempotent: the guarded UPDATE (``WHERE withdrawn_at IS NULL``) applies to
    at most one row. When it applies, an ``audit_log`` row and a
    ``resume.withdrawn`` outbox event (the un-project signal) are written in
    the SAME transaction (atomic). An already-withdrawn résumé matches zero
    rows and is a quiet no-op success — no new audit/outbox row, no error.

    Raises ``NotFoundError`` for a résumé id that does not exist at all.
    Returns whether the withdrawal actually applied.
    """
    exists = await conn.fetchval(_RESUME_EXISTS_SQL, resume_id)
    if exists is None:
        raise NotFoundError(f"resume {resume_id} not found", resume_id=str(resume_id))

    async with conn.transaction():
        result = await conn.execute(_WITHDRAW_SQL, resume_id, reason)
        if not result.endswith(" 1"):
            return False
        await audit_service.record_audit(
            conn,
            actor_kind=actor_kind,
            actor_user_id=actor_user_id,
            actor_service=actor_service,
            action="withdraw_resume",
            subject_type="resume",
            subject_id=resume_id,
            details={"reason": reason} if reason else None,
        )
        await outbox_service.enqueue_outbox(
            conn,
            aggregate="resume",
            aggregate_id=resume_id,
            event_type="resume.withdrawn",
            payload={},
        )
    return True


async def reinstate_resume(
    conn: DbConn,
    resume_id: UUID,
    *,
    actor_kind: str,
    actor_user_id: UUID | None,
    actor_service: str | None,
) -> bool:
    """Re-enter a withdrawn résumé into the ranking pool (ADR-026 decision 1).

    REPLAYS the last delivered ``resume.parsed`` payload — it never re-embeds
    (no ``llm``/``embedder`` parameter, mirroring ``project_resume``'s ADR-008
    guarantee). The existing drainer branch re-projects that byte-identical
    payload, restoring identical recall. Idempotent via the guarded UPDATE
    (``WHERE withdrawn_at IS NOT NULL``): a non-withdrawn résumé is a quiet
    no-op. A résumé withdrawn before it was ever parsed has no payload to
    replay, so the columns are cleared and the reinstatement is audited, but
    no ``resume.parsed`` event is enqueued.

    Raises ``NotFoundError`` for a résumé id that does not exist at all.
    Returns whether the reinstatement actually applied.
    """
    exists = await conn.fetchval(_RESUME_EXISTS_SQL, resume_id)
    if exists is None:
        raise NotFoundError(f"resume {resume_id} not found", resume_id=str(resume_id))

    async with conn.transaction():
        result = await conn.execute(_REINSTATE_SQL, resume_id)
        if not result.endswith(" 1"):
            return False
        await audit_service.record_audit(
            conn,
            actor_kind=actor_kind,
            actor_user_id=actor_user_id,
            actor_service=actor_service,
            action="reinstate_resume",
            subject_type="resume",
            subject_id=resume_id,
        )
        raw_payload = await conn.fetchval(_LAST_PARSED_PAYLOAD_SQL, resume_id)
        if raw_payload is not None:
            payload = (
                json.loads(raw_payload) if isinstance(raw_payload, str) else raw_payload
            )
            await outbox_service.enqueue_outbox(
                conn,
                aggregate="resume",
                aggregate_id=resume_id,
                event_type="resume.parsed",
                payload=payload,
            )
    return True


async def status_breakdown(conn: DbConn, job_id: UUID) -> ResumeStatusBreakdown:
    """Per-job résumé status counts (ADR-026 decision 5).

    Five mutually-exclusive buckets: a withdrawn résumé counts as ``withdrawn``
    regardless of its parse ``status`` (the CASE puts it in that bucket), so
    the five counts partition the job's résumés. Absent buckets zero-fill, so
    a job with no résumés still yields the full five-bucket shape.

    FU-7 §4 / ADR-030: ``degraded`` is a SUB-count of ``parsed`` (degraded ⊆
    parsed), carried as a SIXTH ``'degraded'`` bucket row UNIONed into the same
    query so ``parsed`` keeps counting ALL parsed rows. A parsed résumé whose
    skills extraction fell back to the keyword scan is counted in BOTH
    ``parsed`` and ``degraded``.
    """
    rows = await conn.fetch(_STATUS_BREAKDOWN_SQL, job_id)
    counts: dict[str, int] = {
        "uploaded": 0,
        "parsing": 0,
        "parsed": 0,
        "failed": 0,
        "withdrawn": 0,
        # FU-7 §4 / ADR-030: SUB-count of parsed, carried as a 6th bucket row
        # in the same query — parsed above stays the FULL parsed count.
        "degraded": 0,
    }
    for row in rows:
        bucket = row["bucket"]
        if bucket in counts:
            counts[bucket] = row["n"]
    return ResumeStatusBreakdown(**counts)
