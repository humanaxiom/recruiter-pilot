"""Parse-time skill-family classifier (ROADMAP A2, Phase 3.3, slice 1).

**The load-bearing design decision** (ADR-044, not restated
here): a résumé skill name outside the 306-canonical vocabulary
(``aliases.yaml``/``categories.yaml``) is hashed one-way at projection
(``skills_graph._canonical_key_for_normalised``) and can therefore never earn
family credit (``ensure_categories`` only stamps a CLEARTEXT canonical). This
module closes that gap by classifying such names into one of the curated
``categories.yaml`` families AT PARSE TIME — where the résumé LLM call is
already being made (``src.worker.resume_tasks._extract_skills_merged``) and
there is no drain deadline — rather than at projection, whose 4-second budget
(``settings.py``, ``outbox_max_skill_resolutions_per_drain``) cannot fit an
inline per-skill LLM call. Projection itself gains NO LLM call: it only
*writes* whatever ``categories`` rode the outbox payload
(``ResumeSkill.categories``). ADR-008's invariant — the résumé side never
calls the LLM/embedder for skill resolution at projection — is unaffected by
construction, not merely by discipline.

Conservative by construction (ADR-044):

* a family the model invents (not in ``known_families()``) is dropped;
* no confident answer -> the skill's key is ABSENT from the result (never an
  empty list) -- identical graph state to a résumé parsed before this feature
  shipped, for that skill;
* at most ``_MAX_FAMILIES_PER_SKILL`` families per skill -- family credit is
  transitive across a whole résumé (``orchestrator.py`` matches ANY résumé
  skill in the family), so unbounded breadth here would amplify false
  credit;
* this module is only ever called with ``unclassified_names()``'s output, so
  an in-vocab canonical (including the four ADR-042 gate-critical
  family-less ones) is structurally unreachable -- never merely untested.

Best-effort, never fatal: mirrors ``_extract_skills_merged``'s posture
exactly. Any failure (transient LLM error, schema-invalid output, even a
stray ``ValidationError`` leaking a skill name into ``str(exc)``) yields an
empty result -- the caller's parse continues and succeeds. Every log line is
count-only; never a skill name.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from src.pipeline import skills_graph
from src.pipeline.llm import REASONING_JSON_MIN_TOKENS, validation_error_digest
from src.pipeline.skills import _basic_normalise
from src.settings import (
    Settings,
    get_settings,
    non_matchable_families_from_settings,
)

log = logging.getLogger(__name__)

# Family credit is transitive across the whole résumé (any résumé skill in a
# family credits a matching requirement -- orchestrator.py), so an unbounded
# per-skill family list would amplify false credit far past what a single
# out-of-vocab skill should earn. A named constant per ADR-044,
# not a settings tunable -- this is a reviewed safety ceiling, not a deploy
# knob.
_MAX_FAMILIES_PER_SKILL = 2

# FLOOR, not a size-of-response estimate: on a reasoning model the DISCARDED
# thinking trace counts against max_tokens before any JSON is ever emitted
# (`client.py:220-235`, ADR-021 §6 -- and `think:false` does NOT reliably
# suppress it on gpt-oss:20b, on either the OpenAI-compat or native path).
# Live probe against the tailnet gpt-oss:20b, batching the same 6 real
# out-of-vocab skill names in one `classify_families` call: at 1024 (the
# previous value) the trace consumed the whole budget, `content` came back
# empty, and the feature silently classified 0 of 6 skills. At 4096, 6 of 6.
# Do not "optimise" this back toward 1024 -- that value was proven live to
# zero out the feature, not merely untested.
_CLASSIFY_MAX_TOKENS = REASONING_JSON_MIN_TOKENS


def known_families() -> tuple[str, ...]:
    """The classifier's closed assignable set -- ``categories.yaml``'s
    top-level family names, read via ``skills_graph._category_family_names()``
    (a SEPARATE, purpose-built accessor beside ``_category_table()``, same
    ``_CATEGORIES_PATH``, same ``@lru_cache``), not a second,
    independently-maintained copy of the family list (the A7 drift shape
    this repo has recorded seventeen times).

    Deliberately NOT derived from ``_category_table()`` (canonical ->
    [families], inverted from the same file for per-skill lookup): that
    table structurally cannot see a family with zero curated skill members
    (``domain``/``other``), and closing that gap by giving either a fake
    curated member -- in the shipped, ADR-042-governed vocabulary file, or
    as a synthetic key injected into ``_category_table()``'s own dict --
    would silently widen the résumé side's in-vocab predicate
    (``_canonical_key_for_normalised`` treats ANY key in ``_category_table()``
    as in-vocabulary/cleartext, and ``unclassified_names`` below shares that
    exact predicate). Sorted, deduplicated, 32 families today (including the
    two non-matchable catch-alls, ``domain``/``other`` -- assignable here,
    since assigning either grants zero family credit per
    ``match_non_matchable_families``)."""
    return skills_graph._category_family_names()


def unclassified_names(names: list[str]) -> list[str]:
    """The names, in order (dupes NOT removed -- callers already dedupe),
    that ``skills_graph._canonical_key_for_normalised`` would hash rather
    than keep cleartext -- i.e. genuinely out of vocabulary.

    Pure, no I/O. Deliberately does NOT restate the alias/category lookup:
    it normalises with the exact same ``_basic_normalise`` and asks the
    exact same ``_canonical_key_for_normalised`` predicate the résumé
    projection path (``skills_graph.resume_skill_canonical_key``) uses, so
    the two can never independently drift on what counts as "in vocab" --
    the drift shape ``test_unclassified_names_drift_guard_cannot_silently_diverge``
    pins.
    """
    out: list[str] = []
    for name in names:
        normalised = _basic_normalise(name)
        if not normalised:
            continue
        key = skills_graph._canonical_key_for_normalised(normalised)
        if key.startswith(skills_graph._HASH_KEY_PREFIX):
            out.append(name)
    return out


class _ClassifyFamiliesOut(BaseModel):
    """Strict schema for the batched classifier call. ``categories`` maps
    the EXACT skill name (as offered in the prompt) to the family/families
    the model assigned it -- an absent name, or an explicit empty list, both
    mean "no confident answer" and are handled identically by the caller.
    """

    categories: dict[str, list[str]] = Field(default_factory=dict)


def _build_messages(
    names: list[str],
    families: tuple[str, ...],
    settings: Settings | None = None,
) -> list[dict[str, str]]:
    """``settings`` defaults to ``get_settings()`` so existing call sites
    that only pass ``(names, families)`` still get the real, configured
    non-matchable set -- never a hardcoded ``{"other", "domain"}`` literal
    here."""
    non_matchable = non_matchable_families_from_settings(settings or get_settings())
    non_matchable_offered = ", ".join(non_matchable)
    offered = ", ".join(families)
    listed = "\n".join(f"- {n}" for n in names)
    return [
        {
            "role": "system",
            "content": (
                "You classify résumé skill names into broad skill families "
                "for a candidate-ranking system. For EACH skill name given, "
                "choose at most two families from this CLOSED list, ONLY "
                f"from this list, never inventing a new one: {offered}. "
                "A wrong specific family is actively harmful: family credit "
                "is transitive across the WHOLE résumé, so a doubtful guess "
                "can falsely credit a candidate with a requirement they do "
                "not meet, while declining costs nothing. For that reason, "
                f"PREFER a non-matchable family ({non_matchable_offered}) "
                "over a specific matchable family whenever the specific "
                "choice is doubtful -- treat a non-matchable family as "
                "safer than a specific guess you are unsure of, and as the "
                "default answer for an uncertain name, not merely a last "
                "resort reached only before guessing. Only fall back to an "
                "empty list if even the non-matchable families clearly do "
                "not fit. "
                'Respond with strict JSON: {"categories": '
                '{"<skill name>": ["<family>", ...]}} with one entry per '
                "skill name given, using the exact skill name text as the "
                "key. No prose, no fences."
            ),
        },
        {
            "role": "user",
            "content": f"Skill names:\n{listed}\n\nReturn JSON only.",
        },
    ]


async def classify_families(
    llm: Any, names: list[str], *, settings: Settings
) -> dict[str, list[str]]:
    """ONE batched ``chat_json`` call classifying every name in ``names`` --
    never one call per name (a résumé can carry dozens of out-of-vocab
    names, and the skills LLM call already happens once per résumé at this
    same call site).

    ``settings`` is keyword-only, per the spec'd signature, and is threaded
    into ``_build_messages`` to source the non-matchable family set
    (``settings.match_non_matchable_families`` via
    ``non_matchable_families_from_settings``) -- every other tunable here
    (the family cap, the token budget) stays a reviewed constant, not a
    deploy knob, per ADR-044.

    Best-effort: any exception (transient LLM failure, schema-invalid
    output, or even a stray ``pydantic.ValidationError``) is caught here and
    converted to an empty result -- this function never raises. Logs are
    count-only; a skill name is never interpolated into a log line, and
    ``validation_error_digest`` is used for a ``ValidationError`` specifically
    because pydantic v2's ``str(ValidationError)`` embeds the offending
    value (here, potentially a skill name) verbatim.
    """
    if not names:
        return {}

    messages = _build_messages(names, known_families(), settings)
    try:
        out = await llm.chat_json(
            messages,
            _ClassifyFamiliesOut,
            max_tokens=_CLASSIFY_MAX_TOKENS,
            max_retries=1,
        )
    except ValidationError as exc:
        log.warning(
            "skill_classifier.classify_failed count=%d error=%s",
            len(names),
            validation_error_digest(exc),
        )
        return {}
    except Exception:  # noqa: BLE001 — best-effort, mirrors
        # skills_graph._ask_llm_tiebreaker's non-fatal posture. Count-only:
        # never log a skill name (F8 discipline, skills_graph.py).
        log.warning("skill_classifier.classify_failed count=%d", len(names))
        return {}

    known = set(known_families())
    result: dict[str, list[str]] = {}
    for name, offered_cats in out.categories.items():
        kept = [c for c in offered_cats if c in known][:_MAX_FAMILIES_PER_SKILL]
        if kept:
            result[name] = kept
    return result
