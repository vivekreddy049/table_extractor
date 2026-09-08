"""S2 -- ruling lines from vector drawing primitives.

Vector-first, not morphological (DECISIONS.md D12). On a digital page the rules
are already in the file as exact coordinates; rasterising 121 pages to recover
them with OpenCV would consume most of the 20-minute budget and be *less*
accurate, because opening a 0.5pt hairline can erase it entirely. The raster
path (morphology on a binarised crop) emits the same ``Rule`` objects, so
everything downstream is shared.

Thresholds are multiples of the page's own ``line_pitch`` (DECISIONS.md D20):
"thin" and "long" mean different absolute things on a dense rating report and a
sparse annexure, but the same thing relative to line spacing.
"""

from __future__ import annotations

import pymupdf

from ..config import Config
from ..geometry import Rect
from ..model import PageStats, Rule, RuleOrientation


def _classify(
    x0: float, y0: float, x1: float, y1: float, stroke: float, max_thick: float, min_len: float
) -> Rule | None:
    if x1 < x0:
        x0, x1 = x1, x0
    if y1 < y0:
        y0, y1 = y1, y0
    w, h = x1 - x0, y1 - y0

    # A stroked line has zero extent across its own direction; give it the
    # stroke width so the box is non-degenerate and the thickness is honest.
    if h <= max_thick and w >= min_len:
        thickness = max(h, stroke)
        pad = 0.5 * max(0.0, thickness - h)
        return Rule(
            orientation=RuleOrientation.HORIZONTAL,
            bbox=Rect(x0, y0 - pad, x1, y1 + pad),
            thickness=thickness,
        )
    if w <= max_thick and h >= min_len:
        thickness = max(w, stroke)
        pad = 0.5 * max(0.0, thickness - w)
        return Rule(
            orientation=RuleOrientation.VERTICAL,
            bbox=Rect(x0 - pad, y0, x1 + pad, y1),
            thickness=thickness,
        )
    return None


def _harvest(page: pymupdf.Page, max_thick: float, min_len: float) -> list[Rule]:
    out: list[Rule] = []
    for d in page.get_drawings():
        stroke = float(d.get("width") or 0.0)
        for item in d.get("items", []):
            kind = item[0]
            if kind == "re":
                r = item[1]
                got = _classify(r.x0, r.y0, r.x1, r.y1, stroke, max_thick, min_len)
            elif kind == "l":
                a, b = item[1], item[2]
                got = _classify(a.x, a.y, b.x, b.y, stroke, max_thick, min_len)
            else:
                # Curves and quads are not rules. A "curve" that happens to be
                # straight is a producer quirk we deliberately ignore rather
                # than chase: it costs a rule, not a wrong rule.
                continue
            if got is not None:
                out.append(got)
    return out


def _merge(rules: list[Rule], merge_tol: float, dedupe_tol: float) -> list[Rule]:
    """Merge collinear segments, then collapse near-duplicate rules.

    Producers routinely emit one table rule as a dozen abutting segments (one
    per cell), and emit a box border twice (once per adjoining cell). Both have
    to collapse before rule counts mean anything.
    """
    merged: list[Rule] = []
    for orientation in (RuleOrientation.HORIZONTAL, RuleOrientation.VERTICAL):
        group = [r for r in rules if r.orientation is orientation]
        if not group:
            continue
        horizontal = orientation is RuleOrientation.HORIZONTAL

        # Bucket by the coordinate the rule sits at.
        group.sort(key=lambda r: (r.position, r.bbox.x0, r.bbox.y0))
        buckets: list[list[Rule]] = []
        for r in group:
            if buckets and abs(r.position - buckets[-1][-1].position) <= merge_tol:
                buckets[-1].append(r)
            else:
                buckets.append([r])

        for bucket in buckets:
            # Merge along the rule's own direction.
            spans = sorted(
                (
                    (r.bbox.x0, r.bbox.x1, r) if horizontal else (r.bbox.y0, r.bbox.y1, r)
                    for r in bucket
                ),
                key=lambda s: (s[0], s[1]),
            )
            cur_lo, cur_hi, cur_r = spans[0]
            acc: list[tuple[float, float, Rule]] = []
            for lo, hi, r in spans[1:]:
                if lo <= cur_hi + merge_tol:
                    cur_hi = max(cur_hi, hi)
                    if r.thickness > cur_r.thickness:
                        cur_r = r
                else:
                    acc.append((cur_lo, cur_hi, cur_r))
                    cur_lo, cur_hi, cur_r = lo, hi, r
            acc.append((cur_lo, cur_hi, cur_r))

            pos = sum(r.position for r in bucket) / len(bucket)
            half = 0.5 * max(r.thickness for r in bucket)
            for lo, hi, r in acc:
                bbox = (
                    Rect(lo, pos - half, hi, pos + half)
                    if horizontal
                    else Rect(pos - half, lo, pos + half, hi)
                )
                merged.append(Rule(orientation=orientation, bbox=bbox, thickness=r.thickness))

    # Collapse rules that survived as near-duplicates (a border drawn twice by
    # two adjoining cells, offset by a rounding error).
    merged.sort(key=lambda r: (r.orientation.value, r.position, r.bbox.x0, r.bbox.y0))
    out: list[Rule] = []
    for r in merged:
        dup = False
        for k in out:
            if k.orientation is not r.orientation:
                continue
            if abs(k.position - r.position) > dedupe_tol:
                continue
            if r.bbox.iou(k.bbox) > 0.0 or _spans_overlap(k, r):
                dup = True
                break
        if not dup:
            out.append(r)
    return out


