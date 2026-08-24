"""Integration tests — the startup DDL against a real Postgres (testcontainers).

String-matching in ``tests/unit/test_ddl.py`` proves the DDL *says* it is
idempotent; this proves it *is*: ``init_schema`` runs twice against a live
database without error. Also proves the pgcrypto round-trip that every PII
column depends on.

Runs in CI (`make gates-integration`), not in the offline gate suite.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import AsyncIterator, Iterator

import asyncpg
import pytest
from testcontainers.postgres import PostgresContainer

from src.models.ddl import init_schema

# 32 random bytes, base64 — the shape PII_KEY must take in production.
PII_KEY = "b3VyLTMyLWJ5dGUtcGlpLWtleS1mb3ItdGVzdHM="

INSERT_RESUME = """
INSERT INTO resumes (
    job_id, blob_key, original_filename, mime_type,
    file_size_bytes, sha256, consent_acknowledged
) VALUES ($1, $2, 'a.pdf', 'application/pdf', 1024, $3, TRUE)
RETURNING id
"""

EXPECTED_TABLES: frozenset[str] = frozenset(
    {
        "jobs",
        "resumes",
        "shortlist_entries",
        "reverse_match_entries",
        "outbox",
        "users",
        "audit_log",
    }
)

# `users` was listed here until FU-5. ADR-019 explicitly reverses the Phase-0
# cut of the CAS auth tables, so it moves into EXPECTED_TABLES above; the
# review-workflow tables stay cut. Mirrors the same change in
# tests/unit/test_ddl.py.
CUT_TABLES: tuple[str, ...] = ("shortlist_decisions", "stage_transitions")

EXPECTED_INDEXES: tuple[str, ...] = (
    "jobs_status_open_idx",
    "jobs_created_at_idx",
    "resumes_job_status_idx",
    "resumes_email_hash_idx",
    "resumes_uploaded_at_idx",
    "resumes_has_cover_idx",
    "shortlist_job_rank_idx",
    "reverse_match_entries_resume_job_idx",
    "reverse_match_entries_resume_idx",
    "outbox_undelivered_idx",
    "outbox_aggregate_idx",
)

ENCRYPTED_COLUMNS: tuple[str, ...] = (
    "candidate_name",
    "candidate_email",
    "candidate_phone",
    "cover_letter_text",
)


@pytest.fixture(scope="session")
def pg_dsn() -> Iterator[str]:
    with PostgresContainer("postgres:16-alpine") as pg:
        # asyncpg needs the raw DSN, not SQLAlchemy's postgresql+driver:// form.
        yield re.sub(r"\+\w+", "", pg.get_connection_url(), count=1)


@pytest.fixture
async def conn(pg_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    connection: asyncpg.Connection = await asyncpg.connect(pg_dsn)
    await init_schema(connection)
    await connection.execute("TRUNCATE jobs, outbox CASCADE")
    try:
        yield connection
    finally:
        await connection.close()


async def _insert_job(conn: asyncpg.Connection) -> uuid.UUID:
    job_id: uuid.UUID = await conn.fetchval(
        "INSERT INTO jobs (title, description_raw) VALUES ($1, $2) RETURNING id",
        "Senior Python Engineer",
        "Build the ranking pipeline.",
    )
    return job_id


async def _insert_resume(
    conn: asyncpg.Connection, job_id: uuid.UUID, sha256: str | None = None
) -> uuid.UUID:
    resume_id: uuid.UUID = await conn.fetchval(
        INSERT_RESUME,
        job_id,
        f"resumes/{uuid.uuid4().hex}.pdf",
        sha256 or uuid.uuid4().hex,
    )
    return resume_id


# ── Idempotency, for real ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_init_schema_twice_in_a_row_succeeds(pg_dsn: str) -> None:
    connection = await asyncpg.connect(pg_dsn)
    try:
        await init_schema(connection)
        await init_schema(connection)  # every boot re-runs this
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_init_schema_accepts_a_pool(pg_dsn: str) -> None:
    pool = await asyncpg.create_pool(dsn=pg_dsn, min_size=1, max_size=2)
    assert pool is not None
    try:
        await init_schema(pool)
    finally:
        await pool.close()


# ── Objects actually created ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_all_five_tables_exist(conn: asyncpg.Connection) -> None:
    rows = await conn.fetch(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
    )
    assert EXPECTED_TABLES <= {row["tablename"] for row in rows}


@pytest.mark.parametrize("table", CUT_TABLES)
@pytest.mark.asyncio
async def test_cut_tables_do_not_exist(conn: asyncpg.Connection, table: str) -> None:
    exists = await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", table)
    assert exists is False


@pytest.mark.parametrize("index", EXPECTED_INDEXES)
@pytest.mark.asyncio
async def test_expected_index_exists(conn: asyncpg.Connection, index: str) -> None:
    found = await conn.fetchval(
        "SELECT indexname FROM pg_indexes WHERE schemaname='public' AND indexname=$1",
        index,
    )
    assert found == index


@pytest.mark.asyncio
async def test_status_enums_exist(conn: asyncpg.Connection) -> None:
    rows = await conn.fetch(
        "SELECT typname FROM pg_type WHERE typname IN ('job_status','resume_status')"
    )
    assert {row["typname"] for row in rows} == {"job_status", "resume_status"}


@pytest.mark.asyncio
async def test_pgcrypto_extension_installed(conn: asyncpg.Connection) -> None:
    installed = await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pgcrypto')"
    )
    assert installed is True


@pytest.mark.parametrize("column", ENCRYPTED_COLUMNS)
@pytest.mark.asyncio
async def test_pii_column_is_bytea(conn: asyncpg.Connection, column: str) -> None:
    data_type = await conn.fetchval(
        """
        SELECT data_type FROM information_schema.columns
        WHERE table_name = 'resumes' AND column_name = $1
        """,
        column,
    )
    assert data_type == "bytea"


@pytest.mark.asyncio
async def test_candidate_email_hash_is_plaintext_text(
    conn: asyncpg.Connection,
) -> None:
    data_type = await conn.fetchval("""
        SELECT data_type FROM information_schema.columns
        WHERE table_name = 'resumes' AND column_name = 'candidate_email_hash'
        """)
    assert data_type == "text"


# ── Behaviour the columns promise ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_pii_round_trip_through_app_pii_key(conn: asyncpg.Connection) -> None:
    """pgp_sym_encrypt on write, pgp_sym_decrypt on read, key from the GUC."""
    await conn.execute("SELECT set_config('app.pii_key', $1, false)", PII_KEY)
    job_id = await _insert_job(conn)
    resume_id = await _insert_resume(conn, job_id)

    await conn.execute(
        """
        UPDATE resumes
           SET candidate_email = pgp_sym_encrypt($2, current_setting('app.pii_key'))
         WHERE id = $1
        """,
        resume_id,
        "ada@example.org",
    )

    stored = await conn.fetchval(
        "SELECT candidate_email FROM resumes WHERE id = $1", resume_id
    )
    assert isinstance(stored, bytes)
    assert b"ada@example.org" not in stored  # ciphertext, not plaintext

    decrypted = await conn.fetchval(
        """
        SELECT pgp_sym_decrypt(candidate_email, current_setting('app.pii_key'))
          FROM resumes WHERE id = $1
        """,
        resume_id,
    )
    assert decrypted == "ada@example.org"


@pytest.mark.asyncio
async def test_jobs_blind_review_defaults_true(conn: asyncpg.Connection) -> None:
    job_id = await _insert_job(conn)
    row = await conn.fetchrow(
        "SELECT blind_review, status, retention_days FROM jobs WHERE id = $1", job_id
    )
    assert row is not None
    assert row["blind_review"] is True
    assert row["status"] == "draft"
    assert row["retention_days"] == 180


@pytest.mark.asyncio
async def test_deleting_a_job_cascades_to_its_resumes(
    conn: asyncpg.Connection,
) -> None:
    job_id = await _insert_job(conn)
    await _insert_resume(conn, job_id)

    await conn.execute("DELETE FROM jobs WHERE id = $1", job_id)

    remaining = await conn.fetchval(
        "SELECT count(*) FROM resumes WHERE job_id = $1", job_id
    )
    assert remaining == 0


@pytest.mark.asyncio
async def test_resume_dedupe_on_job_id_sha256(conn: asyncpg.Connection) -> None:
    job_id = await _insert_job(conn)
    digest = uuid.uuid4().hex
    await _insert_resume(conn, job_id, sha256=digest)

    with pytest.raises(asyncpg.UniqueViolationError):
        await _insert_resume(conn, job_id, sha256=digest)


@pytest.mark.asyncio
async def test_resume_requires_explicit_consent(conn: asyncpg.Connection) -> None:
    """consent_acknowledged has no default — omitting it must fail."""
    job_id = await _insert_job(conn)
    with pytest.raises(asyncpg.NotNullViolationError):
        await conn.execute(
            """
            INSERT INTO resumes (
                job_id, blob_key, original_filename, mime_type,
                file_size_bytes, sha256
            ) VALUES ($1, 'k', 'a.pdf', 'application/pdf', 10, $2)
            """,
            job_id,
            uuid.uuid4().hex,
        )


@pytest.mark.asyncio
async def test_shortlist_score_must_be_between_0_and_1(
    conn: asyncpg.Connection,
) -> None:
    job_id = await _insert_job(conn)
    resume_id = await _insert_resume(conn, job_id)
    with pytest.raises(asyncpg.CheckViolationError):
        await conn.execute(
            """
            INSERT INTO shortlist_entries (
                job_id, resume_id, rank, score_final,
                score_breakdown, evidence, pipeline_meta
            ) VALUES ($1, $2, 1, 1.5, '{}', '{}', '{}')
            """,
            job_id,
            resume_id,
        )


@pytest.mark.asyncio
async def test_shortlist_score_is_a_float_not_a_decimal(
    conn: asyncpg.Connection,
) -> None:
    """DOUBLE PRECISION, so asyncpg never hands back a Decimal."""
    job_id = await _insert_job(conn)
    resume_id = await _insert_resume(conn, job_id)
    await conn.execute(
        """
        INSERT INTO shortlist_entries (
            job_id, resume_id, rank, score_final,
            score_breakdown, evidence, pipeline_meta
        ) VALUES ($1, $2, 1, 0.875, '{}', '{}', '{}')
        """,
        job_id,
        resume_id,
    )
    score = await conn.fetchval(
        "SELECT score_final FROM shortlist_entries WHERE resume_id = $1", resume_id
    )
    assert isinstance(score, float)
    assert score == pytest.approx(0.875)


@pytest.mark.asyncio
async def test_reverse_match_entry_round_trips_jsonb_payload(
    conn: asyncpg.Connection,
) -> None:
    """Exercise reverse_match_entries live so its JSONB payload columns are
    behaviourally proven, not just declared. score_breakdown/evidence/
    pipeline_meta must accept a JSON object and hand it back as a mapping."""
    job_id = await _insert_job(conn)
    resume_id = await _insert_resume(conn, job_id)
    await conn.execute(
        """
        INSERT INTO reverse_match_entries (
            resume_id, job_id, rank, score_final, score_structured,
            score_evidence, score_breakdown, evidence, requirement_count,
            must_have_count, pipeline_meta
        ) VALUES (
            $1, $2, 1, 0.5, 0.6, 0.4,
            '{"must_have": 0.7}', '[{"req": "python", "quote": "5y python"}]',
            3, 2, '{"stage": "reverse"}'
        )
        """,
        resume_id,
        job_id,
    )
    row = await conn.fetchrow(
        """
        SELECT score_breakdown, evidence, pipeline_meta
          FROM reverse_match_entries WHERE resume_id = $1
        """,
        resume_id,
    )
    assert row is not None
    # asyncpg returns JSONB as str; a lossy TEXT downgrade would still parse,
    # but the schema-level unit guard forbids TEXT — here we prove the value
    # survives a JSON round-trip intact.
    assert json.loads(row["score_breakdown"]) == {"must_have": 0.7}
    assert json.loads(row["evidence"]) == [{"req": "python", "quote": "5y python"}]
    assert json.loads(row["pipeline_meta"]) == {"stage": "reverse"}


# ── résumé withdrawal lifecycle (ADR-026 decision 1, FU-8) ─────────────────


@pytest.mark.asyncio
async def test_withdrawn_at_column_is_a_nullable_timestamptz(
    conn: asyncpg.Connection,
) -> None:
    row = await conn.fetchrow("""
        SELECT data_type, is_nullable FROM information_schema.columns
        WHERE table_name = 'resumes' AND column_name = 'withdrawn_at'
        """)
    assert row is not None, "resumes.withdrawn_at column does not exist"
    assert row["data_type"] == "timestamp with time zone"
    assert row["is_nullable"] == "YES"


@pytest.mark.asyncio
async def test_withdrawal_reason_column_is_a_nullable_text(
    conn: asyncpg.Connection,
) -> None:
    row = await conn.fetchrow("""
        SELECT data_type, is_nullable FROM information_schema.columns
        WHERE table_name = 'resumes' AND column_name = 'withdrawal_reason'
        """)
    assert row is not None, "resumes.withdrawal_reason column does not exist"
    assert row["data_type"] == "text"
    assert row["is_nullable"] == "YES"


@pytest.mark.asyncio
async def test_newly_inserted_resume_has_null_withdrawal_columns(
    conn: asyncpg.Connection,
) -> None:
    """A fresh row must never be born withdrawn — proves no DEFAULT sneaked
    onto either column."""
    job_id = await _insert_job(conn)
    resume_id = await _insert_resume(conn, job_id)
    row = await conn.fetchrow(
        "SELECT withdrawn_at, withdrawal_reason FROM resumes WHERE id = $1",
        resume_id,
    )
    assert row is not None
    assert row["withdrawn_at"] is None
    assert row["withdrawal_reason"] is None


@pytest.mark.asyncio
async def test_withdrawn_at_and_reason_round_trip_through_an_update(
    conn: asyncpg.Connection,
) -> None:
    job_id = await _insert_job(conn)
    resume_id = await _insert_resume(conn, job_id)
    await conn.execute(
        "UPDATE resumes SET withdrawn_at = now(), withdrawal_reason = $2 "
        "WHERE id = $1",
        resume_id,
        "candidate accepted another offer",
    )
    row = await conn.fetchrow(
        "SELECT withdrawn_at, withdrawal_reason FROM resumes WHERE id = $1",
        resume_id,
    )
    assert row is not None
    assert row["withdrawn_at"] is not None
    assert row["withdrawal_reason"] == "candidate accepted another offer"


@pytest.mark.asyncio
async def test_withdrawal_columns_survive_init_schema_run_twice(pg_dsn: str) -> None:
    """The idempotency proof this ADR's own column-addition convention
    demands (mirrors ``test_init_schema_twice_in_a_row_succeeds``, but also
    checks the specific columns this slice adds land — and stay landed —
    across two boots against the SAME already-migrated volume)."""
    connection = await asyncpg.connect(pg_dsn)
    try:
        await init_schema(connection)
        await init_schema(connection)

        withdrawn_at_row = await connection.fetchrow("""
            SELECT data_type, is_nullable FROM information_schema.columns
            WHERE table_name = 'resumes' AND column_name = 'withdrawn_at'
            """)
        assert withdrawn_at_row is not None
        assert withdrawn_at_row["data_type"] == "timestamp with time zone"
        assert withdrawn_at_row["is_nullable"] == "YES"

        reason_row = await connection.fetchrow("""
            SELECT data_type, is_nullable FROM information_schema.columns
            WHERE table_name = 'resumes' AND column_name = 'withdrawal_reason'
            """)
        assert reason_row is not None
        assert reason_row["data_type"] == "text"
        assert reason_row["is_nullable"] == "YES"
    finally:
        await connection.close()


# ── fix/regenerate-shortlist-no-feedback — widen shortlist_state's CHECK ───
#
# ``jobs.shortlist_state`` today (ddl.py:132) carries
# ``CHECK (shortlist_state IN ('awaiting_llm'))`` — added by FU-7 §2 and, per
# CLAUDE.md's "offline is not always enough", this widening is EXACTLY the
# class of change the unit suite structurally cannot verify: it is a real
# constraint on a real server, not a string in a module.
#
# Confirmed live against the running dev stack (docker compose postgres,
# 2026-08-18) BEFORE writing these tests — the auto-generated name Postgres
# actually gave the inline CHECK is:
#
#     jobs_shortlist_state_check | CHECK ((shortlist_state = 'awaiting_llm'::text))
#
# (a single-value CHECK compiles to ``= 'awaiting_llm'``, not an ``IN (...)``
# — same constraint, Postgres's own normal form). The spec's suggested
# ``DROP CONSTRAINT IF EXISTS jobs_shortlist_state_check`` / ``ADD CONSTRAINT
# jobs_shortlist_state_check CHECK (... IN ('awaiting_llm', 'ranking'))`` pair
# targets the RIGHT name — this is not an assumption, it is what Postgres
# reported.
#
# ``test_init_schema_twice_in_a_row_succeeds`` above and
# ``test_withdrawal_columns_survive_init_schema_run_twice`` are the direct
# precedent for the pattern below: ``ADD COLUMN IF NOT EXISTS`` is a no-op the
# SECOND time ``init_schema`` runs (a real deployed volume that already has
# the column), so the FIRST call here stands in for "the database an existing
# deploy already carries" and the SECOND stands in for "the next boot, once
# this fix has landed" — the exact upgrade path the spec requires proof of,
# not merely a fresh-create. Today (before the fix) the constraint is
# unconditionally re-declared as the SAME narrow CHECK on every call (no
# DROP+ADD pair exists yet), so the UPDATE below fails with
# ``asyncpg.CheckViolationError`` — the real defect this test pins.


@pytest.mark.asyncio
async def test_shortlist_state_check_accepts_ranking_after_init_schema_run_twice(
    pg_dsn: str,
) -> None:
    """The upgrade path: a database that already carries the OLD
    awaiting_llm-only constraint (first ``init_schema`` call, standing in for
    an already-migrated volume) must accept ``'ranking'`` once ``init_schema``
    runs again (the next boot, carrying the widened CHECK)."""
    connection = await asyncpg.connect(pg_dsn)
    try:
        await init_schema(connection)  # "already deployed" — old constraint
        await init_schema(connection)  # next boot — must widen it

        job_id = await _insert_job(connection)
        await connection.execute(
            "UPDATE jobs SET shortlist_state = 'ranking', shortlist_state_at = now() "
            "WHERE id = $1",
            job_id,
        )
        row = await connection.fetchrow(
            "SELECT shortlist_state FROM jobs WHERE id = $1", job_id
        )
        assert row is not None
        assert row["shortlist_state"] == "ranking"
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_shortlist_state_check_still_accepts_awaiting_llm_after_widening(
    pg_dsn: str,
) -> None:
    """Widening must not be a REPLACEMENT — the pre-existing fail-closed
    ``awaiting_llm`` value (FU-7 §2) must still round-trip after the CHECK is
    widened to also allow ``'ranking'``."""
    connection = await asyncpg.connect(pg_dsn)
    try:
        await init_schema(connection)
        await init_schema(connection)

        job_id = await _insert_job(connection)
        await connection.execute(
            "UPDATE jobs SET shortlist_state = 'awaiting_llm', "
            "shortlist_state_at = now() WHERE id = $1",
            job_id,
        )
        row = await connection.fetchrow(
            "SELECT shortlist_state FROM jobs WHERE id = $1", job_id
        )
        assert row is not None
        assert row["shortlist_state"] == "awaiting_llm"
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_shortlist_state_check_still_rejects_an_arbitrary_value(
    pg_dsn: str,
) -> None:
    """Widening to exactly two legal values, not opening the column up
    entirely — a third, unrelated string must still violate the CHECK."""
    connection = await asyncpg.connect(pg_dsn)
    try:
        await init_schema(connection)
        await init_schema(connection)

        job_id = await _insert_job(connection)
        with pytest.raises(asyncpg.CheckViolationError):
            await connection.execute(
                "UPDATE jobs SET shortlist_state = 'bogus' WHERE id = $1", job_id
            )
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_shortlist_state_check_constraint_keeps_its_original_name(
    pg_dsn: str,
) -> None:
    """The DROP+ADD pair must re-create the constraint under the SAME name
    Postgres originally gave it (confirmed live — see the module comment
    above), not leave a stray differently-named constraint behind; otherwise a
    second boot's DROP CONSTRAINT IF EXISTS silently stops finding it and the
    widening becomes a one-shot fluke instead of a re-runnable migration.

    **F11 (PR review, 2026-08-18):** this used to read the constraint name
    via ``fetchval`` — first row only — while claiming to prove no STRAY
    differently-named constraint survives. ``fetchval`` cannot prove that: a
    second, differently-named constraint sitting in a later row is invisible
    to it. Reading every matching row via ``fetch`` and asserting there is
    exactly one makes the assertion match the docstring's claim.
    """
    connection = await asyncpg.connect(pg_dsn)
    try:
        await init_schema(connection)
        await init_schema(connection)

        rows = await connection.fetch("""
            SELECT conname FROM pg_constraint
            WHERE conrelid = 'jobs'::regclass
              AND pg_get_constraintdef(oid) ILIKE '%shortlist_state%'
            """)
        assert len(rows) == 1, (
            "expected exactly one shortlist_state-related constraint, found "
            f"{len(rows)}: {[r['conname'] for r in rows]}"
        )
        assert rows[0]["conname"] == "jobs_shortlist_state_check"
    finally:
        await connection.close()
