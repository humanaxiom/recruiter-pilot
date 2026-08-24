"""Smoke tests — the real product, driven over real HTTP through the Flask BFF.

**The hole these fill, measured.** This repo has ~5,500 unit and ~540 integration
tests at 94% coverage. All 31 frontend test files mock ``api_client``; the
integration suite exercises services and API routes against real datastores but
never drives Flask. **Nothing in ~6,000 tests crosses the browser→Flask→API
seam** — and four of the last nine defects to reach the running product lived
exactly there:

* the Regenerate button did nothing — the pipeline was perfect, the fragment came
  back with no ``hx-trigger`` (PR #93);
* the withdraw form never collected a reason, so every withdrawal recorded
  ``None`` and D1's audited reveal had nothing to reveal;
* a withdrawn candidate rendered identically to an active one on the job page;
* withdrawing from a shortlist card threw the user onto the résumé page, so
  pressing Back showed a cached list with the candidate still on it.

Every one is asserted below. The rule for adding a test here is the same as for
``src.doctor``: **it earns its place by mapping to a defect that actually
reached a user.** A smoke suite that drifts into re-testing what the unit suite
already covers becomes slow, then ignored, then deleted.

**Why HTTP and HTML rather than a browser.** htmx attributes are server-rendered,
so ``hx-trigger``, form fields, hidden inputs and redirect targets are all
assertable from the response body. That covers every defect above without
Playwright's setup cost or flakiness. What it deliberately does NOT cover is
anything only a JS engine can show — CSS layout, and whether htmx actually
*fires* a trigger it was correctly given. Those stay manual, and this file does
not pretend otherwise.

**Runs against a stack with CAS DISABLED**, and fails loudly if CAS is on rather
than skipping (``conftest.py``). A smoke suite that silently skips is worse than
none: it reports green having tested nothing.
"""

from __future__ import annotations

import re
from typing import Any

import httpx
import pytest

pytestmark = pytest.mark.smoke


# ── the flows ────────────────────────────────────────────────────────────


def test_the_shortlist_renders_candidate_cards(ranked_job: dict[str, Any]) -> None:
    body = ranked_job["shortlist_html"]
    assert "Candidate" in body
    assert 'class="card' in body or "score-final" in body


def test_no_skill_chip_renders_a_raw_hash(ranked_job: dict[str, Any]) -> None:
    """ROADMAP A7 (20), "the fix that never ran". ADR-032 shipped 2026-08-07 to
    render JD-authored skill names instead of ADR-008 hashes, and had never
    applied to a single row thirteen days later — every job predated it and the
    label is written only during projection. Recruiters saw
    ``h:2431ff17cb58a88d057650c93758977d`` where a skill name belonged, behind a
    permanently green suite, because no test has ever looked at this page.

    This assertion is the whole reason that class is catchable now: a freshly
    projected job MUST carry its labels, so if this fails the projection write
    is broken rather than merely stale.
    """
    hashes = re.findall(r"h:[0-9a-f]{32}", ranked_job["shortlist_html"])
    assert not hashes, (
        f"{len(hashes)} skill chips rendered a raw ADR-008 hash instead of the "
        f"JD's own wording, e.g. {hashes[0]}"
    )


def test_the_card_withdraw_form_carries_its_return_address(
    ranked_job: dict[str, Any],
) -> None:
    """The card has posted ``context=shortlist`` since FU-8 and the route never
    read it, so the user was thrown onto the résumé page and Back served a
    cached shortlist. ``job_id`` is the half that was missing."""
    form = _form_containing(ranked_job["shortlist_html"], "/withdraw")
    assert 'name="context"' in form and 'value="shortlist"' in form
    assert ranked_job["job_id"] in form, "the withdraw form has no return address"


def test_withdrawing_from_a_card_returns_to_the_shortlist(
    client: httpx.Client, ranked_job: dict[str, Any]
) -> None:
    """The reported defect, end to end: withdraw from the shortlist and you
    must land back on the shortlist, not on some other screen."""
    job_id = ranked_job["job_id"]
    resume_id = ranked_job["redirect_probe_resume_id"]
    # Re-fetch: the captured HTML predates the fixture's own withdrawal, and
    # the per-card tokens are one-shot.
    form = _form_containing(
        client.get(f"/jobs/{job_id}/shortlist").text, f"/resumes/{resume_id}/with"
    )
    token = _hidden(form, "csrf_token")
    resp = client.post(
        f"/resumes/{resume_id}/withdraw",
        data={"csrf_token": token, "context": "shortlist", "job_id": job_id},
        follow_redirects=False,
    )
    assert resp.status_code == 302, resp.text[:400]
    assert resp.headers["location"].endswith(f"/jobs/{job_id}/shortlist")


