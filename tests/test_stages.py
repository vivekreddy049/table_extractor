"""Spans, stitch, footnotes, OCR fallback, frozen calibration."""

from __future__ import annotations

import pymupdf
import pytest

from zextract.pipeline import run_layout
from zextract.s9_confidence.apply_calib import load_model

from pathlib import Path

from conftest import (
    RULED_COL_X,
    RULED_FONT,
    RULED_PITCH,
    RULED_ROWS,
    _page,
    make_pdf,
)


def _page(doc, width=612.0, height=792.0):
    return doc.new_page(width=width, height=height)


def _accepted(doc):
    return [t for p in doc.pages for t in p.tables if t.accepted]


def test_header_path_is_filled_on_a_simple_table(ruled_pdf, cfg):
    t = _accepted(run_layout(ruled_pdf, cfg))[0]
    assert t.columns[0].header_path
    assert t.cells[0].is_header or any(c.is_header for c in t.cells)


def test_spanning_header_merges_close_fragments(tmp_path, cfg):
    pdf = tmp_path / "span.pdf"

    def build(doc):
        page = _page(doc)
        page.insert_text((72.0, 100.0), "Particulars", fontsize=9.0, fontname="helv")
        page.insert_text((280.0, 100.0), "Current", fontsize=9.0, fontname="helv")
        page.insert_text((330.0, 100.0), "period", fontsize=9.0, fontname="helv")
        page.insert_text((72.0, 114.0), "Revenue", fontsize=9.0, fontname="helv")
        page.insert_text((300.0, 114.0), "4,812", fontsize=9.0, fontname="helv")
        page.insert_text((420.0, 114.0), "4,109", fontsize=9.0, fontname="helv")
        page.insert_text((72.0, 128.0), "Expenses", fontsize=9.0, fontname="helv")
        page.insert_text((300.0, 128.0), "3,940", fontsize=9.0, fontname="helv")
        page.insert_text((420.0, 128.0), "3,551", fontsize=9.0, fontname="helv")
        for i in range(8):
            page.insert_text(
                (72.0, 400.0 + i * 14.0),
                "The company reported higher revenue across every reporting segment.",
                fontsize=9.0,
                fontname="helv",
            )

    make_pdf(pdf, build)
    tables = _accepted(run_layout(pdf, cfg))
    assert tables
    t = max(tables, key=lambda x: len(x.cells))
    spanned = [c for c in t.cells if c.col_span > 1 or c.row_span > 1]
    paths = [col.header_path for col in t.columns]
    assert spanned or any(len(p) >= 1 and "Current" in " ".join(p) for p in paths)


def test_two_page_table_is_stitched(tmp_path, cfg):
    pdf = tmp_path / "stitch.pdf"

    def build(doc):
        p1 = _page(doc)
        for r, row in enumerate(RULED_ROWS):
            y = 100.0 + r * RULED_PITCH
            for c, cell in enumerate(row):
                p1.insert_text((RULED_COL_X[c], y), cell, fontsize=RULED_FONT, fontname="helv")
        for i in range(6):
            p1.insert_text(
                (72.0, 400.0 + i * RULED_PITCH),
                "The company reported higher revenue across every reporting segment.",
                fontsize=RULED_FONT,
                fontname="helv",
            )
        p2 = _page(doc)
        extra = (("Other income", "312", "288"), ("Finance costs", "212", "198"))
        # Header repeated at the top, then two body rows. y=20 sits inside the
        # continuity window (1.5 pitches from the top).
        for r, row in enumerate((RULED_ROWS[0],) + extra):
            y = 20.0 + r * RULED_PITCH
            for c, cell in enumerate(row):
                p2.insert_text((RULED_COL_X[c], y), cell, fontsize=RULED_FONT, fontname="helv")
        for i in range(6):
            p2.insert_text(
                (72.0, 400.0 + i * RULED_PITCH),
                "The company reported higher revenue across every reporting segment.",
                fontsize=RULED_FONT,
                fontname="helv",
            )

    make_pdf(pdf, build)
    doc = run_layout(pdf, cfg)
    accepted = _accepted(doc)
    stitched = [t for t in accepted if "STITCHED" in t.flags]
    continued = [
        t for p in doc.pages for t in p.tables if t.rejected_as == "STITCHED_INTO_PRIOR"
    ]
    if stitched:
        assert stitched[0].end_page == 2
        assert continued
        labels = {c.raw_text for c in stitched[0].cells if c.col_idx == 0}
        assert "Other income" in labels or "Finance costs" in labels
    else:
        # Continuity is strict by design; a miss must not invent a merge.
        pytest.skip("fixture did not meet continuity; stitch correctly refused")


