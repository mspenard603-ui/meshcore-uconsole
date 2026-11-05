"""Repeater CLI command reference for the Admin console '?' button. [cli-help patch]

Taken from the MeshCore firmware source (simple_repeater + CommonCLI +
CommonRadioPrefs, v1.18.0). Older firmware lacks some commands; the repeater
answers those with 'unknown config' or '??'. Commands marked serial-only are
refused over the mesh (the firmware checks sender_timestamp == 0).

CLI commands are only answered for admin logins.
"""

from __future__ import annotations

from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

FIRMWARE_REF = "v1.18.0"

# (section, [(command, description), ...]). {braces} mark a value to fill in.
COMMANDS: list[tuple[str, list[tuple[str, str]]]] = [
    ("Information", [
        ("ver", "Firmware version and build date."),
        ("board", "Board / manufacturer name."),
        ("clock", "The repeater's clock, UTC."),
        ("power", "Battery mV and USB / solar / external / charger state."),
        ("neighbors", "Repeaters heard directly (zero-hop adverts): key prefix, seconds ago, SNR."),
        ("get public.key", "Full public key."),
        ("get role", "Node role (repeater, room server, ...)."),
        ("get bootloader.ver", "Bootloader version (nRF52 boards only)."),
        ("get pwrmgt.bootreason", "Last reset and shutdown reason."),
        ("get pwrmgt.source", "external or battery (nRF52 power management)."),
        ("get pwrmgt.bootmv", "Battery voltage at boot (nRF52 power management)."),
    ]),
    ("Actions", [
        ("advert", "Send a flood advert now."),
        ("advert.zerohop", "Send a zero-hop advert (direct neighbours only, not relayed)."),
        ("discover.neighbors", "Send a node-discover request to nearby nodes."),
        ("clock sync", "Set the clock from the timestamp on this command. Forward only."),
        ("time {epoch-seconds}", "Set the clock to a Unix time. Forward only."),
        ("clear stats", "Reset the packet / airtime counters."),
        ("neighbor.remove {pubkey-hex}", "Drop a node from the neighbour table (a prefix is enough)."),
        ("reboot", "Restart the repeater."),
        ("clkreboot", "Reset the clock to 15 May 2024 and reboot. Use when the clock was set in the future."),
        ("poweroff", "Turn the repeater off (also 'shutdown'). Needs someone on site to turn it back on."),
        ("start ota", "Start an over-the-air firmware update (board dependent)."),
    ]),
    ("Radio", [
        ("get radio", "freq,bw,sf,cr"),
        ("set radio {freq},{bw},{sf},{cr}", "MHz, kHz, SF 5-12, CR 5-8. Applies after reboot."),
        ("tempradio {freq},{bw},{sf},{cr},{minutes}", "Use these radio settings for N minutes, then revert."),
        ("get freq", "Frequency, MHz."),
        ("get tx", "TX power, dBm."),
        ("set tx {dBm}", "TX power. Applies immediately."),
        ("get af", "Airtime factor."),
        ("set af {0-9}", "Airtime factor: wait af x (last TX airtime) before sending again."),
        ("get dutycycle", "Duty cycle implied by af: 100 / (af + 1) %."),
        ("set dutycycle {1-100}", "Set af from a duty cycle in %."),
        ("get rxdelay", "RX delay base."),
        ("set rxdelay {0-20}", "Delay before re-flooding, weighted by received signal (0 = off)."),
        ("get txdelay", "Flood TX delay factor."),
        ("set txdelay {0-2}", "Random delay factor before relaying floods."),
        ("get direct.txdelay", "Direct TX delay factor."),
        ("set direct.txdelay {0-2}", "Random delay factor before relaying direct packets."),
        ("get int.thresh", "Interference threshold (0 = off)."),
        ("set int.thresh {n}", "Interference threshold (0 = off)."),
        ("get cad", "Channel activity detection before TX: on/off."),
        ("set cad {on|off}", "Channel activity detection before TX."),
        ("get radio.rxgain", "Boosted RX gain: on/off."),
        ("set radio.rxgain {on|off}", "Boosted RX gain (radios that support it)."),
        ("get agc.reset.interval", "AGC reset interval, seconds."),
        ("set agc.reset.interval {seconds}", "0-1020 s, rounded to a multiple of 4 (0 = off)."),
        ("get path.hash.mode", "Path hash width on packets this repeater sends."),
        ("set path.hash.mode {0|1|2}", "0 = 1-byte, 1 = 2-byte, 2 = 3-byte path hashes on its own adverts/floods."),
        ("get multi.acks", "Extra ACK copies sent."),
        ("set multi.acks {n}", "Extra ACK copies sent."),
    ]),
    ("Repeating", [
        ("get repeat", "Forwarding on/off."),
        ("set repeat {on|off}", "Turn forwarding on or off."),
        ("get flood.max", "Max hops of flood packets it will forward."),
        ("set flood.max {0-64}", "Max hops of flood packets it will forward."),
        ("get flood.max.advert", "Max hops for forwarded flood adverts."),
        ("set flood.max.advert {0-64}", "Max hops for forwarded flood adverts."),
        ("get flood.max.unscoped", "Max hops for floods with no region scope."),
        ("set flood.max.unscoped {0-64}", "Max hops for floods with no region scope."),
        ("get loop.detect", "Loop detection level."),
        ("set loop.detect {off|minimal|moderate|strict}", "Drop flood packets whose path loops back through it."),
    ]),
    ("Regions (flood scope)", [
        ("region", "The region table."),
        ("region get {name}", "One region, its parent, F = flood allowed."),
        ("region put {name} {parent}", "Add a region (parent optional, default *). Flood allowed."),
        ("region remove {name}", "Remove an empty region."),
        ("region allowf {name}", "Allow floods scoped to this region."),
        ("region denyf {name}", "Deny floods scoped to this region."),
        ("region list {allowed|denied}", "Region names by flood setting."),
        ("region home", "Show the home region."),
        ("region home {name}", "Set the home region."),
        ("region default", "Show the default scope for its own floods."),
        ("region default {name}", "Set the default scope."),
        ("region def {name|jump ...}", "Define several regions in one line."),
        ("region save", "Save the region table."),
        ("region load", "Reload the region table."),
    ]),
    ("Identity and adverts", [
        ("get name", "Node name."),
        ("set name {name}", "Node name."),
        ("get lat", "Latitude in adverts."),
        ("set lat {degrees}", "Latitude in adverts (decimal degrees)."),
        ("get lon", "Longitude in adverts."),
        ("set lon {degrees}", "Longitude in adverts (decimal degrees)."),
        ("get advert.interval", "Zero-hop advert interval, minutes."),
        ("set advert.interval {60-240}", "Zero-hop advert interval, minutes (0 = off)."),
        ("get flood.advert.interval", "Flood advert interval, hours."),
        ("set flood.advert.interval {3-168}", "Flood advert interval, hours (0 = off)."),
        ("get owner.info", "Owner info text."),
        ("set owner.info {text}", "Owner info; '|' starts a new line. Readable by guests."),
        ("set prv.key {hex}", "Replace the private key (new identity) - careful. Applies after reboot."),
    ]),
    ("Access", [
        ("password {new}", "Change the admin password (echoed back)."),
        ("get guest.password", "Guest password."),
        ("set guest.password {pw}", "Guest password."),
        ("setperm {pubkey-hex} {0-3}", "Set a client's role: 0 guest, 1 read-only, 2 read-write, 3 admin."),
        ("get allow.read.only", "Read-only access setting (room servers)."),
        ("set allow.read.only {on|off}", "Read-only access setting (room servers)."),
    ]),
    ("Power and sensors", [
        ("powersaving", "Power saving on/off."),
        ("powersaving {on|off}", "Power saving (nRF52 immediately, ESP32 after 2 min)."),
        ("get adc.multiplier", "Battery ADC calibration multiplier."),
        ("set adc.multiplier {x}", "Battery ADC calibration (0 = board default)."),
        ("sensor list", "Sensor settings (add a start index to page)."),
        ("sensor get {key}", "One sensor setting."),
        ("sensor set {key} {value}", "Change a sensor setting."),
        ("gps", "GPS state, fix and satellites (GPS boards)."),
        ("gps {on|off}", "GPS on or off."),
        ("gps sync", "Set the clock from GPS."),
        ("gps setloc", "Copy the GPS position to lat/lon."),
        ("gps advert {none|share|prefs}", "Position in adverts: none, live GPS, or lat/lon setting."),
        ("gps diag", "GPS diagnostics."),
    ]),
    ("Logging", [
        ("log start", "Start the packet log."),
        ("log stop", "Stop the packet log."),
        ("log erase", "Erase the packet log."),
    ]),
    ("Serial only (refused over the mesh)", [
        ("log", "Dump the packet log."),
        ("stats-core / stats-radio / stats-packets", "Stats as text. Use the stats button here instead."),
        ("get prv.key", "Private key."),
        ("set freq {MHz}", "Frequency only."),
        ("get acl", "Clients with their permissions."),
        ("erase", "Format the file system."),
    ]),
]

