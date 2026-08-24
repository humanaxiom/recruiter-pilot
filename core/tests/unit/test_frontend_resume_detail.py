"""Slice S6 — rebuilt résumé-detail view (``resume_detail.html``).

The backend ``GET /resumes/{id}`` (reveal=False) returns a ``ResumeOut`` whose
``parsed`` holds the redacted résumé. This slice renders ONLY the non-PII
structured fields (skills, experience, education, cover letter, source chunks)
plus an amber blind banner.

**Hard invariant (ADR-011/012/013) — the reason this app exists:** the
résumé-detail template has NO code branch capable of rendering
``candidate.name`` / ``candidate.email`` / ``candidate.phone`` /
``candidate.location``. This is proved structurally: even if
``api_client.get_resume`` is monkeypatched to return a payload that *contains* a
real name/email/phone (as a reveal=True response would), the rendered HTML must
not contain those bytes — because the template simply has no path that prints
them. ``get_resume`` is always called with ``reveal=False``.

**FU-4/D4 — CSRF on the reveal form (amended: per-résumé tokens).**
``POST /resumes/<id>/reveal`` requires a session-bound, one-shot anti-forgery
token (``frontend.csrf``, tested directly in ``test_frontend_csrf.py``): the
preceding ``GET /resumes/<id>`` mints a token FOR THAT RÉSUMÉ ID and renders
it into the reveal form as a hidden ``csrf_token`` input; the POST route
rejects (403) a missing/wrong/replayed/cross-résumé token, or a request
tagged with a cross-origin ``Origin`` header, WITHOUT ever calling
``api_client.reveal_resume`` — no reveal audit row is implied by a rejected
forgery attempt. Every reveal-POST test below mints a real token via
``_mint_csrf_token`` (which mints for a SPECIFIC ``resume_id``) before
posting.

Tokens are scoped per résumé, not per Flask session: the shortlist page (not
covered by this file) renders one reveal form per candidate card, all
posting to this SAME route — under the old per-session single-token design,
minting the second card's token silently invalidated the first card's, so
only the first reveal a recruiter clicked ever worked. The tests in the
"one-shot, now scoped per résumé" section below pin the fixed contract:
revealing résumé A must not disturb résumé B's token, and a token minted for
résumé A must never validate a reveal of résumé B.

**FU-8/ADR-026 — résumé withdrawal lifecycle (added below).** The résumé-
detail page gains a SECOND audited action, mirroring the reveal button
pattern: a "Withdraw candidate" button (posting to ``resume_withdraw``) when
``resume.withdrawn_at`` is falsy, or a "Reinstate" button (posting to
``resume_reinstate``) plus a withdrawn marker/badge when it is set. This is a
DISTINCT audited action from reveal — it must carry its OWN one-shot CSRF
token, independent of the reveal button's token on the same page (see
``test_frontend_csrf.py``'s "tokens scoped by (résumé id, action)" section
for the module-level regression this depends on). Blind posture is
unchanged: the withdraw/reinstate control sits OUTSIDE the
``{% if revealed %}...{% elif resume.blinded %}`` identity block, so it must
never require (or imply) a reveal.
"""

from __future__ import annotations

import datetime as _dt
import re
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from frontend import api_client
from frontend.app import app

_REAL_NAME = "Zzyzxqrst Wibblesworth"
_REAL_EMAIL = "zzyzxqrst.wibblesworth@example.test"
_REAL_PHONE = "604-555-0192"
_REAL_LOCATION = "Nowheresville, Yukon"

_CUR = _dt.date.today().year


@pytest.fixture
def client() -> Any:
    app.config.update(TESTING=True)
    return app.test_client()


def _parsed(*, with_pii: bool = False) -> dict[str, Any]:
    """A redacted ``ResumeParsed`` payload. With ``with_pii`` the identity
    fields are populated the way a reveal=True response would carry them —
    used by the structural byte-scan guard."""
    candidate: dict[str, Any]
    if with_pii:
        candidate = {
            "name": _REAL_NAME,
            "email": _REAL_EMAIL,
            "phone": _REAL_PHONE,
            "location": _REAL_LOCATION,
        }
    else:
        candidate = {"name": None, "email": None, "phone": None, "location": None}
    return {
        "candidate": candidate,
        "summary": "Seasoned backend engineer with a platform focus.",
        "total_years_experience": 8,
        "skills": [
            {
                "name": "Python",
                "years": 6,
                "last_used_year": _CUR,
                "evidence_chunk_ids": ["c_1"],
            },
            {"name": "Kubernetes", "years": 3, "last_used_year": _CUR - 4},
            {"name": "COBOL", "years": 2, "last_used_year": _CUR - 12},
            {"name": "Docker"},
        ],
        "experience": [
            {
                "company": "Employer A",
                "title": "Staff Engineer",
                "start": "2019",
                "end": "2024",
                "is_current": False,
                "bullets": [
                    {"text": "Led the platform rewrite.", "chunk_id": "c_5"},
                    {"text": "Mentored four engineers.", "chunk_id": None},
                ],
            }
        ],
        "education": [
            {
                "degree": "BSc",
                "institution": "Institution A",
                "field": "Computer Science",
                "year": 2014,
            }
        ],
        "chunks": [
            {"id": "c_1", "section": "skills", "page": 1, "text": "Expert in Python."}
        ],
        "cover_letter_chunks": [],
    }


def _resume(
    resume_id: Any,
    *,
    blinded: bool = True,
    with_pii: bool = False,
    cover_letter: bool = True,
    withdrawn_at: str | None = None,
    withdrawal_reason: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": str(resume_id),
        "job_id": str(uuid4()),
        "original_filename": "resume.pdf",
        "mime_type": "application/pdf",
        "file_size_bytes": 1234,
        "sha256": "0" * 64,
        "candidate": _parsed(with_pii=with_pii)["candidate"],
        "candidate_email_hash": None,
        "parsed": _parsed(with_pii=with_pii),
        "status": "parsed",
        "uploaded_by": None,
        "uploaded_at": "2026-07-01T00:00:00Z",
        "parsed_at": "2026-07-01T00:05:00Z",
        "failure_reason": None,
        "consent_acknowledged": True,
        "blinded": blinded,
        "cover_letter_text": None,
        "cover_letter_parsed": None,
        # ADR-026/FU-8: the withdrawal lifecycle pair. Both None for a résumé
        # that has never been withdrawn.
        "withdrawn_at": withdrawn_at,
        "withdrawal_reason": withdrawal_reason,
    }
    if cover_letter:
        payload["cover_letter_text"] = (
            "I am excited to bring my platform experience to your team."
        )
        payload["cover_letter_parsed"] = {
            "raw_text": ("I am excited to bring my platform experience to your team."),
            "themes": ["platform ownership", "mentorship"],
            "key_claims": ["scaled the API 10x"],
            "sentiment": "positive",
        }
    return payload


