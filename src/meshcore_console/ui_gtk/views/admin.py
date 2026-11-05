from __future__ import annotations

import logging
import threading
import time  # [repeater-stats patch]
from typing import TYPE_CHECKING

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gdk, GLib, Gtk, Pango

from meshcore_console.core.models import Peer
from meshcore_console.core.radio import format_rssi, format_snr
from meshcore_console.core.services import MeshcoreService
from meshcore_console.core.time import to_local
from meshcore_console.ui_gtk.helpers import clear_children
from meshcore_console.ui_gtk.layout import Layout
from meshcore_console.ui_gtk.state import UiEventStore
from meshcore_console.ui_gtk.widgets import DetailRow

if TYPE_CHECKING:
    from meshcore_console.core.models import RepeaterLoginState

logger = logging.getLogger(__name__)

_MAX_HISTORY = 50


def to_local_ts(ts: float | None) -> str:
    """Epoch seconds -> 'Oct 31 11:02 (12 min ago)'. [repeater-stats patch]"""
    if not ts:
        return "never"
    ago = max(0, int(time.time() - ts))
    if ago < 3600:
        rel = f"{ago // 60} min ago"
    elif ago < 86400:
        rel = f"{ago // 3600} h ago"
    else:
        rel = f"{ago // 86400} d ago"
    return f"{time.strftime('%b %-d %H:%M', time.localtime(ts))} ({rel})"


# --- [admin-sort patch] -------------------------------------------------------
ADMIN_SORT_MODES = ("name", "newest", "oldest", "signal", "distance", "logged_in")
ADMIN_SORT_LABELS = ("A\u2013Z", "Newest", "Oldest", "Signal", "Distance", "Logged in")


def _admin_sort_file():
    from meshcore_console.meshcore.paths import config_dir

    return config_dir() / "admin_sort.json"


def _load_admin_sort() -> str:
    import json

    try:
        mode = json.loads(_admin_sort_file().read_text()).get("mode")
    except (OSError, ValueError, AttributeError):
        mode = None
    return mode if mode in ADMIN_SORT_MODES else "name"


def _save_admin_sort(mode: str) -> None:
    import json

    try:
        path = _admin_sort_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"mode": mode}))
    except OSError as e:
        logger.warning("Could not save admin sort order: %s", e)


def _on_tab_toggled(btn: Gtk.ToggleButton, page: int) -> None:  # [repeater-tabs patch]
    if not btn.get_active():
        return
    view = _find_admin(btn)
    if view is not None and view._notebook.get_current_page() != page:
        view._notebook.set_current_page(page)


def _find_admin(widget: Gtk.Widget):
    """The switcher sits in the Repeaters title row, beside (not inside) the AdminView."""
    w = widget
    while w is not None:
        admin = getattr(w, "_admin", None)
        if admin is not None:
            return admin
        w = w.get_parent()
    return None


def _on_page_switched(notebook: Gtk.Notebook, _page, num: int) -> None:  # [repeater-tabs patch]
    admin = notebook.get_ancestor(AdminView)
    btns = getattr(admin, "_tab_btns", None) if admin is not None else None
    if btns and 0 <= num < len(btns) and not btns[num].get_active():
        btns[num].set_active(True)


