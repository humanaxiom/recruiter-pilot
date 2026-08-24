"""RED pin -- D2 = option B (``docs/OPEN_DECISIONS.md`` §D2, answered by
product 2026-08-19), closing the carried question in
``docs/adr/034-auth-boundary-fails-open.md``'s "Accepted residuals": *"Whether
machine readers are legitimate at all is a product question -- recorded
rather than silently answered."*

**The decision.** ``require_role_assigned`` (``core/src/api/deps.py``)
currently PASSES when ``user is None`` -- a caller holding a valid API key
with no CAS session gets unscoped READS across every business router
(``audit``/``job_assignees``/``jobs``/``resumes``/``shortlist``, wired at
``src.api.main``:87-92). Writes were already closed by ADR-034's
``require_session_role`` 403ing on ``user is None`` -- that asymmetry was
deliberate and undecided; it is now decided. Every read must also 403 on
``user is None``, so the boundary says one thing rather than two. The
sharpest instance: a bare key reading ``GET /audit/reveals-legacy`` --
an unattributable read of the audit log itself, the same shape as the
problem ADR-036 exists to fix.

This file pins the pure ``require_role_assigned`` LOGIC only (mocked
``User | None`` inputs, no I/O) -- mirroring ``test_api_deps.py``'s own
"offline genuinely suffices for the LOGIC" framing for this dependency. The
router-level WIRING interaction (a live request with a real key and no
session, through the real business routers) is proved separately, against
real Postgres, in ``tests/integration/test_close_unscoped_reads_pg.py`` --
including the CAS-off dev-boot sentinel path and the Flask-viewer
key+session pattern, both of which this unit file can only gesture at with
a mocked ``User`` and cannot prove untouched by this change.

Two of the three tests this decision reverses live IN PLACE, in their
original files, with a docstring explaining the reversal (not here, and not
deleted):

* ``test_api_deps.py::
  test_require_role_assigned_403s_a_bare_service_key_caller_with_no_session``
  (renamed from ``..._passes_..._with_no_session``)
* ``test_api_deps.py::
  test_require_role_assigned_composes_with_resolve_user_via_dependency_override``
  (same name; only its final ``None`` -> 200 block flipped to 403)
* ``../integration/test_no_role_fail_closed_pg.py::
  test_bare_recruiter_key_with_no_session_cookie_now_403s_get_jobs``
  (renamed from ``..._still_200s_get_jobs``)

This file adds the NEW coverage the decision calls for: a non-leaky detail
message on the new 403, and regression guards proving the other two
``require_role_assigned`` branches (``user.role is None``;
``user.active is False``) and the dev-anonymous CAS-off sentinel are
untouched by this change -- plus a static wiring guard that ``/users`` (its
own, stricter, session-only gate) is not accidentally pulled behind this one
by a future "fix" of the asymmetry the product has not been asked about.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import HTTPException

from src.schemas.auth import User


def _user(role: str | None, *, active: bool = True) -> User:
    """A real (non-sentinel) session ``User`` with the given ``role``."""
    now = datetime.now(UTC)
    return User(
        id=uuid4(),
        cas_username="jordan",
        display_name=None,
        email=None,
        role=role,
        active=active,
        created_at=now,
        last_seen_at=now,
    )


def _dev_anonymous_admin() -> User:
    """The exact synthetic identity ``resolve_user`` returns when
    ``cas_enabled=False`` -- see ``deps._DEV_ADMIN_SENTINEL_ID``. Mirrors
    ``test_api_deps.py``'s own helper of the same name exactly."""
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


# ── the new rule: user is None now 403s ─────────────────────────────────


@pytest.mark.asyncio
async def test_require_role_assigned_403s_when_user_is_none() -> None:
    """THE core D2 reversal: a bare service-key caller with no CAS session
    resolves ``user=None`` -- this must now 403, closing the last case
    ``require_role_assigned`` still let through unscoped."""
    from src.api.deps import require_role_assigned

    with pytest.raises(HTTPException) as excinfo:
        await require_role_assigned(user=None)
    assert excinfo.value.status_code == 403


@pytest.mark.asyncio
async def test_require_role_assigned_403_on_none_has_a_clear_non_empty_detail() -> None:
    from src.api.deps import require_role_assigned

    with pytest.raises(HTTPException) as excinfo:
        await require_role_assigned(user=None)
    assert isinstance(excinfo.value.detail, str)
    assert excinfo.value.detail.strip() != ""


@pytest.mark.asyncio
async def test_require_role_assigned_403_on_none_detail_does_not_leak_internals() -> (
    None
):
    """ "Non-leaky" (per the task): the 403 for an absent session must not
    echo back anything that would help an attacker distinguish "no key",
    "bad key", "expired session" or "revoked session" from one another, and
    must not surface any implementation detail (a stack trace, a SQL
    fragment, a cookie/header name, or a raw exception repr)."""
    from src.api.deps import require_role_assigned

    with pytest.raises(HTTPException) as excinfo:
        await require_role_assigned(user=None)
    detail = excinfo.value.detail.lower()
    for leaky in (
        "traceback",
        "sql",
        "select ",
        "ra_session",
        "cookie",
        "x-api-key",
        "exception",
        "nonetype",
    ):
        assert leaky not in detail, f"detail leaks {leaky!r}: {detail!r}"


