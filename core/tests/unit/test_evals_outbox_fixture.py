"""Corpus-integrity guard for the Phase-4b outbox-shaped evals fixture.

Carried-forward requirement from the 4a hardening audit
(``docs/EXTRACTION_PLAN.md``, 4b row): nothing in the repo encoded what the
``outbox`` payload is *allowed* to contain before this file existed — no
fixture asserted "no ``candidate`` block, no ``chunks[].text``, no
``summary``" — so 4b could project a payload carrying candidate identity
straight into Neo4j with no fixture catching it. This module is that
fixture's integrity guard, in the same spirit as
``tests/unit/test_evals_corpus.py`` for the 4a résumé corpus:

* the fixture's own SHAPE is asserted structurally (top-level key set, no
  ``candidate``/``summary``, chunk dicts carry no ``text`` key, embeddings
  are 768-d) — this needs no 4b code and is expected to PASS today, exactly
  like ``test_evals_corpus.py`` needs only the already-merged Phase 2/3
  schemas (see that file's closing docstring note);
* the fixture cannot SILENTLY DRIFT from the actual Phase-3 producer: the
  ``resume_parsed.json`` fixture's ``parsed`` key set is compared, by
  IDENTITY, against ``ResumeParsed.model_dump(exclude=_OUTBOX_PARSED_EXCLUDE)``
  — ``_OUTBOX_PARSED_EXCLUDE`` is a NEW named constant this file requires
  ``src.worker.resume_tasks`` to export (today the exclude dict is an inline
  literal at the ``outbox_service.enqueue_outbox`` call site) so this test
  and the producer share ONE source of truth instead of the test
  re-implementing the exclude clause by hand and silently drifting from it
  the way the 4a corpus's hand-enumerated claims did for nine audit rounds.
  Until that constant exists, this whole file fails to import — RED;
* the new ``fixtures/outbox/`` directory is actually covered by the 4a
  corpus's PII scanners, which glob ``FIXTURES_DIR.rglob("*.json")`` (finding
  B5) rather than an enumerated filename list — proven two ways: (a) the glob
  itself reaches the new subdirectory, and (b) the scanning FUNCTIONS
  (imported from ``test_evals_corpus``) actually flag a planted leak inside
  an outbox-shaped fixture placed under a nested ``outbox/`` directory.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from src.schemas.jobs import JDExtracted
from src.schemas.resumes import ResumeChunk, ResumeParsed, ResumeSkill

EVALS_DIR = Path(__file__).resolve().parent.parent / "evals"
FIXTURES_DIR = EVALS_DIR / "fixtures"
OUTBOX_DIR = FIXTURES_DIR / "outbox"
RESUME_FIXTURE_PATH = OUTBOX_DIR / "resume_parsed.json"
JOB_FIXTURE_PATH = OUTBOX_DIR / "job_parsed.json"

# ADR-004: 768-d nomic-embed-text, cosine — the same contract
# ``src/worker/neo4j_bootstrap.py`` builds the vector indexes from.
EXPECTED_EMBEDDING_DIM = 768

RESUME_TOP_LEVEL_KEYS = {
    "parsed",
    "summary_emb",
    "chunk_embs",
    "prompt_version",
    "job_id",
}
JOB_TOP_LEVEL_KEYS = {"embedding", "extracted", "prompt_version"}

# N1 (ADR-007 §7): these fields are EXPECTED to ride the outbox unscrubbed —
# they are structured, non-contact fields the graph projection needs. A
# future silent scrub of any of these is a deliberate change that must be
# caught here, not discovered as a regression in 4c's scoring.
EXPECTED_PARSED_KEYS = {
    "total_years_experience",
    "skills",
    "experience",
    "education",
    "chunks",
    "cover_letter_chunks",
}


def _load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        result: dict[str, Any] = json.load(fh)
    return result


# ── Directory / files exist ──────────────────────────────────────────────


def test_outbox_fixtures_dir_exists() -> None:
    assert OUTBOX_DIR.is_dir()
    assert OUTBOX_DIR.parent == FIXTURES_DIR


def test_resume_and_job_outbox_fixtures_exist() -> None:
    assert RESUME_FIXTURE_PATH.is_file()
    assert JOB_FIXTURE_PATH.is_file()


def test_outbox_fixtures_parse_as_json() -> None:
    _load_json(RESUME_FIXTURE_PATH)
    _load_json(JOB_FIXTURE_PATH)


# ── resume_parsed.json — top-level shape ─────────────────────────────────


def test_resume_fixture_top_level_key_set_is_exactly_the_outbox_contract() -> None:
    """ADR-007 §7's ``resume.parsed`` event shape, pinned exactly. Adding or
    dropping a top-level key here without updating this test is drift."""
    payload = _load_json(RESUME_FIXTURE_PATH)
    assert set(payload.keys()) == RESUME_TOP_LEVEL_KEYS


def test_resume_fixture_parsed_has_no_candidate_block() -> None:
    """Decision 1 / ADR-007 F2: the outbox NEVER carries identity."""
    payload = _load_json(RESUME_FIXTURE_PATH)
    assert "candidate" not in payload["parsed"]


def test_resume_fixture_parsed_has_no_summary_field() -> None:
    """ADR-007 F2 (round 3): a small model sometimes opens ``summary`` with
    the candidate's own name — dropped from the outbox entirely."""
    payload = _load_json(RESUME_FIXTURE_PATH)
    assert "summary" not in payload["parsed"]


