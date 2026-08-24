"""Unit tests for ``src/services/cas_service.py`` (FU-5 slice 4, ADR-019 §10).

Ports ``C:\\repos\\hris\\tests\\unit\\test_cas_service.py`` (9 tests) onto this
repo's conventions: stdlib ``logging`` (not ``structlog`` — not a dependency
here; see ``src/services/outbox_service.py`` / ``src/api/deps.py`` for the
established ``logging.getLogger(__name__)`` pattern) and the
``httpx.MockTransport`` style already used in ``test_llm_client.py``.

Pure HTTP-mocked via ``httpx.MockTransport`` — no real network, no real CAS
server, no Postgres. This module is stdlib XML parsing (``xml.etree.
ElementTree``) over a fully mocked transport, so the OFFLINE unit suite is
structurally sufficient to pin its behaviour end-to-end; there is no real CAS
server reachable from CI to exercise in an integration test, so none is
added here (see CLAUDE.md's "offline is not always enough" guidance — the
exception is a module with no real backing service at all).

XXE safety note (for the security gate): CAS 2.0 responses are parsed with
stdlib ``xml.etree.ElementTree.fromstring``, which — unlike ``lxml`` or
``defusedxml``-less C extensions — does not resolve external entities or
external DTD subsets by default. It is XXE-safe by construction, with no
extra dependency. ``test_validate_ticket_does_not_expand_external_entities``
below documents this property with an XXE payload; it is not a defense
mechanism, just a regression pin should ``ET.fromstring`` ever be swapped for
a parser without that guarantee.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
import pytest

from src.services.cas_service import CAS_NS, CasValidationError, validate_ticket

CAS_URL = "https://cas.example.com/cas"


def _xml_success(username: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<cas:serviceResponse xmlns:cas="http://www.yale.edu/tp/cas">
  <cas:authenticationSuccess>
    <cas:user>{username}</cas:user>
  </cas:authenticationSuccess>
</cas:serviceResponse>"""


def _xml_failure(code: str = "INVALID_TICKET") -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<cas:serviceResponse xmlns:cas="http://www.yale.edu/tp/cas">
  <cas:authenticationFailure code="{code}">ticket rejected</cas:authenticationFailure>
</cas:serviceResponse>"""


def _make_client(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ── namespace drift guard ────────────────────────────────────────────────


def test_namespace_constant_matches_yale_cas() -> None:
    # Sanity check: CAS_NS must match the Yale CAS XML namespace used in the
    # CAS 2.0 protocol response. Don't accept silent drift — every element
    # lookup in validate_ticket is built from this string.
    assert CAS_NS == "{http://www.yale.edu/tp/cas}"


# ── happy path ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_validate_ticket_returns_username_on_success() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_xml_success("asalah"))

    async with _make_client(handler) as http:
        username = await validate_ticket(
            cas_server_url=CAS_URL,
            validate_route="/serviceValidate",
            service_url="https://app/cb",
            ticket="ST-1234",
            http=http,
        )
    assert username == "asalah"


@pytest.mark.asyncio
async def test_validate_ticket_strips_whitespace_around_username() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_xml_success("  asalah  "))

    async with _make_client(handler) as http:
        username = await validate_ticket(
            cas_server_url=CAS_URL,
            validate_route="/serviceValidate",
            service_url="https://app/cb",
            ticket="ST-1234",
            http=http,
        )
    assert username == "asalah"


# ── CAS-said-no ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_validate_ticket_raises_on_authentication_failure() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_xml_failure("INVALID_TICKET"))

    async with _make_client(handler) as http:
        with pytest.raises(CasValidationError, match="INVALID_TICKET"):
            await validate_ticket(
                cas_server_url=CAS_URL,
                validate_route="/serviceValidate",
                service_url="https://app/cb",
                ticket="ST-bad",
                http=http,
            )


@pytest.mark.parametrize(
    "code", ["INVALID_TICKET", "INVALID_SERVICE", "EXPIRED_TICKET"]
)
@pytest.mark.asyncio
async def test_validate_ticket_surfaces_the_cas_failure_code(code: str) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_xml_failure(code))

    async with _make_client(handler) as http:
        with pytest.raises(CasValidationError, match=code):
            await validate_ticket(
                cas_server_url=CAS_URL,
                validate_route="/serviceValidate",
                service_url="https://app/cb",
                ticket="ST-bad",
                http=http,
            )


# ── transport / HTTP failures ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_validate_ticket_raises_on_non_200() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal error")

    async with _make_client(handler) as http:
        with pytest.raises(CasValidationError, match="500"):
            await validate_ticket(
                cas_server_url=CAS_URL,
                validate_route="/serviceValidate",
                service_url="https://app/cb",
                ticket="ST-1234",
                http=http,
            )


@pytest.mark.asyncio
async def test_validate_ticket_raises_on_network_failure() -> None:
    """CAS unreachable (host down, TLS failure, DNS) must surface as a typed
    CasValidationError mentioning "unreachable" — not a bare httpx exception
    escaping into the route layer."""

    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    async with _make_client(handler) as http:
        with pytest.raises(CasValidationError, match="unreachable"):
            await validate_ticket(
                cas_server_url=CAS_URL,
                validate_route="/serviceValidate",
                service_url="https://app/cb",
                ticket="ST-1234",
                http=http,
            )


# ── malformed / schema-invalid responses ─────────────────────────────────


@pytest.mark.asyncio
async def test_validate_ticket_raises_on_malformed_xml() -> None:
    """A non-XML / truncated body must raise CasValidationError, not an
    uncaught xml.etree.ElementTree.ParseError."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<not-xml")

    async with _make_client(handler) as http:
        with pytest.raises(CasValidationError, match="malformed"):
            await validate_ticket(
                cas_server_url=CAS_URL,
                validate_route="/serviceValidate",
                service_url="https://app/cb",
                ticket="ST-1234",
                http=http,
            )