def test_a_withdrawn_candidate_leaves_the_shortlist(
    client: httpx.Client, ranked_job: dict[str, Any]
) -> None:
    """The claim the user actually cared about — and the one the suite could
    only ever make about the service function, never about the screen."""
    job_id = ranked_job["job_id"]
    body = client.get(f"/jobs/{job_id}/shortlist").text
    assert ranked_job["withdrawn_resume_id"] not in body


def test_a_withdrawn_candidate_is_marked_on_the_job_page(
    client: httpx.Client, ranked_job: dict[str, Any]
) -> None:
    """Retained, not hidden (ADR-026 "exclude and retain") — but visibly
    marked, or withdrawal looks like it did nothing."""
    body = client.get(f"/jobs/{ranked_job['job_id']}").text
    assert "withdrawn" in body.lower()


def test_the_resume_page_collects_a_withdrawal_reason(
    client: httpx.Client, ranked_job: dict[str, Any]
) -> None:
    """Without the input, every withdrawal records None and D1 = option C's
    audited reveal has nothing to reveal — forever."""
    body = client.get(f"/resumes/{ranked_job['active_resume_id']}").text
    form = _form_containing(body, "/withdraw")
    assert 'name="reason"' in form
    assert 'maxlength="500"' in form


def test_regenerate_reports_that_it_is_working(
    client: httpx.Client, ranked_job: dict[str, Any]
) -> None:
    """PR #93. The pipeline was perfect and the UI never said so: the fragment
    came back with no ``hx-trigger``, so the browser never polled and the
    previous run's cards sat there unchanged for two minutes."""
    job_id = ranked_job["job_id"]
    fragment = client.post(
        f"/jobs/{job_id}/shortlist",
        headers={"X-CSRF-Token": ranked_job["page_token"]},
    ).text
    assert "hx-trigger" in fragment, (
        "Regenerate returned a fragment with no hx-trigger — the browser will "
        "never poll, so the screen silently shows the previous run"
    )


def test_the_audit_page_offers_the_reveal_control(
    client: httpx.Client, ranked_job: dict[str, Any]
) -> None:
    """D1 = option C. The withdrawal above recorded a reason, so the access
    record must withhold it AND offer the audited reveal beside it."""
    body = client.get("/audit?action=withdraw_resume").text
    assert "withheld" in body
    assert "/reveal" in body


def test_revealing_a_withheld_reason_returns_the_prose_and_is_audited(
    client: httpx.Client, ranked_job: dict[str, Any]
) -> None:
    body = client.get("/audit?action=withdraw_resume").text
    audit_id = re.search(r"/audit/([0-9a-f-]{36})/reveal", body)
    assert audit_id, "no reveal control on a row that should have a withheld reason"
    form = _form_containing(body, f"/audit/{audit_id.group(1)}/reveal")
    revealed = client.post(
        f"/audit/{audit_id.group(1)}/reveal",
        data={"csrf_token": _hidden(form, "csrf_token"), "action": "withdraw_resume"},
    )
    assert revealed.status_code == 200
    assert ranked_job["withdrawal_reason"] in revealed.text
    # The reveal is itself recorded — that record is the whole of option C.
    assert "reveal_audit_detail" in client.get("/audit").text


# ── helpers ──────────────────────────────────────────────────────────────


def _form_containing(html: str, needle: str) -> str:
    """The ``<form>...</form>`` whose markup contains ``needle``."""
    idx = html.index(needle)
    start = html.rindex("<form", 0, idx)
    return html[start : html.index("</form>", idx) + 7]


def _hidden(form_html: str, name: str) -> str:
    match = re.search(rf'name="{name}"\s+value="([^"]*)"', form_html) or re.search(
        rf'value="([^"]*)"\s+name="{name}"', form_html
    )
    assert match, f"no hidden input named {name}"
    return match.group(1)
