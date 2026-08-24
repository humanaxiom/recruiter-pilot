# CLAUDE.md — Agent Harness v2 (Offline-First / Python / FastAPI / Neo4j / Postgres)

Read automatically by Claude Code every session. This governs ALL work in this repo.

---

## Stack (do not deviate)

- **Python 3.11+**, FastAPI (API), Flask (frontend), arq + Redis (async queue)
- **Postgres** = transactional data (asyncpg; tables created on startup, no migration framework yet)
- **Neo4j** = agent graph memory + vector indexes (768-dim, cosine, `nomic-embed-text`)
- **Ollama over the tailnet** at `${LLM_BASE_URL}` — defaults to the GPU host `aria-gb10`
  (`.env.example` ships its tailnet IP); `host.docker.internal:11434/v1` is only the fallback for someone
  running their own Ollama on the app box. There is NO local Ollama in the standard setup.
  NEVER add cloud API calls.
- Everything except Ollama runs in Docker (`docker compose up -d`)

## Non-negotiable gates — run before EVERY commit

```bash
./scripts/verify.sh              # offline gates (= make gates)
./scripts/verify.sh integration  # integration suite vs real Postgres/Neo4j/Redis
./scripts/verify.sh all          # everything CI runs (= make gates-all)
```

**There is no usable Python on this host** (only the WindowsApps stub), so
`make gates` cannot run natively. `scripts/verify.sh` runs those exact Makefile
targets inside a container — the Makefile stays the single source of truth, so
the gate cannot drift from CI. **Never hand-write a `docker run` to check work
and never substitute a narrower command.** Every recorded instance of that ran
something narrower than the real gate and let a defect through.

Gates: ruff · black · mypy --strict · pytest unit · pytest integration (testcontainers) · coverage ≥ 80% · branch-name. **A single red gate = the work is not done. Iterate until all green — do not report success, do not open a PR, do not stop.**

### `./scripts/model-check.sh` — run this BEFORE swapping models

Every token budget, timeout and concurrency number in this repo was measured
against `gpt-oss:20b`. Point the stack at a different model and all of them are
wrong at the same moment, with no signal — on 2026-08-21 that happened *within*
one model and stopped the product ranking anyone, behind a green suite.

`scripts/model-check.sh` builds the REAL prompts from `./fixtures`, runs each at
the worker's own concurrency, finds the smallest budget that yields
schema-valid JSON, and writes `docs/model-profiles/<model>.json`. **Commit that
file** — `doctor.sh` fails when the configured model has no profile, or when
`LLM_TIMEOUT_S` is below the measured latency.

Both design choices were earned: a synthetic prompt of the same length passed
while the real résumé failed 3/3, and a single uncontended call took ~35s while
four concurrent ones blew a 300s timeout. Measure real inputs, at real
concurrency, or the number is accurate and useless.

### `./scripts/smoke.sh` — the gates prove the CODE; this proves the SCREEN

~6,000 tests and not one crosses the browser→Flask→API seam: every frontend test
mocks `api_client`. Four of the last nine defects to reach users lived exactly
there. `smoke.sh` drives the running product over HTTP with real fixtures and
asserts on rendered HTML. It needs CAS OFF and FAILS rather than skips when CAS
is on — a green run that tested nothing is worse than no run.

### `./scripts/doctor.sh` — the gates prove the CODE; this proves the DATA

`verify.sh` cannot see a defect that lives in STATE rather than in code, and this
repo has now shipped four of those. The sharpest (ROADMAP A7 (20)) was a fix that
was correct, gated green, and had **never applied to a single row** thirteen days
later, because the write only fires on new data and every row predated it.

No test can catch that class: a fixture is always freshly built, always
well-formed, always young. `scripts/doctor.sh` asks the RUNNING deployment
whether the invariants the code promises are actually true of the data that is
there. Run it after any change to projection, migration, or a rendered label,
and before handing the stack to anyone.

It exits non-zero on a failed invariant **or on a datastore it could not reach** —
"could not check" is never a clean bill of health.

### `offline` is not always enough

If the correctness of a change depends on how a real database, driver, or
service behaves, the unit suite **structurally cannot** prove it — it can only
string-match the source. Run `./scripts/verify.sh all` for any diff touching
`models/` (schema, SQL), `api/`, `services/`, `worker/`, or Neo4j/embedding code.

