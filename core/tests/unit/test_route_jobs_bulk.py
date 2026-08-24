"""Route-level tests for ``POST /jobs/bulk`` (FU-3 Slice 4 — bulk JD upload).

Mirrors ``test_route_jobs.py``: a fresh ``FastAPI()`` with the real router and
``dependency_overrides`` for ``get_db`` / ``get_arq`` / ``resolve_role``; the
real service layer runs against a mocked asyncpg connection (no service-internals
monkeypatching).

**Ambiguities this file locks:**

* ``POST /jobs/bulk`` accepts ``files: list[UploadFile]`` (one part per JD, or a
  single ``.zip`` that is expanded server-side via ``expand_zip_entries`` with the
  bulk-JD extension allowlist ``{pdf,docx,txt,json}``) plus an OPTIONAL
  ``manifest: UploadFile`` (a CSV). It is declared BEFORE ``/jobs/{job_id}`` so
  "bulk" is never parsed as a job id.
* Returns **202** with a ``list[BulkJobResult]`` — one per expanded file — and
  enqueues ``arq.enqueue_job("parse_job", str(job_id))`` ONCE PER CREATED job
  (never for duplicate/failed rows).
* The route never client-expands beyond ``expand_zip_entries`` — a ``.zip`` is
  forwarded verbatim to that hardened expander.

**FU-4 (RBAC):** ``POST /jobs/bulk`` is admin/recruiter ONLY (same allowed
set as ``POST /jobs``/``POST /jobs/jd-extract`` — see the route→role table in
``test_route_jobs.py``'s module docstring).

**FU-5 slice 7 (ADR-019 §8.3/§9.2):** ``created_by`` here is sourced from
``src.api.deps.resolve_user`` exactly like ``POST /jobs`` — see the dedicated
section near the end of this file. The deleted ``resolve_actor``/
``X-Actor-Name`` header is read nowhere.
"""

from __future__ import annotations

import datetime as dt
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
from src.api.routes import jobs as jobs_routes
from src.errors import AppError
from src.models.pool import get_db
from src.schemas.auth import User

_NOW = dt.datetime(2026, 7, 17, tzinfo=dt.UTC)
_JD = "We are hiring a senior backend engineer with deep Python experience."
_NON_JOB_WRITER_ROLES: tuple[Role, ...] = (Role.HIRING_MANAGER, Role.AUDITOR)


class _Row(dict[str, Any]):
    def __getitem__(self, key: str) -> Any:
        return dict.get(self, key)


def _job_row(*, job_id: UUID, title: str) -> _Row:
    return _Row(
        {
            "id": job_id,
            "title": title,
            "department": None,
            "location": None,
            "employment_type": None,
            "seniority": None,
            "min_years": None,
            "description_raw": _JD,
            "description_parsed": None,
            "status": "draft",
            "retention_days": 180,
            "shortlist_top_percent": 100,
            "blind_review": True,
            "failure_reason": None,
            "created_by": "api",
            "created_at": _NOW,
            "updated_at": _NOW,
            "parsed_at": None,
            "closed_at": None,
        }
    )


def _mock_conn() -> MagicMock:
    conn = MagicMock(name="conn")
    conn.fetchval = AsyncMock(return_value=None)  # no existing sha

    async def _fetchrow(_sql: str, *args: Any) -> _Row:
        return _job_row(job_id=uuid4(), title=args[0])

    conn.fetchrow = AsyncMock(side_effect=_fetchrow)
    conn.fetch = AsyncMock(return_value=[])
    conn.execute = AsyncMock(return_value="OK")
    return conn


def _build_app(
    conn: MagicMock, *, arq: MagicMock | None = None, role: Role = Role.ADMIN
) -> FastAPI:
    app = FastAPI()
    app.include_router(jobs_routes.router)

    async def _get_db_override() -> AsyncIterator[Any]:
        yield conn

    app.dependency_overrides[get_db] = _get_db_override
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
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in entries:
            zf.writestr(name, content)
    return buf.getvalue()


