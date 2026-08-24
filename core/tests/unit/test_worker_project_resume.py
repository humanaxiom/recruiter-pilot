"""Unit tests for ``src.worker.resume_tasks.project_resume`` /
``_resume_projection_tx`` — the résumé half of Phase 4b's graph projection.

Uses a FAKE Neo4j tx/session that RECORDS every Cypher statement and its
params, so every assertion below is against what would actually be written,
not against a mocked return value. Ported behaviourally from hris
``apps/worker/src/worker/resume_tasks.py::project_resume`` /
``_resume_projection_tx``, with the human-locked deviations from
``docs/EXTRACTION_PLAN.md`` (4b row) pinned explicitly:

* **Decision 1 (CRIT) / R1 — Neo4j gets NO chunk text, ever.** Not even a
  200-char preview. hris writes ``preview=chunk["text"][:200]`` — the résumé
  header chunk's first 200 chars ARE the candidate's name/email/phone. The
  reveal path is Postgres-backed (``resumes.parsed``, ADR-007 §6), so Neo4j
  never needs it. A test asserting only "text_preview is non-empty" would
  stay green under a real leak — see
  ``test_no_text_preview_key_is_ever_written_to_any_node`` below, which walks
  every captured Cypher CALL's params instead.
* **R2 (HIGH) — the outbox payload has NO ``chunks[].text`` key at all**
  (ADR-007 §7). A verbatim ``_resume_projection_tx`` does ``chunk["text"]`` —
  a hard subscript on a key the payload doesn't have — which raises
  ``KeyError``, gets swallowed by the drainer's blanket ``except Exception``,
  and the row retries forever. Pinned by feeding chunks shaped EXACTLY like
  the real outbox payload (``id``/``section``/``page`` only).
* **ADR-008 — skill resolution is a PURE, LOCAL computation, no Neo4j
  session, no ``llm``, no ``embedder`` at all.** ``project_resume`` computes
  ``{raw_name: canonical_key}`` via ``skills_graph.resume_skill_canonical_key``
  (a plain function, no I/O) BEFORE ``session.execute_write`` is ever called
  — superseding the OLD "Decision 3" architecture (resolve via
  ``skills_graph.resolve_canonical_names`` on a plain session), which is now
  JOB-side only (see ``test_worker_project_job.py``). The callback handed to
  ``execute_write`` never receives ``llm``/``embedder`` either — it never did
  need them, and now nothing upstream of it does either.
* **R8 (MED) — the pinned label set.** The projection writes only
  ``{Resume, ResumeChunk, Skill}`` from this module (``Job`` is the JD side)
  — never ``Company``/``Institution``, even though core's Neo4j bootstrap
  already declares constraints for those labels (an inviting trap). The
  ``Skill`` nodes this module MERGEs on never get ``display_name``/
  ``embedding``/any cleartext written from this side either (ADR-008) — see
  the dedicated section near the bottom of this file.

* **ADR-026 (FU-8) — ``unproject_resume``, the un-project exclusion point.**
  On withdrawal, the résumé's ``Resume`` node (and its ``ResumeChunk``s) are
  DETACH DELETEd from Neo4j so it simply is not in the
  ``resume_summary_idx`` recall set any more (ADR-026 decision 3, option 1).
  ``Skill`` nodes are NEVER touched — they are shared across every résumé
  that has ever claimed them, so deleting one candidate's projection must
  not erase a vocabulary node other résumés still reference.
"""

from __future__ import annotations

import inspect
import re
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from src.pipeline import skills_graph
from src.worker.resume_tasks import (
    _resume_projection_tx,
    project_resume,
    unproject_resume,
)

# ── fakes ─────────────────────────────────────────────────────────────────


class _FakeResult:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self._rows = rows or []

    async def single(self) -> dict[str, Any] | None:
        return self._rows[0] if self._rows else None

    def __aiter__(self) -> _FakeResult:
        self._iter = iter(self._rows)
        return self

    async def __anext__(self) -> dict[str, Any]:
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration from None


