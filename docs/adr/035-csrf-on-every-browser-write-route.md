# ADR-035: CSRF on every browser state-changing route (`fix/csrf-all-browser-write-routes`)

**Status:** Accepted (closes ROADMAP.md A1 step (iv) / Phase 1.3 — the last unclosed piece of the A1
authorization thread; completes [ADR-033](033-session-role-enforcement-on-writes.md) and
[ADR-034](034-auth-boundary-fails-open.md); extends the FU-4/D4 control documented in
`core/frontend/csrf.py`)
**Date:** 2026-08-13

## Context

FU-4/D4 built a genuine anti-forgery control — a session-bound, per-résumé, per-action **one-shot** token
plus a same-origin check — and wired it to **three** routes: `resume_reveal`, `resume_withdraw`,
`resume_reinstate`. The Flask viewer has **twelve** POST routes. The other nine had nothing.

Two of the nine are the ones that matter:

- **`POST /admin/users/<user_id>/role` — privilege escalation.** A forged cross-site auto-submit,
  triggered from a logged-in admin's browser, promotes an attacker-controlled account to `admin`.
  Measured before the fix: the forged request returns **302**, i.e. it completes.
- **`POST /jobs/<job_id>/blind-review`** — the **same un-blinding flip the ADR-034 exploit used**. That
  finding closed the *unauthenticated* path to it. A forged request reaches it a different way: riding a
  real recruiter's session. Same room, different door.

The remaining seven (`create_job`, `bulk_create_jobs`, `upload_resumes`, `transition_status`,
`generate_shortlist`, `resume_match_jobs`, `jd_extract`) are lower severity but all change state or
consume model capacity on the GPU host.

**Why the Flask hop is the vulnerable one** — unchanged from FU-4/D4's own reasoning: the browser supplies
no credential of its own to Flask, and Flask attaches its **own** server-held API key on the outbound leg
(`api_client.build_client`), so Flask cannot distinguish a forged cross-site submit from a genuine click.

**ADR-034 made this strictly more important, not less.** Every backend write now demands a real CAS
session. The session cookie the browser attaches to a forged cross-site request is precisely what makes
the forgery succeed — closing the sessionless door raised the value of the session-riding one.

## Decision

### 1. A single `before_request` hook — opt-OUT, not opt-in

`frontend.app._csrf_gate` guards every request whose method is state-changing. A **new POST route is
protected the moment it is added**; removing that protection requires an explicit, tested entry in
`_CSRF_HOOK_EXEMPT_ENDPOINTS`.

Opt-in protection is what produced this gap, and it is the ROADMAP A7 shape: `csrf.py`'s module docstring
reads as *the* CSRF story for the application while nine routes had none of it. A per-route decorator
would have reproduced the same failure mode one generation later.

The hook rejects with **403 before the view runs**, so a forgery never reaches the backend and cannot
cause the effect it intends. It is registered **after** `_cas_auth_gate`, so an unauthenticated browser is
still redirected to login rather than shown a bare 403.

### 2. A session-wide *page* token — deliberately not one-shot

`csrf.issue_page_token` / `verify_page_token`: the classic synchronizer-token pattern, one value per
session, idempotent to mint, **never consumed on use**.

This is a considered departure from the one-shot design next to it. A page token that burned on use would
break the back button, a second tab, and every "fix the validation error and resubmit" flow — all of which
post the same rendered form twice. One-shot semantics earn their cost on reveal/withdraw/reinstate, where
each control is rendered fresh per résumé and replay is the threat; they are pure breakage on an ordinary
form.

Minted in the `inject_current_user` context processor, so **every** render carries it and a template that
grows a new form cannot forget to ask for one.

### 3. Two submission channels

A hidden form field (`csrf_token`) and an `X-CSRF-Token` header. The header is set **once** as
`hx-headers` on `<body>` and inherited by every htmx request on the page, including requests fired from
partials swapped in later (htmx resolves inherited attributes by walking the live DOM).

Both are accepted on every guarded route rather than per-route. Two of the nine controls (`jd_extract`,
`generate_shortlist`) are bare `hx-post` buttons with no surrounding `<form>`, so a form-field-only reader
would have left exactly those either unprotected or broken — and a control that later changes between a
plain form and an htmx post must not silently lose its protection in the process.

### 4. Exemptions that are not downgrades

