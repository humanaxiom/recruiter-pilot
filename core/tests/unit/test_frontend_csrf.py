"""Unit tests for ``frontend.csrf`` (FU-4/D4, amended twice; FU-8/ADR-026
amends it a third time — see the section near the bottom of this file).

**FU-4/D4 — the threat this module closes.** Classic CSRF does NOT apply to
the FastAPI backend: it authenticates on the ``X-API-Key`` header, and a
cross-origin ``<form>`` cannot attach a custom header — so a forged
cross-site POST straight at the backend is simply rejected as unauthenticated.
The real gap is the Flask hop: the browser supplies NO credential of its own
for ``POST /resumes/<id>/reveal`` (``core/frontend/app.py``) — Flask attaches
its own server-held recruiter key on the OUTBOUND leg to the backend
(``api_client.build_client``) — so Flask itself cannot distinguish a forged
cross-site auto-submit from a real click. This module is the fix: a
session-bound, one-shot anti-forgery token (NOT a login — it carries no
identity), plus an ``Origin``/``Referer`` same-origin check as
defense-in-depth layered ON TOP of the token, never instead of it.

**Amendment 1 — tokens are scoped PER RÉSUMÉ ID, not per session.** The
shipped v1 design stored a single bare token in the Flask session. The FU-1
reveal button appears on EVERY shortlist card, all posting to the SAME
``resume_reveal`` route: minting one card's token silently invalidated every
OTHER card's already-rendered token (only the most-recently-minted token was
ever valid, session-wide), so the first reveal a recruiter clicked worked and
every other card's reveal 403'd until a full page reload re-minted a fresh
single token. Reveal-from-shortlist is the primary FU-1 workflow, so this was
a functional regression, not a cosmetic one.

**Amendment 2 — shrink each mapping entry so the cap actually fits a signed
cookie.** Amendment 1's first cut used a full ``secrets.token_urlsafe(32)``
token (43 chars) keyed by the raw ~36-char UUID string — MEASURED (not
estimated) against the real itsdangerous-signed session serializer this
gives ~85 raw bytes/entry, and random tokens don't compress (itsdangerous'
zlib step buys nothing), so a full ``MAX_TOKENS_PER_SESSION=64`` mapping
serialized to ~5.2KB — over the ~4093-byte ceiling most browsers silently
enforce on a single cookie (49 entries ≈ 4.03KB, already close; 50 ≈ 4.11KB,
already OVER). Browsers don't error on an oversized cookie — they silently
DROP it, so the whole session vanishes and every subsequent reveal 403s: the
exact regression Amendment 1 exists to prevent, re-triggered at full
shortlist size (shortlist rows are structurally capped at the stage-1
``k=50`` oversample, ADR-012 SEC-3, so a full shortlist render was exactly
the scenario that broke). The fix shrinks each entry instead of the cap:
- the token is ``secrets.token_urlsafe(16)`` (22 chars) — still 128 bits of
  entropy, ample for a one-shot anti-forgery value;
- the mapping KEY is the first 12 hex characters of
  ``hashlib.sha256(str(resume_id).encode()).hexdigest()`` instead of the
  raw ~36-char UUID string.
This drops each entry to ~40 bytes; MEASURED at ``MAX_TOKENS_PER_SESSION=64``
the real signed cookie now serializes to roughly 2.4KB — comfortably under
the ~4093-byte ceiling, with headroom to spare. See
``test_serialized_session_cookie_stays_under_the_4093_byte_ceiling_at_cap``
below, which measures the ACTUAL cookie rather than re-deriving an estimate
(the estimate is what let the v1 shrink's wrong cap through unnoticed).

**The contract this file locks (the coder implements exactly this shape):**

* ``SESSION_KEY: str`` — the Flask session dict key under which the current
  mapping is stored (Flask's EXISTING signed session, already used for
  ``flash()`` — ``core/frontend/app.py`` sets ``app.secret_key`` from
  ``settings.flask_secret_key``). The stored value is a MAPPING of
  ``<12-hex-char truncated sha256 of resume_id> -> <22-char token>``
  (JSON-object-shaped, since the signed session is JSON-serialized — so keys
  are strings), never a bare token string and never keyed by the raw
  résumé id.
* ``FORM_FIELD: str`` — MUST equal ``"csrf_token"``. This is the form field
  name ``resume_detail.html``'s and ``shortlist_cards.html``'s reveal
  ``<form>``s render as a hidden input, and the field name
  ``frontend.app.resume_reveal`` reads from ``request.form``.
* ``MAX_TOKENS_PER_SESSION: int`` — MUST equal ``64`` (unchanged by Amendment
  2 — only the per-entry byte cost shrank, not the cap). Shortlist rows are
  structurally capped at the stage-1 ``k=50`` oversample (ADR-012 SEC-3), so
  a single shortlist render mints at most 50 tokens; 64 clears that with
  headroom for a couple of OTHER open résumé tabs in the same browser
  session. When minting a token would push the mapping over this cap, the
  OLDEST entry (by insertion/issue order) is evicted first.
* ``issue_token(resume_id: Any) -> str`` — call inside an active Flask
  request context. Generates a token with ``secrets.token_urlsafe(16)``
  (imported as ``import secrets`` — NOT ``from secrets import
  token_urlsafe``, so tests can monkeypatch ``csrf.secrets.token_urlsafe``;
  ``nbytes=16`` EXACTLY, pinned literally — this is the byte count the
  cookie-budget math above depends on), stores it in the
  ``flask.session[SESSION_KEY]`` mapping under the 12-hex-char truncated
  ``hashlib.sha256`` digest of ``str(resume_id)`` (imported as ``import
  hashlib``, so tests can monkeypatch ``csrf.hashlib.sha256``) — overwriting
  any previously-issued, unconsumed token for that SAME ``resume_id``
  (tokens for OTHER résumé ids are left untouched), evicting the oldest
  entry first if the mapping would exceed ``MAX_TOKENS_PER_SESSION`` — and
  returns the new token. Never uses the ``random`` module.
* ``verify_and_consume(resume_id: Any, submitted: str | None) -> bool`` —
  call inside an active Flask request context. Hashes ``resume_id`` the same
  way and POPS ONLY that key's entry out of the ``flask.session[SESSION_KEY]``
  mapping UNCONDITIONALLY (so the stored token is a strict one-shot PER
  RÉSUMÉ: this call invalidates that résumé's token whether or not
  ``submitted`` matches, but leaves every OTHER résumé's token untouched).
  Returns ``True`` iff a token was stored for ``resume_id`` AND ``submitted``
  is truthy AND ``secrets.compare_digest(stored, submitted)`` is ``True``
  (imported the same ``import secrets`` way, so tests can monkeypatch
  ``csrf.secrets.compare_digest``). Returns ``False`` in every other case
  (no stored token for this ``resume_id``, ``submitted`` is ``None``/empty, a
  mismatch, or — the cross-résumé-confusion case — ``submitted`` is a token
  that was validly minted but for a DIFFERENT ``resume_id``) — never raises.
* ``same_origin(req: Any) -> bool`` — UNCHANGED by either amendment.
  Defense-in-depth, evaluated INDEPENDENTLY of the token. Compares the
  request's own origin (``req.host_url``) against an ``Origin`` header if
  present, else a ``Referer`` header if present. Returns ``False`` (blocks)
  iff a cross-origin ``Origin``/``Referer`` is present. Returns ``True``
  (does not block) when the header is absent entirely OR it matches — the
  token stays the PRIMARY control; this is a secondary layer, not a
  replacement.

``frontend.app`` wires this in as follows (asserted indirectly through
``test_frontend_resume_detail.py``'s route-level tests, not here):

* ``GET /resumes/<id>`` calls ``csrf.issue_token(resume_id)`` and passes the
  result to ``resume_detail.html`` as ``csrf_token``, rendered as a hidden
  ``<input name="csrf_token" value="...">`` inside THAT résumé's reveal
  ``<form>``.
* Each shortlist card in ``shortlist_cards.html`` similarly carries its OWN
  token, minted for its OWN ``entry.resume_id`` — so many cards on one
  render each get an independently valid, independently one-shot token.
* ``POST /resumes/<id>/reveal`` first checks ``csrf.same_origin(request)``
  (``abort(403)`` if it fails, WITHOUT ever calling
  ``csrf.verify_and_consume`` or ``api_client.reveal_resume``), then checks
  ``csrf.verify_and_consume(resume_id, request.form.get(csrf.FORM_FIELD))``
  (``abort(403)`` if it fails, WITHOUT ever calling
  ``api_client.reveal_resume``). Only if both pass does it call
  ``api_client.reveal_resume(...)``.
"""

