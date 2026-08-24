"""Integration tests — D1 = option C: the audited reveal of a withheld
``audit_log.details`` value, against a REAL Postgres (testcontainers).

**Why these cannot be unit tests.** CLAUDE.md's rule applies literally here —
every claim below is a property of the database, and the unit suite can only
string-match the query text:

* ``details`` is ``jsonb``. What asyncpg hands back for a round-tripped object
  (``str`` vs ``dict``) is driver behaviour, and the reveal read's decode guard
  is either correct against the real driver or it hands the auditor a quoted
  blob. Mocks return whatever the test author typed.
* the reveal writes ``actor_kind='user'`` with a real ``actor_user_id``, which
  is an **FK into ``users``** and is additionally covered by the
  ``audit_log_actor_identity`` CHECK. A wrong actor derivation fails at the
  database, not in Python — this is exactly the FU-5 slice-1 shape where a
  column definition passed 2764 unit tests and failed the first real INSERT.
* the reveal row must come back through ``list_audit_log`` with its
  ``revealed_action`` marker DISCLOSED, so the trail of reveals is readable
  without revealing it in turn. That composes the writer, the reader and the
  redaction allowlist through real SQL.

Harness is the established one (testcontainers Postgres + ``init_schema``),
matching ``test_audit_log_read_pg.py``. No new harness.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator, Iterator

import asyncpg
import pytest
from testcontainers.postgres import PostgresContainer

from src.models.ddl import init_schema
from src.services import audit_service

_REASON = "withdrawn after the hiring manager raised a concern about the reference"


@pytest.fixture(scope="session")
def pg_dsn() -> Iterator[str]:
    with PostgresContainer("postgres:16-alpine") as pg:
        yield re.sub(r"\+\w+", "", pg.get_connection_url(), count=1)


@pytest.fixture
async def pg_pool(pg_dsn: str) -> AsyncIterator[asyncpg.Pool]:
    pool = await asyncpg.create_pool(dsn=pg_dsn, min_size=2, max_size=8)
    await init_schema(pool)
    async with pool.acquire() as conn:
        await conn.execute("TRUNCATE users, audit_log, jobs, resumes CASCADE")
    try:
        yield pool
    finally:
        await pool.close()


async def _seed_user(pool: asyncpg.Pool, username: str, role: str) -> uuid.UUID:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO users (cas_username, display_name, role) "
            "VALUES ($1, $2, $3) RETURNING id",
            username,
            username,
            role,
        )
    assert row is not None
    return uuid.UUID(str(row["id"]))


async def _seed_withdrawal(pool: asyncpg.Pool, actor_id: uuid.UUID) -> uuid.UUID:
    """Write a real ``withdraw_resume`` event through the real writer."""
    subject_id = uuid.uuid4()
    async with pool.acquire() as conn:
        await audit_service.record_audit(
            conn,
            actor_kind="user",
            actor_user_id=actor_id,
            actor_service=None,
            action="withdraw_resume",
            subject_type="resume",
            subject_id=subject_id,
            details={"reason": _REASON},
        )
        row = await conn.fetchrow(
            "SELECT id FROM audit_log WHERE action = 'withdraw_resume' "
            "ORDER BY occurred_at DESC LIMIT 1"
        )
    assert row is not None
    return uuid.UUID(str(row["id"]))


async def test_the_reason_round_trips_through_real_jsonb_unredacted(
    pg_pool: asyncpg.Pool,
) -> None:
    """The prose someone typed comes back as the prose someone typed — not a
    quoted JSON string, not a ``dict`` the test author invented."""
    recruiter = await _seed_user(pg_pool, "rowan", "recruiter")
    audit_id = await _seed_withdrawal(pg_pool, recruiter)

    async with pg_pool.acquire() as conn:
        detail = await audit_service.read_audit_detail(conn, audit_id=audit_id)

    assert detail is not None
    assert detail.action == "withdraw_resume"
    assert isinstance(
        detail.details, dict
    ), f"jsonb decode guard failed: got {type(detail.details).__name__}"
    assert detail.details["reason"] == _REASON


async def test_the_same_row_is_withheld_on_the_ordinary_list_read(
    pg_pool: asyncpg.Pool,
) -> None:
    """The two reads must disagree — that disagreement IS the feature. If the
    list read ever discloses the reason, option C has silently become option
    B and the audited reveal is recording a restriction that no longer exists.
    """
    recruiter = await _seed_user(pg_pool, "rowan", "recruiter")
    await _seed_withdrawal(pg_pool, recruiter)

    async with pg_pool.acquire() as conn:
        items = await audit_service.list_audit_log(conn, action="withdraw_resume")

    assert len(items) == 1
    assert items[0].details == {"reason": audit_service.WITHHELD}


async def test_a_missing_row_returns_none_against_a_real_database(
    pg_pool: asyncpg.Pool,
) -> None:
    async with pg_pool.acquire() as conn:
        detail = await audit_service.read_audit_detail(conn, audit_id=uuid.uuid4())
    assert detail is None


async def test_the_reveal_row_survives_the_actor_fk_and_check_constraint(
    pg_pool: asyncpg.Pool,
) -> None:
    """``audit_log.actor_user_id`` is an FK into ``users`` and the
    ``audit_log_actor_identity`` CHECK demands exactly one of user/service.
    A wrong actor derivation fails HERE, and only here."""
    recruiter = await _seed_user(pg_pool, "rowan", "recruiter")
    auditor = await _seed_user(pg_pool, "amara", "auditor")
    audit_id = await _seed_withdrawal(pg_pool, recruiter)

    async with pg_pool.acquire() as conn:
        await audit_service.record_audit(
            conn,
            actor_kind="user",
            actor_user_id=auditor,
            actor_service=None,
            action="reveal_audit_detail",
            subject_type="audit_log",
            subject_id=audit_id,
            context="complaint 2026-08",
            details={"revealed_action": "withdraw_resume"},
        )
        items = await audit_service.list_audit_log(conn, action="reveal_audit_detail")

    assert len(items) == 1
    reveal = items[0]
    assert reveal.actor_kind == "user"
    assert reveal.actor_user_id == auditor
    assert reveal.actor_username == "amara", (
        "the reveal is not attributable to a named person — which is the only "
        "thing option C buys over blanket disclosure"
    )
    assert reveal.subject_type == "audit_log"
    assert reveal.subject_id == audit_id
    assert reveal.context == "complaint 2026-08"


async def test_the_trail_of_reveals_is_readable_without_revealing_it(
    pg_pool: asyncpg.Pool,
) -> None:
    """``revealed_action`` is enum-shaped and non-PII, so it must pass the
    allowlist through real SQL — otherwise the record of who revealed what is
    itself a wall of ``<withheld>`` and the compensating control is unusable."""
    recruiter = await _seed_user(pg_pool, "rowan", "recruiter")
    auditor = await _seed_user(pg_pool, "amara", "auditor")
    audit_id = await _seed_withdrawal(pg_pool, recruiter)

    async with pg_pool.acquire() as conn:
        await audit_service.record_audit(
            conn,
            actor_kind="user",
            actor_user_id=auditor,
            actor_service=None,
            action="reveal_audit_detail",
            subject_type="audit_log",
            subject_id=audit_id,
            details={"revealed_action": "withdraw_resume"},
        )
        items = await audit_service.list_audit_log(conn, action="reveal_audit_detail")

    assert items[0].details == {"revealed_action": "withdraw_resume"}
    assert audit_service.WITHHELD not in str(items[0].details)


async def test_the_reveal_row_is_not_itself_revealable(
    pg_pool: asyncpg.Pool,
) -> None:
    """No recursion: revealing a reveal would be a route that audits reads of
    a value the allowlist already discloses."""
    assert audit_service.is_revealable_action("reveal_audit_detail") is False
