"""Integration tests -- FU-6 slice 8 (ADR-020 SS6): "the watchers are
watched" -- auditor read-logging, against a REAL Postgres (testcontainers)
via a real ASGI app and a real CAS session cookie.

What a REAL Postgres proves that the mocked-conn route tests
(``test_route_jobs.py``/``test_route_resumes.py``/``test_route_shortlist.py``'s
new "FU-6 slice 8" sections) structurally cannot:

* a real auditor session's read really inserts EXACTLY ONE ``audit_log`` row
  satisfying the real ``audit_log_actor_identity`` CHECK constraint
  (``actor_kind='user'`` with ``actor_user_id`` set and ``actor_service``
  NULL) -- not just that the route called some function with plausible
  arguments,
* a non-auditor session's IDENTICAL read writes EXACTLY ZERO rows -- proven
  by a real ``COUNT(*)`` against the real table, not a mocked assertion,
* a 404 (a nonexistent resource) writes ZERO rows -- nothing was read, so
  nothing is logged, even for a real auditor session.

**Scope decision (a coordinator judgment, pinned identically in the unit
route-test files' "FU-6 slice 8" sections -- see those for the full
rationale).** Only DELIBERATE single-subject / bulk reads are logged:

* ``GET /jobs/{id}``                        -> ``read_job``
* ``GET /resumes/{id}``                     -> ``read_resume``
* ``GET /shortlist/{id}``                   -> ``read_shortlist_entry``
* ``GET /jobs/{id}/shortlist/export``       -> ``read_shortlist_export``

The polled list-index routes (``GET /jobs``, ``GET /jobs/{id}/resumes``,
``GET /jobs/{id}/shortlist`` -- hit by the Flask frontend every ~3s, and
carrying no specific subject) are deliberately NOT logged; a dedicated test
below proves each writes zero rows for a real auditor session, against the
real table.

Auditor is UNSCOPED (ADR-020 SS4) -- no ``job_assignees`` seeding is needed
for any test in this file; an auditor session always sees every seeded
row. ``users.role`` is set directly via SQL when seeding the auditor (and
comparison recruiter/admin) rows -- see ``test_job_scoping_pg.py``'s module
docstring for why this is the only way to get a real, cookie-backed
non-default-role session.

Nothing in this slice exists yet (no route writes ``read_job`` /
``read_resume`` / ``read_shortlist_entry`` / ``read_shortlist_export`` rows
at all), so every "auditor writes exactly one row" test below is expected to
fail on a real row COUNT of 0. RED half of the TDD cycle.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from testcontainers.postgres import PostgresContainer

from src.api import deps
from src.api.deps import get_arq
from src.api.routes import auth as auth_routes
from src.api.routes import jobs as jobs_routes
from src.api.routes import resumes as resumes_routes
from src.api.routes import shortlist as shortlist_routes
from src.errors import AppError
from src.models.ddl import init_schema
from src.models.pool import get_db
from src.pipeline.matching.orchestrator import ShortlistResult, ShortlistResultEntry
from src.schemas.matching import DEFAULT_WEIGHTS, PipelineMeta, ScoreBreakdown
from src.services import pii as pii_service
from src.services.shortlist_service import persist_shortlist
from src.settings import Settings

_JD = "We need a senior backend engineer. " * 3

TEST_PII_KEY = "integration-test-pii-key-do-not-use-in-prod"


@pytest.fixture(scope="session")
def pg_dsn() -> Iterator[str]:
    with PostgresContainer("postgres:16-alpine") as pg:
        yield re.sub(r"\+\w+", "", pg.get_connection_url(), count=1)


@pytest.fixture
async def pg_pool(pg_dsn: str) -> AsyncIterator[asyncpg.Pool]:
    pool = await asyncpg.create_pool(dsn=pg_dsn, min_size=2, max_size=8)
    await init_schema(pool)
    async with pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE job_assignees, sessions, users, audit_log, jobs, "
            "resumes, shortlist_entries, outbox CASCADE"
        )
    try:
        yield pool
    finally:
        await pool.close()


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "cas_enabled": True,
        "cas_server_url": "https://cas.example.edu/cas",
        "session_cookie_name": "ra_session",
        "session_ttl_hours": 8,
        "default_admin_cas_username": "nobody-uses-this-default",
        "skill_hash_salt": "test-salt",
        "pii_key": TEST_PII_KEY,
    }
    base.update(overrides)
    return Settings(**base)


def _unique_username(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _build_app(pool: asyncpg.Pool) -> FastAPI:
    app = FastAPI()
    app.include_router(auth_routes.router)
    app.include_router(jobs_routes.router)
    app.include_router(resumes_routes.router)
    app.include_router(shortlist_routes.router)

    async def _get_db_override() -> AsyncIterator[Any]:
        async with pool.acquire() as conn:
            yield conn

    app.dependency_overrides[get_db] = _get_db_override
    app.dependency_overrides[get_arq] = lambda: MagicMock(enqueue_job=AsyncMock())
    # RBAC (require_role) is orthogonal to this slice's read-logging -- every
    # route exercised here is readable by every Role anyway, and auth is
    # disabled by default in these Settings (no api_key_* configured), so
    # resolve_role always resolves Role.ADMIN regardless of any header. Left
    # un-overridden deliberately: the read-log decision is driven entirely by
    # the CAS SESSION identity (resolve_user), never the key role.

    @app.exception_handler(AppError)
    async def _app_error_handler(_request: Any, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status, content={"detail": exc.message, "code": exc.code}
        )

    return app


async def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _login_and_get_sid(
    client: AsyncClient, settings: Settings, username: str
) -> str:
    resp = await client.get("/auth/cas/validate", params={"ticket": "any-ticket"})
    assert resp.status_code == 302
    sid = resp.cookies.get(settings.session_cookie_name)
    assert sid is not None
    return sid


async def _insert_job(
    pool: asyncpg.Pool, *, title: str = "Senior Backend Engineer"
) -> uuid.UUID:
    async with pool.acquire() as conn:
        job_id: uuid.UUID = await conn.fetchval(
            "INSERT INTO jobs (title, description_raw) VALUES ($1, $2) RETURNING id",
            title,
            _JD,
        )
    return job_id


async def _insert_user(
    pool: asyncpg.Pool, *, cas_username: str, role: str
) -> uuid.UUID:
    """Seed a ``users`` row with an EXPLICIT role, set directly via SQL -- see
    ``test_job_scoping_pg.py``'s module docstring for why no provisioning
    path can do this."""
    async with pool.acquire() as conn:
        user_id: uuid.UUID = await conn.fetchval(
            "INSERT INTO users (cas_username, display_name, email, role) "
            "VALUES ($1, $2, $3, $4) RETURNING id",
            cas_username,
            cas_username,
            None,
            role,
        )
    return user_id


async def _login_as_seeded_user(
    pool: asyncpg.Pool,
    client: AsyncClient,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    *,
    role: str,
) -> tuple[uuid.UUID, str]:
    """Seed a ``users`` row with ``role`` and drive a real CAS login for that
    SAME username, returning ``(user_id, session_cookie_value)``."""
    username = _unique_username(role)
    user_id = await _insert_user(pool, cas_username=username, role=role)
    monkeypatch.setattr(
        auth_routes.cas_service,
        "validate_ticket",
        AsyncMock(return_value=username),
    )
    sid = await _login_and_get_sid(client, settings, username)
    return user_id, sid


def _patch_settings(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    # resume/shortlist service calls pii_service.set_pii_key, which reads the
    # PII key from this module's own get_settings binding.
    monkeypatch.setattr(pii_service, "get_settings", lambda: settings)


async def _insert_resume(
    pool: asyncpg.Pool, job_id: uuid.UUID, *, name: str = "Jane Smith"
) -> uuid.UUID:
    """Seed a real, PII-encrypted ``resumes`` row -- requires
    ``_patch_settings`` to have already run so ``pii_service.get_settings``
    resolves ``TEST_PII_KEY``."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            await pii_service.set_pii_key(conn)
            name_enc = await pii_service.encrypt(conn, name)
            resume_id: uuid.UUID = await conn.fetchval(
                """
                INSERT INTO resumes (
                    job_id, blob_key, original_filename, mime_type,
                    file_size_bytes, sha256, consent_acknowledged, status,
                    candidate_name
                ) VALUES ($1, $2, 'resume.pdf', 'application/pdf', 1024, $3,
                           TRUE, 'parsed', $4)
                RETURNING id
                """,
                job_id,
                f"resumes/{uuid.uuid4().hex}.pdf",
                uuid.uuid4().hex,
                name_enc,
            )
    return resume_id


