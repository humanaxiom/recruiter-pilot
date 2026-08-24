"""Display-time redaction for blind (anonymized) review.

Pure, DB-free helpers (trivially unit-tested, reused across every read path).
We mask the candidate's own name and any email/phone patterns, the employer +
school names carried in the parsed résumé (mapping each to a stable label
"Employer A" / "Institution A" so the career narrative survives while identity
is hidden), and — when ``redact_locations`` is on — **foreign** locations (US
states / other countries). **Canadian** locations and employment **dates** are
deliberately left visible: the bias concern is foreign employment history (a
national-origin / discounted-international-experience proxy), not a Canadian
city. Blind review reduces, not guarantees, anonymity.

Ported near-verbatim from hris ``apps/api/src/api/services/redaction.py``
(ADR 0013 / 0026 / 0028 there; this project's ADR-006 §4 / ADR-010 §6 carry the
same redaction-boundary contract into Phase 5).
"""

from __future__ import annotations

import os
import re
import string

_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")

# Location scrubbing. The bias concern is FOREIGN employment history, NOT a
# Canadian city (which stays visible). So we redact a "City, Region" only when
# the region is a US state or a foreign country (never a Canadian province).
# Anchoring on a known foreign-region set (not "any capitalised word after a
# comma") keeps a comma-separated tools list or ordinary prose from tripping it.
_US_STATES: tuple[str, ...] = (
    "Alabama",
    "Alaska",
    "Arizona",
    "Arkansas",
    "California",
    "Colorado",
    "Connecticut",
    "Delaware",
    "Florida",
    "Georgia",
    "Hawaii",
    "Idaho",
    "Illinois",
    "Indiana",
    "Iowa",
    "Kansas",
    "Kentucky",
    "Louisiana",
    "Maine",
    "Maryland",
    "Massachusetts",
    "Michigan",
    "Minnesota",
    "Mississippi",
    "Missouri",
    "Montana",
    "Nebraska",
    "Nevada",
    "New Hampshire",
    "New Jersey",
    "New Mexico",
    "New York",
    "North Carolina",
    "North Dakota",
    "Ohio",
    "Oklahoma",
    "Oregon",
    "Pennsylvania",
    "Rhode Island",
    "South Carolina",
    "South Dakota",
    "Tennessee",
    "Texas",
    "Utah",
    "Vermont",
    "Virginia",
    "Washington",
    "West Virginia",
    "Wisconsin",
    "Wyoming",
    # Postal abbreviations, minus codes that collide with common résumé tokens:
    # OK/HI/IN/OR/ME/OH/LA/ID/DE (English words), MS (MS Project/Office),
    # MA (Master of Arts), MD (doctor), PA (personal assistant). Their full
    # state names still match.
    "CA",
    "NY",
    "TX",
    "WA",
    "FL",
    "IL",
    "GA",
    "NJ",
    "VA",
    "NC",
    "CO",
    "AZ",
    "MI",
    "TN",
    "MO",
    "WI",
    "MN",
    "SC",
    "NV",
    "CT",
    "KY",
    "AL",
    "AR",
    "KS",
    "NE",
    "NM",
    "NH",
    "ND",
    "SD",
    "RI",
    "UT",
    "VT",
    "WV",
    "WY",
    "AK",
    "DC",
    "MT",
)
_COUNTRIES: tuple[str, ...] = (
    "United States",
    "United Kingdom",
    "Northern Ireland",
    "United Arab Emirates",
    "South Korea",
    "South Africa",
    "Sri Lanka",
    "Hong Kong",
    "New Zealand",
    "Saudi Arabia",
    "Czech Republic",
    "Dominican Republic",
    "Trinidad and Tobago",
    "USA",
    "U.S.A.",
    "U.S.",
    "UK",
    "U.K.",
    "UAE",
    "America",
    "England",
    "Scotland",
    "Wales",
    "Ireland",
    "India",
    "China",
    "Taiwan",
    "Pakistan",
    "Bangladesh",
    "Nepal",
    "Philippines",
    "Vietnam",
    "Thailand",
    "Indonesia",
    "Malaysia",
    "Singapore",
    "Japan",
    "Korea",
    "Nigeria",
    "Kenya",
    "Ghana",
    "Ethiopia",
    "Egypt",
    "Morocco",
    "Qatar",
    "Kuwait",
    "Bahrain",
    "Oman",
    "Lebanon",
    "Jordan",
    "Israel",
    "Turkey",
    "Iran",
    "Iraq",
    "Syria",
    "Afghanistan",
    "Russia",
    "Ukraine",
    "Poland",
    "Germany",
    "France",
    "Spain",
    "Portugal",
    "Italy",
    "Netherlands",
    "Belgium",
    "Switzerland",
    "Austria",
    "Sweden",
    "Norway",
    "Denmark",
    "Finland",
    "Greece",
    "Romania",
    "Hungary",
    "Australia",
    "Brazil",
    "Mexico",
    "Argentina",
    "Chile",
    "Colombia",
    "Peru",
    "Venezuela",
    "Ecuador",
    "Cuba",
    "Jamaica",
    "Haiti",
)
_FOREIGN_REGIONS: tuple[str, ...] = _US_STATES + _COUNTRIES
# Longest alternatives first so "United Kingdom" beats a bare "UK"/"United".
_FOREIGN_ALT = "|".join(
    re.escape(r) for r in sorted(_FOREIGN_REGIONS, key=len, reverse=True)
)
# City token requires Capital + a lowercase letter (a real place name), so an
# all-caps token like "BA" (degree) can't pose as a city in "BA, MA". Region is
# matched in its canonical case (Title-case names, UPPER abbrevs).
_FOREIGN_LOCATION_RE = re.compile(
    r"\b[A-Z][a-z][\w.'\-]*(?:[ \-][A-Z][a-z][\w.'\-]*){0,3}\s*,\s*(?:"
    + _FOREIGN_ALT
    + r")\b"
)
# Bare foreign-region token (for classifying a structured location field).
_FOREIGN_TOKEN_RE = re.compile(r"\b(?:" + _FOREIGN_ALT + r")\b")
_LOCATION_MASK = "[location redacted]"