class _RecordingTx:
    """Captures every ``tx.run(cypher, **params)`` call, in order."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def run(self, cypher: str, **params: Any) -> _FakeResult:
        self.calls.append((cypher, params))
        return _FakeResult([])


def _acm(return_value: Any) -> MagicMock:
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=return_value)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _fake_driver_and_tx() -> tuple[MagicMock, _RecordingTx, list[str]]:
    """A neo4j driver double whose ``session()`` yields an object exposing
    ONLY ``execute_write`` (no bare ``.run`` — ADR-008 means résumé-side
    resolution never touches Neo4j at all, so nothing here should ever call
    it). Returns (driver, recording_tx, events)."""
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

    return driver, tx, events


def _outbox_chunk(
    chunk_id: str, section: str = "experience", page: int = 1
) -> dict[str, Any]:
    """A chunk exactly as the real outbox payload shapes it — NO 'text' key
    (ADR-007 §7 / R2)."""
    return {"id": chunk_id, "section": section, "page": page}


def _payload(
    *,
    skills: list[dict[str, Any]] | None = None,
    chunks: list[dict[str, Any]] | None = None,
    chunk_embs: dict[str, list[float]] | None = None,
    job_id: str | None = "11111111-1111-1111-1111-111111111111",
    total_years_experience: int = 6,
) -> dict[str, Any]:
    chunks = (
        chunks
        if chunks is not None
        else [_outbox_chunk("c_001"), _outbox_chunk("c_002")]
    )
    chunk_embs = (
        chunk_embs if chunk_embs is not None else {c["id"]: [0.1] * 8 for c in chunks}
    )
    skills = skills if skills is not None else [{"name": "python", "years": 5}]
    return {
        "parsed": {
            "total_years_experience": total_years_experience,
            "skills": skills,
            "experience": [],
            "education": [],
            "chunks": chunks,
            "cover_letter_chunks": [],
        },
        "summary_emb": [0.2] * 8,
        "chunk_embs": chunk_embs,
        "prompt_version": "resume_core_v1+resume_skills_v2",
        "job_id": job_id,
    }


def _all_run_calls(tx: _RecordingTx) -> list[tuple[str, dict[str, Any]]]:
    return tx.calls


def _all_cypher(tx: _RecordingTx) -> str:
    return "\n".join(c for c, _ in tx.calls)


def _all_params(tx: _RecordingTx) -> list[Any]:
    out: list[Any] = []
    for _cypher, params in tx.calls:
        out.extend(params.values())
    return out


async def _project(
    payload: dict[str, Any],
) -> tuple[_RecordingTx, MagicMock, list[str]]:
    resume_id = uuid4()
    driver, tx, events = _fake_driver_and_tx()
    await project_resume(driver, resume_id=resume_id, payload=payload)
    return tx, driver, events


async def _unproject(resume_id: Any) -> tuple[_RecordingTx, MagicMock, list[str]]:
    driver, tx, events = _fake_driver_and_tx()
    await unproject_resume(driver, resume_id=resume_id)
    return tx, driver, events


# ── Decision 1 / R1: no chunk text, no preview, anywhere ─────────────────


@pytest.mark.asyncio
async def test_no_text_preview_key_is_ever_written_to_any_node() -> None:
    """R1 (CRIT). Walks EVERY captured Cypher call's params — not just the
    ResumeChunk creation call — so a preview smuggled onto a different node
    (e.g. the Resume node itself) would also be caught."""
    tx, _driver, _events = await _project(_payload())
    for cypher, params in _all_run_calls(tx):
        assert "text_preview" not in params
        assert "preview" not in params
        assert "text_preview" not in cypher


@pytest.mark.asyncio
async def test_resume_chunk_creation_params_never_include_a_text_key() -> None:
    tx, _driver, _events = await _project(_payload())
    chunk_calls = [
        (cypher, params)
        for cypher, params in _all_run_calls(tx)
        if "ResumeChunk" in cypher
        and ("CREATE" in cypher.upper() or "MERGE" in cypher.upper())
    ]
    assert chunk_calls, "expected at least one ResumeChunk write"
    for _cypher, params in chunk_calls:
        assert "text" not in params


@pytest.mark.asyncio
async def test_resume_chunk_node_only_carries_id_section_page_and_embedding() -> None:
    tx, _driver, _events = await _project(_payload())
    chunk_calls = [
        (cypher, params)
        for cypher, params in _all_run_calls(tx)
        if re.search(r"CREATE\s*\(\s*c\s*:\s*ResumeChunk", cypher, re.IGNORECASE)
    ]
    assert chunk_calls
    allowed = {"rid", "cid", "section", "page", "emb"}
    for _cypher, params in chunk_calls:
        assert set(params.keys()) <= allowed, params.keys()


# ── R2: the outbox payload has no chunks[].text — no KeyError ────────────


@pytest.mark.asyncio
async def test_projection_does_not_keyerror_on_outbox_shaped_chunks() -> None:
    """R2 (HIGH). A verbatim ``chunk["text"]`` subscript raises KeyError on
    a real outbox payload's chunks (which never carry 'text' — ADR-007 §7).
    This must not raise at all."""
    payload = _payload(chunks=[_outbox_chunk("c_001")], chunk_embs={"c_001": [0.3] * 8})
    tx, _driver, _events = await _project(payload)
    assert any("ResumeChunk" in cypher for cypher, _ in _all_run_calls(tx))


@pytest.mark.asyncio
async def test_chunks_with_no_matching_embedding_are_skipped() -> None:
    payload = _payload(
        chunks=[_outbox_chunk("c_001"), _outbox_chunk("c_002")],
        chunk_embs={"c_001": [0.3] * 8},  # c_002 has no embedding
    )
    tx, _driver, _events = await _project(payload)
    chunk_ids_written = {
        params.get("cid")
        for cypher, params in _all_run_calls(tx)
        if re.search(r"CREATE\s*\(\s*c\s*:\s*ResumeChunk", cypher, re.IGNORECASE)
    }
    assert chunk_ids_written == {"c_001"}


# ── Resume node: only total_years_experience, no PII, no candidate ───────


@pytest.mark.asyncio
async def test_resume_node_write_carries_no_candidate_key_anywhere() -> None:
    tx, _driver, _events = await _project(_payload())
    for _cypher, params in _all_run_calls(tx):
        assert "candidate" not in params
        for key in ("name", "email", "phone", "location"):
            assert key not in params, f"unexpected key {key!r} in {params!r}"


@pytest.mark.asyncio
async def test_resume_node_write_sets_only_total_years_experience_scalar() -> None:
    """Only ``total_years_experience`` from the parsed payload lands on the
    Resume node — no experience/education list, ever (R8: those would need
    Company/Institution nodes this module must not create)."""
    payload = _payload(total_years_experience=9)
    tx, _driver, _events = await _project(payload)
    resume_calls = [
        (cypher, params)
        for cypher, params in _all_run_calls(tx)
        if re.search(r"MERGE\s*\(\s*r\s*:\s*Resume", cypher, re.IGNORECASE)
    ]
    assert len(resume_calls) == 1
    _cypher, params = resume_calls[0]
    assert 9 in params.values()
    assert "experience" not in params
    assert "education" not in params


@pytest.mark.asyncio
async def test_no_experience_education_company_institution_writes_anywhere() -> None:
    """R8 (MED) unit-level pin, complementing the integration label-set
    sweep. Looks for the Cypher LABEL syntax specifically (``:Company`` /
    ``:Institution``), not a loose substring, to avoid false positives on
    unrelated words."""
    tx, _driver, _events = await _project(_payload())
    cypher = _all_cypher(tx)
    for label in ("Company", "Institution"):
        assert f":{label}" not in cypher, f"unexpected {label} label"


@pytest.mark.asyncio
async def test_job_id_is_coalesced_with_the_existing_value() -> None:
    tx, _driver, _events = await _project(_payload(job_id=None))
    resume_cypher = next(
        cypher
        for cypher, _params in _all_run_calls(tx)
        if re.search(r"MERGE\s*\(\s*r\s*:\s*Resume", cypher, re.IGNORECASE)
    )
    assert re.search(
        r"coalesce\(\s*\$\w+\s*,\s*r\.job_id\s*\)", resume_cypher, re.IGNORECASE
    )


@pytest.mark.asyncio
async def test_status_and_updated_at_are_set_on_the_resume_node() -> None:
    tx, _driver, _events = await _project(_payload())
    resume_cypher = next(
        cypher
        for cypher, _params in _all_run_calls(tx)
        if re.search(r"MERGE\s*\(\s*r\s*:\s*Resume", cypher, re.IGNORECASE)
    )
    assert "status" in resume_cypher
    assert "updated_at" in resume_cypher


# ── HAS_SKILL uses the RESOLVED canonical key ─────────────────────────────


@pytest.mark.asyncio
async def test_has_skill_edge_uses_resolved_canonical_key_not_raw_name() -> None:
    """ "Py" is an alias of the vocab term "python" (aliases.yaml) — real,
    unmocked ``resume_skill_canonical_key`` resolution, exercised end to
    end."""
    payload = _payload(skills=[{"name": "Py", "years": 4, "last_used_year": 2026}])
    tx, _driver, _events = await _project(payload)
    skill_calls = [p for c, p in _all_run_calls(tx) if "HAS_SKILL" in c]
    assert skill_calls
    assert any(p.get("cn") == "python" or "python" in p.values() for p in skill_calls)
    assert not any("Py" in p.values() for p in skill_calls)


@pytest.mark.asyncio
async def test_has_skill_edge_for_a_non_vocab_skill_uses_an_opaque_hash_key() -> None:
    """ADR-008: a skill name that ISN'T in the ~220-term vocabulary (e.g. the
    candidate's own name, extracted as a "skill" by a hallucinating small
    model) never reaches the graph as cleartext — it lands as an opaque
    ``h:<hash>`` key, indistinguishable at the Cypher-parameter level from
    any other non-vocab skill."""
    payload = _payload(skills=[{"name": "Casey Rivera", "years": 4}])
    tx, _driver, _events = await _project(payload)
    skill_calls = [p for c, p in _all_run_calls(tx) if "HAS_SKILL" in c]
    assert skill_calls
    cn = next(p["cn"] for p in skill_calls if "cn" in p)
    assert cn.startswith(skills_graph._HASH_KEY_PREFIX)
    assert not any(
        "casey" in str(v).lower() or "rivera" in str(v).lower()
        for _c, p in _all_run_calls(tx)
        for v in p.values()
    )


# ── F6 (security re-audit): fail loud on a missing resolution entry ──────


@pytest.mark.asyncio
async def test_skill_name_absent_from_resolved_mapping_raises_loudly() -> None:
    """F6 (HIGH). A name ABSENT from the resolved mapping means the caller
    never even attempted to resolve it — a caller bug (this mapping is
    normally built exhaustively, by dict comprehension, over the same skills
    list — so this scenario only arises via a direct, deliberately-broken
    call to the write-tx callback, exercised here). hris's
    ``resolved_skills.get(name, name)`` silently falls back to the
    UNRESOLVED raw name, which matches no ``Skill`` node in Cypher and the
    HAS_SKILL edge silently vanishes (R5's exact failure class,
    reintroduced). Must fail loud instead."""
    tx = _RecordingTx()
    with pytest.raises(skills_graph.UnresolvedSkillNameError):
        await _resume_projection_tx(
            tx,
            "resume-1",
            {
                "total_years_experience": 4,
                "skills": [{"name": "python", "years": 4}],
                "chunks": [],
            },
            [0.1] * 8,
            {},
            {},  # no entry at all for "python"
            job_id=None,
        )


@pytest.mark.asyncio
async def test_skill_name_resolved_to_none_is_dropped_silently_not_projected() -> None:
    """A ``None`` value means the name was shape-rejected as junk (email/
    phone-shaped) at the resolution boundary — this is a legitimate outcome,
    not a caller bug: the skill/edge must be dropped silently, and this must
    NOT raise ``UnresolvedSkillNameError``."""
    payload = _payload(
        skills=[
            {"name": "python", "years": 4},
            {"name": "casey.rivera@example.test"},
        ]
    )
    tx, _driver, _events = await _project(payload)
    # MERGE-scoped — "HAS_SKILL" alone also matches the old-edge cleanup's
    # ``DELETE h`` call, which carries no skill-identifying params at all.
    skill_calls = [
        p for c, p in _all_run_calls(tx) if "HAS_SKILL" in c and "MERGE" in c.upper()
    ]
    assert len(skill_calls) == 1
    assert any(p.get("cn") == "python" or "python" in p.values() for p in skill_calls)
    assert not any(
        "casey.rivera@example.test" in p.values() for _c, p in _all_run_calls(tx)
    )


@pytest.mark.asyncio
async def test_old_skill_edges_and_chunks_are_detached_before_recreation() -> None:
    """Idempotency at the write-tx level — a re-parse must not accumulate
    stale HAS_CHUNK/HAS_SKILL edges. Order: DELETE calls precede CREATE."""
    tx, _driver, _events = await _project(_payload())
    calls = _all_run_calls(tx)
    delete_idxs = [i for i, (c, _p) in enumerate(calls) if "DELETE" in c.upper()]
    create_idxs = [
        i
        for i, (c, _p) in enumerate(calls)
        if "CREATE" in c.upper() and "ResumeChunk" in c
    ]
    assert delete_idxs, "expected at least one DETACH DELETE / DELETE"
    assert create_idxs
    assert max(delete_idxs) < min(create_idxs)


# ── ADR-008: résumé-side skill resolution is pure/local, never Neo4j/LLM ──


@pytest.mark.asyncio
async def test_resolve_canonical_names_is_never_called_from_the_resume_side() -> None:
    """ADR-008 supersedes the OLD Decision-3 architecture: the résumé side
    no longer calls ``skills_graph.resolve_canonical_names`` (job-side only
    now) at all — skill resolution is pure and local
    (``resume_skill_canonical_key``)."""
    with patch(
        "src.pipeline.skills_graph.resolve_canonical_names", new_callable=AsyncMock
    ) as resolve_mock:
        await _project(_payload())
    resolve_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_write_transaction_callback_never_receives_llm_or_embedder() -> None:
    """The callback handed to ``execute_write`` must not have ``llm``/
    ``embedder`` in its args/kwargs at all — so it CANNOT call
    ``chat_json``/``embed`` while the write transaction is open, not merely
    'doesn't happen to' in this particular test run."""
    resume_id = uuid4()
    driver, _tx, _events = _fake_driver_and_tx()
    await project_resume(driver, resume_id=resume_id, payload=_payload())
    session = driver.session.return_value.__aenter__.return_value
    for args, kwargs in zip(
        session._captured_write_args, session._captured_write_kwargs, strict=True
    ):
        assert "llm" not in kwargs
        assert "embedder" not in kwargs
        assert not any(hasattr(a, "chat_json") or hasattr(a, "embed") for a in args)


