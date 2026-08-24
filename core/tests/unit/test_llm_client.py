"""Unit tests for the OpenAI-compatible LLM client
(``src/pipeline/llm/client.py``).

Ported from hris ``packages/pipeline/src/pipeline/llm/client.py``
(``phase3-source-dossier.md`` §3). ALL HTTP goes through
``httpx.MockTransport`` — no real network, no real Ollama. Every test
that could invoke a retry patches ``LLMClient._sleep_backoff`` so the
suite runs instantly; circuit-breaker tests patch ``time.monotonic`` with
a fake, settable clock instead of sleeping real seconds.

── Round 2 (merge-blocking re-audit) addition ──────────────────────────────
* Finding 4 (MEDIUM): ``_strip_nuls`` recurses over parsed LLM JSON with no
  depth bound. Deeply nested JSON (reachable within ``max_tokens`` via
  ``[[[...]]]``, and the résumé is prompt-injectable) can trip a
  ``RecursionError`` in the ``_strip_nuls(json.loads(...))`` chain, which is
  neither ``JSONDecodeError`` nor ``ValidationError`` — it must not escape
  ``chat_json`` uncaught.

── FU-7 §2/§6 (ADR-021 §6, ADR-029) — fail-closed ranking + empty-content ──
* §6: both ``_chat_openai`` and ``_chat_native`` already raise
  ``LLMOutputInvalidError`` when ``content`` is not a string. Empty/
  whitespace-only ``content`` (a string that decodes fine but carries
  nothing — the classic "reasoning model burned its whole token budget on a
  discarded trace" failure) must ALSO raise, with a diagnostic that says
  whether a reasoning/thinking field was present in the response.
* The ``_chat_openai`` ``json_mode`` comment currently claims switching to
  ``_chat_native`` (``llm_ollama_native=True``) is "a reliable thinking-off
  path" — per ADR-021 §6 that claim is false (neither path reliably
  suppresses reasoning for gpt-oss:20b) and must be corrected, citing
  ADR-021 §6, not merely deleted.
"""

from __future__ import annotations

import inspect
import json
import logging
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import BaseModel

from src.pipeline.llm.client import (
    LLMClient,
    LLMOutputInvalidError,
    LLMUnavailableError,
)
from src.settings import get_settings

BASE_URL = "http://fake/v1"


class _Widget(BaseModel):
    name: str
    count: int


# ── Finding 1 / Finding 4 fixtures: a schema shaped like the real ResumeCore
# candidate block, so a wrong-typed ``phone`` produces the exact pydantic v2
# error string the finding cites (``candidate.phone ... input_value=[...]``).


class _CandidatePii(BaseModel):
    name: str
    phone: str | None = None


class _ResumeLike(BaseModel):
    candidate: _CandidatePii


# ── Fake clock (circuit breaker tests) ──────────────────────────────────────


class _FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# ── Transport helpers ────────────────────────────────────────────────────


def _status_response(code: int) -> httpx.Response:
    return httpx.Response(code, text="boom")


