"""Unit tests for the Redis-backed embedding cache
(``src/pipeline/llm/cache.py``).

Ported from hris ``packages/pipeline/src/pipeline/llm/cache.py``
(``phase3-source-dossier.md`` §4). No ``fakeredis`` dependency — a
minimal hand-rolled async stub implements only the surface
``CachedEmbedder`` actually calls: ``mget`` / ``pipeline`` / ``set`` /
``execute``.

The key-derivation tests independently reimplement the documented
formula (``sha256(f"{model}\\n{text}")``, key
``emb:v1:{model}:{digest}``) rather than reaching into
``CachedEmbedder``'s private ``_key`` method — that way the tests pin
the actual on-the-wire cache-key CONTRACT, not just "whatever the
private method currently does".
"""

from __future__ import annotations

import hashlib
import json
from unittest.mock import AsyncMock

import pytest

from src.pipeline.llm.cache import CachedEmbedder


def _expected_key(model: str, text: str) -> str:
    digest = hashlib.sha256(f"{model}\n{text}".encode()).hexdigest()
    return f"emb:v1:{model}:{digest}"


# ── Hand-rolled async Redis stub ─────────────────────────────────────────


class _FakePipeline:
    def __init__(self, redis: _FakeRedis) -> None:
        self._redis = redis
        self._ops: list[tuple[str, bytes, int | None]] = []

    def set(self, key: str, value: bytes, ex: int | None = None) -> _FakePipeline:
        self._ops.append((key, value, ex))
        return self

    async def execute(self) -> list[bool]:
        for key, value, ex in self._ops:
            self._redis.store[key] = value
            self._redis.set_calls.append((key, value, ex))
        applied = len(self._ops)
        self._ops = []
        return [True] * applied


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}
        self.mget_calls: list[list[str]] = []
        self.pipeline_calls: int = 0
        self.set_calls: list[tuple[str, bytes, int | None]] = []

    async def mget(self, keys: list[str]) -> list[bytes | None]:
        self.mget_calls.append(list(keys))
        return [self.store.get(k) for k in keys]

    def pipeline(self, transaction: bool = True) -> _FakePipeline:
        self.pipeline_calls += 1
        return _FakePipeline(self)


# ── Full miss ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_full_miss_calls_embed_once_and_writes_back_with_ttl() -> None:
    redis = _FakeRedis()
    llm = AsyncMock()
    llm.embed = AsyncMock(return_value=[[1.0, 2.0], [3.0, 4.0]])
    cache = CachedEmbedder(llm, redis, "nomic-embed-text", ttl_s=555)

    result = await cache.embed(["hello", "world"])

    assert result == [[1.0, 2.0], [3.0, 4.0]]
    llm.embed.assert_awaited_once_with(["hello", "world"])
    assert len(redis.set_calls) == 2
    assert all(ex == 555 for _key, _value, ex in redis.set_calls)


@pytest.mark.asyncio
async def test_default_ttl_is_ninety_days() -> None:
    redis = _FakeRedis()
    llm = AsyncMock()
    llm.embed = AsyncMock(return_value=[[1.0]])
    cache = CachedEmbedder(llm, redis, "m")

    await cache.embed(["x"])

    assert redis.set_calls[0][2] == 60 * 60 * 24 * 90


# ── Order preservation ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_order_preserved_for_mixed_hit_and_miss() -> None:
    redis = _FakeRedis()
    model = "m"
    texts = ["alpha", "beta", "gamma"]
    redis.store[_expected_key(model, "alpha")] = json.dumps([1.0]).encode()
    redis.store[_expected_key(model, "gamma")] = json.dumps([3.0]).encode()
    llm = AsyncMock()
    llm.embed = AsyncMock(return_value=[[2.0]])
    cache = CachedEmbedder(llm, redis, model)

    result = await cache.embed(texts)

    assert result == [[1.0], [2.0], [3.0]]  # original input order, not hit-then-miss
    llm.embed.assert_awaited_once_with(["beta"])


# ── Key derivation includes the model name (mutation guard) ─────────────


@pytest.mark.asyncio
async def test_cache_write_uses_the_documented_model_scoped_key() -> None:
    redis = _FakeRedis()
    llm = AsyncMock()
    llm.embed = AsyncMock(return_value=[[9.0]])
    cache = CachedEmbedder(llm, redis, "model-a")

    await cache.embed(["shared text"])

    assert _expected_key("model-a", "shared text") in redis.store


@pytest.mark.asyncio
async def test_switching_embedding_models_never_returns_a_stale_vector() -> None:
    redis = _FakeRedis()
    # Pre-seed as if model-a already cached a vector for this exact text.
    redis.store[_expected_key("model-a", "shared text")] = json.dumps([1.0]).encode()

    llm_b = AsyncMock()
    llm_b.embed = AsyncMock(return_value=[[2.0]])
    cache_b = CachedEmbedder(llm_b, redis, "model-b")

    result = await cache_b.embed(["shared text"])

    # model-b must NOT silently reuse model-a's cached vector.
    llm_b.embed.assert_awaited_once_with(["shared text"])
    assert result == [[2.0]]


# ── Empty input ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_empty_input_returns_empty_and_touches_neither_redis_nor_llm() -> None:
    redis = _FakeRedis()
    llm = AsyncMock()
    cache = CachedEmbedder(llm, redis, "m")

    result = await cache.embed([])

    assert result == []
    assert redis.mget_calls == []
    assert redis.pipeline_calls == 0
    llm.embed.assert_not_called()


# ── `_decode` never raises ────────────────────────────────────────────────


def test_decode_returns_none_for_malformed_json() -> None:
    assert CachedEmbedder._decode(b"not json {{{") is None


def test_decode_returns_none_for_non_list_payload() -> None:
    assert CachedEmbedder._decode(b'{"not": "a list"}') is None


def test_decode_returns_none_for_none_input() -> None:
    assert CachedEmbedder._decode(None) is None


def test_decode_returns_vector_for_valid_json_list() -> None:
    assert CachedEmbedder._decode(b"[1.0, 2.5, 3.0]") == [1.0, 2.5, 3.0]
