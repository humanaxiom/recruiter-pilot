"""Unit tests for the filesystem ``BlobStore`` (``src/storage/blob_store.py``).

Pure filesystem, driven off ``tmp_path`` — no live services. The store is the
port of hris's raw ``minio-py`` calls (``put_object`` / ``get_object`` /
``remove_object`` / ``list_objects``) onto a bind-mounted directory root.

Two things carry contracts the rest of the port hangs off:

* **The async surface.** Every op is awaitable (project rule: async
  everywhere; the sync filesystem IO is wrapped in ``asyncio.to_thread``).
* **Path-traversal safety (merge-blocking).** ``blob_key`` columns are
  unvalidated TEXT and ``storage_dir`` is a bind-mounted host root, so a
  malformed or malicious key must never escape the root. Every method that
  takes a key resolves it and rejects escapes *before* any IO, raising a
  dedicated ``InvalidBlobKey(ValueError)``. The traversal + symlink tests below
  assert *both* the exception *and* that nothing was written/read outside the
  root — a store that wrote the file first and raised second would still fail.

Review/security hardenings (RED increment on top of the first green):

* **Restrictive modes (PIPEDA/FIPPA).** Raw resume bytes must not be
  world-readable: created blobs are ``0o600`` and store-created dirs ``0o700``.
* **Null-byte key.** ``"a\\x00b.txt"`` makes ``Path.resolve()`` raise a *bare*
  ``ValueError``; it must surface as ``InvalidBlobKey`` so callers catching the
  specific type still catch it.
* **``list_keys`` symlink asymmetry.** ``rglob`` follows symlinks, so a link
  inside root pointing outside must be realpath-filtered out of the listing —
  the read side must be as strict as the write side.
"""

from __future__ import annotations

import inspect
import os
from pathlib import Path

import pytest

from src.storage.blob_store import BlobNotFound, BlobStore, InvalidBlobKey

# ── Traversal / invalid keys ────────────────────────────────────────────────
# Escapes that every key-taking method must reject before touching disk.
TRAVERSAL_KEYS: tuple[str, ...] = (
    "../escape.txt",  # parent-dir climb
    "a/../../escape.txt",  # climb hidden mid-path
    "/etc/passwd",  # POSIX-absolute
    "..\\..\\escape.txt",  # Windows-style separators (must be normalised)
)
# Keys that resolve to the root itself / are empty — invalid as a blob *key*
# (you cannot put/get/delete the root as a file), but note ``list_keys("")``
# is the *valid* "list everything" default and is tested separately.
ROOT_OR_EMPTY_KEYS: tuple[str, ...] = ("", ".")
# A key with an embedded null byte makes ``Path.resolve()`` raise a *bare*
# ``ValueError("embedded null byte")``; the guard must convert it to
# ``InvalidBlobKey`` (a ValueError subclass) so callers catching the specific
# type still catch it (security finding, low).
NULL_BYTE_KEY: str = "a\x00b.txt"
INVALID_KEYS: tuple[str, ...] = TRAVERSAL_KEYS + ROOT_OR_EMPTY_KEYS + (NULL_BYTE_KEY,)
# ``list_keys`` accepts ``""`` (list everything), but any *non-empty* prefix is
# a key and must be validated the same way — including the null-byte case.
INVALID_PREFIXES: tuple[str, ...] = TRAVERSAL_KEYS + (NULL_BYTE_KEY,)


# ── Fixtures / helpers ──────────────────────────────────────────────────────


@pytest.fixture
def store(tmp_path: Path) -> BlobStore:
    return BlobStore(tmp_path)


@pytest.fixture
def rooted(tmp_path: Path) -> tuple[Path, Path, BlobStore]:
    """A store rooted at ``base/root`` so escapes have somewhere to land.

    Returns ``(base, root, store)`` — ``base`` is the parent that an escaping
    write would leak into; assertions check nothing appears under ``base``
    outside ``root``.
    """
    root = tmp_path / "root"
    root.mkdir()
    return tmp_path, root, BlobStore(root)


def _escaped_files(base: Path, root: Path) -> list[Path]:
    """Every regular file under ``base`` that is *not* inside ``root``."""
    root_real = root.resolve()
    return [
        p
        for p in base.rglob("*")
        if p.is_file() and not p.resolve().is_relative_to(root_real)
    ]


# ── The async surface ───────────────────────────────────────────────────────


@pytest.mark.parametrize("name", ["put", "get", "delete", "exists", "list_keys"])
def test_every_public_method_is_a_coroutine(name: str) -> None:
    assert inspect.iscoroutinefunction(getattr(BlobStore, name))


# ── Roundtrip ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_put_then_get_roundtrips_identical_bytes(store: BlobStore) -> None:
    await store.put("a/b/c.txt", b"hello world")
    assert await store.get("a/b/c.txt") == b"hello world"


@pytest.mark.asyncio
async def test_put_writes_the_file_under_root(store: BlobStore, tmp_path: Path) -> None:
    await store.put("dir/file.bin", b"\x00\x01\x02")
    assert (tmp_path / "dir" / "file.bin").read_bytes() == b"\x00\x01\x02"