def _ok_chat_response(content: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def _ok_native_response(content: str) -> httpx.Response:
    return httpx.Response(200, json={"message": {"content": content}})


def _ok_embed_response(vectors: list[list[float]]) -> httpx.Response:
    return httpx.Response(200, json={"data": [{"embedding": v} for v in vectors]})


def _sequenced_transport(
    outcomes: list[Exception | httpx.Response],
) -> tuple[httpx.MockTransport, list[httpx.Request]]:
    """A transport that replays ``outcomes`` in order (raising or returning),
    repeating the last outcome if called more times than provided. Every
    request is recorded so tests can assert call count and body contents."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        idx = min(len(calls) - 1, len(outcomes) - 1)
        outcome = outcomes[idx]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    return httpx.MockTransport(handler), calls


def _client(
    transport: httpx.MockTransport, *, native_chat: bool = False, **kwargs: Any
) -> LLMClient:
    http_client = httpx.AsyncClient(transport=transport)
    return LLMClient(
        BASE_URL,
        "gen-model",
        "emb-model",
        native_chat=native_chat,
        http_client=http_client,
        **kwargs,
    )


@pytest.fixture(autouse=True)
def _no_real_backoff_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retry tests exercise real retry loops; never actually sleep for them."""
    monkeypatch.setattr(LLMClient, "_sleep_backoff", AsyncMock())


# ── chat(): request shape ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_chat_posts_to_chat_completions_with_stream_false() -> None:
    transport, calls = _sequenced_transport([_ok_chat_response("hi there")])
    llm = _client(transport)

    result = await llm.chat([{"role": "user", "content": "hello"}])

    assert result == "hi there"
    assert len(calls) == 1
    assert str(calls[0].url) == f"{BASE_URL}/chat/completions"
    body = json.loads(calls[0].content)
    assert body["stream"] is False
    assert body["model"] == "gen-model"
    assert "response_format" not in body
    assert "think" not in body
    await llm.aclose()


@pytest.mark.asyncio
async def test_chat_json_mode_adds_response_format_and_think_false() -> None:
    transport, calls = _sequenced_transport([_ok_chat_response('{"a": 1}')])
    llm = _client(transport)

    await llm.chat([{"role": "user", "content": "hello"}], json_mode=True)

    body = json.loads(calls[0].content)
    assert body["response_format"] == {"type": "json_object"}
    assert body["think"] is False
    await llm.aclose()


@pytest.mark.asyncio
async def test_native_chat_strips_v1_and_posts_api_chat() -> None:
    transport, calls = _sequenced_transport([_ok_native_response("native reply")])
    llm = _client(transport, native_chat=True)

    result = await llm.chat(
        [{"role": "user", "content": "hi"}],
        json_mode=True,
        max_tokens=256,
        temperature=0.2,
    )

    assert result == "native reply"
    assert str(calls[0].url) == "http://fake/api/chat"
    body = json.loads(calls[0].content)
    assert body["stream"] is False
    assert body["format"] == "json"
    assert body["think"] is False
    assert body["options"]["num_predict"] == 256
    assert body["options"]["temperature"] == 0.2
    await llm.aclose()


# ── Retry: transport-level exceptions ────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectError("boom"),
        httpx.ReadTimeout("boom"),
        httpx.RemoteProtocolError("boom"),
    ],
)
async def test_retries_on_transport_exceptions_then_succeeds(
    exc: Exception,
) -> None:
    outcomes: list[Exception | httpx.Response] = [exc, _ok_chat_response("recovered")]
    transport, calls = _sequenced_transport(outcomes)
    llm = _client(transport, max_retries=2)

    result = await llm.chat([{"role": "user", "content": "hi"}])

    assert result == "recovered"
    assert len(calls) == 2
    await llm.aclose()


# ── Retry: retryable status codes ────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
async def test_retries_on_retryable_status_then_succeeds(status: int) -> None:
    outcomes: list[Exception | httpx.Response] = [
        _status_response(status),
        _ok_chat_response("ok"),
    ]
    transport, calls = _sequenced_transport(outcomes)
    llm = _client(transport, max_retries=2)

    result = await llm.chat([{"role": "user", "content": "hi"}])

    assert result == "ok"
    assert len(calls) == 2
    await llm.aclose()


@pytest.mark.asyncio
async def test_retries_exhausted_raises_llm_unavailable() -> None:
    outcomes: list[Exception | httpx.Response] = [_status_response(500)] * 3
    transport, calls = _sequenced_transport(outcomes)
    llm = _client(transport, max_retries=2)  # 3 total attempts

    with pytest.raises(LLMUnavailableError):
        await llm.chat([{"role": "user", "content": "hi"}])

    assert len(calls) == 3
    await llm.aclose()