def test_resume_fixture_chunks_carry_no_text_key() -> None:
    """R1/R2: header chunks carry the candidate's name/email/phone verbatim
    in ``text`` — the outbox must never carry it, at all, ever."""
    payload = _load_json(RESUME_FIXTURE_PATH)
    chunks = payload["parsed"]["chunks"]
    assert chunks, "fixture must carry at least one chunk to be a meaningful guard"
    for chunk in chunks:
        assert "text" not in chunk, f"chunk {chunk.get('id')} carries a 'text' key"
        assert set(chunk.keys()) <= {"id", "section", "page"}


def test_resume_fixture_cover_letter_chunks_carry_no_text_key_if_present() -> None:
    payload = _load_json(RESUME_FIXTURE_PATH)
    for chunk in payload["parsed"].get("cover_letter_chunks", []):
        assert "text" not in chunk


def test_resume_fixture_parsed_retains_the_n1_allowed_structured_fields() -> None:
    """N1 (ADR-007 §7) is EXPECTED, not a bug: structured, non-contact fields
    ride the outbox unscrubbed because Phase 4's graph projection needs them.
    A future PR that silently drops one of these must go RED here, not be
    discovered as a missing HAS_SKILL/REQUIRES edge three phases later."""
    payload = _load_json(RESUME_FIXTURE_PATH)
    assert set(payload["parsed"].keys()) == EXPECTED_PARSED_KEYS


def test_resume_fixture_chunk_ids_match_chunk_embs_keys_exactly() -> None:
    payload = _load_json(RESUME_FIXTURE_PATH)
    chunk_ids = {c["id"] for c in payload["parsed"]["chunks"]}
    assert chunk_ids == set(payload["chunk_embs"].keys())
    assert chunk_ids  # non-trivial


@pytest.mark.parametrize("which", ["summary_emb", "chunk_embs"])
def test_resume_fixture_embeddings_are_768_dimensional(which: str) -> None:
    payload = _load_json(RESUME_FIXTURE_PATH)
    if which == "summary_emb":
        vectors = [payload["summary_emb"]]
    else:
        vectors = list(payload["chunk_embs"].values())
    assert vectors
    for vec in vectors:
        assert len(vec) == EXPECTED_EMBEDDING_DIM


def test_resume_fixture_job_id_is_a_uuid_string() -> None:
    import uuid

    payload = _load_json(RESUME_FIXTURE_PATH)
    uuid.UUID(payload["job_id"])  # raises ValueError if malformed


def test_resume_fixture_parsed_validates_with_placeholder_chunk_text() -> None:
    """The outbox-shaped ``parsed`` dict is a SUBSET of ``ResumeParsed`` —
    every field OTHER than chunk text must already be schema-shaped.
    ``ResumeChunk.text`` is mandatory (``min_length=1``) and is exactly the
    field the outbox never carries (decision 1), so a placeholder is
    substituted for validation purposes only — this test would be
    unsatisfiable by construction otherwise, which is not the same claim as
    "the fixture is well-formed"."""
    payload = _load_json(RESUME_FIXTURE_PATH)
    parsed = dict(payload["parsed"])
    parsed["chunks"] = [{**c, "text": "placeholder"} for c in parsed["chunks"]]
    ResumeParsed.model_validate(parsed)


