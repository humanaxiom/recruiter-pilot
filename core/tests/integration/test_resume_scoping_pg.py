"""Integration tests -- FU-6 slice 6 (ADR-020 SS3/SS5): row-scoping the
resume read paths (``GET /jobs/{job_id}/resumes``, ``GET /resumes/{id}``,
``POST /resumes/{id}/reveal``) against a REAL Postgres (testcontainers),
driven through a real ASGI app and a real CAS session cookie -- mirroring
``test_job_scoping_pg.py``'s own fixtures/helpers for the sibling jobs
routes, extended with ``test_resume_read_pg.py``'s real-PII-encryption
seeding pattern (``pii_encrypt`` / ``set_pii_key``) so reveal's "unblinded"
assertion is against genuinely decrypted bytes, not a mocked row.

This is the LOAD-BEARING proof of ADR-020 SS3 for these three routes: a
mocked-conn route test (``test_route_resumes.py`` / ``test_route_reveal.py``)
can only prove the route calls ``resume_service.list_for_job`` /
``resume_service.get_one`` with a particular ``user_id`` kwarg, or that a
mocked existence probe blocks the reveal route's audit/decrypt -- neither can
prove the ``EXISTS (SELECT 1 FROM job_assignees ...)`` predicate actually
FILTERS rows in a real query planner against real data, and neither can prove
that a BLOCKED reveal leaves the real ``audit_log`` table untouched. Only a
real Postgres, seeded with real ``users`` / ``jobs`` / ``job_assignees`` /
``resumes`` rows, can prove that -- the zero-audit-row count on a blocked
reveal is the single most security-critical assertion in this file.

**Locked decision: list-by-job-id returns an EMPTY list for an
unassigned/nonexistent job; get-by-resume-id 404s.** ``GET
/jobs/{job_id}/resumes`` is a LIST subresource keyed by ``job_id`` --
BEFORE this slice, and unaffected by it, an entirely nonexistent ``job_id``
already returns 200 with an empty list (the route's SQL only ever filters
``resumes.job_id = $1``; it never joins back to ``jobs`` to check the job
itself exists). Per ADR-020 SS5's own invariant ("unassigned" must stay
OBSERVATIONALLY IDENTICAL to "nonexistent"), an unassigned job must resolve
the SAME way -- 200, empty list -- rather than 404, which would newly
distinguish the two cases this slice must not distinguish. ``GET
/resumes/{id}`` and ``POST /resumes/{id}/reveal``, by contrast, are
single-RESOURCE routes keyed by ``resume_id`` (mirroring ``GET
/jobs/{job_id}``'s existing SS5 404 pattern) -- both 404 for an
unassigned/nonexistent resume, exactly like the jobs single-get route.

``users.role`` is set directly via SQL when seeding a non-default role
(``hiring_manager``/``auditor``/``admin``) -- see
``test_job_scoping_pg.py``'s module docstring for why this is the only way
to get a real, cookie-backed hiring_manager session.

**Reveal reversal (`fix/session-role-on-writes`, 2026-08-07; ADR-020 §9,
ADR-033).** The three ``test_reveal_hiring_manager_session_403s_*`` tests
below SUPERSEDE this file's original slice-6 expectation that a scoped,
assigned hiring_manager session could reveal -- see ADR-020 §9 for the full
account. Reveal is now recruiter/admin ONLY; a hiring_manager session 403s
on ``POST /resumes/{id}/reveal`` unconditionally, assigned or not, with zero
``audit_log`` rows written either way. The list/get read routes in this file
(``GET /jobs/{id}/resumes``, ``GET /resumes/{id}``) are UNCHANGED by this
reversal -- ADR-020 §3/§4/§5 still governs them exactly as built.

**RBAC key-role note.** These Settings configure no ``api_key_*`` values, so
``auth_enabled`` is False and ``resolve_role`` ALWAYS resolves
``Role.ADMIN`` regardless of any header -- exactly like
``test_job_scoping_pg.py``. This means ``require_role(*_REVEALERS)`` (admin,
recruiter) is satisfied for every session driven through this file,
including a hiring_manager CAS session: the RBAC gate is orthogonal to
row-scoping here, and only the CAS session's resolved ``user.role`` decides
whether a caller is scoped (ADR-020 SS3's "shared browser key" scenario).
"""

