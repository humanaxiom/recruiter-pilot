"""RED pin — security finding, fix/auth-boundary-fails-open (F1b).

**The proven defect.** ``validate_startup_auth_config``'s own docstring
already claims it exists to "refuse to boot on an auth configuration that
would silently fail open" — but today it checks only for a stale legacy
``API_KEY`` and byte-identical role-key collisions. It does NOT catch the
configuration that actually shipped and was exploited: ``cas_enabled=True``
(a real deploy, not single-operator local dev) with all four
``api_key_*`` fields empty. That combination boots clean today and is
EXACTLY the shape a live audit proved lets a cookie-less, key-less caller
reach every write route with no credential at all.

**Why this must be a STARTUP refusal, not just a runtime 403.** The route-
level fix (``require_session_role`` 403ing ``user is None``, pinned
elsewhere in this slice) closes the hole for THIS deploy once it is
applied — but nothing stops the NEXT deploy from shipping the identical
misconfiguration again with a coder who never reads the audit. A fail-open
auth boundary that boots silently is a standing invitation to repeat this
exact incident; refusing to boot makes the misconfiguration impossible to
ship by accident.

**The deployment-channel half of the same finding.** Even a correctly
patched ``validate_startup_auth_config`` is useless if there is no CHANNEL
by which an operator can actually configure the four role keys: today
``docker-compose.yml``'s ``&app_env`` block forwards zero ``API_KEY_*``
variables (0 matches) and ``.env.example`` defines none either — an
operator following the documented ``.env.example`` -> ``.env`` workflow has
no way to set any of the four keys even if they wanted to. Both gaps are
pinned here; either one alone leaves the vulnerability shippable again.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.settings import Settings, validate_startup_auth_config

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DOCKER_COMPOSE_YML = _REPO_ROOT / "docker-compose.yml"
_ENV_EXAMPLE = _REPO_ROOT / ".env.example"

_ROLE_KEY_ENV_VARS = (
    "API_KEY_ADMIN",
    "API_KEY_RECRUITER",
    "API_KEY_HIRING_MANAGER",
    "API_KEY_AUDITOR",
)


def _cas_enabled_no_keys(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "cas_enabled": True,
        "skill_hash_salt": "test-salt",
        "pii_key": "test-key",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


# ── the startup refusal itself ──────────────────────────────────────────


def test_validate_startup_auth_config_raises_when_cas_enabled_with_no_role_keys() -> (
    None
):
    """THE fix this file pins. Today this boots clean; it must instead
    refuse — this is the exact configuration a live audit proved lets
    anyone flip ``PATCH /jobs/{id}`` with no credential at all."""
    s = _cas_enabled_no_keys()
    with pytest.raises(RuntimeError):
        validate_startup_auth_config(s)


def test_raise_message_names_cas_and_at_least_one_role_key_env_var() -> None:
    """Not a strict content assertion (mirrors the existing collision-
    message pin's discipline), just enough to prove the message is
    informative and actionable rather than a bare traceback."""
    s = _cas_enabled_no_keys()
    with pytest.raises(RuntimeError) as excinfo:
        validate_startup_auth_config(s)
    message = str(excinfo.value).lower()
    assert "cas" in message
    assert any(var.lower() in message for var in _ROLE_KEY_ENV_VARS)


@pytest.mark.parametrize("configured_field", _ROLE_KEY_ENV_VARS)
def test_validate_startup_auth_config_passes_when_cas_enabled_with_any_one_role_key(
    configured_field: str,
) -> None:
    """Positive control — this is not a blanket refusal to ever enable CAS;
    configuring even ONE role key is enough to boot clean."""
    field_name = configured_field.lower()
    s = _cas_enabled_no_keys(**{field_name: "a-real-configured-secret"})
    validate_startup_auth_config(s)  # must not raise


def test_validate_startup_auth_config_passes_when_cas_disabled_with_no_role_keys() -> (
    None
):
    """The legitimate single-operator local-dev boot (``cas_enabled=False``,
    the ``Settings()`` default) must keep booting clean — this check is
    scoped to CAS-enabled deploys only, never a blanket refusal of the
    all-disabled default."""
    validate_startup_auth_config(Settings())


# ── the deployment channel must exist at all ────────────────────────────


def test_docker_compose_app_env_forwards_all_four_role_keys() -> None:
    """``docker-compose.yml``'s ``&app_env`` anchor (shared by ``api`` and
    ``worker``) must forward every one of the four role-key env vars — an
    operator cannot configure a key ``validate_startup_auth_config`` cannot
    even see."""
    assert _DOCKER_COMPOSE_YML.is_file(), f"expected {_DOCKER_COMPOSE_YML} to exist"
    text = _DOCKER_COMPOSE_YML.read_text(encoding="utf-8")
    missing = [var for var in _ROLE_KEY_ENV_VARS if var not in text]
    assert not missing, (
        f"docker-compose.yml's app_env block is missing: {missing} — there is "
        "no channel to configure these keys in a real deploy"
    )


def test_env_example_defines_all_four_role_keys() -> None:
    """``.env.example`` is the documented ``.env`` starting point — it must
    define (even if left blank, mirroring ``PII_KEY``/``SKILL_HASH_SALT``'s
    own discipline) every one of the four role-key variables, so an
    operator following the README's setup steps sees them at all."""
    assert _ENV_EXAMPLE.is_file(), f"expected {_ENV_EXAMPLE} to exist"
    text = _ENV_EXAMPLE.read_text(encoding="utf-8")
    missing = [var for var in _ROLE_KEY_ENV_VARS if var not in text]
    assert not missing, f".env.example is missing: {missing}"


__all__: list[str] = []
