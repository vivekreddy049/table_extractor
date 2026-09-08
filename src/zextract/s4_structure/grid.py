"""S4 -- rows and cells inside a table candidate.

Rows are recovered here, not at S2, because a wrapped cell and a genuine new row
are geometrically identical until the column partition is known
(``model.Block``). With the columns in hand the question becomes answerable:
a line that puts content in only the label column, indented past the row above
it, is a wrap; a line that continues unfinished cells in the same columns
("September 28," / "2024") is a wrap; a line that fills columns the row above
left empty, without stacking two finished numbers, is a wrap. Anything else is
a new row.

Where the signal is not decisive we DO NOT GUESS. Splitting a wrapped label into
two rows is a visible, recoverable error that a reviewer can see and fix.
Silently merging two data rows destroys a number and is not recoverable. So the
merge rule refuses to stack two finished values in the same column, and the
genuinely ambiguous label-only case is flagged (ROW_WRAP_AMBIGUOUS) rather
than resolved.
"""

from __future__ import annotations

import re
from typing import Sequence

from ..config import Config
from ..geometry import Rect, union_all
from ..model import (
    Cell,
    Column,
    Page,
    Row,
    Table,
    TextLine,
    Token,
    TokenSource,
)
from .spans import apply_spans
from .tree import build_tree


def _lines_of(table: Table, page: Page) -> list[TextLine]:
    wanted: list[int] = []
    for bi in table.block_indices:
        if 0 <= bi < len(page.blocks):
            wanted.extend(page.blocks[bi].line_indices)
    by_index = {ln.index: ln for ln in page.lines}
    out = [by_index[i] for i in wanted if i in by_index]
    out.sort(key=lambda ln: ln.sort_key)
    return out


LEADING_SYMBOLS = frozenset({"$", "₹", "€", "£", "¥", "`", "Rs", "Rs.", "INR", "USD"})

# A finished blank, as printed. These are values, not fragments waiting for
# another line, so stacking two of them is two rows, not a wrap.
_CLOSED_BLANKS = frozenset({"-", "–", "—", "Nil", "NIL", "NA", "N/A", "n.a.", "n.a"})

# Wrap punctuation: a cell ending in one of these is incomplete on this line
# and the next line in the same columns is its continuation, not a new row.
# Trailing comma is the Apple-header case ("September 28," / "2024").
_WRAP_ENDINGS = (",", ";", "/", ":", "-", "–", "—", "(", "&")

# A finished number: digits with optional grouping, decimal, sign, percent
# or a leading currency glyph. Letters make it a fragment (e.g. "Aug 29,").
_COMPLETE_NUMBER = re.compile(
    r"^[\(\[\{\-−–—]?"
    r"(?:[$€£¥₹`]|Rs\.?|INR|USD)?"
    r"\d+(?:[,.\s]\d{2,3})*(?:\.\d+)?"
    r"[\)\]\}\-−–—]?"
    r"%?$"
)


# A trailing footnote marker is NOT wrap punctuation. "10.18*" is as finished a
# value as "10.18"; the star says something about the exhibit, not about whether
# the cell continues on the next line. This is the same split applied in S8 --
# raw_text keeps the marker, structural decisions ignore it.
#
# The run must END on a marker glyph, so "10.19*," (marker, then a real trailing
# comma) is still read as open. Separators are allowed only BETWEEN markers,
# which is how Apple prints a doubly-noted exhibit: "10.19*, **".
_FOOTNOTE_MARKER_TAIL = re.compile(r"(?:\s*,?\s*[*†‡]+)+$")


def _strip_footnote_markers(s: str) -> str:
    return _FOOTNOTE_MARKER_TAIL.sub("", s).strip()


def _is_complete_number(text: str) -> bool:
    s = re.sub(r"\s+", "", _strip_footnote_markers((text or "").strip()))
    if not s or s[-1:] in ",;/:":
        return False
    return _COMPLETE_NUMBER.match(s) is not None


def _is_closed_value(text: str) -> bool:
    """A cell that is a finished value, not a fragment expecting a wrap."""
    s = (text or "").strip()
    if not s:
        return False
    if s in _CLOSED_BLANKS or s.upper() in {"NIL", "NA", "N/A", "N.A."}:
        return True
    if s.startswith("#"):
        return True
    return _is_complete_number(s)


