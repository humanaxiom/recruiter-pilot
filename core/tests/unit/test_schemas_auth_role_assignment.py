"""Unit tests — user-admin-roles slice 6, ``src.schemas.auth.RoleAssignment``
(``PATCH /users/{id}/role``'s request body).

``RoleAssignment`` does not exist yet in ``core/src/schemas/auth.py`` — every
test below fails at import (``ImportError``) or construction
(``AttributeError``/validation), RED half of the TDD cycle.

Design contract this file pins (task's THE SURFACE section):

* ``RoleAssignment(role: Role)`` — ``Role`` is the closed
  ``src.api.deps.Role`` enum (admin/recruiter/hiring_manager/auditor), so an
  unknown role string is a 422/``ValidationError``.
* ``{"role": null}`` is ALSO invalid — this endpoint only ever ASSIGNS a real
  role, it never clears one to null (unlike ``users.role`` itself, which is
  nullable at the DB/``User`` schema level for an unset first-login).
* ``extra="forbid"`` mirrors every other schema in this module (``User``/
  ``Session``) — an unexpected extra field is rejected, not silently dropped.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.api.deps import Role

_VALID_ROLES: tuple[str, ...] = (
    "admin",
    "recruiter",
    "hiring_manager",
    "auditor",
)


@pytest.mark.parametrize("role", _VALID_ROLES)
def test_role_assignment_accepts_every_valid_role(role: str) -> None:
    from src.schemas.auth import RoleAssignment

    assignment = RoleAssignment(role=Role(role))  # type: ignore[call-arg]
    assert assignment.role == role


@pytest.mark.parametrize("role", _VALID_ROLES)
def test_role_assignment_accepts_the_role_as_a_plain_string(role: str) -> None:
    """A JSON body only ever carries plain strings — pydantic must coerce a
    bare wire string into the ``Role`` enum member, not require the caller to
    construct the enum in Python."""
    from src.schemas.auth import RoleAssignment

    assignment = RoleAssignment(role=role)  # type: ignore[arg-type]
    assert assignment.role == role


def test_role_assignment_rejects_an_unknown_role_string() -> None:
    from src.schemas.auth import RoleAssignment

    with pytest.raises(ValidationError):
        RoleAssignment(role="superuser")  # type: ignore[arg-type]


def test_role_assignment_rejects_null_role() -> None:
    """This endpoint only ASSIGNS a real role — it never clears one to null,
    unlike the nullable ``users.role`` column itself."""
    from src.schemas.auth import RoleAssignment

    with pytest.raises(ValidationError):
        RoleAssignment(role=None)  # type: ignore[arg-type]


def test_role_assignment_rejects_an_empty_string_role() -> None:
    from src.schemas.auth import RoleAssignment

    with pytest.raises(ValidationError):
        RoleAssignment(role="")  # type: ignore[arg-type]


def test_role_assignment_requires_the_role_field() -> None:
    from src.schemas.auth import RoleAssignment

    with pytest.raises(ValidationError):
        RoleAssignment()  # type: ignore[call-arg]


def test_role_assignment_rejects_unexpected_extra_fields() -> None:
    """Mirrors ``User``/``Session``'s ``extra="forbid"`` discipline in this
    same module."""
    from src.schemas.auth import RoleAssignment

    with pytest.raises(ValidationError):
        RoleAssignment(role="admin", is_super=True)  # type: ignore[call-arg]


def test_role_assignment_is_case_sensitive() -> None:
    """No case-folding surprise that would silently widen the role
    vocabulary — matches ADR-019's default-admin comparison discipline."""
    from src.schemas.auth import RoleAssignment

    with pytest.raises(ValidationError):
        RoleAssignment(role="Admin")  # type: ignore[arg-type]


__all__: list[str] = []
