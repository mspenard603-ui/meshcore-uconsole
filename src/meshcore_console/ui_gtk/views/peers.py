from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, Gtk, Pango

from meshcore_console.ui_gtk.compat import build_alert_dialog, present_dialog

if TYPE_CHECKING:
    pass

from meshcore_console.core.models import Peer
from meshcore_console.core.radio import format_rssi, format_snr
from meshcore_console.core.services import MeshcoreService
from meshcore_console.ui_gtk.helpers import clear_children, clear_listbox, navigate
from meshcore_console.ui_gtk.widgets.mini_map import SHUMATE_AVAILABLE as MINI_MAP_AVAILABLE
from meshcore_console.ui_gtk.widgets.mini_map import MiniMap
from meshcore_console.ui_gtk.layout import Layout
from meshcore_console.ui_gtk.state import UiEventStore
from meshcore_console.core.time import to_local
from meshcore_console.ui_gtk.widgets import (
    DetailRow,
    EmptyState,
    PathVisualization,
    PeerListRow,
)
from meshcore_console.ui_gtk.widgets.node_badge import STYLE_DEFAULT, STYLE_SELF

logger = logging.getLogger(__name__)


def format_public_key(key: str | None) -> str:
    """Format public key as two-byte hex chunks."""
    if not key:
        return "Unknown"
    # Split into 4-char (2 byte) chunks
    chunks = [key[i : i + 4] for i in range(0, len(key), 4)]
    return " ".join(chunks)


def format_coordinates(lat: float | None, lon: float | None) -> str:
    """Format lat/lon coordinates for display."""
    if lat is None or lon is None:
        return "Unknown"
    lat_dir = "N" if lat >= 0 else "S"
    lon_dir = "E" if lon >= 0 else "W"
    return f"{abs(lat):.5f}° {lat_dir}, {abs(lon):.5f}° {lon_dir}"


# --- [peer-sort patch] --------------------------------------------------------
SORT_MODES = ("newest", "oldest", "name", "distance")
SORT_LABELS = ("Newest", "Oldest", "A\u2013Z", "Distance")
DISTANCE_IN_MILES = True  # False = kilometres


def _sort_file():
    from meshcore_console.meshcore.paths import config_dir

    return config_dir() / "peer_sort.json"


def _load_sort_modes() -> dict[bool, str]:
    """Saved sort choice per list; keyed by is_repeater."""
    import json

    modes = {False: "newest", True: "newest"}
    try:
        data = json.loads(_sort_file().read_text())
        for key, is_rep in (("contacts", False), ("repeaters", True)):
            if data.get(key) in SORT_MODES:
                modes[is_rep] = data[key]
    except (OSError, ValueError, AttributeError):
        pass
    return modes


def _save_sort_modes(modes: dict[bool, str]) -> None:
    import json

    try:
        path = _sort_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"contacts": modes[False], "repeaters": modes[True]}))
    except OSError as e:
        logger.warning("Could not save peer sort order: %s", e)


