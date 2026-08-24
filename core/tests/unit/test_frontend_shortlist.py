"""Slice S5 — shortlist generate + ranked candidate cards.

Covers ``api_client.generate_shortlist`` (POST /jobs/{id}/shortlist enqueue ack,
via ``httpx.MockTransport``), the Flask ``POST /jobs/<id>/shortlist`` generate
route (calls the backend then returns a pollable "Generating…" fragment) and the
``GET /jobs/<id>/shortlist-cards`` HTMX poll fragment (keeps its ``hx-trigger``
while the list is empty and DROPS it once ranked entries exist). Also asserts the
ranked-card rendering: rank, score×100, the five sub-score tiles, matched/missing
skill chips and the evidence panel — plus the graceful ``evidence=None`` fallback.

**Blind invariants (the reason this app exists):** the card renders
``display_label`` ("Candidate A"), never a real name; ``list_shortlist`` is
called with NO ``reveal`` kwarg on the card-render path; and a planted fake
name/email/phone is byte-absent from the rendered cards.

**FU-8/ADR-026 — per-card withdraw control (added below).** Each candidate
card mirrors the FU-1 reveal button pattern with a SECOND, independent
audited action: a "Withdraw candidate" form posting to ``resume_withdraw``
with ``context=shortlist``, carrying its OWN one-shot CSRF token
(``action="withdraw"``, distinct from the same card's reveal token — see
``test_frontend_csrf.py``'s "tokens scoped by (résumé id, action)" section).

**FU-7 §2 (ADR-021 §2 / ADR-029) — fail-closed ``awaiting_llm`` state (added
below).** ``api_client.get_shortlist_status`` does not exist yet — every new
test that overrides it via ``monkeypatch.setattr`` (default ``raising=True``)
fails at that call with ``AttributeError`` until it's added. The autouse
``_default_shortlist_status`` fixture below stubs a harmless "no state" shape
with ``raising=False`` so every EXISTING test in this file (written before the
``awaiting_llm`` state existed) keeps passing once ``shortlist_cards`` starts
consulting the status endpoint unconditionally — a job with no fail-closed
state at all is overwhelmingly the common case this whole file exercises.

**"Why this rank?" defense pack, slice 1 (added below).** The shortlist
ENTRY DETAIL page (``GET /shortlist/<entry_id>``, ``shortlist_entry.html``) is
currently a 16-line stub — rank/label/score-final and a résumé link, nothing
else. This slice adds a deterministic score-composition table (top-level
structured/evidence/motivation, each with weight × sub-score = contribution,
plus the five structured sub-rows) and a verified-evidence panel
(met/partial/missing badges per requirement, collapsible source context, a
fixed forward-direction banner). The route (``shortlist_entry_detail``) and
``api_client.get_shortlist_entry`` already exist and need no code change —
only the template. Every number asserted below is INDEPENDENTLY hand-computed
in the ``_full_entry_detail`` fixture's own comments (weights × sub-score),
not re-derived from whatever formula the template ends up using, and
``_assert_number_rendered`` tolerates several reasonable numeric renderings
(raw float str, 1/2/3-decimal, or rounded percentage) so the test pins the
ARITHMETIC, not a specific formatting choice.
"""

from __future__ import annotations

import inspect
import json
import re
from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import httpx
import pytest

from frontend import api_client
from frontend import app as frontend_app_module
from src.schemas.matching import ShortlistEntry
from src.services import explanation as explanation_module

_REAL_NAME = "Zzyzxqrst Wibblesworth"
_REAL_EMAIL = "zzyzxqrst.wibblesworth@example.test"
_REAL_PHONE = "604-555-0192"

# The two tests below exercise the REAL api_client.get_shortlist_status
# passthrough (their own httpx.MockTransport handler) -- the autouse
# _default_shortlist_status fixture below must not monkeypatch it out from
# under them.
_REAL_SHORTLIST_STATUS_TESTS = frozenset(
    {
        "test_get_shortlist_status_gets_the_status_path",
        "test_get_shortlist_status_maps_5xx_to_backend_unavailable",
    }
)


@pytest.fixture
def client(csrf_client: Any) -> Any:
    """The shared CSRF-carrying browser client (Phase 1.3).

    These tests exercise the route's BUSINESS logic and predate the
    anti-forgery guard; they now present a page token the way a real browser
    does, rather than the guard being relaxed for them. See
    ``tests/unit/conftest.py`` for why this is not autouse."""
    return csrf_client


