"""Unit tests for Phase 6's résumé upload service surface —
``src.services.resume_service.detect_mime`` / ``upload_resumes`` (single AND
multi-file, one optional pasted-or-file cover letter). All I/O (BlobStore,
Postgres, pgcrypto) is mocked.

Neither function exists yet. ``src/services/resume_service.py`` currently
ships only the worker write-back trio + the Phase 5 read pair — see its
module docstring, which explicitly defers upload/validation to Phase 6.
Every test below fails at collection or the first ``await``. RED half of the
TDD cycle.

**Ambiguities this file locks:**

* ``detect_mime(filename: str, data: bytes) -> str`` returns one of
  ``src.pipeline.parsing.{MIME_PDF,MIME_DOCX,MIME_RTF,MIME_TXT}``, sniffed
  from MAGIC BYTES (not trusted from the filename extension alone) and cross-
  checked against the extension — a mismatch (a ``.pdf``-named file whose
  bytes are actually a DOCX zip) is rejected via
  ``src.pipeline.parsing.UnsupportedMimeError`` (the SAME exception type
  ``extract_text`` already raises, so callers have one type to catch).
* ``upload_resumes(conn, blob_store, *, job_id, files, consent_acknowledged,
  uploaded_by=None, cover_letter_file=None, cover_letter_text=None) ->
  list[ResumeUploadResult]``:
  - ``consent_acknowledged=False`` raises ``ValueError`` IMMEDIATELY, before
    touching the blob store or the connection at all (a single flag for the
    whole upload batch, not per file).
  - Per-file oversize (> the shared 10 MB ``extract.py`` cap) / empty (0
    bytes) input yields a ``ResumeUploadResult(outcome="rejected", ...)`` row
    for THAT file — it does not abort the whole batch.
  - sha256 dedup: BEFORE calling ``blob_store.put``, the function checks
    whether ``(job_id, sha256)`` already exists (the DDL's own UNIQUE
    constraint) and short-circuits to
    ``ResumeUploadResult(outcome="duplicate", ...)`` — ``blob_store.put`` is
    never called for a duplicate.
  - The blob key handed to ``blob_store.put`` is ALWAYS server-generated
    (``f"resumes/{uuid4()}.{ext}"``-shaped) — never derived from
    ``original_filename`` or (for the zip case, tested separately) a zip
    entry name.
  - A pasted ``cover_letter_text`` is encrypted via
    ``pii_service.set_pii_key`` + ``pii_service.encrypt`` in the SAME
    ``conn.transaction()`` block as the résumé row insert(s) — never written
    cleartext, and a failure from ``encrypt`` (e.g. the strict
    ``current_setting('app.pii_key')`` GUC raising on an empty/unset key)
    propagates uncaught (fails loud), it is not swallowed into a "rejected"
    row.
  - **Backend-robustness fix (diagnosed live):** a ``job_id`` with no
    matching ``jobs`` row raises ``src.errors.NotFoundError`` — BEFORE any
    blob write or résumé INSERT is attempted — rather than letting a real
    ``asyncpg.exceptions.ForeignKeyViolationError`` from the INSERT escape
    uncaught as an unhandled 500 at the route layer. The recommended fix is
    a pre-INSERT existence check (``SELECT ... FROM jobs WHERE id = $1``,
    mirroring every other job-scoped read in this codebase — see
    ``job_service.get_job``/``resumes.py``'s own ``_EXISTS_SQL`` pattern),
    not a catch of the FK violation itself: it is the same behaviour with no
    reliance on the database driver's exception type or message text. This
    file's ``_mock_conn`` answers that check via ``job_exists`` — any
    ``fetchval`` query naming ``jobs`` returns a truthy id when
    ``job_exists=True`` (the default, so every OTHER test in this file, all
    written before this fix, is unaffected) and ``None`` when
    ``job_exists=False``. The observable contract (404 + no side effects) is
    also pinned end-to-end against a REAL Postgres in
    ``tests/integration/test_api_resumes_pg.py`` — only a real FK violation
    proves the 500 this fix removes; this file cannot reproduce it because
    ``conn.execute`` here is a bare mock that never raises.
"""

from __future__ import annotations

import hashlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from src.errors import NotFoundError
from src.pipeline.parsing import MIME_DOCX, MIME_PDF, MIME_RTF, MIME_TXT
from src.schemas.resumes import ResumeUploadResult

