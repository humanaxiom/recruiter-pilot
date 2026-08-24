"""Unit tests for Settings — the single source of configuration truth.

Guards the two invariants that the rest of the port hangs off:

* the **768-d contract** (``llm_embedding_dim`` is the one number that the
  Neo4j vector indexes must agree with — see ``test_neo4j_bootstrap.py``),
* the **offline invariant** (``llm_base_url`` must never point at a cloud
  inference endpoint).

── Phase 4b additions ──────────────────────────────────────────────────────

Four new knobs the graph-projection drainer and the Neo4j-half of skill
normalisation must read from settings, never hard-code (the deliverable's own
words: "no hard-coded 0.92 / 0.88 / 50 / 200"):

* ``outbox_drain_batch_size`` (50)              — ``project_to_graph``'s
  per-call row limit.
* ``outbox_max_delivery_attempts`` (200)        — decision 2's poison-row
  dead-letter cap.
* ``skill_auto_merge_threshold`` (0.92)         — hris's ``AUTO_MERGE_THRESHOLD``.
* ``skill_tiebreaker_threshold`` (0.88)         — hris's ``TIEBREAKER_THRESHOLD``.

Each is asserted to exist with the stated default AND to be READ (not just
present as an unused field) — the "actually read" half lives in
``test_pipeline_skills_graph.py`` (thresholds) and
``test_worker_project_to_graph.py`` (batch size / dead-letter cap), which
patch ``get_settings`` and prove the returned value changes behaviour. This
file only pins the field itself existing with the right type/default/env
override, matching the existing style for every other setting in this file.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from pytest import MonkeyPatch

from src.settings import Settings, get_settings

# Hosts that would break the offline guarantee if they ever appeared in config.
CLOUD_HOSTS: tuple[str, ...] = (
    "api.openai.com",
    "openai.azure.com",
    "anthropic.com",
    "azure.com",
    "googleapis.com",
    "amazonaws.com",
    "cohere.ai",
    "mistral.ai",
    "huggingface.co",
    "together.xyz",
    "groq.com",
)

LOCAL_HOSTS: tuple[str, ...] = ("localhost", "127.0.0.1", "host.docker.internal")


# ── Defaults ───────────────────────────────────────────────────────────────


def test_postgres_defaults() -> None:
    s = Settings()
    assert s.postgres_dsn == "postgresql://app:app@postgres:5432/recruiter"
    assert s.postgres_pool_min == 2
    assert s.postgres_pool_max == 10


def test_postgres_dsn_is_raw_asyncpg_dsn_not_sqlalchemy_url() -> None:
    """asyncpg.create_pool rejects SQLAlchemy's ``postgresql+asyncpg://``."""
    dsn = Settings().postgres_dsn
    assert dsn.startswith("postgresql://")
    assert "+asyncpg" not in dsn
    assert "+psycopg" not in dsn


def test_neo4j_and_redis_defaults() -> None:
    s = Settings()
    assert s.neo4j_uri == "bolt://neo4j:7687"
    assert s.neo4j_user == "neo4j"
    assert s.neo4j_password == "recruiterpass"
    assert s.redis_url == "redis://redis:6379/0"


def test_llm_defaults() -> None:
    s = Settings()
    assert s.llm_base_url == "http://host.docker.internal:11434/v1"
    assert s.llm_model_generation == "gpt-oss:20b"
    assert s.llm_model_embedding == "nomic-embed-text"
    assert s.llm_timeout_s == 120.0


def test_embedding_dim_is_768() -> None:
    """CONTRACT: must equal the Neo4j vector-index ``vector.dimensions``."""
    assert Settings().llm_embedding_dim == 768


def test_blind_review_default_is_true() -> None:
    """Decision 4 — redaction is ON unless a recruiter opts out."""
    assert Settings().blind_review_default is True


def test_storage_and_pii_defaults() -> None:
    s = Settings()
    assert s.storage_dir == "/data"
    assert s.pii_key == ""  # env-supplied; empty default forces explicit config
    assert s.coverage_threshold == 80


