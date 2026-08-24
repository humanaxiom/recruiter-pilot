"""Unit tests for Phase 6's job CRUD/status service surface —
``src.services.job_service.create_job`` / ``get_job`` / ``list_jobs`` /
``transition_status`` / ``_row_to_jobout``.

None of these exist yet — ``src/services/job_service.py`` currently ships
only the worker write-back pair (``record_parsed`` / ``record_parse_failure``,
Phase 3). Every test below fails at collection (``ImportError``) or the first
``await`` (``AttributeError``). RED half of the TDD cycle.

**Ambiguities this file locks (the coder's implementation MUST match):**

* ``create_job(conn, payload: JobCreate, *, created_by: str | None) -> JobOut``
  inserts a row with ``status='draft'`` (the DDL default) and returns the full
  ``JobOut`` built from the inserted row.
* ``get_job(conn, job_id) -> JobOut`` raises ``src.errors.NotFoundError`` when
  the id does not resolve (mirrors ``resume_service.get_one``).
* ``list_jobs(conn, *, limit=50, offset=0, status=None) -> list[JobListItem]``
  — ``status`` is an optional filter; omitted means "all statuses".
* ``_row_to_jobout(row) -> JobOut`` is the SINGLE place that builds a
  ``JobOut`` from a raw DB row. THE LOAD-BEARING CONTRACT (ADR-006 / Phase 2
  security "low", carried forward through every HANDOFF phase since): the DTO
  field ``JobOut.blind_review`` defaults to ``False`` (fail-OPEN) if a builder
  ever omits it, so ``_row_to_jobout`` MUST read ``row["blind_review"]``
  explicitly — never rely on the pydantic default. Tested BOTH directions
  (True row -> True DTO, False row -> False DTO) so the test cannot pass by a
  builder that hardcodes either literal.
* ``transition_status(conn, job_id, to) -> JobOut`` validates the transition
  against a known state graph (draft -> open -> closed -> archived, strictly
  forward, no skipping) and raises ``ValueError`` for any transition not in
  that graph (including a same-state no-op and a backward move) — this is the
  route's 409 material, NOT a pydantic ``Literal`` rejection (JobStatus is
  already a closed enum at the schema layer; this is business-rule
  validation on TOP of a syntactically valid target).

**FU-5 slice 9 (ADR-019 §1.4/§6) — ``update_job`` gains attributable audit for
the ``blind_review`` flip.** ``blind_review`` is called out by ADR-019 §1.4 as
the widest-blast-radius unaudited action in the codebase: flipping it
permanently un-blinds every résumé/shortlist under a job. This slice makes
``update_job`` require THREE new keyword-only actor-identity parameters —
``actor_kind`` / ``actor_user_id`` / ``actor_service`` — mirroring
``audit_service.record_audit``'s own kwargs, because the caller (the route)
already resolves this identity via ``resolve_user`` (see ``test_route_jobs.py``
for the three actor-derivation cases). ``update_job`` itself:

* Detects a flip by comparing the job's PRE-update ``blind_review`` value
  against the payload's new value (only when ``blind_review`` is actually
  present in the PATCH payload — an omitted field can never flip anything).
* Writes exactly ONE ``audit_log`` row via ``audit_service.record_audit``
  (``action='blind_review_toggled'``, ``subject_type='job'``,
  ``subject_id=job_id``, ``job_id=job_id``,
  ``details={"old": <bool>, "new": <bool>}``) ONLY when the value actually
  changes — a same-value PATCH (client resends the current value) or a PATCH
  that never touches ``blind_review`` writes NO audit row.
* Does the UPDATE and the (conditional) audit INSERT inside the SAME
  ``conn.transaction()`` — SECURITY-CRITICAL per ADR-019 §1.4: a crash must
  never leave the flip applied without an audit row (this is the OPPOSITE
  ordering from reveal's audit-before-decrypt, whose crash-safety concern
  runs the other way; here flip-without-audit, not audit-without-flip, is the
  failure to prevent). The crash-atomicity guarantee itself can only be
  proven against a real Postgres — see
  ``tests/integration/test_blind_review_audit_pg.py``; the tests below pin
  the STRUCTURAL contract (a transaction is used, record_audit is called
  with the right args, inside/around the same connection).

**Pre-existing tests updated in this RED commit (mechanical, not a behaviour
contradiction):** every existing ``job_service.update_job(...)`` call site
below now passes ``**_ACTOR_KWARGS`` because the three actor parameters are
now REQUIRED keyword-only arguments — without this, every one of those calls
would raise ``TypeError: update_job() missing 3 required keyword-only
arguments`` regardless of what it is actually testing. None of their
assertions changed.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from src.errors import NotFoundError
from src.schemas.jobs import JobCreate, JobListItem, JobOut, JobUpdate

_NOW = dt.datetime(2026, 7, 16, tzinfo=dt.UTC)

# FU-5 slice 9: a fixed, generic actor for pre-existing tests that are NOT
# about the audit behaviour at all (title changes, get/list/transition, etc.)
# — using "service"/"test-harness" keeps them satisfying the CHECK-mirrored
# "exactly one actor field set" shape without pretending a fake human logged
# in.
_ACTOR_KWARGS: dict[str, Any] = {
    "actor_kind": "service",
    "actor_user_id": None,
    "actor_service": "test-harness",
}


class _Row(dict[str, Any]):
    def __getitem__(self, key: str) -> Any:
        return dict.get(self, key)


def _job_row(
    *,
    job_id: UUID | None = None,
    title: str = "Senior Backend Engineer",
    status: str = "draft",
    blind_review: bool = True,
    description_parsed: dict[str, Any] | None = None,
    created_by: str | None = "api",
) -> _Row:
    return _Row(
        {
            "id": job_id or uuid4(),
            "title": title,
            "department": "Engineering",
            "location": "Remote",
            "employment_type": "full_time",
            "seniority": "senior",
            "min_years": 5,
            "description_raw": "We are looking for a senior backend engineer " * 3,
            "description_parsed": (
                json.dumps(description_parsed)
                if description_parsed is not None
                else None
            ),
            "status": status,
            "retention_days": 180,
            "shortlist_top_percent": 100,
            "blind_review": blind_review,
            "failure_reason": None,
            "created_by": created_by,
            "created_at": _NOW,
            "updated_at": _NOW,
            "parsed_at": None,
            "closed_at": None,
        }
    )


def _acm(return_value: Any = None) -> MagicMock:
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=return_value)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _mock_conn(
    *,
    fetchrow: _Row | None = None,
    fetch: list[_Row] | None = None,
    fetchval: Any = None,
) -> MagicMock:
    conn = MagicMock(name="conn")
    conn.fetchrow = AsyncMock(return_value=fetchrow)
    conn.fetch = AsyncMock(return_value=fetch or [])
    conn.fetchval = AsyncMock(return_value=fetchval)
    conn.execute = AsyncMock(return_value="UPDATE 1")
    conn.transaction = MagicMock(return_value=_acm())
    return conn


def _payload(**overrides: Any) -> JobCreate:
    base = {
        "title": "Senior Backend Engineer",
        "description_raw": "We are looking for a senior backend engineer " * 3,
    }
    base.update(overrides)
    return JobCreate.model_validate(base)


# ── create_job ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_job_returns_a_joubout_in_draft_status() -> None:
    from src.services import job_service

    row = _job_row(status="draft")
    conn = _mock_conn(fetchrow=row)
    out = await job_service.create_job(conn, _payload(), created_by="alice")
    assert isinstance(out, JobOut)
    assert out.status == "draft"


@pytest.mark.asyncio
async def test_create_job_inserts_with_the_payload_title() -> None:
    from src.services import job_service

    row = _job_row(title="Staff ML Engineer")
    conn = _mock_conn(fetchrow=row)
    await job_service.create_job(
        conn, _payload(title="Staff ML Engineer"), created_by="x"
    )
    query, *args = (
        conn.execute.await_args.args
        if conn.execute.await_args
        else (conn.fetchrow.await_args.args)
    )
    assert "Staff ML Engineer" in args or "INSERT" in query.upper()


@pytest.mark.asyncio
async def test_create_job_persists_created_by_actor_label() -> None:
    from src.services import job_service

    row = _job_row(created_by="alice")
    conn = _mock_conn(fetchrow=row)
    out = await job_service.create_job(conn, _payload(), created_by="alice")
    assert out.created_by == "alice"


@pytest.mark.asyncio
async def test_create_job_default_actor_label_is_api_when_none_passed() -> None:
    """The route resolves a default actor of "api" when X-Actor-Name is
    absent — create_job must round-trip whatever the caller passes (None
    stays None at the DB level; the "api" default is the ROUTE's job, tested
    in test_route_jobs.py)."""
    from src.services import job_service

    row = _job_row(created_by=None)
    conn = _mock_conn(fetchrow=row)
    out = await job_service.create_job(conn, _payload(), created_by=None)
    assert out.created_by is None


# ── get_job ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_job_returns_joubout_for_an_existing_job() -> None:
    from src.services import job_service

    job_id = uuid4()
    conn = _mock_conn(fetchrow=_job_row(job_id=job_id))
    out = await job_service.get_job(conn, job_id)
    assert out.id == job_id


@pytest.mark.asyncio
async def test_get_job_raises_not_found_error_on_missing_id() -> None:
    from src.services import job_service

    conn = _mock_conn(fetchrow=None)
    with pytest.raises(NotFoundError):
        await job_service.get_job(conn, uuid4())


# ── list_jobs ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_jobs_returns_joblistitem_rows() -> None:
    from src.services import job_service

    rows = [_job_row(title="A"), _job_row(title="B")]
    conn = _mock_conn(fetch=rows)
    out = await job_service.list_jobs(conn)
    assert len(out) == 2
    assert all(isinstance(o, JobListItem) for o in out)


@pytest.mark.asyncio
async def test_list_jobs_passes_limit_and_offset_through_to_the_query() -> None:
    from src.services import job_service

    conn = _mock_conn(fetch=[])
    await job_service.list_jobs(conn, limit=10, offset=20)
    args = conn.fetch.await_args.args
    assert 10 in args
    assert 20 in args


@pytest.mark.asyncio
async def test_list_jobs_status_filter_is_passed_when_provided() -> None:
    from src.services import job_service

    conn = _mock_conn(fetch=[])
    await job_service.list_jobs(conn, status="open")
    args = conn.fetch.await_args.args
    assert "open" in args


@pytest.mark.asyncio
async def test_list_jobs_status_filter_omitted_means_no_status_arg() -> None:
    """Without a status filter, the query must not silently scope to one
    status (e.g. hard-coding 'open') — omitted means "every status"."""
    from src.services import job_service

    conn = _mock_conn(fetch=[])
    await job_service.list_jobs(conn)
    query = conn.fetch.await_args.args[0]
    # No status literal should be baked into the SQL when the filter is None.
    assert "status =" not in query.lower() or "$" in query.lower()


# ── _row_to_jobout — the fail-open blind_review contract ───────────────────


def test_row_to_jobout_sets_blind_review_true_from_a_true_row() -> None:
    from src.services.job_service import _row_to_jobout

    out = _row_to_jobout(_job_row(blind_review=True))
    assert out.blind_review is True


def test_row_to_jobout_sets_blind_review_false_from_a_false_row() -> None:
    """The other direction: a builder that hardcodes True (instead of reading
    the row) would pass the test above but fail this one — the pair is what
    makes the guard load-bearing, not either test alone."""
    from src.services.job_service import _row_to_jobout

    out = _row_to_jobout(_job_row(blind_review=False))
    assert out.blind_review is False


def test_row_to_jobout_parses_jsonb_description_parsed() -> None:
    from src.services.job_service import _row_to_jobout

    row = _job_row(description_parsed={"title": "Senior Backend Engineer"})
    out = _row_to_jobout(row)
    assert out.description_parsed is not None
    assert out.description_parsed.title == "Senior Backend Engineer"


def test_row_to_jobout_handles_null_description_parsed() -> None:
    from src.services.job_service import _row_to_jobout

    out = _row_to_jobout(_job_row(description_parsed=None))
    assert out.description_parsed is None


# ── transition_status ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_transition_status_draft_to_open_is_valid() -> None:
    from src.services import job_service

    job_id = uuid4()
    conn = _mock_conn(fetchrow=_job_row(job_id=job_id, status="open"))
    conn.fetchval = AsyncMock(return_value="draft")
    out = await job_service.transition_status(conn, job_id, "open")
    assert out.status == "open"


@pytest.mark.asyncio
async def test_transition_status_rejects_an_unknown_target_state() -> None:
    """Skipping straight from draft to archived is not a valid forward
    transition — the service layer must reject it even though 'archived' is
    itself a syntactically valid JobStatus member."""
    from src.services import job_service

    job_id = uuid4()
    conn = _mock_conn(fetchrow=_job_row(job_id=job_id, status="draft"))
    conn.fetchval = AsyncMock(return_value="draft")
    with pytest.raises(ValueError, match="(?i)transition"):
        await job_service.transition_status(conn, job_id, "archived")


@pytest.mark.asyncio
async def test_transition_status_rejects_backward_moves() -> None:
    from src.services import job_service

    job_id = uuid4()
    conn = _mock_conn(fetchrow=_job_row(job_id=job_id, status="draft"))
    conn.fetchval = AsyncMock(return_value="open")
    with pytest.raises(ValueError):
        await job_service.transition_status(conn, job_id, "draft")


@pytest.mark.asyncio
async def test_transition_status_raises_not_found_for_a_missing_job() -> None:
    from src.services import job_service

    conn = _mock_conn(fetchrow=None)
    conn.fetchval = AsyncMock(return_value=None)
    with pytest.raises(NotFoundError):
        await job_service.transition_status(conn, uuid4(), "open")


# ── update_job — general partial-update endpoint ────────────────────────────
#
# ``JobUpdate`` (src/schemas/jobs.py) has every field optional and a PATCH
# omit (field unset) must mean "unchanged", NOT "set to null" — this is why
# the implementation MUST build its SET clause from
# ``payload.model_dump(exclude_unset=True)``, never from the model's full
# field set (which would stamp every unset field to None/its default and
# clobber the row). ``blind_review`` is the sharpest case: it is a bool, so
# ``False`` is a legitimate value a client sends on purpose and must be
# distinguishable from "omitted".


@pytest.mark.asyncio
async def test_update_job_partial_update_only_touches_sent_fields() -> None:
    """Only ``title`` was sent — the SQL args must carry the new title but
    must NOT carry any other JobUpdate field name/value pair."""
    from src.services import job_service

    job_id = uuid4()
    conn = _mock_conn(fetchrow=_job_row(job_id=job_id, title="New Title"))
    await job_service.update_job(
        conn,
        job_id,
        JobUpdate.model_validate({"title": "New Title"}),
        **_ACTOR_KWARGS,
    )
    query, *args = conn.fetchrow.await_args.args
    assert "New Title" in args
    assert "title" in query.lower()
    # Only the job_id + the one changed value should be bound — no other
    # JobUpdate field (department, location, ...) leaks into the args.
    assert len(args) == 2  # job_id, "New Title"


@pytest.mark.asyncio
async def test_update_job_blind_review_flip_true_to_false() -> None:
    from src.services import job_service

    job_id = uuid4()
    conn = _mock_conn(
        fetchrow=_job_row(job_id=job_id, blind_review=False), fetchval=True
    )
    out = await job_service.update_job(
        conn,
        job_id,
        JobUpdate.model_validate({"blind_review": False}),
        **_ACTOR_KWARGS,
    )
    assert out.blind_review is False
    query, *args = conn.fetchrow.await_args.args
    assert False in args
    assert "blind_review" in query.lower()


@pytest.mark.asyncio
async def test_update_job_blind_review_flip_false_to_true() -> None:
    from src.services import job_service

    job_id = uuid4()
    conn = _mock_conn(
        fetchrow=_job_row(job_id=job_id, blind_review=True), fetchval=False
    )
    out = await job_service.update_job(
        conn,
        job_id,
        JobUpdate.model_validate({"blind_review": True}),
        **_ACTOR_KWARGS,
    )
    assert out.blind_review is True
    query, *args = conn.fetchrow.await_args.args
    assert True in args


@pytest.mark.asyncio
async def test_update_job_omitted_blind_review_leaves_it_unchanged() -> None:
    """Sending only ``title`` must NOT assign ``blind_review`` in the SQL SET
    clause, proving the implementation reads ``exclude_unset`` rather than
    the full model (which would otherwise stamp ``blind_review=None`` over
    the row). ``blind_review`` may still legitimately appear elsewhere in the
    query (e.g. a ``RETURNING`` column list needed to rebuild the ``JobOut``)
    — only an *assignment* to it is forbidden here."""
    from src.services import job_service

    job_id = uuid4()
    conn = _mock_conn(fetchrow=_job_row(job_id=job_id, title="New Title"))
    await job_service.update_job(
        conn,
        job_id,
        JobUpdate.model_validate({"title": "New Title"}),
        **_ACTOR_KWARGS,
    )
    query = conn.fetchrow.await_args.args[0]
    assert "blind_review =" not in query.lower()
    assert "blind_review=" not in query.lower()


@pytest.mark.asyncio
async def test_update_job_raises_not_found_for_missing_id() -> None:
    from src.services import job_service

    conn = _mock_conn(fetchrow=None)
    with pytest.raises(NotFoundError):
        await job_service.update_job(
            conn,
            uuid4(),
            JobUpdate.model_validate({"title": "New Title"}),
            **_ACTOR_KWARGS,
        )


@pytest.mark.asyncio
async def test_update_job_empty_payload_is_a_noop_returning_current_row() -> None:
    """No fields sent at all — the service must not error, and must return
    the CURRENT row (fetched via a plain SELECT / get_job path, never an
    UPDATE with an empty SET clause, which is invalid SQL)."""
    from src.services import job_service

    job_id = uuid4()
    conn = _mock_conn(fetchrow=_job_row(job_id=job_id, title="Unchanged Title"))
    out = await job_service.update_job(conn, job_id, JobUpdate(), **_ACTOR_KWARGS)
    assert out.id == job_id
    assert out.title == "Unchanged Title"


@pytest.mark.asyncio
async def test_update_job_empty_payload_missing_id_still_raises_not_found() -> None:
    from src.services import job_service

    conn = _mock_conn(fetchrow=None)
    with pytest.raises(NotFoundError):
        await job_service.update_job(conn, uuid4(), JobUpdate(), **_ACTOR_KWARGS)


# ── update_job — FU-5 slice 9 (ADR-019 §1.4/§6): blind_review flip audit ────
#
# ``blind_review`` is ADR-019 §1.4's "widest-blast-radius unaudited action" —
# a PATCH that flips it permanently un-blinds every résumé/shortlist under a
# job. The tests below pin: exactly one audit row on a real flip, zero rows
# when the value doesn't actually change (same-value resend, or the field is
# absent entirely), the forwarded actor identity, and the atomic-transaction
# structure (real crash-atomicity is an integration-only guarantee — see
# ``tests/integration/test_blind_review_audit_pg.py``).


@pytest.mark.asyncio
async def test_update_job_blind_review_flip_true_to_false_writes_audit_row_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.services import job_service

    job_id = uuid4()
    conn = _mock_conn(
        fetchrow=_job_row(job_id=job_id, blind_review=False), fetchval=True
    )
    audit = AsyncMock(return_value=None)
    monkeypatch.setattr(job_service.audit_service, "record_audit", audit)

    await job_service.update_job(
        conn,
        job_id,
        JobUpdate.model_validate({"blind_review": False}),
        **_ACTOR_KWARGS,
    )

    audit.assert_awaited_once()
    kwargs = audit.await_args.kwargs
    assert kwargs["action"] == "blind_review_toggled"
    assert kwargs["subject_type"] == "job"
    assert kwargs["subject_id"] == job_id
    assert kwargs["job_id"] == job_id
    assert kwargs["details"] == {"old": True, "new": False}


@pytest.mark.asyncio
async def test_update_job_blind_review_flip_false_to_true_writes_audit_row_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pairing test for the flip direction — a builder that only checks
    ``new is False`` would pass the test above but fail this one."""
    from src.services import job_service

    job_id = uuid4()
    conn = _mock_conn(
        fetchrow=_job_row(job_id=job_id, blind_review=True), fetchval=False
    )
    audit = AsyncMock(return_value=None)
    monkeypatch.setattr(job_service.audit_service, "record_audit", audit)

    await job_service.update_job(
        conn,
        job_id,
        JobUpdate.model_validate({"blind_review": True}),
        **_ACTOR_KWARGS,
    )

    audit.assert_awaited_once()
    kwargs = audit.await_args.kwargs
    assert kwargs["action"] == "blind_review_toggled"
    assert kwargs["details"] == {"old": False, "new": True}


