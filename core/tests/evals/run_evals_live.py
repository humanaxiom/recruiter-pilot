"""Phase-7 LIVE end-to-end ranking eval harness (closes ADR-013 §5).

Where ``run_evals.py`` proves the 4a/4c corpus against ``thresholds.toml`` with an
OFFLINE deterministic stand-in (a bag-of-words ``_embed`` + no Neo4j/Ollama/
Postgres), THIS harness drives the SAME corpus through the REAL production
pipeline: real ``nomic-embed-text`` (768-d) embeddings, real Neo4j skill graph,
the real ``shortlist_job`` orchestrator, and real Postgres persistence — then
re-checks every ``thresholds.toml`` gate against the PERSISTED ``shortlist_entries``
rows.

Faithfulness (critical): the corpus is PRE-PARSED by design — the fixtures are
already ``JDExtracted`` / ``ResumeParsed`` objects. Re-running the LLM parse would
drift from the calibration the thresholds were tuned against, for reasons that
have nothing to do with RANKING. So this harness seeds the pre-parsed corpus at
the POST-PARSE boundary (the exact shape ``parse_job`` / ``parse_resume`` leave
behind: a ``jobs``/``resumes`` row + the ``job.parsed`` / ``resume.parsed`` outbox
event carrying REAL embeddings computed through the production embed boundary,
PII redaction included) and drives everything downstream for real:

  seed Postgres + outbox  ->  project_to_graph (real Neo4j + nomic skill graph)
                          ->  shortlist_job (real stage-1..4 orchestrator)
                          ->  read shortlist_entries  ->  evaluate vs thresholds

``stages.py`` / ``orchestrator.py`` / ``thresholds.toml`` / ``fixtures/`` /
``labels.json`` are reused byte-identical; nothing here modifies them.

GATE-ABILITY: the PURE evaluation logic (``eval_*`` below — compute every metric
from a set of persisted-row dicts vs labels/thresholds) is offline and is
unit-tested in ``core/tests/unit/test_evals_live_metrics.py`` with SYNTHETIC rows
(including bad rankings that must FAIL). This module lives under ``tests/evals``
(NOT ``tests/unit``) so ``pytest tests/unit`` never collects it, and the live
orchestration (``run_live`` and the ``_seed`` / ``_drain`` / DB helpers) only
executes when the file is run as a script against a live stack. CI stays green
with no Ollama.

Run it (inside the api or worker container, which mount ``./core`` at ``/app`` and
carry the real settings env)::

    docker compose -f docker-compose.yml -f compose.live-eval.yml \\
        exec -T api python tests/evals/run_evals_live.py

Exit code 0 on a full pass, 1 on any threshold miss.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rapidfuzz import fuzz

# --- path setup: make `core/` (for `src`) and this dir (for `run_evals`) importable
_EVALS_DIR = Path(__file__).resolve().parent
_CORE_ROOT = _EVALS_DIR.parents[1]
for _p in (str(_CORE_ROOT), str(_EVALS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import run_evals  # noqa: E402  (reuse load_corpus / load_thresholds — never duplicate)

# ===========================================================================
# PURE evaluation layer — no DB/Neo4j/Ollama. Every function takes already-read
# persisted-row dicts + labels/thresholds + corpus-derived aux maps and returns a
# MetricResult. This is the offline-gateable half (unit-tested in tests/unit).
# ===========================================================================


@dataclass(frozen=True)
class MetricResult:
    """One threshold's verdict + the actual numbers behind it."""

    name: str
    passed: bool
    detail: str


@dataclass
class PersistedRow:
    """One persisted ``shortlist_entries`` row, keyed by FIXTURE id (not the DB
    UUID) so the pure metrics stay decoupled from the live seeding."""

    resume_id: str
    rank: int
    score_final: float
    breakdown: dict[str, Any]
    evidence: dict[str, Any] | None


