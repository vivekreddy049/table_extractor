"""S1 -- Docstrum page self-calibration.

Implements the nearest-neighbour analysis of O'Gorman (1993), "The Document
Spectrum for Page Layout Analysis", adapted from connected components to text
layer word tokens.

Why this exists (ARCHITECTURE.md 9.1, DECISIONS.md D20): every threshold in
every later stage is expressed as a multiple of the numbers this module
produces, so that no threshold is tied to a font size, a page size or a DPI.
Apple's body text is 8.1pt; a rating report is denser; a 300dpi scan has no
points at all. "1.6 line pitches" means the same thing in all three; "6 points"
does not.

Adaptation from the original: for near-horizontal neighbour pairs we measure the
EDGE-TO-EDGE gap rather than the centre-to-centre distance. Docstrum operated on
connected components, where a component is roughly a character and centre
distance approximates spacing. Our tokens are whole words of wildly varying
width, so centre distance would measure word length, not spacing. Vertical pairs
still use centre distance, which is what line pitch means.
"""

from __future__ import annotations

import math
from typing import Sequence

import numpy as np

from ..config import Config
from ..geometry import (
    Rect,
    horizontal_gap,
    horizontal_overlap_ratio,
    median,
    pair_angle_deg,
    quantile,
    vertical_overlap_ratio,
)
from ..model import PageStats, Token


# Rows of the distance matrix computed per block. Bounds peak memory at
# _KNN_BLOCK * n floats regardless of how many tokens a page has, so a dense
# page cannot blow the 4 GB budget.
_KNN_BLOCK = 512


def _k_nearest(boxes: Sequence[Rect], k: int) -> list[list[int]]:
    """For each box, the indices of its k nearest neighbours by centre distance.

    Brute force over all pairs, blocked and vectorised. A spatial index would be
    asymptotically better but costs more to build than the scan costs to run at
    a few hundred to a few thousand tokens per page, and it would introduce
    tie-breaking subtleties that a full scan simply does not have.

    Ties are broken by index: ``argsort(kind="stable")`` preserves input order
    among equal distances, so two tokens at identical distance always resolve
    the same way and the neighbour set is byte-identical across runs.
    """
    n = len(boxes)
    if n == 0:
        return []
    if n == 1:
        return [[]]

    cx = np.fromiter((b.cx for b in boxes), dtype=np.float64, count=n)
    cy = np.fromiter((b.cy for b in boxes), dtype=np.float64, count=n)
    kk = min(k, n - 1)

    out: list[list[int]] = []
    for start in range(0, n, _KNN_BLOCK):
        stop = min(start + _KNN_BLOCK, n)
        dx = cx[start:stop, None] - cx[None, :]
        dy = cy[start:stop, None] - cy[None, :]
        d2 = dx * dx + dy * dy
        # Exclude self without disturbing the ordering of anything else.
        d2[np.arange(stop - start), np.arange(start, stop)] = np.inf
        # argpartition would be faster but leaves the top-k unordered, and we
        # need a deterministic order within the k. At these sizes the full
        # stable argsort is cheap enough.
        idx = np.argsort(d2, axis=1, kind="stable")[:, :kk]
        out.extend(row.tolist() for row in idx)
    return out


