# ADR-032: render JD-authored skill display names, never the global node property

**Status:** Accepted — implemented, gate-green on branch `fix/skill-display-names-and-corpus-gap`, HEAD `ac95f0e`.
**Date:** 2026-08-07
**Amends:** [ADR-008](008-skill-graph-pii-by-construction.md) (residual #4, and a new hard constraint on `Skill.display_name`)

## Context

A live screenshot of the running stack showed the shortlist skill chips rendering hashes:

```
h:6fe660ffc4c40b114fd8a5d365668323 — missing · must-have
h:8cd23374eeec9e197bd9188e7701fbf4 — missing · must-have
leadership — missing · must-have
```

ADR-008 eliminates a PII class *by construction*: any skill outside the curated vocabulary is
salted-hashed (`_canonical_key_for_normalised`, `core/src/pipeline/skills_graph.py:252-296`), so an LLM
mis-extracting `"Casey Rivera"` as a "skill" from a résumé cannot land readable in Neo4j. Five prior
attempts at pattern-matching PII failed; hashing was the answer.

ADR-008 §2 anticipated the display problem and solved it: the **JD side** writes a cleartext
`Skill.display_name` (`core/src/worker/tasks.py`), justified on the grounds that a job description carries
no candidate identity.

**The defect was that `display_name` was never read anywhere in `core/src/`.** It was a dead write. Stage 2
returned `reqSkill.canonical_key AS skill`, that landed in `SkillContribution.skill`, and the UI printed it
raw. The design was complete; the read was missing.

`leadership` rendered correctly only because it is a member of the `soft` family in
`core/src/pipeline/skill_data/categories.yaml` — pure vocabulary membership, unrelated to scoring. The bug
also silently degraded *matched* chips (the JD said "Apache Airflow"; the chip showed `airflow`) and fed
the CSV export columns `skills_matched` / `skills_missing` / `must_have_missing`.

## Decision

1. **Project the JD's own wording onto the relationship.** `_job_projection_tx` now also sets the raw skill
   wording on the `REQUIRES` / `NICE_TO_HAVE` relationship, so each job carries its own label.
2. **Stage 2 reads exactly two sources:** `coalesce(req.display_name, reqSkill.canonical_key) AS skill`.

### The constraint that matters most — never render `Skill.display_name`

The obvious implementation was a three-way fallback
(`req.display_name` → `reqSkill.display_name` → `canonical_key`). **It was wrong, and the security gate
caught it as a cross-job information disclosure.**

The node-level write is `MATCH (s:Skill {canonical_key: $cname}) SET s.display_name = $display` — **globally
scoped, last-writer-wins across all jobs**. The node rung therefore rendered whenever `req.display_name` was
absent, which is *every `REQUIRES` edge projected before this change lands* — the entire pre-existing graph
until re-parse. Job A's shortlist would render Job B's JD wording to Job A's assignees, crossing the per-job
authorization boundary otherwise enforced by `_SHORTLIST_ASSIGNEE_EXISTS_SQL`. It compounds ADR-008 residual
#4: if Job B's author pasted résumé text into a skill field, that cleartext would surface under Job A.

Verified by direct observation against a real Neo4j, in both states: with the node rung, job 1's shortlist
rendered job 2's wording (`React JS`); without it, the same edge renders the opaque
`h:9a8db328d3dfd678a0a238174f1d3c51`.

**So a stale edge deliberately renders an opaque hash.** This is a deliberate, safe choice, not a bug.
Anyone who later sees an `h:` chip and is tempted to "fix" it by re-adding the node rung would be
reintroducing the cross-job leak. That is the single most important thing this ADR records.

**`Skill.display_name` is global and last-writer-wins. It must never be rendered.**
The per-job label lives on the relationship; the node property is not a display source.

### Guards

Both invariants are pinned, not merely asserted in prose:
- `core/tests/unit/test_stage2_skill_label_source.py` — source-level pins that the `coalesce(...)` in
  `_stage2_skill_rows` references only `req.` / `reqSkill.` (no `has.`, no `:Resume`) and does not read the
  node-level `display_name`.
- `core/tests/integration/test_skill_display_names_pg.py` — behavioural pins against a real Neo4j, seeded
  through the **real** `_job_projection_tx` (not a hand-rolled `MERGE`), covering: a non-vocab skill renders
  readably; a vocab hit renders the JD's wording (`Python`, not `python`); a stale edge falls back to
  `canonical_key` and *not* to another job's wording; two jobs sharing a skill cannot see each other's
  wording; and a résumé-side property is never the rendered label.

The last of those exists because the security gate mutated the Cypher to read `has.evidence_chunk_id`
(ADR-008 residual #3 — a pointer back to a résumé region) as the highest-priority label source, and **the
full gate stayed green**. Only pydantic's `str` type caught the control mutation. The invariant ADR-008's
posture rests on, and which this change made load-bearing for the first time, had nothing pinning it.

## Consequences

- **Scoring math is byte-unchanged.** `SkillContribution.skill` is an inert label; `score_skill_breakdown`
  never reads it. ranking-evals confirmed the corpus metric dump is byte-identical to base
  (md5 `58ff49f68cbf4b8390be06024152249b`), and `stages.py` / `schemas/matching.py` are blob-identical.
- **ranking-evals structurally cannot see this change.** `run_evals.py::_skill_rows_for` reimplements the
  Cypher in Python and can never produce an `h:` key, so its PASS is a statement of *non-regression*, not of
  correctness. The correctness evidence is the two test files above. This is stated plainly rather than
  letting a green gate imply more than it proved.
- **Existing `shortlist_entries` keep their hashed labels** until the shortlist is regenerated. Read-time
  repair was considered and rejected as *unsound*, not merely costly: the stored label is keyed to the
  requirement set the persisted score was computed against, so a re-parsed JD would paint today's wording
  onto yesterday's arithmetic — silently. A hash reads as opaque; a plausible wrong name does not. A one-shot
  offline backfill (re-generate affected jobs) is available if determinism is wanted.
- **CSV formula injection was fixed across the whole export**, not just the widened columns. The class was
  pre-existing (via `candidate_name`, `evidence_summary`, `quote`, `requirement`, `resume_file`) but this
  change widened it: those skill columns moved from a closed set (curated vocab or `h:<32 hex>`) to arbitrary
  JD text, and `reject_reason_for_skill_name` only screens length/token-count/email/phone shape — so a skill
  named `=cmd|'/c calc'!A1` reached the export. Every string cell in both writers now routes through
  `_csv_safe`.

### ADR-008 amendment — residual #4 is now a render- and export-path exposure

ADR-008 residual #4 records that JD-authored cleartext is trusted. **This change does not alter what is
stored, and creates no new PII class** — but it converts that residual from a latent, write-only exposure
visible only to someone with Neo4j read access into a **rendered** one: shown in shortlist chips to all four
roles (shortlist reads are `tuple(Role)`, including `auditor` and `hiring_manager`) and written into CSV
files that leave the system.

Bounded by: JD authoring is admin/recruiter only, the raw name is pydantic-validated and capped at 60
characters / 8 tokens, and `reject_reason_for_skill_name` drops PII-shaped names before projection (pinned
by `test_pii_shape_rejected_skill_is_dropped_silently_not_projected`, which asserts a rejected name reaches
**no** `tx.run` params — so it covers write paths that do not exist yet).

## Accepted residuals

- **Three write-only JD-side properties.** `Skill.display_name` (both the REQUIRES and NICE_TO_HAVE loops)
  and the `NICE_TO_HAVE` relationship property are now written and read by nothing in `core/src/`. Not a
  hazard for the audited path — re-adding the node rung to `_stage2_skill_rows` fails loud — but the source
  pin is scoped to that one function, so a *future* reader elsewhere (a skills-browser endpoint, say) would
  reintroduce the cross-job class unguarded. Follow-up: either delete the writes (needs its own RED cycle
  and a re-projection story) or add a repo-wide source pin that no module reads `Skill.display_name`.
- **`test_stage2_skill_rows_reads_canonical_key_not_canonical_name`** is now misleadingly named — it still
  kills the terminal-drop mutant, but its message blames `reqSkill.canonical_name` for what is a
  coalesce failure. Cosmetic; not folded in.
- **The corpus gap is NOT closed.** An attempt to close it on this branch was reverted (see below).

## The reverted attempt, recorded so it is not repeated naively

The first version of this branch also tried to close the evals corpus's blindness to ADR-008 hashing. It
was reverted in `53f225e` because the reviewer gate showed it was both harmful and inert:

- **Harmful:** it moved r09 (the adversarial keyword-stuffer) from rank 12 to 11 and r18 (tagged `strong`)
  from 11 to 12 — the bait overtook a strong fixture — and the margin below the 5th-place cutoff fell
  0.1726 → 0.1452. `thresholds.toml` names *"the bait is BELOW EVERY STRONG FIXTURE"* as what is gated, **but
  that invariant lives in prose rather than an enforced threshold, so `run_evals.py` still exited 0** and the
  regression was invisible to the gate.
- **Inert:** `_canonical_key_for_normalised` is called only in the `jd["required"]` loop, and the added skill
  was a *nice-to-have* — nice-to-haves never reach `_skill_rows_for` — so no hashed key entered the corpus at
  all. Proven by mutation: reverting the keying swap left every metric byte-identical.

Closing the blindness properly requires the non-vocab skill to go in `required_skills`, which forces a
must-have miss for every honest fixture and re-bands the corpus. The documented margins must then be
re-measured, not assumed. That is a deliberate corpus change of its own.

## Related open findings (not this ADR's scope)

- **The skill-vocabulary gap.** Measured across the live DB: every real SFU job description is 47-84%
  outside the curated vocabulary and scores ≤ 0.0375 on the skill sub-score, because hashed nodes carry no
  categories (`hashed_total=288, with_categories=0`), making ontology family credit unreachable by
  construction — the only surviving path is exact normalised-string equality. The two corpus fixture JDs are
  0% hashed, which is exactly why the gate never saw it. The hashes were the *marker*; non-vocab-ness is the
  cause. Any fix changes scoring math and is ranking-evals-gated — and the corpus cannot currently measure
  it, so the corpus work is a prerequisite.
- **Unenforced corpus invariants:** "bait below every strong fixture" is prose, not a gated assertion;
  `expected_rank_band` is never referenced by `run_evals.py`, and r18 currently violates its own declared
  band (tagged `strong`, band `{1,9}`, actual rank 11).

## Alternatives considered

- **Gate the node fallback on vocab keys** (`NOT reqSkill.canonical_key STARTS WITH 'h:'`) — would confine
  the fallback to the closed curated vocabulary and keep nicer casing for pre-existing vocab edges. Rejected
  for slice 1 as more complexity for a cosmetic gain, given the stale-row story already accepts
  hashes-until-regenerate.
- **Read-time repair in `shortlist_service`** — rejected as unsound (see Consequences) and because the read
  path is deliberately Postgres-only; it would need a Neo4j driver threaded through the read routes.
