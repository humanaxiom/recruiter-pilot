"""Unit tests for deterministic section-aware chunking
(``src/pipeline/parsing/chunk.py``).

Ported from hris ``packages/pipeline/src/pipeline/parsing/chunk.py``
(``phase3-source-dossier.md`` §2). Pure text -> ``list[ResumeChunk]``, no
LLM, no I/O.

Chunk ids are **one-based**: the counter starts at 1, so the first id is
``c_001``, not ``c_000``. This is the hris behaviour and it is load-bearing
downstream — Phase 4's evidence verifier matches LLM-cited chunk ids against
the ids minted here, so an off-by-one in the series silently invalidates
every citation.
"""

from __future__ import annotations

from typing import Any

import pytest

from src.pipeline.parsing.chunk import chunk_resume, scrub_invalid_chunk_refs
from src.pipeline.parsing.extract import ExtractedText, PageText

_MIN_CHARS_PER_CHUNK = 40  # mirrors the module constant documented in the dossier
_MAX_CHARS_PER_CHUNK = 1200


def _extracted(*page_texts: str) -> ExtractedText:
    pages = tuple(PageText(page_no=i, text=t) for i, t in enumerate(page_texts))
    return ExtractedText(pages=pages, method="text")


# ── Chunk id series ──────────────────────────────────────────────────────


def test_default_prefix_ids_are_zero_padded_one_based_c_series() -> None:
    body_a = "A" * 50
    body_b = "B" * 50
    extracted = _extracted(f"Summary\n{body_a}\n\nExperience\n{body_b}")

    chunks = chunk_resume(extracted)

    ids = [c.id for c in chunks]
    assert ids == [f"c_{i:03d}" for i in range(1, len(chunks) + 1)]
    assert ids[0] == "c_001"


def test_custom_prefix_yields_its_own_zero_padded_series() -> None:
    body = "X" * 50
    extracted = _extracted(f"Summary\n{body}")

    chunks = chunk_resume(extracted, prefix="cl")

    assert chunks[0].id == "cl_001"


def test_default_and_cover_letter_prefix_id_spaces_are_disjoint() -> None:
    body = "Y" * 50
    extracted = _extracted(f"Summary\n{body}")

    resume_ids = {c.id for c in chunk_resume(extracted, prefix="c")}
    cover_letter_ids = {c.id for c in chunk_resume(extracted, prefix="cl")}

    assert resume_ids.isdisjoint(cover_letter_ids)
    assert resume_ids  # sanity: not vacuously true
    assert cover_letter_ids


# ── Min/max chunk-size discipline ────────────────────────────────────────


def test_chunks_below_min_chars_are_dropped() -> None:
    tiny = "short"  # < 40 chars
    long_enough = "z" * 50
    extracted = _extracted(f"Skills\n{tiny}\n\nExperience\n{long_enough}")

    chunks = chunk_resume(extracted)

    assert all(len(c.text) >= _MIN_CHARS_PER_CHUNK for c in chunks)
    assert not any(c.text == tiny for c in chunks)


def test_hard_split_never_exceeds_max_chars_for_one_unbroken_sentence() -> None:
    body = "x" * 5000  # no punctuation/whitespace -> forces fixed-boundary slicing
    extracted = _extracted(f"Experience\n{body}")

    chunks = chunk_resume(extracted)

    assert chunks
    assert all(len(c.text) <= _MAX_CHARS_PER_CHUNK for c in chunks)
    # every character survives the hard split, none duplicated/dropped
    assert sum(len(c.text) for c in chunks) == 5000


# ── Section heading classification ───────────────────────────────────────


@pytest.mark.parametrize(
    ("heading", "expected_section"),
    [
        ("Experience", "experience"),
        ("WORK HISTORY", "experience"),
        ("Skills:", "skills"),
    ],
)
def test_section_headings_classify_correctly(
    heading: str, expected_section: str
) -> None:
    body = "z" * 60
    extracted = _extracted(f"{heading}\n{body}")

    chunks = chunk_resume(extracted)

    assert chunks
    assert all(c.section == expected_section for c in chunks)