def _histogram_peak(
    values: Sequence[float],
    n_bins: int,
    range_quantile: float,
    smoothing_bins: int,
    min_peak_mass: float,
) -> tuple[float, bool]:
    """Modal value of a 1-D distribution, and whether the mode is trustworthy.

    Bin edges run from 0 to the requested quantile of the data, so bin width
    scales with the page rather than being an absolute width. The peak is
    refined to the median of the values inside the winning bin, which keeps the
    answer stable when the bin count changes.
    """
    vals = sorted(v for v in values if v >= 0.0 and math.isfinite(v))
    if len(vals) < n_bins // 8 or not vals:
        return (0.0, False)

    hi = quantile(vals, range_quantile)
    if hi <= 0.0:
        return (0.0, False)

    width = hi / n_bins
    counts = [0] * n_bins
    members: list[list[float]] = [[] for _ in range(n_bins)]
    for v in vals:
        if v > hi:
            continue
        b = int(v / width)
        if b >= n_bins:
            b = n_bins - 1
        counts[b] += 1
        members[b].append(v)

    total = sum(counts)
    if total == 0:
        return (0.0, False)

    # Moving average. Smoothing before argmax stops a one-bin spike caused by a
    # handful of identical coordinates from winning.
    half = max(0, smoothing_bins // 2)
    smoothed = []
    for b in range(n_bins):
        lo = max(0, b - half)
        hi_b = min(n_bins - 1, b + half)
        window = counts[lo : hi_b + 1]
        smoothed.append(sum(window) / float(len(window)))

    best = 0
    for b in range(1, n_bins):
        if smoothed[b] > smoothed[best]:
            best = b
    # Ties resolve to the lowest bin index by the strict > above, which is what
    # makes this deterministic.

    # Peak mass is measured over the SMOOTHING WINDOW, not over the single
    # winning bin. Bin width scales with the data range, so a genuine peak with
    # any spread at all lands across several bins; testing one bin in isolation
    # rejects real peaks purely because the histogram is fine-grained.
    w_lo = max(0, best - half)
    w_hi = min(n_bins - 1, best + half)
    peak_mass = sum(counts[w_lo : w_hi + 1]) / float(total)
    if peak_mass < min_peak_mass:
        return (0.0, False)

    # Refine to the median of the values in the window, for the same reason.
    pool: list[float] = []
    for b in range(w_lo, w_hi + 1):
        pool.extend(members[b])
    if not pool:
        return ((best + 0.5) * width, False)
    return (median(pool), True)


def _angle_peak(
    angles: Sequence[float], n_bins: int, smoothing_bins: int, vertical_dominance: float
) -> tuple[float, bool]:
    """Dominant text direction in degrees, and whether writing runs vertically.

    A naive argmax over the folded angle histogram is wrong on exactly the pages
    we care most about. On a sparse table page, a number's nearest neighbours are
    the numbers ABOVE and BELOW it in its own column -- column gutters are wide
    and line pitch is small -- so vertical pairs outnumber horizontal ones and
    the "text angle" comes back as -90 on a perfectly upright page. Measured on
    Apple p32 and Shell.pdf p1 before this guard existed.

    So the direction is decided first, by comparing total mass near 0 against
    total mass near +/-90, with a bias towards horizontal: vertical only wins if
    it dominates by ``vertical_dominance``. The skew is then the mode WITHIN the
    winning band, which keeps a genuinely rotated landscape page working.
    """
    if not angles:
        return (0.0, False)

    mass_h = sum(1 for a in angles if abs(a) <= 45.0)
    mass_v = len(angles) - mass_h
    is_vertical = mass_v > mass_h * vertical_dominance
    if is_vertical:
        # Re-express vertical pairs as an offset from +/-90 so the mode is taken
        # on a continuous band rather than across the fold discontinuity.
        band = [(a - 90.0 if a > 0.0 else a + 90.0) for a in angles if abs(a) > 45.0]
    else:
        band = [a for a in angles if abs(a) <= 45.0]

    if not band:
        return (0.0, is_vertical)

    lo, hi = -45.0, 45.0
    width = (hi - lo) / n_bins
    counts = [0] * n_bins
    members: list[list[float]] = [[] for _ in range(n_bins)]
    for a in band:
        b = int((a - lo) / width)
        b = 0 if b < 0 else (n_bins - 1 if b >= n_bins else b)
        counts[b] += 1
        members[b].append(a)

    half = max(0, smoothing_bins // 2)
    best, best_val = 0, -1.0
    for b in range(n_bins):
        w_lo = max(0, b - half)
        w_hi = min(n_bins - 1, b + half)
        val = sum(counts[w_lo : w_hi + 1]) / float(w_hi - w_lo + 1)
        if val > best_val:
            best, best_val = b, val

    w_lo = max(0, best - half)
    w_hi = min(n_bins - 1, best + half)
    pool: list[float] = []
    for b in range(w_lo, w_hi + 1):
        pool.extend(members[b])
    return (median(pool) if pool else 0.0, is_vertical)


def compute_page_stats(
    tokens: Sequence[Token], cfg: Config, known_text_angle: float | None = None
) -> PageStats:
    """Docstrum statistics for one page.

    ``known_text_angle`` is the writing direction read from the text layer
    (``tokens.text_layer_direction``). Pass it whenever there is a text layer:
    the direction is then a fact rather than an inference, and the histogram
    fallback is reserved for raster pages, which is the case Docstrum was built
    for.

    Returns ``stable=False`` when the page cannot support the histograms -- too
    few tokens, no peak carrying enough mass, or a pitch that fails the
    typographic sanity band. The caller is expected to substitute document-level
    statistics and mark the page LOW_CONFIDENCE_LAYOUT (ARCHITECTURE.md 9.5);
    this function never invents a plausible number.
    """
    k = cfg.i("docstrum.k_neighbours")
    cone = cfg.f("docstrum.angle_cone_deg")
    n_bins = cfg.i("docstrum.n_bins")
    rq = cfg.f("docstrum.range_quantile")
    smooth = cfg.i("docstrum.smoothing_bins")
    min_tokens = cfg.i("docstrum.min_tokens")
    min_mass = cfg.f("docstrum.min_peak_mass")

    notes: list[str] = []
    n = len(tokens)
    if n < min_tokens:
        return PageStats(
            line_pitch=0.0,
            within_line_word_gap=0.0,
            text_angle_deg=0.0,
            modal_font_size=_modal_size(tokens),
            token_count=n,
            pitch_stable=False,
            word_gap_stable=False,
            source="fallback",
            notes=("TOO_FEW_TOKENS",),
        )

    vdom = cfg.f("docstrum.vertical_dominance")

    boxes = [t.bbox for t in tokens]
    neighbours = _k_nearest(boxes, k)

    angles = [
        [pair_angle_deg(boxes[i], boxes[j]) for j in js] for i, js in enumerate(neighbours)
    ]
    if known_text_angle is not None:
        text_angle = _fold(known_text_angle)
        is_vertical = abs(text_angle) > 45.0
        notes.append("ANGLE_FROM_TEXT_LAYER")
    else:
        band_angle, is_vertical = _angle_peak(
            [a for row in angles for a in row], n_bins, smooth, vdom
        )
        # ``band_angle`` is the deviation within the winning band; convert it
        # back to a real writing-direction angle.
        text_angle = _fold(band_angle + 90.0) if is_vertical else band_angle
        notes.append("ANGLE_INFERRED")

    # One sample per token per direction: the NEAREST along-neighbour and the
    # NEAREST across-neighbour.
    #
    # Taking all k neighbours instead spreads the across-distance mass over the
    # line pitch AND its multiples -- with k=5 a token sees the lines one, two
    # and three rows away -- which flattens the very peak we are trying to find.
    # Restricting to the nearest concentrates the mass at the fundamental.
    #
    # The 30-degree cone leaves a dead zone between the two classes; pairs
    # falling in it carry no clean evidence either way and are dropped rather
    # than forced into one.
    # The angle cone alone is not enough to say two words share a line. On Apple
    # p30 (pitch 10pt, words ~30pt wide) a word on the NEXT line sits at a
    # centre-to-centre angle of only 20-30 degrees, lands inside the cone, and
    # contributes a horizontal gap of 0 because the two boxes overlap in x. That
    # put 142 spurious zeros into a 432-sample histogram and destroyed the peak.
    #
    # Docstrum could not do better because it worked on connected components,
    # which are point-like. Our tokens are whole words with real extent, so we
    # can add the test that actually expresses the question: words on the same
    # line OVERLAP VERTICALLY, and words in the same column OVERLAP
    # HORIZONTALLY. Both are dimensionless ratios, so D20 holds.
    min_ov = cfg.f("docstrum.min_pair_overlap")

    along: list[float] = []
    across: list[float] = []
    for i, js in enumerate(neighbours):
        best_along: float | None = None
        best_across: float | None = None
        for slot, j in enumerate(js):
            delta = _fold(angles[i][slot] - text_angle)
            if abs(delta) <= cone:
                if vertical_overlap_ratio(boxes[i], boxes[j]) < min_ov:
                    continue
                g = horizontal_gap(boxes[i], boxes[j])
                if best_along is None or g < best_along:
                    best_along = g
            elif abs(abs(delta) - 90.0) <= cone:
                if horizontal_overlap_ratio(boxes[i], boxes[j]) < min_ov:
                    continue
                d = abs(boxes[j].cy - boxes[i].cy)
                if best_across is None or d < best_across:
                    best_across = d
        if best_along is not None:
            along.append(best_along)
        if best_across is not None:
            across.append(best_across)

    word_gap, gap_ok = _histogram_peak(along, n_bins, rq, smooth, min_mass)
    pitch, pitch_ok = _histogram_peak(across, n_bins, rq, smooth, min_mass)

    if not gap_ok:
        notes.append("WORD_GAP_UNSTABLE")
    if not pitch_ok:
        notes.append("LINE_PITCH_UNSTABLE")

    # A degenerate word gap of exactly 0 happens on pages whose producer emits
    # touching boxes. It is a real measurement, but useless as a scale unit, so
    # it is reported as unstable rather than propagated.
    if gap_ok and word_gap <= 0.0:
        gap_ok = False
        notes.append("WORD_GAP_ZERO")

    # Typographic sanity band on the pitch.
    #
    # A peak can carry plenty of mass and still be the wrong peak: MRPL p5 is a
    # sparse annexure page whose 78 tokens produced a confident line pitch of
    # 40.1pt against a 9pt body font -- a ratio of 4.5, which is not line
    # spacing, it is the gap between two isolated blocks. Line pitch in real
    # typography sits at roughly 1.0 to 3.0 times the font size; outside that,
    # we have measured something else and should say so rather than propagate a
    # confident wrong number into every threshold downstream. The band is a
    # dimensionless ratio, so D20 holds.
    modal = _modal_size(tokens)
    if pitch_ok and modal > 0.0:
        ratio = pitch / modal
        if ratio < cfg.f("docstrum.min_pitch_size_ratio") or ratio > cfg.f(
            "docstrum.max_pitch_size_ratio"
        ):
            pitch_ok = False
            notes.append("PITCH_SIZE_RATIO_OUT_OF_BAND")


    # Typographic sanity band on the WORD GAP, mirroring the pitch band above.
    #
    # A sparse table page has no genuine inter-word spacing to measure: every
    # token is its own cell, so the "gaps" the histogram finds are COLUMN
    # GUTTERS. Apple p23 (the stock-performance table) reported a word gap of
    # 10.28pt against an 8.1pt font -- a ratio of 1.27, where the median across
    # all 130 sample pages is 0.28 and no other page exceeds 0.30. That inflated
    # every column threshold downstream (gutters had to exceed 22.6pt, tokens
    # were padded by 6.2pt a side) and collapsed a 4x7 table into 2x2.
    #
    # An inter-word space is a FRACTION of the font size. A gap wider than the
    # font is not a word gap, whatever the histogram says.
    if gap_ok and modal > 0.0:
        gap_ratio = word_gap / modal
        if gap_ratio > cfg.f("docstrum.max_word_gap_size_ratio"):
            gap_ok = False
            notes.append("WORD_GAP_SIZE_RATIO_OUT_OF_BAND")

    if is_vertical:
        # Writing runs down the page: a landscape table inside a portrait
        # document, or a page whose /Rotate we have not applied. The along/across
        # measures above assume horizontal writing, so their numbers are not
        # meaningful here. Report the direction, refuse the statistics, and let
        # S1 rotation normalisation re-extract the page in the upright frame.
        notes.append("VERTICAL_TEXT_DIRECTION")
        pitch_ok = False
        gap_ok = False
    return PageStats(
        line_pitch=pitch,
        within_line_word_gap=word_gap,
        text_angle_deg=text_angle,
        modal_font_size=modal,
        token_count=n,
        pitch_stable=pitch_ok,
        word_gap_stable=gap_ok,
        source="page" if (pitch_ok and gap_ok) else "fallback",
        notes=tuple(notes),
    )


def _fold(deg: float) -> float:
    while deg >= 90.0:
        deg -= 180.0
    while deg < -90.0:
        deg += 180.0
    return deg


def _modal_size(tokens: Sequence[Token]) -> float:
    """Most common font size, weighted by character count.

    Character-weighted rather than token-weighted so that a page of body text
    with a large heading reports the body size: the heading has few characters
    however large it is.
    """
    if not tokens:
        return 0.0
    weights: dict[float, int] = {}
    for t in tokens:
        key = round(t.size, 2)
        weights[key] = weights.get(key, 0) + len(t.text)
    # Sort by (-weight, size) so ties resolve to the smaller size, and so the
    # result cannot depend on dict insertion order.
    return sorted(weights.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


def document_fallback_stats(page_stats: Sequence[PageStats]) -> PageStats | None:
    """Median of the stable pages, used for pages that could not calibrate.

    Falling back to the document rather than to a constant keeps D20 intact: an
    unusual page inherits a measured number from its own document, not a number
    that came from ours.
    """
    pitch_pool = [s.line_pitch for s in page_stats if s.pitch_stable and s.line_pitch > 0.0]
    gap_pool = [
        s.within_line_word_gap
        for s in page_stats
        if s.word_gap_stable and s.within_line_word_gap > 0.0
    ]
    if not pitch_pool and not gap_pool:
        return None

    # Each statistic is pooled over the pages that measured IT successfully, not
    # over pages that measured everything successfully -- a document of sparse
    # table pages may never have a page that does both.
    sized = [s.modal_font_size for s in page_stats if s.modal_font_size > 0.0]
    angles = [s.text_angle_deg for s in page_stats if s.pitch_stable]
    return PageStats(
        line_pitch=median(pitch_pool) if pitch_pool else 0.0,
        within_line_word_gap=median(gap_pool) if gap_pool else 0.0,
        text_angle_deg=median(angles) if angles else 0.0,
        modal_font_size=median(sized) if sized else 0.0,
        token_count=0,
        pitch_stable=False,
        word_gap_stable=False,
        source="document",
        notes=("INHERITED_FROM_DOCUMENT",),
    )
