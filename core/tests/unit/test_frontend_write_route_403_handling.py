"""RED pin — security finding, fix/auth-boundary-fails-open (F4).

**The proven regression.** ADR-033 made every write route on the FastAPI
backend check the CAS session role, which now (this slice) 403s a
hiring_manager/auditor session on every write route. ``core/frontend/app.py``
was never updated to expect that new 403: ``api_client._raise_for_status``
maps ANY backend 4xx other than 404/409 to ``api_client.BadRequest`` (see
``api_client.py``'s own docstring), and six of this module's write routes —
``transition_status`` (``app.py:625``), ``blind_review`` (``app.py:641``),
``generate_shortlist`` (``app.py:715``), ``resume_withdraw``
(``app.py:899``), ``resume_reinstate`` (``app.py:924``), and
``resume_match_jobs`` (``app.py:983``) — catch only ``api_client.NotFound``
and ``api_client.BackendUnavailable``. A ``BadRequest`` (what a 403 now
becomes) is not caught by any of them, so it propagates as an unhandled
exception: a hiring_manager or auditor clicking ANY of these controls in
the Flask BFF gets an unhandled 500 instead of a comprehensible message.

**Why every test below wraps the request in ``try/except
api_client.BadRequest`` instead of inspecting a 500 ``Response``.** This
Flask/Werkzeug version (``werkzeug==3.x``) removed
``Client(raise_server_exceptions=...)`` entirely — the test client cannot be
asked to swallow an unhandled exception into a synthetic 500 ``Response``
any more. With ``app.config.update(TESTING=True)`` (this file's fixture,
matching every sibling ``test_frontend_*.py`` file's convention), Flask's
own ``PROPAGATE_EXCEPTIONS`` is implicitly ``True``, so an exception the
view function does not catch is NOT turned into a response at all — it
propagates straight through ``client.post(...)`` and is raised INTO the
test. Today (before the fix) that is exactly ``api_client.BadRequest``
escaping uncaught; catching it here and failing with a clear message is
the honest way to observe that "the app never handled this" — a bare
``resp = client.post(...)`` would simply crash every test below with an
unlabelled traceback instead of a clean, named RED assertion. After the
fix, ``app.py`` catches ``BadRequest`` itself and returns a normal,
non-5xx response — no exception reaches this test at all, and the
``try`` block's happy path executes.
"""

from __future__ import annotations

import re
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from frontend import api_client
from frontend.app import app


def _forbidden() -> api_client.BadRequest:
    """A fresh instance per call — some CPython versions cache exception
    ``__traceback__`` state on a re-raised singleton in ways that make a
    shared module-level instance confusing to reason about across many
    ``side_effect`` uses."""
    return api_client.BadRequest(
        "backend 403: session role not permitted for this route",
        status_code=403,
        detail={"detail": "session role not permitted for this route"},
    )


@pytest.fixture
def client() -> Any:
    app.config.update(TESTING=True)
    return app.test_client()


def _post_expecting_a_handled_response(client: Any, *args: Any, **kwargs: Any) -> Any:
    """POSTs and asserts the request completed as an ordinary, non-5xx
    ``Response`` — never an uncaught ``api_client.BadRequest`` escaping the
    view function. See the module docstring for why this wrapper exists
    instead of inspecting a synthetic 500 ``Response`` object."""
    try:
        resp = client.post(*args, **kwargs)
    except api_client.BadRequest:
        pytest.fail(
            "api_client.BadRequest (the backend's 403) propagated as an "
            "UNHANDLED exception instead of being caught and rendered as a "
            "comprehensible response — this is the app.py:6xx regression "
            "this file pins."
        )
    assert (
        resp.status_code < 500
    ), f"expected a handled response, got {resp.status_code}"
    body = resp.get_data(as_text=True)
    assert "Internal Server Error" not in body
    return resp


# ── POST /jobs/<id>/status (app.py:625) ──────────────────────────────────


def test_transition_status_403_from_backend_is_handled_not_500(
    monkeypatch: Any, client: Any
) -> None:
    monkeypatch.setattr(
        api_client, "transition_status", MagicMock(side_effect=_forbidden())
    )
    _post_expecting_a_handled_response(
        client, f"/jobs/{uuid4()}/status", data={"to": "open"}
    )


