"""Unit tests for FU-5 slice 11's session-cookie forwarding —
``frontend.api_client.build_client`` must ALSO forward the browser's
``ra_session`` cookie on the outgoing httpx request to the API, alongside the
existing ``X-API-Key`` header (ADR-019 §10). This is the load-bearing half of
slice 11: without it, every browser-driven action resolves as service/``api``
at the API and the whole FU-5 human-attribution chain (``created_by``,
reveal-audit-as-user) is unreachable from the UI.

**RED**: today ``build_client`` (``core/frontend/api_client.py:83-109``)
attaches ONLY ``X-API-Key`` and reads nothing from the inbound Flask request —
every assertion below that expects the ``ra_session`` cookie to be forwarded
fails because it is never attached at all.

**Mechanism note for the coder.** These tests do not perform a real network
call — they build the ``httpx.Client`` inside an active Flask request context
(``app.test_request_context``) and then call ``client.build_request(...)``,
which materialises the exact ``httpx.Request`` (headers included) that would
be sent, without touching the network. This is a genuinely observable pin on
the outgoing ``Cookie`` header (confirmed against real ``httpx`` 0.27
behaviour: a client constructed with a populated cookie jar attaches a
``Cookie`` header to every request built through it), not a pin on *how*
``build_client`` gets the value there (``cookies=`` kwarg vs. a manually
composed ``Cookie:`` header string both satisfy these assertions).

Also adds direct wiring tests for the new ``api_client.get_cas_user()``
function (``GET /auth/cas/user`` — the status endpoint the auth gate in
``test_frontend_auth_gate.py`` consumes), mirroring the existing
``get_job``/``get_resume`` wiring-test pattern in
``test_frontend_api_client.py``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest

from frontend import api_client
from frontend.app import app
from src.settings import Settings

# ── build_client: forwards the inbound ra_session cookie ────────────────


def _cookie_header_of(client: httpx.Client, path: str = "/jobs") -> str:
    request = client.build_request("GET", path)
    return request.headers.get("cookie") or ""


def test_build_client_forwards_inbound_ra_session_cookie() -> None:
    with app.test_request_context(
        "/", environ_base={"HTTP_COOKIE": "ra_session=abc123"}
    ):
        client = api_client.build_client()
        try:
            cookie_header = _cookie_header_of(client)
        finally:
            client.close()
    assert "ra_session=abc123" in cookie_header


def test_build_client_omits_cookie_header_when_no_inbound_session_cookie() -> None:
    with app.test_request_context("/"):
        client = api_client.build_client()
        try:
            cookie_header = _cookie_header_of(client)
        finally:
            client.close()
    assert "ra_session=" not in cookie_header


def test_build_client_forwards_only_the_session_cookie_not_unrelated_cookies() -> None:
    """Werkzeug parses ``HTTP_COOKIE`` into multiple named cookies; only the
    configured session cookie (``ra_session`` by default) is meant to be
    forwarded to the backend — an unrelated browser cookie is not a backend
    concern."""
    with app.test_request_context(
        "/", environ_base={"HTTP_COOKIE": "other_cookie=xyz; ra_session=def456"}
    ):
        client = api_client.build_client()
        try:
            cookie_header = _cookie_header_of(client)
        finally:
            client.close()
    assert "ra_session=def456" in cookie_header


def test_build_client_forwards_cookie_alongside_the_existing_api_key_header(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(
        api_client,
        "get_settings",
        lambda: Settings(api_key_recruiter="secret123"),
    )
    with app.test_request_context(
        "/", environ_base={"HTTP_COOKIE": "ra_session=abc123"}
    ):
        client = api_client.build_client()
        try:
            assert client.headers.get("X-API-Key") == "secret123"
            cookie_header = _cookie_header_of(client)
        finally:
            client.close()
    assert "ra_session=abc123" in cookie_header


def test_build_client_outside_a_flask_request_context_does_not_crash() -> None:
    """``build_client`` may still be called with no active Flask request (e.g.
    a future non-browser caller) — reading the inbound cookie must not raise
    ``RuntimeError: Working outside of request context``."""
    client = api_client.build_client()
    try:
        assert client is not None
    finally:
        client.close()


# ── api_client.get_cas_user — GET /auth/cas/user wiring ─────────────────


def _client_with(
    handler: Callable[[httpx.Request], httpx.Response],
) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test")


def test_get_cas_user_hits_the_expected_path_and_returns_parsed_json() -> None:
    captured: dict[str, httpx.URL] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = request.url
        return httpx.Response(
            200,
            json={"authenticated": False, "username": None, "cas_enabled": True},
        )

    result = api_client.get_cas_user(client=_client_with(handler))
    assert captured["url"].path == "/auth/cas/user"
    assert result == {
        "authenticated": False,
        "username": None,
        "cas_enabled": True,
    }


def test_get_cas_user_raises_backend_unavailable_on_5xx() -> None:
    client = _client_with(lambda request: httpx.Response(500, json={"detail": "boom"}))
    with pytest.raises(api_client.BackendUnavailable):
        api_client.get_cas_user(client=client)


def test_get_cas_user_raises_backend_unavailable_on_connect_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = _client_with(handler)
    with pytest.raises(api_client.BackendUnavailable):
        api_client.get_cas_user(client=client)
