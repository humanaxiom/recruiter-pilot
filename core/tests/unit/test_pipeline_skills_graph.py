"""Unit tests for ``src.pipeline.skills_graph`` — the Neo4j-backed half of
skill normalisation, deferred out of Phase 3 (ADR-007 §4) and landing here.

Ported behaviourally from hris ``apps/worker/src/worker/skill_normalize.py``
(``_resolve_canonical`` / ``normalize_skill`` / ``_ask_llm_tiebreaker`` /
``categories_for`` / ``ensure_categories`` / ``_category_table``), with
THREE deliberate, human-locked deviations this file pins:

* **Decision 3 — resolution runs OUTSIDE any Neo4j write transaction.**
  hris's ``_resolve_canonical`` takes a ``tx`` (an ``AsyncManagedTransaction``
  passed to ``execute_write``) and calls ``embedder.embed`` / ``llm.chat_json``
  from inside it — up to 40 sequential local-model round trips held under one
  write lock. Here, resolution takes a plain ``session`` and issues every
  Cypher statement via auto-commit ``session.run(...)``, never
  ``session.execute_write``/``execute_read``. Every fake session below makes
  those two methods raise if called AT ALL, so any test that reaches them
  fails loudly rather than silently passing.
* **Decision 4 — the LLM tiebreaker's answer is constrained to the ``near``
  candidate set.** hris's Cypher ``MATCH (s:Skill {canonical_name: $c})``
  against a hallucinated ``$c`` matches nothing and is a silent no-op — the
  skill's ``HAS_SKILL``/``REQUIRES`` edge simply never gets created, no error,
  no log. Here a tiebreaker answer that isn't one of the ``near`` candidates'
  own names is treated exactly like "no match" and falls through to
  create-new, so a real Skill node backing the answer always exists before
  any caller writes an edge to it.
* **Thresholds are settings, not hard-coded module constants** — hris's
  ``AUTO_MERGE_THRESHOLD = 0.92`` / ``TIEBREAKER_THRESHOLD = 0.88`` become
  ``settings.skill_auto_merge_threshold`` / ``settings.skill_tiebreaker_threshold``
  (see ``test_settings.py``), proven READ (not just present) by
  ``test_auto_merge_threshold_is_read_from_settings_not_hardcoded`` below.

ADR-008 (architectural PII fix, phase 4b rework): the person-name-shape /
personal-name-lexicon / vendor-prefix-veto machinery this file used to pin
(rounds 2-5 of the security re-audit) is DELETED — see
``src.pipeline.skills_graph``'s module docstring for the full rationale.
``reject_reason_for_skill_name`` is now shape-ONLY junk filtering (email/
phone-shape, length/token cap); the privacy control moved to canonical-key
hashing (``_hash_key`` / ``_canonical_key_for_normalised`` /
``resume_skill_canonical_key``), pinned in the dedicated sections below.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.pipeline import skills_graph

# ── fakes ─────────────────────────────────────────────────────────────────


class _FakeRecord(dict[str, Any]):
    """A Neo4j ``Record`` double — ``record["key"]`` and ``dict(record)`` both
    work, matching how hris's source reads query results."""


