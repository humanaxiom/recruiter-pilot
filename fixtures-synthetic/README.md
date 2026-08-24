# Synthetic fixture corpus — safe to commit, publish and copy

78 résumé PDFs, one generated per real job description, by
`core/src/synth_fixtures.py` (run it with `./scripts/gen-fixtures.sh`).

**These describe no real person.** Emails use `example.invalid` (RFC 2606 —
permanently unresolvable), phone numbers use the reserved `555-01xx` block, and
every PDF carries a `Synthetic-Fixture` marker in its metadata so the question
"is this file synthetic?" is answerable mechanically rather than by trusting a
filename. `manifest.json` records the source JD, coverage tier and matched-skill
count for each.

## Why this exists

`scripts/smoke.sh` and `scripts/model-check.sh` both need
`resumes/*_resume.pdf`, and the only PDFs that ever lived in `fixtures/` were
**real applicants'**. That made the corpus unpublishable, unshippable to a new
machine, and impossible to put in CI — so a fresh deployment either skipped its
two most valuable verification harnesses or moved third-party PII around to get
them. This directory removes that trade entirely.

## Why it is not just lorem

Each résumé is generated **from a real JD**, so it references skills that
posting actually asks for, at one of three coverage tiers (strong / partial /
weak). A pool where every candidate matches everything cannot demonstrate that
ranking works, and a pool of unrelated text would certify a broken ranker as
healthy — the same trap `model_probe_live` documents for synthetic prompts.

Current spread: **37 strong · 25 partial · 16 weak**.

## What is NOT here

**The job descriptions.** They are real postings, 81 MB across 78 `.docx` files,
and they are the *input* to generation rather than an output of it. Committing
them would re-bloat a repository that was deliberately forked down to 1.75 MiB.
Provision `fixtures/JDs/` out-of-band, then:

```bash
./scripts/gen-fixtures.sh                       # fixtures/JDs -> fixtures/resumes
./scripts/gen-fixtures.sh fixtures/JDs out/dir  # or name both explicitly
```

Regeneration is deterministic — seeded from each JD's filename, so the same
inputs reproduce the same corpus on any machine.

## The rule that still applies

`fixtures/` stays gitignored. It is where **real** candidate documents go, and
nothing in it is ever committed. This directory is the committable twin. Never
copy real résumés in here, and never `git add -A`.
