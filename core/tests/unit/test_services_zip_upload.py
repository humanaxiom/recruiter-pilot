"""Unit tests for Phase 6's zip-upload expansion guard —
``src.services.zip_upload.expand_zip_entries``.

This module does not exist yet. Every test below fails at collection
(``ModuleNotFoundError``). RED half of the TDD cycle.

**Why this is a NEW attack surface (per the human decision this phase
locks):** a zip is a new untrusted-bytes trust boundary the same way a raw
PDF/DOCX blob already is (``src/pipeline/parsing/extract.py``'s
``_enforce_docx_decompression_cap`` — the design template this guard mirrors,
including its NON-NEGOTIABLE lesson: **never trust the archive's
self-declared entry size**, stream and sum REAL decompressed bytes instead).
A malicious zip must be rejected loud, and — because ``expand_zip_entries``
is a PURE function (bytes in, ``list[tuple[filename, bytes]]`` out, no
``BlobStore``/connection parameter at all) — it is architecturally
INCAPABLE of writing anything to disk, whether it accepts or rejects.

**Ambiguities this file locks:**

* ``expand_zip_entries(data: bytes) -> list[tuple[str, bytes]]`` — one
  ``(entry_name, entry_bytes)`` tuple per archive member, in archive order.
* ``src.services.zip_upload.ZipRejected`` (a plain ``Exception`` subclass) is
  raised, BEFORE any entry bytes are returned, for:
  - a path-traversal entry name: any ``..`` segment, a POSIX-absolute name
    (leading ``/``), a Windows drive-qualified name (``C:\\...``), or a
    backslash-separated name that normalises to a traversal or absolute path
    — mirrors ``BlobStore._resolve``'s guard vocabulary exactly, since a zip
    entry name is exactly as untrusted as a blob key.
  - an entry whose extension is not in the résumé allowlist
    (``pdf``/``docx``/``rtf``/``txt``) — "non-résumé" entries are rejected at
    THIS layer, not deferred to ``detect_mime``.
  - an entry whose REAL decompressed size (streamed and summed, never the
    zip's self-declared ``ZipInfo.file_size``) exceeds
    ``_MAX_ENTRY_UNCOMPRESSED_BYTES`` (10 MB, matching
    ``extract.py``'s per-document cap).
  - a running total of real decompressed bytes across all entries exceeding
    ``_MAX_TOTAL_UNCOMPRESSED_BYTES`` (100 MB).
  - more than ``_MAX_ZIP_ENTRIES`` (50) members.
  - a corrupt / not-a-zip payload (``zipfile.BadZipFile``).
"""

from __future__ import annotations

import zipfile
from io import BytesIO

import pytest


def _zip_bytes(entries: list[tuple[str, bytes]], *, compress: bool = False) -> bytes:
    """Build real zip bytes from ``(name, content)`` pairs, using
    ``ZipInfo`` directly (not ``ZipFile.write``) so a malicious entry NAME
    (traversal-shaped) is written verbatim rather than sanitised by the
    stdlib's own path-joining convenience methods.

    PROVABLY-WRONG-TEST-FIXTURE FIX (coder note, not a weakened assertion):
    ``zipfile.ZipInfo(filename=name)`` always constructs with
    ``compress_type=ZIP_STORED`` regardless of the enclosing ``ZipFile``'s own
    ``compression=`` setting, and ``ZipFile.writestr`` honours an explicit
    ``ZipInfo``'s own ``compress_type`` over the archive default — so the
    original ``zf.writestr(zipfile.ZipInfo(filename=name), content)`` made
    ``compress=True`` a silent no-op (every entry was written STORED, never
    DEFLATED), which is exactly why
    ``test_expand_zip_entries_rejects_an_entry_over_the_per_entry_cap``'s own
    sanity assertion (``len(data) < 1024 * 1024``) failed: an 11 MB run of
    zero bytes came out ~11 MB on disk instead of compressing to a few KB.
    Passing ``compress_type=method`` explicitly to ``writestr`` (verified:
    11 MB of zeros -> 11333 bytes compressed, vs. 11534450 uncompressed)
    makes the fixture match its own documented intent — a genuine bomb-ratio
    archive — for both this test and
    ``test_expand_zip_entries_rejects_when_the_running_total_exceeds_the_cap``.
    """
    buf = BytesIO()
    method = zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED
    with zipfile.ZipFile(buf, "w", compression=method) as zf:
        for name, content in entries:
            zf.writestr(zipfile.ZipInfo(filename=name), content, compress_type=method)
    return buf.getvalue()


def _resume_bytes(n: int = 500) -> bytes:
    return b"%PDF-1.4\nresume content\n" + b"x" * n


# ── happy path ───────────────────────────────────────────────────────────


def test_expand_zip_entries_yields_one_tuple_per_archive_member() -> None:
    from src.services.zip_upload import expand_zip_entries

    data = _zip_bytes(
        [("alice.pdf", _resume_bytes()), ("bob.docx", b"PK\x03\x04" + b"x" * 500)]
    )
    out = expand_zip_entries(data)
    assert len(out) == 2
    names = {name for name, _ in out}
    assert names == {"alice.pdf", "bob.docx"}


def test_expand_zip_entries_preserves_entry_bytes() -> None:
    from src.services.zip_upload import expand_zip_entries

    payload = _resume_bytes()
    data = _zip_bytes([("resume.pdf", payload)])
    [(name, content)] = expand_zip_entries(data)
    assert content == payload


