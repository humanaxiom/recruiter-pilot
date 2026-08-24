"""Transactional outbox writes.

The worker writes an outbox row in the SAME transaction as the state change it
describes (the ``jobs``/``resumes`` write-back). A separate drainer projects
those rows into Neo4j and stamps ``delivered_at``. That drainer is Phase 4 — in
Phase 3 the rows simply accumulate undelivered, which is the outbox pattern
working as intended, not a dropped requirement.

The corollary, enforced by the callers: a write-back that did NOT apply (the
optimistic-concurrency guard returned ``False``) must NOT enqueue anything. An
event for a write that never landed would project stale state into the graph.

Deviation from hris: no ``RETURNING id``. Nothing in this project consumes the
outbox row id, and ``execute`` keeps the call a single round trip.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID

from src.services import DbConn

logger = logging.getLogger(__name__)

_ENQUEUE_SQL = """
INSERT INTO outbox (aggregate, aggregate_id, event_type, payload)
VALUES ($1, $2, $3, $4::jsonb)
"""


async def enqueue_outbox(
    conn: DbConn,
    *,
    aggregate: str,
    aggregate_id: UUID,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    """Append one outbox row.

    The caller is responsible for running this inside the transaction that also
    writes the corresponding state change. ``default=str`` keeps a stray UUID or
    datetime in the payload from blowing up the JSON encode.
    """
    await conn.execute(
        _ENQUEUE_SQL,
        aggregate,
        aggregate_id,
        event_type,
        json.dumps(payload, default=str),
    )
    logger.debug(
        "outbox.enqueued aggregate=%s aggregate_id=%s event_type=%s",
        aggregate,
        aggregate_id,
        event_type,
    )
