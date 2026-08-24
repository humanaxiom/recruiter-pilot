"""Deterministic section-aware chunking.

Splits the extracted resume text into roughly-paragraph-sized chunks
labelled by section (experience, education, skills, etc.). Chunk ids
are stable ``c_NNN`` tokens — the LLM cites them as
``evidence_chunk_ids`` and we validate the citations against the
chunk set before persistence.

No LLM here. Pure text -> list[ResumeChunk]. Tested unit-level.

Ported near-verbatim from hris
``packages/pipeline/src/pipeline/parsing/chunk.py`` (see
``phase3-source-dossier.md`` §2). Only the import path changed
(``from schemas import ResumeChunk`` -> ``from src.schemas import
ResumeChunk``).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from src.pipeline.parsing.extract import ExtractedText
from src.schemas.resumes import ResumeChunk

log = logging.getLogger(__name__)

# Section heading detection. Patterns are matched against single lines
# (case-insensitive) so "## Experience" and "WORK HISTORY" both hit.
_SECTION_PATTERNS: dict[str, re.Pattern[str]] = {
    "summary": re.compile(r"\b(summary|profile|objective|about)\b", re.IGNORECASE),
    "experience": re.compile(
        r"\b(experience|employment|work history|professional history)\b",
        re.IGNORECASE,
    ),
    "education": re.compile(r"\b(education|academic|qualifications)\b", re.IGNORECASE),
    "skills": re.compile(
        r"\b(skills|technical skills|technologies|competencies)\b", re.IGNORECASE
    ),
    "projects": re.compile(r"\b(projects|selected projects)\b", re.IGNORECASE),
    "certifications": re.compile(r"\b(certifications|certificates)\b", re.IGNORECASE),
    "publications": re.compile(r"\b(publications|papers)\b", re.IGNORECASE),
}

# Rough cap per chunk. We don't tokenise — character count is a
# coarse proxy that beats nothing. Real token counts are deferred to
# later rescoring work; here we just want chunks small enough for the
# LLM's context budget.
_MAX_CHARS_PER_CHUNK = 1200
_MIN_CHARS_PER_CHUNK = 40

# Hard cap on the NUMBER of chunks, per id namespace. These mirror the
# ``ResumeParsed`` list caps exactly (``chunks`` max_length=200,
# ``cover_letter_chunks`` max_length=100) — and pydantic's ``max_length``
# RAISES rather than truncates, so an over-long CV would otherwise blow up in
# ``ResumeParsed.model_validate`` deep inside ``parse_resume``, after several
# expensive LLM calls already ran. Truncate here, loudly, instead.
_MAX_CHUNKS_BY_PREFIX = {"c": 200, "cl": 100}
_DEFAULT_MAX_CHUNKS = 200


@dataclass(frozen=True)
class _Section:
    name: str
    page: int
    text: str


def chunk_resume(extracted: ExtractedText, *, prefix: str = "c") -> list[ResumeChunk]:
    """Build the chunk list from extracted text.

    Returns at least one chunk per non-empty section. Sections we don't
    recognise get bucketed under ``other``. ``prefix`` keys the chunk-id
    namespace: ``c_NNN`` for a résumé, ``cl_NNN`` for a cover letter —
    kept disjoint so the evidence verifier can scrub fabricated ids
    per-source.

    The output is BOUNDED by the ``ResumeParsed`` list cap for that namespace
    (200 for ``c``, 100 for ``cl``). Ids stay contiguous and one-based; a
    truncated tail is logged, never dropped silently.
    """
    max_chunks = _MAX_CHUNKS_BY_PREFIX.get(prefix, _DEFAULT_MAX_CHUNKS)
    sections = _split_by_section(extracted)
    chunks: list[ResumeChunk] = []
    counter = 1
    dropped = 0
    for section in sections:
        for body in _split_paragraphs(section.text, max_chars=_MAX_CHARS_PER_CHUNK):
            stripped = body.strip()
            if len(stripped) < _MIN_CHARS_PER_CHUNK:
                continue
            if len(chunks) >= max_chunks:
                dropped += 1
                continue
            chunks.append(
                ResumeChunk(
                    id=f"{prefix}_{counter:03d}",
                    section=section.name,
                    page=section.page,
                    text=stripped,
                )
            )
            counter += 1
    if dropped:
        log.warning(
            "chunk_resume.truncated prefix=%s kept=%d dropped=%d",
            prefix,
            len(chunks),
            dropped,
        )
    return chunks


# ---------------- internals ----------------


def _split_by_section(extracted: ExtractedText) -> list[_Section]:
    """Walk the extracted text line-by-line. Each section heading
    starts a new section; text before the first heading is bucketed
    under ``other``.
    """
    sections: list[_Section] = []
    current_name = "other"
    current_page = 0
    current_lines: list[str] = []

    for page in extracted.pages:
        for raw in page.text.splitlines():
            line = raw.strip()
            heading = _match_section(line)
            if heading is not None:
                _flush(sections, current_name, current_page, current_lines)
                current_name = heading
                current_page = page.page_no
                current_lines = []
                continue
            current_lines.append(raw)
        # End-of-page is not a section break.

    _flush(sections, current_name, current_page, current_lines)
    return sections


def _flush(out: list[_Section], name: str, page: int, lines: list[str]) -> None:
    # keep paragraph breaks
    body = "\n".join(line for line in lines if line.strip() or lines)
    body = body.strip()
    if body:
        out.append(_Section(name=name, page=page, text=body))


def _match_section(line: str) -> str | None:
    """Heading detector. A line is a heading if it's short (<= ~40
    chars), mostly word characters, and matches a known section
    pattern. Lots of false positives otherwise (regex hits inside
    paragraph prose).
    """
    if not line or len(line) > 40:
        return None
    # Strip trailing colon: "Experience:" -> "Experience"
    candidate = line.rstrip(":").strip()
    if not candidate or len(candidate) > 40:
        return None
    for name, pattern in _SECTION_PATTERNS.items():
        if pattern.fullmatch(candidate) or pattern.match(candidate):
            return name
    return None


def _split_paragraphs(text: str, *, max_chars: int) -> Iterable[str]:
    """Split a section body into <=max_chars chunks at paragraph
    boundaries (blank lines), falling back to sentence boundaries,
    falling back to hard slicing.
    """
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n+", text) if p.strip()]
    buf: list[str] = []
    buf_len = 0
    for para in paragraphs:
        if len(para) > max_chars:
            # Hard split a too-long paragraph at sentence boundaries.
            for sentence_chunk in _hard_split(para, max_chars):
                if buf:
                    yield "\n\n".join(buf)
                    buf, buf_len = [], 0
                yield sentence_chunk
            continue
        if buf_len + len(para) + 2 > max_chars:
            yield "\n\n".join(buf)
            buf, buf_len = [para], len(para)
        else:
            buf.append(para)
            buf_len += len(para) + 2
    if buf:
        yield "\n\n".join(buf)


_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")


def _hard_split(text: str, max_chars: int) -> Iterable[str]:
    sentences = _SENTENCE_RE.split(text)
    buf: list[str] = []
    buf_len = 0
    for sentence in sentences:
        if not sentence:
            continue
        if buf_len + len(sentence) + 1 > max_chars and buf:
            yield " ".join(buf)
            buf, buf_len = [], 0
        if len(sentence) > max_chars:
            # Even a single sentence is too long — slice.
            for start in range(0, len(sentence), max_chars):
                yield sentence[start : start + max_chars]
        else:
            buf.append(sentence)
            buf_len += len(sentence) + 1
    if buf:
        yield " ".join(buf)


def scrub_invalid_chunk_refs(skills: list[dict[str, Any]], valid_ids: set[str]) -> None:
    """Drop ``evidence_chunk_ids`` entries that don't exist.

    Mutates the list in place. The LLM hallucinates chunk IDs sometimes;
    persistence with bad refs leads to broken evidence links in the UI.
    """
    for skill in skills:
        ids = skill.get("evidence_chunk_ids")
        if not ids:
            continue
        skill["evidence_chunk_ids"] = [i for i in ids if i in valid_ids]


__all__ = ["chunk_resume", "scrub_invalid_chunk_refs"]