def test_skill_hash_salt_default_is_empty() -> None:
    """ADR-008 — same discipline as ``pii_key``: empty by default, forcing an
    operator to explicitly configure it (``src.worker.main.startup`` fails
    loud on an empty salt, mirroring the existing ``PII_KEY`` check).

    Checked against the FIELD's declared default (not a fresh ``Settings()``
    instance) because the test session's own root ``conftest.py`` sets
    ``SKILL_HASH_SALT`` in the environment for the whole run (so that every
    OTHER test exercising real skill-name hashing doesn't have to configure
    it individually) — a live instance in this process legitimately picks
    that up, which is correct env-override behaviour, not a bug. See
    ``test_env_override_skill_hash_salt`` for the override path itself."""
    assert Settings.model_fields["skill_hash_salt"].default == ""


def test_env_override_skill_hash_salt(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("SKILL_HASH_SALT", "a-real-salt-value")
    assert Settings().skill_hash_salt == "a-real-salt-value"


def test_frontend_defaults() -> None:
    s = Settings()
    assert s.api_base_url == "http://api:8000"
    assert s.flask_secret_key == "dev-only"


# ── Offline invariant ──────────────────────────────────────────────────────


@pytest.mark.parametrize("cloud_host", CLOUD_HOSTS)
def test_llm_base_url_is_not_a_cloud_endpoint(cloud_host: str) -> None:
    assert cloud_host not in Settings().llm_base_url.lower()


def test_llm_base_url_points_at_a_local_ollama() -> None:
    url = Settings().llm_base_url.lower()
    assert any(host in url for host in LOCAL_HOSTS)
    assert url.startswith("http://")  # no TLS needed on loopback
    assert url.endswith("/v1")  # OpenAI-compatible surface


# ── Demo fields dropped ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "dropped", ["agent_model", "database_url", "max_review_iterations"]
)
def test_template_demo_fields_are_gone(dropped: str) -> None:
    assert dropped not in Settings.model_fields


# ── Env overrides + caching ────────────────────────────────────────────────


def test_get_settings_is_cached() -> None:
    assert get_settings() is get_settings()


