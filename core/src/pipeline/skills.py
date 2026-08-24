"""Skill normalisation — Neo4j-free slice.

Trimmed from hris ``apps/worker/src/worker/skill_normalize.py`` (see
``phase3-source-dossier.md`` §12 / the binding contract). This module
keeps ONLY the pure, deterministic functions that don't touch Neo4j:

  - ``_basic_normalise``       lowercase, strip, alias map lookup
  - ``_alias_table``           alias -> canonical_name, from aliases.yaml
  - ``match_skills_in_text``   deterministic vocabulary scan
  - ``canonicalize_skill_names``  normalise + dedupe a name list
  - ``build_summary_text``     canonical JD embedding text

The vector-match / LLM-tiebreaker / Skill-graph half of the hris module
(``normalize_skill``, ``_ask_llm_tiebreaker``, ``categories_for``,
``_family_vocab``, ``skill_categories_vocab``, ``categories.yaml``) is a
Phase 4 concern and is deliberately NOT ported here.

Path-resolution deviation: hris resolves ``aliases.yaml`` via
``Path(__file__).resolve().parents[4]`` to a repo-root ``infra/skills/``
directory. That depth is wrong at this module's (flatter) nesting, and a
repo-root ``infra/`` directory would be invisible inside the Docker build
context anyway (``compose`` builds with ``context: ./core``). The YAML
ships instead at ``core/src/pipeline/skill_data/aliases.yaml`` (inside the
build context) and is resolved relative to ``Path(__file__).resolve().parent``.
A missing file degrades gracefully to an empty table (matches nothing),
never crashes.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path

import yaml  # type: ignore[import-untyped]

from src.schemas.jobs import JDExtracted

log = logging.getLogger(__name__)

_ALIASES_PATH = Path(__file__).resolve().parent / "skill_data" / "aliases.yaml"

_WHITESPACE_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[^\w.+#\- ]+")

# Phase 4b spelling-recall fix (ranking-evals: 37.5% spelling recall against
# realistic résumé wording, entirely traceable to vocabulary/normalisation —
# ADR-008 demoted vector auto-merge out of the scoring path, so this IS the
# whole matching surface now). Two ADDITIVE transforms, tried only as a
# fallback AFTER the full (pre-existing) cleaned string fails to resolve —
# see `_basic_normalise`'s docstring for why that ordering matters.
#
#   - `_TRAILING_VERSION_RE` strips a trailing " <version>" token so
#     "PostgreSQL 14"/"Python 3"/"Apache Airflow 2.7" resolve to the same key
#     as "postgresql"/"python"/"airflow".
#   - `_PAREN_RE` strips a "(...)" qualifier so "Kubernetes (EKS)"/
#     "Terraform (IaC)" resolve to the outer term, and "AWS MWAA (Airflow)"
#     falls back to the PARENTHETICAL's own content when the outer phrase
#     alone isn't a vocab hit BUT is still vocab-adjacent (see
#     `_outer_phrase_is_vocab_adjacent` — a security gate closing a
#     scoring-integrity finding: post-4b re-audit, LOW #2).
_TRAILING_VERSION_RE = re.compile(r"\s+v?\d+(?:\.\d+)*$", re.IGNORECASE)
_PAREN_RE = re.compile(r"\(([^()]*)\)")


@lru_cache(maxsize=1)
def _alias_table() -> dict[str, str]:
    """alias -> canonical_name, materialised from aliases.yaml."""
    if not _ALIASES_PATH.is_file():
        log.warning("skill_aliases.missing path=%s", _ALIASES_PATH)
        return {}
    data = yaml.safe_load(_ALIASES_PATH.read_text(encoding="utf-8")) or []
    out: dict[str, str] = {}
    for entry in data:
        canonical = entry["canonical"].strip().lower()
        for alias in entry.get("aliases", []):
            out[alias.strip().lower()] = canonical
        out[canonical] = canonical
    return out


def _clean(text: str) -> str:
    """Punctuation-strip + whitespace-collapse — the pre-existing
    ``_basic_normalise`` body, factored out so both the full-string fallback
    and the parenthetical-split attempt below share ONE implementation."""
    text = _PUNCT_RE.sub("", text)
    return _WHITESPACE_RE.sub(" ", text).strip()


def _resolve_or_none(cleaned: str) -> str | None:
    """Alias-table lookup on ``cleaned``, then (only on a miss) on
    ``cleaned`` with a trailing version token stripped. Returns ``None``
    (never a bare cleaned string) when neither resolves — the caller decides
    the fallback, so this function is pure "did the vocab recognise this".
    """
    table = _alias_table()
    if cleaned in table:
        return table[cleaned]
    stripped = _TRAILING_VERSION_RE.sub("", cleaned).strip()
    if stripped and stripped != cleaned and stripped in table:
        return table[stripped]
    return None


def _outer_phrase_is_vocab_adjacent(outer_clean: str) -> bool:
    """Security gate (post-4b re-audit, LOW #2 — parenthetical-split skill
    inflation): decides whether the parenthetical-CONTENT fallback in
    ``_basic_normalise`` is even allowed to run.

    Without this gate, a name-bearing string like ``"Casey Rivera (Python)"``
    or ``"Rivera (psql)"`` would fall through to the parenthetical's own
    content and resolve to a REAL vocab skill (``python``/``postgresql``) —
    an outer phrase that is free text about a *person*, not a qualifier on a
    skill, has no business unlocking that skill via its parenthetical. That
    would mint a spurious ``HAS_SKILL`` edge (a scoring-integrity problem,
    not a PII leak — the key never carries the name either way).

    Returns ``True`` (fallback allowed) when the outer phrase is empty, or
    when at least one of its whitespace-separated tokens is ITSELF a
    recognised vocab term on its own — the signal that distinguishes a
    technical qualifier phrase (``"AWS MWAA"`` — ``"aws"`` is vocab) from
    name-bearing free text (``"Casey Rivera"``, ``"Rivera"``, ``"Casey"`` —
    no token is vocab). Legitimate outer phrases that are neither a vocab hit
    nor share a vocab token (``"Containerization (Docker)"``) are handled by
    registering the outer phrase itself as a vocab alias (see
    ``skill_data/aliases.yaml``) so they resolve at the earlier, unconditional
    outer-full-resolve step and never need this gate at all — deliberately:
    growing the vocabulary is the only lever ADR-008 sanctions for recall,
    never a shape/name heuristic.
    """
    if not outer_clean:
        return True
    table = _alias_table()
    return any(tok in table for tok in outer_clean.split())


def _basic_normalise(raw: str) -> str:
    """Lowercases, collapses whitespace, strips most punctuation, then
    resolves via the alias map if present — trying, in order:

    1. The full cleaned string (identical to the pre-existing algorithm,
       parentheses folded into the phrase like any other stripped
       punctuation). Returned as-is if unresolved — an unlisted/non-vocab
       name normalises EXACTLY as it did before this fix; nothing here
       widens what counts as a match for a name that was never vocab in the
       first place.
    2. A trailing-version-token strip of that same string (``"PostgreSQL
       14"`` -> ``"postgresql"``), tried INSIDE step 1/3 via
       ``_resolve_or_none`` — only used if it lands on a real vocab entry.
    3. If (1) still doesn't resolve AND the raw string has a
       parenthetical, the OUTER phrase with the parenthetical entirely
       removed (``"Kubernetes (EKS)"`` -> ``"kubernetes"``) — again, only
       used on an actual vocab hit. If that ALSO misses, each
       parenthetical's own content in turn (``"AWS MWAA (Airflow)"`` ->
       ``"airflow"``) — but ONLY when the outer phrase is empty or itself
       vocab-adjacent (``_outer_phrase_is_vocab_adjacent``): a name-bearing
       outer phrase (``"Casey Rivera (Python)"``, ``"Rivera (psql)"``,
       ``"Casey (Kafka Streams)"``) is NOT allowed to reach into its own
       parenthetical for a real skill term — that would inflate the
       candidate's skill match on what is, most likely, a mis-extracted
       name (security finding, post-4b re-audit LOW #2). Such a string
       falls through to (1)'s unresolved return instead.

    Invariant (security re-audit, mutation-killed; re-affirmed by the Phase
    4b dedup fix): the JD side (``skills_graph``, which now IMPORTS this
    exact function rather than keeping its own copy) and the résumé side
    (this function) MUST apply this identical transform, so
    ``_canonical_key_for_normalised`` — called from the SAME normalised
    string on both sides — always agrees on one Skill node for the same
    skill text. See ``tests/unit/test_skill_normalise_parity.py`` for the
    regression test that pins this by identity, not just by output.
    """
    s = raw.strip().lower()
    full_clean = _clean(s)

    resolved = _resolve_or_none(full_clean)
    if resolved is not None:
        return resolved

    inner_candidates = [m for m in _PAREN_RE.findall(s) if m.strip()]
    if inner_candidates:
        outer_clean = _clean(_PAREN_RE.sub(" ", s))
        resolved = _resolve_or_none(outer_clean)
        if resolved is not None:
            return resolved
        if _outer_phrase_is_vocab_adjacent(outer_clean):
            for inner in inner_candidates:
                resolved = _resolve_or_none(_clean(inner))
                if resolved is not None:
                    return resolved

    return full_clean


# ---------------- deterministic skill matching ----------------
#
# Resume skills come from a reliable deterministic scan (this) PLUS a
# best-effort LLM list, merged. The scan finds known vocabulary terms
# (alias-table keys) verbatim in the resume text — no model, no loop.
#
# Boundary = non-[A-Za-z0-9+#]. We keep + and # in the token class so
# "c++"/"c#" match as units and don't fire on substrings ("abc#"). We do
# NOT include "." — otherwise a skill ending a sentence ("PostgreSQL.")
# wouldn't match. Multi-token terms with internal dots ("node.js") still
# match because the alternation is longest-first, so the full literal is
# consumed before its "js" suffix could match separately.


@lru_cache(maxsize=1)
def _skill_scanner() -> re.Pattern[str]:
    table = _alias_table()
    terms = sorted(table.keys(), key=len, reverse=True)
    if not terms:
        return re.compile(r"(?!x)x")  # matches nothing
    alt = "|".join(re.escape(t) for t in terms)
    return re.compile(rf"(?<![A-Za-z0-9+#])(?:{alt})(?![A-Za-z0-9+#])", re.IGNORECASE)


def match_skills_in_text(text: str) -> list[str]:
    """Canonical skills found verbatim in ``text`` via the alias table,
    in first-seen order. Deterministic; no LLM."""
    table = _alias_table()
    seen: dict[str, None] = {}
    for m in _skill_scanner().finditer(text):
        canonical = table.get(m.group(0).lower())
        if canonical and canonical not in seen:
            seen[canonical] = None
    return list(seen)


def canonicalize_skill_names(names: Iterable[str]) -> list[str]:
    """Normalise + alias-resolve + dedupe a sequence of skill names,
    preserving first-seen order. Used to merge the deterministic and
    LLM-extracted skill lists into one clean set."""
    out: dict[str, None] = {}
    for name in names:
        canonical = _basic_normalise(name)
        if canonical and canonical not in out:
            out[canonical] = None
    return list(out)


def build_summary_text(extracted: JDExtracted) -> str:
    """Canonical embedding text for a JD — fed to nomic-embed-text."""
    parts = [extracted.title]
    if extracted.responsibilities:
        parts.append(" ".join(extracted.responsibilities))
    if extracted.required_skills:
        parts.append(
            "Required: " + ", ".join(s.name for s in extracted.required_skills)
        )
    if extracted.nice_to_have_skills:
        parts.append(
            "Nice to have: " + ", ".join(s.name for s in extracted.nice_to_have_skills)
        )
    return ". ".join(parts)


__all__ = [
    "build_summary_text",
    "canonicalize_skill_names",
    "match_skills_in_text",
]
