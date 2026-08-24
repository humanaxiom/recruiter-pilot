# Deriving the skill vocabulary from real SFU job descriptions

**Date:** 2026-08-07 · **Feeds:** ROADMAP A2 / pilot-readiness Phase 3.2 and 3.3

## Why this exists

ROADMAP A2 recorded that real SFU postings were "47–84% outside the curated vocabulary" and scored
0.0000–0.0375 on the skill sub-score. That framed the problem as *the vocabulary is too small*. Measuring
it against a real corpus shows the framing was wrong: **the ontology is for the wrong domain.**

The shipped vocabulary is a software-engineering ontology — `javascript`, `react`, `postgresql`, `docker`,
`kafka`. SFU's actual postings are administrative, academic and professional-services work: financial
administration, academic advising, collective agreements, curriculum development, Truth and Reconciliation
Commission Calls to Action, campus space planning, athletic injury assessment.

## The corpus

The sibling **jd-assistant** project (`github.com/humanaxiom/jd-assistant`) runs locally as `jd-bank-*`
containers and holds a harmonized archive of SFU job descriptions.

| | |
|---|---|
| Source | `jd-bank-postgres-1`, database `harness`, table `canonical_jds` |
| Canonical JDs | 1,802 |
| Qualification statements | 9,176 (`experience` 2,885 · `education` 2,193 · `knowledge` 1,874 · `skill` 1,290 · `ability` 904 · `security` 30) |
| Distinct titles | 1,222 |
| Distinct departments | 449 |

Statements are prose, not skill tokens — e.g. *"Knowledge of campus space planning principles, scheduling
software and university policies/regulations"*. That shape matters: the JD parser must extract skill terms
from sentences, not read a list.

## Method

Deterministic and reproducible — **no LLM**, so the result is auditable and re-runnable.

1. `ngrams.py` — unigram/bigram/trigram frequency over the `knowledge`/`skill`/`ability`/`experience`
   statements, with a stop list covering the harmonizer's boilerplate. Establishes what vocabulary the
   corpus *actually* uses.
2. `derived-families.yaml` — families and members selected by corpus frequency, grouped by domain. Every
   member appears in the corpus at or above the floor; comments carry the counts.
3. `coverage.py` — statement-level coverage, shipped vocabulary vs shipped + derived. A statement counts as
   covered if any vocabulary term occurs in it as a whole word — the closest honest proxy for "stage 2
   could match anything here at all". The 977 occurrences of the boilerplate *"or an equivalent combination
   of education, training and experience"* are excluded; they are not skill statements.

To reproduce (there is no usable Python on the host, so run it in a container):

```bash
docker exec jd-bank-postgres-1 psql -U app -d harness -t -A -c \
  "SELECT replace(replace(q->>'text', E'\t',' '), E'\n',' ')
   FROM canonical_jds c, jsonb_array_elements(c.content->'qualifications') q
   WHERE q->>'kind' IN ('skill','knowledge','ability','experience');" > quals.txt

docker run --rm -v "$PWD:/w" \
  -v "c:/repos/recruiter-assistant/core/src/pipeline/skill_data:/vocab:ro" \
  -w /w python:3.11-slim sh -c "pip install -q pyyaml; python coverage.py"
```

## Result

| Measure | Value |
|---|---|
| Shipped vocabulary | 231 terms |
| Statements it can match at all | **15.6%** (933 / 5,976) |
| Frequent unigrams in vocabulary | 0.9% |
| Frequent bigrams in vocabulary | 1.3% |
| Derived additions | 13 families, 234 terms |
| Statements matched with them | **54.8%** (3,275 / 5,976) |
| **Gain** | **+39.2 percentage points** |

Reach per derived family (statements touched): leadership_management 634 · finance 586 · communications
550 · student_affairs 442 · governance_policy 404 · human_resources 320 · analysis_reporting 269 ·
academic_programs 249 · interpersonal_core 170 · facilities_operations 67 · health_wellness 64 ·
research_admin 41 · equity_indigenous 34.

## What the remaining 45.2% tells us

The tail is genuinely long and role-specific, not a curation backlog:

- *"expert scientific knowledge of MRI and MEG research methods"*
- *"minimum three years of experience in microfabrication techniques"*
- *"eligibility to practice law in British Columbia"*
- *"knowledge of Canadian post-secondary admission policies and study permit requirements"*

**Curation plateaus around 55%.** Closing the rest needs the Phase 3.3 projection-time family classifier —
which is why that slice is load-bearing rather than optional. Note the hash is one-way, so classification
must run at projection time while the cleartext skill name is still in hand; it cannot be backfilled over
the graph later.

## The open decision this surfaces

Many derived terms are **competencies**, not tools: communication, leadership, problem-solving,
adaptability, attention to detail. The skill scorer is `years × recency × ontology_weight`
(`stages.py:107-167`). *"Three years of interpersonal skills, last used 2024"* is not a meaningful
statement, and the recency banding would penalise a career break on *communication* exactly as it does on
*Kubernetes*.

Options, unresolved and gating Phase 3.1/3.2:

1. Score competencies on a different model — proficiency, presence/absence, or recency-exempt.
2. Keep one model but exempt competencies from the must-have penalty.
3. Decide per family as each is curated.

This is a product decision with adverse-impact implications (register items 3, 8, 9 already flag the
years-and-recency axis), so it should be made deliberately rather than inherited from the tool model.

## Files here

| File | Purpose |
|---|---|
| `ngrams.py` | Frequency ground truth over the corpus |
| `derived-families.yaml` | The 13 derived families, with corpus counts as comments |
| `coverage.py` | The before/after measurement |

`derived-families.yaml` is a **proposal**, not shipped configuration. Phase 3.2 merges it into
`core/src/pipeline/skill_data/categories.yaml` and `aliases.yaml`, which is `ranking-evals` gated — and per
ROADMAP A3 the gate cannot currently see out-of-vocabulary skills at all, so Phase 2.4 must land first or
the merge is unmeasurable.