@pytest.mark.asyncio
async def test_validate_ticket_raises_on_empty_body() -> None:
    """An empty response body is also a ParseError from ElementTree — must
    not escape uncaught either."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="")

    async with _make_client(handler) as http:
        with pytest.raises(CasValidationError):
            await validate_ticket(
                cas_server_url=CAS_URL,
                validate_route="/serviceValidate",
                service_url="https://app/cb",
                ticket="ST-1234",
                http=http,
            )


@pytest.mark.asyncio
async def test_validate_ticket_raises_when_success_has_no_user_element() -> None:
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<cas:serviceResponse xmlns:cas="http://www.yale.edu/tp/cas">
  <cas:authenticationSuccess>
  </cas:authenticationSuccess>
</cas:serviceResponse>"""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=xml)

    async with _make_client(handler) as http:
        with pytest.raises(CasValidationError, match="no <user>"):
            await validate_ticket(
                cas_server_url=CAS_URL,
                validate_route="/serviceValidate",
                service_url="https://app/cb",
                ticket="ST-1234",
                http=http,
            )


@pytest.mark.asyncio
async def test_validate_ticket_raises_when_success_has_empty_user_element() -> None:
    """<cas:user></cas:user> (present but empty/whitespace-only) must be
    treated the same as absent — an empty username is not a valid identity."""
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<cas:serviceResponse xmlns:cas="http://www.yale.edu/tp/cas">
  <cas:authenticationSuccess>
    <cas:user>   </cas:user>
  </cas:authenticationSuccess>
