"""Unit tests for ``src.services.shortlist_service.list_for_job`` /
``get_one`` — the Phase 5 read + blind-review redaction boundary for the
shortlist. All I/O is mocked (a bare ``MagicMock`` connection, following the
``_Row``/``_acm`` conventions in ``test_worker_shortlist_job.py`` /
``test_pii.py``); the same contract proven against a real Postgres lives in
``tests/integration/test_shortlist_read_export_pg.py``.

``src.services.shortlist_service.list_for_job`` / ``.get_one`` do not exist
yet — RED half of the TDD cycle; every test below fails, either at collection
(``ImportError`` — the two names aren't exported yet) or at the first
``await`` (``AttributeError``).

Column-alias contract these mocks assume for the BLIND branch (ported from
hris ``shortlist_service._BLIND_COLS``): the SQL the implementation issues
under ``jobs.blind_review = TRUE`` must alias the joined ``resumes`` row's
decrypted PII / parsed json as ``_c_name`` / ``_c_email`` / ``_c_phone`` /
``_c_parsed`` so the read layer can redact them out of the evidence text
before they are ever assigned onto a response field.

THE TWO LOAD-BEARING TESTS IN THIS FILE:

1. ``test_score_breakdown_fold_does_not_break_read`` — ``persist_shortlist``
   (4d) folds ``score_structured``/``score_evidence`` INTO the
   ``score_breakdown`` jsonb it writes (ADR-010 §2). ``ScoreBreakdown`` has
   ``model_config = ConfigDict(extra="forbid")``, so a naive
   ``ScoreBreakdown.model_validate(row["score_breakdown"])`` on ANY row 4d
   ever wrote RAISES ``pydantic.ValidationError``. The read layer MUST strip
   the two folded keys before validating. If a future edit reverts to a bare
   ``.model_validate()`` call, THIS test is what catches it.
2. ``test_blind_entry_redacts_cover_letter_evidence_and_motivation_text`` —
   hris's ``_redact_evidence`` redacts ``requirements[].evidence`` /
   ``overall_summary`` but NEVER touches ``cover_letter_evidence[].evidence``
   / ``overall_motivation`` — a real gap: a cover-letter quote can carry the
   candidate's own name. This project's read layer MUST close that gap (a
   must-fix-beyond-verbatim-port, not an accepted residual).

RED-FIRST EXTENSION ("Why this rank?" defense pack, slice 1) — the two fold
tests above are EXTENDED (not weakened) to additionally assert that
``score_structured``/``score_evidence`` — which the fold strips out of
``score_breakdown`` before ``ScoreBreakdown.model_validate`` — SURVIVE onto
the ``ShortlistEntry`` DTO itself, rather than being discarded once stripped.
Until now discarding them was correct (``ShortlistEntry`` had no fields to
put them in and no consumer needed them); this flip is a deliberate,
legitimate RED-first extension, not a contradiction of the original test's
intent — the fold-safety guard they already prove stays intact byte-for-byte,
this only adds a further assertion on the same read path.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from src.errors import NotFoundError
from src.schemas.matching import (
    DEFAULT_WEIGHTS,
    EvidenceObject,
    MatchWeights,
    PipelineMeta,
    ScoreBreakdown,
)


class _Row(dict[str, Any]):
    """Dict-like fake asyncpg Record: an absent key returns ``None`` instead
    of raising ``KeyError``."""

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


def _folded_breakdown_dict() -> dict[str, Any]:
    """Exactly what ``persist_shortlist`` writes to ``score_breakdown``: a
    full ``ScoreBreakdown.model_dump()`` PLUS the two folded keys."""
    raw = ScoreBreakdown(
        skill=0.8,
        experience=0.6,
        education=0.4,
        seniority=0.5,
        vector=0.3,
        structured=0.55,
        motivation=0.1,
    ).model_dump()
    raw["score_structured"] = 0.77
    raw["score_evidence"] = 0.66
    return raw


def _entry_row(
    *,
    entry_id: UUID | None = None,
    job_id: UUID,
    resume_id: UUID | None = None,
    rank: int = 1,
    score_breakdown: dict[str, Any] | None = None,
    evidence: dict[str, Any] | None = None,
    generated_at: dt.datetime | None = None,
) -> _Row:
    return _Row(
        {
            "id": entry_id or uuid4(),
            "job_id": job_id,
            "resume_id": resume_id or uuid4(),
            "rank": rank,
            "score_final": 0.9,
            "score_breakdown": json.dumps(score_breakdown or _breakdown_dict()),
            "evidence": json.dumps(evidence if evidence is not None else {}),
            "generated_at": generated_at or dt.datetime(2026, 7, 15, tzinfo=dt.UTC),
        }
    )


def _blind_entry_row(
    *,
    entry_id: UUID | None = None,
    job_id: UUID,
    resume_id: UUID | None = None,
    rank: int = 1,
    score_breakdown: dict[str, Any] | None = None,
    evidence: dict[str, Any] | None = None,
    name: str | None = "Jane Smith",
    email: str | None = "jane.smith@example.test",
    phone: str | None = "604-555-0192",
    parsed: dict[str, Any] | None = None,
    generated_at: dt.datetime | None = None,
) -> _Row:
    row = _entry_row(
        entry_id=entry_id,
        job_id=job_id,
        resume_id=resume_id,
        rank=rank,
        score_breakdown=score_breakdown,
        evidence=evidence,
        generated_at=generated_at,
    )
    row["_c_name"] = name
    row["_c_email"] = email
    row["_c_phone"] = phone
    row["_c_parsed"] = json.dumps(parsed) if parsed is not None else None
    return row


def _mock_conn(
    *,
    blind: bool,
    rows: list[_Row] | None = None,
    row: _Row | None = None,
) -> MagicMock:
    conn = MagicMock(name="conn")
    conn.fetchval = AsyncMock(return_value=blind)
    conn.execute = AsyncMock()
    conn.transaction = MagicMock(return_value=_acm())
    conn.fetch = AsyncMock(return_value=rows or [])
    conn.fetchrow = AsyncMock(return_value=row)
    return conn


# ── landmine 1: the ScoreBreakdown fold read guard ──────────────────────────


@pytest.mark.asyncio
async def test_score_breakdown_fold_does_not_break_read() -> None:
    from src.services.shortlist_service import list_for_job

    job_id = uuid4()
    folded = _folded_breakdown_dict()
    row = _entry_row(job_id=job_id, score_breakdown=folded, evidence={})
    conn = _mock_conn(blind=False, rows=[row])

    entries = await list_for_job(conn, job_id=job_id)

    assert len(entries) == 1
    entry = entries[0]
    assert isinstance(entry.score_breakdown, ScoreBreakdown)
    assert entry.score_breakdown.skill == pytest.approx(0.8)
    assert entry.score_breakdown.experience == pytest.approx(0.6)
    assert entry.score_breakdown.structured == pytest.approx(0.55)
    # RED-FIRST EXTENSION (Why-this-rank? slice 1): the two folded keys are
    # stripped from score_breakdown before ScoreBreakdown.model_validate (the
    # assertions above), but they must not simply vanish — they belong on the
    # ShortlistEntry DTO itself so the defense-pack panel can show them.
    assert entry.score_structured == pytest.approx(0.77)
    assert entry.score_evidence == pytest.approx(0.66)


@pytest.mark.asyncio
async def test_score_breakdown_fold_does_not_break_read_via_get_one() -> None:
    """Same landmine, proven through get_one's own row-to-model path (a
    separate code path from list_for_job in most implementations)."""
    from src.services.shortlist_service import get_one

    entry_id = uuid4()
    folded = _folded_breakdown_dict()
    row = _entry_row(
        job_id=uuid4(), entry_id=entry_id, score_breakdown=folded, evidence={}
    )
    conn = _mock_conn(blind=False, row=row)

    entry = await get_one(conn, entry_id)

    assert isinstance(entry.score_breakdown, ScoreBreakdown)
    assert entry.score_breakdown.skill == pytest.approx(0.8)
    # RED-FIRST EXTENSION (Why-this-rank? slice 1) — see the sibling
    # assertion above; same guard, proven through get_one's own path.
    assert entry.score_structured == pytest.approx(0.77)
    assert entry.score_evidence == pytest.approx(0.66)


# ── landmine 2 setup: the evidence={} residual (documented, not a bug) ──────


@pytest.mark.asyncio
async def test_shortlist_evidence_empty_dict_deserializes_not_none() -> None:
    """``shortlist_entries.evidence == {}`` is the 4d DELETE-first coercion
    for a ``NOT NULL`` column (``persist_shortlist``'s ``evidence=None -> {}``
    rule). The read layer must deserialize ``{}`` into a valid, empty-fielded
    ``EvidenceObject``, NOT ``None`` — and it does NOT (and structurally
    CANNOT) disambiguate "never evidence-scored" from "scored, found nothing"
    at this layer. That information loss is an ACCEPTED ADR-010 §2 residual,
    not a bug this test is meant to catch."""
    from src.services.shortlist_service import list_for_job

    job_id = uuid4()
    row = _entry_row(job_id=job_id, evidence={})
    conn = _mock_conn(blind=False, rows=[row])

    entries = await list_for_job(conn, job_id=job_id)

    assert entries[0].evidence is not None
    assert isinstance(entries[0].evidence, EvidenceObject)
    assert entries[0].evidence.requirements == []
    assert entries[0].evidence.overall_summary == ""


# ── non-blind vs blind branch ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_non_blind_job_returns_real_evidence_unblinded() -> None:
    from src.services.shortlist_service import list_for_job

    job_id = uuid4()
    real_evidence = {
        "requirements": [
            {
                "requirement": "Python",
                "status": "met",
                "evidence": "Jane Smith built the payments service.",
                "evidence_chunk_ids": ["c_001"],
                "confidence": 0.9,
            }
        ],
        "overall_summary": "Strong candidate.",
    }
    row = _entry_row(job_id=job_id, rank=1, evidence=real_evidence)
    conn = _mock_conn(blind=False, rows=[row])

    entries = await list_for_job(conn, job_id=job_id)

    entry = entries[0]
    assert entry.blinded is False
    assert entry.display_label is None
    assert entry.evidence is not None
    assert "Jane Smith" in entry.evidence.requirements[0].evidence
    assert entry.evidence.overall_summary == "Strong candidate."


@pytest.mark.asyncio
async def test_blind_job_masks_identity_and_labels_display_by_rank() -> None:
    from src.services.shortlist_service import list_for_job

    job_id = uuid4()
    evidence_1 = {
        "requirements": [
            {
                "requirement": "Python",
                "status": "met",
                "evidence": "Jane Smith led the migration at Acme Corp.",
                "evidence_chunk_ids": ["c_001"],
                "confidence": 0.9,
            }
        ],
        "overall_summary": "Jane Smith is a strong candidate.",
    }
    parsed_1 = {
        "candidate": {
            "name": "Jane Smith",
            "email": "jane.smith@example.test",
            "phone": "604-555-0192",
            "location": None,
        },
        "summary": "",
        "total_years_experience": 0,
        "skills": [],
        "experience": [{"company": "Acme Corp", "title": "Engineer", "bullets": []}],
        "education": [],
        "chunks": [],
        "cover_letter_chunks": [],
    }
    evidence_2 = {"requirements": [], "overall_summary": "Bob Lee is also strong."}
    parsed_2 = {
        "candidate": {"name": "Bob Lee"},
        "summary": "",
        "total_years_experience": 0,
        "skills": [],
        "experience": [],
        "education": [],
        "chunks": [],
        "cover_letter_chunks": [],
    }

    row1 = _blind_entry_row(
        job_id=job_id,
        rank=1,
        evidence=evidence_1,
        parsed=parsed_1,
        name="Jane Smith",
        email="jane.smith@example.test",
        phone="604-555-0192",
    )
    row2 = _blind_entry_row(
        job_id=job_id,
        rank=2,
        evidence=evidence_2,
        parsed=parsed_2,
        name="Bob Lee",
        email=None,
        phone=None,
    )
    conn = _mock_conn(blind=True, rows=[row1, row2])

    entries = await list_for_job(conn, job_id=job_id)

    assert entries[0].blinded is True
    assert entries[0].display_label == "Candidate A"
    assert entries[1].blinded is True
    assert entries[1].display_label == "Candidate B"

    assert entries[0].evidence is not None
    req = entries[0].evidence.requirements[0]
    assert "Jane Smith" not in req.evidence
    assert "jane.smith@example.test" not in req.evidence
    assert "Acme Corp" not in req.evidence
    assert "Employer A" in req.evidence
    assert "Jane Smith" not in entries[0].evidence.overall_summary


# ── landmine 2: cover-letter evidence redaction gap, CLOSED ─────────────────


@pytest.mark.asyncio
async def test_blind_entry_redacts_cover_letter_evidence_and_motivation_text() -> None:
    from src.services.shortlist_service import list_for_job

    job_id = uuid4()
    evidence = {
        "requirements": [],
        "cover_letter_presence": True,
        "cover_letter_evidence": [
            {
                "theme": "motivation",
                "evidence": "I, Jane Smith, am drawn to this role.",
                "evidence_chunk_ids": ["cl_001"],
                "confidence": 0.8,
            }
        ],
        "overall_motivation": "Jane Smith is genuinely motivated by the mission.",
    }
    parsed = {
        "candidate": {"name": "Jane Smith"},
        "summary": "",
        "total_years_experience": 0,
        "skills": [],
        "experience": [],
        "education": [],
        "chunks": [],
        "cover_letter_chunks": [],
    }
    row = _blind_entry_row(
        job_id=job_id, rank=1, evidence=evidence, parsed=parsed, name="Jane Smith"
    )
    conn = _mock_conn(blind=True, rows=[row])

    entries = await list_for_job(conn, job_id=job_id)

    ev = entries[0].evidence
    assert ev is not None
    assert "Jane Smith" not in ev.cover_letter_evidence[0].evidence
    assert "Jane Smith" not in ev.overall_motivation


@pytest.mark.asyncio
async def test_black_box_scan_no_pii_in_blind_shortlist_entry() -> None:
    """Black-box guard on the FULL serialized ``ShortlistEntry`` — catches a
    masking call applied AFTER DTO construction (only some fields nulled) as
    well as an outright missing masking call, not just the individually
    checked fields above."""
    from src.services.shortlist_service import list_for_job

    job_id = uuid4()
    real_name = "Zzyzxqrst Wibblesworth"
    real_email = "zzyzxqrst.wibblesworth@example.test"
    real_phone = "778-555-0199"
    evidence = {
        "requirements": [
            {
                "requirement": "X",
                "status": "met",
                "evidence": f"{real_name} built the whole thing.",
                "evidence_chunk_ids": [],
                "confidence": 0.9,
            }
        ],
        "cover_letter_evidence": [
            {
                "theme": "motivation",
                "evidence": f"Contact {real_name} at {real_email} or {real_phone}.",
                "evidence_chunk_ids": [],
                "confidence": 0.7,
            }
        ],
        "overall_summary": f"{real_name} is impressive.",
        "overall_motivation": f"{real_name} really wants this job.",
    }
    parsed = {
        "candidate": {"name": real_name, "email": real_email, "phone": real_phone},
        "summary": "",
        "total_years_experience": 0,
        "skills": [],
        "experience": [],
        "education": [],
        "chunks": [],
        "cover_letter_chunks": [],
    }
    row = _blind_entry_row(
        job_id=job_id,
        rank=1,
        evidence=evidence,
        parsed=parsed,
        name=real_name,
        email=real_email,
        phone=real_phone,
    )
    conn = _mock_conn(blind=True, rows=[row])

    entries = await list_for_job(conn, job_id=job_id)
    dumped = entries[0].model_dump_json()

    assert real_name not in dumped
    assert real_email not in dumped
    assert real_phone not in dumped


# ── empty job / get_one branches ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_empty_job_returns_empty_list_not_error() -> None:
    from src.services.shortlist_service import list_for_job

    job_id = uuid4()
    conn = _mock_conn(blind=False, rows=[])

    entries = await list_for_job(conn, job_id=job_id)

    assert entries == []


@pytest.mark.asyncio
async def test_get_one_nonexistent_entry_raises_not_found() -> None:
    from src.services.shortlist_service import get_one

    conn = _mock_conn(blind=False, row=None)
    # blind lookup on a missing entry -> NULL from the join
    conn.fetchval = AsyncMock(return_value=None)

    with pytest.raises(NotFoundError):
        await get_one(conn, uuid4())


@pytest.mark.asyncio
async def test_get_one_non_blind_returns_real_entry() -> None:
    from src.services.shortlist_service import get_one

    entry_id = uuid4()
    row = _entry_row(job_id=uuid4(), entry_id=entry_id, evidence={"requirements": []})
    conn = _mock_conn(blind=False, row=row)

    entry = await get_one(conn, entry_id)

    assert entry.id == entry_id
    assert entry.blinded is False
    assert entry.display_label is None


@pytest.mark.asyncio
async def test_get_one_blind_returns_masked_entry() -> None:
    from src.services.shortlist_service import get_one

    entry_id = uuid4()
    parsed = {
        "candidate": {"name": "Jane Smith"},
        "summary": "",
        "total_years_experience": 0,
        "skills": [],
        "experience": [],
        "education": [],
        "chunks": [],
        "cover_letter_chunks": [],
    }
    evidence = {
        "requirements": [
            {
                "requirement": "Python",
                "status": "met",
                "evidence": "Jane Smith did great work.",
                "evidence_chunk_ids": [],
                "confidence": 0.8,
            }
        ]
    }
    row = _blind_entry_row(
        job_id=uuid4(),
        entry_id=entry_id,
        rank=3,
        evidence=evidence,
        parsed=parsed,
        name="Jane Smith",
    )
    conn = _mock_conn(blind=True, row=row)

    entry = await get_one(conn, entry_id)

    assert entry.blinded is True
    assert entry.display_label == "Candidate C"
    assert entry.evidence is not None
    assert "Jane Smith" not in entry.evidence.requirements[0].evidence


# ── the malformed-stamp guard (Why-this-rank? slice 1) ──────────────────────
#
# ``_parse_pipeline_meta``'s ``return None`` on the failure branch is the ONLY
# thing that makes the module docstring's central claim true: "a malformed
# stamp can never be mistaken for the weights that actually produced the
# score". It was previously enforced by a COMMENT ONLY -- review proved that
# this mutant survived the entire gate with EXIT=0:
#
#     except ValidationError:
#   +     if isinstance(raw_meta, dict) and "weights" in raw_meta:
#   +         return PipelineMeta(model_gen="", model_emb="", prompt_versions={},
#   +                             weights=DEFAULT_WEIGHTS,
#   +                             generated_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC))
#         return None
#
# i.e. a malformed stamp rendering as TODAY's DEFAULT_WEIGHTS, presented to a
# recruiter as the weights that produced a historical score. That is precisely
# the dishonesty this whole feature exists to prevent, so it gets real tests.


def _valid_meta_dict() -> dict[str, Any]:
    """A well-formed stamp, as JSON round-tripped bytes -- exactly the shape
    ``_meta_json``/``model_dump_json`` writes to the jsonb column."""
    return dict(
        json.loads(
            PipelineMeta(
                model_gen="gpt-oss:20b",
                model_emb="nomic-embed-text",
                prompt_versions={"shortlist_evidence": "shortlist_evidence_v1"},
                weights=DEFAULT_WEIGHTS,
                git_sha="abc123def",
                generated_at=dt.datetime(2026, 7, 1, tzinfo=dt.UTC),
                timings_ms={},
            ).model_dump_json()
        )
    )


def test_parse_pipeline_meta_accepts_a_well_formed_stamp() -> None:
    """POSITIVE CONTROL. Every ``is None`` assertion below is only meaningful
    if the parser actually accepts a genuine stamp -- otherwise a parser that
    returned ``None`` unconditionally would pass them all."""
    from src.services.shortlist_service import _parse_pipeline_meta

    meta = _parse_pipeline_meta(_valid_meta_dict())

    assert meta is not None
    assert meta.git_sha == "abc123def"
    assert meta.model_emb == "nomic-embed-text"


def test_parse_pipeline_meta_accepts_a_stamp_arriving_as_a_json_string() -> None:
    """POSITIVE CONTROL for the other codec: asyncpg hands jsonb back as a
    dict OR as a JSON string depending on the codec in force."""
    from src.services.shortlist_service import _parse_pipeline_meta

    meta = _parse_pipeline_meta(json.dumps(_valid_meta_dict()))

    assert meta is not None
    assert meta.git_sha == "abc123def"


def test_parse_pipeline_meta_degenerate_write_path_stamp_is_none() -> None:
    """THE stamp the write path itself produces. ``_meta_json(None)`` writes
    ``{"weights": DEFAULT_WEIGHTS}`` and NOTHING else -- purely to satisfy
    ``pipeline_meta JSONB NOT NULL`` for a job that vanished mid-run. Those
    weights are a placeholder that never ranked anything, so the read path must
    refuse the whole stamp rather than let the panel present them as the
    generation-time weights."""
    from src.services.shortlist_service import _meta_json, _parse_pipeline_meta

    degenerate = json.loads(_meta_json(None))
    # The exact shape the surviving mutant keyed on: a dict WITH "weights".
    assert isinstance(degenerate, dict)
    assert "weights" in degenerate

    assert _parse_pipeline_meta(degenerate) is None


def test_parse_pipeline_meta_unknown_extra_key_is_none() -> None:
    """``PipelineMeta`` is ``extra="forbid"``. A stamp carrying a key this
    build does not know (a row written by a newer build, or a hand-edited row)
    must be refused WHOLESALE -- never silently downgraded to today's
    defaults, which is what makes the refusal honest."""
    from src.services.shortlist_service import _parse_pipeline_meta

    drifted = {**_valid_meta_dict(), "sampling_temperature": 0.2}

    assert _parse_pipeline_meta(drifted) is None


def test_parse_pipeline_meta_weights_failing_the_sum_validator_is_none() -> None:
    """THE REALISTIC FUTURE TRIGGER. There is no migration framework here, so
    the day ``MatchWeights`` gains or renames a field, EVERY historical stamp
    starts failing validation at once. Simulated with weights whose top-level
    sum breaks ``MatchWeights._sums_close_to_one`` (0.9+0.9+0.1). The panel
    must fall back to "weights unavailable"; substituting DEFAULT_WEIGHTS
    would turn a schema drift into a silent, fabricated audit trail on every
    row in the table."""
    from src.services.shortlist_service import _parse_pipeline_meta

    raw = _valid_meta_dict()
    weights = dict(raw["weights"])
    weights["structured"] = 0.9
    weights["evidence"] = 0.9
    raw["weights"] = weights
    # Sanity: these weights really are rejected by MatchWeights itself.
    with pytest.raises(ValidationError):
        MatchWeights.model_validate(weights)

    assert _parse_pipeline_meta(raw) is None


def test_parse_pipeline_meta_unparseable_string_is_none() -> None:
    """A jsonb column value that is not JSON at all (nothing can rewrite the
    row, so a raise here would 500 the whole shortlist permanently)."""
    from src.services.shortlist_service import _parse_pipeline_meta

    assert _parse_pipeline_meta("{not json at all") is None


@pytest.mark.asyncio
async def test_malformed_stamp_row_explains_as_weights_unavailable() -> None:
    """END-TO-END, stored bytes -> the panel the recruiter reads: a row whose
    ``pipeline_meta`` is the degenerate write-path stamp must arrive at the
    explanation as "weights unavailable", with NO weight and NO contribution on
    ANY row -- never today's DEFAULT_WEIGHTS wearing the costume of the weights
    that produced this score."""
    from src.services.explanation import shortlist_entry_explanation
    from src.services.shortlist_service import _meta_json, get_one

    entry_id = uuid4()
    row = _entry_row(job_id=uuid4(), entry_id=entry_id, evidence={})
    row["pipeline_meta"] = _meta_json(None)
    conn = _mock_conn(blind=False, row=row)

    entry = await get_one(conn, entry_id)
    assert entry.pipeline_meta is None

    explanation = shortlist_entry_explanation(entry)
    assert explanation.weights_available is False
    for name in (
        "structured",
        "evidence",
        "motivation",
        "skill",
        "experience",
        "education",
        "seniority",
        "vector",
    ):
        contribution_row = getattr(explanation, name)
        assert contribution_row.weight is None, name
        assert contribution_row.contribution is None, name


# ── absent fold: "not recorded", never an affirmative 0% ────────────────────


@pytest.mark.asyncio
async def test_row_without_folded_subscores_reports_none_not_zero() -> None:
    """A row whose ``score_breakdown`` carries NO folded
    ``score_structured``/``score_evidence`` (a pre-4d row) must surface them as
    ``None`` -- "not recorded" -- not as ``0.0``. ``0.0`` renders as an
    affirmative "0% contribution", a POSITIVE FALSE CLAIM, and it is asymmetric
    with the ``pipeline_meta=None`` handling right beside it, which already
    refuses to state what it does not know."""
    from src.services.shortlist_service import list_for_job

    job_id = uuid4()
    unfolded = _breakdown_dict()
    assert "score_structured" not in unfolded
    row = _entry_row(job_id=job_id, score_breakdown=unfolded, evidence={})
    conn = _mock_conn(blind=False, rows=[row])

    entries = await list_for_job(conn, job_id=job_id)

    assert entries[0].score_structured is None
    assert entries[0].score_evidence is None


# ── the read path's WARNING logs carry no candidate field ────────────────────
#
# ``_parse_pipeline_meta``/``_folded_subscore`` are the only two log sites on
# this read path, and both run on a row that (under blind review) has just been
# joined to DECRYPTED candidate PII. Their docstring promises "entry id only --
# never candidate fields", and by construction they emit only ``entry_id``, the
# jsonb key name, ``type(value).__name__``, ``exc.error_count()`` and the
# pydantic error *loc* path (field NAMES, never values).
#
# That is safe TODAY and unenforced: nothing failed if a future edit widened a
# format string to ``%s`` the payload, which on this path would write decrypted
# candidate text into the application log -- defeating the redaction boundary
# in exactly the place a compliance artifact must not. The frontend sibling
# (``test_entry_detail_malformed_payload_is_logged_not_swallowed_silently`` in
# test_frontend_shortlist.py) already pins its own log site this way; these are
# the missing service-side halves.
#
# Mutation-proven: widening EITHER log call to also emit the offending value
# (e.g. adding ``raw_meta``/``value`` as a trailing ``%s`` argument) fails the
# corresponding test below.

_LOG_PII_NAME = "Zzyzxqrst Wibblesworth"
_LOG_PII_EMAIL = "zzyzxqrst.wibblesworth@example.test"
_LOG_PII_PHONE = "604-555-0192"


def test_parse_pipeline_meta_validation_warning_logs_no_candidate_field(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A malformed stamp whose VALUES carry candidate identity must be logged
    with the entry id and the failure shape only."""
    from src.services.shortlist_service import _parse_pipeline_meta

    entry_id = uuid4()
    raw = _valid_meta_dict()
    raw["model_gen"] = _LOG_PII_NAME
    raw["prompt_versions"] = {"shortlist_evidence": _LOG_PII_EMAIL}
    raw["git_sha"] = _LOG_PII_PHONE
    # Forces the ValidationError branch (``extra="forbid"``).
    raw["sampling_temperature"] = 0.2

    with caplog.at_level("WARNING"):
        assert _parse_pipeline_meta(raw, entry_id=entry_id) is None

    assert caplog.records, "a refused stamp must be logged, not swallowed"
    assert str(entry_id) in caplog.text
    assert _LOG_PII_NAME not in caplog.text
    assert _LOG_PII_EMAIL not in caplog.text
    assert _LOG_PII_PHONE not in caplog.text


