"""Unit tests for ``src.worker.tasks.parse_job`` — all I/O mocked.

Pins the arq task's control flow against the REAL ``ctx`` keys the worker
actually sets in ``src/worker/main.py::startup`` — ``pg_pool``, ``llm``,
``embedder``, ``blob_store`` — NOT hris's ``ctx["pool"]``. A verbatim
copy-paste of the hris task body reads ``ctx["pool"]`` and would ``KeyError``
at runtime; a mock ``ctx`` built from memory of hris (rather than from
``core/src/worker/main.py``) would silently pass that regression. Every test
below builds its ``ctx`` from the pinned real key set.

Also pinned:
* missing job row -> "missing"
* non-draft status -> "stale", LLM never touched
* ``LLMOutputInvalidError`` -> ``job_service.record_parse_failure``, "failed",
  NO outbox row (an outbox event for a failed parse would corrupt Phase 4)
* happy path -> ``job_service.record_parsed`` with the ``JDExtracted``,
  exactly one ``embedder.embed([summary_text])``, one outbox insert with
  ``event_type="job.parsed"`` carrying ``embedding``/``extracted``/
  ``prompt_version``, returns "parsed"
* race guard: ``record_parsed`` returns ``False`` (row left 'draft' mid-parse)
  -> "stale" and NO outbox row (an outbox event for a stale write would
  corrupt Phase 4's projection)

``src.worker.tasks`` does not exist yet — RED half of the TDD cycle; this
whole file is expected to fail at collection (ImportError).

── Round 3 (merge-blocking re-audit) additions ─────────────────────────────
* Reviewer minor #2: the JD summary-embed call (``[embedding] = await
  embedder.embed([summary_text])``) is UNGUARDED today. A permanent
  ``LLMOutputInvalidError`` must funnel through ``record_parse_failure`` ->
  "failed" with no outbox row, mirroring round-2 finding 3's fix for
  ``parse_resume``'s embedding calls. A transient ``LLMUnavailableError``
  must still ESCAPE uncaught so arq retries the genuine Ollama outage.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from src.pipeline.llm import LLMOutputInvalidError, LLMUnavailableError
from src.schemas import JDExtracted, Skill
from src.worker.tasks import parse_job


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
    return conn


def _make_ctx(conn: MagicMock, llm: MagicMock, embedder: MagicMock) -> dict[str, Any]:
    pool = MagicMock(name="pg_pool")
    pool.acquire = MagicMock(return_value=_acm(conn))
    return {
        "pg_pool": pool,
        "llm": llm,
        "embedder": embedder,
        "blob_store": MagicMock(name="blob_store"),
    }


def _fake_prompt() -> MagicMock:
    prompt = MagicMock()
    prompt.messages = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "user prompt"},
    ]
    prompt.version = "jd_extract_v1"
    return prompt


# ── missing / stale ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_missing_job_row_returns_missing() -> None:
    conn = _make_conn(None)
    llm = MagicMock(chat_json=AsyncMock())
    embedder = MagicMock(embed=AsyncMock())
    ctx = _make_ctx(conn, llm, embedder)

    result = await parse_job(ctx, str(uuid4()))

    assert result == "missing"
    llm.chat_json.assert_not_called()


@pytest.mark.parametrize("status", ["open", "closed", "archived"])
@pytest.mark.asyncio
async def test_non_draft_status_returns_stale_and_llm_never_called(
    status: str,
) -> None:
    conn = _make_conn({"description_raw": "Build things.", "status": status})
    llm = MagicMock(chat_json=AsyncMock())
    embedder = MagicMock(embed=AsyncMock())
    ctx = _make_ctx(conn, llm, embedder)

    result = await parse_job(ctx, str(uuid4()))

    assert result == "stale"
    llm.chat_json.assert_not_called()


# ── LLM failure ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_llm_output_invalid_records_failure_and_enqueues_no_outbox_row() -> None:
    job_id = uuid4()
    conn = _make_conn({"description_raw": "Build things.", "status": "draft"})
    llm = MagicMock(chat_json=AsyncMock(side_effect=LLMOutputInvalidError("bad json")))
    embedder = MagicMock(embed=AsyncMock())
    ctx = _make_ctx(conn, llm, embedder)

    with (
        patch("src.worker.tasks.load_prompt", return_value=_fake_prompt()),
        patch(
            "src.worker.tasks.job_service.record_parse_failure", new_callable=AsyncMock
        ) as record_failure,
        patch(
            "src.worker.tasks.outbox_service.enqueue_outbox", new_callable=AsyncMock
        ) as enqueue,
    ):
        result = await parse_job(ctx, str(job_id))

    assert result == "failed"
    record_failure.assert_awaited_once()
    assert job_id in _flat_call_args(record_failure.await_args)
    enqueue.assert_not_awaited()
    embedder.embed.assert_not_called()


# ── happy path ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_happy_path_records_parsed_with_the_extracted_jd() -> None:
    job_id = uuid4()
    conn = _make_conn(
        {"description_raw": "Build the ranking pipeline.", "status": "draft"}
    )
    extracted = JDExtracted(
        title="Senior Backend Engineer",
        required_skills=[Skill(name="Python")],
        min_years_experience=5,
    )
    llm = MagicMock(chat_json=AsyncMock(return_value=extracted))
    embedder = MagicMock(embed=AsyncMock(return_value=[[0.1] * 8]))
    ctx = _make_ctx(conn, llm, embedder)

    with (
        patch("src.worker.tasks.load_prompt", return_value=_fake_prompt()),
        patch(
            "src.worker.tasks.job_service.record_parsed", new_callable=AsyncMock
        ) as record_parsed,
        patch(
            "src.worker.tasks.outbox_service.enqueue_outbox", new_callable=AsyncMock
        ) as enqueue,
    ):
        record_parsed.return_value = True
        result = await parse_job(ctx, str(job_id))

    assert result == "parsed"
    record_parsed.assert_awaited_once()
    flat = _flat_call_args(record_parsed.await_args)
    assert job_id in flat
    assert extracted in flat
    assert any(isinstance(a, dt.datetime) for a in flat)
    enqueue.assert_awaited_once()


@pytest.mark.asyncio
async def test_happy_path_embeds_the_summary_text_exactly_once() -> None:
    job_id = uuid4()
    conn = _make_conn(
        {"description_raw": "Build the ranking pipeline.", "status": "draft"}
    )
    extracted = JDExtracted(title="Senior Backend Engineer")
    llm = MagicMock(chat_json=AsyncMock(return_value=extracted))
    embedder = MagicMock(embed=AsyncMock(return_value=[[0.1] * 8]))
    ctx = _make_ctx(conn, llm, embedder)

    with (
        patch("src.worker.tasks.load_prompt", return_value=_fake_prompt()),
        patch(
            "src.worker.tasks.job_service.record_parsed",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch("src.worker.tasks.outbox_service.enqueue_outbox", new_callable=AsyncMock),
    ):
        await parse_job(ctx, str(job_id))

    embedder.embed.assert_awaited_once()
    texts = _flat_call_args(embedder.embed.await_args)[0]
    assert isinstance(texts, list)
    assert len(texts) == 1
    assert isinstance(texts[0], str)


@pytest.mark.asyncio
async def test_happy_path_outbox_payload_carries_job_parsed_event() -> None:
    job_id = uuid4()
    conn = _make_conn(
        {"description_raw": "Build the ranking pipeline.", "status": "draft"}
    )
    extracted = JDExtracted(title="Senior Backend Engineer")
    embedding = [0.42] * 8
    llm = MagicMock(chat_json=AsyncMock(return_value=extracted))
    embedder = MagicMock(embed=AsyncMock(return_value=[embedding]))
    ctx = _make_ctx(conn, llm, embedder)

    with (
        patch("src.worker.tasks.load_prompt", return_value=_fake_prompt()),
        patch(
            "src.worker.tasks.job_service.record_parsed",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "src.worker.tasks.outbox_service.enqueue_outbox", new_callable=AsyncMock
        ) as enqueue,
    ):
        result = await parse_job(ctx, str(job_id))

    assert result == "parsed"
    enqueue.assert_awaited_once()
    kwargs = enqueue.await_args.kwargs
    assert kwargs["event_type"] == "job.parsed"
    assert kwargs["aggregate_id"] == job_id
    payload = kwargs["payload"]
    assert payload["embedding"] == embedding
    assert payload["extracted"] == extracted.model_dump()
    assert payload["prompt_version"] == "jd_extract_v1"


# ── race guard ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_race_guard_stale_write_returns_stale_and_enqueues_no_outbox_row() -> (
    None
):
    """``record_parsed`` returning False means the job was no longer 'draft'
    by the time the UPDATE ran (a concurrent transition). The parse must
    return "stale" and MUST NOT enqueue an outbox row for a write that never
    happened — that would corrupt Phase 4's projection."""
    job_id = uuid4()
    conn = _make_conn(
        {"description_raw": "Build the ranking pipeline.", "status": "draft"}
    )
    extracted = JDExtracted(title="Senior Backend Engineer")
    llm = MagicMock(chat_json=AsyncMock(return_value=extracted))
    embedder = MagicMock(embed=AsyncMock(return_value=[[0.1] * 8]))
    ctx = _make_ctx(conn, llm, embedder)

    with (
        patch("src.worker.tasks.load_prompt", return_value=_fake_prompt()),
        patch(
            "src.worker.tasks.job_service.record_parsed",
            new_callable=AsyncMock,
            return_value=False,
        ),
        patch(
            "src.worker.tasks.outbox_service.enqueue_outbox", new_callable=AsyncMock
        ) as enqueue,
    ):
        result = await parse_job(ctx, str(job_id))

    assert result == "stale"
    enqueue.assert_not_awaited()


