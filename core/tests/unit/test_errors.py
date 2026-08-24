"""Unit tests — ``src.errors.ConflictError`` (user-admin-roles slice 6), the
409 the last-admin-lockout guard raises on ``PATCH /users/{id}/role``.

``ConflictError`` does not exist yet in ``core/src/errors.py`` — every test
below fails at import (``ImportError``), RED half of the TDD cycle.

Mirrors ``NotFoundError``'s own shape one class up: a plain ``AppError``
subclass with a fixed ``code``/``status``, no bespoke ``__init__`` override.
"""

from __future__ import annotations

import pytest

from src.errors import AppError


def test_conflict_error_is_an_app_error() -> None:
    from src.errors import ConflictError

    assert issubclass(ConflictError, AppError)


def test_conflict_error_status_is_409() -> None:
    from src.errors import ConflictError

    assert ConflictError.status == 409


def test_conflict_error_has_a_non_default_error_code() -> None:
    """Distinct from the base ``AppError.code`` ("app.error") and from
    ``NotFoundError``'s ("resource.not_found") — a caller (or the FastAPI
    exception-handler body) must be able to tell a lockout conflict apart
    from a generic error or a 404 by code alone."""
    from src.errors import ConflictError, NotFoundError

    assert ConflictError.code != AppError.code
    assert ConflictError.code != NotFoundError.code


def test_conflict_error_carries_the_message_and_context() -> None:
    from src.errors import ConflictError

    err = ConflictError("cannot demote the last active admin", user_id="abc-123")
    assert err.message == "cannot demote the last active admin"
    assert err.context == {"user_id": "abc-123"}


def test_conflict_error_str_is_the_message() -> None:
    from src.errors import ConflictError

    err = ConflictError("cannot demote the last active admin")
    assert str(err) == "cannot demote the last active admin"


def test_conflict_error_raises_and_is_catchable_as_app_error() -> None:
    from src.errors import ConflictError

    with pytest.raises(AppError):
        raise ConflictError("conflict")


def test_conflict_error_context_defaults_to_empty() -> None:
    from src.errors import ConflictError

    err = ConflictError("conflict, no extra context")
    assert err.context == {}


__all__: list[str] = []
