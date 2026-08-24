"""Unit tests for ``src.services.session_service`` (FU-5 slice 5, ADR-019
§10 steps 4-5).

Nothing here exists yet (``core/src/services/session_service.py``,
``core/src/schemas/auth.py``), so every test is expected to FAIL (RED) as an
``ImportError``/``ModuleNotFoundError`` at collection.

Deliberately narrow scope. Most of this slice's correctness — expiry
predicates evaluated by real Postgres, the FK, the upsert race — is DB
behaviour and belongs in
``tests/integration/test_session_user_service_pg.py``; a mocked connection
cannot prove any of that (per CLAUDE.md's ``offline`` caveat). What IS
genuinely unit-provable without a database:

* the session id really comes from ``secrets.token_urlsafe(32)`` (entropy
  source, not a weaker generator),
* ``expires_at`` really is computed as ``now() + ttl_seconds`` in Python,
  independent of what Postgres does with it,
* ``get_active_session`` handles the "no matching row" case,
* ``refresh_if_needed``'s pure branch logic — whether it decides to touch
  the database at all — given a session object built directly in memory,
* ``revoke_session`` issues exactly one write, carrying the session id.

None of these assertions string-match SQL text; they capture bound
parameters or observe pure Python control flow.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest

from src.schemas.auth import Session
from src.services import session_service

_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{20,}$")


class _NullAsyncCtx:
    async def __aenter__(self) -> _NullAsyncCtx:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeConn:
    """Minimal double for the asyncpg connection surface these services use.

    ``fetchrow_row`` may be a plain dict (always returned) or a callable
    that receives the call's positional bind args and builds the row —
    used where the test needs to echo a value the service generated
    (e.g. the opaque session id) back into the "returned" row, the way a
    real ``RETURNING`` clause would.
    """

    def __init__(
        self,
        fetchrow_row: dict[str, Any] | Callable[..., dict[str, Any]] | None = None,
    ) -> None:
        self._fetchrow_row = fetchrow_row
        self.fetchrow_calls: list[tuple[Any, ...]] = []
        self.execute_calls: list[tuple[Any, ...]] = []

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        self.fetchrow_calls.append(args)
        if callable(self._fetchrow_row):
            return self._fetchrow_row(*args)
        return self._fetchrow_row

    async def execute(self, query: str, *args: Any) -> str:
        self.execute_calls.append(args)
        return "UPDATE 1"

    def transaction(self) -> _NullAsyncCtx:
        return _NullAsyncCtx()


class _ExecuteForbiddenConn:
    """Fails the test loudly if anything ever calls ``execute`` — used to
    prove ``refresh_if_needed`` skips the database entirely outside the
    idle-refresh window."""

    async def execute(self, query: str, *args: Any) -> str:
        raise AssertionError(
            "execute() must not be called when the session is outside the "
            "idle-refresh window"
        )

    def transaction(self) -> _NullAsyncCtx:
        return _NullAsyncCtx()


def _echo_row(*args: Any) -> dict[str, Any]:
    """Builds a plausible ``sessions`` RETURNING row from create_session's
    bind args, locating the generated opaque token and the computed
    ``expires_at`` by type/shape rather than by hard-coded position."""
    token = next(a for a in args if isinstance(a, str) and _TOKEN_RE.match(a))
    expires_at = next(a for a in args if isinstance(a, datetime))
    return {
        "id": token,
        "user_id": uuid4(),
        "expires_at": expires_at,
        "revoked_at": None,
        "user_agent": None,
        "ip_addr": None,
        "created_at": datetime.now(UTC),
    }


# ── create_session — token entropy ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_session_uses_secrets_token_urlsafe_with_32_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_nbytes: list[int] = []

    def fake_token_urlsafe(nbytes: int) -> str:
        captured_nbytes.append(nbytes)
        return "sentinel-opaque-token-value-000000"

    monkeypatch.setattr("secrets.token_urlsafe", fake_token_urlsafe)

    row = {
        "id": "sentinel-opaque-token-value-000000",
        "user_id": uuid4(),
        "expires_at": datetime.now(UTC) + timedelta(hours=1),
        "revoked_at": None,
        "user_agent": None,
        "ip_addr": None,
        "created_at": datetime.now(UTC),
    }
    conn = _FakeConn(row)

    session = await session_service.create_session(
        conn, user_id=uuid4(), ttl_seconds=3600
    )

    assert captured_nbytes == [32], "id must come from secrets.token_urlsafe(32)"
    assert session.id == "sentinel-opaque-token-value-000000"


@pytest.mark.asyncio
async def test_create_session_generates_distinct_tokens_across_calls() -> None:
    """Real (unmocked) ``secrets.token_urlsafe`` — two sessions must not
    collide, and the token must be long enough to be a real opaque id, not
    a short/weak generator."""
    conn_a = _FakeConn(_echo_row)
    conn_b = _FakeConn(_echo_row)

    session_a = await session_service.create_session(
        conn_a, user_id=uuid4(), ttl_seconds=3600
    )
    session_b = await session_service.create_session(
        conn_b, user_id=uuid4(), ttl_seconds=3600
    )

    assert session_a.id != session_b.id
    assert len(session_a.id) >= 40, "token_urlsafe(32) should yield ~43 chars"


@pytest.mark.asyncio
async def test_create_session_computes_expires_at_as_now_plus_ttl_seconds() -> None:
    conn = _FakeConn(_echo_row)
    before = datetime.now(UTC)

    session = await session_service.create_session(
        conn, user_id=uuid4(), ttl_seconds=3600
    )

    after = datetime.now(UTC)
    assert before + timedelta(seconds=3600) <= session.expires_at
    assert session.expires_at <= after + timedelta(seconds=3600)


# ── get_active_session — no matching row ────────────────────────────────────


@pytest.mark.asyncio
async def test_get_active_session_returns_none_when_no_matching_row() -> None:
    conn = _FakeConn(fetchrow_row=None)

    result = await session_service.get_active_session(conn, "no-such-session-id")

    assert result is None


# ── refresh_if_needed — pure branch logic ───────────────────────────────────


@pytest.mark.asyncio
async def test_refresh_if_needed_does_not_touch_db_outside_the_idle_window() -> None:
    session = Session(
        id="tok-outside-window",
        user_id=uuid4(),
        expires_at=datetime.now(UTC) + timedelta(hours=2),
        revoked_at=None,
        user_agent=None,
        ip_addr=None,
        created_at=datetime.now(UTC),
    )
    conn = _ExecuteForbiddenConn()

    result = await session_service.refresh_if_needed(
        conn, session, ttl_seconds=3600, idle_refresh_seconds=60
    )

    assert result.expires_at == session.expires_at


@pytest.mark.asyncio
async def test_refresh_if_needed_extends_and_writes_inside_the_idle_window() -> None:
    original_expiry = datetime.now(UTC) + timedelta(seconds=30)
    session = Session(
        id="tok-inside-window",
        user_id=uuid4(),
        expires_at=original_expiry,
        revoked_at=None,
        user_agent=None,
        ip_addr=None,
        created_at=datetime.now(UTC),
    )
    conn = _FakeConn()

    before = datetime.now(UTC)
    result = await session_service.refresh_if_needed(
        conn, session, ttl_seconds=3600, idle_refresh_seconds=60
    )
    after = datetime.now(UTC)

    assert result.expires_at > original_expiry
    assert before + timedelta(seconds=3600) <= result.expires_at
    assert result.expires_at <= after + timedelta(seconds=3600)
    assert len(conn.execute_calls) == 1


# ── revoke_session — exactly one write, carrying the session id ────────────


@pytest.mark.asyncio
async def test_revoke_session_issues_exactly_one_write_with_the_session_id() -> None:
    conn = _FakeConn()

    await session_service.revoke_session(conn, "session-abc-123")

    assert len(conn.execute_calls) == 1
    assert "session-abc-123" in conn.execute_calls[0]
