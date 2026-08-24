"""Server-side session store, backed by Postgres (FU-5 slice 5, ADR-019
§10 steps 4-5).

Ported from hris ``apps/api/src/api/services/session_service.py``. The
session id is an opaque ~256-bit URL-safe token (``secrets.token_urlsafe(32)``)
held as the httpOnly cookie value — never a guessable/short id, and never a
UUID (this repo's ``sessions.id`` is TEXT, unlike every other PK). Sliding
refresh: a resolve that lands within ``idle_refresh_seconds`` of expiry
extends the row by ``ttl_seconds``; anything further out is left untouched so
a live session isn't rewritten on every single request.
"""

from __future__ import annotations

import logging
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID

from src.schemas.auth import Session
from src.services import DbConn

logger = logging.getLogger(__name__)

_CREATE_SESSION_SQL = """
INSERT INTO sessions (id, user_id, expires_at, user_agent, ip_addr)
VALUES ($1, $2, $3, $4, $5::inet)
RETURNING id, user_id, expires_at, revoked_at, user_agent,
          ip_addr::text AS ip_addr, created_at
"""

_GET_ACTIVE_SESSION_SQL = """
SELECT id, user_id, expires_at, revoked_at, user_agent,
       ip_addr::text AS ip_addr, created_at
FROM sessions
WHERE id = $1 AND revoked_at IS NULL AND expires_at > now()
"""

_REFRESH_SESSION_SQL = "UPDATE sessions SET expires_at = $1 WHERE id = $2"

_REVOKE_SESSION_SQL = (
    "UPDATE sessions SET revoked_at = now() WHERE id = $1 AND revoked_at IS NULL"
)


async def create_session(
    conn: DbConn,
    *,
    user_id: UUID,
    ttl_seconds: int,
    user_agent: str | None = None,
    ip: str | None = None,
) -> Session:
    """Create a new session row and return it.

    ``id`` comes from ``secrets.token_urlsafe(32)`` — a real entropy source,
    never a weaker generator. ``expires_at`` is computed in Python as
    ``now() + ttl_seconds`` (a negative ``ttl_seconds`` yields an
    already-expired row, used by tests to exercise the expiry predicate).
    """
    session_id = secrets.token_urlsafe(32)
    expires_at = datetime.now(UTC) + timedelta(seconds=ttl_seconds)
    row = await conn.fetchrow(
        _CREATE_SESSION_SQL,
        session_id,
        user_id,
        expires_at,
        user_agent,
        ip,
    )
    assert row is not None
    logger.info(
        "session.created session_id=%s user_id=%s", session_id[:8] + "…", user_id
    )
    return Session(**dict(row))


async def get_active_session(conn: DbConn, session_id: str) -> Session | None:
    """Resolve a session id to a live (unrevoked, unexpired) ``Session``.

    Returns ``None`` for no matching row, a revoked session, or an expired
    one — the caller cannot distinguish those cases from this return value
    alone, which is deliberate (no timing/existence oracle for a stolen
    cookie).
    """
    row = await conn.fetchrow(_GET_ACTIVE_SESSION_SQL, session_id)
    return Session(**dict(row)) if row is not None else None


async def refresh_if_needed(
    conn: DbConn,
    session: Session,
    *,
    ttl_seconds: int,
    idle_refresh_seconds: int,
) -> Session:
    """Sliding-window refresh.

    If ``session`` is still more than ``idle_refresh_seconds`` from expiry,
    returns it unchanged WITHOUT touching the database — this is the
    common case on every request and must not turn into a write on each
    one. Otherwise extends ``expires_at`` to ``now() + ttl_seconds`` both in
    Postgres and on the returned object.
    """
    now = datetime.now(UTC)
    if (session.expires_at - now).total_seconds() > idle_refresh_seconds:
        return session
    new_expiry = now + timedelta(seconds=ttl_seconds)
    await conn.execute(_REFRESH_SESSION_SQL, new_expiry, session.id)
    return session.model_copy(update={"expires_at": new_expiry})


async def revoke_session(conn: DbConn, session_id: str) -> None:
    """Mark a session revoked. Idempotent — a second revoke is a no-op."""
    await conn.execute(_REVOKE_SESSION_SQL, session_id)
    logger.info("session.revoked session_id=%s", session_id[:8] + "…")