def test_resume_fixture_every_chunk_id_is_a_valid_resumechunk_id_shape() -> None:
    payload = _load_json(RESUME_FIXTURE_PATH)
    for chunk in payload["parsed"]["chunks"]:
        # ResumeChunk.text is required — synthesize a placeholder to validate
        # everything else (id/section/page shape) without re-adding text.
        ResumeChunk.model_validate({**chunk, "text": "placeholder"})


# ── Decision B (security re-audit round 2): skill-recall guard ───────────
#
# The 4a corpus is blind to skill-score degradation (its own recorded R1
# residual — `weights.skill = 0.0` passes it). A PII filter that silently
# eats a legitimate skill would be just as invisible there, so this guard
# rides the SAME outbox fixture the corpus-integrity tests above already
# pin (no new résumé twins) and asserts every skill name it declares
# survives Decision A's shape+vocab reject all the way through
# `resolve_canonical_names` — covering the four cases Decision B names
# explicitly: a vocab-known skill, a multi-token proper-noun skill, a
# 7-token certification, and a C++-style technical marker.
#
# NOTE on what this specific guard does/doesn't prove: this fixture's
# `skills[]` names are already-CANONICALISED (lowercase) — the shape a real
# outbox payload actually carries at this stage of the pipeline (downstream
# of `_extract_skills_merged`'s own `canonicalize_skill_names` call). The
# person-name-shape test requires Title Case, so it never fires on an
# already-lowercase name; what THIS layer actually pins is the widened
# token cap (the 7-token certification) and that a technical-marker/
# multi-token vocab alias survives `resolve_canonical_names` unchanged. The
# person-name-shape-vs-vocab INTERACTION itself (Decision A's core
# trade-off — "Kafka"/"Django"/"Kubernetes"/"Google Cloud Platform" kept,
# "Rivera" rejected) is proven upstream, against the RAW (Title-Case) LLM
# name `_extract_skills_merged` actually sees, in
# `tests/unit/test_worker_parse_resume.py`
# (`test_extract_skills_merged_keeps_vocab_known_skill_despite_person_shape`
# and its neighbours) — verified to go RED under a vocab-check mutation
# there, not here.


def test_outbox_fixture_declares_all_four_decision_b_recall_cases() -> None:
    """Guards the guard: if this fixture ever lost one of the four
    categories Decision B names, the tests below would pass VACUOUSLY."""
    names = {s["name"] for s in _load_json(RESUME_FIXTURE_PATH)["parsed"]["skills"]}
    assert "python" in names, "expected a vocab-known single-word skill"
    assert "apache airflow" in names, "expected a multi-token proper-noun skill"
    assert any(
        len(n.split()) == 7 for n in names
    ), "expected a 7-token certification name"
    assert "c++" in names, "expected a C++/.NET-style technical-marker skill"


def test_every_fixture_declared_skill_name_survives_the_shape_vocab_reject() -> None:
    """Decision A's `reject_reason_for_skill_name` must return ``None`` (not
    rejected) for every skill this fixture declares — a mutation that
    over-widens the shape/vocab reject (e.g. dropping the vocab check, or
    over-tightening the token cap back down) goes RED here."""
    from src.pipeline import skills_graph

    names = [s["name"] for s in _load_json(RESUME_FIXTURE_PATH)["parsed"]["skills"]]
    assert names, "fixture must declare at least one skill to be a meaningful guard"
    for name in names:
        reason = skills_graph.reject_reason_for_skill_name(name)
        assert reason is None, (
            f"legitimate fixture-declared skill {name!r} was rejected "
            f"(reason={reason!r}) — Decision B's recall guard failed"
        )


