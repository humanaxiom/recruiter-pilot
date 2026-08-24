"""Integration tests — RED for the shortlist READ bug (branch
``fix/shortlist-read-excludes-withdrawn``): a withdrawn candidate
(FU-8, ``resumes.withdrawn_at IS NOT NULL``) still appears in the persisted
shortlist because ``src.services.shortlist_service``'s four read queries
(``_LIST_QUERY`` / ``_GET_QUERY`` / ``_BLIND_LIST_QUERY`` /
``_BLIND_GET_QUERY``) do not filter on ``withdrawn_at``.

FU-8 un-projects from Neo4j so a NEWLY GENERATED shortlist excludes a
withdrawn candidate, but the persisted ``shortlist_entries`` row survives a
withdrawal untouched — only the READ path is supposed to hide it. Today it
does not: these tests seed a REAL persisted row (via ``persist_shortlist``,
the real Phase 4d write path), flip ``withdrawn_at`` directly via SQL (a pure
read-layer probe — see the task brief: this is simpler and sufficient, since
the withdraw/reinstate FLIP itself is already pinned in
``test_resume_withdraw_pg.py``), and assert the withdrawn row disappears from
``list_for_job`` / ``get_one`` and reappears on reinstatement.

Against a REAL Postgres (testcontainers) because the defect is entirely in
the SQL text — a mocked-conn unit test can only prove the query STRING
contains/doesn't contain a WHERE clause fragment; it cannot prove the clause
actually filters real joined rows the way ``jobs``/``resumes``/
``shortlist_entries`` are really shaped.

Every test below is expected to FAIL today: the withdrawn entry is still
returned. RED half of the TDD cycle — no implementation change here.

NOTE on seeding multiple entries: ``persist_shortlist`` is DELETE-first PER
``job_id`` (it replaces the whole shortlist for a job in one call — see its
own docstring). Seeding two candidates for the SAME job therefore requires
ONE ``persist_shortlist`` call with both entries in it; two separate calls
would silently wipe out the first candidate's row, producing a
false-green single-row list regardless of whether the withdrawn-filter bug
is fixed. ``_seed_shortlist_entries`` (plural) below is the only helper used
whenever a test needs more than one persisted row for the same job.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from collections.abc import AsyncIterator, Iterator
from uuid import UUID

import asyncpg
import pytest
from testcontainers.postgres import PostgresContainer

from src.errors import NotFoundError
from src.models.ddl import init_schema
from src.pipeline.matching.orchestrator import ShortlistResult, ShortlistResultEntry
from src.schemas.matching import DEFAULT_WEIGHTS, PipelineMeta, ScoreBreakdown
from src.services.shortlist_service import get_one, list_for_job, persist_shortlist

_JD = "We need a senior backend engineer. " * 3


@pytest.fixture(scope="session")
def pg_dsn() -> Iterator[str]:
    with PostgresContainer("postgres:16-alpine") as pg:
        yield re.sub(r"\+\w+", "", pg.get_connection_url(), count=1)


@pytest.fixture
async def pg_pool(pg_dsn: str) -> AsyncIterator[asyncpg.Pool]:
    pool = await asyncpg.create_pool(dsn=pg_dsn, min_size=2, max_size=8)
    await init_schema(pool)
    async with pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE job_assignees, users, jobs, resumes, "
            "shortlist_entries, outbox CASCADE"
        )
    try:
        yield pool
    finally:
        await pool.close()


# ── seeding helpers (mirror test_shortlist_read_export_pg.py /
#    test_shortlist_scoping_pg.py) ──────────────────────────────────────────


async def _insert_job(pool: asyncpg.Pool, *, blind_review: bool) -> UUID:
    async with pool.acquire() as conn:
        job_id: UUID = await conn.fetchval(
            "INSERT INTO jobs (title, description_raw, blind_review) "
            "VALUES ($1, $2, $3) RETURNING id",
            "Senior Backend Engineer",
            _JD,
            blind_review,
        )
    return job_id


async def _insert_resume(
    pool: asyncpg.Pool, job_id: UUID, *, name: str = "Real Candidate Name"
) -> UUID:
    """A résumé row, unencrypted-PII columns left NULL — sufficient for these
    read-filter tests: no test here asserts on decrypted identity, only on
    which ``resume_id``s the read layer returns."""
    async with pool.acquire() as conn:
        resume_id: UUID = await conn.fetchval(
            """
            INSERT INTO resumes (
                job_id, blob_key, original_filename, mime_type,
                file_size_bytes, sha256, consent_acknowledged, status
            ) VALUES ($1, $2, 'resume.pdf', 'application/pdf', 1024, $3, TRUE,
                       'parsed')
            RETURNING id
            """,
            job_id,
            f"resumes/{uuid.uuid4().hex}.pdf",
            uuid.uuid4().hex,
        )
    return resume_id


def _breakdown() -> ScoreBreakdown:
    return ScoreBreakdown(
        skill=0.8,
        experience=0.6,
        education=0.4,
        seniority=0.5,
        vector=0.3,
        structured=0.55,
    )


def _meta() -> PipelineMeta:
    return PipelineMeta(
        model_gen="test-gen",
        model_emb="test-emb",
        prompt_versions={"shortlist_evidence": "shortlist_evidence_v1"},
        weights=DEFAULT_WEIGHTS,
        git_sha="abc123",
        generated_at=dt.datetime.now(dt.UTC),
        timings_ms={},
    )


async def _seed_shortlist_entries(
    pool: asyncpg.Pool, *, job_id: UUID, resume_ids_ranked: list[UUID]
) -> dict[UUID, UUID]:
    """Persist one ranked shortlist row per résumé id in ``resume_ids_ranked``
    (rank = 1-based position) via ONE real ``persist_shortlist`` call — see
    the module docstring for why this must be a single call when seeding more
    than one entry for the same job. Returns ``{resume_id: shortlist_entry_id}``."""
    entries = [
        ShortlistResultEntry(
            resume_id=resume_id,
            rank=rank,
            score_final=0.9,
            score_structured=0.8,
            score_evidence=0.7,
            breakdown=_breakdown(),
            evidence=None,
        )
        for rank, resume_id in enumerate(resume_ids_ranked, start=1)
    ]
    result = ShortlistResult(job_id=job_id, entries=entries, pipeline_meta=_meta())
    async with pool.acquire() as conn:
        async with conn.transaction():
            await persist_shortlist(conn, result)
        rows = await conn.fetch(
            "SELECT id, resume_id FROM shortlist_entries WHERE job_id = $1", job_id
        )
    by_resume = {r["resume_id"]: r["id"] for r in rows}
    assert set(by_resume) == set(resume_ids_ranked)
    return by_resume


async def _seed_shortlist_entry(
    pool: asyncpg.Pool, *, job_id: UUID, resume_id: UUID
) -> UUID:
    """Single-entry convenience wrapper over ``_seed_shortlist_entries``."""
    by_resume = await _seed_shortlist_entries(
        pool, job_id=job_id, resume_ids_ranked=[resume_id]
    )
    return by_resume[resume_id]


async def _withdraw(pool: asyncpg.Pool, resume_id: UUID) -> None:
    """Flip ``withdrawn_at`` directly via SQL — a pure read-layer probe. The
    withdraw/reinstate FLIP itself (audit_log + outbox side effects) is
    already pinned in ``test_resume_withdraw_pg.py``; these tests exercise
    ONLY the shortlist read filter."""
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE resumes SET withdrawn_at = now(), "
            "withdrawal_reason = 'test withdrawal' "
            "WHERE id = $1 AND withdrawn_at IS NULL",
            resume_id,
        )
    assert result.endswith(" 1"), "the seeded résumé must not already be withdrawn"


async def _reinstate(pool: asyncpg.Pool, resume_id: UUID) -> None:
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE resumes SET withdrawn_at = NULL, withdrawal_reason = NULL "
            "WHERE id = $1 AND withdrawn_at IS NOT NULL",
            resume_id,
        )
    assert result.endswith(" 1"), "the seeded résumé must have been withdrawn first"


async def _insert_user(pool: asyncpg.Pool, *, role: str) -> UUID:
    async with pool.acquire() as conn:
        user_id: UUID = await conn.fetchval(
            "INSERT INTO users (cas_username, display_name, role) "
            "VALUES ($1, $1, $2) RETURNING id",
            f"hm-{uuid.uuid4().hex[:8]}",
            role,
        )
    return user_id


async def _assign(
    pool: asyncpg.Pool, *, job_id: UUID, user_id: UUID, assigned_by: UUID
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO job_assignees (job_id, user_id, assigned_by) "
            "VALUES ($1, $2, $3)",
            job_id,
            user_id,
            assigned_by,
        )


# ── 1. blind list excludes withdrawn ────────────────────────────────────────


@pytest.mark.asyncio
async def test_blind_list_for_job_excludes_withdrawn_candidate(
    pg_pool: asyncpg.Pool,
) -> None:
    job_id = await _insert_job(pg_pool, blind_review=True)
    withdrawn_resume = await _insert_resume(
        pg_pool, job_id, name="Withdrawn Candidate Zyzzyva"
    )
    active_resume = await _insert_resume(pg_pool, job_id, name="Active Candidate")
    await _seed_shortlist_entries(
        pg_pool, job_id=job_id, resume_ids_ranked=[withdrawn_resume, active_resume]
    )

    await _withdraw(pg_pool, withdrawn_resume)

    async with pg_pool.acquire() as conn:
        entries = await list_for_job(conn, job_id=job_id)

    resume_ids = {e.resume_id for e in entries}
    assert (
        withdrawn_resume not in resume_ids
    ), "a withdrawn candidate must not appear in the BLIND shortlist read"
    assert active_resume in resume_ids
    assert len(entries) == 1


# ── 2. non-blind list excludes withdrawn (exercises _LIST_QUERY, which
#       today has NO join to resumes at all) ────────────────────────────────


@pytest.mark.asyncio
async def test_non_blind_list_for_job_excludes_withdrawn_candidate(
    pg_pool: asyncpg.Pool,
) -> None:
    job_id = await _insert_job(pg_pool, blind_review=False)
    withdrawn_resume = await _insert_resume(pg_pool, job_id)
    active_resume = await _insert_resume(pg_pool, job_id)
    await _seed_shortlist_entries(
        pg_pool, job_id=job_id, resume_ids_ranked=[withdrawn_resume, active_resume]
    )

    await _withdraw(pg_pool, withdrawn_resume)

    async with pg_pool.acquire() as conn:
        entries = await list_for_job(conn, job_id=job_id)

    resume_ids = {e.resume_id for e in entries}
    assert withdrawn_resume not in resume_ids, (
        "a withdrawn candidate must not appear in the NON-BLIND shortlist "
        "read (_LIST_QUERY has no resumes join today — the fix must add one)"
    )
    assert active_resume in resume_ids
    assert len(entries) == 1


# ── 3. get_one on a withdrawn entry ─────────────────────────────────────────


@pytest.mark.parametrize("blind_review", [False, True])
@pytest.mark.asyncio
async def test_get_one_for_withdrawn_candidate_raises_not_found(
    pg_pool: asyncpg.Pool, blind_review: bool
) -> None:
    """Matches ``get_one``'s EXISTING contract for a genuinely absent entry
    (see ``list_for_job``/``get_one``'s ADR-020 §5 docstrings): a shortlist
    entry that should not be visible 404s via ``NotFoundError``, never a
    successful (and identity-leaking) response."""
    job_id = await _insert_job(pg_pool, blind_review=blind_review)
    resume_id = await _insert_resume(pg_pool, job_id)
    entry_id = await _seed_shortlist_entry(pg_pool, job_id=job_id, resume_id=resume_id)

    await _withdraw(pg_pool, resume_id)

    async with pg_pool.acquire() as conn:
        with pytest.raises(NotFoundError):
            await get_one(conn, entry_id)


# ── 4. reinstate restores visibility, from the SAME persisted row ──────────


@pytest.mark.asyncio
async def test_reinstate_restores_shortlist_visibility_from_same_row(
    pg_pool: asyncpg.Pool,
) -> None:
    job_id = await _insert_job(pg_pool, blind_review=False)
    resume_id = await _insert_resume(pg_pool, job_id)
    entry_id = await _seed_shortlist_entry(pg_pool, job_id=job_id, resume_id=resume_id)

    await _withdraw(pg_pool, resume_id)
    async with pg_pool.acquire() as conn:
        entries_while_withdrawn = await list_for_job(conn, job_id=job_id)
        with pytest.raises(NotFoundError):
            await get_one(conn, entry_id)
    assert entries_while_withdrawn == []

    await _reinstate(pg_pool, resume_id)

    async with pg_pool.acquire() as conn:
        entries_after_reinstate = await list_for_job(conn, job_id=job_id)
        got = await get_one(conn, entry_id)

    assert len(entries_after_reinstate) == 1
    assert entries_after_reinstate[0].id == entry_id, (
        "reinstatement must surface the SAME persisted shortlist_entries row "
        "-- no regenerate"
    )
    assert entries_after_reinstate[0].resume_id == resume_id
    assert got.id == entry_id


# ── 5. FU-6 row-scoping still composes with the withdrawn filter ───────────


@pytest.mark.asyncio
async def test_list_for_job_scoped_by_user_still_excludes_withdrawn(
    pg_pool: asyncpg.Pool,
) -> None:
    """A hiring_manager scoped ``list_for_job`` call on a BLIND job must
    still exclude a withdrawn entry AND still honor the ``job_assignees``
    scoping predicate — guards that the withdrawn-filter fix doesn't clobber
    the FU-6 ``.replace(...)`` row-scoping SQL, and vice versa."""
    job_id = await _insert_job(pg_pool, blind_review=True)
    withdrawn_resume = await _insert_resume(pg_pool, job_id, name="Withdrawn HM View")
    active_resume = await _insert_resume(pg_pool, job_id, name="Active HM View")
    await _seed_shortlist_entries(
        pg_pool, job_id=job_id, resume_ids_ranked=[withdrawn_resume, active_resume]
    )
    await _withdraw(pg_pool, withdrawn_resume)

    hm_id = await _insert_user(pg_pool, role="hiring_manager")
    await _assign(pg_pool, job_id=job_id, user_id=hm_id, assigned_by=hm_id)

    async with pg_pool.acquire() as conn:
        scoped_entries = await list_for_job(conn, job_id=job_id, user_id=hm_id)

    scoped_resume_ids = {e.resume_id for e in scoped_entries}
    assert withdrawn_resume not in scoped_resume_ids, (
        "the withdrawn filter must apply even on the scoped (hiring_manager) "
        "branch of list_for_job"
    )
    assert active_resume in scoped_resume_ids
    assert len(scoped_entries) == 1


@pytest.mark.asyncio
async def test_list_for_job_scoped_by_unassigned_user_stays_empty_regardless(
    pg_pool: asyncpg.Pool,
) -> None:
    """The mirror-image guard: an unassigned hiring_manager still gets an
    empty list for a job with only an ACTIVE (non-withdrawn) entry — proving
    the scoping predicate itself is untouched by the withdrawn-filter change,
    not merely coincidentally empty because everything got withdrawn."""
    job_id = await _insert_job(pg_pool, blind_review=False)
    active_resume = await _insert_resume(pg_pool, job_id)
    await _seed_shortlist_entry(pg_pool, job_id=job_id, resume_id=active_resume)

    unassigned_hm_id = await _insert_user(pg_pool, role="hiring_manager")

    async with pg_pool.acquire() as conn:
        scoped_entries = await list_for_job(
            conn, job_id=job_id, user_id=unassigned_hm_id
        )

    assert scoped_entries == []


# -- 6. export (CSV/JSON) also excludes withdrawn (reviewer follow-up) --------


@pytest.mark.asyncio
async def test_export_rows_excludes_withdrawn_candidate(
    pg_pool: asyncpg.Pool,
) -> None:
    """The export is a shortlist view too (GET /jobs/{id}/shortlist/export), so
    a withdrawn candidate must not appear in the exported rows either."""
    from src.services.pii import set_pii_key
    from src.services.shortlist_service import export_rows

    job_id = await _insert_job(pg_pool, blind_review=True)
    withdrawn_resume = await _insert_resume(pg_pool, job_id)
    active_resume = await _insert_resume(pg_pool, job_id)
    await _seed_shortlist_entries(
        pg_pool, job_id=job_id, resume_ids_ranked=[withdrawn_resume, active_resume]
    )

    await _withdraw(pg_pool, withdrawn_resume)

    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await set_pii_key(conn)
            rows = await export_rows(conn, job_id=job_id, reveal=False)

    resume_ids = {r["resume_id"] for r in rows}
    assert (
        withdrawn_resume not in resume_ids
    ), "a withdrawn candidate must not appear in the shortlist export"
    assert active_resume in resume_ids
    assert len(rows) == 1
