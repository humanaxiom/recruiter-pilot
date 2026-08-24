"""Unit tests for ``src.worker.job_lock`` — the Postgres advisory-lock wrapper
that dedupes concurrent ``shortlist_job`` / ``reverse_match_job`` runs
(ADR-010 §1 residual: "No advisory lock on concurrent duplicate enqueues").

All I/O is mocked here — a mocked ``conn.fetchval``/``conn.execute`` can only
prove WHAT SQL this module issues and with WHAT ARGUMENTS, never whether
``pg_try_advisory_lock`` actually blocks a second, concurrent SESSION. That
real semantic (the entire point of this fix) can only be shown against a real
Postgres with two live connections — see
``tests/integration/test_job_lock_pg.py``, which is MANDATORY, not optional,
for this change.

``src.worker.job_lock`` does not exist yet — every test below fails at
collection with ``ModuleNotFoundError``. RED half of the TDD cycle.

Contract pinned here (the coder must match this exactly, since the
integration tests and the ``matching_tasks`` wiring tests both import these
same names):

* ``_CLASS_ID`` — a per-``kind`` namespace so a job and a résumé whose low
  bits collide never fight over the same advisory-lock key:
    - ``"shortlist"``     -> classid ``1``
    - ``"reverse_match"`` -> classid ``2``
* ``_entity_to_objid(entity_id: UUID) -> int`` — deterministic UUID -> signed
  32-bit int (the ``objid`` half of the two-int advisory-lock key), taking
  the LOW 32 BITS of ``entity_id.int`` and reinterpreting them as a signed
  postgres ``int4`` (i.e. subtract ``2**32`` when the unsigned low-32 value is
  ``>= 2**31``, matching postgres's own int4 wraparound so the value always
  fits ``-2147483648..2147483647``).
* ``try_job_lock(conn, kind: str, entity_id: UUID) -> bool`` issues
  ``SELECT pg_try_advisory_lock($1, $2)`` via ``conn.fetchval`` with
  ``($1, $2) == (_CLASS_ID[kind], _entity_to_objid(entity_id))`` and returns
  exactly what ``fetchval`` returns.
* ``release_job_lock(conn, kind: str, entity_id: UUID) -> None`` issues
  ``SELECT pg_advisory_unlock($1, $2)`` (or bare ``pg_advisory_unlock``) via
  ``conn.execute`` with the SAME two-int key.
* an unrecognised ``kind`` raises ``ValueError`` BEFORE any query is issued,
  for both functions.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest


def _make_conn(fetchval_result: Any = None) -> MagicMock:
    conn = MagicMock(name="conn")
    conn.fetchval = AsyncMock(return_value=fetchval_result)
    conn.execute = AsyncMock()
    return conn


# ── namespace constants ─────────────────────────────────────────────────────


def test_shortlist_classid_is_1() -> None:
    from src.worker.job_lock import _CLASS_ID

    assert _CLASS_ID["shortlist"] == 1


def test_reverse_match_classid_is_2() -> None:
    from src.worker.job_lock import _CLASS_ID

    assert _CLASS_ID["reverse_match"] == 2


def test_classids_are_distinct_per_kind() -> None:
    from src.worker.job_lock import _CLASS_ID

    assert len(set(_CLASS_ID.values())) == len(_CLASS_ID)


# ── UUID -> int32 objid derivation ──────────────────────────────────────────


def test_entity_to_objid_is_deterministic() -> None:
    from src.worker.job_lock import _entity_to_objid

    entity_id = uuid4()
    assert _entity_to_objid(entity_id) == _entity_to_objid(entity_id)


def test_entity_to_objid_matches_low32_signed_wraparound_formula() -> None:
    from src.worker.job_lock import _entity_to_objid

    # low 32 bits all-ones -> unsigned 4294967295 -> signed int4 -1
    all_ones_low32 = UUID(int=0xFFFFFFFF)
    assert _entity_to_objid(all_ones_low32) == -1

    # low 32 bits == 1 -> signed int4 1 (no wraparound needed)
    low32_is_one = UUID(int=1)
    assert _entity_to_objid(low32_is_one) == 1

    # low 32 bits == 2**31 (msb set, rest zero) -> signed int4 -2**31
    low32_is_msb = UUID(int=0x80000000)
    assert _entity_to_objid(low32_is_msb) == -2_147_483_648


def test_entity_to_objid_ignores_the_high_96_bits() -> None:
    from src.worker.job_lock import _entity_to_objid

    low = 0x0000_0042
    a = UUID(int=low)
    b = UUID(int=(0xDEADBEEF << 32) | low)
    assert _entity_to_objid(a) == _entity_to_objid(b)


def test_entity_to_objid_always_within_postgres_int4_range() -> None:
    from src.worker.job_lock import _entity_to_objid

    for _ in range(200):
        value = _entity_to_objid(uuid4())
        assert -2_147_483_648 <= value <= 2_147_483_647


def test_entity_to_objid_differs_across_random_uuids() -> None:
    from src.worker.job_lock import _entity_to_objid

    a, b = uuid4(), uuid4()
    assert _entity_to_objid(a) != _entity_to_objid(b)


# ── try_job_lock ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_try_job_lock_issues_pg_try_advisory_lock_with_classid_and_objid() -> (
    None
):
    from src.worker.job_lock import _entity_to_objid, try_job_lock

    conn = _make_conn(fetchval_result=True)
    entity_id = uuid4()

    result = await try_job_lock(conn, "shortlist", entity_id)

    assert result is True
    conn.fetchval.assert_awaited_once()
    call = conn.fetchval.await_args
    sql = call.args[0]
    assert "pg_try_advisory_lock" in sql
    assert call.args[1] == 1
    assert call.args[2] == _entity_to_objid(entity_id)


@pytest.mark.asyncio
async def test_try_job_lock_returns_false_verbatim_when_fetchval_is_false() -> None:
    from src.worker.job_lock import try_job_lock

    conn = _make_conn(fetchval_result=False)
    result = await try_job_lock(conn, "shortlist", uuid4())

    assert result is False


@pytest.mark.asyncio
async def test_try_job_lock_reverse_match_uses_classid_2() -> None:
    from src.worker.job_lock import try_job_lock

    conn = _make_conn(fetchval_result=True)
    await try_job_lock(conn, "reverse_match", uuid4())

    assert conn.fetchval.await_args.args[1] == 2


@pytest.mark.asyncio
async def test_try_job_lock_unknown_kind_raises_before_any_query() -> None:
    from src.worker.job_lock import try_job_lock

    conn = _make_conn()

    with pytest.raises(ValueError, match="kind"):
        await try_job_lock(conn, "bogus", uuid4())

    conn.fetchval.assert_not_called()


# ── release_job_lock ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_release_job_lock_issues_pg_advisory_unlock_with_same_key() -> None:
    from src.worker.job_lock import _entity_to_objid, release_job_lock

    conn = _make_conn()
    entity_id = uuid4()

    await release_job_lock(conn, "shortlist", entity_id)

    conn.execute.assert_awaited_once()
    call = conn.execute.await_args
    sql = call.args[0]
    assert "pg_advisory_unlock" in sql
    assert call.args[1] == 1
    assert call.args[2] == _entity_to_objid(entity_id)


@pytest.mark.asyncio
async def test_release_job_lock_reverse_match_uses_classid_2() -> None:
    from src.worker.job_lock import release_job_lock

    conn = _make_conn()
    await release_job_lock(conn, "reverse_match", uuid4())

    assert conn.execute.await_args.args[1] == 2


@pytest.mark.asyncio
async def test_release_job_lock_unknown_kind_raises_before_any_query() -> None:
    from src.worker.job_lock import release_job_lock

    conn = _make_conn()

    with pytest.raises(ValueError, match="kind"):
        await release_job_lock(conn, "bogus", uuid4())

    conn.execute.assert_not_called()


@pytest.mark.asyncio
async def test_release_job_lock_returns_none() -> None:
    from src.worker.job_lock import release_job_lock

    conn = _make_conn()
    result = await release_job_lock(conn, "shortlist", uuid4())

    assert result is None