_PDF_MAGIC = b"%PDF-1.4\n%mock pdf content\n" + b"x" * 200
_DOCX_MAGIC = b"PK\x03\x04" + b"x" * 200
_RTF_MAGIC = rb"{\rtf1\ansi " + b"x" * 200
_TXT_CONTENT = b"Plain text resume content. " * 20

_MAX_UPLOAD_BYTES = 10 * 1024 * 1024


def _acm(return_value: Any = None) -> MagicMock:
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=return_value)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _mock_conn(
    *, existing_sha: str | None = None, job_exists: bool = True
) -> MagicMock:
    """``fetchval`` answers TWO independent pre-checks, distinguished by SQL
    text, so a caller can flip either one without disturbing the other:

    * a query naming ``jobs`` (the job-existence pre-check this file's
      newest tests pin) -> a truthy id when ``job_exists`` (the default —
      preserves every test written before that check existed), else
      ``None``.
    * the sha-dedup pre-check (``SELECT id FROM resumes WHERE job_id = $1
      AND sha256 = $2`` — note: no literal ``"jobs"`` substring, so it can
      never collide with the branch above) -> a résumé id when
      ``existing_sha`` matches the sha being checked, else ``None``.
    """
    conn = MagicMock(name="conn")

    async def _fetchval(query: str, *args: Any) -> Any:
        if "jobs" in query.lower():
            return uuid4() if job_exists else None
        if existing_sha is not None and existing_sha in args:
            return uuid4()
        return None

    conn.fetchval = AsyncMock(side_effect=_fetchval)
    conn.fetchrow = AsyncMock(return_value=None)
    conn.execute = AsyncMock(return_value="INSERT 0 1")
    conn.transaction = MagicMock(return_value=_acm())
    return conn


def _mock_blob_store() -> MagicMock:
    store = MagicMock(name="blob_store")
    store.put = AsyncMock(return_value=None)
    return store


# ── detect_mime ──────────────────────────────────────────────────────────


def test_detect_mime_recognises_pdf_from_magic_bytes() -> None:
    from src.services.resume_service import detect_mime

    assert detect_mime("resume.pdf", _PDF_MAGIC) == MIME_PDF


def test_detect_mime_recognises_docx_from_magic_bytes() -> None:
    from src.services.resume_service import detect_mime

    assert detect_mime("resume.docx", _DOCX_MAGIC) == MIME_DOCX


def test_detect_mime_recognises_rtf_from_magic_bytes() -> None:
    from src.services.resume_service import detect_mime

    assert detect_mime("resume.rtf", _RTF_MAGIC) == MIME_RTF


def test_detect_mime_recognises_plain_text() -> None:
    from src.services.resume_service import detect_mime

    assert detect_mime("resume.txt", _TXT_CONTENT) == MIME_TXT


def test_detect_mime_rejects_extension_content_mismatch() -> None:
    """A .pdf-named file whose bytes are actually a DOCX zip must be
    rejected, not silently trusted off the extension."""
    from src.pipeline.parsing import UnsupportedMimeError
    from src.services.resume_service import detect_mime

    with pytest.raises(UnsupportedMimeError):
        detect_mime("resume.pdf", _DOCX_MAGIC)


def test_detect_mime_rejects_an_unsupported_extension() -> None:
    from src.pipeline.parsing import UnsupportedMimeError
    from src.services.resume_service import detect_mime

    with pytest.raises(UnsupportedMimeError):
        detect_mime("resume.exe", b"MZ" + b"x" * 200)


# ── upload_resumes: consent gate ────────────────────────────────────────


@pytest.mark.asyncio
async def test_upload_resumes_rejects_before_any_io_when_consent_false() -> None:
    from src.services.resume_service import upload_resumes

    conn = _mock_conn()
    store = _mock_blob_store()
    with pytest.raises(ValueError, match="(?i)consent"):
        await upload_resumes(
            conn,
            store,
            job_id=uuid4(),
            files=[("resume.pdf", _PDF_MAGIC)],
            consent_acknowledged=False,
        )
    store.put.assert_not_called()
    conn.execute.assert_not_called()


# ── upload_resumes: per-file size validation ────────────────────────────


