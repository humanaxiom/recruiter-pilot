"""Unit tests for the Neo4j-free skill-matching slice
(``src/pipeline/skills.py``).

Ported from hris ``apps/worker/src/worker/skill_normalize.py``
(``phase3-source-dossier.md`` §12), trimmed to the three pure functions
that don't touch Neo4j: ``match_skills_in_text``,
``canonicalize_skill_names``, ``build_summary_text``. The alias table
itself (``core/src/pipeline/skill_data/aliases.yaml``) is ported
verbatim from hris ``infra/skills/aliases.yaml`` — ``javascript``/``js``
and ``kubernetes``/``k8s`` are real entries there (verified by reading
the shipped hris file), used below as real-world regression anchors.

**Path-resolution regression (the actual bug this file guards against)**:
hris's ``skill_normalize.py`` resolves its YAML via a hardcoded
``Path(__file__).resolve().parents[4]``-relative walk to a repo-root
``infra/skills/`` directory. That depth is specific to hris's deeper
package nesting and is WRONG for the flatter ``core/src/pipeline/
skills.py`` module depth in this repo — and a repo-root ``infra/`` would
also be invisible inside the Docker build context anyway (``./core``
only). ``test_match_skills_resolves_real_aliases_yaml_regardless_of_cwd``
is what catches a wrong-depth port.

**Naming assumption**: the "missing YAML degrades gracefully" test
monkeypatches a module attribute named ``_ALIASES_PATH`` (the same name
hris's ``skill_normalize.py`` uses for its resolved path constant) since
the trimmed port is not expected to rename it. If the implementation
uses a different constant name, that one test will need updating to
match — it is not a behavioural assumption, just a naming one.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.pipeline.skills import (
    build_summary_text,
    canonicalize_skill_names,
    match_skills_in_text,
)
from src.schemas.jobs import JDExtracted, Skill

# ── match_skills_in_text ──────────────────────────────────────────────────


def test_match_skills_finds_aliased_term_case_insensitively() -> None:
    text = "I have shipped production apps using JS extensively."
    assert "javascript" in match_skills_in_text(text)


def test_match_skills_respects_word_boundaries_not_substring() -> None:
    """ "js" must not fire inside a longer token like "abcjs"."""
    text = "I maintain a legacy internal tool called abcjs for reporting."
    assert "javascript" not in match_skills_in_text(text)


def test_match_skills_returns_first_seen_order_deduped() -> None:
    text = "Python and Python again, then SQL, then Python once more."
    matches = match_skills_in_text(text)
    assert matches.count("python") == 1
    assert matches.index("python") < matches.index("sql")


# ── canonicalize_skill_names ──────────────────────────────────────────────


def test_canonicalize_dedupes_case_and_whitespace_preserving_order() -> None:
    names = ["Python", "python", "  PYTHON  ", "SQL", "sql"]
    assert canonicalize_skill_names(names) == ["python", "sql"]


def test_canonicalize_skill_names_empty_input_is_empty_output() -> None:
    assert canonicalize_skill_names([]) == []


# ── Phase 4b spelling-recall fix ──────────────────────────────────────────
#
# ranking-evals measured spelling recall against realistic résumé wording at
# 37.5% (15/40) — with ADR-008 demoting vector auto-merge out of the scoring
# path, `_basic_normalise` + the alias table ARE the entire matching surface
# now. Each pair below is a real spelling variant that must normalise to the
# SAME canonical term as the plain vocab spelling. Mutation-kill: reverting
# the trailing-version-strip / parenthetical-split logic in
# `_basic_normalise` turns every one of these RED (each variant would
# normalise to itself — or, for the parenthetical cases, to the OLD
# paren-folded phrase — never `expected_canonical`).

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
def test_spelling_variant_normalises_to_the_expected_canonical(
    variant: str, expected_canonical: str
) -> None:
    assert canonicalize_skill_names([variant]) == [expected_canonical]


@pytest.mark.parametrize("variant,expected_canonical", SPELLING_RECALL_FIXTURES)
def test_spelling_variant_round_trips_jd_and_resume_side_to_the_same_key(
    variant: str, expected_canonical: str
) -> None:
    """The critical invariant (security re-audit, mutation-killed on the
    Neo4j-backed half): normalisation runs BEFORE the canonical key is
    computed, so the JD side (a variant spelling like "PostgreSQL 14") and
    the résumé side (the plain canonical spelling, "postgresql") MUST still
    agree on one normalised string — proven here via the same
    `canonicalize_skill_names` entry point both sides funnel through."""
    jd_side = canonicalize_skill_names([variant])
    resume_side = canonicalize_skill_names([expected_canonical])
    assert jd_side == resume_side == [expected_canonical]


def test_every_shipped_alias_still_resolves_to_its_own_canonical() -> None:
    """Zero-regression guard: none of the ~220+ shipped vocabulary spellings
    may be rejected or mis-keyed by the new version-strip/parenthetical-
    split logic — every alias in the REAL, shipped `aliases.yaml` must still
    normalise to its own documented canonical."""
    import yaml

    from src.pipeline.skills import _ALIASES_PATH

    data = yaml.safe_load(_ALIASES_PATH.read_text(encoding="utf-8")) or []
    checked = 0
    for entry in data:
        canonical = entry["canonical"].strip().lower()
        for alias in entry.get("aliases", []):
            assert canonicalize_skill_names([alias]) == [
                canonical
            ], f"alias {alias!r} no longer resolves to canonical {canonical!r}"
            checked += 1
    assert checked >= 150  # sanity: the vocab was not accidentally shrunk


# ── Security fix (post-4b re-audit, LOW #2): paren-split skill inflation ──
#
# The parenthetical-split fallback added above extracts a real skill term
# out of a NAME-BEARING string: "Casey Rivera (Python)" -> "python",
# "Rivera (psql)" -> "postgresql", "Casey (Kafka Streams)" -> "kafka". The
# key itself carries no name (not a PII leak — see ADR-008), but a
# mis-extracted name that ends up with a spurious HAS_SKILL edge INFLATES
# that candidate's skill score on garbage the 4a evals corpus is blind to.
#
# Fix: the parenthetical-CONTENT fallback only runs when the outer phrase
# (parens stripped) is empty or itself vocab-adjacent — a technical
# qualifier phrase like "AWS MWAA" shares a vocab token ("aws") with the
# table; name-bearing free text ("Casey Rivera", "Rivera", "Ada Lovelace",
# "Casey") shares none. "Containerization (Docker)" is preserved by
# registering "containerization" itself as a docker alias (resolves at the
# earlier, unconditional outer-full-resolve step, never needs the gate).

# MUST-NOT-EXTRACT: a name-bearing outer phrase must never pull the real
# skill out of its own parenthetical.
PAREN_SPLIT_INFLATION_FIXTURES: tuple[str, ...] = (
    "Casey Rivera (Python)",
    "Rivera (psql)",
    "Ada Lovelace (Julia)",
    "Casey (Kafka Streams)",
)


_INFLATION_TARGET_SKILLS = ("python", "postgresql", "julia", "kafka")


@pytest.mark.parametrize("name", PAREN_SPLIT_INFLATION_FIXTURES)
def test_name_bearing_parenthetical_does_not_inflate_a_skill(name: str) -> None:
    """None of these may resolve to the parenthetical's own skill term —
    the candidate/name-shaped outer phrase must not unlock it. It must fall
    through to the (unresolved) full-cleaned string instead — hashed
    downstream by the résumé-side projection, never a bare vocab hit."""
    result = canonicalize_skill_names([name])
    assert result not in ([s] for s in _INFLATION_TARGET_SKILLS)
    expected_unresolved = re.sub(r"[^\w.+#\- ]+", "", name.lower())
    expected_unresolved = re.sub(r"\s+", " ", expected_unresolved).strip()
    assert result == [expected_unresolved]


# MUST-KEEP: every Phase-4b recovered spelling must still round-trip.
PAREN_SPLIT_LEGITIMATE_FIXTURES: tuple[tuple[str, str], ...] = (
    ("Kubernetes (EKS)", "kubernetes"),
    ("Terraform (IaC)", "terraform"),
    ("Python (3.11)", "python"),
    ("AWS MWAA (Airflow)", "airflow"),
    ("Containerization (Docker)", "docker"),
)


@pytest.mark.parametrize("variant,expected_canonical", PAREN_SPLIT_LEGITIMATE_FIXTURES)
def test_legitimate_parenthetical_still_resolves(
    variant: str, expected_canonical: str
) -> None:
    assert canonicalize_skill_names([variant]) == [expected_canonical]


def test_outer_phrase_vocab_adjacency_gate_mutation_kill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mutation-proof: reverting the gate (always allowing the
    parenthetical-content fallback, as the pre-fix code did) must turn the
    inflation cases RED — proving this test suite actually depends on the
    gate rather than passing by some other coincidence."""
    import src.pipeline.skills as skills_mod

    monkeypatch.setattr(
        skills_mod, "_outer_phrase_is_vocab_adjacent", lambda outer_clean: True
    )
    assert skills_mod.canonicalize_skill_names(["Casey Rivera (Python)"]) == ["python"]
    assert skills_mod.canonicalize_skill_names(["Rivera (psql)"]) == ["postgresql"]
    assert skills_mod.canonicalize_skill_names(["Ada Lovelace (Julia)"]) == ["julia"]
    assert skills_mod.canonicalize_skill_names(["Casey (Kafka Streams)"]) == ["kafka"]


