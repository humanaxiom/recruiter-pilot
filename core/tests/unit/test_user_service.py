"""Unit tests for ``src.services.user_service`` (FU-5 slice 5, ADR-019 §10
step 3, §10a; user-admin-roles slice 2 reverses §10a's provisioning default —
see the module docstring note below).

Nothing here exists yet (``core/src/services/user_service.py``,
``core/src/schemas/auth.py``), so every test is expected to FAIL (RED) as an
``ImportError``/``ModuleNotFoundError`` at collection.

Deliberately narrow scope, per CLAUDE.md's ``offline`` caveat: whether the
``INSERT ... ON CONFLICT (cas_username) DO UPDATE`` actually upserts, whether
a second login truly leaves ``role`` untouched, and whether concurrent first
logins collapse to one row are all real-Postgres behaviour a mocked
connection cannot prove — that is
``tests/integration/test_session_user_service_pg.py``'s job.

What IS genuinely unit-provable without a database is the pure Python
decision of *which role value* ``provision_or_get`` computes before it ever
talks to Postgres — ADR-019 §10a's default-admin comparison
(``cas_username == default_admin_cas_username``) — captured via the bound
call args rather than by asserting on SQL text, plus the signature-level
contract that ``default_admin_cas_username`` must be passed in explicitly
(CLAUDE.md: "Config only via settings.py — never scattered"; this service
must not reach into settings itself).

**user-admin-roles slice 2 — human-directed reversal of ADR-019 §10a/§2
(2026-07-25).** A first CAS login for a username other than the configured
default-admin now captures NO role (``None``), not the old ``'recruiter'``
default. The default-admin allowlist itself (``asalah`` → ``'admin'`` on
first login) is UNCHANGED. The tests below that previously asserted
``role == "recruiter"`` for a non-default first login are rewritten to
assert ``role is None`` — this is the authorized reversal target, not a
"modify tests to pass" violation (see the coordinator's task citation of
this ADR reversal). Slice 3 adds the fail-closed gate that rejects a
no-role user from doing anything; a no-role user existing but not yet
blocked is this slice's intentional, known intermediate state.

**user-admin-roles slice 5 — ``list_users``, the ``GET /users`` admin-listing
backend.** ``core/src/services/user_service.py`` does not have this function
yet — every ``list_users`` test below fails with ``AttributeError: module
'src.services.user_service' has no attribute 'list_users'``, RED. Purely a
pass-through-and-shape contract: whether a real Postgres actually orders by
``created_at`` is proved against a live database in
``tests/integration/test_users_admin_routes_pg.py`` — a fake connection
cannot prove a real ``ORDER BY`` clause; it can only prove ``list_users``
does not itself re-sort/drop/mutate whatever ``conn.fetch`` hands back, and
that it maps each row into a real ``User``.

**user-admin-roles slice 6 — ``set_role``/``count_active_admins``, the
``PATCH /users/{id}/role`` backend (the merge-blocking payoff slice).**
Neither function exists yet on ``src.services.user_service`` — every test in
the two new sections below fails with ``AttributeError``, RED. Both are
plain pass-through SQL, same discipline as ``list_users`` above: whether the
real ``UPDATE``/``COUNT`` actually behave against Postgres (including the
atomicity of ``set_role`` + the paired ``audit_log`` write, and the real
admin-count lockout arithmetic) is proved only in
``tests/integration/test_users_admin_routes_pg.py`` — these unit tests only
pin that neither function mangles its arguments or the shape of what
``conn.execute``/``conn.fetchval`` hands back.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from src.services import user_service

DEFAULT_ADMIN = "asalah"


class _NullAsyncCtx:
    async def __aenter__(self) -> _NullAsyncCtx:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeConn:
    def __init__(self, fetchrow_row: Callable[..., dict[str, Any]]) -> None:
        self._fetchrow_row = fetchrow_row
        self.fetchrow_calls: list[tuple[Any, ...]] = []

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any]:
        self.fetchrow_calls.append(args)
        return self._fetchrow_row(*args)

    async def execute(self, query: str, *args: Any) -> str:
        return "OK"

    def transaction(self) -> _NullAsyncCtx:
        return _NullAsyncCtx()


def _build_row(username: str, *args: Any) -> dict[str, Any]:
    """Echoes the computed role back, located by its known *position* on the
    INSERT's bind args (``cas_username, display_name, email, role`` — the
    last positional arg is always ``role``) rather than by scanning for the
    literal strings ``"admin"``/``"recruiter"``. That scan-by-value approach
    cannot distinguish "role bound as None" (the slice-2 reversed default)
    from "no role value present at all", so it is replaced with a
    position-based read that captures ``None`` correctly."""
    role = args[-1] if args else None
    now = datetime.now(UTC)
    return {
        "id": uuid4(),
        "cas_username": username,
        "display_name": None,
        "email": None,
        "role": role,
        "active": True,
        "created_at": now,
        "last_seen_at": now,
    }


# ── default-admin role selection (ADR-019 §10a) — pure decision logic ──────


@pytest.mark.asyncio
async def test_admin_role_selected_for_default_admin_username() -> None:
    username = DEFAULT_ADMIN
    conn = _FakeConn(lambda *args: _build_row(username, *args))

    user = await user_service.provision_or_get(
        conn, cas_username=username, default_admin_cas_username=DEFAULT_ADMIN
    )

    assert user.role == "admin"


@pytest.mark.asyncio
async def test_no_role_selected_for_non_default_username() -> None:
    """user-admin-roles slice 2 (ADR-019 §10a/§2 reversal, human directive
    2026-07-25): a first CAS login for a username other than the configured
    default-admin now captures NO role at all — previously ``'recruiter'``,
    superseded here. Slice 3 will add the fail-closed gate that blocks a
    no-role user; that a no-role user is merely *captured* and not yet
    blocked is this slice's known, intentional intermediate state."""
    username = "someone-else"
    conn = _FakeConn(lambda *args: _build_row(username, *args))

    user = await user_service.provision_or_get(
        conn, cas_username=username, default_admin_cas_username=DEFAULT_ADMIN
    )

    assert user.role is None