def is_foreign_location(location: str | None) -> bool:
    """True when a location names a non-Canadian region — a US state or a
    foreign country. Canadian and unrecognised locations return False (kept
    visible). Used to decide whether to mask a structured location field."""
    if not location or not location.strip():
        return False
    return _FOREIGN_TOKEN_RE.search(location) is not None


# Phone candidate: a run of digits + common separators. We only redact a
# candidate that contains >= 10 digits, so it catches real phone numbers
# without eating year ranges ("2002-2007", 8 digits) or single years.
_PHONE_CAND_RE = re.compile(r"(?<!\w)\+?\d[\d().\-\s]{6,}\d(?!\w)")
_MIN_PHONE_DIGITS = 10


def _redact_phones(text: str) -> str:
    def _repl(m: re.Match[str]) -> str:
        digits = sum(c.isdigit() for c in m.group(0))
        return "[contact redacted]" if digits >= _MIN_PHONE_DIGITS else m.group(0)

    return _PHONE_CAND_RE.sub(_repl, text)


def _name_pattern(name: str) -> re.Pattern[str] | None:
    """Whole-word, case-insensitive matcher for a person's name and each of its
    parts (so 'Jane Smith', 'Jane', and 'Smith' all redact)."""
    parts = [p for p in re.split(r"\s+", name.strip()) if len(p) >= 2]
    if not parts:
        return None
    # Longest first so the full name is consumed before its parts.
    parts.sort(key=len, reverse=True)
    alt = "|".join(re.escape(p) for p in [name.strip(), *parts] if p)
    # The alternation MUST be grouped: without ``(?:...)`` the leading
    # lookbehind binds only to the first alternative and the trailing lookahead
    # only to the last, so a middle part ("Smith") matches inside a longer word
    # ("Smithsonian"). Grouping makes the whole-word guards apply to every arm.
    return re.compile(rf"(?<![\w])(?:{alt})(?![\w])", re.IGNORECASE)


def _terms_pattern(terms: list[str]) -> re.Pattern[str] | None:
    """Whole-word, case-insensitive matcher for a set of known strings
    (employer / school names), longest first so a longer name is consumed
    before a shorter one it contains ("Acme Corp" before "Acme")."""
    clean = sorted({t.strip() for t in terms if t and t.strip()}, key=len, reverse=True)
    if not clean:
        return None
    alt = "|".join(re.escape(t) for t in clean)
    # Group the alternation so the whole-word lookbehind/lookahead apply to
    # every arm, not just the first/last (see ``_name_pattern``).
    return re.compile(rf"(?<![\w])(?:{alt})(?![\w])", re.IGNORECASE)


