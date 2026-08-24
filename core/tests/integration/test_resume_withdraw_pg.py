"""Integration tests — ``src.services.resume_service`` withdrawal-lifecycle
functions (``withdraw_resume`` / ``reinstate_resume`` / ``status_breakdown``,
ADR-026 decisions 1-3/5, FU-8) against a REAL Postgres (testcontainers).

What a REAL Postgres proves that the mocked-conn unit tests
(``test_services_resume_withdraw.py``) structurally cannot:

* **Idempotency, for real.** A second ``withdraw_resume``/``reinstate_resume``
  call against an already-withdrawn/already-active row writes ZERO new
  ``audit_log`` rows AND ZERO new ``outbox`` rows — a real row-count query, not
  a mocked call-count assertion — and the ``withdrawn_at`` timestamp is
  BYTE-IDENTICAL across both calls (not merely "still non-null").
* **Atomicity, for real.** Forcing ``audit_service.record_audit`` to raise
  AFTER the real guarded UPDATE would have applied must leave
  ``withdrawn_at`` NULL once the exception propagates — a mutant that moves
  the audit/outbox write OUTSIDE the wrapping ``conn.transaction()`` commits
  the flip anyway and fails this test; a mocked connection cannot prove a
  real ROLLBACK undoes an already-``execute``'d UPDATE.
* **Reinstate replay payload equality.** The re-enqueued ``resume.parsed``
  outbox payload is byte-for-byte the LAST DELIVERED ``resume.parsed``
  payload for that résumé — asserted against a real jsonb round trip, not a
  mocked ``fetchval`` return value.
* **Status breakdown, mutual exclusion.** A withdrawn-but-parsed row counts
  ONLY under ``withdrawn`` — never double-counted under ``parsed`` too — real
  ``GROUP BY`` behaviour a mocked ``fetch`` cannot prove.

None of ``withdraw_resume``/``reinstate_resume``/``status_breakdown`` exist
yet on ``src.services.resume_service`` — every test below fails at import or
at the first call. RED half of the TDD cycle.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock

import asyncpg
import pytest
from testcontainers.postgres import PostgresContainer

from src.errors import NotFoundError
from src.models.ddl import init_schema
from src.services import outbox_service

# A real ``users`` row backing the ``actor_kind='user'`` audit rows these
# service tests write. ``audit_log.actor_user_id`` carries a FK to ``users``
# (FU-5, ADR-019 §6) — in production the withdraw/reinstate route always
# supplies a real CAS-session ``user.id`` (or a service actor with
# ``actor_user_id=None``), so a fabricated id never reaches the audit sink. At
# the service layer these tests must seed the referenced row for the FK to
# hold; the assertions (``actor_kind``/``actor_user_id``/row counts) are
# unchanged — only this precondition is supplied.
_ACTOR_ID = uuid.UUID("a11ce000-0000-4000-8000-000000000001")


@pytest.fixture(scope="session")
def pg_dsn() -> Iterator[str]:
    with PostgresContainer("postgres:16-alpine") as pg:
        yield re.sub(r"\+\w+", "", pg.get_connection_url(), count=1)


@pytest.fixture
async def pg_pool(pg_dsn: str) -> AsyncIterator[asyncpg.Pool]:
    pool = await asyncpg.create_pool(dsn=pg_dsn, min_size=2, max_size=8)
    await init_schema(pool)
    async with pool.acquire() as conn:
        await conn.execute("TRUNCATE jobs, resumes, outbox, audit_log CASCADE")
        await conn.execute(
            "INSERT INTO users (id, cas_username, role) "
            "VALUES ($1, 'withdraw-test-actor', 'recruiter') "
            "ON CONFLICT (id) DO NOTHING",
            _ACTOR_ID,
        )
    try:
        yield pool
    finally:
        await pool.close()


async def _insert_job(pool: asyncpg.Pool) -> uuid.UUID:
    async with pool.acquire() as conn:
        job_id: uuid.UUID = await conn.fetchval(
            "INSERT INTO jobs (title, description_raw) VALUES ($1, $2) RETURNING id",
            "Senior Backend Engineer",
            "raw jd text",
        )
    return job_id


async def _insert_resume(
    pool: asyncpg.Pool,
    job_id: uuid.UUID,
    *,
    status: str = "parsed",
) -> uuid.UUID:
    async with pool.acquire() as conn:
        resume_id: uuid.UUID = await conn.fetchval(
            """
            INSERT INTO resumes (
                job_id, blob_key, original_filename, mime_type,
                file_size_bytes, sha256, consent_acknowledged, status
            ) VALUES ($1, $2, 'resume.pdf', 'application/pdf', 1024, $3, TRUE, $4)
            RETURNING id
            """,
            job_id,
            f"resumes/{uuid.uuid4().hex}.pdf",
            uuid.uuid4().hex,
            status,
        )
    return resume_id


async def _seed_delivered_resume_parsed(
    pool: asyncpg.Pool, resume_id: uuid.UUID, *, marker: str
) -> dict[str, Any]:
    """Enqueue a ``resume.parsed`` outbox row and mark it delivered, as the
    real drainer would after a successful projection. ``marker`` lands in the
    payload so a test can tell WHICH seeded payload was replayed."""
    payload: dict[str, Any] = {
        "parsed": {"total_years_experience": 5, "skills": [], "chunks": []},
        "summary_emb": [0.1, 0.2, 0.3],
        "chunk_embs": {},
        "prompt_version": "resume_core_v1+resume_skills_v2",
        "job_id": None,
        "marker": marker,
    }
    async with pool.acquire() as conn:
        await outbox_service.enqueue_outbox(
            conn,
            aggregate="resume",
            aggregate_id=resume_id,
            event_type="resume.parsed",
            payload=payload,
        )
        await conn.execute(
            "UPDATE outbox SET delivered_at = now() "
            "WHERE aggregate_id = $1 AND event_type = 'resume.parsed' "
            "AND delivered_at IS NULL",
            resume_id,
        )
    return payload


async def _resume_row(pool: asyncpg.Pool, resume_id: uuid.UUID) -> asyncpg.Record:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT withdrawn_at, withdrawal_reason FROM resumes WHERE id = $1",
            resume_id,
        )
    assert row is not None
    return row


async def _audit_rows(
    pool: asyncpg.Pool, resume_id: uuid.UUID, action: str
) -> list[asyncpg.Record]:
    async with pool.acquire() as conn:
        return await conn.fetch(
            "SELECT actor_kind, actor_user_id, actor_service, action, "
            "subject_type, subject_id, details FROM audit_log "
            "WHERE subject_id = $1 AND action = $2",
            resume_id,
            action,
        )


async def _outbox_rows(
    pool: asyncpg.Pool, resume_id: uuid.UUID, event_type: str
) -> list[asyncpg.Record]:
    async with pool.acquire() as conn:
        return await conn.fetch(
            "SELECT id, payload, delivered_at FROM outbox "
            "WHERE aggregate_id = $1 AND event_type = $2 ORDER BY id",
            resume_id,
            event_type,
        )


# ── withdraw_resume: happy path, real row ──────────────────────────────────


@pytest.mark.asyncio
async def test_withdraw_resume_sets_withdrawn_at_and_reason(
    pg_pool: asyncpg.Pool,
) -> None:
    from src.services.resume_service import withdraw_resume

    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume(pg_pool, job_id)

    async with pg_pool.acquire() as conn:
        await withdraw_resume(
            conn,
            resume_id,
            reason="accepted another offer",
            actor_kind="user",
            actor_user_id=_ACTOR_ID,
            actor_service=None,
        )

    row = await _resume_row(pg_pool, resume_id)
    assert row["withdrawn_at"] is not None
    assert row["withdrawal_reason"] == "accepted another offer"


@pytest.mark.asyncio
async def test_withdraw_resume_writes_exactly_one_audit_log_row(
    pg_pool: asyncpg.Pool,
) -> None:
    from src.services.resume_service import withdraw_resume

    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume(pg_pool, job_id)
    actor_id = _ACTOR_ID

    async with pg_pool.acquire() as conn:
        await withdraw_resume(
            conn,
            resume_id,
            reason=None,
            actor_kind="user",
            actor_user_id=actor_id,
            actor_service=None,
        )

    rows = await _audit_rows(pg_pool, resume_id, "withdraw_resume")
    assert len(rows) == 1
    row = rows[0]
    assert row["actor_kind"] == "user"
    assert row["actor_user_id"] == actor_id
    assert row["subject_type"] == "resume"


@pytest.mark.asyncio
async def test_withdraw_resume_enqueues_exactly_one_resume_withdrawn_outbox_row(
    pg_pool: asyncpg.Pool,
) -> None:
    from src.services.resume_service import withdraw_resume

    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume(pg_pool, job_id)

    async with pg_pool.acquire() as conn:
        await withdraw_resume(
            conn,
            resume_id,
            reason=None,
            actor_kind="user",
            actor_user_id=_ACTOR_ID,
            actor_service=None,
        )

    rows = await _outbox_rows(pg_pool, resume_id, "resume.withdrawn")
    assert len(rows) == 1
    assert rows[0]["delivered_at"] is None


@pytest.mark.asyncio
async def test_withdraw_resume_nonexistent_resume_raises_not_found(
    pg_pool: asyncpg.Pool,
) -> None:
    from src.services.resume_service import withdraw_resume

    async with pg_pool.acquire() as conn:
        with pytest.raises(NotFoundError):
            await withdraw_resume(
                conn,
                uuid.uuid4(),
                reason=None,
                actor_kind="user",
                actor_user_id=_ACTOR_ID,
                actor_service=None,
            )


# ── withdraw_resume: idempotency, for real ──────────────────────────────────


@pytest.mark.asyncio
async def test_withdraw_resume_twice_is_idempotent_zero_new_rows_unchanged_timestamp(
    pg_pool: asyncpg.Pool,
) -> None:
    """The MUTATION-GRADE idempotency obligation: a real second call must
    write ZERO new audit_log rows AND ZERO new outbox rows, and
    ``withdrawn_at`` must be the SAME instant across both calls — not just
    'still non-null'. A mutant that re-stamps ``now()`` on every call, or
    that writes audit/outbox unconditionally, fails this test."""
    from src.services.resume_service import withdraw_resume

    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume(pg_pool, job_id)

    async with pg_pool.acquire() as conn:
        await withdraw_resume(
            conn,
            resume_id,
            reason="first",
            actor_kind="user",
            actor_user_id=_ACTOR_ID,
            actor_service=None,
        )
    first_row = await _resume_row(pg_pool, resume_id)
    first_withdrawn_at = first_row["withdrawn_at"]
    assert first_withdrawn_at is not None

    async with pg_pool.acquire() as conn:
        await withdraw_resume(
            conn,
            resume_id,
            reason="second attempt, different reason text",
            actor_kind="user",
            actor_user_id=_ACTOR_ID,
            actor_service=None,
        )

    second_row = await _resume_row(pg_pool, resume_id)
    assert second_row["withdrawn_at"] == first_withdrawn_at
    assert second_row["withdrawal_reason"] == "first", (
        "the second call's reason must NOT overwrite the first — it is a "
        "guarded no-op, not an update"
    )

    audit_rows = await _audit_rows(pg_pool, resume_id, "withdraw_resume")
    assert len(audit_rows) == 1, "exactly one audit_log row across BOTH calls"

    outbox_rows = await _outbox_rows(pg_pool, resume_id, "resume.withdrawn")
    assert len(outbox_rows) == 1, "exactly one outbox row across BOTH calls"


# ── withdraw_resume: atomicity, for real ────────────────────────────────────


@pytest.mark.asyncio
async def test_audit_write_failure_rolls_back_the_withdraw_flip_too(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SECURITY-CRITICAL, mirrors ``test_blind_review_audit_pg.py``'s own
    atomicity proof: force the real ``audit_service.record_audit`` call to
    raise AFTER the real guarded UPDATE would have applied — the flip must
    NOT be observable once the exception propagates. A mutant that moves the
    audit/outbox write outside the wrapping transaction commits the flip
    anyway and fails this test; a mocked connection cannot prove a real
    ROLLBACK undoes an already-``execute``'d UPDATE."""
    import src.services.resume_service as resume_service_mod
    from src.services.resume_service import withdraw_resume

    monkeypatch.setattr(
        resume_service_mod.audit_service,
        "record_audit",
        AsyncMock(side_effect=RuntimeError("simulated audit-write crash")),
    )

    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume(pg_pool, job_id)

    async with pg_pool.acquire() as conn:
        with pytest.raises(RuntimeError):
            await withdraw_resume(
                conn,
                resume_id,
                reason="should not stick",
                actor_kind="user",
                actor_user_id=_ACTOR_ID,
                actor_service=None,
            )

    row = await _resume_row(pg_pool, resume_id)
    assert row["withdrawn_at"] is None, "the flip must not have applied"
    assert row["withdrawal_reason"] is None

    assert await _audit_rows(pg_pool, resume_id, "withdraw_resume") == []
    assert await _outbox_rows(pg_pool, resume_id, "resume.withdrawn") == []


