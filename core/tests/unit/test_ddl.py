"""Unit tests for the Postgres startup DDL (``src/models/ddl.py``).

The DDL is the schema contract for the whole port. These tests pin:

* **idempotency** — every ``CREATE`` carries ``IF NOT EXISTS`` so the app can
  run ``init_schema`` on every boot (integration proves it for real),
* **scope** — the ported tables exist (now including ``users``/``audit_log``
  per ADR-019, FU-5 slice 1 — schema only, ``sessions`` per ADR-019 §10
  step 4, FU-5 slice 3, and ``job_assignees`` per ADR-020 §1, FU-6 slice 1)
  and the still-cut ones (review workflow, JD-Harmonizer) cannot creep back
  in,
* **PII at rest** — name/email/phone/cover-letter-text are ``BYTEA``
  (pgcrypto), and only the email *hash* is plaintext,
* **blind review ON by default** (decision 4),
* **attributable audit** — ``audit_log`` carries an actor-identity CHECK
  distinguishing a human actor from a service actor (ADR-019 §1.4/§6).
* **résumé withdrawal lifecycle** (ADR-026 decision 1, FU-8) — a dedicated,
  nullable ``withdrawn_at``/``withdrawal_reason`` pair on ``resumes``, NOT a
  new terminal value on the ``resume_status`` enum.

Contract with the implementation: ``init_schema`` awaits ``.execute(stmt)`` on
the object it is handed (an asyncpg ``Connection`` or ``Pool``) once per
statement, in ``_STATEMENTS`` order.

The ``sessions``-table tests below (ADR-019 §10 step 4) are, like the rest of
this module, string-matches against the DDL source text: they can prove a
column is *named* with a given type/constraint keyword, but NOT that
Postgres actually enforces it (a real FK violation, a real CASCADE delete, a
real INET-vs-TEXT distinction). That behavioural proof lives in
``tests/integration/test_sessions_pg.py`` against a live testcontainers
Postgres — this module and that one are deliberately complementary, not
redundant.

The ``job_assignees``-table tests below (ADR-020 §1, FU-6 slice 1) are the
same kind of string-match: they can prove a column is declared with a given
type/PK/FK/DEFAULT keyword, but NOT that Postgres actually enforces the
composite PK, the CASCADE on ``job_id``/``user_id``, or the RESTRICT on
``assigned_by`` at INSERT/DELETE time. That behavioural proof lives in
``tests/integration/test_job_assignees_pg.py`` against a live testcontainers
Postgres.

The résumé-withdrawal tests below (ADR-026, FU-8) are the same string-match
kind again: they can prove the two ALTERs are *declared* nullable/no-default,
but not that a real Postgres round-trips ``withdrawn_at IS NULL`` for every
pre-existing row after the ALTER lands. That behavioural proof lives in
``tests/integration/test_schema.py``.
"""

from __future__ import annotations

import inspect
import re
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.models.ddl import _STATEMENTS, init_schema

EXPECTED_TABLES: frozenset[str] = frozenset(
    {
        "jobs",
        "resumes",
        "shortlist_entries",
        "reverse_match_entries",
        "outbox",
        "reveal_audit",
        "users",
        "audit_log",
        "sessions",
        "job_assignees",
    }
)

# Explicitly out of scope: review workflow (hris 0004/0007) and the
# JD-Harmonizer (`jd_*`). See EXTRACTION_PLAN keep/cut boundary.
#
# NOTE: `users` was ALSO listed here originally — ddl.py's module docstring
# (line 6) still says CAS auth tables were "deliberately excluded" from v1.
# ADR-019 (FU-5) explicitly REVERSES that specific cut: a `users` table is
# the foundation for attributable audit (per-person reveal/blind_review
# actor identity). `users` therefore moves OUT of this cut-tables tuple and
# INTO EXPECTED_TABLES above. This is a legitimate RED-phase update
# authorized by ADR-019, not a weakening of an existing assertion.
CUT_TABLES: tuple[str, ...] = (
    "shortlist_decisions",
    "stage_transitions",
    "jd_documents",
    "jd_harmonizations",
)

# Columns cut from hris `jobs`: Taleo ingest, JD-Harmonizer, review workflow.
CUT_JOB_COLUMNS: tuple[str, ...] = (
    "source",
    "external_id",
    "external_url",
    "external_last_seen_at",
    "description_sfu",
    "title_autofilled",
    "approval_required_2nd_review",
)

ENCRYPTED_COLUMNS: tuple[str, ...] = (
    "candidate_name",
    "candidate_email",
    "candidate_phone",
    "cover_letter_text",
)

# JSONB in ddl.py is already correct; this guard stops a silent downgrade to a
# lossy TEXT column. `evidence` holds the per-requirement verified quotes whose
# fabrication is a hard fail from Phase 3 — a TEXT column would accept '{}' and
# ship green, so the guard must fail on TEXT.
JSONB_PAYLOAD_COLUMNS: tuple[str, ...] = (
    "score_breakdown",
    "evidence",
    "pipeline_meta",
)

EXPECTED_INDEXES: tuple[str, ...] = (
    "jobs_status_open_idx",
    "jobs_created_at_idx",
    "jobs_description_sha256_idx",
    "resumes_job_status_idx",
    "resumes_email_hash_idx",
    "resumes_uploaded_at_idx",
    "resumes_has_cover_idx",
    "shortlist_job_rank_idx",
    "reverse_match_entries_resume_job_idx",
    "reverse_match_entries_resume_idx",
    "outbox_undelivered_idx",
    "outbox_aggregate_idx",
    "reveal_audit_resume_idx",
    "sessions_user_id_idx",
    "sessions_active_idx",
    "job_assignees_user_idx",
)

# ADR-019 §1: the minimum column set for `users` — real identity entity.
USERS_EXPECTED_COLUMNS: tuple[str, ...] = (
    "id",
    "cas_username",
    "display_name",
    "email",
    "role",
    "active",
    "created_at",
    "last_seen_at",
)