@pytest.mark.asyncio
async def test_bulk_three_files_returns_202_and_three_results() -> None:
    conn = _mock_conn()
    arq = MagicMock(enqueue_job=AsyncMock())
    app = _build_app(conn, arq=arq)
    files = [
        ("files", ("Backend.txt", ("Backend role. " + _JD).encode(), "text/plain")),
        ("files", ("Frontend.txt", ("Frontend role. " + _JD).encode(), "text/plain")),
        ("files", ("Data.txt", ("Data role. " + _JD).encode(), "text/plain")),
    ]
    async with await _client(app) as client:
        resp = await client.post("/jobs/bulk", files=files)
    assert resp.status_code == 202
    body = resp.json()
    assert len(body) == 3
    assert all(r["outcome"] == "created" for r in body)
    # parse_job enqueued once per created job.
    assert arq.enqueue_job.await_count == 3
    assert all(call.args[0] == "parse_job" for call in arq.enqueue_job.await_args_list)


@pytest.mark.asyncio
async def test_bulk_over_file_count_cap_rejected_nothing_enqueued() -> None:
    """Memory-exhaustion guard (parity with the résumé route): a batch over the
    per-request file cap is rejected on COUNT before any body is read, and
    nothing is enqueued."""
    from src.api.routes.jobs import _MAX_BULK_JD_FILES

    conn = _mock_conn()
    arq = MagicMock(enqueue_job=AsyncMock())
    app = _build_app(conn, arq=arq)
    files = [
        ("files", (f"jd_{i}.txt", ("Role. " + _JD).encode(), "text/plain"))
        for i in range(_MAX_BULK_JD_FILES + 1)
    ]
    async with await _client(app) as client:
        resp = await client.post("/jobs/bulk", files=files)
    assert resp.status_code >= 400
    arq.enqueue_job.assert_not_awaited()


@pytest.mark.asyncio
async def test_bulk_enqueues_only_for_created_jobs() -> None:
    conn = _mock_conn()
    arq = MagicMock(enqueue_job=AsyncMock())
    app = _build_app(conn, arq=arq)
    # Two identical bodies → first created, second duplicate (in-batch).
    identical = ("An identical JD body. " + _JD).encode()
    files = [
        ("files", ("first.txt", identical, "text/plain")),
        ("files", ("second.txt", identical, "text/plain")),
    ]
    async with await _client(app) as client:
        resp = await client.post("/jobs/bulk", files=files)
    body = resp.json()
    assert [r["outcome"] for r in body] == ["created", "duplicate"]
    assert arq.enqueue_job.await_count == 1


@pytest.mark.asyncio
async def test_bulk_expands_a_zip_of_jds() -> None:
    conn = _mock_conn()
    arq = MagicMock(enqueue_job=AsyncMock())
    app = _build_app(conn, arq=arq)
    zip_blob = _zip_bytes(
        [
            ("Backend.txt", ("Backend role. " + _JD).encode()),
            ("Frontend.txt", ("Frontend role. " + _JD).encode()),
        ]
    )
    files = [("files", ("jobs.zip", zip_blob, "application/zip"))]
    async with await _client(app) as client:
        resp = await client.post("/jobs/bulk", files=files)
    assert resp.status_code == 202
    body = resp.json()
    assert len(body) == 2
    assert all(r["outcome"] == "created" for r in body)


@pytest.mark.asyncio
async def test_bulk_with_a_manifest_applies_the_title_override() -> None:
    conn = _mock_conn()
    app = _build_app(conn)
    manifest_csv = b"filename,title\nBackend.txt,Staff Backend Engineer\n"
    files = [
        ("files", ("Backend.txt", ("Backend role. " + _JD).encode(), "text/plain")),
        ("manifest", ("manifest.csv", manifest_csv, "text/csv")),
    ]
    async with await _client(app) as client:
        resp = await client.post("/jobs/bulk", files=files)
    assert resp.status_code == 202
    body = resp.json()
    assert body[0]["title"] == "Staff Backend Engineer"


