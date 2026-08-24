"""Model acceptance — measure a model against THIS product's real prompts.

**Why this exists.** Model-specific behaviour has been encoded in this codebase
as constants, each measured once, against one model, in a different file, with
nothing that re-measures when the model changes:

===========================  =========================  ========================
value                        lives in                   was measured against
===========================  =========================  ========================
``REASONING_JSON_MIN_TOKENS`` ``pipeline/llm/client.py``  gpt-oss:20b, ONE prompt
``~23.5 tok/s``               a comment in ``.env.example`` gpt-oss:20b, when the
                                                         budget was 3072
``LLM_TIMEOUT_S``             ``.env``                   set independently
``max_jobs``                  ``worker/main.py``         never related to these
``llm_ollama_native``         ``settings.py``            never measured at all
===========================  =========================  ========================

Point the stack at a larger model — which the data-centre move will do — and
every one of those becomes wrong at the same moment, with no signal. On
2026-08-21 exactly that happened within a single model: raising one number
turned "returns nothing" into "times out", and it took a production incident to
notice. This module turns that discovery process into a measurement.

**The design constraint, learned the hard way.** While diagnosing that incident
a single uncontended call to the real prompt returned valid JSON in ~35 seconds
on BOTH transports at BOTH budgets — it could not reproduce the failure at all,
while production failed 3 for 3. The difference was **real extracted résumé text
and four jobs sharing one GPU**. So:

* the harness drives the REAL prompt templates, not a synthetic stand-in;
* it measures at the CONCURRENCY the worker actually uses, because per-call
  latency under contention is the number the timeout must cover;
* and it reports what it could not establish, rather than implying coverage it
  does not have.

A probe that cannot reproduce a known failure is not evidence of health, and
this module must never present it as such.

**What it produces** is a profile (see :class:`ModelProfile`) recording, per
prompt: whether the model emits schema-valid JSON, the budget needed, the
latency observed under contention, and whether the endpoint supports
schema-constrained decoding. ``recommended_timeout_s`` is DERIVED from those
measurements rather than declared, which is the specific coupling that broke.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

#: Multiplier applied to the slowest observed call when deriving a timeout.
#: Not conservatism for its own sake: a timeout that trips converts a slow
#: answer into a retry storm and an open circuit breaker, which is how parsing
#: stopped entirely on 2026-08-21. Being late costs latency; being early costs
#: the feature.
_TIMEOUT_SAFETY = 2.0

#: Below this, a timeout is too tight to survive ordinary variance regardless of
#: what a quiet probe measured.
_MIN_TIMEOUT_S = 120


@dataclass(frozen=True)
class PromptResult:
    """One prompt's measured behaviour against one model."""

    prompt: str
    schema_valid: bool
    min_tokens_needed: int | None
    latency_s: float
    concurrency: int
    thinking_chars: int
    structured_output: bool
    note: str = ""


@dataclass
class ModelProfile:
    """Everything the product needs to know about a model it has not met.

    Committed to ``docs/model-profiles/<model>.json`` so a swap is reviewable in
    a diff: what changed about the model is visible next to what changed about
    the config, instead of being discovered in production a fortnight later.
    """

    model: str
    endpoint: str
    transport: str
    max_jobs: int
    results: list[PromptResult] = field(default_factory=list)

    @property
    def accepted(self) -> bool:
        """Every prompt produced schema-valid JSON. Anything less is a model
        this product cannot run on, however good it is at other things."""
        return bool(self.results) and all(r.schema_valid for r in self.results)

    @property
    def recommended_max_tokens(self) -> int:
        """The largest budget any prompt needed — one floor, not per-call
        literals, because per-call literals are what drifted."""
        needed = [r.min_tokens_needed for r in self.results if r.min_tokens_needed]
        return max(needed) if needed else 0

    @property
    def recommended_timeout_s(self) -> int:
        """DERIVED from the slowest measured call, never declared.

        ``LLM_TIMEOUT_S`` and the token budget were set in different files by
        different people and are not independent — that is precisely the
        coupling that broke. Deriving it means the next person to raise a budget
        cannot forget to raise this too, because they do not set it at all.
        """
        slowest = max((r.latency_s for r in self.results), default=0.0)
        return max(_MIN_TIMEOUT_S, int(slowest * _TIMEOUT_SAFETY))

    def to_json(self) -> str:
        payload: dict[str, Any] = {
            "model": self.model,
            "endpoint": self.endpoint,
            "transport": self.transport,
            "max_jobs": self.max_jobs,
            "accepted": self.accepted,
            "recommended_max_tokens": self.recommended_max_tokens,
            "recommended_timeout_s": self.recommended_timeout_s,
            "results": [asdict(r) for r in self.results],
        }
        return json.dumps(payload, indent=2, sort_keys=True) + "\n"

    @staticmethod
    def from_json(text: str) -> ModelProfile:
        raw = json.loads(text)
        profile = ModelProfile(
            model=raw["model"],
            endpoint=raw.get("endpoint", ""),
            transport=raw.get("transport", ""),
            max_jobs=int(raw.get("max_jobs", 1)),
        )
        profile.results = [PromptResult(**r) for r in raw.get("results", [])]
        return profile


def render(profile: ModelProfile) -> str:
    """A report an operator can act on, including what was NOT established."""
    lines = [
        f"model      : {profile.model}",
        f"endpoint   : {profile.endpoint}  (transport: {profile.transport})",
        f"concurrency: {profile.max_jobs}",
        "",
    ]
    for r in profile.results:
        mark = "ok  " if r.schema_valid else "FAIL"
        lines.append(
            f"  {mark} {r.prompt:<24} {r.latency_s:6.1f}s  "
            f"tokens>={r.min_tokens_needed or '?'}  thinking={r.thinking_chars}  "
            f"structured={'yes' if r.structured_output else 'no'}"
            + (f"  — {r.note}" if r.note else "")
        )
    lines += [
        "",
        f"accepted              : {profile.accepted}",
        f"recommended max_tokens: {profile.recommended_max_tokens}",
        f"recommended timeout   : {profile.recommended_timeout_s}s "
        f"(slowest call x{_TIMEOUT_SAFETY:g}, floor {_MIN_TIMEOUT_S}s)",
    ]
    if not profile.accepted:
        lines += [
            "",
            "This model is NOT accepted: at least one real prompt failed to "
            "produce schema-valid JSON. Do not point the stack at it.",
        ]
    return "\n".join(lines)
