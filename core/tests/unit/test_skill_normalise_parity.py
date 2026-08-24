"""Regression test for a Phase 4b defect (post-merge, on top of
``feat/phase-4b-graph-projection``): ``_basic_normalise`` — the function
whose output feeds ``_canonical_key_for_normalised`` on BOTH the JD/job side
(``src.pipeline.skills_graph``) and the résumé side
(``src.pipeline.skills``) — used to be defined TWICE, once per module, "kept
in sync by convention" per comments in both files. Nothing enforced that
convention, the two copies had already textually diverged (confirmed via
``inspect.getsource`` inequality before this fix), and every shipped alias/
recall fixture happened to still agree only by luck.

That is exactly the F1 hazard (security re-audit, `docs/adr` ADR-008's
predecessor rounds): if the JD side and the résumé side ever derive a
different normalised string for the same skill text, ``REQUIRES``/
``HAS_SKILL`` stop meeting at the same ``Skill`` node and that requirement
silently scores zero for every candidate who genuinely has the skill — no
exception, no log, just a quietly wrong ranking.

The fix: ``src.pipeline.skills_graph`` now IMPORTS ``_basic_normalise`` (and
``_alias_table``) from ``src.pipeline.skills`` instead of redefining them —
one function, one definition, checked here by IDENTITY (not just source
equality, which luck could still satisfy for two independently-maintained
copies).
"""

from __future__ import annotations

import importlib
import inspect

import pytest
import yaml

from src.pipeline import skills, skills_graph

# ── corpus: every shipped alias/canonical, every recall fixture, every one of
# the 18 PII shapes, and every Phase 4b spelling-recall variant + its
# canonical — the full input surface the human's report calls out by name.

_aliases_data = yaml.safe_load(skills._ALIASES_PATH.read_text(encoding="utf-8")) or []

_SHIPPED_VOCAB: list[str] = []
for _entry in _aliases_data:
    _canonical = _entry["canonical"].strip().lower()
    _SHIPPED_VOCAB.append(_canonical)
    for _alias in _entry.get("aliases", []):
        _SHIPPED_VOCAB.append(_alias)