def _distance_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle distance in km (haversine)."""
    import math

    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return 2 * 6371.0088 * math.asin(min(1.0, math.sqrt(h)))


def _has_location(lat: float | None, lon: float | None) -> bool:
    return lat is not None and lon is not None and (lat != 0.0 or lon != 0.0)


class PeersView(Gtk.Box):
    def __init__(
        self, service: MeshcoreService, event_store: UiEventStore, layout: Layout, kind: str = "all"
    ) -> None:  # [rooms patch] "contacts", "rooms" or "repeaters"
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._service = service
        self._event_store = event_store
        self._kind = kind  # [rooms patch]
        self._stale = False
        self._last_peer_snapshot: str = ""
        self._selected_peer: Peer | None = None
        self.connect("map", self._on_peers_shown)  # [map-select patch]
        self._peer_telemetry: dict[str, dict] = {}
        self._sort_modes = _load_sort_modes()  # [peer-sort patch]
        self._distances: dict[str, float] = {}

        # Column 1: Contacts
        contacts_column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        contacts_column.add_css_class("panel-card")
        contacts_column.set_size_request(layout.peers_list_width, -1)
        if kind == "rooms":  # the room chat gets the spare width, as Channels' chat does
            contacts_column.set_hexpand(False)

        # [clear-peers patch] header with Clear button
        contacts_column.append(
            self._list_header(
                "Rooms" if kind == "rooms" else "Contacts",  # [rooms patch]
                repeaters=False,
            )
        )

        contacts_scroll = Gtk.ScrolledWindow.new()
        contacts_scroll.set_vexpand(True)
        contacts_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        self._contacts_list = Gtk.ListBox.new()
        self._contacts_list.add_css_class("peer-list")
        self._contacts_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._contacts_list.connect("row-selected", self._on_peer_selected)
        contacts_scroll.set_child(self._contacts_list)
        contacts_column.append(contacts_scroll)

        self.append(contacts_column)

        # Column 2: Repeaters/Network
        network_column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        network_column.add_css_class("panel-card")
        network_column.set_size_request(layout.peers_list_width, -1)

        # [clear-peers patch] header with Clear button
        network_column.append(self._list_header("Repeaters", repeaters=True))

        network_scroll = Gtk.ScrolledWindow.new()
        network_scroll.set_vexpand(True)
        network_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        self._network_list = Gtk.ListBox.new()
        self._network_list.add_css_class("peer-list")
        self._network_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._network_list.connect("row-selected", self._on_peer_selected)
        network_scroll.set_child(self._network_list)
        network_column.append(network_scroll)

        self.append(network_column)
        contacts_column.set_visible(kind != "repeaters")  # [rooms patch] one list per tab
        network_column.set_visible(kind in ("all", "repeaters"))
        if kind == "rooms":  # [room-chat patch] rooms with saved passwords: log in after start-up
            GLib.timeout_add_seconds(45, self._room_autologin_tick)

        # Column 3: Details panel
        details_column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        details_column.add_css_class("panel-card")
        details_column.set_hexpand(True)

        self._details_title = Gtk.Label(label="Select a peer")
        self._details_title.add_css_class("panel-title")
        self._details_title.set_halign(Gtk.Align.START)
        self._details_title.set_margin_start(12)
        self._details_title.set_margin_top(10)
        self._details_title.set_margin_bottom(8)
        self._title_row = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=8
        )  # [admin-fold patch]
        self._details_title.set_hexpand(True)
        # A long name ("FrancestownNH DodgeHill") must shorten with an ellipsis:
        # an unbounded title's natural width pushed the whole window off screen.
        self._details_title.set_ellipsize(Pango.EllipsizeMode.END)
        self._details_title.set_max_width_chars(28)
        self._title_row.append(self._details_title)
        # Repeaters: "· 59B9" hash, kept whole while the name is the part that shortens.
        self._title_hash = Gtk.Label()
        self._title_hash.add_css_class("panel-title")
        self._title_hash.set_halign(Gtk.Align.START)
        self._title_hash.set_margin_top(10)
        self._title_hash.set_margin_bottom(8)
        self._title_hash.set_visible(False)
        self._title_row.append(self._title_hash)
        # Rooms: the "Room server" pill follows the name, Favorite sits at the end.
        self._title_pill = Gtk.Label(label="Room server")
        self._title_pill.add_css_class("status-pill")
        self._title_pill.add_css_class("room")
        self._title_pill.set_valign(Gtk.Align.CENTER)
        self._title_pill.set_hexpand(True)
        self._title_pill.set_halign(Gtk.Align.START)
        self._title_pill.set_visible(False)
        self._title_row.append(self._title_pill)
        self._title_fav = Gtk.Button()
        self._title_fav.set_valign(Gtk.Align.CENTER)
        self._title_fav.set_margin_end(12)
        self._title_fav.set_visible(False)
        self._title_fav.connect("clicked", self._on_title_fav_clicked)
        self._title_row.append(self._title_fav)
        details_column.append(self._title_row)

        self._details_scroll = Gtk.ScrolledWindow.new()
        details_scroll = self._details_scroll
        details_scroll.set_vexpand(True)
        details_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        self._details_content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self._details_content.set_margin_start(12)
        self._details_content.set_margin_end(12)
        self._details_content.set_margin_bottom(12)
        details_scroll.set_child(self._details_content)
        self._admin = None  # [admin-fold patch] Repeaters: Status / Admin tabs
        self._sub_stack = Gtk.Stack()
        self._sub_stack.set_vexpand(True)
        self._sub_stack.set_transition_type(Gtk.StackTransitionType.NONE)
        self._sub_stack.add_titled(details_scroll, "status", "Status")
        details_column.append(self._sub_stack)

        self.append(details_column)

        self._show_empty_details()
        self._refresh_peers()
        self._event_store.connect("events-available", lambda _store: self._poll_peers())

    @staticmethod
    def _peer_snapshot(peers: list[Peer]) -> str:
        """Compute a snapshot string to detect peer data changes."""
        parts = []
        for p in peers:
            parts.append(
                f"{p.display_name}:{p.last_advert_time}:{p.signal_quality}:{p.rssi}:{p.snr}:{p.is_favorite}"
            )
        return "|".join(parts)

    def _poll_peers(self) -> bool:
        """Check for peer changes and refresh if needed."""
        if not self.get_mapped():  # [rooms patch] hidden tab: rebuild when it is shown
            self._stale = True
            return True
        peers = self._service.list_peers()
        snapshot = self._peer_snapshot(peers) + self._login_snapshot()  # [admin-fold patch]
        if snapshot != self._last_peer_snapshot:
            self._refresh_peers()
        return True

    def _refresh_peers(self) -> None:
        """Refresh the peers list display, preserving selection."""
        # Remember selected peer
        selected_name = self._selected_peer.display_name if self._selected_peer else None

        # Clear existing rows
        for listbox in (self._contacts_list, self._network_list):
            clear_listbox(listbox)

        peers = self._service.list_peers()
        self._last_peer_snapshot = (
            self._peer_snapshot(peers) + self._login_snapshot()
        )  # [admin-fold patch]

        # [peer-sort patch] user-selected order, favourites always on top
        self._distances = self._compute_distances(peers)
        contacts = self._sort_peers(
            [p for p in peers if not p.is_repeater and self._in_view(p)], False
        )
        network = self._sort_peers([p for p in peers if p.is_repeater and self._in_view(p)], True)

        self._populate_list(
            self._contacts_list,
            contacts,  # [rooms patch]
            "No room servers heard yet" if self._kind == "rooms" else "No contacts yet",
        )
        self._populate_list(self._network_list, network, "No repeaters yet")

        # Restore selection
        if selected_name:
            self._reselect_peer(selected_name)

    def _populate_list(self, listbox: Gtk.ListBox, peers: list[Peer], empty_msg: str) -> None:
        logged_in = set()  # [admin-fold patch]
        if self._kind == "repeaters" and listbox is self._network_list:
            try:
                logged_in = set(self._service.list_logged_in_repeaters())
            except Exception:  # noqa: BLE001
                pass
        """Populate a listbox with peers."""
        if not peers:
            row = Gtk.ListBoxRow.new()
            row.set_selectable(False)
            row.set_child(EmptyState(empty_msg))
            listbox.append(row)
            return

        for peer in peers:
            # [peer-sort patch] show distance when that list is sorted by distance
            is_rep = listbox is self._network_list
            dist = (
                self._distances.get(peer.peer_id)
                if self._sort_modes[is_rep] == "distance"
                else None
            )
            row = PeerListRow(peer, distance_km=dist)
            self._add_row_menu(row, peer)  # [delete-peer patch]
            if is_rep and peer.display_name in logged_in:  # [admin-fold patch]
                dot = Gtk.Label(label="\u25cf")
                dot.add_css_class("admin-login-dot")
                dot.set_valign(Gtk.Align.CENTER)
                dot.set_tooltip_text("Logged in")
                row.get_child().prepend(dot)
            listbox.append(row)

    def _on_peer_selected(self, listbox: Gtk.ListBox, row: Gtk.ListBoxRow | None) -> None:
        """Handle peer selection to show details."""
        # Deselect the other list
        other = self._network_list if listbox == self._contacts_list else self._contacts_list
        other.unselect_all()

        if row is None:
            self._show_empty_details()
            return

        peer = getattr(row, "peer", None)
        if peer is None:
            self._show_empty_details()
            return

        logger.debug("UI: peer selected name=%s id=%s", peer.display_name, peer.peer_id)
        self._selected_peer = peer
        self._show_peer_details(peer)

    def get_default_focus(self) -> Gtk.Widget:
        """Return the widget that should receive focus when this view is shown."""
        return (
            self._network_list if self._kind == "repeaters" else self._contacts_list
        )  # [rooms patch]

    def _room_autologin_tick(self) -> bool:  # [room-chat patch]
        """Once after start-up, then every 6 h: a room stops pushing to a client whose
        ACKs failed 3 times (e.g. while Console was off) until it hears from it again."""
        import threading

        from meshcore_console.ui_gtk.widgets.room_view import auto_login_rooms

        if getattr(self._service.get_status(), "connected", False):
            threading.Thread(target=auto_login_rooms, args=(self._service,), daemon=True).start()
        GLib.timeout_add_seconds(6 * 3600, self._room_autologin_tick)
        return False

    # --- [admin-fold patch] -------------------------------------------------------
    def attach_admin(self, admin) -> None:
        """Put the Admin panel next to Status in the details panel."""
        self._admin = admin
        admin.embed()
        admin.set_vexpand(True)
        self._sub_stack.add_named(admin, "admin")  # [repeater-tabs patch] one panel:
        self._sub_stack.set_visible_child_name("admin")  # route + login, then Status/Graph/Console

        # Status = Admin's stats, then this view's extras (last trace, telemetry, buttons)
        def _detach(w):
            parent = w.get_parent()
            if isinstance(parent, Gtk.Viewport):
                parent.set_child(None)
            elif isinstance(parent, Gtk.ScrolledWindow):
                parent.set_child(None)

        _detach(self._details_content)
        _detach(admin._status_content)
        admin._status_scroll.set_child(None)
        combo = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        combo.append(admin._status_content)
        combo.append(self._details_content)
        admin._status_scroll.set_child(combo)
        self._details_scroll = admin._status_scroll
        admin._on_trace_done = self._refresh_repeater_extras
        switcher = admin.page_switcher()
        switcher.set_valign(Gtk.Align.CENTER)
        switcher.set_margin_top(6)
        switcher.set_margin_end(10)
        self._title_row.append(switcher)
        self._sub_switcher = switcher
        switcher.set_visible(self._selected_peer is not None)
        if self._selected_peer is not None:
            admin.show_repeater(self._selected_peer)

    def _sync_admin(self, peer: Peer | None) -> None:
        admin = getattr(self, "_admin", None)
        if admin is None:
            return
        if peer is None:  # a list rebuild deselects for a moment: act only if it stays so
            if not getattr(self, "_admin_idle", 0):
                self._admin_idle = GLib.idle_add(self._admin_deselected)
            return
        self._sub_switcher.set_visible(True)
        admin.show_repeater(peer)

    def _show_repeater_extras(self, peer: Peer) -> None:  # [repeater-tabs patch]
        """What Status adds under Admin's stats: last trace, telemetry, buttons."""
        self._clear_details()
        self._add_section_header("Last Trace")
        results = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self._details_content.append(results)
        self._show_latest_trace(peer, Gtk.Button(), results)
        if results.get_first_child() is None:
            none = Gtk.Label(label="No trace yet: use Trace next to the route")
            none.add_css_class("panel-muted")
            none.set_halign(Gtk.Align.START)
            results.append(none)
        cached = self._peer_telemetry.get(peer.peer_id)
        if cached is not None:
            self._render_telemetry(cached)
        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        actions.set_margin_top(16)
        # Favorite is in the header by Trace, shared by every repeater tab.
        telem_btn = Gtk.Button.new_with_label("Request Telemetry")
        telem_btn.connect("clicked", self._on_request_telemetry_clicked, peer)
        actions.append(telem_btn)
        self._details_content.append(actions)

    def _refresh_repeater_extras(
        self,
    ) -> None:  # [repeater-tabs patch] after a trace from the header
        if self._selected_peer is not None and self._selected_peer.is_repeater:
            self._show_repeater_extras(self._selected_peer)

    def _admin_deselected(self) -> bool:
        self._admin_idle = 0
        if self._network_list.get_selected_row() is None and self._admin is not None:
            self._sub_switcher.set_visible(
                False
            )  # [repeater-tabs patch] Admin shows 'Select a repeater'
            self._admin.show_repeater(None)
        return False

    def show_admin(self, _arg: object = None) -> None:
        """Open the Admin tab for the selected repeater."""
        if getattr(self, "_admin", None) is not None and self._selected_peer is not None:
            self._sub_stack.set_visible_child_name("admin")

    def _login_snapshot(self) -> str:
        if self._kind != "repeaters":
            return ""
        try:
            return "|in:" + ",".join(sorted(self._service.list_logged_in_repeaters()))
        except Exception:  # noqa: BLE001
            return ""

    def _in_view(self, peer: Peer) -> bool:  # [rooms patch]
        """Whether this tab lists *peer*: room servers only under Rooms."""
        if self._kind == "all":
            return True
        if peer.is_repeater:
            return self._kind == "repeaters"
        is_room = getattr(peer, "node_type", None) == 3
        return is_room if self._kind == "rooms" else self._kind == "contacts" and not is_room

    def _add_mini_map(self, peer: Peer) -> None:
        """Small map around a room server, as on a repeater's Status. One map is
        kept and re-centred: details are rebuilt often, a new map refetches tiles."""
        if not MINI_MAP_AVAILABLE:
            return
        if getattr(self, "_mini_map", None) is None:
            self._mini_map = MiniMap()
            self._mini_map.set_margin_top(4)
        parent = self._mini_map.get_parent()
        if parent is not None:
            parent.remove(self._mini_map)
        self._mini_map.show_peer(peer)
        self._details_content.append(self._mini_map)

    def _set_room_title(self, peer: Peer | None) -> None:
        """Show the room pill and Favorite button in the title row for a room server."""
        is_room = (
            peer is not None
            and self._kind == "rooms"
            and getattr(peer, "node_type", None) == 3
            and not peer.is_repeater
        )
        self._title_pill.set_visible(is_room)
        self._title_fav.set_visible(is_room)
        # The title fills the row unless the pill or hash does, so they hug the name.
        has_hash = bool(peer is not None and peer.is_repeater and peer.public_key)
        self._title_hash.set_visible(has_hash)
        if has_hash:  # [title-hash patch] "Name · 59B9", as the header shows this node
            self._title_hash.set_markup(f"\u00b7 <b>{peer.public_key[:4].upper()}</b>")
        self._title_hash.set_hexpand(has_hash and not is_room)
        self._details_title.set_hexpand(not is_room and not has_hash)
        self._details_title.set_tooltip_text(peer.display_name if peer is not None else None)
        if is_room:
            self._title_fav.set_label("★ Unfavorite" if peer.is_favorite else "☆ Favorite")
            self._title_fav_peer = peer

    def _on_title_fav_clicked(self, button: Gtk.Button) -> None:
        peer = getattr(self, "_title_fav_peer", None)
        if peer is not None:
            self._on_toggle_favorite_clicked(button, peer)

    def _show_empty_details(self) -> None:
        """Show empty state in details panel."""
        self._set_room_title(None)
        self._details_title.set_text("Peer Details")
        self._sync_admin(None)  # [admin-fold patch]
        self._clear_details()
        self._details_content.append(
            EmptyState(
                {
                    "contacts": "Select a contact to view details",  # [rooms patch]
                    "rooms": "Select a room server to view details",
                    "repeaters": "Select a repeater to view details",
                }.get(self._kind, "Select a contact or repeater to view details"),
                vexpand=True,
            )
        )

    def _clear_details(self) -> None:
        """Clear the details content area."""
        clear_children(self._details_content)

    def _show_peer_details(self, peer: Peer) -> None:
        """Show details for a selected peer."""
        self._details_title.set_text(peer.display_name)
        self._set_room_title(peer)
        is_room = self._title_pill.get_visible()
        self._sync_admin(peer if peer.is_repeater else None)  # [admin-fold patch]
        if getattr(self, "_admin", None) is not None and peer.is_repeater:  # [repeater-tabs patch]
            self._show_repeater_extras(peer)  # signal, location etc. are in Admin's Status
            return
        self._clear_details()
        if self._kind == "rooms":  # [room-chat patch] log in, read and post first
            from meshcore_console.ui_gtk.widgets.room_view import RoomPanel

            self._details_content.append(RoomPanel(self._service, peer))

        # Type badge
        type_label = Gtk.Label(
            label="Repeater"
            if peer.is_repeater
            # [rooms patch]
            else {3: "Room server", 4: "Sensor"}.get(getattr(peer, "node_type", None), "Contact")
        )
        type_label.add_css_class("status-pill")
        type_label.add_css_class(
            "warn"
            if peer.is_repeater
            # [room-marker patch]
            else "room"
            if getattr(peer, "node_type", None) == 3
            else "ok"
        )
        type_label.set_halign(Gtk.Align.START)
        if not is_room:  # rooms show it in the title row
            self._details_content.append(type_label)

        # === Signal Section ===
        self._add_section_header("Signal")

        if peer.rssi is not None:
            self._details_content.append(DetailRow("RSSI:", format_rssi(peer.rssi)))
        if peer.snr is not None:
            self._details_content.append(DetailRow("SNR:", format_snr(peer.snr)))
        if peer.signal_quality is not None:
            self._details_content.append(DetailRow("Quality:", f"{peer.signal_quality}%"))

        if peer.rssi is None and peer.snr is None and peer.signal_quality is None:
            no_signal = Gtk.Label(label="No signal data available")
            no_signal.add_css_class("panel-muted")
            no_signal.set_halign(Gtk.Align.START)
            self._details_content.append(no_signal)

        # === Location Section ===
        self._add_section_header("Location")

        if peer.latitude is not None and peer.longitude is not None:
            coords = format_coordinates(peer.latitude, peer.longitude)
            self._details_content.append(DetailRow("Coordinates:", coords))

            if peer.location_updated:
                loc_time = to_local(peer.location_updated).strftime("%b %d at %H:%M")
                self._details_content.append(DetailRow("Updated:", loc_time))
            if is_room and _has_location(peer.latitude, peer.longitude):
                self._add_mini_map(peer)
        else:
            no_loc = Gtk.Label(label="No location reported")
            no_loc.add_css_class("panel-muted")
            no_loc.set_halign(Gtk.Align.START)
            self._details_content.append(no_loc)

        # === Last Seen Section ===
        self._add_section_header("Activity")

        if peer.last_advert_time:
            time_str = to_local(peer.last_advert_time).strftime("%b %d, %Y at %I:%M %p")
            self._details_content.append(DetailRow("Last Seen:", time_str))
        else:
            self._details_content.append(DetailRow("Last Seen:", "Unknown"))

        # === Network Path Section ===
        self._add_section_header("Network Path")

        if peer.last_path:
            peer_prefix = (peer.public_key or peer.display_name)[:2].upper()
            self_key = self._service.get_self_public_key()
            self_prefix = (self_key or "")[:2].upper() or "Me"
            self_name = self._service.get_status().node_id
            path = PathVisualization(
                hops=peer.last_path,
                peers=self._service.list_peers(),
                start=(self_prefix, self_name, None, STYLE_SELF),
                end=(peer_prefix, peer.display_name, peer, STYLE_DEFAULT),
            )
            path.set_min_children_per_line(
                min(len(peer.last_path) + 2, 6)
            )  # [peers-trace patch] one row,
            self._details_content.append(path)  # so the badges never stack over what follows
            if not peer.is_repeater:  # [hop-target patch] click a repeater: 'Trace to here'
                from meshcore_console.ui_gtk.widgets.trace_view import enable_hop_targets

                enable_hop_targets(path)
        else:
            direct_label = Gtk.Label(label="Direct connection (no hops)")
            direct_label.add_css_class("panel-muted")
            direct_label.set_halign(Gtk.Align.START)
            self._details_content.append(direct_label)

        if (
            not peer.is_repeater
        ):  # [contact-trace patch] trace the repeaters on the route to a contact
            if peer.last_path and hasattr(self._service, "trace_hops"):
                c_btn = Gtk.Button(label="Trace route")
                c_btn.add_css_class("hop-trace-btn")  # [hop-target patch]
                c_btn.set_halign(Gtk.Align.START)
                c_btn.set_tooltip_text(
                    "Round-trip TRACE through the repeaters on the route to this contact, out to the one "
                    "nearest them and back (contacts don't relay TRACE themselves): SNR for every link"
                )
                c_results = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
                c_btn.connect("clicked", self._on_contact_trace_clicked, peer, c_results)
                self._details_content.append(c_btn)
                self._details_content.append(c_results)
                self._show_latest_trace(peer, c_btn, c_results)
            elif peer.last_path is not None and len(peer.last_path) == 0:
                c_note = Gtk.Label(
                    label="Heard directly (no repeaters), so there is no route to trace."
                )
                c_note.add_css_class("panel-muted")
                c_note.set_halign(Gtk.Align.START)
                self._details_content.append(c_note)

        if peer.is_repeater:  # [peers-trace patch] round-trip TRACE under the path
            trace_btn = Gtk.Button(label="Trace path")
            trace_btn.set_halign(Gtk.Align.START)
            trace_btn.set_tooltip_text(
                "Send a round-trip TRACE along this path: SNR for every link, both ways"
            )
            trace_results = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            trace_btn.connect("clicked", self._on_peers_trace_clicked, peer, trace_results)
            self._details_content.append(trace_btn)
            self._details_content.append(trace_results)
            self._show_latest_trace(
                peer, trace_btn, trace_results
            )  # [trace-source patch] newest from the log

        # === Public Key Section ===
        self._add_section_header("Public Key")

        key_text = format_public_key(peer.public_key)
        key_label = Gtk.Label(label=key_text)
        key_label.add_css_class("analyzer-raw")
        key_label.set_halign(Gtk.Align.START)
        key_label.set_wrap(True)
        key_label.set_wrap_mode(Pango.WrapMode.CHAR)
        key_label.set_selectable(True)
        self._details_content.append(key_label)

        # === Cached Telemetry ===
        cached = self._peer_telemetry.get(peer.peer_id)
        if cached is not None:
            self._render_telemetry(cached)

        # === Action buttons ===
        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        actions.set_margin_top(16)

        # Rooms: Favorite is in the title row, and posting is the room panel's Post.
        if not is_room:
            fav_label = "☆ Favorite" if not peer.is_favorite else "★ Unfavorite"
            fav_btn = Gtk.Button.new_with_label(fav_label)
            fav_btn.connect("clicked", self._on_toggle_favorite_clicked, peer)
            actions.append(fav_btn)

        if not peer.is_repeater and not is_room:
            message_btn = Gtk.Button.new_with_label("Send Message")
            message_btn.add_css_class("suggested-action")
            message_btn.connect("clicked", self._on_send_message_clicked, peer)
            actions.append(message_btn)

        if not is_room:  # room servers don't answer telemetry requests
            telem_btn = Gtk.Button.new_with_label("Request Telemetry")
            telem_btn.connect("clicked", self._on_request_telemetry_clicked, peer)
            actions.append(telem_btn)

        if actions.get_first_child() is not None:  # rooms have no buttons left here
            self._details_content.append(actions)

    _TRACE_SOURCES = {
        "auto": "auto-trace",
        "admin": "Admin",
        "map": "Map",
        "peers": "Peers",
        "channels": "Channels",
    }  # [channel-actions patch]

    def _show_latest_trace(
        self, peer: Peer, btn: Gtk.Button, results: Gtk.Box
    ) -> None:  # [trace-source patch]
        """Newest trace to this repeater: from the trace log (auto-trace, Admin, Map,
        Peers) or this screen's own memory, whichever is newer, with when and who."""
        import time as _time
        from datetime import datetime

        from meshcore_console.ui_gtk.widgets.trace_view import build_trace_view

        cache, running = self._peer_trace_state()
        self._peer_trace_widgets = (peer.peer_id, btn, results)
        if peer.peer_id in running:
            btn.set_sensitive(False)
            btn.set_label("Tracing\u2026")
            return
        best = None  # (time, result, source)
        mem = cache.get(peer.peer_id)
        if mem is not None:
            best = (mem[0], mem[1], "peers")
        try:
            store = self._service.get_repeater_samples()
            rows = (
                store.trace_series(peer.public_key, _time.time() - 30 * 86400)
                if peer.public_key
                else []
            )
        except Exception:  # noqa: BLE001
            rows = []
        if rows and (best is None or rows[-1]["t"] > best[0] + 1):
            row = rows[-1]
            result = {
                "success": row["ok"],
                "links": [
                    {"from": lk[0], "to": lk[1], "snr": lk[2]} for lk in row.get("links") or []
                ],
                "rtt_ms": row.get("rtt_ms") or 0,
                "reason": row.get("reason") or "no reply",
            }
            best = (row["t"], result, row.get("source"))
        if best is None:
            return
        when, result, source = best
        dt = datetime.fromtimestamp(when)
        stamp_txt = dt.strftime(
            "%H:%M:%S" if dt.date() == datetime.now().date() else "%b %-d %H:%M"
        )
        who = self._TRACE_SOURCES.get(source or "", "")
        stamp = Gtk.Label(label=f"Last trace at {stamp_txt}" + (f" ({who})" if who else ""))
        stamp.add_css_class("panel-muted")
        stamp.set_halign(Gtk.Align.START)
        results.append(stamp)
        self_key = self._service.get_self_public_key() or ""
        results.append(
            build_trace_view(
                result,
                self._service.list_peers(),
                target=peer.display_name,
                self_label=f"{self._service.get_status().node_id} ({self_key[:2].upper() or 'me'})",
                spreading_factor=self._service.get_settings().spreading_factor,
                compact=True,
            )
        )

    def _peer_trace_state(self) -> tuple[dict, set]:  # [peers-trace keep]
        """(last trace per peer id: (time, result), peer ids with a trace in flight)."""
        if not hasattr(self, "_peer_traces"):
            self._peer_traces: dict = {}
            self._peer_trace_running: set = set()
        return self._peer_traces, self._peer_trace_running

    def _restore_peer_trace(
        self, peer: Peer, btn: Gtk.Button, results: Gtk.Box
    ) -> None:  # [peers-trace keep]
        """After a panel rebuild: show the running state or the last trace again."""
        from datetime import datetime

        from meshcore_console.ui_gtk.widgets.trace_view import build_trace_view

        cache, running = self._peer_trace_state()
        self._peer_trace_widgets = (peer.peer_id, btn, results)
        if peer.peer_id in running:
            btn.set_sensitive(False)
            btn.set_label("Tracing\u2026")
            return
        cached = cache.get(peer.peer_id)
        if cached is None:
            return
        when, result = cached
        stamp = Gtk.Label(
            label=f"Last trace at {datetime.fromtimestamp(when).strftime('%H:%M:%S')}"
        )
        stamp.add_css_class("panel-muted")
        stamp.set_halign(Gtk.Align.START)
        results.append(stamp)
        self_key = self._service.get_self_public_key() or ""
        results.append(
            build_trace_view(
                result,
                self._service.list_peers(),
                target=peer.display_name,
                self_label=f"{self._service.get_status().node_id} ({self_key[:2].upper() or 'me'})",
                spreading_factor=self._service.get_settings().spreading_factor,
                compact=True,
            )
        )

    def _on_contact_trace_clicked(
        self, btn: Gtk.Button, peer: Peer, results: Gtk.Box
    ) -> None:  # [contact-trace patch]
        """Trace the repeaters on a contact's route (our side first), out and back."""
        import threading

        from meshcore_console.ui_gtk.widgets.trace_view import build_trace_view

        hops = [str(h) for h in (peer.last_path or [])]
        from meshcore_console.ui_gtk.widgets.trace_view import hops_to_target, trace_button_label

        full_len = len(hops)
        hops = hops_to_target(btn, hops)  # [hop-target patch] only out to the picked repeater
        btn.set_sensitive(False)
        btn.set_label("Tracing\u2026")
        clear_children(results)
        cache, running = self._peer_trace_state()
        running.add(peer.peer_id)
        relay = next(
            (
                p
                for p in self._service.list_peers()
                if p.is_repeater
                and p.public_key
                and p.public_key.lower().startswith(hops[-1].lower())
            ),
            None,
        )
        relay_name = relay.display_name if relay is not None else hops[-1]

        def work() -> None:
            try:
                from meshcore_console.meshcore.trace import trace_source

                with trace_source("peers"):
                    result = self._service.trace_hops(hops)
            except Exception as exc:  # noqa: BLE001
                result = {"success": False, "reason": str(exc) or type(exc).__name__}

            def done() -> bool:
                import time as _time

                running.discard(peer.peer_id)
                cache[peer.peer_id] = (_time.time(), result)
                live = getattr(self, "_peer_trace_widgets", None)
                b, res = (live[1], live[2]) if live and live[0] == peer.peer_id else (btn, results)
                b.set_sensitive(True)
                b.set_label(trace_button_label(b))  # [hop-target patch]
                if self._selected_peer is None or self._selected_peer.peer_id != peer.peer_id:
                    return False  # another peer is shown now; the result is kept for this one
                clear_children(res)
                where = (
                    "(the repeater nearest them)"
                    if len(hops) == full_len
                    else f"(hop {len(hops)} of {full_len})"
                )  # [hop-target patch]
                note = Gtk.Label(
                    label=f"Route to {peer.display_name}, traced out to {relay_name} "
                    f"{where} and back."
                )
                note.add_css_class("panel-muted")
                note.set_wrap(True)
                note.set_xalign(0.0)
                note.set_halign(Gtk.Align.START)
                res.append(note)
                self_key = self._service.get_self_public_key() or ""
                res.append(
                    build_trace_view(
                        result,
                        self._service.list_peers(),
                        target=relay_name,
                        self_label=f"{self._service.get_status().node_id} ({self_key[:2].upper() or 'me'})",
                        spreading_factor=self._service.get_settings().spreading_factor,
                        compact=True,
                    )
                )
                if not result.get("success") and not result.get("busy"):  # [find-break patch]
                    from meshcore_console.ui_gtk.widgets.trace_view import find_break_widget

                    res.append(find_break_widget(self._service, hops, "peers"))
                return False

            GLib.idle_add(done)

        threading.Thread(target=work, daemon=True).start()

    def _on_peers_trace_clicked(
        self, btn: Gtk.Button, peer: Peer, results: Gtk.Box
    ) -> None:  # [peers-trace patch]
        import threading

        from meshcore_console.ui_gtk.widgets.trace_view import build_trace_view

        btn.set_sensitive(False)
        btn.set_label("Tracing\u2026")
        clear_children(results)
        self._peer_trace_state()[1].add(peer.peer_id)  # [peers-trace keep]

        def work() -> None:
            try:
                from meshcore_console.meshcore.trace import trace_source  # [trace-source patch]

                with trace_source("peers"):
                    result = self._service.trace_repeater(peer.display_name)
            except Exception as exc:  # noqa: BLE001
                result = {"success": False, "reason": str(exc) or type(exc).__name__}

            def done() -> bool:
                import time as _time  # [peers-trace keep]

                cache, running = self._peer_trace_state()
                running.discard(peer.peer_id)
                cache[peer.peer_id] = (_time.time(), result)
                live = getattr(self, "_peer_trace_widgets", None)
                b, res = (live[1], live[2]) if live and live[0] == peer.peer_id else (btn, results)
                b.set_sensitive(True)
                b.set_label("Trace path")
                if self._selected_peer is None or self._selected_peer.peer_id != peer.peer_id:
                    return False  # another peer is shown now; the result is kept for it
                clear_children(res)
                self_key = self._service.get_self_public_key() or ""
                res.append(  # [peers-trace keep]
                    build_trace_view(
                        result,
                        self._service.list_peers(),
                        target=peer.display_name,
                        self_label=f"{self._service.get_status().node_id} ({self_key[:2].upper() or 'me'})",
                        spreading_factor=self._service.get_settings().spreading_factor,
                        compact=True,  # [peers-trace fix] full table overflowed the window
                    )
                )
                logger.info(
                    "trace %s: %s",
                    peer.display_name,
                    "ok" if result.get("success") else result.get("reason"),
                )
                return False

            GLib.idle_add(done)

        threading.Thread(target=work, daemon=True).start()

    def _add_section_header(self, title: str) -> None:
        """Add a section header to the details panel."""
        header = Gtk.Label(label=title)
        header.add_css_class("message-detail-header")
        header.set_halign(Gtk.Align.START)
        header.set_margin_top(12)
        self._details_content.append(header)

    def _on_peers_shown(self, _widget) -> None:  # [map-select patch]
        """Select the node last opened on the Map, if that's newer than what we applied."""
        if self._stale:  # [rooms patch] changes heard while this tab was hidden
            self._stale = False
            self._refresh_peers()
        root = self.get_root()
        picked = getattr(root, "_map_selected", None) if root is not None else None
        if not picked or picked[1] == getattr(self, "_map_applied", 0):
            return
        self._map_applied = picked[1]
        if self._selected_peer is None or self._selected_peer.peer_id != picked[0]:
            GLib.idle_add(lambda: (self.select_peer(picked[0]), False)[1])

    def select_peer(self, peer_id: str) -> None:
        """Select a peer programmatically and show its details."""
        for listbox in (self._contacts_list, self._network_list):
            index = 0
            while True:
                row = listbox.get_row_at_index(index)
                if row is None:
                    break
                peer = getattr(row, "peer", None)
                if peer and peer.peer_id == peer_id:
                    listbox.select_row(row)
                    row.grab_focus()  # [neighbors patch] scroll it into view
                    return
                index += 1

    def _reselect_peer(self, display_name: str) -> None:
        """Re-select a peer by name after a list refresh."""
        for listbox in (self._contacts_list, self._network_list):
            index = 0
            while True:
                row = listbox.get_row_at_index(index)
                if row is None:
                    break
                peer = getattr(row, "peer", None)
                if peer and peer.display_name == display_name:
                    listbox.select_row(row)
                    return
                index += 1

    def _on_toggle_favorite_clicked(self, _button: Gtk.Button, peer: Peer) -> None:
        """Toggle the favorite status of a peer."""
        logger.debug(
            "UI: toggle favorite peer=%s currently=%s", peer.display_name, peer.is_favorite
        )
        self._service.set_favorite(peer.peer_id, not peer.is_favorite)
        GLib.idle_add(self._refresh_peers)

    # --- [clear-peers patch] -------------------------------------------------
    def _list_header(self, title: str, *, repeaters: bool) -> Gtk.Box:
        """Column header: title on the left, Clear button on the right."""
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        box.set_margin_start(12)
        box.set_margin_end(8)
        box.set_margin_top(10)
        box.set_margin_bottom(8)
        label = Gtk.Label(label=title)
        label.add_css_class("panel-title")
        label.set_halign(Gtk.Align.START)
        label.set_hexpand(True)
        box.append(label)
        box.append(self._sort_dropdown(repeaters))  # [peer-sort patch]
        button = Gtk.Button(label="Clear")
        button.add_css_class("flat")
        button.set_valign(Gtk.Align.CENTER)
        button.set_tooltip_text(f"Delete all {title.lower()}")
        button.connect("clicked", self._on_clear_clicked, repeaters)
        box.append(button)
        return box

    # --- [peer-sort patch] ---------------------------------------------------
    def _sort_dropdown(self, repeaters: bool) -> Gtk.DropDown:
        dropdown = Gtk.DropDown.new_from_strings(list(SORT_LABELS))
        dropdown.set_selected(SORT_MODES.index(self._sort_modes[repeaters]))
        dropdown.set_valign(Gtk.Align.CENTER)
        dropdown.set_tooltip_text("Sort order (favourites stay on top)")
        dropdown.connect("notify::selected", self._on_sort_changed, repeaters)
        return dropdown

    def _on_sort_changed(self, dropdown: Gtk.DropDown, _pspec, repeaters: bool) -> None:
        mode = SORT_MODES[dropdown.get_selected()]
        logger.debug("UI: sort %s by %s", "repeaters" if repeaters else "contacts", mode)
        self._sort_modes[repeaters] = mode
        _save_sort_modes(self._sort_modes)
        if mode == "distance" and self._sort_origin() is None:
            self._toast("No GPS fix or fixed position in Settings; can't sort by distance")
        self._refresh_peers()

    def _sort_origin(self) -> tuple[float, float] | None:
        """Live GPS, else the fixed position from Settings (even if not shared)."""
        loc = self._service.get_device_location()
        if loc and _has_location(loc[0], loc[1]):
            return loc
        s = self._service.get_settings()
        if _has_location(s.latitude, s.longitude):
            return (s.latitude, s.longitude)
        return None

    def _compute_distances(self, peers: list[Peer]) -> dict[str, float]:
        if "distance" not in self._sort_modes.values():
            return {}
        origin = self._sort_origin()
        if origin is None:
            return {}
        return {
            p.peer_id: _distance_km(origin, (p.latitude, p.longitude))
            for p in peers
            if _has_location(p.latitude, p.longitude)
        }

    def _sort_peers(self, peers: list[Peer], repeaters: bool) -> list[Peer]:
        mode = self._sort_modes[repeaters]

        def seen(p: Peer) -> float:
            return p.last_advert_time.timestamp() if p.last_advert_time else 0.0

        def name(p: Peer) -> str:
            return (p.display_name or "").casefold()

        if mode == "name":
            key = lambda p: (not p.is_favorite, name(p))  # noqa: E731
        elif mode == "oldest":
            key = lambda p: (not p.is_favorite, p.last_advert_time is None, seen(p))  # noqa: E731
        elif mode == "distance":
            d = self._distances
            key = lambda p: (  # noqa: E731
                not p.is_favorite,
                p.peer_id not in d,
                d.get(p.peer_id, 0.0),
                name(p),
            )
        else:  # newest
            key = lambda p: (not p.is_favorite, p.last_advert_time is None, -seen(p))  # noqa: E731
        return sorted(peers, key=key)

    def _toast(self, message: str) -> None:
        root = self.get_root()
        overlay = getattr(root, "_toast_overlay", None) if root is not None else None
        if overlay is not None:
            overlay.add_toast(Adw.Toast.new(message))

    # --- [delete-peer patch] --------------------------------------------------
    def _add_row_menu(self, row: Gtk.ListBoxRow, peer: Peer) -> None:
        # Right-click anywhere on the row, including its badge: the capture phase
        # sees the click before the badge's own right-click handler does.
        click = Gtk.GestureClick.new()
        click.set_button(3)
        click.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        click.connect("pressed", self._on_row_right_click, peer)  # [leak-fix patch] no row ref
        row.add_controller(click)
        if peer.is_repeater:  # double-click: show it on the full Map
            double = Gtk.GestureClick.new()
            double.set_button(1)
            double.connect("pressed", self._on_row_double_click, peer.peer_id)
            row.add_controller(double)

    def _on_row_double_click(self, _gesture, n_press: int, _x: float, _y: float, peer_id) -> None:
        if n_press != 2:
            return  # a single click just selects, as before
        peer = next((p for p in self._service.list_peers() if p.peer_id == peer_id), None)
        if peer is None or not _has_location(peer.latitude, peer.longitude):
            self._toast("No location for that repeater yet")
            return
        navigate(self, "map", ("focus_peer", peer_id))

    def _on_row_right_click(self, gesture, _n_press, x: float, y: float, peer: Peer) -> None:
        from gi.repository import Gdk

        row = gesture.get_widget()  # [leak-fix patch]
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        popover = Gtk.Popover()
        popover.set_parent(row)
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        popover.set_pointing_to(rect)
        kind = "repeater" if peer.is_repeater else "contact"
        menu = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        if peer.is_repeater:
            current = next(
                (p for p in self._service.list_peers() if p.peer_id == peer.peer_id), peer
            )
            fav = Gtk.Button(label="★ Unfavorite" if current.is_favorite else "☆ Favorite")
            fav.add_css_class("flat")

            def _fav_clicked(_b) -> None:
                popover.popdown()
                self._on_toggle_favorite_clicked(_b, current)
                admin = getattr(self, "_admin", None)
                sel = self._selected_peer
                if admin is not None and sel is not None and sel.peer_id == current.peer_id:
                    admin._update_admin_path(current)  # relabel the header's Favorite

            fav.connect("clicked", _fav_clicked)
            menu.append(fav)
        button = Gtk.Button(label=f"Delete {kind}…")
        button.add_css_class("flat")

        def _clicked(_b) -> None:
            popover.popdown()
            self._confirm_delete_peer(peer)

        button.connect("clicked", _clicked)
        menu.append(button)
        popover.set_child(menu)
        popover.connect("closed", lambda p: GLib.idle_add(p.unparent))
        popover.popup()

    def _confirm_delete_peer(self, peer: Peer) -> None:
        kind = "repeater" if peer.is_repeater else "contact"
        body = f"Removes this {kind} from your list. It comes back if its advert is heard again."
        if peer.is_favorite:
            body += "\n\nIt is a favourite (\u2605)."
        dialog = build_alert_dialog(self.get_root(), f"Delete {peer.display_name}?", body)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("delete", "Delete")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", self._on_delete_peer_confirmed, peer.peer_id)
        present_dialog(dialog, self.get_root())

    def _on_delete_peer_confirmed(self, _dialog, response: str, peer_id: str) -> None:
        if response != "delete":
            return
        if not self._service.delete_peer(peer_id):
            return
        logger.info("UI: deleted peer %s", peer_id)
        was_selected = self._selected_peer is not None and self._selected_peer.peer_id == peer_id
        self._refresh_peers()
        if was_selected:
            # GTK moves focus to a neighbouring row and selection follows focus;
            # show an empty details panel instead of jumping to another peer.
            def _clear() -> bool:
                self._contacts_list.unselect_all()
                self._network_list.unselect_all()
                self._selected_peer = None
                self._show_empty_details()
                return False

            _clear()
            GLib.idle_add(_clear)

    def _on_clear_clicked(self, _button: Gtk.Button, repeaters: bool) -> None:
        kind = (
            "repeaters" if repeaters else ("rooms" if self._kind == "rooms" else "contacts")
        )  # [rooms patch]
        peers = [
            p
            for p in self._service.list_peers()
            if bool(p.is_repeater) == repeaters and self._in_view(p)
        ]
        if not peers:
            return
        favs = sum(1 for p in peers if p.is_favorite)
        body = (
            f"Removes all {len(peers)} {kind} from this list. "
            "They come back as their adverts are heard again."
        )
        if favs:
            body += f"\n\n{favs} of them are favourites (\u2605)."
        dialog = build_alert_dialog(self.get_root(), f"Delete all {kind}?", body)
        dialog.add_response("cancel", "Cancel")
        if favs and favs < len(peers):
            dialog.add_response("keep", "Keep favourites")
        dialog.add_response("all", "Delete all")
        dialog.set_response_appearance("all", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", self._on_clear_confirmed, repeaters)
        present_dialog(dialog, self.get_root())

    def _on_clear_confirmed(self, _dialog, response: str, repeaters: bool) -> None:
        if response not in ("keep", "all"):
            return
        removed = 0  # [rooms patch] only what this tab lists (Rooms must not clear Contacts)
        for p in list(self._service.list_peers()):
            if bool(p.is_repeater) != repeaters or not self._in_view(p):
                continue
            if response == "keep" and p.is_favorite:
                continue
            if self._service.delete_peer(p.peer_id):
                removed += 1
        logger.info("UI: cleared %d %s", removed, "repeaters" if repeaters else "contacts")
        if self._selected_peer is not None:
            names = {p.display_name for p in self._service.list_peers()}
            if self._selected_peer.display_name not in names:
                self._selected_peer = None
                self._show_empty_details()
        self._refresh_peers()

    def _on_send_message_clicked(self, _button: Gtk.Button, peer: Peer) -> None:
        """Navigate to messages view and start a conversation with this peer."""
        logger.debug("UI: send message to peer=%s", peer.display_name)
        navigate(self, "messages", ("select_channel", peer.display_name))

    def _on_request_telemetry_clicked(self, button: Gtk.Button, peer: Peer) -> None:
        """Request telemetry from a peer in a background thread."""
        import threading

        button.set_sensitive(False)
        button.set_label("Requesting...")

        # Add a spinner below the actions
        spinner_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        spinner_box.set_halign(Gtk.Align.START)
        spinner = Gtk.Spinner()
        spinner.start()
        spinner_box.append(spinner)
        spinner_label = Gtk.Label(label="Waiting for telemetry response...")
        spinner_label.add_css_class("panel-muted")
        spinner_box.append(spinner_label)
        self._details_content.append(spinner_box)

        def _do_request() -> None:
            try:
                logger.debug("telemetry request starting for peer=%s", peer.display_name)
                result = self._service.request_telemetry(peer.display_name)
                logger.debug("telemetry request result: %s", result)
                GLib.idle_add(self._show_telemetry_result, spinner_box, button, peer, result)
            except Exception as exc:
                logger.exception("telemetry request failed for peer=%s", peer.display_name)
                GLib.idle_add(self._show_telemetry_error, spinner_box, button, peer, str(exc))

        threading.Thread(target=_do_request, daemon=True, name="telemetry-req").start()

    def _show_telemetry_result(
        self, spinner_box: Gtk.Box, button: Gtk.Button, peer: Peer, result: dict
    ) -> None:
        """Display telemetry results in the details panel (called on main thread)."""
        self._peer_telemetry[peer.peer_id] = result

        if self._selected_peer is None or self._selected_peer.peer_id != peer.peer_id:
            return

        if spinner_box.get_parent() is not None:
            self._details_content.remove(spinner_box)
        button.set_sensitive(True)
        button.set_label("Request Telemetry")

        self._render_telemetry(result)
        self._scroll_details_to_bottom()

    def _show_telemetry_error(
        self, spinner_box: Gtk.Box, button: Gtk.Button, peer: Peer, error: str
    ) -> None:
        """Display telemetry error in the details panel (called on main thread)."""
        if self._selected_peer is None or self._selected_peer.peer_id != peer.peer_id:
            return

        if spinner_box.get_parent() is not None:
            self._details_content.remove(spinner_box)
        button.set_sensitive(True)
        button.set_label("Request Telemetry")

        self._add_section_header("Telemetry")

        err_scroll = Gtk.ScrolledWindow()
        err_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        err_scroll.set_max_content_height(120)
        err_scroll.set_propagate_natural_height(True)

        err_label = Gtk.Label(label=f"Request failed: {error}")
        err_label.add_css_class("panel-muted")
        err_label.set_halign(Gtk.Align.START)
        err_label.set_wrap(True)
        err_label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        err_label.set_selectable(True)
        err_scroll.set_child(err_label)
        self._details_content.append(err_scroll)

        self._scroll_details_to_bottom()

    def _render_telemetry(self, result: dict) -> None:
        """Render telemetry data widgets into the details panel."""
        self._add_section_header("Telemetry")

        if not result or not result.get("success"):
            reason = result.get("reason", "No telemetry data returned") if result else "No response"
            no_data = Gtk.Label(label=reason)
            no_data.add_css_class("panel-muted")
            no_data.set_halign(Gtk.Align.START)
            self._details_content.append(no_data)
            return

        rtt = result.get("rtt_ms")
        if rtt is not None:
            self._details_content.append(DetailRow("RTT:", f"{rtt:.0f} ms"))

        telemetry = result.get("telemetry_data") or {}
        sensors = telemetry.get("sensors", [])

        if not sensors:
            text = telemetry.get("formatted") or result.get("response_text") or "No sensor data"
            fallback = Gtk.Label(label=text)
            fallback.add_css_class("panel-muted")
            fallback.set_halign(Gtk.Align.START)
            self._details_content.append(fallback)
            return

        for sensor in sensors:
            value = sensor.get("value")
            sensor_type = sensor.get("type", "Unknown")

            if isinstance(value, dict) and "latitude" in value:
                self._details_content.append(
                    DetailRow(
                        "Location:", format_coordinates(value["latitude"], value["longitude"])
                    )
                )
                alt = value.get("altitude")
                if alt is not None and alt != 0.0:
                    self._details_content.append(DetailRow("Altitude:", f"{alt:.1f} m"))
            elif sensor_type == "Voltage":
                self._details_content.append(DetailRow("Battery:", f"{value:.2f} V"))
            elif sensor_type == "Temperature":
                self._details_content.append(DetailRow("Temperature:", f"{value:.1f} °C"))
            elif sensor_type == "Humidity":
                self._details_content.append(DetailRow("Humidity:", f"{value:.0f}%"))
            elif sensor_type == "Barometer":
                self._details_content.append(DetailRow("Pressure:", f"{value:.1f} hPa"))
            else:
                self._details_content.append(DetailRow(f"{sensor_type}:", str(value)))

    def _scroll_details_to_bottom(self) -> None:
        """Scroll the details panel to the bottom after content is added."""

        def _do_scroll() -> bool:
            vadj = self._details_scroll.get_vadjustment()
            vadj.set_value(vadj.get_upper())
            return False

        # Defer so GTK has time to allocate the new widgets
        GLib.idle_add(_do_scroll)