from __future__ import annotations

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
from src.api.routes import resumes as resumes_routes
from src.errors import AppError
from src.models.ddl import init_schema
from src.models.pool import get_db
from src.services import pii as pii_service
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
            "resumes, outbox CASCADE"
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
    app.include_router(resumes_routes.router)

    async def _get_db_override() -> AsyncIterator[Any]:
        async with pool.acquire() as conn:
            yield conn

    app.dependency_overrides[get_db] = _get_db_override
    app.dependency_overrides[get_arq] = lambda: MagicMock(enqueue_job=AsyncMock())
    # RBAC (require_role) is orthogonal to row-scoping here -- see the module
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
    """Seed a ``users`` row with an EXPLICIT role, set directly via SQL -- see
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
    # resume_service's list_for_job/get_one call pii_service.set_pii_key,
    # which reads the PII key from this module's own get_settings binding.
    monkeypatch.setattr(pii_service, "get_settings", lambda: settings)


async def _insert_resume(
    pool: asyncpg.Pool,
    job_id: uuid.UUID,
    *,
    name: str | None = None,
    email: str | None = None,
    original_filename: str = "resume.pdf",
) -> uuid.UUID:
    """Seed a real, PII-encrypted ``resumes`` row -- mirrors
    ``test_resume_read_pg.py``'s ``_insert_resume`` helper. Requires
    ``_patch_settings`` to have already run so ``pii_service.get_settings``
    resolves ``TEST_PII_KEY``."""
    async with pool.acquire() as conn:
        resume_id: uuid.UUID = await conn.fetchval(
            """
            INSERT INTO resumes (
                job_id, blob_key, original_filename, mime_type,
                file_size_bytes, sha256, consent_acknowledged, status
            ) VALUES ($1, $2, $3, 'application/pdf', 1024, $4, TRUE, 'parsed')
            RETURNING id
            """,
            job_id,
            f"resumes/{uuid.uuid4().hex}.pdf",
            original_filename,
            uuid.uuid4().hex,
        )
        async with conn.transaction():
            await pii_service.set_pii_key(conn)
            name_enc = await pii_service.encrypt(conn, name)
            email_enc = await pii_service.encrypt(conn, email)
            await conn.execute(
                "UPDATE resumes SET candidate_name = $2, candidate_email = $3 "
                "WHERE id = $1",
                resume_id,
                name_enc,
                email_enc,
            )
    return resume_id


async def _count_reveal_audit_rows(pool: asyncpg.Pool, resume_id: uuid.UUID) -> int:
    async with pool.acquire() as conn:
        count: int = await conn.fetchval(
            "SELECT count(*) FROM audit_log "
            "WHERE subject_id = $1 AND action = 'reveal'",
            resume_id,
        )
    return count


# ── (a) admin / recruiter / auditor -- UNSCOPED, see/reveal every resume ──


@pytest.mark.parametrize("role", ["admin", "recruiter", "auditor"])
@pytest.mark.asyncio
async def test_list_resumes_unscoped_roles_see_every_seeded_resume(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    settings = _settings()
    _patch_settings(monkeypatch, settings)
    app = _build_app(pg_pool)

    job_id = await _insert_job(pg_pool)
    r1 = await _insert_resume(pg_pool, job_id, name="Alice One")
    r2 = await _insert_resume(pg_pool, job_id, name="Bob Two")

    async with await _client(app) as client:
        _, sid = await _login_as_seeded_user(
            pg_pool, client, settings, monkeypatch, role=role
        )
        resp = await client.get(
            f"/jobs/{job_id}/resumes", cookies={settings.session_cookie_name: sid}
        )

    assert resp.status_code == 200
    returned_ids = {item["id"] for item in resp.json()}
    assert returned_ids == {str(r1), str(r2)}


@pytest.mark.parametrize("role", ["admin", "recruiter", "auditor"])
@pytest.mark.asyncio
async def test_get_resume_unscoped_roles_200_for_any_resume(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    settings = _settings()
    _patch_settings(monkeypatch, settings)
    app = _build_app(pg_pool)

    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume(pg_pool, job_id, name="Alice One")

    async with await _client(app) as client:
        _, sid = await _login_as_seeded_user(
            pg_pool, client, settings, monkeypatch, role=role
        )
        resp = await client.get(
            f"/resumes/{resume_id}", cookies={settings.session_cookie_name: sid}
        )

    assert resp.status_code == 200
    assert resp.json()["id"] == str(resume_id)


@pytest.mark.parametrize("role", ["admin", "recruiter"])
@pytest.mark.asyncio
async def test_reveal_unscoped_roles_writes_one_audit_row_and_unblinds(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    """auditor is excluded here -- D2/RBAC forbids auditor from revealing at
    all (403), unrelated to ADR-020 row-scoping; already pinned in
    ``test_route_reveal.py``."""
    settings = _settings()
    _patch_settings(monkeypatch, settings)
    app = _build_app(pg_pool)

    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume(
        pg_pool, job_id, name="Jane Smith", email="jane.smith@example.test"
    )

    async with await _client(app) as client:
        _, sid = await _login_as_seeded_user(
            pg_pool, client, settings, monkeypatch, role=role
        )
        resp = await client.post(
            f"/resumes/{resume_id}/reveal", cookies={settings.session_cookie_name: sid}
        )

    assert resp.status_code == 200
    assert resp.json()["candidate"]["name"] == "Jane Smith"
    assert await _count_reveal_audit_rows(pg_pool, resume_id) == 1


# ── (b) hiring_manager assigned to the resume's job ────────────────────────


@pytest.mark.asyncio
async def test_list_resumes_hiring_manager_sees_assigned_jobs_resumes(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings()
    _patch_settings(monkeypatch, settings)
    app = _build_app(pg_pool)

    assigned_job = await _insert_job(pg_pool, title="Assigned")
    other_job = await _insert_job(pg_pool, title="Not mine")
    r_assigned = await _insert_resume(pg_pool, assigned_job, name="Assigned Candidate")
    await _insert_resume(pg_pool, other_job, name="Other Candidate")

    async with await _client(app) as client:
        hm_id, sid = await _login_as_seeded_user(
            pg_pool, client, settings, monkeypatch, role="hiring_manager"
        )
        await _assign(pg_pool, job_id=assigned_job, user_id=hm_id, assigned_by=hm_id)

        resp = await client.get(
            f"/jobs/{assigned_job}/resumes",
            cookies={settings.session_cookie_name: sid},
        )

    assert resp.status_code == 200
    returned_ids = {item["id"] for item in resp.json()}
    assert returned_ids == {str(r_assigned)}


@pytest.mark.asyncio
async def test_get_resume_hiring_manager_200_for_assigned_jobs_resume(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings()
    _patch_settings(monkeypatch, settings)
    app = _build_app(pg_pool)

    assigned_job = await _insert_job(pg_pool, title="Assigned")
    resume_id = await _insert_resume(pg_pool, assigned_job, name="Assigned Candidate")

    async with await _client(app) as client:
        hm_id, sid = await _login_as_seeded_user(
            pg_pool, client, settings, monkeypatch, role="hiring_manager"
        )
        await _assign(pg_pool, job_id=assigned_job, user_id=hm_id, assigned_by=hm_id)

        resp = await client.get(
            f"/resumes/{resume_id}", cookies={settings.session_cookie_name: sid}
        )

    assert resp.status_code == 200
    assert resp.json()["id"] == str(resume_id)


@pytest.mark.asyncio
async def test_reveal_hiring_manager_session_403s_even_when_assigned_writes_zero_audit_rows(  # noqa: E501
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reversal (`fix/session-role-on-writes`, 2026-08-07; ADR-020 §9,
    ADR-033) — SUPERSEDES this test's original assigned-hiring_manager-can-
    reveal-and-unblind expectation, exactly like the mocked-conn pin in
    ``test_route_reveal.py``'s own "Reversal" section. Reveal is now
    recruiter/admin ONLY: a hiring_manager session 403s on reveal even for a
    job they ARE assigned to — `require_session_role(*_REVEALERS)` fires
    before `scoped_user_id_or_403`'s hiring_manager-scoping branch is ever
    reached, so the real ``audit_log`` table proves what the mocked route
    test could only assert on a mock: zero rows, no decrypt, for a blocked
    reveal regardless of assignment."""
    settings = _settings()
    _patch_settings(monkeypatch, settings)
    app = _build_app(pg_pool)

    assigned_job = await _insert_job(pg_pool, title="Assigned")
    resume_id = await _insert_resume(
        pg_pool, assigned_job, name="Jane Smith", email="jane.smith@example.test"
    )

    async with await _client(app) as client:
        hm_id, sid = await _login_as_seeded_user(
            pg_pool, client, settings, monkeypatch, role="hiring_manager"
        )
        await _assign(pg_pool, job_id=assigned_job, user_id=hm_id, assigned_by=hm_id)

        resp = await client.post(
            f"/resumes/{resume_id}/reveal", cookies={settings.session_cookie_name: sid}
        )

    assert resp.status_code == 403
    assert await _count_reveal_audit_rows(pg_pool, resume_id) == 0


