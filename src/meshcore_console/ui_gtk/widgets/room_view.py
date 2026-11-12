"""Room server panel for the Rooms tab: log in, read posts, post. [room-chat patch]

A room server (simple_room_server firmware) keeps the last 32 posts. After a
login it pushes the ones newer than the 'sync since' we sent, one at a time,
each waiting for our ACK (openhop_core sends it); the client stores them in a
per-room channel. Posting is a plain text message to the room.

Password: the room password gives read/write, the admin password admin; a blank
one works when you are already on its access list or the room allows read-only
guests.

Signal handlers are module functions that find the panel from the widget, and
the refresh timer holds only a weak reference: a handler holding its own panel
would keep every closed panel alive (see the leak-fix patch).
"""

from __future__ import annotations

import threading
import time
import weakref

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk, Pango  # noqa: E402

from meshcore_console.core.time import to_local  # noqa: E402
from meshcore_console.ui_gtk.widgets.day_separator import DaySeparator  # noqa: E402
from meshcore_console.ui_gtk.widgets.message_bubble import MessageBubble  # noqa: E402

MAX_POST_LEN = 151  # firmware MAX_POST_TEXT_LEN (160 - 9)


def _muted(text: str = "") -> Gtk.Label:
    lab = Gtk.Label(label=text)
    lab.add_css_class("panel-muted")
    lab.set_wrap(True)
    lab.set_xalign(0.0)
    lab.set_halign(Gtk.Align.START)
    return lab