@dataclass
class EvalReport:
    metrics: list[MetricResult] = field(default_factory=list)

    @property
    def all_passed(self) -> bool:
        return all(m.passed for m in self.metrics)


def _partial_ratio(needle: str, haystack: str) -> float:
    """Fuzzy substring score in [0, 1] — the SAME measure the real verifier uses
    (``rapidfuzz.fuzz.partial_ratio``, lower-cased), so the re-check here matches
    ``stages.verify_evidence`` exactly."""
    if not needle:
        return 0.0
    return float(fuzz.partial_ratio(needle.lower().strip(), haystack.lower())) / 100.0


def _tag(labels: dict[str, Any], rid: str) -> str:
    return str(labels["resumes"][rid]["tag"])


def _topk(rows: list[PersistedRow], k: int) -> list[PersistedRow]:
    return sorted(rows, key=lambda r: r.rank)[:k]


def _requirements(row: PersistedRow) -> list[dict[str, Any]]:
    ev = row.evidence or {}
    reqs = ev.get("requirements") or []
    return [r for r in reqs if isinstance(r, dict)]


def _cover_evidence(row: PersistedRow) -> list[dict[str, Any]]:
    ev = row.evidence or {}
    cov = ev.get("cover_letter_evidence") or []
    return [c for c in cov if isinstance(c, dict)]


def eval_precision_at_k(
    rows: list[PersistedRow], labels: dict[str, Any], thresholds: dict[str, Any]
) -> MetricResult:
    k = int(thresholds["precision_at_k"]["k"])
    min_p = float(thresholds["precision_at_k"]["min_precision"])
    topk = _topk(rows, k)
    good = sum(1 for r in topk if _tag(labels, r.resume_id) in {"strong", "borderline"})
    precision = good / len(topk) if topk else 0.0
    passed = len(topk) == k and precision >= min_p
    listing = [(r.resume_id, _tag(labels, r.resume_id)) for r in topk]
    return MetricResult(
        "precision_at_k",
        passed,
        f"precision@{k}={precision:.3f} (min {min_p}); top-k={listing}",
    )


def eval_must_not_surface(
    rows: list[PersistedRow], labels: dict[str, Any], thresholds: dict[str, Any]
) -> MetricResult:
    k = int(thresholds["precision_at_k"]["k"])
    topk_ids = {r.resume_id for r in _topk(rows, k)}
    offenders = sorted(
        rid
        for rid, entry in labels["resumes"].items()
        if entry.get("must_not_surface_in_topk") and rid in topk_ids
    )
    return MetricResult(
        "adversarial.must_not_surface_in_topk",
        not offenders,
        f"top-k={sorted(topk_ids)}; offenders_in_topk={offenders}",
    )


def eval_verification_rate(
    rows: list[PersistedRow],
    thresholds: dict[str, Any],
    chunks_by_resume: dict[str, dict[str, str]],
) -> MetricResult:
    """Every SURFACED quote (a persisted requirement/cover evidence with a quote
    AND a citation) must fuzzy-match its cited chunk >= fuzz_threshold."""
    fuzz_t = float(thresholds["evidence"]["fuzz_threshold"])
    min_rate = float(thresholds["evidence"]["verification_rate_min"])
    surfaced = 0
    verified = 0
    unverified: list[str] = []
    for row in rows:
        chunks = chunks_by_resume.get(row.resume_id, {})
        for item in _requirements(row) + _cover_evidence(row):
            quote = str(item.get("evidence") or "")
            cids = [c for c in (item.get("evidence_chunk_ids") or [])]
            if not quote or not cids:
                continue
            surfaced += 1
            if any(_partial_ratio(quote, chunks.get(c, "")) >= fuzz_t for c in cids):
                verified += 1
            else:
                unverified.append(f"{row.resume_id}:{quote[:48]!r}")
    rate = verified / surfaced if surfaced else 1.0
    return MetricResult(
        "evidence.verification_rate",
        rate >= min_rate,
        f"verified {verified}/{surfaced} surfaced quotes = {rate:.3f} "
        f"(min {min_rate}); unverified={unverified[:5]}",
    )