def _is_open_cell(text: str) -> bool:
    """True when another physical line may still belong to this cell."""
    s = (text or "").strip()
    if not s:
        return True
    if s.endswith(_WRAP_ENDINGS):
        return True
    return not _is_closed_value(s)


def _line_cells(
    line: TextLine, tokens: Sequence[Token], columns: Sequence[Column]
) -> dict[int, str]:
    buckets: dict[int, list[str]] = {}
    for t_i, c_i in _assign_line(line, tokens, columns):
        buckets.setdefault(c_i, []).append(tokens[t_i].text)
    return {c: " ".join(parts) for c, parts in buckets.items()}


def _group_cells(
    group: Sequence[TextLine], tokens: Sequence[Token], columns: Sequence[Column]
) -> dict[int, str]:
    buckets: dict[int, list[str]] = {}
    for ln in group:
        for t_i, c_i in _assign_line(ln, tokens, columns):
            buckets.setdefault(c_i, []).append(tokens[t_i].text)
    return {c: " ".join(parts) for c, parts in buckets.items()}


def _should_wrap_multicol(
    prev_cells: dict[int, str], curr_cells: dict[int, str], label_col: int
) -> bool:
    """May the current line join the row above as a wrapped continuation?

    Two regimes, both observed in the samples and neither specific to them:

    * The line occupies a subset of the columns already open above it, and
      those cells are unfinished ("September 28," / "2024"; a rating that
      ends in ';' / "reaffirmed"). Stacking two finished numbers in the same
      column is a new row, never a wrap.
    * The line fills additional columns whose first line was sparse -- a
      wrapped multi-column row whose date and rating appear one physical
      line below the instrument name. A label-only row above (a section
      heading) is excluded: "Net sales:" must not swallow "Products".
    """
    curr_occ = set(curr_cells)
    prev_occ = set(prev_cells)
    if not curr_occ or not prev_occ:
        return False
    if curr_occ <= {label_col}:
        return False
    if prev_occ <= {label_col}:
        return False

    overlap = curr_occ & prev_occ
    if overlap:
        for col in overlap:
            prev_t = prev_cells[col]
            curr_t = curr_cells.get(col, "")
            if _is_closed_value(prev_t) and _is_closed_value(curr_t):
                return False
            # A finished number under a header word is a new data row, not a
            # wrap. Completing a fragment ("September 28," / "2024") is a wrap
            # because the cell above ends in wrap punctuation.
            if _is_closed_value(curr_t):
                p = prev_t.strip()
                if p and not p.endswith(_WRAP_ENDINGS):
                    return False
        if not all(_is_open_cell(prev_cells[col]) for col in overlap):
            return False
        return True

    # No overlapping columns: the continuation lives entirely in columns the
    # first line left empty. Only join when the previous cells are still open
    # text, never when the previous line already closed its values.
    return all(_is_open_cell(t) for t in prev_cells.values())


def _column_of(columns: Sequence[Column], box: Rect) -> int | None:
    cx = box.cx
    for c in columns:
        if c.x0 <= cx <= c.x1:
            return c.index
    best, best_ov = None, 0.0
    for c in columns:
        ov = max(0.0, min(box.x1, c.x1) - max(box.x0, c.x0))
        if ov > best_ov:
            best, best_ov = c.index, ov
    return best


def _assign_line(
    line: TextLine, tokens: Sequence[Token], columns: Sequence[Column]
) -> list[tuple[int, int]]:
    """Assign each token of a line to a column, as (token index, column index).

    Plain centre containment is not enough for currency symbols. Apple p32 puts
    the `$` in its own narrow band, and because that band is populated on only 4
    rows out of 28 it falls below the gutter-persistence threshold at table
    scale -- correctly, since it is not a column. But the token still exists, and
    landing it by nearest-centre puts `$` in the column to its LEFT, producing
    `$ 294,866 $` in one cell and `298,085 $` in the next.

    A leading currency symbol belongs to the number it introduces, so it is
    assigned to the column of its right-hand neighbour on the same line. This is
    the token-level half of symbol absorption (ARCHITECTURE.md S4.4); the
    column-level half lives in s3_detect.columns.
    """
    idxs = sorted(
        (i for i in line.token_indices if 0 <= i < len(tokens)),
        key=lambda i: (tokens[i].bbox.x0, i),
    )
    assigned: list[tuple[int, int]] = []
    for pos, t_i in enumerate(idxs):
        tok = tokens[t_i]
        col = _column_of(columns, tok.bbox)
        if tok.text.strip() in LEADING_SYMBOLS and pos + 1 < len(idxs):
            neighbour = _column_of(columns, tokens[idxs[pos + 1]].bbox)
            if neighbour is not None:
                col = neighbour
        if col is not None:
            assigned.append((t_i, col))
    return assigned