@pytest.mark.asyncio
async def test_put_overwrites_an_existing_key(store: BlobStore) -> None:
    await store.put("k", b"one")
    await store.put("k", b"two")
    assert await store.get("k") == b"two"


@pytest.mark.asyncio
async def test_dotted_filenames_roundtrip(store: BlobStore) -> None:
    await store.put("resumes/2024/r.final.pdf", b"%PDF")
    assert await store.get("resumes/2024/r.final.pdf") == b"%PDF"


@pytest.mark.asyncio
async def test_double_dot_inside_a_filename_is_allowed(store: BlobStore) -> None:
    """``..`` is only a traversal as a whole *segment* — not inside a name."""
    await store.put("a..b.txt", b"x")
    assert await store.get("a..b.txt") == b"x"


# ── Restrictive file/dir permissions (PIPEDA/FIPPA — not world-readable) ─────


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes only")
@pytest.mark.asyncio
async def test_put_creates_blob_and_dirs_with_restrictive_modes(
    store: BlobStore, tmp_path: Path
) -> None:
    """Raw resume bytes must not be world-readable.

    A blob lands ``0o600``; every directory the store creates under root —
    including the *intermediate* ones (so the impl can't lean on
    ``mkdir(parents=True)``, which ignores ``mode`` for parents) — lands
    ``0o700``. A nested key exercises the intermediate-dir case.
    """
    await store.put("sub/dir/blob.bin", b"secret resume bytes")

    blob = tmp_path / "sub" / "dir" / "blob.bin"
    assert blob.stat().st_mode & 0o777 == 0o600

    for created_dir in (tmp_path / "sub", tmp_path / "sub" / "dir"):
        assert created_dir.stat().st_mode & 0o777 == 0o700


# ── get ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_missing_raises_blob_not_found(store: BlobStore) -> None:
    with pytest.raises(BlobNotFound):
        await store.get("nope.txt")


# ── delete ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_removes_the_file(store: BlobStore, tmp_path: Path) -> None:
    await store.put("k", b"x")
    await store.delete("k")
    assert not (tmp_path / "k").exists()
    with pytest.raises(BlobNotFound):
        await store.get("k")


@pytest.mark.asyncio
async def test_delete_missing_key_is_a_noop_success(store: BlobStore) -> None:
    """Idempotent retention — hris swallows NoSuchKey; re-running must not raise."""
    await store.delete("never-existed")


# ── exists ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_exists_tracks_put_and_delete(store: BlobStore) -> None:
    assert await store.exists("k") is False
    await store.put("k", b"x")
    assert await store.exists("k") is True
    await store.delete("k")
    assert await store.exists("k") is False


@pytest.mark.asyncio
async def test_exists_is_false_for_a_directory(store: BlobStore) -> None:
    """``exists`` is true iff a *regular file* — a parent dir must read False."""
    await store.put("d/f.txt", b"x")
    assert await store.exists("d") is False


# ── list_keys ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_keys_empty_store_returns_empty(store: BlobStore) -> None:
    assert await store.list_keys() == []


@pytest.mark.asyncio
async def test_list_keys_returns_all_keys_sorted_and_posix(store: BlobStore) -> None:
    for key in ("b.txt", "a.txt", "nested/deep/c.txt"):
        await store.put(key, b"x")
    # Sorted, root-relative, forward-slash separators even on Windows walks.
    assert await store.list_keys() == ["a.txt", "b.txt", "nested/deep/c.txt"]


@pytest.mark.asyncio
async def test_list_keys_honours_prefix_recursively(store: BlobStore) -> None:
    await store.put("resumes/1.pdf", b"x")
    await store.put("resumes/sub/2.pdf", b"x")
    await store.put("jobs/9.txt", b"x")
    assert await store.list_keys("resumes") == [
        "resumes/1.pdf",
        "resumes/sub/2.pdf",
    ]


@pytest.mark.asyncio
async def test_list_keys_lists_files_not_directories(store: BlobStore) -> None:
    await store.put("d/f.txt", b"x")
    assert await store.list_keys() == ["d/f.txt"]


@pytest.mark.asyncio
async def test_list_keys_excludes_symlinks_escaping_root(
    rooted: tuple[Path, Path, BlobStore],
) -> None:
    """The read side must be as strict as the write side.

    ``get``/``put``/``exists``/``delete`` reject a key that symlinks outside
    root, but ``list_keys`` walks with ``rglob`` + ``is_file()`` which FOLLOWS
    symlinks — so a link inside root pointing at an outside file would be
    enumerated (a listing leak). The store must realpath-filter walked entries
    to those genuinely under root. A genuine in-root file is planted too, so the
    test proves the filter is *selective*, not a blanket "drop all symlinks".
    """
    base, root, store = rooted
    outside_file = base / "outside_secret.txt"
    outside_file.write_bytes(b"secret")
    link = root / "leak.txt"
    try:
        link.symlink_to(outside_file)
    except (OSError, NotImplementedError):
        pytest.skip("platform cannot create symlinks")
    # Sanity: the link really is a doorway out of the root (test isn't vacuous).
    assert link.resolve() == outside_file.resolve()
    assert not link.resolve().is_relative_to(root.resolve())

    await store.put("real.txt", b"ok")

    keys = await store.list_keys("")

    assert "real.txt" in keys  # selective: the genuine in-root file is listed
    assert "leak.txt" not in keys  # the escaping symlink is filtered out


