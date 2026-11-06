"""Place link labels (the SNR bubbles) clear of node names and of each other.

Pure screen-space geometry, no GTK: the map view converts coordinates to
pixels, calls :func:`place_labels`, and converts the answer back.

Each label first tries to sit on its own link, at the midpoint and then
stepping toward the ends. If every spot on the line is taken it moves off to
either side, further each step, and gets a *leader*: an anchor point on the
link that the map joins to the label with a short line and arrowhead. When
nothing is free, the spot with the least overlap wins.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

Rect = tuple[float, float, float, float]  # x0, y0, x1, y1
Point = tuple[float, float]

_ALONG = (0.5, 0.42, 0.58, 0.34, 0.66, 0.26, 0.74, 0.18, 0.82)
_ALONG_OFF = (0.5, 0.38, 0.62, 0.26, 0.74)
_GAP = 10.0  # px between the line and an off-line label
_PAD = 3.0  # px kept clear around every label


@dataclass(frozen=True)
class LabelSpot:
    center: Point
    anchor: Point | None  # point on the link to point a leader at; None = on the line


def _rect(center: Point, size: Point, pad: float = _PAD) -> Rect:
    (cx, cy), (w, h) = center, size
    return (cx - w / 2 - pad, cy - h / 2 - pad, cx + w / 2 + pad, cy + h / 2 + pad)


def _overlap(a: Rect, b: Rect) -> float:
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    return w * h if w > 0 and h > 0 else 0.0


def _candidates(a: Point, b: Point, size: Point):
    (xa, ya), (xb, yb), (w, h) = a, b, size
    dx, dy = xb - xa, yb - ya
    length = math.hypot(dx, dy)
    if length < 1e-6:
        yield (xa, ya), None
        return
    ux, uy = dx / length, dy / length
    nx, ny = -uy, ux
    # On the line, if the link is long enough to show some of it past the label.
    if length > max(w, h) * 1.5:
        for t in _ALONG:
            yield (xa + dx * t, ya + dy * t), None
    # Off to the side: clear the line by the label's half-extent along the normal.
    half = abs(nx) * w / 2 + abs(ny) * h / 2
    step = max(w, h) + 4
    for k in range(4):
        dist = half + _GAP + k * step
        for t in _ALONG_OFF:
            px, py = xa + dx * t, ya + dy * t
            for side in (1, -1):
                yield (px + side * nx * dist, py + side * ny * dist), (px, py)


def place_labels(links: list[tuple[Point, Point, Point]], obstacles: list[Rect]) -> list[LabelSpot]:
    """One spot per (a, b, label_size) link, in input order.

    *obstacles* are screen rectangles labels must avoid (node names, dots).
    Short links are placed first: they have the fewest spots to choose from.
    """
    order = sorted(range(len(links)), key=lambda i: math.dist(links[i][0], links[i][1]))
    taken: list[Rect] = list(obstacles)
    spots: dict[int, LabelSpot] = {}
    for i in order:
        a, b, size = links[i]
        best: tuple[float, LabelSpot] | None = None
        for center, anchor in _candidates(a, b, size):
            box = _rect(center, size)
            cost = sum(_overlap(box, r) for r in taken)
            if best is None or cost < best[0]:
                best = (cost, LabelSpot(center, anchor))
            if cost == 0:
                break
        assert best is not None
        spots[i] = best[1]
        taken.append(_rect(best[1].center, size))
    return [spots[i] for i in range(len(links))]


def leader_start(center: Point, size: Point, anchor: Point) -> Point:
    """Where a leader from *anchor* meets the edge of the label box."""
    (cx, cy), (w, h) = center, size
    dx, dy = anchor[0] - cx, anchor[1] - cy
    if dx == 0 and dy == 0:
        return center
    scale = min(
        (w / 2) / abs(dx) if dx else math.inf,
        (h / 2) / abs(dy) if dy else math.inf,
    )
    return cx + dx * scale, cy + dy * scale