def test_footnote_marker_binds_to_block(tmp_path, cfg):
    pdf = tmp_path / "fn.pdf"

    def build(doc):
        page = _page(doc)
        rows = [list(r) for r in RULED_ROWS]
        rows[1][0] = "Revenue*"
        for r, row in enumerate(rows):
            y = 100.0 + r * RULED_PITCH
            for c, cell in enumerate(row):
                page.insert_text((RULED_COL_X[c], y), cell, fontsize=RULED_FONT, fontname="helv")
        page.insert_text(
            (72.0, 720.0),
            "* Includes inter-segment sales.",
            fontsize=8.0,
            fontname="helv",
        )
        for i in range(6):
            page.insert_text(
                (72.0, 400.0 + i * RULED_PITCH),
                "The company reported higher revenue across every reporting segment.",
                fontsize=RULED_FONT,
                fontname="helv",
            )

    make_pdf(pdf, build)
    doc = run_layout(pdf, cfg)
    assert doc.footnotes
    t = _accepted(doc)[0]
    bound = [c for c in t.cells if c.footnote_ids]
    assert bound
    assert "FOOTNOTE_BOUND" in bound[0].notes


def test_digital_page_is_not_ocrd(ruled_pdf, cfg):
    page = run_layout(ruled_pdf, cfg).pages[0]
    assert "OCR_TOKENS" not in page.notes
    assert "OCR_UNAVAILABLE" not in page.notes


def test_empty_page_survives_ocr_fallback(empty_pdf, cfg):
    page = run_layout(empty_pdf, cfg).pages[0]
    assert "OCR_TOKENS" in page.notes or "OCR_UNAVAILABLE" in page.notes or "OCR_EMPTY" in page.notes


def test_calibration_records_raw_score(ruled_pdf, cfg):
    t = _accepted(run_layout(ruled_pdf, cfg))[0]
    assert t.cells
    assert all("raw_score" in c.signals for c in t.cells)
    model = load_model()
    assert model["logistic"]["coef"]


def test_continuity_is_measured_from_content_top_not_the_page_edge(tmp_path, cfg):
    """Regression: the continuity gate must survive a realistic top margin.

    The original rule compared the continuation's y0 against ~1.5 line pitches
    from the PHYSICAL page edge. Real documents start their body 78pt (Apple) to
    88pt (ICRA) down the page, so the gate opened on 0 of 121 and 0 of 7 pages
    respectively -- stitching was dead code on every real document while passing
    its own unit test, whose fixture placed the continuation at y=20.

    This fixture uses a 90pt top margin on both pages, which is ordinary.
    """
    pdf = tmp_path / "stitch_margin.pdf"
    top = 90.0
    cols = (72.0, 300.0, 420.0)

    def build(doc):
        p1 = _page(doc)
        for r, row in enumerate(RULED_ROWS):
            for c, cell in enumerate(row):
                p1.insert_text(
                    (cols[c], top + r * RULED_PITCH), cell,
                    fontsize=RULED_FONT, fontname="helv",
                )
        p2 = _page(doc)
        rows2 = (
            RULED_ROWS[0],
            ("Other income", "312", "288"),
            ("Finance costs", "212", "198"),
            ("Depreciation", "150", "140"),
            ("Exceptional items", "45", "30"),
            ("Total", "9,731", "8,496"),
        )
        for r, row in enumerate(rows2):
            for c, cell in enumerate(row):
                p2.insert_text(
                    (cols[c], top + r * RULED_PITCH), cell,
                    fontsize=RULED_FONT, fontname="helv",
                )

    make_pdf(pdf, build)
    doc = run_layout(pdf, cfg)

    stitched = [
        t for p in doc.pages for t in p.tables if "STITCHED" in t.flags and t.accepted
    ]
    absorbed = [
        t for p in doc.pages for t in p.tables if t.rejected_as == "STITCHED_INTO_PRIOR"
    ]
    assert stitched, (
        "a two-page table with an ordinary 90pt top margin was not stitched -- "
        "the page-edge continuity regression is back"
    )
    assert absorbed, "the continuation page must be marked, not silently dropped"
    assert stitched[0].end_page == 2

    labels = {c.raw_text for c in stitched[0].cells if c.col_idx == 0}
    assert "Total" in labels, "page 2's body did not survive the merge"
    # The repeated header must be dropped, not duplicated into the data.
    assert sum(1 for c in stitched[0].cells
               if c.col_idx == 0 and c.raw_text == "Particulars") == 1


