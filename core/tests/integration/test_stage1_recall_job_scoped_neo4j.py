"""Integration tests — ROADMAP A4 **M2**: stage-1 recall must be job-scoped,
against a REAL Neo4j (testcontainers).

**The defect.** ``resume_summary_idx`` is a **global** vector index over every
``:Resume`` node in the database — it is not partitioned by job, and cannot be:
Neo4j's ``db.index.vector.queryNodes`` takes no pre-filter. ``stage1_coarse``
therefore asked the index for the global top ``k*3`` (150 by default) and only
*then* applied ``WHERE r.job_id = $jid``.

So the filter runs **after** the crowd-out has already happened. Once the
corpus holds more than ~150 résumés in total, a job's own candidates compete
for those 150 slots against **every résumé of every other job** — and lose,
because similarity to a job description is not job-specific. A job with 20
applicants, **well under ``coarse_k=50``**, can come back with three of them,
or none.

**Why this is nearly invisible.** Every existing test uses one job and a
handful of résumés, where the global top-150 trivially contains the whole
corpus. The defect needs *other jobs' data* to exist before it appears, which
no unit test and no single-job integration test creates. It is a property of
the database's global contents, so only a real Neo4j with a populated
neighbourhood can show it — CLAUDE.md's rule exactly.

**Raising ``coarse_k`` does not fix it** and would mask it: the oversample is
``k*3``, so a bigger ``k`` buys a bigger *global* window, which the next
hundred résumés fill again. The pool being searched is still the whole
database.

**The measurement this file makes** is deliberately extreme in one direction —
the interfering résumés are made *more* similar to the JD than the job's own
are — because the failure is about **which pool is searched**, not about how
close the margins are. A corpus where the noise happens to be less similar
hides the bug without fixing it.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import AsyncIterator, Iterator

import pytest
from neo4j import AsyncDriver, AsyncGraphDatabase
from testcontainers.neo4j import Neo4jContainer

from src.pipeline.matching.orchestrator import stage1_coarse
from src.settings import get_settings
from src.worker.neo4j_bootstrap import bootstrap_neo4j_schema

_DIM = get_settings().llm_embedding_dim


@pytest.fixture(scope="module")
def neo4j_container() -> Iterator[Neo4jContainer]:
    with Neo4jContainer("neo4j:5-community") as container:
        yield container


@pytest.fixture
async def driver(neo4j_container: Neo4jContainer) -> AsyncIterator[AsyncDriver]:
    neo4j_driver = AsyncGraphDatabase.driver(
        neo4j_container.get_connection_url(),
        auth=("neo4j", neo4j_container.password),
    )
    await bootstrap_neo4j_schema(neo4j_driver)
    async with neo4j_driver.session() as session:
        await session.run("MATCH (n) DETACH DELETE n")
    try:
        yield neo4j_driver
    finally:
        await neo4j_driver.close()


def _unit_vector(angle: float) -> list[float]:
    """A unit vector in the first two dimensions, zero elsewhere.

    Keeping every vector in one plane makes the cosine — and therefore the
    expected ordering — exact arithmetic rather than something to eyeball.
    """
    vec = [0.0] * _DIM
    vec[0] = math.cos(angle)
    vec[1] = math.sin(angle)
    return vec


async def _seed(
    driver: AsyncDriver,
    *,
    job_id: uuid.UUID,
    job_vec: list[float],
    resumes: list[tuple[uuid.UUID, list[float]]],
) -> None:
    async with driver.session() as session:
        await session.run(
            "MERGE (j:Job {id: $jid}) SET j.summary_embedding = $vec",
            jid=str(job_id),
            vec=job_vec,
        )
        for rid, vec in resumes:
            await session.run(
                "MERGE (r:Resume {id: $rid}) "
                "SET r.job_id = $jid, r.summary_embedding = $vec",
                rid=str(rid),
                jid=str(job_id),
                vec=vec,
            )


async def test_a_jobs_own_candidates_are_not_crowded_out_by_other_jobs(
    driver: AsyncDriver,
) -> None:
    """THE pin.

    Target job: 5 résumés, a pool far below ``coarse_k=50``. Every one of them
    must come back.

    Interference: 300 résumés belonging to a DIFFERENT job, each made *more*
    similar to the target JD than the target's own candidates are. Under the
    global index the first 150 slots are consumed entirely by those, and
    ``WHERE r.job_id`` then filters the result down to nothing.
    """
    target_job = uuid.uuid4()
    other_job = uuid.uuid4()
    job_vec = _unit_vector(0.0)

    # The target's own candidates sit at a moderate angle from the JD.
    own = [(uuid.uuid4(), _unit_vector(0.8)) for _ in range(5)]
    await _seed(driver, job_id=target_job, job_vec=job_vec, resumes=own)

    # 300 résumés on another job, all CLOSER to the JD than any of the above.
    noise = [(uuid.uuid4(), _unit_vector(0.05)) for _ in range(300)]
    await _seed(driver, job_id=other_job, job_vec=job_vec, resumes=noise)

    got = await stage1_coarse(driver, target_job)
    got_ids = {c.resume_id for c in got}

    assert got_ids == {rid for rid, _ in own}, (
        f"stage-1 recall returned {len(got_ids)} of this job's 5 candidates. "
        "The job's pool is far below coarse_k, so every one of them must be "
        "recalled regardless of how many résumés other jobs hold."
    )
    assert all(c.resume_id in got_ids for c in got), "a foreign résumé leaked in"


async def test_recall_is_stable_as_the_rest_of_the_corpus_grows(
    driver: AsyncDriver,
) -> None:
    """The same job must recall the same candidates before and after unrelated
    résumés are loaded.

    This is the property a pilot actually depends on: adding a second
    requisition must not silently change the shortlist of the first. Under the
    global index it did.
    """
    target_job = uuid.uuid4()
    job_vec = _unit_vector(0.0)
    own = [(uuid.uuid4(), _unit_vector(0.5 + i * 0.01)) for i in range(8)]
    await _seed(driver, job_id=target_job, job_vec=job_vec, resumes=own)

    before = await stage1_coarse(driver, target_job)

    other_job = uuid.uuid4()
    noise = [(uuid.uuid4(), _unit_vector(0.02)) for _ in range(300)]
    await _seed(driver, job_id=other_job, job_vec=job_vec, resumes=noise)

    after = await stage1_coarse(driver, target_job)

    assert [c.resume_id for c in before] == [c.resume_id for c in after], (
        "loading another job's résumés changed this job's stage-1 recall — "
        "shortlists must not depend on unrelated corpus contents"
    )
    assert [c.vec_score for c in before] == [
        c.vec_score for c in after
    ], "the vec_scores moved too, so any downstream normalisation shifted"


async def test_scores_match_the_vector_index_normalisation(
    driver: AsyncDriver,
) -> None:
    """``vec_score`` must stay on the SAME [0,1] scale the vector index used.

    Both ``db.index.vector.queryNodes`` and ``vector.similarity.cosine`` report
    cosine normalised as ``(1 + cos) / 2``. If a job-scoped rewrite returned a
    raw cosine instead, every score would silently change scale — and
    ``normalise_vector_scores`` would carry that straight into ``score_final``
    without anything failing. Verified against the real server rather than
    assumed from documentation.
    """
    job_id = uuid.uuid4()
    job_vec = _unit_vector(0.0)
    identical = uuid.uuid4()
    orthogonal = uuid.uuid4()
    await _seed(
        driver,
        job_id=job_id,
        job_vec=job_vec,
        resumes=[
            (identical, _unit_vector(0.0)),
            (orthogonal, _unit_vector(math.pi / 2)),
        ],
    )

    got = {c.resume_id: c.vec_score for c in await stage1_coarse(driver, job_id)}

    assert got[identical] == pytest.approx(
        1.0, abs=1e-6
    ), "an identical vector must score 1.0 on the normalised scale"
    assert got[orthogonal] == pytest.approx(0.5, abs=1e-6), (
        "an orthogonal vector must score 0.5 — a raw cosine would give 0.0, "
        "which is the same number the index reports for OPPOSITE vectors"
    )


async def test_a_resume_without_an_embedding_is_skipped_not_crashed(
    driver: AsyncDriver,
) -> None:
    """The global index only ever contained embedded nodes, so an un-embedded
    résumé was invisible to stage 1 for free. A job-scoped MATCH sees every
    résumé of the job, including ones whose projection has not run yet — those
    must be skipped, not returned with a null score."""
    job_id = uuid.uuid4()
    embedded = uuid.uuid4()
    await _seed(
        driver,
        job_id=job_id,
        job_vec=_unit_vector(0.0),
        resumes=[(embedded, _unit_vector(0.3))],
    )
    async with driver.session() as session:
        await session.run(
            "MERGE (r:Resume {id: $rid}) SET r.job_id = $jid",
            rid=str(uuid.uuid4()),
            jid=str(job_id),
        )

    got = await stage1_coarse(driver, job_id)

    assert [c.resume_id for c in got] == [embedded]


async def test_k_still_bounds_the_result(driver: AsyncDriver) -> None:
    """Job-scoping must not quietly remove the ``k`` bound — a job with more
    applicants than ``coarse_k`` still yields at most ``k``, highest first."""
    job_id = uuid.uuid4()
    own = [(uuid.uuid4(), _unit_vector(0.1 + i * 0.001)) for i in range(30)]
    await _seed(driver, job_id=job_id, job_vec=_unit_vector(0.0), resumes=own)

    got = await stage1_coarse(driver, job_id, k=10)

    assert len(got) == 10
    scores = [c.vec_score for c in got]
    assert scores == sorted(scores, reverse=True), "results are not rank-ordered"