def eval_min_completeness_in_topk(
    rows: list[PersistedRow], thresholds: dict[str, Any]
) -> MetricResult:
    k = int(thresholds["precision_at_k"]["k"])
    floor = float(thresholds["evidence"]["min_completeness_in_topk"])
    topk = _topk(rows, k)
    complete = sum(
        1
        for r in topk
        if any(
            req.get("status") == "met" and req.get("evidence")
            for req in _requirements(r)
        )
    )
    frac = complete / len(topk) if topk else 0.0
    return MetricResult(
        "evidence.min_completeness_in_topk",
        frac >= floor,
        f"{complete}/{len(topk)} top-k entries carry >=1 verified quote = "
        f"{frac:.3f} (min {floor})",
    )


def eval_gold_recall(
    rows: list[PersistedRow],
    labels: dict[str, Any],
    thresholds: dict[str, Any],
    chunks_by_resume: dict[str, dict[str, str]],
) -> MetricResult:
    fuzz_t = float(thresholds["evidence"]["fuzz_threshold"])
    floor = float(thresholds["evidence"]["gold_recall_min"])
    rows_by_id = {r.resume_id: r for r in rows}
    total = 0
    recovered = 0
    misses: list[str] = []
    for rid, entry in labels["resumes"].items():
        gold = entry.get("gold_evidence") or {}
        if not gold:
            continue
        row = rows_by_id.get(rid)
        chunks = chunks_by_resume.get(rid, {})
        by_req: dict[str, dict[str, Any]] = {}
        if row is not None:
            for req in _requirements(row):
                by_req[str(req.get("requirement") or "").strip().lower()] = req
        for skill_name in gold:
            total += 1
            req = by_req.get(skill_name.strip().lower())
            quote = str(req.get("evidence") or "") if req else ""
            cids = list(req.get("evidence_chunk_ids") or []) if req else []
            ok = bool(
                req is not None
                and req.get("status") == "met"
                and quote
                and any(
                    _partial_ratio(quote, chunks.get(c, "")) >= fuzz_t for c in cids
                )
            )
            if ok:
                recovered += 1
            else:
                misses.append(f"{rid}:{skill_name}")
    recall = recovered / total if total else 0.0
    return MetricResult(
        "evidence.gold_recall",
        total > 0 and recall >= floor,
        f"recovered {recovered}/{total} gold anchors = {recall:.3f} "
        f"(min {floor}); misses={misses}",
    )


def eval_negative_evidence(
    labels: dict[str, Any],
    chunks_by_resume: dict[str, dict[str, str]],
    weights: Any,
) -> MetricResult:
    """Anti-fabrication falsifier: run the REAL ``stages.verify_evidence`` (the
    same verifier the pipeline uses) on every ``negative_evidence`` fabrication
    and assert it is BLANKED. Pure/offline — needs no live stack, but exercises
    the production verifier at its real 0.85 fuzz bar."""
    from src.pipeline.matching.stages import verify_evidence
    from src.schemas.matching import EvidenceObject, RequirementEvidence

    ran = 0
    leaks: list[str] = []
    for rid, entry in labels["resumes"].items():
        neg = entry.get("negative_evidence") or {}
        chunks = chunks_by_resume.get(rid, {})
        for chunk_id, fabricated in neg.items():
            ran += 1
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
            cleaned = verify_evidence(ev, chunks, weights=weights).requirements[0]
            if not (cleaned.evidence == "" and cleaned.status == "missing"):
                leaks.append(f"{rid}:{chunk_id}")
    passed = ran == 4 and not leaks
    return MetricResult(
        "evidence.negative_evidence_must_fail",
        passed,
        f"ran {ran} fabrications (expected 4); un-scrubbed={leaks}",
    )


