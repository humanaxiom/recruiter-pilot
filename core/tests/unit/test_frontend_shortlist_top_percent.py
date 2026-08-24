"""Slice C — per-job configurable shortlist size, Flask viewer wiring.

Pins the CREATE-job form field + ``frontend.app.create_job`` route + the
``api_client.create_job`` JSON-payload wiring for ``shortlist_top_percent``
(``JobCreate.shortlist_top_percent``, 1-100, default 100 — slices A/B already
land the column + schema + engine cap; this slice only exposes it in the
viewer).

Pinned design decisions (see task brief):
  * The form field renders with ``min="1" max="100"`` and defaults to 100.
  * A blank/absent submitted value maps to an EXPLICIT ``100`` in the payload
    (not omitted, not ``None``) — the route never sends a broken/blank value.
  * An out-of-range value the browser failed to block (e.g. "0" or "150") is
    forwarded to the backend AS-IS (no client-side clamping) — the existing
    ``BadRequest``/``_format_error`` 422 re-render path is the single source
    of truth for validation, exactly like every other JobCreate field
    (``min_years``, ``description_raw``) already behaves.
  * A non-numeric submitted value (a tampered/malformed form post) falls back
    to the 100 default rather than forwarding garbage, mirroring
    ``min_years``'s ``ValueError`` -> ``None`` fallback pattern.

Edit is OUT of scope: there is no job-edit form in this codebase (only the
create form and the blind-review PATCH toggle) — see
``core/frontend/templates/job_detail.html``. This is Flask
form/route/api_client wiring over a mocked backend transport; no real
Postgres/Neo4j/Redis is involved, so the offline suite is sufficient (no
testcontainers integration test needed for this slice).
"""

from __future__ import annotations

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


def _client_with(handler: Any) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test")


# ── GET / — the shortlist-size field renders on the create form ───────────


def test_index_renders_shortlist_top_percent_field(
    monkeypatch: Any, client: Any
) -> None:
    monkeypatch.setattr(api_client, "list_jobs", MagicMock(return_value=[]))
    body = client.get("/").get_data(as_text=True)
    assert 'name="shortlist_top_percent"' in body
    assert 'min="1"' in body
    assert 'max="100"' in body


def test_index_shortlist_top_percent_field_defaults_to_100(
    monkeypatch: Any, client: Any
) -> None:
    monkeypatch.setattr(api_client, "list_jobs", MagicMock(return_value=[]))
    body = client.get("/").get_data(as_text=True)
    assert 'value="100"' in body


# ── POST /jobs — shortlist_top_percent forwarded in the create payload ────


def test_post_jobs_forwards_shortlist_top_percent(
    monkeypatch: Any, client: Any
) -> None:
    spy = MagicMock(return_value={"id": str(uuid4())})
    monkeypatch.setattr(api_client, "create_job", spy)
    client.post(
        "/jobs",
        data={
            "title": "Backend Eng",
            "description_raw": "x" * 80,
            "shortlist_top_percent": "25",
        },
    )
    payload = spy.call_args.args[0]
    assert payload["shortlist_top_percent"] == 25


def test_post_jobs_blank_shortlist_top_percent_defaults_to_100(
    monkeypatch: Any, client: Any
) -> None:
    spy = MagicMock(return_value={"id": str(uuid4())})
    monkeypatch.setattr(api_client, "create_job", spy)
    client.post(
        "/jobs",
        data={
            "title": "Backend Eng",
            "description_raw": "x" * 80,
            "shortlist_top_percent": "",
        },
    )
    payload = spy.call_args.args[0]
    assert payload["shortlist_top_percent"] == 100


def test_post_jobs_absent_shortlist_top_percent_defaults_to_100(
    monkeypatch: Any, client: Any
) -> None:
    spy = MagicMock(return_value={"id": str(uuid4())})
    monkeypatch.setattr(api_client, "create_job", spy)
    client.post(
        "/jobs",
        data={"title": "Backend Eng", "description_raw": "x" * 80},
    )
    payload = spy.call_args.args[0]
    assert payload["shortlist_top_percent"] == 100


