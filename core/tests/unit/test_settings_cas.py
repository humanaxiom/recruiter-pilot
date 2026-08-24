"""Unit tests for FU-5 slice 2's CAS + session settings surface —
``src.settings`` (thirteen new flat fields; none exist yet). RED half of the
TDD cycle — this whole file fails at collection with a ``ValidationError``/
``AttributeError`` until the coder adds the fields.

**Scope note.** These are pure ``pydantic-settings`` field/default/env-override
tests — no DB, no network, no CAS server. ``offline`` genuinely suffices for
this slice: there is nothing here that a real Postgres/Neo4j/Redis/CAS
instance could reveal that a mocked/absent-env-var construction of ``Settings``
cannot. No integration test is added by this file.

**Architecture note — read ADR-019 §10 before touching this file.** The FU-5
authentication architecture was pivoted 2026-07-22 (mid-build) from the
original §8 HMAC ``X-Actor-Assertion`` design to a ported hris-style
FastAPI + Postgres-session CAS model (§10/§10a/§10b). §8 is SUPERSEDED — there
is no ``ACTOR_ASSERTION_SECRET``, no assertion mint/verify module, and this
file intentionally does NOT test any such thing.

**Locked decisions this file pins:**

* Thirteen new ``Settings`` fields (names/defaults ported from hris
  ``apps/api/src/api/config.py``, adapted to this project per ADR-019 §10):
  ``cas_enabled``, ``cas_server_url``, ``cas_validate_route``,
  ``cas_service_base_url``, ``cas_service_from_request``, ``cas_verify_tls``,
  ``cas_dev_fake_user``, ``session_cookie_name``, ``session_ttl_hours``,
  ``session_idle_refresh_hours``, ``session_cookie_secure``,
  ``session_cookie_samesite``, ``default_admin_cas_username``.
* ``default_admin_cas_username`` defaults to exactly ``"asalah"`` — ADR-019
  §10a, the ratified default-admin allowlist. This is a real, deliberately
  committed operational config default (not PII), env-overridable per
  deployment.
* ADR-019 §10b — auth ships disabled by default (``cas_enabled=False``) and
  the all-disabled default configuration must boot clean through
  ``validate_startup_auth_config`` (mirrors the existing FU-4 role-key
  disabled-by-default contract in ``test_settings_rbac.py``).

**Post-login frontend redirect fix (branch
``fix/cas-post-login-frontend-redirect``).** The Flask frontend (``:5000``)
and the FastAPI backend (``:18000``) are different origins. Every existing
CAS redirect target (``next``, sanitized by ``_safe_next`` to a relative
path) is resolved by the BROWSER against whatever origin issued the
``Location`` header — which today is always the API's own origin, so a
successful login lands the user on the raw JSON API instead of the Flask
UI. The fix adds one new field, ``cas_frontend_base_url: str = ""``
(empty string = same-origin, i.e. today's behaviour is preserved exactly),
which a "landing" redirect helper in ``src.api.routes.auth`` prefixes onto
``safe_next`` only for redirects where the user actually LANDS on a page
(the post-validate success redirect and the CAS-disabled dev passthrough) —
NOT the login->CAS-server redirect or the no-ticket restart, both of which
stay on the API by design. See ``test_route_auth.py``'s "frontend landing
redirect" section for the route-level assertions; this file pins only the
new field itself.
"""

from __future__ import annotations

from pytest import MonkeyPatch

from src.settings import Settings, validate_startup_auth_config

# ── Field defaults ───────────────────────────────────────────────────────


def test_cas_enabled_defaults_false() -> None:
    """ADR-019 §10b — the shipped compose default is auth-disabled."""
    assert Settings().cas_enabled is False


def test_cas_server_url_default() -> None:
    assert Settings().cas_server_url == "https://cas.sfu.ca/cas"


def test_cas_validate_route_default() -> None:
    assert Settings().cas_validate_route == "/serviceValidate"


def test_cas_service_base_url_default() -> None:
    assert Settings().cas_service_base_url == "http://localhost:8000"


def test_cas_service_from_request_defaults_false() -> None:
    assert Settings().cas_service_from_request is False


def test_cas_verify_tls_defaults_true() -> None:
    assert Settings().cas_verify_tls is True


def test_cas_dev_fake_user_defaults_empty() -> None:
    assert Settings().cas_dev_fake_user == ""