def _occupied(
    line: TextLine, tokens: Sequence[Token], columns: Sequence[Column]
) -> set[int]:
    return {c for _, c in _assign_line(line, tokens, columns)}


def _trim_trailing_notes(
    groups: list[list[TextLine]],
    flags: list[list[str]],
    table: Table,
    page: Page,
    min_margin: float,
    symmetry: float,
) -> int:
    """Drop trailing rows that are page notes rather than table content.

    "See accompanying Notes to Consolidated Financial Statements." sits directly
    under Apple's income statement, close enough that no gap splits it into its
    own block, so S3 never gets a merge decision to refuse -- it arrives already
    inside the table.

    Two properties separate it from a real last row, and both are needed. It is
    CENTRED in the table extent, where data rows are anchored to the label column
    on the left; and its LAST COLUMN IS EMPTY, where a genuine closing row -- a
    total, a grand total -- always carries a figure in the final numeric column.
    Requiring both is what keeps this from eating totals.

    Returns the number of rows removed.
    """
    if len(table.columns) < 2 or len(groups) < 2:
        return 0

    left_edge = table.columns[0].x0
    right_edge = table.columns[-1].x1
    last_col = table.columns[-1].index
    removed = 0

    while len(groups) > 1:
        group = groups[-1]
        box = union_all([ln.bbox for ln in group])

        occupied = set()
        for ln in group:
            occupied |= _occupied(ln, page.tokens, table.columns)
        if last_col in occupied:
            break

        left = box.x0 - left_edge
        right = right_edge - box.x1
        if left <= 0.0 or right <= 0.0:
            break
        if left <= min_margin or right <= min_margin:
            break
        if abs(left - right) > symmetry * max(left, right):
            break

        groups.pop()
        flags.pop()
        removed += 1

    return removed


def _rule_between(prev_bottom: float, curr_top: float, rules: Sequence[float]) -> bool:
    lo, hi = (prev_bottom, curr_top) if prev_bottom <= curr_top else (curr_top, prev_bottom)
    return any(lo < y < hi for y in rules)


def _ruled_row_boundaries(table: Table, page: Page, cfg: Config) -> list[float]:
    """y positions where the producer drew a row separator across this table.

    S3 already reads vertical rules as column boundaries; this is the same
    principle applied to rows, and it is the strongest row signal available
    because it is declared rather than inferred. A rule only counts when it
    crosses most of the table -- a full-width separator does, a hyperlink
    underline or a bracket beneath one cell does not.
    """
    width = table.bbox.width
    if width <= 0.0:
        return []
    share = cfg.f("rows.row_rule_min_width_share")
    out = [
        r.position
        for r in page.horizontal_rules()
        if r.bbox.intersection_width(table.bbox) / width >= share
    ]
    return sorted(out)


