"""RED tests for merge-blocking review findings on
``fix/regenerate-shortlist-no-feedback`` (PR review, 2026-08-18).

Covers F1 (major), F3 (minor) and F4 (minor). F2 (the DDL boot race) is
pinned separately, in ``core/tests/integration/test_schema.py``, because it
needs a real Postgres — see that file's own section for the honest
assessment of what it can and cannot prove.

Deliberately a SEPARATE file from ``test_frontend_shortlist.py`` (which
already covers the same route/template pair extensively) purely to keep this
review-findings pass self-contained and easy to diff away once it lands —
it reuses that file's own fixtures/helpers by re-declaring the same small
``client``/``_norm``/``_full_entry`` conveniences rather than importing
private names across test modules.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from frontend import api_client

_REAL_NAME = "Zzyzxqrst Wibblesworth"


@pytest.fixture
def client(csrf_client: Any) -> Any:
    """The shared CSRF-carrying browser client (Phase 1.3) — see
    ``test_frontend_shortlist.py``'s identical fixture for why this is not
    autouse."""
    return csrf_client


@pytest.fixture(autouse=True)
def _default_shortlist_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """Benign default so any test in this file that forgets to stub
    ``get_shortlist_status`` still gets a harmless 'no state' shape rather
    than an ``AttributeError``/real-network call. Every test below that cares
    about the status overrides this explicitly."""
    monkeypatch.setattr(
        api_client,
        "get_shortlist_status",
        lambda *_a, **_k: {"job_id": None, "state": None, "reason": None, "at": None},
        raising=False,
    )


def _norm(text: str) -> str:
    """Collapse whitespace and lowercase — a template line-wrap can split a
    phrase across a newline, which would let an absence assertion pass for
    the wrong reason. Mirrors ``test_frontend_shortlist.py``'s helper."""
    return " ".join(text.split()).lower()


def _full_entry(entry_id: Any) -> dict[str, Any]:
    return {
        "id": str(entry_id),
        "job_id": str(uuid4()),
        "resume_id": str(uuid4()),
        "rank": 1,
        "score_final": 0.87,
        "score_breakdown": {
            "skill": 0.90,
            "experience": 0.80,
            "education": 0.70,
            "seniority": 0.60,
            "vector": 0.75,
            "structured": 0.50,
            "motivation": 0.40,
            "skill_contributions": [],
        },
        "evidence": {
            "requirements": [],
            "overall_summary": "Strong backend candidate.",
        },
        "blinded": True,
        "display_label": "Candidate A",
    }


# ── F1 (major) — the fix misses the page-load path ─────────────────────────
#
# ``job_shortlist`` (frontend/app.py:823-853) renders ``shortlist_list.html``
# WITHOUT ever passing ``shortlist_status`` — that template ``{% include %}``s
# ``shortlist_cards.html``, so on a full page load ``_status`` is always
# ``None`` regardless of what the backend reports. Reloading during an
# in-flight Regenerate (``jobs.shortlist_state = 'ranking'``) therefore
# reproduces the ORIGINAL no-feedback defect exactly — no hx-trigger, no
# banner — even though the fragment route (``shortlist_cards``) already
# fetches and honors the status. These tests fail against today's code
# because ``job_shortlist`` never calls ``api_client.get_shortlist_status``
# at all, not because of a broken test.


def test_job_shortlist_full_page_load_during_ranking_shows_trigger_and_banner(
    monkeypatch: Any, client: Any
) -> None:
    """Mirrors ``test_frontend_shortlist.py``'s
    ``test_shortlist_cards_entries_present_and_ranking_shows_trigger_banner_and_old_cards``
    for the full-page route: a reload while Regenerate is in flight must show
    the same poll trigger + banner + stale cards as the fragment route does,
    because reloading is exactly what a user does when 'nothing happened'."""
    job_id = uuid4()
    monkeypatch.setattr(
        api_client,
        "list_shortlist",
        MagicMock(return_value=[_full_entry(uuid4())]),
    )
    monkeypatch.setattr(
        api_client, "list_resumes", MagicMock(return_value=[{"status": "parsed"}])
    )
    status_spy = MagicMock(
        return_value={
            "job_id": str(job_id),
            "state": "ranking",
            "reason": None,
            "at": "2026-08-18T00:00:00+00:00",
        }
    )
    monkeypatch.setattr(api_client, "get_shortlist_status", status_spy)

    resp = client.get(f"/jobs/{job_id}/shortlist")
    body = resp.get_data(as_text=True)
    normalized = _norm(body)

    assert resp.status_code == 200
    status_spy.assert_called_once()
    assert "hx-trigger" in body, (
        "a run is in flight -- a full page load must poll too, not just the "
        "fragment route"
    )
    assert "previous run" in normalized
    assert "in progress" in normalized
    assert "Candidate A" in body  # the previous run's cards must still render


