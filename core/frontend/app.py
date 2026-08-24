"""Flask frontend — Phase 7 read-only viewer.

Talks to the FastAPI backend over HTTP via ``frontend.api_client``. Every
route renders server-side only (Jinja2 templates, no client-side JS that
re-fetches raw/reveal endpoints) — the redaction boundary established by the
backend (ADR-011/012: résumés and shortlist entries are redacted server-side
before ever leaving the FastAPI process) is enforced HERE too, at the second
hop: this module never forwards a browser-supplied ``?reveal=`` query string
to the backend, and the shortlist list/detail routes never pass a ``reveal``
kwarg to ``api_client`` at all (mirroring ``shortlist_service`` itself taking
no such parameter).
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode
from uuid import UUID

from flask import (
    Flask,
    Response,
    abort,
    flash,
    g,
    redirect,
    render_template,
    request,
    url_for,
)
from pydantic import ValidationError

from frontend import api_client, csrf
from src.schemas.matching import ShortlistEntry
from src.services.explanation import ShortlistExplanation, shortlist_entry_explanation
from src.settings import get_settings

logger = logging.getLogger(__name__)

# The known ShortlistEntry field names, computed once at import time. Any key
# on the raw dict from ``api_client.get_shortlist_entry`` that is NOT one of
# these is dropped before validation.
#
# WHAT THIS IS FOR: ROBUSTNESS, not PII control. ``ShortlistEntry`` is
# ``extra="forbid"``, so the day the backend adds a field to the shortlist
# payload, an unfiltered ``model_validate`` would start raising and the
# explanation panel would silently vanish from every entry page until the
# frontend caught up. Dropping unknown keys first means a forward-compatible
# payload still renders.
#
# WHAT IT IS NOT: it is NOT the redaction boundary. That is enforced
# server-side by ``shortlist_service._row_to_blind_entry`` BEFORE the DTO is
# ever built (ADR-006 §4/ADR-011/012) -- this hop is downstream of it and
# cannot un-leak anything the backend already sent. The reason a stray
# ``candidate`` blob cannot reach the template is not this whitelist, it is
# that the template is handed the VALIDATED DTO (below), which structurally
# has no field to carry one.
_SHORTLIST_ENTRY_FIELDS = frozenset(ShortlistEntry.model_fields)


@dataclass(frozen=True)
class _EntryHeader:
    """The bare identity block (rank / label / final score / résumé link) for
    an entry payload that FAILED ``ShortlistEntry`` validation.

    The template renders the same four attribute names off either this or a
    real ``ShortlistEntry``, so the degraded page stays identical to the
    pre-slice stub without the template ever touching a raw dict. Every field
    is defensively coerced -- a malformed payload is exactly the case this
    exists for, so it must not itself raise."""

    rank: int | None
    display_label: str | None
    score_final: float | None
    resume_id: UUID | None


def _entry_header(raw: dict[str, Any]) -> _EntryHeader:
    rank = raw.get("rank")
    label = raw.get("display_label")
    score = raw.get("score_final")
    raw_resume_id = raw.get("resume_id")
    resume_id: UUID | None = None
    if raw_resume_id is not None:
        try:
            resume_id = UUID(str(raw_resume_id))
        except (ValueError, TypeError):
            resume_id = None
    # bool is a subclass of int/float here, and "True" is not a rank.
    score_final: float | None = None
    if isinstance(score, (int, float)) and not isinstance(score, bool):
        score_final = float(score)
    return _EntryHeader(
        rank=rank if isinstance(rank, int) and not isinstance(rank, bool) else None,
        display_label=label if isinstance(label, str) else None,
        score_final=score_final,
        resume_id=resume_id,
    )


# The backend caps résumé uploads at ~10 MB/file and up to 20 files per
# request (`src.api.routes.resumes`), plus the (much smaller) JD-extract
# upload. This is a defensive total-body cap so an oversized multipart
# request is rejected by Werkzeug with 413 BEFORE it is buffered into Flask
# process memory, rather than after the backend's own per-file/file-count
# limits have a chance to run.
MAX_UPLOAD_BYTES = 210 * 1024 * 1024  # 20 files * 10 MB + headroom

_settings = get_settings()
app = Flask(__name__)
app.secret_key = _settings.flask_secret_key
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES
API = _settings.api_base_url


# Routes reachable with no CAS session even when `cas_enabled=True` — just
# the liveness probe, so orchestration/health-checks never need a session.
_CAS_GATE_EXEMPT_PATHS = frozenset({"/health"})


@app.before_request
def _cas_auth_gate() -> Any:
    """FU-5 slice 11 (ADR-019 §10/§3/§10b) — gate unauthenticated browser
    access when CAS is enabled.

    Reads settings via a FRESH :func:`get_settings` call every request (never
    the frozen ``_settings`` captured once at import time below) so tests —
    and a real config reload — can flip ``cas_enabled`` without restarting the
    process. ``cas_enabled=False`` is an unconditional passthrough (dev mode,
    §10b) with NO call to the backend at all.

    When enabled, delegates the actual session check to the backend's own
    ``GET /auth/cas/user`` (never 401s) via :func:`api_client.get_cas_user` —
    this hook holds no session-validity logic of its own, it only acts on the
    backend's answer. A backend that cannot be reached fails CLOSED (503),
    never silently lets the request through as if it were authenticated.
    """
    settings = get_settings()
    if not settings.cas_enabled:
        return None
    if request.path in _CAS_GATE_EXEMPT_PATHS:
        return None
    try:
        status = api_client.get_cas_user()
    except api_client.BackendUnavailable as exc:
        return _unavailable(exc)
    g.cas_user = status
    if status.get("authenticated"):
        if status.get("role") is None:
            return render_template("pending_access.html"), 200
        return None
    login_url = (
        f"{settings.cas_service_base_url.rstrip('/')}/auth/cas/login?"
        + urlencode({"next": request.path})
    )
    return redirect(login_url)


#: Endpoints the CSRF hook below does NOT guard, because each already carries a
#: STRICTLY STRONGER control of its own: FU-4/D4's per-résumé, per-action
#: ONE-SHOT token (``csrf.verify_and_consume``), checked inside the view before
#: any backend call. They are exempt rather than double-guarded — stacking the
#: session-wide page token on top would mean rendering two tokens into one form
#: for no security gain, and would weaken nothing but clarity.
#:
#: This mirrors ADR-033's exemption discipline (``PATCH /users/{id}/role`` is
#: exempt from ``require_session_role`` because ``_require_admin_session`` is
#: already narrower). Like that one, the set is ASSERTED by a test
#: (``test_frontend_csrf_write_route_enforcement.py``), because an exemption
#: list is the natural place for a control like this to rot: adding an endpoint
#: here is a one-line change that silently disables the guard for it.
#:
#: An exemption is not a downgrade: the same test file proves a page token does
#: NOT open a reveal, so these routes accept only their own one-shot tokens.
_CSRF_HOOK_EXEMPT_ENDPOINTS = frozenset(
    {"resume_reveal", "resume_withdraw", "resume_reinstate"}
)

_STATE_CHANGING_METHODS = frozenset({"POST", "PATCH", "PUT", "DELETE"})


@app.before_request
def _csrf_gate() -> Any:
    """Phase 1.3 (ROADMAP A1 step (iv)) — anti-forgery on EVERY state-changing
    browser route, enforced centrally and fail-closed.

    **Why a hook and not a per-route decorator.** FU-4/D4 built a sound
    anti-forgery control and wired it to three routes; the other nine POST
    routes were never guarded, including ``admin_set_user_role`` (privilege
    escalation: a forged auto-submit from an admin's browser promotes an
    attacker's account) and ``blind_review`` (the same un-blinding flip the
    ADR-034 exploit used, reached here through a real recruiter's session
    instead of through no session at all). Opt-in protection is the ROADMAP A7
    defect shape — an invariant with nothing enforcing it — so this is opt-OUT:
    a new POST route is protected the moment it is added, and removing that
    protection requires an explicit, tested entry in
    :data:`_CSRF_HOOK_EXEMPT_ENDPOINTS`.

    **ADR-034 made this more important, not less.** Every backend write now
    demands a real CAS session, so the session cookie the browser attaches to a
    forged cross-site request is exactly what makes the forgery succeed. Flask
    still cannot tell a forged auto-submit from a genuine click on its own: the
    browser sends no credential of its own to the Flask hop, and Flask attaches
    its OWN server-held API key on the outbound leg (``api_client
    .build_client``).

    Rejects with 403 BEFORE the view runs, so a forgery can never reach the
    backend and cause the effect it intends. Runs after ``_cas_auth_gate``
    (registration order), so an unauthenticated browser is still redirected to
    login rather than shown a bare 403.
    """
    if request.method not in _STATE_CHANGING_METHODS:
        return None
    if request.endpoint in _CSRF_HOOK_EXEMPT_ENDPOINTS:
        return None
    if not csrf.same_origin(request):
        abort(403)
    if not csrf.verify_page_token(csrf.token_from_request(request)):
        abort(403)
    return None


_WRITER_ROLES = ("admin", "recruiter")


@app.context_processor
def inject_current_user() -> dict[str, Any]:
    """Injects the header auth widget's context into every template render.

    ``current_user`` is the ``g.cas_user`` status dict stashed by
    ``_cas_auth_gate`` above (reused, never re-fetched) — ``None`` when CAS is
    disabled (dev mode, no gate call at all). ``logout_url``/``login_url`` are
    built from a FRESH :func:`get_settings` call, mirroring the gate's own
    settings-reload discipline.

    ``is_writer`` (security finding fix/auth-boundary-fails-open, F4,
    defence in depth): every backend write route now 403s a CAS session
    whose role is not ``admin``/``recruiter`` (ADR-033 + this fix's
    ``require_session_role`` reversal) — this mirrors that same allowed set
    so templates can hide a write control for a session that could only ever
    see a 403 anyway. ``current_user is None`` (CAS disabled — the
    dev-anonymous sentinel is admin-equivalent) is a writer. This is NOT the
    actual authorization boundary (the backend gate is); it is a compensating
    UX control only.
    """
    settings = get_settings()
    base = settings.cas_service_base_url.rstrip("/")
    current_user = getattr(g, "cas_user", None)
    is_writer = current_user is None or current_user.get("role") in _WRITER_ROLES
    return {
        "current_user": current_user,
        "is_writer": is_writer,
        "logout_url": f"{base}/auth/cas/logout",
        "login_url": f"{base}/auth/cas/login?next=/",
        # Phase 1.3: minted here rather than per-route so EVERY render carries
        # it — a template that grows a new form cannot forget to ask for one,
        # and `issue_page_token` is idempotent, so this costs one session read
        # on all but the first render of a session.
        "csrf_page_token": csrf.issue_page_token(),
    }


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def _unavailable(exc: api_client.BackendUnavailable) -> Any:
    return render_template("error.html"), 503


_ASSIGNABLE_ROLES = ("admin", "recruiter", "hiring_manager", "auditor")


def _require_admin_page() -> None:
    """user-admin-roles slice 7 — the admin-only gate shared by the
    ``/admin/users`` GET/POST routes, mirroring the backend's own
    ``_require_admin_session`` (``src/api/routes/users.py``).

    ``cas_enabled=False`` (dev mode): unconditional passthrough, with NO call
    to ``api_client.get_cas_user`` — dev-anonymous IS the backend's own
    synthetic admin sentinel, so the page is always reachable in dev mode
    (mirrors ``index()``'s "only call ``get_cas_user`` when ``cas_enabled``"
    discipline).

    ``cas_enabled=True``: reuses the status ``_cas_auth_gate`` already
    stashed on ``flask.g.cas_user`` for THIS request — never re-fetched.
    A ``role`` other than ``"admin"`` aborts 403 before any
    ``list_users``/``set_user_role`` call. A ``role=None`` session never
    reaches here at all: ``_cas_auth_gate`` already intercepts it with
    ``pending_access.html`` before this route's body ever runs.
    """
    settings = get_settings()
    if not settings.cas_enabled:
        return None
    cas_status = getattr(g, "cas_user", None) or {}
    if cas_status.get("role") != "admin":
        abort(403)
    return None


#: The roles the audit-log viewer serves, matching the backend's own
#: ``_AUDIT_READERS`` on ``GET /audit/log``. Admin is included because an
#: administrator investigating a report should not have to be re-roled to see
#: the record they are being asked about.
_AUDIT_PAGE_ROLES = ("admin", "auditor")

#: One page of audit rows. Well inside the backend's ``le=500`` bound.
_AUDIT_PAGE_SIZE = 100

#: The ``action`` values ``record_audit`` writes today, offered as a filter.
#: A value NOT in this list is still accepted and forwarded — the list is a
#: convenience for the reader, never a whitelist that could silently hide a
#: newly-added action from an auditor.
_AUDIT_ACTIONS = (
    "reveal",
    "reveal_audit_detail",
    "withdraw_resume",
    "reinstate_resume",
    "role_changed",
    "assign_job",
    "unassign_job",
)

#: The marker the BACKEND substitutes for a value it will not disclose
#: (``audit_service.WITHHELD``). The page compares against it to decide where to
#: offer the audited-reveal control — it is a rendering cue, never a second
#: implementation of the disclosure rule, which lives only in the backend.
_AUDIT_WITHHELD_MARKER = "<withheld>"


def _require_audit_page() -> None:
    """Phase 1.4 / ADR-036 — the admin+auditor gate on the audit-log page.

    Mirrors :func:`_require_admin_page` exactly, one role wider. Like it, this
    is a **compensating UX control, not the authorization boundary**: the
    backend's ``require_session_role(ADMIN, AUDITOR)`` on ``GET /audit/log`` is
    what actually protects the data, and it re-checks the same session this page
    read. Gating here only means a recruiter gets a comprehensible 403 instead
    of an empty page wrapped around a backend refusal.

    ``cas_enabled=False`` (dev mode) is an unconditional passthrough, matching
    ``_require_admin_page``: dev-anonymous IS the backend's synthetic admin
    sentinel, so the page stays reachable with no CAS server.
    """
    settings = get_settings()
    if not settings.cas_enabled:
        return None
    cas_status = getattr(g, "cas_user", None) or {}
    if cas_status.get("role") not in _AUDIT_PAGE_ROLES:
        abort(403)
    return None


_JOB_STATUSES = ("draft", "open", "closed", "archived")


@app.get("/")
def index() -> Any:
    """FU-6/ADR-020 §7 default-view switch: a hiring_manager sees THEIR
    assigned jobs (``GET /my/jobs``) by default; every other role (and the
    CAS-disabled dev-anonymous default) keeps the global ``GET /jobs`` list,
    unchanged.

    Reads a FRESH :func:`get_settings` every request (mirroring
    ``_cas_auth_gate`` above) and calls :func:`api_client.get_cas_user` ONLY
    when ``cas_enabled`` is True — an unconditional call would turn every
    existing CAS-disabled test (which relies on ``get_cas_user`` never being
    called) into a real, unmocked outbound HTTP attempt.
    """
    status = request.args.get("status") or None
    settings = get_settings()
    scoped_view = False
    if settings.cas_enabled:
        cas_status = api_client.get_cas_user()
        scoped_view = cas_status.get("role") == "hiring_manager"
    try:
        if scoped_view:
            jobs = api_client.my_jobs(status=status)
        else:
            jobs = api_client.list_jobs(status=status)
    except api_client.BackendUnavailable as exc:
        return _unavailable(exc)
    return render_template(
        "index.html",
        jobs=jobs,
        statuses=_JOB_STATUSES,
        status_filter=status,
        form={},
        errors=None,
        show_form=False,
        scoped_view=scoped_view,
    )


def _job_create_payload(form: Any) -> dict[str, Any]:
    """Build the ``JobCreate`` dict from the submitted form. Empty optional
    fields collapse to ``None``; the Blind-review checkbox is present in the
    form IFF checked (default checked) — absence means the recruiter opted
    out."""
    min_years_raw = (form.get("min_years") or "").strip()
    min_years: int | None
    try:
        min_years = int(min_years_raw) if min_years_raw else None
    except ValueError:
        min_years = None
    shortlist_top_percent_raw = (form.get("shortlist_top_percent") or "").strip()
    shortlist_top_percent: int
    try:
        shortlist_top_percent = (
            int(shortlist_top_percent_raw) if shortlist_top_percent_raw else 100
        )
    except ValueError:
        shortlist_top_percent = 100
    return {
        "title": (form.get("title") or "").strip(),
        "department": (form.get("department") or "").strip() or None,
        "location": (form.get("location") or "").strip() or None,
        "min_years": min_years,
        "description_raw": form.get("description_raw") or "",
        "blind_review": "blind_review" in form,
        "shortlist_top_percent": shortlist_top_percent,
    }


@app.post("/jobs/jd-extract")
def jd_extract() -> Any:
    """Proxy a JD upload to the backend extractor and return the extracted
    text as the HTMX swap fragment that prefills the ``#description``
    textarea."""
    upload = request.files.get("file")
    if upload is None:
        abort(400)
    try:
        result = api_client.extract_jd(
            upload.filename or "upload",
            upload.read(),
            upload.content_type or "application/octet-stream",
        )
    except api_client.BadRequest:
        return "Could not extract text from this file.", 200
    except api_client.BackendUnavailable as exc:
        return _unavailable(exc)
    return str(result.get("text", ""))


@app.post("/jobs")
def create_job() -> Any:
    payload = _job_create_payload(request.form)
    try:
        job = api_client.create_job(payload)
    except api_client.BadRequest as exc:
        try:
            jobs = api_client.list_jobs()
        except api_client.BackendUnavailable as unavail:
            return _unavailable(unavail)
        return (
            render_template(
                "index.html",
                jobs=jobs,
                statuses=_JOB_STATUSES,
                status_filter=None,
                form=request.form,
                errors=_format_error(exc.detail),
                show_form=True,
            ),
            200,
        )
    except api_client.BackendUnavailable as exc:
        return _unavailable(exc)
    return redirect(url_for("job_detail", job_id=job["id"]))


@app.post("/jobs/bulk")
def bulk_create_jobs() -> Any:
    """Bulk-JD upload: many JD files (or a ``.zip``) plus an optional CSV
    metadata manifest, forwarded to the backend which creates ONE draft job per
    file. Renders a created/duplicate/failed summary with a link back to the
    jobs list. The ``.zip`` is expanded SERVER-side by the backend — we only
    forward the raw parts, never expand here."""
    uploads = request.files.getlist("files")
    files: list[tuple[str, bytes, str]] = [
        (
            upload.filename or "upload",
            upload.read(),
            upload.content_type or "application/octet-stream",
        )
        for upload in uploads
        if upload.filename
    ]
    manifest_upload = request.files.get("manifest")
    manifest: tuple[str, bytes, str] | None = None
    if manifest_upload is not None and manifest_upload.filename:
        manifest = (
            manifest_upload.filename,
            manifest_upload.read(),
            manifest_upload.content_type or "text/csv",
        )
    try:
        results = api_client.bulk_create_jobs(files, manifest=manifest)
    except api_client.BadRequest as exc:
        return (
            render_template(
                "jobs_bulk.html", results=[], error=_format_error(exc.detail)
            ),
            200,
        )
    except api_client.BackendUnavailable as exc:
        return _unavailable(exc)
    return render_template(
        "jobs_bulk.html", results=results, summary=_summarise_bulk(results), error=None
    )


def _summarise_bulk(results: Any) -> dict[str, int]:
    """Created/duplicate/failed counts for the bulk-JD result summary — mirrors
    the résumé-upload ``_summarise_upload`` counting pattern."""
    rows = results if isinstance(results, list) else []
    return {
        "created": sum(1 for r in rows if r.get("outcome") == "created"),
        "duplicate": sum(1 for r in rows if r.get("outcome") == "duplicate"),
        "failed": sum(1 for r in rows if r.get("outcome") == "failed"),
    }


def _format_error(detail: Any) -> str:
    """Render a backend validation ``detail`` into a short human message.

    FastAPI/pydantic 422 bodies carry ``detail`` as a list of error dicts
    (``{"type": ..., "loc": [...], "msg": ..., "input": ...}``) — never show
    that raw ``repr`` to a recruiter; join the human ``msg`` fields instead.
    """
    if detail is None:
        return "Please correct the highlighted fields and try again."
    if isinstance(detail, list):
        messages = [
            str(item["msg"])
            for item in detail
            if isinstance(item, dict) and "msg" in item
        ]
        if messages:
            return "The upload was rejected: " + "; ".join(messages)
        return "Please correct the highlighted fields and try again."
    if isinstance(detail, dict):
        inner = detail.get("detail", detail)
        return str(inner)
    return str(detail)


# Legal job-status edges (mirrors the backend's transition guard):
# draft→{open,archived}, open→{closed,archived}, closed→{archived}.
_LEGAL_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "draft": ("open", "archived"),
    "open": ("closed", "archived"),
    "closed": ("archived",),
    "archived": (),
}
_TRANSITION_LABELS: dict[str, str] = {
    "open": "Open for applicants",
    "closed": "Close",
    "archived": "Archive",
}


# A résumé row is "terminal" once the backend has finished parsing it (or
# given up). While ANY row is still uploaded/parsing the résumés table keeps
# its HTMX poll trigger; once every row is terminal the trigger is dropped so
# the browser stops polling.
_TERMINAL_RESUME_STATUSES = ("parsed", "failed")
# Defensive cap on the free-text cover letter before it ever hits the network.
_MAX_COVER_LETTER_CHARS = 20000
# Bound the shortlist "Generating…" poll so a job that never yields ranked rows
# (e.g. Generate clicked before any résumé parsed) stops after ~20 min at 3s/poll
# with a give-up message, instead of polling forever. Matches hris's safety valve.
_MAX_SHORTLIST_POLL_ATTEMPTS = 400
# Bound the reverse-match "Finding matching jobs…" poll with the same safety
# valve/cap as the shortlist poll: reverse_match_job runs asynchronously, so a
# résumé whose run never lands (or a stack with no worker) stops after the cap
# with a give-up message instead of polling forever.
_MAX_MATCH_POLL_ATTEMPTS = 400


def _any_resume_pending(resumes: list[dict[str, Any]]) -> bool:
    return any(r.get("status") not in _TERMINAL_RESUME_STATUSES for r in resumes)


def _any_resume_parsed(resumes: list[dict[str, Any]]) -> bool:
    return any(r.get("status") == "parsed" for r in resumes)


def _render_job_detail(
    job_id: UUID, *, error: str | None = None, status_code: int = 200
) -> Any:
    try:
        job = api_client.get_job(job_id)
        resumes = api_client.list_resumes(job_id)
    except api_client.NotFound:
        abort(404)
    except api_client.BackendUnavailable as exc:
        return _unavailable(exc)
    next_states = _LEGAL_TRANSITIONS.get(job.get("status", ""), ())
    return (
        render_template(
            "job_detail.html",
            job=job,
            resumes=resumes,
            resumes_pending=_any_resume_pending(resumes),
            next_states=next_states,
            transition_labels=_TRANSITION_LABELS,
            error=error,
        ),
        status_code,
    )


@app.post("/jobs/<uuid:job_id>/resumes")
def upload_resumes(job_id: UUID) -> Any:
    """Multipart résumé upload. Consent is MANDATORY: if the recruiter did not
    tick the consent checkbox we do NOT call the backend at all — we re-render
    the job detail with an error, so no candidate bytes ever leave the browser
    without an explicit PIPEDA/FIPPA acknowledgement."""
    consent = (request.form.get("consent_acknowledged") or "").strip().lower() == "true"
    if not consent:
        return _render_job_detail(
            job_id,
            error="You must confirm the candidate consented to this processing.",
            status_code=400,
        )
    uploads = request.files.getlist("files")
    files: list[tuple[str, bytes, str]] = [
        (
            upload.filename or "upload",
            upload.read(),
            upload.content_type or "application/octet-stream",
        )
        for upload in uploads
        if upload.filename
    ]
    if not files:
        return _render_job_detail(
            job_id,
            error="Select at least one résumé file (PDF/DOCX, or a .zip of many).",
            status_code=400,
        )
    cover_letter_raw = request.form.get("cover_letter_text")
    cover_letter_text: str | None = None
    if cover_letter_raw:
        cover_letter_text = cover_letter_raw[:_MAX_COVER_LETTER_CHARS]

    cover_upload = request.files.get("cover_letter_file")
    cover_letter_file: tuple[str, bytes, str] | None = None
    if cover_upload is not None and cover_upload.filename:
        cover_letter_file = (
            cover_upload.filename,
            cover_upload.read(),
            cover_upload.content_type or "application/octet-stream",
        )

    manifest_upload = request.files.get("pairing_manifest")
    pairing_manifest: tuple[str, bytes, str] | None = None
    if manifest_upload is not None and manifest_upload.filename:
        pairing_manifest = (
            manifest_upload.filename,
            manifest_upload.read(),
            manifest_upload.content_type or "application/json",
        )
    try:
        results = api_client.upload_resumes(
            job_id,
            files,
            consent_acknowledged=True,
            cover_letter_text=cover_letter_text,
            cover_letter_file=cover_letter_file,
            pairing_manifest=pairing_manifest,
        )
    except api_client.BadRequest as exc:
        return _render_job_detail(
            job_id, error=_format_error(exc.detail), status_code=400
        )
    except api_client.NotFound:
        abort(404)
    except api_client.BackendUnavailable as exc:
        return _unavailable(exc)
    # Post-upload results summary (flash-style): the recruiter sees what
    # happened per-file — counts + any pairing warnings — on the job-detail
    # page they're redirected to. Warnings are the backend's STATIC English
    # strings, never filename-derived candidate PII.
    for message in _summarise_upload(results):
        flash(message)
    return redirect(url_for("job_detail", job_id=job_id))


def _summarise_upload(results: Any) -> list[str]:
    """Build a short human summary from the ``ResumeUploadResult[]`` the backend
    returns: an accepted/with-cover/duplicate/rejected count line, one line per
    rejection, and any per-file pairing warnings."""
    rows = results if isinstance(results, list) else []
    accepted = [r for r in rows if r.get("outcome") == "accepted"]
    with_cover = [r for r in accepted if r.get("cover_letter_filename")]
    duplicate = [r for r in rows if r.get("outcome") == "duplicate"]
    rejected = [r for r in rows if r.get("outcome") == "rejected"]

    summary = f"{len(accepted)} accepted"
    if with_cover:
        summary += f" ({len(with_cover)} with a cover letter)"
    if duplicate:
        summary += f", {len(duplicate)} duplicate"
    if rejected:
        summary += f", {len(rejected)} rejected"
    messages = [summary]
    for r in rejected:
        reason = r.get("reason") or "rejected"
        messages.append(f"Rejected {r.get('original_filename')}: {reason}")
    for r in rows:
        for warning in r.get("warnings") or []:
            messages.append(warning)
    return messages


@app.get("/jobs/<uuid:job_id>/resumes-table")
def resumes_table(job_id: UUID) -> Any:
    """HTMX poll fragment. While any résumé row is still uploaded/parsing it
    keeps its ``hx-trigger`` so the browser re-polls every 3s; once every row
    is terminal (parsed/failed) it renders without the trigger, so polling
    stops."""
    try:
        resumes = api_client.list_resumes(job_id)
    except api_client.NotFound:
        abort(404)
    except api_client.BackendUnavailable as exc:
        return _unavailable(exc)
    return render_template(
        "resumes_table.html",
        job_id=job_id,
        resumes=resumes,
        resumes_pending=_any_resume_pending(resumes),
    )


@app.get("/jobs/<uuid:job_id>")
def job_detail(job_id: UUID) -> Any:
    return _render_job_detail(job_id)


@app.get("/jobs/<uuid:job_id>/parse-status")
def parse_status(job_id: UUID) -> Any:
    """HTMX poll fragment. While ``parsed_at`` is null it renders a
    ``parsing…`` badge AND keeps its ``hx-trigger`` so the browser polls again;
    once the LLM sets ``parsed_at`` it renders the required-skill pills WITHOUT
    the trigger, so polling stops."""
    try:
        job = api_client.get_job(job_id)
    except api_client.NotFound:
        abort(404)
    except api_client.BackendUnavailable as exc:
        return _unavailable(exc)
    return render_template("parse_status.html", job=job)


@app.post("/jobs/<uuid:job_id>/status")
def transition_status(job_id: UUID) -> Any:
    to = (request.form.get("to") or "").strip()
    try:
        api_client.transition_status(job_id, to)
    except api_client.Conflict as exc:
        return _render_job_detail(
            job_id, error=_format_error(exc.detail), status_code=409
        )
    except api_client.NotFound:
        abort(404)
    except api_client.BackendUnavailable as exc:
        return _unavailable(exc)
    except api_client.BadRequest as exc:
        # Security finding fix/auth-boundary-fails-open (F4): ADR-033 made
        # every write route 403 a non-writer CAS session (`require_session_role`),
        # which this view's caller never used to see. Left uncaught, this
        # propagated as an unhandled 500 for a hiring_manager/auditor clicking
        # the control. Render the actual backend status (usually 403) instead.
        abort(exc.status_code)
    return redirect(url_for("job_detail", job_id=job_id))


@app.post("/jobs/<uuid:job_id>/reparse")
def reparse_job(job_id: UUID) -> Any:
    """Re-queue a JD parse that failed or was stranded.

    Mirrors ``transition_status``'s error handling exactly, including the
    ``BadRequest``/403 branch: ADR-033 made every write route 403 a non-writer
    CAS session, and a view that leaves that uncaught turns a hiring manager's
    click into an unhandled 500.
    """
    try:
        api_client.reparse_job(job_id)
    except api_client.Conflict as exc:
        return _render_job_detail(
            job_id, error=_format_error(exc.detail), status_code=409
        )
    except api_client.NotFound:
        abort(404)
    except api_client.BackendUnavailable as exc:
        return _unavailable(exc)
    except api_client.BadRequest as exc:
        abort(exc.status_code)
    return redirect(url_for("job_detail", job_id=job_id))


@app.post("/jobs/<uuid:job_id>/blind-review")
def blind_review(job_id: UUID) -> Any:
    desired = (request.form.get("blind_review") or "").strip().lower() in (
        "true",
        "1",
        "on",
        "yes",
    )
    try:
        api_client.patch_job(job_id, {"blind_review": desired})
    except api_client.NotFound:
        abort(404)
    except api_client.BackendUnavailable as exc:
        return _unavailable(exc)
    except api_client.BadRequest as exc:
        # See the identical F4 comment on transition_status above.
        abort(exc.status_code)
    return redirect(url_for("job_detail", job_id=job_id))


def _mint_card_tokens(
    entries: list[dict[str, Any]] | None, *, action: str = "reveal"
) -> dict[str, str]:
    """Mint one per-résumé CSRF token per shortlist card (FU-4/D4).

    Returns a ``str(resume_id) -> token`` mapping the card template indexes by
    its own entry's résumé id, so every card on one render carries an
    independently valid, independently one-shot token. Entries without a usable
    ``resume_id`` are skipped rather than minting an unusable slot.

    FU-8/ADR-026: ``action`` defaults to ``"reveal"`` (unchanged pre-existing
    behaviour); each card's withdraw control mints a SEPARATE set of tokens by
    calling this again with ``action="withdraw"``, so the two audited actions
    on one card never share a slot.
    """
    tokens: dict[str, str] = {}
    for entry in entries or []:
        resume_id = entry.get("resume_id") if isinstance(entry, dict) else None
        if resume_id is None:
            continue
        tokens[str(resume_id)] = csrf.issue_token(resume_id, action=action)
    return tokens


@app.get("/jobs/<uuid:job_id>/shortlist")
def job_shortlist(job_id: UUID) -> Any:
    # Blind by design: no `reveal` kwarg is ever passed here — the shortlist
    # list read is unconditionally redacted, matching
    # `shortlist_service.list_for_job` accepting no such parameter either.
    try:
        entries = api_client.list_shortlist(job_id)
        # Gate "Generate": ranking a job with no parsed résumé yields an empty
        # shortlist and an endless "Generating…" poll — so disable the button
        # until at least one résumé has finished parsing.
        resumes = api_client.list_resumes(job_id)
    except api_client.NotFound:
        abort(404)
    except api_client.BackendUnavailable as exc:
        return _unavailable(exc)
    # F1 (review findings, 2026-08-18): a full page load must fetch and honor
    # the ranking status EXACTLY like `_render_shortlist_cards` already does —
    # otherwise a reload during an in-flight Regenerate (`shortlist_state =
    # 'ranking'`) renders `shortlist_cards.html` with no status at all, so it
    # has no `hx-trigger` and no banner: the original no-feedback defect,
    # reproduced via a page reload instead of the poll fragment. Shared helper
    # so the two call sites' NotFound/BackendUnavailable handling cannot drift
    # apart.
    shortlist_status = _fetch_shortlist_status(job_id)
    return render_template(
        "shortlist_list.html",
        job_id=job_id,
        entries=entries,
        shortlist_status=shortlist_status,
        any_resume_parsed=_any_resume_parsed(resumes),
        attempt=0,
        max_attempts=_MAX_SHORTLIST_POLL_ATTEMPTS,
        # The included `shortlist_cards.html` carries one reveal form per card,
        # all posting to the SAME guarded route, so each card needs its OWN
        # token keyed by its own résumé id (FU-4/D4).
        csrf_tokens=_mint_card_tokens(entries),
        # FU-8/ADR-026: each card's withdraw control needs its OWN token,
        # independent of the reveal token above (same résumé id, different
        # action).
        withdraw_csrf_tokens=_mint_card_tokens(entries, action="withdraw"),
    )


@app.post("/jobs/<uuid:job_id>/shortlist")
def generate_shortlist(job_id: UUID) -> Any:
    """Enqueue the ranking job, then return the pollable ``shortlist-cards``
    fragment. Ranking runs asynchronously on the backend, so the fragment comes
    back empty → it shows "Generating…" and keeps its ``hx-trigger`` until the
    ranked entries appear."""
    try:
        api_client.generate_shortlist(job_id)
    except api_client.NotFound:
        abort(404)
    except api_client.BackendUnavailable as exc:
        return _unavailable(exc)
    except api_client.BadRequest as exc:
        # See the F4 comment on transition_status above.
        abort(exc.status_code)
    return _render_shortlist_cards(job_id)


@app.get("/jobs/<uuid:job_id>/shortlist-cards")
def shortlist_cards(job_id: UUID) -> Any:
    """HTMX poll fragment. While the (blind) shortlist read is still empty it
    renders "Generating…" AND keeps its ``hx-trigger`` so the browser re-polls
    every 3s; once ranked entries exist it renders the cards WITHOUT the
    trigger, so polling stops. A bounded ``attempt`` counter (clamped
    server-side) stops the poll after ``_MAX_SHORTLIST_POLL_ATTEMPTS`` with a
    give-up message, so a job that never produces entries doesn't poll forever."""
    attempt = request.args.get("attempt", default=0, type=int) or 0
    attempt = max(0, min(attempt, _MAX_SHORTLIST_POLL_ATTEMPTS))
    return _render_shortlist_cards(job_id, attempt=attempt)