def eval_ordering_controls(
    rows: list[PersistedRow], thresholds: dict[str, Any]
) -> MetricResult:
    oc = thresholds["ordering_controls"]
    if not oc.get("enforce"):
        return MetricResult("ordering_controls", True, "enforce=false")
    min_gap = float(oc["min_score_gap"])
    by_id = {r.resume_id: r for r in rows}
    lines: list[str] = []
    failures: list[str] = []
    for pair in oc["pairs"]:
        dim = pair["dimension"]
        hi, lo = pair["higher_id"], pair["lower_id"]
        h, low = by_id.get(hi), by_id.get(lo)
        if h is None or low is None:
            failures.append(f"{dim}: missing persisted row (hi={h}, lo={low})")
            continue
        gap = h.score_final - low.score_final
        ok = h.rank < low.rank and gap >= min_gap
        lines.append(
            f"{dim}: rank {h.rank}<{low.rank} gap={gap:+.6e} {'OK' if ok else 'FAIL'}"
        )
        if not ok:
            failures.append(
                f"{dim}: rank({hi})={h.rank} rank({lo})={low.rank} "
                f"gap={gap:+.6e} (min {min_gap:.1e})"
            )
    return MetricResult(
        "ordering_controls",
        not failures,
        "; ".join(lines) + (f" | FAILURES={failures}" if failures else ""),
    )


def _pii_markers(candidate: dict[str, Any]) -> list[str]:
    markers: list[str] = []
    name = str(candidate.get("name") or "").strip().lower()
    if name:
        markers.append(name)
    email = str(candidate.get("email") or "").strip().lower()
    if email:
        markers.append(email)
    phone = str(candidate.get("phone") or "").strip()
    if phone:
        digits = "".join(ch for ch in phone if ch.isdigit())
        if len(digits) >= 7:
            markers.append(digits)
    return markers


def eval_pii_leak_in_output(
    rows: list[PersistedRow],
    thresholds: dict[str, Any],
    candidate_by_resume: dict[str, dict[str, Any]],
) -> MetricResult:
    """exported_output_pii_free: no top-k entry's surfaced evidence text may
    carry that candidate's own name/email/phone."""
    k = int(thresholds["precision_at_k"]["k"])
    leaks: list[str] = []
    for row in _topk(rows, k):
        markers = _pii_markers(candidate_by_resume.get(row.resume_id, {}))
        ev = row.evidence or {}
        texts = [str(item.get("evidence") or "") for item in _requirements(row)]
        texts += [str(item.get("evidence") or "") for item in _cover_evidence(row)]
        texts += [
            str(ev.get("overall_summary") or ""),
            str(ev.get("overall_motivation") or ""),
        ]
        for text in texts:
            low = text.lower()
            for marker in markers:
                if marker in low:
                    leaks.append(f"{row.resume_id}:{marker!r}")
    return MetricResult(
        "pii.exported_output_pii_free",
        not leaks,
        f"scanned top-{k} evidence; leaks={leaks}",
    )


def eval_embedding_input_pii_free(
    corpus: Any,
) -> MetricResult:
    """embedding_input_pii_free (every fixture): reproduce the production embed
    boundary redaction and confirm no candidate identity survives into any text
    that gets embedded. This is the load-bearing privacy invariant; it is pure
    (the redaction functions are pure) so it is offline-testable too."""
    from src.schemas.resumes import ResumeParsed
    from src.worker.resume_tasks import _build_summary_text, _redact_candidate_pii

    leaks: list[str] = []
    for rf in corpus.resumes:
        parsed = ResumeParsed.model_validate(rf.parsed)
        cand = parsed.candidate
        markers = _pii_markers(rf.parsed.get("candidate", {}))
        embed_inputs = [_redact_candidate_pii(_build_summary_text(parsed), cand)]
        embed_inputs += [
            _redact_candidate_pii(c.get("text", ""), cand)
            for c in rf.parsed.get("chunks", [])
        ]
        for text in embed_inputs:
            low = text.lower()
            for marker in markers:
                if marker in low:
                    leaks.append(f"{rf.resume_id}:{marker!r}")
    return MetricResult(
        "pii.embedding_input_pii_free",
        not leaks,
        f"scanned {len(corpus.resumes)} fixtures' embed inputs; leaks={leaks}",
    )


