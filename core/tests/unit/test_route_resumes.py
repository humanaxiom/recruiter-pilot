"""Route-level tests for Phase 6's résumé routes —
``src.api.routes.resumes`` (new module; does not exist yet). The whole file
fails at collection (``ModuleNotFoundError``) — RED half of the TDD cycle.

Reverse-match subresource routes (``POST /resumes/{id}/match-jobs`` /
``GET /resumes/{id}/match-results``) are exercised separately in
``test_route_reverse_match.py`` — this file covers upload + list + get only.
The audited ``POST /resumes/{id}/reveal`` is exercised in
``test_route_reveal.py``.

**Ambiguities this file locks:**

* ``src.api.routes.resumes`` exposes ``router: APIRouter`` (absolute paths,
  no router-level prefix): ``POST /jobs/{job_id}/resumes`` (upload),
  ``GET /jobs/{job_id}/resumes`` (list), ``GET /resumes/{resume_id}``
  (get one — **no ``reveal`` query param at all as of FU-4/D3; see below**).
* Upload is ``multipart/form-data``: a REPEATED ``files`` field (one or more
  résumé files, individually OR one entry ending ``.zip`` which is expanded
  via ``src.services.zip_upload.expand_zip_entries`` and merged into the
  same accepted/rejected accounting), a ``consent_acknowledged`` form field
  (``"true"``/``"false"``), and an optional ``cover_letter_text`` form
  field (pasted paste-only cover letter — no file-cover-letter case tested
  here). Returns **202** with a JSON array of ``ResumeUploadResult`` rows and
  enqueues ``arq.enqueue_job("parse_resume", str(resume_id))`` ONCE PER
  ACCEPTED résumé (duplicates/rejections are never enqueued).
* **Locked ID-generation design** (resolves what would otherwise be an
  underspecified DB-round-trip ambiguity): ``resume_id`` is minted by the
  SERVICE layer itself (``uuid4()``) BEFORE any I/O — the same value backs
  both the inserted row's primary key and the server-generated blob key
  stem (``resumes/{resume_id}.{ext}``), so the route can enqueue the correct
  id without a second DB round trip.
* A zip containing a path-traversal or over-cap entry is rejected for the
  WHOLE request with a 4xx (``src.services.zip_upload.ZipRejected``
  surfaced through the same ``AppError``-style handler, or a plain
  ``HTTPException`` — either way, status in ``{400, 422}``) — nothing at all
  is enqueued for that request, not even the valid entries alongside it.
* ``GET /resumes/{resume_id}`` — 404 (``NotFoundError`` -> the ``AppError``
  handler) when the id doesn't resolve. **The redaction-boundary byte-scan
  (ADR-006 §4, at the HTTP layer)**: under a BLIND job, none of the
  candidate's real name/email/phone literal byte-sequences appear ANYWHERE
  in the raw response body (not just absent from specific fields); under a
  NON-blind job, at least the email is present verbatim — proving the route
  actually goes through ``resume_service.get_one``'s redaction, not a
  raw re-query.

**FU-4/D3 — the ``reveal`` query param is REMOVED from this route entirely**
(ADR-016's own decision: "reveal is an explicit, POST-only, audited action").
The route now hardcodes ``reveal=False`` when calling
``resume_service.get_one`` — a caller sending ``?reveal=true`` gets FastAPI's
default "extra query params are silently ignored" behaviour, NOT an
un-blinded response. This is asserted below on the ACTUAL SERIALIZED
response bytes (the byte-scan style already established in this file for the
default case), not merely on a field being ``None`` — a weaker assertion
would pass even if the route accidentally still read the param and dropped
it from the JSON shape while leaking it into an unrelated field.

**FU-4 (RBAC) — route→role table for this file:**

| Route                          | Method | Allowed roles |
|----------------------------------|--------|-----------------|
| ``/jobs/{id}/resumes`` (upload)  | POST   | admin, recruiter |
| ``/jobs/{id}/resumes`` (list)    | GET    | all four |
| ``/resumes/{id}``                | GET    | all four (blind only) |
"""

from __future__ import annotations

import datetime as dt
import json
import zipfile
from collections.abc import AsyncIterator
from io import BytesIO
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

from src.api.deps import Role, get_arq, resolve_role, resolve_user
from src.api.routes import resumes as resumes_routes
from src.errors import AppError, NotFoundError
from src.models.pool import get_db
from src.schemas.auth import User
from src.schemas.resumes import CandidateInfo, ResumeOut
from src.storage.blob_store import get_blob_store

_NOW = dt.datetime(2026, 7, 16, tzinfo=dt.UTC)
_PDF_MAGIC = b"%PDF-1.4\nresume content\n" + b"x" * 500

_NAME = "Zzyzxqrst Wibblesworth"
_EMAIL = "zzyzxqrst.wibblesworth@example.test"
_PHONE = "604-555-0192"

_NON_RESUME_WRITER_ROLES: tuple[Role, ...] = (Role.HIRING_MANAGER, Role.AUDITOR)
_ALL_ROLES: tuple[Role, ...] = tuple(Role)


class _Row(dict[str, Any]):
    def __getitem__(self, key: str) -> Any:
        return dict.get(self, key)


def _full_parsed(*, name: str, email: str, phone: str) -> dict[str, Any]:
    return {
        "candidate": {"name": name, "email": email, "phone": phone, "location": None},
        "summary": f"{name} is a senior engineer reachable at {email} or {phone}.",
        "total_years_experience": 8,
        "skills": [],
        "experience": [],
        "education": [],
        "chunks": [
            {
                "id": "c_001",
                "section": "header",
                "page": 0,
                "text": f"{name} | {email} | {phone}",
            }
        ],
        "cover_letter_chunks": [],
    }


def _get_row(
    *,
    resume_id: UUID,
    job_id: UUID | None = None,
    parsed: dict[str, Any] | None = None,
    candidate_name: str | None = None,
    candidate_email: str | None = None,
    candidate_phone: str | None = None,
) -> _Row:
    return _Row(
        {
            "id": resume_id,
            "job_id": job_id or uuid4(),
            "original_filename": "resume.pdf",
            "mime_type": "application/pdf",
            "file_size_bytes": 12345,
            "sha256": "deadbeef",
            "c_name": candidate_name,
            "c_email": candidate_email,
            "c_phone": candidate_phone,
            "cl_text": None,
            "cover_letter_parsed": None,
            "candidate_email_hash": None,
            "parsed": json.dumps(parsed) if parsed is not None else None,
            "status": "parsed",
            "uploaded_by": None,
            "uploaded_at": _NOW,
            "parsed_at": _NOW,
            "failure_reason": None,
            "consent_acknowledged": True,
        }
    )


