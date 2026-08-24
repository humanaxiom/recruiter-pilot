# ADR-020: Per-job assignment and row-level scoping (FU-6)

**Status:** Accepted (extends ADR-018's role-level RBAC to row level, closing its "roles are role-level, not row-level" residual; depends on [ADR-019](019-cas-identity-attributable-audit.md), which establishes the `users` table and the `audit_log` this ADR writes to — FU-6 cannot ship before FU-5)
**Date:** 2026-07-20

## Context

ADR-018 (FU-4) shipped RBAC, but explicitly deferred row-level scoping. The `Role` enum docstring in `core/src/api/deps.py` (L54–56) states plainly: "there is no per-job owner column, so a hiring-manager or auditor key grants its read access across every job company-wide." The accepted residual (ADR-018 §Accepted residuals, L273–277) records this as a deliberate scope cut: "there is no notion of 'this hiring manager's own requisitions only.'"

The practical consequence: any recruiter, hiring_manager, or auditor credential reads **every** job, résumé, and shortlist in the company. There is no owner column, no membership table, and no actor predicate in any query route handler — they query by `job_id`/`resume_id` alone (e.g., `core/src/api/routes/shortlist.py` L98–99, L106: `list_shortlist` and `get_shortlist_entry` pass `job_id`/`entry_id` to service functions with no scoping predicate). This is a legitimate need-to-know for recruiters orchestrating a hire, but hiring_managers — who typically own a small set of requisitions — have no visibility boundary. An organization with 100 open reqs and 50 hiring managers should not permit each manager to see all 100; they should see only their own.

FU-6 adds per-job assignment, so a hiring_manager's visible job set is the intersection of (a) jobs they are assigned to, and (b) their role's read permission.

## Decision

### 1. `job_assignees` schema

A new table, `job_assignees`, links a user to a single job, created during `init_schema` startup (idempotent DDL in `core/src/models/ddl.py`):

```sql
CREATE TABLE IF NOT EXISTS job_assignees (
    job_id       UUID NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
    user_id      UUID NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    assigned_by  UUID NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
    assigned_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (job_id, user_id)
);
CREATE INDEX IF NOT EXISTS job_assignees_user_idx ON job_assignees (user_id, assigned_at DESC);
```

- **PK is `(job_id, user_id)`**: a user may be assigned to each job at most once; re-assigning a user to the same job is an update (INSERT … ON CONFLICT … DO UPDATE) on the `assigned_at` timestamp, not a duplicate row.
- **FK on `assigned_by` uses `ON DELETE RESTRICT`**: if the assigning user is deleted, orphaned assignments are not silently cascaded; instead, the DELETE fails and the assignment must be explicitly cleared by a different admin first — a safety guard against accidental admin-account cleanup erasing delegation records.
- **FK on `job_id` uses `ON DELETE CASCADE`**: when a job is deleted, its assignments cascade away (a job has no hiring managers if it doesn't exist).
- **Index on `(user_id, assigned_at DESC)`**: powers the fast "all jobs for this user" query (`SELECT job_id FROM job_assignees WHERE user_id = $1 ORDER BY assigned_at DESC`), which the API uses to derive a scoped user's visible job set.

The `users` table (ADR-019) carries `id UUID PRIMARY KEY` and other auth/identity fields — it is the reference for both `user_id` and `assigned_by`.

### 2. Who may assign

Only `admin` and `recruiter` may create, update, or delete assignments. A new route, `POST /jobs/{job_id}/assignees` (admin/recruiter only), accepts a user id and an optional `note` field, records the assignment, and writes an auditable action to ADR-019's `audit_log` table with `action = "assign_job"`, `actor_kind = 'user'`, `actor_user_id = <admin_or_recruiter_id>`, `subject_type = "job"`, `subject_id = job_id`, and contextual fields (`user_id`, `note`). See ADR-019 for the full `audit_log` schema.

Similarly, `DELETE /jobs/{job_id}/assignees/{user_id}` (admin/recruiter only) revokes an assignment and logs `action = "unassign_job"`.

Auditor credentials may **never** assign; assignment is an operational act, not an audit act.

### 3. Scoping is enforced in the query, not the handler (the critical decision)

**This is the single most important decision in this ADR.** A scoping predicate applied in Python *after* fetching from the database is a leak waiting to happen: a typo or a skipped middleware layer and a handler accidentally returns all rows. The actor predicate **must** be part of the SQL query itself, enforced by the database layer.

Every read path for a scoped role (`hiring_manager`) gains a JOIN or EXISTS against `job_assignees`. The service-layer query functions (`job_service.list_jobs`, `job_service.get_job`, `resume_service.list_for_job`, `resume_service.get_one`, `shortlist_service.list_for_job`, `shortlist_service.get_one`) accept an optional `user_id` parameter. When `user_id` is supplied, a WHERE clause AND's in an EXISTS predicate:

For `jobs` table:
```sql
AND EXISTS (
    SELECT 1 FROM job_assignees
    WHERE job_assignees.job_id = jobs.id
    AND job_assignees.user_id = $<user_id>
)
```

For `resumes` and `shortlist_entries` tables:
```sql
AND EXISTS (
    SELECT 1 FROM job_assignees
    WHERE job_assignees.job_id = <main_table>.job_id
    AND job_assignees.user_id = $<user_id>
)
```

The route handler is responsible for determining whether to pass `user_id` (based on the resolved role) before calling the service function. The service function receives it as a parameter — never infers it from the request context — so the scoping predicate is visible at the call site and traceable via grep.

**Affected routes:**
- `GET /jobs` (L152, `core/src/api/routes/jobs.py`) — scoped readers see only their assigned jobs
- `GET /jobs/{id}` (L164, `core/src/api/routes/jobs.py`) — scoped reader requesting an unassigned job gets 404
- `GET /jobs/{id}/resumes` (L199, `core/src/api/routes/resumes.py`) — scoped reader sees resumes only for assigned jobs
- `GET /resumes/{id}` (L212–214, `core/src/api/routes/resumes.py`) — scoped reader accessing a résumé for an unassigned job gets 404
- ~~`POST /resumes/{id}/reveal` (L232–234, `core/src/api/routes/resumes.py`) — scoped reader can only reveal for assigned jobs~~ **SUPERSEDED 2026-08-07 — see §9. Reveal is admin/recruiter only; a hiring_manager session cannot reveal at all, assigned or not.**
- `GET /jobs/{id}/shortlist` (L94–99, `core/src/api/routes/shortlist.py`) — scoped reader lists only their assigned jobs' shortlists
- `GET /jobs/{id}/shortlist/export` (L51–54, `core/src/api/routes/shortlist.py`) — scoped reader exports only their assigned jobs
- `GET /shortlist/{entry_id}` (L102–105, `core/src/api/routes/shortlist.py`) — scoped reader accessing a shortlist entry from an unassigned job gets 404

### 4. Which roles are scoped

- **`hiring_manager`**: scoped to assigned jobs only. A hiring_manager sees only the jobs they are explicitly assigned to.
- **`auditor`**: NOT scoped — retains global read across all jobs. Rationale: an auditor's legitimacy depends on the ability to inspect what others did across the full organization; scoping them to a subset of jobs defeats the role's purpose. To compensate, every auditor read is itself logged to ADR-019's `audit_log` (the watchers are watched), creating an audit trail of audit activity.
- **`admin` and `recruiter`**: unscoped (see every job globally). Rationale: recruiters are the operational pivot of the hiring workflow — they upload résumés into jobs, generate shortlists, and coordinate across requisitions. Scoping them to individual jobs would break cross-requisition workflows (e.g., a recruiter reusing a résumé parsed from one job as a candidate for another, or finding a reverse match across all jobs). Admins are trusted infrastructure operators with full access by design.

The scoping check is applied at the route level. The handler resolves the caller's role via `resolve_role` and the caller's *identity* via `resolve_user` — the dependency ADR-019 §8 introduces, which verifies the HMAC-signed `X-Actor-Assertion` header and returns the authenticated user's id. `resolve_role` alone is insufficient here: it returns a `Role` and never a user id, so scoping cannot be built on it. The handler passes `user_id` to the service function only if the role is `hiring_manager`; for `admin`, `recruiter`, or `auditor`, `user_id` is omitted (None by default).

A scoped role presenting a valid API key but **no** verifiable actor assertion cannot be scoped, because there is no identity to scope against. Such a request is rejected rather than served unscoped — failing closed, since the alternative is silently granting company-wide read to a caller who should see one requisition.

### 5. Unassigned-job behavior: 404, not 403

A scoped user requesting a job or résumé they are not assigned to receives **404 Not Found**, not 403 Forbidden. This is a deliberate, security-relevant choice:

- A 403 response confirms the resource exists, which leaks the existence of requisitions the requesting user may not know about — metadata that could infer headcount, hiring plans, or which teams are understaffed.
- A 404 is indistinguishable from "this job was deleted" or "this job never existed," so it reveals no information beyond the absence of visibility.
- An unassigned user acting on a truly nonexistent job also receives 404, so the two scenarios are observationally identical.

The `NOT EXISTS` predicate in the WHERE clause causes the row to be filtered out silently; a service function like `job_service.get_job(db, job_id, user_id=hiring_manager_id)` returns None (0 rows), and the route handler raises `NotFoundError`, which the global error handler renders as 404 — no additional logic needed.

### 6. Auditor global visibility and the audit log (policy decision requiring ratification)

Auditors retain global, unscoped read access across all jobs in order to fulfill the audit function — they must be able to inspect what others did across the full organization.

**Compensating control:** every auditor read is itself logged to ADR-019's `audit_log`. Every call to a service function by an auditor (characterized by `actor_kind = 'user'` and `actor_user_id = <auditor_id>` in the log row, with `action` being a read operation like `"read_job"` or `"read_resume"`) writes a record with the target and timestamp, creating an audit trail of audit activity — the watchers are watched. See ADR-019 for the full `audit_log` schema and the design of auditor-read logging.

**Note:** This is a policy decision, not a technical one, and the opposite choice is defensible. An organization may prefer auditors to be scoped to assigned jobs (like hiring_managers), avoiding the overhead of logging every read. That choice is not implemented here and would require modifying the role-dispatch logic in section 4 above. Stakeholder alignment on this policy should be sought before deploying auditor credentials at scale.

### 7. A "my jobs" view

The default landing page for a `hiring_manager` is their assigned job set, not the global list. The API provides `GET /my/jobs` (routed through `require_role(hiring_manager, auditor)`), which internally calls `list_jobs(user_id=<resolved_user_id>)` and returns only that user's assigned jobs. The Flask viewer updates its job list view to hit this endpoint instead of `GET /jobs` for non-admin users, surfacing "your assigned requisitions" as the default experience.

### 8. Build reconciliation and status (implemented 2026-07-24)

FU-6 shipped in 10 TDD slices on `feat/fu6-job-assignment-scoping`, all gate-green
(`./scripts/verify.sh all`): the `job_assignees` table (§1); `job_assignee_service`;
the assign/unassign routes (§2); the `scoped_user_id_or_403` helper; row-scoping on
the jobs, résumé (+reveal), and shortlist reads (§3/§5); auditor read-logging (§6);
`GET /my/jobs` (§7); and `role` on `GET /auth/cas/user`. Merge-blocking gates:
**reviewer APPROVE** (12 mutations caught across the scoping predicates, the helper,
reveal ordering, `/my/jobs`, and auditor logging), **security PASS** (no
critical/high; IDOR, fail-open, existence-oracle, and assignment-privilege surfaces
all cleared). `ranking-evals` n/a — no scoring code touched.

**Scoping keys off the CAS session role, not the API key (the crux reconciliation).**
§3/§4 as written assumed the key carried identity — but that predates FU-5's CAS
pivot. As built (`core/frontend/api_client.py`), the Flask viewer presents ONE shared
`recruiter` key for every browser user, so the key-derived `Role` cannot identify a
hiring_manager. Scoping therefore keys off `resolve_user().role` (the CAS session
identity, cryptographically tied to a real login), via `scoped_user_id_or_403(user,
key_role)` in `core/src/api/deps.py`: a real hiring_manager session scopes to
`user.id` even under the shared recruiter key; a hiring_manager *key* with no/mismatched
session **403s** (fail-closed, never served unscoped); admin/recruiter/auditor and the
dev-anonymous sentinel are unscoped. `GET /my/jobs` is the exception — it scopes to the
session `user.id` **directly** (not via the helper), so it always returns the caller's
own assignments regardless of role.

**Auditor read-logging scope (§6 as built).** Only the four deliberate single-subject /
bulk reads are logged (`read_job` on `GET /jobs/{id}`, `read_resume` on `GET
/resumes/{id}`, `read_shortlist_entry` on `GET /shortlist/{id}`, `read_shortlist_export`
on the export), written **after** a successful read (a 404 logs nothing). The polled
list-index routes (`GET /jobs`, `/jobs/{id}/resumes`, `/jobs/{id}/shortlist`) are NOT
logged — the frontend polls them every 3s and they carry no specific subject.

**Accepted residuals / deferred work:**
- **Assign-route session-role check (security, latent LOW).** The assign/unassign routes
  gate on the *key* role (`require_role(ADMIN, RECRUITER)`) plus a real-assigner check
  (rejects `None`/the dev-anonymous sentinel), NOT the session role. Not reachable today
  — the Flask viewer exposes no assign/unassign proxy, and a direct hiring_manager/auditor
  caller presents their own key and is correctly 403'd. **When the assignment UI/proxy
  (Consequences §1) is added, `_require_real_assigner` must ALSO verify
  `user.role in {admin, recruiter}`** (the same session-role reasoning the read routes use).
- **The Flask viewer default-view switch (§7 frontend half) is deferred.** Exposing `role`
  on `/auth/cas/user` (the API enabler) shipped here; the Flask change that makes a
  hiring_manager land on `/my/jobs` needs the session-cookie forwarding + `get_cas_user()`
  helper that live on the separate branch `fix/upload-and-progress-ux` (PR #30). It lands
  as a small follow-up on `main` once both that branch and this one merge — building it on
  either branch alone would duplicate/conflict the Flask plumbing.
- **`JobAssigneeCreate.note` is now capped** at 200 chars (was unbounded — reviewer +
  security low finding, closed in-branch), since it rides into `audit_log.details`.
- **No role-provisioning path** for `hiring_manager`/`auditor` (carried from FU-5 §10a):
  a first CAS login lands as `recruiter`; testing/using scoped roles needs an admin to set
  `users.role` via SQL until an admin endpoint exists. FU-6's integration fixtures seed
  these roles directly.

### 9. Amendment (2026-08-07, `fix/session-role-on-writes`) — reveal scoping is SUPERSEDED, not extended

**§3/§5's "scoped hiring_manager can reveal an assigned job's résumé" line (the `POST
/resumes/{id}/reveal` bullet in §3, struck through above) is wrong and must not be
followed.** It was built in good faith in FU-6 slice 6 and had its own green tests
(`test_reveal_scoped_hiring_manager_{assigned,unassigned}_*` in
`core/tests/unit/test_route_reveal.py`), but it directly contradicted two things that
predate it and were never reconciled against it:

- `_REVEALERS = (Role.ADMIN, Role.RECRUITER)` (`core/src/api/routes/resumes.py`,
  ADR-018/D2) — reveal has always been admin/recruiter ONLY at the key-role layer.
- The HR-facing ranking-metrics explainer, which states plainly that a hiring manager
  cannot reveal a candidate's identity and must go through a recruiter.

Both statements were simultaneously true in the test suite and simultaneously false in
production, because `core/frontend/api_client.py` sends the ONE shared `recruiter` API
key for every browser user (ADR-019's own crux reconciliation, §8 above) — so the
key-role gate never saw a real hiring_manager key to reject, and the scoping gate in §3
happily let an assigned hiring_manager SESSION through underneath it. Neither test
suite ever exercised the actual production combination (recruiter key + hiring_manager
session) until `fix/session-role-on-writes`'s `test_route_resumes_session_gate.py`
did, at which point the contradiction became two tests failing no matter which way it
was resolved.

**Human decision:** reveal is recruiter/admin only, full stop. A hiring_manager
SESSION now gets 403 on `POST /resumes/{id}/reveal` unconditionally — assigned or not
— via the new `require_session_role(*_REVEALERS)` gate (ADR-033). The blind-review
value proposition wins: un-blinding stays a genuine two-person action (a hiring manager
requests, a recruiter/admin reveals), not something a hiring manager can trigger
themselves for their own assigned jobs. The two outcomes (assigned vs. unassigned) are
now deliberately indistinguishable to the caller — both 403, no audit row, no
decrypt — which is a strict improvement on §5's existence-oracle rationale below: a
hiring_manager can no longer probe assignment status via this route AT ALL, not even
through the assigned/unassigned 200-vs-404 split.

**§5's 404-not-403 existence-oracle rationale is UNCHANGED for the read routes** (`GET
/jobs`, `GET /jobs/{id}`, `GET /jobs/{id}/resumes`, `GET /resumes/{id}`, `GET
/jobs/{id}/shortlist*`, `GET /shortlist/{id}`) — those still scope a hiring_manager
session via `scoped_user_id_or_403` exactly as built, and an unassigned/nonexistent
resource there is still 404, not 403. This amendment is scoped to the one write route
(reveal) that also changed key-role-vs-session-role gating; it does not touch §4's
role-scoping table for reads at all.

`scoped_user_id_or_403`'s call inside `reveal_resume` (`resumes.py`) is **retained**,
not removed — see ADR-033 for why it is correct, structurally unreachable-for-
hiring_manager defence in depth rather than dead code, and why extending it further
(ROADMAP A1 step (iii)) is deliberately not needed once `require_session_role` gates
every write route's allowed set down to `{admin, recruiter}`, both unscoped by design.

## Consequences

- **The shipped `hiring_manager` API key becomes useless until assignments exist.** On first deployment, any hiring_manager credential resolves to an empty job set (no assignments yet). An admin must run an assignment backfill (bulk INSERT into `job_assignees` from a CSV or JSON file naming which users own which jobs) or use the `POST /jobs/{job_id}/assignees` route to assign jobs one by one. The assignment workflow must be documented in the deployment guide so a deployer knows to backfill **before** distributing hiring_manager credentials to users.

- **Every scoped route needs a negative test.** The test suite gains a test matrix: for each of the eight affected routes (GET/POST/DELETE on jobs, resumes, and shortlist), verify that (a) an unscoped user (admin/recruiter/auditor) sees all rows, and (b) a scoped user (hiring_manager) assigned to a subset of jobs sees only their assigned rows, and (c) a scoped user with zero assignments sees an empty list, and (d) a scoped user requesting a job they are **not** assigned to gets 404. That is a 4×8 = 32-test addition to the suite, structured to reuse parameterized fixtures.

- **Hiring_manager credentials need a separate Postgres user table to exist first.** ADR-019 must land before this feature ships. There is no way to assign a job to a user without a `users` table to FK against.

### Accepted residuals

- **No delegation or temporary access.** A hiring_manager cannot grant a temporary read-only or read-write window to another user; there is no "assign this job to user A for one week." Delegation is out of scope for v1; a hiring_manager who needs help enlists an admin to add a new user with recruiter scope (global, permanent). This is a UX limitation worth surfacing to the product team.

- **No bulk assignment UI.** The MVP assignment flow is `POST /jobs/{job_id}/assignees` (one assignment per request) or a manual CSV backfill. A bulk "assign 20 jobs to user X" UI is deferred to a future feature.

- **Reverse-match results remain unavailable to hiring managers.** `GET /resumes/{id}/match-results` is admin/recruiter only because it carries unredacted match evidence (see ADR-018, lines 291–297). A scoped hiring_manager cannot fetch reverse-match results even for their assigned jobs — there is no scoped route for this. This is deliberate: reverse-match is a tool for understanding candidate fit, not a routine hiring workflow, and the evidence payload carries unredacted sourcing. If a hiring_manager needs to see reverse-match results, an admin or recruiter (with global access) must run the query and share the results. If future versions require hiring_manager access to reverse-match data, a separate scoped route with narrower evidence redaction must be designed.

- **Résumé-level scoping is fully implied by job scoping.** Résumés are reachable only via their job (`POST /jobs/{id}/resumes` is the only way to add a résumé; `GET /jobs/{id}/resumes` lists by job). A scoped user cannot reach a résumé except through their assigned job. For direct access via `GET /resumes/{id}`, the route handler must apply the job-assignment scoping predicate (verifying the résumé's parent job is in the user's assigned set) before returning the résumé. There is no standalone "all résumés I've uploaded" or "all résumés matching this skill" endpoint. No additional résumé-level membership table is needed.

- **Assignment records live in Postgres, not Neo4j.** Job membership is a relational fact (who is assigned to which job), not a graph relationship. The Neo4j `jobs` nodes are untouched; no MANAGES/ASSIGNED_TO relationship is added. All scoping queries hit Postgres (it's the transactional source of truth); this keeps authorization fast and tightly coupled to the source table.

- **Auditor global visibility requires stakeholder ratification.** This ADR gives auditors global, unscoped read access with compensating audit logging (see §6 above). This is a policy call, not a technical one. Organizations may prefer auditors to be scoped to assigned jobs instead. Seek stakeholder alignment before deploying auditor credentials.

## Alternatives Considered

- **Named cohorts (job groups).** Instead of assigning users to jobs one-to-one, assign users to named groups of jobs (e.g., "EMEA hiring" = jobs matching a regex on title/department/location). More scalable for a manager owning a requisition family. Rejected because (a) two overlapping permission paths (user → job direct + user → cohort → job) make effective access hard to audit and test; a bug in either path leaks data. (b) Cohorts add a UI for naming/managing groups, and that adds scope to FU-6. Start with direct assignment; if many-to-many job sets become painful, lift to cohorts in a later feature.

- **Department or organization-unit scoping from CAS attributes.** A hiring_manager's department (released by CAS on login, see ADR-019 §2) could determine their visible job set automatically — no assignment needed. Rejected because (a) institution-dependent; not every organization's CAS releases department as a claim. (b) Requires user-login infrastructure (ADR-019), which is a separate precondition. (c) Attribute release policies can change; an organization's CAS admin might release "department" to some endpoints and not others, creating a maintenance headache. (d) A job can belong to multiple departments (e.g., a cross-functional role) — the CAS attribute is a single value. Direct assignment is simpler and more flexible.

- **Doing nothing.** Accepted for ADR-018 v1. Rejected now because FU-6 is explicitly asked for: organizations need hiring_managers scoped to their own requisitions.