def eval_determinism(
    rows_a: list[PersistedRow],
    rows_b: list[PersistedRow],
    thresholds: dict[str, Any],
) -> MetricResult:
    """Two-run stability (thresholds [determinism]): order identical
    (max_rank_delta=0) and score_final within max_score_delta. NOTE: with a warm
    Redis embedding cache the SECOND run reuses cached vectors, so this compares a
    re-run to itself for the embed half; stage-3 (gpt-oss greedy decode) is not
    guaranteed bit-stable, so a small score drift here is a real observation, not
    a harness bug."""
    max_rank_delta = int(thresholds["determinism"]["max_rank_delta"])
    max_score_delta = float(thresholds["determinism"]["max_score_delta"])
    a = {r.resume_id: r for r in rows_a}
    b = {r.resume_id: r for r in rows_b}
    order_a = [r.resume_id for r in sorted(rows_a, key=lambda r: r.rank)]
    order_b = [r.resume_id for r in sorted(rows_b, key=lambda r: r.rank)]
    worst_rank = 0
    for rid in a:
        if rid in b:
            worst_rank = max(worst_rank, abs(a[rid].rank - b[rid].rank))
    worst_score = 0.0
    for rid in a:
        if rid in b:
            worst_score = max(worst_score, abs(a[rid].score_final - b[rid].score_final))
    passed = (
        order_a == order_b
        and worst_rank <= max_rank_delta
        and worst_score <= max_score_delta
    )
    return MetricResult(
        "determinism",
        passed,
        f"order_identical={order_a == order_b}; max_rank_delta={worst_rank} "
        f"(<= {max_rank_delta}); max_score_delta={worst_score:.3e} "
        f"(<= {max_score_delta:.1e})",
    )


def chunks_by_resume(corpus: Any) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for rf in corpus.resumes:
        mapping: dict[str, str] = {}
        for c in rf.parsed.get("chunks", []) or []:
            if c.get("id"):
                mapping[c["id"]] = c.get("text", "")
        for c in rf.parsed.get("cover_letter_chunks", []) or []:
            if c.get("id"):
                mapping[c["id"]] = c.get("text", "")
        out[rf.resume_id] = mapping
    return out


def candidate_by_resume(corpus: Any) -> dict[str, dict[str, Any]]:
    return {rf.resume_id: (rf.parsed.get("candidate") or {}) for rf in corpus.resumes}


def evaluate(
    rows: list[PersistedRow],
    corpus: Any,
    labels: dict[str, Any],
    thresholds: dict[str, Any],
    weights: Any,
    *,
    rows_second: list[PersistedRow] | None = None,
) -> EvalReport:
    """Run every thresholds.toml gate against the persisted rows. PURE — the only
    inputs are already-read dicts + the corpus fixtures."""
    cbr = chunks_by_resume(corpus)
    candmap = candidate_by_resume(corpus)
    metrics = [
        eval_precision_at_k(rows, labels, thresholds),
        eval_must_not_surface(rows, labels, thresholds),
        eval_verification_rate(rows, thresholds, cbr),
        eval_min_completeness_in_topk(rows, thresholds),
        eval_gold_recall(rows, labels, thresholds, cbr),
        eval_negative_evidence(labels, cbr, weights),
        eval_ordering_controls(rows, thresholds),
        eval_embedding_input_pii_free(corpus),
        eval_pii_leak_in_output(rows, thresholds, candmap),
    ]
    if rows_second is not None:
        metrics.append(eval_determinism(rows, rows_second, thresholds))
    return EvalReport(metrics=metrics)


