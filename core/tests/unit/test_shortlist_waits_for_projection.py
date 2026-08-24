"""RED pin — ranking must not silently drop candidates the graph has not seen.

**Found by `scripts/smoke.sh`, 2026-08-22, and invisible to every other gate.**
Three résumés uploaded through the real UI all parsed successfully, with 56, 48
and 33 skills extracted. The shortlist came back with **two** of them. The third
— the one with the MOST skills — was missing. Re-running the ranking minutes
later, with nothing else changed, produced all three.

The cause is a race between two sources of truth. `resumes.status` becomes
``parsed`` in Postgres the moment the LLM pipeline finishes, but ranking reads
Neo4j, and graph projection is an asynchronous cron (`project_to_graph`, every
5s). Between those two events a résumé is *parsed* and *unrankable*, and Stage 1
recall (``MATCH (r:Resume {job_id: $jid})``) simply does not see it.

**Why this is a product defect and not a test artefact.** The résumé table shows
``parsed``. A recruiter watching it click Generate the moment the last row turns
green gets a shortlist missing candidates — with no error, no warning, and no
way to tell which. On a 39-résumé pilot batch that is a candidate silently
excluded from consideration, which is the single worst thing this product can
do.

No unit or integration test could catch it: both either mock the graph or drive
projection synchronously, so the window does not exist for them.

**The fix defers rather than blocks**, and is BOUNDED. `project_to_graph` runs
every 5s, so a short retry resolves the ordinary case; but a résumé whose
projection genuinely failed would otherwise defer for ever, so past the ceiling
the run proceeds and records that it did. Ranking a subset is the status quo —
doing it silently is the bug.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from arq import Retry

from src.worker import matching_tasks


def _neo4j(projected: int) -> MagicMock:
    session = MagicMock()
    result = MagicMock()
    result.single = AsyncMock(return_value={"n": projected})
    session.run = AsyncMock(return_value=result)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    driver = MagicMock()
    driver.session = MagicMock(return_value=ctx)
    return driver


def _conn(eligible: int) -> MagicMock:
    conn = MagicMock()
    conn.fetchval = AsyncMock(return_value=eligible)
    return conn


async def test_ranking_defers_while_projection_is_behind() -> None:
    """The reported case: 3 parsed in Postgres, 2 visible in the graph."""
    with pytest.raises(Retry):
        await matching_tasks.ensure_projection_caught_up(
            _conn(3), _neo4j(2), job_id=uuid4(), job_try=1, max_tries=5, defer_s=5
        )


async def test_ranking_proceeds_once_the_graph_has_caught_up() -> None:
    await matching_tasks.ensure_projection_caught_up(
        _conn(3), _neo4j(3), job_id=uuid4(), job_try=1, max_tries=5, defer_s=5
    )


async def test_a_graph_ahead_of_postgres_never_defers() -> None:
    """Projection can legitimately hold nodes Postgres no longer counts — a
    withdrawn résumé keeps its node. Only a graph BEHIND the eligible set can
    drop a candidate from ranking, so only that direction may defer."""
    await matching_tasks.ensure_projection_caught_up(
        _conn(2), _neo4j(5), job_id=uuid4(), job_try=1, max_tries=5, defer_s=5
    )


async def test_the_defer_is_bounded_so_a_failed_projection_cannot_wedge_it() -> None:
    """A résumé whose projection genuinely failed is never coming. Deferring
    for ever would turn one broken row into a job that can never be ranked —
    strictly worse than the bug being fixed."""
    await matching_tasks.ensure_projection_caught_up(
        _conn(3), _neo4j(2), job_id=uuid4(), job_try=5, max_tries=5, defer_s=5
    )


async def test_nothing_eligible_never_defers() -> None:
    """An empty job ranks empty. Deferring would pin the UI on 'Generating…'
    for a job with no parsed résumés at all."""
    await matching_tasks.ensure_projection_caught_up(
        _conn(0), _neo4j(0), job_id=uuid4(), job_try=1, max_tries=5, defer_s=5
    )


async def test_an_unreachable_graph_does_not_defer() -> None:
    """Fail OPEN here, deliberately, and it is the one place in this repo that
    does. If Neo4j cannot be counted, the ranking that follows will fail on its
    own and report properly; turning an unreadable count into an infinite defer
    would replace a loud failure with a silent one."""
    driver = MagicMock()
    driver.session = MagicMock(side_effect=OSError("neo4j down"))
    await matching_tasks.ensure_projection_caught_up(
        _conn(3), driver, job_id=uuid4(), job_try=1, max_tries=5, defer_s=5
    )


async def test_the_eligible_count_excludes_withdrawn_resumes() -> None:
    """A withdrawn résumé is not a candidate, so it must not hold up ranking
    for everyone else."""
    conn = _conn(3)
    await matching_tasks.ensure_projection_caught_up(
        conn, _neo4j(3), job_id=uuid4(), job_try=1, max_tries=5, defer_s=5
    )
    sql = str(conn.fetchval.await_args.args[0]).lower()
    assert "withdrawn_at is null" in sql
    assert "parsed" in sql


def test_the_helper_is_wired_into_shortlist_job() -> None:
    """A guard that exists but is never called is the ROADMAP A7 shape, and
    this repo has shipped that exact thing repeatedly."""
    import inspect

    src = inspect.getsource(matching_tasks.shortlist_job)
    assert "ensure_projection_caught_up" in src
