"""Phase 1.4 / ADR-036 — the auditor's access-record page in the Flask viewer.

**What this page is for.** The auditor role has existed since FU-5 but has
never had anywhere to go: the application had no read path to ``audit_log`` at
all, so producing the access record meant an engineer running SQL against
production by hand. That is what ROADMAP guardrail 2's "an auditor account
cannot do its job" meant, and this page is the thing that retires it.

**What is asserted here, and what deliberately is not.** The authorization
boundary is the BACKEND's ``require_session_role(ADMIN, AUDITOR)`` on
``GET /audit/log``, covered by ``test_route_audit_log.py``. The page gate tested
below is a compensating UX control — it exists so a recruiter gets a
comprehensible 403 rather than an empty page wrapped around a backend refusal.
Both are tested because both can fail, but only one of them is the boundary,
and the docstrings say which.
"""

from __future__ import annotations

import re
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from frontend import api_client
from frontend.app import app


@pytest.fixture
def client(csrf_client: Any) -> Any:
    return csrf_client


def _entry(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": str(uuid4()),
        "actor_kind": "user",
        "actor_user_id": str(uuid4()),
        "actor_username": "priya",
        "actor_service": None,
        "action": "reveal",
        "subject_type": "resume",
        "subject_id": str(uuid4()),
        "job_id": str(uuid4()),
        "context": "reviewing shortlist",
        "details": None,
        "occurred_at": "2026-08-13T10:00:00Z",
    }
    base.update(overrides)
    return base


def _cas(role: str | None) -> Any:
    return {"authenticated": True, "role": role, "username": "someone"}


def test_the_page_renders_the_record_with_the_actor_named(client: Any) -> None:
    """Attributable audit, rendered: an auditor sees WHO, not a UUID."""
    with patch.object(api_client, "list_audit_log", return_value=[_entry()]):
        resp = client.get("/audit")
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_data(as_text=True)
    assert "priya" in body
    assert "reveal" in body


def test_an_unattributable_service_event_is_named_not_left_blank(
    client: Any,
) -> None:
    """These are the rows an auditor is looking for — an
    ``actor_service='api'`` write is the signature of the ADR-034 exploit. A
    blank cell would read as missing data rather than as a finding.
    """
    entry = _entry(
        actor_kind="service",
        actor_user_id=None,
        actor_username=None,
        actor_service="api",
        action="blind_review_changed",
    )
    with patch.object(api_client, "list_audit_log", return_value=[entry]):
        resp = client.get("/audit")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "service" in body
    assert "api" in body


def test_a_withheld_detail_renders_as_withheld_and_never_as_content(
    client: Any,
) -> None:
    """End of the disclosure chain. The backend withholds the value; this
    proves the page renders the marker as a marker rather than printing it
    raw or, worse, falling back to some other field."""
    entry = _entry(action="withdraw_resume", details={"reason": "<withheld>"})
    with patch.object(api_client, "list_audit_log", return_value=[entry]):
        resp = client.get("/audit")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "withheld" in body


def test_a_disclosable_detail_is_shown(client: Any) -> None:
    """The counterpart: withholding must not swallow the classified values.
    A role change an auditor cannot read is not an audit trail."""
    entry = _entry(
        action="role_changed",
        details={"old_role": "recruiter", "new_role": "admin"},
    )
    with patch.object(api_client, "list_audit_log", return_value=[entry]):
        resp = client.get("/audit")
    body = resp.get_data(as_text=True)
    assert "old_role" in body
    assert "recruiter" in body
    assert "admin" in body


def test_an_empty_record_says_so_rather_than_rendering_an_empty_table(
    client: Any,
) -> None:
    with patch.object(api_client, "list_audit_log", return_value=[]):
        resp = client.get("/audit")
    assert resp.status_code == 200
    assert "No recorded actions" in resp.get_data(as_text=True)


@pytest.mark.parametrize("role", ["admin", "auditor"])
def test_admin_and_auditor_reach_the_page(client: Any, role: str) -> None:
    settings = MagicMock(cas_enabled=True)
    with (
        patch("frontend.app.get_settings", return_value=settings),
        patch.object(api_client, "get_cas_user", return_value=_cas(role)),
        patch.object(api_client, "list_audit_log", return_value=[_entry()]),
    ):
        resp = client.get("/audit")
    assert resp.status_code == 200, resp.get_data(as_text=True)


