# ADR-033: Session-role enforcement on write routes (`fix/session-role-on-writes`)

> **⚠️ AMENDED 2026-08-13 by [ADR-034](034-auth-boundary-fails-open.md) — read that first.** This ADR
> **did not close its own stated worst case**. In the shipped configuration no `API_KEY_*` existed in any
> channel, so `auth_enabled` was `False`, `resolve_role` returned `Role.ADMIN` for every request, and both
> this ADR's `require_session_role` and the pre-existing `require_role_assigned` passed on `user is None` —
> two gates, ANDed, both vacuous. An unauthenticated caller could flip `blind_review` and read candidates
> un-blinded. **§1's `user is None` → PASS contract below is REVERSED** (ADR-034 §2: a valid API key alone
> is never sufficient for a write); §3's structural guard, §4's reveal reversal and §5's step-(iii)
> reasoning all still stand.

**Status:** Accepted, **amended by ADR-034** (closes ROADMAP.md A1 P0 — "the human's role is not enforced on writes"; depends
on [ADR-018](018-rbac-keyed-roles.md) for the keyed-role model, [ADR-019](019-cas-identity-attributable-audit.md)
for `resolve_user`/CAS session identity, and [ADR-020](020-per-job-assignment-scoping.md) for
`scoped_user_id_or_403` — amends ADR-020 §3/§9 for the reveal route specifically)
**Date:** 2026-08-07

## Context

`require_role`/`resolve_role` (`core/src/api/deps.py:92-147`, ADR-018) authorizes the presented
**API key** only. `require_role_assigned` (`deps.py:284-313`, ADR-019 §10a/§10b) intersects the
session with the key in exactly one way — it 403s a REAL session whose `role` is `None` — but never
checks whether a real session's assigned role is actually permitted to write. `scoped_user_id_or_403`
(`deps.py:316-356`, ADR-020) scopes **reads** to a hiring_manager session's assigned jobs; it is never
called on a write route.

The Flask BFF attaches ONE shared `recruiter` API key to every browser request
(`core/frontend/api_client.py:118-119`), because the frontend has no per-role key provisioning. The
consequence, stated plainly at `deps.py:294-295` (`require_role_assigned`'s own docstring) before this
fix: *"`require_role` only judges the KEY, never the session."* Concretely: a signed-in hiring_manager
or auditor, browsing through the shared recruiter key, passes every `require_role(Role.ADMIN,
Role.RECRUITER)` check on every write route exactly like a real recruiter — turn blind review off,
withdraw/reinstate a candidate, regenerate a shortlist, upload résumés, create/close jobs, assign/
unassign job ownership. Worse for auditor: a read-only oversight role, whose entire justification for
unscoped global read is a compensating audit trail (ADR-020 §6), could reveal — the de-anonymization
action itself — with zero role check ever seeing its real session role.

**Why the gates missed it (ROADMAP A1).** Every existing negative-authz test parametrizes the
**API-key** role (`test_route_jobs.py`, `test_api_resumes_withdraw_pg.py`, etc.). No test anywhere
exercised the actual production combination — recruiter key + hiring_manager/auditor **session** —
until this branch's `test_route_{jobs,resumes,shortlist,job_assignees}_session_gate.py` and
`test_write_route_session_gate.py` added that axis. All 40 of those tests were RED against the
pre-fix code; they are the spec this ADR describes the implementation of.

## Decision

### 1. `require_session_role(*allowed)` — a new dependency factory

Added to `core/src/api/deps.py`, mirroring `require_role`'s shape (a factory returning a distinct
closure per call site, so `app.dependency_overrides` targets the shared `resolve_user`, never a
`require_session_role(...)` closure by identity) and `users.py::_require_admin_session`'s semantics
(a plain `fastapi.HTTPException(status_code=403, ...)`, not `AppError`):

```python
def require_session_role(*allowed: Role) -> Callable[..., Awaitable[None]]:
    allowed_roles = frozenset(allowed)

    async def _check(user: Annotated[User | None, Depends(resolve_user)]) -> None:
        if user is not None and user.role not in allowed_roles:
            raise HTTPException(status_code=403, detail="session role not permitted for this route")

    return _check
```

