"""Unit tests for ``src.services.redaction`` — display-time blind-review
masking (Phase 5).

Pure, DB-free string functions ported from hris
``apps/api/src/api/services/redaction.py`` (hris ADR 0013/0026/0028; this
project's ADR-006 §4 / ADR-010 §6 carry the same redaction-boundary contract
into Phase 5). No I/O to mock — every function here is a pure transform on
strings/dicts.

``src.services.redaction`` does not exist yet — RED half of the TDD cycle;
this whole file is expected to fail at collection (``ModuleNotFoundError``).

Locked decision pinned here explicitly (human, this session): ``redact_text``'s
``redact_locations`` parameter is KEYWORD-ONLY and defaults to ``False``. Blind
call sites (in ``shortlist_service`` / ``resume_service``, tested in their own
files) pass ``redact_locations=True`` explicitly — this file only pins the
function's own default + behaviour, not who calls it how.
"""

from __future__ import annotations

import inspect

import pytest

from src.services.redaction import (
    blind_label_map,
    is_foreign_location,
    pseudonym,
    redact_text,
    redacted_filename,
)

# ---------------- redact_text: name masking ----------------


def test_redact_text_masks_full_name_and_each_part_whole_word_only() -> None:
    text = "Jane Smith led the migration. Jane approved it. Smithsonian unaffected."
    out = redact_text(text, name="Jane Smith")
    assert "Jane Smith" not in out
    assert "[name redacted]" in out
    # whole-word only: "Smithsonian" must NOT be mangled by the "Smith" part
    assert "Smithsonian" in out


def test_redact_text_masks_each_name_part_independently() -> None:
    out = redact_text("Jane led it. Smith approved it too.", name="Jane Smith")
    assert "Jane" not in out
    assert "Smith" not in out


def test_redact_text_name_none_or_blank_is_a_no_op_for_names() -> None:
    text = "Jane Smith did the thing."
    assert redact_text(text, name=None) == text
    assert redact_text(text, name="") == text
    assert redact_text(text, name="   ") == text


def test_redact_text_empty_or_none_input_is_safe() -> None:
    assert redact_text("", name="Jane Smith") == ""
    assert redact_text(None, name="Jane Smith") is None  # type: ignore[arg-type]


# ---------------- redact_text: contact info ----------------


def test_redact_text_masks_email_shaped_substring() -> None:
    out = redact_text("Reach me at jane.doe@example.test please.", name=None)
    assert "jane.doe@example.test" not in out
    assert "[contact redacted]" in out


def test_redact_text_masks_phone_shaped_substring() -> None:
    out = redact_text("Call me at 604-555-0192 anytime.", name=None)
    assert "604-555-0192" not in out
    assert "[contact redacted]" in out


def test_redact_text_does_not_eat_a_short_year_range() -> None:
    """A year range like '2019-2022' (8 digits) must NOT be treated as a
    phone number — only >=10-digit runs are contact-shaped."""
    out = redact_text("Worked there 2019-2022 leading the team.", name=None)
    assert "2019-2022" in out


def test_redact_text_email_and_phone_safe_on_empty_or_none() -> None:
    assert redact_text("", email="a@example.test", phone="6045550192") == ""
    assert redact_text(None, email="a@example.test") is None  # type: ignore[arg-type]


# ---------------- redact_text: term_map (employer/institution labels) ------


def test_redact_text_term_map_replaces_names_case_insensitively() -> None:
    out = redact_text(
        "Worked at ACME CORP before joining acme corp again.",
        term_map={"Acme Corp": "Employer A"},
    )
    assert out.casefold().count("acme corp") == 0
    assert out.count("Employer A") == 2


def test_redact_text_term_map_longest_match_first() -> None:
    """'Acme Corp International' must be consumed whole, not left with a
    dangling 'International' after a shorter 'Acme Corp' match eats part of
    it first."""
    out = redact_text(
        "I worked at Acme Corp International for five years.",
        term_map={"Acme Corp": "Employer A", "Acme Corp International": "Employer B"},
    )
    assert "Employer B" in out
    assert "International" not in out
    assert "Employer A" not in out


def test_redact_text_term_map_none_or_empty_is_a_no_op() -> None:
    text = "Worked at Acme Corp for years."
    assert redact_text(text, term_map=None) == text
    assert redact_text(text, term_map={}) == text


# ---------------- pseudonym ----------------


@pytest.mark.parametrize(
    ("rank", "expected"),
    [(1, "Candidate A"), (2, "Candidate B"), (26, "Candidate Z"), (27, "Candidate AA")],
)
def test_pseudonym_rank_to_label(rank: int, expected: str) -> None:
    assert pseudonym(rank) == expected


@pytest.mark.parametrize("rank", [0, -1, -100])
def test_pseudonym_rank_below_one_returns_placeholder(rank: int) -> None:
    assert pseudonym(rank) == "Candidate ?"


def test_pseudonym_is_deterministic_for_the_same_rank() -> None:
    assert pseudonym(5) == pseudonym(5)


