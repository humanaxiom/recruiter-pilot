# Subagent Model Tiering Policy

How we assign models to the TDD subagents so **simpler work runs on cheaper models without
compromising quality**. The governing idea: **cheap producers + strong verifiers**. Every
producer's output flows through the three merge-blocking gates before it can land, so those
gates — never the producers — are where quality is guaranteed. That lets us run the high-volume,
well-specified producer work on cheaper tiers and spend the strongest model only where a subtle
miss is expensive and un-caught.

## Tiers

| Tier | Model | When |
|---|---|---|
| **STRONG** | `opus` (Opus 4.8) | Adversarial / correctness-critical reasoning where a miss ships silently. |
| **MID** | `sonnet` (Sonnet 5) | Standard implementation, test-writing, extraction, decomposition — well-specified work backed by a strong gate. |
| **CHEAP** | `haiku` (Haiku 4.5) | Mechanical, low-ambiguity, low-risk, human-reviewed, non-gated work. |

## Per-agent defaults (set in `.claude/agents/*.md` frontmatter)

| Agent | Default | Rationale |
|---|---|---|
| `reviewer` | **opus** | Merge-blocking. Must catch subtle correctness/scope bugs. **Never downgrade.** |
| `security` | **opus** | Merge-blocking adversarial audit (PII, path-safety, offline egress). **Never downgrade.** |
| `ranking-evals` | **opus** | Merge-blocking. Mutation testing + invariant reasoning (the ranking contract). **Never downgrade.** |
| `data-pipeline` | **sonnet** | Domain coder. Most phases are ports/glue Sonnet handles well; **override to `opus`** for the hard core (below). |
| `planner` | **sonnet** | Decomposition / TDD sequencing. |
| `tester` | **sonnet** | Writes guards from a detailed spec; the opus-tier `ranking-evals` gate catches any vacuous guard. |
| `coder` | **sonnet** | Generic green-step coder; **drop to `haiku`** for purely mechanical fixes. |
| `docs` | **haiku** | Writes from a detailed brief; low-risk, human-reviewed, not gated. **Override to `sonnet`** for accuracy-load-bearing handoff/plan refreshes. |
| `Explore` (built-in) | — | No frontmatter; pass `model: sonnet` per-call for extractions, `haiku` for pure file-finding. |

## Per-call overrides (the coordinator decides)

Frontmatter sets the baseline; the coordinator overrides via the Agent tool's `model` param when a
specific task departs from the agent's norm.

**Override UP to `opus`:**
- `data-pipeline` on any diff touching: the **4-stage ranking algorithm**, the **evidence /
  anti-fabrication verifier** (the ≥0.85 fuzzy-quote check), **PII encryption/crypto**, or the
  **Neo4j vector/graph scoring**. These are the invariants a subtle bug silently corrupts.
- `docs` when the deliverable is the **plan-of-record or HANDOFF** and factual precision drives
  session continuity.

**Override DOWN to `haiku`:**
- `coder`/`data-pipeline` for one-shot mechanical edits: import re-sort, dependency version pin,
  rename, formatting-only, boilerplate.
- `Explore` for "where is X defined" lookups.

## Why this doesn't compromise quality

1. **The gates stay strong.** `reviewer` + `security` + `ranking-evals` are all `opus` and all
   merge-blocking. A regression from a cheaper producer is caught before merge, not shipped.
2. **Producers work from detailed specs.** The coordinator writes an explicit phase spec (KEEP/CUT/
   DEVIATION, exact contracts) before dispatch, so producer tasks are well-specified — the regime
   Sonnet/Haiku handle reliably.
3. **The hard core keeps the strong model.** The ranking algorithm, evidence verifier, and PII
   crypto — where correctness is subtle and expensive — get `opus` via per-call override.
4. **CI is the final backstop.** `make gates` (ruff · black · mypy --strict · unit · coverage) and
   the integration suite run on every PR regardless of which model produced the diff.

If a cheaper tier ever produces work the gates repeatedly bounce, raise that agent's default a tier
and note it here — the policy is meant to be tuned by observed gate-pass rates, not guessed once.
