"""The live half of model acceptance — talks to a real model.

Split from :mod:`src.model_probe` deliberately: that module is pure and
unit-tested (the profile maths, the derived timeout, the accept/reject rule),
and this one cannot be, because its entire job is to make network calls to a
model. Keeping them apart means the rules a model swap depends on are covered
by the ordinary gate, while the measurement itself is exercised by running it.

Never imported by the application. Entry point is ``scripts/model-check.sh``.
"""

from __future__ import annotations

import argparse
import asyncio
import time
from pathlib import Path
from typing import Any

import httpx

from src.model_probe import ModelProfile, PromptResult, render

#: Tried smallest-first; the first budget that yields schema-valid JSON for
#: EVERY concurrent call is recorded as that prompt's requirement. Smallest-first
#: matters: the answer we want is the minimum that works, not merely a number
#: that happens to.
_PROBE_BUDGETS = (1536, 4096, 8192)

#: Per-call ceiling, and an ACCEPTANCE CRITERION rather than an arbitrary large
#: number. A model that cannot answer one prompt within this under the worker's
#: own concurrency is not deployable here whatever it eventually returns: résumés
#: would sit in `parsing` for the length of an upload batch, and the job-layer
#: retry budget would multiply it threefold before the circuit breaker opened.
#:
#: The first version used 1800s, which meant a single starved call could stall
#: the whole run for half an hour with no output and no verdict — measuring
#: patience rather than the model.
_CALL_TIMEOUT_S = 420.0


def _load_cases(fixtures: Path) -> list[tuple[str, Any, Any]]:
    """``(prompt_name, prompt, schema)`` built from REAL fixture documents.

    Real inputs, not a synthetic stand-in, and this is the hard-won part. While
    diagnosing the 2026-08-21 incident, a hand-written prompt of comparable
    length returned valid JSON on both transports at both budgets — while the
    real extracted résumé failed three times out of three. A harness built on a
    stand-in would have certified the broken configuration as healthy.
    """
    from src.pipeline.parsing.chunk import chunk_resume
    from src.pipeline.parsing.extract import extract_text
    from src.prompts import load_prompt
    from src.schemas import JDExtracted
    from src.schemas.resumes import ResumeCore, ResumeSkillDetails

    resumes = sorted(fixtures.glob("resumes/*_resume.pdf"))
    jds = sorted(fixtures.glob("JDs/*.docx"))
    if not resumes or not jds:
        raise SystemExit(f"model-check: no fixture résumés/JDs under {fixtures}")

    chunks = chunk_resume(extract_text(resumes[0].read_bytes(), "application/pdf"))
    jd_text = extract_text(
        jds[0].read_bytes(),
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ).full_text

    return [
        ("resume_core_v1", load_prompt("resume_core_v1", chunks=chunks), ResumeCore),
        (
            "resume_skills_v2",
            load_prompt("resume_skills_v2", chunks=chunks),
            ResumeSkillDetails,
        ),
        (
            "jd_extract_v1",
            load_prompt("jd_extract_v1", jd_text=jd_text),
            JDExtracted,
        ),
    ]


async def _one_call(
    http: httpx.AsyncClient,
    endpoint: str,
    model: str,
    messages: Any,
    schema: Any,
    budget: int,
) -> tuple[bool, float, int]:
    """One schema-constrained call. Returns (valid, seconds, thinking_chars).

    Uses Ollama's native ``/api/chat`` with ``format`` set to the pydantic
    schema — schema-constrained decoding, so the model cannot ramble past the
    budget and return nothing. That is the failure mode this whole exercise
    exists to remove, and constraining generation removes it at the source
    rather than budgeting around it.
    """
    body = {
        "model": model,
        "messages": list(messages),
        "stream": False,
        "think": False,
        "format": schema.model_json_schema(),
        "options": {"num_predict": budget, "temperature": 0},
    }
    started = time.monotonic()
    try:
        resp = await http.post(endpoint + "/api/chat", json=body)
        payload = resp.json().get("message", {}) if resp.status_code == 200 else {}
    except (httpx.HTTPError, ValueError):
        return (False, time.monotonic() - started, 0)
    content = str(payload.get("content") or "").strip()
    thinking = len(str(payload.get("thinking") or ""))
    try:
        schema.model_validate_json(content)
    except Exception:  # noqa: BLE001 - any validation failure is a failed probe
        return (False, time.monotonic() - started, thinking)
    return (True, time.monotonic() - started, thinking)


