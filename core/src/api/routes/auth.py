"""CAS auth routes (FU-5 slice 6, ADR-019 §10).

Ports the hris Flask/FastAPI blueprint shape
(``C:\\repos\\hris\\apps\\api\\src\\api\\routes\\auth.py``) to this repo's
conventions and route table:

  GET /auth/cas/login    -> 302 to the CAS server (or straight to ``next``
                             when CAS is disabled — dev mode)
  GET /auth/cas/validate -> ticket consumer; provisions/looks up the user,
                             creates a session, sets the httpOnly cookie,
                             302s to ``next``
  GET /auth/cas/logout   -> revokes the local session, clears the cookie
  GET /auth/cas/user     -> JSON status: {username, authenticated,
                             cas_enabled} — NEVER 401s, so a web client can
                             render a "log in" button without an error
                             round-trip

Settings are read via a plain module-level ``get_settings()`` call inside
each route function — NOT ``Depends(get_settings)`` — mirroring
``src.api.deps.resolve_role``'s existing convention (every other settings
consumer in this codebase calls ``get_settings()`` directly).

Scope boundary (slice 6 ONLY, per the task): this module does not write
``audit_log`` rows (slice 8), and does not implement the hris
``cas_dev_fake_user`` / ``cas_service_from_request``-forwarded-host
short-circuits — neither is exercised by this slice's locked test contract,
so they are deliberately left for a later slice rather than shipped
untested.

The raw CAS ticket and the session id are NEVER logged here — only their
lengths / short prefixes, matching ``src.services.cas_service`` and
``src.services.session_service``'s existing discipline.
"""

from __future__ import annotations

import logging
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse

from src.errors import AppError
from src.models.pool import Db
from src.services import cas_service, session_service, user_service
from src.settings import Settings, get_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth")


class CasAuthError(AppError):
    """The CAS server rejected the ticket, or the round trip failed."""

    code = "auth.cas_validation_failed"
    status = 502


def _service_base(settings: Settings, request: Request) -> str:
    """External base URL the CAS server should redirect back to.

    Mirrors the hris port source: when ``cas_service_from_request`` is set,
    derive it from ``X-Forwarded-Host``/``-Proto`` (a reverse proxy in
    front of the API) so the service URL matches whatever host the browser
    actually used. Falls back to the static ``cas_service_base_url``
    otherwise. Login and validate both derive it the same way so the
    service URL CAS issued and the one presented at validation match (CAS
    requires byte-equality).
    """
    if settings.cas_service_from_request:
        fwd_host = request.headers.get("x-forwarded-host")
        if fwd_host:
            host = fwd_host.split(",", 1)[0].strip()
            fwd_proto = request.headers.get("x-forwarded-proto", "http")
            proto = fwd_proto.split(",", 1)[0].strip()
            return f"{proto}://{host}"
    return settings.cas_service_base_url.rstrip("/")


def _service_url(settings: Settings, request: Request, next_path: str) -> str:
    base = _service_base(settings, request).rstrip("/")
    query = urlencode({"next": next_path})
    return f"{base}/auth/cas/validate?{query}"


def _safe_next(next_value: str) -> str:
    """Sanitize a caller-supplied ``next`` redirect target (FU-5 slice 13,
    security gate, FIX #2 — open-redirect prevention).

    Returns ``next_value`` unchanged ONLY when it is a safe, in-app relative
    path: starts with exactly one ``/`` (never ``//``, which browsers treat
    as protocol-relative), never contains a backslash (browsers normalise
    ``\\`` to ``/``, so ``/\\evil.com`` is equivalent to ``//evil.com``), and
    never smuggles a URL scheme (a ``:`` appearing before the first ``/``
    after the leading slash, e.g. ``javascript:...``). Any other value
    silently falls back to ``"/"`` — never a 400, mirroring mature CAS
    clients' least-surprising behaviour.
    """
    if not next_value.startswith("/") or next_value.startswith("//"):
        return "/"
    if "\\" in next_value:
        return "/"
    rest = next_value[1:]
    colon_idx = rest.find(":")
    slash_idx = rest.find("/")
    if colon_idx != -1 and (slash_idx == -1 or colon_idx < slash_idx):
        return "/"
    return next_value


def _landing_url(settings: Settings, safe_next: str) -> str:
    """Resolve a "landing" redirect target — a page the browser actually ends
    up on, as opposed to a hop back to the CAS server or a restart of the
    login dance (see the "frontend landing redirect" note in this module's
    docstring).

    ``safe_next`` must already be sanitized by ``_safe_next`` before this is
    called — this function only ever prefixes a base URL onto it, it never
    re-validates it.
    """
    if not settings.cas_frontend_base_url:
        return safe_next
    return f"{settings.cas_frontend_base_url.rstrip('/')}{safe_next}"


def _set_session_cookie(
    response: RedirectResponse, settings: Settings, sid: str
) -> None:
    response.set_cookie(
        key=settings.session_cookie_name,
        value=sid,
        max_age=settings.session_ttl_hours * 3600,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite=settings.session_cookie_samesite,  # type: ignore[arg-type]
        path="/",
    )