from __future__ import annotations

import hashlib
import secrets
from pathlib import Path
from typing import Any
from uuid import uuid4

import flask

from frontend import csrf
from frontend.app import app

# ── test-local helper: the expected mapping key for a résumé id ────────────


def _hashed_key(resume_id: Any) -> str:
    """The mapping key the SUT is expected to derive for ``resume_id``: the
    first 12 hex characters of a sha256 digest of its string form."""
    return hashlib.sha256(str(resume_id).encode("utf-8")).hexdigest()[:12]


def _install_forced_hash_collision(monkeypatch: Any) -> None:
    """Monkeypatch ``csrf.hashlib.sha256`` so EVERY résumé id hashes to the
    SAME fixed digest — used to force the (astronomically unlikely in
    practice) truncated-hash collision scenario deterministically."""

    class _FixedDigest:
        def hexdigest(self) -> str:
            return "0" * 64

    monkeypatch.setattr(csrf.hashlib, "sha256", lambda _data: _FixedDigest())


# ── module contract: constants ────────────────────────────────────────────


def test_module_exposes_session_key_and_form_field_constants() -> None:
    assert isinstance(csrf.SESSION_KEY, str) and csrf.SESSION_KEY
    assert isinstance(csrf.FORM_FIELD, str) and csrf.FORM_FIELD


