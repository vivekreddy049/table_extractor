"""S3 -- table candidates, grown from blocks that agree on their columns.

The inversion described in ARCHITECTURE.md 9.2: nothing here recognises a table
by appearance. A table is *defined* as a maximal run of consecutive blocks whose
column partitions agree, and it falls out of the geometry. Side-by-side tables
separate because they produce different partitions in the same y-range; a table
embedded in prose separates because the prose around it partitions into one
column.

The trap classifier then runs on the reconstructed grid rather than on a fuzzy
region, which is what makes rules like "the last column is a page-number
sequence" expressible at all (DECISIONS.md D5).
"""

from __future__ import annotations

import re
from typing import Sequence

from ..config import Config
from ..geometry import Rect, union_all
from ..model import (
    Block,
    Column,
    ColumnPartition,
    Page,
    Table,
    TextLine,
)
from .columns import _SYMBOL_TOKENS, horizontal_unit, partition_block, partition_lines

_INT_RE = re.compile(r"^\d{1,4}$")
_NUMERIC_RE = re.compile(r"^[\(\-−]?[\d,.]+[\)%]?$")
_INDEX_RE = re.compile(r"\d+(\s*[-–]\s*\d+)?(\s*,\s*\d+(\s*[-–]\s*\d+)?)+$")
# A Western-grouped number is comma-separated digits too, so "2,940" matched the
# index pattern and got a genuine revenue table rejected as a glossary -- a
# silent detection failure of exactly the kind this project exists to avoid.
# Thousands separators are followed by EXACTLY three digits and page references
# are not, so the two are separable without guessing.
_GROUPED_NUMBER_RE = re.compile(r"^[\(\-−]?\d{1,3}(,\d{3})+(\.\d+)?[\)%]?$")
_TOC_HEADING_RE = re.compile(r"\b(contents|index|glossary)\b", re.I)


def _is_spanning(
    block: Block,
    prev: Block,
    cols: Sequence[Column],
    tol: float,
    indent_tol: float,
    max_gap: float,
) -> bool:
    """Can this one-column block be absorbed into the run as a section row?

    A section label inside a statement ("Cost of sales:") must not cut the table
    in two. But "sits somewhere inside the table's horizontal extent" is far too
    weak a test: the centred footnote "See accompanying Notes to Consolidated
    Financial Statements." also sits inside that extent, and absorbing it put
    the word "Financial" into a currency column on Apple p32, which in turn
    stopped the `$` column being recognised as a symbol column.

    So a spanning row must also START in the first column -- section labels are
    left-aligned with the row labels above them -- and must be vertically
    adjacent to the run rather than merely below it.
    """
    if not cols:
        return False
    # "Starts somewhere inside the first column" is still too weak when that
    # column is wide: on Apple p32 the label column spans x=19..262 and the
    # centred note "See accompanying Notes to..." starts at x=192, comfortably
    # inside it. Section labels are LEFT-ALIGNED with the row labels above them,
    # at one of the table's indent levels; a centred line is not a section
    # label. So the test is alignment with the label column's content edge.
    aligned_left = block.bbox.x0 <= cols[0].x0 + indent_tol
    within_extent = block.bbox.x1 <= cols[-1].x1 + tol
    adjacent = (block.bbox.y0 - prev.bbox.y1) <= max_gap
    return aligned_left and within_extent and adjacent


def _centred_within(
    box: Rect, left_edge: float, right_edge: float,
    unit: float, min_margin: float, symmetry: float,
) -> bool:
    """Large, near-equal margins on both sides of the given extent."""
    left = box.x0 - left_edge
    right = right_edge - box.x1
    if left <= min_margin * unit or right <= min_margin * unit:
        return False
    widest = max(left, right)
    return abs(left - right) <= symmetry * widest


_DIGIT = re.compile(r"[0-9]")


