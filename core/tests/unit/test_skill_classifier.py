"""Unit tests -- ``src.pipeline.skill_classifier`` (ROADMAP A2, Phase 3.3,
slice 1). All LLM I/O mocked at the ``llm.chat_json`` boundary.

Design (ADR-044, not restated here): a skill name outside the
306-canonical vocabulary is hashed one-way at projection and can never earn
family credit. This module classifies such names into one of the 32
``categories.yaml`` families AT PARSE TIME (where the LLM is already being
called and there is no drain deadline) -- projection itself gains no LLM
call.

``src.pipeline.skill_classifier`` does not exist yet -- this whole file
fails at collection (``ModuleNotFoundError``). RED half of the TDD cycle.

── The LLM response contract this file pins ────────────────────────────────
The spec deliberately leaves the exact ``chat_json`` schema unspecified
("a pydantic model constrained to known_families()"). Since ``llm.chat_json``
is mocked wholesale below (never actually parses JSON), this file pins the
one attribute ``classify_families`` must read off whatever object
``chat_json`` resolves to: a ``.categories`` mapping of
``{skill_name: [family, ...]}`` -- mirroring the function's own public
return shape. A future implementation is free to name its *internal*
pydantic schema class anything it likes, as long as an instance of it
exposes ``.categories`` in this shape.

── Two live defects (this file's newest section, below) ────────────────────
Everything above mocks ``llm.chat_json`` wholesale, so it cannot see how the
REAL client behaves against a REAL reasoning model. A live probe against the
tailnet ``gpt-oss:20b`` found two defects invisible to the entire mocked
suite:

1. ``_CLASSIFY_MAX_TOKENS`` (1024) is too small for a reasoning model: the
   discarded thinking trace consumes the whole budget, ``content`` comes
   back empty, and the feature silently classifies 0 of 6 live-probed
   skills. At 4096, 6 of 6.
2. The prompt (``_build_messages``) never tells the model the non-matchable
   bucket (``other``/``domain``) is a PREFERRED answer for a doubtful name --
   only that an empty list is a last resort. Live, this let two nonsense
   assignments through to a MATCHABLE family (``bi``), which -- because
   family credit is transitive across the whole résumé
   (``orchestrator.py``) -- would grant false credit toward a requirement
   the candidate does not meet.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml
from annotated_types import MaxLen
from pydantic import BaseModel, ValidationError

from src.pipeline import skill_classifier, skills_graph
from src.pipeline.llm import LLMOutputInvalidError
from src.schemas.resumes import ResumeSkill
from src.settings import get_settings

# ── independent oracle for known_families() ─────────────────────────────


def _real_family_keys() -> set[str]:
    """Parses ``categories.yaml`` directly -- NOT via
    ``skills_graph._category_table()`` -- so this is a genuinely independent
    check of ``known_families()``, not a tautology against the same cache."""
    data = (
        yaml.safe_load(skills_graph._CATEGORIES_PATH.read_text(encoding="utf-8")) or {}
    )
    return {str(k).strip().lower() for k in data}


def test_known_families_matches_categories_yaml_exactly() -> None:
    assert set(skill_classifier.known_families()) == _real_family_keys()


def test_known_families_has_thirty_two_families_today() -> None:
    """A count sanity pin -- catches an accidental gutting of the taxonomy
    (or a hard-coded second copy that silently forked from the real file)."""
    assert len(skill_classifier.known_families()) == 32


def test_known_families_returns_a_tuple() -> None:
    result = skill_classifier.known_families()
    assert isinstance(result, tuple)
    assert all(isinstance(f, str) for f in result)


def test_known_families_shares_the_real_category_family_names_not_a_hardcoded_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UPDATED, EXPLICITLY, post-RED (coordinator correction on this same
    slice): this test originally monkeypatched ``skills_graph._category_table``
    and asserted ``known_families()`` tracked it. That pinned an
    implementation choice -- deriving ``known_families()`` from
    ``_category_table()`` (canonical -> [families], inverted from
    categories.yaml for per-skill lookup) -- that turned out to be unsafe:
    that table structurally cannot see a family with zero curated skill
    members (``domain``/``other``), and an earlier version of this slice
    closed that gap by giving each a fake curated member in the shipped,
    ADR-042-governed vocabulary file. That widened
    ``_canonical_key_for_normalised``'s in-vocab predicate (ANY key in
    ``_category_table()`` counts as in-vocabulary/cleartext) -- exactly the
    predicate ``unclassified_names`` above shares and must never drift wider
    than. Reverted; ``known_families()`` now reads
    ``skills_graph._category_family_names()``, a SEPARATE, purpose-built
    accessor beside ``_category_table()`` (same ``_CATEGORIES_PATH``, same
    ``@lru_cache``) that reads the file's top-level family names directly --
    a second VIEW of the same on-disk source of truth, never a second,
    independently-maintained copy of it, and never a mutation of
    ``_category_table()``'s security-relevant key space or of the shipped
    vocabulary data. This test now pins THAT rule, proven the same way as
    before: mutate the real cached accessor and confirm ``known_families()``
    tracks it."""

    def _stub_category_family_names() -> tuple[str, ...]:
        return ("probe_family_xyz",)

    monkeypatch.setattr(
        skills_graph, "_category_family_names", _stub_category_family_names
    )

    assert skill_classifier.known_families() == ("probe_family_xyz",)


