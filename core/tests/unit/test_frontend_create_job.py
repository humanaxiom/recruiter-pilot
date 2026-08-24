"""Slice S2 — create a job.

Covers ``api_client.create_job`` / ``api_client.extract_jd`` (via
``httpx.MockTransport``) and the Flask ``GET /`` form, ``POST /jobs/jd-extract``
proxy and ``POST /jobs`` create route. The 422 (validation) path maps to a
typed ``api_client.BadRequest`` the route catches to re-render the form with
the recruiter's inputs intact — no data loss.
"""

from __future__ import annotations

import io
from collections.abc import Callable
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


# ── api_client.create_job ────────────────────────────────────────────────


def test_create_job_posts_payload_and_returns_json() -> None:
    job_id = uuid4()
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = request.url
        captured["method"] = request.method
        captured["body"] = request.content
        return httpx.Response(201, json={"id": str(job_id), "title": "Backend Eng"})

    result = api_client.create_job(
        {"title": "Backend Eng", "description_raw": "x" * 60},
        client=_client_with(handler),
    )
    assert captured["method"] == "POST"
    assert captured["url"].path == "/jobs"
    assert result == {"id": str(job_id), "title": "Backend Eng"}
    assert b"Backend Eng" in captured["body"]


def test_create_job_raises_bad_request_on_422() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"detail": "description_raw too short"})

    with pytest.raises(api_client.BadRequest) as exc:
        api_client.create_job({"title": "x"}, client=_client_with(handler))
    assert exc.value.status_code == 422
    assert exc.value.detail == {"detail": "description_raw too short"}


def test_bad_request_is_a_backend_error() -> None:
    assert issubclass(api_client.BadRequest, api_client.BackendError)


def test_create_job_raises_backend_unavailable_on_500() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "boom"})

    with pytest.raises(api_client.BackendUnavailable):
        api_client.create_job({"title": "x"}, client=_client_with(handler))


# ── api_client.extract_jd ────────────────────────────────────────────────


def test_extract_jd_posts_multipart_and_returns_json() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = request.url
        captured["method"] = request.method
        captured["content_type"] = request.headers.get("content-type", "")
        captured["body"] = request.content
        return httpx.Response(
            200, json={"filename": "jd.pdf", "text": "Parsed JD body", "chars": 14}
        )

    result = api_client.extract_jd(
        "jd.pdf", b"%PDF-1.4 fake", "application/pdf", client=_client_with(handler)
    )
    assert captured["method"] == "POST"
    assert captured["url"].path == "/jobs/jd-extract"
    assert "multipart/form-data" in captured["content_type"]
    assert result["text"] == "Parsed JD body"


def test_extract_jd_raises_bad_request_on_422() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"detail": "unsupported file type"})

    with pytest.raises(api_client.BadRequest):
        api_client.extract_jd(
            "jd.exe", b"MZ", "application/octet-stream", client=_client_with(handler)
        )


# ── GET / — the New job form + status filters ────────────────────────────


def test_index_renders_the_new_job_form(monkeypatch: Any, client: Any) -> None:
    monkeypatch.setattr(api_client, "list_jobs", MagicMock(return_value=[]))
    resp = client.get("/")
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert 'action="/jobs"' in body
    assert 'method="post"' in body.lower()
    assert 'id="description"' in body
    assert 'hx-post="/jobs/jd-extract"' in body


def test_index_status_filter_forwards_to_list_jobs(
    monkeypatch: Any, client: Any
) -> None:
    spy = MagicMock(return_value=[])
    monkeypatch.setattr(api_client, "list_jobs", spy)
    client.get("/?status=open")
    assert spy.call_args.kwargs.get("status") == "open"


def test_index_status_filter_absent_means_none(monkeypatch: Any, client: Any) -> None:
    spy = MagicMock(return_value=[])
    monkeypatch.setattr(api_client, "list_jobs", spy)
    client.get("/")
    assert spy.call_args.kwargs.get("status") is None


def test_index_renders_status_filter_pills(monkeypatch: Any, client: Any) -> None:
    monkeypatch.setattr(api_client, "list_jobs", MagicMock(return_value=[]))
    body = client.get("/").get_data(as_text=True)
    for status in ("draft", "open", "closed", "archived"):
        assert f"status={status}" in body


