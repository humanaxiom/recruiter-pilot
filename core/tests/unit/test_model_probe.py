"""Tests for the model-acceptance profile — the numbers a model swap depends on.

The point of ``src.model_probe`` is that ``LLM_TIMEOUT_S`` and the token budget
stop being two independently-set literals in two files. So the tests that matter
are the ones pinning that the timeout is DERIVED, that a floor exists, and that
"accepted" is not something a partially-working model can claim.
"""

from __future__ import annotations

from src.model_probe import ModelProfile, PromptResult, render


def _r(prompt: str, *, ok: bool = True, latency: float = 30.0, tokens: int = 1536):
    return PromptResult(
        prompt=prompt,
        schema_valid=ok,
        min_tokens_needed=tokens,
        latency_s=latency,
        concurrency=4,
        thinking_chars=3116,
        structured_output=True,
    )


def _profile(*results: PromptResult) -> ModelProfile:
    p = ModelProfile(
        model="gpt-oss:20b",
        endpoint="http://peer:11434",
        transport="native",
        max_jobs=4,
    )
    p.results = list(results)
    return p


def test_a_model_is_accepted_only_when_every_prompt_works() -> None:
    """Partial credit is what let a model that could not do skill extraction
    stay in production while everything else looked fine."""
    assert _profile(_r("resume_core"), _r("resume_skills")).accepted
    assert not _profile(_r("resume_core"), _r("resume_skills", ok=False)).accepted


def test_an_unmeasured_model_is_never_accepted() -> None:
    """Absence of evidence must not read as evidence — an empty profile is
    'nobody has checked', which is exactly the state that shipped."""
    assert not _profile().accepted


def test_the_timeout_is_derived_from_the_slowest_measured_call() -> None:
    """The coupling that broke on 2026-08-21: the budget was raised in one file
    and the timeout left alone in another, turning empty responses into
    timeouts. Deriving it means nobody sets it by hand."""
    p = _profile(_r("a", latency=30.0), _r("b", latency=200.0))
    assert p.recommended_timeout_s == 400


def test_the_timeout_has_a_floor_a_quiet_probe_cannot_undercut() -> None:
    """A probe on an idle GPU measures the best case. Production is four jobs
    deep — the floor stops one lucky measurement setting a hair-trigger."""
    assert _profile(_r("a", latency=1.0)).recommended_timeout_s == 120


def test_the_token_budget_is_the_max_any_prompt_needed() -> None:
    """One floor for every call site. Per-call literals are what drifted: four
    of them held 1536/1024/2048/3072 while only one had been measured."""
    assert _profile(_r("a", tokens=1536), _r("b", tokens=4096)).recommended_max_tokens


def test_a_profile_round_trips_so_a_swap_is_reviewable_in_a_diff() -> None:
    p = _profile(_r("resume_skills", latency=42.0, tokens=2048))
    back = ModelProfile.from_json(p.to_json())
    assert back.model == p.model
    assert back.accepted == p.accepted
    assert back.recommended_timeout_s == p.recommended_timeout_s
    assert back.results[0].prompt == "resume_skills"


def test_a_rejected_model_says_so_in_words_not_just_a_flag() -> None:
    """An operator reads the report, not the dataclass."""
    out = render(_profile(_r("resume_skills", ok=False)))
    assert "NOT accepted" in out
    assert "Do not point the stack at it" in out


def test_the_report_names_the_concurrency_it_measured_at() -> None:
    """A latency figure without the contention it was measured under is the
    number that produced a 300s timeout for calls that take 35s when idle."""
    assert "concurrency: 4" in render(_profile(_r("resume_skills")))
