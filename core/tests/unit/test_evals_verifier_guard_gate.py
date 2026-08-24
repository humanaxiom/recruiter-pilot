"""The ranking-evals corpus must FAIL when an ADR-022 verifier guard is removed.

``run_evals.py`` calls ``_assert_gold_anchor_text_survives_the_verifier`` and
``_assert_superset_quotes_are_scrubbed`` from exactly ONE line each inside
``_run_corpus``. Nothing in ``tests/unit`` referenced either name, so deleting
a call line was silently undetectable — the same shape ADR-022 already flagged
for ``test_evals_uncited_quote_gate.py`` ("delete that file and reverting the
harness line becomes silently undetectable"), recurring one round later.

This file pins both call sites BEHAVIOURALLY rather than by scraping source:
each test applies one of the mutations the ``ranking-evals`` gate runs against
this branch, and asserts ``_run_corpus`` RAISES. Delete a call line and the
corresponding test goes green-to-red, because nothing else in the harness can
see the mutation:

===========================================================================
mutation                              caught by            probe that bites
===========================================================================
min-quote floor removed               superset call site   c_001_bare_token
length guard removed                  superset call site   c_001_append_long
length guard loosened to k = 1.2      superset call site   c_001_append_short
empty-needle guard removed            superset call site   blank-needle probe
whitespace collapse disabled          anchor call site     column_padded_4
invisible scrub made needle-only      anchor call site     invisible haystack
invisible scrub removed entirely      anchor call site     invisible haystack
===========================================================================

The corpus's own 81 surfaced quotes cannot catch ANY of these: the stand-in
extractor quotes the WHOLE cited chunk, so every needle is byte-identical to
its haystack, the floor never binds, the length guard sits exactly at its
boundary and both normalisations are identities. That is what the probes fix
and what these tests keep fixed.

The mutants below are copies of ``_fuzz_ratio`` with ONE guard changed, patched
over ``stages._fuzz_ratio`` (``verify_evidence`` resolves it as a module
global). ``run_evals``'s own verification-rate loop uses its own private
``_partial_ratio``, so a mutant here cannot leak into that gate and the failure
is attributable.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

from src.pipeline.matching import stages
from src.schemas.matching import DEFAULT_WEIGHTS

_EVALS_DIR = Path(__file__).resolve().parents[1] / "evals"

_LOOSENED_K = 1.2

# Message fragments unique to each call site, so a test that passes for the
# WRONG reason (some other threshold tripping first) is caught.
_ANCHOR_SITE = "SCRUBBED by the verifier"
_SUPERSET_SITE = "was NOT scrubbed"
# Also reached from the superset call site — ``_assert_superset_quotes_are_
# scrubbed`` runs the blank-needle probe last, so deleting that call line takes
# this mutation's only detector with it too.
_BLANK_NEEDLE_SITE = "empty-needle guard is gone"


def _import_run_evals(name: str) -> ModuleType:
    """Import ``tests/evals/run_evals.py`` by path (``tests/evals`` is a script
    directory, not a package). Mirrors ``test_evals_uncited_quote_gate``: the
    module must be in ``sys.modules`` BEFORE ``exec_module``, because its
    ``@dataclass`` bodies resolve string annotations through it."""
    spec = importlib.util.spec_from_file_location(name, _EVALS_DIR / "run_evals.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _run(name: str) -> None:
    module = _import_run_evals(name)
    module._run_corpus(module.load_corpus(), module.load_thresholds())


def _identity(text: str) -> str:
    return text


def _fuzz_without_the_floor(
    needle: str,
    haystack: str,
    *,
    min_chars: int = DEFAULT_WEIGHTS.evidence_min_quote_chars,
) -> float:
    """``_fuzz_ratio`` with ADR-022 follow-up #4's minimum-quote-length floor
    deleted. A bare token the chunk genuinely contains then scores 1.000."""
    needle = stages._collapse_whitespace(stages._strip_control_chars(needle))
    haystack = stages._collapse_whitespace(stages._strip_control_chars(haystack))
    if not needle:
        return 0.0
    if len(needle) > len(haystack):
        return 0.0
    return float(stages.fuzz.partial_ratio(needle, haystack)) / 100.0


def _fuzz_without_the_length_guard(
    needle: str,
    haystack: str,
    *,
    min_chars: int = DEFAULT_WEIGHTS.evidence_min_quote_chars,
) -> float:
    """``_fuzz_ratio`` with ADR-022 follow-up #1's length guard deleted. A quote
    containing its whole cited chunk plus appended invention scores 1.000."""
    needle = stages._collapse_whitespace(stages._strip_control_chars(needle))
    haystack = stages._collapse_whitespace(stages._strip_control_chars(haystack))
    if not needle:
        return 0.0
    if len(needle) < min_chars:
        return 0.0
    return float(stages.fuzz.partial_ratio(needle, haystack)) / 100.0


def _fuzz_with_a_loosened_length_guard(
    needle: str,
    haystack: str,
    *,
    min_chars: int = DEFAULT_WEIGHTS.evidence_min_quote_chars,
) -> float:
    """``_fuzz_ratio`` with the STRUCTURAL length guard swapped for the tunable
    ratio the security finding originally proposed (k = 1.2). A one-character
    append lands at k = 1.0068 and rides straight through it."""
    needle = stages._collapse_whitespace(stages._strip_control_chars(needle))
    haystack = stages._collapse_whitespace(stages._strip_control_chars(haystack))
    if not needle:
        return 0.0
    if len(needle) < min_chars:
        return 0.0
    if len(needle) > len(haystack) * _LOOSENED_K:
        return 0.0
    return float(stages.fuzz.partial_ratio(needle, haystack)) / 100.0


def _fuzz_without_the_empty_needle_guard(
    needle: str,
    haystack: str,
    *,
    min_chars: int = DEFAULT_WEIGHTS.evidence_min_quote_chars,
) -> float:
    """``_fuzz_ratio`` with the empty-needle guard deleted.
    ``partial_ratio("", "")`` is 100, so a blank quote verifies against an
    empty chunk once the floor is configured away."""
    needle = stages._collapse_whitespace(stages._strip_control_chars(needle))
    haystack = stages._collapse_whitespace(stages._strip_control_chars(haystack))
    if len(needle) < min_chars:
        return 0.0
    if len(needle) > len(haystack):
        return 0.0
    return float(stages.fuzz.partial_ratio(needle, haystack)) / 100.0


def _fuzz_with_a_needle_only_control_scrub(
    needle: str,
    haystack: str,
    *,
    min_chars: int = DEFAULT_WEIGHTS.evidence_min_quote_chars,
) -> float:
    """``_fuzz_ratio`` as it stood at ``fbdd112``: the invisible/bidi scrub
    applied to the NEEDLE only. A chunk carrying SOFT HYPHENs then scores 0.838
    against its own clean text — real evidence rejected as fabrication."""
    needle = stages._collapse_whitespace(stages._strip_control_chars(needle))
    haystack = stages._collapse_whitespace(haystack)
    if not needle:
        return 0.0
    if len(needle) < min_chars:
        return 0.0
    if len(needle) > len(haystack):
        return 0.0
    return float(stages.fuzz.partial_ratio(needle, haystack)) / 100.0


@pytest.mark.parametrize(
    "mutant, site",
    [
        pytest.param(_fuzz_without_the_floor, _SUPERSET_SITE, id="floor-removed"),
        pytest.param(
            _fuzz_without_the_length_guard, _SUPERSET_SITE, id="length-guard-removed"
        ),
        pytest.param(
            _fuzz_with_a_loosened_length_guard,
            _SUPERSET_SITE,
            id="length-guard-k-1.2",
        ),
        pytest.param(
            _fuzz_without_the_empty_needle_guard,
            _BLANK_NEEDLE_SITE,
            id="empty-needle-guard-removed",
        ),
        pytest.param(
            _fuzz_with_a_needle_only_control_scrub,
            _ANCHOR_SITE,
            id="control-scrub-needle-only",
        ),
    ],
)
def test_corpus_fails_when_a_fuzz_ratio_guard_is_mutated(
    monkeypatch: pytest.MonkeyPatch, mutant: object, site: str
) -> None:
    monkeypatch.setattr(stages, "_fuzz_ratio", mutant)
    with pytest.raises(AssertionError) as excinfo:
        _run(f"run_evals_guard_{id(mutant)}")
    assert site in str(excinfo.value), (
        "the corpus failed, but not at the call site this mutation is supposed "
        f"to reach (wanted {site!r}, got {str(excinfo.value)[:400]!r})"
    )


def test_corpus_fails_when_the_whitespace_collapse_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_collapse_whitespace`` -> ``return text``. Only the re-rendered gold
    anchors can see this: every surfaced corpus quote is byte-identical to its
    chunk, so collapsing it is an identity."""
    monkeypatch.setattr(stages, "_collapse_whitespace", _identity)
    with pytest.raises(AssertionError) as excinfo:
        _run("run_evals_guard_collapse")
    assert _ANCHOR_SITE in str(excinfo.value), str(excinfo.value)[:400]


def test_corpus_fails_when_the_invisible_character_scrub_is_removed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_strip_control_chars`` -> ``return value`` inside ``_fuzz_ratio``.
    Only the soft-hyphenated HAYSTACK probe can see this."""
    monkeypatch.setattr(stages, "_strip_control_chars", _identity)
    with pytest.raises(AssertionError) as excinfo:
        _run("run_evals_guard_scrub")
    assert _ANCHOR_SITE in str(excinfo.value), str(excinfo.value)[:400]


def test_the_unmutated_corpus_is_green() -> None:
    """Control. Every failure above is attributable to its own mutation and to
    nothing this file does to the harness."""
    _run("run_evals_guard_control")
