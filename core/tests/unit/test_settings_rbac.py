"""Unit tests for FU-4's RBAC settings surface — ``src.settings`` (the four
flat ``api_key_*`` role fields, ``Settings.auth_enabled``, and
``validate_startup_auth_config``; none of the three exist yet). RED half of
the TDD cycle — this whole file fails at collection with an
``ImportError``/``AttributeError`` until the coder adds them.

**Locked decisions this file pins (see the FU-4 decisions doc):**

* ``Settings.api_key`` is REPLACED as the auth switch by four flat fields:
  ``api_key_admin`` / ``api_key_recruiter`` / ``api_key_hiring_manager`` /
  ``api_key_auditor``, each defaulting to ``""``. The field is KEPT on
  ``Settings`` (not removed) but repurposed as legacy-detection-only — it no
  longer gates auth at all, it exists solely so startup can detect a stale
  ``API_KEY`` env var and refuse to boot (D1).
* ``Settings.auth_enabled: bool`` (a computed property) is ``False`` iff ALL
  FOUR role-key fields are empty, ``True`` iff ANY is non-empty. This is the
  single source of truth ``src.api.deps`` and the lifespan both read — no
  duplicated "any non-empty" logic scattered across modules.
* ``validate_startup_auth_config(settings: Settings) -> None`` — a
  module-level function in ``src.settings`` (mirrors ``src.api.main``'s
  inline ``SKILL_HASH_SALT`` refusal, but complex enough to warrant its own
  testable function rather than an inline ``if``):
  - D1: raises ``RuntimeError`` naming all four replacement env vars
    (``API_KEY_ADMIN``, ``API_KEY_RECRUITER``, ``API_KEY_HIRING_MANAGER``,
    ``API_KEY_AUDITOR``) when the legacy ``settings.api_key`` is non-empty —
    regardless of whether any new-style field is also set. A stale ``API_KEY``
    left in a deploy's ``.env`` must be impossible to silently ignore
    (``Settings.model_config`` has ``extra="ignore"``, so a bare env-var
    rename would otherwise fall through to auth-disabled fail-open).
  - Collision refusal: raises ``RuntimeError`` when two CONFIGURED (non-empty)
    role keys are byte-identical — a silent role collapse (e.g. recruiter and
    auditor keys accidentally set to the same value) would let an auditor key
    holder perform recruiter-only actions. Two role fields BOTH being empty is
    NOT a collision — that is simply "not configured", the normal disabled
    state, and must not raise.
  - Passes silently (returns ``None``) on a valid configuration: legacy
    ``api_key`` empty, and every configured role key distinct.
"""

from __future__ import annotations

import pytest

from src.settings import Settings, validate_startup_auth_config

_ROLE_FIELDS = (
    "api_key_admin",
    "api_key_recruiter",
    "api_key_hiring_manager",
    "api_key_auditor",
)


# ── the four role-key fields ─────────────────────────────────────────────


def test_all_four_role_key_fields_default_to_empty_string() -> None:
    s = Settings()
    assert s.api_key_admin == ""
    assert s.api_key_recruiter == ""
    assert s.api_key_hiring_manager == ""
    assert s.api_key_auditor == ""


@pytest.mark.parametrize("field", _ROLE_FIELDS)
def test_each_role_key_field_is_independently_settable(field: str) -> None:
    s = Settings(**{field: "a-configured-secret"})
    assert getattr(s, field) == "a-configured-secret"
    # every OTHER field stays at its empty default — no cross-talk.
    for other in _ROLE_FIELDS:
        if other != field:
            assert getattr(s, other) == ""


def test_legacy_api_key_field_still_exists_and_defaults_empty() -> None:
    """Kept on Settings (not deleted) — legacy-detection-only (D1)."""
    s = Settings()
    assert s.api_key == ""


# ── Settings.auth_enabled ────────────────────────────────────────────────


def test_auth_disabled_when_all_four_role_keys_are_empty() -> None:
    s = Settings()
    assert s.auth_enabled is False


@pytest.mark.parametrize("field", _ROLE_FIELDS)
def test_auth_enabled_when_exactly_one_role_key_is_set(field: str) -> None:
    s = Settings(**{field: "secret"})
    assert s.auth_enabled is True