class RoomPanel(Gtk.Box):
    """Login row, the room's posts, and a compose box for one room server."""

    def __init__(self, service, room) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.room_name = room.display_name
        self._service = service
        self._shown = -1  # number of posts rendered

        # --- login row
        login = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.password = Gtk.PasswordEntry()
        self.password.set_show_peek_icon(True)
        self.password.set_hexpand(True)
        self.password.set_property(
            "placeholder-text", "Room password (blank = read-only, if the room allows it)"
        )
        saved = (
            service.get_saved_repeater_password(self.room_name)
            if hasattr(service, "get_saved_repeater_password")
            else None
        )
        if saved:
            self.password.set_text(saved)
        self.password.connect("activate", _on_login)
        login.append(self.password)
        self.login_btn = Gtk.Button(label="Log in")
        self.login_btn.connect("clicked", _on_login)
        login.append(self.login_btn)
        self.append(login)
        opts = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.save_pw = Gtk.CheckButton(label="Save password (logs in again when Console starts)")
        self.save_pw.set_active(bool(saved))
        opts.append(self.save_pw)
        self.append(opts)
        # One line, always: a long login error must not push the compose row off
        # screen. Ellipsized; the whole text is in the tooltip. max_width_chars(1)
        # keeps the label's natural width from growing the window (it fills via hexpand).
        self.status = _muted()
        self.status.set_wrap(False)
        self.status.set_single_line_mode(True)
        self.status.set_ellipsize(Pango.EllipsizeMode.END)
        self.status.set_max_width_chars(1)
        self.status.set_hexpand(True)
        self.status.set_halign(Gtk.Align.FILL)
        self.append(self.status)

        # --- posts: chat bubbles and day separators, as in Channels
        self.scroll = Gtk.ScrolledWindow()
        self.scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.scroll.set_min_content_height(120)
        self.scroll.set_vexpand(True)
        self.scroll.add_css_class("panel-card")
        self.posts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.posts.set_margin_start(12)
        self.posts.set_margin_end(12)
        self.posts.set_margin_top(8)
        self.posts.set_margin_bottom(8)
        self.scroll.set_child(self.posts)
        self.append(self.scroll)

        # --- compose
        compose = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.entry = Gtk.Entry()
        self.entry.set_hexpand(True)
        self.entry.set_max_length(MAX_POST_LEN)
        self.entry.set_placeholder_text(f"Post to {self.room_name}…")
        self.entry.connect("activate", _on_send)
        from meshcore_console.ui_gtk.widgets.auto_capitalize import attach_auto_capitalize

        attach_auto_capitalize(self.entry)
        compose.append(self.entry)
        self.send_btn = Gtk.Button(label="Post")
        self.send_btn.connect("clicked", _on_send)
        compose.append(self.send_btn)
        self.append(compose)

        self.refresh_status()
        self.refresh_posts()
        ref = weakref.ref(self)
        GLib.timeout_add(2000, _tick, ref)
        # The room's details follow this panel in the same scrolled page: fill
        # exactly the visible height so the compose row is never below the screen.
        self._fit = None  # (adjustment, handler id)
        self.connect("map", _fit_to_viewport)
        self.connect("unmap", _unfit)

    # --- display -------------------------------------------------------------
    def set_status(self, text: str) -> None:
        self.status.set_text(text)
        self.status.set_tooltip_text(text or None)

    def refresh_status(self) -> None:
        state = (
            self._service.get_room_login_state(self.room_name)
            if hasattr(self._service, "get_room_login_state")
            else None
        )
        if state:
            when = time.strftime("%H:%M", time.localtime(state["time"]))
            self.set_status(
                f"Logged in at {when} ({state['role']}). New posts arrive as the room pushes them."
            )
            self.login_btn.set_label("Log in again")
        else:
            self.set_status(
                "Not logged in this session. Log in to receive the room's posts; "
                "posts already received are shown below."
            )
            self.login_btn.set_label("Log in")

    def refresh_posts(self) -> None:
        posts = (
            self._service.list_room_posts(self.room_name)
            if hasattr(self._service, "list_room_posts")
            else []
        )
        # [chat-100 patch] the list is capped at 200: a new post keeps the count the
        # same, so compare the newest post too
        key = (len(posts), posts[-1].message_id if posts else None)
        if key == getattr(self, "_shown_key", None):
            return
        self._shown_key = key
        at_bottom = self._at_bottom()
        child = self.posts.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.posts.remove(child)
            child = nxt
        if not posts:
            self.posts.append(_muted("No posts yet."))
        today = GLib.DateTime.new_now_local().format("%Y-%m-%d")
        prev_date: str | None = None
        for m in sorted(posts, key=lambda m: m.created_at):
            day = to_local(m.created_at).strftime("%Y-%m-%d")
            if day != prev_date and not (prev_date is None and day == today):
                self.posts.append(DaySeparator(day))
            prev_date = day
            self.posts.append(MessageBubble(m, self._service, _on_post_clicked))
        self._shown = len(posts)
        if hasattr(self._service, "mark_room_read"):
            self._service.mark_room_read(self.room_name)
        if at_bottom or self._shown <= 1:
            GLib.idle_add(_scroll_to_end, weakref.ref(self))

    def _at_bottom(self) -> bool:
        adj = self.scroll.get_vadjustment()
        return adj.get_value() + adj.get_page_size() >= adj.get_upper() - 24

    def _on_message_reply_clicked(self, _btn: Gtk.Button, message) -> None:
        """The bubble's reply arrow: start the draft with @[author], as in Channels."""
        text = self.entry.get_text()
        mention = f"@[{message.sender_id}] "
        if not text.startswith(mention):
            self.entry.set_text(mention + text)
        self.entry.grab_focus()
        self.entry.set_position(-1)


def _on_post_clicked(_button: Gtk.Button, _message) -> None:
    """Room posts carry no route or signal details to show; nothing to open."""


def _fit_to_viewport(panel: RoomPanel) -> None:
    _unfit(panel)
    outer = panel.get_ancestor(Gtk.ScrolledWindow)
    if outer is None:
        return
    adj = outer.get_vadjustment()
    ref = weakref.ref(panel)

    def apply() -> bool:
        p = ref()
        if p is not None and p._fit is not None:
            height = int(p._fit[0].get_page_size())
            if height > 0 and p.get_size_request()[1] != height:
                p.set_size_request(-1, height)
        return False

    def on_page_size(_a, _pspec=None) -> None:
        # page-size changes during size allocation: resize after it, not inside it
        GLib.idle_add(apply)

    panel._fit = (adj, adj.connect("notify::page-size", on_page_size))
    on_page_size(adj)


