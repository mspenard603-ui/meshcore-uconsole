from __future__ import annotations

import logging

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import GLib, Gio, Gtk

from meshcore_console.core.services import MeshcoreService
from meshcore_console.meshcore.config import hardware_env_overrides, parse_pin_list
from meshcore_console.meshcore.logging_setup import (
    VALID_LEVELS,
    export_logs_to_path,
    get_rf_noise_counts,
    set_stderr_level,
)
from meshcore_console.meshcore.region import normalize_region_scope
from meshcore_console.meshcore.settings import (
    REPEATER_EXPIRY,
    MeshcoreSettings,
    apply_hardware_preset,
    apply_preset,
)
from meshcore_console.ui_gtk.widgets.qr_dialog import QrCodeDialog

logger = logging.getLogger(__name__)


def _abbreviate_key(key: str | None) -> str:
    """Abbreviate a public key for display."""
    if not key:
        return "Not available"
    if len(key) <= 16:
        return key
    return f"{key[:8]}...{key[-8:]}"


# --- [settings-guard patch] ----------------------------------------------------
_RADIO_FIELDS = (
    ("frequency", "Frequency", lambda v: f"{v / 1_000_000:.3f} MHz"),
    ("bandwidth", "Bandwidth", lambda v: f"{v / 1000:g} kHz"),
    ("spreading_factor", "Spreading factor", lambda v: f"SF{v}"),
    ("coding_rate", "Coding rate", lambda v: f"4/{v}"),
    ("tx_power", "TX power", lambda v: f"{v} dBm"),
    ("preamble_length", "Preamble", str),
    ("path_hash_mode", "Path hash", lambda v: f"{int(v) + 1}-byte"),
    ("hardware_preset", "Hardware preset", str),
    ("en_pins", "Enable pins", lambda v: v or "none"),
    ("reset_pin", "Reset pin", str),
    ("busy_pin", "Busy pin", str),
    ("irq_pin", "IRQ pin", str),
    ("txen_pin", "TXEN pin", str),
    ("rxen_pin", "RXEN pin", str),
    ("use_dio2_rf", "DIO2 RF switch", str),
    ("use_dio3_tcxo", "DIO3 TCXO", str),
)


def _radio_changes(old, new) -> list[str]:
    """'Frequency: 910.525 MHz \u2192 869.525 MHz' for each radio/hardware field that differs."""
    out = []
    for key, label, fmt in _RADIO_FIELDS:
        a, b = getattr(old, key, None), getattr(new, key, None)
        if a != b:
            try:
                out.append(f"{label}: {fmt(a)} \u2192 {fmt(b)}")
            except Exception:  # noqa: BLE001
                out.append(f"{label}: {a} \u2192 {b}")
    return out


def _scroll_page_instead(ctrl, _dx, dy):
    """Capture-phase scroll on a dropdown: scroll the page, swallow the event."""
    widget = ctrl.get_widget()
    page = widget.get_ancestor(Gtk.ScrolledWindow) if widget is not None else None
    if page is not None:
        adj = page.get_vadjustment()
        adj.set_value(adj.get_value() + dy * 48)
    return True


def _settings_escape(ctrl, keyval, _code, _state) -> bool:  # [settings-close patch]
    from gi.repository import Gdk

    if keyval != Gdk.KEY_Escape:
        return False
    view = ctrl.get_widget()
    if view is not None and hasattr(view, "_on_close_settings"):
        view._on_close_settings()
    return True


def _guard_scroll(root) -> None:
    child = root.get_first_child()
    while child is not None:
        if isinstance(child, (Gtk.ComboBox, Gtk.DropDown, Gtk.SpinButton)):
            ctrl = Gtk.EventControllerScroll.new(Gtk.EventControllerScrollFlags.VERTICAL)
            ctrl.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
            ctrl.connect("scroll", _scroll_page_instead)
            child.add_controller(ctrl)
        else:
            _guard_scroll(child)
        child = child.get_next_sibling()


