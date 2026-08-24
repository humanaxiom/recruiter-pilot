"""RED pin — ROADMAP A4's evidence cliff, write path: the marker must record
what actually happened, and survive the round trip.

``tests/unit/test_evidence_cliff_disclosure.py`` pins how the marker is
*rendered*. This pins that it is *true* — set from the real ``evidence_k``
boundary rather than guessed — and that ``persist_shortlist`` /
``_parse_entry_jsonb`` carry it intact.

**Why it is folded into ``score_breakdown`` rather than given a column.**
``shortlist_entries`` has no dedicated columns for ``score_structured`` /
``score_evidence`` either; ``persist_shortlist`` already folds those into the
``score_breakdown`` jsonb and ``_parse_entry_jsonb`` unfolds them. Following
the established shape means no DDL, and it gives the legacy case the right
answer for free: a pre-existing row simply has no folded key, which unfolds to
``None`` — "we do not know" — exactly the third state the panel needs.

``ScoreBreakdown`` is ``extra="forbid"``, so a folded key that is not popped
before validation raises on **every** row. That is the failure mode this file
exists to catch early rather than in production.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import pytest

from src.schemas.matching import ScoreBreakdown


def _breakdown() -> ScoreBreakdown:
    return ScoreBreakdown(
        skill=0.8,
        experience=0.7,
        education=0.6,
        seniority=0.5,
        vector=0.4,
        structured=0.65,
    )


def test_the_marker_survives_the_fold_and_unfold_round_trip() -> None:
    """The whole point of folding: what goes in comes back out."""
    from src.services.shortlist_service import _parse_entry_jsonb

    folded: dict[str, Any] = _breakdown().model_dump()
    folded["score_structured"] = 0.65
    folded["score_evidence"] = 0.0
    folded["evidence_evaluated"] = False

    raw: dict[str, Any] = {
        "id": uuid4(),
        "score_breakdown": json.dumps(folded),
        "evidence": None,
    }
    _structured, _evidence, evaluated = _parse_entry_jsonb(raw)

    assert evaluated is False
    assert isinstance(raw["score_breakdown"], ScoreBreakdown), (
        "the folded marker was not popped before validating ScoreBreakdown "
        "(extra='forbid') — this raises on EVERY row, not just marked ones"
    )


def test_a_legacy_row_without_the_marker_unfolds_to_unknown() -> None:
    """A row written before this slice has no folded key. It must unfold to
    ``None`` — "we do not know whether stage 3 ran" — never to a convenient
    ``True``/``False``, either of which invents a fact about a real ranking."""
    from src.services.shortlist_service import _parse_entry_jsonb

    folded: dict[str, Any] = _breakdown().model_dump()
    folded["score_structured"] = 0.65
    folded["score_evidence"] = 0.0

    raw: dict[str, Any] = {
        "id": uuid4(),
        "score_breakdown": json.dumps(folded),
        "evidence": None,
    }
    assert _parse_entry_jsonb(raw)[2] is None


@pytest.mark.parametrize("junk", ["yes", 1.5, {}, []])
def test_a_corrupted_marker_degrades_to_unknown_rather_than_raising(
    junk: Any,
) -> None:
    """Same tolerance the sibling folded values already have: this runs on
    bytes already committed to a NOT NULL column that nothing can rewrite, so a
    raise here would 500 the whole shortlist PERMANENTLY. Degrading to "we do
    not know" is the honest answer and the safe one.

    Note ``1.5`` and ``"yes"`` are truthy: a bare ``bool(value)`` would report
    them as an affirmative "assessed", which is precisely the invented fact
    this marker exists to avoid.
    """
    from src.services.shortlist_service import _parse_entry_jsonb

    folded: dict[str, Any] = _breakdown().model_dump()
    folded["evidence_evaluated"] = junk

    raw: dict[str, Any] = {
        "id": uuid4(),
        "score_breakdown": json.dumps(folded),
        "evidence": None,
    }
    assert _parse_entry_jsonb(raw)[2] is None


def test_the_orchestrator_marks_exactly_the_candidates_stage_3_ran_for() -> None:
    """The marker must come from the real ``evidence_k`` boundary.

    Built as an ordering fact rather than a recomputation: entries ranked
    within ``evidence_k`` of the STRUCTURED ordering are the ones handed to
    stage 3. Anything that re-derives it downstream (from ``rank``, or from
    ``requirements == []``) is inferring pipeline state from a display
    artifact, which ROADMAP A4 names explicitly as the wrong fix.
    """
    from src.pipeline.matching.orchestrator import ShortlistResultEntry

    fields = ShortlistResultEntry.__dataclass_fields__
    assert "evidence_evaluated" in fields, (
        "ShortlistResultEntry carries no evidence_evaluated marker, so the "
        "write path cannot record which candidates stage 3 actually saw"
    )
