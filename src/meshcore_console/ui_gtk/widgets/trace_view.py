"""Table of per-link SNR from a round-trip TRACE. [trace patch]"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import GLib, Gtk

from meshcore_console.ui_gtk.widgets.node_badge import find_peer_for_hop

SNR_LIMIT_DB = {5: -2.5, 6: -5.0, 7: -7.5, 8: -10.0, 9: -12.5, 10: -15.0, 11: -17.5, 12: -20.0}


def _margin_word(margin: float) -> str:
    if margin >= 10:
        return "strong"
    if margin >= 5:
        return "good"
    if margin >= 0:
        return "weak"
    return "below limit"


def build_trace_view(
    result: dict,
    peers: list,
    *,
    target: str,
    self_label: str,
    spreading_factor: int,
    css_class: str | None = None,
    compact: bool = False,
) -> Gtk.Widget:
    """The trace table (see _build_trace_view), with every cell selectable and a
    'Copy table' button that copies it as tab-separated text. [trace-copy patch]"""
    widget = _build_trace_view(result, peers, target=target, self_label=self_label,
                               spreading_factor=spreading_factor, css_class=css_class, compact=compact)
    if not result.get("success") or not result.get("links"):
        return widget  # nothing to copy from a failed trace [trace-copy patch]
    text = trace_text(result, peers, target=target, self_label=self_label, spreading_factor=spreading_factor)
    return _with_copy(widget, text)


def trace_text(result: dict, peers: list, *, target: str, self_label: str, spreading_factor: int) -> str:
    """Plain-text trace: a heading line, then Leg/From/To/SNR/Margin, tab-separated."""
    if not result.get("success"):
        return f"Trace to {target} failed: {result.get('reason', 'no response')}"
    limit = SNR_LIMIT_DB.get(int(spreading_factor), -7.5)
    links = result.get("links") or []

    def name(hop: str) -> str:
        return _hop_label(peers, hop, self_label)[0]

    lines = [
        f"Trace to {target} and back: {len(links)} links, {result.get('rtt_ms', 0)} ms "
        f"(SF{spreading_factor}, decode limit {limit:+.1f} dB)",
        "Leg\tFrom\tTo\tSNR dB\tMargin dB",
    ]
    for i, link in enumerate(links, start=1):
        snr = link.get("snr")
        snr_txt = "n/a" if snr is None else f"{snr:+.2f}"
        margin_txt = "" if snr is None else f"{snr - limit:+.1f}"
        lines.append(f"{i}\t{name(link['from'])}\t{name(link['to'])}\t{snr_txt}\t{margin_txt}")
    note = _ambiguity_note(result, peers, self_label)
    if note:
        lines.append(note)
    return "\n".join(lines)



def _hop_label(peers: list, hop: str, self_label: str) -> tuple[str, list[str]]:
    """Display name for a hop and the other nodes its (short) hash could be.

    'NH-MANCH? (F7)' when F7 is shared by several known nodes. [hop-rank patch]
    """
    if hop == "me":
        return self_label, []
    try:
        from meshcore_console.ui_gtk.widgets.node_badge import peers_for_hop
    except ImportError:  # unpatched node_badge
        peer = find_peer_for_hop(peers, hop)
        return (f"{peer.display_name} ({hop})" if peer else str(hop)), []
    found = peers_for_hop(peers, hop)
    if not found:
        return str(hop), []
    others = [p.display_name for p in found[1:]]
    return f"{found[0].display_name}{'?' if others else ''} ({hop})", others


def _ambiguity_note(result: dict, peers: list, self_label: str) -> str:
    """'F7 could also be HAIRCUT; ...' for hops whose hash several nodes share."""
    notes, seen = [], set()
    for link in result.get("links") or []:
        for hop in (link.get("from"), link.get("to")):
            if hop in seen or hop in (None, "me"):
                continue
            seen.add(hop)
            label, others = _hop_label(peers, hop, self_label)
            if others:
                notes.append(f"{hop} could also be {', '.join(others)}")
    if not notes:
        return ""
    return "'?' = short hash shared by several nodes; the likeliest is shown. " + "; ".join(notes) + "."


def _with_copy(widget: Gtk.Widget, text: str) -> Gtk.Widget:
    def labels(w):
        child = w.get_first_child()
        while child is not None:
            if isinstance(child, Gtk.Label):
                yield child
            yield from labels(child)
            child = child.get_next_sibling()

    for lab in labels(widget):
        lab.set_selectable(True)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
    box.append(widget)
    btn = Gtk.Button(label="Copy table")
    btn.set_halign(Gtk.Align.START)
    btn.set_tooltip_text("Copy this trace as text (tab-separated, full names)")
    btn.connect("clicked", _copy_text, text)
    box.append(btn)
    return box


def _copy_text(btn: Gtk.Button, text: str) -> None:
    from gi.repository import Gdk, GLib

    clipboard = btn.get_clipboard()
    try:
        clipboard.set_content(Gdk.ContentProvider.new_for_value(text))
    except Exception:  # noqa: BLE001 - older bindings
        clipboard.set(text)
    btn.set_label("Copied")
    GLib.timeout_add(1500, lambda: (btn.set_label("Copy table"), False)[1])


def _build_trace_view(
    result: dict,
    peers: list,
    *,
    target: str,
    self_label: str,
    spreading_factor: int,
    css_class: str | None = None,
    compact: bool = False,
) -> Gtk.Widget:
    """Caption + grid (Leg, From, To, SNR, Margin), or a one-line failure.

    *compact* (Map panel): three narrow columns, Link (hash > hash), SNR and
    Margin (coloured), node names in tooltips, and wrapped short text, so the
    narrow details panel does not have to widen. [compact-trace patch]
    """
    if compact:
        return _build_compact(result, peers, target=target, self_label=self_label,
                              spreading_factor=spreading_factor)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)

    def label(text: str, *classes: str, align=Gtk.Align.START) -> Gtk.Label:
        lab = Gtk.Label(label=text)
        for c in classes:
            if c:
                lab.add_css_class(c)
        lab.set_halign(align)
        lab.set_xalign(0.0 if align == Gtk.Align.START else 1.0)
        return lab

    if not result.get("success"):
        box.append(label(f"Trace to {target} failed: {result.get('reason', 'no response')}", "cli-error"))
        return box

    def name(hop: str) -> str:
        return _hop_label(peers, hop, self_label)[0]

    limit = SNR_LIMIT_DB.get(int(spreading_factor), -7.5)
    links = result.get("links") or []
    caption = label(
        f"Trace to {target} and back: {len(links)} links in {result.get('rtt_ms', 0)} ms. "
        f"SNR as measured by the receiving node; margin is above the SF{spreading_factor} "
        f"decode limit ({limit:+.1f} dB).",
        "panel-muted",
    )
    caption.set_wrap(True)
    box.append(caption)

    grid = Gtk.Grid(column_spacing=14, row_spacing=2)
    headers = (
        ("Leg", "Order of the hop on the round trip"),
        ("From", "Transmitting node"),
        ("To", "Receiving node (it measured the SNR)"),
        ("SNR", "Signal-to-noise ratio of this link, in dB"),
        ("Margin", f"How far above the SF{spreading_factor} decode limit of {limit:+.1f} dB"),
    )
    for col, (title, tip) in enumerate(headers):
        h = label(title, "message-detail-header")
        h.set_tooltip_text(tip)
        grid.attach(h, col, 0, 1, 1)
    for row, link in enumerate(links, start=1):
        snr = link.get("snr")
        if snr is None:
            snr_txt, margin_txt = "n/a", ""
        else:
            snr_txt = f"{snr:+.2f} dB"
            margin = snr - limit
            margin_txt = f"{margin:+.1f} dB {_margin_word(margin)}"
        cells = (str(row), name(link["from"]), name(link["to"]), snr_txt, margin_txt)
        for col, text in enumerate(cells):
            align = Gtk.Align.END if col in (0, 3) else Gtk.Align.START
            grid.attach(label(text, css_class or "", align=align), col, row, 1, 1)
    box.append(grid)
    note = _ambiguity_note(result, peers, self_label)
    if note:
        box.append(_wrapped(note, "panel-muted"))
    return box


# -- compact view for the Map panel [compact-trace patch] -------------------------
_CSS = b"""
.trace-compact { font-family: "DejaVu Sans Mono", "Liberation Mono", monospace; font-size: 16px; }
.trace-compact-head { font-size: 15px; font-weight: 700; opacity: 0.85; }
.trace-weak { color: #efb93f; }
.trace-bad { color: #ff7070; }
.trace-target { outline: 2px solid #4ac8ff; outline-offset: 1px; border-radius: 6px; }
.trace-beyond { opacity: 0.4; }
"""
_css_done = False


def _ensure_css() -> None:
    global _css_done
    if _css_done:
        return
    from gi.repository import Gdk

    provider = Gtk.CssProvider()
    provider.load_from_data(_CSS)
    Gtk.StyleContext.add_provider_for_display(
        Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )
    _css_done = True


def _wrapped(text: str, *classes: str) -> Gtk.Label:
    lab = Gtk.Label(label=text)
    for c in classes:
        lab.add_css_class(c)
    lab.set_wrap(True)
    lab.set_max_width_chars(34)  # natural width capped: never widens the panel
    lab.set_xalign(0.0)
    lab.set_halign(Gtk.Align.START)
    return lab


def _build_compact(result: dict, peers: list, *, target: str, self_label: str, spreading_factor: int) -> Gtk.Widget:
    _ensure_css()
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
    if not result.get("success"):
        reason = str(result.get("reason", "no response"))
        if reason.startswith("No trace came back"):
            reason = "no reply within 20 s; a hop may be down or out of range"
        box.append(_wrapped(f"Trace failed: {reason}.", "cli-error"))
        return box

    limit = SNR_LIMIT_DB.get(int(spreading_factor), -7.5)
    links = result.get("links") or []
    me_short = self_label.split("(")[-1].rstrip(")") if "(" in self_label else "me"

    def short(hop: str) -> str:
        if hop == "me":
            return me_short
        return str(hop)[:4].upper() + ("?" if _hop_label(peers, hop, self_label)[1] else "")

    def name(hop: str) -> str:
        return _hop_label(peers, hop, self_label)[0]

    secs = (result.get("rtt_ms") or 0) / 1000
    box.append(_wrapped(f"Round trip, {len(links)} links, {secs:.1f} s. SNR at the receiver; "
                        f"margin over the SF{spreading_factor} limit ({limit:+.1f} dB).", "panel-muted"))
    grid = Gtk.Grid(column_spacing=10, row_spacing=1)
    grid.add_css_class("trace-compact")
    for col, (title, tip) in enumerate((
        ("Link", "Transmitter \u2192 receiver, in round-trip order; hover for names"),
        ("SNR", "Signal-to-noise as measured by the receiving node, dB"),
        ("Margin", f"dB above the SF{spreading_factor} decode limit ({limit:+.1f} dB); "
                   "amber under 5 dB, red below the limit"),
    )):
        h = Gtk.Label(label=title)
        h.add_css_class("trace-compact-head")
        h.set_tooltip_text(tip)
        h.set_xalign(1.0 if col else 0.0)
        h.set_halign(Gtk.Align.END if col else Gtk.Align.START)
        grid.attach(h, col, 0, 1, 1)
    for row, link in enumerate(links, start=1):
        snr = link.get("snr")
        cells = [f"{short(link['from'])}\u2192{short(link['to'])}",
                 "n/a" if snr is None else f"{snr:+.1f}",
                 "" if snr is None else f"{snr - limit:+.1f}"]
        tip = f"{row}. {name(link['from'])} \u2192 {name(link['to'])}"
        for hop in (link["from"], link["to"]):
            others = _hop_label(peers, hop, self_label)[1]
            if others:
                tip += f"\n{hop} could also be: {', '.join(others)}"
        for col, text in enumerate(cells):
            lab = Gtk.Label(label=text)
            lab.set_xalign(1.0 if col else 0.0)
            lab.set_halign(Gtk.Align.END if col else Gtk.Align.START)
            lab.set_tooltip_text(tip)
            if col and snr is not None:
                margin = snr - limit
                if margin < 0:
                    lab.add_css_class("trace-bad")
                elif margin < 5:
                    lab.add_css_class("trace-weak")
            grid.attach(lab, col, row, 1, 1)
    box.append(grid)
    note = _ambiguity_note(result, peers, self_label)
    if note:
        box.append(_wrapped(note, "panel-muted"))
    return box


# -- Pick the repeater a route trace goes out to [hop-target patch] ---------------
# Clicking an orange hop in a route opens its popover with "Trace to here"; the
# next 'Trace route' then goes out to that repeater and back only, so a failing
# full trace can be narrowed down hop by hop. Nothing picked = whole route.
# Handlers find their widgets from the click (no widget references are kept in
# closures, which would leak: see the leak-fix patch).
def _find_desc(widget, cls):
    if isinstance(widget, cls):
        return widget
    child = widget.get_first_child()
    while child is not None:
        found = _find_desc(child, cls)
        if found is not None:
            return found
        child = child.get_next_sibling()
    return None


def path_hop_badges(path) -> list:
    """The repeater badges of a PathVisualization (start and end are you and the sender)."""
    from meshcore_console.ui_gtk.widgets.node_badge import NodeBadge

    badges = []
    child = path.get_first_child()
    while child is not None:
        badge = _find_desc(child, NodeBadge)
        if badge is not None:
            badges.append(badge)
        child = child.get_next_sibling()
    return badges[1:-1]


def _path_of(widget):
    from meshcore_console.ui_gtk.widgets.path_visualization import PathVisualization

    return widget.get_ancestor(PathVisualization)


def _hop_actions(badge) -> list:
    path = _path_of(badge)
    if path is None:
        return []
    badges = path_hop_badges(path)
    if badge not in badges:
        return []
    i = badges.index(badge)
    if getattr(path, "trace_target", None) == i:
        return [("Trace whole route", lambda w: _set_target(w, None))]
    return [(f"Trace to here (hop {i + 1} of {len(badges)})", lambda w, i=i: _set_target(w, i))]


def _set_target(widget, index) -> None:
    path = _path_of(widget)
    if path is None:
        return
    path.trace_target = index
    for j, b in enumerate(path_hop_badges(path)):
        b.remove_css_class("trace-target")
        b.remove_css_class("trace-beyond")
        if index is not None and j == index:
            b.add_css_class("trace-target")
        elif index is not None and j > index:
            b.add_css_class("trace-beyond")
    scope = path.get_parent()
    btn = None
    while scope is not None:
        btn = _find_hop_button(scope)
        if btn is not None:
            break
        scope = scope.get_parent()
    if btn is not None and btn.get_sensitive():
        btn.set_label(trace_button_label(btn))


def _find_hop_button(widget):
    if isinstance(widget, Gtk.Button) and widget.has_css_class("hop-trace-btn"):
        return widget
    child = widget.get_first_child()
    while child is not None:
        found = _find_hop_button(child)
        if found is not None:
            return found
        child = child.get_next_sibling()
    return None


def enable_hop_targets(path) -> None:
    """Give each repeater in *path* a 'Trace to here' popover action."""
    _ensure_css()
    for badge in path_hop_badges(path):
        badge.popover_actions = _hop_actions


def _route_path_near(widget):
    """The PathVisualization shown alongside *widget* (a trace button)."""
    from meshcore_console.ui_gtk.widgets.path_visualization import PathVisualization

    scope = widget.get_parent()
    while scope is not None:
        found = _find_desc(scope, PathVisualization)
        if found is not None:
            return found
        scope = scope.get_parent()
    return None


def hops_to_target(widget, hops: list[str]) -> list[str]:
    """*hops* cut at the repeater picked in the route next to *widget*, if any."""
    path = _route_path_near(widget)
    k = getattr(path, "trace_target", None) if path is not None else None
    if k is None or k >= len(hops):
        return hops
    return hops[: k + 1]


def trace_button_label(btn) -> str:
    path = _route_path_near(btn)
    k = getattr(path, "trace_target", None) if path is not None else None
    if k is None:
        return "Trace route"
    badges = path_hop_badges(path)
    name = badges[k]._prefix if k < len(badges) else "?"
    return f"Trace to {name} (hop {k + 1} of {len(badges)})"


# -- Find the break [find-break patch] ----------------------------------------------
# A TRACE that dies gives no error: some repeater just didn't hear it (or its
# reply). After a failed trace, this re-tests the route and then halves it with
# shorter traces (out to hop k and back) until the first failing link is found.
# Each length gets BREAK_TRIES attempts, since lossy links drop single traces.
BREAK_TRIES = 2


def find_break_widget(service, hops: list[str], source: str) -> Gtk.Widget:
    """'Find the break' button + status, for a trace along *hops* (our side first) that failed."""
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
    btn = Gtk.Button(label="Find the break")
    btn.set_halign(Gtk.Align.START)
    btn.add_css_class("find-break-btn")
    n = len(hops)
    btn.set_tooltip_text(
        "Re-test this route, then trace shorter and shorter parts of it (out to a repeater and back, "
        f"{BREAK_TRIES} tries each) to find the first link that fails. About "
        f"{max(1, n.bit_length())}\u2013{2 * (max(1, n.bit_length()) + 1)} traces of up to 20 s; press again to stop."
    )
    btn.connect("clicked", _on_find_break, service, list(hops), source)
    box.append(btn)
    status = _wrapped("", "panel-muted")
    status.add_css_class("find-break-status")
    status.set_visible(False)
    box.append(status)
    return box


def _break_parts(btn):
    box = btn.get_parent()
    status = None
    child = box.get_first_child() if box is not None else None
    while child is not None:
        if isinstance(child, Gtk.Label) and child.has_css_class("find-break-status"):
            status = child
        child = child.get_next_sibling()
    return box, status


def _on_find_break(btn, service, hops: list[str], source: str) -> None:
    import threading

    state = getattr(btn, "_fb_state", None)
    if state is not None and state.get("running"):
        state["stop"] = True
        btn.set_label("Stopping\u2026")
        btn.set_sensitive(False)
        return
    state = {"running": True, "stop": False}
    btn._fb_state = state
    btn.set_label("Stop")
    box, status = _break_parts(btn)
    child = status.get_next_sibling() if status is not None else None
    while child is not None:  # previous result table
        nxt = child.get_next_sibling()
        box.remove(child)
        child = nxt

    peers = service.list_peers()
    reps = [p for p in peers if p.is_repeater]

    def name(k: int) -> str:
        """Hop k (1-based) as 'Name (HASH)'."""
        if k <= 0:
            return "you"
        hop = str(hops[k - 1]).upper()
        found = find_peer_for_hop(reps, hop)
        return f"{found.display_name} ({hop})" if found is not None else hop

    def post(text: str, final: bool = False, table: dict | None = None) -> None:
        def ui() -> bool:
            if status is None or status.get_root() is None:
                return False  # panel closed or rebuilt
            status.set_text(text)
            status.set_visible(True)
            if table is not None:
                self_key = service.get_self_public_key() or ""
                box.append(build_trace_view(
                    table, service.list_peers(), target=name(len(table.get("_hops", []))),
                    self_label=f"{service.get_status().node_id} ({self_key[:2].upper() or 'me'})",
                    spreading_factor=service.get_settings().spreading_factor, compact=True))
            if final:
                state["running"] = False
                btn.set_label("Find the break")
                btn.set_sensitive(True)
            return False

        GLib.idle_add(ui)

    def trace(k: int) -> dict | None:
        """Up to BREAK_TRIES traces out to hop k; the first success, else the last failure.
        None when stopped or the radio is gone."""
        import time as _time

        from meshcore_console.meshcore.trace import trace_source

        last: dict = {}
        tries = 0
        while tries < BREAK_TRIES:
            if state["stop"]:
                return None
            post(f"Tracing out to {name(k)}, hop {k} of {len(hops)} (try {tries + 1} of {BREAK_TRIES})\u2026")
            with trace_source(source):
                last = service.trace_hops(hops[:k])
            if last.get("busy"):  # another trace (e.g. auto-trace) is running: wait, don't count it
                _time.sleep(3)
                continue
            if "not connected" in str(last.get("reason", "")).lower():
                return None
            tries += 1
            if last.get("success"):
                last["_hops"] = hops[:k]
                return last
        return last

    def work() -> None:
        n = len(hops)
        full = trace(n)
        if full is None:
            post("Stopped." if state["stop"] else "Radio not connected.", final=True)
            return
        if full.get("success"):
            post(f"The whole route worked this time (out to {name(n)} and back): the failure was "
                 "intermittent, not a dead link. Lossy links drop single traces.", final=True, table=full)
            return
        lo, hi, good = 0, n, None  # hop lo is reachable (0 = you), hop hi is not
        while hi - lo > 1:
            mid = (lo + hi) // 2
            res = trace(mid)
            if res is None:
                post("Stopped." if state["stop"] else "Radio not connected.", final=True)
                return
            if res.get("success"):
                lo, good = mid, res
            else:
                hi = mid
        if lo == 0:
            text = (f"Break: the first link, between you and {name(1)}. Nothing came back even from "
                    f"{name(1)} alone ({BREAK_TRIES} tries): it didn't hear you, or you didn't hear its reply.")
        else:
            text = (f"Break: between {name(lo)} (hop {lo}) and {name(lo + 1)} (hop {lo + 1}). "
                    f"Traces out to {name(lo)} come back; one hop further they don't ({BREAK_TRIES} tries), "
                    "so that link fails in one direction or the other. SNR up to the break:")
        post(text, final=True, table=good)

    threading.Thread(target=work, daemon=True).start()
