"""Redis-backed cache for embedding vectors.

Embeddings are deterministic for a fixed (model, text) pair, so we get
big savings by caching them — re-running matches doesn't re-embed every
resume chunk. Keys include the model name so switching models doesn't
return stale vectors silently.

Storage format: JSON-encoded list of floats. ~6 KB per 768-d vector.
TTL is 90 days by default (see Settings.embedding_cache_ttl_s).

Ported from hris ``packages/pipeline/src/pipeline/llm/cache.py``
(``phase3-source-dossier.md`` §4). Behaviorally verbatim — only the
import paths and the ``structlog`` -> stdlib ``logging`` swap changed.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    import redis.asyncio as aioredis

    from src.pipeline.llm.client import LLMClient

log = logging.getLogger(__name__)

_KEY_PREFIX = "emb:v1"


class CachedEmbedder:
    """Read-through cache: hit Redis, fall through to LLMClient.embed for misses.

    Preserves input order. Safe to share across async tasks (the underlying
    redis client and httpx client are both async-safe).
    """

    def __init__(
        self,
        llm: LLMClient,
        redis: aioredis.Redis[bytes],
        model: str,
        *,
        ttl_s: int = 60 * 60 * 24 * 90,
    ) -> None:
        self._llm = llm
        self._redis = redis
        self._model = model
        self._ttl_s = ttl_s

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []

        keys = [self._key(t) for t in texts]
        cached_raw = await self._redis.mget(keys)
        results: list[list[float] | None] = [self._decode(raw) for raw in cached_raw]

        miss_indices = [i for i, v in enumerate(results) if v is None]
        if miss_indices:
            miss_texts = [texts[i] for i in miss_indices]
            new_vecs = await self._llm.embed(miss_texts)

            # Write-back. Pipeline avoids N round-trips.
            pipe = self._redis.pipeline(transaction=False)
            for idx, vec in zip(miss_indices, new_vecs, strict=True):
                results[idx] = vec
                pipe.set(keys[idx], self._encode(vec), ex=self._ttl_s)
            await pipe.execute()

        log.info(
            "embedding_cache.lookup",
            extra={
                "total": len(texts),
                "hits": len(texts) - len(miss_indices),
                "misses": len(miss_indices),
            },
        )

        # All slots filled by now (either from cache or from llm.embed).
        return [v for v in results if v is not None]

    # ---------------- internals ----------------

    def _key(self, text: str) -> str:
        digest = hashlib.sha256(f"{self._model}\n{text}".encode()).hexdigest()
        return f"{_KEY_PREFIX}:{self._model}:{digest}"

    @staticmethod
    def _encode(vec: list[float]) -> bytes:
        return json.dumps(vec, separators=(",", ":")).encode()

    @staticmethod
    def _decode(raw: bytes | None) -> list[float] | None:
        if raw is None:
            return None
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return None
        if not isinstance(parsed, list):
            return None
        return [float(x) for x in parsed]
