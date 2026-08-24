"""Invariant checks against the REAL deployment — the defect class no test can see.

**The measurement that forced this module.** This repo carries ~5,500 unit and
~540 integration tests at 94% coverage. Nine defects still reached the running
product across two sessions, and the suite could not have caught one of them.
Four lived on the frontend↔API seam that no test crosses (that is the smoke
suite's job). The other four lived in **state**: a graph projected before a fix
shipped, a ``jsonb`` column holding text that is not JSON, a résumé stuck
mid-parse, a container losing a boot race.

That second group is invisible to testing **by construction**. A fixture is
always freshly built, always well-formed, always young — it structurally cannot
have the shape that breaks production. The sharpest instance is ROADMAP A7 (20),
*"the fix that never ran"*: ADR-032 shipped on 2026-08-07 and had **never applied
to a single row** thirteen days later, because every job predated it and the
write only fires during projection. Recruiters saw salted hashes where skill
names belonged, behind a permanently green suite. The count in
:func:`_check_job_skill_labels` would have said so on day one.

So this module does not test code. **It asks the live deployment whether the
invariants the code promises are actually true of the data that is there.**

Three properties make it worth having, and each is pinned by a test:

1. **A check that cannot run REPORTS that it could not run.** Never a silent
   pass. A doctor that says "healthy" because it could not reach Neo4j
   manufactures exactly the confidence it exists to withhold — the ADR-031
   inert-scan lesson, aimed at the one tool that must be immune to it.
2. **Every finding carries a remedy.** A finding without one is another prose
   invariant nobody acts on: the A7 shape, reintroduced by the module built to
   break it.
3. **It never raises.** This is run when something is already wrong; a traceback
   instead of a report is a second outage. Any check that throws becomes a
   ``fail`` finding naming the exception.

Every check below is here because it maps to an incident that actually happened.
None is speculative, and new ones should meet the same bar — a check nobody has
ever needed is a line of output that trains people to skim the report.

Run it with ``scripts/doctor.sh`` (which executes it inside the stack, the way
``scripts/verify.sh`` runs the gates).
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import asyncpg
from neo4j import AsyncGraphDatabase

from src.settings import get_settings

Severity = Literal["fail", "warn", "info"]

#: A résumé that has not reached a terminal parse state within this many hours
#: is stuck, not slow. The remote model takes ~2 minutes for a large PDF, so
#: this is two orders of magnitude of headroom: anything past it is a timed-out
#: parse or a dead worker, and a dead worker looks exactly like a healthy stack
#: that silently never ranks.
_STUCK_HOURS = 6

#: Committed model measurements — see `src.model_probe`. Repo-root relative so
#: the doctor finds them whether it runs from /app or a mounted checkout.
_DEFAULT_PROFILE_DIR = Path(__file__).resolve().parents[2] / "docs" / "model-profiles"


@dataclass(frozen=True)
class Finding:
    """One failed invariant, and what to do about it.

    ``remedy`` is not optional and not decoration. The defect class this module
    exists to catch is precisely "a true statement nobody acted on", so a
    finding that does not name the next action reproduces it.
    """

    check: str
    severity: Severity
    count: int
    detail: str
    remedy: str


# ── Postgres checks ──────────────────────────────────────────────────────

_STALE_LABEL_SQL = """
SELECT count(*) FROM shortlist_entries
WHERE score_breakdown::text LIKE '%"h:%'
"""

_STUCK_SQL = f"""
SELECT count(*) FROM resumes
WHERE status NOT IN ('parsed', 'failed')
  AND withdrawn_at IS NULL
  AND uploaded_at < now() - interval '{_STUCK_HOURS} hours'
