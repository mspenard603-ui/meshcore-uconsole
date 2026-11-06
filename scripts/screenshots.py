"""Regenerate the README screenshots (docs/*.png) from the app in mock mode.

    nix develop path:. --command uv run python scripts/screenshots.py

Opens the real main window at 1280x720 (the uConsole screen) with mock data,
visits each screen and renders the window to a PNG. Needs a display (Wayland or
X11) and network access for the map tiles.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ["MESHCORE_MOCK"] = "1"

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gio, GLib, Gtk  # noqa: E402

from meshcore_console.app import MeshcoreApplication  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "docs"


def _render(win: Gtk.Window, name: str) -> None:
    w, h = win.get_width(), win.get_height()
    paintable = Gtk.WidgetPaintable.new(win)
    snap = Gtk.Snapshot()
    paintable.snapshot(snap, w, h)
    texture = win.get_native().get_renderer().render_texture(snap.to_node(), None)
    texture.save_to_png(str(OUT / name))
    print(f"docs/{name}  {w}x{h}")


def main() -> int:
    app = MeshcoreApplication()
    app.set_flags(Gio.ApplicationFlags.NON_UNIQUE)  # run beside an open instance

    def win() -> Gtk.Window:
        return app.props.active_window

    def view(name: str):
        return win()._stack.get_child_by_name(name)

    def admin():
        return view("peers")._admin

    def fit_favourites() -> None:
        """Frame the favourite repeaters: turning links on leaves the view alone."""
        m = view("map")
        m._fit_route(
            [
                (p.latitude, p.longitude)
                for p in m._service.list_peers()
                if p.is_repeater and p.is_favorite and p.latitude is not None
            ]
        )

    def dismiss_toasts() -> None:
        win()._toast_overlay.dismiss_all()

    def graph_mode(label: str) -> None:
        """Click the Repeaters > Graph button labelled *label* (Signal, Packets, ...)."""
        stack = [admin()._graph_page]
        while stack:
            w = stack.pop()
            if isinstance(w, Gtk.ToggleButton) and w.get_label() == label:
                w.set_active(True)
                return
            child = w.get_first_child()
            while child is not None:
                stack.append(child)
                child = child.get_next_sibling()

    def select_row(listbox: Gtk.ListBox, index: int) -> None:
        row = listbox.get_row_at_index(index)
        if row is not None:
            listbox.select_row(row)

    # (what to do, seconds to wait, screenshot to take afterwards or None)
    steps = [
        (lambda: win().navigate_to("analyzer"), 1.0, None),
        (lambda: select_row(view("analyzer")._stream, 2), 1.0, "analyzer.png"),
        (lambda: view("analyzer").close_active_detail(), 0.3, None),
        (lambda: win().navigate_to("peers", ("select_peer", "peer-alice")), 1.5, "contacts.png"),
        (lambda: win().navigate_to("messages", ("select_channel", "public")), 1.5, "channels.png"),
        (lambda: win().navigate_to("peers", ("select_peer", "room-001")), 2.0, "rooms.png"),
        (lambda: win().navigate_to("peers", ("select_peer", "relay-002")), 1.5, None),
        (lambda: admin()._tab_btns[1].set_active(True), 0.5, None),
        (lambda: graph_mode("Packets"), 1.5, "repeater-graph.png"),
        (lambda: graph_mode("Signal"), 0.3, None),
        (lambda: admin()._tab_btns[3].set_active(True), 1.0, None),
        (lambda: select_row(admin()._packets_view._stream, 3), 1.0, "repeater-packets.png"),
        (lambda: admin()._tab_btns[0].set_active(True), 0.3, None),
        (lambda: win().navigate_to("map"), 1.5, None),
        # Favourite links button: one click is green (links), a second purple
        # (links, favourite repeaters only).
        (fit_favourites, 2.0, None),
        (lambda: view("map")._fav_links_btn.emit("clicked"), 1.5, None),
        (lambda: view("map")._fav_links_btn.emit("clicked"), 6.0, None),
        (dismiss_toasts, 1.0, "map.png"),
    ]

    def run(i: int = 0) -> bool:
        if i >= len(steps):
            app.quit()
            return False
        action, wait, shot = steps[i]
        action()

        def after() -> bool:
            if shot:
                win().present()  # an unfocused window renders greyed out (backdrop)
                _render(win(), shot)
            GLib.idle_add(run, i + 1)
            return False

        GLib.timeout_add(int(wait * 1000), after)
        return False

    def start() -> bool:
        w = win()
        w.unmaximize()
        w.set_default_size(1280, 720)
        GLib.timeout_add(2500, run)
        return False

    # the window is made in do_activate, which runs after this handler
    app.connect("activate", lambda _app: GLib.idle_add(start))
    GLib.timeout_add_seconds(90, app.quit)  # never hang
    return app.run([])


if __name__ == "__main__":
    sys.exit(main())
