"""S1 -- document intake and per-page typing.

Page typing is per page, never per document: a scanned exhibit pasted into an
otherwise digital report is the common case, not the exception (Apple p108 is
39 characters of caption over a full-page image).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pymupdf

from ..config import Config
from ..geometry import Rect
from ..model import Document, Page, PageType

# Resolution of the boolean grid used to measure image coverage. Coverage is a
# ratio, so this is a precision choice rather than a measurement; a grid is used
# instead of summing areas because images overlap and a sum would exceed 1.0.
_COVERAGE_GRID = 128


class IntakeError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def open_document(path: Path | str) -> pymupdf.Document:
    """Open a PDF, decrypting with an empty password if that is all it needs.

    Apple's 10-K is RC4-128 encrypted but carries an empty user password, which
    PyMuPDF reports as ``needs_pass == 0``. A document that genuinely needs a
    password fails loudly here rather than silently yielding zero tables.
    """
    doc = pymupdf.open(str(path))
    if doc.needs_pass:
        if not doc.authenticate(""):
            raise IntakeError(f"{path}: encrypted and requires a password")
    return doc


def _image_coverage(page: pymupdf.Page) -> float:
    """Fraction of the page covered by raster images, via a boolean grid."""
    prect = page.rect
    if prect.width <= 0 or prect.height <= 0:
        return 0.0
    rects: list[Rect] = []
    for info in page.get_images(full=True):
        xref = info[0]
        for r in page.get_image_rects(xref):
            rects.append(Rect(r.x0, r.y0, r.x1, r.y1))
    if not rects:
        return 0.0

    cw = prect.width / _COVERAGE_GRID
    ch = prect.height / _COVERAGE_GRID
    covered = bytearray(_COVERAGE_GRID * _COVERAGE_GRID)
    for r in rects:
        c0 = max(0, int((r.x0 - prect.x0) / cw))
        c1 = min(_COVERAGE_GRID - 1, int((r.x1 - prect.x0) / cw))
        r0 = max(0, int((r.y0 - prect.y0) / ch))
        r1 = min(_COVERAGE_GRID - 1, int((r.y1 - prect.y0) / ch))
        for row in range(r0, r1 + 1):
            base = row * _COVERAGE_GRID
            for col in range(c0, c1 + 1):
                covered[base + col] = 1
    return sum(covered) / float(_COVERAGE_GRID * _COVERAGE_GRID)


def classify_page(char_count: int, image_coverage: float, cfg: Config) -> PageType:
    """Digital / raster / hybrid, from character count and image coverage."""
    digital_min = cfg.i("page_typing.digital_min_chars")
    raster_max = cfg.i("page_typing.raster_max_chars")
    raster_cov = cfg.f("page_typing.raster_min_image_coverage")
    hybrid_cov = cfg.f("page_typing.hybrid_min_image_coverage")

    if char_count < raster_max:
        # Almost no text layer at all. Route to the raster path whether or not
        # an image is present: a genuinely blank page simply yields nothing
        # there, which is the correct outcome rather than a special case.
        return PageType.RASTER

    if image_coverage >= raster_cov and char_count < digital_min:
        # A scanned page whose only text is a header stamp or an exhibit label.
        return PageType.RASTER

    if image_coverage >= hybrid_cov:
        # Substantial text AND a large image: a scanned exhibit inside a digital
        # report. Routed per region, not per page.
        return PageType.HYBRID

    return PageType.DIGITAL


def load_document(path: Path | str, cfg: Config) -> tuple[Document, pymupdf.Document]:
    """Build the page skeleton. Tokens, rules and stats are filled in later.

    Returns the state object and the open PyMuPDF handle, which the caller owns
    and must close.
    """
    p = Path(path)
    doc = open_document(p)
    out = Document(filename=p.name, sha256=sha256_file(p), page_count=doc.page_count)

    for i in range(doc.page_count):
        page = doc[i]
        char_count = len(page.get_text().strip())
        coverage = _image_coverage(page)
        ptype = classify_page(char_count, coverage, cfg)
        out.pages.append(
            Page(
                page_no=i + 1,
                width=float(page.rect.width),
                height=float(page.rect.height),
                rotation=int(page.rotation),
                page_type=ptype,
                image_coverage=coverage,
                char_count=char_count,
            )
        )
    return out, doc