def _unfit(panel: RoomPanel) -> None:
    if panel._fit is not None:
        adj, handler = panel._fit
        adj.disconnect(handler)
        panel._fit = None


def _scroll_to_end(ref) -> bool:
    panel = ref()
    if panel is not None:
        adj = panel.scroll.get_vadjustment()
        adj.set_value(adj.get_upper())
    return False


def _tick(ref) -> bool:
    panel = ref()
    if panel is None or panel.get_root() is None:
        return False  # panel closed: stop the timer
    panel.refresh_posts()
    return True


def _on_login(widget) -> None:
    panel = widget.get_ancestor(RoomPanel)
    if panel is None or not hasattr(panel._service, "login_to_room"):
        return
    password = panel.password.get_text()
    save = panel.save_pw.get_active()
    panel.login_btn.set_sensitive(False)
    panel.login_btn.set_label("Logging in…")
    service, name, ref = panel._service, panel.room_name, weakref.ref(panel)

    def work() -> None:
        result = service.login_to_room(name, password, save_password=save)

        def done() -> bool:
            p = ref()
            if p is None or p.get_root() is None:
                return False
            p.login_btn.set_sensitive(True)
            if result.get("success"):
                if not save and hasattr(service, "delete_saved_repeater_password"):
                    service.delete_saved_repeater_password(name)
                p.refresh_status()
            else:
                p.refresh_status()
                reason = result.get("reason", "no reply")
                hint = (
                    " (wrong password, or the room doesn't allow read-only guests)"
                    if "timeout" in reason.lower()
                    else ""
                )
                p.set_status(f"Login failed: {reason}{hint}.")
            return False

        GLib.idle_add(done)

    threading.Thread(target=work, daemon=True).start()


def _on_send(widget) -> None:
    panel = widget.get_ancestor(RoomPanel)
    if panel is None or not hasattr(panel._service, "send_room_post"):
        return
    from meshcore_console.ui_gtk.helpers import capitalize_first  # [capitalize patch]

    text = capitalize_first(panel.entry.get_text().strip())
    if not text:
        return
    panel.send_btn.set_sensitive(False)
    panel.entry.set_sensitive(False)
    service, name, ref = panel._service, panel.room_name, weakref.ref(panel)

    def work() -> None:
        result = service.send_room_post(name, text)

        def done() -> bool:
            p = ref()
            if p is None or p.get_root() is None:
                return False
            p.send_btn.set_sensitive(True)
            p.entry.set_sensitive(True)
            if result.get("success"):
                p.entry.set_text("")
                p.refresh_posts()
                GLib.idle_add(_scroll_to_end, ref)
            else:
                p.set_status(f"Post failed: {result.get('reason', 'no reply')}.")
            p.entry.grab_focus()
            return False

        GLib.idle_add(done)

    threading.Thread(target=work, daemon=True).start()


def auto_login_rooms(service, *, max_age: float | None = None) -> None:
    """Log in (one after another) to every room with a saved password that we
    aren't logged in to, or whose login is older than ``max_age`` seconds.
    Runs in a worker thread."""
    if hasattr(service, "relogin_rooms"):
        service.relogin_rooms(max_age=max_age)
        return
    if not hasattr(service, "login_to_room"):
        return
    rooms = [p for p in service.list_peers() if getattr(p, "node_type", None) == 3]
    for room in rooms:
        password = service.get_saved_repeater_password(room.display_name)
        if password is None or service.get_room_login_state(room.display_name):
            continue
        try:
            service.login_to_room(room.display_name, password, save_password=True)
        except Exception:  # noqa: BLE001
            pass