def _heading_lines(page: Page, unit: float, cfg: Config) -> frozenset[int]:
    """Lines that are page headings, not table content.

    A statement title destroys the geometry of the table beneath it, which is
    why this is worth doing in S3 rather than leaving to presentation. Apple p35
    (shareholders' equity) is the case: the centred title lays "Apple Inc.",
    "CONSOLIDATED STATEMENTS OF SHAREHOLDERS' EQUITY" and "(In millions, except
    per-share amounts)" across the page at three different indents, and the
    partition comes out SEVEN columns where the statement has four. A merge may
    not lose columns, so every block below was refused: the page split into
    three tables and the closing "Dividends declared per share" row was
    stranded as a one-row candidate and rejected outright. With these lines
    held out the same page partitions as four columns at support 1.000.

    Two conditions, both about what a title IS. It is centred on the page's
    content extent rather than anchored to a label column -- the same test
    `_is_centred` already applies to trailing notes. And it carries no digits: a
    row of a financial statement has figures in it, a title does not.

    The test is per LINE, not per block, because block grouping does not respect
    the distinction: on p35 the title's three lines are grouped together with
    "Years ended", which belongs to the column header, and that one line pulls
    the block's bounding box far enough off-centre to defeat a block-level test.
    Nothing is lost by holding these lines out of the geometry -- S8 binds the
    same text as the table's section context, which is where a caption belongs.
    """
    if not page.blocks:
        return frozenset()
    left_edge = min(b.bbox.x0 for b in page.blocks)
    right_edge = max(b.bbox.x1 for b in page.blocks)
    min_margin = cfg.f("rows.centred_min_margin_unit_factor")
    symmetry = cfg.f("rows.centred_symmetry_ratio")
    return frozenset(
        ln.index
        for ln in page.lines
        if not _DIGIT.search(ln.text)
        and _centred_within(ln.bbox, left_edge, right_edge, unit, min_margin, symmetry)
    )


def _is_centred(
    block: Block,
    cols: Sequence[Column],
    unit: float,
    min_margin: float,
    symmetry: float,
) -> bool:
    """Is this block centred within the table's horizontal extent?

    The remaining way a note gets absorbed. "See accompanying Notes to
    Consolidated Financial Statements." partitions into two columns of its own,
    so the single-column guard never sees it, and it sits close enough below the
    last data row to pass the gap test.

    But it is CENTRED -- on Apple p32 it leaves a 173pt margin on both sides of a
    table spanning x=19..593 -- and a table row never is. Data rows are anchored
    to the label column on the left and the last numeric column on the right;
    section labels are anchored left and ragged right. Equal, large margins on
    both sides means the line belongs to the page, not to the grid.

    Expressed as a ratio, so it holds at any page size.
    """
    if not cols:
        return False
    return _centred_within(
        block.bbox, cols[0].x0, cols[-1].x1, unit, min_margin, symmetry
    )