# ── FU-4/D4 CSRF helpers ─────────────────────────────────────────────────

_CSRF_INPUT_RE = re.compile(
    r'<input[^>]*name="csrf_token"[^>]*value="([^"]+)"'
    r'|<input[^>]*value="([^"]+)"[^>]*name="csrf_token"'
)


def _extract_csrf_token(html: str) -> str:
    match = _CSRF_INPUT_RE.search(html)
    assert match is not None, "expected a hidden csrf_token input in the reveal form"
    return match.group(1) or match.group(2)


def _mint_csrf_token(monkeypatch: Any, client: Any, resume_id: Any) -> str:
    """GET the résumé-detail page for ``resume_id`` (which must be blinded, so
    the reveal form — and its token — is rendered) and pull the
    freshly-minted ``csrf_token`` hidden-input value out of the response
    body. Mints a token bound to BOTH whichever session ``client`` is
    carrying cookies for AND this specific ``resume_id`` — a token minted via
    a call for one résumé id is not interchangeable with one minted for
    another (see the "one-shot, now scoped per résumé" tests below). Calling
    this repeatedly with different ``resume_id``s against the SAME ``client``
    (i.e. the same session) mints independent, simultaneously-valid tokens —
    this is what a multi-card shortlist render does in practice."""
    monkeypatch.setattr(
        api_client,
        "get_resume",
        MagicMock(return_value=_resume(resume_id, blinded=True)),
    )
    body = client.get(f"/resumes/{resume_id}").get_data(as_text=True)
    return _extract_csrf_token(body)


# ── FU-8/ADR-026 withdrawal-lifecycle helpers ───────────────────────────


def _extract_form_token(html: str, action_fragment: str) -> str:
    """Locate the ``<form>`` whose ``action`` attribute contains
    ``action_fragment`` (e.g. ``"/withdraw"``) and pull ITS OWN hidden
    ``csrf_token`` input value out of that form's body — used where a page
    renders MULTIPLE forms (each with its own independent token) so a plain
    "first csrf_token on the page" search would grab the wrong one."""
    form_re = re.compile(
        r'<form[^>]*action="[^"]*'
        + re.escape(action_fragment)
        + r'[^"]*"[^>]*>(.*?)</form>',
        re.DOTALL,
    )
    match = form_re.search(html)
    assert match is not None, f"expected a <form> posting to a {action_fragment} path"
    return _extract_csrf_token(match.group(1))


def _mint_withdraw_lifecycle_tokens(
    monkeypatch: Any, client: Any, resume_id: Any, *, withdrawn: bool = False
) -> str:
    """GET the résumé-detail page and pull the withdraw-or-reinstate form's
    OWN token (whichever is currently rendered, per ``withdrawn``)."""
    monkeypatch.setattr(
        api_client,
        "get_resume",
        MagicMock(
            return_value=_resume(
                resume_id,
                blinded=True,
                withdrawn_at="2026-07-20T00:00:00Z" if withdrawn else None,
            )
        ),
    )
    body = client.get(f"/resumes/{resume_id}").get_data(as_text=True)
    fragment = "/reinstate" if withdrawn else "/withdraw"
    return _extract_form_token(body, fragment)


# ── FU-7 §4 / ADR-030 — degraded-parse visibility ────────────────────────
#
# When skills extraction fell back to the deterministic keyword scan (the
# `resume_skills_v2` `LLMOutputInvalidError` catch), `parsed.degraded` is
# True and `parsed.degradation_reason` carries a short, PII-free note. The
# résumé-detail page must render a visible warning naming the reason and the
# ranking-exclusion consequence -- the 2026-07-19 incident's blind spot was
# exactly that nothing distinguished this from a clean parse.


def test_resume_detail_shows_degraded_warning_with_reason(
    monkeypatch: Any, client: Any
) -> None:
    resume_id = uuid4()
    resume = _resume(resume_id, blinded=True)
    resume["parsed"]["degraded"] = True
    resume["parsed"][
        "degradation_reason"
    ] = "skills extraction failed (AI); using keyword-scan fallback"
    monkeypatch.setattr(api_client, "get_resume", MagicMock(return_value=resume))
    body = client.get(f"/resumes/{resume_id}").get_data(as_text=True)
    assert "degraded" in body.lower()
    assert "skills extraction failed (AI); using keyword-scan fallback" in body
    assert "excluded from shortlists" in body.lower() or "excluded" in body.lower()


def test_resume_detail_omits_degraded_warning_for_a_clean_parse(
    monkeypatch: Any, client: Any
) -> None:
    resume_id = uuid4()
    resume = _resume(resume_id, blinded=True)
    resume["parsed"]["degraded"] = False
    resume["parsed"]["degradation_reason"] = None
    monkeypatch.setattr(api_client, "get_resume", MagicMock(return_value=resume))
    body = client.get(f"/resumes/{resume_id}").get_data(as_text=True)
    assert "degraded" not in body.lower()


def test_resume_detail_degraded_warning_renders_regardless_of_blind_state(
    monkeypatch: Any, client: Any
) -> None:
    """Non-PII: the degraded warning must render on a NON-blind job too."""
    resume_id = uuid4()
    resume = _resume(resume_id, blinded=False)
    resume["parsed"]["degraded"] = True
    resume["parsed"]["degradation_reason"] = "skills extraction failed (AI)"
    monkeypatch.setattr(api_client, "get_resume", MagicMock(return_value=resume))
    body = client.get(f"/resumes/{resume_id}").get_data(as_text=True)
    assert "degraded" in body.lower()


# ── section rendering ────────────────────────────────────────────────────


