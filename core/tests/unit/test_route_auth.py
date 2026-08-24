"""Route-level tests for FU-5 slice 6 — the CAS routes (``src.api.routes.auth``,
new module; does not exist yet) plus the cookie -> session -> user resolution
dependency (``src.api.deps.resolve_user``, new function; does not exist yet).

The whole file fails at collection (``ModuleNotFoundError`` /
``ImportError``) — RED half of the TDD cycle.

Scope boundary (slice 6 ONLY): the CAS routes, session-resolution dependency,
and dev-anonymous authz resolution per ADR-019 §10/§10b. Slice 6 does NOT
remove ``X-Actor-Name``/``resolve_actor`` (slice 7), does NOT write
``audit_log`` rows or touch reveal (slice 8), and does NOT touch the Flask
frontend (slice 11) — none of that is exercised here.

Uses a real ASGI app (a fresh ``FastAPI()``, not the whole ``src.api.main``
app) with ``httpx.AsyncClient`` + ``ASGITransport``, mirroring
``test_route_jobs.py``'s pattern: mock ``db`` via ``app.dependency_overrides``
targeting the shared ``get_db``, let the real service layer
(``cas_service``/``session_service``/``user_service``) run against a mocked
asyncpg-shaped connection double — no service-internals monkeypatching,
except the one genuinely-external boundary: CAS's own network call, which is
monkeypatched at ``auth_routes.cas_service.validate_ticket`` (an ``AsyncMock``
standing in for the real ``httpx`` round trip to ``cas.sfu.ca``).

**Ambiguities this file locks (the coder's implementation MUST match):**

* ``src.api.routes.auth`` exposes an ``APIRouter`` named ``router`` with
  ``prefix="/auth"`` — ``GET /auth/cas/login``, ``GET /auth/cas/validate``,
  ``GET /auth/cas/logout``, ``GET /auth/cas/user`` (GET for logout too,
  matching the ``hris`` port source). ``src.api.main`` mounts it with
  ``app.include_router(auth.router)`` — pinned separately by this red commit's
  edit to ``test_api.py``'s ``_PHASE_6_ROUTES`` set (ADR-019 §10 widens the
  route table; that assertion is a closed positive set and would otherwise
  wrongly still pass green with the new routes silently unregistered).
* Settings are read via a plain module-level ``get_settings()`` call inside
  the route functions — NOT ``Depends(get_settings)`` — mirroring
  ``resolve_role``'s existing convention in ``src.api.deps`` (every other
  settings consumer in this codebase, per ``grep``, calls ``get_settings()``
  directly; none uses ``Depends(get_settings)``). Tests here monkeypatch
  ``auth_routes.get_settings`` for the routes and ``deps.get_settings`` for
  the dependency.
* ``src.api.deps.resolve_user(request: Request, db: Db, ra_session: str |
  None = Cookie(default=None)) -> User | None`` — called DIRECTLY with
  keyword arguments in these tests (bypassing FastAPI's DI, exactly the way
  ``test_api_deps.py`` calls ``resolve_role``/``require_role`` directly). This
  is the ONE new dependency function slice 6 adds; it does not replace or
  modify ``resolve_role``/``require_role``/``resolve_actor``.
* CAS-disabled (``cas_enabled=False``) dev-anonymous resolution (§10b): the
  ROLE resolves to ``"admin"``. Whether the synthetic user is DB-backed
  (upserted, hris-style) or a plain in-memory object is explicitly left to
  the coder — these tests assert only the observable shape/role, never
  ``db.execute``/``db.fetchrow`` call counts for that branch.

**FU-5 slice 13 (security gate, FIX #2) — open-redirect sanitization.** The
``next`` query param was echoed unvalidated into a ``RedirectResponse`` in
``cas_login``, ``cas_validate``, and the dev-mode passthrough — an attacker
could craft ``/auth/cas/login?next=https://evil.com`` (or a protocol-relative
``//evil.com``, or a scheme smuggled past a naive ``/``-prefix check via
``javascript:``/backslash tricks browsers normalise to forward slashes) and
have the browser land off-site right after authentication. The fix: ``next``
must be a SAFE RELATIVE path — starts with exactly one ``/``, never ``//``,
never contains a scheme (``http:``/``https:``/``javascript:``) or a
backslash. An unsafe ``next`` is silently replaced with the safe default
``"/"`` (not a 400 — the least-surprising fallback, mirroring mature CAS
clients). See the "``next`` open-redirect sanitization" section near the
bottom of this file. None of the PRE-EXISTING tests above use an unsafe
``next`` value, so none needed adjusting for this fix — every legitimate
relative ``next`` used above (``/jobs``, ``/jobs/42``, ``/``) round-trips
unchanged under the new sanitization.

**FU-6 slice 10 (ADR-020 §7) — ``role`` on ``GET /auth/cas/user``.** The Flask
viewer needs the logged-in user's ``role`` to decide whether to default a
hiring_manager to the ``/my/jobs`` scoped view. This is purely additive to
the status endpoint's existing shape (``username``/``authenticated``/
``cas_enabled``): an authenticated session now also carries ``role`` (the
resolved ``users.role`` value, e.g. ``"hiring_manager"``/``"admin"``); an
unauthenticated caller (no valid session, ``cas_enabled=True``) gets
``role: null`` — PRESENT but ``None``, deliberately mirroring how
``username`` is already handled in that same anonymous branch (also present,
also ``None``) rather than omitting the key, so a client never has to
special-case "key missing" vs "key null". Dev-anonymous mode
(``cas_enabled=False``) resolves the synthetic admin identity, so
``role: "admin"`` there too — the existing ``authenticated``/``cas_enabled``
values in dev mode are UNCHANGED by this slice (see the dev-mode section of
``cas_user`` — it currently reports ``authenticated: False`` even though a
synthetic admin identity is what ``resolve_user`` would resolve; this slice
does not touch that field, only adds ``role``).

**Post-login frontend redirect fix (branch
``fix/cas-post-login-frontend-redirect``, bug observed live).** The Flask
frontend (``:5000``) and the FastAPI backend (``:18000``) are different
origins. After a successful CAS validate, the API redirects the browser to
``next`` (a relative path like ``/``); the browser resolves a relative
``Location`` against the origin that ISSUED the redirect, i.e. the API's own
``:18000`` origin — so the user lands on the raw JSON API (``:18000/jobs``)
instead of the Flask UI (``:5000/jobs``). The fix adds a new setting,
``Settings.cas_frontend_base_url: str = ""`` (empty = same-origin, preserving
today's behaviour exactly), and a "landing" redirect helper that prefixes it
onto ``safe_next`` ONLY for the two redirects where the browser actually
LANDS on a page for the human to look at:

* the ``cas_validate`` SUCCESS redirect (issued right after the session
  cookie is set), and
* the CAS-disabled dev passthrough in both ``cas_login`` and ``cas_validate``.

It must NOT be applied to the login->CAS-server redirect (which uses
``cas_service_base_url``/``_service_url`` for the API's OWN ``/auth/cas/
validate`` callback — CAS must call us back, not the frontend) or to the
no-ticket->``/auth/cas/login`` restart (which deliberately stays on the API
to redo the CAS dance). See the "frontend landing redirect" section near the
bottom of this file.

Security note pinned by the malicious-``next`` test below: ``safe_next`` is
already sanitized to a safe relative path by ``_safe_next`` BEFORE the
frontend base is prefixed, so ``f"{cas_frontend_base_url}{safe_next}"``
introduces no new open-redirect surface — a malicious ``next`` still
collapses to ``"/"`` first, so the final Location is
``f"{cas_frontend_base_url}/"``, never an attacker-controlled host.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs, urlparse
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from src.api import deps
from src.api.routes import auth as auth_routes
from src.models.pool import get_db
from src.settings import Settings

_NOW = dt.datetime(2026, 7, 22, tzinfo=dt.UTC)


class _Row(dict[str, Any]):
    """Dict double that behaves like an ``asyncpg.Record`` for ``row["x"]``
    AND for ``dict(row)`` — missing keys read as ``None`` rather than
    ``KeyError``, matching ``test_route_jobs.py``'s convention."""

    def __getitem__(self, key: str) -> Any:
        return dict.get(self, key)


