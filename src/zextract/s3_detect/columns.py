"""S3 -- column partition of a block.

Two evidence sources, fused rather than arbitrated (DECISIONS.md D19):

  * VERTICAL RULES, where the producer drew them -- exact, and used directly.
  * PERSISTENT WHITESPACE, otherwise -- an x-interval that is empty on most of
    the block's lines is a gutter, and the covered runs between gutters are the
    columns.

"Most of the lines", not "all", is the whole point. A single long spanning row
("Cost of sales:") covers a gutter that every other row leaves empty; requiring
unanimity would merge two columns because of one row. Requiring persistence is
what the projection-profile literature means by a stable minimum, and it is
scale-free.

After segmentation, each column's ALIGNMENT is measured. That is where the
right-edge finding (ARCHITECTURE.md F2) earns its place: gutters tell you where
the columns are, but the spread of left versus right edges tells you what kind
of column it is, and right-aligned means numeric in a financial document.
"""

from __future__ import annotations

import math
from typing import Sequence

from ..config import Config
from ..geometry import Rect, median
from ..model import Alignment, Block, Column, ColumnPartition, PageStats, TextLine, Token

_SYMBOL_TOKENS = frozenset(
    {"$", "₹", "€", "£", "¥", "%", "*", "†", "‡", "(", ")", "`", "Rs", "Rs.", "INR", "USD"}
)


def horizontal_unit(stats: PageStats, cfg: Config) -> float:
    """The smallest meaningful horizontal distance on this page.

    Every column threshold is a multiple of this (DECISIONS.md D20). The
    inter-word gap is the right unit -- a gutter is by definition wider than the
    space between two words in the same cell. The fallbacks matter: a table whose
    every cell is a single word has no measurable word gap at all, and a page
    with no usable statistic at all must still produce *something* rather than
    divide by zero.
    """
    if stats.within_line_word_gap > 0.0:
        return stats.within_line_word_gap
    if stats.modal_font_size > 0.0:
        return cfg.f("columns.unit_from_font_size") * stats.modal_font_size
    if stats.line_pitch > 0.0:
        return cfg.f("columns.unit_from_line_pitch") * stats.line_pitch
    return 1.0


def _block_lines(block: Block, lines: Sequence[TextLine]) -> list[TextLine]:
    by_index = {ln.index: ln for ln in lines}
    return [by_index[i] for i in block.line_indices if i in by_index]


def _tokens_of(line: TextLine, tokens: Sequence[Token]) -> list[Token]:
    return [tokens[i] for i in line.token_indices if 0 <= i < len(tokens)]


def _columns_from_vertical_rules_over(
    extent: Rect, v_rules: Sequence, unit: float, min_overlap: float
) -> list[tuple[float, float]] | None:
    """Column spans from vertical rules that actually cross this block.

    A rule only counts if it spans most of the block's height; a short rule is a
    box edge or a bracket, not a column separator.
    """
    spans = []
    for r in v_rules:
        overlap = r.bbox.intersection_height(extent)
        if extent.height > 0.0 and overlap / extent.height >= min_overlap:
            spans.append(r.position)
    spans = sorted(set(round(s, 3) for s in spans))
    if len(spans) < 3:
        # Fewer than three rules cannot bound two columns on both sides; the
        # gutter path will do better than half a grid.
        return None

    out: list[tuple[float, float]] = []
    for a, b in zip(spans, spans[1:]):
        if b - a > unit:
            out.append((a, b))
    return out or None


