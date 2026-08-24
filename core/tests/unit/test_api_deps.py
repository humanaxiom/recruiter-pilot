"""Unit tests for FU-4's cross-cutting API dependencies — ``src.api.deps``.

**REWRITE (FU-4, keyed roles).** The Phase-6 single-switch ``require_api_key``
dependency is RETIRED entirely — the route→role table needs different
allowed-role sets on different routes of the SAME router (e.g. ``PATCH
/jobs/{id}`` is admin/recruiter-only while ``GET /jobs/{id}`` is open to all
four roles), which a single boolean pass/fail dependency cannot express. It is
replaced by two primitives:

* ``Role(str, Enum)`` — ``ADMIN``/``RECRUITER``/``HIRING_MANAGER``/``AUDITOR``,
  values ``"admin"``/``"recruiter"``/``"hiring_manager"``/``"auditor"``.
* ``resolve_role(x_api_key: str | None = Header(alias="X-API-Key")) -> Role``
  — the KEY→ROLE resolution step. ``settings.auth_enabled is False`` (all
  four ``api_key_*`` fields empty) -> ALWAYS resolves ``Role.ADMIN``,
  regardless of what ``x_api_key`` carries (today's fail-open-by-explicit-
  configuration local-dev mode, preserved verbatim — "nothing that works
  today with auth off may change behavior"). Enabled: the header must match
  exactly one configured role key (constant-time, UTF-8 bytes), else
  ``HTTPException(401)``.
* ``require_role(*allowed: Role)`` — a dependency FACTORY. Returns an async
  callable with signature ``async def _check(role: Annotated[Role,
  Depends(resolve_role)]) -> Role`` (so it composes ``resolve_role`` as its
  own sub-dependency rather than re-parsing the header) that raises
  ``HTTPException(403)`` when the resolved role is not in ``allowed``, else
  returns it. Calling the factory's *return value* directly with a keyword
  ``role=`` argument (bypassing FastAPI's DI) is the locked shape route test
  files rely on for isolated unit coverage.

**Why route tests override ``resolve_role``, never ``require_role(...)``:**
every ``Depends(require_role(Role.ADMIN, Role.RECRUITER))`` call site
produces a DISTINCT closure object, even across two routes with the identical
allowed-role tuple — so ``app.dependency_overrides`` cannot target "every
require_role(...) call" as one entry. ``resolve_role`` is the single shared
function object every one of those closures depends on, so overriding IT
propagates through every route's role check uniformly. This is proved
directly below, by
``test_require_role_composes_with_resolve_role_via_dependency_override``,
and is the exact mechanism every ``test_route_*.py`` file's ``_build_app``
helper now relies on in place of the old
``app.dependency_overrides[require_api_key] = lambda: None`` bypass.

``get_arq`` is UNCHANGED from Phase 6 — kept here for regression coverage.
``log_auth_mode`` is updated: it now reads ``settings.auth_enabled`` (not the
retired ``settings.api_key`` truthiness) so it warns/informs consistently
with the new four-key switch.

**RETIRED (FU-5 slice 7, ADR-019 §8.3).** ``resolve_actor`` and the
``X-Actor-Name`` header it read are DELETED entirely — "the optional,
unverified ``X-Actor-Name`` header ... is removed entirely". The five
``resolve_actor`` regression tests that lived in this file under Phase 6 are
deleted in this same commit (not left red pointing at a function the ADR says
must not exist); ``test_resolve_actor_no_longer_exists`` below is the
positive pin for the deletion. ``created_by``/``uploaded_by`` are now sourced
from ``src.api.deps.resolve_user`` (ADR-019 §9.2/§10) — see
``test_route_jobs.py``, ``test_route_jobs_bulk.py``, ``test_route_resumes.py``
and ``test_route_reveal.py`` for the call-site coverage of that switch.

**FU-5 slice 12 (ADR-019 §10 step 5) — sliding-window refresh wiring.** The
tests near the bottom of this file (``test_resolve_user_calls_refresh_if_
needed_...`` and friends) pin that ``resolve_user`` calls
``session_service.refresh_if_needed`` when a session resolves, with
settings-derived ``ttl_seconds``/``idle_refresh_seconds`` — closing a
reviewer finding that ``refresh_if_needed`` existed and was unit-tested in
isolation (``tests/unit/test_session_service.py``) but nothing on the
request path ever called it, so sessions hard-expired at the fixed TTL
instead of sliding. Nothing here mocks a real database — the actual
``expires_at`` extension is real-Postgres behaviour and is separately pinned
in ``tests/integration/test_auth_routes_pg.py``.

**FU-5 slice 13 (security gate, FIX #1) — insecure-cookie startup warning.**
``log_auth_mode`` gains a SECOND, independent warning: ``cas_enabled=True``
combined with ``session_cookie_secure=False`` (the dev default) must emit a
WARNING mentioning the insecure cookie at startup — not fatal (ADR-019
§3/§10b frame fail-closed as a configured-deployment concern, and dev may
legitimately run plain http), just loud enough that a misconfigured
deployment cannot silently ship a session cookie that travels in the clear.
``session_cookie_secure=True`` OR ``cas_enabled=False`` must NOT emit it.
Pure settings/log-line logic, no DB — ``offline`` genuinely suffices; see the
tests under "log_auth_mode — insecure-cookie startup warning" below.

**FU-6 slice 4 (ADR-020 §3/§4/§5) — ``scoped_user_id_or_403``, the row-
scoping identity helper.** The Flask viewer presents ONE shared ``recruiter``
API key for every browser user (``core/frontend/api_client.py``), so the
key-derived ``Role`` (``resolve_role``) cannot identify a hiring_manager.
Row-scoping therefore keys off the CAS SESSION identity (``resolve_user``'s
``User | None``), never the API-key role. See the dedicated section near the
bottom of this file for the full decision-table coverage.

**user-admin-roles slice 3 — ``require_role_assigned``, the fail-closed
no-role gate.** Slice 2 (ADR-019 §10a/§2 reversal) stopped defaulting a
non-default-admin CAS login to ``role='recruiter'`` — a first login now
captures ``role=None``. But nothing yet BLOCKS that no-role user: the Flask
viewer always presents the ONE shared ``recruiter`` API key for every
browser, so ``require_role`` passes on the KEY alone regardless of who is
signed in, and ``scoped_user_id_or_403`` returns ``None`` (UNSCOPED) for any
non-hiring_manager SESSION role — including ``role=None`` — so a genuinely
no-role human ends up with full recruiter-equivalent, company-wide access.
``require_role_assigned`` closes this hole:

* ``user is None`` (a bare service-key caller, no session at all) -> PASS,
  unaffected — that caller is already governed by ``require_role`` alone;
  this gate only judges a REAL resolved session's ``role``.
* ``user is not None and user.role is None`` (a real, logged-in, no-role
  human) -> ``HTTPException(403)``, fail-closed.
* ``user is not None and user.role is not None`` (any real assigned role, OR
  the CAS-disabled dev-anonymous synthetic sentinel whose ``role='admin'``)
  -> PASS.

**REVERSED 2026-08-19 (D2 = option B, docs/OPEN_DECISIONS.md §D2).** The
first bullet above described this gate's behaviour only up to that date.
Product decided reads must be symmetric with writes: ``user is None`` now
ALSO ``HTTPException(403)``s -- closing the last case this gate still
passed unscoped, the sharpest instance being a bare key reading
``GET /audit/reveals-legacy`` unattributably. See the dedicated
``test_require_role_assigned_403s_a_bare_service_key_caller_with_no_session``
below (renamed from ``..._passes_..._with_no_session``, reversed in place)
and ``tests/unit/test_api_deps_d2_close_unscoped_reads.py`` for the fuller
new-behaviour coverage.

Wired ONCE, at the ROUTER level (``app.include_router(..., dependencies=[
Depends(require_role_assigned)])``) on every business router
(``jobs``/``resumes``/``shortlist``/``job_assignees``/``audit``) in
``src.api.main`` — deliberately NOT on ``auth``, so a no-role user can still
reach ``/auth/cas/user``/``/auth/cas/logout`` to see their own status or log
out. The end-to-end router-level wiring interaction (real session cookie +
the real shared recruiter key, through a real FastAPI app) is proved in
``tests/integration/test_no_role_fail_closed_pg.py`` — a mocked
``resolve_user`` here cannot prove that wiring actually blocks a live
request; this file pins only the pure gate LOGIC.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import Depends, FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from src.schemas.auth import User
from src.settings import Settings


def _settings(
    *,
    api_key_admin: str = "",
    api_key_recruiter: str = "",
    api_key_hiring_manager: str = "",
    api_key_auditor: str = "",
    cas_enabled: bool = False,
    session_cookie_secure: bool = False,
) -> Settings:
    return Settings(
        api_key_admin=api_key_admin,
        api_key_recruiter=api_key_recruiter,
        api_key_hiring_manager=api_key_hiring_manager,
        api_key_auditor=api_key_auditor,
        cas_enabled=cas_enabled,
        session_cookie_secure=session_cookie_secure,
        skill_hash_salt="test-salt",
        pii_key="test-key",
    )


# ── Role enum ─────────────────────────────────────────────────────────────


def test_role_enum_has_exactly_the_four_expected_members() -> None:
    from src.api.deps import Role

    assert {r.value for r in Role} == {
        "admin",
        "recruiter",
        "hiring_manager",
        "auditor",
    }


def test_role_enum_members_are_string_valued() -> None:
    from src.api.deps import Role

    assert Role.ADMIN == "admin"
    assert Role.RECRUITER == "recruiter"
    assert Role.HIRING_MANAGER == "hiring_manager"
    assert Role.AUDITOR == "auditor"


# ── resolve_role: enabled, key→role resolution ──────────────────────────


@pytest.mark.asyncio
async def test_resolve_role_maps_the_admin_key_to_admin(monkeypatch: Any) -> None:
    from src.api import deps

    monkeypatch.setattr(
        deps, "get_settings", lambda: _settings(api_key_admin="admin-secret")
    )
    assert await deps.resolve_role(x_api_key="admin-secret") == deps.Role.ADMIN


@pytest.mark.asyncio
async def test_resolve_role_maps_the_recruiter_key_to_recruiter(
    monkeypatch: Any,
) -> None:
    from src.api import deps

    monkeypatch.setattr(
        deps, "get_settings", lambda: _settings(api_key_recruiter="recruiter-secret")
    )
    assert await deps.resolve_role(x_api_key="recruiter-secret") == deps.Role.RECRUITER


@pytest.mark.asyncio
async def test_resolve_role_maps_the_hiring_manager_key_to_hiring_manager(
    monkeypatch: Any,
) -> None:
    from src.api import deps

    monkeypatch.setattr(
        deps, "get_settings", lambda: _settings(api_key_hiring_manager="hm-secret")
    )
    assert await deps.resolve_role(x_api_key="hm-secret") == deps.Role.HIRING_MANAGER


@pytest.mark.asyncio
async def test_resolve_role_maps_the_auditor_key_to_auditor(monkeypatch: Any) -> None:
    from src.api import deps

    monkeypatch.setattr(
        deps, "get_settings", lambda: _settings(api_key_auditor="auditor-secret")
    )
    assert await deps.resolve_role(x_api_key="auditor-secret") == deps.Role.AUDITOR


@pytest.mark.asyncio
async def test_resolve_role_401s_an_unknown_key_when_enabled(monkeypatch: Any) -> None:
    from src.api import deps

    monkeypatch.setattr(
        deps, "get_settings", lambda: _settings(api_key_admin="admin-secret")
    )
    with pytest.raises(HTTPException) as excinfo:
        await deps.resolve_role(x_api_key="totally-wrong")
    assert excinfo.value.status_code == 401


@pytest.mark.asyncio
async def test_resolve_role_401s_a_missing_key_when_enabled(monkeypatch: Any) -> None:
    from src.api import deps

    monkeypatch.setattr(
        deps, "get_settings", lambda: _settings(api_key_admin="admin-secret")
    )
    with pytest.raises(HTTPException) as excinfo:
        await deps.resolve_role(x_api_key=None)
    assert excinfo.value.status_code == 401


@pytest.mark.asyncio
async def test_resolve_role_401s_a_key_that_matches_no_configured_role(
    monkeypatch: Any,
) -> None:
    """A syntactically plausible key that just doesn't match ANY of the four
    configured secrets — distinct scenario from a totally-empty header."""
    from src.api import deps

    monkeypatch.setattr(
        deps,
        "get_settings",
        lambda: _settings(
            api_key_admin="admin-secret", api_key_recruiter="recruiter-secret"
        ),
    )
    with pytest.raises(HTTPException) as excinfo:
        await deps.resolve_role(x_api_key="hiring-manager-secret")
    assert excinfo.value.status_code == 401


@pytest.mark.asyncio
async def test_resolve_role_401s_not_500_on_a_non_ascii_key(monkeypatch: Any) -> None:
    """SEC-1 regression pin, carried forward under the new keyed-role
    resolution: Starlette latin-1-decodes header bytes, so a non-ASCII
    ``X-API-Key`` is a valid (non-ASCII) ``str``. ``secrets.compare_digest``
    on two ``str`` requires both ASCII-only or raises ``TypeError`` — must
    never propagate as an unhandled 500; always fails closed with 401."""
    from src.api import deps

    monkeypatch.setattr(
        deps, "get_settings", lambda: _settings(api_key_admin="admin-secret")
    )
    with pytest.raises(HTTPException) as excinfo:
        await deps.resolve_role(x_api_key="clé-secrète-café")
    assert excinfo.value.status_code == 401


# ── resolve_role: disabled (all four empty) ─────────────────────────────


@pytest.mark.asyncio
async def test_resolve_role_disabled_always_resolves_admin_with_no_header(
    monkeypatch: Any,
) -> None:
    from src.api import deps

    monkeypatch.setattr(deps, "get_settings", lambda: _settings())
    assert await deps.resolve_role(x_api_key=None) == deps.Role.ADMIN


@pytest.mark.asyncio
async def test_resolve_role_disabled_always_resolves_admin_with_a_bogus_header(
    monkeypatch: Any,
) -> None:
    """Nothing that works today with auth off may change behavior — a bogus
    header is simply ignored, exactly as the retired ``require_api_key``
    ignored one when disabled."""
    from src.api import deps

    monkeypatch.setattr(deps, "get_settings", lambda: _settings())
    assert await deps.resolve_role(x_api_key="anything-at-all") == deps.Role.ADMIN


# ── resolve_role: constant-time comparison preserved ────────────────────


@pytest.mark.asyncio
async def test_resolve_role_compares_via_secrets_compare_digest_not_eq(
    monkeypatch: Any,
) -> None:
    import secrets as real_secrets

    from src.api import deps

    monkeypatch.setattr(
        deps, "get_settings", lambda: _settings(api_key_admin="admin-secret")
    )
    spy = MagicMock(wraps=real_secrets.compare_digest)
    monkeypatch.setattr(deps.secrets, "compare_digest", spy)
    await deps.resolve_role(x_api_key="admin-secret")
    assert spy.called, "expected resolve_role to compare via secrets.compare_digest"
    # Every call must compare BYTES (never raw str), so a non-ASCII header
    # can never hit compare_digest's ASCII-only str/str path and raise.
    for call in spy.call_args_list:
        for arg in call.args:
            assert isinstance(arg, bytes)


@pytest.mark.asyncio
async def test_resolve_role_does_not_short_circuit_after_the_first_match(
    monkeypatch: Any,
) -> None:
    """``resolve_role`` must compare EVERY configured role key on every
    request, even after it has already found a match — never stop early.

    All four role keys are configured, and the presented key is
    ``api_key_admin``'s value: the FIRST candidate ``_configured_role_keys``
    yields. A short-circuit (``break``/early ``return`` right after the
    match) would stop the loop after exactly one ``secrets.compare_digest``
    call. Comparing all four regardless of where the match falls is what
    keeps neither response timing nor comparison count from leaking which
    role a near-miss guess was closest to (see ADR-018 §5)."""
    import secrets as real_secrets

    from src.api import deps

    monkeypatch.setattr(
        deps,
        "get_settings",
        lambda: _settings(
            api_key_admin="admin-secret",
            api_key_recruiter="recruiter-secret",
            api_key_hiring_manager="hiring-manager-secret",
            api_key_auditor="auditor-secret",
        ),
    )
    spy = MagicMock(wraps=real_secrets.compare_digest)
    monkeypatch.setattr(deps.secrets, "compare_digest", spy)
    result = await deps.resolve_role(x_api_key="admin-secret")
    assert result == deps.Role.ADMIN
    assert spy.call_count == 4, (
        "expected resolve_role to compare all four configured role keys "
        "even after matching the first one (no short-circuit)"
    )


# ── require_role: per-route allowed-role check ──────────────────────────


@pytest.mark.asyncio
async def test_require_role_returns_the_role_when_it_is_allowed() -> None:
    from src.api import deps

    checker = deps.require_role(deps.Role.ADMIN, deps.Role.RECRUITER)
    result = await checker(role=deps.Role.RECRUITER)
    assert result == deps.Role.RECRUITER


@pytest.mark.asyncio
async def test_require_role_allows_every_member_of_its_own_allowed_set() -> None:
    from src.api import deps

    checker = deps.require_role(deps.Role.ADMIN, deps.Role.RECRUITER)
    assert await checker(role=deps.Role.ADMIN) == deps.Role.ADMIN
    assert await checker(role=deps.Role.RECRUITER) == deps.Role.RECRUITER


@pytest.mark.asyncio
async def test_require_role_403s_a_role_outside_the_allowed_set() -> None:
    from src.api import deps

    checker = deps.require_role(deps.Role.ADMIN, deps.Role.RECRUITER)
    with pytest.raises(HTTPException) as excinfo:
        await checker(role=deps.Role.AUDITOR)
    assert excinfo.value.status_code == 403


@pytest.mark.asyncio
async def test_require_role_403s_hiring_manager_too_when_not_allowed() -> None:
    from src.api import deps

    checker = deps.require_role(deps.Role.ADMIN, deps.Role.RECRUITER)
    with pytest.raises(HTTPException) as excinfo:
        await checker(role=deps.Role.HIRING_MANAGER)
    assert excinfo.value.status_code == 403


@pytest.mark.asyncio
async def test_require_role_with_a_single_allowed_role_admits_only_that_role() -> None:
    from src.api import deps

    checker = deps.require_role(deps.Role.ADMIN)
    assert await checker(role=deps.Role.ADMIN) == deps.Role.ADMIN
    with pytest.raises(HTTPException) as excinfo:
        await checker(role=deps.Role.RECRUITER)
    assert excinfo.value.status_code == 403


@pytest.mark.asyncio
async def test_require_role_with_all_four_roles_admits_every_role() -> None:
    from src.api import deps

    all_roles = tuple(deps.Role)
    checker = deps.require_role(*all_roles)
    for role in all_roles:
        assert await checker(role=role) == role


# ── require_role composes with resolve_role (the override mechanism every
#    test_route_*.py file relies on) ─────────────────────────────────────


@pytest.mark.asyncio
async def test_require_role_composes_with_resolve_role_via_dependency_override() -> (
    None
):
    """Proves the exact mechanism ``test_route_*.py``'s ``_build_app`` helpers
    use in place of the retired ``dependency_overrides[require_api_key] =
    lambda: None`` bypass: overriding the SHARED ``resolve_role`` dependency
    (never the route-specific ``require_role(...)`` closure) propagates
    through every route's role check."""
    from src.api import deps

    app = FastAPI()

    @app.get("/protected", dependencies=[Depends(deps.require_role(deps.Role.ADMIN))])
    async def _protected() -> dict[str, bool]:
        return {"ok": True}

    app.dependency_overrides[deps.resolve_role] = lambda: deps.Role.RECRUITER
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/protected")
    assert resp.status_code == 403

    app.dependency_overrides[deps.resolve_role] = lambda: deps.Role.ADMIN
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/protected")
    assert resp.status_code == 200


