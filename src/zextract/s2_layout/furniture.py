"""S2 -- running headers, footers and repeated page images.

The cheapest large win in the pipeline. Without it, Apple p32's income statement
absorbs "See accompanying Notes to Consolidated Financial Statements." and
"Apple Inc. | 2024 Form 10-K | 29" as two extra table rows, and MRPL's three
repeated logos become 19 assets instead of 1 (ARCHITECTURE.md F6).

Detection is cross-page and content-blind: a line at the very top or bottom of
the page whose text -- with digits masked, because the page number changes --
recurs in that same position on a large share of pages is furniture. No string
from any sample document appears anywhere in this module.

Furniture is EXCLUDED from table candidates but RETAINED on the page, because
running footers frequently carry the document-level unit or scale declaration
that S8 needs.
"""

from __future__ import annotations

import re
from typing import Sequence

from ..config import Config
from ..model import Page

_DIGITS = re.compile(r"\d+")
_WS = re.compile(r"\s+")


def _signature(text: str) -> str:
    """Normalise a line so that page numbers do not make every footer unique."""
    return _WS.sub(" ", _DIGITS.sub("#", text)).strip().casefold()


def mark_furniture(pages: Sequence[Page], cfg: Config) -> None:
    """Populate ``page.furniture_line_indices`` in place."""
    min_share = cfg.f("furniture.min_page_share")

    n_pages = len(pages)
    if n_pages == 0:
        return
    # A one- or two-page document cannot support a frequency argument: every
    # line would look either unique or universal. Say so rather than guess.
    if n_pages < cfg.i("furniture.min_pages"):
        for p in pages:
            p.notes.append("FURNITURE_NOT_TESTED_TOO_FEW_PAGES")
        return

    # Candidates are the first and last few lines of each page, by POSITION,
    # not by geometry.
    #
    # A margin band is the obvious rule and it does not work. Measured on the
    # Apple 10-K: the running footer is the last line of every page, but its y
    # ranges from 523 to 671 on a 792pt page, because the converter places it
    # directly under the content rather than at a fixed height. Nothing in the
    # bottom 12% of those pages is furniture, and everything that IS furniture
    # sits outside the band. Ordinal position is a property of the page's
    # structure; y is a property of one producer's habits.
    depth = cfg.i("furniture.edge_line_depth")

    groups: dict[tuple[str, str], list[tuple[int, int]]] = {}
    for p in pages:
        if p.stats is None or not p.lines:
            continue
        ordered = sorted(p.lines, key=lambda ln: ln.sort_key)
        candidates: list[tuple[str, object]] = [("top", ln) for ln in ordered[:depth]]
        candidates += [("bottom", ln) for ln in ordered[-depth:]]
        for role, ln in candidates:
            sig = _signature(ln.text)  # type: ignore[union-attr]
            if not sig:
                continue
            # Role is part of the key: a string that appears as a header on some
            # pages and inside a table on others must not be conflated.
            groups.setdefault((sig, role), []).append((p.page_no, ln.index))  # type: ignore[union-attr]

    threshold = min_share * n_pages
    furniture: dict[int, set[int]] = {}
    for (_sig, _role), members in sorted(groups.items()):
        distinct_pages = {pg for pg, _ in members}
        if len(distinct_pages) >= threshold:
            for pg, li in members:
                furniture.setdefault(pg, set()).add(li)

    for p in pages:
        p.furniture_line_indices = tuple(sorted(furniture.get(p.page_no, ())))
