# Architecture Decision Records

43 records, written at decision time. They explain **why the live code is shaped
the way it is** — read the ones covering an area before changing it.

**These are historical records and are not edited retroactively.** Some reference
build-phase documents (`EXTRACTION_PLAN.md`, `OPEN_DECISIONS.md`,
`docs/activity/`, the long-form roadmap) that were deliberately left behind when
this pilot repository was forked clean. Those links dangle on purpose; the
decisions they record still hold.

**The bar for writing a new one** (`CLAUDE.md` §Economy 0a) — all three must hold:
live alternatives a competent engineer would have chosen between; expensive to
reverse; and irrecoverable from the code, its tests and the commit message.
Failing any one, the reasoning goes in the commit message. Amend an existing ADR
rather than adding a sibling.

---

## Privacy, redaction and PII

The load-bearing set for an HR product. Read all of these before touching
anything that displays or stores candidate data.

| ADR | Subject |
|---|---|
| [007](007-phase3-ingest-parse-hardening.md) | PII-at-rest posture; encryption on parse |
| [008](008-skill-graph-pii-by-construction.md) | Skill-graph projection — PII elimination by construction; the salted canonical key |
| [011](011-display-redaction-read-export-boundary.md) | Display redaction — the read/export boundary |
| [016](016-audited-reveal.md) | Audited candidate reveal |
| [022](022-uncited-evidence-quote-scrub.md) · [023](023-evidence-verifier-hardening.md) | An uncited evidence quote is scrubbed like a fabricated one |
| [026](026-resume-withdrawal-lifecycle.md) | Résumé lifecycle — withdrawal, exclude-and-retain |

## Identity, authorization and audit

| ADR | Subject |
|---|---|
| [018](018-rbac-keyed-roles.md) | RBAC — keyed roles |
| [019](019-cas-identity-attributable-audit.md) | CAS identity, user records, attributable audit |
| [020](020-per-job-assignment-scoping.md) | Per-job assignment and row-level scoping |
| [025](025-user-admin-roles.md) | User administration; no role by default |
| [033](033-session-role-enforcement-on-writes.md) | Session-role enforcement on write routes |
| [034](034-auth-boundary-fails-open.md) | The auth boundary failed open in the shipped configuration |
| [035](035-csrf-on-every-browser-write-route.md) | CSRF on every browser state-changing route |
| [036](036-auditor-audit-log-viewer.md) | The auditor's access-record viewer, and the audited reveal of a withheld reason |

## Ranking, matching and evidence

| ADR | Subject |
|---|---|
| [009](009-matching-engine-port.md) | Matching engine — stages and orchestrator |
| [010](010-shortlist-reverse-match-write-path.md) | Shortlist + reverse-match write path |
| [015](015-evidence-chunk-expansion.md) | Evidence chunk-id expansion |
| [024](024-configurable-shortlist-size.md) | Per-job configurable shortlist size (top P%) |
| [028](028-education-field-relevance.md) | Education field-of-study relevance |
| [029](029-fail-closed-ranking-fu7.md) · [037](037-stage3-fails-closed-on-non-llm-error.md) | Fail-closed ranking, including on non-LLM errors |
| [031](031-why-this-rank-defense-pack.md) | "Why this rank?" defense pack |
| [038](038-gate-the-bait-below-strong-ordering.md) | Gate the bait-below-strong ordering the corpus only asserted in prose |
| [039](039-stage1-recall-is-job-scoped.md) | Stage-1 recall searches the job's pool, not the whole database |
| [040](040-evidence-cliff-disclosure.md) | Disclose the evidence cliff instead of rendering a fabricated 0% |
| [041](041-sub-score-measurement-markers.md) | Sub-scores that read as measurements when nothing was measured |

## Skills vocabulary

| ADR | Subject |
|---|---|
| [032](032-skill-display-names.md) | Render JD-authored display names, never the global node property |
| [042](042-skill-vocabulary-domain-families.md) | Merge the derived domain families |
| [044](044-skill-family-classifier.md) | Parse-time skill-family classifier for out-of-vocabulary names |

## The model as a dependency

| ADR | Subject |
|---|---|
| [003](003-offline-inference-ollama.md) | Offline inference — Ollama on metal, OpenAI-compatible client. **Never a cloud host.** |
| [021](021-llm-failover-fail-closed-ranking.md) | LLM failover and fail-closed ranking |
| [027](027-honest-resume-parse-status-fu7.md) | Honest résumé parse status |
| [030](030-fu7-degraded-parse-visibility.md) | Degraded-parse visibility |
| [045](045-model-acceptance.md) | **The model is a dependency with an acceptance test.** Run `scripts/model-check.sh` before pointing at any new model. |

## Platform and storage

| ADR | Subject |
|---|---|
| [002](002-neo4j-memory-postgres-ledger.md) | Neo4j for graph + vectors, Postgres as the transactional ledger |
| [004](004-phase-0-storage-schema-embedding-contract.md) | Filesystem storage, startup DDL, the 768-d embedding contract |
| [005](005-filesystem-blobstore-interface-path-safety.md) | Filesystem BlobStore — interface and path safety |
| [006](006-schema-port-trim-ddl-alignment.md) | Schema shape |
| [012](012-api-routes-auth-upload-scope.md) | API routes, auth, upload scope |

## Interface

| ADR | Subject |
|---|---|
| [014](014-workflow-ui.md) | Workflow UI — Flask + HTMX recruiter interface |
| [017](017-bulk-ingest-pairing.md) | Bulk ingest — cover-letter pairing, bulk JD upload |
| [043](043-shortlist-ranking-state.md) | Shortlist regeneration polling gate |
