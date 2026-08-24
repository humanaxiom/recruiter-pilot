"""GUARD test — pins the REAL ``require_role_assigned`` router-level wiring
on ``src.api.main.app`` itself (reviewer finding, mutation-confirmed).

**The finding.** ``tests/integration/test_no_role_fail_closed_pg.py``'s own
``_build_app`` helper rebuilds every ``app.include_router(...)`` call in a
private, hand-copied function instead of importing ``src.api.main.app`` —
so it proves a duplicate wiring, never the real one. A mutation that
REMOVED ``dependencies=[Depends(require_role_assigned)]`` from the
``resumes`` ``app.include_router(...)`` call in ``main.py`` SURVIVED the
full suite (confirmed by mutation testing), because nothing anywhere
imported the real ``app`` and inspected it. This module closes that gap by
inspecting ``src.api.main.app`` directly.

**How the check works, concretely (pinned to fastapi==0.139.2's real
shape — verified by dumping the live object graph, not guessed).** This
installed version's ``FastAPI.include_router`` does NOT copy an
include-time ``dependencies=[...]`` down into each child route's own
``APIRoute.dependencies``/``Dependant`` tree — every business route's own
``.dependencies`` list only ever holds its PER-ROUTE decorator dependency
(e.g. ``require_role(...)``'s closure), never ``require_role_assigned``.
Instead, each ``app.include_router(router, dependencies=[...])`` call parks
a ``_IncludedRouter`` wrapper object directly on ``app.routes``, and the
include-time dependency list lives on
``wrapper.include_context.dependencies`` — ONE list, shared by every route
``wrapper.original_router`` mounts. That is exactly the shape ``main.py``
writes (one ``dependencies=[Depends(require_role_assigned)]`` per
``include_router`` call, not one per route), so asserting against
``include_context.dependencies`` pins the mechanism this codebase actually
uses, on the version it actually pins.
"""

from __future__ import annotations

from typing import Any

import pytest

from src.api.deps import require_role_assigned
from src.api.main import app

_BUSINESS_PATH_PREFIXES: tuple[str, ...] = (
    "/jobs",
    "/resumes",
    "/shortlist",
    "/audit",
)
_BUSINESS_EXACT_PATHS: tuple[str, ...] = ("/my/jobs",)
_UNGATED_PREFIX = "/auth/cas"

_KNOWN_BUSINESS_PATHS: tuple[str, ...] = (
    "/jobs",
    "/jobs/jd-extract",
    "/jobs/bulk",
    "/jobs/{job_id}",
    "/jobs/{job_id}/status",
    "/jobs/{job_id}/resumes",
    "/resumes/{resume_id}",
    "/resumes/{resume_id}/reveal",
    "/resumes/{resume_id}/match-jobs",
    "/resumes/{resume_id}/match-results",
    "/jobs/{job_id}/shortlist",
    "/jobs/{job_id}/shortlist/export",
    "/shortlist/{entry_id}",
    "/audit/reveals-legacy",
    "/jobs/{job_id}/assignees",
    "/jobs/{job_id}/assignees/{user_id}",
    "/my/jobs",
)

_KNOWN_AUTH_PATHS: tuple[str, ...] = (
    "/auth/cas/login",
    "/auth/cas/validate",
    "/auth/cas/logout",
    "/auth/cas/user",
)


def _is_business_path(path: str) -> bool:
    if path in _BUSINESS_EXACT_PATHS:
        return True
    return any(path.startswith(prefix) for prefix in _BUSINESS_PATH_PREFIXES)


def _router_paths(router: object) -> set[str]:
    """All path strings mounted (possibly nested) on a plain ``APIRouter``.

    Mirrors ``test_api.py``'s ``_all_route_paths`` recursion for the rare
    case of a router-of-routers — every router in this codebase is flat,
    but this stays robust if that ever changes.
    """
    paths: set[str] = set()
    for route in getattr(router, "routes", ()):  # type: ignore[attr-defined]
        nested = getattr(route, "original_router", None)
        if nested is not None:
            paths |= _router_paths(nested)
            continue
        path = getattr(route, "path", None)
        if path is not None:
            paths.add(path)
    return paths


def _included_router_wrappers(routes: object) -> list[Any]:
    """The ``_IncludedRouter`` wrapper objects ``app.include_router(...)``
    parks directly on ``app.routes`` under fastapi==0.139.2 — each one
    corresponds to exactly one ``app.include_router(<router>, ...)`` call
    site in ``src.api.main``. A plain route decorated directly on ``app``
    (``/health``) has no ``include_context`` and is excluded here.
    """
    return [r for r in routes if hasattr(r, "include_context")]  # type: ignore[attr-defined]


