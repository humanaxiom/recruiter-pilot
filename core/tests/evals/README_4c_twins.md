# Round-8 (4c prep) eval-corpus additions

Scope: `docs/EXTRACTION_PLAN.md`'s "4b -> 4c BLOCKERS" section (2026-07-14) and
the carried-forward R1/R2 residuals it names. This note is the coder-facing
summary; the full rationale lives in `fixtures/labels.json`'s `_comment` array
and its per-fixture / per-pair notes, and in `thresholds.toml`'s comments --
this file does not repeat that prose, it indexes it and lists the exact
mechanical follow-ups.

**Ownership boundary that produced this split note:** this round's tester owns
`fixtures/resumes/*.json`, `fixtures/labels.json`, `thresholds.toml`, and this
README. It explicitly does **not** own `core/tests/unit/test_evals_corpus.py`
(or `src/pipeline/matching/*`, which does not exist yet). Three constants in
that test file are now stale as a **direct, mechanical, and required**
consequence of this round's additions -- not a defect in this round's work.
Confirmed by patching a **scratch copy** of that file (never the real one)
with exactly the three changes below and running it against the corpus
unmodified: **351 passed, 1 skipped, 0 failed.** Against the *real*
(unpatched) `test_evals_corpus.py`, the corpus as landed produces exactly
**8 failures, all attributable to these three constants** (342 passed, 1
skipped otherwise) -- see "Verification" below for the exact command and
output.

## 1. Three fixtures added

| Fixture | Tag | Pairs against | Isolates |
|---|---|---|---|
| `r18_casey_rivera_missing_must_have.json` | `strong` | `r01_casey_rivera` (`skill_missing_must`) | A genuinely missing must-have. Byte-for-byte r01 minus the `REST API design` skill entry. That skill carries no family in `categories.yaml`, so its absence is an unambiguous `ontology_weight=0` -- unlike Apache Airflow, whose family (`data`) is shared with Python, so any candidate holding Python (itself a required must-have) always gets family credit toward a missing Airflow row and can never demonstrate a clean zero for it. |
| `r19_jamie_okafor_recency_twin.json` | `borderline` | `r10_jamie_okafor` (`recency`) | Recency decay. Byte-for-byte r10 with every skill's `last_used_year` moved from the old bucket (2017/2018) to the recent bucket (2026); nothing else changed. `_build_summary_text` reads neither `last_used_year` nor experience start/end dates, so the embedding input is **byte-identical** to r10's -- the same technique that makes the existing overqual/motivation pairs airtight. Gives r10's `decision_point` (`recency_decay_stale_skills`, decorative since round 3) an actual twin. |
| `r20_casey_rivera_spelling_twin.json` | `strong` | none (deliberately not an ordering pair -- see below) | Skill-spelling normalisation. Byte-for-byte r01 except the `REST API design` skill entry is spelled `REST APIs`. Both spellings resolve to the same canonical concept via `src/pipeline/skill_data/aliases.yaml`'s alias list (ADR-008 residual #6's post-4b fix). Gated by sharing r01's `strong` band, not by a pairwise assertion -- see the fixture's own `expected_rank_band_note` and `labels.json`'s round-8 comment for why a rank-band membership check is the right mechanism here (r01 and r20 should *tie*, and `ordering_controls` has no way to express "approximately equal", only strict `rank(higher) < rank(lower)`). |

All three reuse an existing `FAKE_NAMES`-allowlisted candidate name (Casey
Rivera x2, Jamie Okafor x1) specifically so this round does not need to touch
that allowlist. Nothing in the corpus asserts candidate-name uniqueness across
fixture files (verified: the round's tester grepped for `unique`/`Counter`/
`duplicate` in `test_evals_corpus.py` and found nothing keyed on names).

## 2. Two new `ordering_controls` pairs

Added to both `fixtures/labels.json`'s `ordering_controls` array and
`thresholds.toml`'s `[ordering_controls].pairs` (kept in lockstep, as the
existing three pairs already are):

- `skill_missing_must`: `r01_casey_rivera` (higher) vs
  `r18_casey_rivera_missing_must_have` (lower). Correct-engine gap: **0.144**
  in `score_final` units (`0.6*0.40*0.60`).
- `recency`: `r19_jamie_okafor_recency_twin` (higher) vs `r10_jamie_okafor`
  (lower). Correct-engine gap: **0.144** in `score_final` units
  (`0.6*0.40*0.60`), and -- unlike every other pair in this corpus, including
  the one above -- **provably exact**, not measured, because the twins'
  embedding input is byte-identical.

Both new pairs' members **share a tag** with their partner (`r18`/`r01` both
`strong`; `r19`/`r10` both `borderline`), matching this corpus's existing
house convention (`test_ordering_control_pair_members_share_the_same_tag`,
which the round-8 additions were specifically checked against -- see
"Verification").

## 3. `thresholds.toml` gains `[evidence].gold_recall_min = 1.0`

