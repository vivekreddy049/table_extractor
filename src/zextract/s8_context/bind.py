"""S8 -- title, caption, section path, unit/scale.

A thin, geometric pass. Footnote binding is not built; figures live in
``assets.py``. Every rule is a document-general pattern: named prefixes
(Table / Exhibit / Annexure / Schedule), bold-and-short, a unit/scale
regex, inheritance from the nearest preceding declaration. No string from
any sample document is used as a detector.
"""

from __future__ import annotations

import re

from ..config import Config
from ..model import Document, Page, Table, TextLine, Token

# Generic catalogue names, not sample-specific titles.
_TITLE_PREFIX = re.compile(
    r"^(table|exhibit|annexure|schedule|statement|consolidated)\b",
    re.IGNORECASE,
)
_SECTION_PREFIX = re.compile(r"^(item|part)\s+[\dIVXivx]+", re.IGNORECASE)
_CAPTION_PREFIX = re.compile(r"^(source|note|notes)\b", re.IGNORECASE)
# Currency glyph includes backtick: some print drivers encode ₹ that way (F9).
_UNIT = re.compile(
    r"(?:"
    r"in\s+(?:millions|thousands|billions)"
    r"|dollars\s+in\s+millions"
    r"|\(?(?:rs\.?|inr|usd|[$€£¥₹`])\s*(?:million|billion|crore|lakh|mn|bn)s?\)?"
    r"|\((?:million|crore|lakh)s?\)"
    r"|(?:million|crore|lakh)s?"
    r")",
    re.IGNORECASE,
)
_WS = re.compile(r"\s+")


def _clean(text: str) -> str:
    return _WS.sub(" ", (text or "").strip())


def _tokens(page: Page, line: TextLine) -> list[Token]:
    return [page.tokens[i] for i in line.token_indices if 0 <= i < len(page.tokens)]


def _line_bold(page: Page, line: TextLine) -> bool:
    toks = _tokens(page, line)
    if not toks:
        return False
    return sum(1 for t in toks if t.bold) * 2 >= len(toks)


def _line_italic(page: Page, line: TextLine) -> bool:
    toks = _tokens(page, line)
    if not toks:
        return False
    return sum(1 for t in toks if t.italic) * 2 >= len(toks)


def _line_size(page: Page, line: TextLine) -> float:
    toks = _tokens(page, line)
    return max(t.size for t in toks) if toks else 0.0


def _word_count(text: str) -> int:
    return len(_clean(text).split()) if _clean(text) else 0


def _pitch(page: Page) -> float:
    return page.stats.line_pitch if page.stats and page.stats.line_pitch > 0.0 else 1.0


def _modal(page: Page) -> float:
    return page.stats.modal_font_size if page.stats else 0.0


def _is_furniture(page: Page, line: TextLine) -> bool:
    return line.index in page.furniture_line_indices