@pytest.fixture(autouse=True)
def _default_shortlist_status(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default stub for the new FU-7 status endpoint — see module docstring.
    ``raising=False`` because ``api_client.get_shortlist_status`` does not
    exist yet on the real module; once it does, this simply overrides it with
    a benign default that every pre-existing test in this file can rely on.

    The two tests in ``_REAL_SHORTLIST_STATUS_TESTS`` exercise the REAL
    ``api_client.get_shortlist_status`` passthrough against their own
    ``httpx.MockTransport`` -- this fixture must NOT shadow it for them, or
    their handler never runs (the monkeypatched stub answers first).
    """
    if request.node.name in _REAL_SHORTLIST_STATUS_TESTS:
        return
    monkeypatch.setattr(
        api_client,
        "get_shortlist_status",
        lambda *_a, **_k: {"job_id": None, "state": None, "reason": None, "at": None},
        raising=False,
    )


def _client_with(
    handler: Callable[[httpx.Request], httpx.Response],
) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test")


def _norm(text: str) -> str:
    """Collapse all whitespace and lowercase before a substring check.

    A template line-wrap can split a phrase across a rendered newline (e.g.
    "...a minute\\n    or two..."), which would let an absence assertion pass
    merely because the phrase happens to wrap, not because it's actually
    gone. Normalizing whitespace first closes that gap.
    """
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
            "skill_contributions": [
                {
                    "skill": "PostgreSQL",
                    "score": 0.9,
                    "is_must_have": True,
                    "reason": None,
                },
                {
                    "skill": "Kubernetes",
                    "score": 0.0,
                    "is_must_have": False,
                    "reason": "missing",
                },
            ],
        },
        "evidence": {
            "requirements": [
                {
                    "requirement": "5+ years backend experience",
                    "status": "met",
                    "evidence": "Led the backend team for six years",
                    "evidence_chunk_ids": ["chunk-11", "chunk-22"],
                    "confidence": 0.92,
                },
                {
                    "requirement": "Kubernetes in production",
                    "status": "missing",
                    "evidence": "",
                    "evidence_chunk_ids": [],
                    "confidence": 0.2,
                },
            ],
            "overall_summary": "Strong backend candidate, no k8s exposure.",
        },
        "blinded": True,
        "display_label": "Candidate A",
    }


# ── api_client.generate_shortlist ────────────────────────────────────────


def test_generate_shortlist_posts_to_the_shortlist_path() -> None:
    job_id = uuid4()
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = request.url
        return httpx.Response(202, json={"job_id": str(job_id), "status": "enqueued"})

    result = api_client.generate_shortlist(job_id, client=_client_with(handler))
    assert captured["method"] == "POST"
    assert captured["url"].path == f"/jobs/{job_id}/shortlist"
    assert result == {"job_id": str(job_id), "status": "enqueued"}


def test_generate_shortlist_maps_5xx_to_backend_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "boom"})

    with pytest.raises(api_client.BackendUnavailable):
        api_client.generate_shortlist(uuid4(), client=_client_with(handler))


def test_generate_shortlist_signature_has_no_reveal_parameter() -> None:
    sig = inspect.signature(api_client.generate_shortlist)
    assert "reveal" not in sig.parameters


# ── POST /jobs/<id>/shortlist — generate route ───────────────────────────


def test_generate_route_calls_backend_and_returns_pollable_fragment(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    spy = MagicMock(return_value={"job_id": str(job_id), "status": "enqueued"})
    monkeypatch.setattr(api_client, "generate_shortlist", spy)
    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=[]))
    resp = client.post(f"/jobs/{job_id}/shortlist")
    assert resp.status_code == 200
    spy.assert_called_once()
    body = resp.get_data(as_text=True)
    assert "hx-trigger" in body  # the returned fragment polls for results
    assert "Generating" in body


def test_generate_route_backend_unavailable_is_not_a_500(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    monkeypatch.setattr(
        api_client,
        "generate_shortlist",
        MagicMock(side_effect=api_client.BackendUnavailable("down")),
    )
    resp = client.post(f"/jobs/{job_id}/shortlist")
    assert resp.status_code in (502, 503)
    assert resp.status_code != 500


# ── GET /jobs/<id>/shortlist-cards — poll fragment ───────────────────────


def test_shortlist_cards_polls_while_empty(monkeypatch: Any, client: Any) -> None:
    job_id = uuid4()
    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=[]))
    resp = client.get(f"/jobs/{job_id}/shortlist-cards")
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "hx-trigger" in body  # keeps polling
    assert "Generating" in body


def test_shortlist_cards_stops_polling_once_entries_exist(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    monkeypatch.setattr(
        api_client,
        "list_shortlist",
        MagicMock(return_value=[_full_entry(uuid4())]),
    )
    resp = client.get(f"/jobs/{job_id}/shortlist-cards")
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "hx-trigger" not in body  # polling stopped


def test_shortlist_cards_gives_up_at_the_attempt_cap(
    monkeypatch: Any, client: Any
) -> None:
    """The bounded poll: at the cap, with still no entries, it STOPS (drops
    hx-trigger) and shows a give-up message instead of polling forever."""
    from frontend.app import _MAX_SHORTLIST_POLL_ATTEMPTS

    job_id = uuid4()
    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=[]))
    body = client.get(
        f"/jobs/{job_id}/shortlist-cards?attempt={_MAX_SHORTLIST_POLL_ATTEMPTS}"
    ).get_data(as_text=True)
    assert "hx-trigger" not in body
    assert "No ranked candidates yet" in body


def test_shortlist_cards_below_cap_polls_and_increments_attempt(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=[]))
    body = client.get(f"/jobs/{job_id}/shortlist-cards?attempt=5").get_data(
        as_text=True
    )
    assert "hx-trigger" in body
    assert "attempt=6" in body  # the next poll carries an incremented counter


def test_shortlist_cards_clamps_out_of_range_attempt(
    monkeypatch: Any, client: Any
) -> None:
    """A hand-edited/garbage ``attempt`` must never crash or unbound the loop."""
    job_id = uuid4()
    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=[]))
    assert client.get(f"/jobs/{job_id}/shortlist-cards?attempt=-9").status_code == 200
    assert client.get(f"/jobs/{job_id}/shortlist-cards?attempt=abc").status_code == 200
    huge = client.get(f"/jobs/{job_id}/shortlist-cards?attempt=999999").get_data(
        as_text=True
    )
    assert "hx-trigger" not in huge  # clamped to the cap → gives up, no runaway


def test_generate_button_disabled_until_a_resume_is_parsed(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=[]))
    monkeypatch.setattr(
        api_client, "list_resumes", MagicMock(return_value=[{"status": "parsing"}])
    )
    body = client.get(f"/jobs/{job_id}/shortlist").get_data(as_text=True)
    assert "disabled" in body
    assert "hx-post" not in body  # the disabled button cannot enqueue a ranking


def test_generate_button_enabled_when_a_resume_is_parsed(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=[]))
    monkeypatch.setattr(
        api_client, "list_resumes", MagicMock(return_value=[{"status": "parsed"}])
    )
    body = client.get(f"/jobs/{job_id}/shortlist").get_data(as_text=True)
    assert "hx-post" in body  # Generate is wired once a résumé is parsed


def test_shortlist_cards_call_carries_no_reveal_kwarg(
    monkeypatch: Any, client: Any
) -> None:
    """The card-render read is unconditionally blind, exactly like the list
    read: the view must never pass ``reveal`` through on this path."""
    job_id = uuid4()
    spy = MagicMock(return_value=[])
    monkeypatch.setattr(api_client, "list_shortlist", spy)
    client.get(f"/jobs/{job_id}/shortlist-cards")
    spy.assert_called_once()
    assert "reveal" not in spy.call_args.kwargs


def test_shortlist_card_has_audited_reveal_button(
    monkeypatch: Any, client: Any
) -> None:
    """FU-1: each card carries an audited-reveal button — a POST form to the
    reveal route with ``context=shortlist`` — so identity can be revealed
    straight from the shortlist (not only from the résumé page)."""
    job_id = uuid4()
    resume_id = uuid4()
    entry = _full_entry(uuid4())
    entry["resume_id"] = str(resume_id)
    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=[entry]))
    body = client.get(f"/jobs/{job_id}/shortlist-cards").get_data(as_text=True)
    assert f"/resumes/{resume_id}/reveal" in body
    assert 'method="post"' in body.lower()
    assert 'value="shortlist"' in body
    assert "Reveal identity" in body


def test_shortlist_cards_404s_when_job_missing(monkeypatch: Any, client: Any) -> None:
    monkeypatch.setattr(
        api_client,
        "list_shortlist",
        MagicMock(side_effect=api_client.NotFound("no job")),
    )
    resp = client.get(f"/jobs/{uuid4()}/shortlist-cards")
    assert resp.status_code == 404


# ── honest progress copy + elapsed indicator (fix/upload-and-progress-ux) ──
#
# Diagnosed live: shortlist ranking runs the evidence model on EVERY candidate
# résumé it considers (per-candidate evidence calls on a local model, ~1-2 min
# PER candidate), so a full run realistically takes several minutes, scaling
# with the number of résumés — not "large PDFs can take a minute or two to
# parse" as the old copy claimed (which in fact described résumé PARSING, a
# different, already-finished step, not the ranking job itself). That undersell
# reads as "not doing much" / broken. This fix is copy + a live elapsed-time
# signal only (no backend/server-state change in this slice — a fully
# server-durable timestamp, so the indicator survives a hard reload, stays out
# of scope). Pure Flask/Jinja rendering over a mocked api_client — offline
# suffices, no integration test needed. See the identical mirror in
# test_frontend_reverse_match.py.


def test_shortlist_cards_still_finding_shows_honest_multi_minute_copy(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=[]))
    body = client.get(f"/jobs/{job_id}/shortlist-cards").get_data(as_text=True)
    assert "several minutes" in _norm(body)
    assert "every candidate" in _norm(body)


def test_shortlist_cards_still_finding_drops_the_old_minute_or_two_phrasing(
    monkeypatch: Any, client: Any
) -> None:
    """Whitespace-normalized so a template line-wrap can't hide the old phrase
    (it currently wraps as '...a minute\\n    or two...')."""
    job_id = uuid4()
    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=[]))
    body = client.get(f"/jobs/{job_id}/shortlist-cards").get_data(as_text=True)
    assert "minute or two" not in _norm(body)


def test_shortlist_cards_elapsed_indicator_at_attempt_zero_shows_a_sane_start(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=[]))
    body = client.get(f"/jobs/{job_id}/shortlist-cards").get_data(as_text=True)
    assert "elapsed" in _norm(body)
    assert "0s" in _norm(body) or "just started" in _norm(body)


def test_shortlist_cards_elapsed_indicator_scales_with_attempt(
    monkeypatch: Any, client: Any
) -> None:
    """The poll re-fires every 3s, so at attempt=40 roughly 120s (~2 min) have
    passed — the fragment must reflect that moving figure, not a static hint."""
    job_id = uuid4()
    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=[]))
    body = client.get(f"/jobs/{job_id}/shortlist-cards?attempt=40").get_data(
        as_text=True
    )
    normalized = _norm(body)
    assert "elapsed" in normalized
    assert "120s" in normalized or "2 min" in normalized or "2:00" in normalized


def test_shortlist_cards_non_empty_has_no_elapsed_or_poll_trigger(
    monkeypatch: Any, client: Any
) -> None:
    """Regression guard: once ranked entries exist, the fragment is terminal —
    no elapsed line, no poll trigger."""
    job_id = uuid4()
    monkeypatch.setattr(
        api_client,
        "list_shortlist",
        MagicMock(return_value=[_full_entry(uuid4())]),
    )
    body = client.get(f"/jobs/{job_id}/shortlist-cards").get_data(as_text=True)
    assert "hx-trigger" not in body
    assert "elapsed" not in _norm(body)
    assert "still working" not in _norm(body)


def test_shortlist_cards_gave_up_has_no_elapsed_or_poll_trigger(
    monkeypatch: Any, client: Any
) -> None:
    from frontend.app import _MAX_SHORTLIST_POLL_ATTEMPTS

    job_id = uuid4()
    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=[]))
    body = client.get(
        f"/jobs/{job_id}/shortlist-cards?attempt={_MAX_SHORTLIST_POLL_ATTEMPTS}"
    ).get_data(as_text=True)
    assert "hx-trigger" not in body
    assert "elapsed" not in _norm(body)
    assert "still working" not in _norm(body)


# ── ranked-card rendering ────────────────────────────────────────────────


def test_card_renders_rank_score_tiles_chips_and_evidence(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    monkeypatch.setattr(
        api_client,
        "list_shortlist",
        MagicMock(return_value=[_full_entry(uuid4())]),
    )
    body = client.get(f"/jobs/{job_id}/shortlist-cards").get_data(as_text=True)
    # rank + display label
    assert "Candidate A" in body
    assert "1" in body
    # final score = round(0.87 * 100)
    assert "87" in body
    # five sub-score tiles
    for label in ("skill", "experience", "education", "seniority", "vector"):
        assert label in body.lower()
    # matched + missing skill chips
    assert "PostgreSQL" in body
    assert "Kubernetes" in body
    assert "missing" in body.lower()
    # evidence panel: quoted evidence, chunk ids, overall summary
    assert "Led the backend team for six years" in body
    assert "chunk-11" in body
    assert "Strong backend candidate, no k8s exposure." in body


def test_card_flags_must_have_skills(monkeypatch: Any, client: Any) -> None:
    job_id = uuid4()
    monkeypatch.setattr(
        api_client,
        "list_shortlist",
        MagicMock(return_value=[_full_entry(uuid4())]),
    )
    body = client.get(f"/jobs/{job_id}/shortlist-cards").get_data(as_text=True)
    assert "must" in body.lower()  # must-have flag surfaced somewhere


def test_card_with_null_evidence_renders_fallback_without_crashing(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    entry = _full_entry(uuid4())
    entry["evidence"] = None
    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=[entry]))
    resp = client.get(f"/jobs/{job_id}/shortlist-cards")
    assert resp.status_code == 200
    assert "Evidence not available for this candidate" in resp.get_data(as_text=True)


def test_card_with_empty_evidence_requirements_renders_fallback(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    entry = _full_entry(uuid4())
    entry["evidence"] = {"requirements": [], "overall_summary": ""}
    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=[entry]))
    resp = client.get(f"/jobs/{job_id}/shortlist-cards")
    assert resp.status_code == 200
    assert "Evidence not available for this candidate" in resp.get_data(as_text=True)


# ── blind invariants ─────────────────────────────────────────────────────


def test_card_uses_display_label_never_a_real_name(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    entry = _full_entry(uuid4())
    # Plant PII on fields the card must never surface.
    entry["candidate_name"] = _REAL_NAME
    entry["candidate"] = {
        "name": _REAL_NAME,
        "email": _REAL_EMAIL,
        "phone": _REAL_PHONE,
    }
    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=[entry]))
    raw = client.get(f"/jobs/{job_id}/shortlist-cards").get_data(as_text=True)
    assert "Candidate A" in raw
    assert _REAL_NAME not in raw
    assert _REAL_EMAIL not in raw
    assert _REAL_PHONE not in raw


def test_shortlist_list_page_shows_generate_button_and_export_link(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=[]))
    monkeypatch.setattr(
        api_client, "list_resumes", MagicMock(return_value=[{"status": "parsed"}])
    )
    body = client.get(f"/jobs/{job_id}/shortlist").get_data(as_text=True)
    # Generate button posts to the generate route.
    assert f"/jobs/{job_id}/shortlist" in body
    assert "Generate" in body
    # Existing Export CSV link preserved.
    assert "Export CSV" in body
    assert f"/jobs/{job_id}/shortlist/export" in body


def test_shortlist_list_page_read_carries_no_reveal_kwarg(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    spy = MagicMock(return_value=[])
    monkeypatch.setattr(api_client, "list_shortlist", spy)
    monkeypatch.setattr(api_client, "list_resumes", MagicMock(return_value=[]))
    client.get(f"/jobs/{job_id}/shortlist")
    spy.assert_called_once()
    assert "reveal" not in spy.call_args.kwargs


# ── FU-8/ADR-026 — per-card withdraw control ──────────────────────────────


def test_shortlist_card_has_withdraw_control(monkeypatch: Any, client: Any) -> None:
    job_id = uuid4()
    resume_id = uuid4()
    entry = _full_entry(uuid4())
    entry["resume_id"] = str(resume_id)
    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=[entry]))
    body = client.get(f"/jobs/{job_id}/shortlist-cards").get_data(as_text=True)
    assert f"/resumes/{resume_id}/withdraw" in body
    assert "Withdraw candidate" in body


def test_shortlist_card_withdraw_form_is_post_method(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    resume_id = uuid4()
    entry = _full_entry(uuid4())
    entry["resume_id"] = str(resume_id)
    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=[entry]))
    body = client.get(f"/jobs/{job_id}/shortlist-cards").get_data(as_text=True)
    tag_re = re.compile(r'<form([^>]*action="[^"]*/withdraw[^"]*"[^>]*)>')
    match = tag_re.search(body)
    assert match is not None, "expected a withdraw <form> on the shortlist card"
    assert "post" in match.group(1).lower()


def test_shortlist_card_withdraw_form_carries_context_shortlist(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    resume_id = uuid4()
    entry = _full_entry(uuid4())
    entry["resume_id"] = str(resume_id)
    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=[entry]))
    body = client.get(f"/jobs/{job_id}/shortlist-cards").get_data(as_text=True)
    form_re = re.compile(
        r'<form[^>]*action="[^"]*/withdraw[^"]*"[^>]*>(.*?)</form>', re.DOTALL
    )
    match = form_re.search(body)
    assert match is not None, "expected a withdraw <form> on the shortlist card"
    assert 'value="shortlist"' in match.group(1)


def test_shortlist_card_withdraw_token_is_independent_of_the_reveal_token(
    monkeypatch: Any, client: Any
) -> None:
    """Two audited actions on the same card must never share a CSRF slot:
    the reveal form's token and the withdraw form's token, both rendered for
    the SAME card's résumé id, must be different values."""
    job_id = uuid4()
    resume_id = uuid4()
    entry = _full_entry(uuid4())
    entry["resume_id"] = str(resume_id)
    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=[entry]))
    body = client.get(f"/jobs/{job_id}/shortlist-cards").get_data(as_text=True)

    reveal_form = re.search(
        r'<form[^>]*action="[^"]*/reveal[^"]*"[^>]*>(.*?)</form>', body, re.DOTALL
    )
    withdraw_form = re.search(
        r'<form[^>]*action="[^"]*/withdraw[^"]*"[^>]*>(.*?)</form>', body, re.DOTALL
    )
    assert reveal_form is not None, "expected a reveal <form> on the shortlist card"
    assert withdraw_form is not None, "expected a withdraw <form> on the shortlist card"

    token_re = re.compile(r'name="csrf_token"[^>]*value="([^"]+)"')
    reveal_token = token_re.search(reveal_form.group(1))
    withdraw_token = token_re.search(withdraw_form.group(1))
    assert reveal_token is not None, "expected a csrf_token input in the reveal form"
    assert (
        withdraw_token is not None
    ), "expected a csrf_token input in the withdraw form"
    assert reveal_token.group(1) != withdraw_token.group(1)


