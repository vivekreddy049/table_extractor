"""S2 -- text lines into blocks.

A block is a run of consecutive lines with no significant vertical break and no
ruling line crossing between them. Blocks are the SCOPE for column analysis, not
table rows: S3 computes a column partition per block and grows tables from runs
of blocks whose partitions agree (ARCHITECTURE.md 9.2), and S4 recovers rows
within a table once that partition is known. See ``model.Block`` for why rows
cannot be settled here.

Two boundary sources, in priority order:

  * a horizontal RULE between two lines -- exact, because the producer drew it;
  * a vertical GAP wider than a multiple of the page's own line pitch.

Rules win where both apply, and the reason is recorded on the block, because
"why did this block end here" is the first question asked when a table comes out
with the wrong shape.
"""

from __future__ import annotations

from typing import Sequence

from ..config import Config
from ..geometry import Rect, union_all
from ..model import PageStats, Rule, Block, RuleOrientation, TextLine, Token


def _separating_rule(
    rules: Sequence[Rule], top: float, bottom: float, block_box: Rect, clearance: float
) -> bool:
    """Is there a horizontal rule in the gap between two lines?

    The rule must also overlap the block horizontally: a rule belonging to a
    different table elsewhere on the page, or to a sidebar, must not split
    content it has nothing to do with.
    """
    lo = top - clearance
    hi = bottom + clearance
    for r in rules:
        if r.orientation is not RuleOrientation.HORIZONTAL:
            continue
        if not (lo <= r.position <= hi):
            continue
        if r.bbox.intersection_width(block_box) <= 0.0:
            continue
        return True
    return False


def build_blocks(
    lines: Sequence[TextLine],
    rules: Sequence[Rule],
    tokens: Sequence[Token],
    stats: PageStats,
    cfg: Config,
) -> list[Block]:
    """Group text lines into blocks."""
    if not lines:
        return []

    pitch = stats.line_pitch
    ordered = sorted(lines, key=lambda ln: ln.sort_key)

    if pitch <= 0.0:
        # No scale, so no scale-free gap threshold exists. Rather than invent
        # one, fall back to rules alone; with no rules either, the whole page is
        # one block. Both outcomes are honest and both are flagged upstream.
        split_gap = float("inf")
        clearance = 0.0
    else:
        split_gap = cfg.f("blocks.split_pitch_factor") * pitch
        clearance = cfg.f("blocks.rule_clearance_pitch_factor") * pitch

    h_rules = [r for r in rules if r.orientation is RuleOrientation.HORIZONTAL]
    left_margin = min(l.bbox.x0 for l in ordered)
    text_width = max(
        (max(l.bbox.x1 for l in ordered) - min(l.bbox.x0 for l in ordered)), 0.0
    )
    prose_share = cfg.f("blocks.prose_min_width_share")
    min_gutter = cfg.f("columns.min_gutter_unit_factor") * (
        stats.within_line_word_gap or 0.0
    )

    blocks: list[Block] = []
    current: list[TextLine] = [ordered[0]]
    current_box = ordered[0].bbox

    def flush(reason: str) -> None:
        nonlocal current, current_box
        blocks.append(
            Block(
                index=len(blocks),
                bbox=union_all([ln.bbox for ln in current]),
                line_indices=tuple(ln.index for ln in current),
                split_reason=reason,
            )
        )
        current = []

    for ln in ordered[1:]:
        gap = ln.bbox.y0 - current_box.y1
        probe = current_box.union(ln.bbox)

        # A line either has column structure or it does not, and where that
        # changes, the block changes. This is the only signal that separates a
        # borderless table from the sentence introducing it: Apple's debt
        # maturity schedule sits 8pt under "The future principal payments ...
        # are as follows (in millions):", well inside the gap threshold and
        # with no rule between them, so the whole table was absorbed into the
        # paragraph, partitioned as one column of prose, and never became a
        # candidate at all. A missing table reports nothing, which makes this
        # the most dangerous failure the pipeline has.
        #
        # The separation is not marginal. On that page the prose lines hold a
        # widest internal gap of 3.1pt while every row of the schedule holds
        # one of 478pt or more, against a 4.95pt gutter threshold.
        structural_change = (
            current
            and _has_column_structure(ln, tokens, min_gutter)
            and not _has_column_structure(current[-1], tokens, min_gutter)
            and text_width > 0.0
            and (current[-1].bbox.width / text_width) >= prose_share
            # ...and anchored to the page's left margin. A paragraph starts at
            # the margin; a WRAPPED CELL starts at its column. Apple's exhibit
            # index is the case that needs this: a description wrapping over
            # three lines is wide and gutter-free, exactly like prose, but it
            # begins 70pt in, at the Description column. Without the anchor
            # test the index split in two at every long description.
            and abs(current[-1].bbox.x0 - left_margin) <= min_gutter
        )

        if _separating_rule(h_rules, current_box.y1, ln.bbox.y0, probe, clearance):
            flush("rule")
        elif gap > split_gap:
            flush("gap")
        elif structural_change:
            flush("structure")

        if not current:
            current = [ln]
            current_box = ln.bbox
        else:
            current.append(ln)
            current_box = current_box.union(ln.bbox)

    flush("page")
    return _rebalance_rule_splits(
        blocks,
        {ln.index: ln for ln in ordered},
        tokens,
        cfg.f("columns.min_gutter_unit_factor") * (stats.within_line_word_gap or 0.0),
    )


