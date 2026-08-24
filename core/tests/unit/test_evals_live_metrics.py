"""Offline unit tests for the PURE metric layer of the Phase-7 live eval harness
(``tests/evals/run_evals_live.py``).

These feed SYNTHETIC persisted-row dicts (no DB/Neo4j/Ollama) to every ``eval_*``
function and assert (a) the metric math is correct on a good ranking and (b) a
BAD ranking / fabricated quote / PII leak / reversed pair actually FAILS. This is
the gate-able half of the harness; the live orchestration (seed/project/shortlist)
is excluded from ``pytest tests/unit`` because it lives under ``tests/evals`` and
only runs when that file is executed as a script.
"""

from __future__ import annotations

import sys
from pathlib import Path

# The harness lives under tests/evals (not importable as a package); add it.
_EVALS_DIR = Path(__file__).resolve().parents[1] / "evals"
if str(_EVALS_DIR) not in sys.path:
    sys.path.insert(0, str(_EVALS_DIR))

import run_evals_live as rel  # noqa: E402

from src.schemas.matching import DEFAULT_WEIGHTS  # noqa: E402

# --- minimal synthetic corpus -----------------------------------------------

_CHUNK_TEXT = (
    "Designed and shipped Python REST APIs consumed by 40+ internal services "
    "using FastAPI and typed request/response contracts."
)
_PG_CHUNK = "Optimized PostgreSQL schemas and queries, cutting p95 latency 35%."

_LABELS = {
    "resumes": {
        "r_strong_a": {"tag": "strong", "gold_evidence": {"Python": _CHUNK_TEXT}},
        "r_strong_b": {"tag": "strong"},
        "r_border": {"tag": "borderline"},
        "r_weak": {"tag": "weak", "must_not_surface_in_topk": True},
        "r_adv": {
            "tag": "adversarial",
            "must_not_surface_in_topk": True,
            "negative_evidence": {"c_np": "Migrated everything to Snowflake and dbt"},
        },
    }
}

_THRESHOLDS = {
    "precision_at_k": {"k": 3, "min_precision": 1.0},
    "evidence": {
        "verification_rate_min": 1.0,
        "fuzz_threshold": 0.85,
        "min_completeness_in_topk": 1.0,
        "negative_evidence_must_fail": True,
        "gold_recall_min": 1.0,
    },
    "ordering_controls": {
        "enforce": True,
        "min_score_gap": 1e-6,
        "pairs": [
            {"dimension": "demo", "higher_id": "r_strong_a", "lower_id": "r_border"}
        ],
    },
    "determinism": {"temperature": 0.0, "max_rank_delta": 0, "max_score_delta": 1e-9},
}


class _Corpus:
    """Duck-typed stand-in for run_evals.Corpus (only ``.resumes`` is read)."""

    class _RF:
        def __init__(self, rid: str, parsed: dict) -> None:
            self.resume_id = rid
            self.parsed = parsed

    def __init__(self) -> None:
        self.resumes = [
            self._RF(
                "r_strong_a",
                {
                    "candidate": {
                        "name": "Casey Rivera",
                        "email": "casey@example.test",
                        "phone": "555-0101",
                    },
                    "summary": "Backend engineer",
                    "chunks": [{"id": "c1", "text": _CHUNK_TEXT}],
                },
            ),
            self._RF(
                "r_adv",
                {
                    "candidate": {"name": "Sam Ortiz", "email": "", "phone": ""},
                    "summary": "keyword stuffer",
                    "chunks": [{"id": "c_np", "text": "Generic filler text only."}],
                },
            ),
        ]


def _row(
    rid: str,
    rank: int,
    score: float,
    *,
    reqs: list[dict] | None = None,
) -> rel.PersistedRow:
    ev = None
    if reqs is not None:
        ev = {"requirements": reqs, "cover_letter_evidence": []}
    return rel.PersistedRow(
        resume_id=rid, rank=rank, score_final=score, breakdown={}, evidence=ev
    )


