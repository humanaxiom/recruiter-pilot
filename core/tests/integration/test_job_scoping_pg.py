"""Integration tests — FU-6 slice 5 (ADR-020 §3/§5): row-scoping the jobs
read paths (``GET /jobs``, ``GET /jobs/{job_id}``) against a REAL Postgres
(testcontainers), driven through a real ASGI app and a real CAS session
cookie (a mocked CAS ticket, per ``test_auth_routes_pg.py``'s own boundary —
the only genuinely-external call these tests mock).

This is the LOAD-BEARING proof of ADR-020 §3: a mocked-conn route test
(``test_route_jobs.py``) can only prove the route calls
``job_service.list_jobs``/``get_job`` with a particular ``user_id`` kwarg —
it cannot prove the ``EXISTS (SELECT 1 FROM job_assignees ...)`` predicate
actually FILTERS rows in a real query planner against real data. Only a real
Postgres, seeded with real ``users``/``jobs``/``job_assignees`` rows, can
prove that.

The ADR-020 §Consequences 4-leg matrix, for the two routes this slice
covers:

    (a) admin / recruiter / auditor (UNSCOPED, ADR-020 §4) -> sees every
        seeded job, 200 on any job id.
    (b) hiring_manager assigned to a SUBSET -> sees only that subset; 200 on
        an assigned job, 404 on an unassigned one (ADR-020 §5: 404, not 403).
    (c) hiring_manager with ZERO assignments -> empty list; 404 on every job.
    (d) a genuinely nonexistent job id, for a scoped hiring_manager -> 404,
        observationally identical to (b)'s unassigned-job 404.

``users.role`` is set directly via SQL when seeding a non-default role
(``hiring_manager``/``auditor``/``admin``) — there is no provisioning path
that assigns anything but the default ``'recruiter'`` (or, for the
configured ``default_admin_cas_username``, ``'admin'``) on first CAS login
(``src/services/user_service.py``), and a *second* login for an
already-existing ``cas_username`` never touches ``role`` (`ON CONFLICT ...`
deliberately excludes it) — so pre-seeding the row and then logging in as
that same username is the only way to get a real, cookie-backed
hiring_manager/auditor session.

FU-6 slice 9 (ADR-020 §7) extends this file with ``GET /my/jobs`` — see the
dedicated section near the end. This is the same load-bearing-proof
reasoning as above, but for a DIFFERENT route with DIFFERENT scoping
semantics: ``/my/jobs`` scopes to the calling session user's OWN id for
EVERY role (not just hiring_manager), so it needs its own real-Postgres
proof that an admin session with zero ``job_assignees`` rows really does see
``[]`` (not "every job" — the trap a ``scoped_user_id_or_403``-reusing
implementation would fall into) and that an admin WHO IS assigned sees
exactly that one job.
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
from src.api.routes import jobs as jobs_routes
from src.errors import AppError
from src.models.ddl import init_schema
from src.models.pool import get_db
from src.settings import Settings

_JD = "We need a senior backend engineer. " * 3


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
        "pii_key": "test-key",
    }
    base.update(overrides)
    return Settings(**base)


def _unique_username(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _build_app(pool: asyncpg.Pool) -> FastAPI:
    app = FastAPI()
    app.include_router(auth_routes.router)
    app.include_router(jobs_routes.router)

    async def _get_db_override() -> AsyncIterator[Any]:
        async with pool.acquire() as conn:
            yield conn

    app.dependency_overrides[get_db] = _get_db_override
    app.dependency_overrides[get_arq] = lambda: MagicMock(enqueue_job=AsyncMock())
    # RBAC (require_role) is orthogonal to row-scoping — GET /jobs and
    # GET /jobs/{id} are readable by every Role anyway (``_JOB_READERS =
    # tuple(Role)``), and auth is disabled by default in these Settings (no
    # api_key_* configured), so `resolve_role` always resolves Role.ADMIN
    # regardless of any header. Left un-overridden deliberately: this proves
    # scoping is driven by the CAS SESSION identity, not the key role,
    # exactly as ADR-020 §3/§4 require.

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
    SAME username, returning ``(user_id, session_cookie_value)``. The login's
    ``ON CONFLICT`` path (``user_service.provision_or_get``) never touches an
    existing row's ``role``, so the pre-seeded role survives."""
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