@pytest.mark.asyncio
async def test_update_job_blind_review_same_value_writes_no_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A client resending the CURRENT value (True -> True) is not a flip —
    no audit row, even though ``blind_review`` is present in the payload."""
    from src.services import job_service

    job_id = uuid4()
    conn = _mock_conn(
        fetchrow=_job_row(job_id=job_id, blind_review=True), fetchval=True
    )
    audit = AsyncMock(return_value=None)
    monkeypatch.setattr(job_service.audit_service, "record_audit", audit)

    await job_service.update_job(
        conn,
        job_id,
        JobUpdate.model_validate({"blind_review": True}),
        **_ACTOR_KWARGS,
    )

    audit.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_job_blind_review_absent_from_payload_writes_no_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``blind_review`` never appears in the payload at all — an omitted
    field can never flip anything, so no audit row, regardless of what the
    job's current value happens to be."""
    from src.services import job_service

    job_id = uuid4()
    conn = _mock_conn(fetchrow=_job_row(job_id=job_id, title="New Title"))
    audit = AsyncMock(return_value=None)
    monkeypatch.setattr(job_service.audit_service, "record_audit", audit)

    await job_service.update_job(
        conn,
        job_id,
        JobUpdate.model_validate({"title": "New Title"}),
        **_ACTOR_KWARGS,
    )

    audit.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_job_forwards_actor_kwargs_to_record_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Whatever actor identity the CALLER (the route) resolved must be
    forwarded verbatim into the ``audit_log`` row — ``update_job`` does not
    invent or override it."""
    from src.services import job_service

    job_id = uuid4()
    user_id = uuid4()
    conn = _mock_conn(
        fetchrow=_job_row(job_id=job_id, blind_review=False), fetchval=True
    )
    audit = AsyncMock(return_value=None)
    monkeypatch.setattr(job_service.audit_service, "record_audit", audit)

    await job_service.update_job(
        conn,
        job_id,
        JobUpdate.model_validate({"blind_review": False}),
        actor_kind="user",
        actor_user_id=user_id,
        actor_service=None,
    )

    kwargs = audit.await_args.kwargs
    assert kwargs["actor_kind"] == "user"
    assert kwargs["actor_user_id"] == user_id
    assert kwargs.get("actor_service") is None


@pytest.mark.asyncio
async def test_update_job_blind_review_flip_uses_a_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SECURITY-CRITICAL (ADR-019 §1.4): the audit_log write must be atomic
    with the blind_review UPDATE, so a crash cannot leave the flip applied
    without an audit row (the ADR-018 gap this closes — the opposite ordering
    concern from reveal). Structurally pinned here as "a transaction wraps
    the operation"; the real crash-atomicity guarantee is proven against a
    real Postgres in ``tests/integration/test_blind_review_audit_pg.py``."""
    from src.services import job_service

    job_id = uuid4()
    conn = _mock_conn(
        fetchrow=_job_row(job_id=job_id, blind_review=False), fetchval=True
    )
    monkeypatch.setattr(
        job_service.audit_service, "record_audit", AsyncMock(return_value=None)
    )

    await job_service.update_job(
        conn,
        job_id,
        JobUpdate.model_validate({"blind_review": False}),
        **_ACTOR_KWARGS,
    )

    conn.transaction.assert_called_once()


