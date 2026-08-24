"""Integration tests — FU-5 slice 9 (ADR-019 §1.4/§6): auditing
``blind_review`` flips into ``audit_log``, against a REAL Postgres
(testcontainers) via a real ASGI app.

ADR-019 §1.4 calls out ``blind_review`` as the widest-blast-radius unaudited
action in the codebase: flipping it permanently un-blinds every
résumé/shortlist under a job. What a REAL Postgres proves that
``test_route_jobs.py``'s mocked-conn route tests structurally cannot:

* a successful flip really inserts EXACTLY ONE ``audit_log`` row satisfying
  the real ``audit_log_actor_identity`` CHECK constraint — not just that the
  service called some function with plausible arguments;
* a no-op PATCH (blind_review absent, or resent unchanged) really commits
  ZERO new rows;
* the SECURITY-CRITICAL atomicity guarantee (ADR-019 §1.4): the
  ``blind_review`` UPDATE and the ``audit_log`` INSERT are atomic — a crash
  in the audit write must roll the flip back too, so a flipped-but-unaudited
  row (the exact gap ADR-018 recorded) can never be observed. This is the
  OPPOSITE ordering concern from reveal (``test_reveal_audit_log_pg.py``),
  which commits its audit row BEFORE decrypting so a crash there doesn't
  erase the trail — here, flip-without-audit, not audit-without-flip, is the
  failure being prevented, and only a real transaction/rollback round trip
  can prove it;
* the two actor-derivation cases (human session / CAS-disabled
  dev-anonymous) really resolve against a real ``users``/``sessions`` chain,
  mirroring ``test_identity_created_by_pg.py``'s and
  ``test_reveal_audit_log_pg.py``'s own real-CAS-session wiring. REVERSED
  (security finding, fix/auth-boundary-fails-open): a THIRD case — no
  session at all, CAS enabled — used to be a legitimate "not human-only"
  success path (200, ``actor_service='api'``). A live audit against real
  Postgres proved that was the vulnerability this file's Case 2 now closes:
  it must 403 and write no audit row, the same as an out-of-allowed-set
  session role. See ``test_auth_boundary_fails_open_pg.py`` for the
  end-to-end exploit-chain proof.

Nothing new here exists yet (the audited ``update_job`` — actor kwargs,
flip detection, the ``audit_log`` write, the wrapping transaction), so every
test below is expected to fail — most immediately at ``TypeError`` (the
current ``update_job`` signature does not accept actor kwargs) or an
assertion against today's silent (unaudited) flip. RED half of the TDD
cycle.

Follows the exact CAS-session/testcontainers wiring already used in
``tests/integration/test_reveal_audit_log_pg.py`` and
``tests/integration/test_identity_created_by_pg.py``. No new harness.
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
from src.api.routes import jobs as jobs_routes
from src.errors import AppError
from src.models.ddl import init_schema
from src.models.pool import get_db
from src.services import job_service
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
            "TRUNCATE sessions, users, audit_log, jobs, resumes, outbox CASCADE"
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


async def _insert_user_with_role(
    pool: asyncpg.Pool, *, cas_username: str, role: str = "recruiter"
) -> None:
    """Seed a ``users`` row with an EXPLICIT role BEFORE the CAS login for
    the SAME username, so ``provision_or_get``'s ``ON CONFLICT`` path (which
    deliberately never touches ``role``, see ``user_service.py``) leaves it
    in place. Needed as of `fix/session-role-on-writes` (ADR-033):
    ``require_session_role`` now 403s a real session whose role is not
    admin/recruiter, and this test's whole point — proving the audit actor
    is correctly derived from a real human session — needs the flip to
    actually succeed so the identity plumbing can be observed; it was never
    testing authorization itself."""
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (cas_username, display_name, email, role) "
            "VALUES ($1, $2, $3, $4)",
            cas_username,
            cas_username,
            None,
            role,
        )


def _build_app(pool: asyncpg.Pool) -> FastAPI:
    app = FastAPI()
    app.include_router(auth_routes.router)
    app.include_router(jobs_routes.router)

    async def _get_db_override() -> AsyncIterator[Any]:
        async with pool.acquire() as conn:
            yield conn

    app.dependency_overrides[get_db] = _get_db_override
    app.dependency_overrides[get_arq] = lambda: MagicMock(enqueue_job=AsyncMock())
    # RBAC is orthogonal to this slice's identity/audit switch.
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


async def _insert_job(pool: asyncpg.Pool, *, blind_review: bool = True) -> uuid.UUID:
    async with pool.acquire() as conn:
        job_id: uuid.UUID = await conn.fetchval(
            "INSERT INTO jobs (title, description_raw, blind_review) "
            "VALUES ($1, $2, $3) RETURNING id",
            "Senior Backend Engineer",
            _JD,
            blind_review,
        )
    return job_id


async def _job_blind_review(pool: asyncpg.Pool, job_id: uuid.UUID) -> bool:
    async with pool.acquire() as conn:
        value: bool = await conn.fetchval(
            "SELECT blind_review FROM jobs WHERE id = $1", job_id
        )
    return value


async def _toggle_audit_rows(
    pool: asyncpg.Pool, job_id: uuid.UUID
) -> list[asyncpg.Record]:
    async with pool.acquire() as conn:
        return await conn.fetch(
            "SELECT actor_kind, actor_user_id, actor_service, action, "
            "subject_type, subject_id, job_id, details FROM audit_log "
            "WHERE subject_id = $1 AND action = 'blind_review_toggled'",
            job_id,
        )


def _details(row: asyncpg.Record) -> Any:
    raw = row["details"]
    return json.loads(raw) if isinstance(raw, str) else raw


# ── A real flip writes exactly one, correctly-shaped, audit_log row ───────


@pytest.mark.asyncio
async def test_flip_blind_review_writes_exactly_one_audit_log_row(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(cas_enabled=False)
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    app = _build_app(pg_pool)
    job_id = await _insert_job(pg_pool, blind_review=True)

    async with await _client(app) as client:
        resp = await client.patch(f"/jobs/{job_id}", json={"blind_review": False})

    assert resp.status_code == 200
    assert resp.json()["blind_review"] is False
    assert await _job_blind_review(pg_pool, job_id) is False

    rows = await _toggle_audit_rows(pg_pool, job_id)
    assert len(rows) == 1, "exactly one audit_log row for this flip"
    row = rows[0]
    assert row["subject_type"] == "job"
    assert row["subject_id"] == job_id
    assert row["job_id"] == job_id
    assert _details(row) == {"old": True, "new": False}


@pytest.mark.asyncio
async def test_flip_the_other_direction_also_writes_one_audit_row(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pairing test for the flip direction — a builder that only wires
    the True->False case would pass the test above but fail this one."""
    settings = _settings(cas_enabled=False)
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    app = _build_app(pg_pool)
    job_id = await _insert_job(pg_pool, blind_review=False)

    async with await _client(app) as client:
        resp = await client.patch(f"/jobs/{job_id}", json={"blind_review": True})

    assert resp.status_code == 200
    assert await _job_blind_review(pg_pool, job_id) is True

    rows = await _toggle_audit_rows(pg_pool, job_id)
    assert len(rows) == 1
    assert _details(rows[0]) == {"old": False, "new": True}


