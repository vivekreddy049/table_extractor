"""S7 value parsing and S9 signals."""

from __future__ import annotations

import pytest

from zextract.model import ValueType
from zextract.s7_normalise import parse_value


def t(raw):
    return parse_value(raw)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1,23,45,678", 12345678.0),   # Indian grouping
        ("12,345,678", 12345678.0),    # Western grouping
        ("4,812", 4812.0),
        ("872", 872.0),
        ("1,009.26", 1009.26),
    ],
)
def test_digit_grouping(raw, expected):
    assert t(raw).normalized_value == pytest.approx(expected)


def test_indian_and_western_are_distinguished_by_shape():
    assert t("1,23,456").rule.startswith("indian")
    assert t("123,456").rule.startswith("western")


@pytest.mark.parametrize(
    "raw",
    ["(1,234)", "-1,234", "1,234-", "−1,234"],
)
def test_all_four_negative_conventions(raw):
    p = t(raw)
    assert p.normalized_value == pytest.approx(-1234.0), p


def test_parenthesised_negative_keeps_raw_text_intact():
    p = t("(412)")
    assert p.normalized_value == -412.0
    # Normalisation is additive: nothing here rewrites the source string.
    assert t("(412)").value_type in (ValueType.INTEGER, ValueType.DECIMAL)


def test_currency_symbol_and_code():
    assert t("$ 294,866").unit == "USD"
    assert t("$ 294,866").value_type is ValueType.CURRENCY
    assert t("Rs. 143.00").unit == "INR"


def test_backtick_rupee_is_reported_as_ambiguous_not_guessed_silently():
    """Shell.pdf renders the rupee sign as a backtick (F9)."""
    p = t("` 1,009.26")
    assert p.unit == "INR"
    assert "UNIT_GLYPH_AMBIGUOUS" in p.notes


@pytest.mark.parametrize(
    "raw,scale,value",
    [
        ("143.00 Cr", "crore", 143.0 * 10**7),
        ("22 lakh", "lakh", 22 * 10**5),
        ("12.5mn", "million", 12.5 * 10**6),
        ("3 bn", "billion", 3 * 10**9),
    ],
)
def test_magnitude_suffixes(raw, scale, value):
    p = t(raw)
    assert p.scale == scale
    assert p.normalized_value == pytest.approx(value)


def test_percent():
    p = t("(8)%")
    assert p.value_type is ValueType.PERCENT
    assert p.normalized_value == pytest.approx(-8.0)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("", ValueType.EMPTY),
        ("-", ValueType.DASH),
        ("–", ValueType.DASH),
        ("Nil", ValueType.NIL),
        ("NA", ValueType.NA),
        ("N/A", ValueType.NA),
        ("0", ValueType.INTEGER),
        ("0.00", ValueType.DECIMAL),
    ],
)
def test_empty_dash_nil_na_and_zero_stay_distinct(raw, expected):
    """Five different meanings, five different types.

    Shell.pdf's balance sheet uses "-" for nil-this-period in columns that also
    contain 0.00 for a measured zero. Collapsing them to NULL destroys
    information no downstream sum can recover.
    """
    assert t(raw).value_type is expected


@pytest.mark.parametrize("raw", ["#REF!", "#DIV/0!", "#N/A", "#VALUE!"])
def test_spreadsheet_errors_are_a_value_type_not_a_parse_failure(raw):
    p = t(raw)
    if raw == "#N/A":
        # Ambiguous with the "not available" marker; either reading is honest.
        assert p.value_type in (ValueType.ERROR, ValueType.NA)
        return
    assert p.value_type is ValueType.ERROR
    assert p.normalized_value is None  # never coerced to 0
    assert "SOURCE_ERROR_VALUE" in p.notes


@pytest.mark.parametrize(
    "raw,iso",
    [
        ("2024-09-28", "2024-09-28"),
        ("28/09/2024", "2024-09-28"),
        ("28.09.2024", "2024-09-28"),
        ("28-Sep-2024", "2024-09-28"),
        ("September 28, 2024", "2024-09-28"),
    ],
)
def test_date_formats(raw, iso):
    p = t(raw)
    assert p.value_type is ValueType.DATE
    assert p.normalized_text == iso


def test_ambiguous_day_month_is_flagged_not_defaulted_to_a_locale():
    p = t("05/06/2024")
    assert p.value_type is ValueType.DATE
    assert "DATE_ORDER_AMBIGUOUS" in p.notes


def test_fiscal_and_quarter_forms():
    assert t("FY 2024-25").value_type is ValueType.DATE
    assert t("Q3 FY25").value_type is ValueType.DATE


def test_prose_containing_a_comma_is_text_not_an_ambiguous_number():
    """The regression: "September 28," and "shares authorized; 15,116,786" both
    contain a comma, match neither grouping convention, and were reported as
    ambiguous numbers -- flooding the review queue with prose."""
    for raw in ("September 28,", "shares authorized; 15,116,786", "Risk Factors."):
        p = t(raw)
        assert p.value_type is ValueType.TEXT
        assert "PARSE_AMBIGUOUS" not in p.notes


def test_genuinely_ambiguous_grouping_is_still_reported():
    p = t("1,23,45,6789")
    assert p.value_type is ValueType.TEXT
    assert "PARSE_AMBIGUOUS" in p.notes