def test_resume_detail_renders_all_sections(monkeypatch: Any, client: Any) -> None:
    resume_id = uuid4()
    monkeypatch.setattr(
        api_client, "get_resume", MagicMock(return_value=_resume(resume_id))
    )
    body = client.get(f"/resumes/{resume_id}").get_data(as_text=True)
    # summary
    assert "Seasoned backend engineer" in body
    # skills
    assert "Python" in body
    assert "Kubernetes" in body
    assert "Docker" in body
    # experience: title + (generic) employer + dates
    assert "Staff Engineer" in body
    assert "Employer A" in body
    assert "2019" in body
    # experience bullets + cited chunk id
    assert "Led the platform rewrite." in body
    assert "c_5" in body
    # education
    assert "BSc" in body
    assert "Institution A" in body
    assert "Computer Science" in body


def test_resume_detail_blind_banner_shows_when_blinded(
    monkeypatch: Any, client: Any
) -> None:
    resume_id = uuid4()
    monkeypatch.setattr(
        api_client,
        "get_resume",
        MagicMock(return_value=_resume(resume_id, blinded=True)),
    )
    body = client.get(f"/resumes/{resume_id}").get_data(as_text=True)
    assert "blind-banner" in body
    assert "Identity hidden for blind review" in body


def test_resume_detail_blind_banner_absent_when_not_blinded(
    monkeypatch: Any, client: Any
) -> None:
    resume_id = uuid4()
    monkeypatch.setattr(
        api_client,
        "get_resume",
        MagicMock(return_value=_resume(resume_id, blinded=False)),
    )
    body = client.get(f"/resumes/{resume_id}").get_data(as_text=True)
    assert "Identity hidden for blind review" not in body


# ── skill recency colour-coding ──────────────────────────────────────────


def test_resume_detail_colour_codes_skills_by_recency(
    monkeypatch: Any, client: Any
) -> None:
    """A skill used this year is green (current), one used four years ago is
    amber (aging), one used twelve years ago is zinc (stale)."""
    resume_id = uuid4()
    monkeypatch.setattr(
        api_client, "get_resume", MagicMock(return_value=_resume(resume_id))
    )
    body = client.get(f"/resumes/{resume_id}").get_data(as_text=True)
    assert "chip-recency-current" in body
    assert "chip-recency-aging" in body
    assert "chip-recency-stale" in body
    # a small legend explaining the buckets
    assert "recency-legend" in body


def test_resume_detail_plain_chip_when_skill_has_no_recency(
    monkeypatch: Any, client: Any
) -> None:
    resume_id = uuid4()
    monkeypatch.setattr(
        api_client, "get_resume", MagicMock(return_value=_resume(resume_id))
    )
    body = client.get(f"/resumes/{resume_id}").get_data(as_text=True)
    # Docker carries no last_used_year → plain chip, no recency class on it.
    assert "Docker" in body


# ── cover letter ─────────────────────────────────────────────────────────


def test_resume_detail_renders_cover_letter(monkeypatch: Any, client: Any) -> None:
    resume_id = uuid4()
    monkeypatch.setattr(
        api_client,
        "get_resume",
        MagicMock(return_value=_resume(resume_id, cover_letter=True)),
    )
    body = client.get(f"/resumes/{resume_id}").get_data(as_text=True)
    assert "platform ownership" in body  # theme
    assert "positive" in body  # sentiment
    assert "I am excited to bring my platform experience" in body  # text


def test_resume_detail_without_cover_letter_omits_section(
    monkeypatch: Any, client: Any
) -> None:
    resume_id = uuid4()
    monkeypatch.setattr(
        api_client,
        "get_resume",
        MagicMock(return_value=_resume(resume_id, cover_letter=False)),
    )
    resp = client.get(f"/resumes/{resume_id}")
    assert resp.status_code == 200


# ── source chunks ────────────────────────────────────────────────────────


def test_resume_detail_shows_source_chunks_details(
    monkeypatch: Any, client: Any
) -> None:
    resume_id = uuid4()
    monkeypatch.setattr(
        api_client, "get_resume", MagicMock(return_value=_resume(resume_id))
    )
    body = client.get(f"/resumes/{resume_id}").get_data(as_text=True)
    assert "<details" in body
    assert "Expert in Python." in body


# ── redaction boundary ───────────────────────────────────────────────────


def test_resume_detail_calls_get_resume_with_reveal_false(
    monkeypatch: Any, client: Any
) -> None:
    resume_id = uuid4()
    spy = MagicMock(return_value=_resume(resume_id))
    monkeypatch.setattr(api_client, "get_resume", spy)
    client.get(f"/resumes/{resume_id}?reveal=true")
    spy.assert_called_once()
    _, kwargs = spy.call_args
    assert kwargs.get("reveal", False) is False


def test_resume_detail_structural_no_pii_byte_scan(
    monkeypatch: Any, client: Any
) -> None:
    """The DEFAULT (blind, non-reveal) GET must never print identity, even fed a
    payload that CONTAINS the candidate's real name/email/phone: the default
    render passes ``revealed=False`` so the identity block (the ONE place
    ``candidate.*`` is rendered) is never emitted. Identity is exposed only via
    the audited reveal POST — see ``test_resume_reveal_*`` below (FU-1)."""
    resume_id = uuid4()
    monkeypatch.setattr(
        api_client,
        "get_resume",
        MagicMock(return_value=_resume(resume_id, with_pii=True)),
    )
    raw = client.get(f"/resumes/{resume_id}?reveal=true").get_data(as_text=True)
    assert _REAL_NAME not in raw
    assert _REAL_EMAIL not in raw
    assert _REAL_PHONE not in raw
    assert _REAL_LOCATION not in raw


def test_resume_detail_default_get_never_calls_reveal(
    monkeypatch: Any, client: Any
) -> None:
    """The blind default GET must never route through the audited reveal path —
    a plain view is not a reveal (and must not write an audit row)."""
    resume_id = uuid4()
    monkeypatch.setattr(
        api_client, "get_resume", MagicMock(return_value=_resume(resume_id))
    )
    reveal_spy = MagicMock()
    monkeypatch.setattr(api_client, "reveal_resume", reveal_spy)
    client.get(f"/resumes/{resume_id}")
    reveal_spy.assert_not_called()


