"""Map view showing peer locations on OpenStreetMap tiles."""

from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Shumate", "1.0")

from gi.repository import GLib, Gtk, Pango

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

try:
    from gi.repository import Shumate

    SHUMATE_AVAILABLE = True
except ImportError:
    SHUMATE_AVAILABLE = False
    print("[MapView] libshumate not available", file=sys.stderr)

from meshcore_console.core.models import Peer
from meshcore_console.core.radio import format_rssi, format_snr
from meshcore_console.core.services import MeshcoreService
from meshcore_console.ui_gtk.helpers import clear_children, navigate
from meshcore_console.ui_gtk.layout import Layout
from meshcore_console.ui_gtk.state import UiEventStore
from meshcore_console.core.time import to_local
from meshcore_console.platform.mbtiles import MBTilesReader, find_mbtiles_files
from meshcore_console.ui_gtk.widgets import DetailRow, EmptyState


# Tile URL templates - Shumate uses {z}, {x}, {y} placeholders
CARTO_DARK_URL = "https://basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png"
OSM_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"


def _map_state_file():
    from meshcore_console.meshcore.paths import config_dir

    return config_dir() / "map_view.json"


def _load_map_view() -> tuple[float | None, tuple[float, float] | None]:
    """(zoom, (lat, lon)) saved by the last session; either may be None."""
    import json
    import math

    try:
        data = json.loads(_map_state_file().read_text())
        zoom, lat, lon = data.get("zoom"), data.get("lat"), data.get("lon")
    except (OSError, ValueError, AttributeError):
        return None, None

    def num(v: object) -> bool:
        return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)

    good_zoom = float(zoom) if num(zoom) and 0 <= zoom <= 20 else None
    good_loc = (
        (float(lat), float(lon))
        if num(lat) and num(lon) and -90 <= lat <= 90 and -180 <= lon <= 180
        else None
    )
    return good_zoom, good_loc


def _save_map_view(zoom: float, lat: float, lon: float) -> None:
    import json

    try:
        path = _map_state_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"zoom": round(zoom, 2), "lat": round(lat, 6), "lon": round(lon, 6)})
        )
    except OSError as e:
        logger.warning("Could not save map view: %s", e)


_ZOOM_OUT_MS = 250  # double right-click zoom-out animation

_HASH_CSS = {"done": False}


def _marker_hash_css() -> None:  # [marker-hash patch]
    if _HASH_CSS["done"]:
        return
    from gi.repository import Gdk

    provider = Gtk.CssProvider()
    provider.load_from_data(
        b".map-marker-hash { font-family: 'DejaVu Sans Mono', monospace; font-size: 11px; font-weight: 700;"
        b" color: #ffe880; background: rgba(160, 120, 40, 0.92); border-radius: 3px; padding: 0 4px; }"
        b".map-marker-elev { font-family: 'DejaVu Sans Mono', monospace; font-size: 11px; font-weight: 700;"
        b" color: #b8f5df; background: rgba(25, 120, 90, 0.92); border-radius: 3px; padding: 0 4px; }"
    )
    Gtk.StyleContext.add_provider_for_display(
        Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )
    _HASH_CSS["done"] = True


