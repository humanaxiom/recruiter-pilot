# ADR-022 — An uncited evidence quote is scrubbed like a fabricated one

**Status:** Accepted · **Date:** 2026-07-21 · **Branch:** `fix/uncited-evidence-quotes` (off `main` @ `961caab`)

Supersedes the asymmetry recorded implicitly in ADR-009's evidence-verifier port. Extends, does not
replace, ADR-010 §6's cleartext-at-rest posture.

## Context

`verify_evidence` (`core/src/pipeline/matching/stages.py`) is the anti-fabrication control on LLM-produced
evidence. It resolves each requirement's cited chunk ids against the candidate's real chunks
(`good_ids`), then took one of two branches:

- **strict** (`evidence and good_ids`) — quote fuzzy-matches no cited chunk at `evidence_verify_fuzz`
  (0.85) → blank `evidence`, demote `status` `met` → `missing`, cap `confidence` at 0.3;
- **lenient** (`evidence and not good_ids`) — cap `confidence` at 0.3, nothing else. The quote text and
  the `"met"` status both survived, and the quote was **never text-matched against anything**.

`good_ids` is empty whenever *every* cited id is hallucinated. So a model that invented a chunk id took
the lenient path, while a model that invented a quote took the strict one: **the verifier was weakest
exactly where the model was least trustworthy.** The cover-letter loop had the same shape.

This was not an oversight in the usual sense. `test_matching_stages.py` carried
`test_verify_evidence_downgrades_confidence_for_uncited_quote_but_keeps_it`, whose docstring stated the
branch was "distinct from the fabrication-scrub branch." The asymmetry was reasoned about, written down,
tested, and never revisited — the test locked in the intent and hid the consequence.

It was found by a `reviewer` fact-check of the HR-facing explainer in `docs/process/`, which claims to HR
that "every quote shown to a human must survive a match against the real document." That claim was false,
and the document is what exposed the code defect rather than the other way round.

## Decision

**Make the lenient arm mirror the strict arm**, in both the requirements and cover-letter loops: blank
`evidence`, demote `met` → `missing`, keep the 0.3 confidence cap.

Rationale: no citation is strictly *less* evidence than a bad citation, so it cannot warrant a *weaker*
response. Any other ordering rewards a model for inventing chunk ids. The function's own docstring already
claimed it downgrades quotes that don't appear in a cited chunk.

**Also close the evals-harness exclusion.** `run_evals.py`'s verification-rate loop conditioned on
`req.evidence and req.evidence_chunk_ids`, which structurally excluded uncited quotes from the "100% of
surfaced quotes verify" figure. It now conditions on `req.evidence` alone.

The two are causally linked, and the mechanism is worth recording: `verify_evidence` sets
`evidence_chunk_ids` to `good_ids` *before* the branch fires, so a fully-hallucinated citation reached the
harness with `evidence_chunk_ids == []` — precisely the shape the old conjunct filtered out. **The gate's
own scrubbing step was manufacturing the input that made the gate blind.**

## Consequences

**Scoring is unchanged at every weighting the gate exercises, and the corpus confirms it.** The full
20-fixture ranking is byte-identical to `main` @ `961caab`: every `score_final` delta `+0.000e+00`, every
rank unchanged, all five ordering pairs identical, r09 still 12th. The pre-existing 0.3 confidence cap
already held these requirements below `_evidence_completeness`'s `met AND confidence >= 0.7` bar.

**The invariance is conditional, and the boundary is stated rather than implied.** It holds for any
`evidence_met_confidence > 0.3`. `match_evidence_met_confidence` is env-settable (`settings.py`), and at
any configured value ≤ 0.3 the fix *does* move completeness (1.0 → 0.0) — toward correctness, and matching
what the fabrication arm already did at those weights. Pinned as an explicit change, not an invariance.
Motivation is unconditionally unchanged: `_motivation_score` gates on `evidence_chunk_ids`, empty by
definition in the lenient arm.

**The hole was reachable, not theoretical.** The `ranking-evals` gate injected a hallucinated-citation
quote through the real extract-then-verify path and ran the 2×2: old verifier + old harness **passed** —
the fabrication rode through the entire corpus gate undetected. Old verifier + new harness fails. New
verifier passes by scrubbing at source.

**A second, larger bypass in the same function is left open and must be fixed next.** The `security` gate
found that `partial_ratio` returns 1.000 when a quote *contains the entire cited chunk verbatim* plus
arbitrary appended text — measured at 1120 appended characters of invented claims, surfacing as `met` @
0.95. This ADR's fix closes the *weaker* of the two bypasses. See the follow-up items below; the HR
explainer's anti-fabrication claim stays false until #1 is closed, and its banner must not be removed
before then.

