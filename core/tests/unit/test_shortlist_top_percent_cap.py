"""Unit tests -- the pure top-P% cap the ranking engine applies to a job's
shortlist (per-job configurable shortlist size, slice B).

Slice A (already GREEN on this branch) added ``jobs.shortlist_top_percent``
(1-100, default 100) as a schema-only column: nothing reads it yet, and
``generate_shortlist`` builds/returns every ranked entry unconditionally.
Slice B wires the cap into the ranking engine itself.

**Pinned contract the coder must implement** (``src/pipeline/matching/
orchestrator.py``):

    def _apply_top_percent_cap(
        entries: Sequence[ShortlistResultEntry], top_percent: int
    ) -> list[ShortlistResultEntry]:
        ...

Called from ``generate_shortlist`` AFTER stage4_combine builds the full,
rank-ordered ``entries`` list (rank 1 = best) and BEFORE the list is handed
to ``ShortlistResult`` / persisted. ``generate_shortlist`` itself gains a
``top_percent: int = 100`` keyword parameter that is threaded straight into
this helper -- 100 (the default, and the schema's own default) must keep
every entry, byte-identical to today's uncapped behaviour, so the eval
corpus (which never sets ``shortlist_top_percent``) is unaffected.

Formula pinned by this file: ``n_keep = ceil(len(entries) * top_percent /
100)``, floored at 1 -- EXCEPT when ``entries`` is empty, where ``n_keep``
must be 0 (a floor of 1 would fabricate a candidate out of nothing). The kept
entries are always the entries at the FRONT of the input list (index
``[:n_keep]``) -- ``generate_shortlist`` already hands this function a
rank-ordered list (rank 1 first), so the cap is a plain prefix-slice, never a
re-sort.

Neither ``_apply_top_percent_cap`` nor ``generate_shortlist``'s
``top_percent`` parameter exist yet -- every test below fails at import
time (``ImportError`` on the helper) or at call time (``TypeError:
unexpected keyword argument 'top_percent'``). RED half of the TDD cycle.
"""

from __future__ import annotations

import inspect
import math
from typing import Any
from uuid import uuid4

import pytest

from src.schemas.matching import ScoreBreakdown


def _breakdown() -> ScoreBreakdown:
    return ScoreBreakdown(
        skill=0.5,
        experience=0.5,
        education=0.5,
        seniority=0.5,
        vector=0.5,
        structured=0.5,
    )


def _entries(n: int) -> list[Any]:
    from src.pipeline.matching.orchestrator import ShortlistResultEntry

    return [
        ShortlistResultEntry(
            resume_id=uuid4(),
            rank=i + 1,
            score_final=1.0 - (i * 0.01),
            score_structured=1.0 - (i * 0.01),
            score_evidence=0.0,
            breakdown=_breakdown(),
            evidence=None,
        )
        for i in range(n)
    ]


# ── the ceil-based formula, at the documented sizes ─────────────────────────


@pytest.mark.parametrize(
    ("n", "pct", "expected_kept"),
    [
        (10, 100, 10),
        (10, 50, 5),
        (50, 20, 10),
        (50, 1, 1),
        (0, 100, 0),
    ],
)
def test_cap_keeps_ceil_n_times_pct_over_100(
    n: int, pct: int, expected_kept: int
) -> None:
    from src.pipeline.matching.orchestrator import _apply_top_percent_cap

    entries = _entries(n)
    kept = _apply_top_percent_cap(entries, pct)
    assert len(kept) == expected_kept


def test_cap_formula_matches_ceil_directly_for_an_odd_case() -> None:
    """N=7, pct=30 -> ceil(2.1) = 3, not floor(2.1) = 2 -- pins ceil, not
    round-half or floor, as the rounding rule."""
    from src.pipeline.matching.orchestrator import _apply_top_percent_cap

    entries = _entries(7)
    kept = _apply_top_percent_cap(entries, 30)
    assert len(kept) == math.ceil(7 * 30 / 100) == 3


# ── top_percent=100 keeps ALL entries, unchanged (the eval-preservation path) ──