def _columns_from_gutters(
    block_lines: Sequence[TextLine],
    tokens: Sequence[Token],
    extent: Rect,
    unit: float,
    cfg: Config,
) -> list[tuple[float, float]]:
    """Column spans from x-intervals that are empty on most lines."""
    persistence = cfg.f("columns.gutter_persistence")
    min_gutter = cfg.f("columns.min_gutter_unit_factor") * unit
    pad = cfg.f("columns.token_pad_unit_factor") * unit
    bin_w = cfg.f("columns.bin_unit_factor") * unit

    if extent.width <= 0.0 or bin_w <= 0.0:
        return []
    n_bins = max(1, int(math.ceil(extent.width / bin_w)))
    if n_bins > 20000:  # pathological page; fall back to one column
        return [(extent.x0, extent.x1)]

    coverage = [0] * n_bins
    n_lines = 0
    for ln in block_lines:
        toks = _tokens_of(ln, tokens)
        if not toks:
            continue
        n_lines += 1
        touched = bytearray(n_bins)
        for t in toks:
            # Padding merges words that belong to the same cell, so the space
            # between "Total" and "net" is not mistaken for a gutter.
            lo = max(0, int((t.bbox.x0 - pad - extent.x0) / bin_w))
            hi = min(n_bins - 1, int((t.bbox.x1 + pad - extent.x0) / bin_w))
            for b in range(lo, hi + 1):
                touched[b] = 1
        for b in range(n_bins):
            if touched[b]:
                coverage[b] += 1

    if n_lines == 0:
        return []

    max_covered = (1.0 - persistence) * n_lines
    is_gutter = [c <= max_covered for c in coverage]

    # Covered runs between gutters are the columns.
    spans: list[tuple[float, float]] = []
    run_start: int | None = None
    for b in range(n_bins):
        if not is_gutter[b]:
            if run_start is None:
                run_start = b
        else:
            if run_start is not None:
                spans.append((run_start, b - 1))
                run_start = None
    if run_start is not None:
        spans.append((run_start, n_bins - 1))

    # Re-merge runs separated by a gutter that is too narrow to be real.
    #
    # NOTE: the width tested here is the PADDED remainder, and min_gutter is
    # applied on top of that padding. That looks like a double-count -- it
    # effectively demands a raw gutter of (min_gutter + 2*pad) = 3.4 word-gaps
    # rather than the 2.2 the config names -- and it was changed to add the
    # padding back on the reasoning that padding alone already closes
    # intra-cell word gaps.
    #
    # Measured, that reasoning does not survive contact with the corpus. The
    # compounded threshold is load-bearing: relaxing it to a raw 2.2 word-gaps
    # recovered the Description/Form gutter on Apple's exhibit index (a genuine
    # 3.24-word-gap gutter, empty on 100% of lines) but introduced a spurious
    # extra column in two dense financial statements, splitting Apple p40
    # (17x8 -> 6x9 + 11x8) and Shell's balance sheet (25x4 -> 20x5 + 22x4),
    # because the run-merge test requires a stable column count. Two broken
    # balance sheets is not worth one recovered index column.
    #
    # The Form column needs a different mechanism -- stable token-edge
    # alignment as an additional column signal, not a looser whitespace
    # threshold. Left as-is deliberately; see LIMITATIONS.md.
    merged: list[tuple[int, int]] = []
    for s_ in spans:
        if merged and (s_[0] - merged[-1][1] - 1) * bin_w < min_gutter:
            merged[-1] = (merged[-1][0], s_[1])
        else:
            merged.append(s_)
    if not merged:
        return []

    # Columns span GUTTER MIDPOINT to GUTTER MIDPOINT, not content edge to
    # content edge.
    #
    # This matters more than it looks. Content extents are a property of what a
    # particular block happens to contain: on Apple p32 the label column is
    # (35,101) in the "Total cost of sales" block and (52,102) in the "Gross
    # margin" block, because those two strings are different lengths. Comparing
    # those extents to decide whether two blocks share a column layout says no,
    # and the income statement fragments into eleven tables. Gutter midpoints
    # are a property of the LAYOUT and are stable across blocks, so the same
    # comparison says yes.
    edges: list[float] = [extent.x0]
    for a, b in zip(merged, merged[1:]):
        gutter_lo = extent.x0 + (a[1] + 1) * bin_w
        gutter_hi = extent.x0 + b[0] * bin_w
        edges.append(0.5 * (gutter_lo + gutter_hi))
    edges.append(extent.x1)
    return [(edges[i], edges[i + 1]) for i in range(len(merged))]


def _alignment(
    xs0: Sequence[float], xs1: Sequence[float], tol: float, ratio: float
) -> Alignment:
    """Classify a column by which edge of its content is the steadier one.

    The measured finding this implements: on Apple p32 the right edges of the
    numeric columns cluster into three sharp spikes (19/18/18 tokens) while the
    left edges are diffuse noise. Numbers are right-aligned; labels are left
    -aligned and ragged.
    """
    if len(xs0) < 3:
        return Alignment.LEFT
    s0 = _spread(xs0)
    s1 = _spread(xs1)
    if s0 <= tol and s1 <= tol:
        return Alignment.LEFT  # single-width content; left is the safe default
    if s1 * ratio < s0:
        return Alignment.RIGHT
    if s0 * ratio < s1:
        return Alignment.LEFT
    return Alignment.CENTRE if abs(s0 - s1) <= tol else Alignment.MIXED


def _spread(values: Sequence[float]) -> float:
    """Median absolute deviation -- robust to the one outlier row."""
    m = median(list(values))
    return median([abs(v - m) for v in values])


