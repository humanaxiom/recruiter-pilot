"""Zip-upload expansion guard — a new untrusted-bytes trust boundary.

Mirrors ``src/pipeline/parsing/extract.py``'s ``_enforce_docx_decompression_
cap`` design (see its docstring): NEVER trust the archive's self-declared
entry size (``ZipInfo.file_size`` is attacker-controlled and can lie), stream
and sum REAL decompressed bytes instead.

``expand_zip_entries`` is a PURE function — bytes in, ``list[tuple[filename,
bytes]]`` out, no ``BlobStore``/connection parameter at all — so it is
architecturally incapable of writing anything to disk, on either the accept
or the reject path.
"""

from __future__ import annotations

import re
import zipfile
from io import BytesIO

# Mirrors BlobStore._resolve's traversal-guard vocabulary — a zip entry name
# is exactly as untrusted as a blob key.
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")

# Résumé-file allowlist — non-résumé entries are rejected HERE, not deferred
# to detect_mime. A ``frozenset`` so it is safe as a function default argument.
# The bulk-JD call site (``routes/jobs.py``) passes its OWN allowlist
# (``{pdf,docx,txt,json}``); the résumé call site keeps this default verbatim.
_ALLOWED_EXTENSIONS: frozenset[str] = frozenset({"pdf", "docx", "rtf", "txt"})

# Matches extract.py's per-document cap.
_MAX_ENTRY_UNCOMPRESSED_BYTES = 10 * 1024 * 1024
_MAX_TOTAL_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
_MAX_ZIP_ENTRIES = 50

_STREAM_CHUNK_BYTES = 1024 * 1024


# Name pinned by the test contract (src.services.zip_upload.ZipRejected); the
# ``Error`` suffix N818 wants would break the pinned public API, same waiver
# as BlobStore's BlobNotFound/InvalidBlobKey.
class ZipRejected(Exception):  # noqa: N818
    """The archive (or one of its members) fails a safety check."""


def _validate_entry_name(name: str) -> None:
    if not name:
        raise ZipRejected("zip entry name must not be empty")
    normalised = name.replace("\\", "/")
    if normalised.startswith("/") or _WINDOWS_DRIVE.match(normalised):
        raise ZipRejected(f"zip entry name must be relative, not absolute: {name!r}")
    segments = normalised.split("/")
    if any(segment == ".." for segment in segments):
        raise ZipRejected(f"zip entry name must not contain '..' segments: {name!r}")


def _extension(name: str) -> str:
    if "." not in name:
        return ""
    return name.rsplit(".", 1)[-1].lower()


def expand_zip_entries(
    data: bytes, *, allowed_extensions: frozenset[str] = _ALLOWED_EXTENSIONS
) -> list[tuple[str, bytes]]:
    """Expand a batch zip into ``(entry_name, entry_bytes)`` tuples, in archive
    order.

    ``allowed_extensions`` defaults to the résumé allowlist
    (``{pdf,docx,rtf,txt}``) so the existing résumé call site is unchanged; the
    bulk-JD call site passes ``{pdf,docx,txt,json}`` instead. Raises
    :class:`ZipRejected`, BEFORE any entry bytes are returned, for a
    path-traversal name, a non-allowlisted extension, a per-entry or running
    total decompressed-size overage (streamed and summed — never the
    archive's declared size), too many entries, or a corrupt archive. One bad
    entry poisons the whole archive — nothing partially succeeds.
    """
    try:
        with zipfile.ZipFile(BytesIO(data)) as zf:
            infos = zf.infolist()
            if len(infos) > _MAX_ZIP_ENTRIES:
                raise ZipRejected(
                    f"zip has {len(infos)} entries; the cap is {_MAX_ZIP_ENTRIES}"
                )

            # Validate every name + extension BEFORE reading any bytes, so a
            # trailing bad entry still poisons the whole archive up front.
            for info in infos:
                _validate_entry_name(info.filename)
                ext = _extension(info.filename)
                if ext not in allowed_extensions:
                    raise ZipRejected(
                        f"zip entry {info.filename!r} has a disallowed extension"
                    )

            results: list[tuple[str, bytes]] = []
            total = 0
            for info in infos:
                # Neutralise the self-declared uncompressed size FIRST — see
                # extract.py's _enforce_docx_decompression_cap for the full
                # rationale (a forged-small file_size would truncate .read()
                # at the fake size instead of revealing the real payload).
                info.file_size = 1 << 40
                chunks: list[bytes] = []
                entry_total = 0
                with zf.open(info) as member:
                    while True:
                        chunk = member.read(_STREAM_CHUNK_BYTES)
                        if not chunk:
                            break
                        entry_total += len(chunk)
                        total += len(chunk)
                        if entry_total > _MAX_ENTRY_UNCOMPRESSED_BYTES:
                            raise ZipRejected(
                                f"zip entry {info.filename!r} decompresses past "
                                f"the per-entry cap of "
                                f"{_MAX_ENTRY_UNCOMPRESSED_BYTES} bytes"
                            )
                        if total > _MAX_TOTAL_UNCOMPRESSED_BYTES:
                            raise ZipRejected(
                                "zip decompresses past the total cap of "
                                f"{_MAX_TOTAL_UNCOMPRESSED_BYTES} bytes"
                            )
                        chunks.append(chunk)
                results.append((info.filename, b"".join(chunks)))
            return results
    except zipfile.BadZipFile as exc:
        raise ZipRejected(f"not a valid zip archive: {exc}") from exc


__all__ = ["ZipRejected", "expand_zip_entries"]