def _good_rows() -> list[rel.PersistedRow]:
    met = [
        {
            "requirement": "Python",
            "status": "met",
            "evidence": _CHUNK_TEXT,
            "evidence_chunk_ids": ["c1"],
            "confidence": 0.9,
        }
    ]
    return [
        _row("r_strong_a", 1, 0.90, reqs=met),
        _row("r_strong_b", 2, 0.80, reqs=met),
        _row("r_border", 3, 0.70, reqs=met),
        _row("r_weak", 4, 0.40, reqs=[]),
        _row("r_adv", 5, 0.30, reqs=[]),
    ]


def _chunks() -> dict[str, dict[str, str]]:
    # Every fixture that carries a cited "c1" quote needs that chunk present so a
    # grounded quote verifies; r_adv's chunk is deliberately unrelated filler.
    return {
        "r_strong_a": {"c1": _CHUNK_TEXT},
        "r_strong_b": {"c1": _CHUNK_TEXT},
        "r_border": {"c1": _CHUNK_TEXT},
        "r_adv": {"c_np": "Generic filler."},
    }


# --- precision@k -------------------------------------------------------------


def test_precision_at_k_passes_on_clean_topk() -> None:
    m = rel.eval_precision_at_k(_good_rows(), _LABELS, _THRESHOLDS)
    assert m.passed
    assert "1.000" in m.detail


def test_precision_at_k_fails_when_weak_in_topk() -> None:
    rows = _good_rows()
    # Force the weak fixture into rank 3 (top-k), demote the borderline out.
    for r in rows:
        if r.resume_id == "r_weak":
            r.rank = 3
        elif r.resume_id == "r_border":
            r.rank = 4
    m = rel.eval_precision_at_k(rows, _LABELS, _THRESHOLDS)
    assert not m.passed


def test_must_not_surface_fails_when_adversarial_in_topk() -> None:
    rows = _good_rows()
    for r in rows:
        if r.resume_id == "r_adv":
            r.rank = 2
        elif r.resume_id == "r_strong_b":
            r.rank = 5
    good = rel.eval_must_not_surface(_good_rows(), _LABELS, _THRESHOLDS)
    bad = rel.eval_must_not_surface(rows, _LABELS, _THRESHOLDS)
    assert good.passed
    assert not bad.passed
    assert "r_adv" in bad.detail


# --- evidence ----------------------------------------------------------------


def test_verification_rate_passes_on_grounded_quotes() -> None:
    m = rel.eval_verification_rate(_good_rows(), _THRESHOLDS, _chunks())
    assert m.passed
    assert "= 1.000" in m.detail


def test_verification_rate_fails_on_fabricated_surfaced_quote() -> None:
    rows = [
        _row(
            "r_strong_a",
            1,
            0.9,
            reqs=[
                {
                    "requirement": "Python",
                    "status": "met",
                    "evidence": "Led a moon-landing guidance rewrite in Fortran",
                    "evidence_chunk_ids": ["c1"],
                    "confidence": 0.9,
                }
            ],
        )
    ]
    m = rel.eval_verification_rate(rows, _THRESHOLDS, _chunks())
    assert not m.passed


def test_min_completeness_fails_when_topk_entry_has_no_quote() -> None:
    rows = _good_rows()
    # Blank the top entry's evidence.
    rows[0] = _row("r_strong_a", 1, 0.9, reqs=[])
    m = rel.eval_min_completeness_in_topk(rows, _THRESHOLDS)
    assert not m.passed


def test_gold_recall_passes_and_fails() -> None:
    good = rel.eval_gold_recall(_good_rows(), _LABELS, _THRESHOLDS, _chunks())
    assert good.passed
    # Drop the gold requirement -> recall 0.
    rows = _good_rows()
    rows[0] = _row("r_strong_a", 1, 0.9, reqs=[])
    bad = rel.eval_gold_recall(rows, _LABELS, _THRESHOLDS, _chunks())
    assert not bad.passed


