# ADR-021: LLM failover and fail-closed ranking (FU-7)

**Status:** Accepted (addresses production incident 2026-07-19/20 where 16 résumés hung at status `uploaded` for ~18 hours; closes FU-7 scope item)  
**Date:** 2026-07-20

## Context

A batch of 16 résumés was stuck at status `uploaded` for approximately 18 hours on 2026-07-19/20, all due to LLM timeouts. Investigation revealed two distinct failure modes, each with separate error handling gaps:

- **Mode A — Availability failure (timeout):** `LLMUnavailableError` is raised when the LLM provider does not respond within `timeout_s`. This is the primary symptom in the incident: 16 résumés hung at `uploaded` status.
- **Mode B — Output invalidity (malformed response):** `LLMOutputInvalidError` is raised when the LLM returns a response that fails schema validation (e.g., empty `content` field). This caused silent 40% degradation in matching scores: 10 résumés had no skills extracted but were marked `status='parsed'`, and later ranked with zeroed evidence/motivation components.

Both are unrelated siblings inheriting from `RuntimeError` (defined at `core/src/pipeline/llm/client.py:59` and `:63`), and both require distinct handling. The five gaps below are grouped by root cause:

### 1. Single-provider fragility with no timeout headroom

The configured Ollama peer (`gpt-oss:20b`) generates at **23.5 tokens/second** (measured 1338 completion tokens in 56.8 seconds from the worker container on 2026-07-19/20). Parse calls use `max_tokens=3072` (line 613 in `core/src/worker/resume_tasks.py`). A core extraction running to ceiling requires ~131 seconds. The timeout is set at 120 seconds as a pydantic-settings default (configured in `core/src/settings.py:40`, also referenced in `docker-compose.yml:62`, `.env.example:27`, `.env:12`, and `compose.live-eval.yml:11` with a 300-second override for live evals). Measured parses on this hardware took 150–205 seconds, exceeding the timeout deterministically. The rates and parse durations are hardware- and model-specific; they must be re-measured on any other deployment and are not portable constants. Short generations succeeded, disguising systematic underprovisioning as flaky infrastructure.

**Note:** The 23.5 tok/s rate and 150–205 second parse measurements are evidence from operator observation (worker container logs and direct timing probes on 2026-07-19/20) against the configured peer, not from a checked-in benchmark or portable constant.

### 2. Timeout swallows the row — no failure recorded (`LLMUnavailableError`)

When `parse_resume` raises `LLMUnavailableError` (from timeout), the exception is deliberately NOT caught in the function body. A code comment at lines 690–692 in `core/src/worker/resume_tasks.py` explains this as intentional — arq's retry mechanism is expected to handle it. Note that `parse_resume` catches and handles other failures at six sites (lines 577, 586, 595, 616, 675, 732), but this specific exception is left uncaught to propagate. When arq exhausts its `max_tries` and stops retrying, nothing calls `record_parse_failure` — the row never transitions to status `'failed'` and stays `'uploaded'` forever, indistinguishable from a job that was never enqueued. (The `'parsing'` status enum value is unreachable because no code path writes it; see section 3 below.)

### 3. The `parsing` status enum is unreachable in code

The Postgres enum includes the value `'parsing'` (lines 52–54 in `core/src/models/ddl.py`), and `_PARSEABLE_STATUSES` in `resume_tasks.py:99` allows parsing from it — but there is nowhere in the codebase that WRITES status `'parsing'` when a job starts. A claim-UPDATE to mark a row under-way never happens. The row simultaneously means "never enqueued", "running right now", and "permanently dead". The enum value exists but is unreachable.

### 4. Reasoning model empty-`content` bug produces silent parse degradation (`LLMOutputInvalidError`)

