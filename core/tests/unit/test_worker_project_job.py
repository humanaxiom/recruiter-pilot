"""Unit tests for ``src.worker.tasks._project_job`` / ``_job_projection_tx``
— the JD half of Phase 4b's graph projection.

Mirrors ``test_worker_project_resume.py``'s fake-tx-capture approach. Ported
behaviourally from hris ``apps/worker/src/worker/tasks.py::_project_job`` /
``_job_projection_tx``, with the same Decision-3 (resolve outside the write
transaction) and R8 (pinned label set — no ``Company``/``Institution``) pins
applied to the JD side.

``src.worker.tasks._project_job`` does not exist yet — this whole file fails
at collection (``ImportError``). RED half of the TDD cycle.
"""

from __future__ import annotations

import inspect
import re
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from src.worker.tasks import _job_projection_tx, _project_job

# All three skills used across the fixture payload below, pre-resolved to
# themselves (canonical == raw) unless a test overrides it.
_RESOLVED = {"python": "python", "postgresql": "postgresql", "kubernetes": "kubernetes"}

# ── fakes (same shape as test_worker_project_resume.py) ──────────────────


class _RecordingTx:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def run(self, cypher: str, **params: Any) -> list[Any]:
        self.calls.append((cypher, params))
        return []


def _acm(return_value: Any) -> MagicMock:
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=return_value)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _fake_driver_and_tx(
    resolve_result: dict[str, str],
) -> tuple[MagicMock, _RecordingTx, AsyncMock, list[str]]:
    tx = _RecordingTx()
    events: list[str] = []
    captured_write_args: list[tuple[Any, ...]] = []
    captured_write_kwargs: list[dict[str, Any]] = []

    async def _execute_write(fn: Any, *args: Any, **kwargs: Any) -> Any:
        events.append("write_start")
        captured_write_args.append(args)
        captured_write_kwargs.append(kwargs)
        result = await fn(tx, *args, **kwargs)
        events.append("write_end")
        return result

    session = MagicMock(name="session")
    session.execute_write = AsyncMock(side_effect=_execute_write)
    session._captured_write_args = captured_write_args
    session._captured_write_kwargs = captured_write_kwargs

    driver = MagicMock(name="driver")
    driver.session = MagicMock(return_value=_acm(session))

    async def _resolve(*args: Any, **kwargs: Any) -> dict[str, str]:
        events.append("resolve")
        return resolve_result

    resolve_mock = AsyncMock(side_effect=_resolve)
    return driver, tx, resolve_mock, events