# ── Fail-fast on non-429 4xx (mutation guard: must NOT retry) ────────────


@pytest.mark.asyncio
async def test_non_retryable_4xx_fails_fast_single_call() -> None:
    transport, calls = _sequenced_transport([_status_response(400)])
    llm = _client(transport, max_retries=3)

    with pytest.raises(LLMUnavailableError):
        await llm.chat([{"role": "user", "content": "hi"}])

    assert len(calls) == 1  # NOT retried — turning this into a retry must go red
    await llm.aclose()


# ── Circuit breaker ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_breaker_opens_after_threshold_and_blocks_without_calling_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock()
    monkeypatch.setattr("src.pipeline.llm.client.time.monotonic", clock)
    outcomes: list[Exception | httpx.Response] = [_status_response(500)] * 3
    transport, calls = _sequenced_transport(outcomes)
    llm = _client(
        transport, max_retries=0, breaker_threshold=3, breaker_cooldown_s=30.0
    )

    for _ in range(3):
        with pytest.raises(LLMUnavailableError):
            await llm.chat([{"role": "user", "content": "hi"}])
    assert len(calls) == 3

    with pytest.raises(LLMUnavailableError):
        await llm.chat([{"role": "user", "content": "hi"}])
    assert len(calls) == 3  # breaker blocked it — transport was NOT called a 4th time

    await llm.aclose()


@pytest.mark.asyncio
async def test_breaker_half_opens_after_cooldown_and_resets_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock()
    monkeypatch.setattr("src.pipeline.llm.client.time.monotonic", clock)
    outcomes: list[Exception | httpx.Response] = [
        _status_response(500),
        _status_response(500),
        _status_response(500),
        _ok_chat_response("recovered"),
        _ok_chat_response("still fine"),
    ]
    transport, calls = _sequenced_transport(outcomes)
    llm = _client(
        transport, max_retries=0, breaker_threshold=3, breaker_cooldown_s=30.0
    )

    for _ in range(3):
        with pytest.raises(LLMUnavailableError):
            await llm.chat([{"role": "user", "content": "hi"}])
    with pytest.raises(LLMUnavailableError):
        await llm.chat([{"role": "user", "content": "hi"}])  # still cooling down
    assert len(calls) == 3

    clock.advance(30.1)  # cooldown elapsed -> half-open trial allowed through
    result = await llm.chat([{"role": "user", "content": "hi"}])
    assert result == "recovered"
    assert len(calls) == 4

    # One success resets the breaker fully: the very next call is NOT blocked.
    result_again = await llm.chat([{"role": "user", "content": "hi"}])
    assert result_again == "still fine"
    assert len(calls) == 5

    await llm.aclose()


# ── chat_json: self-correction retry loop ────────────────────────────────


@pytest.mark.asyncio
async def test_chat_json_retries_with_validation_error_appended() -> None:
    outcomes: list[Exception | httpx.Response] = [
        _ok_chat_response("not json at all"),
        _ok_chat_response(json.dumps({"name": "widget", "count": 3})),
    ]
    transport, calls = _sequenced_transport(outcomes)
    llm = _client(transport)

    result = await llm.chat_json(
        [{"role": "user", "content": "give me json"}], _Widget, max_retries=1
    )

    assert result == _Widget(name="widget", count=3)
    assert len(calls) == 2
    second_messages = json.loads(calls[1].content)["messages"]
    assert len(second_messages) == 2  # original message + appended correction
    assert second_messages[-1]["role"] == "user"
    assert "failed validation" in second_messages[-1]["content"]
    await llm.aclose()


@pytest.mark.asyncio
async def test_chat_json_raises_after_retries_exhausted() -> None:
    outcomes: list[Exception | httpx.Response] = [
        _ok_chat_response("still not json"),
        _ok_chat_response("still not json either"),
    ]
    transport, calls = _sequenced_transport(outcomes)
    llm = _client(transport)

    with pytest.raises(LLMOutputInvalidError):
        await llm.chat_json(
            [{"role": "user", "content": "give me json"}], _Widget, max_retries=1
        )

    assert len(calls) == 2
    await llm.aclose()


