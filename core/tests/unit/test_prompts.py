"""Unit tests for the versioned Jinja2 prompt loader (``src/prompts/``).

Ported from hris ``packages/prompts/src/prompts/__init__.py``
(``phase3-source-dossier.md`` §9). Phase 3 ported four prompt pairs:
``jd_extract_v1``, ``resume_core_v1``, ``resume_skills_v2``,
``cover_letter_v1``. Phase 4c adds the matching stage-3 evidence pair
``shortlist_evidence_v1`` (and its cover-letter variant
``shortlist_evidence_v2``), so the loader now reports six versions.

Variable fixtures for each template were built by reading the actual
hris ``.user.j2`` sources (not guessed): ``jd_extract_v1`` needs
``jd_text: str``; the other three need ``chunks: list[ResumeChunk]``
(rendered via ``chunk.id`` / ``chunk.section`` / ``chunk.page`` /
``chunk.text``).
"""

from __future__ import annotations

from pathlib import Path

import jinja2
import pytest

from src.prompts import RenderedPrompt, list_prompts, load_prompt
from src.schemas.resumes import ResumeChunk

# core/tests/unit/test_prompts.py -> parents[2] == core/
_TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "src" / "prompts" / "templates"

EXPECTED_PROMPT_NAMES: tuple[str, ...] = (
    "cover_letter_v1",
    "jd_extract_v1",
    "resume_core_v1",
    "resume_skills_v2",
    "shortlist_evidence_v1",
    "shortlist_evidence_v2",
)


def _chunks() -> list[ResumeChunk]:
    return [
        ResumeChunk(
            id="c_000", section="experience", page=0, text="Built things at Acme."
        ),
        ResumeChunk(
            id="c_001", section="skills", page=0, text="Python, SQL, Kubernetes."
        ),
    ]


# ── Renders all four pairs without StrictUndefined blowing up ───────────


def test_load_prompt_jd_extract_v1_renders() -> None:
    rendered = load_prompt("jd_extract_v1", jd_text="Senior Python Engineer, 5+ years.")
    assert isinstance(rendered, RenderedPrompt)
    assert rendered.version == "jd_extract_v1"
    assert "Senior Python Engineer" in rendered.user
    assert rendered.system  # non-empty


def test_load_prompt_resume_core_v1_renders() -> None:
    rendered = load_prompt("resume_core_v1", chunks=_chunks())
    assert "c_000" in rendered.user
    assert "Built things at Acme." in rendered.user


def test_load_prompt_resume_skills_v2_renders() -> None:
    rendered = load_prompt("resume_skills_v2", chunks=_chunks())
    assert "c_001" in rendered.user
    assert "Python, SQL, Kubernetes." in rendered.user


def test_load_prompt_cover_letter_v1_renders() -> None:
    rendered = load_prompt("cover_letter_v1", chunks=_chunks())
    assert "c_000" in rendered.user


# ── Fail-fast on a typo'd/unknown prompt name ────────────────────────────


def test_load_prompt_unknown_name_raises_template_not_found() -> None:
    with pytest.raises(jinja2.TemplateNotFound):
        load_prompt("nonexistent_v1")


# ── StrictUndefined: a missing var raises, never renders empty ──────────


def test_missing_template_var_raises_instead_of_rendering_empty() -> None:
    with pytest.raises(jinja2.UndefinedError):
        load_prompt("jd_extract_v1")  # jd_text intentionally omitted


# ── RenderedPrompt.messages shape/order ──────────────────────────────────


def test_rendered_prompt_messages_are_system_then_user_in_order() -> None:
    rendered = load_prompt("jd_extract_v1", jd_text="x")
    assert rendered.messages == [
        {"role": "system", "content": rendered.system},
        {"role": "user", "content": rendered.user},
    ]


# ── list_prompts() ────────────────────────────────────────────────────────


def test_list_prompts_returns_exactly_the_ported_names() -> None:
    assert list_prompts() == sorted(EXPECTED_PROMPT_NAMES)


def test_every_listed_prompt_has_a_matching_user_template_on_disk() -> None:
    """``list_prompts`` only globs ``*.system.j2`` — a half-ported pair
    (system present, user missing) would otherwise slip through silently."""
    for name in list_prompts():
        assert (_TEMPLATES_DIR / f"{name}.user.j2").is_file(), name


def test_shortlist_evidence_pair_is_ported_in_phase_4c() -> None:
    """Phase 4c ports the matching stage-3 evidence prompt (and its
    cover-letter variant) verbatim from hris — both halves of each pair must
    be present on disk and loadable."""
    versions = list_prompts()
    assert "shortlist_evidence_v1" in versions
    assert "shortlist_evidence_v2" in versions
