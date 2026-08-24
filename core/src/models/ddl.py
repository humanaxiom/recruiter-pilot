"""Idempotent Postgres startup DDL — the schema comes up with the app.

There is no migration framework: ``init_schema`` runs on every boot of the API
and the worker, so **every statement must be re-runnable**. Ported from the
hris migrations, with the review workflow, the JD-Harmonizer, and the Taleo
ingest columns cut (see docs/EXTRACTION_PLAN.md). CAS auth tables were
originally cut too, but ADR-019 (FU-5) reverses that: ``users`` and
``audit_log`` below are the foundation for attributable audit.

Deviations from hris, all deliberate:

* ``jobs.blind_review`` defaults ``TRUE`` (hris: ``FALSE``) — decision 4.
* ``jobs.created_by`` / ``resumes.uploaded_by`` are nullable ``TEXT`` actor
  labels, not UUID FKs — this predates ADR-019's ``users`` table and is not
  yet wired to it (FU-5 slice 1 is schema only).
* ``score_final`` is ``DOUBLE PRECISION`` in both ranking tables (hris mixed
  ``NUMERIC(5,4)`` and ``DOUBLE PRECISION``, so asyncpg handed back a
  ``Decimal`` from one and a ``float`` from the other). The 0..1 CHECK stays.
* ``reverse_match_entries.rank`` gains the ``> 0`` CHECK its twin already had.

PII (``candidate_name``/``candidate_email``/``candidate_phone``/
``cover_letter_text``) is ``BYTEA``, encrypted at rest with pgcrypto's
``pgp_sym_encrypt`` under the ``app.pii_key`` GUC. Only the *hash* of the email
is plaintext, and only so subject-access requests can find a candidate.

**Concurrent-boot safety (F2/F2b, review findings 2026-08-18).** ``api`` and
``worker`` both call ``init_schema`` and start together with no ``restart:``
policy, so every statement here also has to be safe against a second,
concurrent, freshly-connected caller — not just safe to re-run sequentially.

The ``jobs_shortlist_state_check`` widen (F2) gets a bespoke fix: DROP+ADD
combined into ONE guarded ``DO $$ ... $$`` transaction, so the table-level
lock ``ALTER TABLE`` takes on the (already-existing) ``jobs`` relation
serializes the two boots — see the statement's own comment for why.

Everything else that creates a genuinely NEW catalog object — extensions,
enum types, tables, indexes — turned out to share ONE hazard, bigger than the
single ``pgcrypto`` statement F2b originally named: Postgres's native ``...
IF NOT EXISTS`` clause on ``CREATE EXTENSION``/``CREATE TABLE``/``CREATE
INDEX`` is NOT atomic against a second connection. Measured directly against
a real Postgres (not theorized): two bare connections racing a single
``CREATE TABLE IF NOT EXISTS`` or ``CREATE INDEX IF NOT EXISTS`` against a
table/index that doesn't yet exist raised ``UniqueViolationError`` on
``pg_type_typname_nsp_index`` / ``pg_class_relname_nsp_index`` in **100/100**
trials (50 each) — this is not a rare edge case, it is the deterministic
outcome of two sessions both passing the guard before either commits. The
hand-rolled ``CREATE TYPE ... IF NOT EXISTS (SELECT ...)`` guard blocks race
identically (20/20). By contrast, every statement that ALTERs an EXISTING
relation (``ADD COLUMN IF NOT EXISTS``, the constraint DROP+ADD) raced ZERO
times in the same measurement — Postgres's own lock on the existing relation
serializes those for free, which is exactly the mechanism F2's fix above
relies on.

Given that, the correct, non-"long list" fix is structural, not per-statement:
``init_schema`` below catches the known duplicate-object sqlstates and
retries the SAME statement in a small bounded loop (see
``_MAX_STATEMENT_ATTEMPTS``). After a loser's failed attempt, the winner of
that particular collision has necessarily already committed (the loser's own
conflicting insert only resolves once the winner's transaction ends), so the
very next retry's fresh ``IF NOT EXISTS``/pg_catalog check sees the object
already there and is a true no-op. The attempt cap is a safety stop against
looping forever if something keeps raising a retryable sqlstate — it is not
a claim about, or derived from, how many callers can be racing concurrently,
and nothing here depends on that count.
"""

