"""Gate-scope meta-test — pins that ``core/frontend/`` is actually covered by
the non-negotiable gate suite (CLAUDE.md: "A single red gate = the work is
not done"), not silently invisible to it.

**The hole this test exists to close.** Today ``Makefile``'s ``gates`` /
``gates-fast`` targets and ``.github/workflows/ci.yml``'s ``static`` / ``unit``
jobs scope every ruff/black/mypy invocation (and the coverage flag) to
``src tests`` only — ``core/frontend/`` (the Phase 7 Flask viewer) is not
linted, not type-checked, not formatted-checked, and not counted toward the
coverage gate by ANY of the commands CI or a local ``make gates`` run actually
executes. A future frontend bug (or a redaction-boundary regression in the
viewer itself) could ship merged with a fully green gate run and nobody would
know, because the gate literally never looks at the directory.

This is a pure text-parsing meta-test (no code import), mirroring
``test_no_scattered_os_environ.py``'s "make the rule enforceable by the gate,
not just by convention" approach — but here the rule is enforced by grepping
the gate DEFINITIONS themselves (``Makefile`` / ``ci.yml``), since there is no
Python AST to walk for a build-tool config file.

Assertions are written against the SHAPE of the recipe (which lines invoke
ruff/black/mypy/pytest, and whether ``frontend`` appears as a whole path
segment/word on those lines) rather than against exact whitespace/column
positions, so a reasonable reformatting of the Makefile/ci.yml (e.g. reordering
the two paths, adding a comment) does not spuriously break this test — only an
actual scope regression (frontend dropped from an invocation) does.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_MAKEFILE = _REPO_ROOT / "Makefile"
_CI_YML = _REPO_ROOT / ".github" / "workflows" / "ci.yml"

_FRONTEND_WORD = re.compile(r"(?<![\w./-])frontend(?![\w./-])")


def _makefile_text() -> str:
    assert _MAKEFILE.is_file(), f"expected a Makefile at {_MAKEFILE}"
    return _MAKEFILE.read_text(encoding="utf-8")


def _ci_text() -> str:
    assert _CI_YML.is_file(), f"expected a CI workflow at {_CI_YML}"
    return _CI_YML.read_text(encoding="utf-8")


def _make_recipe(makefile_text: str, target: str) -> str:
    """Return the tab-indented recipe body immediately under ``target:``.

    Matches from the target's header line up to (not including) the first
    following line that is NOT recipe-indented (a blank line, a comment, or
    the next target) — i.e. exactly the shell commands ``make`` would run for
    that target.
    """
    pattern = re.compile(rf"^{re.escape(target)}:.*\n((?:\t.*\n?)*)", re.MULTILINE)
    m = pattern.search(makefile_text)
    assert m is not None, f"Makefile target {target!r} not found"
    return m.group(1)


def _joined(recipe: str) -> str:
    """Collapse a Make recipe's backslash-newline shell continuations into one
    logical line per shell command, so a flag split across ``\\``-continued
    lines (e.g. the ``pytest ... \\n --cov=...`` shape already in this
    Makefile) is still matched as a single command string."""
    return re.sub(r"\\\s*\n\t?", " ", recipe)


def _yaml_job_block(ci_text: str, job_name: str) -> str:
    """Return the raw text of one top-level job block (2-space indented key
    directly under ``jobs:``) up to the next 2-space-indented key or EOF."""
    pattern = re.compile(
        rf"\n  {re.escape(job_name)}:\n(.*?)(?=\n  [A-Za-z0-9_-]+:\n|\Z)",
        re.DOTALL,
    )
    m = pattern.search(ci_text)
    assert m is not None, f"ci.yml job {job_name!r} not found"
    return m.group(1)


def _lines_containing(text: str, needle: str) -> list[str]:
    return [line for line in text.splitlines() if needle in line]


# ── Makefile: `gates` target ─────────────────────────────────────────────


def test_makefile_gates_ruff_check_lints_frontend() -> None:
    recipe = _make_recipe(_makefile_text(), "gates")
    ruff_lines = _lines_containing(recipe, "ruff check")
    assert ruff_lines, "`gates` target must run `ruff check` at all"
    for line in ruff_lines:
        assert _FRONTEND_WORD.search(line), (
            "`gates`'s `ruff check` invocation must include `frontend` "
            f"as a lint target, got: {line!r}"
        )


def test_makefile_gates_black_check_formats_frontend() -> None:
    recipe = _make_recipe(_makefile_text(), "gates")
    black_lines = _lines_containing(recipe, "black --check")
    assert black_lines, "`gates` target must run `black --check` at all"
    for line in black_lines:
        assert _FRONTEND_WORD.search(line), (
            "`gates`'s `black --check` invocation must include `frontend`, "
            f"got: {line!r}"
        )


def test_makefile_gates_mypy_type_checks_frontend() -> None:
    recipe = _make_recipe(_makefile_text(), "gates")
    mypy_lines = _lines_containing(recipe, "mypy ")
    assert mypy_lines, "`gates` target must run `mypy` at all"
    for line in mypy_lines:
        assert _FRONTEND_WORD.search(
            line
        ), f"`gates`'s `mypy` invocation must include `frontend`, got: {line!r}"


def test_makefile_gates_coverage_covers_frontend_alongside_src() -> None:
    recipe = _joined(_make_recipe(_makefile_text(), "gates"))
    assert "--cov=src" in recipe, "`gates` must still measure coverage on src"
    assert "--cov=frontend" in recipe, (
        "`gates`'s coverage invocation must ALSO measure `--cov=frontend` "
        "alongside `--cov=src` — the frontend viewer must count toward the "
        "coverage gate, not be invisible to it"
    )


# ── Makefile: `gates-fast` target ────────────────────────────────────────


def test_makefile_gates_fast_ruff_check_lints_frontend() -> None:
    recipe = _make_recipe(_makefile_text(), "gates-fast")
    ruff_lines = _lines_containing(recipe, "ruff check")
    assert ruff_lines, "`gates-fast` target must run `ruff check` at all"
    for line in ruff_lines:
        assert _FRONTEND_WORD.search(line), (
            f"`gates-fast`'s `ruff check` invocation must include `frontend`, "
            f"got: {line!r}"
        )


def test_makefile_gates_fast_black_check_formats_frontend() -> None:
    recipe = _make_recipe(_makefile_text(), "gates-fast")
    black_lines = _lines_containing(recipe, "black --check")
    assert black_lines, "`gates-fast` target must run `black --check` at all"
    for line in black_lines:
        assert _FRONTEND_WORD.search(line), (
            f"`gates-fast`'s `black --check` invocation must include "
            f"`frontend`, got: {line!r}"
        )


def test_makefile_gates_fast_mypy_type_checks_frontend() -> None:
    recipe = _make_recipe(_makefile_text(), "gates-fast")
    mypy_lines = _lines_containing(recipe, "mypy ")
    assert mypy_lines, "`gates-fast` target must run `mypy` at all"
    for line in mypy_lines:
        assert _FRONTEND_WORD.search(line), (
            f"`gates-fast`'s `mypy` invocation must include `frontend`, "
            f"got: {line!r}"
        )


# ── ci.yml: `static` job ─────────────────────────────────────────────────


def test_ci_static_job_ruff_check_lints_frontend() -> None:
    block = _yaml_job_block(_ci_text(), "static")
    ruff_lines = _lines_containing(block, "ruff check")
    assert ruff_lines, "CI `static` job must run `ruff check` at all"
    for line in ruff_lines:
        assert _FRONTEND_WORD.search(line), (
            f"CI `static` job's `ruff check` step must include `frontend`, "
            f"got: {line!r}"
        )


def test_ci_static_job_black_check_formats_frontend() -> None:
    block = _yaml_job_block(_ci_text(), "static")
    black_lines = _lines_containing(block, "black --check")
    assert black_lines, "CI `static` job must run `black --check` at all"
    for line in black_lines:
        assert _FRONTEND_WORD.search(line), (
            f"CI `static` job's `black --check` step must include `frontend`, "
            f"got: {line!r}"
        )


def test_ci_static_job_mypy_type_checks_frontend() -> None:
    block = _yaml_job_block(_ci_text(), "static")
    mypy_lines = _lines_containing(block, "mypy ")
    assert mypy_lines, "CI `static` job must run `mypy` at all"
    for line in mypy_lines:
        assert _FRONTEND_WORD.search(line), (
            f"CI `static` job's `mypy` step must include `frontend`, " f"got: {line!r}"
        )


# ── ci.yml: `unit` job (coverage) ────────────────────────────────────────


def test_ci_unit_job_coverage_covers_frontend_alongside_src() -> None:
    block = _joined(_yaml_job_block(_ci_text(), "unit"))
    assert "--cov=src" in block, "CI `unit` job must still measure coverage on src"
    assert "--cov=frontend" in block, (
        "CI `unit` job's coverage invocation must ALSO measure "
        "`--cov=frontend` alongside `--cov=src` — matching the Makefile's "
        "`gates` target so local and CI coverage scope cannot drift apart"
    )


# ── Sanity checks on the meta-test's own machinery ───────────────────────


@pytest.mark.parametrize("target", ["gates", "gates-fast"])
def test_make_recipe_helper_finds_a_non_empty_recipe(target: str) -> None:
    """Guards the extractor itself: if `_make_recipe` ever silently matched
    nothing (e.g. after a Makefile reformat this helper doesn't understand),
    every assertion above would vacuously pass on an empty ruff_lines/
    black_lines list rather than failing loud — this pins that the recipe
    body is genuinely non-empty today."""
    recipe = _make_recipe(_makefile_text(), target)
    assert recipe.strip(), f"`{target}` recipe parsed as empty — extractor is broken"


@pytest.mark.parametrize("job", ["static", "unit"])
def test_yaml_job_block_helper_finds_a_non_empty_block(job: str) -> None:
    block = _yaml_job_block(_ci_text(), job)
    assert block.strip(), f"ci.yml job {job!r} parsed as empty — extractor is broken"
