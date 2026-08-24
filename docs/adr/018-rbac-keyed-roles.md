# ADR-018: RBAC — Keyed Roles (FU-4)

**Status:** Accepted (closes ADR-016's R1, R2, R5 — R2's closure goes further than ADR-016 recorded,
see §6; the Flask-side R5 fix, §8, has landed (`core/frontend/csrf.py`, `08a6edf`) and was amended
twice post-landing (`b0634bd`, `ea31123`) — see §8. Touches no ranking/scoring code
(`pipeline/matching/*`, `stages.py`, `orchestrator.py`, `matching_tasks.py` are byte-unchanged) and
does not change the at-rest PII posture — ADR-007 §6/§7 and ADR-010 §6 stand exactly as recorded.)
**Date:** 2026-07-19

## Context

ADR-016 (FU-1) shipped the audited, explicit reveal action — `POST /resumes/{id}/reveal` — as the
prerequisite for role-gating identity disclosure, but recorded plainly (its R1) that RBAC itself was
a separate, not-yet-built task: "Any authenticated caller (auth is the single optional API key today)
can reveal." From Phase 6 (ADR-012) through every feature shipped since, `Settings.api_key` has been
a single flat switch: auth off (empty, every route unauthenticated) or on (any caller holding the one
key can do anything the API exposes, including reveal). RBAC — gating *who* is permitted to reveal,
write, or export — was named in the early system design but deferred until this feature.

FU-4 closes that gap on branch `feat/fu4-rbac`, off `main`. Building it surfaced two findings neither
ADR-012 nor ADR-016 recorded: an unaudited bulk de-anonymization path on the shortlist export route
(§6), and a job-update route that can permanently un-blind a whole job with no audit trail at all
(§7) — a wider blast radius than the reveal endpoint ADR-016 was built to audit.

## Decision

### 1. The role model

Four roles — `admin`, `recruiter`, `hiring_manager`, `auditor` — implemented as `Role(StrEnum)` in
`core/src/api/deps.py`, so each member IS its own wire string (`Role.ADMIN == "admin"`).

- **Key → role resolution** (`resolve_role`) reads the `X-API-Key` header and matches it against the
  four configured role keys (`src/settings.py`, §2 below). Auth is disabled iff all four are empty
  (`Settings.auth_enabled`); enabled iff any is non-empty.
- **Auth-disabled resolves to `Role.ADMIN`** unconditionally, regardless of what `X-API-Key` carries
  — the existing local-dev fail-open-by-explicit-configuration posture from ADR-012, preserved
  verbatim so nothing that works today with auth off changes behavior.
- **The 401-vs-403 split is new.** Phase 6 was 401-or-pass only (`require_api_key`, a single
  boolean dependency). FU-4 splits authentication from authorization into two primitives:
  `resolve_role` (a missing key, or one matching no configured role, raises 401 — unauthenticated)
  and `require_role(*allowed)` (a dependency *factory* that composes `resolve_role` as a
  sub-dependency and 403s a resolved role outside the route's allowed set — authenticated but not
  authorized). `require_role` is called once per route with that route's own allowed-role tuple
  (`_JOB_WRITERS`, `_RESUME_READERS`, `_REVEALERS`, etc.) rather than applied router-wide, because
  the route→role table needs different allowed sets on different routes of the same router (e.g.
  `PATCH /jobs/{id}` is admin/recruiter-only while `GET /jobs/{id}` is open to all four).
- **`X-Actor-Name` remains an unverified display label** (`resolve_actor`), populating audit columns
  only (`reveal_audit.actor`, `created_by`/`uploaded_by`). It is never read by `resolve_role` or
  `require_role` and is not, and must never become, an authorization input — stated explicitly in
  `deps.py`'s module docstring so a future change cannot fold it into the auth path by accident.

### 2. Four flat settings fields, not a dict