# ── No-op PATCHes write zero audit rows ────────────────────────────────────


@pytest.mark.asyncio
async def test_patch_that_never_touches_blind_review_writes_no_audit_row(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(cas_enabled=False)
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    app = _build_app(pg_pool)
    job_id = await _insert_job(pg_pool, blind_review=True)

    async with await _client(app) as client:
        resp = await client.patch(f"/jobs/{job_id}", json={"title": "Renamed"})

    assert resp.status_code == 200
    assert await _job_blind_review(pg_pool, job_id) is True
    assert await _toggle_audit_rows(pg_pool, job_id) == []


@pytest.mark.asyncio
async def test_patch_resending_the_current_blind_review_value_writes_no_audit_row(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(cas_enabled=False)
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    app = _build_app(pg_pool)
    job_id = await _insert_job(pg_pool, blind_review=True)

    async with await _client(app) as client:
        resp = await client.patch(f"/jobs/{job_id}", json={"blind_review": True})

    assert resp.status_code == 200
    assert await _toggle_audit_rows(pg_pool, job_id) == []


# ── SECURITY-CRITICAL: the flip and the audit are atomic ──────────────────


@pytest.mark.asyncio
async def test_audit_write_failure_rolls_back_the_blind_review_flip_too(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-019 §1.4 — the ordering guarantee OPPOSITE to reveal's: here the
    UPDATE and the ``audit_log`` INSERT must be atomic, so a crash in the
    audit write can never leave the flip applied without an audit row (the
    exact ADR-018 gap this feature closes). Proved here against a REAL
    connection (unprovable by a mocked one): force the real
    ``audit_service.record_audit`` call to raise AFTER the real UPDATE would
    have applied, and assert the row is UNCHANGED once the exception has
    propagated — a flipped-but-unaudited row must never be observable."""
    settings = _settings(cas_enabled=False)
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    monkeypatch.setattr(
        job_service.audit_service,
        "record_audit",
        AsyncMock(side_effect=RuntimeError("simulated audit-write crash")),
    )
    app = _build_app(pg_pool)
    job_id = await _insert_job(pg_pool, blind_review=True)

    async with await _client(app) as client:
        with pytest.raises(RuntimeError):
            await client.patch(f"/jobs/{job_id}", json={"blind_review": False})

    # The flip must NOT have applied — proves atomicity, not merely "an
    # error surfaced somewhere".
    assert await _job_blind_review(pg_pool, job_id) is True
    assert await _toggle_audit_rows(pg_pool, job_id) == []


# ── Case 1: a real human session -> user actor ─────────────────────────────


@pytest.mark.asyncio
async def test_flip_by_a_human_session_records_user_actor(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(cas_enabled=True)
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    username = _unique_username("priya")
    monkeypatch.setattr(
        auth_routes.cas_service, "validate_ticket", _mock_validate_ticket(username)
    )
    await _insert_user_with_role(pg_pool, cas_username=username)
    app = _build_app(pg_pool)
    job_id = await _insert_job(pg_pool, blind_review=True)

    async with await _client(app) as client:
        sid = await _login_and_get_sid(client, settings, username)
        resp = await client.patch(
            f"/jobs/{job_id}",
            json={"blind_review": False},
            cookies={settings.session_cookie_name: sid},
        )

    assert resp.status_code == 200

    async with pg_pool.acquire() as conn:
        user_id = await conn.fetchval(
            "SELECT id FROM users WHERE cas_username = $1", username
        )
    assert user_id is not None

    rows = await _toggle_audit_rows(pg_pool, job_id)
    assert len(rows) == 1
    row = rows[0]
    assert row["actor_kind"] == "user"
    assert row["actor_user_id"] == user_id
    assert row["actor_service"] is None


# ── Case 2: no session, CAS enabled -> 403, no audit row at all ────────────
#
# REVERSED (security finding, fix/auth-boundary-fails-open): this case used
# to be titled "no session, CAS enabled -> service/'api' actor, NOT 403" and
# asserted a 200 + an unattributable ``actor_kind='service'`` audit row for
# a bare service-key caller with no session. A live audit against real
# Postgres proved that was the vulnerability: this deploy's real config
# ships with zero ``API_KEY_*`` configured (so ``resolve_role`` trivially
# resolves ``Role.ADMIN`` for every caller regardless of any header), so a
# cookie-less, key-less request could reach this route with no credential
# at all — end-to-end proof is
# ``tests/integration/test_auth_boundary_fails_open_pg.py``. The human
# decision: a valid API key is NOT sufficient on its own — every write
# route now requires a REAL, resolvable CAS session, so ``blind_review`` is
# no longer an exception to the human-session requirement.


@pytest.mark.asyncio
async def test_flip_with_no_session_cas_enabled_403s_and_writes_no_audit_row(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REVERSED (security finding, fix/auth-boundary-fails-open) — see the
    section comment above. A bare service-key caller with no session must
    now 403, the column must stay unchanged, and no audit row (not even an
    unattributable ``actor_kind='service'`` one) may be written."""
    settings = _settings(cas_enabled=True)
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    app = _build_app(pg_pool)
    job_id = await _insert_job(pg_pool, blind_review=True)

    async with await _client(app) as client:
        resp = await client.patch(f"/jobs/{job_id}", json={"blind_review": False})

    assert resp.status_code == 403
    assert await _job_blind_review(pg_pool, job_id) is True

    rows = await _toggle_audit_rows(pg_pool, job_id)
    assert rows == []


# ── Case 3: CAS disabled -> dev-anonymous service actor ────────────────────


@pytest.mark.asyncio
async def test_flip_cas_disabled_records_dev_anonymous_service_actor(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(cas_enabled=False)
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    app = _build_app(pg_pool)
    job_id = await _insert_job(pg_pool, blind_review=True)

    async with await _client(app) as client:
        resp = await client.patch(f"/jobs/{job_id}", json={"blind_review": False})

    assert resp.status_code == 200

    rows = await _toggle_audit_rows(pg_pool, job_id)
    assert len(rows) == 1
    row = rows[0]
    assert row["actor_kind"] == "service"
    assert row["actor_service"] == "dev-anonymous"
    assert row["actor_user_id"] is None
