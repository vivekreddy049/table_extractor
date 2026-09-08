"""S3/S4 -- column partition, table growth, grid reconstruction, trap rejection."""

from __future__ import annotations

import os
import pathlib

import pymupdf
import pytest

from zextract.model import Alignment
from zextract.pipeline import run_layout

from conftest import BORDERLESS_ROWS, RULED_COL_X, RULED_ROWS, make_pdf


def _only_table(pdf, cfg, page_no: int = 0):
    page = run_layout(pdf, cfg).pages[page_no]
    tables = [t for t in page.tables if t.accepted]
    assert tables, f"no accepted table; candidates={[t.rejected_as for t in page.tables]}"
    return max(tables, key=lambda t: len(t.rows) * len(t.columns))


def _grid(table):
    g = [["" for _ in table.columns] for _ in table.rows]
    for c in table.cells:
        g[c.row_idx][c.col_idx] = c.raw_text
    return g


def test_ruled_table_is_reconstructed_exactly(ruled_pdf, cfg):
    t = _only_table(ruled_pdf, cfg)
    assert len(t.columns) == len(RULED_ROWS[0])
    assert _grid(t) == [list(r) for r in RULED_ROWS]


def test_borderless_table_is_reconstructed_exactly(borderless_pdf, cfg):
    t = _only_table(borderless_pdf, cfg)
    assert len(t.columns) == len(BORDERLESS_ROWS[0])
    assert _grid(t) == [list(r) for r in BORDERLESS_ROWS]


def test_numeric_columns_are_detected_as_right_aligned(borderless_pdf, cfg):
    t = _only_table(borderless_pdf, cfg)
    # The fixture writes labels left-aligned and figures as fixed-width strings
    # starting at a fixed x, so the label column must not read as right-aligned.
    assert t.columns[0].alignment is not Alignment.RIGHT


def test_a_table_spanning_several_blocks_stays_one_table(tmp_path, cfg):
    """A rule under the header must not split the table in two.

    The regression this guards is the one that fragmented Apple's income
    statement into eleven tables: per-block partitions were compared to each
    other, and a wide gutter's midpoint moves with the longest label in each
    block.
    """
    pdf = tmp_path / "multiblock.pdf"

    def build(doc):
        page = doc.new_page(width=612.0, height=792.0)
        rows = [
            ("Particulars", "FY25", "FY24"),
            ("Revenue from operations", "4,812", "4,109"),
            ("Other income", "312", "288"),
            ("Total income", "5,124", "4,397"),
            ("Operating expenditure", "3,940", "3,551"),
            ("Finance costs", "212", "198"),
            ("Profit before tax", "972", "648"),
        ]
        for r, row in enumerate(rows):
            y = 100.0 + r * 14.0
            for c, cell in enumerate(row):
                page.insert_text((RULED_COL_X[c], y), cell, fontsize=9.0, fontname="helv")
        # Rules under the header and under two subtotals -- three block splits.
        for r in (0, 3, 6):
            ry = 100.0 + (r + 0.5) * 14.0
            page.draw_line(pymupdf.Point(72.0, ry), pymupdf.Point(500.0, ry), width=0.6)

    make_pdf(pdf, build)
    page = run_layout(pdf, cfg).pages[0]
    assert len(page.blocks) >= 3, "fixture should produce several blocks"
    accepted = [t for t in page.tables if t.accepted]
    assert len(accepted) == 1
    assert len(accepted[0].rows) == 7