# ADR-019 §1.4/§6: the minimum column set for the generalized `audit_log`,
# which must be able to record both reveals and `blind_review` flips.
AUDIT_LOG_EXPECTED_COLUMNS: tuple[str, ...] = (
    "id",
    "actor_kind",
    "actor_user_id",
    "actor_service",
    "action",
    "subject_type",
    "subject_id",
    "job_id",
    "context",
    "details",
    "occurred_at",
)

# ADR-019 §10 step 4: the minimum column set for `sessions` — opaque,
# server-held session state behind an httpOnly cookie, ported from hris.
SESSIONS_EXPECTED_COLUMNS: tuple[str, ...] = (
    "id",
    "user_id",
    "expires_at",
    "revoked_at",
    "user_agent",
    "ip_addr",
    "created_at",
)

# ADR-020 §1: the minimum column set for `job_assignees` — links a user to a
# single job, with an attributable `assigned_by`.
JOB_ASSIGNEES_EXPECTED_COLUMNS: tuple[str, ...] = (
    "job_id",
    "user_id",
    "assigned_by",
    "assigned_at",
)

# ADR-019 §4: the Role vocabulary — data now, not code. `recruiter` is what
# provisioning inserts as of FU-5 slice 1 CODE (NOT the DDL default any
# longer: ADR-019 §10a, a human-directed reversal of §2 step 3, drops the
# schema-level DEFAULT/NOT NULL below so a first CAS login can capture NULL;
# slice 2 flips the provisioning INSERT itself to match).
ROLE_VALUES: tuple[str, ...] = ("admin", "recruiter", "hiring_manager", "auditor")


# ── Helpers ────────────────────────────────────────────────────────────────


def _squash(sql: str) -> str:
    """Collapse all whitespace so multi-line DDL can be matched literally."""
    return " ".join(sql.split())


def _all_sql() -> str:
    return _squash(" ".join(_STATEMENTS))


def _created_tables() -> set[str]:
    return {
        m.group(1).lower()
        for m in re.finditer(
            r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+([A-Za-z_][A-Za-z0-9_]*)",
            _all_sql(),
            re.IGNORECASE,
        )
    }


def _table_sql(table: str) -> str:
    """The single CREATE TABLE statement for ``table`` (squashed)."""
    pattern = re.compile(
        rf"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+{table}\b", re.IGNORECASE
    )
    matches = [_squash(s) for s in _STATEMENTS if pattern.search(_squash(s))]
    assert len(matches) == 1, f"expected exactly one CREATE TABLE for {table}"
    return matches[0]


def _statement_index(table: str) -> int:
    pattern = re.compile(
        rf"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+{table}\b", re.IGNORECASE
    )
    for i, stmt in enumerate(_STATEMENTS):
        if pattern.search(_squash(stmt)):
            return i
    raise AssertionError(f"no CREATE TABLE for {table}")


def _fake_conn() -> Any:
    """asyncpg Connection/Pool double: ``execute`` is awaitable, and
    ``async with conn.transaction():`` works if the impl wraps the DDL."""
    conn = MagicMock()
    conn.execute = AsyncMock(return_value="OK")
    return conn


def _executed(conn: Any) -> list[str]:
    return [_squash(call.args[0]) for call in conn.execute.await_args_list]


# ── Idempotency ────────────────────────────────────────────────────────────


def test_statements_is_a_non_empty_tuple() -> None:
    assert isinstance(_STATEMENTS, tuple)
    assert len(_STATEMENTS) > 0
    assert all(isinstance(s, str) and s.strip() for s in _STATEMENTS)


@pytest.mark.parametrize("stmt", _STATEMENTS, ids=range(len(_STATEMENTS)))
def test_every_create_statement_is_idempotent(stmt: str) -> None:
    """Every CREATE TABLE/INDEX/EXTENSION must be re-runnable on each boot."""
    sql = _squash(stmt)
    if re.search(r"CREATE\s+(UNIQUE\s+)?(TABLE|INDEX|EXTENSION)\b", sql, re.IGNORECASE):
        assert re.search(r"IF\s+NOT\s+EXISTS", sql, re.IGNORECASE), sql


def test_enums_are_guarded_by_a_pg_type_existence_check() -> None:
    """Enums have no IF NOT EXISTS — they need the DO $$ ... pg_type guard."""
    for enum in ("job_status", "resume_status"):
        guarded = [
            _squash(s)
            for s in _STATEMENTS
            if f"'{enum}'" in _squash(s) or f"typname = '{enum}'" in _squash(s)
        ]
        assert guarded, f"no guarded CREATE TYPE for {enum}"
        block = " ".join(guarded)
        assert "pg_type" in block
        assert "CREATE TYPE" in block.upper()


def test_job_status_enum_values() -> None:
    sql = _all_sql()
    for value in ("'draft'", "'open'", "'closed'", "'archived'"):
        assert value in sql


def test_resume_status_enum_values() -> None:
    sql = _all_sql()
    for value in ("'uploaded'", "'parsing'", "'parsed'", "'failed'"):
        assert value in sql


# ── Extensions ─────────────────────────────────────────────────────────────


def test_pgcrypto_extension_is_created_first() -> None:
    """pgp_sym_encrypt/decrypt underpin every PII column — it must exist."""
    first = _squash(_STATEMENTS[0])
    assert re.search(
        r"CREATE\s+EXTENSION\s+IF\s+NOT\s+EXISTS\s+pgcrypto", first, re.IGNORECASE
    )


# ── Scope: the expected tables in, the cut ones out ────────────────────────


def test_exactly_the_expected_tables_are_created() -> None:
    assert _created_tables() == EXPECTED_TABLES


@pytest.mark.parametrize("table", CUT_TABLES)
def test_cut_tables_are_not_created(table: str) -> None:
    assert table not in _created_tables()
    assert not re.search(rf"\b{table}\b", _all_sql(), re.IGNORECASE)


def test_no_jd_harmonizer_tables() -> None:
    assert not [t for t in _created_tables() if t.startswith("jd_")]


@pytest.mark.parametrize("column", CUT_JOB_COLUMNS)
def test_cut_job_columns_are_absent(column: str) -> None:
    assert not re.search(rf"\b{column}\b", _table_sql("jobs"), re.IGNORECASE)


