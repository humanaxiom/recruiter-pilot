"""Failing (RED) tests for the FU-6/ADR-020 §7 viewer default-view switch: a
hiring_manager sees THEIR assigned jobs (``GET /my/jobs``) by default on the
index/job-list page; every other role (and the CAS-disabled dev-anonymous
default) keeps today's global ``GET /jobs`` list, unchanged.

Today (before this slice):

* ``frontend.api_client`` has NO ``my_jobs()`` function at all — every test
  below that references ``api_client.my_jobs`` fails, either at collection
  time (``monkeypatch.setattr(api_client, "my_jobs", ...)`` raises
  ``AttributeError`` because the attribute does not exist — ``monkeypatch``
  defaults to ``raising=True``) or, for the direct client-wiring tests, with
  ``AttributeError: module 'frontend.api_client' has no attribute 'my_jobs'``.
* ``frontend.app.index()`` unconditionally calls ``api_client.list_jobs()``
  and never looks at the caller's role at all — so it cannot possibly branch
  to a scoped view.

**Mechanism decisions pinned here (stated for the coder):**

* ``frontend.api_client`` gets a new ``my_jobs()`` function — ``GET
  /my/jobs`` — mirroring the existing ``list_jobs()`` wiring exactly (same
  ``limit``/``offset``/``status`` kwargs, same keyword-only ``client:``
  injection point for tests, same ``_request``/error-mapping plumbing).
* The ``index()`` route's role check calls ``api_client.get_cas_user()``
  **only when a FRESH ``get_settings().cas_enabled`` is True** — the same
  per-request-settings-read convention ``frontend.app._cas_auth_gate`` (right
  above ``index()`` in ``app.py``) already uses — NOT unconditionally on
  every load. This is a deliberate choice, not the simplest reading of "call
  it on every index load": a large share of the existing frontend test suite
  (``test_frontend_assets.py``, ``test_frontend_styling.py``,
  ``test_frontend_create_job.py``, ``test_frontend_bulk_jobs.py``,
  ``test_frontend_shortlist_top_percent.py``, and several cases inside
  ``test_frontend_routes.py`` itself) hits ``GET /`` with NO mock on
  ``api_client.get_cas_user`` at all, relying on it never being called
  (``Settings.cas_enabled`` defaults to ``False``). An unconditional call
  would turn every one of those into a real, unmocked outbound HTTP attempt
  and fail them for a reason that has nothing to do with what they test.
  Gating on ``cas_enabled`` keeps that entire suite green with NO edits,
  while still fully covering the CAS-enabled path this feature targets.
  ``test_dev_anonymous_default_does_not_call_get_cas_user`` below pins this
  contract directly, mirroring the existing
  ``test_frontend_auth_gate.py::test_cas_disabled_is_a_passthrough_with_no_session``
  "``spy.assert_not_called()``" idiom.
* The backend's ``GET /auth/cas/user`` (``core/src/api/routes/auth.py::cas_user``)
  already returns ``role="admin"`` for the CAS-disabled synthetic
  dev-anonymous session and ``role=None`` for an unauthenticated CAS-enabled
  one — both non-``"hiring_manager"`` values, so both naturally fall into the
  ``list_jobs()`` branch from a plain ``role == "hiring_manager"`` check; no
  extra special-casing is required in the route.
* No caching beyond the single request is pinned here: when CAS is enabled,
  ``get_cas_user()`` may end up called twice per request (once by
  ``_cas_auth_gate``, once by ``index()`` itself) — that duplication is
  accepted for simplicity per the task's "keep it minimal" instruction.
  These tests assert observable behaviour only (which client function gets
  called, what gets rendered), never an exact ``get_cas_user`` call count.

Per CLAUDE.md's integration-vs-offline rule: this is pure Flask route/client
wiring over a MOCKED backend (``httpx.MockTransport`` for the client-wiring
tests, monkeypatched ``api_client`` functions for the route tests) — no real
Postgres/Neo4j/Redis/Ollama is involved anywhere in this slice, so the
offline suite is sufficient and there is no integration test in this file.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import httpx
import pytest

from frontend import api_client
from frontend import app as frontend_app_module
from frontend.app import app
from src.settings import Settings


@pytest.fixture
def client() -> Any:
    app.config.update(TESTING=True)
    return app.test_client()


def _job(job_id: Any, title: str = "Data Scientist") -> dict[str, Any]:
    return {
        "id": str(job_id),
        "title": title,
        "department": "R&D",
        "status": "open",
        "blind_review": True,
    }


def _authenticated(role: str | None) -> dict[str, Any]:
    return {
        "authenticated": True,
        "username": "alice",
        "cas_enabled": True,
        "role": role,
    }


def _hiring_manager_status() -> dict[str, Any]:
    return _authenticated("hiring_manager")


def _cas_enabled_settings() -> Settings:
    return Settings(cas_enabled=True)


# ── api_client.my_jobs — GET /my/jobs wiring ─────────────────────────────


def _client_with(handler: Any) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test")


def test_my_jobs_hits_the_expected_path_and_returns_parsed_json() -> None:
    captured: dict[str, httpx.URL] = {}
    job_id = uuid4()

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = request.url
        return httpx.Response(
            200,
            json=[
                {
                    "id": str(job_id),
                    "title": "Data Scientist",
                    "department": "R&D",
                    "status": "open",
                    "created_at": "2026-01-01T00:00:00Z",
                    "parsed_at": None,
                }
            ],
        )

    result = api_client.my_jobs(client=_client_with(handler))

    assert captured["url"].path == "/my/jobs"
    assert result[0]["title"] == "Data Scientist"


def test_my_jobs_forwards_status_filter_as_query_param() -> None:
    captured: dict[str, httpx.URL] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = request.url
        return httpx.Response(200, json=[])

    api_client.my_jobs(status="open", client=_client_with(handler))

    assert captured["url"].params.get("status") == "open"


def test_my_jobs_raises_backend_unavailable_on_5xx() -> None:
    client = _client_with(lambda request: httpx.Response(500, json={"detail": "boom"}))
    with pytest.raises(api_client.BackendUnavailable):
        api_client.my_jobs(client=client)


def test_my_jobs_raises_backend_unavailable_on_connect_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = _client_with(handler)
    with pytest.raises(api_client.BackendUnavailable):
        api_client.my_jobs(client=client)


def test_my_jobs_raises_not_found_on_404() -> None:
    client = _client_with(lambda request: httpx.Response(404, json={"detail": "nope"}))
    with pytest.raises(api_client.NotFound):
        api_client.my_jobs(client=client)


# ── index() route: hiring_manager -> my_jobs, everyone else -> list_jobs ──


def test_hiring_manager_sees_my_jobs_not_the_global_list(
    monkeypatch: Any, client: Any
) -> None:
    monkeypatch.setattr(
        frontend_app_module, "get_settings", lambda: _cas_enabled_settings()
    )
    monkeypatch.setattr(
        api_client, "get_cas_user", MagicMock(return_value=_hiring_manager_status())
    )
    job_id = uuid4()
    my_jobs_mock = MagicMock(
        return_value=[_job(job_id, title="Assigned Data Scientist Role")]
    )
    list_jobs_guard = MagicMock(
        side_effect=AssertionError("list_jobs must not be called for a hiring_manager")
    )
    monkeypatch.setattr(api_client, "my_jobs", my_jobs_mock)
    monkeypatch.setattr(api_client, "list_jobs", list_jobs_guard)

    resp = client.get("/", headers={"Cookie": "ra_session=tok-live"})

    assert resp.status_code == 200
    assert b"Assigned Data Scientist Role" in resp.data
    my_jobs_mock.assert_called()
    list_jobs_guard.assert_not_called()


@pytest.mark.parametrize("role", ["admin", "recruiter", "auditor"])
def test_other_roles_see_the_global_job_list(
    monkeypatch: Any, client: Any, role: str
) -> None:
    monkeypatch.setattr(
        frontend_app_module, "get_settings", lambda: _cas_enabled_settings()
    )
    monkeypatch.setattr(
        api_client, "get_cas_user", MagicMock(return_value=_authenticated(role))
    )
    job_id = uuid4()
    list_jobs_mock = MagicMock(return_value=[_job(job_id, title="Global Job Listing")])
    my_jobs_guard = MagicMock(
        side_effect=AssertionError(f"my_jobs must not be called for role={role}")
    )
    monkeypatch.setattr(api_client, "list_jobs", list_jobs_mock)
    monkeypatch.setattr(api_client, "my_jobs", my_jobs_guard)

    resp = client.get("/", headers={"Cookie": "ra_session=tok-live"})

    assert resp.status_code == 200
    assert b"Global Job Listing" in resp.data
    list_jobs_mock.assert_called()
    my_jobs_guard.assert_not_called()


def test_none_role_authenticated_user_is_intercepted_with_pending_access(
    monkeypatch: Any, client: Any
) -> None:
    """SUPERSEDED by user-admin slice 4 (ADR-019 §10a reversal): this test
    previously asserted an authenticated ``role=None`` session fell back to the
    global ``list_jobs()`` — modelling None as a malformed/edge status. Slice 4
    redefines an authenticated ``role=None`` as a first-class "no role assigned
    yet" state that ``_cas_auth_gate`` intercepts BEFORE ``index()`` runs, so
    the fallback path is now unreachable via the client. The correct behaviour
    is: the pending-access page renders, and NEITHER ``list_jobs`` nor
    ``my_jobs`` is called."""
    monkeypatch.setattr(
        frontend_app_module, "get_settings", lambda: _cas_enabled_settings()
    )
    monkeypatch.setattr(
        api_client, "get_cas_user", MagicMock(return_value=_authenticated(None))
    )
    list_jobs_guard = MagicMock(
        side_effect=AssertionError("list_jobs must not run for a no-role user")
    )
    my_jobs_guard = MagicMock(
        side_effect=AssertionError("my_jobs must not run for a no-role user")
    )
    monkeypatch.setattr(api_client, "list_jobs", list_jobs_guard)
    monkeypatch.setattr(api_client, "my_jobs", my_jobs_guard)

    resp = client.get("/", headers={"Cookie": "ra_session=tok-live"})

    assert resp.status_code == 200
    assert b"no role" in resp.data.lower()
    list_jobs_guard.assert_not_called()
    my_jobs_guard.assert_not_called()


def test_dev_anonymous_default_does_not_call_get_cas_user(
    monkeypatch: Any, client: Any
) -> None:
    """CAS-disabled dev-anonymous default (``Settings.cas_enabled=False``):
    the index route must keep today's unchanged behaviour — ``list_jobs()``,
    with NO call to ``get_cas_user()`` at all — mirroring
    ``test_frontend_auth_gate.py``'s existing
    ``test_cas_disabled_is_a_passthrough_with_no_session`` idiom for the auth
    gate itself."""
    settings = Settings(cas_enabled=False)
    monkeypatch.setattr(frontend_app_module, "get_settings", lambda: settings)
    guard = MagicMock(
        side_effect=AssertionError(
            "index must not call get_cas_user when CAS is disabled"
        )
    )
    monkeypatch.setattr(api_client, "get_cas_user", guard)
    job_id = uuid4()
    monkeypatch.setattr(api_client, "list_jobs", MagicMock(return_value=[_job(job_id)]))

    resp = client.get("/")

    assert resp.status_code == 200
    guard.assert_not_called()


def test_hiring_manager_view_shows_a_scoped_view_hint(
    monkeypatch: Any, client: Any
) -> None:
    """The hiring_manager's scoped view must render a distinguishing hint so
    the UI signals "this is not the full job list" — pins a substring rather
    than exact copy."""
    monkeypatch.setattr(
        frontend_app_module, "get_settings", lambda: _cas_enabled_settings()
    )
    monkeypatch.setattr(
        api_client, "get_cas_user", MagicMock(return_value=_hiring_manager_status())
    )
    monkeypatch.setattr(api_client, "my_jobs", MagicMock(return_value=[]))

    resp = client.get("/", headers={"Cookie": "ra_session=tok-live"})

    assert resp.status_code == 200
    body = resp.data.decode().lower()
    assert ("assigned" in body) or ("your requisitions" in body)


def test_admin_role_view_does_not_show_the_scoped_view_hint(
    monkeypatch: Any, client: Any
) -> None:
    """The global-list view (any non-hiring_manager role) must NOT show the
    hiring_manager-only scoped-view hint — otherwise the hint carries no
    signal."""
    monkeypatch.setattr(
        frontend_app_module, "get_settings", lambda: _cas_enabled_settings()
    )
    monkeypatch.setattr(
        api_client, "get_cas_user", MagicMock(return_value=_authenticated("admin"))
    )
    monkeypatch.setattr(api_client, "list_jobs", MagicMock(return_value=[]))
    monkeypatch.setattr(
        api_client,
        "my_jobs",
        MagicMock(side_effect=AssertionError("my_jobs must not be called for admin")),
    )

    resp = client.get("/", headers={"Cookie": "ra_session=tok-live"})

    assert resp.status_code == 200
    body = resp.data.decode().lower()
    assert "your requisitions" not in body


def test_hiring_manager_my_jobs_backend_unavailable_returns_503_not_500(
    monkeypatch: Any, client: Any
) -> None:
    monkeypatch.setattr(
        frontend_app_module, "get_settings", lambda: _cas_enabled_settings()
    )
    monkeypatch.setattr(
        api_client, "get_cas_user", MagicMock(return_value=_hiring_manager_status())
    )
    monkeypatch.setattr(
        api_client,
        "my_jobs",
        MagicMock(side_effect=api_client.BackendUnavailable("backend down")),
    )

    resp = client.get("/", headers={"Cookie": "ra_session=tok-live"})

    assert resp.status_code == 503
    assert resp.status_code != 500