def test_parse_pipeline_meta_bad_json_warning_logs_no_candidate_field(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The other refusal branch: a jsonb value that is not JSON at all. The
    unparseable bytes are the ROW's own content and must not be echoed."""
    from src.services.shortlist_service import _parse_pipeline_meta

    entry_id = uuid4()
    corrupt = f"{{not json at all -- {_LOG_PII_NAME} <{_LOG_PII_EMAIL}>"

    with caplog.at_level("WARNING"):
        assert _parse_pipeline_meta(corrupt, entry_id=entry_id) is None

    assert caplog.records, "an unparseable stamp must be logged, not swallowed"
    assert str(entry_id) in caplog.text
    assert _LOG_PII_NAME not in caplog.text
    assert _LOG_PII_EMAIL not in caplog.text


def test_folded_subscore_warning_logs_no_candidate_field(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``_folded_subscore``'s refusal log names the jsonb KEY and the value's
    TYPE — never the value, which on a corrupted row can be arbitrary stored
    text."""
    from src.services.shortlist_service import _folded_subscore

    entry_id = uuid4()

    with caplog.at_level("WARNING"):
        assert (
            _folded_subscore(
                f"{_LOG_PII_NAME} {_LOG_PII_EMAIL} {_LOG_PII_PHONE}",
                key="score_structured",
                entry_id=entry_id,
            )
            is None
        )

    assert caplog.records, "an unreadable sub-score must be logged, not swallowed"
    assert str(entry_id) in caplog.text
    assert "score_structured" in caplog.text
    assert _LOG_PII_NAME not in caplog.text
    assert _LOG_PII_EMAIL not in caplog.text
    assert _LOG_PII_PHONE not in caplog.text


@pytest.mark.asyncio
async def test_blind_read_warning_logs_no_candidate_field_end_to_end(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The same guard on the REAL path: a BLIND row (joined to decrypted PII)
    whose stamp and folded sub-score are both corrupt. Neither refusal may put
    the candidate's decrypted name/email/phone into the log."""
    from src.services.shortlist_service import list_for_job

    job_id = uuid4()
    corrupt = _breakdown_dict()
    corrupt["score_structured"] = _LOG_PII_NAME
    parsed = {
        "candidate": {
            "name": _LOG_PII_NAME,
            "email": _LOG_PII_EMAIL,
            "phone": _LOG_PII_PHONE,
        },
        "experience": [],
        "education": [],
        "chunks": [],
        "cover_letter_chunks": [],
    }
    row = _blind_entry_row(
        job_id=job_id,
        evidence={},
        score_breakdown=corrupt,
        parsed=parsed,
        name=_LOG_PII_NAME,
        email=_LOG_PII_EMAIL,
        phone=_LOG_PII_PHONE,
    )
    row["pipeline_meta"] = "{not json at all"
    conn = _mock_conn(blind=True, rows=[row])

    with caplog.at_level("WARNING"):
        entries = await list_for_job(conn, job_id=job_id)

    assert entries[0].score_structured is None
    assert entries[0].pipeline_meta is None
    assert caplog.records, "both refusals must be logged, not swallowed"
    assert _LOG_PII_NAME not in caplog.text
    assert _LOG_PII_EMAIL not in caplog.text
    assert _LOG_PII_PHONE not in caplog.text


@pytest.mark.asyncio
async def test_non_numeric_folded_subscore_degrades_instead_of_500ing() -> None:
    """``float(folded)`` on a corrupted jsonb value raises ``ValueError``, NOT
    ``ValidationError`` -- so it escapes ``_parse_pipeline_meta``'s tolerance
    and would 500 the entire shortlist read permanently (nothing can rewrite
    the row). It must degrade to "not recorded" like every other unreadable
    field on this path."""
    from src.services.shortlist_service import list_for_job

    job_id = uuid4()
    corrupt = _breakdown_dict()
    corrupt["score_structured"] = "not-a-number"
    corrupt["score_evidence"] = {"nested": "garbage"}
    row = _entry_row(job_id=job_id, score_breakdown=corrupt, evidence={})
    conn = _mock_conn(blind=False, rows=[row])

    entries = await list_for_job(conn, job_id=job_id)

    assert entries[0].score_structured is None
    assert entries[0].score_evidence is None
