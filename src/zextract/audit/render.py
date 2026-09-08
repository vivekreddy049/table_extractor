"""S11 -- debug overlay renderer.

Built now, at stage 2, rather than at the end. Every bug from here on is a
geometry bug, and a geometry bug you can only read as numbers costs an order of
magnitude more to find than one you can see. The brief says the live review will
use this renderer; so will we, well before then.

Deterministic by construction: fixed DPI, fixed colours, no timestamps, no
antialiasing decisions left to the platform. Two runs produce byte-identical
PNGs.
"""

from __future__ import annotations

from pathlib import Path

import pymupdf
from PIL import Image, ImageDraw

from ..config import Config
from ..geometry import Rect
from ..model import Page, RuleOrientation

# RGBA, fixed. Order of drawing is fixed too, so overlaps composite identically
# on every run.
_C_TOKEN = (120, 120, 120, 90)
_C_LINE = (0, 120, 220, 160)
_C_BLOCK_GAP = (40, 160, 60, 220)
_C_BLOCK_RULE = (200, 100, 0, 220)
_C_BLOCK_PAGE = (140, 140, 140, 200)
_C_HRULE = (220, 30, 30, 230)
_C_VRULE = (150, 30, 200, 230)
_C_LABEL = (20, 20, 20, 255)
_C_PANEL = (255, 255, 255, 215)

_BLOCK_COLOURS = {"gap": _C_BLOCK_GAP, "rule": _C_BLOCK_RULE, "page": _C_BLOCK_PAGE}


def _scaled(r: Rect, s: float) -> tuple[float, float, float, float]:
    return (r.x0 * s, r.y0 * s, r.x1 * s, r.y1 * s)


def render_page(
    pdf_path: Path | str, page: Page, cfg: Config, out_path: Path | str
) -> Path:
    """Write a PNG of one page with the S1/S2 findings overlaid.

    Layers, bottom to top: the rendered page, token boxes (grey), text lines
    (blue), ruling lines (red horizontal / purple vertical), block boundaries
    (coloured by why the block ended), and a statistics panel.
    """
    dpi = cfg.f("render.dpi")
    scale = dpi / 72.0

    doc = pymupdf.open(str(pdf_path))
    try:
        src = doc[page.page_no - 1]
        pix = src.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
        base = Image.frombytes("RGB", (pix.width, pix.height), pix.samples).convert("RGBA")
    finally:
        doc.close()

    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)

    for t in page.tokens:
        d.rectangle(_scaled(t.bbox, scale), outline=_C_TOKEN, width=1)

    for ln in page.lines:
        d.rectangle(_scaled(ln.bbox, scale), outline=_C_LINE, width=1)

    for r in page.rules:
        colour = _C_HRULE if r.orientation is RuleOrientation.HORIZONTAL else _C_VRULE
        x0, y0, x1, y1 = _scaled(r.bbox, scale)
        # Rules are thin; draw a visible stroke down their centre line rather
        # than an outline nobody can see.
        if r.orientation is RuleOrientation.HORIZONTAL:
            mid = 0.5 * (y0 + y1)
            d.line((x0, mid, x1, mid), fill=colour, width=2)
        else:
            mid = 0.5 * (x0 + x1)
            d.line((mid, y0, mid, y1), fill=colour, width=2)

    for b in page.blocks:
        colour = _BLOCK_COLOURS.get(b.split_reason, _C_BLOCK_PAGE)
        d.rectangle(_scaled(b.bbox, scale), outline=colour, width=2)
        x0, y0, _, _ = _scaled(b.bbox, scale)
        d.text((x0 + 2, y0 + 1), f"B{b.index}:{b.split_reason}", fill=colour)

    _draw_panel(d, page, base.size)

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    Image.alpha_composite(base, overlay).convert("RGB").save(
        out, format="PNG", optimize=False, compress_level=6
    )
    return out


def _draw_panel(d: ImageDraw.ImageDraw, page: Page, size: tuple[int, int]) -> None:
    s = page.stats
    rows = [
        f"page {page.page_no}  type={page.page_type.value}  rot={page.rotation}",
        f"tokens={len(page.tokens)}  lines={len(page.lines)}  blocks={len(page.blocks)}",
        f"rules h={len(page.horizontal_rules())} v={len(page.vertical_rules())}",
    ]
    if s is not None:
        rows.append(f"pitch={s.line_pitch:.2f}  word_gap={s.within_line_word_gap:.2f}")
        rows.append(f"modal_size={s.modal_font_size:.2f}  angle={s.text_angle_deg:.2f}")
        rows.append(f"stats_source={s.source}  stable={s.stable}")
    if page.notes:
        rows.append("notes: " + ",".join(page.notes[:3]))

    pad, lh = 6, 13
    w = 8 + max(len(r) for r in rows) * 6
    h = 2 * pad + lh * len(rows)
    d.rectangle((4, 4, 4 + w, 4 + h), fill=_C_PANEL, outline=(0, 0, 0, 200), width=1)
    for i, r in enumerate(rows):
        d.text((4 + pad, 4 + pad + i * lh), r, fill=_C_LABEL)
