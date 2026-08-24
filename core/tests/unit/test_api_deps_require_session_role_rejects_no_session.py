"""RED pin — security finding, fix/auth-boundary-fails-open (F1a).

**The proven defect.** ``src.api.deps.require_session_role`` currently
PASSES whenever ``resolve_user()`` resolves ``None`` — "this gate never
judges the ABSENCE of a session, only a REAL session's role" (its own
module docstring, as merged in ADR-033). Combined with this deploy's real
config (zero ``API_KEY_*`` configured anywhere — 0 matches in
``docker-compose.yml``, ``compose.cas.yml``, ``.env.example``, or the
running container — so ``Settings.auth_enabled`` is ``False`` and
``resolve_role`` resolves ``Role.ADMIN`` for every request regardless of
any header), a cookie-less, key-less caller sails through EVERY write
route's ``require_role(...)`` AND ``require_session_role(...)`` gate with no
credential at all. Proved end-to-end against real Postgres: a bare
``PATCH /jobs/{id} {"blind_review": false}`` returned 200 and really
flipped the column, audited to an unattributable ``actor_kind='service'`` /
``actor_service='api'`` row.

**The human decision this file pins.** A valid API key is NEVER sufficient
on its own — every write route now requires a REAL, resolvable CAS session.
``require_session_role`` must 403 on ``user is None``, the exact same
mechanism (``fastapi.HTTPException(status_code=403, ...)``) it already uses
for an out-of-allowed-set session role.

**Why this file calls the returned closure directly, offline, with no
settings/DB at all (mirrors ``test_api_deps.py``'s own
``require_role_assigned`` coverage discipline).** ``require_session_role``'s
``user is None`` branch is exercised HERE regardless of ``cas_enabled``,
because ``resolve_user`` itself already guarantees ``user is None`` can only
ever be produced when CAS is enabled (see ``src.api.deps.resolve_user``: CAS
disabled unconditionally returns the synthetic dev-anonymous sentinel, never
``None``) — so a bare ``user=None`` input to this gate always represents the
"CAS enabled, no live session" case from production, and the CAS-disabled
sentinel case is represented here by passing the sentinel-shaped ``User``
directly, exactly as ``resolve_user`` would hand it to the route. The
router-level WIRING interaction (a live cookie-less HTTP request against a
real app) is proved separately in
``tests/integration/test_auth_boundary_fails_open_pg.py`` — a call here
proves the gate's pure LOGIC, not that a real request reaches it.
"""

from __future__ import annotations

import datetime as dt
from uuid import uuid4

import pytest
from fastapi import Depends, FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from src.api import deps
from src.api.deps import _DEV_ADMIN_SENTINEL_ID, Role, require_session_role
from src.schemas.auth import User

_NOW = dt.datetime(2026, 8, 11, tzinfo=dt.UTC)


def _session_user(*, role: str = "recruiter", active: bool = True) -> User:
    return User(
        id=uuid4(),
        cas_username="priya",
        display_name="priya",
        email=None,
        role=role,
        active=active,
        created_at=_NOW,
        last_seen_at=_NOW,
    )


def _dev_anonymous_sentinel() -> User:
    """The exact shape ``resolve_user`` returns when CAS is disabled — a
    real (non-``None``) ``User`` carrying the fixed sentinel id and
    ``role='admin'``."""
    return User(
        id=_DEV_ADMIN_SENTINEL_ID,
        cas_username="dev-anonymous",
        display_name="Dev Anonymous (auth disabled)",
        email=None,
        role="admin",
        active=True,
        created_at=_NOW,
        last_seen_at=_NOW,
    )


# ── the reversal: user is None must now 403, not PASS ──────────────────────


@pytest.mark.asyncio
async def test_require_session_role_403s_when_user_is_none() -> None:
    """THE fix this file pins. ``resolve_user`` -> ``None`` (CAS enabled,
    no live session at all — a cookie-less, key-less caller in production)
    must now be rejected, not waved through."""
    check = require_session_role(Role.ADMIN, Role.RECRUITER)
    with pytest.raises(HTTPException) as excinfo:
        await check(user=None)
    assert excinfo.value.status_code == 403