@pytest.mark.asyncio
async def test_upload_resumes_rejects_an_oversize_file() -> None:
    from src.services.resume_service import upload_resumes

    conn = _mock_conn()
    store = _mock_blob_store()
    oversize = b"%PDF-1.4\n" + b"x" * (_MAX_UPLOAD_BYTES + 1)
    results = await upload_resumes(
        conn,
        store,
        job_id=uuid4(),
        files=[("huge.pdf", oversize)],
        consent_acknowledged=True,
    )
    assert results[0].outcome == "rejected"
    store.put.assert_not_called()


@pytest.mark.asyncio
async def test_upload_resumes_rejects_an_empty_file() -> None:
    from src.services.resume_service import upload_resumes

    conn = _mock_conn()
    store = _mock_blob_store()
    results = await upload_resumes(
        conn,
        store,
        job_id=uuid4(),
        files=[("empty.pdf", b"")],
        consent_acknowledged=True,
    )
    assert results[0].outcome == "rejected"
    store.put.assert_not_called()


# ── upload_resumes: sha dedup ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upload_resumes_dedupes_on_existing_job_sha256() -> None:
    from src.services.resume_service import upload_resumes

    sha = hashlib.sha256(_PDF_MAGIC).hexdigest()
    conn = _mock_conn(existing_sha=sha)
    store = _mock_blob_store()
    results = await upload_resumes(
        conn,
        store,
        job_id=uuid4(),
        files=[("resume.pdf", _PDF_MAGIC)],
        consent_acknowledged=True,
    )
    assert results[0].outcome == "duplicate"
    store.put.assert_not_called()


@pytest.mark.asyncio
async def test_upload_resumes_non_duplicate_file_calls_blob_store_put() -> None:
    from src.services.resume_service import upload_resumes

    conn = _mock_conn(existing_sha=None)
    store = _mock_blob_store()
    results = await upload_resumes(
        conn,
        store,
        job_id=uuid4(),
        files=[("resume.pdf", _PDF_MAGIC)],
        consent_acknowledged=True,
    )
    assert results[0].outcome == "accepted"
    store.put.assert_awaited_once()


# ── upload_resumes: cover-letter FILE (stored as a blob) ────────────────


def _insert_call_args(conn: MagicMock) -> tuple[object, ...]:
    """The positional args of the résumé INSERT execute() call."""
    for call in conn.execute.await_args_list:
        if call.args and "INSERT INTO resumes" in str(call.args[0]):
            return call.args
    raise AssertionError("no résumé INSERT execute() call was made")


@pytest.mark.asyncio
async def test_upload_resumes_cover_letter_file_stored_as_blob_and_keyed_on_row() -> (
    None
):
    from src.services.resume_service import upload_resumes

    conn = _mock_conn()
    store = _mock_blob_store()
    await upload_resumes(
        conn,
        store,
        job_id=uuid4(),
        files=[("resume.pdf", _PDF_MAGIC)],
        consent_acknowledged=True,
        cover_letter_file=("cover.pdf", _PDF_MAGIC),
    )
    # A server-generated cover-letter blob key (never derived from the filename)
    # was stored...
    put_keys = [c.args[0] for c in store.put.await_args_list]
    cover_keys = [k for k in put_keys if k.startswith("cover_letters/")]
    assert len(cover_keys) == 1
    assert cover_keys[0].endswith(".pdf")
    # ...and it is the last positional arg of the résumé INSERT (cover_letter_blob_key).
    assert _insert_call_args(conn)[-1] == cover_keys[0]


@pytest.mark.asyncio
async def test_upload_resumes_no_cover_letter_file_leaves_blob_key_null() -> None:
    from src.services.resume_service import upload_resumes

    conn = _mock_conn()
    store = _mock_blob_store()
    await upload_resumes(
        conn,
        store,
        job_id=uuid4(),
        files=[("resume.pdf", _PDF_MAGIC)],
        consent_acknowledged=True,
    )
    assert _insert_call_args(conn)[-1] is None
    assert not any(
        c.args[0].startswith("cover_letters/") for c in store.put.await_args_list
    )


@pytest.mark.asyncio
async def test_upload_resumes_unsupported_cover_letter_file_raises() -> None:
    from src.services.resume_service import upload_resumes

    conn = _mock_conn()
    store = _mock_blob_store()
    with pytest.raises(ValueError, match="cover letter"):
        await upload_resumes(
            conn,
            store,
            job_id=uuid4(),
            files=[("resume.pdf", _PDF_MAGIC)],
            consent_acknowledged=True,
            cover_letter_file=("cover.xyz", b"not a known file type at all"),
        )


# ── upload_resumes: server-generated blob key ───────────────────────────


