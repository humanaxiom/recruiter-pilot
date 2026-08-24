"""Slice S4 — résumé upload + polling table.

Covers ``api_client.upload_resumes`` (multipart shape via ``httpx.MockTransport``:
the ``files=`` parts, the ``consent_acknowledged`` form field as ``true``/
``false``, the optional ``cover_letter_text``, and a backend 4xx mapped to the
typed ``BadRequest``) and the Flask routes: the mandatory-consent upload POST
(unchecked consent → the backend is NEVER called and the page re-renders with an
error) and the HTMX ``resumes-table`` poll fragment (keeps its ``hx-trigger``
while any résumé row is still ``uploaded``/``parsing`` and DROPS it once every
row is terminal ``parsed``/``failed``). Includes a blind-boundary byte-scan:
a planted fake name/email/phone must be absent from the rendered table.

Also covers the upload-UX fix: an empty/misdirected file selection (nothing
attached to the ``files`` input, or a part with an empty filename) must be
caught by the ROUTE itself — before ``api_client.upload_resumes`` is ever
called — with a friendly re-render, never a raw pydantic 422 list forwarded
to and echoed by the page; and ``_format_error`` must turn a raw pydantic
error-list ``detail`` into readable text in general (not just for this one
route). All of this is pure Flask/route/formatting logic over a MOCKED
``api_client`` — no real backend or database involved — so the offline unit
suite is the correct (and sufficient) place for it; no integration test is
needed for this diff.
"""

from __future__ import annotations

import html
import zipfile
from collections.abc import Callable
from io import BytesIO
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import httpx
import pytest

from frontend import api_client
from frontend.app import _format_error

_REAL_NAME = "Zzyzxqrst Wibblesworth"
_REAL_EMAIL = "zzyzxqrst.wibblesworth@example.test"
_REAL_PHONE = "604-555-0192"


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


def _json_handler(
    status_code: int, body: Any
) -> Callable[[httpx.Request], httpx.Response]:
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=body)

    return _handler


def _job(job_id: Any, *, status: str = "open") -> dict[str, Any]:
    return {
        "id": str(job_id),
        "title": "Senior Backend Engineer",
        "department": "Engineering",
        "location": "Remote",
        "min_years": 5,
        "status": status,
        "blind_review": True,
        "parsed_at": "2026-07-17T00:00:00Z",
        "description_parsed": {"required_skills": []},
    }


def _resume(
    *,
    status: str = "parsing",
    candidate_name: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": str(uuid4()),
        "original_filename": "resume.pdf",
        "status": status,
        "uploaded_at": "2026-07-17T00:00:00Z",
        "parsed_at": None,
        "candidate_name": candidate_name,
        "has_cover_letter": False,
    }
    row.update(extra)
    return row


def _fake_resume_zip() -> bytes:
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.writestr(zipfile.ZipInfo(filename="a.pdf"), b"%PDF-1.4 fake pdf bytes")
        zf.writestr(zipfile.ZipInfo(filename="b.pdf"), b"%PDF-1.4 fake pdf bytes 2")
    return buf.getvalue()


# ── api_client.upload_resumes — multipart shape ──────────────────────────


def test_upload_resumes_posts_multipart_to_the_resumes_path() -> None:
    job_id = uuid4()
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = request.url
        captured["content_type"] = request.headers.get("content-type", "")
        captured["body"] = request.content
        return httpx.Response(
            202,
            json=[
                {
                    "original_filename": "a.pdf",
                    "outcome": "accepted",
                    "resume_id": str(uuid4()),
                    "reason": None,
                    "cover_letter_filename": None,
                    "warnings": [],
                }
            ],
        )

    result = api_client.upload_resumes(
        job_id,
        [("a.pdf", b"%PDF-1.4 body-bytes", "application/pdf")],
        consent_acknowledged=True,
        client=_client_with(handler),
    )
    assert captured["method"] == "POST"
    assert captured["url"].path == f"/jobs/{job_id}/resumes"
    assert "multipart/form-data" in captured["content_type"]
    body = captured["body"]
    assert b"a.pdf" in body
    assert b"%PDF-1.4 body-bytes" in body
    assert b"consent_acknowledged" in body
    assert b"true" in body
    assert result[0]["outcome"] == "accepted"