def build_grid(table: Table, page: Page, cfg: Config) -> None:
    """Populate ``table.rows`` and ``table.cells`` in place."""
    assert page.stats is not None
    lines = _lines_of(table, page)
    if not lines or not table.columns:
        return

    pitch = page.stats.line_pitch
    wrap_gap = cfg.f("rows.wrap_max_gap_pitch_factor") * pitch if pitch > 0.0 else 0.0
    label_col = table.columns[0].index

    row_rules = _ruled_row_boundaries(table, page, cfg)

    groups: list[list[TextLine]] = []
    flags: list[list[str]] = []
    prev_occ: set[int] = set()

    for ln in lines:
        occ = _occupied(ln, page.tokens, table.columns)
        merged = False
        note: str | None = None
        gap = (ln.bbox.y0 - groups[-1][-1].bbox.y1) if groups else 0.0
        close = (not groups) or pitch <= 0.0 or gap <= wrap_gap
        # A ruled separator between the two lines ends the row, whatever the
        # gap and the cell shapes suggest. MRPL p4's complexity table is the
        # case: the producer rules every row, but "Instrument"/"Complexity
        # indicator" and the data line beneath it are both unfinished-looking
        # text in the same two columns, so the wrap test fused the header into
        # the first data row.
        if groups and _rule_between(groups[-1][-1].bbox.y1, ln.bbox.y0, row_rules):
            close = False

        if groups and close and occ == {label_col} and len(prev_occ) > 1:
            prev_x0 = groups[-1][-1].bbox.x0
            indent_eps = cfg.f("rows.wrap_min_indent_unit_factor") * (
                page.stats.within_line_word_gap or page.stats.modal_font_size
            )
            indented = ln.bbox.x0 > prev_x0 + indent_eps
            if indented:
                # Hanging indent under a populated row: a wrapped label.
                groups[-1].append(ln)
                merged = True
            else:
                # Same shape, no indent. This is the genuinely ambiguous case --
                # a wrapped label and a section heading look alike. Keep them
                # separate (the recoverable error) and say so.
                note = "ROW_WRAP_AMBIGUOUS"
        elif groups and close:
            prev_cells = _group_cells(groups[-1], page.tokens, table.columns)
            curr_cells = _line_cells(ln, page.tokens, table.columns)
            if _should_wrap_multicol(prev_cells, curr_cells, label_col):
                groups[-1].append(ln)
                merged = True

        if not merged:
            groups.append([ln])
            flags.append([note] if note else [])
        prev_occ = occ if not merged else (prev_occ | occ)

    unit = page.stats.within_line_word_gap or page.stats.modal_font_size
    trimmed = _trim_trailing_notes(
        groups,
        flags,
        table,
        page,
        cfg.f("rows.centred_min_margin_unit_factor") * unit,
        cfg.f("rows.centred_symmetry_ratio"),
    )

    rows: list[Row] = []
    for i, (group, fl) in enumerate(zip(groups, flags)):
        rows.append(
            Row(
                index=i,
                bbox=union_all([ln.bbox for ln in group]),
                line_indices=tuple(ln.index for ln in group),
                flags=tuple(fl),
            )
        )
    table.rows = rows
    if trimmed:
        table.flags.append("NOTE_ROW_TRIMMED")

    # Cells: every token lands in exactly one (row, column).
    buckets: dict[tuple[int, int], list[int]] = {}
    for r_i, group in enumerate(groups):
        for ln in group:
            for t_i, c_i in _assign_line(ln, page.tokens, table.columns):
                buckets.setdefault((r_i, c_i), []).append(t_i)

    cells: list[Cell] = []
    for (r_i, c_i) in sorted(buckets):
        idxs = sorted(buckets[(r_i, c_i)], key=lambda i: page.tokens[i].sort_key)
        boxes = [page.tokens[i].bbox for i in idxs]
        cells.append(
            Cell(
                row_idx=r_i,
                col_idx=c_i,
                raw_text=" ".join(page.tokens[i].text for i in idxs),
                bbox=union_all(boxes) if boxes else None,
                token_indices=tuple(idxs),
                page_no=page.page_no,
                source=(
                    TokenSource.OCR
                    if "OCR_TOKENS" in page.notes
                    else TokenSource.TEXT_LAYER
                ),
            )
        )
    table.cells = cells
    assign_row_labels(table, page, cfg)
    build_tree(table)
    apply_spans(table, page, cfg)

    if any("ROW_WRAP_AMBIGUOUS" in r.flags for r in rows):
        table.flags.append("ROW_WRAP_AMBIGUOUS")


_DIGIT_RE = re.compile(r"[0-9]")

