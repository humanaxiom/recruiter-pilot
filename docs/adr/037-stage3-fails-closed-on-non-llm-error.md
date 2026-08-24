# ADR-037: Stage 3 fails closed on a non-LLM error too (`fix/stage3-fail-open-non-llm`)

**Status:** Accepted (closes ROADMAP.md A4 **M1**; completes the fail-closed posture
[ADR-029](029-fail-closed-ranking-fu7.md) claims for the ranking stage, and honours
[ADR-021](021-llm-resilience.md) §2's rejection of partial shortlists)
**Date:** 2026-08-13

## Context

`stage3_evidence._one` caught bare `Exception` per candidate and set `results[id] = None`, under the
comment *"one candidate must not sink all"*. The intent was kindness. The effect was a wrong ranking.

`_evidence_completeness` maps `None` to `0.0`, and `_motivation_score` likewise, so the affected candidate
silently lost the whole `evidence` (0.30) plus `motivation` (0.10) share — **40% of `score_final`** — and
the row was **persisted unmarked**.

### Why this one is worse than the evidence cliff sitting next to it

The systematic evidence cliff (`evidence_k=15`) provably *cannot* reorder the displayed list: every
candidate past the cliff loses the same share, so `0.6·s_i + (≥0) ≥ 0.6·s_j` preserves order. **M1 is not
systematic.** It hits one candidate, at random, *inside the top 15* — the ranks a recruiter actually
reads — and only when a transient Neo4j/Postgres hiccup happens to land on them.

That combination is the problem:

- it **displaces real people** in the visible ranks;
- it is **unreproducible** by the time anyone notices, because the trigger was transient;
- on screen it is **indistinguishable from a candidate evaluated and found lacking**, because
  `stage4_combine` produces a real `0.0` float.

### It was invisible by construction

A `None` from a *failure* looked exactly like a `None` from being *past the cliff*. One `None` meant "we
tried and it broke"; the other meant "we never looked". Both rendered an affirmative `0%`. No amount of
inspecting the persisted row could tell them apart.

## Decision

**Fail closed.** `stage3_evidence._one` re-raises instead of swallowing, and `generate_shortlist` wraps a
non-LLM stage-3 failure into `RankingUnavailableError` — the same typed error the Mode A/B LLM path already
raises.

The whole change is **four functional lines, all inside exception handlers.**

### 1. Reuse the existing machinery rather than adding any

`shortlist_job` already catches `RankingUnavailableError`, records the visible fail-closed state
(`reason=str(exc)`, its own transaction so it commits as the run bails out), and re-runs under `arq.Retry`
up to `shortlist_max_tries`.

A transient Neo4j/Postgres blip — the realistic cause — is **exactly what a retry fixes**. So failing
closed costs a deferred re-run rather than a silently corrupted ranking. No DDL, no new state value, no new
settings knob.

### 2. The reason string carries the exception type

`jobs.shortlist_state` is CHECK-constrained to the single value `'awaiting_llm'`, so the state *label*
cannot distinguish these causes. `shortlist_state_reason` is free text and is the only diagnostic an
operator will ever see, so the non-LLM arm prefixes it with `type(exc).__name__`:

```
stage 3 evidence failed (ConnectionError): connection reset by peer
```

Three causes — the model was down, the database blinked, this is a bug that will retry to the ceiling and
never succeed — have three different fixes and must not read identically.

### 3. `None` regains a single meaning

This is the part worth keeping in mind when reading the pipeline, not a side effect. After this change
`None` in `evidence_by_id` means only:

- **nothing to evaluate** — `_stage3_per_candidate` returns `None` for a candidate with no chunks, or for a
  job with no `required_skills`; or
- **past the `evidence_k` cliff** — the id is simply absent and `.get()` yields `None`.

It never again means "we tried and it broke." A test pins the first case explicitly, because failing closed
must not turn a legitimate "nothing to evaluate" into an error — every job without required skills would
otherwise withhold its shortlist forever.

## Consequences

- One flaky candidate now withholds the whole shortlist and retries, instead of quietly producing a wrong
  one. That is the ADR-021 §2 trade this repo already made for LLM failures, applied to the path that was
  missed.
- **Scoring math is byte-unchanged, by construction rather than by measurement.** The entire diff is
  confined to `except` branches, and a successful run does not enter one — the eval corpus raises nothing,
  so there is no code path on which its ranking could differ.
- A genuine coding error (say a `TypeError`) now retries to the ceiling before giving up, rather than
  producing a shortlist. Bounded, visible in the reason string, and strictly better than persisting a
  corrupted ranking.

### Accepted residuals

- **The two tests that pinned this as correct were rewritten, not deleted** — the same shape as ADR-034's
  13 tests. Their docstrings carry the reversal, so a reader who finds them later sees why the contract
  flipped instead of assuming a regression.
- **`asyncio.gather` does not cancel siblings.** When one candidate raises, the others run to completion in
  the background before the error propagates — wasted LLM calls on the peer. **Pre-existing and identical
  on the LLM path**; not introduced or worsened here, and not fixed here because it is a separate change to
  a shared code path. Worth a follow-up.
- **The evidence cliff itself is NOT addressed** (ROADMAP A4's third item): a past-the-cliff candidate still
  renders an affirmative `Evidence · 30% · 0%`. That needs a persisted `evidence_evaluated` marker on the
  write path. This ADR only removes the *failure* case from the same ambiguity — it does not resolve the
  *never-computed* case.
- **M2 (stage-1 recall is a global vector query) is untouched** and remains ROADMAP A4's other open defect.

## Alternatives considered

- **Mark the candidate "evidence not evaluated" and exclude them from the shortlist**, mirroring ADR-030's
  degraded-parse skip. Rejected: it silently drops a real candidate, which is its own harm and arguably
  worse — an excluded candidate is invisible, where a withheld shortlist is loud. ADR-021 §2 already
  rejects partial shortlists.
- **Persist the degraded score with a marker**, leaving the ranking wrong but labelled. Rejected: the rank
  order is what a recruiter acts on, and a label does not un-displace the candidate who was pushed down.
- **Retry just that candidate in place.** Rejected for this slice: it needs a per-candidate retry budget and
  interacts with the shared `asyncio.Semaphore`; the job-level retry already exists and is bounded.
- **A new `shortlist_state` value** (e.g. `ranking_failed`) instead of reusing `awaiting_llm`. Rejected as
  scope: it needs a DDL change to the CHECK constraint plus API and frontend updates. The reason string
  carries the distinction today; the label is recorded here as a known misnomer rather than left implicit.

## Gate state

`./scripts/verify.sh all` green, exit code captured directly rather than piped: `EXIT=0`, **4381 unit tests
@ 94.20% coverage, 488 integration tests**. RED was measured first: 3 failed, 4378 passed.
