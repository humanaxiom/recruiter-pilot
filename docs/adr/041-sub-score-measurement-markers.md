# ADR-041: Two sub-scores that read as measurements when nothing was measured (`fix/a6-fabricated-sub-scores`)

**Status:** Accepted (closes ROADMAP.md A6's two smaller scoring defects — `normalise_vector_scores` degenerate pool and `_most_recent_title` fallback — with the same strategy [ADR-040](040-evidence-cliff-disclosure.md) used for the evidence cliff: mark on write, disclose on read, leave arithmetic alone)
**Date:** 2026-08-13

## Context

Two sub-scores fall back to a numeric value when no comparison happened, producing byte-identical numbers that are indistinguishable from real measurements:

### D1 — Vector degenerate pool

`normalise_vector_scores` (`stages.py:379-398`, was `:300-311` when recorded — this branch shifted the file) min-max scales the stage-1 pool. When `hi - lo < 1e-9` — which includes **every single-candidate pool** — it returns `1.0` for everyone. The panel rendered `vector | 10 | 100 | 10`: a perfect semantic match. Reverse match hits this routinely, since a résumé with one candidate job is a one-element pool.

### D2 — Seniority untitled role

When `_most_recent_title(parsed)` returned falsy, `seniority = 0.0` (`orchestrator.py:503-512`). The panel rendered `seniority | 15 | 0 | 0`, byte-identical to a genuinely poor title match, and `weights.seniority` is `0.15`.

This is [ADR-040](040-evidence-cliff-disclosure.md)'s defect one layer down, with the same root cause: a risk documented in prose (`ranking-metrics-explainer.html:460` already said "an unreadable recent title scores zero") with nothing surfacing it in the product and nothing enforcing it in code. Instances of the same A7 pattern.

## Decision

Mark on the write path, disclose on the read path, leave the arithmetic alone.

Two markers on `ScoreBreakdown` itself, `bool | None = None`, three states each:
- `True` — the comparison ran. A `0.0` or `1.0` is then a real measurement.
- `False` — no comparison was possible. The stored value came from a fallback.
- `None` — the row predates the marker. Assert neither.

Both markers are set from **the branch actually taken**, never re-derived from the resulting number. This is pinned by tests: a readable title that scores a genuine `0.0` (orthogonal embeddings, clamped by rescale) must mark `seniority_measured=True`, and a `1.0` from a degenerate pool must mark `vector_discriminating=False`, regardless of the number itself.

### 1. Schema — where the markers live

`seniority_measured` and `vector_discriminating` live **inside** `ScoreBreakdown`, not folded into the jsonb the way `evidence_evaluated` is. `ScoreBreakdown` is persisted verbatim — the fold/pop path exists only for fields that could not live in the model. Duplicating it would have been a second divergent mechanism for the same idea. They default to `None` so the ~25 existing test files that construct `ScoreBreakdown` keep working, and a pre-existing jsonb row (no key) validates to the legacy "unknown" state.

### 2. Write path — where the fact comes from

**`normalise_vector_scores` and its degenerate-pool constant.** Extract the `1e-9` epsilon to a module constant `_DEGENERATE_POOL_EPS` and add a sibling predicate `vector_pool_is_degenerate`, so the two callers cannot drift on the threshold — a second literal `1e-9` written independently is precisely the A7 defect shape the spec warns against.

**`_stage2_per_candidate` takes a keyword-only `vec_discriminating: bool | None = None` parameter**, threaded from both call sites (forward `generate_shortlist`, reverse `match_resume_to_jobs`), each computing it once from its own pool's raw `vec_score`s. It records the marker directly onto the breakdown, independent of what the seniority branch decided.

The default is `None`, not `True`, and that is load-bearing rather than incidental: a caller with no pool to speak of has no opinion, and "unknown" is the honest record of that. A `bool` default would make the parameter structurally unable to express "unknown", so any future call site that forgot the kwarg would persist an affirmative "a real comparison happened" claim about a named candidate — the invented fact the marker exists to prevent, pointed the other way. See M6 below; this is not hypothetical, it was the shipped default until the mutation probe caught it.

**Seniority branch.** Set `seniority_measured=True` when a title was actually read and compared; `False` when **no role on the résumé carried a readable title at all** — whether because there is no experience section or because every entry's title is blank. Note this is deliberately mechanical: it answers "did a comparison happen", not "does the number mean anything". A candidate with no work history gets `False` too, because no comparison happened, even though scoring them low is a defensible policy.

The one value change: **`_most_recent_title` now falls back to the most recent role that actually has a title**, instead of returning `None` when the top pick's own title is blank. It previously took `current[0]` unconditionally, so a résumé whose current role had a blank title scored `0.0` even when the previous role read "Senior Backend Engineer" — a real candidate losing the full 15% to a title that was readable elsewhere on the page. A whitespace-only title (`"   "`, `"\t\n"`) counts as unreadable and falls through too, but a title that is already non-blank is returned **unstripped** — stripping would change the string handed to the embedder, and leaving it alone is what keeps the corpus-neutrality argument airtight.

Precedence (current-first, then document order) is unchanged; the fallback only widens which role's title is read.

**The direction-of-change claim, stated precisely, because two earlier drafts of it were too broad.** The fallback raises **that candidate's own seniority sub-score** — not the run as a whole, since a raised structured score can displace a *different* candidate below the stage-3 evidence cut-off and so lower *their* final score. And it is not monotonic even for that candidate: the whitespace gate **lowers** one case on purpose. A résumé whose sole role has a title of `"   "` was previously embedded as literal whitespace and scored a garbage non-zero (measured: `0.6` against a readable-title comparison, with `seniority_floor = 0.5`); it now scores `0.0` and is marked unmeasured. That is the point rather than a regression — a garbage measurement replaced by a disclosed non-measurement — but "can only ever raise a score" was wrong and is not repeated here.

All 20 corpus fixtures have a titled current role and produce an identical title on `main`, before the remediation, and after (verified fixture by fixture, `r01`–`r20`), so the corpus is provably unaffected.

**Do NOT call `ctx.embedder` on the no-title path.** An integration test (`test_shortlist_fail_closed_pg.py`) seeds a résumé with no experience and a bare `MagicMock` embedder — `await MagicMock()` raises `TypeError`, which is outside the LLM exception guard and would escape stage 2 uncaught.

### 3. Read path — what gets disclosed

**`explanation.py`** adds two DTO fields, copied faithfully off the stored row, never re-derived:

```python
seniority_assessed: bool | None = None
vector_comparable: bool | None = None
```

These are a **different question** from `scores_available`, exactly as `evidence_assessed` is. A past-the-fallback row stores a perfectly readable `0.0` or `1.0`, so `scores_available` is `True` while these are `False`. Collapsing them silently re-opens the defect.

**The panel.** The structured sub-score loop at `shortlist_entry.html:133-154` adds a conditional disclosure using `is sameas false` — Jinja identity, so `None` and `0` and `0.0` do not match. A plain `{% if not ... %}` is the mutant to guard against. Render **"not assessed"** in the score cell and **—** in the contribution cell for affected rows only, and state the consequence in prose above the table the way ADR-040 does.

For each marker:
- **seniority** — no readable job title was found, so nothing was compared; the score is a fallback, not a judgement.
- **vector** — every candidate in this pool scored identically on semantic similarity (often because the pool has only one candidate), so the 100% reflects the pool, not the match.

## Consequences

- A recruiter can no longer mistake "we could not read a title" for "we read a weak title" or "the only candidate in the pool" for "a strong match".
- The markers are independent: a degenerate vector pool does not suppress a genuinely measured seniority comparison.
- **Scoring math is unchanged.** This slice adds markers and changes rendering; no weight, sub-score or ordering moves.
- Both directions (`forward_match` and `reverse_match`) carry the markers, since they call the identical stage-2 function.

### Accepted residuals

- **The cliffs themselves remain.** A candidate with no readable title still scores `0.0` for seniority; a degenerate pool still scores `1.0` for everyone. Visible, not gone. Removing them means renormalising the remaining sub-weights when a dimension is unmeasurable, which re-bands the corpus and every margin must be re-measured — a product decision that belongs with the corpus owner.
- **The reverse-match vector scale is unverified.** `stage1_coarse_jobs` uses `job_summary_idx` via `db.index.vector.queryNodes` and there is no analogue of `test_stage1_recall_job_scoped_neo4j.py::test_scores_match_the_vector_index_normalisation` for it. The forward path's `(1 + cos)/2` scale is integration-verified; the reverse path's is assumed. Named follow-up.
- **Reverse-match panel still does not exist**, so reverse-match rows carry the markers but nothing renders them (ADR-031's forward-only display boundary, unchanged).
- **Two other live forward surfaces still render these numbers undisclosed.** The shortlist card tiles (`shortlist_cards.html:98-106`) show `SENIORITY / 0` and `VECTOR / 100` as bare integers, and the CSV export (`shortlist_service.py:1187-1188,1239-1240`) emits the raw `seniority`/`vector` columns. The disclosure is on the entry-detail panel only. ADR-040 recorded the equivalent limitation ("the list view is unchanged") and this ADR must too — the explainer's register decisions 10 and 11 were narrowed to say "Why this rank?" page rather than "the screen" for exactly this reason.
- **`seniority_measured` is a one-bit inference channel under blind review, and is deliberately kept anyway.** A blind-review reader sees `seniority_measured=False` on a pseudonymous entry and learns "this résumé has no parseable job title" — a document-quality fact the old bare `0.0` did not separate from a genuine low score. It is negligible beside what the same blind entry already exposes to the same reader (six float sub-scores at full precision, plus redacted-but-substantive evidence quotes), and it is not identity. **Do not "fix" it by stripping the marker under blind review:** that would reinstate the fabricated affirmative `0.0` for precisely the readers ADR-011 exists to protect. `vector_discriminating` carries no per-candidate information at all — it is pool-constant by construction, so it cannot distinguish two blinded candidates. Security audit finding F1, accepted.
- **The whitespace gate uses `str.strip()`, which does not treat U+200B as whitespace.** A title made only of zero-width characters is still "readable" and is marked measured, embedding an effectively empty string. Unchanged from before this ADR rather than a regression, but it is the same family as the whitespace case, and this repo already scrubs invisible characters elsewhere (ADR-022). One line of `_sanitize`-style normalisation would close it; not done here to keep the embedded string provably unchanged for every currently non-blank title.
- **The two markers disagree with their own sibling about failure policy.** `evidence_evaluated`'s reader (`shortlist_service.py:543-566`) deliberately degrades a corrupt stored value to `None` and never raises, because "a raise would 500 the whole shortlist permanently". These two markers instead raise on junk, and pydantic's lax bool aliases coerce `1`/`"yes"` into an affirmative `True`. There is no live exploit — the write path only ever writes `bool` or `None`, and a hand-corrupted row is the only way in — but the asymmetry is real and a future reader should pick one policy rather than inherit both.

## Mutation testing evidence

After implementation was green, a mutation probe was run against the branch's own new invariants: six mutants, all killed by the final suite. But two survived the full **4457 unit tests** before two additional tests were added to the branch:

### M1 — seniority marker derivation

**Mutant:** Replace `seniority_measured = True/False` (set from the branch taken) with `seniority_measured = seniority != 0.0` (re-derived from the score).

**Why it survived:** Every pre-existing test paired `measured=True` with a `1.0` score and `measured=False` with a `0.0` score, so the two could not be distinguished. The only case that separates them is a title that IS read and compared and honestly scores `0.0` — orthogonal embeddings producing cosine `0.0`, clamped by the `seniority_floor` rescale. This is the rarest case in practice and did not exist in the prior test suite.

**Killed by:** `test_seniority_measured_is_not_re_derived_from_a_zero_score`. Builds a candidate whose title comparison genuinely ran and genuinely scored zero, proving the marker and the score are independent facts.

**Evidence:** 4457 unit tests passed with M1 live, 2 deselected (the M1 and M6 guards). With M1 killed: 4459 passed.

### M6 — vector marker default

**Mutant:** Flip the `vec_discriminating` parameter default from `bool | None = None` back to `bool = True`.

**Why it survived:** No test called `_stage2_per_candidate` without the kwarg. A caller that supplies no opinion about the pool would silently get an affirmative "this pool discriminated" claim.

**Killed by:** `test_an_unsupplied_pool_opinion_is_unknown_never_an_affirmative_claim`. Calls the function without the `vec_discriminating` kwarg and asserts the marker lands on `None`, never `True`.

**Evidence:** 4457 unit tests passed with M6 live, 2 deselected. With M6 killed: 4459 passed.

**Note:** M6 is the same defect shape as ADR-040's own `ShortlistResultEntry.evidence_evaluated: bool = False`, which still cannot express "unknown" and is a live residual there. Both should be addressed together in a follow-up that lifts defaults to `None` across the marker landscape.

### A7 instance 16 — found by the merge-blocking reviewer, and the sharpest of the three

The reviewer ran **17** mutants against this branch. Ten died; seven survived, and one of the survivors was
a genuine major defect rather than a missing edge-case test.

**The mutant:** drop the `not` from the **forward** call site's
`vec_discriminating = not vector_pool_is_degenerate(raw_vec_scores)`. It passed **4459 unit and 56
integration tests**.

**The consequence:** a job with one parsed résumé — the routine degenerate pool this ADR is largely
about — would persist `vector_discriminating=True` and the panel would render `vector | 10% | 100% | 10%`
as a measured semantic match. The exact fabrication this ADR exists to close, reintroduced by deleting
three characters, with nothing complaining.

**Why it is worth recording rather than quietly fixing.** The branch *did* pin the marker wiring — for
**reverse match**, which by this ADR's own residual has no rendering surface at all. It pinned the
invisible direction and left the visible one open. The comment at `orchestrator.py:469` asserted that
"both real call sites always pass it explicitly" as though that settled it; only one was enforced. So
"both call sites are wired" was itself an unenforced invariant — the A7 pattern one level up from the
markers, inside the branch whose entire subject is the A7 pattern.

Closed by two integration tests driving the real forward `generate_shortlist` path against Postgres and
Neo4j, verified by kill-and-restore: `2 passed` unmutated, `2 failed` with the `not` removed, `2 passed`
after restoring.

Three of the reviewer's other survivors were also closed, because each was the fabrication direction:
`vector_pool_is_degenerate([])` returned `False` (an affirmative claim about an empty pool — now `True`);
the "not assessed" suppression could be wired to the wrong row entirely and nothing noticed (the two
panel tests now assert the other three rows keep their real numbers); and the shared-epsilon test pinned
the *constant* while two independently-written `<` comparisons remained, so `normalise_vector_scores` now
**calls** `vector_pool_is_degenerate` and exactly one comparison exists — drift made impossible rather
than merely tested against.

The remaining survivor, `r not in current` in `_most_recent_title`, was proven behaviourally inert
(removing the filter leaves all tests green; dict equality forces equal `is_current`, so no role that
should be considered can be dropped) and is deliberately left as-is.

## Alternatives considered

- **Infer the markers from the numbers downstream.** Rejected — it is the mutants the branch's own spec warns against. A readable title scoring zero cannot be distinguished from a missing title without the marker.
- **Render the scores as missing/not recorded rather than marked.** Rejected — it collapses into ADR-031's existing "not recorded" wording, which means something different (unreadable, not un-measured), and loses the three-state distinction on legacy rows.
- **Renormalise the remaining weights when a dimension is unmeasurable.** The real fix for D2's gravity, and out of scope: it re-bands the corpus and requires every margin to be re-measured. Owner: corpus owner + HR.
- **Change the fallback values.** Rejected per the spec's reasoning: the eval corpus cannot exercise either branch (all 20 fixtures have distinct summaries and titled current roles), so changing the values would be unverifiable and earn a revert like ADR-032.

### Three siblings found while writing this, now marked and disclosed

Grepping for the same shape in the same two files turned up three more fallbacks that render as
measurements. All belong to the A6 family and are now fixed using the same strategy:

- **`score_education`'s `if not ranked: return 0.0` (`stages.py:327`, was `:277` when recorded)** is D2 again, one dimension over.
  A résumé whose education section did not parse scores `0.0` — and that is *worse* than being below the
  bar, which earns partial credit via `education_partial`. A parsing failure is indistinguishable from
  "no qualifications at all", on 10% of the score. This is the strongest of the three. ✅ **Now marked
  with `education_readable` and disclosed when `False`.**
- **`score_experience`'s `if not jd_min_years: return 1.0` (`stages.py:220-223`, was `:211` when recorded)** is D1 again: a JD that
  states no minimum gives *every* candidate full marks on 25% of the score. Defensible as policy — no
  bar, everyone clears it — but no comparison happened, and nothing says so. ✅ **Now marked with
  `experience_bar_stated` and disclosed when `False`.**
- **`score_education`'s `if not jd_min_level: return 1.0` (`stages.py:315-316`, was `:271` when recorded)** is the same, on 10%. ✅
  **Now marked with `education_bar_stated` and disclosed when `False`.**

The two `1.0` cases are the explainer's register decision 10 ("scored relative to the batch") applied to
two more dimensions. Marking all three is mechanically identical to this slice and reuses `ScoreBreakdown`'s
marker pattern directly. **See the addendum below for details — the implementation mirrors ADR-041's own
two markers, with one structural difference (same field names, not renamed) and one precedence rule (bar-not-stated
wins for education when both markers are false).**

## Addendum — Three siblings now marked and disclosed (2026-08-18)

The three siblings documented above have been fixed using the same strategy as this ADR.

### The three defects, now fixed

1. **`score_education`'s `if not ranked: return 0.0` ([stages.py:327](../../core/src/pipeline/matching/stages.py))** — Unparsed education scored worse than below-bar. An `education_readable` marker is now set from `education_levels_readable(candidate_levels)` ([stages.py:284-296](../../core/src/pipeline/matching/stages.py), called from [orchestrator.py:532](../../core/src/pipeline/matching/orchestrator.py)) and disclosed in the template when `False` ([shortlist_entry.html:180-187](../../core/frontend/templates/shortlist_entry.html)).

2. **`score_experience`'s `if not jd_min_years: return 1.0` ([stages.py:220-223](../../core/src/pipeline/matching/stages.py))** — No JD bar gives everyone full marks. An `experience_bar_stated` marker is now set from `jd_states_experience_bar(job.min_years)` ([stages.py:202-212](../../core/src/pipeline/matching/stages.py), called from [orchestrator.py:514](../../core/src/pipeline/matching/orchestrator.py)) and disclosed when `False` ([shortlist_entry.html:167-172](../../core/frontend/templates/shortlist_entry.html)).

3. **`score_education`'s `if not jd_min_level: return 1.0` ([stages.py:315-316](../../core/src/pipeline/matching/stages.py))** — No JD education bar gives everyone full marks. An `education_bar_stated` marker is now set from `jd_states_education_bar(job.education_min_level)` ([stages.py:271-280](../../core/src/pipeline/matching/stages.py), called from [orchestrator.py:531](../../core/src/pipeline/matching/orchestrator.py)) and disclosed when `False` ([shortlist_entry.html:174-179](../../core/frontend/templates/shortlist_entry.html)).

### Why the arithmetic is unchanged

**The reason is not the same for all three, and an earlier draft of this addendum got it wrong.** That draft
claimed the eval corpus cannot exercise any of the branches because "all 20 fixtures parse at least one
education entry". All 20 fixtures do *have* an education section; two of them do not yield a readable
**level**, which is a different thing. The corrected position:

- **Defects 2 and 3 (the two `1.0` fallbacks) genuinely cannot be exercised.** The corpus has a single JD
  and it states both a `min_years` and an `education.min_level` (`core/tests/evals/fixtures/jd_backend_data_engineer.json`),
  so neither no-bar branch is ever taken. Changing either value would be unverifiable by the gate — the
  reasoning that earned [ADR-032](032-re-band-the-corpus-on-the-new-skill-family.md) its revert.
- **Defect 1 IS exercised, on 2 of the 20 fixtures.** `r07_alex_nguyen` (`"Certificate, Full-Stack Web
  Development"`) and `r08_riley_chen` (`"Diploma, Business Administration"`) match none of
  `_DEGREE_KEYWORDS` ([orchestrator.py:589-595](../../core/src/pipeline/matching/orchestrator.py)), so
  `_level_from_degree` returns `None`, `education_levels_readable` is `False`, and both score the unreadable
  `0.0` **against a JD that does state a bachelors bar**. `run_evals.py:722-725` runs this exact code, so the
  branch fires inside a live corpus run for 10% of the fixtures. A value change here would move both
  composites and the gate would see it.

So defect 1 is left alone for a *different* reason than 2 and 3, and it is worth stating plainly: the
corpus's current bands were measured with r07 and r08 sitting at education `0.0`. Changing the fallback
re-bands the corpus and requires every margin to be re-measured — the same owner-assigned work as
renormalising sub-weights when a dimension is unmeasurable (ROADMAP A6). It is not that the gate is blind to
it. **A corpus owner deciding whether to change this fallback should know the gate can measure the change**,
and that the cost is re-banding, not unverifiability.

That two real fixtures already sit on the strongest of the three defects is also the answer to "is this
disclosure hypothetical" — it is not. The `(bar stated, education unreadable)` combination is the shape a
freshly-written row takes for any r07- or r08-like candidate, and it is now pinned by a test at the write
path and in the template.

### Deliberate deviation — same field names, not renamed

This ADR renamed the seniority/vector pair on the read path (`seniority_measured` → `seniority_assessed`, `vector_discriminating` → `vector_comparable`). These three siblings keep the same names on both sides — `experience_bar_stated` / `education_bar_stated` / `education_readable` in both `ScoreBreakdown` and the read-path DTO.

This is a deliberate exception to the pattern above, not an oversight: a rename buys no semantic clarity here (the direction is already unambiguous) and introduces one more place the two copies can drift. The comments in [schemas/matching.py:319-332](../../core/src/schemas/matching.py) and [services/explanation.py:143-150](../../core/src/services/explanation.py) record this decision.

### Precedence rule in the template — bar-not-stated wins when both are false

The template ([shortlist_entry.html:156-188](../../core/frontend/templates/shortlist_entry.html)) implements a precedence rule for education disclosure: when both `education_bar_stated` and `education_readable` are `False`, the bar-not-stated paragraph is shown, not the unreadable-education one.

The reason: `score_education` returns its `1.0` fallback BEFORE checking whether the candidate's education parsed ([stages.py:315-327](../../core/src/pipeline/matching/stages.py)). If the JD states no education bar, the function never looks at candidate levels at all, so an unreadable-education paragraph would describe a `0%` that was never computed. Both facts stay recorded independently on the write path; only the *paragraph shown* changes — when both markers are `False`, the first one ("this job posting states no bar") is the honest claim about what the scorer did.

### Structural point — predicates called, not re-stated

Each scorer now calls the predicate that owns its condition ([stages.py:202-212, 280, 296](../../core/src/pipeline/matching/stages.py)), the same structural remedy as this ADR's `vector_pool_is_degenerate` / `normalise_vector_scores` precedent ([stages.py:360-398](../../core/src/pipeline/matching/stages.py)). A second `if not jd_min_years` written independently cannot appear at the write site — drift made impossible rather than tested against. The comments at [orchestrator.py:511-514, 528-532](../../core/src/pipeline/matching/orchestrator.py) record this discipline.

### Honest residual — disclosure still reaches the detail panel only

Exactly as this ADR records for its own pair, the shortlist card tiles ([shortlist_cards.html:98-106](../../core/frontend/templates/shortlist_cards.html)) and the CSV export ([shortlist_service.py:1185-1186, 1237-1238](../../core/src/services/shortlist_service.py)) still render the bare `0` / `100` undisclosed. The "Why this rank?" panel is the only surface where disclosure reaches the reader.

## Gate state

`./scripts/verify.sh all` green, exit code captured directly rather than piped: **4466 unit tests @
94.41% coverage, 499 integration tests** (after the reviewer remediation round; it was 4459 @ 94.40% /
497 when the reviewer first ran).

RED was measured first and is recorded honestly: the initial failing state was a *collection* error
(`ImportError: cannot import name '_DEGENERATE_POOL_EPS'`) plus, once collection was made to proceed,
per-test failures of the expected kind — `ValidationError: Extra inputs are not permitted` for the two
new `ScoreBreakdown` fields, `TypeError: unexpected keyword argument 'vec_discriminating'`, and
`AttributeError` on the markers. Eleven of the new tests passed vacuously at RED because the template
had no disclosure branch at all to render the wrong thing; they are mutant guards, and their positive
counterparts were the ones genuinely red. The two mutation-probe guards (M1, M6) were added after
green, which is why they are listed as such above rather than presented as part of the original RED.