def test_job_shortlist_full_page_load_awaiting_llm_with_entries_keeps_polling(
    monkeypatch: Any, client: Any
) -> None:
    """Sibling of the test above for the ``awaiting_llm`` state (a fail-closed
    Regenerate retry queued server-side) — same missing wiring, same defect."""
    job_id = uuid4()
    monkeypatch.setattr(
        api_client,
        "list_shortlist",
        MagicMock(return_value=[_full_entry(uuid4())]),
    )
    monkeypatch.setattr(
        api_client, "list_resumes", MagicMock(return_value=[{"status": "parsed"}])
    )
    monkeypatch.setattr(
        api_client,
        "get_shortlist_status",
        MagicMock(
            return_value={
                "job_id": str(job_id),
                "state": "awaiting_llm",
                "reason": "boom",
                "at": "2026-08-01T00:00:00+00:00",
            }
        ),
    )
    resp = client.get(f"/jobs/{job_id}/shortlist")
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert (
        "hx-trigger" in body
    ), "a retry is queued server-side -- polling must continue"
    assert "Candidate A" in body


def test_job_shortlist_full_page_load_fetches_status_unconditionally(
    monkeypatch: Any, client: Any
) -> None:
    """Structural pin, independent of the wording assertions above: the
    full-page route must call ``api_client.get_shortlist_status`` on every
    load, exactly like the fragment route already does."""
    job_id = uuid4()
    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=[]))
    monkeypatch.setattr(api_client, "list_resumes", MagicMock(return_value=[]))
    spy = MagicMock(
        return_value={"job_id": str(job_id), "state": None, "reason": None, "at": None}
    )
    monkeypatch.setattr(api_client, "get_shortlist_status", spy)
    client.get(f"/jobs/{job_id}/shortlist")
    spy.assert_called_once()


def test_job_shortlist_full_page_load_not_ranking_has_no_trigger(
    monkeypatch: Any, client: Any
) -> None:
    """Negative case, pinned explicitly (mirrors the fragment route's own
    negative test): entries present and no run in flight must NOT trigger a
    poll on the full page either — trading the no-feedback bug for an
    infinite poll would be exactly as broken."""
    job_id = uuid4()
    monkeypatch.setattr(
        api_client,
        "list_shortlist",
        MagicMock(return_value=[_full_entry(uuid4())]),
    )
    monkeypatch.setattr(
        api_client, "list_resumes", MagicMock(return_value=[{"status": "parsed"}])
    )
    monkeypatch.setattr(
        api_client,
        "get_shortlist_status",
        MagicMock(
            return_value={
                "job_id": str(job_id),
                "state": None,
                "reason": None,
                "at": None,
            }
        ),
    )
    resp = client.get(f"/jobs/{job_id}/shortlist")
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "hx-trigger" not in body
    assert "Candidate A" in body


def test_job_shortlist_full_page_load_never_leaks_pii_even_with_a_run_in_flight(
    monkeypatch: Any, client: Any
) -> None:
    """Blind invariant carried over onto the newly-wired status path: adding
    ``shortlist_status`` to the page-load render must not introduce a
    PII-carrying code path anywhere in ``job_shortlist``."""
    job_id = uuid4()
    entry = _full_entry(uuid4())
    entry["candidate_name"] = _REAL_NAME
    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=[entry]))
    monkeypatch.setattr(
        api_client, "list_resumes", MagicMock(return_value=[{"status": "parsed"}])
    )
    monkeypatch.setattr(
        api_client,
        "get_shortlist_status",
        MagicMock(
            return_value={
                "job_id": str(job_id),
                "state": "ranking",
                "reason": None,
                "at": "2026-08-18T00:00:00+00:00",
            }
        ),
    )
    body = client.get(f"/jobs/{job_id}/shortlist").get_data(as_text=True)
    assert "Candidate A" in body
    assert _REAL_NAME not in body