# ── unclassified_names: the drift guard ──────────────────────────────────
#
# Requirement (ADR-044): "It MUST share the existing predicate
# rather than restating it: derive from `_basic_normalise` + the alias/
# category table lookups already used by `_canonical_key_for_normalised`."
# A second, independently-maintained copy of that condition is the A7 drift
# shape this repo has recorded seventeen times.


@pytest.mark.parametrize(
    ("name", "expect_unclassified"),
    [
        ("python", False),  # in-vocab canonical
        ("Py", False),  # alias.yaml alias of "python"
        ("dns", False),  # category-only: in categories.yaml, NOT aliases.yaml
        ("voip", False),  # category-only, multi-family (networking + hardware)
        ("mri methods", True),  # genuinely out of vocab -- the 45.2% case
        ("microfabrication", True),  # genuinely out of vocab
    ],
)
def test_unclassified_names_agrees_with_the_real_hashing_decision(
    name: str, expect_unclassified: bool
) -> None:
    """Cross-checked against the REAL, independent oracle
    (``skills_graph._canonical_key_for_normalised`` on the same normalised
    string) for a table spanning in-vocab, alias, category-only, and
    out-of-vocab names."""
    normalised = skills_graph._basic_normalise(name)
    would_be_hashed = skills_graph._canonical_key_for_normalised(normalised).startswith(
        skills_graph._HASH_KEY_PREFIX
    )
    assert would_be_hashed is expect_unclassified, (
        f"test table itself is wrong for {name!r} -- fix the parametrize case, "
        "not the assertion below"
    )

    result = skill_classifier.unclassified_names([name])
    assert (name in result) is expect_unclassified


def test_unclassified_names_preserves_order_and_drops_only_vocab_hits() -> None:
    names = ["python", "mri methods", "voip", "microfabrication"]
    result = skill_classifier.unclassified_names(names)
    assert result == ["mri methods", "microfabrication"]


def test_unclassified_names_empty_input_returns_empty_list() -> None:
    assert skill_classifier.unclassified_names([]) == []


def test_unclassified_names_all_vocab_returns_empty_list() -> None:
    assert skill_classifier.unclassified_names(["python", "voip", "dns"]) == []


