"""RED — the browser half of the JD re-parse recovery path.

A backend route nobody can click is not a recovery path. The job-detail page
currently renders ``parsing…`` purely from ``parsed_at is none``
(``job_detail.html``), so a JD that died yesterday looks identical to one that
started five seconds ago — which is precisely how twenty dead JDs went unnoticed
for a day. The control therefore has to be gated on ``failure_reason``, which is
already carried on ``JobOut`` and already reaches the template unused.

Scope note: this deliberately does NOT change the ``parsing…`` badge itself or
the 3-second ``hx-trigger`` in ``parse_status.html`` that keeps polling forever
for a failed job. Both are real and both are recorded in ``docs/ROADMAP.md``;
neither is this branch's theme.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from frontend import api_client


@pytest.fixture
def client(csrf_client: Any) -> Any:
    """The shared CSRF-carrying browser client — a real browser presents a page
    token, so these do too rather than the guard being relaxed for them."""
    return csrf_client


def _client_with(
    handler: Callable[[httpx.Request], httpx.Response],
) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test")


def _job(
    job_id: Any,
    *,
    status: str = "draft",
    parsed_at: str | None = None,
    failure_reason: str | None = None,
) -> dict[str, Any]:
    return {
        "id": str(job_id),
        "title": "Senior Backend Engineer",
        "department": "Engineering",
        "location": "Remote",
        "min_years": 5,
        "status": status,
        "blind_review": True,
        "parsed_at": parsed_at,
        "description_parsed": None,
        "failure_reason": failure_reason,
    }


# ── api_client.reparse_job ───────────────────────────────────────────────


def test_reparse_job_posts_to_the_reparse_route() -> None:
    job_id = uuid4()
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        return httpx.Response(202, json={"status": "queued"})

    api_client.reparse_job(job_id, client=_client_with(handler))
    assert captured["method"] == "POST"
    assert captured["path"] == f"/jobs/{job_id}/reparse"


def test_reparse_job_raises_conflict_on_409() -> None:
    """A job past 'draft' cannot be re-parsed; the route needs to tell the user
    that rather than silently claiming it queued something."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"detail": "job is no longer in draft"})

    with pytest.raises(api_client.Conflict) as exc:
        api_client.reparse_job(uuid4(), client=_client_with(handler))
    assert exc.value.status_code == 409


# ── POST /jobs/<id>/reparse (the Flask BFF route) ────────────────────────


def test_reparse_route_redirects_back_to_the_job_page(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_id = uuid4()
    called: dict[str, Any] = {}

    def fake(jid: UUID) -> dict[str, str]:
        called["job_id"] = jid
        return {"status": "queued"}

    monkeypatch.setattr(api_client, "reparse_job", fake)
    resp = client.post(f"/jobs/{job_id}/reparse")
    assert resp.status_code == 302
    assert str(job_id) in resp.headers["Location"]
    assert called["job_id"] == job_id


def test_reparse_route_surfaces_a_conflict_rather_than_500ing(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake(jid: UUID) -> dict[str, str]:
        raise api_client.Conflict(
            "job is no longer in draft",
            status_code=409,
            detail="job is no longer in draft",
        )

    monkeypatch.setattr(api_client, "reparse_job", fake)
    monkeypatch.setattr(
        api_client, "get_job", lambda jid, **kw: _job(jid, status="open")
    )
    monkeypatch.setattr(api_client, "list_resumes", lambda jid, **kw: [])
    resp = client.post(f"/jobs/{uuid4()}/reparse")
    assert resp.status_code == 409


def test_reparse_route_404s_when_the_backend_404s(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake(jid: UUID) -> dict[str, str]:
        raise api_client.NotFound("no such job")

    monkeypatch.setattr(api_client, "reparse_job", fake)
    resp = client.post(f"/jobs/{uuid4()}/reparse")
    assert resp.status_code == 404


# ── the control has to be visible to be a recovery path ──────────────────


def test_job_detail_renders_the_reparse_control_when_the_parse_failed(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_id = uuid4()
    monkeypatch.setattr(
        api_client,
        "get_job",
        lambda jid, **kw: _job(
            jid, failure_reason="LLMUnavailableError: circuit breaker open"
        ),
    )
    monkeypatch.setattr(api_client, "list_resumes", lambda jid, **kw: [])
    html = client.get(f"/jobs/{job_id}").get_data(as_text=True)
    assert f"/jobs/{job_id}/reparse" in html


def test_job_detail_shows_why_the_parse_failed(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ "It failed" is not actionable; "the circuit breaker was open" is. The
    reason is already on ``JobOut`` and was simply never rendered."""
    job_id = uuid4()
    monkeypatch.setattr(
        api_client,
        "get_job",
        lambda jid, **kw: _job(
            jid, failure_reason="LLMUnavailableError: circuit breaker open"
        ),
    )
    monkeypatch.setattr(api_client, "list_resumes", lambda jid, **kw: [])
    html = client.get(f"/jobs/{job_id}").get_data(as_text=True)
    assert "circuit breaker open" in html


def test_job_detail_hides_the_reparse_control_when_nothing_failed(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A healthy in-flight parse must not offer a retry — clicking it would
    clear a ``failure_reason`` that is legitimately absent and queue a second
    run of work already in progress."""
    job_id = uuid4()
    monkeypatch.setattr(
        api_client, "get_job", lambda jid, **kw: _job(jid, failure_reason=None)
    )
    monkeypatch.setattr(api_client, "list_resumes", lambda jid, **kw: [])
    html = client.get(f"/jobs/{job_id}").get_data(as_text=True)
    assert f"/jobs/{job_id}/reparse" not in html
