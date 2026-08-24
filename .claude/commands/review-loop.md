# /review-loop

**Usage:** `/review-loop <task description>`

Full TDD + iterate-until-green cycle for a task on the current agent branch.

## Steps

1. Verify branch matches `agent/<id>-<slug>` — if on main, create the branch first
2. Query memory: `GET /memory/similar?q=$ARGUMENTS` — surface similar prior artifacts
3. **Red**: write failing tests covering the acceptance criteria; commit `red: failing tests for <task>`
4. Run `make gates-fast` — unit tests MUST fail (if they pass, the tests are too weak — strengthen)
5. **Green**: implement minimally; run `make gates`; iterate on failures (max 5)
6. Commit `green: <task>` only when all gates green
7. **Refactor**: improve with gates staying green; commit
8. Update ADR/diagrams if architecture changed; commit `docs: <what changed>`
9. Record artifacts to memory via `POST /tasks` lineage
10. Report: iterations used, final coverage, files touched, ready-for-PR summary

## Escalation

If red after 5 iterations: stop, output the failure report verbatim plus your hypothesis, and ask the human how to proceed. Never lower the coverage threshold, skip a gate, or weaken a test to get green.
