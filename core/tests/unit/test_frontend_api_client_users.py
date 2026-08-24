"""Failing (RED) tests for the user-admin-roles Flask viewer client wiring —
``frontend/api_client.py`` — slice 7 (the admin users page).

Today (before this slice):

* ``frontend.api_client`` has NO ``list_users`` and NO ``set_user_role``
  functions at all. Every test below that calls ``api_client.list_users(...)``
  or ``api_client.set_user_role(...)`` fails with ``AttributeError: module
  'frontend.api_client' has no attribute ...`` — RED half of the TDD cycle,
  mirroring ``test_frontend_my_jobs_view.py``'s treatment of a not-yet-existent
  ``my_jobs`` function.

**The contract this file locks (the coder implements exactly this shape),
mirroring the existing one-function-per-route convention in
``api_client.py``:**

* ``list_users(*, client=None) -> Any`` — ``GET /users``, returns the parsed
  JSON list. Same ``_request``/error-mapping plumbing as ``list_jobs``.
* ``set_user_role(user_id: UUID, role: str, *, client=None) -> Any`` —
  ``PATCH /users/{user_id}/role`` with JSON body ``{"role": role}``, returns
  the parsed JSON (the updated ``User``). A backend 409 (last-admin lockout)
  raises ``api_client.Conflict``; a 404 raises ``NotFound``; a 422 raises
  ``BadRequest`` — identical error mapping to ``transition_status``, which
  already exercises this exact ``_raise_for_status`` path for a PATCH route.

Not repeated here: ``Conflict``/``BadRequest``/``BackendError``'s class
hierarchy is already pinned by the existing
``test_frontend_api_client.py::test_not_found_and_backend_unavailable_share_a_common_base_error``
and the ``Conflict``/``BadRequest`` classes already exist today (unaffected by
this slice) — a hierarchy assertion here would pass before any of this file's
new functions exist, which is not a useful RED test.

Per CLAUDE.md: pure client-side HTTP wiring over ``httpx.MockTransport`` — no
real Postgres/Neo4j/Redis/Ollama I/O, so the offline suite is sufficient.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any
from uuid import uuid4

import httpx
import pytest

from frontend import api_client


def _client_with(
    handler: Callable[[httpx.Request], httpx.Response],
) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test")


def _json_handler(
    status_code: int, body: Any
) -> Callable[[httpx.Request], httpx.Response]:
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=body)

    return _handler


# ── api_client.list_users — GET /users wiring ────────────────────────────


def test_list_users_hits_the_expected_path_and_returns_parsed_json() -> None:
    captured: dict[str, httpx.URL] = {}
    user_id = uuid4()

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method  # type: ignore[assignment]
        captured["url"] = request.url
        return httpx.Response(
            200,
            json=[
                {
                    "id": str(user_id),
                    "cas_username": "asalah",
                    "display_name": "Adam Salah",
                    "email": "asalah@sfu.ca",
                    "role": "admin",
                    "active": True,
                    "created_at": "2026-01-01T00:00:00Z",
                    "last_seen_at": "2026-07-01T00:00:00Z",
                }
            ],
        )

    result = api_client.list_users(client=_client_with(handler))

    assert captured["method"] == "GET"
    assert captured["url"].path == "/users"
    assert result[0]["cas_username"] == "asalah"


def test_list_users_raises_backend_unavailable_on_5xx() -> None:
    client = _client_with(_json_handler(500, {"detail": "boom"}))
    with pytest.raises(api_client.BackendUnavailable):
        api_client.list_users(client=client)


def test_list_users_raises_backend_unavailable_on_connect_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = _client_with(handler)
    with pytest.raises(api_client.BackendUnavailable):
        api_client.list_users(client=client)


# ── api_client.set_user_role — PATCH /users/{id}/role wiring ────────────


def test_set_user_role_patches_the_expected_path_with_the_role_json_body() -> None:
    captured: dict[str, Any] = {}
    user_id = uuid4()

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = request.url
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": str(user_id),
                "cas_username": "bob",
                "display_name": "Bob",
                "email": "bob@example.org",
                "role": "recruiter",
                "active": True,
                "created_at": "2026-01-01T00:00:00Z",
                "last_seen_at": "2026-07-01T00:00:00Z",
            },
        )

    result = api_client.set_user_role(
        user_id, "recruiter", client=_client_with(handler)
    )

    assert captured["method"] == "PATCH"
    assert captured["url"].path == f"/users/{user_id}/role"
    assert captured["body"] == {"role": "recruiter"}
    assert result["role"] == "recruiter"


def test_set_user_role_raises_conflict_on_409_last_admin_lockout() -> None:
    client = _client_with(
        _json_handler(409, {"detail": "cannot demote the last active admin"})
    )
    with pytest.raises(api_client.Conflict):
        api_client.set_user_role(uuid4(), "recruiter", client=client)


def test_set_user_role_raises_not_found_on_404() -> None:
    client = _client_with(_json_handler(404, {"detail": "user not found"}))
    with pytest.raises(api_client.NotFound):
        api_client.set_user_role(uuid4(), "admin", client=client)


def test_set_user_role_raises_bad_request_on_422() -> None:
    client = _client_with(_json_handler(422, {"detail": [{"msg": "bad role"}]}))
    with pytest.raises(api_client.BadRequest):
        api_client.set_user_role(uuid4(), "not-a-real-role", client=client)
