"""Reusable clickable node identifier badge.

Displays a small 2-char hex prefix. Hover shows the node's display name.
Left-click opens a popover with node details. Right-click navigates to the
Peers tab with the node selected.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import GLib, Gtk

from meshcore_console.core.models import Peer
from meshcore_console.core.time import to_local
from meshcore_console.ui_gtk.helpers import navigate

STYLE_DEFAULT = "default"  # Blue (contacts, general nodes)
STYLE_REPEATER = "repeater"  # Amber (repeater/relay hops)
STYLE_SELF = "self"  # Green (self node)


def peers_for_hop(peers: list[Peer], hop: str) -> list[Peer]:
    """Every known peer a hop identifier could be, likeliest first. [hop-rank patch]

    A 1-byte path hash (e.g. 'F7') is shared by about one repeater in 256, so on
    a busy mesh several often match. Rank: exact name / id, then repeaters (path
    hops relay), favourites, nodes heard directly (zero-hop), most recently heard.
    """
    hop = str(hop)
    exact = [p for p in peers if p.display_name == hop or p.peer_id == hop]
    if exact:
        return exact
    hop_lower = hop.lower()
    matches = [p for p in peers if p.public_key and p.public_key.lower().startswith(hop_lower)]

    def rank(p: Peer):
        seen = getattr(p, "last_advert_time", None)
        heard_direct = getattr(p, "last_path", None) == [] and getattr(p, "rssi", None) is not None
        return (
            not p.is_repeater,
            not getattr(p, "is_favorite", False),
            not heard_direct,
            -(seen.timestamp() if seen is not None else 0.0),
        )

    return sorted(matches, key=rank)


def find_peer_for_hop(peers: list[Peer], hop: str) -> Peer | None:
    """Best-effort match of a path hop identifier to a known peer (likeliest of several)."""
    found = peers_for_hop(peers, hop)
    return found[0] if found else None


# [leak-fix patch] Handlers connected as self._method make a reference cycle
# through the GClosure that Python's GC cannot collect, so every badge in a
# rebuilt list leaked. These look the badge up from the event instead.
def _badge_left_click(gesture, n_press, x, y):
    badge = gesture.get_widget()
    if badge is not None:
        badge._on_left_click(gesture, n_press, x, y)


def _badge_right_click(gesture, n_press, x, y):
    badge = gesture.get_widget()
    if badge is not None:
        badge._on_right_click(gesture, n_press, x, y)


def _badge_action(btn, fn):  # [hop-target patch] popover action, then close it
    pop = btn.get_ancestor(Gtk.Popover)
    fn(btn)
    if pop is not None:
        pop.popdown()


def _badge_destroy(badge):
    if hasattr(badge, "_popover_timer"):
        badge._on_destroy(badge)
        return
    # Python state already released: just detach any popover child.
    child = badge.get_first_child()
    while child is not None:
        nxt = child.get_next_sibling()
        if isinstance(child, Gtk.Popover):
            child.unparent()
        child = nxt


class NodeBadge(Gtk.Box):
    """Clickable node identifier badge with hover tooltip and click popover.

    Uses a plain Gtk.Box with a styled label to avoid Gtk.MenuButton's
    internal dimming of flat toggle buttons in Adwaita.

    - Left click: opens popover with node details.
    - Right click: navigates to the Peers tab with the node selected.
    - Double click: shows the node on the Map tab. [map-jump patch]

    Usage::

        badge = NodeBadge("A3", "Alice", peer=alice_peer)
        badge = NodeBadge("B7", "B7", style=STYLE_REPEATER)
        badge = NodeBadge("Me", "You", style=STYLE_SELF)
    """

    def __init__(
        self,
        prefix: str,
        display_name: str,
        peer: Peer | None = None,
        style: str = STYLE_DEFAULT,
    ) -> None:
        super().__init__()
        self.add_css_class("node-badge")

        self._peer = peer
        self._prefix = prefix
        self._display_name = display_name
        self._popover: Gtk.Popover | None = None
        self._style = style  # [map-jump patch]
        self._popover_timer = 0

        # Inner badge label
        label = Gtk.Label(label=prefix)
        label.add_css_class("node-prefix")
        if style == STYLE_REPEATER:
            label.add_css_class("node-prefix-repeater")
        elif style == STYLE_SELF:
            label.add_css_class("node-prefix-self")
        if peer is not None and peer.is_favorite:
            label.add_css_class("node-prefix-favorite")
        self.append(label)

        # Accessible label and tooltip
        self.update_property([Gtk.AccessibleProperty.LABEL], [f"Node: {display_name}"])
        self.set_tooltip_text(display_name)

        # Left click → open popover
        left_click = Gtk.GestureClick.new()
        left_click.set_button(1)
        left_click.connect("released", _badge_left_click)  # [leak-fix patch]
        self.add_controller(left_click)

        self.connect("destroy", _badge_destroy)  # [leak-fix patch]

        # Right click → navigate to Peers tab
        if peer is not None:
            right_click = Gtk.GestureClick.new()
            right_click.set_button(3)
            right_click.connect("released", _badge_right_click)  # [leak-fix patch]
            self.add_controller(right_click)

    def _on_destroy(self, _widget: Gtk.Widget) -> None:
        if self._popover_timer:  # [map-jump patch]
            GLib.source_remove(self._popover_timer)
            self._popover_timer = 0
        if self._popover is not None:
            self._popover.unparent()
            self._popover = None

    def _on_left_click(self, gesture: Gtk.GestureClick, n_press: int, _x: float, _y: float) -> None:
        # [map-jump patch] A single click opens the popover once the double-click
        # window has passed; a double click jumps to the node on the Map tab.
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        if self._popover_timer:
            GLib.source_remove(self._popover_timer)
            self._popover_timer = 0
        if n_press >= 2:
            if self._popover is not None and self._popover.get_visible():
                self._popover.popdown()
            self._show_on_map()
            return
        settings = Gtk.Settings.get_default()
        delay = settings.props.gtk_double_click_time if settings is not None else 400
        self._popover_timer = GLib.timeout_add(delay, self._toggle_popover)

    def _toggle_popover(self) -> bool:
        self._popover_timer = 0
        self._open_or_close_popover()
        return False

    def _open_or_close_popover(self) -> None:
        if self._popover is not None and self._popover.get_visible():
            self._popover.popdown()
            return
        if self._popover is None:
            self._popover = Gtk.Popover.new()
            self._popover.set_parent(self)
        content = self._build_popover(self._prefix, self._display_name, self._peer)
        actions = getattr(self, "popover_actions", None)  # [hop-target patch]
        for label, fn in actions(self) if actions else []:
            act = Gtk.Button(label=label)
            act.add_css_class("flat")
            act.set_margin_top(4)
            act.connect("clicked", _badge_action, fn)
            content.append(act)
        self._popover.set_child(content)
        self._popover.popup()

    def _on_right_click(
        self, gesture: Gtk.GestureClick, _n_press: int, _x: float, _y: float
    ) -> None:
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        if self._popover is not None and self._popover.get_visible():
            self._popover.popdown()
        self._navigate_to_peer()

    def _navigate_to_peer(self) -> None:
        """Navigate to the Peers tab with this peer selected."""
        if self._peer is None:
            return
        navigate(self, "peers", ("select_peer", self._peer.peer_id))

    def _show_on_map(self) -> None:
        """[map-jump patch] Jump to this node on the Map tab."""
        if self._peer is None:
            if self._style == STYLE_SELF:
                navigate(self, "map", ("center_on_device", ""))
            else:
                self._toast(f"{self._display_name}: unknown node, not on the map")
            return
        if self._peer.latitude is None or self._peer.longitude is None:
            self._toast(f"{self._peer.display_name} has no location yet")
            return
        navigate(self, "map", ("focus_peer", self._peer.peer_id))

    def _toast(self, message: str) -> None:
        root = self.get_root()
        overlay = getattr(root, "_toast_overlay", None) if root is not None else None
        if overlay is None:
            return
        gi.require_version("Adw", "1")
        from gi.repository import Adw

        overlay.add_toast(Adw.Toast.new(message))

    @staticmethod
    def _build_popover(prefix: str, display_name: str, peer: Peer | None) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_margin_top(8)
        box.set_margin_bottom(8)
        box.set_margin_start(10)
        box.set_margin_end(10)

        # Display name
        name_label = Gtk.Label(label=display_name)
        name_label.add_css_class("node-popover-name")
        name_label.set_halign(Gtk.Align.START)
        box.append(name_label)

        if peer is None:
            # Minimal info
            id_label = Gtk.Label(label=f"Node: {prefix}")
            id_label.add_css_class("panel-muted")
            id_label.set_halign(Gtk.Align.START)
            box.append(id_label)
            return box

        # Type
        type_text = "Repeater" if peer.is_repeater else "Contact"
        type_label = Gtk.Label(label=type_text)
        type_label.add_css_class("panel-muted")
        type_label.set_halign(Gtk.Align.START)
        box.append(type_label)

        # Signal quality
        if peer.signal_quality is not None:
            sig = Gtk.Label(label=f"Signal: {peer.signal_quality}%")
            sig.set_halign(Gtk.Align.START)
            box.append(sig)

        # RSSI
        if peer.rssi is not None:
            rssi = Gtk.Label(label=f"RSSI: {peer.rssi} dBm")
            rssi.add_css_class("panel-muted")
            rssi.set_halign(Gtk.Align.START)
            box.append(rssi)

        # Last seen
        if peer.last_advert_time:
            time_str = to_local(peer.last_advert_time).strftime("%b %d at %H:%M")
            seen = Gtk.Label(label=f"Seen: {time_str}")
            seen.add_css_class("panel-muted")
            seen.set_halign(Gtk.Align.START)
            box.append(seen)

        # Public key (truncated)
        if peer.public_key:
            chunks = [
                peer.public_key[i : i + 4] for i in range(0, min(len(peer.public_key), 16), 4)
            ]
            key_short = " ".join(chunks) + " ..."
            key = Gtk.Label(label=key_short)
            key.add_css_class("analyzer-raw")
            key.set_halign(Gtk.Align.START)
            box.append(key)

        return box
