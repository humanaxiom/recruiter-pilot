"""Integration tests — ``users`` and ``audit_log`` (ADR-019, FU-5 slice 1)
against a REAL Postgres (testcontainers).

Schema-only slice: ``src/models/ddl.py`` is NOT yet implemented for these two
tables, so every test below is expected to FAIL (RED) until that lands. These
tests prove, against a live database, what ``tests/unit/test_ddl.py``'s
string-matching can only assert about the SQL text:

* ``init_schema`` stays idempotent with the two new tables added,
* ``users.cas_username`` uniqueness is enforced by the database, not just by
  application discipline,
* ``audit_log``'s actor-identity CHECK (ADR-019 §1.4/§6) rejects both
  malformed shapes (both actors set, neither actor set) and accepts both
  valid single-actor shapes (human XOR service),
* ``users.role`` is NULLABLE with NO default (ADR-019 §10a — a
  human-directed reversal of this same ADR's §2 step 3, which originally had
  the DDL default a first login's row to ``recruiter``). FU-5 slice 1 is the
  schema half only, so the ``_insert_user`` helper below still passes
  ``role='recruiter'`` as an explicit Python kwarg, and Postgres itself
  still accepts each of the four known role values (ADR-019 §4 — the DB does
  NOT constrain role to the vocabulary; that is application-enforced).
"""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator, Iterator

import asyncpg
import pytest
from testcontainers.postgres import PostgresContainer

from src.models.ddl import init_schema

ROLE_VALUES: tuple[str, ...] = ("admin", "recruiter", "hiring_manager", "auditor")

INSERT_USER = """
INSERT INTO users (cas_username, display_name, email, role)
VALUES ($1, $2, $3, $4)
RETURNING id
"""

INSERT_AUDIT_LOG_USER_ACTOR = """
INSERT INTO audit_log (
    actor_kind, actor_user_id, actor_service,
    action, subject_type, subject_id
) VALUES ('user', $1, NULL, $2, $3, $4)
RETURNING id
"""

