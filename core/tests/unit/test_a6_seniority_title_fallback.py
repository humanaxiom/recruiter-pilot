"""RED pin — ROADMAP A6 (4): the one real fix on this branch.

``orchestrator.py::_most_recent_title`` picks ``current[0]`` unconditionally,
so a résumé whose CURRENT role has a blank title returns ``None`` even when a
previous role reads "Senior Backend Engineer" — costing a real candidate the
full 15% seniority weight for a title that was, in fact, readable.

The fix: fall back to the most recent role that actually HAS a title,
instead of returning ``None`` outright. Precedence is preserved — current
role first, then document order — the fallback only widens WHICH role's
title is read, never re-orders the roles themselves.

This is deliberately the ONLY value change on the branch (see the spec's
scope decisions). It raises a candidate's own seniority sub-score in the
fallback case — though NOT monotonically once the whitespace gate below is
included, which deliberately LOWERS one case: a sole role titled ``"   "``
used to embed literal whitespace and score a garbage non-zero, and now
scores 0.0 marked unmeasured. An earlier draft of this docstring claimed
"never lower it"; that was wrong, and the reviewer caught it. All 20 corpus
fixtures have a titled current role and are provably unaffected either way.

REMEDIATION ROUND (reviewer CHANGES-REQUIRED, F5): the shipped gate is
``if title:`` — truthy, not "readable". A whitespace-only title
(``"   "``, ``"\\t\\n"``) is truthy in Python, so it blocks the fallback and
yields ``seniority_measured=True`` on a comparison against garbage: the
disclosure never fires for exactly the malformed-résumé case the ADR's
framing implies is covered. The tests below pin the corrected gate
(``if title and title.strip():``) and, separately, that a title which is
already non-blank is still returned UNSTRIPPED — stripping it would change
what gets embedded for every ordinary candidate, which is out of scope here.
"""

from __future__ import annotations

from src.pipeline.matching.orchestrator import _most_recent_title


def test_falls_back_to_titled_previous_role_when_current_title_blank() -> None:
    """THE pin. Blank current title, titled previous role -> the previous
    role's title, not None."""
    parsed = {
        "experience": [
            {"title": "", "is_current": True},
            {"title": "Senior Backend Engineer", "is_current": False},
        ]
    }
    assert _most_recent_title(parsed) == "Senior Backend Engineer", (
        "a blank CURRENT title must fall back to the previous role's title, "
        "not return None and cost the candidate the full seniority weight"
    )


def test_falls_back_when_the_current_roles_title_key_is_missing_entirely() -> None:
    parsed = {
        "experience": [
            {"is_current": True},  # no "title" key at all
            {"title": "Staff Engineer"},
        ]
    }
    assert _most_recent_title(parsed) == "Staff Engineer"


def test_still_returns_none_when_no_role_has_a_title_at_all() -> None:
    """The fallback widens WHICH role is read; it must not invent a title
    that does not exist anywhere in the résumé."""
    parsed = {
        "experience": [
            {"title": "", "is_current": True},
            {"title": None},
        ]
    }
    assert _most_recent_title(parsed) is None


def test_returns_none_for_no_experience_at_all() -> None:
    assert _most_recent_title({"experience": []}) is None
    assert _most_recent_title({}) is None


def test_current_role_precedence_is_unchanged_when_the_current_role_has_a_title() -> (
    None
):
    """The fallback must never override a perfectly good current-role title
    with an earlier one — current-first still wins."""
    parsed = {
        "experience": [
            {"title": "Backend Engineer", "is_current": False},
            {"title": "Staff Engineer", "is_current": True},
        ]
    }
    assert _most_recent_title(parsed) == "Staff Engineer"


def test_document_order_precedence_is_unchanged_when_the_first_role_has_a_title() -> (
    None
):
    """No ``is_current`` role at all — existing precedence already falls
    through to ``roles[0]``. When it has a title, the fallback must not
    disturb that."""
    parsed = {"experience": [{"title": "Engineer II"}, {"title": "Engineer I"}]}
    assert _most_recent_title(parsed) == "Engineer II"


def test_document_order_fallback_when_no_role_is_current_and_the_first_is_blank() -> (
    None
):
    """No ``is_current`` role, and ``roles[0]`` itself has a blank title —
    the fallback must still prefer DOCUMENT ORDER among the titled roles
    (the first titled role in list order), not an arbitrary titled role."""
    parsed = {
        "experience": [
            {"title": ""},
            {"title": "Backend Engineer"},
            {"title": "Staff Engineer"},
        ]
    }
    assert _most_recent_title(parsed) == "Backend Engineer", (
        "with no current role and a blank roles[0], the fallback must pick "
        "the first TITLED role in document order, not any titled role"
    )


# ── F5 (remediation) — whitespace-only is "unreadable", not "readable" ─────


def test_whitespace_only_current_title_falls_back_to_a_titled_previous_role() -> None:
    """``"   "`` is truthy in Python -- the shipped ``if title:`` gate treats
    it as readable and blocks the fallback. It must be treated the same as a
    blank title: fall through to the next titled role in precedence order."""
    parsed = {
        "experience": [
            {"title": "   ", "is_current": True},
            {"title": "Senior Backend Engineer", "is_current": False},
        ]
    }
    assert _most_recent_title(parsed) == "Senior Backend Engineer", (
        "a whitespace-only CURRENT title must fall back to the previous "
        "role's title, exactly like a blank one -- 'truthy' is not "
        "'readable'"
    )


def test_tab_and_newline_only_title_falls_back_to_a_titled_previous_role() -> None:
    """A different whitespace-only value (tabs/newlines rather than spaces)
    -- same requirement, guards against a fix that special-cases the literal
    space character instead of using ``str.strip()``."""
    parsed = {
        "experience": [
            {"title": "\t\n", "is_current": True},
            {"title": "Staff Engineer", "is_current": False},
        ]
    }
    assert _most_recent_title(parsed) == "Staff Engineer"


def test_whitespace_only_title_with_no_titled_role_anywhere_returns_none() -> None:
    """The fallback widens WHICH role is read; a whitespace-only title must
    not be treated as content just because there is nothing else to fall
    back to either."""
    parsed = {
        "experience": [
            {"title": "   ", "is_current": True},
            {"title": "\t"},
        ]
    }
    assert _most_recent_title(parsed) is None


def test_a_readable_title_is_returned_unstripped() -> None:
    """A title that is ALREADY non-blank must be returned byte-identical --
    not ``.strip()``-ed -- so the string fed to the embedder is unchanged for
    every candidate whose title was already readable. Only the READABILITY
    CHECK may use ``.strip()``; the return value must not."""
    parsed = {
        "experience": [
            {"title": "  Senior Backend Engineer  ", "is_current": True},
        ]
    }
    assert _most_recent_title(parsed) == "  Senior Backend Engineer  ", (
        "a non-blank title must be returned exactly as stored, surrounding "
        "whitespace and all -- stripping it would change what gets embedded"
    )
