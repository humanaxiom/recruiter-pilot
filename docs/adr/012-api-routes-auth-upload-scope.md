# ADR-012: API Routes — Configurable Auth, Upload Scope, and the First Status Transition

**Status:** Accepted (closes ADR-006 §4's `JobOut.blind_review` fail-open note by enforcing it in the
route layer; consumes ADR-007/ADR-010/ADR-011's read/write/redaction layers over HTTP for the first time;
touches no ranking-scoring code — `stages.py`/`orchestrator.py` are untouched by this phase)
**Date:** 2026-07-16

## Context

Phase 6 ships the HTTP surface over the service layer Phases 3–5 built: job create/read/list/status,
résumé upload/read/list, shortlist generate/list/get/export, and reverse-match trigger/read. This is the
first phase where any of that code runs behind a real ASGI request — every prior phase exercised its
service functions directly from tests.

Built on branch `feat/phase-6-api-routes`, off `main` @ `6deade3`, HEAD `837de9e`. Commit chain: RED
`209bff7` → GREEN `bc9a3d6` (initial routes) → RED `1f2b161` → GREEN `344f6bf` (SEC-1/SEC-2/SEC-4 security
hardening + exact dependency pins) → RED `c75f4a7` → GREEN `837de9e` (non-ASCII API-key 401 fix +
upload-ordering regression pin). All three merge-blocking gates are green on `837de9e` (reviewer APPROVE,
security PASS, ranking-evals PASS). **Phase 6 is gate-green and pre-PR — a PR opens after a human
check-in; CI (`gates-all`, including a live `run_evals.py` re-measurement) has not run on this branch.**
Do not read this ADR as recording a merged state.

## Decision

### 1. Configurable auth switch — one settings flag, fail-closed when set

`Settings.api_key: str = ""` is the single switch. Empty (the default) disables auth entirely — every
route is unauthenticated, which is correct for local-only offline dev, and `log_auth_mode` (called once
in the API lifespan) logs a loud `WARNING` so a misconfigured deploy is impossible to miss in the logs.
Non-empty enables auth fail-closed: `require_api_key` (`core/src/api/deps.py`), applied at
`APIRouter(dependencies=[...])` level on every Phase-6 router so a future route added to an existing
router cannot forget it, requires an exact `X-API-Key` header match or raises 401.