def test_unclassified_names_drift_guard_cannot_silently_diverge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The core anti-drift proof: if ``unclassified_names`` shares the real
    predicate (rather than restating it), mutating the REAL alias table both
    it and ``_canonical_key_for_normalised`` read must flip BOTH sides'
    answer for the same probe name together -- they cannot disagree.

    Patches ``_alias_table`` on BOTH ``src.pipeline.skills`` (the single
    source of truth per ``test_skill_normalise_parity.py``) and
    ``src.pipeline.skills_graph`` (which imports the same function object by
    name, so a module-level rebind on one side does not reach the other's
    already-bound reference -- the exact footgun that file's own docstring
    documents) so this test does not care which import path
    ``unclassified_names`` happens to use.
    """
    from src.pipeline import skills

    probe = "zzz-probe-term-never-shipped-in-any-vocab-file"

    # Sanity: today, genuinely out of vocab on both sides.
    assert skills_graph._canonical_key_for_normalised(probe).startswith(
        skills_graph._HASH_KEY_PREFIX
    )
    assert probe in skill_classifier.unclassified_names([probe])

    def _stub_alias_table() -> dict[str, str]:
        return {probe: probe}

    monkeypatch.setattr(skills, "_alias_table", _stub_alias_table)
    monkeypatch.setattr(skills_graph, "_alias_table", _stub_alias_table)

    # The shared predicate now says `probe` IS vocab (cleartext).
    assert skills_graph._canonical_key_for_normalised(probe) == probe

    # If `unclassified_names` shares that predicate rather than restating
    # its own copy, it MUST agree: `probe` drops out of the unclassified set.
    assert probe not in skill_classifier.unclassified_names([probe])


# ── ADR-042: the gate-critical family-less canonicals are structurally
# unreachable by the classifier (never merely untested) ──────────────────


@pytest.mark.parametrize("canonical", ["rest api design", "c++", "hudson", "julia"])
def test_gate_critical_canonicals_are_structurally_unreachable_by_the_classifier(
    canonical: str,
) -> None:
    """ADR-042 pinned these four canonicals family-less
    (``test_skill_vocabulary_families.py::test_gate_critical_canonical_stays_familyless``,
    ~line 209) because giving any of them a family collapses a ranking-evals
    ordering pair's margin 80% while still passing. The classifier here runs
    ONLY on ``unclassified_names()`` output, so an in-vocab canonical like
    these four can never reach it -- unreachable BY CONSTRUCTION, not merely
    untested."""
    assert canonical not in skill_classifier.unclassified_names([canonical])


# ── classify_families: happy path, batched ───────────────────────────────


def _fake_llm(categories: dict[str, list[str]]) -> MagicMock:
    return MagicMock(
        chat_json=AsyncMock(return_value=SimpleNamespace(categories=categories))
    )


@pytest.mark.asyncio
async def test_classify_families_happy_path_returns_assigned_families() -> None:
    llm = _fake_llm(
        {"mri methods": ["health_wellness"], "microfabrication": ["hardware"]}
    )
    result = await skill_classifier.classify_families(
        llm, ["mri methods", "microfabrication"], settings=get_settings()
    )
    assert result == {
        "mri methods": ["health_wellness"],
        "microfabrication": ["hardware"],
    }


@pytest.mark.asyncio
async def test_classify_families_issues_exactly_one_batched_call_not_one_per_name() -> (
    None
):
    llm = _fake_llm({"a": ["backend"], "b": ["data"], "c": ["ml"]})
    await skill_classifier.classify_families(
        llm, ["a", "b", "c"], settings=get_settings()
    )
    llm.chat_json.assert_awaited_once()


@pytest.mark.asyncio
async def test_classify_families_empty_names_never_calls_the_llm() -> None:
    llm = _fake_llm({})
    result = await skill_classifier.classify_families(llm, [], settings=get_settings())
    assert result == {}
    llm.chat_json.assert_not_awaited()


# ── conservative rules ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_classify_families_drops_a_family_the_model_invents() -> None:
    """A family the model returns that is not in ``known_families()`` is
    dropped -- the valid family alongside it survives."""
    llm = _fake_llm({"skill x": ["not_a_real_family_xyz", "backend"]})
    result = await skill_classifier.classify_families(
        llm, ["skill x"], settings=get_settings()
    )
    assert result == {"skill x": ["backend"]}


@pytest.mark.asyncio
async def test_classify_families_no_confident_answer_omits_the_key_entirely() -> None:
    """An EXPLICIT empty-list answer means 'no confident family' -- the key
    must be ABSENT from the result, not present with an empty list. Absence
    is what makes this identical to today's (pre-feature) behaviour for that
    skill: ``ResumeSkill.categories`` stays at its default (``None``)."""
    llm = _fake_llm({"skill y": []})
    result = await skill_classifier.classify_families(
        llm, ["skill y"], settings=get_settings()
    )
    assert "skill y" not in result
    assert result == {}


@pytest.mark.asyncio
async def test_classify_families_name_the_model_never_addresses_is_also_omitted() -> (
    None
):
    """The model may simply not mention a name at all (not even an empty
    list) -- same conservative outcome: absent, not an empty list."""
    llm = _fake_llm({})  # model addressed nothing
    result = await skill_classifier.classify_families(
        llm, ["skill z"], settings=get_settings()
    )
    assert "skill z" not in result
    assert result == {}


@pytest.mark.asyncio
async def test_classify_families_all_invented_families_leaves_the_name_omitted() -> (
    None
):
    """Every family offered for a name is invented (unknown) -- after
    dropping them all, zero real families remain, which is the same
    "no confident answer" outcome as an explicit empty list: the key must be
    absent, not present with ``[]``."""
    llm = _fake_llm({"skill w": ["not_real_1", "not_real_2"]})
    result = await skill_classifier.classify_families(
        llm, ["skill w"], settings=get_settings()
    )
    assert "skill w" not in result


@pytest.mark.asyncio
async def test_classify_families_caps_at_two_families_per_skill() -> None:
    """Family credit is transitive across the whole résumé (any résumé
    skill in the family credits a matching requirement), so unbounded
    breadth here amplifies false credit -- capped at 2, a named constant."""
    llm = _fake_llm({"skill v": ["backend", "data", "ml"]})  # 3 real families
    result = await skill_classifier.classify_families(
        llm, ["skill v"], settings=get_settings()
    )
    assert len(result["skill v"]) == 2, (
        f"expected the per-skill family cap (<=2) to trim 3 valid families "
        f"down to 2, got {result['skill v']!r}"
    )
    assert set(result["skill v"]) <= {"backend", "data", "ml"}


@pytest.mark.asyncio
async def test_classify_families_two_valid_families_are_not_trimmed() -> None:
    """The cap must not be so aggressive it drops a legitimate SECOND
    family — exactly 2 offered, exactly 2 kept."""
    llm = _fake_llm({"skill u": ["backend", "data"]})
    result = await skill_classifier.classify_families(
        llm, ["skill u"], settings=get_settings()
    )
    assert result == {"skill u": ["backend", "data"]}


# ── A2 recurrence guard: this repo's own recurring defect shape ──────────
#
# `_MAX_SKILLS` mirroring `ResumeParsed.skills`'s `max_length` as an
# uncoupled literal became unreachable-by-construction and truncated a
# TRUSTED source (ADR-042, ROADMAP A2) -- see
# `test_worker_parse_resume.py::test_max_skills_matches_resume_parsed_skills_schema_cap`
# -- the established remedy for exactly this shape. `_MAX_FAMILIES_PER_SKILL`
# here (skill_classifier.py:60) and `ResumeSkill.categories`'s own
# `max_length` (resumes.py:155) are the SAME coupling: two literal `2`s tied
# together only by a comment naming the constant, nothing enforcing
# agreement between them.


def test_max_families_per_skill_matches_resume_skill_categories_schema_cap() -> None:
    """`ResumeSkill.categories`'s own docstring (resumes.py:150-158) says it
    is "capped at `skill_classifier._MAX_FAMILIES_PER_SKILL` (2) as a second
    line of defence" -- nothing enforced that either. A live desync (the
    classifier's cap raised while the schema's `max_length` is left behind,
    or vice versa) is not cosmetic: `classify_families` could legitimately
    assign more families than the schema's `max_length` permits, and
    `ResumeParsed.model_validate` -- called on the SAME LLM turn's résumé
    payload -- would raise `ValidationError` for an otherwise-good résumé
    (identical failure mode to the `_MAX_SKILLS` incident this guard
    mirrors)."""
    field_info = ResumeSkill.model_fields["categories"]
    max_lengths = [m.max_length for m in field_info.metadata if isinstance(m, MaxLen)]
    assert max_lengths, "ResumeSkill.categories has no MaxLen constraint at all"
    assert max_lengths[0] == skill_classifier._MAX_FAMILIES_PER_SKILL, (
        f"ResumeSkill.categories max_length={max_lengths[0]} != "
        f"_MAX_FAMILIES_PER_SKILL={skill_classifier._MAX_FAMILIES_PER_SKILL} "
        "in skill_classifier.py -- these two caps MUST stay in sync "
        "(resumes.py:150-158). A desync lets the classifier legitimately "
        "assign more families than the schema then accepts, failing "
        "ResumeParsed.model_validate for an otherwise-good résumé."
    )


# ── failure posture: best-effort, never fails the caller ─────────────────


@pytest.mark.asyncio
async def test_classify_families_llm_raises_returns_empty_dict_without_raising() -> (
    None
):
    llm = MagicMock(chat_json=AsyncMock(side_effect=RuntimeError("ollama down")))
    result = await skill_classifier.classify_families(
        llm, ["mri methods"], settings=get_settings()
    )
    assert result == {}


@pytest.mark.asyncio
async def test_classify_families_llm_output_invalid_error_is_non_fatal() -> None:
    """The REAL ``LLMClient.chat_json`` raises this (not a bare
    ``RuntimeError``) after exhausting its self-correction retries."""
    llm = MagicMock(
        chat_json=AsyncMock(side_effect=LLMOutputInvalidError("categories: list_type"))
    )
    result = await skill_classifier.classify_families(
        llm, ["mri methods"], settings=get_settings()
    )
    assert result == {}


# ── PII: no skill name in any logged or persisted string ─────────────────


class _Probe(BaseModel):
    x: int


def _leaky_validation_error(offending_value: str) -> ValidationError:
    """A REAL ``pydantic.ValidationError`` whose ``str()`` embeds
    ``offending_value`` as ``input_value=...`` -- exactly the pydantic v2
    behaviour ``validation_error_digest`` exists to neutralise. Simulates
    what an UNGUARDED validation failure inside ``classify_families`` (e.g.
    building its own per-name structured object) would leak if the skill
    name itself is embedded in the model's structured response."""
    try:
        _Probe.model_validate({"x": offending_value})
    except ValidationError as exc:
        return exc
    raise AssertionError("expected a ValidationError")  # pragma: no cover