def _fetch_shortlist_status(job_id: UUID) -> dict[str, Any] | None:
    """Shared by both the full-page route and the poll-fragment route (F1,
    review findings 2026-08-18) so their NotFound/BackendUnavailable handling
    cannot drift apart.

    fix/regenerate-shortlist-no-feedback: fetch the status UNCONDITIONALLY,
    even when `entries` is non-empty. It used to be gated on `not entries`
    (correct back when the only server-side state was FU-7's fail-closed
    `awaiting_llm`, which only ever coexists with an empty shortlist) — but
    Regenerate has entries already on screen AND a new run in flight at the
    same time (`jobs.shortlist_state = 'ranking'`), so the template needs
    this fetched every time to tell "stale but a new run is coming" apart
    from "these are current". A NotFound here still 404s (the job is
    genuinely gone); a transient backend outage on JUST the status endpoint
    degrades gracefully to the ordinary path (`None`), never a 500.
    """
    try:
        status: dict[str, Any] = api_client.get_shortlist_status(job_id)
        return status
    except api_client.NotFound:
        abort(404)
    except api_client.BackendUnavailable:
        return None


def _render_shortlist_cards(job_id: UUID, *, attempt: int = 0) -> Any:
    # Blind by design: no `reveal` kwarg is ever passed here — the card-render
    # read is unconditionally redacted, exactly like the list read above.
    try:
        entries = api_client.list_shortlist(job_id)
    except api_client.NotFound:
        abort(404)
    except api_client.BackendUnavailable as exc:
        return _unavailable(exc)
    shortlist_status = _fetch_shortlist_status(job_id)
    return render_template(
        "shortlist_cards.html",
        job_id=job_id,
        entries=entries,
        shortlist_status=shortlist_status,
        attempt=attempt,
        max_attempts=_MAX_SHORTLIST_POLL_ATTEMPTS,
        # Each poll re-renders the cards, so re-minting here keeps every card's
        # token in the swapped-in DOM in sync with its session slot. Re-minting
        # for a résumé already present overwrites that résumé's slot in place,
        # so repeated polls cannot grow the mapping or evict unrelated tokens.
        csrf_tokens=_mint_card_tokens(entries),
        withdraw_csrf_tokens=_mint_card_tokens(entries, action="withdraw"),
    )


