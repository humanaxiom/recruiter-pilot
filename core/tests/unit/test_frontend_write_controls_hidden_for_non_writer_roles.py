"""RED pin — security finding, fix/auth-boundary-fails-open (F4, defence in
depth).

**Not the fix — a compensating control.** The actual fix is the backend
session-role gate (``require_session_role``) plus the Flask 403-handling
pin in ``test_frontend_write_route_403_handling.py``. This file pins a
SEPARATE, additional property: today NOTHING in any template checks
``current_user.role`` before rendering a write control (``base.html`` is
the only template that consults ``current_user.role`` at all, for the
admin nav link — see ``test_frontend_admin_nav_link.py``). A signed-in
hiring_manager or auditor browsing the Flask viewer sees every write
control (status-transition buttons, the blind-review toggle, the résumé-
upload form, the shortlist "Generate" button, the withdraw/reinstate
button) rendered exactly as a recruiter would, and only discovers they
cannot actually use them after clicking — a confusing UX at best, and the
kind of gap that hides a REAL bug behind "well, it 403s anyway" reasoning.
This file pins that the controls are not rendered at all for a
non-writer session, so a future regression in the backend gate would be
caught here too, independently.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from frontend import api_client
from frontend import app as frontend_app_module
from frontend.app import app
from src.settings import Settings


@pytest.fixture
def client() -> Any:
    app.config.update(TESTING=True)
    return app.test_client()


def _authenticated(role: str) -> dict[str, Any]:
    return {
        "authenticated": True,
        "username": "jordan",
        "cas_enabled": True,
        "role": role,
    }


def _enable_cas_session(monkeypatch: Any, role: str) -> None:
    settings = Settings(cas_enabled=True)
    monkeypatch.setattr(frontend_app_module, "get_settings", lambda: settings)
    monkeypatch.setattr(
        api_client, "get_cas_user", MagicMock(return_value=_authenticated(role))
    )


def _job(job_id: Any) -> dict[str, Any]:
    return {
        "id": str(job_id),
        "title": "Senior Backend Engineer",
        "department": "Engineering",
        "location": "Remote",
        "min_years": 5,
        "status": "open",
        "blind_review": True,
        "parsed_at": "2026-07-01T00:00:00Z",
        "description_parsed": {"required_skills": []},
    }


def _resume(
    resume_id: Any, *, withdrawn: bool = False, status: str = "parsed"
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
        "status": status,
        "uploaded_by": None,
        "uploaded_at": "2026-07-01T00:00:00Z",
        "parsed_at": "2026-07-01T00:05:00Z",
        "failure_reason": None,
        "consent_acknowledged": True,
        "blinded": True,
        "withdrawn_at": "2026-07-20T00:00:00Z" if withdrawn else None,
        "withdrawal_reason": None,
    }


# ── job detail: status transitions / blind toggle / upload form ──────────


@pytest.mark.parametrize("role", ["hiring_manager", "auditor"])
def test_job_detail_hides_every_write_control_for_a_non_writer_session(
    monkeypatch: Any, client: Any, role: str
) -> None:
    _enable_cas_session(monkeypatch, role)
    job_id = uuid4()
    monkeypatch.setattr(api_client, "get_job", MagicMock(return_value=_job(job_id)))
    monkeypatch.setattr(api_client, "list_resumes", MagicMock(return_value=[]))

    resp = client.get(f"/jobs/{job_id}", headers={"Cookie": "ra_session=tok-live"})
    body = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert f'action="/jobs/{job_id}/status"' not in body
    assert f'action="/jobs/{job_id}/blind-review"' not in body
    assert f'action="/jobs/{job_id}/resumes"' not in body


def test_job_detail_shows_the_write_controls_for_a_recruiter_session(
    monkeypatch: Any, client: Any
) -> None:
    """Positive control — proves the hiding above is role-specific, not a
    template regression that hides the controls from everyone."""
    _enable_cas_session(monkeypatch, "recruiter")
    job_id = uuid4()
    monkeypatch.setattr(api_client, "get_job", MagicMock(return_value=_job(job_id)))
    monkeypatch.setattr(api_client, "list_resumes", MagicMock(return_value=[]))

    resp = client.get(f"/jobs/{job_id}", headers={"Cookie": "ra_session=tok-live"})
    body = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert f'action="/jobs/{job_id}/blind-review"' in body
    assert f'action="/jobs/{job_id}/resumes"' in body


# ── shortlist page: the "Generate" button ─────────────────────────────────
#
# ``job_shortlist`` gates the button on ``any_resume_parsed`` (a real bool,
# not Jinja-``undefined`` — the template's own ``|default(true)`` filter
# therefore never kicks in for it); every test below seeds ONE ``parsed``
# résumé so the button is otherwise-eligible to render, isolating the
# assertion to the ROLE check alone rather than an unrelated "no résumés
# parsed yet" disablement.


@pytest.mark.parametrize("role", ["hiring_manager", "auditor"])
def test_shortlist_page_hides_the_generate_button_for_a_non_writer_session(
    monkeypatch: Any, client: Any, role: str
) -> None:
    _enable_cas_session(monkeypatch, role)
    job_id = uuid4()
    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=[]))
    monkeypatch.setattr(
        api_client, "list_resumes", MagicMock(return_value=[_resume(uuid4())])
    )

    resp = client.get(
        f"/jobs/{job_id}/shortlist", headers={"Cookie": "ra_session=tok-live"}
    )
    body = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert f'hx-post="/jobs/{job_id}/shortlist"' not in body


def test_shortlist_page_shows_the_generate_button_for_a_recruiter_session(
    monkeypatch: Any, client: Any
) -> None:
    _enable_cas_session(monkeypatch, "recruiter")
    job_id = uuid4()
    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=[]))
    monkeypatch.setattr(
        api_client, "list_resumes", MagicMock(return_value=[_resume(uuid4())])
    )

    resp = client.get(
        f"/jobs/{job_id}/shortlist", headers={"Cookie": "ra_session=tok-live"}
    )
    body = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert f'hx-post="/jobs/{job_id}/shortlist"' in body


# ── résumé detail: the withdraw button ─────────────────────────────────────


@pytest.mark.parametrize("role", ["hiring_manager", "auditor"])
def test_resume_detail_hides_the_withdraw_control_for_a_non_writer_session(
    monkeypatch: Any, client: Any, role: str
) -> None:
    _enable_cas_session(monkeypatch, role)
    resume_id = uuid4()
    monkeypatch.setattr(
        api_client, "get_resume", MagicMock(return_value=_resume(resume_id))
    )

    resp = client.get(
        f"/resumes/{resume_id}", headers={"Cookie": "ra_session=tok-live"}
    )
    body = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert f'action="/resumes/{resume_id}/withdraw"' not in body


def test_resume_detail_shows_the_withdraw_control_for_a_recruiter_session(
    monkeypatch: Any, client: Any
) -> None:
    _enable_cas_session(monkeypatch, "recruiter")
    resume_id = uuid4()
    monkeypatch.setattr(
        api_client, "get_resume", MagicMock(return_value=_resume(resume_id))
    )

    resp = client.get(
        f"/resumes/{resume_id}", headers={"Cookie": "ra_session=tok-live"}
    )
    body = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert f'action="/resumes/{resume_id}/withdraw"' in body


__all__: list[str] = []
