# ADR-034: The auth boundary failed open in the shipped configuration (`fix/auth-boundary-fails-open`)

**Status:** Accepted (closes ROADMAP.md A1b P0 — "the auth boundary is OFF in the shipped config";
**supersedes [ADR-033](033-session-role-enforcement-on-writes.md)'s claim to have closed its own stated
worst case**, and reverses that ADR's §1 `user is None` → PASS contract; depends on
[ADR-018](018-rbac-keyed-roles.md) for the keyed-role model and
[ADR-019](019-cas-identity-attributable-audit.md) for `resolve_user`/CAS session identity)
**Date:** 2026-08-11 (merged 2026-08-13, PR #72, squash `299b529`)

## Context

ADR-033 gated all 13 write routes behind `require_session_role(*allowed)` and recorded ROADMAP A1 as
resolved. A retrospective `reviewer` pass over `ab6c278` — the pair of merge-blocking gates the human
authorised merging #68 without — found that **ADR-033 did not close its own stated worst case**, and
direct probing of the live stack escalated it.

### The defect, as shipped

`auth_enabled` is `False` iff all four role keys are empty (`settings.py:253-263`). **No `API_KEY_*`
existed anywhere**: zero matches in `docker-compose.yml` and `compose.cas.yml`, none in `.env.example`,
none in the running container. Compounding it, only `./core:/app` is mounted while `Settings` uses
`env_file=".env"`, which inside the container resolves to `/app/.env` = `core/.env`. **There was no
channel by which key auth could be turned on at all.**

So `resolve_role` returned `Role.ADMIN` for every request (`deps.py:102-103`), and both
`require_role_assigned` (`deps.py:299-301`) and ADR-033's new `require_session_role` passed on
`user is None`. **Two gates, ANDed, both vacuous.**

Proven against the live stack with **no cookie and no key**:

```
GET   /jobs                            -> 200, real job data
GET   /audit/reveals-legacy            -> 200
PATCH /jobs/{id} {blind_review:false}  -> 200, column really flipped
```

…after which the same caller reads `candidate_name: "Jane Q Candidate"` un-redacted. The flip audits as
`actor_service='api'` — unattributable to any person. **Reads were open too**, which the earlier audit had
not reached: no session was required at any point, so anyone who could reach the port had the whole API,
including the audit log.

### Why nothing caught it

Config-dependent, and every unit test mocks `resolve_user` — the suite **structurally cannot** see it.
`validate_startup_auth_config` exists precisely to *"refuse to boot on an auth configuration that would
silently fail open"*; it checked stale legacy keys and key collisions but **not
CAS-enabled-with-zero-role-keys, which was the shipped default**. The invariant lived in that docstring
with nothing enforcing it — the **third occurrence of the ROADMAP A7 pattern in one session**.

## Decision

### 1. F1b — make it impossible to ship the boundary off (primary)

`validate_startup_auth_config` now **raises** on `cas_enabled=True` with zero role keys configured. The
supporting channel is built in the same change so the boot stays one command: `docker-compose.yml`'s
`&app_env` forwards all four `API_KEY_*` (with `API_KEY_RECRUITER` also on the frontend service so the BFF
keeps working once auth is on), `.env.example` defines them, and `quickstart.ps1` generates them beside
`PII_KEY`/`SKILL_HASH_SALT`.

This is the primary fix because it is the one that cannot be undone by a future misconfiguration: the
failure mode is now a loud refusal to boot rather than serving everything to everyone.

### 2. F1a — `require_session_role` 403s on `user is None`

**Reversed from ADR-033 §1**, which let `user is None` PASS on the theory that the gate "never judges the
ABSENCE of a session, only a REAL session's role". The live audit proved that theory wrong.

**Human decision: a valid API key alone is never sufficient for a write.** Every write route now requires
a real, resolvable CAS session.

This required rewriting **13 tests that pinned the fail-open**, each docstringed *"the case most likely to
be broken by an over-eager fix"* — written to protect a behaviour we have since learned is the
vulnerability. They were **rewritten, not deleted**, with the reversal explained in place. The tester found
the same defect pinned in **7 further tests**, including `test_blind_review_audit_pg.py` Case 2, which
asserted *the exploit itself succeeds* as correct behaviour.

### 3. F5 — `users.active` enforced in all four session gates

`active` was consulted by none of them, while `session_service.refresh_if_needed` slides expiry forward on
every request — so **a deactivated account's session need never expire**. All four gates now 403 on
`user.active is False`. In `scoped_user_id_or_403` the check sits *inside* the `hiring_manager` branch
deliberately: falling through to the `None` (unscoped, company-wide) branch would be a **worse** outcome
than the status quo — a deactivated account seeing every row instead of none.

### 4. F4 — the 403→500 regression ADR-033 introduced

A `hiring_manager` or `auditor` clicking any write control got an unhandled Flask 500. Caught on all six
routes **plus `resume_reveal`** (`app.py:866`), which the tester flagged as the identical gap outside their
own scope. Write controls are now role-gated in the templates as defence in depth.

## Consequences

- **Operational, and intended:** the stack **refuses to boot** until `./scripts/quickstart.ps1` is re-run
  to generate the keys. That is the fix working. `.env` is permission-protected, so this is a human step.
- The `user is None` → PASS contract documented in ADR-033 §1 is dead. Anything citing it is wrong.
- A bare service-key caller can no longer perform any write. If a legitimate machine writer ever exists, it
  needs a real principal, not a shared key — see the carried question below.

### Accepted residuals

- **Carried, not decided: `require_role_assigned` still passes on `user is None`**, so a bare
  service-key reader still gets unscoped reads. F1b closes it *in practice* (there is no longer a
  configuration in which the boundary is off). Whether machine readers are legitimate at all is a product
  question — **recorded rather than silently answered**, because answering it in code would have decided
  the product question by implementation, which is how ADR-033's residual became this ADR.
- **F3 — three flaky reveal tests** — deliberately out of scope.
- **F7 — dead `_EXISTS_SCOPED_SQL`** — deliberately out of scope.
- **CSRF still covers 3 of 12 browser state-changing routes** (ROADMAP A1 step (iv) / Phase 1.3) —
  unchanged by this ADR and still a pilot blocker.

## Alternatives considered

- **F1a alone, without F1b.** Rejected: it closes the write path but leaves reads open and leaves the
  boundary switchable-off by configuration. The defect was never really "the gate is wrong" — it was "the
  gate could not be turned on".
- **F1b alone, without F1a.** Rejected by the human decision in §2: relying solely on a key being present
  would mean a leaked shared key is a full write credential with no attributable actor, which is the
  `actor_service='api'` untraceability that made the original finding severe.
- **Answering the `require_role_assigned` question in the same change** (403 on `user is None` for reads
  too). Rejected as out of scope: it is a product decision about whether machine readers exist, and this
  branch was already reversing one contract that had been decided by implementation rather than by a human.

## Gate state

`./scripts/verify.sh all` green — **re-run independently of the implementing agent, with the exit code
captured directly rather than piped**, after a piped invocation earlier in the same session fooled one
agent into reporting success on a non-zero exit. `EXIT=0`, **4296 unit tests @ 94.39% coverage, 482
integration tests**. CI green on PR #72 (all checks SUCCESS) before merge.

---

## Amendment — D2 = option B closes the carried question (dated 2026-08-19)

**The product decision answered.** `docs/OPEN_DECISIONS.md` §D2 recorded an undecided question: *"are
machine readers legitimate at all?"* ADR-034's "Accepted residuals" carried it forward — `require_role_assigned`
still passed on `user is None`, so a bare service-key reader got unscoped reads. Writes were already closed
by F1a. Product answered: **every read now requires a real principal.** D2 = option B (closes the read path
symmetrically with writes).

**What changed: `require_role_assigned` (deps.py:351-393).**

- **Before:** `user is None` → PASS (lines 366-372 in the pre-D2 version documented the theory: "this gate never
  judges the ABSENCE of a session, only a REAL session's role").
- **After:** `user is None` → **403** (the 403 is reversal of ADR-033 §1's contract, documented inline with
  the 2026-08-19 reversal date and the D2 decision reference). `require_role_assigned`'s docstring
  (`deps.py:354-393`) now explains the three cases: `user is None` (no session at all, now 403), `user.role is
  None` (real session, no assigned role, 403 as before), any other real user (PASS if active, 403 if
  deactivated — F5, unchanged).

**Measurement of legitimacy: no keyed tooling depends on bare-key reads.** Applying the change and running
the full suite broke exactly **three tests**, all of which asserted the old behaviour being removed
(`core/tests/unit/test_api_deps_d2_close_unscoped_reads.py`; `core/tests/integration/test_close_unscoped_reads_pg.py`;
and one in `test_no_role_fail_closed_pg.py`). The integration tests pin both entry points that D2 could have
broken:

- **CAS-off dev boot:** `resolve_user` (`deps.py:261-313`) returns the `dev-anonymous` sentinel on line 288-297,
  **before** the `if not ra_session: return None` branch on line 299-300 even runs. The sentinel's
  `role="admin"` means `user is not None`, so the 403-on-None rule never applies to this path. Untouched.
- **Flask viewer forwarding session:** `api_client.py:117-128` forwards both the fixed recruiter key (header)
  **and** the browser's `ra_session` cookie (on line 121-124, reading it from `request.cookies` inside a
  `has_request_context()` guard). A keyed request that carries a session is never `user is None`, so still
  200s. Untouched.

The eval harness never calls the API directly — it runs `orchestrator.py` and asserts corpus metrics.
No machine reader exists in this product today.

**Survivability fact 1: the CAS-off dev boot is untouched.** The dev-anonymous sentinel is a real `User`
object, not `None`, so any `user is None` gate structurally cannot affect it. `resolve_user`'s docstring
records this as the load-bearing point.

**Survivability fact 2: the Flask viewer is untouched.** It is the only shipped entry point that carries
both a key AND a session. Forward-and-back are the intended pattern: a key with a session reads exactly as
before; a key alone is now rejected at the router gate before route bodies run, failing safely closed rather
than returning unattributable data.

**The sharpest instance closed:** a bare key reading `GET /audit/reveals-legacy` with no session now 403s
instead of 200, unattributable audit-log access (`actor='api'`) closed. Same shape as ADR-036's audit-access
problem, different vector (keyed instead of sessioned).