Closes carried-forward finding R2 ("the corpus gates the evidence verifier,
never the evidence extractor"). Full rationale in `thresholds.toml`'s own
comment above the key. Because this is a genuinely new threshold key, the
round's tester also updated the two other legs of the three-way key contract
it could reach: `run_evals.py`'s docstring (documentation only -- `_run_corpus`
and every other executable line is untouched) and
`.claude/agents/ranking-evals.md`'s threshold table. The **fourth** place this
key is enumerated, `test_evals_corpus.py::_THRESHOLD_KEYS` (a hardcoded
`list[tuple[str, str]]` literal), is out of this round's ownership -- see
below.

## 4. Mutation obligations (the six named in the task, plus the corpus's own history of three)

| Mutation | Gated by | How |
|---|---|---|
| `weights.education = 0` | `education` pair (pre-existing) | Rank+gap, measured residual (-8.7e-04) |
| `overqual_ratio = 99` | `overqual` pair (pre-existing) | Rank+gap, pure arithmetic (+0.0120) |
| `weights.motivation = 0` | `motivation` pair (pre-existing) | Rank+gap, measured LLM confidence (+0.0900) |
| `weights.skill = 0` | **both new pairs** | Rank+gap. `recency` pair: byte-identical twins, so this mutation forces an **exact** tie (0.000e+00) -- deterministic, no embedder needed. `skill_missing_must` pair: removes the entire 0.144 arithmetic gap, leaving only a small, **unmeasured** vector residual from one dropped skill-name token in the embedded `Skills:` line -- 4c must measure its sign before trusting this pair's verdict at the `min_score_gap` boundary (same discipline already applied to the `education` pair). |
| recency-decay-disabled (force `recency_recent`/`recency_mid`/`recency_old` all to `1.0`) | `recency` pair | Rank+gap, **exact** tie by construction (byte-identical twins) -- deterministic. |
| `must_have_miss_penalty: 0.5 -> 1.0` | **Not gateable by any pairwise rank+gap check, for any twin of this shape** -- see below. |

### Why `must_have_miss_penalty` can't be a pairwise mutation gate (a round-8 finding, provable without an embedder)

`stages.score_skill_breakdown` forces a genuinely-missing row's score to
exactly `0.0` (`if row.ontology_weight == 0: score = 0.0`), independent of the
penalty. For `r18` (4/5 required skills present, 1 genuinely missing), the
pre-penalty mean is `0.8` regardless of the penalty's value; the penalty only
multiplies the *overall* mean once, afterward. So:

- `must_have_miss_penalty = 0.5` (current default): `r18` scores `0.8*0.5 =
  0.40`. Gap vs `r01` (`1.00`): `0.60` skill units, `0.144` score_final units.
- `must_have_miss_penalty = 1.0` (mutated): `r18` scores `0.8*1.0 = 0.80`. Gap
  vs `r01`: `0.20` skill units, `0.048` score_final units.

The gap **shrinks by ~3x** but can never reverse or collapse below
`min_score_gap`, because the raw `1.00` vs `0.80` difference (one row out of
five, forced to `0.0` by construction) survives *any* penalty value up to and
including `1.0`. This is an algebraic property of a "mean of per-skill scores,
then one multiplicative penalty" formula -- **it would hold for any similarly-shaped
twin**, not just this one, so no amount of fixture redesign closes it via the
existing pairwise mechanism.

**Required 4c verification instead:** a single-candidate, before/after
numeric check on `r18` alone: `score_final(r18, must_have_miss_penalty=0.5)`
must be measurably lower than `score_final(r18, must_have_miss_penalty=1.0)`
by approximately `0.048` (`0.6*0.40*0.20`). This is a **review obligation**,
in the same spirit as round 7's M-3 obligations for the three blind-engine
mutations above (which also cannot be run by `thresholds.toml` or this
harness alone -- they need the live engine with mutated `MatchWeights`) --
recorded here, in `fixtures/labels.json`'s `skill_missing_must` rationale,
and in `thresholds.toml`'s `[ordering_controls]` comment, not silently
dropped.

## 5. Required companion changes to `core/tests/unit/test_evals_corpus.py`

Not made by this round (explicit ownership boundary: fixtures/labels/toml
only). All three are single, precisely-located constant updates; **verified**
by patching a scratch copy with exactly these three diffs and confirming the
full suite goes green (351 passed, 1 skipped, 0 failed) -- see "Verification"
below for the reproduction command.

1. **`_THRESHOLD_KEYS`** (around line 920) -- append one tuple:
   ```python
   ("evidence", "gold_recall_min"),
   ```
2. **`_TAG_POPULATIONS_AT_AUTHORING_TIME`** (around line 2496) -- update to
   reflect the corpus's growth from 17 to 20 fixtures:
   ```python
   _TAG_POPULATIONS_AT_AUTHORING_TIME = {
       "strong": 9,       # was 7
       "borderline": 5,   # was 4
       "weak": 5,         # unchanged
       "adversarial": 1,  # unchanged
   }
   ```
   This derives (via the existing formulas already in the file)
   `TAG_RANK_BANDS = {"strong": [1, 9], "borderline": [10, 15], "weak": [15,
   null], "adversarial": [10, null]}`, which is exactly what
   `fixtures/labels.json` now asserts per-entry. Whoever changes a fixture's
   tag again must recompute both together, per the round-2 rule this file's
   own comments already state.