@pytest.mark.asyncio
async def test_update_job_missing_id_blind_review_touched_raises_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A job that vanishes out from under a blind_review-touching PATCH must
    404 (mirroring the existing missing-id contract) and must NOT write an
    audit row for a job that was never actually updated."""
    from src.services import job_service

    conn = _mock_conn(fetchrow=None, fetchval=True)
    audit = AsyncMock(return_value=None)
    monkeypatch.setattr(job_service.audit_service, "record_audit", audit)

    with pytest.raises(NotFoundError):
        await job_service.update_job(
            conn,
            uuid4(),
            JobUpdate.model_validate({"blind_review": False}),
            **_ACTOR_KWARGS,
        )

    audit.assert_not_awaited()


# ── FU-configurable-shortlist-size slice B: shortlist_top_percent persistence
#
# Slice A (already GREEN on this branch) added ``jobs.shortlist_top_percent``
# to the DDL + ``_JOB_COLS`` SELECT/RETURNING list and to ``JobCreate`` /
# ``JobUpdate`` -- but NEITHER ``_insert_job`` nor ``_UPDATABLE_JOB_COLUMNS``
# were updated, so a caller's chosen value silently never reaches Postgres:
# create relies entirely on the column DEFAULT (100), and a PATCH touching
# ONLY ``shortlist_top_percent`` is filtered out of ``update_job``'s
# ``fields`` dict entirely -- an empty ``fields`` short-circuits to
# ``get_job`` (a pure read), so the PATCH is a silent no-op. These tests pin
# that both paths must actually write the value.


@pytest.mark.asyncio
async def test_create_job_persists_a_custom_shortlist_top_percent_value() -> None:
    """``JobCreate(shortlist_top_percent=30)`` must flow into the INSERT's
    bound arguments -- today ``_insert_job`` never reads this field at all,
    relying entirely on the column's own DEFAULT (100), so 30 never appears
    among the args this mock captures."""
    from src.services import job_service

    row = _job_row()
    row["shortlist_top_percent"] = 30
    conn = _mock_conn(fetchrow=row)
    await job_service.create_job(
        conn, _payload(shortlist_top_percent=30), created_by="alice"
    )
    args = conn.fetchrow.await_args.args
    assert 30 in args, (
        "create_job must pass payload.shortlist_top_percent into the INSERT "
        f"-- got args={args!r}"
    )


@pytest.mark.asyncio
async def test_create_job_default_shortlist_top_percent_is_explicitly_passed() -> None:
    """Even the SCHEMA default (100, when the caller sends nothing) must be
    passed through explicitly once slice B lands -- ``JobCreate`` always
    carries a concrete value (never truly "unset"), so the INSERT should not
    depend on the column DEFAULT to reproduce it."""
    from src.services import job_service

    row = _job_row()
    row["shortlist_top_percent"] = 100
    conn = _mock_conn(fetchrow=row)
    await job_service.create_job(conn, _payload(), created_by="alice")
    args = conn.fetchrow.await_args.args
    assert 100 in args, (
        "create_job must pass the default shortlist_top_percent=100 into "
        f"the INSERT explicitly -- got args={args!r}"
    )


@pytest.mark.asyncio
async def test_update_job_shortlist_top_percent_is_updatable() -> None:
    """A PATCH touching ONLY ``shortlist_top_percent`` must actually issue an
    UPDATE that assigns the new value -- today ``_UPDATABLE_JOB_COLUMNS``
    omits it, so ``fields`` ends up empty and ``update_job`` short-circuits
    to a plain ``get_job`` read: the PATCH silently never reaches Postgres."""
    from src.services import job_service

    job_id = uuid4()
    row = _job_row(job_id=job_id)
    row["shortlist_top_percent"] = 50
    conn = _mock_conn(fetchrow=row)
    out = await job_service.update_job(
        conn,
        job_id,
        JobUpdate.model_validate({"shortlist_top_percent": 50}),
        **_ACTOR_KWARGS,
    )
    assert out.shortlist_top_percent == 50
    query, *args = conn.fetchrow.await_args.args
    assert 50 in args, (
        "update_job must bind the new shortlist_top_percent value -- got "
        f"args={args!r}"
    )
    assert "shortlist_top_percent =" in query.lower(), (
        "update_job must assign shortlist_top_percent in its SET clause -- "
        f"got query={query!r}"
    )


@pytest.mark.asyncio
async def test_update_job_omitted_shortlist_top_percent_leaves_it_unchanged() -> None:
    """Sending only ``title`` must NOT assign ``shortlist_top_percent`` in
    the SQL SET clause -- a PATCH omit means "unchanged", the same
    convention as every other ``JobUpdate`` field."""
    from src.services import job_service

    job_id = uuid4()
    conn = _mock_conn(fetchrow=_job_row(job_id=job_id, title="New Title"))
    await job_service.update_job(
        conn,
        job_id,
        JobUpdate.model_validate({"title": "New Title"}),
        **_ACTOR_KWARGS,
    )
    query = conn.fetchrow.await_args.args[0]
    assert "shortlist_top_percent =" not in query.lower()