def _absorb_symbol_columns(
    cols: list[Column],
    contents: list[list[str]],
    content_spans: list[tuple[float, float]],
    total_width: float,
    cfg: Config,
) -> list[Column]:
    """Merge currency-symbol and marker columns into their neighbours.

    Apple p32 puts every `$` in its own physical column: a naive partition emits
    seven columns where the document means four (ARCHITECTURE.md F3). The symbol
    is not dropped -- it is merged, and the merge is recorded on the surviving
    column, because S8 needs the currency identity and the footnote markers.
    """
    max_share = cfg.f("columns.symbol_max_width_share")
    min_purity = cfg.f("columns.symbol_min_purity")

    flags: list[bool] = []
    for col, cells, span in zip(cols, contents, content_spans):
        filled = [c for c in cells if c.strip()]
        # Width is measured over the column's CONTENT, not its boundaries.
        # Columns run gutter-midpoint to gutter-midpoint, so a 6pt-wide `$`
        # band sitting in a 90pt slot reads as 23% of the table and fails a
        # test meant to ask "is this column narrow?". The content extent is
        # what that question was always about.
        width = max(0.0, span[1] - span[0])
        width_ok = total_width > 0.0 and width / total_width <= max_share
        pure = bool(filled) and sum(
            1 for c in filled if c.strip() in _SYMBOL_TOKENS
        ) >= min_purity * len(filled)
        flags.append(bool(width_ok and pure))

    out: list[Column] = []
    pending: list[int] = []
    for i, col in enumerate(cols):
        if flags[i] and i + 1 < len(cols):
            # A leading symbol belongs to the column on its right.
            pending.append(i)
            continue
        x0 = col.x0
        absorbed = tuple(pending)
        for p in pending:
            x0 = min(x0, cols[p].x0)
        pending = []
        out.append(
            Column(
                index=len(out),
                x0=x0,
                x1=col.x1,
                alignment=col.alignment,
                is_symbol=False,
                absorbed=absorbed,
            )
        )
    # A trailing symbol column with nothing to its right joins the last column.
    if pending and out:
        last = out[-1]
        out[-1] = Column(
            index=last.index,
            x0=last.x0,
            x1=max(last.x1, max(cols[p].x1 for p in pending)),
            alignment=last.alignment,
            is_symbol=False,
            absorbed=last.absorbed + tuple(pending),
        )
    return out


def partition_block(
    block: Block,
    lines: Sequence[TextLine],
    tokens: Sequence[Token],
    v_rules: Sequence,
    stats: PageStats,
    cfg: Config,
) -> ColumnPartition:
    """Compute the column partition of one block."""
    return partition_lines(
        _block_lines(block, lines), tokens, block.bbox, v_rules, stats, cfg
    )


def partition_lines(
    blines: Sequence[TextLine],
    tokens: Sequence[Token],
    extent: Rect,
    v_rules: Sequence,
    stats: PageStats,
    cfg: Config,
) -> ColumnPartition:
    """Compute a column partition over an arbitrary set of lines.

    Taking lines rather than a block is what lets S3 run this twice: once per
    block to decide which blocks belong together, and once over the whole run to
    produce the table's actual columns.
    """
    if not blines:
        return ColumnPartition(columns=(), source="empty", support=0.0)

    unit = horizontal_unit(stats, cfg)
    spans = _columns_from_vertical_rules_over(
        extent, v_rules, unit, cfg.f("columns.rule_block_overlap_min")
    )
    source = "vertical-rules"
    if not spans:
        spans = _columns_from_gutters(blines, tokens, extent, unit, cfg)
        source = "gutters"
    if not spans:
        return ColumnPartition(columns=(), source=source, support=0.0)

    # Content per span, used for alignment and symbol detection.
    lefts: list[list[float]] = [[] for _ in spans]
    rights: list[list[float]] = [[] for _ in spans]
    contents: list[list[str]] = [[] for _ in spans]
    assigned = 0
    total = 0
    for ln in blines:
        for t in _tokens_of(ln, tokens):
            total += 1
            k = _span_index(spans, t.bbox)
            if k is None:
                continue
            assigned += 1
            lefts[k].append(t.bbox.x0)
            rights[k].append(t.bbox.x1)
            contents[k].append(t.text)

    cols = [
        Column(
            index=i,
            x0=lo,
            x1=hi,
            alignment=_alignment(
                lefts[i],
                rights[i],
                cfg.f("columns.alignment_tolerance_unit_factor") * unit,
                cfg.f("columns.alignment_ratio"),
            ),
        )
        for i, (lo, hi) in enumerate(spans)
    ]
    content_spans = [
        (min(lefts[i]), max(rights[i])) if lefts[i] else (0.0, 0.0)
        for i in range(len(spans))
    ]
    cols = _absorb_symbol_columns(cols, contents, content_spans, extent.width, cfg)
    cols = [
        Column(
            index=i,
            x0=c.x0,
            x1=c.x1,
            alignment=c.alignment,
            is_symbol=c.is_symbol,
            absorbed=c.absorbed,
        )
        for i, c in enumerate(cols)
    ]

    support = assigned / total if total else 0.0
    return ColumnPartition(columns=tuple(cols), source=source, support=support)


def _span_index(spans: Sequence[tuple[float, float]], box: Rect) -> int | None:
    """Which span a token belongs to, by centre, falling back to best overlap."""
    cx = box.cx
    for i, (lo, hi) in enumerate(spans):
        if lo <= cx <= hi:
            return i
    best, best_ov = None, 0.0
    for i, (lo, hi) in enumerate(spans):
        ov = max(0.0, min(box.x1, hi) - max(box.x0, lo))
        if ov > best_ov:
            best, best_ov = i, ov
    return best
