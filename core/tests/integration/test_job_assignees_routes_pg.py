"""Integration tests — FU-6 slice 3 (ADR-020 §2): the assign/unassign routes
(``src.api.routes.job_assignees``) against a REAL Postgres (testcontainers)
via a real ASGI app, exercised through the real CAS session cookie chain
(a mocked ticket, per ``test_auth_routes_pg.py``'s own boundary).

Nothing here exists yet (``core/src/api/routes/job_assignees.py``), so every
test below fails at collection (``ModuleNotFoundError``) — RED half of the
TDD cycle.

What a REAL Postgres proves that ``test_route_job_assignees.py``'s
mocked-conn route tests structurally cannot:

* a real ``POST`` really inserts a ``job_assignees`` row satisfying the real
  FK constraints (``job_id`` -> ``jobs``, ``user_id`` -> ``users``,
  ``assigned_by`` -> ``users``) AND writes exactly one real ``audit_log`` row
  satisfying the real ``audit_log_actor_identity`` CHECK constraint — not
  just that the route called some function with plausible arguments;
* a real ``DELETE`` really removes the row and writes the paired
  ``unassign_job`` audit row;
* the SECURITY-CRITICAL atomicity guarantee this slice inherits from FU-5's
  ``blind_review`` flip (ADR-019 §1.4, mirrored here per the task): the
  ``job_assignees`` write and the ``audit_log`` write are atomic in BOTH
  directions — a crash in the audit write must roll the assignment (or
  unassignment) back too, so an assigned-but-unaudited (or
  unassigned-but-unaudited) row can never be observed. Only a real
  transaction/rollback round trip can prove this.

Follows the exact asyncpg/testcontainers/CAS-session fixture wiring already
used in ``tests/integration/test_blind_review_audit_pg.py`` and
``tests/integration/test_auth_routes_pg.py`` — no new harness.

**Acting-session role seeding (`fix/session-role-on-writes`, 2026-08-07;
ADR-033).** The four tests that log in as ``admin_username`` and then POST/
DELETE now pre-seed that same ``cas_username`` with ``role="admin"`` via
``_insert_user`` BEFORE logging in — ``require_session_role(*_ASSIGNERS)``
403s a real session whose role isn't admin/recruiter, and a first CAS login
for a non-default-admin username otherwise captures ``role IS NULL``
(ADR-019 §10a/§2 reversal). This is setup only; none of these tests were
ever exercising authorization itself.
"""

from __future__ import annotations

import json
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
from src.api.deps import Role, get_arq, resolve_role
from src.api.routes import auth as auth_routes
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
        "default_admin_cas_username": "asalah",
        "skill_hash_salt": "test-salt",
        "pii_key": "test-key",
    }
    base.update(overrides)
    return Settings(**base)


def _unique_username(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _mock_validate_ticket(username: str) -> AsyncMock:
    """Stands in for the real ``httpx`` round trip to the CAS server — the
    one genuinely-external boundary these tests mock."""
    return AsyncMock(return_value=username)


def _build_app(pool: asyncpg.Pool) -> FastAPI:
    from src.api.routes import job_assignees as job_assignees_routes

    app = FastAPI()
    app.include_router(auth_routes.router)
    app.include_router(job_assignees_routes.router)

    async def _get_db_override() -> AsyncIterator[Any]:
        async with pool.acquire() as conn:
            yield conn

    app.dependency_overrides[get_db] = _get_db_override
    app.dependency_overrides[get_arq] = lambda: MagicMock(enqueue_job=AsyncMock())
    # RBAC is orthogonal to this slice's identity/audit/atomicity focus —
    # mirrors test_blind_review_audit_pg.py's own scoping decision.
    app.dependency_overrides[resolve_role] = lambda: Role.ADMIN

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
    pool: asyncpg.Pool,
    *,
    cas_username: str | None = None,
    role: str = "hiring_manager",
) -> uuid.UUID:
    async with pool.acquire() as conn:
        user_id: uuid.UUID = await conn.fetchval(
            "INSERT INTO users (cas_username, display_name, email, role) "
            "VALUES ($1, $2, $3, $4) RETURNING id",
            cas_username or _unique_username("seed"),
            "Seed User",
            None,
            role,
        )
    return user_id


async def _lookup_user_id(pool: asyncpg.Pool, cas_username: str) -> uuid.UUID:
    async with pool.acquire() as conn:
        user_id: uuid.UUID = await conn.fetchval(
            "SELECT id FROM users WHERE cas_username = $1", cas_username
        )
    return user_id


async def _assignee_row(
    pool: asyncpg.Pool, job_id: uuid.UUID, user_id: uuid.UUID
) -> asyncpg.Record | None:
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT job_id, user_id, assigned_by, assigned_at FROM job_assignees "
            "WHERE job_id = $1 AND user_id = $2",
            job_id,
            user_id,
        )


