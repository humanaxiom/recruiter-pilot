# ADR-016: Audited candidate reveal (FU-1)

**Status:** Accepted
**Date:** 2026-07-18

## Context

Through the v1 plan and the first post-v1 features, the frontend was **blind-only by
construction** (ADR-013/014): identity was masked everywhere and the résumé template had *no code
path* capable of rendering `candidate.*` — a property pinned by a structural source-scan test. That
was the right default, but it left recruiters unable to ever see who a candidate is, which real
hiring requires.

The user (2026-07-18) asked for the **hris model**: blind stays ON at every step by default, and
identity is exposed only through an **explicit, audited reveal** — clicking a candidate ("Candidate
A") lets you reveal the full résumé, and every reveal is recorded. RBAC (who is *permitted* to
reveal) was mandated in the early system design but never implemented; it is a **separate** task
(FU-4). This ADR ships the reveal audited-first so RBAC can layer on later without rework.

## Decision

1. **Reveal is an explicit, POST-only, audited action.**
   - New append-only `reveal_audit` table (idempotent startup DDL; never UPDATEd/DELETEd):
     `id, resume_id, job_id, actor, context, revealed_at`.
   - `reveal_service.record_reveal(...)` — a pure insert (no decryption, no read-back).
   - `POST /resumes/{id}/reveal` (under `require_api_key`): probes existence FIRST (a missing id
     404s and writes **no** audit row and never decrypts), records exactly one audit row with the
     resolved actor + optional `context`, then returns the UN-blinded `ResumeOut` via
     `resume_service.get_one(reveal=True)`.
   - The pre-existing `GET /resumes/{id}?reveal=true` is **retained** for direct API callers but is
     unaudited; the UI never uses it (see residual R2).

2. **Frontend: blind stays the default; reveal is the only path to identity.**
   - `GET /resumes/<id>` still renders blind (no `candidate.*`). A "Reveal identity (audited)"
     button in the blind banner POSTs to a new Flask `/resumes/<id>/reveal`, which calls
     `api_client.reveal_resume` (POST — **never** `get_resume(reveal=True)`, which would skip the
     audit) and re-renders the résumé un-blinded with an "Identity revealed — recorded in the audit
     log" notice. Reveal is POST-only so a URL edit / link prefetch can't trigger it.
   - The shortlist card's candidate label links to the résumé detail, where reveal lives. Shortlist
     reads themselves stay blind (no `reveal` kwarg).

3. **Actor is best-effort until RBAC.** The recorded `actor` is the optional `X-Actor-Name` header
   (or the default) — there is no per-user identity/login yet. FU-4 closes this.

## Consequences

- **The blind-only structural invariant is deliberately relaxed** — from "the template is incapable
  of rendering PII" (ADR-013) to "the template renders `candidate.*` only inside the single
  `{% if revealed %}` block, reached only via the audited reveal." This is enforced by an updated
  structural guard (every `candidate.*` reference must fall between the `revealed` gate and the
  `elif blinded` branch) plus behavioral tests: the default GET renders no identity even when fed a
  full-PII payload; the reveal POST renders it and fires the audit; the reveal never routes through
  the raw `get_resume(reveal=True)`.
- Merge-blocking gates: reviewer + security both required (this is the de-anonymization surface).
  Offline gates green (ruff/black/mypy --strict; full unit suite + coverage ≥ 80%). Live-verified
  end-to-end: default view blind, reveal shows identity + writes a `reveal_audit` row.

### Accepted residuals

- **R1 — no RBAC.** Any authenticated caller (auth is the single optional API key today) can reveal.
  Gating reveal by role is FU-4; this ADR is a prerequisite for it (reveal is the first action FU-4
  will gate).
- **R2 — the unaudited `GET /resumes/{id}?reveal=true` still exists** for direct API callers and
  writes no audit row. The UI never uses it, but a power user hitting the API directly can un-blind
  without an audit trail. Revisit alongside FU-4: either remove it, or record an audit row there too.
- **R3 — no reveal-audit viewer.** The `reveal_audit` rows are written but there is no admin/UI to
  review them yet. A "who revealed whom" report is a natural FU-4/admin follow-up.
- **R4 — FU-2 `source_context` on reveal.** The evidence source-text expansion (ADR-015) is redacted
  on the blind read path; when a résumé is revealed, its `source_context` should carry *unredacted*
  chunk text. Wiring the revealed résumé/shortlist read to populate it unredacted is a follow-up.
- **R5 — no CSRF token on the reveal POST.** The Flask viewer has no CSRF protection, so a cross-site
  auto-submitting form could force `POST /resumes/<id>/reveal` — writing a spurious `reveal_audit`
  row and un-blinding in the victim's own browser. Same-origin policy blocks the attacker from
  *reading* the response, so there is **no identity exfiltration**; the impact is audit-log noise / a
  forced local reveal, further bounded by the local, single-API-key deployment. Accepted for now; add
  a CSRF token (or `SameSite=Strict` session + origin check) when the viewer gains multi-user auth
  (FU-4).
