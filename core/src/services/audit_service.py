"""FU-5 slice 8 (ADR-019 §6/§7) — the generalized, append-only ``audit_log``
writer that replaces ``reveal_service.record_reveal`` on the reveal path.

:func:`record_audit` is a PURE insert, mirroring ``reveal_service
.record_reveal``'s own discipline: one INSERT into ``audit_log``, no
read-back, no decryption, no PII ever touched here. Unlike ``record_reveal``
(which mints and returns a ``UUID`` because the old reveal route needed it
back), ``record_audit`` has no caller that needs the minted id — the row's
own ``id`` is DB-generated (``DEFAULT gen_random_uuid()``) and this function
returns ``None``.

**No defensive validation of the actor identity here.** The
``audit_log_actor_identity`` CHECK constraint (``src/models/ddl.py``) is the
single source of truth for "exactly one of ``actor_user_id`` /
``actor_service`` is set" — this function is a passthrough, same as
``record_reveal``. Callers (``src.api.routes.resumes``'s reveal handler
above all) are responsible for supplying a legal combination; a caller that
does not gets a real Postgres constraint violation, not a Python-side
exception masking it.

The old ``reveal_audit`` sink (``src.services.reveal_service``) is KEPT,
read-only, per ADR-019 §6's migration posture — this module does not replace
or delete it.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from src.schemas.audit import AuditDetail, AuditLogItem
from src.services import DbConn

#: Rendered in place of a value the auditor viewer will not disclose. A marker,
#: never a removal: an auditor must be able to tell "no value was recorded" from
#: "a value was recorded and you are not being shown it" — materially different
#: facts in a compliance review, and a view that silently omits fields is worse
#: than useless because it looks complete.
WITHHELD = "<withheld>"

#: ALLOWLIST of ``details`` keys safe to disclose, keyed by the ``action`` that
#: writes them. Scoped by action, not by bare key name: ``old_role`` is safe
#: *because of what ``role_changed`` puts there*, and an allowlist keyed only by
#: name would let a future writer smuggle content through a familiar-looking
#: key.
#:
#: **Fail-closed.** Anything absent is WITHHELD, so a new ``record_audit``
#: caller inventing a new details key gets withholding by default until someone
#: classifies it. A blocklist would protect against today's two writers and
#: silently leak the third one added next year — the ROADMAP A7 shape.
#:
#: Deliberately NOT listed: ``withdraw_resume``'s ``reason``. It is operator-
#: typed prose about a specific, named candidate, and this viewer is exactly the
#: surface that would render it. Whether an auditor should be able to read it at
#: all WAS a product/privacy question carried in ADR-036; it is now answered —
#: D1 = option C, 2026-08-19 — and the answer is "on request, separately
#: audited", NOT "disclosed here". So it stays off this allowlist and appears
#: instead on ``_REVEALABLE_DETAIL_ACTIONS`` below.
#:
#: ``reveal_audit_detail``'s ``revealed_action`` IS listed. It names which
#: action's details a reveal disclosed, and it is enum-shaped and non-PII
#: (``"withdraw_resume"``). Withholding it would make the trail of reveals
#: unreadable without revealing it in turn, which defeats the compensating
#: control the reveal route exists to provide.
_DISCLOSABLE_DETAIL_KEYS: dict[str, frozenset[str]] = {
    "role_changed": frozenset({"old_role", "new_role"}),
    "reveal_audit_detail": frozenset({"revealed_action"}),
}

#: ALLOWLIST of actions whose WITHHELD ``details`` an auditor may reveal on
#: request, each reveal separately audited — D1 = option C, answered by the
#: product owner on 2026-08-19 (see ``docs/adr/036-auditor-audit-log-viewer.md``).
#:
#: **This is the same fail-closed posture as ``_DISCLOSABLE_DETAIL_KEYS``, and
#: for a sharper reason.** That allowlist governs what is disclosed to everyone
#: with audit access; this one governs what can be *un*-withheld at all. A
#: blocklist here would hand every future ``record_audit`` caller a reveal path
#: for its new details key by default — the exact ROADMAP A7 shape, where the
#: rule is written in prose and nothing enforces it.
#:
#: Two invariants hold between the two allowlists, and
#: ``tests/unit/test_audit_service_detail_reveal.py`` enforces both rather than
#: leaving them to a comment: (1) no action is both freely disclosed and
#: revealable — a "reveal" of a value already on screen would audit a
#: restricted read that was never restricted; (2) every revealable action
#: actually has something withheld, or the control is dead weight that reads,
#: to an auditor, as though something is being kept from them.
_REVEALABLE_DETAIL_ACTIONS: frozenset[str] = frozenset({"withdraw_resume"})


def _decode_details(raw: Any) -> Any:
    """Decode a ``details`` value as asyncpg hands it back.

    asyncpg returns ``jsonb`` as ``str``, so both audit reads must parse it —
    but a legacy or hand-written row can hold text that is not valid JSON, and
    ``json.loads`` on it raises. **An audit read must degrade, never 500**:
    ``redact_audit_details`` has promised exactly that since Phase 1.4, and the
    promise was enforced only *inside* that function while the decode one layer
    above it could still crash the whole page. The ROADMAP A7 shape — an
    invariant stated in prose with nothing holding it.

    An undecodable payload is returned verbatim, which makes it a non-``dict``,
    which makes ``redact_audit_details`` withhold it wholesale and the reveal
    route refuse it. Fail-closed by construction rather than by remembering.
    """
    if not isinstance(raw, str):
        return raw
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def redact_audit_details(action: str, details: Any) -> Any:
    """Apply the disclosure allowlist to one row's ``details``.

    Per-KEY, not per-row: withholding one value must not blind the auditor to
    the classified ones beside it. An empty or null value is passed through
    as-is — there is nothing to protect, and marking it withheld would assert
    that a value exists when none does.

    Never raises. ``details`` is ``jsonb``, so a legacy or hand-written row may
    hold a scalar or a list rather than an object; anything that is not a dict
    is withheld wholesale rather than inspected. An audit read must degrade,
    never 500.
    """
    if details is None:
        return None
    if not isinstance(details, dict):
        return WITHHELD
    allowed = _DISCLOSABLE_DETAIL_KEYS.get(action, frozenset())
    return {
        key: value if (key in allowed or not value) else WITHHELD
        for key, value in details.items()
    }


_INSERT_SQL = """
INSERT INTO audit_log (
    actor_kind, actor_user_id, actor_service, action, subject_type,
    subject_id, job_id, context, details
) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb)
"""


async def record_audit(
    conn: DbConn,
    *,
    actor_kind: str,
    actor_user_id: UUID | None,
    actor_service: str | None,
    action: str,
    subject_type: str,
    subject_id: UUID,
    job_id: UUID | None = None,
    context: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    """Write one append-only ``audit_log`` row.

    Deliberately NOT wrapped in ``async with conn.transaction():`` — a bare
    ``conn.execute`` outside an explicit transaction block is a single
    autocommitted statement under asyncpg, so the row is durably committed
    the instant this call returns. This is load-bearing for ADR-016/ADR-019
    §7's ordering guarantee: callers that write the audit row BEFORE
    attempting a decrypt (see ``src.api.routes.resumes.reveal_resume``) get a
    row that survives a crash in the decrypt step, even though both steps
    share the same pooled connection.
    """
    await conn.execute(
        _INSERT_SQL,
        actor_kind,
        actor_user_id,
        actor_service,
        action,
        subject_type,
        subject_id,
        job_id,
        context,
        json.dumps(details) if details is not None else None,
    )


_LIST_SQL = """
SELECT a.id,
       a.actor_kind,
       a.actor_user_id,
       u.cas_username AS actor_username,
       a.actor_service,
       a.action,
       a.subject_type,
       a.subject_id,
       a.job_id,
       a.context,
       a.details,
       a.occurred_at