def _has_column_structure(line: TextLine, tokens: Sequence[Token], min_gutter: float) -> bool:
    """Does this line hold a gutter-sized gap -- is it a ROW, or one phrase?

    The distinction decides whether a line pulled down across a rule is a
    header row or a spanning label. "2024 2023 2022" has two gutter-wide gaps in
    it and is the column header of the table below. "Years ended" is two words
    at ordinary word spacing sitting above three date columns -- a spanning
    header, which this pipeline cannot yet represent (S4.3 is unbuilt), so
    dragging it into the grid appends it to whichever cell it lands on. That is
    exactly what it did to Apple p32, turning a perfect table's
    "September 30, 2023" into "Years ended September 30, 2023".
    """
    if min_gutter <= 0.0:
        return False
    boxes = sorted(
        (tokens[i].bbox for i in line.token_indices if 0 <= i < len(tokens)),
        key=lambda b: b.x0,
    )
    return any(b.x0 - a.x1 >= min_gutter for a, b in zip(boxes, boxes[1:]))


def _rebalance_rule_splits(
    blocks: list[Block], by_index, tokens: Sequence[Token], min_gutter: float
) -> list[Block]:
    """A ruling line must not override proximity when assigning a line.

    A rule under a column header is drawn BELOW the header, so splitting at it
    puts the header with whatever sits above -- usually the paragraph that
    introduces the table. Apple p38 is the shape: "2024 2023 2022" sits 2.6pt
    above the iPhone row and 6.0pt below the last line of the Note 2 prose, yet
    the three underlines at y=472.5 severed it from its own table and left it
    inside a one-column prose block, where nothing downstream could recover it.
    The extracted table began at "iPhone" and had no years on it.

    So where a RULE made the boundary (a gap-made one is left alone: that is
    proximity already deciding), the last line of the block above is moved down
    when it is nearer to the block below than to the rest of its own block.
    Comparing two gaps needs no threshold and no page constant -- it asks only
    which neighbour the line actually sits with.
    """
    if len(blocks) < 2:
        return blocks

    moved: dict[int, tuple[int, ...]] = {}
    for a, b in zip(blocks, blocks[1:]):
        if a.split_reason != "rule" or len(a.line_indices) < 2:
            continue
        last = by_index.get(a.line_indices[-1])
        prev = by_index.get(a.line_indices[-2])
        first_below = by_index.get(b.line_indices[0])
        if last is None or prev is None or first_below is None:
            continue
        if not _has_column_structure(last, tokens, min_gutter):
            continue
        if first_below.bbox.y0 - last.bbox.y1 < last.bbox.y0 - prev.bbox.y1:
            moved[a.index] = a.line_indices[:-1]
            moved[b.index] = (last.index,) + b.line_indices
    if not moved:
        return blocks

    out: list[Block] = []
    for blk in blocks:
        idxs = moved.get(blk.index, blk.line_indices)
        out.append(
            Block(
                index=blk.index,
                bbox=union_all([by_index[i].bbox for i in idxs]),
                line_indices=idxs,
                split_reason=blk.split_reason,
            )
        )
    return out
