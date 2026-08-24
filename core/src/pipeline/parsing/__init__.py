"""Text extraction + deterministic chunking for résumés/JDs.

Re-exports the public surface of ``extract.py`` and ``chunk.py`` so
callers can do ``from src.pipeline.parsing import extract_text,
chunk_resume``.
"""

from __future__ import annotations

from src.pipeline.parsing.chunk import chunk_resume, scrub_invalid_chunk_refs
from src.pipeline.parsing.extract import (
    MIME_DOCX,
    MIME_PDF,
    MIME_RTF,
    MIME_TXT,
    EncryptedPdfError,
    ExtractedText,
    InputTooLargeError,
    UnsupportedMimeError,
    extract_text,
)

__all__ = [
    "MIME_DOCX",
    "MIME_PDF",
    "MIME_RTF",
    "MIME_TXT",
    "EncryptedPdfError",
    "ExtractedText",
    "InputTooLargeError",
    "UnsupportedMimeError",
    "chunk_resume",
    "extract_text",
    "scrub_invalid_chunk_refs",
]
