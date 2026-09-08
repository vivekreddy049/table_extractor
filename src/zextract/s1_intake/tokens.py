"""S1 -- word tokens from the text layer, with typography.

Words are assembled from per-character boxes (``rawdict``) rather than taken
from PyMuPDF's ``get_text("words")``, for two reasons:

  * ``words`` carries no font, size or weight, and font weight is a header
    signal we want available for free (ARCHITECTURE.md F8);
  * character boxes let us split on whitespace ourselves, so a producer that
    emits a whole line as one "word" -- or one glyph per word -- is handled the
    same way.

We do not re-OCR anything the text layer already holds exactly (S5).
"""

from __future__ import annotations

import math
import unicodedata

import pymupdf

from ..geometry import Rect
from ..model import Token, TokenSource

# PyMuPDF span flag bits.
_FLAG_ITALIC = 1 << 1
_FLAG_BOLD = 1 << 4


def _is_word_break(ch: str) -> bool:
    if not ch:
        return True
    if ch.isspace():
        return True
    # U+00A0 NBSP and friends are whitespace for layout but not for str.isspace
    # in every case; normalise the decision through the Unicode category.
    return unicodedata.category(ch) == "Zs"


def _bold_from(font: str, flags: int) -> bool:
    if flags & _FLAG_BOLD:
        return True
    lowered = font.lower()
    return "bold" in lowered or "black" in lowered or "heavy" in lowered


def _italic_from(font: str, flags: int) -> bool:
    if flags & _FLAG_ITALIC:
        return True
    lowered = font.lower()
    return "italic" in lowered or "oblique" in lowered


def read_page_text(page: pymupdf.Page) -> tuple[list[Token], float | None]:
    """Tokens and writing direction from a single ``rawdict`` parse.

    ``rawdict`` walks every glyph on the page and is the most expensive call in
    S1; parsing it once for both answers roughly halves intake cost on a
    121-page document.
    """
    raw = page.get_text("rawdict", sort=False)
    return (_tokens_from_raw(raw), _direction_from_raw(raw))


def text_layer_direction(page: pymupdf.Page) -> float | None:
    """Writing-direction angle in degrees from the text layer, or None.

    On a digital page the writing direction is *known*: PyMuPDF reports each
    line's ``dir`` as a unit vector taken straight from the text matrix. Docstrum
    infers direction from neighbour angles because it was designed for connected
    components on a scan, where nothing else is available -- but inference is
    strictly worse when the truth is in the file, and it fails in a specific,
    predictable way. On a sparse financial table (Apple p32: 176 tokens, wide
    column gutters, 10pt line pitch) a number's nearest neighbours are the
    numbers above and below it, so vertical pairs outnumber horizontal ones and
    the inferred direction comes back as -90 on a perfectly upright page.

    So: read it here when there is a text layer, and fall back to inference only
    on raster pages, which is what Docstrum was for in the first place.

    Lines are weighted by character count, so a rotated watermark or a single
    vertical axis label cannot outvote the body text.
    """
    return _direction_from_raw(page.get_text("rawdict", sort=False))


def _direction_from_raw(raw: dict) -> float | None:
    weights: dict[float, int] = {}
    for block in raw.get("blocks", []):
        if block.get("type", 0) != 0:
            continue
        for line in block.get("lines", []):
            d = line.get("dir")
            if not d:
                continue
            cos_t, sin_t = float(d[0]), float(d[1])
            if cos_t == 0.0 and sin_t == 0.0:
                continue
            ang = math.degrees(math.atan2(sin_t, cos_t))
            while ang >= 90.0:
                ang -= 180.0
            while ang < -90.0:
                ang += 180.0
            n_chars = sum(len(s.get("chars", [])) for s in line.get("spans", []))
            if n_chars <= 0:
                continue
            key = round(ang, 3)
            weights[key] = weights.get(key, 0) + n_chars

    if not weights:
        return None
    # Sort by (-weight, angle) so ties resolve to the smaller angle and the
    # result cannot depend on dict insertion order.
    return sorted(weights.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


def extract_tokens(page: pymupdf.Page) -> list[Token]:
    """Extract word tokens in content-stream order.

    The returned order is the producer's order. It is preserved on ``Token.order``
    purely as a deterministic tie-break; no stage infers structure from it.
    """
    return _tokens_from_raw(page.get_text("rawdict", sort=False))


def _tokens_from_raw(raw: dict, source: TokenSource | None = None) -> list[Token]:
    tokens: list[Token] = []
    order = 0
    src = source if source is not None else TokenSource.TEXT_LAYER

    for block in raw.get("blocks", []):
        if block.get("type", 0) != 0:  # 0 = text, 1 = image
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                font = str(span.get("font", ""))
                size = float(span.get("size", 0.0))
                flags = int(span.get("flags", 0))
                color = int(span.get("color", 0))
                bold = _bold_from(font, flags)
                italic = _italic_from(font, flags)

                chars: list[str] = []
                boxes: list[Rect] = []

                def flush() -> None:
                    nonlocal order, chars, boxes
                    if not chars or not boxes:
                        chars, boxes = [], []
                        return
                    text = "".join(chars)
                    if text.strip():
                        x0 = min(b.x0 for b in boxes)
                        y0 = min(b.y0 for b in boxes)
                        x1 = max(b.x1 for b in boxes)
                        y1 = max(b.y1 for b in boxes)
                        if x1 > x0 and y1 > y0:
                            tokens.append(
                                Token(
                                    text=text,
                                    bbox=Rect(x0, y0, x1, y1),
                                    font=font,
                                    size=size,
                                    bold=bold,
                                    italic=italic,
                                    color=color,
                                    order=order,
                                    source=src,
                                )
                            )
                            order += 1
                    chars, boxes = [], []

                for c in span.get("chars", []):
                    ch = c.get("c", "")
                    if _is_word_break(ch):
                        flush()
                        continue
                    bx = c.get("bbox")
                    if not bx:
                        continue
                    x0, y0, x1, y1 = (float(v) for v in bx)
                    if x1 < x0:
                        x0, x1 = x1, x0
                    if y1 < y0:
                        y0, y1 = y1, y0
                    chars.append(ch)
                    boxes.append(Rect(x0, y0, x1, y1))
                flush()

    return tokens
