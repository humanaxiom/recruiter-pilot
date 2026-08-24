# ADR-015: Evidence chunk-id expansion (FU-2)

**Status:** Accepted
**Date:** 2026-07-18

## Context

Shortlist evidence surfaces a per-requirement `evidence` quote plus `evidence_chunk_ids` (`c_001`,
`cl_001`) that anchor the quote to the résumé's parsed chunks. The ids were shown verbatim — the evidence
CSV had an `evidence_chunk_ids` column and the UI evidence panel printed `chunks: c_001, c_002`, both
opaque. A recruiter reviewing a shortlist could not see the surrounding source text a claim was drawn from
without leaving the app. User request (2026-07-18): *"Export evidence is not useful. things like chunk_id
should expand the actual content."*

The full chunk text already lives in `resumes.parsed.chunks[]` (`c_NNN`) and
`resumes.parsed.cover_letter_chunks[]` (`cl_NNN`). Critically, both the export path (`export_rows`) and the
blind read path (`_row_to_blind_entry`) **already fetch that parsed json** (as `candidate_parsed` /
`_c_parsed`) to derive redaction labels — so no new SQL join is needed. But surfacing raw chunk text is a
**new PII-exposure surface**: a header/summary chunk carries the candidate's name, and a cover-letter
(`cl_NNN`) chunk carries letterhead PII.

## Decision

1. **Resolve ids → text via a pure helper.** `_resolve_chunk_context(raw_parsed, chunk_ids)` merges
   `chunks` + `cover_letter_chunks` into one `{id: text}` map and returns the requested texts joined in
   order (deduped, unknown ids skipped, `""` when nothing resolves). Accepts parsed as dict / JSON-string
   / `None`. It does **no** redaction itself — each call site scrubs to match its reveal state.

2. **Display-only, never persisted.** A new optional `RequirementEvidence.source_context: str | None` field
   carries the resolved text on the read/export DTOs. The LLM never emits it, so at write time it is always
   `None` and persists as JSONB `null` — it is recomputed on every read/export, never stored.

3. **Redaction parity with the evidence quote — the load-bearing decision.** The resolved text is scrubbed
   with the **same `redact_text` parameters already applied to the evidence quote**:
   - **Blind read** (`_attach_source_context_model`, called from `_row_to_blind_entry`): the read path is
     unconditionally blind, so the resolved text is always scrubbed with the candidate's
     `name/email/phone`, `labels_from_parsed` employer/school aliases, and location.
   - **Anonymized export** (`_attach_export_context`): runs in `export_rows` **before** `_apply_reveal`
     swaps the real name for a pseudonym, so the real identity is still available to the redactor. Under
     `reveal=False` it scrubs with the same params; under `reveal=True` (the pre-existing, API-key-gated
     export path) full text passes.

4. **Surfaces.** The evidence CSV gains an `evidence_context` column (the `evidence_chunk_ids` column is
   kept for traceability); the UI evidence panel renders the resolved text as a collapsible under the
   `chunks:` line (autoescaped Jinja).

## Consequences

- The evidence export/panel now show the actual source passage behind each cited chunk, redacted under
  blind review / anonymized export exactly like the quote itself. Proven by black-box byte-scan tests that
  plant a candidate name/email/phone in a chunk and assert its absence from both the rendered card and the
  anonymized export, while non-identity content is present.
- Merge-blocking gates: reviewer **APPROVE**, security **PASS** (both confirmed the resolve-before-pseudonym
  ordering and that no non-blind path can populate `source_context` into a blind render). Offline gates:
  ruff · black · mypy --strict clean; **2375 unit tests @ 91.22%**.

### Accepted residual (flagged, not fixed)

Expanding the exposed text from a short quote to the **full cited chunk** widens the surface for PII
categories `redact_text` does **not** pattern-match: street addresses, profile URLs, dates of birth, and
third-party (reference/co-worker) names. Redaction **parity** with the sanctioned quote redactor is met, so
this is consistent with the project's standing posture that *blind review reduces, not guarantees,
anonymity* (ADR-011). Accepted for now; if tightening is wanted later, two options were identified:
(a) extend `redact_text` with address/URL patterns, or (b) cap `source_context` to the sentence(s) around
the quote rather than the whole chunk. Revisit alongside the at-rest-PII hardening before any multi-tenant
deploy.

### Non-blind detail note

`_row_to_entry` (the non-blind read) does not populate `source_context` — the viewer is blind-only today,
so there is no unblinded surface to show it on. When the audited-reveal feature (FU-1, ADR-016) lands, the
reveal path will need to populate `source_context` with **unredacted** chunk text for a deliberately
revealed candidate.