def _breakdown() -> ScoreBreakdown:
    return ScoreBreakdown(
        skill=0.8,
        experience=0.6,
        education=0.4,
        seniority=0.5,
        vector=0.3,
        structured=0.55,
    )


def _meta() -> PipelineMeta:
    return PipelineMeta(
        model_gen="test-gen",
        model_emb="test-emb",
        prompt_versions={"shortlist_evidence": "shortlist_evidence_v1"},
        weights=DEFAULT_WEIGHTS,
        git_sha="abc123",
        generated_at=dt.datetime.now(dt.UTC),
        timings_ms={},
    )


async def _seed_shortlist_entry(
    pool: asyncpg.Pool, *, job_id: uuid.UUID, resume_id: uuid.UUID
) -> uuid.UUID:
    """Write ONE ranked shortlist row via the real Phase 4d write path
    (``persist_shortlist``) -- mirrors ``test_shortlist_scoping_pg.py``'s
    own helper."""
    result = ShortlistResult(
        job_id=job_id,
        entries=[
            ShortlistResultEntry(
                resume_id=resume_id,
                rank=1,
                score_final=0.9,
                score_structured=0.8,
                score_evidence=0.7,
                breakdown=_breakdown(),
                evidence=None,
            )
        ],
        pipeline_meta=_meta(),
    )
    async with pool.acquire() as conn:
        async with conn.transaction():
            await persist_shortlist(conn, result)
        row = await conn.fetchrow(
            "SELECT id FROM shortlist_entries WHERE job_id = $1 AND resume_id = $2",
            job_id,
            resume_id,
        )
    assert row is not None
    entry_id: uuid.UUID = row["id"]
    return entry_id