def _payload(
    *,
    required: list[dict[str, Any]] | None = None,
    nice_to_have: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    required = (
        required
        if required is not None
        else [
            {"name": "python", "min_years": 4},
            {"name": "postgresql", "min_years": 3},
        ]
    )
    nice_to_have = (
        nice_to_have if nice_to_have is not None else [{"name": "kubernetes"}]
    )
    return {
        "embedding": [0.1] * 8,
        "extracted": {
            "title": "Senior Backend Engineer",
            "required_skills": required,
            "nice_to_have_skills": nice_to_have,
            "min_years_experience": 4,
            "education": {"min_level": "bachelors", "fields": []},
            "location": None,
            "remote_policy": None,
            "responsibilities": [],
        },
        "prompt_version": "jd_extract_v1",
    }


async def _project(
    payload: dict[str, Any], resolve_result: dict[str, str]
) -> tuple[_RecordingTx, MagicMock, list[str]]:
    job_id = uuid4()
    driver, tx, resolve_mock, events = _fake_driver_and_tx(resolve_result)
    llm = MagicMock(chat_json=AsyncMock())
    embedder = MagicMock(embed=AsyncMock())
    with patch("src.worker.tasks.skills_graph.resolve_canonical_names", resolve_mock):
        await _project_job(driver, job_id, payload, llm=llm, embedder=embedder)
    return tx, driver, events


# ── Job node + REQUIRES/NICE_TO_HAVE shape ────────────────────────────────


@pytest.mark.asyncio
async def test_job_node_merge_sets_title_and_summary_embedding() -> None:
    tx, _driver, _events = await _project(_payload(), _RESOLVED)
    job_calls = [
        (cypher, params)
        for cypher, params in tx.calls
        if re.search(r"MERGE\s*\(\s*j\s*:\s*Job", cypher, re.IGNORECASE)
    ]
    assert len(job_calls) == 1
    _cypher, params = job_calls[0]
    assert "Senior Backend Engineer" in params.values()
    assert [0.1] * 8 in params.values()


@pytest.mark.asyncio
async def test_required_skill_gets_a_requires_edge_with_is_must_have_true() -> None:
    tx, _driver, _events = await _project(
        _payload(required=[{"name": "python", "min_years": 4}], nice_to_have=[]),
        {"python": "python"},
    )
    # F4 (security re-audit): classify by MERGE, not a bare "REQUIRES"
    # substring — the old-edge cleanup's DELETE is a typed
    # ``-[r:REQUIRES|NICE_TO_HAVE]->`` (F4's own fix), so its Cypher text
    # ALSO contains "REQUIRES"; a naive substring filter would pick up the
    # DELETE call (whose params are just `jid`) instead of the actual MERGE
    # call, and the assertions below would fail against the wrong call.
    requires_calls = [
        (c, p) for c, p in tx.calls if "REQUIRES" in c and "MERGE" in c.upper()
    ]
    assert requires_calls
    _cypher, params = requires_calls[0]
    assert True in params.values()
    assert 4 in params.values()
    assert "python" in params.values()


@pytest.mark.asyncio
async def test_nice_to_have_skill_gets_a_nice_to_have_edge_not_requires() -> None:
    tx, _driver, _events = await _project(
        _payload(required=[], nice_to_have=[{"name": "kubernetes"}]),
        {"kubernetes": "kubernetes"},
    )
    # F4: same MERGE-scoped classification — the typed cleanup DELETE
    # legitimately mentions REQUIRES (it targets both edge types) even when
    # there are zero required skills this call.
    nice_calls = [
        (c, p) for c, p in tx.calls if "NICE_TO_HAVE" in c and "MERGE" in c.upper()
    ]
    requires_calls = [
        (c, p)
        for c, p in tx.calls
        if re.search(r"\bREQUIRES\b", c) and "MERGE" in c.upper()
    ]
    assert nice_calls
    assert not requires_calls


@pytest.mark.asyncio
async def test_requires_edge_uses_resolved_canonical_name_not_raw_name() -> None:
    tx, _driver, _events = await _project(
        _payload(required=[{"name": "Py", "min_years": 2}], nice_to_have=[]),
        {"Py": "python"},
    )
    # F4: MERGE-scoped (see the sibling test above for why).
    requires_calls = [
        (c, p) for c, p in tx.calls if "REQUIRES" in c and "MERGE" in c.upper()
    ]
    assert any("python" in p.values() for _c, p in requires_calls)
    assert not any("Py" in p.values() for _c, p in requires_calls)


@pytest.mark.asyncio
async def test_old_requires_and_nice_to_have_edges_are_dropped_before_recreation() -> (
    None
):
    tx, _driver, _events = await _project(_payload(), _RESOLVED)
    delete_idxs = [i for i, (c, _p) in enumerate(tx.calls) if "DELETE" in c.upper()]
    requires_idxs = [
        i
        for i, (c, _p) in enumerate(tx.calls)
        if "REQUIRES" in c and "MERGE" in c.upper()
    ]
    assert delete_idxs
    assert requires_idxs
    assert max(delete_idxs) < min(requires_idxs)


@pytest.mark.asyncio
async def test_old_edge_cleanup_delete_is_typed_not_a_blanket_wildcard() -> None:
    """F4 (security re-audit, MEDIUM). The old-edge cleanup DELETE must name
    exactly REQUIRES/NICE_TO_HAVE. A wildcard ``-[r]->(:Skill) DELETE r``
    silently destroys ANY OTHER Job->Skill edge type a future phase (4c/4d)
    might introduce, the moment such an edge exists — the earlier production
    code widened to the wildcard purely to dodge this file's naive
    substring-matching tests (now fixed above), which is backwards: tests
    must never drive production into a less-safe shape. Proven at the
    Cypher-text level here; ``test_graph_projection_e2e.py`` proves it
    behaviourally against a real Neo4j graph carrying a foreign edge type."""
    tx, _driver, _events = await _project(_payload(), _RESOLVED)
    delete_calls = [c for c, _p in tx.calls if "DELETE" in c.upper()]
    assert delete_calls
    for cypher in delete_calls:
        assert re.search(r"REQUIRES\s*\|\s*NICE_TO_HAVE", cypher), (
            "the cleanup DELETE is not typed to REQUIRES|NICE_TO_HAVE — it "
            "will delete every Job->Skill edge type, including ones this "
            "module never created (silent data loss)"
        )


# ── F6 (security re-audit): fail loud on a missing resolution entry ──────


@pytest.mark.asyncio
async def test_required_skill_name_absent_from_resolved_mapping_raises_loudly() -> None:
    from src.pipeline.skills_graph import UnresolvedSkillNameError

    with pytest.raises(UnresolvedSkillNameError):
        await _project(
            _payload(required=[{"name": "python", "min_years": 4}], nice_to_have=[]),
            {},  # no entry at all for "python"
        )


@pytest.mark.asyncio
async def test_nice_to_have_skill_name_absent_from_resolved_mapping_raises_loudly() -> (
    None
):
    from src.pipeline.skills_graph import UnresolvedSkillNameError

    with pytest.raises(UnresolvedSkillNameError):
        await _project(_payload(required=[], nice_to_have=[{"name": "kubernetes"}]), {})


@pytest.mark.asyncio
async def test_pii_shape_rejected_skill_is_dropped_silently_not_projected() -> None:
    """F3 — a ``None`` resolution (shape-rejected as PII) is a legitimate
    outcome, not a caller bug, and must not raise."""
    tx, _driver, _events = await _project(
        _payload(
            required=[{"name": "casey.rivera@example.test", "min_years": None}],
            nice_to_have=[],
        ),
        {"casey.rivera@example.test": None},
    )
    assert not any("casey.rivera@example.test" in p.values() for _c, p in tx.calls)
    merge_calls = [
        (c, p) for c, p in tx.calls if "REQUIRES" in c and "MERGE" in c.upper()
    ]
    assert not merge_calls


@pytest.mark.asyncio
async def test_pii_shape_rejected_nice_to_have_skill_is_dropped_silently() -> None:
    """Same as above, for the nice-to-have loop's independent None branch."""
    tx, _driver, _events = await _project(
        _payload(required=[], nice_to_have=[{"name": "casey.rivera@example.test"}]),
        {"casey.rivera@example.test": None},
    )
    assert not any("casey.rivera@example.test" in p.values() for _c, p in tx.calls)
    merge_calls = [
        (c, p) for c, p in tx.calls if "NICE_TO_HAVE" in c and "MERGE" in c.upper()
    ]
    assert not merge_calls


# ── R8: pinned label set (no Company/Institution) ─────────────────────────


@pytest.mark.asyncio
async def test_no_company_or_institution_writes_on_the_job_side() -> None:
    tx, _driver, _events = await _project(_payload(), _RESOLVED)
    cypher = "\n".join(c for c, _ in tx.calls)
    for label in ("Company", "Institution"):
        assert f":{label}" not in cypher


# ── ADR-008: display_name is written ONLY from the job/JD side ───────────
#
# A job description carries no candidate identity, so stamping the RAW
# (cleartext) skill name onto the Skill node's `display_name` is always
# safe — including when `canonical` is an opaque `h:<hash>` key for a
# non-vocab skill (the recruiter-facing `SkillContribution.skill` field
# still needs something readable to show).


@pytest.mark.asyncio
async def test_required_skill_display_name_is_set_to_the_raw_jd_text() -> None:
    required = [{"name": "Distributed Systems", "min_years": 3}]
    resolved = {"Distributed Systems": "h:deadbeefdeadbeefdeadbeefdeadbeef"}
    tx, _driver, _events = await _project(
        _payload(required=required, nice_to_have=[]), resolved
    )
    display_calls = [
        (c, p) for c, p in tx.calls if "display_name" in c and "MERGE" not in c.upper()
    ]
    assert display_calls
    assert any(p.get("display") == "Distributed Systems" for _c, p in display_calls)
    assert any(
        p.get("cname") == "h:deadbeefdeadbeefdeadbeefdeadbeef"
        for _c, p in display_calls
    )


@pytest.mark.asyncio
async def test_nice_to_have_skill_display_name_is_set_to_the_raw_jd_text() -> None:
    nice_to_have = [{"name": "Cloud-Native", "min_years": None}]
    resolved = {"Cloud-Native": "h:0123456789abcdef0123456789abcdef"}
    tx, _driver, _events = await _project(
        _payload(required=[], nice_to_have=nice_to_have), resolved
    )
    display_calls = [
        (c, p) for c, p in tx.calls if "display_name" in c and "MERGE" not in c.upper()
    ]
    assert display_calls
    assert any(p.get("display") == "Cloud-Native" for _c, p in display_calls)


@pytest.mark.asyncio
async def test_display_name_write_is_a_dedicated_statement_not_folded_into_edge() -> (
    None
):
    """The edge write's own params must never carry the raw JD text — kept
    as a separate statement so ``test_requires_edge_uses_resolved_canonical_
    name_not_raw_name``'s "raw name never appears in the edge call" pin
    stays meaningful even though display_name legitimately carries it
    elsewhere."""
    tx, _driver, _events = await _project(
        _payload(required=[{"name": "Py", "min_years": 2}], nice_to_have=[]),
        {"Py": "python"},
    )
    requires_calls = [
        (c, p) for c, p in tx.calls if "REQUIRES" in c and "MERGE" in c.upper()
    ]
    assert requires_calls
    assert not any("Py" in p.values() for _c, p in requires_calls)
    display_calls = [(c, p) for c, p in tx.calls if "display_name" in c]
    assert any(p.get("display") == "Py" for _c, p in display_calls)


# ── Decision 3: resolution outside the write transaction ─────────────────


@pytest.mark.asyncio
async def test_job_skills_are_resolved_before_the_write_transaction_opens() -> None:
    _tx, _driver, events = await _project(_payload(), _RESOLVED)
    assert events[0] == "resolve"
    assert events.index("resolve") < events.index("write_start")


@pytest.mark.asyncio
async def test_job_write_transaction_callback_never_receives_llm_or_embedder() -> None:
    driver, tx, resolve_mock, _events = _fake_driver_and_tx(_RESOLVED)
    llm = MagicMock(chat_json=AsyncMock())
    embedder = MagicMock(embed=AsyncMock())
    with patch("src.worker.tasks.skills_graph.resolve_canonical_names", resolve_mock):
        await _project_job(driver, str(uuid4()), _payload(), llm=llm, embedder=embedder)
    session = driver.session.return_value.__aenter__.return_value
    for args, kwargs in zip(
        session._captured_write_args, session._captured_write_kwargs, strict=True
    ):
        assert llm not in args and llm not in kwargs.values()
        assert embedder not in args and embedder not in kwargs.values()


def test_job_projection_tx_signature_has_no_llm_or_embedder_parameter() -> None:
    params = set(inspect.signature(_job_projection_tx).parameters)
    assert not params & {"llm", "embedder"}


# ── target implementation item 1: per-job REQUIRES/NICE_TO_HAVE display_name ──
#
# The bug this pins (skill-display-names-and-corpus-gap): `display_name` was
# previously written ONLY on the Skill node -- a single, global, last-writer-
# -wins property. Two jobs requiring the SAME canonical skill with DIFFERENT
# raw wording ("ReactJS" vs "React JS") stomp each other's node-level
# `display_name`, so whichever job's projection ran LAST wins for every job.
# The fix adds a SECOND, per-job display name on the REQUIRES/NICE_TO_HAVE
# RELATIONSHIP itself (`r.display_name`) -- kept as its OWN dedicated Cypher
# statement (never folded into the REQUIRES/NICE_TO_HAVE MERGE), matching this
# file's existing `test_display_name_write_is_a_dedicated_statement_not_
# folded_into_edge` pin for the node-level write above: the edge MERGE's own
# params must never carry the raw JD text directly.


def _relationship_display_calls(
    tx: _RecordingTx, edge_type: str
) -> list[tuple[str, dict[str, Any]]]:
    """Calls whose Cypher sets `display_name` AND mentions the relationship
    type -- distinguishes the NEW per-job (relationship) write from the
    EXISTING node-only write (`MATCH (s:Skill {canonical_key: $cname}) SET
    s.display_name = $display`), whose Cypher text never mentions
    REQUIRES/NICE_TO_HAVE at all."""
    return [
        (c, p)
        for c, p in tx.calls
        if "display_name" in c and re.search(rf"\b{edge_type}\b", c)
    ]


@pytest.mark.asyncio
async def test_required_skill_relationship_gets_its_own_per_job_display_name() -> None:
    required = [{"name": "Apache Airflow", "min_years": 2}]
    tx, _driver, _events = await _project(
        _payload(required=required, nice_to_have=[]), {"Apache Airflow": "airflow"}
    )
    rel_calls = _relationship_display_calls(tx, "REQUIRES")
    assert rel_calls, (
        "expected a dedicated Cypher statement setting a per-job display_name "
        "on the REQUIRES relationship -- got none"
    )
    assert any(p.get("display") == "Apache Airflow" for _c, p in rel_calls)


@pytest.mark.asyncio
async def test_nice_to_have_skill_relationship_gets_its_own_per_job_display_name() -> (
    None
):
    nice_to_have = [{"name": "Kube Native Tooling", "min_years": None}]
    tx, _driver, _events = await _project(
        _payload(required=[], nice_to_have=nice_to_have),
        {"Kube Native Tooling": "h:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
    )
    rel_calls = _relationship_display_calls(tx, "NICE_TO_HAVE")
    assert rel_calls, (
        "expected a dedicated Cypher statement setting a per-job display_name "
        "on the NICE_TO_HAVE relationship -- got none"
    )
    assert any(p.get("display") == "Kube Native Tooling" for _c, p in rel_calls)


@pytest.mark.asyncio
async def test_relationship_display_name_write_is_scoped_by_job_id() -> None:
    """The whole point of the relationship-level write is PER-JOB scoping --
    a call that carries `display` but no `jid` param cannot be the per-job
    write (the existing node-level write has no `jid` param at all: it
    MATCHes the Skill node globally, by `canonical_key` alone)."""
    required = [{"name": "Apache Airflow", "min_years": 2}]
    job_id = uuid4()
    driver, tx, resolve_mock, _events = _fake_driver_and_tx(
        {"Apache Airflow": "airflow"}
    )
    llm = MagicMock(chat_json=AsyncMock())
    embedder = MagicMock(embed=AsyncMock())
    with patch("src.worker.tasks.skills_graph.resolve_canonical_names", resolve_mock):
        await _project_job(
            driver,
            job_id,
            _payload(required=required, nice_to_have=[]),
            llm=llm,
            embedder=embedder,
        )
    rel_calls = _relationship_display_calls(tx, "REQUIRES")
    assert rel_calls
    assert any(
        p.get("jid") == str(job_id) and p.get("display") == "Apache Airflow"
        for _c, p in rel_calls
    )


@pytest.mark.asyncio
async def test_relationship_display_name_keeps_the_node_level_write() -> None:
    """The node-level `display_name` write (ADR-008, pre-existing) must stay,
    for ADR-008 continuity -- but it is NOT a fallback rung and MUST NEVER be
    rendered.

    `Skill.display_name` is written `MATCH (s:Skill {canonical_key: $cname})`
    -- global, last-writer-wins across every job. Rendering it was a cross-job
    information disclosure (job A's shortlist showing job B's JD wording to
    job A's assignees); the security gate caught it and the node rung was
    removed from the label chain, which is now the two-rung
    `coalesce(req.display_name, reqSkill.canonical_key)`. A stale edge
    therefore renders an opaque `h:<hex>` deliberately -- do not "fix" that by
    re-adding the node rung. See ADR-032 and
    `test_stage2_skill_label_source.py`, which fails loud if anyone does."""
    required = [{"name": "Apache Airflow", "min_years": 2}]
    tx, _driver, _events = await _project(
        _payload(required=required, nice_to_have=[]), {"Apache Airflow": "airflow"}
    )
    node_only_calls = [
        (c, p)
        for c, p in tx.calls
        if "display_name" in c and "MERGE" not in c.upper() and "REQUIRES" not in c
    ]
    assert node_only_calls, "the existing node-level display_name write regressed"
    assert any(p.get("display") == "Apache Airflow" for _c, p in node_only_calls)
    rel_calls = _relationship_display_calls(tx, "REQUIRES")
    assert rel_calls, "the new relationship-level display_name write is missing"


@pytest.mark.asyncio
async def test_relationship_display_name_not_folded_into_requires_merge() -> None:
    """Mirrors the sibling node-level pin
    (`test_display_name_write_is_a_dedicated_statement_not_folded_into_edge`):
    the REQUIRES MERGE's own params must never carry the raw JD text -- the
    relationship display_name write must be issued as ITS OWN statement, not
    folded into `MERGE (j)-[r:REQUIRES]->(s) SET r.min_years = ..., r.
    is_must_have = ...`."""
    required = [{"name": "Apache Airflow", "min_years": 2}]
    tx, _driver, _events = await _project(
        _payload(required=required, nice_to_have=[]), {"Apache Airflow": "airflow"}
    )
    merge_calls = [
        (c, p) for c, p in tx.calls if "REQUIRES" in c and "MERGE" in c.upper()
    ]
    assert merge_calls
    assert not any("Apache Airflow" in p.values() for _c, p in merge_calls), (
        "the REQUIRES MERGE call's own params carry the raw JD text -- the "
        "relationship display_name write must be a SEPARATE statement"
    )
