"""RED pin — Phase 1.4 slice 1, the ``audit_log.details`` disclosure boundary.

**Why this is the first test written for the auditor viewer.** The viewer's
whole purpose is to show an auditor the access record, and ``audit_log.details``
is the only column in it that carries free-form, caller-supplied content. Two
writers populate it today:

* ``users.py`` role changes — ``{"old_role", "new_role"}``. Enum-shaped, non-PII,
  and the single most audit-relevant detail the table holds.
* ``resume_service`` withdrawals — ``{"reason": <operator free text>}``. An
  arbitrary string typed by a human about a specific candidate. **This is the
  leak.** A candidate's withdrawal reason may name them, describe their
  circumstances, or quote correspondence, and the auditor viewer is exactly the
  surface that would render it.

**The decision, and why it is fail-CLOSED rather than a blocklist.** A
blocklist of known-bad keys ("hide ``reason``") protects against today's two
writers and silently leaks the third one somebody adds next year — the ROADMAP
A7 shape, an invariant that holds only as long as nobody extends the code. So
the boundary is an allowlist: a key not explicitly classified as safe is
**withheld**, and a new ``record_audit`` caller adding a new details key gets
withholding by default until someone classifies it.

**Withheld, not dropped.** The auditor is told a value EXISTS and is not shown
it, rather than the key vanishing. An audit view that silently omits fields is
worse than useless to an auditor — they cannot tell "no reason was recorded"
from "a reason was recorded and you are not being shown it", and those are
materially different facts in a compliance review.

**What is deliberately NOT decided here.** Whether an auditor *should* be able
to read a withdrawal reason at all is a product/privacy question for a human —
it is plausibly within their remit and plausibly a PIPEDA/FIPPA problem. This
slice makes the safe choice, marks it visibly in the UI, and records the
question rather than answering it by implementation. See ADR-036.
"""

from __future__ import annotations

from typing import Any

import pytest

from src.services.audit_service import WITHHELD, redact_audit_details


def test_role_change_keys_are_disclosed_verbatim() -> None:
    """The role-change pair is the most audit-relevant detail in the table and
    is enum-shaped — no free text, no PII, nothing to withhold."""
    assert redact_audit_details(
        "role_changed", {"old_role": "recruiter", "new_role": "admin"}
    ) == {"old_role": "recruiter", "new_role": "admin"}


def test_withdrawal_reason_is_withheld_not_shown() -> None:
    """THE pin. Operator free text about a specific candidate never renders."""
    secret = "Jane Q Candidate emailed asking us to delete her file"
    result = redact_audit_details("withdraw_resume", {"reason": secret})
    assert result == {"reason": WITHHELD}
    assert secret not in str(result)
    assert "Jane" not in str(result)


def test_withdrawal_reason_absent_is_distinguishable_from_withheld() -> None:
    """An auditor must be able to tell "no reason recorded" from "a reason was
    recorded and you are not being shown it" — materially different facts."""
    assert redact_audit_details("withdraw_resume", None) is None
    assert redact_audit_details("withdraw_resume", {}) == {}


def test_an_unclassified_key_is_withheld_by_default() -> None:
    """Fail-closed. A future ``record_audit`` caller inventing a new details
    key gets withholding for free — the allowlist has to be extended
    deliberately, so a new writer cannot silently widen disclosure.

    This is the assertion that would have caught the whole A7 family: the
    default for something nobody has thought about is SAFE.
    """
    assert redact_audit_details(
        "some_future_action", {"candidate_email": "jane@example.test"}
    ) == {"candidate_email": WITHHELD}


def test_an_unclassified_key_alongside_a_safe_one_is_withheld_individually() -> None:
    """Per-key, not per-row: withholding one value must not blind the auditor
    to the classified ones sitting beside it."""
    assert redact_audit_details(
        "role_changed",
        {"old_role": "recruiter", "new_role": "admin", "note": "see ticket 1234"},
    ) == {"old_role": "recruiter", "new_role": "admin", "note": WITHHELD}


def test_a_safe_key_is_scoped_to_the_action_that_declares_it() -> None:
    """``old_role`` is safe *because of what role_changed writes there*. The
    same key name arriving under a different action is not covered by that
    reasoning and is withheld — an allowlist keyed only by name would let a
    future writer smuggle content through a familiar-looking key."""
    assert redact_audit_details("withdraw_resume", {"old_role": "anything"}) == {
        "old_role": WITHHELD
    }


@pytest.mark.parametrize("payload", [{"reason": None}, {"reason": ""}])
def test_withholding_does_not_invent_content_for_an_empty_value(
    payload: dict[str, Any],
) -> None:
    """An empty or null value is disclosed as-is: there is nothing to protect,
    and marking it WITHHELD would tell the auditor a value exists when none
    does — the same confusion the withheld marker exists to prevent."""
    assert redact_audit_details("withdraw_resume", payload) == payload


def test_non_dict_details_never_raises() -> None:
    """``details`` is jsonb — a hand-written or legacy row could hold a scalar
    or a list. The viewer must degrade, never 500 on an audit read."""
    assert redact_audit_details("withdraw_resume", "a bare string") == WITHHELD
    assert redact_audit_details("withdraw_resume", [1, 2, 3]) == WITHHELD
