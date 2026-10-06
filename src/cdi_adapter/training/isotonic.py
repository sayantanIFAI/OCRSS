"""Isotonic regression by pool-adjacent-violators (backlog E4-S2), without a new dependency.

``fit(x, y)`` returns the non-decreasing step function that best fits (least squares) the 0/1
outcomes ``y`` against the scores ``x``; ``predict`` interpolates linearly between the fitted
observed score knots and clips outside them - the same model as scikit-learn's IsotonicRegression
with ``increasing=True, out_of_bounds="clip"``.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import groupby


@dataclass(frozen=True)
class Curve:
    x: tuple[float, ...]  # observed scores, strictly increasing
    y: tuple[float, ...]  # fitted probability per block, non-decreasing

    def predict(self, v: float) -> float:
        if not self.x:
            raise ValueError("empty curve")
        if v <= self.x[0]:
            return self.y[0]
        if v >= self.x[-1]:
            return self.y[-1]
        lo, hi = 0, len(self.x) - 1
        while hi - lo > 1:  # the block pair around v
            mid = (lo + hi) // 2
            if self.x[mid] <= v:
                lo = mid
            else:
                hi = mid
        x0, x1, y0, y1 = self.x[lo], self.x[hi], self.y[lo], self.y[hi]
        return y0 + (y1 - y0) * (v - x0) / (x1 - x0)

    def as_json(self) -> dict[str, list[float]]:
        return {"x": list(self.x), "y": list(self.y)}


def fit(x: Sequence[float], y: Sequence[float]) -> Curve:
    if len(x) != len(y) or not x:
        raise ValueError("x and y must be non-empty and of equal length")
    if any(not math.isfinite(v) or not 0 <= v <= 1 for v in (*x, *y)):
        raise ValueError("scores and outcomes must be finite probabilities")
    # Group ties before PAV; retain every observed x so each pooled block stays flat.
    xs: list[float] = []
    blocks: list[tuple[int, int, float, int]] = []
    for xi, group in groupby(sorted(zip(x, y, strict=True)), key=lambda point: point[0]):
        ys = [yi for _, yi in group]
        i = len(xs)
        xs.append(xi)
        blocks.append((i, i + 1, sum(ys), len(ys)))
        while len(blocks) > 1 and blocks[-2][2] / blocks[-2][3] > blocks[-1][2] / blocks[-1][3]:
            right, left = blocks.pop(), blocks.pop()
            blocks.append((left[0], right[1], left[2] + right[2], left[3] + right[3]))
    fitted = [0.0] * len(xs)
    for start, end, total, weight in blocks:
        fitted[start:end] = [total / weight] * (end - start)
    return Curve(tuple(xs), tuple(fitted))
