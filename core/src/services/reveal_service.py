"""FU-1 — the append-only audit sink behind the AUDITED candidate-reveal action.

Revealing a candidate's identity is the de-anonymization action, so every reveal
must leave an audit trail. :func:`record_reveal` is a PURE insert: it writes one
``reveal_audit`` row and returns its id. No decryption happens here (that is
``resume_service.get_one``'s job) — this module never touches PII.

The id is app-minted (like the résumé-upload path) so no ``RETURNING`` round trip
is needed. ``actor`` is sourced from the CAS session identity as of FU-5 slice 7
(ADR-019 §8.3/§9.2) — the resolved user's ``cas_username``, or the ``"api"``
fallback when no identity resolves.

**FU-5 slice 10 (ADR-019 §6 / §9.4).** :func:`list_reveal_audit` is the READ
side backing ``GET /audit/reveals-legacy`` — a read-only, paginated,
newest-first view of this same table, kept for historical review now that
``audit_log`` (``audit_service``) is the live sink (slice 8). Selects
``reveal_audit``'s own columns ONLY — no join against ``resumes``, so nothing
here can ever surface candidate PII.
"""

from __future__ import annotations

from uuid import UUID, uuid4

from src.schemas.audit import RevealAuditItem
from src.services import DbConn

_INSERT_SQL = """
INSERT INTO reveal_audit (id, resume_id, job_id, actor, context)
VALUES ($1, $2, $3, $4, $5)
"""


async def record_reveal(
    conn: DbConn,
    *,
    resume_id: UUID,
    actor: str | None,
    job_id: UUID | None = None,
    context: str | None = None,
) -> UUID:
    """Write one append-only ``reveal_audit`` row and return its (app-minted) id.

    A pure insert — no ``set_pii_key`` / ``pgp_sym_decrypt``, no read-back. Only
    ``resume_id`` is mandatory; ``job_id`` / ``actor`` / ``context`` are
    nullable.
    """
    audit_id = uuid4()
    await conn.execute(_INSERT_SQL, audit_id, resume_id, job_id, actor, context)
    return audit_id


_LIST_SQL = """
SELECT id, resume_id, job_id, actor, context, revealed_at
FROM reveal_audit
ORDER BY revealed_at DESC
LIMIT $1 OFFSET $2
"""


async def list_reveal_audit(
    conn: DbConn, *, limit: int = 100, offset: int = 0
) -> list[RevealAuditItem]:
    """Read-only, paginated, newest-first view of ``reveal_audit`` — the
    frozen table's own columns, nothing joined in. No PII decryption, no
    ``resumes`` join: see the module docstring."""
    rows = await conn.fetch(_LIST_SQL, limit, offset)
    return [
        RevealAuditItem(
            id=r["id"],
            resume_id=r["resume_id"],
            job_id=r["job_id"],
            actor=r["actor"],
            context=r["context"],
            revealed_at=r["revealed_at"],
        )
        for r in rows
    ]
