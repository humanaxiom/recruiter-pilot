"""Measure statement-level coverage of the SFU JD corpus: shipped vocabulary vs derived.

A statement is COVERED if any vocabulary term occurs in it as a whole-word substring.
That is the closest honest proxy for "stage 2 could match something here at all".
"""

from __future__ import annotations

import re

import yaml


def load_shipped() -> set[str]:
    vocab: set[str] = set()
    with open("/vocab/aliases.yaml", encoding="utf-8") as fh:
        for entry in yaml.safe_load(fh) or []:
            vocab.add(str(entry["canonical"]).lower())
            for al in entry.get("aliases") or []:
                vocab.add(str(al).lower())
    with open("/vocab/categories.yaml", encoding="utf-8") as fh:
        for members in (yaml.safe_load(fh) or {}).values():
            for m in members or []:
                vocab.add(str(m).lower())
    return vocab


def load_derived() -> dict[str, list[str]]:
    with open("/w/derived_families.yaml", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def norm(s: str) -> str:
    s = s.lower().replace("‑", "-").replace("‐", "-")
    s = s.replace("’", "'")
    return re.sub(r"\s+", " ", s)


def build_matcher(terms: set[str]) -> re.Pattern[str]:
    ordered = sorted((t for t in terms if len(t) > 2), key=len, reverse=True)
    return re.compile(r"(?<!\w)(" + "|".join(re.escape(t) for t in ordered) + r")(?!\w)")


def main() -> int:
    lines = [
        norm(ln)
        for ln in open("/w/quals.txt", encoding="utf-8", errors="replace")
        if ln.strip()
    ]
    # the harmonizer's boilerplate equivalency clause is not a skill statement
    lines = [ln for ln in lines if "equivalent combination of education" not in ln]

    shipped = load_shipped()
    derived_map = load_derived()
    derived: set[str] = {m.lower() for members in derived_map.values() for m in members}
    combined = shipped | derived

    m_ship = build_matcher(shipped)
    m_comb = build_matcher(combined)

    n = len(lines)
    hit_ship = sum(1 for ln in lines if m_ship.search(ln))
    hit_comb = sum(1 for ln in lines if m_comb.search(ln))

    print(f"qualification statements analysed : {n}")
    print(f"shipped vocabulary terms          : {len(shipped)}")
    print(f"derived terms added               : {len(derived)}  "
          f"across {len(derived_map)} new families")
    print(f"combined vocabulary terms         : {len(combined)}\n")
    print(f"COVERED by shipped vocabulary  : {hit_ship:5d} / {n}  "
          f"({100.0*hit_ship/n:5.1f}%)")
    print(f"COVERED by shipped + derived   : {hit_comb:5d} / {n}  "
          f"({100.0*hit_comb/n:5.1f}%)")
    print(f"absolute gain                  : "
          f"{100.0*(hit_comb-hit_ship)/n:5.1f} percentage points\n")

    per: list[tuple[int, str]] = []
    for fam, members in derived_map.items():
        mm = build_matcher({m.lower() for m in members})
        per.append((sum(1 for ln in lines if mm.search(ln)), fam))
    print("statements touched, by derived family:")
    for cnt, fam in sorted(per, reverse=True):
        print(f"  {cnt:5d}  {fam}")

    still = [ln for ln in lines if not m_comb.search(ln)]
    print(f"\nSTILL UNCOVERED: {len(still)} ({100.0*len(still)/n:.1f}%). Sample of 15:")
    for ln in still[:: max(1, len(still) // 15)][:15]:
        print(f"  - {ln[:110]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
