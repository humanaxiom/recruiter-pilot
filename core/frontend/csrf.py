"""Anti-forgery token for the Flask viewer's state-changing reveal form (FU-4/D4).

Classic CSRF does NOT apply to the FastAPI backend: it authenticates on the
``X-API-Key`` header and a cross-origin ``<form>`` cannot attach a custom
header, so a forged cross-site POST aimed straight at the backend is simply
rejected as unauthenticated. The real gap is the Flask hop — the browser
supplies no credential of its own for ``POST /resumes/<id>/reveal``; Flask
attaches its own server-held recruiter key on the OUTBOUND leg
(:func:`frontend.api_client.build_client`), so Flask itself cannot distinguish
a forged cross-site auto-submit from a genuine click. Left unguarded, a forged
POST would produce a real, attributable ``reveal_audit`` row.

The fix is a session-bound, ONE-SHOT token, scoped PER RÉSUMÉ ID. It carries no
identity — this is not a login — it only proves the submitting page was
rendered by us, for this browser session. It lives in Flask's EXISTING signed
session (``app.secret_key`` from ``settings.flask_secret_key``), so it cannot be
forged or read cross-origin.

Tokens are keyed by résumé id because the FU-1 reveal button appears on EVERY
shortlist card, all posting to the SAME ``resume_reveal`` route: with a single
per-session token, minting one card's token invalidated every other card's, so
only the first reveal a recruiter clicked worked. Per-résumé scoping also means
a token minted for résumé A can never validate a reveal of résumé B.

:func:`same_origin` is layered ON TOP of the token as defense-in-depth, never
instead of it: it blocks only when a cross-origin ``Origin``/``Referer`` is
actually present, and stays silent when neither header exists.
"""

from __future__ import annotations

import hashlib
import secrets
from typing import Any
from urllib.parse import urlsplit

import flask

#: Flask-session key holding the ``hashed resume_id -> token`` mapping.
SESSION_KEY = "_csrf_token"

#: Flask-session key holding the session-wide PAGE token (Phase 1.3). Distinct
#: from :data:`SESSION_KEY` so the two mechanisms can never be confused for one
#: another: a page token must never open a reveal, and a reveal's one-shot
#: token must never satisfy an ordinary form post.
PAGE_SESSION_KEY = "_csrf_page_token"

#: Form field name rendered as a hidden input and read from ``request.form``.
#: Shared by both mechanisms — a given route reads only one of them, so the
#: field name carries no ambiguity at any single call site.
FORM_FIELD = "csrf_token"

#: Request header carrying the page token for htmx-driven posts. Several write
#: controls are bare ``hx-post`` buttons with no surrounding ``<form>`` to hold
#: a hidden input, so a form-field-only reader would leave exactly those either
#: unprotected or broken. Set once as ``hx-headers`` on ``<body>`` and inherited
#: by every htmx request on the page, including ones inside swapped-in
#: partials (htmx resolves inherited attributes by walking up the live DOM).
HEADER_FIELD = "X-CSRF-Token"

#: Upper bound on how many per-résumé tokens one session may hold at once.
#: A shortlist render mints one token per card and is structurally capped at
#: the stage-1 ``k=50`` oversample (ADR-012 SEC-3), so 64 clears a full
#: shortlist page with headroom for a couple of other open résumé tabs while
#: still bounding the signed session cookie well inside the ~4KB ceiling.
MAX_TOKENS_PER_SESSION = 64

#: Entropy of a minted token, in bytes (URL-safe base64 expands this ~1.3x, so
#: 16 bytes -> a 22-char token). 128 bits is ample for a one-shot anti-forgery
#: value, and the byte count is load-bearing for the cookie budget below.
_TOKEN_BYTES = 16

#: Hex characters of the sha256 digest kept as the mapping key. 12 hex chars is
#: 48 bits; at :data:`MAX_TOKENS_PER_SESSION` concurrent entries the birthday
#: bound puts a collision at ~4.5e-13, an accepted residual risk (a collision
#: degrades exactly like re-issuing the same résumé's token: the later mint
#: overwrites the earlier slot).
_KEY_HEX_CHARS = 12


