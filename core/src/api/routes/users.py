"""user-admin-roles slice 5/6 — ``GET /users``, an admin-only listing of every
``users`` row, and ``PATCH /users/{user_id}/role``, the admin role-assignment
route (the merge-blocking payoff slice), with a ``role_changed`` audit row and
a last-admin-lockout guard.

**The core design decision this module pins (planner decision #3).** The gate
is on the CAS SESSION role (``resolve_user().role == "admin"``), NOT the
API-key role (``resolve_role``/``require_role``). The Flask viewer sends the
ONE shared ``recruiter`` API key for every browser user
(``core/frontend/api_client.py``) — a key-role gate would 403 every real admin
browsing through that shared key, and would ALSO let a bare service/admin KEY
with no verifiable session list every user, which is exactly the "is a real
human" gap ``job_assignees._require_real_assigner`` (ADR-020 §2, FU-6 slice 3)
left open by only checking "a real human session resolves" and never checking
that session's ROLE. ``_require_admin_session`` closes both gaps in one gate:
``user is not None and user.role == "admin"`` — no key is consulted at all.

Deliberately NOT added to the ``require_role_assigned`` guard in
``src.api.main`` — ``_require_admin_session`` is stricter (a no-role session
is already 403'd by it alone), so stacking the two would be redundant.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException

from src.api.deps import actor_fields_from_user, resolve_user
from src.errors import ConflictError, NotFoundError
from src.models.pool import Db
from src.schemas.auth import RoleAssignment, User
from src.services import audit_service, user_service

router = APIRouter()


async def _require_admin_session(
    user: Annotated[User | None, Depends(resolve_user)],
) -> User:
    """403 unless ``user`` is a real, ACTIVE session with ``role == "admin"``
    — the CAS-disabled synthetic dev-anonymous sentinel (``role="admin"``,
    ``active=True``) also passes, exactly like
    ``require_role_assigned``/``scoped_user_id_or_403`` treat it.

    **F5 (security finding, ``fix/auth-boundary-fails-open``).** A
    deactivated admin session (``user.active is False``) also 403s, same
    mechanism — deactivation must actually revoke authority, not just be
    cosmetic while ``session_service.refresh_if_needed`` keeps sliding the
    session's expiry forward.
    """
    if user is None or user.role != "admin" or not user.active:
        raise HTTPException(
            status_code=403, detail="admin session required for this route"
        )
    return user


@router.get("/users")
async def list_users(
    db: Db,
    _admin: Annotated[User, Depends(_require_admin_session)],
) -> list[User]:
    return await user_service.list_users(db)


@router.patch("/users/{user_id}/role")
async def set_user_role(
    user_id: UUID,
    payload: RoleAssignment,
    db: Db,
    acting_admin: Annotated[User, Depends(_require_admin_session)],
) -> User:
    """An admin session assigns ``user_id`` a new role.

    Gated by the same ``_require_admin_session`` as ``GET /users`` (the CAS
    session role, never an API key). ATOMIC — the role ``UPDATE`` and the
    ``role_changed`` audit row run inside ONE ``conn.transaction()``, mirroring
    ``job_service.update_job``'s ``blind_review``-flip pattern and
    ``job_assignees``'s assign/unassign atomicity: a crash or exception in the
    audit write must roll back the role change too, so a
    changed-but-unaudited row is never observable.

    LAST-ADMIN LOCKOUT (SECURITY-CRITICAL): if the target is currently
    ``role='admin'`` AND the new role is not ``'admin'`` AND exactly one
    active admin exists, this raises ``ConflictError`` (409) BEFORE the
    ``UPDATE``/audit write — the last active admin can never demote
    themselves (or be demoted) into a state where nobody can administer the
    system. No audit row is written for this rejected attempt, mirroring
    ``job_assignees``'s "a no-op gets no audit row" discipline.
    """
    async with db.transaction():
        target = await user_service.get_by_id(db, user_id)
        if target is None:
            raise NotFoundError(f"user {user_id} not found", user_id=str(user_id))

        new_role = payload.role.value
        if target.role == "admin" and new_role != "admin":
            if await user_service.count_active_admins(db) == 1:
                raise ConflictError(
                    "cannot demote the last active admin", user_id=str(user_id)
                )

        await user_service.set_role(db, user_id, new_role)

        actor_kind, actor_user_id, actor_service = actor_fields_from_user(acting_admin)
        await audit_service.record_audit(
            db,
            actor_kind=actor_kind,
            actor_user_id=actor_user_id,
            actor_service=actor_service,
            action="role_changed",
            subject_type="user",
            subject_id=user_id,
            details={"old_role": target.role, "new_role": new_role},
        )

    return target.model_copy(update={"role": new_role})


__all__ = ["router"]
