"""CAS user provisioning (FU-5 slice 5, ADR-019 §10 step 3, §10a).

Ported from hris ``apps/api/src/api/services/user_service.py``, adapted to
this repo's schema: ``role`` is a plain column on ``users`` (no
``user_roles`` join table), so the default role is written directly on the
INSERT rather than a second statement against a roles table, and the
``ON CONFLICT`` (second-login) path must never touch it.

ADR-019 §10a — the default-admin allowlist: on a user's FIRST login only, a
``cas_username`` that matches the caller-supplied ``default_admin_cas_username``
is provisioned ``role='admin'`` instead of the non-default-admin default. This
is decided in plain Python BEFORE the INSERT and only takes effect on row
creation — the Postgres ``(xmax = 0)`` trick tells us whether THIS statement
created the row (never re-promoting/re-applying on a later login, including
one that raced a concurrent first login for the same username down to a
single row).

``default_admin_cas_username`` is a required keyword argument, never read
from ``settings`` inside this module — CLAUDE.md's "config only via
settings.py, never scattered" rule; the caller (the CAS callback route) is
the one place settings are read.

**user-admin-roles slice 2 — human-directed reversal of ADR-019 §10a/§2
(2026-07-25).** A first CAS login for a username other than the configured
default-admin now captures NO role (``role IS NULL``) instead of the old
``'recruiter'`` default — ``_DEFAULT_ROLE`` is retired. The default-admin
allowlist itself (``asalah`` -> ``'admin'`` on first login) and the
``ON CONFLICT`` role-sticky behaviour are UNCHANGED. Slice 3 adds the
fail-closed gate that rejects a no-role user from doing anything; a no-role
user existing but not yet blocked is this slice's intentional, known
intermediate state.
"""

from __future__ import annotations

import logging
from uuid import UUID

from src.schemas.auth import User
from src.services import DbConn

logger = logging.getLogger(__name__)

_ADMIN_ROLE = "admin"

_GET_BY_ID_SQL = """
SELECT id, cas_username, display_name, email, role, active, created_at,
       last_seen_at
FROM users WHERE id = $1
"""

# On conflict, refresh last_seen_at and (only when a non-null value was
# supplied) display_name/email. ``role`` is deliberately absent from the SET
# clause on the conflict path — a role changed out-of-band (e.g. by an
# admin) must survive every subsequent login, including one by the
# default-admin username that was since demoted.
_PROVISION_SQL = """
INSERT INTO users (cas_username, display_name, email, role)
VALUES ($1, $2, $3, $4)
ON CONFLICT (cas_username) DO UPDATE
    SET last_seen_at = now(),
        display_name = COALESCE(EXCLUDED.display_name, users.display_name),
        email = COALESCE(EXCLUDED.email, users.email)
RETURNING id, cas_username, display_name, email, role, active, created_at,
          last_seen_at, (xmax = 0) AS was_created
"""


async def provision_or_get(
    conn: DbConn,
    *,
    cas_username: str,
    display_name: str | None = None,
    email: str | None = None,
    default_admin_cas_username: str,
) -> User:
    """Return the user for ``cas_username``, creating them on first sight.

    Race-safe via ``ON CONFLICT``: concurrent first logins for the same
    unknown username collapse to exactly one row (the ``xmax = 0`` trick
    identifies which statement — if any — actually inserted).
    """
    role = _ADMIN_ROLE if cas_username == default_admin_cas_username else None
    row = await conn.fetchrow(
        _PROVISION_SQL,
        cas_username,
        display_name,
        email,
        role,
    )
    assert row is not None
    data = dict(row)
    was_created = data.pop("was_created", None)
    user = User(**data)
    if was_created:
        logger.info(
            "user.provisioned cas_username=%s user_id=%s role=%s",
            cas_username,
            user.id,
            user.role,
        )
    return user


async def get_by_id(conn: DbConn, user_id: UUID) -> User | None:
    """Look up one ``users`` row by id, or ``None`` if it does not exist.

    Used by ``GET /auth/cas/user`` and ``src.api.deps.resolve_user`` to turn
    an already-resolved session's ``user_id`` into the full row.
    """
    row = await conn.fetchrow(_GET_BY_ID_SQL, user_id)
    return User(**dict(row)) if row is not None else None


_LIST_USERS_SQL = """
SELECT id, cas_username, display_name, email, role, active, created_at,
       last_seen_at
FROM users ORDER BY created_at
"""


async def list_users(conn: DbConn) -> list[User]:
    """Return every ``users`` row, ordered by ``created_at`` (user-admin-roles
    slice 5) — the ``GET /users`` admin-listing backend.

    A plain pass-through: one ``SELECT`` (no N+1), no re-sorting/filtering of
    whatever ``conn.fetch`` hands back, mapping each row straight into a
    :class:`~src.schemas.auth.User` (``role`` may be ``None``).
    """
    rows = await conn.fetch(_LIST_USERS_SQL)
    return [User(**dict(row)) for row in rows]


_SET_ROLE_SQL = "UPDATE users SET role = $2 WHERE id = $1"


async def set_role(conn: DbConn, user_id: UUID, role: str) -> None:
    """Assign ``role`` to ``users.id = user_id`` (user-admin-roles slice 6) —
    the ``PATCH /users/{id}/role`` backend.

    A plain ``.execute``-and-forget UPDATE, mirroring
    ``job_assignee_service.assign``'s own shape: no ``RETURNING`` clause, the
    caller (the route) already holds the pre-update row (from ``get_by_id``)
    and does not need one back here. The caller is responsible for running
    this inside the SAME ``conn.transaction()`` as the paired
    ``audit_service.record_audit`` call — this function does not open one
    itself.
    """
    await conn.execute(_SET_ROLE_SQL, user_id, role)


_COUNT_ACTIVE_ADMINS_SQL = "SELECT count(*) FROM users WHERE role = 'admin' AND active"


async def count_active_admins(conn: DbConn) -> int:
    """Count currently-active ``role='admin'`` users — the last-admin-lockout
    guard's arithmetic (user-admin-roles slice 6).

    A fixed, parameter-free query: nothing caller-supplied is bound into it.
    """
    count: int = await conn.fetchval(_COUNT_ACTIVE_ADMINS_SQL)
    return count