INSERT_AUDIT_LOG_SERVICE_ACTOR = """
INSERT INTO audit_log (
    actor_kind, actor_user_id, actor_service,
    action, subject_type, subject_id
) VALUES ('service', NULL, $1, $2, $3, $4)
RETURNING id
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
    await connection.execute("TRUNCATE users, audit_log, jobs, outbox CASCADE")
    try:
        yield connection
    finally:
        await connection.close()


async def _insert_user(
    conn: asyncpg.Connection,
    cas_username: str | None = None,
    role: str = "recruiter",
) -> uuid.UUID:
    user_id: uuid.UUID = await conn.fetchval(
        INSERT_USER,
        cas_username or f"user-{uuid.uuid4().hex}",
        "Ada Lovelace",
        "ada@example.org",
        role,
    )
    return user_id


# ── Idempotency, for real ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_init_schema_twice_in_a_row_succeeds_with_users_and_audit_log(
    pg_dsn: str,
) -> None:
    """``init_schema`` re-runs on every boot (no migration framework) — the
    two new tables must not break that guarantee."""
    connection = await asyncpg.connect(pg_dsn)
    try:
        await init_schema(connection)
        await init_schema(connection)
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_users_and_audit_log_tables_exist(conn: asyncpg.Connection) -> None:
    rows = await conn.fetch(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
    )
    tables = {row["tablename"] for row in rows}
    assert "users" in tables
    assert "audit_log" in tables


@pytest.mark.asyncio
async def test_init_schema_does_not_duplicate_the_actor_identity_check(
    pg_dsn: str,
) -> None:
    """Re-running init_schema must not create a second copy of the CHECK
    constraint (that would make ``ALTER TABLE ... DROP CONSTRAINT`` and any
    future re-add ambiguous). Guards against a non-idempotent CHECK add."""
    connection = await asyncpg.connect(pg_dsn)
    try:
        await init_schema(connection)
        await init_schema(connection)
        count = await connection.fetchval("""
            SELECT count(*) FROM information_schema.check_constraints
            WHERE constraint_name = 'audit_log_actor_identity'
            """)
        assert count == 1
    finally:
        await connection.close()


# ── users.cas_username uniqueness ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_cas_username_uniqueness_is_enforced(conn: asyncpg.Connection) -> None:
    username = f"netid-{uuid.uuid4().hex}"
    await _insert_user(conn, cas_username=username)

    with pytest.raises(asyncpg.UniqueViolationError):
        await _insert_user(conn, cas_username=username)


@pytest.mark.asyncio
async def test_distinct_cas_usernames_both_succeed(conn: asyncpg.Connection) -> None:
    first = await _insert_user(conn, cas_username=f"netid-{uuid.uuid4().hex}")
    second = await _insert_user(conn, cas_username=f"netid-{uuid.uuid4().hex}")
    assert first != second


# ── users.role defaults / vocabulary ────────────────────────────────────────


@pytest.mark.asyncio
async def test_role_is_null_when_omitted_on_insert(
    conn: asyncpg.Connection,
) -> None:
    """ADR-019 §10a — a human-directed reversal of §2 step 3 ('creates a new
    row with role=recruiter by default'). REWRITE of this test's prior
    version (``test_role_defaults_to_recruiter_on_first_login``), which
    asserted the now-superseded default: first CAS login must be able to
    capture a user with NO role, so omitting ``role`` entirely on INSERT must
    now yield NULL, not 'recruiter'. This is schema-only (FU-5 slice 1) — the
    Python provisioning code that actually runs on first login does not flip
    to omitting role until slice 2; this test proves the column itself no
    longer forces a default so slice 2 can rely on it."""
    user_id: uuid.UUID = await conn.fetchval(
        """
        INSERT INTO users (cas_username, display_name, email)
        VALUES ($1, $2, $3)
        RETURNING id
        """,
        f"netid-{uuid.uuid4().hex}",
        "Ada Lovelace",
        "ada@example.org",
    )
    role = await conn.fetchval("SELECT role FROM users WHERE id = $1", user_id)
    assert role is None


@pytest.mark.asyncio
async def test_users_role_column_is_nullable_per_information_schema(
    conn: asyncpg.Connection,
) -> None:
    """The load-bearing proof ``tests/unit/test_ddl.py`` cannot give: that
    Postgres itself, not just the DDL source text, considers the live
    ``users.role`` column nullable after ``init_schema`` has run."""
    is_nullable = await conn.fetchval("""
        SELECT is_nullable FROM information_schema.columns
        WHERE table_name = 'users' AND column_name = 'role'
        """)
    assert is_nullable == "YES"


@pytest.mark.asyncio
async def test_users_role_has_no_column_default_per_information_schema(
    conn: asyncpg.Connection,
) -> None:
    """Companion to the nullability check above: the DROP DEFAULT ALTER must
    actually clear ``pg_attrdef``, not just the NOT NULL flag."""
    column_default = await conn.fetchval("""
        SELECT column_default FROM information_schema.columns
        WHERE table_name = 'users' AND column_name = 'role'
        """)
    assert column_default is None


@pytest.mark.asyncio
async def test_init_schema_twice_keeps_role_nullable(pg_dsn: str) -> None:
    """ADR-019 §10a: on a volume that has already had ``init_schema`` run
    once (this slice's own relaxation), running it again — the ordinary boot
    sequence, no migration framework — must not error and must not silently
    re-add the DEFAULT/NOT NULL the first run dropped."""
    connection = await asyncpg.connect(pg_dsn)
    try:
        await init_schema(connection)
        await init_schema(connection)
        is_nullable = await connection.fetchval("""
            SELECT is_nullable FROM information_schema.columns
            WHERE table_name = 'users' AND column_name = 'role'
            """)
        assert is_nullable == "YES"
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_explicit_null_role_insert_succeeds(
    conn: asyncpg.Connection,
) -> None:
    """An explicit NULL must not trip a NOT NULL violation now that the
    column is nullable — the fail-closed gate that reads this NULL is a
    later slice (slice 3); this slice only proves NULL is storable at all."""
    user_id: uuid.UUID = await conn.fetchval(
        """
        INSERT INTO users (cas_username, display_name, email, role)
        VALUES ($1, $2, $3, NULL)
        RETURNING id
        """,
        f"netid-{uuid.uuid4().hex}",
        "Ada Lovelace",
        "ada@example.org",
    )
    role = await conn.fetchval("SELECT role FROM users WHERE id = $1", user_id)
    assert role is None


@pytest.mark.asyncio
async def test_existing_explicit_roles_survive_the_relaxation(
    conn: asyncpg.Connection,
) -> None:
    """``ALTER COLUMN role DROP DEFAULT`` / ``DROP NOT NULL`` touch only the
    column's metadata, never existing row data — an explicitly-set
    'recruiter'/'admin' role must still read back unchanged after
    ``init_schema`` (which re-runs those ALTERs idempotently on every boot)
    runs again over rows that already have a role."""
    recruiter_id = await _insert_user(conn, role="recruiter")
    admin_id = await _insert_user(conn, role="admin")

    await init_schema(conn)  # boot-time re-run; the ALTERs are now no-ops

    recruiter_role = await conn.fetchval(
        "SELECT role FROM users WHERE id = $1", recruiter_id
    )
    admin_role = await conn.fetchval("SELECT role FROM users WHERE id = $1", admin_id)
    assert recruiter_role == "recruiter"
    assert admin_role == "admin"


@pytest.mark.asyncio
async def test_users_active_defaults_true(conn: asyncpg.Connection) -> None:
    user_id = await _insert_user(conn)
    active = await conn.fetchval("SELECT active FROM users WHERE id = $1", user_id)
    assert active is True


@pytest.mark.parametrize("role", ROLE_VALUES)
@pytest.mark.asyncio
async def test_each_known_role_value_is_accepted(
    conn: asyncpg.Connection, role: str
) -> None:
    user_id = await _insert_user(conn, role=role)
    stored = await conn.fetchval("SELECT role FROM users WHERE id = $1", user_id)
    assert stored == role


@pytest.mark.asyncio
async def test_display_name_and_email_may_be_absent(
    conn: asyncpg.Connection,
) -> None:
    """Accepted residual — some CAS institutions release only username."""
    user_id: uuid.UUID = await conn.fetchval(
        "INSERT INTO users (cas_username, role) VALUES ($1, 'recruiter') RETURNING id",
        f"netid-{uuid.uuid4().hex}",
    )
    row = await conn.fetchrow(
        "SELECT display_name, email FROM users WHERE id = $1", user_id
    )
    assert row is not None
    assert row["display_name"] is None
    assert row["email"] is None


# ── audit_log actor-identity CHECK ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_actor_identity_check_rejects_both_actor_fields_set(
    conn: asyncpg.Connection,
) -> None:
    """Neither `actor_kind='user'` with `actor_service` also set, nor the
    reverse, may pass — exactly one actor per row."""
    user_id = await _insert_user(conn)
    with pytest.raises(asyncpg.CheckViolationError):
        await conn.execute(
            """
            INSERT INTO audit_log (
                actor_kind, actor_user_id, actor_service,
                action, subject_type, subject_id
            ) VALUES ('user', $1, 'worker', 'reveal', 'resume', $2)
            """,
            user_id,
            uuid.uuid4(),
        )


@pytest.mark.asyncio
async def test_actor_identity_check_rejects_neither_actor_field_set(
    conn: asyncpg.Connection,
) -> None:
    """A row naming no actor at all must be rejected — every audited action
    must be attributable to a human or a service, never anonymous."""
    with pytest.raises(asyncpg.CheckViolationError):
        await conn.execute(
            """
            INSERT INTO audit_log (
                actor_kind, actor_user_id, actor_service,
                action, subject_type, subject_id
            ) VALUES ('user', NULL, NULL, 'reveal', 'resume', $1)
            """,
            uuid.uuid4(),
        )


@pytest.mark.asyncio
async def test_actor_identity_check_rejects_service_kind_with_user_id_set(
    conn: asyncpg.Connection,
) -> None:
    user_id = await _insert_user(conn)
    with pytest.raises(asyncpg.CheckViolationError):
        await conn.execute(
            """
            INSERT INTO audit_log (
                actor_kind, actor_user_id, actor_service,
                action, subject_type, subject_id
            ) VALUES ('service', $1, 'worker', 'reveal', 'resume', $2)
            """,
            user_id,
            uuid.uuid4(),
        )


@pytest.mark.asyncio
async def test_actor_identity_check_accepts_valid_human_actor_shape(
    conn: asyncpg.Connection,
) -> None:
    """actor_kind='user', actor_user_id set, actor_service NULL — valid."""
    user_id = await _insert_user(conn)
    row_id = await conn.fetchval(
        INSERT_AUDIT_LOG_USER_ACTOR,
        user_id,
        "reveal",
        "resume",
        uuid.uuid4(),
    )
    assert row_id is not None

    row = await conn.fetchrow(
        "SELECT actor_kind, actor_user_id, actor_service FROM audit_log WHERE id = $1",
        row_id,
    )
    assert row is not None
    assert row["actor_kind"] == "user"
    assert row["actor_user_id"] == user_id
    assert row["actor_service"] is None


@pytest.mark.asyncio
async def test_actor_identity_check_accepts_valid_service_actor_shape(
    conn: asyncpg.Connection,
) -> None:
    """actor_kind='service', actor_user_id NULL, actor_service set — valid.
    This is how a worker background job (blind_review flip, role change)
    is expected to record itself: no human present, no false attribution."""
    row_id = await conn.fetchval(
        INSERT_AUDIT_LOG_SERVICE_ACTOR,
        "worker",
        "blind_review_toggled",
        "job",
        uuid.uuid4(),
    )
    assert row_id is not None

    row = await conn.fetchrow(
        "SELECT actor_kind, actor_user_id, actor_service FROM audit_log WHERE id = $1",
        row_id,
    )
    assert row is not None
    assert row["actor_kind"] == "service"
    assert row["actor_user_id"] is None
    assert row["actor_service"] == "worker"


@pytest.mark.asyncio
async def test_audit_log_records_a_blind_review_flip(
    conn: asyncpg.Connection,
) -> None:
    """ADR-019 §1.4 — the generalized table must be able to record a
    blind_review toggle, not just a reveal, keyed to the job as subject."""
    user_id = await _insert_user(conn)
    job_id: uuid.UUID = await conn.fetchval(
        "INSERT INTO jobs (title, description_raw) VALUES ($1, $2) RETURNING id",
        "Senior Python Engineer",
        "Build the ranking pipeline.",
    )
    row_id = await conn.fetchval(
        """
        INSERT INTO audit_log (
            actor_kind, actor_user_id, actor_service,
            action, subject_type, subject_id, job_id, details
        ) VALUES (
            'user', $1, NULL, 'blind_review_toggled', 'job', $2, $2,
            '{"old_value": true, "new_value": false}'
        )
        RETURNING id
        """,
        user_id,
        job_id,
    )
    assert row_id is not None


@pytest.mark.asyncio
async def test_audit_log_details_round_trips_jsonb(conn: asyncpg.Connection) -> None:
    import json

    row_id = await conn.fetchval(
        """
        INSERT INTO audit_log (
            actor_kind, actor_user_id, actor_service,
            action, subject_type, subject_id, details
        ) VALUES (
            'service', NULL, 'worker', 'role_changed', 'user', $1,
            '{"old_role": "recruiter", "new_role": "admin"}'
        )
        RETURNING id
        """,
        uuid.uuid4(),
    )
    stored = await conn.fetchval("SELECT details FROM audit_log WHERE id = $1", row_id)
    assert json.loads(stored) == {"old_role": "recruiter", "new_role": "admin"}
