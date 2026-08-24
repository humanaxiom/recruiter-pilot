"""Extract job-description text from an uploaded file (Phase 6).

Recruiters can upload a JD as txt / json / pdf / docx instead of pasting.
This service turns the uploaded bytes into plain text that pre-fills the
"description" field — the recruiter reviews/edits, then creates the job
through the normal ``JobCreate`` path (so parsing/retention are unchanged).
Nothing is persisted here; this is a PURE transform (no DB, no BlobStore).

Ported from hris ``apps/api/src/api/services/jd_import_service.py`` behaviour
only — the CAS/MinIO coupling is cut. PDF/DOCX reuse the same extractor the
résumé pipeline uses (``src.pipeline.parsing.extract``).
"""

from __future__ import annotations

import json
from pathlib import Path

from src.errors import AppError
from src.pipeline.parsing.extract import (
    MIME_DOCX,
    MIME_PDF,
    EncryptedPdfError,
    UnsupportedMimeError,
    extract_text,
)

# Cap uploads so a hostile/huge file can't exhaust memory. JDs are a few kB;
# 10 MB is generous headroom for an image-heavy PDF.
MAX_JD_UPLOAD_BYTES = 10 * 1024 * 1024

# JSON keys we treat as "this is the description" (first match wins).
_JSON_TEXT_KEYS = (
    "description",
    "job_description",
    "jd",
    "text",
    "body",
    "content",
    "summary",
)


class JDImportError(AppError):
    """Upload couldn't be turned into JD text (bad type, too big, empty)."""

    code = "jd.import_failed"
    status = 422


def extract_jd_text(
    blob: bytes, *, filename: str, content_type: str | None = None
) -> str:
    """Return plain JD text from an uploaded file's bytes.

    Dispatch is by file EXTENSION first (browsers send wildly inconsistent
    content-types for json/docx), with the declared content-type as a
    fallback. Raises ``JDImportError`` (422) on anything unreadable.
    """
    if not blob:
        raise JDImportError("the uploaded file is empty", filename=filename)
    if len(blob) > MAX_JD_UPLOAD_BYTES:
        raise JDImportError(
            f"file is too large ({len(blob) // 1024} KB); max is "
            f"{MAX_JD_UPLOAD_BYTES // (1024 * 1024)} MB",
            filename=filename,
        )

    ext = Path(filename).suffix.lower()
    ct = (content_type or "").split(";", 1)[0].strip().lower()

    if ext == ".txt" or ct == "text/plain":
        text = _decode(blob)
    elif ext == ".json" or ct == "application/json":
        text = _json_to_text(_decode(blob))
    elif ext == ".pdf" or ct == MIME_PDF:
        text = _via_extractor(blob, MIME_PDF, filename)
    elif ext == ".docx" or ct == MIME_DOCX:
        text = _via_extractor(blob, MIME_DOCX, filename)
    else:
        raise JDImportError(
            f"unsupported file type {ext or ct or 'unknown'!r}; "
            "upload a .txt, .json, .pdf, or .docx",
            filename=filename,
        )

    text = text.strip()
    if not text:
        raise JDImportError(
            "no readable text found in the file (a scanned/image-only PDF "
            "won't extract — paste the text instead)",
            filename=filename,
        )
    return text


def _via_extractor(blob: bytes, mime: str, filename: str) -> str:
    """Run the shared PDF/DOCX extractor, mapping its errors to JDImportError."""
    try:
        return extract_text(blob, mime).full_text
    except EncryptedPdfError as exc:
        raise JDImportError("the PDF is password-protected", filename=filename) from exc
    except UnsupportedMimeError as exc:
        raise JDImportError(
            f"could not read the file: {exc}", filename=filename
        ) from exc


def _decode(blob: bytes) -> str:
    """Decode text bytes tolerantly — strip a UTF-8 BOM, fall back to
    latin-1 so a stray non-UTF-8 byte doesn't blow up the whole upload."""
    for codec in ("utf-8-sig", "utf-8"):
        try:
            return blob.decode(codec)
        except UnicodeDecodeError:
            continue
    return blob.decode("latin-1", errors="replace")


def _json_to_text(raw: str) -> str:
    """Turn a JSON JD into text: prefer a known description field, else
    pretty-print the document so the content is preserved."""
    try:
        data = json.loads(raw)
    except ValueError:
        # Not valid JSON despite the extension — treat as plain text.
        return raw
    if isinstance(data, dict):
        for key in _JSON_TEXT_KEYS:
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value
    return json.dumps(data, indent=2, ensure_ascii=False)


__all__ = ["MAX_JD_UPLOAD_BYTES", "JDImportError", "extract_jd_text"]
