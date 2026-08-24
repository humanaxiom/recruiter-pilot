"""Integration tests — the ``sessions`` table (ADR-019 §10 step 4, FU-5
slice 3) against a REAL Postgres (testcontainers).

Schema-only slice: ``src/models/ddl.py`` does not declare ``sessions`` yet,
so every test below is expected to FAIL (RED) until that lands — most as an
``asyncpg.UndefinedTableError`` (or the same surfacing through the ``conn``
fixture's ``TRUNCATE``), since the table does not exist at all.

These tests prove, against a live database, what
``tests/unit/test_ddl.py``'s string-matching structurally cannot:

* ``init_schema`` stays idempotent with ``sessions`` added,
* a ``sessions`` row REQUIRES a real ``users`` row — the FK is enforced by
  Postgres, not just declared in SQL text,
* ``ON DELETE CASCADE`` really deletes a user's sessions when the user row is
  deleted (the specific behaviour hris relied on for revocation-on-
  deprovision — a string-match test cannot observe a CASCADE firing),
* the ``id`` PRIMARY KEY really rejects a duplicate session id,
* ``ip_addr`` is really the Postgres ``INET`` type, not ``TEXT`` — it accepts
  a valid address (and the driver hands back a non-``str`` ``ipaddress``
  object, not a plain string) and rejects a non-IP string.

Follows the exact asyncpg/testcontainers fixture wiring already used in
``tests/integration/test_users_audit_pg.py`` — no new harness.
"""

from __future__ import annotations

import re
import secrets
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest
from testcontainers.postgres import PostgresContainer

from src.models.ddl import init_schema

INSERT_USER = """
INSERT INTO users (cas_username, display_name, email, role)
VALUES ($1, $2, $3, 'recruiter')
RETURNING id
"""

INSERT_SESSION = """
INSERT INTO sessions (id, user_id, expires_at)
VALUES ($1, $2, $3)
"""


@pytest.fixture(scope="session")
def pg_dsn() -> Iterator[str]:
    with PostgresContainer("postgres:16-alpine") as pg:
        # asyncpg needs the raw DSN, not SQLAlchemy's postgresql+driver:// form.
        yield re.sub(r"\+\w+", "", pg.get_connection_url(), count=1)


@pytest.fixture
async def conn(pg_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    connection: asyncpg.Connection = await asyncpg.connect(pg_dsn)
    await init_schema(connection)
    await connection.execute(
        "TRUNCATE sessions, users, audit_log, jobs, outbox CASCADE"
    )
    try:
        yield connection
    finally:
        await connection.close()


def _session_id() -> str:
    """Same shape the app will really use: secrets.token_urlsafe(32)."""
    return secrets.token_urlsafe(32)


def _future(hours: int = 8) -> datetime:
    return datetime.now(UTC) + timedelta(hours=hours)


async def _insert_user(
    conn: asyncpg.Connection, cas_username: str | None = None
) -> uuid.UUID:
    user_id: uuid.UUID = await conn.fetchval(
        INSERT_USER,
        cas_username or f"user-{uuid.uuid4().hex}",
        "Ada Lovelace",
        "ada@example.org",
    )
    return user_id


# ── Idempotency, for real ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_init_schema_twice_in_a_row_succeeds_with_sessions(
    pg_dsn: str,
) -> None:
    """``init_schema`` re-runs on every boot (no migration framework) — the
    new table must not break that guarantee."""
    connection = await asyncpg.connect(pg_dsn)
    try:
        await init_schema(connection)
        await init_schema(connection)
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_sessions_table_exists(conn: asyncpg.Connection) -> None:
    rows = await conn.fetch(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
    )
    tables = {row["tablename"] for row in rows}
    assert "sessions" in tables


@pytest.mark.asyncio
async def test_sessions_indexes_exist(conn: asyncpg.Connection) -> None:
    rows = await conn.fetch("""
        SELECT indexname FROM pg_indexes
         WHERE schemaname = 'public' AND tablename = 'sessions'
        """)
    names = {row["indexname"] for row in rows}
    assert "sessions_user_id_idx" in names
    assert "sessions_active_idx" in names


@pytest.mark.asyncio
async def test_sessions_active_idx_is_a_partial_index_on_unrevoked_sessions(
    conn: asyncpg.Connection,
) -> None:
    """Proves the partial predicate was actually accepted and stored by
    Postgres — a source string-match cannot see the catalog's own rendering
    of the WHERE clause survive a real CREATE INDEX."""
    indexdef = await conn.fetchval("""
        SELECT indexdef FROM pg_indexes
         WHERE schemaname = 'public' AND indexname = 'sessions_active_idx'
        """)
    assert indexdef is not None
    assert "revoked_at" in indexdef.lower()
    assert "is null" in indexdef.lower()


# ── FK: a session requires a real user ──────────────────────────────────────


@pytest.mark.asyncio
async def test_session_insert_requires_an_existing_user(
    conn: asyncpg.Connection,
) -> None:
    """A session for a user_id with no matching users row must be rejected
    by the database FK, not merely by application discipline."""
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await conn.execute(
            INSERT_SESSION,
            _session_id(),
            uuid.uuid4(),  # no such user
            _future(),
        )


@pytest.mark.asyncio
async def test_session_insert_succeeds_for_a_real_user(
    conn: asyncpg.Connection,
) -> None:
    user_id = await _insert_user(conn)
    session_id = _session_id()
    await conn.execute(INSERT_SESSION, session_id, user_id, _future())

    stored = await conn.fetchval(
        "SELECT user_id FROM sessions WHERE id = $1", session_id
    )
    assert stored == user_id


# ── ON DELETE CASCADE — the deprovisioning behaviour hris relied on ────────


