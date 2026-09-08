"""Docstrum self-calibration.

The load-bearing property is that the measured pitch matches the pitch the
fixture was BUILT with. If that drifts, every threshold in every later stage
drifts with it (DECISIONS.md D20).
"""

from __future__ import annotations

import pymupdf
import pytest

from zextract.s1_intake import compute_page_stats, document_fallback_stats
from zextract.s1_intake.tokens import read_page_text

from conftest import RULED_FONT, RULED_PITCH


def _stats(pdf, cfg, page_no=0):
    doc = pymupdf.open(str(pdf))
    try:
        tokens, direction = read_page_text(doc[page_no])
        return tokens, compute_page_stats(tokens, cfg, known_text_angle=direction)
    finally:
        doc.close()


def test_recovers_the_pitch_the_fixture_was_built_with(ruled_pdf, cfg):
    _, s = _stats(ruled_pdf, cfg)
    assert s.stable
    assert s.line_pitch == pytest.approx(RULED_PITCH, abs=0.5)
    assert s.modal_font_size == pytest.approx(RULED_FONT, abs=0.1)
    assert s.text_angle_deg == pytest.approx(0.0, abs=0.1)


def test_word_gap_is_positive_and_smaller_than_pitch(ruled_pdf, cfg):
    _, s = _stats(ruled_pdf, cfg)
    # An inter-word space is always a fraction of the line pitch. A word gap at
    # or above the pitch means the along/across classification has collapsed --
    # which is exactly the failure the vertical-overlap guard was added for.
    assert 0.0 < s.within_line_word_gap < s.line_pitch


def test_upright_sparse_table_is_not_called_vertical(borderless_pdf, cfg):
    # The regression this guards: on a sparse table a token's nearest
    # neighbours are above and below it, so naive Docstrum reports -90 on a
    # perfectly upright page (measured on Apple p32 and Shell.pdf p1).
    _, s = _stats(borderless_pdf, cfg)
    assert s.stable, f"sparse table failed to calibrate: {s.notes}"
    assert abs(s.text_angle_deg) < 1.0
    assert "VERTICAL_TEXT_DIRECTION" not in s.notes


def test_empty_page_is_refused_not_guessed(empty_pdf, cfg):
    tokens, s = _stats(empty_pdf, cfg)
    assert tokens == []
    assert not s.pitch_stable and not s.word_gap_stable
    assert "TOO_FEW_TOKENS" in s.notes
    assert s.line_pitch == 0.0  # refused, not invented


def test_pitch_and_word_gap_fail_independently(ruled_pdf, cfg):
    """A page can have a sound pitch and no measurable word gap.

    Failing the whole page for that would mark most sparse table pages in a
    financial document low-confidence for a reason that has nothing to do with
    their line work (see model.PageStats).
    """
    tokens, _ = _stats(ruled_pdf, cfg)
    impossible_gap = type(cfg)(
        data={**cfg.data, "docstrum": {**cfg.data["docstrum"], "min_peak_mass": 1.01}}
    )
    s = compute_page_stats(tokens, impossible_gap, known_text_angle=0.0)
    assert not s.pitch_stable and not s.word_gap_stable
    assert not s.stable


def test_pitch_size_ratio_band_rejects_a_confident_wrong_peak(ruled_pdf, cfg):
    # A sparse page can produce a high-mass peak that is a block gap rather than
    # line spacing (MRPL p5: pitch 40.1 against a 9pt font). The typographic
    # band is what catches it.
    tokens, _ = _stats(ruled_pdf, cfg)
    narrowed = type(cfg)(
        data={**cfg.data, "docstrum": {**cfg.data["docstrum"], "max_pitch_size_ratio": 1.0}}
    )
    s = compute_page_stats(tokens, narrowed, known_text_angle=0.0)
    assert not s.pitch_stable
    assert "PITCH_SIZE_RATIO_OUT_OF_BAND" in s.notes


def test_document_fallback_is_median_of_stable_pages(ruled_pdf, cfg):
    _, s = _stats(ruled_pdf, cfg)
    unstable = type(s)(
        line_pitch=0.0,
        within_line_word_gap=0.0,
        text_angle_deg=0.0,
        modal_font_size=0.0,
        token_count=0,
        pitch_stable=False,
        word_gap_stable=False,
        source="fallback",
    )
    fb = document_fallback_stats([s, s, unstable])
    assert fb is not None
    assert fb.line_pitch == pytest.approx(s.line_pitch)
    assert fb.source == "document"
    assert not fb.stable  # inherited values are never presented as measured

    assert document_fallback_stats([unstable]) is None


def test_stats_are_identical_across_repeated_computation(ruled_pdf, cfg):
    tokens, first = _stats(ruled_pdf, cfg)
    second = compute_page_stats(tokens, cfg, known_text_angle=0.0)
    assert first.line_pitch == second.line_pitch
    assert first.within_line_word_gap == second.within_line_word_gap
