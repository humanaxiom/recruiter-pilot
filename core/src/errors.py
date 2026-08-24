"""Application-typed errors for the service layer.

Phase 5 adds the read paths (résumé / shortlist), which raise
``NotFoundError`` when an id does not resolve. Ported minimal from hris
``apps/api/src/api/errors.py`` — just the base ``AppError`` and
``NotFoundError``. The FastAPI envelope + exception handlers land with the
routes in Phase 6; the service layer only needs the typed exception here.
"""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    """Base for application-typed errors. Subclass per error class.

    ``**context`` carries structured detail (e.g. the missing id) alongside the
    human message so a later boundary can render it without re-parsing the text.
    """

    code: str = "app.error"
    status: int = 500

    def __init__(self, message: str, **context: Any) -> None:
        super().__init__(message)
        self.message = message
        self.context = context


class NotFoundError(AppError):
    """A requested resource (résumé, shortlist entry, …) does not exist."""

    code = "resource.not_found"
    status = 404


class FileRejectedError(AppError):
    """An upload is rejected at the route boundary before any body is read —
    e.g. too many files in one multipart batch (memory-exhaustion guard)."""

    code = "upload.rejected"
    status = 413


class ConflictError(AppError):
    """The request is well-formed and authorized but conflicts with the
    resource's current state — e.g. ``PATCH /users/{id}/role`` demoting the
    last active admin (user-admin-roles slice 6)."""

    code = "resource.conflict"
    status = 409