def test_auth_enabled_when_all_four_role_keys_are_set() -> None:
    s = Settings(
        api_key_admin="a1",
        api_key_recruiter="a2",
        api_key_hiring_manager="a3",
        api_key_auditor="a4",
    )
    assert s.auth_enabled is True


def test_auth_enabled_is_unaffected_by_the_legacy_api_key_field() -> None:
    """The legacy field is detection-only — it must never itself flip auth
    on, or a stale ``API_KEY`` with none of the new fields set would enable
    auth in a confusing, undocumented way instead of failing loud via
    ``validate_startup_auth_config``."""
    s = Settings(api_key="stale-value-only")
    assert s.auth_enabled is False


# ── D1: hard-fail on a set legacy API_KEY ────────────────────────────────


def test_validate_startup_auth_config_raises_on_a_set_legacy_api_key() -> None:
    s = Settings(api_key="a-stale-secret")
    with pytest.raises(RuntimeError) as excinfo:
        validate_startup_auth_config(s)
    message = str(excinfo.value)
    for var in (
        "API_KEY_ADMIN",
        "API_KEY_RECRUITER",
        "API_KEY_HIRING_MANAGER",
        "API_KEY_AUDITOR",
    ):
        assert var in message


def test_validate_startup_auth_config_raises_on_legacy_key_even_with_new_fields_set() -> (  # noqa: E501
    None
):
    """A stale API_KEY alongside correctly-configured new fields is STILL a
    hard-fail — the legacy field must be removed from config entirely, not
    merely superseded."""
    s = Settings(
        api_key="a-stale-secret",
        api_key_admin="a1",
        api_key_recruiter="a2",
        api_key_hiring_manager="a3",
        api_key_auditor="a4",
    )
    with pytest.raises(RuntimeError, match="API_KEY_ADMIN"):
        validate_startup_auth_config(s)


def test_validate_startup_auth_config_passes_when_legacy_key_is_empty() -> None:
    s = Settings(api_key_admin="a1")
    validate_startup_auth_config(s)  # must not raise


def test_validate_startup_auth_config_passes_on_a_fully_disabled_config() -> None:
    """Empty legacy field, all four role keys empty (today's local-dev
    default) — must not raise."""
    validate_startup_auth_config(Settings())


# ── byte-identical-key collision refusal ─────────────────────────────────


@pytest.mark.parametrize(
    "field_a,field_b",
    [
        ("api_key_admin", "api_key_recruiter"),
        ("api_key_admin", "api_key_hiring_manager"),
        ("api_key_admin", "api_key_auditor"),
        ("api_key_recruiter", "api_key_hiring_manager"),
        ("api_key_recruiter", "api_key_auditor"),
        ("api_key_hiring_manager", "api_key_auditor"),
    ],
)
def test_validate_startup_auth_config_raises_on_any_colliding_pair(
    field_a: str, field_b: str
) -> None:
    s = Settings(**{field_a: "shared-secret-value", field_b: "shared-secret-value"})
    with pytest.raises(RuntimeError):
        validate_startup_auth_config(s)


def test_validate_startup_auth_config_passes_when_all_configured_keys_are_distinct() -> (  # noqa: E501
    None
):
    s = Settings(
        api_key_admin="admin-secret-1",
        api_key_recruiter="recruiter-secret-2",
        api_key_hiring_manager="hm-secret-3",
        api_key_auditor="auditor-secret-4",
    )
    validate_startup_auth_config(s)  # must not raise


def test_two_empty_role_keys_are_not_a_collision() -> None:
    """Both unset (``""``) is the normal disabled state, not a role
    collapse — only CONFIGURED (non-empty) duplicate keys must refuse boot."""
    s = Settings(api_key_admin="only-this-one-is-set")
    validate_startup_auth_config(s)  # must not raise despite 3 empty fields


def test_three_empty_role_keys_are_not_a_collision() -> None:
    s = Settings(api_key_recruiter="only-recruiter-is-set")
    validate_startup_auth_config(s)  # must not raise


def test_collision_message_mentions_the_word_role_or_key() -> None:
    """Not a strict content assertion, just enough to prove the message is
    informative rather than a bare traceback."""
    s = Settings(api_key_admin="dup", api_key_recruiter="dup")
    with pytest.raises(RuntimeError) as excinfo:
        validate_startup_auth_config(s)
    message = str(excinfo.value).lower()
    assert "role" in message or "key" in message