def test_no_grant_statements() -> None:
    """hris's app_writer/app_auditor roles are not ported."""
    assert "GRANT" not in _all_sql().upper()


def test_tables_are_created_before_the_tables_that_reference_them() -> None:
    assert (
        _statement_index("jobs")
        < _statement_index("resumes")
        < _statement_index("shortlist_entries")
        < _statement_index("reverse_match_entries")
    )


# ── jobs ───────────────────────────────────────────────────────────────────


def test_jobs_blind_review_defaults_true() -> None:
    """Decision 4 — DEVIATION from hris, whose default was FALSE."""
    assert re.search(
        r"blind_review\s+BOOLEAN\s+NOT\s+NULL\s+DEFAULT\s+TRUE",
        _table_sql("jobs"),
        re.IGNORECASE,
    )


def test_jobs_retention_days_check() -> None:
    sql = _table_sql("jobs")
    assert re.search(
        r"retention_days\s+INTEGER\s+NOT\s+NULL\s+DEFAULT\s+180", sql, re.I
    )
    assert re.search(r"retention_days\s+BETWEEN\s+30\s+AND\s+730", sql, re.IGNORECASE)


def test_jobs_created_by_is_a_nullable_text_actor_label() -> None:
    """DEVIATION: hris had a UUID FK -> users(id); there is no users table."""
    sql = _table_sql("jobs")
    assert re.search(r"created_by\s+TEXT", sql, re.IGNORECASE)
    assert not re.search(r"created_by[^,]*REFERENCES", sql, re.IGNORECASE)


def test_jobs_uses_the_job_status_enum() -> None:
    assert re.search(
        r"status\s+job_status\s+NOT\s+NULL\s+DEFAULT\s+'draft'",
        _table_sql("jobs"),
        re.IGNORECASE,
    )


def test_jobs_description_sha256_in_create_table() -> None:
    """FU-3 Slice 4 dedup key — the column is declared in the CREATE TABLE."""
    assert re.search(r"description_sha256\s+TEXT", _table_sql("jobs"), re.IGNORECASE)


def test_jobs_description_sha256_has_an_idempotent_alter() -> None:
    """RISK 3 — ``CREATE TABLE IF NOT EXISTS`` is a NO-OP against an existing
    dev/CI volume, so the new column would never appear. A separate, idempotent
    ``ALTER TABLE ... ADD COLUMN IF NOT EXISTS`` guarantees it lands. This is a
    new convention (the port has never used ALTER before)."""
    alters = [
        _squash(s)
        for s in _STATEMENTS
        if re.search(
            r"ALTER\s+TABLE\s+jobs\s+ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\s+"
            r"description_sha256\s+TEXT",
            _squash(s),
            re.IGNORECASE,
        )
    ]
    assert len(alters) == 1, "expected exactly one idempotent ALTER for the column"


def test_jobs_description_sha256_alter_runs_after_the_create_table() -> None:
    """The ALTER (and its dependent partial index) must come AFTER the jobs
    CREATE TABLE so the column exists when they run."""
    alter_idx = next(
        i
        for i, s in enumerate(_STATEMENTS)
        if re.search(
            r"ALTER\s+TABLE\s+jobs\s+ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\s+"
            r"description_sha256",
            _squash(s),
            re.IGNORECASE,
        )
    )
    assert _statement_index("jobs") < alter_idx


def test_jobs_description_sha256_index_is_partial() -> None:
    """The dedup-lookup index skips NULLs (rows predating the backfill)."""
    assert re.search(
        r"CREATE\s+INDEX\s+IF\s+NOT\s+EXISTS\s+jobs_description_sha256_idx\s+"
        r"ON\s+jobs\s*\(\s*description_sha256\s*\)\s+"
        r"WHERE\s+description_sha256\s+IS\s+NOT\s+NULL",
        _all_sql(),
        re.IGNORECASE,
    )


# ── jobs.shortlist_top_percent (per-job configurable shortlist cap, slice A) ─
# Today the shortlist persists ALL ranked candidates up to match_coarse_k=50 —
# no fixed cap. shortlist_top_percent (1-100, default 100 = today's "keep
# all") caps the persisted shortlist to the top P% of the ranked pool. THIS
# SLICE is schema-only (the engine cap and the form land in slices B/C). Like
# description_sha256 above, this is a NEW COLUMN on an EXISTING table: both
# the CREATE TABLE column (fresh installs) AND a matching idempotent ALTER
# (already-migrated dev/CI volumes) are required by the FU-3 convention.
#
# There is no idempotent-CHECK precedent anywhere in this DDL (no
# ``ADD CONSTRAINT IF NOT EXISTS`` — that clause does not exist in Postgres
# for CHECK constraints pre-15, and this codebase does not use the DO-block
# guard style for constraints the way it does for enums). So the 1-100 CHECK
# rides inline on the ADD COLUMN clause itself: the WHOLE clause — including
# the constraint — is skipped by IF NOT EXISTS once the column exists, which
# keeps the statement safely idempotent without a separate guarded ALTER.


def test_jobs_shortlist_top_percent_in_create_table() -> None:
    """Fresh installs get the column straight from the CREATE TABLE."""
    assert re.search(
        r"shortlist_top_percent\s+INTEGER\s+NOT\s+NULL\s+DEFAULT\s+100",
        _table_sql("jobs"),
        re.IGNORECASE,
    )


def test_jobs_shortlist_top_percent_check_in_create_table() -> None:
    assert re.search(
        r"shortlist_top_percent\s+BETWEEN\s+1\s+AND\s+100",
        _table_sql("jobs"),
        re.IGNORECASE,
    )


def test_jobs_shortlist_top_percent_has_an_idempotent_alter() -> None:
    """Existing dev/CI volumes already have a `jobs` table — CREATE TABLE IF
    NOT EXISTS is a no-op against them, so only a separate idempotent ALTER
    lands the column there (mirrors description_sha256 above)."""
    alters = [
        _squash(s)
        for s in _STATEMENTS
        if re.search(
            r"ALTER\s+TABLE\s+jobs\s+ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\s+"
            r"shortlist_top_percent\s+INTEGER\s+NOT\s+NULL\s+DEFAULT\s+100",
            _squash(s),
            re.IGNORECASE,
        )
    ]
    assert len(alters) == 1, "expected exactly one idempotent ALTER for the column"


