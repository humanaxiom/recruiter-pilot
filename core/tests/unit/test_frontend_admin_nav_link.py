"""Failing (RED) tests — user-admin-roles slice 7: the "Admin"/"Users" nav
link in the header (``core/frontend/templates/base.html``), visible only to
an admin session, linking to the new ``/admin/users`` page.

Today (before this slice): ``base.html``'s nav has no admin-users link at all
— ``href="/admin/users"`` never appears in any rendered page regardless of
role. The presence test below
(``test_admin_nav_link_appears_for_an_admin_session``) is the genuine RED
assertion here (it currently fails, since the link doesn't exist yet).

The two absence tests
(``test_admin_nav_link_absent_for_a_non_admin_session`` and
``test_admin_nav_link_absent_in_dev_mode_with_no_current_user``) pin
non-regression: they happen to ALSO pass today (there is no link to find for
ANY role yet), but they must keep passing once the admin-only link exists, so
they are kept as forward-looking pins rather than dropped — see this
project's evidence report for the explicit call-out that they are not
independently RED right now.

**Design decision pinned for the coder** (task's D bullet: "acceptable either
way, assert actual"): the dev-mode/anonymous case (``cas_enabled=False``) has
``current_user`` injected as ``None`` (see
``test_frontend_auth_widget.py::test_cas_disabled_context_processor_current_user_is_none``)
— there is no role to check in that branch, so a
``current_user and current_user.role == "admin"`` guard around the nav link
naturally renders it ABSENT in dev mode. This file pins ABSENT as the chosen,
asserted behaviour for that case.

Per CLAUDE.md: pure Flask/Jinja rendering over a mocked ``api_client`` — no
real Postgres/Neo4j/Redis I/O, so the offline suite is sufficient.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from frontend import api_client
from frontend import app as frontend_app_module
from frontend.app import app
from src.settings import Settings


@pytest.fixture
def client() -> Any:
    app.config.update(TESTING=True)
    return app.test_client()


def _authenticated(role: str | None) -> dict[str, Any]:
    return {
        "authenticated": True,
        "username": "alice",
        "cas_enabled": True,
        "role": role,
    }


def _mock_both_job_listing_functions(monkeypatch: Any) -> None:
    """``index()`` calls ``my_jobs()`` for a hiring_manager session and
    ``list_jobs()`` for every other role (FU-6/ADR-020 §7) — mock BOTH so
    this file's nav-link assertions never depend on which branch a given
    role takes."""
    monkeypatch.setattr(api_client, "list_jobs", MagicMock(return_value=[]))
    monkeypatch.setattr(api_client, "my_jobs", MagicMock(return_value=[]))


def test_admin_nav_link_appears_for_an_admin_session(
    monkeypatch: Any, client: Any
) -> None:
    settings = Settings(cas_enabled=True)
    monkeypatch.setattr(frontend_app_module, "get_settings", lambda: settings)
    monkeypatch.setattr(
        api_client, "get_cas_user", MagicMock(return_value=_authenticated("admin"))
    )
    _mock_both_job_listing_functions(monkeypatch)

    resp = client.get("/", headers={"Cookie": "ra_session=tok-live"})
    body = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert 'href="/admin/users"' in body


@pytest.mark.parametrize("role", ["recruiter", "hiring_manager", "auditor"])
def test_admin_nav_link_absent_for_a_non_admin_session(
    monkeypatch: Any, client: Any, role: str
) -> None:
    settings = Settings(cas_enabled=True)
    monkeypatch.setattr(frontend_app_module, "get_settings", lambda: settings)
    monkeypatch.setattr(
        api_client, "get_cas_user", MagicMock(return_value=_authenticated(role))
    )
    _mock_both_job_listing_functions(monkeypatch)

    resp = client.get("/", headers={"Cookie": "ra_session=tok-live"})
    body = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert 'href="/admin/users"' not in body


def test_admin_nav_link_absent_in_dev_mode_with_no_current_user(
    monkeypatch: Any, client: Any
) -> None:
    settings = Settings(cas_enabled=False)
    monkeypatch.setattr(frontend_app_module, "get_settings", lambda: settings)
    _mock_both_job_listing_functions(monkeypatch)

    resp = client.get("/")
    body = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert 'href="/admin/users"' not in body
