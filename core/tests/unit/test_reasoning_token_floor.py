"""RED pin — every JSON extraction call must clear the reasoning-model floor.

**The incident.** On 2026-08-21 the first real smoke run uploaded three résumés
and *none* of them got LLM skill extraction: two fell back to the keyword scan
(``degraded``) and one failed outright, with
``parse_resume.skills_llm_failed ... response content was empty (possibly
reasoning model exhausted token budget); reasoning_present=True``. Degraded
résumés are excluded from shortlisting (ADR-030), so the shortlist came back
empty — **the ranking pipeline could not rank anybody.**

The cause was a number. ``resume_skills_v2`` was called with
``max_tokens=1536``. On ``gpt-oss:20b`` the DISCARDED reasoning trace counts
against ``max_tokens`` before a single byte of JSON is emitted (ADR-021 §6), so
the budget was gone before the answer started.

**This was already known and already written down.** ADR-044 / PR #94 hit the
identical failure on the skill classifier, proved live that 1024 classified 0 of
6 skills while 4096 classified 6 of 6, and left a comment saying "do not
optimise this back toward 1024 — that value was proven live to zero out the
feature". The lesson was recorded against ONE call site while four others kept
their own smaller literals, and the most important of them — the extraction the
entire product depends on — was one of those four.

That is this repo's signature defect (ROADMAP A7): a true, hard-won invariant
living in a comment, with nothing enforcing it anywhere else. So this file
enforces it structurally rather than trusting the next author to have read
ADR-044.

**Why a source scan and not a call assertion.** The failure mode is a NEW call
site added later with a hand-picked literal, which no per-call test would cover
because nobody writes a test for the call they forgot to think about. The scan
sees every call, including ones that do not exist yet.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from src.pipeline.llm.client import REASONING_JSON_MIN_TOKENS

_SRC = Path(__file__).resolve().parents[2] / "src"
_PROFILE_DIR = Path(__file__).resolve().parents[3] / "docs" / "model-profiles"

#: The extraction paths whose output the product cannot work without. A short
#: response is not the point — the reasoning trace is charged first regardless
#: of how small the answer is.
_MUST_CLEAR_FLOOR = (
    "worker/resume_tasks.py",
    "worker/tasks.py",
    "pipeline/skill_classifier.py",
)

#: Below-floor call sites that a MEASUREMENT says are safe, mapped to the exact
#: budget that was measured. A path alone is not enough: the previous version of
#: this was a bare set, which permitted ANY value below the floor at that path —
#: someone could have dropped the tiebreaker to 8 tokens and this file would
#: still have gone green. The recorded decision now has to match what was
#: actually measured, or the test fails.
#:
#: `pipeline/skills_graph.py` — the vocabulary tiebreaker, measured 2026-08-22 on
#: an IDLE aria-gb10 (`/api/ps` empty) against `gpt-oss:20b`, through
#: `LLMClient.chat_json` on the OpenAI-compat transport the app actually runs
#: (`llm_ollama_native=False`), NOT the native schema-constrained path
#: `scripts/model-check.sh` probes:
#:
#:     candidate "postgresql" vs {postgres, mysql, sql}
#:       max_tokens=128  -> "postgres" 3/3, 4.3-4.8s
#:       max_tokens=8192 -> "postgres" 3/3, 4.1-4.6s
#:     candidate "react native" vs {react, react router, javascript}
#:       max_tokens=128  -> null 4/4 at concurrency 4, 5.7-6.4s
#:       max_tokens=8192 -> null 4/4 at concurrency 4, 5.6-6.3s
#:
#: 128 returns schema-valid JSON on every call at the worker's own concurrency
#: and gives the SAME answers as 8192, including matching when a match exists.
#: The 8192 floor is a property of the PROMPT, not of the model: résumé
#: extraction feeds thousands of input tokens and burns ~15k chars of reasoning
#: trace before emitting anything, while this prompt is three lines. The
#: committed profile already records that per-prompt (4096 / 8192 / 4096).
#:
#: The reason recorded here previously — "raising it changes canonical-key
#: resolution, which is a scoring-path change" — was STALE. Per F1 the
#: tiebreaker is enrichment-only: it may add an alias to a near-matched node and
#: may never change the key `_resolve_one` returns. It cannot move a score.
_RECORDED_EXCEPTIONS = {"pipeline/skills_graph.py": 128}


def _call_sites() -> list[tuple[str, int, int]]:
    """Every literal ``max_tokens=<int>`` under ``src/``, as (path, line, n)."""
    found: list[tuple[str, int, int]] = []
    for path in sorted(_SRC.rglob("*.py")):
        rel = path.relative_to(_SRC).as_posix()
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            match = re.search(r"\bmax_tokens\s*=\s*(\d+)\b", line)
            if match:
                found.append((rel, lineno, int(match.group(1))))
    return found


def test_the_floor_covers_every_accepted_model_profile() -> None:
    """**This assertion used to read ``== 4096``, and that was the bug.**

    4096 came from ADR-044, measured against ONE prompt (the classifier). On
    2026-08-22 `scripts/model-check.sh` probed every real prompt on an idle peer
    and found `resume_skills_v2` — the extraction the whole product depends on —
    managed only 2 of 4 concurrent calls at 4096, and 4 of 4 at 8192. A constant
    pinned to a number measured elsewhere was still too low for the call that
    mattered, and a test asserting that exact number made it *harder* to
    correct, not easier.

    So the floor is now tied to the MEASUREMENTS rather than to a literal: it
    must cover the largest ``recommended_max_tokens`` of every accepted profile
    in ``docs/model-profiles/``. Point the stack at a hungrier model, run
    `model-check.sh`, commit its profile, and this fails until the floor is
    raised to match — which is the coupling ADR-045 exists to enforce.
    """
    profiles = sorted(_PROFILE_DIR.glob("*.json"))
    assert profiles, (
        "no committed model profiles — run scripts/model-check.sh and commit "
        "what it writes; the floor is meaningless without a measurement"
    )
    for path in profiles:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not data.get("accepted"):
            continue
        needed = int(data.get("recommended_max_tokens") or 0)
        assert REASONING_JSON_MIN_TOKENS >= needed, (
            f"{path.name} measured a need for {needed} tokens but the shared "
            f"floor is {REASONING_JSON_MIN_TOKENS}. Raise the floor: a budget "
            "below the measured requirement does not truncate the answer, it "
            "returns an empty one."
        )


@pytest.mark.parametrize("module", _MUST_CLEAR_FLOOR)
def test_no_extraction_call_sits_below_the_reasoning_floor(module: str) -> None:
    below = [
        (path, line, n)
        for path, line, n in _call_sites()
        if path == module and n < REASONING_JSON_MIN_TOKENS
    ]
    assert not below, (
        f"{module} has a max_tokens literal below the proven floor of "
        f"{REASONING_JSON_MIN_TOKENS}: {below}. On gpt-oss:20b the discarded "
        "reasoning trace is charged against this budget BEFORE any JSON is "
        "emitted, so a smaller value does not truncate the answer — it returns "
        "an empty one. This exact number emptied every shortlist on 2026-08-21."
    )


def test_every_below_floor_call_site_is_a_recorded_decision() -> None:
    """The scan must not quietly grow exceptions. Anything below the floor is
    either fixed or listed above with a reason — never merely present."""
    offenders = {
        path
        for path, _line, n in _call_sites()
        if n < REASONING_JSON_MIN_TOKENS and path not in _RECORDED_EXCEPTIONS
    }
    # `client.py`'s own default is the signature default, not a call site.
    offenders.discard("pipeline/llm/client.py")
    assert not offenders, (
        f"new below-floor LLM call sites appeared: {sorted(offenders)}. Either "
        "raise them to REASONING_JSON_MIN_TOKENS or record why they are safe."
    )


def test_a_recorded_exception_may_not_drift_below_what_was_measured() -> None:
    """An exception is only as good as the measurement behind it.

    The previous ``_RECORDED_EXCEPTIONS`` was a bare set of paths, so it
    excused the *file* rather than the *value*. Any literal below the floor at
    that path passed — 128, or 8. That is the same shape as the defect this
    whole file exists to prevent: a real, hard-won number living somewhere with
    nothing enforcing it. The budget recorded above was measured against the
    live model on the transport the app actually uses; this pins the source to
    it, so lowering the call site is a RED test rather than a silent
    degradation with a stale comment still vouching for it.

    Raising it is equally a failure, and deliberately so — a value above what
    was measured is no longer the recorded decision either, and 8192 here would
    be a 64x budget increase that the measurement says changes no answer.
    """
    for path, measured in _RECORDED_EXCEPTIONS.items():
        found = [(line, n) for p, line, n in _call_sites() if p == path]
        assert found, (
            f"{path} is listed as a recorded below-floor exception but has no "
            "max_tokens call site at all. Delete the entry: an exception with "
            "nothing to excuse is stale documentation that will mislead the "
            "next reader."
        )
        wrong = [(line, n) for line, n in found if n != measured]
        assert not wrong, (
            f"{path} has max_tokens values {wrong} but the recorded, MEASURED "
            f"budget is {measured}. Either restore it, or re-measure against "
            "the live model on the app's own transport and update both the "
            "number and the evidence block above — never just the number."
        )
