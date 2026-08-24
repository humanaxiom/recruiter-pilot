"""Unit tests — the evidence-cap INGEST/READ split (ADR-022 follow-up #3,
reviewer round 2, MAJOR 2).

The caps added for follow-up #3 were declared as pydantic ``max_length``
constraints on the models the READ path validates with. That broke retrieval:
``shortlist_service._parse_entry_jsonb`` (``:328``) and
``_row_to_job_match_entry`` (``:196``) both do an UNCAUGHT
``EvidenceObject.model_validate`` on stored JSONB, and this project has no
migration framework. So any row already on disk carrying a 2500-char quote or
100 requirements — exactly the pathological output the caps exist to stop —
made the whole shortlist / reverse-match endpoint 500 for that job. The bad
bytes became PERMANENTLY UNREADABLE.

HUMAN DECISION implemented here, in two halves:

1. **The read path is TOLERANT; ingest is STRICT.** A cap prevents a bad
   WRITE. Once the bytes are on disk the cap buys no protection at all and
   only breaks retrieval, so ``EvidenceObject`` / ``RequirementEvidence`` /
   ``CoverLetterEvidence`` — the DTO, read-path and ``verify_evidence`` models
   — declare NO length constraints, and the ``*Ingest`` subclasses carry them.
2. **At ingest, drop ONLY the offending quote.** A single over-long quote used
   to fail validation for the ENTIRE ``EvidenceObject``, so
   ``orchestrator._stage3_per_candidate`` fell into its
   ``LLMOutputInvalidError`` branch and returned ``None`` — the candidate lost
   every other requirement's evidence too. The ingest models now scrub the bad
   field and keep the rest, consistent with ``verify_evidence``, which already
   scrubs per-requirement rather than rejecting wholesale.

THE ANTI-SWAP ARM is the point of this file. Two mutations must both fail:

* using an ``*Ingest`` model on the read path — killed by the tests that
  assert an over-cap legacy row reads back BYTE-IDENTICAL (not merely
  "without raising"); and
* using a tolerant model at ingest — killed by
  ``test_orchestrator_parses_llm_evidence_with_the_strict_ingest_model`` plus
  the structural guard that the tolerant models declare zero ``MaxLen``.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from annotated_types import MaxLen
from pydantic import BaseModel

from src.schemas.matching import (
    MAX_EVIDENCE_QUOTE_CHARS,
    MAX_REQUIREMENTS,
    SCRUBBED_CONFIDENCE_CAP,
    CoverLetterEvidence,
    CoverLetterEvidenceIngest,
    EvidenceObject,
    EvidenceObjectIngest,
    RequirementEvidence,
    RequirementEvidenceIngest,
    ScoreBreakdown,
)

# ── fixtures: the pathological shapes the reviewer measured ─────────────────

# The reviewer's two concrete reproductions: a 2500-char quote raised
# ``string_too_long`` and a 100-requirement list raised ``too_long``, each
# taking down the whole endpoint for the job.
LEGACY_OVERSIZE_QUOTE = "x" * 2500
LEGACY_REQUIREMENT_COUNT = 100


def _legacy_evidence_dict() -> dict[str, Any]:
    """A row written BEFORE the caps existed: over-long quotes, an over-long
    requirement label, more requirements than the cap and more cited chunk ids
    than the cap. Every one of these is retrievable-forever data."""
    return {
        "requirements": [
            {
                "requirement": f"req-{i}",
                "status": "met",
                "evidence": LEGACY_OVERSIZE_QUOTE,
                "evidence_chunk_ids": [f"c_{j:03d}" for j in range(12)],
                "confidence": 0.9,
            }
            for i in range(LEGACY_REQUIREMENT_COUNT)
        ],
        "overall_summary": "s" * 4000,
        "cover_letter_presence": True,
        "cover_letter_evidence": [
            {
                "theme": "motivation",
                "evidence": LEGACY_OVERSIZE_QUOTE,
                "evidence_chunk_ids": [f"cl_{j:03d}" for j in range(12)],
                "confidence": 0.9,
            }
        ],
        "overall_motivation": "m" * 4000,
    }


class _Row(dict[str, Any]):
    """Dict-like fake asyncpg Record (same convention as
    ``test_services_shortlist_read.py``)."""

    def __getitem__(self, key: str) -> Any:
        return dict.get(self, key)


def _acm(return_value: Any = None) -> MagicMock:
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=return_value)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _breakdown_dict() -> dict[str, Any]:
    return ScoreBreakdown(
        skill=0.7,
        experience=0.6,
        education=0.5,
        seniority=0.5,
        vector=0.4,
        structured=0.6,
    ).model_dump()


def _shortlist_row(*, job_id: UUID) -> _Row:
    return _Row(
        {
            "id": uuid4(),
            "job_id": job_id,
            "resume_id": uuid4(),
            "rank": 1,
            "score_final": 0.9,
            "score_breakdown": json.dumps(_breakdown_dict()),
            "evidence": json.dumps(_legacy_evidence_dict()),
            "generated_at": dt.datetime(2026, 7, 15, tzinfo=dt.UTC),
        }
    )


def _reverse_match_row(*, resume_id: UUID) -> _Row:
    return _Row(
        {
            "job_id": uuid4(),
            "resume_id": resume_id,
            "title": "Backend Data Engineer",
            "department": "Platform",
            "rank": 1,
            "score_final": 0.9,
            "score_structured": 0.8,
            "score_evidence": 0.7,
            "score_breakdown": json.dumps(_breakdown_dict()),
            "evidence": json.dumps(_legacy_evidence_dict()),
            "requirement_count": 8,
            "must_have_count": 3,
            "pipeline_meta": None,
            "generated_at": dt.datetime(2026, 7, 15, tzinfo=dt.UTC),
        }
    )


def _mock_conn(*, rows: list[_Row] | None = None, row: _Row | None = None) -> MagicMock:
    conn = MagicMock(name="conn")
    conn.fetchval = AsyncMock(return_value=False)  # blind_review off
    conn.execute = AsyncMock()
    conn.transaction = MagicMock(return_value=_acm())
    conn.fetch = AsyncMock(return_value=rows or [])
    conn.fetchrow = AsyncMock(return_value=row)
    return conn


# ── 1. the READ path must stay tolerant ─────────────────────────────────────


def test_read_model_validates_a_legacy_over_cap_row_unchanged() -> None:
    """The narrowest statement of the decision: the model the read path uses
    accepts the pathological stored shape and hands back every byte."""
    ev = EvidenceObject.model_validate(_legacy_evidence_dict())

    assert len(ev.requirements) == LEGACY_REQUIREMENT_COUNT
    assert all(r.evidence == LEGACY_OVERSIZE_QUOTE for r in ev.requirements)
    assert all(len(r.evidence_chunk_ids) == 12 for r in ev.requirements)
    assert len(ev.overall_summary) == 4000
    assert len(ev.overall_motivation) == 4000
    assert ev.cover_letter_evidence[0].evidence == LEGACY_OVERSIZE_QUOTE


@pytest.mark.asyncio
async def test_list_for_job_reads_a_legacy_over_cap_row() -> None:
    """``_parse_entry_jsonb`` (:328) validates stored JSONB with no try/except.
    A cap there is a 500 on the whole shortlist for the job."""
    from src.services.shortlist_service import list_for_job

    job_id = uuid4()
    conn = _mock_conn(rows=[_shortlist_row(job_id=job_id)])

    entries = await list_for_job(conn, job_id=job_id)

    assert len(entries) == 1
    ev = entries[0].evidence
    assert ev is not None
    assert len(ev.requirements) == LEGACY_REQUIREMENT_COUNT
    # Byte-identical, not merely "did not raise" — this is what kills the
    # mutation that swaps the strict ingest model in on the read path.
    assert ev.requirements[0].evidence == LEGACY_OVERSIZE_QUOTE


@pytest.mark.asyncio
async def test_get_one_reads_a_legacy_over_cap_row() -> None:
    from src.services.shortlist_service import get_one

    job_id = uuid4()
    row = _shortlist_row(job_id=job_id)
    conn = _mock_conn(row=row)

    entry = await get_one(conn, UUID(str(row["id"])))

    assert entry.evidence is not None
    assert len(entry.evidence.requirements) == LEGACY_REQUIREMENT_COUNT
    assert entry.evidence.requirements[0].evidence == LEGACY_OVERSIZE_QUOTE


@pytest.mark.asyncio
async def test_get_reverse_match_result_reads_a_legacy_over_cap_row() -> None:
    """``_row_to_job_match_entry`` (:196) is the mirror image of the shortlist
    read path and carries the identical uncaught ``model_validate``."""
    from src.services.shortlist_service import get_reverse_match_result

    resume_id = uuid4()
    conn = _mock_conn(rows=[_reverse_match_row(resume_id=resume_id)])

    result = await get_reverse_match_result(conn, resume_id)

    assert len(result.entries) == 1
    ev = result.entries[0].evidence
    assert ev is not None
    assert len(ev.requirements) == LEGACY_REQUIREMENT_COUNT
    assert ev.requirements[0].evidence == LEGACY_OVERSIZE_QUOTE


@pytest.mark.asyncio
async def test_export_rows_reads_a_legacy_over_cap_row() -> None:
    """``export_rows`` keeps evidence as plain dicts today. Pinned so a future
    edit cannot introduce a validating step and re-break the export."""
    from src.services.shortlist_service import export_rows

    row = {
        "rank": 1,
        "resume_id": uuid4(),
        "score_final": 0.9,
        "score_breakdown": _breakdown_dict(),
        "evidence": _legacy_evidence_dict(),
        "pipeline_meta": {"git_sha": None, "weights": {}},
        "generated_at": dt.datetime(2026, 7, 15, tzinfo=dt.UTC),
        "job_title": "Backend Data Engineer",
        "job_department": "Platform",
        "original_filename": "resume.pdf",
        "candidate_name": "Candidate A",
        "candidate_email": None,
        "candidate_phone": None,
        "candidate_parsed": None,
    }
    conn = _mock_conn(rows=[_Row(row)])

    out = await export_rows(conn, job_id=uuid4(), reveal=True)

    assert len(out) == 1
    reqs = out[0]["evidence"]["requirements"]
    assert len(reqs) == LEGACY_REQUIREMENT_COUNT
    assert reqs[0]["evidence"] == LEGACY_OVERSIZE_QUOTE


# ── 2. the tolerant models must carry NO length caps (structural guard) ─────

TOLERANT_MODELS: tuple[type[BaseModel], ...] = (
    RequirementEvidence,
    CoverLetterEvidence,
    EvidenceObject,
)


@pytest.mark.parametrize(
    "model", TOLERANT_MODELS, ids=[m.__name__ for m in TOLERANT_MODELS]
)
def test_tolerant_read_model_declares_no_length_constraint_at_all(
    model: type[BaseModel],
) -> None:
    """The invariant that makes the split checkable rather than a convention:
    a read model with ANY ``MaxLen`` can 500 the endpoint on a legacy row, so
    re-adding one to these three fails here by name."""
    offenders = [
        name
        for name, info in model.model_fields.items()
        if any(isinstance(m, MaxLen) for m in info.metadata)
    ]
    assert not offenders, (
        f"{model.__name__} declares max_length on {offenders} — the read path "
        "validates stored JSONB with this model and there is no migration "
        "framework, so a cap here makes pre-existing rows permanently "
        "unreadable. Caps belong on the *Ingest subclass."
    )


INGEST_PAIRS: tuple[tuple[type[BaseModel], type[BaseModel]], ...] = (
    (RequirementEvidenceIngest, RequirementEvidence),
    (CoverLetterEvidenceIngest, CoverLetterEvidence),
    (EvidenceObjectIngest, EvidenceObject),
)


@pytest.mark.parametrize(
    "ingest, tolerant", INGEST_PAIRS, ids=[i.__name__ for i, _ in INGEST_PAIRS]
)
def test_ingest_model_is_a_subclass_of_its_tolerant_read_model(
    ingest: type[BaseModel], tolerant: type[BaseModel]
) -> None:
    """Variance runs one way only: an ingest instance is usable everywhere a
    read instance is (so ``verify_evidence`` / the DTOs take it unchanged),
    but never the reverse."""
    assert issubclass(ingest, tolerant)
    assert ingest is not tolerant


# ── 3. at ingest, drop ONLY the offending quote ─────────────────────────────


def test_ingest_drops_one_over_long_quote_and_keeps_every_other_requirement() -> None:
    """The core of the second human decision. One bad quote must not cost the
    candidate all of their evidence."""
    payload = {
        "requirements": [
            {
                "requirement": "Python",
                "status": "met",
                "evidence": "Designed and shipped Python REST APIs at Nimbus.",
                "evidence_chunk_ids": ["c_001"],
                "confidence": 0.9,
            },
            {
                "requirement": "Kubernetes",
                "status": "met",
                "evidence": "y" * (MAX_EVIDENCE_QUOTE_CHARS + 1),
                "evidence_chunk_ids": ["c_002"],
                "confidence": 0.95,
            },
            {
                "requirement": "PostgreSQL",
                "status": "partial",
                "evidence": "Optimized PostgreSQL schemas and queries.",
                "evidence_chunk_ids": ["c_003"],
                "confidence": 0.8,
            },
        ]
    }

    ev = EvidenceObjectIngest.model_validate(payload)

    assert len(ev.requirements) == 3, "the whole object must survive"
    kept_first, offender, kept_last = ev.requirements

    assert kept_first.evidence.startswith("Designed and shipped Python")
    assert kept_first.status == "met"
    assert kept_first.confidence == 0.9
    assert kept_last.evidence.startswith("Optimized PostgreSQL")
    assert kept_last.status == "partial"
    assert kept_last.confidence == 0.8

    assert offender.evidence == ""
    assert offender.requirement == "Kubernetes", "the row itself is kept"


def test_ingest_demotes_the_dropped_quote_exactly_like_verify_evidence() -> None:
    """Blanking alone would be a scoring hole: ``_evidence_completeness``
    counts ``met AND confidence >= evidence_met_confidence`` and never looks at
    the quote text, so a blanked-but-still-``met`` requirement would keep full
    credit for evidence that was thrown away."""
    ev = EvidenceObjectIngest.model_validate(
        {
            "requirements": [
                {
                    "requirement": "Kubernetes",
                    "status": "met",
                    "evidence": "y" * (MAX_EVIDENCE_QUOTE_CHARS + 1),
                    "evidence_chunk_ids": ["c_002"],
                    "confidence": 0.95,
                }
            ]
        }
    )
    req = ev.requirements[0]
    assert req.evidence == ""
    assert req.status == "missing"
    assert req.confidence <= SCRUBBED_CONFIDENCE_CAP


def test_ingest_does_not_truncate_the_offending_quote() -> None:
    """Truncating would be actively unsafe: the prefix of a superset-bypass
    quote can still contain the cited chunk verbatim, so a truncated quote
    could go on to VERIFY at 1.000. Drop, never trim."""
    chunk = "Designed and shipped Python REST APIs consumed by 40+ services."
    payload = {
        "requirements": [
            {
                "requirement": "Python",
                "status": "met",
                "evidence": chunk + ("z" * MAX_EVIDENCE_QUOTE_CHARS),
                "evidence_chunk_ids": ["c_001"],
                "confidence": 0.95,
            }
        ]
    }
    req = EvidenceObjectIngest.model_validate(payload).requirements[0]
    assert req.evidence == ""
    assert chunk not in req.evidence


def test_ingest_drops_an_over_long_cover_letter_quote_and_keeps_the_theme() -> None:
    ev = EvidenceObjectIngest.model_validate(
        {
            "cover_letter_presence": True,
            "cover_letter_evidence": [
                {
                    "theme": "motivation",
                    "evidence": "I'm reaching out because the role lines up.",
                    "evidence_chunk_ids": ["cl_001"],
                    "confidence": 0.9,
                },
                {
                    "theme": "growth",
                    "evidence": "y" * (MAX_EVIDENCE_QUOTE_CHARS + 1),
                    "evidence_chunk_ids": ["cl_002"],
                    "confidence": 0.9,
                },
            ],
        }
    )
    assert len(ev.cover_letter_evidence) == 2
    assert ev.cover_letter_evidence[0].evidence.startswith("I'm reaching out")
    assert ev.cover_letter_evidence[0].confidence == 0.9
    assert ev.cover_letter_evidence[1].evidence == ""
    assert ev.cover_letter_evidence[1].theme == "growth"
    assert ev.cover_letter_evidence[1].confidence <= SCRUBBED_CONFIDENCE_CAP


def test_ingest_truncates_an_over_long_requirement_label_rather_than_dropping() -> None:
    """The label is not a quote and is not evidence of anything — it is the
    row's key. Blanking it would orphan the requirement, so this one is
    trimmed."""
    ev = EvidenceObjectIngest.model_validate(
        {"requirements": [{"requirement": "R" * 900}]}
    )
    assert ev.requirements[0].requirement == "R" * 500


def test_ingest_truncates_the_requirements_list_instead_of_rejecting() -> None:
    """A 100-entry list used to raise ``too_long`` and cost the candidate ALL
    evidence. The first ``MAX_REQUIREMENTS`` survive."""
    ev = EvidenceObjectIngest.model_validate(
        {"requirements": [{"requirement": f"req-{i}"} for i in range(100)]}
    )
    assert len(ev.requirements) == MAX_REQUIREMENTS
    assert ev.requirements[0].requirement == "req-0"


def test_ingest_bounds_the_pathological_100000_entry_list() -> None:
    """The count ADR-022 names explicitly. Bounded BEFORE per-item validation
    (a ``mode="before"`` slice), so the O(n) scrub never runs 100,000 times —
    which the old ``max_length`` cap did not actually prevent."""
    ev = EvidenceObjectIngest.model_validate(
        {"requirements": [{"requirement": "x"} for _ in range(100_000)]}
    )
    assert len(ev.requirements) == MAX_REQUIREMENTS


def test_ingest_truncates_over_long_chunk_id_lists() -> None:
    ev = EvidenceObjectIngest.model_validate(
        {
            "requirements": [
                {
                    "requirement": "Python",
                    "evidence_chunk_ids": [f"c_{i}" for i in range(30)],
                }
            ]
        }
    )
    assert ev.requirements[0].evidence_chunk_ids == [f"c_{i}" for i in range(8)]


def test_ingest_truncates_over_long_overall_text() -> None:
    ev = EvidenceObjectIngest.model_validate(
        {"overall_summary": "s" * 4000, "overall_motivation": "m" * 4000}
    )
    assert ev.overall_summary == "s" * 1000
    assert ev.overall_motivation == "m" * 1000


def test_ingest_never_raises_on_the_worst_legacy_shape() -> None:
    """End of the argument: the strict model is strict by SCRUBBING, so the
    ingest boundary can no longer throw away a whole candidate's evidence."""
    ev = EvidenceObjectIngest.model_validate(_legacy_evidence_dict())
    assert len(ev.requirements) == MAX_REQUIREMENTS
    assert all(r.evidence == "" for r in ev.requirements)
    assert all(r.status == "missing" for r in ev.requirements)