@pytest.mark.asyncio
async def test_non_default_login_binds_null_role_on_the_insert() -> None:
    """Direct check of the bound INSERT argument (not just the returned
    ``User.role``) — proves the ``None`` flows all the way to the SQL call
    ``provision_or_get`` makes, not merely survives an echo through the fake
    row builder."""
    username = "someone-else"
    conn = _FakeConn(lambda *args: _build_row(username, *args))

    await user_service.provision_or_get(
        conn, cas_username=username, default_admin_cas_username=DEFAULT_ADMIN
    )

    assert len(conn.fetchrow_calls) == 1
    assert conn.fetchrow_calls[0][-1] is None, (
        "the bound role bind-arg for a non-default-admin first login must "
        "be None (ADR-019 §10a/§2 reversal), not 'recruiter'"
    )


@pytest.mark.asyncio
async def test_default_admin_match_is_case_sensitive() -> None:
    """``"Asalah" != "asalah"`` — no case-folding surprise that would widen
    the admin allowlist beyond the exact configured username. The non-match
    now yields ``None`` (slice 2's reversed default), not ``'recruiter'``."""
    username = "Asalah"
    conn = _FakeConn(lambda *args: _build_row(username, *args))

    user = await user_service.provision_or_get(
        conn, cas_username=username, default_admin_cas_username=DEFAULT_ADMIN
    )

    assert user.role is None


@pytest.mark.asyncio
async def test_default_admin_username_is_parameter_driven_not_hardcoded() -> None:
    """Changing the configured default-admin value (as if from settings)
    must change who gets promoted — proves the comparison uses the passed-in
    parameter, not a literal baked into the service."""
    username = "custom-admin-id"
    conn = _FakeConn(lambda *args: _build_row(username, *args))

    user = await user_service.provision_or_get(
        conn, cas_username=username, default_admin_cas_username="custom-admin-id"
    )

    assert user.role == "admin"