def test_post_jobs_non_numeric_shortlist_top_percent_defaults_to_100(
    monkeypatch: Any, client: Any
) -> None:
    """A tampered/malformed form post (non-numeric) falls back to the 100
    default rather than forwarding garbage — mirrors min_years's
    ValueError -> None fallback."""
    spy = MagicMock(return_value={"id": str(uuid4())})
    monkeypatch.setattr(api_client, "create_job", spy)
    client.post(
        "/jobs",
        data={
            "title": "Backend Eng",
            "description_raw": "x" * 80,
            "shortlist_top_percent": "not-a-number",
        },
    )
    payload = spy.call_args.args[0]
    assert payload["shortlist_top_percent"] == 100


def test_post_jobs_out_of_range_shortlist_top_percent_forwarded_unclamped(
    monkeypatch: Any, client: Any
) -> None:
    """No client-side clamping: an out-of-range value that slipped past the
    browser's min/max is forwarded to the backend as-is, which is the single
    source of truth for the 1-100 validation (matches min_years/
    description_raw — no other JobCreate field is clamped client-side
    either)."""
    spy = MagicMock(return_value={"id": str(uuid4())})
    monkeypatch.setattr(api_client, "create_job", spy)
    client.post(
        "/jobs",
        data={
            "title": "Backend Eng",
            "description_raw": "x" * 80,
            "shortlist_top_percent": "150",
        },
    )
    payload = spy.call_args.args[0]
    assert payload["shortlist_top_percent"] == 150


def test_post_jobs_out_of_range_shortlist_top_percent_422_does_not_500(
    monkeypatch: Any, client: Any
) -> None:
    """The backend's ge=1/le=100 constraint rejects an out-of-range value
    with a 422; the route surfaces it via the existing BadRequest/
    _format_error re-render path (same as any other invalid JobCreate
    field) instead of crashing. Also pins that the OUT-OF-RANGE value was
    the one actually forwarded (150), not silently swapped for something
    in-range — a route that dropped/clamped the field before this call
    would fail this assertion."""
    spy = MagicMock(
        side_effect=api_client.BadRequest(
            "bad",
            status_code=422,
            detail={
                "detail": [
                    {
                        "loc": ["body", "shortlist_top_percent"],
                        "msg": "Input should be less than or equal to 100",
                    }
                ]
            },
        )
    )
    monkeypatch.setattr(api_client, "create_job", spy)
    monkeypatch.setattr(api_client, "list_jobs", MagicMock(return_value=[]))
    resp = client.post(
        "/jobs",
        data={
            "title": "Backend Eng",
            "description_raw": "x" * 80,
            "shortlist_top_percent": "150",
        },
    )
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "less than or equal to 100" in body
    payload = spy.call_args.args[0]
    assert payload["shortlist_top_percent"] == 150


def test_post_jobs_success_forwards_custom_shortlist_top_percent(
    monkeypatch: Any, client: Any
) -> None:
    """Full happy-path check: a valid non-default shortlist_top_percent both
    reaches the backend payload AND doesn't break the existing create ->
    redirect flow."""
    job_id = uuid4()
    spy = MagicMock(return_value={"id": str(job_id), "title": "Backend Eng"})
    monkeypatch.setattr(api_client, "create_job", spy)
    resp = client.post(
        "/jobs",
        data={
            "title": "Backend Eng",
            "description_raw": "x" * 80,
            "shortlist_top_percent": "50",
        },
    )
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith(f"/jobs/{job_id}")
    payload = spy.call_args.args[0]
    assert payload["shortlist_top_percent"] == 50


# ── api_client.create_job — shortlist_top_percent rides in the JSON body ──


def test_create_job_includes_shortlist_top_percent_in_json_body() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content
        return httpx.Response(201, json={"id": str(uuid4())})

    api_client.create_job(
        {
            "title": "Backend Eng",
            "description_raw": "x" * 60,
            "shortlist_top_percent": 42,
        },
        client=_client_with(handler),
    )
    assert b'"shortlist_top_percent":42' in captured["body"]