`gpt-oss:20b` is a reasoning model that returns its chain-of-thought in a separate `reasoning` field. Even with `"think": False` set, it can exhaust `max_tokens` filling the reasoning buffer before emitting any `content`, producing an empty string that fails JSON parsing. From operator observation on 2026-07-19/20 (worker container logs and direct inspection), **10 of 16 résumés logged `parse_resume.skills_llm_failed`** with error message `Expecting value: line 1 column 1 (char 0)` — the error JSON strings produce when empty. These measurements are hardware- and model-specific. These résumés were still marked `status='parsed'`, their skills came only from deterministic vocabulary scan, and the degraded parse was **indistinguishable from a good one** in the UI and persisted data.

### 5. Matching rank collapses on LLM failure with no visibility (`LLMOutputInvalidError`)

In the MATCH path, stage 3 (lines 506–512 in `core/src/pipeline/matching/orchestrator.py`) catches `LLMOutputInvalidError` (output-invalidity failures, Mode B) and returns `None`. This is the distinct path that does NOT catch `LLMUnavailableError` (availability failures, Mode A), which propagates and fails the job loudly. A `None` evidence object flows through to stage 4, where it is treated as zero-score on 40% of the final composite (evidence 0.3 + motivation 0.1). The candidate keeps their structured score but **silently scores 0.0 on evidence and motivation**, a technical failure indistinguishable from a genuinely weak candidate. Only a `log.warning` is emitted; there is no signal to block or quarantine the shortlist.

### 6. Circuit breaker comment does not match code

At `client.py:450–451`, a code comment claims a failing half-open trial "will re-open immediately". In reality, `_on_failure` increments `_consecutive_failures` (line 456), which was just reset to 0 at line 453. The counter must accumulate `_breaker_threshold` (10) more failures before re-opening — one failure does not re-open immediately. The comment is misleading.

---

## Decision

### 1. Provider chain with per-provider circuit breakers (addresses Mode A — availability)

`LLMClient` will accept an **ordered list of providers** instead of a single endpoint. Each provider specifies:
- `base_url` (OpenAI-compatible endpoint)
- `model_generation` (the model name)
- `timeout_s` (provider-specific timeout, e.g. a faster peer can use a lower bound)

Retries against provider A use a per-provider circuit breaker. On exhausting retries against provider A (either breaker open OR all attempts failed), failover attempts provider B with its own timeout and circuit breaker. The same pattern continues through the list.

**Failover scope:** Failover is triggered only by availability errors (timeout, connection error, 5xx, 429). **Never** fail over on 4xx or schema-validation errors, since these will fail identically on any provider — a validation error should not waste time trying other endpoints.

**Traceability:** The result object records which provider generated it (e.g. "primary" vs "fallback"). A score is traceable to the model and endpoint that produced it — this is critical for debugging ranking anomalies.

### 2. Fail-closed ranking (addresses Mode B — output invalidity)

When an LLM call produces invalid output (Mode B: `LLMOutputInvalidError`) during the MATCH phase, the pipeline must **NOT emit a shortlist containing silently-zeroed components**. Introduce a job-level state such as `awaiting_llm` (or similar) that:
- Blocks shortlist production (the persistent `shortlist_entries` table is not written)
- Is surfaced to the UI as a user-facing status (e.g. "Waiting for AI to rank candidates…")
- Is retried until a provider recovers

**Rationale:** A degraded ranking that reaches human eyes is worse than no ranking. A recruiter seeing a candidate scored 0.5 expects it means something about the candidate; they do not expect it to mean "the LLM timed out." A transparent "ranking is unavailable" is better than a silent zero-score penalty for a technical reason.

This replaces the prior behaviour of catching `LLMOutputInvalidError` and silently returning `None`, which resulted in a broken-down candidate staying in the shortlist with degraded scores.