def test_upload_resumes_sends_consent_false_when_not_acknowledged() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content
        return httpx.Response(202, json=[])

    api_client.upload_resumes(
        uuid4(),
        [("a.pdf", b"x", "application/pdf")],
        consent_acknowledged=False,
        client=_client_with(handler),
    )
    assert b"consent_acknowledged" in captured["body"]
    assert b"false" in captured["body"]


def test_upload_resumes_forwards_multiple_files() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content
        return httpx.Response(202, json=[])

    api_client.upload_resumes(
        uuid4(),
        [
            ("a.pdf", b"aaaa", "application/pdf"),
            ("b.docx", b"bbbb", "application/octet-stream"),
        ],
        consent_acknowledged=True,
        client=_client_with(handler),
    )
    body = captured["body"]
    assert b"a.pdf" in body
    assert b"b.docx" in body
    assert b"aaaa" in body
    assert b"bbbb" in body


def test_upload_resumes_includes_cover_letter_text_when_given() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content
        return httpx.Response(202, json=[])

    api_client.upload_resumes(
        uuid4(),
        [("a.pdf", b"x", "application/pdf")],
        consent_acknowledged=True,
        cover_letter_text="Dear hiring manager, please consider me.",
        client=_client_with(handler),
    )
    body = captured["body"]
    assert b"cover_letter_text" in body
    assert b"Dear hiring manager" in body


def test_upload_resumes_omits_cover_letter_when_none() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content
        return httpx.Response(202, json=[])

    api_client.upload_resumes(
        uuid4(),
        [("a.pdf", b"x", "application/pdf")],
        consent_acknowledged=True,
        client=_client_with(handler),
    )
    assert b"cover_letter_text" not in captured["body"]


def test_upload_resumes_forwards_cover_letter_file_as_a_multipart_part() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content
        return httpx.Response(202, json=[])

    api_client.upload_resumes(
        uuid4(),
        [("a.pdf", b"resume-bytes", "application/pdf")],
        consent_acknowledged=True,
        cover_letter_file=("cover.pdf", b"cover-letter-bytes", "application/pdf"),
        client=_client_with(handler),
    )
    body = captured["body"]
    assert b"cover_letter_file" in body
    assert b"cover.pdf" in body
    assert b"cover-letter-bytes" in body


def test_upload_resumes_omits_cover_letter_file_when_none() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content
        return httpx.Response(202, json=[])

    api_client.upload_resumes(
        uuid4(),
        [("a.pdf", b"x", "application/pdf")],
        consent_acknowledged=True,
        client=_client_with(handler),
    )
    assert b"cover_letter_file" not in captured["body"]


def test_upload_resumes_forwards_pairing_manifest_as_a_multipart_part() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content
        return httpx.Response(202, json=[])

    api_client.upload_resumes(
        uuid4(),
        [("a.pdf", b"resume-bytes", "application/pdf")],
        consent_acknowledged=True,
        pairing_manifest=("manifest.json", b'{"applicants": []}', "application/json"),
        client=_client_with(handler),
    )
    body = captured["body"]
    assert b"pairing_manifest" in body
    assert b"manifest.json" in body
    assert b'{"applicants": []}' in body


def test_upload_resumes_omits_pairing_manifest_when_none() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content
        return httpx.Response(202, json=[])

    api_client.upload_resumes(
        uuid4(),
        [("a.pdf", b"x", "application/pdf")],
        consent_acknowledged=True,
        client=_client_with(handler),
    )
    assert b"pairing_manifest" not in captured["body"]


def test_upload_resumes_maps_backend_4xx_to_bad_request() -> None:
    client = _client_with(_json_handler(422, {"detail": "unsupported file"}))
    with pytest.raises(api_client.BadRequest):
        api_client.upload_resumes(
            uuid4(),
            [("a.pdf", b"x", "application/pdf")],
            consent_acknowledged=True,
            client=client,
        )