3. **`_EXPECTED_ORDERING_CONTROL_DIMENSIONS`** (around line 2868) -- add the
   two new dimension names:
   ```python
   _EXPECTED_ORDERING_CONTROL_DIMENSIONS = {
       "education", "overqual", "motivation", "skill_missing_must", "recency",
   }
   ```

No other constant in that file needed a change -- confirmed by the full-suite
scratch-patch run below, which exercises every other invariant in the file
(twin-integrity, PII scanning, gold/negative-evidence anchors, rank-band
feasibility, adversarial-bait potency, etc.) against the corpus exactly as
landed.

## Verification

Reproduced from a clean clone, `core/` as the working directory, in a
`python:3.11-slim` container with `requirements.txt` + `requirements-dev.txt`
installed:

```
python -m pytest tests/unit/test_evals_corpus.py -q
# 8 failed, 342 passed, 1 skipped
# failures: test_thresholds_toml_has_no_key_outside_the_enumerated_contract,
#           test_every_threshold_key_is_enumerated_by_both_consumers,
#           test_thresholds_ordering_control_pairs_match_labels_json_exactly,
#           test_every_label_entry_has_an_expected_rank_band_matching_its_tag,
#           test_expected_rank_bands_fit_tier_populations,
#           test_labels_manifest_has_ordering_controls_for_each_gated_dimension,
#           test_ordering_control_entry_is_well_formed[skill_missing_must],
#           test_ordering_control_entry_is_well_formed[recency]
```

All eight trace to exactly the three constants in section 5. Applying only
those three edits to a scratch copy of the test file (never the real one) and
re-running against the same fixtures/labels.json/thresholds.toml:

```
351 passed, 1 skipped in 2.96s
```

`run_evals.py` still exits `1` (unchanged RED state -- the orchestrator import
guard, not this round's additions, is what fails it):

```
python tests/evals/run_evals.py
# ranking-evals: src.pipeline.matching.orchestrator is not implemented yet ...
echo $?  # 1
```

## 6. Post-4c finding N-1/N-2 (measured on branch `feat/why-this-rank-defense-pack`, present identically on `main`)

Section 4 above flagged the `skill_missing_must` mutation obligation as leaving an **unmeasured** vector
residual — "4c must measure its sign before trusting this pair's verdict at the `min_score_gap` boundary."
It is now measured, as a side effect of the `ranking-evals` run for the "Why this rank?" defense pack
(ADR-031). This finding is **not caused by that change** — the ordering pair and both fixtures are unchanged
— and reproduces identically against a `main` worktree.

- **N-1: the `skill_missing_must` ordering pair is inert against `weights.skill = 0`.** Measured: with
  `weights.skill = 0` the pair stays correctly ordered by `+4.895691e-03` in `score_final` units, on **both**
  input orders — roughly 4900x above `min_score_gap = 1e-6`. Root cause: `r18` is `r01` minus one skill
  entry, and `_build_summary_text` embeds the `Skills:` line, so the vector encoder sees a different string
  for the two fixtures even though `_build_summary_text` was designed, for the corpus's other twin pairs, to
  produce byte-identical embedding input. The resulting **vector** residual is `0.08159484562693509` (vector
  units); at `weights.vector = 0.6 * 0.10`, that is `+0.00490` of `score_final` — which is what survives when
  the `weights.skill = 0` mutation removes the pair's intended `0.144` arithmetic gap and points the surviving
  gap at `r01`, the higher twin (the correct direction, just not for the intended reason). Same shape as the
  round-5 F2 and round-7 R7-2 vector confounds this corpus already documents elsewhere. **Verdict: inert** —
  the mutation does not flip or collapse the pair's ordering, so it does not currently threaten the gate, but
  the pair is not actually gating what its name claims (a genuinely-missing-skill penalty) once `weights.skill
  = 0` is in play; it is gating a residual embedding artifact instead. Adjacent to, but distinct from, the
  standing R1 residual.
- **N-2 (doc nit): §4's stated `must_have_miss_penalty` gap for `r18` is wrong; the number directly above it
  in the same section is right.** §4's prose states the penalty-mutated gap as "approximately 0.048
  (0.6*0.40*0.20)". Measured: **0.096** (`0.6*0.40*0.40`) — `r18`'s skill mean goes `0.40 -> 0.80` under the
  `must_have_miss_penalty: 0.5 -> 1.0` mutation, a delta of `0.40`, not `0.20`; the bullet list immediately
  above that sentence in §4 already states the `0.40` delta correctly. The underlying obligation is satisfied
  regardless — `_assert_must_have_penalty_fires_on_r18` is green — this is a correction to the write-up's
  arithmetic only, not to any fixture, threshold, or test.

**Follow-up ownership:** both are corpus-level findings, owned by the round's corpus tester per this file's
own ownership boundary (section "Ownership boundary" above), not by whatever feature branch happened to run
`ranking-evals` and notice them. Recorded here so a future round doesn't have to re-derive the sign; not
fixed as part of this note.