> **2026-08-02 note:** Implemented — see [ADR-029](029-fail-closed-ranking-fu7.md). Scope was widened by a
> human decision beyond this section's literal text: ADR-029 fails closed on **both** Mode B
> (`LLMOutputInvalidError`, as scoped here) **and** Mode A (`LLMUnavailableError` — timeout/connection/5xx/429),
> not Mode B alone. The `awaiting_llm` job state is a dedicated nullable column trio on `jobs`
> (`shortlist_state`/`_reason`/`_at`), not a new `job_status` enum value — see ADR-029 for why.

### 3. Honest résumé parse status

Implement the following state machine:
- `uploaded` → `parsing` transition: When `parse_resume` starts (e.g. immediately inside arq's task dispatch), claim the row with `UPDATE resumes SET status='parsing' WHERE id=$1 AND status='uploaded'`. This makes the unreachable `'parsing'` enum value real and distinguishes "never enqueued" from "running right now".
- `parsing` → `failed` transition: When arq exhausts `max_tries` (detected via `ctx["job_try"] >= ctx["max_tries"] - 1`), call `record_parse_failure` with a reason such as "timeout after N retries". The `failure_reason` column (already exists in `core/src/models/ddl.py` and is written by `resume_service.record_parse_failure`) will be populated.

A row that timeouts now moves to `failed` instead of staying `uploaded`, making it observable and excluding it from future shortlist attempts.

> **2026-07-30 note:** Implemented — see [ADR-027](027-honest-resume-parse-status-fu7.md). Also corrects
> this section's premise about arq: `ctx` has no `max_tries` key, and a plain uncaught exception does
> **not** trigger an arq retry (only `arq.Retry` does) — so the pre-existing assumption that arq was
> retrying the timeout automatically was wrong. See ADR-027's "Notes / corrections" section.

### 4. Degraded-parse visibility

When a résumé's skills extraction fails (catches `LLMOutputInvalidError`, line 154 in `resume_tasks.py`), add a `degraded` boolean and a `degradation_reason` to the `ResumeParsed` schema. Persist both to the `resumes.parsed` jsonb.

In the UI and ranking pipeline:
- Display a degraded-parse indicator (e.g. "Skills not available due to AI extraction failure")
- Exclude degraded résumés from ranking until they are re-parsed (align with decision 2's fail-closed stance — consistency)

This closes the gap where 10 résumés silently had no skills but were marked "parsed".

> **2026-08-02 note:** Implemented — see [ADR-030](030-fu7-degraded-parse-visibility.md). The flag/reason
> ride the existing `resumes.parsed` jsonb verbatim (no DDL change), exactly as scoped here. The exclusion
> mechanism is a skip of the `resume.parsed` outbox enqueue (no Neo4j projection → no stage-1 recall hit),
> mirroring the ADR-026 withdrawn-during-parse skip rather than any new scoring code — consistent with the
> ADR-029 fail-closed stance this section's own rationale anticipates. Visibility surfaces: `ResumeListItem`,
> a `ResumeStatusBreakdown.degraded` sub-count of `parsed` (not a disjoint peer bucket), `get_one` under
> blind+reveal, and UI badges on the résumé detail/list/status-breakdown views. Re-parse is via re-upload
> today; a dedicated `POST /resumes/{id}/reparse` route is a documented follow-up, not built in this slice.

### 5. Timeout sizing as a documented function of hardware

`LLM_TIMEOUT_S` must be set as: **`max_tokens / measured_tok_per_s` + headroom**

The worked example from this incident:
- `max_tokens` = 3072 (from `resume_core_v1` calls in `parse_resume`)
- Measured rate = 23.5 tok/s
- Required time = 3072 / 23.5 ≈ 131 seconds
- Headroom for jitter and contention = 20–40 seconds
- **Correct timeout = 150–170 seconds minimum** (the hardcoded 120 was insufficient)

**Calibration check at startup:** Add a gate in `src/worker/main.py::startup` that logs a WARNING if:
- `llm_timeout_s < max_prompts_max_tokens / measured_tok_per_s + 30`

where `measured_tok_per_s` is either:
- A pre-measured configuration value (e.g. `LLM_MEASURED_TOK_PER_S`), or
- A runtime calibration: send a small known-size prompt, measure the time, log the results, and cross-check against `timeout_s`

Log loudly so a slow peer surfaces at boot rather than 18 hours later.

### 6. Reasoning-model handling

When using a reasoning model (one that allocates tokens to reasoning and content separately), `reasoning_effort` or `think: false` is a large latency lever (~7x on a toy prompt) but also changes extraction quality. This setting must go through the ranking-evals merge-blocking gate (`core/tests/evals/run_evals.py`) before being adopted — never set unilaterally in a worker path or a config file without gate confirmation.

**The `think: false` mitigation already in the code is inert for `gpt-oss:20b`, on both paths.** `client.py:217` (OpenAI-compat) and `client.py:248` (Ollama native) both set `body["think"] = False`, and the code comment at `client.py:212-215` states that the compat layer "honours `think` only intermittently" and directs the reader to `llm_ollama_native=True` for "a reliable thinking-off path". Operator measurement on 2026-07-20 against the configured peer shows **neither path suppresses reasoning for this model**: the OpenAI-compat endpoint returned 579 characters of `reasoning` with `think: False` set, and the native `/api/chat` endpoint returned 1173 characters of `thinking` with the same flag. `llm_ollama_native` also defaults to `False` (`core/src/settings.py:48`), so the path the comment recommends is not even the one in use.

Three consequences follow, and they are the reason this decision cannot rest on `think`:

1. **The existing defence against reasoning-token exhaustion does not work**, so the empty-`content` failure mode is currently unmitigated rather than merely under-mitigated. The comment at `client.py:212-215` should be corrected — it currently tells a future maintainer that a working escape hatch exists when it does not.
2. **`max_tokens` must be budgeted for reasoning *plus* content**, not content alone. The `max_tokens=3072` ceiling in `parse_resume` was sized as if the whole budget were available for JSON output; on a reasoning model an arbitrary and unpredictable fraction is consumed before the first content token.
3. **Detection cannot be replaced by prevention.** Because no flag reliably disables reasoning for this model, the explicit empty-`content` handling below is not defence-in-depth — it is the primary control.

If a reliable thinking-off mechanism is wanted, `reasoning_effort` is the remaining candidate and must be gate-verified; it is not adopted here.

Additionally, handle the **empty `content` string** case explicitly in `_chat_openai` and `_chat_native` (around lines 223–225, 252–254). Both sites already raise `LLMOutputInvalidError` for non-string content, but an empty string passes the `isinstance(content, str)` check and flows downstream to JSON parsing, producing an opaque `JSONDecodeError`. Instead:
- Check if `content` is a string but empty after strip
- Log a diagnostic warning that includes whether a `reasoning` field was present (to distinguish token-exhaustion on reasoning from other causes)
- Raise `LLMOutputInvalidError` with a message like "response content was empty (possibly reasoning model exhausted token budget)" before JSON parsing

> **2026-08-02 note:** Implemented — see [ADR-029](029-fail-closed-ranking-fu7.md). Both `_chat_openai` and
> `_chat_native` now raise `LLMOutputInvalidError` on empty-after-strip `content`, via a shared
> `_empty_content_message(reasoning_present: bool)` helper reporting whether a `reasoning`/`thinking` field
> was present. The inert `think:false`/"reliable thinking-off path" comment this section describes was also
> corrected in the same branch to state plainly that neither path suppresses reasoning for `gpt-oss:20b`.

---

## Consequences

- **LLM outages now block shortlist production instead of silently degrading it.** This is the intended tradeoff: a recruiter who sees "Ranking unavailable" is alerted to investigate; a recruiter who sees a mysteriously low score for a candidate that should rank higher never connects it to an LLM failure. Visibility over silence.
- **Operational requirement: a second provider must be configured and tested.** The single-provider setup is no longer adequate. A pair of Ollama instances (or Ollama + a fallback vLLM deployment, etc.) must be specified in `.env` or deployment documentation.
- **Startup time may increase slightly** if a calibration check is added (a small inference loop to measure token/s). This is acceptable and outweighs the ~18-hour operational pain of discovering timeout insufficiency in production.
- **`parsing` status is now observable in the UI.** Recruiters will see jobs with candidate counts broken down by status, and will notice when a batch of résumés get stuck in `parsing` — an early warning before the 18-hour mark.

---

## Accepted residuals (non-blocking, recorded not fixed)

- **Provider scores may not be strictly comparable.** Provider B may have different quality/calibration than provider A, so the `score_final` for a candidate ranked by provider A is not directly comparable to one ranked by provider B (both are in [0, 1] but different distributions). This is why provider identity is recorded. A future enhancement might run all candidates through both providers for comparison, but that is out of scope here. **Workaround:** When failover occurs, a human reviewer can re-run shortlists against the primary provider once it recovers, if comparability is critical.
- **Circuit breaker state is per-process, in-memory.** A multi-worker deployment has one breaker per worker process. There is no shared backpressure signal across processes. If worker 1's breaker opens, worker 2 will still attempt the endpoint. This is acceptable (no shared state means lower operational complexity); worse case is workers thrash the broken endpoint briefly until each locally opens its breaker.
- **Provider failover does not retry within a single worker task.** Once a provider is exhausted and failover occurs, there are no retries against provider B within the same `parse_resume` call — the task either succeeds or fails over. Retries across multiple arq attempts will re-attempt the full provider chain. This trades some efficiency for simplicity and ensures timeouts are bounded per attempt.

---

## Alternatives Considered

- **Rank structured-only (skills/experience/education) and mark the ranking degraded** — rejected. Decision 2 supersedes this: a degraded ranking that reaches human eyes is worse than no ranking at all, and the fail-closed posture is clearer.
- **Per-candidate quarantine (skip degraded candidates from the shortlist but keep others)** — rejected. An incomplete shortlist is easy to miss or misinterpret. If 10 of 20 candidates are silently filtered due to degradation, a recruiter may not notice and make hiring decisions based on a truncated pool.
- **Raise timeouts alone without addressing the provider chain** — rejected. This treats the symptom, not the root cause. A 170-second timeout would have worked for this incident, but a 20-hour batch job or a multi-step reasoning model could still exceed it. Single-provider fragility remains.
- **Degrade to structured-only scoring without blocking** — rejected. Same as alternative 1.
- **Persist provider identity in the `score_breakdown` row but do not offer failover** — rejected. Traceability without failover leaves users stranded on outages.
- **Support multiple models in a single provider (e.g. "if gpt-oss:20b is slow, try gpt-oss:15b")** — out of scope. Model changes have different quality implications and should be tested through the evals gate. This ADR addresses endpoint failover, not model downgrade.

---

## Notes and findings

### Circuit breaker comment inaccuracy (finding)

`client.py:450–451` code comment claims a half-open trial "will re-open immediately" on failure. The code at line 456 (`_consecutive_failures += 1`) resets after line 453, so re-opening requires `_breaker_threshold` more failures (10 by default), not immediate. The comment should read:

> "Cooldown elapsed — half-open: allow one trial through. If it fails, the failure counter increments; re-opening requires `_breaker_threshold` more failures."

**Fix:** Update the comment to match the code. This is a documentation error, not a code bug.

### Failure reason collection point

The `record_parse_failure` call in `parse_resume` must be wired to catch `LLMUnavailableError` at the task boundary (either in `parse_resume` itself when arq is about to give up, or in the arq task wrapper in `src/worker/main.py`). The exact location is a Phase FU-7 implementation detail; this ADR specifies the contract (row reaches `failed` status with a reason), not the call site.
