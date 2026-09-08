"""Stage driver.

Each stage is a pure function over the state tree; this module sequences them
and owns the one piece of cross-page reasoning at this level -- substituting
document-median statistics into pages that could not calibrate themselves.
"""

from __future__ import annotations

from pathlib import Path

import pymupdf

from .config import Config
from .model import Document, PageStats
from .s1_intake import compute_page_stats, document_fallback_stats, load_document
from .s1_intake.tokens import read_page_text
from .s2_layout import build_blocks, build_lines, extract_rules, mark_furniture
from .s3_detect import detect_tables
from .s4_structure import build_grid
from .s5_ocr import maybe_ocr_page
from .s6_stitch import stitch_document
from .s7_normalise import normalise_table
from .s8_context import (
    associate_assets,
    bind_footnotes,
    capture_context,
    harvest_assets,
    strip_markers_from_row_labels,
)
from .s9_confidence import score_table
from .s9_confidence.apply_calib import apply_calibration

LOW_CONFIDENCE_LAYOUT = "LOW_CONFIDENCE_LAYOUT"


def run_layout(path: Path | str, cfg: Config) -> Document:
    """Run the built stages and return the populated state tree."""
    doc, handle = load_document(path, cfg)
    try:
        _pass_tokens_and_stats(doc, handle, cfg)
        _apply_document_fallback(doc)
        _pass_rules_and_lines(doc, handle, cfg)
        mark_furniture(doc.pages, cfg)
        _pass_blocks(doc, cfg)
        _pass_tables(doc, cfg)
        stitch_document(doc, cfg)
        harvest_assets(doc, handle, cfg)
        capture_context(doc, cfg)
        associate_assets(doc, cfg)
        _pass_normalise_and_score(doc, cfg)
        bind_footnotes(doc, cfg)
        # Markers are known only once footnotes are bound, so the semantic
        # labels are cleaned here rather than when they were first built.
        strip_markers_from_row_labels(doc, cfg)
    finally:
        handle.close()
    return doc


def _pass_tokens_and_stats(doc: Document, handle: pymupdf.Document, cfg: Config) -> None:
    for page in doc.pages:
        src = handle[page.page_no - 1]
        # Writing direction is a fact on a digital page and an inference only
        # on a raster one (see tokens.text_layer_direction). One rawdict parse
        # yields both.
        page.tokens, direction = read_page_text(src)
        maybe_ocr_page(page, src, cfg)
        if "OCR_TOKENS" in page.notes:
            direction = None
        page.stats = compute_page_stats(
            page.tokens, cfg, known_text_angle=direction if page.tokens else None
        )


def _apply_document_fallback(doc: Document) -> None:
    """Give pages that could not calibrate the document's median statistics.

    Falling back to the document rather than to a constant is what keeps D20
    intact: an unusual page inherits a number measured from its own document,
    not one measured from ours. Every page that takes the fallback is marked
    LOW_CONFIDENCE_LAYOUT, and every cell extracted from it will carry a
    confidence penalty (ARCHITECTURE.md 9.5).
    """
    fallback = document_fallback_stats([p.stats for p in doc.pages if p.stats is not None])
    if fallback is None:
        doc.notes.append("NO_STABLE_PAGE_STATS")

    for page in doc.pages:
        s = page.stats
        if s is None or s.stable:
            continue

        # Only an unstable PITCH makes a page low-confidence. Pitch drives
        # lines, blocks and rules; a page that measured its pitch cleanly did
        # its S2 work on its own geometry and should not be penalised because it
        # had no inter-word spacing to measure (see model.PageStats).
        if not s.pitch_stable:
            page.notes.append(LOW_CONFIDENCE_LAYOUT)
        page.notes.extend(s.notes)

        if fallback is None:
            continue
        # Inherit each statistic independently: keep whatever this page
        # established for itself, and fill only the gaps.
        page.stats = PageStats(
            line_pitch=s.line_pitch if s.pitch_stable else fallback.line_pitch,
            within_line_word_gap=(
                s.within_line_word_gap if s.word_gap_stable else fallback.within_line_word_gap
            ),
            text_angle_deg=s.text_angle_deg,
            modal_font_size=(
                s.modal_font_size if s.modal_font_size > 0.0 else fallback.modal_font_size
            ),
            token_count=s.token_count,
            pitch_stable=s.pitch_stable,
            word_gap_stable=s.word_gap_stable,
            source="page" if s.pitch_stable else "document",
            notes=tuple(s.notes) + ("INHERITED_FROM_DOCUMENT",),
        )


def _pass_rules_and_lines(doc: Document, handle: pymupdf.Document, cfg: Config) -> None:
    for page in doc.pages:
        assert page.stats is not None
        src = handle[page.page_no - 1]
        page.rules = extract_rules(src, page.stats, cfg)
        page.lines = build_lines(page.tokens, page.stats, cfg)


def _pass_blocks(doc: Document, cfg: Config) -> None:
    """Blocks are built from body lines only.

    Furniture has to be identified first, and that is a cross-page question, so
    it cannot happen inside the per-page line pass.
    """
    for page in doc.pages:
        assert page.stats is not None
        furniture = set(page.furniture_line_indices)
        body = [ln for ln in page.lines if ln.index not in furniture]
        page.blocks = build_blocks(body, page.rules, page.tokens, page.stats, cfg)


def _pass_tables(doc: Document, cfg: Config) -> None:
    for page in doc.pages:
        page.tables = detect_tables(page, cfg, build_grid)


def _pass_normalise_and_score(doc: Document, cfg: Config) -> None:
    """S7 then S9: types and values first, then the signals that judge them."""
    for page in doc.pages:
        for table in page.tables:
            if not table.accepted:
                continue
            normalise_table(table, cfg)
            score_table(table, page, cfg)
            apply_calibration(table, cfg)