# ── Architectural: no Postgres dependency, no llm/embedder (Decision 1/ADR-008)


def test_project_resume_signature_has_no_postgres_connection_parameter() -> None:
    """Decision 1 supersedes the EXTRACTION_PLAN's earlier suggestion of
    reading a chunk-text preview from ``resumes.parsed`` — Neo4j gets NO
    chunk text at all, so this function must not even depend on a Postgres
    connection/pool to read one."""
    params = set(inspect.signature(project_resume).parameters)
    assert not params & {"conn", "pool", "pg_pool"}


def test_project_resume_signature_has_no_llm_or_embedder_parameter() -> None:
    """ADR-008: résumé-side skill resolution needs no model call at all any
    more, so ``project_resume`` itself takes no ``llm``/``embedder`` either —
    a stronger guarantee than "the write-tx callback doesn't get them"."""
    params = set(inspect.signature(project_resume).parameters)
    assert not params & {"llm", "embedder"}


def test_resume_projection_tx_signature_has_no_llm_or_embedder_parameter() -> None:
    """Architectural guarantee: the write-tx function itself cannot even be
    called with an llm/embedder — the parameter doesn't exist."""
    params = set(inspect.signature(_resume_projection_tx).parameters)
    assert not params & {"llm", "embedder"}


# ── ADR-008: the Skill node this module MERGEs on is never display_name'd,
# embedded, or given any other cleartext — canonical_key only. ──────────────