@pytest.mark.asyncio
async def test_every_fixture_declared_skill_name_resolves_to_a_non_none_canonical() -> (
    None
):
    """End to end through the REAL ``resolve_canonical_names`` (a fake Neo4j
    session standing in for the driver — no testcontainers needed here):
    proves the recall guard holds through the WHOLE resolution path, not
    just the shape+vocab function in isolation. A filter regression that
    resolves any of these to ``None`` (the "drop this skill/edge" signal)
    goes RED here, not three phases later in 4c's scoring."""
    from unittest.mock import AsyncMock, MagicMock

    from src.pipeline import skills_graph

    names = [s["name"] for s in _load_json(RESUME_FIXTURE_PATH)["parsed"]["skills"]]

    class _EmptyResult:
        async def single(self) -> dict[str, Any] | None:
            return None

        def __aiter__(self) -> _EmptyResult:
            return self

        async def __anext__(self) -> dict[str, Any]:
            raise StopAsyncIteration

    session = MagicMock(run=AsyncMock(return_value=_EmptyResult()))
    llm = MagicMock(chat_json=AsyncMock())
    embedder = MagicMock(embed=AsyncMock(return_value=[[0.1] * EXPECTED_EMBEDDING_DIM]))

    resolved = await skills_graph.resolve_canonical_names(
        session, names, llm=llm, embedder=embedder
    )
    for name in names:
        assert resolved[name] is not None, (
            f"legitimate fixture-declared skill {name!r} resolved to None "
            f"(dropped) — Decision B's recall guard failed"
        )


# ── job_parsed.json — top-level shape ────────────────────────────────────


def test_job_fixture_top_level_key_set_is_exactly_the_outbox_contract() -> None:
    payload = _load_json(JOB_FIXTURE_PATH)
    assert set(payload.keys()) == JOB_TOP_LEVEL_KEYS


def test_job_fixture_embedding_is_768_dimensional() -> None:
    payload = _load_json(JOB_FIXTURE_PATH)
    assert len(payload["embedding"]) == EXPECTED_EMBEDDING_DIM


def test_job_fixture_extracted_validates_against_jdextracted() -> None:
    payload = _load_json(JOB_FIXTURE_PATH)
    jd = JDExtracted.model_validate(payload["extracted"])
    assert jd.title
    assert jd.required_skills


def test_job_fixture_extracted_key_set_matches_the_real_producer() -> None:
    """``parse_job`` (``src/worker/tasks.py``) enqueues
    ``extracted.model_dump()`` with NO exclude clause — unlike the résumé
    side, a JD carries no candidate identity to strip. The fixture's
    ``extracted`` key set must equal what ``JDExtracted.model_dump()``
    actually produces, so a future field addition to the schema is reflected
    here rather than silently drifting."""
    payload = _load_json(JOB_FIXTURE_PATH)
    jd = JDExtracted.model_validate(payload["extracted"])
    assert set(payload["extracted"].keys()) == set(jd.model_dump().keys())


# ── The fixture cannot drift from the Phase-3 PRODUCER ───────────────────


def test_outbox_parsed_exclude_constant_matches_the_fixtures_dropped_keys() -> None:
    """Derives the payload via the ACTUAL producer's own
    ``model_dump(exclude=...)`` path, per the deliverable's own instruction:
    "do not hand-enumerate". ``_OUTBOX_PARSED_EXCLUDE`` is a NEW named
    constant ``src.worker.resume_tasks`` must export (today it is an inline
    literal at the ``enqueue_outbox`` call site) so this fixture and the
    producer share ONE source of truth — hand-duplicating the exclude clause
    here would be exactly the drift-prone shape the 4a corpus's nine audit
    rounds kept finding. Until the constant exists, this whole module fails
    to import (RED)."""
    from src.worker.resume_tasks import _OUTBOX_PARSED_EXCLUDE

    full = ResumeParsed.model_validate(
        {
            "candidate": {"name": "Someone Fake", "email": "someone@example.test"},
            "summary": "A summary that must never reach the outbox.",
            "total_years_experience": 3,
            "skills": [{"name": "python"}],
            "experience": [],
            "education": [],
            "chunks": [{"id": "c_001", "section": "summary", "page": 0, "text": "hi"}],
            "cover_letter_chunks": [],
        }
    )
    produced = full.model_dump(exclude=_OUTBOX_PARSED_EXCLUDE)

    assert "candidate" not in produced
    assert "summary" not in produced
    for chunk in produced["chunks"]:
        assert "text" not in chunk

    fixture = _load_json(RESUME_FIXTURE_PATH)
    assert set(produced.keys()) == set(fixture["parsed"].keys()), (
        "the outbox fixture's 'parsed' key set has drifted from what "
        "ResumeParsed.model_dump(exclude=_OUTBOX_PARSED_EXCLUDE) actually "
        "produces — update the fixture (or the exclude constant) so the two "
        "agree; the fixture must describe reality, not a hand-written claim "
        "about it"
    )