def test_resume_reveal_shows_identity_and_audit_notice(
    monkeypatch: Any, client: Any
) -> None:
    """FU-1: POST /resumes/<id>/reveal renders the UN-blinded identity and the
    audit notice. This is the ONE render path that surfaces ``candidate.*``.
    FU-4/D4: requires a valid one-shot CSRF token, minted for THIS résumé id
    by the preceding GET."""
    resume_id = uuid4()
    token = _mint_csrf_token(monkeypatch, client, resume_id)
    monkeypatch.setattr(
        api_client,
        "reveal_resume",
        MagicMock(return_value=_resume(resume_id, with_pii=True)),
    )
    raw = client.post(
        f"/resumes/{resume_id}/reveal", data={"csrf_token": token}
    ).get_data(as_text=True)
    assert _REAL_NAME in raw
    assert _REAL_EMAIL in raw
    assert _REAL_PHONE in raw
    assert "audit log" in raw.lower()


def test_resume_reveal_forwards_context_from_form(
    monkeypatch: Any, client: Any
) -> None:
    """A reveal triggered from the shortlist card posts ``context=shortlist``;
    the route forwards it so the audit row records the true origin. FU-4/D4:
    also requires a valid CSRF token alongside ``context``."""
    resume_id = uuid4()
    token = _mint_csrf_token(monkeypatch, client, resume_id)
    spy = MagicMock(return_value=_resume(resume_id, with_pii=True))
    monkeypatch.setattr(api_client, "reveal_resume", spy)
    client.post(
        f"/resumes/{resume_id}/reveal",
        data={"context": "shortlist", "csrf_token": token},
    )
    spy.assert_called_once()
    assert spy.call_args.kwargs["context"] == "shortlist"


def test_resume_reveal_context_defaults_to_resume_detail(
    monkeypatch: Any, client: Any
) -> None:
    resume_id = uuid4()
    token = _mint_csrf_token(monkeypatch, client, resume_id)
    spy = MagicMock(return_value=_resume(resume_id, with_pii=True))
    monkeypatch.setattr(api_client, "reveal_resume", spy)
    client.post(f"/resumes/{resume_id}/reveal", data={"csrf_token": token})
    spy.assert_called_once()
    assert spy.call_args.kwargs["context"] == "resume_detail"


def test_resume_reveal_uses_audited_endpoint_not_raw_reveal(
    monkeypatch: Any, client: Any
) -> None:
    """The reveal must go through ``reveal_resume`` (POST, which audits), NOT a
    raw ``get_resume(reveal=True)`` that would skip the audit trail. FU-4/D4:
    the token-minting GET legitimately calls ``get_resume`` once — reset the
    spy before the assertion-relevant POST so that call doesn't get
    conflated with a (forbidden) reveal-time ``get_resume`` call."""
    resume_id = uuid4()
    get_spy = MagicMock(return_value=_resume(resume_id, with_pii=True, blinded=True))
    monkeypatch.setattr(api_client, "get_resume", get_spy)
    body = client.get(f"/resumes/{resume_id}").get_data(as_text=True)
    token = _extract_csrf_token(body)
    get_spy.reset_mock()

    reveal_spy = MagicMock(return_value=_resume(resume_id, with_pii=True))
    monkeypatch.setattr(api_client, "reveal_resume", reveal_spy)
    client.post(f"/resumes/{resume_id}/reveal", data={"csrf_token": token})
    reveal_spy.assert_called_once()
    get_spy.assert_not_called()


def test_resume_detail_candidate_refs_only_inside_reveal_branch() -> None:
    """Structural guard (updated for FU-1): the template MAY reference
    ``candidate.*`` — but every such reference must sit inside the single
    ``{% if revealed %}`` block, so no non-reveal render can reach it."""
    template = (Path(app.root_path) / "templates" / "resume_detail.html").read_text(
        encoding="utf-8"
    )
    gate = template.find("{% if revealed %}")
    elif_marker = template.find("{% elif resume.blinded %}")
    assert gate != -1, "expected an `{% if revealed %}` gate in resume_detail.html"
    assert elif_marker > gate, "expected the reveal block to end at `{% elif %}`"
    # Every candidate.* reference must fall strictly inside the reveal-only span
    # (between the `revealed` gate and the `elif blinded` branch), so no
    # non-reveal render can reach it. Robust to the nested per-field ifs.
    banned = (
        "candidate.name",
        "candidate.email",
        "candidate.phone",
        "candidate.location",
        "candidate['name']",
        "candidate['email']",
        "candidate['phone']",
        "candidate['location']",
    )
    for token in banned:
        idx = template.find(token)
        while idx != -1:
            assert gate < idx < elif_marker, f"{token} outside the reveal block"
            idx = template.find(token, idx + 1)


# ── FU-4/D4 — CSRF on the reveal form ─────────────────────────────────────


def test_resume_detail_get_renders_a_csrf_token_hidden_input(
    monkeypatch: Any, client: Any
) -> None:
    resume_id = uuid4()
    monkeypatch.setattr(
        api_client,
        "get_resume",
        MagicMock(return_value=_resume(resume_id, blinded=True)),
    )
    body = client.get(f"/resumes/{resume_id}").get_data(as_text=True)
    token = _extract_csrf_token(body)
    assert len(token) >= 16


def test_resume_reveal_without_a_token_is_rejected_and_backend_never_called(
    monkeypatch: Any, client: Any
) -> None:
    """No token at all (e.g. a cross-site auto-submitting form, which supplies
    no session cookie's token) → 403, and CRITICALLY the audited reveal call
    never happens — no reveal_audit row is implied by a rejected forgery."""
    resume_id = uuid4()
    reveal_spy = MagicMock(return_value=_resume(resume_id, with_pii=True))
    monkeypatch.setattr(api_client, "reveal_resume", reveal_spy)
    resp = client.post(f"/resumes/{resume_id}/reveal")
    assert resp.status_code == 403
    reveal_spy.assert_not_called()


def test_resume_reveal_with_a_garbage_token_is_rejected_and_backend_never_called(
    monkeypatch: Any, client: Any
) -> None:
    resume_id = uuid4()
    _mint_csrf_token(monkeypatch, client, resume_id)  # a real token now exists…
    reveal_spy = MagicMock(return_value=_resume(resume_id, with_pii=True))
    monkeypatch.setattr(api_client, "reveal_resume", reveal_spy)
    # …but we submit a different, made-up value instead of it.
    resp = client.post(
        f"/resumes/{resume_id}/reveal", data={"csrf_token": "not-the-real-token"}
    )
    assert resp.status_code == 403
    reveal_spy.assert_not_called()