# ── Finding 1 (HIGH): chat_json's raised error must not leak PII ────────
#
# pydantic v2 embeds the offending input value in ValidationError's str():
# "candidate.phone / Input should be a valid string [type=string_type,
# input_value=['555-867-5309'], input_type=list]". LLMClient.chat_json does
# `last_error = str(exc)[:500]` and raises that verbatim as
# LLMOutputInvalidError, which is then logged (llm.json_invalid) and — via
# record_parse_failure — written UNENCRYPTED to resumes.failure_reason. A
# wrong-typed field from a small local model is the routine failure path
# (it's why the self-correction retry exists), so this leaks real candidate
# PII to logs + cleartext DB on an everyday path.


@pytest.mark.asyncio
async def test_chat_json_invalid_error_omits_pii_but_keeps_field_path() -> None:
    bad_json = json.dumps(
        {"candidate": {"name": "Ada Lovelace", "phone": ["555-867-5309"]}}
    )
    outcomes: list[Exception | httpx.Response] = [
        _ok_chat_response(bad_json),
        _ok_chat_response(bad_json),
    ]
    transport, calls = _sequenced_transport(outcomes)
    llm = _client(transport)

    with pytest.raises(LLMOutputInvalidError) as exc_info:
        await llm.chat_json(
            [{"role": "user", "content": "parse this resume"}],
            _ResumeLike,
            max_retries=1,
        )

    message = str(exc_info.value)
    assert "candidate.phone" in message
    assert "string_type" in message  # the error TYPE is fine to surface
    assert "Ada Lovelace" not in message
    assert "555-867-5309" not in message
    assert len(calls) == 2
    await llm.aclose()


# ── Finding 4 (MEDIUM): LLM-emitted NUL must not survive into the model ─
#
# Postgres text/jsonb reject U+0000 (UntranslatableCharacterError). Document
# text is sanitised on the way IN (extract._sanitize), but LLM OUTPUT never
# passes through an equivalent guard — json.loads happily accepts a U+0000
# escape, and that NUL then rides straight into resumes.parsed jsonb / the
# pgp_sym_encrypt bytea column, crashing the write inside the transaction.


@pytest.mark.asyncio
async def test_chat_json_strips_nul_bytes_from_llm_string_output() -> None:
    raw = json.dumps({"name": "wid\x00get", "count": 3})
    transport, calls = _sequenced_transport([_ok_chat_response(raw)])
    llm = _client(transport)

    result = await llm.chat_json(
        [{"role": "user", "content": "give me json"}], _Widget, max_retries=0
    )

    assert "\x00" not in result.name
    assert len(calls) == 1
    await llm.aclose()


# ── Finding 4 round 2 (MEDIUM): `_strip_nuls` unbounded recursion ───────
#
# `_strip_nuls` recurses over parsed LLM JSON with no depth bound. Deeply
# nested JSON (reachable within max_tokens via `[[[...]]]`, and the résumé
# is prompt-injectable) trips a RecursionError somewhere in the
# `_strip_nuls(json.loads(...))` chain. RecursionError is neither
# JSONDecodeError nor ValidationError, so today it escapes chat_json's
# except clauses entirely — and from there, escapes parse_resume uncaught,
# stranding the row and burning an arq retry storm.