# ── Reviewer minor #2 (round 3) — JD summary-embed guard parity ────────────
#
# `[embedding] = await embedder.embed([summary_text])` is UNGUARDED today. A
# permanent `LLMOutputInvalidError` (dim/count mismatch) must funnel through
# `record_parse_failure` -> "failed", exactly like round-2 finding 3 fixed
# for `parse_resume`'s embedding calls — not escape uncaught (a stranded
# 'draft' row + arq retry storm that re-burns the JD LLM extraction on every
# attempt). A transient `LLMUnavailableError` must still ESCAPE uncaught so
# arq retries the genuine Ollama outage.


@pytest.mark.asyncio
async def test_summary_embed_permanent_failure_records_failure_and_returns_failed() -> (
    None
):
    job_id = uuid4()
    conn = _make_conn({"description_raw": "Build things.", "status": "draft"})
    extracted = JDExtracted(title="Senior Backend Engineer")
    llm = MagicMock(chat_json=AsyncMock(return_value=extracted))
    embedder = MagicMock(
        embed=AsyncMock(side_effect=LLMOutputInvalidError("768-d contract violated"))
    )
    ctx = _make_ctx(conn, llm, embedder)

    with (
        patch("src.worker.tasks.load_prompt", return_value=_fake_prompt()),
        patch(
            "src.worker.tasks.job_service.record_parse_failure", new_callable=AsyncMock
        ) as record_failure,
        patch(
            "src.worker.tasks.job_service.record_parsed", new_callable=AsyncMock
        ) as record_parsed,
        patch(
            "src.worker.tasks.outbox_service.enqueue_outbox", new_callable=AsyncMock
        ) as enqueue,
    ):
        result = await parse_job(ctx, str(job_id))

    assert result == "failed"
    record_failure.assert_awaited_once()
    assert job_id in _flat_call_args(record_failure.await_args)
    record_parsed.assert_not_awaited()
    enqueue.assert_not_awaited()


@pytest.mark.asyncio
async def test_summary_embed_transient_failure_escapes_uncaught_for_arq_retry() -> None:
    job_id = uuid4()
    conn = _make_conn({"description_raw": "Build things.", "status": "draft"})
    extracted = JDExtracted(title="Senior Backend Engineer")
    llm = MagicMock(chat_json=AsyncMock(return_value=extracted))
    embedder = MagicMock(
        embed=AsyncMock(side_effect=LLMUnavailableError("ollama down"))
    )
    ctx = _make_ctx(conn, llm, embedder)

    with (
        patch("src.worker.tasks.load_prompt", return_value=_fake_prompt()),
        patch(
            "src.worker.tasks.job_service.record_parse_failure", new_callable=AsyncMock
        ) as record_failure,
        patch(
            "src.worker.tasks.outbox_service.enqueue_outbox", new_callable=AsyncMock
        ) as enqueue,
        pytest.raises(LLMUnavailableError),
    ):
        await parse_job(ctx, str(job_id))

    record_failure.assert_not_awaited()
    enqueue.assert_not_awaited()