def _respects_gutters(
    block: Block,
    lines_by_index,
    tokens,
    cols: Sequence[Column],
    share: float,
    skip: frozenset[int] = frozenset(),
) -> bool:
    """Do this block's lines leave the table's gutters empty?

    This is what separates a table row from a line of prose, and it is a
    property of what a table IS rather than of any particular document. A table
    row puts its content inside the columns and leaves the gutters clear. A
    sentence runs straight across them.

    Apple p25 is the shape: the section heading, the introducing sentence
    ("The following table shows net sales by reportable segment for 2024, 2023
    and 2022 (dollars in millions):") and the commentary paragraph beneath the
    table all sit in their own blocks, all overlap the table's x-range, and all
    keep the joint partition's support high -- so every other test waves them
    through. But each one lays text across the column boundaries, which no row
    of the table does.

    A boundary only counts when it falls inside the line's own x-extent: a short
    label that stops before the first gutter is not evidence either way.
    """
    if len(cols) < 2:
        return True
    bounds = [c.x1 for c in cols[:-1]]

    checked = respected = 0
    for li in block.line_indices:
        if li in skip:
            continue  # a centred title is not evidence about this table's gutters
        ln = lines_by_index.get(li)
        if ln is None:
            continue
        inner = [b for b in bounds if ln.bbox.x0 < b < ln.bbox.x1]
        if not inner:
            continue  # nothing to test against
        checked += 1
        crossed = False
        for ti in ln.token_indices:
            if not (0 <= ti < len(tokens)):
                continue
            tok = tokens[ti]
            # A lone currency glyph straddling a boundary is not prose. The
            # symbol column is deliberately absorbed when it appears on too few
            # rows to persist (ARCHITECTURE.md F3), which leaves the boundary
            # sitting inside the glyph: on Apple p33 the "$" of "Total
            # comprehensive income" spans 363.1-367.6 across a boundary at
            # 365.1, and that one token was enough to read the statement's
            # closing total as a paragraph and split it off as its own table.
            # What this test is for is a WORD laid across a gutter.
            if tok.text.strip() in _SYMBOL_TOKENS:
                continue
            tb = tok.bbox
            if any(tb.x0 < b < tb.x1 for b in inner):
                crossed = True
                break
        if not crossed:
            respected += 1

    if checked == 0:
        return True
    return respected / checked >= share


def _cell_texts(table: Table) -> list[list[str]]:
    grid = [["" for _ in table.columns] for _ in table.rows]
    for c in table.cells:
        if 0 <= c.row_idx < len(grid) and 0 <= c.col_idx < len(table.columns):
            grid[c.row_idx][c.col_idx] = c.raw_text.strip()
    return grid


def classify_trap(table: Table, page: Page, cfg: Config) -> str | None:
    """Name the reason this candidate is not a data table, or None.

    Every rule is stated as a document-general property, never as a
    sample-specific string, and every rejection keeps its code so that a wrong
    rejection is one grep away rather than indistinguishable from a miss.
    """
    grid = _cell_texts(table)
    n_rows = len(grid)
    n_cols = len(table.columns)
    if n_rows == 0 or n_cols == 0:
        return "TRAP_EMPTY"

    body = grid[1:] if n_rows > 1 else grid
    filled_rows = [r for r in body if any(c for c in r)]
    if not filled_rows:
        return "TRAP_EMPTY"

    share = cfg.f("traps.row_share")

    # TABLE OF CONTENTS -- the trap that scores HIGHEST on tableness: two clean
    # columns, perfectly right-aligned integers. No threshold separates it from
    # a two-column data table; only the observation that its last column is a
    # page-number sequence bounded by the document's own page count.
    last_col = [r[-1] for r in filled_rows]
    page_numbers = [
        c for c in last_col if _INT_RE.match(c) and int(c) <= page.page_no + 10000
    ]
    if page_numbers and len(page_numbers) >= share * len(filled_rows):
        in_range = [int(c) for c in page_numbers]
        non_decreasing = all(a <= b for a, b in zip(in_range, in_range[1:]))
        heading_says_toc = _TOC_HEADING_RE.search(
            " ".join(grid[0]) if grid else ""
        ) is not None
        first_col_prose = sum(
            1 for r in filled_rows if len(r[0].split()) >= 2
        ) >= share * len(filled_rows)
        if non_decreasing and (heading_says_toc or first_col_prose) and n_cols <= 3:
            return "TRAP_TOC"

    # INDEX / GLOSSARY -- a label followed by comma-separated page references.
    def _looks_like_page_refs(cells: list[str]) -> bool:
        """Does any single cell in this row hold a list of page references?

        Tested per cell and with a full match, not by searching the row's joined
        text. Joining "4,812" and "4,109" produces "4,812 4,109", in which the
        index pattern happily finds "4,109" -- and Apple's income statement gets
        rejected as a glossary. Cell boundaries are information; discarding them
        before matching throws away the thing that makes the two distinguishable.
        """
        for text in cells:
            t = text.strip()
            if not t or _GROUPED_NUMBER_RE.match(t):
                continue  # a thousands separator, not a page-reference list
            if _INDEX_RE.fullmatch(t):
                return True
        return False

    index_like = sum(1 for r in filled_rows if _looks_like_page_refs(r[1:] or r[:1]))
    if index_like >= share * len(filled_rows) and n_cols <= 3:
        return "TRAP_INDEX"

    # TWO-COLUMN PROSE -- exactly the gutter signature of a data table, with
    # none of the content. Long cells, no numbers, both columns left-aligned.
    if n_cols == 2:
        words = [len(c.split()) for r in filled_rows for c in r if c]
        mean_words = sum(words) / len(words) if words else 0.0
        numeric = sum(
            1 for r in filled_rows for c in r if c and _NUMERIC_RE.match(c)
        )
        total_cells = sum(1 for r in filled_rows for c in r if c)
        numeric_share = numeric / total_cells if total_cells else 0.0
        if mean_words > cfg.f("traps.prose_mean_words") and numeric_share < cfg.f(
            "traps.prose_max_numeric_share"
        ):
            return "TRAP_PROSE_2COL"

    # KEY-VALUE BLOCK -- a couple of labelled fields, not a table.
    if n_cols == 2 and n_rows <= cfg.i("traps.keyvalue_max_rows"):
        if all(r[0].rstrip().endswith(":") for r in filled_rows if r[0]):
            return "TRAP_KEYVALUE"

    if n_rows < cfg.i("traps.min_rows"):
        return "TRAP_TOO_FEW_ROWS"

    return None



