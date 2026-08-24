"""FU-5 slice 10 (ADR-019 §6 / §9.4) — ``GET /audit/reveals-legacy``, a
read-only, paginated view of the FROZEN ``reveal_audit`` table.

``reveal_audit`` was the live reveal-audit sink before slice 8 cut reveal over
to the generalized ``audit_log`` table (see ``src.services.reveal_service``'s
module docstring and ``src.api.routes.resumes.reveal_resume``). This route
exists purely for historical review of that frozen table, per ADR-019 §6 —
it is the auditor role's first real capability anywhere in the codebase
(§9.4's ratified build decision 4), so it is gated admin + auditor, not
admin-only like the other write-adjacent routes.

Reads ``reveal_audit`` ALONE — never joins ``resumes`` — so nothing decrypts
and no candidate PII can leak through this endpoint even by accident. No
router-level ``prefix``; the absolute path lives on the route decorator,
mirroring every other Phase-6/FU-5 route module.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from src.api.deps import (
    Role,
    actor_fields_from_user,
    require_role,
    require_session_role,
    resolve_user,
)
from src.errors import NotFoundError
from src.models.pool import Db
from src.schemas.audit import AuditDetail, AuditLogItem, RevealAuditItem
from src.schemas.auth import User
from src.services import audit_service, reveal_service

router = APIRouter()

_AUDIT_READERS: tuple[Role, ...] = (Role.ADMIN, Role.AUDITOR)


@router.get(
    "/audit/reveals-legacy", dependencies=[Depends(require_role(*_AUDIT_READERS))]
)
async def list_reveals_legacy(
    db: Db,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[RevealAuditItem]:
    return await reveal_service.list_reveal_audit(db, limit=limit, offset=offset)


@router.get(
    "/audit/log",
    dependencies=[Depends(require_session_role(*_AUDIT_READERS))],
)
async def list_audit_log(
    db: Db,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    action: str | None = Query(default=None),
    subject_type: str | None = Query(default=None),
    # `Annotated` rather than a bare `Query(...)` default: bugbear's B008 flags
    # the call-in-default form for this annotation, and jobs.py's
    # `status_filter` already established this shape for the same reason.
    job_id: Annotated[UUID | None, Query()] = None,
) -> list[AuditLogItem]:
    """Phase 1.4 / ADR-036 — the auditor's read of the LIVE ``audit_log``.

    The sibling ``/audit/reveals-legacy`` above reads ``reveal_audit``, FROZEN
    at FU-5 slice 8; every audit event since the cutover lives in ``audit_log``,
    which until this route had **no read path anywhere in the application** —
    producing the access record meant an engineer running SQL by hand.

    **Gated on the CAS SESSION role alone — deliberately, and not by
    oversight.** Two reasons, and they point the same way:

    1. *Attributability.* The audit log names who looked at whom, and it is the
       record an auditor relies on to detect misuse. Reading it should itself be
       attributable to a person; a shared service key is by construction not a
       person. ``require_session_role`` 403s on ``user is None`` (ADR-034 §2),
       so there is no keyless path in. This is a judgement about THIS surface,
       not an answer to ADR-034's carried question about machine readers in
       general (ADR-036 §4).
    2. *Reachability.* A keyed ``require_role(ADMIN, AUDITOR)`` would make this
       route **unreachable from the browser**, which is the only way an auditor
       would ever use it: the Flask BFF presents ONE fixed ``recruiter`` key for
       every browser it serves (FU-4/D6, ``api_client.build_client``), while
       forwarding the real user's session cookie. Session-role gating is exactly
       how the other browser-reachable privileged surface already works —
       ``users.py::_require_admin_session``. Its sibling
       ``/audit/reveals-legacy`` gates on the KEY and is, for this reason,
       browser-unreachable today.

    Like its sibling it never joins ``resumes``, so nothing decrypts and no
    candidate PII can leak here even by accident; ``details`` is additionally
    allowlist-filtered in the service (``redact_audit_details``).
    """
    return await audit_service.list_audit_log(
        db,
        limit=limit,
        offset=offset,
        action=action,
        subject_type=subject_type,
        job_id=job_id,
    )


@router.post(
    "/audit/log/{audit_id}/reveal",
    dependencies=[Depends(require_session_role(*_AUDIT_READERS))],
)
async def reveal_audit_detail(
    audit_id: UUID,
    db: Db,
    user: Annotated[User | None, Depends(resolve_user)],
    context: Annotated[str | None, Query(max_length=64)] = None,
) -> AuditDetail:
    """D1 = option C (answered 2026-08-19) — the AUDITED reveal of one withheld
    ``audit_log.details`` payload.

    **What this exists to fix.** ``withdraw_resume``'s ``reason`` is the only
    free-text an operator types about a named candidate, and
    ``redact_audit_details`` withholds it. That is right by default and wrong in
    the one case that matters: an auditor investigating a wrongful-withdrawal
    complaint could not do the job without escalating to an engineer running SQL
    by hand — the unaudited read ADR-036 was written to eliminate. Option C
    keeps the default closed and makes the exception *recorded* rather than
    *ad hoc*.

    **The shape is copied, on purpose, from ``POST /resumes/{id}/reveal``** —
    the audited un-blinding of candidate PII. Same class of data, same
    mechanism: an explicit POST (never a query parameter on a GET, per ADR-016),
    an attributable human, and the audit row written first.

    Ordering, restating ADR-016 / ADR-019 §7: **read → gate → audit → return.**
    The audit row is written and autocommitted (``record_audit`` is a bare
    ``execute``) BEFORE the value leaves this function, so a crash mid-response
    leaves a record of a reveal that may have been seen, never a disclosure with
    no record. The read that precedes it discloses nothing — its result does not
    leave this function unless the gates pass.

    **A refused reveal writes NOTHING**, matching ``reveal_resume``'s discipline
    for a scope-blocked reveal. An audit trail that records reads which never
    happened is not a trail an auditor can rely on.

    Three refusals, and each is a different fact:

    1. *No attributable session* -> 403, before anything is read.
       ``require_session_role`` already 403s on ``user is None`` since D2 =
       option B; the explicit check here is what makes the guarantee local and
       readable, and it is the whole basis of C over B — a reveal a bare service
       key could perform would launder an unattributable read through a route
       whose only justification is that it records one.
    2. *No such row* -> 404.
    3. *The row's action is not revealable, or it holds no withheld object*
       -> 403. Fail-closed via ``audit_service.is_revealable_action``: this
       route must not become a general un-redactor that every future ``details``
       writer inherits. ``details`` is ``jsonb``, so a legacy row may hold a
       scalar, a list, or nothing — none of which is a withheld value to reveal.

    Gated on the CAS SESSION role alone, for the two reasons ``GET /audit/log``
    above documents at length: attributability, and reachability behind the BFF's
    one fixed ``recruiter`` key.
    """
    if user is None:
        raise HTTPException(
            status_code=403,
            detail="revealing an audit detail requires an attributable human session",
        )

    detail = await audit_service.read_audit_detail(db, audit_id=audit_id)
    if detail is None:
        raise NotFoundError(f"audit row {audit_id} not found", audit_id=str(audit_id))

    if (
        not audit_service.is_revealable_action(detail.action)
        or not isinstance(detail.details, dict)
        # An empty object is "nothing was recorded", not "something is being
        # kept from you" — revealing it would write an audit row asserting a
        # disclosure that did not occur.
        or not detail.details
    ):
        raise HTTPException(
            status_code=403,
            detail=f"no revealable detail is recorded for action '{detail.action}'",
        )

    actor_kind, actor_user_id, actor_service = actor_fields_from_user(user)
    await audit_service.record_audit(
        db,
        actor_kind=actor_kind,
        actor_user_id=actor_user_id,
        actor_service=actor_service,
        action="reveal_audit_detail",
        subject_type="audit_log",
        subject_id=audit_id,
        context=context,
        details={"revealed_action": detail.action},
    )
    return detail


__all__ = ["router"]