def test_shortlist_card_withdraw_control_uses_display_label_context_never_pii(
    monkeypatch: Any, client: Any
) -> None:
    """Blind invariant carried over: the withdraw control itself must not leak
    identity even when a fake name/email/phone is planted on the entry."""
    job_id = uuid4()
    resume_id = uuid4()
    entry = _full_entry(uuid4())
    entry["resume_id"] = str(resume_id)
    entry["candidate"] = {
        "name": _REAL_NAME,
        "email": _REAL_EMAIL,
        "phone": _REAL_PHONE,
    }
    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=[entry]))
    raw = client.get(f"/jobs/{job_id}/shortlist-cards").get_data(as_text=True)
    assert f"/resumes/{resume_id}/withdraw" in raw
    assert _REAL_NAME not in raw
    assert _REAL_EMAIL not in raw
    assert _REAL_PHONE not in raw


# ── FU-7 §2 (ADR-021 §2 / ADR-029) — fail-closed "awaiting_llm" state ──────
#
# ``api_client.get_shortlist_status`` (new passthrough) and the
# ``shortlist_cards.html`` awaiting_llm branch don't exist yet. Every
# ``monkeypatch.setattr(api_client, "get_shortlist_status", ...)`` call below
# uses the DEFAULT ``raising=True`` (unlike the autouse fixture above), so it
# fails with ``AttributeError`` until the attribute is added — that failure
# IS the RED signal for these tests specifically.


def test_get_shortlist_status_gets_the_status_path() -> None:
    job_id = uuid4()
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = request.url
        return httpx.Response(
            200,
            json={"job_id": str(job_id), "state": None, "reason": None, "at": None},
        )

    result = api_client.get_shortlist_status(job_id, client=_client_with(handler))
    assert captured["method"] == "GET"
    assert captured["url"].path == f"/jobs/{job_id}/shortlist/status"
    assert result["state"] is None


def test_get_shortlist_status_maps_5xx_to_backend_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "boom"})

    with pytest.raises(api_client.BackendUnavailable):
        api_client.get_shortlist_status(uuid4(), client=_client_with(handler))


def test_shortlist_cards_calls_get_shortlist_status(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=[]))
    spy = MagicMock(
        return_value={"job_id": str(job_id), "state": None, "reason": None, "at": None}
    )
    monkeypatch.setattr(api_client, "get_shortlist_status", spy)
    client.get(f"/jobs/{job_id}/shortlist-cards")
    spy.assert_called_once()


def test_shortlist_cards_shows_awaiting_llm_message_and_keeps_polling(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=[]))
    monkeypatch.setattr(
        api_client,
        "get_shortlist_status",
        MagicMock(
            return_value={
                "job_id": str(job_id),
                "state": "awaiting_llm",
                "reason": "llm output invalid: empty response",
                "at": "2026-08-01T00:00:00+00:00",
            }
        ),
    )
    resp = client.get(f"/jobs/{job_id}/shortlist-cards")
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "Waiting for AI to rank candidates" in body
    assert "hx-trigger" in body  # still polling — a retry is queued server-side


def test_shortlist_cards_no_awaiting_message_when_state_is_null(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=[]))
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
    body = client.get(f"/jobs/{job_id}/shortlist-cards").get_data(as_text=True)
    assert "Waiting for AI to rank candidates" not in body
    assert "hx-trigger" in body  # ordinary "still generating" path, unchanged


def test_shortlist_cards_entries_present_and_awaiting_llm_keeps_polling(
    monkeypatch: Any, client: Any
) -> None:
    """Reviewer finding on fix/regenerate-shortlist-no-feedback (PR review,
    2026-08-18): a Regenerate whose LLM call fails closed calls
    ``set_shortlist_awaiting_llm`` and returns WITHOUT ever reaching
    ``persist_shortlist`` (ADR-029's worker mechanism) — so the PREVIOUS run's
    entries are still on the row when the status endpoint reports
    ``state == 'awaiting_llm'``. That is the reported no-feedback bug one
    state over: ``ranking`` is false (the state is 'awaiting_llm', not
    'ranking'), so the old ``polling = (a < m) and ((not entries) or ranking)``
    computes False, the poll silently stops, and the recruiter is left on a
    stale list with no sign a retry is queued.

    ADR-029's own rationale ("a degraded ranking that reaches human eyes is
    worse than no ranking" / "visibility over silence") and the identical
    'ranking'-state fix immediately above ("stale-but-labelled beats blank")
    both point the same way: a retry IS queued server-side here (the worker
    raises ``arq.Retry`` below the retry ceiling — ADR-029's mechanism
    section), so polling must continue and the recruiter must be told a
    retry is in flight, exactly as it already is for the 'ranking' case.

    **Corrected from a prior assertion.** This test previously asserted the
    OPPOSITE — no trigger, no banner — on the premise that entries could only
    be non-empty here because a stale flag lingered AFTER a successful
    retry. That premise doesn't hold: ADR-029 clears ``shortlist_state`` in
    the SAME transaction as a successful persist, so a genuinely-stale
    post-success flag is not a reachable state, while a currently-in-flight
    fail-closed retry with old entries still present (the Regenerate case
    above) is the dominant real one. The two are indistinguishable from this
    template's inputs alone, so the fail-safe default (poll + disclose) must
    win.
    """
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
    resp = client.get(f"/jobs/{job_id}/shortlist-cards")
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "hx-trigger" in body, "a retry is queued server-side — polling must continue"
    assert "Candidate A" in body, "the previous run's cards must still render"


def test_shortlist_cards_entries_present_and_awaiting_llm_banner_precedes_stale_cards(
    monkeypatch: Any, client: Any
) -> None:
    """Companion to the test above: the "waiting for AI / retry queued"
    banner must render ABOVE the stale cards (so a recruiter reads the
    explanation before the possibly-outdated list), and the cards themselves
    must still be intact underneath it — never blanked in favour of the
    banner."""
    job_id = uuid4()
    resume_id = uuid4()
    entry = _full_entry(uuid4())
    entry["resume_id"] = str(resume_id)
    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=[entry]))
    monkeypatch.setattr(
        api_client,
        "get_shortlist_status",
        MagicMock(
            return_value={
                "job_id": str(job_id),
                "state": "awaiting_llm",
                "reason": "llm output invalid: empty response",
                "at": "2026-08-01T00:00:00+00:00",
            }
        ),
    )
    body = client.get(f"/jobs/{job_id}/shortlist-cards").get_data(as_text=True)
    normalized = _norm(body)

    assert "waiting for ai to rank candidates" in normalized
    assert "retry" in normalized  # "...queued to retry automatically..."
    assert "candidate a" in normalized
    # Ordering: the banner text must precede the card content, not follow it.
    banner_idx = normalized.find("waiting for ai to rank candidates")
    card_idx = normalized.find("candidate a")
    assert banner_idx != -1 and card_idx != -1
    assert (
        banner_idx < card_idx
    ), "the awaiting_llm banner must render ABOVE the stale cards, not below"


def test_shortlist_cards_404s_when_status_endpoint_reports_missing_job(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=[]))
    monkeypatch.setattr(
        api_client,
        "get_shortlist_status",
        MagicMock(side_effect=api_client.NotFound("no job")),
    )
    resp = client.get(f"/jobs/{job_id}/shortlist-cards")
    assert resp.status_code == 404


# ── fix/regenerate-shortlist-no-feedback — poll on a real 'ranking' state ──
#
# Root cause (measured live): shortlist_cards.html gates its htmx poll on
# ``not entries`` — true only on first Generate. On Regenerate, entries
# already exist, so the returned fragment carries NO hx-trigger and silently
# re-renders the stale list while the real run grinds on for minutes. The fix
# records a real ``'ranking'`` state on ``jobs.shortlist_state`` (set by the
# API route before it even enqueues) and polls on THAT fact instead of
# inferring "empty == in progress".
#
# ``api_client.get_shortlist_status`` already exists (FU-7 §2 above); what's
# new here is (1) ``_render_shortlist_cards`` must fetch it UNCONDITIONALLY —
# today it only fetches when ``entries`` is empty (frontend/app.py:904), so
# with entries present the spy below is never even called — and (2) the
# template must treat ``state == 'ranking'`` as "still polling" even when
# ``entries`` is non-empty. Every test in this section fails against today's
# code for exactly that reason (not a broken test): with entries present, the
# fragment renders the terminal (non-polling) branch unconditionally,
# regardless of what the status endpoint reports.


def test_shortlist_cards_entries_present_and_ranking_shows_trigger_banner_and_old_cards(  # noqa: E501
    monkeypatch: Any, client: Any
) -> None:
    """THE regression guard for the actually-reported bug: Regenerate with a
    prior shortlist already on screen. Entries present + a real 'ranking' run
    in flight must (a) keep the htmx poll trigger, (b) show a clearly-worded
    banner explaining a new run is in progress, and (c) still render the
    previous run's cards — stale-but-labelled, never blank."""
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
    resp = client.get(f"/jobs/{job_id}/shortlist-cards")
    body = resp.get_data(as_text=True)
    normalized = _norm(body)

    assert resp.status_code == 200
    assert "hx-trigger" in body, "a run is in flight — polling must continue"
    # A clearly-worded banner: the cards below are the PREVIOUS run, a NEW one
    # is in progress. Phrasing lifted straight from the spec's own wording so
    # the assertion pins the required MEANING, not an incidental rewrite.
    assert "previous run" in normalized
    assert "in progress" in normalized
    assert "several minutes" in normalized  # same honest-duration copy as Generate
    # The stale cards themselves must still render — never blanked.
    assert "Candidate A" in body
    assert "87" in body  # score_final = 0.87 -> round(87)


def test_shortlist_cards_entries_present_and_not_ranking_has_no_trigger(
    monkeypatch: Any, client: Any
) -> None:
    """The negative case, pinned explicitly: entries present and NO run in
    flight must NOT trigger a poll — trading the no-feedback bug for an
    infinite poll is exactly as broken as the original bug."""
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
                "state": None,
                "reason": None,
                "at": None,
            }
        ),
    )
    resp = client.get(f"/jobs/{job_id}/shortlist-cards")
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "hx-trigger" not in body
    assert "Candidate A" in body


def test_shortlist_cards_no_entries_no_ranking_state_still_shows_generating(
    monkeypatch: Any, client: Any
) -> None:
    """No entries, status carries no state at all -> the ordinary first-
    Generate path, byte-for-byte unchanged by the 'ranking' branch existing."""
    job_id = uuid4()
    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=[]))
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
    body = client.get(f"/jobs/{job_id}/shortlist-cards").get_data(as_text=True)
    assert "hx-trigger" in body
    assert "Generating" in body


def test_shortlist_cards_fetches_status_even_when_entries_present(
    monkeypatch: Any, client: Any
) -> None:
    """``_render_shortlist_cards`` must consult the status endpoint
    UNCONDITIONALLY (frontend/app.py currently gates the call on ``not
    entries`` — see the module comment above), because a Regenerate has
    entries present AND a run in flight at the same time."""
    job_id = uuid4()
    monkeypatch.setattr(
        api_client,
        "list_shortlist",
        MagicMock(return_value=[_full_entry(uuid4())]),
    )
    spy = MagicMock(
        return_value={"job_id": str(job_id), "state": None, "reason": None, "at": None}
    )
    monkeypatch.setattr(api_client, "get_shortlist_status", spy)
    client.get(f"/jobs/{job_id}/shortlist-cards")
    spy.assert_called_once()