def _acm() -> MagicMock:
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=None)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _mock_conn(
    *,
    fetchrow: _Row | None = None,
    fetch: list[_Row] | None = None,
    fetchval: Any = None,
    job_exists: bool = True,
) -> MagicMock:
    """``fetchval`` answers the job-existence pre-check
    (``resume_service._JOB_EXISTS_SQL``, ``"SELECT id FROM jobs WHERE id =
    $1"`` EXACTLY) by ``job_exists`` — a truthy id by default, so every
    pre-existing caller of this fixture (all written before that check
    existed) is unaffected — while every OTHER ``fetchval`` query (the
    sha-dedup pre-check, the blind-review flag lookup, …) keeps answering
    the plain ``fetchval`` param unchanged. Matching on the FULL exact query
    string (not a ``"jobs" in query`` substring test) is deliberate: the
    blind-review lookups (``...FROM jobs j JOIN resumes r...``) also contain
    the substring ``"jobs"`` and must keep going through ``fetchval``, not
    this branch."""
    conn = MagicMock(name="conn")
    conn.fetchrow = AsyncMock(return_value=fetchrow)
    conn.fetch = AsyncMock(return_value=fetch or [])

    async def _fetchval(query: str, *args: Any) -> Any:
        if query.strip() == "SELECT id FROM jobs WHERE id = $1":
            return uuid4() if job_exists else None
        return fetchval

    conn.fetchval = AsyncMock(side_effect=_fetchval)
    conn.execute = AsyncMock(return_value="INSERT 0 1")
    conn.transaction = MagicMock(return_value=_acm())
    return conn


def _mock_blob_store() -> MagicMock:
    store = MagicMock(name="blob_store")
    store.put = AsyncMock(return_value=None)
    return store


def _build_app(
    conn: MagicMock,
    *,
    arq: MagicMock | None = None,
    store: MagicMock | None = None,
    role: Role = Role.ADMIN,
) -> FastAPI:
    app = FastAPI()
    app.include_router(resumes_routes.router)

    async def _get_db_override() -> AsyncIterator[Any]:
        yield conn

    app.dependency_overrides[get_db] = _get_db_override
    app.dependency_overrides[get_blob_store] = lambda: store or _mock_blob_store()
    app.dependency_overrides[get_arq] = lambda: arq or MagicMock(
        enqueue_job=AsyncMock()
    )
    app.dependency_overrides[resolve_role] = lambda: role

    @app.exception_handler(AppError)
    async def _app_error_handler(_request: Any, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status, content={"detail": exc.message, "code": exc.code}
        )

    return app


async def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _zip_bytes(entries: list[tuple[str, bytes]]) -> bytes:
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
        for name, content in entries:
            zf.writestr(zipfile.ZipInfo(filename=name), content)
    return buf.getvalue()


# ── upload: multi-file ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upload_resumes_multi_file_returns_202_and_enqueues_per_file() -> None:
    conn = _mock_conn()
    arq = MagicMock(enqueue_job=AsyncMock())
    store = _mock_blob_store()
    app = _build_app(conn, arq=arq, store=store)
    async with await _client(app) as client:
        resp = await client.post(
            f"/jobs/{uuid4()}/resumes",
            files=[
                ("files", ("a.pdf", _PDF_MAGIC, "application/pdf")),
                ("files", ("b.pdf", _PDF_MAGIC, "application/pdf")),
            ],
            data={"consent_acknowledged": "true"},
        )
    assert resp.status_code == 202
    body = resp.json()
    assert len(body) == 2
    assert arq.enqueue_job.await_count == 2
    for call in arq.enqueue_job.await_args_list:
        assert call.args[0] == "parse_resume"


@pytest.mark.asyncio
async def test_upload_resumes_cover_letter_file_is_read_and_stored() -> None:
    """The route reads the optional ``cover_letter_file`` part and forwards it;
    the service stores it under a server-generated ``cover_letters/`` blob key."""
    conn = _mock_conn()
    store = _mock_blob_store()
    app = _build_app(conn, store=store)
    async with await _client(app) as client:
        resp = await client.post(
            f"/jobs/{uuid4()}/resumes",
            files=[
                ("files", ("a.pdf", _PDF_MAGIC, "application/pdf")),
                ("cover_letter_file", ("cover.pdf", _PDF_MAGIC, "application/pdf")),
            ],
            data={"consent_acknowledged": "true"},
        )
    assert resp.status_code == 202
    cover_keys = [
        c.args[0]
        for c in store.put.await_args_list
        if c.args[0].startswith("cover_letters/")
    ]
    assert len(cover_keys) == 1


# ── upload: per-résumé cover pairing (FU-3 Slice 2) ─────────────────────


@pytest.mark.asyncio
async def test_upload_resumes_filename_pairing_attaches_cover_per_resume() -> None:
    """``jane_resume`` + ``jane_cover_letter`` + ``bob_resume`` in one request:
    two accepted résumé rows, only jane's carries ``cover_letter_filename``."""
    conn = _mock_conn()
    arq = MagicMock(enqueue_job=AsyncMock())
    store = _mock_blob_store()
    app = _build_app(conn, arq=arq, store=store)
    async with await _client(app) as client:
        resp = await client.post(
            f"/jobs/{uuid4()}/resumes",
            files=[
                ("files", ("jane_resume.pdf", _PDF_MAGIC, "application/pdf")),
                ("files", ("jane_cover_letter.pdf", _PDF_MAGIC, "application/pdf")),
                ("files", ("bob_resume.pdf", _PDF_MAGIC, "application/pdf")),
            ],
            data={"consent_acknowledged": "true"},
        )
    assert resp.status_code == 202
    body = resp.json()
    accepted = [r for r in body if r["outcome"] == "accepted"]
    assert len(accepted) == 2  # jane + bob résumés; the cover is NOT its own row
    by_name = {r["original_filename"]: r for r in accepted}
    assert (
        by_name["jane_resume.pdf"]["cover_letter_filename"] == "jane_cover_letter.pdf"
    )
    assert by_name["bob_resume.pdf"]["cover_letter_filename"] is None
    # One parse task per accepted résumé (the cover is not enqueued).
    assert arq.enqueue_job.await_count == 2


@pytest.mark.asyncio
async def test_upload_resumes_ambiguous_pairing_plus_singular_cover_is_422() -> None:
    """Per-résumé pairing AND a singular batch cover letter together is
    ambiguous — reject with 422 rather than silently picking one."""
    conn = _mock_conn()
    store = _mock_blob_store()
    app = _build_app(conn, store=store)
    async with await _client(app) as client:
        resp = await client.post(
            f"/jobs/{uuid4()}/resumes",
            files=[
                ("files", ("jane_resume.pdf", _PDF_MAGIC, "application/pdf")),
                ("files", ("jane_cover_letter.pdf", _PDF_MAGIC, "application/pdf")),
                ("cover_letter_file", ("batch.pdf", _PDF_MAGIC, "application/pdf")),
            ],
            data={"consent_acknowledged": "true"},
        )
    assert resp.status_code == 422
    assert "ambiguous" in resp.json()["detail"].lower()
    store.put.assert_not_called()