# Trap codes that mean "this looked like a list/index/prose fragment", as
# opposed to TRAP_EMPTY (nothing here) or a future data-quality code. A small
# accepted candidate sharing a rejected neighbour's exact column structure
# under one of these is not a coincidence -- it is the SAME underlying list,
# split by the run-growing test into pieces that happened to land on
# different sides of the row-count cutoff.
_LIST_LIKE_TRAPS = frozenset(
    {"TRAP_TOO_FEW_ROWS", "TRAP_TOC", "TRAP_INDEX", "TRAP_PROSE_2COL", "TRAP_KEYVALUE"}
)


def _table_extent(table: Table) -> tuple[float, float] | None:
    if not table.columns:
        return None
    return (table.columns[0].x0, table.columns[-1].x1)


def _columns_match(a: Table, b: Table, tol: float) -> bool:
    """Do these two candidates occupy the same physical footprint on the page?

    Measured on the real cases this targets, comparing full interior column
    boundaries does not work: a 1-2 row fragment's INTERIOR gutter is the
    least reliable thing about it -- it is inferred from whatever little
    content that fragment happens to contain, which is exactly why it ended up
    a fragment. Apple p31's title/header stub infers its one interior gutter
    at x=399.6 from two sparse lines, while the real index sharing its run
    has its gutter at x=542.4 (the actual page-number column) -- 143pt apart,
    despite being the same list. The OUTER extent, by contrast, is stable:
    both start at the same left margin and end at the same right edge,
    because that is set by the table's overall content width, not by which
    two lines a particular fragment happened to keep. Combined with the other
    three signals this check runs alongside (small row count, tight vertical
    adjacency, a list-like-rejected neighbour), outer-extent agreement alone
    is strong enough without also demanding interior agreement.
    """
    ea, eb = _table_extent(a), _table_extent(b)
    if ea is None or eb is None:
        return False
    return abs(ea[0] - eb[0]) <= tol and abs(ea[1] - eb[1]) <= tol


