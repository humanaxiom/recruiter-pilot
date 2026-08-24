# Backlog

The pilot's open work. Short by design — this replaced a 1,018-line roadmap that
had become a queue nobody worked from.

**Nothing here is picked up unless it moves the pilot forward.** Items are
guesses about what a user will need; one watched session with a recruiter
replaces most of them.

> **Historical note.** ADRs in `docs/adr/`, and a number of code docstrings,
> reference build-phase documents — `EXTRACTION_PLAN.md`, `OPEN_DECISIONS.md`,
> `docs/activity/`, and the long-form roadmap this file replaced. Those were
> deliberately left behind when this pilot repository was forked clean; they are
> not missing files. Both ADRs and code comments are records written at the time
> a decision was made and are **not edited retroactively** — the reasoning they
> carry still holds even where the document they cite does not travel with this
> repo. See `docs/adr/README.md`.

---

## Open now

| Item | Why | Size |
|---|---|---|
| `pg.jobs_stuck` in `core/src/doctor.py` | `doctor.sh` checks stranded résumés but nothing checks `jobs.failure_reason IS NOT NULL` or draft jobs with no `parsed_at`. Twenty dead jobs were once invisible to the tooling for 24 hours until a human noticed a spinner. | ~1h |
| One JD fails extraction on model output | The longest posting in the source corpus (9,523 chars) returned `llm output invalid: title: missing`. Different class from an infrastructure failure — the model answered and the answer failed schema validation. `jd_extract_v1`'s measured floor came from a shorter fixture. **Measure before guessing**; `temperature=0` means a retry reproduces it. | ~2h |

## Deferred, with owners

- **Competency scoring model** — owner: corpus owner + HR, **with pilot data**.
  It needs the pilot; the pilot does not need it. The skill *vocabulary* work it
  was once thought to gate already shipped (ADR-042): coverage of real
  qualification statements went 15.6% → 54.8%.
- **The remaining 45.2% of qualification-statement coverage** — job-side work for
  the parse-time skill-family classifier (ADR-044). Its résumé half shipped and is
  credit-disabled by default. Until this lands, an unscripted real posting can
  still show missing-must-have chips for skills a candidate plainly has.
- **ADR-045's measurement gap** — `scripts/model-check.sh` probes Ollama's native
  transport with schema-constrained decoding; the app runs the OpenAI-compatible
  one with `json_object` only, so the committed profile certifies a path the
  product does not use. Direction chosen (give the app schema-constrained
  decoding, portable to vLLM `guided_json`); **no measurement supports it yet.**
  Do not treat the decision as data. Not pilot-blocking.

## Known and disclosed, not defects to fix blind

Carried forward because a pilot operator should know them, not because they are
queued:

- Retention is not enforced automatically; `retention_days` is recorded, not acted on.
- The candidate email hash is unsalted.
- `audit_log` immutability is by convention, not by database constraint.
- `/health` is shallow — it does not prove the datastores are reachable.
- The evidence "cliff" is disclosed rather than removed (ADR-040): below the
  threshold the UI says *not assessed* instead of rendering a fabricated 0%.
