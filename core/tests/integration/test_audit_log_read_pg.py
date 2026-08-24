"""Integration tests — Phase 1.4 slice 1: the ``audit_log`` READ path, against
a REAL Postgres (testcontainers).

**The gap this closes, stated plainly.** ``audit_log`` is the live,
authoritative audit trail: nine call sites across reveals, résumé
withdraw/reinstate, job-assignee changes, role changes and job-service writes
insert into it. **Nothing in the codebase reads it.** ``grep -rn "FROM
audit_log" core/src/`` returns nothing — no service function, no route, no UI.
The one audit route that does exist (``GET /audit/reveals-legacy``) reads
``reveal_audit``, the table FROZEN at FU-5 slice 8, so it returns only
pre-cutover history and nothing that has happened since.

So the auditor role's position today is not "the screen has not been built
yet" — it is that **the application cannot read its own audit log at all**, and
producing the access record means an engineer running SQL by hand against
production. That is what makes the audit-log viewer the last thing standing
between here and issuing auditor accounts (ROADMAP guardrail 2).

**Why these tests must run against a real Postgres.** Every claim below is a
property of SQL, and the unit suite can only string-match the query text:

* the ``LEFT JOIN users`` really resolves ``actor_user_id`` to a username, and
  really yields NULL — not a dropped row — for a ``actor_kind='service'`` row
  whose ``actor_user_id`` is NULL. An INNER JOIN here would silently hide every
  service-actor row from the auditor, which is precisely the class of event
  (the unattributable ``actor_service='api'`` write) that ADR-034's exploit
  produced.
* ``ORDER BY occurred_at DESC`` with a real tiebreak, against real
  ``TIMESTAMPTZ DEFAULT now()`` values written inside one transaction, which
  can and do collide.
* ``LIMIT``/``OFFSET`` paginate a stable order rather than re-shuffling rows
  between pages.
* the filters compose as AND, and an absent filter does not silently drop rows.

CLAUDE.md's rule applies exactly: "if the correctness of a change depends on
how a real database behaves, the unit suite structurally cannot prove it."

Harness is the established one (testcontainers Postgres + ``init_schema``),
matching ``test_reveal_audit_log_pg.py``. No new harness.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from collections.abc import AsyncIterator, Iterator

import asyncpg
import pytest
from testcontainers.postgres import PostgresContainer

from src.models.ddl import init_schema
from src.services import audit_service


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


async def _seed_user(
    pool: asyncpg.Pool, username: str, role: str = "admin"
) -> uuid.UUID:
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


async def _seed_event(
    pool: asyncpg.Pool,
    *,
    actor_kind: str = "user",
    actor_user_id: uuid.UUID | None = None,
    actor_service: str | None = None,
    action: str = "reveal",
    subject_type: str = "resume",
    subject_id: uuid.UUID | None = None,
    job_id: uuid.UUID | None = None,
    context: str | None = None,
    details: str | None = None,
) -> uuid.UUID:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO audit_log (actor_kind, actor_user_id, actor_service, "
            "action, subject_type, subject_id, job_id, context, details) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb) RETURNING id",
            actor_kind,
            actor_user_id,
            actor_service,
            action,
            subject_type,
            subject_id or uuid.uuid4(),
            job_id,
            context,
            details,
        )
    assert row is not None
    return uuid.UUID(str(row["id"]))


async def test_reads_a_row_and_resolves_the_human_actor_to_a_username(
    pg_pool: asyncpg.Pool,
) -> None:
    """The point of attributable audit: the auditor sees WHO, not a UUID."""
    user_id = await _seed_user(pg_pool, "areviewer")
    await _seed_event(pg_pool, actor_user_id=user_id, action="reveal")

    items = await audit_service.list_audit_log(pg_pool, limit=50, offset=0)

    assert len(items) == 1
    assert items[0].actor_username == "areviewer"
    assert items[0].actor_kind == "user"
    assert items[0].action == "reveal"


async def test_a_service_actor_row_is_returned_not_dropped(
    pg_pool: asyncpg.Pool,
) -> None:
    """THE join pin. ``actor_kind='service'`` rows have a NULL
    ``actor_user_id``; an INNER JOIN would silently hide every one of them.

    These are exactly the events an auditor most needs to see — an
    unattributable ``actor_service='api'`` write is the signature of the
    ADR-034 exploit. A viewer that quietly omitted them would be worse than no
    viewer, because it would look complete.
    """
    await _seed_event(
        pg_pool,
        actor_kind="service",
        actor_user_id=None,
        actor_service="api",
        action="blind_review_changed",
        subject_type="job",
    )

    items = await audit_service.list_audit_log(pg_pool, limit=50, offset=0)

    assert len(items) == 1, "the service-actor row was dropped by the join"
    assert items[0].actor_kind == "service"
    assert items[0].actor_service == "api"
    assert items[0].actor_username is None


async def test_rows_come_back_newest_first(pg_pool: asyncpg.Pool) -> None:
    user_id = await _seed_user(pg_pool, "bwatcher")
    # Real ``datetime`` objects, not ISO strings: asyncpg binds ``timestamptz``
    # by inferred Python type and rejects a ``str`` outright, regardless of any
    # ``::timestamptz`` cast in the SQL text.
    stamps = [
        dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
        dt.datetime(2026, 6, 1, tzinfo=dt.UTC),
        dt.datetime(2026, 3, 1, tzinfo=dt.UTC),
    ]
    async with pg_pool.acquire() as conn:
        for n, ts in enumerate(stamps):
            await conn.execute(
                "INSERT INTO audit_log (actor_kind, actor_user_id, action, "
                "subject_type, subject_id, occurred_at) "
                "VALUES ('user', $1, $2, 'resume', $3, $4)",
                user_id,
                f"action_{n}",
                uuid.uuid4(),
                ts,
            )

    items = await audit_service.list_audit_log(pg_pool, limit=50, offset=0)

    assert [i.action for i in items] == ["action_1", "action_2", "action_0"]


async def test_pagination_walks_a_stable_order_without_repeats(
    pg_pool: asyncpg.Pool,
) -> None:
    """Rows written inside one statement share an ``occurred_at`` to the
    microsecond, so ``ORDER BY occurred_at DESC`` alone is not a total order —
    without a tiebreak the same row can appear on two pages while another
    appears on none. Only a real database exhibits this.
    """
    user_id = await _seed_user(pg_pool, "cpager")
    async with pg_pool.acquire() as conn:
        await conn.executemany(
            "INSERT INTO audit_log (actor_kind, actor_user_id, action, "
            "subject_type, subject_id, occurred_at) "
            "VALUES ('user', $1, 'reveal', 'resume', $2, now())",
            [(user_id, uuid.uuid4()) for _ in range(10)],
        )

    page1 = await audit_service.list_audit_log(pg_pool, limit=4, offset=0)
    page2 = await audit_service.list_audit_log(pg_pool, limit=4, offset=4)
    page3 = await audit_service.list_audit_log(pg_pool, limit=4, offset=8)

    seen = [i.id for i in [*page1, *page2, *page3]]
    assert len(seen) == 10
    assert len(set(seen)) == 10, "pagination repeated a row across pages"


async def test_filters_narrow_and_compose_as_and(pg_pool: asyncpg.Pool) -> None:
    user_id = await _seed_user(pg_pool, "dfilter")
    job_id = uuid.uuid4()
    await _seed_event(pg_pool, actor_user_id=user_id, action="reveal", job_id=job_id)
    await _seed_event(pg_pool, actor_user_id=user_id, action="reveal", job_id=None)
    await _seed_event(
        pg_pool, actor_user_id=user_id, action="withdraw_resume", job_id=job_id
    )

    by_action = await audit_service.list_audit_log(pg_pool, action="reveal")
    assert len(by_action) == 2

    by_job = await audit_service.list_audit_log(pg_pool, job_id=job_id)
    assert len(by_job) == 2

    both = await audit_service.list_audit_log(pg_pool, action="reveal", job_id=job_id)
    assert len(both) == 1, "filters did not compose as AND"

    unfiltered = await audit_service.list_audit_log(pg_pool)
    assert len(unfiltered) == 3, "an absent filter dropped rows"


async def test_the_withdrawal_reason_never_leaves_the_database(
    pg_pool: asyncpg.Pool,
) -> None:
    """The disclosure boundary, proven end to end against a REAL row rather
    than against the pure function alone.

    ``test_audit_log_details_redaction.py`` proves ``redact_audit_details``
    withholds the reason. This proves the read path actually CALLS it — the
    two are different claims, and it is the second one that protects the
    candidate. A boundary that exists but is not wired in is the ADR-031
    inert-PII-scan lesson repeating.
    """
    user_id = await _seed_user(pg_pool, "eleaker")
    secret = "Jane Q Candidate asked us to delete her file"
    await _seed_event(
        pg_pool,
        actor_user_id=user_id,
        action="withdraw_resume",
        details=f'{{"reason": "{secret}"}}',
    )

    items = await audit_service.list_audit_log(pg_pool)

    assert len(items) == 1
    rendered = items[0].model_dump_json()
    assert secret not in rendered
    assert "Jane" not in rendered
    assert items[0].details == {"reason": audit_service.WITHHELD}