@pytest.mark.asyncio
async def test_require_session_role_403_on_none_has_a_non_empty_detail_message() -> (
    None
):
    check = require_session_role(Role.ADMIN, Role.RECRUITER)
    with pytest.raises(HTTPException) as excinfo:
        await check(user=None)
    assert excinfo.value.detail


@pytest.mark.asyncio
async def test_require_session_role_403_on_none_uses_the_plain_httpexception_mechanism() -> (  # noqa: E501
    None
):
    """Same 403 mechanism as the out-of-allowed-set-role branch and every
    other session gate in this module — a plain ``fastapi.HTTPException``,
    never a bespoke ``AppError`` subclass."""
    check = require_session_role(Role.ADMIN, Role.RECRUITER)
    with pytest.raises(HTTPException) as excinfo:
        await check(user=None)
    assert type(excinfo.value) is HTTPException


@pytest.mark.parametrize("allowed", [(Role.ADMIN,), (Role.ADMIN, Role.RECRUITER)])
@pytest.mark.asyncio
async def test_require_session_role_403s_on_none_regardless_of_the_allowed_set(
    allowed: tuple[Role, ...],
) -> None:
    """The 403-on-``None`` behaviour does not depend on which roles the
    route allows — it fires before any role comparison is even possible."""
    check = require_session_role(*allowed)
    with pytest.raises(HTTPException) as excinfo:
        await check(user=None)
    assert excinfo.value.status_code == 403


# ── must NOT break the two legitimate paths ─────────────────────────────────


@pytest.mark.parametrize("role", ["admin", "recruiter"])
@pytest.mark.asyncio
async def test_require_session_role_passes_a_real_session_with_an_allowed_role(
    role: str,
) -> None:
    check = require_session_role(Role.ADMIN, Role.RECRUITER)
    await check(user=_session_user(role=role))  # must not raise


@pytest.mark.asyncio
async def test_require_session_role_still_403s_a_real_session_with_a_disallowed_role() -> (  # noqa: E501
    None
):
    """Unaffected by this reversal — the pre-existing branch this gate was
    built for."""
    check = require_session_role(Role.ADMIN, Role.RECRUITER)
    with pytest.raises(HTTPException) as excinfo:
        await check(user=_session_user(role="auditor"))
    assert excinfo.value.status_code == 403


@pytest.mark.asyncio
async def test_require_session_role_passes_the_cas_disabled_dev_anonymous_sentinel() -> (  # noqa: E501
    None
):
    """The CAS-disabled synthetic dev-anonymous identity (``role='admin'``)
    is a REAL, non-``None`` ``User`` — it must keep passing through this
    gate exactly as before. This is the "do not break the sentinel" pin:
    the reversal targets ``user is None`` specifically, never a resolved
    ``User`` object, however synthetic."""
    check = require_session_role(Role.ADMIN, Role.RECRUITER)
    await check(user=_dev_anonymous_sentinel())  # must not raise


@pytest.mark.asyncio
async def test_require_session_role_composes_with_resolve_user_via_dependency_override() -> (  # noqa: E501
    None
):
    """End-to-end through a real (minimal) FastAPI app, mirroring
    ``test_api_deps.py``'s own ``require_role_assigned`` dependency-override
    coverage — proves the override mechanism, not just the bare function
    call above."""
    app = FastAPI()
    check = require_session_role(Role.ADMIN, Role.RECRUITER)

    @app.get("/protected", dependencies=[Depends(check)])
    async def _protected() -> dict[str, bool]:
        return {"ok": True}

    app.dependency_overrides[deps.resolve_user] = lambda: None
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/protected")
    assert resp.status_code == 403

    app.dependency_overrides[deps.resolve_user] = lambda: _session_user(role="admin")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/protected")
    assert resp.status_code == 200


__all__: list[str] = []
