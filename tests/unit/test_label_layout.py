"""SNR bubble placement on map links (ui_gtk/label_layout.py)."""

from __future__ import annotations

import pytest

from meshcore_console.ui_gtk.label_layout import _overlap, _rect, leader_start, place_labels

SIZE = (56.0, 36.0)  # a two-line "↗ +4.8 / ↙ +5.5" bubble


def test_clear_link_keeps_label_on_the_midpoint() -> None:
    (spot,) = place_labels([((0, 0), (400, 0), SIZE)], [])
    assert spot.center == (200, 0)
    assert spot.anchor is None


def test_label_slides_along_the_line_past_a_name() -> None:
    name = (170.0, -15.0, 230.0, 15.0)  # a node label sitting on the midpoint
    (spot,) = place_labels([((0, 0), (400, 0), SIZE)], [name])
    assert spot.anchor is None  # still on the line, no leader needed
    assert spot.center[1] == 0
    assert _overlap(_rect(spot.center, SIZE), name) == 0


def test_crowded_line_moves_off_with_a_leader() -> None:
    # Names along the whole link: no spot on the line is free.
    names = [(x, -14.0, x + 50.0, 14.0) for x in range(0, 400, 45)]
    (spot,) = place_labels([((0, 0), (400, 0), SIZE)], names)
    assert spot.anchor is not None
    assert spot.anchor[1] == 0  # the leader points at the link itself
    box = _rect(spot.center, SIZE)
    assert all(_overlap(box, r) == 0 for r in names)


def test_two_labels_on_crossing_links_do_not_overlap() -> None:
    links = [((0, 0), (400, 400), SIZE), ((0, 400), (400, 0), SIZE)]
    a, b = place_labels(links, [])
    assert _overlap(_rect(a.center, SIZE), _rect(b.center, SIZE)) == 0


def test_many_parallel_links_all_get_free_spots() -> None:
    links = [((0, y), (300, y), SIZE) for y in range(0, 60, 12)]  # 12 px apart: crowded
    spots = place_labels(links, [])
    boxes = [_rect(s.center, SIZE) for s in spots]
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            assert _overlap(boxes[i], boxes[j]) == 0, (i, j)


def test_short_link_still_gets_a_spot() -> None:
    (spot,) = place_labels([((0, 0), (20, 0), SIZE)], [])
    assert spot.anchor is not None  # too short to sit on: beside it, with a leader


def test_results_follow_input_order() -> None:
    links = [((0, 0), (600, 0), SIZE), ((0, 100), (100, 100), SIZE)]
    long_spot, short_spot = place_labels(links, [])
    assert long_spot.center[1] == 0
    assert abs(short_spot.center[1] - 100) < 80


@pytest.mark.parametrize(
    ("anchor", "expected"),
    [((100, 0), (28, 0)), ((0, 100), (0, 18)), ((-100, 0), (-28, 0))],
)
def test_leader_starts_on_the_label_edge(anchor, expected) -> None:
    assert leader_start((0, 0), SIZE, anchor) == pytest.approx(expected)
