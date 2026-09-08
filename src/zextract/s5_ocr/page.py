"""S5 -- OCR fallback for raster / hybrid pages with no usable text layer.

Digital pages are never re-OCR'd (ARCHITECTURE.md S5). When Tesseract is not
installed the page is annotated ``OCR_UNAVAILABLE`` and the rest of the
pipeline continues with whatever tokens the text layer had -- usually none.
"""

from __future__ import annotations

import shutil

from ..config import Config
from ..model import Page, PageType, Token, TokenSource
from ..s1_intake.tokens import _tokens_from_raw


def maybe_ocr_page(page: Page, src, cfg: Config) -> None:
    """Replace ``page.tokens`` with OCR tokens when the text layer is unusable."""
    if page.page_type is PageType.DIGITAL:
        return
    floor = cfg.i("ocr.min_text_tokens")
    if len(page.tokens) >= floor:
        return
    if shutil.which("tesseract") is None:
        page.notes.append("OCR_UNAVAILABLE")
        return
    tokens = _ocr_tokens(src, cfg)
    if tokens is None:
        page.notes.append("OCR_UNAVAILABLE")
        return
    if not tokens:
        page.notes.append("OCR_EMPTY")
        return
    page.tokens = tokens
    page.notes.append("OCR_TOKENS")
    page.char_count = sum(len(t.text) for t in tokens)


def _ocr_tokens(src, cfg: Config) -> list[Token] | None:
    dpi = cfg.i("ocr.dpi")
    try:
        textpage = src.get_textpage_ocr(dpi=dpi, full=True)
        raw = src.get_text("rawdict", textpage=textpage)
    except Exception:
        return None
    return _tokens_from_raw(raw, source=TokenSource.OCR)
