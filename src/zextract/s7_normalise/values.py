"""S7 -- value normalisation and per-column type inference.

Additive, never destructive: ``raw_text`` is captured at S5 and never touched.
Everything here writes NEW fields alongside it, so no later stage and no reviewer
is ever looking at a value we have silently rewritten.

The two rules that matter most:

  * The five-way distinction between empty, ``-``, ``Nil``, ``NA`` and ``0`` is
    PRESERVED. Shell.pdf's balance sheet uses ``-`` for "nil this period" in
    columns where ``0.00`` also appears for a measured zero. Collapsing them to
    NULL destroys information no downstream sum can recover.
  * Excel error literals are a VALUE TYPE, not a parse failure. Shell.pdf p3
    contains live ``#REF!`` cells printed into the PDF (ARCHITECTURE.md F5).
    Coercing them to zero corrupts every total below; coercing them to null
    hides that the source document is broken. We ship the error and flag it.

Ambiguity is reported, never resolved by preference: ``1,23,456`` is
unambiguously Indian grouping, ``12,345`` unambiguously Western, and anything
that is neither stays ``text`` with a note.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Sequence

from ..model import ValueType

# Excel/Sheets error literals, printed into the PDF as text.
_ERRORS = frozenset(
    {"#REF!", "#DIV/0!", "#N/A", "#VALUE!", "#NAME?", "#NUM!", "#NULL!", "#ERROR!"}
)
_NIL = frozenset({"nil", "nill"})
_NA = frozenset({"na", "n/a", "n.a.", "n.a", "not applicable", "not available"})
# Dash-only cells. Every one of these is a distinct Unicode character that a
# producer may emit for "nothing here"; they are one semantic class, and a
# different class from an empty cell.
_DASHES = frozenset({"-", "‐", "‑", "‒", "–", "—", "−"})

_CURRENCY_SYMBOLS = {
    "$": "USD",
    "₹": "INR",
    "€": "EUR",
    "£": "GBP",
    "¥": "JPY",
    # Shell.pdf renders the rupee sign as a backtick, because the glyph is
    # outside the font's declared encoding (ARCHITECTURE.md F9). Mapping it is
    # not a guess about THIS document -- a bare backtick is never a legitimate
    # cell value -- but it is recorded as a low-confidence unit either way.
    "`": "INR?",
}
_CURRENCY_WORDS = {
    "rs": "INR",
    "rs.": "INR",
    "inr": "INR",
    "usd": "USD",
    "us$": "USD",
    "eur": "EUR",
    "gbp": "GBP",
}
# Magnitude suffixes, with the factor each implies.
_SCALES = {
    "k": ("thousand", Decimal(10) ** 3),
    "thousand": ("thousand", Decimal(10) ** 3),
    "mn": ("million", Decimal(10) ** 6),
    "m": ("million", Decimal(10) ** 6),
    "million": ("million", Decimal(10) ** 6),
    "bn": ("billion", Decimal(10) ** 9),
    "b": ("billion", Decimal(10) ** 9),
    "billion": ("billion", Decimal(10) ** 9),
    "lakh": ("lakh", Decimal(10) ** 5),
    "lac": ("lakh", Decimal(10) ** 5),
    "lakhs": ("lakh", Decimal(10) ** 5),
    "cr": ("crore", Decimal(10) ** 7),
    "crore": ("crore", Decimal(10) ** 7),
    "crores": ("crore", Decimal(10) ** 7),
}

# Grouping patterns. The Indian pattern requires at least one 2-digit group
# before the final 3-digit group, which is exactly what distinguishes it.
_WESTERN = re.compile(r"^\d{1,3}(,\d{3})+$")
_INDIAN = re.compile(r"^\d{1,2}(,\d{2})+,\d{3}$")
_PLAIN = re.compile(r"^\d+$")

_MONTHS = {
    m: i + 1
    for i, m in enumerate(
        "jan feb mar apr may jun jul aug sep oct nov dec".split()
    )
}

_DATE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$"), "iso"),
    (re.compile(r"^(\d{1,2})[/](\d{1,2})[/](\d{4})$"), "slash"),
    (re.compile(r"^(\d{1,2})[.](\d{1,2})[.](\d{4})$"), "dot"),
    (re.compile(r"^(\d{1,2})[-\s]([A-Za-z]{3,9})[-\s,]*(\d{4})$"), "d-mon-y"),
    (re.compile(r"^([A-Za-z]{3,9})\s+(\d{1,2}),?\s+(\d{4})$"), "mon-d-y"),
]
_FISCAL = re.compile(r"^(FY)\s*(\d{2,4})(\s*[-/]\s*(\d{2,4}))?$", re.I)
_QUARTER = re.compile(r"^Q([1-4])\s*(FY)?\s*(\d{2,4})$", re.I)


@dataclass(frozen=True)
class ParsedValue:
    value_type: ValueType
    normalized_value: float | None = None
    normalized_text: str | None = None
    unit: str | None = None
    scale: str | None = None
    rule: str = ""
    notes: tuple[str, ...] = ()


def _clean(raw: str) -> str:
    s = unicodedata.normalize("NFKC", raw)
    s = s.replace(" ", " ").replace(" ", " ").replace(" ", " ")
    return " ".join(s.split()).strip()


def _strip_sign(s: str) -> tuple[str, bool, str]:
    """Return (body, negative, rule). Handles all four negative conventions."""
    if s.startswith("(") and s.endswith(")"):
        return s[1:-1].strip(), True, "paren"
    if s.startswith("-") or s.startswith("−"):
        return s[1:].strip(), True, "leading-sign"
    if s.endswith("-") or s.endswith("−"):
        # Trailing sign: standard in SAP and Tally exports.
        return s[:-1].strip(), True, "trailing-sign"
    return s, False, "unsigned"


def _strip_currency(s: str) -> tuple[str, str | None]:
    for sym, code in _CURRENCY_SYMBOLS.items():
        if s.startswith(sym):
            return s[len(sym) :].strip(), code
        if s.endswith(sym):
            return s[: -len(sym)].strip(), code
    head = s.split(" ", 1)
    if len(head) == 2 and head[0].lower() in _CURRENCY_WORDS:
        return head[1].strip(), _CURRENCY_WORDS[head[0].lower()]
    return s, None


def _strip_scale(s: str) -> tuple[str, str | None, Decimal]:
    parts = s.rsplit(" ", 1)
    if len(parts) == 2 and parts[1].lower() in _SCALES:
        name, factor = _SCALES[parts[1].lower()]
        return parts[0].strip(), name, factor
    # Suffix with no space: "12.5mn".
    m = re.match(r"^(.*?)([A-Za-z]{1,7})$", s)
    if m and m.group(2).lower() in _SCALES and re.search(r"\d$", m.group(1)):
        name, factor = _SCALES[m.group(2).lower()]
        return m.group(1).strip(), name, factor
    return s, None, Decimal(1)


# A string is a NUMBER CANDIDATE only if it is made of digits, separators and
# nothing else. Without this test, "September 28," and "shares authorized;
# 15,116,786" both contain a comma, fail both grouping patterns, and get
# reported as ambiguous numbers -- flooding the review queue with prose. They
# are not ambiguous numbers; they are text, and text is not a parse failure.
_NUMBER_CANDIDATE = re.compile(r"^[\d,.]+$")


def _parse_number(body: str) -> tuple[Decimal | None, str, tuple[str, ...]]:
    """Parse a grouped number, returning (value, grouping rule, notes)."""
    if not body:
        return None, "", ()
    if not _NUMBER_CANDIDATE.match(body):
        return None, "", ()
    intpart, _, frac = body.partition(".")
    if frac and not frac.isdigit():
        return None, "", ("PARSE_AMBIGUOUS",)  # e.g. "1,234.56.78"

    if _PLAIN.match(intpart):
        rule = "plain"
    elif _INDIAN.match(intpart):
        rule = "indian-grouping"
    elif _WESTERN.match(intpart):
        rule = "western-grouping"
    elif "," in intpart:
        # Comma-separated but matching neither convention: do not pick one.
        return None, "", ("PARSE_AMBIGUOUS",)
    else:
        return None, "", ()

    digits = intpart.replace(",", "")
    try:
        return Decimal(digits + ("." + frac if frac else "")), rule, ()
    except InvalidOperation:
        return None, "", ("PARSE_AMBIGUOUS",)


def _parse_date(s: str) -> ParsedValue | None:
    for pat, name in _DATE_PATTERNS:
        m = pat.match(s)
        if not m:
            continue
        notes: tuple[str, ...] = ()
        if name == "iso":
            y, mo, d = int(m[1]), int(m[2]), int(m[3])
        elif name in ("slash", "dot"):
            a, b, y = int(m[1]), int(m[2]), int(m[3])
            if a > 12 and b <= 12:
                d, mo = a, b
            elif b > 12 and a <= 12:
                mo, d = a, b
            else:
                # Both readings are possible. The brief's day/month ambiguity:
                # resolved per column by S7's caller where an unambiguous
                # sibling exists, otherwise flagged rather than defaulted.
                d, mo = a, b
                notes = ("DATE_ORDER_AMBIGUOUS",)
        elif name == "d-mon-y":
            d = int(m[1])
            mo = _MONTHS.get(m[2][:3].lower(), 0)
            y = int(m[3])
        else:
            mo = _MONTHS.get(m[1][:3].lower(), 0)
            d, y = int(m[2]), int(m[3])
        if not (1 <= mo <= 12 and 1 <= d <= 31):
            continue
        return ParsedValue(
            value_type=ValueType.DATE,
            normalized_text=f"{y:04d}-{mo:02d}-{d:02d}",
            rule=f"date:{name}",
            notes=notes,
        )

    m = _FISCAL.match(s)
    if m:
        return ParsedValue(
            value_type=ValueType.DATE, normalized_text=s.upper(), rule="date:fiscal"
        )
    m = _QUARTER.match(s)
    if m:
        return ParsedValue(
            value_type=ValueType.DATE, normalized_text=s.upper(), rule="date:quarter"
        )
    return None


def parse_value(raw: str) -> ParsedValue:
    """Normalise one cell. Never raises; unparseable input becomes ``text``."""
    s = _clean(raw)

    if not s:
        return ParsedValue(ValueType.EMPTY, rule="empty")
    if s.upper() in _ERRORS:
        # A live spreadsheet error printed into the document. Kept verbatim.
        return ParsedValue(
            ValueType.ERROR,
            normalized_text=s.upper(),
            rule="source-error",
            notes=("SOURCE_ERROR_VALUE",),
        )
    if s in _DASHES:
        return ParsedValue(ValueType.DASH, rule="dash")
    low = s.lower().rstrip(".")
    if low in _NIL:
        return ParsedValue(ValueType.NIL, rule="nil")
    if s.lower() in _NA or low in _NA:
        return ParsedValue(ValueType.NA, rule="na")

    date = _parse_date(s)
    if date is not None:
        return date

    body, negative, sign_rule = _strip_sign(s)
    body, currency = _strip_currency(body)
    # A currency symbol may sit inside the parentheses, or the sign outside it.
    if not negative:
        body, negative, sign_rule = _strip_sign(body)

    percent = False
    if body.endswith("%"):
        percent = True
        body = body[:-1].strip()
        if not negative:
            # "(8)%" -- the parentheses wrap the number, the % sits outside
            # them, so the sign is only visible once the % has been removed.
            body, negative, sign_rule = _strip_sign(body)

    body, scale_name, factor = _strip_scale(body)
    body, currency2 = _strip_currency(body)
    currency = currency or currency2

    number, grouping, notes = _parse_number(body)
    if number is None:
        return ParsedValue(
            ValueType.TEXT, normalized_text=s, rule="text", notes=notes
        )

    value = -number if negative else number
    value = value * factor

    if percent:
        vtype = ValueType.PERCENT
    elif currency:
        vtype = ValueType.CURRENCY
    elif value == value.to_integral_value() and "." not in body:
        vtype = ValueType.INTEGER
    else:
        vtype = ValueType.DECIMAL

    extra: tuple[str, ...] = notes
    if currency == "INR?":
        # The glyph was a backtick, not a rupee sign. Report the doubt.
        extra = extra + ("UNIT_GLYPH_AMBIGUOUS",)
        currency = "INR"

    return ParsedValue(
        value_type=vtype,
        normalized_value=float(value),
        unit=currency,
        scale=scale_name,
        rule=f"{grouping}/{sign_rule}",
        notes=extra,
    )


# Types that carry a number a column can be summed over.
NUMERIC_TYPES = frozenset(
    {ValueType.INTEGER, ValueType.DECIMAL, ValueType.CURRENCY, ValueType.PERCENT}
)
# Types that mean "no value here" rather than "a value we failed to read".
BLANK_TYPES = frozenset(
    {ValueType.EMPTY, ValueType.DASH, ValueType.NIL, ValueType.NA}
)


def infer_column_type(
    parsed: Sequence[ParsedValue], min_agreement: float
) -> tuple[ValueType, str | None, float]:
    """Vote a column's type over its cells.

    The minority is NOT coerced. One string in a numeric column is the red flag
    the brief asks for, so it is left alone and surfaced as a conformity signal
    by S9 instead of being quietly rewritten.

    Returns (type, unit, agreement share).
    """
    considered = [p for p in parsed if p.value_type not in BLANK_TYPES]
    if not considered:
        return ValueType.EMPTY, None, 1.0

    counts: dict[ValueType, int] = {}
    for p in considered:
        counts[p.value_type] = counts.get(p.value_type, 0) + 1

    # Numeric subtypes vote together before competing with text: a column of
    # integers with one decimal in it is a numeric column, not a mixed one.
    numeric = sum(v for k, v in counts.items() if k in NUMERIC_TYPES)
    if numeric and numeric >= min_agreement * len(considered):
        best = max(
            ((k, v) for k, v in counts.items() if k in NUMERIC_TYPES),
            key=lambda kv: (kv[1], kv[0].value),
        )[0]
        units = [p.unit for p in considered if p.unit]
        unit = max(set(units), key=units.count) if units else None
        return best, unit, numeric / len(considered)

    best_type, best_count = max(counts.items(), key=lambda kv: (kv[1], kv[0].value))
    share = best_count / len(considered)
    if share < min_agreement:
        return ValueType.TEXT, None, share
    return best_type, None, share