def test_upload_resumes_maps_5xx_to_backend_unavailable() -> None:
    client = _client_with(_json_handler(500, {"detail": "boom"}))
    with pytest.raises(api_client.BackendUnavailable):
        api_client.upload_resumes(
            uuid4(),
            [("a.pdf", b"x", "application/pdf")],
            consent_acknowledged=True,
            client=client,
        )


# ── POST /jobs/<id>/resumes — mandatory consent ──────────────────────────


def test_upload_route_without_consent_never_calls_backend_and_shows_error(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    spy = MagicMock()
    monkeypatch.setattr(api_client, "upload_resumes", spy)
    monkeypatch.setattr(api_client, "get_job", MagicMock(return_value=_job(job_id)))
    monkeypatch.setattr(api_client, "list_resumes", MagicMock(return_value=[]))
    resp = client.post(
        f"/jobs/{job_id}/resumes",
        data={"files": (BytesIO(b"pdf-bytes"), "a.pdf")},
        content_type="multipart/form-data",
    )
    spy.assert_not_called()
    assert resp.status_code != 500
    assert b"consent" in resp.data.lower()


def test_upload_route_with_consent_calls_backend_and_redirects(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    spy = MagicMock(
        return_value=[{"original_filename": "a.pdf", "outcome": "accepted"}]
    )
    monkeypatch.setattr(api_client, "upload_resumes", spy)
    resp = client.post(
        f"/jobs/{job_id}/resumes",
        data={
            "consent_acknowledged": "true",
            "files": (BytesIO(b"pdf-bytes"), "a.pdf"),
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith(f"/jobs/{job_id}")
    spy.assert_called_once()
    assert spy.call_args.kwargs["consent_acknowledged"] is True


def test_upload_route_renders_results_summary_after_redirect(
    monkeypatch: Any, client: Any
) -> None:
    """The upload route surfaces a post-upload results summary (flash-style)
    built from the returned ``ResumeUploadResult`` rows — visible on the
    job-detail page the upload redirects to."""
    job_id = uuid4()
    monkeypatch.setattr(
        api_client,
        "upload_resumes",
        MagicMock(
            return_value=[
                {
                    "original_filename": "jane_resume.pdf",
                    "outcome": "accepted",
                    "cover_letter_filename": "jane_cover_letter.pdf",
                    "warnings": [],
                },
                {
                    "original_filename": "bob_resume.pdf",
                    "outcome": "accepted",
                    "cover_letter_filename": None,
                    "warnings": [],
                },
                {
                    "original_filename": "dup.pdf",
                    "outcome": "duplicate",
                    "warnings": [],
                },
            ]
        ),
    )
    monkeypatch.setattr(api_client, "get_job", MagicMock(return_value=_job(job_id)))
    monkeypatch.setattr(api_client, "list_resumes", MagicMock(return_value=[]))
    resp = client.post(
        f"/jobs/{job_id}/resumes",
        data={
            "consent_acknowledged": "true",
            "files": (BytesIO(b"pdf"), "jane_resume.pdf"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    # Summary counts (accepted / with-cover / duplicate) appear.
    assert "2 accepted" in body
    assert "1 with a cover letter" in body
    assert "1 duplicate" in body


def test_upload_route_surfaces_pairing_warnings(monkeypatch: Any, client: Any) -> None:
    job_id = uuid4()
    warning = "looked like a cover letter but had no matching résumé"
    monkeypatch.setattr(
        api_client,
        "upload_resumes",
        MagicMock(
            return_value=[
                {
                    "original_filename": "stray_cover.pdf",
                    "outcome": "accepted",
                    "cover_letter_filename": None,
                    "warnings": [warning],
                }
            ]
        ),
    )
    monkeypatch.setattr(api_client, "get_job", MagicMock(return_value=_job(job_id)))
    monkeypatch.setattr(api_client, "list_resumes", MagicMock(return_value=[]))
    resp = client.post(
        f"/jobs/{job_id}/resumes",
        data={
            "consent_acknowledged": "true",
            "files": (BytesIO(b"pdf"), "stray_cover.pdf"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert warning in resp.get_data(as_text=True)


def test_upload_route_forwards_cover_letter_text(monkeypatch: Any, client: Any) -> None:
    job_id = uuid4()
    spy = MagicMock(return_value=[])
    monkeypatch.setattr(api_client, "upload_resumes", spy)
    client.post(
        f"/jobs/{job_id}/resumes",
        data={
            "consent_acknowledged": "true",
            "cover_letter_text": "A short letter.",
            "files": (BytesIO(b"pdf"), "a.pdf"),
        },
        content_type="multipart/form-data",
    )
    assert spy.call_args.kwargs["cover_letter_text"] == "A short letter."


def test_upload_route_forwards_cover_letter_file(monkeypatch: Any, client: Any) -> None:
    job_id = uuid4()
    spy = MagicMock(return_value=[])
    monkeypatch.setattr(api_client, "upload_resumes", spy)
    client.post(
        f"/jobs/{job_id}/resumes",
        data={
            "consent_acknowledged": "true",
            "files": (BytesIO(b"pdf"), "a.pdf"),
            "cover_letter_file": (BytesIO(b"cover-bytes"), "cover.pdf"),
        },
        content_type="multipart/form-data",
    )
    cover = spy.call_args.kwargs["cover_letter_file"]
    assert cover is not None
    assert cover[0] == "cover.pdf"
    assert cover[1] == b"cover-bytes"


def test_upload_route_forwards_pairing_manifest(monkeypatch: Any, client: Any) -> None:
    job_id = uuid4()
    spy = MagicMock(return_value=[])
    monkeypatch.setattr(api_client, "upload_resumes", spy)
    client.post(
        f"/jobs/{job_id}/resumes",
        data={
            "consent_acknowledged": "true",
            "files": (BytesIO(b"pdf"), "a.pdf"),
            "pairing_manifest": (BytesIO(b'{"applicants": []}'), "manifest.json"),
        },
        content_type="multipart/form-data",
    )
    manifest = spy.call_args.kwargs["pairing_manifest"]
    assert manifest is not None
    assert manifest[0] == "manifest.json"
    assert manifest[1] == b'{"applicants": []}'


def test_upload_route_pairing_manifest_none_when_absent(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    spy = MagicMock(return_value=[])
    monkeypatch.setattr(api_client, "upload_resumes", spy)
    client.post(
        f"/jobs/{job_id}/resumes",
        data={
            "consent_acknowledged": "true",
            "files": (BytesIO(b"pdf"), "a.pdf"),
        },
        content_type="multipart/form-data",
    )
    assert spy.call_args.kwargs["pairing_manifest"] is None


def test_upload_route_cover_letter_file_none_when_absent(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    spy = MagicMock(return_value=[])
    monkeypatch.setattr(api_client, "upload_resumes", spy)
    client.post(
        f"/jobs/{job_id}/resumes",
        data={
            "consent_acknowledged": "true",
            "files": (BytesIO(b"pdf"), "a.pdf"),
        },
        content_type="multipart/form-data",
    )
    assert spy.call_args.kwargs["cover_letter_file"] is None


def test_upload_route_caps_cover_letter_length_before_network(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    spy = MagicMock(return_value=[])
    monkeypatch.setattr(api_client, "upload_resumes", spy)
    huge = "x" * 100_000
    client.post(
        f"/jobs/{job_id}/resumes",
        data={
            "consent_acknowledged": "true",
            "cover_letter_text": huge,
            "files": (BytesIO(b"pdf"), "a.pdf"),
        },
        content_type="multipart/form-data",
    )
    sent = spy.call_args.kwargs["cover_letter_text"]
    assert sent is not None
    assert len(sent) < len(huge)


def test_upload_route_backend_unavailable_is_not_a_500(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    monkeypatch.setattr(
        api_client,
        "upload_resumes",
        MagicMock(side_effect=api_client.BackendUnavailable("down")),
    )
    resp = client.post(
        f"/jobs/{job_id}/resumes",
        data={
            "consent_acknowledged": "true",
            "files": (BytesIO(b"pdf"), "a.pdf"),
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code in (502, 503)
    assert resp.status_code != 500


# ── POST /jobs/<id>/resumes — empty/misdirected file-selection guard ────
#
# Diagnosed against the live app: submitting the form with consent ticked but
# nothing (or an empty part) attached to the ``files`` input forwards an
# EMPTY list to ``api_client.upload_resumes``. The backend then 422s with a
# raw pydantic error body, which — pre-fix — the frontend renders VERBATIM.
# The fix is a route-level guard, mirroring the existing consent guard: if
# ``files`` ends up empty, never call the backend at all.


def test_upload_route_with_no_files_part_never_calls_backend_and_shows_friendly_error(
    monkeypatch: Any, client: Any
) -> None:
    """Consent is ticked but the ``files`` input has nothing attached at all
    (no ``files`` part in the multipart body whatsoever) — e.g. the recruiter
    forgot to select a résumé, or attached it under one of the form's OTHER
    file inputs (cover letter / pairing manifest) by mistake."""
    job_id = uuid4()
    spy = MagicMock()
    monkeypatch.setattr(api_client, "upload_resumes", spy)
    monkeypatch.setattr(api_client, "get_job", MagicMock(return_value=_job(job_id)))
    monkeypatch.setattr(api_client, "list_resumes", MagicMock(return_value=[]))
    resp = client.post(
        f"/jobs/{job_id}/resumes",
        data={"consent_acknowledged": "true"},
        content_type="multipart/form-data",
    )
    spy.assert_not_called()
    assert resp.status_code == 400
    body = resp.get_data(as_text=True).lower()
    assert "select at least one" in body
    assert "résumé" in body or "resume" in body
    # And, critically, no raw pydantic error leaks through:
    assert "{'type'" not in body
    assert "'loc'" not in body


def test_upload_route_with_empty_filename_file_never_calls_backend(
    monkeypatch: Any, client: Any
) -> None:
    """A ``files`` part IS present but carries an empty filename (the
    existing ``if upload.filename`` filter in the route drops it, yielding
    the same empty list as the no-part case above) — the guard must fire
    here too."""
    job_id = uuid4()
    spy = MagicMock()
    monkeypatch.setattr(api_client, "upload_resumes", spy)
    monkeypatch.setattr(api_client, "get_job", MagicMock(return_value=_job(job_id)))
    monkeypatch.setattr(api_client, "list_resumes", MagicMock(return_value=[]))
    resp = client.post(
        f"/jobs/{job_id}/resumes",
        data={
            "consent_acknowledged": "true",
            "files": (BytesIO(b""), ""),
        },
        content_type="multipart/form-data",
    )
    spy.assert_not_called()
    assert resp.status_code == 400
    assert "select at least one" in resp.get_data(as_text=True).lower()


def test_upload_route_with_a_real_file_still_calls_backend(
    monkeypatch: Any, client: Any
) -> None:
    """The new guard must NOT false-trip on a normal, well-formed upload —
    a single real résumé file must still reach ``api_client.upload_resumes``
    exactly as before."""
    job_id = uuid4()
    spy = MagicMock(
        return_value=[{"original_filename": "a.pdf", "outcome": "accepted"}]
    )
    monkeypatch.setattr(api_client, "upload_resumes", spy)
    resp = client.post(
        f"/jobs/{job_id}/resumes",
        data={
            "consent_acknowledged": "true",
            "files": (BytesIO(b"%PDF-1.4 fake pdf bytes"), "a.pdf"),
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 302
    spy.assert_called_once()


def test_upload_route_with_a_zip_file_still_calls_backend(
    monkeypatch: Any, client: Any
) -> None:
    """Nor may the guard break the documented ``.zip`` batch-upload path —
    the backend expands a ``.zip`` server-side; the route only ever forwards
    the raw bytes as a single ``files=`` part, and the guard's emptiness
    check must not mistake a non-empty zip part for an empty selection."""
    job_id = uuid4()
    spy = MagicMock(
        return_value=[
            {"original_filename": "a.pdf", "outcome": "accepted"},
            {"original_filename": "b.pdf", "outcome": "accepted"},
        ]
    )
    monkeypatch.setattr(api_client, "upload_resumes", spy)
    resp = client.post(
        f"/jobs/{job_id}/resumes",
        data={
            "consent_acknowledged": "true",
            "files": (BytesIO(_fake_resume_zip()), "batch.zip"),
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 302
    spy.assert_called_once()
    forwarded_files = spy.call_args.args[1]
    assert forwarded_files[0][0] == "batch.zip"


def test_upload_route_translates_raw_pydantic_422_detail_to_friendly_text(
    monkeypatch: Any, client: Any
) -> None:
    """End-to-end through the route: if the backend still returns a 422 with
    a raw pydantic-style error-list ``detail`` (e.g. some validation path the
    empty-files guard above does not cover), the recruiter must see readable
    text — never the raw ``[{'type': ...}]`` repr, whether checked as literal
    text or (since Jinja autoescapes single quotes to ``&#39;``) after
    HTML-unescaping the rendered body."""
    job_id = uuid4()
    pydantic_detail = [
        {
            "type": "missing",
            "loc": ["body", "files"],
            "msg": "Field required",
            "input": None,
        }
    ]
    monkeypatch.setattr(
        api_client,
        "upload_resumes",
        MagicMock(
            side_effect=api_client.BadRequest(
                "backend 422", status_code=422, detail=pydantic_detail
            )
        ),
    )
    monkeypatch.setattr(api_client, "get_job", MagicMock(return_value=_job(job_id)))
    monkeypatch.setattr(api_client, "list_resumes", MagicMock(return_value=[]))
    resp = client.post(
        f"/jobs/{job_id}/resumes",
        data={
            "consent_acknowledged": "true",
            "files": (BytesIO(b"%PDF-1.4 fake pdf bytes"), "a.pdf"),
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
    body = resp.get_data(as_text=True)
    unescaped = html.unescape(body)
    assert "Field required" in unescaped
    assert "{'type'" not in unescaped
    assert "'loc'" not in unescaped
    assert "'input'" not in unescaped


# ── _format_error — friendly rendering of backend error details ─────────


def test_format_error_renders_pydantic_style_list_detail_as_readable_text() -> None:
    """A raw FastAPI/pydantic 422 body — ``[{'type': 'missing', 'loc': [...],
    'msg': 'Field required', 'input': None}]`` — must never be shown
    verbatim; it must be translated into readable text (e.g. joining the
    ``msg`` fields)."""
    detail = [
        {
            "type": "missing",
            "loc": ["body", "files"],
            "msg": "Field required",
            "input": None,
        }
    ]
    result = _format_error(detail)
    assert "Field required" in result
    assert "{'type'" not in result
    assert "'loc'" not in result
    assert "'input'" not in result


def test_format_error_joins_multiple_pydantic_errors() -> None:
    detail = [
        {"type": "missing", "loc": ["body", "files"], "msg": "Field required"},
        {
            "type": "value_error",
            "loc": ["body", "consent_acknowledged"],
            "msg": "must be true",
        },
    ]
    result = _format_error(detail)
    assert "Field required" in result
    assert "must be true" in result
    assert "{'type'" not in result


def test_format_error_plain_string_detail_is_unchanged() -> None:
    assert _format_error("unsupported file type") == "unsupported file type"


def test_format_error_none_detail_has_a_sensible_fallback() -> None:
    result = _format_error(None)
    assert result
    assert isinstance(result, str)
    assert "{" not in result


def test_format_error_empty_list_detail_has_a_sensible_fallback() -> None:
    result = _format_error([])
    assert result
    assert isinstance(result, str)
    assert result != "[]"


# ── GET /jobs/<id>/resumes-table — poll fragment ─────────────────────────


def test_resumes_table_keeps_polling_while_a_row_is_unparsed(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    monkeypatch.setattr(
        api_client,
        "list_resumes",
        MagicMock(
            return_value=[
                _resume(status="parsed"),
                _resume(status="parsing"),
            ]
        ),
    )
    resp = client.get(f"/jobs/{job_id}/resumes-table")
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "hx-trigger" in body  # keeps polling
    assert "parsing" in body.lower()


def test_resumes_table_stops_polling_once_every_row_is_terminal(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    monkeypatch.setattr(
        api_client,
        "list_resumes",
        MagicMock(
            return_value=[
                _resume(status="parsed"),
                _resume(status="failed"),
            ]
        ),
    )
    resp = client.get(f"/jobs/{job_id}/resumes-table")
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "hx-trigger" not in body  # polling stopped


def test_resumes_table_shows_degraded_badge_when_row_is_degraded(
    monkeypatch: Any, client: Any
) -> None:
    """FU-7 §4 / ADR-030: the résumé list must flag a degraded row (skills
    extraction fell back to the keyword scan) -- the incident this slice
    fixes was invisible precisely because the list looked complete."""
    job_id = uuid4()
    monkeypatch.setattr(
        api_client,
        "list_resumes",
        MagicMock(
            return_value=[
                _resume(status="parsed", degraded=True),
                _resume(status="parsed", degraded=False),
            ]
        ),
    )
    resp = client.get(f"/jobs/{job_id}/resumes-table")
    body = resp.get_data(as_text=True).lower()
    assert resp.status_code == 200
    assert "degraded" in body


def test_resumes_table_omits_degraded_badge_for_a_clean_parse(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    monkeypatch.setattr(
        api_client,
        "list_resumes",
        MagicMock(return_value=[_resume(status="parsed", degraded=False)]),
    )
    resp = client.get(f"/jobs/{job_id}/resumes-table")
    body = resp.get_data(as_text=True).lower()
    assert resp.status_code == 200
    assert "degraded" not in body


def test_resumes_table_404s_when_job_missing(monkeypatch: Any, client: Any) -> None:
    monkeypatch.setattr(
        api_client,
        "list_resumes",
        MagicMock(side_effect=api_client.NotFound("no job")),
    )
    resp = client.get(f"/jobs/{uuid4()}/resumes-table")
    assert resp.status_code == 404


def test_resumes_table_blind_render_never_contains_raw_pii_bytes(
    monkeypatch: Any, client: Any
) -> None:
    """Blind-boundary byte-scan: a planted real name/email/phone (on fields the
    table must never surface) must be absent from the rendered fragment."""
    job_id = uuid4()
    monkeypatch.setattr(
        api_client,
        "list_resumes",
        MagicMock(
            return_value=[
                _resume(
                    status="parsed",
                    candidate_name=None,
                    email=_REAL_EMAIL,
                    phone=_REAL_PHONE,
                    candidate={
                        "name": _REAL_NAME,
                        "email": _REAL_EMAIL,
                        "phone": _REAL_PHONE,
                    },
                )
            ]
        ),
    )
    resp = client.get(f"/jobs/{job_id}/resumes-table")
    raw = resp.get_data(as_text=True)
    assert _REAL_NAME not in raw
    assert _REAL_EMAIL not in raw
    assert _REAL_PHONE not in raw


def test_job_detail_open_job_shows_upload_form_with_consent_checkbox(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    monkeypatch.setattr(api_client, "get_job", MagicMock(return_value=_job(job_id)))
    monkeypatch.setattr(api_client, "list_resumes", MagicMock(return_value=[]))
    body = client.get(f"/jobs/{job_id}").get_data(as_text=True)
    assert 'name="consent_acknowledged"' in body
    assert 'type="file"' in body
    assert 'name="files"' in body