# ── path traversal ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "bad_name",
    [
        "../evil.pdf",
        "sub/../../evil.pdf",
        "/etc/passwd.pdf",
        "C:\\evil.pdf",
        "sub\\..\\..\\evil.pdf",
    ],
)
def test_expand_zip_entries_rejects_path_traversal_names(bad_name: str) -> None:
    from src.services.zip_upload import ZipRejected, expand_zip_entries

    data = _zip_bytes([(bad_name, _resume_bytes())])
    with pytest.raises(ZipRejected):
        expand_zip_entries(data)


def test_expand_zip_entries_rejects_traversal_even_when_mixed_with_good_entries() -> (
    None
):
    """One bad entry poisons the whole archive — nothing partially succeeds."""
    from src.services.zip_upload import ZipRejected, expand_zip_entries

    data = _zip_bytes([("good.pdf", _resume_bytes()), ("../evil.pdf", _resume_bytes())])
    with pytest.raises(ZipRejected):
        expand_zip_entries(data)


# ── extension allowlist ─────────────────────────────────────────────────


def test_expand_zip_entries_rejects_a_non_resume_extension() -> None:
    from src.services.zip_upload import ZipRejected, expand_zip_entries

    data = _zip_bytes([("payload.exe", b"MZ" + b"x" * 500)])
    with pytest.raises(ZipRejected):
        expand_zip_entries(data)


# ── per-entry uncompressed cap ──────────────────────────────────────────


def test_expand_zip_entries_rejects_an_entry_over_the_per_entry_cap() -> None:
    """A highly compressible member (real bomb-ratio content, not a forged
    header) whose TRUE decompressed size exceeds the per-entry cap must be
    rejected — proves the guard streams and sums real bytes, not the
    archive's declared size."""
    from src.services.zip_upload import ZipRejected, expand_zip_entries

    huge = b"\x00" * (11 * 1024 * 1024)  # 11 MB of zeros: trivially compresses to KB
    data = _zip_bytes([("bomb.pdf", huge)], compress=True)
    assert len(data) < 1024 * 1024  # sanity: the on-disk archive is tiny
    with pytest.raises(ZipRejected):
        expand_zip_entries(data)


def test_expand_zip_entries_accepts_an_entry_at_exactly_the_per_entry_cap() -> None:
    from src.services.zip_upload import expand_zip_entries

    exactly_cap = b"%PDF-1.4\n" + b"y" * (10 * 1024 * 1024 - 9)
    data = _zip_bytes([("edge.pdf", exactly_cap)])
    out = expand_zip_entries(data)
    assert len(out[0][1]) == len(exactly_cap)


# ── total uncompressed cap ───────────────────────────────────────────────


def test_expand_zip_entries_rejects_when_the_running_total_exceeds_the_cap() -> None:
    """Several entries, each individually under the per-entry cap, whose SUM
    exceeds the total cap — the total-bytes guard, not the per-entry one."""
    from src.services.zip_upload import ZipRejected, expand_zip_entries

    # 12 entries * 9 MB = 108 MB > the 100 MB total cap, each entry alone is
    # comfortably under the 10 MB per-entry cap.
    chunk = b"%PDF-1.4\n" + b"z" * (9 * 1024 * 1024)
    entries = [(f"r{i}.pdf", chunk) for i in range(12)]
    data = _zip_bytes(entries, compress=True)
    with pytest.raises(ZipRejected):
        expand_zip_entries(data)


# ── entry-count cap ──────────────────────────────────────────────────────


def test_expand_zip_entries_rejects_too_many_entries() -> None:
    from src.services.zip_upload import ZipRejected, expand_zip_entries

    entries = [(f"r{i}.pdf", _resume_bytes(50)) for i in range(51)]
    data = _zip_bytes(entries)
    with pytest.raises(ZipRejected):
        expand_zip_entries(data)


def test_expand_zip_entries_accepts_exactly_the_entry_count_cap() -> None:
    from src.services.zip_upload import expand_zip_entries

    entries = [(f"r{i}.pdf", _resume_bytes(50)) for i in range(50)]
    data = _zip_bytes(entries)
    out = expand_zip_entries(data)
    assert len(out) == 50


# ── corrupt archive ──────────────────────────────────────────────────────


def test_expand_zip_entries_rejects_a_corrupt_zip() -> None:
    from src.services.zip_upload import ZipRejected, expand_zip_entries

    with pytest.raises(ZipRejected):
        expand_zip_entries(b"this is not a zip file at all")


# ── nothing is ever written to disk ─────────────────────────────────────


def test_expand_zip_entries_never_touches_the_filesystem_on_rejection(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """``expand_zip_entries`` takes no ``BlobStore``/path parameter at all —
    a rejected (or accepted) call cannot write anything. Proven by running a
    real ``BlobStore`` rooted at ``tmp_path`` alongside the call and asserting
    its directory stays empty regardless of outcome."""
    import asyncio

    from src.services.zip_upload import ZipRejected, expand_zip_entries
    from src.storage.blob_store import BlobStore

    store = BlobStore(str(tmp_path))
    data = _zip_bytes([("../evil.pdf", _resume_bytes())])
    with pytest.raises(ZipRejected):
        expand_zip_entries(data)
    assert asyncio.run(store.list_keys()) == []
    assert list(tmp_path.iterdir()) == []
