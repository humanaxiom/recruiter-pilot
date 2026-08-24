"""Phase-4a ranking-evals harness -- STUB.

This script is what the `ranking-evals` merge-blocking gate
(`.claude/agents/ranking-evals.md`) will run once Phase 4c lands
`src.pipeline.matching.orchestrator`. Today it is expected to FAIL, and to
fail for exactly one reason: the orchestrator does not exist yet.

Do NOT read a green run of this script as "ranking works" -- it is a
scaffold. `core/tests/unit/test_evals_corpus.py` is what actually proves the
fixture corpus + thresholds.toml are well-formed right now.

Usage (once 4c lands):
    cd core && python tests/evals/run_evals.py

THE THRESHOLD KEY SET IS A THREE-WAY CONTRACT between `thresholds.toml`, this
docstring, and `.claude/agents/ranking-evals.md`. Every key below is read
literally, and `core/tests/unit/test_evals_corpus.py::
test_every_threshold_key_is_enumerated_by_both_consumers` PARSES this docstring
(a section is a 2-space-indented `[name]`; a key is a 4-space-indented token)
and fails if the toml grows or loses a key without both consumers being updated
in the same change. Keep the indentation shape. (That drift already happened
once: the toml grew `[adversarial]` and `[evidence].min_completeness_in_topk`
and neither consumer enumerated them -- so a 4c coder wiring this harness from
this docstring would have built a gate that a naive pure-vector ranker passes.
And until round 4 the contract test the comments named did not exist at all.)

Computes, against `fixtures/` + `thresholds.toml` -- EVERY key, none optional:

  [precision_at_k]
    k = 5                       -- shortlist window
    min_precision = 1.0         -- ALL of the top-k must be tagged strong/
                                   borderline in labels.json. Exactly 1.0: a
                                   lower floor admits a weak/adversarial
                                   fixture into the top-k and contradicts
                                   [adversarial].must_not_surface_in_topk.
  [evidence]
    verification_rate_min = 1.0 -- fraction of SURFACED quotes that fuzzy-match
                                   (>= fuzz_threshold, rapidfuzz partial_ratio
                                   or token_set_ratio -- NOT fuzz.ratio, which
                                   scores this corpus's own gold anchors at
                                   0.648/0.796 and so can never reach 1.0)
                                   against their cited chunk. Any unverifiable
                                   quote reaching output is a hard fail
                                   (anti-fabrication invariant).
    fuzz_threshold = 0.85       -- == MatchWeights.evidence_verify_fuzz.
    min_completeness_in_topk    -- = 1.0, PINNED. Fraction of top-k entries
                                   carrying >= 1 VERIFIED quote. Stops
                                   verification_rate_min passing vacuously over
                                   an empty quote set.
    negative_evidence_must_fail -- labels.json's `negative_evidence` quotes are
                                   FABRICATED and MUST score below
                                   fuzz_threshold against their cited chunk.
                                   Without these, verification_rate_min = 1.0 is
                                   satisfiable by a verifier that always returns
                                   True. Beware the measure: fuzz.WRatio scores
                                   r02's fabricated anchor at 0.855 >= 0.85, and
                                   partial_token_set_ratio returns 1.000 on 2 of
                                   the 4 negatives.
    gold_recall_min = 1.0       -- (round-8 addition, R2 closure) fraction of
                                   labels.json's `gold_evidence` anchors that
                                   must come back `status="met"` with a
                                   SURVIVING verified quote from the FULL
                                   extract-then-verify pipeline. Every other
                                   key in this section gates the VERIFY half
                                   only (an extractor that never finds real
                                   evidence in the first place passes all of
                                   them vacuously, short of the
                                   min_completeness_in_topk floor). Gates the
                                   extractor's RECALL, not just the verifier's
                                   precision.
    min_quote_chars = 16        -- == MatchWeights.evidence_min_quote_chars.
                                   Floor below which a "verified" evidence
                                   quote is too short to be meaningful
                                   evidence (ADR-022 hardening). LOWERED
                                   32 -> 16 by human decision (security
                                   FINDING 4): at 32 it blanked genuine short
                                   credentials ("PhD in Computer Science", 23)
                                   and demoted them met -> missing. Defended
                                   by r03's short gold anchor, scored through
                                   the real _fuzz_ratio -- see
                                   _assert_gold_anchor_text_survives_the_verifier.
  [adversarial]
    must_not_surface_in_topk    -- r09 (the keyword-stuffer) must never appear
                                   in the top-k. Same for every fixture flagged
                                   must_not_surface_in_topk in labels.json. r09
                                   is structurally top-tier on ALL FIVE
                                   structured sub-scores -- skill, experience,
                                   seniority (its most-recent title is the JD
                                   title VERBATIM, so the cosine is 1.0 by
                                   arithmetic), education (a JD-allowed BSc) and
                                   vector (every JD skill in the embedded
                                   summary) -- so ONLY the evidence verifier can
                                   reject it. Scoring 0.6*structured + 0.3*0 +
                                   0.1*0 ~= 0.597, it lands just BELOW THE STRONG
                                   TIER (near-tied with r04, so its exact rank
                                   moves between builds and is NOT gated) and
                                   ~0.19 clear of the k=5 cutoff -- not below
                                   every weak fixture. That is arithmetic: do not
                                   "fix" it by re-tagging r09 or moving a
                                   threshold, and do not re-pin an exact rank.
    must_rank_below_every_strong -- ROADMAP A3. The "below the strong tier"
                                   claim above was PROSE until 2026-08-13 and
                                   nothing read it, so a change that violated it
                                   still exited 0 -- which is what the reverted
                                   ADR-032 attempt did. Now enforced as an ORDER
                                   RELATION over tags (every 'adversarial'
                                   fixture ranks strictly below every 'strong'
                                   one), which needs no measured constants and
                                   so cannot drift with the corpus the way a
                                   pinned rank does. Deliberately NOT
                                   expected_rank_band, which goes red on r18.
                                   See _assert_bait_ranks_below_every_strong.
  [ordering_controls]
    enforce = true              -- for every pair below, the live ranker must
    pairs = [...]                  satisfy BOTH halves: rank(higher) <
    min_score_gap = 1e-6           rank(lower), AND score_final(higher) -
                                   score_final(lower) >= min_score_gap. Each pair
                                   is identical in every scoring-relevant field
                                   except one dimension (education / overqual /
                                   motivation / skill_missing_must / recency --
                                   the last two added round 8, see
                                   fixtures/labels.json and thresholds.toml for
                                   their rationale), so a ranker blind to that
                                   dimension fails -- but ONLY IF THE GAP IS
                                   CHECKED TOO (round-6 finding F5): the overqual
                                   and motivation twins have BYTE-IDENTICAL
                                   embedding input (_build_summary_text reads
                                   neither total_years_experience nor
                                   cover_letter_chunks), so a blind engine ties
                                   them EXACTLY and the stable sort decides the
                                   pair arbitrarily -- a motivation-blind engine
                                   PASSED the rank-only assertion. min_score_gap
                                   is an anti-tie epsilon: > 0 (else the tie
                                   passes) and far below the smallest correct-
                                   engine gap (0.0120 -- the overqual pair's, and
                                   the ONLY one of the three original pairs that
                                   is arithmetic rather than measured -- the two
                                   round-8 pairs' gaps (0.144 each) are larger
                                   still; see thresholds.toml).
                                   REVIEW OBLIGATION, NOT A GATE (round-7 M-3,
                                   extended round 8): the three original
                                   blind-engine mutations (education = 0,
                                   overqual_ratio = 99, motivation = 0) plus
                                   weights.skill = 0 and a recency-decay-disabled
                                   mutation are what prove these pairs gate their
                                   dimension at all, and each must FAIL on BOTH
                                   input orders -- but nothing in this file or
                                   thresholds.toml can run them, because they
                                   need the engine with MUTATED MatchWeights.
                                   They belong in 4c's own test suite, and 4c's
                                   reviewer must check for them. Do not read this
                                   key as enforcing them. A SIXTH mutation,
                                   must_have_miss_penalty: 0.5 -> 1.0, is a
                                   review obligation of a DIFFERENT shape (round
                                   8): it cannot be expressed as a pairwise
                                   rank+gap check at all (removing the penalty
                                   only shrinks, never reverses, the
                                   skill_missing_must pair's gap -- an algebraic
                                   property of the mean+multiplicative-penalty
                                   formula, not a fixture gap). It must be
                                   verified numerically on r18 in isolation
                                   instead; see fixtures/labels.json's
                                   skill_missing_must rationale and
                                   core/tests/evals/README_4c_twins.md.
  [pii]
    leak_check = true           -- no fixture's candidate name/email/phone may
                                   appear in embedding input or exported output.
    allow_structured_fields     -- ADR-007 N1: structured experience/education
    structured_fields_surface      free text may carry identity ONLY on the
                                   surface named here (`outbox_at_rest`).
    embedding_input_pii_free    -- embedding input must contain no name/email/
    exported_output_pii_free       phone REGARDLESS of the originating field
                                   (ADR-007 §7-F1). A bullet-derived chunk is
                                   NOT exempt: r12's c_003 is byte-identical to
                                   its bullet text.
  [determinism]
    temperature = 0.0           -- pinned for the eval runs.
    max_rank_delta = 0          -- ZERO tolerance: ranking ORDER must be
                                   identical across two runs.
    max_score_delta = 1e-9      -- score_final gets an epsilon, not exact
                                   equality. 4c REQUIREMENT: pin `seed` on the
                                   eval path and state the embedding-cache state
                                   (cold vs warm) across the two runs -- with a
                                   warm Redis cache this check compares the
                                   cache to itself, not the model to itself.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import tomllib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Path setup: this file lives at core/tests/evals/run_evals.py. `src` is a
# top-level package rooted at `core/`, so make sure `core/` is importable
# whether this script is invoked as `python run_evals.py` (cwd == this dir),
# `python tests/evals/run_evals.py` (cwd == core/), or via an absolute path
# from anywhere.
# ---------------------------------------------------------------------------
_EVALS_DIR = Path(__file__).resolve().parent
_CORE_ROOT = _EVALS_DIR.parents[1]
if str(_CORE_ROOT) not in sys.path:
    sys.path.insert(0, str(_CORE_ROOT))

FIXTURES_DIR = _EVALS_DIR / "fixtures"
THRESHOLDS_PATH = _EVALS_DIR / "thresholds.toml"

# ---------------------------------------------------------------------------
# The Phase 4c dependency. Guarded import: this is expected to fail today
# (ModuleNotFoundError) because src.pipeline.matching.orchestrator does not
# exist until Phase 4c ("4c - Matching engine" in docs/EXTRACTION_PLAN.md).
# We do NOT let this raise at import time so that a future test suite can
# still `import run_evals` and unit-test the fixture-loading helpers below
# without an orchestrator; `main()` is what enforces the hard failure.
# ---------------------------------------------------------------------------
_ORCHESTRATOR_IMPORT_ERROR: ModuleNotFoundError | None
try:
    from src.pipeline.matching.orchestrator import (  # type: ignore[import-not-found]
        run_match,
    )
except ModuleNotFoundError as exc:  # pragma: no cover - exercised until 4c lands
    run_match = None
    _ORCHESTRATOR_IMPORT_ERROR = exc
else:  # pragma: no cover - not reachable until 4c lands
    _ORCHESTRATOR_IMPORT_ERROR = None

_NOT_IMPLEMENTED_MSG = (
    "ranking-evals: src.pipeline.matching.orchestrator is not implemented yet "
    "(Phase 4c of docs/EXTRACTION_PLAN.md -- 'Matching engine'). This is the "
    "expected RED state for Phase 4a (evals corpus). Once 4c lands the "
    "orchestrator (`run_match` or equivalent stage-1..4 entrypoint), wire it "
    "into `_run_corpus()` below and this script will compute precision@k, "
    "evidence-verification-rate, PII-leak, and determinism against "
    "thresholds.toml. This is NOT a fixture bug -- "
    "`pytest tests/unit/test_evals_corpus.py` proves the corpus itself is "
    "valid independently of the orchestrator."
)


@dataclass(frozen=True)
class ResumeFixture:
    """One labelled resume fixture loaded from fixtures/resumes/*.json."""

    resume_id: str
    tag: str
    path: Path
    parsed: dict[str, Any]


@dataclass(frozen=True)
class Corpus:
    job_id: str
    job_extracted: dict[str, Any]
    resumes: tuple[ResumeFixture, ...]


def load_thresholds() -> dict[str, Any]:
    """Parse thresholds.toml. Pure I/O + parsing -- no orchestrator needed."""
    with THRESHOLDS_PATH.open("rb") as fh:
        return tomllib.load(fh)


def _fixture_path(relative: str) -> Path:
    """Resolve a labels.json `fixture` value inside FIXTURES_DIR.

    Defense in depth (test-only surface, low severity): the value comes from a
    JSON file, and in pathlib an ABSOLUTE right-hand side silently REPLACES the
    left-hand side (``Path("/a") / "/etc/passwd" == Path("/etc/passwd")``), so
    a plain join would happily read anything on disk. Resolve and confine.
    """
    root = FIXTURES_DIR.resolve()
    candidate = (root / relative).resolve()
    if not candidate.is_relative_to(root):
        raise ValueError(
            f"labels.json fixture path {relative!r} resolves to {candidate}, "
            f"which is outside the fixtures directory {root}"
        )
    return candidate


def load_corpus() -> Corpus:
    """Load the JD + labelled resumes from fixtures/. Pure I/O -- no
    orchestrator needed; this is what test_evals_corpus.py exercises today."""
    with (FIXTURES_DIR / "labels.json").open("r", encoding="utf-8") as fh:
        labels = json.load(fh)

    job_meta = labels["job"]
    with _fixture_path(job_meta["fixture"]).open("r", encoding="utf-8") as fh:
        job_extracted = json.load(fh)

    resumes: list[ResumeFixture] = []
    for resume_id, entry in labels["resumes"].items():
        path = _fixture_path(entry["fixture"])
        with path.open("r", encoding="utf-8") as fh:
            parsed = json.load(fh)
        resumes.append(
            ResumeFixture(
                resume_id=resume_id,
                tag=entry["tag"],
                path=path,
                parsed=parsed,
            )
        )

    return Corpus(
        job_id=job_meta["id"], job_extracted=job_extracted, resumes=tuple(resumes)
    )


# ---------------------------------------------------------------------------
# Phase 4c: the offline, deterministic corpus engine.
#
# run_evals runs with NO live Postgres/Neo4j/Ollama (it must exit 0 in a bare
# `python:3.11-slim`), so it reproduces the live orchestrator's 4-stage
# arithmetic against the fixtures directly, using the SAME pure stage functions
# the DB path uses (src.pipeline.matching.stages) plus the SAME skill
# normalisation / family ontology (src.pipeline.skills / skills_graph) and the
# SAME PII redaction the embed boundary uses (src.worker.resume_tasks). Only the
# two I/O surfaces the script can't have offline are stood in for
# deterministically:
#
#   * the embedder -> a deterministic bag-of-words hashing embedder
#     (`_embed`): byte-identical text embeds to an identical vector (so the
#     byte-identical twins tie EXACTLY, as the corpus's min_score_gap design
#     requires) and a one-token edit moves the vector only slightly (so the
#     education / skill_missing_must residuals stay dominated by their real
#     sub-score gaps, never inverted the way a crc32-random embedder would).
#   * the stage-3 LLM extractor -> `_extract_evidence`: a faithful extractor
#     quotes a requirement's evidence from a resume CHUNK that actually contains
#     the skill term (deterministic vocab scan). A keyword-stuffer whose chunks
#     carry only filler (r09) yields no quote for any requirement -> its
#     evidence completeness is 0 by construction, exactly the mechanism the
#     anti-fabrication backstop relies on. The verifier (stages.verify_evidence,
#     rapidfuzz partial_ratio at 0.85) is the REAL one.
#
# Determinism (thresholds.toml [determinism]): the embedder carries no RNG and
# `today_year` is pinned (`_EVAL_TODAY_YEAR`), so two runs are bit-identical.
# Cache state: cold / none (in-process, no Redis) — so the determinism check
# compares the engine to itself, never a warm cache to itself.
# ---------------------------------------------------------------------------

_EVAL_TODAY_YEAR = 2026  # pinned "now" — the corpus's recent bucket is 2026
_EMBED_DIM = 768
_NON_MATCHABLE_FAMILIES = ("other", "domain")


def _embed(text: str) -> list[float]:
    """Deterministic bag-of-words hashing embedder (768-d).

    Semantically SMOOTH — a one-token edit changes exactly one bucket, so twins
    that differ by a single skill/degree token stay near-parallel (unlike a
    crc32-of-the-whole-string embedder, which would make a one-token edit
    orthogonal and invert the small vector residuals the twin pairs depend on
    being dominated). Byte-identical text -> identical vector.
    """
    vec = [0.0] * _EMBED_DIM
    for tok in re.findall(r"[a-z0-9]+", text.lower()):
        vec[hash_token(tok) % _EMBED_DIM] += 1.0
    return vec


def hash_token(tok: str) -> int:
    """Stable, process-independent token hash (Python's ``hash`` is salted per
    process, which would break the determinism guarantee across runs)."""
    return int.from_bytes(hashlib.sha1(tok.encode("utf-8")).digest()[:8], "big")


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _seniority_subscore(jd_title: str, recent_title: str, floor: float) -> float:
    """Cosine(jd.title, most-recent role title), rescaled [floor, 1] -> [0, 1] —
    the same shape orchestrator.py computes with a live embedder. r09's title is
    the JD title verbatim, so cosine(x, x) = 1.0 -> seniority 1.0 by arithmetic."""
    cos = _cosine(_embed(jd_title), _embed(recent_title))
    return max(0.0, min(1.0, (cos - floor) / (1.0 - floor)))


def _run_corpus(corpus: Corpus, thresholds: dict[str, Any]) -> None:
    """Run the corpus through the live stage functions and assert every
    thresholds.toml gate. Raises AssertionError on any violation (so
    ``main()`` returns non-zero); returns None on a fully green corpus."""
    # Imports are local so the module still imports (and its fixture-loading
    # helpers stay unit-testable) even in the pre-4c import-guard state above.
    from src.pipeline.matching.orchestrator import RankInput, run_match
    from src.schemas.matching import DEFAULT_WEIGHTS, MatchWeights

    weights = DEFAULT_WEIGHTS
    scored = _score_corpus(corpus, weights)
    ranked = run_match(
        [
            RankInput(
                resume_id=rid,
                structured=s.structured,
                breakdown=s.breakdown,
                evidence=s.evidence,
            )
            for rid, s in scored.items()
        ],
        weights,
    )
    by_id = {m.resume_id: m for m in ranked}
    tag_by_id = {r.resume_id: r.tag for r in corpus.resumes}
    labels = _labels()

    # ── precision@k ────────────────────────────────────────────────────────
    k = int(thresholds["precision_at_k"]["k"])
    min_precision = float(thresholds["precision_at_k"]["min_precision"])
    topk = sorted(ranked, key=lambda m: m.rank)[:k]
    good = sum(1 for m in topk if tag_by_id[m.resume_id] in {"strong", "borderline"})
    precision = good / len(topk)
    assert precision >= min_precision, (
        f"precision@{k}={precision:.3f} < {min_precision}: top-k = "
        f"{[(m.resume_id, tag_by_id[m.resume_id]) for m in topk]}"
    )

    # ── adversarial / weak backstop: must_not_surface_in_topk ──────────────
    topk_ids = {m.resume_id for m in topk}
    for rid, entry in labels["resumes"].items():
        if entry.get("must_not_surface_in_topk"):
            assert rid not in topk_ids, (
                f"{rid} is flagged must_not_surface_in_topk but appears in the "
                f"top-{k} (rank {by_id[rid].rank}, score {by_id[rid].score_final:.4f})"
            )

    # ── adversarial: must_rank_below_every_strong (ROADMAP A3) ─────────────
    if thresholds["adversarial"].get("must_rank_below_every_strong"):
        _assert_bait_ranks_below_every_strong(ranked, tag_by_id)

    # ── evidence: verification_rate + min_completeness_in_topk ─────────────
    verify_fuzz = float(thresholds["evidence"]["fuzz_threshold"])
    assert (
        verify_fuzz == MatchWeights().evidence_verify_fuzz
    ), "thresholds.toml fuzz_threshold drifted from MatchWeights.evidence_verify_fuzz"
    min_quote_chars = int(thresholds["evidence"]["min_quote_chars"])
    assert min_quote_chars == MatchWeights().evidence_min_quote_chars, (
        "thresholds.toml min_quote_chars drifted from "
        "MatchWeights.evidence_min_quote_chars"
    )
    for m in topk:
        surviving = [
            r
            for r in (m.evidence.requirements if m.evidence else [])
            if r.status == "met" and r.evidence
        ]
        assert surviving, (
            f"min_completeness_in_topk: top-k entry {m.resume_id} carries no "
            "verified evidence quote"
        )
    # Every SURFACED quote across the whole ranking must verify against its
    # cited chunk (verification_rate_min = 1.0). Surfaced == survived the
    # verifier: non-empty evidence, full stop. Conditioning on a citation as
    # well used to exclude uncited quotes from this check entirely.
    for rid, s in scored.items():
        chunks_by_id = s.chunks_by_id
        for req in s.evidence.requirements if s.evidence else []:
            if req.evidence:
                assert any(
                    _partial_ratio(req.evidence, chunks_by_id.get(cid, ""))
                    >= verify_fuzz
                    for cid in req.evidence_chunk_ids
                ), f"{rid}: surfaced quote does not verify against its cited chunk"

    # ── negative_evidence_must_fail ────────────────────────────────────────
    if thresholds["evidence"]["negative_evidence_must_fail"]:
        _assert_fabrications_are_scrubbed(scored, weights)
        _assert_superset_quotes_are_scrubbed(scored, weights)

    # ── gold_recall_min ────────────────────────────────────────────────────
    gold_recall_min = float(thresholds["evidence"]["gold_recall_min"])
    _assert_gold_recall(scored, gold_recall_min, verify_fuzz)

    # ── the gold anchor TEXT itself, through the real verifier ─────────────
    _assert_gold_anchor_text_survives_the_verifier(corpus, verify_fuzz)

    # ── ordering_controls ──────────────────────────────────────────────────
    if thresholds["ordering_controls"]["enforce"]:
        min_gap = float(thresholds["ordering_controls"]["min_score_gap"])
        for pair in thresholds["ordering_controls"]["pairs"]:
            hi, lo = pair["higher_id"], pair["lower_id"]
            hi_m, lo_m = by_id[hi], by_id[lo]
            assert hi_m.rank < lo_m.rank, (
                f"ordering_controls[{pair['dimension']}]: rank({hi})={hi_m.rank} "
                f"is not above rank({lo})={lo_m.rank}"
            )
            gap = hi_m.score_final - lo_m.score_final
            assert gap >= min_gap, (
                f"ordering_controls[{pair['dimension']}]: gap {gap:.6e} < "
                f"min_score_gap {min_gap:.6e}"
            )

    # ── must_have_miss_penalty is WIRED (review obligation on r18 alone) ───
    _assert_must_have_penalty_fires_on_r18(corpus, weights)

    # ── ADR-028 education field-of-study relevance (single-fixture guard) ──
    _assert_education_field_relevance(corpus, weights)

    # ── pii leak-check ─────────────────────────────────────────────────────
    if thresholds["pii"]["leak_check"]:
        _assert_no_pii_leak(corpus, scored, topk_ids)

    # ── determinism (two runs, pinned seed / cold cache) ───────────────────
    _assert_deterministic(corpus, weights, ranked)


@dataclass(frozen=True)
class _Scored:
    """Everything a single resume contributes to the ranking + the audits."""

    structured: float
    breakdown: Any
    evidence: Any
    chunks_by_id: dict[str, str]
    vec_raw: float


def _labels() -> dict[str, Any]:
    with (FIXTURES_DIR / "labels.json").open(encoding="utf-8") as fh:
        result: dict[str, Any] = json.load(fh)
        return result


def _partial_ratio(needle: str, haystack: str) -> float:
    from rapidfuzz import fuzz

    if not needle:
        return 0.0
    return float(fuzz.partial_ratio(needle.lower().strip(), haystack.lower())) / 100.0


def _jd_view(corpus: Corpus) -> dict[str, Any]:
    """Materialise the JD once: canonical required/nice skills + min_years,
    education level, title, and the JD's own summary embedding text."""
    from src.pipeline.skills import _basic_normalise, build_summary_text
    from src.schemas.jobs import JDExtracted

    jd = JDExtracted.model_validate(corpus.job_extracted)
    required = [
        (s.name, _basic_normalise(s.name), s.min_years) for s in jd.required_skills
    ]
    nice = [(s.name, _basic_normalise(s.name)) for s in jd.nice_to_have_skills]
    return {
        "title": jd.title,
        "min_years": jd.min_years_experience or None,
        "edu_min_level": jd.education.min_level if jd.education else None,
        "edu_fields": tuple(jd.education.fields) if jd.education else (),
        "required": required,
        "nice": nice,
        "summary_text": build_summary_text(jd),
    }


def _candidate_families(canonicals: set[str]) -> set[str]:
    from src.pipeline.skills_graph import categories_for

    fams: set[str] = set()
    for c in canonicals:
        for f in categories_for(c):
            if f not in _NON_MATCHABLE_FAMILIES:
                fams.add(f)
    return fams


def _skill_rows_for(parsed: dict[str, Any], jd: dict[str, Any]) -> list[Any]:
    """Reproduce _stage2_skill_rows against fixture JSON: direct match ->
    ontology_weight 1.0; shared curated family -> 0.5; else 0.0. REQUIRES-only
    (blocker #5: nice-to-haves never feed this)."""
    from src.pipeline.matching.stages import _SkillRowFromCypher
    from src.pipeline.skills import _basic_normalise
    from src.pipeline.skills_graph import categories_for

    cand: dict[str, dict[str, Any]] = {}
    for s in parsed.get("skills", []):
        canon = _basic_normalise(s["name"])
        if canon:
            cand.setdefault(canon, s)
    cand_fams = _candidate_families(set(cand))

    rows: list[Any] = []
    for _name, canon, min_years in jd["required"]:
        if canon in cand:
            s = cand[canon]
            rows.append(
                _SkillRowFromCypher(
                    skill=canon,
                    req_years=min_years,
                    is_must_have=True,
                    years=s.get("years"),
                    last_used_year=s.get("last_used_year"),
                    ontology_weight=1.0,
                )
            )
            continue
        req_fams = {
            f for f in categories_for(canon) if f not in _NON_MATCHABLE_FAMILIES
        }
        weight = 0.5 if req_fams & cand_fams else 0.0
        rows.append(
            _SkillRowFromCypher(
                skill=canon,
                req_years=min_years,
                is_must_have=True,
                years=None,
                last_used_year=None,
                ontology_weight=weight,
            )
        )
    return rows


def _extract_evidence(
    parsed: dict[str, Any], jd: dict[str, Any]
) -> tuple[Any, dict[str, str]]:
    """Deterministic stand-in for the stage-3 LLM extractor + the REAL verifier.

    For each requirement (required + nice-to-have — blocker #5) find a resume
    CHUNK that actually contains the skill term (vocab scan) and quote it; a
    keyword-stuffer whose chunks carry only filler yields no quote and the
    requirement comes back missing. Then run the real anti-fabrication verifier.
    """
    from src.pipeline.matching.stages import verify_evidence
    from src.pipeline.skills import match_skills_in_text
    from src.schemas.matching import (
        CoverLetterEvidence,
        EvidenceObject,
        RequirementEvidence,
    )

    chunks = parsed.get("chunks") or []
    chunks_by_id = {c["id"]: c["text"] for c in chunks if c.get("id")}
    # canonical skill -> first chunk that mentions it
    chunk_for_canon: dict[str, tuple[str, str]] = {}
    for c in chunks:
        found = match_skills_in_text(c.get("text", ""))
        for canon in found:
            chunk_for_canon.setdefault(canon, (c["id"], c["text"]))

    reqs: list[Any] = []
    for name, canon, _my in jd["required"]:
        hit = chunk_for_canon.get(canon)
        if hit:
            reqs.append(
                RequirementEvidence(
                    requirement=name,
                    status="met",
                    evidence=hit[1],
                    evidence_chunk_ids=[hit[0]],
                    confidence=0.9,
                )
            )
        else:
            reqs.append(RequirementEvidence(requirement=name, status="missing"))
    for name, canon in jd["nice"]:
        hit = chunk_for_canon.get(canon)
        if hit:
            reqs.append(
                RequirementEvidence(
                    requirement=name,
                    status="met",
                    evidence=hit[1],
                    evidence_chunk_ids=[hit[0]],
                    confidence=0.9,
                )
            )
        else:
            reqs.append(RequirementEvidence(requirement=name, status="missing"))

    cover: list[Any] = []
    cl_chunks = parsed.get("cover_letter_chunks") or []
    for c in cl_chunks:
        chunks_by_id[c["id"]] = c["text"]
    if cl_chunks:
        first = cl_chunks[0]
        cover.append(
            CoverLetterEvidence(
                theme="motivation",
                evidence=first["text"],
                evidence_chunk_ids=[first["id"]],
                confidence=0.9,
            )
        )
    evidence = EvidenceObject(requirements=reqs, cover_letter_evidence=cover)
    verified = verify_evidence(evidence, chunks_by_id)
    return verified, chunks_by_id


def _breakdown_for(
    parsed: dict[str, Any], jd: dict[str, Any], vec_normalised: float, weights: Any
) -> tuple[float, Any]:
    from src.pipeline.matching.orchestrator import (
        _level_from_degree,
        _most_recent_title,
    )
    from src.pipeline.matching.stages import (
        is_senior_candidate,
        score_education,
        score_experience,
        score_skill_breakdown,
    )
    from src.schemas.matching import ScoreBreakdown

    total_years = parsed.get("total_years_experience")
    senior = is_senior_candidate(total_years, jd["min_years"], weights=weights)
    rows = _skill_rows_for(parsed, jd)
    skill_overall, contribs = score_skill_breakdown(
        rows, weights=weights, senior=senior, today_year=_EVAL_TODAY_YEAR
    )
    exp = score_experience(total_years, jd["min_years"], weights=weights)
    edu_entries = parsed.get("education", []) or []
    levels = [_level_from_degree(e.get("degree")) for e in edu_entries]
    cand_fields = [e.get("field") for e in edu_entries]
    edu = score_education(
        levels,
        jd["edu_min_level"],
        candidate_fields=cand_fields,
        jd_fields=jd["edu_fields"],
        weights=weights,
    )
    recent = _most_recent_title(parsed)
    seniority = (
        _seniority_subscore(jd["title"], recent, weights.seniority_floor)
        if recent
        else 0.0
    )
    structured = (
        weights.skill * skill_overall
        + weights.experience * exp
        + weights.education * edu
        + weights.seniority * seniority
        + weights.vector * vec_normalised
    )
    breakdown = ScoreBreakdown(
        skill=skill_overall,
        experience=exp,
        education=edu,
        seniority=seniority,
        vector=vec_normalised,
        structured=structured,
        implied_experience=any(c.reason == "implied-experience" for c in contribs),
        skill_contributions=contribs,
    )
    return structured, breakdown


def _score_corpus(corpus: Corpus, weights: Any) -> dict[str, _Scored]:
    """Full stages 1-4 (minus the final combine) per resume, against fixtures."""
    from src.pipeline.matching.stages import normalise_vector_scores
    from src.schemas.resumes import ResumeParsed
    from src.worker.resume_tasks import _build_summary_text, _redact_candidate_pii

    jd = _jd_view(corpus)
    jd_emb = _embed(jd["summary_text"])

    # Stage-1 vector score = cosine(JD summary, redacted resume summary), then
    # min-max normalised across the pool (stages.normalise_vector_scores).
    vec_raw: dict[str, float] = {}
    for r in corpus.resumes:
        rp = ResumeParsed.model_validate(r.parsed)
        summary_text = _redact_candidate_pii(_build_summary_text(rp), rp.candidate)
        vec_raw[r.resume_id] = _cosine(jd_emb, _embed(summary_text))
    ids = [r.resume_id for r in corpus.resumes]
    vec_norm = dict(
        zip(ids, normalise_vector_scores([vec_raw[i] for i in ids]), strict=True)
    )

    out: dict[str, _Scored] = {}
    for r in corpus.resumes:
        structured, breakdown = _breakdown_for(
            r.parsed, jd, vec_norm[r.resume_id], weights
        )
        evidence, chunks_by_id = _extract_evidence(r.parsed, jd)
        out[r.resume_id] = _Scored(
            structured=structured,
            breakdown=breakdown,
            evidence=evidence,
            chunks_by_id=chunks_by_id,
            vec_raw=vec_raw[r.resume_id],
        )
    return out


# ---------------------------------------------------------------------------
# ADR-022 round-2 probe builders.
#
# EVERY invisible character below is built with ``chr()`` and never written as
# a literal. A literal NUL broke this branch's compilation three times and a
# literal SOFT HYPHEN in a fixture is unreviewable in a diff; the whole point
# of these probes is invisible-character handling, so the source that builds
# them must stay visibly auditable.
# ---------------------------------------------------------------------------

_SOFT_HYPHEN = chr(0x00AD)  # U+00AD, what a PDF emits at a line-break point
_NEWLINE = chr(0x0A)


def _soft_hyphenate(text: str, every: int = 5) -> str:
    """Insert a SOFT HYPHEN every ``every`` characters (PDF line-break debris).

    ADR-022 measures the resulting fuzz score against a CLEAN needle at 0.838
    for ``every == 5`` — below the 0.85 bar, i.e. the résumé's own text
    rejected as a fabrication — unless the invisible/bidi scrub is applied to
    the HAYSTACK as well as the needle.
    """
    out: list[str] = []
    for i, ch in enumerate(text):
        out.append(ch)
        if (i + 1) % every == 0:
            out.append(_SOFT_HYPHEN)
    return "".join(out)


def _column_pad(text: str) -> str:
    """Re-render with 4-space column padding (measured raw: 0.826)."""
    return text.replace(" ", " " * 4)


def _triple_space(text: str) -> str:
    """Re-render triple-spaced (raw needle outgrows its chunk -> length guard)."""
    return text.replace(" ", " " * 3)


def _newline_wrap(text: str, width: int = 40) -> str:
    """Hard-wrap at ``width`` columns (measured raw: 0.986 — a PASSING control)."""
    lines: list[str] = []
    line: list[str] = []
    used = 0
    for tok in text.split(" "):
        if line and used + len(tok) > width:
            lines.append(" ".join(line))
            line, used = [], 0
        line.append(tok)
        used += len(tok) + 1
    if line:
        lines.append(" ".join(line))
    return _NEWLINE.join(lines)


# variant name -> (gold anchor, cited chunk text) -> the needle to submit.
_RENDER_VARIANTS: dict[str, Callable[[str, str], str]] = {
    "column_padded_4": lambda anchor, _chunk: _column_pad(anchor),
    "triple_spaced_cited_chunk": lambda _anchor, chunk: _triple_space(chunk),
    "newline_wrapped_40": lambda anchor, _chunk: _newline_wrap(anchor),
}


def _assert_quote_is_scrubbed(
    label: str,
    quote: str,
    chunk_id: str,
    chunks_by_id: dict[str, str],
    weights: Any,
) -> None:
    """Submit ``quote`` as the evidence for ``chunk_id`` through the REAL
    verifier and require it to come back blanked and demoted."""
    from src.pipeline.matching.stages import verify_evidence
    from src.schemas.matching import EvidenceObject, RequirementEvidence

    cleaned = verify_evidence(
        EvidenceObject(
            requirements=[
                RequirementEvidence(
                    requirement=f"{label} probe",
                    status="met",
                    evidence=quote,
                    evidence_chunk_ids=[chunk_id],
                    confidence=0.9,
                )
            ]
        ),
        chunks_by_id,
        weights=weights,
    ).requirements[0]
    assert cleaned.evidence == "" and cleaned.status == "missing", (
        f"{label}: a quote of {len(quote)} chars against {chunk_id} "
        f"({len(chunks_by_id[chunk_id])} chars) was NOT scrubbed "
        f"(status={cleaned.status!r}, evidence={cleaned.evidence[:80]!r}...). "
        "Do NOT relax this by weakening the probe — it is a guard regression."
    )


def _assert_superset_quotes_are_scrubbed(
    scored: dict[str, _Scored], weights: Any
) -> None:
    """labels.json's ``superset_evidence`` + ``degenerate_evidence`` probes.

    Symmetric with ``_assert_fabrications_are_scrubbed`` and gated by the same
    ``[evidence].negative_evidence_must_fail`` key: these are fabrications too,
    just of the two shapes ``partial_ratio`` scores 1.000 on. A SUPERSET quote
    contains its whole cited chunk verbatim plus appended invention (ADR-022
    follow-up #1); a DEGENERATE quote is a bare token the chunk genuinely
    contains (follow-up #4). Neither is falsifiable by the corpus's own
    surfaced quotes, which are byte-identical to their chunks.
    """
    labels = _labels()
    n = 0
    for rid, entry in labels["resumes"].items():
        chunks_by_id = scored[rid].chunks_by_id
        for probe_id, spec in entry.get("superset_evidence", {}).items():
            cid = spec["chunk_id"]
            assert cid in chunks_by_id, f"{rid}: superset chunk {cid} missing"
            quote = chunks_by_id[cid]
            other = spec.get("concat_chunk_id")
            if other is not None:
                assert other in chunks_by_id, f"{rid}: superset chunk {other} missing"
                quote = f"{quote} {chunks_by_id[other]}"
            quote += spec.get("append", "")
            assert len(quote) > len(chunks_by_id[cid]), (
                f"{rid}/{probe_id}: a superset probe must be strictly LONGER "
                "than its cited chunk or it probes nothing"
            )
            _assert_quote_is_scrubbed(
                f"{rid}/{probe_id}", quote, cid, chunks_by_id, weights
            )
            n += 1
        for probe_id, spec in entry.get("degenerate_evidence", {}).items():
            cid = spec["chunk_id"]
            assert cid in chunks_by_id, f"{rid}: degenerate chunk {cid} missing"
            quote = spec["quote"]
            assert quote.lower() in chunks_by_id[cid].lower(), (
                f"{rid}/{probe_id}: {quote!r} is not genuinely present in {cid} "
                "— a degenerate probe only tests the length floor if the text "
                "itself would otherwise score 1.000"
            )
            _assert_quote_is_scrubbed(
                f"{rid}/{probe_id}", quote, cid, chunks_by_id, weights
            )
            n += 1
    assert n == 4, f"expected 4 superset/degenerate probes, ran {n}"
    _assert_a_blank_needle_never_verifies()


def _assert_a_blank_needle_never_verifies() -> None:
    """``_fuzz_ratio``'s empty-needle guard, probed where it is REACHABLE.

    The guard is normally shadowed: with ``evidence_min_quote_chars`` at its
    default 16 an empty needle is already rejected by the length floor one line
    later, so deleting the guard is an EQUIVALENT mutation over the whole
    corpus. ``evidence_min_quote_chars`` is env-settable and ``ge=0``, and at 0
    the guard is the only thing left — ``rapidfuzz.fuzz.partial_ratio("", "")``
    returns 100, so a whitespace-only quote against an empty chunk VERIFIES AT
    1.000 without it.

    The needle is whitespace rather than an invisible character on purpose:
    ``CleanText`` scrubs the invisible class at the schema boundary, so an
    all-invisible quote never reaches the verifier as a non-empty string at
    all. ``verify_evidence`` then ``.strip()``s, which is what makes this the
    empty-needle path rather than the floor path.
    """
    from src.pipeline.matching.stages import verify_evidence
    from src.schemas.matching import EvidenceObject, MatchWeights, RequirementEvidence

    blank = chr(0x09) + chr(0x20) * 4 + chr(0x0A)
    cleaned = verify_evidence(
        EvidenceObject(
            requirements=[
                RequirementEvidence(
                    requirement="blank-needle probe",
                    status="met",
                    evidence=blank,
                    evidence_chunk_ids=["c_blank"],
                    confidence=0.9,
                )
            ]
        ),
        {"c_blank": ""},
        weights=MatchWeights(evidence_min_quote_chars=0),
    ).requirements[0]
    assert cleaned.evidence == "" and cleaned.status == "missing", (
        "a whitespace-only quote verified against an empty chunk with the "
        "min-quote floor configured to 0 — _fuzz_ratio's empty-needle guard "
        f"is gone (status={cleaned.status!r}, evidence={cleaned.evidence!r})"
    )


def _assert_fabrications_are_scrubbed(scored: dict[str, _Scored], weights: Any) -> None:
    """Every labels.json negative_evidence anchor is a FABRICATION and must be
    blanked by the verifier (score < evidence_verify_fuzz against its chunk)."""
    from src.pipeline.matching.stages import verify_evidence
    from src.schemas.matching import EvidenceObject, RequirementEvidence

    labels = _labels()
    n = 0
    for rid, entry in labels["resumes"].items():
        neg = entry.get("negative_evidence", {})
        chunks_by_id = scored[rid].chunks_by_id
        for chunk_id, fabricated in neg.items():
            assert chunk_id in chunks_by_id, f"{rid}: negative chunk {chunk_id} missing"
            ev = EvidenceObject(
                requirements=[
                    RequirementEvidence(
                        requirement="fabrication probe",
                        status="met",
                        evidence=fabricated,
                        evidence_chunk_ids=[chunk_id],
                        confidence=0.9,
                    )
                ]
            )
            cleaned = verify_evidence(ev, chunks_by_id, weights=weights).requirements[0]
            assert cleaned.evidence == "" and cleaned.status == "missing", (
                f"{rid}: fabricated quote against {chunk_id} was NOT scrubbed "
                f"(evidence={cleaned.evidence!r})"
            )
            n += 1
    assert n == 4, f"expected 4 negative_evidence fabrications, ran {n}"


def _assert_gold_recall(
    scored: dict[str, _Scored], gold_recall_min: float, verify_fuzz: float
) -> None:
    """Every labels.json gold_evidence anchor must come back met+verified from
    the FULL extract-then-verify pipeline (extractor recall, not just verifier
    precision)."""
    labels = _labels()
    total = 0
    recovered = 0
    for rid, entry in labels["resumes"].items():
        gold = entry.get("gold_evidence", {})
        if not gold:
            continue
        s = scored[rid]
        by_req = {
            r.requirement: r for r in (s.evidence.requirements if s.evidence else [])
        }
        for skill_name in gold:
            total += 1
            req = by_req.get(skill_name)
            if (
                req is not None
                and req.status == "met"
                and req.evidence
                and any(
                    _partial_ratio(req.evidence, s.chunks_by_id.get(cid, ""))
                    >= verify_fuzz
                    for cid in req.evidence_chunk_ids
                )
            ):
                recovered += 1
    assert total > 0, "no gold_evidence anchors found in labels.json"
    recall = recovered / total
    assert (
        recall >= gold_recall_min
    ), f"gold_recall={recall:.3f} ({recovered}/{total}) < {gold_recall_min}"


def _assert_gold_anchor_text_survives_the_verifier(
    corpus: Corpus, verify_fuzz: float
) -> None:
    """Every gold anchor's own TEXT, run through the REAL verifier.

    ``_assert_gold_recall`` above reads only the anchor KEYS: the stand-in
    extractor quotes a whole chunk, so the anchor STRING never reaches
    ``verify_evidence`` and its LENGTH is gated by nothing. That is exactly why
    ``min_quote_chars`` was the one threshold in thresholds.toml whose value no
    fixture could falsify — every anchor was 71-123 characters, so any floor up
    to 71 passed this gate untouched (security FINDING 4).

    This loop closes that. Each anchor is submitted as the quote for its own
    cited chunk and must come back UNBLANKED and still ``met``. Because
    ``verify_evidence`` applies the minimum-quote-length floor, r03's short
    29-char anchor fails here at any floor above 29 — which is what makes the
    human's 32 -> 16 decision a corpus-enforced value rather than a comment.

    ADR-022 round 2 extends the same loop to the two NORMALISATIONS inside
    ``_fuzz_ratio``, which the corpus was equally blind to for the same reason
    (a whole-chunk quote needs no normalisation, so disabling one is a no-op):

    * ``gold_anchor_render_variants`` — the anchor RE-RENDERED the way an
      extractor emits it (column padding, hard wrapping, triple spacing),
      against its own unmodified chunk. Falsifies the whitespace collapse.
    * ``invisible_char_haystack`` — a CLEAN anchor against a chunk re-rendered
      with SOFT HYPHENs. The only probe that perturbs the HAYSTACK, and so the
      only one that can tell a symmetric invisible-character scrub from a
      needle-only one.

    All of them assert SURVIVAL, not rejection: they are the résumé's own text
    in a different rendering, and scrubbing them is the verifier calling real
    evidence a fabrication.
    """
    from src.pipeline.matching.stages import verify_evidence
    from src.schemas.matching import (
        EvidenceObject,
        RequirementEvidence,
    )

    def _must_survive(
        label: str,
        needle: str,
        cited_ids: list[str],
        haystacks: dict[str, str],
    ) -> None:
        req = verify_evidence(
            EvidenceObject(
                requirements=[
                    RequirementEvidence(
                        requirement=label,
                        status="met",
                        evidence=needle,
                        evidence_chunk_ids=cited_ids,
                        confidence=0.9,
                    )
                ]
            ),
            haystacks,
        ).requirements[0]
        assert req.evidence == needle and req.status == "met", (
            f"{label} ({len(needle)} chars) was SCRUBBED by the verifier — it "
            "is a genuine span of its cited chunk, so either a threshold is "
            "too high, a normalisation was removed, or the fixture is wrong. "
            "Do NOT relax a threshold to clear this."
        )

    labels = _labels()
    parsed_by_id = {r.resume_id: r.parsed for r in corpus.resumes}
    checked = 0
    rendered_checked = 0
    haystack_checked = 0
    shortest = None
    for rid, entry in labels["resumes"].items():
        gold = entry.get("gold_evidence", {})
        if not gold:
            continue
        parsed = parsed_by_id[rid]
        chunks_by_id = {
            c["id"]: c["text"] for c in (parsed.get("chunks") or []) if c.get("id")
        }
        skill_ids = {
            s["name"]: s.get("evidence_chunk_ids", []) for s in parsed.get("skills", [])
        }
        for skill_name, quote in gold.items():
            cited = skill_ids.get(skill_name, [])
            assert cited, f"{rid}: gold anchor {skill_name!r} cites no chunk"
            verified = verify_evidence(
                EvidenceObject(
                    requirements=[
                        RequirementEvidence(
                            requirement=skill_name,
                            status="met",
                            evidence=quote,
                            evidence_chunk_ids=list(cited),
                            confidence=0.9,
                        )
                    ]
                ),
                chunks_by_id,
            )
            req = verified.requirements[0]
            assert req.evidence == quote and req.status == "met", (
                f"{rid}: gold anchor {skill_name!r} ({len(quote)} chars) was "
                "SCRUBBED by the verifier — it is a genuine exact substring of "
                "its cited chunk, so either the floor is too high or the "
                "fixture is wrong. Do NOT relax a threshold to clear this."
            )
            checked += 1
            shortest = len(quote) if shortest is None else min(shortest, len(quote))

            # ── re-rendered anchors (whitespace collapse) ──────────────────
            cited_text = chunks_by_id[cited[0]]
            for variant in entry.get("gold_anchor_render_variants", {}).get(
                skill_name, []
            ):
                _must_survive(
                    f"{rid}: gold anchor {skill_name!r} rendered {variant}",
                    _RENDER_VARIANTS[variant](quote, cited_text),
                    [cited[0]],
                    chunks_by_id,
                )
                rendered_checked += 1

            # ── perturbed HAYSTACK (symmetry of the invisible-char scrub) ──
            shy_chunk = entry.get("invisible_char_haystack", {}).get(skill_name)
            if shy_chunk is not None:
                assert (
                    shy_chunk in chunks_by_id
                ), f"{rid}: invisible_char_haystack cites unknown chunk {shy_chunk!r}"
                _must_survive(
                    f"{rid}: gold anchor {skill_name!r} against a "
                    "soft-hyphenated chunk",
                    quote,
                    [shy_chunk],
                    {shy_chunk: _soft_hyphenate(chunks_by_id[shy_chunk])},
                )
                haystack_checked += 1

    assert checked > 0, "no gold anchors were verified"
    assert rendered_checked == 3, (
        f"expected 3 re-rendered gold-anchor variants, ran {rendered_checked} "
        "— labels.json's gold_anchor_render_variants block is the only thing "
        "that falsifies _collapse_whitespace"
    )
    assert haystack_checked == 1, (
        f"expected 1 soft-hyphenated-haystack probe, ran {haystack_checked} — "
        "labels.json's invisible_char_haystack block is the only thing that "
        "falsifies the SYMMETRY of the invisible-character scrub"
    )
    assert shortest is not None and shortest < 32, (
        f"shortest gold anchor is {shortest} chars; with none below the "
        "pre-FINDING-4 floor of 32 the corpus cannot detect a revert of the "
        "min_quote_chars decision"
    )


def _assert_bait_ranks_below_every_strong(
    ranked: Sequence[Any], tag_by_id: dict[str, str]
) -> None:
    """ROADMAP A3 — enforce the order relation ``[adversarial]`` only asserted
    in PROSE.

    ``thresholds.toml``'s ``[adversarial]`` block states, as its round-6
    reconciliation, that *"the bait is BELOW EVERY STRONG FIXTURE and therefore
    outside the k=5 window"*, with ~0.19 of measured margin either way. That
    sentence was a comment. Nothing read it. **A change that violated it still
    exited 0** — which is exactly what happened: the ADR-032 attempt inverted
    the bait-below-strong ordering and had to be reverted, and no gate in this
    repo objected.

    This is the A7 defect shape occurring *inside the gate itself*, which is
    the worst place for it: every scoring fix is only as trustworthy as the
    harness, and the harness was asserting in English.

    **Why an order relation over tags, and not ``expected_rank_band``.**
    Enforcing the per-fixture bands wholesale goes red immediately on r18
    (tagged ``strong``, band ``{1,9}``, actual rank 11), which is its own
    unreconciled discrepancy and not this gate's business. The order relation
    needs no new fixtures and no measured constants — it is a pure comparison
    between two tag groups, so it cannot drift with the corpus the way a pinned
    rank does. That is also why the round-6 note says "do not re-pin a rank
    here": r09 sits in a near-tie with r04 (2.8e-04 apart) and the two swap
    between builds. Their *relation to the strong tier* is stable; their exact
    ranks are not.

    Deliberately scoped to ``adversarial``, not to ``weak`` as well. The
    measured claim covers the bait only; extending it to the five weak fixtures
    is unmeasured, and a gate that goes red on arrival teaches people to
    disable gates.
    """
    strong_ranks = {
        m.resume_id: m.rank for m in ranked if tag_by_id.get(m.resume_id) == "strong"
    }
    bait = [m for m in ranked if tag_by_id.get(m.resume_id) == "adversarial"]

    # Guard against a vacuous pass: with no bait or no strong tier this check
    # is trivially true and would sit here proving nothing, which is the exact
    # failure mode it exists to correct.
    assert bait, (
        "no 'adversarial'-tagged fixture in the corpus — this gate would pass "
        "vacuously; re-scope it or restore the bait fixture"
    )
    assert strong_ranks, (
        "no 'strong'-tagged fixtures in the corpus — this gate would pass " "vacuously"
    )

    worst_strong_rank = max(strong_ranks.values())
    for m in bait:
        assert m.rank > worst_strong_rank, (
            f"{m.resume_id} is tagged 'adversarial' but ranks {m.rank}, at or "
            f"above the worst 'strong' fixture (rank {worst_strong_rank}). The "
            f"keyword-stuffer must sit below EVERY strong fixture — "
            f"thresholds.toml's [adversarial] block asserts this with ~0.19 of "
            f"measured margin, and a ranker that violates it is surfacing bare "
            f"keyword overlap over real evidence. Strong ranks: "
            f"{sorted(strong_ranks.values())}"
        )


def _assert_must_have_penalty_fires_on_r18(corpus: Corpus, weights: Any) -> None:
    """Single-candidate review obligation (README_4c_twins.md §4): r18's
    score_final at must_have_miss_penalty=0.5 must be measurably lower than at
    =1.0 — the mutation the pairwise ordering mechanism provably CANNOT gate."""
    from src.pipeline.matching.orchestrator import RankInput, run_match
    from src.schemas.matching import MatchWeights

    jd = _jd_view(corpus)
    r18 = next(
        r for r in corpus.resumes if r.resume_id == "r18_casey_rivera_missing_must_have"
    )

    def _final(penalty: float) -> float:
        # relief must be >= penalty for the MatchWeights validator; r18 is not
        # senior (6 < 5*1.5) so relief never fires and its value is inert here.
        w = MatchWeights(
            must_have_miss_penalty=penalty,
            implied_experience_relief=max(penalty, 0.75),
        )
        structured, breakdown = _breakdown_for(r18.parsed, jd, 0.5, w)
        evidence, _ = _extract_evidence(r18.parsed, jd)
        ranked = run_match(
            [
                RankInput(
                    resume_id="r18",
                    structured=structured,
                    breakdown=breakdown,
                    evidence=evidence,
                )
            ],
            w,
        )
        return ranked[0].score_final

    lo = _final(0.5)
    hi = _final(1.0)
    assert lo < hi - 0.02, (
        f"must_have_miss_penalty is not wired: score_final(0.5)={lo:.4f} is not "
        f"measurably below score_final(1.0)={hi:.4f}"
    )

    # ── ROADMAP A3: the same check against the SHIPPED value ────────────────
    #
    # Everything above proves the MECHANISM exists. It proved nothing about the
    # value production actually uses, because it builds its own MatchWeights
    # with hardcoded 0.5 and 1.0 and never reads the shipped one. MEASURED:
    # setting `DEFAULT_WEIGHTS.must_have_miss_penalty = 1.0` — switching the
    # must-have penalty OFF entirely — left the whole corpus GREEN, this
    # assertion included.
    #
    # That is the same shape as ADR-035's finding: a control that is real and
    # correct, checked in a way that cannot see whether it is in force. An
    # invariant needs enforcement; a mechanism needs a check that it is
    # actually applied where it counts.
    shipped = weights.must_have_miss_penalty
    assert shipped < 1.0, (
        f"the SHIPPED must_have_miss_penalty is {shipped}, which is no penalty "
        "at all — a candidate missing a required skill scores the same as one "
        "who has it. The mechanism check above still passes, because it builds "
        "its own weights; this line reads the value production uses."
    )
    shipped_final = _final(shipped)
    assert shipped_final < hi - 0.02, (
        f"at the shipped must_have_miss_penalty={shipped}, r18's "
        f"score_final={shipped_final:.4f} is not measurably below the "
        f"no-penalty score {hi:.4f} — the penalty is configured but inert"
    )


def _assert_education_field_relevance(corpus: Corpus, weights: Any) -> None:
    """ADR-028 / ADR-009 §7 field-of-study relevance control (ranking-evals).

    Falsifiable guard for score_education's field-of-study cap: a candidate who
    MEETS the JD's degree-LEVEL bar but whose qualifying degree is in a
    NON-allowed field is capped at ``weights.education_partial`` instead of 1.0
    (fuzzy match via ``token_set_ratio >= weights.education_field_fuzz``).

    Without this the new behaviour is UNGATED by the corpus: the only demoted
    fixtures (r06/r12/r17) are already weak/must_not_surface and sit at the
    bottom of the ranking, so making the cap inert -- revert the scorer to
    level-only, set ``education_field_fuzz=0.0`` so every field "matches", or
    empty ``jd.education.fields`` -- leaves precision@5, the adversarial
    backstop and every ordering pair GREEN. This closes the round-5 F2 "open
    decision" labels.json documents (score_education ignoring jd.education.fields).

    For each labels.json ``education_field_relevance.field_demoted`` fixture,
    under the REAL engine:
      * level-only (``jd_fields=()``) == 1.0 -- it genuinely CLEARS the level
        bar, so the demotion isolates the FIELD cap, not a below-level partial
        (this is what distinguishes it from r11's 0.333 associate case);
      * fields-on == ``weights.education_partial`` -- the cap fires (kills a
        scorer reverted to level-only);
      * ``education_field_fuzz=0.0`` == 1.0 -- the fuzzy-match knob is
        load-bearing (kills a match-everything mutation).
    Same class of single-fixture obligation as
    ``_assert_must_have_penalty_fires_on_r18``; enforced unconditionally in
    ``_run_corpus`` rather than via a new thresholds.toml key.
    """
    from src.pipeline.matching.orchestrator import _level_from_degree
    from src.pipeline.matching.stages import score_education
    from src.schemas.matching import MatchWeights

    labels = _labels()
    block = labels.get("education_field_relevance") or {}
    demoted = list(block.get("field_demoted") or [])
    assert demoted, (
        "labels.json education_field_relevance.field_demoted is empty -- the "
        "ADR-028 field-relevance control has no fixture to prove"
    )
    jd = _jd_view(corpus)
    assert jd["edu_fields"], (
        "the JD lists no education.fields, so the field-relevance control is "
        "vacuous -- did the JD fixture lose its allowed-fields list?"
    )
    parsed_by_id = {r.resume_id: r.parsed for r in corpus.resumes}
    partial = weights.education_partial
    for rid in demoted:
        assert (
            rid in parsed_by_id
        ), f"education_field_relevance names unknown fixture {rid!r}"
        edu_entries = parsed_by_id[rid].get("education", []) or []
        levels = [_level_from_degree(e.get("degree")) for e in edu_entries]
        fields = [e.get("field") for e in edu_entries]
        level_only = score_education(
            levels,
            jd["edu_min_level"],
            candidate_fields=fields,
            jd_fields=(),
            weights=weights,
        )
        real = score_education(
            levels,
            jd["edu_min_level"],
            candidate_fields=fields,
            jd_fields=jd["edu_fields"],
            weights=weights,
        )
        inert = score_education(
            levels,
            jd["edu_min_level"],
            candidate_fields=fields,
            jd_fields=jd["edu_fields"],
            weights=MatchWeights(education_field_fuzz=0.0),
        )
        assert level_only == 1.0, (
            f"{rid}: an education_field_relevance fixture must CLEAR the level "
            f"bar (level-only score == 1.0) so its demotion isolates the FIELD "
            f"cap; got {level_only}"
        )
        assert real == partial, (
            f"{rid}: score_education did NOT cap a non-allowed-field degree at "
            f"education_partial ({partial}); got {real}. The ADR-028 field cap "
            "is inert -- do NOT relax this control, fix the scorer."
        )
        assert inert == 1.0, (
            f"{rid}: with education_field_fuzz=0.0 (every field matches) the "
            f"cap must lift to 1.0; got {inert}. The fuzzy-match knob is not "
            "load-bearing."
        )
        assert real < level_only, (
            f"{rid}: field cap did not lower the education sub-score "
            f"(real={real} !< level_only={level_only})"
        )


def _assert_no_pii_leak(
    corpus: Corpus, scored: dict[str, _Scored], topk_ids: set[str]
) -> None:
    """embedding_input_pii_free (every fixture) + exported_output_pii_free
    (top-k evidence). Uses the SAME redaction the embed boundary uses; the r12/
    r17 self-dox controls exercise it."""
    from src.schemas.resumes import ResumeParsed
    from src.worker.resume_tasks import _build_summary_text, _redact_candidate_pii

    for r in corpus.resumes:
        rp = ResumeParsed.model_validate(r.parsed)
        cand = rp.candidate
        markers = _pii_markers(r.parsed.get("candidate", {}))
        # embedding input: redacted summary + every redacted chunk.
        embed_inputs = [_redact_candidate_pii(_build_summary_text(rp), cand)]
        embed_inputs += [
            _redact_candidate_pii(c.get("text", ""), cand)
            for c in r.parsed.get("chunks", [])
        ]
        for text in embed_inputs:
            low = text.lower()
            for marker in markers:
                assert (
                    marker not in low
                ), f"{r.resume_id}: PII marker {marker!r} leaked into embedding input"
    # exported output: the surfaced evidence quotes of the top-k entries.
    for rid in topk_ids:
        markers = _pii_markers(
            next(r.parsed for r in corpus.resumes if r.resume_id == rid).get(
                "candidate", {}
            )
        )
        for req in scored[rid].evidence.requirements if scored[rid].evidence else []:
            low = (req.evidence or "").lower()
            for marker in markers:
                assert (
                    marker not in low
                ), f"{rid}: PII marker {marker!r} leaked into exported evidence"


def _pii_markers(candidate: dict[str, Any]) -> list[str]:
    markers: list[str] = []
    name = (candidate.get("name") or "").strip().lower()
    if name:
        markers.append(name)
    email = (candidate.get("email") or "").strip().lower()
    if email:
        markers.append(email)
    phone = (candidate.get("phone") or "").strip()
    if phone:
        digits = "".join(ch for ch in phone if ch.isdigit())
        if len(digits) >= 7:
            markers.append(digits)
    return markers


def _assert_deterministic(corpus: Corpus, weights: Any, first: list[Any]) -> None:
    """thresholds.toml [determinism]: max_rank_delta = 0 (order identical) and
    max_score_delta = 1e-9. Deterministic embedder + pinned today_year -> the
    second run is bit-identical to the first (cache state: cold / none)."""
    from src.pipeline.matching.orchestrator import RankInput, run_match

    scored = _score_corpus(corpus, weights)
    second = run_match(
        [
            RankInput(
                resume_id=rid,
                structured=s.structured,
                breakdown=s.breakdown,
                evidence=s.evidence,
            )
            for rid, s in scored.items()
        ],
        weights,
    )
    order1 = [m.resume_id for m in sorted(first, key=lambda m: m.rank)]
    order2 = [m.resume_id for m in sorted(second, key=lambda m: m.rank)]
    assert order1 == order2, "determinism: ranking order changed between two runs"
    s1 = {m.resume_id: m.score_final for m in first}
    for m in second:
        assert (
            abs(m.score_final - s1[m.resume_id]) <= 1e-9
        ), f"determinism: score_final for {m.resume_id} moved > 1e-9"


def main() -> int:
    if _ORCHESTRATOR_IMPORT_ERROR is not None:
        print(_NOT_IMPLEMENTED_MSG, file=sys.stderr)
        return 1

    thresholds = load_thresholds()
    corpus = load_corpus()
    _run_corpus(corpus, thresholds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
