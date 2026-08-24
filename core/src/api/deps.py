"""Cross-cutting API dependencies — RBAC, actor resolution, arq dep.

**Keyed roles (FU-4).** Phase 6's single ``require_api_key`` switch is retired:
the route→role table needs DIFFERENT allowed-role sets on different routes of
the same router (``PATCH /jobs/{id}`` is admin/recruiter-only while ``GET
/jobs/{id}`` is open to all four roles), which one boolean pass/fail dependency
cannot express. Two primitives replace it:

* :func:`resolve_role` — the KEY→ROLE step. Auth DISABLED (all four
  ``settings.api_key_*`` empty) always resolves :attr:`Role.ADMIN`, whatever
  the header carries — today's fail-open-by-EXPLICIT-configuration local-dev
  mode, preserved verbatim. Auth ENABLED: the ``X-API-Key`` header must match
  exactly one configured role key (constant-time, over UTF-8 bytes) or the
  request is rejected with 401.
* :func:`require_role` — a dependency FACTORY producing a per-route check that
  403s a resolved role outside its allowed set. It composes ``resolve_role`` as
  a sub-dependency rather than re-parsing the header, so ``resolve_role`` stays
  the ONE shared function object an ``app.dependency_overrides`` entry can
  target to bypass auth uniformly across every route in a test.

``log_auth_mode`` logs a loud WARNING at API startup when the switch is off, so
a misconfigured deploy is impossible to miss in the logs. No role key value is
ever logged, here or anywhere else.

**Session identity (FU-5 slice 6, ADR-019 §10/§10b).** :func:`resolve_user`
is a SEPARATE, additive dependency — the cookie -> session -> user chain used
by ``src.api.routes.auth`` and (as of slice 7) the audit-identity call sites
in ``src.api.routes.jobs``/``resumes``. It does not replace or modify
:func:`resolve_role`/:func:`require_role`.

**Actor identity (FU-5 slice 7, ADR-019 §8.3/§9.2).** The formerly optional,
unverified actor-name header (and the function that read it) is REMOVED
ENTIRELY — a spoofable identity-shaped header next to a cryptographically
verified session invited confusion and misconfiguration.
``created_by``/``uploaded_by``/the reveal actor are now sourced exclusively
from :func:`resolve_user`'s resolved ``cas_username`` (``None`` when no
identity resolves, e.g. a bare service-key caller with CAS enabled).

**Role now lives in ``src.schemas.auth`` (user-admin-roles slice 6).**
``Role`` moved there so ``schemas.auth.RoleAssignment`` (``PATCH
/users/{id}/role``'s request body) can use it as a field type without an
import cycle — this module previously defined it and now just re-exports it
(``from src.schemas.auth import Role``), so every existing ``from
src.api.deps import Role`` / ``deps.Role`` call site is unaffected.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from arq.connections import ArqRedis
from fastapi import Cookie, Depends, Header, HTTPException, Request

from src.models.pool import Db

# ``Role as Role`` — mypy --strict's implicit-reexport check requires this
# exact "as"-form for a re-exported name (see the module docstring's "Role
# now lives in src.schemas.auth" note): every existing ``from src.api.deps
# import Role`` call site needs this to keep resolving.
from src.schemas.auth import Role as Role
from src.schemas.auth import User
from src.services import DbConn, audit_service, session_service, user_service
from src.settings import Settings, get_settings

logger = logging.getLogger(__name__)

# ADR-019 §10b — the dev-anonymous synthetic admin's fixed sentinel id.
# Deliberately the all-zero UUID so it is instantly recognisable in logs/DB
# dumps as "not a real user." This id is NEVER written to `users` and is
# NEVER a valid `audit_log` FK target — auth-disabled actions are attributed
# to a fixed "service" actor label at the audit layer (slice 8), not to this
# id. A real DB-backed row (hris's `_ensure_dev_anonymous_user` upsert
# pattern) was deliberately NOT ported: it would pollute `users` with an
# account that never actually logged in.
_DEV_ADMIN_SENTINEL_ID = UUID("00000000-0000-0000-0000-000000000000")


def _configured_role_keys(settings: Settings) -> tuple[tuple[Role, str], ...]:
    return (
        (Role.ADMIN, settings.api_key_admin),
        (Role.RECRUITER, settings.api_key_recruiter),
        (Role.HIRING_MANAGER, settings.api_key_hiring_manager),
        (Role.AUDITOR, settings.api_key_auditor),
    )


async def resolve_role(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> Role:
    """Resolve the presented ``X-API-Key`` to exactly one :class:`Role`.

    Auth disabled (all four role keys empty) ALWAYS resolves ``Role.ADMIN``,
    regardless of what ``x_api_key`` carries. Enabled: a missing key, or one
    matching no configured role, raises 401.
    """
    settings = get_settings()
    if not settings.auth_enabled:
        return Role.ADMIN

    # Compare on UTF-8 BYTES, not ``str``: ``secrets.compare_digest`` requires
    # ASCII-only strings and raises ``TypeError`` otherwise — Starlette
    # latin-1-decodes header bytes, so a non-ASCII ``X-API-Key`` header is a
    # valid (non-ASCII) ``str`` that would otherwise crash this dependency into
    # an unhandled 500 instead of failing closed with 401 (SEC-1).
    presented = (x_api_key or "").encode("utf-8")
    matched: Role | None = None
    for role, configured in _configured_role_keys(settings):
        if not configured:
            continue
        # Constant-time, and deliberately NOT short-circuited on the first
        # match: every configured key is compared on every request, so neither
        # response timing nor the number of comparisons leaks which role a
        # near-miss guess was closest to.
        if secrets.compare_digest(presented, configured.encode("utf-8")):
            matched = role

    if matched is None:
        # Never log the presented value or any configured key.
        logger.warning("auth.rejected — X-API-Key matched no configured role")
        raise HTTPException(status_code=401, detail="invalid or missing API key")
    return matched


def require_role(*allowed: Role) -> Callable[..., Awaitable[Role]]:
    """Build the per-route authorization dependency for ``allowed``.

    Each call returns a DISTINCT closure (so ``dependency_overrides`` cannot
    target "every ``require_role(...)`` call" as one entry — override the
    shared :func:`resolve_role` instead). A role outside ``allowed`` raises
    403, which is distinct from ``resolve_role``'s 401: authenticated but not
    authorized.
    """
    allowed_roles = frozenset(allowed)

    async def _check(role: Annotated[Role, Depends(resolve_role)]) -> Role:
        if role not in allowed_roles:
            raise HTTPException(
                status_code=403, detail="role not permitted for this route"
            )
        return role

    return _check


def require_session_role(*allowed: Role) -> Callable[..., Awaitable[None]]:
    """Build a per-route dependency that 403s a REAL session whose ``role`` is
    outside ``allowed`` — the SESSION-role counterpart to :func:`require_role`
    (FU-5 pilot-readiness fix, ``fix/session-role-on-writes``).

    ``require_role``/``resolve_role`` judges only the presented API KEY. The
    Flask BFF attaches ONE shared ``recruiter`` key to every browser request
    (``core/frontend/api_client.py``), so a signed-in hiring_manager or
    auditor performs a write indistinguishably from a recruiter as far as the
    key alone is concerned — this dependency closes that hole by additionally
    consulting the CAS session's own ``role``.

    Each call returns a DISTINCT closure (mirrors ``require_role``): override
    the shared :func:`resolve_user`, never a ``require_session_role(...)``
    closure by identity, in tests.

    Three cases, same contract as ``src.api.routes.users._require_admin_session``
    and ``require_role_assigned``:

    * ``user is None`` — no session at all (a bare service-key caller or a
      cookie-less/key-less caller with CAS enabled) — **403** (security
      finding, ``fix/auth-boundary-fails-open``, F1a). REVERSED from this
      gate's original design, which let ``user is None`` PASS on the theory
      that it "never judges the ABSENCE of a session" — a live audit against
      real Postgres proved that theory wrong: combined with zero
      ``API_KEY_*`` configured (so ``resolve_role`` trivially resolves
      ``Role.ADMIN`` for anyone), a cookie-less, key-less caller could reach
      every write route with no credential at all. The human decision: a
      valid API key (or no key at all, when auth is disabled) is NEVER
      sufficient on its own — every write route now requires a REAL,
      resolvable CAS session.
    * a real session whose ``role`` is not in ``allowed`` — 403, plain
      ``fastapi.HTTPException`` (not ``AppError``), same mechanism as
      ``require_role``/``require_role_assigned``/``scoped_user_id_or_403``.
    * a real session whose ``role`` is in ``allowed`` AND ``active`` — PASS.
      The CAS-disabled synthetic dev-anonymous sentinel (``role="admin"``,
      ``active=True``) passes naturally through this same comparison, with
      no special-casing needed.

    **F5 (security finding, ``fix/auth-boundary-fails-open``).** A resolved
    session whose ``user.active is False`` also 403s, same mechanism,
    regardless of ``role`` — a deactivated account must not keep acting just
    because ``session_service.refresh_if_needed`` keeps sliding its expiry
    forward on every request.
    """
    allowed_roles = frozenset(allowed)

    async def _check(user: Annotated[User | None, Depends(resolve_user)]) -> None:
        if user is None:
            raise HTTPException(
                status_code=403,
                detail="a verified CAS session is required for this route",
            )
        if not user.active:
            raise HTTPException(
                status_code=403,
                detail="this account has been deactivated",
            )
        if user.role not in allowed_roles:
            raise HTTPException(
                status_code=403,
                detail="session role not permitted for this route",
            )

    return _check


def get_arq(request: Request) -> ArqRedis:
    """FastAPI dependency — hand the lifespan-built arq pool to routes.

    Mirrors ``get_db``/``get_blob_store``: raises ``RuntimeError`` when the
    lifespan has not parked a pool on ``app.state.arq``.
    """
    arq: ArqRedis | None = getattr(request.app.state, "arq", None)
    if arq is None:
        raise RuntimeError("arq pool not initialised (lifespan not running)")
    return arq


def log_auth_mode(settings: Settings) -> None:
    """Called ONCE at API startup. Loud WARNING when auth is disabled, an
    informational line otherwise. Never logs a key value.

    Also emits a SEPARATE, independent WARNING (FU-5 slice 13, security
    gate, FIX #1) when ``cas_enabled=True`` and ``session_cookie_secure=
    False``: the session cookie would be sent over plain HTTP, letting it
    leak to a network eavesdropper. Not fatal — a local dev CAS deployment
    over http:// is a legitimate, if insecure, setup — just loud. This
    warning is independent of the auth-disabled warning above: neither's
    presence/absence suppresses the other.
    """
    if not settings.auth_enabled:
        logger.warning(
            "AUTH DISABLED — all four role keys are empty; every route is "
            "unauthenticated and resolves to the admin role. Set "
            "API_KEY_ADMIN / API_KEY_RECRUITER / API_KEY_HIRING_MANAGER / "
            "API_KEY_AUDITOR to enable auth."
        )
    else:
        logger.info("auth.enabled")

    if settings.cas_enabled and not settings.session_cookie_secure:
        logger.warning(
            "INSECURE SESSION COOKIE — cas_enabled=True but "
            "session_cookie_secure=False; the CAS session cookie will be "
            "sent over plain HTTP and can be intercepted. Set "
            "SESSION_COOKIE_SECURE=true for any deployment reachable over "
            "the network."
        )


async def resolve_user(
    request: Request,
    db: Db,
    ra_session: str | None = Cookie(default=None),
) -> User | None:
    """Resolve the CAS session cookie to a :class:`~src.schemas.auth.User`.

    ADR-019 §10b — CAS DISABLED: every request resolves a synthetic
    in-memory dev-admin identity (``role="admin"``), regardless of
    ``ra_session`` (a presented cookie is ignored, not even looked up). See
    the module-level ``_DEV_ADMIN_SENTINEL_ID`` comment for why this is a
    plain object, not a DB-backed row.

    CAS ENABLED: no cookie, or a cookie that does not resolve to a live
    (unrevoked, unexpired) session, or a session whose user no longer
    exists, all return ``None`` — the caller cannot distinguish those cases
    from this return value alone (no timing/existence oracle for a stolen
    cookie, matching ``session_service.get_active_session``'s own
    discipline).

    ``request`` is accepted (unused) to match FastAPI's dependency-injection
    shape and the port source's signature; a later slice may read
    ``request.state``/headers here.
    """
    settings = get_settings()
    if not settings.cas_enabled:
        now = datetime.now(UTC)
        return User(
            id=_DEV_ADMIN_SENTINEL_ID,
            cas_username="dev-anonymous",
            display_name="Dev Anonymous (auth disabled)",
            email=None,
            role="admin",
            active=True,
            created_at=now,
            last_seen_at=now,
        )

    if not ra_session:
        return None

    session = await session_service.get_active_session(db, ra_session)
    if session is None:
        return None

    session = await session_service.refresh_if_needed(
        db,
        session,
        ttl_seconds=settings.session_ttl_hours * 3600,
        idle_refresh_seconds=settings.session_idle_refresh_hours * 3600,
    )

    return await user_service.get_by_id(db, session.user_id)


def actor_fields_from_user(
    user: User | None,
) -> tuple[str, UUID | None, str | None]:
    """Derive the ``(actor_kind, actor_user_id, actor_service)`` triple
    ``audit_service.record_audit`` expects, from a :func:`resolve_user`
    resolution.

    FU-5 slice 9 (ADR-019 §1.4/§6) factors this OUT of
    ``src.api.routes.resumes.reveal_resume``'s inline if/else (which handles
    only two of these three cases, because reveal 403s outright on ``user is
    None``) so ``src.api.routes.jobs``'s ``blind_review``-flip audit — which
    is NOT human-only and must succeed for a bare service-key caller too —
    can share the derivation without duplicating it. Three cases:

    1. ``user is None`` (no identity resolves at all, e.g. CAS enabled with
       no live session) -> ``("service", None, "api")``.
    2. ``user.id == _DEV_ADMIN_SENTINEL_ID`` (the synthetic dev-anonymous
       identity, CAS disabled) -> ``("service", None, user.cas_username)`` —
       never ``actor_kind="user"`` against the sentinel id: it is not a real
       ``users`` row and would violate ``audit_log.actor_user_id``'s FK.
    3. Any other resolved user (a real human session) ->
       ``("user", user.id, None)``.

    Deliberately NOT wired into ``reveal_resume`` in this slice: reveal's
    403-on-``None`` gate runs BEFORE any actor derivation, so reusing this
    helper there would require restructuring an already-green, security-
    sensitive code path for no behavioural gain. Left alone.
    """
    if user is None:
        return ("service", None, "api")
    if user.id == _DEV_ADMIN_SENTINEL_ID:
        return ("service", None, user.cas_username)
    return ("user", user.id, None)


async def require_role_assigned(
    user: Annotated[User | None, Depends(resolve_user)],
) -> None:
    """Fail-closed gate (user-admin-roles slice 3, ADR-019 §10a/§10b reversal
    follow-up) — blocks a REAL session whose ``role`` is ``None``.

    Slice 2 stopped defaulting a non-default-admin CAS login to
    ``role='recruiter'``; a first login now captures ``role=None``. Nothing
    in slice 2 blocked that no-role user from then riding the Flask viewer's
    single shared ``recruiter`` API key to full recruiter-equivalent,
    company-wide access (``require_role`` only judges the KEY, never the
    session). This dependency closes that hole at the router level.

    Three cases:

    * ``user is None`` — no session at all (a bare service-key caller, or
      CAS disabled outside the sentinel path) — **403**. REVERSED 2026-08-19
      (D2 = option B, ``docs/OPEN_DECISIONS.md`` §D2, closing the question
      ADR-034's "Accepted residuals" carried forward). Until that date this
      case PASSED, on the theory that "this gate never judges the ABSENCE of
      a session, only a REAL session's role" — so a caller holding a valid
      API key with no CAS session got unscoped reads across every business
      router, the sharpest instance being a bare key reading
      ``GET /audit/reveals-legacy`` unattributably. Product decided reads
      must be symmetric with writes (ADR-034 F1a already 403s ``user is
      None`` for ``require_session_role``): every read now needs a real
      principal too. The CAS-off dev boot is untouched by this — see
      ``resolve_user``'s docstring: it returns the ``dev-anonymous`` sentinel
      BEFORE the ``if not ra_session: return None`` branch even runs, so
      ``user`` is never ``None`` on that path.
    * ``user.role is None`` — a genuinely resolved session with no assigned
      role — 403, fail-closed, before any route body runs. Uses the same
      plain ``fastapi.HTTPException(status_code=403, ...)`` mechanism as
      ``require_role``/the reveal-route 403, not a bespoke ``AppError``.
    * any other resolved user (a real assigned role, or the CAS-disabled
      synthetic dev-anonymous admin whose ``role='admin'``) — PASS, provided
      ``user.active`` is true.

    **F5 (security finding, ``fix/auth-boundary-fails-open``).** A resolved
    session whose ``user.active is False`` also 403s, same mechanism — a
    deactivated account's still-live session must not keep passing this
    gate just because its role was assigned before deactivation.
    """
    if user is None:
        raise HTTPException(
            status_code=403,
            detail="a verified session is required for this route",
        )
    if user.role is None:
        raise HTTPException(
            status_code=403,
            detail="no role assigned to this account — contact an administrator",
        )
    if not user.active:
        raise HTTPException(
            status_code=403,
            detail="this account has been deactivated",
        )


async def scoped_user_id_or_403(user: User | None, key_role: Role) -> UUID | None:
    """Row-scoping identity helper (FU-6 slice 4, ADR-020 §3/§4/§5).

    Scoping keys off the CAS SESSION identity (``user``, ``resolve_user``'s
    resolved ``User | None``), never the API-key ``Role``. The Flask viewer
    presents ONE shared ``recruiter`` API key for every browser user
    (``core/frontend/api_client.py``), so ``key_role`` resolves to
    ``Role.RECRUITER`` for every browser request regardless of which human is
    actually signed in — a hiring_manager browsing through the shared key
    would look, to ``key_role`` alone, indistinguishable from a recruiter.
    Keying scope off ``key_role`` would silently serve that hiring_manager
    company-wide data instead of scoping them to their own requisitions
    (ADR-020 §4). The session's ``user.role`` is cryptographically tied to a
    real login and cannot be spoofed by the shared browser key, so it — not
    ``key_role`` — decides whether the caller is actually a hiring_manager.

    Returns:
        * a real ``UUID`` (``user.id``) — a genuine hiring_manager session;
          the caller is scoped, filter every query by this id.
        * ``None`` — the caller is UNSCOPED: an admin/recruiter/auditor
          session, the dev-anonymous synthetic admin, or no session at all
          paired with a non-hiring_manager ``key_role`` (a bare service/admin
          key) — see every row.
        * raises ``HTTPException(403)`` — ``key_role == Role.HIRING_MANAGER``
          (the caller CLAIMS a scoped role) but there is no verifiable
          session identity to scope against, or the session resolves to a
          different role. Fail-closed (ADR-020 §3, final paragraph): never
          silently serve company-wide data to a caller who should see one
          requisition. Uses the same ``fastapi.HTTPException(status_code=
          403, ...)`` mechanism as ``require_role`` and the reveal-route 403.

    **F5 (security finding, ``fix/auth-boundary-fails-open``).** A
    deactivated hiring_manager session (``user.active is False``) also
    raises ``HTTPException(403)``, regardless of ``key_role`` — it must
    NEVER silently fall through to the ``None`` (unscoped, company-wide)
    branch below, which would be a WORSE outcome than the status quo: a
    deactivated account seeing every row instead of none.
    """
    if user is not None and user.role == "hiring_manager":
        if not user.active:
            raise HTTPException(
                status_code=403,
                detail="this account has been deactivated",
            )
        return user.id

    if key_role == Role.HIRING_MANAGER:
        raise HTTPException(
            status_code=403,
            detail="hiring_manager key requires a verified hiring_manager session",
        )

    return None


async def log_auditor_read(
    db: DbConn,
    user: User | None,
    *,
    action: str,
    subject_type: str,
    subject_id: UUID,
    job_id: UUID | None = None,
) -> None:
    """FU-6 slice 8 (ADR-020 §6) — "the watchers are watched."

    Auditors keep GLOBAL, unscoped read access (ADR-020 §4); the
    compensating control is that every auditor READ is itself logged to
    ``audit_log``. This helper is the single auditor-check shared by the four
    deliberate single-subject/bulk read routes (``GET /jobs/{id}``, ``GET
    /resumes/{id}``, ``GET /shortlist/{id}``, ``GET
    /jobs/{id}/shortlist/export``) — the polled list-index routes never call
    this at all.

    Fires ONLY for a REAL auditor session: ``user is not None and user.role
    == "auditor"`` AND ``user.id != _DEV_ADMIN_SENTINEL_ID`` (the ambient
    CAS-disabled synthetic identity resolves ``role="admin"``, so it is
    already excluded by the role check alone — the sentinel check is a
    defense-in-depth belt-and-suspenders, since it can never be a real
    ``users`` row and would violate ``audit_log.actor_user_id``'s FK if it
    somehow reached here as ``actor_kind='user'``).

    Callers MUST invoke this only AFTER the underlying read has already
    resolved successfully (a 200) — a 404/``NotFoundError`` means nothing was
    read, so nothing is logged. This function does not itself catch
    exceptions; ordering is the caller's responsibility, matching
    ``reveal_resume``'s audit-then-decrypt discipline.
    """
    if user is None or user.role != "auditor" or user.id == _DEV_ADMIN_SENTINEL_ID:
        return
    await audit_service.record_audit(
        db,
        actor_kind="user",
        actor_user_id=user.id,
        actor_service=None,
        action=action,
        subject_type=subject_type,
        subject_id=subject_id,
        job_id=job_id,
    )
