"""Unit tests for ``src.worker.matching_tasks.reverse_match_job`` — all I/O
mocked (the 4c orchestrator is patched out entirely; this file pins CONTROL
FLOW, not scoring). Twin of ``test_worker_shortlist_job.py``.

``src.worker.matching_tasks`` does not exist yet — RED half of the TDD cycle;
this whole file is expected to fail at collection (``ModuleNotFoundError``).

Pinned control flow:

* missing résumé row -> ``"missing"``, ``match_resume_to_jobs`` NEVER called.
* row exists but ``status != 'parsed'`` -> ``"not_parsed"``,
  ``match_resume_to_jobs`` NEVER called.
* happy path -> ``persist_reverse_match`` called EXACTLY ONCE with the
  orchestrator's ``JobMatchResult``, returns ``"persisted"``.
* a ZERO-candidate result (``entries == []``) still calls
  ``persist_reverse_match`` (clears a stale prior reverse-match run) and
  returns the DISTINCT status ``"empty"``.
* ``allowed_job_ids`` is built from jobs with ``description_parsed IS NOT
  NULL`` and passed as an explicit, non-``None`` set to
  ``match_resume_to_jobs`` — reverse match must never silently rank a résumé
  against an unparsed JD (no ``required_skills``/``nice_to_have_skills`` to
  score against) or, worse, pass ``allowed_job_ids=None`` (no filter at all).
* **ADR-026 (FU-8) — a withdrawn résumé returns the DISTINCT status
  ``"withdrawn"``** (never ``"not_parsed"``, even though a withdrawn row
  might also happen to be unparsed) and the orchestrator is NEVER called —
  the same "row exists but is excluded" shape as ``"not_parsed"``, but
  observably distinct so a caller/log can tell the two exclusions apart.

ADR-010 §1 residual / concurrency dedup (this slice) — twin of the
``shortlist_job`` lock tests, using ``kind="reverse_match"`` (a distinct
advisory-lock namespace from ``"shortlist"`` — see
``src.worker.job_lock._CLASS_ID``):

* the lock is acquired FIRST, right after ``pool.acquire()`` — BEFORE the
  résumé row is even fetched. Already-held -> ``"already_running"``, nothing
  else touched, and no attempt to release a lock never held.
* released in a ``finally`` on both the success path and when the
  orchestrator raises. Real session-lock semantics against a real Postgres
  live in ``tests/integration/test_job_lock_pg.py`` — mandatory for this
  change, not optional.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from src.pipeline.matching.orchestrator import JobMatchResult, JobMatchResultEntry
from src.schemas.matching import ScoreBreakdown
from src.settings import Settings

_NOW = dt.datetime(2026, 7, 28, tzinfo=dt.UTC)


class _Row(dict[str, Any]):
    """A dict-like fake asyncpg Record: an absent key returns ``None``
    instead of raising ``KeyError``."""

    def __getitem__(self, key: str) -> Any:
        return dict.get(self, key)


def _flat_call_args(mock_call: Any) -> list[Any]:
    return list(mock_call.args) + list(mock_call.kwargs.values())


def _acm(return_value: Any = None) -> MagicMock:
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=return_value)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _make_conn(
    fetchrow_result: Any, fetch_result: list[Any] | None = None
) -> MagicMock:
    conn = MagicMock(name="conn")
    conn.fetchrow = AsyncMock(return_value=fetchrow_result)
    conn.fetch = AsyncMock(return_value=fetch_result or [])
    return conn


def _make_ctx(conn: MagicMock) -> dict[str, Any]:
    pool = MagicMock(name="pg_pool")
    pool.acquire = MagicMock(return_value=_acm(conn))
    return {
        "pg_pool": pool,
        "neo4j": MagicMock(name="neo4j"),
        "llm": MagicMock(name="llm"),
        "embedder": MagicMock(name="embedder"),
    }


def _breakdown() -> ScoreBreakdown:
    return ScoreBreakdown(
        skill=0.8,
        experience=0.7,
        education=0.6,
        seniority=0.5,
        vector=0.4,
        structured=0.65,
    )


def _lock_patches(*, held: bool = True) -> tuple[Any, Any]:
    """Patch ``try_job_lock``/``release_job_lock`` as imported into
    ``matching_tasks`` — the lock is acquired by default (``held=True``) so
    existing control-flow tests written before this slice keep passing
    unchanged."""
    return (
        patch(
            "src.worker.matching_tasks.try_job_lock",
            new_callable=AsyncMock,
            return_value=held,
        ),
        patch("src.worker.matching_tasks.release_job_lock", new_callable=AsyncMock),
    )


# ── missing / not-parsed ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_missing_resume_row_returns_missing_and_orchestrator_not_called() -> None:
    from src.worker.matching_tasks import reverse_match_job

    conn = _make_conn(None)
    ctx = _make_ctx(conn)
    lock_patch, release_patch = _lock_patches()

    with (
        patch("src.worker.matching_tasks.get_settings", return_value=Settings()),
        lock_patch,
        release_patch,
        patch(
            "src.worker.matching_tasks.match_resume_to_jobs", new_callable=AsyncMock
        ) as match_jobs,
        patch(
            "src.worker.matching_tasks.persist_reverse_match", new_callable=AsyncMock
        ) as persist,
    ):
        result = await reverse_match_job(ctx, str(uuid4()))

    assert result == "missing"
    match_jobs.assert_not_called()
    persist.assert_not_called()


@pytest.mark.parametrize("status", ["uploaded", "parsing", "failed"])
@pytest.mark.asyncio
async def test_resume_not_parsed_returns_not_parsed_and_orchestrator_not_called(
    status: str,
) -> None:
    from src.worker.matching_tasks import reverse_match_job

    conn = _make_conn(_Row({"status": status, "withdrawn_at": None}))
    ctx = _make_ctx(conn)
    lock_patch, release_patch = _lock_patches()

    with (
        patch("src.worker.matching_tasks.get_settings", return_value=Settings()),
        lock_patch,
        release_patch,
        patch(
            "src.worker.matching_tasks.match_resume_to_jobs", new_callable=AsyncMock
        ) as match_jobs,
        patch(
            "src.worker.matching_tasks.persist_reverse_match", new_callable=AsyncMock
        ) as persist,
    ):
        result = await reverse_match_job(ctx, str(uuid4()))

    assert result == "not_parsed"
    match_jobs.assert_not_called()
    persist.assert_not_called()


# ── ADR-026 (FU-8): a withdrawn résumé is excluded, distinctly ────────────


@pytest.mark.asyncio
async def test_withdrawn_parsed_resume_returns_withdrawn_and_orchestrator_not_called() -> (  # noqa: E501
    None
):
    """A withdrawn résumé that WAS successfully parsed (the common case —
    withdrawal happens after the candidate is already in the pool) must
    still be excluded: the orchestrator is never called and nothing is
    persisted."""
    from src.worker.matching_tasks import reverse_match_job

    conn = _make_conn(_Row({"status": "parsed", "withdrawn_at": _NOW}))
    ctx = _make_ctx(conn)
    lock_patch, release_patch = _lock_patches()

    with (
        patch("src.worker.matching_tasks.get_settings", return_value=Settings()),
        lock_patch,
        release_patch,
        patch(
            "src.worker.matching_tasks.match_resume_to_jobs", new_callable=AsyncMock
        ) as match_jobs,
        patch(
            "src.worker.matching_tasks.persist_reverse_match", new_callable=AsyncMock
        ) as persist,
    ):
        result = await reverse_match_job(ctx, str(uuid4()))

    assert result == "withdrawn"
    match_jobs.assert_not_called()
    persist.assert_not_called()


@pytest.mark.parametrize("status", ["uploaded", "parsing", "failed"])
@pytest.mark.asyncio
async def test_withdrawn_unparsed_resume_returns_withdrawn_not_not_parsed(
    status: str,
) -> None:
    """The status string is DISTINCT from ``"not_parsed"`` even when the row
    would ALSO have failed the parsed check — withdrawal is checked and
    reported as its own exclusion reason, never conflated with 'never got
    parsed'."""
    from src.worker.matching_tasks import reverse_match_job

    conn = _make_conn(_Row({"status": status, "withdrawn_at": _NOW}))
    ctx = _make_ctx(conn)
    lock_patch, release_patch = _lock_patches()

    with (
        patch("src.worker.matching_tasks.get_settings", return_value=Settings()),
        lock_patch,
        release_patch,
        patch(
            "src.worker.matching_tasks.match_resume_to_jobs", new_callable=AsyncMock
        ) as match_jobs,
        patch(
            "src.worker.matching_tasks.persist_reverse_match", new_callable=AsyncMock
        ) as persist,
    ):
        result = await reverse_match_job(ctx, str(uuid4()))

    assert result == "withdrawn"
    assert result != "not_parsed"
    match_jobs.assert_not_called()
    persist.assert_not_called()