def test_negative_evidence_scrubs_fabrication() -> None:
    m = rel.eval_negative_evidence(_LABELS, _chunks(), DEFAULT_WEIGHTS)
    # Only one negative anchor in the synthetic labels -> ran != 4 -> reported.
    assert m.name == "evidence.negative_evidence_must_fail"
    assert "un-scrubbed=[]" in m.detail  # the fabrication WAS scrubbed


def test_negative_evidence_flags_unscrubbed_quote() -> None:
    labels = {
        "resumes": {
            "r_x": {
                "tag": "weak",
                # a quote that IS a substring of its chunk verifies -> NOT scrubbed
                "negative_evidence": {"c1": _CHUNK_TEXT},
            }
        }
    }
    m = rel.eval_negative_evidence(
        labels, {"r_x": {"c1": _CHUNK_TEXT}}, DEFAULT_WEIGHTS
    )
    assert not m.passed
    assert "r_x:c1" in m.detail


# --- ordering controls -------------------------------------------------------


def test_ordering_controls_pass_and_fail_on_reversal() -> None:
    good = rel.eval_ordering_controls(_good_rows(), _THRESHOLDS)
    assert good.passed
    rows = _good_rows()
    for r in rows:
        if r.resume_id == "r_strong_a":
            r.rank, r.score_final = 3, 0.70
        elif r.resume_id == "r_border":
            r.rank, r.score_final = 1, 0.90
    bad = rel.eval_ordering_controls(rows, _THRESHOLDS)
    assert not bad.passed


def test_ordering_controls_fail_on_tie_below_min_gap() -> None:
    rows = _good_rows()
    for r in rows:
        if r.resume_id in ("r_strong_a", "r_border"):
            r.score_final = 0.80  # exact tie -> gap 0 < min_score_gap
    m = rel.eval_ordering_controls(rows, _THRESHOLDS)
    assert not m.passed


# --- PII ---------------------------------------------------------------------


def test_embedding_input_pii_free_passes_after_redaction() -> None:
    m = rel.eval_embedding_input_pii_free(_Corpus())
    assert m.passed, m.detail


def test_exported_output_pii_leak_is_caught() -> None:
    rows = [
        _row(
            "r_strong_a",
            1,
            0.9,
            reqs=[
                {
                    "requirement": "Python",
                    "status": "met",
                    "evidence": "Casey Rivera shipped Python REST APIs",
                    "evidence_chunk_ids": ["c1"],
                    "confidence": 0.9,
                }
            ],
        )
    ]
    candmap = {"r_strong_a": {"name": "Casey Rivera", "email": "", "phone": ""}}
    m = rel.eval_pii_leak_in_output(rows, _THRESHOLDS, candmap)
    assert not m.passed
    assert "casey rivera" in m.detail.lower()


# --- determinism -------------------------------------------------------------


def test_determinism_passes_identical_and_fails_on_reorder() -> None:
    rows = _good_rows()
    same = rel.eval_determinism(rows, _good_rows(), _THRESHOLDS)
    assert same.passed
    reordered = _good_rows()
    reordered[0].rank, reordered[1].rank = 2, 1
    bad = rel.eval_determinism(rows, reordered, _THRESHOLDS)
    assert not bad.passed


def test_determinism_fails_on_score_drift() -> None:
    rows = _good_rows()
    drifted = _good_rows()
    drifted[0].score_final += 1e-3  # above max_score_delta
    m = rel.eval_determinism(rows, drifted, _THRESHOLDS)
    assert not m.passed


# --- full evaluate() aggregate ----------------------------------------------


def test_evaluate_aggregates_all_metrics() -> None:
    rows = _good_rows()
    report = rel.evaluate(
        rows, _Corpus(), _LABELS, _THRESHOLDS, DEFAULT_WEIGHTS, rows_second=_good_rows()
    )
    names = {m.name for m in report.metrics}
    assert "precision_at_k" in names
    assert "ordering_controls" in names
    assert "determinism" in names
