"""Failing (RED) tests — user-admin-roles slice 7: the Flask admin users page,
an admin-only viewer surface listing every user and assigning roles, wired to
the already-built backend admin API (``src/api/routes/users.py``:
``GET /users`` / ``PATCH /users/{user_id}/role``).

Today (before this slice):

* There is no ``GET /admin/users`` or ``POST /admin/users/<uuid:user_id>/role``
  route registered on the Flask app at all — every ``client.get``/``client.post``
  call below currently 404s (Flask's own "no matching route" 404, NOT the
  ``abort(404)`` the coder will later add for a missing user) rather than
  producing any of the behaviours pinned here.
* ``frontend.api_client`` has no ``list_users``/``set_user_role`` (see
  ``test_frontend_api_client_users.py``) — even once a route exists it cannot
  call the backend without those.
* There is no ``admin_users.html`` template.

**Route names pinned for the coder** (so there is one unambiguous target):
``admin_users`` for ``GET /admin/users``; ``admin_set_user_role`` for
``POST /admin/users/<uuid:user_id>/role``.

**Admin-gate contract pinned for the coder** — a small ``_require_admin_page()``
helper, mirroring the backend's own ``_require_admin_session``
(``src/api/routes/users.py``):

* ``cas_enabled=False`` (dev mode): unconditional passthrough, NO call to
  ``api_client.get_cas_user`` at all — dev-anonymous is the backend's own
  synthetic admin sentinel (``src/api/deps.py``'s ``_DEV_ADMIN_SENTINEL_ID``,
  ``role="admin"``), so the page is always reachable in dev mode. This mirrors
  ``index()``'s existing "only call ``get_cas_user`` when ``cas_enabled``"
  discipline (``test_dev_anonymous_default_does_not_call_get_cas_user`` in
  ``test_frontend_my_jobs_view.py``).
* ``cas_enabled=True``: reuse the status ``_cas_auth_gate`` (``app.py``'s
  ``before_request`` hook) already stashed on ``flask.g.cas_user`` for THIS
  request (see ``test_frontend_auth_widget.py``'s
  ``test_gate_stash_is_reused_not_refetched_...`` pin for the established
  reuse-not-refetch convention) — do NOT call ``api_client.get_cas_user()`` a
  second time from inside the route/helper. ``role != "admin"`` -> ``abort(403)``
  before any ``api_client.list_users``/``set_user_role`` call.

**A pinned divergence from the task's literal wording, and why:** the task
asks for role=None to ALSO 403 as "just another non-admin role". That is
unreachable as stated: ``_cas_auth_gate`` (already shipped, ADR-019 §10a
reversal, ``test_frontend_pending_access.py``) intercepts EVERY route — proven
generically via ``job_detail``, not just ``index`` — for an authenticated
role=None session BEFORE any route body ever runs, rendering
``pending_access.html`` at HTTP 200 with "no role"/"contact an admin" copy,
never a 403. Making ``/admin/users`` a 403 for role=None would require
special-casing this ONE route out of that already-tested, general gate
contract, which the task did not ask for and no ADR authorizes. This file pins
the ALREADY-established pending-access behaviour for role=None instead (see
``test_no_role_user_on_admin_users_page_gets_the_pending_access_page_not_403``
below) and reserves 403 for the three role-bearing non-admin roles
(recruiter/hiring_manager/auditor), which are genuinely new coverage for this
route.

Per CLAUDE.md: pure Flask route/client wiring over a MOCKED backend
(``monkeypatch``ed ``api_client`` functions) — no real Postgres/Neo4j/Redis
I/O, so the offline suite is sufficient; no integration test is added here.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from frontend import api_client
from frontend import app as frontend_app_module
from src.settings import Settings

_NOW = "2026-07-01T00:00:00Z"


@pytest.fixture
def client(csrf_client: Any) -> Any:
    """The shared CSRF-carrying browser client (Phase 1.3).

    These tests exercise the route's BUSINESS logic and predate the
    anti-forgery guard; they now present a page token the way a real browser
    does, rather than the guard being relaxed for them. See
    ``tests/unit/conftest.py`` for why this is not autouse."""
    return csrf_client


def _user(
    *,
    user_id: Any | None = None,
    cas_username: str = "bob",
    role: str | None = "recruiter",
) -> dict[str, Any]:
    return {
        "id": str(user_id or uuid4()),
        "cas_username": cas_username,
        "display_name": cas_username.title(),
        "email": f"{cas_username}@example.org",
        "role": role,
        "active": True,
        "created_at": _NOW,
        "last_seen_at": _NOW,
    }


def _authenticated(role: str | None) -> dict[str, Any]:
    return {
        "authenticated": True,
        "username": "alice",
        "cas_enabled": True,
        "role": role,
    }


def _cas_enabled() -> Settings:
    return Settings(cas_enabled=True)


def _cas_disabled() -> Settings:
    return Settings(cas_enabled=False)


# ── GET /admin/users — dev mode (cas_enabled=False) ──────────────────────


def test_dev_mode_admin_users_page_renders_the_user_list(
    monkeypatch: Any, client: Any
) -> None:
    monkeypatch.setattr(frontend_app_module, "get_settings", lambda: _cas_disabled())
    guard = MagicMock(
        side_effect=AssertionError("get_cas_user must not be called in dev mode")
    )
    monkeypatch.setattr(api_client, "get_cas_user", guard)
    monkeypatch.setattr(
        api_client,
        "list_users",
        MagicMock(return_value=[_user(cas_username="asalah", role="admin")]),
    )

    resp = client.get("/admin/users")

    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "asalah" in body
    assert "admin" in body
    guard.assert_not_called()


def test_dev_mode_admin_users_backend_unavailable_returns_503(
    monkeypatch: Any, client: Any
) -> None:
    monkeypatch.setattr(frontend_app_module, "get_settings", lambda: _cas_disabled())
    monkeypatch.setattr(
        api_client,
        "list_users",
        MagicMock(side_effect=api_client.BackendUnavailable("backend down")),
    )

    resp = client.get("/admin/users")

    assert resp.status_code == 503
    assert resp.status_code != 500


# ── GET /admin/users — cas-enabled + admin session ───────────────────────


def test_admin_session_sees_the_user_list_with_role_selection_controls(
    monkeypatch: Any, client: Any
) -> None:
    monkeypatch.setattr(frontend_app_module, "get_settings", lambda: _cas_enabled())
    monkeypatch.setattr(
        api_client, "get_cas_user", MagicMock(return_value=_authenticated("admin"))
    )
    target_id = uuid4()
    target_user = _user(user_id=target_id, cas_username="carol", role="auditor")
    monkeypatch.setattr(api_client, "list_users", MagicMock(return_value=[target_user]))

    resp = client.get("/admin/users", headers={"Cookie": "ra_session=tok-live"})

    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "carol" in body
    # A role-selection control (select/form) per user, offering all four roles.
    for role in ("admin", "recruiter", "hiring_manager", "auditor"):
        assert role in body
    assert ("<select" in body) or ("<form" in body)
    assert str(target_id) in body


# ── GET /admin/users — cas-enabled, non-admin roles -> 403 ───────────────


@pytest.mark.parametrize("role", ["recruiter", "hiring_manager", "auditor"])
def test_non_admin_session_gets_403_and_never_calls_list_users(
    monkeypatch: Any, client: Any, role: str
) -> None:
    monkeypatch.setattr(frontend_app_module, "get_settings", lambda: _cas_enabled())
    monkeypatch.setattr(
        api_client, "get_cas_user", MagicMock(return_value=_authenticated(role))
    )
    guard = MagicMock(
        side_effect=AssertionError(f"list_users must not be called for role={role}")
    )
    monkeypatch.setattr(api_client, "list_users", guard)

    resp = client.get("/admin/users", headers={"Cookie": "ra_session=tok-live"})

    assert resp.status_code == 403
    guard.assert_not_called()


def test_no_role_user_on_admin_users_page_gets_the_pending_access_page_not_403(
    monkeypatch: Any, client: Any
) -> None:
    """See the module docstring's "pinned divergence" note: an authenticated
    role=None session is intercepted by the shared ``_cas_auth_gate`` BEFORE
    this route ever runs (already-established, general behaviour —
    ``test_frontend_pending_access.py`` proves it generically via
    ``job_detail``), so it sees the pending-access page (200), not a 403."""
    monkeypatch.setattr(frontend_app_module, "get_settings", lambda: _cas_enabled())
    monkeypatch.setattr(
        api_client, "get_cas_user", MagicMock(return_value=_authenticated(None))
    )
    guard = MagicMock(
        side_effect=AssertionError("list_users must not be called for a no-role user")
    )
    monkeypatch.setattr(api_client, "list_users", guard)

    resp = client.get("/admin/users", headers={"Cookie": "ra_session=tok-live"})

    assert resp.status_code == 200
    assert "no role" in resp.get_data(as_text=True).lower()
    guard.assert_not_called()


# ── POST /admin/users/<uuid:user_id>/role — dev mode / admin session ─────


def test_dev_mode_role_change_calls_set_user_role_and_redirects_to_admin_users(
    monkeypatch: Any, client: Any
) -> None:
    monkeypatch.setattr(frontend_app_module, "get_settings", lambda: _cas_disabled())
    user_id = uuid4()
    mock = MagicMock(return_value=_user(user_id=user_id, role="recruiter"))
    monkeypatch.setattr(api_client, "set_user_role", mock)

    resp = client.post(
        f"/admin/users/{user_id}/role",
        data={"role": "recruiter"},
        follow_redirects=False,
    )

    assert resp.status_code == 302
    assert resp.headers["Location"].rstrip("/").endswith("/admin/users")
    mock.assert_called_once_with(user_id, "recruiter")


@pytest.mark.parametrize("role", ["recruiter", "hiring_manager", "auditor"])
def test_non_admin_session_cannot_post_a_role_change(
    monkeypatch: Any, client: Any, role: str
) -> None:
    monkeypatch.setattr(frontend_app_module, "get_settings", lambda: _cas_enabled())
    monkeypatch.setattr(
        api_client, "get_cas_user", MagicMock(return_value=_authenticated(role))
    )
    guard = MagicMock(
        side_effect=AssertionError(f"set_user_role must not be called for role={role}")
    )
    monkeypatch.setattr(api_client, "set_user_role", guard)
    user_id = uuid4()

    resp = client.post(
        f"/admin/users/{user_id}/role",
        data={"role": "admin"},
        headers={"Cookie": "ra_session=tok-live"},
    )

    assert resp.status_code == 403
    guard.assert_not_called()


def test_last_admin_lockout_conflict_shows_a_human_readable_message(
    monkeypatch: Any, client: Any
) -> None:
    monkeypatch.setattr(frontend_app_module, "get_settings", lambda: _cas_disabled())
    user_id = uuid4()
    monkeypatch.setattr(
        api_client,
        "set_user_role",
        MagicMock(
            side_effect=api_client.Conflict(
                "backend 409",
                status_code=409,
                detail="cannot demote the last active admin",
            )
        ),
    )
    monkeypatch.setattr(
        api_client,
        "list_users",
        MagicMock(return_value=[_user(user_id=user_id, role="admin")]),
    )

    resp = client.post(f"/admin/users/{user_id}/role", data={"role": "recruiter"})

    assert resp.status_code == 409
    body = resp.get_data(as_text=True).lower()
    assert "last" in body and "admin" in body
    assert "traceback" not in body


def test_role_change_not_found_aborts_404(monkeypatch: Any, client: Any) -> None:
    monkeypatch.setattr(frontend_app_module, "get_settings", lambda: _cas_disabled())
    monkeypatch.setattr(
        api_client,
        "set_user_role",
        MagicMock(side_effect=api_client.NotFound("no such user")),
    )
    user_id = uuid4()

    resp = client.post(f"/admin/users/{user_id}/role", data={"role": "admin"})

    assert resp.status_code == 404


def test_role_change_backend_unavailable_returns_503(
    monkeypatch: Any, client: Any
) -> None:
    monkeypatch.setattr(frontend_app_module, "get_settings", lambda: _cas_disabled())
    monkeypatch.setattr(
        api_client,
        "set_user_role",
        MagicMock(side_effect=api_client.BackendUnavailable("backend down")),
    )
    user_id = uuid4()

    resp = client.post(f"/admin/users/{user_id}/role", data={"role": "admin"})

    assert resp.status_code == 503
    assert resp.status_code != 500