@pytest.mark.asyncio
async def test_upload_resumes_plain_upload_with_singular_cover_is_not_ambiguous() -> (
    None
):
    """A plain no-suffix upload pairs no per-résumé cover, so the singular
    ``cover_letter_file`` still works (backward compatible — no 422)."""
    conn = _mock_conn()
    store = _mock_blob_store()
    app = _build_app(conn, store=store)
    async with await _client(app) as client:
        resp = await client.post(
            f"/jobs/{uuid4()}/resumes",
            files=[
                ("files", ("a.pdf", _PDF_MAGIC, "application/pdf")),
                ("cover_letter_file", ("cover.pdf", _PDF_MAGIC, "application/pdf")),
            ],
            data={"consent_acknowledged": "true"},
        )
    assert resp.status_code == 202


# ── upload: manifest.json-driven pairing (FU-3 Slice 3) ─────────────────


@pytest.mark.asyncio
async def test_upload_resumes_manifest_field_pairs_by_name() -> None:
    """A ``pairing_manifest`` multipart field pairs ``jane_resume`` ↔
    ``jane_cover`` by name even without the ``_cover_letter`` convention
    suffix — manifest takes precedence over convention."""
    conn = _mock_conn()
    arq = MagicMock(enqueue_job=AsyncMock())
    store = _mock_blob_store()
    app = _build_app(conn, arq=arq, store=store)
    manifest = json.dumps(
        {
            "applicants": [
                {
                    "resume_file": "jane_resume.pdf",
                    "cover_letter_file": "jane_cover.pdf",
                    "cover_letter_flag": "Yes",
                }
            ]
        }
    ).encode("utf-8")
    async with await _client(app) as client:
        resp = await client.post(
            f"/jobs/{uuid4()}/resumes",
            files=[
                ("files", ("jane_resume.pdf", _PDF_MAGIC, "application/pdf")),
                ("files", ("jane_cover.pdf", _PDF_MAGIC, "application/pdf")),
                ("pairing_manifest", ("manifest.json", manifest, "application/json")),
            ],
            data={"consent_acknowledged": "true"},
        )
    assert resp.status_code == 202
    accepted = [r for r in resp.json() if r["outcome"] == "accepted"]
    assert len(accepted) == 1
    assert accepted[0]["original_filename"] == "jane_resume.pdf"
    assert accepted[0]["cover_letter_filename"] == "jane_cover.pdf"
    assert arq.enqueue_job.await_count == 1


@pytest.mark.asyncio
async def test_upload_resumes_manifest_in_zip_rejected_with_guidance() -> None:
    """A ``manifest.json`` zipped WITH the résumés hits the zip allowlist (no
    json) → 400 with clear guidance to upload it as its own field."""
    conn = _mock_conn()
    arq = MagicMock(enqueue_job=AsyncMock())
    store = _mock_blob_store()
    app = _build_app(conn, arq=arq, store=store)
    archive = _zip_bytes(
        [("jane_resume.pdf", _PDF_MAGIC), ("manifest.json", b'{"applicants": []}')]
    )
    async with await _client(app) as client:
        resp = await client.post(
            f"/jobs/{uuid4()}/resumes",
            files=[("files", ("batch.zip", archive, "application/zip"))],
            data={"consent_acknowledged": "true"},
        )
    assert resp.status_code == 400
    detail = resp.json()["detail"].lower()
    assert "own field" in detail
    arq.enqueue_job.assert_not_awaited()
    store.put.assert_not_called()


@pytest.mark.asyncio
async def test_upload_resumes_malformed_manifest_is_422() -> None:
    conn = _mock_conn()
    store = _mock_blob_store()
    app = _build_app(conn, store=store)
    async with await _client(app) as client:
        resp = await client.post(
            f"/jobs/{uuid4()}/resumes",
            files=[
                ("files", ("jane_resume.pdf", _PDF_MAGIC, "application/pdf")),
                (
                    "pairing_manifest",
                    ("manifest.json", b"{not json", "application/json"),
                ),
            ],
            data={"consent_acknowledged": "true"},
        )
    assert resp.status_code == 422
    store.put.assert_not_called()


# ── upload: file-count cap (SEC-2, memory-exhaustion DoS) ────────────────


@pytest.mark.asyncio
async def test_upload_resumes_over_max_count_rejected_nothing_enqueued() -> None:
    from src.services.zip_upload import _MAX_ZIP_ENTRIES

    conn = _mock_conn()
    arq = MagicMock(enqueue_job=AsyncMock())
    store = _mock_blob_store()
    app = _build_app(conn, arq=arq, store=store)
    files = [
        ("files", (f"r{i}.pdf", _PDF_MAGIC, "application/pdf"))
        for i in range(_MAX_ZIP_ENTRIES + 1)
    ]
    async with await _client(app) as client:
        resp = await client.post(
            f"/jobs/{uuid4()}/resumes",
            files=files,
            data={"consent_acknowledged": "true"},
        )
    assert resp.status_code in (413, 422)
    arq.enqueue_job.assert_not_awaited()
    store.put.assert_not_called()


@pytest.mark.asyncio
async def test_upload_resumes_over_max_count_rejected_before_bodies_processed(
    monkeypatch: Any,
) -> None:
    from src.services.zip_upload import _MAX_ZIP_ENTRIES

    conn = _mock_conn()
    arq = MagicMock(enqueue_job=AsyncMock())
    store = _mock_blob_store()
    app = _build_app(conn, arq=arq, store=store)
    # Spy the service: the batch must be rejected on count BEFORE any body is
    # read/expanded and BEFORE the service is ever invoked.
    spy = AsyncMock()
    monkeypatch.setattr(resumes_routes.resume_service, "upload_resumes", spy)
    files = [
        ("files", (f"r{i}.pdf", _PDF_MAGIC, "application/pdf"))
        for i in range(_MAX_ZIP_ENTRIES + 1)
    ]
    async with await _client(app) as client:
        resp = await client.post(
            f"/jobs/{uuid4()}/resumes",
            files=files,
            data={"consent_acknowledged": "true"},
        )
    assert resp.status_code in (413, 422)
    spy.assert_not_awaited()
    conn.execute.assert_not_called()


@pytest.mark.asyncio
async def test_upload_resumes_over_max_count_never_reads_any_body(
    monkeypatch: Any,
) -> None:
    """Regression guard (security re-audit LOW): pin that the >50-file cap is
    rejected BEFORE any file body is read into memory. A mutation that moves
    the count check to after the ``for f in files: await f.read()`` loop
    would still pass ``test_upload_resumes_over_max_count_rejected_*`` above
    (those only assert nothing was enqueued/persisted downstream) — this test
    spies directly on ``UploadFile.read`` and asserts it is never awaited on
    the over-cap path.
    """
    from starlette.datastructures import UploadFile as StarletteUploadFile

    from src.services.zip_upload import _MAX_ZIP_ENTRIES

    conn = _mock_conn()
    arq = MagicMock(enqueue_job=AsyncMock())
    store = _mock_blob_store()
    app = _build_app(conn, arq=arq, store=store)

    read_calls = 0
    original_read = StarletteUploadFile.read

    async def _spy_read(self: Any, *args: Any, **kwargs: Any) -> bytes:
        nonlocal read_calls
        read_calls += 1
        return await original_read(self, *args, **kwargs)  # type: ignore[no-any-return]

    monkeypatch.setattr(StarletteUploadFile, "read", _spy_read)

    files = [
        ("files", (f"r{i}.pdf", _PDF_MAGIC, "application/pdf"))
        for i in range(_MAX_ZIP_ENTRIES + 1)
    ]
    async with await _client(app) as client:
        resp = await client.post(
            f"/jobs/{uuid4()}/resumes",
            files=files,
            data={"consent_acknowledged": "true"},
        )
    assert resp.status_code in (413, 422)
    assert read_calls == 0, "over-cap batch must be rejected before any body is read"