`Settings` gains `api_key_admin`, `api_key_recruiter`, `api_key_hiring_manager`, `api_key_auditor`
(all `str = ""`) rather than one dict- or JSON-encoded field. This matches `settings.py`'s existing
convention throughout the file — every other multi-value config surface (the match-weight tunables,
the outbox drain knobs) is a flat, individually-named field, never a nested structure — and keeps
each key settable as its own plain environment variable (`API_KEY_ADMIN=...`) with no JSON-escaping
footgun in a `.env` file or a container's env block.

### 3. The legacy `API_KEY` hard-fail

`Settings.model_config` sets `extra="ignore"`. Renaming `API_KEY` to the four new fields without
also removing the old variable from a deploy's environment would, under `extra="ignore"`, silently
drop `api_key` on the floor — `Settings.auth_enabled` never consults it — and if none of the four new
keys happen to be set either, the deployment lands in **auth-disabled** mode with no error, no
warning survivable past a glance at the logs, nothing: a fail-**open** regression from Phase 6's
already-fail-open-by-default posture, now reached by a misconfiguration instead of an explicit
choice.

`validate_startup_auth_config` (`src/settings.py`), called from the API lifespan
(`core/src/api/main.py`) before any pool/schema/graph work begins, raises `RuntimeError` naming all
four replacement env vars the moment `settings.api_key` is non-empty — regardless of whether the new
fields are also configured. **A warning was rejected**, not merely as belt-and-suspenders: a WARNING
is exactly what Phase 6's `log_auth_mode` already emits for the *legitimate* all-four-empty local-dev
case, so a stale-`API_KEY` warning would be indistinguishable in the log stream from the normal,
intended disabled-auth line — an operator scanning logs for "is auth on" has no signal that this
particular disabled state is a bug, not a choice. A hard boot failure is the only signal that cannot
be scrolled past.

### 4. The byte-identical-key collision refusal at startup

