from __future__ import annotations

import pytest

from zextract.geometry import (
    Rect,
    horizontal_gap,
    horizontal_overlap_ratio,
    median,
    pair_angle_deg,
    quantile,
    union_all,
    vertical_overlap_ratio,
)


def test_rect_rejects_degenerate():
    with pytest.raises(ValueError):
        Rect(10.0, 0.0, 5.0, 10.0)


def test_vertical_overlap_normalises_by_shorter_box():
    # A short token inside a tall line overlaps that line completely, even
    # though IoU would call them dissimilar. That is the property that lets a
    # token join a line.
    tall = Rect(0.0, 0.0, 100.0, 20.0)
    short = Rect(10.0, 5.0, 20.0, 15.0)
    assert vertical_overlap_ratio(short, tall) == pytest.approx(1.0)
    assert horizontal_overlap_ratio(short, tall) == pytest.approx(1.0)


def test_vertical_overlap_zero_for_stacked_boxes():
    a = Rect(0.0, 0.0, 10.0, 10.0)
    b = Rect(0.0, 20.0, 10.0, 30.0)
    assert vertical_overlap_ratio(a, b) == 0.0


def test_horizontal_gap_is_edge_to_edge_and_symmetric():
    a = Rect(0.0, 0.0, 10.0, 10.0)
    b = Rect(14.0, 0.0, 20.0, 10.0)
    assert horizontal_gap(a, b) == pytest.approx(4.0)
    assert horizontal_gap(b, a) == pytest.approx(4.0)
    # Overlapping boxes have no gap, which is what makes a zero gap meaningful
    # as a signal rather than as a measurement.
    assert horizontal_gap(a, Rect(5.0, 0.0, 15.0, 10.0)) == 0.0


def test_pair_angle_folds_to_half_plane():
    a = Rect(0.0, 0.0, 1.0, 1.0)
    right = Rect(10.0, 0.0, 11.0, 1.0)
    left = Rect(-10.0, 0.0, -9.0, 1.0)
    # An undirected pair carries the same direction evidence either way round.
    assert pair_angle_deg(a, right) == pytest.approx(0.0)
    assert pair_angle_deg(a, left) == pytest.approx(0.0)
    below = Rect(0.0, 10.0, 1.0, 11.0)
    assert abs(pair_angle_deg(a, below)) == pytest.approx(90.0)


def test_quantile_interpolates_and_clamps():
    vals = [0.0, 1.0, 2.0, 3.0, 4.0]
    assert quantile(vals, 0.0) == 0.0
    assert quantile(vals, 1.0) == 4.0
    assert quantile(vals, 0.5) == pytest.approx(2.0)
    assert quantile(vals, 0.25) == pytest.approx(1.0)


def test_median_even_and_odd():
    assert median([3.0, 1.0, 2.0]) == 2.0
    assert median([4.0, 1.0, 3.0, 2.0]) == pytest.approx(2.5)


def test_union_all_requires_input():
    assert union_all([Rect(0, 0, 1, 1), Rect(5, 5, 6, 6)]) == Rect(0, 0, 6, 6)
    with pytest.raises(ValueError):
        union_all([])
