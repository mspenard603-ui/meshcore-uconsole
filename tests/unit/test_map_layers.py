"""Map layer buttons: node type of a marker, saved on/off state. [map-layers patch]"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("gi")

from meshcore_console.core.models import Peer  # noqa: E402
from meshcore_console.ui_gtk.views import map as map_view  # noqa: E402


@pytest.mark.parametrize(
    ("is_repeater", "node_type", "kind"),
    [
        (True, 2, "repeater"),
        (True, None, "repeater"),
        (False, 3, "room"),
        (False, 1, "companion"),
        (False, None, "companion"),
        (False, 4, "companion"),  # sensors sit with companions
    ],
)
def test_marker_kind(is_repeater: bool, node_type: int | None, kind: str) -> None:
    peer = Peer(peer_id="p", display_name="n", is_repeater=is_repeater, node_type=node_type)
    assert map_view.marker_kind(peer) == kind


def test_layers_default_on_and_round_trip(tmp_path, monkeypatch) -> None:
    path = tmp_path / "map_layers.json"
    monkeypatch.setattr(map_view, "_layers_file", lambda: path)
    assert map_view.load_map_layers() == {"repeater": True, "companion": True, "room": True}
    map_view._save_map_layers({"repeater": True, "companion": False, "room": True})
    assert map_view.load_map_layers()["companion"] is False


def test_layers_ignore_bad_file(tmp_path, monkeypatch) -> None:
    path = tmp_path / "map_layers.json"
    monkeypatch.setattr(map_view, "_layers_file", lambda: path)
    path.write_text("not json")
    assert all(map_view.load_map_layers().values())
    path.write_text(json.dumps({"room": "no", "companion": False, "extra": False}))
    assert map_view.load_map_layers() == {"repeater": True, "companion": False, "room": True}