@pytest.mark.asyncio
async def test_chat_json_deeply_nested_json_never_lets_recursionerror_escape() -> None:
    depth = 5000  # comfortably past CPython's default ~1000 recursion limit
    nested = "[" * depth + '"a\x00b"' + "]" * depth
    outcomes: list[Exception | httpx.Response] = [
        _ok_chat_response(nested),
        _ok_chat_response(nested),
    ]
    transport, calls = _sequenced_transport(outcomes)
    llm = _client(transport)

    try:
        with pytest.raises(LLMOutputInvalidError):
            await llm.chat_json(
                [{"role": "user", "content": "give me json"}],
                _Widget,
                max_retries=1,
            )
    except RecursionError:
        pytest.fail(
            "RecursionError escaped chat_json — deeply nested LLM JSON must be "
            "handled (caught + converted to LLMOutputInvalidError), not "
            "propagate out of the task"
        )

    assert len(calls) == 2
    await llm.aclose()


# ── _extract_json (static) ────────────────────────────────────────────────


def test_extract_json_strips_code_fence() -> None:
    raw = '```json\n{"a": 1}\n```'
    assert LLMClient._extract_json(raw) == '{"a": 1}'


def test_extract_json_falls_back_to_bracket_slicing() -> None:
    raw = 'Sure, here you go: {"a": 1} -- hope that helps'
    assert LLMClient._extract_json(raw) == '{"a": 1}'


def test_extract_json_returns_raw_unchanged_when_no_json_like_content() -> None:
    raw = "no braces here at all"
    assert LLMClient._extract_json(raw) == raw


# ── embed(): order/count integrity ───────────────────────────────────────


@pytest.mark.asyncio
async def test_embed_raises_when_returned_count_mismatches_input() -> None:
    transport, _calls = _sequenced_transport([_ok_embed_response([[0.1, 0.2, 0.3]])])
    llm = _client(transport)

    with pytest.raises(LLMOutputInvalidError):
        await llm.embed(["a", "b"])

    await llm.aclose()


@pytest.mark.asyncio
async def test_embed_returns_vectors_in_input_order() -> None:
    vectors = [[1.0, 2.0], [3.0, 4.0]]
    transport, _calls = _sequenced_transport([_ok_embed_response(vectors)])
    llm = _client(transport)

    result = await llm.embed(["x", "y"])

    assert result == vectors
    await llm.aclose()


# ── Finding 6 (hardening): embed() must validate vector dimension ───────
#
# 768-d is a hard contract — it must match the Neo4j vector indexes.
# LLMClient.embed never checks the returned vector length today, so pointing
# llm_model_embedding at a non-768-d model silently produces vectors that
# only blow up much later, at Neo4j write time in Phase 4. The coder adds an
# `expected_dim` constructor param (sourced from settings.llm_embedding_dim).


@pytest.mark.asyncio
async def test_embed_raises_when_vector_dimension_mismatches_expected_dim() -> None:
    transport, _calls = _sequenced_transport([_ok_embed_response([[0.1, 0.2, 0.3]])])
    llm = _client(transport, expected_dim=768)

    with pytest.raises(LLMOutputInvalidError):
        await llm.embed(["a"])

    await llm.aclose()


@pytest.mark.asyncio
async def test_embed_accepts_vector_matching_expected_dim() -> None:
    vec = [0.1] * 768
    transport, _calls = _sequenced_transport([_ok_embed_response([vec])])
    llm = _client(transport, expected_dim=768)

    result = await llm.embed(["a"])

    assert result == [vec]
    await llm.aclose()


# ── aclose(): ownership of the http client ───────────────────────────────


@pytest.mark.asyncio
async def test_aclose_does_not_close_an_externally_supplied_http_client() -> None:
    external = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: _ok_chat_response("x"))
    )
    llm = LLMClient(BASE_URL, "g", "e", http_client=external)

    await llm.aclose()

    assert external.is_closed is False
    await external.aclose()


@pytest.mark.asyncio
async def test_aclose_closes_a_self_owned_http_client() -> None:
    llm = LLMClient(BASE_URL, "g", "e")

    await llm.aclose()

    assert llm._http.is_closed is True  # noqa: SLF001 — white-box ownership check


