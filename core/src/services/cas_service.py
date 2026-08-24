"""CAS v2 ticket validation (FU-5 slice 4, ADR-019 §10).

Ports the validation half of the hris Flask blueprint
(``C:\\repos\\hris\\apps\\api\\src\\api\\services\\cas_service.py``) to this
repo's conventions: async httpx + stdlib ``xml.etree.ElementTree`` and stdlib
``logging`` (not ``structlog`` — not a dependency here; see
``src/services/outbox_service.py`` for the established
``logging.getLogger(__name__)`` pattern).

The two-call shape (redirect to CAS -> CAS redirects back with ticket -> we
validate the ticket -> on success, create our session) lives in the route
layer, not here.

CAS protocol reference:
https://apereo.github.io/cas/development/protocol/CAS-Protocol-Specification.html
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET

import httpx

logger = logging.getLogger(__name__)

# httpx's own client logger emits an INFO-level "HTTP Request: GET <url>"
# line that includes the full query string — i.e. the raw ticket, since it
# is passed as a query param. That logger defaults to NOTSET and would
# otherwise inherit an ancestor's DEBUG/INFO level (e.g. a test's
# caplog.set_level(logging.DEBUG)) and leak the ticket into the log stream.
# Pin it above INFO here so the ticket can never surface via httpx's own
# logging, independent of what any caller/test sets on the root logger.
logging.getLogger("httpx").setLevel(logging.WARNING)

CAS_NS = "{http://www.yale.edu/tp/cas}"


class CasValidationError(RuntimeError):
    """The CAS server rejected the ticket or returned a malformed response."""


async def validate_ticket(
    *,
    cas_server_url: str,
    validate_route: str,
    service_url: str,
    ticket: str,
    http: httpx.AsyncClient,
    timeout_s: float = 30.0,
) -> str:
    """Validate a CAS ticket and return the authenticated username.

    Raises ``CasValidationError`` on any failure (HTTP, XML, schema,
    IdP-said-no). The Flask reference returned a redirect on every failure;
    we surface a typed error so the route layer can convert to 401/502 as
    appropriate.

    The raw ticket is NEVER logged — it is a short-lived bearer credential.
    Only its length is logged, to confirm CAS actually sent one.
    """
    url = f"{cas_server_url.rstrip('/')}{validate_route}"
    params = {"service": service_url, "ticket": ticket}

    logger.info(
        "cas.validate.start validate_url=%s service_url=%s ticket_len=%d",
        url,
        service_url,
        len(ticket),
    )

    try:
        response = await http.get(url, params=params, timeout=timeout_s)
    except httpx.HTTPError as exc:
        # CAS deployments frequently fail strict TLS verification from
        # inside containers without a recent ca-certificates bundle. If you
        # see SSL: CERTIFICATE_VERIFY_FAILED here, flip settings.cas_verify_tls.
        logger.error(
            "cas.validate.network error=%s error_type=%s validate_url=%s",
            exc,
            type(exc).__name__,
            url,
        )
        raise CasValidationError(f"cas server unreachable: {exc}") from exc

    if response.status_code != 200:
        logger.error(
            "cas.validate.http status=%d validate_url=%s response_excerpt=%s",
            response.status_code,
            url,
            response.text[:500],
        )
        raise CasValidationError(f"cas server returned {response.status_code}")

    try:
        # noqa: S314 — stdlib ElementTree does not resolve external entities
        # or external DTD subsets, so this is XXE-safe by construction; no
        # defusedxml dependency needed.
        root = ET.fromstring(response.text)  # noqa: S314
    except ET.ParseError as exc:
        logger.error(
            "cas.validate.parse error=%s response_excerpt=%s",
            exc,
            response.text[:500],
        )
        raise CasValidationError(f"cas response malformed: {exc}") from exc

    success = root.find(f".//{CAS_NS}authenticationSuccess")
    if success is None:
        failure = root.find(f".//{CAS_NS}authenticationFailure")
        code = failure.get("code") if failure is not None else "unknown"
        # The CAS failure body usually carries useful diagnostics
        # (INVALID_SERVICE, INVALID_TICKET, etc.) — log it.
        detail = (failure.text or "").strip() if failure is not None else ""
        logger.warning(
            "cas.validate.rejected code=%s detail=%s service_url=%s",
            code,
            detail,
            service_url,
        )
        raise CasValidationError(f"cas authentication failed: {code}")

    username = success.findtext(f"{CAS_NS}user")
    if username is None or not username.strip():
        logger.error(
            "cas.validate.no_user_tag response_excerpt=%s", response.text[:500]
        )
        raise CasValidationError("cas success but no <user> element")

    logger.info("cas.validate.ok username_len=%d", len(username.strip()))
    return username.strip()