def _include_time_dependency_callables(wrapper: Any) -> list[object]:
    """The callables passed as ``dependencies=[Depends(...)]`` to the
    ``include_router(...)`` call this wrapper represents."""
    deps = getattr(wrapper.include_context, "dependencies", None) or []
    return [getattr(d, "dependency", None) for d in deps]


def _wrapper_paths(wrapper: Any) -> set[str]:
    return _router_paths(wrapper.original_router)


def _gated_and_all_paths() -> tuple[set[str], set[str]]:
    """Returns ``(gated_paths, all_paths)`` across every
    ``_IncludedRouter`` wrapper on the real ``src.api.main.app`` — computed
    fresh per call so no test can leak state into another."""
    wrappers = _included_router_wrappers(app.routes)
    assert wrappers, "expected app.routes to contain include_router wrappers"

    gated_paths: set[str] = set()
    all_paths: set[str] = set()
    for wrapper in wrappers:
        paths = _wrapper_paths(wrapper)
        all_paths |= paths
        if require_role_assigned in _include_time_dependency_callables(wrapper):
            gated_paths |= paths
    return gated_paths, all_paths


# ── the guard ────────────────────────────────────────────────────────────


def test_every_business_route_is_behind_a_require_role_assigned_router() -> None:
    """THE guard. For every route path that belongs to one of the five
    gated business routers (jobs/resumes/shortlist/job_assignees/audit),
    the ``_IncludedRouter`` wrapper mounting it on the REAL
    ``src.api.main.app`` must carry ``require_role_assigned`` in its
    include-time dependency list.

    Removing ``dependencies=[Depends(require_role_assigned)]`` from ANY ONE
    of the five ``app.include_router(...)`` calls in ``main.py`` makes this
    fail: every path that router mounts drops out of ``gated_paths``, so it
    is no longer a superset of ``business_paths`` and ``missing`` is
    non-empty — this is the exact mutation the reviewer found surviving.
    """
    gated_paths, all_paths = _gated_and_all_paths()

    business_paths = {p for p in all_paths if _is_business_path(p)}
    assert business_paths, "expected at least one business route path on app"

    missing = business_paths - gated_paths
    assert not missing, (
        "these business route paths are NOT behind a require_role_assigned "
        f"include_router(...) call: {sorted(missing)}"
    )


@pytest.mark.parametrize("path", _KNOWN_BUSINESS_PATHS)
def test_each_known_business_route_individually_is_gated(path: str) -> None:
    """Same guard as above, pinned to each individual known route path (the
    exact positive set ``test_api.py``'s ``_PHASE_6_ROUTES`` already locks)
    so a failure names the precise missing route rather than only
    'some business path is ungated somewhere'."""
    gated_paths, all_paths = _gated_and_all_paths()

    assert path in all_paths, f"{path!r} must be a mounted route on the real app"
    assert path in gated_paths, (
        f"{path!r} must be mounted behind an include_router(...) call "
        "carrying dependencies=[Depends(require_role_assigned)]"
    )


# ── the boundary — /auth/cas stays reachable to a no-role/no-session user ──


def test_auth_cas_router_is_not_behind_require_role_assigned() -> None:
    """The auth router is deliberately UNGATED — a no-role (or no-session)
    caller must still reach login/validate/logout/user, or they could never
    discover why every business route now 403s them."""
    wrappers = _included_router_wrappers(app.routes)
    auth_wrappers = [
        w
        for w in wrappers
        if any(p.startswith(_UNGATED_PREFIX) for p in _wrapper_paths(w))
    ]
    assert auth_wrappers, "expected an /auth/cas include_router wrapper"

    for wrapper in auth_wrappers:
        callables = _include_time_dependency_callables(wrapper)
        assert require_role_assigned not in callables, (
            "the /auth/cas router must never carry require_role_assigned — "
            "a no-role session needs to reach it to see its own status"
        )


@pytest.mark.parametrize("path", _KNOWN_AUTH_PATHS)
def test_each_auth_cas_route_is_reachable_and_ungated(path: str) -> None:
    gated_paths, all_paths = _gated_and_all_paths()

    assert path in all_paths, f"{path!r} must be a mounted route on the real app"
    assert (
        path not in gated_paths
    ), f"{path!r} must not be gated by require_role_assigned"


def test_health_route_is_not_mounted_via_any_include_router_wrapper() -> None:
    """Sanity boundary: ``/health`` is a plain ``@app.get`` route, mounted
    directly on ``app`` (no ``include_router`` call at all), so it must
    never appear inside any wrapper's path set (business or auth), and must
    be reachable outside the wrapper set entirely."""
    wrappers = _included_router_wrappers(app.routes)
    for wrapper in wrappers:
        assert "/health" not in _wrapper_paths(wrapper)

    direct_paths = {
        getattr(r, "path", None)
        for r in app.routes
        if not hasattr(r, "include_context")
    }
    assert "/health" in direct_paths