def test_form_field_constant_is_csrf_token() -> None:
    """Pinned literally: ``resume_detail.html``, ``shortlist_cards.html`` and
    the route-level tests in ``test_frontend_resume_detail.py`` all hardcode
    the field name ``csrf_token``."""
    assert csrf.FORM_FIELD == "csrf_token"


def test_max_tokens_per_session_constant_is_sixty_four() -> None:
    """The cap itself is UNCHANGED by the Amendment-2 shrink: shortlist rows
    are structurally capped at the stage-1 ``k=50`` oversample (ADR-012
    SEC-3), so a single shortlist render mints at most 50 tokens into one
    session. 64 clears that with headroom for a couple of OTHER open résumé
    tabs in the same browser session. What changed is the per-entry byte
    cost (see the shrink tests below), not this number."""
    assert isinstance(csrf.MAX_TOKENS_PER_SESSION, int)
    assert csrf.MAX_TOKENS_PER_SESSION == 64


# ── issue_token ────────────────────────────────────────────────────────────


def test_issue_token_returns_a_string() -> None:
    resume_id = uuid4()
    with app.test_request_context():
        token = csrf.issue_token(resume_id)
        assert isinstance(token, str)
        assert len(token) >= 16


def test_issue_token_produces_a_22_char_token_from_16_bytes_of_entropy() -> None:
    """``secrets.token_urlsafe(16)`` always yields a 22-character URL-safe
    base64 string (16 bytes -> no padding needed after stripping). Pinned
    literally so a regression back to the old, larger 32-byte/43-char token
    (the shape that blew the cookie budget — see the module docstring) is
    caught immediately, independent of the cookie-size test below."""
    resume_id = uuid4()
    with app.test_request_context():
        token = csrf.issue_token(resume_id)
    assert len(token) == 22


def test_session_holds_a_mapping_keyed_by_a_hashed_resume_id_not_a_bare_token() -> None:
    """Pins the current contract: the session value under ``SESSION_KEY`` is
    a MAPPING keyed by a 12-hex-char truncated sha256 digest of the résumé
    id — never a bare token string, and never the raw ~36-char UUID (that
    was Amendment 1's shape, replaced by Amendment 2's shrink)."""
    resume_id = uuid4()
    with app.test_request_context():
        token = csrf.issue_token(resume_id)
        stored = flask.session[csrf.SESSION_KEY]
        assert isinstance(stored, dict)
        assert stored[_hashed_key(resume_id)] == token


def test_mapping_key_is_exactly_12_lowercase_hex_characters() -> None:
    resume_id = uuid4()
    with app.test_request_context():
        csrf.issue_token(resume_id)
        (key,) = flask.session[csrf.SESSION_KEY].keys()
    assert len(key) == 12
    assert all(c in "0123456789abcdef" for c in key)


def test_issue_token_differs_across_repeated_calls_for_same_resume() -> None:
    resume_id = uuid4()
    with app.test_request_context():
        first = csrf.issue_token(resume_id)
        second = csrf.issue_token(resume_id)
    assert first != second


def test_issue_token_for_one_resume_does_not_disturb_another_resumes_token() -> None:
    """The crux of Amendment 1: minting résumé B's token must not overwrite
    or invalidate résumé A's still-unconsumed token."""
    resume_a, resume_b = uuid4(), uuid4()
    with app.test_request_context():
        token_a = csrf.issue_token(resume_a)
        csrf.issue_token(resume_b)
        assert csrf.verify_and_consume(resume_a, token_a) is True