def test_long_prose_mentioning_experience_is_not_a_heading() -> None:
    """A > 40-char prose line containing "experience" must NOT
    false-positive as a section heading — only the length gate protects
    against regex hits inside ordinary paragraph text."""
    prose = (
        "I have significant hands-on experience leading distributed "
        "engineering teams at scale for many years across the industry."
    )
    assert len(prose) > 40  # the length gate is what should reject this as a heading
    extracted = _extracted(prose + "\n" + ("filler text here " * 5))

    chunks = chunk_resume(extracted)

    assert chunks
    assert all(c.section == "other" for c in chunks)


def test_text_before_first_heading_is_section_other() -> None:
    intro = "z" * 60
    extracted = _extracted(f"{intro}\n\nExperience\n{'y' * 60}")

    chunks = chunk_resume(extracted)

    other_chunks = [c for c in chunks if c.section == "other"]
    assert other_chunks
    assert intro in other_chunks[0].text


def test_section_spanning_a_page_boundary_stays_one_section() -> None:
    page0 = f"Experience\n{'a' * 40}"
    page1 = "b" * 40
    extracted = _extracted(page0, page1)

    chunks = chunk_resume(extracted)

    experience_chunks = [c for c in chunks if c.section == "experience"]
    assert len(experience_chunks) == 1
    assert "a" * 40 in experience_chunks[0].text
    assert "b" * 40 in experience_chunks[0].text
    assert experience_chunks[0].page == 0  # page of the heading, not the continuation


# ── scrub_invalid_chunk_refs ──────────────────────────────────────────────


def test_scrub_invalid_chunk_refs_drops_unknown_ids_in_place() -> None:
    skills: list[dict[str, Any]] = [
        {"name": "Python", "evidence_chunk_ids": ["c_000", "c_999"]},
        {"name": "SQL", "evidence_chunk_ids": ["c_002"]},
        {"name": "NoRefs"},
    ]
    valid_ids = {"c_000", "c_002"}

    scrub_invalid_chunk_refs(skills, valid_ids)

    assert skills[0]["evidence_chunk_ids"] == ["c_000"]
    assert skills[1]["evidence_chunk_ids"] == ["c_002"]  # untouched: already valid
    assert "evidence_chunk_ids" not in skills[2]  # untouched: never had the key


def test_scrub_invalid_chunk_refs_drops_all_ids_when_none_are_valid() -> None:
    skills: list[dict[str, Any]] = [{"name": "Rust", "evidence_chunk_ids": ["c_010"]}]

    scrub_invalid_chunk_refs(skills, valid_ids=set())

    assert skills[0]["evidence_chunk_ids"] == []


# ── Finding 2a (HIGH): chunk_resume output must be bounded ─────────────────
#
# ResumeParsed.chunks caps at max_length=200 and cover_letter_chunks caps at
# max_length=100, but chunk_resume() itself has no cap. Pydantic's
# max_length RAISES on a too-long list rather than truncating, so an
# ordinary long CV (chunking into >200 pieces — there is no upload byte-cap
# until Phase 6) blows up ResumeParsed.model_validate() deep inside
# parse_resume, after several expensive LLM calls already ran.


def _giant_unbroken_text(total_chars: int) -> str:
    """One giant word with no whitespace/punctuation, so _hard_split slices
    it into exactly ceil(total_chars / _MAX_CHARS_PER_CHUNK) same-sized
    chunks — the simplest way to deterministically mint N chunks."""
    return "x" * total_chars


def test_chunk_resume_output_is_capped_at_200_for_default_prefix() -> None:
    # 300 chunks worth of unbroken text -> without a cap this raises
    # ValidationError deep inside ResumeParsed.model_validate; chunk_resume
    # itself must never hand back more than 200.
    body = _giant_unbroken_text(_MAX_CHARS_PER_CHUNK * 300)
    extracted = _extracted(f"Experience\n{body}")

    chunks = chunk_resume(extracted)

    assert len(chunks) <= 200
    ids = [c.id for c in chunks]
    assert ids == [
        f"c_{i:03d}" for i in range(1, len(chunks) + 1)
    ]  # contiguous, 1-based


def test_chunk_resume_output_is_capped_at_100_for_cover_letter_prefix() -> None:
    body = _giant_unbroken_text(_MAX_CHARS_PER_CHUNK * 150)
    extracted = _extracted(f"Experience\n{body}")

    chunks = chunk_resume(extracted, prefix="cl")

    assert len(chunks) <= 100
    ids = [c.id for c in chunks]
    assert ids == [f"cl_{i:03d}" for i in range(1, len(chunks) + 1)]
