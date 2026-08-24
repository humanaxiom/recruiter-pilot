"""Integration tests — FU-6 slice 7 (ADR-020 §3/§5): row-scoping the
shortlist read/export paths (``GET /jobs/{job_id}/shortlist``, ``GET
/jobs/{job_id}/shortlist/export``, ``GET /shortlist/{entry_id}``) against a
REAL Postgres (testcontainers), driven through a real ASGI app and a real
CAS session cookie — mirroring ``test_resume_scoping_pg.py`` /
``test_job_scoping_pg.py``'s own fixtures/helpers for the sibling
jobs/resumes routes, extended with ``test_api_shortlist_export_pg.py``'s
real-write-path seeding pattern (``persist_shortlist``) so the shortlist rows
scoped here are ones a real Phase 4d write actually produced, not a hand-
rolled INSERT that might drift from the real column shapes.

This is the LOAD-BEARING proof of ADR-020 §3 for these three routes: a
mocked-conn route test (``test_route_shortlist.py``) can only prove the
route calls ``shortlist_service.list_for_job`` / ``.get_one`` /
``.export_rows`` with a particular ``user_id`` kwarg — it cannot prove the
``EXISTS (SELECT 1 FROM job_assignees ...)`` predicate actually FILTERS rows
in a real query planner against real data.

**Locked decision: by-job list AND by-job export both return an EMPTY
result for an unassigned/nonexistent job; get-by-entry-id 404s.**
``GET /jobs/{job_id}/shortlist`` and ``GET /jobs/{job_id}/shortlist/export``
are both LIST-shaped subresources keyed by ``job_id`` (mirroring ``GET
/jobs/{job_id}/resumes``'s own ADR-020 §5 precedent) — an unassigned or
genuinely nonexistent ``job_id`` resolves the SAME way for both: 200 with an
empty body (empty JSON array for the list; a header-only/empty CSV or an
empty JSON array for the export). This keeps the export observationally
consistent with its sibling list route rather than introducing a THIRD
distinct behaviour (404) for what is, structurally, the same "list scoped by
job_id" shape as the list route sitting right next to it. ``GET
/shortlist/{entry_id}``, by contrast, is a single-RESOURCE route keyed by
``entry_id`` (mirroring ``GET /resumes/{id}``'s existing ADR-020 §5 404
pattern) — it 404s for an unassigned/nonexistent entry, exactly like the
résumé single-get route.

``users.role`` is set directly via SQL when seeding a non-default role
(``hiring_manager``/``auditor``/``admin``) — see
``test_job_scoping_pg.py``'s module docstring for why this is the only way
to get a real, cookie-backed hiring_manager session.

**RBAC key-role note.** These Settings configure no ``api_key_*`` values, so
``auth_enabled`` is False and ``resolve_role`` ALWAYS resolves
``Role.ADMIN`` regardless of any header — exactly like
``test_job_scoping_pg.py`` / ``test_resume_scoping_pg.py``. This means
``require_role(*_SHORTLIST_READERS)`` is satisfied for every session driven
through this file, including a hiring_manager CAS session: the RBAC gate is
orthogonal to row-scoping here, and only the CAS session's resolved
``user.role`` decides whether a caller is scoped (ADR-020 §3's "shared
browser key" scenario).
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
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
    app.include_router(shortlist_routes.router)

    async def _get_db_override() -> AsyncIterator[Any]:
        async with pool.acquire() as conn:
            yield conn

    app.dependency_overrides[get_db] = _get_db_override
    app.dependency_overrides[get_arq] = lambda: MagicMock(enqueue_job=AsyncMock())
    # RBAC (require_role) is orthogonal to row-scoping here — see the module
    # docstring's "RBAC key-role note". Left un-overridden deliberately.

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
    """Seed a ``users`` row with an EXPLICIT role, set directly via SQL — see
    the module docstring for why no provisioning path can do this."""
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


async def _assign(
    pool: asyncpg.Pool, *, job_id: uuid.UUID, user_id: uuid.UUID, assigned_by: uuid.UUID
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO job_assignees (job_id, user_id, assigned_by) "
            "VALUES ($1, $2, $3)",
            job_id,
            user_id,
            assigned_by,
        )


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
    # shortlist_service's list_for_job/get_one/export_rows call
    # pii_service.set_pii_key, which reads the PII key from this module's own
    # get_settings binding.
    monkeypatch.setattr(pii_service, "get_settings", lambda: settings)


def _sha_for(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


async def _insert_resume(
    pool: asyncpg.Pool,
    job_id: uuid.UUID,
    *,
    name: str = "Jane Smith",
) -> uuid.UUID:
    """Seed a real, PII-encrypted ``resumes`` row — mirrors
    ``test_api_shortlist_export_pg.py`` / ``test_resume_scoping_pg.py``'s own
    helper. Requires ``_patch_settings`` to have already run so
    ``pii_service.get_settings`` resolves ``TEST_PII_KEY``."""
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
                _sha_for(f"{job_id}-{uuid.uuid4().hex}"),
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
    pool: asyncpg.Pool, *, job_id: uuid.UUID, resume_id: uuid.UUID, rank: int = 1
) -> uuid.UUID:
    """Write ONE ranked shortlist row via the real Phase 4d write path
    (``persist_shortlist``) — a hand-rolled INSERT could drift from the real
    column shapes (the fold-pop discipline ADR-011 §2 documents)."""
    result = ShortlistResult(
        job_id=job_id,
        entries=[
            ShortlistResultEntry(
                resume_id=resume_id,
                rank=rank,
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


# ── (a) admin / recruiter / auditor — UNSCOPED, see/export every job's
#        shortlist and GET any entry ────────────────────────────────────


@pytest.mark.parametrize("role", ["admin", "recruiter", "auditor"])
@pytest.mark.asyncio
async def test_list_shortlist_unscoped_roles_see_every_seeded_entry(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    settings = _settings()
    _patch_settings(monkeypatch, settings)
    app = _build_app(pg_pool)

    job_a = await _insert_job(pg_pool, title="Job A")
    job_b = await _insert_job(pg_pool, title="Job B")
    r_a = await _insert_resume(pg_pool, job_a, name="Alice One")
    r_b = await _insert_resume(pg_pool, job_b, name="Bob Two")
    entry_a = await _seed_shortlist_entry(pg_pool, job_id=job_a, resume_id=r_a)
    entry_b = await _seed_shortlist_entry(pg_pool, job_id=job_b, resume_id=r_b)

    async with await _client(app) as client:
        _, sid = await _login_as_seeded_user(
            pg_pool, client, settings, monkeypatch, role=role
        )
        resp_a = await client.get(
            f"/jobs/{job_a}/shortlist", cookies={settings.session_cookie_name: sid}
        )
        resp_b = await client.get(
            f"/jobs/{job_b}/shortlist", cookies={settings.session_cookie_name: sid}
        )

    assert resp_a.status_code == 200
    assert {e["id"] for e in resp_a.json()} == {str(entry_a)}
    assert resp_b.status_code == 200
    assert {e["id"] for e in resp_b.json()} == {str(entry_b)}


@pytest.mark.parametrize("role", ["admin", "recruiter", "auditor"])
@pytest.mark.asyncio
async def test_export_shortlist_unscoped_roles_export_every_seeded_job(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    settings = _settings()
    _patch_settings(monkeypatch, settings)
    app = _build_app(pg_pool)

    job_a = await _insert_job(pg_pool, title="Job A")
    job_b = await _insert_job(pg_pool, title="Job B")
    r_a = await _insert_resume(pg_pool, job_a, name="Alice One")
    r_b = await _insert_resume(pg_pool, job_b, name="Bob Two")
    await _seed_shortlist_entry(pg_pool, job_id=job_a, resume_id=r_a)
    await _seed_shortlist_entry(pg_pool, job_id=job_b, resume_id=r_b)

    async with await _client(app) as client:
        _, sid = await _login_as_seeded_user(
            pg_pool, client, settings, monkeypatch, role=role
        )
        resp_a = await client.get(
            f"/jobs/{job_a}/shortlist/export?format=csv",
            cookies={settings.session_cookie_name: sid},
        )
        resp_b = await client.get(
            f"/jobs/{job_b}/shortlist/export?format=csv",
            cookies={settings.session_cookie_name: sid},
        )

    assert resp_a.status_code == 200
    assert len(list(csv.DictReader(io.StringIO(resp_a.text)))) == 1
    assert resp_b.status_code == 200
    assert len(list(csv.DictReader(io.StringIO(resp_b.text)))) == 1


@pytest.mark.parametrize("role", ["admin", "recruiter", "auditor"])
@pytest.mark.asyncio
async def test_get_shortlist_entry_unscoped_roles_200_for_any_entry(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    settings = _settings()
    _patch_settings(monkeypatch, settings)
    app = _build_app(pg_pool)

    job_a = await _insert_job(pg_pool, title="Job A")
    job_b = await _insert_job(pg_pool, title="Job B")
    r_a = await _insert_resume(pg_pool, job_a, name="Alice One")
    r_b = await _insert_resume(pg_pool, job_b, name="Bob Two")
    entry_a = await _seed_shortlist_entry(pg_pool, job_id=job_a, resume_id=r_a)
    entry_b = await _seed_shortlist_entry(pg_pool, job_id=job_b, resume_id=r_b)

    async with await _client(app) as client:
        _, sid = await _login_as_seeded_user(
            pg_pool, client, settings, monkeypatch, role=role
        )
        resp_a = await client.get(
            f"/shortlist/{entry_a}", cookies={settings.session_cookie_name: sid}
        )
        resp_b = await client.get(
            f"/shortlist/{entry_b}", cookies={settings.session_cookie_name: sid}
        )

    assert resp_a.status_code == 200
    assert resp_a.json()["id"] == str(entry_a)
    assert resp_b.status_code == 200
    assert resp_b.json()["id"] == str(entry_b)


# ── (b) hiring_manager assigned to the shortlist's job ──────────────────


@pytest.mark.asyncio
async def test_list_shortlist_hiring_manager_sees_assigned_jobs_shortlist(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings()
    _patch_settings(monkeypatch, settings)
    app = _build_app(pg_pool)

    assigned_job = await _insert_job(pg_pool, title="Assigned")
    resume_id = await _insert_resume(pg_pool, assigned_job, name="Assigned Candidate")
    entry_id = await _seed_shortlist_entry(
        pg_pool, job_id=assigned_job, resume_id=resume_id
    )

    async with await _client(app) as client:
        hm_id, sid = await _login_as_seeded_user(
            pg_pool, client, settings, monkeypatch, role="hiring_manager"
        )
        await _assign(pg_pool, job_id=assigned_job, user_id=hm_id, assigned_by=hm_id)

        resp = await client.get(
            f"/jobs/{assigned_job}/shortlist",
            cookies={settings.session_cookie_name: sid},
        )

    assert resp.status_code == 200
    assert {e["id"] for e in resp.json()} == {str(entry_id)}


@pytest.mark.asyncio
async def test_export_shortlist_hiring_manager_exports_assigned_job(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings()
    _patch_settings(monkeypatch, settings)
    app = _build_app(pg_pool)

    assigned_job = await _insert_job(pg_pool, title="Assigned")
    resume_id = await _insert_resume(pg_pool, assigned_job, name="Assigned Candidate")
    await _seed_shortlist_entry(pg_pool, job_id=assigned_job, resume_id=resume_id)

    async with await _client(app) as client:
        hm_id, sid = await _login_as_seeded_user(
            pg_pool, client, settings, monkeypatch, role="hiring_manager"
        )
        await _assign(pg_pool, job_id=assigned_job, user_id=hm_id, assigned_by=hm_id)

        resp = await client.get(
            f"/jobs/{assigned_job}/shortlist/export?format=csv",
            cookies={settings.session_cookie_name: sid},
        )

    assert resp.status_code == 200
    assert len(list(csv.DictReader(io.StringIO(resp.text)))) == 1


@pytest.mark.asyncio
async def test_get_shortlist_entry_hiring_manager_200_for_assigned_jobs_entry(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings()
    _patch_settings(monkeypatch, settings)
    app = _build_app(pg_pool)

    assigned_job = await _insert_job(pg_pool, title="Assigned")
    resume_id = await _insert_resume(pg_pool, assigned_job, name="Assigned Candidate")
    entry_id = await _seed_shortlist_entry(
        pg_pool, job_id=assigned_job, resume_id=resume_id
    )

    async with await _client(app) as client:
        hm_id, sid = await _login_as_seeded_user(
            pg_pool, client, settings, monkeypatch, role="hiring_manager"
        )
        await _assign(pg_pool, job_id=assigned_job, user_id=hm_id, assigned_by=hm_id)

        resp = await client.get(
            f"/shortlist/{entry_id}", cookies={settings.session_cookie_name: sid}
        )

    assert resp.status_code == 200
    assert resp.json()["id"] == str(entry_id)


# ── (c) hiring_manager NOT assigned to the shortlist's job ──────────────


@pytest.mark.asyncio
async def test_list_shortlist_hiring_manager_unassigned_job_returns_empty(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Locked decision (module docstring): a list-by-job-id subresource
    returns an EMPTY list, never 404, for a job the caller cannot see —
    matching how a genuinely nonexistent ``job_id`` already behaves on the
    sibling résumé route, so "unassigned" stays observationally identical to
    "nonexistent" per ADR-020 §5."""
    settings = _settings()
    _patch_settings(monkeypatch, settings)
    app = _build_app(pg_pool)

    owned_job = await _insert_job(pg_pool, title="Owned")
    other_job = await _insert_job(pg_pool, title="Not mine")
    other_resume = await _insert_resume(pg_pool, other_job, name="Other Candidate")
    await _seed_shortlist_entry(pg_pool, job_id=other_job, resume_id=other_resume)

    async with await _client(app) as client:
        hm_id, sid = await _login_as_seeded_user(
            pg_pool, client, settings, monkeypatch, role="hiring_manager"
        )
        await _assign(pg_pool, job_id=owned_job, user_id=hm_id, assigned_by=hm_id)

        resp = await client.get(
            f"/jobs/{other_job}/shortlist",
            cookies={settings.session_cookie_name: sid},
        )

    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_export_shortlist_hiring_manager_unassigned_job_returns_empty_export(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pinned consistent with the list route above (module docstring):
    export for an unassigned job is a 200 with an empty (header-only) body,
    never a 404 — the export is the SAME by-job-id list shape as the list
    route, not a single-resource route."""
    settings = _settings()
    _patch_settings(monkeypatch, settings)
    app = _build_app(pg_pool)

    owned_job = await _insert_job(pg_pool, title="Owned")
    other_job = await _insert_job(pg_pool, title="Not mine")
    other_resume = await _insert_resume(pg_pool, other_job, name="Other Candidate")
    await _seed_shortlist_entry(pg_pool, job_id=other_job, resume_id=other_resume)

    async with await _client(app) as client:
        hm_id, sid = await _login_as_seeded_user(
            pg_pool, client, settings, monkeypatch, role="hiring_manager"
        )
        await _assign(pg_pool, job_id=owned_job, user_id=hm_id, assigned_by=hm_id)

        resp = await client.get(
            f"/jobs/{other_job}/shortlist/export?format=csv",
            cookies={settings.session_cookie_name: sid},
        )

    assert resp.status_code == 200
    assert list(csv.DictReader(io.StringIO(resp.text))) == []


@pytest.mark.asyncio
async def test_get_shortlist_entry_hiring_manager_404_for_unassigned_jobs_entry(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings()
    _patch_settings(monkeypatch, settings)
    app = _build_app(pg_pool)

    owned_job = await _insert_job(pg_pool, title="Owned")
    other_job = await _insert_job(pg_pool, title="Not mine")
    other_resume = await _insert_resume(pg_pool, other_job, name="Other Candidate")
    other_entry = await _seed_shortlist_entry(
        pg_pool, job_id=other_job, resume_id=other_resume
    )

    async with await _client(app) as client:
        hm_id, sid = await _login_as_seeded_user(
            pg_pool, client, settings, monkeypatch, role="hiring_manager"
        )
        await _assign(pg_pool, job_id=owned_job, user_id=hm_id, assigned_by=hm_id)

        resp = await client.get(
            f"/shortlist/{other_entry}", cookies={settings.session_cookie_name: sid}
        )

    assert resp.status_code == 404, (
        "ADR-020 §5: a shortlist entry under an unassigned job must 404, "
        "never 403 — indistinguishable from an entry that never existed"
    )


# ── (d) hiring_manager with ZERO assignments — empty/404 everywhere ─────


@pytest.mark.asyncio
async def test_list_shortlist_hiring_manager_zero_assignments_returns_empty(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings()
    _patch_settings(monkeypatch, settings)
    app = _build_app(pg_pool)

    job_id = await _insert_job(pg_pool, title="Somebody Else's Job")
    resume_id = await _insert_resume(pg_pool, job_id, name="Somebody Else's Candidate")
    await _seed_shortlist_entry(pg_pool, job_id=job_id, resume_id=resume_id)

    async with await _client(app) as client:
        _, sid = await _login_as_seeded_user(
            pg_pool, client, settings, monkeypatch, role="hiring_manager"
        )
        resp = await client.get(
            f"/jobs/{job_id}/shortlist", cookies={settings.session_cookie_name: sid}
        )

    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_export_shortlist_hiring_manager_zero_assignments_returns_empty_export(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings()
    _patch_settings(monkeypatch, settings)
    app = _build_app(pg_pool)

    job_id = await _insert_job(pg_pool, title="Somebody Else's Job")
    resume_id = await _insert_resume(pg_pool, job_id, name="Somebody Else's Candidate")
    await _seed_shortlist_entry(pg_pool, job_id=job_id, resume_id=resume_id)

    async with await _client(app) as client:
        _, sid = await _login_as_seeded_user(
            pg_pool, client, settings, monkeypatch, role="hiring_manager"
        )
        resp = await client.get(
            f"/jobs/{job_id}/shortlist/export?format=csv",
            cookies={settings.session_cookie_name: sid},
        )

    assert resp.status_code == 200
    assert list(csv.DictReader(io.StringIO(resp.text))) == []


@pytest.mark.asyncio
async def test_get_shortlist_entry_hiring_manager_zero_assignments_404s(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings()
    _patch_settings(monkeypatch, settings)
    app = _build_app(pg_pool)

    job_id = await _insert_job(pg_pool, title="Somebody Else's Job")
    resume_id = await _insert_resume(pg_pool, job_id, name="Somebody Else's Candidate")
    entry_id = await _seed_shortlist_entry(pg_pool, job_id=job_id, resume_id=resume_id)

    async with await _client(app) as client:
        _, sid = await _login_as_seeded_user(
            pg_pool, client, settings, monkeypatch, role="hiring_manager"
        )
        resp = await client.get(
            f"/shortlist/{entry_id}", cookies={settings.session_cookie_name: sid}
        )

    assert resp.status_code == 404


# ── explicit auditor-unscoped pin (ADR-020 §4's most commonly-inverted
#    rule, standalone from the parametrized (a) matrix above) ───────────


@pytest.mark.asyncio
async def test_auditor_is_unscoped_for_list_export_and_get_even_with_zero_assignments(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An auditor with NO ``job_assignees`` rows at all must still see the
    full shortlist and export for any job — auditors are never scoped by
    assignment (unlike hiring_manager in the identical zero-assignment
    situation above)."""
    settings = _settings()
    _patch_settings(monkeypatch, settings)
    app = _build_app(pg_pool)

    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume(pg_pool, job_id, name="Some Candidate")
    entry_id = await _seed_shortlist_entry(pg_pool, job_id=job_id, resume_id=resume_id)

    async with await _client(app) as client:
        _, sid = await _login_as_seeded_user(
            pg_pool, client, settings, monkeypatch, role="auditor"
        )
        list_resp = await client.get(
            f"/jobs/{job_id}/shortlist", cookies={settings.session_cookie_name: sid}
        )
        export_resp = await client.get(
            f"/jobs/{job_id}/shortlist/export?format=csv",
            cookies={settings.session_cookie_name: sid},
        )
        get_resp = await client.get(
            f"/shortlist/{entry_id}", cookies={settings.session_cookie_name: sid}
        )

    assert list_resp.status_code == 200
    assert {e["id"] for e in list_resp.json()} == {str(entry_id)}
    assert export_resp.status_code == 200
    assert len(list(csv.DictReader(io.StringIO(export_resp.text)))) == 1
    assert get_resp.status_code == 200
    assert get_resp.json()["id"] == str(entry_id)