class AdminView(Gtk.Box):
    def __init__(self, service: MeshcoreService, event_store: UiEventStore, layout: Layout) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._service = service
        self._event_store = event_store
        self._event_cursor = 0
        self._selected_repeater: Peer | None = None
        self._command_history: list[str] = []
        self._history_index = -1
        self._busy = False
        self._refreshing_list = False
        self._last_poll: dict[str, float] = {}  # [repeater-stats patch]
        self._poll_result: dict[str, str] = {}
        self._stats_to_console: set[str] = set()  # [stats-button patch]
        self._autologin_next: dict[str, float] = {}  # [auto-login patch]
        self._autologin_attempts: dict[str, int] = {}
        self._autologin_suppressed: set[str] = set()  # manual logout this session
        self._relogin_needed: set[str] = set()

        # -- Left: repeater list -----------------------------------------------
        list_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        list_col.add_css_class("panel-card")
        list_col.set_size_request(layout.admin_list_width, -1)

        header = Gtk.Label(label="Repeaters")
        header.add_css_class("panel-title")
        header.set_halign(Gtk.Align.START)
        header.set_margin_start(12)
        header.set_margin_top(10)
        header.set_margin_bottom(8)
        header.set_hexpand(True)  # [admin-sort patch] title + sort + Clear on one row
        head_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        head_row.set_margin_end(6)
        head_row.append(header)
        self._admin_sort = _load_admin_sort()
        self._admin_sort_btn = Gtk.MenuButton(label="⇅")  # compact: the list is narrow
        self._admin_sort_btn.add_css_class("flat")
        self._admin_sort_btn.set_valign(Gtk.Align.CENTER)
        sort_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        for side in ("start", "end", "top", "bottom"):
            getattr(sort_box, f"set_margin_{side}")(6)
        sort_title = Gtk.Label(label="Sort (favourites on top)")
        sort_title.add_css_class("panel-muted")
        sort_title.set_xalign(0.0)
        sort_box.append(sort_title)
        group = None
        for mode, text in zip(ADMIN_SORT_MODES, ADMIN_SORT_LABELS):
            chk = Gtk.CheckButton(label=text)
            if group is not None:
                chk.set_group(group)
            group = group or chk
            chk.set_active(mode == self._admin_sort)
            chk.connect("toggled", self._on_admin_sort_changed, mode)
            sort_box.append(chk)
        pop = Gtk.Popover()
        pop.set_child(sort_box)
        self._admin_sort_btn.set_popover(pop)
        self._admin_sort_btn.set_tooltip_text(self._admin_sort_tip())
        head_row.append(self._admin_sort_btn)
        clear_btn = Gtk.Button(label="Clear")
        clear_btn.add_css_class("flat")
        clear_btn.set_valign(Gtk.Align.CENTER)
        clear_btn.set_tooltip_text("Delete repeaters from the list (logged-in ones are kept)")
        clear_btn.connect("clicked", self._on_admin_clear_clicked)
        head_row.append(clear_btn)
        list_col.append(head_row)
        list_col.set_hexpand(False)  # the header's hexpand must not widen the list

        scroll = Gtk.ScrolledWindow.new()
        scroll.set_vexpand(True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        self._repeater_list = Gtk.ListBox.new()
        self._repeater_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._repeater_list.connect("row-selected", self._on_repeater_selected)
        # [admin-refresh patch] favourites, deletes and clears made on other screens
        # send no event; rebuild the list whenever this screen is shown.
        self.connect("map", self._on_admin_shown)
        scroll.set_child(self._repeater_list)
        list_col.append(scroll)
        self.append(list_col)

        # -- Right: panel with notebook ----------------------------------------
        panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        panel.add_css_class("panel-card")
        panel.set_hexpand(True)

        self._panel_stack = Gtk.Stack.new()
        self._panel_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self._panel_stack.set_transition_duration(150)

        # Empty state
        empty = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        empty.set_valign(Gtk.Align.CENTER)
        empty.set_halign(Gtk.Align.CENTER)
        empty_label = Gtk.Label(label="Select a repeater")
        empty_label.add_css_class("panel-muted")
        empty.append(empty_label)
        self._panel_stack.add_named(empty, "empty")

        # Admin panel with title + notebook
        admin_outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        self._admin_title = Gtk.Label(label="")
        self._admin_title.add_css_class("panel-title")
        self._admin_title.set_halign(Gtk.Align.START)
        self._admin_title.set_margin_start(12)
        self._admin_title.set_margin_top(10)
        self._admin_title.set_margin_bottom(4)
        # [admin-path patch] repeater name, then its network path on the same row
        title_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        title_row.append(self._admin_title)
        self._admin_path_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        self._admin_path_box.set_valign(Gtk.Align.CENTER)
        self._admin_path_box.set_margin_top(6)
        self._admin_path_box.set_margin_end(12)
        self._admin_path_box.set_hexpand(True)
        title_row.append(self._admin_path_box)
        self._trace_btn = Gtk.Button(label="Trace")  # [trace patch]
        self._trace_btn.set_tooltip_text(
            "Round-trip TRACE along this path: SNR for every link, both ways"
        )
        self._trace_btn.set_valign(Gtk.Align.CENTER)
        self._trace_btn.set_margin_top(6)
        self._trace_btn.set_margin_end(12)
        self._trace_btn.connect("clicked", self._on_trace_clicked)
        title_row.append(self._trace_btn)
        admin_outer.append(title_row)

        # Login bar (always visible at top of admin panel)
        self._login_box = self._build_login_bar()
        admin_outer.append(self._login_box)

        sep = Gtk.Separator.new(Gtk.Orientation.HORIZONTAL)
        sep.set_margin_start(12)
        sep.set_margin_end(12)
        admin_outer.append(sep)

        # Notebook with Status and Console tabs
        self._notebook = Gtk.Notebook.new()
        self._notebook.set_vexpand(True)
        self._notebook.set_margin_start(4)
        self._notebook.set_margin_end(4)
        self._notebook.set_margin_bottom(4)

        # Status tab
        self._status_scroll = Gtk.ScrolledWindow.new()
        self._status_scroll.set_vexpand(True)
        self._status_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._status_content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self._status_content.set_margin_start(12)
        self._status_content.set_margin_end(12)
        self._status_content.set_margin_top(8)
        self._status_content.set_margin_bottom(8)
        self._status_scroll.set_child(self._status_content)
        self._notebook.append_page(
            self._status_scroll, Gtk.Label(label="Status")
        )  # [admin-fold patch]

        # Graph tab [repeater-stats patch]
        from meshcore_console.ui_gtk.widgets.repeater_graph import RepeaterGraphPage

        self._graph_page = RepeaterGraphPage(self._service)
        self._notebook.append_page(self._graph_page, Gtk.Label(label="Graph"))

        # Console tab
        console_page = self._build_console_tab()
        self._notebook.append_page(console_page, Gtk.Label(label="Console"))

        admin_outer.append(self._notebook)
        self._panel_stack.add_named(admin_outer, "admin")

        self._panel_stack.set_visible_child_name("empty")
        panel.append(self._panel_stack)
        self.append(panel)

        # Wire events
        event_store.connect("events-available", self._on_events)
        GLib.idle_add(self._refresh_list)
        # [poll-1min patch] check every 10 s so a 1-minute interval stays ~1 min
        GLib.timeout_add_seconds(10, self._poll_due_repeaters)  # [repeater-stats patch]

    # -- Build login bar -------------------------------------------------------

    def _build_login_bar(self) -> Gtk.Box:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_start(12)
        box.set_margin_end(12)
        box.set_margin_bottom(8)

        # Status line
        self._status_label = Gtk.Label(label="Not logged in")
        self._status_label.add_css_class("login-status-offline")
        self._status_label.set_halign(Gtk.Align.START)
        box.append(self._status_label)

        # Password row
        pw_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._password_entry = Gtk.PasswordEntry.new()
        self._password_entry.set_property("placeholder-text", "Password")
        self._password_entry.set_show_peek_icon(True)
        self._password_entry.set_hexpand(True)
        self._password_entry.connect("activate", self._on_login_activate)
        pw_row.append(self._password_entry)

        self._login_btn = Gtk.Button.new_with_label("Login")
        self._login_btn.connect("clicked", self._on_login_clicked)
        pw_row.append(self._login_btn)

        self._guest_btn = Gtk.Button.new_with_label("Guest")
        self._guest_btn.connect("clicked", self._on_guest_clicked)
        pw_row.append(self._guest_btn)
        box.append(pw_row)

        # Save password / forget / logout row
        save_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._save_check = Gtk.CheckButton.new_with_label("Save password")
        save_row.append(self._save_check)
        self._autologin_check = Gtk.CheckButton.new_with_label("Auto-login")  # [auto-login patch]
        self._autologin_check.set_tooltip_text(
            "Log in automatically when the radio connects, and again if the session "
            "lapses. Uses the saved password, or guest if none is saved."
        )
        self._autologin_check.connect("toggled", self._on_autologin_toggled)
        save_row.append(self._autologin_check)

        self._forget_btn = Gtk.Button.new_with_label("Forget saved")
        self._forget_btn.connect("clicked", self._on_forget_clicked)
        self._forget_btn.set_visible(False)
        save_row.append(self._forget_btn)

        self._logout_btn = Gtk.Button.new_with_label("Logout")
        self._logout_btn.connect("clicked", self._on_logout_clicked)
        self._logout_btn.set_sensitive(False)
        save_row.append(self._logout_btn)
        box.append(save_row)

        return box

    # -- Build console tab -----------------------------------------------------

    def _build_console_tab(self) -> Gtk.Box:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        # Quick command buttons
        quick_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        quick_row.set_margin_start(8)
        quick_row.set_margin_end(8)
        quick_row.set_margin_top(8)
        quick_row.set_margin_bottom(4)
        # [stats-button patch] 'telemetry' is not a repeater CLI command; 'stats'
        # sends the RepeaterStats request instead and prints the result here.
        for cmd in (
            "neighbors",
            "stats",
            "owner",
            "advert",
            "get repeat",
            "reboot",
            "ver",
        ):  # [owner-info patch]
            btn = Gtk.Button.new_with_label(cmd)
            btn.add_css_class("quick-cmd-btn")
            if cmd == "advert":  # [advert-button patch] firmware CLI: flood advert
                btn.set_tooltip_text("Ask this repeater to send a flood advert now")
            if cmd == "owner":  # [owner-info patch]
                btn.set_tooltip_text(
                    "Owner info, name and firmware version (guest login is enough)"
                )
            if cmd == "stats":
                btn.set_tooltip_text("Request repeater stats (same as Poll now)")
                btn.connect("clicked", self._on_stats_quick)
            else:
                btn.connect("clicked", self._on_quick_cmd, cmd)
            quick_row.append(btn)
        spacer = Gtk.Box()  # [cli-help patch] push '?' to the far right
        spacer.set_hexpand(True)
        quick_row.append(spacer)
        help_btn = Gtk.Button.new_with_label("?")
        help_btn.add_css_class("quick-cmd-btn")
        help_btn.set_name("cli-help")
        help_btn.set_tooltip_text("Repeater command reference")
        help_btn.connect("clicked", self._on_cli_help)
        quick_row.append(help_btn)
        self._quick_row = quick_row
        box.append(quick_row)

        # Console output
        console_scroll = Gtk.ScrolledWindow.new()
        console_scroll.set_vexpand(True)
        console_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        console_scroll.set_margin_start(8)
        console_scroll.set_margin_end(8)

        self._console_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self._console_box.add_css_class("cli-console")
        console_scroll.set_child(self._console_box)
        self._console_scroll = console_scroll
        box.append(console_scroll)

        # Command entry
        entry_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        entry_row.set_margin_start(8)
        entry_row.set_margin_end(8)
        entry_row.set_margin_top(4)
        entry_row.set_margin_bottom(8)

        self._cmd_entry = Gtk.Entry.new()
        self._cmd_entry.add_css_class("cli-entry")
        self._cmd_entry.set_placeholder_text("Type a command...")
        self._cmd_entry.set_hexpand(True)
        self._cmd_entry.connect("activate", self._on_cmd_activate)

        key_ctrl = Gtk.EventControllerKey.new()
        key_ctrl.connect("key-pressed", self._on_cmd_key)
        self._cmd_entry.add_controller(key_ctrl)

        entry_row.append(self._cmd_entry)

        self._send_btn = Gtk.Button.new_with_label("Send")
        self._send_btn.connect("clicked", self._on_send_clicked)
        entry_row.append(self._send_btn)
        box.append(entry_row)

        self._set_cli_sensitive(False)
        return box

    # -- Build status tab content ----------------------------------------------

    def _populate_status(self, peer: Peer) -> None:
        clear_children(self._status_content)

        # Signal section
        self._add_section_header("Signal")

        if peer.rssi is not None:
            self._status_content.append(DetailRow("RSSI:", format_rssi(peer.rssi)))
        if peer.snr is not None:
            self._status_content.append(DetailRow("SNR:", format_snr(peer.snr)))
        if peer.signal_quality is not None:
            self._status_content.append(DetailRow("Quality:", f"{peer.signal_quality}%"))

        if peer.rssi is None and peer.snr is None and peer.signal_quality is None:
            no_signal = Gtk.Label(label="No signal data available")
            no_signal.add_css_class("panel-muted")
            no_signal.set_halign(Gtk.Align.START)
            self._status_content.append(no_signal)

        self._add_repeater_stats_section(peer)  # [repeater-stats patch]

        # Location section
        self._add_section_header("Location")

        if peer.latitude is not None and peer.longitude is not None:
            lat_dir = "N" if peer.latitude >= 0 else "S"
            lon_dir = "E" if peer.longitude >= 0 else "W"
            coords = f"{abs(peer.latitude):.5f}° {lat_dir}, {abs(peer.longitude):.5f}° {lon_dir}"
            self._status_content.append(DetailRow("Coordinates:", coords))

            if peer.location_updated:
                loc_time = to_local(peer.location_updated).strftime("%b %d at %H:%M")
                self._status_content.append(DetailRow("Updated:", loc_time))
        else:
            no_loc = Gtk.Label(label="No location reported")
            no_loc.add_css_class("panel-muted")
            no_loc.set_halign(Gtk.Align.START)
            self._status_content.append(no_loc)

        # Activity section
        self._add_section_header("Activity")

        if peer.last_advert_time:
            time_str = to_local(peer.last_advert_time).strftime("%b %d, %Y at %I:%M %p")
            self._status_content.append(DetailRow("Last Seen:", time_str))
        else:
            self._status_content.append(DetailRow("Last Seen:", "Unknown"))

        # Login state section
        state = self._service.get_repeater_login_state(peer.display_name)
        self._add_section_header("Login")

        if state is not None:
            role = "Admin" if state.is_admin else "Guest"
            self._status_content.append(DetailRow("Role:", role))
            if state.firmware_ver_level is not None:
                self._status_content.append(DetailRow("Firmware:", f"v{state.firmware_ver_level}"))
            if state.keep_alive_interval:
                self._status_content.append(
                    DetailRow("Keep-alive:", f"{state.keep_alive_interval}s")
                )
        else:
            not_logged = Gtk.Label(label="Not logged in")
            not_logged.add_css_class("panel-muted")
            not_logged.set_halign(Gtk.Align.START)
            self._status_content.append(not_logged)

        self._add_hardware_rows(peer)  # [board-history patch]

        # Public key section
        if peer.public_key:
            self._add_section_header("Public Key")
            chunks = [peer.public_key[i : i + 4] for i in range(0, len(peer.public_key), 4)]
            key_label = Gtk.Label(label=" ".join(chunks))
            key_label.add_css_class("analyzer-raw")
            key_label.set_halign(Gtk.Align.START)
            key_label.set_wrap(True)
            key_label.set_wrap_mode(Pango.WrapMode.CHAR)
            key_label.set_selectable(True)
            self._status_content.append(key_label)

    # --- [repeater-stats patch] -----------------------------------------------
    def _add_repeater_stats_section(self, peer: Peer) -> None:
        store = self._service.get_repeater_samples()
        key = peer.public_key or peer.peer_id

        self._add_section_header("Heard by this radio (24 h)")
        heard = store.summary(key, "heard", time.time() - 86400) if key else {"n": 0}
        if heard["n"]:
            self._status_content.append(DetailRow("Packets heard:", str(heard["n"])))
            self._status_content.append(DetailRow("Avg RSSI:", f"{heard['rssi']:.1f} dBm"))
            if heard["snr"] is not None:
                self._status_content.append(DetailRow("Avg SNR:", f"{heard['snr']:.1f} dB"))
            last = to_local_ts(heard["last"])
            self._status_content.append(DetailRow("Last heard:", last))
        else:
            self._status_content.append(self._muted("Nothing heard directly from it in 24 h"))

        self._add_section_header("Repeater Stats")
        latest = store.latest(key, "stats") if key else None
        logged_in = self._service.get_repeater_login_state(peer.display_name) is not None
        result = self._poll_result.get(peer.display_name)
        if latest:
            up = latest.get("total_up_time_secs") or 0
            self._status_content.append(DetailRow("Polled:", to_local_ts(latest["ts"])))
            if latest.get("batt_mv"):
                self._status_content.append(
                    DetailRow("Battery:", f"{latest['batt_mv'] / 1000:.2f} V")
                )
            if latest.get("noise_floor") is not None:
                self._status_content.append(
                    DetailRow("Noise floor:", f"{latest['noise_floor']:.0f} dBm")
                )
            if latest.get("rssi") is not None:
                snr = latest.get("snr")
                snr_txt = f", SNR {snr:.2f} dB" if snr is not None else ""
                self._status_content.append(
                    DetailRow("Its last RX:", f"{latest['rssi']:.0f} dBm{snr_txt}")
                )
            if up:
                d, rem = divmod(int(up), 86400)
                self._status_content.append(
                    DetailRow("Uptime:", f"{d}d {rem // 3600}h {rem % 3600 // 60}m")
                )
                tx = latest.get("total_air_time_secs") or 0
                rx = latest.get("total_rx_air_time_secs") or 0
                self._status_content.append(
                    DetailRow("Airtime:", f"TX {100 * tx / up:.1f}%  RX {100 * rx / up:.1f}%")
                )
            if "n_packets_recv" in latest:
                self._status_content.append(
                    DetailRow(
                        "Packets:",
                        f"RX {latest['n_packets_recv']:,}  TX {latest.get('n_packets_sent', 0):,}",
                    )
                )
                self._status_content.append(
                    DetailRow(
                        "Flood / direct:",
                        f"RX {latest.get('n_recv_flood', 0):,} / {latest.get('n_recv_direct', 0):,}  "
                        f"TX {latest.get('n_sent_flood', 0):,} / {latest.get('n_sent_direct', 0):,}",
                    )
                )
                self._status_content.append(
                    DetailRow(
                        "Dups / errors:",
                        f"{latest.get('n_flood_dups', 0) + latest.get('n_direct_dups', 0):,} / "
                        f"{latest.get('n_recv_errors', 0):,}",
                    )
                )
                self._status_content.append(
                    DetailRow("TX queue:", str(latest.get("curr_tx_queue_len", 0)))
                )
        else:
            self._status_content.append(self._muted("No stats polled yet"))
        if result:
            self._status_content.append(self._muted(result))

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row.set_margin_top(4)
        btn = Gtk.Button.new_with_label("Poll now")
        btn.set_sensitive(logged_in and not self._busy)
        btn.connect("clicked", lambda _b: self._poll_repeater(peer.display_name))
        row.append(btn)
        row.append(self._muted("Auto-poll"))  # [poll-interval patch]
        row.append(self._poll_interval_dropdown(peer))
        row.append(
            self._muted("while logged in" if logged_in else "(log in to poll; guest is enough)")
        )
        self._status_content.append(row)
        trow = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)  # [trace-log patch]
        trow.append(self._muted("Auto-trace"))
        trow.append(self._trace_interval_dropdown(peer))
        trow.append(self._muted("round trip, no login needed; see Graph \u203a Trace"))
        tally = getattr(self, "_trace_retry_tally", {}).get(
            self._repeater_key(peer.display_name)
        )  # [trace-retry patch]
        if tally and tally[0]:
            trow.append(self._muted(f"\u00b7 retry recovered {tally[1]} of {tally[0]} failures"))
        for box in (row, trow):  # one line each: short labels, plenty of width
            child = box.get_first_child()
            while child is not None:
                if isinstance(child, Gtk.Label):
                    child.set_wrap(False)
                child = child.get_next_sibling()
        self._status_content.append(trow)

    @staticmethod
    def _muted(text: str) -> Gtk.Label:
        label = Gtk.Label(label=text)
        label.add_css_class("panel-muted")
        label.set_halign(Gtk.Align.START)
        label.set_wrap(True)
        return label

    def _poll_due_repeaters(self) -> bool:
        """Every 60 s: poll one logged-in repeater whose last poll is >= 15 min old."""

        if self._busy:
            return True
        if getattr(
            self, "_cmd_queue", None
        ):  # [login-queue patch] your clicks go before background work
            return True
        now = time.time()
        if self._auto_login_due(now):  # [auto-login patch] logins before polls
            return True
        if self._auto_trace_due(now):  # [trace-log patch]
            return True
        for name in self._service.list_logged_in_repeaters():
            interval = self._poll_interval_for(name)  # [poll-interval patch]
            if interval and now - self._last_poll.get(name, 0.0) >= interval:
                self._poll_repeater(name)
                break
        return True

    def _poll_repeater(self, peer_name: str) -> None:
        # Shares the console's busy flag: a status request and a CLI command to
        # the same repeater would otherwise race for the same response callback.
        if self._busy:
            return
        self._busy = True
        self._last_poll[peer_name] = time.time()
        self._send_btn.set_sensitive(False)
        self._refresh_selected_status(peer_name)

        def work() -> None:
            try:
                result = self._service.request_repeater_stats(peer_name)
                if result.get("success"):
                    self._poll_power(peer_name)  # [power-source patch]
                    counter = getattr(self._service, "request_repeater_neighbour_counts", None)
                    if counter is not None:  # [neighbour-graph patch]
                        try:
                            nb = counter(peer_name)
                            logger.debug("neighbour counts %s: %s", peer_name, nb)
                        except Exception as exc:  # noqa: BLE001
                            logger.debug("neighbour counts %s failed: %s", peer_name, exc)
            except Exception as exc:  # noqa: BLE001
                result = {"success": False, "reason": str(exc) or type(exc).__name__}

            def done() -> bool:
                self._busy = False
                sel = self._selected_repeater
                if sel is not None:
                    self._update_login_ui(self._service.get_repeater_login_state(sel.display_name))
                stamp = time.strftime("%H:%M")
                if result.get("success"):
                    self._poll_result[peer_name] = (
                        f"Last poll {stamp}: OK ({result.get('rtt_ms', 0)} ms)"
                    )
                else:
                    self._poll_result[peer_name] = (
                        f"Last poll {stamp} failed: {result.get('reason', 'no response')}"
                    )
                    if self._autologin_enabled(peer_name):  # [auto-login patch]
                        self._relogin_needed.add(peer_name)  # session may have lapsed
                logger.info("repeater stats poll %s: %s", peer_name, self._poll_result[peer_name])
                if peer_name in self._stats_to_console:  # [stats-button patch]
                    self._stats_to_console.discard(peer_name)
                    self._print_stats_to_console(peer_name, result)
                self._refresh_selected_status(peer_name)
                return False

            GLib.idle_add(done)

        threading.Thread(target=work, daemon=True).start()

    # --- [stats-button patch] -------------------------------------------------
    def _on_stats_quick(self, _btn: Gtk.Button | None) -> None:
        sel = self._selected_repeater
        if sel is not None and self._busy:  # [cmd-queue patch]
            self._queue_command(sel.display_name, "stats", lambda: self._on_stats_quick(None))
            return
        if sel is None or self._busy:
            return
        self._append_console_command("stats")
        self._stats_to_console.add(sel.display_name)
        self._poll_repeater(sel.display_name)

    def _print_stats_to_console(self, peer_name: str, result: dict) -> None:
        with self._console_target(peer_name):  # [console-history patch] even if you moved on
            if not result.get("success"):
                self._append_console_error(result.get("reason", "No response"))
                return
            self._append_console_response("\n".join(self._format_stats_lines(result)))

    @staticmethod
    def _format_stats_lines(result: dict) -> list[str]:
        st = result.get("stats") or {}
        g = lambda k: st.get(k) or 0  # noqa: E731
        up = g("total_up_time_secs")
        d, rem = divmod(int(up), 86400)
        lines = [
            f"Battery {g('batt_milli_volts') / 1000:.2f} V   Noise floor {g('noise_floor')} dBm   "
            f"Last RX {g('last_rssi')} dBm / {st.get('last_snr', 0):.2f} dB",
            f"Uptime {d}d {rem // 3600}h {rem % 3600 // 60}m   TX queue {g('curr_tx_queue_len')}",
        ]
        if up:
            lines.append(
                f"Airtime TX {100 * g('total_air_time_secs') / up:.1f}%   "
                f"RX {100 * g('total_rx_air_time_secs') / up:.1f}%"
            )
        lines += [
            f"Packets RX {g('n_packets_recv'):,}  TX {g('n_packets_sent'):,}",
            f"Flood   RX {g('n_recv_flood'):,}  TX {g('n_sent_flood'):,}",
            f"Direct  RX {g('n_recv_direct'):,}  TX {g('n_sent_direct'):,}",
            f"Dups flood {g('n_flood_dups'):,}  direct {g('n_direct_dups'):,}   "
            f"RX errors {g('n_recv_errors'):,}   Err events {g('err_events')}",
        ]
        if result.get("rtt_ms") is not None:
            lines.append(f"(reply in {result['rtt_ms']} ms)")
        return lines

    # --- [poll-interval patch] ------------------------------------------------
    _POLL_CHOICES = (
        ("Off", 0),
        ("1 min", 60),  # [poll-1min patch] antenna alignment
        ("5 min", 300),
        ("15 min", 900),
        ("30 min", 1800),
        ("1 h", 3600),
        ("6 h", 21600),
    )

    @staticmethod
    def _poll_file():
        from meshcore_console.meshcore.paths import config_dir

        return config_dir() / "repeater_poll.json"

    def _poll_intervals(self) -> dict[str, int]:
        # Saved interval (s) per repeater key; 0 = off. Loaded once.
        cache = getattr(self, "_poll_interval_cache", None)
        if cache is None:
            import json

            try:
                data = json.loads(self._poll_file().read_text())
            except (OSError, ValueError):
                data = {}
            cache = {
                str(k): int(v)
                for k, v in (data.items() if isinstance(data, dict) else ())
                if isinstance(v, (int, float)) and v >= 0
            }
            self._poll_interval_cache = cache
        return cache

    def _repeater_key(self, name: str) -> str:
        peer = next((p for p in self._service.list_peers() if p.display_name == name), None)
        return (peer.public_key or peer.peer_id) if peer is not None else name

    def _poll_interval_for(self, name: str) -> int:
        from meshcore_console.meshcore.repeater_stats import POLL_INTERVAL_S

        return self._poll_intervals().get(self._repeater_key(name), POLL_INTERVAL_S)

    def _poll_interval_dropdown(self, peer: Peer) -> Gtk.DropDown:
        secs = [s for _l, s in self._POLL_CHOICES]
        dropdown = Gtk.DropDown.new_from_strings([lab for lab, _s in self._POLL_CHOICES])
        current = self._poll_interval_for(peer.display_name)
        dropdown.set_selected(secs.index(current) if current in secs else secs.index(900))
        dropdown.set_valign(Gtk.Align.CENTER)
        dropdown.set_tooltip_text("How often to request stats from this repeater")
        dropdown.connect("notify::selected", self._on_poll_interval_changed, peer.display_name)
        return dropdown

    def _on_poll_interval_changed(self, dropdown: Gtk.DropDown, _pspec, name: str) -> None:
        import json

        seconds = self._POLL_CHOICES[dropdown.get_selected()][1]
        self._poll_intervals()[self._repeater_key(name)] = seconds
        logger.info("auto-poll for %s set to %ss", name, seconds)
        try:
            path = self._poll_file()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(self._poll_intervals(), indent=1))
        except OSError as e:
            logger.warning("Could not save auto-poll interval: %s", e)

    # --- [auto-login patch] ---------------------------------------------------
    @staticmethod
    def _autologin_file():
        from meshcore_console.meshcore.paths import config_dir

        return config_dir() / "repeater_autologin.json"

    def _autologin_keys(self) -> set[str]:
        keys = getattr(self, "_autologin_cache", None)
        if keys is None:
            import json

            try:
                data = json.loads(self._autologin_file().read_text())
            except (OSError, ValueError):
                data = []
            keys = {str(k) for k in data} if isinstance(data, list) else set()
            self._autologin_cache = keys
        return keys

    def _autologin_enabled(self, name: str) -> bool:
        return self._repeater_key(name) in self._autologin_keys()

    def _sync_autologin_check(self, peer: Peer) -> None:
        self._autologin_syncing = True
        try:
            self._autologin_check.set_active(self._autologin_enabled(peer.display_name))
        finally:
            self._autologin_syncing = False

    def _on_autologin_toggled(self, check: Gtk.CheckButton) -> None:
        if getattr(self, "_autologin_syncing", False) or self._selected_repeater is None:
            return
        import json

        name = self._selected_repeater.display_name
        keys = self._autologin_keys()
        if check.get_active():
            keys.add(self._repeater_key(name))
            self._autologin_suppressed.discard(name)
            self._autologin_next.pop(name, None)
            self._autologin_attempts.pop(name, None)
        else:
            keys.discard(self._repeater_key(name))
        logger.info("auto-login for %s: %s", name, check.get_active())
        try:
            path = self._autologin_file()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(sorted(keys), indent=1))
        except OSError as e:
            logger.warning("Could not save auto-login setting: %s", e)

    def _auto_login_due(self, now: float) -> bool:
        """Start one auto-login if any is due. Returns True if one was started."""
        try:
            if not self._service.get_status().connected:
                return False
        except Exception:  # noqa: BLE001
            return False
        logged_in = set(self._service.list_logged_in_repeaters())
        for peer in self._service.list_peers():
            name = peer.display_name
            if not peer.is_repeater or not self._autologin_enabled(name):
                continue
            if name in self._autologin_suppressed:
                continue
            if name in logged_in and name not in self._relogin_needed:
                continue
            if now < self._autologin_next.get(name, 0.0):
                continue
            self._auto_login(name, relogin=name in logged_in)
            return True
        return False

    def _auto_login(self, name: str, *, relogin: bool = False) -> None:
        self._busy = True
        password = self._service.get_saved_repeater_password(name) or ""
        attempt = self._autologin_attempts.get(name, 0)
        what = "re-login" if relogin else "auto-login"

        def work() -> None:
            try:
                result = self._service.login_to_repeater(name, password, save_password=False)
            except Exception as exc:  # noqa: BLE001
                result = {"success": False, "reason": str(exc) or type(exc).__name__}

            def done() -> bool:
                self._busy = False
                ok = bool(result.get("success"))
                if ok:
                    self._autologin_attempts.pop(name, None)
                    self._relogin_needed.discard(name)
                    # At most one re-login per 10 min per repeater, so a repeater
                    # that accepts logins but not polls cannot cause a loop.
                    self._autologin_next[name] = time.time() + 600
                    if not relogin:
                        self._last_poll.pop(name, None)  # first poll right away
                    msg = f"{what.capitalize()} to {name}: OK" + ("" if password else " (guest)")
                else:
                    n = attempt + 1
                    self._autologin_attempts[name] = n
                    delay = min(20 * 3 ** (n - 1), 1800)  # 20 s, 1, 3, 9, 27, 30 min
                    self._autologin_next[name] = time.time() + delay
                    when = f"{delay} s" if delay < 60 else f"{delay // 60} min"
                    msg = f"{what.capitalize()} to {name} failed ({result.get('reason', 'no response')}); retry in {when}"
                logger.info(msg)
                sel = self._selected_repeater
                self._append_for(
                    name, "response" if ok else "error", msg
                )  # [console-history patch]
                if sel is not None and sel.display_name == name:
                    self._update_login_ui(self._service.get_repeater_login_state(name))
                    self._populate_status(sel)
                self._refresh_list()
                return False

            GLib.idle_add(done)

        threading.Thread(target=work, daemon=True).start()

    # --- [neighbors patch] ----------------------------------------------------
    @staticmethod
    def _parse_neighbors(text: str) -> list[tuple[str, int, float]] | None:
        # Firmware format, one per line, newest first: AABBCCDD:secs_ago:snr_x4
        rows = []
        for line in text.strip().splitlines():
            parts = line.strip().split(":")
            if len(parts) != 3:
                return None
            hex_id, secs, snr = parts
            try:
                bytes.fromhex(hex_id)
                rows.append((hex_id.upper(), int(secs), int(snr) / 4.0))
            except ValueError:
                return None
        return rows

    @staticmethod
    def _ago(secs: int) -> str:
        if secs < 60:
            return f"{secs} s"
        if secs < 3600:
            return f"{secs // 60} min"
        if secs < 86400:
            return f"{secs // 3600} h {secs % 3600 // 60} min"
        return f"{secs // 86400} d {secs % 86400 // 3600} h"

    def _append_neighbors_table(self, command: str, text: str) -> bool:
        """Render a 'neighbors' reply as a table. False = not a neighbors reply."""
        if command.strip().lower() != "neighbors":
            return False
        if text.strip() == "-none-":
            self._append_console_response("No neighbours heard yet.")
            return True
        rows = self._parse_neighbors(text)
        if not rows:
            return False
        peers = [p for p in self._service.list_peers() if p.public_key]

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_margin_top(2)
        box.set_margin_bottom(4)
        caption = Gtk.Label(
            label=f"{len(rows)} repeater{'s' if len(rows) != 1 else ''} this one hears directly "
            "(from their adverts), newest first. Click a badge to open it in Peers."
        )
        caption.add_css_class("panel-muted")
        caption.set_halign(Gtk.Align.START)
        caption.set_wrap(True)
        caption.set_xalign(0.0)
        box.append(caption)

        grid = Gtk.Grid(column_spacing=14, row_spacing=3)
        headers = (
            ("Node", "Short address; click to open in Peers"),
            ("Name", "Name from your peer list"),
            ("Last heard", "Time since this repeater last heard its advert"),
            ("SNR", "Signal-to-noise of that advert, as heard by this repeater"),
            ("ID", "First 4 bytes of its public key"),
        )
        for col, (title, tip) in enumerate(headers):
            h = Gtk.Label(label=title)
            h.add_css_class("message-detail-header")
            h.set_halign(Gtk.Align.START)
            h.set_tooltip_text(tip)
            h.set_selectable(True)  # [neighbors-copy patch]
            grid.attach(h, col, 0, 1, 1)

        for r, (hex_id, secs, snr) in enumerate(rows, start=1):
            peer = next((p for p in peers if p.public_key.upper().startswith(hex_id)), None)
            grid.attach(self._neighbor_badge(hex_id[:2], peer), 0, r, 1, 1)
            cells = (
                peer.display_name if peer else "(not in your peers)",
                self._ago(secs),
                f"{snr:+.2f} dB",
                hex_id,
            )
            for col, value in enumerate(cells, start=1):
                lab = Gtk.Label(label=value)
                lab.add_css_class("cli-response")
                if peer is None and col == 1:
                    lab.add_css_class("panel-muted")
                lab.set_halign(Gtk.Align.END if col == 3 else Gtk.Align.START)
                lab.set_selectable(True)  # [neighbors-copy patch] every cell, not just the ID
                grid.attach(lab, col, r, 1, 1)
        box.append(grid)
        lines = ["Node\tName\tLast heard\tSNR dB\tID"]  # [neighbors-copy patch] whole table as text
        for hex_id, secs, snr in rows:
            peer = next((p for p in peers if p.public_key.upper().startswith(hex_id)), None)
            name = peer.display_name if peer else "(not in your peers)"
            lines.append(f"{hex_id[:2]}\t{name}\t{self._ago(secs)}\t{snr:+.2f}\t{hex_id}")
        from meshcore_console.ui_gtk.widgets.trace_view import _copy_text

        copy_btn = Gtk.Button(label="Copy table")
        copy_btn.set_halign(Gtk.Align.START)
        copy_btn.set_tooltip_text("Copy all rows as text (tab-separated), to paste anywhere")
        copy_btn.connect("clicked", lambda b: _copy_text(b, "\n".join(lines)))
        box.append(copy_btn)
        self._console_box.append(box)
        self._scroll_console()
        return True

    def _neighbor_badge(self, prefix: str, peer) -> Gtk.Widget:
        badge = Gtk.Box()
        badge.add_css_class("node-badge")
        badge.set_valign(Gtk.Align.CENTER)
        label = Gtk.Label(label=prefix)
        label.add_css_class("node-prefix")
        label.add_css_class("node-prefix-repeater")
        badge.append(label)
        if peer is None:
            badge.set_tooltip_text("Not in your peer list yet")
            badge.set_opacity(0.6)
            return badge
        badge.set_tooltip_text(f"{peer.display_name}: open in Peers")
        badge.set_cursor_from_name("pointer")
        click = Gtk.GestureClick.new()
        click.set_button(1)

        def _open(gesture, _n, _x, _y, peer_id=peer.peer_id):
            from meshcore_console.ui_gtk.helpers import navigate

            gesture.set_state(Gtk.EventSequenceState.CLAIMED)
            navigate(self, "peers", ("select_peer", peer_id))

        click.connect("released", _open)
        badge.add_controller(click)
        return badge

    # --- [admin-path patch] ---------------------------------------------------
    def _update_admin_path(self, peer: Peer) -> None:
        # Same badges-and-arrows as Peers > Network Path: you -> hops -> repeater.
        from meshcore_console.ui_gtk.widgets import PathVisualization
        from meshcore_console.ui_gtk.widgets.node_badge import STYLE_DEFAULT, STYLE_SELF

        clear_children(self._admin_path_box)
        current = next((p for p in self._service.list_peers() if p.peer_id == peer.peer_id), peer)
        self_key = self._service.get_self_public_key()
        self_prefix = (self_key or "")[:2].upper() or "Me"
        self_name = self._service.get_status().node_id
        peer_prefix = (current.public_key or current.display_name)[:2].upper()
        path = PathVisualization(
            hops=list(current.last_path or []),
            peers=self._service.list_peers(),
            start=(self_prefix, self_name, None, STYLE_SELF),
            end=(peer_prefix, current.display_name, current, STYLE_DEFAULT),
        )
        hops = len(current.last_path or [])
        # One row: the FlowBox otherwise wraps to one badge per line here.
        path.set_min_children_per_line(min(hops + 2, 6))
        path.set_tooltip_text(
            "Direct (heard with no hops)" if hops == 0 else f"{hops} hop{'s' if hops != 1 else ''}"
        )
        self._admin_path_box.append(path)

    # --- [trace patch] --------------------------------------------------------
    # --- [trace-log patch] auto-trace -----------------------------------------
    _TRACE_CHOICES = (
        ("Off", 0),
        ("1 min", 60),
        ("5 min", 300),
        ("15 min", 900),
        ("30 min", 1800),
        ("1 h", 3600),
        ("6 h", 21600),
    )

    @staticmethod
    def _trace_file():
        from meshcore_console.meshcore.paths import config_dir

        return config_dir() / "repeater_trace.json"

    def _trace_intervals(self) -> dict[str, int]:
        cache = getattr(self, "_trace_interval_cache", None)
        if cache is None:
            import json

            cache = {}
            try:
                data = json.loads(self._trace_file().read_text())
                if isinstance(data, dict):
                    cache = {str(k): int(v) for k, v in data.items()}
            except (OSError, ValueError, TypeError):
                pass
            self._trace_interval_cache = cache
        return cache

    def _trace_interval_dropdown(self, peer: Peer) -> Gtk.DropDown:
        secs = [s for _l, s in self._TRACE_CHOICES]
        dropdown = Gtk.DropDown.new_from_strings([lab for lab, _s in self._TRACE_CHOICES])
        current = self._trace_intervals().get(self._repeater_key(peer.display_name), 0)
        dropdown.set_selected(secs.index(current) if current in secs else 0)
        dropdown.set_valign(Gtk.Align.CENTER)
        dropdown.set_tooltip_text(
            "How often to send a round-trip TRACE to this repeater and log the SNR of every link"
        )
        dropdown.connect("notify::selected", self._on_trace_interval_changed, peer.display_name)
        return dropdown

    def _on_trace_interval_changed(self, dropdown: Gtk.DropDown, _pspec, name: str) -> None:
        import json

        seconds = self._TRACE_CHOICES[dropdown.get_selected()][1]
        key = self._repeater_key(name)
        if seconds:
            self._trace_intervals()[key] = seconds
        else:
            self._trace_intervals().pop(key, None)
        logger.info("auto-trace for %s set to %ss", name, seconds)
        try:
            path = self._trace_file()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(self._trace_intervals(), indent=1))
        except OSError as e:
            logger.warning("Could not save auto-trace interval: %s", e)

    def _auto_trace_due(self, now: float) -> bool:
        """Start at most one due auto-trace per tick. True if one was started."""
        intervals = self._trace_intervals()
        if not intervals or getattr(self, "_trace_running", False):
            return False
        last = getattr(self, "_last_trace", None)
        if last is None:
            last = self._last_trace = {}
        for peer in self._service.list_peers():
            if not peer.is_repeater:
                continue
            key = peer.public_key or peer.peer_id
            interval = intervals.get(key, 0)
            if interval and now - last.get(key, 0.0) >= interval:
                last[key] = now
                self._auto_trace(peer.display_name)
                return True
        return False

    # --- [trace-retry patch] ----------------------------------------------------
    # A failed auto-trace (one that went on air and timed out) is retried once,
    # 10 s later. Both attempts are logged, so Graph > Trace shows every failure;
    # the Status tab shows how many failures the retry recovered.
    TRACE_RETRY_S = 10

    def _after_auto_trace(self, name: str, result: dict) -> None:
        inflight = getattr(self, "_trace_retry_inflight", None)
        if inflight is None:
            inflight = self._trace_retry_inflight = set()
            self._trace_retry_tally = {}  # repeater key -> [failures retried, recovered]
        key = self._repeater_key(name)
        if name in inflight:  # this was the retry
            inflight.discard(name)
            tally = self._trace_retry_tally.setdefault(key, [0, 0])
            if result.get("success"):
                tally[1] += 1
            logger.info(
                "auto-trace retry %s: %s",
                name,
                "ok" if result.get("success") else result.get("reason"),
            )
            sel = self._selected_repeater
            if sel is not None and sel.display_name == name:
                self._populate_status(sel)
            return
        went_on_air = "route" in result and not result.get("busy")
        if result.get("success") or not went_on_air:
            return
        self._trace_retry_tally.setdefault(key, [0, 0])[0] += 1
        inflight.add(name)
        logger.info("auto-trace %s failed; retrying in %ss", name, self.TRACE_RETRY_S)

        def retry(tries_left: int = 6) -> bool:
            if name not in inflight:
                return False
            if getattr(self, "_trace_running", False):  # another trace in flight: try again shortly
                if tries_left > 0:
                    GLib.timeout_add_seconds(5, lambda: retry(tries_left - 1))
                else:
                    inflight.discard(name)
                return False
            self._auto_trace(name)
            return False

        GLib.timeout_add_seconds(self.TRACE_RETRY_S, retry)

    def _auto_trace(self, name: str) -> None:
        self._trace_running = True
        btn = getattr(self, "_trace_btn", None)
        if btn is not None:
            btn.set_sensitive(False)
            btn.set_label("Tracing\u2026")

        def work() -> None:
            try:
                from meshcore_console.meshcore.trace import trace_source  # [trace-source patch]

                with trace_source("auto"):
                    result = self._service.trace_repeater(name)
            except Exception as exc:  # noqa: BLE001
                result = {"success": False, "reason": str(exc) or type(exc).__name__}

            def done() -> bool:
                self._trace_running = False
                if btn is not None:
                    btn.set_sensitive(True)
                    btn.set_label("Trace")
                logger.info(
                    "auto-trace %s: %s",
                    name,
                    "ok" if result.get("success") else result.get("reason"),
                )
                self._after_auto_trace(name, result)  # [trace-retry patch]
                sel = self._selected_repeater
                if sel is not None and sel.display_name == name:
                    self._graph_page.refresh()
                return False

            GLib.idle_add(done)

        threading.Thread(target=work, daemon=True).start()

    def _on_trace_clicked(self, btn: Gtk.Button) -> None:
        peer = self._selected_repeater
        if peer is None or getattr(self, "_trace_running", False):
            return
        self._trace_running = True
        btn.set_sensitive(False)
        btn.set_label("Tracing…")
        self._notebook.set_current_page(self._notebook.get_n_pages() - 1)  # Console
        self._append_console_command(f"trace {peer.display_name}")
        name = peer.display_name

        def work() -> None:
            try:
                from meshcore_console.meshcore.trace import trace_source  # [trace-source patch]

                with trace_source("admin"):
                    result = self._service.trace_repeater(name)
            except Exception as exc:  # noqa: BLE001
                result = {"success": False, "reason": str(exc) or type(exc).__name__}

            def done() -> bool:
                self._trace_running = False
                btn.set_sensitive(True)
                btn.set_label("Trace")
                self._show_trace_result(name, result)
                return False

            GLib.idle_add(done)

        threading.Thread(target=work, daemon=True).start()

    def _show_trace_result(self, name: str, result: dict) -> None:
        from meshcore_console.ui_gtk.widgets.trace_view import build_trace_view

        self_key = self._service.get_self_public_key() or ""
        self_label = f"{self._service.get_status().node_id} ({self_key[:2].upper() or 'me'})"
        view = build_trace_view(
            result,
            self._service.list_peers(),
            target=name,
            self_label=self_label,
            spreading_factor=self._service.get_settings().spreading_factor,
            css_class="cli-response",
        )
        logger.info("trace %s: %s", name, "ok" if result.get("success") else result.get("reason"))
        self._graph_page.refresh()  # [trace-log patch]
        cb = getattr(
            self, "_on_trace_done", None
        )  # [repeater-tabs patch] Status shows the last trace
        if cb is not None:
            cb()
        with self._console_target(name):  # [console-history patch]
            self._console_box.append(view)
            self._scroll_console()

    def _refresh_selected_status(self, peer_name: str) -> None:
        sel = self._selected_repeater
        if sel is not None and sel.display_name == peer_name:
            self._populate_status(sel)
            self._graph_page.refresh()

    def _add_section_header(self, title: str) -> None:
        header = Gtk.Label(label=title)
        header.add_css_class("message-detail-header")
        header.set_halign(Gtk.Align.START)
        header.set_margin_top(12)
        self._status_content.append(header)

    # -- List management -------------------------------------------------------

    def _refresh_list(self) -> bool:
        peers = self._service.list_peers()
        logged_in = set(self._service.list_logged_in_repeaters())
        repeaters = self._sort_admin_repeaters(
            [p for p in peers if p.is_repeater], logged_in
        )  # [admin-sort patch]

        # Suppress selection handler during rebuild
        self._refreshing_list = True

        child = self._repeater_list.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self._repeater_list.remove(child)
            child = nxt

        for rp in repeaters:
            row = self._make_repeater_row(rp, rp.display_name in logged_in)
            self._repeater_list.append(row)

        # Re-select if still valid, and update the peer object
        if self._selected_repeater:
            for i, rp in enumerate(repeaters):
                if rp.display_name == self._selected_repeater.display_name:
                    self._selected_repeater = rp
                    row_widget = self._repeater_list.get_row_at_index(i)
                    if row_widget:
                        self._repeater_list.select_row(row_widget)
                    break

        self._refreshing_list = False
        return False

    def _make_repeater_row(self, peer: Peer, logged_in: bool) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow.new()
        row._peer = peer  # type: ignore[attr-defined]
        hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        hbox.add_css_class("admin-repeater-row")

        dot = Gtk.Label(label="●")
        dot.add_css_class("admin-login-dot" if logged_in else "admin-login-dot-off")
        hbox.append(dot)

        name = Gtk.Label(label=peer.display_name)
        name.set_halign(Gtk.Align.START)
        name.set_hexpand(True)
        name.set_ellipsize(Pango.EllipsizeMode.END)
        name.set_max_width_chars(20)
        hbox.append(name)

        if peer.is_favorite:
            star = Gtk.Image.new_from_icon_name("starred-symbolic")
            star.add_css_class("panel-muted")
            hbox.append(star)

        if peer.rssi is not None:
            rssi = Gtk.Label(label=f"{peer.rssi} dBm")
            rssi.add_css_class("panel-muted")
            hbox.append(rssi)

        row.set_child(hbox)
        row.set_tooltip_text(self._repeater_tooltip(peer, logged_in))  # [admin-tip patch]
        return row

    def _repeater_tooltip(self, peer: Peer, logged_in: bool) -> str:  # [admin-tip patch]
        from datetime import datetime, timezone

        lines = [peer.display_name]
        key = (peer.public_key or "").upper()
        if key:
            lines.append(f"Hash: {key[:2]} \u00b7 {key[:4]} \u00b7 {key[:6]}  (1/2/3-byte)")
        if peer.last_advert_time is not None:
            seen = peer.last_advert_time
            if seen.tzinfo is None:
                seen = seen.replace(tzinfo=timezone.utc)
            secs = max(0, int((datetime.now(timezone.utc) - seen).total_seconds()))
            if secs < 60:
                ago = f"{secs} s ago"
            elif secs < 3600:
                ago = f"{secs // 60} min ago"
            elif secs < 86400:
                ago = f"{secs // 3600} h {secs % 3600 // 60} min ago"
            else:
                ago = f"{secs // 86400} d ago"
            lines.append(f"Last seen: {to_local(seen).strftime('%b %-d %H:%M')} ({ago})")
        else:
            lines.append("Last seen: never (no advert heard)")
        path = peer.last_path
        if peer.snr is not None or peer.rssi is not None:
            sig = []
            if peer.snr is not None:
                sig.append(f"SNR {peer.snr:+.1f} dB")
            if peer.rssi is not None:
                sig.append(f"RSSI {peer.rssi} dBm")
            how = "heard directly" if path == [] else "of the last relay hop"
            lines.append(f"{'  '.join(sig)}  ({how})")
        else:
            lines.append("SNR: n/a")
        if path:
            lines.append(
                f"Route: {len(path)} hop{'s' if len(path) != 1 else ''} ({' \u2192 '.join(str(h).upper() for h in path)})"
            )
        elif path == [] and peer.last_advert_time is not None:
            lines.append("Route: direct")
        try:
            dist = self._admin_distances([peer]).get(peer.peer_id)
        except Exception:  # noqa: BLE001
            dist = None
        if dist is not None:
            try:
                from meshcore_console.ui_gtk.views.peers import DISTANCE_IN_MILES
            except ImportError:
                DISTANCE_IN_MILES = False
            lines.append(
                f"Distance: {dist * 0.621371:.1f} mi"
                if DISTANCE_IN_MILES
                else f"Distance: {dist:.1f} km"
            )
        elif (
            peer.latitude is None
            or peer.longitude is None
            or (peer.latitude == 0 and peer.longitude == 0)
        ):
            lines.append("Distance: no location advertised")
        if logged_in:
            lines.append("Logged in")
        if peer.is_favorite:
            lines.append("\u2605 Favourite")
        return "\n".join(lines)

    # -- Selection -------------------------------------------------------------

    def _on_repeater_selected(self, _listbox: Gtk.ListBox, row: Gtk.ListBoxRow | None) -> None:
        if self._refreshing_list:
            return
        if row is None:
            self._selected_repeater = None
            self._panel_stack.set_visible_child_name("empty")
            return
        peer: Peer = row._peer  # type: ignore[attr-defined]
        self._selected_repeater = peer
        self._show_admin_panel(peer)

    def _show_admin_panel(self, peer: Peer) -> None:
        self._admin_title.set_text(peer.display_name)
        self._update_admin_path(peer)  # [admin-path patch]
        self._panel_stack.set_visible_child_name("admin")

        # Update login state UI
        state = self._service.get_repeater_login_state(peer.display_name)
        self._update_login_ui(state)

        # Pre-fill saved password
        saved = self._service.get_saved_repeater_password(peer.display_name)
        if saved:
            self._password_entry.set_text(saved)
            self._forget_btn.set_visible(True)
            self._save_check.set_active(True)
        else:
            self._password_entry.set_text("")
            self._forget_btn.set_visible(False)
            self._save_check.set_active(False)

        # Populate status tab
        self._populate_status(peer)
        self._graph_page.set_repeater(peer.public_key or peer.peer_id)  # [repeater-stats patch]
        self._sync_autologin_check(peer)  # [auto-login patch]

        self._switch_console(peer)  # [console-history patch] each repeater keeps its own console

    def _update_login_ui(self, state: RepeaterLoginState | None) -> None:
        if state is None:
            self._status_label.set_text("Not logged in")
            self._status_label.remove_css_class("login-status-admin")
            self._status_label.remove_css_class("login-status-guest")
            self._status_label.add_css_class("login-status-offline")
            self._login_btn.set_sensitive(True)
            self._guest_btn.set_sensitive(True)
            self._password_entry.set_sensitive(True)
            self._logout_btn.set_sensitive(False)
            self._set_cli_sensitive(False)
        else:
            if state.is_admin:
                self._status_label.set_text("Logged in (admin)")
                self._status_label.remove_css_class("login-status-offline")
                self._status_label.remove_css_class("login-status-guest")
                self._status_label.add_css_class("login-status-admin")
            else:
                self._status_label.set_text("Logged in (guest)")
                self._status_label.remove_css_class("login-status-offline")
                self._status_label.remove_css_class("login-status-admin")
                self._status_label.add_css_class("login-status-guest")
            if state.firmware_ver_level is not None:
                self._status_label.set_text(
                    self._status_label.get_text() + f"  FW: v{state.firmware_ver_level}"
                )
            self._login_btn.set_sensitive(False)
            self._guest_btn.set_sensitive(False)
            self._password_entry.set_sensitive(False)
            self._logout_btn.set_sensitive(True)
            self._set_cli_sensitive(True)

    def _set_cli_sensitive(self, sensitive: bool) -> None:
        self._cmd_entry.set_sensitive(sensitive)
        self._send_btn.set_sensitive(sensitive)
        child = self._quick_row.get_first_child()
        while child is not None:
            if child.get_name() != "cli-help":  # [cli-help patch] help works logged out too
                child.set_sensitive(sensitive)
            child = child.get_next_sibling()

    # -- Login actions ---------------------------------------------------------

    def _on_login_activate(self, _entry: Gtk.PasswordEntry) -> None:
        self._do_login(self._password_entry.get_text())

    def _on_login_clicked(self, _btn: Gtk.Button) -> None:
        self._do_login(self._password_entry.get_text())

    def _on_guest_clicked(self, _btn: Gtk.Button) -> None:
        self._do_login("")

    def _do_login(self, password: str) -> None:
        if self._selected_repeater is None:
            return
        if self._busy:  # [login-queue patch] was silently ignored while a poll/auto-login ran
            name = self._selected_repeater.display_name
            self._status_label.set_text(
                "Login queued: waiting for the current background request "
                "(stats poll or auto-login) to finish\u2026"
            )
            self._queue_command(name, "login", lambda: self._do_login(password))
            return
        peer_name = self._selected_repeater.display_name
        save = self._save_check.get_active()
        self._busy = True
        self._login_btn.set_sensitive(False)
        self._guest_btn.set_sensitive(False)
        self._status_label.set_text("Logging in...")

        def work() -> None:
            error: str | None = None
            result: dict = {}
            try:
                result = self._service.login_to_repeater(peer_name, password, save_password=save)
            except Exception as exc:
                error = str(exc) or type(exc).__name__

            def done() -> bool:
                self._busy = False
                still_selected = (
                    self._selected_repeater is not None
                    and self._selected_repeater.display_name == peer_name
                )
                if error:
                    self._append_for(
                        peer_name, "error", f"Login failed: {error}"
                    )  # [console-history patch]
                    if still_selected:
                        self._login_btn.set_sensitive(True)
                        self._guest_btn.set_sensitive(True)
                        self._update_login_ui(None)
                elif not result.get("success"):
                    reason = result.get("reason", "Login failed")
                    self._append_for(peer_name, "error", reason)  # [console-history patch]
                    if still_selected:
                        self._login_btn.set_sensitive(True)
                        self._guest_btn.set_sensitive(True)
                        self._update_login_ui(None)
                else:
                    if still_selected and self._selected_repeater is not None:
                        state = self._service.get_repeater_login_state(peer_name)
                        self._update_login_ui(state)
                        self._populate_status(self._selected_repeater)
                    self._append_for(
                        peer_name, "response", "Login successful"
                    )  # [console-history patch]
                    self._refresh_list()
                return False

            GLib.idle_add(done)

        threading.Thread(target=work, daemon=True).start()

    def _on_logout_clicked(self, _btn: Gtk.Button) -> None:
        if self._selected_repeater is None or self._busy:
            return
        peer_name = self._selected_repeater.display_name
        self._busy = True
        self._logout_btn.set_sensitive(False)

        def work() -> None:
            error: str | None = None
            try:
                self._service.logout_from_repeater(peer_name)
            except Exception as exc:
                error = str(exc) or type(exc).__name__

            def done() -> bool:
                self._busy = False
                if error:
                    self._append_for(
                        peer_name, "error", f"Logout failed: {error}"
                    )  # [console-history patch]
                    self._logout_btn.set_sensitive(True)
                    return False
                if self._selected_repeater and self._selected_repeater.display_name == peer_name:
                    self._update_login_ui(None)
                    self._populate_status(self._selected_repeater)
                self._append_for(peer_name, "response", "Logged out")  # [console-history patch]
                self._autologin_suppressed.add(peer_name)  # [auto-login patch]
                self._refresh_list()
                return False

            GLib.idle_add(done)

        threading.Thread(target=work, daemon=True).start()

    def _on_forget_clicked(self, _btn: Gtk.Button) -> None:
        if self._selected_repeater is None:
            return
        self._service.delete_saved_repeater_password(self._selected_repeater.display_name)
        self._password_entry.set_text("")
        self._forget_btn.set_visible(False)
        self._save_check.set_active(False)

    # -- CLI actions -----------------------------------------------------------

    # --- [board-history patch] -------------------------------------------------
    def _note_reported(self, peer_name: str, command: str, result: dict) -> None:
        """Remember what 'board', 'ver' and owner info report, per repeater."""
        cmd = command.strip().lower()
        text = (result.get("response_text") or "").strip()
        if cmd == "power":  # [power-source patch] typed by hand: log it for the Battery graph
            self._record_power(peer_name, text)
            return
        if text.startswith(">"):
            text = text[1:].strip()
        if cmd == "board":
            kind, value = "board", text.splitlines()[0] if text else ""
        elif cmd == "ver":
            kind, value = "firmware", text.split()[0] if text else ""
        elif cmd in ("owner", "owner info"):
            fw = (result.get("firmware") or "").split()
            kind, value = "firmware", fw[0] if fw else ""
        else:
            return
        low = value.lower()
        if not value or low.startswith(("unknown", "error", "err", "??", "(")):
            return
        peer = next((p for p in self._service.list_peers() if p.display_name == peer_name), None)
        get_store = getattr(self._service, "get_repeater_samples", None)
        store = get_store() if get_store is not None else None
        if peer is None or not peer.public_key or store is None or not hasattr(store, "note_info"):
            return
        try:
            store.note_info(peer.public_key, kind, value)
        except Exception:  # noqa: BLE001
            logger.exception("could not record %s for %s", kind, peer_name)
            return
        sel = self._selected_repeater
        if sel is not None and sel.display_name == peer_name:
            self._populate_status(sel)

    # --- [power-source patch] --------------------------------------------------
    def _poll_power(self, peer_name: str) -> None:
        """After a stats poll (worker thread): ask an admin-logged-in repeater for 'power'.
        Firmware without the command is skipped for the rest of the session."""
        state = self._service.get_repeater_login_state(peer_name)
        if state is None or not state.is_admin:
            return
        skip = getattr(self, "_power_unsupported", None)
        if skip is None:
            skip = self._power_unsupported = set()
        if peer_name in skip:
            return
        try:
            reply = self._service.send_repeater_command(peer_name, "power")
        except Exception as exc:  # noqa: BLE001
            logger.debug("power poll %s: %s", peer_name, exc)
            return
        if not reply.get("success"):
            return
        if not self._record_power(peer_name, reply.get("response_text") or ""):
            skip.add(peer_name)
            logger.info(
                "%s: no 'power' command (%r); not asking again this session",
                peer_name,
                (reply.get("response_text") or "")[:40],
            )

    def _record_power(self, peer_name: str, text: str) -> bool:
        from meshcore_console.meshcore.repeater_stats import parse_power

        info = parse_power(text)
        if info is None:
            return False
        peer = next((p for p in self._service.list_peers() if p.display_name == peer_name), None)
        get_store = getattr(self._service, "get_repeater_samples", None)
        store = get_store() if get_store is not None else None
        if peer is not None and peer.public_key and store is not None:
            store.add(peer.public_key, "power", batt_mv=info["batt_mv"], data=info)
        return True

    def _add_hardware_rows(self, peer: Peer) -> None:
        """Hardware section: current board / firmware and what it reported before."""
        get_store = getattr(self._service, "get_repeater_samples", None)
        store = get_store() if get_store is not None else None
        if store is None or not hasattr(store, "info_history") or not peer.public_key:
            return
        try:
            boards = store.info_history(peer.public_key, "board")
            firmware = store.info_history(peer.public_key, "firmware")
        except Exception:  # noqa: BLE001
            return
        self._add_section_header("Hardware")
        if not boards and not firmware:
            hint = Gtk.Label(
                label="Not reported yet. Run 'board' or 'ver' in Console (admin), "
                "or 'owner' (any login)."
            )
            hint.add_css_class("panel-muted")
            hint.set_wrap(True)
            hint.set_xalign(0.0)
            hint.set_halign(Gtk.Align.START)
            self._status_content.append(hint)
            return

        def day(ts: float) -> str:
            return time.strftime("%b %-d %Y", time.localtime(ts))

        def span(h: dict) -> str:
            first, last = day(h["first"]), day(h["last"])
            return first if first == last else f"{first} \u2013 {last}"

        for label, hist, source in (
            ("Board:", boards, "'board'"),
            ("Version:", firmware, "'ver' / owner info"),
        ):
            if not hist:
                continue
            cur = hist[0]
            row = DetailRow(label, cur["value"])
            row.set_tooltip_text(f"Reported by {source} {cur['n']}\u00d7, {span(cur)}")
            self._status_content.append(row)
            for old in hist[1:]:
                prev = DetailRow("  before:", f"{old['value']}  ({span(old)})")
                prev.set_tooltip_text(f"Reported {old['n']}\u00d7")
                prev.add_css_class("panel-muted")
                self._status_content.append(prev)

    def _on_cli_help(self, btn: Gtk.Button) -> None:  # [cli-help patch]
        from meshcore_console.ui_gtk.widgets.cli_help import CliHelpWindow

        win = getattr(self, "_cli_help_win", None)
        if win is None:
            win = CliHelpWindow(btn, self._cli_help_pick)
            win.connect("close-request", self._on_cli_help_closed)
            self._cli_help_win = win
        win.present()

    def _on_cli_help_closed(self, _win) -> bool:
        self._cli_help_win = None
        return False

    def _cli_help_pick(self, text: str) -> None:
        """Put a command from the help window in the command line (not sent)."""
        if not self._cmd_entry.get_sensitive():
            return
        self._cmd_entry.set_text(text)
        self._cmd_entry.grab_focus()
        self._cmd_entry.set_position(-1)

    def _on_quick_cmd(self, _btn: Gtk.Button, command: str) -> None:
        self._send_command(command)

    def _on_cmd_activate(self, _entry: Gtk.Entry) -> None:
        text = self._cmd_entry.get_text().strip()
        if text:
            self._send_command(text)

    def _on_send_clicked(self, _btn: Gtk.Button) -> None:
        text = self._cmd_entry.get_text().strip()
        if text:
            self._send_command(text)

    def _on_cmd_key(
        self,
        _ctrl: Gtk.EventControllerKey,
        keyval: int,
        _keycode: int,
        _state: Gdk.ModifierType,
    ) -> bool:
        if keyval == Gdk.KEY_Up:
            if self._command_history and self._history_index < len(self._command_history) - 1:
                self._history_index += 1
                self._cmd_entry.set_text(self._command_history[-(self._history_index + 1)])
                self._cmd_entry.set_position(-1)
            return True
        if keyval == Gdk.KEY_Down:
            if self._history_index > 0:
                self._history_index -= 1
                self._cmd_entry.set_text(self._command_history[-(self._history_index + 1)])
                self._cmd_entry.set_position(-1)
            elif self._history_index == 0:
                self._history_index = -1
                self._cmd_entry.set_text("")
            return True
        return False

    # --- [neighbours-request patch] ---------------------------------------------
    def _owner_info(self, peer_name: str) -> dict:  # [owner-info patch]
        """Owner info by request (any login); admins fall back to 'get owner.info'."""
        from meshcore_console.meshcore.repeater_stats import format_owner_info

        request = getattr(self._service, "request_repeater_owner_info", None)
        result = (
            request(peer_name)
            if request is not None
            else {"success": False, "reason": "not supported"}
        )
        if result.get("success"):
            return {
                "success": True,
                "response_text": format_owner_info(result),
                "firmware": result.get("firmware", ""),
            }  # [board-history patch]
        state = self._service.get_repeater_login_state(peer_name)
        if state is None or not state.is_admin:
            return {
                "success": False,
                "reason": f"Owner info request: {result.get('reason', 'no response')} "
                "(needs repeater firmware level 2 or later).",
            }
        fallback = self._service.send_repeater_command(peer_name, "get owner.info")
        if fallback.get("success"):
            text = fallback.get("response_text", "").strip()
            text = text[1:].strip() if text.startswith(">") else text
            lines = [line for line in text.split("|")] or ["(not set)"]
            fallback["response_text"] = "\n".join(
                [f"Owner     {lines[0] or '(not set)'}"]
                + [f"          {line}" for line in lines[1:]]
            )
        return fallback

    def _repeater_command(self, peer_name: str, command: str) -> dict:
        """Send a CLI command; 'neighbors' goes by request first so guest logins work.

        The firmware only runs CLI commands for admin logins, but answers the
        neighbours request for guests too. Admins fall back to the CLI command if
        the request goes unanswered (firmware without it); such repeaters are
        remembered for the session so admins don't wait for the request again.
        """
        if command.strip().lower() in ("owner", "owner info"):  # [owner-info patch]
            return self._owner_info(peer_name)
        if command.strip().lower() != "neighbors":
            return self._service.send_repeater_command(peer_name, command)
        request = getattr(self._service, "request_repeater_neighbours", None)
        state = self._service.get_repeater_login_state(peer_name)
        is_admin = bool(state is not None and state.is_admin)
        cli_only = getattr(self, "_neighbours_cli_only", None)
        if cli_only is None:
            cli_only = self._neighbours_cli_only = set()
        if request is None or (is_admin and peer_name in cli_only):
            return self._service.send_repeater_command(peer_name, command)
        result = request(peer_name)
        if result.get("success"):
            logger.info(
                "neighbours of %s by request: %d of %d",
                peer_name,
                len(result.get("rows", [])),
                result.get("total", 0),
            )
            return {"success": True, "response_text": result["text"]}
        if not is_admin:
            return {
                "success": False,
                "reason": f"Neighbour request: {result.get('reason', 'no response')}. A guest login can't use "
                "the 'neighbors' command, and this repeater's firmware may be too old for the request.",
            }
        logger.info(
            "neighbours request to %s failed (%s); using the CLI command",
            peer_name,
            result.get("reason"),
        )
        fallback = self._service.send_repeater_command(peer_name, command)
        if fallback.get("success"):
            cli_only.add(peer_name)
        return fallback

    # --- [cmd-queue patch] --------------------------------------------------------
    def _queue_command(self, peer_name: str, label: str, run) -> None:
        """Hold a command clicked while another request is in flight; send it when free."""
        queue = getattr(self, "_cmd_queue", None)
        if queue is None:
            queue = self._cmd_queue = []
        if any(p == peer_name and lbl == label for p, lbl, _r in queue):
            return  # already waiting
        queue.append((peer_name, label, run))
        with self._console_target(peer_name):
            self._append_console_lines(
                f"{label}: queued, waiting for the current request "
                "(auto-login or stats poll) to finish\u2026",
                "panel-muted",
            )
        if not getattr(self, "_cmd_queue_tick", False):
            self._cmd_queue_tick = True
            GLib.timeout_add(300, self._drain_command_queue)

    def _drain_command_queue(self) -> bool:
        queue = getattr(self, "_cmd_queue", [])
        if self._busy:
            return True
        if not queue:
            self._cmd_queue_tick = False
            return False
        peer_name, label, run = queue.pop(0)
        sel = self._selected_repeater
        if sel is None or sel.display_name != peer_name:
            self._append_for(
                peer_name, "error", f"{label}: not sent (another repeater was selected)"
            )
            return True
        run()
        return True

    def _send_command(self, command: str) -> None:
        if self._selected_repeater is not None and self._busy:  # [cmd-queue patch]
            self._queue_command(
                self._selected_repeater.display_name, command, lambda: self._send_command(command)
            )
            return
        if self._selected_repeater is None or self._busy:
            return
        peer_name = self._selected_repeater.display_name

        # History
        self._command_history.append(command)
        if len(self._command_history) > _MAX_HISTORY:
            self._command_history = self._command_history[-_MAX_HISTORY:]
        self._history_index = -1

        self._cmd_entry.set_text("")
        self._append_console_command(command)
        self._busy = True
        self._send_btn.set_sensitive(False)

        def work() -> None:
            error: str | None = None
            result: dict = {}
            try:
                result = self._repeater_command(peer_name, command)  # [neighbours-request patch]
            except Exception as exc:
                error = str(exc) or type(exc).__name__

            def done() -> bool:
                self._busy = False
                self._send_btn.set_sensitive(True)
                with self._console_target(
                    peer_name
                ):  # [console-history patch] reply goes to its own console
                    if error:
                        self._append_console_error(f"Error: {error}")
                    elif not result.get("success"):
                        reason = result.get("reason", "No response")
                        self._append_console_error(reason)
                    else:
                        text = result.get("response_text", "")
                        self._note_reported(peer_name, command, result)  # [board-history patch]
                        if text and self._append_neighbors_table(
                            command, text
                        ):  # [neighbors patch]
                            pass
                        elif text:
                            self._append_console_response(text)
                        else:
                            self._append_console_response("(no response)")
                return False

            GLib.idle_add(done)

        threading.Thread(target=work, daemon=True).start()

    # -- Console output --------------------------------------------------------

    def _make_console_label(self, text: str, css_class: str) -> Gtk.Label:
        label = Gtk.Label(label=text)
        label.add_css_class(css_class)
        label.set_halign(Gtk.Align.START)
        label.set_xalign(0.0)
        label.set_wrap(True)
        label.set_wrap_mode(Pango.WrapMode.CHAR)
        label.set_selectable(True)
        label.set_size_request(10, -1)
        label.set_hexpand(True)
        return label

    # --- [console-history patch] ----------------------------------------------
    # One console per repeater (keyed by public key): output, scroll position
    # and up/down command recall survive switching repeaters and app screens.
    # Replies that arrive after you switch away go to their own repeater.
    _CONSOLE_MAX_LINES = 2000

    def _console_entry(self, key: str) -> dict:
        consoles = getattr(self, "_consoles", None)
        if consoles is None:
            consoles = self._consoles = {}
        entry = consoles.get(key)
        if entry is None:
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            box.add_css_class("cli-console")
            entry = consoles[key] = {"box": box, "history": [], "follow": True, "value": 0.0}
        return entry

    def _switch_console(self, peer: Peer) -> None:
        key = self._repeater_key(peer.display_name)
        current = getattr(self, "_console_current_key", None)
        if key == current:
            return  # same repeater (re-selected, or list refreshed): keep it as it is
        adj = self._console_scroll.get_vadjustment()
        if current is not None:
            old = self._console_entry(current)
            old["history"] = self._command_history
            old["follow"] = getattr(self, "_console_follow", True)
            old["value"] = adj.get_value()
        entry = self._console_entry(key)
        holder = self._console_scroll.get_child()
        if isinstance(holder, Gtk.Viewport):
            holder.set_child(None)  # release the previous console box (kept in _consoles)
        self._console_scroll.set_child(entry["box"])
        self._console_box = entry["box"]
        self._console_current_key = key
        self._command_history = entry["history"]
        self._history_index = -1
        if entry["follow"]:
            self._scroll_console()
        else:
            self._console_follow = False
            value = entry["value"]
            GLib.idle_add(lambda: (adj.set_value(value), False)[1], priority=GLib.PRIORITY_LOW)

    def _console_target(self, peer_name: str):
        """Context manager: console output inside goes to *peer_name*'s console."""
        view = self

        class _Target:
            def __enter__(self_):
                self_.prev = (view._console_box, getattr(view, "_console_offscreen", False))
                key = view._repeater_key(peer_name)
                if key != getattr(view, "_console_current_key", None):
                    view._console_box = view._console_entry(key)["box"]
                    view._console_offscreen = True
                return view._console_box

            def __exit__(self_, *_exc):
                view._trim_console(view._console_box)
                view._console_box, view._console_offscreen = self_.prev
                return False

        return _Target()

    def _append_for(self, peer_name: str, kind: str, text: str) -> None:
        with self._console_target(peer_name):
            (self._append_console_response if kind == "response" else self._append_console_error)(
                text
            )

    def _trim_console(self, box: Gtk.Box) -> None:
        n = 0
        child = box.get_first_child()
        while child is not None:
            n += 1
            child = child.get_next_sibling()
        while n > self._CONSOLE_MAX_LINES:
            first = box.get_first_child()
            if first is None:
                break
            box.remove(first)
            n -= 1

    def _append_console_command(self, text: str) -> None:
        self._console_box.append(self._make_console_label(f"> {text}", "cli-command"))
        self._scroll_console()

    def _append_console_response(self, text: str) -> None:
        self._append_console_lines(text, "cli-response")

    def _append_console_error(self, text: str) -> None:
        self._append_console_lines(text, "cli-error")

    def _append_console_lines(self, text: str, css_class: str) -> None:
        # [console-lines patch] One label per line: GTK under-measured the height
        # of a single wrapping multi-line label here, so the scroll range ended
        # early and the last lines of e.g. the stats reply were clipped.
        for line in text.split("\n") or [""]:
            self._console_box.append(self._make_console_label(line or " ", css_class))
        self._scroll_console()

    def _scroll_console(self) -> None:
        if getattr(self, "_console_offscreen", False):  # [console-history patch] not on screen
            return
        # [autoscroll patch v3] Keep the console pinned to the bottom after new
        # output, three ways: follow changes of the scroll range (notify::upper /
        # page-size), an idle scroll, and a per-frame pin for ~90 drawn frames.
        # Frames do not advance while the UI thread is busy (e.g. rebuilding the
        # Status tab and graph after a stats reply), so the pin outlasts any delay.
        # Only a real scroll-up by the user stops following.
        sw = self._console_scroll
        adj = sw.get_vadjustment()
        if not getattr(self, "_autoscroll_hooked", False):
            adj.connect("notify::upper", self._on_console_extent)
            adj.connect("notify::page-size", self._on_console_extent)
            wheel = Gtk.EventControllerScroll.new(Gtk.EventControllerScrollFlags.VERTICAL)
            wheel.connect("scroll", self._on_console_wheel)
            sw.add_controller(wheel)
            self._autoscroll_hooked = True
        self._console_follow = True
        self._console_pin_frames = 90
        if not getattr(self, "_console_tick_id", 0):
            self._console_tick_id = sw.add_tick_callback(self._console_tick)
        GLib.idle_add(self._console_to_bottom, priority=GLib.PRIORITY_LOW)

    def _console_to_bottom(self, *_args) -> bool:
        adj = self._console_scroll.get_vadjustment()
        target = adj.get_upper() - adj.get_page_size()
        if adj.get_value() < target:
            adj.set_value(target)
        return False

    def _on_console_extent(self, _adj, _pspec) -> None:
        # [autoscroll patch v4] notify::upper fires while GTK is laying out the
        # viewport. Setting the value there updates the adjustment, but the
        # viewport ignores the re-layout it queues, so the view stays at the old
        # offset while the value claims 'bottom'. Defer to just after layout.
        if not getattr(self, "_console_follow", False):
            return
        if getattr(self, "_console_idle_pending", False):
            return
        self._console_idle_pending = True

        def _after_layout() -> bool:
            self._console_idle_pending = False
            self._console_to_bottom()
            return False

        GLib.idle_add(_after_layout)

    def _console_tick(self, _widget, _clock) -> bool:
        if getattr(self, "_console_follow", False):
            self._console_to_bottom()
        self._console_pin_frames -= 1
        if self._console_pin_frames > 0:
            return True
        adj = self._console_scroll.get_vadjustment()
        logger.debug(
            "console autoscroll settled: value=%.0f upper=%.0f page=%.0f follow=%s",
            adj.get_value(),
            adj.get_upper(),
            adj.get_page_size(),
            self._console_follow,
        )
        self._console_tick_id = 0
        return False

    def _on_console_wheel(self, _ctrl, _dx: float, dy: float) -> bool:
        adj = self._console_scroll.get_vadjustment()
        if dy < 0:
            self._console_follow = False  # user scrolled up to read history
        elif adj.get_value() + dy * 40 >= adj.get_upper() - adj.get_page_size() - 2:
            self._console_follow = True  # back at the bottom
        return False

    # -- Events ----------------------------------------------------------------

    def _on_events(self, _store: object) -> None:
        self._event_cursor, events = self._event_store.since(self._event_cursor, limit=200)
        needs_refresh = False
        for event in events:
            etype = event.get("type", "")
            if etype in (
                "repeater_login",
                "repeater_logout",
                "advert_received",
                "contact_received",
            ):
                needs_refresh = True
        if needs_refresh:
            self._refresh_list()
            if self._selected_repeater is not None:  # [admin-path patch] path may have changed
                self._update_admin_path(self._selected_repeater)

    # -- Public API for MainWindow.navigate_to ---------------------------------

    # --- [admin-sort patch] ---------------------------------------------------
    def _sort_admin_repeaters(self, repeaters: list[Peer], logged_in: set[str]) -> list[Peer]:
        mode = getattr(self, "_admin_sort", "name")

        def seen(p: Peer) -> float:
            return p.last_advert_time.timestamp() if p.last_advert_time else 0.0

        def name(p: Peer) -> str:
            return (p.display_name or "").casefold()

        if mode == "newest":
            key = lambda p: (not p.is_favorite, p.last_advert_time is None, -seen(p))  # noqa: E731
        elif mode == "oldest":
            key = lambda p: (not p.is_favorite, p.last_advert_time is None, seen(p))  # noqa: E731
        elif mode == "signal":  # strongest RSSI first, never-heard-directly last
            key = lambda p: (not p.is_favorite, p.rssi is None, -(p.rssi or 0), name(p))  # noqa: E731
        elif mode == "logged_in":
            key = lambda p: (not p.is_favorite, p.display_name not in logged_in, name(p))  # noqa: E731
        elif mode == "distance":
            dist = self._admin_distances(repeaters)
            key = lambda p: (  # noqa: E731
                not p.is_favorite,
                p.peer_id not in dist,
                dist.get(p.peer_id, 0.0),
                name(p),
            )
        else:
            key = lambda p: (not p.is_favorite, name(p))  # noqa: E731
        return sorted(repeaters, key=key)

    def _admin_distances(self, repeaters: list[Peer]) -> dict[str, float]:
        try:
            from meshcore_console.ui_gtk.views.peers import _distance_km, _has_location
        except ImportError:
            return {}
        origin = None
        try:
            loc = self._service.get_device_location()
            if loc and _has_location(loc[0], loc[1]):
                origin = loc
            else:
                st = self._service.get_settings()
                if _has_location(st.latitude, st.longitude):
                    origin = (st.latitude, st.longitude)
        except Exception:  # noqa: BLE001
            origin = None
        if origin is None:
            return {}
        return {
            p.peer_id: _distance_km(origin, (p.latitude, p.longitude))
            for p in repeaters
            if _has_location(p.latitude, p.longitude)
        }

    def _admin_sort_tip(self) -> str:
        label = ADMIN_SORT_LABELS[ADMIN_SORT_MODES.index(self._admin_sort)]
        return f"Sort: {label} (favourites stay on top)"

    def _on_admin_sort_changed(self, chk: Gtk.CheckButton, mode: str) -> None:
        if not chk.get_active():
            return
        self._admin_sort = mode
        _save_admin_sort(mode)
        self._admin_sort_btn.set_tooltip_text(self._admin_sort_tip())
        self._refresh_list()

    def _on_admin_clear_clicked(self, _btn: Gtk.Button) -> None:
        from gi.repository import Adw

        from meshcore_console.ui_gtk.compat import build_alert_dialog, present_dialog

        logged_in = set(self._service.list_logged_in_repeaters())
        reps = [
            p
            for p in self._service.list_peers()
            if p.is_repeater and p.display_name not in logged_in
        ]
        if not reps:
            return
        favs = sum(1 for p in reps if p.is_favorite)
        body = (
            f"Removes {len(reps)} repeaters from the list (also from Peers and the Map). "
            "They come back as their adverts are heard again."
        )
        if logged_in:
            body += (
                f"\n\n{len(logged_in)} logged-in repeater{'s' if len(logged_in) != 1 else ''} stay."
            )
        if favs:
            body += f"\n\n{favs} of them are favourites (\u2605)."
        dialog = build_alert_dialog(self.get_root(), "Clear repeaters?", body)
        dialog.add_response("cancel", "Cancel")
        if favs and favs < len(reps):
            dialog.add_response("keep", "Keep favourites")
        dialog.add_response("all", "Delete all")
        dialog.set_response_appearance("all", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", self._on_admin_clear_confirmed)
        present_dialog(dialog, self.get_root())

    def _on_admin_clear_confirmed(self, _dialog, response: str) -> None:
        if response not in ("keep", "all"):
            return
        logged_in = set(self._service.list_logged_in_repeaters())
        removed = 0
        for p in list(self._service.list_peers()):
            if not p.is_repeater or p.display_name in logged_in:
                continue
            if response == "keep" and p.is_favorite:
                continue
            if self._service.delete_peer(p.peer_id):
                removed += 1
        logger.info("UI: admin cleared %d repeaters", removed)
        sel = self._selected_repeater
        if sel is not None and sel.display_name not in {
            p.display_name for p in self._service.list_peers()
        }:
            self._selected_repeater = None
            self._panel_stack.set_visible_child_name("empty")
        self._refresh_list()

    def _on_admin_shown(self, _widget) -> None:  # [admin-refresh patch]
        self._refresh_list()
        if self._selected_repeater is not None:
            self._update_admin_path(self._selected_repeater)

    # --- [admin-fold patch] -------------------------------------------------------
    def embed(self) -> None:
        """Shown inside Repeaters: hide the own list and the name (Repeaters has both)."""
        first, last = self.get_first_child(), self.get_last_child()
        if first is not None and first is not last:
            first.set_visible(False)
        if last is not None:
            last.remove_css_class("panel-card")  # already inside the Repeaters card
        self._admin_title.set_visible(False)

    def page_switcher(self) -> Gtk.Widget:  # [repeater-tabs patch]
        """Status / Graph / Console buttons for the panel title row (the tabs are hidden)."""
        self._notebook.set_show_tabs(False)
        self._notebook.set_show_border(False)
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        box.add_css_class("linked")
        self._tab_btns = []
        for i, name in enumerate(("Status", "Graph", "Console")):
            btn = Gtk.ToggleButton(label=name)
            if self._tab_btns:
                btn.set_group(self._tab_btns[0])
            btn.connect("toggled", _on_tab_toggled, i)
            self._tab_btns.append(btn)
            box.append(btn)
        self._tab_btns[0].set_active(True)
        self._notebook.connect("switch-page", _on_page_switched)
        return box

    def show_repeater(self, peer: Peer | None) -> None:
        """Follow the selection in the Repeaters list."""
        if peer is None:
            self._repeater_list.unselect_all()
            return
        cur = self._selected_repeater
        if (
            cur is not None
            and cur.display_name == peer.display_name
            and self._panel_stack.get_visible_child_name() == "admin"
        ):
            self._selected_repeater = (
                peer  # [status-live patch] heard again: signal, last seen, path
            )
            self._populate_status(peer)
            self._update_admin_path(peer)
            return  # same repeater: keep a half-typed password or command
        row = self._repeater_list.get_first_child()
        while row is not None:
            rp = getattr(row, "_peer", None)
            if rp is not None and rp.display_name == peer.display_name:
                self._repeater_list.select_row(row)
                return
            row = row.get_next_sibling()
        self._selected_repeater = peer  # not in the hidden list yet
        self._show_admin_panel(peer)

    def get_default_focus(self) -> Gtk.Widget | None:
        return self._repeater_list