# ── Offline-egress guard ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_client_built_from_settings_never_points_at_a_cloud_host() -> None:
    settings = get_settings()
    llm = LLMClient(
        settings.llm_base_url,
        settings.llm_model_generation,
        settings.llm_model_embedding,
    )
    try:
        base = llm._base.lower()  # noqa: SLF001 — white-box egress check
        assert any(
            host in base for host in ("host.docker.internal", "localhost", "127.0.0.1")
        )
        cloud_hosts = (
            "openai.com",
            "anthropic.com",
            "azure.com",
            "googleapis.com",
            "amazonaws.com",
        )
        assert not any(cloud in base for cloud in cloud_hosts)
    finally:
        await llm.aclose()


# ── FU-7 §6 (ADR-021 §6, ADR-029) — empty-content diagnostic ────────────────
#
# Both `_chat_openai` and `_chat_native` already raise LLMOutputInvalidError
# when `content` is not a string at all. Empty/whitespace-only content is a
# DIFFERENT, common failure: a reasoning model (gpt-oss:20b et al) burns its
# whole `max_tokens` budget on a discarded reasoning trace, so `content`
# comes back a legitimate but EMPTY string. That must also raise, and the
# message must say whether a reasoning/thinking field was present in the
# response — the signal that distinguishes "reasoning ate the budget" from
# some other cause of an empty response.


@pytest.mark.asyncio
async def test_chat_openai_raises_on_empty_string_content() -> None:
    transport, _calls = _sequenced_transport([_ok_chat_response("")])
    llm = _client(transport)

    with pytest.raises(LLMOutputInvalidError):
        await llm.chat([{"role": "user", "content": "hi"}], json_mode=True)

    await llm.aclose()


@pytest.mark.asyncio
async def test_chat_openai_raises_on_whitespace_only_content() -> None:
    transport, _calls = _sequenced_transport([_ok_chat_response("   \n\t  ")])
    llm = _client(transport)

    with pytest.raises(LLMOutputInvalidError):
        await llm.chat([{"role": "user", "content": "hi"}], json_mode=True)

    await llm.aclose()


@pytest.mark.asyncio
async def test_chat_openai_empty_content_diagnostic_notes_no_reasoning_present() -> (
    None
):
    transport, _calls = _sequenced_transport([_ok_chat_response("")])
    llm = _client(transport)

    with pytest.raises(LLMOutputInvalidError) as exc_info:
        await llm.chat([{"role": "user", "content": "hi"}], json_mode=True)

    message = str(exc_info.value).lower()
    assert "empty" in message
    assert "reasoning_present=false" in message.replace(" ", "")
    await llm.aclose()


