"""Unit tests for the arq worker wiring (``src/worker/main.py``).

Phase 1 adds one thing to the worker: on ``startup`` it builds *its own*
``BlobStore`` (rooted at ``settings.storage_dir``) and parks it on
``ctx['blob_store']`` so the parse/project tasks in Phases 3-4 can read it.
The worker does not share the API's store.

All external IO (asyncpg, Neo4j, the DDL) is mocked exactly as the Phase 0
tests do — no live services. ``get_settings`` is patched so the store roots at
``tmp_path`` instead of the container's ``/data``.

── Round 3 (merge-blocking re-audit) additions ─────────────────────────────
* F3 (LOW): an empty ``settings.pii_key`` must fail ``startup`` loud, BEFORE
  any pool/driver is opened — ``pii.set_pii_key`` binds an unset ``PII_KEY``
  as ``""``, and ``pgp_sym_encrypt(plaintext, '')`` silently succeeds with an
  empty passphrase (weak-key ciphertext) instead of failing. A misconfigured
  deploy must crash at startup, not ship empty-passphrase ciphertext.

── ADR-008 additions ────────────────────────────────────────────────────────
* An empty ``settings.skill_hash_salt`` must ALSO fail ``startup`` loud,
  BEFORE any pool/driver is opened — same discipline as ``PII_KEY`` above: an
  unsalted hash of a non-vocab skill name is dictionary-attackable (an
  attacker who can read the graph could precompute hashes of common
  names/phrases and confirm one is present).
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.settings import Settings, get_settings
from src.storage.blob_store import BlobStore
from src.worker.main import WorkerSettings, startup
from src.worker.matching_tasks import reverse_match_job, shortlist_job
from src.worker.resume_tasks import parse_resume
from src.worker.tasks import parse_job


def _fake_driver() -> Any:
    driver = MagicMock()
    driver.close = AsyncMock()
    return driver


def test_startup_is_a_coroutine() -> None:
    assert inspect.iscoroutinefunction(startup)


@pytest.mark.asyncio
async def test_startup_parks_a_blob_store_on_ctx(tmp_path: Path) -> None:
    ctx: dict[str, Any] = {}
    # Round 3 F3: startup now fails loud on an empty pii_key, so supply one.
    settings = Settings(
        storage_dir=str(tmp_path),
        pii_key="a-real-secret-key",
        skill_hash_salt="a-real-salt",
    )
    with (
        patch("src.worker.main.get_settings", return_value=settings),
        patch(
            "src.worker.main.asyncpg.create_pool",
            AsyncMock(return_value=MagicMock()),
        ),
        patch("src.worker.main.init_schema", AsyncMock()),
        patch(
            "src.worker.main.AsyncGraphDatabase.driver",
            return_value=_fake_driver(),
        ),
        patch("src.worker.main.bootstrap_neo4j_schema", AsyncMock()),
        # `startup` now opens a dedicated ArqRedis for the reconcile cron to
        # enqueue with — see the ctx["arq"] comment in main.py. Mocked here for
        # the same reason asyncpg and Neo4j are: these tests assert WIRING, and
        # must not need a live broker.
        patch("src.worker.main.create_pool", AsyncMock()),
    ):
        await startup(ctx)

    store = ctx.get("blob_store")
    assert isinstance(store, BlobStore)


@pytest.mark.asyncio
async def test_startup_blob_store_is_rooted_at_storage_dir(tmp_path: Path) -> None:
    ctx: dict[str, Any] = {}
    # Round 3 F3: startup now fails loud on an empty pii_key, so supply one.
    settings = Settings(
        storage_dir=str(tmp_path),
        pii_key="a-real-secret-key",
        skill_hash_salt="a-real-salt",
    )
    with (
        patch("src.worker.main.get_settings", return_value=settings),
        patch(
            "src.worker.main.asyncpg.create_pool",
            AsyncMock(return_value=MagicMock()),
        ),
        patch("src.worker.main.init_schema", AsyncMock()),
        patch(
            "src.worker.main.AsyncGraphDatabase.driver",
            return_value=_fake_driver(),
        ),
        patch("src.worker.main.bootstrap_neo4j_schema", AsyncMock()),
        # `startup` now opens a dedicated ArqRedis for the reconcile cron to
        # enqueue with — see the ctx["arq"] comment in main.py. Mocked here for
        # the same reason asyncpg and Neo4j are: these tests assert WIRING, and
        # must not need a live broker.
        patch("src.worker.main.create_pool", AsyncMock()),
    ):
        await startup(ctx)

    store: BlobStore = ctx["blob_store"]
    await store.put("worker.txt", b"z")
    assert (tmp_path / "worker.txt").read_bytes() == b"z"


def test_worker_registers_the_parse_and_matching_tasks() -> None:
    """Phase 3 registered the parse tasks; Phase 4d ADDS the matching
    write-path tasks (``shortlist_job``, ``reverse_match_job``) to the SAME
    ``functions`` registry — the outbox drainer (``project_to_graph``) stays
    deliberately absent: it is a ``cron_jobs`` entry, never enqueued by name
    (see the module docstring in ``src/worker/main.py``).

    RENAMED from ``test_worker_registers_the_phase_3_parse_tasks`` (this test
    previously asserted ``functions == [parse_job, parse_resume]`` only) per
    the 4d plan-of-record, which explicitly scopes "worker wiring" into this
    sub-phase. This is NOT a weakened test — the new assertion is STRICTER
    (four named functions, in the order they were registered) than the one it
    replaces, which a change that dropped Phase 3's two tasks would still
    fail.
    """
    assert WorkerSettings.functions == [
        parse_job,
        parse_resume,
        shortlist_job,
        reverse_match_job,
    ]


# ── F3 (LOW, round 3) — empty PII_KEY must fail startup loud ────────────────
#
# ``pii.set_pii_key`` binds ``settings.pii_key`` (default ``""``) into the
# transaction-scoped ``app.pii_key`` GUC unconditionally — an unset ``PII_KEY``
# env var means every résumé's PII gets ``pgp_sym_encrypt``'d with an EMPTY
# passphrase (weak-key ciphertext) instead of the worker refusing to start.
# ``startup()`` must assert ``settings.pii_key`` is non-empty and fail loud
# BEFORE opening any pool/driver/store.


@pytest.mark.asyncio
async def test_startup_raises_when_pii_key_is_empty(tmp_path: Path) -> None:
    ctx: dict[str, Any] = {}
    settings = Settings(storage_dir=str(tmp_path), pii_key="")
    with (
        patch("src.worker.main.get_settings", return_value=settings),
        patch(
            "src.worker.main.asyncpg.create_pool",
            AsyncMock(return_value=MagicMock()),
        ) as create_pool,
        patch("src.worker.main.init_schema", AsyncMock()),
        patch(
            "src.worker.main.AsyncGraphDatabase.driver",
            return_value=_fake_driver(),
        ),
        patch("src.worker.main.bootstrap_neo4j_schema", AsyncMock()),
        # `startup` now opens a dedicated ArqRedis for the reconcile cron to
        # enqueue with — see the ctx["arq"] comment in main.py. Mocked here for
        # the same reason asyncpg and Neo4j are: these tests assert WIRING, and
        # must not need a live broker.
        patch("src.worker.main.create_pool", AsyncMock()),
    ):
        with pytest.raises(RuntimeError):
            await startup(ctx)

    # Fail loud BEFORE any I/O — a misconfigured deploy must never open a
    # pool under an empty PII_KEY.
    create_pool.assert_not_called()
    assert "pg_pool" not in ctx


@pytest.mark.asyncio
async def test_startup_does_not_raise_when_pii_key_is_configured(
    tmp_path: Path,
) -> None:
    ctx: dict[str, Any] = {}
    settings = Settings(
        storage_dir=str(tmp_path),
        pii_key="a-real-secret-key",
        skill_hash_salt="a-real-salt",
    )
    with (
        patch("src.worker.main.get_settings", return_value=settings),
        patch(
            "src.worker.main.asyncpg.create_pool",
            AsyncMock(return_value=MagicMock()),
        ),
        patch("src.worker.main.init_schema", AsyncMock()),
        patch(
            "src.worker.main.AsyncGraphDatabase.driver",
            return_value=_fake_driver(),
        ),
        patch("src.worker.main.bootstrap_neo4j_schema", AsyncMock()),
        # `startup` now opens a dedicated ArqRedis for the reconcile cron to
        # enqueue with — see the ctx["arq"] comment in main.py. Mocked here for
        # the same reason asyncpg and Neo4j are: these tests assert WIRING, and
        # must not need a live broker.
        patch("src.worker.main.create_pool", AsyncMock()),
    ):
        await startup(ctx)  # must not raise

    assert "pg_pool" in ctx


# ── ADR-008 — empty SKILL_HASH_SALT must fail startup loud ─────────────────
#
# Same discipline as the PII_KEY check above: an unsalted hash of a non-vocab
# skill name is dictionary-attackable, so ``startup()`` must assert
# ``settings.skill_hash_salt`` is non-empty and fail loud BEFORE opening any
# pool/driver/store.


@pytest.mark.asyncio
async def test_startup_raises_when_skill_hash_salt_is_empty(tmp_path: Path) -> None:
    ctx: dict[str, Any] = {}
    settings = Settings(
        storage_dir=str(tmp_path), pii_key="a-real-secret-key", skill_hash_salt=""
    )
    with (
        patch("src.worker.main.get_settings", return_value=settings),
        patch(
            "src.worker.main.asyncpg.create_pool",
            AsyncMock(return_value=MagicMock()),
        ) as create_pool,
        patch("src.worker.main.init_schema", AsyncMock()),
        patch(
            "src.worker.main.AsyncGraphDatabase.driver",
            return_value=_fake_driver(),
        ),
        patch("src.worker.main.bootstrap_neo4j_schema", AsyncMock()),
        # `startup` now opens a dedicated ArqRedis for the reconcile cron to
        # enqueue with — see the ctx["arq"] comment in main.py. Mocked here for
        # the same reason asyncpg and Neo4j are: these tests assert WIRING, and
        # must not need a live broker.
        patch("src.worker.main.create_pool", AsyncMock()),
    ):
        with pytest.raises(RuntimeError):
            await startup(ctx)

    create_pool.assert_not_called()
    assert "pg_pool" not in ctx


@pytest.mark.asyncio
async def test_startup_does_not_raise_when_skill_hash_salt_is_configured(
    tmp_path: Path,
) -> None:
    ctx: dict[str, Any] = {}
    settings = Settings(
        storage_dir=str(tmp_path),
        pii_key="a-real-secret-key",
        skill_hash_salt="a-real-salt",
    )
    with (
        patch("src.worker.main.get_settings", return_value=settings),
        patch(
            "src.worker.main.asyncpg.create_pool",
            AsyncMock(return_value=MagicMock()),
        ),
        patch("src.worker.main.init_schema", AsyncMock()),
        patch(
            "src.worker.main.AsyncGraphDatabase.driver",
            return_value=_fake_driver(),
        ),
        patch("src.worker.main.bootstrap_neo4j_schema", AsyncMock()),
        # `startup` now opens a dedicated ArqRedis for the reconcile cron to
        # enqueue with — see the ctx["arq"] comment in main.py. Mocked here for
        # the same reason asyncpg and Neo4j are: these tests assert WIRING, and
        # must not need a live broker.
        patch("src.worker.main.create_pool", AsyncMock()),
    ):
        await startup(ctx)  # must not raise

    assert "pg_pool" in ctx


# ── FU-7 (ADR-021 §3) — honest résumé parse status: arq retry ceiling ──────
#
# `parse_resume`'s single `LLMUnavailableError` boundary reads
# `settings.resume_parse_max_tries` (via `ctx["job_try"]`, arq's 1-based
# per-job attempt counter) to decide "let arq retry" vs "give up". arq
# itself only actually RETRIES a job up to `WorkerSettings.max_tries` (a
# class attribute on the arq `Worker`, not sourced from `ctx` at all) --
# so that class attribute must be wired from the SAME settings value, not a
# bare literal duplicated here, or the two ceilings can silently drift
# apart (e.g. arq stops retrying at try 3 while the boundary is still
# waiting for try 5, permanently stranding the row 'parsing').


def test_worker_settings_max_tries_matches_resume_parse_max_tries_setting() -> None:
    assert WorkerSettings.max_tries == get_settings().resume_parse_max_tries


def test_worker_settings_max_tries_is_a_plain_int() -> None:
    """Pinned as a concrete int (e.g. 5), not e.g. a bool (a `bool` is an
    `int` subclass in Python and would sail past a bare `isinstance(...,
    int)` check while being nonsensical here) or a settings object itself."""
    assert isinstance(WorkerSettings.max_tries, int)
    assert not isinstance(WorkerSettings.max_tries, bool)
    assert WorkerSettings.max_tries == 5
