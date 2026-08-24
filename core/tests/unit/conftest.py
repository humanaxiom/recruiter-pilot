"""Unit-suite conftest — the CSRF-carrying Flask test client (Phase 1.3).

**Why this exists.** ROADMAP A1 step (iv) put an anti-forgery guard in front of
every state-changing Flask route (``frontend.app._csrf_gate``). Thirty-three
pre-existing tests across five files POST to those routes to exercise their
BUSINESS logic — payload shaping, error handling, redirects — and each of them
started 403ing the moment the guard landed, because a bare ``app.test_client()``
sends no token.

**Those tests were not wrong, and they are not weakened here.** Unlike the 13
tests ADR-034 had to rewrite — which actively *pinned* a fail-open as correct
behaviour — these simply predate the control. The honest fix is to make the
test client behave like the real browser it stands in for: hold a page token in
its session and present it on every request. That is what this fixture does. It
does **not** disable, patch, or bypass the guard: a request that omits the token
still 403s, which is what
``test_frontend_csrf_covers_every_write_route.py`` relies on (it builds its own
plain client precisely so it sees the guard).

**Deliberately not autouse.** An autouse token would silently satisfy the guard
for the whole suite, including the tests written to prove the guard works — the
exact way a control like this rots into decoration. Files opt in by name.

The token is written straight into the session rather than through
``csrf.issue_page_token`` because minting requires an active request context,
and the ceremony of borrowing one inside ``session_transaction`` obscures what
is a one-line piece of state. If the storage shape ever changes, every test
using this fixture 403s loudly rather than passing for the wrong reason.
"""

from __future__ import annotations

from typing import Any

import pytest

from frontend import csrf
from frontend.app import app

#: A fixed, obviously-test-only token. Fixed rather than random so a failure
#: message shows a recognisable value instead of noise.
TEST_PAGE_TOKEN = "test-only-page-token"


@pytest.fixture
def csrf_client() -> Any:
    """A Flask test client that carries a valid page token on every request.

    Sets the token in BOTH channels the guard accepts — the session (so the
    server has something to compare against) and the ``X-CSRF-Token`` request
    header via ``environ_base`` (so every request presents it, mirroring the
    ``hx-headers`` attribute on ``<body>`` that does this for real htmx
    traffic). Using the header rather than injecting a form field keeps the
    fixture from disturbing the request bodies these tests assert on.
    """
    app.config.update(TESTING=True)
    client = app.test_client()
    with client.session_transaction() as session:
        session[csrf.PAGE_SESSION_KEY] = TEST_PAGE_TOKEN
    client.environ_base["HTTP_X_CSRF_TOKEN"] = TEST_PAGE_TOKEN
    return client