@app.get("/shortlist/<uuid:entry_id>")
def shortlist_entry_detail(entry_id: UUID) -> Any:
    try:
        raw = api_client.get_shortlist_entry(entry_id)
    except api_client.NotFound:
        abort(404)
    except api_client.BackendUnavailable as exc:
        return _unavailable(exc)
    # THE HONESTY GUARD (weight * score = contribution, "no generation-time
    # weights -> no contribution shown") lives in EXACTLY ONE place:
    # ``src.services.explanation.shortlist_entry_explanation``. This route
    # never re-derives that arithmetic itself, and the template renders only
    # what the explanation object already computed.
    #
    # A raw payload that fails validation (a legacy/malformed row missing a
    # field ``ShortlistEntry`` requires) degrades to ``explanation=None``
    # rather than a 500 -- the template still shows the bare rank/label/score
    # (identical to the pre-slice stub), it just omits the score-composition
    # and evidence panels, which cannot be shown honestly without a
    # well-formed DTO.
    #
    # ``api_client.get_shortlist_entry`` is typed ``-> Any`` (whatever JSON the
    # backend sent), so a non-object payload must degrade here too rather than
    # raise ``AttributeError`` on ``.items()`` -- a 500 defeats the whole point
    # of the fallback.
    payload: dict[str, Any] = raw if isinstance(raw, dict) else {}
    explanation: ShortlistExplanation | None
    entry: ShortlistEntry | _EntryHeader
    known = {k: v for k, v in payload.items() if k in _SHORTLIST_ENTRY_FIELDS}
    try:
        entry = ShortlistEntry.model_validate(known)
        explanation = shortlist_entry_explanation(entry)
    except ValidationError as exc:
        # NOT silent: this page is a compliance artifact, and a silent swallow
        # makes genuine corruption indistinguishable from an ordinary legacy
        # row. Logs the entry id ONLY -- never a candidate field, and never the
        # payload, which would defeat the redaction boundary in the log file.
        logger.warning(
            "shortlist entry %s: detail payload failed ShortlistEntry "
            "validation (%d error(s)); rendering without the explanation panel",
            entry_id,
            exc.error_count(),
        )
        entry = _entry_header(known)
        explanation = None
    # The template is handed the VALIDATED DTO (or the coerced header), never
    # the raw payload -- so a field the backend should not have sent has no
    # route to the page at all.
    return render_template("shortlist_entry.html", entry=entry, explanation=explanation)