FROM audit_log a
LEFT JOIN users u ON u.id = a.actor_user_id
WHERE ($1::text IS NULL OR a.action = $1)
  AND ($2::text IS NULL OR a.subject_type = $2)
  AND ($3::uuid IS NULL OR a.job_id = $3)
ORDER BY a.occurred_at DESC, a.id DESC
LIMIT $4 OFFSET $5
"""


async def list_audit_log(
    conn: Any,
    *,
    limit: int = 100,
    offset: int = 0,
    action: str | None = None,
    subject_type: str | None = None,
    job_id: UUID | None = None,
) -> list[AuditLogItem]:
    """Read the LIVE ``audit_log``, newest first (Phase 1.4 / ADR-036).

    **The LEFT JOIN is load-bearing.** ``actor_kind='service'`` rows carry a
    NULL ``actor_user_id`` by CHECK constraint, so an INNER JOIN would silently
    hide every one of them — and those are precisely the events an auditor most
    needs, since an unattributable ``actor_service='api'`` write is the
    signature of the ADR-034 exploit. A viewer that quietly dropped them would
    be worse than no viewer, because it would look complete.

    **``a.id`` is a tiebreak, not decoration.** Rows written inside one
    statement share ``occurred_at`` to the microsecond, so ordering by the
    timestamp alone is not a total order: without the tiebreak the same row can
    appear on two pages while another appears on none.

    **Never joins ``resumes`` or ``jobs``**, mirroring
    ``/audit/reveals-legacy``'s own discipline — nothing decrypts, so no
    candidate PII can reach this path even by accident. ``details`` is passed
    through :func:`redact_audit_details` before it leaves this function, so the
    boundary holds for every caller rather than depending on each one
    remembering to apply it.

    Filters are NULL-guarded in a fixed-shape query rather than concatenated,
    so parameter positions stay stable and there is no dynamic SQL to review.
    """
    rows = await conn.fetch(_LIST_SQL, action, subject_type, job_id, limit, offset)
    items: list[AuditLogItem] = []
    for row in rows:
        # asyncpg hands back `jsonb` as `str`; `_decode_details` also survives
        # a row whose text is not valid JSON (see its docstring).
        decoded = _decode_details(row["details"])
        items.append(
            AuditLogItem(
                id=row["id"],
                actor_kind=row["actor_kind"],
                actor_user_id=row["actor_user_id"],
                actor_username=row["actor_username"],
                actor_service=row["actor_service"],
                action=row["action"],
                subject_type=row["subject_type"],
                subject_id=row["subject_id"],
                job_id=row["job_id"],
                context=row["context"],
                details=redact_audit_details(row["action"], decoded),
                occurred_at=row["occurred_at"],
            )
        )
    return items


def is_revealable_action(action: str) -> bool:
    """Is this ``action``'s withheld ``details`` payload revealable on request?

    The single read of :data:`_REVEALABLE_DETAIL_ACTIONS`. Exposed as a
    function rather than the set itself so the route layer cannot accidentally
    grow its own membership rule — the frontend does not even know this
    allowlist exists (it offers the control wherever a value is withheld and
    lets this fail-close), which keeps one implementation of the disclosure
    decision instead of two that drift.
    """
    return action in _REVEALABLE_DETAIL_ACTIONS


_READ_DETAIL_SQL = """
SELECT id, action, details, occurred_at
FROM audit_log
WHERE id = $1
"""


async def read_audit_detail(conn: DbConn, *, audit_id: UUID) -> AuditDetail | None:
    """Read ONE ``audit_log`` row's ``details`` **un-redacted** — D1 = option C.

    This is the only read in the codebase that bypasses
    :func:`redact_audit_details`, and it is deliberately a separate function
    rather than a flag on :func:`list_audit_log`: a boolean parameter on the
    list read would put "disclose everything" one keyword argument away from
    every existing caller, which is precisely how a fail-closed boundary stops
    being one.

    **This function does NOT decide whether the reveal is allowed.** It returns
    the row; the route gates on :func:`is_revealable_action` and on an
    attributable human session, and writes the audit row BEFORE the value is
    returned to the caller. Keeping the read dumb means there is exactly one
    place (the route) where the ordering guarantee can be read off.

    Selects ``audit_log``'s own columns only — no join against ``resumes`` or
    ``jobs``, matching every other read in this module, so nothing decrypts and
    no candidate PII can reach this path even by accident.

    Returns ``None`` for a missing row rather than raising: "no such row" is an
    ordinary outcome the route turns into a 404, not an error condition.
    """
    row = await conn.fetchrow(_READ_DETAIL_SQL, audit_id)
    if row is None:
        return None
    decoded = _decode_details(row["details"])
    return AuditDetail(id=row["id"], action=row["action"], details=decoded)


__all__ = [
    "record_audit",
    "list_audit_log",
    "read_audit_detail",
    "redact_audit_details",
    "is_revealable_action",
    "WITHHELD",
]