def test_shortlist_cards_status_backend_unavailable_with_entries_degrades_gracefully(
    monkeypatch: Any, client: Any
) -> None:
    """A transient outage on JUST the status endpoint, with entries present,
    must never 500 and must never crash — it degrades to the ordinary
    (non-polling) rendering of the cards already in hand."""
    job_id = uuid4()
    monkeypatch.setattr(
        api_client,
        "list_shortlist",
        MagicMock(return_value=[_full_entry(uuid4())]),
    )
    spy = MagicMock(side_effect=api_client.BackendUnavailable("down"))
    monkeypatch.setattr(api_client, "get_shortlist_status", spy)
    resp = client.get(f"/jobs/{job_id}/shortlist-cards")
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    # A test that passes merely because the fetch was never attempted would
    # prove nothing about graceful degradation -- assert it really was called.
    spy.assert_called_once()
    assert "Candidate A" in body
    assert "hx-trigger" not in body


# ── "Why this rank?" defense pack, slice 1 — entry detail panel ───────────
#
# GET /shortlist/<entry_id> (shortlist_entry_detail / shortlist_entry.html).
# The route already exists and needs NO code change — only the template.
# ``api_client.get_shortlist_entry`` returns the raw JSON dict the FastAPI
# backend sends back (already extended, per the read-path RED tests in
# test_services_shortlist_read.py / test_services_explanation.py, with
# score_structured/score_evidence/pipeline_meta); this file exercises the
# TEMPLATE only, over a hand-built dict standing in for that response.

# Deliberately share NO token with ``_REAL_NAME``: ``redact_text`` masks the
# name (and each of its parts) BEFORE the employer/school term_map runs, so an
# employer literally containing "Wibblesworth" would come out as
# "[name redacted] Aerodynamics Ltd" and the "Employer A" label assertions
# below would be testing the name pass, not the term_map pass.
_REAL_EMPLOYER = "Quibbleflux Aerodynamics Ltd"
_REAL_SCHOOL = "Marrowbank Institute of Technology"
_SOURCE_CONTEXT_MARKER = "SOURCE-CONTEXT-MARKER-full-chunk-text-about-python-work"

# Hand-computed contribution table for the fixture below (weight * score):
#
#   top level:
#     structured: weight=0.50, score=0.68, contribution=0.34
#     evidence:   weight=0.30, score=0.80, contribution=0.24
#     motivation: weight=0.20, score=0.60, contribution=0.12
#     sum(contribution) = 0.70 == score_final
#
#   structured sub-rows:
#     skill:       weight=0.40, score=0.80, contribution=0.32
#     experience:  weight=0.25, score=0.60, contribution=0.15
#     education:   weight=0.15, score=0.40, contribution=0.06
#     seniority:   weight=0.10, score=0.50, contribution=0.05
#     vector:      weight=0.10, score=1.00, contribution=0.10
#     sum(contribution) = 0.68 == score_breakdown.structured

_ENTRY_WEIGHTS = {
    "structured": 0.50,
    "evidence": 0.30,
    "motivation": 0.20,
    "skill": 0.40,
    "experience": 0.25,
    "education": 0.15,
    "seniority": 0.10,
    "vector": 0.10,
    "must_have_miss_penalty": 0.5,
    "implied_experience_relief": 0.75,
    "recency_recent_years": 2,
    "recency_mid_years": 5,
    "recency_recent": 1.0,
    "recency_mid": 0.7,
    "recency_old": 0.4,
    "overqual_ratio": 2.0,
    "overqual_slope": 0.1,
    "overqual_floor": 0.8,
    "education_partial": 0.5,
    "education_field_fuzz": 0.85,
    "seniority_floor": 0.5,
    "implied_seniority_factor": 1.5,
    "implied_min_coverage": 0.5,
    "evidence_met_confidence": 0.7,
    "evidence_partial_weight": 0.5,
    "evidence_verify_fuzz": 0.85,
    "evidence_min_quote_chars": 16,
    "motivation_min_confidence": 0.7,
}


def _full_entry_detail(entry_id: Any) -> dict[str, Any]:
    return {
        "id": str(entry_id),
        "job_id": str(uuid4()),
        "resume_id": str(uuid4()),
        "rank": 2,
        "score_final": 0.70,
        "score_structured": 0.68,
        "score_evidence": 0.80,
        "score_breakdown": {
            "skill": 0.80,
            "experience": 0.60,
            "education": 0.40,
            "seniority": 0.50,
            "vector": 1.00,
            "structured": 0.68,
            "motivation": 0.60,
            "implied_experience": False,
            "skill_contributions": [],
        },
        "evidence": {
            "requirements": [
                {
                    "requirement": "Python",
                    "status": "met",
                    "evidence": "Built the payments service in Python for four years.",
                    "evidence_chunk_ids": ["c_001"],
                    "confidence": 0.92,
                    "source_context": _SOURCE_CONTEXT_MARKER,
                },
                {
                    "requirement": "Kubernetes",
                    "status": "partial",
                    "evidence": "Some exposure to Helm charts.",
                    "evidence_chunk_ids": ["c_002"],
                    "confidence": 0.55,
                },
                {
                    # Demoted by verify_evidence (anti-fabrication scrub):
                    # quote blanked, status demoted met -> missing, confidence
                    # capped at SCRUBBED_CONFIDENCE_CAP (0.3).
                    "requirement": "AWS certification",
                    "status": "missing",
                    "evidence": "",
                    "evidence_chunk_ids": ["c_003"],
                    "confidence": 0.3,
                },
            ],
            "overall_summary": "Strong backend candidate overall.",
            "cover_letter_presence": False,
            "cover_letter_evidence": [],
            "overall_motivation": "",
        },
        "pipeline_meta": {
            "model_gen": "gpt-oss:20b",
            "model_emb": "nomic-embed-text",
            "prompt_versions": {"shortlist_evidence": "shortlist_evidence_v1"},
            "weights": _ENTRY_WEIGHTS,
            "git_sha": "deadbeef",
            "generated_at": "2026-07-15T00:00:00+00:00",
            "timings_ms": {},
        },
        "generated_at": "2026-07-15T00:00:00+00:00",
        "blinded": True,
        "display_label": "Candidate B",
    }


def _num_variants(value: float) -> list[str]:
    """Every reasonable textual rendering of ``value`` this test will accept:
    the raw float repr, 1/2/3-decimal fixed formatting, and a rounded
    percentage integer. Tolerates the coder's formatting choice while still
    pinning the underlying ARITHMETIC — a wrong weight or sub-score produces
    a value whose variants don't match any of these."""
    return list(
        {
            str(value),
            f"{value:.1f}",
            f"{value:.2f}",
            f"{value:.3f}",
            str(int(round(value * 100))),
        }
    )


_CONTRIBUTION_TABLE_RE = re.compile(
    r'<table[^>]*class="[^"]*contribution-table[^"]*"[^>]*>(.*?)</table>',
    re.DOTALL,
)


def _contribution_html(body: str) -> str:
    """ONLY the contribution tables, not the whole page.

    Substring-searching the whole body for a 2-digit percentage is close to
    vacuous: the page carries a random ``resume_id`` UUID, so "68" has a real
    chance of matching random hex and passing an assertion that the number was
    rendered when the table is in fact wrong (or absent). Scoping the haystack
    to the ``contribution-table`` blocks removes that accident."""
    blocks = _CONTRIBUTION_TABLE_RE.findall(body)
    assert blocks, "no contribution-table block rendered on the entry page"
    return "\n".join(blocks)


def _contribution_rows(body: str) -> list[tuple[str, str, str, str]]:
    """The contribution tables' data rows as
    ``(component, weight, score, contribution)`` cell text -- so a column can
    be asserted on directly instead of substring-searching for a number that
    another column legitimately also contains."""
    rows: list[tuple[str, str, str, str]] = []
    for tr in re.findall(r"<tr>(.*?)</tr>", _contribution_html(body), re.DOTALL):
        cells = [c.strip() for c in re.findall(r"<td>(.*?)</td>", tr, re.DOTALL)]
        if len(cells) == 4:
            rows.append((cells[0], cells[1], cells[2], cells[3]))
    return rows


def _assert_number_rendered(body: str, value: float, *, label: str) -> None:
    haystack = _contribution_html(body)
    variants = _num_variants(value)
    assert any(
        v in haystack for v in variants
    ), f"expected {label} ({value}) to render as one of {variants}"


def test_entry_detail_renders_contribution_table(monkeypatch: Any, client: Any) -> None:
    """The top-level score composition (structured/evidence/motivation), each
    with its GENERATION-TIME weight, its own sub-score, and
    weight * score = contribution, must be visible on the entry detail page."""
    entry_id = uuid4()
    entry = _full_entry_detail(entry_id)
    monkeypatch.setattr(
        api_client, "get_shortlist_entry", MagicMock(return_value=entry)
    )
    resp = client.get(f"/shortlist/{entry_id}")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    normalized = _norm(body)
    for label in ("structured", "evidence", "motivation"):
        assert label in normalized
    # structured: weight 0.50, score 0.68, contribution 0.34
    _assert_number_rendered(body, 0.50, label="structured weight")
    _assert_number_rendered(body, 0.68, label="structured score")
    _assert_number_rendered(body, 0.34, label="structured contribution")
    # evidence: weight 0.30, score 0.80, contribution 0.24
    _assert_number_rendered(body, 0.30, label="evidence weight")
    _assert_number_rendered(body, 0.24, label="evidence contribution")
    # motivation: weight 0.20, score 0.60, contribution 0.12
    _assert_number_rendered(body, 0.20, label="motivation weight")
    _assert_number_rendered(body, 0.12, label="motivation contribution")


def test_entry_detail_renders_structured_sub_contribution_rows(
    monkeypatch: Any, client: Any
) -> None:
    entry_id = uuid4()
    entry = _full_entry_detail(entry_id)
    monkeypatch.setattr(
        api_client, "get_shortlist_entry", MagicMock(return_value=entry)
    )
    resp = client.get(f"/shortlist/{entry_id}")
    body = resp.get_data(as_text=True)
    normalized = _norm(body)
    for label in ("skill", "experience", "education", "seniority", "vector"):
        assert label in normalized
    # skill: weight 0.40, score 0.80, contribution 0.32
    _assert_number_rendered(body, 0.40, label="skill weight")
    _assert_number_rendered(body, 0.32, label="skill contribution")
    # education: weight 0.15, score 0.40, contribution 0.06
    _assert_number_rendered(body, 0.15, label="education weight")
    _assert_number_rendered(body, 0.06, label="education contribution")
    # seniority: weight 0.10, contribution 0.05
    _assert_number_rendered(body, 0.05, label="seniority contribution")


def test_entry_detail_renders_requirement_status_badges(
    monkeypatch: Any, client: Any
) -> None:
    """met/partial/missing badges per requirement, mirroring the
    ``badge badge-{status}`` convention already used on the candidate card
    (shortlist_cards.html). The demoted AWS row must render as missing —
    NEVER as met, which would silently re-fabricate the confidence the
    anti-fabrication scrub just stripped out."""
    entry_id = uuid4()
    entry = _full_entry_detail(entry_id)
    monkeypatch.setattr(
        api_client, "get_shortlist_entry", MagicMock(return_value=entry)
    )
    resp = client.get(f"/shortlist/{entry_id}")
    body = resp.get_data(as_text=True)
    assert body.count("badge-met") == 1
    assert body.count("badge-partial") == 1
    assert body.count("badge-missing") == 1

    aws_idx = body.find("AWS certification")
    assert aws_idx != -1
    nearby = body[max(0, aws_idx - 400) : aws_idx + 400]
    assert "badge-missing" in nearby
    assert "badge-met" not in nearby


def test_entry_detail_renders_forward_direction_banner(
    monkeypatch: Any, client: Any
) -> None:
    """Guards the ADR-009 reverse-match 0.9-cap ambiguity: this panel must
    state, unambiguously, that it explains the ranking FOR this job's
    requirements (not "what jobs suit this candidate" — the reverse-match
    direction lives on a different page entirely)."""
    entry_id = uuid4()
    entry = _full_entry_detail(entry_id)
    monkeypatch.setattr(
        api_client, "get_shortlist_entry", MagicMock(return_value=entry)
    )
    resp = client.get(f"/shortlist/{entry_id}")
    body = resp.get_data(as_text=True)
    assert "for this job's requirements" in _norm(body)