class _NullAsyncCtx:
    async def __aenter__(self) -> _NullAsyncCtx:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeAuthConn:
    """Minimal asyncpg-connection double for the auth routes.

    Dispatches ``fetchrow`` by which of the two fixed table names
    (``sessions`` / ``users`` — both fixed by the slice-1/slice-3 DDL, not by
    this route's SQL phrasing) appears in the query text, rather than
    string-matching an exact query. A query naming neither table returns
    ``None``.
    """

    def __init__(
        self,
        *,
        session_row: _Row | None = None,
        user_row: _Row | None = None,
    ) -> None:
        self.session_row = session_row
        self.user_row = user_row
        self.fetchrow_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.execute_calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetchrow(self, query: str, *args: Any) -> _Row | None:
        self.fetchrow_calls.append((query, args))
        q = query.lower()
        if "sessions" in q:
            return self.session_row
        if "users" in q:
            return self.user_row
        return None

    async def execute(self, query: str, *args: Any) -> str:
        self.execute_calls.append((query, args))
        return "OK"

    def transaction(self) -> _NullAsyncCtx:
        return _NullAsyncCtx()


def _settings(
    *,
    cas_enabled: bool = True,
    cas_server_url: str = "https://cas.example.edu/cas",
    cas_validate_route: str = "/serviceValidate",
    session_cookie_name: str = "ra_session",
    default_admin_cas_username: str = "asalah",
    cas_frontend_base_url: str = "",
) -> Settings:
    return Settings(
        cas_enabled=cas_enabled,
        cas_server_url=cas_server_url,
        cas_validate_route=cas_validate_route,
        session_cookie_name=session_cookie_name,
        default_admin_cas_username=default_admin_cas_username,
        cas_frontend_base_url=cas_frontend_base_url,
        skill_hash_salt="test-salt",
        pii_key="test-key",
    )


def _session_row(*, id: str = "tok-abc123", user_id: UUID | None = None) -> _Row:
    return _Row(
        {
            "id": id,
            "user_id": user_id or uuid4(),
            "expires_at": _NOW + dt.timedelta(hours=8),
            "revoked_at": None,
            "user_agent": "pytest",
            "ip_addr": None,
            "created_at": _NOW,
        }
    )