def test_leading_currency_symbol_joins_the_number_it_introduces(tmp_path, cfg):
    """`$` sits in its own band; it must not land in the column to its left."""
    pdf = tmp_path / "currency.pdf"

    def build(doc):
        page = doc.new_page(width=612.0, height=792.0)
        rows = [
            ("Segment revenue", "FY25", "FY24"),
            ("Domestic products", "2,940", "2,655"),
            ("Export products", "1,872", "1,454"),
            ("Services and other", "1,104", "991"),
            ("Total net revenue", "5,916", "5,100"),
        ]
        for r, row in enumerate(rows):
            y = 100.0 + r * 14.0
            page.insert_text((72.0, y), row[0], fontsize=9.0, fontname="helv")
            for c, val in enumerate(row[1:]):
                x = 300.0 + c * 120.0
                if r in (1, 4):  # a $ only on the first and last data rows
                    page.insert_text((x, y), "$", fontsize=9.0, fontname="helv")
                page.insert_text((x + 22.0, y), val, fontsize=9.0, fontname="helv")

    make_pdf(pdf, build)
    t = _only_table(pdf, cfg)
    grid = _grid(t)
    assert len(t.columns) == 3, f"columns={[(c.x0, c.x1) for c in t.columns]}"
    # The symbol belongs with its own number, not with the previous cell.
    assert grid[1][1] == "$ 2,940"
    assert grid[1][2] == "$ 2,655"
    assert grid[2][1] == "1,872"


def test_table_of_contents_is_rejected_with_a_reason(tmp_path, cfg):
    """The trap that scores highest on tableness must still be refused."""
    pdf = tmp_path / "toc.pdf"

    def build(doc):
        page = doc.new_page(width=612.0, height=792.0)
        entries = [
            ("Business overview", "1"),
            ("Risk factors", "5"),
            ("Unresolved staff comments", "17"),
            ("Legal proceedings", "18"),
            ("Market for common equity", "19"),
            ("Financial statements", "28"),
            ("Controls and procedures", "51"),
        ]
        page.insert_text((72.0, 80.0), "TABLE OF CONTENTS", fontsize=11.0, fontname="hebo")
        for r, (label, pageno) in enumerate(entries):
            y = 110.0 + r * 14.0
            page.insert_text((72.0, y), label, fontsize=9.0, fontname="helv")
            page.insert_text((500.0, y), pageno, fontsize=9.0, fontname="helv")

    make_pdf(pdf, build)
    page = run_layout(pdf, cfg).pages[0]
    reasons = {t.rejected_as for t in page.tables}
    assert "TRAP_TOC" in reasons, f"candidates={[(len(t.rows), t.rejected_as) for t in page.tables]}"
    assert not [t for t in page.tables if t.accepted]


def test_rejected_candidates_are_kept_not_dropped(tmp_path, cfg):
    """A rejection must be visible, not indistinguishable from a miss (D5)."""
    pdf = tmp_path / "kv.pdf"

    def build(doc):
        page = doc.new_page(width=612.0, height=792.0)
        for r, (k, v) in enumerate([("Instrument:", "Term loan"), ("Tenor:", "5 years")]):
            y = 100.0 + r * 14.0
            page.insert_text((72.0, y), k, fontsize=9.0, fontname="helv")
            page.insert_text((300.0, y), v, fontsize=9.0, fontname="helv")

    make_pdf(pdf, build)
    page = run_layout(pdf, cfg).pages[0]
    assert page.tables, "candidate must survive as a rejection, not vanish"
    assert all(t.rejected_as for t in page.tables)


def test_every_cell_points_at_real_tokens(ruled_pdf, cfg):
    """Provenance is not optional: every cell traces back to page tokens."""
    page = run_layout(ruled_pdf, cfg).pages[0]
    for t in page.tables:
        for c in t.cells:
            assert c.token_indices
            for i in c.token_indices:
                assert 0 <= i < len(page.tokens)
            assert c.raw_text == " ".join(page.tokens[i].text for i in c.token_indices)
            assert c.bbox is not None


def test_empty_page_yields_no_tables(empty_pdf, cfg):
    assert run_layout(empty_pdf, cfg).pages[0].tables == []


