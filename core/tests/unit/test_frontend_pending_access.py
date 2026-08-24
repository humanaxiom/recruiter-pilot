"""Failing (RED) tests — user-admin slice 4: the Flask "pending access" page
for an authenticated-but-no-role CAS user.

After slices 2+3, a first-login CAS user is captured with ``role=None`` and
the backend fail-closes every business route with a raw 403 (ADR-019). That
403 is correct for the API but wrong for the *browser*: today
``frontend.app._cas_auth_gate`` (``core/frontend/app.py``, ~line 56-89) only
branches on ``status.get("authenticated")`` — it has no notion of ``role`` at
all — so a no-role user's request proceeds straight through to the requested
business route, which then proxies to the backend and the browser would see
whatever the backend does with a 403 (currently unhandled here: no
``except api_client.Forbidden`` anywhere in ``app.py``). Either way the
visitor never sees a friendly explanation, and today's actual observable
behaviour is simply "the business page renders as if the user had a role"
because the gate does not intercept on role at all yet — that is what most of
the assertions below catch.

**Desired behaviour pinned here (for the coder):**

* In ``_cas_auth_gate``, once ``status.get("authenticated")`` is True, check
  ``status.get("role")``. If it is ``None``, do NOT let the request reach the
  requested route at all — render a new ``pending_access.html`` template and
  return it (HTTP 200, NOT a redirect, NOT a proxied 403).
* ``pending_access.html`` extends ``base.html`` (so it gets the header/auth
  widget for free) and contains a plain-English message telling the user they
  have no role yet and to contact an administrator.
* Because the gate stashes ``g.cas_user = status`` BEFORE the role check (this
  already happens today, per ``app.py`` line 82), the existing context
  processor (``inject_current_user``) picks it up unchanged: the pending page
  shows the username and a working Logout link with zero extra plumbing.
* A role-bearing authenticated user, an unauthenticated user, cas_enabled=False,
  and the ``/health`` exempt path are all UNCHANGED by this slice — asserted
  below as non-regression pins.

Per CLAUDE.md: this is pure Flask gate/template logic over a mocked
``api_client`` backend (no real Postgres/Neo4j/Redis/Ollama I/O is exercised
by the correctness being pinned) — the unit/offline suite suffices; no
integration test is added for this slice.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock
from urllib.parse import urlsplit

import pytest

from frontend import api_client
from frontend import app as frontend_app_module
from frontend.app import app
from src.settings import Settings


@pytest.fixture
def client() -> Any:
    app.config.update(TESTING=True)
    return app.test_client()


def _no_role_status(*, username: str = "newuser") -> dict[str, Any]:
    return {
        "authenticated": True,
        "username": username,
        "cas_enabled": True,
        "role": None,
    }


def _role_status(*, username: str = "alice", role: str = "recruiter") -> dict[str, Any]:
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


# ── 1. no-role authenticated user → pending-access page, business route
#      never runs ──────────────────────────────────────────────────────


def test_no_role_user_sees_pending_access_page_not_the_business_page(
    monkeypatch: Any, client: Any
) -> None:
    settings = Settings(cas_enabled=True)
    monkeypatch.setattr(frontend_app_module, "get_settings", lambda: settings)
    monkeypatch.setattr(
        api_client, "get_cas_user", MagicMock(return_value=_no_role_status())
    )
    guard = MagicMock(side_effect=AssertionError("business route must not run"))
    monkeypatch.setattr(api_client, "list_jobs", guard)

    resp = client.get("/", follow_redirects=False)
    body = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert resp.status_code != 302
    assert resp.status_code != 403
    lowered = body.lower()
    assert "no role" in lowered
    assert "contact an admin" in lowered
    guard.assert_not_called()


def test_no_role_user_on_a_different_business_route_is_also_intercepted(
    monkeypatch: Any, client: Any
) -> None:
    """Pins that the interception happens in the shared gate, not in a
    per-route special case for ``index`` — probed via ``job_detail``, which
    (unlike ``index``) never calls ``get_cas_user`` itself, so the gate is the
    only caller."""
    from uuid import uuid4

    settings = Settings(cas_enabled=True)
    monkeypatch.setattr(frontend_app_module, "get_settings", lambda: settings)
    monkeypatch.setattr(
        api_client, "get_cas_user", MagicMock(return_value=_no_role_status())
    )
    guard = MagicMock(side_effect=AssertionError("business route must not run"))
    monkeypatch.setattr(api_client, "get_job", guard)
    job_id = uuid4()

    resp = client.get(f"/jobs/{job_id}", follow_redirects=False)
    body = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert "no role" in body.lower()
    guard.assert_not_called()


# ── 2. the pending page still carries the header widget (username + Logout) ─


def test_pending_access_page_shows_username_and_logout_link(
    monkeypatch: Any, client: Any
) -> None:
    settings = Settings(
        cas_enabled=True, cas_service_base_url="http://ra.example.test:18000"
    )
    monkeypatch.setattr(frontend_app_module, "get_settings", lambda: settings)
    monkeypatch.setattr(
        api_client,
        "get_cas_user",
        MagicMock(return_value=_no_role_status(username="newuser")),
    )

    resp = client.get("/")
    body = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert "newuser" in body
    assert "http://ra.example.test:18000/auth/cas/logout" in body


# ── 3. authenticated WITH a role → unchanged, business page renders ─────────


def test_user_with_a_role_proceeds_to_the_business_page_unchanged(
    monkeypatch: Any, client: Any
) -> None:
    settings = Settings(cas_enabled=True)
    monkeypatch.setattr(frontend_app_module, "get_settings", lambda: settings)
    monkeypatch.setattr(
        api_client, "get_cas_user", MagicMock(return_value=_role_status())
    )
    list_jobs = MagicMock(return_value=[])
    monkeypatch.setattr(api_client, "list_jobs", list_jobs)

    resp = client.get("/", follow_redirects=False)
    body = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert "no role" not in body.lower()
    list_jobs.assert_called()


# ── 4. still-unauthenticated → still redirected to CAS login (unchanged) ────


def test_unauthenticated_user_is_still_redirected_to_cas_login(
    monkeypatch: Any, client: Any
) -> None:
    settings = Settings(cas_enabled=True, cas_service_base_url="http://ra.example.test")
    monkeypatch.setattr(frontend_app_module, "get_settings", lambda: settings)
    monkeypatch.setattr(
        api_client, "get_cas_user", MagicMock(return_value=_anonymous_status())
    )

    resp = client.get("/", follow_redirects=False)

    assert resp.status_code == 302
    parsed = urlsplit(resp.headers["Location"])
    assert parsed.path == "/auth/cas/login"


# ── 5. cas_enabled=False (dev) → unchanged, no pending page, no gate call ────


def test_cas_disabled_dev_mode_has_no_pending_page(
    monkeypatch: Any, client: Any
) -> None:
    settings = Settings(cas_enabled=False)
    monkeypatch.setattr(frontend_app_module, "get_settings", lambda: settings)
    spy = MagicMock(
        side_effect=AssertionError("gate must not call get_cas_user when cas disabled")
    )
    monkeypatch.setattr(api_client, "get_cas_user", spy)
    list_jobs = MagicMock(return_value=[])
    monkeypatch.setattr(api_client, "list_jobs", list_jobs)

    resp = client.get("/", follow_redirects=False)
    body = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert "no role" not in body.lower()
    spy.assert_not_called()
    list_jobs.assert_called()


# ── 6. /health stays exempt, unaffected by the role check ───────────────────


def test_health_route_is_exempt_from_the_role_check(
    monkeypatch: Any, client: Any
) -> None:
    settings = Settings(cas_enabled=True)
    monkeypatch.setattr(frontend_app_module, "get_settings", lambda: settings)
    spy = MagicMock(side_effect=AssertionError("gate must not call get_cas_user"))
    monkeypatch.setattr(api_client, "get_cas_user", spy)

    resp = client.get("/health")

    assert resp.status_code == 200
    spy.assert_not_called()