The three FU-4/D4 routes keep their one-shot tokens and are **skipped** by the hook rather than guarded
twice — stacking a weaker session-wide token on top of a stronger per-resource one would mean rendering
two tokens into one form for no gain. This mirrors ADR-033's exemption discipline (`PATCH /users/{id}/role`
is exempt from `require_session_role` because `_require_admin_session` is already narrower).

An exemption list is the natural place for a control like this to rot, so **two** tests hold it:

- the exemption set is asserted to be **exactly** those three endpoints, so widening it requires
  deliberately editing a test that explains why the current three are there;
- a page token is asserted **not** to open a reveal, so an exemption cannot quietly become a swap of a
  one-shot per-résumé control for a session-wide reusable one.

### 5. The assertion that would have caught it — behavioural, not introspective

`test_frontend_csrf_covers_every_write_route.py` enumerates `app.url_map` and **drives a real forged
request at every state-changing route**, asserting 403 *and* that no backend call was made.

This is deliberately unlike its sibling `test_write_route_session_gate.py`, which recognises a gate by
`__qualname__`. That works on the FastAPI side because the gate is a per-route dependency object. It would
be the **wrong check here**: a hook that is registered but inert would satisfy any introspective check
while protecting nothing — exactly the defect shape A7 names. The route list comes from the url_map, so it
cannot drift out of date the way a hand-maintained list silently does.

## Consequences

- Every state-changing browser route is guarded, and future ones are guarded by default.
- Templates rendering a form to a guarded route must include the hidden field; htmx callers need nothing,
  having inherited the header. A miss fails loudly (403 in the developer's face), not silently.
- **33 pre-existing tests started 403ing and were fixed, not weakened.** Unlike the 13 tests ADR-034 had to
  rewrite — which actively *pinned a fail-open as correct behaviour* — these simply predate the control:
  they POST to these routes to exercise business logic. They now present a page token the way a real
  browser does, via a shared `csrf_client` fixture. That fixture is **deliberately not autouse**: an
  autouse token would satisfy the guard for the very tests written to prove the guard works.

### Accepted residuals

- **The same-origin check remains advisory-by-design.** `csrf.same_origin` blocks only when a *cross*-origin
  `Origin`/`Referer` is actually present and stays silent when neither header exists — unchanged from
  FU-4/D4, and why the token is the primary control rather than the origin check.
- **No token rotation on privilege change.** The page token lives as long as the session. A session whose
  role changes mid-flight keeps the same token; the backend's own role gates (ADR-033/034) are what
  actually authorize the request, so this is a defence-in-depth gap, not an authorization one.
- **`SameSite` cookie attributes are not set explicitly** by this change; the token does not depend on
  them. Worth revisiting as belt-and-braces, not required for this control to hold.
- **Not verified against a live browser.** The stack does not boot in this environment without a human
  running `./scripts/quickstart.ps1` (ADR-034). The guard is proven by the full unit and integration
  suites, including real forged requests through the Flask test client, but a human should click through
  the workflow UI once before the pilot widens — particularly the htmx-driven controls, whose
  `hx-headers` inheritance is exercised in tests through the header path rather than through a real htmx
  runtime.

## Alternatives considered

- **A per-route decorator on each of the nine.** Rejected: it protects exactly today's nine and reproduces
  the opt-in failure mode for route ten. The whole point of A7 is that the enforcement, not the intention,
  is what has to be structural.
- **Extending the per-résumé one-shot token to all twelve.** Rejected: one-shot semantics break ordinary
  form flows (back button, second tab, resubmit-after-validation-error), and most of the nine have no
  natural per-resource scope — `create_job` has no id yet, by definition.
- **Flask-WTF / a CSRF extension.** Rejected: it would duplicate a working, well-documented in-repo control
  with different semantics, forcing a migration of the three one-shot routes or leaving two frameworks
  side by side. The offline-first constraint also makes a new runtime dependency a cost that needs a reason.
- **Double-guarding the three exempt routes.** Rejected — see §4.

## Gate state

`./scripts/verify.sh all` green, exit code captured directly rather than piped: `EXIT=0`, **4338 unit tests
@ 94.25% coverage, 482 integration tests**.

RED was measured before implementation and was precise: the nine unguarded routes failed, the three FU-4/D4
routes already returned 403, and `admin_set_user_role` returned **302** — the forged privilege escalation
completing.
