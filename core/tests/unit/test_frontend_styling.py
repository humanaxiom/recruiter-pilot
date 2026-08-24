"""Slice S8 — presentation-only styling/polish pass across all templates.

Spot-checks that each template carries the expected ``app.css`` utility classes
(nav header, cards, tables, buttons, status pills, ``.htmx-indicator`` spinners,
responsive ``.container``). This is a PRESENTATION-ONLY pass: it must not change
any data-rendering logic or route behaviour, so the blind-sensitive templates
keep their exact data branches — the blind byte-scan guards are re-run here as a
regression gate and must still pass.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from frontend import api_client
from frontend.app import app

_REAL_NAME = "Zzyzxqrst Wibblesworth"
_REAL_EMAIL = "zzyzxqrst.wibblesworth@example.test"
_REAL_PHONE = "604-555-0192"

_CUR = _dt.date.today().year


@pytest.fixture
def client() -> Any:
    app.config.update(TESTING=True)
    return app.test_client()


def _job(job_id: Any, *, status: str = "open") -> dict[str, Any]:
    return {
        "id": str(job_id),
        "title": "Senior Backend Engineer",
        "department": "Engineering",
        "location": "Remote",
        "min_years": 5,
        "status": status,
        "blind_review": True,
        "parsed_at": "2026-07-01T00:00:00Z",
        "description_parsed": {"required_skills": [{"name": "Python"}]},
    }


def _full_entry(entry_id: Any) -> dict[str, Any]:
    return {
        "id": str(entry_id),
        "job_id": str(uuid4()),
        "resume_id": str(uuid4()),
        "rank": 1,
        "score_final": 0.87,
        "score_breakdown": {"skill": 0.9, "experience": 0.8},
        "evidence": {"requirements": [], "overall_summary": ""},
        "blinded": True,
        "display_label": "Candidate A",
    }


def _resume(resume_id: Any, *, with_pii: bool = False) -> dict[str, Any]:
    name = _REAL_NAME if with_pii else None
    email = _REAL_EMAIL if with_pii else None
    phone = _REAL_PHONE if with_pii else None
    candidate = {"name": name, "email": email, "phone": phone, "location": None}
    return {
        "id": str(resume_id),
        "job_id": str(uuid4()),
        "original_filename": "resume.pdf",
        "mime_type": "application/pdf",
        "file_size_bytes": 10,
        "sha256": "0" * 64,
        "candidate": candidate,
        "candidate_email_hash": None,
        "parsed": {
            "candidate": candidate,
            "summary": "Engineer.",
            "total_years_experience": 5,
            "skills": [{"name": "Python", "last_used_year": _CUR}],
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


# ── per-template utility-class spot checks ───────────────────────────────


def test_base_layout_has_nav_and_container(monkeypatch: Any, client: Any) -> None:
    monkeypatch.setattr(api_client, "list_jobs", MagicMock(return_value=[]))
    body = client.get("/").get_data(as_text=True)
    assert 'class="nav"' in body
    assert 'class="container' in body
    assert "nav-links" in body


def test_index_uses_table_and_pill_and_button(monkeypatch: Any, client: Any) -> None:
    monkeypatch.setattr(
        api_client, "list_jobs", MagicMock(return_value=[_job(uuid4())])
    )
    body = client.get("/").get_data(as_text=True)
    assert 'class="table"' in body
    assert "pill pill-open" in body
    assert "btn btn-primary" in body


def test_job_detail_uses_card_and_status_pill(monkeypatch: Any, client: Any) -> None:
    job_id = uuid4()
    monkeypatch.setattr(api_client, "get_job", MagicMock(return_value=_job(job_id)))
    monkeypatch.setattr(api_client, "list_resumes", MagicMock(return_value=[]))
    body = client.get(f"/jobs/{job_id}").get_data(as_text=True)
    assert 'class="card"' in body
    assert "pill pill-open" in body
    assert "btn" in body


def test_resumes_table_has_indicator_while_pending(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    monkeypatch.setattr(
        api_client,
        "list_resumes",
        MagicMock(
            return_value=[
                {
                    "id": str(uuid4()),
                    "original_filename": "r.pdf",
                    "status": "parsing",
                    "candidate_name": None,
                    "uploaded_at": "2026-07-01T00:00:00Z",
                }
            ]
        ),
    )
    body = client.get(f"/jobs/{job_id}/resumes-table").get_data(as_text=True)
    assert 'class="table"' in body
    assert "pill pill-parsing" in body
    assert "htmx-indicator" in body


def test_shortlist_list_has_buttons(monkeypatch: Any, client: Any) -> None:
    job_id = uuid4()
    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=[]))
    monkeypatch.setattr(
        api_client, "list_resumes", MagicMock(return_value=[{"status": "parsed"}])
    )
    body = client.get(f"/jobs/{job_id}/shortlist").get_data(as_text=True)
    assert "btn" in body
    assert "htmx-indicator" in body


def test_shortlist_cards_has_indicator_while_generating(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=[]))
    body = client.get(f"/jobs/{job_id}/shortlist-cards").get_data(as_text=True)
    assert "htmx-indicator" in body


def test_shortlist_cards_uses_candidate_card(monkeypatch: Any, client: Any) -> None:
    job_id = uuid4()
    monkeypatch.setattr(
        api_client, "list_shortlist", MagicMock(return_value=[_full_entry(uuid4())])
    )
    body = client.get(f"/jobs/{job_id}/shortlist-cards").get_data(as_text=True)
    assert "card candidate-card" in body


def test_shortlist_entry_uses_card(monkeypatch: Any, client: Any) -> None:
    entry_id = uuid4()
    monkeypatch.setattr(
        api_client,
        "get_shortlist_entry",
        MagicMock(return_value=_full_entry(entry_id)),
    )
    body = client.get(f"/shortlist/{entry_id}").get_data(as_text=True)
    assert 'class="card"' in body


def test_resume_detail_uses_card(monkeypatch: Any, client: Any) -> None:
    resume_id = uuid4()
    monkeypatch.setattr(
        api_client, "get_resume", MagicMock(return_value=_resume(resume_id))
    )
    body = client.get(f"/resumes/{resume_id}").get_data(as_text=True)
    assert 'class="card"' in body


def test_match_results_uses_table(monkeypatch: Any, client: Any) -> None:
    resume_id = uuid4()
    monkeypatch.setattr(
        api_client,
        "get_match_results",
        MagicMock(return_value={"resume_id": str(resume_id), "entries": []}),
    )
    body = client.get(f"/resumes/{resume_id}/match-results").get_data(as_text=True)
    assert 'class="table"' in body


def test_error_page_uses_card(monkeypatch: Any, client: Any) -> None:
    monkeypatch.setattr(
        api_client,
        "list_jobs",
        MagicMock(side_effect=api_client.BackendUnavailable("down")),
    )
    body = client.get("/").get_data(as_text=True)
    assert 'class="card"' in body


# ── blind byte-scan regression gate (must still pass unchanged) ──────────


def test_regression_resume_detail_no_pii_bytes(monkeypatch: Any, client: Any) -> None:
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


def test_regression_shortlist_cards_no_pii_bytes(monkeypatch: Any, client: Any) -> None:
    job_id = uuid4()
    entry = _full_entry(uuid4())
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
