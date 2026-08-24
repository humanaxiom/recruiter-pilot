"""Integration tests — ``session_service`` + ``user_service`` (FU-5 slice 5,
ADR-019 §10 steps 3-5, §10a) against a REAL Postgres (testcontainers).

Ports the behaviour proven by hris's
``C:\\repos\\hris\\tests\\integration\\test_auth_postgres.py`` (idempotency,
concurrent-first-login race collapsing to one row, session lifecycle) onto
THIS repo's schema — which differs from hris in two load-bearing ways that
only a real database can prove:

* ``role`` is a plain column on ``users`` (no ``user_roles`` join table), so
  ``provision_or_get`` must write the role directly on INSERT and must NOT
  touch it on the ``ON CONFLICT`` (second-login) path — a manually-changed
  role must survive a re-login.
* ADR-019 §10a's default-admin allowlist: on a user's FIRST login only, a
  ``cas_username`` matching ``settings.default_admin_cas_username`` is
  provisioned ``role='admin'`` instead of the non-default-admin default.
  A SECOND login by that same username must NOT re-promote a role that was
  since changed.

**user-admin-roles slice 2 — human-directed reversal of ADR-019 §10a/§2
(2026-07-25).** A first CAS login for a username other than the configured
default-admin now captures NO role (``role IS NULL``), not the old
``'recruiter'`` default recorded in §10a/§2. The default-admin allowlist
itself (``asalah`` → ``'admin'`` on first login) is UNCHANGED, and role
stickiness on the ``ON CONFLICT`` path (a manually-set role survives
re-login) is UNCHANGED. Tests below that previously asserted
``role == "recruiter"`` for a non-default first login are rewritten to
assert ``role IS NULL`` — this is the authorized reversal target the
coordinator cited, not a "modify tests to pass" violation. Slice 3 adds the
fail-closed gate that rejects a no-role user from doing anything; a no-role
user existing but not yet blocked is this slice's intentional, known
intermediate state.

Neither of these is provable by mocking a connection — the whole point is
the ``INSERT ... ON CONFLICT (cas_username) DO UPDATE`` semantics, which only
a real Postgres enforces. Nothing here exists yet
(``core/src/services/session_service.py``, ``core/src/services/user_service.py``,
``core/src/schemas/auth.py``), so every test below is expected to FAIL (RED)
— most as a ``ModuleNotFoundError``/``ImportError`` at collection time.

Follows the exact asyncpg/testcontainers fixture wiring already used in
``tests/integration/test_sessions_pg.py`` and ``test_users_audit_pg.py`` — no
new harness.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime

import asyncpg
import pytest
from testcontainers.postgres import PostgresContainer

from src.models.ddl import init_schema
from src.schemas.auth import User
from src.services import session_service, user_service

DEFAULT_ADMIN = "asalah"


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


def _unique_username(prefix: str = "netid") -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


async def _provision(
    conn: asyncpg.Connection, cas_username: str, *, default_admin: str = DEFAULT_ADMIN
) -> User:
    return await user_service.provision_or_get(
        conn, cas_username=cas_username, default_admin_cas_username=default_admin
    )


# ── user_service.provision_or_get — first login ────────────────────────────


@pytest.mark.asyncio
async def test_provision_or_get_first_call_creates_row_with_no_role(
    conn: asyncpg.Connection,
) -> None:
    """user-admin-roles slice 2 (ADR-019 §10a/§2 reversal, human directive
    2026-07-25): a first CAS login for a non-default-admin username now
    captures NO role, in the real row — previously ``'recruiter'``,
    superseded here."""
    username = _unique_username("alice")
    user = await _provision(conn, username)
    assert user.cas_username == username
    assert user.role is None
    assert user.active is True

    stored_role = await conn.fetchval(
        "SELECT role FROM users WHERE cas_username = $1", username
    )
    assert stored_role is None


@pytest.mark.asyncio
async def test_provision_or_get_first_call_for_default_admin_creates_admin_role(
    conn: asyncpg.Connection,
) -> None:
    """ADR-019 §10a — the default-admin allowlist. First login by the
    configured ``default_admin_cas_username`` is provisioned as admin.
    UNCHANGED by the slice-2 reversal."""
    user = await _provision(conn, DEFAULT_ADMIN)
    assert user.cas_username == DEFAULT_ADMIN
    assert user.role == "admin"

    stored_role = await conn.fetchval(
        "SELECT role FROM users WHERE cas_username = $1", DEFAULT_ADMIN
    )
    assert stored_role == "admin"


# ── user_service.provision_or_get — second login, role stickiness ──────────


@pytest.mark.asyncio
async def test_provision_or_get_second_call_bumps_last_seen_but_not_role(
    conn: asyncpg.Connection,
) -> None:
    """Second call for an existing user must refresh ``last_seen_at`` and
    must NOT reset a role that was manually changed after first login — the
    ``ON CONFLICT`` path in this schema updates last_seen_at only.

    ``first.role`` is asserted ``is None`` (not ``"recruiter"``) per the
    slice-2 reversal — this user's first login captures no role at all."""
    username = _unique_username("carol")
    first = await _provision(conn, username)
    assert first.role is None

    # Manually promote — simulates an admin's out-of-band role change.
    await conn.execute("UPDATE users SET role = 'admin' WHERE id = $1", first.id)

    await asyncio.sleep(0.05)  # ensure a distinguishable last_seen_at tick

    second = await _provision(conn, username)
    assert second.id == first.id
    assert second.role == "admin", "manual role change must survive re-login"
    assert second.last_seen_at >= first.last_seen_at
    assert (
        second.last_seen_at > first.last_seen_at
    ), "second login must bump last_seen_at"