def _session_key_for(resume_id: Any, action: str = "reveal") -> str:
    """Derive the mapping key for ``(resume_id, action)``.

    Hashing keeps each entry ~12 bytes instead of a raw ~36-char UUID string:
    the signed session cookie must stay inside the ~4093-byte ceiling browsers
    silently enforce, and an oversized cookie is DROPPED rather than rejected —
    which would empty the session and 403 every reveal.

    FU-8/ADR-026: for the default ``action="reveal"`` the hashed input is
    UNCHANGED — plain ``str(resume_id)``, byte-for-byte identical to the
    pre-amendment key derivation — so every pre-existing caller (which never
    passes ``action``) derives the EXACT SAME key as before this amendment
    (existing sessions/tests keep working unmodified). A non-default action
    (e.g. "withdraw") folds the action into the hashed input instead, which
    mints into an INDEPENDENT slot for the SAME résumé id — minting one
    action's token never invalidates the other's.
    """
    seed = str(resume_id) if action == "reveal" else f"{action}:{resume_id}"
    return hashlib.sha256(seed.encode()).hexdigest()[:_KEY_HEX_CHARS]


def _mapping() -> dict[str, str]:
    """Return the session's ``hashed resume_id -> token`` mapping, coercing junk.

    A session written by the previous per-session design held a bare token
    string under ``SESSION_KEY``; anything that is not a ``str -> str`` mapping
    is discarded rather than raising, so an old cookie simply mints afresh.
    """
    stored = flask.session.get(SESSION_KEY)
    if not isinstance(stored, dict):
        return {}
    return {
        k: v for k, v in stored.items() if isinstance(k, str) and isinstance(v, str)
    }


def issue_token(resume_id: Any, *, action: str = "reveal") -> str:
    """Mint a fresh token for ``(resume_id, action)``, store it and return it.

    Overwrites any previously-issued, unconsumed token for the SAME résumé AND
    action, but leaves every other résumé/action slot untouched — the
    shortlist renders one reveal form per card, all posting to the same route,
    so each card needs its own independently-valid token; similarly, FU-8's
    withdraw button shares a résumé id with the reveal button but must mint
    into its OWN slot (``action="withdraw"``). When adding a NEW slot would
    push the mapping past :data:`MAX_TOKENS_PER_SESSION`, the oldest entry is
    evicted first (strict FIFO by issue order; re-issuing for an existing
    résumé/action keeps that slot's original position rather than promoting
    it). Must be called inside an active Flask request context.
    """
    key = _session_key_for(resume_id, action)
    mapping = _mapping()
    if key not in mapping:
        while len(mapping) >= MAX_TOKENS_PER_SESSION:
            oldest = next(iter(mapping))
            del mapping[oldest]
    token = secrets.token_urlsafe(_TOKEN_BYTES)
    mapping[key] = token
    # Reassign rather than mutate in place so Flask marks the session dirty.
    flask.session[SESSION_KEY] = mapping
    return token


def verify_and_consume(
    resume_id: Any, submitted: str | None, *, action: str = "reveal"
) -> bool:
    """Return ``True`` iff ``submitted`` matches ``(resume_id, action)``'s
    stored token.

    On a MATCH, ``(resume_id, action)``'s entry is popped (strict one-shot).

    On a MISMATCH, the target slot is popped UNCONDITIONALLY *unless*
    ``submitted`` is itself a still-live token currently stored under some
    OTHER ``(resume_id, action)`` slot in this SAME session (e.g. the FU-8
    withdraw button's token mistakenly posted to the reveal route, or vice
    versa — both rendered on the SAME résumé-detail page). A blind guess
    burns the slot regardless of match — this is the anti-probing guarantee
    (an attacker cannot replay guesses against a live slot) — but a genuine,
    currently-valid credential the caller already holds for a DIFFERENT
    action gains an attacker nothing by being rejected non-destructively, so
    it leaves every slot (including the misdirected target) untouched:
    résumé A's token posted at résumé B's route, or one action's token
    posted at a different action's route on the SAME résumé, both fail
    without burning the rightful slot. Never raises: a missing, empty or
    non-ASCII submission is simply ``False``.
    """
    key = _session_key_for(resume_id, action)
    mapping = _mapping()
    stored = mapping.get(key)
    if stored and submitted:
        # Compare UTF-8 bytes: `compare_digest` raises on non-ASCII `str`
        # inputs, and `submitted` is attacker-controlled form data.
        if secrets.compare_digest(stored.encode("utf-8"), submitted.encode("utf-8")):
            del mapping[key]
            flask.session[SESSION_KEY] = mapping
            return True
    if submitted and any(
        other_key != key and other_value == submitted
        for other_key, other_value in mapping.items()
    ):
        # `submitted` is a genuine, still-live token for a DIFFERENT slot —
        # a misdirected-but-honest submission, not a blind guess. Leave
        # every slot untouched.
        return False
    mapping.pop(key, None)
    flask.session[SESSION_KEY] = mapping
    return False


