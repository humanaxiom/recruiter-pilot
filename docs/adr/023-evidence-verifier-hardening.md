# ADR-023 — Evidence-verifier hardening (ADR-022 follow-up items #1–#4)

**Status:** Accepted · **Date:** 2026-07-21 · **Branch:** `fix/adr022-evidence-verifier-hardening`
(off `main` @ `1f526f6`)

Supersedes the "Follow-up items" list in [ADR-022](022-uncited-evidence-quote-scrub.md). Extends, does
not replace, ADR-022's decision or ADR-010 §6's cleartext-at-rest posture.

## Context

ADR-022 closed the weaker of two bypasses in `verify_evidence` (`core/src/pipeline/matching/stages.py`)
and, deliberately, merged with the larger one still open: `partial_ratio` scores 1.000 for a quote that
contains its cited chunk verbatim plus arbitrary appended text, at any append length. HANDOFF.md recorded
this as the immediate next work item, ahead of FU-5, together with three lower-severity findings from the
same `security` gate pass: a NUL-byte availability bug that kills the whole `persist_shortlist`
transaction, unbounded evidence fields accepted into O(n·m) fuzzy matching and JSONB, and no minimum quote
length (`"API"` scores 1.000 against any chunk containing it).

This branch closes all four.

## Decision

### 1. `_fuzz_ratio` guard chain

`_fuzz_ratio(needle, haystack)` now runs, in order:

1. Strip the invisible/bidi control class (`schemas.matching._strip_control_chars`) from **both** needle
   and haystack.
2. Collapse whitespace (`_collapse_whitespace`) on **both** sides.
3. Blank-needle guard: empty after scrub/collapse → `0.0`.
4. Minimum-quote-length floor (`evidence_min_quote_chars`, default 16) on the **collapsed** needle → `0.0`
   below it.
5. Length guard: `len(collapsed needle) > len(collapsed haystack)` → `0.0`.
6. `fuzz.partial_ratio(needle, haystack) / 100.0`.

Symmetry is load-bearing at every stage, not just the final ratio call. The quote is already scrubbed at
the schema boundary (see §3); the chunk is not — it comes from `resumes.parsed`, whose only prior
sanitisation strips NULs. A résumé whose extracted text carries soft hyphens (a common PDF line-break
artefact) would otherwise give a haystack full of U+00AD and a needle with every one already removed:
measured on a 148-char chunk, SHY every 8 characters scores 0.892, every 5 characters 0.838, every 2
characters 0.671 — the chunk's own text rejected as fabrication. Scrubbing and collapsing both sides is a
normalisation, exactly like the existing `.lower()`: it cannot turn a non-span into a span, only let a
genuine span keep scoring as one.

### 2. Length guard is fully closed, not ratio-bounded

ADR-022's follow-up #1 suggested a length-ratio guard (`len(quote) > len(chunk) * k`, k≈1.2 or ≈1.05).
Implemented instead: `len(needle) > len(haystack) → 0.0`, with no multiplier.

**Measured reason a k-guard does not work:** on r01's real 148-character chunk, a needle equal to the
chunk plus one appended character scores **1.000** at ratio 149/148 ≈ **1.007**. No k close enough to 1.0
to matter catches a +1 append, and no k far enough from 1.0 to tolerate normal LLM re-quoting excludes
that append. There is no usable threshold between those two constraints.

Framed as an invariant, not a tunable: a quote is a span of exactly one cited chunk, and a span cannot be
longer than the thing it spans. This is documented as a structural property, not a `MatchWeights` field —
there is no settings knob for it, unlike `evidence_verify_fuzz` or `evidence_min_quote_chars`. Cross-chunk
concatenation (a quote built by joining two different cited chunks) is rejected as a direct consequence,
which is intended — a span exists in exactly one chunk.

### 3. C0 / invisible-control sanitisation moves to the schema boundary

`RequirementEvidence.evidence` / `.requirement` / `.source_context`, `CoverLetterEvidence.evidence`,
`EvidenceObject.overall_summary` / `.overall_motivation` are now `CleanText = Annotated[str,
AfterValidator(_strip_control_chars)]` (`core/src/schemas/matching.py`), not a scrub added inside
`verify_evidence`.

