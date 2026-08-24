"""Postgres session-scoped advisory-lock wrapper used to dedupe concurrent
``shortlist_job`` / ``reverse_match_job`` runs for the same entity
(ADR-010 §1 residual: "No advisory lock on concurrent duplicate enqueues").

Two-int advisory-lock key: ``(classid, objid)``.

* ``classid`` is a per-``kind`` namespace (see ``_CLASS_ID``) so a job id and
  a résumé id that happen to collide on the low 32 bits never fight over the
  same lock.
* ``objid`` is derived deterministically from the entity's UUID by taking the
  low 32 bits of ``entity_id.int`` and reinterpreting them as a signed
  Postgres ``int4`` (Postgres's own wraparound), via ``_entity_to_objid``.

Advisory locks acquired with ``pg_try_advisory_lock`` are SESSION-scoped: they
are held by the physical backend connection until explicitly released with
``pg_advisory_unlock`` (or the session ends). Callers MUST release in a
``finally`` on every path that successfully acquired the lock — a leaked
session-level lock on a pooled connection wedges that job type until the
connection happens to be recycled.
"""

from __future__ import annotations

from typing import Final
from uuid import UUID

from src.services import DbConn

_CLASS_ID: Final[dict[str, int]] = {
    "shortlist": 1,
    "reverse_match": 2,
}

_TRY_LOCK_SQL: Final[str] = "SELECT pg_try_advisory_lock($1, $2)"
_UNLOCK_SQL: Final[str] = "SELECT pg_advisory_unlock($1, $2)"


def _entity_to_objid(entity_id: UUID) -> int:
    """Low 32 bits of ``entity_id.int``, reinterpreted as a signed int4."""
    v = entity_id.int & 0xFFFFFFFF
    return v - 2**32 if v >= 2**31 else v


def _classid(kind: str) -> int:
    try:
        return _CLASS_ID[kind]
    except KeyError as exc:
        raise ValueError(f"unknown job-lock kind: {kind!r}") from exc


async def try_job_lock(conn: DbConn, kind: str, entity_id: UUID) -> bool:
    """Attempt to acquire the session-scoped advisory lock for ``(kind,
    entity_id)``. Returns ``True`` if acquired, ``False`` if already held by
    another session."""
    classid = _classid(kind)
    result = await conn.fetchval(_TRY_LOCK_SQL, classid, _entity_to_objid(entity_id))
    return bool(result)


async def release_job_lock(conn: DbConn, kind: str, entity_id: UUID) -> None:
    """Release the session-scoped advisory lock for ``(kind, entity_id)``."""
    classid = _classid(kind)
    await conn.execute(_UNLOCK_SQL, classid, _entity_to_objid(entity_id))
