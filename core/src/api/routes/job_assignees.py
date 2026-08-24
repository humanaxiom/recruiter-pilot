"""Per-job assignment routes (ADR-020 §2, FU-6 slice 3).

``router`` is mounted with absolute paths (no router-level ``prefix``),
matching every other Phase-6 router's convention.

**Who may assign (ADR-020 §2).** Only ``admin`` and ``recruiter`` may create
or remove an assignment — ``require_role(Role.ADMIN, Role.RECRUITER)`` gates
BOTH routes. "Auditor credentials may never assign; assignment is an
operational act, not an audit act." (ADR-020 §2, verbatim.)

**The real-assigner gate (the FU-5-forced wrinkle).** ``job_assignees
.assigned_by`` and ``audit_log.actor_user_id`` are both NOT-NULL FKs to a
real ``users`` row. ``resolve_user`` can resolve three things: a real human
session, ``None`` (no session at all), or the CAS-disabled synthetic
dev-admin sentinel (``_DEV_ADMIN_SENTINEL_ID`` — not a real ``users`` row).
Only the first is usable as ``assigned_by``/``actor_user_id`` here, so both
routes 403 on the other two cases, BEFORE the assign/unassign service call or
the audit write — mirroring ``resumes.reveal_resume``'s human-attribution
gate, but stricter: reveal tolerates the sentinel (attributing it to
``actor_kind='service'``); this slice cannot, because the sentinel is not a
legal ``assigned_by`` FK target at all.

**Atomicity (mirrors ``jobs.update_job``'s ``blind_review`` flip, ADR-019
§1.4).** The assign/unassign write and the paired audit write run inside ONE
``conn.transaction()`` each — a crash or exception in the audit write must
roll back the assignment (or unassignment) too, so an assigned-but-unaudited
(or unassigned-but-unaudited) row is never observable.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status

from src.api.deps import (
    _DEV_ADMIN_SENTINEL_ID,
    Role,
    require_role,
    require_session_role,
    resolve_user,
)
from src.errors import NotFoundError
from src.models.pool import Db
from src.schemas.auth import User
from src.schemas.jobs import JobAssigneeCreate
from src.services import audit_service, job_assignee_service

router = APIRouter()

# ADR-020 §2 — only admin/recruiter may assign or unassign. Auditor and
# hiring_manager get 403 on both routes below.
_ASSIGNERS: tuple[Role, ...] = (Role.ADMIN, Role.RECRUITER)


def _require_real_assigner(user: User | None) -> User:
    """Enforce the real-assigner gate described in the module docstring.

    Raises 403 for ``user is None`` (no session resolves) OR ``user.id ==
    _DEV_ADMIN_SENTINEL_ID`` (the CAS-disabled synthetic identity) — neither
    is a legal ``assigned_by``/``actor_user_id`` FK target. Otherwise returns
    the real, attributable :class:`User`.
    """
    if user is None or user.id == _DEV_ADMIN_SENTINEL_ID:
        raise HTTPException(
            status_code=403,
            detail="assignment requires an attributable human session",
        )
    return user


@router.post(
    "/jobs/{job_id}/assignees",
    status_code=status.HTTP_201_CREATED,
    dependencies=[
        Depends(require_role(*_ASSIGNERS)),
        Depends(require_session_role(*_ASSIGNERS)),
    ],
)
async def create_assignee(
    job_id: UUID,
    payload: JobAssigneeCreate,
    db: Db,
    user: Annotated[User | None, Depends(resolve_user)],
) -> None:
    """Assign ``payload.user_id`` to ``job_id``.

    Runs ``job_assignee_service.assign`` THEN
    ``audit_service.record_audit`` (action ``"assign_job"``) inside a single
    ``conn.transaction()`` — see the module docstring for why this must be
    atomic.
    """
    actor = _require_real_assigner(user)
    async with db.transaction():
        await job_assignee_service.assign(
            db, job_id, payload.user_id, assigned_by=actor.id
        )
        await audit_service.record_audit(
            db,
            actor_kind="user",
            actor_user_id=actor.id,
            actor_service=None,
            action="assign_job",
            subject_type="job",
            subject_id=job_id,
            job_id=job_id,
            details={"user_id": str(payload.user_id), "note": payload.note},
        )


@router.delete(
    "/jobs/{job_id}/assignees/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[
        Depends(require_role(*_ASSIGNERS)),
        Depends(require_session_role(*_ASSIGNERS)),
    ],
)
async def delete_assignee(
    job_id: UUID,
    user_id: UUID,
    db: Db,
    user: Annotated[User | None, Depends(resolve_user)],
) -> None:
    """Remove the ``(job_id, user_id)`` assignment, if any.

    ``job_assignee_service.unassign`` reports whether a row was actually
    removed. A no-op (nothing to remove) raises ``NotFoundError`` (404)
    BEFORE the audit write — a no-op DELETE gets no ``unassign_job`` audit
    row. A real removal writes the audit row inside the SAME
    ``conn.transaction()`` as the DELETE (same atomicity guarantee as
    ``create_assignee`` above)."""
    actor = _require_real_assigner(user)
    async with db.transaction():
        removed = await job_assignee_service.unassign(db, job_id, user_id)
        if not removed:
            raise NotFoundError(
                f"assignment for job {job_id} / user {user_id} not found",
                job_id=str(job_id),
                user_id=str(user_id),
            )
        await audit_service.record_audit(
            db,
            actor_kind="user",
            actor_user_id=actor.id,
            actor_service=None,
            action="unassign_job",
            subject_type="job",
            subject_id=job_id,
            job_id=job_id,
            details={"user_id": str(user_id)},
        )


__all__ = ["router"]