# ── content_type parity ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_put_accepts_content_type_and_is_harmless(store: BlobStore) -> None:
    """The param exists for MinIO call-site parity; a filesystem store ignores it."""
    await store.put("f.pdf", b"%PDF-1.4", content_type="application/pdf")
    assert await store.get("f.pdf") == b"%PDF-1.4"


# ── Construction ────────────────────────────────────────────────────────────


def test_construction_creates_a_missing_root(tmp_path: Path) -> None:
    root = tmp_path / "does" / "not" / "exist"
    assert not root.exists()
    BlobStore(root)
    assert root.is_dir()


def test_construction_accepts_str_and_path(tmp_path: Path) -> None:
    BlobStore(str(tmp_path / "as_str"))
    BlobStore(tmp_path / "as_path")


# ── Exception taxonomy ──────────────────────────────────────────────────────


def test_invalid_blob_key_is_a_value_error() -> None:
    assert issubclass(InvalidBlobKey, ValueError)


def test_blob_not_found_is_an_exception() -> None:
    assert issubclass(BlobNotFound, Exception)


# ── Path-traversal safety (the merge-blocking security core) ────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", INVALID_KEYS)
async def test_put_rejects_bad_keys_and_writes_nothing_outside_root(
    bad: str, rooted: tuple[Path, Path, BlobStore]
) -> None:
    base, root, store = rooted
    with pytest.raises(InvalidBlobKey):
        await store.put(bad, b"pwned")
    # Both halves: the exception *and* that no file leaked out of the root.
    assert _escaped_files(base, root) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", INVALID_KEYS)
async def test_get_rejects_bad_keys(
    bad: str, rooted: tuple[Path, Path, BlobStore]
) -> None:
    """``get('/etc/passwd')`` must not read a real file outside the root."""
    _, _, store = rooted
    with pytest.raises(InvalidBlobKey):
        await store.get(bad)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", INVALID_KEYS)
async def test_exists_rejects_bad_keys(
    bad: str, rooted: tuple[Path, Path, BlobStore]
) -> None:
    """``exists`` must not probe existence of files outside the root."""
    _, _, store = rooted
    with pytest.raises(InvalidBlobKey):
        await store.exists(bad)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", INVALID_KEYS)
async def test_delete_rejects_bad_keys(
    bad: str, rooted: tuple[Path, Path, BlobStore]
) -> None:
    """Every bad key — traversal, root/empty, and null-byte — is rejected and
    nothing under the root's parent is touched."""
    base, root, store = rooted
    with pytest.raises(InvalidBlobKey):
        await store.delete(bad)
    assert _escaped_files(base, root) == []


@pytest.mark.asyncio
async def test_delete_rejects_traversal_and_keeps_outside_files(
    rooted: tuple[Path, Path, BlobStore],
) -> None:
    base, root, store = rooted
    victim = base / "victim.txt"
    victim.write_bytes(b"keep me")
    with pytest.raises(InvalidBlobKey):
        await store.delete("../victim.txt")
    assert victim.read_bytes() == b"keep me"
    assert _escaped_files(base, root) == [victim]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", INVALID_PREFIXES)
async def test_list_keys_rejects_traversal_prefix(
    bad: str, rooted: tuple[Path, Path, BlobStore]
) -> None:
    """A non-empty prefix must be validated like a key: a traversal walks
    outside the root (``list_keys('/etc')`` leaks) and a null-byte prefix must
    raise ``InvalidBlobKey``, not a bare ``ValueError``."""
    _, _, store = rooted
    with pytest.raises(InvalidBlobKey):
        await store.list_keys(bad)


@pytest.mark.asyncio
async def test_symlink_escape_is_rejected_and_writes_nothing_outside(
    rooted: tuple[Path, Path, BlobStore],
) -> None:
    """A symlink planted inside the root pointing out must not be a doorway.

    A guard that only string-checks for ``..`` segments would pass the key
    ``link/escape.txt`` (no ``..``) and write through the symlink; a guard that
    realpath-resolves and asserts ``is_relative_to(root)`` rejects it. Skips
    cleanly where the OS can't symlink, but MUST run under Linux CI.
    """
    base, root, store = rooted
    outside = base / "outside"
    outside.mkdir()
    link = root / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("platform cannot create symlinks")

    with pytest.raises(InvalidBlobKey):
        await store.put("link/escape.txt", b"pwned")
    assert not (outside / "escape.txt").exists()