@app.get("/resumes/<uuid:resume_id>")
def resume_detail(resume_id: UUID) -> Any:
    # CRITICAL redaction-boundary: any browser-supplied `?reveal=` query
    # string is deliberately never read/forwarded — `reveal` is always
    # False here, so a visitor cannot re-introduce de-anonymization by
    # editing the URL.
    try:
        resume = api_client.get_resume(resume_id)
    except api_client.NotFound:
        abort(404)
    except api_client.BackendUnavailable as exc:
        return _unavailable(exc)
    # `current_year` drives the skill-recency colour buckets in the template
    # (current/aging/stale). Passed in so the comparison stays deterministic
    # and doesn't need any candidate.* field.
    return render_template(
        "resume_detail.html",
        resume=resume,
        current_year=dt.date.today().year,
        revealed=False,
        # FU-4/D4: mint the one-shot anti-forgery token the reveal form posts
        # back, bound to THIS résumé id, so a cross-site auto-submit cannot
        # manufacture an audit row.
        csrf_token=csrf.issue_token(resume_id),
        # FU-8/ADR-026: a SECOND, independent one-shot token for whichever of
        # the withdraw/reinstate controls the template renders — same résumé
        # id, distinct action, so minting it never disturbs the reveal token
        # above.
        withdraw_csrf_token=csrf.issue_token(resume_id, action="withdraw"),
    )