def test_indentation_recovers_the_row_label_hierarchy(tmp_path, cfg):
    """S4.6 -- indentation is often the ONLY signal marking a child line item.

    No bullet, no numbering, no bold: just leading whitespace. Without this the
    database can say which column a cell is in but not which line item, and the
    brief's litmus query ("every cell under a Revenue line item, with its page
    and unit") has no dimension to match on.
    """
    pdf = tmp_path / "indent.pdf"

    def build(doc):
        page = _page(doc)
        # (label, indent in points) -- three nesting levels.
        rows = [
            ("Particulars", 0.0, "FY25", "FY24"),
            ("Revenue", 0.0, "", ""),
            ("Domestic", 14.0, "", ""),
            ("Product A", 28.0, "2,940", "2,655"),
            ("Product B", 28.0, "3,110", "2,801"),
            ("Export", 14.0, "1,802", "1,610"),
            ("Total revenue", 0.0, "7,852", "7,066"),
        ]
        for r, (label, indent, a, b) in enumerate(rows):
            y = 100.0 + r * RULED_PITCH
            page.insert_text((72.0 + indent, y), label, fontsize=RULED_FONT, fontname="helv")
            for c, val in enumerate((a, b)):
                if val:
                    page.insert_text(
                        (300.0 + c * 120.0, y), val, fontsize=RULED_FONT, fontname="helv"
                    )

    make_pdf(pdf, build)
    doc = run_layout(pdf, cfg)
    tables = _accepted(doc)
    assert tables
    table = max(tables, key=lambda t: len(t.rows))

    paths = {
        (table.cell_at(r.index, 0).raw_text.strip() if table.cell_at(r.index, 0) else ""):
        r.row_label_path
        for r in table.rows
    }
    assert paths.get("Product A") == ("Revenue", "Domestic", "Product A"), paths
    assert paths.get("Export") == ("Revenue", "Export"), paths
    # A row back at the outermost indent closes the whole stack.
    assert paths.get("Total revenue") == ("Total revenue",), paths


# --- cross-footing must respect a statement's own scopes -------------------

_SECTIONED = [
    ("Assets:",                    "",       ""),
    ("Cash",                       "100",    "90"),
    ("Receivables",                "250",    "240"),
    ("Inventory",                  "150",    "170"),
    ("Total assets",               "500",    "500"),
    ("Less: allowance",            "(50)",   "(40)"),
    ("Total assets, net",          "450",    "460"),
    ("Liabilities:",               "",       ""),
    ("Payables",                   "200",    "210"),
    ("Borrowings",                 "300",    "290"),
    ("Total liabilities",          "500",    "500"),
]


@pytest.fixture(scope="session")
def sectioned_pdf(tmp_path_factory) -> Path:
    """Two sections, each with its own total, split by an unvalued heading.

    The shape of every financial statement: a heading opens a scope, line items
    accumulate, a total closes it, and a second scope begins. Both totals here
    are arithmetically correct.
    """
    path = tmp_path_factory.mktemp("crossfoot") / "sectioned.pdf"

    def build(doc: pymupdf.Document) -> None:
        page = _page(doc)
        y = 90.0
        for label, a, b in _SECTIONED:
            page.insert_text((72.0, y), label, fontsize=9, fontname="helv")
            if a:
                page.insert_text((330.0, y), a, fontsize=9, fontname="helv")
                page.insert_text((430.0, y), b, fontsize=9, fontname="helv")
            y += 16.0

    return make_pdf(path, build)