class _FakeResult:
    """A Neo4j query-result double supporting ``await result.single()`` and
    ``async for record in result``, matching the two access patterns hris's
    ``_resolve_canonical`` uses."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = [_FakeRecord(r) for r in rows]

    async def single(self) -> _FakeRecord | None:
        return self._rows[0] if self._rows else None

    def __aiter__(self) -> _FakeResult:
        self._iter = iter(self._rows)
        return self

    async def __anext__(self) -> _FakeRecord:
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration from None


def _managed_tx_forbidden() -> AsyncMock:
    """A callable that fails the test loudly if it is ever awaited — wired
    onto ``execute_write``/``execute_read`` on every fake session below, so
    decision 3 (resolution never opens a managed transaction) is enforced by
    EVERY test in this file, not just a dedicated one."""

    async def _raise(*args: Any, **kwargs: Any) -> None:
        raise AssertionError(
            "skill resolution must run OUTSIDE any Neo4j managed transaction "
            "(decision 3) — execute_write/execute_read must never be called "
            "during resolve_canonical_names/_resolve_canonical"
        )

    return AsyncMock(side_effect=_raise)


def _make_session(run_results: list[_FakeResult]) -> MagicMock:
    session = MagicMock(name="session")
    session.run = AsyncMock(side_effect=run_results)
    session.execute_write = _managed_tx_forbidden()
    session.execute_read = _managed_tx_forbidden()
    return session


def _make_llm(match: str | None = None, *, raises: bool = False) -> MagicMock:
    if raises:
        return MagicMock(chat_json=AsyncMock(side_effect=RuntimeError("llm down")))
    out = MagicMock(match=match)
    return MagicMock(chat_json=AsyncMock(return_value=out))


def _make_embedder(vectors: list[list[float]] | None = None) -> MagicMock:
    vecs = vectors if vectors is not None else [[0.1] * 8]
    return MagicMock(embed=AsyncMock(return_value=vecs))


def _run_calls(session: MagicMock) -> list[tuple[str, dict[str, Any]]]:
    return [
        (call.args[0] if call.args else call.kwargs.get("cypher", ""), call.kwargs)
        for call in session.run.await_args_list
    ]


# ── exact-match path ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_exact_match_short_circuits_before_any_vector_or_llm_call() -> None:
    session = _make_session([_FakeResult([{"name": "python"}])])
    llm = _make_llm()
    embedder = _make_embedder()

    resolved = await skills_graph.resolve_canonical_names(
        session, ["Python"], llm=llm, embedder=embedder
    )

    assert resolved == {"Python": "python"}
    assert session.run.await_count == 1
    embedder.embed.assert_not_awaited()
    llm.chat_json.assert_not_awaited()


@pytest.mark.asyncio
async def test_exact_match_cypher_never_queries_by_aliases() -> None:
    """L2 (security re-audit): a direct structural guard on the exact-match
    branch's own Cypher text, independent of any auto-merge/alias fixture —
    the previous coverage of this property (no `OR $n IN s.aliases`) was
    purely incidental, via the GCP-alias non-regression test happening to
    take this branch. Pre-fix, this branch's `MATCH` also matched
    `$n IN s.aliases`, which let a graph-learned alias redirect the
    returned key (see F1's docstring on `_resolve_one`) and defeated the
    `canonical_key` index. The fast path must stay a single indexed lookup
    on `canonical_key` alone, forever."""
    session = _make_session([_FakeResult([{"name": "python"}])])
    await skills_graph.resolve_canonical_names(
        session, ["Python"], llm=_make_llm(), embedder=_make_embedder()
    )
    exact_match_cypher = _run_calls(session)[0][0]
    assert "aliases" not in exact_match_cypher
    assert "canonical_key: $n" in exact_match_cypher


@pytest.mark.asyncio
async def test_resolve_canonical_names_keys_the_result_by_the_input_name() -> None:
    """Callers (``project_resume``/``_project_job``) look up
    ``resolved[skill["name"]]`` using the RAW name they already hold — the
    returned mapping must be keyed by that exact input string, not by a
    re-normalised form."""
    session = _make_session(
        [_FakeResult([{"name": "python"}]), _FakeResult([{"name": "postgresql"}])]
    )
    resolved = await skills_graph.resolve_canonical_names(
        session,
        ["  Python  ", "PostgreSQL"],
        llm=_make_llm(),
        embedder=_make_embedder(),
    )
    assert set(resolved.keys()) == {"  Python  ", "PostgreSQL"}


# ── F3 (security re-audit): PII-shaped skill names are rejected outright ──
#
# Layer 1 of the defence-in-depth fix — an LLM-authored skill name is free
# text (`ResumeSkill.name` is a 200-char-capped string, not a vocabulary
# allowlist), and this module is the ONLY place a skill name is ever handed
# to the embedder before landing in Neo4j cleartext. A name that is SHAPED
# like contact info, or implausibly long/verbose, must never reach
# `embed()`/any Cypher call, and must resolve to `None` (never a canonical
# name a caller could write an edge against).


@pytest.mark.asyncio
async def test_email_shaped_skill_name_is_rejected_before_any_io() -> None:
    session = _make_session([])
    llm = _make_llm()
    embedder = _make_embedder()

    resolved = await skills_graph.resolve_canonical_names(
        session, ["casey.rivera@example.test"], llm=llm, embedder=embedder
    )

    assert resolved == {"casey.rivera@example.test": None}
    session.run.assert_not_awaited()
    embedder.embed.assert_not_awaited()
    llm.chat_json.assert_not_awaited()


@pytest.mark.parametrize(
    "name",
    [
        "john.smith @ corp.test",  # S6: whitespace around '@'
        "casey.rivera (at) example.test",  # S6: '(at)' obfuscation + whitespace
    ],
)
def test_whitespace_obfuscated_email_shape_is_still_rejected(name: str) -> None:
    """S6 (security re-audit round 3): the OLD `_EMAIL_SHAPE_RE` required an
    exact, whitespace-free `local@domain` literal — a whitespace-padded '@'
    or an '(at)'/'[at]' obfuscation (both common copy-paste/anti-scraping
    header renderings) sailed straight through."""
    assert skills_graph.reject_reason_for_skill_name(name) == "email_shape"


@pytest.mark.asyncio
async def test_phone_shaped_skill_name_is_rejected_before_any_io() -> None:
    session = _make_session([])
    resolved = await skills_graph.resolve_canonical_names(
        session, ["555-0101"], llm=_make_llm(), embedder=_make_embedder()
    )
    assert resolved == {"555-0101": None}
    session.run.assert_not_awaited()


@pytest.mark.asyncio
async def test_phone_shaped_skill_name_with_punctuation_is_rejected() -> None:
    session = _make_session([])
    resolved = await skills_graph.resolve_canonical_names(
        session, ["+1 (604) 555-0101"], llm=_make_llm(), embedder=_make_embedder()
    )
    assert resolved == {"+1 (604) 555-0101": None}
    session.run.assert_not_awaited()


@pytest.mark.asyncio
async def test_implausibly_long_skill_name_is_rejected() -> None:
    long_name = "a" * 61  # over _MAX_SKILL_NAME_CHARS
    session = _make_session([])
    resolved = await skills_graph.resolve_canonical_names(
        session, [long_name], llm=_make_llm(), embedder=_make_embedder()
    )
    assert resolved == {long_name: None}
    session.run.assert_not_awaited()


@pytest.mark.asyncio
async def test_too_many_tokens_skill_name_is_rejected() -> None:
    """Round 2 (security re-audit): the token cap was widened 6 -> 8 (zero of
    the 220 shipped vocab terms trip 6, so this is headroom for a legitimate
    multi-word certification name, not a regression) — this must exceed the
    NEW cap, not the old one."""
    many_tokens = "one two three four five six seven eight nine"  # 9 tokens
    session = _make_session([])
    resolved = await skills_graph.resolve_canonical_names(
        session, [many_tokens], llm=_make_llm(), embedder=_make_embedder()
    )
    assert resolved == {many_tokens: None}
    session.run.assert_not_awaited()


@pytest.mark.asyncio
async def test_widened_token_cap_does_not_reject_at_eight_tokens() -> None:
    """Non-regression for the widened cap: exactly 8 tokens (the new limit)
    must NOT be rejected on token-count grounds alone (a 7-8 token
    certification name is legitimate — Decision B's skill-recall guard)."""
    eight_tokens = "alpha bravo charlie delta echo foxtrot golf hotel"
    # F1 (security re-audit round 2): the exact-match fast path now queries by
    # the PURE `canonical_key` (never the raw/aliased name echoed back by the
    # fake row) — a single non-empty result is enough to hit it, whatever the
    # fake's own "name" field says.
    session = _make_session([_FakeResult([{"name": eight_tokens}])])
    resolved = await skills_graph.resolve_canonical_names(
        session, [eight_tokens], llm=_make_llm(), embedder=_make_embedder()
    )
    assert resolved[eight_tokens] == skills_graph._canonical_key_for_normalised(
        skills_graph._basic_normalise(eight_tokens)
    )


@pytest.mark.asyncio
async def test_rejection_is_logged_as_a_category_never_the_raw_value(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """R3 discipline — the rejection log line must carry a CATEGORY
    (email/phone/length), never the value that triggered it."""
    caplog.set_level(logging.WARNING)
    session = _make_session([])
    await skills_graph.resolve_canonical_names(
        session,
        ["casey.rivera@example.test"],
        llm=_make_llm(),
        embedder=_make_embedder(),
    )
    all_text = "\n".join(r.getMessage() for r in caplog.records)
    assert "casey.rivera@example.test" not in all_text
    assert "rejected" in all_text.lower()


@pytest.mark.asyncio
async def test_legitimate_short_multiword_skill_name_is_not_rejected() -> None:
    """Non-regression: a real multi-word skill name (well under the shape
    caps) must resolve normally, not get swept up by the PII guard.

    "Google Cloud Platform" is a listed alias of the "gcp" vocab entry
    (``aliases.yaml``), so its PURE canonical key (F1) is "gcp" — not the
    literal input string, and not whatever a fake row happens to echo back."""
    session = _make_session([_FakeResult([{"name": "google cloud platform"}])])
    resolved = await skills_graph.resolve_canonical_names(
        session,
        ["Google Cloud Platform"],
        llm=_make_llm(),
        embedder=_make_embedder(),
    )
    assert resolved["Google Cloud Platform"] == "gcp"
    session.run.assert_awaited()


# ── ADR-008: reject_reason_for_skill_name is shape-only junk filtering ────
#
# Rounds 2-5 tried to reject a skill name that LOOKS like a person's name —
# deleted (see the module docstring's ADR-008 section). A name that IS the
# candidate's own identity (e.g. "Casey Rivera") now sails straight through
# this function: the load-bearing privacy control moved to the graph-
# projection layer (canonical-key hashing — see the tests further down this
# file and ``resume_skill_canonical_key``'s own tests).


@pytest.mark.parametrize(
    "name",
    [
        "Casey Rivera",
        "Rivera, Casey",
        "Casey M. Rivera",
        "Casey-Rivera",
        "Rivera",
        "John Smith",
        "RIVERA, CASEY",
        "CASEY RIVERA",
        "casey rivera",
        "Sean McDonald",
        "John O'Brien",
        "Maria del Carmen Rivera Lopez",
        "Ana van der Berg",
        "Кейси Ривера",
        "李伟",
    ],
)
def test_person_shaped_names_are_no_longer_rejected_by_this_function(
    name: str,
) -> None:
    """ADR-008: none of these shapes are rejected here any more — this is
    now a documented, deliberate behaviour change (privacy moved downstream
    to hashing), not a regression. Pinned explicitly so a future "helpful"
    re-add of shape heuristics here doesn't sneak back in unreviewed."""
    assert skills_graph.reject_reason_for_skill_name(name) is None


@pytest.mark.parametrize(
    "name",
    [
        "Kafka",
        "Django",
        "Kubernetes",
        "distributed systems",
        "data engineering",
        "natural language processing",
        "event driven architecture",
        "test driven development",
        "Amazon Aurora",
        "IBM Watson",
        "Apache Felix",
        "Victoria Metrics",
        "VictoriaMetrics",
        "Julia",
        "Hudson",
        "React Native",
        "Ruby on Rails",
        "Site Reliability Engineering",
    ],
)
def test_recall_fixtures_are_never_rejected(name: str) -> None:
    """The full recall fixture list this phase must round-trip — none of
    these (vocab or non-vocab) are ever shape-rejected."""
    assert skills_graph.reject_reason_for_skill_name(name) is None


def test_unresolved_skill_name_error_is_a_runtime_error() -> None:
    """F6 — the exception projection callbacks raise for a name that was
    never resolved at all (a caller bug, distinct from a legitimate
    None-valued PII rejection)."""
    assert issubclass(skills_graph.UnresolvedSkillNameError, RuntimeError)


# ── auto-merge path (score >= AUTO_MERGE_THRESHOLD) ───────────────────────


@pytest.mark.asyncio
async def test_auto_merge_path_merges_without_asking_the_llm() -> None:
    """F1 (security re-audit round 2): auto-merge is enrichment-only — it
    still writes an alias onto the near-matched "python" node (below), but
    the KEY RETURNED to the caller must be the PURE
    ``_canonical_key_for_normalised("py3")`` (a hash — "py3" is not itself a
    listed vocab alias), never "python" (the near candidate's own key). Prior
    to this fix, this test asserted ``resolved == {"py3": "python"}`` — the
    exact divergence bug F1 describes: a JD requiring "py3" and a résumé
    having "py3" would have landed on DIFFERENT Skill nodes, since the résumé
    side (``resume_skill_canonical_key``) always computes the pure key.
    """
    session = _make_session(
        [
            _FakeResult([]),  # exact match: miss
            _FakeResult([{"name": "python", "aliases": ["py"], "score": 0.95}]),
            _FakeResult([]),  # alias-update write (enrichment onto "python")
            _FakeResult([]),  # ensure-node write (the pure key, always last)
        ]
    )
    llm = _make_llm()
    embedder = _make_embedder()

    resolved = await skills_graph.resolve_canonical_names(
        session, ["py3"], llm=llm, embedder=embedder
    )

    expected_key = skills_graph._canonical_key_for_normalised("py3")
    assert resolved == {"py3": expected_key}
    assert resolved["py3"] != "python", (
        "auto-merge redirected the returned key to the near candidate's own "
        "key instead of the pure canonical_key_for_normalised() result (F1)"
    )
    llm.chat_json.assert_not_awaited()
    embedder.embed.assert_awaited_once()
    assert session.run.await_count == 4


@pytest.mark.asyncio
async def test_auto_merge_path_writes_an_alias_update_for_the_new_spelling() -> None:
    """The enrichment side effect is still real: the near-matched node
    ("python") gains "py3" as an alias, even though (per F1, above) that
    match no longer decides the returned key."""
    session = _make_session(
        [
            _FakeResult([]),
            _FakeResult([{"name": "python", "aliases": [], "score": 0.99}]),
            _FakeResult([]),
            _FakeResult([]),  # ensure-node write (the pure key)
        ]
    )
    await skills_graph.resolve_canonical_names(
        session, ["py3"], llm=_make_llm(), embedder=_make_embedder()
    )
    _cypher, kwargs = _run_calls(session)[2]
    assert kwargs.get("c") == "python" or "python" in kwargs.values()
    assert "py3" in kwargs.values()


@pytest.mark.asyncio
async def test_auto_merge_never_redirects_the_key_even_with_a_graph_learned_alias() -> (
    None
):
    """F1's own round-trip guard, at the unit level: a near-candidate scoring
    ABOVE the auto-merge threshold, where that candidate ALSO already carries
    a graph-learned alias (simulating a pre-fix-poisoned graph, or this
    function's own enrichment from a previous call), must still never
    redirect the returned key — job-side and résumé-side must agree.

    Mutation-kill: reverting the auto-merge branch to
    ``return str(near[0]["name"])`` (the pre-fix behaviour) turns this RED.
    """
    session = _make_session(
        [
            _FakeResult([]),  # exact match: miss
            _FakeResult(
                [
                    {
                        "name": "react",
                        "aliases": ["reactjs", "react native"],
                        "score": 0.95,
                    }
                ]
            ),
            _FakeResult([]),  # alias-update write (enrichment onto "react")
            _FakeResult([]),  # ensure-node write (the pure key)
        ]
    )
    job_side = await skills_graph.resolve_canonical_names(
        session, ["React Native"], llm=_make_llm(), embedder=_make_embedder()
    )
    resume_side = skills_graph.resume_skill_canonical_key("React Native")

    assert job_side["React Native"] == resume_side
    assert job_side["React Native"] != "react"


# ── grey-zone LLM tiebreaker path ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_grey_zone_asks_the_llm_and_merges_on_a_valid_match() -> None:
    # NOTE (round-4 recall regression fix): this was briefly renamed to the
    # single bare token "postgresvariant" during round 3, when the (buggy)
    # two-way name-shape+vocab-miss conjunction would have incorrectly
    # shape-rejected the two-lowercase-word phrase "postgres db" before ever
    # reaching the mechanics this test exists to exercise (grey-zone
    # vector-score -> LLM-tiebreaker MECHANICS, not the PII shape guard).
    # Decision C's personal-name-lexicon arm fixed that: neither "postgres"
    # nor "db" is a personal name, so this phrase is correctly KEPT again —
    # restored to the original, more realistic placeholder.
    # F1 (security re-audit round 2): a VALID LLM-confirmed match is still
    # enrichment-only — "postgres db" is not itself a vocab alias, so its
    # pure key is a hash, never "postgresql" (the confirmed match's own key).
    # Prior to this fix, this test asserted
    # ``resolved == {"postgres db": "postgresql"}`` — the exact divergence
    # bug F1 describes.
    near = [{"name": "postgresql", "aliases": ["postgres"], "score": 0.90}]
    session = _make_session(
        [
            _FakeResult([]),
            _FakeResult(near),
            _FakeResult([]),  # alias-update write (enrichment onto "postgresql")
            _FakeResult([]),  # ensure-node write (the pure key)
        ]
    )
    llm = _make_llm(match="postgresql")
    resolved = await skills_graph.resolve_canonical_names(
        session, ["postgres db"], llm=llm, embedder=_make_embedder()
    )
    expected_key = skills_graph._canonical_key_for_normalised("postgres db")
    assert resolved == {"postgres db": expected_key}
    assert resolved["postgres db"] != "postgresql"
    llm.chat_json.assert_awaited_once()


@pytest.mark.asyncio
async def test_grey_zone_llm_receives_the_near_candidates() -> None:
    near = [{"name": "postgresql", "aliases": ["postgres"], "score": 0.90}]
    session = _make_session(
        [_FakeResult([]), _FakeResult(near), _FakeResult([]), _FakeResult([])]
    )
    llm = _make_llm(match="postgresql")
    await skills_graph.resolve_canonical_names(
        session, ["postgres db"], llm=llm, embedder=_make_embedder()
    )
    flat_args = [
        *llm.chat_json.await_args.args,
        *llm.chat_json.await_args.kwargs.values(),
    ]
    assert any("postgresql" in str(a) for a in flat_args)


@pytest.mark.asyncio
async def test_llm_tiebreak_never_redirects_the_key_job_and_resume_agree() -> None:
    """F1's round-trip guard for the LLM-tiebreak branch specifically.

    Mutation-kill: reverting that branch to ``return match`` (the pre-fix
    behaviour) turns this RED.
    """
    near = [{"name": "postgresql", "aliases": ["postgres"], "score": 0.90}]
    session = _make_session(
        [_FakeResult([]), _FakeResult(near), _FakeResult([]), _FakeResult([])]
    )
    llm = _make_llm(match="postgresql")
    job_side = await skills_graph.resolve_canonical_names(
        session, ["postgres db"], llm=llm, embedder=_make_embedder()
    )
    resume_side = skills_graph.resume_skill_canonical_key("postgres db")
    assert job_side["postgres db"] == resume_side
    assert job_side["postgres db"] != "postgresql"


@pytest.mark.asyncio
async def test_grey_zone_llm_says_no_match_falls_through_to_create_new() -> None:
    near = [{"name": "postgresql", "aliases": [], "score": 0.90}]
    session = _make_session(
        [
            _FakeResult([]),
            _FakeResult(near),
            _FakeResult([]),  # create-new write
        ]
    )
    llm = _make_llm(match=None)
    resolved = await skills_graph.resolve_canonical_names(
        session, ["cassandra"], llm=llm, embedder=_make_embedder()
    )
    assert resolved["cassandra"] != "postgresql"


@pytest.mark.asyncio
async def test_llm_tiebreaker_failure_is_non_fatal_and_treated_as_no_match() -> None:
    """Mirrors hris: a raising ``chat_json`` must not crash skill resolution
    — the résumé/JD parse it's attached to must still complete."""
    near = [{"name": "postgresql", "aliases": [], "score": 0.90}]
    session = _make_session([_FakeResult([]), _FakeResult(near), _FakeResult([])])
    llm = _make_llm(raises=True)
    resolved = await skills_graph.resolve_canonical_names(
        session, ["cassandra"], llm=llm, embedder=_make_embedder()
    )
    assert "cassandra" in resolved  # did not raise


# ── R5: hallucinated tiebreaker answer must not vanish the edge ──────────


@pytest.mark.asyncio
async def test_hallucinated_tiebreaker_answer_is_rejected_not_trusted() -> None:
    """R5 (HIGH). hris's Cypher ``MATCH (s:Skill {canonical_name: $c})``
    against a hallucinated ``$c`` is a silent no-op: no error, no edge, the
    skill just vanishes. Here, an answer that is NOT one of the offered
    ``near`` candidates is rejected outright and treated as 'create new' —
    the hallucinated string must never be used as a canonical_name to MATCH
    against, only a real (offered-or-freshly-created) name may be returned.

    NOTE (round-4 recall regression fix): this was briefly renamed to the
    single bare token "cockroachvariant" during round 3, when the (buggy)
    two-way name-shape+vocab-miss conjunction would have incorrectly
    shape-rejected the two-lowercase-word phrase "cockroach db" before ever
    reaching the mechanics this test exists to exercise (the
    hallucinated-tiebreaker-answer mechanics). Decision C's personal-name-
    lexicon arm fixed that: neither "cockroach" nor "db" is a personal
    name, so this phrase is correctly KEPT again — restored to the
    original, more realistic placeholder."""
    near = [{"name": "postgresql", "aliases": [], "score": 0.90}]
    session = _make_session(
        [
            _FakeResult([]),
            _FakeResult(near),
            _FakeResult([]),  # create-new write, NOT an alias-update on 'postgresql'
        ]
    )
    llm = _make_llm(match="not-a-real-skill-node")

    resolved = await skills_graph.resolve_canonical_names(
        session, ["cockroach db"], llm=llm, embedder=_make_embedder()
    )

    canonical = resolved["cockroach db"]
    assert canonical != "not-a-real-skill-node"
    # The hallucinated string must never appear as a Cypher parameter value —
    # proof it was never used to MATCH an existing (nonexistent) node.
    for _cypher, kwargs in _run_calls(session):
        assert "not-a-real-skill-node" not in kwargs.values()
    # A real node backing `canonical` was actually created (the create-new
    # write, not a no-op) — so a caller's subsequent HAS_SKILL/REQUIRES edge
    # write against `canonical` will succeed, not silently vanish.
    create_cypher, _ = _run_calls(session)[-1]
    assert re.search(r"MERGE.*Skill", create_cypher, re.IGNORECASE | re.DOTALL)
    assert "ON CREATE" in create_cypher.upper()


# ── create-new path (no near candidates at all) ───────────────────────────


@pytest.mark.asyncio
async def test_create_new_path_when_no_near_candidates_exist() -> None:
    session = _make_session(
        [_FakeResult([]), _FakeResult([]), _FakeResult([])]  # exact, vector, create
    )
    embedder = _make_embedder()
    llm = _make_llm()

    resolved = await skills_graph.resolve_canonical_names(
        session, ["rust"], llm=llm, embedder=embedder
    )

    assert resolved["rust"] == "rust"
    llm.chat_json.assert_not_awaited()
    embedder.embed.assert_awaited_once()
    create_cypher, _ = _run_calls(session)[-1]
    assert re.search(r"MERGE.*Skill", create_cypher, re.IGNORECASE | re.DOTALL)
    assert "ON CREATE" in create_cypher.upper()


# ── S8 (security re-audit round 3): ACCEPTED names must never be logged ───
# verbatim either -- F8 was previously only half-closed (rejected names were
# category-only, but an accepted auto-merge/LLM-merge/create-new line still
# printed `canonical=%s` in full).


@pytest.mark.asyncio
async def test_created_skill_acceptance_is_not_logged_verbatim(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`cockroachdb` is a bare single lowercase token (never folded — see
    S1's single-bare-token carve-out) and not in the 220-term vocabulary, so
    it is neither shape-rejected nor vocab-known: it sails through to the
    create-new path, exercising the ACCEPTED-name log line."""
    caplog.set_level(logging.DEBUG)
    assert skills_graph.reject_reason_for_skill_name("cockroachdb") is None
    session = _make_session([_FakeResult([]), _FakeResult([]), _FakeResult([])])
    await skills_graph.resolve_canonical_names(
        session, ["cockroachdb"], llm=_make_llm(), embedder=_make_embedder()
    )
    all_text = "\n".join(r.getMessage() for r in caplog.records)
    assert "cockroachdb" not in all_text.lower()
    assert "created" in all_text


@pytest.mark.asyncio
async def test_auto_merged_skill_acceptance_is_not_logged_verbatim(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    near = [{"name": "cockroachdb", "aliases": [], "score": 0.95}]
    session = _make_session(
        [_FakeResult([]), _FakeResult(near), _FakeResult([]), _FakeResult([])]
    )
    await skills_graph.resolve_canonical_names(
        session, ["crdb"], llm=_make_llm(), embedder=_make_embedder()
    )
    all_text = "\n".join(r.getMessage() for r in caplog.records)
    assert "cockroachdb" not in all_text.lower()
    assert "auto_merged" in all_text


# ── thresholds are settings, not hard-coded literals ──────────────────────


@pytest.mark.asyncio
async def test_auto_merge_threshold_is_read_from_settings_not_hardcoded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A score of 0.95 auto-merges under hris's hard-coded 0.92 default —
    but if ``skill_auto_merge_threshold`` is genuinely READ from settings
    (not baked in as a literal), raising it to 0.99 must route the SAME
    score through the LLM tiebreaker instead."""
    from src.settings import Settings

    raised = Settings(skill_auto_merge_threshold=0.99, skill_tiebreaker_threshold=0.88)
    monkeypatch.setattr(skills_graph, "get_settings", lambda: raised)

    near = [{"name": "python", "aliases": [], "score": 0.95}]
    session = _make_session(
        [_FakeResult([]), _FakeResult(near), _FakeResult([]), _FakeResult([])]
    )
    llm = _make_llm(match="python")

    await skills_graph.resolve_canonical_names(
        session, ["py3"], llm=llm, embedder=_make_embedder()
    )
    llm.chat_json.assert_awaited_once()


@pytest.mark.asyncio
async def test_tiebreaker_threshold_is_read_from_settings_not_hardcoded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The vector query's own lower bound (``WHERE score > tiebreaker
    threshold``) must also come from settings: lowering it must widen what
    counts as a 'near' candidate passed to the query, which we observe
    through the parameter actually sent to Neo4j."""
    from src.settings import Settings

    lowered = Settings(skill_auto_merge_threshold=0.92, skill_tiebreaker_threshold=0.10)
    monkeypatch.setattr(skills_graph, "get_settings", lambda: lowered)

    session = _make_session([_FakeResult([]), _FakeResult([]), _FakeResult([])])
    await skills_graph.resolve_canonical_names(
        session, ["rust"], llm=_make_llm(), embedder=_make_embedder()
    )
    _cypher, vector_kwargs = _run_calls(session)[1]
    assert 0.10 in vector_kwargs.values()


# ── categories_for / ensure_categories ────────────────────────────────────


def test_categories_for_a_seeded_skill_is_non_empty() -> None:
    """R4 (HIGH). ``categories.yaml`` must ship at
    ``core/src/pipeline/skill_data/categories.yaml`` (beside the existing
    ``aliases.yaml``) — if the file is missing, ``categories_for`` degrades
    gracefully to ``[]`` (see the next test), which is EXACTLY the failure
    mode this test catches: with the real shipped path, a well-known skill
    like 'python' must resolve to a non-empty family list, or every Skill
    node's ``.categories`` stays empty and 4c's ontology partial-credit is
    dead on arrival."""
    assert skills_graph.categories_for("python") != []


def test_categories_for_an_unseeded_skill_is_empty() -> None:
    assert skills_graph.categories_for("a-totally-unseeded-made-up-skill-xyz") == []


def test_categories_for_missing_yaml_file_degrades_to_empty_not_a_crash(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """R4 mutation target: point the categories path at a nonexistent file —
    ``categories_for`` must degrade gracefully (never raise), matching the
    aliases.yaml precedent in ``src/pipeline/skills.py``."""
    skills_graph._category_table.cache_clear()  # type: ignore[attr-defined]
    monkeypatch.setattr(skills_graph, "_CATEGORIES_PATH", tmp_path / "nonexistent.yaml")
    try:
        assert skills_graph.categories_for("python") == []
    finally:
        skills_graph._category_table.cache_clear()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_ensure_categories_stamps_curated_families(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(skills_graph, "categories_for", lambda c: ["backend", "data"])
    tx = MagicMock(run=AsyncMock(return_value=_FakeResult([])))

    await skills_graph.ensure_categories(tx, "python")

    tx.run.assert_awaited_once()
    cypher, kwargs = tx.run.await_args.args[0], tx.run.await_args.kwargs
    assert "SET" in cypher.upper() and "categories" in cypher
    assert (
        kwargs.get("cats") == ["backend", "data"]
        or [
            "backend",
            "data",
        ]
        in kwargs.values()
    )


@pytest.mark.asyncio
async def test_ensure_categories_is_a_noop_when_uncurated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(skills_graph, "categories_for", lambda c: [])
    tx = MagicMock(run=AsyncMock(return_value=_FakeResult([])))

    await skills_graph.ensure_categories(tx, "some-uncurated-skill")

    tx.run.assert_not_awaited()


# ── R6: alias updates must be dedupe-safe Cypher ──────────────────────────


def _alias_update_cypher_from_auto_merge() -> str:
    return (
        "MATCH (s:Skill {canonical_key: $c}) "
        "SET s.aliases = CASE WHEN $alias IN coalesce(s.aliases, []) "
        "THEN coalesce(s.aliases, []) "
        "ELSE coalesce(s.aliases, []) + [$alias] END"
    )


@pytest.mark.asyncio
async def test_alias_update_cypher_is_dedupe_safe_not_the_naive_hris_pattern() -> None:
    """R6 (MED). hris: ``SET s.aliases = coalesce(s.aliases, []) + [$alias]``
    — re-running the SAME alias on a re-parse appends a duplicate every time,
    unbounded. The Cypher this module issues for BOTH the auto-merge and the
    LLM-merge alias update must guard against re-adding an alias already
    present, so a full behavioural round-trip against a real Neo4j (the
    integration idempotency test) sees no duplicates after draining twice."""
    session = _make_session(
        [
            _FakeResult([]),
            _FakeResult([{"name": "python", "aliases": ["py3"], "score": 0.95}]),
            _FakeResult([]),
            _FakeResult([]),  # ensure-node write (the pure key)
        ]
    )
    await skills_graph.resolve_canonical_names(
        session, ["py3"], llm=_make_llm(), embedder=_make_embedder()
    )
    alias_cypher, _ = _run_calls(session)[2]
    naive = "SET s.aliases = coalesce(s.aliases, []) + [$alias]"
    assert re.sub(r"\s+", " ", alias_cypher).strip() != naive, (
        "the alias-update Cypher is the naive hris pattern verbatim — it "
        "will accumulate duplicate aliases forever across re-parses (R6)"
    )
    assert re.search(r"CASE\s+WHEN", alias_cypher, re.IGNORECASE), (
        "expected a dedupe guard (e.g. a CASE WHEN ... IN ... check) in the "
        "alias-update Cypher"
    )


# ── _ask_llm_tiebreaker (pure-ish unit, exercised indirectly above too) ───


@pytest.mark.asyncio
async def test_ask_llm_tiebreaker_returns_the_match_field() -> None:
    llm = _make_llm(match="python")
    result = await skills_graph._ask_llm_tiebreaker(
        llm, "py3", [{"name": "python", "aliases": []}]
    )
    assert result == "python"


@pytest.mark.asyncio
async def test_ask_llm_tiebreaker_returns_none_when_llm_raises() -> None:
    llm = _make_llm(raises=True)
    result = await skills_graph._ask_llm_tiebreaker(
        llm, "py3", [{"name": "python", "aliases": []}]
    )
    assert result is None


# ── resolve_canonical_names never opens a managed transaction ────────────


@pytest.mark.asyncio
async def test_resolution_never_calls_execute_write_or_execute_read() -> None:
    """Decision 3, stated directly (every other test in this file also
    enforces it implicitly via ``_managed_tx_forbidden`` — this test just
    names the invariant explicitly for the reader)."""
    session = _make_session([_FakeResult([]), _FakeResult([]), _FakeResult([])])
    await skills_graph.resolve_canonical_names(
        session, ["rust"], llm=_make_llm(), embedder=_make_embedder()
    )
    session.execute_write.assert_not_awaited()
    session.execute_read.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolve_canonical_names_accepts_any_iterable_of_names() -> None:
    """``names`` is typed ``Iterable[str]`` (not ``list[str]``) — pin that a
    one-shot generator and a ``set`` both actually resolve correctly, not
    just that the function is ``callable`` (that assertion is true of any
    object and proves nothing about the parameter type)."""
    session = _make_session(
        [_FakeResult([{"name": "python"}]), _FakeResult([{"name": "rust"}])]
    )
    generator_input = (n for n in ["python", "rust"])
    resolved = await skills_graph.resolve_canonical_names(
        session, generator_input, llm=_make_llm(), embedder=_make_embedder()
    )
    assert resolved == {"python": "python", "rust": "rust"}

    session2 = _make_session(
        [_FakeResult([{"name": "python"}]), _FakeResult([{"name": "rust"}])]
    )
    set_input = {"python", "rust"}
    resolved2 = await skills_graph.resolve_canonical_names(
        session2, set_input, llm=_make_llm(), embedder=_make_embedder()
    )
    assert resolved2 == {"python": "python", "rust": "rust"}


def test_resolve_canonical_names_with_empty_input_returns_empty_dict() -> None:
    import asyncio

    session = _make_session([])

    async def _run() -> dict[str, str]:
        return await skills_graph.resolve_canonical_names(
            session, [], llm=_make_llm(), embedder=_make_embedder()
        )

    resolved = asyncio.run(_run())
    assert resolved == {}
    session.run.assert_not_awaited()


# ═══════════════════════════════════════════════════════════════════════════
# ADR-008 — canonical-key hashing: the actual privacy control.
#
# The Skill node's MERGE key is either the vocab canonical term (cleartext —
# deniable/ambiguous, not PII-free by definition: a name colliding with a
# vocab term like "julia"/"hudson" still lands cleartext, see ADR-008
# residual #13) or, for a non-vocab name, a salted hash (``h:`` +
# sha256(salt + normalised)[:32]).
# ═══════════════════════════════════════════════════════════════════════════


def test_vocab_term_canonical_key_is_cleartext() -> None:
    assert skills_graph._canonical_key_for_normalised("python") == "python"
    assert skills_graph._canonical_key_for_normalised("kafka") == "kafka"


def test_non_vocab_term_canonical_key_is_an_opaque_hash() -> None:
    key = skills_graph._canonical_key_for_normalised("casey rivera")
    assert key.startswith(skills_graph._HASH_KEY_PREFIX)
    assert "casey" not in key
    assert "rivera" not in key
    # sha256 hex digest, truncated — never the raw input, never reversible
    # by inspection.
    hex_part = key[len(skills_graph._HASH_KEY_PREFIX) :]
    assert all(c in "0123456789abcdef" for c in hex_part)


def test_hash_key_is_stable_for_the_same_input_and_salt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Salt stability: the SAME normalised string, under the SAME salt, must
    always hash to the SAME key — otherwise a re-parse would disconnect
    every existing REQUIRES/HAS_SKILL edge from its Skill node."""
    from src.settings import Settings

    stable = Settings(skill_hash_salt="a-stable-test-salt")
    monkeypatch.setattr(skills_graph, "get_settings", lambda: stable)

    first = skills_graph._hash_key("casey rivera")
    second = skills_graph._hash_key("casey rivera")
    assert first == second


def test_hash_key_differs_across_different_salts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rotating the salt changes every non-vocab skill's key — documented in
    ``settings.skill_hash_salt``'s docstring as requiring a full
    re-projection of the graph. Pinned here as the flip side of the
    stability test above."""
    from src.settings import Settings

    monkeypatch.setattr(
        skills_graph, "get_settings", lambda: Settings(skill_hash_salt="salt-one")
    )
    first = skills_graph._hash_key("casey rivera")
    monkeypatch.setattr(
        skills_graph, "get_settings", lambda: Settings(skill_hash_salt="salt-two")
    )
    second = skills_graph._hash_key("casey rivera")
    assert first != second


def test_hash_key_empty_salt_fails_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unsalted hash is dictionary-attackable (precompute hashes of
    common names/phrases, confirm one is present in the graph) — this must
    fail loud, never silently hash with an empty salt, mirroring
    ``src.worker.main.startup``'s ``PII_KEY`` check."""
    from src.settings import Settings

    monkeypatch.setattr(
        skills_graph, "get_settings", lambda: Settings(skill_hash_salt="")
    )
    with pytest.raises(RuntimeError):
        skills_graph._hash_key("casey rivera")


# ═══════════════════════════════════════════════════════════════════════════
# ADR-008 — ``resume_skill_canonical_key``: the résumé side's ENTIRE skill
# resolution. Pure — no session, no llm, no embedder parameter exists at all.
# ═══════════════════════════════════════════════════════════════════════════


def test_resume_skill_canonical_key_is_a_pure_function_no_io_parameters() -> None:
    import inspect

    params = set(inspect.signature(skills_graph.resume_skill_canonical_key).parameters)
    assert not params & {"session", "llm", "embedder"}


def test_resume_skill_canonical_key_vocab_hit_is_cleartext() -> None:
    assert skills_graph.resume_skill_canonical_key("Python") == "python"
    assert skills_graph.resume_skill_canonical_key("Kafka") == "kafka"


# Every one of these 18+ PII shapes must produce EITHER `None` (the cheap
# email/phone/length reject still catches the two contact-info rows) OR an
# opaque `h:<hash>` key — NEVER the cleartext name/a recognisable fragment of
# it, and NEVER anything that would cause an embed() call (this function
# takes no embedder at all — see the signature test above).
PII_SHAPES: tuple[str, ...] = (
    "Casey Rivera",
    "RIVERA, CASEY",
    "Sean McDonald",
    "Torbjorn Kvistad",
    "Ludovica Brambilla",
    "Anouk Vandenberghe",
    "Siobhan O'Callaghan",
    "John Fotheringay",
    "Casey Zzyzx",
    "Dr. Casey Rivera",
    "Casey Rivera (referee)",
    "IBM John Smith",
    "Google Кейси Ривера",
    "李伟",
    "casey.rivera@example.test",
    "555-0101",
    "+1 (604) 555-0101",
    "casey.rivera (at) example.test",
)


@pytest.mark.parametrize("name", PII_SHAPES)
def test_resume_skill_canonical_key_never_leaks_cleartext_for_any_pii_shape(
    name: str,
) -> None:
    key = skills_graph.resume_skill_canonical_key(name)
    if key is None:
        return  # email/phone/length shape reject — nothing projected at all
    assert key.startswith(skills_graph._HASH_KEY_PREFIX), (
        f"{name!r} resolved to a NON-hash canonical_key {key!r} — a PII "
        "shape must never land on a cleartext vocab key"
    )
    # Every real-word fragment of the input must be absent from the key,
    # case-insensitively — the whole point of hashing.
    for fragment in re.split(r"[^\w]+", name.lower()):
        if len(fragment) < 3:  # "李"/"伟" are single chars; short tokens too noisy
            continue
        assert fragment not in key.lower(), (
            f"fragment {fragment!r} of {name!r} survived into canonical_key " f"{key!r}"
        )


@pytest.mark.parametrize("name", PII_SHAPES)
def test_resume_skill_canonical_key_is_deterministic(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same input, same (test-session) salt -> same key every time — a
    re-parse of the SAME résumé must not disconnect its own HAS_SKILL edges."""
    from src.settings import Settings

    stable = Settings(skill_hash_salt="a-stable-test-salt")
    monkeypatch.setattr(skills_graph, "get_settings", lambda: stable)
    assert skills_graph.resume_skill_canonical_key(
        name
    ) == skills_graph.resume_skill_canonical_key(name)


# ═══════════════════════════════════════════════════════════════════════════
# ADR-008 — recall round trip: a JD requiring a skill and a résumé having the
# SAME skill (by exact normalised string) must land on the SAME canonical
# key, whether that key is a vocab cleartext term or a hash. This is the
# test that proves hashing didn't break matching.
# ═══════════════════════════════════════════════════════════════════════════

RECALL_FIXTURES: tuple[str, ...] = (
    "Amazon Aurora",
    "IBM Watson",
    "Apache Felix",
    "Julia",
    "VictoriaMetrics",
    "Hudson",
    "distributed systems",
    "data engineering",
    "natural language processing",
    "event driven architecture",
    "test driven development",
    "React Native",
    "Ruby on Rails",
    "Site Reliability Engineering",
)


@pytest.mark.parametrize("name", RECALL_FIXTURES)
@pytest.mark.asyncio
async def test_recall_fixture_round_trips_job_side_and_resume_side_to_the_same_key(
    name: str,
) -> None:
    """The JD side (``resolve_canonical_names``, via a fresh/empty graph —
    no near candidate, so it takes the create-new path) and the résumé side
    (``resume_skill_canonical_key``, pure) must agree on the canonical key
    for the identical skill string — proving a JD requirement and a résumé's
    HAS_SKILL edge actually meet at the same Skill node."""
    session = _make_session(
        [_FakeResult([]), _FakeResult([]), _FakeResult([])]  # exact, vector, create
    )
    job_side = await skills_graph.resolve_canonical_names(
        session, [name], llm=_make_llm(), embedder=_make_embedder()
    )
    resume_side = skills_graph.resume_skill_canonical_key(name)
    assert job_side[name] == resume_side
    assert job_side[name] is not None


@pytest.mark.parametrize("name", RECALL_FIXTURES)
def test_recall_fixture_casing_invariant_on_the_resume_side(name: str) -> None:
    """Recall must not depend on the LLM's arbitrary capitalisation of a
    skill name — every casing of a recall fixture must resolve to the SAME
    canonical key on the résumé side."""
    r = skills_graph.resume_skill_canonical_key
    keys = {r(name), r(name.title()), r(name.lower()), r(name.upper())}
    assert len(keys) == 1, f"casing-dependent canonical_key for {name!r}: {keys}"


# ═══════════════════════════════════════════════════════════════════════════
# Phase 4b spelling-recall fix — ranking-evals measured spelling recall
# against realistic résumé wording at 37.5% (15/40); ADR-008 demoted vector
# auto-merge out of the scoring path, so `_basic_normalise` + the alias
# table ARE the entire matching surface now. Every fixture below is a real
# spelling variant that must resolve to the SAME vocab canonical
# (`canonical_key` is CLEARTEXT for a vocab hit — see
# `test_vocab_term_canonical_key_is_cleartext` above) as the plain spelling.
#
# Mutation-kill: reverting the trailing-version-strip / parenthetical-split
# logic in `_basic_normalise` turns every one of these RED — each variant
# would either stay itself (an opaque hash, not the vocab canonical) or, for
# the parenthetical cases, fold into the OLD paren-merged phrase (also not
# in the alias table, also an opaque hash) — never the cleartext canonical
# asserted here.
# ═══════════════════════════════════════════════════════════════════════════

SPELLING_RECALL_FIXTURES: tuple[tuple[str, str], ...] = (
    ("Python 3", "python"),
    ("Python (3.11)", "python"),
    ("PostgreSQL 14", "postgresql"),
    ("Postgres 15", "postgresql"),
    ("Airflow 2", "airflow"),
    ("Apache Airflow 2.7", "airflow"),
    ("Kubernetes (EKS)", "kubernetes"),
    ("Terraform (IaC)", "terraform"),
    ("AWS MWAA (Airflow)", "airflow"),
    ("Containerization (Docker)", "docker"),
    ("PSQL", "postgresql"),
    ("Docker Compose", "docker"),
    ("Kafka Streams", "kafka"),
    ("REST APIs", "rest api design"),
    ("RESTful APIs", "rest api design"),
    ("REST API", "rest api design"),
)


@pytest.mark.parametrize("variant,expected_canonical", SPELLING_RECALL_FIXTURES)
def test_spelling_variant_resume_side_resolves_to_the_vocab_canonical(
    variant: str, expected_canonical: str
) -> None:
    """The résumé side (pure, no I/O) must resolve straight to the
    CLEARTEXT vocab canonical — not a hash — proving the variant is
    recognised as the vocab hit it is, not treated as an unlisted skill."""
    assert skills_graph.resume_skill_canonical_key(variant) == expected_canonical


@pytest.mark.parametrize("variant,expected_canonical", SPELLING_RECALL_FIXTURES)
@pytest.mark.asyncio
async def test_spelling_variant_round_trips_job_side_and_resume_side_to_the_same_key(
    variant: str, expected_canonical: str
) -> None:
    """The critical invariant (security re-audit, mutation-killed): both
    sides call `_canonical_key_for_normalised` on the SAME normalised
    string, so a JD requiring the variant spelling and a résumé stating the
    plain canonical spelling still land on the same Skill node. A vocab hit
    exists already (this fixture's own canonical, seeded by a prior JD
    parse), so the job side takes the exact-match fast path."""
    session = _make_session([_FakeResult([{"name": expected_canonical}])])
    job_side = await skills_graph.resolve_canonical_names(
        session, [variant], llm=_make_llm(), embedder=_make_embedder()
    )
    resume_side = skills_graph.resume_skill_canonical_key(variant)
    assert job_side[variant] == resume_side == expected_canonical


@pytest.mark.parametrize("variant,expected_canonical", SPELLING_RECALL_FIXTURES)
def test_spelling_variant_and_plain_canonical_agree_on_the_resume_side(
    variant: str, expected_canonical: str
) -> None:
    """A JD-side variant spelling and a résumé stating the plain canonical
    term directly must resolve to the identical key — the round-trip the
    human's report requires proof of, expressed purely on the résumé-side
    function (no fakes/mocking needed)."""
    r = skills_graph.resume_skill_canonical_key
    assert r(variant) == r(expected_canonical) == expected_canonical


# ═══════════════════════════════════════════════════════════════════════════
# Security fix (post-4b re-audit, LOW #2) — paren-split skill inflation.
# `_basic_normalise` is shared (imported) between this module and
# `src.pipeline.skills`, so the gate lives there — these tests confirm it
# holds on THIS side (`resume_skill_canonical_key`) too, and that the
# JD/résumé parity invariant is unbroken by the fix.
# ═══════════════════════════════════════════════════════════════════════════

PAREN_SPLIT_INFLATION_FIXTURES: tuple[str, ...] = (
    "Casey Rivera (Python)",
    "Rivera (psql)",
    "Ada Lovelace (Julia)",
    "Casey (Kafka Streams)",
)

_INFLATION_TARGET_KEYS = ("python", "postgresql", "julia", "kafka")


@pytest.mark.parametrize("name", PAREN_SPLIT_INFLATION_FIXTURES)
def test_resume_skill_canonical_key_does_not_inflate_via_parenthetical(
    name: str,
) -> None:
    """A name-bearing outer phrase must never resolve to the real skill
    named in its own parenthetical — the key must be `None` (shape-
    rejected) or an opaque hash, never one of the cleartext vocab keys the
    parenthetical would otherwise leak into scoring."""
    key = skills_graph.resume_skill_canonical_key(name)
    if key is None:
        return
    assert key not in _INFLATION_TARGET_KEYS
    assert key.startswith(skills_graph._HASH_KEY_PREFIX)


PAREN_SPLIT_LEGITIMATE_FIXTURES: tuple[tuple[str, str], ...] = (
    ("Kubernetes (EKS)", "kubernetes"),
    ("Terraform (IaC)", "terraform"),
    ("Python (3.11)", "python"),
    ("AWS MWAA (Airflow)", "airflow"),
    ("Containerization (Docker)", "docker"),
)


@pytest.mark.parametrize("variant,expected_canonical", PAREN_SPLIT_LEGITIMATE_FIXTURES)
def test_legitimate_parenthetical_still_resolves_on_the_resume_side(
    variant: str, expected_canonical: str
) -> None:
    assert skills_graph.resume_skill_canonical_key(variant) == expected_canonical


@pytest.mark.parametrize("variant,expected_canonical", PAREN_SPLIT_LEGITIMATE_FIXTURES)
@pytest.mark.asyncio
async def test_legitimate_parenthetical_round_trips_job_side_and_resume_side(
    variant: str, expected_canonical: str
) -> None:
    """JD/résumé parity confirmation: the gate must not break the round trip
    for any of the still-legitimate parenthetical spellings."""
    session = _make_session([_FakeResult([{"name": expected_canonical}])])
    job_side = await skills_graph.resolve_canonical_names(
        session, [variant], llm=_make_llm(), embedder=_make_embedder()
    )
    resume_side = skills_graph.resume_skill_canonical_key(variant)
    assert job_side[variant] == resume_side == expected_canonical


def test_every_shipped_alias_still_resolves_on_the_graph_side() -> None:
    """Zero-regression guard for the Neo4j-backed copy of the basic-
    normalisation logic (duplicated, not imported, from `src.pipeline.
    skills` — see this module's docstring): every alias in the REAL, shipped
    `aliases.yaml` must still resolve to its own documented canonical via
    `resume_skill_canonical_key`, and land on a CLEARTEXT key (never a
    hash) — proving no shipped vocab term was accidentally demoted to
    non-vocab by the new logic."""
    import yaml

    data = yaml.safe_load(skills_graph._ALIASES_PATH.read_text(encoding="utf-8")) or []
    checked = 0
    for entry in data:
        canonical = entry["canonical"].strip().lower()
        for alias in entry.get("aliases", []):
            key = skills_graph.resume_skill_canonical_key(alias)
            msg = f"alias {alias!r} no longer resolves to canonical {canonical!r}"
            assert key == canonical, msg
            checked += 1
    assert checked >= 150  # sanity: the vocab was not accidentally shrunk