# ── signature contract: no scattered config ─────────────────────────────────


def test_provision_or_get_requires_default_admin_cas_username_kwarg() -> None:
    """Must NOT read ``settings`` internally — the caller is required to
    pass ``default_admin_cas_username`` in. Omitting it is a TypeError at
    call time, not a silently-applied internal default."""
    conn = _FakeConn(lambda *args: _build_row("whoever", *args))

    with pytest.raises(TypeError):
        user_service.provision_or_get(conn, cas_username="whoever")  # type: ignore[call-arg]


# ── list_users — the GET /users admin-listing backend (user-admin-roles
#    slice 5) ─────────────────────────────────────────────────────────────


class _FakeFetchConn:
    """Mirrors ``_FakeConn`` above but for ``.fetch`` (many rows) rather than
    ``.fetchrow`` (one row) — ``list_users`` is a plain SELECT-many, not an
    INSERT ... RETURNING."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self.fetch_calls: list[tuple[Any, ...]] = []

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        self.fetch_calls.append(args)
        return self._rows


def _user_row(
    *,
    cas_username: str,
    role: str | None,
    display_name: str | None = "Someone",
    email: str | None = "someone@example.org",
    active: bool = True,
) -> dict[str, Any]:
    now = datetime.now(UTC)
    return {
        "id": uuid4(),
        "cas_username": cas_username,
        "display_name": display_name,
        "email": email,
        "role": role,
        "active": active,
        "created_at": now,
        "last_seen_at": now,
    }


@pytest.mark.asyncio
async def test_list_users_returns_a_user_for_every_row() -> None:
    from src.schemas.auth import User as _User

    rows = [
        _user_row(cas_username="alice", role="admin"),
        _user_row(cas_username="bob", role="recruiter"),
        _user_row(cas_username="carol", role=None),
    ]
    conn = _FakeFetchConn(rows)

    result = await user_service.list_users(conn)

    assert len(result) == 3
    assert all(isinstance(u, _User) for u in result)


@pytest.mark.asyncio
async def test_list_users_preserves_fetch_order() -> None:
    """``list_users`` must not itself re-sort — whatever order ``conn.fetch``
    hands back (the real ORDER BY clause, proved against Postgres in the
    integration file) is the order the caller sees."""
    rows = [
        _user_row(cas_username="zeta", role="admin"),
        _user_row(cas_username="alpha", role="recruiter"),
        _user_row(cas_username="mid", role=None),
    ]
    conn = _FakeFetchConn(rows)

    result = await user_service.list_users(conn)

    assert [u.cas_username for u in result] == ["zeta", "alpha", "mid"]


@pytest.mark.asyncio
async def test_list_users_maps_a_no_role_row_to_a_none_role() -> None:
    """The whole point of the admin surface (user-admin-roles slice 5,
    feeding slice 6's role assignment): a no-role user must show up with
    ``role: None``, not be silently dropped or coerced to a string."""
    rows = [_user_row(cas_username="no-role-dana", role=None)]
    conn = _FakeFetchConn(rows)

    result = await user_service.list_users(conn)

    assert len(result) == 1
    assert result[0].cas_username == "no-role-dana"
    assert result[0].role is None


@pytest.mark.asyncio
async def test_list_users_returns_empty_list_when_no_users_exist() -> None:
    conn = _FakeFetchConn([])

    result = await user_service.list_users(conn)

    assert result == []


@pytest.mark.asyncio
async def test_list_users_maps_every_expected_field() -> None:
    row = _user_row(
        cas_username="frank",
        role="hiring_manager",
        display_name="Frank N. Stein",
        email="frank@example.org",
        active=False,
    )
    conn = _FakeFetchConn([row])

    result = await user_service.list_users(conn)

    assert len(result) == 1
    user = result[0]
    assert user.id == row["id"]
    assert user.cas_username == "frank"
    assert user.display_name == "Frank N. Stein"
    assert user.email == "frank@example.org"
    assert user.role == "hiring_manager"
    assert user.active is False
    assert user.created_at == row["created_at"]
    assert user.last_seen_at == row["last_seen_at"]


@pytest.mark.asyncio
async def test_list_users_calls_fetch_exactly_once() -> None:
    """No N+1 — one SELECT for the whole listing, not one per row."""
    conn = _FakeFetchConn(
        [
            _user_row(cas_username="one", role="admin"),
            _user_row(cas_username="two", role="recruiter"),
        ]
    )

    await user_service.list_users(conn)

    assert len(conn.fetch_calls) == 1


# ── set_role / count_active_admins — the PATCH /users/{id}/role backend
#    (user-admin-roles slice 6, the merge-blocking payoff slice) ───────────


class _FakeExecuteConn:
    """A bare ``.execute``-only fake — ``set_role`` is a plain UPDATE, no
    RETURNING clause, mirroring ``job_assignee_service.assign``'s own
    ``.execute``-and-forget shape."""

    def __init__(self, status: str = "UPDATE 1") -> None:
        self._status = status
        self.execute_calls: list[tuple[Any, ...]] = []

    async def execute(self, query: str, *args: Any) -> str:
        self.execute_calls.append(args)
        return self._status


class _FakeFetchvalConn:
    def __init__(self, value: Any) -> None:
        self._value = value
        self.fetchval_calls: list[tuple[Any, ...]] = []

    async def fetchval(self, query: str, *args: Any) -> Any:
        self.fetchval_calls.append(args)
        return self._value


@pytest.mark.asyncio
async def test_set_role_calls_execute_exactly_once() -> None:
    user_id = uuid4()
    conn = _FakeExecuteConn()

    await user_service.set_role(conn, user_id, "recruiter")

    assert len(conn.execute_calls) == 1


@pytest.mark.asyncio
async def test_set_role_passes_the_user_id_and_role_through() -> None:
    """Proves the bound args reach the UPDATE, not merely that *some*
    ``.execute`` call happened — mirrors ``test_non_default_login_binds_
    null_role_on_the_insert``'s discipline of checking the actual bind args."""
    user_id = uuid4()
    conn = _FakeExecuteConn()

    await user_service.set_role(conn, user_id, "hiring_manager")

    args = conn.execute_calls[0]
    assert user_id in args
    assert "hiring_manager" in args


@pytest.mark.asyncio
async def test_set_role_does_not_mutate_its_arguments() -> None:
    user_id = uuid4()
    conn = _FakeExecuteConn()

    await user_service.set_role(conn, user_id, "auditor")

    # the UUID/str objects passed in must be exactly the ones bound, not
    # copies/re-derived values.
    args = conn.execute_calls[0]
    assert any(a == user_id for a in args)
    assert any(a == "auditor" for a in args)


@pytest.mark.asyncio
async def test_count_active_admins_returns_the_fetchval_result() -> None:
    conn = _FakeFetchvalConn(3)

    result = await user_service.count_active_admins(conn)

    assert result == 3


@pytest.mark.asyncio
async def test_count_active_admins_returns_zero_when_none_exist() -> None:
    conn = _FakeFetchvalConn(0)

    result = await user_service.count_active_admins(conn)

    assert result == 0


@pytest.mark.asyncio
async def test_count_active_admins_calls_fetchval_exactly_once() -> None:
    conn = _FakeFetchvalConn(1)

    await user_service.count_active_admins(conn)

    assert len(conn.fetchval_calls) == 1


@pytest.mark.asyncio
async def test_count_active_admins_takes_no_positional_query_args() -> None:
    """A fixed, parameter-free query (``role='admin' AND active``) — nothing
    caller-supplied is bound into it."""
    conn = _FakeFetchvalConn(2)

    await user_service.count_active_admins(conn)

    assert conn.fetchval_calls[0] == ()


__all__: list[str] = []
