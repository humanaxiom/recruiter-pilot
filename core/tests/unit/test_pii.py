"""Unit tests for ``src/services/pii.py`` — the pgcrypto PII helpers.

Phase 3 ports hris ``apps/api/src/api/services/pii.py`` with ONE deliberate
deviation: the encryption key is sourced from ``get_settings().pii_key`` (an
env-supplied string), never from a key *file* — hris reads
``/run/secrets/pii_key``; this project has no such file and never should.

Everything here mocks the asyncpg connection — no live database. The real
"does the GUC actually enforce anything" proof lives in
``tests/integration/test_pii_encryption.py`` (needs real pgcrypto).

Merge-blocking invariant pinned here (belt-and-braces alongside the
integration mutation test): ``pii.py``'s source must never contain
``missing_ok`` — that flag on a ``current_setting`` read turns a missing
``app.pii_key`` into a silent ``NULL`` instead of a raised error, and because
``pgp_sym_encrypt`` is a STRICT SQL function, ``pgp_sym_encrypt(x, NULL)``
silently returns ``NULL`` too. That is silent, irreversible PII data loss
with a green build.
"""

from __future__ import annotations

import hashlib
import inspect
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import src.services.pii as pii
from src.services.pii import decrypt, email_hash, encrypt, set_pii_key


def _squash(sql: str) -> str:
    return " ".join(sql.split())


def _mock_conn() -> Any:
    """asyncpg Connection double with both read paths mocked.

    ``encrypt``/``decrypt``/``set_pii_key`` all issue ``SELECT`` statements
    that return a single value, so ``fetchval`` is the expected asyncpg
    idiom — but we also mock ``execute`` so a ``set_pii_key`` implementation
    that (reasonably) ignores the return value and calls ``execute`` instead
    is not unfairly penalised.
    """
    conn = MagicMock()
    conn.fetchval = AsyncMock()
    conn.execute = AsyncMock()
    return conn


def _all_calls(conn: Any) -> list[Any]:
    return list(conn.fetchval.await_args_list) + list(conn.execute.await_args_list)


# ── email_hash: deliberately PLAINTEXT sha256 (PIPEDA subject-access) ───────


def test_email_hash_is_case_insensitively_deterministic() -> None:
    assert email_hash("Ada@Example.ORG") == email_hash("ada@example.org")


def test_email_hash_strips_surrounding_whitespace() -> None:
    assert email_hash("  ada@example.org  ") == email_hash("ada@example.org")


def test_email_hash_is_plaintext_sha256_of_normalized_email() -> None:
    """Deliberately NOT encrypted — a plaintext hash so subject-access
    requests can find a candidate by email without the pii key."""
    expected = hashlib.sha256(b"ada@example.org").hexdigest()
    assert email_hash("Ada@Example.org") == expected


def test_email_hash_returns_a_hex_digest_string() -> None:
    result = email_hash("ada@example.org")
    assert isinstance(result, str)
    assert len(result) == 64  # sha256 hex digest length
    int(result, 16)  # must be valid hex


@pytest.mark.parametrize("raw", [None, "", "   ", "\t\n"])
def test_email_hash_none_or_blank_returns_none(raw: str | None) -> None:
    assert email_hash(raw) is None


# ── encrypt/decrypt None short-circuit — must NEVER touch the connection ────


@pytest.mark.asyncio
async def test_encrypt_none_short_circuits_without_touching_the_connection() -> None:
    """Proves the None short-circuit is distinct from the missing-key path:
    if this ever touched the connection, a caller under an unset key would
    get a PostgresError instead of a clean None for an absent field."""
    conn = _mock_conn()
    result = await encrypt(conn, None)
    assert result is None
    conn.fetchval.assert_not_awaited()
    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_decrypt_none_short_circuits_without_touching_the_connection() -> None:
    conn = _mock_conn()
    result = await decrypt(conn, None)
    assert result is None
    conn.fetchval.assert_not_awaited()
    conn.execute.assert_not_awaited()


# ── encrypt/decrypt SQL — pinned verbatim per the phase-3 contract ──────────


@pytest.mark.asyncio
async def test_encrypt_issues_the_verbatim_pgp_sym_encrypt_sql() -> None:
    conn = _mock_conn()
    conn.fetchval.return_value = b"\x01\x02ciphertext"

    result = await encrypt(conn, "ada@example.org")

    assert result == b"\x01\x02ciphertext"
    conn.fetchval.assert_awaited_once()
    query, *args = conn.fetchval.await_args.args
    assert _squash(query) == _squash(
        "SELECT pgp_sym_encrypt($1::text, current_setting('app.pii_key'))::bytea"
    )
    assert args == ["ada@example.org"]