def _reject_fragments_of_rejected_runs(tables: list[Table], unit: float, cfg: Config) -> None:
    """Guilt by adjacency: a small accepted table next to a rejected one with
    the same column structure is a surviving splinter of the same list.

    This is deliberately narrower than making the run-growing merge test
    itself more permissive. An earlier attempt at that (trimming a run's
    leading/trailing blocks to strip absorbed prose) was reverted because it
    also cut into genuinely correct tables -- it dropped Apple's income
    statement from 100% content match to 40.6% and cut Shell's balance sheet
    from 46 rows to 25. This pass touches NOTHING about how a table's own
    rows or columns were built; it only asks whether an ALREADY-ACCEPTED
    candidate's immediate neighbour was rejected as list-like and shares its
    exact column boundaries -- a signature no genuine data table produces,
    because S3 already grows real financial tables into one clean run with no
    rejected same-shaped neighbours to begin with.

    Evidence this targets: Apple p31 (a 2-row title+header stub, accepted,
    sitting directly above a 7-row TRAP_TOC-rejected index with matching
    columns) and Apple p58 (an exhibit index shredded into 22 candidates by
    inconsistent per-row column occupancy; every surviving accepted fragment
    was sandwiched between TRAP_TOO_FEW_ROWS-rejected neighbours with the
    identical column layout).
    """
    max_gap = cfg.f("traps.fragment_adjacency_gap_unit_factor") * unit
    tol = cfg.f("traps.fragment_column_tol_unit_factor") * unit
    max_rows = cfg.i("traps.fragment_max_rows")

    for t in tables:
        if t.rejected_as is not None or len(t.rows) > max_rows:
            continue
        for other in tables:
            if other is t or other.rejected_as not in _LIST_LIKE_TRAPS:
                continue
            gap = max(0.0, t.bbox.y0 - other.bbox.y1, other.bbox.y0 - t.bbox.y1)
            if gap > max_gap:
                continue
            if _columns_match(t, other, tol):
                t.rejected_as = "TRAP_FRAGMENT_OF_REJECTED_RUN"
                t.flags.append(f"adjacent_to_table{other.index}")
                break