The canonical example, from FU-5 slice 1: a `users.role` column declared
`NOT NULL` with no `DEFAULT` passed all 2764 unit tests and would have failed
the first real INSERT, because ADR-019 requires a first login to omit `role`.
Only the integration run against a real Postgres could see it.

## Trusting subagent reports — coordinator rule

**A subagent's claim of green is not evidence of green.** Require the pasted
command and its real output; if a report says "gates pass" or only summarizes a
diff, treat the work as unverified and re-run the gate yourself before
committing. This is cheap and has already caught a real defect.

When a subagent's report is thin, the fault is usually in the instruction, not
the agent: check what you actually asked for before attributing the miss. Asking
for "a diff summary" and receiving a diff summary is a prompt bug. State the
required evidence explicitly in the task prompt, and prefer the standing
contract in `.claude/agents/*.md` over re-describing it each time.

## Git workflow — mandatory

1. NEVER commit to `main`
2. Branch: `git checkout -b agent/<task-id>-<slug>` (or `feat|fix|chore/<slug>`)
3. Commit sequence tells the TDD story: `red: failing tests` → `green: implementation` → `refactor/docs`
4. Open PR only when `make gates` is fully green locally

## TDD order — mandatory

1. Write failing tests FIRST (`tests/unit/`, `tests/integration/`)
2. Run tests, confirm RED
3. Implement minimally until GREEN
4. Refactor with gates green
5. Update `docs/adr/` if architecture changed; update Mermaid diagrams in README/docs

## Review-iterate loop

When gates fail, read the failure output, fix ONLY what failed, re-run `make gates`. Max 5 self-iterations; if still red, STOP and present the failure report to the human with your analysis — never silently weaken a test or lower the coverage bar to get green.

## Economy — the rules that stop this repo gold-plating itself

### 0. The destination outranks every rule below

**A real recruiter, signed in as themselves, ranks a real requisition against real
applicants and says whether the shortlist is sensible.** Nothing that does not move
that forward gets built. Not a test, not an ADR, not a roadmap entry.

This rule exists because the four below were not enough. They were written on
2026-08-14 against a measured problem — *"85 commits after the v1 scope was
complete, 35 of them `docs`/`chore` against 26 `feat`/`fix`, twelve ADRs, and the
highest-value P0 sat blocked for a week"* — and **ten days later every ratio was
worse**: 94 commits, 32 ADRs against 12 for the entire original build, test code
outweighing source 5.1:1, and still zero recruiters. A rule that says "do less of
this" loses to a process that generates work faster than the rule can veto it.
A destination does not.

Concretely, and these are the reflexes that actually cost the time:

- **Do not hunt A7 instances.** Twenty-one are catalogued and the taxonomy became
  a generator — naming the pattern made finding more instances feel like progress.
  Fix defects users hit; stop auditing for defects nobody has hit.
- **`HANDOFF.md` is capped at EIGHT ITEMS.** A ninth means one is no longer
  relevant — delete it. Do not inline history; this repo was forked clean of it.
- **A finding nobody will action gets deleted, not recorded.** Every recorded
  finding is re-read and re-litigated by future sessions; filing has a cost.

The gates themselves stay exactly as they are. What changes is what gets *built*
between them, not how it is verified.

### 0a. The ADR bar — 32 were written about a finished product

Twelve ADRs carried Phases 0–7, the entire build. Thirty-two more followed it.
An ADR is now an **exceptional** act, roughly monthly. Write one only when
**all three** hold:

1. There were **live alternatives** a competent engineer would have chosen
   between — not a single obvious implementation written up after the fact.
2. The decision is **expensive to reverse** later: schema, wire format, auth
   model, storage layout, a cross-cutting invariant.
3. Someone **six months out cannot recover the reasoning** from the code, its
   tests and the commit message.

Fails any one of them → the reasoning goes in the **commit message**, which is
already where this repo writes its best explanations, and which nobody has to
maintain, re-read, or reconcile with a later change.

**Never** write an ADR to: record a bug fix; restate what a test already
enforces; document a number that was measured (that belongs in
`docs/model-profiles/` or the test that pins it); or memorialise a decision not
to change something. **Amend an existing ADR** rather than adding a sibling —
ADR-036 and ADR-045 both took amendments correctly.

### 0b. The PR bar — batch, don't stream

Four PRs were opened in a single session, one of them purely to update a handoff
that the next PR then rewrote. Every PR costs a full CI run (~9 min), a review
pass, a merge, and a `main` sync.

