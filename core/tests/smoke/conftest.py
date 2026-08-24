"""Smoke harness — drives the REAL running stack, and refuses to pretend.

Two rules govern everything here, and both come from defects this repo has
already shipped:

1. **Never skip silently.** If CAS is enabled, or the stack is down, or the
   fixtures are missing, these tests FAIL rather than skip. A smoke suite that
   skips reports green having exercised nothing, which is precisely the
   false-confidence failure mode the whole tool exists to remove.
2. **Build the data through the product.** The job and the résumés below are
   created by POSTing to the Flask BFF exactly as a browser would — multipart
   uploads, one-shot CSRF tokens, same-origin headers. Seeding the database
   directly would bypass the seam these tests exist to cover.

Waiting is bounded and loud: the local model takes ~2 minutes per large PDF, so
the parse waits are generous, and a timeout reports what state it got stuck in
rather than a bare assertion failure.
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

FRONTEND = os.environ.get("SMOKE_FRONTEND", "http://frontend:5000")
FIXTURES = Path(os.environ.get("SMOKE_FIXTURES", "/repo/fixtures"))

#: The local model is slow and this suite must not be flaky. A JD is text-only
#: and quick; a résumé PDF goes through extraction + embedding + graph
#: projection, and the peer has been measured at ~131s for a large one.
_JD_PARSE_TIMEOUT = 300
_RESUME_PARSE_TIMEOUT = 900
_RANK_TIMEOUT = 900

_REASON = "smoke: withdrawn to verify the audited reveal end to end"


@pytest.fixture(scope="session")
def client() -> Iterator[httpx.Client]:
    """A browser-like client: same-origin header on every request, cookies kept
    across the session so the one-shot CSRF tokens validate."""
    with httpx.Client(
        base_url=FRONTEND,
        timeout=60.0,
        follow_redirects=False,
        headers={"Origin": FRONTEND, "Referer": FRONTEND + "/"},
    ) as c:
        yield c


@pytest.fixture(scope="session", autouse=True)
def _preconditions(client: httpx.Client) -> None:
    """Fail — never skip — when the stack cannot actually be exercised."""
    try:
        root = client.get("/")
    except httpx.HTTPError as exc:  # pragma: no cover - environment failure
        pytest.fail(
            f"smoke: cannot reach the frontend at {FRONTEND} ({exc}). "
            "Start the stack with `docker compose up -d`."
        )
    if (
        root.status_code in (301, 302)
        and "cas" in root.headers.get("location", "").lower()
    ):
        pytest.fail(
            "smoke: CAS is ENABLED, so these tests cannot drive the UI. Run them "
            "against a stack with CAS_ENABLED=false, then pair-test the "
            "authenticated paths in a browser. Refusing to skip: a green smoke "
            "run that tested nothing is worse than no smoke run."
        )
    assert root.status_code == 200, f"frontend returned {root.status_code} for /"
    if not FIXTURES.is_dir():
        pytest.fail(f"smoke: fixtures directory {FIXTURES} not found")


def _page_token(client: httpx.Client, path: str = "/") -> str:
    body = client.get(path).text
    match = re.search(r'hx-headers=\'\{"X-CSRF-Token": "([^"]+)"\}\'', body)
    assert match, f"no page CSRF token on {path}"
    return match.group(1)


def _wait(what: str, probe: Any, timeout: int) -> Any:
    """Poll ``probe`` until it returns truthy. Reports the LAST observed state
    on timeout — a bare 'timed out' tells an operator nothing."""
    deadline = time.time() + timeout
    last: Any = None
    while time.time() < deadline:
        last = probe()
        if last:
            return last
        time.sleep(3)
    pytest.fail(f"smoke: timed out after {timeout}s waiting for {what}; last={last!r}")


@pytest.fixture(scope="session")
def ranked_job(client: httpx.Client) -> dict[str, Any]:
    """Create a job from a real JD, upload two real résumés, rank them, and
    withdraw the top candidate — all through the browser-facing routes.

    Returns the artefacts the assertions need, so the (slow) flow runs once per
    session rather than once per test.
    """
    token = _page_token(client)

    jd = sorted(FIXTURES.glob("JDs/*.docx"))[0]
    extracted = client.post(
        "/jobs/jd-extract",
        files={
            "file": (
                jd.name,
                jd.read_bytes(),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
        headers={"X-CSRF-Token": token},
    )
    assert extracted.status_code == 200, extracted.text[:300]
    description = extracted.text
    assert len(description) > 200, f"JD extraction returned {len(description)} chars"

    created = client.post(
        "/jobs",
        data={
            "title": "Smoke Test Position",
            "department": "Smoke",
            "description_raw": description,
            "blind_review": "on",
            "shortlist_top_percent": "100",
            "csrf_token": token,
        },
        headers={"X-CSRF-Token": token},
    )
    assert created.status_code == 302, created.text[:300]
    job_id = created.headers["location"].rstrip("/").rsplit("/", 1)[-1]

    # The fragment's OWN contract is the signal: while parsing it renders a
    # `badge-parsing` span and keeps its hx-trigger; once `parsed_at` is set it
    # renders the skill pills and drops both. It never contains the word
    # "parsed", which is what an earlier version of this probe assumed — the
    # same "assert the contract you imagined" mistake this suite exists to catch.
    _wait(
        "the JD to finish parsing",
        lambda: "badge-parsing" not in client.get(f"/jobs/{job_id}/parse-status").text,
        _JD_PARSE_TIMEOUT,
    )

    # A job accepts résumés only once it is OPEN — the upload form is not even
    # rendered on a draft. This is a real step in the recruiter's flow, so the
    # smoke suite performs it rather than reaching past it into the database.
    token = _page_token(client, f"/jobs/{job_id}")
    opened = client.post(
        f"/jobs/{job_id}/status",
        data={"to": "open", "csrf_token": token},
        headers={"X-CSRF-Token": token},
    )
    assert opened.status_code in (200, 302), opened.text[:300]

    # THREE résumés, so every test below owns a distinct candidate and none
    # depends on another having run first. A smoke suite is inherently stateful;
    # the way to keep it honest is to give each assertion its own subject rather
    # than rely on pytest's definition order and hope nobody reorders the file.
    resumes = sorted(FIXTURES.glob("resumes/*_resume.pdf"))[:3]
    assert len(resumes) == 3, f"need 3 résumé fixtures, found {len(resumes)}"
    token = _page_token(client, f"/jobs/{job_id}")
    uploaded = client.post(
        f"/jobs/{job_id}/resumes",
        data={"consent_acknowledged": "true", "csrf_token": token},
        files=[("files", (r.name, r.read_bytes(), "application/pdf")) for r in resumes],
        headers={"X-CSRF-Token": token},
        timeout=120.0,
    )
    assert uploaded.status_code in (200, 302), uploaded.text[:300]

    _wait(
        "all three résumés to finish parsing",
        lambda: client.get(f"/jobs/{job_id}/resumes-table").text.count("pill-parsed")
        >= 3,
        _RESUME_PARSE_TIMEOUT,
    )

    token = _page_token(client, f"/jobs/{job_id}/shortlist")
    client.post(f"/jobs/{job_id}/shortlist", headers={"X-CSRF-Token": token})
    shortlist_html = _wait(
        "the shortlist to be ranked",
        lambda: (
            html
            if "/withdraw" in (html := client.get(f"/jobs/{job_id}/shortlist").text)
            else None
        ),
        _RANK_TIMEOUT,
    )

    ids = re.findall(r"/resumes/([0-9a-f-]{36})/withdraw", shortlist_html)
    assert len(ids) >= 3, f"expected 3 ranked candidates, found {len(ids)}"

    # Withdraw the top candidate WITH a reason, through the card's own form —
    # this is the state the "left the shortlist", "marked on the job page" and
    # audited-reveal assertions all read. Done here rather than in a test so no
    # assertion depends on another test having run.
    form = _form_for(shortlist_html, f"/resumes/{ids[0]}/withdraw")
    withdrawn = client.post(
        f"/resumes/{ids[0]}/withdraw",
        data={
            "csrf_token": _hidden_value(form, "csrf_token"),
            "context": "shortlist",
            "job_id": job_id,
            "reason": _REASON,
        },
    )
    assert withdrawn.status_code == 302, withdrawn.text[:300]

    return {
        "job_id": job_id,
        "withdrawn_resume_id": ids[0],
        "redirect_probe_resume_id": ids[1],
        "active_resume_id": ids[2],
        "shortlist_html": shortlist_html,
        "page_token": _page_token(client, f"/jobs/{job_id}/shortlist"),
        "withdrawal_reason": _REASON,
    }


def _form_for(html: str, needle: str) -> str:
    idx = html.index(needle)
    return html[html.rindex("<form", 0, idx) : html.index("</form>", idx) + 7]


def _hidden_value(form_html: str, name: str) -> str:
    match = re.search(rf'name="{name}"\s+value="([^"]*)"', form_html)
    assert match, f"no hidden input named {name}"
    return match.group(1)
