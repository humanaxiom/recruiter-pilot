"""Versioned LLM prompts.

Each prompt is a pair of jinja2 templates (`<name>.system.j2` and
`<name>.user.j2`) under `templates/`. The version number is in the
filename; rolling a new prompt = new file = new ``RenderedPrompt.version``.

Loader:

    >>> p = load_prompt("jd_extract_v1", jd_text="Senior Python engineer ...")
    >>> p.version
    'jd_extract_v1'
    >>> p.messages
    [{"role": "system", "content": "..."}, {"role": "user", "content": "..."}]

The ``messages`` shape is what LLMClient.chat / chat_json take directly.

Ported near-verbatim from hris
``packages/prompts/src/prompts/__init__.py`` (see
``phase3-source-dossier.md`` §9). Phase 3 shipped four prompt pairs:
``jd_extract_v1``, ``resume_core_v1``, ``resume_skills_v2``,
``cover_letter_v1``. Phase 4c adds the shortlist-evidence pair
``shortlist_evidence_v1`` (and ``shortlist_evidence_v2``, the cover-letter
variant), copied verbatim from hris — the matching orchestrator's stage-3
per-requirement evidence prompt.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jinja2

_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

_env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(str(_TEMPLATES_DIR)),
    autoescape=False,  # noqa: S701 — these are LLM prompts, not HTML
    keep_trailing_newline=True,
    undefined=jinja2.StrictUndefined,
)


@dataclass(frozen=True)
class RenderedPrompt:
    """A rendered prompt ready to hand to LLMClient.chat[/_json]."""

    version: str
    system: str
    user: str

    @property
    def messages(self) -> list[dict[str, str]]:
        return [
            {"role": "system", "content": self.system},
            {"role": "user", "content": self.user},
        ]


def load_prompt(name: str, /, **vars: Any) -> RenderedPrompt:
    """Render ``<name>.{system,user}.j2`` with ``vars``.

    Raises jinja2.TemplateNotFound if either half is missing; that's a
    deliberate fail-fast so a typo in the prompt name doesn't silently
    drop the system prompt. Raises jinja2.UndefinedError if a template
    references a variable that wasn't supplied (StrictUndefined).
    """
    system = _env.get_template(f"{name}.system.j2").render(**vars)
    user = _env.get_template(f"{name}.user.j2").render(**vars)
    return RenderedPrompt(version=name, system=system, user=user)


def list_prompts() -> list[str]:
    """Return the set of available prompt versions (basenames)."""
    versions: set[str] = set()
    for path in _TEMPLATES_DIR.glob("*.system.j2"):
        versions.add(path.name.removesuffix(".system.j2"))
    return sorted(versions)


__all__ = ["RenderedPrompt", "list_prompts", "load_prompt"]