@pytest.mark.asyncio
async def test_withdrawn_resume_lock_is_still_released(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.worker.matching_tasks import reverse_match_job

    resume_id = uuid4()
    conn = _make_conn(_Row({"status": "parsed", "withdrawn_at": _NOW}))
    ctx = _make_ctx(conn)

    with (
        patch("src.worker.matching_tasks.get_settings", return_value=Settings()),
        patch(
            "src.worker.matching_tasks.try_job_lock",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "src.worker.matching_tasks.release_job_lock", new_callable=AsyncMock
        ) as release_lock,
        patch(
            "src.worker.matching_tasks.match_resume_to_jobs", new_callable=AsyncMock
        ) as match_jobs,
        patch(
            "src.worker.matching_tasks.persist_reverse_match", new_callable=AsyncMock
        ) as persist,
    ):
        result = await reverse_match_job(ctx, str(resume_id))

    assert result == "withdrawn"
    match_jobs.assert_not_called()
    persist.assert_not_called()
    release_lock.assert_awaited_once_with(conn, "reverse_match", resume_id)


# ── happy path ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_happy_path_persists_the_orchestrator_result_exactly_once() -> None:
    from src.worker.matching_tasks import reverse_match_job

    resume_id = uuid4()
    conn = _make_conn(_Row({"status": "parsed", "withdrawn_at": None}), fetch_result=[])
    ctx = _make_ctx(conn)
    lock_patch, release_patch = _lock_patches()

    fake_result = JobMatchResult(
        resume_id=resume_id,
        entries=[
            JobMatchResultEntry(
                job_id=uuid4(),
                title="Senior Backend Engineer",
                rank=1,
                score_final=0.9,
                score_structured=0.8,
                score_evidence=0.7,
                breakdown=_breakdown(),
                evidence=None,
                requirement_count=5,
                must_have_count=3,
            )
        ],
    )

    with (
        patch("src.worker.matching_tasks.get_settings", return_value=Settings()),
        lock_patch,
        release_patch,
        patch(
            "src.worker.matching_tasks.match_resume_to_jobs",
            new_callable=AsyncMock,
            return_value=fake_result,
        ) as match_jobs,
        patch(
            "src.worker.matching_tasks.persist_reverse_match", new_callable=AsyncMock
        ) as persist,
    ):
        result = await reverse_match_job(ctx, str(resume_id))

    assert result == "persisted"
    match_jobs.assert_awaited_once()
    assert resume_id in _flat_call_args(match_jobs.await_args)
    persist.assert_awaited_once()
    assert any(
        arg is fake_result for arg in _flat_call_args(persist.await_args)
    ), "persist_reverse_match must be called with the orchestrator's own result object"


# ── zero-candidate result: still persisted (clears stale prior run) ────────


@pytest.mark.asyncio
async def test_zero_candidate_result_still_calls_persist_and_returns_empty() -> None:
    from src.worker.matching_tasks import reverse_match_job

    resume_id = uuid4()
    conn = _make_conn(_Row({"status": "parsed", "withdrawn_at": None}), fetch_result=[])
    ctx = _make_ctx(conn)
    lock_patch, release_patch = _lock_patches()
    empty_result = JobMatchResult(resume_id=resume_id, entries=[])

    with (
        patch("src.worker.matching_tasks.get_settings", return_value=Settings()),
        lock_patch,
        release_patch,
        patch(
            "src.worker.matching_tasks.match_resume_to_jobs",
            new_callable=AsyncMock,
            return_value=empty_result,
        ),
        patch(
            "src.worker.matching_tasks.persist_reverse_match", new_callable=AsyncMock
        ) as persist,
    ):
        result = await reverse_match_job(ctx, str(resume_id))

    assert result == "empty"
    assert result != "persisted"
    persist.assert_awaited_once()
    assert any(arg is empty_result for arg in _flat_call_args(persist.await_args))


# ── allowed_job_ids: parsed jobs only, an explicit set, never None ────────


@pytest.mark.asyncio
async def test_allowed_job_ids_built_from_parsed_jobs_and_passed_as_explicit_set() -> (
    None
):
    from src.worker.matching_tasks import reverse_match_job

    resume_id = uuid4()
    job_a: UUID = uuid4()
    job_b: UUID = uuid4()
    conn = _make_conn(
        _Row({"status": "parsed", "withdrawn_at": None}),
        fetch_result=[_Row({"id": job_a}), _Row({"id": job_b})],
    )
    ctx = _make_ctx(conn)
    lock_patch, release_patch = _lock_patches()
    fake_result = JobMatchResult(resume_id=resume_id, entries=[])

    with (
        patch("src.worker.matching_tasks.get_settings", return_value=Settings()),
        lock_patch,
        release_patch,
        patch(
            "src.worker.matching_tasks.match_resume_to_jobs",
            new_callable=AsyncMock,
            return_value=fake_result,
        ) as match_jobs,
        patch(
            "src.worker.matching_tasks.persist_reverse_match", new_callable=AsyncMock
        ),
    ):
        await reverse_match_job(ctx, str(resume_id))

    match_jobs.assert_awaited_once()
    allowed = match_jobs.await_args.kwargs["allowed_job_ids"]
    assert allowed is not None, (
        "allowed_job_ids must be an explicit set scoped to parsed jobs, never "
        "None (None means 'no filter at all' to match_resume_to_jobs)"
    )
    assert allowed == {job_a, job_b}


# ── advisory-lock dedup (ADR-010 §1 residual) ───────────────────────────────


@pytest.mark.asyncio
async def test_lock_already_held_returns_already_running_before_any_row_fetch() -> None:
    """When the advisory lock is already held by a concurrent duplicate run,
    ``reverse_match_job`` must short-circuit BEFORE fetching the résumé row."""
    from src.worker.matching_tasks import reverse_match_job

    conn = _make_conn(_Row({"status": "parsed", "withdrawn_at": None}), fetch_result=[])
    ctx = _make_ctx(conn)

    with (
        patch("src.worker.matching_tasks.get_settings", return_value=Settings()),
        patch(
            "src.worker.matching_tasks.try_job_lock",
            new_callable=AsyncMock,
            return_value=False,
        ),
        patch(
            "src.worker.matching_tasks.release_job_lock", new_callable=AsyncMock
        ) as release_lock,
        patch(
            "src.worker.matching_tasks.match_resume_to_jobs", new_callable=AsyncMock
        ) as match_jobs,
        patch(
            "src.worker.matching_tasks.persist_reverse_match", new_callable=AsyncMock
        ) as persist,
    ):
        result = await reverse_match_job(ctx, str(uuid4()))

    assert result == "already_running"
    conn.fetchrow.assert_not_called()
    match_jobs.assert_not_called()
    persist.assert_not_called()
    release_lock.assert_not_called()


@pytest.mark.asyncio
async def test_lock_is_acquired_with_reverse_match_kind_and_the_resume_id() -> None:
    from src.worker.matching_tasks import reverse_match_job

    resume_id = uuid4()
    conn = _make_conn(_Row({"status": "parsed", "withdrawn_at": None}), fetch_result=[])
    ctx = _make_ctx(conn)

    with (
        patch("src.worker.matching_tasks.get_settings", return_value=Settings()),
        patch(
            "src.worker.matching_tasks.try_job_lock",
            new_callable=AsyncMock,
            return_value=True,
        ) as try_lock,
        patch("src.worker.matching_tasks.release_job_lock", new_callable=AsyncMock),
        patch(
            "src.worker.matching_tasks.match_resume_to_jobs",
            new_callable=AsyncMock,
            return_value=JobMatchResult(resume_id=resume_id, entries=[]),
        ),
        patch(
            "src.worker.matching_tasks.persist_reverse_match", new_callable=AsyncMock
        ),
    ):
        await reverse_match_job(ctx, str(resume_id))

    try_lock.assert_awaited_once_with(conn, "reverse_match", resume_id)


@pytest.mark.asyncio
async def test_normal_run_releases_the_lock_it_acquired() -> None:
    from src.worker.matching_tasks import reverse_match_job

    resume_id = uuid4()
    conn = _make_conn(_Row({"status": "parsed", "withdrawn_at": None}), fetch_result=[])
    ctx = _make_ctx(conn)

    with (
        patch("src.worker.matching_tasks.get_settings", return_value=Settings()),
        patch(
            "src.worker.matching_tasks.try_job_lock",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "src.worker.matching_tasks.release_job_lock", new_callable=AsyncMock
        ) as release_lock,
        patch(
            "src.worker.matching_tasks.match_resume_to_jobs",
            new_callable=AsyncMock,
            return_value=JobMatchResult(resume_id=resume_id, entries=[]),
        ),
        patch(
            "src.worker.matching_tasks.persist_reverse_match", new_callable=AsyncMock
        ),
    ):
        result = await reverse_match_job(ctx, str(resume_id))

    assert result == "empty"
    release_lock.assert_awaited_once_with(conn, "reverse_match", resume_id)


@pytest.mark.asyncio
async def test_lock_is_released_even_when_the_orchestrator_raises() -> None:
    from src.worker.matching_tasks import reverse_match_job

    resume_id = uuid4()
    conn = _make_conn(_Row({"status": "parsed", "withdrawn_at": None}), fetch_result=[])
    ctx = _make_ctx(conn)

    with (
        patch("src.worker.matching_tasks.get_settings", return_value=Settings()),
        patch(
            "src.worker.matching_tasks.try_job_lock",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "src.worker.matching_tasks.release_job_lock", new_callable=AsyncMock
        ) as release_lock,
        patch(
            "src.worker.matching_tasks.match_resume_to_jobs",
            new_callable=AsyncMock,
            side_effect=RuntimeError("boom"),
        ),
    ):
        with pytest.raises(RuntimeError, match="boom"):
            await reverse_match_job(ctx, str(resume_id))

    release_lock.assert_awaited_once_with(conn, "reverse_match", resume_id)


@pytest.mark.asyncio
async def test_missing_resume_row_still_releases_the_lock() -> None:
    from src.worker.matching_tasks import reverse_match_job

    conn = _make_conn(None)
    ctx = _make_ctx(conn)

    with (
        patch("src.worker.matching_tasks.get_settings", return_value=Settings()),
        patch(
            "src.worker.matching_tasks.try_job_lock",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "src.worker.matching_tasks.release_job_lock", new_callable=AsyncMock
        ) as release_lock,
    ):
        result = await reverse_match_job(ctx, str(uuid4()))

    assert result == "missing"
    release_lock.assert_awaited_once()