def test_jobs_shortlist_top_percent_alter_includes_the_check() -> None:
    """No idempotent-CHECK precedent exists in this DDL, so the CHECK must
    ride inline on the ADD COLUMN clause — see the section note above."""
    alters = [
        _squash(s)
        for s in _STATEMENTS
        if re.search(
            r"ALTER\s+TABLE\s+jobs\s+ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\s+"
            r"shortlist_top_percent",
            _squash(s),
            re.IGNORECASE,
        )
    ]
    assert alters, "no ALTER for shortlist_top_percent"
    assert re.search(
        r"CHECK\s*\(\s*shortlist_top_percent\s+BETWEEN\s+1\s+AND\s+100\s*\)",
        alters[0],
        re.IGNORECASE,
    ), alters[0]


def test_jobs_shortlist_top_percent_alter_runs_after_the_create_table() -> None:
    alter_idx = next(
        i
        for i, s in enumerate(_STATEMENTS)
        if re.search(
            r"ALTER\s+TABLE\s+jobs\s+ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\s+"
            r"shortlist_top_percent",
            _squash(s),
            re.IGNORECASE,
        )
    )
    assert _statement_index("jobs") < alter_idx


# ── resumes / PII ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("column", ENCRYPTED_COLUMNS)
def test_pii_columns_are_bytea(column: str) -> None:
    """Encrypted at rest via pgcrypto — never plaintext, never embedded."""
    assert re.search(rf"\b{column}\s+BYTEA\b", _table_sql("resumes"), re.IGNORECASE)


def test_candidate_email_hash_is_plaintext_text() -> None:
    """Subject-access lookup key: sha256(lower(trim(email))), not encrypted."""
    sql = _table_sql("resumes")
    assert re.search(r"candidate_email_hash\s+TEXT", sql, re.IGNORECASE)
    assert not re.search(r"candidate_email_hash\s+BYTEA", sql, re.IGNORECASE)


def test_resumes_consent_acknowledged_has_no_default() -> None:
    """Every insert must supply consent explicitly."""
    sql = _table_sql("resumes")
    assert re.search(r"consent_acknowledged\s+BOOLEAN\s+NOT\s+NULL", sql, re.IGNORECASE)
    assert not re.search(
        r"consent_acknowledged\s+BOOLEAN\s+NOT\s+NULL\s+DEFAULT", sql, re.IGNORECASE
    )


def test_resumes_dedupe_constraint() -> None:
    assert re.search(
        r"UNIQUE\s*\(\s*job_id\s*,\s*sha256\s*\)", _table_sql("resumes"), re.IGNORECASE
    )


def test_resumes_cascade_from_jobs() -> None:
    assert re.search(
        r"job_id\s+UUID\s+NOT\s+NULL\s+REFERENCES\s+jobs\s*\(\s*id\s*\)"
        r"\s+ON\s+DELETE\s+CASCADE",
        _table_sql("resumes"),
        re.IGNORECASE,
    )


def test_resumes_file_size_check() -> None:
    assert re.search(
        r"file_size_bytes\s+BIGINT\s+NOT\s+NULL\s+CHECK\s*\(\s*file_size_bytes\s*>=\s*0",
        _table_sql("resumes"),
        re.IGNORECASE,
    )


# ── resumes / withdrawal lifecycle (ADR-026 decision 1, FU-8) ──────────────
#
# ``withdrawn_at TIMESTAMPTZ`` (nullable) + ``withdrawal_reason TEXT``
# (nullable) — a DEDICATED pair on the existing ``resumes`` table, NOT a new
# terminal value on the ``resume_status`` enum (that stays exactly
# ``('uploaded', 'parsing', 'parsed', 'failed')``, unchanged). Same
# already-migrated-volume risk as every other ALTER in this module: a
# separate idempotent ``ALTER TABLE resumes ADD COLUMN IF NOT EXISTS`` is
# required so both columns land on an existing dev/CI volume too, not just a
# fresh CREATE TABLE.


def test_resumes_withdrawn_at_has_an_idempotent_alter() -> None:
    alters = [
        _squash(s)
        for s in _STATEMENTS
        if re.search(
            r"ALTER\s+TABLE\s+resumes\s+ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\s+"
            r"withdrawn_at\s+TIMESTAMPTZ",
            _squash(s),
            re.IGNORECASE,
        )
    ]
    assert len(alters) == 1, "expected exactly one idempotent ALTER for withdrawn_at"


def test_resumes_withdrawn_at_alter_has_no_not_null_or_default() -> None:
    """A withdrawn résumé is the exception, not the rule — the column must
    stay nullable with no default so every pre-existing row still reads back
    ``withdrawn_at IS NULL`` (not withdrawn) once the ALTER lands."""
    alters = [
        _squash(s)
        for s in _STATEMENTS
        if re.search(
            r"ALTER\s+TABLE\s+resumes\s+ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\s+"
            r"withdrawn_at",
            _squash(s),
            re.IGNORECASE,
        )
    ]
    assert alters, "no ALTER for withdrawn_at"
    assert "NOT NULL" not in alters[0].upper()
    assert "DEFAULT" not in alters[0].upper()


def test_resumes_withdrawal_reason_has_an_idempotent_alter() -> None:
    alters = [
        _squash(s)
        for s in _STATEMENTS
        if re.search(
            r"ALTER\s+TABLE\s+resumes\s+ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\s+"
            r"withdrawal_reason\s+TEXT",
            _squash(s),
            re.IGNORECASE,
        )
    ]
    assert (
        len(alters) == 1
    ), "expected exactly one idempotent ALTER for withdrawal_reason"


def test_resumes_withdrawal_reason_alter_has_no_not_null_or_default() -> None:
    alters = [
        _squash(s)
        for s in _STATEMENTS
        if re.search(
            r"ALTER\s+TABLE\s+resumes\s+ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\s+"
            r"withdrawal_reason",
            _squash(s),
            re.IGNORECASE,
        )
    ]
    assert alters, "no ALTER for withdrawal_reason"
    assert "NOT NULL" not in alters[0].upper()
    assert "DEFAULT" not in alters[0].upper()