@app.post("/resumes/<uuid:resume_id>/reveal")
def resume_reveal(resume_id: UUID) -> Any:
    """AUDITED de-anonymization (FU-1). Deliberately POST-only — a GET could be
    prefetched/link-crawled, but a reveal must be an explicit act. Calls the
    backend's audited reveal endpoint (which records who/what/when), then
    re-renders the résumé UN-blinded in place. Blind stays the default; this is
    the only path that surfaces identity.

    FU-4/D4: guarded by a session-bound one-shot CSRF token plus an
    ``Origin``/``Referer`` same-origin check. BOTH are evaluated BEFORE any call
    reaches the backend, so a rejected forgery attempt can never imply a
    ``reveal_audit`` row."""
    if not csrf.same_origin(request):
        abort(403)
    if not csrf.verify_and_consume(resume_id, request.form.get(csrf.FORM_FIELD)):
        abort(403)
    # `context` records WHERE the reveal was triggered (shortlist card vs the
    # résumé page) in the audit row; defaults to the résumé page.
    context = (request.form.get("context") or "resume_detail").strip()[:64]
    try:
        resume = api_client.reveal_resume(resume_id, context=context)
    except api_client.NotFound:
        abort(404)
    except api_client.BackendUnavailable as exc:
        return _unavailable(exc)
    except api_client.BadRequest as exc:
        # Security finding fix/auth-boundary-fails-open — the identical F4 gap
        # exists here too (a hiring_manager session now 403s on every reveal,
        # ADR-033 §4), out of the tester's original scope but fixed in the
        # same pass. See the F4 comment on transition_status above.
        abort(exc.status_code)
    return render_template(
        "resume_detail.html",
        resume=resume,
        current_year=dt.date.today().year,
        revealed=True,
    )