</cas:serviceResponse>"""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=xml)

    async with _make_client(handler) as http:
        with pytest.raises(CasValidationError):
            await validate_ticket(
                cas_server_url=CAS_URL,
                validate_route="/serviceValidate",
                service_url="https://app/cb",
                ticket="ST-1234",
                http=http,
            )


# ── request construction ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_validate_ticket_passes_service_and_ticket_query_params() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, text=_xml_success("u"))

    async with _make_client(handler) as http:
        await validate_ticket(
            cas_server_url="https://cas.example.com/cas/",  # trailing slash
            validate_route="/serviceValidate",
            service_url="https://app/cb",
            ticket="ST-XYZ",
            http=http,
        )

    # Trailing slash on cas_server_url is stripped so the path doesn't get a
    # doubled "//serviceValidate"; query params present.
    assert "https://cas.example.com/cas/serviceValidate" in captured["url"]
    assert "ticket=ST-XYZ" in captured["url"]
    assert "service=https" in captured["url"]


@pytest.mark.asyncio
async def test_validate_ticket_normalizes_no_trailing_slash_the_same_way() -> None:
    """No trailing slash on cas_server_url must build the identical URL as
    the trailing-slash case above — rstrip('/') is idempotent either way."""
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, text=_xml_success("u"))

    async with _make_client(handler) as http:
        await validate_ticket(
            cas_server_url="https://cas.example.com/cas",  # no trailing slash
            validate_route="/serviceValidate",
            service_url="https://app/cb",
            ticket="ST-XYZ",
            http=http,
        )

    assert "https://cas.example.com/cas/serviceValidate" in captured["url"]


# ── XXE safety (documentation pin, not a defense mechanism) ─────────────


@pytest.mark.asyncio
async def test_validate_ticket_does_not_expand_external_entities() -> None:
    """stdlib ``ET.fromstring`` does not resolve external entities/DTDs, so
    an XXE payload embedded in a CAS response body must not read a local
    file or otherwise "succeed" with attacker-controlled content substituted
    in — it must be treated as a normal (non-matching or malformed) CAS
    response and raise CasValidationError, never leak file contents into the
    returned username."""
    xxe_payload = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE cas:serviceResponse [
  <!ENTITY xxe SYSTEM "file:///etc/passwd">
]>
<cas:serviceResponse xmlns:cas="http://www.yale.edu/tp/cas">
  <cas:authenticationSuccess>
    <cas:user>&xxe;</cas:user>
  </cas:authenticationSuccess>
</cas:serviceResponse>"""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=xxe_payload)

    async with _make_client(handler) as http:
        with pytest.raises(CasValidationError):
            await validate_ticket(
                cas_server_url=CAS_URL,
                validate_route="/serviceValidate",
                service_url="https://app/cb",
                ticket="ST-1234",
                http=http,
            )


# ── the ticket itself is never logged ────────────────────────────────────


@pytest.mark.asyncio
async def test_validate_ticket_never_logs_the_raw_ticket_value(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The ticket is a short-lived bearer credential (CAS ST-/PT- token).
    Every log call the module makes — start, success, failure, and error
    branches — must never include the raw ticket string, only (at most) its
    length. Exercised across all four branches so a future log line can't
    quietly reintroduce it in just one of them."""
    caplog.set_level(logging.DEBUG)
    secret_ticket = "ST-super-secret-ticket-value-do-not-log-me"

    # success branch
    async with _make_client(
        lambda _r: httpx.Response(200, text=_xml_success("asalah"))
    ) as http:
        await validate_ticket(
            cas_server_url=CAS_URL,
            validate_route="/serviceValidate",
            service_url="https://app/cb",
            ticket=secret_ticket,
            http=http,
        )

    # authenticationFailure branch
    async with _make_client(
        lambda _r: httpx.Response(200, text=_xml_failure("INVALID_TICKET"))
    ) as http:
        with pytest.raises(CasValidationError):
            await validate_ticket(
                cas_server_url=CAS_URL,
                validate_route="/serviceValidate",
                service_url="https://app/cb",
                ticket=secret_ticket,
                http=http,
            )

    # non-200 branch
    async with _make_client(lambda _r: httpx.Response(500, text="boom")) as http:
        with pytest.raises(CasValidationError):
            await validate_ticket(
                cas_server_url=CAS_URL,
                validate_route="/serviceValidate",
                service_url="https://app/cb",
                ticket=secret_ticket,
                http=http,
            )

    # network-failure branch
    def _raise(_r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    async with _make_client(_raise) as http:
        with pytest.raises(CasValidationError):
            await validate_ticket(
                cas_server_url=CAS_URL,
                validate_route="/serviceValidate",
                service_url="https://app/cb",
                ticket=secret_ticket,
                http=http,
            )

    joined = " ".join(r.getMessage() for r in caplog.records)
    assert secret_ticket not in joined
