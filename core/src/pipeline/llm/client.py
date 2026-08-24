"""OpenAI-compatible LLM client.

One class — ``LLMClient`` — covers chat, JSON-strict chat, and embeddings
against any OpenAI-compatible endpoint. The same code talks to Ollama in
dev and vLLM in prod; the differences live in env (LLM_BASE_URL).

Reliability features baked in:

* exponential backoff with jitter on 5xx / connection / timeout errors
* circuit breaker (N consecutive failures -> open for cooldown_s)
* JSON-mode: validates against a pydantic schema and retries once with
  the validator error appended, so a model that produced *almost*-valid
  JSON gets one shot to correct itself before we raise

Privacy: prompt/response BODIES are never logged — no log site in this
module emits them, under any setting. Résumé content goes into prompts and
must be treated as PII: what we log is a prompt HASH plus status/latency/
token counts, and validation failures are logged/raised as a PII-FREE digest
(field path + error type only — pydantic v2 embeds the offending input value
in ``str(ValidationError)``, which for a résumé IS the candidate's name/phone/
email). ``debug_llm`` is a reserved, currently INERT flag: it turns nothing
on today.

Ported from hris ``packages/pipeline/src/pipeline/llm/client.py``
(``phase3-source-dossier.md`` §3). Behaviorally verbatim — only the
import paths and the ``structlog`` -> stdlib ``logging`` swap changed.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import re
import time
from typing import TYPE_CHECKING, Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

if TYPE_CHECKING:
    from collections.abc import Sequence

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

# Status codes worth retrying. 429 included because Ollama returns it
# when KEEP_ALIVE evicts a model mid-flight.
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})

# Strips a ```json ... ``` (or plain ```) fence the model wraps around
# its output, plus any prose before the first '{' or after the last '}'.
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


class LLMUnavailableError(RuntimeError):
    """Circuit breaker is open OR all retries exhausted on a transient error."""


class LLMOutputInvalidError(RuntimeError):
    """Model produced non-JSON or schema-invalid JSON after the retry budget."""


#: The minimum ``max_tokens`` any JSON-extraction call may use against a
#: reasoning model. NOT a response-size estimate: on ``gpt-oss:20b`` the
#: DISCARDED thinking trace is charged against this budget BEFORE any JSON is
#: emitted (ADR-021 §6), and ``think:false`` does not reliably suppress it on
#: either the OpenAI-compat or the native path. A value below this does not
#: truncate the answer — it returns an EMPTY one.
#:
#: 8192 is MEASURED, not chosen, and it moved once already. ADR-044 probed 4096
#: against the classifier prompt; on 2026-08-22 `scripts/model-check.sh` probed
#: every real prompt on an idle peer and found `resume_skills_v2` managed only
#: 2 of 4 concurrent calls at 4096 and 4 of 4 at 8192 — while 1536 returned
#: nothing at all for any prompt. A value tuned against ONE prompt was still too
#: low for the prompt the whole product depends on.
#:
#: That is why the number now comes from `docs/model-profiles/<model>.json`'s
#: `recommended_max_tokens` rather than from anyone's judgement, and why
#: `tests/unit/test_reasoning_token_floor.py` enforces it by scanning the
#: source instead of trusting a comment.
REASONING_JSON_MIN_TOKENS = 8192


def _empty_content_message(reasoning_present: bool) -> str:
    """PII-free diagnostic for an empty/whitespace-only chat completion.

    An empty ``content`` is the classic "reasoning model burned its whole
    ``max_tokens`` budget on a discarded reasoning/thinking trace" failure
    (ADR-021 §6). ``reasoning_present`` distinguishes that cause (a
    reasoning/thinking field WAS present alongside the empty content) from
    some other reason the model returned nothing.
    """
    return (
        "response content was empty (possibly reasoning model exhausted "
        f"token budget); reasoning_present={reasoning_present}"
    )


def validation_error_digest(exc: ValidationError) -> str:
    """A PII-FREE one-line digest of a pydantic ``ValidationError``.

    MERGE-BLOCKING PRIVACY INVARIANT: ``str(ValidationError)`` in pydantic v2
    embeds ``input_value=...`` — the offending value itself. On the résumé
    path that value IS the candidate's name/phone/email, and a wrong-typed
    field from a small local model is the ROUTINE failure (it is the reason
    the self-correction retry exists). Left as ``str(exc)``, that PII lands in
    the log line, in the raised ``LLMOutputInvalidError``, and — via
    ``record_parse_failure`` — in the CLEARTEXT ``resumes.failure_reason``
    column, right next to the pgcrypto-encrypted columns.

    This digest keeps only what is actually diagnostic — the field path and
    the error type (``candidate.phone: string_type``) — and never the value.
    The full, unredacted error may still go into the RETRY PROMPT (in-memory,
    sent to a local model, never persisted): that is what lets the model
    self-correct. Only the logged/raised/persisted path gets this digest.
    """
    parts = [f"{'.'.join(str(p) for p in e['loc'])}: {e['type']}" for e in exc.errors()]
    return "; ".join(parts)[:500]


# Depth ceiling for the NUL-strip walk. Bounds recursion so deeply-nested LLM
# JSON (prompt-injectable via ``[[[...]]]``) can't blow the Python stack. A
# real résumé's parsed structure is only a few levels deep, so 200 is far past
# anything legitimate while still well under CPython's ~1000 recursion limit.
_MAX_JSON_DEPTH = 200


def _strip_nuls(value: Any, _depth: int = 0) -> Any:
    """Recursively strip U+0000 from LLM-parsed JSON.

    ``extract._sanitize`` strips NULs from DOCUMENT text, but LLM output never
    passes through it and ``json.loads`` happily accepts a ``\\u0000`` escape.
    Postgres ``text``/``jsonb`` reject U+0000 (``UntranslatableCharacterError``
    / ``CharacterNotInRepertoireError``), so a single NUL in the model's output
    raises INSIDE the write transaction — uncaught, straight into an arq retry
    loop that re-runs the whole LLM pipeline. The résumé is attacker-supplied
    and goes verbatim into the prompt, so prompt injection (or a looping model)
    can trigger it on demand.

    Bounded: past ``_MAX_JSON_DEPTH`` levels this raises ``RecursionError``,
    which ``chat_json`` catches and funnels into ``LLMOutputInvalidError`` — the
    same path deeply-nested input takes when ``json.loads`` itself overflows.
    """
    if _depth > _MAX_JSON_DEPTH:
        raise RecursionError(f"LLM JSON nested past {_MAX_JSON_DEPTH} levels")
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, dict):
        return {
            (k.replace("\x00", "") if isinstance(k, str) else k): _strip_nuls(
                v, _depth + 1
            )
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_strip_nuls(v, _depth + 1) for v in value]
    return value


class LLMClient:
    """Async OpenAI-compatible client with retries, circuit breaker, JSON mode.

    Use as an async context manager so the underlying httpx client closes:

        async with LLMClient(base_url, gen_model, emb_model) as llm:
            ...
    """

    def __init__(
        self,
        base_url: str,
        model_gen: str,
        model_emb: str,
        *,
        timeout_s: float = 120.0,
        max_retries: int = 2,
        breaker_threshold: int = 10,
        breaker_cooldown_s: float = 30.0,
        debug_llm: bool = False,
        native_chat: bool = False,
        expected_dim: int | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        # Ollama's native API lives at the host root, not under /v1.
        self._native_chat = native_chat
        self._native_root = self._base.removesuffix("/v1")
        self._gen = model_gen
        self._emb = model_emb
        self._timeout_s = timeout_s
        self._max_retries = max_retries
        self._breaker_threshold = breaker_threshold
        self._breaker_cooldown_s = breaker_cooldown_s
        # RESERVED, INERT: nothing reads this. It exists so a future verbose
        # mode has a settings-sourced switch to hang off; today NO log site in
        # this module emits prompt or response bodies regardless of its value.
        self._debug = debug_llm
        # 768-d is a hard contract: it must equal the `vector.dimensions` of the
        # Neo4j indexes (both are sourced from settings.llm_embedding_dim). When
        # set, embed() rejects any vector of a different length — otherwise a
        # mis-pointed llm_model_embedding produces vectors that only blow up
        # much later, at Neo4j write time.
        self._expected_dim = expected_dim

        self._owns_http = http_client is None
        self._http = http_client or httpx.AsyncClient(timeout=timeout_s)

        self._consecutive_failures = 0
        self._opened_at: float | None = None

    # ---------------- public API ----------------

    async def chat(
        self,
        messages: Sequence[dict[str, str]],
        *,
        temperature: float = 0.0,
        max_tokens: int = 2048,
        json_mode: bool = False,
    ) -> str:
        if self._native_chat:
            return await self._chat_native(messages, temperature, max_tokens, json_mode)
        return await self._chat_openai(messages, temperature, max_tokens, json_mode)

    async def _chat_openai(
        self,
        messages: Sequence[dict[str, str]],
        temperature: float,
        max_tokens: int,
        json_mode: bool,
    ) -> str:
        body: dict[str, Any] = {
            "model": self._gen,
            "messages": list(messages),
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if json_mode:
            # OpenAI JSON mode + a best-effort `think:false`. On reasoning
            # models (glm-4.7, gpt-oss, deepseek-r1) the reasoning trace
            # counts against max_tokens; on a large prompt it fills the budget
            # so generation stops before any JSON is emitted and `content`
            # comes back empty (raised as LLMOutputInvalidError below). We
            # never use the reasoning text. NOTE (ADR-021 §6): neither this
            # OpenAI-compat path NOR the native /api/chat path (_chat_native)
            # reliably suppresses reasoning for gpt-oss:20b — `think:false` is
            # only intermittently honoured on both, so switching to
            # llm_ollama_native is NOT a guaranteed escape hatch from
            # reasoning-token exhaustion, only a different roll of the dice.
            body["response_format"] = {"type": "json_object"}
            body["think"] = False
        payload = await self._post_json("/chat/completions", body)
        choices = payload.get("choices") or []
        if not choices:
            raise LLMOutputInvalidError("response had no choices")
        message = choices[0].get("message") or {}
        content = message.get("content")
        if not isinstance(content, str):
            raise LLMOutputInvalidError("response choice had no string content")
        if not content.strip():
            reasoning_present = bool(
                message.get("reasoning") or choices[0].get("reasoning")
            )
            msg = _empty_content_message(reasoning_present)
            log.warning("llm.empty_content %s", msg)
            raise LLMOutputInvalidError(msg)
        return content

    async def _chat_native(
        self,
        messages: Sequence[dict[str, str]],
        temperature: float,
        max_tokens: int,
        json_mode: bool,
    ) -> str:
        """Ollama's native /api/chat. It ALSO passes ``think: false``, but
        per ADR-021 §6 neither this path nor the OpenAI-compat one reliably
        suppresses reasoning for gpt-oss:20b — an empty ``content`` (the
        reasoning trace ate the whole token budget) can still come back and is
        raised as LLMOutputInvalidError below. Content may also arrive fenced
        (```json …```); ``_extract_json`` downstream strips that."""
        body: dict[str, Any] = {
            "model": self._gen,
            "messages": list(messages),
            "stream": False,
            "options": {"temperature": temperature, "num_predict": max_tokens},
        }
        if json_mode:
            body["format"] = "json"
            body["think"] = False
        payload = await self._post_json(
            "/api/chat", body, url=f"{self._native_root}/api/chat"
        )
        content = (payload.get("message") or {}).get("content")
        if not isinstance(content, str):
            raise LLMOutputInvalidError("native chat response had no string content")
        if not content.strip():
            reasoning_present = bool(payload.get("thinking"))
            msg = _empty_content_message(reasoning_present)
            log.warning("llm.empty_content %s", msg)
            raise LLMOutputInvalidError(msg)
        return content

    async def chat_json(
        self,
        messages: Sequence[dict[str, str]],
        schema: type[T],
        *,
        temperature: float = 0.0,
        max_tokens: int = 2048,
        max_retries: int = 1,
    ) -> T:
        """Call chat, parse JSON, validate against ``schema``.

        On parse/validation failure, retries up to ``max_retries`` extra
        times, each time appending the validator error so the model has
        a chance to self-correct. Raises ``LLMOutputInvalidError`` on
        final failure.

        Two DIFFERENT renderings of the same failure, deliberately:

        * ``prompt_error`` — the full, unredacted error. Goes ONLY into the
          retry prompt: in-memory, sent to the local model, never persisted.
          Redacting it here would gut the self-correction loop.
        * ``safe_error`` — a PII-free digest (see ``validation_error_digest``).
          This is the ONLY rendering that is logged, raised, and — via
          ``record_parse_failure`` — written to ``resumes.failure_reason``.
        """
        attempt_messages: list[dict[str, str]] = list(messages)
        prompt_error: str | None = None
        safe_error: str | None = None

        for attempt in range(max_retries + 1):
            if prompt_error is not None and attempt > 0:
                attempt_messages = [
                    *attempt_messages,
                    {
                        "role": "user",
                        "content": (
                            "Your previous response failed validation: "
                            f"{prompt_error}. Return ONLY valid JSON matching "
                            "the schema. No prose, no fences."
                        ),
                    },
                ]
            raw = await self.chat(
                attempt_messages,
                temperature=temperature,
                max_tokens=max_tokens,
                json_mode=True,
            )
            try:
                obj = _strip_nuls(json.loads(self._extract_json(raw)))
                return schema.model_validate(obj)
            except json.JSONDecodeError as exc:
                # JSONDecodeError's str() is "msg: line L column C (char N)" —
                # position only, never the document body. Safe as-is.
                prompt_error = safe_error = str(exc)[:500]
            except RecursionError:
                # Deeply-nested LLM JSON (prompt-injectable ``[[[...]]]``) can
                # overflow the stack in ``json.loads`` itself OR in the
                # ``_strip_nuls`` walk — RecursionError is neither JSONDecode nor
                # Validation, so without this it would escape ``chat_json`` (and
                # ``parse_resume``) uncaught. Treat it as invalid LLM output and
                # funnel it into the same LLMOutputInvalidError path. The reason
                # is PII-free (structural only — no document body).
                prompt_error = safe_error = "llm output nested too deeply to parse"
            except ValidationError as exc:
                prompt_error = str(exc)[:500]
                safe_error = validation_error_digest(exc)
            log.warning(
                "llm.json_invalid",
                extra={
                    "attempt": attempt,
                    "error": safe_error,  # PII-free digest only — never str(exc)
                    "prompt_hash": self._prompt_hash(attempt_messages),
                },
            )

        raise LLMOutputInvalidError(safe_error or "no detail")

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        body: dict[str, Any] = {"model": self._emb, "input": list(texts)}
        payload = await self._post_json("/embeddings", body)
        data = payload.get("data") or []
        if len(data) != len(texts):
            raise LLMOutputInvalidError(
                f"expected {len(texts)} embeddings, got {len(data)}"
            )
        out: list[list[float]] = []
        for item in data:
            vec = item.get("embedding")
            if not isinstance(vec, list):
                raise LLMOutputInvalidError("embedding entry missing 'embedding' list")
            if self._expected_dim is not None and len(vec) != self._expected_dim:
                # Fail HERE, at the source, not in Phase 4 at Neo4j write time:
                # the index dimension is fixed at bootstrap from the same
                # settings value this is checked against.
                raise LLMOutputInvalidError(
                    f"embedding model {self._emb!r} returned a {len(vec)}-d vector; "
                    f"expected {self._expected_dim}-d (must match the Neo4j "
                    f"vector indexes)"
                )
            out.append([float(x) for x in vec])
        return out

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def __aenter__(self) -> LLMClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    # ---------------- internals ----------------

    async def _post_json(
        self, path: str, body: dict[str, Any], *, url: str | None = None
    ) -> dict[str, Any]:
        self._check_breaker()
        # ``path`` is the log label; ``url`` overrides the target for
        # endpoints that don't live under the OpenAI /v1 base (native Ollama).
        url = url or f"{self._base}{path}"
        prompt_hash = self._prompt_hash(body.get("messages") or body.get("input"))

        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):
            start = time.perf_counter()
            try:
                response = await self._http.post(url, json=body)
            except (
                httpx.ConnectError,
                httpx.ReadTimeout,
                httpx.RemoteProtocolError,
            ) as exc:
                last_exc = exc
                self._on_failure()
                self._log_attempt(path, prompt_hash, attempt, start, error=str(exc))
                await self._sleep_backoff(attempt)
                continue

            if response.status_code in _RETRY_STATUSES:
                last_exc = httpx.HTTPStatusError(
                    f"{response.status_code}",
                    request=response.request,
                    response=response,
                )
                self._on_failure()
                self._log_attempt(
                    path, prompt_hash, attempt, start, status=response.status_code
                )
                await self._sleep_backoff(attempt)
                continue

            if response.status_code >= 400:
                # 4xx (non-429): not retryable, fail fast.
                self._on_failure()
                self._log_attempt(
                    path, prompt_hash, attempt, start, status=response.status_code
                )
                # Do NOT embed ``response.text`` — an upstream 4xx body can
                # reflect the request (résumé/prompt text, candidate PII), and
                # this message flows into the cleartext, blind-review-exposed
                # ``resumes.failure_reason`` column. Status code alone is the
                # PII-free diagnostic; full body detail stays in ``_log_attempt``.
                raise LLMUnavailableError(f"HTTP {response.status_code}")

            payload: dict[str, Any] = response.json()
            self._on_success()
            usage = payload.get("usage") or {}
            self._log_attempt(
                path,
                prompt_hash,
                attempt,
                start,
                status=response.status_code,
                tokens_in=usage.get("prompt_tokens"),
                tokens_out=usage.get("completion_tokens"),
                ok=True,
            )
            return payload

        # exhausted retries
        raise LLMUnavailableError(
            f"all {self._max_retries + 1} attempts failed: {last_exc!r}"
        )

    def _check_breaker(self) -> None:
        if self._opened_at is None:
            return
        elapsed = time.monotonic() - self._opened_at
        if elapsed < self._breaker_cooldown_s:
            raise LLMUnavailableError(
                f"circuit breaker open ({self._breaker_cooldown_s - elapsed:.1f}s left)"
            )
        # Cooldown elapsed — half-open: allow one trial through. If it
        # fails, _on_failure will re-open immediately.
        self._opened_at = None
        self._consecutive_failures = 0

    def _on_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._breaker_threshold:
            self._opened_at = time.monotonic()
            log.warning(
                "llm.breaker_open",
                extra={
                    "failures": self._consecutive_failures,
                    "cooldown_s": self._breaker_cooldown_s,
                },
            )

    def _on_success(self) -> None:
        self._consecutive_failures = 0
        self._opened_at = None

    async def _sleep_backoff(self, attempt: int) -> None:
        # 1, 2, 4, 8 ... capped, with ±25% jitter.
        # Non-cryptographic randomness is intentional (jitter spread).
        base = min(8.0, 1.0 * (2**attempt))
        await asyncio.sleep(base * (1.0 + random.uniform(-0.25, 0.25)))  # noqa: S311

    def _log_attempt(
        self,
        path: str,
        prompt_hash: str,
        attempt: int,
        start: float,
        *,
        status: int | None = None,
        tokens_in: int | None = None,
        tokens_out: int | None = None,
        error: str | None = None,
        ok: bool = False,
    ) -> None:
        # Never log raw prompt content — only the hash, status, latency, and
        # token counts. This holds UNCONDITIONALLY: there is no debug_llm
        # branch here (the flag is inert, see __init__), so no setting can
        # turn prompt/response bodies into log lines.
        log.info(
            "llm.attempt",
            extra={
                "path": path,
                "attempt": attempt,
                "status": status,
                "ok": ok,
                "error": error,
                "prompt_hash": prompt_hash,
                "latency_ms": int((time.perf_counter() - start) * 1000),
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
            },
        )

    @staticmethod
    def _prompt_hash(payload: object) -> str:
        try:
            blob = json.dumps(payload, sort_keys=True, default=str)
        except TypeError:
            blob = repr(payload)
        return hashlib.sha256(blob.encode()).hexdigest()[:16]

    @staticmethod
    def _extract_json(raw: str) -> str:
        """Best-effort: strip ```json fences and leading/trailing prose.

        Returns the substring most likely to be JSON. Caller still has to
        json.loads it; this just removes the common envelope noise.
        """
        s = raw.strip()
        fence = _FENCE_RE.search(s)
        if fence is not None:
            return fence.group(1).strip()
        # Look for the first '{' / '[' and the matching close, naïvely.
        starts = [s.find("{"), s.find("[")]
        starts = [i for i in starts if i != -1]
        if not starts:
            return s
        start = min(starts)
        ends = [s.rfind("}"), s.rfind("]")]
        end = max(ends)
        if end <= start:
            return s
        return s[start : end + 1]