def test_issue_token_uses_secrets_token_urlsafe_with_16_bytes(monkeypatch: Any) -> None:
    calls: list[int] = []
    real_token_urlsafe = secrets.token_urlsafe

    def spy(nbytes: int = 32) -> str:
        calls.append(nbytes)
        return real_token_urlsafe(nbytes)

    monkeypatch.setattr(csrf.secrets, "token_urlsafe", spy)
    with app.test_request_context():
        csrf.issue_token(uuid4())
    assert calls, "issue_token() must generate its token via secrets.token_urlsafe"
    assert calls[0] == 16, (
        "Amendment 2 pins nbytes=16 exactly (128 bits, still ample) — this is "
        "the byte count the measured cookie-size budget depends on"
    )


def test_issue_token_uses_hashlib_sha256_to_derive_the_mapping_key(
    monkeypatch: Any,
) -> None:
    calls: list[Any] = []
    real_sha256 = hashlib.sha256

    def spy(data: bytes) -> Any:
        calls.append(data)
        return real_sha256(data)

    monkeypatch.setattr(csrf.hashlib, "sha256", spy)
    with app.test_request_context():
        csrf.issue_token(uuid4())
    assert calls, "issue_token() must derive the mapping key via hashlib.sha256"


# ── verify_and_consume: basic one-shot behaviour, scoped per résumé ────────


def test_verify_and_consume_succeeds_with_the_matching_token_for_that_resume() -> None:
    resume_id = uuid4()
    with app.test_request_context():
        token = csrf.issue_token(resume_id)
        assert csrf.verify_and_consume(resume_id, token) is True


def test_verify_and_consume_removes_only_that_resumes_entry_from_the_mapping() -> None:
    """One-shot, per résumé: after ANY call to ``verify_and_consume`` for a
    given ``resume_id``, that résumé's stored token must be gone — whether or
    not the call succeeded — but OTHER résumés' entries are untouched."""
    resume_a, resume_b = uuid4(), uuid4()
    with app.test_request_context():
        token_a = csrf.issue_token(resume_a)
        token_b = csrf.issue_token(resume_b)
        csrf.verify_and_consume(resume_a, token_a)
        mapping = flask.session[csrf.SESSION_KEY]
        assert _hashed_key(resume_a) not in mapping
        assert mapping[_hashed_key(resume_b)] == token_b


def test_verify_and_consume_is_single_use_a_second_call_with_the_same_value_fails() -> (
    None
):
    resume_id = uuid4()
    with app.test_request_context():
        token = csrf.issue_token(resume_id)
        first = csrf.verify_and_consume(resume_id, token)
        second = csrf.verify_and_consume(resume_id, token)
    assert first is True
    assert second is False


def test_verify_and_consume_fails_on_wrong_token_and_still_consumes_the_slot() -> None:
    resume_id = uuid4()
    with app.test_request_context():
        csrf.issue_token(resume_id)
        result = csrf.verify_and_consume(resume_id, "not-the-right-token")
        assert result is False
        assert _hashed_key(resume_id) not in flask.session[csrf.SESSION_KEY]


def test_verify_and_consume_fails_when_no_token_was_ever_issued_for_that_resume() -> (
    None
):
    with app.test_request_context():
        assert csrf.verify_and_consume(uuid4(), "anything") is False


def test_verify_and_consume_fails_for_resume_with_no_token_if_others_have_one() -> None:
    resume_with_token, resume_without = uuid4(), uuid4()
    with app.test_request_context():
        csrf.issue_token(resume_with_token)
        assert csrf.verify_and_consume(resume_without, "anything") is False


def test_verify_and_consume_fails_on_none_submitted() -> None:
    resume_id = uuid4()
    with app.test_request_context():
        csrf.issue_token(resume_id)
        assert csrf.verify_and_consume(resume_id, None) is False


def test_verify_and_consume_fails_on_empty_string_submitted() -> None:
    resume_id = uuid4()
    with app.test_request_context():
        csrf.issue_token(resume_id)
        assert csrf.verify_and_consume(resume_id, "") is False


def test_verify_and_consume_uses_secrets_compare_digest(monkeypatch: Any) -> None:
    calls: list[tuple[Any, Any]] = []
    real_compare_digest = secrets.compare_digest

    def spy(a: Any, b: Any) -> bool:
        calls.append((a, b))
        return bool(real_compare_digest(a, b))

    monkeypatch.setattr(csrf.secrets, "compare_digest", spy)
    resume_id = uuid4()
    with app.test_request_context():
        token = csrf.issue_token(resume_id)
        csrf.verify_and_consume(resume_id, token)
    assert calls, "verify_and_consume() must compare via secrets.compare_digest"


# ── verify_and_consume: cross-résumé confusion ──────────────────────────────