@pytest.mark.asyncio
async def test_provision_or_get_second_call_for_no_role_user_stays_null(
    conn: asyncpg.Connection,
) -> None:
    """user-admin-roles slice 2 — a SECOND login of a user whose first login
    captured no role must NOT gain a role from anywhere (no default is ever
    applied on the ``ON CONFLICT`` path); the row stays ``role IS NULL``
    unless an admin explicitly sets it (a later slice's concern)."""
    username = _unique_username("nolan")
    first = await _provision(conn, username)
    assert first.role is None

    await asyncio.sleep(0.05)

    second = await _provision(conn, username)
    assert second.id == first.id
    assert second.role is None, "a no-role user must stay role-less across logins"
    assert second.last_seen_at > first.last_seen_at


@pytest.mark.asyncio
async def test_default_admin_second_login_does_not_repromote_after_demotion(
    conn: asyncpg.Connection,
) -> None:
    """The default-admin path only fires on row CREATION. A later manual
    demotion of the default-admin's own account must stick across logins.
    UNAFFECTED by the slice-2 reversal (this test sets role via an explicit
    UPDATE, not the provisioning default)."""
    first = await _provision(conn, DEFAULT_ADMIN)
    assert first.role == "admin"

    await conn.execute("UPDATE users SET role = 'recruiter' WHERE id = $1", first.id)

    second = await _provision(conn, DEFAULT_ADMIN)
    assert second.id == first.id
    assert (
        second.role == "recruiter"
    ), "a demoted default-admin must not be silently re-promoted on re-login"


# ── user_service.provision_or_get — concurrent first login (the race) ──────


@pytest.mark.asyncio
async def test_concurrent_first_logins_for_same_cas_username_collapse_to_one_row(
    pg_dsn: str,
) -> None:
    """Two (in fact four) parallel first-login provisions for the same
    unknown username must all succeed and resolve to exactly one row — the
    ``INSERT ... ON CONFLICT`` path, ported from hris's race test."""
    username = _unique_username("race")
    async with asyncpg.create_pool(pg_dsn, min_size=4, max_size=4) as pool:
        async with pool.acquire() as c:
            await init_schema(c)
            await c.execute("DELETE FROM users WHERE cas_username = $1", username)

        async def call() -> uuid.UUID:
            async with pool.acquire() as c:
                u = await _provision(c, username)
                return u.id

        results = await asyncio.gather(*(call() for _ in range(4)))
        assert (
            len({str(r) for r in results}) == 1
        ), f"expected one user id, got {results}"

        async with pool.acquire() as c:
            count = await c.fetchval(
                "SELECT count(*) FROM users WHERE cas_username = $1", username
            )
        assert count == 1


# ── session_service — create / get_active_session round trip ───────────────