# Mirrors `test_pipeline_skills_graph.RECALL_FIXTURES`.
_RECALL_FIXTURES: tuple[str, ...] = (
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

# Mirrors `test_pipeline_skills_graph.PII_SHAPES` — the 18-shape probe.
_PII_SHAPES: tuple[str, ...] = (
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
assert len(_PII_SHAPES) == 18

# Mirrors `SPELLING_RECALL_FIXTURES` (both `test_pipeline_skills.py` and
# `test_pipeline_skills_graph.py`) — the newly-normalised spellings, plus
# their expected canonicals (also part of the corpus: a plain canonical
# spelling must agree with itself across both sides too).
_SPELLING_RECALL_FIXTURES: tuple[tuple[str, str], ...] = (
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

CORPUS: list[str] = [
    *_SHIPPED_VOCAB,
    *_RECALL_FIXTURES,
    *_PII_SHAPES,
    *(v for variant, _ in _SPELLING_RECALL_FIXTURES for v in (variant,)),
    *(c for _, c in _SPELLING_RECALL_FIXTURES),
]

# Sanity: the corpus was not accidentally gutted (~220 shipped vocab entries
# + 14 recall fixtures + 18 PII shapes + 32 spelling-recall entries).
assert len(CORPUS) >= 260


# ── the actual dedup, checked by identity ────────────────────────────────


@pytest.fixture(autouse=True, scope="module")
def _normalise_module_state_before_parity_checks() -> None:
    """Reload ``skills`` then ``skills_graph``, in that order, before this
    file's tests run.

    Defensive against a Python footgun unrelated to whether this dedup fix
    is correct: ``test_pipeline_skills.py``'s
    ``test_match_skills_resolves_real_aliases_yaml_regardless_of_cwd`` does
    ``importlib.reload(src.pipeline.skills)`` (legitimately, to prove the
    YAML path resolves regardless of cwd) earlier in the same pytest
    session. That reload creates FRESH function objects bound to
    ``src.pipeline.skills``'s own namespace — but ``src.pipeline.
    skills_graph`` imported ``_basic_normalise``/``_alias_table`` via
    ``from src.pipeline.skills import ...`` back at ITS OWN (one-time,
    collection-time) import, so it still holds the PRE-reload objects.
    ``a is b`` would then spuriously fail — not because the dedup broke,
    but because ``from X import name`` only copies a reference at import
    time and never sees a later rebind of ``X``'s own attribute.

    Reloading both modules here, in dependency order, re-establishes a
    clean baseline immediately before every identity assertion in this
    file, so the result is independent of what did or didn't run earlier
    in the session — and the reload of ``skills_graph`` still re-executes
    its own ``from src.pipeline.skills import ...`` line, so a genuine
    future re-duplication (a ``def _basic_normalise(...)`` added back into
    ``skills_graph.py``) is still caught: reloading would bind the name to
    that new local ``def``, not to ``skills``'s object, and the identity
    check below would go RED exactly as intended.
    """
    importlib.reload(skills)
    importlib.reload(skills_graph)


def test_basic_normalise_is_a_single_shared_function_object() -> None:
    """The core of this fix: ``skills_graph`` must IMPORT
    ``skills._basic_normalise`` rather than redefine its own copy.

    Mutation-kill: re-introducing a second ``def _basic_normalise(...)`` in
    ``skills_graph.py`` — even one that starts out byte-identical to
    ``skills.py``'s copy — makes this assertion fail immediately, because
    the two would be different function objects. An ``inspect.getsource``
    equality check ALONE (which is all the human's report could verify
    before this fix) would NOT catch that regression the moment the two
    copies next diverge by even one edit to either side — identity is the
    only check that is dedup-proof by construction.
    """
    assert skills_graph._basic_normalise is skills._basic_normalise
    assert inspect.getsource(skills_graph._basic_normalise) == inspect.getsource(
        skills._basic_normalise
    )


def test_alias_table_is_also_a_single_shared_function_object() -> None:
    """``_basic_normalise`` calls ``_alias_table()`` internally — if that
    helper were ever re-duplicated too (even while ``_basic_normalise``
    itself stayed imported), the two sides could still silently diverge on
    which YAML/entries they see. Pinned by identity for the same reason as
    above."""
    assert skills_graph._alias_table is skills._alias_table


# ── the invariant itself, over the full corpus ───────────────────────────


@pytest.mark.parametrize("name", CORPUS)
def test_jd_and_resume_side_normalisation_agree(name: str) -> None:
    """The JD side (``skills_graph``, feeding ``_canonical_key_for_normalised``
    on the job/JD projection path) and the résumé side (``skills``, feeding
    ``canonicalize_skill_names``/``resume_skill_canonical_key``) must derive
    the IDENTICAL normalised string for every name in this corpus — every
    shipped alias/canonical, every recall fixture, every one of the 18 PII
    shapes, and every Phase 4b spelling-recall variant (plus its expected
    canonical). A divergence here means ``REQUIRES``/``HAS_SKILL`` silently
    stop meeting at the same ``Skill`` node for that skill."""
    assert skills_graph._basic_normalise(name) == skills._basic_normalise(name)


# ── mutation-proof: a future re-divergence must turn the above test RED ──


def test_a_diverged_copy_is_actually_caught_not_silently_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Post-dedupe, there is no second COPY of ``_basic_normalise`` left to
    edit independently — so this proves the parity invariant is still
    enforced by monkeypatching ONE call site (``skills_graph``'s imported
    reference) to simulate exactly the defect this file exists to close: a
    future change that re-introduces a divergent definition on one side
    only. If this test ever went green (i.e. the parity assertion below did
    NOT raise), the corpus test above would be worthless — it would pass by
    construction regardless of whether the two sides actually agree.
    """
    monkeypatch.setattr(skills_graph, "_basic_normalise", lambda raw: f"DIVERGED-{raw}")

    with pytest.raises(AssertionError):
        assert skills_graph._basic_normalise("python") == skills._basic_normalise(
            "python"
        )