class MapView(Gtk.Box):
    """Map view with peer markers and details panel."""

    def __init__(self, service: MeshcoreService, event_store: UiEventStore, layout: Layout) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self._service = service
        self._event_store = event_store
        self._layout = layout
        self._selected_peer: Peer | None = None
        self._peer_markers: dict[str, object] = {}  # peer_id -> marker
        self._device_marker: object | None = None
        self._mbtiles: MBTilesReader | None = None
        self._last_peer_count = 0
        self._shown_gps_errors: set[str] = set()  # Track shown errors to avoid repeats
        self._following = False  # Whether map follows device location
        from meshcore_console.platform.elevation import get_store

        self._elevation = get_store()
        self._elevation.add_listener(self._on_elevation_tile)
        self._programmatic_move = False  # Suppress follow-disable during go_to

        if not SHUMATE_AVAILABLE:
            self._build_fallback_ui()
            return

        self._build_map_ui()
        self._event_store.connect("events-available", self._on_events_available)
        GLib.timeout_add(2000, self._poll_gps)

        # In mock mode, auto-cycle GPS position every 5 seconds
        if self._service.is_mock_mode():
            GLib.timeout_add(5000, self._cycle_mock_gps)

    def _build_fallback_ui(self) -> None:
        """Build fallback UI when Shumate is not available."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_hexpand(True)
        box.set_vexpand(True)

        title = Gtk.Label(label="Map unavailable")
        title.add_css_class("panel-title")
        title.set_halign(Gtk.Align.CENTER)
        title.set_valign(Gtk.Align.END)
        title.set_vexpand(True)
        box.append(title)

        hint = EmptyState("Install gir1.2-shumate-1.0 to enable the map view.")
        hint.set_vexpand(True)
        hint.set_valign(Gtk.Align.START)
        box.append(hint)

        self.append(box)

    def _build_map_ui(self) -> None:
        """Build the map UI with Shumate."""
        # Main horizontal split: map | details panel
        # Map takes remaining space, details panel is fixed width

        # Use overlay as the main container to avoid re-parenting
        overlay = Gtk.Overlay.new()
        overlay.set_hexpand(True)
        overlay.set_vexpand(True)
        self.append(overlay)

        # Create Shumate map
        self._map = Shumate.SimpleMap.new()
        self._map.set_hexpand(True)
        self._map.set_vexpand(True)

        # Hide built-in controls - we'll add our own styled ones
        self._map.set_show_zoom_buttons(False)
        self._map.get_scale().set_visible(False)
        self._map.get_compass().set_visible(False)

        # Get the map widget and viewport
        map_widget = self._map.get_map()
        self._viewport = map_widget.get_viewport()

        # Set up tile source - CartoDB Dark Matter
        self._tile_source = self._create_tile_source()
        self._map.set_map_source(self._tile_source)

        # Set initial view - center on SF Bay Area if mock mode, otherwise US
        if self._service.is_mock_mode():
            # SF Bay Area for mock data
            self._viewport.set_zoom_level(12)
            self._viewport.set_location(37.7749, -122.4194)
        else:
            # Centered on US
            self._viewport.set_zoom_level(4)
            self._viewport.set_location(39.8283, -98.5795)
        saved_zoom, saved_loc = _load_map_view()
        if saved_zoom is not None:
            self._viewport.set_zoom_level(saved_zoom)
        if saved_loc is not None:
            self._viewport.set_location(*saved_loc)

        # Double left-click zooms in (Shumate); double right-click zooms out,
        # keeping the point under the pointer where it is.
        zoom_out_click = Gtk.GestureClick.new()
        zoom_out_click.set_button(3)
        zoom_out_click.connect("pressed", self._on_right_double_click, map_widget)
        map_widget.add_controller(zoom_out_click)

        # Remember zoom and position across sessions, written once they settle.
        self._view_save_id = 0
        for prop in ("zoom-level", "latitude", "longitude"):
            self._viewport.connect(f"notify::{prop}", self._on_view_changed)

        # Detect user panning to disable follow mode
        self._viewport.connect("notify::latitude", self._on_viewport_moved)
        self._viewport.connect("notify::longitude", self._on_viewport_moved)

        # Create marker layer
        self._marker_layer = Shumate.MarkerLayer.new(self._viewport)
        map_widget.add_layer(self._marker_layer)

        # Add map as the base child of overlay
        overlay.set_child(self._map)

        # Add control overlays
        self._add_map_controls(overlay)
        self._add_split_toggle(overlay)  # [panel-split patch]

        # Details panel (initially hidden)
        self._details_panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self._details_panel.add_css_class("panel-card")
        self._details_panel.add_css_class("map-details-panel")
        self._details_panel.set_size_request(self._layout.map_details_width, -1)
        self._details_panel.set_visible(False)
        self.append(self._details_panel)
        if getattr(self, "_split_btn", None) is not None:  # [panel-split fix]
            self._details_panel.connect(
                "notify::visible", lambda p, _s: self._split_btn.set_visible(p.get_visible())
            )

        self._details_title = Gtk.Label(label="")
        self._details_title.add_css_class("panel-title")
        self._details_title.set_halign(Gtk.Align.START)
        self._details_panel.append(self._details_title)

        self._details_content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        # [panel-split patch] the content scrolls inside a fixed-width panel, so wide
        # output (trace tables, errors) wraps or scrolls instead of pushing the map aside
        self._details_scroll = Gtk.ScrolledWindow()
        self._details_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._details_scroll.set_propagate_natural_width(False)
        self._details_scroll.set_vexpand(True)
        self._details_scroll.set_child(self._details_content)
        self._details_panel.append(self._details_scroll)

        # SNR bubble styles go in now: adding a CSS provider later (on the first
        # Plot path / favourite links click) restyles the whole window, which
        # flashed the map blank for a frame.
        from gi.repository import Gdk

        provider = Gtk.CssProvider()
        provider.load_from_data(self._SNR_CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        # Load MBTiles if available for offline support
        self._load_mbtiles()

        # Initial marker load
        self._refresh_markers()

    def _create_tile_source(self) -> Shumate.MapSource:
        """Create a tile source for the map."""
        # Use built-in OSM source which is known to work
        registry = Shumate.MapSourceRegistry.new_with_defaults()
        osm = registry.get_by_id("osm-mapnik")
        if osm is not None:
            print(f"[MapView] Using built-in OSM source: {osm.get_name()}", file=sys.stderr)
            return osm

        # Fallback: create OSM source manually
        print(f"[MapView] Creating manual OSM source with URL: {OSM_URL}", file=sys.stderr)
        return Shumate.RasterRenderer.new_full_from_url(
            "osm-tiles",
            "OpenStreetMap",
            "© OpenStreetMap contributors",
            "https://www.openstreetmap.org/copyright",
            0,  # min zoom
            19,  # max zoom
            256,  # tile size
            Shumate.MapProjection.MERCATOR,
            OSM_URL,
        )

    def _add_map_controls(self, overlay: Gtk.Overlay) -> None:
        """Add zoom and location controls as overlay."""
        # Zoom controls in bottom-right
        zoom_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        zoom_box.set_halign(Gtk.Align.END)
        zoom_box.set_valign(Gtk.Align.END)
        zoom_box.set_margin_end(12)
        zoom_box.set_margin_bottom(12)

        # [favorite-links patch] plot which favourite repeaters hear each other.
        # Three states, one per click: off -> links (green) -> favourites only
        # (purple: links, and every other repeater hidden) -> off.
        self._fav_mode = "off"
        self._fav_links_btn = Gtk.Button()
        icon = Gtk.DrawingArea()
        icon.set_size_request(18, 18)
        icon.set_draw_func(self._draw_fav_links_icon)
        self._fav_links_btn.set_child(icon)
        self._fav_links_btn.add_css_class("map-control-button")
        self._fav_links_btn.update_property(
            [Gtk.AccessibleProperty.LABEL], ["Favourite repeater links"]
        )
        self._fav_links_btn.connect("clicked", self._on_fav_links_clicked)
        zoom_box.append(self._fav_links_btn)

        zoom_in = Gtk.Button.new_from_icon_name("zoom-in-symbolic")
        zoom_in.add_css_class("map-control-button")
        zoom_in.update_property([Gtk.AccessibleProperty.LABEL], ["Zoom in"])
        zoom_in.connect("clicked", self._on_zoom_in)
        zoom_box.append(zoom_in)

        zoom_out = Gtk.Button.new_from_icon_name("zoom-out-symbolic")
        zoom_out.add_css_class("map-control-button")
        zoom_out.update_property([Gtk.AccessibleProperty.LABEL], ["Zoom out"])
        zoom_out.connect("clicked", self._on_zoom_out)
        zoom_box.append(zoom_out)

        overlay.add_overlay(zoom_box)

        # Center on device button in bottom-left
        center_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        center_box.set_halign(Gtk.Align.START)
        center_box.set_valign(Gtk.Align.END)
        center_box.set_margin_start(12)
        center_box.set_margin_bottom(12)

        self._center_btn = Gtk.Button.new_from_icon_name("find-location-symbolic")
        self._center_btn.add_css_class("map-control-button")
        self._center_btn.set_tooltip_text("Center on device")
        self._center_btn.update_property([Gtk.AccessibleProperty.LABEL], ["Center on device"])
        self._center_btn.connect("clicked", self._on_center_device)
        center_box.append(self._center_btn)

        # In mock mode, add a "simulate movement" button
        if self._service.is_mock_mode():
            move_btn = Gtk.Button.new_from_icon_name("media-skip-forward-symbolic")
            move_btn.add_css_class("map-control-button")
            move_btn.set_tooltip_text("Simulate GPS movement")
            move_btn.connect("clicked", self._on_simulate_movement)
            center_box.append(move_btn)

        overlay.add_overlay(center_box)

        # Mock mode indicator in top-left
        if self._service.is_mock_mode():
            mock_label = Gtk.Label(label="MOCK MODE")
            mock_label.add_css_class("status-pill")
            mock_label.add_css_class("warn")
            mock_label.set_halign(Gtk.Align.START)
            mock_label.set_valign(Gtk.Align.START)
            mock_label.set_margin_start(12)
            mock_label.set_margin_top(12)
            overlay.add_overlay(mock_label)

    # --- [panel-split patch] --------------------------------------------------
    # A button on the map switches the width between map 2/3 + panel 1/3 and
    # map 1/3 + panel 2/3. Shown while the details panel is open.
    def _add_split_toggle(self, overlay: Gtk.Overlay) -> None:
        self._panel_wide = False
        btn = Gtk.Button(label="\u21e4")  # [panel-split symbol] divider moves left: wider panel
        btn.add_css_class("map-split-toggle")
        from gi.repository import Gdk

        css = Gtk.CssProvider()
        css.load_from_data(
            b".map-split-toggle label { font-size: 22px; font-weight: 800; color: #e7eef8; }"
        )
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1
        )
        btn.add_css_class("map-control-button")
        btn.set_halign(Gtk.Align.END)
        btn.set_valign(Gtk.Align.START)
        btn.set_margin_end(12)
        btn.set_margin_top(12)
        btn.set_tooltip_text("Give the details panel two thirds of the width")
        btn.connect("clicked", self._on_split_toggle)
        btn.set_visible(False)  # [panel-split fix] shown with the details panel; hooked up below
        overlay.add_overlay(btn)
        self._split_btn = btn

    def _on_split_toggle(self, btn: Gtk.Button) -> None:
        self._panel_wide = not getattr(self, "_panel_wide", False)
        btn.set_label("\u21e5" if self._panel_wide else "\u21e4")  # [panel-split symbol]
        btn.set_tooltip_text(
            "Give the map two thirds of the width"
            if self._panel_wide
            else "Give the details panel two thirds of the width"
        )
        self._apply_split()

    def _apply_split(self, tries: int = 20) -> None:
        total = self.get_width()
        if total < 300:  # not laid out yet (e.g. just navigated here): retry for up to 2 s
            if tries > 0:
                GLib.timeout_add(100, lambda: (self._apply_split(tries - 1), False)[1])
            return
        share = 2 / 3 if getattr(self, "_panel_wide", False) else 1 / 3
        self._details_panel.set_size_request(int(total * share) - 16, -1)  # 16 = panel margins

    def _on_view_changed(self, _viewport: object, _pspec: object) -> None:
        if self._view_save_id:
            GLib.source_remove(self._view_save_id)
        self._view_save_id = GLib.timeout_add(500, self._save_view_now)

    def _save_view_now(self) -> bool:
        self._view_save_id = 0
        vp = self._viewport
        _save_map_view(vp.get_zoom_level(), vp.get_latitude(), vp.get_longitude())
        return False

    def _on_right_double_click(
        self, _gesture: Gtk.GestureClick, n_press: int, x: float, y: float, map_widget
    ) -> None:
        if n_press != 2:
            return
        current = self._viewport.get_zoom_level()
        target = max(current - 1, 0)
        if target == current:
            return
        logger.debug("UI: map double right-click zoom out %s -> %s", current, target)
        import math

        lat, lon = self._viewport.get_latitude(), self._viewport.get_longitude()
        if not self._following:  # keep the point under the pointer where it is
            # One level out doubles the map distance per pixel, so the new centre
            # is the pointer's point plus twice its (Web Mercator) offset to it.
            p_lat, p_lon = self._viewport.widget_coords_to_location(map_widget, x, y)

            def merc(la: float, lo: float) -> tuple[float, float]:
                s = math.sin(math.radians(max(-85.0, min(85.0, la))))
                return (lo + 180.0) / 360.0, 0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)

            (px, py), (cx, cy) = merc(p_lat, p_lon), merc(lat, lon)
            nx, ny = px + 2 * (cx - px), min(1.0, max(0.0, py + 2 * (cy - py)))
            lon = (nx % 1.0) * 360.0 - 180.0
            lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * ny))))
        # Animated, like Shumate's own double-click zoom in. The animation's
        # moves aren't the user panning: don't let them end follow mode.
        self._zoom_animating = True
        map_widget.go_to_full_with_duration(lat, lon, target, _ZOOM_OUT_MS)

        def done() -> bool:
            self._zoom_animating = False
            return False

        GLib.timeout_add(_ZOOM_OUT_MS + 100, done)

    def _on_zoom_in(self, _button: Gtk.Button) -> None:
        """Zoom in on the map."""
        logger.debug("UI: map zoom in")
        self._programmatic_move = True
        current = self._viewport.get_zoom_level()
        self._viewport.set_zoom_level(min(current + 1, 19))
        self._programmatic_move = False

    def _on_zoom_out(self, _button: Gtk.Button) -> None:
        """Zoom out on the map."""
        logger.debug("UI: map zoom out")
        self._programmatic_move = True
        current = self._viewport.get_zoom_level()
        self._viewport.set_zoom_level(max(current - 1, 0))
        self._programmatic_move = False

    def _on_center_device(self, _button: Gtk.Button) -> None:
        """Center the map on the device location and enable follow mode."""
        logger.debug("UI: center on device clicked")
        location = self._service.get_device_location()
        if location:
            lat, lon = location
            self._set_following(True)
            self._programmatic_move = True
            self._viewport.set_zoom_level(12)
            self._viewport.set_location(lat, lon)
            self._programmatic_move = False
        else:
            # No fix yet - show feedback
            if self._service.has_gps_fix():
                self._show_toast("GPS location unavailable")
            else:
                self._show_toast("GPS acquiring satellites...")

    def _on_viewport_moved(self, viewport: object, pspec: object) -> None:
        """Disable follow mode when user pans the map."""
        if (
            not self._programmatic_move
            and not getattr(self, "_zoom_animating", False)
            and self._following
        ):
            self._set_following(False)

    def _set_following(self, active: bool) -> None:
        """Toggle follow mode and update button visual."""
        self._following = active
        if active:
            self._center_btn.add_css_class("map-control-active")
        else:
            self._center_btn.remove_css_class("map-control-active")

    def _on_simulate_movement(self, _button: Gtk.Button) -> None:
        """Manually trigger GPS position cycle in mock mode."""
        logger.debug("UI: simulate GPS movement clicked")
        self._service.cycle_mock_gps()
        self._update_device_marker()

    def _cycle_mock_gps(self) -> bool:
        """Auto-cycle mock GPS position. GLib timeout callback."""
        if not self._service.is_mock_mode():
            return False
        self._service.cycle_mock_gps()
        return True  # Continue timeout

    def _load_mbtiles(self) -> None:
        """Load MBTiles file for offline tiles if available."""
        files = find_mbtiles_files()
        if files:
            self._mbtiles = MBTilesReader(files[0])
            if self._mbtiles.open():
                metadata = self._mbtiles.get_metadata()
                name = metadata.get("name", files[0].name)
                print(f"[MapView] Loaded offline tiles: {name}", file=sys.stderr)

    def _on_events_available(self, _store: object) -> None:
        """Handle events-available signal — refresh peer markers."""
        peers = self._service.list_peers()
        peers_with_location = [
            p for p in peers if p.latitude is not None and p.longitude is not None
        ]
        if len(peers_with_location) != self._last_peer_count:
            self._refresh_markers()

    def _poll_gps(self) -> bool:
        """Poll GPS for new data (serial port). GLib timeout callback."""
        self._service.poll_gps()
        self._check_gps_status()
        self._update_device_marker()
        return True

    def _check_gps_status(self) -> None:
        """Check GPS status and show toast for errors."""
        error = self._service.get_gps_error()
        if error and error not in self._shown_gps_errors:
            self._shown_gps_errors.add(error)
            logger.warning("GPS error: %s", error)
            self._show_toast(f"GPS: {error}")

    def _show_toast(self, message: str) -> None:
        """Show a toast message via the main window's toast overlay."""
        logger.debug("UI: toast %r", message)
        try:
            import gi

            gi.require_version("Adw", "1")
            from gi.repository import Adw

            root = self.get_root()
            if root is None:
                return

            # Look for toast overlay on the main window
            toast_overlay = getattr(root, "_toast_overlay", None)
            if toast_overlay is not None and hasattr(toast_overlay, "add_toast"):
                toast_overlay.add_toast(Adw.Toast.new(message))
        except Exception as e:
            print(f"[MapView] Toast error: {e}", file=sys.stderr)

    def _refresh_markers(self) -> None:
        """Refresh all peer markers."""
        peers = self._service.list_peers()
        self._last_peer_count = sum(1 for p in peers if p.latitude is not None)

        # Track which peers we've seen
        seen_ids: set[str] = set()

        for peer in peers:
            if peer.latitude is None or peer.longitude is None:
                continue

            seen_ids.add(peer.peer_id)

            if peer.peer_id in self._peer_markers:
                # Update existing marker position
                marker = self._peer_markers[peer.peer_id]
                kind = (peer.is_repeater, getattr(peer, "node_type", None))  # [room-marker patch]
                if getattr(marker, "_kind", kind) != kind:  # type learned later: redraw
                    self._marker_layer.remove_marker(marker)
                    marker = self._create_peer_marker(peer)
                    self._peer_markers[peer.peer_id] = marker
                    self._marker_layer.add_marker(marker)
                marker.set_location(peer.latitude, peer.longitude)
                self._set_marker_elevation(marker, peer)
            else:
                # Create new marker
                marker = self._create_peer_marker(peer)
                self._peer_markers[peer.peer_id] = marker
                self._marker_layer.add_marker(marker)

        # Remove markers for peers no longer in list
        for peer_id in list(self._peer_markers.keys()):
            if peer_id not in seen_ids:
                marker = self._peer_markers.pop(peer_id)
                self._marker_layer.remove_marker(marker)

        self._apply_marker_filter()

        # Update device marker
        self._update_device_marker()

    def _create_peer_marker(self, peer: Peer) -> Shumate.Marker:
        """Create a marker for a peer."""
        marker = Shumate.Marker.new()
        marker.set_location(peer.latitude, peer.longitude)

        # Create marker widget
        marker_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        marker_box.set_halign(Gtk.Align.CENTER)

        # [marker-anchor patch] invisible twin of the name above the dot, so the dot
        # (not the dot + name block) sits on the node's coordinate
        twin = Gtk.Label(label=peer.display_name)
        twin.add_css_class("map-marker-label")
        twin.set_ellipsize(Pango.EllipsizeMode.END)
        twin.set_max_width_chars(12)
        twin.set_opacity(0.0)
        twin.set_can_target(False)
        marker_box.append(twin)
        # Marker dot
        dot = Gtk.DrawingArea()
        dot.set_size_request(16, 16)
        dot.set_draw_func(self._draw_marker_dot, peer)
        marker_box.append(dot)

        # Label
        label = Gtk.Label(label=peer.display_name)
        label.add_css_class("map-marker-label")
        label.set_ellipsize(Pango.EllipsizeMode.END)
        label.set_max_width_chars(12)
        marker_box.append(label)
        if peer.is_repeater and peer.public_key:  # [marker-hash patch] ID hash under the name
            _marker_hash_css()
            for visible in (True, False):
                tag = Gtk.Label(label=peer.public_key[:4].upper())
                tag.add_css_class("map-marker-hash")
                if visible:
                    # Ground elevation (green) to the left of the hash, once known.
                    row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=3)
                    row.set_halign(Gtk.Align.CENTER)
                    elev = Gtk.Label()
                    elev.add_css_class("map-marker-elev")
                    elev.set_tooltip_text("Ground elevation (SRTM)")
                    elev.set_visible(False)
                    row.append(elev)
                    row.append(tag)
                    marker_box.append(row)
                    setattr(marker, "_elev_label", elev)
                    self._set_marker_elevation(marker, peer)
                else:  # invisible twin on top keeps the dot on the coordinate
                    tag.set_halign(Gtk.Align.CENTER)
                    tag.set_opacity(0.0)
                    tag.set_can_target(False)
                    marker_box.prepend(tag)

        marker.set_child(marker_box)

        # Store peer reference for click handling
        setattr(marker, "_peer", peer)
        setattr(
            marker, "_kind", (peer.is_repeater, getattr(peer, "node_type", None))
        )  # [room-marker patch]

        # Make marker clickable
        click = Gtk.GestureClick.new()
        click.connect("released", self._on_marker_clicked, marker)
        marker_box.add_controller(click)

        return marker

    def _set_marker_elevation(self, marker: Shumate.Marker, peer: Peer) -> None:
        """Fill the marker's green elevation square, if the tile is on disk yet."""
        label = getattr(marker, "_elev_label", None)
        if label is None or getattr(marker, "_elev_done", None) == (
            peer.latitude,
            peer.longitude,
        ):
            return
        metres = self._elevation.elevation(peer.latitude, peer.longitude)
        if metres is None:
            label.set_visible(False)  # tile still downloading, or no data here
            return
        label.set_text(f"{metres:.0f}m")
        label.set_visible(True)
        setattr(marker, "_elev_done", (peer.latitude, peer.longitude))

    def _on_elevation_tile(self, _name: str) -> None:
        """A tile finished downloading (worker thread): fill in the squares."""

        def fill() -> bool:
            for peer in self._service.list_peers():
                marker = self._peer_markers.get(peer.peer_id)
                if marker is not None:
                    self._set_marker_elevation(marker, peer)
            return False

        GLib.idle_add(fill)

    def _draw_marker_dot(
        self,
        area: Gtk.DrawingArea,
        cr: object,  # cairo.Context
        width: int,
        height: int,
        peer: Peer,
    ) -> None:
        """Draw a marker dot."""
        if getattr(peer, "node_type", None) == 3 and not peer.is_repeater:  # [room-marker patch]
            cx, cy = width / 2, height / 2
            cr.set_source_rgba(0.663, 0.545, 1.0, 1.0)  # #a98bff violet
            cr.arc(cx, cy, 6.5, 0, 2 * 3.14159)
            cr.fill()
            cr.set_source_rgba(0.180, 0.235, 0.286, 1.0)  # #2e3a49 border, as other dots
            cr.arc(cx, cy, 6.5, 0, 2 * 3.14159)
            cr.set_line_width(2)
            cr.stroke()
            cr.set_source_rgba(1.0, 1.0, 1.0, 1.0)  # white centre: tells it apart without colour
            cr.arc(cx, cy, 2.2, 0, 2 * 3.14159)
            cr.fill()
            return
        # Choose color based on peer type
        if peer.is_repeater:
            # Warn color (orange)
            cr.set_source_rgba(0.937, 0.725, 0.247, 1.0)  # #efb93f
        else:
            # Text color (light)
            cr.set_source_rgba(0.906, 0.933, 0.973, 1.0)  # #e7eef8

        # Draw filled circle
        cr.arc(width / 2, height / 2, 6, 0, 2 * 3.14159)
        cr.fill()

        # Draw border
        cr.set_source_rgba(0.180, 0.235, 0.286, 1.0)  # #2e3a49
        cr.arc(width / 2, height / 2, 6, 0, 2 * 3.14159)
        cr.set_line_width(2)
        cr.stroke()

    def _update_device_marker(self) -> None:
        """Update or create the device location marker."""
        location = self._service.get_device_location()

        if location is None:
            if self._device_marker is not None:
                self._marker_layer.remove_marker(self._device_marker)
                self._device_marker = None
            if self._following:
                self._set_following(False)
            return

        lat, lon = location

        if self._device_marker is None:
            self._device_marker = self._create_device_marker(lat, lon)
            self._marker_layer.add_marker(self._device_marker)
        else:
            self._device_marker.set_location(lat, lon)

        # Keep map centered on device when following
        if self._following:
            self._programmatic_move = True
            self._viewport.set_location(lat, lon)
            self._programmatic_move = False

    def _create_device_marker(self, lat: float, lon: float) -> Shumate.Marker:
        """Create a marker for the device location."""
        marker = Shumate.Marker.new()
        marker.set_location(lat, lon)

        # Create marker widget with pulsing effect
        marker_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        marker_box.set_halign(Gtk.Align.CENTER)

        twin = Gtk.Label(label="You")  # [marker-anchor patch] keeps the dot on the coordinate
        twin.add_css_class("map-marker-label")
        twin.set_opacity(0.0)
        twin.set_can_target(False)
        marker_box.append(twin)
        # Marker dot with accent color
        dot = Gtk.DrawingArea()
        dot.set_size_request(20, 20)
        dot.set_draw_func(self._draw_device_dot, None)
        marker_box.append(dot)

        # Label
        label = Gtk.Label(label="You")
        label.add_css_class("map-marker-label")
        marker_box.append(label)

        marker.set_child(marker_box)
        return marker

    def _draw_device_dot(
        self,
        area: Gtk.DrawingArea,
        cr: object,  # cairo.Context
        width: int,
        height: int,
        _data: object,
    ) -> None:
        """Draw the device marker dot with accent color."""
        # Outer ring (semi-transparent)
        cr.set_source_rgba(0.208, 0.761, 0.608, 0.3)  # #35c29b at 30%
        cr.arc(width / 2, height / 2, 9, 0, 2 * 3.14159)
        cr.fill()

        # Inner dot (solid accent)
        cr.set_source_rgba(0.208, 0.761, 0.608, 1.0)  # #35c29b
        cr.arc(width / 2, height / 2, 5, 0, 2 * 3.14159)
        cr.fill()

    def _on_marker_clicked(
        self,
        gesture: Gtk.GestureClick,
        n_press: int,
        x: float,
        y: float,
        marker: Shumate.Marker,
    ) -> None:
        """Handle marker click to show details."""
        peer = getattr(marker, "_peer", None)
        if peer is None:
            return

        logger.debug("UI: map marker clicked peer=%s", peer.display_name)
        self._selected_peer = peer
        self._show_peer_details(peer)

    def _show_peer_details(self, peer: Peer) -> None:
        """Show details panel for a peer."""
        self._details_title.set_text(peer.display_name)
        self._clear_details()
        root = self.get_root()  # [map-select patch] Peers selects it when next shown
        if root is not None:
            seq = getattr(root, "_map_selected", (None, 0))[1] + 1
            root._map_selected = (peer.peer_id, seq)
        self._details_title.set_wrap(
            True
        )  # [marker-hash patch] long names wrap, never widen the panel
        self._details_title.set_max_width_chars(26)
        self._details_title.set_xalign(0.0)
        if peer.is_repeater and peer.public_key:  # ID hash next to the name, as on the marker
            self._details_title.set_markup(
                # "Name · 59B9", as the header shows this node, with the hash in bold
                f"{GLib.markup_escape_text(peer.display_name)} \u00b7 "
                f"<b>{peer.public_key[:4].upper()}</b>"
            )

        # Type badge
        _room = (
            getattr(peer, "node_type", None) == 3 and not peer.is_repeater
        )  # [room-marker patch]
        type_label = Gtk.Label(
            label="Repeater" if peer.is_repeater else ("Room server" if _room else "Contact")
        )
        type_label.add_css_class("status-pill")
        type_label.add_css_class("warn" if peer.is_repeater else ("room" if _room else "ok"))
        type_label.set_halign(Gtk.Align.START)
        self._details_content.append(type_label)

        # [map-path patch] Network path: you -> hops -> this node (as in Peers)
        from meshcore_console.ui_gtk.widgets import PathVisualization
        from meshcore_console.ui_gtk.widgets.node_badge import STYLE_DEFAULT, STYLE_SELF

        hops = list(peer.last_path or [])
        path_title = Gtk.Label(label="Network path")
        path_title.add_css_class("panel-muted")
        path_title.set_halign(Gtk.Align.START)
        path_title.set_margin_top(4)
        self._details_content.append(path_title)
        self_key = self._service.get_self_public_key()
        path = PathVisualization(
            hops=hops,
            peers=self._service.list_peers(),
            start=(
                (self_key or "")[:2].upper() or "Me",
                self._service.get_status().node_id,
                None,
                STYLE_SELF,
            ),
            end=(
                (peer.public_key or peer.display_name)[:2].upper(),
                peer.display_name,
                peer,
                STYLE_DEFAULT,
            ),
        )
        path.set_tooltip_text(
            "Direct (heard with no hops)"
            if not hops
            else f"{len(hops)} hop{'s' if len(hops) != 1 else ''}"
        )
        path.set_min_children_per_line(min(len(hops) + 2, 6))  # one row, as in the Admin header
        self._details_content.append(path)
        self._link_path_badges_to_map(path)  # [map-path-click patch]
        if peer.is_repeater:  # [trace patch]
            self._details_content.append(self._trace_section(peer))

        # Location
        if peer.latitude is not None and peer.longitude is not None:
            coord_str = f"{peer.latitude:.6f}, {peer.longitude:.6f}"
            self._details_content.append(DetailRow("Location:", coord_str))

        if peer.location_updated:
            time_str = to_local(peer.location_updated).strftime("%H:%M")
            self._details_content.append(DetailRow("Updated:", time_str))

        # Signal info
        if peer.rssi is not None:
            self._details_content.append(DetailRow("RSSI:", format_rssi(peer.rssi)))
        if peer.snr is not None:
            self._details_content.append(DetailRow("SNR:", format_snr(peer.snr)))

        # Last seen
        if peer.last_advert_time:
            time_str = to_local(peer.last_advert_time).strftime("%b %d at %H:%M")
            self._details_content.append(DetailRow("Last seen:", time_str))

        if peer.public_key:  # [map-key patch] the node's full address (public key)
            key_title = Gtk.Label(label="Public key")
            key_title.add_css_class("panel-muted")
            key_title.set_halign(Gtk.Align.START)
            key_title.set_margin_top(6)
            self._details_content.append(key_title)
            key = peer.public_key.lower()
            key_label = Gtk.Label(label=" ".join(key[i : i + 4] for i in range(0, len(key), 4)))
            key_label.add_css_class("analyzer-raw")
            key_label.set_halign(Gtk.Align.START)
            key_label.set_xalign(0.0)
            key_label.set_wrap(True)
            key_label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
            key_label.set_max_width_chars(40)
            key_label.set_selectable(True)
            self._details_content.append(key_label)

        # Action buttons (only for contacts)
        if not peer.is_repeater:
            actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            actions.set_margin_top(16)

            message_btn = Gtk.Button.new_with_label("Send Message")
            message_btn.connect("clicked", self._on_send_message_clicked, peer)
            actions.append(message_btn)

            self._details_content.append(actions)

        # Close button
        close_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        close_box.set_halign(Gtk.Align.END)
        close_box.set_margin_top(8)

        close_btn = Gtk.Button.new_from_icon_name("window-close-symbolic")
        close_btn.add_css_class("flat")
        close_btn.update_property([Gtk.AccessibleProperty.LABEL], ["Close details"])
        close_btn.connect("clicked", self._on_close_details)
        close_box.append(close_btn)

        self._details_panel.prepend(close_box)
        self._details_panel.set_visible(True)
        self._apply_split()  # [panel-split patch]

    def _clear_details(self) -> None:
        """Clear the details content area."""
        clear_children(
            self._details_content
        )  # [plot-path patch] line stays until Plot path is toggled off

        # Also remove close button if present
        child = self._details_panel.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            if (
                child != self._details_title
                and child != self._details_content
                and child is not getattr(self, "_details_scroll", None)
            ):  # [panel-split patch]
                self._details_panel.remove(child)
            child = next_child

    def close_active_detail(self) -> bool:
        """Close the details panel if open. Returns True if something was closed."""
        if hasattr(self, "_details_panel") and self._details_panel.get_visible():
            self._selected_peer = None
            self._details_panel.set_visible(
                False
            )  # [plot-path patch] line stays until Plot path is toggled off
            return True
        return False

    # --- [map-jump patch] ---------------------------------------------------
    # --- [trace patch] --------------------------------------------------------
    # --- [map-route patch] ----------------------------------------------------
    # Trace path first draws the stored network path on the map as a dashed
    # line: you -> each hop -> this repeater, then fits the map to it. Hops whose
    # position is unknown are skipped (listed under the button).
    _ROUTE_RGBA = (0.937, 0.725, 0.247, 0.95)  # repeater marker amber #efb93f

    def _clear_route_line(self) -> None:
        layer = getattr(self, "_route_layer", None)
        if layer is not None:
            try:
                self._map.get_map().remove_layer(layer)
            except Exception:  # noqa: BLE001
                pass
            self._route_layer = None
        self._clear_snr_bubbles()  # [path-snr patch]

    # --- [path-snr patch] ------------------------------------------------------
    # A small bubble at the middle of each plotted link with its SNR both ways,
    # from the latest trace to this repeater. The arrows follow the line on
    # screen: the arrow points the way the signal travelled across that link.
    _SNR_CSS = b"""
    .route-snr-bubble { background: rgba(0,0,0,0.88); color: #ffffff; border: 1px solid rgba(255,255,255,0.75);
                        border-radius: 3px; padding: 1px 4px; font-family: \"DejaVu Sans Mono\", \"Liberation Mono\", monospace; font-size: 11px; font-weight: 700; }
    .route-snr-weak { color: #efb93f; }
    .route-snr-bad { color: #ff7070; }
    """
    _ARROWS = ("\u2192", "\u2198", "\u2193", "\u2199", "\u2190", "\u2196", "\u2191", "\u2197")

    def _clear_snr_bubbles(self) -> None:
        layer = getattr(self, "_snr_layer", None)
        if layer is not None:
            try:
                self._map.get_map().remove_layer(layer)
            except Exception:  # noqa: BLE001
                pass
            self._snr_layer = None

    # --- [favorite-links patch] ------------------------------------------------
    # One line per pair of favourite repeaters whose neighbour tables list each
    # other (heard within MAX_LINK_AGE_S), coloured by the weaker direction's
    # margin over the decode limit, with both SNRs in a bubble at the midpoint.
    _FAV_LINK_RGBA = {
        "good": (0.208, 0.761, 0.608, 1.0),  # mc_accent
        "weak": (0.937, 0.725, 0.247, 1.0),  # mc_warn
        "bad": (1.0, 0.373, 0.427, 1.0),  # mc_danger
    }

    def _draw_fav_links_icon(self, area: Gtk.DrawingArea, cr, width: int, height: int) -> None:
        """Three nodes joined by lines, in the button's text colour."""
        import math

        color = area.get_color()
        cr.set_source_rgba(color.red, color.green, color.blue, color.alpha)
        pts = (
            (width * 0.2, height * 0.75),
            (width * 0.5, height * 0.2),
            (width * 0.82, height * 0.7),
        )
        cr.set_line_width(1.6)
        for (xa, ya), (xb, yb) in zip(pts, pts[1:] + pts[:1]):
            cr.move_to(xa, ya)
            cr.line_to(xb, yb)
        cr.stroke()
        for x, y in pts:
            cr.arc(x, y, 2.6, 0, 2 * math.pi)
            cr.fill()

    _FAV_MODES = ("off", "links", "favs")

    def _on_fav_links_clicked(self, btn: Gtk.Button) -> None:
        nxt = self._FAV_MODES[(self._FAV_MODES.index(self._fav_mode) + 1) % 3]
        self._set_fav_mode(nxt)

    def _set_fav_mode(self, mode: str) -> None:
        """off: no links, every repeater. links (green): links plus every repeater.
        favs (purple): links, and only favourite repeaters on the map."""
        logger.debug("UI: favourite links mode %s -> %s", self._fav_mode, mode)
        self._fav_mode = mode
        if mode == "off":
            self._clear_fav_links()
        elif mode == "links" or not getattr(self, "_fav_layers", None):
            self._draw_fav_links()  # also toasts the link count
        # favs normally keeps the links drawn by the links step
        self._fav_links_btn.remove_css_class("map-control-active")
        self._fav_links_btn.remove_css_class("map-control-favs")
        if mode == "links":
            self._fav_links_btn.add_css_class("map-control-active")
        elif mode == "favs":
            self._fav_links_btn.add_css_class("map-control-favs")
            self._show_toast("Favourite repeaters only")
        self._apply_marker_filter()

    def _apply_marker_filter(self) -> None:
        """In favourites-only mode show only favourite repeaters: other repeaters,
        companions and room servers are hidden (your own marker stays)."""
        favs_only = getattr(self, "_fav_mode", "off") == "favs"
        for peer in self._service.list_peers():
            marker = self._peer_markers.get(peer.peer_id)
            if marker is not None:
                marker.set_visible(not favs_only or (peer.is_repeater and peer.is_favorite))

    def _clear_fav_links(self) -> None:
        map_widget = self._map.get_map()
        for layer in getattr(self, "_fav_layers", None) or []:
            try:
                map_widget.remove_layer(layer)
            except Exception:  # noqa: BLE001
                pass
        self._fav_layers = []

    def _draw_fav_links(self) -> None:
        import math

        from gi.repository import Gdk

        from meshcore_console.meshcore.favorite_links import (
            MAX_LINK_AGE_S,
            NEIGHBOUR_TABLE,
            favorite_mesh,
        )

        self._clear_fav_links()
        store = self._service.get_repeater_samples()
        mesh = favorite_mesh(
            self._service.list_peers(), lambda key: store.get_state(key, NEIGHBOUR_TABLE)
        )
        try:
            from meshcore_console.meshcore.trace import SNR_LIMIT_DB

            limit = SNR_LIMIT_DB.get(int(self._service.get_settings().spreading_factor), -7.5)
        except Exception:  # noqa: BLE001
            limit = -7.5

        def merc(lat: float, lon: float) -> tuple[float, float]:
            s = math.sin(math.radians(max(-85.0, min(85.0, lat))))
            return (lon + 180.0) / 360.0, 0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)

        def grade(snr: float) -> str:
            margin = snr - limit
            return "bad" if margin < 0 else "weak" if margin < 5 else "good"

        def ago(secs: int) -> str:
            if secs < 3600:
                return f"{max(1, secs // 60)} min ago"
            if secs < 86400:
                return f"{secs // 3600} h ago"
            return f"{secs // 86400} d ago"

        map_widget = self._map.get_map()
        bubbles = Shumate.MarkerLayer.new(self._viewport)
        layers = []
        edge = Gdk.RGBA()
        edge.red = edge.green = edge.blue = 0.0
        edge.alpha = 0.8
        for link in mesh.links:
            a, b = link.a, link.b
            worst = grade(min(link.snr_ab, link.snr_ba))
            layer = Shumate.PathLayer.new(self._viewport)
            rgba = Gdk.RGBA()
            rgba.red, rgba.green, rgba.blue, rgba.alpha = self._FAV_LINK_RGBA[worst]
            layer.set_stroke_color(rgba)
            layer.set_stroke_width(5.0)
            try:
                layer.set_outline_color(edge)  # dark edge: reads on light tiles too
                layer.set_outline_width(1.0)
            except Exception:  # noqa: BLE001
                layer.set_stroke_width(4.0)
            for lat, lon in ((b.latitude, b.longitude), (a.latitude, a.longitude)):
                layer.add_node(Shumate.Coordinate.new_full(lat, lon))  # add_node prepends
            try:
                map_widget.insert_layer_behind(layer, self._marker_layer)
            except Exception:  # noqa: BLE001
                map_widget.add_layer(layer)
            layers.append(layer)

            (xa, ya), (xb, yb) = merc(a.latitude, a.longitude), merc(b.latitude, b.longitude)
            sector = int(round(math.degrees(math.atan2(yb - ya, xb - xa)) / 45.0)) % 8
            mx, my = (xa + xb) / 2, (ya + yb) / 2
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
            box.add_css_class("route-snr-bubble")
            for arrow, value in (
                (self._ARROWS[sector], link.snr_ab),
                (self._ARROWS[(sector + 4) % 8], link.snr_ba),
            ):
                lab = Gtk.Label(label=f"{arrow} {value:+.1f}")
                lab.set_xalign(0.0)
                if grade(value) != "good":
                    lab.add_css_class(f"route-snr-{grade(value)}")
                box.append(lab)
            box.set_tooltip_text(
                f"{a.display_name} \u2192 {b.display_name}: {link.snr_ab:+.2f} dB\n"
                f"{b.display_name} \u2192 {a.display_name}: {link.snr_ba:+.2f} dB\n"
                f"From their neighbour tables, heard {ago(link.age_s)}\n"
                f"SNR as measured by the receiving repeater (decode limit {limit:+.1f} dB)"
            )
            marker = Shumate.Marker.new()
            marker.set_location(
                math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * my)))), mx * 360.0 - 180.0
            )
            marker.set_child(box)
            bubbles.add_marker(marker)
        if mesh.links:
            try:
                map_widget.insert_layer_behind(bubbles, self._marker_layer)
            except Exception:  # noqa: BLE001
                map_widget.add_layer(bubbles)
            layers.append(bubbles)
            # No auto-fit: turning the links on leaves the zoom and view alone.
        self._fav_layers = layers

        n = len(mesh.links)
        # Short toast (a toast never wraps: a long one widens the window); the
        # full breakdown, with names, goes in the button's tooltip and the log.
        short = [f"{n} favourite link{'s' if n != 1 else ''}" if n else "No favourite links"]
        detail = [
            f"{n} link{'s' if n != 1 else ''} between favourite repeaters that hear each other"
        ]
        if mesh.one_way:
            short.append(f"{len(mesh.one_way)} one-way")
            detail.append(
                "Heard one way only: "
                + ", ".join(f"{a.display_name} \u2192 {b.display_name}" for a, b in mesh.one_way)
            )
        if mesh.stale:
            short.append(f"{mesh.stale} stale")
            detail.append(f"{mesh.stale} not heard in {MAX_LINK_AGE_S // 3600} h")
        if mesh.no_table:
            short.append(f"{len(mesh.no_table)} without neighbour list")
            detail.append(
                "No neighbour list yet (log in to poll): "
                + ", ".join(p.display_name for p in mesh.no_table)
            )
        self._show_toast(" \u00b7 ".join(short))
        logger.info("favourite links: %s", "; ".join(detail))

    def _route_nodes(self, peer: Peer) -> list[tuple[str, float | None, float | None]]:
        """(id, lat, lon) for you, each hop and the repeater; lat/lon None if unknown."""
        peers = self._service.list_peers()
        here = self._service.get_device_location()
        nodes: list[tuple[str, float | None, float | None]] = [
            ("me", float(here[0]), float(here[1])) if here is not None else ("me", None, None)
        ]
        for hop in list(peer.last_path or []):
            h = str(hop).lower()
            located = [
                p
                for p in peers
                if p.public_key
                and p.public_key.lower().startswith(h)
                and p.latitude is not None
                and p.longitude is not None
            ]
            located = [p for p in located if p.is_repeater] or located
            prev = next(((la, lo) for _i, la, lo in reversed(nodes) if la is not None), None)
            if len(located) > 1 and prev is not None:
                located.sort(
                    key=lambda p: (p.latitude - prev[0]) ** 2 + (p.longitude - prev[1]) ** 2
                )
            nodes.append(
                (str(hop), located[0].latitude, located[0].longitude)
                if located
                else (str(hop), None, None)
            )
        nodes.append((peer.public_key or peer.peer_id, peer.latitude, peer.longitude))
        return nodes

    def _latest_trace(self, peer: Peer) -> dict | None:
        import time as _time

        store = (
            self._service.get_repeater_samples()
            if hasattr(self._service, "get_repeater_samples")
            else None
        )
        if store is None or not hasattr(store, "trace_series") or not peer.public_key:
            return None
        ok = [t for t in store.trace_series(peer.public_key, _time.time() - 30 * 86400) if t["ok"]]
        if not ok:
            return None
        last = ok[-1]
        links = [{"from": lk[0], "to": lk[1], "snr": lk[2]} for lk in last["links"]]
        return {"success": True, "links": links, "t": last["t"]}

    def _add_snr_bubbles(
        self, peer: Peer, result: dict | None = None, notes: Gtk.Box | None = None
    ) -> None:
        import math
        from datetime import datetime

        self._clear_snr_bubbles()
        fresh = result is not None
        if result is None:
            result = self._latest_trace(peer)
        links = list((result or {}).get("links") or [])
        half = len(links) / 2

        def same(a, b) -> bool:
            a, b = str(a).lower(), str(b).lower()
            if "me" in (a, b):
                return a == b
            return a.startswith(b) or b.startswith(a)

        def snr(a, b, outbound: bool):
            for i, link in enumerate(links):
                if (i < half) == outbound and same(link["from"], a) and same(link["to"], b):
                    return link.get("snr")
            return None

        def merc(lat: float, lon: float) -> tuple[float, float]:
            s = math.sin(math.radians(max(-85.0, min(85.0, lat))))
            return (lon + 180.0) / 360.0, 0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)

        try:
            from meshcore_console.meshcore.trace import SNR_LIMIT_DB

            limit = SNR_LIMIT_DB.get(int(self._service.get_settings().spreading_factor), -7.5)
        except Exception:  # noqa: BLE001
            limit = -7.5

        def line(arrow: str, value) -> Gtk.Label:
            lab = Gtk.Label(label=f"{arrow} {value:+.1f}" if value is not None else f"{arrow}  n/a")
            lab.set_xalign(0.0)
            if value is not None and value - limit < 0:
                lab.add_css_class("route-snr-bad")
            elif value is not None and value - limit < 5:
                lab.add_css_class("route-snr-weak")
            return lab

        nodes = self._route_nodes(peer)
        layer = Shumate.MarkerLayer.new(self._viewport)
        count = 0
        missing_links = 0  # [path-snr patch]
        for (ida, la, loa), (idb, lb, lob) in zip(nodes, nodes[1:]):
            if la is None or lb is None:
                continue
            out, back = snr(ida, idb, True), snr(idb, ida, False)
            if out is None and back is None:
                if not links:
                    continue
                missing_links += 1  # [path-snr patch] not in that trace: show n/a, don't hide it
            (xa, ya), (xb, yb) = merc(la, loa), merc(lb, lob)
            sector = (
                int(round(math.degrees(math.atan2(yb - ya, xb - xa)) / 45.0)) % 8
            )  # screen y points down
            mx, my = (xa + xb) / 2, (ya + yb) / 2
            mid_lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * my))))
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
            box.add_css_class("route-snr-bubble")
            box.append(line(self._ARROWS[sector], out))
            box.append(line(self._ARROWS[(sector + 4) % 8], back))

            def name(i: object) -> str:
                return "you" if i == "me" else str(i)[:4].upper()

            box.set_tooltip_text(
                f"{name(ida)} \u2192 {name(idb)}: "
                + (f"{out:+.2f} dB" if out is not None else "n/a")
                + f"\n{name(idb)} \u2192 {name(ida)}: "
                + (f"{back:+.2f} dB" if back is not None else "n/a")
                + f"\nSNR as measured by the receiving node (decode limit {limit:+.1f} dB)"
            )
            marker = Shumate.Marker.new()
            marker.set_location(mid_lat, mx * 360.0 - 180.0)
            marker.set_child(box)
            layer.add_marker(marker)
            count += 1
        if count:
            map_widget = self._map.get_map()
            try:
                map_widget.insert_layer_behind(layer, self._marker_layer)  # nodes stay on top
            except Exception:  # noqa: BLE001
                map_widget.add_layer(layer)
            self._snr_layer = layer
        if not links:
            text = "Press Trace path to add each link's SNR to the plot."
        elif fresh:
            text = "Link SNR on the map from this trace."
        else:
            when = datetime.fromtimestamp(result["t"])
            text = f"Link SNR on the map from the trace at {when.strftime('%H:%M' if when.date() == datetime.now().date() else '%b %-d %H:%M')}."
        if missing_links:  # [path-snr patch]
            text += (
                f" {missing_links} link{'s' if missing_links != 1 else ''} on the plot "
                f"{'are' if missing_links != 1 else 'is'} not in that trace (the path has changed "
                "since): press Trace path to measure the current path."
            )
        note = getattr(self, "_snr_note", None)
        if notes is not None:
            note = Gtk.Label(label=text)
            note.add_css_class("panel-muted")
            note.set_wrap(True)
            note.set_xalign(0.0)
            note.set_halign(Gtk.Align.START)
            notes.append(note)
            self._snr_note = note
        elif note is not None:
            note.set_label(text)

    def _route_points(self, peer: Peer) -> tuple[list[tuple[float, float]], list[str]]:
        """Located nodes along the path (you first) and the labels of those skipped."""
        peers = self._service.list_peers()
        points: list[tuple[float, float]] = []
        missing: list[str] = []
        here = self._service.get_device_location()
        if here is not None:
            points.append((float(here[0]), float(here[1])))
        else:
            missing.append("you (no GPS fix)")
        for hop in list(peer.last_path or []):
            h = str(hop).lower()
            matches = [
                p
                for p in peers
                if p.public_key
                and p.public_key.lower().startswith(h)
                and p.latitude is not None
                and p.longitude is not None
            ]
            repeaters = [p for p in matches if p.is_repeater] or matches
            if not repeaters:
                missing.append(str(hop).upper())
                continue
            if (
                len(repeaters) > 1 and points
            ):  # short hash shared by several: nearest to previous node
                plat, plon = points[-1]
                repeaters.sort(key=lambda p: (p.latitude - plat) ** 2 + (p.longitude - plon) ** 2)
            points.append((repeaters[0].latitude, repeaters[0].longitude))
        if peer.latitude is not None and peer.longitude is not None:
            points.append((peer.latitude, peer.longitude))
        else:
            missing.append(peer.display_name)
        return points, missing

    def _draw_route_line(self, peer: Peer, results: Gtk.Box) -> None:
        from gi.repository import Gdk

        self._clear_route_line()
        points, missing = self._route_points(peer)
        if len(points) < 2:
            note = Gtk.Label(
                label="Path not drawn on the map: fewer than two nodes have a known position."
            )
        else:
            layer = Shumate.PathLayer.new(self._viewport)
            rgba = Gdk.RGBA()
            rgba.red, rgba.green, rgba.blue, rgba.alpha = self._ROUTE_RGBA
            layer.set_stroke_color(rgba)
            layer.set_stroke_width(3.0)
            black = Gdk.RGBA()  # [path-snr patch] black dashes with a thin white edge:
            black.red = black.green = black.blue = 0.0  # readable on dark and light tiles
            black.alpha = 1.0
            edge = Gdk.RGBA()
            edge.red = edge.green = edge.blue = 1.0
            edge.alpha = 0.85
            layer.set_stroke_color(black)
            layer.set_stroke_width(5.0)  # 3 px black inside a 1 px white edge
            try:
                layer.set_outline_color(edge)
                layer.set_outline_width(1.0)
            except Exception:  # noqa: BLE001
                layer.set_stroke_width(3.0)
            try:
                layer.set_dash([10, 6])
            except Exception:  # noqa: BLE001 - older bindings: solid line
                logger.debug("PathLayer.set_dash unavailable; drawing a solid route line")
            for lat, lon in reversed(points):  # add_node prepends
                layer.add_node(Shumate.Coordinate.new_full(lat, lon))
            map_widget = self._map.get_map()
            try:
                map_widget.insert_layer_behind(layer, self._marker_layer)  # markers stay on top
            except Exception:  # noqa: BLE001
                map_widget.add_layer(layer)
            self._route_layer = layer
            self._add_snr_bubbles(peer, notes=results)  # [path-snr patch]
            self._fit_route(points)
            note = (
                Gtk.Label(label=f"Skipped on the map (position unknown): {', '.join(missing)}")
                if missing
                else None
            )
        if note is not None:
            note.add_css_class("panel-muted")
            note.set_wrap(True)
            note.set_xalign(0.0)
            note.set_halign(Gtk.Align.START)
            results.append(note)

    def _fit_route(
        self, points: list[tuple[float, float]], *, only_if_hidden: bool = False
    ) -> None:
        """Centre and zoom so the whole path is in view (Web Mercator maths).

        With *only_if_hidden*, leave the view alone when every point is already on
        screen. The move is animated: a jump to a new zoom showed only the tiles
        already loaded, so for a moment the map covered part of its width.
        """
        import math

        def merc_y(lat: float) -> float:
            lat = max(-85.0, min(85.0, lat))
            s = math.sin(math.radians(lat))
            return 0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)

        xs = [(lon + 180.0) / 360.0 for _lat, lon in points]
        ys = [merc_y(lat) for lat, _lon in points]
        w = max(self._map.get_width(), 200)
        h = max(self._map.get_height(), 200)
        if only_if_hidden:
            scale = 256 * 2 ** self._viewport.get_zoom_level()  # world width in px
            cx0 = (self._viewport.get_longitude() + 180.0) / 360.0
            cy0 = merc_y(self._viewport.get_latitude())
            pad = 40  # px: a marker's label must fit too
            if all(
                abs((x - cx0) * scale) <= w / 2 - pad and abs((y - cy0) * scale) <= h / 2 - pad
                for x, y in zip(xs, ys)
            ):
                return
        dx = max(max(xs) - min(xs), 1e-9) * 1.35
        dy = max(max(ys) - min(ys), 1e-9) * 1.35
        zoom = math.log2(min(w / (256 * dx), h / (256 * dy)))
        zoom = max(3.0, min(15.0, math.floor(zoom * 2) / 2))
        cx = (min(xs) + max(xs)) / 2
        cy = (min(ys) + max(ys)) / 2
        lon = cx * 360.0 - 180.0
        lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * cy))))
        if getattr(self, "_following", False):
            self._set_following(False)
        map_widget = self._map.get_map()
        if hasattr(map_widget, "go_to_full_with_duration"):  # libshumate >= 1.1
            map_widget.go_to_full_with_duration(lat, lon, zoom, 600)
        else:  # 1.0 (Raspberry Pi OS bookworm): animated, default duration
            map_widget.go_to_full(lat, lon, zoom)

    def _switch_route_to(self, peer: Peer, result: dict) -> None:  # [path-owner patch]
        """A trace succeeded for a repeater other than the one plotted: plot this one instead."""
        ref = getattr(self, "_panel_plot", None)
        mine = ref is not None and ref[0] == peer.peer_id
        notes = ref[2] if mine else Gtk.Box()
        clear_children(notes)
        self._draw_route_line(peer, notes)
        if getattr(self, "_route_layer", None) is None:
            return
        self._route_peer_id = peer.peer_id
        self._add_snr_bubbles(peer, result=result)
        if mine:
            ref[1].set_label("Hide path")

    def _on_map_fav_clicked(self, btn: Gtk.Button, peer_id: str) -> None:  # [map-fav patch]
        peer = next((p for p in self._service.list_peers() if p.peer_id == peer_id), None)
        if peer is None:
            return
        favorite = not peer.is_favorite  # decide first: the service may update this same object
        self._service.set_favorite(peer_id, favorite)
        btn.set_label("\u2605 Unfavorite" if favorite else "\u2606 Favorite")
        btn.set_tooltip_text(
            "Remove from favourites" if favorite else "Add to favourites (Peers, Admin)"
        )

    def _on_plot_path_clicked(
        self, btn: Gtk.Button, peer: Peer, notes: Gtk.Box
    ) -> None:  # [plot-path patch]
        """Toggle the dashed network-path line for this repeater."""
        clear_children(notes)
        if (
            getattr(self, "_route_layer", None) is not None
            and getattr(self, "_route_peer_id", None) == peer.peer_id
        ):
            self._clear_route_line()
            btn.set_label("Plot path")
            return
        self._draw_route_line(peer, notes)
        if getattr(self, "_route_layer", None) is not None:
            self._route_peer_id = peer.peer_id
            btn.set_label("Hide path")

    def _trace_section(self, peer: Peer) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        btn = Gtk.Button(label="Trace path")
        btn.set_halign(Gtk.Align.START)
        btn.set_tooltip_text(
            "Send a round-trip TRACE along this path: SNR for every link, both ways"
        )
        results = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        btn.connect("clicked", self._on_map_trace_clicked, peer, results)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)  # [plot-path patch]
        plot = Gtk.Button(label="Plot path")
        if (
            getattr(self, "_route_layer", None) is not None
            and getattr(self, "_route_peer_id", None) == peer.peer_id
        ):
            plot.set_label("Hide path")  # [plot-path patch] its line is still on the map
        plot.set_tooltip_text("Draw this repeater's network path on the map as a dashed line")
        notes = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        plot.connect("clicked", self._on_plot_path_clicked, peer, notes)
        self._panel_plot = (peer.peer_id, plot, notes)  # [path-owner patch]
        row.append(plot)
        row.append(btn)
        box.append(row)
        fav = Gtk.Button(
            label="\u2605 Unfavorite" if peer.is_favorite else "\u2606 Favorite"
        )  # [map-fav patch]
        fav.set_tooltip_text(
            "Remove from favourites" if peer.is_favorite else "Add to favourites (Peers, Admin)"
        )
        fav.add_css_class("map-fav-btn")
        fav.connect("clicked", self._on_map_fav_clicked, peer.peer_id)
        fav.set_halign(Gtk.Align.START)
        box.append(fav)  # own row: the panel is narrow
        box.append(notes)
        if (
            getattr(self, "_route_layer", None) is not None
            and getattr(self, "_route_peer_id", None) == peer.peer_id
        ):
            self._add_snr_bubbles(peer, notes=notes)  # [path-snr patch] note back; newest trace
        elif (
            getattr(self, "_route_layer", None) is not None
        ):  # [path-owner patch] say whose line it is
            owner = next(
                (
                    p.display_name
                    for p in self._service.list_peers()
                    if p.peer_id == getattr(self, "_route_peer_id", None)
                ),
                "another repeater",
            )
            hint = Gtk.Label(
                label=f"The path on the map is {owner}'s. Plot path or a successful Trace path shows this one."
            )
            hint.add_css_class("panel-muted")
            hint.set_wrap(True)
            hint.set_xalign(0.0)
            hint.set_halign(Gtk.Align.START)
            notes.append(hint)
        box.append(results)
        return box

    def _on_map_trace_clicked(self, btn: Gtk.Button, peer: Peer, results: Gtk.Box) -> None:
        import threading

        from meshcore_console.ui_gtk.widgets.trace_view import build_trace_view

        btn.set_sensitive(False)
        btn.set_label("Tracing…")
        clear_children(results)  # [plot-path patch] path drawing is the Plot path button

        def work() -> None:
            try:
                from meshcore_console.meshcore.trace import trace_source  # [trace-source patch]

                with trace_source("map"):
                    result = self._service.trace_repeater(peer.display_name)
            except Exception as exc:  # noqa: BLE001
                result = {"success": False, "reason": str(exc) or type(exc).__name__}

            def done() -> bool:
                btn.set_sensitive(True)
                btn.set_label("Trace path")
                if (  # [path-snr patch] fresh trace: update the bubbles on the plotted path
                    result.get("success")
                    and getattr(self, "_route_layer", None) is not None
                    and getattr(self, "_route_peer_id", None) == peer.peer_id
                ):
                    self._add_snr_bubbles(peer, result=result)
                elif result.get("success") and getattr(self, "_route_layer", None) is not None:
                    self._switch_route_to(
                        peer, result
                    )  # [path-owner patch] line was another repeater's
                if self._selected_peer is None or self._selected_peer.peer_id != peer.peer_id:
                    return False  # panel now shows another node
                self_key = self._service.get_self_public_key() or ""
                results.append(
                    build_trace_view(
                        result,
                        self._service.list_peers(),
                        target=peer.display_name,
                        self_label=f"{self._service.get_status().node_id} ({self_key[:2].upper() or 'me'})",
                        spreading_factor=self._service.get_settings().spreading_factor,
                        compact=True,  # [compact-trace patch] narrow table for the panel
                    )
                )
                return False

            GLib.idle_add(done)

        threading.Thread(target=work, daemon=True).start()

    def focus_peer(self, peer_id: str) -> None:
        """Navigation entry (a jump here from another screen): centre on a peer.

        Turns the favourite-link view off, so every repeater shows again, unless
        the peer is a favourite repeater: then the link view stays as it was.
        """
        self._focus_peer(peer_id, reset_links=True)

    def _focus_peer(self, peer_id: str, *, reset_links: bool) -> None:
        """Center the map on a peer, zoom in, and open its details panel."""
        if not SHUMATE_AVAILABLE:
            return
        peer = next((p for p in self._service.list_peers() if p.peer_id == peer_id), None)
        if peer is None or peer.latitude is None or peer.longitude is None:
            self._show_toast("No location for that node yet")
            return
        logger.debug("UI: map focus peer=%s", peer.display_name)
        if reset_links and self._fav_mode != "off" and not (peer.is_repeater and peer.is_favorite):
            self._set_fav_mode("off")
        self._refresh_markers()
        self._selected_peer = peer
        self._show_peer_details(peer)
        self._set_following(False)
        self._programmatic_move = True
        self._viewport.set_zoom_level(max(self._viewport.get_zoom_level(), 13))
        self._viewport.set_location(peer.latitude, peer.longitude)
        self._programmatic_move = False

    def center_on_device(self, _arg: object = None) -> None:
        """Center the map on this device (same as the locate button)."""
        if SHUMATE_AVAILABLE:
            self._on_center_device(None)

    # --- [map-path-click patch] -----------------------------------------------
    def _link_path_badges_to_map(self, path: Gtk.Widget) -> None:
        # On the Map, one click on a path badge pans to that node and opens its
        # details (instead of the stats popover). Capture phase, so this gesture
        # claims the click before the badge's own handler sees it.
        from meshcore_console.ui_gtk.widgets.node_badge import STYLE_SELF, NodeBadge

        def badges(widget):
            child = widget.get_first_child()
            while child is not None:
                if isinstance(child, NodeBadge):
                    yield child
                else:
                    yield from badges(child)
                child = child.get_next_sibling()

        for badge in badges(path):
            peer = getattr(badge, "_peer", None)
            is_self = peer is None and getattr(badge, "_style", None) == STYLE_SELF
            if peer is None and not is_self:
                continue  # hop not in your peer list: nothing to go to
            click = Gtk.GestureClick.new()
            click.set_button(1)
            click.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
            click.connect("pressed", self._on_path_badge_clicked, peer)
            badge.add_controller(click)
            name = "your position" if peer is None else peer.display_name
            badge.set_tooltip_text(f"Show {name} on the map")

    def _on_path_badge_clicked(self, gesture, _n_press: int, _x: float, _y: float, peer) -> None:
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        logger.debug("UI: map path badge clicked: %s", peer.display_name if peer else "self")

        # Deferred: focusing rebuilds the details panel, which destroys this badge.
        def go() -> bool:
            if peer is None:
                self.center_on_device()
            else:  # already on the Map: leave the link view as it is
                self._focus_peer(peer.peer_id, reset_links=False)
            return False

        GLib.idle_add(go)

    def get_default_focus(self) -> Gtk.Widget:
        """Return the widget that should receive focus when this view is shown."""
        if hasattr(self, "_map"):
            return self._map
        return self

    def _on_close_details(self, _button: Gtk.Button) -> None:
        """Close the details panel."""
        self._selected_peer = None
        self._details_panel.set_visible(
            False
        )  # [plot-path patch] line stays until Plot path is toggled off

    def _on_send_message_clicked(self, _button: Gtk.Button, peer: Peer) -> None:
        """Navigate to messages view and start a conversation with this peer."""
        navigate(self, "messages", ("select_channel", peer.display_name))