@app.post("/resumes/<uuid:resume_id>/withdraw")
def resume_withdraw(resume_id: UUID) -> Any:
    """AUDITED. Mirrors ``resume_reveal``'s guard shape exactly (FU-8/ADR-026):
    same-origin check first, then a one-shot CSRF token — this time scoped to
    ``action="withdraw"``, independent of the SAME résumé's reveal token —
    BOTH evaluated before any call reaches the backend, so a rejected forgery
    attempt can never imply a withdrawal audit row.

    **Returns the user to the page they acted on** (2026-08-20, reported from
    the live product). The shortlist card's form has posted
    ``context=shortlist`` since FU-8 and this route never read it, redirecting
    unconditionally to the résumé page — so withdrawing from the shortlist
    threw the user onto a different screen, and pressing Back served the
    browser's CACHED shortlist with the candidate still on it. The withdrawal
    had worked every time; only the destination was wrong, which is
    indistinguishable from "withdraw does nothing" at the keyboard.

    ``job_id`` is the return address, and it is attacker-controllable form
    input: it is parsed as a ``UUID`` and the URL is built server-side with
    ``url_for``, never echoed into a ``Location`` header, so there is no open
    redirect here. Anything unparseable degrades to the résumé page."""
    if not csrf.same_origin(request):
        abort(403)
    if not csrf.verify_and_consume(
        resume_id, request.form.get(csrf.FORM_FIELD), action="withdraw"
    ):
        abort(403)
    reason = (request.form.get("reason") or "").strip() or None
    try:
        api_client.withdraw_resume(resume_id, reason=reason)
    except api_client.NotFound:
        abort(404)
    except api_client.BackendUnavailable as exc:
        return _unavailable(exc)
    except api_client.BadRequest as exc:
        # See the F4 comment on transition_status above.
        abort(exc.status_code)
    if (request.form.get("context") or "").strip() == "shortlist":
        try:
            job_id = UUID((request.form.get("job_id") or "").strip())
        except ValueError:
            pass
        else:
            return redirect(url_for("job_shortlist", job_id=job_id))
    return redirect(url_for("resume_detail", resume_id=resume_id))


