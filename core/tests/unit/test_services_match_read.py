"""Unit tests for Phase 6's reverse-match READ surface —
``src.services.shortlist_service.get_reverse_match_result``.

Placed alongside ``persist_reverse_match`` in ``shortlist_service.py`` (not a
new module) for symmetry: the write and read sides of the SAME table
(``reverse_match_entries``) live together, mirroring how
``persist_shortlist``/``list_for_job``/``get_one`` all live in this one
module for ``shortlist_entries``. This function does not exist yet — every
test below fails at collection or the first ``await``. RED half of the TDD
cycle.

**Ambiguities this file locks:**

* ``get_reverse_match_result(conn, resume_id) -> JobMatchResultOut`` reads
  ALL ``reverse_match_entries`` rows for ``resume_id`` (ordered by rank),
  joined to ``jobs`` for ``title``/``department``.
* A résumé that has never been reverse-matched (zero rows, NOT a missing
  résumé — this function does not 404 on an unknown résumé id either; that
  is the ROUTE's job, tested in ``test_route_reverse_match.py``) returns the
  EMPTY shape: ``JobMatchResultOut(resume_id=resume_id, entries=[],
  pipeline_meta=None, generated_at=None)``.
* THE MIRROR-IMAGE CONTRACT (ADR-010 §2), read side: ``reverse_match_entries``
  HAS dedicated ``score_structured``/``score_evidence`` COLUMNS (unlike
  ``shortlist_entries``, whose read path — ``shortlist_service._parse_entry_
  jsonb`` — must POP folded ``score_structured``/``score_evidence`` keys back
  out of ``score_breakdown`` before validating it, because THAT table folds
  them in on write). The reverse-match read must NOT apply that same pop —
  ``JobMatchEntry.score_structured``/``score_evidence`` come straight from
  their own SQL columns, and ``ScoreBreakdown.model_validate`` is called on
  the ``score_breakdown`` jsonb AS-IS (no fold to undo, because
  ``persist_reverse_match`` never folded anything into it in the first
  place).
* ``JobMatchResultOut.pipeline_meta``/``generated_at`` are TOP-level
  (singular), while the underlying table stores one ``pipeline_meta``/
  ``generated_at`` PER ROW (every row from one run carries the identical
  value, stamped once per orchestrator run) — the read function takes them
  from the first row.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from src.schemas.matching import JobMatchResultOut

_NOW = dt.datetime(2026, 7, 16, tzinfo=dt.UTC)


class _Row(dict[str, Any]):
    def __getitem__(self, key: str) -> Any:
        return dict.get(self, key)


def _score_breakdown_json() -> str:
    return json.dumps(
        {
            "skill": 0.8,
            "experience": 0.6,
            "education": 0.4,
            "seniority": 0.5,
            "vector": 0.3,
            "structured": 0.55,
            "motivation": 0.0,
            "implied_experience": False,
            "skill_contributions": [],
        }
    )


def _pipeline_meta_json() -> str:
    return json.dumps(
        {
            "model_gen": "test-gen",
            "model_emb": "test-emb",
            "prompt_versions": {},
            "weights": {},
            "git_sha": "abc123",
            "generated_at": _NOW.isoformat(),
            "timings_ms": {},
        }
    )


def _entry_row(
    *,
    job_id: UUID | None = None,
    title: str = "Senior Backend Engineer",
    rank: int = 1,
    score_final: float = 0.9,
    score_structured: float = 0.77,
    score_evidence: float = 0.88,
    evidence: dict[str, Any] | None = None,
) -> _Row:
    return _Row(
        {
            "job_id": job_id or uuid4(),
            "title": title,
            "department": "Engineering",
            "rank": rank,
            "score_final": score_final,
            "score_structured": score_structured,
            "score_evidence": score_evidence,
            "score_breakdown": _score_breakdown_json(),
            "evidence": json.dumps(evidence) if evidence is not None else None,
            "requirement_count": 5,
            "must_have_count": 2,
            "pipeline_meta": _pipeline_meta_json(),
            "generated_at": _NOW,
        }
    )


def _mock_conn(rows: list[_Row]) -> MagicMock:
    conn = MagicMock(name="conn")
    conn.fetch = AsyncMock(return_value=rows)
    return conn


# ── empty shape ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_reverse_match_result_empty_shape_for_never_matched_resume() -> None:
    from src.services.shortlist_service import get_reverse_match_result

    resume_id = uuid4()
    conn = _mock_conn([])
    out = await get_reverse_match_result(conn, resume_id)
    assert isinstance(out, JobMatchResultOut)
    assert out.resume_id == resume_id
    assert out.entries == []
    assert out.generated_at is None
    assert out.pipeline_meta is None


# ── real row mapping ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_reverse_match_result_maps_a_real_row() -> None:
    from src.services.shortlist_service import get_reverse_match_result

    resume_id = uuid4()
    job_id = uuid4()
    conn = _mock_conn([_entry_row(job_id=job_id, title="Staff Engineer", rank=1)])
    out = await get_reverse_match_result(conn, resume_id)
    assert len(out.entries) == 1
    entry = out.entries[0]
    assert entry.job_id == job_id
    assert entry.title == "Staff Engineer"
    assert entry.rank == 1


@pytest.mark.asyncio
async def test_reverse_match_result_uses_dedicated_score_columns() -> None:
    """score_structured/score_evidence come from their OWN SQL columns — the
    mirror image of the shortlist fold. If the implementation wrongly tried
    to pop them out of score_breakdown (which never had them folded in), the
    dedicated-column values below must still be what the DTO carries."""
    from src.services.shortlist_service import get_reverse_match_result

    conn = _mock_conn([_entry_row(score_structured=0.42, score_evidence=0.13)])
    out = await get_reverse_match_result(conn, uuid4())
    entry = out.entries[0]
    assert entry.score_structured == pytest.approx(0.42)
    assert entry.score_evidence == pytest.approx(0.13)


@pytest.mark.asyncio
async def test_get_reverse_match_result_score_breakdown_validates_without_a_pop() -> (
    None
):
    """ScoreBreakdown is extra='forbid' — if the read path wrongly injected a
    fold-pop step that expects (and silently ignores) score_structured/
    score_evidence keys inside score_breakdown, this would still pass; the
    REAL regression this guards is the read path calling
    ``ScoreBreakdown.model_validate`` on the jsonb AS-STORED (no popped keys
    ever existed on this table) without raising ValidationError."""
    from src.services.shortlist_service import get_reverse_match_result

    conn = _mock_conn([_entry_row()])
    out = await get_reverse_match_result(conn, uuid4())
    assert out.entries[0].score_breakdown.structured == pytest.approx(0.55)


@pytest.mark.asyncio
async def test_get_reverse_match_result_top_level_meta_from_first_row() -> None:
    from src.services.shortlist_service import get_reverse_match_result

    conn = _mock_conn([_entry_row(rank=1), _entry_row(rank=2)])
    out = await get_reverse_match_result(conn, uuid4())
    assert out.generated_at == _NOW
    assert out.pipeline_meta is not None
    assert out.pipeline_meta.model_gen == "test-gen"


@pytest.mark.asyncio
async def test_get_reverse_match_result_orders_multiple_entries_by_rank() -> None:
    from src.services.shortlist_service import get_reverse_match_result

    conn = _mock_conn([_entry_row(rank=1), _entry_row(rank=2), _entry_row(rank=3)])
    out = await get_reverse_match_result(conn, uuid4())
    ranks = [e.rank for e in out.entries]
    assert ranks == sorted(ranks)


@pytest.mark.asyncio
async def test_get_reverse_match_result_null_evidence_maps_to_none() -> None:
    from src.services.shortlist_service import get_reverse_match_result

    conn = _mock_conn([_entry_row(evidence=None)])
    out = await get_reverse_match_result(conn, uuid4())
    assert out.entries[0].evidence is None


@pytest.mark.asyncio
async def test_get_reverse_match_result_queries_the_reverse_match_entries_table() -> (
    None
):
    from src.services.shortlist_service import get_reverse_match_result

    conn = _mock_conn([])
    await get_reverse_match_result(conn, uuid4())
    query = conn.fetch.await_args.args[0]
    assert "reverse_match_entries" in query.lower()
