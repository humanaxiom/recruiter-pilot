# ADR-025: User administration and no-role-by-default access control

**Status:** Accepted (implemented on `feat/user-admin-roles`; gates green — reviewer APPROVE ×2, security PASS; not yet integrated to main at time of writing)
**Date:** 2026-07-25

## Context

ADR-019 (FU-5) shipped CAS identity with a default-admin allowlist (§10a): a user's first CAS login
provisioned `role='recruiter'` unless their `cas_username` matched `settings.default_admin_cas_username`
(`asalah`), in which case they were provisioned `role='admin'`. Every other new user therefore landed with
full recruiter-equivalent access on first login, with no admin step in between — anyone who could reach the
institution's CAS could self-provision a working recruiter account. ADR-019 §9 recorded this gap
explicitly ("FU-5 ships no role-provisioning path — every CAS login lands as `recruiter`… promotion…
requires manual SQL") and deferred it.

This feature closes that gap: new users get no access until an admin grants it, and an admin role-management
surface (API + Flask UI) replaces the manual-SQL promotion path.

## Decision

### 1. No-role-by-default first login (reverses ADR-019 §10a)

`users.role` becomes nullable with no default (`core/src/models/ddl.py`): the `CREATE TABLE` column is bare
`role TEXT`, and two idempotent statements —
`ALTER TABLE users ALTER COLUMN role DROP DEFAULT` / `ALTER TABLE users ALTER COLUMN role DROP NOT NULL` —
relax an already-migrated dev/CI volume that still has the old `NOT NULL DEFAULT 'recruiter'` column, matching
this repo's no-migration-framework convention (every schema change ships as an idempotent statement re-run on
every boot, alongside the `CREATE TABLE IF NOT EXISTS`).

`user_service.provision_or_get` now writes `role = NULL` on a non-default-admin's first login instead of
`'recruiter'` — fail-closed, no access, rather than fail-open. The default-admin allowlist itself is
unchanged: a `cas_username` matching `settings.default_admin_cas_username` (`asalah`) still lands `admin` on
first login. The `ON CONFLICT` (second-login) path still never touches `role` — a role assigned or changed
out-of-band always survives the next login, including a later login by the default-admin username after a
demotion.

### 2. Fail-closed access gate

A no-role user existing in `users` is not by itself harmless: nothing before this feature stopped that user
from riding the Flask viewer's one shared `recruiter` API key to full recruiter-equivalent, company-wide
access (`require_role`/`resolve_role` only ever judges the *key*, never the session). `require_role_assigned`
(`core/src/api/deps.py`) closes this: a dependency wired on the 5 business routers — `jobs`, `resumes`,
`shortlist`, `job_assignees`, `audit` (`core/src/api/main.py`) — that 403s a **real, resolved** session whose
`role` is `None`, before any route body runs. It never judges the *absence* of a session (`user is None` —
a bare service-key caller, or CAS disabled outside the synthetic dev-admin sentinel — passes unconditionally),
only a genuinely authenticated session with no assigned role.

The Flask frontend mirrors this at the second hop: `_cas_auth_gate` (`core/frontend/app.py`) intercepts an
authenticated CAS status with `role: None` and renders `pending_access.html` (200, "contact an administrator")
before any route body runs — the same shape as the backend gate, one layer up.

### 3. Admin role-assignment API

`GET /users` (list every user) and `PATCH /users/{id}/role` (assign a role) — `core/src/api/routes/users.py`
— are both gated by `_require_admin_session`, a route-local dependency distinct from `require_role_assigned`.
It keys off the **CAS session's** `user.role == "admin"`, never the API-key-derived `Role` from
`resolve_role`/`require_role`. This is deliberate: the Flask viewer sends the *same* shared `recruiter`
API key for every browser user (`core/frontend/api_client.py`), so gating on the key would 403 every real
admin browsing through that shared key, and would separately let a bare service/admin *key* with no
verifiable human session list or reassign every user — the same "is this a real, role-verified human"
gap that `scoped_user_id_or_403` (ADR-020 §3/§4/§5, FU-6) closes for row-scoping. `_require_admin_session`
is deliberately **not** stacked with `require_role_assigned` on `/users` — it is strictly stronger (a
no-role session is already 403'd by it alone), so the pairing would be redundant.

`RoleAssignment` (`core/src/schemas/auth.py`) closes the request body's vocabulary to the `Role` enum — an
unknown or null role string is a 422 `ValidationError`, not a value written verbatim; this endpoint only ever
*assigns* a real role, it never clears one (`{"role": null}` is rejected).

The `role_changed` audit row is written in the **same** `conn.transaction()` as the `role` `UPDATE` — a
failed audit write rolls back the role change, mirroring `job_service.update_job`'s `blind_review`-flip
pattern and `job_assignees`'s assign/unassign atomicity. Actor attribution (`actor_kind`/`actor_user_id`/
`actor_service`) comes from `actor_fields_from_user(acting_admin)` — the session identity — never from the
request body.

### 4. Last-admin lockout guard