def format_report(
    report: EvalReport,
    *,
    rows: list[PersistedRow],
    labels: dict[str, Any],
    header: str,
) -> str:
    lines: list[str] = []
    lines.append("=" * 78)
    lines.append(header)
    lines.append("=" * 78)
    lines.append("")
    lines.append("Persisted ranking (rank  score_final  tag  resume_id):")
    for r in sorted(rows, key=lambda r: r.rank):
        tag = _tag(labels, r.resume_id) if r.resume_id in labels["resumes"] else "?"
        lines.append(f"  {r.rank:>3}  {r.score_final:.6f}  {tag:<11}  {r.resume_id}")
    lines.append("")
    lines.append("Threshold metrics:")
    width = max((len(m.name) for m in report.metrics), default=0)
    for m in report.metrics:
        status = "PASS" if m.passed else "FAIL"
        lines.append(f"  [{status}] {m.name.ljust(width)}  {m.detail}")
    lines.append("")
    verdict = "ALL THRESHOLDS PASS" if report.all_passed else "THRESHOLD MISS(ES)"
    lines.append(f"VERDICT: {verdict}  (exit {0 if report.all_passed else 1})")
    lines.append("=" * 78)
    return "\n".join(lines)


# ===========================================================================
# LIVE orchestration — Postgres + Neo4j + Ollama. Runs ONLY when this file is
# executed as a script (main()); never imported/collected by pytest tests/unit.
# ===========================================================================