def test_resumes_withdrawal_alters_run_after_the_resumes_create_table() -> None:
    resumes_idx = _statement_index("resumes")
    withdrawn_at_idx = next(
        i
        for i, s in enumerate(_STATEMENTS)
        if re.search(
            r"ALTER\s+TABLE\s+resumes\s+ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\s+"
            r"withdrawn_at",
            _squash(s),
            re.IGNORECASE,
        )
    )
    withdrawal_reason_idx = next(
        i
        for i, s in enumerate(_STATEMENTS)
        if re.search(
            r"ALTER\s+TABLE\s+resumes\s+ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\s+"
            r"withdrawal_reason",
            _squash(s),
            re.IGNORECASE,
        )
    )
    assert resumes_idx < withdrawn_at_idx
    assert resumes_idx < withdrawal_reason_idx


def test_resume_status_enum_has_no_withdrawn_value() -> None:
    """Decision 1 — withdrawal is a dedicated column pair, NOT a fifth
    ``resume_status`` enum value. A ``'withdrawn'`` literal added to the enum
    definition would conflate processing state with application state and
    erase the parse outcome (ADR-026 §Decision 1)."""
    enum_stmts = [
        _squash(s)
        for s in _STATEMENTS
        if "resume_status" in _squash(s) and "ENUM" in _squash(s).upper()
    ]
    assert enum_stmts, "no resume_status CREATE TYPE statement found"
    assert "'withdrawn'" not in enum_stmts[0]


# ── ranking tables ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("table", ["shortlist_entries", "reverse_match_entries"])
def test_score_final_is_double_precision_bounded_0_to_1(table: str) -> None:
    """DEVIATION: unified on DOUBLE PRECISION so asyncpg never returns Decimal."""
    sql = _table_sql(table)
    assert re.search(r"score_final\s+DOUBLE\s+PRECISION\s+NOT\s+NULL", sql, re.I)
    assert not re.search(r"score_final\s+NUMERIC", sql, re.IGNORECASE)
    assert re.search(r"score_final\s+BETWEEN\s+0\s+AND\s+1", sql, re.IGNORECASE)


@pytest.mark.parametrize("table", ["shortlist_entries", "reverse_match_entries"])
def test_rank_is_positive(table: str) -> None:
    assert re.search(
        r"rank\s+INTEGER\s+NOT\s+NULL\s+CHECK\s*\(\s*rank\s*>\s*0\s*\)",
        _table_sql(table),
        re.IGNORECASE,
    )


@pytest.mark.parametrize("table", ["shortlist_entries", "reverse_match_entries"])
@pytest.mark.parametrize("column", JSONB_PAYLOAD_COLUMNS)
def test_ranking_payload_columns_are_jsonb(table: str, column: str) -> None:
    """The 4-stage ranking output columns must be JSONB, never TEXT.

    Parsed per-table so a downgrade of one table cannot hide behind the other:
    reverse_match_entries is checked independently of shortlist_entries.
    """
    sql = _table_sql(table)
    assert re.search(rf"\b{column}\s+JSONB\b", sql, re.IGNORECASE), sql
    assert not re.search(rf"\b{column}\s+TEXT\b", sql, re.IGNORECASE), sql


def test_shortlist_entries_unique_per_job_resume() -> None:
    assert re.search(
        r"UNIQUE\s*\(\s*job_id\s*,\s*resume_id\s*\)",
        _table_sql("shortlist_entries"),
        re.IGNORECASE,
    )


def test_reverse_match_upsert_target_is_a_unique_index() -> None:
    """The DELETE + re-INSERT-per-run target: UNIQUE (resume_id, job_id)."""
    assert re.search(
        r"CREATE\s+UNIQUE\s+INDEX\s+IF\s+NOT\s+EXISTS\s+"
        r"reverse_match_entries_resume_job_idx\s+ON\s+reverse_match_entries\s*"
        r"\(\s*resume_id\s*,\s*job_id\s*\)",
        _all_sql(),
        re.IGNORECASE,
    )


# ── outbox ─────────────────────────────────────────────────────────────────


def test_outbox_aggregate_id_has_no_foreign_key() -> None:
    """Polymorphic aggregate id — an FK would break job/resume reuse."""
    sql = _table_sql("outbox")
    assert re.search(r"aggregate_id\s+UUID\s+NOT\s+NULL", sql, re.IGNORECASE)
    assert "REFERENCES" not in sql.upper()
    assert re.search(r"id\s+BIGSERIAL\s+PRIMARY\s+KEY", sql, re.IGNORECASE)


# ── users / audit_log (ADR-019, FU-5 slice 1: schema only) ─────────────────


@pytest.mark.parametrize("column", USERS_EXPECTED_COLUMNS)
def test_users_table_has_expected_columns(column: str) -> None:
    assert re.search(rf"\b{column}\b", _table_sql("users"), re.IGNORECASE)


def test_users_id_is_uuid_primary_key() -> None:
    assert re.search(r"id\s+UUID\s+PRIMARY\s+KEY", _table_sql("users"), re.IGNORECASE)


def test_users_cas_username_is_unique_and_not_null() -> None:
    """ADR-019 §1 — the unique, immutable identity from CAS."""
    sql = _table_sql("users")
    assert re.search(r"cas_username\s+TEXT\s+NOT\s+NULL", sql, re.IGNORECASE)
    assert re.search(r"cas_username[^,]*\bUNIQUE\b", sql, re.IGNORECASE)


def test_users_role_is_nullable_with_no_default() -> None:
    """ADR-019 §10a — a human-directed reversal of this same ADR's §2 step 3
    / §4, which originally required ``role TEXT NOT NULL DEFAULT
    'recruiter'`` (asserted by this test's now-superseded prior version,
    ``test_users_role_is_not_null``). First CAS login must be able to
    capture a user with NO role, so the CREATE TABLE column for fresh
    installs is bare ``role TEXT`` — nullable, no default. FU-5 slice 1 is
    schema-only: provisioning itself (still inserting 'recruiter'/'admin'
    explicitly) does not change until slice 2."""
    sql = _table_sql("users")
    assert re.search(r"\brole\s+TEXT\b", sql, re.IGNORECASE)
    assert not re.search(r"role\s+TEXT\s+NOT\s+NULL", sql, re.IGNORECASE)
    assert not re.search(r"role[^,]*DEFAULT\s+'recruiter'", sql, re.IGNORECASE)