@pytest.mark.asyncio
async def test_upload_resumes_blob_key_is_never_derived_from_the_filename() -> None:
    from src.services.resume_service import upload_resumes

    conn = _mock_conn()
    store = _mock_blob_store()
    await upload_resumes(
        conn,
        store,
        job_id=uuid4(),
        files=[("Jane_Doe_Confidential_Resume.pdf", _PDF_MAGIC)],
        consent_acknowledged=True,
    )
    key = store.put.await_args.args[0]
    assert "jane" not in key.lower()
    assert "doe" not in key.lower()
    assert "confidential" not in key.lower()
    assert key.endswith(".pdf")


@pytest.mark.asyncio
async def test_upload_resumes_stores_the_original_filename_in_the_result_row() -> None:
    """The crafted/PII-bearing filename IS retained as metadata (the DDL
    column) — only the BLOB KEY is server-generated, not the filename field
    the DB stores it under."""
    from src.services.resume_service import upload_resumes

    conn = _mock_conn()
    store = _mock_blob_store()
    results = await upload_resumes(
        conn,
        store,
        job_id=uuid4(),
        files=[("Jane_Doe_Resume.pdf", _PDF_MAGIC)],
        consent_acknowledged=True,
    )
    assert isinstance(results[0], ResumeUploadResult)
    assert results[0].original_filename == "Jane_Doe_Resume.pdf"


# ── upload_resumes: multi-file ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_upload_resumes_accepts_multiple_files_in_one_call() -> None:
    from src.services.resume_service import upload_resumes

    conn = _mock_conn()
    store = _mock_blob_store()
    results = await upload_resumes(
        conn,
        store,
        job_id=uuid4(),
        files=[
            ("a.pdf", _PDF_MAGIC),
            ("b.docx", _DOCX_MAGIC),
            ("c.txt", _TXT_CONTENT),
        ],
        consent_acknowledged=True,
    )
    assert len(results) == 3
    assert store.put.await_count == 3


# ── upload_resumes: pasted cover-letter encryption ──────────────────────


@pytest.mark.asyncio
async def test_upload_resumes_encrypts_pasted_cover_letter_same_tx() -> None:
    from src.services import pii as pii_service
    from src.services.resume_service import upload_resumes

    conn = _mock_conn()
    store = _mock_blob_store()
    with (pytest.MonkeyPatch().context() as mp,):
        set_key = AsyncMock()
        encrypt = AsyncMock(return_value=b"ciphertext")
        mp.setattr(pii_service, "set_pii_key", set_key)
        mp.setattr(pii_service, "encrypt", encrypt)
        await upload_resumes(
            conn,
            store,
            job_id=uuid4(),
            files=[("resume.pdf", _PDF_MAGIC)],
            consent_acknowledged=True,
            cover_letter_text="Dear hiring manager, I am excited to apply.",
        )
    set_key.assert_awaited()
    encrypt.assert_awaited()
    # The transaction context manager must have been entered — encryption
    # happens inside the same atomic write as the résumé row(s).
    conn.transaction.assert_called()


@pytest.mark.asyncio
async def test_upload_resumes_pasted_cover_letter_never_written_cleartext() -> None:
    """The plaintext cover-letter string must never appear as a raw SQL
    argument to ``conn.execute`` — only the ciphertext ``encrypt`` returns."""
    from src.services import pii as pii_service
    from src.services.resume_service import upload_resumes

    conn = _mock_conn()
    store = _mock_blob_store()
    plaintext = "Dear hiring manager, this is my very distinctive cover letter body."
    with pytest.MonkeyPatch().context() as mp:
        mp.setattr(pii_service, "set_pii_key", AsyncMock())
        mp.setattr(pii_service, "encrypt", AsyncMock(return_value=b"ciphertext-bytes"))
        await upload_resumes(
            conn,
            store,
            job_id=uuid4(),
            files=[("resume.pdf", _PDF_MAGIC)],
            consent_acknowledged=True,
            cover_letter_text=plaintext,
        )
    for call in conn.execute.await_args_list:
        for arg in call.args:
            if isinstance(arg, str):
                assert plaintext not in arg