# ── F3 (minor, on-theme) — the new banners outlive the poll ────────────────
#
# ``shortlist_cards.html:46`` (``{% if entries and ranking %}``) and ``:58``
# (``{% elif entries and awaiting_llm %}``) are NOT gated on ``polling``,
# unlike the pre-existing ``{% elif polling and awaiting_llm %}`` /
# ``{% elif polling %}`` branches below them. At ``a >= m`` the wrapping div's
# own hx-trigger is already correctly dropped (``polling`` is False), but
# these two banners still render their spinner and their "will replace ...
# automatically" / "no action needed" promise — a promise nothing is left to
# keep once the poll has genuinely stopped. Fails against today's code
# because the branches never consult ``polling`` at all.

_HONEST_GIVE_UP_PHRASES: tuple[str, ...] = (
    "click generate",
    "click regenerate",
    "try again",
    "check back",
    "did not finish",
    "took too long",
    "stopped waiting",
    "stopped checking",
)


def test_shortlist_cards_entries_present_ranking_at_ceiling_stops_faking_progress(
    monkeypatch: Any, client: Any
) -> None:
    from frontend.app import _MAX_SHORTLIST_POLL_ATTEMPTS

    job_id = uuid4()
    monkeypatch.setattr(
        api_client,
        "list_shortlist",
        MagicMock(return_value=[_full_entry(uuid4())]),
    )
    monkeypatch.setattr(
        api_client,
        "get_shortlist_status",
        MagicMock(
            return_value={
                "job_id": str(job_id),
                "state": "ranking",
                "reason": None,
                "at": "2026-08-18T00:00:00+00:00",
            }
        ),
    )
    resp = client.get(
        f"/jobs/{job_id}/shortlist-cards?attempt={_MAX_SHORTLIST_POLL_ATTEMPTS}"
    )
    body = resp.get_data(as_text=True)
    normalized = _norm(body)

    assert resp.status_code == 200
    assert "hx-trigger" not in body  # the poll itself has already stopped
    assert "htmx-indicator" not in body, "no spinner once nothing is still polling"
    assert "automatically" not in normalized, "no promise the page can no longer keep"
    assert any(p in normalized for p in _HONEST_GIVE_UP_PHRASES), (
        "expected an honest give-up message once the poll ceiling is hit, "
        f"got: {normalized!r}"
    )
    assert "Candidate A" in body  # the previous run's cards stay visible


def test_shortlist_cards_entries_present_awaiting_llm_at_ceiling_stops_faking_progress(
    monkeypatch: Any, client: Any
) -> None:
    from frontend.app import _MAX_SHORTLIST_POLL_ATTEMPTS

    job_id = uuid4()
    monkeypatch.setattr(
        api_client,
        "list_shortlist",
        MagicMock(return_value=[_full_entry(uuid4())]),
    )
    monkeypatch.setattr(
        api_client,
        "get_shortlist_status",
        MagicMock(
            return_value={
                "job_id": str(job_id),
                "state": "awaiting_llm",
                "reason": "boom",
                "at": "2026-08-01T00:00:00+00:00",
            }
        ),
    )
    resp = client.get(
        f"/jobs/{job_id}/shortlist-cards?attempt={_MAX_SHORTLIST_POLL_ATTEMPTS}"
    )
    body = resp.get_data(as_text=True)
    normalized = _norm(body)

    assert resp.status_code == 200
    assert "hx-trigger" not in body
    assert "htmx-indicator" not in body, "no spinner once nothing is still polling"
    assert "automatically" not in normalized, "no promise the page can no longer keep"
    assert "no action needed" not in normalized
    assert any(p in normalized for p in _HONEST_GIVE_UP_PHRASES), (
        "expected an honest give-up message once the poll ceiling is hit, "
        f"got: {normalized!r}"
    )
    assert "Candidate A" in body


