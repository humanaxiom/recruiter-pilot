"""FU-3 Slice 5 — many-to-many candidate↔job views (reverse-match UI wiring).

The job→candidates direction already exists (a job's shortlist lists its
candidates, each linking to the résumé). This slice adds the candidate→jobs
direction and makes it actionable:

* ``api_client.trigger_reverse_match`` — ``POST /resumes/{id}/match-jobs``
  (enqueue ack; 404→``NotFound``, 5xx→``BackendUnavailable``).
* Flask ``POST /resumes/<id>/match-jobs`` — enqueues then returns the pollable
  match-results fragment (``reverse_match_job`` is async, so results arrive
  later). The trigger is POST-only (a side-effecting trigger must never be a
  prefetchable GET).
* ``GET /resumes/<id>/match-results-cards`` — the bounded, server-clamped HTMX
  poll fragment (mirrors ``shortlist-cards``).
* ``match_results.html`` — renders each entry's ``title``/``department`` and its
  ``score_final``×100, and links each row to that job's detail page (the
  candidate→job navigation). Three distinct empty states: still-finding (poll),
  empty-done (a résumé genuinely matches no open job), give-up at the cap.

**No-redaction note (ADR-012 §4):** reverse-match applies NONE — the caller
owns the résumé and jobs are not personal data, so this path shows the REAL job
titles (not blinded). That is correct here; do NOT add résumé-PII rendering.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import httpx
import pytest

from frontend import api_client

# A distinctive job title we can byte-scan for: reverse-match intentionally
# shows real job titles (jobs aren't PII), so this MUST appear in the output.
_JOB_TITLE = "Principal Reliability Engineer"
_DEPARTMENT = "Infrastructure Platform"


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


def _norm(text: str) -> str:
    """Collapse all whitespace and lowercase before a substring check.

    A template line-wrap can split a phrase across a rendered newline (e.g.
    "...a minute\n    or two..."), which would let an absence assertion pass
    merely because the phrase happens to wrap, not because it's actually
    gone. Normalizing whitespace first closes that gap.
    """
    return " ".join(text.split()).lower()


def _entry(
    job_id: Any,
    *,
    title: str = _JOB_TITLE,
    department: str | None = _DEPARTMENT,
    rank: int = 1,
    score: float = 0.83,
) -> dict[str, Any]:
    return {
        "job_id": str(job_id),
        "title": title,
        "department": department,
        "rank": rank,
        "score_final": score,
        "score_structured": 0.80,
        "score_evidence": 0.70,
        "score_breakdown": {
            "skill": 0.9,
            "experience": 0.8,
            "education": 0.7,
            "seniority": 0.6,
            "vector": 0.75,
            "structured": 0.5,
            "motivation": 0.4,
            "skill_contributions": [],
        },
        "evidence": None,
        "requirement_count": 5,
        "must_have_count": 2,
    }


def _results(
    resume_id: Any,
    *,
    entries: list[dict[str, Any]] | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    return {
        "resume_id": str(resume_id),
        "entries": entries or [],
        "pipeline_meta": None,
        "generated_at": generated_at,
    }


# ── api_client.trigger_reverse_match ──────────────────────────────────────


def test_trigger_reverse_match_posts_to_the_match_jobs_path() -> None:
    resume_id = uuid4()
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = request.url
        return httpx.Response(
            202, json={"resume_id": str(resume_id), "status": "enqueued"}
        )

    result = api_client.trigger_reverse_match(resume_id, client=_client_with(handler))
    assert captured["method"] == "POST"
    assert captured["url"].path == f"/resumes/{resume_id}/match-jobs"
    assert result == {"resume_id": str(resume_id), "status": "enqueued"}


def test_trigger_reverse_match_maps_404_to_not_found() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "no resume"})

    with pytest.raises(api_client.NotFound):
        api_client.trigger_reverse_match(uuid4(), client=_client_with(handler))


def test_trigger_reverse_match_maps_5xx_to_backend_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "boom"})

    with pytest.raises(api_client.BackendUnavailable):
        api_client.trigger_reverse_match(uuid4(), client=_client_with(handler))


def test_trigger_reverse_match_signature_has_no_reveal_parameter() -> None:
    # Reverse-match has no redaction concept (ADR-012 §4).
    sig = inspect.signature(api_client.trigger_reverse_match)
    assert "reveal" not in sig.parameters


# ── POST /resumes/<id>/match-jobs — trigger route ─────────────────────────


def test_match_jobs_route_calls_trigger_and_returns_pollable_fragment(
    monkeypatch: Any, client: Any
) -> None:
    resume_id = uuid4()
    spy = MagicMock(return_value={"resume_id": str(resume_id), "status": "enqueued"})
    monkeypatch.setattr(api_client, "trigger_reverse_match", spy)
    monkeypatch.setattr(
        api_client, "get_match_results", MagicMock(return_value=_results(resume_id))
    )
    resp = client.post(f"/resumes/{resume_id}/match-jobs")
    assert resp.status_code == 200
    spy.assert_called_once()
    body = resp.get_data(as_text=True)
    # results arrive asynchronously → the returned fragment polls for them
    assert "hx-trigger" in body
    assert "Finding matching jobs" in body


def test_match_jobs_route_is_post_only(client: Any) -> None:
    """A trigger with a side effect must never be a prefetchable GET — a GET to
    the trigger path is 405 (method not allowed), not a route that enqueues."""
    resp = client.get(f"/resumes/{uuid4()}/match-jobs")
    assert resp.status_code == 405


def test_match_jobs_route_404s_when_resume_missing(
    monkeypatch: Any, client: Any
) -> None:
    monkeypatch.setattr(
        api_client,
        "trigger_reverse_match",
        MagicMock(side_effect=api_client.NotFound("no resume")),
    )
    resp = client.post(f"/resumes/{uuid4()}/match-jobs")
    assert resp.status_code == 404


def test_match_jobs_route_backend_unavailable_is_not_a_500(
    monkeypatch: Any, client: Any
) -> None:
    monkeypatch.setattr(
        api_client,
        "trigger_reverse_match",
        MagicMock(side_effect=api_client.BackendUnavailable("down")),
    )
    resp = client.post(f"/resumes/{uuid4()}/match-jobs")
    assert resp.status_code in (502, 503)
    assert resp.status_code != 500


# ── GET /resumes/<id>/match-results-cards — poll fragment ─────────────────


def test_match_cards_polls_while_empty_and_not_done(
    monkeypatch: Any, client: Any
) -> None:
    resume_id = uuid4()
    monkeypatch.setattr(
        api_client, "get_match_results", MagicMock(return_value=_results(resume_id))
    )
    resp = client.get(f"/resumes/{resume_id}/match-results-cards")
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "hx-trigger" in body
    assert "Finding matching jobs" in body


def test_match_cards_below_cap_increments_attempt(
    monkeypatch: Any, client: Any
) -> None:
    resume_id = uuid4()
    monkeypatch.setattr(
        api_client, "get_match_results", MagicMock(return_value=_results(resume_id))
    )
    body = client.get(f"/resumes/{resume_id}/match-results-cards?attempt=5").get_data(
        as_text=True
    )
    assert "hx-trigger" in body
    assert "attempt=6" in body


def test_match_cards_stops_once_entries_exist(monkeypatch: Any, client: Any) -> None:
    resume_id = uuid4()
    monkeypatch.setattr(
        api_client,
        "get_match_results",
        MagicMock(
            return_value=_results(
                resume_id,
                entries=[_entry(uuid4())],
                generated_at="2026-07-18T00:00:00Z",
            )
        ),
    )
    resp = client.get(f"/resumes/{resume_id}/match-results-cards")
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "hx-trigger" not in body  # polling stopped
    assert _JOB_TITLE in body


def test_match_cards_gives_up_at_the_attempt_cap(monkeypatch: Any, client: Any) -> None:
    """At the cap, still empty and not done, it STOPS (drops hx-trigger) and
    shows a give-up message instead of polling forever."""
    from frontend.app import _MAX_MATCH_POLL_ATTEMPTS

    resume_id = uuid4()
    monkeypatch.setattr(
        api_client, "get_match_results", MagicMock(return_value=_results(resume_id))
    )
    body = client.get(
        f"/resumes/{resume_id}/match-results-cards?attempt={_MAX_MATCH_POLL_ATTEMPTS}"
    ).get_data(as_text=True)
    assert "hx-trigger" not in body
    assert "Finding matching jobs" not in body


def test_match_cards_empty_done_is_distinct_from_still_finding(
    monkeypatch: Any, client: Any
) -> None:
    """Empty-but-DONE (a run completed, this résumé matches no open job) must
    read clearly as 'no matches' — NOT as the still-working 'Finding…' poll —
    and must NOT keep polling."""
    resume_id = uuid4()
    monkeypatch.setattr(
        api_client,
        "get_match_results",
        MagicMock(
            return_value=_results(resume_id, generated_at="2026-07-18T00:00:00Z")
        ),
    )
    body = client.get(f"/resumes/{resume_id}/match-results-cards").get_data(
        as_text=True
    )
    assert "hx-trigger" not in body  # done → not still working
    assert "Finding matching jobs" not in body
    assert "No matching" in body


def test_match_cards_clamps_out_of_range_attempt(monkeypatch: Any, client: Any) -> None:
    """A hand-edited/garbage ``attempt`` must never crash or unbound the loop."""
    resume_id = uuid4()
    monkeypatch.setattr(
        api_client, "get_match_results", MagicMock(return_value=_results(resume_id))
    )
    base = f"/resumes/{resume_id}/match-results-cards"
    assert client.get(f"{base}?attempt=-9").status_code == 200
    assert client.get(f"{base}?attempt=abc").status_code == 200
    huge = client.get(f"{base}?attempt=999999").get_data(as_text=True)
    assert "hx-trigger" not in huge  # clamped to the cap → gives up, no runaway


def test_match_cards_404s_when_resume_missing(monkeypatch: Any, client: Any) -> None:
    monkeypatch.setattr(
        api_client,
        "get_match_results",
        MagicMock(side_effect=api_client.NotFound("no resume")),
    )
    resp = client.get(f"/resumes/{uuid4()}/match-results-cards")
    assert resp.status_code == 404


def test_match_cards_read_carries_no_reveal_kwarg(
    monkeypatch: Any, client: Any
) -> None:
    """Reverse-match has no redaction concept — the read must never pass a
    ``reveal`` kwarg through (ADR-012 §4)."""
    resume_id = uuid4()
    spy = MagicMock(return_value=_results(resume_id))
    monkeypatch.setattr(api_client, "get_match_results", spy)
    client.get(f"/resumes/{resume_id}/match-results-cards")
    spy.assert_called_once()
    assert "reveal" not in spy.call_args.kwargs


# ── honest progress copy + elapsed indicator (fix/upload-and-progress-ux) ──
#
# Diagnosed live: the reverse-match job runs the evidence model on EVERY
# candidate the run considers (per-candidate evidence calls on a local model,
# ~1-2 min PER candidate), so a full run realistically takes several minutes,
# scaling with the number of jobs/candidates involved — not "a minute or two"
# as the old copy claimed. That undersell reads as "not doing much" / broken.
# This fix is copy + a live elapsed-time signal only (no backend/server-state
# change in this slice — a fully server-durable timestamp, so the indicator
# survives a hard reload, stays out of scope). Pure Flask/Jinja rendering over
# a mocked api_client — offline suffices, no integration test needed.


def test_match_cards_still_finding_shows_honest_multi_minute_copy(
    monkeypatch: Any, client: Any
) -> None:
    """The old copy ('the local model can take a minute or two') badly
    undersells a job that runs the model on every candidate; several minutes
    is the honest floor."""
    resume_id = uuid4()
    monkeypatch.setattr(
        api_client, "get_match_results", MagicMock(return_value=_results(resume_id))
    )
    body = client.get(f"/resumes/{resume_id}/match-results-cards").get_data(
        as_text=True
    )
    assert "several minutes" in _norm(body)
    assert "every candidate" in _norm(body)


def test_match_cards_still_finding_drops_the_old_minute_or_two_phrasing(
    monkeypatch: Any, client: Any
) -> None:
    resume_id = uuid4()
    monkeypatch.setattr(
        api_client, "get_match_results", MagicMock(return_value=_results(resume_id))
    )
    body = client.get(f"/resumes/{resume_id}/match-results-cards").get_data(
        as_text=True
    )
    assert "minute or two" not in _norm(body)


def test_match_cards_elapsed_indicator_at_attempt_zero_shows_a_sane_start(
    monkeypatch: Any, client: Any
) -> None:
    """At ``attempt=0`` (~0s since the poll started) the indicator must not
    lie about elapsed time — it shows a near-zero/just-started reading."""
    resume_id = uuid4()
    monkeypatch.setattr(
        api_client, "get_match_results", MagicMock(return_value=_results(resume_id))
    )
    body = client.get(f"/resumes/{resume_id}/match-results-cards").get_data(
        as_text=True
    )
    assert "elapsed" in _norm(body)
    assert "0s" in _norm(body) or "just started" in _norm(body)


def test_match_cards_elapsed_indicator_scales_with_attempt(
    monkeypatch: Any, client: Any
) -> None:
    """The poll re-fires every 3s, so at attempt=40 roughly 120s (~2 min) have
    passed — the fragment must reflect that moving figure, not a static hint."""
    resume_id = uuid4()
    monkeypatch.setattr(
        api_client, "get_match_results", MagicMock(return_value=_results(resume_id))
    )
    body = client.get(f"/resumes/{resume_id}/match-results-cards?attempt=40").get_data(
        as_text=True
    )
    assert "elapsed" in _norm(body)
    assert "120s" in _norm(body) or "2 min" in _norm(body) or "2:00" in _norm(body)


def test_match_cards_empty_done_has_no_elapsed_or_still_working_line(
    monkeypatch: Any, client: Any
) -> None:
    """A completed-but-empty result is terminal — it must not carry the
    still-working elapsed indicator."""
    resume_id = uuid4()
    monkeypatch.setattr(
        api_client,
        "get_match_results",
        MagicMock(
            return_value=_results(resume_id, generated_at="2026-07-18T00:00:00Z")
        ),
    )
    body = client.get(f"/resumes/{resume_id}/match-results-cards").get_data(
        as_text=True
    )
    assert "elapsed" not in _norm(body)
    assert "still working" not in _norm(body)
    assert "No matching" in body


def test_match_cards_gave_up_has_no_elapsed_or_still_working_line(
    monkeypatch: Any, client: Any
) -> None:
    """The give-up state (attempt at the cap) is terminal too — no elapsed
    line, no poll trigger."""
    from frontend.app import _MAX_MATCH_POLL_ATTEMPTS

    resume_id = uuid4()
    monkeypatch.setattr(
        api_client, "get_match_results", MagicMock(return_value=_results(resume_id))
    )
    body = client.get(
        f"/resumes/{resume_id}/match-results-cards?attempt={_MAX_MATCH_POLL_ATTEMPTS}"
    ).get_data(as_text=True)
    assert "hx-trigger" not in body
    assert "elapsed" not in _norm(body)
    assert "still working" not in _norm(body)


# ── match_results.html — candidate→job navigation ─────────────────────────


def test_match_results_renders_title_department_and_score(
    monkeypatch: Any, client: Any
) -> None:
    resume_id = uuid4()
    monkeypatch.setattr(
        api_client,
        "get_match_results",
        MagicMock(
            return_value=_results(
                resume_id,
                entries=[_entry(uuid4(), score=0.83)],
                generated_at="2026-07-18T00:00:00Z",
            )
        ),
    )
    body = client.get(f"/resumes/{resume_id}/match-results").get_data(as_text=True)
    assert _JOB_TITLE in body
    assert _DEPARTMENT in body
    assert "83" in body  # round(0.83 * 100)


def test_match_results_links_each_row_to_job_detail(
    monkeypatch: Any, client: Any
) -> None:
    """The candidate→job navigation: each ranked row links to that job's detail
    page (``url_for('job_detail', job_id=...)``)."""
    resume_id = uuid4()
    job_id = uuid4()
    monkeypatch.setattr(
        api_client,
        "get_match_results",
        MagicMock(
            return_value=_results(
                resume_id,
                entries=[_entry(job_id)],
                generated_at="2026-07-18T00:00:00Z",
            )
        ),
    )
    body = client.get(f"/resumes/{resume_id}/match-results").get_data(as_text=True)
    assert f"/jobs/{job_id}" in body


def test_match_results_full_page_still_uses_a_table(
    monkeypatch: Any, client: Any
) -> None:
    """Even while empty the full page renders a ``class="table"`` (styling gate
    regression — the empty state lives inside the table, not instead of it)."""
    resume_id = uuid4()
    monkeypatch.setattr(
        api_client, "get_match_results", MagicMock(return_value=_results(resume_id))
    )
    body = client.get(f"/resumes/{resume_id}/match-results").get_data(as_text=True)
    assert 'class="table"' in body


def test_match_results_shows_real_job_titles_not_blinded(
    monkeypatch: Any, client: Any
) -> None:
    """Documents ADR-012 §4: reverse-match has NO redaction — the caller owns
    the résumé and jobs are not PII, so REAL job titles are shown here. (This is
    correct, unlike the shortlist which blinds candidate identity.)"""
    resume_id = uuid4()
    monkeypatch.setattr(
        api_client,
        "get_match_results",
        MagicMock(
            return_value=_results(
                resume_id,
                entries=[_entry(uuid4(), title="Confidential Skunkworks Lead")],
                generated_at="2026-07-18T00:00:00Z",
            )
        ),
    )
    body = client.get(f"/resumes/{resume_id}/match-results").get_data(as_text=True)
    assert "Confidential Skunkworks Lead" in body


# ── resume_detail.html — the "Find matching jobs" trigger ─────────────────


def _resume(resume_id: Any) -> dict[str, Any]:
    return {
        "id": str(resume_id),
        "job_id": str(uuid4()),
        "original_filename": "resume.pdf",
        "mime_type": "application/pdf",
        "file_size_bytes": 1234,
        "sha256": "0" * 64,
        "candidate": {"name": None, "email": None, "phone": None, "location": None},
        "candidate_email_hash": None,
        "parsed": {
            "candidate": {
                "name": None,
                "email": None,
                "phone": None,
                "location": None,
            },
            "summary": "Backend engineer.",
            "skills": [],
            "experience": [],
            "education": [],
            "chunks": [],
            "cover_letter_chunks": [],
        },
        "status": "parsed",
        "uploaded_by": None,
        "uploaded_at": "2026-07-01T00:00:00Z",
        "parsed_at": "2026-07-01T00:05:00Z",
        "failure_reason": None,
        "consent_acknowledged": True,
        "blinded": True,
        "cover_letter_text": None,
        "cover_letter_parsed": None,
    }


def test_resume_detail_has_find_matching_jobs_post_trigger(
    monkeypatch: Any, client: Any
) -> None:
    """The résumé page carries a 'Find matching jobs' button that is a POST form
    to the trigger route (POST-only — a side-effecting trigger must not be a
    prefetchable GET link)."""
    resume_id = uuid4()
    monkeypatch.setattr(
        api_client, "get_resume", MagicMock(return_value=_resume(resume_id))
    )
    body = client.get(f"/resumes/{resume_id}").get_data(as_text=True)
    assert "Find matching jobs" in body
    assert f"/resumes/{resume_id}/match-jobs" in body
    # It is a POST form, not an anchor/GET link to the trigger.
    assert 'method="post"' in body.lower()


def test_resume_detail_keeps_link_to_view_match_results(
    monkeypatch: Any, client: Any
) -> None:
    """A link to view already-computed results is preserved alongside the
    trigger (the read is a safe GET)."""
    resume_id = uuid4()
    monkeypatch.setattr(
        api_client, "get_resume", MagicMock(return_value=_resume(resume_id))
    )
    body = client.get(f"/resumes/{resume_id}").get_data(as_text=True)
    assert f"/resumes/{resume_id}/match-results" in body
