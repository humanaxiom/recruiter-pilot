"""RED — a failed JD must stop claiming it is still parsing, and stop polling.

The display half of the 2026-08-21 stuck-JD incident. `feat/jd-reparse-control`
gave a failed JD a recovery path; this gives it an honest status.

**What was wrong.** Both the badge in ``job_detail.html`` and the HTMX fragment
in ``parse_status.html`` derived "is it parsing?" from ``parsed_at is none``
alone, never consulting ``failure_reason``. Two consequences, both observed on
the live product:

1. A JD that died 24 hours ago rendered identically to one that started five
   seconds ago — which is exactly why twenty dead JDs went unnoticed for a day,
   and why the user who reported them described them as "stuck on parsing".
2. ``parse_status.html`` keeps ``hx-trigger="every 3s"`` for as long as
   ``parsed_at`` is null. For a failed job that is **forever**: every open tab
   re-polls ``GET /jobs/<id>/parse-status`` every three seconds against a row
   that will never change. The fragment's own comment claims it "DROPS the
   trigger, so polling stops" — true only for the success path it was written
   against.

The tri-state the templates actually need is parsed / failed / in-flight, and
``failure_reason`` is what separates the middle one. It has been on ``JobOut``
and reaching these templates unused the whole time.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from frontend import api_client


@pytest.fixture
def client(csrf_client: Any) -> Any:
    return csrf_client


def _job(
    job_id: Any,
    *,
    status: str = "draft",
    parsed_at: str | None = None,
    failure_reason: str | None = None,
    description_parsed: Any = None,
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
        "description_parsed": description_parsed,
        "failure_reason": failure_reason,
    }


def _detail(client: Any, monkeypatch: pytest.MonkeyPatch, job: dict[str, Any]) -> str:
    monkeypatch.setattr(api_client, "get_job", lambda jid, **kw: job)
    monkeypatch.setattr(api_client, "list_resumes", lambda jid, **kw: [])
    return client.get(f"/jobs/{job['id']}").get_data(as_text=True)


def _fragment(client: Any, monkeypatch: pytest.MonkeyPatch, job: dict[str, Any]) -> str:
    monkeypatch.setattr(api_client, "get_job", lambda jid, **kw: job)
    return client.get(f"/jobs/{job['id']}/parse-status").get_data(as_text=True)


# ── the badge must not claim a dead job is in flight ─────────────────────


def test_the_badge_does_not_say_parsing_for_a_failed_job(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole reported symptom: "stuck on parsing since yesterday" for jobs
    that were not parsing and never would."""
    job = _job(uuid4(), failure_reason="LLMUnavailableError: circuit breaker open")
    assert "parsing…" not in _detail(client, monkeypatch, job)


def test_the_badge_still_says_parsing_while_a_parse_is_genuinely_in_flight(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The load-bearing other direction — this must not become "never show
    parsing", which would trade one lie for another."""
    job = _job(uuid4(), failure_reason=None)
    assert "parsing…" in _detail(client, monkeypatch, job)


def test_a_failed_job_is_labelled_as_failed_on_the_page(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = _job(uuid4(), failure_reason="LLMUnavailableError: circuit breaker open")
    assert "parse failed" in _detail(client, monkeypatch, job).lower()


def test_a_failed_job_is_not_told_to_wait_for_the_llm(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``job_detail.html`` tells the user to "Wait for the LLM to finish parsing
    the JD before opening for applicants". For a failed job that is advice to
    wait forever."""
    job = _job(uuid4(), failure_reason="LLMUnavailableError: circuit breaker open")
    assert "wait for the llm" not in _detail(client, monkeypatch, job).lower()


# ── the poll must stop, or it never stops ────────────────────────────────


def test_the_poll_fragment_drops_its_trigger_for_a_failed_job(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Otherwise every open tab polls a row that will never change, every three
    seconds, indefinitely."""
    job = _job(uuid4(), failure_reason="LLMUnavailableError: circuit breaker open")
    assert "hx-trigger" not in _fragment(client, monkeypatch, job)


def test_the_poll_fragment_keeps_its_trigger_while_parsing(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other direction: a genuine in-flight parse must still self-update,
    or the page stops reflecting reality."""
    job = _job(uuid4(), failure_reason=None)
    assert "hx-trigger" in _fragment(client, monkeypatch, job)


def test_the_poll_fragment_drops_its_trigger_once_parsed(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pre-existing behaviour, pinned here so the tri-state rewrite cannot
    regress the success path it was originally written for."""
    job = _job(
        uuid4(),
        parsed_at="2026-08-23T06:00:30Z",
        description_parsed={"required_skills": [{"name": "python"}]},
    )
    assert "hx-trigger" not in _fragment(client, monkeypatch, job)


def test_the_poll_fragment_shows_the_failure_instead_of_parsing(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = _job(uuid4(), failure_reason="LLMUnavailableError: circuit breaker open")
    html = _fragment(client, monkeypatch, job)
    assert "parsing…" not in html
    assert "circuit breaker open" in html