# ── (a) admin / recruiter / auditor — UNSCOPED, see every job ─────────────


@pytest.mark.parametrize("role", ["admin", "recruiter", "auditor"])
@pytest.mark.asyncio
async def test_list_jobs_unscoped_roles_see_every_seeded_job(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    """ADR-020 §4: admin/recruiter/auditor are unscoped — explicitly
    including auditor, the common place to get this backwards (auditors keep
    GLOBAL read, compensated by audit logging, not by row scoping)."""
    settings = _settings()
    _patch_settings(monkeypatch, settings)
    app = _build_app(pg_pool)

    job_ids = {await _insert_job(pg_pool, title=f"Job {i}") for i in range(3)}

    async with await _client(app) as client:
        _, sid = await _login_as_seeded_user(
            pg_pool, client, settings, monkeypatch, role=role
        )
        resp = await client.get("/jobs", cookies={settings.session_cookie_name: sid})

    assert resp.status_code == 200
    returned_ids = {item["id"] for item in resp.json()}
    assert returned_ids == {str(j) for j in job_ids}


@pytest.mark.parametrize("role", ["admin", "recruiter", "auditor"])
@pytest.mark.asyncio
async def test_get_job_unscoped_roles_200_for_any_job(
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
    assert resp.json()["id"] == str(job_id)


# ── (b) hiring_manager assigned to a SUBSET ────────────────────────────────


@pytest.mark.asyncio
async def test_list_jobs_hiring_manager_sees_only_assigned_subset(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings()
    _patch_settings(monkeypatch, settings)
    app = _build_app(pg_pool)

    assigned_1 = await _insert_job(pg_pool, title="Assigned One")
    assigned_2 = await _insert_job(pg_pool, title="Assigned Two")
    unassigned = await _insert_job(pg_pool, title="Not Assigned")

    async with await _client(app) as client:
        hm_id, sid = await _login_as_seeded_user(
            pg_pool, client, settings, monkeypatch, role="hiring_manager"
        )
        await _assign(pg_pool, job_id=assigned_1, user_id=hm_id, assigned_by=hm_id)
        await _assign(pg_pool, job_id=assigned_2, user_id=hm_id, assigned_by=hm_id)

        resp = await client.get("/jobs", cookies={settings.session_cookie_name: sid})

    assert resp.status_code == 200
    returned_ids = {item["id"] for item in resp.json()}
    assert returned_ids == {str(assigned_1), str(assigned_2)}
    assert str(unassigned) not in returned_ids


@pytest.mark.asyncio
async def test_get_job_hiring_manager_200_for_assigned_404_for_unassigned(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings()
    _patch_settings(monkeypatch, settings)
    app = _build_app(pg_pool)

    assigned = await _insert_job(pg_pool, title="Assigned")
    unassigned = await _insert_job(pg_pool, title="Not Assigned")

    async with await _client(app) as client:
        hm_id, sid = await _login_as_seeded_user(
            pg_pool, client, settings, monkeypatch, role="hiring_manager"
        )
        await _assign(pg_pool, job_id=assigned, user_id=hm_id, assigned_by=hm_id)

        assigned_resp = await client.get(
            f"/jobs/{assigned}", cookies={settings.session_cookie_name: sid}
        )
        unassigned_resp = await client.get(
            f"/jobs/{unassigned}", cookies={settings.session_cookie_name: sid}
        )

    assert assigned_resp.status_code == 200
    assert assigned_resp.json()["id"] == str(assigned)
    assert unassigned_resp.status_code == 404, (
        "ADR-020 §5: an unassigned job must 404, never 403 — indistinguishable "
        "from a job that never existed"
    )


# ── (c) hiring_manager with ZERO assignments ───────────────────────────────


@pytest.mark.asyncio
async def test_list_jobs_hiring_manager_with_zero_assignments_returns_empty_list(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings()
    _patch_settings(monkeypatch, settings)
    app = _build_app(pg_pool)

    await _insert_job(pg_pool, title="Somebody Else's Job")

    async with await _client(app) as client:
        _, sid = await _login_as_seeded_user(
            pg_pool, client, settings, monkeypatch, role="hiring_manager"
        )
        resp = await client.get("/jobs", cookies={settings.session_cookie_name: sid})

    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_get_job_hiring_manager_with_zero_assignments_404s_on_every_job(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings()
    _patch_settings(monkeypatch, settings)
    app = _build_app(pg_pool)

    job_id = await _insert_job(pg_pool, title="Somebody Else's Job")

    async with await _client(app) as client:
        _, sid = await _login_as_seeded_user(
            pg_pool, client, settings, monkeypatch, role="hiring_manager"
        )
        resp = await client.get(
            f"/jobs/{job_id}", cookies={settings.session_cookie_name: sid}
        )

    assert resp.status_code == 404


# ── (d) genuinely nonexistent job id, scoped hiring_manager ────────────────


@pytest.mark.asyncio
async def test_get_job_hiring_manager_404_for_genuinely_nonexistent_job(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Observationally identical to the unassigned-job 404 above (ADR-020
    §5) — a scoped hiring_manager with at least one real assignment still
    gets 404, never a different status code, for an id that plain doesn't
    exist in ``jobs`` at all."""
    settings = _settings()
    _patch_settings(monkeypatch, settings)
    app = _build_app(pg_pool)

    owned = await _insert_job(pg_pool, title="Owned")
    nonexistent = uuid.uuid4()

    async with await _client(app) as client:
        hm_id, sid = await _login_as_seeded_user(
            pg_pool, client, settings, monkeypatch, role="hiring_manager"
        )
        await _assign(pg_pool, job_id=owned, user_id=hm_id, assigned_by=hm_id)

        resp = await client.get(
            f"/jobs/{nonexistent}", cookies={settings.session_cookie_name: sid}
        )

    assert resp.status_code == 404


# ── explicit auditor-unscoped pin (standalone from the parametrized (a)
#    matrix above) — ADR-020 §4's most commonly-inverted rule ─────────────


@pytest.mark.asyncio
async def test_auditor_is_unscoped_even_with_zero_assignments(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An auditor with NO ``job_assignees`` rows at all must still see every
    job — auditors are never scoped by assignment, unlike hiring_managers in
    the identical zero-assignment situation (see the (c) tests above)."""
    settings = _settings()
    _patch_settings(monkeypatch, settings)
    app = _build_app(pg_pool)

    job_ids = {await _insert_job(pg_pool, title=f"Job {i}") for i in range(2)}

    async with await _client(app) as client:
        _, sid = await _login_as_seeded_user(
            pg_pool, client, settings, monkeypatch, role="auditor"
        )
        resp = await client.get("/jobs", cookies={settings.session_cookie_name: sid})

    assert resp.status_code == 200
    returned_ids = {item["id"] for item in resp.json()}
    assert returned_ids == {str(j) for j in job_ids}


# ── FU-6 slice 9 (ADR-020 §7) — GET /my/jobs: the caller's own assigned set,
#    for ANY role ─────────────────────────────────────────────────────────
#
# The load-bearing structural proof this slice needs: a mocked-service route
# test (``test_route_jobs.py``) can only prove the route calls
# ``job_service.list_jobs(user_id=<session user's own id>)`` — it cannot
# prove that id actually filters real ``job_assignees`` rows for a
# NON-hiring_manager role, since ``GET /jobs``'s existing scoping
# (``scoped_user_id_or_403``) never exercises that path against real data
# (admin/recruiter/auditor always resolve ``user_id=None`` there — "all
# jobs"). Only a real Postgres, seeded with a real admin assignment, can
# prove an admin sees exactly their own assignment through ``/my/jobs`` and
# nothing else.


@pytest.mark.asyncio
async def test_my_jobs_hiring_manager_sees_only_assigned_subset(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings()
    _patch_settings(monkeypatch, settings)
    app = _build_app(pg_pool)

    assigned_1 = await _insert_job(pg_pool, title="Assigned One")
    assigned_2 = await _insert_job(pg_pool, title="Assigned Two")
    unassigned = await _insert_job(pg_pool, title="Not Assigned")

    async with await _client(app) as client:
        hm_id, sid = await _login_as_seeded_user(
            pg_pool, client, settings, monkeypatch, role="hiring_manager"
        )
        await _assign(pg_pool, job_id=assigned_1, user_id=hm_id, assigned_by=hm_id)
        await _assign(pg_pool, job_id=assigned_2, user_id=hm_id, assigned_by=hm_id)

        resp = await client.get("/my/jobs", cookies={settings.session_cookie_name: sid})

    assert resp.status_code == 200
    returned_ids = {item["id"] for item in resp.json()}
    assert returned_ids == {str(assigned_1), str(assigned_2)}
    assert str(unassigned) not in returned_ids


@pytest.mark.asyncio
async def test_my_jobs_hiring_manager_with_zero_assignments_returns_empty_list(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings()
    _patch_settings(monkeypatch, settings)
    app = _build_app(pg_pool)

    await _insert_job(pg_pool, title="Somebody Else's Job")

    async with await _client(app) as client:
        _, sid = await _login_as_seeded_user(
            pg_pool, client, settings, monkeypatch, role="hiring_manager"
        )
        resp = await client.get("/my/jobs", cookies={settings.session_cookie_name: sid})

    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_my_jobs_admin_with_no_assignments_returns_empty_list_not_all_jobs(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The critical proof against reusing ``scoped_user_id_or_403`` (which
    would resolve ``user_id=None`` for an admin session -> "all jobs"): a
    real admin session with zero ``job_assignees`` rows must see ``[]``
    through ``/my/jobs``, even though every seeded job exists and the SAME
    admin session sees ALL of them through ``GET /jobs`` (proving the
    difference is the route's scoping semantics, not the admin's actual
    read permission)."""
    settings = _settings()
    _patch_settings(monkeypatch, settings)
    app = _build_app(pg_pool)

    await _insert_job(pg_pool, title="Job One")
    await _insert_job(pg_pool, title="Job Two")

    async with await _client(app) as client:
        _, sid = await _login_as_seeded_user(
            pg_pool, client, settings, monkeypatch, role="admin"
        )
        my_jobs_resp = await client.get(
            "/my/jobs", cookies={settings.session_cookie_name: sid}
        )
        all_jobs_resp = await client.get(
            "/jobs", cookies={settings.session_cookie_name: sid}
        )

    assert my_jobs_resp.status_code == 200
    assert my_jobs_resp.json() == []
    assert all_jobs_resp.status_code == 200
    assert len(all_jobs_resp.json()) == 2


@pytest.mark.asyncio
async def test_my_jobs_admin_who_is_assigned_sees_exactly_that_one_job(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Role-agnostic personal scoping (ADR-020 §7): an admin IS a valid
    ``job_assignees.user_id`` — if assigned, they see that one job through
    ``/my/jobs``, proving the scope key is the session user's own id, not a
    role-based branch that only ever considers hiring_managers."""
    settings = _settings()
    _patch_settings(monkeypatch, settings)
    app = _build_app(pg_pool)

    own_job = await _insert_job(pg_pool, title="My Own Job")
    other_job = await _insert_job(pg_pool, title="Somebody Else's Job")

    async with await _client(app) as client:
        admin_id, sid = await _login_as_seeded_user(
            pg_pool, client, settings, monkeypatch, role="admin"
        )
        await _assign(pg_pool, job_id=own_job, user_id=admin_id, assigned_by=admin_id)

        resp = await client.get("/my/jobs", cookies={settings.session_cookie_name: sid})

    assert resp.status_code == 200
    returned_ids = {item["id"] for item in resp.json()}
    assert returned_ids == {str(own_job)}
    assert str(other_job) not in returned_ids