def test_resume_reveal_with_a_token_from_a_different_session_is_rejected(
    monkeypatch: Any, client: Any
) -> None:
    """A token minted for one browser session must not validate a POST arriving
    with a different session's cookie — the token is session-bound."""
    resume_id = uuid4()
    token = _mint_csrf_token(monkeypatch, client, resume_id)
    other_client = app.test_client()
    reveal_spy = MagicMock(return_value=_resume(resume_id, with_pii=True))
    monkeypatch.setattr(api_client, "reveal_resume", reveal_spy)
    resp = other_client.post(f"/resumes/{resume_id}/reveal", data={"csrf_token": token})
    assert resp.status_code == 403
    reveal_spy.assert_not_called()


def test_resume_reveal_with_a_valid_token_succeeds(
    monkeypatch: Any, client: Any
) -> None:
    resume_id = uuid4()
    token = _mint_csrf_token(monkeypatch, client, resume_id)
    reveal_spy = MagicMock(return_value=_resume(resume_id, with_pii=True))
    monkeypatch.setattr(api_client, "reveal_resume", reveal_spy)
    resp = client.post(f"/resumes/{resume_id}/reveal", data={"csrf_token": token})
    assert resp.status_code == 200
    reveal_spy.assert_called_once()
    assert _REAL_NAME in resp.get_data(as_text=True)


def test_resume_reveal_cross_origin_request_is_rejected_even_with_a_valid_token(
    monkeypatch: Any, client: Any
) -> None:
    """The Origin/Referer same-origin check is layered ON TOP of the token, not
    instead of it: a cross-origin Origin header must reject the request even
    when the (correct, unconsumed) token is present."""
    resume_id = uuid4()
    token = _mint_csrf_token(monkeypatch, client, resume_id)
    reveal_spy = MagicMock(return_value=_resume(resume_id, with_pii=True))
    monkeypatch.setattr(api_client, "reveal_resume", reveal_spy)
    resp = client.post(
        f"/resumes/{resume_id}/reveal",
        data={"csrf_token": token},
        headers={"Origin": "http://evil.example"},
    )
    assert resp.status_code == 403
    reveal_spy.assert_not_called()


# ── FU-4/D4 amendment — one-shot is now scoped PER RÉSUMÉ, not per session ──
#
# WHY this section exists: the shipped design stored a single bare token in
# the Flask session, so minting a SECOND résumé's token silently invalidated
# the FIRST résumé's still-unused token. The reveal button appears on every
# shortlist card, all posting to this SAME route — so only the first card a
# recruiter clicked ever worked; every other card 403'd until a full page
# reload re-minted a fresh single token. Reveal-from-shortlist is the primary
# FU-1 workflow, so this was a functional regression. The tests below pin the
# fixed contract.


def test_resume_reveal_token_is_single_use_per_resume(
    monkeypatch: Any, client: Any
) -> None:
    """One-shot is now per résumé, not per session: revealing résumé A must
    NOT invalidate résumé B's token, but replaying A's (already-consumed)
    token must still fail."""
    resume_a, resume_b = uuid4(), uuid4()
    token_a = _mint_csrf_token(monkeypatch, client, resume_a)
    token_b = _mint_csrf_token(monkeypatch, client, resume_b)
    reveal_spy = MagicMock(
        side_effect=lambda rid, **kwargs: _resume(rid, with_pii=True)
    )
    monkeypatch.setattr(api_client, "reveal_resume", reveal_spy)

    first = client.post(f"/resumes/{resume_a}/reveal", data={"csrf_token": token_a})
    assert first.status_code == 200

    # B's still-unused token is untouched by A's reveal.
    second = client.post(f"/resumes/{resume_b}/reveal", data={"csrf_token": token_b})
    assert second.status_code == 200

    # Replaying A's already-consumed token still fails (one-shot preserved).
    replay = client.post(f"/resumes/{resume_a}/reveal", data={"csrf_token": token_a})
    assert replay.status_code == 403
    assert reveal_spy.call_count == 2


def test_resume_reveal_token_minted_for_a_different_resume_is_rejected(
    monkeypatch: Any, client: Any
) -> None:
    """The new cross-résumé confusion risk this amendment introduces: a token
    minted for résumé A must NOT validate a reveal POST aimed at résumé B,
    even though both tokens live in the same session simultaneously."""
    resume_a, resume_b = uuid4(), uuid4()
    token_a = _mint_csrf_token(monkeypatch, client, resume_a)
    reveal_spy = MagicMock(return_value=_resume(resume_b, with_pii=True))
    monkeypatch.setattr(api_client, "reveal_resume", reveal_spy)

    resp = client.post(f"/resumes/{resume_b}/reveal", data={"csrf_token": token_a})
    assert resp.status_code == 403
    reveal_spy.assert_not_called()


def test_resume_reveal_token_minted_for_a_different_resume_does_not_burn_its_own_slot(
    monkeypatch: Any, client: Any
) -> None:
    """A misdirected reveal attempt (résumé A's token posted against résumé
    B's route) must not consume résumé A's real, rightful token slot — A must
    still be revealable afterwards with its own token."""
    resume_a, resume_b = uuid4(), uuid4()
    token_a = _mint_csrf_token(monkeypatch, client, resume_a)
    reveal_spy = MagicMock(
        side_effect=lambda rid, **kwargs: _resume(rid, with_pii=True)
    )
    monkeypatch.setattr(api_client, "reveal_resume", reveal_spy)

    misdirected = client.post(
        f"/resumes/{resume_b}/reveal", data={"csrf_token": token_a}
    )
    assert misdirected.status_code == 403

    correct = client.post(f"/resumes/{resume_a}/reveal", data={"csrf_token": token_a})
    assert correct.status_code == 200
    reveal_spy.assert_called_once()


