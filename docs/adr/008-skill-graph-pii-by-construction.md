# ADR-008: Skill-Graph Projection — PII Elimination By Construction

**Status:** Accepted (supersedes the heuristic PII filter built and re-audited five times inside
ADR-007's Phase 4b `skills_graph` module; extends ADR-004's 768-d/cosine embedding contract)
**Date:** 2026-07-14

## Context

Phase 4b's skill-graph projection (`src/pipeline/skills_graph.py`, `src/worker/tasks.py::
_job_projection_tx`, `src/worker/resume_tasks.py::_resume_projection_tx`) resolves every JD/résumé
skill name to a canonical Neo4j `Skill` node. A résumé's skill list comes from an LLM call
(`resume_skills_v2`) against untrusted document text; a small model, prompted to extract "skills"
from a header-shaped chunk, will sometimes emit the candidate's own name, email, or phone number as
a "skill". Left unhandled, that string is embedded (`nomic-embed-text`) and written to Neo4j
cleartext — a structural PII leak independent of, and downstream from, every redaction Phase 3
already does to `resumes.parsed`/the outbox.

Five rounds of a security re-audit tried to close this by **pattern-matching** the leak out of the
skill name before it reached the graph: a person-name shape detector (capitalisation, tokens,
technical-marker exceptions), then an offline personal-name lexicon, then a vendor/brand-prefix
veto, then a "strict mode" collapse for when no candidate context was available to redact against.
Every fix to one side of the trade-off reopened the other:

- Round 3 tightened the shape detector for privacy and broke recall on legitimate multi-word skills
  (`distributed systems`, `data engineering`) that are shaped exactly like a person's name.
- Round 4 added a personal-name lexicon to fix that recall regression, but shipped the lexicon check
  as `any(token in lexicon for token in tokens)` — one name-shaped token anywhere in a multi-word
  skill condemned the whole string, so real vendor/product names (`Amazon Aurora`, `IBM Watson`,
  `Victoria Metrics`) were dropped as false positives.
- Round 5 fixed the quantifier (`any` → `all`) and added a vendor-prefix veto to recover those
  fixtures — but the same `all()` quantifier let a two-real-name candidate through whenever it
  happened to also start with a vendor word (`IBM John Smith`), and the vendor veto and lexicon
  fail-closed logic combined to let `Sean Kvistad` / `Torbjorn Kvistad` / `Ludovica Brambilla`
  (single-lexicon-miss full names) and `Google Кейси Ривера` (script-mixed) through.

Five rounds, the same two failure classes trading places. **A skill name is untrusted free text; no
shape heuristic can distinguish "person's name" from "unlisted multi-word skill" with the recall and
precision this system needs simultaneously.**

## Decision

Eliminate the leak class **by construction** instead of detecting it, exploiting one structural
asymmetry: **a job description carries no candidate PII. Only the résumé side can leak an
identity into the graph.**

### 1. `Skill.canonical_key` is either a cleartext vocab term or a salted hash — never free text

The `Skill` node's unique key (`canonical_key`, replacing the old `canonical_name`) is computed by
one shared function (`_canonical_key_for_normalised`), called identically from both sides:

- **Vocab hit** (`aliases.yaml` / `categories.yaml`, a closed ~220-term set): the canonical term,
  cleartext. A vocab hit makes identity **deniable and unembedded, not absent**: the node is a
  shared global term with no `display_name` and no embedding written from the résumé side, so a
  `HAS_SKILL` edge to it is ambiguous — it could equally mean the candidate genuinely has that
  skill. This is NOT "cannot contain a person's name by definition" — the vocabulary we shipped
  demonstrably does (`julia`, `hudson`; see residual #13).
- **Everything else**: `"h:" + sha256(settings.skill_hash_salt + normalised)[:32]` — opaque,
  un-invertible, and (critically) computed from the exact same normalised string on both sides, so a
  JD requiring a skill and a résumé having the identical skill text still land on the same node.
  `REQUIRES`/`HAS_SKILL` still meet; no requirement silently vanishes.

> **Correction (F1, security re-audit round 2, 2026-07-14):** the paragraph above describes the
> INTENDED invariant, which the first cut of `skills_graph._resolve_one` did not actually hold. Three
> of its four branches (exact/graph-learned-alias match, vector auto-merge, LLM tiebreak) returned an
> EXISTING node's own key instead of calling `_canonical_key_for_normalised` — only the create-new
> branch did. Whenever the job side took one of those three branches, `REQUIRES` pointed at a
> different node than the résumé side's `HAS_SKILL` (which always calls the pure function), silently
> zeroing that skill's score. Worse, the auto-merge/LLM branches then PERSISTED the divergence via an
> alias write, so once (e.g.) "react native" auto-merged into the "react" node, every later JD
> mentioning it took the now-poisoned exact-match branch too — permanent, self-reinforcing drift. Fixed
> by making `_resolve_one` return `_canonical_key_for_normalised(normalised)` on **every** branch,
> unconditionally; vector auto-merge and the LLM tiebreak are kept but demoted to alias-list enrichment
> of the near-matched node only — they may add a synonym for humans/analytics to read, but they never
> again choose the key. The "Consequences" section below is corrected to match.

### 2. `display_name` (cleartext, human-readable) is written ONLY by the job/JD side

`src/worker/tasks.py::_job_projection_tx` stamps the Skill node's `display_name` with the raw JD
text, in a dedicated Cypher statement — always safe, since a job description carries no candidate
identity, even when the node's own key is an opaque hash. `src/worker/resume_tasks.py::
_resume_projection_tx` never sets this field.

### 3. The résumé side never embeds, never vector-searches, never writes cleartext

`src/pipeline/skills_graph.py::resume_skill_canonical_key` is the **entire** résumé-side skill
resolution — a pure function: no Neo4j session, no `embedder`, no `llm` parameter exists on it or on
anything that calls it (`project_resume`'s signature dropped both entirely). A résumé-derived
non-vocab skill name is therefore **never** handed to the embedder, so it can never surface as a
Neo4j vector-search near-candidate on a later job's resolution either (the old auto-merge/
LLM-tiebreaker mechanism — kept for the job side — only ever finds nodes that carry an `embedding`,
and a hash-keyed node from the résumé side never does).

The leak case from the audits — `"Casey Rivera"` extracted as a "skill" — now becomes a Skill node
keyed `h:<hash>`, with no `display_name`, no `embedding`, no `categories`, that no job ever requires:
unreadable, un-invertible, and it contributes nothing to any score.

### 4. `reject_reason_for_skill_name` is demoted to pure junk filtering

The email-shape / phone-shape / length-and-token-cap checks are **kept** (they cost nothing and stop
obvious garbage — a copy-pasted header block — from ever becoming a graph node), but the
person-name-shape detector, the offline personal-name lexicon (`skill_data/person_names.txt`,
deleted), the vendor-prefix veto, and the `strict_lexicon` parameter are **deleted outright**. A name
that IS the candidate's own identity now sails through this function unshaped — that is a deliberate,
documented behaviour change, not a regression: privacy no longer depends on this function at all.

### 5. The salt is a required setting, fails loud exactly like `PII_KEY`

`settings.skill_hash_salt` (`SKILL_HASH_SALT` env var) defaults to `""`. `src/worker/main.py::startup`
refuses to start when it is empty, mirroring the existing `PII_KEY` check — an unsalted hash of a
likely-candidate-name keyspace is dictionary-attackable (an attacker who can read the graph could
precompute `sha256(common_name)` for a list of common names/phrases and confirm one is present).
`_hash_key` fails loud on an empty salt too, as an independent second line of defence.

## Consequences

- **No skill — vocab or non-vocab — ever gets its `canonical_key` redirected by vector auto-merge or
  the LLM tiebreak (post-F1).** Both are job-side-only mechanisms that still run, but only to enrich
  the near-matched node's alias list (and, for the job side, its `display_name`) — never to choose
  what this function returns. A non-vocab skill therefore loses vector auto-merge / synonym resolution
  entirely (not just at its terminal "nothing matched, mint a new key" step, as originally documented
  here): two different spellings of the same unlisted skill hash to two different keys and never
  connect, unless one of them is a literal alias of a vocab term (resolved deterministically, before
  any graph query, by `_basic_normalise`'s alias table). This is an accepted cost, not a silent one —
  and it is strictly better than the pre-F1 alternative (letting the job side alone redirect the key),
  which reopened exactly the divergence this ADR's `REQUIRES`/`HAS_SKILL`-meet guarantee depends on.
- **The disparate-impact problem (the round-3 shape widening's own S1/S4/S5 fixes) disappears
  entirely** — there is no shape heuristic left to be biased against any naming convention, because
  there is no shape heuristic left, full stop.
- **Rotating `SKILL_HASH_SALT` changes every non-vocab skill's key** and requires re-projecting the
  whole graph (every job and résumé re-parsed) to reconnect `REQUIRES`/`HAS_SKILL` edges under the new
  keys. Documented on the setting itself.
- Graph debugging shows opaque `h:<hash>` keys for every non-vocab résumé-derived skill. That is
  intended, not a bug to "fix" by adding cleartext back.
- `resumes.parsed` (Postgres) still holds the skill name cleartext at rest (ADR-007 §6 already
  accepts this), and the outbox still carries it unencrypted (ADR-007 §7 / N1, unchanged) — this ADR
  is scoped to the **graph**, which is the artifact a recruiter-facing UI and the ranking/evidence
  pipeline actually read from and could leak through. `_redact_skill_names_pii` (parse-time,
  candidate-identity-aware structured scrub) is kept as defence in depth for that Postgres/outbox
  surface, but is no longer the control this ADR depends on.

## Security Sign-off — Accepted Residuals (verbatim)

Security reviewed the Phase 4b ADR-008 rearchitecture, mutation-killed all four `_resolve_one`
branches, verified the constraint migration against a real Neo4j with the old constraint
pre-created, and confirmed the corrected tests were strengthened, not weakened. The review passed.
What follows is the exhaustive residuals list from that sign-off — what this design knowingly
ships. It is recorded here plainly, not softened.

**Identity that can still reach Neo4j:**

1. A candidate's name mis-extracted as a skill still becomes a **salted-hash node key**
   (`h:eea6e36e…`), with no `display_name`, no embedding, no categories, plus a `HAS_SKILL` edge.
   Unreadable and un-invertible — but **its existence is observable**: anyone with graph read
   access **plus the salt** can confirm a guessed name is present by recomputing the hash. **The
   graph is not a zero-knowledge store.**
2. The hash is only as strong as the **salt's secrecy**. Salt disclosure retroactively makes every
   non-vocab key confirmable against a name list. Treat `SKILL_HASH_SALT` at exactly `PII_KEY`'s
   handling bar.
3. `h.evidence_chunk_id` on `HAS_SKILL` carries an opaque chunk id (`c_001`) — not text, but a
   **pointer back to a résumé region**.
4. **JD-side cleartext is unconditional and intentional.** Every `display_name`, skill embedding and
   alias string in the graph comes from a job description. Safe **only** while the JD-authoring
   surface is trusted — **if a recruiter pastes résumé text into a JD field, it lands in the graph
   as cleartext and nothing in this design stops it.**

**Legitimate skills that can still be dropped or fail to match:**

5. Shape-rejected skills are **dropped entirely** — no node, no edge, silently: >60 chars, >8
   tokens, or email/phone-shaped. A genuine 9-token certification is lost. This is junk filtering,
   not privacy, and it is lossy.
6. **A non-vocab skill loses synonym auto-merge permanently.** `py3` will **never** match `python`;
   `React Native` never matches `react`. Two spellings of one unlisted skill are two unrelated nodes
   that never score against each other. **This is the single biggest recall cost of ADR-008.**
   Non-vocab recall now requires the JD and the résumé to use the **byte-identical normalised
   spelling**.
   > **Correction (Phase 4b, ranking-evals, 2026-07-14):** this residual is understated as written —
   > it does not only cost recall on "a non-vocab skill." A **vocab** skill spelled a way
   > `_basic_normalise` doesn't (yet) fold into its canonical also loses the match entirely:
   > `PostgreSQL 14` **is** the vocab skill `postgresql`, just spelled with a trailing version
   > number `_basic_normalise` didn't strip pre-fix. Measured cost: one such variant alone cost a
   > strong candidate **−0.144 on `score_final`** in the 4a corpus — more than `education` (0.0391)
   > + `overqual` (0.0120) + `motivation` (0.0900) *combined* — enough to drop a qualified candidate
   > out of the k=5 shortlist, and it rewarded the adversarial fixture's exact thesis (copying the
   > JD's literal string is the only behaviour guaranteed to match). Partially addressed by the
   > trailing-version-token / parenthetical normalisation and the small, tight alias set landed
   > alongside this correction (see residual #8's updated counts) — not eliminated: a vocab skill
   > spelled in a way neither normalisation nor the alias table recognises still misses, same as any
   > non-vocab spelling.
7. Vector auto-merge and the LLM tiebreaker **no longer affect scoring at all** — they are
   alias-list enrichment only. `react.aliases` containing `"react native"` is **advisory
   metadata**; anyone reading `s.aliases` and inferring "this resolves to react" will be wrong.
8. **Recall is only as good as the ~220-term vocabulary.** `aliases.yaml`/`categories.yaml` are now
   the **entire** matching surface for reliable cross-spelling recall. **Growing that vocabulary is
   the only lever that improves non-vocab recall.**
   > **Correction (Phase 4b, ranking-evals, 2026-07-14):** "~220 terms" is the **spelling** count
   > (every alias string across both files), not the **concept** count — a reader takes "220 terms"
   > to mean 220 distinct skills, which overstates true breadth roughly 1.5×. Both numbers, stated
   > together: **before** this correction, `aliases.yaml` held 71 canonical concepts (146 total
   > spellings across its aliases) plus 75 concepts that exist only in `categories.yaml` (one bare
   > spelling each, no alias list) — **146 concepts, ~221 spellings**, ~1.5 spellings/concept.
   > **After** this correction's tight, judgement-call additions (`psql`; `docker compose`;
   > `kafka streams`; a new `rest api design` concept covering `rest api`/`rest apis`/
   > `restful api`/`restful apis`) plus the trailing-version-token and parenthetical normalisation
   > rules (which add zero vocabulary): **147 concepts, ~229 spellings.** Deliberately not the full
   > 8-15-alias-per-concept expansion — see the Phase 4b spelling-recall fix commit for what was
   > added and why each addition was justified.
9. A Skill node **first created by the résumé side never gets an embedding** — the résumé MERGE is
   bare, and a later JD requiring it hits the exact-match fast path and returns before the embedding
   write. Such a node is absent from `skill_emb_idx` and can never serve as a vector-merge target.

**Salt rotation:**

10. **Rotating `SKILL_HASH_SALT` silently orphans the entire non-vocab half of the graph.** Same
    name + new salt = a different key. Existing `REQUIRES`/`HAS_SKILL` edges keep pointing at the
    old key while new projections write the new one. **Nothing detects this; scores just quietly
    degrade. Rotation requires a full re-projection of every job and résumé.** There is no tooling
    and no guard — say so.

**Operational:**

11. An empty salt is fail-loud in three places, but a **weak** salt is accepted silently — no
    entropy check.
12. Categories are curated-only: a hashed non-vocab skill contributes **nothing** to stage-2 ontology
    partial credit, by construction — and contributes nothing for a slightly different reason than this
    residual originally said. `categories_for()` returns `[]` for it, but `ensure_categories`'s `if cats:`
    guard means **no Cypher runs at all** when the list is empty — the node's `categories` property is
    never *set* to `[]`, it stays absent entirely (corrected 2026-08-14, ROADMAP A2/ADR-042). The
    consequence for stage 2 is identical either way (`reqSkill.categories IS NOT NULL` is false for an
    absent property exactly as it would be for `[]`), so nothing scoring-relevant was wrong before this
    correction — only the property's stored state was misdescribed.
13. A candidate whose name collides with a vocabulary term (`julia`, `hudson`, `kafka`, `django`,
    `cassandra`) gets a **cleartext** `canonical_key`. No `display_name` and no embedding are
    written from the résumé side, so the disclosure is ambiguous (the node is shared with everyone
    who genuinely has that skill) — but it is cleartext, and it is not eliminated by construction.
    Accepted: removing these terms from the vocabulary would cost real recall for real
    technologies.
    > **Widened (post-4b security re-audit).** As written above this residual describes only the
    > *exact bare name* case (a candidate literally named "Julia" or "Hudson"). The Phase 4b
    > spelling-recall normalisation (trailing-version-strip, parenthetical-split) routes a WIDER
    > input set to the same collision node: `"Julia 3"`, `"Hudson v2"` (version-strip), and
    > `"X (Julia)"` / `"X (Hudson)"` for any outer phrase `X` that is itself vocab-adjacent, e.g.
    > `"Data Pipeline (Julia)"` (paren-split — see residual #14 for the gate that now bounds which
    > outer phrases can reach this at all). Same deniable/unembedded property as the bare-name case
    > — nothing here lands *new* identifying value in the graph — but the set of spellings that
    > collide with the shared node is wider than "exact bare name" suggested, and the node itself is
    > correspondingly more ambiguous about who it actually describes.
14. **Parenthetical-split skill inflation — closed.** The parenthetical-CONTENT fallback (residual
    #6/#8's normalisation fix) could, unguarded, extract a real vocab skill out of a name-bearing
    string — `"Casey Rivera (Python)"` → `python`, `"Rivera (psql)"` → `postgresql`, `"Casey (Kafka
    Streams)"` → `kafka`. The `canonical_key` produced carries no name either way (not a new PII
    residual — the hash/cleartext-vocab split already covers it), but a mis-extracted name silently
    minting a spurious `HAS_SKILL` edge is a **scoring-integrity** hazard the 4a evals corpus cannot
    see. Closed by `_outer_phrase_is_vocab_adjacent` (`src/pipeline/skills.py`): the parenthetical's
    own content is only used as a fallback when the outer phrase (parens stripped) is empty or
    itself shares a vocab-table token (`"aws"` inside `"AWS MWAA"`) — a name-bearing outer phrase
    (`"Casey Rivera"`, `"Rivera"`, `"Ada Lovelace"`, `"Casey"`) shares none, so the fallback never
    runs and the string falls through to the unresolved (eventually hashed) full-cleaned form
    instead. `"Containerization (Docker)"` is preserved without needing the gate at all by
    registering `containerization` itself as a `docker` alias, so it resolves at the earlier,
    unconditional outer-full-resolve step. Honest caveat: this gate is a **token-overlap** check, not
    a semantic one — an adversarial outer phrase that happens to embed a genuine vocab word
    incidentally (e.g. a made-up qualifier phrase containing "aws" as a substring token) could still
    unlock its parenthetical; the gate closes the concrete inflation cases identified, not every
    theoretically constructible one.

## Alternatives Considered

- **A stricter allowlist** (only the ~220-term vocabulary is ever projected) — rejected in the
  original round-2 audit and again here: it silently drops every legitimate skill outside the
  vocabulary, a large, invisible recall loss that the 4a evals corpus (whose own recorded residual is
  `weights.skill = 0.0`) would not have caught.
- **A sixth heuristic round** (yet another shape/lexicon refinement) — rejected per the human
  direction that started this ADR: "you cannot reliably pattern-match PII out of an untrusted
  free-text field. Stop trying." Five rounds of evidence support that conclusion directly.