`validate_startup_auth_config` also refuses to boot when two *configured* (non-empty) role keys are
byte-identical, e.g. `API_KEY_AUDITOR` accidentally set to the same value as `API_KEY_RECRUITER`.
Two roles sharing one key silently collapse into the more privileged of the two — every caller who
authenticates as the "auditor" would in fact also pass every recruiter-only `require_role` check,
defeating the entire point of a role model without any code path ever raising, logging, or otherwise
signaling that the split had failed. Two *empty* fields are explicitly not a collision (that state is
simply "not configured," handled by §1's auth-disabled path) — the check only fires on two distinct,
non-empty env vars holding the same bytes. This mirrors the existing ADR-008 `SKILL_HASH_SALT`
startup refusal already present in `core/src/api/main.py` and `core/src/worker/main.py`: same
discipline, same "loud `RuntimeError` before any other startup work" placement, never a value logged.

### 5. Constant-time comparison across all four keys, no short-circuit

`resolve_role` compares the presented `X-API-Key` against **every** configured role key with
`secrets.compare_digest`, and does not `break`/return on the first match — the loop runs to
completion over all four candidates every time, keeping whichever match (if any) was found in a
local variable. This is deliberate: `compare_digest` is already constant-time per comparison, but
short-circuiting the *loop* itself on the first hit would let response timing (or, in principle, an
instrumented comparison counter) correlate with *which* role key configuration was closest to a
near-miss guess. Running all four comparisons unconditionally removes that channel too.

The comparison is over **UTF-8-encoded bytes**, not `str` — the same fix ADR-012 §1 shipped for
`require_api_key`'s SEC-1 regression, now re-applied here because it is a *new* function, not
inherited code: `secrets.compare_digest` requires ASCII-only `str` arguments and raises `TypeError`
on anything else, while Starlette latin-1-decodes header bytes, so a non-ASCII `X-API-Key` header is
a syntactically valid (non-ASCII) Python `str`. Without the explicit `.encode("utf-8")` step on both
sides, a non-ASCII key would crash `resolve_role` into an unhandled 500 instead of failing closed
with 401. This regression pin still holds under the new keyed-role code path, not just the retired
single-key one.

### 6. R2 closure and the bulk-export finding

ADR-016 recorded, as its own accepted residual R2, that `GET /resumes/{id}?reveal=true` was
unaudited and still reachable by a direct API caller, even though the UI never used it. FU-4 removes
the `reveal` query parameter from `GET /resumes/{resume_id}` (`core/src/api/routes/resumes.py`)
entirely — a caller appending `?reveal=true` now gets FastAPI's ordinary ignore-unknown-query-param
behavior and a fully blind response; the route has no code path left capable of un-blinding.

Planning this closure surfaced a **second, un-recorded instance of the same defect with a larger
blast radius**: `GET /jobs/{job_id}/shortlist/export` also accepted `reveal=true`, and doing so
de-anonymized **every résumé on the entire shortlist in one unaudited response** — not one candidate
at a time behind an audited POST, but a bulk export. ADR-016 never mentioned this path at all. FU-4
removes `reveal` from this route too: `export_rows` is now called with `reveal=False` hardcoded, and
the `-anon` filename suffix (previously conditional on the reveal flag) is now unconditional. In both
cases the blast radius of *actually removing* the parameter was zero in practice before this change —
`core/frontend/app.py` hardcoded `reveal=False` on the résumé-detail GET and never forwarded a
browser-supplied `reveal` to the export route either — but a direct API caller could reach both paths
unaudited, and the export path could do it for a whole shortlist in one call.

The audited `POST /resumes/{id}/reveal` (ADR-016, narrowed to admin/recruiter by `_REVEALERS` here)
is now the **only** un-blinding path anywhere in the system. If a bulk-reveal export becomes a real
product need later, it gets its own new, audited, POST route — not a parameter re-added to either
GET.

### 7. The `PATCH /jobs/{id}` finding (D5)

The most significant finding of this feature. `JobUpdate` accepts `blind_review: bool`, and every
redaction key in the service layer gates off `jobs.blind_review`
(`core/src/services/resume_service.py`) — not off any per-request `reveal` flag. Before FU-4, `PATCH
/jobs/{job_id}` sat behind the same single `require_api_key` boolean as every other Phase-6 route, so
**any authenticated caller could send `{"blind_review": false}` and permanently un-blind every résumé
and every shortlist entry under that job for every future reader — with no audit row written
anywhere.** This is a wider blast radius than the reveal endpoint ADR-016 built an entire audit table
for: one PATCH call de-anonymizes a whole job's candidate pool, permanently, silently, and it was
never recorded as a residual in ADR-016 or flagged in ADR-012.

Redaction in this codebase has **two independent triggers** — the per-request `reveal` flag (now
gone entirely per §6) and the `jobs.blind_review` column — and prior to this ADR only the first ever
routed through an audit sink. `PATCH /jobs/{job_id}` is now restricted to admin/recruiter via
`_JOB_WRITERS` (`core/src/api/routes/jobs.py`), the same allowed set as job creation and every other
job-mutating route. This closes the *authorization* gap; it does not add an audit row to a
`blind_review` flip — that remains an accepted gap for a future feature (see Consequences).

### 8. R5 / CSRF

Classic CSRF **does not apply to the FastAPI backend** under header-based auth, and this is true as
a *consequence* of enabling auth at all, not as a fix this ADR ships: a cross-origin `<form>` submit
cannot attach a custom `X-API-Key` header, so a forged cross-site POST straight at any backend route
is simply rejected as unauthenticated (401) the moment auth is enabled. Half of ADR-016's R5 is
closed by §1–§5 above with no CSRF-specific code required.

The real exposure is the **Flask hop**. The browser supplies no credential of its own for `POST
/resumes/<id>/reveal` (`core/frontend/app.py`) — Flask attaches its own server-held recruiter key on
the *outbound* leg to the backend (`api_client.build_client`, §9) — so Flask itself cannot
distinguish a forged cross-site auto-submitting form from a real click; both arrive at the Flask
route as an ordinary authenticated-by-Flask POST. The fix, `core/frontend/csrf.py`, is a
**session-bound, one-shot anti-forgery token, scoped per résumé id**, stored in Flask's existing
signed session (already in use for `flash()`), issued on `GET /resumes/<id>` (and on each shortlist
card render) and rendered as a hidden `csrf_token` input in the reveal form, checked on `POST
/resumes/<id>/reveal` before `api_client.reveal_resume` is ever called. The token carries no
identity — it is not a login, not an authorization input, and does not touch the backend's role model
at all; it only proves the POST originated from a page the same Flask session actually rendered. It
landed in `08a6edf` and was amended twice on the same branch before merge, both amendments driven by
defects the first cut didn't anticipate.

**Per-résumé scoping (`b0634bd`), not per-session.** The first cut stored one bare token under a
single session key. The FU-1 reveal button appears on *every* shortlist card, all posting to the same
`resume_reveal` route: minting a token for one card silently invalidated every other card's
already-rendered token (only the most-recently-issued token was ever valid, session-wide), so only
the first reveal a recruiter clicked worked — every other card 403'd until a full page reload
re-minted a fresh single token. This was a functional regression on the primary FU-1 workflow, not a
cosmetic one, and the HTMX poll that would otherwise mint a fresh token stops once ranked shortlist
entries exist — exactly the point at which users start clicking reveal. The fix scopes the session
mapping by résumé id: `SESSION_KEY` now holds a `<mapping-key> -> <token>` dict, one entry per résumé,
each independently one-shot. `verify_and_consume` pops *only* that résumé's entry unconditionally
(matched or not) — an anti-oracle property: a wrong-token guess burns that résumé's slot exactly like
a correct one, so a forged attempt cannot be replayed against a still-live slot, and a misdirected
attempt (résumé A's token posted against résumé B) fails without disturbing A's own entry.

**The cookie byte budget (`ea31123`).** Amendment 1's first cut keyed the per-résumé mapping by the
raw ~36-char résumé UUID and used a full `secrets.token_urlsafe(32)` (43-char) token — measured
against the real itsdangerous-signed session serializer, ~85 bytes/entry. Random tokens do not
compress (itsdangerous' zlib step buys nothing on high-entropy bytes, and base64 adds a third on top
of the raw 32 bytes), so a full `MAX_TOKENS_PER_SESSION = 64` mapping serialized to ~5.2 KB — over the
~4093-byte ceiling most browsers silently enforce on a single cookie (measured: ~4,090 B already at 49
entries, over at 50). **Browsers do not error on an oversized cookie — they silently drop it**, which
empties the whole session and 403s every subsequent reveal: the exact per-session-single-token
regression Amendment 1 exists to fix, re-triggered at full shortlist size, since shortlist rows are
structurally capped at the stage-1 `k=50` oversample (ADR-012 SEC-3) and a full shortlist render was
exactly the scenario that overflowed. The original cap was chosen by reasoning about entropy, not by
measuring the serialized cookie — that reasoning was correct about the security property (128 bits is
ample) and silently wrong about the size budget. **The lesson for the next person who changes the
token size or the cap: measure the actual signed cookie, don't re-derive an estimate.**

The fix shrinks each entry instead of the cap: the token is `secrets.token_urlsafe(16)` (22 chars,
still 128 bits — unchanged entropy, half the bytes), and the mapping key is the first 12 hex
characters of `hashlib.sha256(str(resume_id).encode()).hexdigest()` (48 bits) instead of the raw UUID
string. This drops each entry to ~38 bytes; **measured at `MAX_TOKENS_PER_SESSION = 64` the real
signed cookie now serializes to ~2,440 B**, comfortably under the ~4093-byte ceiling. A regression
test, `test_serialized_session_cookie_stays_under_the_4093_byte_ceiling_at_cap`
(`core/tests/unit/test_frontend_csrf.py`), fills the mapping to the cap via the real `issue_token` and
measures the actual itsdangerous-signed cookie value Flask would emit — not a re-derived estimate —
and pins it under 4093 B (with a tighter 3500 B check that would catch a partial shrink, e.g. only the
token or only the key shrinking).

Truncating the mapping key to 12 hex characters (48 bits) introduces an accepted collision surface: at
`MAX_TOKENS_PER_SESSION = 64` concurrent entries the birthday-bound collision probability is
~4.5×10⁻¹³. Not defended against — doing so would require a reverse mapping back to the original
résumé id, defeating the point of hashing at all — but pinned by test
(`test_forced_key_collision_does_not_duplicate_the_mapping_entry`,
`test_forced_key_collision_invalidates_the_earlier_resumes_stale_token`) to degrade gracefully: a
collision silently overwrites the earlier entry, exactly as if that résumé's own token had been
re-issued, never duplicates an entry or grows the mapping unboundedly, and never corrupts other
entries.

`MAX_TOKENS_PER_SESSION = 64`, unchanged by either amendment — 64 clears a full shortlist render with
headroom for a couple of other open résumé tabs in the same browser session. Eviction on overflow is
strict FIFO by issue order (the oldest entry is dropped first; re-issuing an existing résumé's token
keeps that résumé's original position rather than promoting it).

An `Origin`/`Referer` `same_origin` check is layered on top as defense-in-depth, evaluated
independently of the token (checked first in the route, `abort(403)` before `verify_and_consume` is
even called) — never the primary control, since the token alone is sufficient. It prefers `Origin`
over `Referer` when both are present, and blocks only when a cross-origin header is actually present;
an absent `Origin`/`Referer` is not a block.

## Consequences

- Every route in `core/src/api/routes/{jobs,resumes,shortlist}.py` now names its own allowed-role
  tuple explicitly (`_JOB_WRITERS`/`_JOB_READERS`, `_RESUME_WRITERS`/`_RESUME_READERS`/`_REVEALERS`,
  `_SHORTLIST_WRITERS`/`_SHORTLIST_READERS`) rather than inheriting one router-wide dependency — a
  route added later to an existing router cannot silently forget to specify a role set, but it also
  means each new route's author must consciously choose the right tuple; there is no structural
  guard forcing the "narrowest set that still works" choice the way router-level auth forced "some
  auth."
- The full route → role table (unchanged from FU-4's locked design):

  | Route | Method | Roles |
  |---|---|---|
  | `/health` | GET | unauthenticated |
  | `/jobs` | POST | admin, recruiter |
  | `/jobs/jd-extract` | POST | admin, recruiter |
  | `/jobs/bulk` | POST | admin, recruiter |
  | `/jobs` | GET | all four |
  | `/jobs/{id}` | GET | all four |
  | `/jobs/{id}` | PATCH | admin, recruiter (§7) |
  | `/jobs/{id}/status` | PATCH | admin, recruiter |
  | `/jobs/{id}/resumes` | POST | admin, recruiter |
  | `/jobs/{id}/resumes` | GET | all four |
  | `/resumes/{id}` | GET | all four (blind only — `reveal` param removed, §6) |
  | `/resumes/{id}/reveal` | POST | admin, recruiter only |
  | `/resumes/{id}/match-jobs` | POST | admin, recruiter |
  | `/resumes/{id}/match-results` | GET | admin, recruiter |
  | `/jobs/{id}/shortlist` | POST | admin, recruiter |
  | `/jobs/{id}/shortlist` | GET | all four |
  | `/jobs/{id}/shortlist/export` | GET | all four (blind only — `reveal` param removed, §6) |
  | `/shortlist/{entry_id}` | GET | all four |

### Accepted residuals

- **Roles are role-level, not row-level.** There is no `jobs.hiring_manager_id`/owner column, and
  adding one is out of scope for this feature (no Postgres user table exists to key it against). A
  `hiring_manager` or `auditor` key therefore grants read access to **every** job, résumé, and
  shortlist company-wide — there is no notion of "this hiring manager's own requisitions only."
  Accepted explicitly for FU-4 v1, not a gap to be discovered later.
- **The Flask viewer presents one fixed role key outbound for every browser it serves.**
  `api_client.build_client` attaches `settings.api_key_recruiter` (if set) to every request Flask
  makes to the backend, regardless of which human is sitting at the browser — the frontend is
  effectively a single-role client, and a browser-side user has no distinct identity at the backend
  at all. Recruiter was chosen as that one role because it is the narrowest role that still supports
  every workflow the viewer exposes (job/résumé writes, the audited reveal) without granting admin.
  This also degrades reveal-audit attribution: Flask never sends `X-Actor-Name` on its outbound
  requests, so every browser-originated reveal writes `reveal_audit.actor = "api"`, identifying
  neither the human who clicked reveal nor even a distinct client. Pre-existing from ADR-016, not
  worsened by this feature, but previously unmentioned as a residual.
- **`auditor` has no capability of its own yet.** It receives exactly the same blind reads as
  `hiring_manager` — read-only, no reveal, no writes. Revisit once a `reveal_audit`-viewing endpoint
  exists (ADR-016's own R3, still open) for the auditor role to actually audit against.
- **`GET /resumes/{id}/match-results` and `POST /resumes/{id}/match-jobs` are admin/recruiter**,
  narrower than the general "all four roles get blind reads" rule applied everywhere else. This is
  deliberate, not an oversight: ADR-012 §4 already recorded that the reverse-match read applies **no
  redaction at all** (the caller is presumed to already possess the résumé they're matching from, so
  there is no third party to protect), which means it is the one read path in the API that is not
  blind-review-aware — keeping it behind the narrower writer set is the compensating control for that
  standing decision, not a new restriction invented here.
- **CSRF tokens live in a client-side signed cookie, capping concurrent reveal targets per session at
  `MAX_TOKENS_PER_SESSION = 64`** (§8). A user with more than 64 résumés' reveal forms open at once
  across tabs in one browser session loses the oldest tokens to FIFO eviction and must re-render that
  page to reveal it — a full shortlist render (≤50 rows, ADR-012 SEC-3) always fits with headroom, so
  this only bites a user deliberately keeping many résumé-detail tabs open simultaneously. A
  server-side (Redis-backed) token store was considered as the alternative that removes the cap
  entirely and deferred as out of FU-4's scope (see Alternatives Considered).
- **CSRF's FIFO eviction is a targetable nuisance, not a bypass.** A cross-site `<img>`/iframe GET
  forced against `/resumes/<id>` mints a fresh per-résumé token in the victim's own session (token
  issuance has no side effect beyond that) and, at the 64-entry cap, evicts the victim's oldest live
  token to make room — which can 403 a subsequent genuine reveal attempt until the page is reloaded
  and a new token is minted. This fails **closed**: the forged request never reveals anything itself
  and cannot forge a valid token for the attacker to replay, it can only deny a legitimate reveal the
  victim was about to make.
- **`shortlist_service.export_rows`'s `reveal=True` branch (and the `reveal=True` branches it feeds
  in `_apply_reveal`/`_attach_export_context`) has no production caller.** §6's route-level removal of
  the export route's `reveal` query param (bulk exports are now blind-only, hardcoded `reveal=False`)
  left the service-layer parameter and its un-blind code paths reachable only from unit tests. The
  parameter is deliberately kept, not deleted — it is the un-exercised capability a future audited
  bulk-export route (its own POST route with its own reveal-audit trail, per the note in §6) would call
  into — so this is retained-but-currently-dead production capability, not dead code to be pruned.
- **A `blind_review` flip is unaudited.** Noted in this ADR's §7 prose; filed here as a tracked residual so it is not lost. One PATCH permanently un-blinds every résumé and shortlist entry under that job for every future reader, with no audit row, while a single reveal writes one. Deferred to FU-5 / ADR-019's generalized `audit_log`.
- **The shipped compose runs auth-disabled.** None of `API_KEY_ADMIN`/`API_KEY_RECRUITER`/`API_KEY_HIRING_MANAGER`/`API_KEY_AUDITOR` appear in `docker-compose.yml` or `compose.live-eval.yml`, and auth is disabled iff all four are empty (`core/src/settings.py:195-202`), in which case every caller resolves to `Role.ADMIN` (`core/src/api/deps.py:83-84`). The fail-open default is therefore the *shipped* default, not merely a possible misconfiguration. No `MATCH_*` tunable is plumbed into either compose file either, so the documented ranking knobs are unreachable in the running containers. Deferred to the FU-7 config-plumbing chore.

### ADR-016 residuals closed by this feature

- **R1 — no RBAC.** Closed: reveal is now `_REVEALERS` (admin/recruiter) only, enforced by
  `require_role` before the existence probe, the audit write, or any decryption runs (§1).
- **R2 — the unaudited `GET /resumes/{id}?reveal=true`.** Closed, and closed **further than
  ADR-016 described**: ADR-016's R2 named only the résumé-detail GET; building this feature also
  found and closed the same defect, at a larger blast radius, on `GET
  /jobs/{id}/shortlist/export` (§6).
- **R5 — no CSRF token on the reveal POST.** Closed (§8): the backend half is closed as a structural
  consequence of enabling keyed-role auth, the Flask half by the per-résumé, session-bound one-shot
  token in `core/frontend/csrf.py` (`08a6edf`, amended `b0634bd`/`ea31123`).

Still open, unchanged by this feature: ADR-016's **R3** (no reveal-audit viewer — the `auditor` role
this ADR adds has nothing to view yet) and **R4** (unredacted `source_context` on reveal).

## Alternatives Considered

- **A dict- or JSON-encoded role-key settings field** instead of four flat fields — rejected (§2):
  breaks `settings.py`'s established one-tunable-per-field convention, and a JSON-encoded env var is
  an easy footgun to misquote/escape in a `.env` file or a container's env block versus four plain
  strings.
- **A warning instead of a hard startup failure for a stale `API_KEY`** (§3) — rejected: Phase 6's
  `log_auth_mode` already emits a legitimate WARNING for the "all four keys empty, auth disabled"
  case; a second warning for "you forgot to migrate off `API_KEY`" would be visually indistinguishable
  in the log stream from the intended, correct disabled-auth state. Only a boot failure cannot be
  scrolled past.
- **Silently accepting two byte-identical role keys** (§4) — rejected: a silent role collapse (an
  auditor key that also happens to open every recruiter-only route) defeats the entire purpose of
  having roles at all, and nothing downstream would ever surface that the split had failed.
- **Short-circuiting `resolve_role`'s comparison loop on the first match** (§5) — rejected: even
  though each individual `compare_digest` call is constant-time, exiting the loop early reintroduces
  a coarser timing/counting channel correlated with which configured role key a near-miss guess was
  closest to matching.
- **Re-adding a `reveal` parameter to the shortlist export as an audited action inline** — rejected
  (§6): bulk-revealing an entire shortlist in one GET is a fundamentally different, higher-blast-radius
  operation than the one-candidate-at-a-time audited POST ADR-016 built; if it's ever needed it should
  be its own new, explicitly audited POST route, not a flag bolted onto an existing read.
- **Adding an audit row on every `blind_review` flip** instead of / in addition to restricting `PATCH
  /jobs/{id}` to admin/recruiter (§7) — considered, deferred: closing the *authorization* gap (who may
  flip the flag) was in scope for this feature; adding a second audit sink parallel to `reveal_audit`
  for column-level changes is a larger, separable piece of work and is left as a follow-up, not
  silently done here.
- **A per-user login / session-carried identity for the Flask viewer**, instead of one fixed
  `recruiter` role key presented for every browser — rejected for this feature: there is no Postgres
  user table anywhere in this codebase to authenticate against, and building one is out of scope for
  route-level RBAC. Recorded as an accepted residual, not solved here.
- **A server-side (Redis-backed) CSRF token store** instead of storing per-résumé tokens in the
  signed Flask session cookie (§8) — considered, deferred: it would remove the 64-token-per-session
  cap and the cookie-byte-budget constraint entirely, but the codebase's only existing Redis usage is
  the arq broker (per CLAUDE.md, "Redis only as arq broker"), so this would be a new use of the
  dependency for a problem the session-scoped shrink (`ea31123`) already solves at the realistic
  scale (a ≤50-row shortlist render). Left as a follow-up if the 64-entry cap is ever actually hit in
  practice, not built speculatively here.