def test_two_resumes_on_one_shortlist_render_can_both_be_revealed_without_a_page_reload(
    monkeypatch: Any, client: Any
) -> None:
    """THE regression test this amendment exists to fix. A shortlist render
    mints one token per candidate card, all into the SAME Flask session (here
    simulated as two back-to-back token-minting GETs against the same
    ``client``, mirroring two cards rendered on one page). Under the old
    per-SESSION single token, minting card B's token silently invalidated
    card A's, so the second reveal always 403'd until the page re-rendered.
    With per-résumé tokens, BOTH cards mint independently and BOTH reveals
    succeed back-to-back — with NO intervening GET/page reload between the two
    POSTs below."""
    resume_a, resume_b = uuid4(), uuid4()
    token_a = _mint_csrf_token(monkeypatch, client, resume_a)
    token_b = _mint_csrf_token(monkeypatch, client, resume_b)
    assert token_a != token_b

    reveal_spy = MagicMock(
        side_effect=lambda rid, **kwargs: _resume(rid, with_pii=True)
    )
    monkeypatch.setattr(api_client, "reveal_resume", reveal_spy)

    # No page reload / re-mint between these two reveal POSTs.
    resp_a = client.post(f"/resumes/{resume_a}/reveal", data={"csrf_token": token_a})
    resp_b = client.post(f"/resumes/{resume_b}/reveal", data={"csrf_token": token_b})

    assert resp_a.status_code == 200
    assert resp_b.status_code == 200
    assert reveal_spy.call_count == 2
    assert _REAL_NAME in resp_a.get_data(as_text=True)
    assert _REAL_NAME in resp_b.get_data(as_text=True)


# ── FU-8/ADR-026 — résumé withdrawal lifecycle ───────────────────────────
#
# A "Withdraw candidate" control (when not withdrawn) or a "Reinstate"
# control plus a withdrawn marker (when withdrawn), rendered on the résumé-
# detail page. Mirrors the audited-reveal pattern (ADR-016): a POST-only
# route, guarded by same-origin + a session-bound one-shot CSRF token that is
# INDEPENDENT of the reveal button's token (FU-8/ADR-026 amends
# ``frontend.csrf`` to key by (résumé id, action) — see
# ``test_frontend_csrf.py``).


def test_resume_detail_shows_withdraw_button_when_not_withdrawn(
    monkeypatch: Any, client: Any
) -> None:
    resume_id = uuid4()
    monkeypatch.setattr(
        api_client,
        "get_resume",
        MagicMock(return_value=_resume(resume_id, blinded=True, withdrawn_at=None)),
    )
    body = client.get(f"/resumes/{resume_id}").get_data(as_text=True)
    assert "Withdraw candidate" in body
    assert f"/resumes/{resume_id}/withdraw" in body
    assert "Reinstate" not in body


def test_resume_detail_shows_reinstate_button_and_badge_when_withdrawn(
    monkeypatch: Any, client: Any
) -> None:
    resume_id = uuid4()
    monkeypatch.setattr(
        api_client,
        "get_resume",
        MagicMock(
            return_value=_resume(
                resume_id, blinded=True, withdrawn_at="2026-07-20T00:00:00Z"
            )
        ),
    )
    body = client.get(f"/resumes/{resume_id}").get_data(as_text=True)
    assert "Reinstate" in body
    assert f"/resumes/{resume_id}/reinstate" in body
    assert "Withdraw candidate" not in body
    # a withdrawn marker/badge is visible somewhere on the page
    assert "Withdrawn" in body


def test_resume_detail_shows_withdrawal_reason_when_present(
    monkeypatch: Any, client: Any
) -> None:
    resume_id = uuid4()
    monkeypatch.setattr(
        api_client,
        "get_resume",
        MagicMock(
            return_value=_resume(
                resume_id,
                blinded=True,
                withdrawn_at="2026-07-20T00:00:00Z",
                withdrawal_reason="Accepted another offer",
            )
        ),
    )
    body = client.get(f"/resumes/{resume_id}").get_data(as_text=True)
    assert "Accepted another offer" in body


def test_resume_detail_withdraw_and_reveal_tokens_on_the_same_page_are_independent(
    monkeypatch: Any, client: Any
) -> None:
    """The crux regression: two audited actions rendered on ONE page must
    never share a CSRF slot."""
    resume_id = uuid4()
    monkeypatch.setattr(
        api_client,
        "get_resume",
        MagicMock(return_value=_resume(resume_id, blinded=True, withdrawn_at=None)),
    )
    body = client.get(f"/resumes/{resume_id}").get_data(as_text=True)
    reveal_token = _extract_form_token(body, "/reveal")
    withdraw_token = _extract_form_token(body, "/withdraw")
    assert reveal_token != withdraw_token


def test_resume_detail_withdraw_control_is_outside_the_reveal_identity_block() -> None:
    """Structural guard: blind posture is UNCHANGED by this feature — the
    withdraw/reinstate control must render regardless of reveal state, so it
    must sit OUTSIDE the ``{% if revealed %}...{% elif resume.blinded %}
    ...{% endif %}`` identity block (found via balanced if/elif/endif
    matching, robust to the nested per-field ifs already inside that block)."""
    template = (Path(app.root_path) / "templates" / "resume_detail.html").read_text(
        encoding="utf-8"
    )
    control_re = re.compile(r"\{%-?\s*(if|elif|endif)\b[^%]*%\}")
    gate = template.find("{% if revealed %}")
    assert gate != -1, "expected an `{% if revealed %}` gate in resume_detail.html"

    depth = 0
    block_end = -1
    for m in control_re.finditer(template, gate):
        keyword = m.group(1)
        if keyword == "if":
            depth += 1
        elif keyword == "endif":
            depth -= 1
            if depth == 0:
                block_end = m.end()
                break
    assert block_end != -1, "could not find the matching endif for the reveal block"

    withdraw_idx = template.find("Withdraw candidate")
    assert withdraw_idx != -1, "expected a 'Withdraw candidate' control in the template"
    assert not (gate < withdraw_idx < block_end), (
        "the withdraw control must not be nested inside the reveal/blind "
        "identity block — blind posture is unchanged by this feature"
    )


# ── FU-8/ADR-026 — POST /resumes/<id>/withdraw ───────────────────────────


def test_resume_withdraw_route_is_post_only(client: Any) -> None:
    resp = client.get(f"/resumes/{uuid4()}/withdraw")
    assert resp.status_code == 405


def test_resume_withdraw_route_posts_and_redirects(
    monkeypatch: Any, client: Any
) -> None:
    resume_id = uuid4()
    token = _mint_withdraw_lifecycle_tokens(monkeypatch, client, resume_id)
    spy = MagicMock(return_value=_resume(resume_id, blinded=True))
    monkeypatch.setattr(api_client, "withdraw_resume", spy)
    resp = client.post(f"/resumes/{resume_id}/withdraw", data={"csrf_token": token})
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith(f"/resumes/{resume_id}")
    spy.assert_called_once()
    assert spy.call_args.args[0] == resume_id