def _classify_rows(table: Table, label_col: int, cfg: Config) -> None:
    """Give every row a KIND before any hierarchy is built.

    Three kinds, and the distinction is what a statement means rather than how
    it is laid out:

    * ``section`` -- a labelled row carrying no figure in ANY column. It opens
      a scope: "Net sales:", "Deferred tax liabilities:".
    * ``total``   -- a row whose label announces that it closes a scope.
    * ``data``    -- a line item.

    Kind is stored on the row so the rest of the pipeline can use it. It is the
    same distinction cross-footing needs (S9 derives its scopes the same way),
    and the same one an Excel or JSON export needs to indent a statement
    correctly, rather than each stage re-deriving it from geometry.
    """
    strong = tuple(cfg.get("rows.total_words_strong"))
    weak = tuple(cfg.get("rows.total_words_weak"))

    def label_of(row) -> str:
        c = table.cell_at(row.index, label_col)
        return c.raw_text.strip().lower() if c else ""

    # The weak terms are honoured only where the table proves it uses totals at
    # all. "Gross margin" closes a scope in an income statement; "Gross mass"
    # is a line item on a specification sheet, and nothing in the words tells
    # them apart. What does tell them apart is the company they keep: a
    # statement that computes a gross margin also states totals elsewhere.
    weak_allowed = any(label_of(r).startswith(strong) for r in table.rows)
    prefixes = strong + weak if weak_allowed else strong

    for row in table.rows:
        cell = table.cell_at(row.index, label_col)
        text = (cell.raw_text.strip().lower() if cell else "")
        # A digit test on the raw text, not the parsed value_type: S4 runs
        # before S7 assigns types, and re-deriving the type here would be a
        # second, divergent parser. "Does this row carry a figure anywhere"
        # needs no more than that.
        has_figure = any(
            _DIGIT_RE.search(c.raw_text)
            for c in table.cells
            if c.row_idx == row.index and c.col_idx != label_col
        )
        if not text:
            # Figures with no line-item name are a HEADER ("2024 2023 2022"),
            # not a member of any sum. Calling them data is how a row of years
            # gets added to the total beneath it: the residual comes out as
            # exactly -2024, which is the year itself.
            row.row_kind = "header" if has_figure else "blank"
            continue
        if not has_figure:
            row.row_kind = "section"
        elif text.startswith(prefixes):
            row.row_kind = "total"
        else:
            row.row_kind = "data"


def assign_row_labels(table: Table, page: Page, cfg: Config) -> None:
    """S4.6 -- recover the row-label hierarchy from indentation.

    In a financial statement the thing a reader searches for -- "Revenue",
    "Trade Receivables" -- is a ROW label, not a column header, and its parent
    is expressed by nothing but leading whitespace. Without this, the database
    can answer "which column is this" but not "which line item is this", and the
    brief's litmus query has no dimension to match on.

    Left edges of the label column are clustered into indent levels: a new
    distinct left edge, more than one word-gap to the right of the previous
    level, opens a child level; an edge at or left of an existing level closes
    back to it. A stack of the most recent label at each level gives every row
    its full path.

    Deliberately conservative. Indent levels are capped, and a row whose label
    cell is empty inherits the current stack rather than inventing a level.
    """
    if not table.columns or not table.rows:
        return

    unit = page.stats.within_line_word_gap or page.stats.modal_font_size or 1.0
    step = cfg.f("rows.indent_step_unit_factor") * unit
    max_levels = cfg.i("rows.max_indent_levels")
    label_col = table.columns[0].index

    _classify_rows(table, label_col, cfg)

    stack: list[tuple[float, str, str]] = []  # (left edge, label, kind) per level
    for row in table.rows:
        cell = table.cell_at(row.index, label_col)
        text = cell.raw_text.strip() if cell else ""
        if not text or cell is None or cell.bbox is None:
            # No label of its own: it belongs to whatever is currently open.
            row.row_label_path = tuple(lbl for _, lbl, _ in stack)
            row.indent_level = max(0, len(stack) - 1)
            continue

        x0 = cell.bbox.x0

        if row.row_kind == "total":
            # A total closes the items it totals; it is their SIBLING, never
            # their child. Pop the data levels first, whatever the indentation
            # says -- "Total net sales" is set further right than "Services" on
            # Apple's income statement, so geometry alone nests a total under
            # the last line item above it and the path claims the total belongs
            # to that one item. What remains on the stack is the section the
            # total closes, which is exactly the parent it should hang from.
            while stack and stack[-1][2] in ("data", "total"):
                stack.pop()

        # Close every level whose indent is at or right of this one.
        while stack and x0 <= stack[-1][0] + step * 0.5:
            stack.pop()
        if len(stack) >= max_levels:
            stack.pop()
        stack.append((x0, text, row.row_kind))

        row.row_label_path = tuple(lbl for _, lbl, _ in stack)
        row.indent_level = len(stack) - 1
