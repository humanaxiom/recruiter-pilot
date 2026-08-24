# ADR-039: Stage-1 recall searches the job's pool, not the whole database (`fix/stage1-recall-job-scoped`)

**Status:** Accepted (closes ROADMAP.md A4 **M2**, the last of A4's two named ranking defects — A4's
evidence-cliff item remains open)
**Date:** 2026-08-13

## Context

`stage1_coarse` retrieved candidates like this:

```cypher
CALL db.index.vector.queryNodes('resume_summary_idx', $oversample, j.summary_embedding)
YIELD node AS r, score AS vec_score
WHERE r.id IS NOT NULL AND r.job_id = $jid
```

`resume_summary_idx` is a **global** vector index over every `:Resume` node in the database. It is not
partitioned by job and **cannot be** — Neo4j's `db.index.vector.queryNodes` takes no pre-filter. So the
job filter ran **after** the index had already chosen its top `k*3` (150 by default) from the entire
corpus.

Once the database held more than that oversample, a job's own candidates competed for those 150 slots
against **every résumé of every other job** — and lost, because similarity to a job description is not
job-specific.

### Measured, and worse than the roadmap estimated

Against a real Neo4j: a job with **5 applicants** — a pool one tenth of `coarse_k=50` — recalled **zero**
of them once 300 résumés belonging to a different job existed.

Not a degraded shortlist. An **empty** one. The roadmap predicted crowding; the measurement found total
starvation.

### Raising `coarse_k` does not fix it

The oversample was `k*3`, so a larger `k` buys a larger **global** window that the next few hundred
résumés fill again. The pool being searched was the whole database. Raising the knob would have masked the
defect while making it more expensive.

### Why nothing caught it

Every existing test uses one job and a handful of résumés, where the global top-150 trivially contains the
entire corpus. The defect only appears once *other jobs' data* exists, which no unit test and no
single-job integration test created. It is a property of the database's **global contents** — precisely
the class CLAUDE.md says the unit suite structurally cannot prove.

## Decision

Score the job's own pool directly:

```cypher
MATCH (j:Job {id: $jid})
WHERE j.summary_embedding IS NOT NULL
MATCH (r:Resume {job_id: $jid})
WHERE r.id IS NOT NULL AND r.summary_embedding IS NOT NULL
RETURN r.id AS resume_id,
       vector.similarity.cosine(r.summary_embedding, j.summary_embedding) AS vec_score
ORDER BY vec_score DESC
LIMIT $k
```

`r.job_id` is already indexed (`resume_job_id_idx`), so this is an indexed lookup over one requisition's
applicants followed by an **exact** cosine — no ANN approximation, and no dependence on what else is in
the database. A single job's applicant pool is bounded by how many people applied, which is the right size
for exact scoring.

### The scale question, verified against a real server rather than the docs

`vector.similarity.cosine` returns the **same** `[0,1]` normalisation the index reported — `(1 + cos) / 2`,
so identical vectors score `1.0` and orthogonal ones `0.5`.

This mattered more than it looks. Had it returned a raw cosine, every `vec_score` would have silently
rescaled, and `normalise_vector_scores` would have carried that straight into `score_final` **with nothing
failing**. The invariant test passes against *both* implementations, which is what makes it a genuine
before/after check rather than a new assertion about new behaviour.

### One guard the index gave for free

`summary_embedding IS NOT NULL`. Only embedded nodes were ever *in* the index, so an un-projected résumé
was invisible to stage 1 automatically. A job-scoped `MATCH` sees every résumé of the job, including ones
whose graph projection has not run yet — those must be skipped, not returned with a null score.

## Consequences

- A job's shortlist no longer depends on what other requisitions are loaded. That is the property a pilot
  actually needs: adding a second requisition must not silently change the first one's shortlist.
- Recall is now **exact** rather than approximate for any pool size, so `coarse_k` means what it says.
- **Eval corpus ranking is structurally unaffected** — `run_evals.py` never calls `stage1_coarse`; it
  scores fixtures directly. That is a stronger statement than "the metrics matched".

### A test stub was coupled to the old implementation

Worth recording as a lesson, not just a fix. `test_shortlist_fail_closed_pg`'s fake Neo4j dispatched on
`"queryNodes" in query`. With that string gone the stub silently returned **no candidates**, so stage 3
never ran, the LLM never failed, and **four fail-closed tests two files away** reported
`shortlist_state='empty'` instead of `'awaiting_llm'`.

Re-keyed on `vec_score` — the column stage 1 must *return* — which ties the stub to the contract rather
than the retrieval mechanism. A stub keyed on an implementation detail fails loudly only if you are lucky.

### Accepted residuals

- **Performance at very large single-job pools is untested.** Exact cosine over one job's résumés is
  O(pool); the ANN index was O(log n) over the whole corpus. For realistic applicant counts this is
  faster (a smaller set, no index traversal), but a requisition with tens of thousands of applicants would
  want measurement. No benchmark was run — recorded rather than claimed either way.
- **`resume_summary_idx` is now unused by stage 1** but is left in place: it is still asserted by
  `test_neo4j_schema.py`, still populated by the projection, and removing it is a separate decision about
  whether anything else should use it. It is no longer load-bearing for ranking.
- **A4's evidence cliff remains open** — a past-the-cliff candidate still renders an affirmative
  `Evidence · 0%`; that needs a persisted `evidence_evaluated` marker.

## Alternatives considered

- **Raise `coarse_k` / the oversample.** Rejected — see Context. It masks the defect and costs more.
- **A per-job vector index.** Rejected: Neo4j vector indexes are per-label/property, not per-value, so this
  would need a label per job — unbounded label growth and a schema change on every requisition.
- **Over-fetch the global index proportionally to corpus size** (e.g. `k * total_resumes / job_pool`).
  Rejected: it makes recall correctness depend on a ratio nobody maintains, degrades as the corpus grows,
  and is still approximate. The exact query is simpler and has no failure mode of this shape.
- **Keep the index and filter in the application layer.** Rejected — identical crowd-out, moved one hop
  later.

## Gate state

`./scripts/verify.sh all` green, exit code captured directly rather than piped: `EXIT=0`, **4387 unit tests
@ 94.20% coverage, 493 integration tests** (up from 488). RED was measured first: 2 of the 5 new
integration tests failed, the other 3 green as before/after invariants.
