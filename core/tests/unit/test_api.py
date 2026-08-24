"""Unit tests for the FastAPI app — Phase 0 exposes ``/health`` and nothing else.

The lifespan (asyncpg pool, arq, Neo4j) is stubbed out, so these run with no
live services. Job/resume/shortlist routes arrive in Phases 1-6.

Phase 1 adds the storage wiring: the lifespan builds a ``BlobStore`` rooted at
``settings.storage_dir`` and parks it on ``app.state.blob_store``, and the
``get_blob_store`` dependency hands it to routes (mirroring ``get_db``). Those
tests import the still-unwritten storage module lazily so this file keeps
collecting the Phase 0 tests until the implementation lands.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.main import app, health

# Template demo routes — Phase 0 deletes the agent-harness app.
DEMO_ROUTES: tuple[str, ...] = (
    "/tasks",
    "/tasks/{task_id}",
    "/tasks/{task_id}/lineage",
    "/memory/similar",
    "/gates/run",
)


@asynccontextmanager
async def _noop_lifespan(_app: FastAPI) -> AsyncIterator[None]:
    yield


@pytest.fixture
def client() -> Iterator[TestClient]:
    with patch.object(app.router, "lifespan_context", _noop_lifespan):
        with TestClient(app) as test_client:
            yield test_client


def test_health_endpoint_returns_200_ok(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_health_handler_returns_ok() -> None:
    assert await health() == {"status": "ok"}


def test_app_is_rebranded() -> None:
    title = app.title.lower()
    assert "recruiter" in title
    assert "harness" not in title


@pytest.mark.parametrize("path", DEMO_ROUTES)
def test_template_demo_routes_are_gone(path: str) -> None:
    paths = {getattr(route, "path", None) for route in app.routes}
    assert path not in paths


# Phase 6 (API routes): this guard's invariant genuinely changes here — the
# app now legitimately exposes the job/résumé/shortlist business routes, not
# just /health. Updated to a POSITIVE set-equality against the exact route
# table Phase 6 wires (src.api.routes.{jobs,resumes,shortlist}), so the guard
# still catches an ACCIDENTAL route addition/removal — it just no longer
# claims /health is the ONLY business route, because that claim is no longer
# true by design, not because the guard was weakened.
#
# FU-5 slice 6 (ADR-019 §10) widens this again: the CAS routes
# (``src.api.routes.auth``) are mounted alongside the Phase-6 routes. This is
# a RED-commit edit, not a coder green-ing a test — a coder is never allowed
# to touch a test to make it pass; this closed positive set is the one
# pre-existing assertion that would otherwise keep passing GREEN even while
# the new auth routes sit unregistered, silently masking exactly the
# "src.api.main forgot app.include_router(auth.router)" mistake this guard
# exists to catch. Widening it here, before the routes exist, is what makes
# it a real RED pin instead of a no-op.
#
# FU-5 slice 10 (ADR-019 §6 / §9.4) widens it a THIRD time, for the exact same
# reason: ``GET /audit/reveals-legacy`` (a read-only paginated view of the
# frozen ``reveal_audit`` table, gated admin+auditor per §9.4's ratified build
# decision 4) is added to the closed positive set here, in the RED commit,
# before ``src.api.routes.audit`` exists — the only way this guard actually
# catches "the coder built the route but forgot
# ``app.include_router(audit.router)`` in ``src.api.main``" instead of just
# passing vacuously.
#
# FU-6 slice 3 (ADR-020 §2) widens it a FOURTH time, same reason again: the
# assign/unassign routes (``src.api.routes.job_assignees``, admin/recruiter
# only — auditor may never assign) are added to the closed positive set here,
# in the RED commit, before ``src.api.routes.job_assignees`` exists.
#
# FU-6 slice 9 (ADR-020 §7) widens it a FIFTH time, same reason again:
# ``GET /my/jobs`` (the caller's own assigned job set, on the SAME
# ``src.api.routes.jobs`` router as the rest of the job routes — see
# ``test_route_jobs.py`` for the route-level contract) is added to the
# closed positive set here, in the RED commit, before the route exists on
# ``src.api.routes.jobs.router`` — the only way this guard actually catches
# "the coder implemented the route function but never decorated it (or
# decorated it under a different path)" instead of just passing vacuously.
#
# user-admin-roles slice 5 widens it a SIXTH time, same reason again:
# ``GET /users`` (the new ``src.api.routes.users`` admin-only user listing,
# gated by the CAS SESSION role — ``_require_admin_session`` — not by
# ``require_role``/an API key; see ``test_route_users.py`` for the route-level
# contract) is added to the closed positive set here, in the RED commit,
# before ``src.api.routes.users`` exists — the only way this guard actually
# catches "the coder built the route but forgot
# ``app.include_router(users.router)`` in ``src.api.main``" instead of just
# passing vacuously. Deliberately NOT added to
# ``test_router_role_gate.py``'s ``require_role_assigned`` business-router
# positive set: the admin session-gate this route carries is STRICTER than
# ``require_role_assigned`` (a no-role session is already 403'd by
# ``_require_admin_session`` alone), so there is nothing for that separate
# guard to additionally prove here.
_PHASE_6_ROUTES: frozenset[str] = frozenset(
    {
        "/health",
        "/jobs",
        "/jobs/jd-extract",
        "/jobs/bulk",
        "/jobs/{job_id}",
        "/jobs/{job_id}/status",
        "/jobs/{job_id}/reparse",
        "/jobs/{job_id}/resumes",
        "/resumes/{resume_id}",
        "/resumes/{resume_id}/reveal",
        "/resumes/{resume_id}/match-jobs",
        "/resumes/{resume_id}/match-results",
        "/jobs/{job_id}/shortlist",
        "/jobs/{job_id}/shortlist/export",
        # FU-7 §2 (ADR-021 §2 / ADR-029) — GET .../shortlist/status, the
        # fail-closed awaiting_llm state read, same router/RBAC as the rest
        # of the shortlist routes.
        "/jobs/{job_id}/shortlist/status",
        "/shortlist/{entry_id}",
        # FU-5 slice 6 (ADR-019 §10) — src.api.routes.auth, mounted with
        # prefix="/auth" per test_route_auth.py's locked contract.
        "/auth/cas/login",
        "/auth/cas/validate",
        "/auth/cas/logout",
        "/auth/cas/user",
        # FU-5 slice 10 (ADR-019 §6 / §9.4) — src.api.routes.audit, the
        # read-only legacy reveal-audit viewer, admin + auditor.
        "/audit/reveals-legacy",
        # Phase 1.4 (ADR-036) — the auditor's read of the LIVE audit_log.
        # Its sibling above reads reveal_audit, FROZEN at FU-5 slice 8; every
        # event since the cutover lives in audit_log, which until this route
        # had NO read path anywhere in the application. Admin + auditor, and
        # additionally gated on a real CAS session (ADR-036 §4).
        "/audit/log",
        # D1 = option C (answered 2026-08-19) — POST /audit/log/{id}/reveal,
        # the separately-audited reveal of a withheld details value. The only
        # route in the app that reads audit_log un-redacted, and the second
        # (after /resumes/{id}/reveal) whose write method performs an audited
        # read rather than a mutation.
        "/audit/log/{audit_id}/reveal",
        # FU-6 slice 3 (ADR-020 §2) — src.api.routes.job_assignees, the
        # assign/unassign routes, admin + recruiter only.
        "/jobs/{job_id}/assignees",
        "/jobs/{job_id}/assignees/{user_id}",
        # FU-6 slice 9 (ADR-020 §7) — GET /my/jobs, on src.api.routes.jobs's
        # existing router: the caller's own assigned job set, for any role.
        "/my/jobs",
        # user-admin-roles slice 5 — src.api.routes.users, the admin-only
        # user listing, gated on the CAS session role (see the comment block
        # above this frozenset).
        "/users",
        # user-admin-roles slice 6 — PATCH /users/{user_id}/role, same
        # router/gate as the listing above: an admin assigns a role, with a
        # role_changed audit row and a last-admin-lockout guard.
        "/users/{user_id}/role",
        # FU-8 (ADR-026) — the résumé-withdrawal lifecycle routes on
        # src.api.routes.resumes's existing router: withdraw/reinstate
        # (admin+recruiter) and the per-job status breakdown (all roles).
        "/resumes/{resume_id}/withdraw",
        "/resumes/{resume_id}/reinstate",
        "/jobs/{job_id}/resume-status",
    }
)


def _all_route_paths(routes: object) -> set[str]:
    """Flatten a ``routes`` iterable into the full set of registered path
    strings, recursively.

    The installed FastAPI wraps every ``include_router(...)`` target as an
    opaque ``_IncludedRouter`` (its own ``.path`` is ``None``; the real
    ``APIRoute``s live on ``.original_router.routes``) rather than flattening
    them directly onto ``app.routes`` — so a shallow ``{r.path for r in
    app.routes}`` silently drops every Phase-6 route behind an
    ``include_router`` call. Walk recursively so the guard actually sees them.
    """
    paths: set[str] = set()
    for route in routes:  # type: ignore[attr-defined]
        sub_router = getattr(route, "original_router", None)
        if sub_router is not None:
            paths |= _all_route_paths(getattr(sub_router, "routes", ()))
            continue
        path = getattr(route, "path", None)
        if path is not None:
            paths.add(path)
    return paths


def test_health_is_the_only_business_route() -> None:
    paths = {
        p
        for p in _all_route_paths(app.routes)
        if not p.startswith(("/openapi", "/docs", "/redoc"))
    }
    assert paths == _PHASE_6_ROUTES


def test_the_cut_review_workflow_routes_are_absent() -> None:
    """hris's shortlist decision/stage routes are deliberately never wired —
    there is no review pipeline in this project."""
    paths = _all_route_paths(app.routes)
    assert "/shortlist/{entry_id}/decision" not in paths
    assert "/shortlist/{entry_id}/stage" not in paths


# ── Phase 1 — BlobStore wiring ──────────────────────────────────────────────


def _request_for(app_: FastAPI) -> MagicMock:
    request = MagicMock()
    request.app = app_
    return request


@pytest.mark.asyncio
async def test_lifespan_parks_a_blob_store_on_app_state(tmp_path: Path) -> None:
    """The real lifespan (external IO mocked) must set ``app.state.blob_store``.

    The store is proved to be rooted at ``settings.storage_dir`` functionally:
    a ``put`` through it lands under ``tmp_path`` — no dependence on the
    implementation's private root attribute.
    """
    from src.api.main import lifespan
    from src.settings import Settings
    from src.storage.blob_store import BlobStore

    fresh = FastAPI()
    arq = MagicMock()
    arq.close = AsyncMock()
    driver = MagicMock()
    driver.close = AsyncMock()

    with (
        patch(
            "src.api.main.get_settings",
            return_value=Settings(storage_dir=str(tmp_path)),
        ),
        patch("src.api.main.init_pool", AsyncMock(return_value=MagicMock())),
        patch("src.api.main.init_schema", AsyncMock()),
        patch("src.api.main.AsyncGraphDatabase.driver", return_value=driver),
        patch("src.api.main.bootstrap_neo4j_schema", AsyncMock()),
        patch("src.api.main.create_pool", AsyncMock(return_value=arq)),
    ):
        async with lifespan(fresh):
            store = getattr(fresh.state, "blob_store", None)
            assert isinstance(store, BlobStore)
            await store.put("wire.txt", b"ok")

    assert (tmp_path / "wire.txt").read_bytes() == b"ok"


# ── L3 (security re-audit round 2): SKILL_HASH_SALT symmetry with the worker ──
#
# ``src.worker.main.startup`` already refuses to start on an empty
# ``skill_hash_salt`` (ADR-008). The API lifespan never hashes a skill name
# itself today, so this is currently latent — but it must fail exactly as
# loud, for exactly the same reason (an unsalted hash of a non-vocab skill
# name is dictionary-attackable), should a future code path reach it from
# this process, or should the salt only be misconfigured for the API.


@pytest.mark.asyncio
async def test_lifespan_raises_when_skill_hash_salt_is_empty(tmp_path: Path) -> None:
    from src.api.main import lifespan
    from src.settings import Settings

    fresh = FastAPI()
    settings = Settings(storage_dir=str(tmp_path), skill_hash_salt="")

    with (
        patch("src.api.main.get_settings", return_value=settings),
        patch(
            "src.api.main.init_pool", AsyncMock(return_value=MagicMock())
        ) as init_pool,
        patch("src.api.main.init_schema", AsyncMock()),
        patch("src.api.main.AsyncGraphDatabase.driver", return_value=MagicMock()),
        patch("src.api.main.bootstrap_neo4j_schema", AsyncMock()),
        patch("src.api.main.create_pool", AsyncMock(return_value=MagicMock())),
    ):
        with pytest.raises(RuntimeError, match="SKILL_HASH_SALT"):
            async with lifespan(fresh):
                pass

    init_pool.assert_not_called()


@pytest.mark.asyncio
async def test_lifespan_does_not_raise_when_skill_hash_salt_is_configured(
    tmp_path: Path,
) -> None:
    from src.api.main import lifespan
    from src.settings import Settings

    fresh = FastAPI()
    arq = MagicMock()
    arq.close = AsyncMock()
    driver = MagicMock()
    driver.close = AsyncMock()
    settings = Settings(storage_dir=str(tmp_path), skill_hash_salt="a-real-salt")

    with (
        patch("src.api.main.get_settings", return_value=settings),
        patch("src.api.main.init_pool", AsyncMock(return_value=MagicMock())),
        patch("src.api.main.init_schema", AsyncMock()),
        patch("src.api.main.AsyncGraphDatabase.driver", return_value=driver),
        patch("src.api.main.bootstrap_neo4j_schema", AsyncMock()),
        patch("src.api.main.create_pool", AsyncMock(return_value=arq)),
    ):
        async with lifespan(fresh):
            pass  # must not raise


def test_get_blob_store_returns_the_store_from_app_state() -> None:
    from src.storage.blob_store import get_blob_store

    fresh = FastAPI()
    sentinel = object()
    fresh.state.blob_store = sentinel
    assert get_blob_store(_request_for(fresh)) is sentinel


def test_get_blob_store_raises_a_clear_error_when_absent() -> None:
    """Mirrors the ``get_db`` missing-pool test — a distinct, clear failure."""
    from src.storage.blob_store import get_blob_store

    fresh = FastAPI()
    with pytest.raises(RuntimeError, match="(?i)blob"):
        get_blob_store(_request_for(fresh))


# ── FU-4 (RBAC): startup refuses to boot on a bad auth config ────────────
#
# Mirrors the SKILL_HASH_SALT lifespan tests above exactly. The actual
# validation logic lives in ``src.settings.validate_startup_auth_config``
# (unit-tested directly in ``test_settings_rbac.py``); these tests pin that
# the LIFESPAN actually calls it, at the same point the SKILL_HASH_SALT
# check runs, before any pool/schema/graph work begins.


@pytest.mark.asyncio
async def test_lifespan_raises_when_a_legacy_api_key_is_configured(
    tmp_path: Path,
) -> None:
    from src.api.main import lifespan
    from src.settings import Settings

    fresh = FastAPI()
    settings = Settings(
        storage_dir=str(tmp_path),
        skill_hash_salt="a-real-salt",
        api_key="a-stale-legacy-secret",
    )

    with (
        patch("src.api.main.get_settings", return_value=settings),
        patch(
            "src.api.main.init_pool", AsyncMock(return_value=MagicMock())
        ) as init_pool,
        patch("src.api.main.init_schema", AsyncMock()),
        patch("src.api.main.AsyncGraphDatabase.driver", return_value=MagicMock()),
        patch("src.api.main.bootstrap_neo4j_schema", AsyncMock()),
        patch("src.api.main.create_pool", AsyncMock(return_value=MagicMock())),
    ):
        with pytest.raises(RuntimeError, match="API_KEY_ADMIN"):
            async with lifespan(fresh):
                pass

    init_pool.assert_not_called()


@pytest.mark.asyncio
async def test_lifespan_raises_when_two_role_keys_are_byte_identical(
    tmp_path: Path,
) -> None:
    from src.api.main import lifespan
    from src.settings import Settings

    fresh = FastAPI()
    settings = Settings(
        storage_dir=str(tmp_path),
        skill_hash_salt="a-real-salt",
        api_key_admin="collided-secret",
        api_key_recruiter="collided-secret",
    )

    with (
        patch("src.api.main.get_settings", return_value=settings),
        patch(
            "src.api.main.init_pool", AsyncMock(return_value=MagicMock())
        ) as init_pool,
        patch("src.api.main.init_schema", AsyncMock()),
        patch("src.api.main.AsyncGraphDatabase.driver", return_value=MagicMock()),
        patch("src.api.main.bootstrap_neo4j_schema", AsyncMock()),
        patch("src.api.main.create_pool", AsyncMock(return_value=MagicMock())),
    ):
        with pytest.raises(RuntimeError):
            async with lifespan(fresh):
                pass

    init_pool.assert_not_called()


@pytest.mark.asyncio
async def test_lifespan_does_not_raise_with_a_valid_distinct_role_key_config(
    tmp_path: Path,
) -> None:
    from src.api.main import lifespan
    from src.settings import Settings

    fresh = FastAPI()
    arq = MagicMock()
    arq.close = AsyncMock()
    driver = MagicMock()
    driver.close = AsyncMock()
    settings = Settings(
        storage_dir=str(tmp_path),
        skill_hash_salt="a-real-salt",
        api_key_admin="admin-1",
        api_key_recruiter="recruiter-2",
        api_key_hiring_manager="hm-3",
        api_key_auditor="auditor-4",
    )

    with (
        patch("src.api.main.get_settings", return_value=settings),
        patch("src.api.main.init_pool", AsyncMock(return_value=MagicMock())),
        patch("src.api.main.init_schema", AsyncMock()),
        patch("src.api.main.AsyncGraphDatabase.driver", return_value=driver),
        patch("src.api.main.bootstrap_neo4j_schema", AsyncMock()),
        patch("src.api.main.create_pool", AsyncMock(return_value=arq)),
    ):
        async with lifespan(fresh):
            pass  # must not raise


@pytest.mark.asyncio
async def test_lifespan_does_not_raise_on_the_fully_disabled_default_auth_config(
    tmp_path: Path,
) -> None:
    """Today's local-dev default (all four role keys empty, no legacy
    ``api_key`` set) must keep booting exactly as it does before FU-4."""
    from src.api.main import lifespan
    from src.settings import Settings

    fresh = FastAPI()
    arq = MagicMock()
    arq.close = AsyncMock()
    driver = MagicMock()
    driver.close = AsyncMock()
    settings = Settings(storage_dir=str(tmp_path), skill_hash_salt="a-real-salt")

    with (
        patch("src.api.main.get_settings", return_value=settings),
        patch("src.api.main.init_pool", AsyncMock(return_value=MagicMock())),
        patch("src.api.main.init_schema", AsyncMock()),
        patch("src.api.main.AsyncGraphDatabase.driver", return_value=driver),
        patch("src.api.main.bootstrap_neo4j_schema", AsyncMock()),
        patch("src.api.main.create_pool", AsyncMock(return_value=arq)),
    ):
        async with lifespan(fresh):
            pass  # must not raise
