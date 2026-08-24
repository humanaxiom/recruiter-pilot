"""RED pin — D1 = option C, viewer layer: the audited "reveal this reason"
control on the access-record page.

**Why the page needs a control at all.** Before this slice the page ended with a
hint telling the auditor to "ask an administrator if a specific one is needed
for a review" — which is the engineer-runs-SQL escape hatch ADR-036 was written
to remove, written into the UI as though it were a feature. Option C replaces it
with a button that produces the record instead of bypassing it.

**The one architectural choice pinned here, and it is deliberate.** This page
does NOT know which actions are revealable. It offers the control wherever the
backend has withheld a value, and the backend's ``_REVEALABLE_DETAIL_ACTIONS``
fail-closes on everything else. The allowlist therefore has exactly ONE
implementation — the ROADMAP A7 shape is a rule stated in prose in two places
that drift, and the disclosure boundary already has one home
(``audit_service``). A refused reveal renders as a message, not a 500.

**What must NOT happen** is a reveal that the auditor did not ask for. A GET of
this page — including pagination and filtering — reveals nothing and writes no
audit row; only the POST does. A page that re-revealed on every load would fill
the trail with reads nobody performed, which is worse than no trail.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from frontend import api_client

_REASON = "duplicate application; withdrawn after the candidate confirmed"
_WITHHELD = "<withheld>"


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
        "action": "withdraw_resume",
        "subject_type": "resume",
        "subject_id": str(uuid4()),
        "job_id": str(uuid4()),
        "context": None,
        "details": {"reason": _WITHHELD},
        "occurred_at": "2026-08-20T10:00:00Z",
    }
    base.update(overrides)
    return base


def _cas(role: str | None) -> Any:
    return {"authenticated": True, "role": role, "username": "someone"}


def test_a_withheld_value_offers_a_reveal_control(client: Any) -> None:
    entry = _entry()
    with patch.object(api_client, "list_audit_log", return_value=[entry]):
        resp = client.get("/audit")
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200, body
    assert f"/audit/{entry['id']}/reveal" in body


def test_a_row_with_nothing_withheld_offers_no_reveal_control(client: Any) -> None:
    """A button that reveals what is already on screen teaches the auditor
    that the control means nothing."""
    entry = _entry(
        action="role_changed", details={"old_role": "recruiter", "new_role": "admin"}
    )
    with patch.object(api_client, "list_audit_log", return_value=[entry]):
        resp = client.get("/audit")
    body = resp.get_data(as_text=True)
    assert f"/audit/{entry['id']}/reveal" not in body


def test_a_row_with_no_details_at_all_offers_no_reveal_control(client: Any) -> None:
    entry = _entry(action="reveal", details=None)
    with patch.object(api_client, "list_audit_log", return_value=[entry]):
        resp = client.get("/audit")
    assert f"/audit/{entry['id']}/reveal" not in resp.get_data(as_text=True)


def test_merely_loading_the_page_reveals_nothing(client: Any) -> None:
    """The load-bearing negative. One click must equal one audited read."""
    guard = MagicMock(side_effect=AssertionError("a GET performed a reveal"))
    with (
        patch.object(api_client, "list_audit_log", return_value=[_entry()]),
        patch.object(api_client, "reveal_audit_detail", guard),
    ):
        resp = client.get("/audit?action=withdraw_resume&offset=100")
    assert resp.status_code == 200
    assert _REASON not in resp.get_data(as_text=True)


def test_posting_the_reveal_shows_the_real_reason_for_that_row(client: Any) -> None:
    entry = _entry()
    revealed = {
        "id": entry["id"],
        "action": "withdraw_resume",
        "details": {"reason": _REASON},
    }
    with (
        patch.object(api_client, "list_audit_log", return_value=[entry]),
        patch.object(api_client, "reveal_audit_detail", return_value=revealed) as call,
    ):
        resp = client.post(f"/audit/{entry['id']}/reveal", data={})
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200, body
    assert _REASON in body
    assert call.call_args.args[0] == entry["id"] or str(call.call_args.args[0]) == str(
        entry["id"]
    )


def test_only_the_revealed_row_is_unmasked(client: Any) -> None:
    """One reveal is one row. A reveal that unmasked the whole page would be
    option B (blanket disclosure) wearing option C's audit trail."""
    target = _entry()
    other = _entry(details={"reason": _WITHHELD})
    revealed = {
        "id": target["id"],
        "action": "withdraw_resume",
        "details": {"reason": _REASON},
    }
    with (
        patch.object(api_client, "list_audit_log", return_value=[target, other]),
        patch.object(api_client, "reveal_audit_detail", return_value=revealed),
    ):
        resp = client.post(f"/audit/{target['id']}/reveal", data={})
    body = resp.get_data(as_text=True)
    assert _REASON in body
    assert "withheld" in body, "the sibling row was unmasked too"


