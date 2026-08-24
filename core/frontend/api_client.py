"""Thin sync HTTP client wrapping the FastAPI backend for the Flask viewer.

One function per consumed backend route (Phase 6's ``jobs``/``resumes``/
``shortlist`` routers). Every function accepts an optional keyword-only
``client: httpx.Client | None = None`` so callers (and tests) can inject an
``httpx.MockTransport``-backed client without any real network — when omitted,
:func:`build_client` constructs one from ``src.settings.Settings``.

**Redaction-boundary contract (mirrors the backend exactly):**

* ``list_shortlist``/``get_shortlist_entry`` take NO ``reveal`` parameter at
  all — shortlist reads are unconditionally blind, matching
  ``shortlist_service.list_for_job``/``get_one`` taking no such kwarg either.
* ``get_resume``/``export_shortlist`` take NO ``reveal`` parameter either
  (FU-4/D3 removed it from both backend routes) — the audited
  ``reveal_resume`` is the only un-blinding path.
* ``get_match_results`` has no redaction concept (ADR-012 §4 — the backend
  applies none here either).

Errors are mapped to a small typed hierarchy so the Flask route layer can
handle them without inspecting raw ``httpx`` exceptions: a backend 404 raises
``NotFound``; a backend 5xx or an ``httpx.ConnectError`` raises
``BackendUnavailable``. Both subclass ``BackendError``.
"""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

import httpx
from flask import has_request_context, request

from src.settings import get_settings

ExportFormat = Literal["csv", "evidence-csv", "json"]

# Explicit timeout so the client never relies on httpx's implicit default:
# 5s to establish a connection, 30s overall for connect/read/write/pool.
_HTTP_TIMEOUT = httpx.Timeout(30.0, connect=5.0)


class BackendError(Exception):
    """Base class for all typed backend-communication failures."""


class NotFound(BackendError):  # noqa: N818 — name is a fixed part of the
    # Phase 7 viewer contract (test_frontend_api_client.py imports
    # `api_client.NotFound` verbatim); it subclasses `BackendError` (which
    # already carries the "Error" suffix) so intentionally omits its own.
    """The backend responded 404 — the requested resource does not exist."""


class BackendUnavailable(BackendError):  # noqa: N818 — same rationale as
    # `NotFound` above: the name is pinned by the test contract.
    """The backend responded 5xx, or the connection itself failed."""