# ── reinstate_resume: happy path — clears columns, audits ──────────────────


@pytest.mark.asyncio
async def test_reinstate_resume_clears_withdrawn_columns(pg_pool: asyncpg.Pool) -> None:
    from src.services.resume_service import reinstate_resume, withdraw_resume

    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume(pg_pool, job_id)
    async with pg_pool.acquire() as conn:
        await withdraw_resume(
            conn,
            resume_id,
            reason="mistaken click",
            actor_kind="user",
            actor_user_id=_ACTOR_ID,
            actor_service=None,
        )

    async with pg_pool.acquire() as conn:
        await reinstate_resume(
            conn,
            resume_id,
            actor_kind="user",
            actor_user_id=_ACTOR_ID,
            actor_service=None,
        )

    row = await _resume_row(pg_pool, resume_id)
    assert row["withdrawn_at"] is None
    assert row["withdrawal_reason"] is None


@pytest.mark.asyncio
async def test_reinstate_resume_writes_exactly_one_audit_log_row(
    pg_pool: asyncpg.Pool,
) -> None:
    from src.services.resume_service import reinstate_resume, withdraw_resume

    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume(pg_pool, job_id)
    async with pg_pool.acquire() as conn:
        await withdraw_resume(
            conn,
            resume_id,
            reason=None,
            actor_kind="user",
            actor_user_id=_ACTOR_ID,
            actor_service=None,
        )

    actor_id = _ACTOR_ID
    async with pg_pool.acquire() as conn:
        await reinstate_resume(
            conn,
            resume_id,
            actor_kind="user",
            actor_user_id=actor_id,
            actor_service=None,
        )

    rows = await _audit_rows(pg_pool, resume_id, "reinstate_resume")
    assert len(rows) == 1
    assert rows[0]["actor_user_id"] == actor_id


