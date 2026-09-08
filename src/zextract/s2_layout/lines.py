"""S2 -- tokens into text lines.

Grouping is geometric: a token joins the line it overlaps vertically. We do not
use the producer's own line structure, because the producer's structure encodes
its content stream and the content stream lies. Shell.pdf p2 emits the whole
profit-and-loss body before the header row, since Excel's print driver writes
frozen panes last (ARCHITECTURE.md F4). Anything that trusts that order promotes
a data row to a column header.

A line here may legitimately span several table columns. That is not a bug to be
fixed downstream -- it is precisely what makes a line usable as row evidence
when S3 grows tables from bands of consistent column structure.
"""

from __future__ import annotations

from typing import Sequence

from ..config import Config
from ..geometry import Rect, union_all, vertical_overlap_ratio
from ..model import PageStats, TextLine, Token


def build_lines(tokens: Sequence[Token], stats: PageStats, cfg: Config) -> list[TextLine]:
    """Cluster tokens into text lines by vertical overlap.

    Tokens are consumed in geometric order (top-to-bottom, then left-to-right,
    then content-stream order as a total tie-break), so the result does not
    depend on the order the producer happened to emit them in.
    """
    if not tokens:
        return []

    min_overlap = cfg.f("lines.min_vertical_overlap")
    max_offset_factor = cfg.f("lines.max_centre_offset_pitch_factor")
    pitch = stats.line_pitch
    # With no usable pitch the centre-offset guard cannot be expressed in a
    # scale-free way, so it is disabled and overlap alone decides. The page is
    # already flagged; this degrades, it does not fail (ARCHITECTURE.md 9.5).
    max_offset = max_offset_factor * pitch if pitch > 0.0 else float("inf")

    order = sorted(range(len(tokens)), key=lambda i: tokens[i].sort_key)

    # Open clusters, as (bbox, [token indices]). A cluster closes once the
    # scan line has passed below it, which keeps the search local.
    open_boxes: list[Rect] = []
    open_members: list[list[int]] = []
    closed: list[tuple[Rect, list[int]]] = []

    for ti in order:
        tok = tokens[ti]
        tb = tok.bbox

        # Retire clusters that can no longer receive a token: their bottom edge
        # is above this token's top edge, and tokens only move downward.
        still_open_boxes: list[Rect] = []
        still_open_members: list[list[int]] = []
        for bx, mem in zip(open_boxes, open_members):
            if bx.y1 < tb.y0:
                closed.append((bx, mem))
            else:
                still_open_boxes.append(bx)
                still_open_members.append(mem)
        open_boxes, open_members = still_open_boxes, still_open_members

        best_idx = -1
        best_overlap = 0.0
        for li, bx in enumerate(open_boxes):
            ov = vertical_overlap_ratio(tb, bx)
            if ov < min_overlap:
                continue
            if abs(tb.cy - bx.cy) > max_offset:
                # A tall token (a heading, a bracket spanning two rows) can
                # overlap a line generously without belonging to it. The centre
                # test is what stops it swallowing its neighbours.
                continue
            if ov > best_overlap:
                best_overlap = ov
                best_idx = li
            # Strict > keeps the earliest-created cluster on a tie, which is
            # what makes the assignment deterministic.

        if best_idx >= 0:
            open_boxes[best_idx] = open_boxes[best_idx].union(tb)
            open_members[best_idx].append(ti)
        else:
            open_boxes.append(tb)
            open_members.append([ti])

    closed.extend(zip(open_boxes, open_members))

    # Emit in reading order. The index assigned here is the line's identity for
    # the rest of the pipeline, so it must come from geometry, not from the
    # order clusters happened to close in.
    closed.sort(key=lambda c: (c[0].y0, c[0].x0, min(c[1])))

    out: list[TextLine] = []
    for idx, (bx, members) in enumerate(closed):
        members_sorted = sorted(members, key=lambda i: (tokens[i].bbox.x0, tokens[i].order))
        text = " ".join(tokens[i].text for i in members_sorted)
        out.append(
            TextLine(
                index=idx,
                bbox=union_all([tokens[i].bbox for i in members_sorted]),
                token_indices=tuple(members_sorted),
                text=text,
            )
        )
    return out