async def measure(
    *, endpoint: str, model: str, max_jobs: int, transport: str, fixtures: Path
) -> ModelProfile:
    """Measure every real prompt AT THE WORKER'S OWN CONCURRENCY.

    The concurrency is the whole point. A single uncontended call to the failing
    prompt returned valid JSON in ~35s on both transports at both budgets — it
    could not reproduce a failure that was hitting production every time,
    because production runs ``max_jobs`` of them against one GPU. A latency
    measured one call at a time is real and useless: it is the number that let
    ``LLM_TIMEOUT_S=300`` look generous for calls that took over 300s.
    """
    profile = ModelProfile(
        model=model, endpoint=endpoint, transport=transport, max_jobs=max_jobs
    )
    async with httpx.AsyncClient(timeout=_CALL_TIMEOUT_S) as http:
        for name, prompt, schema in _load_cases(fixtures):
            print(f"  probing {name} at concurrency {max_jobs} ...", flush=True)
            result: PromptResult | None = None
            for budget in _PROBE_BUDGETS:
                # Per-budget progress, because silence is indistinguishable from
                # a hang. The first live run sat on one prompt for twenty
                # minutes with no output, and there was no way to tell whether
                # it was working, wedged, or queued behind another model on a
                # shared GPU. A tool built to remove false confidence must not
                # be opaque about its own state.
                print(f"    budget {budget} ...", end="", flush=True)
                started = time.monotonic()
                outcomes = await asyncio.gather(
                    *(
                        _one_call(
                            http, endpoint, model, prompt.messages, schema, budget
                        )
                        for _ in range(max_jobs)
                    )
                )
                passed = sum(1 for ok, _s, _t in outcomes if ok)
                print(
                    f" {passed}/{max_jobs} valid in {time.monotonic() - started:.0f}s",
                    flush=True,
                )
                if all(ok for ok, _s, _t in outcomes):
                    result = PromptResult(
                        prompt=name,
                        schema_valid=True,
                        min_tokens_needed=budget,
                        latency_s=round(max(s for _o, s, _t in outcomes), 1),
                        concurrency=max_jobs,
                        thinking_chars=max(t for _o, _s, t in outcomes),
                        structured_output=True,
                    )
                    break
            profile.results.append(
                result
                or PromptResult(
                    prompt=name,
                    schema_valid=False,
                    min_tokens_needed=None,
                    latency_s=0.0,
                    concurrency=max_jobs,
                    thinking_chars=0,
                    structured_output=True,
                    note=(
                        f"no budget up to {_PROBE_BUDGETS[-1]} produced valid JSON "
                        f"within {_CALL_TIMEOUT_S:.0f}s per call at concurrency "
                        f"{max_jobs}"
                    ),
                )
            )
    return profile


def _write_profile(profile: ModelProfile, model: str, out_dir: Path) -> Path:
    """Sync on purpose: ruff's ASYNC240 rightly objects to blocking pathlib
    calls inside an async function, and this one runs after all I/O is done."""
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{model.replace(':', '-')}.json"
    out.write_text(profile.to_json(), encoding="utf-8")
    return out


async def main(args: argparse.Namespace) -> int:
    from src.settings import get_settings

    settings = get_settings()
    endpoint = str(settings.llm_base_url).rsplit("/v1", 1)[0]
    model = str(settings.llm_model_generation)
    max_jobs = args.concurrency

    print(f"▶ measuring {model} at {endpoint} (concurrency {max_jobs})\n", flush=True)
    profile = await measure(
        endpoint=endpoint,
        model=model,
        max_jobs=max_jobs,
        # What was actually PROBED, not what the app is configured to use.
        # `_one_call` always uses Ollama's native /api/chat, so reporting the
        # application's transport here would certify a path this run never
        # touched — the precise class of false confidence this tool exists to
        # remove. The gap between the two is recorded in ADR-045 as the next
        # slice: the harness should probe the transport the app actually runs.
        transport="ollama-native",
        fixtures=args.fixtures,
    )
    print("\n" + render(profile))

    written = _write_profile(profile, model, args.out)
    print("\nprofile written: " + str(written))
    return 0 if profile.accepted else 1


def _parse_args() -> argparse.Namespace:
    """CLI arguments, not env vars: CLAUDE.md forbids `os.environ` outside
    `settings.py`, and a meta-test enforces it. The rule is right — scattered
    env reads are how configuration drifts out of one place — and a harness is
    not exempt from it."""
    p = argparse.ArgumentParser(description="Measure a model against the real prompts.")
    p.add_argument("--fixtures", type=Path, default=Path("/repo/fixtures"))
    p.add_argument("--out", type=Path, default=Path("/repo/docs/model-profiles"))
    p.add_argument("--concurrency", type=int, default=4)
    return p.parse_args()


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(asyncio.run(main(_parse_args())))
