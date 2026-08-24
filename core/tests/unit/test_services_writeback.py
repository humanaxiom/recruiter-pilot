"""Unit tests for the worker write-back services (``src/services/*``),
mocked-connection only. Real-Postgres proof lives in
``tests/integration/test_services_writeback_pg.py``.

Covers:
  * ``job_service.record_parsed`` / ``record_parse_failure``
  * ``resume_service.record_parsed`` / ``record_parse_failure`` /
    ``encrypt_pii_via_session``
  * ``outbox_service.enqueue_outbox``

Two cut-feature regression guards live here and are the whole point of the
job_service tests: hris's ``jobs`` table had a ``title_autofilled`` column
(bulk-ingest title refinement — a CUT feature) that does not exist in this
project's DDL (``src/models/ddl.py`` — no ``title`` write at all in
``record_parsed``). If that branch is ever ported back in, the query would
break at runtime with an ``UndefinedColumnError``; this test catches the
code-level reintroduction before it ever reaches a live database.

Optimistic-concurrency contract for both ``record_parsed`` variants: the
UPDATE is scoped to specific source statuses (``jobs.status = 'draft'``;
``resumes.status IN ('uploaded', 'parsing')``) so a second parse attempt
racing a first, or a parse landing on an already-terminal row, does not
clobber it. Zero rows updated -> the function returns ``False`` and the
caller (``parse_job``/``parse_resume``) must NOT enqueue an outbox row.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

import src.services.pii as pii
from src.schemas.jobs import JDExtracted, Skill
from src.schemas.resumes import CandidateInfo, CoverLetterParsed, ResumeParsed
from src.services import job_service, outbox_service, resume_service

_NOW = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)


def _json_args(args: tuple[Any, ...]) -> list[Any]:
    """Decode the query args that are JSON documents, skipping the ones that aren't.

    The same arg list carries plain TEXT bindings (``aggregate``, ``event_type``,
    the email hash) alongside the jsonb payload, so decoding every ``str`` arg
    unconditionally blows up on the first one that isn't JSON.
    """
    decoded: list[Any] = []
    for arg in args:
        if not isinstance(arg, str):
            continue
        try:
            decoded.append(json.loads(arg))
        except json.JSONDecodeError:
            continue
    return decoded


def _squash(sql: str) -> str:
    return " ".join(sql.split())


def _mock_conn(execute_return: str = "UPDATE 1") -> Any:
    """asyncpg Connection double for a single UPDATE/INSERT statement.

    asyncpg's ``execute`` returns a command tag like ``"UPDATE 1"`` /
    ``"UPDATE 0"`` / ``"INSERT 0 1"`` — the standard idiom for detecting how
    many rows an optimistic-concurrency UPDATE actually touched without a
    round trip through ``RETURNING``.
    """
    conn = MagicMock()
    conn.execute = AsyncMock(return_value=execute_return)
    conn.fetchval = AsyncMock()
    conn.fetchrow = AsyncMock()
    return conn


# ── job_service.record_parsed ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_job_service_record_parsed_returns_true_on_success() -> None:
    conn = _mock_conn("UPDATE 1")
    extracted = JDExtracted(title="Senior Engineer")
    result = await job_service.record_parsed(conn, uuid4(), extracted, _NOW)
    assert result is True


@pytest.mark.asyncio
async def test_job_service_record_parsed_returns_false_when_zero_rows_updated() -> None:
    """The optimistic-concurrency race guard: 0 rows -> False, no exception."""
    conn = _mock_conn("UPDATE 0")
    extracted = JDExtracted(title="Senior Engineer")
    result = await job_service.record_parsed(conn, uuid4(), extracted, _NOW)
    assert result is False


@pytest.mark.asyncio
async def test_job_service_record_parsed_where_clause_restricts_to_draft() -> None:
    conn = _mock_conn("UPDATE 1")
    extracted = JDExtracted(title="Senior Engineer")
    await job_service.record_parsed(conn, uuid4(), extracted, _NOW)
    query = conn.execute.await_args.args[0]
    assert re.search(r"status\s*=\s*'draft'", query, re.IGNORECASE), query


@pytest.mark.asyncio
async def test_job_service_record_parsed_sql_never_mentions_title_autofilled() -> None:
    """title_autofilled is a hris bulk-ingest column that does NOT exist in
    this project's DDL — its reintroduction here would be an
    UndefinedColumnError waiting to happen in prod."""
    conn = _mock_conn("UPDATE 1")
    extracted = JDExtracted(title="Senior Engineer")
    await job_service.record_parsed(conn, uuid4(), extracted, _NOW)
    query = conn.execute.await_args.args[0]
    assert "title_autofilled" not in query.lower()


@pytest.mark.asyncio
async def test_job_service_record_parsed_sql_never_touches_the_title_column() -> None:
    """record_parsed only writes description_parsed/parsed_at/failure_reason
    — it must never SET the `title` column at all (no refine_title branch;
    that's a cut hris bulk-ingest feature)."""
    conn = _mock_conn("UPDATE 1")
    extracted = JDExtracted(title="Senior Engineer")
    await job_service.record_parsed(conn, uuid4(), extracted, _NOW)
    query = conn.execute.await_args.args[0]
    assert not re.search(r"\btitle\b", query, re.IGNORECASE), query


@pytest.mark.asyncio
async def test_job_service_record_parsed_serializes_description_parsed_as_jsonb() -> (
    None
):
    conn = _mock_conn("UPDATE 1")
    extracted = JDExtracted(
        title="Senior Engineer", required_skills=[Skill(name="Python")]
    )
    await job_service.record_parsed(conn, uuid4(), extracted, _NOW)
    query, *args = conn.execute.await_args.args
    assert "description_parsed" in query.lower()
    matched = [d for d in _json_args(args) if d == extracted.model_dump()]
    assert matched, f"no arg round-trips to extracted.model_dump(); got {args!r}"


# ── job_service.record_parse_failure ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_job_service_record_parse_failure_sets_failure_reason() -> None:
    conn = _mock_conn("UPDATE 1")
    await job_service.record_parse_failure(conn, uuid4(), "LLM timeout")
    query, *args = conn.execute.await_args.args
    assert re.search(r"failure_reason", query, re.IGNORECASE)
    assert "LLM timeout" in args


@pytest.mark.asyncio
async def test_job_service_record_parse_failure_does_not_set_a_failed_status() -> None:
    """job_status has NO 'failed' value in this project's DDL (only draft/
    open/closed/archived) — record_parse_failure must not try to write one."""
    conn = _mock_conn("UPDATE 1")
    await job_service.record_parse_failure(conn, uuid4(), "LLM timeout")
    query = conn.execute.await_args.args[0]
    assert "'failed'" not in query.lower()


# ── resume_service.record_parsed ─────────────────────────────────────────────


def _pii_tuple() -> tuple[bytes | None, bytes | None, bytes | None, str | None]:
    return (b"enc-name", b"enc-email", b"enc-phone", "hash123")


@pytest.mark.asyncio
async def test_resume_service_record_parsed_returns_true_on_success() -> None:
    conn = _mock_conn("UPDATE 1")
    result = await resume_service.record_parsed(
        conn, uuid4(), ResumeParsed(), _pii_tuple(), None, _NOW
    )
    assert result is True


@pytest.mark.asyncio
async def test_resume_service_record_parsed_returns_false_when_zero_rows_updated() -> (
    None
):
    conn = _mock_conn("UPDATE 0")
    result = await resume_service.record_parsed(
        conn, uuid4(), ResumeParsed(), _pii_tuple(), None, _NOW
    )
    assert result is False


@pytest.mark.asyncio
async def test_resume_service_record_parsed_sets_status_parsed_and_clears_failure() -> (
    None
):
    conn = _mock_conn("UPDATE 1")
    await resume_service.record_parsed(
        conn, uuid4(), ResumeParsed(), _pii_tuple(), None, _NOW
    )
    query = _squash(conn.execute.await_args.args[0]).lower()
    assert "status = 'parsed'" in query
    assert "parsed_at" in query
    assert "failure_reason = null" in query


@pytest.mark.asyncio
async def test_resume_service_record_parsed_sets_the_pii_columns() -> None:
    conn = _mock_conn("UPDATE 1")
    await resume_service.record_parsed(
        conn, uuid4(), ResumeParsed(), _pii_tuple(), None, _NOW
    )
    query, *args = conn.execute.await_args.args
    lowered = query.lower()
    for column in (
        "candidate_name",
        "candidate_email",
        "candidate_phone",
        "candidate_email_hash",
    ):
        assert column in lowered, f"{column} missing from record_parsed SQL"
    assert b"enc-name" in args
    assert b"enc-email" in args
    assert b"enc-phone" in args
    assert "hash123" in args


@pytest.mark.asyncio
async def test_resume_record_parsed_where_restricts_to_uploaded_or_parsing() -> None:
    conn = _mock_conn("UPDATE 1")
    await resume_service.record_parsed(
        conn, uuid4(), ResumeParsed(), _pii_tuple(), None, _NOW
    )
    query = _squash(conn.execute.await_args.args[0])
    assert re.search(
        r"status\s+IN\s*\(\s*'uploaded'\s*,\s*'parsing'\s*\)", query, re.IGNORECASE
    ), query


@pytest.mark.asyncio
async def test_resume_service_record_parsed_serializes_parsed_as_jsonb() -> None:
    conn = _mock_conn("UPDATE 1")
    parsed = ResumeParsed(summary="Experienced engineer")
    await resume_service.record_parsed(conn, uuid4(), parsed, _pii_tuple(), None, _NOW)
    query, *args = conn.execute.await_args.args
    assert "parsed" in query.lower()
    matched = [d for d in _json_args(args) if d == parsed.model_dump()]
    assert matched, f"no arg round-trips to parsed.model_dump(); got {args!r}"


@pytest.mark.asyncio
async def test_resume_service_record_parsed_serializes_cover_letter_parsed() -> None:
    conn = _mock_conn("UPDATE 1")
    cl = CoverLetterParsed(raw_text="Dear hiring manager", themes=["enthusiasm"])
    await resume_service.record_parsed(
        conn, uuid4(), ResumeParsed(), _pii_tuple(), cl, _NOW
    )
    query, *args = conn.execute.await_args.args
    assert "cover_letter_parsed" in query.lower()
    matched = [d for d in _json_args(args) if d == cl.model_dump()]
    assert matched, f"no arg round-trips to cover_letter_parsed; got {args!r}"


@pytest.mark.asyncio
async def test_resume_service_record_parsed_accepts_none_cover_letter() -> None:
    conn = _mock_conn("UPDATE 1")
    result = await resume_service.record_parsed(
        conn, uuid4(), ResumeParsed(), _pii_tuple(), None, _NOW
    )
    assert result is True


# ── resume_service.record_parse_failure ──────────────────────────────────────


@pytest.mark.asyncio
async def test_resume_service_record_parse_failure_sets_status_failed() -> None:
    conn = _mock_conn("UPDATE 1")
    await resume_service.record_parse_failure(conn, uuid4(), "corrupt pdf")
    query, *args = conn.execute.await_args.args
    assert re.search(r"status\s*=\s*'failed'", query, re.IGNORECASE)
    assert re.search(r"failure_reason", query, re.IGNORECASE)
    assert "corrupt pdf" in args


# ── resume_service.encrypt_pii_via_session — ordering is the whole point ────


@pytest.mark.asyncio
async def test_encrypt_pii_session_calls_set_key_once_before_encrypt() -> None:
    """set_config must land before any current_setting() read in the same
    transaction — call pii.set_pii_key exactly once, and strictly before all
    three pii.encrypt calls (name, email, phone)."""
    conn = _mock_conn()
    candidate = CandidateInfo(name="Ada", email="ada@example.org", phone="555-1234")
    call_log: list[str] = []

    async def _fake_set_pii_key(c: object) -> None:
        call_log.append("set_pii_key")

    async def _fake_encrypt(c: object, plaintext: str | None) -> bytes | None:
        call_log.append(f"encrypt:{plaintext}")
        return None if plaintext is None else f"enc:{plaintext}".encode()

    with (
        patch("src.services.pii.set_pii_key", side_effect=_fake_set_pii_key),
        patch("src.services.pii.encrypt", side_effect=_fake_encrypt),
    ):
        result = await resume_service.encrypt_pii_via_session(conn, candidate)

    assert call_log[0] == "set_pii_key", (
        f"set_pii_key must be the FIRST call in the same transaction (SET "
        f"LOCAL must land before any current_setting() read); got {call_log!r}"
    )
    assert call_log.count("set_pii_key") == 1
    encrypt_calls = [c for c in call_log if c.startswith("encrypt:")]
    assert len(encrypt_calls) == 3, f"expected 3 encrypt calls, got {call_log!r}"
    assert result == (
        b"enc:Ada",
        b"enc:ada@example.org",
        b"enc:555-1234",
        pii.email_hash("ada@example.org"),
    )


@pytest.mark.asyncio
async def test_encrypt_pii_session_returns_none_slots_for_missing_fields() -> None:
    conn = _mock_conn()
    candidate = CandidateInfo()  # every field None

    async def _fake_set_pii_key(c: object) -> None:
        return None

    async def _fake_encrypt(c: object, plaintext: str | None) -> bytes | None:
        return None if plaintext is None else f"enc:{plaintext}".encode()

    with (
        patch("src.services.pii.set_pii_key", side_effect=_fake_set_pii_key),
        patch("src.services.pii.encrypt", side_effect=_fake_encrypt),
    ):
        result = await resume_service.encrypt_pii_via_session(conn, candidate)

    assert result[0] is None
    assert result[1] is None
    assert result[2] is None
    assert result[3] is None  # email_hash(None) is None


# ── outbox_service.enqueue_outbox ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_enqueue_outbox_inserts_into_the_outbox_table() -> None:
    conn = _mock_conn("INSERT 0 1")
    job_id = uuid4()
    await outbox_service.enqueue_outbox(
        conn,
        aggregate="job",
        aggregate_id=job_id,
        event_type="job.parsed",
        payload={"a": 1},
    )
    query = conn.execute.await_args.args[0]
    assert re.search(r"INSERT\s+INTO\s+outbox", query, re.IGNORECASE)


@pytest.mark.asyncio
async def test_enqueue_outbox_sets_aggregate_aggregate_id_and_event_type() -> None:
    conn = _mock_conn("INSERT 0 1")
    job_id = uuid4()
    await outbox_service.enqueue_outbox(
        conn,
        aggregate="job",
        aggregate_id=job_id,
        event_type="job.parsed",
        payload={"a": 1},
    )
    query, *args = conn.execute.await_args.args
    lowered = query.lower()
    assert "aggregate_id" in lowered
    assert "event_type" in lowered
    assert re.search(r"\baggregate\b", lowered)
    assert "job" in args
    assert job_id in args
    assert "job.parsed" in args


@pytest.mark.asyncio
async def test_enqueue_outbox_serializes_payload_as_jsonb() -> None:
    conn = _mock_conn("INSERT 0 1")
    payload = {"embedding": [0.1, 0.2], "prompt_version": "jd_extract_v1"}
    await outbox_service.enqueue_outbox(
        conn,
        aggregate="job",
        aggregate_id=uuid4(),
        event_type="job.parsed",
        payload=payload,
    )
    query, *args = conn.execute.await_args.args
    assert "payload" in query.lower()
    matched = [d for d in _json_args(args) if d == payload]
    assert matched, f"no arg round-trips to the payload dict; got {args!r}"