def test_wrapped_period_header_merges_into_one_row(tmp_path, cfg):
    """A date header split across two physical lines is one logical row.

    This is the silent-error shape on Apple p32: 'September 28,' / '2024'
    shipped as two rows, the year cells scoring 0.95 with no reason code.
    """
    pdf = tmp_path / "wrap_header.pdf"

    def build(doc):
        page = doc.new_page(width=612.0, height=792.0)
        page.insert_text((72.0, 100.0), "Particulars", fontsize=9.0, fontname="helv")
        for i, month in enumerate(("September 28,", "September 30,", "September 24,")):
            page.insert_text((280.0 + i * 100.0, 100.0), month, fontsize=9.0, fontname="helv")
        for i, year in enumerate(("2024", "2023", "2022")):
            page.insert_text((280.0 + i * 100.0, 114.0), year, fontsize=9.0, fontname="helv")
        rows = [
            ("Products", "294,866", "298,085", "316,199"),
            ("Services", "96,169", "85,200", "78,129"),
            ("Total net sales", "391,035", "383,285", "394,328"),
            ("Cost of sales", "210,352", "214,137", "223,546"),
            ("Gross margin", "180,683", "169,148", "170,782"),
            ("Operating income", "123,216", "114,301", "119,437"),
            ("Net income", "93,736", "96,995", "99,803"),
        ]
        xs = (72.0, 280.0, 380.0, 480.0)
        for r, row in enumerate(rows):
            y = 128.0 + r * 14.0
            for c, cell in enumerate(row):
                page.insert_text((xs[c], y), cell, fontsize=9.0, fontname="helv")

    make_pdf(pdf, build)
    t = _only_table(pdf, cfg)
    grid = _grid(t)
    assert grid[0][1] == "September 28, 2024", grid[0]
    assert grid[0][2] == "September 30, 2023"
    assert grid[0][3] == "September 24, 2022"
    assert grid[1][0] == "Products"
    assert grid[1][1] == "294,866"


def test_section_heading_does_not_swallow_the_next_data_row(tmp_path, cfg):
    """'Net sales:' is a row of the table, not a wrap of 'Products'."""
    pdf = tmp_path / "section.pdf"

    def build(doc):
        page = doc.new_page(width=612.0, height=792.0)
        rows = [
            ("Particulars", "FY25", "FY24"),
            ("Net sales:", "", ""),
            ("Products", "294,866", "298,085"),
            ("Services", "96,169", "85,200"),
            ("Total net sales", "391,035", "383,285"),
            ("Cost of sales:", "", ""),
            ("Products", "185,233", "189,282"),
            ("Total cost of sales", "210,352", "214,137"),
        ]
        xs = (72.0, 300.0, 420.0)
        for r, row in enumerate(rows):
            y = 100.0 + r * 14.0
            for c, cell in enumerate(row):
                if cell:
                    page.insert_text((xs[c], y), cell, fontsize=9.0, fontname="helv")

    make_pdf(pdf, build)
    grid = _grid(_only_table(pdf, cfg))
    labels = [row[0] for row in grid]
    assert "Net sales:" in labels
    assert "Products" in labels
    # The heading and the first item under it stayed distinct rows.
    i = labels.index("Net sales:")
    assert labels[i + 1] == "Products"


def test_wrapped_rating_text_stays_in_one_cell(tmp_path, cfg):
    """A rating that wraps after a semicolon is one cell, not two rows."""
    pdf = tmp_path / "wrap_rating.pdf"

    def build(doc):
        page = doc.new_page(width=612.0, height=792.0)
        headers = ("Instrument", "Amount", "Rating action")
        xs = (72.0, 300.0, 400.0)
        for c, h in enumerate(headers):
            page.insert_text((xs[c], 100.0), h, fontsize=9.0, fontname="helv")
        page.insert_text((72.0, 114.0), "Term loan facility", fontsize=9.0, fontname="helv")
        page.insert_text((300.0, 114.0), "143.00", fontsize=9.0, fontname="helv")
        page.insert_text((400.0, 114.0), "[ICRA]AA-(Stable);", fontsize=9.0, fontname="helv")
        page.insert_text((400.0, 128.0), "reaffirmed", fontsize=9.0, fontname="helv")
        # Enough extra rows to clear docstrum.min_tokens.
        extras = [
            ("Working capital", "22.00", "reaffirmed"),
            ("Bank guarantee", "10.00", "assigned"),
            ("Unallocated limits", "5.00", "reaffirmed"),
            ("Export packing", "8.00", "assigned"),
            ("Cash credit", "12.00", "reaffirmed"),
            ("Letter of credit", "9.00", "assigned"),
            ("Total", "209.00", ""),
        ]
        for r, row in enumerate(extras):
            y = 142.0 + r * 14.0
            for c, cell in enumerate(row):
                if cell:
                    page.insert_text((xs[c], y), cell, fontsize=9.0, fontname="helv")

    make_pdf(pdf, build)
    grid = _grid(_only_table(pdf, cfg))
    rating_rows = [row for row in grid if row[0].startswith("Term loan")]
    assert rating_rows, grid
    assert "reaffirmed" in rating_rows[0][2]
    assert rating_rows[0][1] == "143.00"