@pytest.mark.asyncio
async def test_upload_resumes_at_max_count_is_accepted() -> None:
    from src.services.zip_upload import _MAX_ZIP_ENTRIES

    conn = _mock_conn()
    arq = MagicMock(enqueue_job=AsyncMock())
    store = _mock_blob_store()
    app = _build_app(conn, arq=arq, store=store)
    files = [
        ("files", (f"r{i}.pdf", _PDF_MAGIC, "application/pdf"))
        for i in range(_MAX_ZIP_ENTRIES)
    ]
    async with await _client(app) as client:
        resp = await client.post(
            f"/jobs/{uuid4()}/resumes",
            files=files,
            data={"consent_acknowledged": "true"},
        )
    assert resp.status_code == 202


# ── upload: zip expansion ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upload_resumes_zip_expands_and_enqueues_per_entry() -> None:
    conn = _mock_conn()
    arq = MagicMock(enqueue_job=AsyncMock())
    store = _mock_blob_store()
    app = _build_app(conn, arq=arq, store=store)
    archive = _zip_bytes([("alice.pdf", _PDF_MAGIC), ("bob.pdf", _PDF_MAGIC)])
    async with await _client(app) as client:
        resp = await client.post(
            f"/jobs/{uuid4()}/resumes",
            files=[("files", ("batch.zip", archive, "application/zip"))],
            data={"consent_acknowledged": "true"},
        )
    assert resp.status_code == 202
    assert arq.enqueue_job.await_count == 2


@pytest.mark.asyncio
async def test_upload_resumes_traversal_zip_rejected_and_nothing_enqueued() -> None:
    conn = _mock_conn()
    arq = MagicMock(enqueue_job=AsyncMock())
    store = _mock_blob_store()
    app = _build_app(conn, arq=arq, store=store)
    archive = _zip_bytes([("../evil.pdf", _PDF_MAGIC)])
    async with await _client(app) as client:
        resp = await client.post(
            f"/jobs/{uuid4()}/resumes",
            files=[("files", ("batch.zip", archive, "application/zip"))],
            data={"consent_acknowledged": "true"},
        )
    assert resp.status_code in (400, 422)
    arq.enqueue_job.assert_not_awaited()
    store.put.assert_not_called()


@pytest.mark.asyncio
async def test_upload_resumes_zip_bomb_rejected() -> None:
    conn = _mock_conn()
    arq = MagicMock(enqueue_job=AsyncMock())
    store = _mock_blob_store()
    app = _build_app(conn, arq=arq, store=store)
    huge = b"\x00" * (11 * 1024 * 1024)
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(zipfile.ZipInfo(filename="bomb.pdf"), huge)
    archive = buf.getvalue()
    async with await _client(app) as client:
        resp = await client.post(
            f"/jobs/{uuid4()}/resumes",
            files=[("files", ("batch.zip", archive, "application/zip"))],
            data={"consent_acknowledged": "true"},
        )
    assert resp.status_code in (400, 422)
    arq.enqueue_job.assert_not_awaited()


# ── upload: server-generated blob key, retained filename ────────────────


@pytest.mark.asyncio
async def test_upload_resumes_crafted_filename_blob_key_server_generated() -> None:
    conn = _mock_conn()
    store = _mock_blob_store()
    app = _build_app(conn, store=store)
    crafted = "Jane_Doe_Confidential_Salary_Resume.pdf"
    async with await _client(app) as client:
        resp = await client.post(
            f"/jobs/{uuid4()}/resumes",
            files=[("files", (crafted, _PDF_MAGIC, "application/pdf"))],
            data={"consent_acknowledged": "true"},
        )
    body = resp.json()
    assert body[0]["original_filename"] == crafted
    key = store.put.await_args.args[0]
    assert "jane" not in key.lower()
    assert "doe" not in key.lower()
    assert "confidential" not in key.lower()


# ── upload: nonexistent job (backend-robustness fix) ────────────────────
#
# ``POST /jobs/{job_id}/resumes`` against a job_id with no matching ``jobs``
# row must return a clean 404, never let a raw
# ``asyncpg.exceptions.ForeignKeyViolationError`` from the résumé INSERT
# escape as an unhandled 500 (diagnosed live against a real Postgres). This
# mocked-conn test proves the ROUTE-level plumbing maps "job missing" -> 404
# and performs no blob/DB write; because everything here is mocked, it can
# only exercise a PRE-INSERT existence check (the recommended fix) — the
# FK-violation-vs-clean-404 distinction itself can only be proven against a
# REAL Postgres, which is why
# ``test_api_resumes_pg.py::test_upload_to_nonexistent_job_returns_404_not_500``
# is the load-bearing test for this fix, not this file.


def _mock_conn_missing_job() -> MagicMock:
    """``fetchval`` answers ``None`` to EVERY query — including whatever
    job-existence pre-check the fix adds — while every other mocked method
    stays identical to a fresh ``_mock_conn()``."""
    conn = MagicMock(name="conn")
    conn.fetchrow = AsyncMock(return_value=None)
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchval = AsyncMock(return_value=None)
    conn.execute = AsyncMock(return_value="INSERT 0 1")
    conn.transaction = MagicMock(return_value=_acm())
    return conn


@pytest.mark.asyncio
async def test_upload_resumes_job_not_found_returns_404_and_writes_nothing() -> None:
    conn = _mock_conn_missing_job()
    store = _mock_blob_store()
    arq = MagicMock(enqueue_job=AsyncMock())
    app = _build_app(conn, arq=arq, store=store)
    async with await _client(app) as client:
        resp = await client.post(
            f"/jobs/{uuid4()}/resumes",
            files=[("files", ("resume.pdf", _PDF_MAGIC, "application/pdf"))],
            data={"consent_acknowledged": "true"},
        )
    assert resp.status_code == 404
    store.put.assert_not_called()
    conn.execute.assert_not_called()
    arq.enqueue_job.assert_not_awaited()


@pytest.mark.asyncio
async def test_upload_resumes_job_not_found_body_is_the_apperror_envelope() -> None:
    """The 404 goes through the SAME ``AppError`` -> JSON envelope every other
    not-found route uses (``{"detail": ..., "code": "resource.not_found"}``),
    not a bare FastAPI/Starlette default error page."""
    conn = _mock_conn_missing_job()
    store = _mock_blob_store()
    app = _build_app(conn, store=store)
    async with await _client(app) as client:
        resp = await client.post(
            f"/jobs/{uuid4()}/resumes",
            files=[("files", ("resume.pdf", _PDF_MAGIC, "application/pdf"))],
            data={"consent_acknowledged": "true"},
        )
    assert resp.status_code == 404
    assert resp.json()["code"] == "resource.not_found"


