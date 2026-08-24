"""RED pin — security finding, fix/auth-boundary-fails-open (F5).

**The proven defect.** ``users.active`` is consulted by NONE of the four
session gates that decide whether a resolved CAS session may act:
``src.api.deps.require_session_role``, ``src.api.deps.require_role_assigned``,
``src.api.routes.users._require_admin_session``, and
``src.api.deps.scoped_user_id_or_403``. Each judges only ``user.role`` (or,
for ``scoped_user_id_or_403``, ``user.role == "hiring_manager"``) — a
deactivated account whose row still carries a real, allowed ``role`` sails
through every one of them exactly like an active account would.

**Why this is exploitable, not theoretical.** ``src.services.session_service
.refresh_if_needed`` slides a session's expiry forward on every request that
lands within ``idle_refresh_seconds`` of the current expiry — it never
checks whether the underlying ``users`` row is still active. An
administrator who deactivates a departing employee's account (``users.active
= false``) does NOT revoke that employee's live session; if they keep
polling the UI (or anything else) often enough to keep sliding the refresh
window, their session — and therefore their write/read access — need NEVER
expire. Deactivation is currently cosmetic.

**The fix this file pins.** Every one of the four gates must also reject
``user.active is False`` with the SAME 403 mechanism (plain
``fastapi.HTTPException``) each already uses for its existing role-based
rejection. An ACTIVE session with an otherwise-allowed role must keep
passing — this is not a blanket 403, only a deactivation-aware one.

Pure ``User | None`` branching logic — no I/O — mirroring
``test_api_deps.py``'s own "offline genuinely suffices for the LOGIC"
framing for ``require_role_assigned``/``scoped_user_id_or_403``. The
router-level WIRING interaction (a live request carrying a deactivated
user's still-valid session cookie) is a separate, follow-on integration
concern and is NOT this file's job.
"""

from __future__ import annotations

import datetime as dt
from uuid import uuid4

import pytest
from fastapi import HTTPException

from src.api.deps import (
    Role,
    require_role_assigned,
    require_session_role,
    scoped_user_id_or_403,
)
from src.api.routes.users import _require_admin_session
from src.schemas.auth import User

_NOW = dt.datetime(2026, 8, 11, tzinfo=dt.UTC)


def _user(*, role: str | None = "recruiter", active: bool = True) -> User:
    return User(
        id=uuid4(),
        cas_username="jordan",
        display_name="jordan",
        email=None,
        role=role,
        active=active,
        created_at=_NOW,
        last_seen_at=_NOW,
    )


# ── require_session_role ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_require_session_role_403s_a_deactivated_recruiter_session() -> None:
    check = require_session_role(Role.ADMIN, Role.RECRUITER)
    with pytest.raises(HTTPException) as excinfo:
        await check(user=_user(role="recruiter", active=False))
    assert excinfo.value.status_code == 403


@pytest.mark.asyncio
async def test_require_session_role_passes_an_active_recruiter_session() -> None:
    """Positive control — proves the deactivation check is not a blanket
    403 for every session."""
    check = require_session_role(Role.ADMIN, Role.RECRUITER)
    await check(user=_user(role="recruiter", active=True))  # must not raise


# ── require_role_assigned ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_require_role_assigned_403s_a_deactivated_session_with_an_assigned_role() -> (  # noqa: E501
    None
):
    with pytest.raises(HTTPException) as excinfo:
        await require_role_assigned(user=_user(role="admin", active=False))
    assert excinfo.value.status_code == 403


@pytest.mark.asyncio
async def test_require_role_assigned_passes_an_active_session_with_an_assigned_role() -> (  # noqa: E501
    None
):
    await require_role_assigned(user=_user(role="admin", active=True))  # must not raise


# ── src.api.routes.users._require_admin_session ─────────────────────────


@pytest.mark.asyncio
async def test_require_admin_session_403s_a_deactivated_admin_session() -> None:
    with pytest.raises(HTTPException) as excinfo:
        await _require_admin_session(user=_user(role="admin", active=False))
    assert excinfo.value.status_code == 403


@pytest.mark.asyncio
async def test_require_admin_session_passes_an_active_admin_session() -> None:
    result = await _require_admin_session(user=_user(role="admin", active=True))
    assert result.active is True


# ── scoped_user_id_or_403 ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_scoped_user_id_or_403_403s_a_deactivated_hiring_manager_session() -> (
    None
):
    """A deactivated hiring_manager must not retain scoped access — and
    critically must NOT silently fall through to the ``None`` (UNSCOPED /
    company-wide) branch either, which would be worse than the status quo.
    Fail-closed: 403, the same mechanism as "no verifiable session"."""
    deactivated_hm = _user(role="hiring_manager", active=False)
    with pytest.raises(HTTPException) as excinfo:
        await scoped_user_id_or_403(deactivated_hm, Role.HIRING_MANAGER)
    assert excinfo.value.status_code == 403


@pytest.mark.asyncio
async def test_scoped_user_id_or_403_scopes_an_active_hiring_manager_session() -> None:
    active_hm = _user(role="hiring_manager", active=True)
    result = await scoped_user_id_or_403(active_hm, Role.HIRING_MANAGER)
    assert result == active_hm.id


# ── mechanism / message pins (mirror the existing gates' own discipline) ──


@pytest.mark.asyncio
async def test_require_session_role_403_on_deactivated_has_a_non_empty_detail() -> None:
    check = require_session_role(Role.ADMIN, Role.RECRUITER)
    with pytest.raises(HTTPException) as excinfo:
        await check(user=_user(role="recruiter", active=False))
    assert excinfo.value.detail


@pytest.mark.asyncio
async def test_require_role_assigned_403_on_deactivated_has_a_non_empty_detail() -> (
    None
):
    with pytest.raises(HTTPException) as excinfo:
        await require_role_assigned(user=_user(role="admin", active=False))
    assert excinfo.value.detail


__all__: list[str] = []