def test_a_corpus_sized_payload_is_untouched_by_the_ingest_caps() -> None:
    """Anti-over-rejection. The caps are only defensible if real evidence
    passes through byte-identical (longest corpus chunk = 148 chars, longest
    cover-letter chunk = 243, JD requirement count = 8)."""
    quote = (
        "Designed and shipped Python REST APIs consumed by 40+ internal "
        "services at Nimbus Analytics Inc, using FastAPI and typed "
        "request/response contracts."
    )
    payload = {
        "requirements": [
            {
                "requirement": name,
                "status": "met",
                "evidence": quote,
                "evidence_chunk_ids": ["c_001"],
                "confidence": 0.9,
            }
            for name in ("Python", "PostgreSQL", "Apache Airflow", "Docker")
        ],
        "overall_summary": "Strong overall fit.",
    }
    strict = EvidenceObjectIngest.model_validate(payload)
    tolerant = EvidenceObject.model_validate(payload)
    assert strict.model_dump() == tolerant.model_dump()


# ── 4. the ingest model is wired at exactly one place ───────────────────────


def test_orchestrator_parses_llm_evidence_with_the_strict_ingest_model() -> None:
    """Kills the swap in the other direction. ``_stage3_per_candidate``'s
    ``chat_json`` call is THE ingest boundary; if it validates with the
    tolerant model the caps stop existing anywhere."""
    import inspect

    from src.pipeline.matching import orchestrator

    source = inspect.getsource(orchestrator._stage3_per_candidate)
    assert "EvidenceObjectIngest," in source, (
        "the chat_json schema argument in _stage3_per_candidate must be "
        "EvidenceObjectIngest — the tolerant EvidenceObject enforces no caps"
    )


