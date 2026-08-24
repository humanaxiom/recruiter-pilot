"""Cheap SOURCE-level pins on where Stage 2's rendered skill label may come
from (merge-blocking security gate, findings S1 and S2).

These complement — they do not replace — the behavioural pins in
``tests/integration/test_skill_display_names_pg.py``. The integration tests
need a real Neo4j; these run in the offline gate and fail loudly the moment
anyone reintroduces either removed rung, which is what makes the invariant
cheap to keep.

* **S1 (MEDIUM).** Security mutated the Cypher to
  ``coalesce(has.evidence_chunk_id, req.display_name, ...)`` — promoting a
  **résumé-side** property to the top of the label's source chain — and the
  full gate stayed green. The label must be **JD-authored only**: the only
  identifiers allowed inside the ``coalesce(...) AS skill`` expression are
  ``req.*`` (the per-job REQUIRES relationship) and ``reqSkill.*`` (the Skill
  node the JD requires).
* **S2 (MEDIUM).** ``reqSkill.display_name`` is a GLOBAL, last-writer-wins
  property (``src.worker.tasks._job_projection_tx`` writes it with a
  job-less ``MATCH (s:Skill {canonical_key: $cname})``), so reading it here
  renders ANOTHER job's JD wording to this job's assignees. It was removed
  from the chain; a stale edge renders the opaque ``canonical_key`` instead.
"""

from __future__ import annotations

import inspect
import re

from src.pipeline.matching.orchestrator import _stage2_skill_rows

_SKILL_LABEL_EXPR = re.compile(r"coalesce\(([^)]*)\)\s*AS skill", re.IGNORECASE)

# Any `identifier.property` reference, e.g. `req.display_name`.
_DOTTED_REF = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\.[A-Za-z_][A-Za-z0-9_]*")

_ALLOWED_SOURCES = {"req", "reqSkill"}


def _skill_label_expression() -> str:
    src = inspect.getsource(_stage2_skill_rows)
    match = _SKILL_LABEL_EXPR.search(src)
    assert match is not None, (
        "could not find the `coalesce(...) AS skill` expression in "
        "_stage2_skill_rows — if the label's construction was rewritten, "
        "rewrite this pin too; do not delete it (security findings S1/S2)"
    )
    return match.group(1)


def test_skill_label_reads_only_jd_authored_sources() -> None:
    """S1: no résumé-side identifier may feed the rendered skill label."""
    expr = _skill_label_expression()
    sources = set(_DOTTED_REF.findall(expr))
    assert sources, f"no property reference found in the skill label: {expr!r}"
    assert sources <= _ALLOWED_SOURCES, (
        f"the rendered skill label reads from {sorted(sources - _ALLOWED_SOURCES)!r} "
        f"-- only {sorted(_ALLOWED_SOURCES)} (JD-authored) are permitted. "
        "Security finding S1: a résumé-side property (e.g. "
        "`has.evidence_chunk_id`, ADR-008 residual #3) must never be rendered "
        "as a skill name."
    )


def test_skill_label_does_not_read_the_resume_side_has_edge() -> None:
    """S1, stated as the concrete mutant security proved was unkilled."""
    expr = _skill_label_expression()
    assert "has." not in expr, (
        f"the skill label coalesce reads the HAS_SKILL edge: {expr!r} -- "
        "that edge is résumé-derived (security finding S1)"
    )
    assert ":Resume" not in expr


def test_skill_label_does_not_read_the_global_node_display_name() -> None:
    """S2: the node-level display_name is global/last-writer-wins, so reading
    it leaks another job's JD wording across the per-job authorization
    boundary. Only the per-job relationship property may supply wording."""
    expr = _skill_label_expression()
    assert "reqSkill.display_name" not in expr, (
        f"the skill label coalesce fell back to the GLOBAL node-level "
        f"display_name: {expr!r} -- security finding S2 (cross-job leak). "
        "A stale edge must render `reqSkill.canonical_key` instead."
    )
    assert "req.display_name" in expr, (
        "the per-job relationship display_name is gone from the chain -- "
        "the label would never render the JD's own wording again"
    )
    assert "reqSkill.canonical_key" in expr, (
        "the opaque canonical_key terminator is gone from the chain -- a "
        "stale edge would render NULL"
    )