Inside the same transaction, before the `UPDATE`: if the target user is currently `role='admin'`, the new
role is not `'admin'`, and `user_service.count_active_admins()` returns exactly `1`, the request is rejected
with `ConflictError` (409) — no `UPDATE`, no audit row. This prevents demoting (or being demoted from) the
last active admin into an unrecoverable, un-administered organization. Mirrors `job_assignees`'s "a rejected
no-op writes no audit row" discipline.

### 5. The `Role` enum move

`Role` moves from `core/src/api/deps.py` to `core/src/schemas/auth.py`, re-exported from `deps` as
`from src.schemas.auth import Role as Role` (the explicit `as`-form mypy `--strict` requires for an
implicit re-export). This is behaviour-preserving — every existing `from src.api.deps import Role` /
`deps.Role` call site is unaffected — and exists solely to break an import cycle: `RoleAssignment` (§3)
needs `Role` as a field type and lives in `schemas.auth`, but `deps.py` already imports `User` from
`schemas.auth`; defining `Role` in `deps.py` and importing it back into `schemas.auth` would be a real
`deps` ↔ `schemas.auth` cycle, broken differently depending on which module a test imports first.

### 6. Flask admin UI

`/admin/users` (`GET`, list + one `<select>`-and-Save form per user) and `POST /admin/users/{id}/role`
(`core/frontend/app.py`, `core/frontend/templates/admin_users.html`) give an admin a role-assignment surface
without SQL. `_require_admin_page` mirrors the backend's `_require_admin_session`: in dev mode
(`cas_enabled=False`) it is an unconditional passthrough (the dev-anonymous sentinel *is* the backend's
synthetic admin); with CAS enabled it reads the same `g.cas_user` status `_cas_auth_gate` already stashed
for the request (never re-fetched) and 403s any non-admin role before any `list_users`/`set_user_role` call.
A backend 409 (last-admin lockout) re-renders `admin_users.html` with the error inline rather than
redirecting. `base.html`'s nav gains an admin-only "Users" link (`current_user.role == "admin"`).

## Consequences

- **A new deployment's first login (other than the configured default-admin) is now a dead end until an
  admin acts.** This is the intended fail-closed behaviour, not a bug — it directly closes the gap ADR-019
  §9 recorded and deferred.
- **`GET /users`/`PATCH /users/{id}/role` are the only two routes in the API gated by session role rather
  than key role.** A future reviewer of this codebase should not assume `require_role`/`resolve_role`
  covers every route — `/users` is a deliberate, documented exception.
- **A demoted user's role sticks across every future login,** including the default-admin username: the
  `ON CONFLICT` provisioning path never rewrites `role`, so `default_admin_cas_username` only ever grants
  admin on the row's *creation*, never re-grants it later.

### Accepted residuals

- **Concurrent double-demote race (Low, ACCEPTED — flagged independently by both the reviewer and
  security).** `count_active_admins()`'s read and the role `UPDATE` both run under asyncpg's default READ
  COMMITTED isolation with no row lock. From exactly 2 active admins, two simultaneous demote requests
  against two *different* admin rows can each observe `count == 2`, each pass the last-admin guard (neither
  sees the other's in-flight change), and both commit — leaving 0 admins, an org-wide lockout. This requires
  two concurrent privileged admin sessions and tight timing; it matches the existing
  `job_assignees`/`blind_review` transaction-without-row-lock pattern already accepted elsewhere in this
  codebase. **Deliberately deferred** — this is an offline, single-org tool with effectively one admin
  today. Remediation when multi-admin becomes real: `SELECT id FROM users WHERE role='admin' AND active
  FOR UPDATE` inside the transaction before the count, or SERIALIZABLE isolation.
- **No-role recovery requires an admin.** A newly-provisioned no-role user sees `pending_access.html` until
  an admin assigns a role; if every admin were somehow removed (see the residual above), recovery needs
  direct database access — there is no self-service or break-glass path. `provision_or_get`'s `ON CONFLICT`
  clause deliberately omits `role`/`active` from its `SET`, so a demoted `asalah` is **not** re-granted admin
  on re-login — role changes are intentionally sticky, not reset by the default-admin allowlist.

## Alternatives Considered

- **Keep the ADR-019 §10a default of `role='recruiter'` and add role management on top.** Rejected: it
  leaves the fail-open window open for every deployment until an admin happens to notice and demote new
  users — the exact gap this feature exists to close.
- **A DB-level `CHECK` enforcing at-least-one-admin instead of an application-level guard.** Rejected:
  Postgres has no cross-row aggregate `CHECK` constraint (a `CHECK` only sees the row being written), so this
  would need a trigger — more moving parts than the transaction-scoped `count_active_admins()` guard already
  proven against a real Postgres, for the same guarantee.
- **Gate `/users` on the API-key role instead of the session role.** Rejected in §3's own reasoning: the
  Flask viewer's one shared `recruiter` key makes key-role indistinguishable across every human browsing
  through it, so a key-role gate cannot tell an admin from a non-admin browser user.