@pytest.mark.asyncio
async def test_resume_side_never_writes_display_name() -> None:
    tx, _driver, _events = await _project(_payload(skills=[{"name": "python"}]))
    cypher = _all_cypher(tx)
    assert "display_name" not in cypher


@pytest.mark.asyncio
async def test_resume_side_never_writes_an_embedding_onto_a_skill_node() -> None:
    """The Resume/ResumeChunk nodes DO carry an embedding (summary/chunk
    vectors) — this pins specifically that no SKILL node write from this
    module ever sets one."""
    tx, _driver, _events = await _project(_payload(skills=[{"name": "python"}]))
    skill_node_calls = [
        (c, p)
        for c, p in _all_run_calls(tx)
        if re.search(r"Skill\s*\{", c) and "embedding" in c
    ]
    assert skill_node_calls == []


@pytest.mark.asyncio
async def test_resume_side_merges_the_skill_node_itself_not_just_the_edge() -> None:
    """A brand-new (never job-required) skill's node may not exist at all
    yet — the résumé side's own MERGE is what creates it (``ON CREATE`` sets
    nothing but the key), so the subsequent HAS_SKILL edge write never
    silently no-ops against a nonexistent node."""
    tx, _driver, _events = await _project(_payload(skills=[{"name": "python"}]))
    skill_merge_calls = [
        c
        for c, _p in _all_run_calls(tx)
        if re.search(r"MERGE\s*\(\s*s\s*:\s*Skill", c, re.IGNORECASE)
    ]
    assert skill_merge_calls, "expected the résumé side to MERGE its own Skill node"


