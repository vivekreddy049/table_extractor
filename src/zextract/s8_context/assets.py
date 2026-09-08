"""S8 -- figures, furniture-image exclusion, table→figure association.

Repeating logos (same xref, near-identical bbox, on a large share of pages)
are page furniture, not assets (ARCHITECTURE.md F6). A unique image is a
figure. Association is a scored sum, never a silent assertion.
"""

from __future__ import annotations

from collections import defaultdict

import pymupdf

from ..config import Config
from ..geometry import Rect
from ..model import Asset, AssetLink, Document, Page


def harvest_assets(doc: Document, handle: pymupdf.Document, cfg: Config) -> None:
    """Populate ``doc.assets`` with non-furniture images."""
    min_area = cfg.f("assets.min_area_share")
    occs: list[tuple[int, int, Rect]] = []  # page_no, xref, bbox
    for page in doc.pages:
        src = handle[page.page_no - 1]
        page_area = page.width * page.height
        if page_area <= 0.0:
            continue
        try:
            infos = src.get_images(full=True)
        except Exception:
            continue
        for info in infos:
            xref = int(info[0])
            try:
                rects = src.get_image_rects(xref)
            except Exception:
                continue
            for r in rects:
                box = Rect(float(r.x0), float(r.y0), float(r.x1), float(r.y1))
                if box.area < min_area * page_area:
                    continue
                occs.append((page.page_no, xref, box))

    furniture_xrefs = _furniture_xrefs(occs, len(doc.pages), cfg)
    for xref in sorted(furniture_xrefs):
        doc.notes.append(f"FURNITURE_IMAGE_XREF:{xref}")

    assets: list[Asset] = []
    for page_no, xref, box in occs:
        if xref in furniture_xrefs:
            continue
        assets.append(
            Asset(
                index=len(assets),
                page_no=page_no,
                kind="figure",
                bbox=box,
                xref=xref,
            )
        )
    by_page = {p.page_no: p for p in doc.pages}
    for asset in assets:
        page = by_page.get(asset.page_no)
        if page is not None:
            asset.caption = _nearest_caption(page, asset.bbox)
    doc.assets = assets


def _furniture_xrefs(
    occs: list[tuple[int, int, Rect]], n_pages: int, cfg: Config
) -> set[int]:
    if n_pages < cfg.i("assets.min_pages"):
        return set()
    threshold = cfg.f("assets.min_page_share") * n_pages
    iou_min = cfg.f("assets.furniture_iou")
    by_xref: dict[int, list[tuple[int, Rect]]] = defaultdict(list)
    for page_no, xref, box in occs:
        by_xref[xref].append((page_no, box))

    furniture: set[int] = set()
    for xref, members in by_xref.items():
        # Same xref at a stable bbox across pages = a logo template.
        pages: set[int] = set()
        for i, (p_i, b_i) in enumerate(members):
            for p_j, b_j in members[i:]:
                if b_i.iou(b_j) >= iou_min:
                    pages.add(p_i)
                    pages.add(p_j)
        if len(pages) >= threshold:
            furniture.add(xref)
    return furniture


def _nearest_caption(page: Page, box: Rect) -> str | None:
    """A short line sitting just under (else just over) the image."""
    pitch = page.stats.line_pitch if page.stats and page.stats.line_pitch > 0.0 else 1.0
    best = None
    best_gap = pitch * 3.0
    for ln in page.lines:
        if ln.index in page.furniture_line_indices:
            continue
        text = " ".join(ln.text.split())
        if not text or len(text.split()) > 20:
            continue
        if ln.bbox.y0 >= box.y1:
            gap = ln.bbox.y0 - box.y1
        elif ln.bbox.y1 <= box.y0:
            gap = box.y0 - ln.bbox.y1
        else:
            continue
        if 0.0 <= gap < best_gap:
            best_gap = gap
            best = text
    return best


def associate_assets(doc: Document, cfg: Config) -> None:
    """Score table↔figure links. Weak associations stay weak."""
    adj = cfg.f("assets.adjacency_pitch_factor")
    by_page: dict[int, list[Asset]] = defaultdict(list)
    for a in doc.assets:
        by_page[a.page_no].append(a)

    for page in doc.pages:
        pitch = page.stats.line_pitch if page.stats and page.stats.line_pitch > 0.0 else 1.0
        nearby = by_page.get(page.page_no, [])
        for table in page.tables:
            if not table.accepted or not nearby:
                continue
            links: list[AssetLink] = []
            for asset in nearby:
                score = 0.40  # same page
                gap = max(0.0, asset.bbox.y0 - table.bbox.y1)
                if gap == 0.0:
                    gap = max(0.0, table.bbox.y0 - asset.bbox.y1)
                if gap <= adj * pitch:
                    score += 0.10
                relation = None
                if score >= 0.55:
                    relation = "visualises"
                elif score >= 0.35:
                    relation = "co-located"
                if relation:
                    links.append(AssetLink(asset.index, relation, round(score, 4)))
            links.sort(key=lambda x: (-x.confidence, x.asset_index))
            table.asset_links = links