**A test was changed, which CLAUDE.md permits only when the test is provably wrong.**
`test_verify_evidence_downgrades_confidence_for_uncited_quote_but_keeps_it` asserted the quote survives —
it pinned the asymmetry as intended behaviour, contradicting the function's own docstring. Updated in place
and renamed to `test_verify_evidence_blanks_and_demotes_an_uncited_quote`; not deleted, not xfailed, no
other test weakened.

**The harness loop is now a backstop, not a live assertion.** On the unmutated corpus the new condition is
extensionally inert — `surfaced (new) == surfaced (old) == 81`, `uncited_surfaced == 0` — because the
deterministic stand-in `_extract_evidence` only ever cites real ids, and with the verifier fixed no uncited
quote can exist by construction. Its falsifiability therefore rests entirely on
`test_evals_uncited_quote_gate.py`. **Delete that file and reverting the harness line becomes silently
undetectable.**

**PII posture: strictly improved.** The scrub runs inside `verify_evidence`, whose output is what reaches
`persist_shortlist`, so the scrubbed form is what gets written — fewer LLM-authored strings reach the
cleartext-at-rest tables, and the ones removed are the least trustworthy. Nothing embeds evidence text.
ADR-010 §6 is otherwise unchanged.

## Follow-up items (from the `security` gate, ordered)

> **Superseded by [ADR-023](023-evidence-verifier-hardening.md).** Status: #1 **PARTIALLY closed** (the
> length guard converts unbounded append into a bounded ~26%-of-chunk replacement — narrowed, not closed;
> see ADR-023's residuals); #2, #3, #4 **closed**; #5 **unchanged, still open**. Read ADR-023 before
> touching `verify_evidence` or these schemas again.

1. **HIGH — `partial_ratio` superset bypass** (`stages.py:277-280`, `:315-318`). Quote containing the whole
   chunk verbatim + appended fabrication scores 1.000 at any append length. Fix: length-ratio guard
   (reject `len(quote) > len(chunk) * k`, k≈1.2) or `partial_ratio_alignment` requiring the matched window
   to cover a fraction of the *needle*.
2. **MEDIUM — NUL-byte availability bug** (`shortlist_service.py:109-123`). A `\x00` in a quote survives the
   verifier, `json.dumps` emits it as a `\u0000` escape, and Postgres rejects that escape outright
   ("unsupported Unicode escape sequence ... cannot be converted to text"). The whole `persist_shortlist`
   transaction dies, so one malformed quote loses the entire shortlist.

   **Correction (ADR-023): the fix site named above is wrong — do not follow it.** The original instruction
   read "Strip C0 controls (except `\n` and `\t`) in `verify_evidence`." `verify_evidence` only ever
   rewrites the two `evidence` fields; `requirement`, `overall_summary` and `overall_motivation` never pass
   through it, so a scrub placed there as specified would leave those three fields reaching `json.dumps`
   unscrubbed and still able to kill the transaction. ADR-023 places the scrub at the schema boundary
   instead (`schemas/matching.py`'s `CleanText` annotation), which covers every free-text evidence field,
   including future producers that never go through `verify_evidence` at all.

   Writing this ADR reproduced the bug in miniature: the first draft embedded a literal NUL while
   describing it, which git classified as binary and committed as a zero-line diff. The byte is easy to
   propagate by accident, which is the point.
3. **MEDIUM — unbounded evidence fields** (`schemas/matching.py:127,147,157`). No `max_length` on either
   `evidence` field nor on the `requirements` list; `overall_summary` is capped at 1000 but the quotes are
   not. 2,000,000-char quotes and 100,000 requirements are accepted into O(n·m) fuzzy matching and JSONB.
4. **MEDIUM — no minimum quote length** (`stages.py:276-280`). `"API"` scores 1.000 against any chunk
   containing it.
5. **LOW, accepted** — homoglyph substitution scores 0.879, passing the 0.85 bar; chunk ids are not
   globally unique (`c_001` exists in every résumé) so cross-résumé isolation rests on `chunks_by_id` being
   built per-candidate, correct at both call sites today but undefended by any assertion.

## Gate state (HEAD `1e1776c`)

reviewer **APPROVE** (7 mutations, 6 killed; the survivor — `_fuzz_ratio`'s empty-needle guard — closed in
`1e1776c`) · security **PASS** on the diff, 7/7 mutations killed, zero findings introduced · ranking-evals
**PASS**, ranking byte-identical to `main`, all six standing mutation obligations still FAIL as required on
both input orders. Offline: ruff · black · `mypy src frontend --strict` clean; **2730 unit tests @ 91.64%
coverage**.
