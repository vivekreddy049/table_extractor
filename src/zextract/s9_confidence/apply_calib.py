"""Apply the frozen logistic + isotonic model (DECISIONS.md D7).

Fitting is offline (``python metrics/calibrate.py``). A run only reads
``calib/model_v1.json`` and evaluates. Two runs therefore stay byte-identical.
"""

from __future__ import annotations

import json
import math
from functools import lru_cache
from pathlib import Path

from ..config import Config
from ..model import Table

_MODEL_PATH = Path(__file__).resolve().parents[1] / "calib" / "model_v1.json"


@lru_cache(maxsize=1)
def load_model() -> dict:
    return json.loads(_MODEL_PATH.read_text(encoding="utf-8"))


def apply_calibration(table: Table, cfg: Config) -> None:
    if not cfg.b("confidence.apply_calibration"):
        return
    model = load_model()
    intercept = float(model["logistic"]["intercept"])
    coef = float(model["logistic"]["coef"])
    xs = [float(x) for x in model["isotonic"]["x"]]
    ys = [float(y) for y in model["isotonic"]["y"]]
    floor = cfg.f("confidence.floor")

    for cell in table.cells:
        if "raw_score" in cell.signals:
            raw = float(cell.signals["raw_score"])
        else:
            raw = cell.confidence
            cell.signals["raw_score"] = raw
        z = intercept + coef * raw
        # Guard overflow; the input is already in [floor, 1].
        if z >= 20.0:
            logit = 1.0
        elif z <= -20.0:
            logit = 0.0
        else:
            logit = 1.0 / (1.0 + math.exp(-z))
        cell.signals["logistic"] = round(logit, 6)
        cell.confidence = max(floor, min(1.0, _isotonic(logit, xs, ys)))


def _isotonic(v: float, xs: list[float], ys: list[float]) -> float:
    """Piecewise-linear interpolation along a frozen isotonic curve."""
    if not xs:
        return v
    if v <= xs[0]:
        return ys[0]
    for i in range(1, len(xs)):
        if v <= xs[i]:
            span = xs[i] - xs[i - 1]
            if span <= 0.0:
                return ys[i]
            t = (v - xs[i - 1]) / span
            return ys[i - 1] + t * (ys[i] - ys[i - 1])
    return ys[-1]