def test_entry_detail_source_context_is_collapsible(
    monkeypatch: Any, client: Any
) -> None:
    """Matches the ``<details class="source-context">`` house style already
    used on the candidate card (shortlist_cards.html) — the resolved chunk
    text must NOT render inline by default."""
    entry_id = uuid4()
    entry = _full_entry_detail(entry_id)
    monkeypatch.setattr(
        api_client, "get_shortlist_entry", MagicMock(return_value=entry)
    )
    resp = client.get(f"/shortlist/{entry_id}")
    body = resp.get_data(as_text=True)
    assert _SOURCE_CONTEXT_MARKER in body

    found_in_details = False
    for m in re.finditer(r"<details[^>]*>(.*?)</details>", body, re.DOTALL):
        if _SOURCE_CONTEXT_MARKER in m.group(1):
            found_in_details = True
            break
    assert found_in_details, (
        "source_context must render inside a collapsible <details> block, "
        "not inline by default"
    )


def test_entry_detail_black_box_pii_scan_under_blind_job(
    monkeypatch: Any, client: Any
) -> None:
    """Downstream scan of the fields the panel ACTUALLY renders.

    **This test does not prove redaction, and must not be read as if it did.**
    THE REDACTION BOUNDARY IS SERVER-SIDE, in
    ``shortlist_service._row_to_blind_entry``, which scrubs the evidence BEFORE
    the ``ShortlistEntry`` DTO is built (ADR-006 §4). Everything in this file
    monkeypatches ``api_client.get_shortlist_entry``, so the route renders
    whatever dict it is handed and can only ever be downstream of that
    boundary. The end-to-end guard that DOES prove redaction — a raw-PII row
    driven through the real ``_row_to_blind_entry`` and then rendered — is
    ``test_entry_detail_real_blind_read_renders_no_pii`` immediately below;
    that one fails if the redaction call loses its arguments, this one does
    not.

    What this test honestly pins is two things:

    1. **The whitelist.** Extra top-level candidate keys on the payload
       (``candidate_name`` / ``candidate`` / the ``_c_*`` join aliases) are
       dropped by ``_SHORTLIST_ENTRY_FIELDS`` and structurally cannot ride a
       ``ShortlistEntry``, so none of them can reach the page.
    2. **Marker fidelity on the rendered surface.** The panel renders the
       requirement quote and its resolved ``source_context`` verbatim — that
       is the feature, so planting RAW PII in a quote and asserting its
       absence would fail by design and would be a dishonest assertion to
       write. Instead the evidence here is what the SERVER would emit: already
       redacted. The page must carry those redaction markers through intact
       (``[name redacted]`` / ``[contact redacted]`` / ``Employer A`` /
       ``Institution A``) and must not resurrect the underlying identity from
       any other source — a template that reached around the DTO for a raw
       field, or dropped the scrubbed text in favour of an unscrubbed one,
       breaks here.
    """
    entry_id = uuid4()
    entry = _full_entry_detail(entry_id)
    # (1) The extra-top-level-key plants: dropped by the whitelist.
    entry["candidate_name"] = _REAL_NAME
    entry["candidate"] = {
        "name": _REAL_NAME,
        "email": _REAL_EMAIL,
        "phone": _REAL_PHONE,
        "employer": _REAL_EMPLOYER,
        "school": _REAL_SCHOOL,
    }
    entry["_c_name"] = _REAL_NAME
    entry["_c_email"] = _REAL_EMAIL
    entry["_c_phone"] = _REAL_PHONE
    # (2) The rendered surface, carrying SERVER-REDACTED evidence — exactly the
    # shape ``_row_to_blind_entry`` produces for a blind job.
    reqs = entry["evidence"]["requirements"]
    reqs[0][
        "evidence"
    ] = "[name redacted] built the payments service at Employer A for four years."
    reqs[0]["source_context"] = (
        f"{_SOURCE_CONTEXT_MARKER}\n\n"
        "[name redacted] — [contact redacted] — [contact redacted]\n"
        "Employer A, Senior Engineer. Institution A, BSc."
    )
    reqs[1]["evidence"] = "Some exposure to Helm charts at Employer A."
    entry["evidence"]["overall_summary"] = "[name redacted] is a strong backend fit."
    entry["evidence"]["overall_motivation"] = "[name redacted] wants this job."
    entry["evidence"]["cover_letter_presence"] = True
    entry["evidence"]["cover_letter_evidence"] = [
        {
            "theme": "motivation",
            "evidence": "Reach [name redacted] at [contact redacted].",
            "evidence_chunk_ids": ["cl_001"],
            "confidence": 0.8,
        }
    ]
    monkeypatch.setattr(
        api_client, "get_shortlist_entry", MagicMock(return_value=entry)
    )
    resp = client.get(f"/shortlist/{entry_id}")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    # Sanity: the new panel actually rendered (this scan is meaningless
    # against the old 16-line stub, which shows none of this).
    _assert_number_rendered(body, 0.34, label="structured contribution")
    # Sanity: the evidence quote + source context really are ON the page, so
    # the marker assertions below are not vacuous.
    assert "payments service" in body
    assert _SOURCE_CONTEXT_MARKER in body
    # The server's redaction markers survive to the reader, unaltered.
    assert body.count("[name redacted]") >= 2
    assert "[contact redacted]" in body
    assert "Employer A" in body
    assert "Institution A" in body
    # And nothing re-materialises the identity behind them.
    assert _REAL_NAME not in body
    assert _REAL_EMAIL not in body
    assert _REAL_PHONE not in body
    assert _REAL_EMPLOYER not in body
    assert _REAL_SCHOOL not in body


def test_entry_detail_real_blind_read_renders_no_pii(
    monkeypatch: Any, client: Any
) -> None:
    """THE load-bearing privacy guard for this page: stored row -> the REAL
    ``shortlist_service._row_to_blind_entry`` -> the serialized DTO the API
    returns -> the rendered HTML.

    Every other test in this file hands the route a hand-built dict, so none of
    them executes the redaction at all — security proved that by stripping the
    arguments off ``_row_to_blind_entry``'s ``_redact_evidence`` call and
    watching the old "load-bearing privacy guard" here still PASS while four
    service tests failed. This test closes that: the row carries the
    candidate's real name/email/phone in the evidence quote AND in the résumé
    chunk that ``source_context`` resolves from, and both of those fields are
    rendered verbatim by ``shortlist_entry.html``. Drop the redaction and the
    name/employer/school land on the page.

    NOTE on what a mutation here proves. ``redact_text`` masks email/phone by
    PATTERN, so those two stay redacted even with ``name``/``term_map``
    stripped; the name, employer and school are the fields that actually leak,
    and they are asserted individually below rather than as one blanket scan
    so a partial regression cannot hide behind the others."""
    from src.services.shortlist_service import _row_to_blind_entry

    entry_id = uuid4()
    chunk_text = (
        f"{_SOURCE_CONTEXT_MARKER} {_REAL_NAME} — {_REAL_EMAIL} — {_REAL_PHONE}. "
        f"Senior Engineer at {_REAL_EMPLOYER}. {_REAL_SCHOOL}, BSc."
    )
    row: dict[str, Any] = {
        "id": entry_id,
        "job_id": uuid4(),
        "resume_id": uuid4(),
        "rank": 2,
        "score_final": 0.70,
        "score_breakdown": json.dumps(
            {
                "skill": 0.80,
                "experience": 0.60,
                "education": 0.40,
                "seniority": 0.50,
                "vector": 1.00,
                "structured": 0.68,
                "motivation": 0.60,
                "implied_experience": False,
                "skill_contributions": [],
                "score_structured": 0.68,
                "score_evidence": 0.80,
            }
        ),
        "evidence": json.dumps(
            {
                "requirements": [
                    {
                        "requirement": "Python",
                        "status": "met",
                        "evidence": (
                            f"{_REAL_NAME} built the payments service at "
                            f"{_REAL_EMPLOYER} for four years."
                        ),
                        "evidence_chunk_ids": ["c_001"],
                        "confidence": 0.92,
                    }
                ],
                "overall_summary": f"{_REAL_NAME} studied at {_REAL_SCHOOL}.",
                "cover_letter_presence": True,
                "cover_letter_evidence": [
                    {
                        "theme": "motivation",
                        "evidence": f"Reach {_REAL_NAME} at {_REAL_EMAIL}.",
                        "evidence_chunk_ids": [],
                        "confidence": 0.8,
                    }
                ],
                "overall_motivation": f"{_REAL_NAME} really wants this job.",
            }
        ),
        "pipeline_meta": json.dumps(
            {
                "model_gen": "gpt-oss:20b",
                "model_emb": "nomic-embed-text",
                "prompt_versions": {"shortlist_evidence": "shortlist_evidence_v1"},
                "weights": _ENTRY_WEIGHTS,
                "git_sha": "deadbeef",
                "generated_at": "2026-07-15T00:00:00+00:00",
                "timings_ms": {},
            }
        ),
        "generated_at": "2026-07-15T00:00:00+00:00",
        "_c_name": _REAL_NAME,
        "_c_email": _REAL_EMAIL,
        "_c_phone": _REAL_PHONE,
        "_c_parsed": json.dumps(
            {
                "candidate": {
                    "name": _REAL_NAME,
                    "email": _REAL_EMAIL,
                    "phone": _REAL_PHONE,
                },
                "experience": [{"company": _REAL_EMPLOYER, "title": "Senior Engineer"}],
                "education": [{"degree": "BSc", "institution": _REAL_SCHOOL}],
                "chunks": [{"id": "c_001", "text": chunk_text}],
                "cover_letter_chunks": [],
            }
        ),
    }

    dto = _row_to_blind_entry(row)
    # The route consumes the API's JSON, so round-trip through it rather than
    # handing the route a live pydantic object it would never receive.
    payload = json.loads(dto.model_dump_json())
    monkeypatch.setattr(
        api_client, "get_shortlist_entry", MagicMock(return_value=payload)
    )

    resp = client.get(f"/shortlist/{entry_id}")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)

    # Sanity, so the absence assertions cannot pass by the panel simply not
    # rendering: the quote AND the resolved source context are both on the page.
    _assert_number_rendered(body, 0.34, label="structured contribution")
    assert "payments service" in body
    assert _SOURCE_CONTEXT_MARKER in body

    assert _REAL_NAME not in body, "candidate name reached the rendered panel"
    assert _REAL_EMAIL not in body, "candidate email reached the rendered panel"
    assert _REAL_PHONE not in body, "candidate phone reached the rendered panel"
    assert _REAL_EMPLOYER not in body, "employer name reached the rendered panel"
    assert _REAL_SCHOOL not in body, "school name reached the rendered panel"
    # Positive control on the substitutions themselves.
    assert "[name redacted]" in body
    assert "Employer A" in body
    assert "Institution A" in body


def test_entry_detail_shows_weights_unavailable_when_pipeline_meta_missing(
    monkeypatch: Any, client: Any
) -> None:
    """Legacy rows written before this slice carry no pipeline_meta. The
    panel must say weights are unavailable rather than silently computing a
    contribution table against today's defaults (the honesty guard, pinned
    at the service layer in test_services_explanation.py, must also hold at
    the template boundary)."""
    entry_id = uuid4()
    entry = _full_entry_detail(entry_id)
    entry["pipeline_meta"] = None
    monkeypatch.setattr(
        api_client, "get_shortlist_entry", MagicMock(return_value=entry)
    )
    resp = client.get(f"/shortlist/{entry_id}")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "weights unavailable" in _norm(body)