@pytest.mark.asyncio
async def test_classify_families_never_logs_the_skill_name_on_validation_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sentinel = "SENTINEL_SKILL_NAME_9f3a2b"
    llm = MagicMock(chat_json=AsyncMock(side_effect=_leaky_validation_error(sentinel)))

    with caplog.at_level(logging.DEBUG):
        result = await skill_classifier.classify_families(
            llm, [sentinel], settings=get_settings()
        )

    assert result == {}
    assert sentinel not in caplog.text, (
        "a skill name leaked into a log line via str(ValidationError) -- "
        "must use validation_error_digest (or count-only logging) instead"
    )


@pytest.mark.asyncio
async def test_classify_families_never_raises_the_leaky_validation_error_itself(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Belt-and-braces: even if the caller's own exception handler somehow
    logged the RAISED exception's repr (not just a log message this module
    authored), the exception itself must never propagate out of
    ``classify_families`` carrying the raw skill name -- it must be caught
    and converted to the safe empty-result outcome before it ever leaves."""
    sentinel = "SENTINEL_SKILL_NAME_leaks_via_repr"
    llm = MagicMock(chat_json=AsyncMock(side_effect=_leaky_validation_error(sentinel)))

    result = await skill_classifier.classify_families(
        llm, [sentinel], settings=get_settings()
    )
    assert result == {}


# ── settings is accepted (keyword-only, per the spec'd signature) ────────


@pytest.mark.asyncio
async def test_classify_families_accepts_settings_as_keyword_only() -> None:
    import inspect

    sig = inspect.signature(skill_classifier.classify_families)
    params = sig.parameters
    assert "settings" in params
    assert params["settings"].kind == inspect.Parameter.KEYWORD_ONLY


def test_classify_families_is_async() -> None:
    import inspect

    assert inspect.iscoroutinefunction(skill_classifier.classify_families)


# ── Defect 1 (live probe against the real tailnet model, gpt-oss:20b): the
# feature is a SILENT NO-OP in production ────────────────────────────────
#
# Every test above mocks `llm.chat_json` at the boundary, so `content` is
# always whatever `SimpleNamespace(categories=...)` the test hands back --
# a MagicMock has no reasoning/thinking trace to discard, so it cannot see
# a max_tokens budget that is too small to survive one. Live, batching the
# SAME 6 out-of-vocab skill names in one `classify_families` call against
# gpt-oss:20b: at `_CLASSIFY_MAX_TOKENS=1024` (today's value) the discarded
# reasoning trace consumed the whole budget, `content` came back empty, and
# `classify_families` correctly degraded to `{}` -- 0 of 6 assigned. At
# 4096, 6 of 6. `client.py`'s own `_empty_content_message` docstring names
# this exact failure mode (ADR-021 §6) and documents that `think:false`
# does not reliably suppress it on gpt-oss:20b (client.py:71-73,
# :223-235). Compare the existing budgets this repo already ships:
# `resume_tasks.py:186` uses 1536 for FULL skill extraction (many skills,
# names + evidence + years); `skills_graph.py:443` uses 128 for a one-word
# tiebreaker. 1024 for a batched, multi-name JSON-object response is not
# obviously wrong by comparison -- it is only provably wrong against a real
# reasoning model, which is exactly why two green gates and 5,890 mocked
# tests missed it.


def test_classify_max_tokens_is_a_named_floor_a_reasoning_trace_cannot_exhaust() -> (
    None
):
    """4096 is the smallest budget the live probe actually cleared (6/6);
    1024 (today's value) cleared 0/6. Pinned as a FLOOR (`>=`), not an exact
    value, so a future increase for an unrelated reason does not need to
    touch this test -- only a regression back toward the value proven live
    to zero out the feature should fail it."""
    assert skill_classifier._CLASSIFY_MAX_TOKENS >= 4096, (
        f"_CLASSIFY_MAX_TOKENS={skill_classifier._CLASSIFY_MAX_TOKENS} -- live probe "
        "against the tailnet gpt-oss:20b assigned 0 of 6 skills at 1024 (the "
        "discarded reasoning trace ate the whole budget, content came back empty, "
        "classify_families correctly degraded to {}) and 6 of 6 at 4096. The mocked "
        "unit suite cannot see this because a MagicMock has no thinking trace to "
        "discard -- this is the ONLY reason the feature shipped as a silent no-op."
    )


@pytest.mark.asyncio
async def test_classify_families_sends_the_floor_value_to_chat_json() -> None:
    """Pins the VALUE THAT ACTUALLY REACHES ``chat_json``, not merely the
    module constant in isolation -- a fix that bumped
    ``_CLASSIFY_MAX_TOKENS`` but left a stale literal at the ``chat_json``
    call site (or a second, uncoupled budget constant) would pass the
    constant-only test above while still failing live, exactly the A7 drift
    shape this repo has recorded repeatedly for other coupled literals
    (see ``test_max_families_per_skill_matches_resume_skill_categories_schema_cap``
    above)."""
    llm = _fake_llm({"mri methods": ["health_wellness"]})
    await skill_classifier.classify_families(
        llm, ["mri methods"], settings=get_settings()
    )
    kwargs = llm.chat_json.await_args.kwargs
    assert kwargs["max_tokens"] >= 4096, (
        f"chat_json was called with max_tokens={kwargs.get('max_tokens')!r} -- below "
        "the floor a reasoning model's discarded thinking trace cannot exhaust "
        "(live: 0/6 assigned at 1024, 6/6 at 4096)"
    )
    assert kwargs["max_tokens"] == skill_classifier._CLASSIFY_MAX_TOKENS, (
        "the value passed to chat_json must BE the named constant, never a second, "
        "independently-set literal -- otherwise raising the constant alone proves "
        "nothing about what actually reaches the model"
    )


# ── Defect 2 (live probe): wrong SPECIFIC-family assignments create FALSE
# family credit, which is worse than the abstention it replaces ──────────
#
# Live, two of six probed names got a wrong SPECIFIC matchable family
# (`"expert scientific knowledge of MRI and MEG research methods" ->
# ['bi']`, `"wildlife habitat restoration planning" -> ['bi']`). `bi` is a
# MATCHABLE family and family credit is transitive across the WHOLE résumé
# (`orchestrator.py` matches ANY résumé skill in the family via
# `reqSkill.categories` / `c.categories` overlap, `NOT t IN $exclude_fam`),
# so a candidate holding any unrelated business-intelligence skill would
# earn credit toward "expert knowledge of MRI research methods" -- the
# product asserting a qualification the candidate does not have. That is
# worse than the `0.0` (no credit) it replaces. Meanwhile
# `"eligibility to practice law in British Columbia" -> ['other']` was a
# CORRECT abstention: `other` is in `settings.match_non_matchable_families`
# (settings.py:204) so it is excluded from family credit
# (`orchestrator.py`'s `NOT t IN $exclude_fam`) -- it earns no credit and
# costs nothing. The system prompt (`_build_messages`) must make declining
# via the non-matchable bucket the PREFERRED answer when a specific
# matchable family is doubtful, not merely a last-resort empty list.


def test_build_messages_prefers_non_matchable_over_a_doubtful_guess() -> None:
    """Pins the INSTRUCTION, not incidental wording: the family names
    themselves are sourced from ``settings.match_non_matchable_families``
    (never hardcoded ``"other"``/``"domain"`` literals in THIS test), and
    the assertion only requires that the prompt (a) actually names the
    non-matchable bucket as a legal answer, AND (b) tells the model to
    PREFER it over a doubtful specific matchable family -- not that any
    particular sentence appears verbatim. Today's prompt does neither: it
    only ever offers an empty list as a last resort ("If no family
    confidently applies... rather than guessing"), and never mentions the
    non-matchable family names at all, so the model has no signal that
    declining THROUGH THEM is preferable to a wrong specific guess."""
    settings = get_settings()
    non_matchable = {
        f.strip().lower()
        for f in settings.match_non_matchable_families.split(",")
        if f.strip()
    }
    assert non_matchable, "settings must configure a non-empty non-matchable set"
    assert non_matchable <= set(skill_classifier.known_families()), (
        "test precondition: the non-matchable names must themselves be assignable "
        "answers (known_families()), or the prompt could never legally offer them"
    )

    messages = skill_classifier._build_messages(
        ["probe skill"], skill_classifier.known_families()
    )
    assert messages[0]["role"] == "system"
    system = messages[0]["content"].lower()

    mentioned = {fam for fam in non_matchable if fam in system}
    assert mentioned, (
        f"system prompt never names the non-matchable bucket {sorted(non_matchable)!r} "
        "at all, so the model has no way to know those families are a legal, "
        f"PREFERRED fallback answer -- got prompt: {system!r}"
    )

    preference_markers = (
        "prefer",
        "default to",
        "rather than guess a specific",
        "before guessing",
        "over a specific",
        "safer than",
    )
    assert any(m in system for m in preference_markers), (
        "system prompt names the non-matchable bucket but never instructs the model "
        "to PREFER it over a doubtful specific matchable family -- declining must be "
        "the DEFAULT posture for a doubtful name, not a last resort reached only via "
        "an 'if no family confidently applies' escape hatch (today's wording), which "
        "is exactly the gap that let 'expert knowledge of MRI research methods' -> "
        "['bi'] and 'wildlife habitat restoration planning' -> ['bi'] both ship live "
        "instead of declining via 'other'"
    )


# ── regression guards: behaviour that is ALREADY correct today and must
# not be broken by the defect-1/defect-2 fixes above ──────────────────────


@pytest.mark.asyncio
async def test_classify_families_non_matchable_answer_survives_the_filter() -> None:
    """Verified against the real source (``classify_families`` keeps any
    ``c in known`` where ``known = set(known_families())``, and
    ``known_families()`` includes the non-matchable catch-alls per its own
    docstring: "assignable here, since assigning either grants zero family
    credit"): an ``other``/``domain`` answer ALREADY survives the
    known-families filter exactly like any matchable family. Pinned here as
    a REGRESSION GUARD, not a new requirement -- the defect-2 fix (making
    the model prefer this bucket) must not "succeed" by secretly dropping
    non-matchable answers at this filter instead of the prompt actually
    steering the model."""
    settings = get_settings()
    non_matchable = [
        f.strip() for f in settings.match_non_matchable_families.split(",") if f.strip()
    ]
    assert non_matchable, "settings must configure a non-empty non-matchable set"
    fallback = non_matchable[0]

    llm = _fake_llm({"eligibility to practice law in bc": [fallback]})
    result = await skill_classifier.classify_families(
        llm, ["eligibility to practice law in bc"], settings=get_settings()
    )
    assert result == {"eligibility to practice law in bc": [fallback]}


@pytest.mark.asyncio
async def test_classify_families_families_are_always_a_subset_of_known() -> None:
    """A doubtful answer must NEVER silently become a matchable family that
    was not actually offered by ``known_families()``: whatever the model
    returns (a mix of real matchable, real non-matchable, and invented
    names), only members of ``known_families()`` survive -- and the
    non-matchable names are themselves LEGAL members of that survivor set,
    not filtered out as a side effect of some other rule."""
    known = set(skill_classifier.known_families())
    non_matchable = {
        f.strip()
        for f in get_settings().match_non_matchable_families.split(",")
        if f.strip()
    }
    assert non_matchable <= known

    first_non_matchable = sorted(non_matchable)[:1]
    llm = _fake_llm(
        {
            "a": ["backend", "totally_made_up_family_xyz"],
            "b": first_non_matchable,
            "c": ["ml", "also_not_real"],
        }
    )
    result = await skill_classifier.classify_families(
        llm, ["a", "b", "c"], settings=get_settings()
    )
    all_returned = {fam for fams in result.values() for fam in fams}
    assert all_returned <= known, (
        f"classify_families returned families outside known_families(): "
        f"{all_returned - known!r}"
    )
    assert result["b"] == first_non_matchable


@pytest.mark.asyncio
async def test_classify_families_empty_content_degrades_with_no_name_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The specific live failure shape (Defect 1, at the OLD 1024 budget):
    the real client raises ``LLMOutputInvalidError`` with the REAL
    ``_empty_content_message`` text when a reasoning trace exhausts the
    budget. Pinned as a REGRESSION GUARD so raising ``_CLASSIFY_MAX_TOKENS``
    to fix Defect 1 cannot reopen this: if the model STILL returns empty
    content for some other reason, the caller must still degrade to ``{}``
    with no exception and no skill name in any log line -- never crash the
    résumé parse, never leak the skill name via a caught exception's own
    ``str()``."""
    from src.pipeline.llm.client import _empty_content_message

    sentinel = "mri methods"
    llm = MagicMock(
        chat_json=AsyncMock(
            side_effect=LLMOutputInvalidError(_empty_content_message(True))
        )
    )
    with caplog.at_level(logging.WARNING):
        result = await skill_classifier.classify_families(
            llm, [sentinel], settings=get_settings()
        )
    assert result == {}
    assert (
        sentinel not in caplog.text
    ), "the skill name leaked into a log line on the empty-content degrade path"