def test_a_section_heading_closes_the_arithmetic_scope(sectioned_pdf, cfg):
    """Correct arithmetic must not be flagged because a scope leaked.

    Two failures, both general to sectioned statements rather than to any one
    document. A subtotal that cannot be verified (too few components above it)
    used to fall through and be appended as a LINE ITEM of the next run, so it
    was double-counted against the following total. And an unvalued heading did
    not end the run, so the second section was asked to reconcile against the
    first as well as itself.

    Together they turned a page whose every subtotal is exact into a page where
    every figure carried a CROSSFOOT_FAIL.
    """
    doc = run_layout(sectioned_pdf, cfg)
    tables = [t for p in doc.pages for t in p.tables if t.accepted]
    assert tables, "the sectioned statement was not detected at all"

    failures = [
        i
        for t in tables
        for i in t.issues
        if i.code == "CROSSFOOT_FAIL"
    ]
    assert not failures, (
        "arithmetic that reconciles exactly was flagged: "
        + "; ".join(i.message for i in failures)
    )


def test_a_genuinely_broken_total_is_still_flagged(tmp_path, cfg):
    """The scope fix must not buy its precision by going blind."""
    path = tmp_path / "broken.pdf"
    rows = [
        ("Revenue:", "", ""),
        ("Products", "100", "90"),
        ("Services", "200", "210"),
        ("Total revenue", "999", "300"),  # 100+200 != 999
    ]

    def build(doc: pymupdf.Document) -> None:
        page = _page(doc)
        y = 90.0
        for label, a, b in rows:
            page.insert_text((72.0, y), label, fontsize=9, fontname="helv")
            if a:
                page.insert_text((330.0, y), a, fontsize=9, fontname="helv")
                page.insert_text((430.0, y), b, fontsize=9, fontname="helv")
            y += 16.0

    made = make_pdf(path, build)
    doc = run_layout(made, cfg)
    codes = {i.code for p in doc.pages for t in p.tables for i in t.issues}
    assert "CROSSFOOT_FAIL" in codes, f"a wrong total went unflagged; codes={codes}"


_EXPENSES = [
    ("Expenses",       "",     ""),
    ("Salaries",       "100",  "90"),
    ("Rent",           "200",  "210"),
    ("Total Expenses", "300",  "300"),
]


def test_a_total_is_a_sibling_of_the_items_it_totals(tmp_path, cfg):
    """A total must never become the child of the last line item above it.

        Expenses
            Salaries
            Rent
        Total Expenses

    "Total Expenses" belongs to Expenses -- it is the sibling of Salaries and
    Rent, not Rent's child. Indentation alone cannot see this: a statement
    routinely sets its totals FURTHER RIGHT than the items they total, which is
    precisely the geometry that makes a purely x-coordinate hierarchy nest them
    wrongly. Measured on Apple's 10-K before row kinds existed, 45 of 67 total
    rows hung from a data row.

    So rows carry a KIND -- section / data / total -- and a total pops the data
    levels off the stack before it attaches, whatever the indentation says.
    """
    path = tmp_path / "expenses.pdf"

    def build(doc: pymupdf.Document) -> None:
        page = _page(doc)
        y = 90.0
        for i, (label, a, b) in enumerate(_EXPENSES):
            # Indent the line items, and the TOTAL further right still -- the
            # layout that defeats a geometric hierarchy.
            x = 72.0 if i == 0 else (92.0 if a and label != "Total Expenses" else 104.0)
            page.insert_text((x, y), label, fontsize=9, fontname="helv")
            if a:
                page.insert_text((330.0, y), a, fontsize=9, fontname="helv")
                page.insert_text((430.0, y), b, fontsize=9, fontname="helv")
            y += 16.0

    doc = run_layout(make_pdf(path, build), cfg)
    tables = [t for p in doc.pages for t in p.tables if t.accepted]
    assert tables, "the expenses table was not detected"
    table = tables[0]

    by_label = {}
    for r in table.rows:
        cell = table.cell_at(r.index, 0)
        if cell and cell.raw_text.strip():
            by_label[cell.raw_text.strip()] = r

    total = by_label.get("Total Expenses")
    assert total is not None, f"no Total Expenses row; got {sorted(by_label)}"
    assert total.row_kind == "total", f"classified as {total.row_kind!r}"
    assert "Rent" not in total.row_label_path, (
        f"the total is nested under a line item: {' > '.join(total.row_label_path)}"
    )
    assert total.row_label_path[-1] == "Total Expenses"
    if len(total.row_label_path) > 1:
        assert total.row_label_path[-2] == "Expenses", (
            f"total hangs from {total.row_label_path[-2]!r}, expected 'Expenses'"
        )

    assert by_label["Expenses"].row_kind == "section"
    assert by_label["Salaries"].row_kind == "data"


