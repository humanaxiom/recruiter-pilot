"""Filesystem ``BlobStore`` — the thin blob primitive every later phase calls.

Ported to replace hris's raw ``minio-py`` calls (``put_object`` /
``get_object`` / ``remove_object`` / ``list_objects``) onto a bind-mounted
directory root (``settings.storage_dir``). The interface is async because the
project rule is async everywhere; the filesystem IO itself is synchronous, so
each op runs the blocking work in :func:`asyncio.to_thread` off the event loop —
mirroring hris, which ran every blob op via ``asyncio.to_thread``. No new
dependency (no ``aiofiles``): stdlib ``pathlib`` / ``asyncio`` only.

**Path-traversal safety (merge-blocking).** ``blob_key`` columns are unvalidated
TEXT and the root is a bind-mounted host directory, so a malformed or malicious
key must never escape the root. Every key-taking method funnels through
:meth:`BlobStore._resolve`, which rejects ``..`` segments, absolute keys, the
empty / root key, and **symlink escapes** (realpath the candidate and assert it
is strictly under the realpath'd root) *before* any write — raising
:class:`InvalidBlobKey` so callers can tell "bad key" from "disk problem".
"""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path

from fastapi import Request

# A leading Windows drive spec (``C:``) once separators are normalised. Blob
# keys are relative POSIX paths, so a drive-qualified key is always an escape.
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")


# Names pinned by the storage contract / call sites (ported from hris); the
# ``Error`` suffix N818 wants would break the pinned public API, so it is waived.
class BlobNotFound(Exception):  # noqa: N818
    """Raised by :meth:`BlobStore.get` when the key has no object."""


class InvalidBlobKey(ValueError):  # noqa: N818
    """Raised when a key would escape the store root (traversal/symlink/absolute)."""


class BlobStore:
    """A blob sink rooted at a single directory. Dumb bytes in, dumb bytes out.

    The store never parses or extracts text from what it holds — it is a byte
    sink, so nothing here feeds a summary/embedding path (the PII-never-in-
    embeddings invariant has no surface in this layer).
    """

    def __init__(self, root: str | Path) -> None:
        base = Path(root)
        # Bucket-bootstrap parity: MinIO created its bucket on startup, so the
        # root must exist before any caller reaches for it.
        base.mkdir(parents=True, exist_ok=True)
        # Realpath the root once so the traversal guard can compare realpaths
        # (this also collapses any symlink in the root's own ancestry).
        self._root: Path = base.resolve()

    # ── The traversal guard — every public method calls this first ───────────
    def _resolve(self, key: str) -> Path:
        """Validate ``key`` and return its absolute path strictly under root.

        Rejects (``InvalidBlobKey``): the empty key, absolute keys (POSIX ``/``
        or a Windows drive), any ``..`` *segment* (dots inside a filename are
        fine — this is a segment check, not a substring check), keys resolving
        to the root itself, and symlink escapes (a parent component symlinked
        outside the root). Runs before any write.
        """
        if not key:
            raise InvalidBlobKey("blob key must not be empty")
        # An embedded null byte makes ``Path.resolve()`` raise a *bare*
        # ``ValueError``; reject it here so callers catching the specific type
        # still catch it.
        if "\x00" in key:
            raise InvalidBlobKey("blob key must not contain a null byte")
        # Normalise Windows separators so a backslash key is checked as segments
        # rather than landing as one literal filename on POSIX.
        normalised = key.replace("\\", "/")
        if normalised.startswith("/") or _WINDOWS_DRIVE.match(normalised):
            raise InvalidBlobKey(f"blob key must be relative, not absolute: {key!r}")
        segments = normalised.split("/")
        if any(segment == ".." for segment in segments):
            raise InvalidBlobKey(f"blob key must not contain '..' segments: {key!r}")

        candidate = (self._root / normalised).resolve()
        if candidate == self._root:
            raise InvalidBlobKey(f"blob key must not resolve to the root: {key!r}")
        if not candidate.is_relative_to(self._root):
            raise InvalidBlobKey(f"blob key escapes the store root: {key!r}")
        return candidate

    # ── Public async surface ─────────────────────────────────────────────────
    async def put(self, key: str, data: bytes, content_type: str | None = None) -> None:
        """Write ``data`` to ``key``, overwriting. ``content_type`` is accepted
        for MinIO call-site parity and ignored — a filesystem store has no
        per-file content type; the key's extension carries it downstream."""
        path = self._resolve(key)

        def _write() -> None:
            # ``mkdir(parents=True, mode=...)`` applies the mode to the leaf
            # only, so walk the missing chain and create each level 0o700 —
            # raw resume bytes must not sit in a world-traversable dir. Only
            # store-created levels are touched; the (pre-existing) root is not.
            missing: list[Path] = []
            probe = path.parent
            while not probe.exists():
                missing.append(probe)
                probe = probe.parent
            for created in reversed(missing):
                created.mkdir(mode=0o700)
                os.chmod(created, 0o700)  # deterministic regardless of umask
            # Create/truncate the blob 0o600 (no world-readable window), then
            # chmod so an overwrite of a looser pre-existing file is tightened.
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd, data)
            finally:
                os.close(fd)
            os.chmod(path, 0o600)

        await asyncio.to_thread(_write)

    async def get(self, key: str) -> bytes:
        """Return the bytes at ``key``; raise :class:`BlobNotFound` if absent."""
        path = self._resolve(key)

        def _read() -> bytes:
            try:
                return path.read_bytes()
            except FileNotFoundError as exc:
                raise BlobNotFound(key) from exc

        return await asyncio.to_thread(_read)

    async def delete(self, key: str) -> None:
        """Remove ``key``. A missing key is success (idempotent retention)."""
        path = self._resolve(key)
        await asyncio.to_thread(path.unlink, missing_ok=True)

    async def exists(self, key: str) -> bool:
        """True iff ``key`` is a regular file under the root."""
        path = self._resolve(key)
        return await asyncio.to_thread(path.is_file)

    async def list_keys(self, prefix: str = "") -> list[str]:
        """Sorted, root-relative, POSIX-separated keys of regular files under
        ``<root>/<prefix>`` (recursive). ``""`` lists everything."""
        base = self._root if prefix == "" else self._resolve(prefix)

        def _walk() -> list[str]:
            if not base.exists():
                return []
            # ``rglob`` follows symlinks, so realpath-filter to entries
            # genuinely under root — a link pointing outside must not leak into
            # the listing (the read side is as strict as the write side).
            return sorted(
                p.relative_to(self._root).as_posix()
                for p in base.rglob("*")
                if p.is_file() and p.resolve().is_relative_to(self._root)
            )

        return await asyncio.to_thread(_walk)


def get_blob_store(request: Request) -> BlobStore:
    """FastAPI dependency — hand the lifespan-built store to routes.

    Mirrors ``get_db`` in ``models/pool.py`` but is a plain sync function (the
    store is process-global, not per-request): raises ``RuntimeError`` when the
    lifespan has not parked a store on ``app.state.blob_store``."""
    store: BlobStore | None = getattr(request.app.state, "blob_store", None)
    if store is None:
        raise RuntimeError("BlobStore not initialised (lifespan not running)")
    return store