def test_users_role_drop_default_alter_is_present() -> None:
    """ADR-019 §10a (FU-5 slice 1): a LIVE dev/CI volume already has
    ``role TEXT NOT NULL DEFAULT 'recruiter'`` from before this reversal, and
    ``CREATE TABLE IF NOT EXISTS`` is a no-op against it — the same
    already-migrated-volume risk the ``description_sha256``/
    ``shortlist_top_percent`` ALTERs above guard against. A separate
    idempotent ``ALTER ... DROP DEFAULT`` is required so existing volumes'
    columns are relaxed too, not just fresh installs' CREATE TABLE."""
    alters = [
        _squash(s)
        for s in _STATEMENTS
        if re.search(
            r"ALTER\s+TABLE\s+users\s+ALTER\s+COLUMN\s+role\s+DROP\s+DEFAULT",
            _squash(s),
            re.IGNORECASE,
        )
    ]
    assert len(alters) == 1, "exactly one idempotent DROP DEFAULT ALTER for role"


def test_users_role_drop_not_null_alter_is_present() -> None:
    """Companion to the DROP DEFAULT ALTER above — both are required to make
    NULL actually insertable; dropping only the default would still reject a
    NULL insert with a NOT NULL violation."""
    alters = [
        _squash(s)
        for s in _STATEMENTS
        if re.search(
            r"ALTER\s+TABLE\s+users\s+ALTER\s+COLUMN\s+role\s+DROP\s+NOT\s+NULL",
            _squash(s),
            re.IGNORECASE,
        )
    ]
    assert len(alters) == 1, "exactly one idempotent DROP NOT NULL ALTER for role"


def test_users_role_alters_run_after_the_users_create_table() -> None:
    """Postgres would reject ``ALTER TABLE users ...`` if the table did not
    exist yet — the relaxation ALTERs must come after the CREATE TABLE."""
    drop_default_idx = next(
        i
        for i, s in enumerate(_STATEMENTS)
        if re.search(
            r"ALTER\s+TABLE\s+users\s+ALTER\s+COLUMN\s+role\s+DROP\s+DEFAULT",
            _squash(s),
            re.IGNORECASE,
        )
    )
    drop_not_null_idx = next(
        i
        for i, s in enumerate(_STATEMENTS)
        if re.search(
            r"ALTER\s+TABLE\s+users\s+ALTER\s+COLUMN\s+role\s+DROP\s+NOT\s+NULL",
            _squash(s),
            re.IGNORECASE,
        )
    )
    users_idx = _statement_index("users")
    assert users_idx < drop_default_idx
    assert users_idx < drop_not_null_idx


def test_users_active_flag_defaults_true() -> None:
    """ADR-019 §1 — deprovisioning without deletion; new rows are active."""
    assert re.search(
        r"active\s+BOOLEAN\s+NOT\s+NULL\s+DEFAULT\s+true",
        _table_sql("users"),
        re.IGNORECASE,
    )


def test_users_display_name_and_email_are_nullable() -> None:
    """ADR-019 Accepted residuals — CAS may release neither attribute."""
    sql = _table_sql("users")
    assert not re.search(r"display_name\s+TEXT\s+NOT\s+NULL", sql, re.IGNORECASE)
    assert not re.search(r"\bemail\s+TEXT\s+NOT\s+NULL", sql, re.IGNORECASE)


def test_users_last_seen_at_defaults_to_now() -> None:
    """Refreshed on every login (ADR-019 §2 step 3); must default on create."""
    assert re.search(
        r"last_seen_at\s+TIMESTAMPTZ\s+NOT\s+NULL\s+DEFAULT\s+now\s*\(\s*\)",
        _table_sql("users"),
        re.IGNORECASE,
    )


@pytest.mark.parametrize("column", AUDIT_LOG_EXPECTED_COLUMNS)
def test_audit_log_table_has_expected_columns(column: str) -> None:
    assert re.search(rf"\b{column}\b", _table_sql("audit_log"), re.IGNORECASE)


def test_audit_log_id_is_uuid_primary_key() -> None:
    assert re.search(
        r"id\s+UUID\s+PRIMARY\s+KEY", _table_sql("audit_log"), re.IGNORECASE
    )


def test_audit_log_action_and_subject_are_not_null() -> None:
    """ADR-019 §6 — required for every row regardless of actor kind."""
    sql = _table_sql("audit_log")
    assert re.search(r"action\s+TEXT\s+NOT\s+NULL", sql, re.IGNORECASE)
    assert re.search(r"subject_type\s+TEXT\s+NOT\s+NULL", sql, re.IGNORECASE)
    assert re.search(r"subject_id\s+UUID\s+NOT\s+NULL", sql, re.IGNORECASE)


def test_audit_log_actor_kind_is_not_null() -> None:
    assert re.search(
        r"actor_kind\s+TEXT\s+NOT\s+NULL", _table_sql("audit_log"), re.IGNORECASE
    )


def test_audit_log_actor_user_id_and_actor_service_are_nullable() -> None:
    """ADR-019 §6 — nullable BY DESIGN; exactly one is set per row, enforced
    by the CHECK constraint below, not by column-level NOT NULL."""
    sql = _table_sql("audit_log")
    assert not re.search(r"actor_user_id\s+UUID\s+NOT\s+NULL", sql, re.IGNORECASE)
    assert not re.search(r"actor_service\s+TEXT\s+NOT\s+NULL", sql, re.IGNORECASE)


def test_audit_log_details_is_jsonb_not_text() -> None:
    """Accepted residual (ADR-019) — unvalidated JSONB, never a lossy TEXT."""
    sql = _table_sql("audit_log")
    assert re.search(r"\bdetails\s+JSONB\b", sql, re.IGNORECASE)
    assert not re.search(r"\bdetails\s+TEXT\b", sql, re.IGNORECASE)


