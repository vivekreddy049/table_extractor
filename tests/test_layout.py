"""S2 -- lines, rules and blocks."""

from __future__ import annotations

import pytest

from zextract.model import RuleOrientation
from zextract.pipeline import run_layout

from conftest import BORDERLESS_ROWS, BORDERLESS_SPLIT_AFTER, RULED_ROWS


def test_lines_group_a_table_row_across_all_columns(ruled_pdf, cfg):
    page = run_layout(ruled_pdf, cfg).pages[0]
    table_lines = [ln for ln in page.lines if ln.bbox.y1 < 200.0]
    assert len(table_lines) == len(RULED_ROWS)
    for ln, expected in zip(table_lines, RULED_ROWS):
        # One line per table row, spanning every column. This is what makes a
        # line usable as row evidence in S3.
        assert ln.text.split() == list(expected)


def test_line_order_is_geometric_not_stream_order(ruled_pdf, cfg):
    page = run_layout(ruled_pdf, cfg).pages[0]
    ys = [ln.bbox.y0 for ln in page.lines]
    assert ys == sorted(ys)


def test_horizontal_rule_is_found_and_no_vertical_rule_is_invented(ruled_pdf, cfg):
    page = run_layout(ruled_pdf, cfg).pages[0]
    h = page.horizontal_rules()
    assert len(h) == 1
    assert h[0].orientation is RuleOrientation.HORIZONTAL
    assert h[0].length > 400.0
    assert page.vertical_rules() == []


def test_rule_splits_a_block_even_without_a_gap(ruled_pdf, cfg):
    page = run_layout(ruled_pdf, cfg).pages[0]
    rule_y = page.horizontal_rules()[0].position
    before = [b for b in page.blocks if b.bbox.y1 < rule_y]
    after = [b for b in page.blocks if b.bbox.y0 > rule_y]
    assert before and after
    # The header sits in its own block because the producer drew a rule under
    # it -- the line spacing on either side is identical.
    assert before[-1].split_reason == "rule"


def test_wide_gap_splits_a_block_without_a_rule(borderless_pdf, cfg):
    page = run_layout(borderless_pdf, cfg).pages[0]
    assert page.vertical_rules() == []
    assert page.horizontal_rules() == []
    assert page.stats is not None and page.stats.stable
    reasons = [b.split_reason for b in page.blocks]
    assert "gap" in reasons
    # Evenly spaced rows, then a 3x-pitch gap, then the remainder.
    assert len(page.blocks) == 2
    assert len(page.blocks[0].line_indices) == BORDERLESS_SPLIT_AFTER
    assert len(page.blocks[1].line_indices) == len(BORDERLESS_ROWS) - BORDERLESS_SPLIT_AFTER


def test_empty_page_produces_no_lines_or_blocks(empty_pdf, cfg):
    page = run_layout(empty_pdf, cfg).pages[0]
    assert page.lines == []
    assert page.blocks == []
    assert "LOW_CONFIDENCE_LAYOUT" in page.notes


def test_every_line_index_referenced_by_a_block_exists(ruled_pdf, cfg):
    page = run_layout(ruled_pdf, cfg).pages[0]
    known = {ln.index for ln in page.lines}
    seen: list[int] = []
    for b in page.blocks:
        for i in b.line_indices:
            assert i in known
            seen.append(i)
    # Blocks partition the lines: every line in exactly one block.
    assert sorted(seen) == sorted(known)


def test_every_token_index_referenced_by_a_line_exists(ruled_pdf, cfg):
    page = run_layout(ruled_pdf, cfg).pages[0]
    seen: list[int] = []
    for ln in page.lines:
        for i in ln.token_indices:
            assert 0 <= i < len(page.tokens)
            seen.append(i)
    assert sorted(seen) == list(range(len(page.tokens)))
