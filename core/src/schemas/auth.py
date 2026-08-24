"""Pydantic schemas for the CAS identity/session domain (FU-5 slice 5,
ADR-019 §10).

Two shapes, mirroring the ``users`` / ``sessions`` DDL in
``src/models/ddl.py`` column-for-column:

* ``User`` — ``role`` is a plain string column here (NOT the hris
  ``user_roles`` join-table tuple), and this schema carries ``active`` /
  ``last_seen_at`` in place of hris's ``status`` / ``last_login_at``.
* ``Session`` — ``id`` is the opaque ``secrets.token_urlsafe(32)`` cookie
  value (TEXT primary key, not UUID). ``ip_addr`` is modelled as
  ``str | None`` even though the column is Postgres ``INET`` — the service
  layer casts with ``::text`` on the way out so this schema never needs a
  Postgres-specific type.
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class Role(StrEnum):
    """The four keyed roles (``StrEnum``, so each member IS its wire string —
    ``Role.ADMIN == "admin"``).

    Defined HERE, not in ``src.api.deps`` (where it lived through FU-4/FU-6),
    because ``RoleAssignment`` below (user-admin-roles slice 6) needs it as a
    field type, and ``src.api.deps`` itself imports ``User`` from this same
    module — defining ``Role`` there and importing it back here would be a
    real import cycle (``src.api.deps`` <-> ``src.schemas.auth``), broken
    either way depending on which module a test imports first. ``src.api.deps``
    re-exports ``Role`` (``from src.schemas.auth import Role``) so every
    existing ``from src.api.deps import Role`` / ``deps.Role`` call site is
    unaffected.

    Role-level, NOT row-level (FU-4/D6): there is no per-job owner column, so
    a hiring-manager or auditor key grants its read access across every job
    company-wide."""

    ADMIN = "admin"
    RECRUITER = "recruiter"
    HIRING_MANAGER = "hiring_manager"
    AUDITOR = "auditor"


class User(BaseModel):
    """One row of ``users``."""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    cas_username: str
    display_name: str | None
    email: str | None
    role: str | None
    active: bool
    created_at: dt.datetime
    last_seen_at: dt.datetime


class Session(BaseModel):
    """One row of ``sessions``."""

    model_config = ConfigDict(extra="forbid")

    id: str
    user_id: UUID
    expires_at: dt.datetime
    revoked_at: dt.datetime | None
    user_agent: str | None
    ip_addr: str | None
    created_at: dt.datetime


class RoleAssignment(BaseModel):
    """``PATCH /users/{id}/role``'s request body (user-admin-roles slice 6).

    ``role`` is the closed :class:`Role` enum, so an unknown wire string is a
    422 ``ValidationError`` rather than being written verbatim. This endpoint
    only ever ASSIGNS a real role — unlike the nullable ``users.role`` column
    itself (unset on a non-default-admin's first login, ADR-019 §10a/§2
    reversal) — so ``role`` is required and non-nullable here; ``{"role":
    null}`` is rejected, it never clears a role via this route.
    """

    model_config = ConfigDict(extra="forbid")

    role: Role
