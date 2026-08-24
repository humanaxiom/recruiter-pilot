"""ROADMAP A2 — merge contract for the 234-term derived skill vocabulary.

**This is a MERGE, not a derivation.** The terms already exist at
``docs/process/skill-vocabulary/derived-families.yaml`` (13 families, 234
terms, additive to the 19 existing software families). This file pins the
contract for merging them into the two shipped vocabulary files:

  - ``core/src/pipeline/skill_data/aliases.yaml``    (alias -> canonical)
  - ``core/src/pipeline/skill_data/categories.yaml``  (family -> [canonicals])

**Why both are required** (not just categories.yaml): ``categories_for()``
reads ``categories.yaml`` alone, but the deterministic résumé free-text
scanner (``match_skills_in_text``, ``skills.py:211-214``) reads ONLY the
alias table. A term that lands in ``categories.yaml`` but never in
``aliases.yaml`` is invisible to résumé text and the coverage gain this
merge exists to deliver never materialises.

Every test below is driven off the REAL, shipped files on disk — reading
``derived-families.yaml`` as the source of truth and iterating it, so this
suite cannot drift from what the merge actually needs to do.

Genuinely-RED tests today (the merge has not happened yet):
  - test_derived_family_exists_in_shipped_categories
  - test_shipped_family_matches_its_derived_family_exactly
  - test_derived_term_resolves_through_the_alias_table
  - test_already_shipping_term_accumulates_its_new_family_without_losing_the_old

Regression GUARDS that are expected to pass today (the current, pre-merge
files are clean) and must keep passing after the merge lands:
  - test_gate_critical_canonical_stays_familyless (rest api design / c++ /
    hudson / julia -- MUST stay family-less through and after the merge)
  - test_no_duplicate_alias_strings_in_shipped_aliases_yaml
  - test_every_shipped_term_survives_normalisation_and_length_caps
  - test_candidate_fixture_name_does_not_become_resolvable_vocabulary
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml  # type: ignore[import-untyped]

from src.pipeline import skills_graph
from src.pipeline.skills import _ALIASES_PATH, _alias_table, canonicalize_skill_names
from src.pipeline.skills_graph import (
    _CATEGORIES_PATH,
    _MAX_SKILL_NAME_CHARS,
    _MAX_SKILL_NAME_TOKENS,
    categories_for,
)

# ``core/tests/unit/<this file>`` -> parents[3] is the repo root (mirrors
# scripts/verify.sh, which mounts the repo root at /repo so meta-tests that
# resolve repo-root files via parents[N] don't resolve to "/").
_REPO_ROOT = Path(__file__).resolve().parents[3]
_DERIVED_FAMILIES_PATH = (
    _REPO_ROOT / "docs" / "process" / "skill-vocabulary" / "derived-families.yaml"
)


def _load_yaml(path: Path) -> object:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _lower(items: object) -> list[str]:
    return [str(item).strip().lower() for item in (items or [])]  # type: ignore[union-attr]


def _load_derived_families() -> dict[str, list[str]]:
    data = _load_yaml(_DERIVED_FAMILIES_PATH)
    assert isinstance(data, dict), (
        f"{_DERIVED_FAMILIES_PATH} did not parse to a family -> [terms] map -- "
        "constraint violation: this file must not be touched by the A2 merge"
    )
    return {
        str(family).strip().lower(): _lower(terms) for family, terms in data.items()
    }


def _load_shipped_categories() -> dict[str, list[str]]:
    data = _load_yaml(_CATEGORIES_PATH)
    assert isinstance(data, dict)
    return {
        str(family).strip().lower(): _lower(terms) for family, terms in data.items()
    }


_DERIVED_FAMILIES = _load_derived_families()

# Every (family, term) pair the source file declares -- used to drive the
# merge-happened assertions below so a partially-merged family (family key
# present but a term missing, or vice versa) cannot slip past.
_DERIVED_FAMILY_TERM_PAIRS: list[tuple[str, str]] = [
    (family, term) for family, terms in _DERIVED_FAMILIES.items() for term in terms
]

# Every distinct term across all 13 derived families, deduped -- some terms
# (none, as of the source file today, but the test must not assume that)
# could in principle belong to more than one family.
_ALL_DERIVED_TERMS: list[str] = sorted(
    {term for _fam, term in _DERIVED_FAMILY_TERM_PAIRS}
)


@pytest.fixture(autouse=True)
def _fresh_vocab_caches() -> None:
    """`_alias_table`/`_category_table` are `lru_cache(maxsize=1)`d; clear
    them before every test in this module so each read reflects the files
    on disk right now, matching the idiom in test_pipeline_skills.py /
    test_pipeline_skills_graph.py."""
    _alias_table.cache_clear()  # type: ignore[attr-defined]
    skills_graph._category_table.cache_clear()  # type: ignore[attr-defined]


# ═══════════════════════════════════════════════════════════════════════════
# 1) The merge happened at all: every derived family + term is shipped.
# ═══════════════════════════════════════════════════════════════════════════


def test_source_file_declares_the_expected_shape() -> None:
    """Sanity check on the fixture itself (constraint: derived-families.yaml
    must not be modified by this merge) -- 13 families, 234 terms, matching
    the A2 spec header. If this fails, the SOURCE moved, not the target;
    do not "fix" it by editing derived-families.yaml."""
    assert len(_DERIVED_FAMILIES) == 13
    assert len(_DERIVED_FAMILY_TERM_PAIRS) == 234


@pytest.mark.parametrize("family", sorted(_DERIVED_FAMILIES))
def test_derived_family_exists_in_shipped_categories(family: str) -> None:
    shipped = _load_shipped_categories()
    assert family in shipped, (
        f"family {family!r} (from derived-families.yaml) is missing entirely "
        f"from {_CATEGORIES_PATH.name} -- the A2 merge has not landed"
    )


@pytest.mark.parametrize("family", sorted(_DERIVED_FAMILIES))
def test_shipped_family_matches_its_derived_family_exactly(family: str) -> None:
    """Bidirectional merge contract. The forward half (every derived term is
    a member of its shipped family) is necessary but not sufficient: a
    forward-only check does not notice an INVENTED term added to a shipped
    family, or an EXISTING term silently re-scoped into a second family it
    was never meant to have -- both survive a subset check untouched. A
    categories-only extra is not inert: ``skills_graph.py:294`` makes it a
    cleartext canonical key and ``categories_for()`` grants it family
    credit, so an extra here is a real leak/scoring-integrity surface, not
    housekeeping. Set equality against derived-families.yaml (the source of
    truth for these 13 families) catches both directions at once.
    """
    shipped = _load_shipped_categories()
    derived_terms = set(_DERIVED_FAMILIES[family])
    shipped_terms = set(shipped.get(family, []))
    assert shipped_terms == derived_terms, (
        f"categories.yaml[{family!r}] does not exactly match "
        f"derived-families.yaml[{family!r}] -- extra (not in the source): "
        f"{sorted(shipped_terms - derived_terms)!r}, missing: "
        f"{sorted(derived_terms - shipped_terms)!r}. An extra term is not "
        "inert -- skills_graph.py:294 makes it a cleartext canonical key "
        "and categories_for() grants it family credit."
    )


# ═══════════════════════════════════════════════════════════════════════════
# 2) Every derived term also resolves through the alias table -- proves it
#    landed in aliases.yaml too, not just categories.yaml.
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("term", _ALL_DERIVED_TERMS)
def test_derived_term_resolves_through_the_alias_table(term: str) -> None:
    """categories.yaml membership alone confers a cleartext canonical key
    (skills_graph.py:286-296) but NOT résumé-text findability -- the
    deterministic scanner (skills.py:211-214) reads only aliases.yaml.

    Deliberately asserts ``_alias_table()`` MEMBERSHIP directly rather than
    round-tripping through ``canonicalize_skill_names`` -- ``_basic_normalise``
    falls back to returning ANY unrecognised, already-clean lowercase phrase
    UNCHANGED (skills.py's documented fallback for a non-vocab name), so
    ``canonicalize_skill_names([term]) == [term]`` is true for every plain
    English phrase whether or not it is actually in aliases.yaml -- it proves
    nothing about alias-table membership. Checking the table directly is the
    only assertion that can actually distinguish "in aliases.yaml" from "not
    in aliases.yaml, but also not mangled by punctuation-stripping"."""
    table = _alias_table()
    assert term in table, (
        f"{term!r} is not a key in the alias table (aliases.yaml) -- "
        "categories.yaml membership alone does not make this term findable "
        "in résumé free text (the scanner reads only the alias table, "
        "skills.py:211-214)"
    )
    assert table[term] == term, (
        f"{term!r} is in the alias table but maps to canonical {table[term]!r}, "
        "not itself -- aliases.yaml convention violation: a canonical name "
        "must appear in its own aliases list for round-trip safety"
    )


# ═══════════════════════════════════════════════════════════════════════════
# 3) 🔴 The one constraint that reddens a corpus gate if broken: these four
#    canonicals must remain family-less through and after the merge.
# ═══════════════════════════════════════════════════════════════════════════

_FAMILY_LESS_CANONICALS: tuple[str, ...] = ("rest api design", "c++", "hudson", "julia")


@pytest.mark.parametrize("canonical", _FAMILY_LESS_CANONICALS)
def test_gate_critical_canonical_stays_familyless(canonical: str) -> None:
    """GUARD, not a merge requirement -- these four canonicals must NEVER be
    given a family, before or after the A2 merge lands.

    ``rest api design`` is the load-bearing one: the `skill_missing_must`
    ordering pair (r18 = r01 minus REST API design; thresholds.toml:384-386)
    depends on its `ontology_weight` being an UNAMBIGUOUS 0 -- a missing
    must-have with no family credit at all. Giving it (or any of the other
    three) a family in categories.yaml does not fail loudly anywhere else in
    the suite; it SILENTLY shrinks or collapses that ordering pair's gap in
    the ranking-evals corpus (thresholds.toml's own comment: the gap is
    designed to be ~0.144, entirely dependent on this staying zero). If this
    assertion goes red, the fix is to remove the family from categories.yaml,
    never to weaken this test.
    """
    got = categories_for(canonical)
    assert got == [], (
        f"{canonical!r} now has a family: {got!r}. This SILENTLY DISARMS the "
        "skill_missing_must ordering-pair gate (r18 = r01 minus REST API "
        "design, thresholds.toml:384-386) by turning an unambiguous "
        "ontology_weight=0 absence into partial family credit for a corpus "
        "fixture. A2's spec forbids giving any of "
        f"{_FAMILY_LESS_CANONICALS!r} a family under any circumstance -- "
        "remove it from categories.yaml, do not weaken this assertion."
    )


# ═══════════════════════════════════════════════════════════════════════════
# 4) No duplicate alias strings anywhere in the shipped aliases.yaml.
# ═══════════════════════════════════════════════════════════════════════════


def test_no_duplicate_alias_strings_in_shipped_aliases_yaml() -> None:
    """GUARD, expected to already pass on the pre-merge file, and MUST keep
    passing once ~225 new canonicals/aliases are added. `_alias_table` in
    `skills.py:69-81` builds `{alias: canonical}` by iterating entries and
    assigning into a plain dict -- LAST WRITER WINS (skills.py:79). A
    duplicate alias string (whether under the same canonical twice, or
    (worse) under two different canonicals) silently re-points an earlier
    canonical's alias to a different key, with no error anywhere."""
    data = _load_yaml(_ALIASES_PATH)
    assert isinstance(data, list)
    seen: dict[str, list[str]] = {}
    for entry in data:
        canonical = str(entry["canonical"]).strip().lower()
        for alias in entry.get("aliases", []):
            key = str(alias).strip().lower()
            seen.setdefault(key, []).append(canonical)
    duplicates = {alias: cans for alias, cans in seen.items() if len(cans) > 1}
    assert not duplicates, (
        "duplicate alias string(s) in aliases.yaml -- last-writer-wins "
        f"(skills.py:79) means each of these silently re-points an earlier "
        f"canonical: {duplicates!r}"
    )


# ═══════════════════════════════════════════════════════════════════════════
# 5) Every shipped canonical/alias survives normalisation and the length
#    caps. Terms with "/", "&", "," or an apostrophe are the trap.
# ═══════════════════════════════════════════════════════════════════════════


def test_every_shipped_term_survives_normalisation_and_length_caps() -> None:
    """GUARD, expected to already pass, and the regression this merge must
    not break: `_MAX_SKILL_NAME_CHARS = 60` / `_MAX_SKILL_NAME_TOKENS = 8`
    (skills_graph.py:160,164) are checked against the RAW candidate name
    (before `_basic_normalise`, per `reject_reason_for_skill_name`'s own
    docstring) -- a term that trips either cap is shape-rejected before it
    ever reaches the alias table. Separately, `_PUNCT_RE` (skills.py:46)
    strips anything outside `[\\w.+#\\- ]` -- a term containing "/", "&",
    "," or an apostrophe cleans to a DIFFERENT string and only round-trips
    if that cleaned form is itself registered (precedent: "ci/cd" only
    resolves because "cicd" is a sibling alias)."""
    data = _load_yaml(_ALIASES_PATH)
    assert isinstance(data, list)
    checked = 0
    for entry in data:
        canonical = str(entry["canonical"]).strip().lower()
        terms = {canonical, *(str(a) for a in entry.get("aliases", []))}
        for term in terms:
            assert len(term) <= _MAX_SKILL_NAME_CHARS, (
                f"{term!r} ({len(term)} chars) exceeds "
                f"_MAX_SKILL_NAME_CHARS={_MAX_SKILL_NAME_CHARS} -- would be "
                "shape-rejected by reject_reason_for_skill_name before it "
                "ever resolves"
            )
            assert len(term.split()) <= _MAX_SKILL_NAME_TOKENS, (
                f"{term!r} ({len(term.split())} tokens) exceeds "
                f"_MAX_SKILL_NAME_TOKENS={_MAX_SKILL_NAME_TOKENS}"
            )
            assert canonicalize_skill_names([term]) == [canonical], (
                f"{term!r} does not survive normalisation back to its own "
                f"canonical {canonical!r} -- likely stripped by _PUNCT_RE "
                "('/', '&', ',' or an apostrophe) with no punctuation-free "
                "sibling alias registered to catch the cleaned form"
            )
            checked += 1
    assert checked >= 150  # sanity: the vocab was not accidentally shrunk


# ═══════════════════════════════════════════════════════════════════════════
# 6) The nine already-shipping terms keep their existing family AND gain the
#    new one -- categories.yaml ACCUMULATES, it never replaces.
# ═══════════════════════════════════════════════════════════════════════════

# Pinned as of the pre-merge categories.yaml (verified by reading the shipped
# file): these are the CURRENT families for the nine terms the A2 spec names
# as already-shipping. Deliberately hardcoded rather than read from the file
# under test -- reading the very file this test is pinning would make the
# "kept its old family" half of the assertion tautological.
_EXISTING_TERM_OLD_FAMILY: dict[str, str] = {
    "collaboration": "soft",
    "curriculum development": "edtech",
    "leadership": "soft",
    "mentoring": "soft",
    "problem solving": "soft",
    "project management": "pm",
    "stakeholder management": "pm",
    "teamwork": "soft",
    "time management": "soft",
}


def _new_families_for(term: str) -> list[str]:
    """Family/families `term` is assigned to in derived-families.yaml --
    driven off the source file, not hardcoded, so this cannot drift from it."""
    return sorted(
        family for family, terms in _DERIVED_FAMILIES.items() if term in terms
    )


@pytest.mark.parametrize("term", sorted(_EXISTING_TERM_OLD_FAMILY))
def test_already_shipping_term_accumulates_its_new_family_without_losing_the_old(
    term: str,
) -> None:
    old_family = _EXISTING_TERM_OLD_FAMILY[term]
    new_families = _new_families_for(term)
    assert new_families, (
        f"{term!r} (one of the nine already-shipping terms named in the A2 "
        "spec) was not found in any derived-families.yaml family -- fixture "
        "drift, check the spec's term list against the source file"
    )
    got = set(categories_for(term))
    assert old_family in got, (
        f"{term!r} lost its pre-existing family {old_family!r} -- "
        f"categories.yaml must ACCUMULATE families, never replace them "
        f"(current families: {sorted(got)!r})"
    )
    for new_family in new_families:
        assert new_family in got, (
            f"{term!r} did not gain its new derived family {new_family!r} "
            f"from derived-families.yaml (current families: {sorted(got)!r})"
        )


# ═══════════════════════════════════════════════════════════════════════════
# 7) PII-shape collision guard (ADR-008 residual #13). ~225 new common-
#    English-word canonicals widen the surface where a candidate's own name
#    collides with vocabulary and gets projected as cleartext instead of an
#    opaque hash.
# ═══════════════════════════════════════════════════════════════════════════

# Every candidate-name fixture used as a "name", not a "skill", across the
# unit test suite and the ranking-evals corpus (core/tests/evals/fixtures/
# resumes/*.json + the recurring Casey Rivera / Ada Lovelace fixtures used
# throughout core/tests/unit/). None of these may become resolvable
# vocabulary -- see test_worker_project_resume.py:362-379, which pins this
# exact invariant for "Casey Rivera" alone; this widens it to the whole
# fixture roster so a merge that accidentally introduces e.g. "Kim" or
# "Reed" as vocabulary doesn't quietly corrupt a corpus ranking score.
_CANDIDATE_NAME_FIXTURES: tuple[str, ...] = (
    "Casey Rivera",
    "Ada Lovelace",
    "Sam Ortiz",
    "Morgan Lee",
    "Jordan Kim",
    "Alex Nguyen",
    "Drew Patel",
    "Taylor Reed",
    "Skyler Brooks",
    "Avery Thompson",
    "Riley Chen",
    "Harper Nakamura",
    "Cameron Whitfield",
    "Rowan Castillo",
    "Jamie Okafor",
    "Quinn Delgado",
    "Reese Dawson",
    "Devon Ashworth",
)


@pytest.mark.parametrize("name", _CANDIDATE_NAME_FIXTURES)
def test_candidate_fixture_name_does_not_become_resolvable_vocabulary(
    name: str,
) -> None:
    """GUARD, expected to already pass, and the regression this merge must
    not break: a candidate's own name must never resolve to a cleartext
    vocabulary key. `resume_skill_canonical_key` either shape-rejects (None)
    or hashes (`h:`-prefixed) anything that is not an exact vocab/alias
    hit -- this asserts every fixture name used as a PERSON, not a skill,
    across this test suite and the eval corpus stays on the hash side of
    that boundary after the ~225-term merge."""
    key = skills_graph.resume_skill_canonical_key(name)
    assert key is not None, (
        f"{name!r} was shape-rejected outright -- unexpected for a plain "
        "two-token name; check the fixture itself before this test"
    )
    assert key.startswith(skills_graph._HASH_KEY_PREFIX), (
        f"{name!r} normalised to a CLEARTEXT vocabulary key {key!r} -- a "
        "candidate's own name must never be resolvable vocabulary "
        "(ADR-008 residual #13); this is exactly the invariant "
        "test_worker_project_resume.py:364-375 pins for 'Casey Rivera' alone, "
        "now widened to every candidate-name fixture in the suite"
    )
