"""The 4-stage hybrid matching pipeline (Phase 4c).

* ``stages`` — pure, DB/LLM-free scoring helpers (sub-score arithmetic, the
  anti-fabrication ``verify_evidence`` check, the final combine).
* ``orchestrator`` — the DB/Neo4j/LLM-driven stages 1-4 + reverse match, plus
  the ``run_match`` in-memory ranker the eval harness wires.

Ported from hris ``packages/pipeline/src/pipeline/matching`` with the recorded
Phase 4c blocker fixes (see ``docs/EXTRACTION_PLAN.md`` and the RED test
docstrings). Kept free of any review-workflow / JD-Harmonizer dependency.
"""

from __future__ import annotations