def test_outbox_parsed_exclude_constant_excludes_only_text_from_chunk_lists() -> None:
    """Pins the SHAPE of the exclude clause itself (not just its effect): it
    must be a per-item ``{"__all__": {"text"}}`` exclusion on both chunk
    lists, not (for example) a blanket ``"chunks": True`` that would also
    drop ``id``/``section``/``page`` — which Phase 4 needs to key
    embeddings by chunk id."""
    from src.worker.resume_tasks import _OUTBOX_PARSED_EXCLUDE

    assert _OUTBOX_PARSED_EXCLUDE.get("candidate") is True
    assert _OUTBOX_PARSED_EXCLUDE.get("summary") is True
    assert _OUTBOX_PARSED_EXCLUDE.get("chunks") == {"__all__": {"text"}}
    assert _OUTBOX_PARSED_EXCLUDE.get("cover_letter_chunks") == {"__all__": {"text"}}


# ── ROADMAP A2, Phase 3.3 skill-family classifier: the fixture's new
# per-skill `categories` field must be REAL, schema-backed data ──────────
#
# UPDATED guard (not a new independent claim): the payload GAINED a field
# (`ResumeSkill.categories`, nested under `parsed.skills[]`) -- this repo's
# fixture is hand-curated JSON that already omits null-valued keys for
# readability (`years`/`last_used_year` are absent, not `null`, on several
# entries above), so the existing TOP-LEVEL-key drift guards
# (``test_outbox_parsed_exclude_constant_matches_the_fixtures_dropped_keys``
# / ``test_resume_fixture_parsed_retains_the_n1_allowed_structured_fields``)
# are structurally unaffected by a nested, optional per-skill field and stay
# green either way -- they were never the right granularity to catch this.
# This is the real, intended strengthening the fixture-drift guard needed:
# tying the fixture's NEW `categories` key to the real schema and the real
# closed family vocabulary, so a future refactor can't let it silently rot
# into decorative JSON nobody validates.


def test_resume_fixture_declares_at_least_one_classifier_assigned_category() -> None:
    """Guards the guard below: if the fixture ever lost its one `categories`
    example, the schema/family-membership assertions would pass vacuously."""
    payload = _load_json(RESUME_FIXTURE_PATH)
    skills = payload["parsed"]["skills"]
    assert any("categories" in s for s in skills), (
        "fixture must demonstrate at least one classifier-assigned "
        "category (Phase 3.3, ROADMAP A2) -- otherwise this guard is vacuous"
    )


def test_resume_fixture_skill_categories_are_real_schema_backed_families() -> None:
    """The fixture's `categories` values must validate against the REAL
    ``ResumeSkill`` model (not merely happen to parse as JSON) and every
    family name in them must be one of the REAL, closed
    ``skill_classifier.known_families()`` -- both checked against the actual
    implementation, never re-typed here, so this cannot silently drift from
    either side."""
    from src.pipeline.skill_classifier import known_families

    families = set(known_families())
    payload = _load_json(RESUME_FIXTURE_PATH)
    for raw in payload["parsed"]["skills"]:
        validated = ResumeSkill.model_validate(raw)
        assert validated.categories == raw.get("categories"), (
            f"{raw.get('name')!r}: fixture's categories did not round-trip "
            "through the real ResumeSkill schema unchanged"
        )
        if validated.categories:
            assert set(validated.categories) <= families, (
                f"{raw['name']!r} carries a family not in the real, closed "
                f"categories.yaml vocabulary: {validated.categories!r}"
            )
            assert (
                len(validated.categories) <= 2
            ), f"{raw['name']!r} exceeds the per-skill family cap"


# ── The new fixtures/outbox/ directory is actually PII-scanned ──────────
#
# The 4a corpus's PII scanners (``tests.unit.test_evals_corpus``) glob
# ``FIXTURES_DIR.rglob("*.json")`` (finding B5) rather than an enumerated
# filename list, SPECIFICALLY so a future non-resume fixture directory (this
# one) would be covered without a code change there. Proven two ways below.