# ── FU-4 (RBAC): POST /jobs/{id}/resumes is admin/recruiter ONLY ────────


@pytest.mark.asyncio
async def test_upload_resumes_as_recruiter_succeeds() -> None:
    conn = _mock_conn()
    store = _mock_blob_store()
    app = _build_app(conn, store=store, role=Role.RECRUITER)
    async with await _client(app) as client:
        resp = await client.post(
            f"/jobs/{uuid4()}/resumes",
            files=[("files", ("a.pdf", _PDF_MAGIC, "application/pdf"))],
            data={"consent_acknowledged": "true"},
        )
    assert resp.status_code == 202


@pytest.mark.parametrize("role", _NON_RESUME_WRITER_ROLES)
@pytest.mark.asyncio
async def test_upload_resumes_403s_for_hiring_manager_and_auditor(role: Role) -> None:
    conn = _mock_conn()
    arq = MagicMock(enqueue_job=AsyncMock())
    store = _mock_blob_store()
    app = _build_app(conn, arq=arq, store=store, role=role)
    async with await _client(app) as client:
        resp = await client.post(
            f"/jobs/{uuid4()}/resumes",
            files=[("files", ("a.pdf", _PDF_MAGIC, "application/pdf"))],
            data={"consent_acknowledged": "true"},
        )
    assert resp.status_code == 403
    arq.enqueue_job.assert_not_awaited()
    store.put.assert_not_called()


# ── GET /jobs/{job_id}/resumes (list) ────────────────────────────────────


@pytest.mark.asyncio
async def test_list_resumes_returns_200() -> None:
    conn = _mock_conn(fetchval=False, fetch=[])
    app = _build_app(conn)
    async with await _client(app) as client:
        resp = await client.get(f"/jobs/{uuid4()}/resumes")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.parametrize("role", _ALL_ROLES)
@pytest.mark.asyncio
async def test_list_resumes_is_readable_by_every_role(role: Role) -> None:
    # FU-6 slice 6 (ADR-020 §3, fail-closed): a ``hiring_manager`` KEY needs a
    # verifiable ``hiring_manager`` SESSION to be scoped-and-served — with no
    # session override it now 403s (see the slice-6 wiring section below), so
    # this "every role can read" case supplies one for that role only. The
    # other three roles are unaffected by row-scoping (ADR-020 §4) and keep
    # relying on the ambient dev-anonymous-admin ``resolve_user`` default.
    conn = _mock_conn(fetchval=False, fetch=[])
    app = _build_app(conn, role=role)
    if role == Role.HIRING_MANAGER:
        app.dependency_overrides[resolve_user] = lambda: _identity_user(
            cas_username="hm", role="hiring_manager"
        )
    async with await _client(app) as client:
        resp = await client.get(f"/jobs/{uuid4()}/resumes")
    assert resp.status_code == 200


# ── GET /resumes/{id} — 404 + redaction byte-scan ───────────────────────


