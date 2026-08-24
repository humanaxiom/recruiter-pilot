"""Frequency ground-truth over the harmonized SFU JD qualification corpus.

Deterministic, no LLM: what skill vocabulary do real SFU postings actually use?
Compares against recruiter-assistant's current curated vocabulary to size the gap.
"""

from __future__ import annotations

import re
import sys
from collections import Counter

STOP = set(
    """a an the and or of in to for with on at by from as is are be been being this that
these those it its their his her our your my we you they i he she them us not no nor so
than then there here when where which who whom whose what why how all any both each few
more most other some such only own same too very can will just should now if but into
about against between during before after above below up down out off over under again
further once other equivalent combination related etc via per within across including
include includes included using use used ability able demonstrated strong excellent
proven solid effective good high level years year minimum least experience knowledge
skill skills abilities work working ha wa doe""".split()
)

PHRASE_STOP = STOP | {"and", "or", "of", "in", "to", "for", "with", "the", "a", "an"}


def clean(line: str) -> str:
    line = line.lower()
    # normalise the unicode punctuation the harmonizer emits
    line = line.replace("‑", "-").replace("‐", "-")
    line = line.replace("’", "'").replace("‘", "'")
    line = line.replace("“", '"').replace("”", '"')
    return line


TOKEN = re.compile(r"[a-z][a-z0-9+#./'-]*")


def tokens(line: str) -> list[str]:
    return [t.strip("'-./") for t in TOKEN.findall(clean(line))]


def ngrams(toks: list[str], n: int) -> list[str]:
    out = []
    for i in range(len(toks) - n + 1):
        gram = toks[i : i + n]
        if gram[0] in PHRASE_STOP or gram[-1] in PHRASE_STOP:
            continue
        if any(len(t) < 2 for t in gram):
            continue
        out.append(" ".join(gram))
    return out


def main(path: str) -> int:
    lines = [ln for ln in open(path, encoding="utf-8", errors="replace") if ln.strip()]
    uni: Counter[str] = Counter()
    bi: Counter[str] = Counter()
    tri: Counter[str] = Counter()

    for ln in lines:
        toks = tokens(ln)
        uni.update(t for t in toks if t not in STOP and len(t) > 2)
        bi.update(ngrams(toks, 2))
        tri.update(ngrams(toks, 3))

    print(f"# statements: {len(lines)}")
    print(f"# distinct unigrams: {len(uni)}  bigrams: {len(bi)}  trigrams: {len(tri)}\n")

    for name, ctr, floor, top in (
        ("UNIGRAMS", uni, 12, 120),
        ("BIGRAMS", bi, 8, 140),
        ("TRIGRAMS", tri, 5, 90),
    ):
        print(f"\n===== {name} (min count {floor}) =====")
        for term, n in ctr.most_common(top):
            if n < floor:
                break
            print(f"{n:5d}  {term}")

    # coverage against the shipped vocabulary
    try:
        import yaml  # type: ignore[import-untyped]

        base = "/vocab"
        with open(f"{base}/aliases.yaml", encoding="utf-8") as fh:
            aliases = yaml.safe_load(fh) or []
        with open(f"{base}/categories.yaml", encoding="utf-8") as fh:
            cats = yaml.safe_load(fh) or {}
        vocab: set[str] = set()
        for entry in aliases:
            vocab.add(str(entry["canonical"]).lower())
            for al in entry.get("aliases") or []:
                vocab.add(str(al).lower())
        for members in cats.values():
            for m in members or []:
                vocab.add(str(m).lower())

        print(f"\n\n===== VOCABULARY COVERAGE =====")
        print(f"shipped vocabulary strings: {len(vocab)}")
        for name, ctr, floor in (("unigram", uni, 12), ("bigram", bi, 8)):
            hits = [(t, n) for t, n in ctr.items() if n >= floor and t in vocab]
            miss = [(t, n) for t, n in ctr.items() if n >= floor and t not in vocab]
            tot = len(hits) + len(miss)
            pct = 100.0 * len(hits) / tot if tot else 0.0
            print(f"\n{name}s at/above floor: {tot}  in-vocab: {len(hits)} ({pct:.1f}%)")
            print(f"  top 40 OUT-OF-VOCAB {name}s by frequency:")
            for t, n in sorted(miss, key=lambda kv: -kv[1])[:40]:
                print(f"    {n:5d}  {t}")
    except Exception as exc:  # pragma: no cover - diagnostic path
        print(f"\n(vocab comparison skipped: {exc})")

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