def test_token_minted_for_one_resume_does_not_validate_a_different_resume() -> None:
    """A token minted for résumé A must NOT validate a reveal of résumé B,
    even though both tokens live in the same session at once."""
    resume_a, resume_b = uuid4(), uuid4()
    with app.test_request_context():
        token_a = csrf.issue_token(resume_a)
        assert csrf.verify_and_consume(resume_b, token_a) is False


def test_misdirected_verify_does_not_consume_the_original_resumes_slot() -> None:
    """A misdirected verify attempt (right token, wrong résumé id) must not
    burn the token's rightful résumé's slot — it simply doesn't match that
    lookup key at all, so résumé A's real token is still there afterwards."""
    resume_a, resume_b = uuid4(), uuid4()
    with app.test_request_context():
        token_a = csrf.issue_token(resume_a)
        assert csrf.verify_and_consume(resume_b, token_a) is False
        # résumé A's own token is unaffected and still verifies correctly.
        assert csrf.verify_and_consume(resume_a, token_a) is True


def test_revealing_one_resume_does_not_invalidate_a_sibling_resumes_token() -> None:
    """One-shot is per résumé, not per session: consuming résumé A's token
    via a full (successful) verify must not touch résumé B's token."""
    resume_a, resume_b = uuid4(), uuid4()
    with app.test_request_context():
        token_a = csrf.issue_token(resume_a)
        token_b = csrf.issue_token(resume_b)
        assert csrf.verify_and_consume(resume_a, token_a) is True
        assert csrf.verify_and_consume(resume_b, token_b) is True


def test_replaying_a_consumed_resumes_token_still_fails() -> None:
    """Replay protection for a given résumé is unchanged: a consumed token
    can never be reused."""
    resume_id = uuid4()
    with app.test_request_context():
        token = csrf.issue_token(resume_id)
        assert csrf.verify_and_consume(resume_id, token) is True
        assert csrf.verify_and_consume(resume_id, token) is False


# ── mapping size cap / eviction ─────────────────────────────────────────────


def test_issuing_tokens_beyond_the_cap_keeps_the_mapping_bounded() -> None:
    resume_ids = [uuid4() for _ in range(csrf.MAX_TOKENS_PER_SESSION + 5)]
    with app.test_request_context():
        for resume_id in resume_ids:
            csrf.issue_token(resume_id)
        mapping = flask.session[csrf.SESSION_KEY]
        assert len(mapping) <= csrf.MAX_TOKENS_PER_SESSION


def test_issuing_tokens_beyond_the_cap_evicts_the_oldest_first() -> None:
    resume_ids = [uuid4() for _ in range(csrf.MAX_TOKENS_PER_SESSION + 5)]
    with app.test_request_context():
        tokens = {resume_id: csrf.issue_token(resume_id) for resume_id in resume_ids}
        # The five oldest are gone...
        for evicted in resume_ids[:5]:
            assert csrf.verify_and_consume(evicted, tokens[evicted]) is False
        # ...but the most recently issued MAX_TOKENS_PER_SESSION are intact.
        for kept in resume_ids[5:]:
            assert csrf.verify_and_consume(kept, tokens[kept]) is True


def test_cap_eviction_does_not_break_a_freshly_issued_token() -> None:
    """Filling the mapping past its cap must not corrupt or block minting
    (and validating) a brand-new token afterwards."""
    resume_ids = [uuid4() for _ in range(csrf.MAX_TOKENS_PER_SESSION + 10)]
    fresh_resume_id = uuid4()
    with app.test_request_context():
        for resume_id in resume_ids:
            csrf.issue_token(resume_id)
        fresh_token = csrf.issue_token(fresh_resume_id)
        assert csrf.verify_and_consume(fresh_resume_id, fresh_token) is True


def test_re_issuing_a_token_for_the_same_resume_does_not_grow_the_mapping() -> None:
    """Re-minting résumé A's token twice (e.g. two GETs of the same résumé
    page in one session) must overwrite its existing slot, not add a second
    entry — otherwise a recruiter re-visiting the same handful of résumé
    pages could exhaust the cap and evict unrelated cards' tokens."""
    resume_id = uuid4()
    with app.test_request_context():
        csrf.issue_token(resume_id)
        csrf.issue_token(resume_id)
        mapping = flask.session[csrf.SESSION_KEY]
        assert len(mapping) == 1


# ── Amendment 2 — measured session-cookie byte budget ───────────────────────


