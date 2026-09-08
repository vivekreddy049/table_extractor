"""S6 -- page-level tables into logical tables.

Two accepted tables merge when a signature match AND a continuity check both
pass (ARCHITECTURE.md S6). Signature is column count plus normalised right-edge
vector. Continuity is: consecutive pages, first table on the later page, and the region
sitting near the document's own top-of-content (not the physical page edge --
see ``_continuity``). A repeated header row is dropped, not
duplicated into the data.

When only one of the two checks passes the tables stay separate and both carry
``STITCH_AMBIGUOUS`` -- two correct tables beat one wrong one.
"""

from __future__ import annotations

from ..config import Config
from ..model import Document, Issue, Page, Row, Table


def stitch_document(doc: Document, cfg: Config) -> None:
    content_top = _content_top(doc)
    for i in range(len(doc.pages) - 1):
        left_page = doc.pages[i]
        right_page = doc.pages[i + 1]
        lefts = [t for t in left_page.tables if t.accepted]
        rights = [t for t in right_page.tables if t.accepted]
        if not lefts or not rights:
            continue
        a = lefts[-1]
        b = rights[0]
        sig = _signature_match(a, b, left_page, right_page, cfg)
        cont = _continuity(b, right_page, rights, content_top, cfg)
        if sig and cont:
            _absorb(a, b, cfg)
            continue
        if sig or cont:
            _ambiguous(a, "signature matched, continuity failed" if sig else "continuity matched, signature failed")
            _ambiguous(b, "paired with previous page")


def _signature_match(
    a: Table, b: Table, pa: Page, pb: Page, cfg: Config
) -> bool:
    if len(a.columns) != len(b.columns) or not a.columns:
        return False
    tol = cfg.f("stitch.max_edge_share")
    wa = pa.width if pa.width > 0.0 else 1.0
    wb = pb.width if pb.width > 0.0 else 1.0
    for ca, cb in zip(a.columns, b.columns):
        if abs((ca.x1 / wa) - (cb.x1 / wb)) > tol:
            return False
    return True


def _content_top(doc: Document) -> float:
    """Where this document actually starts its body content, in points.

    Median of each page's first block top, over the document. This is a
    page-derived statistic like any other (D20): it is measured from the
    document in hand, not assumed.
    """
    tops = sorted(p.blocks[0].bbox.y0 for p in doc.pages if p.blocks)
    if not tops:
        return 0.0
    mid = len(tops) // 2
    return tops[mid] if len(tops) % 2 else 0.5 * (tops[mid - 1] + tops[mid])


def _continuity(
    b: Table, page: Page, accepted: list[Table], content_top: float, cfg: Config
) -> bool:
    """Does this table sit at the top of its page, where a continuation would?

    Measured from the document's OWN top-of-content, not from the physical page
    edge. The distinction is the whole rule: real pages carry a top margin and
    usually a running header, so body content starts 78pt down in the Apple 10-K
    and 88pt down in the ICRA report. Comparing against the page edge with a
    threshold of ~1.5 line pitches (16-21pt) meant the gate could never open --
    measured at 0 of 121 Apple pages and 0 of 7 MRPL pages. Stitching passed its
    own unit test, whose fixture places the continuation at y=20, and was dead
    code on every real document.
    """
    if not accepted or accepted[0] is not b:
        return False
    pitch = page.stats.line_pitch if page.stats and page.stats.line_pitch > 0.0 else 1.0
    max_top = content_top + cfg.f("stitch.top_margin_pitch_factor") * pitch
    return b.bbox.y0 <= max_top


def _row_text(table: Table, row_idx: int) -> tuple[str, ...]:
    return tuple(
        (c.raw_text.strip() for c in sorted(
            (x for x in table.cells if x.row_idx == row_idx),
            key=lambda x: x.col_idx,
        ))
    )


def _jaccard(a: tuple[str, ...], b: tuple[str, ...]) -> float:
    sa, sb = set(x.casefold() for x in a if x), set(x.casefold() for x in b if x)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def _header_skip(left: Table, right: Table, cfg: Config) -> int:
    """How many leading rows of ``right`` repeat ``left``'s header."""
    rows = sorted({c.row_idx for c in left.cells if c.is_header})
    if not rows:
        rows = [0] if left.rows else []
    skip = 0
    thresh = cfg.f("stitch.header_similarity")
    for i, r in enumerate(rows):
        if i >= len(right.rows):
            break
        if _jaccard(_row_text(left, r), _row_text(right, i)) >= thresh:
            skip += 1
        else:
            break
    return skip


def _absorb(left: Table, right: Table, cfg: Config) -> None:
    skip = _header_skip(left, right, cfg)
    offset = len(left.rows)
    kept = right.rows[skip:]
    for i, row in enumerate(kept):
        left.rows.append(
            Row(
                index=offset + i,
                bbox=row.bbox,
                line_indices=row.line_indices,
                flags=row.flags + ("STITCH_BOUNDARY",) if i == 0 else row.flags,
            )
        )
    for cell in right.cells:
        if cell.row_idx < skip:
            continue
        cell.row_idx = cell.row_idx - skip + offset
        left.cells.append(cell)
    left.end_page = right.page_no
    left.flags.append("STITCHED")
    right.rejected_as = "STITCHED_INTO_PRIOR"
    right.is_continuation = True
    right.flags.append("STITCHED_INTO_PRIOR")


def _ambiguous(table: Table, why: str) -> None:
    table.flags.append("STITCH_AMBIGUOUS")
    table.issues.append(
        Issue(
            severity="warning",
            code="STITCH_AMBIGUOUS",
            message=why,
        )
    )