def test_top_percent_100_returns_every_entry_in_original_order() -> None:
    from src.pipeline.matching.orchestrator import _apply_top_percent_cap

    entries = _entries(12)
    kept = _apply_top_percent_cap(entries, 100)
    assert kept == entries
    assert [e.resume_id for e in kept] == [e.resume_id for e in entries]


# ── the kept entries are the HIGHEST-ranked prefix, not a re-sort ──────────


def test_cap_keeps_the_top_ranked_prefix_not_an_arbitrary_subset() -> None:
    from src.pipeline.matching.orchestrator import _apply_top_percent_cap

    entries = _entries(10)
    kept = _apply_top_percent_cap(entries, 50)
    assert len(kept) == 5
    assert kept == entries[:5]
    assert [e.rank for e in kept] == [1, 2, 3, 4, 5]
    assert [e.resume_id for e in kept] == [e.resume_id for e in entries[:5]]


def test_cap_never_promotes_a_lower_ranked_entry_into_the_kept_set() -> None:
    from src.pipeline.matching.orchestrator import _apply_top_percent_cap

    entries = _entries(10)
    kept = _apply_top_percent_cap(entries, 20)
    kept_ids = {e.resume_id for e in kept}
    dropped_ids = {e.resume_id for e in entries[2:]}
    assert kept_ids.isdisjoint(dropped_ids)
    assert kept_ids == {e.resume_id for e in entries[:2]}


# ── the minimum-1 floor, and its N=0 exception ──────────────────────────────


def test_cap_floors_at_one_even_for_a_tiny_percent_on_a_nonempty_pool() -> None:
    """top_percent=1 on N=50 -> ceil(0.5) = 1, already >= 1, but this also
    proves the floor holds even if a future weights/precision change made the
    raw ceil computation land at 0 for a nonempty pool."""
    from src.pipeline.matching.orchestrator import _apply_top_percent_cap

    entries = _entries(50)
    kept = _apply_top_percent_cap(entries, 1)
    assert len(kept) == 1
    assert kept[0] is entries[0]


def test_cap_on_an_empty_pool_at_top_percent_100_keeps_zero_not_one() -> None:
    """N=0 is the one case where the floor must NOT apply -- there is
    nothing to keep, so a naive ``max(1, ...)`` that ignores the empty-input
    case would fabricate a candidate out of thin air."""
    from src.pipeline.matching.orchestrator import _apply_top_percent_cap

    kept = _apply_top_percent_cap([], 100)
    assert kept == []


def test_cap_on_an_empty_pool_at_a_low_percent_also_keeps_zero() -> None:
    from src.pipeline.matching.orchestrator import _apply_top_percent_cap

    kept = _apply_top_percent_cap([], 20)
    assert kept == []


# ── generate_shortlist gains a top_percent kwarg (structural pin) ──────────


def test_generate_shortlist_accepts_a_top_percent_keyword_argument() -> None:
    """A structural signature pin, independent of the full async DB/Neo4j
    path exercised in the integration suite: ``generate_shortlist`` must
    accept ``top_percent`` as a keyword parameter, defaulting to 100 -- the
    value that keeps today's uncapped behaviour byte-identical."""
    from src.pipeline.matching.orchestrator import generate_shortlist

    sig = inspect.signature(generate_shortlist)
    assert "top_percent" in sig.parameters
    assert sig.parameters["top_percent"].default == 100


def test_match_resume_to_jobs_does_not_gain_a_top_percent_parameter() -> None:
    """Reverse match (résumé -> jobs) is a SEPARATE function from the
    forward shortlist and must NOT be capped by any job's
    ``shortlist_top_percent`` -- that column is a forward-shortlist job
    property, not a résumé-scoped one. This is a regression guard: it is
    expected to already pass (today's signature has no such parameter) and
    must keep passing after slice B lands."""
    from src.pipeline.matching.orchestrator import match_resume_to_jobs

    sig = inspect.signature(match_resume_to_jobs)
    assert "top_percent" not in sig.parameters