def test_pseudonym_survives_regeneration_same_rank_same_label() -> None:
    """Deterministic per-rank label — a shortlist regeneration that recomputes
    identical ranks must yield identical pseudonyms (no randomness/state)."""
    labels_run1 = [pseudonym(i) for i in range(1, 30)]
    labels_run2 = [pseudonym(i) for i in range(1, 30)]
    assert labels_run1 == labels_run2


# ---------------- blind_label_map ----------------


def test_blind_label_map_dedupes_case_insensitively() -> None:
    labels = blind_label_map(["Acme Corp", "ACME CORP", "acme corp"], [])
    assert len({v for v in labels.values()}) == 1


def test_blind_label_map_orders_by_first_appearance() -> None:
    labels = blind_label_map(["Beta Inc", "Alpha LLC"], [])
    assert labels["Beta Inc"] == "Employer A"
    assert labels["Alpha LLC"] == "Employer B"


def test_blind_label_map_repeated_employer_keeps_one_label() -> None:
    labels = blind_label_map(["Acme Corp", "Beta Inc", "Acme Corp"], [])
    assert labels["Acme Corp"] == "Employer A"
    assert labels["Beta Inc"] == "Employer B"
    # only two DISTINCT label values even though 3 keys were passed in
    assert len(set(labels.values())) == 2


def test_blind_label_map_labels_institutions_separately_from_employers() -> None:
    labels = blind_label_map(["Acme Corp"], ["State University"])
    assert labels["Acme Corp"] == "Employer A"
    assert labels["State University"] == "Institution A"


def test_blind_label_map_empty_inputs_yield_empty_map() -> None:
    assert blind_label_map([], []) == {}


def test_blind_label_map_skips_blank_entries() -> None:
    labels = blind_label_map(["", "  ", "Acme Corp"], [])
    assert labels == {"Acme Corp": "Employer A"}


# ---------------- redacted_filename (blind de-anonymization leak) ----------


def test_redacted_filename_masks_identity_preserving_extension() -> None:
    """Résumés are commonly named ``Jane_Smith_Resume.pdf`` — under blind
    review the real filename leaks identity. Mask to a non-identifying
    ``resume<ext>`` that keeps the extension visible for UX."""
    assert redacted_filename("Jane_Smith_Resume.pdf") == "resume.pdf"


def test_redacted_filename_lowercases_extension() -> None:
    assert redacted_filename("CV.DOCX") == "resume.docx"


def test_redacted_filename_no_extension_returns_bare_resume() -> None:
    assert redacted_filename("JaneSmithResume") == "resume"


@pytest.mark.parametrize("value", [None, "", "   "])
def test_redacted_filename_none_or_blank_is_safe_default(value: str | None) -> None:
    assert redacted_filename(value) == "resume"


def test_redacted_filename_carries_no_candidate_tokens() -> None:
    out = redacted_filename("Zzyzxqrst_Wibblesworth_CV.pdf")
    assert "Zzyzxqrst" not in out
    assert "Wibblesworth" not in out
    assert out == "resume.pdf"


# ---------------- is_foreign_location / redact_locations -------------------


@pytest.mark.parametrize(
    "location",
    ["San Francisco, California", "New York, NY", "London, United Kingdom"],
)
def test_is_foreign_location_true_for_us_states_and_countries(location: str) -> None:
    assert is_foreign_location(location) is True


@pytest.mark.parametrize(
    "location", ["Toronto, Ontario", "Vancouver, British Columbia", None, ""]
)
def test_is_foreign_location_false_for_canadian_or_empty(location: str | None) -> None:
    assert is_foreign_location(location) is False


def test_redact_text_locations_masked_only_when_redact_locations_true() -> None:
    text = "Previously based in Austin, Texas before relocating."
    default_out = redact_text(text, redact_locations=False)
    assert "Austin, Texas" in default_out, (
        "redact_locations defaults False — a foreign location must stay "
        "visible unless explicitly requested"
    )
    masked_out = redact_text(text, redact_locations=True)
    assert "Austin, Texas" not in masked_out
    assert "[location redacted]" in masked_out


def test_canadian_location_visible_with_redact_locations_true() -> None:
    text = "Previously based in Toronto, Ontario before relocating."
    out = redact_text(text, redact_locations=True)
    assert "Toronto, Ontario" in out


def test_redact_text_default_call_leaves_all_locations_visible() -> None:
    """The DEFAULT (no redact_locations kwarg at all) must behave identically
    to the explicit False — locations of every kind stay visible."""
    text = "Based in Austin, Texas, previously Toronto, Ontario."
    out = redact_text(text, name="Someone Else")
    assert "Austin, Texas" in out
    assert "Toronto, Ontario" in out


def test_redact_text_redact_locations_is_keyword_only_and_defaults_false() -> None:
    """Locked decision (human, this session): keyword-only, default False."""
    sig = inspect.signature(redact_text)
    param = sig.parameters["redact_locations"]
    assert param.default is False
    assert param.kind == inspect.Parameter.KEYWORD_ONLY
