# ADR-017: Bulk ingest — per-résumé cover-letter pairing + bulk JD upload (FU-3)

**Status:** Accepted
**Date:** 2026-07-18

## Context

Post-v1 user request (2026-07-18): recruiters need to ingest candidates and jobs **in bulk**, offline.
Concretely: (1) upload many résumés at once, each paired with its OWN cover letter (a cover letter is an
optional "intention/motivation" bonus, not shared across the batch); (2) upload many job descriptions at
once, one job per file; (3) navigate candidate↔job both ways; and (4) fix a UX defect found in live
testing where the shortlist "Generating…" poll never stopped. This is the **offline** half of the old
"connectors" concept — the Taleo *job-source scraper* remains separately deferred (ADR-012 §2).

Prior art was ported from `C:\repos\hris\apps\api\src\api\services\bulk_ingest_service.py`
(`pair_applicants`/`_classify`/`parse_pairing_manifest`/`parse_csv_manifest`/`title_from_filename`),
adapted to this repo's `(filename, bytes)` upload representation and its hardened
`zip_upload.expand_zip_entries` (rather than porting hris's weaker `expand_archive`).

## Decision

Delivered as five independently-gated slices on `feat/fu3-bulk-ingest`:

1. **Shortlist-poll UX fix.** The `shortlist_cards` poll now carries a server-clamped `attempt` counter and
   stops at `_MAX_SHORTLIST_POLL_ATTEMPTS` (~20 min) with a give-up message; the **Generate button is
   disabled until ≥1 résumé is `parsed`** (the primary fix — ranking a job with no parsed résumé was what
   produced the endless poll). A parse-time hint ("~1–2 min per large PDF on the local model") was added so
   the inherent LLM latency isn't mistaken for a hang.
2. **Per-résumé cover-letter pairing (filename convention).** New pure, I/O-free
   `core/src/services/bulk_ingest_service.py` (`pair_applicants`/`_classify`/`ApplicantFiles`/
   `PairingResult`). Résumé/cover files are paired by suffix convention (`_resume`/`_cv` ↔
   `_cover_letter`/`_coverletter`/`_cover_note`/`_cover`, longest-first, case-insensitive). Each résumé is
   stored with its OWN `cover_letter_blob_key`. `resume_service.upload_resumes` grew additive optional
   `cover_letter_map`/`warnings_map` params (old callers untouched). A post-upload **results summary**
   ("N accepted (M with a cover letter), K duplicate, J rejected") is shown.
3. **Manifest-driven pairing.** `parse_pairing_manifest` + `ManifestError` (an `AppError`, 422). A
   `pairing_manifest` upload field maps résumé→cover by name; the manifest takes precedence, and files it
   doesn't name fall back to convention.
4. **Bulk JD upload.** `POST /jobs/bulk` → `job_service.create_jobs_bulk`: one job per file (loose or
   `.zip`), optional CSV metadata manifest (`parse_csv_manifest`), with per-file resilience and dedup.
5. **Reverse-match UI (candidate→jobs).** `trigger_reverse_match` + a POST-only "Find matching jobs"
   button + a bounded results poll; rows link to job detail — completing the many-to-many navigation
   (job→candidates already existed via the shortlist).

### Load-bearing decisions

- **Manifest is its own field, never zipped.** `expand_zip_entries`'s allowlist has no `json`/`csv`, so a
  manifest zipped with the résumés trips the allowlist and the whole zip 400s with guidance to upload it
  separately. This keeps the zip threat surface unchanged rather than widening it for a manifest.
- **`expand_zip_entries(allowed_extensions=…)` is an ADDITIVE kwarg.** Default preserves the résumé
  allowlist `{pdf,docx,rtf,txt}` exactly (résumé call site untouched); bulk-JD passes `{pdf,docx,txt,json}`
  through the SAME hardened expander (no second/weaker zip impl).
- **Ambiguity is a 422, never a silent choice.** Supplying BOTH per-résumé pairing AND the singular
  batch `cover_letter_file`/`cover_letter_text` → 422. A plain no-suffix upload classifies every file as a
  résumé with no cover → empty map → the singular path is fully backward-compatible.
- **Nothing is silently dropped.** An orphan cover-named file demotes to a standalone résumé with a
  (static-English) note; a manifest-named-but-absent résumé/cover surfaces as a `rejected` row / note.
- **Bulk-JD batch resilience.** A <50-char extracted JD → `outcome="failed"` (not a 422 aborting the
  batch); a manifest field that fails validation → `failed`; in-batch dedup stops two identical JDs in one
  request from both inserting.
- **Bounded polls + POST-only triggers.** Both the shortlist and reverse-match polls clamp `attempt`
  server-side and stop at the cap; the reverse-match trigger is POST-only (a side-effecting enqueue must
  not be a prefetchable GET).

### New convention: idempotent `ALTER TABLE`

Jobs gained a `description_sha256 TEXT` dedup column. Because `CREATE TABLE IF NOT EXISTS` is a **no-op**
against an already-existing dev/CI Postgres volume, the column is added in BOTH the `CREATE TABLE jobs`
block (fresh DBs) AND a separate idempotent `ALTER TABLE jobs ADD COLUMN IF NOT EXISTS description_sha256
TEXT` (existing volumes), plus a partial dedup index. **This is the port's first use of `ALTER`** — a new
convention for adding columns to an existing table without a migration framework.

## Consequences

- Recruiters can bulk-load candidates (with per-applicant cover letters via convention or manifest) and
  bulk-create jobs, offline. Live-verified against the `hris/fixtures/llm_split` sample PDFs: the
  `004_ayomide_abass` résumé+cover pair matched by convention ("2 accepted (1 with a cover letter)"), a
  manifest paired files by name, and 3 JD files → 2 jobs created + 1 deduped.
- Gates: **reviewer APPROVE, security PASS, ranking-evals PASS** (scoring code byte-identical to `main`;
  corpus green: precision@5=1.0, evidence 1.0, 0 PII leaks, exact determinism). ruff/black/mypy --strict
  clean; **~2527 unit tests @ 91.36%**. The one merge-blocking-adjacent finding (both gates flagged it) —
  the `/jobs/bulk` route lacked the résumé route's file-count cap — was fixed (reject on count before any
  body is read).