def _all_caps(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    return bool(letters) and all(c.isupper() for c in letters)


def _is_title_line(page: Page, line: TextLine, cfg: Config) -> bool:
    text = _clean(line.text)
    if not text or _word_count(text) > cfg.i("context.max_title_words"):
        return False
    if _TITLE_PREFIX.match(text):
        return True
    size = _line_size(page, line)
    modal = _modal(page)
    bigger = modal > 0.0 and size >= modal * cfg.f("context.heading_size_factor")
    if _line_bold(page, line) and (_word_count(text) <= 12 or bigger):
        return True
    if _all_caps(text) and _word_count(text) >= 2:
        return True
    return False


def _is_section_line(page: Page, line: TextLine, cfg: Config) -> bool:
    text = _clean(line.text)
    if not text:
        return False
    if _SECTION_PREFIX.match(text):
        return True
    return _is_title_line(page, line, cfg)


def _scan_unit(text: str) -> str | None:
    m = _UNIT.search(text or "")
    if not m:
        return None
    return _clean(m.group(0))


def _lines_above(page: Page, table: Table) -> list[TextLine]:
    return sorted(
        (ln for ln in page.lines if ln.bbox.y1 <= table.bbox.y0 + 0.0),
        key=lambda ln: ln.bbox.y0,
    )


def _lines_below(page: Page, table: Table) -> list[TextLine]:
    return sorted(
        (ln for ln in page.lines if ln.bbox.y0 >= table.bbox.y1),
        key=lambda ln: ln.bbox.y0,
    )


def _bind_title(page: Page, table: Table, cfg: Config) -> str | None:
    pitch = _pitch(page)
    max_gap = cfg.f("context.title_max_pitch_factor") * pitch
    best: TextLine | None = None
    for ln in reversed(_lines_above(page, table)):
        if _is_furniture(page, ln):
            continue
        gap = table.bbox.y0 - ln.bbox.y1
        if gap > max_gap:
            break
        if _is_title_line(page, ln, cfg):
            best = ln
            break
    return _clean(best.text) if best else None


def _bind_caption(page: Page, table: Table, cfg: Config) -> str | None:
    pitch = _pitch(page)
    max_gap = cfg.f("context.caption_max_pitch_factor") * pitch
    max_words = cfg.i("context.max_caption_words")
    for ln in _lines_below(page, table):
        if _is_furniture(page, ln):
            continue
        gap = ln.bbox.y0 - table.bbox.y1
        if gap > max_gap:
            break
        text = _clean(ln.text)
        if not text or _word_count(text) > max_words:
            continue
        centred = False
        left = ln.bbox.x0 - table.bbox.x0
        right = table.bbox.x1 - ln.bbox.x1
        if left > 0.0 and right > 0.0 and abs(left - right) <= max(left, right) * 0.5:
            centred = True
        smaller = _modal(page) > 0.0 and _line_size(page, ln) < _modal(page)
        if _CAPTION_PREFIX.match(text) or _line_italic(page, ln) or centred or smaller:
            return text
    return None


def _bind_section(page: Page, table: Table, cfg: Config) -> str | None:
    depth = cfg.i("context.max_section_depth")
    hits: list[str] = []
    for ln in reversed(_lines_above(page, table)):
        if _is_furniture(page, ln):
            continue
        if not _is_section_line(page, ln, cfg):
            continue
        text = _clean(ln.text)
        if text and text not in hits:
            hits.append(text)
        if len(hits) >= depth:
            break
    hits.reverse()
    return " / ".join(hits) if hits else None


def _header_text(table: Table) -> str:
    parts: list[str] = []
    for c in table.cells:
        if c.row_idx <= 1 and c.raw_text.strip():
            parts.append(c.raw_text)
    return " ".join(parts)


def _local_unit(page: Page, table: Table, cfg: Config) -> tuple[str, str] | None:
    """Return (note, source) if a declaration sits in or just above the table."""
    header_hit = _scan_unit(_header_text(table))
    if header_hit:
        return header_hit, "header"

    pitch = _pitch(page)
    local_gap = cfg.f("context.unit_local_pitch_factor") * pitch
    for ln in reversed(_lines_above(page, table)):
        gap = table.bbox.y0 - ln.bbox.y1
        if gap > local_gap:
            break
        hit = _scan_unit(ln.text)
        if hit:
            return hit, "local"
    # Furniture often carries the document unit; allow it if it is close.
    for ln in page.lines:
        if ln.index not in page.furniture_line_indices:
            continue
        hit = _scan_unit(ln.text)
        if hit and abs(ln.bbox.cy - table.bbox.y0) <= local_gap * 2.0:
            return hit, "local"
    return None


def capture_context(doc: Document, cfg: Config) -> None:
    """Fill title, caption, section_path and unit_scale_note on accepted tables."""
    inherited: str | None = None
    for page in doc.pages:
        page_decls: list[tuple[float, str]] = []
        for ln in page.lines:
            hit = _scan_unit(ln.text)
            if hit:
                page_decls.append((ln.bbox.y0, hit))
        page_decls.sort()

        for table in page.tables:
            if not table.accepted:
                continue
            table.title = _bind_title(page, table, cfg)
            table.caption = _bind_caption(page, table, cfg)
            table.section_path = _bind_section(page, table, cfg)
            local = _local_unit(page, table, cfg)
            if local:
                table.unit_scale_note, table.unit_scale_source = local
            elif inherited:
                table.unit_scale_note = inherited
                table.unit_scale_source = "inherited"

        # Declarations on this page become the inheritance for tables below
        # and on later pages. Take the last (lowest) one so a restated scale
        # replaces an earlier one.
        if page_decls:
            inherited = page_decls[-1][1]