def test_entry_detail_weights_unavailable_shows_no_fabricated_weight(
    monkeypatch: Any, client: Any
) -> None:
    """The other half of the "weights unavailable" contract: saying it is not
    enough, the page must ALSO not print a weight. A legacy row rendered with
    today's DEFAULT_WEIGHTS (0.6/0.3/0.1 top level, 0.40 skill) would present
    a fabricated arithmetic as an audit trail, which is exactly what the
    backend's ``_parse_pipeline_meta -> None`` exists to prevent."""
    entry_id = uuid4()
    entry = _full_entry_detail(entry_id)
    entry["pipeline_meta"] = None
    monkeypatch.setattr(
        api_client, "get_shortlist_entry", MagicMock(return_value=entry)
    )
    resp = client.get(f"/shortlist/{entry_id}")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "weights unavailable" in _norm(body)

    # Asserted COLUMN-WISE rather than "0.6 is absent from the page": the raw
    # sub-scores legitimately include 0.60, so a substring check would collide
    # with them. Every WEIGHT and every CONTRIBUTION cell must be the omitted
    # marker -- nothing numeric, borrowed or otherwise.
    rows = _contribution_rows(body)
    assert len(rows) == 8, "3 top-level + 5 structured sub-rows"
    for label, weight, score, contribution in rows:
        assert weight == "—", f"{label}: weight must be omitted, got {weight!r}"
        assert (
            contribution == "—"
        ), f"{label}: contribution must be omitted, got {contribution!r}"
        # The raw sub-SCORES came off the row itself and are not in question.
        assert score not in ("", "—"), f"{label}: sub-score must still be shown"
    assert rows[0][2] == "68", "structured sub-score still rendered"


def test_entry_detail_non_blind_entry_never_renders_the_word_none(
    monkeypatch: Any, client: Any
) -> None:
    """``display_label`` is a BLIND-review field -- ``_row_to_entry`` (the
    non-blind path) never sets it, so it is ``None`` on every entry of a job
    with ``blind_review = FALSE``. Interpolating it raw produced "This panel
    explains how None was scored...". The page must degrade to a neutral
    phrase instead."""
    entry_id = uuid4()
    entry = _full_entry_detail(entry_id)
    entry["blinded"] = False
    entry["display_label"] = None
    monkeypatch.setattr(
        api_client, "get_shortlist_entry", MagicMock(return_value=entry)
    )
    resp = client.get(f"/shortlist/{entry_id}")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)

    banner = re.search(r'<div class="direction-banner">(.*?)</div>', body, re.DOTALL)
    assert banner is not None, "direction banner must still render"
    banner_text = _norm(banner.group(1))
    assert "none" not in banner_text
    assert "this candidate" in banner_text
    assert "for this job's requirements" in banner_text
    # The <title> and the <h2> read off the SAME field and had the same
    # defect, so they are pinned here too rather than left to regress.
    title = re.search(r"<title>(.*?)</title>", body, re.DOTALL)
    assert title is not None
    assert "None" not in title.group(1)
    heading = re.search(r"<h2[^>]*>(.*?)</h2>", body, re.DOTALL)
    assert heading is not None
    assert "None" not in heading.group(1)


def test_entry_detail_unrecorded_subscores_render_as_not_recorded(
    monkeypatch: Any, client: Any
) -> None:
    """A row that never recorded the two composed sub-scores must say so.
    Rendering ``0.0`` as "0%" is a positive false claim about the candidate
    and is asymmetric with the "weights unavailable" treatment right beside
    it."""
    entry_id = uuid4()
    entry = _full_entry_detail(entry_id)
    del entry["score_structured"]
    del entry["score_evidence"]
    monkeypatch.setattr(
        api_client, "get_shortlist_entry", MagicMock(return_value=entry)
    )
    resp = client.get(f"/shortlist/{entry_id}")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)

    assert "not recorded" in _norm(body)
    tables = _contribution_html(body)
    # Pin the marker in the TABLE CELL, not merely somewhere on the page: the
    # explanatory banner hardcodes "not recorded" whenever scores_available is
    # False, so the page-wide assertion above passes even if the cell's marker
    # drifts out of sync with the banner promising it.
    rows = {r[0]: r for r in _contribution_rows(body)}
    assert rows["Structured"][2] == "not recorded", rows["Structured"]
    # No affirmative zero anywhere in the top-level rows, and no contribution
    # derived from a score that was never recorded (0.5 * 0.0 = 0.0).
    assert ">0%<" not in tables.replace(" ", "")
    assert ">0.0<" not in tables.replace(" ", "")
    # The structured SUB-rows are unaffected -- they came off score_breakdown.
    _assert_number_rendered(body, 0.32, label="skill contribution")


def test_entry_detail_affirmative_zero_subscores_render_as_zero(
    monkeypatch: Any, client: Any
) -> None:
    """The mirror of the test above. When a sub-score genuinely IS ``0.0`` --
    the common case for motivation, since ``_motivation_score`` returns
    ``0.0`` for every candidate who submitted no cover letter -- it must
    render as an affirmative zero, never be disguised as "not recorded".

    This pins the ``v is not none`` boundary in BOTH ``pct`` and
    ``score_cell`` in the other direction from the test above: a
    ``v is not none`` -> ``v`` (truthiness) mutant on either macro collapses
    a real ``0.0`` into the same branch as an unrecorded ``None`` and would
    pass every other test in this file undetected."""
    entry_id = uuid4()
    entry = _full_entry_detail(entry_id)
    entry["score_structured"] = 0.0
    entry["score_breakdown"]["motivation"] = 0.0
    monkeypatch.setattr(
        api_client, "get_shortlist_entry", MagicMock(return_value=entry)
    )
    resp = client.get(f"/shortlist/{entry_id}")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)

    rows = {row[0]: row for row in _contribution_rows(body)}
    assert rows["Structured"][2] == "0", rows["Structured"]
    assert rows["Motivation"][2] == "0", rows["Motivation"]
    tables = _contribution_html(body)
    assert "not recorded" not in tables


def test_entry_detail_non_dict_payload_degrades_instead_of_500ing(
    monkeypatch: Any, client: Any
) -> None:
    """``api_client.get_shortlist_entry`` returns whatever JSON the backend
    sent (``-> Any``). A non-object payload must degrade to the bare page,
    not raise ``AttributeError: 'list' object has no attribute 'items'`` --
    that 500 defeats the graceful degradation the route is built around."""
    entry_id = uuid4()
    monkeypatch.setattr(
        api_client, "get_shortlist_entry", MagicMock(return_value=["not", "a", "dict"])
    )
    resp = client.get(f"/shortlist/{entry_id}")
    assert resp.status_code == 200
    assert "explanation unavailable" in _norm(resp.get_data(as_text=True))


def test_entry_detail_malformed_payload_is_logged_not_swallowed_silently(
    monkeypatch: Any, client: Any, caplog: Any
) -> None:
    """The explanation panel is a COMPLIANCE artifact. A payload that fails
    validation degrades the page silently today, so genuine corruption is
    indistinguishable from an ordinary legacy row and invisible to whoever
    operates this. It must leave a warning -- carrying the entry id and NO
    candidate fields."""
    entry_id = uuid4()
    entry = _full_entry_detail(entry_id)
    entry["score_final"] = "not-a-number"
    entry["candidate_name"] = _REAL_NAME
    monkeypatch.setattr(
        api_client, "get_shortlist_entry", MagicMock(return_value=entry)
    )
    with caplog.at_level("WARNING"):
        resp = client.get(f"/shortlist/{entry_id}")
    assert resp.status_code == 200

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert warnings, "a malformed entry payload must be logged, not swallowed"
    text = " ".join(r.getMessage() for r in warnings)
    assert str(entry_id) in text
    assert _REAL_NAME not in text
    # Black-box PII scan of the rendered BODY too -- this is the only render
    # path (the degraded ``_EntryHeader`` fallback) with no such check above.
    # Structurally it cannot leak (``_entry_header``/``known`` only ever carry
    # the four ``ShortlistEntry`` display fields), but this is belt-and-braces
    # on the one uncovered path.
    assert _REAL_NAME not in resp.get_data(as_text=True)


# ── ROADMAP A4: the evidence cliff is disclosed, not silently rendered as 0% ──


def test_entry_detail_says_not_assessed_for_a_past_the_cliff_candidate(
    monkeypatch: Any, client: Any
) -> None:
    """THE pin, at the surface where the false claim was actually made.

    A candidate below the ``evidence_k`` cut-off was never submitted to
    evidence extraction. Their stored evidence and motivation scores are real
    ``0.0`` floats, so before this the panel rendered an affirmative ``0%`` —
    indistinguishable from a candidate whose evidence WAS examined and
    supported nothing.
    """
    entry_id = uuid4()
    entry = _full_entry_detail(entry_id)
    entry["evidence_evaluated"] = False
    monkeypatch.setattr(
        api_client, "get_shortlist_entry", MagicMock(return_value=entry)
    )
    body = client.get(f"/shortlist/{entry_id}").get_data(as_text=True)

    assert "not assessed" in _norm(body), (
        "a candidate past the evidence cliff is still being shown a measured "
        "evidence score"
    )
    assert "not comparable" in _norm(body), (
        "the panel must say the headline score is not comparable across the "
        "cut-off — that is the fact a recruiter needs, not just a blank cell"
    )


def test_entry_detail_keeps_a_genuine_zero_for_an_assessed_candidate(
    monkeypatch: Any, client: Any
) -> None:
    """The mirror-image mutant ADR-031 records: the fix must not start hiding
    REAL zeros.

    Motivation is ``0.0`` for every candidate with no cover letter, so a real
    zero on an assessed candidate is the common case, not an edge case. It must
    keep rendering as a number.
    """
    entry_id = uuid4()
    entry = _full_entry_detail(entry_id)
    entry["evidence_evaluated"] = True
    monkeypatch.setattr(
        api_client, "get_shortlist_entry", MagicMock(return_value=entry)
    )
    body = client.get(f"/shortlist/{entry_id}").get_data(as_text=True)

    assert "not assessed" not in _norm(body), (
        "an ASSESSED candidate is being labelled 'not assessed' — the fix for "
        "the fabricated zero has started hiding genuine measurements"
    )


def test_entry_detail_makes_no_claim_for_a_legacy_row(
    monkeypatch: Any, client: Any
) -> None:
    """A row written before the marker existed does not know whether stage 3
    ran. Asserting "not assessed" would invent a fact just as surely as
    asserting a measured 0% does, so the panel says neither."""
    entry_id = uuid4()
    entry = _full_entry_detail(entry_id)
    entry.pop("evidence_evaluated", None)
    monkeypatch.setattr(
        api_client, "get_shortlist_entry", MagicMock(return_value=entry)
    )
    resp = client.get(f"/shortlist/{entry_id}")

    assert resp.status_code == 200
    assert "not assessed" not in _norm(resp.get_data(as_text=True))


# ── ROADMAP A6: two sub-scores render as measurements when nothing was
# ── measured -- disclosed, not silently rendered, at the surface where the
# ── false claim is actually made ────────────────────────────────────────────


def _row_for(
    rows: list[tuple[str, str, str, str]], label: str
) -> tuple[str, str, str, str]:
    matches = [r for r in rows if r[0].strip().lower() == label]
    assert len(matches) == 1, f"expected exactly one {label!r} row, got {matches!r}"
    return matches[0]


