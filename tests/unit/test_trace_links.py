"""Trace graph links follow the current network path (widgets/repeater_graph.py)."""

from __future__ import annotations

import pytest

pytest.importorskip("gi")

from meshcore_console.ui_gtk.widgets.repeater_graph import trace_links  # noqa: E402

TARGET = "C3D4E5F6"


def _trace(t: float, hops: list[str], snr: float = 5.0) -> dict:
    route = [*hops, TARGET[:2], *reversed(hops)]
    nodes = ["me", *route, "me"]
    legs = [[nodes[i], nodes[i + 1], snr] for i in range(len(nodes) - 1)]
    return {"t": t, "ok": True, "links": legs}


def _keys(links: list[dict]) -> list[tuple]:
    return [(str(lk["a"])[:2], str(lk["b"])[:2]) for lk in links]


def test_without_path_every_traced_link_is_kept() -> None:
    links = trace_links([_trace(1, ["AA"]), _trace(2, ["BB"])])
    assert len(links) == 4  # me-AA, AA-C3, me-BB, BB-C3


def test_path_drops_links_from_an_old_route() -> None:
    traces = [_trace(1, ["AA"]), _trace(2, ["BB"])]
    links = trace_links(traces, path=["me", "BB", TARGET])
    assert _keys(links) == [("me", "BB"), ("BB", "C3")]
    assert all(lk["out"] and lk["ret"] for lk in links)


def test_new_path_links_show_before_they_have_data() -> None:
    links = trace_links([_trace(1, ["AA"])], path=["me", "DD", "AA", TARGET])
    assert _keys(links) == [("me", "DD"), ("DD", "AA"), ("AA", "C3")]
    assert links[0]["out"] == [] and links[2]["out"]


def test_direct_path_is_one_link() -> None:
    links = trace_links([_trace(1, [])], path=["me", TARGET])
    assert _keys(links) == [("me", "C3")]
    assert links[0]["out"] and links[0]["ret"]
