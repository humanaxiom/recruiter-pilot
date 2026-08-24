"""Structural RED pin — Phase 1.1 of the pilot-readiness plan
(fix/session-role-on-writes). THE most important test in this slice.

Every per-route negative test in the sibling ``test_route_*_session_gate.py``
files proves the defect one route at a time, against a *fresh* ``FastAPI()``
carrying only that one router. This file proves it differently: it imports
the REAL, fully-wired ``src.api.main.app`` and introspects its actual route
table, so a FUTURE write route added to this API without a session-role gate
fails HERE — at test-collection/introspection time — rather than only in
production against a real browser session.

**How the check works, concretely (pinned to fastapi==0.139.2's real
shape — verified by dumping the live object graph, not guessed, mirroring
the discipline in ``test_router_role_gate.py``'s own module docstring).**
A route's OWN per-route decorator dependency (``dependencies=[Depends(...)]``
on the ``@router.post(...)``/etc. call, e.g. every ``require_role(...)``
closure in this codebase) DOES appear in that route's ``APIRoute.dependant
.dependencies`` list — unlike an ``include_router(router, dependencies=
[...])`` include-time dependency, which does NOT propagate down into it (see
``test_router_role_gate.py`` for that distinct, router-level mechanism).
Each ``Dependant`` node's ``.dependencies`` list holds further nested
``Dependant`` nodes for ITS OWN sub-dependencies (e.g. ``require_role(...)``'s
inner closure itself depends on ``resolve_role`` via a parameter-level
``Depends()``), so a full recursive walk of ``route.dependant.dependencies``
surfaces every dependency callable reachable from a route, at any depth.

**Why this cannot check object identity against ``src.api.deps
.require_session_role`` directly.** Like the pre-existing ``require_role``,
the target dependency (``require_session_role(*allowed)``, per the task) is
a FACTORY — every ``Depends(require_session_role(...))`` call site produces
a DISTINCT closure object, even across two routes with an identical allowed-
role tuple (the exact reason ``test_api_deps.py`` documents for why route
tests override the SHARED ``resolve_role``, never a ``require_role(...)``
closure, by identity). No single object could ever match every route's own
closure. Instead this file recognises the closure by its ``__qualname__`` —
``require_session_role.<locals>._check`` (or any inner name), mirroring
``require_role``'s own ``require_role.<locals>._check`` shape exactly
(confirmed directly against a live fastapi==0.139.2 process: a factory
``def require_role(*allowed): async def _check(...): ...; return _check``
produces a closure whose ``__qualname__`` is ``require_role.<locals>._check``
— substring-matching ``"require_session_role"`` against ``__qualname__`` is
therefore satisfied by ANY inner name the coder chooses, and is not an
import of a symbol that does not exist yet: this file never imports
``require_session_role`` at all, so a missing implementation cannot produce
a collection/import error — only a clean, named assertion failure below.

**The exemption list — visible, not implied.** Exactly ONE write route in
this API is deliberately NOT required to carry this gate:

* ``PATCH /users/{user_id}/role`` — already correct via
  ``src.api.routes.users._require_admin_session``, which gates on the CAS
  session role directly (``user is not None and user.role == "admin"``) —
  strictly narrower than any ``require_session_role(...)`` set this slice
  would otherwise add, so stacking the two would be redundant (see that
  module's own docstring).

``/auth/cas/*`` needs NO exemption entry here: every route under it is a
GET (session lifecycle — login/validate/logout/user), so none of them are
write routes in the first place and none are ever selected by the
POST/PATCH/PUT/DELETE filter below. Documented anyway, rather than left
implied, per the task: a no-role or no-session caller must still be able to
reach it to discover why every business route now 403s them, and it must
never gain a session-role gate.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from src.api.main import app

_WRITE_METHODS = frozenset({"POST", "PATCH", "PUT", "DELETE"})

# Every write route this slice pins as GATED, i.e. it MUST carry a
# require_session_role(...)-produced dependency once the fix lands.
_KNOWN_GATED_WRITE_ROUTES: tuple[tuple[str, str], ...] = (
    ("POST", "/jobs"),
    ("POST", "/jobs/jd-extract"),
    ("POST", "/jobs/bulk"),
    ("PATCH", "/jobs/{job_id}"),
    ("PATCH", "/jobs/{job_id}/status"),
    ("POST", "/jobs/{job_id}/reparse"),
    ("POST", "/jobs/{job_id}/resumes"),
    ("POST", "/resumes/{resume_id}/reveal"),
    ("POST", "/resumes/{resume_id}/withdraw"),
    ("POST", "/resumes/{resume_id}/reinstate"),
    ("POST", "/resumes/{resume_id}/match-jobs"),
    ("POST", "/jobs/{job_id}/shortlist"),
    ("POST", "/jobs/{job_id}/assignees"),
    ("DELETE", "/jobs/{job_id}/assignees/{user_id}"),
    # D1 = option C — the audited reveal of a withheld audit_log detail. A
    # write METHOD performing an audited READ (same shape as POST
    # /resumes/{id}/reveal above), so it belongs in this registry for exactly
    # the reason the registry exists: it must carry a session-role gate, and
    # for this route the attributable session IS the feature.
    ("POST", "/audit/log/{audit_id}/reveal"),
)

# The one write route explicitly exempted — see the module docstring for why.
_EXEMPT_WRITE_ROUTES: dict[tuple[str, str], str] = {
    ("PATCH", "/users/{user_id}/role"): (
        "already correct via src.api.routes.users._require_admin_session — "
        "gates the CAS session role directly, strictly narrower than any "
        "require_session_role(...) set this slice would add."
    ),
}


def _iter_dependency_callables(dependant: Any) -> Iterator[Any]:
    """Recursively yield every ``.call`` in a route's FULL dependant tree —
    both its own route-decorator ``dependencies=[Depends(...)]`` entries and
    every parameter-level ``Depends()`` (and their own sub-dependencies, at
    any depth). See the module docstring for how this shape was confirmed
    against a live fastapi==0.139.2 process."""
    for sub in getattr(dependant, "dependencies", None) or []:
        yield sub.call
        yield from _iter_dependency_callables(sub)


def _all_api_routes() -> list[Any]:
    """Every real ``APIRoute`` mounted (possibly nested) on the REAL
    ``src.api.main.app`` — recurses through ``_IncludedRouter`` wrapper
    objects (mirroring ``test_router_role_gate.py``'s own
    ``_router_paths``), and excludes the framework's own ``/docs``/
    ``/openapi.json``/``/redoc`` routes, which are plain Starlette ``Route``
    objects with no ``.dependant``."""
    routes: list[Any] = []

    def _walk(rs: Any) -> None:
        for r in rs:
            nested = getattr(r, "original_router", None)
            if nested is not None:
                _walk(nested.routes)
                continue
            if hasattr(r, "methods") and hasattr(r, "dependant"):
                routes.append(r)

    _walk(app.routes)
    return routes


def _write_routes() -> list[Any]:
    return [
        r
        for r in _all_api_routes()
        if (getattr(r, "methods", None) or set()) & _WRITE_METHODS
    ]


def _route_key(route: Any) -> tuple[str, str]:
    methods = sorted((route.methods or set()) - {"HEAD", "OPTIONS"})
    assert len(methods) == 1, (
        f"unexpected multi-verb write route, this file's key scheme assumes "
        f"exactly one real HTTP verb per path: {route.path} {methods}"
    )
    return (methods[0], route.path)


def _has_session_role_gate(route: Any) -> bool:
    """True iff ANY callable in the route's dependant tree was produced by a
    ``require_session_role(*allowed)``-style factory — recognised by its
    closure's ``__qualname__`` containing ``"require_session_role"`` (see the
    module docstring for why an identity check cannot work here, and why
    this is not an import of a not-yet-existing symbol)."""
    for call in _iter_dependency_callables(route.dependant):
        qualname = getattr(call, "__qualname__", "")
        if "require_session_role" in qualname:
            return True
    return False


def _write_routes_by_key() -> dict[tuple[str, str], Any]:
    return {_route_key(r): r for r in _write_routes()}


# ── sanity precondition ──────────────────────────────────────────────────


def test_the_known_write_route_set_matches_the_real_apps_write_routes() -> None:
    """Precondition for every test below: the expected write-route set is
    exactly what ``src.api.main.app`` mounts today. Fails loudly (not
    vacuously) if a router is dropped from ``main.py``, a route is renamed,
    or a NEW write route is added without updating this file's known set —
    every case should be visible here, not silently skipped."""
    actual = set(_write_routes_by_key())
    expected = set(_KNOWN_GATED_WRITE_ROUTES) | set(_EXEMPT_WRITE_ROUTES)
    assert actual == expected, (
        f"missing from the known set: {expected - actual}; "
        f"unexpected/new write routes not yet classified: {actual - expected}"
    )


# ── the guard ────────────────────────────────────────────────────────────


def test_every_known_write_route_carries_a_session_role_gate() -> None:
    """THE guard. Fails TODAY for every route in ``_KNOWN_GATED_WRITE_ROUTES``
    — no ``require_session_role(...)``-produced dependency exists anywhere in
    this codebase yet, so a recruiter-key + hiring_manager/auditor-session
    caller can still reach every one of them (the exact defect this whole
    slice pins, reproduced structurally rather than one HTTP call at a
    time)."""
    routes_by_key = _write_routes_by_key()
    missing = [
        key
        for key in _KNOWN_GATED_WRITE_ROUTES
        if not _has_session_role_gate(routes_by_key[key])
    ]
    assert not missing, (
        "these write routes carry no require_session_role(...) dependency — "
        f"a recruiter-key + hiring_manager/auditor-session caller can still "
        f"reach them: {sorted(missing)}"
    )


@pytest.mark.parametrize("key", _KNOWN_GATED_WRITE_ROUTES)
def test_each_known_write_route_individually_carries_a_session_role_gate(
    key: tuple[str, str],
) -> None:
    """Same guard, pinned per-route so a failure names the exact missing
    route instead of only 'some write route somewhere' — mirrors
    ``test_router_role_gate.py``'s own per-route parametrized pin."""
    routes_by_key = _write_routes_by_key()
    assert (
        key in routes_by_key
    ), f"{key!r} must be a mounted write route on the real app"
    assert _has_session_role_gate(
        routes_by_key[key]
    ), f"{key!r} carries no require_session_role(...) dependency"


# ── the boundary — the one deliberate exemption stays visible ─────────────


def test_the_exempt_admin_role_route_is_not_required_to_carry_the_new_gate() -> None:
    """``PATCH /users/{user_id}/role`` is the one write route this slice does
    NOT require to carry ``require_session_role(...)`` — not a silent
    loophole: it is enumerated explicitly in ``_EXEMPT_WRITE_ROUTES`` (so
    removing the entry, rather than skipping the check, is what would make
    ``test_the_known_write_route_set_matches_the_real_apps_write_routes``
    fail), and it already gates on the CAS session role via
    ``_require_admin_session`` — see the module docstring."""
    routes_by_key = _write_routes_by_key()
    key = ("PATCH", "/users/{user_id}/role")
    assert key in routes_by_key, f"{key!r} must be a mounted route on the real app"
    assert key in _EXEMPT_WRITE_ROUTES


__all__: list[str] = []