def test_audit_log_occurred_at_defaults_to_now() -> None:
    assert re.search(
        r"occurred_at\s+TIMESTAMPTZ\s+NOT\s+NULL\s+DEFAULT\s+now\s*\(\s*\)",
        _table_sql("audit_log"),
        re.IGNORECASE,
    )


def test_audit_log_actor_identity_check_constraint_is_present() -> None:
    """ADR-019 §1.4/§6 — a row must name EXACTLY ONE actor: a human
    (actor_kind='user' AND actor_user_id set AND actor_service NULL) or a
    service (actor_kind='service' AND actor_user_id NULL AND actor_service
    set). This is the constraint that distinguishes attributable-human rows
    from service rows and is the whole point of the generalized table."""
    sql = _table_sql("audit_log")
    assert re.search(r"CHECK\s*\(", sql, re.IGNORECASE), sql
    assert re.search(r"actor_kind\s*=\s*'user'", sql, re.IGNORECASE)
    assert re.search(r"actor_kind\s*=\s*'service'", sql, re.IGNORECASE)
    assert re.search(r"actor_user_id\s+IS\s+NOT\s+NULL", sql, re.IGNORECASE)
    assert re.search(r"actor_user_id\s+IS\s+NULL", sql, re.IGNORECASE)
    assert re.search(r"actor_service\s+IS\s+NOT\s+NULL", sql, re.IGNORECASE)
    assert re.search(r"actor_service\s+IS\s+NULL", sql, re.IGNORECASE)


def test_users_table_is_created_before_audit_log() -> None:
    """audit_log's actor_user_id is conceptually keyed to a users row
    (ADR-019 §6); users must exist first."""
    assert _statement_index("users") < _statement_index("audit_log")


# ── sessions (ADR-019 §10 step 4, FU-5 slice 3) ─────────────────────────────
#
# The ported hris session store: an opaque server-held session row behind an
# httpOnly cookie. NOTE (honesty about what this section can and cannot
# prove): these are all string-matches against the DDL source. They can show
# a column is *declared* TEXT/UUID/INET/NOT NULL/REFERENCES-with-CASCADE, but
# NOT that Postgres actually enforces the FK, the CASCADE, the PK uniqueness,
# or the INET type distinction at INSERT time. That behavioural proof is
# integration-only — see tests/integration/test_sessions_pg.py.


@pytest.mark.parametrize("column", SESSIONS_EXPECTED_COLUMNS)
def test_sessions_table_has_expected_columns(column: str) -> None:
    assert re.search(rf"\b{column}\b", _table_sql("sessions"), re.IGNORECASE)


def test_sessions_id_is_a_text_primary_key() -> None:
    """Opaque ``secrets.token_urlsafe(32)`` id — TEXT, not UUID (unlike every
    other primary key in this schema)."""
    assert re.search(
        r"id\s+TEXT\s+PRIMARY\s+KEY", _table_sql("sessions"), re.IGNORECASE
    )


def test_sessions_user_id_references_users_with_cascade() -> None:
    """ON DELETE CASCADE — deprovisioning/deleting a user revokes (removes)
    their sessions, the behaviour hris relied on for revocation-on-deprovision
    (ADR-019 §10 step 4)."""
    assert re.search(
        r"user_id\s+UUID\s+NOT\s+NULL\s+REFERENCES\s+users\s*\(\s*id\s*\)"
        r"\s+ON\s+DELETE\s+CASCADE",
        _table_sql("sessions"),
        re.IGNORECASE,
    )


def test_sessions_expires_at_is_not_null() -> None:
    assert re.search(
        r"expires_at\s+TIMESTAMPTZ\s+NOT\s+NULL",
        _table_sql("sessions"),
        re.IGNORECASE,
    )


def test_sessions_revoked_at_is_nullable() -> None:
    """No NOT NULL — an active/live session has ``revoked_at IS NULL``; this
    is exactly what the partial index below relies on."""
    sql = _table_sql("sessions")
    assert re.search(r"revoked_at\s+TIMESTAMPTZ\b", sql, re.IGNORECASE)
    assert not re.search(r"revoked_at\s+TIMESTAMPTZ\s+NOT\s+NULL", sql, re.IGNORECASE)


def test_sessions_ip_addr_is_inet_not_text() -> None:
    """Must stay the real Postgres INET type, not a plain string — unit-level
    this only proves the DDL says INET; integration proves the driver really
    rejects a non-IP value."""
    sql = _table_sql("sessions")
    assert re.search(r"ip_addr\s+INET\b", sql, re.IGNORECASE)
    assert not re.search(r"ip_addr\s+TEXT\b", sql, re.IGNORECASE)


def test_sessions_created_at_defaults_to_now() -> None:
    assert re.search(
        r"created_at\s+TIMESTAMPTZ\s+NOT\s+NULL\s+DEFAULT\s+now\s*\(\s*\)",
        _table_sql("sessions"),
        re.IGNORECASE,
    )


def test_users_table_is_created_before_sessions() -> None:
    """sessions.user_id FKs to users — users must exist first."""
    assert _statement_index("users") < _statement_index("sessions")


def test_sessions_active_idx_is_partial_on_unrevoked_sessions() -> None:
    """The sliding-window active-session lookup index (ADR-019 §10 step 5):
    ``ON sessions(expires_at) WHERE revoked_at IS NULL``."""
    assert re.search(
        r"CREATE\s+INDEX\s+IF\s+NOT\s+EXISTS\s+sessions_active_idx\s+"
        r"ON\s+sessions\s*\(\s*expires_at\s*\)\s+"
        r"WHERE\s+revoked_at\s+IS\s+NULL",
        _all_sql(),
        re.IGNORECASE,
    )


def test_sessions_user_id_idx_exists() -> None:
    assert re.search(
        r"CREATE\s+INDEX\s+IF\s+NOT\s+EXISTS\s+sessions_user_id_idx\s+"
        r"ON\s+sessions\s*\(\s*user_id\s*\)",
        _all_sql(),
        re.IGNORECASE,
    )