@pytest.mark.asyncio
async def test_chat_openai_empty_content_diagnostic_notes_reasoning_present() -> None:
    """When the OpenAI-compat choice/message carries a `reasoning` field
    alongside empty `content`, the diagnostic must say reasoning WAS
    present — the signal that this is token-budget exhaustion, not some
    other cause."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": "",
                            "reasoning": "a very long discarded chain of thought",
                        }
                    }
                ]
            },
        )

    transport = httpx.MockTransport(handler)
    llm = _client(transport)

    with pytest.raises(LLMOutputInvalidError) as exc_info:
        await llm.chat([{"role": "user", "content": "hi"}], json_mode=True)

    message = str(exc_info.value).lower()
    assert "reasoning_present=true" in message.replace(" ", "")
    await llm.aclose()


@pytest.mark.asyncio
async def test_chat_openai_empty_content_logs_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    transport, _calls = _sequenced_transport([_ok_chat_response("")])
    llm = _client(transport)

    with caplog.at_level(logging.WARNING, logger="src.pipeline.llm.client"):
        with pytest.raises(LLMOutputInvalidError):
            await llm.chat([{"role": "user", "content": "hi"}], json_mode=True)

    assert any(
        "empty" in record.getMessage().lower() for record in caplog.records
    ), "an empty-content response must log a warning, not just raise silently"
    await llm.aclose()


@pytest.mark.asyncio
async def test_chat_native_raises_on_empty_string_content() -> None:
    transport, _calls = _sequenced_transport([_ok_native_response("")])
    llm = _client(transport, native_chat=True)

    with pytest.raises(LLMOutputInvalidError):
        await llm.chat([{"role": "user", "content": "hi"}], json_mode=True)

    await llm.aclose()


@pytest.mark.asyncio
async def test_chat_native_raises_on_whitespace_only_content() -> None:
    transport, _calls = _sequenced_transport([_ok_native_response("  \n  ")])
    llm = _client(transport, native_chat=True)

    with pytest.raises(LLMOutputInvalidError):
        await llm.chat([{"role": "user", "content": "hi"}], json_mode=True)

    await llm.aclose()


@pytest.mark.asyncio
async def test_chat_native_empty_content_diagnostic_notes_no_reasoning_present() -> (
    None
):
    transport, _calls = _sequenced_transport([_ok_native_response("")])
    llm = _client(transport, native_chat=True)

    with pytest.raises(LLMOutputInvalidError) as exc_info:
        await llm.chat([{"role": "user", "content": "hi"}], json_mode=True)

    message = str(exc_info.value).lower()
    assert "empty" in message
    assert "reasoning_present=false" in message.replace(" ", "")
    await llm.aclose()


@pytest.mark.asyncio
async def test_chat_native_empty_content_diagnostic_notes_thinking_present() -> None:
    """For the native `/api/chat` path, the presence signal is a top-level
    `thinking` field on the response payload (per the spec: "For
    `_chat_native`: check the response for a `thinking` field")."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "message": {"content": ""},
                "thinking": "a very long discarded chain of thought",
            },
        )

    transport = httpx.MockTransport(handler)
    llm = _client(transport, native_chat=True)

    with pytest.raises(LLMOutputInvalidError) as exc_info:
        await llm.chat([{"role": "user", "content": "hi"}], json_mode=True)

    message = str(exc_info.value).lower()
    assert "reasoning_present=true" in message.replace(" ", "")
    await llm.aclose()


@pytest.mark.asyncio
async def test_chat_native_empty_content_logs_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    transport, _calls = _sequenced_transport([_ok_native_response("")])
    llm = _client(transport, native_chat=True)

    with caplog.at_level(logging.WARNING, logger="src.pipeline.llm.client"):
        with pytest.raises(LLMOutputInvalidError):
            await llm.chat([{"role": "user", "content": "hi"}], json_mode=True)

    assert any(
        "empty" in record.getMessage().lower() for record in caplog.records
    ), "an empty-content response must log a warning, not just raise silently"
    await llm.aclose()


# ── §6 comment correction (ADR-021 §6) ───────────────────────────────────
#
# The `_chat_openai` json_mode comment currently claims switching to
# `_chat_native` (llm_ollama_native=True) is "a reliable thinking-off path".
# ADR-021 §6 established that neither path reliably suppresses reasoning for
# gpt-oss:20b — that claim is false and must be corrected (citing ADR-021
# §6), not merely deleted. Static source check, mirroring the precedent
# established in test_summary_text_pii.py / test_pii.py for pinning a
# docstring/comment's factual content via inspect.getsource.


def test_chat_openai_json_mode_comment_no_longer_claims_a_reliable_escape_hatch() -> (
    None
):
    source = inspect.getsource(LLMClient._chat_openai)
    assert "reliable thinking-off path" not in source, (
        "the json_mode comment must stop claiming _chat_native is a "
        "reliable escape hatch from reasoning-token exhaustion — ADR-021 §6 "
        "found neither path reliably suppresses reasoning for gpt-oss:20b"
    )
    assert "ADR-021" in source or "ADR 0021" in source, (
        "the corrected comment must cite ADR-021 §6, per the spec's "
        "instruction to correct (not merely delete) the inert claim"
    )
