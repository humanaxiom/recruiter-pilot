"""Generate a synthetic résumé corpus — one résumé PDF per real job description.

**Why this exists.** `scripts/smoke.sh` and `scripts/model-check.sh` both need
résumé PDFs (`fixtures/resumes/*_resume.pdf`), and until now the only PDFs in
`fixtures/` were **real applicants'** — names, personal email addresses and phone
numbers. That made the fixture corpus unpublishable, unshippable to a new machine,
and impossible to put in CI, so every fresh deployment either went without its two
most valuable verification harnesses or moved third-party PII around to get them.

This produces a corpus with the same *shape* and none of the exposure. It is
deliberately generated **from the real JDs**, so the résumés reference skills the
job descriptions actually ask for and the matching engine has something real to rank —
a corpus of unrelated lorem would certify a broken ranker as healthy, which is the
same trap `model_probe_live` documents for synthetic prompts.

**Nothing here can collide with a real person.** Emails use `example.invalid`
(RFC 2606 reserves `.invalid` as permanently unresolvable) and phone numbers use
the `555-01xx` block (reserved for fiction). Every generated PDF carries a
`Synthetic-Fixture` marker in its metadata, and `synthetic_marker_present()` lets
a test assert it — so a real résumé cannot be mistaken for a generated one, in
either direction.

**Deterministic.** Output is seeded from the JD filename, so re-running produces
byte-identical text for the same input. A fixture corpus that changes under you is
not a fixture corpus.

Never imported by the application. Entry point is `scripts/gen-fixtures.sh`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

#: RFC 2606 §2 reserves `.invalid` as a TLD guaranteed never to resolve, and the
#: 555-0100..555-0199 range is reserved for fictional use. Using anything else
#: risks generating a real person's contact details by coincidence.
_EMAIL_DOMAIN = "example.invalid"
_PHONE_PREFIX = "555-01"

#: Stamped into every generated PDF's metadata. A test asserts its presence, so
#: "is this file synthetic?" is answerable mechanically rather than by filename
#: convention — filenames get renamed, metadata travels with the bytes.
SYNTHETIC_MARKER = "Synthetic-Fixture"

#: Deliberately ordinary-sounding but arbitrary given/family name pools. The
#: cross-product is large enough that the seeded pick is effectively unique per
#: JD, and the reserved email/phone ranges mean a coincidental real-name match
#: still carries no real contact information.
_GIVEN = (
    "Avery Rowan Sasha Devon Quinn Marlowe Ellis Harper Reese Emerson Tatum Blair "
    "Sawyer Lennox Arden Kai Noor Rin Zaid Imani Anouk Mattias Yara Soren"
).split()
_FAMILY = (
    "Calloway Whitfield Marchetti Okonkwo Lindqvist Baptiste Nakamura Aldridge "
    "Ferreira Balogun Vasquez Thornbury Ivanova Kaur Mensah Delacroix Rahimi "
    "Sandoval Petrov Achebe Lindgren Osei Novak Farrow"
).split()

_ROLES = (
    "Program Assistant",
    "Administrative Coordinator",
    "Operations Specialist",
    "Project Officer",
    "Research Assistant",
    "Student Services Advisor",
)

_EMPLOYERS = (
    "Northline Institute",
    "Harbourview College",
    "Cedar Ridge Services",
    "Bellwether Group",
    "Summit Lake Foundation",
    "Fairmount Public Trust",
)

#: How much of the JD's vocabulary a generated résumé covers. Varying this makes
#: the corpus rankable — a pool where every candidate matches everything cannot
#: demonstrate that ordering works. Seeded per JD, so the spread is stable.
_COVERAGE_TIERS = (("strong", 0.85), ("partial", 0.55), ("weak", 0.3))


@dataclass
class SynthPerson:
    """A generated identity. Every field is drawn from a reserved range."""

    name: str
    email: str
    phone: str
    role: str
    employer: str
    coverage: str
    skills: list[str] = field(default_factory=list)


def _seed(text: str) -> int:
    """Stable across processes and platforms — `hash()` is salted per run."""
    return int(hashlib.sha256(text.encode("utf-8")).hexdigest()[:12], 16)


def _pick(pool: tuple[str, ...] | list[str], seed: int, salt: int = 0) -> str:
    return pool[(seed + salt) % len(pool)]


def load_vocabulary(skill_data: Path) -> list[str]:
    """Canonical skill names from `aliases.yaml`, read WITHOUT importing yaml.

    A deliberately dumb line scan: this module must run in the bare container
    that `gen-fixtures.sh` starts, and the point is to avoid pulling the
    application's dependency graph into a fixture generator.
    """
    text = (skill_data / "aliases.yaml").read_text(encoding="utf-8")
    return [
        m.group(1).strip()
        for m in re.finditer(r"^-\s*canonical:\s*(.+)$", text, re.MULTILINE)
    ]


def skills_for_jd(jd_text: str, vocabulary: list[str], coverage: float) -> list[str]:
    """Vocabulary terms the JD actually mentions, truncated to `coverage`.

    Matching on the real JD text is the load-bearing part. A résumé built from
    an arbitrary skill list would rank identically against every posting, and a
    corpus like that cannot tell a working ranker from a broken one.

    **Word-boundary matching, not substring.** The first version used
    ``skill in lowered`` and generated a Student Services Advisor whose skills
    were "go, scala, ssis" — because ``ssis`` is inside *assist*, ``go`` is
    inside *goals*, and ``scala`` is inside *escalate*. Short vocabulary entries
    turn a substring test into a false-positive generator, and a corpus of
    plausible-looking nonsense is worse than an obviously empty one: it would
    have been measured against for weeks before anyone read a résumé.
    """
    lowered = jd_text.lower()
    present = [
        s
        for s in vocabulary
        if s and re.search(rf"(?<!\w){re.escape(s.lower())}(?!\w)", lowered)
    ]
    if not present:
        return []
    keep = max(1, round(len(present) * coverage))
    return present[:keep]


def person_for_jd(jd_name: str, jd_text: str, vocabulary: list[str]) -> SynthPerson:
    seed = _seed(jd_name)
    tier, coverage = _COVERAGE_TIERS[seed % len(_COVERAGE_TIERS)]
    given = _pick(_GIVEN, seed)
    family = _pick(_FAMILY, seed, salt=7)
    handle = f"{given.lower()}.{family.lower()}"
    return SynthPerson(
        name=f"{given} {family}",
        email=f"{handle}@{_EMAIL_DOMAIN}",
        phone=f"{_PHONE_PREFIX}{seed % 100:02d}",
        role=_pick(_ROLES, seed, salt=3),
        employer=_pick(_EMPLOYERS, seed, salt=5),
        coverage=tier,
        skills=skills_for_jd(jd_text, vocabulary, coverage),
    )


def resume_lines(person: SynthPerson) -> list[str]:
    """The résumé body. Plain prose on purpose — the parser must cope with the
    shape of a document a person wrote, not a form the generator invented."""
    skills = person.skills or ["general office administration"]
    lines = [
        person.name,
        f"{person.email}  |  {person.phone}",
        "",
        "SUMMARY",
        f"{person.role} with experience in {', '.join(skills[:3])}. Comfortable "
        "coordinating across teams, maintaining records, and supporting "
        "day-to-day operations in a post-secondary environment.",
        "",
        "EXPERIENCE",
        f"{person.role} - {person.employer}   2023-present",
    ]
    for skill in skills[:6]:
        lines.append(f"  * Applied {skill} in support of departmental objectives.")
    lines += [
        "",
        f"Assistant - {_pick(_EMPLOYERS, _seed(person.name), salt=2)}   2021-2023",
        "  * Maintained scheduling, correspondence and records for a small team.",
        "  * Prepared reports and supported budget tracking.",
        "",
        "SKILLS",
    ]
    for chunk in range(0, len(skills), 6):
        lines.append("  " + ", ".join(skills[chunk : chunk + 6]))
    lines += [
        "",
        "EDUCATION",
        "  Bachelor of Arts, Northline Institute, 2021",
        "",
        f"[{SYNTHETIC_MARKER}] This document is generated test data. It "
        "describes no real person.",
    ]
    return lines


def write_pdf(lines: list[str], out: Path) -> None:
    """Render with PyMuPDF, already a runtime dependency (`import fitz`) — this
    generator deliberately adds no new package to the project."""
    import fitz  # type: ignore[import-untyped]

    doc = fitz.open()
    page = doc.new_page()
    y = 60.0
    for line in lines:
        if y > 760:
            page = doc.new_page()
            y = 60.0
        size = 15 if y == 60.0 and line and not line.startswith(" ") else 10
        page.insert_text((56, y), line, fontsize=size, fontname="helv")
        y += 15 if size > 10 else 13
    doc.set_metadata(
        {
            "title": lines[0],
            "subject": SYNTHETIC_MARKER,
            "creator": SYNTHETIC_MARKER,
            "author": lines[0],
        }
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out))
    doc.close()


def synthetic_marker_present(pdf: Path) -> bool:
    """True when a PDF was produced by this generator. Lets a test assert that
    a fixture directory contains no real résumé, mechanically."""
    import fitz

    with fitz.open(str(pdf)) as doc:
        meta = doc.metadata or {}
        return SYNTHETIC_MARKER in (meta.get("subject") or "") or SYNTHETIC_MARKER in (
            meta.get("creator") or ""
        )


def jd_text_of(path: Path) -> str:
    """Extract text from a JD .docx without importing the application."""
    import docx

    return "\n".join(p.text for p in docx.Document(str(path)).paragraphs)


def generate(jd_dir: Path, out_dir: Path, skill_data: Path) -> list[dict[str, str]]:
    vocabulary = load_vocabulary(skill_data)
    jds = sorted(p for p in jd_dir.glob("*.docx") if not p.name.startswith("~$"))
    if not jds:
        raise SystemExit(f"no .docx job descriptions under {jd_dir}")

    manifest: list[dict[str, str]] = []
    for index, jd in enumerate(jds, start=1):
        person = person_for_jd(jd.name, jd_text_of(jd), vocabulary)
        slug = person.name.lower().replace(" ", "_")
        out = out_dir / f"{index:03d}_{slug}_resume.pdf"
        write_pdf(resume_lines(person), out)
        manifest.append(
            {
                "resume_file": out.name,
                "candidate_name": person.name,
                "email": person.email,
                "phone": person.phone,
                "coverage": person.coverage,
                "skills_matched": str(len(person.skills)),
                "source_jd": jd.name,
                "synthetic": "true",
            }
        )
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--jds", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--skill-data", type=Path, required=True)
    args = ap.parse_args(argv)

    manifest = generate(args.jds, args.out, args.skill_data)
    tiers: dict[str, int] = {}
    for row in manifest:
        tiers[row["coverage"]] = tiers.get(row["coverage"], 0) + 1
    print(f"generated {len(manifest)} synthetic résumés -> {args.out}")
    spread = ", ".join(f"{k}={v}" for k, v in sorted(tiers.items()))
    print(f"  coverage spread: {spread}")
    print(f"  emails @{_EMAIL_DOMAIN}, phones {_PHONE_PREFIX}xx — reserved ranges")
    return 0


if __name__ == "__main__":
    sys.exit(main())