@pytest.mark.asyncio
async def test_bulk_bad_manifest_is_a_422() -> None:
    conn = _mock_conn()
    app = _build_app(conn)
    bad_manifest = b"title,department\nBackend,Engineering\n"  # no filename column
    files = [
        ("files", ("Backend.txt", ("Backend role. " + _JD).encode(), "text/plain")),
        ("manifest", ("manifest.csv", bad_manifest, "text/csv")),
    ]
    async with await _client(app) as client:
        resp = await client.post("/jobs/bulk", files=files)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_bulk_route_is_declared_before_job_id_route() -> None:
    """``/jobs/bulk`` must not be swallowed by ``/jobs/{job_id}`` — a POST to it
    reaches the bulk handler (202), never a 422 uuid-parse error."""
    conn = _mock_conn()
    app = _build_app(conn)
    files = [
        ("files", ("Backend.txt", ("Backend role. " + _JD).encode(), "text/plain"))
    ]
    async with await _client(app) as client:
        resp = await client.post("/jobs/bulk", files=files)
    assert resp.status_code == 202


# ── FU-4 (RBAC): admin/recruiter ONLY ────────────────────────────────────


@pytest.mark.asyncio
async def test_bulk_as_recruiter_succeeds() -> None:
    conn = _mock_conn()
    app = _build_app(conn, role=Role.RECRUITER)
    files = [
        ("files", ("Backend.txt", ("Backend role. " + _JD).encode(), "text/plain"))
    ]
    async with await _client(app) as client:
        resp = await client.post("/jobs/bulk", files=files)
    assert resp.status_code == 202


@pytest.mark.parametrize("role", _NON_JOB_WRITER_ROLES)
@pytest.mark.asyncio
async def test_bulk_403s_for_hiring_manager_and_auditor(role: Role) -> None:
    conn = _mock_conn()
    arq = MagicMock(enqueue_job=AsyncMock())
    app = _build_app(conn, arq=arq, role=role)
    files = [
        ("files", ("Backend.txt", ("Backend role. " + _JD).encode(), "text/plain"))
    ]
    async with await _client(app) as client:
        resp = await client.post("/jobs/bulk", files=files)
    assert resp.status_code == 403
    arq.enqueue_job.assert_not_awaited()


# ── FU-5 slice 7 (ADR-019 §8.3/§9.2): created_by sourced from resolve_user ──


@pytest.mark.asyncio
async def test_bulk_created_by_is_dev_anonymous_when_cas_disabled() -> None:
    """Same identity source as ``POST /jobs`` — CAS disabled resolves the
    synthetic dev-anonymous identity for every created job in the batch."""
    conn = _mock_conn()
    app = _build_app(conn)
    files = [
        ("files", ("Backend.txt", ("Backend role. " + _JD).encode(), "text/plain"))
    ]
    async with await _client(app) as client:
        resp = await client.post("/jobs/bulk", files=files)
    assert resp.status_code == 202
    assert any("dev-anonymous" in call.args for call in conn.fetchrow.await_args_list)


@pytest.mark.asyncio
async def test_bulk_ignores_an_x_actor_name_header_entirely() -> None:
    conn = _mock_conn()
    app = _build_app(conn)
    files = [
        ("files", ("Backend.txt", ("Backend role. " + _JD).encode(), "text/plain"))
    ]
    async with await _client(app) as client:
        resp = await client.post(
            "/jobs/bulk",
            files=files,
            headers={"X-Actor-Name": "totally-ignored@example.test"},
        )
    assert resp.status_code == 202
    all_args = [a for call in conn.fetchrow.await_args_list for a in call.args]
    assert "totally-ignored@example.test" not in all_args
    assert "dev-anonymous" in all_args


@pytest.mark.asyncio
async def test_bulk_created_by_is_the_resolved_users_cas_username() -> None:
    conn = _mock_conn()
    app = _build_app(conn)
    app.dependency_overrides[resolve_user] = lambda: User(
        id=uuid4(),
        cas_username="priya",
        display_name="priya",
        email=None,
        role="recruiter",
        active=True,
        created_at=_NOW,
        last_seen_at=_NOW,
    )
    files = [
        ("files", ("Backend.txt", ("Backend role. " + _JD).encode(), "text/plain"))
    ]
    async with await _client(app) as client:
        resp = await client.post("/jobs/bulk", files=files)
    assert resp.status_code == 202
    assert any("priya" in call.args for call in conn.fetchrow.await_args_list)
