"""RED pin — ``scripts/*.ps1`` must be readable by Windows PowerShell 5.1.

**The defect this closes, found live 2026-08-13.** ``scripts/quickstart.ps1`` —
the documented fresh-box entry point, and the ONLY way to boot the stack since
ADR-034 made the API refuse to start without generated keys — contains 32 lines
of non-ASCII characters (em-dashes, ``▶``, ``✔``, box-drawing rules) and was
saved as UTF-8 **without a BOM**.

Windows PowerShell 5.1 (``powershell.exe``) reads a BOM-less ``.ps1`` as the
legacy ANSI codepage, so every em-dash decodes to ``â€"`` and the script dies in
a cascade of ``ParserError`` — **before executing a single line**. PowerShell 7
(``pwsh``) defaults to UTF-8 and is fine.

**The part that makes it dangerous rather than merely annoying:
``powershell.exe`` still exits 0.** A caller — a human reading a terminal in a
hurry, a CI step, or an agent — that checks the exit status concludes the stack
booted when nothing ran at all. This was observed exactly that way.

**Why a BOM and not "just use ASCII".** Stripping the punctuation would fix
today's file and silently re-break the moment somebody types an em-dash in a new
comment — the ROADMAP A7 shape, an invariant that holds only while nobody edits
the file. A BOM makes the file robust to *future* non-ASCII additions. That
still leaves "the BOM must be there" as an invariant, which is what this test
is: **the assertion that would have caught it**.

Either property is accepted, because either one is genuinely safe:

* pure ASCII — no encoding to get wrong; or
* a UTF-8 BOM — 5.1 and 7 both decode it correctly.

A file that is neither is the broken combination, and it fails here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_UTF8_BOM = b"\xef\xbb\xbf"

#: Repo root: core/tests/unit/<this file> -> parents[3].
_SCRIPTS_DIR = Path(__file__).resolve().parents[3] / "scripts"


def _powershell_scripts() -> list[Path]:
    if not _SCRIPTS_DIR.is_dir():
        return []
    return sorted(_SCRIPTS_DIR.glob("*.ps1"))


def test_there_is_at_least_one_powershell_script_to_check() -> None:
    """Guards against this whole file passing vacuously if the scripts move."""
    assert _powershell_scripts(), (
        f"no .ps1 files found under {_SCRIPTS_DIR} — this test has stopped "
        "checking anything; update the path rather than deleting the guard"
    )


@pytest.mark.parametrize(
    "script", _powershell_scripts(), ids=lambda p: p.name  # type: ignore[misc]
)
def test_script_is_ascii_or_carries_a_utf8_bom(script: Path) -> None:
    raw = script.read_bytes()

    if raw.startswith(_UTF8_BOM):
        return  # 5.1 and 7 both decode this correctly.

    try:
        raw.decode("ascii")
    except UnicodeDecodeError as exc:
        offending = raw[exc.start : exc.end]
        line = raw[: exc.start].count(b"\n") + 1
        pytest.fail(
            f"{script.name} contains the non-ASCII byte sequence {offending!r} "
            f"at line {line} and has NO UTF-8 BOM.\n\n"
            "Windows PowerShell 5.1 reads a BOM-less .ps1 as the legacy ANSI "
            "codepage, so this file will fail to PARSE there — and "
            "powershell.exe still exits 0, so the failure looks like success.\n\n"
            "Fix: re-save the file as UTF-8 WITH a BOM (in pwsh: "
            "[System.IO.File]::WriteAllText($p, $text, "
            "(New-Object System.Text.UTF8Encoding $true))), or keep it pure "
            "ASCII."
        )