@pytest.mark.asyncio
async def test_resume_side_categories_write_curated_wins_over_a_payload_override() -> (
    None
):
    """UPDATED (ROADMAP A2, Phase 3.3 skill-family classifier, slice 1):
    the OLD name/docstring here ("still vocab scoped") meant, pre-feature,
    that a categories WRITE ever happening at all implied vocab membership
    -- true only because ``categories_for`` degrading to ``[]`` for a
    hash-keyed skill meant ``ensure_categories``'s ``if cats:`` guard never
    fired for one. Post-feature that implication is gone: a hashed skill CAN
    now get a categories write too, from the payload's classifier-assigned
    value (see ``test_resume_side_writes_classifier_categories_for_a_hashed_skill``
    below). What this test pins now is the STRONGER, more precise claim the
    old name only implied by accident: for an IN-VOCAB skill specifically,
    the write is ``ensure_categories``'s curated set ONLY, never a
    classifier/payload value -- even if a buggy or future producer attached
    a bogus ``categories`` list to an in-vocab skill's outbox row. Curated
    must win; a classifier value must never override it."""
    tx, _driver, _events = await _project(
        _payload(
            skills=[
                {
                    "name": "python",
                    # A payload override that MUST be ignored -- "python" is
                    # in-vocab, so only its curated categories.yaml families
                    # may ever be written for it.
                    "categories": ["not_a_real_family_a_buggy_producer_sent"],
                }
            ]
        )
    )
    categories_calls = [(c, p) for c, p in _all_run_calls(tx) if "categories" in c]
    assert categories_calls
    written = next(p["cats"] for _c, p in categories_calls if "cats" in p)
    assert written == skills_graph.categories_for("python")
    assert "not_a_real_family_a_buggy_producer_sent" not in written