Three cases, identical in shape to `require_role_assigned`/`scoped_user_id_or_403`/
`_require_admin_session` — this repo now has four independent gates sharing one contract, which is
deliberate, not duplicated accidentally (see §4):

- **`user is None` (no session at all — a bare service-key caller) → PASS.** ⚠️ **REVERSED by
  [ADR-034](034-auth-boundary-fails-open.md) §2 — this bullet is the defect, not the contract.** It now
  **403s**. The reasoning below was wrong: combined with zero `API_KEY_*` configured anywhere (so
  `resolve_role` trivially resolved `Role.ADMIN` for anyone), "never judge the absence of a session" meant
  a cookie-less, key-less caller reached every write route with no credential at all. The human decision
  recorded in ADR-034: a valid API key alone is **never** sufficient for a write. *Original text, retained
  so the reversal is legible:* "This is the case an
  over-eager implementation breaks, and it is load-bearing: not every write route this gate guards is
  human-only. `PATCH /jobs/{id}` accepts a bare service-key caller and audits it as
  `actor_kind='service'` (`actor_fields_from_user`, ADR-019 §9.2); a bare-key CI script or worker
  calling a write route with no CAS session must keep working. This dependency never judges the
  ABSENCE of a session — only what a REAL session's role is."
- **A real session whose `role` is not in `allowed` → 403.** The defect this ADR closes.
- **A real session whose `role` is in `allowed` → PASS.** The CAS-disabled synthetic dev-anonymous
  sentinel (`role="admin"`, ADR-019 §10b) passes naturally through this same string comparison — no
  special-casing needed, because `Role` is a `StrEnum` and `User.role` is `str | None`.

### 2. Applied to all 13 write routes, as a per-route dependency — never router-level

Every route below now carries **both** `Depends(require_role(*_WRITERS))` (the pre-existing key
gate) and `Depends(require_session_role(*_WRITERS))` (the new session gate) in its own
`dependencies=[...]` list — independent checks that must both hold, not one replacing the other. All
thirteen use the same allowed set, `{Role.ADMIN, Role.RECRUITER}`:

- `routes/jobs.py`: `POST /jobs`, `POST /jobs/jd-extract`, `POST /jobs/bulk`, `PATCH /jobs/{id}`,
  `PATCH /jobs/{id}/status`
- `routes/resumes.py`: `POST /jobs/{id}/resumes` (upload), `POST /resumes/{id}/reveal`,
  `POST /resumes/{id}/withdraw`, `POST /resumes/{id}/reinstate`, `POST /resumes/{id}/match-jobs`
- `routes/shortlist.py`: `POST /jobs/{id}/shortlist` (generate)
- `routes/job_assignees.py`: `POST /jobs/{id}/assignees`, `DELETE /jobs/{id}/assignees/{user_id}`

This is **deliberately a per-route dependency, not `include_router(..., dependencies=[...])`.** The
read routes on these same routers (`_JOB_READERS`/`_RESUME_READERS`/`_SHORTLIST_READERS = tuple(Role)`)
share the router object and must keep their wider access — a router-level dependency would gate every
route on it identically, which is exactly the router-vs-route distinction `deps.py`'s own module
docstring (§FU-4) already establishes for `require_role`.

`reveal_resume` binds `require_role` as a **parameter** (`role: Annotated[Role, Depends(require_role(
*_REVEALERS))]`, because it uses the resolved value for `scoped_user_id_or_403`), while
`require_session_role(*_REVEALERS)` is added to the decorator's `dependencies=` list — it needs no
value, only the side effect.