**ADR-022's follow-up item #2 (line 104) and HANDOFF.md's "NEXT WORK ITEM" section (line 1003) both named
`verify_evidence` as the fix site. That instruction was wrong and must not be followed.** `verify_evidence`
only ever rewrites the two `evidence` fields (`RequirementEvidence.evidence`,
`CoverLetterEvidence.evidence`) — it never touches `requirement`, `overall_summary`, or
`overall_motivation`. A scrub placed inside `verify_evidence` as specified would have left those three
fields reaching `persist_shortlist`'s `json.dumps` unscrubbed, and a NUL in any of them still kills the
whole transaction. The schema boundary is the only site that covers every free-text field on every model
that reaches Postgres, including future callers that never go through `verify_evidence` at all (read-path
revalidation, tests, any future non-LLM producer). This correction is recorded here explicitly so a future
reader does not re-derive the wrong fix site from ADR-022 alone.

The stripped class is C0 controls + DEL (keeping TAB/LF/CR, which carry real formatting) plus the Cf
invisible/bidi class: soft hyphen, Mongolian vowel separator, zero-width space, LTR/RTL marks, bidi
embedding/override/isolate controls, word joiner, BOM. ZWNJ/ZWJ are deliberately excluded (script-meaningful
in Persian/Arabic/Devanagari and emoji sequences); NBSP/U+2028/U+2029/U+3000 are excluded because they are
real whitespace already normalised symmetrically by `_collapse_whitespace`.

### 4. Caps split: strict ingest, tolerant read

`RequirementEvidence`, `CoverLetterEvidence`, `EvidenceObject` carry no length/count caps and are used by
`verify_evidence`, the DTOs, and every read path — they must accept anything ever written. `*Ingest`
subclasses (`RequirementEvidenceIngest`, `CoverLetterEvidenceIngest`, `EvidenceObjectIngest`) carry the
caps and are wired at exactly one place: the `chat_json` call in
`pipeline/matching/orchestrator.py::_stage3_per_candidate`. An ingest instance is accepted everywhere a
read instance is; a read instance can never stand in for an ingest one.

`EvidenceObjectIngest` bounds `requirements` and `cover_letter_evidence` with a `mode="before"` validator
that slices the raw list before pydantic validates any item. The original `max_length` constraint did not
prevent the DoS it was written for: pydantic validates every item first and only checks the length
afterward, so a 100,000-item list of 2,000,000-character quotes ran the per-item scrub 100,000 times before
ever raising.

Caps: `MAX_EVIDENCE_QUOTE_CHARS = 2000`, `MAX_REQUIREMENT_CHARS = 500`, `MAX_EVIDENCE_CHUNK_IDS = 8`,
`MAX_REQUIREMENTS = 64`, `MAX_OVERALL_TEXT_CHARS = 1000`.

## Human decisions taken this session

1. **Length guard fully closed, not `k=1.05`.** See §2 — the measured +1-char / 1.007-ratio bypass leaves
   no usable k.
2. **Minimum quote length set to 16, lowered from an initial 32.** At 32, the `security` gate measured the
   floor scrubbing genuine short evidence indistinguishably from fabrication: `"PhD in Computer Science"`
   (23 chars), `"AWS Solutions Architect"` (23 chars), `"Postgres schema migrations"` (26 chars) were all
   blanked and demoted `met` → `missing`. 16 still rejects the degenerate cases the floor exists for
   (`"API"` = 3, `"SQL"` = 3, `"Kubernetes"` = 10).
3. **Cross-chunk quotes are rejected.** "A quote must be a span of exactly one cited chunk" is a new
   framing, not one any prior ADR stated. It falls out of the length guard as a byproduct (a needle built
   by concatenating two chunks generally exceeds either individual haystack) but is being recorded here
   explicitly as a behavioural narrowing: a genuine quote spanning two cited chunks — which the pre-existing
   verifier's `any(... for cid in good_ids)` loop could previously accept if it matched *either* chunk
   above the fuzz bar — is now unrepresentable by construction. No corpus fixture exercises this shape.
4. **Read path tolerant, ingest path strict.** Enforcing caps on the read model was tried and rejected:
   applying `MAX_EVIDENCE_QUOTE_CHARS` on read makes a pre-existing over-cap row (e.g. a stored 2500-char
   quote written before this branch) fail `model_validate` inside `shortlist_service`'s uncaught call,
   which 500s the entire shortlist endpoint for every candidate on that job — and this project has no
   migration framework to backfill or truncate the offending rows. Rationale: a cap prevents a bad *write*;
   once the bytes are already on disk it buys no protection and only breaks retrieval.
