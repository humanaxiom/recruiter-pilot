"""Skill normalisation — the Neo4j-backed half, deferred out of Phase 3.

Ported behaviourally from hris ``apps/worker/src/worker/skill_normalize.py``
(the vector-match / LLM-tiebreaker / Skill-graph half hris's ``normalize_skill``
covers).

Phase 4b defect fix (post-merge, `feat/phase-4b-graph-projection`): the
"basic normalisation" section below (``_alias_table``/``_clean``/
``_resolve_or_none``/``_basic_normalise``) used to be a byte-for-byte
duplicate of ``src.pipeline.skills``'s copy, "kept in sync by convention"
per the (now-removed) comments here and there. Nothing enforced that, the
two copies had already textually diverged (no functional difference yet,
but no test caught it either), and any future edit to one copy without the
other silently breaks the invariant that
``_canonical_key_for_normalised`` requires: the JD side and the résumé
side MUST derive the identical normalised string, or ``REQUIRES``/
``HAS_SKILL`` stop meeting at the same node and that skill silently scores
zero for every candidate. This module now IMPORTS those four names from
``src.pipeline.skills`` instead of redefining them — one function, one
definition, no possible divergence. There is no import cycle:
``src.pipeline.skills`` depends only on ``src.schemas.jobs``/``yaml``/
stdlib, never on this module or on ``src.settings``. See
``tests/unit/test_skill_normalise_parity.py`` for the regression test
(mutation-proven: the test fails if either side's derived key is ever
allowed to diverge from the other's, including via a future re-duplication
of these functions).

ADR-008 — architectural PII fix (supersedes rounds 2-5 of the security
re-audit below, which is kept here ONLY as a historical record of why the
heuristic approach was abandoned):

Rounds 2-5 of the security re-audit tried to pattern-match PII (specifically,
a candidate's own name) out of an untrusted, LLM-authored skill-name string —
a name-shape detector, an offline personal-name lexicon, a vendor-prefix
veto, and a "strict mode" collapse, stacked five deep. Every fix to one side
(privacy) broke the other (recall), and vice versa: round 3 fixed privacy and
broke recall, round 4 fixed recall and reopened the leak, round 5 fixed
recall and reopened it again (an ``any()``/``all()`` quantifier bug let
``Sean Kvistad``/``Torbjorn Kvistad``/``Ludovica Brambilla`` through, and the
vendor veto let ``IBM John Smith``/``Google Кейси Ривера`` through). **You
cannot reliably pattern-match PII out of an untrusted free-text field.**

The fix instead eliminates the class BY CONSTRUCTION, exploiting one
asymmetry: a job description carries no candidate PII — only the résumé side
can leak a candidate's identity into the graph.

* **``canonical_key`` (the Skill node's MERGE key / unique constraint)** is
  either the vocab canonical term, CLEARTEXT (a vocab hit makes identity
  DENIABLE and unembedded, not absent — see ADR-008 residual #13: a name that
  collides with a real vocab term like ``julia``/``hudson`` still lands as a
  cleartext key, just an ambiguous, shared, un-embedded one), or, for a
  non-vocab name, ``h:`` + a salted sha256 hash of the normalised string (see
  ``_hash_key`` / ``settings.skill_hash_salt``). Both sides compute this key
  from the SAME normalised string via the SAME function
  (``_canonical_key_for_normalised``), so ``REQUIRES``/``HAS_SKILL`` still
  meet at the same node — no JD requirement silently vanishes.
* **``display_name``** (cleartext, human-readable) is written ONLY by the
  job/JD side (``src.worker.tasks._job_projection_tx``) — a job description
  carries no candidate identity, so this is always safe. The résumé side
  (``src.worker.resume_tasks._resume_projection_tx``) MERGEs on
  ``canonical_key`` alone and never sets this field.
* **The résumé side never calls the embedder or the LLM for skill-name
  resolution at all** — see ``resume_skill_canonical_key``, a pure function.
  A résumé-derived non-vocab skill name is NEVER embedded, so it can never
  surface as a Neo4j vector-search near-candidate either — the leak
  (``"Casey Rivera"`` extracted as a "skill") becomes a ``h:<hash>`` node
  with no display name, no embedding, no categories, and no JD ever requires
  it: unreadable, un-invertible, contributes nothing to any score.
* **Accepted residual** — a non-vocab skill loses vector auto-merge /
  synonym resolution for its FINAL (create-new) resolution: two different
  spellings of the same unlisted skill hash to two different keys and never
  connect, unless one is a literal alias of a vocab term. Vocab skills keep
  the full canonicalisation path (exact/alias match, then vector-match/LLM-
  tiebreak against other EXISTING canonical nodes, same as before this
  change) — only the terminal "nothing matched, mint a new canonical key"
  step is different for a non-vocab name.
* The disparate-impact problem (round 3's S1/S4/S5 widening) disappears
  entirely: this module no longer guesses what a person's name looks like,
  so there is no shape heuristic left to be biased against any naming
  convention.

DELETED as part of this fix (dead weight the moment the class is eliminated
by construction): the name-shape decomposer, the offline personal-name
lexicon and ``skill_data/person_names.txt``, the vendor-prefix veto, the
non-Latin-script fallback, and the ``strict_lexicon`` parameter.
``reject_reason_for_skill_name`` keeps ONLY the cheap, load-bearing-for-
nothing-but-junk-filtering email/phone-shape and length/token-count checks
(kept because they cost nothing and stop obvious junk becoming a graph node
— NOT because they do any privacy work any more; the hash-keying above is
what does that).

Also folds in two hris hardening findings, and the earlier deviations from
``docs/EXTRACTION_PLAN.md`` (4b row), both unaffected by ADR-008:

* **Decision 3 — resolution runs OUTSIDE any Neo4j write transaction.** hris's
  ``_resolve_canonical`` takes a managed ``tx`` and calls ``embedder.embed`` /
  ``llm.chat_json`` from inside it — up to 40 sequential local-model round
  trips held under one write lock, against a 5s cron. Here,
  ``resolve_canonical_names`` (job side only, post-ADR-008) takes a plain
  Neo4j ``session`` and issues every Cypher statement via auto-commit
  ``session.run(...)`` — never ``session.execute_write``/``execute_read``.
  Callers resolve every skill name BEFORE opening a write transaction, then
  pass only the resolved ``{raw_name: canonical_key}`` mapping into the write
  callback.
* **Decision 4 — the LLM tiebreaker's answer is constrained to the ``near``
  candidate set.** hris's Cypher ``MATCH (s:Skill {canonical_name: $c})``
  against a hallucinated ``$c`` matches nothing and is a silent no-op — the
  skill's edge never gets created, no error, no log (R5). Here, a tiebreaker
  answer that is not one of the offered ``near`` candidates' own names is
  rejected outright and treated exactly like "no match": fall through to
  create-new, so a real Skill node backing the returned name always exists.
* **Thresholds are settings, not hard-coded module constants** — hris's
  ``AUTO_MERGE_THRESHOLD = 0.92`` / ``TIEBREAKER_THRESHOLD = 0.88`` become
  ``settings.skill_auto_merge_threshold`` / ``settings.skill_tiebreaker_threshold``,
  read fresh on every call (never cached at import time) so a settings
  override actually changes behaviour.
* **R4 — ``categories.yaml`` ships inside the Docker build context.**
  ``core/src/pipeline/skill_data/categories.yaml``, beside the existing
  ``aliases.yaml`` — a repo-root ``infra/skills/`` path (hris's resolution) is
  invisible to the ``./core`` build context. A missing file degrades
  gracefully to ``{}``/``[]``, never crashes.
* **R6 — alias updates are dedupe-safe.** hris's
  ``SET s.aliases = coalesce(s.aliases, []) + [$alias]`` never de-dupes, so a
  re-parse appends the same alias forever. The Cypher here guards with a
  ``CASE WHEN $alias IN coalesce(s.aliases, [])`` check.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, Field

from src.pipeline.skills import _ALIASES_PATH, _alias_table, _basic_normalise
from src.settings import get_settings

log = logging.getLogger(__name__)

_CATEGORIES_PATH = Path(__file__).resolve().parent / "skill_data" / "categories.yaml"

# Vector recall breadth for the near-candidate query — matches hris's
# `db.index.vector.queryNodes('skill_emb_idx', 5, ...)`.
_NEAR_CANDIDATE_LIMIT = 5

# ADR-008: these cheap shape checks are KEPT, but are no longer load-bearing
# for privacy (that job now belongs to canonical-key hashing, below) — they
# are pure junk filtering. A legitimate skill name is short: 200 chars of free
# text, or a dozen tokens, is not a skill; it is exactly the shape a
# looping/hallucinating small model produces when it copies a header block
# into a "skill". Checked against the RAW input (never the alias/punctuation
# -normalised form — `_basic_normalise` strips "@", which would defeat the
# email check below).
_MAX_SKILL_NAME_CHARS = 60
# Round 2 (security re-audit): widened 6 -> 8. Zero of the 220 shipped vocab
# terms trip the OLD 6-token cap, so this is pure headroom for a legitimate
# multi-word certification name (e.g. a 7-token cert), not a regression.
_MAX_SKILL_NAME_TOKENS = 8
# S6 (security re-audit round 3): tolerate whitespace around the separator,
# and an "(at)"/"[at]" obfuscation of "@" — "john.smith @ corp.test" and
# "casey.rivera (at) example.test" both carry a real email address that the
# OLD strict, whitespace-intolerant `local@domain` pattern missed outright.
_EMAIL_SHAPE_RE = re.compile(
    r"[^\s@]+\s*(?:@|\(at\)|\[at\])\s*[^\s@]+\.[^\s@]+", re.IGNORECASE
)
# A contiguous run of digits/phone-punctuation (no letters) that carries at
# least 7 digits — long enough to be a real phone number, short enough that
# a legitimate skill name's occasional digit ("ISO 27001", "IPv6") never
# trips it (those digits aren't contiguous with dashes/spaces/parens).
_PHONE_RUN_RE = re.compile(r"[+()\-.\s\d]{7,}")


class UnresolvedSkillNameError(RuntimeError):
    """Raised by a projection write-tx callback when it looks up a skill
    name that ``resolve_canonical_names`` was never asked to resolve at all
    — a programming error (a name missing from the resolved mapping is NOT
    the same outcome as a legitimate no-near-candidate or PII-shape-rejected
    resolution, both of which DO have an entry). F6 (security re-audit):
    hris's ``resolved_skills.get(name, name)`` silently falls back to the
    UNRESOLVED raw name, which matches no ``Skill`` node in Cypher and the
    HAS_SKILL/REQUIRES edge silently vanishes (R5's exact failure class,
    reintroduced). Fail loud instead — never include the skill name itself
    in the message (it may be PII; see F3)."""


def _looks_like_phone(name: str) -> bool:
    for m in _PHONE_RUN_RE.finditer(name):
        run = m.group()
        digits = sum(ch.isdigit() for ch in run)
        if digits >= 7:
            return True
    return False


def _skill_log_ref(canonical: str) -> str:
    """S8 (security re-audit round 3): an ACCEPTED skill-normalisation log
    line (auto-merged / LLM-merged / newly created) previously logged
    ``canonical=%s`` verbatim — exactly as much of an R3 violation as
    logging a REJECTED name would be, since a résumé header name that slips
    past the shape+vocab guard (S9's documented residual, or a future gap)
    still lands in the log line-for-line the moment it's accepted. This
    returns a short, non-reversible reference (12 hex chars of sha256) that
    still lets two log lines about the SAME skill be correlated, without
    ever printing the name. Not a cryptographic guarantee against a
    small-vocabulary brute force — a mitigation, same class as the
    category-only discipline `reject_reason_for_skill_name` already uses
    for the rejected side."""
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


def reject_reason_for_skill_name(name: str) -> str | None:
    """The shape-only reject decision for a skill name — shared by
    ``_resolve_one`` (this module, job side) AND
    ``src.worker.resume_tasks._extract_skills_merged``/``resume_skill_canonical_key``
    (which call this directly on a skill name outside any Neo4j write).
    Returns a rejection-reason CATEGORY (``email_shape`` / ``phone_shape`` /
    ``length_or_token_cap``), never the value that triggered it (R3
    discipline) — or ``None`` when the name is fine.

    ADR-008: this is deliberately NOT a privacy control any more (see the
    module docstring) — rounds 2-5's person-name-shape/lexicon/vendor-veto
    machinery is gone. What is left is pure junk filtering: a name that is
    implausibly long/verbose, or shaped like an email address or a phone
    number, is rejected before it is ever handed to the embedder or written
    to Neo4j — cheap, and it stops obvious garbage becoming a graph node —
    but a name that IS the candidate's own identity (e.g. "Casey Rivera")
    sails straight through this function now. That is fine: the résumé-side
    projection layer (``resume_skill_canonical_key`` / the hash-keying it
    does) is what actually stops that name from ever landing as cleartext or
    an embedding in the graph, regardless of what this function says.
    """
    if len(name) > _MAX_SKILL_NAME_CHARS:
        return "length_or_token_cap"
    if len(name.split()) > _MAX_SKILL_NAME_TOKENS:
        return "length_or_token_cap"
    if _EMAIL_SHAPE_RE.search(name):
        return "email_shape"
    if _looks_like_phone(name):
        return "phone_shape"
    return None


# ---------------- ADR-008: canonical-key hashing (the privacy control) ------


_HASH_KEY_PREFIX = "h:"
_HASH_KEY_HEX_LEN = 32


def _hash_key(normalised: str) -> str:
    """The non-vocab half of ``canonical_key``: ``h:`` + a salted sha256 hash
    of ``normalised``, truncated to ``_HASH_KEY_HEX_LEN`` hex chars — long
    enough to be collision-safe for a skill-name-sized keyspace, short enough
    to stay a reasonable Neo4j property value. The ``h:`` prefix makes an
    opaque key visually obvious in any Cypher browser/log (never mistakable
    for a cleartext vocab term).

    The salt is mixed in specifically so the hash is NOT dictionary-
    attackable: without it, anyone who can read the graph could precompute
    ``sha256(candidate_name)`` for a list of common names and confirm one is
    present just by checking whether that key exists. Read fresh from
    settings on every call (never cached at import time), matching this
    module's existing threshold-reading discipline.

    Fails loud (mirrors ``src.worker.main.startup``'s ``PII_KEY`` check) on
    an empty salt — belt-and-braces alongside that startup check, since this
    function may in principle be reached from a context that bypassed it.
    """
    settings = get_settings()
    salt = settings.skill_hash_salt
    if not salt:
        raise RuntimeError(
            "skill_hash_salt is empty — refusing to hash a skill name; an "
            "unsalted hash is dictionary-attackable. Set SKILL_HASH_SALT."
        )
    digest = hashlib.sha256(f"{salt}{normalised}".encode()).hexdigest()
    return f"{_HASH_KEY_PREFIX}{digest[:_HASH_KEY_HEX_LEN]}"


def _canonical_key_for_normalised(normalised: str) -> str:
    """The single function BOTH the job side and the résumé side call to
    turn an already-normalised skill name into a ``canonical_key`` — a vocab
    term is kept cleartext (deniable/ambiguous, not PII-free by definition:
    see ADR-008 residual #13), anything else is hashed. Called from the SAME
    normalised string on both sides (``_basic_normalise``), so
    ``REQUIRES``/``HAS_SKILL`` always meet at the same Skill node for a given
    skill name."""
    if normalised in _alias_table() or normalised in _category_table():
        return normalised
    return _hash_key(normalised)


def resume_skill_canonical_key(name: str) -> str | None:
    """The ENTIRE résumé-side skill-name -> ``canonical_key`` resolution —
    a PURE function: no Neo4j session, no embedder, no LLM. Returns ``None``
    when ``name`` is shape-rejected (junk, per ``reject_reason_for_skill_name``)
    or normalises to nothing.

    This is the ADR-008 fix's core: a résumé-derived skill name is never
    handed to the embedder and never vector-searched against the graph — it
    is either an exact vocabulary/alias hit (cleartext, safe, a closed set)
    or it gets hashed. Either way, no I/O, so a caller can compute the whole
    ``{raw_name: canonical_key}`` mapping locally before ever opening a
    Neo4j session (see ``src.worker.resume_tasks.project_resume``).
    """
    if reject_reason_for_skill_name(name) is not None:
        return None
    normalised = _basic_normalise(name)
    if not normalised:
        return None
    return _canonical_key_for_normalised(normalised)


# ---------------- basic normalisation ---------------------------------------
#
# `_alias_table`/`_basic_normalise` are imported from `src.pipeline.skills`
# (single source of truth — see the module docstring). Both this module's
# `_canonical_key_for_normalised`/`resume_skill_canonical_key` and
# `src.pipeline.skills.canonicalize_skill_names` now call the exact same
# function object, so the JD side and the résumé side can never disagree on
# a skill's normalised form.


# ---------------- categories.yaml (curated skill families) ------------------


@lru_cache(maxsize=1)
def _category_table() -> dict[str, list[str]]:
    """canonical_name -> [family, ...], inverted from categories.yaml
    (family -> [skills]). Degrades gracefully to {} when the file is
    missing — matches the aliases.yaml precedent in src.pipeline.skills."""
    if not _CATEGORIES_PATH.is_file():
        log.warning("skill_categories.missing path=%s", _CATEGORIES_PATH)
        return {}
    data = yaml.safe_load(_CATEGORIES_PATH.read_text(encoding="utf-8")) or {}
    out: dict[str, list[str]] = {}
    for family, skills in data.items():
        fam = str(family).strip().lower()
        for skill in skills or []:
            canonical = str(skill).strip().lower()
            out.setdefault(canonical, [])
            if fam not in out[canonical]:
                out[canonical].append(fam)
    return out


@lru_cache(maxsize=1)
def _category_family_names() -> tuple[str, ...]:
    """The file's TOP-LEVEL family names, read straight from
    ``categories.yaml`` -- a SECOND, purpose-built VIEW of the same on-disk
    source ``_category_table()`` reads (same ``_CATEGORIES_PATH``, same
    ``@lru_cache``, same graceful-degrade-to-empty posture), not a second,
    independently-maintained copy of the family list.

    ``_category_table()`` inverts family -> [skills] into canonical ->
    [families] for per-skill lookup (``categories_for``/``ensure_categories``)
    and therefore structurally cannot see a family with zero curated skill
    members (``domain``/``other``, per the yaml header) -- that is a
    limitation of the INVERTED view, not a reason to give either family a
    fake curated member. Adding one (to the shipped, ADR-042-governed
    vocabulary file, or as a synthetic key injected into
    ``_category_table()``'s own dict) would silently widen the résumé side's
    in-vocab predicate: ``_canonical_key_for_normalised`` treats ANY key in
    ``_category_table()`` as in-vocabulary/cleartext, and
    ``skill_classifier.unclassified_names`` shares that exact predicate.
    ``skill_classifier.known_families()`` reads THIS accessor instead, for
    exactly that reason.
    """
    if not _CATEGORIES_PATH.is_file():
        log.warning("skill_categories.missing path=%s", _CATEGORIES_PATH)
        return ()
    data = yaml.safe_load(_CATEGORIES_PATH.read_text(encoding="utf-8")) or {}
    return tuple(sorted({str(family).strip().lower() for family in data}))


def categories_for(canonical: str) -> list[str]:
    """Curated families for a canonical skill name, or [] if not seeded."""
    return list(_category_table().get(canonical.strip().lower(), []))


async def ensure_categories(tx: Any, canonical: str) -> None:
    """Stamp a Skill node with its CURATED families so stage-2 ontology
    partial-credit has something to read. Curated wins; skills with no
    curated family are simply left uncategorised (no LLM backfill in v1 —
    EXTRACTION_PLAN's `skill_category_task` deferral)."""
    cats = categories_for(canonical)
    if cats:
        await tx.run(
            "MATCH (s:Skill {canonical_key: $c}) SET s.categories = $cats",
            c=canonical,
            cats=cats,
        )


# ---------------- LLM tiebreaker --------------------------------------------


class _LLMTiebreakerOut(BaseModel):
    """Strict schema for the LLM normaliser. ``match`` is the canonical name
    to merge into, or null to indicate 'create new'."""

    match: str | None = Field(default=None)


async def _ask_llm_tiebreaker(
    llm: Any, candidate: str, near: list[dict[str, Any]]
) -> str | None:
    """0.88-0.92 (settings-configured) cosine grey zone: ask the LLM to
    decide. Returns one of the candidate canonical names, or None to
    indicate 'these are not the same skill, create new'. Non-fatal: an LLM
    failure is logged and treated as no match, matching hris."""
    choices = "\n".join(
        f"- {n['name']} (aliases: {', '.join(n.get('aliases', []))})" for n in near
    )
    messages = [
        {
            "role": "system",
            "content": (
                "You disambiguate skill names. Given a candidate and a "
                "shortlist of similar existing canonical skills, decide "
                "whether the candidate is the SAME skill as one of them. "
                'Respond with strict JSON: {"match": "<canonical_name>"} '
                'if it matches one, or {"match": null} if none match. '
                "No prose, no fences."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Candidate: {candidate!r}\n\n"
                f"Existing skills (top vector-similar):\n{choices}\n\n"
                "Return JSON only."
            ),
        },
    ]
    try:
        out = await llm.chat_json(messages, _LLMTiebreakerOut, max_tokens=128)
    except Exception:  # noqa: BLE001 — non-fatal by design, matches hris
        # F8 (security re-audit round 2): never log the raw candidate value
        # (potentially PII-shaped, per F3) — count-only.
        log.warning("skill_normalize.tiebreaker_failed")
        return None
    match: str | None = out.match
    return match


# ---------------- alias update (R6: dedupe-safe) -----------------------------

_ALIAS_UPDATE_CYPHER = (
    "MATCH (s:Skill {canonical_key: $c}) "
    "SET s.aliases = CASE WHEN $alias IN coalesce(s.aliases, []) "
    "THEN coalesce(s.aliases, []) "
    "ELSE coalesce(s.aliases, []) + [$alias] END"
)


async def _alias_update(session: Any, canonical: str, alias: str) -> None:
    await session.run(_ALIAS_UPDATE_CYPHER, c=canonical, alias=alias)


# ---------------- main entry point ------------------------------------------


async def resolve_canonical_names(
    session: Any,
    names: Iterable[str],
    *,
    llm: Any,
    embedder: Any,
) -> dict[str, str | None]:
    """Resolve every name in ``names`` to a canonical Neo4j ``Skill`` key.

    ADR-008: this is now the JOB/JD side ONLY — a job description carries no
    candidate identity, so embedding/vector-searching its skill names is
    always safe. The résumé side never calls this (see
    ``resume_skill_canonical_key``, a pure, I/O-free function).

    Runs entirely via auto-commit ``session.run`` (Decision 3) — never
    ``session.execute_write``/``execute_read``, so this can safely be called
    BEFORE a caller opens its own write transaction. Returns a mapping keyed
    by the exact INPUT string (not a re-normalised form) so callers can look
    up ``resolved[skill["name"]]`` with the raw name they already hold.

    A value of ``None`` means ``raw`` was shape-rejected as junk (see
    ``reject_reason_for_skill_name``) — the caller must skip projecting that
    skill/edge entirely, NOT fall back to the raw name (see
    ``UnresolvedSkillNameError``/F6). Every name in ``names`` gets an entry,
    rejected or not, so a caller can tell "rejected" (key present, value
    ``None``) apart from "never resolved at all" (key absent — a caller bug).
    """
    resolved: dict[str, str | None] = {}
    for raw in names:
        resolved[raw] = await _resolve_one(session, raw, llm=llm, embedder=embedder)
    return resolved


async def _resolve_one(
    session: Any, raw: str, *, llm: Any, embedder: Any
) -> str | None:
    """F1 (security re-audit round 2, post-ADR-008): ``canonical_key`` is a
    PURE function of ``normalised`` (``_canonical_key_for_normalised``) and
    this function returns EXACTLY that value on every non-rejected branch —
    never a graph query result. The résumé side
    (``resume_skill_canonical_key``) computes the SAME key with no graph
    access at all, so this is the only way ``REQUIRES``/``HAS_SKILL`` are
    guaranteed to meet at the same node for every branch, not just
    create-new.

    Pre-fix, three of the four branches below returned an EXISTING node's
    own key (the exact/alias match's row, the auto-merge near-candidate's
    name, the LLM tiebreaker's confirmed match) instead of this pure key —
    a job requirement resolved through any of those branches diverged from
    the résumé side's hash, silently zeroing that skill's score for every
    candidate who genuinely has it. Worse, the auto-merge/LLM branches then
    PERSISTED the divergence via ``_alias_update``, so once (e.g.) "react
    native" auto-merged into the "react" node, every future JD mentioning it
    took the now-poisoned exact-match branch too — permanent, self
    -reinforcing drift.

    Vector auto-merge and the LLM tiebreaker are KEPT (still useful,
    job-side only, since a JD carries no candidate PII) but DEMOTED to
    alias/display-name enrichment of the near-matched node only — they may
    never change what this function returns.
    """
    reason = reject_reason_for_skill_name(raw)
    if reason is not None:
        # R3 discipline: category only, never the value.
        log.warning("skill_normalize.pii_shaped_name_rejected reason=%s", reason)
        return None

    settings = get_settings()
    normalised = _basic_normalise(raw)
    if not normalised:
        return None

    # The pure key, computed FIRST and returned on every branch below.
    canonical_key = _canonical_key_for_normalised(normalised)

    # 1. Fast path: a node already exists at this EXACT key (a vocab node, or
    # a hash node a previous call already created) — nothing left to decide,
    # and no embed()/vector/LLM round trip needed. L2 (security re-audit):
    # a single indexed lookup on `canonical_key` — no `OR $n IN s.aliases`
    # (that clause is exactly the mechanism that let an alias-learned node
    # redirect the key pre-fix, and it also defeated the index).
    exact = await session.run(
        "MATCH (s:Skill {canonical_key: $n}) RETURN s.canonical_key AS name LIMIT 1",
        n=canonical_key,
    )
    row = await exact.single()
    if row:
        return canonical_key

    # 2. Vector match — ENRICHMENT ONLY. Safe on the job side regardless of
    # vocab status (a JD carries no candidate PII). A high-scoring near
    # candidate gets `normalised` added to ITS alias list (so the graph still
    # records the synonym relationship for humans/analytics), but the key
    # this function returns is always `canonical_key` above — never the near
    # candidate's own key.
    [emb] = await embedder.embed([normalised])
    near_cursor = await session.run(
        """
        CALL db.index.vector.queryNodes('skill_emb_idx', $k, $e)
        YIELD node, score WHERE score > $low_threshold
          AND node.canonical_key IS NOT NULL
        RETURN node.canonical_key AS name,
               coalesce(node.aliases, []) AS aliases,
               score
        ORDER BY score DESC
        """,
        k=_NEAR_CANDIDATE_LIMIT,
        e=emb,
        low_threshold=settings.skill_tiebreaker_threshold,
    )
    near = [dict(r) async for r in near_cursor]

    if near and near[0]["score"] >= settings.skill_auto_merge_threshold:
        enrich_target = str(near[0]["name"])
        await _alias_update(session, enrich_target, normalised)
        # S8 (round 3): NOT the canonical name either — a hash reference
        # only (see `_skill_log_ref`'s docstring for why "canonical only"
        # was still an R3 violation for an ACCEPTED name).
        log.debug(
            "skill_normalize.auto_merged ref=%s score=%s",
            _skill_log_ref(enrich_target),
            near[0]["score"],
        )
    elif near:
        # 3. Grey zone -> LLM tiebreaker — ENRICHMENT ONLY, same discipline as
        # the auto-merge branch above. Decision 4: the answer must be one of
        # the OFFERED near candidates, or it is treated as no match — a
        # hallucinated name is never used to MATCH an existing node, and (per
        # F1) even a VALID match is never used to redirect the returned key.
        valid_names = {str(n["name"]) for n in near}
        match = await _ask_llm_tiebreaker(llm, normalised, near)
        if match is not None and match in valid_names:
            await _alias_update(session, match, normalised)
            # S8 (round 3): hash reference only, never the name itself.
            log.info(
                "skill_normalize.llm_merged ref=%s top_score=%s",
                _skill_log_ref(match),
                near[0]["score"],
            )
        elif match is not None:
            # F8: no value logged at all — the hallucinated match is never a
            # real Skill name and `raw` may be PII-shaped.
            log.warning("skill_normalize.tiebreaker_hallucinated answer_rejected=true")

    # 4. Ensure a node exists at the pure key itself — a vocab node that may
    # already have been created under a different raw spelling, or a brand
    # new hash node for a genuinely novel non-vocab name. ADR-008: the KEY is
    # the vocab canonical term (cleartext) if `raw` is a vocab hit, else a
    # salted hash — computed via the SAME `_canonical_key_for_normalised` the
    # résumé side uses, so a brand-new non-vocab skill's key agrees on both
    # sides. The embedding is still written unconditionally here (job side
    # is always safe to embed).
    await session.run(
        "MERGE (s:Skill {canonical_key: $n}) "
        "ON CREATE SET s.aliases = [$alias], s.embedding = $e",
        n=canonical_key,
        alias=normalised,
        e=emb,
    )
    # S8 (round 3): hash reference only, never the name itself.
    log.info("skill_normalize.created ref=%s", _skill_log_ref(canonical_key))
    return canonical_key


# `_ALIASES_PATH` is re-exported (imported, not redefined) from
# `src.pipeline.skills` — the single source of truth for the shipped
# aliases.yaml location — kept accessible as `skills_graph._ALIASES_PATH` only
# so the pre-existing test that resolves the real YAML that way keeps
# working unmodified.
__all__ = [
    "_ALIASES_PATH",
    "UnresolvedSkillNameError",
    "categories_for",
    "ensure_categories",
    "reject_reason_for_skill_name",
    "resolve_canonical_names",
    "resume_skill_canonical_key",
]