def test_sparse_single_token_columns_do_not_collapse_the_grid(tmp_path, cfg):
    """Regression for the page-23 word-gap bug.

    Root cause: Docstrum's inter-word-gap statistic assumes a token's nearest
    same-line neighbour is the next WORD IN ITS OWN CELL. On a sparse table
    where almost every cell holds exactly one token (a lone year, a lone
    dollar figure), that neighbour is instead the first token of the NEXT
    COLUMN -- so the statistic measures a column gutter and reports it as the
    word gap.

    On the real page this produced word_gap=10.28pt against an 8.1pt font
    (ratio 1.27, vs. a corpus median of 0.28). Every column threshold
    downstream is a multiple of that number (min_gutter_unit_factor,
    token_pad_unit_factor), so the inflated unit swallowed every real gutter
    in the table and a 3-label x 6-year grid collapsed to 2x2.

    The fix is docstrum's WORD_GAP_SIZE_RATIO_OUT_OF_BAND band: a word gap
    that exceeds max_word_gap_size_ratio times the modal font size is refused
    rather than trusted, and the page falls back to the document median. This
    test pins the END-TO-END SHAPE so a future change to that band, or to
    the gutter thresholds it feeds, fails a test instead of only showing up
    as a collapsed table in the viewer.
    """
    pdf = tmp_path / "sparse_single_token.pdf"
    years = ("2019", "2020", "2021", "2022", "2023", "2024")
    rows = [
        ("Alpha Corp", "100", "207", "273", "281", "322", "430"),
        ("Beta Index", "100", "113", "156", "131", "155", "210"),
        ("Gamma Sector Index", "100", "146", "216", "156", "215", "322"),
    ]
    xs = (72.0, 260.0, 310.0, 360.0, 410.0, 460.0, 510.0)

    def build(doc):
        page = doc.new_page(width=612.0, height=792.0)
        for c, year in enumerate(years):
            page.insert_text((xs[c + 1], 100.0), year, fontsize=9.0, fontname="helv")
        for r, row in enumerate(rows):
            y = 118.0 + r * 14.0
            page.insert_text((xs[0], y), row[0], fontsize=9.0, fontname="helv")
            for c, val in enumerate(row[1:]):
                page.insert_text((xs[c + 1], y), val, fontsize=9.0, fontname="helv")

    make_pdf(pdf, build)
    t = _only_table(pdf, cfg)
    assert len(t.columns) == 7, f"columns={[(c.x0, c.x1) for c in t.columns]}"
    grid = _grid(t)
    assert grid[0][1:] == list(years)
    assert grid[1] == ["Alpha Corp", "100", "207", "273", "281", "322", "430"]


# The sample corpus is not redistributable, so it is not in the repository.
# Point ZEXTRACT_SAMPLES at the directory holding the assignment PDFs to run
# the tests that pin behaviour against real documents; without it they skip,
# and the synthetic-fixture tests still cover the same rules.
SAMPLES = pathlib.Path(
    os.environ.get("ZEXTRACT_SAMPLES", "samples")
)
APPLE = SAMPLES / "Apple Form 10-K.pdf"
MRPL = SAMPLES / "Shell MRPL Aviation Fuels and Services Limited.pdf"


