"""Synthetic PDF fixtures.

Fixtures are BUILT, not committed as binaries, for two reasons: a checked-in PDF
is opaque to review, and a generated one lets a test state its own ground truth
("this table has 4 rows at 14pt pitch") next to the assertion that checks it.
"""

from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest

from zextract.config import Config


@pytest.fixture(scope="session")
def cfg() -> Config:
    return Config.load()


def _page(doc: pymupdf.Document, width: float = 612.0, height: float = 792.0):
    return doc.new_page(width=width, height=height)


def make_pdf(path: Path, build) -> Path:
    doc = pymupdf.open()
    build(doc)
    doc.save(str(path), deflate=True)
    doc.close()
    return path


# Ground truth shared by the tests that use the ruled fixture.
RULED_PITCH = 14.0
RULED_FONT = 9.0
RULED_ROWS = [
    ("Particulars", "FY25", "FY24"),
    ("Revenue", "4,812", "4,109"),
    ("Expenses", "3,940", "3,551"),
    ("PBT", "872", "558"),
]
RULED_COL_X = (72.0, 300.0, 420.0)
RULED_TOP = 100.0


@pytest.fixture(scope="session")
def ruled_pdf(tmp_path_factory) -> Path:
    """A 4x3 table with a horizontal rule under the header, and a prose block.

    Deliberately mirrors the regime the samples are actually in: horizontal
    rules only, no vertical rules (ARCHITECTURE.md F1).
    """
    out = tmp_path_factory.mktemp("fixtures") / "ruled.pdf"

    def build(doc: pymupdf.Document) -> None:
        page = _page(doc)
        for r, row in enumerate(RULED_ROWS):
            y = RULED_TOP + r * RULED_PITCH
            for c, cell in enumerate(row):
                page.insert_text((RULED_COL_X[c], y), cell, fontsize=RULED_FONT, fontname="helv")
        rule_y = RULED_TOP + 0.5 * RULED_PITCH
        page.draw_line(
            pymupdf.Point(72.0, rule_y), pymupdf.Point(500.0, rule_y), width=0.6
        )
        # A prose block far below, to give the page a second block and a
        # realistic word-gap distribution.
        for i in range(6):
            page.insert_text(
                (72.0, 400.0 + i * RULED_PITCH),
                "The company reported higher revenue across every reporting segment.",
                fontsize=RULED_FONT,
                fontname="helv",
            )

    return make_pdf(out, build)


# The borderless fixture needs enough tokens to clear docstrum.min_tokens --
# a table too sparse to calibrate cannot demonstrate gap splitting, because
# without a pitch there is no scale-free gap threshold to exceed. Eight rows of
# four cells gives 32 tokens against a floor of 24.
BORDERLESS_COL_X = (72.0, 240.0, 340.0, 440.0)
# Multi-word labels on purpose: a table whose every cell is a single word has
# no inter-word spacing to measure, and real financial tables are not like that.
BORDERLESS_ROWS = [
    ("Reportable segment", "FY25", "FY24", "FY23"),
    ("Domestic products", "2,940", "2,655", "2,401"),
    ("Export products", "1,872", "1,454", "1,309"),
    ("Services and support", "1,104", "0,991", "0,874"),
    ("Licensing income", "0,806", "0,733", "0,655"),
    ("Other operating", "0,214", "0,198", "0,171"),
    ("Inter segment eliminations", "0,124", "0,111", "0,098"),
    ("Total net revenue", "7,060", "6,142", "5,508"),
]
# Rows before the gap; the last row sits BORDERLESS_GAP_PITCHES below.
#
# The gap is chosen to sit BETWEEN the two thresholds it has to exercise: wider
# than blocks.split_pitch_factor (1.6) so S2 splits the block, narrower than
# columns.run_max_gap_pitch_factor (3.0) so S3 still merges the two blocks into
# one table. A gap of exactly 3.0 pitches lands on the second threshold and
# makes the test a coin-flip on rounding rather than a statement about
# behaviour.
BORDERLESS_SPLIT_AFTER = 7
BORDERLESS_GAP_PITCHES = 2.2


@pytest.fixture(scope="session")
def borderless_pdf(tmp_path_factory) -> Path:
    """A table with no rules at all, and one wide vertical gap near the bottom."""
    out = tmp_path_factory.mktemp("fixtures") / "borderless.pdf"

    def build(doc: pymupdf.Document) -> None:
        page = _page(doc)
        for r, row in enumerate(BORDERLESS_ROWS):
            extra = BORDERLESS_GAP_PITCHES * RULED_PITCH if r >= BORDERLESS_SPLIT_AFTER else 0.0
            y = RULED_TOP + r * RULED_PITCH + extra
            for c, cell in enumerate(row):
                page.insert_text(
                    (BORDERLESS_COL_X[c], y), cell, fontsize=RULED_FONT, fontname="helv"
                )

    return make_pdf(out, build)


@pytest.fixture(scope="session")
def empty_pdf(tmp_path_factory) -> Path:
    """A page with no text at all -- the degenerate case every stage must survive."""
    out = tmp_path_factory.mktemp("fixtures") / "empty.pdf"
    return make_pdf(out, lambda doc: _page(doc))