def test_shortlist_cards_entries_present_ranking_below_ceiling_still_polls_and_shows_spinner(  # noqa: E501
    monkeypatch: Any, client: Any
) -> None:
    """Negative case pinned explicitly: F3's fix must gate the banner on
    ``polling``, not simply delete the spinner/promise outright — BELOW the
    ceiling the existing (correct, already-tested) in-progress banner and
    spinner must keep rendering exactly as before."""
    job_id = uuid4()
    monkeypatch.setattr(
        api_client,
        "list_shortlist",
        MagicMock(return_value=[_full_entry(uuid4())]),
    )
    monkeypatch.setattr(
        api_client,
        "get_shortlist_status",
        MagicMock(
            return_value={
                "job_id": str(job_id),
                "state": "ranking",
                "reason": None,
                "at": "2026-08-18T00:00:00+00:00",
            }
        ),
    )
    resp = client.get(f"/jobs/{job_id}/shortlist-cards?attempt=1")
    body = resp.get_data(as_text=True)
    normalized = _norm(body)
    assert resp.status_code == 200
    assert "hx-trigger" in body
    assert "htmx-indicator" in body
    assert "previous run" in normalized
    assert "in progress" in normalized


# ── F4 (minor, on-theme) — the banner promises something untrue at the
#    retry ceiling ──────────────────────────────────────────────────────────
#
# ``shortlist_cards.html:62-65`` unconditionally says a retry "is queued
# automatically ... no action needed" whenever entries are present and
# ``state == 'awaiting_llm'``. That is only true BELOW the retry ceiling
# (``matching_tasks.py``: while ``job_try < settings.shortlist_max_tries``,
# the worker raises ``arq.Retry``). At/above the ceiling
# (``awaiting_llm_exhausted``, ``matching_tasks.py:149-154``) no retry is
# queued at all and the state persists until a successful run — ADR-029's
# design expects the recruiter to re-Generate. The status payload the
# frontend receives (``job_id``/``state``/``reason``/``at``) carries no field
# distinguishing the two cases, so the copy must not assert the
# retry-is-queued case as an unconditional fact. Pinned on wording, not a new
# state column — see the finding.


def test_shortlist_cards_entries_present_awaiting_llm_banner_never_asserts_a_retry_is_queued(  # noqa: E501
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    entry = _full_entry(uuid4())
    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=[entry]))
    monkeypatch.setattr(
        api_client,
        "get_shortlist_status",
        MagicMock(
            return_value={
                "job_id": str(job_id),
                "state": "awaiting_llm",
                "reason": "boom",
                "at": "2026-08-01T00:00:00+00:00",
            }
        ),
    )
    body = client.get(f"/jobs/{job_id}/shortlist-cards").get_data(as_text=True)
    normalized = _norm(body)
    assert (
        "no action needed" not in normalized
    ), "false at the retry ceiling -- no retry is queued there at all"
    assert (
        "is queued automatically" not in normalized
    ), "unconditional claim the frontend cannot know is true from this payload"


def test_shortlist_cards_entries_present_awaiting_llm_banner_still_advises_a_manual_fallback(  # noqa: E501
    monkeypatch: Any, client: Any
) -> None:
    """Companion to the test above: silently dropping the false certainty is
    not enough on its own — the copy must still leave the recruiter with an
    actionable fallback for the (from this payload, unknowable) exhausted
    case, e.g. mentioning the manual Generate/Regenerate action — exactly
    like the honest no-entries give-up message already does ('...click
    Generate again'). ``shortlist_cards.html`` never renders the literal word
    "generate" anywhere else (the Generate/Regenerate BUTTON lives only in
    the enclosing ``shortlist_list.html``, not this fragment), so this is a
    clean pin, not an accidental match."""
    job_id = uuid4()
    entry = _full_entry(uuid4())
    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=[entry]))
    monkeypatch.setattr(
        api_client,
        "get_shortlist_status",
        MagicMock(
            return_value={
                "job_id": str(job_id),
                "state": "awaiting_llm",
                "reason": "boom",
                "at": "2026-08-01T00:00:00+00:00",
            }
        ),
    )
    body = client.get(f"/jobs/{job_id}/shortlist-cards").get_data(as_text=True)
    normalized = _norm(body)
    assert "generate" in normalized, (
        "expected the banner to mention the manual Generate/Regenerate "
        "fallback for the case a retry was not actually queued"
    )
