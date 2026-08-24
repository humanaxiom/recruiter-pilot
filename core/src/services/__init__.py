"""Service layer — the SQL that the worker (and later the API) writes through.

Raw asyncpg, hand-written SQL, no ORM. Every function here takes an open
``asyncpg.Connection`` and leaves transaction scoping to the caller: the
worker wraps the write-back + the outbox INSERT in ONE transaction so a
parsed row and its projection event commit atomically.

Three modules in Phase 3:

* ``pii`` — pgcrypto encrypt/decrypt under the transaction-scoped
  ``app.pii_key`` GUC, plus the plaintext ``email_hash``.
* ``job_service`` / ``resume_service`` — the worker write-back functions
  (``record_parsed`` / ``record_parse_failure``), each guarded by an
  optimistic-concurrency WHERE clause on the source status.
* ``outbox_service`` — append-only event enqueue, drained to Neo4j in Phase 4.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, TypeAlias

if TYPE_CHECKING:
    import asyncpg
    from asyncpg import Record
    from asyncpg.pool import PoolConnectionProxy

# Every caller of these services holds a POOLED connection: the worker does
# ``async with ctx["pg_pool"].acquire() as conn``, and the API's ``get_db``
# dependency yields the same thing. asyncpg's stubs type that as
# ``PoolConnectionProxy[Record]``, which is NOT a subclass of
# ``Connection[Record]`` — so a service annotated with the bare Connection
# would force every call site to cast. Accept both instead; the two share the
# execute/fetchval surface these modules use.
#
# The alias is a STRING because ``PoolConnectionProxy`` is generic only in the
# stubs and is not subscriptable at runtime (see ``src/models/pool.py``).
DbConn: TypeAlias = "asyncpg.Connection[Record] | PoolConnectionProxy[Record]"