async def _audit_rows(
    pool: asyncpg.Pool, *, action: str, subject_id: uuid.UUID
) -> list[asyncpg.Record]:
    async with pool.acquire() as conn:
        return await conn.fetch(
            "SELECT actor_kind, actor_user_id, actor_service, action, "
            "subject_type, subject_id FROM audit_log "
            "WHERE action = $1 AND subject_id = $2",
            action,
            subject_id,
        )


async def _audit_row_count(pool: asyncpg.Pool) -> int:
    async with pool.acquire() as conn:
        count: int = await conn.fetchval("SELECT count(*) FROM audit_log")
    return count


# ── GET /jobs/{job_id} -> "read_job" ────────────────────────────────────


@pytest.mark.asyncio
async def test_get_job_auditor_writes_exactly_one_read_job_row(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings()
    _patch_settings(monkeypatch, settings)
    app = _build_app(pg_pool)
    job_id = await _insert_job(pg_pool)

    async with await _client(app) as client:
        auditor_id, sid = await _login_as_seeded_user(
            pg_pool, client, settings, monkeypatch, role="auditor"
        )
        resp = await client.get(
            f"/jobs/{job_id}", cookies={settings.session_cookie_name: sid}
        )

    assert resp.status_code == 200
    rows = await _audit_rows(pg_pool, action="read_job", subject_id=job_id)
    assert len(rows) == 1, "exactly one read_job audit_log row for this read"
    row = rows[0]
    assert row["actor_kind"] == "user"
    assert row["actor_user_id"] == auditor_id
    assert row["actor_service"] is None
    assert row["subject_type"] == "job"


@pytest.mark.parametrize("role", ["recruiter", "admin"])
@pytest.mark.asyncio
async def test_get_job_non_auditor_writes_zero_audit_rows(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    settings = _settings()
    _patch_settings(monkeypatch, settings)
    app = _build_app(pg_pool)
    job_id = await _insert_job(pg_pool)

    async with await _client(app) as client:
        _, sid = await _login_as_seeded_user(
            pg_pool, client, settings, monkeypatch, role=role
        )
        resp = await client.get(
            f"/jobs/{job_id}", cookies={settings.session_cookie_name: sid}
        )

    assert resp.status_code == 200
    assert await _audit_row_count(pg_pool) == 0


@pytest.mark.asyncio
async def test_get_job_nonexistent_auditor_404_writes_zero_audit_rows(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings()
    _patch_settings(monkeypatch, settings)
    app = _build_app(pg_pool)
    nonexistent = uuid.uuid4()

    async with await _client(app) as client:
        _, sid = await _login_as_seeded_user(
            pg_pool, client, settings, monkeypatch, role="auditor"
        )
        resp = await client.get(
            f"/jobs/{nonexistent}", cookies={settings.session_cookie_name: sid}
        )

    assert resp.status_code == 404
    assert await _audit_row_count(pg_pool) == 0


@pytest.mark.asyncio
async def test_list_jobs_auditor_never_writes_a_read_log_row(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The polled list-index route is never read-logged, even for a real
    auditor session -- proven against the real table."""
    settings = _settings()
    _patch_settings(monkeypatch, settings)
    app = _build_app(pg_pool)
    await _insert_job(pg_pool)

    async with await _client(app) as client:
        _, sid = await _login_as_seeded_user(
            pg_pool, client, settings, monkeypatch, role="auditor"
        )
        resp = await client.get("/jobs", cookies={settings.session_cookie_name: sid})

    assert resp.status_code == 200
    assert await _audit_row_count(pg_pool) == 0


# ── GET /resumes/{resume_id} -> "read_resume" ───────────────────────────


@pytest.mark.asyncio
async def test_get_resume_auditor_writes_exactly_one_read_resume_row(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings()
    _patch_settings(monkeypatch, settings)
    app = _build_app(pg_pool)
    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume(pg_pool, job_id, name="Jane Smith")

    async with await _client(app) as client:
        auditor_id, sid = await _login_as_seeded_user(
            pg_pool, client, settings, monkeypatch, role="auditor"
        )
        resp = await client.get(
            f"/resumes/{resume_id}", cookies={settings.session_cookie_name: sid}
        )

    assert resp.status_code == 200
    rows = await _audit_rows(pg_pool, action="read_resume", subject_id=resume_id)
    assert len(rows) == 1, "exactly one read_resume audit_log row for this read"
    row = rows[0]
    assert row["actor_kind"] == "user"
    assert row["actor_user_id"] == auditor_id
    assert row["actor_service"] is None
    assert row["subject_type"] == "resume"


@pytest.mark.parametrize("role", ["recruiter", "admin"])
@pytest.mark.asyncio
async def test_get_resume_non_auditor_writes_zero_audit_rows(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    settings = _settings()
    _patch_settings(monkeypatch, settings)
    app = _build_app(pg_pool)
    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume(pg_pool, job_id, name="Jane Smith")

    async with await _client(app) as client:
        _, sid = await _login_as_seeded_user(
            pg_pool, client, settings, monkeypatch, role=role
        )
        resp = await client.get(
            f"/resumes/{resume_id}", cookies={settings.session_cookie_name: sid}
        )

    assert resp.status_code == 200
    assert await _audit_row_count(pg_pool) == 0


@pytest.mark.asyncio
async def test_get_resume_nonexistent_auditor_404_writes_zero_audit_rows(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings()
    _patch_settings(monkeypatch, settings)
    app = _build_app(pg_pool)
    nonexistent = uuid.uuid4()

    async with await _client(app) as client:
        _, sid = await _login_as_seeded_user(
            pg_pool, client, settings, monkeypatch, role="auditor"
        )
        resp = await client.get(
            f"/resumes/{nonexistent}", cookies={settings.session_cookie_name: sid}
        )

    assert resp.status_code == 404
    assert await _audit_row_count(pg_pool) == 0


@pytest.mark.asyncio
async def test_list_resumes_auditor_never_writes_a_read_log_row(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings()
    _patch_settings(monkeypatch, settings)
    app = _build_app(pg_pool)
    job_id = await _insert_job(pg_pool)
    await _insert_resume(pg_pool, job_id, name="Jane Smith")

    async with await _client(app) as client:
        _, sid = await _login_as_seeded_user(
            pg_pool, client, settings, monkeypatch, role="auditor"
        )
        resp = await client.get(
            f"/jobs/{job_id}/resumes", cookies={settings.session_cookie_name: sid}
        )

    assert resp.status_code == 200
    assert await _audit_row_count(pg_pool) == 0


# ── GET /shortlist/{entry_id} -> "read_shortlist_entry" ─────────────────


@pytest.mark.asyncio
async def test_get_shortlist_entry_auditor_writes_exactly_one_read_row(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings()
    _patch_settings(monkeypatch, settings)
    app = _build_app(pg_pool)
    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume(pg_pool, job_id, name="Jane Smith")
    entry_id = await _seed_shortlist_entry(pg_pool, job_id=job_id, resume_id=resume_id)

    async with await _client(app) as client:
        auditor_id, sid = await _login_as_seeded_user(
            pg_pool, client, settings, monkeypatch, role="auditor"
        )
        resp = await client.get(
            f"/shortlist/{entry_id}", cookies={settings.session_cookie_name: sid}
        )

    assert resp.status_code == 200
    rows = await _audit_rows(
        pg_pool, action="read_shortlist_entry", subject_id=entry_id
    )
    assert len(rows) == 1
    row = rows[0]
    assert row["actor_kind"] == "user"
    assert row["actor_user_id"] == auditor_id
    assert row["actor_service"] is None
    assert row["subject_type"] == "shortlist_entry"


@pytest.mark.parametrize("role", ["recruiter", "admin"])
@pytest.mark.asyncio
async def test_get_shortlist_entry_non_auditor_writes_zero_audit_rows(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    settings = _settings()
    _patch_settings(monkeypatch, settings)
    app = _build_app(pg_pool)
    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume(pg_pool, job_id, name="Jane Smith")
    entry_id = await _seed_shortlist_entry(pg_pool, job_id=job_id, resume_id=resume_id)

    async with await _client(app) as client:
        _, sid = await _login_as_seeded_user(
            pg_pool, client, settings, monkeypatch, role=role
        )
        resp = await client.get(
            f"/shortlist/{entry_id}", cookies={settings.session_cookie_name: sid}
        )

    assert resp.status_code == 200
    assert await _audit_row_count(pg_pool) == 0


@pytest.mark.asyncio
async def test_get_shortlist_entry_nonexistent_auditor_404_writes_zero_audit_rows(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings()
    _patch_settings(monkeypatch, settings)
    app = _build_app(pg_pool)
    nonexistent = uuid.uuid4()

    async with await _client(app) as client:
        _, sid = await _login_as_seeded_user(
            pg_pool, client, settings, monkeypatch, role="auditor"
        )
        resp = await client.get(
            f"/shortlist/{nonexistent}", cookies={settings.session_cookie_name: sid}
        )

    assert resp.status_code == 404
    assert await _audit_row_count(pg_pool) == 0


@pytest.mark.asyncio
async def test_list_shortlist_auditor_never_writes_a_read_log_row(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings()
    _patch_settings(monkeypatch, settings)
    app = _build_app(pg_pool)
    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume(pg_pool, job_id, name="Jane Smith")
    await _seed_shortlist_entry(pg_pool, job_id=job_id, resume_id=resume_id)

    async with await _client(app) as client:
        _, sid = await _login_as_seeded_user(
            pg_pool, client, settings, monkeypatch, role="auditor"
        )
        resp = await client.get(
            f"/jobs/{job_id}/shortlist", cookies={settings.session_cookie_name: sid}
        )

    assert resp.status_code == 200
    assert await _audit_row_count(pg_pool) == 0


# ── GET /jobs/{job_id}/shortlist/export -> "read_shortlist_export" ──────


@pytest.mark.asyncio
async def test_export_shortlist_auditor_writes_exactly_one_read_export_row(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings()
    _patch_settings(monkeypatch, settings)
    app = _build_app(pg_pool)
    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume(pg_pool, job_id, name="Jane Smith")
    await _seed_shortlist_entry(pg_pool, job_id=job_id, resume_id=resume_id)

    async with await _client(app) as client:
        auditor_id, sid = await _login_as_seeded_user(
            pg_pool, client, settings, monkeypatch, role="auditor"
        )
        resp = await client.get(
            f"/jobs/{job_id}/shortlist/export?format=csv",
            cookies={settings.session_cookie_name: sid},
        )

    assert resp.status_code == 200
    rows = await _audit_rows(pg_pool, action="read_shortlist_export", subject_id=job_id)
    assert len(rows) == 1, "exactly one read_shortlist_export audit_log row"
    row = rows[0]
    assert row["actor_kind"] == "user"
    assert row["actor_user_id"] == auditor_id
    assert row["actor_service"] is None
    assert row["subject_type"] == "job"


@pytest.mark.parametrize("role", ["recruiter", "admin"])
@pytest.mark.asyncio
async def test_export_shortlist_non_auditor_writes_zero_audit_rows(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    settings = _settings()
    _patch_settings(monkeypatch, settings)
    app = _build_app(pg_pool)
    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume(pg_pool, job_id, name="Jane Smith")
    await _seed_shortlist_entry(pg_pool, job_id=job_id, resume_id=resume_id)

    async with await _client(app) as client:
        _, sid = await _login_as_seeded_user(
            pg_pool, client, settings, monkeypatch, role=role
        )
        resp = await client.get(
            f"/jobs/{job_id}/shortlist/export?format=csv",
            cookies={settings.session_cookie_name: sid},
        )

    assert resp.status_code == 200
    assert await _audit_row_count(pg_pool) == 0
