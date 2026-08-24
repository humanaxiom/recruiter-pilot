# ADR-038: Gate the bait-below-strong ordering the corpus only asserted in prose (`fix/evals-gate-bait-below-strong`)

**Status:** Accepted (closes ROADMAP.md A3's "recommended first move" and its second bullet's
bait-ordering half; the ADR-008 hashing blindness, the `expected_rank_band`/r18 discrepancy and the inert
`skill_missing_must` pair remain open)
**Date:** 2026-08-13

## Context

`thresholds.toml`'s `[adversarial]` block carries a long round-6 reconciliation ending in this claim:

> *"What IS gated, and by ~0.19 of margin either way: the bait is BELOW EVERY STRONG FIXTURE and therefore
> outside the k=5 window."*

It was not gated. It was a **comment**. Nothing read it, so a change that violated it still exited 0 — and
one did: the ADR-032 attempt inverted this ordering and had to be reverted, with no gate objecting.

This is the ROADMAP A7 defect shape — an invariant stated in prose with nothing enforcing it — occurring
**inside the gate itself**, which is the worst place for it. Every scoring fix in this repo is only as
trustworthy as this harness, and on this axis the harness was asserting in English.

### What the pre-existing gates actually covered

`precision@k` and `must_not_surface_in_topk` both only notice the bait **once it reaches the top-5**. The
region between "outranks a strong fixture" and "reaches the top-5" was uncovered — and it is not a narrow
corner, as the measurement below shows.

## Decision

Add `[adversarial] must_rank_below_every_strong = true`, enforced by
`_assert_bait_ranks_below_every_strong` as an **order relation over tags**: every `adversarial`-tagged
fixture must rank strictly below every `strong`-tagged one.

### Why an order relation and not `expected_rank_band`

- The per-fixture bands **go red immediately on r18** (tagged `strong`, declared band `{1,9}`, actual rank
  11). That discrepancy is real and needs its own reconciliation; it is not this gate's business, and a
  gate that goes red on arrival teaches people to disable gates.
- An order relation needs **no new fixtures and no measured constants**, so it cannot drift with the corpus
  the way a pinned rank does. This is also why the round-6 note says *"do not re-pin a rank here"*: r09
  sits 2.8e-04 from r04 and the two swap between builds, while their **relation** to the strong tier holds
  by ~0.19.

### Scoped to `adversarial`, not `weak`

The measured claim covers the bait. Extending the relation to the five `weak` fixtures is unmeasured, and
the `[adversarial]` section name is the honest scope.

### Measured arming — the part that matters

Adding an unfalsifiable assertion to a harness whose problem *is* unfalsifiable assertions would be worse
than adding nothing. So the gate was swept against the **real corpus** by mutating `weights.evidence` and
recording which assertion fires first:

| `weights.evidence` | What fires |
|---|---|
| 0.30 (default), 0.28 | GREEN |
| **0.25** | **this gate alone** — bait rank 11, worst strong 12 |
| **0.22** | this gate alone — rank 10 vs 12 |
| **0.20**, **0.15** | this gate alone — rank 9 vs 12 |
| **0.10** | this gate alone — rank 7 vs 13 |
| 0.05, 0.00 | `precision@k` (the bait reaches the top-5) |

**There is a real detection band, ~0.25 down to ~0.10, in which the bait outranks strong fixtures while
every pre-existing gate stays green.** Halving the evidence weight — a plausible tuning change — used to
pass the entire harness.

The margin from the default is ~0.02–0.05 of evidence weight, which is modest and worth stating plainly.
It is **not** a false-positive risk: below ~0.28 the bait genuinely *is* competitive with real candidates,
and that is the fact worth failing on.

### The helper is unit-tested directly

Four tests feed `_assert_bait_ranks_below_every_strong` synthetic rankings: it passes when the bait is
last; fires when the bait outranks one strong fixture; fires on an **exact tie** (pinning `>` rather than
`>=`, since an off-by-one silently re-opens the hole); and **refuses to pass vacuously** if either tag
group empties.

The existing `_assert_*` helpers in `run_evals.py` have no such coverage — they are exercised only by the
corpus run, which proves they pass but never that they *can fail*. This is a deliberate improvement on that
convention rather than an inconsistency with it.

## Consequences

- A ranker that lets bare keyword overlap outrank evidence-backed candidates now fails the gate, in a band
  where nothing previously objected.
- All four legs of the machine-checked three-way key contract were updated together, as that contract
  requires: `thresholds.toml`, `run_evals.py`'s docstring, `.claude/agents/ranking-evals.md`, and
  `_THRESHOLD_KEYS` in `test_evals_corpus.py`.
- The measured band is recorded in `thresholds.toml` beside the key, so a future editor tuning
  `weights.evidence` sees why it trips before they conclude the gate is broken.

### Accepted residuals — A3 is NOT fully closed

- **Blind to ADR-008 hashing by construction.** `run_evals.py::_skill_rows_for` reimplements the stage-2
  Cypher in Python and keys via `_basic_normalise`, so it can never produce an `h:` key. Untouched here;
  closing it needs a non-vocab skill in `required_skills`, which forces a must-have miss for every honest
  fixture and re-bands the corpus — margins must be **re-measured**.
- **`expected_rank_band` is still never referenced**, and r18 still violates its own declared band.
  Deliberate: enforcing it wholesale goes red on arrival.
- **The `skill_missing_must` ordering pair is still inert** against `weights.skill = 0` (the carried N-1
  corpus finding — a vector-embedding residual, not the intended arithmetic gap).
- **`weak` fixtures are not covered** by the new relation.

## Alternatives considered

- **Enforce `expected_rank_band` wholesale.** Rejected — goes red immediately on r18. ROADMAP A3 names this
  explicitly as the thing not to do.
- **Pin r09's exact rank.** Rejected — it is near-tied with r04 (2.8e-04) and the two swap between builds;
  the round-6 note warns against exactly this.
- **Extend the relation to `weak` fixtures too.** Rejected as unmeasured for this slice.
- **Assert only via the corpus run, matching the existing `_assert_*` convention.** Rejected: that proves
  the gate passes, never that it can fail, which is the precise failure this ADR exists to correct.

## Gate state

`./scripts/verify.sh all` green, exit code captured directly rather than piped: `EXIT=0`, **4387 unit tests
@ 94.20% coverage, 488 integration tests**.