@app.post("/resumes/<uuid:resume_id>/reinstate")
def resume_reinstate(resume_id: UUID) -> Any:
    """AUDITED. Same guard shape as ``resume_withdraw`` (shares the
    ``action="withdraw"`` CSRF slot — the résumé-detail page renders exactly
    ONE of the withdraw/reinstate controls at a time, so they never contend
    for the same slot in practice)."""
    if not csrf.same_origin(request):
        abort(403)
    if not csrf.verify_and_consume(
        resume_id, request.form.get(csrf.FORM_FIELD), action="withdraw"
    ):
        abort(403)
    try:
        api_client.reinstate_resume(resume_id)
    except api_client.NotFound:
        abort(404)
    except api_client.BackendUnavailable as exc:
        return _unavailable(exc)
    except api_client.BadRequest as exc:
        # See the F4 comment on transition_status above.
        abort(exc.status_code)
    return redirect(url_for("resume_detail", resume_id=resume_id))


@app.get("/jobs/<uuid:job_id>/resume-status")
def resume_status_widget(job_id: UUID) -> Any:
    """HTMX fragment (ADR-026 decision 5) — the per-job résumé status
    breakdown widget. Lazy-loaded: the main ``job_detail`` render never calls
    ``get_resume_status_breakdown`` itself, only this dedicated fragment
    route does."""
    try:
        breakdown = api_client.get_resume_status_breakdown(job_id)
    except api_client.NotFound:
        abort(404)
    except api_client.BackendUnavailable as exc:
        return _unavailable(exc)
    return render_template(
        "resume_status_breakdown.html", job_id=job_id, breakdown=breakdown
    )


@app.get("/resumes/<uuid:resume_id>/match-results")
def resume_match_results(resume_id: UUID) -> Any:
    # Full-page view of the (candidate→jobs) reverse match. No redaction on this
    # path (ADR-012 §4 — the caller owns the résumé; jobs are not PII), so no
    # `reveal` kwarg is ever passed. Renders the pollable cards fragment so an
    # in-flight run keeps filling in as ranked jobs land.
    try:
        results = api_client.get_match_results(resume_id)
    except api_client.NotFound:
        abort(404)
    except api_client.BackendUnavailable as exc:
        return _unavailable(exc)
    return render_template(
        "match_results.html",
        resume_id=resume_id,
        results=results,
        attempt=0,
        max_attempts=_MAX_MATCH_POLL_ATTEMPTS,
    )


@app.post("/resumes/<uuid:resume_id>/match-jobs")
def resume_match_jobs(resume_id: UUID) -> Any:
    """Enqueue the reverse-match job, then return the pollable match-results
    fragment. POST-only: a side-effecting trigger must never be a prefetchable
    GET. ``reverse_match_job`` runs asynchronously on the backend, so the
    fragment comes back empty → it shows "Finding matching jobs…" and keeps its
    ``hx-trigger`` until the ranked jobs appear (mirrors ``generate_shortlist``)."""
    try:
        api_client.trigger_reverse_match(resume_id)
    except api_client.NotFound:
        abort(404)
    except api_client.BackendUnavailable as exc:
        return _unavailable(exc)
    except api_client.BadRequest as exc:
        # See the F4 comment on transition_status above.
        abort(exc.status_code)
    return _render_match_cards(resume_id)


@app.get("/resumes/<uuid:resume_id>/match-results-cards")
def resume_match_results_cards(resume_id: UUID) -> Any:
    """HTMX poll fragment (mirrors ``shortlist_cards``). While the reverse-match
    read is still empty AND the run is not yet done it renders "Finding matching
    jobs…" AND keeps its ``hx-trigger`` so the browser re-polls every 3s; once
    ranked jobs exist it renders them WITHOUT the trigger, so polling stops. A
    bounded ``attempt`` counter (clamped server-side) stops the poll after
    ``_MAX_MATCH_POLL_ATTEMPTS`` with a give-up message, so a run that never
    lands doesn't poll forever."""
    attempt = request.args.get("attempt", default=0, type=int) or 0
    attempt = max(0, min(attempt, _MAX_MATCH_POLL_ATTEMPTS))
    return _render_match_cards(resume_id, attempt=attempt)


def _render_match_cards(resume_id: UUID, *, attempt: int = 0) -> Any:
    # No redaction on this path (ADR-012 §4): the caller owns the résumé and
    # jobs are not PII, so real job titles/departments are shown here — this is
    # correct, unlike the blind shortlist. No `reveal` kwarg is ever passed.
    try:
        results = api_client.get_match_results(resume_id)
    except api_client.NotFound:
        abort(404)
    except api_client.BackendUnavailable as exc:
        return _unavailable(exc)
    return render_template(
        "match_results_cards.html",
        resume_id=resume_id,
        results=results,
        attempt=attempt,
        max_attempts=_MAX_MATCH_POLL_ATTEMPTS,
    )