async def _seed_corpus(
    ctx: dict[str, Any], corpus: Any, job_id: uuid.UUID
) -> dict[str, str]:
    """Seed the pre-parsed corpus at the post-parse boundary: one ``jobs`` row +
    20 ``resumes`` rows (PII encrypted via the real pii path) + the ``job.parsed``
    / ``resume.parsed`` outbox rows carrying REAL embeddings (computed through the
    production embed boundary, PII-redacted). Returns a DB-UUID -> fixture-id map."""
    import hashlib as _hashlib

    from src.pipeline.parsing import MIME_TXT
    from src.pipeline.skills import build_summary_text
    from src.schemas.jobs import JDExtracted
    from src.schemas.resumes import ResumeParsed
    from src.services import outbox_service, resume_service
    from src.worker.resume_tasks import (
        _OUTBOX_PARSED_EXCLUDE,
        _build_summary_text,
        _embed_batched,
        _is_header_like_chunk,
        _redact_candidate_pii,
        _redaction_available,
    )

    pool = ctx["pg_pool"]
    embedder = ctx["embedder"]

    jd = JDExtracted.model_validate(corpus.job_extracted)
    [job_emb] = await embedder.embed([build_summary_text(jd)])

    async with pool.acquire() as conn:
        async with conn.transaction():
            # DELETE-first idempotency (a fresh UUID would not collide, but this
            # keeps a manual re-run with a fixed id clean too).
            await conn.execute(
                "DELETE FROM shortlist_entries WHERE job_id = $1", job_id
            )
            await conn.execute("DELETE FROM resumes WHERE job_id = $1", job_id)
            await conn.execute("DELETE FROM jobs WHERE id = $1", job_id)
            await conn.execute(
                "INSERT INTO jobs (id, title, description_raw, description_parsed, "
                "min_years, status) VALUES ($1, $2, $3, $4::jsonb, $5, 'open')",
                job_id,
                jd.title,
                "(seeded by run_evals_live — post-parse boundary)",
                json.dumps(corpus.job_extracted),
                jd.min_years_experience,
            )
            await outbox_service.enqueue_outbox(
                conn,
                aggregate="job",
                aggregate_id=job_id,
                event_type="job.parsed",
                payload={
                    "embedding": job_emb,
                    "extracted": corpus.job_extracted,
                    "prompt_version": "jd_extract_v1",
                },
            )

    id_map: dict[str, str] = {}
    for rf in corpus.resumes:
        resume_id = uuid.uuid4()
        id_map[str(resume_id)] = rf.resume_id
        parsed = ResumeParsed.model_validate(rf.parsed)
        candidate = parsed.candidate

        # Real embed boundary — identical to parse_resume (redaction + header skip).
        summary_text = _redact_candidate_pii(_build_summary_text(parsed), candidate)
        [summary_emb] = await embedder.embed([summary_text])
        if _redaction_available(candidate):
            chunks_for_emb = list(parsed.chunks)
        else:
            chunks_for_emb = [
                c
                for i, c in enumerate(parsed.chunks)
                if not _is_header_like_chunk(i, c)
            ]
        chunk_emb_list = await _embed_batched(
            embedder, [_redact_candidate_pii(c.text, candidate) for c in chunks_for_emb]
        )
        chunk_embs = {
            chunks_for_emb[i].id: chunk_emb_list[i] for i in range(len(chunks_for_emb))
        }
        parsed_payload = parsed.model_dump(exclude=_OUTBOX_PARSED_EXCLUDE)
        sha = _hashlib.sha256(rf.resume_id.encode("utf-8")).hexdigest()

        async with pool.acquire() as conn:
            async with conn.transaction():
                name_enc, email_enc, phone_enc, email_hash = (
                    await resume_service.encrypt_pii_via_session(conn, candidate)
                )
                await conn.execute(
                    "INSERT INTO resumes (id, job_id, blob_key, original_filename, "
                    "mime_type, file_size_bytes, sha256, candidate_name, "
                    "candidate_email, candidate_phone, candidate_email_hash, parsed, "
                    "status, consent_acknowledged, parsed_at) VALUES "
                    "($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12::jsonb,'parsed',true,$13)",
                    resume_id,
                    job_id,
                    f"resumes/{resume_id}.txt",
                    f"{rf.resume_id}.txt",
                    MIME_TXT,
                    len(json.dumps(rf.parsed)),
                    sha,
                    name_enc,
                    email_enc,
                    phone_enc,
                    email_hash,
                    json.dumps(parsed.model_dump()),
                    dt.datetime.now(dt.UTC),
                )
                await outbox_service.enqueue_outbox(
                    conn,
                    aggregate="resume",
                    aggregate_id=resume_id,
                    event_type="resume.parsed",
                    payload={
                        "parsed": parsed_payload,
                        "summary_emb": summary_emb,
                        "chunk_embs": chunk_embs,
                        "prompt_version": "resume_core_v1+resume_skills_v2",
                        "job_id": str(job_id),
                    },
                )
    return id_map


async def _neo4j_counts(ctx: dict[str, Any], job_id: uuid.UUID) -> dict[str, int]:
    driver = ctx["neo4j"]
    counts: dict[str, int] = {}
    async with driver.session() as session:
        for label in ("Job", "Resume", "Skill", "ResumeChunk"):
            res = await session.run(f"MATCH (n:{label}) RETURN count(n) AS c")
            rec = await res.single()
            counts[label] = int(rec["c"]) if rec else 0
        res = await session.run(
            "MATCH (r:Resume {job_id: $jid}) RETURN count(r) AS c", jid=str(job_id)
        )
        rec = await res.single()
        counts["ResumeForJob"] = int(rec["c"]) if rec else 0
    return counts


async def _drain_and_verify(
    ctx: dict[str, Any], job_id: uuid.UUID, expected_resumes: int
) -> dict[str, int]:
    """Drain the outbox into Neo4j via the REAL production drainer, polling until
    the graph is fully projected (the live worker's cron drainer may also help —
    FOR UPDATE SKIP LOCKED makes that safe). Fails loud if skills never land."""
    from src.worker.graph_tasks import project_to_graph

    deadline = asyncio.get_event_loop().time() + 180.0
    counts: dict[str, int] = {}
    while asyncio.get_event_loop().time() < deadline:
        await project_to_graph(ctx, batch=100)
        counts = await _neo4j_counts(ctx, job_id)
        if (
            counts["ResumeForJob"] >= expected_resumes
            and counts["Job"] >= 1
            and counts["Skill"] > 0
        ):
            return counts
        await asyncio.sleep(2.0)
    raise RuntimeError(
        f"graph projection incomplete after 180s: {counts} "
        f"(expected >= {expected_resumes} resumes, >= 1 job, > 0 skills)"
    )