**`PATCH /users/{user_id}/role` is the one write route deliberately exempt.** It already gates on the
CAS session role directly via `users.py::_require_admin_session` (`user is not None and user.role ==
"admin"`) — strictly narrower than any `require_session_role(...)` set this ADR would otherwise add
(a set of one, `{admin}`, vs. `require_session_role`'s minimum useful set of `{admin, recruiter}`), so
stacking the two would be redundant. `/auth/cas/*` needs no exemption entry at all: every route under
it is a GET (session lifecycle), so none are write routes in the first place.

### 3. Anti-regression control: a structural route-table guard

`core/tests/unit/test_write_route_session_gate.py` imports the REAL, fully-wired `src.api.main.app`
and walks its actual route table (`APIRoute.dependant.dependencies`, recursively, matching a closure by
`__qualname__` containing `"require_session_role"` — the same technique the pre-existing
`test_router_role_gate.py` uses for router-level dependencies). It maintains an explicit, enumerated
`_KNOWN_GATED_WRITE_ROUTES` set (all 13 above) plus a visible `_EXEMPT_WRITE_ROUTES` dict (the one
`PATCH /users/{id}/role` exemption, with its reason recorded inline) and asserts the union equals every
POST/PATCH/PUT/DELETE route actually mounted. **This is the control that prevents this fix from rotting**:
a future write route added to any of these routers without a `require_session_role(...)` dependency
fails this test at collection time — the exact same class of gap this whole ADR exists to close — rather
than silently reaching production the way the original defect did for 40 previously-unexercised test
combinations.

### 4. The reveal reversal — recruiter/admin only, no scoped hiring_manager exception

Applying `require_session_role(*_REVEALERS)` (`_REVEALERS = (Role.ADMIN, Role.RECRUITER)`, unchanged
from ADR-018/D2) to `POST /resumes/{id}/reveal` surfaced a genuine, pre-existing contradiction: ADR-020
§3/§5 (FU-6 slice 6) had built and tested a scoped hiring_manager SESSION reveal path — a hiring_manager
assigned to a job could reveal résumés under it, blocked only when unassigned — directly contradicting
`_REVEALERS` and the HR-facing ranking-metrics explainer's own claim that a hiring manager cannot reveal
and must go through a recruiter. Both were true in their respective test suites simultaneously, because
the shared recruiter key meant neither's negative case ever ran against the other's positive case, until
this branch's session-gate tests exercised the real production combination and forced two tests to fail
no matter which way the contradiction resolved.

**Human decision:** reveal is recruiter/admin only. A hiring_manager session now 403s on every reveal
attempt, assigned or not — un-blinding stays a genuine two-person action (a hiring manager requests, a
recruiter/admin reveals), not something a hiring manager can trigger for their own assigned jobs. See
ADR-020 §9 for the full account (including the struck-through §3 bullet) and
`core/tests/unit/test_route_reveal.py`'s "Reversal" section for the two tests that replace FU-6 slice
6's assigned/200 vs. unassigned/404 pair — under the new policy both cases are deliberately
indistinguishable (403, no audit row, no decrypt), which is a strict improvement on the existence-oracle
concern ADR-020 §5 raises for the read routes: a hiring_manager can no longer probe assignment status
via this route at all.

`reveal_resume`'s existing `scoped_user_id_or_403` call is **retained**, not removed. It is now defence
in depth: `require_session_role` already blocks any hiring_manager session before that call's own
hiring_manager-scoping branch could ever fire for THIS route, but the call still runs (harmlessly, always
resolving `None` for an admin/recruiter session) and stays load-bearing for every READ route on
`resumes.py`/`jobs.py`/`shortlist.py`, which ADR-020 §3/§4/§5 continue to govern unchanged.

### 5. Why ROADMAP A1 step (iii) — "extend `scoped_user_id_or_403` to writes" — is deliberately NOT built

ROADMAP.md's A1 plan-of-record listed four steps: (i) the missing test axis, (ii) `require_session_role`,
(iii) extend `scoped_user_id_or_403` to writes, (iv) CSRF on all 12 (now 13, +assignees) routes. This
ADR implements (i) and (ii) in full and leaves (iii) undone **on purpose**, not as a deferred residual:

Once every write route's `require_session_role` allowed set is `{admin, recruiter}` (§2 above), there is
no *scoped* role left that can reach a write route at all. `scoped_user_id_or_403` exists to confine a
`hiring_manager` **session** to their own assigned jobs (ADR-020 §4); both `admin` and `recruiter` are
unscoped by design (`deps.py:346-356` — `scoped_user_id_or_403` returns `None`, "see every row," for
anything other than a real `hiring_manager` session). A hiring_manager session cannot pass
`require_session_role(Role.ADMIN, Role.RECRUITER)` in the first place, so it never reaches a point in
any write route's dependency chain where a scoping predicate on `user_id` would have anything to scope —
there is no `hiring_manager`-authored write action anywhere in this API to bound to their own jobs. Wiring
`scoped_user_id_or_403` into a write route today would be dead code: it would only ever be called with a
`user` whose role already passed the `{admin, recruiter}` filter, and both of those roles resolve
`user_id=None` (unscoped) from that helper unconditionally. **Recording this explicitly is the point of
this section** — so a future session re-deriving "extend scoping to writes" from the stale ROADMAP.md
plan does not spend an iteration re-discovering that it has no work left to do, or worse, add a
scoping call that can never do anything but return `None`.

If a future feature ever needs a scoped role to perform SOME write (e.g., a hiring_manager requesting —
not performing — a reveal, or leaving a note on their own assigned job), that is new product scope
requiring its own ADR, not an extension of this one.

## Consequences

- Every write route in this API (13 of 13, minus the one structurally-exempt admin-role route) now
  requires the CALLER's real CAS session role, not just the presented API key, to be `admin` or
  `recruiter` — the shared-browser-key gap ROADMAP A1 named is closed for every write action, including
  the previously-uncaught auditor-can-reveal path.
- `test_write_route_session_gate.py` is a merge-blocking structural guard: any future write route added
  to `jobs.py`/`resumes.py`/`shortlist.py`/`job_assignees.py` without a `require_session_role(...)`
  dependency fails at test-collection time, not in production.
- ADR-020's FU-6 slice 6 scoped-hiring-manager-reveal capability is retired; ADR-020 §3/§9 documents the
  reversal in place rather than silently editing the earlier decision.
- CSRF extension to the remaining write routes (ROADMAP A1 step iv) remains open — untouched by this
  branch, tracked separately in ROADMAP.md.

### Accepted residuals

- **Step (iii) is deliberately not built** — see §5. Not a deferral; there is no remaining work under
  the current role model.
- **CSRF (step iv)** is out of scope for this branch — see Consequences above.
- **No admin/recruiter-scoped-to-a-subset-of-jobs model exists.** `admin`/`recruiter` remain globally
  unscoped by design (ADR-020 §4); this ADR does not change that, only closes the session-vs-key gap for
  the roles that already had wide access.

## Alternatives considered

- **Router-level `dependencies=[Depends(require_session_role(...))]` on `include_router(...)`.**
  Rejected: the read and write routes on `jobs.py`/`resumes.py`/`shortlist.py` share one router object
  each, and reads must stay open to all four roles (`tuple(Role)`). A router-level dependency cannot
  express "some routes on this router need this, others don't" — the same reasoning ADR-018 already used
  to justify per-route (not per-router) `require_role`.
- **Widening `_REVEALERS`/reveal's `require_session_role` set to include `hiring_manager`**, preserving
  FU-6 slice 6's scoped capability instead of retiring it. Rejected by human decision — see §4: the
  blind-review value proposition (a genuine two-person un-blind action) outweighs a hiring_manager's
  convenience of self-serve reveal for their own assigned jobs, and it matches what the HR explainer
  already told users was true.
- **Building ROADMAP A1 step (iii) anyway, as originally planned**, for symmetry with the read routes.
  Rejected — see §5: it would be dead code under the role model this ADR establishes, and shipping dead
  authorization code invites a future "why doesn't this scope anything" investigation for no security
  benefit.

## Gate state

Offline (`./scripts/verify.sh`): ruff · black · `mypy --strict` clean. All 71 tests across the five new
session-gate test files pass (40 previously RED, 31 already-passing structural/positive cases), plus the
two reversed reveal tests in `test_route_reveal.py` (see §4). Integration (`./scripts/verify.sh
integration`) run against real Postgres/Neo4j/Redis. See the branch's PR for the exact pass counts and
coverage percentage at merge.
