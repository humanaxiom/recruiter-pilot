"""RED pin — ``src.doctor``: invariant checks against the REAL deployment.

**Why this exists, stated as the measurement that forced it.** This repo has
~5,500 unit and ~540 integration tests and 94% coverage. In the two sessions
before this module was written, nine defects reached the running product anyway,
and the suite could not have caught a single one of them. Four lived on the
frontend↔API seam that no test crosses. The other four lived in *state* — a
graph projected before a fix shipped, a `jsonb` column holding text that is not
JSON, a résumé stuck mid-parse, a container booting into a race.

The second group is the one this module is for, and it is invisible to testing
**by construction**: a fixture is always freshly built, always well-formed,
always young. The sharpest instance (ROADMAP A7 (20), "the fix that never ran")
was ADR-032, which shipped 2026-08-07 and had never applied to a single row
thirteen days later, because every job in the database predated it and the write
only fires on projection. Every gate was green the whole time. A count of
unlabelled edges would have said so on day one.

So the doctor does not test code. **It asks the live deployment whether the
invariants the code promises are actually true of the data that is there.**

**The three properties pinned below are the ones that decide whether it is worth
having at all:**

1. **A check that cannot run REPORTS that it could not run.** Never a silent
   pass. This is the ADR-031 inert-PII-scan lesson and the whole reason this
   repo distrusts green: a doctor that says "healthy" when it could not reach
   Neo4j is worse than no doctor, because it manufactures the confidence it was
   built to withhold.
2. **Every finding carries a remedy.** A finding without one is another prose
   invariant nobody acts on — the exact A7 shape this module exists to break.
3. **It never raises.** Any check that throws becomes a `fail` finding naming
   the exception. An operator runs this when something is already wrong; a
   traceback instead of a report is a second outage.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from src import doctor


def _pg(**counts: int) -> MagicMock:
    """A Postgres pool whose `fetchval` answers each check in call order."""
    conn = MagicMock(name="pg")
    conn.fetchval = AsyncMock(side_effect=list(counts.values()))
    conn.fetch = AsyncMock(return_value=[])
    pool = MagicMock(name="pool")
    pool.acquire = MagicMock(return_value=_acm(conn))
    return pool


def _acm(value: Any) -> Any:
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=value)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


def _neo(records: list[dict[str, Any]]) -> MagicMock:
    session = MagicMock(name="session")

    async def _run(*_a: Any, **_k: Any) -> Any:
        result = MagicMock()
        result.single = AsyncMock(return_value=records.pop(0) if records else {})
        return result

    session.run = AsyncMock(side_effect=_run)
    driver = MagicMock(name="driver")
    driver.session = MagicMock(return_value=_acm(session))
    return driver


# ── the contract every finding must satisfy ──────────────────────────────


def test_a_finding_always_carries_a_remedy() -> None:
    """A finding that does not say what to DO is another invariant in prose."""
    f = doctor.Finding(
        check="x", severity="fail", count=3, detail="three bad rows", remedy="do y"
    )
    assert f.remedy
    assert f.remedy in doctor.render([f])


def test_render_says_healthy_only_when_there_is_nothing_to_report() -> None:
    assert "healthy" in doctor.render([]).lower()


def test_the_exit_code_is_nonzero_only_for_a_failing_check() -> None:
    ok = doctor.Finding(check="a", severity="info", count=0, detail="", remedy="-")
    warn = doctor.Finding(check="b", severity="warn", count=1, detail="", remedy="-")
    bad = doctor.Finding(check="c", severity="fail", count=1, detail="", remedy="-")
    assert doctor.exit_code([]) == 0
    assert doctor.exit_code([ok, warn]) == 0
    assert doctor.exit_code([ok, warn, bad]) == 1


# ── property 1: a check that cannot run must not pass silently ───────────


async def test_an_unreachable_neo4j_reports_a_failure_not_a_clean_bill() -> None:
    """**The load-bearing test in this file.** A doctor that says "healthy"
    when it could not reach a datastore manufactures exactly the confidence it
    exists to withhold — the ADR-031 inert-scan lesson, applied to the tool
    that is supposed to be immune to it."""
    driver = MagicMock(name="driver")
    driver.session = MagicMock(side_effect=OSError("connection refused"))
    findings = await doctor.run_checks(pg=_pg(a=0, b=0, c=0, d=0, e=0), neo4j=driver)
    unreachable = [f for f in findings if f.severity == "fail" and "neo4j" in f.check]
    assert unreachable, "an unreachable Neo4j was reported as healthy"
    assert "connection refused" in unreachable[0].detail
    assert doctor.exit_code(findings) == 1


async def test_a_check_that_raises_becomes_a_finding_not_a_traceback() -> None:
    """The doctor is run when something is already wrong. A traceback instead
    of a report is a second outage."""
    pool = MagicMock()
    pool.acquire = MagicMock(side_effect=RuntimeError("pool is closed"))
    findings = await doctor.run_checks(pg=pool, neo4j=_neo([]))
    assert any(f.severity == "fail" for f in findings)
    assert any("pool is closed" in f.detail for f in findings)


# ── property 2: the checks that map to real incidents ────────────────────


async def test_unlabelled_job_skill_edges_are_reported() -> None:
    """ROADMAP A7 (20) — "the fix that never ran". ADR-032 shipped 2026-08-07
    and had never applied to one row thirteen days later; 160 of 160 edges
    carried no label and every shortlist rendered `h:<hash>` where a skill name
    belonged. This single number would have said so on day one."""
    findings = await doctor.run_checks(
        pg=_pg(jobs=0, stale=0, stuck=0, undecodable=0, unattributable=0),
        neo4j=_neo([{"n": 160}, {"n": 0}]),
    )
    edge = next(f for f in findings if f.check == "neo4j.job_skill_labels")
    assert edge.severity == "fail"
    assert edge.count == 160
    assert "re-project" in edge.remedy.lower() or "backfill" in edge.remedy.lower()


async def test_a_fully_labelled_graph_reports_nothing_for_that_check() -> None:
    findings = await doctor.run_checks(
        pg=_pg(jobs=0, stale=0, stuck=0, undecodable=0, unattributable=0),
        neo4j=_neo([{"n": 0}, {"n": 0}]),
    )
    assert not [f for f in findings if f.check == "neo4j.job_skill_labels"]


async def test_shortlists_still_caching_hashed_labels_are_reported() -> None:
    """The second half of A7 (20): `score_breakdown` caches the RENDERED label,
    so a graph backfill stays invisible until each job is regenerated. Fixing
    the graph and stopping there leaves the screen unchanged — which is
    indistinguishable, to the person looking at it, from not having fixed it."""
    findings = await doctor.run_checks(
        pg=_pg(jobs=0, stale=55, stuck=0, undecodable=0, unattributable=0),
        neo4j=_neo([{"n": 0}, {"n": 0}]),
    )
    stale = next(f for f in findings if f.check == "pg.shortlist_hashed_labels")
    assert stale.count == 55
    assert "regenerate" in stale.remedy.lower()


async def test_resumes_stuck_mid_parse_are_reported() -> None:
    """The LLM-timeout class: a parse that exceeds the client timeout leaves
    the résumé at `uploaded` forever, and a dead worker looks exactly like a
    healthy stack that silently never ranks."""
    findings = await doctor.run_checks(
        pg=_pg(jobs=0, stale=0, stuck=7, undecodable=0, unattributable=0),
        neo4j=_neo([{"n": 0}, {"n": 0}]),
    )
    stuck = next(f for f in findings if f.check == "pg.resumes_stuck")
    assert stuck.count == 7
    assert stuck.severity in ("warn", "fail")


async def test_undecodable_audit_details_are_reported() -> None:
    """ROADMAP A7 (19): one row of `details` that is not valid JSON used to
    500 the entire access record — the first page an auditor opens."""
    findings = await doctor.run_checks(
        pg=_pg(jobs=0, stale=0, stuck=0, undecodable=2, unattributable=0),
        neo4j=_neo([{"n": 0}, {"n": 0}]),
    )
    bad = next(f for f in findings if f.check == "pg.audit_details_decodable")
    assert bad.count == 2


async def test_unattributable_audit_writes_are_reported() -> None:
    """The ADR-034 exploit signature. D2 = option B closed the fallback that
    produced these; a NEW one appearing means it has been reintroduced."""
    findings = await doctor.run_checks(
        pg=_pg(jobs=0, stale=0, stuck=0, undecodable=0, unattributable=4),
        neo4j=_neo([{"n": 0}, {"n": 0}]),
    )
    api = next(f for f in findings if f.check == "pg.unattributable_audit_writes")
    assert api.count == 4


async def test_a_clean_deployment_reports_healthy() -> None:
    """The counterpart to every check above: a doctor that always finds
    something is a doctor nobody runs twice."""
    findings = await doctor.run_checks(
        pg=_pg(jobs=0, stale=0, stuck=0, undecodable=0, unattributable=0),
        neo4j=_neo([{"n": 0}, {"n": 0}]),
    )
    assert findings == []
    assert doctor.exit_code(findings) == 0
    assert "healthy" in doctor.render(findings).lower()


@pytest.mark.parametrize("payload", [None, "", "not json", json.dumps({"ok": 1}), "[]"])
def test_render_never_raises_on_any_finding_detail(payload: Any) -> None:
    f = doctor.Finding(
        check="x", severity="warn", count=1, detail=str(payload), remedy="r"
    )
    assert isinstance(doctor.render([f]), str)


# ── the check that would have caught an unauthenticated pilot stack ──────
#
# Found 2026-08-21, and it is the sharpest argument for this module existing.
# `docker-compose.yml` mentions CAS_ENABLED only in a COMMENT ("Set in .env"),
# and a variable in `.env` reaches a container only if the compose file names it
# in that service's `environment:` block. It does not. So every `docker compose
# up` produced an auth-DISABLED stack while the comment said otherwise, and the
# audit-log viewer — the most sensitive read in the product — was reachable with
# no login.
#
# Nothing could have caught this: not the unit suite (settings default to False
# and the tests say so), not the integration suite (it never boots compose), not
# `validate_startup_auth_config` (which only refuses CAS-ON-with-no-keys, never
# CAS silently OFF). It is deployment STATE, which is exactly this module's job,
# and the first version of this module missed it — so it is pinned now.
#
# It fires only when there is DATA to protect: an empty dev stack running open
# is a reasonable default, and a check that cries wolf on a fresh clone is a
# check people learn to ignore.


async def test_an_auth_disabled_stack_with_real_data_is_a_failure() -> None:
    settings = MagicMock(cas_enabled=False, auth_enabled=False)
    findings = await doctor.run_checks(
        pg=_pg(jobs=3, stale=0, stuck=0, undecodable=0, unattributable=0),
        neo4j=_neo([{"n": 0}, {"n": 0}]),
        settings=settings,
    )
    auth = next(f for f in findings if f.check == "deploy.auth_disabled")
    assert auth.severity == "fail"
    assert "cas" in auth.remedy.lower() or "auth" in auth.remedy.lower()


async def test_an_empty_stack_running_open_is_not_reported() -> None:
    """A check that fires on a fresh clone is a check people learn to skim."""
    settings = MagicMock(cas_enabled=False, auth_enabled=False)
    findings = await doctor.run_checks(
        pg=_pg(jobs=0, stale=0, stuck=0, undecodable=0, unattributable=0),
        neo4j=_neo([{"n": 0}, {"n": 0}]),
        settings=settings,
    )
    assert not [f for f in findings if f.check == "deploy.auth_disabled"]


async def test_an_authenticated_stack_is_not_reported() -> None:
    settings = MagicMock(cas_enabled=True, auth_enabled=True)
    findings = await doctor.run_checks(
        pg=_pg(jobs=9, stale=0, stuck=0, undecodable=0, unattributable=0),
        neo4j=_neo([{"n": 0}, {"n": 0}]),
        settings=settings,
    )
    assert not [f for f in findings if f.check == "deploy.auth_disabled"]


# ── the model a swap points at must have been measured ───────────────────
#
# The data-centre move will swap gpt-oss:20b for something larger. Every
# model-shaped constant in this repo was measured against gpt-oss:20b and will
# be wrong at that moment, silently — which is how 2026-08-21 went, within a
# single model. `src.model_probe` produces the measurement; these checks make
# NOT having one, or having one the config contradicts, a loud failure.


async def test_a_model_with_no_accepted_profile_is_reported(tmp_path: Any) -> None:
    settings = MagicMock(
        cas_enabled=True, llm_model_generation="brand-new-model:70b", llm_timeout_s=900
    )
    findings = await doctor.run_checks(
        pg=_pg(jobs=1, stale=0, stuck=0, undecodable=0, unattributable=0),
        neo4j=_neo([{"n": 0}, {"n": 0}]),
        settings=settings,
        profile_dir=tmp_path,
    )
    unmeasured = next(f for f in findings if f.check == "deploy.model_unprofiled")
    assert unmeasured.severity == "fail"
    assert "brand-new-model:70b" in unmeasured.detail


async def test_a_timeout_below_the_measured_recommendation_is_reported(
    tmp_path: Any,
) -> None:
    """The exact 2026-08-21 shape: the budget moved, the timeout did not, and
    every call timed out. Here the measurement disagrees with the config and
    says so instead of waiting for a retry storm."""
    (tmp_path / "measured-model.json").write_text(
        json.dumps(
            {
                "model": "measured-model",
                "recommended_timeout_s": 900,
                "accepted": True,
                "results": [],
            }
        ),
        encoding="utf-8",
    )
    settings = MagicMock(
        cas_enabled=True, llm_model_generation="measured-model", llm_timeout_s=300
    )
    findings = await doctor.run_checks(
        pg=_pg(jobs=1, stale=0, stuck=0, undecodable=0, unattributable=0),
        neo4j=_neo([{"n": 0}, {"n": 0}]),
        settings=settings,
        profile_dir=tmp_path,
    )
    tight = next(f for f in findings if f.check == "deploy.timeout_below_profile")
    assert tight.severity == "fail"
    assert "900" in tight.detail and "300" in tight.detail


async def test_a_measured_model_with_a_matching_timeout_is_quiet(
    tmp_path: Any,
) -> None:
    (tmp_path / "measured-model.json").write_text(
        json.dumps(
            {
                "model": "measured-model",
                "recommended_timeout_s": 900,
                "accepted": True,
                "results": [],
            }
        ),
        encoding="utf-8",
    )
    settings = MagicMock(
        cas_enabled=True, llm_model_generation="measured-model", llm_timeout_s=900
    )
    findings = await doctor.run_checks(
        pg=_pg(jobs=1, stale=0, stuck=0, undecodable=0, unattributable=0),
        neo4j=_neo([{"n": 0}, {"n": 0}]),
        settings=settings,
        profile_dir=tmp_path,
    )
    assert not [f for f in findings if f.check.startswith("deploy.model")]
    assert not [f for f in findings if f.check == "deploy.timeout_below_profile"]