@pytest.mark.asyncio
async def test_resume_side_writes_classifier_categories_for_a_hashed_skill() -> None:
    """UPDATED (this decision memo, 2026-08-19, slice 2): the parse-time
    classifier's assignment rides the outbox payload straight through to the
    graph for a hashed (out-of-vocab) skill -- projection itself makes no LLM
    call; it only writes what it was handed. CHANGED from the slice-1
    behaviour this test used to pin: the write now lands on the SEPARATE
    ``Skill.classified_categories`` property, NEVER on ``Skill.categories``
    (which stays exclusively curated, from ``ensure_categories``) -- a
    curated family and an inferred one must remain distinguishable in the
    graph forever. Live measurement against the real tailnet gpt-oss:20b
    found the classifier stably mis-families some skills, and family credit
    is transitive across the whole résumé, so the record must be provenance-
    tagged even though (per the new ``match_use_classified_families`` flag,
    default off) it does not yet drive ranking."""
    payload = _payload(
        skills=[{"name": "microfabrication", "categories": ["hardware"]}]
    )
    tx, _driver, _events = await _project(payload)
    skill_calls = [
        p for c, p in _all_run_calls(tx) if "HAS_SKILL" in c and "MERGE" in c.upper()
    ]
    hashed_key = next(p["cn"] for p in skill_calls if "cn" in p)
    assert hashed_key.startswith(skills_graph._HASH_KEY_PREFIX)

    classified_calls = [
        (c, p)
        for c, p in _all_run_calls(tx)
        if re.search(r"SET\s+s\.classified_categories\s*=", c)
    ]
    assert any(p.get("cats") == ["hardware"] for _c, p in classified_calls), (
        "expected a Cypher call writing s.classified_categories = ['hardware'] "
        "for the hashed skill node -- the payload's classifier-assigned "
        "categories never reached the graph"
    )

    curated_calls = [
        (c, p)
        for c, p in _all_run_calls(tx)
        if re.search(r"SET\s+s\.categories\s*=", c)
    ]
    assert curated_calls == [], (
        "a classifier-assigned family for a hashed skill must NEVER be "
        "written to the curated s.categories property -- curated and "
        "inferred provenance must stay distinguishable in the graph forever"
    )


