"""Unit tests — FU-7 §2 fail-closed ranking (ADR-021 §2 / ADR-029).

Pins the propagation contract at three levels of
``src.pipeline.matching.orchestrator``, all I/O mocked:

1. ``_stage3_per_candidate`` — currently catches ``LLMOutputInvalidError``,
   logs, and returns ``None`` (silently zeroing that candidate's evidence).
   Must instead RE-RAISE. ``LLMUnavailableError`` is already uncaught here
   and must keep propagating.
2. ``stage3_evidence._one`` — every exception fails CLOSED. Mode A/B LLM
   signals re-raise (FU-7 §2), and **as of ROADMAP A4 M1 so does a generic
   ``Exception``**. This file originally pinned the opposite for the generic
   case ("one candidate must not sink all"); that isolation silently zeroed
   40% of a top-15 candidate's ``score_final`` and persisted it unmarked, so
   it was the defect rather than the contract — see
   ``test_stage3_evidence_fails_closed_on_a_generic_exception`` for the
   reversal and why.
3. ``generate_shortlist`` — wraps BOTH the stage-2 per-candidate loop (the
   embedder call for the seniority cosine can raise ``LLMUnavailableError``)
   and the ``stage3_evidence`` call: any failure anywhere in that path becomes
   a single typed ``RankingUnavailableError``, which ``shortlist_job`` already
   turns into a withheld shortlist, a visible state, and a bounded retry.

``None`` survives as a meaningful value throughout, and that is the point of
the A4 M1 change rather than a side effect: after it, ``None`` means only
"nothing to evaluate" (no chunks, or a job with no ``required_skills``) or
"past the ``evidence_k`` cliff" — never "we tried and it broke".

Every test here white-box patches orchestrator module-level functions
(``load_job_view`` / ``stage1_coarse`` / ``_stage2_per_candidate`` /
``stage3_evidence`` / ``_stage3_per_candidate``) exactly like the existing
precedent in ``test_matching_context_settings_wiring.py`` and
``test_matching_orchestrator.py`` (``load_prompt`` patched to a fixed
``RenderedPrompt``) — no real Postgres/Neo4j/Ollama needed.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from src.pipeline.llm import LLMOutputInvalidError, LLMUnavailableError
from src.pipeline.matching.orchestrator import (
    JobView,
    MatchingContext,
    Stage1Candidate,
    Stage2Candidate,
    generate_shortlist,
    stage3_evidence,
)
from src.prompts import RenderedPrompt
from src.schemas.matching import EvidenceObjectIngest, ScoreBreakdown

_STUBBED_PROMPT = RenderedPrompt(version="test-stub", system="system", user="user")


def _job(*, required_skills: tuple[str, ...] = ("Python",)) -> JobView:
    return JobView(
        id=uuid4(),
        title="Senior Backend Engineer",
        min_years=None,
        education_min_level=None,
        education_fields=(),
        required_skills=required_skills,
        nice_to_have_skills=(),
    )


def _breakdown() -> ScoreBreakdown:
    return ScoreBreakdown(
        skill=0.8,
        experience=0.7,
        education=0.6,
        seniority=0.5,
        vector=0.4,
        structured=0.65,
    )


def _stage2_candidate(resume_id: UUID) -> Stage2Candidate:
    return Stage2Candidate(
        resume_id=resume_id, vec_score=0.9, structured=0.65, breakdown=_breakdown()
    )


def _ctx(*, llm: MagicMock | None = None, embedder: MagicMock | None = None) -> Any:
    return MatchingContext(
        db=MagicMock(name="db"),
        neo4j=MagicMock(name="neo4j"),
        llm=llm or MagicMock(name="llm"),
        embedder=embedder or MagicMock(name="embedder"),
        model_gen="test-gen",
        model_emb="test-emb",
    )


# ── 1. _stage3_per_candidate re-raises LLMOutputInvalidError ────────────────


@pytest.mark.asyncio
async def test_stage3_per_candidate_reraises_llm_output_invalid_error() -> None:
    from src.pipeline.matching.orchestrator import _stage3_per_candidate

    job = _job()
    candidate = _stage2_candidate(uuid4())
    parsed = {"chunks": [{"id": "c_001", "section": "summary", "page": 0, "text": "x"}]}
    llm = MagicMock(
        chat_json=AsyncMock(side_effect=LLMOutputInvalidError("empty response"))
    )
    ctx = _ctx(llm=llm)

    with patch(
        "src.pipeline.matching.orchestrator.load_prompt", return_value=_STUBBED_PROMPT
    ):
        with pytest.raises(LLMOutputInvalidError):
            await _stage3_per_candidate(ctx, job, candidate, parsed)


@pytest.mark.asyncio
async def test_stage3_per_candidate_llm_unavailable_error_still_propagates() -> None:
    """Already-uncaught behaviour, pinned as a regression guard: a
    ``LLMUnavailableError`` from ``chat_json`` must never be silently
    swallowed either."""
    from src.pipeline.matching.orchestrator import _stage3_per_candidate

    job = _job()
    candidate = _stage2_candidate(uuid4())
    parsed = {"chunks": [{"id": "c_001", "section": "summary", "page": 0, "text": "x"}]}
    llm = MagicMock(chat_json=AsyncMock(side_effect=LLMUnavailableError("ollama down")))
    ctx = _ctx(llm=llm)

    with patch(
        "src.pipeline.matching.orchestrator.load_prompt", return_value=_STUBBED_PROMPT
    ):
        with pytest.raises(LLMUnavailableError):
            await _stage3_per_candidate(ctx, job, candidate, parsed)


# ── 2. stage3_evidence: re-raise LLMOutputInvalidError/LLMUnavailableError, ──
#       but still isolate a generic Exception to None ───────────────────────


@pytest.mark.asyncio
async def test_stage3_evidence_reraises_llm_output_invalid_error() -> None:
    job = _job()
    resume_id = uuid4()
    candidate = _stage2_candidate(resume_id)
    ctx = _ctx()
    ctx.db.fetchrow = AsyncMock(return_value={"parsed": {"chunks": []}})

    with patch(
        "src.pipeline.matching.orchestrator._stage3_per_candidate",
        new_callable=AsyncMock,
        side_effect=LLMOutputInvalidError("empty response"),
    ):
        with pytest.raises(LLMOutputInvalidError):
            await stage3_evidence(ctx, job, [candidate])


@pytest.mark.asyncio
async def test_stage3_evidence_reraises_llm_unavailable_error() -> None:
    job = _job()
    resume_id = uuid4()
    candidate = _stage2_candidate(resume_id)
    ctx = _ctx()
    ctx.db.fetchrow = AsyncMock(return_value={"parsed": {"chunks": []}})

    with patch(
        "src.pipeline.matching.orchestrator._stage3_per_candidate",
        new_callable=AsyncMock,
        side_effect=LLMUnavailableError("ollama down"),
    ):
        with pytest.raises(LLMUnavailableError):
            await stage3_evidence(ctx, job, [candidate])


@pytest.mark.asyncio
async def test_stage3_evidence_fails_closed_on_a_generic_exception() -> None:
    """A GENERIC (non-LLM) per-candidate exception fails CLOSED — ROADMAP A4
    M1.

    **REVERSED from this test's original assertion**, which was
    ``assert results[bad_id] is None`` under the heading "one candidate must
    not sink all". That behaviour was the defect, not the contract, and the
    reversal is explained here rather than in a commit nobody will read.

    What the old behaviour actually did: ``stage3_evidence`` caught bare
    ``Exception`` and set ``results[id] = None``; ``_evidence_completeness``
    maps ``None`` to ``0.0``; so the candidate silently lost the whole
    ``evidence`` (0.30) and ``motivation`` (0.10) share — **40% of
    ``score_final``** — and the row was persisted UNMARKED. For a top-15
    candidate that is not a cosmetic degradation: it **displaces real people
    inside the ranks a recruiter actually looks at**, and only when a
    transient Neo4j/Postgres hiccup happens to land on them, so it is
    unreproducible by the time anyone notices.

    It was also invisible by construction. A ``None`` from a *failure* was
    indistinguishable from a ``None`` from being *past the evidence cliff*
    (``evidence_k=15``) — both render as an affirmative ``0%``. After this
    change ``None`` means only "nothing to evaluate" (no chunks, or a job with
    no required skills — see ``_stage3_per_candidate``'s early return), never
    "we tried and it broke".

    This restores the posture ADR-029/ADR-021 §2 already claim: a shortlist is
    withheld rather than silently degraded, and the caller retries.
    """
    job = _job()
    good_id = uuid4()
    bad_id = uuid4()
    good_candidate = _stage2_candidate(good_id)
    bad_candidate = _stage2_candidate(bad_id)
    ctx = _ctx()
    ctx.db.fetchrow = AsyncMock(return_value={"parsed": {"chunks": []}})

    good_evidence = EvidenceObjectIngest()

    async def _fake_stage3(
        _ctx: Any, _job: Any, candidate: Stage2Candidate, _parsed: Any, **_kw: Any
    ) -> EvidenceObjectIngest | None:
        if candidate.resume_id == bad_id:
            raise RuntimeError("totally unexpected")
        return good_evidence

    with patch(
        "src.pipeline.matching.orchestrator._stage3_per_candidate",
        new_callable=AsyncMock,
        side_effect=_fake_stage3,
    ):
        with pytest.raises(RuntimeError, match="totally unexpected"):
            await stage3_evidence(ctx, job, [good_candidate, bad_candidate])


@pytest.mark.asyncio
async def test_stage3_evidence_still_returns_none_when_there_is_nothing_to_evaluate() -> (  # noqa: E501
    None
):
    """The counterpart to the test above, and the reason it is safe.

    ``None`` must keep meaning "nothing to evaluate" — ``_stage3_per_candidate``
    returns it for a candidate with no chunks, or for a job with no
    ``required_skills``. Failing closed must not turn that legitimate outcome
    into an error, or every job without required skills would withhold its
    shortlist forever.
    """
    job = _job()
    resume_id = uuid4()
    ctx = _ctx()
    ctx.db.fetchrow = AsyncMock(return_value={"parsed": {"chunks": []}})

    with patch(
        "src.pipeline.matching.orchestrator._stage3_per_candidate",
        new_callable=AsyncMock,
        return_value=None,
    ):
        results = await stage3_evidence(ctx, job, [_stage2_candidate(resume_id)])

    assert results[resume_id] is None


# ── 3. generate_shortlist wraps Mode A/B failures into RankingUnavailableError

_DIM = 4


def _vec() -> list[float]:
    return [0.1] * _DIM


@pytest.mark.asyncio
async def test_generate_shortlist_raises_ranking_unavailable_when_stage2_embedder_fails() -> (  # noqa: E501
    None
):
    """A LLMUnavailableError from ``ctx.embedder.embed`` (the stage-2
    seniority-cosine call) inside the per-candidate loop must become a
    RankingUnavailableError, not escape ``generate_shortlist`` as a bare
    LLMUnavailableError (which would NOT trigger an arq retry — ADR-027)."""
    from src.pipeline.matching.orchestrator import RankingUnavailableError

    job_id = uuid4()
    job = _job(required_skills=())
    resume_id = uuid4()
    embedder = MagicMock(embed=AsyncMock(side_effect=LLMUnavailableError("embed down")))
    ctx = _ctx(embedder=embedder)

    with (
        patch(
            "src.pipeline.matching.orchestrator.load_job_view",
            new_callable=AsyncMock,
            return_value=job,
        ),
        patch(
            "src.pipeline.matching.orchestrator.stage1_coarse",
            new_callable=AsyncMock,
            return_value=[Stage1Candidate(resume_id=resume_id, vec_score=0.9)],
        ),
        patch.object(
            ctx.db,
            "fetchrow",
            new=AsyncMock(
                return_value={
                    "parsed": {
                        "total_years_experience": 5,
                        "experience": [
                            {"title": "Senior Backend Engineer", "is_current": True}
                        ],
                        "education": [],
                    }
                }
            ),
        ),
        patch.object(
            ctx.neo4j,
            "session",
            new=MagicMock(return_value=_empty_neo4j_session_cm()),
        ),
    ):
        with pytest.raises(RankingUnavailableError):
            await generate_shortlist(job_id, ctx)


def _empty_neo4j_session_cm() -> MagicMock:
    class _EmptyResult:
        def __aiter__(self) -> _EmptyResult:
            return self

        async def __anext__(self) -> Any:
            raise StopAsyncIteration

    session = MagicMock(name="session")
    session.run = AsyncMock(return_value=_EmptyResult())
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


@pytest.mark.asyncio
async def test_generate_shortlist_raises_ranking_unavailable_when_stage3_fails() -> (
    None
):
    from src.pipeline.matching.orchestrator import RankingUnavailableError

    job_id = uuid4()
    job = _job()
    resume_id = uuid4()
    candidate2 = _stage2_candidate(resume_id)

    with (
        patch(
            "src.pipeline.matching.orchestrator.load_job_view",
            new_callable=AsyncMock,
            return_value=job,
        ),
        patch(
            "src.pipeline.matching.orchestrator.stage1_coarse",
            new_callable=AsyncMock,
            return_value=[Stage1Candidate(resume_id=resume_id, vec_score=0.9)],
        ),
        patch(
            "src.pipeline.matching.orchestrator._stage2_per_candidate",
            new_callable=AsyncMock,
            return_value=candidate2,
        ),
        patch(
            "src.pipeline.matching.orchestrator.stage3_evidence",
            new_callable=AsyncMock,
            side_effect=LLMOutputInvalidError("empty response"),
        ),
    ):
        ctx = _ctx()
        with pytest.raises(RankingUnavailableError):
            await generate_shortlist(job_id, ctx)


@pytest.mark.asyncio
async def test_generate_shortlist_raises_ranking_unavailable_when_stage3_llm_unavailable() -> (  # noqa: E501
    None
):
    from src.pipeline.matching.orchestrator import RankingUnavailableError

    job_id = uuid4()
    job = _job()
    resume_id = uuid4()
    candidate2 = _stage2_candidate(resume_id)

    with (
        patch(
            "src.pipeline.matching.orchestrator.load_job_view",
            new_callable=AsyncMock,
            return_value=job,
        ),
        patch(
            "src.pipeline.matching.orchestrator.stage1_coarse",
            new_callable=AsyncMock,
            return_value=[Stage1Candidate(resume_id=resume_id, vec_score=0.9)],
        ),
        patch(
            "src.pipeline.matching.orchestrator._stage2_per_candidate",
            new_callable=AsyncMock,
            return_value=candidate2,
        ),
        patch(
            "src.pipeline.matching.orchestrator.stage3_evidence",
            new_callable=AsyncMock,
            side_effect=LLMUnavailableError("ollama down"),
        ),
    ):
        ctx = _ctx()
        with pytest.raises(RankingUnavailableError):
            await generate_shortlist(job_id, ctx)


@pytest.mark.asyncio
async def test_generate_shortlist_still_produces_a_normal_shortlist_on_a_generic_per_candidate_error() -> (  # noqa: E501
    None
):
    """A ``None`` evidence value that ``stage3_evidence`` RETURNS normally must
    still produce a shortlist.

    **Re-documented, not reversed** (ROADMAP A4 M1). The original docstring
    justified this by "a generic per-candidate exception is ALREADY isolated to
    ``None``" — that isolation is the defect and is gone. The test itself stays
    valid, because ``None`` still legitimately arrives here two ways: a
    candidate with nothing to evaluate (``_stage3_per_candidate``'s early
    return), and a candidate past the ``evidence_k`` cliff whose id is simply
    absent from the dict (``evidence_by_id.get`` → ``None``).

    Those must keep producing a shortlist. What must NOT is a candidate whose
    evidence stage raised — that now never reaches this point at all.

    The evidence cliff itself (a past-the-cliff candidate scoring an
    affirmative ``0%``) is ROADMAP A4's third item and is deliberately NOT
    addressed here — it needs a persisted ``evidence_evaluated`` marker."""
    job_id = uuid4()
    job = _job()
    resume_id = uuid4()
    candidate2 = _stage2_candidate(resume_id)

    with (
        patch(
            "src.pipeline.matching.orchestrator.load_job_view",
            new_callable=AsyncMock,
            return_value=job,
        ),
        patch(
            "src.pipeline.matching.orchestrator.stage1_coarse",
            new_callable=AsyncMock,
            return_value=[Stage1Candidate(resume_id=resume_id, vec_score=0.9)],
        ),
        patch(
            "src.pipeline.matching.orchestrator._stage2_per_candidate",
            new_callable=AsyncMock,
            return_value=candidate2,
        ),
        patch(
            "src.pipeline.matching.orchestrator.stage3_evidence",
            new_callable=AsyncMock,
            return_value={resume_id: None},  # the isolated-to-None outcome
        ),
    ):
        ctx = _ctx()
        result = await generate_shortlist(job_id, ctx)

    assert len(result.entries) == 1
    assert result.entries[0].resume_id == resume_id


@pytest.mark.asyncio
async def test_generate_shortlist_withholds_the_whole_shortlist_on_a_generic_stage3_error() -> (  # noqa: E501
    None
):
    """ROADMAP A4 M1, end to end: a non-LLM stage-3 failure must withhold the
    shortlist, not persist a silently-degraded one.

    The wrapping into ``RankingUnavailableError`` is what makes the existing
    machinery do the right thing for free: ``shortlist_job`` catches exactly
    that type, records the visible fail-closed state with ``reason=str(exc)``,
    and re-runs under ``arq.Retry`` up to ``shortlist_max_tries``. A transient
    Neo4j/Postgres hiccup — the realistic cause — is precisely what a retry
    fixes, so failing closed here costs a deferred re-run rather than a wrong
    ranking that nobody can reproduce later.
    """
    from src.pipeline.matching.orchestrator import RankingUnavailableError

    job_id = uuid4()
    job = _job()
    resume_id = uuid4()

    with (
        patch(
            "src.pipeline.matching.orchestrator.load_job_view",
            new_callable=AsyncMock,
            return_value=job,
        ),
        patch(
            "src.pipeline.matching.orchestrator.stage1_coarse",
            new_callable=AsyncMock,
            return_value=[Stage1Candidate(resume_id=resume_id, vec_score=0.9)],
        ),
        patch(
            "src.pipeline.matching.orchestrator._stage2_per_candidate",
            new_callable=AsyncMock,
            return_value=_stage2_candidate(resume_id),
        ),
        patch(
            "src.pipeline.matching.orchestrator.stage3_evidence",
            new_callable=AsyncMock,
            side_effect=RuntimeError("neo4j went away mid-run"),
        ),
    ):
        ctx = _ctx()
        with pytest.raises(RankingUnavailableError) as caught:
            await generate_shortlist(job_id, ctx)

    # The reason is surfaced verbatim into `jobs.shortlist_state_reason`, so it
    # has to name the real cause rather than a generic "ranking failed" — an
    # operator reading that column is the only person who will ever see it.
    assert "neo4j went away mid-run" in str(caught.value)


@pytest.mark.asyncio
async def test_a_generic_stage3_error_is_not_mislabelled_as_an_llm_failure() -> None:
    """Failing closed through the LLM path's machinery must not make the cause
    *look* like an LLM outage.

    ``shortlist_state`` has a CHECK constraint allowing only ``awaiting_llm``,
    so the state label is shared — but ``shortlist_state_reason`` is free text
    and is the only diagnostic an operator gets. It must distinguish "the model
    was down" from "the database blinked", because those have different fixes.
    """
    from src.pipeline.matching.orchestrator import RankingUnavailableError

    job_id = uuid4()
    job = _job()
    resume_id = uuid4()

    with (
        patch(
            "src.pipeline.matching.orchestrator.load_job_view",
            new_callable=AsyncMock,
            return_value=job,
        ),
        patch(
            "src.pipeline.matching.orchestrator.stage1_coarse",
            new_callable=AsyncMock,
            return_value=[Stage1Candidate(resume_id=resume_id, vec_score=0.9)],
        ),
        patch(
            "src.pipeline.matching.orchestrator._stage2_per_candidate",
            new_callable=AsyncMock,
            return_value=_stage2_candidate(resume_id),
        ),
        patch(
            "src.pipeline.matching.orchestrator.stage3_evidence",
            new_callable=AsyncMock,
            side_effect=ValueError("a coding error, not an outage"),
        ),
    ):
        ctx = _ctx()
        with pytest.raises(RankingUnavailableError) as caught:
            await generate_shortlist(job_id, ctx)

    reason = str(caught.value)
    assert "ValueError" in reason, (
        "the exception TYPE must reach the reason string — without it an "
        "operator cannot tell a transient outage from a bug that will retry "
        "to the ceiling and never succeed"
    )
    assert "a coding error, not an outage" in reason
