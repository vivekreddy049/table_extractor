"""S4.3 / S4.5 -- col/row spans and header_path.

Geometric, document-general. A span exists when a cell's ink covers a neighbour
column (or row) that has no text of its own in that band. Header rows are the
leading run of low-numeric-density rows; ``header_path`` is the spanned text
from outermost to innermost, never a canonicalised concept (DECISIONS.md D10).

No measurement float literals live here (D20): overlap and density thresholds
come from config.yaml.
"""

from __future__ import annotations

from ..config import Config
from ..model import Cell, Page, Table, TokenSource


def apply_spans(table: Table, page: Page, cfg: Config) -> None:
    if not table.cells or not table.columns or not table.rows:
        return

    _merge_close_header_fragments(table, page, cfg)
    _assign_geometric_spans(table, cfg)
    header_n = _mark_header_rows(table, cfg)
    _fill_header_paths(table, header_n)

    ocr_page = "OCR_TOKENS" in page.notes
    for cell in table.cells:
        if not cell.page_no:
            cell.page_no = page.page_no
        if ocr_page:
            cell.source = TokenSource.OCR


def _numeric_share(table: Table, row_idx: int) -> float:
    cells = [c for c in table.cells if c.row_idx == row_idx and c.raw_text.strip()]
    if not cells:
        return 0.0
    n = 0
    for c in cells:
        t = c.raw_text.replace(",", "").replace(" ", "")
        if t[:1] in "-−($€£¥₹`":
            t = t[1:]
        if t.endswith("%"):
            t = t[:-1]
        if t.replace(".", "", 1).isdigit():
            n += 1
    return n / len(cells)


def _header_row_count(table: Table, cfg: Config) -> int:
    cap = cfg.i("spans.max_header_rows")
    max_share = cfg.f("spans.header_max_numeric_share")
    n = 0
    for r in range(min(cap, len(table.rows))):
        if _numeric_share(table, r) > max_share:
            break
        filled = any(
            c.raw_text.strip() for c in table.cells if c.row_idx == r
        )
        if not filled:
            break
        n += 1
    return n


def _merge_close_header_fragments(table: Table, page: Page, cfg: Config) -> None:
    """Join adjacent header words that sit closer than a body gutter.

    A spanning title is several tokens assigned to neighbouring columns by
    centroid. The gap between those fragments is a word-gap, not a column
    gutter, which is how it is distinguished from two real header labels.
    """
    n_header = _header_row_count(table, cfg)
    if n_header <= 0:
        return
    unit = 0.0
    if page.stats is not None:
        unit = page.stats.within_line_word_gap or page.stats.modal_font_size
    max_gap = cfg.f("spans.header_merge_gap_unit_factor") * unit
    by_rc = {(c.row_idx, c.col_idx): c for c in table.cells}

    for r in range(n_header):
        c_i = 0
        while c_i < len(table.columns) - 1:
            left = by_rc.get((r, c_i))
            right = by_rc.get((r, c_i + 1))
            if (
                left is None
                or right is None
                or not left.raw_text.strip()
                or not right.raw_text.strip()
                or left.bbox is None
                or right.bbox is None
            ):
                c_i += 1
                continue
            gap = right.bbox.x0 - left.bbox.x1
            if gap < 0.0:
                gap = 0.0
            if gap > max_gap:
                c_i += 1
                continue
            left.raw_text = (left.raw_text + " " + right.raw_text).strip()
            left.token_indices = left.token_indices + right.token_indices
            if right.bbox is not None:
                left.bbox = left.bbox.union(right.bbox)
            left.col_span = max(left.col_span, 2)
            right.raw_text = ""
            right.token_indices = ()
            c_i += 1


def _assign_geometric_spans(table: Table, cfg: Config) -> None:
    min_share = cfg.f("spans.min_overlap_share")
    occupied = {
        (c.row_idx, c.col_idx) for c in table.cells if c.raw_text.strip()
    }

    for cell in table.cells:
        if not cell.raw_text.strip() or cell.bbox is None:
            continue
        span = cell.col_span
        for nxt in range(cell.col_idx + span, len(table.columns)):
            if (cell.row_idx, nxt) in occupied:
                break
            col = table.columns[nxt]
            width = col.x1 - col.x0
            if width <= 0.0:
                break
            overlap = min(cell.bbox.x1, col.x1) - max(cell.bbox.x0, col.x0)
            if overlap <= 0.0 or overlap / width < min_share:
                break
            span += 1
        cell.col_span = span

        rspan = cell.row_span
        for nxt in range(cell.row_idx + rspan, len(table.rows)):
            if (nxt, cell.col_idx) in occupied:
                break
            neighbour = any(
                (nxt, col.index) in occupied
                for col in table.columns
                if col.index != cell.col_idx
            )
            if not neighbour:
                break
            band = table.rows[nxt].bbox
            height = band.height
            if height <= 0.0:
                break
            overlap = min(cell.bbox.y1, band.y1) - max(cell.bbox.y0, band.y0)
            if overlap <= 0.0 or overlap / height < min_share:
                break
            rspan += 1
        cell.row_span = rspan


def _mark_header_rows(table: Table, cfg: Config) -> int:
    n = _header_row_count(table, cfg)
    for cell in table.cells:
        if cell.row_idx < n:
            cell.is_header = True
    return n


def _cell_covering(table: Table, row: int, col: int) -> Cell | None:
    for c in table.cells:
        if not c.raw_text.strip():
            continue
        if c.row_idx <= row < c.row_idx + c.row_span and c.col_idx <= col < c.col_idx + c.col_span:
            return c
    return table.cell_at(row, col)


def _fill_header_paths(table: Table, header_n: int) -> None:
    if header_n <= 0:
        return
    for col in table.columns:
        path: list[str] = []
        seen: set[str] = set()
        for r in range(header_n):
            cell = _cell_covering(table, r, col.index)
            if cell is None:
                continue
            text = cell.raw_text.strip()
            if not text or text in seen:
                continue
            seen.add(text)
            path.append(text)
        col.header_path = tuple(path)
