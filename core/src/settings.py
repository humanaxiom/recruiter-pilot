"""Central configuration — the single source of truth, loaded from env.

Nothing else in the codebase may read ``os.environ``. Two fields carry
contracts that other modules assert against:

* ``llm_embedding_dim`` — the Neo4j vector indexes are built from this number
  (see ``src/worker/neo4j_bootstrap.py``); the two must never drift apart.
* ``llm_base_url`` — must always point at a local, OpenAI-compatible endpoint
  (Ollama on the host). This project is offline by design: no cloud inference.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings

from src.schemas.matching import MatchWeights


class Settings(BaseSettings):
    # ── Postgres (transactional store; raw asyncpg DSN, not a SQLAlchemy URL) ─
    postgres_dsn: str = "postgresql://app:app@postgres:5432/recruiter"
    postgres_pool_min: int = 2
    postgres_pool_max: int = 10

    # ── Neo4j (skill/experience graph + vector retrieval) ────────────────────
    neo4j_uri: str = "bolt://neo4j:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "recruiterpass"

    # ── Redis (arq broker + embedding cache) ─────────────────────────────────
    redis_url: str = "redis://redis:6379/0"

    # ── Inference (Ollama on metal, OpenAI-compatible /v1 — never a cloud host)
    llm_base_url: str = "http://host.docker.internal:11434/v1"
    llm_model_generation: str = "gpt-oss:20b"
    llm_model_embedding: str = "nomic-embed-text"
    llm_embedding_dim: int = 768  # CONTRACT: == Neo4j `vector.dimensions`
    llm_timeout_s: float = 120.0
    # LLMClient's own HTTP-layer retry budget (httpx-level, inside ONE arq job
    # try) — do not confuse with `resume_parse_max_tries` below, which is the
    # arq JOB-layer retry ceiling (a whole job re-enqueue, across possibly
    # many `LLMClient` calls each with their own `llm_max_retries` budget).
    llm_max_retries: int = 2
    llm_breaker_threshold: int = 10  # consecutive failures before the breaker opens
    llm_breaker_cooldown_s: float = 30.0
    # Ollama's OpenAI-compat layer only intermittently honours `think: false`, so a
    # reasoning model (the default gpt-oss:20b is one) can burn its whole token budget
    # on a discarded reasoning trace and return empty content. The native /api/chat
    # route honours it reliably — flip this on if JSON-mode parses come back empty.
    llm_ollama_native: bool = False
    # RESERVED / INERT: passed to LLMClient, read by nothing. It does NOT gate a
    # prompt-logging path, because there isn't one — no log site in the client
    # emits prompt or response bodies at any setting (only a prompt hash), and
    # validation errors are logged as a PII-free digest. Flipping this on today
    # changes no behaviour; if a verbose mode is ever added it must not log
    # prompt bodies, which carry résumé PII.
    debug_llm: bool = False

    # ── Embedding cache (Redis read-through) ─────────────────────────────────
    embedding_cache_ttl_s: int = 60 * 60 * 24 * 90  # 90 days

    # ── Storage (filesystem BlobStore root — no MinIO/S3) ────────────────────
    storage_dir: str = "/data"

    # ── API auth (FU-4: keyed roles) ─────────────────────────────────────────
    # LEGACY, DETECTION-ONLY (FU-4/D1). Phase 6's single `API_KEY` switch is
    # superseded by the four role keys below and no longer gates auth at all.
    # The field is KEPT so `validate_startup_auth_config` can spot a stale
    # `API_KEY` still sitting in a deploy's .env and refuse to boot — with
    # `extra="ignore"`, a bare env-var rename would otherwise fall through to
    # auth-disabled (fail-open) silently.
    api_key: str = ""
    # The four role keys. Auth is DISABLED iff ALL FOUR are empty (local dev;
    # fail-open by EXPLICIT configuration, never by omission-in-code —
    # src.api.deps.log_auth_mode logs a loud WARNING at startup in that case,
    # and every caller resolves to Role.ADMIN). Auth is ENABLED iff ANY is
    # non-empty: the presented X-API-Key must match exactly one of them
    # (constant-time, UTF-8 bytes) or the request is rejected with 401; a
    # resolved role outside a route's allowed set is a 403.
    api_key_admin: str = ""
    api_key_recruiter: str = ""
    api_key_hiring_manager: str = ""
    api_key_auditor: str = ""

    # ── CAS + session auth (FU-5, ADR-019 §10/§10a/§10b) ──────────────────────
    # §10 supersedes §8's HMAC X-Actor-Assertion design — no assertion secret
    # exists in this model. Disabled by default (§10b); the all-disabled
    # default configuration must boot clean (see `validate_startup_auth_config`
    # below, which is unchanged by this slice — CAS adds no new startup-fatal
    # check).
    cas_enabled: bool = False
    cas_server_url: str = "https://cas.sfu.ca/cas"
    cas_validate_route: str = "/serviceValidate"
    cas_service_base_url: str = "http://localhost:8000"
    cas_service_from_request: bool = False
    cas_verify_tls: bool = True
    cas_dev_fake_user: str = ""
    session_cookie_name: str = "ra_session"
    session_ttl_hours: int = 8
    session_idle_refresh_hours: int = 1
    session_cookie_secure: bool = False
    session_cookie_samesite: str = "lax"
    # ADR-019 §10a — the ratified default-admin CAS allowlist value. A real
    # operational identity, deliberately committed (not PII), env-overridable
    # per deployment.
    default_admin_cas_username: str = "asalah"
    # fix/cas-post-login-frontend-redirect — the Flask frontend and the
    # FastAPI backend are different origins, so a bare relative `Location`
    # header resolves against the API's own origin, not the UI. Empty
    # (default) = same-origin = today's behaviour, unchanged. When set, the
    # "landing" redirects (post-validate success; the CAS-disabled dev
    # passthroughs) are prefixed with this base — see
    # `src.api.routes.auth._landing_url`.
    cas_frontend_base_url: str = ""

    # ── Privacy ──────────────────────────────────────────────────────────────
    pii_key: str = ""  # env-supplied pgcrypto key for the app.pii_key GUC
    blind_review_default: bool = True  # decision 4 — redaction ON by default
    # ADR-008 — the salt folded into every NON-VOCAB skill's canonical Skill-
    # graph key (``h:sha256(salt + normalised_name)[:32]``). Empty by default,
    # same discipline as ``pii_key`` above: ``src.worker.main.startup`` refuses
    # to start on an empty salt (an UNSALTED hash of a closed-ish set of likely
    # candidate names is dictionary-attackable — precompute hashes of common
    # names and confirm a candidate is in the graph). Rotating this value
    # changes every non-vocab skill's key, which requires re-projecting the
    # whole graph (every job/résumé re-parsed) to reconnect REQUIRES/HAS_SKILL
    # edges under the new keys.
    skill_hash_salt: str = ""

    # ── Gates ────────────────────────────────────────────────────────────────
    coverage_threshold: int = 80

    # ── Phase 4b: graph-projection outbox drainer ─────────────────────────────
    # hris hard-codes both as Python default parameters / module constants;
    # CLAUDE.md forbids hard-coded tunables, so they live here instead.
    outbox_drain_batch_size: int = 50
    # Decision 2 — poison rows are capped, not retried forever (hris retries
    # forever). The drainer's SELECT excludes rows at/past this many failed
    # delivery attempts — dead-lettered, not deleted, not retried.
    outbox_max_delivery_attempts: int = 200
    # F5 (security re-audit) — the whole batch (SELECT + every row's model
    # round trips) runs under ONE Postgres transaction with no deadline; a
    # handful of skill-heavy rows can hold that transaction open well past
    # the arq cron tick that invoked the drain. `project_to_graph` always
    # attempts at least one row (forward progress guaranteed even if that one
    # row alone busts the budget), then stops dispatching further rows once
    # either bound is hit — the untouched rows are simply left for the next
    # drain tick (no attempt increment, no dead-lettering).
    outbox_drain_deadline_seconds: float = 4.0
    outbox_max_skill_resolutions_per_drain: int = 200

    # ── Phase 4b: skill-normalisation (Neo4j half) thresholds ─────────────────
    # hris's AUTO_MERGE_THRESHOLD / TIEBREAKER_THRESHOLD module constants.
    # [tiebreaker, auto_merge) is the LLM-tiebreaker grey zone.
    skill_auto_merge_threshold: float = 0.92
    skill_tiebreaker_threshold: float = 0.88

    # ── Phase 4c: matching / ranking tunables (ADR 0021 port) ─────────────────
    # Every default below is copied verbatim from hris
    # ``packages/pipeline/src/pipeline/config.py`` lines 99-159 and mirrors
    # ``src.schemas.matching.MatchWeights``' defaults. CLAUDE.md forbids
    # hard-coded tunables, so the orchestrator sources these via
    # ``weights_from_settings`` below rather than the in-code MatchWeights
    # defaults. Top-level blend + the five structured sub-weights each sum to
    # ~1.0 (the MatchWeights validator enforces it on build).
    match_structured: float = 0.6
    match_evidence: float = 0.3
    match_motivation: float = 0.1
    match_skill: float = 0.40
    match_experience: float = 0.25
    match_education: float = 0.10
    match_seniority: float = 0.15
    match_vector: float = 0.10
    match_must_have_miss_penalty: float = 0.5
    match_implied_experience_relief: float = 0.75
    match_recency_recent_years: int = 2
    match_recency_mid_years: int = 5
    match_recency_recent: float = 1.0
    match_recency_mid: float = 0.7
    match_recency_old: float = 0.4
    match_overqual_ratio: float = 2.0
    match_overqual_slope: float = 0.1
    match_overqual_floor: float = 0.8
    match_education_partial: float = 0.5
    match_education_field_fuzz: float = 0.85
    match_seniority_floor: float = 0.5
    match_implied_seniority_factor: float = 1.5
    match_implied_min_coverage: float = 0.5
    match_evidence_met_confidence: float = 0.7
    match_evidence_partial_weight: float = 0.5
    match_evidence_verify_fuzz: float = 0.85
    # ADR-022 follow-up #4: minimum evidence-quote length, in characters.
    # Lowered 32 -> 16 by human decision (security FINDING 4) — at 32 the floor
    # blanked genuine short credentials and demoted them met -> missing. Must
    # stay equal to MatchWeights().evidence_min_quote_chars and to
    # tests/evals/thresholds.toml's [evidence].min_quote_chars.
    match_evidence_min_quote_chars: int = 16
    match_motivation_min_confidence: float = 0.7
    # Semantic skill matching (ADR 0020) + latency caps (ADR 0021).
    match_family_weight: float = 0.5
    match_non_matchable_families: str = "other,domain"
    # ROADMAP A2, Phase 3.3 slice 2 (2026-08-19 decision memo): the parse-time
    # classifier's output (Skill.classified_categories) must never move
    # ranking until this is explicitly switched on. Live measurement against
    # the real tailnet gpt-oss:20b found the classifier stably mis-families
    # domain-expert phrases, and family credit is transitive across a whole
    # résumé, so an unreviewed misfire would tell a recruiter a candidate
    # holds a qualification they don't. Default False; record the fact,
    # defer the scoring change (ADR-040/ADR-041 precedent).
    match_use_classified_families: bool = False
    match_coarse_k: int = 50
    match_evidence_k: int = 15
    # Blocker #10: recruiter-assistant has NO synchronous reverse-match endpoint
    # (nothing on a proxied request path to protect from LLM fan-out), so it
    # inherits hris's CURRENT worker-path default (> 0), never the pre-ADR-0023
    # synchronous-endpoint value of 0.
    match_reverse_evidence_k: int = 10
    match_llm_concurrency: int = 4
    match_evidence_max_tokens: int = 2048

    # ── FU-7 (ADR-021 §3): honest résumé parse status ─────────────────────────
    # The arq JOB-layer retry ceiling `parse_resume`'s `LLMUnavailableError`
    # boundary reads (via `ctx["job_try"]`, arq's 1-based per-job attempt
    # counter) to decide "let arq retry" vs "give up, record_parse_failure".
    # NOT `llm_max_retries` above, which is the LLMClient HTTP-layer retry
    # budget inside a single call. The SAME value also backs
    # `WorkerSettings.max_tries` in `src/worker/main.py`, so arq's own retry
    # ceiling and this boundary's give-up threshold can never silently drift
    # apart.
    resume_parse_max_tries: int = 5

    # ── FU-7 §2 (ADR-021 §2 / ADR-029): fail-closed shortlist ranking ─────────
    # The arq JOB-layer retry ceiling ``shortlist_job``'s
    # ``RankingUnavailableError`` boundary reads (via ``ctx.get("job_try", 1)``)
    # to decide "let arq retry" vs "give up, leave jobs.shortlist_state=
    # 'awaiting_llm' visible". Unlike ``resume_parse_max_tries`` above (no upper
    # bound), this one gets an UPPER sanity cap (the FU-7 residual) so a
    # misconfigured/huge value fails loud at startup rather than silently
    # retrying a shortlist run forever.
    shortlist_max_tries: int = Field(default=20, ge=1, le=1000)
    # The arq defer (seconds) between fail-closed shortlist retries.
    shortlist_retry_defer_s: float = 45.0

    # ── fix/regenerate-shortlist-no-feedback — 'ranking' staleness bound ──────
    # ``jobs.shortlist_state = 'ranking'`` is set by the API route at enqueue
    # and cleared/overwritten by the worker on every terminal path. A row this
    # old cannot still be genuinely in flight — the worker would have been
    # killed by its own job timeout — so ``get_shortlist_state`` treats it as
    # NOT ranking (without clearing the row) once it is older than this bound.
    # Defaults to the worker's own ``job_timeout`` (``src/worker/main.py:162``
    # — 3600s). F5 (review findings, 2026-08-18): that default is a
    # reasonable starting point, NOT because the two clocks measure the same
    # thing — they don't. ``job_timeout`` bounds worker EXECUTION time once a
    # job starts running. This bound has to cover enqueue→now
    # (``jobs.shortlist_state`` is set to ``'ranking'`` at enqueue time, in
    # ``routes/shortlist.py:62``, before the job is even picked up), so it
    # also has to absorb however long the job sits queued — and queue wait is
    # unbounded in principle (``max_jobs = 4``, so a burst of Regenerates can
    # queue behind each other with no cap on how long the wait is). A crashed
    # worker can never permanently pin the UI in "Regenerating…" either way,
    # but sizing this purely off ``job_timeout`` under-covers a busy queue.
    shortlist_ranking_stale_after_s: float = 3600.0

    # ── Flask viewer ─────────────────────────────────────────────────────────
    api_base_url: str = "http://api:8000"
    flask_secret_key: str = "dev-only"

    # ── Build provenance (Phase 4c) ───────────────────────────────────────────
    # Optional build-provenance metadata threaded into PipelineMeta.git_sha via
    # MatchingContext. pydantic-settings maps this to the `GIT_SHA` env var
    # (no env_prefix is configured above, so the field name is used verbatim).
    # None when unset — matches the prior (pre-Settings) `os.environ.get`
    # behaviour exactly.
    git_sha: str | None = None

    model_config = {"env_file": ".env", "extra": "ignore"}

    @property
    def auth_enabled(self) -> bool:
        """The single source of truth for "is the auth boundary on?".

        ``False`` iff ALL FOUR role keys are empty. The legacy ``api_key``
        field is deliberately NOT consulted — a stale ``API_KEY`` must fail
        loud through ``validate_startup_auth_config``, never quietly enable
        auth in an undocumented way.
        """
        return any(
            (
                self.api_key_admin,
                self.api_key_recruiter,
                self.api_key_hiring_manager,
                self.api_key_auditor,
            )
        )


# The env-var names of the four role keys, in the order they are reported to a
# misconfigured operator. Kept next to the fields they mirror.
_ROLE_KEY_ENV_VARS: tuple[str, ...] = (
    "API_KEY_ADMIN",
    "API_KEY_RECRUITER",
    "API_KEY_HIRING_MANAGER",
    "API_KEY_AUDITOR",
)


def validate_startup_auth_config(settings: Settings) -> None:
    """Refuse to boot on an auth configuration that would silently fail open
    or silently collapse two roles into one (FU-4).

    Called from ``src.api.main``'s lifespan, alongside the ADR-008
    ``SKILL_HASH_SALT`` refusal. Raises ``RuntimeError``; never logs, and
    never includes a key VALUE in the message it raises.

    * **D1 — legacy ``API_KEY``**: a non-empty ``settings.api_key`` is a hard
      failure regardless of whether the new fields are also set. ``Settings``
      has ``extra="ignore"``, so a stale ``API_KEY`` with none of the new keys
      configured would otherwise land in auth-DISABLED mode — a fail-open
      regression from Phase 6.
    * **Collision refusal**: two CONFIGURED (non-empty) role keys that are
      byte-identical collapse two roles into one (e.g. an auditor key that
      also opens every recruiter-only route). Two EMPTY fields are not a
      collision — that is simply "not configured".
    * **F1b (security finding, ``fix/auth-boundary-fails-open``) — CAS
      enabled with zero role keys configured**: ``cas_enabled=True`` (a real
      deploy, not the single-operator local-dev default) with all four role
      keys empty means ``settings.auth_enabled`` is ``False`` —
      ``resolve_role`` then resolves ``Role.ADMIN`` for EVERY caller,
      regardless of any header, which combined with the per-route session
      gate closing (``require_session_role`` now 403ing ``user is None``)
      would only leave the API-key half of the boundary permanently open.
      A live audit against real Postgres proved this exact configuration
      lets a cookie-less, key-less caller reach every write route with no
      credential at all. Refusing to boot makes this misconfiguration
      impossible to ship by accident, on the same "refuse rather than
      silently fail open" discipline as the two checks above. Scoped to
      ``cas_enabled=True`` only — the all-disabled local-dev default
      (``Settings()``) must keep booting clean.
    """
    if settings.cas_enabled and not settings.auth_enabled:
        raise RuntimeError(
            "cas_enabled is True but none of the four role-key env vars are "
            "configured (" + ", ".join(_ROLE_KEY_ENV_VARS) + ") — refusing to "
            "start: with CAS enabled and auth disabled, resolve_role trivially "
            "grants admin authority to every caller regardless of credential, "
            "which silently fails the whole auth boundary open. Configure at "
            "least one of the four role keys."
        )

    if settings.api_key:
        raise RuntimeError(
            "API_KEY is set but is no longer used — FU-4 replaced the single "
            "API key with per-role keys. Remove API_KEY from the environment "
            "and configure the roles you need instead: "
            + ", ".join(_ROLE_KEY_ENV_VARS)
            + "."
        )

    configured: dict[str, str] = {}
    for env_var, value in zip(
        _ROLE_KEY_ENV_VARS,
        (
            settings.api_key_admin,
            settings.api_key_recruiter,
            settings.api_key_hiring_manager,
            settings.api_key_auditor,
        ),
        strict=True,
    ):
        if not value:
            continue
        previous = configured.get(value)
        if previous is not None:
            raise RuntimeError(
                f"role keys {previous} and {env_var} are byte-identical — "
                "refusing to start: two roles sharing one key silently "
                "collapses them into the more privileged of the two. Give "
                "every configured role its own distinct key."
            )
        configured[value] = env_var


@lru_cache
def get_settings() -> Settings:
    return Settings()


def weights_from_settings(settings: Settings) -> MatchWeights:
    """Build a validated ``MatchWeights`` from flat settings (ADR 0021 port).

    Kept IN ``src/settings.py`` (not a sibling ``matching/config.py`` the way
    hris splits it) per CLAUDE.md's "config only via src/settings.py". The
    ``MatchWeights`` validator enforces that ``structured+evidence+motivation``
    and the five sub-weights each sum to ~1.0, so a misconfigured ``.env`` fails
    fast (ValueError) at the start of a matching run rather than silently
    skewing ranks.
    """
    return MatchWeights(
        structured=settings.match_structured,
        evidence=settings.match_evidence,
        motivation=settings.match_motivation,
        skill=settings.match_skill,
        experience=settings.match_experience,
        education=settings.match_education,
        seniority=settings.match_seniority,
        vector=settings.match_vector,
        must_have_miss_penalty=settings.match_must_have_miss_penalty,
        implied_experience_relief=settings.match_implied_experience_relief,
        recency_recent_years=settings.match_recency_recent_years,
        recency_mid_years=settings.match_recency_mid_years,
        recency_recent=settings.match_recency_recent,
        recency_mid=settings.match_recency_mid,
        recency_old=settings.match_recency_old,
        overqual_ratio=settings.match_overqual_ratio,
        overqual_slope=settings.match_overqual_slope,
        overqual_floor=settings.match_overqual_floor,
        education_partial=settings.match_education_partial,
        education_field_fuzz=settings.match_education_field_fuzz,
        seniority_floor=settings.match_seniority_floor,
        implied_seniority_factor=settings.match_implied_seniority_factor,
        implied_min_coverage=settings.match_implied_min_coverage,
        evidence_met_confidence=settings.match_evidence_met_confidence,
        evidence_partial_weight=settings.match_evidence_partial_weight,
        evidence_verify_fuzz=settings.match_evidence_verify_fuzz,
        evidence_min_quote_chars=settings.match_evidence_min_quote_chars,
        motivation_min_confidence=settings.match_motivation_min_confidence,
    )


def non_matchable_families_from_settings(settings: Settings) -> tuple[str, ...]:
    """Parse the comma-separated non-matchable-families setting into a tuple of
    lower-cased family names (empty entries dropped)."""
    return tuple(
        part.strip().lower()
        for part in settings.match_non_matchable_families.split(",")
        if part.strip()
    )