5. **At ingest, drop only the offending quote, never truncate — and drop the whole field, not the
   `EvidenceObject`.** Truncation was rejected because a truncated superset-bypass quote (chunk + huge
   append) can still contain the cited chunk verbatim in its prefix and would verify at 1.000 after
   truncation — trimming would manufacture exactly the fabrication the length guard exists to catch. The
   ingest cap therefore drops (`evidence = ""`) and, when `status == "met"`, demotes to `"missing"`, mirroring
   `verify_evidence`'s own scrub shape. A blanked-but-still-`"met"` row was rejected because
   `_evidence_completeness` scores on `status` and `confidence` and never reads quote text — leaving
   `status` untouched would give an over-cap quote full completeness credit for evidence that was just
   discarded.
6. **The HR explainer's DRAFT/NOT-FOR-CIRCULATION banner stays.** See [Deliverable 3 /
   `docs/process/ranking-metrics-explainer.html`](process/ranking-metrics-explainer.html) and the note
   below — item #1 is only partially closed, so the explainer's anti-fabrication claim is still not
   literally true.

## Accepted residuals

Read these as carefully as the wins above — several are more consequential than what closed.

- **Item #1 (`partial_ratio` superset bypass) is NOT fully closed.** The length guard closes *unbounded*
  append (any quote longer than its cited chunk scores 0.0), but a quote at or under chunk length can
  still replace roughly a quarter of its content with invention and pass: `chunk[:130] + " ALSO CTO"` on a
  148-character chunk (≈26% of the chunk's length replaced) scores **0.982** and verifies. This is inherent
  to `partial_ratio` at a 0.85 bar — the guard converts *unbounded append* into *bounded replacement*. That
  is a real and large reduction in exposure (from "arbitrary fabrication length" to "a bounded fraction of
  one chunk's length"), but it is a narrowing, not a closure, and should not be described as one.
- **Ellipsis-joined quotes score 0.792 and are scrubbed.** `"...start of chunk ... end of chunk..."` — one
  of the most common real LLM quoting idioms for a non-contiguous span — falls below the 0.85 bar and is
  blanked as if fabricated. This is a **pre-existing** property of `partial_ratio` at this threshold, not
  introduced by this branch, but it was not previously written down and is now pinned here: genuine
  evidence in this shape reads to the verifier as fabrication.
- **The length guard's `>` has zero tolerance.** Any LLM normalisation (e.g. re-punctuation, a trailing
  space) that lengthens a genuine quoted span by even one character relative to the collapsed chunk is a
  hard `0.0`, with no slack. Deliberate, per §2, but its false-rejection rate against real model output is
  unmeasured.
- **Item #5's residuals from ADR-022 are unchanged.** Homoglyph substitution still scores ~0.879 and passes
  the 0.85 bar (measured byte-identical to `main`). Chunk ids are still not globally unique (`c_001` exists
  in every résumé); cross-résumé isolation still rests entirely on `chunks_by_id` being built per-candidate
  at both call sites, correct today, undefended by any assertion.
- **Read models deliberately lost bounds `main` had.** Pre-branch, `overall_summary` was capped at 1000
  characters and `evidence_chunk_ids` implicitly bounded by `max_length` on the field; both bounds are gone
  from the read/DTO models by design (§4) — a 50,000-character `overall_summary` or a 5,000-entry
  `evidence_chunk_ids` list, if it somehow reached storage, is now accepted on read. Writes remain capped.
- **Stripping U+202E does not lower the bidi attack's fuzzy score.** Measured 0.948 → 0.952 after the
  strip — the appended reversed text is the same number of visible characters either way, and a short
  append onto a long chunk sits inside `partial_ratio`'s existing tolerance regardless. What the C0/Cf scrub
  removes is the *rendering* deception (the quote a reviewer sees is now the quote that was scored), not
  the underlying match.

## The reusable lesson

The `ranking-evals` gate found the corpus could falsify **none of the six guards this branch added.** All
seven guard mutations (the six additive ones plus the pre-existing empty-needle guard) survived unmutated
and survived on both input orders — i.e. every attempted mutation of the new guard logic passed the
ranking gate anyway.

**Cause:** `_extract_evidence`, the deterministic stand-in for the LLM used by `run_evals.py`, quotes each
cited chunk **in full**. All 81 surfaced quotes in the corpus are byte-identical to their cited chunk. Under
that input:

- the minimum-length floor never binds (every quote is well over 16 characters);
- the length guard sits exactly at its boundary (`len(needle) == len(haystack)`, never over it);
- whitespace-collapse and control-char scrub are no-ops (the stand-in never emits irregular whitespace or
  control characters).

**A byte-identical ranking was therefore the only possible outcome of this branch on the existing corpus —
it would have been identical for a materially broken implementation of any of the six guards.** This is
the same corpus inertness ADR-022 recorded about its own harness loop (`surfaced == 81` either way,
falsifiability resting entirely on a dedicated regression test), recurring one layer up. It is also the
same shape of defect the Phase 4a "recurring lesson" recorded — a control that looked rigorous asserted
what the code *should* do rather than exercising what it *does* — making this the third occurrence of that
pattern in this repo's history.

**Fixed in-branch, not deferred:** additive probe fixtures (`superset_evidence`, `degenerate_evidence`, and
control/whitespace render variants) plus `core/tests/unit/test_evals_verifier_guard_gate.py`, which pins
the assertion call sites so a reverted guard is caught even though the baseline corpus can't see it. The
guard-mutation battery now kills 20 of 20 across 10 mutations × 2 input orders, independently reproduced by
the `reviewer` gate.

**Deferred, and recommended as the next step:** making `_extract_evidence` quote a genuine *span* of each
chunk rather than the whole chunk (call it Fix A). This is the change that would let the baseline corpus
itself exercise the length floor and the length guard's boundary instead of relying solely on the
dedicated probe file. It is deferred here because it moves every evidence-derived score the stand-in
produces and needs deliberate re-baselining against the labelled corpus — out of scope for a hardening
branch whose own gate required ranking to stay byte-identical to `main`.

## Consequences

- Ranking is **byte-identical to `main`** on the unmutated 20-fixture corpus (per the reusable-lesson
  finding above, this is expected and not by itself evidence the guards work — see
  `test_evals_verifier_guard_gate.py` for the assertion that actually exercises them).
- The NUL-byte availability bug (ADR-022 follow-up #2) is closed for every free-text evidence field, not
  only the two `verify_evidence` rewrites — see §3's correction of the original fix-site instruction.
- The evidence size DoS (follow-up #3) is closed at ingest; pre-existing over-cap rows, if any exist on
  `main`, remain readable and unbounded on read by design (see residuals).
- The degenerate-quote hole (follow-up #4) is closed, with the floor recalibrated once by measurement
  (32 → 16) to avoid scrubbing genuine short evidence.
- The superset bypass (follow-up #1) is narrowed from unbounded to a bounded fraction of one chunk's
  length, not closed — the HR explainer's anti-fabrication claim remains not literally true (see
  Deliverable 3 / the explainer's banner).

## Alternatives considered

- **Length-ratio guard with `k≈1.05–1.2`** (ADR-022's original suggestion) — rejected; see §2, no k value
  separates a genuine re-quote from a minimal-append bypass.
- **`partial_ratio_alignment` requiring the matched window to cover a fraction of the needle** — not
  implemented this branch; would address the bounded-replacement residual (26% case) but changes the
  scoring function's shape rather than adding a guard in front of it, and needs its own corpus
  re-baselining similar to Fix A. Left as a candidate for a future branch, not adopted here.
- **C0 scrub inside `verify_evidence`** (as ADR-022 literally specified) — rejected once it was noticed
  that three of the five affected fields never pass through that function; see §3.
- **Raising on an over-cap ingest field instead of scrubbing** — rejected; the earlier caps-as-`max_length`
  attempt showed this fails the *entire* candidate's evidence object on one over-long field, discarding
  every other requirement's evidence along with it.
- **Enforcing caps on the read model too** — rejected; see human decision #4, this makes any pre-existing
  over-cap row permanently unreadable with no migration path to fix it.

## Gate state (HEAD `2fc61c8`)

reviewer **APPROVE** · security **PASS** · ranking-evals **PASS**, ranking byte-identical to `main`.
Offline: ruff · black · `mypy --strict` clean; **3125 unit tests**; **123 integration tests** passed live
against real Postgres + Neo4j.