@pytest.mark.asyncio
async def test_reinstate_resume_nonexistent_resume_raises_not_found(
    pg_pool: asyncpg.Pool,
) -> None:
    from src.services.resume_service import reinstate_resume

    async with pg_pool.acquire() as conn:
        with pytest.raises(NotFoundError):
            await reinstate_resume(
                conn,
                uuid.uuid4(),
                actor_kind="user",
                actor_user_id=_ACTOR_ID,
                actor_service=None,
            )


# ── reinstate_resume: the replay contract — byte-identical last payload ────


@pytest.mark.asyncio
async def test_reinstate_resume_replays_the_last_delivered_resume_parsed_payload(
    pg_pool: asyncpg.Pool,
) -> None:
    """The load-bearing decision: reinstate re-enqueues a ``resume.parsed``
    event whose payload is EXACTLY the last DELIVERED ``resume.parsed``
    payload — proven here by seeding TWO delivered payloads (simulating an
    earlier re-parse) and asserting the SECOND (most recent) one, not the
    first, is what gets replayed."""
    from src.services.resume_service import reinstate_resume, withdraw_resume

    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume(pg_pool, job_id)
    await _seed_delivered_resume_parsed(pg_pool, resume_id, marker="first-parse")
    latest_payload = await _seed_delivered_resume_parsed(
        pg_pool, resume_id, marker="second-parse-latest"
    )

    async with pg_pool.acquire() as conn:
        await withdraw_resume(
            conn,
            resume_id,
            reason=None,
            actor_kind="user",
            actor_user_id=_ACTOR_ID,
            actor_service=None,
        )
        await reinstate_resume(
            conn,
            resume_id,
            actor_kind="user",
            actor_user_id=_ACTOR_ID,
            actor_service=None,
        )

    rows = await _outbox_rows(pg_pool, resume_id, "resume.parsed")
    # Two seeded (delivered) + one replayed (undelivered).
    assert len(rows) == 3
    replayed = [r for r in rows if r["delivered_at"] is None]
    assert len(replayed) == 1
    replayed_payload = json.loads(replayed[0]["payload"])
    assert replayed_payload == latest_payload
    assert replayed_payload["marker"] == "second-parse-latest"