@pytest.mark.asyncio
async def test_resume_side_no_categories_write_for_a_hashed_skill_the_classifier_skipped() -> (  # noqa: E501
    None
):
    """Conservative default, pinned at the projection layer: a hashed skill
    whose payload carries NO ``categories`` key at all (the classifier gave
    no confident answer, or was never run) gets NO categories write --
    identical to how every hash-keyed skill behaved before this feature
    existed."""
    payload = _payload(skills=[{"name": "a totally novel unclassified skill"}])
    tx, _driver, _events = await _project(payload)
    categories_calls = [(c, p) for c, p in _all_run_calls(tx) if "categories" in c]
    assert categories_calls == []


# ── ADR-026 (FU-8): unproject_resume — the withdrawal un-project point ────


@pytest.mark.asyncio
async def test_unproject_resume_detach_deletes_the_resume_node() -> None:
    resume_id = uuid4()
    tx, _driver, _events = await _unproject(resume_id)
    cypher = _all_cypher(tx)
    assert re.search(
        r"MATCH\s*\(\s*r\s*:\s*Resume\s*\{\s*id\s*:\s*\$\w+\s*\}\s*\)",
        cypher,
        re.IGNORECASE,
    )
    assert "DETACH DELETE" in cypher.upper()


@pytest.mark.asyncio
async def test_unproject_resume_removes_its_resume_chunks_too() -> None:
    tx, _driver, _events = await _unproject(uuid4())
    cypher = _all_cypher(tx)
    assert "ResumeChunk" in cypher


@pytest.mark.asyncio
async def test_unproject_resume_never_touches_a_skill_node_or_edge() -> None:
    """Skill nodes/edges are SHARED across every résumé that has ever
    claimed them — un-projecting one candidate must never delete or even
    reference a Skill, which would corrupt every OTHER résumé's HAS_SKILL
    edges that still point at it."""
    tx, _driver, _events = await _unproject(uuid4())
    cypher = _all_cypher(tx)
    assert ":Skill" not in cypher
    assert "HAS_SKILL" not in cypher


@pytest.mark.asyncio
async def test_unproject_resume_binds_the_resume_id_as_a_parameter() -> None:
    resume_id = uuid4()
    tx, _driver, _events = await _unproject(resume_id)
    all_values = [v for _cypher, params in _all_run_calls(tx) for v in params.values()]
    assert str(resume_id) in all_values


@pytest.mark.asyncio
async def test_unproject_resume_issues_at_least_one_write() -> None:
    tx, _driver, _events = await _unproject(uuid4())
    assert tx.calls, "unproject_resume must issue at least one Cypher write"


def test_unproject_resume_signature_has_no_postgres_llm_or_embedder_parameter() -> None:
    """Mirrors ``project_resume``'s own architectural guarantees — un-project
    is a pure Neo4j operation, no Postgres round trip, no model call."""
    params = set(inspect.signature(unproject_resume).parameters)
    assert not params & {"conn", "pool", "pg_pool", "llm", "embedder"}


def test_unproject_resume_is_async() -> None:
    assert inspect.iscoroutinefunction(unproject_resume)