def issue_page_token() -> str:
    """Return this session's page token, minting one on first use (Phase 1.3).

    **Idempotent, and deliberately NOT one-shot** — the opposite of
    :func:`issue_token`/:func:`verify_and_consume` above, and the difference is
    load-bearing. This is the classic synchronizer-token pattern: one value per
    session, rendered into every form and every htmx request, valid for as long
    as the session is. A page token that burned on use would break the back
    button, a second tab, and every "fix the validation error and resubmit"
    flow — all of which post the same rendered form twice.

    The stronger one-shot, per-résumé, per-action tokens stay exactly where
    FU-4/D4 put them (reveal/withdraw/reinstate), where replay actually matters
    and where each control is rendered fresh per resource. Those routes are
    exempt from the request hook that consumes THIS token; see
    ``frontend.app._CSRF_HOOK_EXEMPT_ENDPOINTS``.

    Uses the same entropy as the one-shot tokens (:data:`_TOKEN_BYTES`, 128
    bits) and lives in the same Flask signed session, so it cannot be forged or
    read cross-origin. It is a single ~22-char string, so unlike the per-résumé
    mapping it poses no cookie-size question. Must be called inside an active
    Flask request context.
    """
    stored = flask.session.get(PAGE_SESSION_KEY)
    if isinstance(stored, str) and stored:
        return stored
    token = secrets.token_urlsafe(_TOKEN_BYTES)
    flask.session[PAGE_SESSION_KEY] = token
    return token


def verify_page_token(submitted: str | None) -> bool:
    """Return ``True`` iff ``submitted`` matches this session's page token.

    **Never consumes it** — see :func:`issue_page_token` for why. Returns
    ``False`` when no page token has been issued for this session at all: a
    request arriving before any page was rendered cannot have come from one of
    our forms, which is precisely the forged-request case.

    Never raises: a missing, empty or non-ASCII submission is simply ``False``
    (``compare_digest`` raises on non-ASCII ``str`` input, and ``submitted`` is
    attacker-controlled, so the comparison is made on UTF-8 bytes — the same
    care :func:`verify_and_consume` takes).
    """
    stored = flask.session.get(PAGE_SESSION_KEY)
    if not isinstance(stored, str) or not stored or not submitted:
        return False
    return secrets.compare_digest(stored.encode("utf-8"), submitted.encode("utf-8"))


def token_from_request(req: Any) -> str | None:
    """Read the submitted page token from a form field or the htmx header.

    Form field first (an ordinary ``<form>`` post is the common case), then
    :data:`HEADER_FIELD`. Both are accepted on every guarded route rather than
    per-route, so a control that changes between a plain form and an htmx post
    does not silently lose its protection in the process.
    """
    submitted = req.form.get(FORM_FIELD) or req.headers.get(HEADER_FIELD)
    # `req` is deliberately `Any` (a Flask request in production, a stub in
    # tests), so narrow explicitly rather than trusting the attribute's type.
    return submitted if isinstance(submitted, str) else None


def _origin_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def same_origin(req: Any) -> bool:
    """Defense-in-depth same-origin check, evaluated independently of the token.

    Prefers ``Origin`` over ``Referer`` when both are present (``Origin`` is the
    header browsers attach to cross-site form posts and cannot be spoofed by
    page content). Returns ``False`` only when a *cross*-origin header is
    present; an absent header is NOT a block — the token remains the primary
    control.
    """
    expected = _origin_of(req.host_url)
    declared = req.headers.get("Origin") or req.headers.get("Referer")
    if not declared:
        return True
    return _origin_of(declared) == expected


__all__ = [
    "SESSION_KEY",
    "PAGE_SESSION_KEY",
    "FORM_FIELD",
    "HEADER_FIELD",
    "MAX_TOKENS_PER_SESSION",
    "issue_token",
    "verify_and_consume",
    "issue_page_token",
    "verify_page_token",
    "token_from_request",
    "same_origin",
]
