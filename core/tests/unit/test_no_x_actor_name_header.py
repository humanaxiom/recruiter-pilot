"""Static-invariant guard — FU-5 slice 7, ADR-019 §8.3.

ADR-019 §8.3: "the optional, unverified ``X-Actor-Name`` header ... is
REMOVED ENTIRELY. A spoofable identity-shaped header next to a
cryptographically verified one invites confusion and misconfiguration;
removing ``X-Actor-Name`` closes that risk." The header's reading function,
``resolve_actor``, is deleted along with it (see ``test_api_deps.py``'s
``test_resolve_actor_no_longer_exists`` for the direct pin on the function
object).

This test makes the ADR's "removed entirely" a GATE, not a promise honoured
only by code review: it walks every ``.py`` file under ``src/`` (a plain text
scan, mirroring the "grep-style meta-test" the tester mandate asked for —
unlike ``test_no_scattered_os_environ.py``'s AST walk, a substring match is
sufficient here because there is no legitimate reason for either string to
appear ANYWHERE in application source, not even in a comment; an AST walk
would in fact be weaker, since it would miss a comment/docstring mention that
this test intentionally still catches — a lingering reference is itself a
sign the deletion was incomplete).

A comment, a docstring, a ``Header(alias=...)`` declaration, a dict key, an
f-string — none of it may still say ``X-Actor-Name`` or ``resolve_actor``
under ``src/``. The one exception is this test file's OWN docstring/logic
(under ``tests/``, outside the walked root, so no self-reference problem
exists to allowlist).
"""

from __future__ import annotations

from pathlib import Path

_SRC_ROOT = Path(__file__).resolve().parents[2] / "src"

_BANNED_STRINGS: tuple[str, ...] = ("X-Actor-Name", "resolve_actor")


def _iter_src_files() -> list[Path]:
    assert _SRC_ROOT.is_dir(), f"expected src root at {_SRC_ROOT}"
    return sorted(_SRC_ROOT.rglob("*.py"))


def _banned_string_hits(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    hits: list[str] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for banned in _BANNED_STRINGS:
            if banned in line:
                hits.append(f"{path}:{lineno}: {banned!r} — {line.strip()!r}")
    return hits


def test_x_actor_name_and_resolve_actor_appear_nowhere_under_src() -> None:
    """ADR-019 §8.3 — the positive gate for the deletion. A future
    reintroduction of either string anywhere under ``src/`` (a stray comment,
    a resurrected function, a copy-pasted docstring) fails this test
    immediately instead of silently reopening the spoofable-header risk the
    ADR closed."""
    offenders: list[str] = []
    for path in _iter_src_files():
        offenders.extend(_banned_string_hits(path))

    assert not offenders, (
        "X-Actor-Name / resolve_actor must not appear anywhere under src/ "
        "(ADR-019 §8.3: 'removed entirely'):\n" + "\n".join(offenders)
    )


def test_src_root_points_at_the_real_source_tree() -> None:
    """Sanity check on the walked root itself — if ``src/`` ever moves, this
    fails loudly instead of silently walking an empty/stale directory and
    reporting a false-clean guard."""
    assert _SRC_ROOT.is_dir()
    assert (_SRC_ROOT / "api" / "deps.py").is_file()


def test_detector_catches_a_planted_x_actor_name_reference(tmp_path: Path) -> None:
    """Pin the detector's own correctness — a planted reference in a
    throwaway file must be caught, so a future refactor of the scan itself
    cannot silently go blind."""
    planted = tmp_path / "module.py"
    planted.write_text(
        '"""A stray comment mentioning X-Actor-Name."""\n', encoding="utf-8"
    )
    assert _banned_string_hits(planted) != []


def test_detector_catches_a_planted_resolve_actor_reference(tmp_path: Path) -> None:
    planted = tmp_path / "module.py"
    planted.write_text("def resolve_actor() -> None: ...\n", encoding="utf-8")
    assert _banned_string_hits(planted) != []


def test_detector_is_clean_against_a_module_with_neither_string(
    tmp_path: Path,
) -> None:
    planted = tmp_path / "module.py"
    planted.write_text(
        'def resolve_user() -> None:\n    """Reads a session cookie."""\n',
        encoding="utf-8",
    )
    assert _banned_string_hits(planted) == []