def _spans_overlap(a: Rule, b: Rule) -> bool:
    if a.orientation is RuleOrientation.HORIZONTAL:
        return a.bbox.intersection_width(b.bbox) > 0.0
    return a.bbox.intersection_height(b.bbox) > 0.0



def _link_underline_rects(page: pymupdf.Page) -> list[Rect]:
    """Rectangles of this page's link annotations.

    Authoritative, not a guess about appearance: these come from the PDF's own
    /Annots, so a line that sits under linked text is identified by what the
    document declares, never by colour or position heuristics.
    """
    out: list[Rect] = []
    for link in page.get_links():
        r = link.get("from")
        if r is not None:
            out.append(Rect(r.x0, r.y0, r.x1, r.y1))
    return out


def _drop_link_underlines(
    rules: list[Rule], links: list[Rect], y_tol: float, x_tol: float
) -> list[Rule]:
    """Discard horizontal rules that are hyperlink underlines, not table rules.

    A 10-K's exhibit index links every description to its filing, and each link
    is drawn with an underline. Those underlines are geometrically
    indistinguishable from a hairline table rule -- thin, horizontal, spanning
    a text run -- so the vector harvest picks them up and the block builder
    then splits EVERY row of the index into its own block. Measured on Apple
    p58: 48 of 58 harvested horizontal rules (83%) sit exactly on a link's
    bottom edge, all 51 blocks came out with split_reason="rule", and the
    exhibit index shredded into 22 candidates that the run-growing merge test
    could not put back together.

    The test requires the rule to be CONTAINED within the link's x-span, not
    merely to overlap it. An underline never extends past the text it
    underlines, whereas a genuine table rule spans the table -- far wider than
    any single linked phrase -- so containment is what keeps a real rule that
    happens to pass near a link from being dropped.
    """
    if not links:
        return rules
    kept: list[Rule] = []
    for r in rules:
        if r.orientation is not RuleOrientation.HORIZONTAL:
            kept.append(r)
            continue
        underline = any(
            abs(r.position - lr.y1) <= y_tol
            and r.bbox.x0 >= lr.x0 - x_tol
            and r.bbox.x1 <= lr.x1 + x_tol
            for lr in links
        )
        if not underline:
            kept.append(r)
    return kept


def extract_rules(page: pymupdf.Page, stats: PageStats, cfg: Config) -> list[Rule]:
    """Ruling lines for one page, merged and deduplicated.

    Returns [] when the page has no usable ``line_pitch``: without a scale there
    is no scale-free way to say what counts as thin or long, and inventing one
    would be the absolute constant D20 forbids. A page in that state is already
    flagged LOW_CONFIDENCE_LAYOUT.
    """
    pitch = stats.line_pitch
    if pitch <= 0.0:
        return []

    max_thick = cfg.f("rules.max_thickness_pitch_factor") * pitch
    min_len = cfg.f("rules.min_length_pitch_factor") * pitch
    merge_tol = cfg.f("rules.merge_tolerance_pitch_factor") * pitch
    dedupe_tol = cfg.f("rules.dedupe_pitch_factor") * pitch

    raw = _harvest(page, max_thick, min_len)
    # Before merging: an underline fragment must not be welded onto a real rule.
    raw = _drop_link_underlines(
        raw,
        _link_underline_rects(page),
        cfg.f("rules.link_underline_y_tol_pitch_factor") * pitch,
        cfg.f("rules.link_underline_x_tol_pitch_factor") * pitch,
    )
    merged = _merge(raw, merge_tol, dedupe_tol)
    merged = [r for r in merged if r.length >= min_len]
    merged.sort(key=lambda r: (r.orientation.value, r.position, r.bbox.x0, r.bbox.y0))
    return merged