def _user_row(
    *,
    id: UUID | None = None,
    cas_username: str = "alice",
    role: str = "recruiter",
) -> _Row:
    return _Row(
        {
            "id": id or uuid4(),
            "cas_username": cas_username,
            "display_name": None,
            "email": None,
            "role": role,
            "active": True,
            "created_at": _NOW,
            "last_seen_at": _NOW,
        }
    )


def _build_app(conn: _FakeAuthConn) -> FastAPI:
    app = FastAPI()
    app.include_router(auth_routes.router)

    async def _get_db_override() -> AsyncIterator[Any]:
        yield conn

    app.dependency_overrides[get_db] = _get_db_override
    return app


async def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _mock_successful_validate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wires the three service calls a successful ``cas_validate`` makes so
    the route reaches its final redirect without hitting a real DB/CAS
    server — shared by several tests in the "frontend landing redirect"
    section below."""
    monkeypatch.setattr(
        auth_routes.cas_service,
        "validate_ticket",
        AsyncMock(return_value="alice"),
    )
    monkeypatch.setattr(
        auth_routes.user_service,
        "provision_or_get",
        AsyncMock(return_value=SimpleNamespace(id=uuid4())),
    )
    monkeypatch.setattr(
        auth_routes.session_service,
        "create_session",
        AsyncMock(return_value=SimpleNamespace(id="tok-new-session")),
    )


# ── GET /auth/cas/login ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cas_login_redirects_to_cas_server_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(cas_enabled=True, cas_server_url="https://cas.example.edu/cas")
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    app = _build_app(_FakeAuthConn())
    async with await _client(app) as client:
        resp = await client.get("/auth/cas/login", params={"next": "/jobs"})
    assert resp.status_code == 302
    location = resp.headers["location"]
    assert location.startswith("https://cas.example.edu/cas/login?")
    outer_qs = parse_qs(urlparse(location).query)
    assert "service" in outer_qs, "login redirect must carry a service= param"


@pytest.mark.asyncio
async def test_cas_login_service_url_round_trips_the_next_param(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(cas_enabled=True, cas_server_url="https://cas.example.edu/cas")
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    app = _build_app(_FakeAuthConn())
    async with await _client(app) as client:
        resp = await client.get("/auth/cas/login", params={"next": "/jobs/42"})
    location = resp.headers["location"]
    outer_qs = parse_qs(urlparse(location).query)
    service_url = outer_qs["service"][0]
    # The service URL itself must point back at our own /cas/validate and
    # carry the caller's `next` — decode its own query string in turn.
    service_parsed = urlparse(service_url)
    assert service_parsed.path.endswith("/auth/cas/validate")
    service_qs = parse_qs(service_parsed.query)
    assert service_qs.get("next") == ["/jobs/42"]


@pytest.mark.asyncio
async def test_cas_login_dev_passthrough_when_cas_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(cas_enabled=False)
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    app = _build_app(_FakeAuthConn())
    async with await _client(app) as client:
        resp = await client.get("/auth/cas/login", params={"next": "/jobs"})
    assert resp.status_code == 302
    assert resp.headers["location"] == "/jobs"


@pytest.mark.asyncio
async def test_cas_login_dev_passthrough_never_redirects_to_a_cas_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(
        cas_enabled=False, cas_server_url="https://cas.example.edu/cas"
    )
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    app = _build_app(_FakeAuthConn())
    async with await _client(app) as client:
        resp = await client.get("/auth/cas/login", params={"next": "/"})
    assert "cas.example.edu" not in resp.headers["location"]


# ── GET /auth/cas/validate ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cas_validate_with_no_ticket_redirects_back_to_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(cas_enabled=True)
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    app = _build_app(_FakeAuthConn())
    async with await _client(app) as client:
        resp = await client.get("/auth/cas/validate")
    assert resp.status_code in (302, 303, 307)
    assert "/auth/cas/login" in resp.headers["location"]


@pytest.mark.asyncio
async def test_cas_validate_with_no_ticket_never_calls_validate_ticket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(cas_enabled=True)
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    spy = AsyncMock()
    monkeypatch.setattr(auth_routes.cas_service, "validate_ticket", spy)
    app = _build_app(_FakeAuthConn())
    async with await _client(app) as client:
        await client.get("/auth/cas/validate", params={"next": "/jobs"})
    spy.assert_not_called()


# ── GET /auth/cas/user ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cas_user_anonymous_never_401s(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(cas_enabled=True)
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    app = _build_app(_FakeAuthConn())
    async with await _client(app) as client:
        resp = await client.get("/auth/cas/user")
    assert resp.status_code == 200
    body = resp.json()
    assert body["authenticated"] is False
    assert body["cas_enabled"] is True
    assert "username" in body


@pytest.mark.asyncio
async def test_cas_user_dev_anonymous_mode_never_401s(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(cas_enabled=False)
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    app = _build_app(_FakeAuthConn())
    async with await _client(app) as client:
        resp = await client.get("/auth/cas/user")
    assert resp.status_code == 200
    body = resp.json()
    assert body["cas_enabled"] is False


@pytest.mark.asyncio
async def test_cas_user_returns_the_logged_in_username_from_a_valid_cookie(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(cas_enabled=True, session_cookie_name="ra_session")
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    user_id = uuid4()
    conn = _FakeAuthConn(
        session_row=_session_row(id="tok-live", user_id=user_id),
        user_row=_user_row(id=user_id, cas_username="alice"),
    )
    app = _build_app(conn)
    async with await _client(app) as client:
        resp = await client.get("/auth/cas/user", cookies={"ra_session": "tok-live"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["authenticated"] is True
    assert body["username"] == "alice"


@pytest.mark.asyncio
async def test_cas_user_never_401s_for_an_expired_or_unknown_cookie(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(cas_enabled=True, session_cookie_name="ra_session")
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    conn = _FakeAuthConn(session_row=None)  # no active session found
    app = _build_app(conn)
    async with await _client(app) as client:
        resp = await client.get(
            "/auth/cas/user", cookies={"ra_session": "bogus-or-expired"}
        )
    assert resp.status_code == 200
    assert resp.json()["authenticated"] is False


# ── GET /auth/cas/user — `role` (FU-6 slice 10, ADR-020 §7) ────────────────
#
# Purely additive: an authenticated session's resolved ``users.role`` is
# surfaced alongside the existing fields; an unauthenticated caller gets
# ``role: null`` (PRESENT, not omitted — matching how ``username`` is
# already handled in that branch); dev-anonymous mode reports the synthetic
# admin's role.


@pytest.mark.asyncio
async def test_cas_user_authenticated_hiring_manager_reports_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(cas_enabled=True, session_cookie_name="ra_session")
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    user_id = uuid4()
    conn = _FakeAuthConn(
        session_row=_session_row(id="tok-hm", user_id=user_id),
        user_row=_user_row(id=user_id, cas_username="hank", role="hiring_manager"),
    )
    app = _build_app(conn)
    async with await _client(app) as client:
        resp = await client.get("/auth/cas/user", cookies={"ra_session": "tok-hm"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["authenticated"] is True
    assert body["username"] == "hank"
    assert body["role"] == "hiring_manager"


@pytest.mark.asyncio
async def test_cas_user_authenticated_admin_reports_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(cas_enabled=True, session_cookie_name="ra_session")
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    user_id = uuid4()
    conn = _FakeAuthConn(
        session_row=_session_row(id="tok-admin", user_id=user_id),
        user_row=_user_row(id=user_id, cas_username="adminuser", role="admin"),
    )
    app = _build_app(conn)
    async with await _client(app) as client:
        resp = await client.get("/auth/cas/user", cookies={"ra_session": "tok-admin"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["authenticated"] is True
    assert body["role"] == "admin"


@pytest.mark.asyncio
async def test_cas_user_anonymous_role_is_null_but_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unauthenticated (CAS enabled, no cookie): ``role`` must be PRESENT
    and ``null`` — the same shape ``username`` already uses in this branch —
    so a client never has to special-case "key missing" vs "key null"."""
    settings = _settings(cas_enabled=True)
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    app = _build_app(_FakeAuthConn())
    async with await _client(app) as client:
        resp = await client.get("/auth/cas/user")
    assert resp.status_code == 200
    body = resp.json()
    assert body["authenticated"] is False
    assert "role" in body, "role must be present (null), matching username's shape"
    assert body["role"] is None