# ── no role key is ever logged ───────────────────────────────────────────


def test_resolve_role_never_logs_any_configured_role_key(
    caplog: pytest.LogCaptureFixture, monkeypatch: Any
) -> None:
    """Regression pin (decision: "No role key is ever logged"): whatever
    ``resolve_role`` DOES log (if anything) on a failed lookup must never
    include any of the configured secrets, nor the wrong key that was
    presented."""
    import asyncio

    from src.api import deps

    settings = _settings(
        api_key_admin="admin-secret-value",
        api_key_recruiter="recruiter-secret-value",
        api_key_hiring_manager="hm-secret-value",
        api_key_auditor="auditor-secret-value",
    )
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    caplog.set_level(logging.DEBUG)

    with pytest.raises(HTTPException):
        asyncio.run(deps.resolve_role(x_api_key="an-attacker-supplied-guess"))

    joined = " ".join(r.getMessage() for r in caplog.records)
    for leaked in (
        "admin-secret-value",
        "recruiter-secret-value",
        "hm-secret-value",
        "auditor-secret-value",
        "an-attacker-supplied-guess",
    ):
        assert leaked not in joined


# ── resolve_actor is RETIRED (FU-5 slice 7, ADR-019 §8.3) ────────────────
#
# DELETED (not rewritten): the five Phase-6 ``resolve_actor`` regression
# tests that used to live here (``..._returns_the_header_value_when_present``,
# ``..._defaults_to_api_when_absent``, ``..._defaults_to_api_for_an_empty_
# header``, ``..._caps_an_overlong_header``,
# ``..._is_never_consulted_by_resolve_role_or_require_role``). ADR-019 §8.3:
# "the optional, unverified ``X-Actor-Name`` header ... is removed entirely
# ... A spoofable identity-shaped header next to a cryptographically verified
# one invites confusion and misconfiguration; removing ``X-Actor-Name``
# closes that risk." Keeping those tests around (even rewritten) would pin a
# function this ADR says must not exist. The positive replacement pin is
# below; behavioural coverage of the new identity source
# (``src.api.deps.resolve_user``) lives in ``test_route_auth.py`` (already
# landed, slice 6) and the route-level call-site tests added in
# ``test_route_jobs.py`` / ``test_route_jobs_bulk.py`` /
# ``test_route_resumes.py`` / ``test_route_reveal.py`` (slice 7).


