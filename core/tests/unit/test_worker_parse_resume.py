"""Unit tests for ``src.worker.resume_tasks`` — all I/O mocked.

Same real-ctx-keys discipline as ``test_worker_parse_job.py``: ``ctx`` is
built from ``pg_pool`` / ``llm`` / ``embedder`` / ``blob_store`` (the keys
``src/worker/main.py::startup`` actually sets), never hris's ``ctx["minio"]``/
``ctx["minio_bucket_resumes_raw"]``.

``blob_store`` is the Phase-1 ``BlobStore`` — ``.get(key)`` is already async
and takes a single relative key, no bucket. Every test that reaches the blob
fetch asserts it goes through ``blob_store.get(meta["blob_key"])`` and that
nothing calls a MinIO-style ``get_object``.

Covers:
* ``parse_resume`` control flow: missing/stale/blob-failure/extract-failure/
  zero-chunks/core-LLM-failure/happy-path/race-guard/ResumeParsed-validation-
  failure (finding 2b)/outbox-payload-excludes-candidate (finding 5).
* ``_drop_smeared_years``: the >=6-shared-exact-value boundary (6 triggers,
  5 does not), unrelated skills untouched.
* ``_extract_skills_merged``: deterministic-scan-floor MERGED with a
  non-fatal best-effort LLM call, first-non-null-wins on canonical
  collision, capped at 400 (LLM half ordered first so it's never the half
  truncated), and trailing-technical-skills survive an administrative-heavy
  overflow.
* ``_parse_cover_letter``: absent (no I/O), blob branch (mime from
  extension), text branch (``set_pii_key`` before ``decrypt``), and a
  non-fatal LLM failure on the cover letter (must not fail the résumé
  parse).

``src.worker.resume_tasks`` does not exist yet — RED half of the TDD cycle;
this whole file is expected to fail at collection (ImportError).

── Round 2 (merge-blocking re-audit) additions ─────────────────────────────
* Finding 3 (MAJOR): a PERMANENT embedding failure (``LLMOutputInvalidError``
  from ``embedder.embed`` — a dim/count mismatch) must funnel through
  ``record_parse_failure`` -> "failed", exactly like the core-LLM failure
  does, not escape ``parse_resume`` uncaught. Covers both the summary-embed
  call and the chunk-embed call (``_embed_batched``) separately.
* Finding 6 (MEDIUM): the outbox payload must not carry raw chunk TEXT
  (``parsed.chunks[].text`` / ``parsed.cover_letter_chunks[].text``) — header
  chunks contain the candidate's name/email/phone verbatim, so shipping
  chunk text in the unencrypted ``outbox`` jsonb still leaks identity even
  though round 1 dropped the structured ``candidate`` block.

── Round 3 (merge-blocking re-audit) additions ─────────────────────────────
* F1 (HIGH): the TEXT handed to the embedder (chunk batch + summary) must
  never carry the candidate's name/email/phone/location verbatim — a header
  chunk's embedding (``chunk_embs``) and a name-opening LLM summary's
  embedding (``summary_emb``) both ride the (unencrypted) outbox and get
  projected into Neo4j in Phase 4, so they are PII-equivalent even though
  round 2 already dropped raw chunk TEXT from the outbox payload. Covers the
  pure ``_redact_candidate_pii`` helper AND end-to-end embed-call-capture
  assertions, and pins that ``resumes.parsed`` (the system of record) keeps
  the FULL, unredacted chunk text/summary — only the embedder's input is
  scrubbed.
* F2 (MEDIUM): the outbox ``parsed`` dict must not carry the cleartext
  ``summary`` field either (belt-and-braces alongside F1's embedding scrub).
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import arq
import pytest
from annotated_types import MaxLen
from pydantic import BaseModel, ValidationError

from src.pipeline.llm import LLMOutputInvalidError, LLMUnavailableError
from src.pipeline.parsing import MIME_DOCX, EncryptedPdfError, UnsupportedMimeError
from src.pipeline.skills import _alias_table
from src.schemas import (
    CandidateInfo,
    CoverLetterParsed,
    Experience,
    ResumeChunk,
    ResumeCore,
    ResumeParsed,
    ResumeSkill,
    ResumeSkillDetail,
    ResumeSkillDetails,
)
from src.settings import Settings
from src.storage.blob_store import BlobNotFound
from src.worker.resume_tasks import (
    _MAX_SKILLS,
    _drop_smeared_years,
    _extract_skills_merged,
    _parse_cover_letter,
    _redact_skill_names_pii,
    parse_resume,
)

# ── shared helpers ──────────────────────────────────────────────────────


def _flat_call_args(mock_call: Any) -> list[Any]:
    """Positional + keyword args of a single mock call, order-agnostic."""
    return list(mock_call.args) + list(mock_call.kwargs.values())


def _acm(return_value: Any = None) -> MagicMock:
    """A MagicMock usable as ``async with x() as y: ...``."""
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=return_value)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _make_conn(fetchrow_result: Any) -> MagicMock:
    conn = MagicMock(name="conn")
    conn.fetchrow = AsyncMock(return_value=fetchrow_result)
    conn.transaction = MagicMock(return_value=_acm())
    # FU-7 (ADR-021 §3): `parse_resume` now calls the REAL
    # `resume_service.claim_parsing(conn, resume_id)` unconditionally right
    # after the status check, on every test that doesn't patch it away (only
    # the two claim_parsing-focused tests below do). That function awaits
    # `conn.execute(...)` — an attribute this fixture never configured before
    # FU-7, so it auto-created as a plain (non-async) `MagicMock`, and
    # awaiting it raised `TypeError: object MagicMock can't be used in
    # 'await' expression` in every OTHER test exercising `parse_resume`'s
    # later failure/success paths, none of which care about the claim
    # outcome (that is exercised by the dedicated claim_parsing tests). A
    # benign default closes that fixture gap without touching any
    # assertion.
    conn.execute = AsyncMock(return_value="UPDATE 1")
    return conn


def _make_ctx(
    conn: MagicMock,
    *,
    llm: MagicMock | None = None,
    embedder: MagicMock | None = None,
    blob_store: MagicMock | None = None,
) -> dict[str, Any]:
    pool = MagicMock(name="pg_pool")
    pool.acquire = MagicMock(return_value=_acm(conn))
    return {
        "pg_pool": pool,
        "llm": llm if llm is not None else MagicMock(chat_json=AsyncMock()),
        "embedder": embedder if embedder is not None else MagicMock(embed=AsyncMock()),
        "blob_store": (
            blob_store if blob_store is not None else MagicMock(get=AsyncMock())
        ),
    }


def _meta_row(
    *,
    job_id: UUID,
    blob_key: str = "resumes/abc.pdf",
    mime_type: str = "application/pdf",
    status: str = "uploaded",
    cover_letter_blob_key: str | None = None,
    cover_letter_text: bytes | None = None,
) -> dict[str, Any]:
    return {
        "job_id": job_id,
        "blob_key": blob_key,
        "mime_type": mime_type,
        "status": status,
        "cover_letter_blob_key": cover_letter_blob_key,
        "cover_letter_text": cover_letter_text,
    }


def _fake_prompt(version: str = "resume_core_v1") -> MagicMock:
    prompt = MagicMock()
    prompt.messages = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "user prompt"},
    ]
    prompt.version = version
    return prompt


def _make_validation_error() -> ValidationError:
    """A real ``pydantic.ValidationError`` instance, the same exception type
    ``ResumeParsed.model_validate`` raises on malformed input."""

    class _Sentinel(BaseModel):
        x: int

    try:
        _Sentinel.model_validate({"x": "not-an-int"})
    except ValidationError as exc:
        return exc
    raise AssertionError("expected ValidationError")  # pragma: no cover


# ── parse_resume: missing / stale ──────────────────────────────────────


@pytest.mark.asyncio
async def test_missing_resume_row_returns_missing() -> None:
    conn = _make_conn(None)
    ctx = _make_ctx(conn)

    result = await parse_resume(ctx, str(uuid4()))

    assert result == "missing"


@pytest.mark.parametrize("status", ["parsed", "failed"])
@pytest.mark.asyncio
async def test_status_not_uploaded_or_parsing_returns_stale_no_io(status: str) -> None:
    conn = _make_conn(_meta_row(job_id=uuid4(), status=status))
    llm = MagicMock(chat_json=AsyncMock())
    blob_store = MagicMock(get=AsyncMock())
    ctx = _make_ctx(conn, llm=llm, blob_store=blob_store)

    result = await parse_resume(ctx, str(uuid4()))

    assert result == "stale"
    llm.chat_json.assert_not_called()
    blob_store.get.assert_not_called()


# ── parse_resume: blob / extract failures ──────────────────────────────


@pytest.mark.asyncio
async def test_blob_fetch_failure_records_failure_and_returns_failed() -> None:
    resume_id = uuid4()
    conn = _make_conn(_meta_row(job_id=uuid4()))
    blob_store = MagicMock(get=AsyncMock(side_effect=BlobNotFound("gone")))
    llm = MagicMock(chat_json=AsyncMock())
    ctx = _make_ctx(conn, llm=llm, blob_store=blob_store)

    with patch(
        "src.worker.resume_tasks.resume_service.record_parse_failure",
        new_callable=AsyncMock,
    ) as record_failure:
        result = await parse_resume(ctx, str(resume_id))

    assert result == "failed"
    record_failure.assert_awaited_once()
    assert resume_id in _flat_call_args(record_failure.await_args)
    llm.chat_json.assert_not_called()


@pytest.mark.parametrize(
    "exc", [UnsupportedMimeError("bad mime"), EncryptedPdfError("locked")]
)
@pytest.mark.asyncio
async def test_extract_text_failure_records_failure_and_returns_failed(
    exc: Exception,
) -> None:
    resume_id = uuid4()
    conn = _make_conn(_meta_row(job_id=uuid4()))
    blob_store = MagicMock(get=AsyncMock(return_value=b"pdf-bytes"))
    llm = MagicMock(chat_json=AsyncMock())
    ctx = _make_ctx(conn, llm=llm, blob_store=blob_store)

    with (
        patch("src.worker.resume_tasks.extract_text", MagicMock(side_effect=exc)),
        patch(
            "src.worker.resume_tasks.resume_service.record_parse_failure",
            new_callable=AsyncMock,
        ) as record_failure,
    ):
        result = await parse_resume(ctx, str(resume_id))

    assert result == "failed"
    record_failure.assert_awaited_once()
    llm.chat_json.assert_not_called()


@pytest.mark.asyncio
async def test_zero_chunks_records_failure_before_any_llm_call() -> None:
    """Must fail before spending an LLM pass on unusable input."""
    resume_id = uuid4()
    conn = _make_conn(_meta_row(job_id=uuid4()))
    blob_store = MagicMock(get=AsyncMock(return_value=b"pdf-bytes"))
    llm = MagicMock(chat_json=AsyncMock())
    ctx = _make_ctx(conn, llm=llm, blob_store=blob_store)

    with (
        patch(
            "src.worker.resume_tasks.extract_text", MagicMock(return_value=MagicMock())
        ),
        patch("src.worker.resume_tasks.chunk_resume", MagicMock(return_value=[])),
        patch(
            "src.worker.resume_tasks.resume_service.record_parse_failure",
            new_callable=AsyncMock,
        ) as record_failure,
    ):
        result = await parse_resume(ctx, str(resume_id))

    assert result == "failed"
    record_failure.assert_awaited_once()
    flat = _flat_call_args(record_failure.await_args)
    assert any(
        isinstance(a, str) and "chunk" in a.lower() for a in flat
    ), "record_parse_failure reason should mention 'no chunks produced'"
    llm.chat_json.assert_not_called()


@pytest.mark.asyncio
async def test_core_llm_failure_records_failure_and_returns_failed() -> None:
    resume_id = uuid4()
    conn = _make_conn(_meta_row(job_id=uuid4()))
    blob_store = MagicMock(get=AsyncMock(return_value=b"pdf-bytes"))
    llm = MagicMock(chat_json=AsyncMock(side_effect=LLMOutputInvalidError("bad json")))
    ctx = _make_ctx(conn, llm=llm, blob_store=blob_store)
    chunks = [
        ResumeChunk(id="c_001", section="summary", page=0, text="Experienced engineer.")
    ]

    with (
        patch(
            "src.worker.resume_tasks.extract_text", MagicMock(return_value=MagicMock())
        ),
        patch("src.worker.resume_tasks.chunk_resume", MagicMock(return_value=chunks)),
        patch(
            "src.worker.resume_tasks.resume_service.record_parse_failure",
            new_callable=AsyncMock,
        ) as record_failure,
        patch(
            "src.worker.resume_tasks.resume_service.record_parsed",
            new_callable=AsyncMock,
        ) as record_parsed,
    ):
        result = await parse_resume(ctx, str(resume_id))

    assert result == "failed"
    record_failure.assert_awaited_once()
    record_parsed.assert_not_awaited()


# ── Finding 2b (HIGH): a ResumeParsed ValidationError must not escape ──────
#
# ResumeParsed.model_validate(...) in parse_resume today sits in NO
# try/except. An ordinary long CV that chunks into >200 pieces (finding 2a)
# — or any other cause of a malformed payload — trips ValidationError,
# which propagates straight out of parse_resume: record_parse_failure never
# runs, the row is stranded uploaded/parsing with a NULL failure_reason, and
# arq re-runs the whole expensive LLM pipeline on every retry.


@pytest.mark.asyncio
async def test_resume_parsed_validation_error_is_caught_not_raised() -> None:
    resume_id = uuid4()
    conn = _make_conn(_meta_row(job_id=uuid4()))
    blob_store = MagicMock(get=AsyncMock(return_value=b"pdf-bytes"))
    core = ResumeCore(summary="Backend engineer.")
    llm = MagicMock(chat_json=AsyncMock(return_value=core))
    chunks = [
        ResumeChunk(id="c_001", section="summary", page=0, text="Backend engineer.")
    ]
    embedder = MagicMock(embed=AsyncMock())
    ctx = _make_ctx(conn, llm=llm, blob_store=blob_store, embedder=embedder)

    with (
        patch(
            "src.worker.resume_tasks.extract_text", MagicMock(return_value=MagicMock())
        ),
        patch("src.worker.resume_tasks.chunk_resume", MagicMock(return_value=chunks)),
        patch(
            "src.worker.resume_tasks._extract_skills_merged",
            new_callable=AsyncMock,
            return_value=([], None),
        ),
        patch(
            "src.worker.resume_tasks.ResumeParsed.model_validate",
            MagicMock(side_effect=_make_validation_error()),
        ),
        patch(
            "src.worker.resume_tasks.resume_service.record_parse_failure",
            new_callable=AsyncMock,
        ) as record_failure,
        patch(
            "src.worker.resume_tasks.resume_service.record_parsed",
            new_callable=AsyncMock,
        ) as record_parsed,
        patch(
            "src.worker.resume_tasks.outbox_service.enqueue_outbox",
            new_callable=AsyncMock,
        ) as enqueue,
    ):
        # Today this raises ValidationError straight out of parse_resume —
        # that IS the bug. The assertion below is what the fix must satisfy.
        result = await parse_resume(ctx, str(resume_id))

    assert result == "failed"
    record_failure.assert_awaited_once()
    record_parsed.assert_not_awaited()
    enqueue.assert_not_awaited()


# ── Finding 3 (MAJOR, round 2): a PERMANENT embedding failure must not ──────
# escape parse_resume uncaught ───────────────────────────────────────────
#
# LLMClient.embed raises LLMOutputInvalidError on a count mismatch AND on
# the expected_dim check — a PERMANENT per-document error, exactly like a
# core-LLM chat_json failure. The embedding calls (summary embed, chunk
# embeds via _embed_batched) are today UNguarded: the exception escapes
# parse_resume uncaught, stranding the row uploaded/parsing with a NULL
# failure_reason. (A transient LLMUnavailableError is NOT asserted here —
# it may still escape so arq retries a genuine outage.)


@pytest.mark.asyncio
async def test_summary_embed_permanent_failure_records_failure_and_returns_failed() -> (
    None
):
    resume_id = uuid4()
    conn = _make_conn(_meta_row(job_id=uuid4()))
    blob_store = MagicMock(get=AsyncMock(return_value=b"pdf-bytes"))
    core = ResumeCore(
        candidate=CandidateInfo(name="Ada Lovelace"), summary="Backend engineer."
    )
    llm = MagicMock(chat_json=AsyncMock(return_value=core))
    chunks = [
        ResumeChunk(id="c_001", section="summary", page=0, text="Backend engineer.")
    ]
    embedder = MagicMock(
        embed=AsyncMock(side_effect=LLMOutputInvalidError("768-d contract violated"))
    )
    ctx = _make_ctx(conn, llm=llm, blob_store=blob_store, embedder=embedder)

    with (
        patch(
            "src.worker.resume_tasks.extract_text", MagicMock(return_value=MagicMock())
        ),
        patch("src.worker.resume_tasks.chunk_resume", MagicMock(return_value=chunks)),
        patch(
            "src.worker.resume_tasks._extract_skills_merged",
            new_callable=AsyncMock,
            return_value=([], None),
        ),
        patch(
            "src.worker.resume_tasks.resume_service.record_parse_failure",
            new_callable=AsyncMock,
        ) as record_failure,
        patch(
            "src.worker.resume_tasks.resume_service.record_parsed",
            new_callable=AsyncMock,
        ) as record_parsed,
        patch(
            "src.worker.resume_tasks.outbox_service.enqueue_outbox",
            new_callable=AsyncMock,
        ) as enqueue,
    ):
        # Today LLMOutputInvalidError escapes uncaught right here — that IS
        # the bug. The assertions below are what the fix must satisfy.
        result = await parse_resume(ctx, str(resume_id))

    assert result == "failed"
    record_failure.assert_awaited_once()
    record_parsed.assert_not_awaited()
    enqueue.assert_not_awaited()


@pytest.mark.asyncio
async def test_chunk_embed_permanent_failure_records_failure_and_returns_failed() -> (
    None
):
    """Same guarantee for the CHUNK embed call (`_embed_batched`), separable
    from the summary embed: the summary embed succeeds, the chunk-batch
    embed raises."""
    resume_id = uuid4()
    conn = _make_conn(_meta_row(job_id=uuid4()))
    blob_store = MagicMock(get=AsyncMock(return_value=b"pdf-bytes"))
    core = ResumeCore(
        candidate=CandidateInfo(name="Ada Lovelace"), summary="Backend engineer."
    )
    llm = MagicMock(chat_json=AsyncMock(return_value=core))
    chunks = [
        ResumeChunk(id="c_001", section="summary", page=0, text="Backend engineer.")
    ]
    embedder = MagicMock(
        embed=AsyncMock(
            side_effect=[[[0.1] * 8], LLMOutputInvalidError("count mismatch")]
        )
    )
    ctx = _make_ctx(conn, llm=llm, blob_store=blob_store, embedder=embedder)

    with (
        patch(
            "src.worker.resume_tasks.extract_text", MagicMock(return_value=MagicMock())
        ),
        patch("src.worker.resume_tasks.chunk_resume", MagicMock(return_value=chunks)),
        patch(
            "src.worker.resume_tasks._extract_skills_merged",
            new_callable=AsyncMock,
            return_value=([], None),
        ),
        patch(
            "src.worker.resume_tasks.resume_service.record_parse_failure",
            new_callable=AsyncMock,
        ) as record_failure,
        patch(
            "src.worker.resume_tasks.resume_service.record_parsed",
            new_callable=AsyncMock,
        ) as record_parsed,
        patch(
            "src.worker.resume_tasks.outbox_service.enqueue_outbox",
            new_callable=AsyncMock,
        ) as enqueue,
    ):
        result = await parse_resume(ctx, str(resume_id))

    assert result == "failed"
    record_failure.assert_awaited_once()
    record_parsed.assert_not_awaited()
    enqueue.assert_not_awaited()


# ── parse_resume: happy path ────────────────────────────────────────────


@pytest.mark.parametrize("status", ["uploaded", "parsing"])
@pytest.mark.asyncio
async def test_happy_path_no_cover_letter_returns_parsed(status: str) -> None:
    resume_id = uuid4()
    job_id = uuid4()
    conn = _make_conn(
        _meta_row(job_id=job_id, status=status, blob_key="resumes/abc.pdf")
    )
    blob_store = MagicMock(get=AsyncMock(return_value=b"pdf-bytes"))
    core = ResumeCore(
        candidate=CandidateInfo(name="Ada Lovelace"),
        summary="Backend engineer.",
        experience=[Experience(company="Acme", title="Engineer")],
    )
    llm = MagicMock(chat_json=AsyncMock(return_value=core))
    chunks = [
        ResumeChunk(id="c_001", section="summary", page=0, text="Backend engineer."),
        ResumeChunk(id="c_002", section="skills", page=0, text="Python, SQL"),
    ]
    summary_vec = [0.1] * 8
    chunk_vecs = [[0.2] * 8, [0.3] * 8]
    embedder = MagicMock(embed=AsyncMock(side_effect=[[summary_vec], chunk_vecs]))
    ctx = _make_ctx(conn, llm=llm, blob_store=blob_store, embedder=embedder)
    merged_skills = [ResumeSkill(name="Python", years=5, evidence_chunk_ids=["c_002"])]

    with (
        patch(
            "src.worker.resume_tasks.extract_text", MagicMock(return_value=MagicMock())
        ),
        patch("src.worker.resume_tasks.chunk_resume", MagicMock(return_value=chunks)),
        patch(
            "src.worker.resume_tasks._extract_skills_merged",
            new_callable=AsyncMock,
            return_value=(merged_skills, None),
        ),
        patch(
            "src.worker.resume_tasks.resume_service.encrypt_pii_via_session",
            new_callable=AsyncMock,
            return_value=(b"enc-name", b"enc-email", b"enc-phone", "hash123"),
        ) as encrypt_pii,
        patch(
            "src.worker.resume_tasks.resume_service.record_parsed",
            new_callable=AsyncMock,
            return_value=True,
        ) as record_parsed,
        patch(
            "src.worker.resume_tasks.outbox_service.enqueue_outbox",
            new_callable=AsyncMock,
        ) as enqueue,
    ):
        result = await parse_resume(ctx, str(resume_id))

    assert result == "parsed"
    blob_store.get.assert_awaited_once_with("resumes/abc.pdf")
    assert blob_store.get_object.called is False  # no MinIO-style call site

    encrypt_pii.assert_awaited_once()
    record_parsed.assert_awaited_once()
    flat = _flat_call_args(record_parsed.await_args)
    assert resume_id in flat
    assert any(isinstance(a, ResumeParsed) for a in flat)
    assert (b"enc-name", b"enc-email", b"enc-phone", "hash123") in flat
    assert None in flat  # cover_letter_parsed — no cover letter attached
    assert any(isinstance(a, dt.datetime) for a in flat)

    assert embedder.embed.call_count == 2
    enqueue.assert_awaited_once()
    kwargs = enqueue.await_args.kwargs
    assert kwargs["event_type"] == "resume.parsed"
    assert kwargs["aggregate_id"] == resume_id
    payload = kwargs["payload"]
    assert payload["summary_emb"] == summary_vec
    assert payload["chunk_embs"] == {"c_001": chunk_vecs[0], "c_002": chunk_vecs[1]}
    assert payload["prompt_version"] == "resume_core_v1+resume_skills_v2"
    assert str(payload["job_id"]) == str(job_id)
    assert isinstance(payload["parsed"], dict)


# ── L1 (security re-audit round 2): hallucinated evidence_chunk_ids scrubbed
#
# ``ResumeChunk``'s own docstring already promises "citations that don't
# exist in the chunk set are scrubbed before persistence" — but
# ``scrub_invalid_chunk_refs`` existed and was never actually called from
# ``parse_resume``. A skill's ``evidence_chunk_ids`` citing a chunk id the
# LLM hallucinated (never in this résumé's real chunk set) must be dropped
# before ``ResumeParsed.model_validate`` — a broken evidence link is exactly
# the failure mode the docstring already describes.


@pytest.mark.asyncio
async def test_hallucinated_evidence_chunk_ids_are_scrubbed_before_persistence() -> (
    None
):
    resume_id = uuid4()
    job_id = uuid4()
    conn = _make_conn(
        _meta_row(job_id=job_id, status="uploaded", blob_key="resumes/abc.pdf")
    )
    blob_store = MagicMock(get=AsyncMock(return_value=b"pdf-bytes"))
    core = ResumeCore(
        candidate=CandidateInfo(name="Ada Lovelace"), summary="Backend engineer."
    )
    llm = MagicMock(chat_json=AsyncMock(return_value=core))
    chunks = [
        ResumeChunk(id="c_001", section="summary", page=0, text="Backend engineer."),
        ResumeChunk(id="c_002", section="skills", page=0, text="Python, SQL"),
    ]
    embedder = MagicMock(
        embed=AsyncMock(side_effect=[[[0.1] * 8], [[0.2] * 8, [0.3] * 8]])
    )
    ctx = _make_ctx(conn, llm=llm, blob_store=blob_store, embedder=embedder)
    # "c_999" was never in this résumé's chunk set — a realistic hallucinated
    # citation. "c_002" is real and must survive untouched.
    merged_skills = [
        ResumeSkill(name="Python", years=5, evidence_chunk_ids=["c_002", "c_999"])
    ]

    with (
        patch(
            "src.worker.resume_tasks.extract_text", MagicMock(return_value=MagicMock())
        ),
        patch("src.worker.resume_tasks.chunk_resume", MagicMock(return_value=chunks)),
        patch(
            "src.worker.resume_tasks._extract_skills_merged",
            new_callable=AsyncMock,
            return_value=(merged_skills, None),
        ),
        patch(
            "src.worker.resume_tasks.resume_service.encrypt_pii_via_session",
            new_callable=AsyncMock,
            return_value=(b"enc-name", b"enc-email", b"enc-phone", "hash123"),
        ),
        patch(
            "src.worker.resume_tasks.resume_service.record_parsed",
            new_callable=AsyncMock,
            return_value=True,
        ) as record_parsed,
        patch(
            "src.worker.resume_tasks.outbox_service.enqueue_outbox",
            new_callable=AsyncMock,
        ),
    ):
        result = await parse_resume(ctx, str(resume_id))

    assert result == "parsed"
    flat = _flat_call_args(record_parsed.await_args)
    persisted = next(a for a in flat if isinstance(a, ResumeParsed))
    skill = next(s for s in persisted.skills if s.name == "Python")
    assert skill.evidence_chunk_ids == ["c_002"], (
        "hallucinated chunk id 'c_999' survived into resumes.parsed — "
        "scrub_invalid_chunk_refs is not wired into parse_resume (L1)"
    )


# ── F3 (security re-audit), layer 2: skill-name identity scrub ───────────
#
# skills_graph's shape-reject (layer 1, Phase 4b) has no candidate context —
# it cannot tell that a skill named "Casey Rivera" IS the résumé's own
# candidate. This module has that context (`core.candidate`), so a skill
# name that matches it must be scrubbed BEFORE it enters `resumes.parsed`
# (Postgres, system of record) and the outbox — not just the embedder's
# input, unlike `_redact_candidate_pii`'s other two call sites.


@pytest.mark.asyncio
async def test_skill_name_matching_candidate_identity_is_dropped_before_storage_and_outbox() -> (  # noqa: E501
    None
):
    resume_id = uuid4()
    job_id = uuid4()
    conn = _make_conn(
        _meta_row(job_id=job_id, status="uploaded", blob_key="resumes/abc.pdf")
    )
    blob_store = MagicMock(get=AsyncMock(return_value=b"pdf-bytes"))
    core = ResumeCore(
        candidate=CandidateInfo(name="Casey Rivera"), summary="Backend engineer."
    )
    llm = MagicMock(chat_json=AsyncMock(return_value=core))
    chunks = [
        ResumeChunk(id="c_001", section="summary", page=0, text="Backend engineer.")
    ]
    embedder = MagicMock(embed=AsyncMock(side_effect=[[[0.1] * 8], [[0.2] * 8]]))
    ctx = _make_ctx(conn, llm=llm, blob_store=blob_store, embedder=embedder)
    # A small model hallucinating the header block as a "skill" — the exact
    # security-report reproducer.
    merged_skills = [
        ResumeSkill(name="python", years=5),
        ResumeSkill(name="Casey Rivera"),
    ]

    with (
        patch(
            "src.worker.resume_tasks.extract_text", MagicMock(return_value=MagicMock())
        ),
        patch("src.worker.resume_tasks.chunk_resume", MagicMock(return_value=chunks)),
        patch(
            "src.worker.resume_tasks._extract_skills_merged",
            new_callable=AsyncMock,
            return_value=(merged_skills, None),
        ),
        patch(
            "src.worker.resume_tasks.resume_service.encrypt_pii_via_session",
            new_callable=AsyncMock,
            return_value=(b"enc-name", b"enc-email", b"enc-phone", "hash123"),
        ),
        patch(
            "src.worker.resume_tasks.resume_service.record_parsed",
            new_callable=AsyncMock,
            return_value=True,
        ) as record_parsed,
        patch(
            "src.worker.resume_tasks.outbox_service.enqueue_outbox",
            new_callable=AsyncMock,
        ) as enqueue,
    ):
        result = await parse_resume(ctx, str(resume_id))

    assert result == "parsed"

    stored_parsed = next(
        a
        for a in _flat_call_args(record_parsed.await_args)
        if isinstance(a, ResumeParsed)
    )
    stored_names = [s.name for s in stored_parsed.skills]
    assert "python" in stored_names
    assert not any(
        "casey" in n.lower() and "rivera" in n.lower() for n in stored_names
    ), f"candidate identity survived as a stored skill name: {stored_names!r}"

    payload = enqueue.await_args.kwargs["payload"]
    outbox_skill_names = [s["name"] for s in payload["parsed"]["skills"]]
    assert "python" in outbox_skill_names
    assert not any(
        "casey" in n.lower() and "rivera" in n.lower() for n in outbox_skill_names
    ), f"candidate identity survived in the outbox skill list: {outbox_skill_names!r}"


@pytest.mark.asyncio
async def test_skill_name_pii_drop_is_logged_as_a_count_never_the_name(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """R3 discipline applied to the layer-2 drop too."""
    import logging

    resume_id = uuid4()
    job_id = uuid4()
    conn = _make_conn(
        _meta_row(job_id=job_id, status="uploaded", blob_key="resumes/abc.pdf")
    )
    blob_store = MagicMock(get=AsyncMock(return_value=b"pdf-bytes"))
    core = ResumeCore(
        candidate=CandidateInfo(name="Casey Rivera"), summary="Backend engineer."
    )
    llm = MagicMock(chat_json=AsyncMock(return_value=core))
    chunks = [
        ResumeChunk(id="c_001", section="summary", page=0, text="Backend engineer.")
    ]
    embedder = MagicMock(embed=AsyncMock(side_effect=[[[0.1] * 8], [[0.2] * 8]]))
    ctx = _make_ctx(conn, llm=llm, blob_store=blob_store, embedder=embedder)
    merged_skills = [ResumeSkill(name="Casey Rivera")]

    with (
        caplog.at_level(logging.WARNING),
        patch(
            "src.worker.resume_tasks.extract_text", MagicMock(return_value=MagicMock())
        ),
        patch("src.worker.resume_tasks.chunk_resume", MagicMock(return_value=chunks)),
        patch(
            "src.worker.resume_tasks._extract_skills_merged",
            new_callable=AsyncMock,
            return_value=(merged_skills, None),
        ),
        patch(
            "src.worker.resume_tasks.resume_service.encrypt_pii_via_session",
            new_callable=AsyncMock,
            return_value=(None, None, None, None),
        ),
        patch(
            "src.worker.resume_tasks.resume_service.record_parsed",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "src.worker.resume_tasks.outbox_service.enqueue_outbox",
            new_callable=AsyncMock,
        ),
    ):
        await parse_resume(ctx, str(resume_id))

    all_text = "\n".join(r.getMessage() for r in caplog.records)
    assert "Casey Rivera" not in all_text
    assert "casey" not in all_text.lower()


@pytest.mark.asyncio
async def test_race_guard_stale_write_returns_stale_and_enqueues_no_outbox_row() -> (
    None
):
    """``record_parsed`` returning False means the row was no longer
    'uploaded'/'parsing' by the time the UPDATE ran. Must return "stale" and
    MUST NOT enqueue an outbox row for a write that never happened."""
    resume_id = uuid4()
    job_id = uuid4()
    conn = _make_conn(_meta_row(job_id=job_id))
    blob_store = MagicMock(get=AsyncMock(return_value=b"pdf-bytes"))
    core = ResumeCore(summary="Backend engineer.")
    llm = MagicMock(chat_json=AsyncMock(return_value=core))
    chunks = [
        ResumeChunk(id="c_001", section="summary", page=0, text="Backend engineer.")
    ]
    embedder = MagicMock(embed=AsyncMock(side_effect=[[[0.1] * 8], [[0.2] * 8]]))
    ctx = _make_ctx(conn, llm=llm, blob_store=blob_store, embedder=embedder)

    with (
        patch(
            "src.worker.resume_tasks.extract_text", MagicMock(return_value=MagicMock())
        ),
        patch("src.worker.resume_tasks.chunk_resume", MagicMock(return_value=chunks)),
        patch(
            "src.worker.resume_tasks._extract_skills_merged",
            new_callable=AsyncMock,
            return_value=([], None),
        ),
        patch(
            "src.worker.resume_tasks.resume_service.encrypt_pii_via_session",
            new_callable=AsyncMock,
            return_value=(None, None, None, None),
        ),
        patch(
            "src.worker.resume_tasks.resume_service.record_parsed",
            new_callable=AsyncMock,
            return_value=False,
        ),
        patch(
            "src.worker.resume_tasks.outbox_service.enqueue_outbox",
            new_callable=AsyncMock,
        ) as enqueue,
    ):
        result = await parse_resume(ctx, str(resume_id))

    assert result == "stale"
    enqueue.assert_not_awaited()


# ── Finding 5 (decision): outbox payload must NOT carry the candidate ─────
#
# Phase 4 needs skills/experience/embeddings, NOT identity — `resumes` is
# the system of record for PII. The outbox payload today ships the full
# ``parsed.model_dump()``, including the cleartext ``candidate`` block
# (name/email/phone), which Phase 4 would otherwise project into Neo4j.


@pytest.mark.asyncio
async def test_outbox_payload_parsed_dict_excludes_candidate_block() -> None:
    resume_id = uuid4()
    job_id = uuid4()
    conn = _make_conn(
        _meta_row(job_id=job_id, status="uploaded", blob_key="resumes/abc.pdf")
    )
    blob_store = MagicMock(get=AsyncMock(return_value=b"pdf-bytes"))
    core = ResumeCore(
        candidate=CandidateInfo(name="Ada Lovelace", email="ada@example.com"),
        summary="Backend engineer.",
        experience=[Experience(company="Acme", title="Engineer")],
    )
    llm = MagicMock(chat_json=AsyncMock(return_value=core))
    chunks = [
        ResumeChunk(id="c_001", section="summary", page=0, text="Backend engineer.")
    ]
    embedder = MagicMock(embed=AsyncMock(side_effect=[[[0.1] * 8], [[0.2] * 8]]))
    ctx = _make_ctx(conn, llm=llm, blob_store=blob_store, embedder=embedder)
    merged_skills = [ResumeSkill(name="Python")]

    with (
        patch(
            "src.worker.resume_tasks.extract_text", MagicMock(return_value=MagicMock())
        ),
        patch("src.worker.resume_tasks.chunk_resume", MagicMock(return_value=chunks)),
        patch(
            "src.worker.resume_tasks._extract_skills_merged",
            new_callable=AsyncMock,
            return_value=(merged_skills, None),
        ),
        patch(
            "src.worker.resume_tasks.resume_service.encrypt_pii_via_session",
            new_callable=AsyncMock,
            return_value=(b"enc-name", b"enc-email", b"enc-phone", "hash123"),
        ),
        patch(
            "src.worker.resume_tasks.resume_service.record_parsed",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "src.worker.resume_tasks.outbox_service.enqueue_outbox",
            new_callable=AsyncMock,
        ) as enqueue,
    ):
        result = await parse_resume(ctx, str(resume_id))

    assert result == "parsed"
    enqueue.assert_awaited_once()
    payload = enqueue.await_args.kwargs["payload"]
    parsed_dict = payload["parsed"]

    assert "candidate" not in parsed_dict, (
        "outbox payload leaks the cleartext candidate (name/email/phone) "
        "block — Phase 4 must receive skills/experience/embeddings only"
    )
    # Round 3 F2: `summary` is dropped from the outbox payload (a small model may
    # write the candidate's name into it); Phase 4 reads it from resumes.parsed.
    assert "summary" not in parsed_dict
    assert "skills" in parsed_dict
    assert "chunks" in parsed_dict
    assert payload["summary_emb"] == [0.1] * 8
    assert "chunk_embs" in payload
    assert payload["prompt_version"] == "resume_core_v1+resume_skills_v2"
    assert str(payload["job_id"]) == str(job_id)


# ── Finding 6 (MEDIUM, round 2): outbox must not carry raw chunk TEXT ─────
#
# Round 1 dropped the structured `candidate` block, but the same payload
# still ships `parsed.chunks[].text` / `parsed.cover_letter_chunks[].text` —
# raw résumé text whose header chunks contain the candidate's name/email/
# phone verbatim. So candidate identity is still in the unencrypted
# `outbox` jsonb. The fix drops chunk TEXT from the outbox `parsed` dict
# too — ids-only (or absent) is fine; the payload still needs `chunk_embs`
# keyed by chunk id plus the structured skills/experience/education/summary.
# `resumes.parsed` (the system of record) keeps full chunk text — that's an
# integration concern, not asserted here.


@pytest.mark.asyncio
async def test_outbox_payload_never_carries_raw_chunk_text_pii() -> None:
    resume_id = uuid4()
    job_id = uuid4()
    conn = _make_conn(
        _meta_row(job_id=job_id, status="uploaded", blob_key="resumes/abc.pdf")
    )
    blob_store = MagicMock(get=AsyncMock(return_value=b"pdf-bytes"))
    core = ResumeCore(
        candidate=CandidateInfo(name="Ada Lovelace", email="ada@example.com"),
        summary="Backend engineer.",
        experience=[Experience(company="Acme", title="Engineer")],
    )
    llm = MagicMock(chat_json=AsyncMock(return_value=core))
    chunks = [
        ResumeChunk(
            id="c_001",
            section="header",
            page=0,
            text="SENTINEL_HEADER_PII Ada Lovelace ada@example.com 555-0100",
        ),
        ResumeChunk(id="c_002", section="skills", page=0, text="Python, SQL"),
    ]
    embedder = MagicMock(
        embed=AsyncMock(side_effect=[[[0.1] * 8], [[0.2] * 8, [0.3] * 8]])
    )
    ctx = _make_ctx(conn, llm=llm, blob_store=blob_store, embedder=embedder)
    merged_skills = [ResumeSkill(name="Python")]

    with (
        patch(
            "src.worker.resume_tasks.extract_text", MagicMock(return_value=MagicMock())
        ),
        patch("src.worker.resume_tasks.chunk_resume", MagicMock(return_value=chunks)),
        patch(
            "src.worker.resume_tasks._extract_skills_merged",
            new_callable=AsyncMock,
            return_value=(merged_skills, None),
        ),
        patch(
            "src.worker.resume_tasks.resume_service.encrypt_pii_via_session",
            new_callable=AsyncMock,
            return_value=(b"enc-name", b"enc-email", b"enc-phone", "hash123"),
        ),
        patch(
            "src.worker.resume_tasks.resume_service.record_parsed",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "src.worker.resume_tasks.outbox_service.enqueue_outbox",
            new_callable=AsyncMock,
        ) as enqueue,
    ):
        result = await parse_resume(ctx, str(resume_id))

    assert result == "parsed"
    enqueue.assert_awaited_once()
    payload = enqueue.await_args.kwargs["payload"]

    dumped = json.dumps(payload, default=str)
    assert "SENTINEL_HEADER_PII" not in dumped, (
        "raw chunk text (candidate PII carried by header chunks) leaked "
        "into the unencrypted outbox payload"
    )

    parsed_dict = payload["parsed"]
    for key in ("chunks", "cover_letter_chunks"):
        if key in parsed_dict:
            for row in parsed_dict[key]:
                assert "text" not in row, (
                    f"outbox parsed.{key}[] still carries chunk TEXT — "
                    "ids-only is fine, raw text is not"
                )

    assert "chunk_embs" in payload
    assert set(payload["chunk_embs"]) == {"c_001", "c_002"}
    assert payload["summary_emb"] == [0.1] * 8
    assert payload["prompt_version"] == "resume_core_v1+resume_skills_v2"
    assert str(payload["job_id"]) == str(job_id)
    assert "skills" in parsed_dict
    assert "experience" in parsed_dict
    assert "education" in parsed_dict
    # Round 3 F2: `summary` is excluded from the outbox payload.
    assert "summary" not in parsed_dict


# ── _drop_smeared_years ──────────────────────────────────────────────────


def _skills_with_years(
    count: int, years: int | None, *, prefix: str = "skill"
) -> list[ResumeSkill]:
    return [ResumeSkill(name=f"{prefix}_{i}", years=years) for i in range(count)]


def test_drop_smeared_years_resets_at_exactly_6_sharing_same_value() -> None:
    """The boundary that TRIGGERS — the smeared-years failure mode."""
    skills = _skills_with_years(6, 12)

    result = _drop_smeared_years(skills, "res-1")

    assert all(s.years is None for s in result)


def test_drop_smeared_years_leaves_5_sharing_same_value_untouched() -> None:
    """The boundary that does NOT trigger."""
    skills = _skills_with_years(5, 12)

    result = _drop_smeared_years(skills, "res-1")

    assert all(s.years == 12 for s in result)


def test_drop_smeared_years_leaves_differing_values_untouched() -> None:
    skills = [ResumeSkill(name=f"skill_{i}", years=i + 1) for i in range(6)]

    result = _drop_smeared_years(skills, "res-1")

    assert [s.years for s in result] == [1, 2, 3, 4, 5, 6]


def test_drop_smeared_years_only_resets_the_smeared_group() -> None:
    smeared = _skills_with_years(6, 12, prefix="smeared")
    unrelated = ResumeSkill(name="unique_skill", years=3)

    result = _drop_smeared_years([*smeared, unrelated], "res-1")

    smeared_result = [s for s in result if s.name.startswith("smeared")]
    unrelated_result = next(s for s in result if s.name == "unique_skill")
    assert all(s.years is None for s in smeared_result)
    assert unrelated_result.years == 3


# ── _extract_skills_merged ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_extract_skills_merged_combines_scan_and_llm_results() -> None:
    llm = MagicMock(
        chat_json=AsyncMock(
            return_value=ResumeSkillDetails(
                skills=[
                    ResumeSkillDetail(name="SQL", years=5),
                    ResumeSkillDetail(name="Kubernetes", years=2),
                ]
            )
        )
    )
    chunks = [ResumeChunk(id="c_001", section="skills", page=0, text="Python, SQL")]

    with (
        patch(
            "src.worker.resume_tasks.match_skills_in_text",
            MagicMock(return_value=["Python", "SQL"]),
        ),
        patch(
            "src.worker.resume_tasks.canonicalize_skill_names",
            MagicMock(side_effect=lambda names: list(names)),
        ),
        patch(
            "src.worker.resume_tasks.load_prompt",
            return_value=_fake_prompt("resume_skills_v2"),
        ),
    ):
        merged, _reason = await _extract_skills_merged(llm, chunks, "res-1")

    names = {s.name for s in merged}
    assert names == {"Python", "SQL", "Kubernetes"}
    sql_skill = next(s for s in merged if s.name == "SQL")
    assert sql_skill.years == 5  # LLM value fills in the scan-only match
    python_skill = next(s for s in merged if s.name == "Python")
    assert python_skill.years is None  # scan-only, no LLM mention


@pytest.mark.asyncio
async def test_extract_skills_merged_llm_failure_is_non_fatal() -> None:
    llm = MagicMock(chat_json=AsyncMock(side_effect=LLMOutputInvalidError("bad json")))
    chunks = [ResumeChunk(id="c_001", section="skills", page=0, text="Python, SQL")]

    with (
        patch(
            "src.worker.resume_tasks.match_skills_in_text",
            MagicMock(return_value=["Python", "SQL"]),
        ),
        patch(
            "src.worker.resume_tasks.canonicalize_skill_names",
            MagicMock(side_effect=lambda names: list(names)),
        ),
        patch(
            "src.worker.resume_tasks.load_prompt",
            return_value=_fake_prompt("resume_skills_v2"),
        ),
    ):
        merged, _reason = await _extract_skills_merged(llm, chunks, "res-1")

    assert {s.name for s in merged} == {"Python", "SQL"}


@pytest.mark.asyncio
async def test_extract_skills_merged_first_non_null_years_wins_on_collision() -> None:
    llm = MagicMock(
        chat_json=AsyncMock(
            return_value=ResumeSkillDetails(
                skills=[
                    ResumeSkillDetail(name="SQL", years=5),
                    ResumeSkillDetail(name="SQL", years=10),
                ]
            )
        )
    )
    chunks = [ResumeChunk(id="c_001", section="skills", page=0, text="SQL expert")]

    with (
        patch(
            "src.worker.resume_tasks.match_skills_in_text", MagicMock(return_value=[])
        ),
        patch(
            "src.worker.resume_tasks.canonicalize_skill_names",
            MagicMock(side_effect=lambda names: list(names)),
        ),
        patch(
            "src.worker.resume_tasks.load_prompt",
            return_value=_fake_prompt("resume_skills_v2"),
        ),
    ):
        merged, _reason = await _extract_skills_merged(llm, chunks, "res-1")

    assert len(merged) == 1
    assert merged[0].years == 5


@pytest.mark.asyncio
async def test_extract_skills_merged_is_capped_at_400() -> None:
    # 500 deterministic-scan hits: one REAL skill sits well inside the new
    # 400 cap (index 10) and must survive; another sits past it (index 450)
    # and must still be truncated -- pins that the cap is enforced, not
    # merely raised to "unbounded".
    scan_names = [f"skill_{i:03d}" for i in range(500)]
    scan_names[10] = "python"
    scan_names[450] = "kubernetes"
    llm = MagicMock(chat_json=AsyncMock(return_value=ResumeSkillDetails(skills=[])))
    chunks = [ResumeChunk(id="c_001", section="skills", page=0, text="lots of skills")]

    with (
        patch(
            "src.worker.resume_tasks.match_skills_in_text",
            MagicMock(return_value=scan_names),
        ),
        patch(
            "src.worker.resume_tasks.canonicalize_skill_names",
            MagicMock(side_effect=lambda names: list(names)),
        ),
        patch(
            "src.worker.resume_tasks.load_prompt",
            return_value=_fake_prompt("resume_skills_v2"),
        ),
    ):
        merged, _reason = await _extract_skills_merged(llm, chunks, "res-1")

    names = {s.name for s in merged}
    assert len(merged) == 400
    assert "python" in names
    assert "kubernetes" not in names


@pytest.mark.asyncio
async def test_extract_skills_merged_trailing_technical_skills_survive_admin_overflow() -> (  # noqa: E501
    None
):
    """Reproduces the merge-blocking finding: an administrative-prose-heavy
    résumé whose deterministic scan alone exceeds the OLD 80-skill cap, with
    the real technical skills (the ones a must-have skill match depends on)
    appearing LAST -- the common layout where ``TECHNICAL SKILLS`` is the
    final section. Under the pre-fix code (``det`` ordered before the LLM
    half, capped at 80) every one of these technical names was truncated
    away and never reached ``ResumeParsed.skills``. Both halves of the fix
    are required: reordering alone doesn't save ``airflow``/``docker``/
    ``kubernetes``/``rest_api_design`` (the LLM never saw them either --
    they're scanner-only), so the cap must also rise above the 106-entry
    merge produced here.
    """
    admin_filler = [f"admin_skill_{i:03d}" for i in range(100)]
    trailing_technical = [
        "python",
        "postgresql",
        "airflow",
        "docker",
        "kubernetes",
        "rest_api_design",
    ]
    scan_names = admin_filler + trailing_technical  # det: 106 total

    # The LLM half independently found the two most prominent technologies,
    # WITH years -- this is the richer half and must never be the one that
    # gets truncated.
    llm = MagicMock(
        chat_json=AsyncMock(
            return_value=ResumeSkillDetails(
                skills=[
                    ResumeSkillDetail(name="python", years=5, last_used_year=2025),
                    ResumeSkillDetail(name="postgresql", years=3, last_used_year=2024),
                ]
            )
        )
    )
    chunks = [
        ResumeChunk(id="c_001", section="skills", page=0, text="administrative resume")
    ]

    with (
        patch(
            "src.worker.resume_tasks.match_skills_in_text",
            MagicMock(return_value=scan_names),
        ),
        patch(
            "src.worker.resume_tasks.canonicalize_skill_names",
            MagicMock(side_effect=lambda names: list(names)),
        ),
        patch(
            "src.worker.resume_tasks.load_prompt",
            return_value=_fake_prompt("resume_skills_v2"),
        ),
    ):
        merged, _reason = await _extract_skills_merged(llm, chunks, "res-1")

    names = {s.name for s in merged}
    for tech in trailing_technical:
        assert tech in names, f"{tech} truncated away by the merge cap"

    by_name = {s.name: s for s in merged}
    assert by_name["python"].years == 5
    assert by_name["python"].last_used_year == 2025
    assert by_name["postgresql"].years == 3
    assert by_name["postgresql"].last_used_year == 2024


# ── A2 recurrence guards (round-1 remediation gaps) ──────────────────────
#
# The merge-blocking reviewer approved this branch, then proved that round-1's
# fix for its own headline defect -- a `_MAX_SKILLS` cap that can silently
# truncate the deterministic scan, a TRUSTED source, as the curated vocabulary
# grows -- pinned nothing: three mutations that reintroduce it survive the
# whole suite. These three guards close that gap; `aliases.yaml`'s header
# plans a further vocabulary pass that would otherwise silently re-arm it.


def test_max_skills_exceeds_curated_vocabulary_size() -> None:
    """`_MAX_SKILLS`'s own comment (resume_tasks.py:93-104) says it must
    leave headroom above the curated vocabulary size -- nothing enforced
    that. A cap at or below the vocabulary size does not merely cap total
    output: the deterministic scan (`det`, above) can only ever emit a term
    it found VERBATIM in the résumé text, so truncating it truncates skills
    this pipeline already trusts, not LLM noise.
    """
    vocab_size = len(set(_alias_table().values()))
    assert _MAX_SKILLS > vocab_size, (
        f"_MAX_SKILLS={_MAX_SKILLS} does not exceed the curated vocabulary's "
        f"{vocab_size} distinct canonicals. A cap at or below the vocabulary "
        "size silently truncates skills the deterministic scan verbatim-"
        "matched in the résumé text -- a TRUSTED source -- not merely "
        "'extra' output. Raise _MAX_SKILLS (and its paired "
        "ResumeParsed.skills max_length, see the sibling test) above the "
        "new vocabulary size."
    )


def test_max_skills_matches_resume_parsed_skills_schema_cap() -> None:
    """`ResumeParsed.skills`'s own docstring (resumes.py:190-195) says its
    `max_length` and `_MAX_SKILLS` here "MUST stay in sync" -- nothing
    enforced that either. A live desync (schema left low while this merge
    cap is raised, or vice versa) is not cosmetic: `_extract_skills_merged`
    can then legitimately build a skills list the schema rejects, and
    `ResumeParsed.model_validate` raises `ValidationError` for an
    otherwise-good résumé.
    """
    field_info = ResumeParsed.model_fields["skills"]
    max_lengths = [m.max_length for m in field_info.metadata if isinstance(m, MaxLen)]
    assert max_lengths, "ResumeParsed.skills has no MaxLen constraint at all"
    assert max_lengths[0] == _MAX_SKILLS, (
        f"ResumeParsed.skills max_length={max_lengths[0]} != "
        f"_MAX_SKILLS={_MAX_SKILLS} in worker/resume_tasks.py -- these two "
        "caps MUST stay in sync (resumes.py:190-195). A desync lets the "
        "merge build a skills list the schema then rejects with "
        "ValidationError, failing the parse of an otherwise-good résumé."
    )


@pytest.mark.asyncio
async def test_extract_skills_merged_llm_skill_survives_when_scan_fills_the_cap() -> (
    None
):
    """Pins the LLM-first merge order (resume_tasks.py:253: LLM names go
    FIRST, deterministic scan second). Reverting to
    ``[*det, *[d.name for d in llm_details]]`` passes the whole suite today,
    because neither existing cap test constructs a case where order
    genuinely decides the outcome: the admin-overflow test above merges 106
    distinct names against a 400 cap (nothing is truncated), and the
    straight cap test supplies zero LLM skills. Here the deterministic scan
    ALONE produces exactly ``_MAX_SKILLS`` distinct filler hits that never
    include the LLM's skill, so which half is ordered first is the only
    thing standing between the LLM skill -- and its ``years``/
    ``last_used_year``, unrecoverable from a name-only scan -- surviving or
    being silently discarded.
    """
    scan_names = [f"filler_skill_{i:03d}" for i in range(_MAX_SKILLS)]
    llm = MagicMock(
        chat_json=AsyncMock(
            return_value=ResumeSkillDetails(
                skills=[ResumeSkillDetail(name="python", years=7, last_used_year=2023)]
            )
        )
    )
    chunks = [ResumeChunk(id="c_001", section="skills", page=0, text="lots of skills")]

    with (
        patch(
            "src.worker.resume_tasks.match_skills_in_text",
            MagicMock(return_value=scan_names),
        ),
        patch(
            "src.worker.resume_tasks.canonicalize_skill_names",
            MagicMock(side_effect=lambda names: list(names)),
        ),
        patch(
            "src.worker.resume_tasks.load_prompt",
            return_value=_fake_prompt("resume_skills_v2"),
        ),
    ):
        merged, _reason = await _extract_skills_merged(llm, chunks, "res-1")

    assert len(merged) == _MAX_SKILLS
    by_name = {s.name: s for s in merged}
    assert "python" in by_name, (
        "the LLM-extracted skill was truncated away when a deterministic-"
        "scan-only filler set alone reached _MAX_SKILLS -- the merge must "
        "put the LLM half FIRST (resume_tasks.py:253) so the richer half "
        "(years/last_used_year the scan can never recover) is never the "
        "half a cap discards"
    )
    assert by_name["python"].years == 7
    assert by_name["python"].last_used_year == 2023


# ── ADR-008: `_extract_skills_merged` is shape-only junk filtering now ────
#
# Rounds 2-5's raw-name person-shape+vocab reject at THIS layer is gone (see
# ``src.pipeline.skills_graph``'s module docstring) — `_extract_skills_merged`
# still calls `skills_graph.reject_reason_for_skill_name` on each LLM detail's
# RAW name, but that function only catches email/phone-shape and length/
# token-cap junk now. A skill name that IS the candidate's own identity
# (e.g. "Casey Rivera") is a DELIBERATE pass-through at this layer: the
# load-bearing privacy control moved downstream to graph-projection hashing
# (``skills_graph.resume_skill_canonical_key``) — see
# ``test_graph_projection_pii_sweep.py`` for the proof that it still never
# reaches the graph as cleartext or an embedding.


def _skills_details_chunks() -> list[ResumeChunk]:
    return [
        ResumeChunk(id="c_001", section="summary", page=0, text="Backend engineer.")
    ]


@pytest.mark.asyncio
async def test_extract_skills_merged_still_drops_email_shaped_raw_names() -> None:
    """The one row of the OLD reproduction table that survives this layer
    unchanged: an email address is still junk-shaped and still dropped
    before canonicalisation (canonicalisation strips "@", which would
    otherwise defeat the email check downstream too)."""
    llm = MagicMock(
        chat_json=AsyncMock(
            return_value=ResumeSkillDetails(
                skills=[
                    ResumeSkillDetail(name="python"),
                    ResumeSkillDetail(name="casey.rivera@example.test"),
                ]
            )
        )
    )
    with patch(
        "src.worker.resume_tasks.load_prompt",
        return_value=_fake_prompt("resume_skills_v2"),
    ):
        merged, _reason = await _extract_skills_merged(
            llm, _skills_details_chunks(), "res-1"
        )

    names = {s.name for s in merged}
    assert "python" in names
    assert "casey.rivera@example.test" not in names


@pytest.mark.parametrize(
    "raw_name",
    [
        "Casey Rivera",
        "Rivera, Casey",
        "Casey M. Rivera",
        "Casey-Rivera",
        "Rivera",
        "John Smith",
    ],
)
@pytest.mark.asyncio
async def test_extract_skills_merged_no_longer_rejects_person_shaped_raw_names(
    raw_name: str,
) -> None:
    """ADR-008: none of the OLD round-2/3 person-shape reproduction rows are
    rejected at THIS layer any more — a documented, deliberate behaviour
    change. The candidate's own identity surviving into
    ``ResumeParsed.skills``/the outbox is the ACCEPTED cost of moving the
    real privacy control to graph-projection hashing (it never survives
    INTO the graph, which is what actually matters — see the projection
    tests)."""
    llm = MagicMock(
        chat_json=AsyncMock(
            return_value=ResumeSkillDetails(
                skills=[
                    ResumeSkillDetail(name="python"),
                    ResumeSkillDetail(name=raw_name),
                ]
            )
        )
    )
    with patch(
        "src.worker.resume_tasks.load_prompt",
        return_value=_fake_prompt("resume_skills_v2"),
    ):
        merged, _reason = await _extract_skills_merged(
            llm, _skills_details_chunks(), "res-1"
        )

    names = {s.name for s in merged}
    assert "python" in names
    # Survived (not silently dropped) — a second, distinct canonicalised
    # name landed alongside "python".
    assert len(names) == 2


@pytest.mark.asyncio
async def test_extract_skills_merged_pii_shaped_drop_is_logged_as_count_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The email-shaped drop (still the only row this layer rejects) is
    still logged as a count only, never the value."""
    import logging

    llm = MagicMock(
        chat_json=AsyncMock(
            return_value=ResumeSkillDetails(
                skills=[ResumeSkillDetail(name="casey.rivera@example.test")]
            )
        )
    )
    with (
        caplog.at_level(logging.WARNING),
        patch(
            "src.worker.resume_tasks.load_prompt",
            return_value=_fake_prompt("resume_skills_v2"),
        ),
    ):
        await _extract_skills_merged(llm, _skills_details_chunks(), "res-1")

    all_text = "\n".join(r.getMessage() for r in caplog.records)
    assert "casey.rivera@example.test" not in all_text.lower()
    assert "count=1" in all_text


# ── Decision B: the shape+vocab reject must not eat legitimate skills ─────


@pytest.mark.parametrize("raw_name", ["Kafka", "Django", "Kubernetes"])
@pytest.mark.asyncio
async def test_extract_skills_merged_keeps_vocab_known_skill(raw_name: str) -> None:
    """A vocab-known skill (single Title-Case token) must survive."""
    llm = MagicMock(
        chat_json=AsyncMock(
            return_value=ResumeSkillDetails(skills=[ResumeSkillDetail(name=raw_name)])
        )
    )
    with patch(
        "src.worker.resume_tasks.load_prompt",
        return_value=_fake_prompt("resume_skills_v2"),
    ):
        merged, _reason = await _extract_skills_merged(
            llm, _skills_details_chunks(), "res-1"
        )
    assert raw_name.lower() in {s.name for s in merged}


@pytest.mark.asyncio
async def test_extract_skills_merged_keeps_multi_token_proper_noun_vocab_skill() -> (
    None
):
    """A multi-token proper-noun skill that IS in the vocabulary (an alias
    of 'gcp') must survive despite being person-name-shaped (3 Title-Case
    tokens)."""
    llm = MagicMock(
        chat_json=AsyncMock(
            return_value=ResumeSkillDetails(
                skills=[ResumeSkillDetail(name="Google Cloud Platform")]
            )
        )
    )
    with patch(
        "src.worker.resume_tasks.load_prompt",
        return_value=_fake_prompt("resume_skills_v2"),
    ):
        merged, _reason = await _extract_skills_merged(
            llm, _skills_details_chunks(), "res-1"
        )
    assert "gcp" in {s.name for s in merged}


@pytest.mark.parametrize("raw_name", ["C++", ".NET", "IPv6", "ISO 27001"])
@pytest.mark.asyncio
async def test_extract_skills_merged_keeps_technical_marker_shaped_skill(
    raw_name: str,
) -> None:
    """A digit/`+`/`#`/leading-`.` technical marker is never person-name
    shaped, so it survives regardless of vocab membership."""
    llm = MagicMock(
        chat_json=AsyncMock(
            return_value=ResumeSkillDetails(skills=[ResumeSkillDetail(name=raw_name)])
        )
    )
    with patch(
        "src.worker.resume_tasks.load_prompt",
        return_value=_fake_prompt("resume_skills_v2"),
    ):
        merged, _reason = await _extract_skills_merged(
            llm, _skills_details_chunks(), "res-1"
        )
    names = {s.name for s in merged}
    assert any(raw_name.lower() in n or n in raw_name.lower() for n in names), names


@pytest.mark.asyncio
async def test_extract_skills_merged_keeps_seven_token_certification_name() -> None:
    """Decision B / widened token cap (6 -> 8): a legitimate 7-token
    certification name must survive — the OLD cap of 6 would have dropped
    it."""
    cert_name = "cisco certified network associate security specialist exam"
    assert len(cert_name.split()) == 7
    assert len(cert_name) <= 60
    llm = MagicMock(
        chat_json=AsyncMock(
            return_value=ResumeSkillDetails(skills=[ResumeSkillDetail(name=cert_name)])
        )
    )
    with patch(
        "src.worker.resume_tasks.load_prompt",
        return_value=_fake_prompt("resume_skills_v2"),
    ):
        merged, _reason = await _extract_skills_merged(
            llm, _skills_details_chunks(), "res-1"
        )
    assert cert_name in {s.name for s in merged}


# ── FU-7 §4 / ADR-030: _extract_skills_merged degradation-reason signal ────
#
# Skills extraction "degraded" means specifically: the `resume_skills_v2`
# LLM sub-call raised `LLMOutputInvalidError` and the deterministic
# keyword-scan floor is all that landed. `_extract_skills_merged` must now
# return `(skills, degradation_reason)` — `degradation_reason` is `None` on
# a clean LLM call and a non-None, PII-free string when the LLM call failed.


@pytest.mark.asyncio
async def test_extract_skills_merged_returns_none_reason_on_llm_success() -> None:
    llm = MagicMock(
        chat_json=AsyncMock(
            return_value=ResumeSkillDetails(skills=[ResumeSkillDetail(name="SQL")])
        )
    )
    chunks = [ResumeChunk(id="c_001", section="skills", page=0, text="SQL expert")]

    with (
        patch(
            "src.worker.resume_tasks.match_skills_in_text", MagicMock(return_value=[])
        ),
        patch(
            "src.worker.resume_tasks.canonicalize_skill_names",
            MagicMock(side_effect=lambda names: list(names)),
        ),
        patch(
            "src.worker.resume_tasks.load_prompt",
            return_value=_fake_prompt("resume_skills_v2"),
        ),
    ):
        merged, reason = await _extract_skills_merged(llm, chunks, "res-1")

    assert reason is None
    assert {s.name for s in merged} == {"SQL"}


@pytest.mark.asyncio
async def test_extract_skills_merged_returns_a_degradation_reason_on_llm_output_invalid() -> (  # noqa: E501
    None
):
    """Mirrors ``test_extract_skills_merged_llm_failure_is_non_fatal`` — same
    stub of ``llm.chat_json`` raising ``LLMOutputInvalidError`` — but pins the
    NEW second return value instead of only the skills list."""
    llm = MagicMock(chat_json=AsyncMock(side_effect=LLMOutputInvalidError("bad json")))
    chunks = [ResumeChunk(id="c_001", section="skills", page=0, text="Python, SQL")]

    with (
        patch(
            "src.worker.resume_tasks.match_skills_in_text",
            MagicMock(return_value=["Python", "SQL"]),
        ),
        patch(
            "src.worker.resume_tasks.canonicalize_skill_names",
            MagicMock(side_effect=lambda names: list(names)),
        ),
        patch(
            "src.worker.resume_tasks.load_prompt",
            return_value=_fake_prompt("resume_skills_v2"),
        ),
    ):
        merged, reason = await _extract_skills_merged(llm, chunks, "res-1")

    assert {s.name for s in merged} == {"Python", "SQL"}  # deterministic floor
    assert reason is not None
    assert isinstance(reason, str)
    assert len(reason) <= 200  # must fit ResumeParsed.degradation_reason's cap


@pytest.mark.asyncio
async def test_extract_skills_merged_degradation_reason_is_pii_free() -> None:
    """The reason must never echo the LLM exception's ``str()`` (which can
    include response content) — a fixed, PII-free message only."""
    llm = MagicMock(
        chat_json=AsyncMock(
            side_effect=LLMOutputInvalidError(
                "raw content: Jane Doe jane@example.com 555-0100"
            )
        )
    )
    chunks = [ResumeChunk(id="c_001", section="skills", page=0, text="Python")]

    with (
        patch(
            "src.worker.resume_tasks.match_skills_in_text",
            MagicMock(return_value=["Python"]),
        ),
        patch(
            "src.worker.resume_tasks.canonicalize_skill_names",
            MagicMock(side_effect=lambda names: list(names)),
        ),
        patch(
            "src.worker.resume_tasks.load_prompt",
            return_value=_fake_prompt("resume_skills_v2"),
        ),
    ):
        _merged, reason = await _extract_skills_merged(llm, chunks, "res-1")

    assert reason is not None
    assert "jane" not in reason.lower()
    assert "555-0100" not in reason
    assert "example.com" not in reason.lower()


# ── skill-family classifier integration (ROADMAP A2, Phase 3.3 slice 1) ──
#
# Design: classification runs INSIDE `_extract_skills_merged`, after
# `canonicalize_skill_names`, on the out-of-vocab subset only (the LLM is
# already being called here at parse time, with no drain deadline --
# ADR-044). `skill_classifier.classify_families` is mocked in
# every test below -- its OWN contract (batching, the family cap, the
# unknown-family drop, the no-confident-answer omission, PII-safe failure)
# is pinned independently in `test_skill_classifier.py`; these tests pin
# ONLY `_extract_skills_merged`'s integration with it: which names it sends,
# how the result attaches to `ResumeSkill.categories`, and the failure
# posture at THIS call site.
#
# `canonicalize_skill_names` is deliberately left UNPATCHED (unlike most
# tests above) so "python" really is in-vocab and "mri methods" really is
# out-of-vocab for `unclassified_names`' real predicate -- these tests would
# be meaningless against a stubbed identity canonicaliser.


@pytest.mark.asyncio
async def test_extract_skills_merged_sends_only_the_out_of_vocab_subset_to_the_classifier() -> (  # noqa: E501
    None
):
    llm = MagicMock(
        chat_json=AsyncMock(
            return_value=ResumeSkillDetails(
                skills=[
                    ResumeSkillDetail(name="python"),  # in-vocab
                    ResumeSkillDetail(name="mri methods"),  # out of vocab
                ]
            )
        )
    )
    chunks = [ResumeChunk(id="c_001", section="skills", page=0, text="irrelevant")]

    with (
        patch(
            "src.worker.resume_tasks.match_skills_in_text", MagicMock(return_value=[])
        ),
        patch(
            "src.worker.resume_tasks.load_prompt",
            return_value=_fake_prompt("resume_skills_v2"),
        ),
        patch(
            "src.worker.resume_tasks.skill_classifier.classify_families",
            new_callable=AsyncMock,
            return_value={},
        ) as classify,
    ):
        await _extract_skills_merged(llm, chunks, "res-1")

    classify.assert_awaited_once()
    call = classify.await_args
    sent_names = next(
        a for a in (*call.args, *call.kwargs.values()) if isinstance(a, list)
    )
    assert "mri methods" in sent_names
    assert "python" not in sent_names


@pytest.mark.asyncio
async def test_extract_skills_merged_attaches_classifier_categories_to_the_skill() -> (
    None
):
    llm = MagicMock(
        chat_json=AsyncMock(
            return_value=ResumeSkillDetails(
                skills=[ResumeSkillDetail(name="mri methods")]
            )
        )
    )
    chunks = [ResumeChunk(id="c_001", section="skills", page=0, text="irrelevant")]

    with (
        patch(
            "src.worker.resume_tasks.match_skills_in_text", MagicMock(return_value=[])
        ),
        patch(
            "src.worker.resume_tasks.load_prompt",
            return_value=_fake_prompt("resume_skills_v2"),
        ),
        patch(
            "src.worker.resume_tasks.skill_classifier.classify_families",
            new_callable=AsyncMock,
            return_value={"mri methods": ["health_wellness"]},
        ),
    ):
        merged, _reason = await _extract_skills_merged(llm, chunks, "res-1")

    skill = next(s for s in merged if s.name == "mri methods")
    assert skill.categories == ["health_wellness"]


@pytest.mark.asyncio
async def test_extract_skills_merged_vocab_skill_never_gets_classifier_categories() -> (
    None
):
    """M8 mutation-review fix (2026-08-19): the ORIGINAL version of this
    test offered only ``python`` (in-vocab), so ``unclassified_names``
    returned ``[]``, ``classify_families`` was never even awaited, and the
    stubbed ``{"python": ["backend"]}`` return value was dead code -- the
    assertion held whether or not the ``c in unclassified_set`` attach guard
    (``resume_tasks.py``) existed, making the test vacuous against deleting
    it. This version ALSO offers ``mri methods`` (genuinely out of vocab),
    so the classifier really is awaited, and the stub answers for BOTH
    names -- proving the guard actually discriminates: the out-of-vocab
    name's category is attached, the in-vocab name's is not, from the SAME
    real classifier call."""
    llm = MagicMock(
        chat_json=AsyncMock(
            return_value=ResumeSkillDetails(
                skills=[
                    ResumeSkillDetail(name="python"),  # in-vocab
                    ResumeSkillDetail(name="mri methods"),  # out of vocab
                ]
            )
        )
    )
    chunks = [ResumeChunk(id="c_001", section="skills", page=0, text="irrelevant")]

    with (
        patch(
            "src.worker.resume_tasks.match_skills_in_text", MagicMock(return_value=[])
        ),
        patch(
            "src.worker.resume_tasks.load_prompt",
            return_value=_fake_prompt("resume_skills_v2"),
        ),
        patch(
            "src.worker.resume_tasks.skill_classifier.classify_families",
            new_callable=AsyncMock,
            # A buggy/future classifier answering for a vocab name it was
            # NEVER SENT (`python` -- see the dedicated
            # ``..._sends_only_the_out_of_vocab_subset...`` test above for
            # that "never sent" guarantee) must still never reach
            # `python`'s `ResumeSkill.categories` -- this proves the attach
            # side, with a call that genuinely happens.
            return_value={"python": ["backend"], "mri methods": ["health_wellness"]},
        ) as classify,
    ):
        merged, _reason = await _extract_skills_merged(llm, chunks, "res-1")

    classify.assert_awaited_once()  # the call this test needs to be real

    vocab_skill = next(s for s in merged if s.name == "python")
    assert vocab_skill.categories is None

    out_of_vocab_skill = next(s for s in merged if s.name == "mri methods")
    assert out_of_vocab_skill.categories == ["health_wellness"]


@pytest.mark.asyncio
async def test_extract_skills_merged_no_classifier_answer_leaves_categories_none() -> (
    None
):
    """Conservative default: no confident answer -> `categories` stays at
    its schema default (``None``, not ``[]``) -- behaviourally identical to
    a résumé parsed before this feature existed."""
    llm = MagicMock(
        chat_json=AsyncMock(
            return_value=ResumeSkillDetails(
                skills=[ResumeSkillDetail(name="mri methods")]
            )
        )
    )
    chunks = [ResumeChunk(id="c_001", section="skills", page=0, text="irrelevant")]

    with (
        patch(
            "src.worker.resume_tasks.match_skills_in_text", MagicMock(return_value=[])
        ),
        patch(
            "src.worker.resume_tasks.load_prompt",
            return_value=_fake_prompt("resume_skills_v2"),
        ),
        patch(
            "src.worker.resume_tasks.skill_classifier.classify_families",
            new_callable=AsyncMock,
            return_value={},
        ),
    ):
        merged, _reason = await _extract_skills_merged(llm, chunks, "res-1")

    skill = next(s for s in merged if s.name == "mri methods")
    assert skill.categories is None


@pytest.mark.asyncio
async def test_extract_skills_merged_classifier_failure_is_non_fatal() -> None:
    """Mirrors `_extract_skills_merged`'s own failure posture for the skills
    LLM call: the classifier is best-effort. Its failure must not fail the
    parse, must not set the skills-extraction degradation reason (a
    DIFFERENT, unrelated signal), and every skill is still extracted."""
    llm = MagicMock(
        chat_json=AsyncMock(
            return_value=ResumeSkillDetails(
                skills=[ResumeSkillDetail(name="mri methods")]
            )
        )
    )
    chunks = [ResumeChunk(id="c_001", section="skills", page=0, text="irrelevant")]

    with (
        patch(
            "src.worker.resume_tasks.match_skills_in_text", MagicMock(return_value=[])
        ),
        patch(
            "src.worker.resume_tasks.load_prompt",
            return_value=_fake_prompt("resume_skills_v2"),
        ),
        patch(
            "src.worker.resume_tasks.skill_classifier.classify_families",
            new_callable=AsyncMock,
            side_effect=RuntimeError("ollama down"),
        ),
    ):
        merged, reason = await _extract_skills_merged(llm, chunks, "res-1")

    assert reason is None  # not the skills-extraction degradation signal
    skill = next(s for s in merged if s.name == "mri methods")
    assert skill.categories is None


@pytest.mark.asyncio
async def test_extract_skills_merged_classifier_failure_logs_no_skill_name(
    caplog: pytest.LogCaptureFixture,
) -> None:
    llm = MagicMock(
        chat_json=AsyncMock(
            return_value=ResumeSkillDetails(
                skills=[
                    ResumeSkillDetail(name="a very distinctive sentinel skill xyz123")
                ]
            )
        )
    )
    chunks = [ResumeChunk(id="c_001", section="skills", page=0, text="irrelevant")]

    with (
        patch(
            "src.worker.resume_tasks.match_skills_in_text", MagicMock(return_value=[])
        ),
        patch(
            "src.worker.resume_tasks.load_prompt",
            return_value=_fake_prompt("resume_skills_v2"),
        ),
        patch(
            "src.worker.resume_tasks.skill_classifier.classify_families",
            new_callable=AsyncMock,
            side_effect=RuntimeError("ollama down"),
        ),
    ):
        with caplog.at_level(logging.DEBUG):
            merged, _reason = await _extract_skills_merged(llm, chunks, "res-1")

    assert any("sentinel" in s.name for s in merged)
    assert "sentinel" not in caplog.text.lower()
    assert "xyz123" not in caplog.text


@pytest.mark.asyncio
async def test_outbox_payload_carries_classifier_categories_through_to_projection() -> (
    None
):
    """The categories a parse-time classifier assigned must ride the
    EXISTING outbox payload unchanged -- `_OUTBOX_PARSED_EXCLUDE` excludes
    nothing under `skills` (ADR-044's whole "strictly additive"
    claim depends on this)."""
    resume_id = uuid4()
    job_id = uuid4()
    conn = _make_conn(
        _meta_row(job_id=job_id, status="uploaded", blob_key="resumes/abc.pdf")
    )
    blob_store = MagicMock(get=AsyncMock(return_value=b"pdf-bytes"))
    core = ResumeCore(
        candidate=CandidateInfo(name="Ada Lovelace"),
        summary="Backend engineer.",
    )
    llm = MagicMock(chat_json=AsyncMock(return_value=core))
    chunks = [ResumeChunk(id="c_001", section="skills", page=0, text="MRI methods")]
    embedder = MagicMock(embed=AsyncMock(side_effect=[[[0.1] * 8], [[0.2] * 8]]))
    ctx = _make_ctx(conn, llm=llm, blob_store=blob_store, embedder=embedder)
    merged_skills = [ResumeSkill(name="mri methods", categories=["health_wellness"])]

    with (
        patch(
            "src.worker.resume_tasks.extract_text", MagicMock(return_value=MagicMock())
        ),
        patch("src.worker.resume_tasks.chunk_resume", MagicMock(return_value=chunks)),
        patch(
            "src.worker.resume_tasks._extract_skills_merged",
            new_callable=AsyncMock,
            return_value=(merged_skills, None),
        ),
        patch(
            "src.worker.resume_tasks.resume_service.encrypt_pii_via_session",
            new_callable=AsyncMock,
            return_value=(b"enc-name", b"enc-email", b"enc-phone", "hash123"),
        ),
        patch(
            "src.worker.resume_tasks.resume_service.record_parsed",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "src.worker.resume_tasks.outbox_service.enqueue_outbox",
            new_callable=AsyncMock,
        ) as enqueue,
    ):
        result = await parse_resume(ctx, str(resume_id))

    assert result == "parsed"
    payload = enqueue.await_args.kwargs["payload"]
    skill_row = next(
        s for s in payload["parsed"]["skills"] if s["name"] == "mri methods"
    )
    assert skill_row["categories"] == ["health_wellness"]


@pytest.mark.asyncio
async def test_classifier_category_survives_redaction_into_an_in_vocab_name() -> None:
    """M9 mutation-review reachability proof (2026-08-19): pins that the
    "landmine" state the projection write guard (``resume_tasks.py``,
    ``if canonical.startswith(skills_graph._HASH_KEY_PREFIX)``) exists to
    stop is genuinely REACHABLE through the real parse-time call chain --
    not merely a hand-built payload. ``_extract_skills_merged`` runs BEFORE
    ``_redact_skill_names_pii`` (see ``parse_resume``'s own call order), so
    a compound name like "Casey Rivera Python" is OUT of vocab when the
    classifier sees it (attracting a model-inferred family), and only AFTER
    that is it rewritten by redaction -- which strips the candidate's own
    name and can leave a bare in-vocab remainder ("python"). The classifier
    category rides along on ``model_copy`` untouched, so the skill that
    reaches the projection layer is name="python" (curated/in-vocab) with a
    model-inferred ``categories`` still attached -- exactly the shape the
    projection guard exists to keep off the curated ``Skill`` node. This
    test does not touch the guard itself (that is
    ``test_curated_in_vocab_skill_categories_are_not_overridden_by_a_payload_value``
    in the Neo4j integration suite); it only proves the input to that guard
    is real, not synthetic.
    """
    from src.pipeline import skills_graph

    llm = MagicMock(
        chat_json=AsyncMock(
            return_value=ResumeSkillDetails(
                skills=[ResumeSkillDetail(name="Casey Rivera Python")]
            )
        )
    )
    chunks = [ResumeChunk(id="c_001", section="skills", page=0, text="irrelevant")]

    with (
        patch(
            "src.worker.resume_tasks.match_skills_in_text", MagicMock(return_value=[])
        ),
        patch(
            "src.worker.resume_tasks.load_prompt",
            return_value=_fake_prompt("resume_skills_v2"),
        ),
        patch(
            "src.worker.resume_tasks.skill_classifier.classify_families",
            new_callable=AsyncMock,
            return_value={"casey rivera python": ["backend"]},
        ) as classify,
    ):
        merged, _reason = await _extract_skills_merged(llm, chunks, "res-1")

    classify.assert_awaited_once()
    pre_redaction = next(s for s in merged if s.name == "casey rivera python")
    assert pre_redaction.categories == ["backend"], (
        "the classifier must have attached a category to the compound "
        "out-of-vocab name BEFORE redaction runs"
    )
    assert skills_graph._canonical_key_for_normalised("casey rivera python").startswith(
        skills_graph._HASH_KEY_PREFIX
    ), (
        "the compound name must genuinely be out of vocab pre-redaction, "
        "or this test proves nothing about the real gap"
    )

    redacted = _redact_skill_names_pii(
        merged, CandidateInfo(name="Casey Rivera"), "res-1"
    )

    landmine_skill = next(s for s in redacted if s.categories is not None)
    assert landmine_skill.name == "python", (
        "redaction must have rewritten the compound name down to the bare "
        "in-vocab remainder for this to be the real landmine shape"
    )
    assert landmine_skill.categories == ["backend"], (
        "the classifier's category must still be attached to the renamed "
        "skill -- model_copy(update={'name': ...}) does not clear it"
    )
    assert not skills_graph._canonical_key_for_normalised("python").startswith(
        skills_graph._HASH_KEY_PREFIX
    ), (
        "the post-redaction name must genuinely resolve in-vocab/cleartext "
        "at projection, or this is not the scenario the projection write "
        "guard (resume_tasks.py) exists to stop"
    )


# ── _parse_cover_letter ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_parse_cover_letter_absent_returns_no_chunks_no_extraction() -> None:
    conn = MagicMock(name="conn")
    llm = MagicMock(chat_json=AsyncMock())
    blob_store = MagicMock(get=AsyncMock())
    meta = _meta_row(job_id=uuid4())  # no cover letter fields set

    chunks, parsed = await _parse_cover_letter(conn, llm, blob_store, meta, uuid4())

    assert chunks == []
    assert parsed is None
    blob_store.get.assert_not_called()
    llm.chat_json.assert_not_called()


@pytest.mark.asyncio
async def test_parse_cover_letter_blob_branch_fetches_via_blob_store_get() -> None:
    conn = MagicMock(name="conn")
    cl_parsed = CoverLetterParsed(raw_text="Dear hiring manager...")
    llm = MagicMock(chat_json=AsyncMock(return_value=cl_parsed))
    blob_store = MagicMock(get=AsyncMock(return_value=b"docx-bytes"))
    meta = _meta_row(job_id=uuid4(), cover_letter_blob_key="resumes/abc/cover.docx")
    chunks_sentinel = [ResumeChunk(id="cl_001", section="other", page=0, text="Dear x")]

    with (
        patch(
            "src.worker.resume_tasks.extract_text", MagicMock(return_value=MagicMock())
        ) as extract_text,
        patch(
            "src.worker.resume_tasks.chunk_resume",
            MagicMock(return_value=chunks_sentinel),
        ),
        patch(
            "src.worker.resume_tasks.load_prompt",
            return_value=_fake_prompt("cover_letter_v1"),
        ),
    ):
        chunks, parsed = await _parse_cover_letter(conn, llm, blob_store, meta, uuid4())

    blob_store.get.assert_awaited_once_with("resumes/abc/cover.docx")
    assert blob_store.get_object.called is False  # no MinIO-style call site
    extract_text.assert_called_once()
    assert MIME_DOCX in _flat_call_args(extract_text.call_args)
    assert chunks == chunks_sentinel
    assert parsed == cl_parsed


@pytest.mark.asyncio
async def test_parse_cover_letter_text_branch_decrypts_after_set_pii_key() -> None:
    """SET LOCAL app.pii_key is transaction-scoped — pgp_sym_decrypt fails
    with an unrecognized-configuration-parameter error unless set_pii_key
    ran first, in the same transaction."""
    conn = MagicMock(name="conn")
    llm = MagicMock(chat_json=AsyncMock(return_value=CoverLetterParsed()))
    blob_store = MagicMock(get=AsyncMock())
    meta = _meta_row(job_id=uuid4(), cover_letter_text=b"pgp-ciphertext-bytes")

    manager = MagicMock()
    set_pii_key = AsyncMock()
    decrypt = AsyncMock(return_value="Dear hiring manager, plaintext cover letter.")
    manager.attach_mock(set_pii_key, "set_pii_key")
    manager.attach_mock(decrypt, "decrypt")

    with (
        patch("src.worker.resume_tasks.pii.set_pii_key", set_pii_key),
        patch("src.worker.resume_tasks.pii.decrypt", decrypt),
        patch(
            "src.worker.resume_tasks.extract_text", MagicMock(return_value=MagicMock())
        ),
        patch(
            "src.worker.resume_tasks.chunk_resume",
            MagicMock(
                return_value=[
                    ResumeChunk(id="cl_001", section="other", page=0, text="x")
                ]
            ),
        ),
        patch(
            "src.worker.resume_tasks.load_prompt",
            return_value=_fake_prompt("cover_letter_v1"),
        ),
    ):
        await _parse_cover_letter(conn, llm, blob_store, meta, uuid4())

    blob_store.get.assert_not_called()
    set_pii_key.assert_awaited_once()
    decrypt.assert_awaited_once()
    call_names = [c[0] for c in manager.mock_calls]
    assert call_names.index("set_pii_key") < call_names.index(
        "decrypt"
    ), "set_pii_key must be called BEFORE decrypt (transaction-scoped GUC)"


@pytest.mark.asyncio
async def test_parse_cover_letter_llm_failure_is_non_fatal() -> None:
    """A résumé parse must not fail because its cover letter's LLM call
    failed — the chunks are preserved (for evidence citation) but the
    extraction comes back as an EMPTY ``CoverLetterParsed()``, never
    ``None`` and never a propagated exception."""
    conn = MagicMock(name="conn")
    llm = MagicMock(chat_json=AsyncMock(side_effect=LLMOutputInvalidError("bad json")))
    blob_store = MagicMock(get=AsyncMock(return_value=b"pdf-bytes"))
    meta = _meta_row(job_id=uuid4(), cover_letter_blob_key="resumes/abc/cover.pdf")
    chunks_sentinel = [ResumeChunk(id="cl_001", section="other", page=0, text="Dear x")]

    with (
        patch(
            "src.worker.resume_tasks.extract_text", MagicMock(return_value=MagicMock())
        ),
        patch(
            "src.worker.resume_tasks.chunk_resume",
            MagicMock(return_value=chunks_sentinel),
        ),
        patch(
            "src.worker.resume_tasks.load_prompt",
            return_value=_fake_prompt("cover_letter_v1"),
        ),
    ):
        chunks, parsed = await _parse_cover_letter(conn, llm, blob_store, meta, uuid4())

    assert chunks == chunks_sentinel
    assert parsed is not None
    assert parsed == CoverLetterParsed()


# ── F1 (HIGH, round 3) — PII-equivalent EMBEDDINGS ride the outbox ─────────
#
# `chunk_embs`/`summary_emb` are nomic-embed-text vectors of the TEXT handed
# to the embedder. A header chunk's text (and sometimes the LLM-authored
# summary) carries the candidate's name/email/phone/location VERBATIM, so
# those vectors are PII-equivalent even though round 2 already dropped raw
# chunk TEXT from the outbox `parsed` dict (finding 6) — the embedding is a
# separate top-level payload key that still encodes identity. The fix is a
# pure `_redact_candidate_pii(text, candidate) -> str` helper applied to every
# chunk's text and the composed summary text BEFORE either is embedded.
# `resumes.parsed` (system of record, ADR-007 §6) keeps the FULL, unredacted
# chunk text and summary at rest — only the embedder's INPUT is scrubbed.
#
# `_redact_candidate_pii` does not exist yet. Each pure-function test below
# imports it LOCALLY (inside the helper, called from each test body) so a
# missing implementation errors only these new tests — not the ~30
# already-green tests earlier in this file that import real,
# already-implemented symbols from `src.worker.resume_tasks` at module scope.


def _redact(text: str, candidate: CandidateInfo) -> str:
    from src.worker.resume_tasks import _redact_candidate_pii

    result: str = _redact_candidate_pii(text, candidate)
    return result


def test_redact_candidate_pii_removes_name_email_phone_location_verbatim() -> None:
    candidate = CandidateInfo(
        name="Ada Lovelace",
        email="ada@example.com",
        phone="555-0100",
        location="Toronto, ON",
    )
    text = "Ada Lovelace | ada@example.com | 555-0100 | Toronto, ON | Senior Engineer"

    result = _redact(text, candidate)

    assert "ada lovelace" not in result.lower()
    assert "ada@example.com" not in result.lower()
    assert "555-0100" not in result.lower()
    assert "toronto, on" not in result.lower()
    assert "senior engineer" in result.lower()


def test_redact_candidate_pii_is_case_insensitive() -> None:
    """A small model may title-case the summary differently than the résumé
    body (e.g. ALL CAPS in a header vs. Title Case from the LLM)."""
    candidate = CandidateInfo(name="Ada Lovelace", email="ADA@EXAMPLE.COM")
    text = "ADA LOVELACE is a senior backend engineer. Contact: ada@example.com"

    result = _redact(text, candidate)

    assert "ada lovelace" not in result.lower()
    assert "ada@example.com" not in result.lower()
    assert "senior backend engineer" in result.lower()


def test_redact_candidate_pii_none_fields_are_noop_for_that_field() -> None:
    """Only email is set — phone/location/name are None and must not crash or
    get matched against anything (there's nothing to match)."""
    candidate = CandidateInfo(email="ada@example.com")
    text = "Ada Lovelace, Python developer, ada@example.com, 555-0100"

    result = _redact(text, candidate)

    assert "ada@example.com" not in result.lower()
    assert "ada lovelace" in result.lower()  # name is None: untouched
    assert "555-0100" in result  # phone is None: untouched


def test_redact_candidate_pii_all_fields_none_returns_text_verbatim() -> None:
    """S7 (security re-audit round 3) note: this still passes, but no longer
    for the reason its name implies. An empty `CandidateInfo` is NOT a
    blanket "return verbatim" case any more (that was the fail-OPEN bug) — a
    generic email/phone scrub always runs. This specific text just happens
    to carry no email/phone shape, so the generic scrub is a no-op and the
    output is byte-identical to the input. See
    `test_redact_candidate_pii_empty_candidate_still_scrubs_email_and_phone`
    below for the case that actually exercises the fail-closed fix."""
    candidate = CandidateInfo()
    text = "Python developer with 5 years of backend experience."

    result = _redact(text, candidate)

    assert result == text


def test_redact_candidate_pii_matches_full_identifier_not_per_token() -> None:
    """`candidate.name="Jane Doe"` must redact the literal "Jane Doe" SUBSTRING
    only — not decompose into per-token global removal, which would also eat
    an unrelated "Doe" elsewhere in the résumé (e.g. a reference's surname)."""
    candidate = CandidateInfo(name="Jane Doe")
    text = "Jane Doe. References: John Doe, former manager at Acme Corp."

    result = _redact(text, candidate)

    assert "jane doe" not in result.lower()
    assert "john doe" in result.lower(), (
        "per-token redaction of 'Doe' corrupted unrelated content that is "
        "not the candidate's own name"
    )


def test_redact_candidate_pii_collapses_resulting_whitespace() -> None:
    candidate = CandidateInfo(name="Ada Lovelace")
    text = "Ada Lovelace   Senior Engineer"

    result = _redact(text, candidate)

    assert "  " not in result  # no doubled/tripled space left by the removal
    assert "senior engineer" in result.lower()


def test_redact_candidate_pii_does_not_mutate_the_candidate_argument() -> None:
    candidate = CandidateInfo(name="Ada Lovelace")
    before = candidate.model_dump()
    _redact("Ada Lovelace, backend engineer.", candidate)

    assert candidate.model_dump() == before


# ── F1-R (MEDIUM, round 4) — whitespace-flexible identifier scrub closes ──
# format-divergent leaks ─────────────────────────────────────────────────
#
# `_redact_candidate_pii` today matches each `candidate.*` value as a single,
# LITERAL `re.escape`'d substring against the un-normalized résumé body. The
# candidate values are the LLM's NORMALIZED output — a name split across a
# PDF line break, a phone number reflowed with extra whitespace, or an email
# whose local-part appears alone in the body all diverge from the literal
# structured value and leak into the text handed to the embedder (security
# round-4 finding F1-R). The fix: for each identifier, split on `\s+` into
# tokens, `re.escape` each token, and join with `r"\s+"` so any run of
# whitespace (space/tab/newline, any count) between tokens still matches.
# Single-token identifiers (and the email as a whole) degrade to the current
# literal match — no regression. The email LOCAL-PART is additionally
# scrubbed as its own whitespace-flexible pattern (the domain is not PII).


def test_redact_candidate_pii_scrubs_newline_split_name() -> None:
    """A two-column PDF, or a name stacked above a title on the next line,
    can insert a NEWLINE where the LLM's normalized `candidate.name` has a
    plain space. The literal `re.escape` match in the current code requires
    an EXACT single space and misses this — "Jane" and "Doe" both survive,
    adjacent, in the text handed to the embedder."""
    candidate = CandidateInfo(name="Jane Doe")
    text = "Jane\nDoe Senior Engineer"

    result = _redact(text, candidate)

    assert not re.search(
        r"jane\s*doe", result, re.IGNORECASE
    ), f"candidate name survived a newline-split format divergence: {result!r}"
    assert "senior engineer" in result.lower()


def test_redact_candidate_pii_scrubs_double_space_split_name() -> None:
    """Same format-divergence leak as the newline case, but a doubled space
    (a common PDF-extraction artifact from column gaps)."""
    candidate = CandidateInfo(name="Jane Doe")
    text = "Jane  Doe Senior Engineer"

    result = _redact(text, candidate)

    assert not re.search(
        r"jane\s*doe", result, re.IGNORECASE
    ), f"candidate name survived a double-space format divergence: {result!r}"
    assert "senior engineer" in result.lower()


def test_redact_candidate_pii_scrubs_tab_split_name() -> None:
    """Same format-divergence leak, this time a tab (some PDF extractors
    render column gaps as literal tab characters)."""
    candidate = CandidateInfo(name="Jane Doe")
    text = "Jane\tDoe Senior Engineer"

    result = _redact(text, candidate)

    assert not re.search(
        r"jane\s*doe", result, re.IGNORECASE
    ), f"candidate name survived a tab-split format divergence: {result!r}"
    assert "senior engineer" in result.lower()


def test_redact_candidate_pii_scrubs_space_divergent_phone_digits() -> None:
    """PDF text extraction can reflow a phone number's whitespace (an extra
    space, or a line break inserted mid-number by a two-column layout) while
    the LLM normalizes the SAME digit groups with single spaces. The
    whitespace-flexible fix closes this; the current literal `re.escape`
    match requires an EXACT single space between "604", "555" and "1212"
    and leaves the digits in the embedded text.

    NOTE (round4-fix-spec.md): the fix is deliberately bounded to WHITESPACE
    divergence only — it does not strip or normalize punctuation (parens/
    dashes) that differs between the body and the LLM-normalized value. This
    body diverges from `candidate.phone` ONLY in whitespace (an extra space
    plus a line break), never in punctuation, which is exactly the
    "space-divergent phone (two-column-PDF)" case the spec fix commits to
    closing — see round4-fix-spec.md for the accepted punctuation-divergence
    residual.
    """
    candidate = CandidateInfo(phone="+1 604 555 1212")
    text = "Contact:\n+1  604 555\n1212\nSenior Engineer"

    result = _redact(text, candidate)

    assert "604" not in result
    assert "555" not in result
    assert "1212" not in result
    assert "senior engineer" in result.lower()


def test_redact_candidate_pii_scrubs_email_local_part_alone() -> None:
    """A résumé header sometimes shows just the local-part of an email (a
    truncated or column-wrapped rendering) while `candidate.email` carries
    the LLM-normalized full address. The current code only matches the FULL
    `name@domain` literal, so a bare local-part in the body is never
    touched. The domain is not PII and is not required to be scrubbed."""
    candidate = CandidateInfo(email="jane.doe@example.com")
    text = "jane.doe Senior Engineer"

    result = _redact(text, candidate)

    assert "jane.doe" not in result.lower()
    assert "senior engineer" in result.lower()


def test_redact_candidate_pii_whitespace_flexible_name_keeps_precision() -> None:
    """Over-redaction guard: the whitespace-flexible pattern must still
    require the candidate's OWN tokens, in the candidate's OWN order,
    separated by nothing but whitespace. An unrelated person who shares only
    ONE token with the candidate's name — a different first name paired
    with the candidate's surname, or vice versa — must be preserved
    verbatim; the fix must not start connecting unrelated tokens across
    word boundaries just because it tolerates whitespace runs."""
    candidate = CandidateInfo(name="Jane Doe")
    text = "Jane Smith is the referring recruiter. Contact John Doe for references."

    result = _redact(text, candidate)

    assert "jane smith" in result.lower(), (
        "an unrelated person's name sharing the candidate's first name was "
        f"corrupted by the whitespace-flexible scrub: {result!r}"
    )
    assert "john doe" in result.lower(), (
        "an unrelated person's name sharing the candidate's surname was "
        f"corrupted by the whitespace-flexible scrub: {result!r}"
    )


# ── F1 (HIGH, round 3) — end-to-end: embed-call TEXT is scrubbed, the ─────
# STORED ResumeParsed stays full/unredacted ─────────────────────────────────


@pytest.mark.asyncio
async def test_header_chunk_pii_redacted_in_embeds_full_text_stored() -> None:
    resume_id = uuid4()
    job_id = uuid4()
    conn = _make_conn(
        _meta_row(job_id=job_id, status="uploaded", blob_key="resumes/abc.pdf")
    )
    blob_store = MagicMock(get=AsyncMock(return_value=b"pdf-bytes"))
    candidate = CandidateInfo(
        name="Ada Lovelace",
        email="ada.lovelace@example.com",
        phone="555-0100",
        location="Toronto, ON",
    )
    core = ResumeCore(
        candidate=candidate, summary="Backend engineer with 10 years experience."
    )
    llm = MagicMock(chat_json=AsyncMock(return_value=core))
    header_text = "Ada Lovelace | ada.lovelace@example.com | 555-0100 | Toronto, ON"
    chunks = [
        ResumeChunk(id="c_001", section="header", page=0, text=header_text),
        ResumeChunk(
            id="c_002", section="skills", page=0, text="Python, SQL, Kubernetes"
        ),
    ]
    embedder = MagicMock(
        embed=AsyncMock(side_effect=[[[0.1] * 8], [[0.2] * 8, [0.3] * 8]])
    )
    ctx = _make_ctx(conn, llm=llm, blob_store=blob_store, embedder=embedder)
    merged_skills = [ResumeSkill(name="Python")]

    with (
        patch(
            "src.worker.resume_tasks.extract_text", MagicMock(return_value=MagicMock())
        ),
        patch("src.worker.resume_tasks.chunk_resume", MagicMock(return_value=chunks)),
        patch(
            "src.worker.resume_tasks._extract_skills_merged",
            new_callable=AsyncMock,
            return_value=(merged_skills, None),
        ),
        patch(
            "src.worker.resume_tasks.resume_service.encrypt_pii_via_session",
            new_callable=AsyncMock,
            return_value=(b"enc-name", b"enc-email", b"enc-phone", "hash123"),
        ),
        patch(
            "src.worker.resume_tasks.resume_service.record_parsed",
            new_callable=AsyncMock,
            return_value=True,
        ) as record_parsed,
        patch(
            "src.worker.resume_tasks.outbox_service.enqueue_outbox",
            new_callable=AsyncMock,
        ),
    ):
        result = await parse_resume(ctx, str(resume_id))

    assert result == "parsed"
    assert embedder.embed.await_count == 2
    summary_call_texts = _flat_call_args(embedder.embed.await_args_list[0])[0]
    chunk_call_texts = _flat_call_args(embedder.embed.await_args_list[1])[0]

    pii_substrings = [
        "Ada Lovelace",
        "ada.lovelace@example.com",
        "555-0100",
        "Toronto, ON",
    ]
    for embedded_text in [*summary_call_texts, *chunk_call_texts]:
        for pii in pii_substrings:
            assert pii.lower() not in embedded_text.lower(), (
                f"embedded text still carries candidate PII substring "
                f"{pii!r}: {embedded_text!r}"
            )

    # Non-header chunks still get embedded — redaction is not "embed nothing".
    assert len(chunk_call_texts) == 2
    assert "python" in chunk_call_texts[1].lower()
    assert "kubernetes" in chunk_call_texts[1].lower()

    # The STORED ResumeParsed (system of record, ADR-007 §6) keeps the FULL,
    # unredacted chunk text — only the embedder's input was scrubbed.
    record_parsed.assert_awaited_once()
    flat = _flat_call_args(record_parsed.await_args)
    stored_parsed = next(a for a in flat if isinstance(a, ResumeParsed))
    assert stored_parsed.chunks[0].text == header_text


@pytest.mark.asyncio
async def test_summary_with_candidate_name_redacted_in_embed_only() -> None:
    """Small models sometimes open the `summary` field with the candidate's
    own name ("Jane Doe is a senior engineer..."). The embedded summary text
    must not carry it; the STORED `ResumeParsed.summary` must stay full."""
    resume_id = uuid4()
    job_id = uuid4()
    conn = _make_conn(
        _meta_row(job_id=job_id, status="uploaded", blob_key="resumes/abc.pdf")
    )
    blob_store = MagicMock(get=AsyncMock(return_value=b"pdf-bytes"))
    candidate = CandidateInfo(name="Jane Doe", email="jane.doe@example.com")
    summary_text = "Jane Doe is a senior backend engineer with deep Python experience."
    core = ResumeCore(candidate=candidate, summary=summary_text)
    llm = MagicMock(chat_json=AsyncMock(return_value=core))
    chunks = [
        ResumeChunk(id="c_001", section="summary", page=0, text="Backend engineer.")
    ]
    embedder = MagicMock(embed=AsyncMock(side_effect=[[[0.1] * 8], [[0.2] * 8]]))
    ctx = _make_ctx(conn, llm=llm, blob_store=blob_store, embedder=embedder)

    with (
        patch(
            "src.worker.resume_tasks.extract_text", MagicMock(return_value=MagicMock())
        ),
        patch("src.worker.resume_tasks.chunk_resume", MagicMock(return_value=chunks)),
        patch(
            "src.worker.resume_tasks._extract_skills_merged",
            new_callable=AsyncMock,
            return_value=([], None),
        ),
        patch(
            "src.worker.resume_tasks.resume_service.encrypt_pii_via_session",
            new_callable=AsyncMock,
            return_value=(b"enc-name", b"enc-email", b"enc-phone", "hash123"),
        ),
        patch(
            "src.worker.resume_tasks.resume_service.record_parsed",
            new_callable=AsyncMock,
            return_value=True,
        ) as record_parsed,
        patch(
            "src.worker.resume_tasks.outbox_service.enqueue_outbox",
            new_callable=AsyncMock,
        ),
    ):
        result = await parse_resume(ctx, str(resume_id))

    assert result == "parsed"
    summary_call_texts = _flat_call_args(embedder.embed.await_args_list[0])[0]
    assert len(summary_call_texts) == 1
    embedded_summary = summary_call_texts[0]
    assert "jane doe" not in embedded_summary.lower()
    assert "jane.doe@example.com" not in embedded_summary.lower()
    assert "senior backend engineer" in embedded_summary.lower()

    record_parsed.assert_awaited_once()
    flat = _flat_call_args(record_parsed.await_args)
    stored_parsed = next(a for a in flat if isinstance(a, ResumeParsed))
    assert stored_parsed.summary == summary_text


# ── F2 (MEDIUM, round 3) — outbox payload must not carry cleartext `summary`
#
# Even with the embedding scrubbed (F1), `outbox.parsed.summary` is still
# cleartext and can carry the candidate's name. Phase 4 reads `summary` from
# `resumes.parsed` (the system of record) — the outbox does not need it.


@pytest.mark.asyncio
async def test_outbox_payload_parsed_dict_excludes_summary_key() -> None:
    resume_id = uuid4()
    job_id = uuid4()
    conn = _make_conn(
        _meta_row(job_id=job_id, status="uploaded", blob_key="resumes/abc.pdf")
    )
    blob_store = MagicMock(get=AsyncMock(return_value=b"pdf-bytes"))
    core = ResumeCore(
        candidate=CandidateInfo(name="Jane Doe"),
        summary="Jane Doe is a senior backend engineer.",
        experience=[Experience(company="Acme", title="Engineer")],
    )
    llm = MagicMock(chat_json=AsyncMock(return_value=core))
    chunks = [
        ResumeChunk(id="c_001", section="summary", page=0, text="Backend engineer.")
    ]
    embedder = MagicMock(embed=AsyncMock(side_effect=[[[0.1] * 8], [[0.2] * 8]]))
    ctx = _make_ctx(conn, llm=llm, blob_store=blob_store, embedder=embedder)
    merged_skills = [ResumeSkill(name="Python")]

    with (
        patch(
            "src.worker.resume_tasks.extract_text", MagicMock(return_value=MagicMock())
        ),
        patch("src.worker.resume_tasks.chunk_resume", MagicMock(return_value=chunks)),
        patch(
            "src.worker.resume_tasks._extract_skills_merged",
            new_callable=AsyncMock,
            return_value=(merged_skills, None),
        ),
        patch(
            "src.worker.resume_tasks.resume_service.encrypt_pii_via_session",
            new_callable=AsyncMock,
            return_value=(b"enc-name", b"enc-email", b"enc-phone", "hash123"),
        ),
        patch(
            "src.worker.resume_tasks.resume_service.record_parsed",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "src.worker.resume_tasks.outbox_service.enqueue_outbox",
            new_callable=AsyncMock,
        ) as enqueue,
    ):
        result = await parse_resume(ctx, str(resume_id))

    assert result == "parsed"
    enqueue.assert_awaited_once()
    parsed_dict = enqueue.await_args.kwargs["payload"]["parsed"]

    assert "summary" not in parsed_dict, (
        "outbox payload leaks the cleartext `summary` — Phase 4 reads it "
        "from resumes.parsed (system of record), not the outbox"
    )
    assert "skills" in parsed_dict
    assert "experience" in parsed_dict


# ── S7 (security re-audit round 3) — redaction must fail CLOSED, not open ──
#
# `_redact_candidate_pii` previously returned its input VERBATIM whenever
# `CandidateInfo` was completely empty (`patterns` stayed `[]`) — "no
# structured identifiers" silently became "nothing to redact". This is
# exactly F3a's correlated-failure condition: an empty candidate block is
# not a rare edge case, it is what happens whenever the `resume_core_v1` LLM
# call under-extracts. Two-part fix: (1) a GENERIC, candidate-free email/
# phone scrub now runs unconditionally; (2) `parse_resume` refuses to embed
# a header-LIKE chunk at all when there is no structured candidate context
# to check against, because no regex can reliably catch a bare NAME.


def test_generic_contact_scrub_redacts_email_with_no_candidate_context() -> None:
    from src.worker.resume_tasks import _generic_contact_scrub

    text = "Contact casey.rivera@example.test for details."
    result = _generic_contact_scrub(text)
    assert "casey.rivera@example.test" not in result.lower()


def test_generic_contact_scrub_redacts_phone_with_no_candidate_context() -> None:
    from src.worker.resume_tasks import _generic_contact_scrub

    text = "Call 555-0101 anytime."
    result = _generic_contact_scrub(text)
    assert "555-0101" not in result


def test_generic_contact_scrub_leaves_benign_text_untouched() -> None:
    from src.worker.resume_tasks import _generic_contact_scrub

    text = "Python developer with 5 years of backend experience."
    assert _generic_contact_scrub(text) == text


def test_redact_candidate_pii_empty_candidate_still_scrubs_email_and_phone() -> None:
    """S7: the OLD behaviour (`result == text`, always, whenever every
    `CandidateInfo` field is `None`) is a fail-OPEN bug — the case pinned by
    `test_redact_candidate_pii_all_fields_none_returns_text_verbatim` above
    still holds only because that specific text carries no email/phone
    shape at all. This test uses a text that DOES, and must come back
    scrubbed even with a totally empty candidate block."""
    candidate = CandidateInfo()
    text = "Casey Rivera, casey.rivera@example.test, 555-0101"

    result = _redact(text, candidate)

    assert "casey.rivera@example.test" not in result.lower()
    assert "555-0101" not in result
    # A bare NAME cannot be caught by a structure-only scrub with no
    # candidate context at all — this is the documented residual the
    # header-chunk-skip end-to-end tests below close a different way.


@pytest.mark.asyncio
async def test_empty_candidate_block_header_chunk_never_reaches_embedder() -> None:
    """The end-to-end S7 close: with a completely empty `CandidateInfo`
    (F3a's exact defeat condition), the résumé's first (header-like) chunk
    must NEVER be handed to `embedder.embed` at all — not even scrubbed."""
    resume_id = uuid4()
    job_id = uuid4()
    conn = _make_conn(
        _meta_row(job_id=job_id, status="uploaded", blob_key="resumes/abc.pdf")
    )
    blob_store = MagicMock(get=AsyncMock(return_value=b"pdf-bytes"))
    core = ResumeCore(candidate=CandidateInfo(), summary="Backend engineer.")
    llm = MagicMock(chat_json=AsyncMock(return_value=core))
    header_text = "Casey Rivera | casey.rivera@example.test | 555-0101 | Vancouver, BC"
    chunks = [
        ResumeChunk(id="c_001", section="other", page=0, text=header_text),
        ResumeChunk(
            id="c_002", section="skills", page=0, text="Python, SQL, Kubernetes"
        ),
    ]
    embedder = MagicMock(embed=AsyncMock(side_effect=[[[0.1] * 8], [[0.2] * 8]]))
    ctx = _make_ctx(conn, llm=llm, blob_store=blob_store, embedder=embedder)
    merged_skills = [ResumeSkill(name="Python")]

    with (
        patch(
            "src.worker.resume_tasks.extract_text", MagicMock(return_value=MagicMock())
        ),
        patch("src.worker.resume_tasks.chunk_resume", MagicMock(return_value=chunks)),
        patch(
            "src.worker.resume_tasks._extract_skills_merged",
            new_callable=AsyncMock,
            return_value=(merged_skills, None),
        ),
        patch(
            "src.worker.resume_tasks.resume_service.encrypt_pii_via_session",
            new_callable=AsyncMock,
            return_value=(b"enc-name", b"enc-email", b"enc-phone", "hash123"),
        ),
        patch(
            "src.worker.resume_tasks.resume_service.record_parsed",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "src.worker.resume_tasks.outbox_service.enqueue_outbox",
            new_callable=AsyncMock,
        ) as enqueue,
    ):
        result = await parse_resume(ctx, str(resume_id))

    assert result == "parsed"
    # Only ONE embed call for the chunk batch (the header chunk was dropped
    # before ever reaching `_embed_batched`) plus the summary call.
    assert embedder.embed.await_count == 2
    chunk_call_texts = _flat_call_args(embedder.embed.await_args_list[1])[0]
    assert len(chunk_call_texts) == 1, (
        "expected exactly one (non-header) chunk to reach the embedder, got "
        f"{chunk_call_texts!r}"
    )
    assert "casey" not in chunk_call_texts[0].lower()
    assert "rivera" not in chunk_call_texts[0].lower()
    assert "python" in chunk_call_texts[0].lower()

    payload = enqueue.await_args.kwargs["payload"]
    assert set(payload["chunk_embs"]) == {"c_002"}, (
        "the header chunk must have NO entry in chunk_embs at all when "
        "redaction is unavailable — not even a scrubbed one"
    )


@pytest.mark.asyncio
async def test_empty_candidate_block_non_header_chunks_still_embed() -> None:
    """Belt-and-braces: the S7 fix is "skip the header-like chunk(s)", not
    "embed nothing" — a résumé with an empty candidate block must still get
    every OTHER chunk embedded normally."""
    resume_id = uuid4()
    job_id = uuid4()
    conn = _make_conn(
        _meta_row(job_id=job_id, status="uploaded", blob_key="resumes/abc.pdf")
    )
    blob_store = MagicMock(get=AsyncMock(return_value=b"pdf-bytes"))
    core = ResumeCore(candidate=CandidateInfo(), summary="Backend engineer.")
    llm = MagicMock(chat_json=AsyncMock(return_value=core))
    chunks = [
        ResumeChunk(id="c_001", section="other", page=0, text="Header block text"),
        ResumeChunk(id="c_002", section="skills", page=0, text="Python, SQL"),
        ResumeChunk(id="c_003", section="experience", page=0, text="Shipped APIs."),
    ]
    embedder = MagicMock(
        embed=AsyncMock(side_effect=[[[0.1] * 8], [[0.2] * 8, [0.3] * 8]])
    )
    ctx = _make_ctx(conn, llm=llm, blob_store=blob_store, embedder=embedder)
    merged_skills = [ResumeSkill(name="Python")]

    with (
        patch(
            "src.worker.resume_tasks.extract_text", MagicMock(return_value=MagicMock())
        ),
        patch("src.worker.resume_tasks.chunk_resume", MagicMock(return_value=chunks)),
        patch(
            "src.worker.resume_tasks._extract_skills_merged",
            new_callable=AsyncMock,
            return_value=(merged_skills, None),
        ),
        patch(
            "src.worker.resume_tasks.resume_service.encrypt_pii_via_session",
            new_callable=AsyncMock,
            return_value=(b"enc-name", b"enc-email", b"enc-phone", "hash123"),
        ),
        patch(
            "src.worker.resume_tasks.resume_service.record_parsed",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "src.worker.resume_tasks.outbox_service.enqueue_outbox",
            new_callable=AsyncMock,
        ) as enqueue,
    ):
        result = await parse_resume(ctx, str(resume_id))

    assert result == "parsed"
    payload = enqueue.await_args.kwargs["payload"]
    assert set(payload["chunk_embs"]) == {"c_002", "c_003"}


@pytest.mark.asyncio
async def test_empty_candidate_block_uncommon_name_skill_survives_at_parse_time() -> (
    None
):
    """ADR-008 (supersedes S12): with `CandidateInfo` completely empty,
    `_redact_candidate_pii` has no structured identifier to match against and
    falls through to `_generic_contact_scrub`, which by its own docstring
    "cannot catch a bare NAME" — so an uncommon full name the LLM emitted as
    a "skill" now DELIBERATELY survives into `resumes.parsed`/the outbox at
    parse time (ADR-007 §6 already accepts `resumes.parsed` cleartext at
    rest; N1 already accepts it riding the outbox). This is the accepted
    trade — the load-bearing privacy control is graph-projection hashing
    (`skills_graph.resume_skill_canonical_key`), proven never to leak this
    name into Neo4j by ``test_graph_projection_pii_sweep.py``, not this
    parse-time layer any more."""
    resume_id = uuid4()
    job_id = uuid4()
    conn = _make_conn(
        _meta_row(job_id=job_id, status="uploaded", blob_key="resumes/abc.pdf")
    )
    blob_store = MagicMock(get=AsyncMock(return_value=b"pdf-bytes"))
    core = ResumeCore(candidate=CandidateInfo(), summary="Backend engineer.")
    llm = MagicMock(chat_json=AsyncMock(return_value=core))
    chunks = [
        ResumeChunk(id="c_001", section="summary", page=0, text="Backend engineer.")
    ]
    embedder = MagicMock(embed=AsyncMock(side_effect=[[[0.1] * 8], [[0.2] * 8]]))
    ctx = _make_ctx(conn, llm=llm, blob_store=blob_store, embedder=embedder)
    merged_skills = [ResumeSkill(name="python"), ResumeSkill(name="torbjorn kvistad")]

    with (
        patch(
            "src.worker.resume_tasks.extract_text", MagicMock(return_value=MagicMock())
        ),
        patch("src.worker.resume_tasks.chunk_resume", MagicMock(return_value=chunks)),
        patch(
            "src.worker.resume_tasks._extract_skills_merged",
            new_callable=AsyncMock,
            return_value=(merged_skills, None),
        ),
        patch(
            "src.worker.resume_tasks.resume_service.encrypt_pii_via_session",
            new_callable=AsyncMock,
            return_value=(None, None, None, None),
        ),
        patch(
            "src.worker.resume_tasks.resume_service.record_parsed",
            new_callable=AsyncMock,
            return_value=True,
        ) as record_parsed,
        patch(
            "src.worker.resume_tasks.outbox_service.enqueue_outbox",
            new_callable=AsyncMock,
        ) as enqueue,
    ):
        result = await parse_resume(ctx, str(resume_id))

    assert result == "parsed"
    stored_parsed = next(
        a
        for a in _flat_call_args(record_parsed.await_args)
        if isinstance(a, ResumeParsed)
    )
    stored_names = [s.name for s in stored_parsed.skills]
    assert "python" in stored_names
    assert any(
        "torbjorn" in n.lower() and "kvistad" in n.lower() for n in stored_names
    ), f"expected the uncommon name to survive at parse time: {stored_names!r}"

    payload = enqueue.await_args.kwargs["payload"]
    outbox_skill_names = [s["name"] for s in payload["parsed"]["skills"]]
    assert "python" in outbox_skill_names
    assert any(
        "torbjorn" in n.lower() and "kvistad" in n.lower() for n in outbox_skill_names
    )


@pytest.mark.asyncio
async def test_non_empty_candidate_block_uncommon_two_word_skill_kept() -> None:
    """A legitimate two-word skill missing from the vocabulary (`postgres
    db`) must still be kept when the candidate block is populated — never
    swept up by the (candidate-identity-scoped) redaction."""
    resume_id = uuid4()
    job_id = uuid4()
    conn = _make_conn(
        _meta_row(job_id=job_id, status="uploaded", blob_key="resumes/abc.pdf")
    )
    blob_store = MagicMock(get=AsyncMock(return_value=b"pdf-bytes"))
    core = ResumeCore(
        candidate=CandidateInfo(name="Casey Rivera"), summary="Backend engineer."
    )
    llm = MagicMock(chat_json=AsyncMock(return_value=core))
    chunks = [
        ResumeChunk(id="c_001", section="summary", page=0, text="Backend engineer.")
    ]
    embedder = MagicMock(embed=AsyncMock(side_effect=[[[0.1] * 8], [[0.2] * 8]]))
    ctx = _make_ctx(conn, llm=llm, blob_store=blob_store, embedder=embedder)
    merged_skills = [ResumeSkill(name="postgres db")]

    with (
        patch(
            "src.worker.resume_tasks.extract_text", MagicMock(return_value=MagicMock())
        ),
        patch("src.worker.resume_tasks.chunk_resume", MagicMock(return_value=chunks)),
        patch(
            "src.worker.resume_tasks._extract_skills_merged",
            new_callable=AsyncMock,
            return_value=(merged_skills, None),
        ),
        patch(
            "src.worker.resume_tasks.resume_service.encrypt_pii_via_session",
            new_callable=AsyncMock,
            return_value=(b"enc-name", b"enc-email", b"enc-phone", "hash123"),
        ),
        patch(
            "src.worker.resume_tasks.resume_service.record_parsed",
            new_callable=AsyncMock,
            return_value=True,
        ) as record_parsed,
        patch(
            "src.worker.resume_tasks.outbox_service.enqueue_outbox",
            new_callable=AsyncMock,
        ),
    ):
        result = await parse_resume(ctx, str(resume_id))

    assert result == "parsed"
    stored_parsed = next(
        a
        for a in _flat_call_args(record_parsed.await_args)
        if isinstance(a, ResumeParsed)
    )
    stored_names = [s.name for s in stored_parsed.skills]
    assert "postgres db" in stored_names


# ── FU-7 (ADR-021 §3) — honest résumé parse status ──────────────────────────
#
# Two transitions `parse_resume` must make honest:
#
# 1. Claim `uploaded -> parsing` at task start (`resume_service.claim_parsing`),
#    BEFORE any blob I/O — so a worker crash mid-parse leaves the row
#    observably 'parsing', never silently stuck at 'uploaded' forever.
# 2. On a TRANSIENT `LLMUnavailableError` (Ollama down / circuit breaker
#    open), let arq retry via `raise arq.Retry(...) from exc` on every
#    NON-final try, and only give up (`record_parse_failure` -> "failed") on
#    the LAST configured try (`ctx["job_try"] >= settings.resume_parse_max_tries`).
#
# CRITICAL arq fact (verified against the INSTALLED arq==0.28.0, not ADR-021
# §3's wording, which is wrong): `ctx` carries `job_try` (1-based) but NO
# `max_tries` key of its own, and a plain uncaught exception does NOT trigger
# an arq retry — only raising `arq.Retry` does.


# ── Decision 1: uploaded -> parsing claim ───────────────────────────────────


@pytest.mark.asyncio
async def test_claim_parsing_called_before_blob_fetch() -> None:
    """`resume_service.claim_parsing(conn, resume_id)` must run BEFORE any
    blob I/O — the whole point of the claim is that a crash mid-parse leaves
    the row observably 'parsing', not silently stuck 'uploaded'."""
    resume_id = uuid4()
    conn = _make_conn(_meta_row(job_id=uuid4(), status="uploaded"))
    blob_store = MagicMock(get=AsyncMock(return_value=b"pdf-bytes"))
    ctx = _make_ctx(conn, blob_store=blob_store)

    manager = MagicMock()
    claim_parsing = AsyncMock(return_value=True)
    manager.attach_mock(claim_parsing, "claim_parsing")
    manager.attach_mock(blob_store.get, "blob_get")

    with (
        patch("src.worker.resume_tasks.resume_service.claim_parsing", claim_parsing),
        patch(
            "src.worker.resume_tasks.extract_text",
            MagicMock(side_effect=UnsupportedMimeError("stop after blob fetch")),
        ),
        patch(
            "src.worker.resume_tasks.resume_service.record_parse_failure",
            new_callable=AsyncMock,
        ),
    ):
        result = await parse_resume(ctx, str(resume_id))

    assert result == "failed"
    claim_parsing.assert_awaited_once()
    flat = _flat_call_args(claim_parsing.await_args)
    assert conn in flat
    assert resume_id in flat
    blob_store.get.assert_awaited_once()
    call_names = [c[0] for c in manager.mock_calls]
    assert call_names.index("claim_parsing") < call_names.index("blob_get"), (
        "claim_parsing must run BEFORE the blob fetch, not after -- a crash "
        "between the claim and the blob fetch must still leave the row "
        "'parsing', not 'uploaded'"
    )


@pytest.mark.asyncio
async def test_claim_parsing_false_on_retry_still_proceeds_to_parse() -> None:
    """A retry where the row is already 'parsing' (`claim_parsing` returns
    False -- the guarded UPDATE matched 0 rows) must NOT be treated as stale
    or an error: the parse proceeds exactly like a fresh claim. The returned
    bool is for logging only, never a control-flow branch."""
    resume_id = uuid4()
    job_id = uuid4()
    conn = _make_conn(
        _meta_row(job_id=job_id, status="parsing", blob_key="resumes/abc.pdf")
    )
    blob_store = MagicMock(get=AsyncMock(return_value=b"pdf-bytes"))
    core = ResumeCore(summary="Backend engineer.")
    llm = MagicMock(chat_json=AsyncMock(return_value=core))
    chunks = [
        ResumeChunk(id="c_001", section="summary", page=0, text="Backend engineer.")
    ]
    embedder = MagicMock(embed=AsyncMock(side_effect=[[[0.1] * 8], [[0.2] * 8]]))
    ctx = _make_ctx(conn, llm=llm, blob_store=blob_store, embedder=embedder)

    with (
        patch(
            "src.worker.resume_tasks.resume_service.claim_parsing",
            new_callable=AsyncMock,
            return_value=False,
        ) as claim_parsing,
        patch(
            "src.worker.resume_tasks.extract_text", MagicMock(return_value=MagicMock())
        ),
        patch("src.worker.resume_tasks.chunk_resume", MagicMock(return_value=chunks)),
        patch(
            "src.worker.resume_tasks._extract_skills_merged",
            new_callable=AsyncMock,
            return_value=([], None),
        ),
        patch(
            "src.worker.resume_tasks.resume_service.encrypt_pii_via_session",
            new_callable=AsyncMock,
            return_value=(None, None, None, None),
        ),
        patch(
            "src.worker.resume_tasks.resume_service.record_parsed",
            new_callable=AsyncMock,
            return_value=True,
        ) as record_parsed,
        patch(
            "src.worker.resume_tasks.outbox_service.enqueue_outbox",
            new_callable=AsyncMock,
        ) as enqueue,
    ):
        result = await parse_resume(ctx, str(resume_id))

    claim_parsing.assert_awaited_once()
    assert result == "parsed", (
        "claim_parsing returning False (already 'parsing') must not be "
        "mistaken for a stale row or a failure"
    )
    record_parsed.assert_awaited_once()
    enqueue.assert_awaited_once()


# ── Decision 2: transient-LLM retry/fail boundary ───────────────────────────
#
# Every test below needs control flow to actually reach the LLM/embed calls,
# so `claim_parsing` is mocked True throughout -- its own behaviour is
# Decision 1's concern, covered above.


@pytest.mark.asyncio
async def test_llm_unavailable_on_non_last_try_raises_arq_retry() -> None:
    """`ctx["job_try"]=1` against `resume_parse_max_tries=5` is NOT the last
    try -- `parse_resume` must let arq retry (`raise arq.Retry(...) from exc`),
    and must NEVER call `record_parse_failure` or write a 'failed' status for
    a transient outage arq is about to retry anyway."""
    resume_id = uuid4()
    conn = _make_conn(_meta_row(job_id=uuid4(), status="uploaded"))
    blob_store = MagicMock(get=AsyncMock(return_value=b"pdf-bytes"))
    llm_exc = LLMUnavailableError("ollama down")
    llm = MagicMock(chat_json=AsyncMock(side_effect=llm_exc))
    chunks = [
        ResumeChunk(id="c_001", section="summary", page=0, text="Backend engineer.")
    ]
    ctx = _make_ctx(conn, llm=llm, blob_store=blob_store)
    ctx["job_try"] = 1

    with (
        patch(
            "src.worker.resume_tasks.resume_service.claim_parsing",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "src.worker.resume_tasks.extract_text", MagicMock(return_value=MagicMock())
        ),
        patch("src.worker.resume_tasks.chunk_resume", MagicMock(return_value=chunks)),
        patch(
            "src.worker.resume_tasks.get_settings",
            return_value=Settings(resume_parse_max_tries=5),
        ),
        patch(
            "src.worker.resume_tasks.resume_service.record_parse_failure",
            new_callable=AsyncMock,
        ) as record_failure,
        patch(
            "src.worker.resume_tasks.resume_service.record_parsed",
            new_callable=AsyncMock,
        ) as record_parsed,
    ):
        with pytest.raises(arq.Retry) as exc_info:
            await parse_resume(ctx, str(resume_id))

    assert exc_info.value.__cause__ is llm_exc, (
        "arq.Retry must chain the original LLMUnavailableError ('raise ... "
        "from exc'), not swallow or replace it"
    )
    record_failure.assert_not_awaited()
    record_parsed.assert_not_awaited()


@pytest.mark.asyncio
async def test_llm_unavailable_on_last_try_records_failure_and_returns_failed() -> None:
    """`ctx["job_try"]=5` against `resume_parse_max_tries=5` IS the last try
    -- `parse_resume` must give up: call `record_parse_failure` with a reason
    mentioning the retry count, return "failed" (NOT raise arq.Retry), and
    never call `record_parsed` / enqueue an outbox row."""
    resume_id = uuid4()
    conn = _make_conn(_meta_row(job_id=uuid4(), status="uploaded"))
    blob_store = MagicMock(get=AsyncMock(return_value=b"pdf-bytes"))
    llm = MagicMock(chat_json=AsyncMock(side_effect=LLMUnavailableError("ollama down")))
    chunks = [
        ResumeChunk(id="c_001", section="summary", page=0, text="Backend engineer.")
    ]
    ctx = _make_ctx(conn, llm=llm, blob_store=blob_store)
    ctx["job_try"] = 5

    with (
        patch(
            "src.worker.resume_tasks.resume_service.claim_parsing",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "src.worker.resume_tasks.extract_text", MagicMock(return_value=MagicMock())
        ),
        patch("src.worker.resume_tasks.chunk_resume", MagicMock(return_value=chunks)),
        patch(
            "src.worker.resume_tasks.get_settings",
            return_value=Settings(resume_parse_max_tries=5),
        ),
        patch(
            "src.worker.resume_tasks.resume_service.record_parse_failure",
            new_callable=AsyncMock,
        ) as record_failure,
        patch(
            "src.worker.resume_tasks.resume_service.record_parsed",
            new_callable=AsyncMock,
        ) as record_parsed,
        patch(
            "src.worker.resume_tasks.outbox_service.enqueue_outbox",
            new_callable=AsyncMock,
        ) as enqueue,
    ):
        result = await parse_resume(ctx, str(resume_id))

    assert result == "failed"
    record_failure.assert_awaited_once()
    flat = _flat_call_args(record_failure.await_args)
    reason = next((a for a in flat if isinstance(a, str) and "retr" in a.lower()), None)
    assert (
        reason is not None
    ), f"record_parse_failure's reason should mention the retry count: {flat!r}"
    assert "5" in reason, f"reason should mention the configured try count: {reason!r}"
    record_parsed.assert_not_awaited()
    enqueue.assert_not_awaited()


@pytest.mark.asyncio
async def test_last_try_failure_reason_never_embeds_the_exception_text() -> None:
    """PRIVACY INVARIANT (security): ``failure_reason`` is a cleartext column
    surfaced by ``get_one`` even under blind review, so the retries-exhausted
    reason must be PII-free BY CONSTRUCTION — it must NOT interpolate
    ``str(exc)``, which can carry an upstream 4xx body that reflects résumé /
    candidate PII. A mutant that reintroduces ``: {exc}`` fails this test."""
    resume_id = uuid4()
    conn = _make_conn(_meta_row(job_id=uuid4(), status="uploaded"))
    blob_store = MagicMock(get=AsyncMock(return_value=b"pdf-bytes"))
    pii_marker = "HTTP 400 candidate Jane Doe jane.doe@example.com 555-0199"
    llm = MagicMock(chat_json=AsyncMock(side_effect=LLMUnavailableError(pii_marker)))
    chunks = [
        ResumeChunk(id="c_001", section="summary", page=0, text="Backend engineer.")
    ]
    ctx = _make_ctx(conn, llm=llm, blob_store=blob_store)
    ctx["job_try"] = 5

    with (
        patch(
            "src.worker.resume_tasks.resume_service.claim_parsing",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "src.worker.resume_tasks.extract_text", MagicMock(return_value=MagicMock())
        ),
        patch("src.worker.resume_tasks.chunk_resume", MagicMock(return_value=chunks)),
        patch(
            "src.worker.resume_tasks.get_settings",
            return_value=Settings(resume_parse_max_tries=5),
        ),
        patch(
            "src.worker.resume_tasks.resume_service.record_parse_failure",
            new_callable=AsyncMock,
        ) as record_failure,
    ):
        result = await parse_resume(ctx, str(resume_id))

    assert result == "failed"
    record_failure.assert_awaited_once()
    flat = _flat_call_args(record_failure.await_args)
    reasons = [a for a in flat if isinstance(a, str)]
    for r in reasons:
        for leaked in ("Jane", "Doe", "jane.doe@example.com", "555-0199"):
            assert leaked not in r, (
                f"failure_reason leaked exception PII {leaked!r}: {r!r} — the "
                "reason must be PII-free by construction, not interpolate str(exc)"
            )


@pytest.mark.asyncio
async def test_llm_unavailable_from_skills_extraction_on_last_try_records_failure() -> (
    None
):
    """SINGLE boundary proof, not a per-call-site patch: the core LLM call
    succeeds, but the LLM call INSIDE `_extract_skills_merged`
    (`resume_skills_v2`) raises `LLMUnavailableError` on the last try. Must
    be caught by the SAME boundary as the core call -- `_extract_skills_merged`
    itself only ever catches `LLMOutputInvalidError`, never
    `LLMUnavailableError` (see its existing non-fatal-failure test above), so
    this exception is UNCAUGHT until it reaches `parse_resume`."""
    resume_id = uuid4()
    conn = _make_conn(_meta_row(job_id=uuid4(), status="uploaded"))
    blob_store = MagicMock(get=AsyncMock(return_value=b"pdf-bytes"))
    core = ResumeCore(summary="Backend engineer.")

    async def _chat_json_dispatch(messages: Any, schema: Any, **kwargs: Any) -> Any:
        if schema is ResumeCore:
            return core
        raise LLMUnavailableError("ollama down mid-skills-call")

    llm = MagicMock(chat_json=AsyncMock(side_effect=_chat_json_dispatch))
    chunks = [ResumeChunk(id="c_001", section="skills", page=0, text="Python, SQL")]
    ctx = _make_ctx(conn, llm=llm, blob_store=blob_store)
    ctx["job_try"] = 5

    with (
        patch(
            "src.worker.resume_tasks.resume_service.claim_parsing",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "src.worker.resume_tasks.extract_text", MagicMock(return_value=MagicMock())
        ),
        patch("src.worker.resume_tasks.chunk_resume", MagicMock(return_value=chunks)),
        patch(
            "src.worker.resume_tasks.match_skills_in_text", MagicMock(return_value=[])
        ),
        patch(
            "src.worker.resume_tasks.get_settings",
            return_value=Settings(resume_parse_max_tries=5),
        ),
        patch(
            "src.worker.resume_tasks.resume_service.record_parse_failure",
            new_callable=AsyncMock,
        ) as record_failure,
        patch(
            "src.worker.resume_tasks.resume_service.record_parsed",
            new_callable=AsyncMock,
        ) as record_parsed,
        patch(
            "src.worker.resume_tasks.outbox_service.enqueue_outbox",
            new_callable=AsyncMock,
        ) as enqueue,
    ):
        result = await parse_resume(ctx, str(resume_id))

    assert result == "failed"
    record_failure.assert_awaited_once()
    record_parsed.assert_not_awaited()
    enqueue.assert_not_awaited()


@pytest.mark.asyncio
async def test_llm_unavailable_from_embedder_on_last_try_records_failure() -> None:
    """SINGLE boundary proof, not a per-call-site patch: the core LLM call
    and skills extraction succeed, but `embedder.embed` (the summary embed
    call) raises `LLMUnavailableError` on the last try. Must be caught by the
    SAME boundary as the core/skills LLM calls, exactly like the last-try
    core-call case above."""
    resume_id = uuid4()
    conn = _make_conn(_meta_row(job_id=uuid4(), status="uploaded"))
    blob_store = MagicMock(get=AsyncMock(return_value=b"pdf-bytes"))
    core = ResumeCore(summary="Backend engineer.")
    llm = MagicMock(chat_json=AsyncMock(return_value=core))
    chunks = [
        ResumeChunk(id="c_001", section="summary", page=0, text="Backend engineer.")
    ]
    embedder = MagicMock(
        embed=AsyncMock(side_effect=LLMUnavailableError("ollama down mid-embed"))
    )
    ctx = _make_ctx(conn, llm=llm, blob_store=blob_store, embedder=embedder)
    ctx["job_try"] = 5

    with (
        patch(
            "src.worker.resume_tasks.resume_service.claim_parsing",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "src.worker.resume_tasks.extract_text", MagicMock(return_value=MagicMock())
        ),
        patch("src.worker.resume_tasks.chunk_resume", MagicMock(return_value=chunks)),
        patch(
            "src.worker.resume_tasks._extract_skills_merged",
            new_callable=AsyncMock,
            return_value=([], None),
        ),
        patch(
            "src.worker.resume_tasks.get_settings",
            return_value=Settings(resume_parse_max_tries=5),
        ),
        patch(
            "src.worker.resume_tasks.resume_service.record_parse_failure",
            new_callable=AsyncMock,
        ) as record_failure,
        patch(
            "src.worker.resume_tasks.resume_service.record_parsed",
            new_callable=AsyncMock,
        ) as record_parsed,
        patch(
            "src.worker.resume_tasks.outbox_service.enqueue_outbox",
            new_callable=AsyncMock,
        ) as enqueue,
    ):
        result = await parse_resume(ctx, str(resume_id))

    assert result == "failed"
    record_failure.assert_awaited_once()
    record_parsed.assert_not_awaited()
    enqueue.assert_not_awaited()


# ── FU-7 §4 / ADR-030: parse_resume degraded-parse visibility + exclusion ──
#
# Mirrors the withdrawn-during-parse skip (ADR-026) at
# `tests/integration/test_parse_resume_withdrawn_race_pg.py` and the unit
# mocking style used throughout this file's happy-path tests. When
# `_extract_skills_merged` signals a non-None degradation reason, the
# persisted `ResumeParsed.degraded` must be True (with the reason carried
# through) AND the `resume.parsed` outbox enqueue — the event that drives
# graph projection / ranking eligibility — must be SKIPPED, exactly like the
# withdrawn-mid-parse race. The task must still return "parsed" (persisted,
# visible — just excluded from ranking until re-parsed).


@pytest.mark.asyncio
async def test_degraded_skills_extraction_sets_degraded_true_on_persisted_parsed() -> (
    None
):
    resume_id = uuid4()
    job_id = uuid4()
    conn = _make_conn(
        _meta_row(job_id=job_id, status="uploaded", blob_key="resumes/abc.pdf")
    )
    blob_store = MagicMock(get=AsyncMock(return_value=b"pdf-bytes"))
    core = ResumeCore(
        candidate=CandidateInfo(name="Ada Lovelace"), summary="Backend engineer."
    )
    llm = MagicMock(chat_json=AsyncMock(return_value=core))
    chunks = [
        ResumeChunk(id="c_001", section="summary", page=0, text="Backend engineer.")
    ]
    embedder = MagicMock(embed=AsyncMock(side_effect=[[[0.1] * 8], [[0.2] * 8]]))
    ctx = _make_ctx(conn, llm=llm, blob_store=blob_store, embedder=embedder)
    degraded_reason = "skills extraction failed (AI); using keyword-scan fallback"
    merged_skills = [ResumeSkill(name="python")]

    with (
        patch(
            "src.worker.resume_tasks.extract_text", MagicMock(return_value=MagicMock())
        ),
        patch("src.worker.resume_tasks.chunk_resume", MagicMock(return_value=chunks)),
        patch(
            "src.worker.resume_tasks._extract_skills_merged",
            new_callable=AsyncMock,
            return_value=(merged_skills, degraded_reason),
        ),
        patch(
            "src.worker.resume_tasks.resume_service.encrypt_pii_via_session",
            new_callable=AsyncMock,
            return_value=(b"enc-name", b"enc-email", b"enc-phone", "hash123"),
        ),
        patch(
            "src.worker.resume_tasks.resume_service.record_parsed",
            new_callable=AsyncMock,
            return_value=True,
        ) as record_parsed,
        patch(
            "src.worker.resume_tasks.outbox_service.enqueue_outbox",
            new_callable=AsyncMock,
        ) as enqueue,
    ):
        result = await parse_resume(ctx, str(resume_id))

    assert result == "parsed"
    record_parsed.assert_awaited_once()
    flat = _flat_call_args(record_parsed.await_args)
    stored_parsed = next(a for a in flat if isinstance(a, ResumeParsed))
    assert stored_parsed.degraded is True
    assert stored_parsed.degradation_reason == degraded_reason

    # The load-bearing exclusion: a degraded parse must NOT enqueue the
    # projection-triggering event — no Neo4j node, no stage-1 recall, no
    # ranking, exactly like the withdrawn-mid-parse race.
    enqueue.assert_not_awaited()


@pytest.mark.asyncio
async def test_degraded_skills_extraction_skips_resume_parsed_outbox_enqueue() -> None:
    """Same scenario, isolated assertion mirroring
    ``test_withdrawn_mid_parse_enqueues_no_resume_parsed_outbox_row`` — the
    single load-bearing behaviour of this slice."""
    resume_id = uuid4()
    job_id = uuid4()
    conn = _make_conn(
        _meta_row(job_id=job_id, status="uploaded", blob_key="resumes/abc.pdf")
    )
    blob_store = MagicMock(get=AsyncMock(return_value=b"pdf-bytes"))
    core = ResumeCore(summary="Backend engineer.")
    llm = MagicMock(chat_json=AsyncMock(return_value=core))
    chunks = [
        ResumeChunk(id="c_001", section="summary", page=0, text="Backend engineer.")
    ]
    embedder = MagicMock(embed=AsyncMock(side_effect=[[[0.1] * 8], [[0.2] * 8]]))
    ctx = _make_ctx(conn, llm=llm, blob_store=blob_store, embedder=embedder)

    with (
        patch(
            "src.worker.resume_tasks.extract_text", MagicMock(return_value=MagicMock())
        ),
        patch("src.worker.resume_tasks.chunk_resume", MagicMock(return_value=chunks)),
        patch(
            "src.worker.resume_tasks._extract_skills_merged",
            new_callable=AsyncMock,
            return_value=([], "skills extraction failed (AI)"),
        ),
        patch(
            "src.worker.resume_tasks.resume_service.encrypt_pii_via_session",
            new_callable=AsyncMock,
            return_value=(None, None, None, None),
        ),
        patch(
            "src.worker.resume_tasks.resume_service.record_parsed",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "src.worker.resume_tasks.outbox_service.enqueue_outbox",
            new_callable=AsyncMock,
        ) as enqueue,
    ):
        result = await parse_resume(ctx, str(resume_id))

    assert result == "parsed"
    enqueue.assert_not_awaited()


@pytest.mark.asyncio
async def test_regression_twin_non_degraded_parse_still_enqueues_resume_parsed() -> (
    None
):
    """The pairing test: an ordinary (non-degraded) parse is UNCHANGED by
    this fix — the outbox row still enqueues and ``degraded`` stays False. A
    builder that skips the enqueue unconditionally (rather than gating on
    the degradation reason) passes the two tests above but fails this one."""
    resume_id = uuid4()
    job_id = uuid4()
    conn = _make_conn(
        _meta_row(job_id=job_id, status="uploaded", blob_key="resumes/abc.pdf")
    )
    blob_store = MagicMock(get=AsyncMock(return_value=b"pdf-bytes"))
    core = ResumeCore(
        candidate=CandidateInfo(name="Ada Lovelace"), summary="Backend engineer."
    )
    llm = MagicMock(chat_json=AsyncMock(return_value=core))
    chunks = [
        ResumeChunk(id="c_001", section="summary", page=0, text="Backend engineer.")
    ]
    embedder = MagicMock(embed=AsyncMock(side_effect=[[[0.1] * 8], [[0.2] * 8]]))
    ctx = _make_ctx(conn, llm=llm, blob_store=blob_store, embedder=embedder)
    merged_skills = [ResumeSkill(name="python")]

    with (
        patch(
            "src.worker.resume_tasks.extract_text", MagicMock(return_value=MagicMock())
        ),
        patch("src.worker.resume_tasks.chunk_resume", MagicMock(return_value=chunks)),
        patch(
            "src.worker.resume_tasks._extract_skills_merged",
            new_callable=AsyncMock,
            return_value=(merged_skills, None),
        ),
        patch(
            "src.worker.resume_tasks.resume_service.encrypt_pii_via_session",
            new_callable=AsyncMock,
            return_value=(b"enc-name", b"enc-email", b"enc-phone", "hash123"),
        ),
        patch(
            "src.worker.resume_tasks.resume_service.record_parsed",
            new_callable=AsyncMock,
            return_value=True,
        ) as record_parsed,
        patch(
            "src.worker.resume_tasks.outbox_service.enqueue_outbox",
            new_callable=AsyncMock,
        ) as enqueue,
    ):
        result = await parse_resume(ctx, str(resume_id))

    assert result == "parsed"
    stored_parsed = next(
        a
        for a in _flat_call_args(record_parsed.await_args)
        if isinstance(a, ResumeParsed)
    )
    assert stored_parsed.degraded is False
    assert stored_parsed.degradation_reason is None
    enqueue.assert_awaited_once()
