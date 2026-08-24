"""RED pin — D1 = option C, service layer: reading ONE ``audit_log`` row's
``details`` un-redacted, for the separately-audited reveal route.

``audit_service.list_audit_log`` passes every row through
``redact_audit_details`` *inside the service*, deliberately, so the disclosure
boundary has one implementation rather than one per caller. That makes the
reveal path a genuinely new read: it must bypass that filter for exactly one
row, and nothing else in the module may grow the ability to.

**The invariants pinned here are the ones a route test cannot see:**

* the read selects ``audit_log``'s own columns only — never a join against
  ``resumes`` or ``jobs``, matching every other read in this module, so no
  candidate PII can reach this path even by accident;
* ``details`` comes back RAW — the whole point — and asyncpg's ``jsonb``-as-
  ``str`` handoff is decoded here, the same idiom ``list_audit_log`` uses;
* the revealable-action allowlist is **fail-closed** and, crucially, is
  **disjoint from the disclosable-key allowlist**: an action may not be both
  freely disclosed and offered as a "reveal", because that would present the
  auditor a button that reveals what they can already read, and would make the
  audit trail of reveals record reads that were never restricted.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from src.services import audit_service

_NOW = dt.datetime(2026, 8, 20, tzinfo=dt.UTC)
_REASON = "withdrawn at the candidate's own request"


class _Row(dict[str, Any]):
    def __getitem__(self, key: str) -> Any:
        return dict.get(self, key)


def _conn(row: _Row | None) -> MagicMock:
    conn = MagicMock(name="conn")
    conn.fetchrow = AsyncMock(return_value=row)
    return conn


async def test_the_raw_details_come_back_unredacted() -> None:
    audit_id = uuid4()
    conn = _conn(
        _Row(
            {
                "id": audit_id,
                "action": "withdraw_resume",
                "details": {"reason": _REASON},
                "occurred_at": _NOW,
            }
        )
    )
    row = await audit_service.read_audit_detail(conn, audit_id=audit_id)
    assert row is not None
    assert row.action == "withdraw_resume"
    assert row.details == {"reason": _REASON}
    assert audit_service.WITHHELD not in json.dumps(row.details)


async def test_a_jsonb_string_payload_is_decoded() -> None:
    """asyncpg hands ``jsonb`` back as ``str``; ``list_audit_log`` already
    guards for this and so must the reveal read, or the auditor is shown a
    quoted JSON blob instead of the sentence someone typed."""
    audit_id = uuid4()
    conn = _conn(
        _Row(
            {
                "id": audit_id,
                "action": "withdraw_resume",
                "details": json.dumps({"reason": _REASON}),
                "occurred_at": _NOW,
            }
        )
    )
    row = await audit_service.read_audit_detail(conn, audit_id=audit_id)
    assert row is not None
    assert row.details == {"reason": _REASON}


async def test_a_missing_row_is_none_not_an_exception() -> None:
    conn = _conn(None)
    assert await audit_service.read_audit_detail(conn, audit_id=uuid4()) is None


async def test_the_read_never_joins_resumes_or_jobs() -> None:
    audit_id = uuid4()
    conn = _conn(
        _Row(
            {
                "id": audit_id,
                "action": "withdraw_resume",
                "details": {"reason": _REASON},
                "occurred_at": _NOW,
            }
        )
    )
    await audit_service.read_audit_detail(conn, audit_id=audit_id)
    sql = str(conn.fetchrow.await_args.args[0]).lower()
    assert "join" not in sql, sql
    assert "resumes" not in sql, sql
    assert "jobs" not in sql, sql


def test_the_revealable_allowlist_is_fail_closed() -> None:
    assert audit_service.is_revealable_action("withdraw_resume") is True
    for action in ("role_changed", "reveal", "assign_job", "", "anything_new"):
        assert audit_service.is_revealable_action(action) is False, action


def test_no_action_is_both_freely_disclosed_and_revealable() -> None:
    """The A7 shape for this slice: an invariant that is obvious in prose and
    enforced nowhere. If a future change classifies ``withdraw_resume``'s
    ``reason`` as disclosable, this route silently becomes a button that
    "reveals" — and audits a restricted read of — a value already on screen."""
    disclosed = {
        action
        for action, keys in audit_service._DISCLOSABLE_DETAIL_KEYS.items()
        if keys
    }
    overlap = disclosed & set(audit_service._REVEALABLE_DETAIL_ACTIONS)
    assert not overlap, (
        f"{sorted(overlap)} is both disclosed by the allowlist and offered for "
        "audited reveal — one of the two classifications is wrong"
    )


@pytest.mark.parametrize("action", sorted(audit_service._REVEALABLE_DETAIL_ACTIONS))
def test_every_revealable_action_actually_has_something_withheld(action: str) -> None:
    """A reveal path for an action that withholds nothing is dead weight that
    reads, to an auditor, as though something is being kept from them."""
    redacted = audit_service.redact_audit_details(action, {"reason": _REASON})
    assert redacted == {"reason": audit_service.WITHHELD}


def test_the_reveal_marker_itself_is_disclosed() -> None:
    """The trail of reveals must be readable without revealing it in turn."""
    details = {"revealed_action": "withdraw_resume"}
    assert audit_service.redact_audit_details("reveal_audit_detail", details) == details


async def test_an_undecodable_jsonb_payload_degrades_rather_than_raising() -> None:
    """Found by the reveal route's own "legacy scalar" case, and it was a real
    defect one layer up from where the promise was written.

    ``redact_audit_details``'s docstring has promised since Phase 1.4 that "an
    audit read must degrade, never 500" — and it does, for anything handed to
    it. But BOTH reads call ``json.loads`` on the raw column first, and a row
    whose text is not valid JSON made that raise before the promise could
    apply. Exactly the ROADMAP A7 shape: the invariant is in prose, and nothing
    holds it.

    Degrading means the value stays a non-``dict``, so it is withheld wholesale
    and the reveal route refuses it — fail-closed, not merely non-crashing.
    """
    audit_id = uuid4()
    conn = _conn(
        _Row(
            {
                "id": audit_id,
                "action": "withdraw_resume",
                "details": "not json at all",
                "occurred_at": _NOW,
            }
        )
    )
    row = await audit_service.read_audit_detail(conn, audit_id=audit_id)
    assert row is not None
    assert not isinstance(row.details, dict)
    assert audit_service.redact_audit_details(row.action, row.details) == (
        audit_service.WITHHELD
    )


async def test_the_list_read_also_survives_an_undecodable_payload() -> None:
    """The same crash sat on the list read, which is the page an auditor opens
    first — one malformed row would have taken down the whole record."""
    conn = MagicMock(name="conn")
    conn.fetch = AsyncMock(
        return_value=[
            _Row(
                {
                    "id": uuid4(),
                    "actor_kind": "user",
                    "actor_user_id": uuid4(),
                    "actor_username": "priya",
                    "actor_service": None,
                    "action": "withdraw_resume",
                    "subject_type": "resume",
                    "subject_id": uuid4(),
                    "job_id": None,
                    "context": None,
                    "details": "not json at all",
                    "occurred_at": _NOW,
                }
            )
        ]
    )
    items = await audit_service.list_audit_log(conn)
    assert len(items) == 1
    assert items[0].details == audit_service.WITHHELD
