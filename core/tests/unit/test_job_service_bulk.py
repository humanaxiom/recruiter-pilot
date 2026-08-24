"""Unit tests for ``job_service.create_jobs_bulk`` (FU-3 Slice 4 — bulk JD).

The service takes a flat list of ``(filename, bytes)`` (the ZIP is expanded at
the route layer), extracts JD text per file, applies an optional CSV manifest's
metadata overrides, deduplicates on ``description_sha256`` and inserts one draft
job per unique, long-enough JD — returning a ``BulkJobResult`` per file so the
route can enqueue ``parse_job`` once per *created* job and surface per-file
outcomes (created / duplicate / failed) without one bad file aborting the batch.

RED half of the TDD cycle — ``create_jobs_bulk`` does not exist yet.

The connection is a mock: ``fetchval`` is the dedup probe (``None`` = no existing
job with that sha), ``fetchrow`` is the INSERT ... RETURNING that hands back the
inserted row. The real service layer runs against these; no service internals are
monkeypatched.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from src.services import job_service
from src.services.bulk_ingest_service import JobManifestRow

_NOW = dt.datetime(2026, 7, 17, tzinfo=dt.UTC)

_JD_TEXT = "We are hiring a senior backend engineer with deep Python experience."


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
            "description_raw": _JD_TEXT,
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


def _mock_conn(*, existing_sha: bool = False) -> MagicMock:
    """A conn whose dedup probe returns None (no existing job) and whose
    INSERT returns a fresh row echoing the requested title."""
    conn = MagicMock(name="conn")
    conn.fetchval = AsyncMock(return_value=uuid4() if existing_sha else None)

    async def _fetchrow(_sql: str, *args: Any) -> _Row:
        # title is the first positional arg to the INSERT.
        return _job_row(job_id=uuid4(), title=args[0])

    conn.fetchrow = AsyncMock(side_effect=_fetchrow)
    return conn


def _txt(text: str = _JD_TEXT) -> bytes:
    return text.encode("utf-8")


@pytest.mark.asyncio
async def test_three_files_create_three_jobs() -> None:
    conn = _mock_conn()
    files = [
        ("Backend Engineer.txt", _txt("Backend role. " + _JD_TEXT)),
        ("Frontend Engineer.txt", _txt("Frontend role. " + _JD_TEXT)),
        ("Data Scientist.txt", _txt("Data role. " + _JD_TEXT)),
    ]
    results = await job_service.create_jobs_bulk(
        conn, files=files, manifest=None, created_by="api"
    )
    assert len(results) == 3
    assert all(r.outcome == "created" for r in results)
    assert all(r.job_id is not None for r in results)
    # Title defaults to the filename stem when no manifest overrides it.
    titles = {r.title for r in results}
    assert titles == {"Backend Engineer", "Frontend Engineer", "Data Scientist"}


@pytest.mark.asyncio
async def test_manifest_title_overrides_filename_stem() -> None:
    conn = _mock_conn()
    files = [("backend.txt", _txt())]
    manifest = {"backend.txt": JobManifestRow(title="Staff Backend Engineer")}
    results = await job_service.create_jobs_bulk(
        conn, files=files, manifest=manifest, created_by="api"
    )
    assert len(results) == 1
    assert results[0].outcome == "created"
    assert results[0].title == "Staff Backend Engineer"
    # The INSERT was called with the manifest title as the first bound arg
    # (args[0] is the SQL string; args[1] is ``title``).
    assert conn.fetchrow.await_args.args[1] == "Staff Backend Engineer"


@pytest.mark.asyncio
async def test_short_jd_fails_without_aborting_the_batch() -> None:
    conn = _mock_conn()
    files = [
        ("good.txt", _txt("A perfectly long job description. " + _JD_TEXT)),
        ("tiny.txt", _txt("too short")),  # < 50 chars
        ("also_good.txt", _txt("Another long enough description. " + _JD_TEXT)),
    ]
    results = await job_service.create_jobs_bulk(
        conn, files=files, manifest=None, created_by="api"
    )
    assert [r.outcome for r in results] == ["created", "failed", "created"]
    failed = results[1]
    assert failed.original_filename == "tiny.txt"
    assert failed.job_id is None
    assert failed.reason
    # Only the two good files were inserted.
    assert conn.fetchrow.await_count == 2


@pytest.mark.asyncio
async def test_byte_identical_jd_twice_second_is_duplicate() -> None:
    conn = _mock_conn()
    identical = _txt("An identical job description body. " + _JD_TEXT)
    files = [("first.txt", identical), ("second.txt", identical)]
    results = await job_service.create_jobs_bulk(
        conn, files=files, manifest=None, created_by="api"
    )
    assert results[0].outcome == "created"
    assert results[1].outcome == "duplicate"
    # The duplicate points back at the first created job's id.
    assert results[1].job_id == results[0].job_id
    # Only ONE INSERT happened.
    assert conn.fetchrow.await_count == 1


@pytest.mark.asyncio
async def test_existing_sha_in_db_is_a_duplicate() -> None:
    existing_id = uuid4()
    conn = MagicMock(name="conn")
    conn.fetchval = AsyncMock(return_value=existing_id)
    conn.fetchrow = AsyncMock()
    results = await job_service.create_jobs_bulk(
        conn, files=[("dupe.txt", _txt())], manifest=None, created_by="api"
    )
    assert results[0].outcome == "duplicate"
    assert results[0].job_id == existing_id
    conn.fetchrow.assert_not_called()


@pytest.mark.asyncio
async def test_unreadable_file_fails_without_aborting_the_batch() -> None:
    conn = _mock_conn()
    files = [
        ("good.txt", _txt("A long enough description body. " + _JD_TEXT)),
        ("empty.txt", b""),  # extract raises → failed row
    ]
    results = await job_service.create_jobs_bulk(
        conn, files=files, manifest=None, created_by="api"
    )
    assert results[0].outcome == "created"
    assert results[1].outcome == "failed"
    assert results[1].reason