# ── POST /jobs/<id>/blind-review (app.py:641) ────────────────────────────


def test_blind_review_403_from_backend_is_handled_not_500(
    monkeypatch: Any, client: Any
) -> None:
    monkeypatch.setattr(api_client, "patch_job", MagicMock(side_effect=_forbidden()))
    _post_expecting_a_handled_response(
        client, f"/jobs/{uuid4()}/blind-review", data={"blind_review": "true"}
    )


# ── POST /jobs/<id>/shortlist (app.py:715) ───────────────────────────────


def test_generate_shortlist_403_from_backend_is_handled_not_500(
    monkeypatch: Any, client: Any
) -> None:
    monkeypatch.setattr(
        api_client, "generate_shortlist", MagicMock(side_effect=_forbidden())
    )
    _post_expecting_a_handled_response(client, f"/jobs/{uuid4()}/shortlist")


# ── POST /resumes/<id>/withdraw (app.py:899) ─────────────────────────────


def _resume_payload(
    resume_id: Any, *, withdrawn_at: str | None = None
) -> dict[str, Any]:
    return {
        "id": str(resume_id),
        "job_id": str(uuid4()),
        "original_filename": "resume.pdf",
        "mime_type": "application/pdf",
        "file_size_bytes": 1234,
        "sha256": "0" * 64,
        "candidate": {"name": None, "email": None, "phone": None, "location": None},
        "candidate_email_hash": None,
        "parsed": None,
        "status": "parsed",
        "uploaded_by": None,
        "uploaded_at": "2026-07-01T00:00:00Z",
        "parsed_at": "2026-07-01T00:05:00Z",
        "failure_reason": None,
        "consent_acknowledged": True,
        "blinded": True,
        "withdrawn_at": withdrawn_at,
        "withdrawal_reason": None,
    }


def _mint_token(
    monkeypatch: Any, client: Any, resume_id: Any, *, fragment: str, withdrawn: bool
) -> str:
    resume = _resume_payload(
        resume_id, withdrawn_at="2026-07-20T00:00:00Z" if withdrawn else None
    )
    monkeypatch.setattr(api_client, "get_resume", MagicMock(return_value=resume))
    body = client.get(f"/resumes/{resume_id}").get_data(as_text=True)
    match = re.search(
        rf'action="[^"]*{fragment}[^"]*"[^>]*>(.*?)</form>', body, re.DOTALL
    )
    assert match is not None, f"expected a {fragment} <form> on the résumé-detail page"
    token_match = re.search(r'name="csrf_token"\s+value="([^"]+)"', match.group(1))
    assert token_match is not None
    return token_match.group(1)


def test_resume_withdraw_403_from_backend_is_handled_not_500(
    monkeypatch: Any, client: Any
) -> None:
    resume_id = uuid4()
    token = _mint_token(
        monkeypatch, client, resume_id, fragment="/withdraw", withdrawn=False
    )
    monkeypatch.setattr(
        api_client, "withdraw_resume", MagicMock(side_effect=_forbidden())
    )
    _post_expecting_a_handled_response(
        client, f"/resumes/{resume_id}/withdraw", data={"csrf_token": token}
    )


# ── POST /resumes/<id>/reinstate (app.py:924) ────────────────────────────


def test_resume_reinstate_403_from_backend_is_handled_not_500(
    monkeypatch: Any, client: Any
) -> None:
    resume_id = uuid4()
    token = _mint_token(
        monkeypatch, client, resume_id, fragment="/reinstate", withdrawn=True
    )
    monkeypatch.setattr(
        api_client, "reinstate_resume", MagicMock(side_effect=_forbidden())
    )
    _post_expecting_a_handled_response(
        client, f"/resumes/{resume_id}/reinstate", data={"csrf_token": token}
    )


# ── POST /resumes/<id>/match-jobs (app.py:983) ───────────────────────────


def test_resume_match_jobs_403_from_backend_is_handled_not_500(
    monkeypatch: Any, client: Any
) -> None:
    monkeypatch.setattr(
        api_client, "trigger_reverse_match", MagicMock(side_effect=_forbidden())
    )
    _post_expecting_a_handled_response(client, f"/resumes/{uuid4()}/match-jobs")


__all__: list[str] = []
