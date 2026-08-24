# Session Handoff — recruiter-pilot

**Eight items. Hard cap.** A ninth means one of these is no longer relevant —
delete it.

This repository is a **clean fork for pilot deployment**: fresh history, no data,
no fixtures, no build-phase archaeology. It will hold **real applicant data** on a
dedicated machine. Treat it accordingly.

---

### 1. The objective

> **A real recruiter, signed in as themselves, ranks a real requisition against
> real applicants and says whether the shortlist is sensible.**

Nothing that does not move that forward gets built. Not a test, not an ADR, not a
backlog entry. The product is feature-complete and has never had a user; that is
the only gap that matters.

### 2. Bring the stack up from scratch

Nothing here is seeded. There is no database, no `data/`, no `fixtures/`.

```
cp .env.example .env          # then fill the secrets — see item 3
pwsh ./scripts/quickstart.ps1 # generates keys; needs pwsh 7, NOT PowerShell 5.1
docker compose up -d
```

Then confirm the deployment is actually healthy before trusting it:

```
./scripts/doctor.sh           # invariants in the LIVE data; non-zero on a
                              # datastore it could not reach
```

### 3. `.env` values that must be right before CAS works

The frontend builds its login link as `{CAS_SERVICE_BASE_URL}/auth/cas/login`
(`core/frontend/app.py`). If that points at a container-internal port, login
fails on the first click. Use the **published host ports**, and pick ports that
do not collide on the target machine:

```
CAS_ENABLED=true
CAS_SERVICE_BASE_URL=http://<host>:<API_PORT>
CAS_FRONTEND_BASE_URL=http://<host>:<FRONTEND_PORT>
LLM_TIMEOUT_S=900
```

`LLM_TIMEOUT_S` is derived from measurement, not taste — `doctor.sh` fails if it
drops below the committed model profile. At least one `API_KEY_*` role key must
be set or the API **refuses to boot** with CAS enabled (a deliberate fail-closed
guard, not a bug).

### 4. Rebuild the JD corpus on the dedicated machine

Job descriptions are ingested, not shipped. Upload them through the UI or
`POST /jobs/bulk`, then verify with `doctor.sh` that none stranded. A JD that
fails parsing stays in `draft` and is re-runnable from the job page
(**Re-parse JD**) — it is not lost.

Expect roughly one posting in twenty-five to fail on model output rather than
infrastructure. Measure before treating it as a prompt bug: `temperature=0` means
a retry reproduces it deterministically.

### 5. Provision `fixtures/` out-of-band, or the harnesses fail loudly

`fixtures/` is **gitignored by design** — it held real candidate résumés and must
never be committed. `scripts/smoke.sh` and `scripts/model-check.sh` both read it
and **hard-fail rather than skip** when it is absent, so an unprovisioned clone
cannot report a green run that tested nothing.

**Never `git add -A` in this repository.** Use explicit pathspecs. One `git add -A`
once pushed 99 MiB of real résumés to a public remote.

### 6. The four verification scripts, and what each proves

| Script | Proves | When |
|---|---|---|
| `./scripts/verify.sh [all]` | The **code** | Before a PR |
| `./scripts/doctor.sh` | The **data** in the live deployment | After any change to projection, migration or a rendered label — and before handing the stack to anyone |
| `./scripts/smoke.sh` | The **screen** (browser → Flask → API) | Before handing the stack to anyone. Needs CAS **off**; fails rather than skips when it is on |
| `./scripts/model-check.sh` | The **model**, at real concurrency | **Before** pointing at any new model. Commit the profile it writes |

There is no usable Python on a typical dev host here — `verify.sh` runs the real
Makefile targets in a container so the gate cannot drift from CI.

**GitHub Actions is currently blocked on billing for this org**, and that is a
consequence of this repo being private: public repositories get unlimited free
Actions minutes, private ones are billed. The first push returned *"the job was
not started because recent account payments have failed or your spending limit
needs to be increased."* Until that is resolved, **`./scripts/verify.sh all` is
the authority** — it runs the same Makefile targets CI does, which is why the
Makefile is the single source of truth. Do not read a red CI badge here as a
broken tree without checking the annotation first. `ci.yml` has
`workflow_dispatch` so an operator can re-run the suite once billing clears,
without needing a new commit.

### 7. Never diagnose the model on a contended GPU

Check `GET /api/ps` on the inference host first — **and again during a long run**.
A foreign large model resident alongside the working one makes every call time
out regardless of token budget, and has already produced one confidently wrong
retraction of a correct fix.

Related: **the token floor is per-PROMPT, not per-model.**
`REASONING_JSON_MIN_TOKENS` is right for résumé/JD extraction and wrong as a
universal — one small call site returns identical answers at 128 and 8192. Do not
"fix" a small budget on sight; measure it.

### 8. Open work

Two items, both in [docs/ROADMAP.md](docs/ROADMAP.md). Everything else there is
deferred with a named owner. Start with `pg.jobs_stuck` in `core/src/doctor.py` —
it is the gap that once let twenty dead jobs sit invisible to the tooling for a
day.

Architecture context lives in [docs/adr/README.md](docs/adr/README.md); read the
privacy set before touching anything that displays candidate data.