@pytest.mark.asyncio
async def test_cas_user_role_is_null_for_an_expired_or_unknown_cookie(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(cas_enabled=True, session_cookie_name="ra_session")
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    conn = _FakeAuthConn(session_row=None)  # no active session found
    app = _build_app(conn)
    async with await _client(app) as client:
        resp = await client.get(
            "/auth/cas/user", cookies={"ra_session": "bogus-or-expired"}
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["authenticated"] is False
    assert body["role"] is None


@pytest.mark.asyncio
async def test_cas_user_dev_anonymous_mode_reports_admin_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR-019 §10b / ADR-020 §7 — CAS disabled resolves the synthetic
    dev-admin identity (``resolve_user``'s ``role="admin"``); the status
    endpoint must surface that role too. This does NOT change the existing
    ``authenticated``/``cas_enabled`` values the dev-mode branch already
    reports — only adds ``role``."""
    settings = _settings(cas_enabled=False)
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    app = _build_app(_FakeAuthConn())
    async with await _client(app) as client:
        resp = await client.get("/auth/cas/user")
    assert resp.status_code == 200
    body = resp.json()
    assert body["cas_enabled"] is False
    assert body["role"] == "admin"


# ── src.api.deps.resolve_user ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_user_dev_anonymous_resolves_a_synthetic_admin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR-019 §10b — CAS disabled: every request resolves an admin identity
    for AUTHORIZATION purposes. This slice writes no audit rows at all."""
    settings = _settings(cas_enabled=False)
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    request = MagicMock()
    conn = _FakeAuthConn()

    user = await deps.resolve_user(request=request, db=conn, ra_session=None)

    assert user is not None
    assert user.role == "admin"


@pytest.mark.asyncio
async def test_resolve_user_dev_anonymous_ignores_any_presented_cookie(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CAS-disabled mode short-circuits before any cookie/session lookup —
    a bogus cookie must not change the outcome."""
    settings = _settings(cas_enabled=False)
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    request = MagicMock()
    conn = _FakeAuthConn()

    user = await deps.resolve_user(
        request=request, db=conn, ra_session="whatever-this-is-ignored"
    )

    assert user is not None
    assert user.role == "admin"


@pytest.mark.asyncio
async def test_resolve_user_returns_none_with_no_session_cookie_when_cas_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(cas_enabled=True)
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    request = MagicMock()
    conn = _FakeAuthConn()

    user = await deps.resolve_user(request=request, db=conn, ra_session=None)

    assert user is None


@pytest.mark.asyncio
async def test_resolve_user_returns_none_for_an_expired_or_unknown_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(cas_enabled=True)
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    request = MagicMock()
    conn = _FakeAuthConn(session_row=None)

    user = await deps.resolve_user(
        request=request, db=conn, ra_session="stale-or-forged-token"
    )

    assert user is None


@pytest.mark.asyncio
async def test_resolve_user_resolves_the_real_user_for_a_live_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(cas_enabled=True)
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    request = MagicMock()
    user_id = uuid4()
    conn = _FakeAuthConn(
        session_row=_session_row(id="tok-live", user_id=user_id),
        user_row=_user_row(id=user_id, cas_username="bob", role="admin"),
    )

    user = await deps.resolve_user(request=request, db=conn, ra_session="tok-live")

    assert user is not None
    assert user.cas_username == "bob"
    assert user.role == "admin"


# ── `next` open-redirect sanitization (security gate, FU-5 slice 13, FIX #2)
# ─────────────────────────────────────────────────────────────────────────
#
# The `next` query param was echoed unvalidated into a `RedirectResponse` in
# `cas_login`, `cas_validate`, and the dev-mode passthrough. `next` must be a
# SAFE RELATIVE path — starts with exactly one `/`, never `//` (protocol-
# relative), never contains a scheme (`http:`/`https:`/`javascript:`) or a
# backslash (browsers normalise `\` to `/`, defeating a naive `/`-prefix-only
# check). An unsafe `next` is silently replaced with the safe default `"/"` —
# not a 400, the least-surprising fallback mirroring mature CAS clients.

_MALICIOUS_NEXT_VALUES = [
    "https://evil.com",
    "//evil.com",
    "http:evil",
    "javascript:alert(1)",
    "\\evil.com",
    "/\\evil.com",
]


@pytest.mark.parametrize("malicious_next", _MALICIOUS_NEXT_VALUES)
@pytest.mark.asyncio
async def test_cas_login_sanitizes_an_unsafe_next_in_the_service_url(
    malicious_next: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CAS enabled: the `next` embedded in the `service=` param handed to the
    CAS server (and echoed straight back to us at /auth/cas/validate after a
    successful login) must never carry an attacker-controlled off-site
    target — the CAS server has no reason to reject an off-site `service`
    sub-param, so this is the step that actually protects the browser."""
    settings = _settings(cas_enabled=True, cas_server_url="https://cas.example.edu/cas")
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    app = _build_app(_FakeAuthConn())
    async with await _client(app) as client:
        resp = await client.get("/auth/cas/login", params={"next": malicious_next})
    location = resp.headers["location"]
    outer_qs = parse_qs(urlparse(location).query)
    service_url = outer_qs["service"][0]
    service_qs = parse_qs(urlparse(service_url).query)
    sanitized_next = service_qs.get("next", ["/"])[0]
    assert sanitized_next == "/", (
        f"unsafe next {malicious_next!r} must be replaced with the safe "
        f"default '/', got {sanitized_next!r}"
    )
    assert "evil.com" not in sanitized_next


@pytest.mark.parametrize("malicious_next", _MALICIOUS_NEXT_VALUES)
@pytest.mark.asyncio
async def test_cas_login_dev_passthrough_sanitizes_an_unsafe_next(
    malicious_next: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CAS disabled (dev mode): `cas_login` 302s straight to `next` — the
    MOST direct open-redirect path (no CAS round trip in between), so the
    Location header itself must never carry the attacker's target."""
    settings = _settings(cas_enabled=False)
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    app = _build_app(_FakeAuthConn())
    async with await _client(app) as client:
        resp = await client.get("/auth/cas/login", params={"next": malicious_next})
    assert resp.status_code == 302
    assert resp.headers["location"] == "/", (
        f"unsafe next {malicious_next!r} must fall back to '/', got "
        f"{resp.headers['location']!r}"
    )


@pytest.mark.parametrize("malicious_next", _MALICIOUS_NEXT_VALUES)
@pytest.mark.asyncio
async def test_cas_validate_dev_passthrough_sanitizes_an_unsafe_next(
    malicious_next: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CAS disabled: `cas_validate` also 302s straight to `next` before any
    ticket handling — same direct open-redirect shape as the login
    passthrough above."""
    settings = _settings(cas_enabled=False)
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    app = _build_app(_FakeAuthConn())
    async with await _client(app) as client:
        resp = await client.get("/auth/cas/validate", params={"next": malicious_next})
    assert resp.status_code == 302
    assert resp.headers["location"] == "/", (
        f"unsafe next {malicious_next!r} must fall back to '/', got "
        f"{resp.headers['location']!r}"
    )


@pytest.mark.parametrize("malicious_next", _MALICIOUS_NEXT_VALUES)
@pytest.mark.asyncio
async def test_cas_validate_post_auth_redirect_sanitizes_an_unsafe_next(
    malicious_next: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CAS enabled, a genuinely valid ticket: the post-auth redirect (issued
    right after the session cookie is set) must not honour an unsafe `next`
    either — this is the step a real attacker would exploit, chaining a
    legitimate CAS login onto an off-site landing page."""
    settings = _settings(cas_enabled=True)
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    monkeypatch.setattr(
        auth_routes.cas_service,
        "validate_ticket",
        AsyncMock(return_value="alice"),
    )
    user_id = uuid4()
    monkeypatch.setattr(
        auth_routes.user_service,
        "provision_or_get",
        AsyncMock(return_value=SimpleNamespace(id=user_id)),
    )
    monkeypatch.setattr(
        auth_routes.session_service,
        "create_session",
        AsyncMock(return_value=SimpleNamespace(id="tok-new-session")),
    )
    app = _build_app(_FakeAuthConn())
    async with await _client(app) as client:
        resp = await client.get(
            "/auth/cas/validate",
            params={"ticket": "ST-123", "next": malicious_next},
        )
    assert resp.status_code == 302
    assert resp.headers["location"] == "/", (
        f"unsafe next {malicious_next!r} must fall back to '/', got "
        f"{resp.headers['location']!r}"
    )


@pytest.mark.asyncio
async def test_cas_login_service_url_preserves_a_legitimate_relative_next(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A genuinely safe relative `next` must NOT be perturbed by the new
    sanitization — only unsafe values fall back to '/'."""
    settings = _settings(cas_enabled=True, cas_server_url="https://cas.example.edu/cas")
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    app = _build_app(_FakeAuthConn())
    async with await _client(app) as client:
        resp = await client.get("/auth/cas/login", params={"next": "/jobs/123"})
    location = resp.headers["location"]
    outer_qs = parse_qs(urlparse(location).query)
    service_url = outer_qs["service"][0]
    service_qs = parse_qs(urlparse(service_url).query)
    assert service_qs["next"] == ["/jobs/123"]


@pytest.mark.asyncio
async def test_cas_login_dev_passthrough_preserves_a_legitimate_relative_next(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(cas_enabled=False)
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    app = _build_app(_FakeAuthConn())
    async with await _client(app) as client:
        resp = await client.get("/auth/cas/login", params={"next": "/jobs/123"})
    assert resp.headers["location"] == "/jobs/123"


@pytest.mark.asyncio
async def test_cas_validate_post_auth_redirect_preserves_a_legitimate_relative_next(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(cas_enabled=True)
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    monkeypatch.setattr(
        auth_routes.cas_service,
        "validate_ticket",
        AsyncMock(return_value="alice"),
    )
    user_id = uuid4()
    monkeypatch.setattr(
        auth_routes.user_service,
        "provision_or_get",
        AsyncMock(return_value=SimpleNamespace(id=user_id)),
    )
    monkeypatch.setattr(
        auth_routes.session_service,
        "create_session",
        AsyncMock(return_value=SimpleNamespace(id="tok-new-session")),
    )
    app = _build_app(_FakeAuthConn())
    async with await _client(app) as client:
        resp = await client.get(
            "/auth/cas/validate",
            params={"ticket": "ST-123", "next": "/jobs/123"},
        )
    assert resp.headers["location"] == "/jobs/123"


# ── frontend landing redirect (fix/cas-post-login-frontend-redirect) ─────
#
# Bug (observed live): the Flask frontend (:5000) and the FastAPI backend
# (:18000) are different origins. A relative `Location` header issued by the
# API is resolved by the BROWSER against the API's OWN origin — so a bare
# `next=/` (or `/jobs`) lands the user on the raw JSON API, not the Flask
# UI. The fix: when `settings.cas_frontend_base_url` is non-empty, the two
# "landing" redirects (post-validate success, and the CAS-disabled dev
# passthrough in both `cas_login` and `cas_validate`) prefix it onto the
# already-sanitized `safe_next`. The login->CAS-server redirect and the
# no-ticket restart are UNCHANGED (asserted below too) — both must keep
# using the API's own origin.


@pytest.mark.asyncio
async def test_cas_validate_success_redirects_to_frontend_origin_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bug, pinned directly: a successful CAS validate with
    `cas_frontend_base_url` configured must land the browser on the FRONTEND
    origin, not the bare relative path the API would otherwise resolve
    against itself."""
    settings = _settings(
        cas_enabled=True, cas_frontend_base_url="http://localhost:5000"
    )
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    _mock_successful_validate(monkeypatch)
    app = _build_app(_FakeAuthConn())
    async with await _client(app) as client:
        resp = await client.get(
            "/auth/cas/validate", params={"ticket": "ST-123", "next": "/jobs"}
        )
    assert resp.status_code == 302
    assert resp.headers["location"] == "http://localhost:5000/jobs"


@pytest.mark.asyncio
async def test_cas_validate_success_redirects_to_frontend_root_for_default_next(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(
        cas_enabled=True, cas_frontend_base_url="http://localhost:5000"
    )
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    _mock_successful_validate(monkeypatch)
    app = _build_app(_FakeAuthConn())
    async with await _client(app) as client:
        resp = await client.get(
            "/auth/cas/validate", params={"ticket": "ST-123", "next": "/"}
        )
    assert resp.status_code == 302
    assert resp.headers["location"] == "http://localhost:5000/"


@pytest.mark.asyncio
async def test_cas_validate_success_still_sets_the_session_cookie_with_frontend_base(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The frontend-origin redirect must not come at the cost of the session
    cookie — the whole point of the redirect is to land an authenticated
    browser on the UI."""
    settings = _settings(
        cas_enabled=True, cas_frontend_base_url="http://localhost:5000"
    )
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    _mock_successful_validate(monkeypatch)
    app = _build_app(_FakeAuthConn())
    async with await _client(app) as client:
        resp = await client.get(
            "/auth/cas/validate", params={"ticket": "ST-123", "next": "/jobs"}
        )
    assert resp.headers["location"] == "http://localhost:5000/jobs"
    assert "ra_session=tok-new-session" in resp.headers.get("set-cookie", "")


@pytest.mark.asyncio
async def test_cas_validate_success_redirect_is_unchanged_when_frontend_base_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default `cas_frontend_base_url=""` must preserve today's relative
    redirect exactly — this is the same-origin (no bug) deployment shape and
    must not regress for anyone who has not opted in to the new setting."""
    settings = _settings(cas_enabled=True, cas_frontend_base_url="")
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    _mock_successful_validate(monkeypatch)
    app = _build_app(_FakeAuthConn())
    async with await _client(app) as client:
        resp = await client.get(
            "/auth/cas/validate", params={"ticket": "ST-123", "next": "/jobs"}
        )
    assert resp.status_code == 302
    assert resp.headers["location"] == "/jobs"


@pytest.mark.parametrize("malicious_next", _MALICIOUS_NEXT_VALUES)
@pytest.mark.asyncio
async def test_cas_validate_success_frontend_redirect_never_honours_a_malicious_next(
    malicious_next: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`safe_next` sanitization must still run BEFORE the frontend base is
    prefixed — a malicious `next` combined with a configured frontend base
    must never produce a Location containing the attacker's host; it must
    collapse to the frontend base's root, exactly like the same-origin case
    collapses to '/'."""
    settings = _settings(
        cas_enabled=True, cas_frontend_base_url="http://localhost:5000"
    )
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    _mock_successful_validate(monkeypatch)
    app = _build_app(_FakeAuthConn())
    async with await _client(app) as client:
        resp = await client.get(
            "/auth/cas/validate",
            params={"ticket": "ST-123", "next": malicious_next},
        )
    assert resp.headers["location"] == "http://localhost:5000/"
    assert "evil.com" not in resp.headers["location"]


@pytest.mark.asyncio
async def test_cas_validate_dev_passthrough_redirects_to_frontend_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CAS-disabled dev passthrough in `cas_validate` must also land on the
    frontend origin when configured — this is the local-dev, CAS-disabled
    equivalent of the same bug."""
    settings = _settings(
        cas_enabled=False, cas_frontend_base_url="http://localhost:5000"
    )
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    app = _build_app(_FakeAuthConn())
    async with await _client(app) as client:
        resp = await client.get("/auth/cas/validate", params={"next": "/jobs"})
    assert resp.status_code == 302
    assert resp.headers["location"] == "http://localhost:5000/jobs"


@pytest.mark.asyncio
async def test_cas_login_dev_passthrough_redirects_to_frontend_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CAS-disabled dev passthrough in `cas_login` must also land on the
    frontend origin when configured."""
    settings = _settings(
        cas_enabled=False, cas_frontend_base_url="http://localhost:5000"
    )
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    app = _build_app(_FakeAuthConn())
    async with await _client(app) as client:
        resp = await client.get("/auth/cas/login", params={"next": "/jobs"})
    assert resp.status_code == 302
    assert resp.headers["location"] == "http://localhost:5000/jobs"


@pytest.mark.asyncio
async def test_cas_login_to_cas_server_is_unchanged_by_frontend_base_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The login->CAS-server redirect (and the `service=` callback URL
    embedded in it) must keep using `cas_service_base_url`/`_service_url` —
    the API's OWN callback — completely independent of
    `cas_frontend_base_url`. CAS must call back to the API, never the
    frontend, so this redirect target must not gain the frontend prefix even
    when one is configured."""
    settings = _settings(
        cas_enabled=True,
        cas_server_url="https://cas.example.edu/cas",
        cas_frontend_base_url="http://localhost:5000",
    )
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    app = _build_app(_FakeAuthConn())
    async with await _client(app) as client:
        resp = await client.get("/auth/cas/login", params={"next": "/jobs"})
    location = resp.headers["location"]
    assert location.startswith("https://cas.example.edu/cas/login?")
    outer_qs = parse_qs(urlparse(location).query)
    service_url = outer_qs["service"][0]
    assert "localhost:5000" not in service_url
    service_parsed = urlparse(service_url)
    assert service_parsed.path.endswith("/auth/cas/validate")


@pytest.mark.asyncio
async def test_cas_validate_no_ticket_restart_is_unchanged_by_frontend_base_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The no-ticket restart (`/auth/cas/login?next=...`) deliberately stays
    on the API to redo the CAS dance — it must NOT gain the frontend prefix
    even when `cas_frontend_base_url` is configured."""
    settings = _settings(
        cas_enabled=True, cas_frontend_base_url="http://localhost:5000"
    )
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    app = _build_app(_FakeAuthConn())
    async with await _client(app) as client:
        resp = await client.get("/auth/cas/validate", params={"next": "/jobs"})
    location = resp.headers["location"]
    assert location.startswith("/auth/cas/login")
    assert "localhost:5000" not in location


# ── GET /auth/cas/logout — the `service`/landing param lands the browser on
# the FRONTEND, not the API (branch feat/frontend-auth-widget, part 2 of the
# two-part CAS-UI fix; same split-origin cause as the post-LOGIN redirect
# fixed above, now for post-LOGOUT) ────────────────────────────────────────
#
# Bug (observed live): CAS's own `/logout?service=...` redirects the browser
# to whatever `service` URL `cas_logout` hands it. Today that is always
# `_service_base(settings, request)` — the API's OWN origin
# (`cas_service_base_url`, :18000) — so after CAS logout the browser lands on
# the raw JSON API, never back on the Flask UI (:5000). Likewise the
# CAS-disabled dev-mode landing is a hardcoded `"/"`, resolved by the browser
# against the API's own origin. The fix: when
# `settings.cas_frontend_base_url` is configured, `cas_logout` hands CAS the
# FRONTEND origin as `service` instead (CAS-enabled branch), and uses
# `_landing_url(settings, "/")` for the dev-mode branch — mirroring the
# post-login fix's `_landing_url` helper exactly. The session cookie is still
# deleted in both branches, unconditionally, regardless of this setting.


@pytest.mark.asyncio
async def test_cas_logout_service_is_the_frontend_origin_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bug, pinned directly: CAS-enabled logout with
    `cas_frontend_base_url` configured must hand CAS the FRONTEND origin as
    `service=`, not the API's own `cas_service_base_url` — so the browser
    lands back on the Flask UI once CAS's own logout redirect fires."""
    settings = _settings(
        cas_enabled=True,
        cas_server_url="https://cas.example.edu/cas",
        cas_frontend_base_url="http://localhost:5000",
    )
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    conn = _FakeAuthConn()
    app = _build_app(conn)
    async with await _client(app) as client:
        resp = await client.get(
            "/auth/cas/logout",
            cookies={settings.session_cookie_name: "tok-live"},
        )
    assert resp.status_code == 302
    location = resp.headers["location"]
    assert location.startswith("https://cas.example.edu/cas/logout?")
    outer_qs = parse_qs(urlparse(location).query)
    assert outer_qs["service"][0] == "http://localhost:5000", (
        "the service handed to CAS's own /logout must be the FRONTEND "
        f"origin, not the API base; got {outer_qs['service'][0]!r}"
    )


@pytest.mark.asyncio
async def test_cas_logout_deletes_the_session_cookie_with_frontend_base_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(
        cas_enabled=True,
        cas_frontend_base_url="http://localhost:5000",
        session_cookie_name="ra_session",
    )
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    conn = _FakeAuthConn()
    app = _build_app(conn)
    async with await _client(app) as client:
        resp = await client.get("/auth/cas/logout", cookies={"ra_session": "tok-live"})
    assert "ra_session" in resp.headers.get("set-cookie", "")


@pytest.mark.asyncio
async def test_cas_logout_still_revokes_the_session_with_frontend_base_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The frontend-origin fix must not come at the cost of actually revoking
    the session — the local double records an `execute` touching the
    `sessions`/`revoked_at` predicate."""
    settings = _settings(
        cas_enabled=True,
        cas_frontend_base_url="http://localhost:5000",
        session_cookie_name="ra_session",
    )
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    conn = _FakeAuthConn()
    app = _build_app(conn)
    async with await _client(app) as client:
        await client.get("/auth/cas/logout", cookies={"ra_session": "tok-live"})
    assert any(
        "revoked_at" in query.lower() and "sessions" in query.lower()
        for query, _ in conn.execute_calls
    ), "logout must still revoke the session row"


@pytest.mark.asyncio
async def test_cas_logout_service_is_unchanged_when_frontend_base_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default `cas_frontend_base_url=""` must preserve today's behaviour
    exactly: the service handed to CAS stays `_service_base` (the API's own
    origin) — this is the same-origin (no bug) deployment shape and must not
    regress for anyone who has not opted in to the new setting."""
    settings = _settings(
        cas_enabled=True,
        cas_server_url="https://cas.example.edu/cas",
        cas_frontend_base_url="",
    )
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    conn = _FakeAuthConn()
    app = _build_app(conn)
    async with await _client(app) as client:
        resp = await client.get(
            "/auth/cas/logout",
            cookies={settings.session_cookie_name: "tok-live"},
        )
    assert resp.status_code == 302
    location = resp.headers["location"]
    outer_qs = parse_qs(urlparse(location).query)
    assert outer_qs["service"][0] == "http://localhost:8000", (
        "with no frontend base configured, the service must stay the API's "
        f"own default origin; got {outer_qs['service'][0]!r}"
    )


@pytest.mark.asyncio
async def test_cas_disabled_logout_lands_on_the_frontend_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CAS-disabled dev-mode logout must also land on the FRONTEND origin
    when configured — the local-dev, CAS-disabled equivalent of the same
    bug, mirroring `_landing_url`'s existing use in `cas_login`/
    `cas_validate`."""
    settings = _settings(
        cas_enabled=False,
        cas_frontend_base_url="http://localhost:5000",
        session_cookie_name="ra_session",
    )
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    conn = _FakeAuthConn()
    app = _build_app(conn)
    async with await _client(app) as client:
        resp = await client.get("/auth/cas/logout", cookies={"ra_session": "tok-live"})
    assert resp.status_code == 302
    assert resp.headers["location"] == "http://localhost:5000/"
    assert "ra_session" in resp.headers.get("set-cookie", "")