@router.get("/cas/login")
async def cas_login(request: Request, next: str = "/") -> RedirectResponse:
    settings = get_settings()
    safe_next = _safe_next(next)
    if not settings.cas_enabled:
        logger.info("cas.login.dev_mode")
        return RedirectResponse(url=_landing_url(settings, safe_next), status_code=302)

    cas_base = settings.cas_server_url.rstrip("/")
    service_url = _service_url(settings, request, safe_next)
    login_url = f"{cas_base}/login?" + urlencode({"service": service_url})
    logger.info("cas.login.redirect cas_server=%s", cas_base)
    return RedirectResponse(url=login_url, status_code=302)


@router.get("/cas/validate")
async def cas_validate(
    request: Request,
    db: Db,
    ticket: str | None = Query(default=None),
    next: str = "/",
) -> RedirectResponse:
    settings = get_settings()
    safe_next = _safe_next(next)
    if not settings.cas_enabled:
        return RedirectResponse(url=_landing_url(settings, safe_next), status_code=302)

    if not ticket:
        # CAS round-trip lost the ticket somehow — restart the dance.
        restart_url = "/auth/cas/login?" + urlencode({"next": safe_next})
        return RedirectResponse(url=restart_url, status_code=302)

    async with httpx.AsyncClient(verify=settings.cas_verify_tls) as http:
        try:
            cas_username = await cas_service.validate_ticket(
                cas_server_url=settings.cas_server_url,
                validate_route=settings.cas_validate_route,
                service_url=_service_url(settings, request, safe_next),
                ticket=ticket,
                http=http,
            )
        except cas_service.CasValidationError as exc:
            logger.warning("cas.validate.failed error=%s", exc)
            raise CasAuthError(f"cas validation failed: {exc}") from exc

    user = await user_service.provision_or_get(
        db,
        cas_username=cas_username,
        default_admin_cas_username=settings.default_admin_cas_username,
    )
    session = await session_service.create_session(
        db,
        user_id=user.id,
        ttl_seconds=settings.session_ttl_hours * 3600,
        user_agent=request.headers.get("user-agent"),
        ip=(request.client.host if request.client else None),
    )

    response = RedirectResponse(url=_landing_url(settings, safe_next), status_code=302)
    _set_session_cookie(response, settings, session.id)
    logger.info("cas.validate.ok user_id=%s", user.id)
    return response


@router.get("/cas/logout")
async def cas_logout(request: Request, db: Db) -> RedirectResponse:
    settings = get_settings()
    sid = request.cookies.get(settings.session_cookie_name)
    if sid:
        await session_service.revoke_session(db, sid)

    if not settings.cas_enabled:
        response = RedirectResponse(url=_landing_url(settings, "/"), status_code=302)
        response.delete_cookie(settings.session_cookie_name, path="/")
        return response

    cas_base = settings.cas_server_url.rstrip("/")
    logout_service = (
        settings.cas_frontend_base_url.rstrip("/")
        if settings.cas_frontend_base_url
        else _service_base(settings, request)
    )
    logout_url = f"{cas_base}/logout?" + urlencode({"service": logout_service})
    response = RedirectResponse(url=logout_url, status_code=302)
    response.delete_cookie(settings.session_cookie_name, path="/")
    return response


@router.get("/cas/user")
async def cas_user(request: Request, db: Db) -> JSONResponse:
    """Public status endpoint — NEVER 401s. Anonymous when not logged in so
    a web client can render a "log in" button without an error round-trip."""
    settings = get_settings()
    if not settings.cas_enabled:
        # ADR-020 §7 — dev-anonymous mode resolves the same synthetic admin
        # identity `resolve_user` would (role="admin"); hardcoded here rather
        # than calling `resolve_user` again, since it is a constant in this
        # branch and would otherwise add a needless second resolution.
        return JSONResponse(
            {
                "username": None,
                "authenticated": False,
                "cas_enabled": False,
                "role": "admin",
            }
        )

    sid = request.cookies.get(settings.session_cookie_name)
    if not sid:
        return JSONResponse(
            {
                "username": None,
                "authenticated": False,
                "cas_enabled": True,
                "role": None,
            }
        )

    session = await session_service.get_active_session(db, sid)
    if session is None:
        return JSONResponse(
            {
                "username": None,
                "authenticated": False,
                "cas_enabled": True,
                "role": None,
            }
        )

    session = await session_service.refresh_if_needed(
        db,
        session,
        ttl_seconds=settings.session_ttl_hours * 3600,
        idle_refresh_seconds=settings.session_idle_refresh_hours * 3600,
    )

    user = await user_service.get_by_id(db, session.user_id)
    return JSONResponse(
        {
            "username": user.cas_username if user else None,
            "authenticated": user is not None,
            "cas_enabled": True,
            "role": user.role if user else None,
        }
    )