@pytest.mark.asyncio
async def test_reinstate_resume_with_no_prior_parse_enqueues_no_resume_parsed_row(
    pg_pool: asyncpg.Pool,
) -> None:
    """A résumé withdrawn while still ``uploaded`` (never successfully
    parsed) has nothing to replay — reinstate must still clear the columns
    and audit, but must not fabricate a ``resume.parsed`` event."""
    from src.services.resume_service import reinstate_resume, withdraw_resume

    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume(pg_pool, job_id, status="uploaded")

    async with pg_pool.acquire() as conn:
        await withdraw_resume(
            conn,
            resume_id,
            reason=None,
            actor_kind="user",
            actor_user_id=_ACTOR_ID,
            actor_service=None,
        )
        await reinstate_resume(
            conn,
            resume_id,
            actor_kind="user",
            actor_user_id=_ACTOR_ID,
            actor_service=None,
        )

    rows = await _outbox_rows(pg_pool, resume_id, "resume.parsed")
    assert rows == []

    row = await _resume_row(pg_pool, resume_id)
    assert row["withdrawn_at"] is None


# ── reinstate_resume: idempotency, for real ─────────────────────────────────


@pytest.mark.asyncio
async def test_reinstate_resume_twice_is_idempotent_zero_new_rows(
    pg_pool: asyncpg.Pool,
) -> None:
    from src.services.resume_service import reinstate_resume, withdraw_resume

    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume(pg_pool, job_id)
    await _seed_delivered_resume_parsed(pg_pool, resume_id, marker="only-parse")

    async with pg_pool.acquire() as conn:
        await withdraw_resume(
            conn,
            resume_id,
            reason=None,
            actor_kind="user",
            actor_user_id=_ACTOR_ID,
            actor_service=None,
        )
        await reinstate_resume(
            conn,
            resume_id,
            actor_kind="user",
            actor_user_id=_ACTOR_ID,
            actor_service=None,
        )
        # Second reinstate on an already-active (never-withdrawn) résumé.
        await reinstate_resume(
            conn,
            resume_id,
            actor_kind="user",
            actor_user_id=_ACTOR_ID,
            actor_service=None,
        )

    audit_rows = await _audit_rows(pg_pool, resume_id, "reinstate_resume")
    assert len(audit_rows) == 1

    parsed_rows = await _outbox_rows(pg_pool, resume_id, "resume.parsed")
    # One seeded + exactly one replay (not two).
    assert len(parsed_rows) == 2