# ── (c) hiring_manager NOT assigned to the resume's job ─────────────────


@pytest.mark.asyncio
async def test_list_resumes_hiring_manager_unassigned_job_returns_empty(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Locked decision (module docstring): a list-by-job-id subresource
    returns an EMPTY list, never 404, for a job the caller cannot see --
    matching how a genuinely nonexistent ``job_id`` already behaves on this
    route, so "unassigned" stays observationally identical to
    "nonexistent" per ADR-020 SS5."""
    settings = _settings()
    _patch_settings(monkeypatch, settings)
    app = _build_app(pg_pool)

    owned_job = await _insert_job(pg_pool, title="Owned")
    other_job = await _insert_job(pg_pool, title="Not mine")
    await _insert_resume(pg_pool, other_job, name="Other Candidate")

    async with await _client(app) as client:
        hm_id, sid = await _login_as_seeded_user(
            pg_pool, client, settings, monkeypatch, role="hiring_manager"
        )
        await _assign(pg_pool, job_id=owned_job, user_id=hm_id, assigned_by=hm_id)

        resp = await client.get(
            f"/jobs/{other_job}/resumes", cookies={settings.session_cookie_name: sid}
        )

    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_get_resume_hiring_manager_404_for_unassigned_jobs_resume(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings()
    _patch_settings(monkeypatch, settings)
    app = _build_app(pg_pool)

    owned_job = await _insert_job(pg_pool, title="Owned")
    other_job = await _insert_job(pg_pool, title="Not mine")
    other_resume = await _insert_resume(pg_pool, other_job, name="Other Candidate")

    async with await _client(app) as client:
        hm_id, sid = await _login_as_seeded_user(
            pg_pool, client, settings, monkeypatch, role="hiring_manager"
        )
        await _assign(pg_pool, job_id=owned_job, user_id=hm_id, assigned_by=hm_id)

        resp = await client.get(
            f"/resumes/{other_resume}", cookies={settings.session_cookie_name: sid}
        )

    assert resp.status_code == 404, (
        "ADR-020 SS5: a resume under an unassigned job must 404, never 403 -- "
        "indistinguishable from a resume that never existed"
    )


@pytest.mark.asyncio
async def test_reveal_hiring_manager_session_403s_for_unassigned_jobs_resume_writes_zero_audit_rows(  # noqa: E501
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reversal (`fix/session-role-on-writes`, 2026-08-07; ADR-020 §9,
    ADR-033) — reveal is recruiter/admin ONLY, so an unassigned job now 403s
    (the session-role gate) rather than 404 (the now-unreachable scoping
    check) — see the sibling "assigned" test above: under the new policy the
    two cases are deliberately indistinguishable, both 403. The
    security-critical proof this file exists for is unchanged in kind: a
    BLOCKED reveal must leave the real ``audit_log`` table with ZERO rows
    for this resume -- no audit trail of an attempted de-anonymization the
    caller was never authorized to see in the first place."""
    settings = _settings()
    _patch_settings(monkeypatch, settings)
    app = _build_app(pg_pool)

    owned_job = await _insert_job(pg_pool, title="Owned")
    other_job = await _insert_job(pg_pool, title="Not mine")
    other_resume = await _insert_resume(
        pg_pool, other_job, name="Zoe Blocked", email="zoe.blocked@example.test"
    )

    async with await _client(app) as client:
        hm_id, sid = await _login_as_seeded_user(
            pg_pool, client, settings, monkeypatch, role="hiring_manager"
        )
        await _assign(pg_pool, job_id=owned_job, user_id=hm_id, assigned_by=hm_id)

        resp = await client.post(
            f"/resumes/{other_resume}/reveal",
            cookies={settings.session_cookie_name: sid},
        )

    assert resp.status_code == 403
    assert await _count_reveal_audit_rows(pg_pool, other_resume) == 0, (
        "a blocked reveal must leave NO audit_log row -- proof that the "
        "session-role gate runs before the audit write"
    )


# ── (d) hiring_manager with ZERO assignments -- empty/404 everywhere ──────


@pytest.mark.asyncio
async def test_list_resumes_hiring_manager_zero_assignments_returns_empty(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings()
    _patch_settings(monkeypatch, settings)
    app = _build_app(pg_pool)

    job_id = await _insert_job(pg_pool, title="Somebody Else's Job")
    await _insert_resume(pg_pool, job_id, name="Somebody Else's Candidate")

    async with await _client(app) as client:
        _, sid = await _login_as_seeded_user(
            pg_pool, client, settings, monkeypatch, role="hiring_manager"
        )
        resp = await client.get(
            f"/jobs/{job_id}/resumes", cookies={settings.session_cookie_name: sid}
        )

    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_get_resume_hiring_manager_zero_assignments_404s(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings()
    _patch_settings(monkeypatch, settings)
    app = _build_app(pg_pool)

    job_id = await _insert_job(pg_pool, title="Somebody Else's Job")
    resume_id = await _insert_resume(pg_pool, job_id, name="Somebody Else's Candidate")

    async with await _client(app) as client:
        _, sid = await _login_as_seeded_user(
            pg_pool, client, settings, monkeypatch, role="hiring_manager"
        )
        resp = await client.get(
            f"/resumes/{resume_id}", cookies={settings.session_cookie_name: sid}
        )

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_reveal_hiring_manager_session_403s_with_zero_assignments_writes_zero_audit_rows(  # noqa: E501
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reversal (`fix/session-role-on-writes`, 2026-08-07; ADR-020 §9,
    ADR-033) — same policy as the two sibling tests above: reveal 403s for
    ANY hiring_manager session, so a zero-assignment hiring_manager gets the
    same 403 (not 404) as an assigned one, with zero audit rows either way."""
    settings = _settings()
    _patch_settings(monkeypatch, settings)
    app = _build_app(pg_pool)

    job_id = await _insert_job(pg_pool, title="Somebody Else's Job")
    resume_id = await _insert_resume(
        pg_pool,
        job_id,
        name="Somebody Else's Candidate",
        email="somebody.else@example.test",
    )

    async with await _client(app) as client:
        _, sid = await _login_as_seeded_user(
            pg_pool, client, settings, monkeypatch, role="hiring_manager"
        )
        resp = await client.post(
            f"/resumes/{resume_id}/reveal", cookies={settings.session_cookie_name: sid}
        )

    assert resp.status_code == 403
    assert await _count_reveal_audit_rows(pg_pool, resume_id) == 0


# ── explicit auditor-unscoped pin (ADR-020 SS4's most commonly-inverted
#    rule, standalone from the parametrized (a) matrix above) ─────────────


@pytest.mark.asyncio
async def test_auditor_is_unscoped_for_list_and_get_even_with_zero_assignments(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An auditor with NO ``job_assignees`` rows at all must still see every
    resume -- auditors are never scoped by assignment (unlike hiring_manager
    in the identical zero-assignment situation above). Reveal is excluded
    here: auditor may never reveal at all (D2/RBAC), unrelated to scoping."""
    settings = _settings()
    _patch_settings(monkeypatch, settings)
    app = _build_app(pg_pool)

    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume(pg_pool, job_id, name="Some Candidate")

    async with await _client(app) as client:
        _, sid = await _login_as_seeded_user(
            pg_pool, client, settings, monkeypatch, role="auditor"
        )
        list_resp = await client.get(
            f"/jobs/{job_id}/resumes", cookies={settings.session_cookie_name: sid}
        )
        get_resp = await client.get(
            f"/resumes/{resume_id}", cookies={settings.session_cookie_name: sid}
        )

    assert list_resp.status_code == 200
    assert {item["id"] for item in list_resp.json()} == {str(resume_id)}
    assert get_resp.status_code == 200
    assert get_resp.json()["id"] == str(resume_id)