def test_serialized_session_cookie_stays_under_the_4093_byte_ceiling_at_cap() -> None:
    """THE assertion whose absence let the wrong cap through: Amendment 1's
    first cut (32-byte/43-char tokens keyed by the raw ~36-char UUID)
    measured at ``MAX_TOKENS_PER_SESSION=64`` to ~5.2KB, over the ~4093-byte
    ceiling most browsers silently enforce on a single cookie. An oversized
    cookie is not rejected with an error — the ENTIRE session silently
    vanishes on the very next request, so every reveal 403s: exactly the
    per-session-single-token regression Amendment 1 exists to fix, re-
    triggered at a full (``k=50``-oversample-sized) shortlist render. This
    test fills the mapping to the cap via the REAL ``issue_token`` and
    measures the ACTUAL itsdangerous-signed cookie value Flask would emit —
    not a re-derived estimate."""
    with app.test_request_context():
        for _ in range(csrf.MAX_TOKENS_PER_SESSION):
            csrf.issue_token(uuid4())
        serializer = app.session_interface.get_signing_serializer(app)
        assert serializer is not None
        cookie_value = serializer.dumps(dict(flask.session))
    raw = (
        cookie_value.encode("utf-8") if isinstance(cookie_value, str) else cookie_value
    )
    assert len(raw) < 4093, (
        f"signed session cookie is {len(raw)} bytes at cap — over the "
        "~4093-byte ceiling browsers silently drop, which empties the "
        "session and re-triggers the per-session-token regression"
    )
    # Comfortable headroom check too: the shrunk scheme measures ~2.4KB in
    # practice. A generous 3500B ceiling here catches a partial/incorrect
    # shrink (e.g. only the token shrank, not the key, or vice versa)
    # without being fragile to itsdangerous version-specific signature
    # overhead.
    assert len(raw) < 3500, (
        f"signed session cookie is {len(raw)} bytes at cap — expected the "
        "shrunk per-entry cost to land well under 3.5KB; a partial shrink "
        "may be in effect"
    )


# ── Amendment 2 — truncated-hash collision (accepted residual risk) ─────────


def test_forced_key_collision_does_not_duplicate_the_mapping_entry(
    monkeypatch: Any,
) -> None:
    """ACCEPTED residual risk of truncating the hash to 12 hex chars (48
    bits): two different résumé ids can, in principle, collide on the same
    mapping key. At ``MAX_TOKENS_PER_SESSION=64`` concurrent entries the real
    collision probability is astronomically small (~64**2 / 2**49 ≈ 4.5e-13
    by the birthday bound) — not worth defending against (doing so would
    require tracking a reverse mapping back to the original résumé id,
    defeating the point of hashing at all). This test forces a collision
    artificially via monkeypatch to pin that it degrades GRACEFULLY — exactly
    like re-issuing a token for the SAME résumé id (the later mint silently
    overwrites the earlier one's slot) — rather than duplicating entries or
    corrupting the mapping."""
    resume_a, resume_b = uuid4(), uuid4()
    _install_forced_hash_collision(monkeypatch)
    with app.test_request_context():
        csrf.issue_token(resume_a)
        csrf.issue_token(resume_b)
        assert len(flask.session[csrf.SESSION_KEY]) == 1


def test_forced_key_collision_invalidates_the_earlier_resumes_stale_token(
    monkeypatch: Any,
) -> None:
    """The flip side of the same accepted residual risk: once résumé B's mint
    overwrites the collided slot, résumé A's still-in-hand token no longer
    verifies — the same outcome as if A's own token had simply been
    re-issued and the old value discarded."""
    resume_a, resume_b = uuid4(), uuid4()
    _install_forced_hash_collision(monkeypatch)
    with app.test_request_context():
        token_a = csrf.issue_token(resume_a)
        csrf.issue_token(resume_b)
        assert csrf.verify_and_consume(resume_a, token_a) is False


# ── same_origin (defense-in-depth) — UNCHANGED by either amendment ─────────


def test_same_origin_true_when_origin_header_matches_the_request_host() -> None:
    with app.test_request_context(headers={"Origin": "http://localhost"}):
        assert csrf.same_origin(flask.request) is True


def test_same_origin_false_when_origin_header_is_cross_site() -> None:
    with app.test_request_context(headers={"Origin": "http://evil.example"}):
        assert csrf.same_origin(flask.request) is False


def test_same_origin_true_when_referer_matches_and_origin_is_absent() -> None:
    with app.test_request_context(headers={"Referer": "http://localhost/resumes/x"}):
        assert csrf.same_origin(flask.request) is True


def test_same_origin_false_when_referer_is_cross_site_and_origin_is_absent() -> None:
    with app.test_request_context(
        headers={"Referer": "http://evil.example/attack.html"}
    ):
        assert csrf.same_origin(flask.request) is False