# ── job_assignees (ADR-020 §1, FU-6 slice 1) ────────────────────────────────
#
# Links a user to a single job, with an attributable `assigned_by`. NOTE
# (honesty about what this section can and cannot prove): these are all
# string-matches against the DDL source text. They can show a column is
# *declared* UUID/NOT NULL/REFERENCES-with-CASCADE-or-RESTRICT/DEFAULT, and
# that the composite PRIMARY KEY clause is textually present, but NOT that
# Postgres actually enforces the composite-PK uniqueness, the CASCADE on
# job_id/user_id, or the RESTRICT on assigned_by at INSERT/DELETE time. That
# behavioural proof is integration-only — see
# tests/integration/test_job_assignees_pg.py.


@pytest.mark.parametrize("column", JOB_ASSIGNEES_EXPECTED_COLUMNS)
def test_job_assignees_table_has_expected_columns(column: str) -> None:
    assert re.search(rf"\b{column}\b", _table_sql("job_assignees"), re.IGNORECASE)


def test_job_assignees_primary_key_is_composite_job_and_user() -> None:
    """ADR-020 §1 — a user may be assigned to each job at most once; the PK
    is the pair, not a surrogate id column."""
    sql = _table_sql("job_assignees")
    assert re.search(
        r"PRIMARY\s+KEY\s*\(\s*job_id\s*,\s*user_id\s*\)", sql, re.IGNORECASE
    )
    # And no surrogate `id` column pretending to be the identity.
    assert not re.search(r"\bid\s+UUID\s+PRIMARY\s+KEY\b", sql, re.IGNORECASE)


def test_job_assignees_job_id_references_jobs_with_cascade() -> None:
    """ADR-020 §1 — a job has no hiring managers if it doesn't exist."""
    assert re.search(
        r"job_id\s+UUID\s+NOT\s+NULL\s+REFERENCES\s+jobs\s*\(\s*id\s*\)"
        r"\s+ON\s+DELETE\s+CASCADE",
        _table_sql("job_assignees"),
        re.IGNORECASE,
    )


def test_job_assignees_user_id_references_users_with_cascade() -> None:
    """ADR-020 §1 — deleting/deprovisioning the assigned user removes their
    assignment (mirrors the sessions.user_id CASCADE convention)."""
    assert re.search(
        r"user_id\s+UUID\s+NOT\s+NULL\s+REFERENCES\s+users\s*\(\s*id\s*\)"
        r"\s+ON\s+DELETE\s+CASCADE",
        _table_sql("job_assignees"),
        re.IGNORECASE,
    )


def test_job_assignees_assigned_by_references_users_with_restrict() -> None:
    """ADR-020 §1 — the safety guard: if the assigning admin/recruiter is
    deleted, the assignment record must not be silently cascaded away; the
    DELETE on `users` must fail until the assignment is explicitly cleared."""
    sql = _table_sql("job_assignees")
    assert re.search(
        r"assigned_by\s+UUID\s+NOT\s+NULL\s+REFERENCES\s+users\s*\(\s*id\s*\)"
        r"\s+ON\s+DELETE\s+RESTRICT",
        sql,
        re.IGNORECASE,
    )
    # Distinct from job_id/user_id — this FK must NOT cascade.
    assert not re.search(
        r"assigned_by\s+UUID\s+NOT\s+NULL\s+REFERENCES\s+users\s*\(\s*id\s*\)"
        r"\s+ON\s+DELETE\s+CASCADE",
        sql,
        re.IGNORECASE,
    )


def test_job_assignees_assigned_at_defaults_to_now() -> None:
    assert re.search(
        r"assigned_at\s+TIMESTAMPTZ\s+NOT\s+NULL\s+DEFAULT\s+now\s*\(\s*\)",
        _table_sql("job_assignees"),
        re.IGNORECASE,
    )


def test_job_assignees_user_idx_covers_user_id_and_assigned_at_desc() -> None:
    """ADR-020 §1 — powers the fast 'all jobs for this user' query:
    ``ON job_assignees (user_id, assigned_at DESC)``."""
    assert re.search(
        r"CREATE\s+INDEX\s+IF\s+NOT\s+EXISTS\s+job_assignees_user_idx\s+"
        r"ON\s+job_assignees\s*\(\s*user_id\s*,\s*assigned_at\s+DESC\s*\)",
        _all_sql(),
        re.IGNORECASE,
    )


def test_jobs_and_users_tables_are_created_before_job_assignees() -> None:
    """ADR-020 §1 — job_assignees FKs to BOTH jobs(id) and users(id); both
    parent tables must exist before Postgres can create the child's FKs."""
    job_assignees_idx = _statement_index("job_assignees")
    assert _statement_index("jobs") < job_assignees_idx
    assert _statement_index("users") < job_assignees_idx


# ── indexes ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("index", EXPECTED_INDEXES)
def test_expected_index_is_created(index: str) -> None:
    assert re.search(
        rf"CREATE\s+(UNIQUE\s+)?INDEX\s+IF\s+NOT\s+EXISTS\s+{index}\b",
        _all_sql(),
        re.IGNORECASE,
    )


# ── init_schema ────────────────────────────────────────────────────────────


def test_init_schema_is_async() -> None:
    assert inspect.iscoroutinefunction(init_schema)


@pytest.mark.asyncio
async def test_init_schema_executes_every_statement_in_order() -> None:
    conn = _fake_conn()
    await init_schema(conn)
    assert _executed(conn) == [_squash(s) for s in _STATEMENTS]


@pytest.mark.asyncio
async def test_init_schema_is_idempotent_when_run_twice() -> None:
    conn = _fake_conn()
    await init_schema(conn)
    await init_schema(conn)
    expected = [_squash(s) for s in _STATEMENTS]
    assert _executed(conn) == expected + expected


@pytest.mark.asyncio
async def test_init_schema_propagates_driver_errors() -> None:
    conn = _fake_conn()
    conn.execute = AsyncMock(side_effect=RuntimeError("connection reset"))
    with pytest.raises(RuntimeError, match="connection reset"):
        await init_schema(conn)