def test_env_override_embedding_dim(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_EMBEDDING_DIM", "1024")
    assert Settings().llm_embedding_dim == 1024


def test_env_override_blind_review_default(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("BLIND_REVIEW_DEFAULT", "false")
    assert Settings().blind_review_default is False


def test_env_override_pii_key(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("PII_KEY", "c2VjcmV0LWtleS0zMi1ieXRlcy1iYXNlNjQ=")
    assert Settings().pii_key == "c2VjcmV0LWtleS0zMi1ieXRlcy1iYXNlNjQ="


def test_env_override_postgres_dsn(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("POSTGRES_DSN", "postgresql://u:p@localhost:5433/other")
    assert Settings().postgres_dsn == "postgresql://u:p@localhost:5433/other"


# ── Phase 4b: outbox drainer knobs ──────────────────────────────────────────


def test_outbox_drain_batch_size_default() -> None:
    """The ``project_to_graph`` drainer's per-call row limit. hris hard-coded
    this as a Python default parameter (``batch: int = 50``); it must instead
    be read from settings so an operator can tune it without a code change."""
    assert Settings().outbox_drain_batch_size == 50


def test_outbox_max_delivery_attempts_default() -> None:
    """Decision 2 — poison rows are capped, not retried forever (hris retries
    forever). Past this many failed delivery attempts a row is dead-lettered
    and skipped by the drainer."""
    assert Settings().outbox_max_delivery_attempts == 200


def test_outbox_drain_batch_size_is_a_positive_int() -> None:
    assert isinstance(Settings().outbox_drain_batch_size, int)
    assert Settings().outbox_drain_batch_size > 0


def test_outbox_max_delivery_attempts_is_a_positive_int() -> None:
    assert isinstance(Settings().outbox_max_delivery_attempts, int)
    assert Settings().outbox_max_delivery_attempts > 0


def test_env_override_outbox_drain_batch_size(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("OUTBOX_DRAIN_BATCH_SIZE", "10")
    assert Settings().outbox_drain_batch_size == 10


def test_env_override_outbox_max_delivery_attempts(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("OUTBOX_MAX_DELIVERY_ATTEMPTS", "3")
    assert Settings().outbox_max_delivery_attempts == 3


def test_outbox_drain_deadline_seconds_default() -> None:
    """F5 (security re-audit) — a per-drain wall-clock budget so a batch of
    slow (skill-heavy) rows can't hold the single Postgres transaction open
    indefinitely past the arq cron tick that invoked the drain."""
    assert Settings().outbox_drain_deadline_seconds == pytest.approx(4.0)


def test_outbox_max_skill_resolutions_per_drain_default() -> None:
    """F5 — bounds the total number of skill-name resolution round trips
    (embed()/chat_json() calls) attempted across an entire drain call."""
    assert Settings().outbox_max_skill_resolutions_per_drain == 200


def test_outbox_drain_deadline_seconds_is_positive() -> None:
    assert Settings().outbox_drain_deadline_seconds > 0


def test_outbox_max_skill_resolutions_per_drain_is_a_positive_int() -> None:
    assert isinstance(Settings().outbox_max_skill_resolutions_per_drain, int)
    assert Settings().outbox_max_skill_resolutions_per_drain > 0


def test_env_override_outbox_drain_deadline_seconds(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("OUTBOX_DRAIN_DEADLINE_SECONDS", "1.5")
    assert Settings().outbox_drain_deadline_seconds == pytest.approx(1.5)


def test_env_override_outbox_max_skill_resolutions_per_drain(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("OUTBOX_MAX_SKILL_RESOLUTIONS_PER_DRAIN", "5")
    assert Settings().outbox_max_skill_resolutions_per_drain == 5


# ── Phase 4b: skill-normalisation thresholds ────────────────────────────────


def test_skill_auto_merge_threshold_default() -> None:
    """hris's ``AUTO_MERGE_THRESHOLD`` module constant (0.92), now a setting."""
    assert Settings().skill_auto_merge_threshold == pytest.approx(0.92)


def test_skill_tiebreaker_threshold_default() -> None:
    """hris's ``TIEBREAKER_THRESHOLD`` module constant (0.88), now a setting."""
    assert Settings().skill_tiebreaker_threshold == pytest.approx(0.88)


def test_skill_auto_merge_threshold_is_above_the_tiebreaker_threshold() -> None:
    """The two define a [tiebreaker, auto_merge) grey zone — inverted
    thresholds would make every vector match either an auto-merge or a
    guaranteed miss, silently deleting the LLM-tiebreaker path entirely."""
    s = Settings()
    assert s.skill_auto_merge_threshold > s.skill_tiebreaker_threshold


@pytest.mark.parametrize(
    "name", ["skill_auto_merge_threshold", "skill_tiebreaker_threshold"]
)
def test_skill_thresholds_are_valid_cosine_similarity_bounds(name: str) -> None:
    value = getattr(Settings(), name)
    assert 0.0 < value < 1.0


def test_env_override_skill_auto_merge_threshold(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("SKILL_AUTO_MERGE_THRESHOLD", "0.95")
    assert Settings().skill_auto_merge_threshold == pytest.approx(0.95)


def test_env_override_skill_tiebreaker_threshold(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("SKILL_TIEBREAKER_THRESHOLD", "0.80")
    assert Settings().skill_tiebreaker_threshold == pytest.approx(0.80)


# ── FU-7 (ADR-021 §3) — honest résumé parse status ──────────────────────────
#
# ``resume_parse_max_tries`` is the retry ceiling `parse_resume`'s single
# `LLMUnavailableError` boundary reads via `ctx["job_try"]` (arq's per-job,
# 1-based attempt counter -- arq's `ctx` carries NO `max_tries` key of its
# own) to decide "let arq retry" vs "give up, record_parse_failure". The SAME
# number must also back `WorkerSettings.max_tries` in `src/worker/main.py` --
# see `test_worker_wiring.py` -- so arq's own retry ceiling and this
# boundary's give-up threshold can never silently drift apart.


def test_resume_parse_max_tries_default() -> None:
    assert Settings().resume_parse_max_tries == 5


def test_resume_parse_max_tries_is_a_positive_int() -> None:
    assert isinstance(Settings().resume_parse_max_tries, int)
    assert Settings().resume_parse_max_tries > 0


def test_env_override_resume_parse_max_tries(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("RESUME_PARSE_MAX_TRIES", "3")
    assert Settings().resume_parse_max_tries == 3


# ── FU-7 §2 (ADR-021 §2 / ADR-029) — fail-closed shortlist ranking ──────────
#
# ``shortlist_max_tries`` is the arq JOB-layer retry ceiling
# ``shortlist_job``'s ``RankingUnavailableError`` boundary reads (via
# ``ctx.get("job_try", 1)``) to decide "let arq retry" vs "give up, leave
# jobs.shortlist_state='awaiting_llm' visible". Unlike
# ``resume_parse_max_tries`` above (no upper bound), this one gets an UPPER
# sanity cap (the FU-7 residual named in the spec) via
# ``Field(default=20, ge=1, le=1000)`` — a misconfigured/huge value must fail
# loud at startup, not silently retry a shortlist run forever.
# ``shortlist_retry_defer_s`` is the arq defer between retries.


def test_shortlist_max_tries_default() -> None:
    assert Settings().shortlist_max_tries == 20


def test_shortlist_max_tries_is_a_positive_int() -> None:
    assert isinstance(Settings().shortlist_max_tries, int)
    assert Settings().shortlist_max_tries > 0


def test_env_override_shortlist_max_tries(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("SHORTLIST_MAX_TRIES", "7")
    assert Settings().shortlist_max_tries == 7


def test_shortlist_max_tries_rejects_zero(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("SHORTLIST_MAX_TRIES", "0")
    with pytest.raises(ValidationError):
        Settings()


def test_shortlist_max_tries_rejects_negative(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("SHORTLIST_MAX_TRIES", "-1")
    with pytest.raises(ValidationError):
        Settings()


def test_shortlist_max_tries_rejects_a_value_above_the_upper_sanity_cap(
    monkeypatch: MonkeyPatch,
) -> None:
    """The FU-7 residual: give this retry ceiling an UPPER bound, unlike
    ``resume_parse_max_tries`` which has none — ``Field(..., le=1000)``."""
    monkeypatch.setenv("SHORTLIST_MAX_TRIES", "1001")
    with pytest.raises(ValidationError):
        Settings()


def test_shortlist_max_tries_accepts_the_upper_sanity_cap_boundary(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("SHORTLIST_MAX_TRIES", "1000")
    assert Settings().shortlist_max_tries == 1000


def test_shortlist_retry_defer_s_default() -> None:
    assert Settings().shortlist_retry_defer_s == pytest.approx(45.0)


def test_shortlist_retry_defer_s_is_positive() -> None:
    assert Settings().shortlist_retry_defer_s > 0


def test_env_override_shortlist_retry_defer_s(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("SHORTLIST_RETRY_DEFER_S", "12.5")
    assert Settings().shortlist_retry_defer_s == pytest.approx(12.5)


# ── fix/regenerate-shortlist-no-feedback — 'ranking' staleness bound ────────
#
# The UI now polls on a real ``jobs.shortlist_state = 'ranking'`` fact set at
# enqueue time (see the ADR-021/ADR-029-style spec for this fix). A crashed
# worker must not pin the UI in a permanent "Regenerating…": ``get_shortlist_
# state`` treats a 'ranking' row older than this threshold as NOT ranking
# (without clearing the row). Named/defaulted here (never hard-coded in
# ``shortlist_service``) per CLAUDE.md's "config only via src/settings.py".
# Defaults to the worker's own ``job_timeout`` (``src/worker/main.py:162`` —
# 3600s) because a run genuinely cannot still be in flight past its own
# job-level timeout; the two are related by construction, not coincidence.


def test_shortlist_ranking_stale_after_s_default_matches_worker_job_timeout() -> None:
    assert Settings().shortlist_ranking_stale_after_s == pytest.approx(3600.0)


def test_shortlist_ranking_stale_after_s_is_positive() -> None:
    assert Settings().shortlist_ranking_stale_after_s > 0


def test_env_override_shortlist_ranking_stale_after_s(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("SHORTLIST_RANKING_STALE_AFTER_S", "120")
    assert Settings().shortlist_ranking_stale_after_s == pytest.approx(120.0)