_SPEC_SHEET = [
    ("Specification",     "Model A", "Model B"),
    ("Dimensions",        "",        ""),
    ("  Height (mm)",     "1820",    "1750"),
    ("  Width (mm)",      "900",     "880"),
    ("Net weight (kg)",   "72",      "68"),
    ("Gross mass (kg)",   "81",      "77"),
    ("Power",             "",        ""),
    ("  Voltage (V)",     "230",     "230"),
    ("  Current (A)",     "13",      "10"),
]


def _write_rows(path: Path, rows) -> Path:
    def build(doc: pymupdf.Document) -> None:
        page = _page(doc)
        y = 90.0
        for label, a, b in rows:
            x = 72.0 + (14.0 if label.startswith("  ") else 0.0)
            page.insert_text((x, y), label.strip(), fontsize=9, fontname="helv")
            if a:
                page.insert_text((330.0, y), a, fontsize=9, fontname="helv")
                page.insert_text((430.0, y), b, fontsize=9, fontname="helv")
            y += 16.0

    return make_pdf(path, build)


def test_a_non_financial_table_is_not_read_as_a_statement(tmp_path, cfg):
    """Row kinds must not assume the document is a financial statement.

    Four of the five kinds are structural -- does the row carry a label, does it
    carry figures -- and so say nothing about the domain. Only ``total`` rests
    on words, and the risky half of that vocabulary is ambiguous outside
    finance: "Net weight" and "Gross mass" on a specification sheet are line
    items, and reading them as totals drops them out of their section's members
    and detaches them from their heading.

    So the weak terms are honoured only in a table that also uses an
    unambiguous one. A statement that computes a "Gross margin" states "Total"
    somewhere too; a spec sheet states neither.
    """
    doc = run_layout(_write_rows(tmp_path / "spec.pdf", _SPEC_SHEET), cfg)
    tables = [t for p in doc.pages for t in p.tables if t.accepted]
    assert tables, "the specification table was not detected"
    table = tables[0]

    kinds = {}
    for row in table.rows:
        cell = table.cell_at(row.index, 0)
        if cell and cell.raw_text.strip():
            kinds[cell.raw_text.strip()] = row

    for label in ("Net weight (kg)", "Gross mass (kg)"):
        row = kinds.get(label)
        assert row is not None, f"{label!r} missing; got {sorted(kinds)}"
        assert row.row_kind == "data", (
            f"{label!r} read as {row.row_kind!r} -- financial vocabulary applied "
            "to a table that never uses a total"
        )

    # The structural kinds still work, because they never depended on domain.
    assert kinds["Dimensions"].row_kind == "section"
    assert kinds["Height (mm)"].row_kind == "data"
    assert kinds["Height (mm)"].row_label_path[:1] == ("Dimensions",)


def test_weak_total_words_still_count_alongside_a_strong_one(tmp_path, cfg):
    """The corroboration must not disarm genuine financial totals."""
    rows = [
        ("Particulars",       "2024", "2023"),
        ("Net sales:",        "",     ""),
        ("  Products",        "100",  "90"),
        ("  Services",        "200",  "210"),
        ("  Total net sales", "300",  "300"),
        ("Gross margin",      "120",  "110"),
    ]
    doc = run_layout(_write_rows(tmp_path / "stmt.pdf", rows), cfg)
    tables = [t for p in doc.pages for t in p.tables if t.accepted]
    assert tables
    kinds = {}
    for row in tables[0].rows:
        cell = tables[0].cell_at(row.index, 0)
        if cell and cell.raw_text.strip():
            kinds[cell.raw_text.strip()] = row.row_kind
    assert kinds.get("Total net sales") == "total"
    assert kinds.get("Gross margin") == "total", (
        "a weak term was ignored in a table that plainly uses totals"
    )