@pytest.mark.skipif(not APPLE.exists(), reason="sample corpus not available")
def test_fragment_of_a_rejected_list_is_not_accepted(cfg):
    """A splinter of a rejected list must not survive on a row-count technicality.

    Pinned against the real document rather than a synthetic fixture, because
    the condition needs two ingredients together that proved fiddly to fake:
    hyperlink underlines harvested as ruling lines (which split every row into
    its own block), AND inconsistent per-row column occupancy from wrapped
    description cells (which stops those blocks re-merging). Apple's exhibit
    index on p58 has both, and gets shredded into ~22 candidates; most are
    correctly rejected as TRAP_TOO_FEW_ROWS, but the ones landing at 2-3 rows
    used to clear the cutoff and ship. Its Item 8 sub-index on p31 fails the
    same way, leaving an accepted 2-row title/header stub flush above the
    7-row TRAP_TOC-rejected index it belongs to.

    Both must now be refused, while the real tables on the same document are
    untouched.
    """
    doc = run_layout(APPLE, cfg)

    # p31: the title/header stub above the rejected index
    p31 = doc.pages[30]
    assert not [t for t in p31.tables if t.accepted], (
        "p31 is an index page; no candidate on it should be accepted, got "
        f"{[(t.index, len(t.rows)) for t in p31.tables if t.accepted]}"
    )

    # p58: the shredded exhibit index
    p58 = doc.pages[57]
    assert not [t for t in p58.tables if t.accepted], (
        "p58 is an exhibit index; no candidate on it should be accepted, got "
        f"{[(t.index, len(t.rows)) for t in p58.tables if t.accepted]}"
    )

    # And the real tables elsewhere in the same document still stand.
    for page_no, min_rows in ((32, 20), (43, 10), (40, 15)):
        accepted = [t for t in doc.pages[page_no - 1].tables if t.accepted]
        assert accepted, f"p{page_no} lost its table entirely"
        assert max(len(t.rows) for t in accepted) >= min_rows, (
            f"p{page_no} table shrank below {min_rows} rows"
        )


@pytest.mark.skipif(not APPLE.exists(), reason="sample corpus not available")
def test_footnote_marked_rows_are_not_fused_into_one_wrapped_row(cfg):
    """A trailing footnote marker must not make a finished value look unfinished.

    Apple's exhibit index ends with thirteen exhibits marked "filed herewith"
    ("10.18*", "10.19*, **", "19.1**" ... "97.1*, **"). Those rows carry no
    Form/Exhibit/Filing-Date values -- that is what "filed herewith" means -- so
    they occupy only the number and description columns, and the wrap test in
    S4 decides whether each line opens a new row.

    It decided wrap, for one reason: `_is_closed_value("10.18")` is True but
    `_is_closed_value("10.18*")` was False, so every marked exhibit number read
    as a fragment awaiting a continuation. The whole run collapsed into the
    table's final row -- 18 physical lines, y=417..596, with all thirteen
    exhibit numbers space-joined inside a single cell -- and the table came out
    38 rows against an actual 50. The unmarked exhibits above (4.10 .. 10.17)
    segmented correctly, which is exactly why the defect was invisible until a
    label covered the tail of the page.

    The marker is a note about the exhibit, not about the line, so it is
    stripped before the closedness test and nowhere else: raw_text keeps it.
    """
    doc = run_layout(APPLE, cfg)
    accepted = [t for t in doc.pages[56].tables if t.accepted]
    assert accepted, "p57 lost its exhibit index entirely"
    table = max(accepted, key=lambda t: len(t.rows))

    assert len(table.rows) >= 50, (
        f"exhibit index collapsed to {len(table.rows)} rows; the footnote-marked "
        "tail has been fused back into one wrapped row"
    )

    first_col = {c.raw_text.strip() for c in table.cells if c.col_idx == 0}
    for exhibit in ("10.18*", "19.1**", "32.1***", "97.1*, **"):
        assert exhibit in first_col, f"{exhibit!r} is not a row of its own"

    # The marker survives in raw_text -- stripping happens for the structural
    # decision only, never in the reported bytes.
    assert any(c.raw_text.strip().endswith("*") for c in table.cells if c.col_idx == 0)


