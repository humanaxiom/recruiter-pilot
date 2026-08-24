"""Unit tests for the Phase 7 Flask viewer's backend client —
``frontend.api_client`` (new module; does not exist yet). The whole file
fails at collection (``ModuleNotFoundError``) — RED half of the TDD cycle,
mirroring the established pattern in ``test_route_shortlist.py`` /
``test_route_resumes.py`` for a not-yet-existent module.

**The contract this file locks (the coder implements exactly this shape):**

* ``build_client() -> httpx.Client`` — a thin ``httpx.Client(base_url=
  settings.api_base_url)`` wrapper. FU-4/D6: the Flask viewer presents ONE
  FIXED role key outbound for every browser it serves — ``recruiter`` (the
  ADR-recommended role; recruiter is the router-writer role Flask exercises
  most, and is never the reveal-restricted-to-admin-only case). Attaches an
  ``X-API-Key`` header IFF ``settings.api_key_recruiter`` is non-empty
  (mirroring ``src.api.deps.resolve_role``'s auth-disabled-when-all-four-
  empty semantics — Flask only ever presents the recruiter key, so ITS
  disabled check is simply that one field being empty) and must not crash on
  a non-ASCII key (the client-side mirror of ADR-012 §1's SEC-1 fix).
* One function per consumed backend route, EVERY function accepting an
  optional keyword-only ``client: httpx.Client | None = None`` so tests can
  inject an ``httpx.MockTransport``-backed client without any real network:
  - ``list_jobs(*, limit=50, offset=0, status=None, client=None)``
  - ``get_job(job_id, *, client=None)``
  - ``list_resumes(job_id, *, client=None)``
  - ``list_shortlist(job_id, *, client=None)`` — **NO ``reveal`` parameter at
    all** (shortlist list reads are unconditionally blind — Phase 6's
    ``shortlist_service.list_for_job`` takes no ``reveal`` kwarg either).
  - ``get_shortlist_entry(entry_id, *, client=None)`` — same, **no
    ``reveal``**.
  - ``get_resume(resume_id, *, client=None)`` — **FU-4/D3: NO ``reveal``
    parameter at all.** The backend route dropped ``reveal`` entirely (the
    audited ``POST /resumes/{id}/reveal`` is the only un-blinding path — see
    ``reveal_resume`` below); accepting a ``reveal`` kwarg here would be an
    inert argument that reads like it works but is silently ignored
    backend-side, so the client itself must reject it, exactly like
    ``list_shortlist``/``get_shortlist_entry`` above.
  - ``get_match_results(resume_id, *, client=None)`` — reverse-match read,
    no redaction on the backend (ADR-012 §4) and no ``reveal`` concept here.
  - ``export_shortlist(job_id, *, format="csv", client=None)`` — **FU-4/D3:
    NO ``reveal`` parameter either** (dropped from the backend export route
    too — this was an UNAUDITED bulk de-anonymization across a whole
    shortlist; exports are unconditionally blind now). Returns the raw
    ``httpx.Response`` so the Flask route can proxy
    ``Content-Disposition``/body straight through.
  - ``withdraw_resume(resume_id, *, reason=None, client=None)`` — POST
    ``/resumes/{id}/withdraw`` (ADR-026/FU-8), JSON body ``{"reason": ...}``.
  - ``reinstate_resume(resume_id, *, client=None)`` — POST
    ``/resumes/{id}/reinstate`` (ADR-026/FU-8), no body required.
  - ``get_resume_status_breakdown(job_id, *, client=None)`` — GET
    ``/jobs/{id}/resume-status`` (ADR-026 decision 5/FU-8), returns the
    5-bucket status count dict.
* Typed error mapping: a backend 404 raises ``NotFound``; a backend 5xx OR
  ``httpx.ConnectError`` raises ``BackendUnavailable``. Both subclass a common
  ``BackendError``.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Callable
from typing import Any
from uuid import uuid4

import httpx
import pytest

from frontend import api_client
from src.settings import Settings


def _settings(
    *, api_key_recruiter: str = "", api_base_url: str = "http://api:8000"
) -> Settings:
    return Settings(api_key_recruiter=api_key_recruiter, api_base_url=api_base_url)


def _client_with(
    handler: Callable[[httpx.Request], httpx.Response],
) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test")


def _json_handler(
    status_code: int, body: Any
) -> Callable[[httpx.Request], httpx.Response]:
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=body)

    return _handler


def _raising_handler(exc: Exception) -> Callable[[httpx.Request], httpx.Response]:
    def _handler(request: httpx.Request) -> httpx.Response:
        raise exc

    return _handler


# ── build_client: X-API-Key header attachment ────────────────────────────


def test_build_client_uses_the_configured_base_url(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        api_client,
        "get_settings",
        lambda: _settings(api_base_url="http://backend:9000"),
    )
    client = api_client.build_client()
    assert str(client.base_url).rstrip("/") == "http://backend:9000"


def test_build_client_attaches_api_key_header_when_configured(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        api_client, "get_settings", lambda: _settings(api_key_recruiter="secret123")
    )
    client = api_client.build_client()
    assert client.headers.get("X-API-Key") == "secret123"


def test_build_client_omits_api_key_header_when_empty(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        api_client, "get_settings", lambda: _settings(api_key_recruiter="")
    )
    client = api_client.build_client()
    assert "X-API-Key" not in client.headers


def test_build_client_survives_a_non_ascii_api_key_without_crashing(
    monkeypatch: Any,
) -> None:
    """Client-side mirror of ADR-012 §1's SEC-1 fix: a non-ASCII API key must
    not crash client construction (the server-side fix compares UTF-8 bytes
    for the same reason)."""
    monkeypatch.setattr(
        api_client,
        "get_settings",
        lambda: _settings(api_key_recruiter="clé-secrète-café"),
    )
    client = api_client.build_client()
    assert client is not None


# ── typed error mapping ───────────────────────────────────────────────────


def test_get_job_raises_not_found_on_backend_404() -> None:
    client = _client_with(_json_handler(404, {"detail": "not found"}))
    with pytest.raises(api_client.NotFound):
        api_client.get_job(uuid4(), client=client)


def test_get_job_raises_backend_unavailable_on_backend_500() -> None:
    client = _client_with(_json_handler(500, {"detail": "boom"}))
    with pytest.raises(api_client.BackendUnavailable):
        api_client.get_job(uuid4(), client=client)


def test_get_job_raises_backend_unavailable_on_connect_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = _client_with(handler)
    with pytest.raises(api_client.BackendUnavailable):
        api_client.get_job(uuid4(), client=client)


def test_not_found_and_backend_unavailable_share_a_common_base_error() -> None:
    assert issubclass(api_client.NotFound, api_client.BackendError)
    assert issubclass(api_client.BackendUnavailable, api_client.BackendError)


@pytest.mark.parametrize(
    "call",
    [
        lambda c: api_client.get_resume(uuid4(), client=c),
        lambda c: api_client.get_shortlist_entry(uuid4(), client=c),
        lambda c: api_client.get_match_results(uuid4(), client=c),
    ],
)
def test_every_get_one_style_function_raises_not_found_on_404(
    call: Callable[[httpx.Client], Any],
) -> None:
    client = _client_with(_json_handler(404, {"detail": "not found"}))
    with pytest.raises(api_client.NotFound):
        call(client)


@pytest.mark.parametrize(
    "call",
    [
        lambda c: api_client.list_jobs(client=c),
        lambda c: api_client.list_resumes(uuid4(), client=c),
        lambda c: api_client.list_shortlist(uuid4(), client=c),
    ],
)
def test_every_list_style_function_raises_backend_unavailable_on_5xx(
    call: Callable[[httpx.Client], Any],
) -> None:
    client = _client_with(_json_handler(503, {"detail": "db down"}))
    with pytest.raises(api_client.BackendUnavailable):
        call(client)


# ── redaction-boundary contract: shortlist reads take NO reveal param ────


def test_list_shortlist_signature_has_no_reveal_parameter() -> None:
    sig = inspect.signature(api_client.list_shortlist)
    assert "reveal" not in sig.parameters


def test_get_shortlist_entry_signature_has_no_reveal_parameter() -> None:
    sig = inspect.signature(api_client.get_shortlist_entry)
    assert "reveal" not in sig.parameters


def test_list_shortlist_rejects_a_reveal_kwarg_at_call_time() -> None:
    with pytest.raises(TypeError):
        api_client.list_shortlist(uuid4(), reveal=True)  # type: ignore[call-arg]


def test_get_shortlist_entry_rejects_a_reveal_kwarg_at_call_time() -> None:
    with pytest.raises(TypeError):
        api_client.get_shortlist_entry(uuid4(), reveal=True)  # type: ignore[call-arg]


def test_list_shortlist_never_sends_a_reveal_query_param() -> None:
    captured: dict[str, httpx.URL] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = request.url
        return httpx.Response(200, json=[])

    api_client.list_shortlist(uuid4(), client=_client_with(handler))
    assert "reveal" not in captured["url"].params


def test_get_shortlist_entry_never_sends_a_reveal_query_param() -> None:
    captured: dict[str, httpx.URL] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = request.url
        return httpx.Response(200, json={})

    api_client.get_shortlist_entry(uuid4(), client=_client_with(handler))
    assert "reveal" not in captured["url"].params


# ── redaction-boundary contract: résumé reads take NO reveal param (D3) ──
#
# FU-4/D3 removed `reveal` from the BACKEND route entirely — the audited
# `POST /resumes/{id}/reveal` (see `reveal_resume` below) is the ONLY
# un-blinding path. A `reveal` kwarg accepted here (and silently ignored by
# the backend either way) would be an inert argument that reads like it
# works. It must not exist on the client either.


def test_get_resume_signature_has_no_reveal_parameter() -> None:
    sig = inspect.signature(api_client.get_resume)
    assert "reveal" not in sig.parameters


def test_get_resume_rejects_a_reveal_kwarg_at_call_time() -> None:
    with pytest.raises(TypeError):
        api_client.get_resume(uuid4(), reveal=True)  # type: ignore[call-arg]


def test_get_resume_never_sends_a_reveal_query_param() -> None:
    captured: dict[str, httpx.URL] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = request.url
        return httpx.Response(200, json={})

    api_client.get_resume(uuid4(), client=_client_with(handler))
    assert "reveal" not in captured["url"].params


def test_reveal_resume_posts_to_reveal_and_returns_unblinded_json() -> None:
    captured: dict[str, Any] = {}
    resume_id = uuid4()

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = request.url
        return httpx.Response(200, json={"candidate": {"name": "Casey Rivera"}})

    result = api_client.reveal_resume(
        resume_id, context="shortlist", client=_client_with(handler)
    )
    assert captured["method"] == "POST"
    assert captured["url"].path == f"/resumes/{resume_id}/reveal"
    assert captured["url"].params.get("context") == "shortlist"
    assert result == {"candidate": {"name": "Casey Rivera"}}


def test_reveal_resume_omits_context_param_when_none() -> None:
    captured: dict[str, httpx.URL] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = request.url
        return httpx.Response(200, json={})

    api_client.reveal_resume(uuid4(), client=_client_with(handler))
    assert "context" not in captured["url"].params


# ── route wiring: each function hits the expected path ──────────────────


def test_list_jobs_hits_jobs_and_returns_the_parsed_json() -> None:
    captured: dict[str, httpx.URL] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = request.url
        return httpx.Response(200, json=[{"id": "j1"}])

    result = api_client.list_jobs(client=_client_with(handler))
    assert result == [{"id": "j1"}]
    assert captured["url"].path == "/jobs"


def test_list_jobs_sends_limit_offset_and_status_query_params() -> None:
    captured: dict[str, httpx.URL] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = request.url
        return httpx.Response(200, json=[])

    api_client.list_jobs(
        limit=10, offset=5, status="open", client=_client_with(handler)
    )
    params = captured["url"].params
    assert params["limit"] == "10"
    assert params["offset"] == "5"
    assert params["status"] == "open"


def test_get_job_hits_the_expected_path() -> None:
    job_id = uuid4()
    captured: dict[str, httpx.URL] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = request.url
        return httpx.Response(200, json={"id": str(job_id)})

    result = api_client.get_job(job_id, client=_client_with(handler))
    assert result == {"id": str(job_id)}
    assert captured["url"].path == f"/jobs/{job_id}"


def test_list_resumes_hits_the_expected_path() -> None:
    job_id = uuid4()
    captured: dict[str, httpx.URL] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = request.url
        return httpx.Response(200, json=[])

    api_client.list_resumes(job_id, client=_client_with(handler))
    assert captured["url"].path == f"/jobs/{job_id}/resumes"


def test_list_shortlist_hits_the_expected_path() -> None:
    job_id = uuid4()
    captured: dict[str, httpx.URL] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = request.url
        return httpx.Response(200, json=[])

    api_client.list_shortlist(job_id, client=_client_with(handler))
    assert captured["url"].path == f"/jobs/{job_id}/shortlist"


def test_get_shortlist_entry_hits_the_expected_path() -> None:
    entry_id = uuid4()
    captured: dict[str, httpx.URL] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = request.url
        return httpx.Response(200, json={})

    api_client.get_shortlist_entry(entry_id, client=_client_with(handler))
    assert captured["url"].path == f"/shortlist/{entry_id}"


def test_get_resume_hits_the_expected_path() -> None:
    resume_id = uuid4()
    captured: dict[str, httpx.URL] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = request.url
        return httpx.Response(200, json={})

    api_client.get_resume(resume_id, client=_client_with(handler))
    assert captured["url"].path == f"/resumes/{resume_id}"


def test_get_match_results_hits_the_expected_path() -> None:
    resume_id = uuid4()
    captured: dict[str, httpx.URL] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = request.url
        return httpx.Response(200, json={"resume_id": str(resume_id), "entries": []})

    api_client.get_match_results(resume_id, client=_client_with(handler))
    assert captured["url"].path == f"/resumes/{resume_id}/match-results"


# ── export proxy ───────────────────────────────────────────────────────
#
# FU-4/D3: `reveal` is gone from `export_shortlist` too (see the module
# docstring and the redaction-boundary section above) — the same contract
# as `get_resume`.


def test_export_shortlist_signature_has_no_reveal_parameter() -> None:
    sig = inspect.signature(api_client.export_shortlist)
    assert "reveal" not in sig.parameters


def test_export_shortlist_rejects_a_reveal_kwarg_at_call_time() -> None:
    with pytest.raises(TypeError):
        api_client.export_shortlist(uuid4(), reveal=True)  # type: ignore[call-arg]


def test_export_shortlist_never_sends_a_reveal_query_param() -> None:
    job_id = uuid4()
    captured: dict[str, httpx.URL] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = request.url
        return httpx.Response(
            200, content=b"data", headers={"content-type": "text/csv"}
        )

    api_client.export_shortlist(job_id, client=_client_with(handler))
    assert "reveal" not in captured["url"].params


def test_export_shortlist_hits_the_expected_path_with_format() -> None:
    job_id = uuid4()
    captured: dict[str, httpx.URL] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = request.url
        return httpx.Response(
            200,
            content=b"rank,score\n1,0.9\n",
            headers={
                "content-type": "text/csv",
                "content-disposition": f'attachment; filename="shortlist-{job_id}.csv"',
            },
        )

    resp = api_client.export_shortlist(
        job_id, format="evidence-csv", client=_client_with(handler)
    )
    assert captured["url"].path == f"/jobs/{job_id}/shortlist/export"
    assert captured["url"].params.get("format") == "evidence-csv"
    assert isinstance(resp, httpx.Response)
    assert resp.content == b"rank,score\n1,0.9\n"
    assert resp.headers["content-disposition"] == (
        f'attachment; filename="shortlist-{job_id}.csv"'
    )


def test_export_shortlist_defaults_to_csv_format() -> None:
    job_id = uuid4()
    captured: dict[str, httpx.URL] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = request.url
        return httpx.Response(
            200, content=b"data", headers={"content-type": "text/csv"}
        )

    api_client.export_shortlist(job_id, client=_client_with(handler))
    assert captured["url"].params.get("format") == "csv"


def test_export_shortlist_raises_backend_unavailable_on_5xx() -> None:
    job_id = uuid4()
    client = _client_with(_json_handler(500, {"detail": "boom"}))
    with pytest.raises(api_client.BackendUnavailable):
        api_client.export_shortlist(job_id, client=client)


def test_export_shortlist_raises_not_found_on_404() -> None:
    job_id = uuid4()
    client = _client_with(_json_handler(404, {"detail": "no such job"}))
    with pytest.raises(api_client.NotFound):
        api_client.export_shortlist(job_id, client=client)


# ── withdrawal lifecycle (ADR-026, FU-8) ─────────────────────────────────
#
# `withdraw_resume`/`reinstate_resume` mirror the reveal client functions:
# POST to a dedicated per-resume route, JSON body (withdraw only), typed
# error mapping shared with every other client function.
# `get_resume_status_breakdown` mirrors a plain GET-one style read.


def test_withdraw_resume_posts_to_withdraw_with_the_reason() -> None:
    captured: dict[str, Any] = {}
    resume_id = uuid4()

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = request.url
        captured["body"] = request.content
        return httpx.Response(
            200,
            json={"id": str(resume_id), "withdrawn_at": "2026-07-28T00:00:00Z"},
        )

    result = api_client.withdraw_resume(
        resume_id, reason="Accepted another offer", client=_client_with(handler)
    )
    assert captured["method"] == "POST"
    assert captured["url"].path == f"/resumes/{resume_id}/withdraw"
    payload = json.loads(captured["body"])
    assert payload == {"reason": "Accepted another offer"}
    assert result["withdrawn_at"] == "2026-07-28T00:00:00Z"


def test_withdraw_resume_reason_defaults_to_none() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content
        return httpx.Response(200, json={})

    api_client.withdraw_resume(uuid4(), client=_client_with(handler))
    payload = json.loads(captured["body"])
    assert payload.get("reason") is None


def test_withdraw_resume_raises_not_found_on_404() -> None:
    client = _client_with(_json_handler(404, {"detail": "no such resume"}))
    with pytest.raises(api_client.NotFound):
        api_client.withdraw_resume(uuid4(), client=client)


def test_withdraw_resume_raises_backend_unavailable_on_5xx() -> None:
    client = _client_with(_json_handler(500, {"detail": "boom"}))
    with pytest.raises(api_client.BackendUnavailable):
        api_client.withdraw_resume(uuid4(), client=client)


def test_reinstate_resume_posts_to_reinstate_and_returns_json() -> None:
    captured: dict[str, Any] = {}
    resume_id = uuid4()

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = request.url
        return httpx.Response(200, json={"id": str(resume_id), "withdrawn_at": None})

    result = api_client.reinstate_resume(resume_id, client=_client_with(handler))
    assert captured["method"] == "POST"
    assert captured["url"].path == f"/resumes/{resume_id}/reinstate"
    assert result["withdrawn_at"] is None


def test_reinstate_resume_raises_not_found_on_404() -> None:
    client = _client_with(_json_handler(404, {"detail": "no such resume"}))
    with pytest.raises(api_client.NotFound):
        api_client.reinstate_resume(uuid4(), client=client)


def test_reinstate_resume_raises_backend_unavailable_on_5xx() -> None:
    client = _client_with(_json_handler(500, {"detail": "boom"}))
    with pytest.raises(api_client.BackendUnavailable):
        api_client.reinstate_resume(uuid4(), client=client)


def test_get_resume_status_breakdown_hits_the_resume_status_route() -> None:
    job_id = uuid4()
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = request.url
        captured["method"] = request.method
        return httpx.Response(
            200,
            json={
                "uploaded": 1,
                "parsing": 0,
                "parsed": 3,
                "failed": 0,
                "withdrawn": 2,
            },
        )

    result = api_client.get_resume_status_breakdown(
        job_id, client=_client_with(handler)
    )
    assert captured["method"] == "GET"
    assert captured["url"].path == f"/jobs/{job_id}/resume-status"
    assert result == {
        "uploaded": 1,
        "parsing": 0,
        "parsed": 3,
        "failed": 0,
        "withdrawn": 2,
    }


def test_get_resume_status_breakdown_raises_not_found_on_404() -> None:
    client = _client_with(_json_handler(404, {"detail": "no such job"}))
    with pytest.raises(api_client.NotFound):
        api_client.get_resume_status_breakdown(uuid4(), client=client)


def test_get_resume_status_breakdown_raises_backend_unavailable_on_5xx() -> None:
    client = _client_with(_json_handler(503, {"detail": "down"}))
    with pytest.raises(api_client.BackendUnavailable):
        api_client.get_resume_status_breakdown(uuid4(), client=client)