async def _audit_rows(
    pool: asyncpg.Pool, job_id: uuid.UUID, action: str
) -> list[asyncpg.Record]:
    async with pool.acquire() as conn:
        return await conn.fetch(
            "SELECT actor_kind, actor_user_id, actor_service, action, "
            "subject_type, subject_id, job_id, details FROM audit_log "
            "WHERE job_id = $1 AND action = $2",
            job_id,
            action,
        )


def _details(row: asyncpg.Record) -> Any:
    raw = row["details"]
    return json.loads(raw) if isinstance(raw, str) else raw


# ── POST /jobs/{job_id}/assignees — real row + real audit row ─────────────


@pytest.mark.asyncio
async def test_post_assignee_creates_row_and_exactly_one_audit_log_row(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings()
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    admin_username = _unique_username("admin")
    monkeypatch.setattr(
        auth_routes.cas_service,
        "validate_ticket",
        _mock_validate_ticket(admin_username),
    )
    app = _build_app(pg_pool)

    # Seed the acting session's own role BEFORE login (`fix/session-role-
    # on-writes`, ADR-033): `require_session_role(*_ASSIGNERS)` now 403s a
    # real session whose role isn't admin/recruiter, and `provision_or_get`'s
    # `ON CONFLICT` path never touches an already-set role (`user_service.py`).
    await _insert_user(pg_pool, cas_username=admin_username, role="admin")
    job_id = await _insert_job(pg_pool)
    target_user_id = await _insert_user(pg_pool, role="hiring_manager")

    async with await _client(app) as client:
        sid = await _login_and_get_sid(client, settings, admin_username)
        resp = await client.post(
            f"/jobs/{job_id}/assignees",
            json={"user_id": str(target_user_id), "note": "backfill assignment"},
            cookies={settings.session_cookie_name: sid},
        )

    assert resp.status_code == 201

    admin_id = await _lookup_user_id(pg_pool, admin_username)
    assert admin_id is not None

    row = await _assignee_row(pg_pool, job_id, target_user_id)
    assert row is not None
    assert row["assigned_by"] == admin_id

    rows = await _audit_rows(pg_pool, job_id, "assign_job")
    assert len(rows) == 1, "exactly one audit_log row for this assignment"
    audit_row = rows[0]
    assert audit_row["actor_kind"] == "user"
    assert audit_row["actor_user_id"] == admin_id
    assert audit_row["actor_service"] is None
    assert audit_row["subject_type"] == "job"
    assert audit_row["subject_id"] == job_id
    assert _details(audit_row) == {
        "user_id": str(target_user_id),
        "note": "backfill assignment",
    }


# ── DELETE /jobs/{job_id}/assignees/{user_id} — real removal + audit ──────


@pytest.mark.asyncio
async def test_delete_assignee_removes_row_and_writes_unassign_job_audit_row(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings()
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    admin_username = _unique_username("admin")
    monkeypatch.setattr(
        auth_routes.cas_service,
        "validate_ticket",
        _mock_validate_ticket(admin_username),
    )
    app = _build_app(pg_pool)

    # Seed the acting session's own role BEFORE login (`fix/session-role-
    # on-writes`, ADR-033): `require_session_role(*_ASSIGNERS)` now 403s a
    # real session whose role isn't admin/recruiter, and `provision_or_get`'s
    # `ON CONFLICT` path never touches an already-set role (`user_service.py`).
    await _insert_user(pg_pool, cas_username=admin_username, role="admin")
    job_id = await _insert_job(pg_pool)
    target_user_id = await _insert_user(pg_pool, role="hiring_manager")

    async with await _client(app) as client:
        sid = await _login_and_get_sid(client, settings, admin_username)
        # First create the assignment we're about to remove.
        create_resp = await client.post(
            f"/jobs/{job_id}/assignees",
            json={"user_id": str(target_user_id)},
            cookies={settings.session_cookie_name: sid},
        )
        assert create_resp.status_code == 201

        del_resp = await client.delete(
            f"/jobs/{job_id}/assignees/{target_user_id}",
            cookies={settings.session_cookie_name: sid},
        )

    assert del_resp.status_code == 204

    row = await _assignee_row(pg_pool, job_id, target_user_id)
    assert row is None, "the assignment row must be gone after DELETE"

    rows = await _audit_rows(pg_pool, job_id, "unassign_job")
    assert len(rows) == 1
    audit_row = rows[0]
    assert audit_row["subject_type"] == "job"
    assert audit_row["subject_id"] == job_id
    assert _details(audit_row).get("user_id") == str(target_user_id)


# ── SECURITY-CRITICAL: assign + audit are atomic ───────────────────────────


@pytest.mark.asyncio
async def test_assign_audit_write_failure_rolls_back_the_assignment_too(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mirrors ``test_blind_review_audit_pg.py``'s
    ``test_audit_write_failure_rolls_back_the_blind_review_flip_too``: force
    the real ``audit_service.record_audit`` call to raise AFTER the real
    ``job_assignees`` INSERT would have applied, and assert no row survives —
    an assigned-but-unaudited row must never be observable."""
    from src.api.routes import job_assignees as job_assignees_routes

    settings = _settings()
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    admin_username = _unique_username("admin")
    monkeypatch.setattr(
        auth_routes.cas_service,
        "validate_ticket",
        _mock_validate_ticket(admin_username),
    )
    monkeypatch.setattr(
        job_assignees_routes.audit_service,
        "record_audit",
        AsyncMock(side_effect=RuntimeError("simulated audit-write crash")),
    )
    app = _build_app(pg_pool)

    # Seed the acting session's own role BEFORE login (`fix/session-role-
    # on-writes`, ADR-033) — see the sibling tests above for why.
    await _insert_user(pg_pool, cas_username=admin_username, role="admin")
    job_id = await _insert_job(pg_pool)
    target_user_id = await _insert_user(pg_pool, role="hiring_manager")

    async with await _client(app) as client:
        sid = await _login_and_get_sid(client, settings, admin_username)
        with pytest.raises(RuntimeError):
            await client.post(
                f"/jobs/{job_id}/assignees",
                json={"user_id": str(target_user_id)},
                cookies={settings.session_cookie_name: sid},
            )

    row = await _assignee_row(pg_pool, job_id, target_user_id)
    assert row is None, "the assignment must not survive an audit-write crash"
    assert await _audit_rows(pg_pool, job_id, "assign_job") == []


@pytest.mark.asyncio
async def test_unassign_audit_write_failure_rolls_back_the_removal_too(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The paired direction: a crash in the audit write during DELETE must
    leave the assignment row intact — an unassigned-but-unaudited state must
    never be observable either."""
    from src.api.routes import job_assignees as job_assignees_routes

    settings = _settings()
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    admin_username = _unique_username("admin")
    monkeypatch.setattr(
        auth_routes.cas_service,
        "validate_ticket",
        _mock_validate_ticket(admin_username),
    )
    app = _build_app(pg_pool)

    # Seed the acting session's own role BEFORE login (`fix/session-role-
    # on-writes`, ADR-033): `require_session_role(*_ASSIGNERS)` now 403s a
    # real session whose role isn't admin/recruiter, and `provision_or_get`'s
    # `ON CONFLICT` path never touches an already-set role (`user_service.py`).
    await _insert_user(pg_pool, cas_username=admin_username, role="admin")
    job_id = await _insert_job(pg_pool)
    target_user_id = await _insert_user(pg_pool, role="hiring_manager")

    async with await _client(app) as client:
        sid = await _login_and_get_sid(client, settings, admin_username)
        create_resp = await client.post(
            f"/jobs/{job_id}/assignees",
            json={"user_id": str(target_user_id)},
            cookies={settings.session_cookie_name: sid},
        )
        assert create_resp.status_code == 201

        monkeypatch.setattr(
            job_assignees_routes.audit_service,
            "record_audit",
            AsyncMock(side_effect=RuntimeError("simulated audit-write crash")),
        )

        with pytest.raises(RuntimeError):
            await client.delete(
                f"/jobs/{job_id}/assignees/{target_user_id}",
                cookies={settings.session_cookie_name: sid},
            )

    row = await _assignee_row(pg_pool, job_id, target_user_id)
    assert row is not None, "the assignment must survive an audit-write crash on DELETE"
    assert await _audit_rows(pg_pool, job_id, "unassign_job") == []


# ── the real-assigner FK gate, end-to-end against real Postgres ───────────


@pytest.mark.asyncio
async def test_post_assignee_403_when_no_session_cas_enabled(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bare, sessionless caller (CAS enabled, no cookie) cannot be
    ``assigned_by`` (no real user id to satisfy the FK) — 403, and no row of
    either kind is written."""
    settings = _settings()
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    app = _build_app(pg_pool)

    job_id = await _insert_job(pg_pool)
    target_user_id = await _insert_user(pg_pool, role="hiring_manager")

    async with await _client(app) as client:
        resp = await client.post(
            f"/jobs/{job_id}/assignees", json={"user_id": str(target_user_id)}
        )

    assert resp.status_code == 403
    assert await _assignee_row(pg_pool, job_id, target_user_id) is None
    assert await _audit_rows(pg_pool, job_id, "assign_job") == []
