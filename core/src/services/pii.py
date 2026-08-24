"""Session-keyed PII encryption via pgcrypto.

The PII columns on ``resumes`` (``candidate_name``/``candidate_email``/
``candidate_phone``/``cover_letter_text``) are ``BYTEA`` holding the output of
``pgp_sym_encrypt(plaintext, app.pii_key)``. The key is SET per-transaction
(``SET LOCAL app.pii_key``, spelled ``set_config(..., is_local => true)``) so
that a pooled connection handed to a later request never carries the key into
that request's session.

Public helpers:

* ``set_pii_key(conn)``   — SET LOCAL the key on ``conn`` for this transaction
* ``encrypt(conn, s)``    — ``pgp_sym_encrypt``, or ``None`` for ``None`` input
* ``decrypt(conn, b)``    — ``pgp_sym_decrypt``, or ``None`` for ``None`` input
* ``email_hash(email)``   — sha256 of the lowercased/stripped email (PLAINTEXT)

Encryption happens SQL-side via ``fetchval``, never in Python: the raw key
stays in the database server's memory and this process only ever holds
ciphertext bytes.

Two invariants that must survive every future edit:

1. **The key comes from settings, never from a file.** hris resolved a
   host-mounted secrets file; this project models the key as an env string
   (``Settings.pii_key``, required as ``PII_KEY`` by compose). Two ways to
   configure one secret is how a NULL key ships.
2. **``current_setting('app.pii_key')`` is called with ONE argument.** It is a
   strict read: when the GUC was never set it RAISES. Do NOT add the tolerant
   second argument — it would make the read return NULL instead, and because
   ``pgp_sym_encrypt`` is a STRICT function, ``pgp_sym_encrypt(plaintext, NULL)``
   returns NULL as well. The PII column would be silently written as NULL and
   the plaintext lost forever, with every gate green. It must fail loud.

``email_hash`` is deliberately NOT encrypted: a plaintext hash is what lets a
PIPEDA subject-access request find a candidate by email without the key.
"""

from __future__ import annotations

import hashlib

from src.services import DbConn
from src.settings import get_settings

_SET_KEY_SQL = "SELECT set_config('app.pii_key', $1, true)"
_ENCRYPT_SQL = "SELECT pgp_sym_encrypt($1::text, current_setting('app.pii_key'))::bytea"
_DECRYPT_SQL = "SELECT pgp_sym_decrypt($1::bytea, current_setting('app.pii_key'))"


async def set_pii_key(conn: DbConn) -> None:
    """SET LOCAL ``app.pii_key`` on the current transaction.

    Must be called inside an open ``conn.transaction()`` block: SET LOCAL is
    transaction-scoped, and outside one Postgres raises "SET LOCAL can only be
    used in transaction blocks".

    ``set_config(name, value, is_local)`` is the parameterisable equivalent of
    ``SET LOCAL``, so the key value is bound rather than escaped into SQL text.
    """
    await conn.execute(_SET_KEY_SQL, get_settings().pii_key)


async def encrypt(conn: DbConn, plaintext: str | None) -> bytes | None:
    """Return ``pgp_sym_encrypt(plaintext, app.pii_key)::bytea``, or ``None``.

    A ``None`` plaintext short-circuits WITHOUT touching the connection — an
    absent optional field (no phone on the résumé) is not a missing-key error.
    A non-None plaintext under a transaction that never called ``set_pii_key``
    raises ``asyncpg.PostgresError``, by design.
    """
    if plaintext is None:
        return None
    result: bytes = await conn.fetchval(_ENCRYPT_SQL, plaintext)
    return result


async def decrypt(conn: DbConn, ciphertext: bytes | None) -> str | None:
    """Return ``pgp_sym_decrypt(ciphertext, app.pii_key)``, or ``None``.

    The caller's transaction must have called ``set_pii_key`` first; if it has
    not, this raises 'unrecognized configuration parameter "app.pii_key"'.
    """
    if ciphertext is None:
        return None
    result: str = await conn.fetchval(_DECRYPT_SQL, ciphertext)
    return result


def email_hash(email: str | None) -> str | None:
    """Plaintext sha256(lower(strip(email))) for subject-access lookup.

    NOT encrypted — see the ``resumes.candidate_email_hash`` note in
    ``src/models/ddl.py``. Returns ``None`` for a null/blank input so the
    partial index on the column excludes the row.
    """
    if not email:
        return None
    normalised = email.strip().lower()
    if not normalised:
        return None
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()
