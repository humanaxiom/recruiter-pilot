"""Route-level tests for the Phase 7 read-only Flask viewer —
``frontend.app`` (routes do not exist yet; ``frontend.api_client`` does not
exist at all). The whole file fails at collection (``ModuleNotFoundError`` on
``from frontend import api_client``) — RED half of the TDD cycle, mirroring
the established pattern in ``test_route_shortlist.py``/``test_route_resumes.py``
for a not-yet-existent module.

**The contract this file locks:**

* ``GET /`` — jobs list (renders ``api_client.list_jobs()``'s result).
* ``GET /jobs/<job_id>`` — job detail + its résumés (``api_client.get_job`` +
  ``api_client.list_resumes``).
* ``GET /jobs/<job_id>/shortlist`` — the BLIND shortlist list. The view must
  call ``api_client.list_shortlist(job_id)`` with **no ``reveal`` kwarg at
  all** — shortlist reads are unconditionally blind (mirrors
  ``shortlist_service.list_for_job`` taking no ``reveal`` param either).
* ``GET /shortlist/<entry_id>`` — one shortlist entry.
* ``GET /resumes/<resume_id>`` — résumé detail. **Redaction-boundary
  load-bearing test**: a browser-supplied ``?reveal=true`` query string must
  NEVER reach ``api_client.get_resume`` as ``reveal=True`` — the viewer must
  not be able to re-introduce de-anonymization from the URL (ADR-011/012's
  redact-before-DTO boundary would otherwise be trivially bypassable by any
  visitor typing ``?reveal=true`` into their own address bar).
* ``GET /resumes/<resume_id>/match-results`` — reverse-match passthrough, no
  added masking (ADR-012 §4 — the backend itself applies none here either).
* ``GET /jobs/<job_id>/shortlist/export`` — server-side export proxy: streams
  ``api_client.export_shortlist(...)``'s response body straight through and
  preserves ``Content-Disposition``, WITHOUT ever exposing the backend
  ``X-API-Key`` to the browser.
* Error mapping: ``api_client.NotFound`` -> Flask 404 (not an unhandled 500);
  ``api_client.BackendUnavailable`` -> a 502/503 page (not an unhandled 500
  traceback either).
* Secret-leak guard: neither the Flask ``secret_key`` nor the backend
  ``X-API-Key`` value ever appears in a rendered response body or in any
  response header sent back to the browser.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import httpx
import pytest

from frontend import api_client
from frontend.app import app

_REAL_NAME = "Zzyzxqrst Wibblesworth"
_REAL_EMAIL = "zzyzxqrst.wibblesworth@example.test"
_REAL_PHONE = "604-555-0192"


@pytest.fixture
def client() -> Any:
    app.config.update(TESTING=True)
    return app.test_client()


def _job(job_id: Any) -> dict[str, Any]:
    return {
        "id": str(job_id),
        "title": "Senior Backend Engineer",
        "department": "Engineering",
        "status": "open",
        "blind_review": True,
    }


def _resume_blind(resume_id: Any) -> dict[str, Any]:
    return {
        "id": str(resume_id),
        "job_id": str(uuid4()),
        "original_filename": "resume.pdf",
        "candidate": {"name": None, "email": None, "phone": None, "location": None},
        "status": "parsed",
        "blinded": True,
    }


def _shortlist_entry(entry_id: Any) -> dict[str, Any]:
    return {
        "id": str(entry_id),
        "job_id": str(uuid4()),
        "resume_id": str(uuid4()),
        "rank": 1,
        "score_final": 0.87,
        "blinded": True,
        "display_label": "Candidate A",
    }


# ── GET / ──────────────────────────────────────────────────────────────


def test_index_renders_the_jobs_list(monkeypatch: Any, client: Any) -> None:
    monkeypatch.setattr(
        api_client, "list_jobs", MagicMock(return_value=[_job(uuid4())])
    )
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"Senior Backend Engineer" in resp.data


def test_index_handles_backend_unavailable(monkeypatch: Any, client: Any) -> None:
    monkeypatch.setattr(
        api_client,
        "list_jobs",
        MagicMock(side_effect=api_client.BackendUnavailable("backend down")),
    )
    resp = client.get("/")
    assert resp.status_code in (502, 503)
    assert resp.status_code != 500


# ── GET /jobs/<job_id> ────────────────────────────────────────────────


def test_job_detail_renders_job_and_its_resumes(monkeypatch: Any, client: Any) -> None:
    job_id = uuid4()
    monkeypatch.setattr(api_client, "get_job", MagicMock(return_value=_job(job_id)))
    monkeypatch.setattr(
        api_client,
        "list_resumes",
        MagicMock(return_value=[{"id": str(uuid4()), "original_filename": "r.pdf"}]),
    )
    resp = client.get(f"/jobs/{job_id}")
    assert resp.status_code == 200
    assert b"Senior Backend Engineer" in resp.data


def test_job_detail_404s_when_the_job_does_not_exist(
    monkeypatch: Any, client: Any
) -> None:
    monkeypatch.setattr(
        api_client, "get_job", MagicMock(side_effect=api_client.NotFound("no job"))
    )
    resp = client.get(f"/jobs/{uuid4()}")
    assert resp.status_code == 404
    assert resp.status_code != 500


def test_job_detail_backend_unreachable_is_not_a_500(
    monkeypatch: Any, client: Any
) -> None:
    monkeypatch.setattr(
        api_client,
        "get_job",
        MagicMock(side_effect=api_client.BackendUnavailable("connect error")),
    )
    resp = client.get(f"/jobs/{uuid4()}")
    assert resp.status_code in (502, 503)
    assert resp.status_code != 500


# ── GET /jobs/<job_id>/shortlist — blind list ────────────────────────


def test_job_shortlist_renders_entries(monkeypatch: Any, client: Any) -> None:
    job_id = uuid4()
    spy = MagicMock(return_value=[_shortlist_entry(uuid4())])
    monkeypatch.setattr(api_client, "list_shortlist", spy)
    # The shortlist route also reads résumés to gate the Generate button.
    monkeypatch.setattr(api_client, "list_resumes", MagicMock(return_value=[]))
    resp = client.get(f"/jobs/{job_id}/shortlist")
    assert resp.status_code == 200
    assert b"Candidate A" in resp.data


def test_job_shortlist_call_carries_no_reveal_kwarg(
    monkeypatch: Any, client: Any
) -> None:
    """Load-bearing: the shortlist list is unconditionally blind — the viewer
    must not be able to pass ``reveal`` through to the backend on this path
    at all, matching ``shortlist_service.list_for_job`` accepting no such
    parameter."""
    job_id = uuid4()
    spy = MagicMock(return_value=[])
    monkeypatch.setattr(api_client, "list_shortlist", spy)
    monkeypatch.setattr(api_client, "list_resumes", MagicMock(return_value=[]))
    client.get(f"/jobs/{job_id}/shortlist")
    spy.assert_called_once()
    assert "reveal" not in spy.call_args.kwargs


# ── GET /shortlist/<entry_id> ────────────────────────────────────────


def test_shortlist_entry_detail_renders(monkeypatch: Any, client: Any) -> None:
    entry_id = uuid4()
    monkeypatch.setattr(
        api_client,
        "get_shortlist_entry",
        MagicMock(return_value=_shortlist_entry(entry_id)),
    )
    resp = client.get(f"/shortlist/{entry_id}")
    assert resp.status_code == 200
    assert b"Candidate A" in resp.data


def test_shortlist_entry_detail_404s_when_missing(
    monkeypatch: Any, client: Any
) -> None:
    monkeypatch.setattr(
        api_client,
        "get_shortlist_entry",
        MagicMock(side_effect=api_client.NotFound("no entry")),
    )
    resp = client.get(f"/shortlist/{uuid4()}")
    assert resp.status_code == 404


# ── GET /resumes/<resume_id> — redaction boundary ────────────────────


def test_resume_detail_renders(monkeypatch: Any, client: Any) -> None:
    resume_id = uuid4()
    monkeypatch.setattr(
        api_client, "get_resume", MagicMock(return_value=_resume_blind(resume_id))
    )
    resp = client.get(f"/resumes/{resume_id}")
    assert resp.status_code == 200


def test_resume_detail_404s_when_missing(monkeypatch: Any, client: Any) -> None:
    monkeypatch.setattr(
        api_client,
        "get_resume",
        MagicMock(side_effect=api_client.NotFound("no resume")),
    )
    resp = client.get(f"/resumes/{uuid4()}")
    assert resp.status_code == 404


def test_resume_detail_strips_a_browser_supplied_reveal_query_param(
    monkeypatch: Any, client: Any
) -> None:
    """CRITICAL redaction-boundary test: a visitor typing ``?reveal=true``
    into their own address bar must NOT be able to make the viewer forward
    ``reveal=True`` to the backend — that would let any third party
    de-anonymize a blind shortlist candidate by editing a URL. The viewer
    must ignore/strip this query param entirely, always calling
    ``api_client.get_resume`` with ``reveal`` false (or omitted)."""
    resume_id = uuid4()
    spy = MagicMock(return_value=_resume_blind(resume_id))
    monkeypatch.setattr(api_client, "get_resume", spy)

    client.get(f"/resumes/{resume_id}?reveal=true")

    spy.assert_called_once()
    _, kwargs = spy.call_args
    assert kwargs.get("reveal", False) is False


def test_resume_detail_blind_render_never_contains_raw_pii_bytes(
    monkeypatch: Any, client: Any
) -> None:
    """Byte-scan guard (same class as ADR-011/012's byte-scan tests, now at
    the viewer layer): even with a browser-supplied ``?reveal=true``, the
    rendered page must not contain the candidate's real name/email/phone
    anywhere in its raw bytes."""
    resume_id = uuid4()
    monkeypatch.setattr(
        api_client, "get_resume", MagicMock(return_value=_resume_blind(resume_id))
    )
    resp = client.get(f"/resumes/{resume_id}?reveal=true")
    raw = resp.get_data(as_text=True)
    assert _REAL_NAME not in raw
    assert _REAL_EMAIL not in raw
    assert _REAL_PHONE not in raw


# ── GET /resumes/<resume_id>/match-results ───────────────────────────


def test_resume_match_results_renders_passthrough(
    monkeypatch: Any, client: Any
) -> None:
    resume_id = uuid4()
    monkeypatch.setattr(
        api_client,
        "get_match_results",
        MagicMock(return_value={"resume_id": str(resume_id), "entries": []}),
    )
    resp = client.get(f"/resumes/{resume_id}/match-results")
    assert resp.status_code == 200


def test_resume_match_results_404s_when_missing(monkeypatch: Any, client: Any) -> None:
    monkeypatch.setattr(
        api_client,
        "get_match_results",
        MagicMock(side_effect=api_client.NotFound("no resume")),
    )
    resp = client.get(f"/resumes/{uuid4()}/match-results")
    assert resp.status_code == 404


# ── GET /jobs/<job_id>/shortlist/export — server-side proxy ──────────


def test_shortlist_export_proxies_body_and_content_disposition(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    content_disposition = f'attachment; filename="shortlist-{job_id}-anon.csv"'
    backend_resp = httpx.Response(
        200,
        content=b"rank,score\n1,0.9\n",
        headers={
            "content-type": "text/csv",
            "content-disposition": content_disposition,
        },
    )
    monkeypatch.setattr(
        api_client, "export_shortlist", MagicMock(return_value=backend_resp)
    )
    resp = client.get(f"/jobs/{job_id}/shortlist/export")
    assert resp.status_code == 200
    assert resp.data == b"rank,score\n1,0.9\n"
    assert (
        resp.headers.get("Content-Disposition")
        == f'attachment; filename="shortlist-{job_id}-anon.csv"'
    )
    assert "text/csv" in resp.headers.get("Content-Type", "")


def test_shortlist_export_never_exposes_the_api_key_to_the_browser(
    monkeypatch: Any, client: Any
) -> None:
    """The export proxy must run entirely server-side: whatever
    ``X-API-Key`` the backend client attaches on the OUTBOUND request to
    FastAPI must never appear in the Flask response returned to the
    browser — neither in the body nor in any response header."""
    job_id = uuid4()
    marker = "super-secret-backend-api-key-marker-999"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("X-API-Key") == marker
        return httpx.Response(
            200,
            content=b"csv-body",
            headers={
                "content-type": "text/csv",
                "content-disposition": f'attachment; filename="shortlist-{job_id}.csv"',
            },
        )

    fake_client = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="http://test",
        headers={"X-API-Key": marker},
    )
    monkeypatch.setattr(api_client, "build_client", lambda: fake_client)

    resp = client.get(f"/jobs/{job_id}/shortlist/export")

    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert marker not in body
    assert "X-API-Key" not in resp.headers
    assert all(marker not in str(v) for v in resp.headers.values())


def test_shortlist_export_backend_unavailable_is_not_a_500(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    monkeypatch.setattr(
        api_client,
        "export_shortlist",
        MagicMock(side_effect=api_client.BackendUnavailable("connect error")),
    )
    resp = client.get(f"/jobs/{job_id}/shortlist/export")
    assert resp.status_code in (502, 503)
    assert resp.status_code != 500


# ── secret-leak guard ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "path_fn",
    [
        lambda: "/",
        lambda: f"/jobs/{uuid4()}",
        lambda: f"/jobs/{uuid4()}/shortlist",
        lambda: f"/shortlist/{uuid4()}",
        lambda: f"/resumes/{uuid4()}",
    ],
)
def test_flask_secret_key_never_leaks_into_a_rendered_page(
    monkeypatch: Any, client: Any, path_fn: Any
) -> None:
    marker = "distinctive-flask-secret-key-marker-abc123"
    monkeypatch.setattr(app, "secret_key", marker)
    monkeypatch.setattr(
        api_client, "list_jobs", MagicMock(return_value=[_job(uuid4())])
    )
    monkeypatch.setattr(api_client, "get_job", MagicMock(return_value=_job(uuid4())))
    monkeypatch.setattr(api_client, "list_resumes", MagicMock(return_value=[]))
    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=[]))
    monkeypatch.setattr(
        api_client,
        "get_shortlist_entry",
        MagicMock(return_value=_shortlist_entry(uuid4())),
    )
    monkeypatch.setattr(
        api_client, "get_resume", MagicMock(return_value=_resume_blind(uuid4()))
    )

    resp = client.get(path_fn())

    assert marker not in resp.get_data(as_text=True)
    assert all(marker not in str(v) for v in resp.headers.values())