from __future__ import annotations

from typing import Protocol

import asyncpg


class _Executor(Protocol):
    """The slice of asyncpg's Connection/Pool that the DDL needs."""

    async def execute(self, query: str, *args: object) -> object: ...


_STATEMENTS: tuple[str, ...] = (
    # ── Extensions ───────────────────────────────────────────────────────────
    # pgp_sym_encrypt/pgp_sym_decrypt underpin every PII column.
    #
    # F2b (review findings, 2026-08-18, PRE-EXISTING — found by the F2
    # boot-race test, not introduced on this branch, but fixed here anyway
    # per severity, not provenance): this statement's native `IF NOT EXISTS`
    # is not atomic against a second connection racing the same create — see
    # the module docstring's "Concurrent-boot safety" section for the
    # measurement and why the fix lives centrally in `init_schema` (a
    # catch-and-retry-once around every statement) rather than here. This
    # statement itself is left in its plain, natural form.
    "CREATE EXTENSION IF NOT EXISTS pgcrypto",
    # ── Enums (no IF NOT EXISTS for CREATE TYPE — guard on pg_type) ──────────
    # F2b audit: these two blocks have the EXACT SAME concurrent-boot hazard
    # as the pgcrypto extension above (and, it turned out, as every other
    # brand-new-catalog-object create in this file — see the module
    # docstring). Covered by the same central retry-once fix; no per-statement
    # change needed here either.
    """
    DO $$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'job_status') THEN
            CREATE TYPE job_status AS ENUM ('draft', 'open', 'closed', 'archived');
        END IF;
    END
    $$
    """,
    """
    DO $$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'resume_status') THEN
            CREATE TYPE resume_status
                AS ENUM ('uploaded', 'parsing', 'parsed', 'failed');
        END IF;
    END
    $$
    """,
    # ── jobs ─────────────────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS jobs (
        id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        title             TEXT NOT NULL,
        department        TEXT,
        location          TEXT,
        employment_type   TEXT,
        seniority         TEXT,
        min_years         INTEGER,
        description_raw   TEXT NOT NULL,
        description_sha256 TEXT,
        description_parsed JSONB,
        status            job_status NOT NULL DEFAULT 'draft',
        retention_days    INTEGER NOT NULL DEFAULT 180
                          CHECK (retention_days BETWEEN 30 AND 730),
        shortlist_top_percent INTEGER NOT NULL DEFAULT 100
                          CHECK (shortlist_top_percent BETWEEN 1 AND 100),
        blind_review      BOOLEAN NOT NULL DEFAULT TRUE,
        failure_reason    TEXT,
        created_by        TEXT,
        created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
        parsed_at         TIMESTAMPTZ,
        closed_at         TIMESTAMPTZ
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS jobs_status_open_idx ON jobs (status)
        WHERE status IN ('draft', 'open')
    """,
    "CREATE INDEX IF NOT EXISTS jobs_created_at_idx ON jobs (created_at DESC)",
    # FU-3 Slice 4 (bulk-JD dedup): ``description_sha256`` is the dedup key. This
    # is the FIRST use of ALTER in the port — ``CREATE TABLE IF NOT EXISTS`` is a
    # NO-OP against an already-existing dev/CI Postgres volume, so the column
    # added to the CREATE above would silently never appear on those volumes and
    # the first dedup query would 500. A separate idempotent ALTER guarantees the
    # column lands on both fresh and existing databases. New convention: when a
    # column is added to an existing table, add BOTH the CREATE-TABLE column and a
    # matching ``ADD COLUMN IF NOT EXISTS`` here.
    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS description_sha256 TEXT",
    # The dedup lookup (``WHERE description_sha256 = $1``). Partial so it skips
    # rows predating the column (NULLs) — the ALTER back-fills nothing.
    """
    CREATE INDEX IF NOT EXISTS jobs_description_sha256_idx
        ON jobs (description_sha256)
        WHERE description_sha256 IS NOT NULL
    """,
    # Per-job configurable shortlist cap (slice A, schema only). Same
    # already-migrated-volume risk as description_sha256 above: CREATE TABLE
    # IF NOT EXISTS is a no-op against an existing dev/CI volume, so a
    # separate idempotent ALTER guarantees the column lands there too. There
    # is no idempotent-CHECK clause in Postgres pre-15 (no
    # ``ADD CONSTRAINT IF NOT EXISTS``), so the 1-100 CHECK rides inline on
    # the ADD COLUMN clause itself — the whole clause is skipped once the
    # column exists, which keeps this statement safely re-runnable.
    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS shortlist_top_percent INTEGER "
    "NOT NULL DEFAULT 100 CHECK (shortlist_top_percent BETWEEN 1 AND 100)",
    # FU-7 §2 (ADR-021 §2 / ADR-029) — fail-closed shortlist ranking state. A
    # DEDICATED, nullable ``shortlist_state``/``shortlist_state_reason``/
    # ``shortlist_state_at`` trio — NOT a new ``job_status`` enum value (that
    # stays the draft/open/closed/archived lifecycle; conflating a transient
    # ranking-retry state with it would overload the state machine, mirroring
    # the ADR-026 withdrawal-columns reasoning). Same already-migrated-volume
    # risk as ``shortlist_top_percent`` above, so each lands via an idempotent
    # ALTER. All nullable with NO default so every pre-existing row reads back
    # "no state" the instant the ALTER lands. The 1-value CHECK rides inline on
    # the ADD COLUMN (no ``ADD CONSTRAINT IF NOT EXISTS`` pre-15) — the whole
    # clause is skipped once the column exists, keeping it safely re-runnable.
    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS shortlist_state TEXT "
    "CHECK (shortlist_state IN ('awaiting_llm'))",
    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS shortlist_state_reason TEXT",
    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS shortlist_state_at TIMESTAMPTZ",
    # fix/regenerate-shortlist-no-feedback — widen the CHECK to also allow
    # 'ranking' (set by the API route at enqueue, cleared/overwritten by the
    # worker on every terminal path — see shortlist_service.py /
    # matching_tasks.py). The inline CHECK above already shipped on deployed
    # volumes under the auto-generated name Postgres gave it (confirmed live
    # against the running dev stack: ``jobs_shortlist_state_check``), and
    # there is no ``ADD CONSTRAINT IF NOT EXISTS`` pre-Postgres-15, so
    # widening it needs an explicit DROP-then-ADD.
    #
    # F2 (review findings, 2026-08-18): this used to be a bare DROP-then-ADD
    # pair as TWO separate statements/autocommit transactions. That is safely
    # idempotent for one boot running alone, but ``api`` and ``worker`` both
    # call ``init_schema`` and start together with no ``restart:`` policy, so
    # two connections can each pass the DROP (both succeed — "IF EXISTS") and
    # then race the bare ADD CONSTRAINT: Postgres's own uniqueness check on
    # the constraint name lets only one land, and the loser raises asyncpg
    # 42710 (duplicate_object) and the container stays exited. Reproduced
    # 25/25 in the boot-race test written for this finding
    # (core/tests/integration/test_ddl_boot_race_pg.py).
    #
    # The fix wraps DROP+ADD in ONE ``DO $$ ... $$`` block guarded by a
    # ``pg_constraint`` existence check for the WIDENED definition
    # specifically (matched via ``pg_get_constraintdef(...) LIKE '%ranking%'``
    # — the narrower pre-widen constraint of the same name must NOT satisfy
    # this guard, or the widen would never fire on an old volume). This MUST
    # stay ONE statement, not two — a ``DO`` block is one statement, hence one
    # implicit transaction, so Postgres's row lock on the ``jobs`` catalog
    # entry (taken by the first ALTER inside it) is held across BOTH the DROP
    # and the ADD; the second connection blocks on that lock until the first
    # commits, then re-evaluates its own guard, sees the widened constraint
    # already exists, and does nothing. Splitting this back into two
    # statements (as a "simplification") reopens exactly this race — the lock
    # from a standalone ALTER releases at the end of THAT statement, not at
    # the end of the pair, so a second connection can interleave between them
    # again. It is also a true no-op on every later boot (the guard is
    # already satisfied), which avoids revalidating the CHECK against every
    # row under ACCESS EXCLUSIVE on each restart.
    """
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conrelid = 'jobs'::regclass
              AND conname = 'jobs_shortlist_state_check'
              AND pg_get_constraintdef(oid) LIKE '%ranking%'
        ) THEN
            ALTER TABLE jobs DROP CONSTRAINT IF EXISTS jobs_shortlist_state_check;
            ALTER TABLE jobs ADD CONSTRAINT jobs_shortlist_state_check
                CHECK (shortlist_state IN ('awaiting_llm', 'ranking'));
        END IF;
    END
    $$
    """,
    # ── resumes ──────────────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS resumes (
        id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        job_id               UUID NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
        blob_key             TEXT NOT NULL,
        original_filename    TEXT NOT NULL,
        mime_type            TEXT NOT NULL,
        file_size_bytes      BIGINT NOT NULL CHECK (file_size_bytes >= 0),
        sha256               TEXT NOT NULL,
        candidate_name       BYTEA,
        candidate_email      BYTEA,
        candidate_phone      BYTEA,
        candidate_email_hash TEXT,
        parsed               JSONB,
        status               resume_status NOT NULL DEFAULT 'uploaded',
        uploaded_by          TEXT,
        uploaded_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
        parsed_at            TIMESTAMPTZ,
        failure_reason       TEXT,
        consent_acknowledged BOOLEAN NOT NULL,
        cover_letter_blob_key TEXT,
        cover_letter_text    BYTEA,
        cover_letter_parsed  JSONB,
        cover_letter_sha256  TEXT,
        UNIQUE (job_id, sha256)
    )
    """,
    "CREATE INDEX IF NOT EXISTS resumes_job_status_idx ON resumes (job_id, status)",
    """
    CREATE INDEX IF NOT EXISTS resumes_email_hash_idx ON resumes (candidate_email_hash)
        WHERE candidate_email_hash IS NOT NULL
    """,
    "CREATE INDEX IF NOT EXISTS resumes_uploaded_at_idx ON resumes (uploaded_at DESC)",
    """
    CREATE INDEX IF NOT EXISTS resumes_has_cover_idx ON resumes (job_id)
        WHERE cover_letter_blob_key IS NOT NULL OR cover_letter_text IS NOT NULL
    """,
    # ── resumes / withdrawal lifecycle (ADR-026 decision 1, FU-8) ────────────
    # A DEDICATED, nullable ``withdrawn_at``/``withdrawal_reason`` pair — NOT a
    # fifth ``resume_status`` enum value (that stays ('uploaded','parsing',
    # 'parsed','failed')): conflating application state with the parse-outcome
    # state machine (ADR-021) would erase the parse result. Same already-
    # migrated-volume risk as ``description_sha256``/``shortlist_top_percent``
    # above — ``CREATE TABLE IF NOT EXISTS`` is a no-op on an existing dev/CI
    # volume — so both columns land via a separate idempotent ALTER. Both stay
    # nullable with NO default so every pre-existing row reads back
    # ``withdrawn_at IS NULL`` (not withdrawn) the instant the ALTER lands.
    "ALTER TABLE resumes ADD COLUMN IF NOT EXISTS withdrawn_at TIMESTAMPTZ",
    "ALTER TABLE resumes ADD COLUMN IF NOT EXISTS withdrawal_reason TEXT",
    # How many times `worker.reconcile.reconcile_stalled_parses` has re-queued
    # this row. Nullable with NO default so every pre-existing row reads back as
    # NULL and is COALESCEd to 0 — a row stranded since July gets a full quota
    # of attempts the moment the reconciler first runs, rather than being
    # abandoned on sight.
    #
    # It exists to BOUND the retry, not to enable it: `extract.py` warns that a
    # row which crashes before reaching a terminal state would otherwise be
    # re-queued forever, burning an LLM pass each time on a peer shared with
    # other systems.
    "ALTER TABLE resumes ADD COLUMN IF NOT EXISTS reconcile_attempts INTEGER",
    # Powers the per-job status breakdown's ``withdrawn`` bucket and any
    # "excluded résumés" listing — partial so it only indexes the rare
    # withdrawn rows.
    """
    CREATE INDEX IF NOT EXISTS resumes_withdrawn_idx ON resumes (job_id)
        WHERE withdrawn_at IS NOT NULL
    """,
    # ── shortlist_entries ────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS shortlist_entries (
        id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        job_id          UUID NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
        resume_id       UUID NOT NULL REFERENCES resumes (id) ON DELETE CASCADE,
        rank            INTEGER NOT NULL CHECK (rank > 0),
        score_final     DOUBLE PRECISION NOT NULL
                        CHECK (score_final BETWEEN 0 AND 1),
        score_breakdown JSONB NOT NULL,
        evidence        JSONB NOT NULL,
        pipeline_meta   JSONB NOT NULL,
        generated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (job_id, resume_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS shortlist_job_rank_idx
        ON shortlist_entries (job_id, rank)
    """,
    # ── reverse_match_entries ────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS reverse_match_entries (
        id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        resume_id         UUID NOT NULL REFERENCES resumes (id) ON DELETE CASCADE,
        job_id            UUID NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
        rank              INTEGER NOT NULL CHECK (rank > 0),
        score_final       DOUBLE PRECISION NOT NULL
                          CHECK (score_final BETWEEN 0 AND 1),
        score_structured  DOUBLE PRECISION NOT NULL,
        score_evidence    DOUBLE PRECISION NOT NULL,
        score_breakdown   JSONB NOT NULL,
        evidence          JSONB,
        requirement_count INTEGER NOT NULL,
        must_have_count   INTEGER NOT NULL,
        pipeline_meta     JSONB NOT NULL,
        generated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    # The DELETE + re-INSERT-per-run upsert target.
    """
    CREATE UNIQUE INDEX IF NOT EXISTS reverse_match_entries_resume_job_idx
        ON reverse_match_entries (resume_id, job_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS reverse_match_entries_resume_idx
        ON reverse_match_entries (resume_id, rank)
    """,
    # ── outbox (polymorphic aggregate id — deliberately no FK) ───────────────
    """
    CREATE TABLE IF NOT EXISTS outbox (
        id                BIGSERIAL PRIMARY KEY,
        aggregate         TEXT NOT NULL,
        aggregate_id      UUID NOT NULL,
        event_type        TEXT NOT NULL,
        payload           JSONB NOT NULL,
        created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
        delivered_at      TIMESTAMPTZ,
        delivery_attempts INTEGER NOT NULL DEFAULT 0,
        last_error        TEXT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS outbox_undelivered_idx ON outbox (id)
        WHERE delivered_at IS NULL
    """,
    """
    CREATE INDEX IF NOT EXISTS outbox_aggregate_idx
        ON outbox (aggregate, aggregate_id)
    """,
    # FU-1 (audited reveal): an append-only audit sink. Revealing a candidate's
    # identity is the de-anonymization action, so every reveal writes one row
    # here. Never UPDATEd or DELETEd by app code. `actor` is sourced from the
    # CAS session identity as of FU-5 slice 7 (ADR-019 §8.3/§9.2), via the new
    # `users`/`audit_log` tables below. This table is kept read-only per
    # ADR-019 §6 — no data migration into `audit_log`.
    """
    CREATE TABLE IF NOT EXISTS reveal_audit (
        id          UUID PRIMARY KEY,
        resume_id   UUID NOT NULL,
        job_id      UUID,
        actor       TEXT,
        context     TEXT,
        revealed_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS reveal_audit_resume_idx
        ON reveal_audit (resume_id, revealed_at DESC)
    """,
    # ── users (ADR-019 §1, FU-5 slice 1: schema only) ────────────────────────
    # Real identity entity: a row per CAS-authenticated person. Append-mostly —
    # rows are created and updated (display_name/email/last_seen_at refresh on
    # login), never deleted, so historical audit_log rows stay attributable.
    """
    CREATE TABLE IF NOT EXISTS users (
        id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        cas_username TEXT NOT NULL UNIQUE,
        display_name TEXT,
        email        TEXT,
        role         TEXT,
        active       BOOLEAN NOT NULL DEFAULT true,
        created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
        last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    # ADR-019 §10a: a human-directed reversal of this same ADR's §2 step 3 /
    # §4, which originally required ``role TEXT NOT NULL DEFAULT 'recruiter'``
    # so a first CAS login would default to the recruiter role. The
    # provisioning INSERT still writes 'recruiter'/'admin' explicitly today
    # (FU-5 slice 1 is schema-only; flipping provisioning to omit role is
    # slice 2) — this pair of ALTERs only makes storing NULL *possible*. Same
    # already-migrated-volume risk as description_sha256/shortlist_top_percent
    # above: CREATE TABLE IF NOT EXISTS is a no-op against an existing dev/CI
    # volume that already has the NOT NULL DEFAULT 'recruiter' column, so both
    # ALTERs are required to relax it there too. Both are no-ops on re-run and
    # never touch existing row data.
    "ALTER TABLE users ALTER COLUMN role DROP DEFAULT",
    "ALTER TABLE users ALTER COLUMN role DROP NOT NULL",
    # ── audit_log (ADR-019 §1.4/§6, FU-5 slice 1: schema only) ───────────────
    # Generalized, append-only audit sink replacing reveal-only reveal_audit.
    # Every row names EXACTLY ONE actor: a human (actor_kind='user', with
    # actor_user_id set and actor_service NULL) or a service (actor_kind=
    # 'service', with actor_service set and actor_user_id NULL) — enforced by
    # the CHECK constraint below, not by column-level NOT NULL.
    """
    CREATE TABLE IF NOT EXISTS audit_log (
        id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        actor_kind    TEXT NOT NULL,
        actor_user_id UUID REFERENCES users (id) ON DELETE RESTRICT,
        actor_service TEXT,
        action        TEXT NOT NULL,
        subject_type  TEXT NOT NULL,
        subject_id    UUID NOT NULL,
        job_id        UUID,
        context       TEXT,
        details       JSONB,
        occurred_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT audit_log_actor_identity CHECK (
            (actor_kind = 'user' AND actor_user_id IS NOT NULL
                AND actor_service IS NULL)
            OR
            (actor_kind = 'service' AND actor_user_id IS NULL
                AND actor_service IS NOT NULL)
        )
    )
    """,
    # ── sessions (ADR-019 §10 step 4, FU-5 slice 3) ──────────────────────────
    # The ported hris session store: an opaque, server-held session row behind
    # an httpOnly cookie. ``id`` is TEXT (a ``secrets.token_urlsafe(32)``
    # value generated by the app), not UUID like every other primary key here.
    # ``ON DELETE CASCADE`` on ``user_id`` is deliberate: deleting/deprovisioning
    # a user removes their sessions too — the revocation-on-deprovision
    # behaviour hris relied on.
    """
    CREATE TABLE IF NOT EXISTS sessions (
        id          TEXT PRIMARY KEY,
        user_id     UUID NOT NULL REFERENCES users (id) ON DELETE CASCADE,
        expires_at  TIMESTAMPTZ NOT NULL,
        revoked_at  TIMESTAMPTZ,
        user_agent  TEXT,
        ip_addr     INET,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS sessions_user_id_idx ON sessions (user_id)",
    # ADR-019 §10 step 5: the sliding-window active-session lookup — partial
    # so it only ever covers unrevoked sessions.
    """
    CREATE INDEX IF NOT EXISTS sessions_active_idx ON sessions (expires_at)
        WHERE revoked_at IS NULL
    """,
    # ── job_assignees (ADR-020 §1, FU-6 slice 1) ─────────────────────────────
    # Per-job assignment / row-level scoping: links a user to a single job,
    # with an attributable ``assigned_by``. PK is the (job_id, user_id) pair —
    # a user may be assigned to each job at most once. ``assigned_by`` uses
    # ON DELETE RESTRICT (not CASCADE like job_id/user_id): if the assigning
    # user is deleted, orphaned assignments are not silently cascaded away;
    # the DELETE fails until the assignment is explicitly cleared first — a
    # safety guard against accidental admin-account cleanup erasing
    # delegation records.
    """
    CREATE TABLE IF NOT EXISTS job_assignees (
        job_id       UUID NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
        user_id      UUID NOT NULL REFERENCES users (id) ON DELETE CASCADE,
        assigned_by  UUID NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
        assigned_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (job_id, user_id)
    )
    """,
    # ADR-020 §1: powers the fast "all jobs for this user" query.
    """
    CREATE INDEX IF NOT EXISTS job_assignees_user_idx
        ON job_assignees (user_id, assigned_at DESC)
    """,
)


# Sqlstates that mean "two connections both tried to create the same
# brand-new catalog object" — see the module docstring's "Concurrent-boot
# safety" section for the measurement behind this list. Deliberately a
# whitelist, not a broad class match (e.g. not "any 42xxx"): those classes
# also contain genuine syntax/programming errors this must NOT swallow.
_RETRYABLE_DUPLICATE_SQLSTATES = frozenset(
    {
        "23505",  # unique_violation — e.g. pg_type/pg_class/pg_extension name races
        "42710",  # duplicate_object — e.g. CREATE TYPE "already exists"
        "42P06",  # duplicate_schema
        "42P07",  # duplicate_table
        "42701",  # duplicate_column
        "42723",  # duplicate_function
    }
)

# How many times a single statement may be attempted in total (the first try
# plus retries) before a retryable duplicate-object error is allowed to
# propagate. This is a safety stop, not a correctness argument: a single
# retry normally already succeeds (see the module docstring's "Concurrent-
# boot safety" section for why), so this only needs headroom above 2 to
# absorb more than one competing creator without asserting an exact count of
# concurrent callers anywhere. Kept as a small, named constant rather than a
# magic number so the bound is visible and changeable in one place.
_MAX_STATEMENT_ATTEMPTS = 5


async def init_schema(conn: _Executor) -> None:
    """Create the schema. Idempotent — safe to run on every boot, INCLUDING
    multiple boots (e.g. ``api`` and ``worker``, or any other concurrent
    caller) running it concurrently against the same, possibly brand-new,
    database.

    Executes against whatever it is handed: an asyncpg ``Connection`` (the
    integration tests, the worker startup) or a ``Pool`` (the API lifespan).

    Every statement is retried, up to ``_MAX_STATEMENT_ATTEMPTS`` attempts in
    total, if it raises one of ``_RETRYABLE_DUPLICATE_SQLSTATES`` — see the
    module docstring for why this is the correct fix (measured, not
    assumed). Each retry re-runs the same statement after a competing
    creator's transaction has committed, so the object it was racing to
    create (or the guard it was checking) is now genuinely there and the
    retry is a true no-op. The attempt cap exists only to stop the loop if
    something keeps raising a retryable sqlstate; it is not a statement
    about, or derived from, how many callers can be concurrent. Any other
    exception propagates immediately and un-retried: this must never mask a
    real bug in the DDL.
    """
    for statement in _STATEMENTS:
        for attempt in range(1, _MAX_STATEMENT_ATTEMPTS + 1):
            try:
                await conn.execute(statement)
                break
            except asyncpg.PostgresError as exc:
                if exc.sqlstate not in _RETRYABLE_DUPLICATE_SQLSTATES:
                    raise
                if attempt == _MAX_STATEMENT_ATTEMPTS:
                    raise
                # The other connection's conflicting create only resolves to
                # a committed row once ITS transaction ends — which is
                # exactly the event that unblocked ours and produced this
                # error — so by this point the object genuinely exists and
                # the retry is a true no-op (or, for the hand-rolled `IF NOT
                # EXISTS (SELECT ...)` enum guards, the guard now correctly
                # sees it and skips). Loop around and try again rather than
                # recursing.