@pytest.mark.parametrize("role", ["recruiter", "hiring_manager"])
def test_other_roles_get_a_comprehensible_403_not_an_empty_page(
    client: Any, role: str
) -> None:
    """The page gate is a UX control, not the boundary — but it must still
    refuse, and refuse BEFORE calling the backend."""
    settings = MagicMock(cas_enabled=True)
    with (
        patch("frontend.app.get_settings", return_value=settings),
        patch.object(api_client, "get_cas_user", return_value=_cas(role)),
        patch.object(api_client, "list_audit_log") as fetch,
    ):
        resp = client.get("/audit")
    assert resp.status_code == 403
    assert not fetch.called, "the page fetched the record before refusing"


def test_a_backend_403_becomes_a_403_not_an_unhandled_500(client: Any) -> None:
    """The F4 lesson from fix/auth-boundary-fails-open: the backend re-checks
    the session independently, and an uncaught ``BadRequest`` from that check
    would surface as a Flask 500."""
    refusal = api_client.BadRequest("backend 403", status_code=403, detail={})
    with patch.object(api_client, "list_audit_log", side_effect=refusal):
        resp = client.get("/audit")
    assert resp.status_code == 403
    assert "Internal Server Error" not in resp.get_data(as_text=True)


def test_the_pages_only_post_control_is_the_audited_reveal(client: Any) -> None:
    """**This test previously asserted the page had NO POST control at all**,
    and that assertion was correct until the product owner answered D1 with
    option C (2026-08-19): reveal a withheld withdrawal reason on request, each
    read separately audited. The page now has exactly one write-method control,
    and it performs an audited READ, not a mutation.

    The invariant worth keeping is the one underneath the old assertion — an
    audit surface that can be *edited* from the browser is not an audit
    surface. So this pins the narrower claim: every POST target on this page is
    a reveal route, and nothing else. If a future change adds a second POST
    here, this fails and someone has to justify it.
    """
    entry = _entry(action="withdraw_resume", details={"reason": "<withheld>"})
    with patch.object(api_client, "list_audit_log", return_value=[entry]):
        resp = client.get("/audit")
    body = resp.get_data(as_text=True)
    assert "hx-post" not in body.lower()
    targets = re.findall(r'<form[^>]+method="post"[^>]+action="([^"]+)"', body, re.I)
    targets += re.findall(r'<form[^>]+action="([^"]+)"[^>]+method="post"', body, re.I)
    assert targets, "the audited reveal control is missing from the page"
    for target in targets:
        assert target.endswith("/reveal"), f"unexpected write control: {target}"


def test_the_action_filter_is_forwarded_to_the_backend(client: Any) -> None:
    with patch.object(api_client, "list_audit_log", return_value=[]) as fetch:
        client.get("/audit?action=reveal")
    assert fetch.call_args.kwargs["action"] == "reveal"


def test_a_junk_offset_does_not_500(client: Any) -> None:
    """Query strings are attacker-controlled; an audit page must degrade."""
    with patch.object(api_client, "list_audit_log", return_value=[]) as fetch:
        resp = client.get("/audit?offset=not-a-number")
    assert resp.status_code == 200
    assert fetch.call_args.kwargs["offset"] == 0


def test_the_nav_link_is_shown_to_an_auditor_and_hidden_from_a_recruiter() -> None:
    """The auditor's only destination — if it is not in the nav, the role is
    still effectively unusable no matter what the route allows."""
    app.config.update(TESTING=True)
    settings = MagicMock(cas_enabled=True, cas_service_base_url="http://cas.test")
    for role, expected in (("auditor", True), ("recruiter", False)):
        with (
            patch("frontend.app.get_settings", return_value=settings),
            patch.object(api_client, "get_cas_user", return_value=_cas(role)),
            patch.object(api_client, "list_jobs", return_value=[]),
        ):
            body = app.test_client().get("/").get_data(as_text=True)
        assert (
            "Access record" in body
        ) is expected, f"nav link visibility wrong for {role}"