def test_same_origin_true_when_neither_header_is_present() -> None:
    """The token stays the PRIMARY control; a same-origin request that omits
    both headers (never actually happens for a genuine cross-site forged POST,
    which browsers always tag with `Origin`) is not blocked by this
    secondary layer alone."""
    with app.test_request_context():
        assert csrf.same_origin(flask.request) is True


def test_same_origin_prefers_origin_over_referer_when_both_present() -> None:
    with app.test_request_context(
        headers={
            "Origin": "http://evil.example",
            "Referer": "http://localhost/resumes/x",
        }
    ):
        assert csrf.same_origin(flask.request) is False


# ── structural guard: no `random` module, the right primitives are used ────


def test_csrf_module_never_imports_the_random_module() -> None:
    src = Path(csrf.__file__).read_text(encoding="utf-8")
    assert "import random" not in src
    assert "from random" not in src


def test_csrf_module_uses_token_urlsafe_and_compare_digest_by_name() -> None:
    src = Path(csrf.__file__).read_text(encoding="utf-8")
    assert "secrets.token_urlsafe(" in src
    assert "secrets.compare_digest(" in src


def test_csrf_module_uses_hashlib_sha256_by_name() -> None:
    src = Path(csrf.__file__).read_text(encoding="utf-8")
    assert "hashlib.sha256(" in src


# ── FU-8/ADR-026 amendment — tokens scoped by (résumé id, action) ──────────
#
# **The regression this section exists to catch.** FU-8 adds a SECOND audited
# action on the résumé-detail page — "Withdraw candidate" / "Reinstate" —
# rendered ALONGSIDE the existing FU-1 reveal button, both posting for the
# SAME résumé id. Under the résumé-id-only keying `_session_key_for` used
# before this amendment, minting the withdraw button's token would land in
# the EXACT SAME mapping slot as the reveal button's token (both hash to
# `sha256(str(resume_id))[:12]`), so minting one silently invalidates the
# other — precisely the FU-1/Amendment-1 bug this module was already fixed
# for once, reintroduced by a second action sharing the same résumé id. No
# EXISTING test in this file catches it: every test above only ever exercises
# ONE implicit action (the reveal flow), so two actions colliding on the same
# résumé id was never exercised.
#
# **The fix under test:** `issue_token`/`verify_and_consume` accept an
# ``action: str = "reveal"`` keyword-only parameter. The default value is
# ``"reveal"`` so every EXISTING call site/test above (which never passes
# `action`) is completely unaffected — same signature-compatible call, same
# observable mapping-key behaviour for the reveal flow specifically. A NEW
# action value (e.g. ``"withdraw"``) must mint into an INDEPENDENT slot: for
# the same résumé id, a "reveal" token and a "withdraw" token coexist
# simultaneously, minting/consuming one never disturbs the other, and a token
# minted for one action never validates a verify call for a different action
# (even for the SAME résumé id).


def test_issue_token_accepts_an_action_keyword_argument() -> None:
    resume_id = uuid4()
    with app.test_request_context():
        token = csrf.issue_token(resume_id, action="withdraw")
    assert isinstance(token, str)
    assert len(token) >= 16


def test_verify_and_consume_accepts_an_action_keyword_argument() -> None:
    resume_id = uuid4()
    with app.test_request_context():
        token = csrf.issue_token(resume_id, action="withdraw")
        assert csrf.verify_and_consume(resume_id, token, action="withdraw") is True


def test_default_action_is_reveal_explicit_and_implicit_calls_agree() -> None:
    """Calling with no `action` at all must be indistinguishable from calling
    with `action="reveal"` explicitly — this is what keeps every pre-existing
    call site (which never passes `action`) working unmodified."""
    resume_id = uuid4()
    with app.test_request_context():
        token = csrf.issue_token(resume_id)  # implicit default
        assert csrf.verify_and_consume(resume_id, token, action="reveal") is True


def test_reveal_and_withdraw_tokens_for_the_same_resume_are_independent_values() -> (
    None
):
    resume_id = uuid4()
    with app.test_request_context():
        reveal_token = csrf.issue_token(resume_id, action="reveal")
        withdraw_token = csrf.issue_token(resume_id, action="withdraw")
    assert reveal_token != withdraw_token


def test_minting_a_withdraw_token_does_not_invalidate_an_already_minted_reveal_token() -> (  # noqa: E501
    None
):
    """THE load-bearing regression test: minting the SECOND audited action's
    token for a résumé that already has a live reveal token must not disturb
    it — this is exactly the FU-1 shortlist-multi-card bug, reintroduced
    across actions instead of across résumé ids."""
    resume_id = uuid4()
    with app.test_request_context():
        reveal_token = csrf.issue_token(resume_id, action="reveal")
        csrf.issue_token(resume_id, action="withdraw")
        assert csrf.verify_and_consume(resume_id, reveal_token, action="reveal") is True