def test_resume_withdraw_route_forwards_the_reason_field(
    monkeypatch: Any, client: Any
) -> None:
    resume_id = uuid4()
    token = _mint_withdraw_lifecycle_tokens(monkeypatch, client, resume_id)
    spy = MagicMock(return_value=_resume(resume_id, blinded=True))
    monkeypatch.setattr(api_client, "withdraw_resume", spy)
    client.post(
        f"/resumes/{resume_id}/withdraw",
        data={"csrf_token": token, "reason": "Accepted another offer"},
    )
    spy.assert_called_once()
    assert spy.call_args.kwargs.get("reason") == "Accepted another offer"


def test_resume_withdraw_route_rejects_missing_csrf_token(
    monkeypatch: Any, client: Any
) -> None:
    resume_id = uuid4()
    spy = MagicMock(return_value=_resume(resume_id))
    monkeypatch.setattr(api_client, "withdraw_resume", spy)
    resp = client.post(f"/resumes/{resume_id}/withdraw")
    assert resp.status_code == 403
    spy.assert_not_called()


def test_resume_withdraw_route_rejects_a_garbage_csrf_token(
    monkeypatch: Any, client: Any
) -> None:
    resume_id = uuid4()
    _mint_withdraw_lifecycle_tokens(monkeypatch, client, resume_id)
    spy = MagicMock(return_value=_resume(resume_id))
    monkeypatch.setattr(api_client, "withdraw_resume", spy)
    resp = client.post(
        f"/resumes/{resume_id}/withdraw", data={"csrf_token": "not-the-real-token"}
    )
    assert resp.status_code == 403
    spy.assert_not_called()


def test_resume_withdraw_route_rejects_cross_origin_even_with_a_valid_token(
    monkeypatch: Any, client: Any
) -> None:
    resume_id = uuid4()
    token = _mint_withdraw_lifecycle_tokens(monkeypatch, client, resume_id)
    spy = MagicMock(return_value=_resume(resume_id))
    monkeypatch.setattr(api_client, "withdraw_resume", spy)
    resp = client.post(
        f"/resumes/{resume_id}/withdraw",
        data={"csrf_token": token},
        headers={"Origin": "http://evil.example"},
    )
    assert resp.status_code == 403
    spy.assert_not_called()


# ── FU-8/ADR-026 — POST /resumes/<id>/reinstate ──────────────────────────


def test_resume_reinstate_route_is_post_only(client: Any) -> None:
    resp = client.get(f"/resumes/{uuid4()}/reinstate")
    assert resp.status_code == 405


def test_resume_reinstate_route_posts_and_redirects(
    monkeypatch: Any, client: Any
) -> None:
    resume_id = uuid4()
    token = _mint_withdraw_lifecycle_tokens(
        monkeypatch, client, resume_id, withdrawn=True
    )
    spy = MagicMock(return_value=_resume(resume_id, blinded=True))
    monkeypatch.setattr(api_client, "reinstate_resume", spy)
    resp = client.post(f"/resumes/{resume_id}/reinstate", data={"csrf_token": token})
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith(f"/resumes/{resume_id}")
    spy.assert_called_once()
    assert spy.call_args.args[0] == resume_id


def test_resume_reinstate_route_rejects_missing_csrf_token(
    monkeypatch: Any, client: Any
) -> None:
    resume_id = uuid4()
    spy = MagicMock(return_value=_resume(resume_id))
    monkeypatch.setattr(api_client, "reinstate_resume", spy)
    resp = client.post(f"/resumes/{resume_id}/reinstate")
    assert resp.status_code == 403
    spy.assert_not_called()


def test_resume_reinstate_route_rejects_cross_origin_even_with_a_valid_token(
    monkeypatch: Any, client: Any
) -> None:
    resume_id = uuid4()
    token = _mint_withdraw_lifecycle_tokens(
        monkeypatch, client, resume_id, withdrawn=True
    )
    spy = MagicMock(return_value=_resume(resume_id))
    monkeypatch.setattr(api_client, "reinstate_resume", spy)
    resp = client.post(
        f"/resumes/{resume_id}/reinstate",
        data={"csrf_token": token},
        headers={"Origin": "http://evil.example"},
    )
    assert resp.status_code == 403
    spy.assert_not_called()


# ── FU-8/ADR-026 — cross-action token confusion (route level) ───────────
#
# Route-level mirror of the module-level regression tests in
# test_frontend_csrf.py: the withdraw button's token must not be usable to
# trigger a reveal, and vice versa, even though both live in the SAME
# session for the SAME résumé at once.


def test_withdraw_token_does_not_validate_the_reveal_route_and_vice_versa(
    monkeypatch: Any, client: Any
) -> None:
    resume_id = uuid4()
    monkeypatch.setattr(
        api_client,
        "get_resume",
        MagicMock(return_value=_resume(resume_id, blinded=True, withdrawn_at=None)),
    )
    body = client.get(f"/resumes/{resume_id}").get_data(as_text=True)
    reveal_token = _extract_form_token(body, "/reveal")
    withdraw_token = _extract_form_token(body, "/withdraw")
    assert reveal_token != withdraw_token

    reveal_spy = MagicMock(return_value=_resume(resume_id, with_pii=True))
    monkeypatch.setattr(api_client, "reveal_resume", reveal_spy)
    withdraw_spy = MagicMock(return_value=_resume(resume_id, blinded=True))
    monkeypatch.setattr(api_client, "withdraw_resume", withdraw_spy)

    # withdraw's token posted at the reveal route → rejected, no reveal call.
    resp1 = client.post(
        f"/resumes/{resume_id}/reveal", data={"csrf_token": withdraw_token}
    )
    assert resp1.status_code == 403
    reveal_spy.assert_not_called()

    # reveal's token posted at the withdraw route → rejected, no withdraw call.
    resp2 = client.post(
        f"/resumes/{resume_id}/withdraw", data={"csrf_token": reveal_token}
    )
    assert resp2.status_code == 403
    withdraw_spy.assert_not_called()

    # each token still works on ITS OWN route afterward — neither was leaked
    # or burned by the misdirected cross-action attempt above.
    resp3 = client.post(
        f"/resumes/{resume_id}/reveal", data={"csrf_token": reveal_token}
    )
    assert resp3.status_code == 200
    resp4 = client.post(
        f"/resumes/{resume_id}/withdraw", data={"csrf_token": withdraw_token}
    )
    assert resp4.status_code == 302