@pytest.mark.asyncio
async def test_session_create_and_get_active_session_round_trip(
    conn: asyncpg.Connection,
) -> None:
    user = await _provision(conn, _unique_username("dan"))
    session = await session_service.create_session(
        conn, user_id=user.id, ttl_seconds=3600, user_agent="pytest", ip="127.0.0.1"
    )
    assert session.user_id == user.id
    assert session.revoked_at is None

    got = await session_service.get_active_session(conn, session.id)
    assert got is not None
    assert got.id == session.id
    assert got.user_id == user.id


@pytest.mark.asyncio
async def test_get_active_session_returns_none_for_a_revoked_session(
    conn: asyncpg.Connection,
) -> None:
    user = await _provision(conn, _unique_username("eve"))
    session = await session_service.create_session(
        conn, user_id=user.id, ttl_seconds=3600, user_agent=None, ip=None
    )

    await session_service.revoke_session(conn, session.id)

    revoked_at = await conn.fetchval(
        "SELECT revoked_at FROM sessions WHERE id = $1", session.id
    )
    assert revoked_at is not None

    got = await session_service.get_active_session(conn, session.id)
    assert got is None


@pytest.mark.asyncio
async def test_get_active_session_returns_none_for_an_expired_session(
    conn: asyncpg.Connection,
) -> None:
    user = await _provision(conn, _unique_username("frank"))
    # A negative TTL yields an expires_at already in the past.
    expired = await session_service.create_session(
        conn, user_id=user.id, ttl_seconds=-10, user_agent=None, ip=None
    )
    stored_expiry: datetime = await conn.fetchval(
        "SELECT expires_at FROM sessions WHERE id = $1", expired.id
    )
    assert stored_expiry < datetime.now(UTC)

    got = await session_service.get_active_session(conn, expired.id)
    assert got is None


# ── session_service.refresh_if_needed — sliding window ─────────────────────


@pytest.mark.asyncio
async def test_refresh_if_needed_extends_only_within_the_idle_window(
    conn: asyncpg.Connection,
) -> None:
    user = await _provision(conn, _unique_username("grace"))

    # Plenty of headroom (ttl=3600s vs idle_refresh_seconds=60) — must NOT move.
    fresh = await session_service.create_session(
        conn, user_id=user.id, ttl_seconds=3600, user_agent=None, ip=None
    )
    not_refreshed = await session_service.refresh_if_needed(
        conn, fresh, ttl_seconds=3600, idle_refresh_seconds=60
    )
    assert not_refreshed.expires_at == fresh.expires_at
    stored = await conn.fetchval(
        "SELECT expires_at FROM sessions WHERE id = $1", fresh.id
    )
    assert stored == fresh.expires_at

    # Already inside the idle-refresh window (ttl=10s vs idle=60s) — must extend.
    near = await session_service.create_session(
        conn, user_id=user.id, ttl_seconds=10, user_agent=None, ip=None
    )
    refreshed = await session_service.refresh_if_needed(
        conn, near, ttl_seconds=3600, idle_refresh_seconds=60
    )
    assert refreshed.expires_at > near.expires_at
    stored_after = await conn.fetchval(
        "SELECT expires_at FROM sessions WHERE id = $1", near.id
    )
    assert stored_after == refreshed.expires_at


# ── FK cascade — deleting a user removes their sessions (slice 3 FK) ───────


@pytest.mark.asyncio
async def test_deleting_a_provisioned_user_cascades_to_their_sessions(
    conn: asyncpg.Connection,
) -> None:
    """The revocation-on-deprovision behaviour hris relied on (ADR-019 §10
    step 4), exercised end-to-end through both new services rather than raw
    SQL against the FK directly (already proven at the schema level by
    ``test_sessions_pg.py``)."""
    user = await _provision(conn, _unique_username("heidi"))
    session = await session_service.create_session(
        conn, user_id=user.id, ttl_seconds=3600, user_agent=None, ip=None
    )

    await conn.execute("DELETE FROM users WHERE id = $1", user.id)

    remaining = await conn.fetchval(
        "SELECT count(*) FROM sessions WHERE user_id = $1", user.id
    )
    assert remaining == 0

    got = await session_service.get_active_session(conn, session.id)
    assert got is None
