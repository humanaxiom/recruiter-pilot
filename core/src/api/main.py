"""FastAPI application — recruiter-assistant.

Phase 0 exposes ``/health`` and nothing else. The lifespan brings up the three
things every later phase depends on: the asyncpg pool (+ idempotent startup
DDL — there is no migration framework), the Neo4j schema bootstrap, and the arq
queue. Job / resume / shortlist routes arrive in Phases 1-6.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from arq import create_pool
from arq.connections import RedisSettings
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from neo4j import AsyncGraphDatabase

from src.api.deps import log_auth_mode, require_role_assigned
from src.api.routes import audit, auth, job_assignees, jobs, resumes, shortlist, users
from src.errors import AppError
from src.models.ddl import init_schema
from src.models.pool import close_pool, init_pool
from src.settings import get_settings, validate_startup_auth_config
from src.storage.blob_store import BlobStore
from src.worker.neo4j_bootstrap import bootstrap_neo4j_schema


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    s = get_settings()
    # L3 (security re-audit round 2): symmetry with ``src.worker.main.startup``'s
    # ADR-008 check. The API never hashes a skill name itself today (only the
    # worker's `skills_graph` resolution does), so this is currently latent —
    # but a future API-side skill-resolution code path (or the salt being
    # misconfigured only for THIS process) must fail loud here too, not start
    # up with a passphrase that would make any hash it later computes
    # dictionary-attackable.
    if not s.skill_hash_salt:
        raise RuntimeError(
            "SKILL_HASH_SALT is empty — refusing to start the API; "
            "non-vocab Skill nodes would be keyed by an unsalted (dictionary-"
            "attackable) hash. Set SKILL_HASH_SALT."
        )
    # FU-4 — refuse to boot on an auth config that would fail open (a stale
    # legacy API_KEY, D1) or silently collapse two roles into one (two
    # byte-identical role keys). Same discipline as the salt check above:
    # loud RuntimeError, before any pool/schema/graph work begins.
    validate_startup_auth_config(s)
    # The configurable auth switch. A loud WARNING when disabled, an
    # informational line otherwise; called once, here, at startup.
    log_auth_mode(s)
    pool = await init_pool(app, s)
    # The schema comes up with the app — every statement is idempotent.
    await init_schema(pool)

    driver = AsyncGraphDatabase.driver(
        s.neo4j_uri, auth=(s.neo4j_user, s.neo4j_password)
    )
    await bootstrap_neo4j_schema(driver)
    app.state.neo4j = driver

    # Filesystem blob primitive — Phase-6 routes reach it via get_blob_store.
    app.state.blob_store = BlobStore(s.storage_dir)

    app.state.arq = await create_pool(RedisSettings.from_dsn(s.redis_url))
    try:
        yield
    finally:
        await app.state.arq.close()
        await driver.close()
        await close_pool(app)


app = FastAPI(
    title="Recruiter Assistant API",
    version="0.1.0",
    lifespan=lifespan,
)

# auth stays UNGATED — a no-role user must still be able to see their own
# status (GET /auth/cas/user) and log out; every business router below is
# gated by require_role_assigned (user-admin-roles slice 3, ADR-019 §10a/
# §10b reversal follow-up) so a real no-role session fail-closed 403s before
# any route body runs, regardless of the shared API key it presents.
app.include_router(audit.router, dependencies=[Depends(require_role_assigned)])
app.include_router(auth.router)
app.include_router(job_assignees.router, dependencies=[Depends(require_role_assigned)])
app.include_router(jobs.router, dependencies=[Depends(require_role_assigned)])
app.include_router(resumes.router, dependencies=[Depends(require_role_assigned)])
app.include_router(shortlist.router, dependencies=[Depends(require_role_assigned)])
# /users is gated by its own, stricter _require_admin_session (the CAS
# session role) — see src.api.routes.users's module docstring — so it is
# deliberately NOT wrapped in require_role_assigned here.
app.include_router(users.router)


@app.exception_handler(AppError)
async def _app_error_handler(_request: Request, exc: AppError) -> JSONResponse:
    content = {"code": exc.code, "message": exc.message, **exc.context}
    return JSONResponse(status_code=exc.status, content=content)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