def redact_text(
    text: str,
    *,
    name: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    term_map: dict[str, str] | None = None,
    location: str | None = None,
    redact_locations: bool = False,
) -> str:
    """Return ``text`` with the candidate's name masked to ``[name redacted]``,
    email/phone-shaped substrings masked to ``[contact redacted]``, and any
    known ``term_map`` keys (employer / school names) replaced by their stable
    label ("Employer A"). Safe on empty/None inputs. ``email``/``phone`` args
    are accepted for symmetry but redaction is pattern-based (catches contact
    info even when it differs slightly).

    When ``redact_locations`` is True, also mask **foreign** locations to
    ``[location redacted]``: any "City, <US state / foreign country>" string,
    plus the candidate's own ``location`` when it is itself foreign. Canadian
    locations are left visible. ``redact_locations`` is keyword-only and
    defaults False — blind call sites opt in explicitly."""
    if not text:
        return text
    out = text
    if name and name.strip():
        pat = _name_pattern(name)
        if pat is not None:
            out = pat.sub("[name redacted]", out)
    if term_map:
        pat = _terms_pattern(list(term_map.keys()))
        if pat is not None:
            # Case-insensitive lookup: the pattern preserves source casing, so
            # resolve the label by the matched text's casefolded form.
            lower_map = {k.casefold(): v for k, v in term_map.items()}
            out = pat.sub(
                lambda m: lower_map.get(m.group(0).casefold(), m.group(0)), out
            )
    if redact_locations:
        # The candidate's own city first, but only when it is itself FOREIGN;
        # Canadian home cities stay. Then foreign "City, Region" mentions
        # anywhere in the prose.
        if location and is_foreign_location(location):
            loc_pat = _terms_pattern([location])
            if loc_pat is not None:
                out = loc_pat.sub(_LOCATION_MASK, out)
        out = _FOREIGN_LOCATION_RE.sub(_LOCATION_MASK, out)
    out = _EMAIL_RE.sub("[contact redacted]", out)
    return _redact_phones(out)


def blind_label_map(employers: list[str], institutions: list[str]) -> dict[str, str]:
    """Build the stable label map for a résumé's employers + institutions: each
    *distinct* name → "Employer A/B…" / "Institution A/B…", deduped
    case-insensitively and ordered by first appearance (so a repeated employer
    keeps one label). Keys are the original strings (for exact-text scrubbing)."""

    def _labels(names: list[str], noun: str) -> dict[str, str]:
        out: dict[str, str] = {}
        seen: dict[str, str] = {}  # casefolded name -> label
        for raw in names:
            key = (raw or "").strip()
            if not key:
                continue
            fold = key.casefold()
            if fold not in seen:
                seen[fold] = f"{noun} {_letter(len(seen) + 1)}"
            out[key] = seen[fold]
        return out

    return {
        **_labels(employers, "Employer"),
        **_labels(institutions, "Institution"),
    }


def _letter(n: int) -> str:
    """1→'A', 26→'Z', 27→'AA' (shared with :func:`pseudonym`'s scheme)."""
    letters = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letters = string.ascii_uppercase[rem] + letters
    return letters


def redacted_filename(original: str | None) -> str:
    """Blind-review placeholder for a résumé's ``original_filename``. Résumés
    are commonly named after their candidate ("Jane_Smith_Resume.pdf"), so
    returning the real filename through a blind surface leaks identity — the
    same privacy concern that drives name/PII masking. Return a non-identifying
    ``resume<ext>`` that PRESERVES the extension (so ``.pdf``/``.docx`` stays
    visible for UX), lower-cased. No extension (or None/blank) → a bare
    ``resume``. The placeholder carries NO rank/pseudonym or candidate data."""
    if not original or not original.strip():
        return "resume"
    ext = os.path.splitext(original.strip())[1]
    return f"resume{ext.lower()}" if ext else "resume"


def pseudonym(rank: int) -> str:
    """Stable per-job label from 1-based rank: 1→'Candidate A', 26→'Candidate
    Z', 27→'Candidate AA'. Deterministic, so it survives shortlist
    regeneration (rank is recomputed identically)."""
    if rank < 1:
        return "Candidate ?"
    return f"Candidate {_letter(rank)}"