# ── Path-resolution regression ────────────────────────────────────────────


def test_match_skills_resolves_real_aliases_yaml_regardless_of_cwd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Import fresh and run from an unrelated cwd: the YAML load must be
    ``Path(__file__)``-relative, not cwd-relative."""
    monkeypatch.chdir(tmp_path)
    import importlib

    import src.pipeline.skills as skills_mod

    importlib.reload(skills_mod)

    matches = skills_mod.match_skills_in_text(
        "Deployed workloads to K8S in production."
    )
    assert "kubernetes" in matches


# ── Missing YAML degrades gracefully ──────────────────────────────────────


def test_missing_aliases_yaml_degrades_to_empty_table_not_an_exception(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A ``finally`` re-clears the (module-level, process-wide) ``lru_cache``
    after the test — without it, the cache stays poisoned with the
    "missing file" empty table/scanner for the REST of the test session
    (``monkeypatch.setattr`` restores ``_ALIASES_PATH`` on teardown, but does
    nothing about the cached function results computed against the
    tmp-path-missing state), silently breaking every later test that expects
    a real alias to resolve (a genuine, order-dependent test-isolation bug
    this file's own `test_categories_for_missing_yaml_file_degrades_to_empty_
    not_a_crash` counterpart in ``test_pipeline_skills_graph.py`` already
    guards against with the same pattern)."""
    import src.pipeline.skills as skills_mod

    missing = tmp_path / "does-not-exist.yaml"
    monkeypatch.setattr(skills_mod, "_ALIASES_PATH", missing)
    for cached_name in ("_alias_table", "_skill_scanner"):
        cached = getattr(skills_mod, cached_name, None)
        if cached is not None and hasattr(cached, "cache_clear"):
            cached.cache_clear()

    try:
        result = skills_mod.match_skills_in_text(
            "Python, Kubernetes, and SQL experience."
        )
        assert result == []
    finally:
        for cached_name in ("_alias_table", "_skill_scanner"):
            cached = getattr(skills_mod, cached_name, None)
            if cached is not None and hasattr(cached, "cache_clear"):
                cached.cache_clear()


# ── build_summary_text ────────────────────────────────────────────────────


def test_build_summary_text_composes_documented_order() -> None:
    extracted = JDExtracted(
        title="Senior Backend Engineer",
        responsibilities=["Own the payments service", "Mentor junior engineers"],
        required_skills=[Skill(name="Python"), Skill(name="PostgreSQL")],
        nice_to_have_skills=[Skill(name="Kubernetes")],
    )

    summary = build_summary_text(extracted)

    assert summary == (
        "Senior Backend Engineer. "
        "Own the payments service Mentor junior engineers. "
        "Required: Python, PostgreSQL. "
        "Nice to have: Kubernetes"
    )


def test_build_summary_text_with_only_title() -> None:
    extracted = JDExtracted(title="Data Analyst")
    assert build_summary_text(extracted) == "Data Analyst"


def test_build_summary_text_never_includes_required_when_absent() -> None:
    extracted = JDExtracted(title="Data Analyst", nice_to_have_skills=[Skill(name="R")])
    summary = build_summary_text(extracted)
    assert "Required:" not in summary
    assert "Nice to have: R" in summary