# ── status_breakdown: mutual exclusion, for real ────────────────────────────


@pytest.mark.asyncio
async def test_status_breakdown_buckets_are_mutually_exclusive(
    pg_pool: asyncpg.Pool,
) -> None:
    """A job with a row in every bucket, INCLUDING a withdrawn-parsed and a
    withdrawn-uploaded row: the withdrawn count is correct and the
    underlying ``status`` bucket does NOT also count them — real ``GROUP BY``
    behaviour a mocked ``fetch`` cannot prove."""
    from src.services.resume_service import status_breakdown, withdraw_resume

    job_id = await _insert_job(pg_pool)
    await _insert_resume(pg_pool, job_id, status="uploaded")
    await _insert_resume(pg_pool, job_id, status="parsing")
    await _insert_resume(pg_pool, job_id, status="parsed")
    await _insert_resume(pg_pool, job_id, status="failed")
    withdrawn_parsed_id = await _insert_resume(pg_pool, job_id, status="parsed")
    withdrawn_uploaded_id = await _insert_resume(pg_pool, job_id, status="uploaded")

    async with pg_pool.acquire() as conn:
        await withdraw_resume(
            conn,
            withdrawn_parsed_id,
            reason=None,
            actor_kind="user",
            actor_user_id=_ACTOR_ID,
            actor_service=None,
        )
        await withdraw_resume(
            conn,
            withdrawn_uploaded_id,
            reason=None,
            actor_kind="user",
            actor_user_id=_ACTOR_ID,
            actor_service=None,
        )

    async with pg_pool.acquire() as conn:
        breakdown = await status_breakdown(conn, job_id)

    assert breakdown.uploaded == 1, "only the NON-withdrawn uploaded row counts"
    assert breakdown.parsing == 1
    assert breakdown.parsed == 1, "only the NON-withdrawn parsed row counts"
    assert breakdown.failed == 1
    assert breakdown.withdrawn == 2

    total = (
        breakdown.uploaded
        + breakdown.parsing
        + breakdown.parsed
        + breakdown.failed
        + breakdown.withdrawn
    )
    assert total == 6, "every row counted exactly once across all five buckets"


@pytest.mark.asyncio
async def test_status_breakdown_zero_resume_job_returns_all_zero_buckets(
    pg_pool: asyncpg.Pool,
) -> None:
    from src.services.resume_service import status_breakdown

    job_id = await _insert_job(pg_pool)

    async with pg_pool.acquire() as conn:
        breakdown = await status_breakdown(conn, job_id)

    assert breakdown.uploaded == 0
    assert breakdown.parsing == 0
    assert breakdown.parsed == 0
    assert breakdown.failed == 0
    assert breakdown.withdrawn == 0
