"""FU-3 Slice 4 — bulk JD upload from the Flask viewer.

Covers ``api_client.bulk_create_jobs`` (multipart shape via
``httpx.MockTransport``: repeated ``files=`` parts + an optional ``manifest``
part) and the Flask ``POST /jobs/bulk`` route (forwards the multi-file/zip input
and the optional CSV manifest, then renders a created/duplicate/failed summary
with a link back to the jobs list). RED half of the TDD cycle.
"""

from __future__ import annotations

from collections.abc import Callable
from io import BytesIO
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import httpx
import pytest

from frontend import api_client


@pytest.fixture
def client(csrf_client: Any) -> Any:
    """The shared CSRF-carrying browser client (Phase 1.3).

    These tests exercise the route's BUSINESS logic and predate the
    anti-forgery guard; they now present a page token the way a real browser
    does, rather than the guard being relaxed for them. See
    ``tests/unit/conftest.py`` for why this is not autouse."""
    return csrf_client


def _client_with(
    handler: Callable[[httpx.Request], httpx.Response],
) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test")


# ── api_client.bulk_create_jobs ──────────────────────────────────────────


def test_bulk_create_jobs_posts_repeated_file_parts() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = request.url
        captured["method"] = request.method
        captured["content_type"] = request.headers.get("content-type", "")
        captured["body"] = request.content
        return httpx.Response(
            202,
            json=[
                {"original_filename": "a.txt", "outcome": "created", "title": "A"},
                {"original_filename": "b.txt", "outcome": "created", "title": "B"},
            ],
        )

    result = api_client.bulk_create_jobs(
        [("a.txt", b"JD one", "text/plain"), ("b.txt", b"JD two", "text/plain")],
        client=_client_with(handler),
    )
    assert captured["method"] == "POST"
    assert captured["url"].path == "/jobs/bulk"
    assert "multipart/form-data" in captured["content_type"]
    # Both file parts are present in the multipart body.
    assert b"a.txt" in captured["body"]
    assert b"b.txt" in captured["body"]
    assert len(result) == 2


def test_bulk_create_jobs_includes_manifest_part_when_given() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content
        return httpx.Response(202, json=[])

    api_client.bulk_create_jobs(
        [("a.txt", b"JD one", "text/plain")],
        manifest=("manifest.csv", b"filename,title\na.txt,A\n", "text/csv"),
        client=_client_with(handler),
    )
    assert b"manifest.csv" in captured["body"]
    assert b'name="manifest"' in captured["body"]


def test_bulk_create_jobs_omits_manifest_part_when_absent() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content
        return httpx.Response(202, json=[])

    api_client.bulk_create_jobs(
        [("a.txt", b"JD one", "text/plain")], client=_client_with(handler)
    )
    assert b'name="manifest"' not in captured["body"]


def test_bulk_create_jobs_raises_bad_request_on_422() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"detail": "bad manifest"})

    with pytest.raises(api_client.BadRequest):
        api_client.bulk_create_jobs(
            [("a.txt", b"x", "text/plain")], client=_client_with(handler)
        )


# ── index bulk-upload form ───────────────────────────────────────────────


def test_index_renders_the_bulk_jd_upload_form(monkeypatch: Any, client: Any) -> None:
    monkeypatch.setattr(api_client, "list_jobs", MagicMock(return_value=[]))
    body = client.get("/").get_data(as_text=True)
    assert 'action="/jobs/bulk"' in body
    # a multi-file/zip input and an optional CSV manifest input.
    assert 'name="files"' in body
    assert "multiple" in body
    assert 'name="manifest"' in body


# ── Flask POST /jobs/bulk ────────────────────────────────────────────────


def test_bulk_route_forwards_files_and_manifest(monkeypatch: Any, client: Any) -> None:
    spy = MagicMock(
        return_value=[
            {"original_filename": "a.txt", "outcome": "created", "title": "A"},
        ]
    )
    monkeypatch.setattr(api_client, "bulk_create_jobs", spy)
    resp = client.post(
        "/jobs/bulk",
        data={
            "files": [
                (BytesIO(b"JD one body"), "a.txt"),
                (BytesIO(b"JD two body"), "b.txt"),
            ],
            "manifest": (BytesIO(b"filename,title\na.txt,A\n"), "manifest.csv"),
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200
    spy.assert_called_once()
    forwarded_files = spy.call_args.args[0]
    assert {f[0] for f in forwarded_files} == {"a.txt", "b.txt"}
    # The manifest is forwarded as the keyword arg.
    manifest = spy.call_args.kwargs.get("manifest")
    assert manifest is not None
    assert manifest[0] == "manifest.csv"


def test_bulk_route_omits_manifest_when_not_uploaded(
    monkeypatch: Any, client: Any
) -> None:
    spy = MagicMock(return_value=[])
    monkeypatch.setattr(api_client, "bulk_create_jobs", spy)
    client.post(
        "/jobs/bulk",
        data={"files": [(BytesIO(b"JD one body"), "a.txt")]},
        content_type="multipart/form-data",
    )
    assert spy.call_args.kwargs.get("manifest") is None


def test_bulk_route_renders_created_duplicate_failed_summary(
    monkeypatch: Any, client: Any
) -> None:
    monkeypatch.setattr(
        api_client,
        "bulk_create_jobs",
        MagicMock(
            return_value=[
                {
                    "original_filename": "a.txt",
                    "outcome": "created",
                    "job_id": str(uuid4()),
                    "title": "A",
                },
                {"original_filename": "b.txt", "outcome": "duplicate", "title": "B"},
                {
                    "original_filename": "c.txt",
                    "outcome": "failed",
                    "reason": "too short",
                },
            ]
        ),
    )
    resp = client.post(
        "/jobs/bulk",
        data={"files": [(BytesIO(b"x"), "a.txt")]},
        content_type="multipart/form-data",
    )
    body = resp.get_data(as_text=True)
    assert "1 created" in body
    assert "1 duplicate" in body
    assert "1 failed" in body
    # A link back to the jobs list.
    assert 'href="/"' in body


def test_bulk_route_maps_bad_request_without_500(monkeypatch: Any, client: Any) -> None:
    monkeypatch.setattr(
        api_client,
        "bulk_create_jobs",
        MagicMock(
            side_effect=api_client.BadRequest(
                "bad", status_code=422, detail={"detail": "missing filename column"}
            )
        ),
    )
    resp = client.post(
        "/jobs/bulk",
        data={"files": [(BytesIO(b"x"), "a.txt")]},
        content_type="multipart/form-data",
    )
    assert resp.status_code != 500