@pytest.mark.asyncio
async def test_upload_resumes_empty_pii_key_fails_loud_on_pasted_cover_letter() -> None:
    """A strict-GUC failure from ``pii_service.encrypt`` (empty/unset
    PII_KEY) must propagate uncaught — the upload must NOT silently swallow
    it into a "rejected" row or drop the cover letter to NULL."""
    from src.services import pii as pii_service
    from src.services.resume_service import upload_resumes

    conn = _mock_conn()
    store = _mock_blob_store()

    class _FakePostgresError(Exception):
        pass

    with pytest.MonkeyPatch().context() as mp:
        mp.setattr(pii_service, "set_pii_key", AsyncMock())
        mp.setattr(
            pii_service,
            "encrypt",
            AsyncMock(
                side_effect=_FakePostgresError(
                    'unrecognized configuration parameter "app.pii_key"'
                )
            ),
        )
        with pytest.raises(_FakePostgresError):
            await upload_resumes(
                conn,
                store,
                job_id=uuid4(),
                files=[("resume.pdf", _PDF_MAGIC)],
                consent_acknowledged=True,
                cover_letter_text="some cover letter text",
            )


@pytest.mark.asyncio
async def test_upload_resumes_without_a_cover_letter_never_calls_encrypt() -> None:
    from src.services import pii as pii_service
    from src.services.resume_service import upload_resumes

    conn = _mock_conn()
    store = _mock_blob_store()
    with pytest.MonkeyPatch().context() as mp:
        encrypt = AsyncMock(return_value=b"unused")
        mp.setattr(pii_service, "encrypt", encrypt)
        await upload_resumes(
            conn,
            store,
            job_id=uuid4(),
            files=[("resume.pdf", _PDF_MAGIC)],
            consent_acknowledged=True,
        )
    encrypt.assert_not_awaited()


# ── upload_resumes: per-résumé cover_letter_map (FU-3 Slice 2) ───────────


def _all_insert_call_args(conn: MagicMock) -> list[tuple[object, ...]]:
    """The positional args of EVERY résumé INSERT execute() call, in order."""
    return [
        call.args
        for call in conn.execute.await_args_list
        if call.args and "INSERT INTO resumes" in str(call.args[0])
    ]


@pytest.mark.asyncio
async def test_upload_resumes_cover_map_stores_a_distinct_blob_key_per_resume() -> None:
    """Each résumé in the map gets its OWN ``cover_letters/<uuid>`` blob key on
    ITS row — never one key shared across the batch."""
    from src.services.resume_service import upload_resumes

    conn = _mock_conn()
    store = _mock_blob_store()
    await upload_resumes(
        conn,
        store,
        job_id=uuid4(),
        files=[("jane_resume.pdf", _PDF_MAGIC), ("bob_resume.pdf", _PDF_MAGIC)],
        consent_acknowledged=True,
        cover_letter_map={
            "jane_resume.pdf": ("jane_cover_letter.pdf", _PDF_MAGIC),
            "bob_resume.pdf": ("bob_cover_letter.pdf", _PDF_MAGIC),
        },
    )
    inserts = _all_insert_call_args(conn)
    assert len(inserts) == 2
    cover_keys = [ins[-1] for ins in inserts]
    assert all(k is not None and k.startswith("cover_letters/") for k in cover_keys)
    # DISTINCT — not one shared key.
    assert cover_keys[0] != cover_keys[1]
    stored_cover_keys = [
        c.args[0]
        for c in store.put.await_args_list
        if c.args[0].startswith("cover_letters/")
    ]
    assert sorted(stored_cover_keys) == sorted(cover_keys)


@pytest.mark.asyncio
async def test_upload_resumes_cover_map_only_keys_the_matching_resume() -> None:
    """A résumé absent from the map carries no cover blob key."""
    from src.services.resume_service import upload_resumes

    conn = _mock_conn()
    store = _mock_blob_store()
    results = await upload_resumes(
        conn,
        store,
        job_id=uuid4(),
        files=[("jane_resume.pdf", _PDF_MAGIC), ("bob_resume.pdf", _PDF_MAGIC)],
        consent_acknowledged=True,
        cover_letter_map={"jane_resume.pdf": ("jane_cover_letter.pdf", _PDF_MAGIC)},
    )
    by_name = {r.original_filename: r for r in results}
    assert by_name["jane_resume.pdf"].cover_letter_filename == "jane_cover_letter.pdf"
    assert by_name["bob_resume.pdf"].cover_letter_filename is None
    # ins = (SQL, resume_id, job_id, blob_key, filename, ...) — key by filename.
    inserts = {ins[4]: ins for ins in _all_insert_call_args(conn)}
    assert inserts["jane_resume.pdf"][-1].startswith("cover_letters/")
    assert inserts["bob_resume.pdf"][-1] is None