@pytest.mark.skipif(not APPLE.exists(), reason="sample corpus not available")
def test_statement_is_one_table_not_a_stack_of_fragments(cfg):
    """A run's gutters must be judged on the merged geometry, not on its seed.

    Apple p33 (comprehensive income) came out as three pieces: the header, the
    lone "Net income" row, and the twelve rows below it. Nothing was wrong with
    the merge itself -- the joint partition of those blocks is four columns at
    support 1.000 -- but the gutter-respect pre-gate was asked about the run's
    CURRENT columns, and a run seeded on one short row puts its label boundary
    at the midpoint of one enormous gutter (x=211.7). Every genuine row below
    carries a longer label than "Net income", crosses there, and reads as prose.

    The closing "Total comprehensive income" row split off for a second reason:
    its "$" glyph straddles a column boundary the symbol-column absorption left
    inside it, and a lone currency symbol was being counted as a word laid
    across a gutter.
    """
    doc = run_layout(APPLE, cfg)
    accepted = [t for t in doc.pages[32].tables if t.accepted]
    assert len(accepted) == 1, (
        "p33 should be a single statement, got "
        f"{[(t.index, len(t.rows), len(t.columns)) for t in accepted]}"
    )
    table = accepted[0]
    assert len(table.columns) == 4
    # 14 rows, not 15: the centred title is held out of the grid and bound as
    # the table's section context instead.
    assert len(table.rows) >= 14, f"p33 lost rows: {len(table.rows)}"
    assert "COMPREHENSIVE INCOME" not in " ".join(
        c.raw_text for c in table.cells
    ), "the page title is being carried as table content"

    # The first data row and the closing total must both be inside it. The
    # currency glyph stays in the cell (the symbol column is absorbed, F3), so
    # match on the figure rather than on the exact cell string.
    texts = " | ".join(c.raw_text for c in table.cells)
    assert "93,736" in texts, "the Net income row is not in the table"
    assert "98,016" in texts, "the Total comprehensive income row was split off"


@pytest.mark.skipif(not MRPL.exists(), reason="sample corpus not available")
def test_a_ruled_separator_is_a_row_boundary_not_a_wrap(cfg):
    """A rule drawn between two lines is the producer declaring a new row.

    S3 reads vertical rules as column boundaries; rows ignored horizontal ones.
    MRPL p4's complexity table rules every row, but its header
    ("Instrument" / "Complexity indicator") and the data line beneath it are
    both unfinished-looking text in the same two columns, so the wrap test fused
    them into one row and the header was lost.
    """
    doc = run_layout(MRPL, cfg)
    accepted = [t for t in doc.pages[3].tables if t.accepted and t.bbox.y0 > 420]
    assert accepted, "MRPL p4 complexity table missing"
    table = accepted[0]
    assert len(table.rows) == 3, f"expected header + 2 rows, got {len(table.rows)}"

    first = {c.raw_text.strip() for c in table.cells if c.row_idx == 0}
    assert "Instrument" in first and "Complexity indicator" in first, (
        f"header row fused into the data rows: {first}"
    )


@pytest.mark.skipif(not APPLE.exists(), reason="sample corpus not available")
def test_a_page_title_does_not_fracture_the_statement_beneath_it(cfg):
    """A centred title must not contribute columns to the table below it.

    Apple p35 (shareholders' equity) split into three tables. The cause was the
    title: "Apple Inc.", "CONSOLIDATED STATEMENTS OF SHAREHOLDERS' EQUITY" and
    "(In millions, except per-share amounts)" sit at three different indents, so
    the partition came out seven columns where the statement has four. Since a
    merge may not lose columns, every block below was refused -- and the closing
    "Dividends declared per share or RSU" row was left as a one-row candidate
    and rejected as TRAP_TOO_FEW_ROWS, despite plainly belonging to the
    statement. Held out, the same page is four columns at support 1.000.
    """
    doc = run_layout(APPLE, cfg)
    accepted = [t for t in doc.pages[34].tables if t.accepted]
    assert len(accepted) == 1, (
        "p35 should be one statement, got "
        f"{[(t.index, len(t.rows), len(t.columns)) for t in accepted]}"
    )
    table = accepted[0]
    assert len(table.columns) == 4, f"title still inflating columns: {len(table.columns)}"

    texts = " | ".join(c.raw_text for c in table.cells)
    assert "62,146" in texts, "the opening balances row is missing"
    assert "0.98" in texts, "the closing dividends-per-share row was stranded"
    assert "SHAREHOLDERS" not in texts, "the title is being carried as a row"
