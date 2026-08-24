# ADR-040: Disclose the evidence cliff instead of rendering a fabricated 0% (`fix/evidence-cliff-not-assessed`)

**Status:** Accepted (closes the last open item in ROADMAP.md A4 — with M1 (ADR-037) and M2 (ADR-039),
**A4 is now fully closed**; extends [ADR-031](031-why-this-rank-defense-pack.md)'s honesty rules to a case
they did not cover)
**Date:** 2026-08-13

## Context

`evidence_k = 15` bounds stage 3, but **all** of `candidates_s2` goes to `stage4_combine`. A candidate
ranked 16th is never submitted to evidence extraction, so `_evidence_completeness` maps their absent
evidence to `0.0` and `_motivation_score` likewise — **40% of `score_final`** — from **compute placement,
not merit**. `shortlist_top_percent` defaults to 100, so this is live on any job with more than 15
recalled candidates.

**Rank order is provably unaffected** (`0.6·s_i + (≥0) ≥ 0.6·s_j`), which is precisely why this is a
*disclosure* defect rather than a ranking one. The harm is on screen:

```
Evidence · 30% · 0% · 0.00
```

`stage4_combine` produces a **real `0.0` float**, so `explanation.py` set `scores_available=True` and the
panel stated that number **affirmatively** — indistinguishable from a candidate whose evidence *was*
examined and supported nothing. On a page whose entire purpose is defending a ranking decision to a
recruiter, that is a positive false claim about a person.

### ADR-031's guard did not cover this

ADR-031 established that an unrecorded sub-score renders "not recorded", never an affirmative 0%. That
guard protects an **unreadable** row — one whose stored value could not be parsed. This is a
**never-computed** one, which parses perfectly well as `0.0`. Different fact, identical rendering, and
only one of them is honest.

## Decision

Persist an `evidence_evaluated` marker on the write path and render three states. **No scoring change and
no DDL.**

### 1. Three states, because two is not enough

| Value | Meaning | Rendering |
|---|---|---|
| `True` | Stage 3 ran. A `0.0` here is a real measurement. | `0%` |
| `False` | Past the `evidence_k` cliff — never evaluated. | "not assessed" |
| `None` | The row predates the marker, or its folded value was unreadable. | no claim either way |

The third state follows ADR-031's `pipeline_meta=None` → "weights unavailable" discipline exactly: refuse
to state what the row does not support, rather than substituting the convenient default. Asserting "not
assessed" for a legacy row invents a fact just as surely as asserting a measured `0%` does.

### 2. Where the fact comes from

`generate_shortlist` sets it from **`top_k` membership** — the one point in the system that knows which
candidates stage 3 was actually handed.

Deliberately **not** re-derived downstream:

- from `rank` — that is post-combine, so it does not identify the pre-combine slice stage 3 received;
- from `requirements == []` — ROADMAP A4 names this explicitly as the wrong fix. It reads pipeline state
  off a display artifact, and a candidate evaluated against a JD with no requirements produces the same
  empty list.

### 3. Where it lives — folded, not a new column

`shortlist_entries` has no dedicated columns for `score_structured`/`score_evidence` either;
`persist_shortlist` already folds those into the `score_breakdown` jsonb and `_parse_entry_jsonb` unfolds
them. Following the established shape means **no DDL**, and it gives the legacy case the right answer for
free: a row written before this slice simply has no folded key, which unfolds to `None`.

`ScoreBreakdown` is `extra="forbid"`, so the folded key **must** be popped before validation or every row
raises. That failure mode is pinned by a test rather than left to be discovered in production.

### 4. `_folded_evidence_evaluated` is strict about type

Not `bool(value)`. `"yes"`, `1.5` and `[]` are all truthy or falsy without being a *recorded* boolean, and
reporting a corrupted value as an affirmative "assessed" is exactly the invented fact this marker exists to
prevent. Only a real `bool` counts; anything else degrades to "we do not know" — never raising, since this
runs on bytes already committed to a `NOT NULL` column that nothing can rewrite.

### 5. `evidence_assessed` is a different field from `scores_available`

Collapsing them is the obvious shortcut and would silently re-open the defect for exactly the rows it was
built for. A past-the-cliff row stores a perfectly readable `0.0`, so `scores_available` is `True` while
`evidence_assessed` is `False`. One asks *"did the row store a number"*; the other asks *"does that number
mean anything"*. A test pins that they disagree.

### 6. The panel marks both rows, and states the consequence

Motivation is derived from the same stage-3 evidence object, so a past-the-cliff candidate has **neither**
measured. The panel also says the thing a recruiter actually needs to know — the headline score is **not
comparable** across the cut-off. A blank cell alone would leave them to infer that.

**The mirror direction is pinned at the surface too.** ADR-031 records that the fix for "don't show a
fabricated 0" *introduced* the opposite mutant, because `_motivation_score` returns `0.0` for every
candidate with no cover letter — real zeros are the common case, not an edge case. Two panel tests prove
the string is conditional rather than boilerplate: one requires "not assessed" present, another requires it
absent on the same page.

## Consequences

- A recruiter can no longer mistake "we did not look" for "we looked and found nothing".
- The score's non-comparability across the cut-off is stated on the page rather than being a fact only the
  pipeline authors knew.
- **Scoring math is unchanged.** This slice adds a marker and changes rendering; no weight, sub-score or
  ordering moves.

### Accepted residuals

- **The cliff itself is not removed.** Candidates past `evidence_k` still score 0 on 40% of the composite,
  and their headline number is still lower than an assessed candidate's for reasons unrelated to merit.
  This ADR makes that **visible**; it does not make it **go away**. Removing it means evaluating every
  retained candidate (expensive — one LLM call each) or separating structured screening from
  evidence-enriched ranking with distinct labels, which is ROADMAP item 2's territory and a product
  decision.
- **Reverse match is untouched.** `reverse_match_entries` has the same `evidence_k` slice
  (`match_reverse_evidence_k = 10`) and the same fabricated zero. Out of scope here, consistent with
  ADR-031's forward-only boundary, and now a named follow-up rather than an unrecorded one.
- **The list view is unchanged** — only the entry-detail panel discloses this. A shortlist row showing
  `score_final` still mixes assessed and unassessed candidates without saying so.
- **No backfill.** Existing rows stay `None` ("unknown") rather than being retro-marked from their rank;
  inferring the marker for historical rows is the same display-artifact mistake the write path avoids.

## Alternatives considered

- **Infer from `requirements == []` in the template.** Rejected — ROADMAP A4 names it explicitly; it reads
  pipeline state off a display artifact and is wrong for a JD with no requirements.
- **Render `None` for the score instead of a marker.** Rejected: it collapses into ADR-031's existing "not
  recorded" wording, which means something different (unreadable, not un-evaluated), and it would lose the
  three-state distinction on legacy rows.
- **A dedicated `evidence_evaluated` column.** Rejected as unnecessary DDL — the table already folds two
  sibling values into `score_breakdown`, and folding gives the legacy `None` for free.
- **Suppress past-the-cliff candidates from the shortlist entirely.** Rejected: an excluded candidate is
  invisible, which is worse than one shown with an honest caveat, and it would change the ranking rather
  than describe it.
- **Evaluate every retained candidate.** The real fix, and out of scope: it is an LLM call per candidate
  and a product decision about cost, not a disclosure change.

## Gate state

`./scripts/verify.sh all` green, exit code captured directly rather than piped: `EXIT=0`, **4401 unit tests
@ 94.00% coverage, 493 integration tests**. RED measured first: 11 failed.