def test_entry_detail_says_seniority_not_assessed_when_no_title_was_readable(
    monkeypatch: Any, client: Any
) -> None:
    """THE pin for D2. No readable title was found for this candidate, so the
    stored seniority score came from a policy fallback, not a comparison —
    the panel must not present it as a measured poor title match."""
    entry_id = uuid4()
    entry = _full_entry_detail(entry_id)
    entry["score_breakdown"]["seniority_measured"] = False
    monkeypatch.setattr(
        api_client, "get_shortlist_entry", MagicMock(return_value=entry)
    )
    body = client.get(f"/shortlist/{entry_id}").get_data(as_text=True)
    rows = _contribution_rows(body)
    _component, _weight, score, contribution = _row_for(rows, "seniority")

    assert _norm(score) == "not assessed", (
        f"seniority_measured=False must render 'not assessed' in the score "
        f"cell, got {score!r}"
    )
    assert contribution.strip() in {"—", "-"}, (
        f"no honest contribution can be stated for an unmeasured sub-score, "
        f"got {contribution!r}"
    )
    assert "no readable job title" in _norm(body), (
        "the prose above the table must say WHY -- no readable job title "
        "was found, so nothing was compared"
    )
    # F2 (remediation): the OTHER three sub-score rows are untouched by this
    # marker and must keep rendering their REAL numbers -- a mutant that
    # suppresses `skill` (or `experience`/`education`) under
    # `seniority_not_assessed` instead of `seniority` passes every assertion
    # above unchanged.
    other_rows = (
        ("skill", 0.80),
        ("experience", 0.60),
        ("education", 0.40),
    )
    for other_label, other_value in other_rows:
        _o_component, _o_weight, other_score, _o_contribution = _row_for(
            rows, other_label
        )
        assert _norm(other_score) != "not assessed", (
            f"the {other_label!r} row was suppressed by seniority's marker -- "
            f"got {other_score!r}"
        )
        assert _norm(other_score) == str(int(round(other_value * 100))), (
            f"expected the real {other_label!r} score ({other_value}) to "
            f"still render, got {other_score!r}"
        )


def test_entry_detail_keeps_the_real_seniority_score_when_a_title_was_readable(
    monkeypatch: Any, client: Any
) -> None:
    """Mirror-image mutant guard: the fix must not start hiding genuine
    measurements."""
    entry_id = uuid4()
    entry = _full_entry_detail(entry_id)
    entry["score_breakdown"]["seniority_measured"] = True
    monkeypatch.setattr(
        api_client, "get_shortlist_entry", MagicMock(return_value=entry)
    )
    body = client.get(f"/shortlist/{entry_id}").get_data(as_text=True)
    rows = _contribution_rows(body)
    _component, _weight, score, _contribution = _row_for(rows, "seniority")

    assert _norm(score) != "not assessed", (
        f"a MEASURED seniority score must not be labelled not assessed, "
        f"got {score!r}"
    )
    assert "no readable job title" not in _norm(body)


def test_entry_detail_says_vector_not_comparable_for_a_degenerate_pool(
    monkeypatch: Any, client: Any
) -> None:
    """THE pin for D1. Every candidate in a degenerate pool scores the same
    100% semantic match -- that reflects the pool, not this candidate."""
    entry_id = uuid4()
    entry = _full_entry_detail(entry_id)
    entry["score_breakdown"]["vector_discriminating"] = False
    monkeypatch.setattr(
        api_client, "get_shortlist_entry", MagicMock(return_value=entry)
    )
    body = client.get(f"/shortlist/{entry_id}").get_data(as_text=True)
    rows = _contribution_rows(body)
    _component, _weight, score, contribution = _row_for(rows, "vector")

    assert _norm(score) == "not assessed", (
        f"vector_discriminating=False must render 'not assessed' in the "
        f"score cell, got {score!r}"
    )
    assert contribution.strip() in {"—", "-"}
    assert "scored identically" in _norm(body), (
        "the prose above the table must say WHY -- every candidate in this "
        "pool scored identically, so the number reflects the pool, not the "
        "match"
    )
    # F2 (remediation): the OTHER three sub-score rows are untouched by this
    # marker and must keep rendering their REAL numbers -- a mutant that
    # suppresses `education` (or `skill`/`experience`) under
    # `vector_not_comparable` instead of `vector` passes every assertion
    # above unchanged.
    other_rows = (
        ("skill", 0.80),
        ("experience", 0.60),
        ("education", 0.40),
    )
    for other_label, other_value in other_rows:
        _o_component, _o_weight, other_score, _o_contribution = _row_for(
            rows, other_label
        )
        assert _norm(other_score) != "not assessed", (
            f"the {other_label!r} row was suppressed by vector's marker -- "
            f"got {other_score!r}"
        )
        assert _norm(other_score) == str(int(round(other_value * 100))), (
            f"expected the real {other_label!r} score ({other_value}) to "
            f"still render, got {other_score!r}"
        )


def test_entry_detail_keeps_the_real_vector_score_for_a_pool_with_spread(
    monkeypatch: Any, client: Any
) -> None:
    entry_id = uuid4()
    entry = _full_entry_detail(entry_id)
    entry["score_breakdown"]["vector_discriminating"] = True
    monkeypatch.setattr(
        api_client, "get_shortlist_entry", MagicMock(return_value=entry)
    )
    body = client.get(f"/shortlist/{entry_id}").get_data(as_text=True)
    rows = _contribution_rows(body)
    _component, _weight, score, _contribution = _row_for(rows, "vector")

    assert _norm(score) != "not assessed"
    assert "scored identically" not in _norm(body)


def test_entry_detail_makes_no_claim_for_a_legacy_row_with_neither_marker(
    monkeypatch: Any, client: Any
) -> None:
    """A row written before this slice's markers existed has NEITHER key on
    ``score_breakdown`` -- both validate to None, and the panel must render
    EXACTLY as it does today. The negative half of the pair above: a
    present-only assertion would let 'the string is always rendered' survive."""
    entry_id = uuid4()
    entry = _full_entry_detail(entry_id)
    assert "seniority_measured" not in entry["score_breakdown"]
    assert "vector_discriminating" not in entry["score_breakdown"]
    monkeypatch.setattr(
        api_client, "get_shortlist_entry", MagicMock(return_value=entry)
    )
    body = client.get(f"/shortlist/{entry_id}").get_data(as_text=True)
    normalized = _norm(body)

    assert "not assessed" not in normalized, (
        "a legacy row (neither marker recorded) must not claim ANY "
        "sub-score was 'not assessed' -- that invents a fact the row does "
        "not support"
    )
    assert "no readable job title" not in normalized
    assert "scored identically" not in normalized
    # The row must render byte-identically to before this slice: the
    # seniority contribution (weight 0.10 x score 0.50 = 0.05) still shows
    # as a real number.
    _assert_number_rendered(
        body, 0.05, label="seniority contribution (legacy row unaffected)"
    )


@pytest.mark.parametrize("junk_value", [0, 0.0, "", "no", "off"])
def test_seniority_disclosure_requires_literal_false_not_merely_falsy_junk(
    monkeypatch: Any, client: Any, junk_value: Any
) -> None:
    """Identity, not truthiness (spec item 7). ``0``, ``0.0``, ``""``,
    ``"no"``, ``"off"`` are all Python/Jinja-falsy but NONE of them
    ``is False`` — a template written as ``{% if not
    explanation.seniority_assessed %}`` (truthiness) or ``{% if
    explanation.seniority_assessed == false %}`` (equality: ``0 == False``
    is True in Python) would show the disclosure for these too. Only a real,
    identical ``False`` may trigger it.

    Bypasses ``ScoreBreakdown``'s own bool coercion (which would just turn
    ``0``/``0.0`` back into a real ``False`` before this ever reaches the
    template) via ``model_copy`` — the same technique
    ``schemas/matching.py``'s own docstring already documents as bypassing
    validation — to put the exact junk value in front of the template."""
    entry_id = uuid4()
    entry_payload = _full_entry_detail(entry_id)
    monkeypatch.setattr(
        api_client, "get_shortlist_entry", MagicMock(return_value=entry_payload)
    )
    real_entry = ShortlistEntry.model_validate(entry_payload)
    base_explanation = explanation_module.shortlist_entry_explanation(real_entry)
    junk_explanation = base_explanation.model_copy(
        update={"seniority_assessed": junk_value}
    )
    monkeypatch.setattr(
        frontend_app_module,
        "shortlist_entry_explanation",
        MagicMock(return_value=junk_explanation),
    )
    body = client.get(f"/shortlist/{entry_id}").get_data(as_text=True)

    assert "no readable job title" not in _norm(body), (
        f"seniority_assessed={junk_value!r} is falsy but not `is False` -- "
        "the disclosure must require identity, not truthiness"
    )


@pytest.mark.parametrize("junk_value", [0, 0.0, "", "no", "off"])
def test_vector_disclosure_requires_literal_false_not_merely_falsy_junk(
    monkeypatch: Any, client: Any, junk_value: Any
) -> None:
    """Sibling of the seniority identity guard above, for the vector row."""
    entry_id = uuid4()
    entry_payload = _full_entry_detail(entry_id)
    monkeypatch.setattr(
        api_client, "get_shortlist_entry", MagicMock(return_value=entry_payload)
    )
    real_entry = ShortlistEntry.model_validate(entry_payload)
    base_explanation = explanation_module.shortlist_entry_explanation(real_entry)
    junk_explanation = base_explanation.model_copy(
        update={"vector_comparable": junk_value}
    )
    monkeypatch.setattr(
        frontend_app_module,
        "shortlist_entry_explanation",
        MagicMock(return_value=junk_explanation),
    )
    body = client.get(f"/shortlist/{entry_id}").get_data(as_text=True)

    assert "scored identically" not in _norm(body), (
        f"vector_comparable={junk_value!r} is falsy but not `is False` -- "
        "the disclosure must require identity, not truthiness"
    )


# ── ROADMAP A6 siblings: three more sub-scores render as measurements when
# ── nothing was measured -- disclosed at the same surface as seniority/
# ── vector immediately above (docs/adr/041-sub-score-measurement-markers.md
# ── "Three siblings found while writing this, recorded not fixed") ─────────


def test_entry_detail_says_experience_not_assessed_when_no_jd_bar_stated(
    monkeypatch: Any, client: Any
) -> None:
    """THE pin for the experience sibling. The JD stated no minimum years, so
    every candidate scores full marks on this row -- it reflects the job
    posting, not this candidate, and must not be presented as a measured
    comparison."""
    entry_id = uuid4()
    entry = _full_entry_detail(entry_id)
    entry["score_breakdown"]["experience_bar_stated"] = False
    monkeypatch.setattr(
        api_client, "get_shortlist_entry", MagicMock(return_value=entry)
    )
    body = client.get(f"/shortlist/{entry_id}").get_data(as_text=True)
    rows = _contribution_rows(body)
    _component, _weight, score, contribution = _row_for(rows, "experience")

    assert _norm(score) == "not assessed", (
        f"experience_bar_stated=False must render 'not assessed' in the "
        f"score cell, got {score!r}"
    )
    assert contribution.strip() in {"—", "-"}, (
        f"no honest contribution can be stated for an unmeasured sub-score, "
        f"got {contribution!r}"
    )
    assert "no minimum years of experience" in _norm(body), (
        "the prose above the table must say WHY -- the job posting stated "
        "no minimum years, so every candidate clears this row by policy"
    )
    # The other three structured rows are untouched by this marker and must
    # keep rendering their REAL numbers -- a mutant that suppresses the
    # wrong row instead of `experience` passes every assertion above
    # unchanged.
    other_rows = (
        ("skill", 0.80),
        ("education", 0.40),
        ("seniority", 0.50),
    )
    for other_label, other_value in other_rows:
        _o_component, _o_weight, other_score, _o_contribution = _row_for(
            rows, other_label
        )
        assert _norm(other_score) != "not assessed", (
            f"the {other_label!r} row was suppressed by experience's "
            f"marker -- got {other_score!r}"
        )
        assert _norm(other_score) == str(int(round(other_value * 100))), (
            f"expected the real {other_label!r} score ({other_value}) to "
            f"still render, got {other_score!r}"
        )