@pytest.mark.asyncio
async def test_upload_resumes_populates_warnings_from_warnings_map() -> None:
    from src.services.resume_service import upload_resumes

    conn = _mock_conn()
    store = _mock_blob_store()
    results = await upload_resumes(
        conn,
        store,
        job_id=uuid4(),
        files=[("stray_cover.pdf", _PDF_MAGIC)],
        consent_acknowledged=True,
        warnings_map={"stray_cover.pdf": ["ingested as a résumé"]},
    )
    assert results[0].warnings == ["ingested as a résumé"]


@pytest.mark.asyncio
async def test_upload_resumes_no_map_falls_back_to_singular_cover() -> None:
    """With no map, the singular ``cover_letter_file`` still stores one shared
    cover blob keyed on the (single) résumé row — today's behaviour."""
    from src.services.resume_service import upload_resumes

    conn = _mock_conn()
    store = _mock_blob_store()
    await upload_resumes(
        conn,
        store,
        job_id=uuid4(),
        files=[("resume.pdf", _PDF_MAGIC)],
        consent_acknowledged=True,
        cover_letter_file=("cover.pdf", _PDF_MAGIC),
    )
    assert _insert_call_args(conn)[-1].startswith("cover_letters/")


# ── upload_resumes: nonexistent job (backend-robustness fix) ────────────


@pytest.mark.asyncio
async def test_upload_resumes_raises_not_found_when_job_does_not_exist() -> None:
    """A ``job_id`` with no matching ``jobs`` row must be rejected with a
    typed ``NotFoundError`` BEFORE any blob or row write is attempted — never
    left to surface as a raw ``ForeignKeyViolationError`` from the INSERT."""
    from src.services.resume_service import upload_resumes

    conn = _mock_conn(job_exists=False)
    store = _mock_blob_store()
    with pytest.raises(NotFoundError):
        await upload_resumes(
            conn,
            store,
            job_id=uuid4(),
            files=[("resume.pdf", _PDF_MAGIC)],
            consent_acknowledged=True,
        )
    store.put.assert_not_called()
    conn.execute.assert_not_called()


@pytest.mark.asyncio
async def test_upload_resumes_missing_job_wins_over_per_file_rejections() -> None:
    """The missing-job check is a whole-request precondition, not a per-file
    one: even a file that would otherwise be rejected (empty) must still
    surface as ``NotFoundError`` — there is no per-file accounting to build
    when the job itself doesn't exist."""
    from src.services.resume_service import upload_resumes

    conn = _mock_conn(job_exists=False)
    store = _mock_blob_store()
    with pytest.raises(NotFoundError):
        await upload_resumes(
            conn,
            store,
            job_id=uuid4(),
            files=[("empty.pdf", b"")],
            consent_acknowledged=True,
        )
    store.put.assert_not_called()


@pytest.mark.asyncio
async def test_upload_resumes_consent_false_wins_over_missing_job() -> None:
    """Consent is checked FIRST, before any I/O at all (the pre-existing
    contract) — a missing job must not short-circuit past the consent gate,
    and the consent ``ValueError`` message must still be the one surfaced."""
    from src.services.resume_service import upload_resumes

    conn = _mock_conn(job_exists=False)
    store = _mock_blob_store()
    with pytest.raises(ValueError, match="(?i)consent"):
        await upload_resumes(
            conn,
            store,
            job_id=uuid4(),
            files=[("resume.pdf", _PDF_MAGIC)],
            consent_acknowledged=False,
        )
    store.put.assert_not_called()
    conn.execute.assert_not_called()


@pytest.mark.asyncio
async def test_upload_resumes_existing_job_is_unaffected() -> None:
    """Sanity/regression guard: a real job (``job_exists=True``, the default)
    uploads exactly as before — guards against the fix being implemented
    backwards (e.g. inverting the existence check)."""
    from src.services.resume_service import upload_resumes

    conn = _mock_conn()  # job_exists=True by default
    store = _mock_blob_store()
    results = await upload_resumes(
        conn,
        store,
        job_id=uuid4(),
        files=[("resume.pdf", _PDF_MAGIC)],
        consent_acknowledged=True,
    )
    assert results[0].outcome == "accepted"
    store.put.assert_awaited_once()
