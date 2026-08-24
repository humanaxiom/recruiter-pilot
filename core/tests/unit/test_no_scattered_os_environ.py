"""Static-invariant guard for CLAUDE.md's config discipline rule.

CLAUDE.md: "Config only via ``src/settings.py`` ... never ``os.environ``
scattered in code." ``src/settings.py``'s own module docstring restates it as
a hard contract: "Nothing else in the codebase may read ``os.environ``."

That invariant was violated in ``src/pipeline/matching/orchestrator.py``
(``os.environ.get("GIT_SHA")`` at two call sites) until a reviewer finding on
Phase 4c fixed it by threading ``git_sha`` through ``Settings`` ->
``MatchingContext``. This test makes the rule enforceable by the gate rather
than by review alone: it walks every ``.py`` module under ``src/`` and fails
if any module OTHER than ``settings.py`` reads ``os.environ`` / ``os.getenv``.

AST-based (not a text grep) so it does not false-positive on comments or
docstrings that merely *mention* ``os.environ`` (as this module's own
docstring, and a code comment in ``orchestrator.py``, do).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_SRC_ROOT = Path(__file__).resolve().parents[2] / "src"

# The one legitimate reader, per settings.py's own docstring contract.
_ALLOWED_READERS = {_SRC_ROOT / "settings.py"}

_BANNED_OS_ATTRS = {"environ", "getenv"}


def _iter_src_files() -> list[Path]:
    assert _SRC_ROOT.is_dir(), f"expected src root at {_SRC_ROOT}"
    return sorted(_SRC_ROOT.rglob("*.py"))


def _os_environ_reads(path: Path) -> list[str]:
    """Return a list of human-readable violation descriptions for `path`,
    empty if the module never reads ``os.environ``/``os.getenv``."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    # Track names bound to `os` (handles `import os` and `import os as _os`)
    # and names bound directly to `environ`/`getenv` via `from os import ...`.
    os_aliases: set[str] = set()
    direct_aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "os":
                    os_aliases.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module == "os":
            for alias in node.names:
                if alias.name in _BANNED_OS_ATTRS:
                    direct_aliases.add(alias.asname or alias.name)

    violations: list[str] = []
    for node in ast.walk(tree):
        # os.environ / os.getenv(...)
        if (
            isinstance(node, ast.Attribute)
            and node.attr in _BANNED_OS_ATTRS
            and isinstance(node.value, ast.Name)
            and node.value.id in os_aliases
        ):
            violations.append(f"{path}:{node.lineno}: os.{node.attr}")
        # environ / getenv used directly after `from os import environ`
        elif isinstance(node, ast.Name) and node.id in direct_aliases:
            violations.append(f"{path}:{node.lineno}: {node.id} (from os import ...)")

    return violations


def test_no_module_other_than_settings_reads_os_environ() -> None:
    """The static equivalent of the reviewer finding: grep-proof this so a
    future ``os.environ.get(...)`` sneaking into any module under ``src/``
    (other than ``settings.py``) fails the gate instead of only review."""
    offenders: dict[Path, list[str]] = {}
    for path in _iter_src_files():
        if path in _ALLOWED_READERS:
            continue
        violations = _os_environ_reads(path)
        if violations:
            offenders[path] = violations

    assert not offenders, (
        "os.environ/os.getenv read outside src/settings.py — config must be "
        "sourced via Settings (CLAUDE.md, settings.py docstring):\n"
        + "\n".join(v for vs in offenders.values() for v in vs)
    )


def test_allowlist_points_at_the_one_real_settings_module() -> None:
    """Sanity check on the allowlist itself, not the detector: settings.py
    must exist at the expected path and be the ONLY allowlisted module — if
    settings.py ever moves, this fails loudly instead of silently widening
    the allowlist to a stale path (which would let a real violation through
    unnoticed)."""
    settings_path = _SRC_ROOT / "settings.py"
    assert settings_path.is_file()
    assert _ALLOWED_READERS == {settings_path}


@pytest.mark.parametrize(
    "snippet",
    [
        'import os\nos.environ.get("X")\n',
        'import os\nos.getenv("X")\n',
        'from os import environ\nenviron.get("X")\n',
        'from os import getenv\ngetenv("X")\n',
        'import os as _os\n_os.environ.get("X")\n',
    ],
)
def test_detector_catches_every_os_environ_read_shape(
    tmp_path: Path, snippet: str
) -> None:
    """Pin the detector's own correctness against the shapes it must catch —
    otherwise the walker above could silently stop detecting real violations
    (e.g. after a refactor) and this whole regression guard would be inert."""
    p = tmp_path / "module.py"
    p.write_text(snippet, encoding="utf-8")
    assert _os_environ_reads(p) != []


def test_detector_does_not_false_positive_on_comments_or_strings() -> None:
    """Mirrors the real orchestrator.py/settings.py situation: modules that
    merely *mention* os.environ in a comment or docstring must NOT trip the
    detector."""
    snippet = (
        '"""This module never reads os.environ, see os.getenv too."""\n'
        "# os.environ.get('GIT_SHA') is forbidden here, per settings.py\n"
        "x = 1\n"
    )
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "mod.py"
        f.write_text(snippet, encoding="utf-8")
        assert _os_environ_reads(f) == []
