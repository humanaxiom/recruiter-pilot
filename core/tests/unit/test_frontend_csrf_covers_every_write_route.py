"""Structural RED pin — Phase 1.3 (ROADMAP A1 step (iv)), CSRF on every
browser state-changing route. THE most important test in this slice.

**The gap this closes.** FU-4/D4 built a genuine anti-forgery control
(``frontend.csrf``) and wired it to exactly THREE routes — ``resume_reveal``,
``resume_withdraw``, ``resume_reinstate``. The Flask viewer has **twelve**
POST routes. The other nine were never guarded, including the two that matter
most:

* ``POST /admin/users/<user_id>/role`` — **privilege escalation.** A forged
  cross-site auto-submit from an admin's logged-in browser promotes an
  attacker-controlled account to ``admin``.
* ``POST /jobs/<job_id>/blind-review`` — **the exact flip the
  fix/auth-boundary-fails-open exploit used** to un-blind a candidate pool
  (ADR-034). That finding closed the *unauthenticated* path to it; a forged
  request rides a real recruiter's session instead, so it is a different door
  to the same room.

The Flask hop is the vulnerable one for the same reason FU-4/D4 already
documented: the browser supplies no credential of its own, and Flask attaches
its own server-held API key on the OUTBOUND leg
(``api_client.build_client``), so Flask cannot distinguish a forged
cross-site submit from a genuine click. **ADR-034 made this strictly more
important, not less**: every backend write now requires a real CAS session,
so the session cookie riding along on a forged request is exactly what makes
the forgery succeed.

**Why this test is behavioural, not introspective.** ``test_write_route_
session_gate.py`` — the sibling structural guard on the FastAPI side — walks
the route table and recognises a gate by its ``__qualname__``. That works
there because the gate is a per-route dependency object. It would be the
WRONG check here: the enforcement this slice adds is a single
``before_request`` hook, and a hook that is registered but inert would pass
any introspective check while protecting nothing. That is precisely the
ROADMAP A7 defect shape — an invariant with nothing enforcing it — and this
file is written to be un-foolable by it.

So this test **drives a real forged request at every POST route the app
actually exposes** and asserts it is rejected. A future route added without
protection fails HERE, by URL-map enumeration, rather than in production
against a real browser session. There is deliberately **no allow-list of
routes to check**: the list comes from ``app.url_map``, so it cannot drift
out of date the way a hand-maintained one silently does.

**No exemption list, either.** All twelve routes must reject an
unaccompanied POST — the nine via the new hook, the three FU-4/D4 routes via
their own strictly-stronger per-résumé one-shot token, which already 403s a
missing token today. The two mechanisms are distinguished in
``test_frontend_csrf_write_route_enforcement.py``; here they are simply
required to produce the same observable outcome.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest

from frontend.app import app

_STATE_CHANGING_METHODS = frozenset({"POST", "PATCH", "PUT", "DELETE"})

#: Every POST route is expected to exist; this is a floor, not the list under
#: test. If the app ever drops below it, the enumeration below has silently
#: stopped covering things and this catches that rather than passing vacuously
#: on an empty set.
_MINIMUM_EXPECTED_WRITE_ROUTES = 12


@pytest.fixture
def client() -> Any:
    app.config.update(TESTING=True)
    return app.test_client()


def _write_rules() -> list[Any]:
    """Every state-changing rule the REAL app exposes, from its own url_map."""
    return [
        rule
        for rule in app.url_map.iter_rules()
        if rule.methods is not None
        and (rule.methods & _STATE_CHANGING_METHODS)
        and rule.endpoint != "static"
    ]


def _concrete_url(rule: Any) -> str:
    """Build a real URL for ``rule``, substituting a dummy UUID per argument.

    Every dynamic segment in this app is a ``<uuid:...>`` converter, so one
    substitution strategy covers the whole map. Built through the rule itself
    rather than string-formatted, so a future converter change surfaces as a
    build error here instead of a silently malformed URL that 404s and makes
    this test pass for the wrong reason.
    """
    values = {arg: uuid4() for arg in rule.arguments}
    with app.test_request_context():
        return str(app.url_map.bind("localhost").build(rule.endpoint, values))


def test_every_state_changing_route_rejects_a_request_with_no_csrf_token(
    client: Any,
) -> None:
    """THE pin. A forged cross-site POST carries no anti-forgery token, so
    every state-changing route must reject it — and must do so BEFORE any
    call reaches the backend, since a rejected forgery that still hit the
    backend would have already caused the effect it was meant to prevent.
    """
    rules = _write_rules()
    assert len(rules) >= _MINIMUM_EXPECTED_WRITE_ROUTES, (
        f"only found {len(rules)} state-changing routes, expected at least "
        f"{_MINIMUM_EXPECTED_WRITE_ROUTES} — the url_map enumeration has "
        "stopped seeing routes, so this test is no longer covering anything"
    )

    unprotected: list[str] = []
    leaked_to_backend: list[str] = []

    for rule in rules:
        url = _concrete_url(rule)
        # Patch the whole api_client module surface: if the guard works, NOTHING
        # here is ever called. Any call at all means the request reached
        # business logic before being rejected.
        with patch("frontend.app.api_client") as mock_api:
            mock_api.BackendUnavailable = Exception
            mock_api.NotFound = Exception
            mock_api.BadRequest = Exception
            try:
                resp = client.post(url)
            except Exception as exc:  # noqa: BLE001 - reported, not swallowed
                unprotected.append(
                    f"{rule.endpoint} ({url}) raised {type(exc).__name__} "
                    "instead of rejecting the forgery"
                )
                continue
            if resp.status_code != 403:
                unprotected.append(
                    f"{rule.endpoint} ({url}) returned {resp.status_code}, "
                    "expected 403"
                )
            if mock_api.method_calls:
                leaked_to_backend.append(
                    f"{rule.endpoint} called {mock_api.method_calls[0][0]} "
                    "before rejecting the request"
                )

    assert not unprotected, (
        "state-changing routes accepted a POST carrying no CSRF token:\n  "
        + "\n  ".join(unprotected)
    )
    assert not leaked_to_backend, (
        "routes reached the backend before rejecting an unaccompanied POST:\n  "
        + "\n  ".join(leaked_to_backend)
    )


def test_every_state_changing_route_rejects_a_cross_origin_post(
    client: Any,
) -> None:
    """Defense in depth, evaluated independently of the token: a POST
    declaring a foreign ``Origin`` is rejected on every state-changing route.

    Layered ON TOP of the token, never instead of it — ``frontend.csrf
    .same_origin``'s own docstring is explicit that an ABSENT origin header is
    not a block, which is why the token test above is the primary pin.
    """
    offenders: list[str] = []
    for rule in _write_rules():
        url = _concrete_url(rule)
        with patch("frontend.app.api_client") as mock_api:
            mock_api.BackendUnavailable = Exception
            mock_api.NotFound = Exception
            mock_api.BadRequest = Exception
            try:
                resp = client.post(url, headers={"Origin": "https://evil.test"})
            except Exception as exc:  # noqa: BLE001 - reported, not swallowed
                offenders.append(
                    f"{rule.endpoint} raised {type(exc).__name__} instead of 403"
                )
                continue
            if resp.status_code != 403:
                offenders.append(f"{rule.endpoint} returned {resp.status_code}")

    assert (
        not offenders
    ), "state-changing routes accepted a cross-origin POST:\n  " + "\n  ".join(
        offenders
    )