def test_resolve_actor_no_longer_exists() -> None:
    """ADR-019 §8.3 — ``resolve_actor`` must not exist on ``src.api.deps`` at
    all, not merely be unused. A future re-introduction (e.g. a well-meaning
    revert, or a merge conflict resolution that resurrects it) fails this
    test immediately instead of silently reopening the spoofable-header risk
    the ADR closed."""
    from src.api import deps

    assert not hasattr(deps, "resolve_actor")


def test_x_actor_name_header_alias_appears_nowhere_in_deps_module_source() -> None:
    """Belt-and-suspenders on the same ADR-019 §8.3 decision: even a
    differently-named function must not still declare a FastAPI ``Header``
    bound to the ``X-Actor-Name`` wire name anywhere in this module."""
    import inspect

    from src.api import deps

    source = inspect.getsource(deps)
    assert "X-Actor-Name" not in source


# ── get_arq (Phase 6, unchanged) ─────────────────────────────────────────


def _request_for(app: FastAPI) -> MagicMock:
    request = MagicMock()
    request.app = app
    return request


def test_get_arq_returns_the_pool_from_app_state() -> None:
    from src.api.deps import get_arq

    app = FastAPI()
    sentinel = object()
    app.state.arq = sentinel
    assert get_arq(_request_for(app)) is sentinel


