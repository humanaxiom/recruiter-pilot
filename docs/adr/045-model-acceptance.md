# ADR-045: The model is a dependency with an acceptance test

**Status:** Accepted
**Date:** 2026-08-21
**Supersedes in practice:** the scattered per-call token budgets ADR-021 §6 and
[ADR-044](044-skill-family-classifier.md) each fixed one instance of.

## Context

### The same defect, four times, escalating

Reasoning-model behaviour has now caused four separate incidents, and each fix
addressed one call site while leaving the others exactly as they were:

1. **ADR-021 §6** — established that on a reasoning model the discarded thinking
   trace is charged against `max_tokens` before any JSON is emitted.
2. **ADR-044 / PR #94** — the skill classifier returned empty content at 1024.
   Probed live: 1024 classified 0 of 6 skills, 4096 classified 6 of 6. Fixed at
   that one call site, with a comment saying not to reduce it.
3. **ROADMAP A7 (21), 2026-08-21** — `resume_skills_v2` was still at 1536. Every
   résumé uploaded degraded or failed, and **no shortlist could be produced at
   all**. The lesson from (2) had been recorded in a comment next to one
   constant while four others kept hand-picked literals: 1536, 1024, 2048, 3072,
   128.
4. **Same day** — raising that one number to 4096 turned "returns nothing" into
   "exceeds `LLM_TIMEOUT_S`". Every call timed out at 366s, the circuit breaker
   opened, and parsing stopped entirely. The budget lived in
   `worker/resume_tasks.py`; the timeout lived in `.env`; nobody owned the
   relationship between them.

### The real problem is not reasoning models

It is that **model behaviour is encoded as constants, each measured once, and
nothing re-measures them when the model changes**:

| value | lives in | measured against |
|---|---|---|
| `REASONING_JSON_MIN_TOKENS` | `pipeline/llm/client.py` | gpt-oss:20b, one prompt |
| `~23.5 tok/s` | a comment in `.env.example` | gpt-oss:20b, when the budget was 3072 |
| `LLM_TIMEOUT_S` | `.env` | set independently of both |
| `max_jobs` | `worker/main.py` | never related to any of them |
| `llm_ollama_native` | `settings.py` | never measured at all |

The data-centre move will swap `gpt-oss:20b` for something larger. At that
moment **all five become wrong simultaneously, with no signal.** Incident (4)
shows this does not even require a new model — it happened within one.

`llm_ollama_native` deserves its own mention: it is `False`, above a comment
reading *"flip this on if JSON-mode parses come back empty."* The remedy for the
exact symptom of incident (3) was implemented, documented, and defaulted to the
broken setting. Nobody flipped it, and a live probe suggests flipping it blindly
would have traded empty responses for ~6× latency. Both the default and the
advice were guesses.

## Decision

**Treat the model as a dependency with an acceptance test.**
`scripts/model-check.sh` builds this product's REAL prompts from real fixture
documents, runs each at the worker's own concurrency, finds the smallest token
budget that yields schema-valid JSON for every concurrent call, and writes
`docs/model-profiles/<model>.json`.

`scripts/doctor.sh` then fails when the configured model has no profile, when
the profile says the model was not accepted, or when `LLM_TIMEOUT_S` is below
the measured latency. Swapping a model becomes: point, measure, commit the
profile, deploy.

### Three properties, each earned during the incidents above

**1. Real inputs, never a stand-in.** While diagnosing incident (3), a
hand-written prompt of comparable length returned valid JSON on *both*
transports at *both* budgets — while the real extracted résumé failed three
times out of three. A harness built on a synthetic prompt would have certified
the broken configuration as healthy. This is the single most important
constraint in the design and the least obvious.

**2. Real concurrency.** A single uncontended call to the failing prompt took
~35s. That is the number that made `LLM_TIMEOUT_S=300` look generous for calls
that were exceeding 300s, because four jobs share one GPU. A latency measured
one call at a time is accurate and useless.

**3. Derived, not declared.** `recommended_timeout_s` is computed from the
slowest measured call (×2, floor 120s). Nobody sets it by hand, so nobody can
raise a budget and forget the timeout — which is exactly incident (4).

### Schema-constrained decoding

Every probe uses Ollama's native `format: <json schema>`. A model that cannot
emit tokens outside the schema cannot ramble through its budget and return
nothing — it removes the failure mode at the source rather than budgeting around
it, and it gets *more* reliable on newer models, not less. Adopting it on the
hot path is the profile's next use; this ADR does not claim it yet.

## Consequences

- A model swap has a defined, cheap procedure (~10 minutes) instead of being
  discovered through production over days.
- An unmeasured model is a loud doctor failure rather than a silent risk.
- The per-call token literals become one profile-derived floor. The remaining
  below-floor call site (`skills_graph`'s tiebreaker, `max_tokens=128`) is
  listed in the enforcing test's `_RECORDED_EXCEPTIONS`, so it is a decision
  with a reason rather than an oversight.

### Accepted residuals

- **The harness measures three prompts** (`resume_core_v1`, `resume_skills_v2`,
  `jd_extract_v1`), not `cover_letter_v1` or `shortlist_evidence_v1`. Those are
  the three on the critical path to a shortlist; the others should be added when
  something depends on them.
- **It measures one résumé and one JD.** A pathological document could still
  behave differently — this bounds the risk, it does not eliminate it.
- **It does not compare transports.** `llm_ollama_native` remains unmeasured
  and defaulted to `False`; the harness uses the native path for probing, so a
  profile does not yet certify the transport the application actually uses.
  This is the most important gap and the obvious next slice.
- **Nothing forces the profile to be re-run when a prompt changes.** Editing a
  template can invalidate a profile as surely as swapping a model, and no check
  currently notices.