async def _read_persisted(
    ctx: dict[str, Any], job_id: uuid.UUID, id_map: dict[str, str]
) -> list[PersistedRow]:
    """Read shortlist_entries back and fold-pop the ADR-011 §2 breakdown keys."""
    pool = ctx["pg_pool"]
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT resume_id, rank, score_final, score_breakdown, evidence "
            "FROM shortlist_entries WHERE job_id = $1 ORDER BY rank ASC",
            job_id,
        )
    out: list[PersistedRow] = []
    for r in rows:
        sb = r["score_breakdown"]
        sb = json.loads(sb) if isinstance(sb, str) else dict(sb)
        sb.pop("score_structured", None)
        sb.pop("score_evidence", None)
        ev = r["evidence"]
        ev = json.loads(ev) if isinstance(ev, str) else ev
        # persist_shortlist writes the JSON literal {} for None evidence; treat an
        # object with no requirements AND no cover evidence as "no evidence".
        if not ev or (
            not ev.get("requirements") and not ev.get("cover_letter_evidence")
        ):
            ev = None
        out.append(
            PersistedRow(
                resume_id=id_map[str(r["resume_id"])],
                rank=int(r["rank"]),
                score_final=float(r["score_final"]),
                breakdown=sb,
                evidence=ev,
            )
        )
    return out


async def run_live() -> int:
    from src.settings import get_settings, weights_from_settings
    from src.worker.main import shutdown, startup
    from src.worker.matching_tasks import shortlist_job

    thresholds = run_evals.load_thresholds()
    corpus = run_evals.load_corpus()
    labels = run_evals._labels()
    weights = weights_from_settings(get_settings())

    ctx: dict[str, Any] = {}
    await startup(ctx)
    try:
        job_id = uuid.uuid4()
        print(f"[live-eval] seeding corpus for job_id={job_id} ...", flush=True)
        id_map = await _seed_corpus(ctx, corpus, job_id)

        print(
            "[live-eval] projecting outbox -> Neo4j (real nomic skill graph) ...",
            flush=True,
        )
        counts = await _drain_and_verify(ctx, job_id, len(corpus.resumes))
        print(f"[live-eval] neo4j populated: {counts}", flush=True)

        print(
            "[live-eval] running real shortlist_job (stage 1..4) — run 1 ...",
            flush=True,
        )
        status1 = await shortlist_job(ctx, str(job_id))
        rows = await _read_persisted(ctx, job_id, id_map)
        print(f"[live-eval] shortlist_job -> {status1}, {len(rows)} rows", flush=True)

        print(
            "[live-eval] running real shortlist_job — run 2 (determinism) ...",
            flush=True,
        )
        status2 = await shortlist_job(ctx, str(job_id))
        rows2 = await _read_persisted(ctx, job_id, id_map)
        print(f"[live-eval] shortlist_job -> {status2}, {len(rows2)} rows", flush=True)

        report = evaluate(rows, corpus, labels, thresholds, weights, rows_second=rows2)
        print(
            format_report(
                report,
                rows=rows,
                labels=labels,
                header="PHASE-7 LIVE END-TO-END RANKING EVAL (real embeddings + "
                "Neo4j + orchestrator + Postgres)",
            ),
            flush=True,
        )
        return 0 if report.all_passed else 1
    finally:
        await shutdown(ctx)


def main() -> int:
    return asyncio.run(run_live())


if __name__ == "__main__":
    raise SystemExit(main())