- **One PR per coherent change, not per commit.** If two pieces of work share a
  theme, they share a branch. Push to the open PR instead of opening another.
- **Docs-only changes do not get their own PR.** They ride along with the next
  code PR, or wait. The only exception is a doc that is itself the deliverable
  (a reset plan, a circulated explainer).
- **Do not run the full local gate on a docs-only commit.** Nine minutes to
  verify a Markdown edit is pure friction — gate code, let CI cover docs.
- **Do not open a PR to record state.** State goes in `HANDOFF.md` on the branch
  that changed it.

The branch-per-task rule and "never commit to `main`" both stand. What changes
is how much work a branch is expected to carry before it earns a PR.

### The four rules that remain

This repo's rigour is real and worth keeping. Its failure mode is the opposite of
sloppiness — gold-plating a product nobody has run. These four rules narrow that.

### 1. Finding disposition — not every finding is a fix

When a gate or review returns findings, dispose of them by severity. Do not fix
everything because everything is fixable:

| Severity | Action |
|---|---|
| critical / major | **Fix on this branch.** Non-negotiable. |
| minor | Fix **only if** cheap *and* on the branch's existing theme. Otherwise record. |
| nit | **Record, do not fix** — unless it is factually wrong in a document a human will act on. |

Recording means one line in `docs/ROADMAP.md` under the owning item, with a
file:line anchor. A recorded finding is a *result*, not a failure to finish.

### 2. Mutation probing is bounded to one pass

Probing the invariants your own branch introduces is required (see the A7 pattern
in `docs/ROADMAP.md`) and it is cheap: mutate each new invariant, run the unit
suite per mutant, revert. Then re-run survivors with **your own new tests
deselected** — that step is what distinguishes "I added tests" from "I closed a
gap". **One pass.** Do not probe the guards you just added to close the last
probe's findings; that recursion has no natural exit and every fix creates new
invariants. Findings outside the branch's scope get recorded, never fixed inline.

### 3. "Blocked on a human" is not a terminal state

If the highest-value item is blocked on a decision, **the block is the work**.
Before setting it down:

- **Check the blocker actually gates the whole item.** It usually gates one part.
  A2 sat blocked for a week on a competency-*scoring* decision while the
  vocabulary work it gated was additive, corpus-neutral, and strictly better than
  the status quo. Nobody checked; every session re-noted the block and moved on.
- **Produce a decision memo**: the options, a **recommended default**, and what
  changes if the human picks otherwise. A P0 must never be handed to the next
  session as a bare "blocked" — that costs a week per hop.

### 4. Prefer product capability when both are available

If an item that changes what the product can *do* and an item that hardens
something already correct are both unblocked, take the capability item. Hardening
work on a feature nobody has run yet is speculative by definition.

## Code rules

- Full type hints; `mypy --strict` clean; no unjustified `# type: ignore`
- Async everywhere (asyncpg, neo4j async driver, httpx)
- Config only via `src/settings.py` (pydantic-settings) — never `os.environ` scattered in code
- Postgres for anything transactional/relational; Neo4j only for graph relationships and vector retrieval; Redis only as arq broker
- All model calls go through the OpenAI-compatible client with `base_url=settings.ollama_base_url`
- Never modify test files to make implementation pass (only if a test is provably wrong, and say so)

## Slash commands (`.claude/commands/`)

| Command | Purpose |
|---|---|
| `/gates` | Run full gate suite, report results table |
| `/review-loop <task>` | Run iterate-until-green cycle on current branch |

## Before implementing anything new

1. Read `HANDOFF.md` (state, environment quirks, exact next step) and the relevant ADRs in `docs/adr/` —
   this is the actual source of truth for prior/similar work in this repo.
2. Check `docker compose ps` — stack must be healthy for integration tests

A `/memory-query` command and a `/memory/similar` route were part of the golden template's demo app,
deleted in Phase 0 along with the rest of the template's task/lineage scaffolding
(`core/tests/unit/test_api.py`'s `DEMO_ROUTES` asserts the route's absence; `core/src/agents/` and the
`BaseAgent._memory_context()` retrieval the command claimed ran inside every subagent are gone too).
There is no graph-memory similarity endpoint in this codebase — do not attempt to curl it, and do not
re-add the command without building the feature first. The command file was removed in the ADR-023
branch; until Phase 0 it had been instructing every session to run a mandated step that could not work.
