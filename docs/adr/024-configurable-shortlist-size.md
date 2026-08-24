# ADR-024: Per-job configurable shortlist size (top P%)

**Status:** Accepted (implemented 2026-07-25 on `feat/configurable-shortlist-size`; all gates green — reviewer APPROVE, security PASS, ranking-evals PASS)
**Date:** 2026-07-25

## Context

A user asked for the number of résumés shortlisted to be "configurable, instead of fixed 16 — 1-100%." Diagnosis found there was **no fixed cap**: `generate_shortlist` persisted *every* ranked candidate returned by stage-1 coarse retrieval (up to `match_coarse_k = 50`), with only the top `match_evidence_k = 15` receiving LLM evidence. The "16" a user saw was simply that job's résumé count. So the ask is a genuine new capability: let each job cap its shortlist to the top P% of the ranked pool.

## Decision

### 1. Per-job, not global

A new `jobs.shortlist_top_percent` column (`INTEGER NOT NULL DEFAULT 100 CHECK (BETWEEN 1 AND 100)`), settable at job creation and editable, caps the persisted forward shortlist to the top P% of the ranked candidate list. **Default 100 = "keep all," byte-identical to prior behaviour.** Per-job (not a global setting) was the human's choice: different requisitions want different shortlist depths, and a global knob would force one cutoff on every job.

Added idempotently (`ALTER TABLE jobs ADD COLUMN IF NOT EXISTS ...` alongside the CREATE TABLE, matching the `description_sha256` convention — no migration framework). The DB `CHECK` is the last line of defence behind the pydantic `ge=1, le=100` bound.

### 2. The cap: a ceil-based prefix slice

`orchestrator._apply_top_percent_cap(entries, top_percent)`:
`n_keep = max(1, ceil(len(entries) * top_percent / 100))` for a non-empty pool, `0` for an empty pool; return `entries[:n_keep]` — the already-rank-ordered prefix, **never a re-sort**. `ceil` + the min-1 floor mean any P≥1 keeps at least the single best candidate. Applied in `generate_shortlist(top_percent=100)`; the worker `shortlist_job` reads the job's column and forwards it.

### 3. Reverse match is NOT capped

`match_resume_to_jobs` (résumé → jobs) gains no `top_percent` parameter and is never capped: `shortlist_top_percent` is a property of a *job's* forward shortlist, and reverse match is the caller's own résumé matched across jobs — a different contract. Proven: 10 jobs all set to `shortlist_top_percent=1` still return all 10 reverse-match entries.

## Consequences

- **The default path is provably unchanged.** `ceil(N*100/100) = N`; `entries[:N] == entries`, no re-sort. The ranking-evals gate compared HEAD against a `main` baseline worktree: precision@5, gold-recall, adversarial-bait rank, ordering pairs, determinism, and PII scan all identical. A mutation (off-by-one in the slice) was confirmed to fail the cap's own unit tests while the eval corpus stayed green — the corpus is structurally blind to the cap (it drives `run_match`, not `generate_shortlist`).

### Accepted residuals

- **The eval corpus does not gate shortlist quality at P<100.** The labelled corpus exercises only the full-list default path, so if a future change made the cap drop the *wrong* candidates at P<100 (a re-sort bug, a bad floor), precision@5-style regressions *inside a truncated shortlist* would not be caught by the ranking-evals gate — only by the feature's own unit tests (`test_shortlist_top_percent_cap.py`) and integration tests (`test_shortlist_top_percent_cap_pg.py`). This is the correct division of labour (the corpus's contract is the full ranking at default settings), but whoever owns the P<100 quality contract should know the eval gate offers no signal there.
- **Cap base = the ranked candidate list**, which is the coarse-retrieval pool (≤ `match_coarse_k = 50`), i.e. effectively all of a job's résumés when it has ≤50. "Top 20%" of a 50-candidate pool = 10. For jobs with >50 résumés, the base is the coarse-50, not the full résumé count — a nuance to surface if very-high-volume jobs need true whole-pool percentages.
- **Evidence is still computed for the top `match_evidence_k=15` regardless of P** — capping the persisted shortlist below 15 simply persists fewer of those already-evidenced entries; capping above 15 persists entries whose evidence was not LLM-generated (unchanged from today's behaviour). `shortlist_top_percent` and `match_evidence_k` remain independent knobs.
- **Edit-form field deferred**: the create-job form exposes `shortlist_top_percent`; there is no job-edit form in the viewer today (only status/blind-review toggles), so PATCH-from-the-UI is deferred. The `PATCH /jobs/{id}` API already accepts it.
