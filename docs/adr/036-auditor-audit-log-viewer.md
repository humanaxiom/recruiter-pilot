# ADR-036: The auditor's access-record viewer, and the `audit_log` read path (`feat/auditor-audit-log-viewer`)

**Status:** Accepted (closes ROADMAP.md Phase 1.4 — the last remaining reason behind demo guardrail 2;
builds on [ADR-019](019-cas-identity-attributable-audit.md) §6/§7 for the `audit_log` sink and
[ADR-034](034-auth-boundary-fails-open.md) §2 for `require_session_role`'s `user is None` → 403)
**Date:** 2026-08-13

## Context

### The finding is sharper than the plan said

The plan described this as "the audit-log viewer has not been built yet". Investigation found something
worse:

```
grep -rn "FROM audit_log" core/src/   →   nothing
```

`audit_log` is written by **nine call sites** — reveals, résumé withdraw/reinstate, job-assignee
assign/unassign, role changes, and job-service writes. **Nothing in the application reads it.** Not a
route, not a service function, not a UI.

The one audit route that does exist, `GET /audit/reveals-legacy`, reads `reveal_audit` — the table
**frozen** at FU-5 slice 8 when reveal was cut over to `audit_log`. It returns pre-cutover history and
nothing that has happened since.

So an auditor's position was not "the screen is missing." It was that **producing the access record meant
an engineer running SQL against production by hand** — which is also, incidentally, an unaudited read of
the audit log.

### Why this was the last pilot blocker

ROADMAP guardrail 2 ("sign in as admin or recruiter only") stood on two legs after ADR-034 closed the
authorization defects: CSRF coverage, closed by [ADR-035](035-csrf-on-every-browser-write-route.md), and
this. Issuing an auditor account to a person who then cannot perform a single auditor task is worse than
not issuing it — it implies a capability that does not exist.

## Decision

### 1. `redact_audit_details` — an allowlist, written before anything else

`audit_log.details` is the only free-text column in the table. Two writers populated it when this ADR
was written; the D1 amendment below added a third:

| Action | Payload | Disposition |
|---|---|---|
| `role_changed` | `{"old_role", "new_role"}` | **Disclosed.** Enum-shaped, non-PII, and the most audit-relevant detail the table holds. |
| `withdraw_resume` | `{"reason": <operator prose>}` | **Withheld by default, revealable on request** — see the D1 amendment below. |
| `reveal_audit_detail` | `{"revealed_action": <action>}` | **Disclosed.** Enum-shaped, non-PII, and withholding it would make the trail of reveals unreadable without revealing it in turn. |

**An allowlist, not a blocklist**, and the distinction is the point. A blocklist ("hide `reason`") protects
against today's two writers and silently leaks the third one somebody adds next year — the ROADMAP A7
shape, an invariant that holds only as long as nobody extends the code. Anything unclassified is
**withheld by default**, so a new `record_audit` caller inventing a new key gets withholding for free
until someone classifies it.

**Scoped by action, not by bare key name.** `old_role` is safe *because of what `role_changed` writes
there*; an allowlist keyed only by name would let a future writer smuggle content through a
familiar-looking key.

**Withheld, not dropped.** The auditor is told a value *exists* and is not shown it. An audit view that
silently omits fields is worse than useless — the reader cannot distinguish "no reason was recorded" from
"a reason was recorded and you are not being shown it", and those are materially different facts in a
compliance review.

**Per-key, not per-row**: withholding one value must not blind the auditor to the classified ones beside
it. And it never raises — `details` is `jsonb`, so a legacy row could hold a scalar; an audit read must
degrade, never 500.

### 2. `list_audit_log` — and two SQL details that are load-bearing

**The `LEFT JOIN users` is not a stylistic choice.** `actor_kind='service'` rows carry a NULL
`actor_user_id` by CHECK constraint, so an INNER JOIN would **silently hide every one of them** — and
those are precisely the events an auditor most needs, because an unattributable `actor_service='api'`
write is the signature of the ADR-034 exploit. A viewer that quietly dropped them would be worse than no
viewer, because it would look complete.

**`ORDER BY a.occurred_at DESC, a.id DESC`** — the tiebreak is not decoration. Rows written inside one
statement share `occurred_at` to the microsecond, so the timestamp alone is not a total order: without the
tiebreak the same row can appear on two pages while another appears on none.

Both are proven against a **real Postgres**, per CLAUDE.md's rule that the unit suite structurally cannot
prove a property of a real database. The redaction is proven wired in there too — that it *exists* and
that the read path *calls it* are different claims, and only the second protects the candidate. (The
ADR-031 inert-PII-scan lesson.)

Never joins `resumes` or `jobs`, mirroring `/audit/reveals-legacy`'s own discipline: nothing decrypts, so
no candidate PII can reach this path even by accident.

### 3. `GET /audit/log` gated on the **session** role alone

This is the decision most likely to look like an oversight later, so it is recorded plainly. Writing it
the obvious way — `require_role(ADMIN, AUDITOR)` on the API key, mirroring its sibling — would have made
the route **unreachable by the only person it was built for**:

> The Flask BFF presents **one fixed `recruiter` key** for every browser it serves (FU-4/D6,
> `api_client.build_client`), while forwarding the real user's session cookie. A keyed admin/auditor gate
> therefore 403s every real auditor at the only door they will ever use — **while every unit test that set
> the key role to `admin` passes.**

That is exactly why `/audit/reveals-legacy` has no page today. Session-role gating is how the other
browser-reachable privileged surface already works (`users.py::_require_admin_session`), and a regression
test now pins the combination the browser actually produces: **recruiter key, auditor session**.

It is also the *stricter* choice on the axis that matters. `require_session_role` 403s on `user is None`
(ADR-034 §2), so reading the record of who looked at whom is itself attributable to a person, and a shared
service key is by construction not a person.

**This is a judgement about this surface, not an answer to ADR-034's carried question** about whether
machine readers are legitimate in general. That question stays open.

### 4. A read-only page, and a nav link

`GET /audit`, admin + auditor. No write control and no backend write route it could call — an audit
surface that can be edited from the browser is not an audit surface. **Superseded in one narrow way by
the D1 amendment below**, which adds the page's single POST control: it performs an audited READ, not a
mutation, and a test now pins that every POST target on the page is a reveal route. Unattributable service events are
**named** (`service: api`) rather than left as a blank cell: they are what an auditor is hunting, and a
blank reads as missing data rather than as a finding.

The nav link matters as much as the route. An auditor who cannot find the page is still an auditor who
cannot do their job, so link visibility is tested, not assumed.

The page **redacts nothing itself** — it renders what the backend already filtered, so the disclosure
boundary has exactly one implementation rather than two that can disagree.

Admin sees the page too: an administrator investigating a report should not have to be re-roled into
`auditor` to read the record they are being asked about.

## Consequences

- The auditor role is usable. Guardrail 2's last leg is gone (see ROADMAP A0 §2).
- `audit_log` has a read path, so producing an access record no longer requires hand-written SQL against
  production.
- Adding a new `record_audit` call site with a novel `details` key shows as `withheld` until classified —
  visible, safe, and a prompt rather than a leak.

### Accepted residuals

- ~~**Whether an auditor should see withdrawal reasons is UNRESOLVED and needs a human.**~~
  **RESOLVED — see the D1 amendment below.** The question was carried here deliberately rather than
  answered by implementation, which is how ADR-033's residual became ADR-034; the same route worked
  again.
- **No export.** An auditor who needs to hand the record to someone else still screenshots or asks an
  engineer. A CSV export is the obvious next slice and was kept out of this one.
- **No date-range filter** — `action` only, plus pagination. Enough to answer "show me every reveal";
  not enough to answer "show me everything in March" without paging.
- **No full-text search** over `context`.
- **`/audit/reveals-legacy` remains browser-unreachable** (keyed gate, no page). Frozen historical data;
  reaching it still needs a direct API call with an admin/auditor key. Not fixed here because widening its
  gate is a change to a route this slice otherwise does not touch.
- **Not verified in a live browser** — same as ADR-035: the stack does not boot in this environment
  without a human running `./scripts/quickstart.ps1`.

## Alternatives considered

- **Extend `/audit/reveals-legacy` to read both tables.** Rejected: it is documented as a view of a frozen
  table and its shape (`RevealAuditItem`) has no room for actor kind, subject type, or details. Merging
  the two would blur a deliberate FU-5 boundary.
- **Blocklist the `reason` key.** Rejected — §1.
- **Render `details` as raw JSON.** Rejected: it is the shape most likely to spill an unclassified value
  into the page the day someone adds one, and it reads badly for the non-engineer this page is for.
- **Keyed `require_role` gate** for symmetry with the sibling route. Rejected — §3; it would ship the page
  broken for auditors while passing every test that set the key role to admin.

## Gate state

`./scripts/verify.sh all` green, exit code captured directly rather than piped: `EXIT=0`, **4376 unit tests
@ 94.20% coverage, 488 integration tests**.

---

## Amendment — D1 = option C: the separately-audited reveal (2026-08-20)

**Decided 2026-08-19 by the product owner; implemented 2026-08-20.** This ADR carried "whether an
auditor should see withdrawal reasons" as a named residual for five sessions. The answer is **option C
— reveal on request, separately audited** (the recommended default; the full memo, including the three
options not taken, is in [OPEN_DECISIONS.md](../OPEN_DECISIONS.md)).

### What was built

`POST /audit/log/{audit_id}/reveal`, session-gated admin + auditor, returning one row's **un-redacted**
`details`. Its shape is copied, on purpose, from `POST /resumes/{id}/reveal` — the audited un-blinding of
candidate PII. Same class of data, same mechanism.

**Ordering: read → gate → audit → return.** The audit row is written and autocommitted before the value
leaves the handler, restating ADR-016 / ADR-019 §7. A crash mid-response leaves a record of a reveal that
may have been seen — never a disclosure with no record.

**A refused reveal writes nothing**, matching `reveal_resume`'s discipline for a scope-blocked reveal. An
audit trail that records reads which never happened is not a trail an auditor can rely on. The three
refusals are distinct facts: no attributable session → 403 before anything is read; no such row → 404;
action off the revealable allowlist, or no withheld object → 403.

**`_REVEALABLE_DETAIL_ACTIONS` is a second fail-closed allowlist** beside `_DISCLOSABLE_DETAIL_KEYS`, and
for a sharper reason: it governs what can be *un*-withheld at all, so a blocklist would hand every future
`record_audit` caller a reveal path for its new key by default. Two invariants between the two allowlists
are **enforced by tests, not stated in a comment** — no action is both freely disclosed and revealable,
and every revealable action actually withholds something.

**The viewer does not know the allowlist.** The access-record page offers the control wherever the backend
withheld a value and lets the backend fail-close. One implementation of the disclosure rule rather than
two that drift — §1's own argument, applied to the layer above it.

### Why C, and why the coupling with D2 mattered

C rests on the reveal being **attributable**. Before 2026-08-19 `reveal_service` fell back to the literal
`actor = "api"` when no identity resolved; **D2 = option B** (ADR-034's amendment, PR #95) closed that, so
every read now requires a real principal. A reveal a bare service key could perform would launder an
unattributable read through a route whose only justification is that it records one — worth strictly less
than the status quo. D2 answered first is what made D1 worth building, and the two were sequenced that way
deliberately.

### Two defects this work surfaced, both invisible to a green suite

1. **Both audit reads could 500 on one malformed row.** `redact_audit_details` has promised since Phase 1.4
   that "an audit read must degrade, never 500" — and it does, for anything handed to it. But both reads
   called `json.loads` on the raw `jsonb` column first, and a row whose text is not valid JSON raised
   before the promise could apply. One such row would have taken down the entire access-record page, which
   is the first thing an auditor opens. `_decode_details` now guards both and degrades **fail-closed**: an
   undecodable payload stays a non-`dict`, so it is withheld wholesale and the reveal refuses it. The
   ROADMAP A7 shape exactly — the invariant was in prose, and nothing held it. The matching guard was
   added to the template, which called `.items()` unconditionally on the same column.

2. **The withdraw form never collected a reason** — so option C would have shipped as a control with
   nothing to reveal, *ever*. The route has always read `request.form.get("reason")`, the backend has
   always accepted one, and a test has always proved the forwarding worked — by POSTing the field itself.
   No rendered form contained the input, and all five withdrawals in the live database have a NULL
   `details` as a result. **Found by running the product**, exactly as ADR-043's `polling` defect was.

The second one carries a policy consequence, not just a UI one. D1's memo turns on the asymmetry that
prose already recorded was typed under a withheld expectation, and that disclosing it later changes the
status of data already collected. C keeps existing rows closed and opens new ones deliberately — which
only holds if the person typing a **new** note is told an auditor can ask to see it. The form now says so.
Without that line, C reproduces the retroactive problem it was chosen to avoid, one row at a time.

### Verified live, not merely green

Withdrew a résumé through the real UI with a reason; saw the access record offer **Reveal note** beside a
withheld value; revealed it; confirmed the prose rendered, that a `reveal_audit_detail` row landed with
`subject_id` pointing at the withdrawal row, and that a page reload re-masks it; then reinstated the
résumé.

### Accepted residuals

- **With CAS disabled the reveal is attributed to `service:dev-anonymous`, not a person** (ADR-019 §10b —
  the sentinel is not a `users` row and cannot be recorded as `actor_kind='user'` without violating the
  FK). This is correct, and it means **the attributability that justifies option C only materialises with
  CAS on**. The live probe above recorded exactly that. Not a gap in this work; a reason the pilot must
  run authenticated.
- **The shortlist card's quick withdraw still collects no reason.** Only the résumé-detail form does. A
  text input on every card in a shortlist grid is bad UX, and the detail page is where a considered note
  belongs — but it does mean the fast path still records `None`. Recorded in the ROADMAP rather than fixed.
- **No bulk reveal, and deliberately none.** Purpose-limited access is the entire difference between C and
  B; a "reveal all on this page" control would be B with extra audit rows.
- **Reveals are not rate-limited.** An auditor could reveal every withheld row one click at a time and
  arrive at B's disclosure with a trail behind it. That trail is the control — the point of C is that the
  access is *recorded*, not that it is *prevented* — but nothing alerts on the pattern.