def detect_tables(page: Page, cfg: Config, build_grid) -> list[Table]:
    """Grow table candidates on one page and classify each one.

    ``build_grid`` is injected (S4's row/cell reconstruction) so that this module
    depends on the grid *interface* rather than on S4 -- the trap classifier
    needs cell text, but detection must not otherwise know how cells are made.
    """
    if page.stats is None or not page.blocks:
        return []

    unit = horizontal_unit(page.stats, cfg)
    tol = cfg.f("columns.agreement_unit_factor") * unit
    v_rules = page.vertical_rules()

    headings = _heading_lines(page, unit, cfg)
    body_lines = [ln for ln in page.lines if ln.index not in headings]

    partitions = [
        partition_block(b, body_lines, page.tokens, v_rules, page.stats, cfg)
        for b in page.blocks
    ]

    # Grow runs greedily, testing each merge by RE-PARTITIONING the union.
    #
    # The obvious approach -- partition each block, then group blocks whose
    # partitions look alike -- does not work, and the reason is worth stating.
    # A column boundary derived from one block is the midpoint of that block's
    # gutters, and a wide gutter's midpoint depends on the longest label that
    # block happens to contain. On Apple p32 the label/number boundary comes out
    # at 245, 251 and 283 in three consecutive blocks of the SAME table. No
    # tolerance separates that from a genuinely different layout.
    #
    # So the question is not "do these two partitions look alike" but "does a
    # single partition still describe both blocks". Re-partitioning the union
    # answers it directly, and it fixes the currency columns for free: a `$` that
    # appears on 4 rows out of 25 falls below the gutter-persistence threshold at
    # table scale and is absorbed, where at block scale it was a column of its own
    # (ARCHITECTURE.md F3).
    max_gap = cfg.f("columns.run_max_gap_pitch_factor") * max(page.stats.line_pitch, 0.0)
    merge_support = cfg.f("columns.merge_min_support")

    by_index = {ln.index: ln for ln in page.lines}

    def lines_for(idxs: Sequence[int]) -> list[TextLine]:
        out = [
            by_index[i]
            for bi in idxs
            for i in page.blocks[bi].line_indices
            if i in by_index and i not in headings
        ]
        out.sort(key=lambda ln: ln.sort_key)
        return out

    indent_tol = cfg.f("columns.spanning_max_indent_unit_factor") * unit

    runs: list[tuple[list[int], ColumnPartition]] = []
    for i, (block, part) in enumerate(zip(page.blocks, partitions)):
        if runs:
            idxs, current = runs[-1]
            prev_block = page.blocks[idxs[-1]]
            gap_ok = (block.bbox.y0 - prev_block.bbox.y1) <= max_gap

            # A single-column block joining a table is a section label, and it
            # gets the stricter test: the joint partition alone would happily
            # swallow a centred note or a page footer, because one extra line
            # barely moves the gutters.
            single_column_ok = len(part.columns) >= 2 or _is_spanning(
                block, prev_block, current.columns, tol, indent_tol, max_gap
            )
            anchored = not _is_centred(
                block,
                current.columns,
                unit,
                cfg.f("rows.centred_min_margin_unit_factor"),
                cfg.f("rows.centred_symmetry_ratio"),
            )

            if (
                gap_ok
                and single_column_ok
                and anchored
                and len(current.columns) >= 2
            ):
                trial_idxs = idxs + [i]
                trial_box = union_all([page.blocks[k].bbox for k in trial_idxs])
                joint = partition_lines(
                    lines_for(trial_idxs), page.tokens, trial_box, v_rules, page.stats, cfg
                )
                # A merge is accepted only if the joint partition keeps at least
                # as many columns as the run already had. A genuinely different
                # table below shares fewer gutters, so the joint partition
                # collapses -- which is exactly the signal to stop.
                # The gutter test is asked of the JOINT partition, not of the
                # run's current columns, because a run's columns are only as
                # trustworthy as the blocks that produced them. A run seeded on
                # one short row has its boundary at the midpoint of one enormous
                # gutter -- on Apple p33 the "Net income" row alone puts the
                # label boundary at x=211.7 -- and every genuine row below it
                # carries a longer label that crosses there. Judged against the
                # seed, the rest of the statement is prose; judged against the
                # partition the merged table would actually have (x=349.9, four
                # columns, support 1.000), it is what it is: more rows.
                #
                # This is the same failure the re-partitioning merge test exists
                # to avoid (see above), which had been fixed for the column-count
                # test but not for this gate. The prose defence is unaffected:
                # a paragraph still lays a token across the merged table's
                # boundaries, and one prose block does not move those boundaries.
                if (
                    len(joint.columns) >= len(current.columns)
                    and joint.support >= merge_support
                    and _respects_gutters(
                        block, by_index, page.tokens, joint.columns,
                        cfg.f("columns.gutter_respect_share"), headings,
                    )
                ):
                    runs[-1] = (trial_idxs, joint)
                    continue
        runs.append(([i], part))

    tables: list[Table] = []
    for idxs, final in runs:
        if len(final.columns) < 2:
            continue

        blocks = [page.blocks[i] for i in idxs]
        bbox = union_all([b.bbox for b in blocks])

        table = Table(
            index=len(tables),
            page_no=page.page_no,
            bbox=bbox,
            columns=list(final.columns),
            block_indices=tuple(idxs),
            partition_source=final.source,
            support=final.support,
        )
        build_grid(table, page, cfg)
        table.rejected_as = classify_trap(table, page, cfg)
        if part.support < cfg.f("columns.min_support"):
            table.flags.append("LOW_PARTITION_SUPPORT")
        tables.append(table)

    _reject_fragments_of_rejected_runs(tables, unit, cfg)

    # Re-index so the identity of a table is its position on the page.
    for i, t in enumerate(tables):
        t.index = i
    return tables
