"""Small, non-interactive map showing one node and the few miles around it."""

from __future__ import annotations

import math

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk

from meshcore_console.core.models import Peer
from meshcore_console.ui_gtk.helpers import navigate

try:
    gi.require_version("Shumate", "1.0")
    from gi.repository import Shumate

    SHUMATE_AVAILABLE = True
except (ImportError, ValueError):
    SHUMATE_AVAILABLE = False

# Zoom 13 is ~15 m/px at mid latitudes: the 520 px map spans about 5 miles.
MINI_MAP_ZOOM = 13
MINI_MAP_SIZE = 520  # px, square


class MiniMap(Gtk.Overlay):
    """A fixed-zoom map centred on a node; click it to open the node on the Map tab.

    The map ignores input so the mouse wheel scrolls the panel it sits in instead of
    zooming. Build one and call ``show_peer`` again on every refresh: re-creating a
    Shumate map per refresh would reload its tiles each time.
    """

    def __init__(self) -> None:
        super().__init__()
        self._peer_id: str | None = None
        self.set_size_request(MINI_MAP_SIZE, MINI_MAP_SIZE)
        self.set_halign(Gtk.Align.START)
        # The Shumate map asks to expand and GTK passes that up; keep it at a fixed size.
        self.set_hexpand(False)
        self.set_vexpand(False)
        self.set_overflow(Gtk.Overflow.HIDDEN)
        self.add_css_class("mini-map")
        self.set_tooltip_text("Open on the Map\nMap data \u00a9 OpenStreetMap contributors")

        self._map = Shumate.SimpleMap.new()
        self._map.set_show_zoom_buttons(False)
        self._map.get_scale().set_visible(False)
        self._map.get_compass().set_visible(False)
        # The attribution label alone is ~510 px wide; its text moves to the tooltip.
        self._map.get_license().set_visible(False)
        self._map.set_can_target(False)
        registry = Shumate.MapSourceRegistry.new_with_defaults()
        self._map.set_map_source(registry.get_by_id("osm-mapnik"))
        self._viewport = self._map.get_map().get_viewport()
        self._viewport.set_zoom_level(MINI_MAP_ZOOM)

        layer = Shumate.MarkerLayer.new(self._viewport)
        self._map.get_map().add_layer(layer)
        self._marker = Shumate.Marker.new()
        dot = Gtk.DrawingArea()
        dot.set_size_request(18, 18)
        dot.set_draw_func(self._draw_dot)
        self._marker.set_child(dot)
        layer.add_marker(self._marker)

        self.set_child(self._map)

        click = Gtk.GestureClick.new()
        click.connect("released", self._on_clicked)
        self.add_controller(click)

    def show_peer(self, peer: Peer) -> None:
        """Centre on *peer*, which must have a latitude and longitude."""
        self._peer_id = peer.peer_id
        self._marker.set_location(peer.latitude, peer.longitude)
        self._viewport.set_zoom_level(MINI_MAP_ZOOM)
        self._viewport.set_location(peer.latitude, peer.longitude)

    def _draw_dot(self, _area: Gtk.DrawingArea, cr: object, width: int, height: int) -> None:
        cx, cy = width / 2, height / 2
        cr.set_source_rgba(0.937, 0.725, 0.247, 1.0)  # mc_warn, as repeaters on the Map
        cr.arc(cx, cy, 7, 0, 2 * math.pi)
        cr.fill()
        cr.set_source_rgba(0.180, 0.235, 0.286, 1.0)  # mc_border
        cr.arc(cx, cy, 7, 0, 2 * math.pi)
        cr.set_line_width(2)
        cr.stroke()

    def _on_clicked(self, _gesture: Gtk.GestureClick, _n: int, _x: float, _y: float) -> None:
        if self._peer_id is not None:
            navigate(self, "map", ("focus_peer", self._peer_id))
