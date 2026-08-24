"""Unit tests for Phase 4d's REQUIREMENT 1 (ADR-009 carried obligation): wire
``MatchingContext``/``weights`` from ``Settings`` at the real worker call
sites, not from the in-code literal fallbacks in
``src/pipeline/matching/orchestrator.py``.

Three things pinned here, none of which exist yet — this whole file is RED by
construction:

1. ``matching_context_from_settings(settings, *, db, neo4j, llm, embedder) ->
   MatchingContext`` — a factory that must populate EVERY non-weight tunable
   (``family_weight`` / ``non_matchable_families`` / ``llm_concurrency`` /
   ``evidence_max_tokens`` / ``model_gen`` / ``model_emb`` / ``git_sha``) from
   the ``Settings`` instance it's handed, not ``MatchingContext``'s own
   dataclass-field defaults (which mirror ``orchestrator.py``'s module-level
   literals ``_FAMILY_MATCH_WEIGHT=0.5``, ``_NON_MATCHABLE_FAMILIES=("other",
   "domain")``, ``_LLM_CONCURRENCY=4``, ``_EVIDENCE_MAX_TOKENS=2048`` — see
   ``orchestrator.py`` lines 86-110). Every value asserted below deliberately
   DIFFERS from those literals, so a factory that quietly falls back to the
   dataclass defaults (ignoring the ``Settings`` it was handed) fails loud.
2. ``shortlist_job``/``reverse_match_job`` (``src.worker.matching_tasks``)
   must call ``generate_shortlist``/``match_resume_to_jobs`` with
   ``weights=weights_from_settings(settings)`` — NOT the orchestrator's
   ``DEFAULT_WEIGHTS`` — and with ``evidence_k`` sourced from
   ``settings.match_reverse_evidence_k`` on the reverse-match path (NOT
   orchestrator's ``_REVERSE_EVIDENCE_K`` module literal, which happens to
   equal the default today and so cannot be told apart from it except by a
   non-default override).
3. ``settings.git_sha`` must flow, end to end, through
   ``matching_context_from_settings`` -> ``MatchingContext.git_sha`` -> the
   REAL orchestrator's ``PipelineMeta.git_sha`` on a shortlist run.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from src.pipeline.matching.orchestrator import (
    MatchingContext,
    generate_shortlist,
    matching_context_from_settings,
)
from src.schemas.matching import DEFAULT_WEIGHTS
from src.settings import Settings, weights_from_settings

# orchestrator.py's own in-code literal fallbacks (ADR-009 "Tunable-default
# duplication" residual) — every value below is chosen to DIFFER from these,
# so a factory that silently falls back to MatchingContext's dataclass
# defaults instead of reading `settings` cannot pass.
_ORCHESTRATOR_FAMILY_WEIGHT_LITERAL = 0.5
_ORCHESTRATOR_NON_MATCHABLE_FAMILIES_LITERAL = ("other", "domain")
_ORCHESTRATOR_LLM_CONCURRENCY_LITERAL = 4
_ORCHESTRATOR_EVIDENCE_MAX_TOKENS_LITERAL = 2048


# ── 1. matching_context_from_settings populates every non-weight tunable ────


def test_matching_context_from_settings_populates_tunables() -> None:
    settings = Settings(
        match_family_weight=0.9,
        match_non_matchable_families="misc,junk,other",
        match_llm_concurrency=11,
        match_evidence_max_tokens=333,
        llm_model_generation="custom-gen-model-9000",
        llm_model_embedding="custom-emb-model-9000",
        git_sha="feedface1234",
    )
    assert settings.match_family_weight != _ORCHESTRATOR_FAMILY_WEIGHT_LITERAL
    assert settings.match_llm_concurrency != _ORCHESTRATOR_LLM_CONCURRENCY_LITERAL
    assert (
        settings.match_evidence_max_tokens != _ORCHESTRATOR_EVIDENCE_MAX_TOKENS_LITERAL
    )

    db = MagicMock(name="db")
    neo4j = MagicMock(name="neo4j")
    llm = MagicMock(name="llm")
    embedder = MagicMock(name="embedder")

    ctx = matching_context_from_settings(
        settings, db=db, neo4j=neo4j, llm=llm, embedder=embedder
    )

    assert isinstance(ctx, MatchingContext)
    assert ctx.db is db
    assert ctx.neo4j is neo4j
    assert ctx.llm is llm
    assert ctx.embedder is embedder
    assert ctx.family_weight == pytest.approx(0.9)
    assert ctx.family_weight != _ORCHESTRATOR_FAMILY_WEIGHT_LITERAL
    assert ctx.non_matchable_families == ("misc", "junk", "other")
    assert ctx.non_matchable_families != _ORCHESTRATOR_NON_MATCHABLE_FAMILIES_LITERAL
    assert ctx.llm_concurrency == 11
    assert ctx.llm_concurrency != _ORCHESTRATOR_LLM_CONCURRENCY_LITERAL
    assert ctx.evidence_max_tokens == 333
    assert ctx.evidence_max_tokens != _ORCHESTRATOR_EVIDENCE_MAX_TOKENS_LITERAL
    assert ctx.model_gen == "custom-gen-model-9000"
    assert ctx.model_emb == "custom-emb-model-9000"
    assert ctx.git_sha == "feedface1234"


def test_matching_context_from_settings_default_settings_still_wires_through() -> None:
    """Not just the non-default path — an UNCONFIGURED Settings must also flow
    through the factory (not silently swap in MatchingContext's own
    dataclass defaults for a different, coincidentally-equal reason)."""
    settings = Settings()
    ctx = matching_context_from_settings(
        settings,
        db=MagicMock(),
        neo4j=MagicMock(),
        llm=MagicMock(),
        embedder=MagicMock(),
    )
    assert ctx.family_weight == settings.match_family_weight
    assert ctx.llm_concurrency == settings.match_llm_concurrency
    assert ctx.evidence_max_tokens == settings.match_evidence_max_tokens
    assert ctx.model_gen == settings.llm_model_generation
    assert ctx.model_emb == settings.llm_model_embedding
    assert ctx.git_sha == settings.git_sha


def test_matching_context_from_settings_use_classified_families_true() -> None:
    """ROADMAP A2, Phase 3.3 slice 2 (2026-08-19 decision memo): the flag must
    flow through the SAME factory as family_weight/non_matchable_families --
    never a hardcoded False, never read ad hoc at a call site."""
    settings = Settings(match_use_classified_families=True)
    ctx = matching_context_from_settings(
        settings,
        db=MagicMock(),
        neo4j=MagicMock(),
        llm=MagicMock(),
        embedder=MagicMock(),
    )
    assert ctx.use_classified_families is True


def test_matching_context_from_settings_use_classified_families_default_false() -> None:
    """An UNCONFIGURED Settings must flow through as False -- the classifier's
    output must never move ranking by accident."""
    settings = Settings()
    assert settings.match_use_classified_families is False
    ctx = matching_context_from_settings(
        settings,
        db=MagicMock(),
        neo4j=MagicMock(),
        llm=MagicMock(),
        embedder=MagicMock(),
    )
    assert ctx.use_classified_families is False


# ── 2. shortlist_job / reverse_match_job pass weights_from_settings(...) ────
#       not DEFAULT_WEIGHTS ──────────────────────────────────────────────────


class _Row(dict[str, Any]):
    """A dict-like fake asyncpg Record: ``row["missing_key"]`` returns
    ``None`` instead of raising, so these tests don't have to know every
    column the real implementation selects."""

    def __getitem__(self, key: str) -> Any:
        return dict.get(self, key)


def _acm(return_value: Any = None) -> MagicMock:
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=return_value)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _make_worker_ctx(conn: MagicMock) -> dict[str, Any]:
    pool = MagicMock(name="pg_pool")
    pool.acquire = MagicMock(return_value=_acm(conn))
    return {
        "pg_pool": pool,
        "neo4j": MagicMock(name="neo4j"),
        "llm": MagicMock(name="llm"),
        "embedder": MagicMock(name="embedder"),
    }


@pytest.mark.asyncio
async def test_shortlist_job_passes_weights_from_settings_not_default_weights() -> None:
    from src.worker.matching_tasks import shortlist_job

    job_id = uuid4()
    # Legal (sums-to-one) but NON-default sub-weights.
    settings = Settings(
        match_skill=0.5,
        match_experience=0.15,
        match_education=0.10,
        match_seniority=0.15,
        match_vector=0.10,
    )
    expected_weights = weights_from_settings(settings)
    assert expected_weights != DEFAULT_WEIGHTS

    conn = MagicMock(name="conn")
    conn.fetchrow = AsyncMock(
        return_value=_Row({"description_parsed": {"required_skills": []}})
    )
    # ADR-010 §1 residual: shortlist_job now acquires/releases a Postgres
    # advisory lock on this same conn before/after its existing work (see
    # src/worker/job_lock.py). This test is about settings wiring, not
    # locking, so make the lock a no-op pass-through: fetchval (try_job_lock)
    # truthy, execute (release_job_lock) a plain AsyncMock.
    conn.fetchval = AsyncMock(return_value=True)
    conn.execute = AsyncMock()
    ctx = _make_worker_ctx(conn)

    fake_result = MagicMock(name="ShortlistResult", entries=[])

    with (
        patch("src.worker.matching_tasks.get_settings", return_value=settings),
        patch(
            "src.worker.matching_tasks.generate_shortlist",
            new_callable=AsyncMock,
            return_value=fake_result,
        ) as generate,
        patch("src.worker.matching_tasks.persist_shortlist", new_callable=AsyncMock),
    ):
        await shortlist_job(ctx, str(job_id))

    generate.assert_awaited_once()
    captured_weights = generate.await_args.kwargs["weights"]
    assert captured_weights == expected_weights
    assert captured_weights != DEFAULT_WEIGHTS


@pytest.mark.asyncio
async def test_reverse_match_job_uses_match_reverse_evidence_k_from_settings() -> None:
    from src.worker.matching_tasks import reverse_match_job

    resume_id = uuid4()
    settings = Settings(match_reverse_evidence_k=3)
    assert settings.match_reverse_evidence_k != 10  # the shipped default

    conn = MagicMock(name="conn")
    conn.fetchrow = AsyncMock(return_value=_Row({"status": "parsed"}))
    conn.fetch = AsyncMock(return_value=[])
    # ADR-010 §1 residual: reverse_match_job now acquires/releases a Postgres
    # advisory lock on this same conn (see src/worker/job_lock.py). Not the
    # concern of this test — make the lock a no-op pass-through.
    conn.fetchval = AsyncMock(return_value=True)
    conn.execute = AsyncMock()
    ctx = _make_worker_ctx(conn)

    fake_result = MagicMock(name="JobMatchResult", entries=[])

    with (
        patch("src.worker.matching_tasks.get_settings", return_value=settings),
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
    assert match_jobs.await_args.kwargs["evidence_k"] == 3


# ── 3. settings.git_sha flows through the REAL orchestrator into           ──
#       PipelineMeta.git_sha (end-to-end at the orchestrator level, no       ─
#       worker task involved — every I/O boundary below is mocked) ─────────


class _EmptyAsyncNeo4jResult:
    """An async-iterable Neo4j result yielding zero rows."""

    def __aiter__(self) -> _EmptyAsyncNeo4jResult:
        return self

    async def __anext__(self) -> Any:
        raise StopAsyncIteration


def _neo4j_with_zero_stage1_candidates() -> MagicMock:
    session = MagicMock(name="session")
    session.run = AsyncMock(return_value=_EmptyAsyncNeo4jResult())
    neo4j = MagicMock(name="neo4j")
    neo4j.session = MagicMock(return_value=_acm(session))
    return neo4j


@pytest.mark.asyncio
async def test_git_sha_from_settings_lands_in_shortlist_pipeline_meta() -> None:
    sentinel = "deadbeefcafefeed"
    settings = Settings(git_sha=sentinel)
    assert settings.git_sha is not None

    db = MagicMock(name="db")
    db.fetchrow = AsyncMock(
        return_value=_Row(
            {"title": "Senior Engineer", "min_years": None, "description_parsed": None}
        )
    )
    neo4j = _neo4j_with_zero_stage1_candidates()

    ctx = matching_context_from_settings(
        settings, db=db, neo4j=neo4j, llm=MagicMock(), embedder=MagicMock()
    )

    job_id: UUID = uuid4()
    result = await generate_shortlist(job_id, ctx)

    assert result.pipeline_meta is not None
    assert result.pipeline_meta.git_sha == sentinel