def test_the_filter_and_page_position_survive_the_reveal(client: Any) -> None:
    """An auditor three pages into a filtered record must not be thrown back
    to the top to read one value."""
    entry = _entry()
    revealed = {
        "id": entry["id"],
        "action": "withdraw_resume",
        "details": {"reason": _REASON},
    }
    with (
        patch.object(api_client, "list_audit_log", return_value=[entry]) as listing,
        patch.object(api_client, "reveal_audit_detail", return_value=revealed),
    ):
        resp = client.post(
            f"/audit/{entry['id']}/reveal",
            data={"action": "withdraw_resume", "offset": "200"},
        )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert listing.call_args.kwargs["action"] == "withdraw_resume"
    assert listing.call_args.kwargs["offset"] == 200


@pytest.mark.parametrize("role", ["recruiter", "hiring_manager"])
def test_other_roles_cannot_post_a_reveal(client: Any, role: str) -> None:
    settings = MagicMock(cas_enabled=True)
    guard = MagicMock(side_effect=AssertionError(f"reveal ran for role={role}"))
    with (
        patch("frontend.app.get_settings", return_value=settings),
        patch.object(api_client, "get_cas_user", return_value=_cas(role)),
        patch.object(api_client, "reveal_audit_detail", guard),
    ):
        resp = client.post(f"/audit/{uuid4()}/reveal", data={})
    assert resp.status_code == 403


def test_a_backend_refusal_renders_a_message_not_a_500(client: Any) -> None:
    """The backend fail-closes on a non-revealable action. The page must say
    so — the F4 lesson from fix/auth-boundary-fails-open is that an uncaught
    ``BadRequest`` surfaces as a Flask 500."""
    entry = _entry()
    refusal = api_client.BadRequest("backend 403", status_code=403, detail={})
    with (
        patch.object(api_client, "list_audit_log", return_value=[entry]),
        patch.object(api_client, "reveal_audit_detail", side_effect=refusal),
    ):
        resp = client.post(f"/audit/{entry['id']}/reveal", data={})
    body = resp.get_data(as_text=True)
    assert resp.status_code == 403, body
    assert "Internal Server Error" not in body
    assert _REASON not in body


def test_a_missing_audit_row_is_a_404(client: Any) -> None:
    with (
        patch.object(api_client, "list_audit_log", return_value=[]),
        patch.object(
            api_client, "reveal_audit_detail", side_effect=api_client.NotFound("gone")
        ),
    ):
        resp = client.post(f"/audit/{uuid4()}/reveal", data={})
    assert resp.status_code == 404


def test_a_backend_outage_degrades_rather_than_500s(client: Any) -> None:
    outage = api_client.BackendUnavailable("connection refused")
    with patch.object(api_client, "reveal_audit_detail", side_effect=outage):
        resp = client.post(f"/audit/{uuid4()}/reveal", data={})
    assert resp.status_code == 503, resp.get_data(as_text=True)


def test_the_reveal_form_carries_a_csrf_token(client: Any) -> None:
    """This is the first write-method control the page has ever had, so it is
    the first time the page needs a token at all."""
    entry = _entry()
    with patch.object(api_client, "list_audit_log", return_value=[entry]):
        body = client.get("/audit").get_data(as_text=True)
    form_start = body.index(f"/audit/{entry['id']}/reveal")
    form_end = body.index("</form>", form_start)
    assert "csrf_token" in body[form_start:form_end]


def test_the_api_client_posts_to_the_backend_reveal_route() -> None:
    """The client is the only place the backend path is written down."""
    response = MagicMock()
    response.json.return_value = {"id": "x", "action": "withdraw_resume", "details": {}}
    audit_id = uuid4()
    with patch.object(api_client, "_request", return_value=response) as request:
        api_client.reveal_audit_detail(audit_id)
    assert request.call_args.args[0] == "POST"
    assert request.call_args.args[1] == f"/audit/log/{audit_id}/reveal"