"""

# `details` is jsonb, so Postgres itself guarantees the COLUMN parses. What it
# does NOT guarantee is that a jsonb *string* scalar holds JSON — a legacy or
# hand-written row can carry `"not json at all"`, which `json.loads` then raises
# on in the reader. That is A7 (19): one such row used to 500 the whole access
# record. `jsonb_typeof` distinguishes an object (the shape every writer emits)
# from a scalar that slipped in some other way.
_UNDECODABLE_SQL = """
SELECT count(*) FROM audit_log
WHERE details IS NOT NULL AND jsonb_typeof(details) <> 'object'
"""

_UNATTRIBUTABLE_SQL = """
SELECT count(*) FROM audit_log
WHERE actor_kind = 'service' AND actor_service = 'api'
"""

#: Is there anything here worth protecting? An empty dev stack running with auth
#: off is a reasonable default; a stack holding real jobs and résumés is a
#: different claim entirely. A check that fires on a fresh clone is a check
#: people learn to skim past, so this one is conditioned on the data existing.
_HAS_DATA_SQL = "SELECT count(*) FROM jobs"


async def _check_postgres(pg: Any, settings: Any) -> list[Finding]:
    async with pg.acquire() as conn:
        jobs = await conn.fetchval(_HAS_DATA_SQL)
        stale = await conn.fetchval(_STALE_LABEL_SQL)
        stuck = await conn.fetchval(_STUCK_SQL)
        undecodable = await conn.fetchval(_UNDECODABLE_SQL)
        unattributable = await conn.fetchval(_UNATTRIBUTABLE_SQL)

    found: list[Finding] = []
    if jobs and settings is not None and not getattr(settings, "cas_enabled", False):
        found.append(
            Finding(
                check="deploy.auth_disabled",
                severity="fail",
                count=int(jobs),
                detail=(
                    "CAS is DISABLED on a stack holding "
                    f"{jobs} jobs — every page, including the audit-log viewer, "
                    "is reachable with no login"
                ),
                remedy=(
                    "Set CAS_ENABLED=true in .env and recreate the stack. NOTE: "
                    "docker-compose.yml must also NAME CAS_ENABLED in the api "
                    "and frontend environment blocks — a variable in .env "
                    "reaches a container only if the compose file references "
                    "it, and for a long time it did not."
                ),
            )
        )
    if stale:
        found.append(
            Finding(
                check="pg.shortlist_hashed_labels",
                severity="warn",
                count=int(stale),
                detail=(
                    f"{stale} shortlist entries cache a hashed skill label "
                    "(h:...) in score_breakdown"
                ),
                remedy=(
                    "Regenerate the shortlist for the affected jobs. The label is "
                    "rendered at ranking time and cached, so fixing the graph "
                    "alone leaves the screen unchanged."
                ),
            )
        )
    if stuck:
        found.append(
            Finding(
                check="pg.resumes_stuck",
                severity="warn",
                count=int(stuck),
                detail=(
                    f"{stuck} résumés have not reached a terminal parse state in "
                    f"over {_STUCK_HOURS}h"
                ),
                remedy=(
                    "Check the worker is alive (`docker compose ps worker`) and "
                    "that LLM_TIMEOUT exceeds the model's real parse time; then "
                    "re-upload or re-enqueue the affected résumés."
                ),
            )
        )
    if undecodable:
        found.append(
            Finding(
                check="pg.audit_details_decodable",
                severity="fail",
                count=int(undecodable),
                detail=(
                    f"{undecodable} audit_log rows hold a non-object details " "payload"
                ),
                remedy=(
                    "The readers degrade fail-closed (these render as withheld), "
                    "but find the writer that produced them — every current "
                    "record_audit caller emits an object."
                ),
            )
        )
    if unattributable:
        found.append(
            Finding(
                check="pg.unattributable_audit_writes",
                severity="fail",
                count=int(unattributable),
                detail=(
                    f"{unattributable} audit_log rows are attributed to "
                    "actor_service='api' — no human is recorded"
                ),
                remedy=(
                    "This is the ADR-034 signature. D2 = option B removed the "
                    "fallback that produced it, so a row written after "
                    "2026-08-19 means it has been reintroduced: find the caller "
                    "reaching a write without a resolved principal."
                ),
            )
        )
    return found


# ── Neo4j checks ─────────────────────────────────────────────────────────

_UNLABELLED_EDGES = """
MATCH (:Job)-[r:REQUIRES|NICE_TO_HAVE]->(:Skill)
WHERE r.display_name IS NULL
RETURN count(r) AS n
"""

_UNPROJECTED_JOBS = """
MATCH (j:Job)
WHERE NOT (j)-[:REQUIRES]->(:Skill)
RETURN count(j) AS n
"""


async def _check_neo4j(driver: Any) -> list[Finding]:
    async with driver.session() as session:
        unlabelled = (await (await session.run(_UNLABELLED_EDGES)).single()) or {}
        unprojected = (await (await session.run(_UNPROJECTED_JOBS)).single()) or {}

    found: list[Finding] = []
    n = int(unlabelled.get("n") or 0)
    if n:
        found.append(
            Finding(
                check="neo4j.job_skill_labels",
                severity="fail",
                count=n,
                detail=(
                    f"{n} job skill edges carry no display_name, so the shortlist "
                    "renders the opaque canonical_key (h:<hash>) instead of the "
                    "skill name"
                ),
                remedy=(
                    "Re-project the affected jobs so ADR-032's per-job label "
                    "write runs, or backfill display_name from each job's own "
                    "stored extraction. Regenerating the shortlist alone cannot "
                    "fix this — it re-ranks against these same edges."
                ),
            )
        )
    m = int(unprojected.get("n") or 0)
    if m:
        found.append(
            Finding(
                check="neo4j.unprojected_jobs",
                severity="fail",
                count=m,
                detail=f"{m} Job nodes have no REQUIRES edges at all",
                remedy=(
                    "The job was parsed but its projection never completed. "
                    "Check the outbox drainer and re-parse the job; until then "
                    "it can only score on the vector stage."
                ),
            )
        )
    return found


# ── the runner ───────────────────────────────────────────────────────────


async def _check_model_profile(settings: Any, profile_dir: Path) -> list[Finding]:
    """Has the configured model ever been measured against our real prompts?

    Every model-shaped constant in this repo was measured against gpt-oss:20b.
    Pointing the stack at a larger model — which the data-centre move will do —
    invalidates all of them at once, silently. On 2026-08-21 that happened
    WITHIN one model: a budget moved, the timeout did not, and parsing stopped
    entirely behind a green suite. This makes the unmeasured state loud.
    """
    if settings is None:
        return []
    model = str(getattr(settings, "llm_model_generation", "") or "")
    if not model:
        return []
    path = profile_dir / f"{model.replace(':', '-')}.json"
    if not path.is_file():
        return [
            Finding(
                check="deploy.model_unprofiled",
                severity="fail",
                count=0,
                detail=(
                    f"the configured model {model!r} has no acceptance profile — "
                    "nobody has measured whether it can produce schema-valid "
                    "JSON for this product's prompts, or how long it takes"
                ),
                remedy=(
                    "Run scripts/model-check.sh and commit the profile it writes "
                    "to docs/model-profiles/. Every token budget and timeout in "
                    "this repo was measured against gpt-oss:20b and does not "
                    "transfer to another model."
                ),
            )
        ]
    try:
        profile = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [
            Finding(
                check="deploy.model_unprofiled",
                severity="fail",
                count=0,
                detail=f"model profile {path.name} is unreadable: {exc}",
                remedy="Re-run scripts/model-check.sh to regenerate it.",
            )
        ]
    found: list[Finding] = []
    if not profile.get("accepted", False):
        found.append(
            Finding(
                check="deploy.model_unprofiled",
                severity="fail",
                count=0,
                detail=f"model {model!r} has a profile but was NOT accepted",
                remedy=(
                    "At least one real prompt failed to produce schema-valid "
                    "JSON on this model. See the profile's per-prompt results."
                ),
            )
        )
    recommended = int(profile.get("recommended_timeout_s") or 0)
    configured = int(getattr(settings, "llm_timeout_s", 0) or 0)
    if recommended and configured and configured < recommended:
        found.append(
            Finding(
                check="deploy.timeout_below_profile",
                severity="fail",
                count=recommended - configured,
                detail=(
                    f"LLM_TIMEOUT_S is {configured}s but {model!r} was measured "
                    f"to need {recommended}s under this concurrency"
                ),
                remedy=(
                    f"Set LLM_TIMEOUT_S={recommended}. A timeout below the "
                    "measured latency does not fail one call — it trips the "
                    "circuit breaker and stops parsing entirely (2026-08-21)."
                ),
            )
        )
    return found


async def run_checks(
    *,
    pg: Any,
    neo4j: Any,
    settings: Any = None,
    profile_dir: Path | None = None,
) -> list[Finding]:
    """Run every check, returning only what is actually wrong.

    **Each datastore is guarded separately and neither can silence the other.**
    A check that raises becomes a ``fail`` finding naming the exception rather
    than propagating: an operator runs this when something is already broken, so
    the report must survive a broken dependency. "Could not check" is a finding,
    never an absence — this is the one place where degrading quietly would
    defeat the entire purpose.
    """
    findings: list[Finding] = []
    profiles = profile_dir if profile_dir is not None else _DEFAULT_PROFILE_DIR
    checks: list[tuple[str, Any]] = [
        ("pg", lambda: _check_postgres(pg, settings)),
        ("neo4j", lambda: _check_neo4j(neo4j)),
        ("model", lambda: _check_model_profile(settings, profiles)),
    ]
    for name, run in checks:
        try:
            findings.extend(await run())
        except Exception as exc:  # noqa: BLE001 — a report must never crash
            findings.append(
                Finding(
                    check=f"{name}.unreachable",
                    severity="fail",
                    count=0,
                    detail=f"could not check {name}: {exc}",
                    remedy=(
                        f"Bring {name} up and re-run. This is NOT a clean bill of "
                        "health — the invariants it owns are unknown, not "
                        "satisfied."
                    ),
                )
            )
    return findings


def exit_code(findings: list[Finding]) -> int:
    """1 if anything failed, else 0 — so this can gate a deploy.

    ``warn`` deliberately does not fail the run: a stuck résumé or a stale
    cached label is real and worth surfacing, but it is a state an operator
    clears, not a reason to block. Only a broken invariant (or an unreachable
    datastore) is a hard stop.
    """
    return 1 if any(f.severity == "fail" for f in findings) else 0


_ICON = {"fail": "🔴", "warn": "🟡", "info": "·"}


def render(findings: list[Finding]) -> str:
    """Human-readable report. Never raises, whatever a detail string holds."""
    if not findings:
        return "✅ healthy — every deployment invariant checked is satisfied."
    lines = [f"{len(findings)} finding(s):", ""]
    for f in findings:
        lines.append(f"{_ICON.get(f.severity, '·')} {f.check} ({f.severity})")
        lines.append(f"    {f.detail}")
        lines.append(f"    → {f.remedy}")
        lines.append("")
    return "\n".join(lines)


async def main(profile_dir: Path) -> int:
    s = get_settings()
    pg = await asyncpg.create_pool(dsn=s.postgres_dsn, min_size=1, max_size=2)
    driver = AsyncGraphDatabase.driver(
        s.neo4j_uri, auth=(s.neo4j_user, s.neo4j_password)
    )
    try:
        findings = await run_checks(
            pg=pg, neo4j=driver, settings=s, profile_dir=profile_dir
        )  # noqa: E501
    finally:
        await driver.close()
        if pg is not None:
            await pg.close()
    print(render(findings))
    return exit_code(findings)


def _parse_args() -> argparse.Namespace:
    """CLI arguments, not env vars — CLAUDE.md forbids `os.environ` outside
    settings.py and a meta-test enforces it.

    ``--profiles`` exists because the default (repo-root-relative) resolves to
    `/docs/model-profiles` inside the api container, which mounts only `core/`
    at `/app`. The doctor then reported every model as UNPROFILED while a
    perfectly good profile sat committed on the host — a tool built to detect
    missing measurements, unable to see the measurements."""
    p = argparse.ArgumentParser(description="Check the live deployment's invariants.")
    p.add_argument("--profiles", type=Path, default=_DEFAULT_PROFILE_DIR)
    return p.parse_args()


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(asyncio.run(main(_parse_args().profiles)))