def test_get_arq_raises_a_clear_runtime_error_when_absent() -> None:
    from src.api.deps import get_arq

    app = FastAPI()
    with pytest.raises(RuntimeError, match="(?i)arq"):
        get_arq(_request_for(app))


# ── log_auth_mode — now driven by Settings.auth_enabled ─────────────────


def test_log_auth_mode_warns_loudly_when_all_four_keys_are_empty(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from src.api.deps import log_auth_mode

    caplog.set_level(logging.WARNING)
    log_auth_mode(_settings())
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "expected at least one WARNING-level log record"
    joined = " ".join(r.getMessage().upper() for r in warnings)
    assert "AUTH" in joined
    assert "DISABLED" in joined


@pytest.mark.parametrize(
    "field",
    [
        "api_key_admin",
        "api_key_recruiter",
        "api_key_hiring_manager",
        "api_key_auditor",
    ],
)
def test_log_auth_mode_does_not_warn_when_any_single_role_key_is_set(
    field: str, caplog: pytest.LogCaptureFixture
) -> None:
    from src.api.deps import log_auth_mode

    caplog.set_level(logging.WARNING)
    log_auth_mode(_settings(**{field: "secret123"}))
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings == []


def test_log_auth_mode_never_logs_a_configured_role_key(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from src.api.deps import log_auth_mode

    caplog.set_level(logging.DEBUG)
    log_auth_mode(_settings(api_key_admin="super-secret-admin-key"))
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "super-secret-admin-key" not in joined


# ── log_auth_mode — insecure-cookie startup warning (security gate, FU-5
#    slice 13, FIX #1). ``cas_enabled=True`` combined with
#    ``session_cookie_secure=False`` (the dev default) must emit a SEPARATE
#    WARNING mentioning the insecure cookie — not fatal (dev may legitimately
#    run plain http, ADR-019 §3/§10b frame fail-closed as a configured-
#    deployment concern only), just loud. ``session_cookie_secure=True`` OR
#    ``cas_enabled=False`` must NOT emit it. ───────────────────────────────


def test_log_auth_mode_warns_about_insecure_cookie_when_cas_enabled_and_cookie_insecure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from src.api.deps import log_auth_mode

    caplog.set_level(logging.WARNING)
    log_auth_mode(_settings(cas_enabled=True, session_cookie_secure=False))
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    joined = " ".join(r.getMessage().upper() for r in warnings)
    assert "COOKIE" in joined
    assert "INSECURE" in joined


def test_log_auth_mode_does_not_warn_about_insecure_cookie_when_cookie_is_secure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from src.api.deps import log_auth_mode

    caplog.set_level(logging.WARNING)
    log_auth_mode(_settings(cas_enabled=True, session_cookie_secure=True))
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    joined = " ".join(r.getMessage().upper() for r in warnings)
    assert "INSECURE" not in joined


def test_log_auth_mode_does_not_warn_about_insecure_cookie_when_cas_disabled(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from src.api.deps import log_auth_mode

    caplog.set_level(logging.WARNING)
    log_auth_mode(_settings(cas_enabled=False, session_cookie_secure=False))
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    joined = " ".join(r.getMessage().upper() for r in warnings)
    assert "INSECURE" not in joined


@pytest.mark.parametrize(
    "cas_enabled,session_cookie_secure,expect_warning",
    [
        (True, False, True),
        (True, True, False),
        (False, False, False),
        (False, True, False),
    ],
)
def test_log_auth_mode_insecure_cookie_warning_matrix(
    cas_enabled: bool,
    session_cookie_secure: bool,
    expect_warning: bool,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from src.api.deps import log_auth_mode

    caplog.set_level(logging.WARNING)
    log_auth_mode(
        _settings(cas_enabled=cas_enabled, session_cookie_secure=session_cookie_secure)
    )
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    joined = " ".join(r.getMessage().upper() for r in warnings)
    assert ("INSECURE" in joined) is expect_warning


def test_log_auth_mode_insecure_cookie_warning_is_independent_of_the_auth_disabled_warning(  # noqa: E501
    caplog: pytest.LogCaptureFixture,
) -> None:
    """With CAS disabled (no insecure-cookie warning expected) and the role
    keys ALSO all empty, the pre-existing "AUTH DISABLED" warning must still
    fire — the two warnings are separate concerns and neither's absence/
    presence should suppress the other."""
    from src.api.deps import log_auth_mode

    caplog.set_level(logging.WARNING)
    log_auth_mode(_settings(cas_enabled=False, session_cookie_secure=False))
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    joined = " ".join(r.getMessage().upper() for r in warnings)
    assert "AUTH" in joined
    assert "DISABLED" in joined
    assert "INSECURE" not in joined


# ── resolve_user — sliding-window refresh wiring (FU-5 slice 12, ADR-019
#    §10 step 5) ─────────────────────────────────────────────────────────
#
# ``resolve_user`` currently resolves a live session via
# ``session_service.get_active_session`` and stops there — nothing on the
# request path ever calls ``session_service.refresh_if_needed`` (already
# implemented and unit-tested in isolation by
# ``tests/unit/test_session_service.py``), so ``settings.
# session_idle_refresh_hours`` is unread and every session hard-expires at
# the fixed TTL instead of sliding, contradicting ADR-019 §10 step 5 ("with
# a sliding-window refresh"). These tests spy on
# ``session_service.refresh_if_needed`` to pin the WIRING — that
# ``resolve_user`` calls it, with which conn/session/ttl/idle arguments, and
# when it must NOT be called at all. The real ``expires_at`` extension is
# genuine Postgres behaviour and is pinned separately in
# ``tests/integration/test_auth_routes_pg.py`` — a mocked spy here cannot
# prove the database write actually lands.


def _cas_settings(
    *, cas_enabled: bool = True, ttl_hours: int = 8, idle_hours: int = 1
) -> Settings:
    return Settings(
        cas_enabled=cas_enabled,
        session_ttl_hours=ttl_hours,
        session_idle_refresh_hours=idle_hours,
        skill_hash_salt="test-salt",
        pii_key="test-key",
    )


def _live_session() -> Any:
    from src.schemas.auth import Session

    return Session(
        id="tok-live-session",
        user_id=uuid4(),
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        revoked_at=None,
        user_agent=None,
        ip_addr=None,
        created_at=datetime.now(UTC),
    )


def _live_user(user_id: Any) -> Any:
    from src.schemas.auth import User as _User

    now = datetime.now(UTC)
    return _User(
        id=user_id,
        cas_username="alice",
        display_name=None,
        email=None,
        role="recruiter",
        active=True,
        created_at=now,
        last_seen_at=now,
    )


@pytest.mark.asyncio
async def test_resolve_user_calls_refresh_if_needed_when_a_session_resolves(
    monkeypatch: Any,
) -> None:
    from src.api import deps

    settings = _cas_settings(ttl_hours=8, idle_hours=1)
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    session = _live_session()
    user = _live_user(session.user_id)

    monkeypatch.setattr(
        deps.session_service, "get_active_session", AsyncMock(return_value=session)
    )
    refresh_spy = AsyncMock(return_value=session)
    monkeypatch.setattr(deps.session_service, "refresh_if_needed", refresh_spy)
    monkeypatch.setattr(deps.user_service, "get_by_id", AsyncMock(return_value=user))

    db = MagicMock()
    result = await deps.resolve_user(request=MagicMock(), db=db, ra_session=session.id)

    assert result is not None
    assert result.cas_username == "alice"
    refresh_spy.assert_awaited_once()
    call = refresh_spy.await_args
    assert call is not None
    assert call.args[0] is db
    assert call.args[1] is session
    assert call.kwargs["ttl_seconds"] == 8 * 3600
    assert call.kwargs["idle_refresh_seconds"] == 1 * 3600


@pytest.mark.parametrize(
    "ttl_hours,idle_hours",
    [(8, 1), (4, 2), (12, 3)],
)
@pytest.mark.asyncio
async def test_resolve_user_derives_refresh_seconds_from_settings_not_hardcoded(
    ttl_hours: int, idle_hours: int, monkeypatch: Any
) -> None:
    """``session_ttl_hours``/``session_idle_refresh_hours`` must be READ from
    settings on every call, never a hard-coded value baked into
    ``resolve_user`` — CLAUDE.md's "config only via src/settings.py"."""
    from src.api import deps

    settings = _cas_settings(ttl_hours=ttl_hours, idle_hours=idle_hours)
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    session = _live_session()
    user = _live_user(session.user_id)

    monkeypatch.setattr(
        deps.session_service, "get_active_session", AsyncMock(return_value=session)
    )
    refresh_spy = AsyncMock(return_value=session)
    monkeypatch.setattr(deps.session_service, "refresh_if_needed", refresh_spy)
    monkeypatch.setattr(deps.user_service, "get_by_id", AsyncMock(return_value=user))

    await deps.resolve_user(request=MagicMock(), db=MagicMock(), ra_session=session.id)

    refresh_spy.assert_awaited_once()
    call = refresh_spy.await_args
    assert call is not None
    assert call.kwargs["ttl_seconds"] == ttl_hours * 3600
    assert call.kwargs["idle_refresh_seconds"] == idle_hours * 3600


@pytest.mark.asyncio
async def test_resolve_user_with_no_cookie_never_calls_refresh_if_needed(
    monkeypatch: Any,
) -> None:
    from src.api import deps

    settings = _cas_settings()
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    refresh_spy = AsyncMock()
    monkeypatch.setattr(deps.session_service, "refresh_if_needed", refresh_spy)
    get_active_spy = AsyncMock()
    monkeypatch.setattr(deps.session_service, "get_active_session", get_active_spy)

    result = await deps.resolve_user(
        request=MagicMock(), db=MagicMock(), ra_session=None
    )

    assert result is None
    get_active_spy.assert_not_awaited()
    refresh_spy.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolve_user_with_no_live_session_never_calls_refresh_if_needed(
    monkeypatch: Any,
) -> None:
    """A cookie present but resolving to nothing live (missing/revoked/
    expired — ``get_active_session`` returns ``None`` for all three,
    deliberately indistinguishable) must never reach ``refresh_if_needed``
    at all."""
    from src.api import deps

    settings = _cas_settings()
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    monkeypatch.setattr(
        deps.session_service, "get_active_session", AsyncMock(return_value=None)
    )
    refresh_spy = AsyncMock()
    monkeypatch.setattr(deps.session_service, "refresh_if_needed", refresh_spy)

    result = await deps.resolve_user(
        request=MagicMock(), db=MagicMock(), ra_session="stale-or-revoked-token"
    )

    assert result is None
    refresh_spy.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolve_user_dev_anonymous_path_never_touches_sessions_at_all(
    monkeypatch: Any,
) -> None:
    """CAS disabled: the synthetic dev-admin identity is returned without
    ever consulting a session — no ``get_active_session`` call, and
    therefore no ``refresh_if_needed`` call either. There is no session to
    slide."""
    from src.api import deps

    settings = _cas_settings(cas_enabled=False)
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    get_active_spy = AsyncMock()
    monkeypatch.setattr(deps.session_service, "get_active_session", get_active_spy)
    refresh_spy = AsyncMock()
    monkeypatch.setattr(deps.session_service, "refresh_if_needed", refresh_spy)

    result = await deps.resolve_user(
        request=MagicMock(), db=MagicMock(), ra_session="whatever-cookie-value"
    )

    assert result is not None
    assert result.cas_username == "dev-anonymous"
    get_active_spy.assert_not_awaited()
    refresh_spy.assert_not_awaited()


# ── scoped_user_id_or_403 — CAS-identity row-scoping helper (FU-6 slice 4,
#    ADR-020 §3/§4/§5) ─────────────────────────────────────────────────────
#
# The Flask viewer presents ONE shared ``recruiter`` API key for every
# browser user (``core/frontend/api_client.py``), so the key-derived
# ``Role`` (``resolve_role``) cannot identify a hiring_manager — every
# browser request "looks like" ``Role.RECRUITER`` regardless of which human
# is actually signed in. Row-scoping therefore keys off the CAS SESSION
# identity (``resolve_user``, a real ``User | None`` with ``.id``/``.role``),
# never the API-key role.
#
# ``scoped_user_id_or_403(user: User | None, key_role: Role) -> UUID | None``
# returns:
#   * a real ``UUID`` — the caller is scoped, filter every query by this id
#   * ``None`` — the caller is UNSCOPED (admin/recruiter/auditor, or the
#     dev-anonymous synthetic admin, or a bare service/admin key with no
#     session at all) — see every row
#   * raises ``HTTPException(403)`` — a caller CLAIMS a scoped role
#     (``key_role == Role.HIRING_MANAGER``) but has no verifiable session
#     identity to scope against, or a mismatched one. Fail-closed: never
#     silently serve company-wide data to a caller who should see one
#     requisition (ADR-020 §3, final paragraph).
#
# This is PURE branching logic over ``Role``/``User | None`` — no I/O, no
# database, no network. ``offline`` genuinely suffices AND is the only layer
# that can fully prove the decision table below; the actual row-filtering
# behaviour (the WHERE/EXISTS predicate hitting a real Postgres table) is
# proved later, per ADR-020 §3's affected-route matrix, by the scoping
# slices' integration tests — NOT here. No integration test is added in this
# slice; it would not exercise anything an integration harness can prove that
# this in-memory decision table doesn't already prove more cheaply.


def _scoped_user(role: str, user_id: Any = None) -> User:
    """Build a real (non-sentinel) ``User`` with the given ``.role``."""
    now = datetime.now(UTC)
    return User(
        id=user_id if user_id is not None else uuid4(),
        cas_username="someone",
        display_name=None,
        email=None,
        role=role,
        active=True,
        created_at=now,
        last_seen_at=now,
    )


def _dev_anonymous_admin() -> User:
    """The exact synthetic identity ``resolve_user`` returns when
    ``cas_enabled=False`` — see ``deps._DEV_ADMIN_SENTINEL_ID``."""
    from src.api.deps import _DEV_ADMIN_SENTINEL_ID

    now = datetime.now(UTC)
    return User(
        id=_DEV_ADMIN_SENTINEL_ID,
        cas_username="dev-anonymous",
        display_name="Dev Anonymous (auth disabled)",
        email=None,
        role="admin",
        active=True,
        created_at=now,
        last_seen_at=now,
    )


# 1. A real hiring_manager session -> SCOPED to their own id, regardless of
#    which key_role the (shared, browser-wide) API key resolved to. This is
#    the actual Flask-browser path: the browser always presents the shared
#    ``recruiter`` key, so ``key_role`` is almost always ``Role.RECRUITER``
#    even for a hiring-manager human — the "browser always looks like
#    recruiter" trap this helper exists to avoid.


@pytest.mark.asyncio
async def test_scoped_user_id_or_403_scopes_a_real_hiring_manager_with_hiring_manager_key() -> (  # noqa: E501
    None
):
    from src.api.deps import Role, scoped_user_id_or_403

    user = _scoped_user("hiring_manager")
    result = await scoped_user_id_or_403(user, Role.HIRING_MANAGER)
    assert result == user.id


@pytest.mark.asyncio
async def test_scoped_user_id_or_403_scopes_a_real_hiring_manager_even_with_the_shared_recruiter_key() -> (  # noqa: E501
    None
):
    """The critical trap test: the Flask viewer's browser session always
    presents the ONE shared ``recruiter`` API key, so ``key_role`` resolves
    to ``Role.RECRUITER`` even when the signed-in human is a
    hiring_manager. Scoping MUST key off the session identity, not the key
    role, or this exact combination silently serves unscoped data to every
    hiring-manager browser user."""
    from src.api.deps import Role, scoped_user_id_or_403

    user = _scoped_user("hiring_manager")
    result = await scoped_user_id_or_403(user, Role.RECRUITER)
    assert result == user.id


@pytest.mark.asyncio
async def test_scoped_user_id_or_403_scopes_a_real_hiring_manager_with_admin_key() -> (
    None
):
    from src.api.deps import Role, scoped_user_id_or_403

    user = _scoped_user("hiring_manager")
    result = await scoped_user_id_or_403(user, Role.ADMIN)
    assert result == user.id


@pytest.mark.asyncio
async def test_scoped_user_id_or_403_scopes_a_real_hiring_manager_with_auditor_key() -> (  # noqa: E501
    None
):
    from src.api.deps import Role, scoped_user_id_or_403

    user = _scoped_user("hiring_manager")
    result = await scoped_user_id_or_403(user, Role.AUDITOR)
    assert result == user.id


# 2. A hiring_manager API key with NO verifiable session identity -> 403,
#    never unscoped. Fail-closed.


@pytest.mark.asyncio
async def test_scoped_user_id_or_403_403s_hiring_manager_key_with_no_session_user() -> (
    None
):
    from src.api.deps import Role, scoped_user_id_or_403

    with pytest.raises(HTTPException) as excinfo:
        await scoped_user_id_or_403(None, Role.HIRING_MANAGER)
    assert excinfo.value.status_code == 403


# 3. A hiring_manager API key with a session that resolves to a DIFFERENT
#    (mismatched) role -> 403, never granted that other identity's scope.


@pytest.mark.parametrize("mismatched_role", ["admin", "recruiter", "auditor"])
@pytest.mark.asyncio
async def test_scoped_user_id_or_403_403s_hiring_manager_key_with_mismatched_session_role(  # noqa: E501
    mismatched_role: str,
) -> None:
    from src.api.deps import Role, scoped_user_id_or_403

    user = _scoped_user(mismatched_role)
    with pytest.raises(HTTPException) as excinfo:
        await scoped_user_id_or_403(user, Role.HIRING_MANAGER)
    assert excinfo.value.status_code == 403


# 4. admin/recruiter/auditor session identity, key_role NOT hiring_manager
#    -> None (UNSCOPED). Auditor is DELIBERATELY unscoped per ADR-020 §4
#    ("auditor: NOT scoped — retains global read access across all jobs").


@pytest.mark.parametrize("session_role", ["admin", "recruiter", "auditor"])
@pytest.mark.parametrize("key_role_name", ["ADMIN", "RECRUITER", "AUDITOR"])
@pytest.mark.asyncio
async def test_scoped_user_id_or_403_returns_none_for_unscoped_session_roles(
    session_role: str, key_role_name: str
) -> None:
    from src.api.deps import Role, scoped_user_id_or_403

    key_role = getattr(Role, key_role_name)
    user = _scoped_user(session_role)
    result = await scoped_user_id_or_403(user, key_role)
    assert result is None


@pytest.mark.asyncio
async def test_scoped_user_id_or_403_returns_none_for_auditor_session_explicitly() -> (
    None
):
    """ADR-020 §4 pin, standalone from the parametrized matrix above: an
    auditor session is unscoped even when presented with its own auditor
    key — auditors keep global read, compensated by audit-log-everything
    (ADR-020 §6), not by row scoping."""
    from src.api.deps import Role, scoped_user_id_or_403

    user = _scoped_user("auditor")
    result = await scoped_user_id_or_403(user, Role.AUDITOR)
    assert result is None


# 5. dev-anonymous synthetic admin (cas_enabled=False path) -> None
#    (UNSCOPED — dev/CI unaffected by row scoping).


@pytest.mark.asyncio
async def test_scoped_user_id_or_403_returns_none_for_dev_anonymous_admin() -> None:
    from src.api.deps import Role, scoped_user_id_or_403

    user = _dev_anonymous_admin()
    result = await scoped_user_id_or_403(user, Role.ADMIN)
    assert result is None


# 6. user is None AND key_role in {admin, recruiter, auditor} -> None
#    (UNSCOPED, not 403 — a bare service/admin key with no session is simply
#    not a scoped role; only a HIRING_MANAGER key with no session fails
#    closed).


@pytest.mark.parametrize("key_role_name", ["ADMIN", "RECRUITER", "AUDITOR"])
@pytest.mark.asyncio
async def test_scoped_user_id_or_403_returns_none_for_no_session_with_unscoped_key(
    key_role_name: str,
) -> None:
    from src.api.deps import Role, scoped_user_id_or_403

    key_role = getattr(Role, key_role_name)
    result = await scoped_user_id_or_403(None, key_role)
    assert result is None


# ── return-type and mechanism pins ───────────────────────────────────────


@pytest.mark.asyncio
async def test_scoped_user_id_or_403_return_type_is_a_real_uuid_when_scoped() -> None:
    from uuid import UUID

    from src.api.deps import Role, scoped_user_id_or_403

    user = _scoped_user("hiring_manager")
    result = await scoped_user_id_or_403(user, Role.HIRING_MANAGER)
    assert isinstance(result, UUID)


@pytest.mark.asyncio
async def test_scoped_user_id_or_403_403_matches_the_reveal_403_mechanism() -> None:
    """Pin the exact 403 mechanism to what the rest of the codebase already
    uses for an authorization-scope failure (``require_role``'s 403 and
    ``routes.resumes.reveal_resume``'s "reveal requires an attributable
    human session" 403) — a plain ``fastapi.HTTPException(status_code=403,
    ...)``, not a ``src.errors.AppError`` subclass or a bespoke exception
    type, so the global FastAPI exception machinery renders it identically
    to every other 403 in this API without any new handler."""
    from src.api.deps import Role, scoped_user_id_or_403

    with pytest.raises(HTTPException) as excinfo:
        await scoped_user_id_or_403(None, Role.HIRING_MANAGER)
    assert isinstance(excinfo.value, HTTPException)
    assert excinfo.value.status_code == 403
    assert excinfo.value.detail, "expected a non-empty detail message on the 403"


@pytest.mark.parametrize(
    ("session_role", "key_role_name"),
    [
        ("admin", "ADMIN"),
        ("recruiter", "RECRUITER"),
        ("auditor", "AUDITOR"),
        ("admin", "RECRUITER"),
    ],
)
@pytest.mark.asyncio
async def test_scoped_user_id_or_403_never_returns_a_non_none_value_for_a_non_hiring_manager_session(  # noqa: E501
    session_role: str, key_role_name: str
) -> None:
    """Belt-and-suspenders: for every session whose ``.role`` is not
    ``"hiring_manager"``, the helper must return exactly ``None`` — never a
    ``UUID`` — regardless of which unscoped key_role is presented. A
    non-``None`` return for a non-hiring_manager session would mean some
    other role is being silently row-scoped, which ADR-020 §4 never asks
    for."""
    from src.api.deps import Role, scoped_user_id_or_403

    key_role = getattr(Role, key_role_name)
    user = _scoped_user(session_role)
    result = await scoped_user_id_or_403(user, key_role)
    assert result is None


# ── require_role_assigned — the fail-closed no-role gate (user-admin-roles
#    slice 3) ────────────────────────────────────────────────────────────
#
# See the module docstring section above for the full decision table. Pure
# ``User | None`` branching logic — no I/O — mirroring
# ``scoped_user_id_or_403``'s own "offline genuinely suffices for the LOGIC"
# framing; the router-level WIRING interaction through a real session +
# the real shared recruiter key is proved separately, against real
# Postgres, in ``tests/integration/test_no_role_fail_closed_pg.py``.


def _no_role_user() -> User:
    """A real (non-sentinel) session ``User`` whose ``role`` is ``None`` --
    exactly what slice 2's first-login reversal now produces for a
    non-default-admin CAS username on their first login."""
    now = datetime.now(UTC)
    return User(
        id=uuid4(),
        cas_username="someone",
        display_name=None,
        email=None,
        role=None,
        active=True,
        created_at=now,
        last_seen_at=now,
    )


@pytest.mark.asyncio
async def test_require_role_assigned_403s_a_bare_service_key_caller_with_no_session() -> (  # noqa: E501
    None
):
    """REVERSED 2026-08-19 -- D2 = option B, ``docs/OPEN_DECISIONS.md`` §D2,
    answered by product; the question ADR-034's "Accepted residuals" carried
    forward. Until today this test asserted the OPPOSITE: ``user is None``
    PASSED, by documented design -- "this gate never judges the ABSENCE of a
    session, only a REAL session's role" -- so a bare service-key caller
    with no CAS session got unscoped reads across every business router.
    Product decided reads must be symmetric with writes (ADR-034 F1a already
    403s ``user is None`` for ``require_session_role``): every read now
    needs a real principal too, closing the sharpest instance -- a bare key
    reading ``GET /audit/reveals-legacy`` unattributably. See
    ``tests/unit/test_api_deps_d2_close_unscoped_reads.py`` for the
    dedicated new-behaviour coverage this reversal is paired with, and
    ``tests/integration/test_close_unscoped_reads_pg.py`` for the router-
    level proof."""
    from src.api.deps import require_role_assigned

    with pytest.raises(HTTPException) as excinfo:
        await require_role_assigned(user=None)
    assert excinfo.value.status_code == 403


@pytest.mark.parametrize("role", ["admin", "recruiter", "hiring_manager", "auditor"])
@pytest.mark.asyncio
async def test_require_role_assigned_passes_every_real_assigned_role(
    role: str,
) -> None:
    from src.api.deps import require_role_assigned

    user = _scoped_user(role)
    await require_role_assigned(user=user)  # must not raise


@pytest.mark.asyncio
async def test_require_role_assigned_403s_a_real_session_with_no_role() -> None:
    """The core fix: a genuinely resolved session (``user is not None``)
    whose ``role`` is ``None`` — exactly what slice 2's first-login reversal
    now produces for a non-default-admin CAS username — must be blocked,
    fail-closed, before any route body runs."""
    from src.api.deps import require_role_assigned

    user = _no_role_user()
    with pytest.raises(HTTPException) as excinfo:
        await require_role_assigned(user=user)
    assert excinfo.value.status_code == 403


@pytest.mark.asyncio
async def test_require_role_assigned_403_has_a_non_empty_detail_message() -> None:
    from src.api.deps import require_role_assigned

    user = _no_role_user()
    with pytest.raises(HTTPException) as excinfo:
        await require_role_assigned(user=user)
    assert excinfo.value.detail


@pytest.mark.asyncio
async def test_require_role_assigned_passes_the_dev_anonymous_sentinel() -> None:
    """The CAS-disabled synthetic dev-admin identity (``role='admin'``) is
    not a no-role human — it's the ambient dev/CI identity — so it must
    PASS, exactly like ``scoped_user_id_or_403`` treats it as UNSCOPED
    rather than blocked."""
    from src.api.deps import require_role_assigned

    user = _dev_anonymous_admin()
    await require_role_assigned(user=user)  # must not raise


@pytest.mark.asyncio
async def test_require_role_assigned_403_matches_the_reveal_403_mechanism() -> None:
    """Same 403 mechanism pin as ``scoped_user_id_or_403`` above — a plain
    ``fastapi.HTTPException(status_code=403, ...)``, not a bespoke
    ``AppError`` subclass, so the global exception machinery renders it
    identically to every other 403 in this API."""
    from src.api.deps import require_role_assigned

    user = _no_role_user()
    with pytest.raises(HTTPException) as excinfo:
        await require_role_assigned(user=user)
    assert isinstance(excinfo.value, HTTPException)
    assert excinfo.value.status_code == 403


@pytest.mark.asyncio
async def test_require_role_assigned_composes_with_resolve_user_via_dependency_override() -> (  # noqa: E501
    None
):
    """Proves the router-level override mechanism end-to-end through a real
    (minimal) FastAPI app — mirroring
    ``test_require_role_composes_with_resolve_role_via_dependency_override``
    above exactly, but for the session-identity gate instead of the
    key-role gate. Overriding the SHARED ``resolve_user`` dependency
    propagates through ``require_role_assigned``'s check.

    **Final block REVERSED 2026-08-19** -- D2 = option B,
    ``docs/OPEN_DECISIONS.md`` §D2. Overriding ``resolve_user`` to ``None``
    used to compose to 200 (the pre-D2 PASS-on-``None`` contract); it now
    must compose to 403, the same as every other business route reached
    with no session at all."""
    from src.api import deps

    app = FastAPI()

    @app.get("/protected", dependencies=[Depends(deps.require_role_assigned)])
    async def _protected() -> dict[str, bool]:
        return {"ok": True}

    app.dependency_overrides[deps.resolve_user] = lambda: _no_role_user()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/protected")
    assert resp.status_code == 403

    app.dependency_overrides[deps.resolve_user] = lambda: _scoped_user("recruiter")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/protected")
    assert resp.status_code == 200

    app.dependency_overrides[deps.resolve_user] = lambda: None
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/protected")
    assert resp.status_code == 403