INGEST_MODEL_NAMES = (
    "EvidenceObjectIngest",
    "RequirementEvidenceIngest",
    "CoverLetterEvidenceIngest",
)


@pytest.mark.parametrize("name", INGEST_MODEL_NAMES)
def test_shortlist_service_does_not_import_any_ingest_model(name: str) -> None:
    """The read layer must never reach for a strict model. Checks the module
    NAMESPACE rather than the source text, so the read path is free to name
    these models in comments explaining why it does not use them — while an
    actual import (the only way one could be called) still fails here. Static,
    so it holds for read paths this file does not exercise."""
    from src.services import shortlist_service

    assert not hasattr(shortlist_service, name), (
        f"shortlist_service imported {name}; it is a READ layer, and a strict "
        "model there re-breaks retrieval of pre-existing rows"
    )


# ── security FINDING 5 — the tolerant/strict split at the WRITE boundary ────
#
# The split above is enforced at exactly one point: the ``chat_json`` schema
# argument. Everything downstream of it — ``ShortlistResultEntry.evidence``,
# ``JobMatchResultEntry.evidence``, ``_JobScore.evidence`` and both persist
# functions (which read those dataclasses) — was annotated with the TOLERANT
# ``EvidenceObject``. So a tolerant, uncapped instance was TYPE-LEGAL all the
# way to ``json.dumps`` and into Postgres; the only thing preventing it was
# that both producers happen to funnel through ``_stage3_per_candidate``.
#
# No live bypass exists today. The point is that the split should be
# STRUCTURAL rather than a convention a future producer can miss without mypy
# saying anything. ``verify_evidence`` and ``stage4_combine`` are generic in
# the evidence type, so ingest-ness survives the whole pipeline instead of
# being widened away at stage 3's return.
#
# These tests read ANNOTATIONS, so the mutation they kill — swapping the write
# boundary back to ``EvidenceObject`` — fails pytest and not only mypy. A
# type-level-only guarantee is invisible to the ranking gate.

