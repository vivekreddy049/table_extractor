"""S8 -- footnote marker → footnote block.

Markers are trailing ``* † ‡ §`` or a parenthesised/superscript digit on a
cell. Blocks are lines that *start* with the same marker. Search the current
page then the next two (ARCHITECTURE.md S8, the brief's following-page case).
"""

from __future__ import annotations

import re

from ..config import Config
from ..model import Document, Footnote, Page, Table

_TRAIL = re.compile(
    r"(?P<body>.*?)(?P<mark>(?:[*†‡§]+|\(\d{1,2}\)|\[\d{1,2}\]|[¹²³⁴⁵⁶⁷⁸⁹⁰]+))\s*$"
)
_LEAD = re.compile(
    r"^(?P<mark>(?:[*†‡§]+|\(\d{1,2}\)|\[\d{1,2}\]|[¹²³⁴⁵⁶⁷⁸⁹⁰]+|\d{1,2}[.)]))\s+(?P<body>.+)$"
)


def bind_footnotes(doc: Document, cfg: Config) -> None:
    """Populate ``doc.footnotes`` and cell ``footnote_ids``."""
    blocks: list[tuple[int, str, str, object]] = []  # page_no, mark, text, bbox
    for page in doc.pages:
        for ln in page.lines:
            m = _LEAD.match((ln.text or "").strip())
            if not m:
                continue
            mark = _norm_mark(m.group("mark"))
            body = " ".join(m.group("body").split())
            if not mark or not body:
                continue
            blocks.append((page.page_no, mark, body, ln.bbox))

    footnotes: list[Footnote] = []
    for page_no, mark, body, bbox in blocks:
        footnotes.append(
            Footnote(
                index=len(footnotes),
                page_no=page_no,
                marker=mark,
                text=body,
                bbox=bbox,
            )
        )
    doc.footnotes = footnotes
    horizon = cfg.i("footnotes.search_pages_ahead")

    for page in doc.pages:
        for table in page.tables:
            if not table.accepted:
                continue
            _bind_table(table, footnotes, page.page_no, horizon)


def _norm_mark(raw: str) -> str:
    s = (raw or "").strip()
    s = s.replace("(", "").replace(")", "").replace("[", "").replace("]", "")
    s = s.rstrip(".")
    trans = str.maketrans("¹²³⁴⁵⁶⁷⁸⁹⁰", "1234567890")
    s = s.translate(trans)
    return s


def _bind_table(
    table: Table, footnotes: list[Footnote], page_no: int, horizon: int
) -> None:
    by_mark: dict[str, list[Footnote]] = {}
    for fn in footnotes:
        if page_no <= fn.page_no <= page_no + horizon:
            by_mark.setdefault(fn.marker, []).append(fn)

    for cell in table.cells:
        m = _TRAIL.match(cell.raw_text or "")
        if not m:
            continue
        mark = _norm_mark(m.group("mark"))
        if not mark or mark not in by_mark:
            continue
        # Prefer the nearest following (or same-page) block with this marker.
        cands = by_mark[mark]
        chosen = cands[0]
        cell.footnote_ids = (chosen.index,)
        cell.notes = tuple(cell.notes) + ("FOOTNOTE_BOUND",)


def strip_markers_from_row_labels(doc: Document, cfg: Config) -> None:
    """Remove footnote markers from the SEMANTIC row-label fields.

    ``raw_text`` is left exactly as the page prints it -- "Total (3)", "4.29*" --
    because raw_text is the document's own bytes and every other stage depends
    on that being true. What gets cleaned is ``row_label_path``, which is the
    queryable hierarchy: ``row_label_tokens`` is indexed off it, and the brief's
    litmus query ("every cell under a Revenue line item") matches tokens
    exactly. A token stored as "Total (3)" simply never matches "Total", so the
    marker silently defeats the index it is stored in.

    Only markers this DOCUMENT actually declares are stripped, which is what
    makes it safe. The candidates that look identical to a marker are the real
    hazard: "(565)" is a parenthesised negative, "(408) 996-1010" is a phone
    area code, "(Address of principal executive offices)" is prose. None of
    them appear in doc.footnotes, so none of them match. Apple declares markers
    * ** *** and 1-23; 408 and 565 are not among them.

    A label that is ONLY a marker keeps its text -- stripping it would leave an
    empty label, which is worse than a decorated one.
    """
    markers = {(f.marker or "").strip() for f in doc.footnotes}
    markers.discard("")
    if not markers:
        return

    star_marks = sorted(
        (m for m in markers if set(m) <= {"*", "†", "‡"}), key=len, reverse=True
    )
    num_marks = {m for m in markers if m.isdigit()}

    def strip(label: str) -> str:
        s = (label or "").strip()
        if not s:
            return label

        # Trailing star/dagger run: "4.29*" -> "4.29".
        for m in star_marks:
            if s.endswith(m) and len(s) > len(m):
                s = s[: -len(m)].rstrip()
                break

        # Trailing "(N)": "Total (3)" -> "Total".
        if s.endswith(")"):
            open_i = s.rfind("(")
            if open_i > 0:
                inner = s[open_i + 1 : -1].strip()
                if inner in num_marks:
                    s = s[:open_i].rstrip()

        # Leading "(N)": "(1) China" -> "China".
        if s.startswith("("):
            close_i = s.find(")")
            if close_i > 0:
                inner = s[1:close_i].strip()
                if inner in num_marks and s[close_i + 1 :].strip():
                    s = s[close_i + 1 :].lstrip()

        return s if s else label

    for page in doc.pages:
        for table in page.tables:
            for row in table.rows:
                if not row.row_label_path:
                    continue
                cleaned = tuple(strip(p) for p in row.row_label_path)
                if cleaned != row.row_label_path:
                    row.row_label_path = cleaned