_EXPORT_FORMATS: tuple[api_client.ExportFormat, ...] = ("csv", "evidence-csv", "json")


def _validated_export_format(raw: str | None) -> api_client.ExportFormat:
    """Validate a browser-supplied ``?format=`` against the allowed set,
    falling back to ``"csv"`` for a missing/unknown value. Never raises."""
    for candidate in _EXPORT_FORMATS:
        if raw == candidate:
            return candidate
    return "csv"


@app.get("/jobs/<uuid:job_id>/shortlist/export")
def shortlist_export(job_id: UUID) -> Any:
    """Server-side export proxy. Streams the backend response body straight
    through and preserves ``Content-Disposition``, without ever exposing the
    backend ``X-API-Key`` (attached only on the outbound leg by
    ``api_client.build_client``) to the browser — only the content type,
    disposition and body are copied onto the Flask response.

    Reads ``?format=`` from the query string and validates it against the
    allowed set, falling back to ``"csv"`` for a missing/unknown value
    (never raises). A browser-supplied ``?reveal=`` is deliberately never
    read or forwarded — exports stay anonymized (``reveal=False`` default)."""
    export_format = _validated_export_format(request.args.get("format"))
    try:
        backend_resp = api_client.export_shortlist(job_id, format=export_format)
    except api_client.NotFound:
        abort(404)
    except api_client.BackendUnavailable as exc:
        return _unavailable(exc)

    headers: dict[str, str] = {}
    content_disposition = backend_resp.headers.get("content-disposition")
    if content_disposition is not None:
        headers["Content-Disposition"] = content_disposition
    return Response(
        backend_resp.content,
        status=backend_resp.status_code,
        content_type=backend_resp.headers.get(
            "content-type", "application/octet-stream"
        ),
        headers=headers,
    )


@app.get("/admin/users")
def admin_users() -> Any:
    """user-admin-roles slice 7 — the admin-only user roster + role-assignment
    page. Gated by :func:`_require_admin_page`; ``BackendUnavailable`` maps to
    the shared 503 error page."""
    _require_admin_page()
    try:
        users = api_client.list_users()
    except api_client.BackendUnavailable as exc:
        return _unavailable(exc)
    return render_template(
        "admin_users.html", users=users, roles=_ASSIGNABLE_ROLES, error=None
    )


@app.get("/audit")
def audit_log() -> Any:
    """Phase 1.4 / ADR-036 — the auditor's view of the access record.

    **This page is why the auditor role existed but could not be used.** Until
    now the application had no read path to ``audit_log`` at all — not a route,
    not a service function — so producing the access record meant an engineer
    running SQL against production by hand. That is what ROADMAP guardrail 2's
    "an auditor account cannot do its job" referred to.

    Read-only by construction: there is no write control on this surface and no
    backend write route it could call. ``details`` arrives already
    allowlist-filtered by the backend (``audit_service.redact_audit_details``) —
    this page renders what it is given and redacts nothing itself, so the
    boundary has exactly one implementation rather than two that can disagree.
    """
    _require_audit_page()
    action = (request.args.get("action") or "").strip() or None
    try:
        offset = max(0, int(request.args.get("offset") or 0))
    except ValueError:
        offset = 0
    return _render_audit_page(action=action, offset=offset)


def _render_audit_page(
    *,
    action: str | None,
    offset: int,
    revealed_id: str | None = None,
    revealed_details: Any = None,
    error: str | None = None,
    status: int = 200,
) -> Any:
    """Render the access record, optionally with ONE row's withheld value
    un-masked (D1 = option C).

    Factored out of :func:`audit_log` so the reveal POST comes back to the same
    view of the record — same filter, same page — instead of bouncing an
    auditor three pages deep back to the top to read one value. The revealed
    payload is passed per-request and never persisted: a reload re-masks it,
    because one audited read should not silently entitle every later page load.
    """
    try:
        entries = api_client.list_audit_log(
            limit=_AUDIT_PAGE_SIZE, offset=offset, action=action
        )
    except api_client.BadRequest:
        # The backend re-checks the session role and 403s independently of the
        # page gate above; surface it as a 403 rather than an unhandled 500
        # (the F4 lesson from fix/auth-boundary-fails-open).
        abort(403)
    except api_client.BackendUnavailable as exc:
        return _unavailable(exc)
    return (
        render_template(
            "audit_log.html",
            entries=entries,
            action=action or "",
            offset=offset,
            page_size=_AUDIT_PAGE_SIZE,
            actions=_AUDIT_ACTIONS,
            withheld=_AUDIT_WITHHELD_MARKER,
            revealed_id=revealed_id,
            revealed_details=revealed_details,
            error=error,
            current_year=dt.date.today().year,
        ),
        status,
    )


@app.post("/audit/<uuid:audit_id>/reveal")
def audit_reveal_detail(audit_id: UUID) -> Any:
    """D1 = option C — ask the backend to disclose one withheld value, and
    render it in place.

    **This page does not know which actions are revealable, deliberately.** It
    offers the control wherever the backend withheld something and lets
    ``audit_service.is_revealable_action`` fail-close, so the disclosure rule
    has exactly one implementation instead of two that drift apart — the
    ROADMAP A7 shape, where a rule lives in prose in two places and nothing
    reconciles them. A refusal comes back as a message on the page.

    The reveal happens ONLY here, never on a GET: pagination and filtering must
    not manufacture audit rows for reads nobody performed. One click, one
    recorded read.
    """
    _require_audit_page()
    action = (request.form.get("action") or "").strip() or None
    try:
        offset = max(0, int(request.form.get("offset") or 0))
    except ValueError:
        offset = 0
    try:
        revealed = api_client.reveal_audit_detail(audit_id)
    except api_client.NotFound:
        abort(404)
    except api_client.BadRequest as exc:
        return _render_audit_page(
            action=action,
            offset=offset,
            error=_format_error(exc.detail)
            or "That value cannot be revealed from this page.",
            status=403,
        )
    except api_client.BackendUnavailable as exc:
        return _unavailable(exc)
    return _render_audit_page(
        action=action,
        offset=offset,
        revealed_id=str(revealed.get("id") or audit_id),
        revealed_details=revealed.get("details"),
    )


@app.post("/admin/users/<uuid:user_id>/role")
def admin_set_user_role(user_id: UUID) -> Any:
    """user-admin-roles slice 7 — assign a new role to a user. A backend 409
    (last-admin lockout) re-renders ``admin_users.html`` with a friendly
    message instead of redirecting; ``NotFound``/``BackendUnavailable`` map to
    404/503 respectively."""
    _require_admin_page()
    role = (request.form.get("role") or "").strip()
    try:
        api_client.set_user_role(user_id, role)
    except api_client.Conflict as exc:
        try:
            users = api_client.list_users()
        except api_client.BackendUnavailable as unavail:
            return _unavailable(unavail)
        return (
            render_template(
                "admin_users.html",
                users=users,
                roles=_ASSIGNABLE_ROLES,
                error=_format_error(exc.detail),
            ),
            409,
        )
    except api_client.NotFound:
        abort(404)
    except api_client.BackendUnavailable as exc:
        return _unavailable(exc)
    return redirect(url_for("admin_users"))