class BadRequest(BackendError):  # noqa: N818 — matches the `NotFound` /
    # `BackendUnavailable` naming convention (subclasses `BackendError`, which
    # already carries the "Error" suffix).
    """The backend rejected the request with a 4xx (e.g. 422 validation).

    Carries the backend ``status_code`` and its parsed ``detail`` body so the
    Flask route layer can re-render the offending form with a friendly message
    and the recruiter's inputs intact (no data loss).
    """

    def __init__(self, message: str, *, status_code: int, detail: Any = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.detail = detail


class Conflict(BadRequest):  # noqa: N818 — same naming convention as the
    # other typed errors (subclasses `BadRequest`/`BackendError`, which already
    # carry the "Error" suffix).
    """The backend responded 409 — an illegal state transition (e.g. an
    illegal job-status edge). A specialisation of :class:`BadRequest` so the
    route can surface it as a friendly message while still catching it as a
    generic 4xx if it wants to."""


def build_client() -> httpx.Client:
    """Build an ``httpx.Client`` bound to ``settings.api_base_url``.

    **FU-4/D6 — the viewer presents ONE FIXED role key outbound for every
    browser it serves: ``recruiter``.** The browser supplies no credential of
    its own; Flask attaches its own server-held key, so every viewer user has
    identical backend authority. Recruiter is the role the viewer's workflows
    actually need (job/résumé writes, audited reveal) without being admin.

    Attaches an ``X-API-Key`` header IFF ``settings.api_key_recruiter`` is
    non-empty — the client-side mirror of ``src.api.deps.resolve_role``'s
    auth-disabled-when-all-four-empty semantics, reduced to the ONE key Flask
    ever presents. Must not crash on a non-ASCII key (client-side mirror of
    ADR-012 §1's SEC-1 fix) — httpx's ``Headers`` defaults to ASCII-only
    encoding for ``str`` values and raises ``UnicodeEncodeError`` on a
    non-ASCII one, so the header value is explicitly UTF-8-encoded here
    (mirroring ``resolve_role`` comparing UTF-8 bytes server-side).

    **FU-5 slice 11 — also forwards the inbound browser session cookie.** The
    backend's CAS session (``settings.session_cookie_name``, ``ra_session`` by
    default) identifies WHO is acting, distinct from the fixed ``recruiter``
    role key above which only grants WHAT authority the viewer has — both are
    needed for ADR-019's human-attribution chain (``created_by``,
    reveal-audit-as-user) to be reachable from the UI. Only THIS one cookie is
    forwarded, read straight off the active Flask request (never from an
    arbitrary/unrelated inbound cookie) and attached to this client's own
    cookie jar so it rides on every request built through it — it is never
    forwarded anywhere but this client's configured ``settings.api_base_url``.
    Reading the inbound cookie is guarded by :func:`flask.has_request_context`
    so this function stays safe to call with no active browser request (e.g.
    a future non-browser caller).
    """
    settings = get_settings()
    headers: dict[str, str] = {}
    if settings.api_key_recruiter:
        headers["X-API-Key"] = settings.api_key_recruiter
    cookies: dict[str, str] = {}
    if has_request_context():
        session_cookie = request.cookies.get(settings.session_cookie_name)
        if session_cookie is not None:
            cookies[settings.session_cookie_name] = session_cookie
    return httpx.Client(
        base_url=settings.api_base_url,
        headers=httpx.Headers(headers, encoding="utf-8"),
        cookies=cookies,
        timeout=_HTTP_TIMEOUT,
    )


def _client_or_default(client: httpx.Client | None) -> tuple[httpx.Client, bool]:
    """Return ``(client, owns_it)`` — ``owns_it`` tells the caller whether to
    close the client after use (only when we built it ourselves)."""
    if client is not None:
        return client, False
    return build_client(), True


def _raise_for_status(response: httpx.Response) -> None:
    if response.status_code == 404:
        raise NotFound(f"backend 404: {response.request.url}")
    if response.status_code >= 500:
        raise BackendUnavailable(
            f"backend {response.status_code}: {response.request.url}"
        )
    if response.status_code >= 400:
        detail: Any
        try:
            detail = response.json()
        except ValueError:
            detail = response.text
        message = f"backend {response.status_code}: {response.request.url}"
        if response.status_code == 409:
            raise Conflict(message, status_code=response.status_code, detail=detail)
        raise BadRequest(message, status_code=response.status_code, detail=detail)


def _request(
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    json: Any | None = None,
    data: dict[str, Any] | None = None,
    files: Any | None = None,
    client: httpx.Client | None = None,
) -> httpx.Response:
    active, owns_it = _client_or_default(client)
    try:
        try:
            response = active.request(
                method, path, params=params, json=json, data=data, files=files
            )
        except httpx.ConnectError as exc:
            raise BackendUnavailable(f"connection failed: {exc}") from exc
        _raise_for_status(response)
        return response
    finally:
        if owns_it:
            active.close()


def list_jobs(
    *,
    limit: int = 50,
    offset: int = 0,
    status: str | None = None,
    client: httpx.Client | None = None,
) -> Any:
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    if status is not None:
        params["status"] = status
    response = _request("GET", "/jobs", params=params, client=client)
    return response.json()


def my_jobs(
    *,
    limit: int = 50,
    offset: int = 0,
    status: str | None = None,
    client: httpx.Client | None = None,
) -> Any:
    """GET /my/jobs — the hiring_manager's scoped view of THEIR assigned
    jobs (FU-6/ADR-020 §7). Mirrors :func:`list_jobs` exactly, including
    error mapping, just against the scoped backend route."""
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    if status is not None:
        params["status"] = status
    response = _request("GET", "/my/jobs", params=params, client=client)
    return response.json()


def create_job(payload: dict[str, Any], *, client: httpx.Client | None = None) -> Any:
    """POST /jobs (JSON ``JobCreate``). A backend 422 surfaces as
    ``BadRequest`` so the route can re-render the form with inputs intact."""
    response = _request("POST", "/jobs", json=payload, client=client)
    return response.json()


def extract_jd(
    filename: str,
    content: bytes,
    content_type: str,
    *,
    client: httpx.Client | None = None,
) -> Any:
    """POST /jobs/jd-extract (multipart ``file=``) → ``{filename,text,chars}``."""
    files = {"file": (filename, content, content_type)}
    response = _request("POST", "/jobs/jd-extract", files=files, client=client)
    return response.json()


def bulk_create_jobs(
    files: list[tuple[str, bytes, str]],
    *,
    manifest: tuple[str, bytes, str] | None = None,
    client: httpx.Client | None = None,
) -> Any:
    """POST /jobs/bulk (multipart) → ``list[BulkJobResult]``.

    ``files`` is a list of ``(filename, content, content_type)`` tuples,
    forwarded as repeated ``files=`` parts (a ``.zip`` is expanded *server*-side
    — we only forward the raw bytes, never expand it here). An optional
    ``manifest`` (filename, content, content_type) is forwarded as its OWN
    ``manifest`` part (the CSV metadata sidecar). A backend 4xx (e.g. a 422 for
    a malformed manifest) surfaces as ``BadRequest``."""
    multipart = [
        ("files", (filename, content, ctype)) for filename, content, ctype in files
    ]
    if manifest is not None:
        multipart.append(("manifest", manifest))
    response = _request("POST", "/jobs/bulk", files=multipart, client=client)
    return response.json()


def get_job(job_id: UUID, *, client: httpx.Client | None = None) -> Any:
    response = _request("GET", f"/jobs/{job_id}", client=client)
    return response.json()


def transition_status(
    job_id: UUID, to: str, *, client: httpx.Client | None = None
) -> Any:
    """PATCH /jobs/{id}/status (JSON ``{to}``). A backend 409 (illegal
    transition) surfaces as ``Conflict`` so the route can show a friendly
    message."""
    response = _request(
        "PATCH", f"/jobs/{job_id}/status", json={"to": to}, client=client
    )
    return response.json()


def patch_job(
    job_id: UUID, payload: dict[str, Any], *, client: httpx.Client | None = None
) -> Any:
    """PATCH /jobs/{id} (partial JSON, e.g. ``{"blind_review": false}``)."""
    response = _request("PATCH", f"/jobs/{job_id}", json=payload, client=client)
    return response.json()


def reparse_job(job_id: UUID, *, client: httpx.Client | None = None) -> Any:
    """POST /jobs/{id}/reparse — re-queue a failed or stranded JD parse.

    A backend 409 (the job has left 'draft', so ``parse_job`` would drop the
    work as stale) surfaces as ``Conflict`` so the route can say that plainly
    rather than redirecting as though it had queued something."""
    response = _request("POST", f"/jobs/{job_id}/reparse", client=client)
    return response.json()


def list_resumes(job_id: UUID, *, client: httpx.Client | None = None) -> Any:
    response = _request("GET", f"/jobs/{job_id}/resumes", client=client)
    return response.json()


def list_audit_log(
    *,
    limit: int = 100,
    offset: int = 0,
    action: str | None = None,
    client: httpx.Client | None = None,
) -> Any:
    """Phase 1.4 / ADR-036 — the auditor's read of the live ``audit_log``.

    The backend gates this on the CAS SESSION role (admin/auditor), not on the
    fixed ``recruiter`` key this client presents — see ``build_client``. So the
    forwarded session cookie is what authorizes the call, and an auditor who is
    not signed in gets a 403 rather than someone else's view of the record.
    """
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    if action:
        params["action"] = action
    response = _request("GET", "/audit/log", params=params, client=client)
    return response.json()


def reveal_audit_detail(
    audit_id: Any,
    *,
    context: str | None = None,
    client: httpx.Client | None = None,
) -> Any:
    """D1 = option C — reveal ONE withheld ``audit_log.details`` payload.

    The backend records who asked, for which row, before it answers; a 403 here
    means either the session is not an auditor/admin or the row's action is not
    on the revealable allowlist, and the caller must render that rather than
    letting it become a 500.

    Like :func:`list_audit_log` this is authorized by the forwarded session
    cookie, not by the fixed ``recruiter`` key this client presents.
    """
    params: dict[str, Any] = {}
    if context:
        params["context"] = context
    response = _request(
        "POST", f"/audit/log/{audit_id}/reveal", params=params, client=client
    )
    return response.json()


def upload_resumes(
    job_id: UUID,
    files: list[tuple[str, bytes, str]],
    *,
    consent_acknowledged: bool,
    cover_letter_text: str | None = None,
    cover_letter_file: tuple[str, bytes, str] | None = None,
    pairing_manifest: tuple[str, bytes, str] | None = None,
    client: httpx.Client | None = None,
) -> Any:
    """POST /jobs/{id}/resumes (multipart).

    ``files`` is a list of ``(filename, content, content_type)`` tuples,
    forwarded as repeated ``files=`` parts (a ``.zip`` is expanded *server*-side
    — we only forward the raw bytes, never expand it here). ``consent_acknowledged``
    is sent as the string ``"true"``/``"false"`` form field the backend expects
    (it accepts iff ``.strip().lower() == "true"``). An optional
    ``cover_letter_text`` form field is included only when provided; an optional
    ``cover_letter_file`` (filename, content, content_type) is forwarded as a
    ``cover_letter_file`` part (the backend stores it as a blob and parses it at
    parse time). An optional ``pairing_manifest`` (filename, content,
    content_type) is forwarded as its OWN ``pairing_manifest`` part — the backend
    pairs each résumé with its cover by name (never inside the résumé zip). A
    backend 4xx surfaces as ``BadRequest``."""
    multipart = [
        ("files", (filename, content, ctype)) for filename, content, ctype in files
    ]
    if cover_letter_file is not None:
        multipart.append(("cover_letter_file", cover_letter_file))
    if pairing_manifest is not None:
        multipart.append(("pairing_manifest", pairing_manifest))
    form: dict[str, Any] = {
        "consent_acknowledged": "true" if consent_acknowledged else "false"
    }
    if cover_letter_text is not None:
        form["cover_letter_text"] = cover_letter_text
    response = _request(
        "POST",
        f"/jobs/{job_id}/resumes",
        data=form,
        files=multipart,
        client=client,
    )
    return response.json()


def generate_shortlist(job_id: UUID, *, client: httpx.Client | None = None) -> Any:
    """POST /jobs/{id}/shortlist — enqueues the ranking job. Returns the
    enqueue ack ``{job_id, status: "enqueued"}`` (results appear
    asynchronously; the caller polls ``list_shortlist`` for them)."""
    response = _request("POST", f"/jobs/{job_id}/shortlist", client=client)
    return response.json()


def list_shortlist(job_id: UUID, *, client: httpx.Client | None = None) -> Any:
    response = _request("GET", f"/jobs/{job_id}/shortlist", client=client)
    return response.json()


def get_shortlist_entry(entry_id: UUID, *, client: httpx.Client | None = None) -> Any:
    response = _request("GET", f"/shortlist/{entry_id}", client=client)
    return response.json()


def get_shortlist_status(job_id: UUID, *, client: httpx.Client | None = None) -> Any:
    """GET /jobs/{id}/shortlist/status (FU-7 §2) — the fail-closed ranking
    state ``{job_id, state, reason, at}``. ``state`` is ``"awaiting_llm"`` when
    a run failed closed and a retry is queued, else ``None``. A backend 404
    (missing job) surfaces as ``NotFound``; a 5xx as ``BackendUnavailable``."""
    response = _request("GET", f"/jobs/{job_id}/shortlist/status", client=client)
    return response.json()


def get_resume(resume_id: UUID, *, client: httpx.Client | None = None) -> Any:
    """GET /resumes/{id} — unconditionally blind.

    FU-4/D3 removed ``reveal`` from the BACKEND route entirely, so this client
    takes no such kwarg either: an accepted-but-ignored parameter would read
    like it works. The audited :func:`reveal_resume` is the only un-blinding
    path.
    """
    response = _request("GET", f"/resumes/{resume_id}", client=client)
    return response.json()


def reveal_resume(
    resume_id: UUID,
    *,
    context: str | None = None,
    client: httpx.Client | None = None,
) -> Any:
    """AUDITED de-anonymization. ``POST /resumes/{id}/reveal`` records a reveal
    audit row server-side and returns the UN-blinded ``ResumeOut``. This is the
    ONLY path the viewer reveals through — ``GET ?reveal=true`` would skip the
    audit, so it is never used here."""
    params = {"context": context} if context is not None else None
    response = _request(
        "POST", f"/resumes/{resume_id}/reveal", params=params, client=client
    )
    return response.json()


def trigger_reverse_match(
    resume_id: UUID, *, client: httpx.Client | None = None
) -> Any:
    """POST /resumes/{id}/match-jobs — enqueues the reverse-match job. Returns
    the 202 enqueue ack ``{resume_id, status: "enqueued"}`` (results appear
    asynchronously; the caller polls ``get_match_results`` for them). This path
    has NO redaction concept (ADR-012 §4 — the caller owns the résumé, and jobs
    are not personal data), so it takes no ``reveal`` kwarg. A backend 404
    surfaces as ``NotFound``; a 5xx (or ``ConnectError``) as
    ``BackendUnavailable``."""
    response = _request("POST", f"/resumes/{resume_id}/match-jobs", client=client)
    return response.json()


def get_match_results(resume_id: UUID, *, client: httpx.Client | None = None) -> Any:
    response = _request("GET", f"/resumes/{resume_id}/match-results", client=client)
    return response.json()


def list_users(*, client: httpx.Client | None = None) -> Any:
    """GET /users — the admin-only user roster (user-admin-roles slice 6/7).
    Same ``_request``/error-mapping plumbing as :func:`list_jobs`."""
    response = _request("GET", "/users", client=client)
    return response.json()


def set_user_role(
    user_id: UUID, role: str, *, client: httpx.Client | None = None
) -> Any:
    """PATCH /users/{id}/role (JSON ``{role}``) — assign a new role to a user.
    A backend 409 (last-admin lockout) surfaces as ``Conflict`` so the route
    can show a friendly message; a 404 as ``NotFound``; other 4xx (e.g. an
    invalid role) as ``BadRequest`` — identical error mapping to
    :func:`transition_status`."""
    response = _request(
        "PATCH", f"/users/{user_id}/role", json={"role": role}, client=client
    )
    return response.json()


def get_cas_user(*, client: httpx.Client | None = None) -> Any:
    """GET /auth/cas/user — the CAS session-status endpoint (slice 6). Never
    401s server-side (returns ``{authenticated: False, ...}`` when there is no
    valid session), so the ONLY typed failure this raises is
    ``BackendUnavailable`` (a backend 5xx or a connect error) — consumed by
    the frontend auth gate (``frontend.app``) to decide whether to redirect to
    CAS login."""
    response = _request("GET", "/auth/cas/user", client=client)
    return response.json()


def withdraw_resume(
    resume_id: UUID,
    *,
    reason: str | None = None,
    client: httpx.Client | None = None,
) -> Any:
    """AUDITED. ``POST /resumes/{id}/withdraw`` (ADR-026/FU-8) — marks the
    résumé withdrawn (excluded from shortlisting, retained for audit — the
    "exclude-and-retain" decision) and returns the updated ``ResumeOut``.
    Mirrors :func:`reveal_resume`'s shape: a dedicated per-résumé POST route,
    the same typed error mapping."""
    response = _request(
        "POST",
        f"/resumes/{resume_id}/withdraw",
        json={"reason": reason},
        client=client,
    )
    return response.json()


def reinstate_resume(resume_id: UUID, *, client: httpx.Client | None = None) -> Any:
    """AUDITED. ``POST /resumes/{id}/reinstate`` (ADR-026/FU-8) — clears the
    withdrawal, returning the résumé to shortlist eligibility, and returns the
    updated ``ResumeOut``. No body required."""
    response = _request("POST", f"/resumes/{resume_id}/reinstate", client=client)
    return response.json()


def get_resume_status_breakdown(
    job_id: UUID, *, client: httpx.Client | None = None
) -> Any:
    """``GET /jobs/{id}/resume-status`` (ADR-026 decision 5/FU-8) — the
    per-job résumé status-breakdown widget's data source: a 5-bucket count
    dict (``uploaded``/``parsing``/``parsed``/``failed``/``withdrawn``)."""
    response = _request("GET", f"/jobs/{job_id}/resume-status", client=client)
    return response.json()


def export_shortlist(
    job_id: UUID,
    *,
    format: ExportFormat = "csv",
    client: httpx.Client | None = None,
) -> httpx.Response:
    """Returns the raw ``httpx.Response`` so the Flask route can proxy
    ``Content-Disposition``/body straight through without re-encoding it.

    FU-4/D3 dropped ``reveal`` from the backend export route — that was an
    UNAUDITED bulk de-anonymization across a whole shortlist — so exports are
    unconditionally blind and this client takes no such kwarg."""
    return _request(
        "GET",
        f"/jobs/{job_id}/shortlist/export",
        params={"format": format},
        client=client,
    )


__all__ = [
    "BackendError",
    "NotFound",
    "BackendUnavailable",
    "BadRequest",
    "Conflict",
    "build_client",
    "list_jobs",
    "my_jobs",
    "create_job",
    "extract_jd",
    "bulk_create_jobs",
    "get_job",
    "transition_status",
    "patch_job",
    "list_resumes",
    "upload_resumes",
    "generate_shortlist",
    "list_shortlist",
    "get_shortlist_entry",
    "get_shortlist_status",
    "get_resume",
    "reveal_resume",
    "withdraw_resume",
    "reinstate_resume",
    "get_resume_status_breakdown",
    "trigger_reverse_match",
    "get_match_results",
    "export_shortlist",
    "list_users",
    "set_user_role",
    "get_cas_user",
]