def test_entry_detail_keeps_the_real_experience_score_when_a_bar_was_stated(
    monkeypatch: Any, client: Any
) -> None:
    """Mirror-image mutant guard: the fix must not start hiding genuine
    measurements."""
    entry_id = uuid4()
    entry = _full_entry_detail(entry_id)
    entry["score_breakdown"]["experience_bar_stated"] = True
    monkeypatch.setattr(
        api_client, "get_shortlist_entry", MagicMock(return_value=entry)
    )
    body = client.get(f"/shortlist/{entry_id}").get_data(as_text=True)
    rows = _contribution_rows(body)
    _component, _weight, score, _contribution = _row_for(rows, "experience")

    assert _norm(score) != "not assessed", (
        f"a MEASURED experience score must not be labelled not assessed, "
        f"got {score!r}"
    )
    assert "no minimum years of experience" not in _norm(body)


def test_entry_detail_says_education_not_assessed_when_no_jd_bar_stated(
    monkeypatch: Any, client: Any
) -> None:
    """THE pin for the education-bar sibling. The JD stated no minimum
    education level, so every candidate scores full marks on this row."""
    entry_id = uuid4()
    entry = _full_entry_detail(entry_id)
    entry["score_breakdown"]["education_bar_stated"] = False
    monkeypatch.setattr(
        api_client, "get_shortlist_entry", MagicMock(return_value=entry)
    )
    body = client.get(f"/shortlist/{entry_id}").get_data(as_text=True)
    rows = _contribution_rows(body)
    _component, _weight, score, contribution = _row_for(rows, "education")

    assert _norm(score) == "not assessed", (
        f"education_bar_stated=False must render 'not assessed' in the "
        f"score cell, got {score!r}"
    )
    assert contribution.strip() in {"—", "-"}
    assert "no minimum education level" in _norm(body), (
        "the prose above the table must say WHY -- the job posting stated "
        "no minimum education level bar"
    )
    assert "no education could be read" not in _norm(body), (
        "the bar-not-stated paragraph fired -- the UNREADABLE paragraph "
        "must not also render for a row that only lacks a stated bar"
    )
    other_rows = (
        ("skill", 0.80),
        ("experience", 0.60),
        ("seniority", 0.50),
    )
    for other_label, other_value in other_rows:
        _o_component, _o_weight, other_score, _o_contribution = _row_for(
            rows, other_label
        )
        assert _norm(other_score) != "not assessed", (
            f"the {other_label!r} row was suppressed by education's "
            f"marker -- got {other_score!r}"
        )
        assert _norm(other_score) == str(int(round(other_value * 100))), (
            f"expected the real {other_label!r} score ({other_value}) to "
            f"still render, got {other_score!r}"
        )


def test_entry_detail_says_education_not_assessed_when_unreadable(
    monkeypatch: Any, client: Any
) -> None:
    """THE pin for the education-readability sibling: no degree level could
    be read from this résumé at all, so the stored education row is a
    parsing limit, not a finding about the candidate. ``education_bar_stated``
    is left unset (``None``, i.e. NOT ``False``), so the bar-not-stated
    paragraph must not fire -- only the unreadable one."""
    entry_id = uuid4()
    entry = _full_entry_detail(entry_id)
    entry["score_breakdown"]["education_readable"] = False
    assert "education_bar_stated" not in entry["score_breakdown"]
    monkeypatch.setattr(
        api_client, "get_shortlist_entry", MagicMock(return_value=entry)
    )
    body = client.get(f"/shortlist/{entry_id}").get_data(as_text=True)
    rows = _contribution_rows(body)
    _component, _weight, score, contribution = _row_for(rows, "education")

    assert _norm(score) == "not assessed", (
        f"education_readable=False must render 'not assessed' in the "
        f"score cell, got {score!r}"
    )
    assert contribution.strip() in {"—", "-"}
    assert "no education could be read" in _norm(body), (
        "the prose above the table must say WHY -- no degree level could "
        "be parsed from this résumé at all"
    )
    assert "no minimum education level" not in _norm(body), (
        "education_bar_stated is None (unknown), not False -- the "
        "bar-not-stated paragraph must not fire for this row"
    )


def test_entry_detail_prefers_bar_not_stated_paragraph_when_both_education_markers_are_false(  # noqa: E501
    monkeypatch: Any, client: Any
) -> None:
    """THE precedence pin (spec item 4's parenthetical): when the JD states
    no education bar AT ALL, ``score_education`` returns its 1.0 fallback
    BEFORE it ever looks at the candidate's levels -- so even though
    ``education_readable`` is independently recorded as False too, the
    unreadable-education paragraph would be describing a 0% that was never
    actually stored. Only the bar-not-stated paragraph may render."""
    entry_id = uuid4()
    entry = _full_entry_detail(entry_id)
    entry["score_breakdown"]["education_bar_stated"] = False
    entry["score_breakdown"]["education_readable"] = False
    monkeypatch.setattr(
        api_client, "get_shortlist_entry", MagicMock(return_value=entry)
    )
    body = client.get(f"/shortlist/{entry_id}").get_data(as_text=True)
    rows = _contribution_rows(body)
    _component, _weight, score, _contribution = _row_for(rows, "education")

    assert _norm(score) == "not assessed"
    assert "no minimum education level" in _norm(body), (
        "the bar-not-stated paragraph must render when its own marker is "
        "False, regardless of the readability marker's value"
    )
    assert "no education could be read" not in _norm(body), (
        "the unreadable-education paragraph must NOT render when the "
        "bar-not-stated paragraph already applies -- both facts are "
        "recorded independently on the write path, but only one paragraph "
        "may be shown"
    )


def test_entry_detail_shows_unreadable_paragraph_when_bar_stated_but_unreadable(
    monkeypatch: Any, client: Any
) -> None:
    """THE pin for Gap 1 (merge-blocking review): the real production
    quadrant, confirmed against the eval corpus itself (r07/r08 both parse
    to degree strings ``_level_from_degree`` cannot map to any level, e.g.
    "Certificate, Full-Stack Web Development"), against a JD that DOES
    state an education bar. ``education_bar_stated=True`` means the
    bar-not-stated paragraph must NOT fire; ``education_readable=False``
    means the unreadable-education paragraph must. Mirror image of
    ``test_entry_detail_prefers_bar_not_stated_paragraph_when_both_education_markers_are_false``
    immediately above, which pins the opposite precedence when BOTH markers
    are False -- this pins the precedence when they disagree."""
    entry_id = uuid4()
    entry = _full_entry_detail(entry_id)
    entry["score_breakdown"]["education_bar_stated"] = True
    entry["score_breakdown"]["education_readable"] = False
    monkeypatch.setattr(
        api_client, "get_shortlist_entry", MagicMock(return_value=entry)
    )
    body = client.get(f"/shortlist/{entry_id}").get_data(as_text=True)
    rows = _contribution_rows(body)
    _component, _weight, score, contribution = _row_for(rows, "education")

    assert _norm(score) == "not assessed", (
        f"education_readable=False must still render 'not assessed' even "
        f"though education_bar_stated=True, got {score!r}"
    )
    assert contribution.strip() in {"—", "-"}
    assert "no education could be read" in _norm(body), (
        "the prose above the table must say WHY -- no degree level could "
        "be parsed from this résumé at all, even though the JD did state "
        "a bar"
    )
    assert "no minimum education level" not in _norm(body), (
        "education_bar_stated is True -- the bar-not-stated paragraph must "
        "not fire, since the JD plainly did state one"
    )


def test_entry_detail_keeps_the_real_education_score_when_bar_stated_and_readable(
    monkeypatch: Any, client: Any
) -> None:
    """Mirror-image mutant guard for both education markers together."""
    entry_id = uuid4()
    entry = _full_entry_detail(entry_id)
    entry["score_breakdown"]["education_bar_stated"] = True
    entry["score_breakdown"]["education_readable"] = True
    monkeypatch.setattr(
        api_client, "get_shortlist_entry", MagicMock(return_value=entry)
    )
    body = client.get(f"/shortlist/{entry_id}").get_data(as_text=True)
    rows = _contribution_rows(body)
    _component, _weight, score, _contribution = _row_for(rows, "education")

    assert _norm(score) != "not assessed"
    assert "no minimum education level" not in _norm(body)
    assert "no education could be read" not in _norm(body)


def test_entry_detail_makes_no_claim_for_a_legacy_row_with_none_of_the_three_new_markers(  # noqa: E501
    monkeypatch: Any, client: Any
) -> None:
    """A row written before this slice's three markers existed has NONE of
    the three keys on ``score_breakdown`` -- all validate to None, and the
    panel must render EXACTLY as it does today for the experience/education
    rows: no disclosure paragraph, and the real stored percentages."""
    entry_id = uuid4()
    entry = _full_entry_detail(entry_id)
    assert "experience_bar_stated" not in entry["score_breakdown"]
    assert "education_bar_stated" not in entry["score_breakdown"]
    assert "education_readable" not in entry["score_breakdown"]
    monkeypatch.setattr(
        api_client, "get_shortlist_entry", MagicMock(return_value=entry)
    )
    body = client.get(f"/shortlist/{entry_id}").get_data(as_text=True)
    normalized = _norm(body)

    assert "no minimum years of experience" not in normalized
    assert "no minimum education level" not in normalized
    assert "no education could be read" not in normalized
    rows = _contribution_rows(body)
    _component, _weight, exp_score, _contribution = _row_for(rows, "experience")
    _component, _weight, edu_score, _contribution = _row_for(rows, "education")
    assert _norm(exp_score) == "60"
    assert _norm(edu_score) == "40"


@pytest.mark.parametrize("junk_value", [0, 0.0, "", "no", "off"])
@pytest.mark.parametrize(
    "field_name,phrase",
    [
        ("experience_bar_stated", "no minimum years of experience"),
        ("education_bar_stated", "no minimum education level"),
        ("education_readable", "no education could be read"),
    ],
)
def test_sibling_disclosure_requires_literal_false_not_merely_falsy_junk(
    monkeypatch: Any,
    client: Any,
    field_name: str,
    phrase: str,
    junk_value: Any,
) -> None:
    """Identity, not truthiness, for all three new markers -- sibling of
    ``test_seniority_disclosure_requires_literal_false_not_merely_falsy_junk``
    immediately above. ``0``, ``0.0``, ``""``, ``"no"``, ``"off"`` are all
    falsy but none ``is False``; only a real, identical ``False`` may
    trigger a disclosure paragraph."""
    entry_id = uuid4()
    entry_payload = _full_entry_detail(entry_id)
    monkeypatch.setattr(
        api_client, "get_shortlist_entry", MagicMock(return_value=entry_payload)
    )
    real_entry = ShortlistEntry.model_validate(entry_payload)
    base_explanation = explanation_module.shortlist_entry_explanation(real_entry)
    junk_explanation = base_explanation.model_copy(update={field_name: junk_value})
    monkeypatch.setattr(
        frontend_app_module,
        "shortlist_entry_explanation",
        MagicMock(return_value=junk_explanation),
    )
    body = client.get(f"/shortlist/{entry_id}").get_data(as_text=True)

    assert phrase not in _norm(body), (
        f"{field_name}={junk_value!r} is falsy but not `is False` -- the "
        "disclosure must require identity, not truthiness"
    )


def test_shortlist_card_withdraw_form_carries_its_job_id(
    monkeypatch: Any, client: Any
) -> None:
    """The return address. ``context=shortlist`` says *which kind* of page to
    go back to; the job id says *which one*. Without it the route cannot build
    the shortlist URL and has no choice but to dump the user on the résumé
    page — which is exactly what it did."""
    job_id = uuid4()
    entry = _full_entry(uuid4())
    entry["resume_id"] = str(uuid4())
    entry["job_id"] = str(job_id)
    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=[entry]))
    body = client.get(f"/jobs/{job_id}/shortlist-cards").get_data(as_text=True)
    form_re = re.compile(
        r'<form[^>]*action="[^"]*/withdraw[^"]*"[^>]*>(.*?)</form>', re.DOTALL
    )
    match = form_re.search(body)
    assert match is not None, "expected a withdraw <form> on the shortlist card"
    assert str(job_id) in match.group(1)