@pytest.mark.asyncio
async def test_encrypt_returns_bytes_from_the_connection_untouched() -> None:
    conn = _mock_conn()
    conn.fetchval.return_value = b"opaque"
    assert await encrypt(conn, "x") == b"opaque"


@pytest.mark.asyncio
async def test_decrypt_issues_the_verbatim_pgp_sym_decrypt_sql() -> None:
    conn = _mock_conn()
    conn.fetchval.return_value = "ada@example.org"

    result = await decrypt(conn, b"ciphertext-bytes")

    assert result == "ada@example.org"
    conn.fetchval.assert_awaited_once()
    query, *args = conn.fetchval.await_args.args
    assert _squash(query) == _squash(
        "SELECT pgp_sym_decrypt($1::bytea, current_setting('app.pii_key'))"
    )
    assert args == [b"ciphertext-bytes"]


# ── set_pii_key sources the key from settings, NOT a file ───────────────────


@pytest.mark.asyncio
async def test_set_pii_key_sources_the_key_from_get_settings() -> None:
    conn = _mock_conn()
    fake_settings = SimpleNamespace(pii_key="test-configured-key")

    with patch("src.services.pii.get_settings", return_value=fake_settings):
        await set_pii_key(conn)

    calls = _all_calls(conn)
    assert calls, "set_pii_key must issue a query against the connection"
    matched = [
        call
        for call in calls
        if call.args
        and "set_config" in _squash(str(call.args[0]))
        and "app.pii_key" in str(call.args[0])
        and "test-configured-key" in call.args
    ]
    assert matched, (
        f"expected a set_config('app.pii_key', 'test-configured-key', ...) "
        f"call; got {calls!r}"
    )


@pytest.mark.asyncio
async def test_set_pii_key_never_reads_a_file() -> None:
    """DEVIATION from hris: hris resolves a key FILE
    (``/run/secrets/pii_key``). This project sources the key from
    ``Settings.pii_key`` (an env string) exclusively. Any filesystem read
    inside ``set_pii_key`` would be the file-path ladder creeping back in."""
    conn = _mock_conn()
    fake_settings = SimpleNamespace(pii_key="k")
    guard = MagicMock(
        side_effect=AssertionError(
            "set_pii_key touched the filesystem — the key must come from "
            "get_settings().pii_key, not a key file"
        )
    )

    with (
        patch("src.services.pii.get_settings", return_value=fake_settings),
        patch("builtins.open", guard),
    ):
        await set_pii_key(conn)  # must not raise


@pytest.mark.asyncio
async def test_set_pii_key_uses_local_scope_true_so_it_is_transaction_scoped() -> None:
    """The third arg to set_config MUST be true (SET LOCAL semantics) —
    session-scoped (false) would leak the key across pooled connections."""
    conn = _mock_conn()
    fake_settings = SimpleNamespace(pii_key="k")

    with patch("src.services.pii.get_settings", return_value=fake_settings):
        await set_pii_key(conn)

    calls = _all_calls(conn)
    matched = [c for c in calls if c.args and "set_config" in _squash(str(c.args[0]))]
    assert matched
    # `true` (is_local) may be passed as a literal in the query text or as a
    # bound parameter — accept either.
    call = matched[0]
    query_text = _squash(str(call.args[0]))
    assert "true" in query_text.lower() or True in call.args


# ── Source-level guard: no missing_ok, ever ──────────────────────────────────


def test_pii_module_source_never_contains_missing_ok() -> None:
    """The merge-blocking invariant. If someone "fixes a flaky test" by
    adding ``missing_ok=true`` to the ``current_setting('app.pii_key')``
    read, the GUC silently returns NULL when the key was never set instead
    of raising — and because ``pgp_sym_encrypt`` is STRICT, the ciphertext
    column is silently written as NULL. This guard, plus the integration
    mutation test in ``test_pii_encryption.py``, are the only things that
    turn that into a red gate instead of a silent production data loss.
    """
    source = inspect.getsource(pii)
    assert "missing_ok" not in source, (
        "found 'missing_ok' in src/services/pii.py — this would make "
        "current_setting('app.pii_key') return NULL instead of raising when "
        "the key was never set, and pgp_sym_encrypt(x, NULL) silently "
        "returns NULL. Remove it."
    )
