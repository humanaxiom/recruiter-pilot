"""Unit tests for FU-5 slice 11's browser session gate — a Flask hook that
gates unauthenticated browser access when CAS is enabled (ADR-019 §10/§3/
§10b). This is the LAST FU-5 build slice.

Today ``core/frontend/app.py`` has NO ``before_request`` gate at all — every
route (other than ``/health``) is reachable with no session. Every test below
that expects a 302/503 currently observes a 200 instead: RED for the right
reason (no gate exists yet), not for an unrelated failure.

**Mechanism decisions pinned here (stated for the coder, per the task):**

* The gate is implemented as a ``before_request`` hook (or equivalent) in
  ``frontend.app`` — these tests exercise it purely through
  ``app.test_client()`` HTTP responses, not through calling an internal
  function directly, so a per-route-decorator implementation that produces
  the same observable responses would also satisfy these tests.
* The gate MUST read ``cas_enabled`` via a FRESH ``get_settings()`` call
  inside the hook (not the module-level ``_settings`` captured once at
  import time in ``app.py:42``) — mirroring ``src.api.routes.auth``'s own
  convention of calling ``get_settings()`` per-request. Tests monkeypatch
  ``frontend.app.get_settings`` (the name the module imports at
  ``from src.settings import get_settings``) to flip ``cas_enabled`` per
  test; if the gate instead reads the frozen ``_settings`` global, these
  tests cannot control it and will fail for the wrong reason — this is
  flagged explicitly for the coder.
* Auth status is determined by calling ``api_client.get_cas_user()`` (new in
  this slice, see ``test_frontend_session_forwarding.py`` — wraps
  ``GET /auth/cas/user``, which never 401s per ADR-019 §10's ``auth.py``).
  Tests monkeypatch ``api_client.get_cas_user`` directly, matching this
  codebase's established convention of monkeypatching ``api_client``
  functions in route-level tests (see ``test_frontend_routes.py``).
* No Flask-side ``/login`` convenience route is required by this slice: the
  gate redirects the browser straight to the API's own
  ``{cas_service_base_url}/auth/cas/login`` (already built in slice 6),
  never to a Flask-owned login route. There is no existing frontend
  route-inventory/meta test to update for this (none was found under
  ``core/tests/unit/`` — ``test_gates_cover_frontend.py`` only pins gate
  *scope*, not a route allowlist), so none is touched here.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlsplit
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


def _authenticated_status() -> dict[str, Any]:
    return {"authenticated": True, "username": "alice", "cas_enabled": True}


def _anonymous_status() -> dict[str, Any]:
    return {"authenticated": False, "username": None, "cas_enabled": True}


# ── cas_enabled=True, no session → 302 to the API's CAS login ───────────


def test_protected_route_redirects_to_cas_login_when_no_session(
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
    assert f"{parsed.scheme}://{parsed.netloc}" == "http://ra.example.test"
    assert parsed.path == "/auth/cas/login"
    assert parse_qs(parsed.query).get("next") == ["/"]


def test_redirect_carries_the_originally_requested_path_as_next(
    monkeypatch: Any, client: Any
) -> None:
    settings = Settings(cas_enabled=True, cas_service_base_url="http://ra.example.test")
    monkeypatch.setattr(frontend_app_module, "get_settings", lambda: settings)
    monkeypatch.setattr(
        api_client, "get_cas_user", MagicMock(return_value=_anonymous_status())
    )
    guard = MagicMock(side_effect=AssertionError("route handler must not run"))
    monkeypatch.setattr(api_client, "get_job", guard)
    job_id = uuid4()

    resp = client.get(f"/jobs/{job_id}", follow_redirects=False)

    assert resp.status_code == 302
    parsed = urlsplit(resp.headers["Location"])
    assert parse_qs(parsed.query).get("next") == [f"/jobs/{job_id}"]
    guard.assert_not_called()


# ── /health is exempt ─────────────────────────────────────────────────


def test_health_stays_200_unauthenticated_even_when_cas_enabled(
    monkeypatch: Any, client: Any
) -> None:
    settings = Settings(cas_enabled=True)
    monkeypatch.setattr(frontend_app_module, "get_settings", lambda: settings)
    spy = MagicMock(side_effect=AssertionError("gate must not call get_cas_user"))
    monkeypatch.setattr(api_client, "get_cas_user", spy)

    resp = client.get("/health")

    assert resp.status_code == 200
    spy.assert_not_called()


# ── valid existing session → NOT redirected, route renders ──────────────


def test_protected_route_renders_normally_with_a_valid_session(
    monkeypatch: Any, client: Any
) -> None:
    settings = Settings(cas_enabled=True)
    monkeypatch.setattr(frontend_app_module, "get_settings", lambda: settings)
    monkeypatch.setattr(
        api_client, "get_cas_user", MagicMock(return_value=_authenticated_status())
    )
    monkeypatch.setattr(api_client, "list_jobs", MagicMock(return_value=[]))

    resp = client.get("/", headers={"Cookie": "ra_session=tok-live"})

    assert resp.status_code == 200


def test_existing_session_keeps_working_without_touching_cas_login(
    monkeypatch: Any, client: Any
) -> None:
    """ADR-019 §3 — a request bearing a currently-valid session is NOT
    redirected even though this test deliberately points
    ``cas_service_base_url`` at an address nothing here can reach: if the
    gate incorrectly redirected anyway, the assertion on ``status_code``
    below (not a 302) is what catches it."""
    settings = Settings(
        cas_enabled=True, cas_service_base_url="http://unreachable.invalid"
    )
    monkeypatch.setattr(frontend_app_module, "get_settings", lambda: settings)
    monkeypatch.setattr(
        api_client, "get_cas_user", MagicMock(return_value=_authenticated_status())
    )
    monkeypatch.setattr(api_client, "list_jobs", MagicMock(return_value=[]))

    resp = client.get(
        "/", headers={"Cookie": "ra_session=tok-live"}, follow_redirects=False
    )

    assert resp.status_code == 200
    assert resp.status_code != 302


# ── cas_enabled=False → passthrough (dev mode, §10b) ─────────────────


def test_cas_disabled_is_a_passthrough_with_no_session(
    monkeypatch: Any, client: Any
) -> None:
    settings = Settings(cas_enabled=False)
    monkeypatch.setattr(frontend_app_module, "get_settings", lambda: settings)
    spy = MagicMock(
        side_effect=AssertionError("gate must not call get_cas_user when cas disabled")
    )
    monkeypatch.setattr(api_client, "get_cas_user", spy)
    monkeypatch.setattr(api_client, "list_jobs", MagicMock(return_value=[]))

    resp = client.get("/", follow_redirects=False)

    assert resp.status_code == 200
    spy.assert_not_called()


# ── fail-closed: auth-status check unreachable → 503, never a silent 200 ─


def test_auth_status_check_backend_unavailable_returns_503_not_200(
    monkeypatch: Any, client: Any
) -> None:
    settings = Settings(cas_enabled=True)
    monkeypatch.setattr(frontend_app_module, "get_settings", lambda: settings)
    monkeypatch.setattr(
        api_client,
        "get_cas_user",
        MagicMock(side_effect=api_client.BackendUnavailable("backend down")),
    )

    resp = client.get("/")

    assert resp.status_code == 503
    assert resp.status_code != 200


def test_auth_status_check_backend_unavailable_never_reaches_the_route_handler(
    monkeypatch: Any, client: Any
) -> None:
    settings = Settings(cas_enabled=True)
    monkeypatch.setattr(frontend_app_module, "get_settings", lambda: settings)
    monkeypatch.setattr(
        api_client,
        "get_cas_user",
        MagicMock(side_effect=api_client.BackendUnavailable("backend down")),
    )
    guard = MagicMock(side_effect=AssertionError("route handler must not run"))
    monkeypatch.setattr(api_client, "list_jobs", guard)

    client.get("/")

    guard.assert_not_called()