@pytest.mark.asyncio
async def test_deleting_a_user_cascades_to_their_sessions(
    conn: asyncpg.Connection,
) -> None:
    """The specific behaviour hris relied on for revocation-on-deprovision
    (ADR-019 §10 step 4). Unprovable by string-matching the DDL — only a
    real DELETE against a real FK proves the CASCADE actually fires."""
    user_id = await _insert_user(conn)
    session_id = _session_id()
    await conn.execute(INSERT_SESSION, session_id, user_id, _future())

    await conn.execute("DELETE FROM users WHERE id = $1", user_id)

    remaining = await conn.fetchval(
        "SELECT count(*) FROM sessions WHERE user_id = $1", user_id
    )
    assert remaining == 0


@pytest.mark.asyncio
async def test_deleting_an_unrelated_user_does_not_touch_other_sessions(
    conn: asyncpg.Connection,
) -> None:
    kept_user = await _insert_user(conn)
    kept_session = _session_id()
    await conn.execute(INSERT_SESSION, kept_session, kept_user, _future())

    doomed_user = await _insert_user(conn)
    await conn.execute(INSERT_SESSION, _session_id(), doomed_user, _future())

    await conn.execute("DELETE FROM users WHERE id = $1", doomed_user)

    survivor = await conn.fetchval(
        "SELECT id FROM sessions WHERE id = $1", kept_session
    )
    assert survivor == kept_session


# ── PRIMARY KEY uniqueness ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_duplicate_session_id_is_rejected(conn: asyncpg.Connection) -> None:
    user_id = await _insert_user(conn)
    session_id = _session_id()
    await conn.execute(INSERT_SESSION, session_id, user_id, _future())

    with pytest.raises(asyncpg.UniqueViolationError):
        await conn.execute(INSERT_SESSION, session_id, user_id, _future())


# ── ip_addr is a real INET column, not TEXT ─────────────────────────────────


@pytest.mark.asyncio
async def test_ip_addr_accepts_a_valid_ip_and_is_not_a_plain_string(
    conn: asyncpg.Connection,
) -> None:
    """A TEXT column would silently accept this and hand back a plain
    ``str``; asyncpg's INET codec hands back a real ``ipaddress`` object
    instead. That type difference is proof the column is INET — unprovable
    by string-matching the DDL source."""
    user_id = await _insert_user(conn)
    session_id = _session_id()
    await conn.execute(
        "INSERT INTO sessions (id, user_id, expires_at, ip_addr) "
        "VALUES ($1, $2, $3, $4)",
        session_id,
        user_id,
        _future(),
        "192.168.1.1",
    )

    stored = await conn.fetchval(
        "SELECT ip_addr FROM sessions WHERE id = $1", session_id
    )
    assert not isinstance(stored, str)
    assert str(stored) == "192.168.1.1"


@pytest.mark.asyncio
async def test_ip_addr_rejects_a_non_ip_string(conn: asyncpg.Connection) -> None:
    """A malformed address must never reach storage. asyncpg's INET codec
    validates client-side via the stdlib ``ipaddress`` module (raising
    ``ValueError`` before a byte is sent) or Postgres itself rejects it
    server-side (``asyncpg.PostgresError``); either is acceptable proof this
    is really INET, not TEXT — a TEXT column would accept this value."""
    user_id = await _insert_user(conn)
    with pytest.raises((ValueError, asyncpg.PostgresError)):
        await conn.execute(
            "INSERT INTO sessions (id, user_id, expires_at, ip_addr) "
            "VALUES ($1, $2, $3, $4)",
            _session_id(),
            user_id,
            _future(),
            "definitely-not-an-ip-address",
        )


# ── revoked_at / expires_at / created_at ────────────────────────────────────


@pytest.mark.asyncio
async def test_revoked_at_is_null_for_a_fresh_session(
    conn: asyncpg.Connection,
) -> None:
    user_id = await _insert_user(conn)
    session_id = _session_id()
    await conn.execute(INSERT_SESSION, session_id, user_id, _future())

    revoked = await conn.fetchval(
        "SELECT revoked_at FROM sessions WHERE id = $1", session_id
    )
    assert revoked is None


@pytest.mark.asyncio
async def test_expires_at_is_required(conn: asyncpg.Connection) -> None:
    user_id = await _insert_user(conn)
    with pytest.raises(asyncpg.NotNullViolationError):
        await conn.execute(
            "INSERT INTO sessions (id, user_id) VALUES ($1, $2)",
            _session_id(),
            user_id,
        )


@pytest.mark.asyncio
async def test_created_at_defaults_to_now(conn: asyncpg.Connection) -> None:
    user_id = await _insert_user(conn)
    session_id = _session_id()
    before = datetime.now(UTC)
    await conn.execute(INSERT_SESSION, session_id, user_id, _future())
    after = datetime.now(UTC)

    created_at = await conn.fetchval(
        "SELECT created_at FROM sessions WHERE id = $1", session_id
    )
    assert created_at is not None
    assert before <= created_at <= after


@pytest.mark.asyncio
async def test_a_session_can_be_marked_revoked(conn: asyncpg.Connection) -> None:
    """Revocation is a plain UPDATE — proves revoked_at is writable and the
    active-session partial index does not block it."""
    user_id = await _insert_user(conn)
    session_id = _session_id()
    await conn.execute(INSERT_SESSION, session_id, user_id, _future())

    await conn.execute(
        "UPDATE sessions SET revoked_at = now() WHERE id = $1", session_id
    )

    revoked = await conn.fetchval(
        "SELECT revoked_at FROM sessions WHERE id = $1", session_id
    )
    assert revoked is not None