def test_minting_a_reveal_token_does_not_invalidate_an_already_minted_withdraw_token() -> (  # noqa: E501
    None
):
    resume_id = uuid4()
    with app.test_request_context():
        withdraw_token = csrf.issue_token(resume_id, action="withdraw")
        csrf.issue_token(resume_id, action="reveal")
        assert (
            csrf.verify_and_consume(resume_id, withdraw_token, action="withdraw")
            is True
        )


def test_a_token_minted_for_reveal_fails_verification_for_withdraw_same_resume() -> (
    None
):
    resume_id = uuid4()
    with app.test_request_context():
        reveal_token = csrf.issue_token(resume_id, action="reveal")
        assert (
            csrf.verify_and_consume(resume_id, reveal_token, action="withdraw") is False
        )


def test_a_token_minted_for_withdraw_fails_verification_for_reveal_same_resume() -> (
    None
):
    resume_id = uuid4()
    with app.test_request_context():
        withdraw_token = csrf.issue_token(resume_id, action="withdraw")
        assert (
            csrf.verify_and_consume(resume_id, withdraw_token, action="reveal") is False
        )


def test_a_misdirected_cross_action_verify_does_not_burn_the_rightful_actions_slot() -> (  # noqa: E501
    None
):
    """A wrong-action attempt (right résumé, right token, wrong action) must
    not consume the token's RIGHTFUL action slot — mirrors the existing
    cross-résumé non-consumption guarantee, one dimension over."""
    resume_id = uuid4()
    with app.test_request_context():
        reveal_token = csrf.issue_token(resume_id, action="reveal")
        misdirected = csrf.verify_and_consume(
            resume_id, reveal_token, action="withdraw"
        )
        assert misdirected is False
        # The reveal token is still valid afterwards, for its OWN action.
        assert csrf.verify_and_consume(resume_id, reveal_token, action="reveal") is True


def test_consuming_the_withdraw_token_does_not_consume_the_reveal_slot() -> None:
    resume_id = uuid4()
    with app.test_request_context():
        reveal_token = csrf.issue_token(resume_id, action="reveal")
        withdraw_token = csrf.issue_token(resume_id, action="withdraw")
        assert (
            csrf.verify_and_consume(resume_id, withdraw_token, action="withdraw")
            is True
        )
        # Reveal's slot for the SAME résumé is untouched by consuming withdraw's.
        assert csrf.verify_and_consume(resume_id, reveal_token, action="reveal") is True


def test_both_actions_on_one_resume_can_be_used_back_to_back_with_no_reissue() -> None:
    """Mirrors the FU-1 multi-card regression test above, one dimension over:
    both the reveal button and the withdraw button on ONE rendered résumé-
    detail page must be independently usable without an intervening reload."""
    resume_id = uuid4()
    with app.test_request_context():
        reveal_token = csrf.issue_token(resume_id, action="reveal")
        withdraw_token = csrf.issue_token(resume_id, action="withdraw")
        first = csrf.verify_and_consume(resume_id, reveal_token, action="reveal")
        second = csrf.verify_and_consume(resume_id, withdraw_token, action="withdraw")
    assert first is True
    assert second is True


def test_withdraw_action_is_one_shot_like_reveal() -> None:
    resume_id = uuid4()
    with app.test_request_context():
        token = csrf.issue_token(resume_id, action="withdraw")
        first = csrf.verify_and_consume(resume_id, token, action="withdraw")
        second = csrf.verify_and_consume(resume_id, token, action="withdraw")
    assert first is True
    assert second is False


def test_reissuing_the_withdraw_token_overwrites_only_its_own_action_slot() -> None:
    """Re-minting résumé A's withdraw token twice must overwrite ITS slot, not
    grow the mapping, and must leave résumé A's reveal slot untouched —
    mirrors the same-résumé re-issue guarantee, restricted to one action."""
    resume_id = uuid4()
    with app.test_request_context():
        reveal_token = csrf.issue_token(resume_id, action="reveal")
        csrf.issue_token(resume_id, action="withdraw")
        csrf.issue_token(resume_id, action="withdraw")
        mapping = flask.session[csrf.SESSION_KEY]
        assert len(mapping) == 2  # one reveal slot + one withdraw slot, no more
        assert csrf.verify_and_consume(resume_id, reveal_token, action="reveal") is True