@pytest.mark.asyncio
async def test_require_role_assigned_403_on_none_matches_the_403_mechanism_used_elsewhere() -> (  # noqa: E501
    None
):
    """Same plain ``fastapi.HTTPException(status_code=403, ...)`` mechanism
    as the existing ``user.role is None`` / ``user.active is False``
    branches -- not a bespoke ``AppError`` -- so the global exception
    machinery renders every 403 on this gate identically."""
    from src.api.deps import require_role_assigned

    with pytest.raises(HTTPException) as excinfo:
        await require_role_assigned(user=None)
    assert isinstance(excinfo.value, HTTPException)
    assert excinfo.value.status_code == 403


# ── regression: the two existing 403 branches must still behave as before ──
#
# Already pinned, unaffected by this change, in:
#   * test_api_deps.py::
#     test_require_role_assigned_403s_a_real_session_with_no_role
#   * test_api_deps_active_account_gates.py::
#     test_require_role_assigned_403s_a_deactivated_session_with_an_assigned_role
# Re-pinned here, self-contained, so this file alone documents every branch
# D2 touches and every branch it must not touch.


@pytest.mark.asyncio
async def test_require_role_assigned_still_403s_a_real_session_with_no_role() -> None:
    from src.api.deps import require_role_assigned

    with pytest.raises(HTTPException) as excinfo:
        await require_role_assigned(user=_user(role=None))
    assert excinfo.value.status_code == 403


@pytest.mark.asyncio
async def test_require_role_assigned_still_403s_a_deactivated_session() -> None:
    from src.api.deps import require_role_assigned

    with pytest.raises(HTTPException) as excinfo:
        await require_role_assigned(user=_user(role="admin", active=False))
    assert excinfo.value.status_code == 403


# ── regression: a resolved user with a real role still passes ──────────────


@pytest.mark.parametrize("role", ["admin", "recruiter", "hiring_manager", "auditor"])
@pytest.mark.asyncio
async def test_require_role_assigned_still_passes_every_real_assigned_role(
    role: str,
) -> None:
    from src.api.deps import require_role_assigned

    await require_role_assigned(user=_user(role=role))  # must not raise


# ── regression: the CAS-off dev-boot sentinel is untouched ─────────────────
#
# ``resolve_user`` returns the synthetic ``dev-anonymous`` admin sentinel
# BEFORE the ``if not ra_session: return None`` branch even runs -- so
# ``user`` is never ``None`` on that path, and this new ``user is None``
# rule structurally cannot apply to it. The load-bearing proof that a whole
# real request (no cookie, CAS disabled) still 200s is at the integration
# level: tests/integration/test_close_unscoped_reads_pg.py::
# test_cas_disabled_dev_boot_still_200s_get_jobs_with_no_session_cookie.
# This is only the pure-logic half.


@pytest.mark.asyncio
async def test_require_role_assigned_still_passes_the_dev_anonymous_sentinel() -> None:
    from src.api.deps import require_role_assigned

    await require_role_assigned(user=_dev_anonymous_admin())  # must not raise


# ── static wiring guard: /users must not be pulled behind this gate ────────


def test_users_router_is_not_wired_behind_require_role_assigned() -> None:
    """``/users`` is gated by its own, stricter, session-only
    ``_require_admin_session`` (``user is None or user.role != "admin" or
    not user.active`` -> 403) -- already 403ing ``user is None`` today, long
    before D2. ``main.py`` deliberately does NOT also wrap it in
    ``require_role_assigned``. This D2 change must not become the occasion
    to "simplify" that by folding ``/users`` into the router-level gate --
    that would be deciding a DIFFERENT, still-undecided question (whether a
    no-role *session* should ever reach ``/users``) as a side effect of
    this one. Mirrors ``test_router_role_gate.py``'s own
    ``_IncludedRouter``-wrapper inspection technique for fastapi==0.139.2.
    """
    from src.api.deps import require_role_assigned
    from src.api.main import app

    wrappers = [r for r in app.routes if hasattr(r, "include_context")]
    assert wrappers, "expected app.routes to contain include_router wrappers"

    users_wrappers = [
        w
        for w in wrappers
        if any(
            getattr(route, "path", None) == "/users"
            for route in getattr(w.original_router, "routes", ())
        )
    ]
    assert users_wrappers, "expected a /users include_router wrapper"

    for wrapper in users_wrappers:
        deps_list = getattr(wrapper.include_context, "dependencies", None) or []
        callables = [getattr(d, "dependency", None) for d in deps_list]
        assert require_role_assigned not in callables, (
            "/users must not be wired behind require_role_assigned -- it has "
            "its own stricter, session-only gate; D2 did not decide to "
            "change that"
        )


__all__: list[str] = []
