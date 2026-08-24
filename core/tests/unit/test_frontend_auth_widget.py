"""Failing (RED) tests — the header auth widget (branch
``feat/frontend-auth-widget``, part 1 of a two-part CAS-UI fix).

CAS login already works end-to-end (FU-5), but the Flask viewer's header
(``core/frontend/templates/base.html``) was built for the old
dev-anonymous/API-key model: it shows no logged-in user, no role, and no
logout link, even when ``cas_enabled=True`` and a real CAS session is active.

Today (before this slice):

* ``frontend.app._cas_auth_gate`` calls ``api_client.get_cas_user()`` but
  throws the result away — it never stashes it anywhere (no ``flask.g``
  write at all), so nothing downstream can reuse it without a second
  backend round trip.
* ``base.html`` has no username/role/Logout/Login markup at all — every
  test below that expects to find "asalah"/"admin"/a logout or login link
  in the rendered body currently fails because that markup does not exist.
* There is no Jinja context processor injecting ``current_user`` /
  ``logout_url`` / ``login_url`` into the template context, so directly
  probing ``app.update_template_context`` for those keys currently raises
  ``KeyError`` (or finds ``None`` where a fixed dict is expected).

**Mechanism pinned here (per the task):**

* ``_cas_auth_gate`` stashes the ``get_cas_user()`` result on
  ``flask.g.cas_user`` (whenever it actually calls the backend — i.e.
  whenever ``cas_enabled`` is True) so it is reusable without a second
  backend call.
* A Jinja context processor injects, into every template context:
  ``current_user`` (the stashed ``g.cas_user`` dict, or ``None`` when
  ``cas_enabled`` is False / nothing was stashed), ``logout_url`` =
  ``f"{settings.cas_service_base_url}/auth/cas/logout"``, and ``login_url``
  = ``f"{settings.cas_service_base_url}/auth/cas/login?next=/"``.
* ``base.html``'s header renders: authenticated → username + role + a
  Logout link (``href=logout_url``); ``current_user`` present but NOT
  authenticated (CAS-enabled edge) → a Login link (``href=login_url``);
  ``current_user`` is ``None`` (CAS-disabled dev mode) → neither.

Per CLAUDE.md/HARNESS_LESSONS.md: this is pure Flask/Jinja rendering plus
mocked ``api_client`` — no real Postgres/Neo4j/Redis/Ollama I/O is involved
in the correctness being pinned here, so the unit (offline) suite suffices;
no integration test is added for this slice.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import flask
import pytest

from frontend import api_client
from frontend import app as frontend_app_module
from frontend.app import app
from src.settings import Settings


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


def _authenticated_status(
    *, username: str = "asalah", role: str = "admin"
) -> dict[str, Any]:
    return {
        "authenticated": True,
        "username": username,
        "cas_enabled": True,
        "role": role,
    }


def _anonymous_status() -> dict[str, Any]:
    return {
        "authenticated": False,
        "username": None,
        "cas_enabled": True,
        "role": None,
    }


# ── 1. authenticated: username + role + Logout link, jobs content intact ──


def test_authenticated_widget_shows_username_role_and_logout_link(
    monkeypatch: Any, client: Any
) -> None:
    settings = Settings(
        cas_enabled=True, cas_service_base_url="http://ra.example.test:18000"
    )
    monkeypatch.setattr(frontend_app_module, "get_settings", lambda: settings)
    monkeypatch.setattr(
        api_client, "get_cas_user", MagicMock(return_value=_authenticated_status())
    )
    job_id = uuid4()
    monkeypatch.setattr(api_client, "list_jobs", MagicMock(return_value=[_job(job_id)]))

    resp = client.get("/")
    body = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert "asalah" in body
    assert "admin" in body
    assert "http://ra.example.test:18000/auth/cas/logout" in body
    # The list_jobs-style content must still render alongside the widget.
    assert "Senior Backend Engineer" in body


def test_authenticated_widget_does_not_show_a_login_link(
    monkeypatch: Any, client: Any
) -> None:
    settings = Settings(
        cas_enabled=True, cas_service_base_url="http://ra.example.test:18000"
    )
    monkeypatch.setattr(frontend_app_module, "get_settings", lambda: settings)
    monkeypatch.setattr(
        api_client, "get_cas_user", MagicMock(return_value=_authenticated_status())
    )
    monkeypatch.setattr(api_client, "list_jobs", MagicMock(return_value=[]))

    resp = client.get("/")
    body = resp.get_data(as_text=True)

    assert "/auth/cas/login" not in body


# ── 2. unauthenticated (CAS-enabled edge): Login link, no username ────────
#
# The gate itself would 302 an unauthenticated browser away before any
# template is rendered, so — per the task's stated alternative — this
# probes the context processor's injected values directly via
# ``app.update_template_context``, exactly the mechanism Flask itself uses
# to build every real render's context.


def test_context_processor_injects_login_url_for_unauthenticated_status(
    monkeypatch: Any,
) -> None:
    settings = Settings(
        cas_enabled=True, cas_service_base_url="http://ra.example.test:18000"
    )
    monkeypatch.setattr(frontend_app_module, "get_settings", lambda: settings)

    with app.test_request_context("/"):
        flask.g.cas_user = _anonymous_status()
        ctx: dict[str, Any] = {}
        app.update_template_context(ctx)

    assert ctx["current_user"] == _anonymous_status()
    assert ctx["login_url"] == "http://ra.example.test:18000/auth/cas/login?next=/"
    assert "asalah" not in repr(ctx["current_user"])


def test_context_processor_logout_url_is_built_from_settings(
    monkeypatch: Any,
) -> None:
    settings = Settings(
        cas_enabled=True, cas_service_base_url="http://ra.example.test:18000"
    )
    monkeypatch.setattr(frontend_app_module, "get_settings", lambda: settings)

    with app.test_request_context("/"):
        flask.g.cas_user = _authenticated_status()
        ctx: dict[str, Any] = {}
        app.update_template_context(ctx)

    assert ctx["logout_url"] == "http://ra.example.test:18000/auth/cas/logout"


# ── 3. CAS-disabled (dev mode): no widget at all ──────────────────────────


def test_cas_disabled_dev_mode_renders_no_widget(monkeypatch: Any, client: Any) -> None:
    settings = Settings(cas_enabled=False)
    monkeypatch.setattr(frontend_app_module, "get_settings", lambda: settings)
    spy = MagicMock(
        side_effect=AssertionError("gate must not call get_cas_user when cas disabled")
    )
    monkeypatch.setattr(api_client, "get_cas_user", spy)
    monkeypatch.setattr(api_client, "list_jobs", MagicMock(return_value=[]))

    resp = client.get("/")
    body = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert "Logout" not in body
    assert "/auth/cas/logout" not in body
    assert "Login" not in body
    assert "/auth/cas/login" not in body


def test_cas_disabled_context_processor_current_user_is_none(
    monkeypatch: Any,
) -> None:
    settings = Settings(cas_enabled=False)
    monkeypatch.setattr(frontend_app_module, "get_settings", lambda: settings)

    with app.test_request_context("/"):
        # No _cas_auth_gate stash happens in this branch (cas_enabled=False
        # is an unconditional passthrough with no backend call at all), so
        # no ``flask.g.cas_user`` is ever set for this request.
        ctx: dict[str, Any] = {}
        app.update_template_context(ctx)

    assert ctx["current_user"] is None


# ── 4. the gate's stash is reused, not re-fetched, by the widget ──────────


def test_gate_stash_is_reused_not_refetched_on_a_route_that_renders_the_widget(
    monkeypatch: Any, client: Any
) -> None:
    """``job_detail`` (unlike ``index``) never calls ``get_cas_user`` itself
    — the ONLY caller on this route is the ``_cas_auth_gate`` hook. If the
    widget's context processor stashed-value reuse works, the whole request
    calls the backend exactly once."""
    settings = Settings(cas_enabled=True)
    monkeypatch.setattr(frontend_app_module, "get_settings", lambda: settings)
    spy = MagicMock(return_value=_authenticated_status())
    monkeypatch.setattr(api_client, "get_cas_user", spy)
    job_id = uuid4()
    monkeypatch.setattr(api_client, "get_job", MagicMock(return_value=_job(job_id)))
    monkeypatch.setattr(api_client, "list_resumes", MagicMock(return_value=[]))

    resp = client.get(f"/jobs/{job_id}", headers={"Cookie": "ra_session=tok-live"})
    body = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert spy.call_count == 1
    assert "asalah" in body