@pytest.mark.asyncio
async def test_get_resume_404_when_missing() -> None:
    conn = _mock_conn(fetchrow=None)
    app = _build_app(conn)
    async with await _client(app) as client:
        resp = await client.get(f"/resumes/{uuid4()}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_resume_under_blind_job_never_leaks_pii_bytes() -> None:
    resume_id = uuid4()
    parsed = _full_parsed(name=_NAME, email=_EMAIL, phone=_PHONE)
    row = _get_row(
        resume_id=resume_id,
        parsed=parsed,
        candidate_name=_NAME,
        candidate_email=_EMAIL,
        candidate_phone=_PHONE,
    )
    conn = _mock_conn(fetchrow=row, fetchval=True)  # blind job
    app = _build_app(conn)
    async with await _client(app) as client:
        resp = await client.get(f"/resumes/{resume_id}")
    assert resp.status_code == 200
    raw = resp.text
    assert _NAME not in raw
    assert _EMAIL not in raw
    assert _PHONE not in raw


@pytest.mark.asyncio
async def test_get_resume_under_non_blind_job_reveals_pii() -> None:
    resume_id = uuid4()
    parsed = _full_parsed(name=_NAME, email=_EMAIL, phone=_PHONE)
    row = _get_row(
        resume_id=resume_id,
        parsed=parsed,
        candidate_name=_NAME,
        candidate_email=_EMAIL,
        candidate_phone=_PHONE,
    )
    conn = _mock_conn(fetchrow=row, fetchval=False)  # non-blind job
    app = _build_app(conn)
    async with await _client(app) as client:
        resp = await client.get(f"/resumes/{resume_id}")
    assert resp.status_code == 200
    assert _EMAIL in resp.text


@pytest.mark.parametrize("role", _ALL_ROLES)
@pytest.mark.asyncio
async def test_get_resume_is_readable_by_every_role(role: Role) -> None:
    # FU-6 slice 6 (ADR-020 §3, fail-closed): the hiring_manager KEY case needs
    # a real hiring_manager session to be served (mirrors the list route above
    # and the slice-5 jobs pattern); the other three roles read via the ambient
    # dev-anonymous-admin default.
    resume_id = uuid4()
    row = _get_row(resume_id=resume_id, parsed=None)
    conn = _mock_conn(fetchrow=row, fetchval=True)
    app = _build_app(conn, role=role)
    if role == Role.HIRING_MANAGER:
        app.dependency_overrides[resolve_user] = lambda: _identity_user(
            cas_username="hm", role="hiring_manager"
        )
    async with await _client(app) as client:
        resp = await client.get(f"/resumes/{resume_id}")
    assert resp.status_code == 200


# ── FU-4/D3: ?reveal=true on the read route is a no-op ──────────────────


@pytest.mark.asyncio
async def test_get_resume_reveal_true_query_param_is_ignored_under_a_blind_job() -> (
    None
):
    """The whole point of D3: even a caller who explicitly appends
    ``?reveal=true`` gets the BLIND response — this route has no code path
    to un-blind at all any more. Asserted on the raw serialized response
    bytes (the established byte-scan style), not merely a field being
    ``None``, so a route that silently reads-and-drops the query param while
    leaking identity into some OTHER field would still fail this test."""
    resume_id = uuid4()
    parsed = _full_parsed(name=_NAME, email=_EMAIL, phone=_PHONE)
    row = _get_row(
        resume_id=resume_id,
        parsed=parsed,
        candidate_name=_NAME,
        candidate_email=_EMAIL,
        candidate_phone=_PHONE,
    )
    conn = _mock_conn(fetchrow=row, fetchval=True)  # blind job
    app = _build_app(conn)
    async with await _client(app) as client:
        resp = await client.get(f"/resumes/{resume_id}?reveal=true")
    assert resp.status_code == 200
    raw = resp.text
    assert _NAME not in raw
    assert _EMAIL not in raw
    assert _PHONE not in raw


@pytest.mark.asyncio
async def test_get_resume_route_never_forwards_a_reveal_kwarg_to_the_service(
    monkeypatch: Any,
) -> None:
    """Structural pin: the route must call
    ``resume_service.get_one(db, resume_id)`` with no ``reveal`` kwarg AT ALL
    (or an explicit ``reveal=False``) — never a value threaded through from
    the query string, because the query string parameter no longer exists in
    the route's own signature."""
    from src.schemas.resumes import CandidateInfo, ResumeOut

    resume_id = uuid4()
    stub_out = ResumeOut(
        id=resume_id,
        job_id=uuid4(),
        original_filename="resume.pdf",
        mime_type="application/pdf",
        file_size_bytes=1234,
        sha256="a" * 64,
        candidate=CandidateInfo(name=None, email=None, phone=None, location=None),
        candidate_email_hash=None,
        parsed=None,
        status="parsed",
        uploaded_by="api",
        uploaded_at=_NOW,
        parsed_at=_NOW,
        failure_reason=None,
        consent_acknowledged=True,
        blinded=True,
    )
    spy = AsyncMock(return_value=stub_out)
    row = _get_row(resume_id=resume_id, parsed=None)
    conn = _mock_conn(fetchrow=row, fetchval=True)
    app = _build_app(conn)
    monkeypatch.setattr(resumes_routes.resume_service, "get_one", spy)
    async with await _client(app) as client:
        resp = await client.get(f"/resumes/{resume_id}?reveal=true")
    assert resp.status_code == 200
    spy.assert_awaited_once()
    _, kwargs = spy.await_args
    assert kwargs.get("reveal", False) is False


# ── FU-5 slice 7 (ADR-019 §8.3/§9.2): uploaded_by sourced from resolve_user,
#    NOT the deleted X-Actor-Name header ──────────────────────────────────
#
# ``resolve_actor``/``X-Actor-Name`` no longer exist (see ``test_api_deps.py``
# for the deletion pin). ``uploaded_by`` on résumé upload now comes from
# ``src.api.deps.resolve_user`` — the session/dev-anonymous identity's
# ``cas_username`` when a user resolves, ``None`` when it does not (a bare
# service-key caller). Only ONE résumé file is uploaded per test here, so
# ``conn.execute.await_args`` unambiguously names the single INSERT call —
# the response body cannot prove this (the route never round-trips
# ``uploaded_by`` back out on the 202 accepted-batch shape).


def _identity_user(*, cas_username: str = "alice", role: str = "recruiter") -> User:
    return User(
        id=uuid4(),
        cas_username=cas_username,
        display_name=cas_username,
        email=None,
        role=role,
        active=True,
        created_at=_NOW,
        last_seen_at=_NOW,
    )


@pytest.mark.asyncio
async def test_upload_resumes_uploaded_by_is_dev_anonymous_when_cas_disabled() -> None:
    """ADR-019 §10b/§9.2: CAS disabled is this test suite's ambient default
    — no ``resolve_user`` override is installed, so the REAL dependency
    resolves the synthetic dev-anonymous identity, and the uploaded résumé's
    ``uploaded_by`` is ``"dev-anonymous"`` — never the retired ``"api"``
    constant."""
    conn = _mock_conn()
    app = _build_app(conn)
    async with await _client(app) as client:
        resp = await client.post(
            f"/jobs/{uuid4()}/resumes",
            files=[("files", ("a.pdf", _PDF_MAGIC, "application/pdf"))],
            data={"consent_acknowledged": "true"},
        )
    assert resp.status_code == 202
    assert "dev-anonymous" in conn.execute.await_args.args


@pytest.mark.asyncio
async def test_upload_resumes_ignores_an_x_actor_name_header_entirely() -> None:
    """A leftover ``X-Actor-Name`` header must be read nowhere — the identity
    source is exclusively ``resolve_user``."""
    conn = _mock_conn()
    app = _build_app(conn)
    async with await _client(app) as client:
        resp = await client.post(
            f"/jobs/{uuid4()}/resumes",
            files=[("files", ("a.pdf", _PDF_MAGIC, "application/pdf"))],
            data={"consent_acknowledged": "true"},
            headers={"X-Actor-Name": "totally-ignored@example.test"},
        )
    assert resp.status_code == 202
    assert "totally-ignored@example.test" not in conn.execute.await_args.args
    assert "dev-anonymous" in conn.execute.await_args.args


@pytest.mark.asyncio
async def test_upload_resumes_uploaded_by_is_the_resolved_users_cas_username() -> None:
    conn = _mock_conn()
    app = _build_app(conn)
    app.dependency_overrides[resolve_user] = lambda: _identity_user(
        cas_username="priya"
    )
    async with await _client(app) as client:
        resp = await client.post(
            f"/jobs/{uuid4()}/resumes",
            files=[("files", ("a.pdf", _PDF_MAGIC, "application/pdf"))],
            data={"consent_acknowledged": "true"},
        )
    assert resp.status_code == 202
    assert "priya" in conn.execute.await_args.args


@pytest.mark.asyncio
async def test_upload_resumes_403s_when_no_user_resolves() -> None:
    """REVERSED (security finding, fix/auth-boundary-fails-open) — this test
    used to pin ``uploaded_by = NULL`` on a 202 for a bare service-key
    caller with NO CAS session at all. A live audit against real Postgres
    proved that this exact combination — zero ``API_KEY_*`` configured (so
    ``resolve_role`` trivially resolves ``Role.ADMIN`` for anyone) plus a
    cookie-less caller (``resolve_user`` -> ``None``) — leaves every write
    route reachable by literally anyone with no credential at all. The
    human decision: a valid API key (or no key at all, when auth is
    disabled) is NEVER sufficient on its own — every write route now
    requires a REAL human CAS session, so ``resolve_user`` -> ``None`` must
    403 before the upload/insert ever runs."""
    conn = _mock_conn()
    app = _build_app(conn)
    app.dependency_overrides[resolve_user] = lambda: None
    async with await _client(app) as client:
        resp = await client.post(
            f"/jobs/{uuid4()}/resumes",
            files=[("files", ("a.pdf", _PDF_MAGIC, "application/pdf"))],
            data={"consent_acknowledged": "true"},
        )
    assert resp.status_code == 403
    conn.execute.assert_not_awaited()


# ── FU-6 slice 6 (ADR-020 §3/§5) — row-scoping WIRING for
#    GET /jobs/{job_id}/resumes and GET /resumes/{resume_id} ────────────────
#
# These tests prove WIRING only: that the route resolves the caller's CAS
# session identity (``resolve_user``) and forwards the correct ``user_id`` to
# ``resume_service.list_for_job`` / ``resume_service.get_one`` via
# ``scoped_user_id_or_403`` (already unit-tested standalone in
# ``test_api_deps.py``, and exercised identically for the sibling jobs routes
# in ``test_route_jobs.py``). They mock ``resume_service`` entirely, so they
# cannot prove the ``EXISTS`` predicate actually FILTERS rows in a real query
# planner against real data — that structural proof, plus the
# unassigned-vs-nonexistent-job list/get distinction, is
# ``test_resume_scoping_pg.py``'s job (real Postgres, ADR-020 §3, mandatory).

_UNSCOPED_KEY_ROLES: tuple[tuple[Role, str], ...] = (
    (Role.ADMIN, "admin"),
    (Role.RECRUITER, "recruiter"),
    (Role.AUDITOR, "auditor"),
)


def _resume_out(resume_id: UUID | None = None) -> ResumeOut:
    """A valid ``ResumeOut`` the mocked ``resume_service.get_one`` can
    return — the route's return-type annotation doubles as its response_model."""
    return ResumeOut(
        id=resume_id or uuid4(),
        job_id=uuid4(),
        original_filename="resume.pdf",
        mime_type="application/pdf",
        file_size_bytes=1234,
        sha256="a" * 64,
        candidate=CandidateInfo(name=None, email=None, phone=None, location=None),
        candidate_email_hash=None,
        parsed=None,
        status="parsed",
        uploaded_by="api",
        uploaded_at=_NOW,
        parsed_at=_NOW,
        failure_reason=None,
        consent_acknowledged=True,
        blinded=True,
    )


# ── GET /jobs/{job_id}/resumes — user_id wiring ─────────────────────────


@pytest.mark.asyncio
async def test_list_resumes_passes_user_id_for_a_hiring_manager_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _mock_conn(fetchval=False, fetch=[])
    app = _build_app(conn, role=Role.HIRING_MANAGER)
    hm = _identity_user(cas_username="hm", role="hiring_manager")
    app.dependency_overrides[resolve_user] = lambda: hm
    list_mock = AsyncMock(return_value=[])
    monkeypatch.setattr(resumes_routes.resume_service, "list_for_job", list_mock)

    async with await _client(app) as client:
        resp = await client.get(f"/jobs/{uuid4()}/resumes")

    assert resp.status_code == 200
    list_mock.assert_awaited_once()
    assert list_mock.await_args.kwargs.get("user_id") == hm.id


@pytest.mark.asyncio
async def test_list_resumes_passes_user_id_for_a_hiring_manager_even_with_the_shared_recruiter_key(  # noqa: E501
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The trap ADR-020 §4/§3 calls out explicitly: the Flask viewer's
    browser session presents the ONE shared "recruiter" API key for every
    human, so ``key_role`` resolves to ``Role.RECRUITER`` even when the
    signed-in human is a hiring_manager. Scoping must key off the SESSION
    identity, not the key role (mirrors ``test_route_jobs.py``'s
    identically-named test)."""
    conn = _mock_conn(fetchval=False, fetch=[])
    app = _build_app(conn, role=Role.RECRUITER)
    hm = _identity_user(cas_username="hm", role="hiring_manager")
    app.dependency_overrides[resolve_user] = lambda: hm
    list_mock = AsyncMock(return_value=[])
    monkeypatch.setattr(resumes_routes.resume_service, "list_for_job", list_mock)

    async with await _client(app) as client:
        resp = await client.get(f"/jobs/{uuid4()}/resumes")

    assert resp.status_code == 200
    list_mock.assert_awaited_once()
    assert list_mock.await_args.kwargs.get("user_id") == hm.id


@pytest.mark.parametrize("key_role,session_role", _UNSCOPED_KEY_ROLES)
@pytest.mark.asyncio
async def test_list_resumes_passes_user_id_none_for_unscoped_roles(
    key_role: Role, session_role: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = _mock_conn(fetchval=False, fetch=[])
    app = _build_app(conn, role=key_role)
    session_user = _identity_user(cas_username="someone", role=session_role)
    app.dependency_overrides[resolve_user] = lambda: session_user
    list_mock = AsyncMock(return_value=[])
    monkeypatch.setattr(resumes_routes.resume_service, "list_for_job", list_mock)

    async with await _client(app) as client:
        resp = await client.get(f"/jobs/{uuid4()}/resumes")

    assert resp.status_code == 200
    list_mock.assert_awaited_once()
    assert list_mock.await_args.kwargs.get("user_id") is None


@pytest.mark.asyncio
async def test_list_resumes_passes_user_id_none_for_dev_anonymous_admin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ambient default (no ``resolve_user`` override, ``cas_enabled=False``):
    the synthetic dev-anonymous admin identity resolves. Must stay
    unscoped."""
    conn = _mock_conn(fetchval=False, fetch=[])
    app = _build_app(conn, role=Role.ADMIN)
    list_mock = AsyncMock(return_value=[])
    monkeypatch.setattr(resumes_routes.resume_service, "list_for_job", list_mock)

    async with await _client(app) as client:
        resp = await client.get(f"/jobs/{uuid4()}/resumes")

    assert resp.status_code == 200
    list_mock.assert_awaited_once()
    assert list_mock.await_args.kwargs.get("user_id") is None


@pytest.mark.asyncio
async def test_list_resumes_403s_and_never_calls_the_service_for_hiring_manager_key_with_no_session(  # noqa: E501
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail-closed (ADR-020 §3 final paragraph): a hiring_manager KEY with no
    verifiable session identity must 403 and must NEVER reach the service
    layer unscoped."""
    conn = _mock_conn(fetchval=False, fetch=[])
    app = _build_app(conn, role=Role.HIRING_MANAGER)
    app.dependency_overrides[resolve_user] = lambda: None
    list_mock = AsyncMock(return_value=[])
    monkeypatch.setattr(resumes_routes.resume_service, "list_for_job", list_mock)

    async with await _client(app) as client:
        resp = await client.get(f"/jobs/{uuid4()}/resumes")

    assert resp.status_code == 403
    list_mock.assert_not_awaited()


# ── GET /resumes/{resume_id} — user_id wiring ───────────────────────────


@pytest.mark.asyncio
async def test_get_resume_passes_user_id_for_a_hiring_manager_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resume_id = uuid4()
    conn = _mock_conn()
    app = _build_app(conn, role=Role.HIRING_MANAGER)
    hm = _identity_user(cas_username="hm", role="hiring_manager")
    app.dependency_overrides[resolve_user] = lambda: hm
    get_mock = AsyncMock(return_value=_resume_out(resume_id=resume_id))
    monkeypatch.setattr(resumes_routes.resume_service, "get_one", get_mock)

    async with await _client(app) as client:
        resp = await client.get(f"/resumes/{resume_id}")

    assert resp.status_code == 200
    get_mock.assert_awaited_once()
    assert get_mock.await_args.kwargs.get("user_id") == hm.id


@pytest.mark.parametrize("key_role,session_role", _UNSCOPED_KEY_ROLES)
@pytest.mark.asyncio
async def test_get_resume_passes_user_id_none_for_unscoped_roles(
    key_role: Role, session_role: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    resume_id = uuid4()
    conn = _mock_conn()
    app = _build_app(conn, role=key_role)
    session_user = _identity_user(cas_username="someone", role=session_role)
    app.dependency_overrides[resolve_user] = lambda: session_user
    get_mock = AsyncMock(return_value=_resume_out(resume_id=resume_id))
    monkeypatch.setattr(resumes_routes.resume_service, "get_one", get_mock)

    async with await _client(app) as client:
        resp = await client.get(f"/resumes/{resume_id}")

    assert resp.status_code == 200
    get_mock.assert_awaited_once()
    assert get_mock.await_args.kwargs.get("user_id") is None


@pytest.mark.asyncio
async def test_get_resume_passes_user_id_none_for_dev_anonymous_admin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resume_id = uuid4()
    conn = _mock_conn()
    app = _build_app(conn, role=Role.ADMIN)
    get_mock = AsyncMock(return_value=_resume_out(resume_id=resume_id))
    monkeypatch.setattr(resumes_routes.resume_service, "get_one", get_mock)

    async with await _client(app) as client:
        resp = await client.get(f"/resumes/{resume_id}")

    assert resp.status_code == 200
    get_mock.assert_awaited_once()
    assert get_mock.await_args.kwargs.get("user_id") is None


@pytest.mark.asyncio
async def test_get_resume_403s_and_never_calls_the_service_for_hiring_manager_key_with_no_session(  # noqa: E501
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resume_id = uuid4()
    conn = _mock_conn()
    app = _build_app(conn, role=Role.HIRING_MANAGER)
    app.dependency_overrides[resolve_user] = lambda: None
    get_mock = AsyncMock(return_value=_resume_out(resume_id=resume_id))
    monkeypatch.setattr(resumes_routes.resume_service, "get_one", get_mock)

    async with await _client(app) as client:
        resp = await client.get(f"/resumes/{resume_id}")

    assert resp.status_code == 403
    get_mock.assert_not_awaited()


# ── FU-6 slice 8 (ADR-020 §6) — auditor read-logging: "the watchers are
#    watched", GET /resumes/{id} ─────────────────────────────────────────────
#
# Scope decision mirrored from ``test_route_jobs.py`` (see that file's
# matching section header for the full rationale): only the single-subject
# ``GET /resumes/{id}`` is logged here — the polled list route ``GET
# /jobs/{id}/resumes`` is deliberately NOT logged (dedicated negative test
# below). The read-log fires ONLY for a REAL auditor session (``user.role ==
# "auditor"`` and not the dev-anonymous sentinel), and only on a SUCCESSFUL
# (200) read. Patches ``resumes_routes.audit_service`` directly — that
# module is ALREADY imported into ``resumes.py`` (used by the reveal route),
# so this is the exact same attribute ``test_route_reveal.py`` patches for
# reveal's own audit row; this slice's read-log is a SEPARATE call, never a
# duplicate of the reveal audit (the reveal route is untouched by this
# slice).


def _auditor_identity_user(*, cas_username: str = "audrey") -> User:
    return _identity_user(cas_username=cas_username, role="auditor")


@pytest.mark.asyncio
async def test_get_resume_real_auditor_session_writes_exactly_one_read_resume_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resume_id = uuid4()
    conn = _mock_conn()
    app = _build_app(conn, role=Role.AUDITOR)
    auditor = _auditor_identity_user()
    app.dependency_overrides[resolve_user] = lambda: auditor
    get_mock = AsyncMock(return_value=_resume_out(resume_id=resume_id))
    monkeypatch.setattr(resumes_routes.resume_service, "get_one", get_mock)
    audit = AsyncMock(return_value=None)
    monkeypatch.setattr(resumes_routes.audit_service, "record_audit", audit)

    async with await _client(app) as client:
        resp = await client.get(f"/resumes/{resume_id}")

    assert resp.status_code == 200
    audit.assert_awaited_once()
    kwargs = audit.await_args.kwargs
    assert kwargs["action"] == "read_resume"
    assert kwargs["actor_kind"] == "user"
    assert kwargs["actor_user_id"] == auditor.id
    assert kwargs["subject_type"] == "resume"
    assert kwargs["subject_id"] == resume_id


@pytest.mark.parametrize(
    "key_role,session_role",
    [
        (Role.ADMIN, "admin"),
        (Role.RECRUITER, "recruiter"),
        (Role.HIRING_MANAGER, "hiring_manager"),
    ],
)
@pytest.mark.asyncio
async def test_get_resume_non_auditor_session_writes_no_read_log(
    key_role: Role, session_role: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    resume_id = uuid4()
    conn = _mock_conn()
    app = _build_app(conn, role=key_role)
    session_user = _identity_user(cas_username="someone", role=session_role)
    app.dependency_overrides[resolve_user] = lambda: session_user
    get_mock = AsyncMock(return_value=_resume_out(resume_id=resume_id))
    monkeypatch.setattr(resumes_routes.resume_service, "get_one", get_mock)
    audit = AsyncMock(return_value=None)
    monkeypatch.setattr(resumes_routes.audit_service, "record_audit", audit)

    async with await _client(app) as client:
        resp = await client.get(f"/resumes/{resume_id}")

    assert resp.status_code == 200
    audit.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_resume_dev_anonymous_admin_writes_no_read_log(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resume_id = uuid4()
    conn = _mock_conn()
    app = _build_app(conn, role=Role.ADMIN)
    get_mock = AsyncMock(return_value=_resume_out(resume_id=resume_id))
    monkeypatch.setattr(resumes_routes.resume_service, "get_one", get_mock)
    audit = AsyncMock(return_value=None)
    monkeypatch.setattr(resumes_routes.audit_service, "record_audit", audit)

    async with await _client(app) as client:
        resp = await client.get(f"/resumes/{resume_id}")

    assert resp.status_code == 200
    audit.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_resume_404_writes_no_read_log_even_for_a_real_auditor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resume_id = uuid4()
    conn = _mock_conn()
    app = _build_app(conn, role=Role.AUDITOR)
    auditor = _auditor_identity_user()
    app.dependency_overrides[resolve_user] = lambda: auditor
    get_mock = AsyncMock(
        side_effect=NotFoundError(
            f"resume {resume_id} not found", resume_id=str(resume_id)
        )
    )
    monkeypatch.setattr(resumes_routes.resume_service, "get_one", get_mock)
    audit = AsyncMock(return_value=None)
    monkeypatch.setattr(resumes_routes.audit_service, "record_audit", audit)

    async with await _client(app) as client:
        resp = await client.get(f"/resumes/{resume_id}")

    assert resp.status_code == 404
    audit.assert_not_awaited()


@pytest.mark.asyncio
async def test_list_resumes_never_writes_a_read_log_even_for_an_auditor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scope decision: the polled list route ``GET /jobs/{id}/resumes`` is
    never read-logged, even for a real auditor session."""
    conn = _mock_conn(fetchval=False, fetch=[])
    app = _build_app(conn, role=Role.AUDITOR)
    auditor = _auditor_identity_user()
    app.dependency_overrides[resolve_user] = lambda: auditor
    list_mock = AsyncMock(return_value=[])
    monkeypatch.setattr(resumes_routes.resume_service, "list_for_job", list_mock)
    audit = AsyncMock(return_value=None)
    monkeypatch.setattr(resumes_routes.audit_service, "record_audit", audit)

    async with await _client(app) as client:
        resp = await client.get(f"/jobs/{uuid4()}/resumes")

    assert resp.status_code == 200
    audit.assert_not_awaited()