# ── POST /jobs/jd-extract — proxy prefill ────────────────────────────────


def test_jd_extract_proxy_returns_prefill_text(monkeypatch: Any, client: Any) -> None:
    monkeypatch.setattr(
        api_client,
        "extract_jd",
        MagicMock(
            return_value={
                "filename": "jd.pdf",
                "text": "Prefilled JD text",
                "chars": 17,
            }
        ),
    )
    resp = client.post(
        "/jobs/jd-extract",
        data={"file": (io.BytesIO(b"%PDF fake"), "jd.pdf")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200
    assert b"Prefilled JD text" in resp.data


def test_jd_extract_proxy_forwards_filename_and_bytes(
    monkeypatch: Any, client: Any
) -> None:
    spy = MagicMock(return_value={"filename": "jd.pdf", "text": "t", "chars": 1})
    monkeypatch.setattr(api_client, "extract_jd", spy)
    client.post(
        "/jobs/jd-extract",
        data={"file": (io.BytesIO(b"RAWBYTES"), "jd.pdf")},
        content_type="multipart/form-data",
    )
    spy.assert_called_once()
    args = spy.call_args.args
    assert args[0] == "jd.pdf"
    assert args[1] == b"RAWBYTES"


# ── POST /jobs — create success + validation re-render ────────────────────


def test_post_jobs_success_redirects_to_job_detail(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    monkeypatch.setattr(
        api_client,
        "create_job",
        MagicMock(return_value={"id": str(job_id), "title": "Backend Eng"}),
    )
    resp = client.post(
        "/jobs",
        data={
            "title": "Backend Eng",
            "department": "Engineering",
            "location": "Remote",
            "min_years": "5",
            "description_raw": "x" * 80,
            "blind_review": "on",
        },
    )
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith(f"/jobs/{job_id}")


def test_post_jobs_builds_expected_payload(monkeypatch: Any, client: Any) -> None:
    spy = MagicMock(return_value={"id": str(uuid4())})
    monkeypatch.setattr(api_client, "create_job", spy)
    client.post(
        "/jobs",
        data={
            "title": "Backend Eng",
            "department": "Engineering",
            "location": "Remote",
            "min_years": "5",
            "description_raw": "x" * 80,
            "blind_review": "on",
        },
    )
    payload = spy.call_args.args[0]
    assert payload["title"] == "Backend Eng"
    assert payload["department"] == "Engineering"
    assert payload["location"] == "Remote"
    assert payload["min_years"] == 5
    assert payload["description_raw"] == "x" * 80
    assert payload["blind_review"] is True


def test_post_jobs_unchecked_blind_review_is_false(
    monkeypatch: Any, client: Any
) -> None:
    spy = MagicMock(return_value={"id": str(uuid4())})
    monkeypatch.setattr(api_client, "create_job", spy)
    client.post(
        "/jobs",
        data={"title": "T", "description_raw": "x" * 80},
    )
    payload = spy.call_args.args[0]
    assert payload["blind_review"] is False


def test_post_jobs_422_rerenders_form_with_inputs_preserved(
    monkeypatch: Any, client: Any
) -> None:
    monkeypatch.setattr(
        api_client,
        "create_job",
        MagicMock(
            side_effect=api_client.BadRequest(
                "bad", status_code=422, detail={"detail": "description_raw too short"}
            )
        ),
    )
    monkeypatch.setattr(api_client, "list_jobs", MagicMock(return_value=[]))
    resp = client.post(
        "/jobs",
        data={
            "title": "My Distinctive Title",
            "department": "Platform",
            "description_raw": "short",
        },
    )
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    # inputs preserved
    assert "My Distinctive Title" in body
    assert "Platform" in body
    assert "short" in body
    # an error surfaced
    assert "too short" in body or "error" in body.lower()


def test_post_jobs_422_does_not_500(monkeypatch: Any, client: Any) -> None:
    monkeypatch.setattr(
        api_client,
        "create_job",
        MagicMock(
            side_effect=api_client.BadRequest("bad", status_code=422, detail=None)
        ),
    )
    monkeypatch.setattr(api_client, "list_jobs", MagicMock(return_value=[]))
    resp = client.post("/jobs", data={"title": "T", "description_raw": "short"})
    assert resp.status_code != 500