### Accepted residuals

- **Global job dedup.** `description_sha256` dedup is cross-job (jobs have no parent aggregate to scope it
  to, unlike résumés' per-`(job_id, sha256)`). A byte-identical JD anywhere → `duplicate`. Deliberate.
- **Manifest-in-zip is rejected, not merged.** By design (see above) — the recruiter must upload the
  manifest as its own field.
- **Reverse-match shows real job titles (no redaction).** Intentional (ADR-012 §4) — the caller owns the
  résumé and jobs aren't candidate PII. No résumé PII is on that path.
- **Consent stays batch-level** (one `consent_acknowledged` per upload request), unchanged from Phase 6 —
  per-résumé consent was not requested.

## Amendment 2026-07-29 — separator-agnostic pairing + ReDoS guard (branch `fix/cover-letter-pairing-separators`)

The filename-convention pairing (decision 2) originally recognized only **underscore**-joined suffixes
(`str.endswith` on `_cover_letter`/`_cover`/`_resume`/`_cv`). Real-world uploads use spaces or dashes —
`Jane Smith Cover Letter.pdf`, `jane-cover-letter.pdf` — which were NOT recognized, so a cover letter in a
zip was silently demoted to a standalone résumé and parsed **as a résumé** (the "resumes parse but not
cover letters" bug). Fixed: `_classify` now treats **space / dash / underscore as equivalent separators**
(case-insensitive) via two anchored regexes, with a normalized pairing `base` so a résumé and its cover
share a key regardless of separator style. The false-hit guards are preserved (a real separator is required
before the suffix, so `discover.pdf` stays a résumé; a non-empty name is required, so a bare `Cover.pdf` /
leading-separator stem stays a résumé).

**Security (ReDoS).** The suffix regexes have two adjacent ambiguous quantifiers → O(n²) backtracking on a
pathological, attacker-controllable filename (zip entry names can be tens of KB), and pairing runs
synchronously inside the async upload route. `_classify` now **caps the stem at 256 chars** before the
regexes (over-length names short-circuit to a plain résumé; no real name is that long) — restoring linear
behaviour. Gates green: reviewer APPROVE, security PASS, `./scripts/verify.sh all` = 3977 unit @ 92.64% +
422 integration. Scoring/ranking code untouched (ranking-evals N/A).