class SettingsView(Gtk.Box):
    """Settings view matching official MeshCore app layout."""

    def __init__(self, service: MeshcoreService) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self._service = service
        self._status_label = Gtk.Label(label="")
        self._status_label.add_css_class("panel-muted")
        self._status_label.set_halign(Gtk.Align.START)
        self._entries: dict[str, Gtk.Entry] = {}
        self._switches: dict[str, Gtk.Switch] = {}
        self._public_key_label: Gtk.Label | None = None
        self._rf_noise_timer_id = 0

        scroll = Gtk.ScrolledWindow.new()
        scroll.set_vexpand(True)
        scroll.set_hexpand(True)
        self.append(scroll)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        content.set_margin_start(8)
        content.set_margin_end(8)
        content.set_margin_top(8)
        scroll.set_child(content)

        # Two-column layout
        columns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
        columns.set_homogeneous(True)
        content.append(columns)

        # Left column: Public Info + Radio
        left_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        left_col.append(self._build_public_info_panel())
        left_col.append(self._build_radio_panel())
        columns.append(left_col)

        # Right column: Hardware (advanced) + Logging
        right_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        right_col.append(self._build_hardware_panel())
        right_col.append(self._build_logging_panel())
        right_col.append(self._build_repeaters_panel())
        right_col.append(self._build_schedule_panel())  # [airtime]
        columns.append(right_col)

        # Actions bar at bottom
        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        actions.add_css_class("panel-card")
        self._save_button = Gtk.Button.new_with_label("Save Settings")
        self._save_button.connect("clicked", self._on_save)
        actions.append(self._save_button)

        reload_btn = Gtk.Button.new_with_label("Reload")
        reload_btn.connect("clicked", self._on_reload)
        actions.append(reload_btn)
        close_btn = Gtk.Button.new_with_label("Close")  # [settings-close patch]
        close_btn.set_tooltip_text(
            "Back to the previous screen (Esc). Unsaved changes are discarded."
        )
        close_btn.connect("clicked", self._on_close_settings)
        actions.append(close_btn)
        esc = Gtk.EventControllerKey.new()
        esc.connect("key-pressed", _settings_escape)
        self.add_controller(esc)
        actions.append(self._status_label)
        content.append(actions)

        self._load_from_service()
        _guard_scroll(self)  # [settings-guard patch] the wheel scrolls the page, never a dropdown

    def _build_public_info_panel(self) -> Gtk.Box:
        panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        panel.add_css_class("panel-card")

        title = Gtk.Label(label="Public Info")
        title.add_css_class("panel-title")
        title.set_halign(Gtk.Align.START)
        panel.append(title)

        grid = Gtk.Grid()
        grid.set_row_spacing(8)
        grid.set_column_spacing(8)

        # Row 0: Name
        grid.attach(self._grid_label("Name"), 0, 0, 1, 1)
        grid.attach(self._grid_entry("node_name", 18), 1, 0, 2, 1)

        # Row 1: Public Key (display only)
        grid.attach(self._grid_label("Public Key"), 0, 1, 1, 1)
        self._public_key_label = Gtk.Label(label="Loading...")
        self._public_key_label.add_css_class("panel-muted")
        self._public_key_label.set_halign(Gtk.Align.START)
        self._public_key_label.set_selectable(True)
        grid.attach(self._public_key_label, 1, 1, 1, 1)

        qr_btn = Gtk.Button.new_with_label("QR")
        qr_btn.set_tooltip_text("Show QR code")
        qr_btn.connect("clicked", self._on_show_qr)
        grid.attach(qr_btn, 2, 1, 1, 1)

        # Row 2: Latitude / Longitude
        grid.attach(self._grid_label("Latitude"), 0, 2, 1, 1)
        grid.attach(self._grid_entry("latitude", 10), 1, 2, 1, 1)
        grid.attach(self._grid_label("Longitude"), 0, 3, 1, 1)
        grid.attach(self._grid_entry("longitude", 10), 1, 3, 1, 1)

        # Row 4: Share Position in Advert
        grid.attach(self._grid_label("Share GPS Position"), 0, 4, 1, 1)
        grid.attach(self._grid_switch("share_position"), 1, 4, 1, 1)

        # Row 5: Allow Telemetry
        grid.attach(self._grid_label("Allow Telemetry"), 0, 5, 1, 1)
        allow_telem_switch = self._grid_switch("allow_telemetry")
        grid.attach(allow_telem_switch, 1, 5, 1, 1)

        # Row 6: Favorites Only (sub-option of Allow Telemetry)
        fav_label = self._grid_label("Favorites Only")
        fav_switch = self._grid_switch("telemetry_favorites_only")
        grid.attach(fav_label, 0, 6, 1, 1)
        grid.attach(fav_switch, 1, 6, 1, 1)

        def _sync_fav_sensitivity(*_args: object) -> None:
            active = allow_telem_switch.get_active()
            fav_label.set_sensitive(active)
            fav_switch.set_sensitive(active)

        allow_telem_switch.connect("notify::active", _sync_fav_sensitivity)
        _sync_fav_sensitivity()

        # Row 7: Autoconnect
        grid.attach(self._grid_label("Autoconnect"), 0, 7, 1, 1)
        grid.attach(self._grid_switch("autoconnect"), 1, 7, 1, 1)

        # Row 8: GPS serial device (empty = auto-detect ttyS0/ttyAMA0)
        grid.attach(self._grid_label("GPS Device"), 0, 8, 1, 1)
        gps_entry = self._grid_entry("gps_device", 18)
        gps_entry.set_placeholder_text("auto (/dev/ttyS0, /dev/ttyAMA0)")
        gps_entry.set_tooltip_text(
            "Serial port for the GPS module. Leave empty to auto-detect. "
            "CM4 units use /dev/ttyS0, CM5 units use /dev/ttyAMA0."
        )
        grid.attach(gps_entry, 1, 8, 2, 1)

        panel.append(grid)
        return panel

    def _build_radio_panel(self) -> Gtk.Box:
        panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        panel.add_css_class("panel-card")

        title = Gtk.Label(label="Radio Settings")
        title.add_css_class("panel-title")
        title.set_halign(Gtk.Align.START)
        panel.append(title)

        grid = Gtk.Grid()
        grid.set_row_spacing(8)
        grid.set_column_spacing(8)

        # Row 0: Preset
        grid.attach(self._grid_label("Preset"), 0, 0, 1, 1)
        self._preset = Gtk.ComboBoxText.new()
        self._preset.append("meshcore-us", "USA/Canada")
        self._preset.append("meshcore-eu", "Europe")
        self._preset.append("custom", "Custom")
        self._preset.set_active_id("meshcore-us")
        self._preset.connect("changed", self._on_preset_changed)
        grid.attach(self._preset, 1, 0, 3, 1)

        # Row 1: Frequency (MHz)
        grid.attach(self._grid_label("Frequency"), 0, 1, 1, 1)
        grid.attach(self._grid_entry("frequency", 10), 1, 1, 1, 1)
        mhz_label = Gtk.Label(label="MHz")
        mhz_label.add_css_class("panel-muted")
        mhz_label.set_halign(Gtk.Align.START)
        grid.attach(mhz_label, 2, 1, 1, 1)

        # Row 2: Bandwidth (kHz)
        grid.attach(self._grid_label("Bandwidth"), 0, 2, 1, 1)
        grid.attach(self._grid_entry("bandwidth", 8), 1, 2, 1, 1)
        khz_label = Gtk.Label(label="kHz")
        khz_label.add_css_class("panel-muted")
        khz_label.set_halign(Gtk.Align.START)
        grid.attach(khz_label, 2, 2, 1, 1)

        # Row 3: Spreading Factor
        grid.attach(self._grid_label("Spreading Factor"), 0, 3, 1, 1)
        grid.attach(self._grid_entry("spreading_factor", 4), 1, 3, 1, 1)

        # Row 4: Coding Rate
        grid.attach(self._grid_label("Coding Rate"), 0, 4, 1, 1)
        grid.attach(self._grid_entry("coding_rate", 4), 1, 4, 1, 1)

        # Row 5: TX Power
        grid.attach(self._grid_label("TX Power"), 0, 5, 1, 1)
        grid.attach(self._grid_entry("tx_power", 4), 1, 5, 1, 1)
        dbm_label = Gtk.Label(label="dBm")
        dbm_label.add_css_class("panel-muted")
        dbm_label.set_halign(Gtk.Align.START)
        grid.attach(dbm_label, 2, 5, 1, 1)

        # Row 6: Preamble Length
        grid.attach(self._grid_label("Preamble"), 0, 6, 1, 1)
        grid.attach(self._grid_entry("preamble_length", 4), 1, 6, 1, 1)

        # Row 7: Path Hash Mode (multi-byte)
        grid.attach(self._grid_label("Path Hash"), 0, 7, 1, 1)
        self._path_hash_combo = Gtk.ComboBoxText.new()
        self._path_hash_combo.append("0", "1-byte (64 hops)")
        self._path_hash_combo.append("1", "2-byte (32 hops)")
        self._path_hash_combo.append("2", "3-byte (21 hops)")
        self._path_hash_combo.set_active_id("0")
        grid.attach(self._path_hash_combo, 1, 7, 3, 1)

        # Row 8: Region scope for outgoing flood packets (issue #90)
        grid.attach(self._grid_label("Region Scope"), 0, 8, 1, 1)
        region_entry = self._grid_entry("flood_region", 14)
        region_entry.set_placeholder_text("unscoped")
        region_entry.set_tooltip_text(
            "Region scope for outgoing flood packets, for example #germany. "
            "Repeaters that filter by region only forward packets with a "
            "matching scope. Leave empty to send unscoped floods."
        )
        grid.attach(region_entry, 1, 8, 2, 1)

        panel.append(grid)
        return panel

    def _build_hardware_panel(self) -> Gtk.Box:
        panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        panel.add_css_class("panel-card")
        panel.set_valign(Gtk.Align.START)

        title = Gtk.Label(label="Hardware")
        title.add_css_class("panel-title")
        title.set_halign(Gtk.Align.START)
        panel.append(title)

        # Hardware preset dropdown
        preset_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        preset_label = Gtk.Label(label="Board Preset")
        preset_label.add_css_class("panel-muted")
        preset_box.append(preset_label)
        self._hw_preset = Gtk.ComboBoxText.new()
        self._hw_preset.append("uconsole", "uConsole (AIO)")
        self._hw_preset.append("hg-aiov2", "uConsole HG AIOv2")
        self._hw_preset.append("waveshare", "Waveshare")
        self._hw_preset.append("meshadv-mini", "meshadv-mini")
        self._hw_preset.append("custom", "Custom")
        self._hw_preset.set_active_id("uconsole")
        self._hw_preset.connect("changed", self._on_hw_preset_changed)
        preset_box.append(self._hw_preset)
        panel.append(preset_box)

        subtitle = Gtk.Label(label="SPI and GPIO pin configuration")
        subtitle.add_css_class("panel-muted")
        subtitle.set_halign(Gtk.Align.START)
        panel.append(subtitle)

        grid = Gtk.Grid()
        grid.set_row_spacing(8)
        grid.set_column_spacing(8)

        # SPI Bus row
        grid.attach(self._grid_label("SPI Bus"), 0, 0, 1, 1)
        grid.attach(self._grid_entry("bus_id", 3), 1, 0, 1, 1)
        grid.attach(self._grid_label("CS ID"), 2, 0, 1, 1)
        grid.attach(self._grid_entry("cs_id", 3), 3, 0, 1, 1)
        grid.attach(self._grid_label("CS Pin"), 4, 0, 1, 1)
        grid.attach(self._grid_entry("cs_pin", 3), 5, 0, 1, 1)

        # GPIO Pins row 1
        grid.attach(self._grid_label("Reset"), 0, 1, 1, 1)
        grid.attach(self._grid_entry("reset_pin", 3), 1, 1, 1, 1)
        grid.attach(self._grid_label("Busy"), 2, 1, 1, 1)
        grid.attach(self._grid_entry("busy_pin", 3), 3, 1, 1, 1)
        grid.attach(self._grid_label("IRQ"), 4, 1, 1, 1)
        grid.attach(self._grid_entry("irq_pin", 3), 5, 1, 1, 1)

        # GPIO Pins row 2
        grid.attach(self._grid_label("TXEN"), 0, 2, 1, 1)
        grid.attach(self._grid_entry("txen_pin", 3), 1, 2, 1, 1)
        grid.attach(self._grid_label("RXEN"), 2, 2, 1, 1)
        grid.attach(self._grid_entry("rxen_pin", 3), 3, 2, 1, 1)

        # Mode switches row
        grid.attach(self._grid_label("Waveshare"), 0, 3, 1, 1)
        grid.attach(self._grid_switch("is_waveshare"), 1, 3, 1, 1)
        grid.attach(self._grid_label("DIO2 RF"), 2, 3, 1, 1)
        grid.attach(self._grid_switch("use_dio2_rf"), 3, 3, 1, 1)
        grid.attach(self._grid_label("DIO3 TCXO"), 4, 3, 1, 1)
        grid.attach(self._grid_switch("use_dio3_tcxo"), 5, 3, 1, 1)

        # GPIO chip / backend row — the header is on gpiochip0 for CM4 but
        # gpiochip15 on CM5 and Pi 5 kernels (#85)
        grid.attach(self._grid_label("GPIO Chip"), 0, 4, 1, 1)
        grid.attach(self._grid_entry("gpio_chip", 3), 1, 4, 1, 1)
        grid.attach(self._grid_label("EN Pins"), 2, 4, 1, 1)
        grid.attach(self._grid_entry("en_pins", 7), 3, 4, 1, 1)
        grid.attach(self._grid_label("Poll IRQ"), 4, 4, 1, 1)
        grid.attach(self._grid_switch("use_gpiod_backend"), 5, 4, 1, 1)

        panel.append(grid)

        hint = Gtk.Label(label=self._gpio_chip_hint())
        hint.add_css_class("panel-muted")
        hint.set_halign(Gtk.Align.START)
        hint.set_wrap(True)
        hint.set_max_width_chars(48)
        hint.set_size_request(360, -1)
        panel.append(hint)

        return panel

    @staticmethod
    def _gpio_chip_hint() -> str:
        """Show which GPIO chips this host actually has, to make #85 self-service."""
        from meshcore_console.platform.conflicts import available_gpio_chips

        found = available_gpio_chips()
        available = ", ".join(str(c) for c in found) if found else "none found"
        lines = [
            f"GPIO chips on this host: {available}.",
            "EN Pins is a comma-separated list of pins driven HIGH at init, "
            "blank if unused. The HackerGadgets AIOv2 LoRa enable pin is 27.",
            "Poll IRQ swaps edge interrupts for a polling thread, for kernels "
            "that reject edge requests.",
        ]

        # An env var wins over these fields, so say so. Otherwise an edit here
        # looks like it does nothing (#85).
        overrides = sorted(set(hardware_env_overrides().values()))
        if overrides:
            names = ", ".join(overrides)
            verb = "is" if len(overrides) == 1 else "are"
            lines.append(
                f"Note: {names} {verb} set in the environment. The environment "
                f"overrides the values saved here."
            )
        return " ".join(lines)

    def _build_logging_panel(self) -> Gtk.Box:
        panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        panel.add_css_class("panel-card")
        panel.set_valign(Gtk.Align.START)

        title = Gtk.Label(label="Logging")
        title.add_css_class("panel-title")
        title.set_halign(Gtk.Align.START)
        panel.append(title)

        grid = Gtk.Grid()
        grid.set_row_spacing(8)
        grid.set_column_spacing(8)

        # Console log level dropdown
        grid.attach(self._grid_label("Console Level"), 0, 0, 1, 1)
        self._log_level_combo = Gtk.ComboBoxText.new()
        for level in VALID_LEVELS:
            self._log_level_combo.append(level, level)
        self._log_level_combo.set_active_id("INFO")
        self._log_level_combo.connect("changed", self._on_log_level_changed)
        grid.attach(self._log_level_combo, 1, 0, 1, 1)

        # Radio warnings are retained in logs and the header status indicator;
        # this controls only the intrusive bottom-of-window toast alerts.
        grid.attach(self._grid_label("Radio Error Toasts"), 0, 1, 1, 1)
        radio_toast_switch = self._grid_switch("show_radio_error_toasts")
        radio_toast_switch.set_tooltip_text("Show radio warnings as popup alerts")
        grid.attach(radio_toast_switch, 1, 1, 1, 1)

        # Reception noise counters. A weak or distant transmitter causes CRC
        # and header errors, so they are reported here, not as radio errors.
        grid.attach(self._grid_label("RX Noise"), 0, 2, 1, 1)
        self._rf_noise_label = Gtk.Label(label="None")
        self._rf_noise_label.add_css_class("panel-muted")
        self._rf_noise_label.set_halign(Gtk.Align.START)
        self._rf_noise_label.set_tooltip_text(
            "CRC and header errors received since start. Weak, distant, or "
            "colliding transmitters cause these. The radio is still healthy."
        )
        grid.attach(self._rf_noise_label, 1, 2, 1, 1)

        # Export logs button
        export_btn = Gtk.Button.new_with_label("Export Logs")
        export_btn.connect("clicked", self._on_export_logs)
        grid.attach(export_btn, 0, 3, 2, 1)

        panel.append(grid)
        panel.connect("map", self._on_logging_panel_map)
        panel.connect("unmap", self._on_logging_panel_unmap)
        return panel

    def _build_repeaters_panel(self) -> Gtk.Box:
        panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        panel.add_css_class("panel-card")
        panel.set_valign(Gtk.Align.START)

        title = Gtk.Label(label="Repeaters")
        title.add_css_class("panel-title")
        title.set_halign(Gtk.Align.START)
        panel.append(title)

        grid = Gtk.Grid()
        grid.set_row_spacing(8)
        grid.set_column_spacing(8)
        grid.attach(self._grid_label("Remove After"), 0, 0, 1, 1)
        self._repeater_expiry_combo = Gtk.ComboBoxText.new()
        for key, (label, _seconds) in REPEATER_EXPIRY.items():
            self._repeater_expiry_combo.append(key, label)
        self._repeater_expiry_combo.set_active_id("never")
        self._repeater_expiry_combo.set_tooltip_text(
            "Repeaters whose last advert is older than this are removed from the "
            "list. Favourites (\u2605) are always kept."
        )
        grid.attach(self._repeater_expiry_combo, 1, 0, 1, 1)
        panel.append(grid)
        return panel

    # --- [airtime] Scheduled transmissions ------------------------------------------
    def _build_schedule_panel(self) -> Gtk.Box:
        panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        panel.add_css_class("panel-card")
        panel.set_valign(Gtk.Align.START)
        title = Gtk.Label(label="Scheduled transmissions")
        title.add_css_class("panel-title")
        title.set_halign(Gtk.Align.START)
        panel.append(title)
        note = Gtk.Label(
            label="Auto-polls and auto-traces transmit on a timer. These stop all of them at once; "
            "nothing is sent."
        )
        note.add_css_class("panel-muted")
        note.set_wrap(True)
        note.set_halign(Gtk.Align.START)
        note.set_size_request(300, -1)  # GTK4 sizing: a wrapped label needs a width
        panel.append(note)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        polls = Gtk.Button.new_with_label("Disable all auto polling")
        polls.set_tooltip_text(
            "Pause every repeater's auto-poll (intervals are kept; choose one to resume)"
        )
        polls.connect("clicked", self._on_disable_polls)
        traces = Gtk.Button.new_with_label("Disable all traces")
        traces.set_tooltip_text("Turn every repeater's auto-trace off")
        traces.connect("clicked", self._on_disable_traces)
        row.append(polls)
        row.append(traces)
        panel.append(row)
        self._schedule_note = Gtk.Label(label="")
        self._schedule_note.add_css_class("panel-muted")
        self._schedule_note.set_halign(Gtk.Align.START)
        panel.append(self._schedule_note)
        self._refresh_schedule_note()
        return panel

    def _refresh_schedule_note(self) -> None:
        from meshcore_console.meshcore import schedule_store

        polls = sum(
            1
            for k, v in schedule_store.poll_intervals().items()
            if v and k not in schedule_store.paused_polls()
        )
        traces = sum(1 for v in schedule_store.trace_intervals().values() if v)
        self._schedule_note.set_text(
            f"Now: {polls} auto-poll{'s' if polls != 1 else ''}, "
            f"{traces} auto-trace{'s' if traces != 1 else ''} active."
        )

    def _schedule_log(self, keys: list[str], text: str) -> None:
        from meshcore_console.meshcore.activity_log import ACTIVITY

        names = {(p.public_key or p.peer_id): p.display_name for p in self._service.list_peers()}
        for key in keys:
            ACTIVITY.add(key, names.get(key, key[:8]), "Setting", text, None)

    def _on_disable_polls(self, _button: Gtk.Button) -> None:
        from meshcore_console.meshcore import schedule_store

        keys = schedule_store.pause_all_polls()
        self._schedule_log(keys, "Auto-poll paused (Settings: disable all)")
        self._status_label.set_text(f"Auto polling paused for {len(keys)} repeater(s)")
        self._refresh_schedule_note()

    def _on_disable_traces(self, _button: Gtk.Button) -> None:
        from meshcore_console.meshcore import schedule_store

        keys = schedule_store.clear_traces()
        self._schedule_log(keys, "Auto-trace off (Settings: disable all)")
        self._status_label.set_text(f"Auto-trace off for {len(keys)} repeater(s)")
        self._refresh_schedule_note()

    def _on_logging_panel_map(self, _panel: Gtk.Widget) -> None:
        self._refresh_rf_noise()
        if not self._rf_noise_timer_id:
            self._rf_noise_timer_id = GLib.timeout_add_seconds(2, self._on_rf_noise_tick)

    def _on_logging_panel_unmap(self, _panel: Gtk.Widget) -> None:
        if self._rf_noise_timer_id:
            GLib.source_remove(self._rf_noise_timer_id)
            self._rf_noise_timer_id = 0

    def _on_rf_noise_tick(self) -> bool:
        self._refresh_rf_noise()
        return True  # GLib.SOURCE_CONTINUE

    def _refresh_rf_noise(self) -> None:
        counts = get_rf_noise_counts()
        if not counts:
            self._rf_noise_label.set_text("None")
            return
        self._rf_noise_label.set_text(
            "   ".join(f"{name} {count}" for name, count in sorted(counts.items()))
        )

    def _on_log_level_changed(self, combo: Gtk.ComboBoxText) -> None:
        level = combo.get_active_id()
        logger.debug("UI: log level changed to %s", level)
        if level:
            set_stderr_level(level)

    def _on_export_logs(self, _button: Gtk.Button) -> None:
        logger.debug("UI: export logs button clicked")
        from datetime import datetime, timezone

        ts = datetime.now(tz=timezone.utc).strftime("%Y%m%d-%H%M%S")
        dialog = Gtk.FileDialog()
        dialog.set_title("Export Logs")
        dialog.set_initial_name(f"meshcore-logs-{ts}.txt")
        txt_filter = Gtk.FileFilter()
        txt_filter.set_name("Text files")
        txt_filter.add_pattern("*.txt")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(txt_filter)
        dialog.set_filters(filters)

        window = self.get_root()
        parent = window if isinstance(window, Gtk.Window) else None
        dialog.save(parent, None, self._on_export_save_done)

    def _on_export_save_done(self, dialog: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
        try:
            gfile = dialog.save_finish(result)
        except Exception:  # noqa: BLE001
            return  # user cancelled
        dest = gfile.get_path()
        if not dest:
            return
        try:
            export_logs_to_path(dest)
            self._status_label.set_text(f"Logs exported to {dest}")
        except Exception as exc:  # noqa: BLE001
            logger.error("Export failed: %s", exc)
            self._status_label.set_text(f"Export failed: {exc}")

    def _grid_label(self, text: str) -> Gtk.Label:
        label = Gtk.Label(label=text)
        label.add_css_class("panel-muted")
        label.set_halign(Gtk.Align.END)
        label.set_valign(Gtk.Align.CENTER)
        return label

    def _grid_entry(self, key: str, width_chars: int) -> Gtk.Entry:
        entry = Gtk.Entry.new()
        entry.set_width_chars(width_chars)
        entry.set_max_width_chars(width_chars)
        entry.set_halign(Gtk.Align.START)
        self._entries[key] = entry
        return entry

    def _grid_switch(self, key: str) -> Gtk.Switch:
        switch = Gtk.Switch.new()
        switch.set_halign(Gtk.Align.START)
        switch.set_valign(Gtk.Align.CENTER)
        self._switches[key] = switch
        return switch

    def get_default_focus(self) -> Gtk.Widget:
        """Return the widget that should receive focus when this view is shown."""
        return self._entries.get("node_name", self)

    def refresh_public_key(self) -> None:
        """Re-read the public key from the service and update the display."""
        public_key = self._service.get_self_public_key()
        if self._public_key_label:
            self._public_key_label.set_text(_abbreviate_key(public_key))

    def _on_show_qr(self, _button: Gtk.Button) -> None:
        logger.debug("UI: show QR button clicked")
        """Show QR code dialog."""
        node_name = self._entries["node_name"].get_text().strip()
        if not node_name:
            node_name = self._service.get_settings().node_name
        public_key = self._service.get_self_public_key()

        window = self.get_root()
        parent = window if isinstance(window, Gtk.Window) else None
        dialog = QrCodeDialog(parent, node_name, public_key)
        dialog.present()

    def _on_preset_changed(self, _combo: Gtk.ComboBoxText) -> None:
        logger.debug("UI: radio preset changed")
        preset = self._preset.get_active_id() or "custom"
        if preset == "custom":
            return
        try:
            current = self._collect_settings(allow_partial=True)
        except ValueError:
            # Fields contain invalid text; fall back to persisted settings
            current = self._service.get_settings()
        updated = apply_preset(current, preset)
        self._set_entry_float("frequency", updated.frequency / 1_000_000)
        self._set_entry_float("bandwidth", updated.bandwidth / 1_000)
        self._set_entry_int("spreading_factor", updated.spreading_factor)
        self._set_entry_int("coding_rate", updated.coding_rate)
        self._set_entry_int("preamble_length", updated.preamble_length)
        self._set_entry_int("tx_power", updated.tx_power)

    def _on_hw_preset_changed(self, _combo: Gtk.ComboBoxText) -> None:
        logger.debug("UI: hardware preset changed")
        preset = self._hw_preset.get_active_id() or "custom"
        if preset == "custom":
            return
        try:
            current = self._collect_settings(allow_partial=True)
        except ValueError:
            current = self._service.get_settings()
        updated = apply_hardware_preset(current, preset)
        for key in (
            "bus_id",
            "cs_id",
            "cs_pin",
            "reset_pin",
            "busy_pin",
            "irq_pin",
            "txen_pin",
            "rxen_pin",
        ):
            self._set_entry_int(key, getattr(updated, key))
        # The enable pin belongs to the board, so the preset owns it too. Write
        # it back to the entry, because _collect_settings reads the entry (#85).
        self._set_entry("en_pins", updated.en_pins)
        self._set_switch("is_waveshare", updated.is_waveshare)
        self._set_switch("use_dio2_rf", updated.use_dio2_rf)
        self._set_switch("use_dio3_tcxo", updated.use_dio3_tcxo)

    def _on_close_settings(self, _button=None) -> None:  # [settings-close patch]
        """Back to the screen Settings was opened from. Unsaved edits are dropped (after
        asking): left in the form, a stray edit used to be saved later by accident."""
        from dataclasses import asdict

        try:
            dirty = asdict(self._collect_settings(allow_partial=True)) != asdict(
                self._service.get_settings()
            )
        except Exception:  # noqa: BLE001 - unparsable text in a field is an unsaved change too
            dirty = True
        if not dirty:
            self._leave_settings()
            return
        from gi.repository import Adw

        from meshcore_console.ui_gtk.compat import build_alert_dialog, present_dialog

        dialog = build_alert_dialog(
            self.get_root(), "Discard unsaved changes?", "Some settings were changed but not saved."
        )
        dialog.add_response("cancel", "Keep editing")
        dialog.add_response("discard", "Discard")
        dialog.set_response_appearance("discard", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", self._on_close_response)
        present_dialog(dialog, self.get_root())

    def _on_close_response(self, _dialog, response: str) -> None:
        if response == "discard":
            self._leave_settings()

    def _leave_settings(self) -> None:
        self._load_from_service()  # the form always matches what is saved
        self._status_label.set_text("")
        root = self.get_root()
        if root is not None and hasattr(root, "close_settings"):
            root.close_settings()

    def _on_reload(self, _button: Gtk.Button) -> None:
        logger.debug("UI: reload settings button clicked")
        self._load_from_service()
        self._status_label.set_text("Reloaded from persisted settings.")

    def _on_save(self, _button: Gtk.Button) -> None:
        logger.debug("UI: save settings button clicked")
        try:
            settings = self._collect_settings()
        except ValueError as exc:
            logger.warning("Settings validation error: %s", exc)
            self._status_label.set_text(f"Invalid: {exc}")
            return

        changes = _radio_changes(self._service.get_settings(), settings)  # [settings-guard patch]
        if changes and not getattr(self, "_radio_change_confirmed", False):
            self._confirm_radio_changes(settings, changes)
            return
        self._radio_change_confirmed = False
        try:
            self._service.update_settings(settings)
        except Exception as exc:  # noqa: BLE001
            logger.error("Settings save failed: %s", exc)
            self._status_label.set_text(f"Save failed: {exc}")
            return
        if changes:
            logger.warning("Radio settings changed: %s", "; ".join(changes))

        logger.debug("UI: settings saved successfully")
        if self._service.get_status().connected:
            self._status_label.set_text("Saved. Restart the app to apply radio changes.")
        else:
            self._status_label.set_text("Settings saved.")

    def _confirm_radio_changes(
        self, settings, changes: list[str]
    ) -> None:  # [settings-guard patch]
        from gi.repository import Adw

        from meshcore_console.ui_gtk.compat import build_alert_dialog, present_dialog

        body = (
            "These radio settings will change:\n\n"
            + "\n".join(f"\u2022 {c}" for c in changes)
            + "\n\nA wrong frequency, bandwidth, SF or CR takes you off the mesh (you can still "
            "hear nothing and nobody hears you). Save anyway?"
        )
        dialog = build_alert_dialog(self.get_root(), "Change radio settings?", body)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("revert", "Discard these changes")
        dialog.add_response("save", "Save")
        dialog.set_response_appearance("save", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", self._on_radio_confirm, settings)
        present_dialog(dialog, self.get_root())

    def _on_radio_confirm(self, _dialog, response: str, _settings) -> None:
        if response == "save":
            self._radio_change_confirmed = True
            self._on_save(None)
        elif response == "revert":
            self._load_from_service()
            self._status_label.set_text(
                "Radio changes discarded; form reloaded from saved settings."
            )
        else:
            self._status_label.set_text("Not saved.")

    def _load_from_service(self) -> None:
        settings = self._service.get_settings()

        # Public Info
        self._set_entry("node_name", settings.node_name)
        self._set_entry_float("latitude", settings.latitude)
        self._set_entry_float("longitude", settings.longitude)
        self._set_switch("share_position", settings.share_position)
        self._set_switch("allow_telemetry", settings.allow_telemetry)
        self._set_switch("telemetry_favorites_only", settings.telemetry_favorites_only)
        self._set_switch("autoconnect", settings.autoconnect)
        self._set_entry("gps_device", settings.gps_device)

        # Update public key display
        public_key = self._service.get_self_public_key()
        if self._public_key_label:
            self._public_key_label.set_text(_abbreviate_key(public_key))

        # Radio
        self._preset.set_active_id(settings.radio_preset)
        self._set_entry_float("frequency", settings.frequency / 1_000_000)
        self._set_entry_float("bandwidth", settings.bandwidth / 1_000)
        self._set_entry_int("spreading_factor", settings.spreading_factor)
        self._set_entry_int("coding_rate", settings.coding_rate)
        self._set_entry_int("tx_power", settings.tx_power)
        self._set_entry_int("preamble_length", settings.preamble_length)

        self._path_hash_combo.set_active_id(str(settings.path_hash_mode))
        self._set_entry("flood_region", settings.flood_region)

        # Hardware
        self._hw_preset.set_active_id(settings.hardware_preset)
        for key in (
            "bus_id",
            "cs_id",
            "cs_pin",
            "reset_pin",
            "busy_pin",
            "irq_pin",
            "txen_pin",
            "rxen_pin",
            "gpio_chip",
        ):
            self._set_entry_int(key, getattr(settings, key))
        self._set_entry("en_pins", settings.en_pins)
        self._set_switch("is_waveshare", settings.is_waveshare)
        self._set_switch("use_dio2_rf", settings.use_dio2_rf)
        self._set_switch("use_dio3_tcxo", settings.use_dio3_tcxo)
        self._set_switch("use_gpiod_backend", settings.use_gpiod_backend)

        # Logging
        self._log_level_combo.set_active_id(settings.log_level)
        self._set_switch("show_radio_error_toasts", settings.show_radio_error_toasts)

        # Repeaters
        self._repeater_expiry_combo.set_active_id(
            settings.repeater_expiry if settings.repeater_expiry in REPEATER_EXPIRY else "never"
        )

    def _collect_settings(self, allow_partial: bool = False) -> MeshcoreSettings:
        current = self._service.get_settings()
        out = current.clone()

        # Public Info
        out.node_name = self._entries["node_name"].get_text().strip() or current.node_name
        out.latitude = self._parse_float("latitude", allow_partial) or current.latitude
        out.longitude = self._parse_float("longitude", allow_partial) or current.longitude
        out.share_position = self._switches["share_position"].get_active()
        out.allow_telemetry = self._switches["allow_telemetry"].get_active()
        out.telemetry_favorites_only = self._switches["telemetry_favorites_only"].get_active()
        out.autoconnect = self._switches["autoconnect"].get_active()
        out.gps_device = self._entries["gps_device"].get_text().strip()

        # Radio
        out.radio_preset = self._preset.get_active_id() or "custom"
        freq = self._parse_float("frequency", allow_partial)
        if freq is not None:
            out.frequency = int(freq * 1_000_000)
        bw = self._parse_float("bandwidth", allow_partial)
        if bw is not None:
            out.bandwidth = int(bw * 1_000)

        path_hash_id = self._path_hash_combo.get_active_id()
        out.path_hash_mode = int(path_hash_id) if path_hash_id else 0

        # Normalizing raises ValueError for a name openhop_core cannot hash,
        # which _on_save reports instead of persisting a scope that would
        # break every send.
        out.flood_region = normalize_region_scope(self._entries["flood_region"].get_text()) or ""

        for key in (
            "spreading_factor",
            "coding_rate",
            "tx_power",
            "preamble_length",
            "bus_id",
            "cs_id",
            "cs_pin",
            "reset_pin",
            "busy_pin",
            "irq_pin",
            "txen_pin",
            "rxen_pin",
            "gpio_chip",
        ):
            val = self._parse_int(key, allow_partial)
            if val is not None:
                setattr(out, key, val)

        # Hardware
        out.hardware_preset = self._hw_preset.get_active_id() or "custom"
        out.is_waveshare = self._switches["is_waveshare"].get_active()
        out.use_dio2_rf = self._switches["use_dio2_rf"].get_active()
        out.use_dio3_tcxo = self._switches["use_dio3_tcxo"].get_active()
        out.use_gpiod_backend = self._switches["use_gpiod_backend"].get_active()
        out.en_pins = ",".join(str(p) for p in parse_pin_list(self._entries["en_pins"].get_text()))

        # Logging
        out.log_level = self._log_level_combo.get_active_id() or "INFO"
        out.show_radio_error_toasts = self._switches["show_radio_error_toasts"].get_active()

        # Repeaters
        out.repeater_expiry = self._repeater_expiry_combo.get_active_id() or "never"

        return out

    def _parse_float(self, key: str, allow_partial: bool) -> float | None:
        text = self._entries[key].get_text().strip()
        if not text:
            if allow_partial:
                return None
            raise ValueError(f"{key} cannot be empty")
        try:
            return float(text)
        except ValueError as exc:
            raise ValueError(f"{key} must be a number") from exc

    def _parse_int(self, key: str, allow_partial: bool) -> int | None:
        text = self._entries[key].get_text().strip()
        if not text:
            if allow_partial:
                return None
            raise ValueError(f"{key} cannot be empty")
        try:
            return int(text)
        except ValueError as exc:
            raise ValueError(f"{key} must be an integer") from exc

    def _set_entry(self, key: str, value: str) -> None:
        self._entries[key].set_text(value)

    def _set_entry_int(self, key: str, value: int) -> None:
        self._entries[key].set_text(str(value))

    def _set_entry_float(self, key: str, value: float) -> None:
        formatted = f"{value:.6f}".rstrip("0").rstrip(".")
        self._entries[key].set_text(formatted)

    def _set_switch(self, key: str, value: bool) -> None:
        self._switches[key].set_active(bool(value))
