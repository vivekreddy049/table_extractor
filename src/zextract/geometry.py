"""Bounding-box primitives.

Coordinates are PDF user-space points with the origin at the TOP-LEFT and y
increasing downward -- PyMuPDF's convention, kept throughout so that no stage
has to reason about which space it is in. ``Rect`` is immutable so it can be a
dict key and can be hashed into deterministic sort tie-breakers.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Sequence


@dataclass(frozen=True, order=True)
class Rect:
    x0: float
    y0: float
    x1: float
    y1: float

    def __post_init__(self) -> None:
        if self.x1 < self.x0 or self.y1 < self.y0:
            raise ValueError(f"degenerate rect: {self}")

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.y1 - self.y0

    @property
    def cx(self) -> float:
        return 0.5 * (self.x0 + self.x1)

    @property
    def cy(self) -> float:
        return 0.5 * (self.y0 + self.y1)

    @property
    def area(self) -> float:
        return self.width * self.height

    def as_tuple(self) -> tuple[float, float, float, float]:
        return (self.x0, self.y0, self.x1, self.y1)

    def rounded(self, nd: int = 3) -> "Rect":
        """Quantise coordinates.

        Applied at serialisation boundaries so that float noise from PDF parsing
        cannot make two otherwise-identical runs differ in the last bit.
        """
        return Rect(round(self.x0, nd), round(self.y0, nd), round(self.x1, nd), round(self.y1, nd))

    def union(self, other: "Rect") -> "Rect":
        return Rect(
            min(self.x0, other.x0),
            min(self.y0, other.y0),
            max(self.x1, other.x1),
            max(self.y1, other.y1),
        )

    def intersection_height(self, other: "Rect") -> float:
        return max(0.0, min(self.y1, other.y1) - max(self.y0, other.y0))

    def intersection_width(self, other: "Rect") -> float:
        return max(0.0, min(self.x1, other.x1) - max(self.x0, other.x0))

    def intersects(self, other: "Rect") -> bool:
        return self.intersection_width(other) > 0.0 and self.intersection_height(other) > 0.0

    def iou(self, other: "Rect") -> float:
        inter = self.intersection_width(other) * self.intersection_height(other)
        if inter <= 0.0:
            return 0.0
        denom = self.area + other.area - inter
        return inter / denom if denom > 0.0 else 0.0

    def contains_point(self, x: float, y: float) -> bool:
        return self.x0 <= x <= self.x1 and self.y0 <= y <= self.y1


def union_all(rects: Iterable[Rect]) -> Rect:
    it = iter(rects)
    try:
        acc = next(it)
    except StopIteration:
        raise ValueError("union_all() of empty sequence")
    for r in it:
        acc = acc.union(r)
    return acc


def vertical_overlap_ratio(a: Rect, b: Rect) -> float:
    """Shared vertical extent as a fraction of the SHORTER box's height.

    Normalising by the shorter box (rather than by the union, as IoU would) is
    what lets a short token join a tall line: the question we are asking is
    "does this token sit on that line", not "are these two boxes similar".
    """
    shorter = min(a.height, b.height)
    if shorter <= 0.0:
        return 0.0
    return a.intersection_height(b) / shorter


def horizontal_overlap_ratio(a: Rect, b: Rect) -> float:
    """Shared horizontal extent as a fraction of the NARROWER box's width."""
    narrower = min(a.width, b.width)
    if narrower <= 0.0:
        return 0.0
    return a.intersection_width(b) / narrower


def horizontal_gap(a: Rect, b: Rect) -> float:
    """Edge-to-edge horizontal separation; 0.0 when the boxes overlap in x."""
    if a.x1 <= b.x0:
        return b.x0 - a.x1
    if b.x1 <= a.x0:
        return a.x0 - b.x1
    return 0.0


def pair_angle_deg(a: Rect, b: Rect) -> float:
    """Angle of the centre-to-centre vector, folded onto [-90, 90).

    Folded because a neighbour pair is undirected: a word to the left and a word
    to the right carry the same "this is the text direction" evidence.
    """
    dx = b.cx - a.cx
    dy = b.cy - a.cy
    ang = math.degrees(math.atan2(dy, dx))
    while ang >= 90.0:
        ang -= 180.0
    while ang < -90.0:
        ang += 180.0
    return ang


def centre_distance(a: Rect, b: Rect) -> float:
    return math.hypot(b.cx - a.cx, b.cy - a.cy)


def quantile(sorted_values: Sequence[float], q: float) -> float:
    """Linear-interpolated quantile over an already-sorted sequence.

    Hand-rolled rather than numpy so the interpolation is pinned: numpy's
    default method has changed across versions, and this value feeds histogram
    bin edges, so a change there would change output bytes.
    """
    if not sorted_values:
        raise ValueError("quantile of empty sequence")
    if q <= 0.0:
        return sorted_values[0]
    if q >= 1.0:
        return sorted_values[-1]
    pos = q * (len(sorted_values) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return sorted_values[lo]
    frac = pos - lo
    return sorted_values[lo] * (1.0 - frac) + sorted_values[hi] * frac


def median(values: Sequence[float]) -> float:
    if not values:
        raise ValueError("median of empty sequence")
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2 == 1:
        return s[mid]
    return 0.5 * (s[mid - 1] + s[mid])