def test_fixtures_rglob_reaches_the_nested_outbox_directory() -> None:
    """Mutation target: replacing ``rglob`` with a non-recursive ``glob`` in
    ``test_evals_corpus.py`` would silently stop scanning this directory —
    this test recomputes the glob independently (not importing the cached
    module-level list) and proves it still reaches nested files."""
    found = {p.name for p in FIXTURES_DIR.rglob("*.json")}
    assert "resume_parsed.json" in found
    assert "job_parsed.json" in found
    # And, the direction that actually matters for the corpus's own gate:
    # every file collected by the corpus's own module-level list too.
    from tests.unit.test_evals_corpus import _ALL_FIXTURE_FILES

    corpus_names = {p.name for p in _ALL_FIXTURE_FILES}
    assert "resume_parsed.json" in corpus_names
    assert "job_parsed.json" in corpus_names


def test_scanners_flag_a_planted_leak_inside_a_nested_outbox_fixture(
    tmp_path: Path,
) -> None:
    """Plants a real-looking phone/email inside a tmp copy of an
    outbox-shaped fixture, nested exactly like ``fixtures/outbox/`` is, and
    proves the corpus's scanning FUNCTIONS (not just the glob) flag it — the
    probe the deliverable asks for. Uses the same RFC-2606 ``.invalid`` /
    off-range convention ``test_evals_corpus.py`` uses for its own probes
    (finding B4): the guard must not itself carry a real-looking string."""
    from tests.unit.test_evals_corpus import _offending_emails, _offending_phones

    nested = tmp_path / "fixtures" / "outbox"
    nested.mkdir(parents=True)
    leaky = nested / "leaky_resume_parsed.json"
    leaky.write_text(
        json.dumps(
            {
                "parsed": {
                    "experience": [
                        {
                            "company": (
                                "Reach me at notreal.person@nonexistent-"
                                "employer.invalid or 604-555-1212 for a "
                                "reference."
                            ),
                            "title": "Engineer",
                            "bullets": [],
                        }
                    ]
                },
                "summary_emb": [0.01] * 8,
                "chunk_embs": {},
                "prompt_version": "resume_core_v1+resume_skills_v2",
                "job_id": "00000000-0000-0000-0000-000000000000",
            }
        ),
        encoding="utf-8",
    )

    email_offenders = _offending_emails(leaky.read_text(encoding="utf-8"))
    phone_offenders = _offending_phones(leaky.read_text(encoding="utf-8"))
    assert email_offenders, "the email scanner did not flag the planted leak"
    assert phone_offenders, "the phone scanner did not flag the planted leak"


# ── The shipped fixtures pass the corpus's own scanners (no leak of our own) ──


def test_shipped_outbox_fixtures_have_no_email_shaped_pii() -> None:
    from tests.unit.test_evals_corpus import _offending_emails

    for path in (RESUME_FIXTURE_PATH, JOB_FIXTURE_PATH):
        offenders = _offending_emails(path.read_text(encoding="utf-8"))
        assert not offenders, f"{path.name}: {offenders!r}"


def test_shipped_outbox_fixtures_have_no_phone_shaped_pii() -> None:
    """R2 warning from the plan: a float embedding rounded to >4 decimal
    places can produce a 10-digit run that the phone scanner flags as a bare
    NANP number. The fixtures are generated at exactly 4 decimal places for
    this reason — this test is the mechanical proof, not a promise."""
    from tests.unit.test_evals_corpus import _offending_phones

    for path in (RESUME_FIXTURE_PATH, JOB_FIXTURE_PATH):
        offenders = _offending_phones(path.read_text(encoding="utf-8"))
        assert not offenders, f"{path.name}: {offenders!r}"


def test_shipped_resume_fixture_embeddings_use_at_most_four_decimal_places() -> None:
    payload = _load_json(RESUME_FIXTURE_PATH)
    raw = RESUME_FIXTURE_PATH.read_text(encoding="utf-8")
    decimals = re.findall(r"\d+\.(\d+)", raw)
    assert decimals, "expected floating-point embedding literals in the fixture"
    assert all(len(d) <= 4 for d in decimals), (
        "an embedding literal in the fixture has more than 4 decimal places "
        "-- this is exactly the phone-scanner false-positive trap the plan "
        "warns about (a float like 0.4353861809 shapes as a bare 10-digit "
        "phone number once the decimal point is stripped)"
    )
    assert payload  # keep the loaded payload referenced (shape already checked above)
