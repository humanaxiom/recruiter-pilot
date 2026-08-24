"""Security sign-off artifact: 4d introduces NO NEW PII redaction and NO NEW
PII leak beyond ``resumes.parsed``'s already-accepted at-rest cleartext
posture (ADR-007 §6 — "``resumes.parsed`` jsonb retains cleartext candidate
[data]; accepted for v1, documented ..., revisit before any multi-tenant
deploy. Phase 5 redaction is display-only and must not be mistaken for
at-rest protection.").

This file does NOT assert that redaction happens — the opposite: it pins the
DELIBERATE v1 decision that ``persist_shortlist`` writes a résumé-chunk
evidence quote to ``shortlist_entries.score_breakdown``/``evidence`` jsonb
VERBATIM, exactly as the LLM's stage-3 evidence extraction produced it (which
itself reads chunk text out of ``resumes.parsed``, per ADR-009's blocker #7).
Any change that starts silently scrubbing evidence text here would be a
scope-creep NEW redaction path outside 4d's remit (Phase 5's job, and even
then Phase 5 redaction is display-only, per ADR-007 §6/ADR-006 §4) — this
test exists so that change gets caught and reviewed, not introduced
accidentally.

``src.services.shortlist_service`` does not exist yet — RED half of the TDD
cycle; this whole file is expected to fail at collection
(``ModuleNotFoundError``).
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from src.pipeline.matching.orchestrator import ShortlistResult, ShortlistResultEntry
from src.schemas.matching import (
    DEFAULT_WEIGHTS,
    CoverLetterEvidence,
    EvidenceObject,
    PipelineMeta,
    RequirementEvidence,
    ScoreBreakdown,
)


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
        git_sha=None,
        generated_at=dt.datetime(2026, 7, 15, tzinfo=dt.UTC),
        timings_ms={},
    )


def _json_args(call: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for arg in list(call.args) + list(call.kwargs.values()):
        if not isinstance(arg, str):
            continue
        try:
            decoded = json.loads(arg)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(decoded, dict):
            out.append(decoded)
    return out


def _find_by_key(dicts: list[dict[str, Any]], key: str) -> dict[str, Any] | None:
    for d in dicts:
        if key in d:
            return d
    return None


def _mock_conn() -> MagicMock:
    conn = MagicMock(name="conn")
    conn.execute = AsyncMock()
    return conn


@pytest.mark.asyncio
async def test_persist_shortlist_writes_resume_chunk_evidence_quote_verbatim() -> None:
    from src.services.shortlist_service import persist_shortlist

    verbatim_quote = (
        "Jane Smith led the platform migration at Acme Corp from 2019 to 2022, "
        "reducing p99 latency by 40%."
    )
    evidence = EvidenceObject(
        requirements=[
            RequirementEvidence(
                requirement="Python",
                status="met",
                evidence=verbatim_quote,
                evidence_chunk_ids=["c_001"],
                confidence=0.92,
            )
        ],
        overall_summary="Strong candidate with direct platform-migration experience.",
    )
    job_id = uuid4()
    entry = ShortlistResultEntry(
        resume_id=uuid4(),
        rank=1,
        score_final=0.9,
        score_structured=0.8,
        score_evidence=0.7,
        breakdown=_breakdown(),
        evidence=evidence,
    )
    result = ShortlistResult(job_id=job_id, entries=[entry], pipeline_meta=_meta())
    conn = _mock_conn()

    await persist_shortlist(conn, result)

    insert_call = conn.execute.await_args_list[1]
    dicts = _json_args(insert_call)
    evidence_json = _find_by_key(dicts, "requirements")
    assert evidence_json is not None
    assert evidence_json["requirements"][0]["evidence"] == verbatim_quote, (
        "4d introduces no new redaction — a résumé-chunk quote must persist "
        "byte-for-byte identical to what stage-3 extracted"
    )
    assert evidence_json["overall_summary"] == evidence.overall_summary


@pytest.mark.asyncio
async def test_persist_shortlist_writes_cover_letter_evidence_quote_verbatim() -> None:
    from src.services.shortlist_service import persist_shortlist

    cover_quote = "I'm drawn to this role because of your team's open-source work."
    evidence = EvidenceObject(
        requirements=[],
        cover_letter_presence=True,
        cover_letter_evidence=[
            CoverLetterEvidence(
                theme="motivation",
                evidence=cover_quote,
                evidence_chunk_ids=["cl_001"],
                confidence=0.8,
            )
        ],
        overall_motivation="Genuinely motivated by the team's mission.",
    )
    job_id = uuid4()
    entry = ShortlistResultEntry(
        resume_id=uuid4(),
        rank=1,
        score_final=0.9,
        score_structured=0.8,
        score_evidence=0.7,
        breakdown=_breakdown(),
        evidence=evidence,
    )
    result = ShortlistResult(job_id=job_id, entries=[entry], pipeline_meta=_meta())
    conn = _mock_conn()

    await persist_shortlist(conn, result)

    insert_call = conn.execute.await_args_list[1]
    dicts = _json_args(insert_call)
    evidence_json = _find_by_key(dicts, "requirements")
    assert evidence_json is not None
    assert evidence_json["cover_letter_evidence"][0]["evidence"] == cover_quote
    assert evidence_json["overall_motivation"] == evidence.overall_motivation


@pytest.mark.asyncio
async def test_persist_shortlist_does_not_redact_a_pii_shaped_evidence_string() -> None:
    """A quote that happens to LOOK PII-shaped (an email address embedded in a
    résumé's verbatim work-history text) is still written unchanged — this is
    the deliberate v1 decision (ADR-007 §6), not an oversight. Any future
    redaction pass is a Phase-5, display-only concern and must not touch this
    at-rest write path silently."""
    from src.services.shortlist_service import persist_shortlist

    pii_shaped_quote = (
        "Contact reference: jane.doe@example.test, led the on-call rotation "
        "for the payments team."
    )
    evidence = EvidenceObject(
        requirements=[
            RequirementEvidence(
                requirement="On-call experience",
                status="met",
                evidence=pii_shaped_quote,
                evidence_chunk_ids=["c_003"],
                confidence=0.7,
            )
        ],
    )
    job_id = uuid4()
    entry = ShortlistResultEntry(
        resume_id=uuid4(),
        rank=1,
        score_final=0.9,
        score_structured=0.8,
        score_evidence=0.7,
        breakdown=_breakdown(),
        evidence=evidence,
    )
    result = ShortlistResult(job_id=job_id, entries=[entry], pipeline_meta=_meta())
    conn = _mock_conn()

    await persist_shortlist(conn, result)

    insert_call = conn.execute.await_args_list[1]
    dicts = _json_args(insert_call)
    evidence_json = _find_by_key(dicts, "requirements")
    assert evidence_json is not None
    assert evidence_json["requirements"][0]["evidence"] == pii_shaped_quote
