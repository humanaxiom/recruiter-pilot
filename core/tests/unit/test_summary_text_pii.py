"""Unit tests for ``src.worker.resume_tasks._build_summary_text``.

**MERGE-BLOCKING INVARIANT — PII NEVER ENTERS EMBEDDINGS.**

``_build_summary_text`` builds the exact text handed to ``embedder.embed(...)``
(``nomic-embed-text``, stored in the Neo4j vector index). Embeddings are
treated as PII-equivalent under PIPEDA/FIPPA — so this function must read
``parsed.summary`` / ``.skills`` / ``.experience`` / ``.education`` and must
**NEVER** touch ``parsed.candidate`` (the ``CandidateInfo`` holding
name/email/phone/location).

Two independent guards, because they catch different regressions:

1. A **runtime sentinel test** — unmistakable sentinel values planted on
   every ``CandidateInfo`` field (including ``location``, which a partial
   fix touching only name/email/phone would miss) must never appear in the
   returned string.
2. A **static source guard** — ``inspect.getsource`` must not contain the
   substring ``.candidate`` anywhere in the function body. This catches a
   ``.candidate`` read whose value happens not to collide with the chosen
   sentinels (e.g. a read that's assigned but discarded, or read into a
   field that isn't substring-checked).

Also pinned here: the empty-input fallback (``"candidate"``, never ``""`` —
an empty string is a degenerate embedding-cache key) and the three
truncation caps (``skills[:30]``, ``experience[:5]``, ``education[:3]``).

``src.worker.resume_tasks`` does not exist yet — this is the RED half of the
TDD cycle; the whole file is expected to fail at collection (ImportError).
"""

from __future__ import annotations

import inspect

from src.schemas import (
    CandidateInfo,
    EducationItem,
    Experience,
    ResumeParsed,
    ResumeSkill,
)
from src.worker.resume_tasks import _build_summary_text

# Unmistakable sentinels — chosen so an accidental collision with ordinary
# resume content is effectively impossible.
_SENTINEL_NAME = "SENTINEL_NAME_DO_NOT_EMBED"
_SENTINEL_EMAIL = "sentinel@do-not-embed.example"
_SENTINEL_PHONE = "555-SENTINEL-PHONE"
_SENTINEL_LOCATION = "SENTINEL_CITY"

_PII_INVARIANT_MSG = (
    "PII-IN-EMBEDDINGS INVARIANT VIOLATED: this is not a formatting nit — "
    "embeddings are treated as PII-equivalent (PIPEDA/FIPPA) and this text "
    "is fed directly to the embedding model / stored in the Neo4j vector "
    "index. _build_summary_text must NEVER read parsed.candidate."
)


def _sentinel_laden_parsed() -> ResumeParsed:
    return ResumeParsed(
        candidate=CandidateInfo(
            name=_SENTINEL_NAME,
            email=_SENTINEL_EMAIL,
            phone=_SENTINEL_PHONE,
            location=_SENTINEL_LOCATION,
        ),
        summary="Experienced backend engineer with distributed systems background.",
        skills=[ResumeSkill(name="Python"), ResumeSkill(name="Kubernetes")],
        experience=[Experience(company="Acme Corp", title="Staff Engineer")],
        education=[
            EducationItem(degree="BSc Computer Science", institution="Example U")
        ],
    )


# ── Guard 1: runtime sentinels ─────────────────────────────────────────────


def test_summary_text_never_contains_candidate_name_sentinel() -> None:
    text = _build_summary_text(_sentinel_laden_parsed())
    assert _SENTINEL_NAME not in text, _PII_INVARIANT_MSG


def test_summary_text_never_contains_candidate_email_sentinel() -> None:
    text = _build_summary_text(_sentinel_laden_parsed())
    assert _SENTINEL_EMAIL not in text, _PII_INVARIANT_MSG


def test_summary_text_never_contains_candidate_phone_sentinel() -> None:
    text = _build_summary_text(_sentinel_laden_parsed())
    assert _SENTINEL_PHONE not in text, _PII_INVARIANT_MSG


def test_summary_text_never_contains_candidate_location_sentinel() -> None:
    """The most commonly missed case: a regression that reads only
    ``.candidate.location`` (e.g. to append "based in {location}" to the
    summary for "better retrieval") would slip past a name/email/phone-only
    guard. Location gets its own dedicated assertion."""
    text = _build_summary_text(_sentinel_laden_parsed())
    assert _SENTINEL_LOCATION not in text, _PII_INVARIANT_MSG


def test_summary_text_contains_none_of_the_four_sentinels_at_once() -> None:
    """Belt-and-braces: all four checked together in one assertion so a
    partial-match debugging session can immediately see which ones leaked."""
    text = _build_summary_text(_sentinel_laden_parsed())
    leaked = [
        s
        for s in (_SENTINEL_NAME, _SENTINEL_EMAIL, _SENTINEL_PHONE, _SENTINEL_LOCATION)
        if s in text
    ]
    assert leaked == [], f"{_PII_INVARIANT_MSG} Leaked sentinel(s): {leaked!r}"


# ── Guard 2: static source guard ───────────────────────────────────────────


def test_build_summary_text_source_never_reads_dot_candidate() -> None:
    """Catches a ``.candidate`` read whose value happens not to collide with
    the sentinels chosen above (e.g. assigned to a local and silently
    dropped, or fed into a field this test suite doesn't substring-check)."""
    source = inspect.getsource(_build_summary_text)
    assert ".candidate" not in source, (
        f"{_PII_INVARIANT_MSG} inspect.getsource(_build_summary_text) "
        f"contains the substring '.candidate' — remove the access:\n{source}"
    )


# ── Degenerate-input fallback ───────────────────────────────────────────────


def test_empty_resume_parsed_returns_literal_candidate_fallback() -> None:
    """An empty ``ResumeParsed()`` (blank summary, no skills/experience/
    education) must fall back to the literal string ``"candidate"`` — never
    ``""``. An empty string is a degenerate embedding-cache key (the cache
    keys on ``sha256(f"{model}\\n{text}")``; an empty text collides across
    every candidate with no other content)."""
    assert _build_summary_text(ResumeParsed()) == "candidate"


# ── Truncation caps ──────────────────────────────────────────────────────


def test_skills_are_capped_at_30_in_the_summary_text() -> None:
    skills = [ResumeSkill(name=f"Skill{i:03d}") for i in range(35)]
    parsed = ResumeParsed(skills=skills)

    text = _build_summary_text(parsed)

    for i in range(30):
        assert f"Skill{i:03d}" in text
    for i in range(30, 35):
        assert f"Skill{i:03d}" not in text


def test_experience_is_capped_at_5_in_the_summary_text() -> None:
    experience = [
        Experience(company=f"Company{i}", title=f"Title{i}") for i in range(7)
    ]
    parsed = ResumeParsed(experience=experience)

    text = _build_summary_text(parsed)

    for i in range(5):
        assert f"Title{i}" in text
        assert f"Company{i}" in text
    for i in range(5, 7):
        assert f"Title{i}" not in text
        assert f"Company{i}" not in text


def test_education_is_capped_at_3_in_the_summary_text() -> None:
    education = [
        EducationItem(degree=f"Degree{i}", institution=f"School{i}") for i in range(5)
    ]
    parsed = ResumeParsed(education=education)

    text = _build_summary_text(parsed)

    for i in range(3):
        assert f"Degree{i}" in text
        assert f"School{i}" in text
    for i in range(3, 5):
        assert f"Degree{i}" not in text
        assert f"School{i}" not in text
