"""Three merge-blocking reviewer/security fixes on the Phase 7 Flask viewer.

1. ``shortlist_export`` must read ``?format=`` from the query string, validate
   it against the allowed set, and forward it to
   ``api_client.export_shortlist`` — never a ``reveal`` param (exports stay
   anonymized; ``reveal=False`` default only).
2. The Flask app must cap ``MAX_CONTENT_LENGTH`` so an oversized multipart
   upload body is rejected with 413 by Werkzeug before it is buffered into
   memory.
3. ``api_client.build_client`` must attach an explicit ``httpx.Timeout`` to
   the constructed client rather than relying on httpx's implicit default.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import httpx
import pytest

from frontend import api_client
from frontend.app import MAX_UPLOAD_BYTES, app


@pytest.fixture
def client() -> Any:
    app.config.update(TESTING=True)
    return app.test_client()


# ── Fix 1: shortlist_export forwards ?format= (never reveal) ─────────────


def test_shortlist_export_forwards_json_format(monkeypatch: Any, client: Any) -> None:
    job_id = uuid4()
    mock = MagicMock(
        return_value=httpx.Response(
            200, content=b"{}", headers={"content-type": "application/json"}
        )
    )
    monkeypatch.setattr(api_client, "export_shortlist", mock)
    resp = client.get(f"/jobs/{job_id}/shortlist/export?format=json")
    assert resp.status_code == 200
    mock.assert_called_once_with(job_id, format="json")


def test_shortlist_export_forwards_evidence_csv_format(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    mock = MagicMock(
        return_value=httpx.Response(
            200, content=b"a,b", headers={"content-type": "text/csv"}
        )
    )
    monkeypatch.setattr(api_client, "export_shortlist", mock)
    resp = client.get(f"/jobs/{job_id}/shortlist/export?format=evidence-csv")
    assert resp.status_code == 200
    mock.assert_called_once_with(job_id, format="evidence-csv")


def test_shortlist_export_defaults_to_csv_when_format_missing(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    mock = MagicMock(
        return_value=httpx.Response(
            200, content=b"a,b", headers={"content-type": "text/csv"}
        )
    )
    monkeypatch.setattr(api_client, "export_shortlist", mock)
    resp = client.get(f"/jobs/{job_id}/shortlist/export")
    assert resp.status_code == 200
    mock.assert_called_once_with(job_id, format="csv")


def test_shortlist_export_falls_back_to_csv_on_unknown_format(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    mock = MagicMock(
        return_value=httpx.Response(
            200, content=b"a,b", headers={"content-type": "text/csv"}
        )
    )
    monkeypatch.setattr(api_client, "export_shortlist", mock)
    resp = client.get(f"/jobs/{job_id}/shortlist/export?format=xml")
    assert resp.status_code == 200
    mock.assert_called_once_with(job_id, format="csv")


def test_shortlist_export_never_forwards_a_reveal_kwarg(
    monkeypatch: Any, client: Any
) -> None:
    """Even if a visitor appends ``?reveal=true`` to the export URL, the
    route must never read/forward it — only ``format`` is ever passed."""
    job_id = uuid4()
    mock = MagicMock(
        return_value=httpx.Response(
            200, content=b"a,b", headers={"content-type": "text/csv"}
        )
    )
    monkeypatch.setattr(api_client, "export_shortlist", mock)
    resp = client.get(f"/jobs/{job_id}/shortlist/export?format=json&reveal=true")
    assert resp.status_code == 200
    mock.assert_called_once_with(job_id, format="json")
    _, kwargs = mock.call_args
    assert "reveal" not in kwargs


# ── Fix 2: MAX_CONTENT_LENGTH caps the request body ───────────────────────


def test_max_content_length_is_configured_on_the_app() -> None:
    assert app.config["MAX_CONTENT_LENGTH"] == MAX_UPLOAD_BYTES
    assert MAX_UPLOAD_BYTES > 0


def test_oversized_upload_body_is_rejected_with_413(client: Any) -> None:
    job_id = uuid4()
    previous = app.config["MAX_CONTENT_LENGTH"]
    app.config["MAX_CONTENT_LENGTH"] = 10
    try:
        resp = client.post(
            f"/jobs/{job_id}/resumes",
            data=b"x" * 1000,
            content_type="application/octet-stream",
        )
        assert resp.status_code == 413
    finally:
        app.config["MAX_CONTENT_LENGTH"] = previous


# ── Fix 3: httpx client has an explicit timeout ───────────────────────────


def test_build_client_has_an_explicit_timeout(monkeypatch: Any) -> None:
    built = api_client.build_client()
    try:
        timeout = built.timeout
        assert timeout.connect == 5.0
        assert timeout.read == 30.0
    finally:
        built.close()