# ── D1 = option C follow-on: the form must actually COLLECT a reason ──────
#
# Found by running the product, not by the suite. `resume_withdraw` reads
# `request.form.get("reason")` and `test_resume_withdraw_route_forwards_the_
# reason_field` above proves it forwards one — by POSTing the field directly.
# No rendered form ever contained it. So in the real application every
# withdrawal recorded `reason = None`: all five withdrawals in the live dev
# database have a NULL `audit_log.details`, and that is structural, not
# incidental.
#
# That makes the audited-reveal control shipped in this branch dead on
# arrival — a button to disclose a value the product cannot record. The
# decision the owner answered (D1 = C) is about a field the UI never offered.
#
# This is the ROADMAP A7 shape one layer out, and the same class as the
# `polling` defect in ADR-043: the plumbing was proved end to end and the
# screen was never asked.


def test_the_withdraw_form_actually_collects_a_reason(
    monkeypatch: Any, client: Any
) -> None:
    """The gap between "the route forwards a reason" and "a person can type
    one". Only the second makes the reveal control mean anything."""
    resume_id = uuid4()
    monkeypatch.setattr(
        api_client,
        "get_resume",
        MagicMock(return_value=_resume(resume_id, blinded=True, withdrawn_at=None)),
    )
    body = client.get(f"/resumes/{resume_id}").get_data(as_text=True)
    start = body.index("/withdraw")
    form = body[start : body.index("</form>", start)]
    assert 'name="reason"' in form, (
        "the withdraw form has no reason field, so every withdrawal records "
        "None and the audited reveal has nothing to reveal"
    )


def test_the_reason_field_is_capped_at_the_backends_own_limit(
    monkeypatch: Any, client: Any
) -> None:
    """``WithdrawRequest.reason`` is ``max_length=500``. A form that lets
    someone type 900 characters and then 422s on submit loses the note they
    wrote — and a withdrawal note is written once, at the moment of a decision
    someone may later be asked to justify."""
    resume_id = uuid4()
    monkeypatch.setattr(
        api_client,
        "get_resume",
        MagicMock(return_value=_resume(resume_id, blinded=True, withdrawn_at=None)),
    )
    body = client.get(f"/resumes/{resume_id}").get_data(as_text=True)
    start = body.index("/withdraw")
    form = body[start : body.index("</form>", start)]
    assert 'maxlength="500"' in form


def test_the_form_says_the_note_can_be_disclosed_to_an_auditor(
    monkeypatch: Any, client: Any
) -> None:
    """**The privacy point, and it is the reason this is on THIS branch.**

    D1's memo turns on an asymmetry: prose already in ``audit_log`` was typed
    under a withheld expectation, and disclosing it later changes the status of
    data already collected. Option C keeps existing rows closed and opens new
    ones deliberately — which only holds if the person typing a NEW note is
    told it can be revealed. Without that line, C quietly reproduces the
    retroactive problem it was chosen to avoid, one row at a time.
    """
    resume_id = uuid4()
    monkeypatch.setattr(
        api_client,
        "get_resume",
        MagicMock(return_value=_resume(resume_id, blinded=True, withdrawn_at=None)),
    )
    body = client.get(f"/resumes/{resume_id}").get_data(as_text=True).lower()
    assert "auditor" in body


# ── withdrawing from a shortlist card must return to the shortlist ────────
#
# Reported from the live product, 2026-08-20: "withdraw candidate doesn't work
# as expected — candidate still shows", on the SHORTLIST screen.
#
# The withdrawal itself always worked (the row is written, and every shortlist
# read filters it out). What did not work is where the user ended up. The card's
# form has posted `context=shortlist` since FU-8 — and
# `test_shortlist_card_withdraw_form_carries_context_shortlist` has pinned that
# it does — but `resume_withdraw` never READ it, redirecting unconditionally to
# the résumé-detail page.
#
# So: click Withdraw on the shortlist, get thrown to a different page, press
# Back, and the browser serves the CACHED shortlist with the candidate still on
# it. Withdrawal looks like it did nothing.
#
# The signal was designed and nothing consumed it — the same shape as the three
# other defects found by running the product today.


def test_withdraw_from_a_shortlist_card_returns_to_that_shortlist(
    monkeypatch: Any, client: Any
) -> None:
    resume_id = uuid4()
    job_id = uuid4()
    token = _mint_withdraw_lifecycle_tokens(monkeypatch, client, resume_id)
    monkeypatch.setattr(
        api_client,
        "withdraw_resume",
        MagicMock(return_value=_resume(resume_id, blinded=True)),
    )
    resp = client.post(
        f"/resumes/{resume_id}/withdraw",
        data={"csrf_token": token, "context": "shortlist", "job_id": str(job_id)},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith(f"/jobs/{job_id}/shortlist"), (
        "withdrawing from a shortlist card dumped the user on the résumé page; "
        "pressing Back then shows a cached list with the candidate still on it"
    )


def test_withdraw_from_the_resume_page_still_returns_there(
    monkeypatch: Any, client: Any
) -> None:
    """No regression for the other caller, which sends no context at all."""
    resume_id = uuid4()
    token = _mint_withdraw_lifecycle_tokens(monkeypatch, client, resume_id)
    monkeypatch.setattr(
        api_client,
        "withdraw_resume",
        MagicMock(return_value=_resume(resume_id, blinded=True)),
    )
    resp = client.post(
        f"/resumes/{resume_id}/withdraw",
        data={"csrf_token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith(f"/resumes/{resume_id}")


@pytest.mark.parametrize("job_id", ["", "not-a-uuid", "../../etc/passwd"])
def test_a_missing_or_junk_job_id_falls_back_to_the_resume_page(
    monkeypatch: Any, client: Any, job_id: str
) -> None:
    """The return address is attacker-controllable form input, so it is parsed
    as a UUID and the URL is built server-side from it — never echoed into a
    Location header. Anything unparseable degrades to the résumé page rather
    than 500ing or redirecting somewhere chosen by the request."""
    resume_id = uuid4()
    token = _mint_withdraw_lifecycle_tokens(monkeypatch, client, resume_id)
    monkeypatch.setattr(
        api_client,
        "withdraw_resume",
        MagicMock(return_value=_resume(resume_id, blinded=True)),
    )
    resp = client.post(
        f"/resumes/{resume_id}/withdraw",
        data={"csrf_token": token, "context": "shortlist", "job_id": job_id},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith(f"/resumes/{resume_id}")