_CSS = b"""
.cli-help-cmd { font-family: "DejaVu Sans Mono", "Liberation Mono", monospace; font-size: 14px; color: #8ec5ff; }
button.cli-help-pick { padding: 2px 6px; min-height: 0; }
button.cli-help-pick:hover .cli-help-cmd { text-decoration: underline; }
.cli-help-section { font-weight: 700; font-size: 15px; margin-top: 8px; }
"""
_css_done = False


def _ensure_css(widget: Gtk.Widget) -> None:
    global _css_done
    if _css_done:
        return
    display = widget.get_display()
    if display is None:
        return
    provider = Gtk.CssProvider()
    provider.load_from_data(_CSS)
    Gtk.StyleContext.add_provider_for_display(display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    _css_done = True


def command_prefix(command: str) -> str:
    """What goes in the command line: up to the first {value}, or the whole command."""
    head = command.split(" / ")[0]
    return head.split("{")[0] if "{" in head else head


def help_text() -> str:
    """Whole reference as plain text (for copying)."""
    lines = [f"MeshCore repeater CLI (firmware {FIRMWARE_REF}); admin login required."]
    for section, rows in COMMANDS:
        lines.append("")
        lines.append(section)
        for cmd, desc in rows:
            lines.append(f"  {cmd:<44} {desc}")
    return "\n".join(lines)


class CliHelpWindow(Gtk.Window):
    """Searchable reference. Clicking a command puts it in the console's command line."""

    def __init__(self, parent: Gtk.Widget | None, on_pick: Callable[[str], None] | None) -> None:
        super().__init__(title="Repeater commands")
        root = parent.get_root() if parent is not None else None
        if isinstance(root, Gtk.Window):
            self.set_transient_for(root)
        self.set_default_size(760, 560)
        self._on_pick = on_pick
        self._rows: list[tuple[Gtk.Widget, str, Gtk.Label]] = []  # (row, search text, section header)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_start(10)
        box.set_margin_end(10)
        box.set_margin_top(10)
        box.set_margin_bottom(10)

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._search = Gtk.SearchEntry()
        self._search.set_placeholder_text("Filter commands")
        self._search.set_hexpand(True)
        self._search.connect("search-changed", self._on_filter)
        top.append(self._search)
        copy_btn = Gtk.Button.new_with_label("Copy all")
        copy_btn.add_css_class("quick-cmd-btn")
        copy_btn.set_tooltip_text("Copy the whole reference as text")
        copy_btn.connect("clicked", self._on_copy)
        top.append(copy_btn)
        box.append(top)

        note = Gtk.Label(
            label=f"From MeshCore firmware {FIRMWARE_REF} source. Admin login required; older firmware "
                  "lacks some commands. {braces} = value to fill in."
                  + (" Click a command to put it in the command line." if on_pick else "")
        )
        note.add_css_class("panel-muted")
        note.set_wrap(True)
        note.set_xalign(0.0)
        box.append(note)

        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        scroll.set_child(self._list)
        box.append(scroll)
        self.set_child(box)
        _ensure_css(self)

        self._headers: list[Gtk.Label] = []
        for section, rows in COMMANDS:
            header = Gtk.Label(label=section)
            header.add_css_class("cli-help-section")
            header.set_xalign(0.0)
            self._list.append(header)
            self._headers.append(header)
            for cmd, desc in rows:
                self._list.append(self._row(cmd, desc, header, serial="Serial only" in section))

        key = Gtk.EventControllerKey.new()
        key.connect("key-pressed", self._on_key)
        self.add_controller(key)
        self._search.grab_focus()

    def _row(self, cmd: str, desc: str, header: Gtk.Label, serial: bool) -> Gtk.Widget:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        if self._on_pick is not None and not serial:
            btn = Gtk.Button(label=cmd)
            btn.add_css_class("flat")
            btn.add_css_class("cli-help-pick")
            btn.set_tooltip_text(f"Put '{command_prefix(cmd)}' in the command line")
            btn.connect("clicked", lambda _b, c=cmd: self._pick(c))
            lab = btn.get_child()
            target: Gtk.Widget = btn
        else:
            lab = Gtk.Label(label=cmd)
            lab.set_selectable(True)
            target = lab
        if isinstance(lab, Gtk.Label):
            lab.add_css_class("cli-help-cmd")
            lab.set_xalign(0.0)
        target.set_size_request(330, -1)
        target.set_halign(Gtk.Align.START)
        row.append(target)
        d = Gtk.Label(label=desc)
        d.set_wrap(True)
        d.set_xalign(0.0)
        d.set_hexpand(True)
        d.set_selectable(True)
        row.append(d)
        self._rows.append((row, f"{cmd} {desc}".lower(), header))
        return row

    def _pick(self, cmd: str) -> None:
        if self._on_pick is not None:
            self._on_pick(command_prefix(cmd))

    def _on_filter(self, entry: Gtk.SearchEntry) -> None:
        words = entry.get_text().lower().split()
        shown: set[int] = set()
        for row, text, header in self._rows:
            vis = all(w in text for w in words)
            row.set_visible(vis)
            if vis:
                shown.add(id(header))
        for header in self._headers:
            header.set_visible(id(header) in shown)

    def _on_copy(self, _btn: Gtk.Button) -> None:
        from gi.repository import Gdk

        clip = self.get_clipboard()
        clip.set_content(Gdk.ContentProvider.new_for_value(help_text()))

    def _on_key(self, _ctrl, keyval: int, _code: int, _state) -> bool:
        from gi.repository import Gdk

        if keyval == Gdk.KEY_Escape:
            self.close()
            return True
        return False
