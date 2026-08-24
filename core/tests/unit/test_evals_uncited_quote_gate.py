"""The ranking-evals verification-rate gate must not EXCLUDE uncited quotes.

``tests/evals/run_evals.py``'s verification-rate loop reads::

    for req in s.evidence.requirements if s.evidence else []:
        if req.evidence and req.evidence_chunk_ids:
            assert any(_partial_ratio(...) >= verify_fuzz for cid in ...)

The ``and req.evidence_chunk_ids`` conjunct makes a SURFACED quote with no
citation structurally invisible to ``verification_rate_min = 1.0`` — the exact
quote shape ``verify_evidence``'s lenient arm lets through. "100% of surfaced
quotes verify" is therefore computed over a set that excludes precisely the
quotes that cannot verify: an inert corner of an otherwise potent gate.

This test is deliberately BEHAVIOURAL rather than a source-text assertion (this
repo's Phase-4a history is a long lesson in eval assertions that read as
rigorous and gated nothing). It injects a surfaced-but-uncited quote into one
resume's evidence and asserts the harness FAILS on it.

The injection is chosen to be SCORE-NEUTRAL by construction: it mutates an
already-``missing`` requirement in place (adding a quote, leaving status
``missing`` and confidence 0.0, appending nothing). ``_evidence_completeness``
counts ``met``-with-confidence and ``partial`` requirements over an unchanged
denominator, so no sub-score, no ``score_final`` and no rank moves — which means
the ONLY gate that can fire is the verification-rate loop, and the assertion
below matches on its message to prove that is the one that fired.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

_EVALS_DIR = Path(__file__).resolve().parents[1] / "evals"

_INJECTED_QUOTE = (
    "Personally rearchitected the global payments ledger for eleven regions"
)


def _import_run_evals() -> ModuleType:
    """Import ``tests/evals/run_evals.py`` by path (``tests/evals`` is a script
    directory, not a package). Mirrors ``test_evals_corpus._import_run_evals``:
    the module must be registered in ``sys.modules`` BEFORE ``exec_module``,
    because its ``@dataclass`` bodies resolve string annotations through it."""
    name = "run_evals_uncited_quote_gate"
    spec = importlib.util.spec_from_file_location(name, _EVALS_DIR / "run_evals.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _inject_uncited_quote(module: ModuleType) -> None:
    """Wrap ``_extract_evidence`` so ONE requirement across the whole corpus
    comes back with a quote and zero citations, leaving every score untouched."""
    real = module._extract_evidence
    done = {"injected": False}

    def _patched(parsed: dict[str, Any], jd: dict[str, Any]) -> Any:
        evidence, chunks_by_id = real(parsed, jd)
        if not done["injected"]:
            for i, req in enumerate(evidence.requirements):
                if req.status == "missing" and not req.evidence:
                    reqs = list(evidence.requirements)
                    reqs[i] = req.model_copy(
                        update={"evidence": _INJECTED_QUOTE, "evidence_chunk_ids": []}
                    )
                    evidence = evidence.model_copy(update={"requirements": reqs})
                    done["injected"] = True
                    break
        return evidence, chunks_by_id

    module._extract_evidence = _patched  # type: ignore[attr-defined]
    module._injection_done = done  # type: ignore[attr-defined]


def test_evals_harness_rejects_a_surfaced_quote_that_has_no_citation() -> None:
    module = _import_run_evals()
    corpus = module.load_corpus()
    thresholds = module.load_thresholds()

    _inject_uncited_quote(module)
    with pytest.raises(AssertionError) as excinfo:
        module._run_corpus(corpus, thresholds)

    assert module._injection_done["injected"], (
        "the injection never fired — no corpus resume had a quote-less "
        "'missing' requirement to mutate, so this test proved nothing"
    )
    message = str(excinfo.value)
    assert "verify" in message.lower() or "cit" in message.lower(), (
        "the harness failed, but not on the verification-rate gate (message: "
        f"{message!r}) — a score-neutral injection must not perturb any other "
        "threshold, so this is the wrong failure"
    )


def test_evals_harness_is_still_green_without_the_injection() -> None:
    """Control: the corpus passes every threshold unmodified, so the failure
    above is attributable to the injected uncited quote and nothing else."""
    module = _import_run_evals()
    module._run_corpus(module.load_corpus(), module.load_thresholds())