The comparison is `secrets.compare_digest` on UTF-8-encoded bytes, not `str` — two deliberate reasons:
constant-time (so response timing cannot leak the key byte-by-byte), and `compare_digest` itself requires
ASCII-only `str` input and raises `TypeError` on anything else. Starlette latin-1-decodes header bytes, so
a non-ASCII `X-API-Key` header is a syntactically valid (non-ASCII) Python `str` — without the
UTF-8-encode step this would crash the dependency into an unhandled 500 instead of failing closed with
401 (closed in the branch's second RED→GREEN cycle, commit `c75f4a7`→`837de9e`; see §4).

An optional `X-Actor-Name` header (`resolve_actor`, capped at 128 characters — SEC-4, an
attacker-controlled string that must not be allowed to overflow the nullable TEXT `created_by`/
`uploaded_by` audit columns) populates those columns; the fixed default is `"api"` when the header is
absent.

### 2. Upload scope — local multi-file + zip only; Taleo/manifest connector CUT

`POST /jobs/{job_id}/resumes` accepts a multipart batch of résumé files, individually or as a single
`.zip` entry that is expanded (each archive member becomes one résumé) and merged into the same
accepted/rejected accounting as a raw multi-file batch. **Taleo / CSV-JSON-manifest source pairing is
explicitly deferred**, not ported: the human's framing this phase was "Taleo was a shortcut to get sample
data … will add more connectors in the future." A future "sources/connectors" feature is the right home
for it, not Phase 6.

`core/src/services/zip_upload.py::expand_zip_entries` is the guard, mirroring the Phase-3 DOCX
decompression-bomb defense (`extract.py`'s `_enforce_docx_decompression_cap`) rather than inventing a new
pattern:

- Path-traversal entry names rejected (`..` segments, absolute paths, Windows-drive prefixes) before any
  bytes are read.
- Never trusts `ZipInfo.file_size` (attacker-forgeable) — every entry is streamed in 1 MiB chunks and its
  *real* decompressed byte count is what the per-entry (10 MB) and running-total (100 MB) caps check.
- A 50-entry cap, checked before any entry is opened.
- An extension allowlist (`pdf`/`docx`/`rtf`/`txt`) — rejects a nested zip and any non-résumé shape.
- A pure function — no `BlobStore`/DB parameter — so it is architecturally incapable of writing anything
  to disk on either the accept or the reject path; one bad entry poisons the whole archive, nothing
  partially succeeds.

The route-level file-count cap (`_MAX_UPLOAD_FILES`, same value as the zip entry cap, 50) is checked
**before any file body is read into memory** — SEC-2, closed in the branch's second RED→GREEN cycle and
regression-pinned by an ordering test (the reject must fire before any `UploadFile.read()`).

### 3. `PATCH /jobs/{id}/status` — the only status-mutating route, forward-only

`patch_job_status` (`core/src/api/routes/jobs.py`) is the first code path in the whole repo that
transitions `jobs.status` — through all of Phases 0–5, `status` was written once at creation and never
moved. `job_service.transition_status` enforces a forward-only state graph; an invalid transition (a
backward move, a same-state no-op, skipping a state) raises `ValueError`, caught at the route and
re-raised as a business-rule 409 — deliberately distinct from the 422 a syntactically-invalid `JobStatus`
enum member already gets from pydantic.

This reopens, but does not resolve, ADR-010 §4's note: `reverse_match_job`'s `allowed_job_ids` filter is
still `description_parsed IS NOT NULL`, not `status = 'open'`. A status route now exists, so filtering on
`status='open'` is no longer structurally impossible — but changing the filter is out of this phase's
scope (no `stages.py`/`orchestrator.py`/`matching_tasks.py` diff shipped here) and is left as the next
natural follow-up, not silently done.

### 4. Reverse-match is a subresource of `routes/resumes.py`; no redaction on its read

`POST /resumes/{id}/match-jobs` (trigger — a lightweight existence probe before enqueueing, not a full
`resume_service.get_one` decrypt; a nonexistent id 404s and nothing is enqueued) and `GET
/resumes/{id}/match-results` (read, via `shortlist_service.get_reverse_match_result`) live in
`routes/resumes.py`, not a standalone `routes/matching.py` — the plan-of-record default, since the
resource being matched *from* is the résumé.

**Explicit decision, not silent inheritance: the reverse-match read applies NO redaction.** Every other
blind-review-aware read path in this repo (ADR-011 §1 — `resume_service.get_one`,
`shortlist_service.list_for_job`/`get_one`/`export_rows`) redacts because the *caller* is a third party
reviewing a candidate they don't already know. Reverse-match inverts that relationship: the caller is
matching a résumé they already possess against the job pool, so there is no third party to protect from
their own document's contents. Recording this here so a future audit does not read the absence of
redaction on this one path as an oversight.

### 5. `POST /jobs/jd-extract` — pre-fill helper, no DB write

Calls `jd_import_service.extract_jd_text` (ported in Phase 3's scope note, wired to a route only now) to
pull plain text out of an uploaded txt/json/pdf/docx so a recruiter can review/edit before `POST /jobs`
actually creates anything. Declared before `GET /jobs/{job_id}` in the router so the literal path segment
`jd-extract` can never be swallowed by the `{job_id}` path parameter.

## Carry-forwards now CLOSED

### `JobOut.blind_review` fail-open (ADR-006 §4 note) — CLOSED

ADR-006 flagged that `JobOut.blind_review` defaults to `False` (fail-open) in the schema, so any route
that omits it from the constructor call would silently under-protect a job that should be blind by
default. `job_service._row_to_jobout` (Phase 6) sets `blind_review=raw["blind_review"]` explicitly from
the row on every construction path — reviewer mutation-proved both directions (omitting the explicit set
regresses a fail-open job to visibly-blind; flipping the column value is reflected in the DTO).

### Redaction boundary at the HTTP layer (ADR-006 §4, extended by ADR-011) — CLOSED

ADR-011 closed the redaction-boundary contract in the *service* layer (redact-before-DTO-construction).
Phase 6 is the first phase that puts an HTTP response on the other end of that boundary — `GET
/resumes/{id}`, `GET /jobs/{id}/shortlist`, `GET /jobs/{id}/shortlist/export` all route straight through
to the already-redacting service functions with no re-query of raw rows in between. The security gate
byte-scanned actual serialized HTTP responses (not just service-layer return values) and confirmed no raw
PII byte-sequence appears in a blind response — the same guard class ADR-011 §1 used, now proven one layer
further out.

## Security hardening + accepted residuals

**Fixed this phase:**

- **SEC-1** — non-ASCII `X-API-Key` crashed `require_api_key` into an unhandled 500 instead of failing
  closed with 401 (§1 above; `secrets.compare_digest` requires ASCII `str`, header value is a valid
  non-ASCII `str` under Starlette's latin-1 header decode). Fixed by comparing UTF-8-encoded bytes.
- **SEC-2** — the upload file-count cap (50) was checked after some file bodies had already been read.
  Fixed to check `len(files) > _MAX_UPLOAD_FILES` first, before any `UploadFile.read()`; regression-pinned
  so the ordering cannot silently regress.
- **SEC-4** — `X-Actor-Name` was unbounded, risking an oversized value against the nullable TEXT
  `created_by`/`uploaded_by` columns. Capped at 128 characters in `resolve_actor`.
- **Exact pins** — `fastapi==0.139.2`, `starlette==1.3.1`, `python-multipart==0.0.32` pinned `==`, not a
  floor, honoring the repo's standing "pin formatters/deps exactly" lesson (the CI-vs-local-container
  drift that hit `ruff`/`black` in Phase 3): the repo's own route-walker test (`test_api.py`) depends on a
  FastAPI-internal structure that is not part of its public API contract, so an unpinned minor bump could
  silently break the gate on a different machine.

**Accepted-for-v1 residuals (non-blocking, recorded not fixed):**

- **SEC-3** — no `LIMIT`/`OFFSET` on the shortlist list/export/reverse-match-result reads. Accepted:
  bounded in practice by shortlist size (stage 1's `k=50` oversample caps the row count structurally),
  revisit if shortlist size assumptions change.
- **SEC-5** — `detect_mime`'s `txt` catch-all (any unrecognized extension/content falls through to plain
  text) is intentional, not a gap — carried from Phase 3, restated here because Phase 6's upload route is
  the first HTTP-reachable caller of it.
- **Blob-write-inside-transaction** — an upload's blob write happens inside the same DB transaction as the
  `resumes` row insert. A transaction rollback (e.g. a later row in the same batch fails a constraint)
  leaves a uuid-keyed orphan blob on disk — harmless wasted bytes, no orphaned enqueue (since
  `parse_resume` is only enqueued after the whole upload transaction commits, per `routes/resumes.py`).

## `pool.py` latent-bug fix

`PoolConnectionProxy[Record]` is generic only in the `asyncpg` type stubs — subscripting the *real*
runtime class raises. `models/pool.py` uses `from __future__ import annotations` (PEP 563 — every
annotation is stored as a string), and FastAPI's dependant-graph builder introspects `get_db` (as a
`Depends(get_db)` sub-dependency of `Db`) with `inspect.signature(eval_str=True)`, which `eval()`s that
annotation string at route-registration time. A real `PoolConnectionProxy[Record]` reference anywhere in
`get_db`'s signature therefore blows up the *first time any route actually depends on `Db`* — which was
never true before Phase 6 (Phases 0–5 called service functions directly in tests, never through a live
FastAPI dependency graph). Fixed with a `TYPE_CHECKING`-gated alias: `_ConnT` is the real parametrised type
under `TYPE_CHECKING` (so mypy --strict sees the precise type) and `Any` at runtime (what the `eval()`
actually resolves to; mypy never evaluates that branch). This was a **latent** bug from the moment
`from __future__ import annotations` and the generic-stub-only class first coexisted in the file — Phase 6
is simply the first phase that exercises the code path that triggers it.

## Consequences

- Auth is off by default in every environment until `API_KEY` is explicitly set — correct for the
  offline-first local-dev posture this repo targets, but means a deployment that forgets to set it is
  silently open. `log_auth_mode`'s loud startup WARNING is the only safety net; there is no separate
  "auth required" health-check or CI gate that would catch a forgotten `API_KEY` in a real deployment.
- The Taleo/manifest connector cut means there is currently no bulk-import path beyond zip; anyone wanting
  to seed a large résumé corpus must either multi-file-upload or zip it client-side. Acceptable for v1;
  revisit when a connectors feature is scoped.
- The reverse-match read's no-redaction decision (§4) means `GET /resumes/{id}/match-results` is the one
  read path in the whole API that is NOT blind-review-aware by design — a future reviewer auditing "does
  every read redact under blind review" must know this is deliberate, not missed.
- `jobs.status` can now move past `draft`, which is a precondition several future features (a
  user-facing shortlist regenerate route, `status='open'`-scoped reverse-match filtering) need but this
  phase does not itself build.

## Alternatives Considered

- **Per-route auth dependency instead of router-level** — rejected: router-level
  `APIRouter(dependencies=[Depends(require_api_key)])` guarantees a route added later to an existing
  router cannot forget the dependency; a per-route decorator is exactly the kind of thing a future PR
  silently omits.
- **Port Taleo/CSV-manifest pairing now, since hris already has it** — rejected per the human's explicit
  framing this phase (Taleo was scaffolding for sample data, not a v1 requirement); scoping it into Phase
  6 would also pull in source-provenance columns (`JobListItem.source`/`external_last_seen_at`) that
  Phase 2 already deliberately cut (ADR-006).
- **Redact the reverse-match read for consistency with every other read path** — rejected (§4): redaction
  exists to protect a third party from a *reviewer's* view of a candidate; there is no third party in the
  reverse-match relationship, and redacting it would strip the caller's own information from their own
  request for no privacy benefit.
- **`status='open'`-scope `reverse_match_job`'s `allowed_job_ids` now that a status route exists** —
  considered, deferred: doing so is a change to `matching_tasks.py`, outside this phase's route-only diff,
  and ADR-010 §4 already names it as the natural follow-up once a status route lands. Bundling it here
  would mix a scoring-adjacent change into a routes-only PR.
- **`PoolConnectionProxy[Record]` fixed by removing `from __future__ import annotations` instead of a
  `TYPE_CHECKING` alias** — rejected: the whole codebase uses PEP 563 consistently (every other module),
  and removing it from just this one file would be a style inconsistency for a runtime-vs-stub mismatch
  that the alias pattern (already used for `BlobNotFound`/`InvalidBlobKey`-style pinned-name waivers
  elsewhere in this repo) solves without touching the rest of the module.