WRITE_BOUNDARY_FIELDS: tuple[tuple[str, str], ...] = (
    ("ShortlistResultEntry", "evidence"),
    ("JobMatchResultEntry", "evidence"),
    ("_JobScore", "evidence"),
)


_WRITE_BOUNDARY_IDS = [f"{n}.{f}" for n, f in WRITE_BOUNDARY_FIELDS]


@pytest.mark.parametrize(
    "dataclass_name, field", WRITE_BOUNDARY_FIELDS, ids=_WRITE_BOUNDARY_IDS
)
def test_write_boundary_dataclass_is_typed_with_the_strict_ingest_model(
    dataclass_name: str, field: str
) -> None:
    """What ``persist_shortlist`` / ``persist_reverse_match`` are handed must
    be an ingest instance by TYPE, not by the accident of who built it."""
    import typing

    from src.pipeline.matching import orchestrator

    hints = typing.get_type_hints(getattr(orchestrator, dataclass_name))
    assert hints[field] == (EvidenceObjectIngest | None), (
        f"{dataclass_name}.{field} is {hints[field]}; the write boundary must "
        "be EvidenceObjectIngest | None so a tolerant instance cannot reach "
        "persist_* without mypy objecting"
    )


def test_stage3_returns_the_strict_model_so_ingest_ness_is_not_widened_away() -> None:
    """The load-bearing link. If ``_stage3_per_candidate`` declares
    ``EvidenceObject | None`` the write-boundary annotations above become
    unsatisfiable and someone widens THEM back instead."""
    import typing

    from src.pipeline.matching import orchestrator

    hints = typing.get_type_hints(orchestrator._stage3_per_candidate)
    assert hints["return"] == (EvidenceObjectIngest | None)


def test_verify_evidence_preserves_the_model_class_it_was_given() -> None:
    """The runtime half of the same guarantee: ``verify_evidence`` is generic
    because it round-trips through ``model_copy``, which preserves the class.
    An implementation that rebuilt an ``EvidenceObject`` would silently
    downgrade every verified object to the tolerant model."""
    from src.pipeline.matching.stages import verify_evidence

    strict = EvidenceObjectIngest.model_validate(
        {"requirements": [{"requirement": "Python", "status": "met", "evidence": "x"}]}
    )
    out = verify_evidence(strict, {})
    assert type(out) is EvidenceObjectIngest
    assert type(out.requirements[0]) is RequirementEvidenceIngest

    tolerant = EvidenceObject.model_validate({"requirements": []})
    assert type(verify_evidence(tolerant, {})) is EvidenceObject
