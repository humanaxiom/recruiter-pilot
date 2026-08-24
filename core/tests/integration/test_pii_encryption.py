"""Integration tests — ``src/services/pii.py`` against a REAL Postgres
(testcontainers, real ``pgcrypto``).

This is the merge-blocking PII criterion for Phase 3. ``pii.py`` sources the
encryption key from ``get_settings().pii_key`` (an env string — the
deliberate deviation from hris's key-*file* ladder), and every SQL statement
is otherwise verbatim:

    SELECT set_config('app.pii_key', $1, true)
    SELECT pgp_sym_encrypt($1::text, current_setting('app.pii_key'))::bytea
    SELECT pgp_sym_decrypt($1::bytea, current_setting('app.pii_key'))

``current_setting('app.pii_key')`` is a STRICT read with NO ``missing_ok``
argument anywhere in the module. That is the single line standing between
correct behaviour and silent, irreversible PII loss — see the docstring on
``test_encrypt_without_set_pii_key_raises_postgres_error`` below, which is
the whole point of this file.

Mocked-connection proof of the same SQL text lives in
``tests/unit/test_pii.py``; this file proves the SQL actually *works* (and
actually *fails* when it must) against a live pgcrypto extension.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator, Iterator
from types import SimpleNamespace
from unittest.mock import patch

import asyncpg
import pytest
from testcontainers.postgres import PostgresContainer

from src.models.ddl import init_schema
from src.services.pii import decrypt, encrypt, set_pii_key

# Arbitrary but realistic — not the production PII_KEY value; just needs to
# be a stable, non-empty string sourced through get_settings().pii_key.
TEST_PII_KEY = "integration-test-pii-key-do-not-use-in-prod"
WRONG_PII_KEY = "a-completely-different-and-wrong-key"

INSERT_RESUME = """
INSERT INTO resumes (
    job_id, blob_key, original_filename, mime_type,
    file_size_bytes, sha256, consent_acknowledged
) VALUES ($1, $2, 'a.pdf', 'application/pdf', 1024, $3, TRUE)
RETURNING id
"""


@pytest.fixture(scope="session")
def pg_dsn() -> Iterator[str]:
    with PostgresContainer("postgres:16-alpine") as pg:
        # asyncpg needs the raw DSN, not SQLAlchemy's postgresql+driver:// form.
        yield re.sub(r"\+\w+", "", pg.get_connection_url(), count=1)


@pytest.fixture
async def conn(pg_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    connection: asyncpg.Connection = await asyncpg.connect(pg_dsn)
    await init_schema(connection)
    await connection.execute("TRUNCATE jobs, outbox CASCADE")
    try:
        yield connection
    finally:
        await connection.close()


@pytest.fixture
def patched_pii_key() -> Iterator[None]:
    """Point ``src.services.pii.get_settings`` at a fixed test key so
    ``set_pii_key`` sources a real, deterministic value without depending on
    an env-supplied ``PII_KEY`` being present in the test environment."""
    fake_settings = SimpleNamespace(pii_key=TEST_PII_KEY)
    with patch("src.services.pii.get_settings", return_value=fake_settings):
        yield


async def _insert_job(conn: asyncpg.Connection) -> uuid.UUID:
    job_id: uuid.UUID = await conn.fetchval(
        "INSERT INTO jobs (title, description_raw) VALUES ($1, $2) RETURNING id",
        "Senior Python Engineer",
        "Build the ranking pipeline.",
    )
    return job_id


async def _insert_resume(conn: asyncpg.Connection, job_id: uuid.UUID) -> uuid.UUID:
    resume_id: uuid.UUID = await conn.fetchval(
        INSERT_RESUME,
        job_id,
        f"resumes/{uuid.uuid4().hex}.pdf",
        uuid.uuid4().hex,
    )
    return resume_id


# ── Happy path: set_pii_key -> encrypt -> decrypt roundtrip ─────────────────


@pytest.mark.asyncio
async def test_encrypt_decrypt_roundtrip_and_ciphertext_hides_plaintext(
    conn: asyncpg.Connection, patched_pii_key: None
) -> None:
    plaintext = "ada@example.org"
    async with conn.transaction():
        await set_pii_key(conn)
        ciphertext = await encrypt(conn, plaintext)
        assert isinstance(ciphertext, bytes)
        # The ciphertext bytes must NOT contain the plaintext substring —
        # proves this is real pgp_sym_encrypt output, not a pass-through.
        assert plaintext.encode() not in ciphertext
        decrypted = await decrypt(conn, ciphertext)

    assert decrypted == plaintext


# ── Raw-column read: never bytes leak as plaintext, even without the key ────


@pytest.mark.asyncio
async def test_raw_column_read_without_set_pii_key_returns_ciphertext_bytes(
    conn: asyncpg.Connection, pg_dsn: str, patched_pii_key: None
) -> None:
    """A value encrypted and stored, then read back in a FRESH connection
    (its own transaction, its own session — never called set_pii_key at
    all), must come back as raw ``bytes`` (bytea ciphertext) — never
    plaintext, and never anything that decodes to the plaintext."""
    plaintext = "ada@example.org"
    job_id = await _insert_job(conn)
    resume_id = await _insert_resume(conn, job_id)

    async with conn.transaction():
        await set_pii_key(conn)
        ciphertext = await encrypt(conn, plaintext)
    await conn.execute(
        "UPDATE resumes SET candidate_email = $2 WHERE id = $1", resume_id, ciphertext
    )

    fresh = await asyncpg.connect(pg_dsn)
    try:
        raw = await fresh.fetchval(
            "SELECT candidate_email FROM resumes WHERE id = $1", resume_id
        )
    finally:
        await fresh.close()

    assert isinstance(raw, bytes)
    assert plaintext.encode() not in raw


# ── Wrong key: proves the key is load-bearing, not decorative ───────────────


@pytest.mark.asyncio
async def test_decrypt_with_wrong_key_raises_postgres_error(
    conn: asyncpg.Connection, patched_pii_key: None
) -> None:
    """Ciphertext made under the real key, decrypted under a DIFFERENT key
    in a fresh transaction, must raise. If it instead returned garbage or
    None silently, the ``app.pii_key`` GUC would be decorative rather than
    actually gating access to plaintext PII."""
    plaintext = "ada@example.org"
    async with conn.transaction():
        await set_pii_key(conn)
        ciphertext = await encrypt(conn, plaintext)

    with pytest.raises(asyncpg.PostgresError):
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.pii_key', $1, true)", WRONG_PII_KEY
            )
            await decrypt(conn, ciphertext)


# ── THE mutation-detecting test — the whole point of this file ──────────────


@pytest.mark.asyncio
async def test_encrypt_without_set_pii_key_raises_postgres_error(
    conn: asyncpg.Connection, patched_pii_key: None
) -> None:
    """``encrypt()`` inside a transaction that has NEVER called
    ``set_pii_key`` MUST raise ``asyncpg.PostgresError`` (in practice:
    'unrecognized configuration parameter "app.pii_key"').

    Why this test exists, spelled out so a future reader can't misread it
    or "fix" it away: ``encrypt()`` reads the key via
    ``current_setting('app.pii_key')`` with NO ``missing_ok`` argument. If
    anyone ever "fixes a flaky test" by adding ``missing_ok=true`` to that
    read, ``current_setting`` silently returns ``NULL`` instead of raising
    when the GUC was never set. ``pgp_sym_encrypt`` is a STRICT SQL
    function, so ``pgp_sym_encrypt(plaintext, NULL)`` ALSO silently returns
    ``NULL`` — not an error, not a warning. The PII column gets written as
    NULL and the plaintext is gone forever, with every other gate green.
    This test is the ONLY thing standing between that one-line "fix" and a
    merged PR that silently destroys PII on every parse.

    This MUST stay a separate test from the encrypt(conn, None) -> None
    pass-through test below: the None short-circuit and the missing-key
    failure are two entirely different code paths, and collapsing them
    into one test would hide a regression in either.
    """
    with pytest.raises(asyncpg.PostgresError) as exc_info:
        async with conn.transaction():
            await encrypt(conn, "some-plaintext")

    assert exc_info.value is not None, (
        "expected PostgresError from a STRICT current_setting read; got "
        "None — has missing_ok=true been added to pii.py?"
    )


@pytest.mark.asyncio
async def test_decrypt_without_set_pii_key_raises_postgres_error(
    conn: asyncpg.Connection, patched_pii_key: None
) -> None:
    """Same invariant as encrypt, mirrored for decrypt: pgp_sym_decrypt is
    also STRICT, so a NULL key would silently decrypt to NULL rather than
    raising. Uses a plausible-shaped ciphertext; the point is that the key
    lookup itself must fail before pgp_sym_decrypt is ever reached."""
    async with conn.transaction():
        await set_pii_key(conn)
        ciphertext = await encrypt(conn, "some-plaintext")

    with pytest.raises(asyncpg.PostgresError):
        async with conn.transaction():
            await decrypt(conn, ciphertext)


# ── None passthrough at integration level (belt-and-braces) ─────────────────


@pytest.mark.asyncio
async def test_encrypt_none_returns_none_without_a_key_at_integration_level(
    conn: asyncpg.Connection,
) -> None:
    """Belt-and-braces: proven with a mock in tests/unit/test_pii.py; here
    it's proven against a real, keyless transaction — encrypt(None) must
    short-circuit to None WITHOUT ever needing (or failing on) the key."""
    async with conn.transaction():
        result = await encrypt(conn, None)
    assert result is None


@pytest.mark.asyncio
async def test_decrypt_none_returns_none_without_a_key_at_integration_level(
    conn: asyncpg.Connection,
) -> None:
    async with conn.transaction():
        result = await decrypt(conn, None)
    assert result is None