def test_session_cookie_name_default() -> None:
    assert Settings().session_cookie_name == "ra_session"


def test_session_ttl_hours_default() -> None:
    assert Settings().session_ttl_hours == 8


def test_session_idle_refresh_hours_default() -> None:
    assert Settings().session_idle_refresh_hours == 1


def test_session_cookie_secure_defaults_false() -> None:
    """False by default so local http:// dev works; a real deployment behind
    TLS must explicitly opt in via env."""
    assert Settings().session_cookie_secure is False


def test_session_cookie_samesite_default() -> None:
    assert Settings().session_cookie_samesite == "lax"


def test_default_admin_cas_username_defaults_to_asalah() -> None:
    """ADR-019 §10a — the ratified default-admin CAS allowlist value. Pinned
    explicitly and exactly: this is a real operational identity, not a
    placeholder, and must not silently drift (e.g. to an empty string, which
    would mean no default admin exists on first boot)."""
    assert Settings().default_admin_cas_username == "asalah"


# ── `cas_frontend_base_url` (fix/cas-post-login-frontend-redirect) ───────


def test_cas_frontend_base_url_defaults_to_empty_string() -> None:
    """Empty string = same-origin = today's (buggy-for-cross-origin, but
    UNCHANGED) relative-redirect behaviour is preserved for anyone who has
    not set the new env var — this field must not silently change existing
    deployments' behaviour on upgrade."""
    assert Settings().cas_frontend_base_url == ""


def test_env_override_cas_frontend_base_url(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("CAS_FRONTEND_BASE_URL", "http://localhost:5000")
    assert Settings().cas_frontend_base_url == "http://localhost:5000"


# ── Types / sanity ───────────────────────────────────────────────────────


def test_session_ttl_hours_is_a_positive_int() -> None:
    assert isinstance(Settings().session_ttl_hours, int)
    assert Settings().session_ttl_hours > 0


def test_session_idle_refresh_hours_is_a_positive_int() -> None:
    assert isinstance(Settings().session_idle_refresh_hours, int)
    assert Settings().session_idle_refresh_hours > 0


def test_session_idle_refresh_hours_is_not_greater_than_ttl() -> None:
    """A sliding-window idle refresh interval longer than the session's own
    TTL would never fire — the default configuration must be internally
    coherent."""
    s = Settings()
    assert s.session_idle_refresh_hours <= s.session_ttl_hours


# ── Env overrides (representative subset — the pattern is uniform across
#    all thirteen fields, per pydantic-settings' UPPER_SNAKE_CASE mapping) ──


def test_env_override_cas_enabled(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("CAS_ENABLED", "true")
    assert Settings().cas_enabled is True


def test_env_override_cas_server_url(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("CAS_SERVER_URL", "https://cas.example.edu/cas")
    assert Settings().cas_server_url == "https://cas.example.edu/cas"


def test_env_override_session_ttl_hours(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("SESSION_TTL_HOURS", "24")
    assert Settings().session_ttl_hours == 24


def test_env_override_default_admin_cas_username(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("DEFAULT_ADMIN_CAS_USERNAME", "someone-else")
    assert Settings().default_admin_cas_username == "someone-else"


# ── §10b: all-auth-disabled default must boot clean ─────────────────────


def test_validate_startup_auth_config_passes_on_default_settings_with_cas_fields() -> (  # noqa: E501
    None
):
    """ADR-019 §10b — auth (both the FU-4 role-key mechanism and CAS) ships
    disabled by default and the API must boot without raising. This exercises
    the EXISTING ``validate_startup_auth_config`` (unchanged by this slice —
    CAS adds no new startup-fatal check per §10's supersession of §9.3) against
    a ``Settings()`` instance that now also carries the thirteen new CAS/
    session fields, confirming their presence does not perturb the existing
    role-key validation path."""
    validate_startup_auth_config(Settings())  # must not raise


def test_all_cas_and_session_fields_coexist_with_disabled_role_key_auth() -> None:
    """A fully-default ``Settings()`` has role-key auth disabled (FU-4) AND
    CAS disabled (FU-5, §10b) simultaneously — the two auth mechanisms are
    independent knobs, not mutually exclusive, and neither's default should
    perturb the other's."""
    s = Settings()
    assert s.auth_enabled is False
    assert s.cas_enabled is False
