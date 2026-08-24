"""RED pin — Phase 1.3 (ROADMAP A1 step (iv)), the per-route half.

``test_frontend_csrf_covers_every_write_route.py`` proves the NEGATIVE across
the whole url_map: no state-changing route accepts an unaccompanied POST. That
test alone is satisfiable by a hook that rejects *everything*, which would be a
perfectly secure and completely broken application. This file pins the other
half of the contract — **a legitimate request still works** — and pins the
mechanism each route uses, so the two are not confused later.

**Two mechanisms, deliberately kept distinct.**

1. **The nine newly-covered routes** use a session-wide *page token*
   (``csrf.issue_page_token``), the classic synchronizer-token pattern. It is
   NOT one-shot: a page token that burned on use would break the back button,
   a second tab, and every ordinary "fix the validation error and resubmit"
   flow — all of which post the same form twice from one render.

2. **The three FU-4/D4 routes** (``resume_reveal``, ``resume_withdraw``,
   ``resume_reinstate``) keep their existing per-résumé, per-action ONE-SHOT
   token, which is strictly stronger: it is scoped to a single resource and a
   single action, and it burns on use to defeat replay. They are exempt from
   the hook rather than protected twice — stacking a weaker control on top of
   a stronger one would mean rendering two tokens into one form for no gain.
   This mirrors ADR-033's exemption discipline (``PATCH /users/{id}/role`` is
   exempt from ``require_session_role`` because ``_require_admin_session`` is
   already narrower) and, like it, the exemption is **visible and asserted**
   below rather than left implicit.

**Why the exemption set is itself under test.** An exemption list is the
natural place for this control to rot: adding an endpoint to it is a one-line
change that silently disables the guard. ``test_the_hook_exemption_set_is_
exactly_the_three_one_shot_routes`` fails if anything is ever added, so
widening it requires a deliberate edit to a test that explains why the current
three are there.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from frontend import csrf
from frontend.app import app

#: (endpoint, url-builder) for each route the new hook must protect. Kept as an
#: explicit list — unlike the url_map enumeration in the sibling structural
#: file, these need per-route request bodies, and naming them makes the
#: positive-path assertions readable when one fails.
_HOOK_PROTECTED_ROUTES: tuple[tuple[str, str], ...] = (
    ("jd_extract", "/jobs/jd-extract"),
    ("create_job", "/jobs"),
    ("bulk_create_jobs", "/jobs/bulk"),
    ("upload_resumes", "/jobs/{job}/resumes"),
    ("transition_status", "/jobs/{job}/status"),
    ("blind_review", "/jobs/{job}/blind-review"),
    ("generate_shortlist", "/jobs/{job}/shortlist"),
    ("resume_match_jobs", "/resumes/{resume}/match-jobs"),
    ("admin_set_user_role", "/admin/users/{user}/role"),
)


@pytest.fixture
def client() -> Any:
    app.config.update(TESTING=True)
    return app.test_client()


def _url(template: str) -> str:
    return template.format(job=uuid4(), resume=uuid4(), user=uuid4())


def _page_token(client: Any) -> str:
    """Mint a page token into ``client``'s session, the way a rendered page
    would, and return it for submission."""
    with client.session_transaction() as session:
        with app.test_request_context():
            import flask

            flask.session.update(session)
            token = csrf.issue_page_token()
            session.update(flask.session)
    return token


@pytest.mark.parametrize(("endpoint", "template"), _HOOK_PROTECTED_ROUTES)
def test_hook_protected_route_rejects_a_missing_token(
    client: Any, endpoint: str, template: str
) -> None:
    """No token at all — the forged-request case."""
    with patch("frontend.app.api_client") as mock_api:
        mock_api.BackendUnavailable = Exception
        mock_api.NotFound = Exception
        mock_api.BadRequest = Exception
        resp = client.post(_url(template))
    assert resp.status_code == 403, (
        f"{endpoint} accepted a POST with no CSRF token "
        f"({resp.status_code}) — this is the Phase 1.3 defect"
    )
    assert not mock_api.method_calls, (
        f"{endpoint} reached the backend before rejecting the request — a "
        "forgery must be stopped before it can cause the effect it intends"
    )


@pytest.mark.parametrize(("endpoint", "template"), _HOOK_PROTECTED_ROUTES)
def test_hook_protected_route_rejects_a_wrong_token(
    client: Any, endpoint: str, template: str
) -> None:
    """A token that is present but not this session's — a guessed or stolen
    value, and the case a naive "is the field non-empty" check would pass."""
    _page_token(client)
    with patch("frontend.app.api_client") as mock_api:
        mock_api.BackendUnavailable = Exception
        mock_api.NotFound = Exception
        mock_api.BadRequest = Exception
        resp = client.post(
            _url(template), data={csrf.FORM_FIELD: "not-the-right-token"}
        )
    assert resp.status_code == 403, (
        f"{endpoint} accepted a POST carrying a WRONG CSRF token "
        f"({resp.status_code})"
    )
    assert not mock_api.method_calls, f"{endpoint} reached the backend anyway"


@pytest.mark.parametrize(("endpoint", "template"), _HOOK_PROTECTED_ROUTES)
def test_hook_protected_route_accepts_a_valid_form_token(
    client: Any, endpoint: str, template: str
) -> None:
    """The positive path: with this session's page token in the form body the
    request passes the guard and reaches the view.

    Asserts only that the guard did NOT reject it — each route's own behaviour
    beyond that point is covered by its existing tests, and re-asserting it
    here would couple this file to nine unrelated response shapes.
    """
    token = _page_token(client)
    with patch("frontend.app.api_client"):
        try:
            resp = client.post(_url(template), data={csrf.FORM_FIELD: token})
        except Exception:  # noqa: BLE001 - a view-level error is not a 403
            return
    assert resp.status_code != 403, (
        f"{endpoint} REJECTED a request carrying this session's valid page "
        "token — the guard is over-tight and has broken the real workflow"
    )


@pytest.mark.parametrize(("endpoint", "template"), _HOOK_PROTECTED_ROUTES)
def test_hook_protected_route_accepts_a_valid_header_token(
    client: Any, endpoint: str, template: str
) -> None:
    """htmx posts carry the token as a header, not a form field.

    Several of these controls are ``hx-post`` buttons with no surrounding
    form to put a hidden input in (``generate_shortlist``, ``jd_extract``), so
    a form-field-only reader would leave exactly those unprotected — or, worse,
    protected and broken. The header name is set once on ``<body>`` via
    ``hx-headers`` and inherited by every htmx request on the page, including
    ones inside swapped-in partials.
    """
    token = _page_token(client)
    with patch("frontend.app.api_client"):
        try:
            resp = client.post(_url(template), headers={csrf.HEADER_FIELD: token})
        except Exception:  # noqa: BLE001 - a view-level error is not a 403
            return
    assert resp.status_code != 403, (
        f"{endpoint} REJECTED a request carrying this session's valid page "
        f"token in the {csrf.HEADER_FIELD} header"
    )


def test_the_hook_exemption_set_is_exactly_the_three_one_shot_routes() -> None:
    """The exemption list is the natural place for this control to rot.

    Adding an endpoint here is a one-line change that silently disables the
    guard for it, so widening the set must require deliberately editing this
    test — and the three that ARE exempt carry a strictly STRONGER control
    (per-résumé, per-action, one-shot), which is why they are exempt rather
    than double-guarded.
    """
    from frontend import app as app_module

    assert app_module._CSRF_HOOK_EXEMPT_ENDPOINTS == frozenset(
        {"resume_reveal", "resume_withdraw", "resume_reinstate"}
    ), (
        "the CSRF hook's exemption set changed. Every exemption is a route "
        "the hook does NOT protect: justify it here, or remove it."
    )


@pytest.mark.parametrize(
    "endpoint", ["resume_reveal", "resume_withdraw", "resume_reinstate"]
)
def test_exempt_routes_still_reject_a_page_token(client: Any, endpoint: str) -> None:
    """The exemption must not become a downgrade.

    These three are skipped by the hook because their own one-shot token is
    stronger. If the hook's weaker page token were ALSO accepted by them, the
    exemption would have quietly replaced a per-résumé one-shot control with a
    session-wide reusable one — a downgrade wearing the costume of an
    exemption. A page token must not open a reveal.
    """
    resume_id = uuid4()
    token = _page_token(client)
    url = f"/resumes/{resume_id}/{endpoint.removeprefix('resume_')}"
    with patch("frontend.app.api_client") as mock_api:
        mock_api.BackendUnavailable = Exception
        mock_api.NotFound = Exception
        mock_api.BadRequest = Exception
        mock_api.reveal_resume = MagicMock()
        resp = client.post(url, data={csrf.FORM_FIELD: token})
    assert resp.status_code == 403, (
        f"{endpoint} accepted the session-wide PAGE token in place of its own "
        f"one-shot per-résumé token ({resp.status_code}) — the hook exemption "
        "has downgraded this route's protection"
    )
