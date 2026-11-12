"""Admin > Graph tab: repeater RSSI and SNR over time. [repeater-stats patch]

Two stacked charts sharing one time axis (never two y-scales on one chart):
  RSSI (dBm)  - heard by this radio, repeater-reported last RX, noise floor
  SNR (dB)    - heard by this radio, repeater-reported last RX
Hover shows a crosshair with every series' value at that time.

Trace mode [trace-log patch] shows logged round-trip TRACE results instead:
one small panel per link (hop pair), outbound SNR (away from this radio) and
return SNR on the same axis, with the decode limit for the spreading factor as
a dashed reference line. Failed traces are marked on a strip under the panels.

Packets mode [packet-graph patch] shows RX/TX packets and errors per hour,
from the change in the repeater's own counters between stats polls.

Drawing is plain cairo (``draw_graph`` / ``draw_trace_graph``) so it can be
rendered off-screen.
"""

from __future__ import annotations

import math
import time
from datetime import datetime

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import GLib, Gtk

RANGES = (
    ("1 h", 3600),
    ("6 h", 6 * 3600),
    ("24 h", 86400),
    ("7 d", 7 * 86400),
    ("30 d", 30 * 86400),
)

# Colours: app dark theme (tokens.css) + categorical slots 1-2 validated on it.
TEXT = (0.906, 0.933, 0.973)  # mc_text #e7eef8
MUTED = (0.667, 0.725, 0.804)  # mc_text_muted #aab9cd
GRID = (0.180, 0.227, 0.286)  # mc_border #2e3a49
SURFACE = (0.114, 0.145, 0.188)  # mc_surface_alt #1d2530
HEARD = (0.224, 0.529, 0.898)  # #3987e5 blue
REPORTED = (0.851, 0.349, 0.149)  # #d95926 orange
NOISE = MUTED

SERIES = (
    # key, label, colour, panel, field
    ("heard", "Heard by this radio", HEARD),
    ("stats", "Repeater reports (last RX)", REPORTED),
)

PAD_L, PAD_R, PAD_T, PAD_B = 52, 14, 48, 24
MAX_MARKERS = 60  # above this many points a series is drawn as a line only
PANEL_GAP = 30
FONT = 11.0


def _nice_step(span: float, target: int = 4) -> float:
    raw = max(span, 1e-9) / target
    mag = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 2.5, 5, 10):
        if raw <= m * mag:
            return m * mag
    return 10 * mag


def _time_step(span_s: float) -> float:
    for step in (
        900,
        1800,
        3600,
        2 * 3600,
        3 * 3600,
        6 * 3600,
        12 * 3600,
        86400,
        2 * 86400,
        4 * 86400,
        7 * 86400,
    ):
        if span_s / step <= 6:
            return step
    return 14 * 86400


def _fmt_time(ts: float, span_s: float) -> str:
    dt = datetime.fromtimestamp(ts)
    return dt.strftime("%H:%M") if span_s <= 2 * 86400 else dt.strftime("%b %-d")


def _text(
    cr,
    x: float,
    y: float,
    s: str,
    rgb=MUTED,
    align: str = "left",
    size: float = FONT,
    bold: bool = False,
) -> float:
    cr.select_font_face("Sans", 0, 1 if bold else 0)
    cr.set_font_size(size)
    ext = cr.text_extents(s)
    w = ext[4] if isinstance(ext, tuple) else ext.x_advance
    if align == "right":
        x -= w
    elif align == "center":
        x -= w / 2
    cr.set_source_rgb(*rgb)
    cr.move_to(x, y)
    cr.show_text(s)
    return w


def _y_range(values: list[float], min_span: float) -> tuple[float, float]:
    lo, hi = min(values), max(values)
    if hi - lo < min_span:
        mid = (hi + lo) / 2
        lo, hi = mid - min_span / 2, mid + min_span / 2
    step = _nice_step(hi - lo)
    return math.floor(lo / step) * step, math.ceil(hi / step) * step


def _segments(points: list[tuple[float, float]], gap_s: float) -> list[list[tuple[float, float]]]:
    segs: list[list[tuple[float, float]]] = []
    for t, v in points:
        if segs and t - segs[-1][-1][0] <= gap_s:
            segs[-1].append((t, v))
        else:
            segs.append([(t, v)])
    return segs


def draw_graph(cr, width: int, height: int, data: dict, hover_x: float | None = None) -> None:
    """Draw both charts. *data*: {"t0","t1","heard": [...], "stats": [...], "gap": {kind: s}}."""
    t0, t1 = data["t0"], data["t1"]
    span = t1 - t0
    plot_w = width - PAD_L - PAD_R
    panel_h = (height - PAD_T - PAD_B - PANEL_GAP) / 2
    if plot_w < 60 or panel_h < 40:
        return

    def X(t: float) -> float:
        return PAD_L + (t - t0) / span * plot_w

    # Legend (always present: 2+ series)
    lx = PAD_L
    for key, label, rgb in SERIES:
        cr.set_source_rgb(*rgb)
        cr.set_line_width(2)
        cr.move_to(lx, 14)
        cr.line_to(lx + 16, 14)
        cr.stroke()
        lx += 22 + _text(cr, lx + 22, 18, label, TEXT) + 18
    cr.set_source_rgb(*NOISE)
    cr.set_dash([4, 3])
    cr.move_to(lx, 14)
    cr.line_to(lx + 16, 14)
    cr.stroke()
    cr.set_dash([])
    _text(cr, lx + 22, 18, "Noise floor", TEXT)

    if not data["heard"] and not data["stats"]:
        _text(
            cr,
            PAD_L + plot_w / 2,
            PAD_T + panel_h,
            "No samples in this time range yet",
            MUTED,
            align="center",
            size=13,
        )
        _text(
            cr,
            PAD_L + plot_w / 2,
            PAD_T + panel_h + 20,
            "Signal is logged whenever this radio hears the repeater; stats when polled.",
            MUTED,
            align="center",
        )
        return

    panels = (
        ("RSSI (dBm)", "rssi", 10.0, True),
        ("SNR (dB)", "snr", 6.0, False),
    )
    readout: list[tuple[tuple, str]] = []
    hover_t = None
    if hover_x is not None and PAD_L <= hover_x <= PAD_L + plot_w:
        hover_t = t0 + (hover_x - PAD_L) / plot_w * span

    for idx, (title, field, min_span, with_noise) in enumerate(panels):
        top = PAD_T + idx * (panel_h + PANEL_GAP)
        bottom = top + panel_h
        vals = [r[field] for k, _l, _c in SERIES for r in data[k] if r[field] is not None]
        if with_noise:
            vals += [r["noise_floor"] for r in data["stats"] if r["noise_floor"] is not None]
        lo, hi = _y_range(vals, min_span) if vals else (0.0, 1.0)

        def Y(v: float, lo=lo, hi=hi, top=top, bottom=bottom) -> float:
            return bottom - (v - lo) / (hi - lo) * (bottom - top)

        # Title, grid, y labels (recessive)
        _text(cr, PAD_L, top - 6, title, TEXT, bold=True)
        step = _nice_step(hi - lo)
        v = lo
        cr.set_line_width(1)
        while v <= hi + step / 1000:
            y = round(Y(v)) + 0.5
            cr.set_source_rgba(*GRID, 1.0)
            cr.move_to(PAD_L, y)
            cr.line_to(PAD_L + plot_w, y)
            cr.stroke()
            _text(cr, PAD_L - 6, y + 4, f"{v:g}", MUTED, align="right")
            v += step

        cr.save()
        cr.rectangle(PAD_L, top - 2, plot_w, panel_h + 4)
        cr.clip()

        # Noise floor: dashed neutral reference line
        if with_noise:
            nf = [(r["t"], r["noise_floor"]) for r in data["stats"] if r["noise_floor"] is not None]
            cr.set_source_rgb(*NOISE)
            cr.set_line_width(1.5)
            cr.set_dash([4, 3])
            for seg in _segments(nf, data["gap"]["stats"]):
                cr.move_to(X(seg[0][0]), Y(seg[0][1]))
                for t, val in seg[1:]:
                    cr.line_to(X(t), Y(val))
                cr.stroke()
            cr.set_dash([])

        for key, _label, rgb in SERIES:
            rows = [r for r in data[key] if r[field] is not None]
            # min-max band for bucketed heard data
            if key == "heard":
                lo_f, hi_f = f"{field}_min", f"{field}_max"
                cr.set_source_rgba(*rgb, 0.18)
                for seg in _segments([(r["t"], r) for r in rows], data["gap"][key]):
                    if len(seg) < 2:
                        continue
                    cr.move_to(X(seg[0][0]), Y(seg[0][1][hi_f]))
                    for t, r in seg[1:]:
                        cr.line_to(X(t), Y(r[hi_f]))
                    for t, r in reversed(seg):
                        cr.line_to(X(t), Y(r[lo_f]))
                    cr.close_path()
                    cr.fill()
            cr.set_source_rgb(*rgb)
            cr.set_line_width(2)
            few = len(rows) <= MAX_MARKERS
            for seg in _segments([(r["t"], r[field]) for r in rows], data["gap"][key]):
                if len(seg) == 1 or (key == "stats" and few):
                    for t, val in seg:  # isolated / polled points get 8px markers
                        cr.arc(X(t), Y(val), 4, 0, 2 * math.pi)
                        cr.fill()
                if len(seg) > 1:
                    cr.move_to(X(seg[0][0]), Y(seg[0][1]))
                    for t, val in seg[1:]:
                        cr.line_to(X(t), Y(val))
                    cr.stroke()
        cr.restore()

        # Hover: nearest point per series within 24 px
        if hover_t is not None:
            unit = "dBm" if field == "rssi" else "dB"
            series = [(k, lab, c, field) for k, lab, c in SERIES]
            if with_noise:
                series.append(("stats", "Noise floor", NOISE, "noise_floor"))
            for key, label, rgb, f in series:
                rows = [r for r in data[key] if r[f] is not None]
                if not rows:
                    continue
                best = min(rows, key=lambda r: abs(r["t"] - hover_t))
                if abs(X(best["t"]) - hover_x) <= 24:
                    cr.set_source_rgb(*rgb)
                    cr.arc(X(best["t"]), Y(best[f]), 5, 0, 2 * math.pi)
                    cr.fill()
                    cr.set_source_rgb(*SURFACE)
                    cr.set_line_width(2)
                    cr.arc(X(best["t"]), Y(best[f]), 5, 0, 2 * math.pi)
                    cr.stroke()
                    readout.append((rgb, f"{best[f]:.1f} {unit}  {label}"))

    # X axis labels under the bottom panel
    base = PAD_T + 2 * panel_h + PANEL_GAP
    tstep = _time_step(span)
    tz_off = datetime.fromtimestamp(t0).astimezone().utcoffset().total_seconds()
    t = math.ceil((t0 + tz_off) / tstep) * tstep - tz_off
    while t <= t1:
        _text(cr, X(t), base + 16, _fmt_time(t, span), MUTED, align="center")
        t += tstep

    # Crosshair + one readout listing every series at that time
    if hover_t is not None:
        cr.set_source_rgba(*TEXT, 0.5)
        cr.set_line_width(1)
        x = round(hover_x) + 0.5
        cr.move_to(x, PAD_T)
        cr.line_to(x, base)
        cr.stroke()
        head = datetime.fromtimestamp(hover_t).strftime("%b %-d %H:%M")
        lines = [((), head)] + (readout or [((), "no sample here")])
        w = 0.0
        for _rgb, s in lines:
            cr.select_font_face("Sans", 0, 0)
            cr.set_font_size(FONT)
            ext = cr.text_extents(s)
            w = max(w, ext[4] if isinstance(ext, tuple) else ext.x_advance)
        bw, bh = w + 30, 16 * len(lines) + 10
        bx = hover_x + 12 if hover_x + 12 + bw < width else hover_x - 12 - bw
        by = PAD_T + 4
        cr.set_source_rgba(*SURFACE, 0.96)
        cr.rectangle(bx, by, bw, bh)
        cr.fill()
        cr.set_source_rgb(*GRID)
        cr.set_line_width(1)
        cr.rectangle(bx + 0.5, by + 0.5, bw - 1, bh - 1)
        cr.stroke()
        for i, (rgb, s) in enumerate(lines):
            y = by + 17 + 16 * i
            if rgb:
                cr.set_source_rgb(*rgb)
                cr.arc(bx + 12, y - 4, 4, 0, 2 * math.pi)
                cr.fill()
            _text(cr, bx + 22 if rgb else bx + 10, y, s, TEXT, bold=not rgb)


# -- Trace mode [trace-log patch] ------------------------------------------------
OUTBOUND = HEARD  # blue: away from this radio
RETURN = REPORTED  # orange: back toward this radio
TRACE_SERIES = (
    ("out", "Outbound (away from you)", OUTBOUND),
    ("ret", "Return (toward you)", RETURN),
)
SNR_LIMIT_DB = {5: -2.5, 6: -5.0, 7: -7.5, 8: -10.0, 9: -12.5, 10: -15.0, 11: -17.5, 12: -20.0}
MAX_TRACE_PANELS = 16  # [trace-scroll patch] more links: the graph scrolls
MIN_TRACE_PANEL_H = 70  # px per link panel before the graph grows and scrolls
TRACE_PANEL_GAP = 26
FAIL_STRIP = 18


def _hop_id(h) -> str:
    """Group key for a hop: 'me' or the first hash byte (hash width may vary over time)."""
    return "me" if h in (None, "me") else str(h)[:2].upper()


def _short(h) -> str:
    return "me" if h in (None, "me") else str(h)[:4].upper()


def path_link_keys(path: list) -> list[tuple]:
    """Link keys along *path* (outbound node list: "me", hops..., target). [trace-path patch]"""
    return [(_hop_id(path[i]), _hop_id(path[i + 1])) for i in range(len(path) - 1)]


def trace_links(
    traces: list[dict], max_points: int = 240, span: float = 0.0, path: list | None = None
) -> list[dict]:
    """Per-link series from logged traces, in the order of the most recent route.

    Returns [{"a": hop, "b": hop, "out": [(t, snr)], "ret": [(t, snr)]}] where
    *a* is the end nearer to this radio. Outbound = first half of the round trip.

    With *path* (the current network path: "me", hops..., target), only the links
    on that path are returned, in path order, each one even before it has data,
    so the graph always matches the Network Path. [trace-path patch]
    """
    links: dict[tuple, dict] = {}
    order: list[tuple] = []
    for tr in traces:
        if not tr["ok"]:
            continue
        legs = tr["links"]
        half = len(legs) / 2
        seen: list[tuple] = []
        for i, (frm, to, snr) in enumerate(legs):
            if snr is None:
                continue
            outbound = i < half
            a, b = (frm, to) if outbound else (to, frm)
            key = (_hop_id(a), _hop_id(b))
            entry = links.setdefault(key, {"a": a, "b": b, "out": [], "ret": []})
            entry["a"], entry["b"] = a, b  # latest full hash for labels
            entry["out" if outbound else "ret"].append((tr["t"], float(snr)))
            if key not in seen:
                seen.append(key)
        order = seen + [k for k in order if k not in seen]
    if path is not None and len(path) > 1:
        order = []
        for i, key in enumerate(path_link_keys(path)):
            if key not in order:
                order.append(key)
                links.setdefault(key, {"a": path[i], "b": path[i + 1], "out": [], "ret": []})
    else:
        for k in links:
            if k not in order:
                order.append(k)
    out = [links[k] for k in order]
    if span > 0:
        bucket = span / max_points
        for entry in out:
            for d in ("out", "ret"):
                if len(entry[d]) > max_points:
                    entry[d] = _bucket(entry[d], bucket)
    return out


def _bucket(points: list[tuple[float, float]], bucket: float) -> list[tuple[float, float]]:
    groups: dict[int, list[tuple[float, float]]] = {}
    for t, v in points:
        groups.setdefault(int(t // bucket), []).append((t, v))
    return [
        (sum(t for t, _ in g) / len(g), sum(v for _, v in g) / len(g))
        for _k, g in sorted(groups.items())
    ]


def trace_graph_height(n_links: int) -> int:  # [trace-scroll patch]
    """Drawing height that gives every link panel at least MIN_TRACE_PANEL_H."""
    n = max(1, min(n_links, MAX_TRACE_PANELS))
    return int(PAD_T + PAD_B + FAIL_STRIP + n * MIN_TRACE_PANEL_H + (n - 1) * TRACE_PANEL_GAP)


def draw_trace_graph(
    cr,
    width: int,
    height: int,
    data: dict,
    hover_x: float | None = None,
    hover_y: float | None = None,
) -> None:
    """*data*: {"t0","t1","links": trace_links(...), "fails": [t...], "limit": dB, "sf": n,
    "names": callable(hop)->str, "gap": s}."""
    t0, t1 = data["t0"], data["t1"]
    span = t1 - t0
    plot_w = width - PAD_L - PAD_R
    links = data["links"][:MAX_TRACE_PANELS]
    hidden = len(data["links"]) - len(links)
    if plot_w < 60:
        return

    def X(t: float) -> float:
        return PAD_L + (t - t0) / span * plot_w

    # Legend
    lx = PAD_L
    for _k, label, rgb in TRACE_SERIES:
        cr.set_source_rgb(*rgb)
        cr.arc(lx + 8, 14, 4, 0, 2 * math.pi)
        cr.fill()
        cr.set_line_width(2)
        cr.move_to(lx, 14)
        cr.line_to(lx + 16, 14)
        cr.stroke()
        lx += 22 + _text(cr, lx + 22, 18, label, TEXT) + 18
    cr.set_source_rgb(*NOISE)
    cr.set_line_width(1.5)
    cr.set_dash([4, 3])
    cr.move_to(lx, 14)
    cr.line_to(lx + 16, 14)
    cr.stroke()
    cr.set_dash([])
    lx += 22 + _text(cr, lx + 22, 18, f"SF{data['sf']} decode limit", TEXT) + 18
    _text(cr, lx, 18, "×", MUTED, bold=True)
    _text(cr, lx + 12, 18, "Failed", TEXT)

    if not links:
        mid = PAD_T + (height - PAD_T - PAD_B) / 2
        _text(
            cr,
            PAD_L + plot_w / 2,
            mid,
            "No traces in this time range yet",
            MUTED,
            align="center",
            size=13,
        )
        _text(
            cr,
            PAD_L + plot_w / 2,
            mid + 20,
            "Press Trace, or set Auto-trace on the Status tab.",
            MUTED,
            align="center",
        )
        if data["fails"]:
            _text(
                cr,
                PAD_L + plot_w / 2,
                mid + 40,
                f"{len(data['fails'])} trace(s) in this range failed.",
                MUTED,
                align="center",
            )
        return

    gap_between = TRACE_PANEL_GAP
    avail = height - PAD_T - PAD_B - FAIL_STRIP - gap_between * (len(links) - 1)
    panel_h = avail / len(links)
    if panel_h < 36:
        _text(cr, PAD_L, PAD_T + 10, "Window too small for this many links", MUTED)
        return

    limit = data["limit"]
    hover_t = None
    if hover_x is not None and PAD_L <= hover_x <= PAD_L + plot_w:
        hover_t = t0 + (hover_x - PAD_L) / plot_w * span
    readout: list[tuple[tuple, str]] = []
    names = data["names"]
    gap = data["gap"]

    for idx, link in enumerate(links):
        top = PAD_T + idx * (panel_h + gap_between)
        bottom = top + panel_h
        vals = [v for d in ("out", "ret") for _t, v in link[d]] + [limit]
        lo, hi = _y_range(vals, 6.0)

        def Y(v: float, lo=lo, hi=hi, top=top, bottom=bottom) -> float:
            return bottom - (v - lo) / (hi - lo) * (bottom - top)

        title = f"{names(link['a'])}  ↔  {names(link['b'])}"
        _text(cr, PAD_L, top - 6, title, TEXT, bold=True)
        step = _nice_step(hi - lo, 3 if panel_h < 90 else 4)
        v = lo
        cr.set_line_width(1)
        while v <= hi + step / 1000:
            y = round(Y(v)) + 0.5
            cr.set_source_rgb(*GRID)
            cr.move_to(PAD_L, y)
            cr.line_to(PAD_L + plot_w, y)
            cr.stroke()
            _text(cr, PAD_L - 6, y + 4, f"{v:g}", MUTED, align="right")
            v += step
        # [trace-unit patch] unit at the right: "dB" next to the title read as "dBm"
        _text(cr, PAD_L + plot_w, top - 6, "SNR, dB", MUTED, align="right")

        cr.save()
        cr.rectangle(PAD_L, top - 2, plot_w, panel_h + 4)
        cr.clip()
        y = round(Y(limit)) + 0.5
        cr.set_source_rgb(*NOISE)
        cr.set_line_width(1.5)
        cr.set_dash([4, 3])
        cr.move_to(PAD_L, y)
        cr.line_to(PAD_L + plot_w, y)
        cr.stroke()
        cr.set_dash([])
        for key, _label, rgb in TRACE_SERIES:
            pts = link[key]
            cr.set_source_rgb(*rgb)
            cr.set_line_width(2)
            if len(pts) <= MAX_MARKERS:
                for t, val in pts:
                    cr.arc(X(t), Y(val), 3.5, 0, 2 * math.pi)
                    cr.fill()
            for seg in _segments(pts, gap):
                if len(seg) > 1:
                    cr.move_to(X(seg[0][0]), Y(seg[0][1]))
                    for t, val in seg[1:]:
                        cr.line_to(X(t), Y(val))
                    cr.stroke()
        cr.restore()

        if hover_t is not None:
            for key, label, rgb in TRACE_SERIES:
                pts = link[key]
                if not pts:
                    continue
                t, val = min(pts, key=lambda p: abs(p[0] - hover_t))
                if abs(X(t) - hover_x) <= 24:
                    cr.set_source_rgb(*rgb)
                    cr.arc(X(t), Y(val), 5, 0, 2 * math.pi)
                    cr.fill()
                    cr.set_source_rgb(*SURFACE)
                    cr.set_line_width(2)
                    cr.arc(X(t), Y(val), 5, 0, 2 * math.pi)
                    cr.stroke()
                    a, b = (link["a"], link["b"]) if key == "out" else (link["b"], link["a"])
                    readout.append(
                        (
                            rgb,
                            f"{val:+.2f} dB  {_short(a)} \u2192 {_short(b)}  "
                            f"({val - limit:+.1f} margin)",
                        )
                    )

    # Failure strip + time axis
    strip_y = PAD_T + len(links) * panel_h + (len(links) - 1) * gap_between + 4
    base = strip_y + FAIL_STRIP
    cr.set_source_rgb(*GRID)
    cr.set_line_width(1)
    cr.move_to(PAD_L, base - 4.5)
    cr.line_to(PAD_L + plot_w, base - 4.5)
    cr.stroke()
    for t in data["fails"]:
        _text(cr, X(t), strip_y + 10, "×", MUTED, align="center", bold=True)
    if hidden > 0:
        _text(
            cr,
            PAD_L + plot_w,
            strip_y + 10,
            f"+{hidden} more link(s) not shown",
            MUTED,
            align="right",
        )
    tstep = _time_step(span)
    tz_off = datetime.fromtimestamp(t0).astimezone().utcoffset().total_seconds()
    t = math.ceil((t0 + tz_off) / tstep) * tstep - tz_off
    while t <= t1:
        _text(cr, X(t), base + 12, _fmt_time(t, span), MUTED, align="center")
        t += tstep

    if hover_t is not None:
        fails_near = [f for f in data["fails"] if abs(X(f) - hover_x) <= 12]
        if fails_near:
            readout.append(((), f"{len(fails_near)} failed trace(s)"))
        _crosshair(cr, width, hover_x, hover_t, PAD_T, base - 4, readout, hover_y=hover_y)


def _crosshair(cr, width, hover_x, hover_t, top, bottom, readout, hover_y=None) -> None:
    cr.set_source_rgba(*TEXT, 0.5)
    cr.set_line_width(1)
    x = round(hover_x) + 0.5
    cr.move_to(x, top)
    cr.line_to(x, bottom)
    cr.stroke()
    head = datetime.fromtimestamp(hover_t).strftime("%b %-d %H:%M")
    lines = [((), head)] + (readout or [((), "no trace here")])
    w = 0.0
    for _rgb, s in lines:
        cr.select_font_face("Sans", 0, 0)
        cr.set_font_size(FONT)
        ext = cr.text_extents(s)
        w = max(w, ext[4] if isinstance(ext, tuple) else ext.x_advance)
    bw, bh = w + 30, 16 * len(lines) + 10
    bx = hover_x + 12 if hover_x + 12 + bw < width else hover_x - 12 - bw
    by = top + 4
    if (
        hover_y is not None
    ):  # [trace-scroll patch] next to the pointer: the top may be scrolled away
        by = max(top + 4, min(hover_y + 12, bottom - bh))
    cr.set_source_rgba(*SURFACE, 0.96)
    cr.rectangle(bx, by, bw, bh)
    cr.fill()
    cr.set_source_rgb(*GRID)
    cr.set_line_width(1)
    cr.rectangle(bx + 0.5, by + 0.5, bw - 1, bh - 1)
    cr.stroke()
    for i, (rgb, s) in enumerate(lines):
        y = by + 17 + 16 * i
        if rgb:
            cr.set_source_rgb(*rgb)
            cr.arc(bx + 12, y - 4, 4, 0, 2 * math.pi)
            cr.fill()
        _text(cr, bx + 22 if rgb else bx + 10, y, s, TEXT, bold=not rgb)


# -- Packets mode [packet-graph patch] --------------------------------------------
# RepeaterStats counters are cumulative since the repeater booted, so the chart
# shows rates: the change between consecutive polls divided by the time between
# them, per hour. A counter that goes down (or uptime that goes down) means the
# repeater rebooted; that interval is skipped and marked.
ERR_RX = (0.898, 0.282, 0.302)  # #e5484d red: radio RX errors
ERR_EV = (0.937, 0.725, 0.247)  # #efb93f amber, dashed: firmware error events
PACKET_FIELDS = {
    "rx": "n_packets_recv",
    "tx": "n_packets_sent",
    "rx_flood": "n_recv_flood",
    "rx_direct": "n_recv_direct",
    "tx_flood": "n_sent_flood",
    "tx_direct": "n_sent_direct",
    "dups_flood": "n_flood_dups",
    "dups_direct": "n_direct_dups",
    "rx_err": "n_recv_errors",
    "err_ev": "err_events",
}
PACKET_PANELS = (
    ("Packets per hour", (("rx", "RX", HEARD, False), ("tx", "TX", REPORTED, False)), 10.0),
    (
        "Errors per hour",
        (
            ("rx_err", "RX errors (CRC/header)", ERR_RX, False),
            ("err_ev", "Error events", ERR_EV, True),
        ),
        2.0,
    ),
)


def packet_rates(rows: list[dict], bucket_s: float, gap_s: float) -> dict:
    """Per-hour rates from polled counters. Returns {"points": [{"t", key: rate...}],
    "reboots": [t...], "polls": n}. Intervals longer than *gap_s* are skipped."""
    buckets: dict[int, dict] = {}
    reboots: list[float] = []
    for a, b in zip(rows, rows[1:]):
        dt = b["t"] - a["t"]
        if dt <= 0 or dt > gap_s:
            continue
        up_a, up_b = a.get("total_up_time_secs"), b.get("total_up_time_secs")
        if (up_a is not None and up_b is not None and up_b < up_a) or any(
            a.get(f) is not None and b.get(f) is not None and b[f] < a[f]
            for f in ("n_packets_recv", "n_packets_sent")
        ):
            reboots.append(b["t"])
            continue
        bk = buckets.setdefault(int(b["t"] // bucket_s), {"t": 0.0, "n": 0, "d": {}, "dt": {}})
        bk["t"] += a["t"] + dt / 2
        bk["n"] += 1
        for key, field in PACKET_FIELDS.items():
            if a.get(field) is None or b.get(field) is None:
                continue
            bk["d"][key] = bk["d"].get(key, 0) + (b[field] - a[field])
            bk["dt"][key] = bk["dt"].get(key, 0.0) + dt
    points = []
    for _k, bk in sorted(buckets.items()):
        pt = {"t": bk["t"] / bk["n"]}
        for key in PACKET_FIELDS:
            pt[key] = bk["d"][key] / bk["dt"][key] * 3600 if bk["dt"].get(key) else None
        points.append(pt)
    return {"points": points, "reboots": reboots, "polls": len(rows)}


def _fmt_rate(v: float | None) -> str:
    if v is None:
        return "n/a"
    return f"{v:.0f}/h" if v >= 10 else f"{v:.1f}/h"


def draw_packet_graph(
    cr, width: int, height: int, data: dict, hover_x: float | None = None
) -> None:
    """*data*: {"t0","t1","points","reboots","latest": stats row or None,"gap": s,"interval": s}."""
    t0, t1 = data["t0"], data["t1"]
    span = t1 - t0
    plot_w = width - PAD_L - PAD_R
    panel_h = (height - PAD_T - PAD_B - PANEL_GAP) / 2
    if plot_w < 60 or panel_h < 40:
        return

    def X(t: float) -> float:
        return PAD_L + (t - t0) / span * plot_w

    points = data["points"]
    latest = data.get("latest") or {}
    if not points:
        mid = PAD_T + panel_h
        _text(
            cr,
            PAD_L + plot_w / 2,
            mid,
            "No packet rates in this time range yet",
            MUTED,
            align="center",
            size=13,
        )
        _text(
            cr,
            PAD_L + plot_w / 2,
            mid + 20,
            f"Rates need two stats polls in a row (polled every {data['interval'] // 60:.0f} min "
            "while logged in).",
            MUTED,
            align="center",
        )
        return

    hover_t = None
    if hover_x is not None and PAD_L <= hover_x <= PAD_L + plot_w:
        hover_t = t0 + (hover_x - PAD_L) / plot_w * span
    best = min(points, key=lambda r: abs(r["t"] - hover_t)) if hover_t is not None else None
    if best is not None and abs(X(best["t"]) - hover_x) > 24:
        best = None
    few = len(points) <= MAX_MARKERS

    for idx, (title, series, min_span) in enumerate(PACKET_PANELS):
        top = PAD_T + idx * (panel_h + PANEL_GAP)
        bottom = top + panel_h
        vals = [r[k] for k, *_ in series for r in points if r[k] is not None]
        hi = max(vals) if vals else 0.0
        step = _nice_step(max(hi, min_span))
        hi = max(step, math.ceil(hi * 1.08 / step) * step)  # headroom above the peak
        lo = 0.0

        def Y(v: float, hi=hi, top=top, bottom=bottom) -> float:
            return bottom - v / hi * (bottom - top)

        # Title + this panel's legend on one line
        lx = PAD_L + _text(cr, PAD_L, top - 6, title, TEXT, bold=True) + 16
        for _k, label, rgb, dashed in series:
            cr.set_source_rgb(*rgb)
            cr.set_line_width(2)
            cr.set_dash([4, 3] if dashed else [])
            cr.move_to(lx, top - 10)
            cr.line_to(lx + 16, top - 10)
            cr.stroke()
            cr.set_dash([])
            lx += 22 + _text(cr, lx + 22, top - 6, label, TEXT) + 14
        if latest:
            if idx == 0:
                tot = f"since boot: {latest.get('n_packets_recv', 0):,} RX · {latest.get('n_packets_sent', 0):,} TX"
            else:
                tot = (
                    f"since boot: {latest.get('n_recv_errors', 'n/a')} RX err · "
                    f"{latest.get('err_events', 'n/a')} events"
                )
            _text(cr, PAD_L + plot_w, top - 6, tot, MUTED, align="right")

        v = lo
        cr.set_line_width(1)
        while v <= hi + step / 1000:
            y = round(Y(v)) + 0.5
            cr.set_source_rgb(*GRID)
            cr.move_to(PAD_L, y)
            cr.line_to(PAD_L + plot_w, y)
            cr.stroke()
            _text(cr, PAD_L - 6, y + 4, f"{v:g}", MUTED, align="right")
            v += step

        cr.save()
        cr.rectangle(PAD_L, top - 2, plot_w, panel_h + 4)
        cr.clip()
        for t in data["reboots"]:  # neutral dotted marker, labelled in the top panel
            cr.set_source_rgba(*MUTED, 0.7)
            cr.set_line_width(1)
            cr.set_dash([2, 3])
            cr.move_to(round(X(t)) + 0.5, top)
            cr.line_to(round(X(t)) + 0.5, bottom)
            cr.stroke()
            cr.set_dash([])
            if idx == 0:
                _text(cr, X(t) + 4, top + 12, "reboot", MUTED)
        for key, _label, rgb, dashed in series:
            pts = [(r["t"], r[key]) for r in points if r[key] is not None]
            cr.set_source_rgb(*rgb)
            cr.set_line_width(2)
            for seg in _segments(pts, data["gap"]):
                if len(seg) == 1 or few:
                    for t, val in seg:
                        cr.arc(X(t), Y(val), 3.5 if few else 4, 0, 2 * math.pi)
                        cr.fill()
                if len(seg) > 1:
                    cr.set_dash([4, 3] if dashed else [])
                    cr.move_to(X(seg[0][0]), Y(seg[0][1]))
                    for t, val in seg[1:]:
                        cr.line_to(X(t), Y(val))
                    cr.stroke()
                    cr.set_dash([])
        cr.restore()

        if best is not None:
            for key, _label, rgb, _d in series:
                if best[key] is None:
                    continue
                cr.set_source_rgb(*rgb)
                cr.arc(X(best["t"]), Y(best[key]), 5, 0, 2 * math.pi)
                cr.fill()
                cr.set_source_rgb(*SURFACE)
                cr.set_line_width(2)
                cr.arc(X(best["t"]), Y(best[key]), 5, 0, 2 * math.pi)
                cr.stroke()

    base = PAD_T + 2 * panel_h + PANEL_GAP
    tstep = _time_step(span)
    tz_off = datetime.fromtimestamp(t0).astimezone().utcoffset().total_seconds()
    t = math.ceil((t0 + tz_off) / tstep) * tstep - tz_off
    while t <= t1:
        _text(cr, X(t), base + 16, _fmt_time(t, span), MUTED, align="center")
        t += tstep

    if hover_t is not None:
        readout: list[tuple[tuple, str]] = []
        if best is not None:
            b = best

            def split(a: str, c: str) -> str:
                return (
                    f" (flood {_fmt_rate(b[a])} · direct {_fmt_rate(b[c])})"
                    if b[a] is not None
                    else ""
                )

            readout = [
                (HEARD, f"RX {_fmt_rate(b['rx'])}" + split("rx_flood", "rx_direct")),
                (REPORTED, f"TX {_fmt_rate(b['tx'])}" + split("tx_flood", "tx_direct")),
                (
                    MUTED,
                    f"Duplicates {_fmt_rate((b['dups_flood'] or 0) + (b['dups_direct'] or 0))}"
                    if b["dups_flood"] is not None
                    else "Duplicates n/a",
                ),
                (ERR_RX, f"RX errors {_fmt_rate(b['rx_err'])}"),
                (ERR_EV, f"Error events {_fmt_rate(b['err_ev'])}"),
            ]
        _crosshair(cr, width, hover_x, hover_t, PAD_T, base, readout or [((), "no rate here")])


# -- Battery mode [battery-graph patch] -------------------------------------------
# batt_milli_volts from each stats poll (every POLL_INTERVAL_S while logged in).
# Line = bucket average; band = min..max inside the bucket.
BATT = (0.290, 0.784, 0.478)  # #4ac87a green


# Power source strip [power-source patch]
POWER_COLORS = {
    "usb": HEARD,  # blue
    "dc": REPORTED,  # orange
    "solar": (0.937, 0.725, 0.247),  # amber
    "battery": BATT,  # green
    "unknown": MUTED,
}
POWER_LABELS = {"usb": "USB", "dc": "DC", "solar": "Solar", "battery": "Battery", "unknown": "?"}
POWER_SOURCES_TEXT = {
    "usb": "On USB power",
    "dc": "On external DC power",
    "solar": "Solar charging",
    "battery": "On battery",
    "unknown": "Source not detectable",
}
STRIP_H = 18


def draw_power_icon(cr, src: str, x: float, y: float, rgb=None) -> float:
    """Small vector icon centred vertically on *y*, left edge at *x*. Returns its width."""
    rgb = rgb or POWER_COLORS.get(src, MUTED)
    cr.save()
    cr.set_source_rgb(*rgb)
    cr.set_line_width(1.5)
    cr.set_dash([])
    if src == "battery":  # cell with a nub, two-thirds full
        cr.rectangle(x + 0.75, y - 4.25, 13, 8.5)
        cr.stroke()
        cr.rectangle(x + 13.75, y - 2, 2, 4)
        cr.fill()
        cr.rectangle(x + 2.5, y - 2.5, 7, 5)
        cr.fill()
        w = 16.0
    elif src == "usb":  # plug: body + two prongs + cable
        cr.rectangle(x + 5, y - 4, 7, 8)
        cr.fill()
        cr.rectangle(x + 12, y - 3, 3, 1.6)
        cr.fill()
        cr.rectangle(x + 12, y + 1.4, 3, 1.6)
        cr.fill()
        cr.move_to(x, y)
        cr.line_to(x + 5, y)
        cr.stroke()
        w = 15.0
    elif src == "dc":  # DC symbol: solid bar over dashed bar
        cr.set_line_width(1.8)
        cr.move_to(x, y - 2.5)
        cr.line_to(x + 14, y - 2.5)
        cr.stroke()
        cr.set_dash([3, 2.5])
        cr.move_to(x, y + 2.5)
        cr.line_to(x + 14, y + 2.5)
        cr.stroke()
        cr.set_dash([])
        w = 14.0
    elif src == "solar":  # sun
        cx, cy = x + 7, y
        cr.arc(cx, cy, 3, 0, 2 * math.pi)
        cr.fill()
        for i in range(8):
            a = i * math.pi / 4
            cr.move_to(cx + 4.6 * math.cos(a), cy + 4.6 * math.sin(a))
            cr.line_to(cx + 6.8 * math.cos(a), cy + 6.8 * math.sin(a))
        cr.stroke()
        w = 14.0
    else:
        cr.restore()
        return _text(cr, x, y + 4, "?", rgb, bold=True)
    cr.restore()
    return w


def _power_runs(
    power: list[dict], t0: float, t1: float, gap: float
) -> list[tuple[float, float, str]]:
    """(start, end, src) runs; a reading holds until the next one, at most *gap* seconds."""
    runs: list[tuple[float, float, str]] = []
    for i, p in enumerate(power):
        nxt = power[i + 1]["t"] if i + 1 < len(power) else t1
        a, b = max(p["t"], t0), min(nxt, p["t"] + gap, t1)
        if b <= a:
            continue
        if runs and runs[-1][2] == p["src"] and abs(runs[-1][1] - a) < 1:
            runs[-1] = (runs[-1][0], b, p["src"])
        else:
            runs.append((a, b, p["src"]))
    return runs


def power_at(power: list[dict], t: float, gap: float) -> str | None:
    """Power source in force at time *t*, or None if no reading covers it."""
    found = None
    for p in power:
        if p["t"] <= t:
            found = p
        else:
            break
    if found is None or t - found["t"] > gap:
        return None
    return found["src"]


def draw_battery_graph(
    cr, width: int, height: int, data: dict, hover_x: float | None = None
) -> None:
    """*data*: {"t0","t1","points": battery_series rows,"latest_mv","gap","interval"}."""
    t0, t1 = data["t0"], data["t1"]
    span = t1 - t0
    plot_w = width - PAD_L - PAD_R
    power = data.get("power") or []
    title_y = PAD_T - 6
    top, bottom = PAD_T + (STRIP_H + 8 if power else 0), height - PAD_B  # strip only with readings
    if plot_w < 60 or bottom - top < 40:
        return

    def X(t: float) -> float:
        return PAD_L + (t - t0) / span * plot_w

    if power:  # [power-source patch] strip of power-source runs with an icon at each change
        sy = PAD_T + 2
        _text(cr, PAD_L - 6, sy + STRIP_H / 2 + 4, "Power", MUTED, align="right")
        cr.set_source_rgb(*GRID)
        cr.rectangle(PAD_L, sy, plot_w, STRIP_H)
        cr.fill()
        last_icon_x = -1e9
        for a, b, src in _power_runs(power, t0, t1, data.get("power_gap", 3 * 3600)):
            x0, x1 = X(a), X(b)
            cr.set_source_rgba(*POWER_COLORS.get(src, MUTED), 0.28)
            cr.rectangle(x0, sy, max(1.0, x1 - x0), STRIP_H)
            cr.fill()
            if x1 - x0 >= 20 and x0 - last_icon_x >= 22:
                draw_power_icon(cr, src, x0 + 3, sy + STRIP_H / 2)
                last_icon_x = x0

    points = [p for p in data["points"] if p.get("mv")]
    if not points:
        _text(
            cr,
            PAD_L + plot_w / 2,
            (top + bottom) / 2,
            "No battery readings in this time range yet",
            MUTED,
            align="center",
            size=13,
        )
        _text(
            cr,
            PAD_L + plot_w / 2,
            (top + bottom) / 2 + 20,
            f"Voltage comes from stats polls (every {data['interval'] // 60:.0f} min while logged in).",
            MUTED,
            align="center",
        )
        return

    vals = [p["mv_min"] / 1000 for p in points] + [p["mv_max"] / 1000 for p in points]
    lo, hi = _y_range(vals, 0.2)
    step = _nice_step(hi - lo)

    def Y(v: float) -> float:
        return bottom - (v - lo) / (hi - lo) * (bottom - top)

    lx = PAD_L + _text(cr, PAD_L, title_y, "Battery voltage", TEXT, bold=True) + 16
    cr.set_source_rgb(*BATT)
    cr.set_line_width(2)
    cr.move_to(lx, title_y - 4)
    cr.line_to(lx + 16, title_y - 4)
    cr.stroke()
    lx += 22 + _text(cr, lx + 22, title_y, "average", TEXT) + 14
    cr.set_source_rgba(*BATT, 0.25)
    cr.rectangle(lx, title_y - 9, 16, 10)
    cr.fill()
    lx += 22 + _text(cr, lx + 22, title_y, "min\u2013max", TEXT) + 18
    for src in [
        s for s in POWER_LABELS if any(p["src"] == s for p in power)
    ]:  # [power-source patch]
        lx += draw_power_icon(cr, src, lx, title_y - 4) + 5
        lx += _text(cr, lx, title_y, POWER_LABELS[src], TEXT) + 12
    right = []
    if power:
        right.append(
            POWER_LABELS.get(power[-1]["src"], "?")
            if power[-1]["src"] != "unknown"
            else "source n/a"
        )
    if data.get("latest_mv"):
        right.append(f"latest {data['latest_mv'] / 1000:.3f} V")
    if right:
        _text(cr, PAD_L + plot_w, title_y, " \u00b7 ".join(right), MUTED, align="right")

    v = lo
    cr.set_line_width(1)
    while v <= hi + step / 1000:
        y = round(Y(v)) + 0.5
        cr.set_source_rgb(*GRID)
        cr.move_to(PAD_L, y)
        cr.line_to(PAD_L + plot_w, y)
        cr.stroke()
        _text(cr, PAD_L - 6, y + 4, f"{v:.2f}", MUTED, align="right")
        v += step

    few = len(points) <= MAX_MARKERS
    cr.save()
    cr.rectangle(PAD_L, top - 2, plot_w, bottom - top + 4)
    cr.clip()
    by_t = {p["t"]: p for p in points}
    for seg in _segments([(p["t"], p["mv"] / 1000) for p in points], data["gap"]):
        if len(seg) > 1:  # min..max band
            cr.set_source_rgba(*BATT, 0.25)
            cr.move_to(X(seg[0][0]), Y(by_t[seg[0][0]]["mv_max"] / 1000))
            for t, _v in seg[1:]:
                cr.line_to(X(t), Y(by_t[t]["mv_max"] / 1000))
            for t, _v in reversed(seg):
                cr.line_to(X(t), Y(by_t[t]["mv_min"] / 1000))
            cr.close_path()
            cr.fill()
        cr.set_source_rgb(*BATT)
        cr.set_line_width(2)
        if len(seg) == 1 or few:
            for t, val in seg:
                cr.arc(X(t), Y(val), 3.5, 0, 2 * math.pi)
                cr.fill()
        if len(seg) > 1:
            cr.move_to(X(seg[0][0]), Y(seg[0][1]))
            for t, val in seg[1:]:
                cr.line_to(X(t), Y(val))
            cr.stroke()
    cr.restore()

    tstep = _time_step(span)
    tz_off = datetime.fromtimestamp(t0).astimezone().utcoffset().total_seconds()
    t = math.ceil((t0 + tz_off) / tstep) * tstep - tz_off
    while t <= t1:
        _text(cr, X(t), bottom + 16, _fmt_time(t, span), MUTED, align="center")
        t += tstep

    if hover_x is not None and PAD_L <= hover_x <= PAD_L + plot_w:
        hover_t = t0 + (hover_x - PAD_L) / plot_w * span
        best = min(points, key=lambda r: abs(r["t"] - hover_t))
        readout = [((), "no reading here")]
        if abs(X(best["t"]) - hover_x) <= 24:
            cr.set_source_rgb(*BATT)
            cr.arc(X(best["t"]), Y(best["mv"] / 1000), 5, 0, 2 * math.pi)
            cr.fill()
            cr.set_source_rgb(*SURFACE)
            cr.set_line_width(2)
            cr.arc(X(best["t"]), Y(best["mv"] / 1000), 5, 0, 2 * math.pi)
            cr.stroke()
            readout = [(BATT, f"{best['mv'] / 1000:.3f} V")]
            if best["n"] > 1:
                readout.append(
                    (
                        (),
                        f"{best['mv_min'] / 1000:.3f}\u2013{best['mv_max'] / 1000:.3f} V, "
                        f"{best['n']} polls",
                    )
                )
        if power:  # [power-source patch]
            src = power_at(power, hover_t, data.get("power_gap", 3 * 3600))
            if src is not None:
                readout.append((POWER_COLORS.get(src, MUTED), POWER_SOURCES_TEXT.get(src, src)))
        _crosshair(cr, width, hover_x, hover_t, PAD_T, bottom, readout)


# -- Neighbours mode [neighbour-graph patch] ---------------------------------------
NB_SERIES = (
    ("h1", "Heard in last hour", HEARD, False),
    ("h24", "Heard in last 24 h", REPORTED, False),
    ("total", "In table", MUTED, True),
)


def draw_neighbour_graph(
    cr, width: int, height: int, data: dict, hover_x: float | None = None
) -> None:
    """*data*: {"t0","t1","points": neighbour_series rows,"gap","interval"}."""
    t0, t1 = data["t0"], data["t1"]
    span = t1 - t0
    plot_w = width - PAD_L - PAD_R
    top, bottom = PAD_T, height - PAD_B
    if plot_w < 60 or bottom - top < 40:
        return

    def X(t: float) -> float:
        return PAD_L + (t - t0) / span * plot_w

    points = data["points"]
    if not points:
        _text(
            cr,
            PAD_L + plot_w / 2,
            (top + bottom) / 2,
            "No neighbour counts in this time range yet",
            MUTED,
            align="center",
            size=13,
        )
        _text(
            cr,
            PAD_L + plot_w / 2,
            (top + bottom) / 2 + 20,
            f"Counted with each stats poll (every {data['interval'] // 60:.0f} min while logged in).",
            MUTED,
            align="center",
        )
        return

    vals = [p[k] for k, *_ in NB_SERIES for p in points if p.get(k) is not None]
    hi = max(vals) if vals else 1
    step = max(1.0, _nice_step(max(hi, 4)))
    hi = max(step, math.ceil(hi * 1.1 / step) * step)

    def Y(v: float) -> float:
        return bottom - v / hi * (bottom - top)

    lx = PAD_L + _text(cr, PAD_L, top - 6, "Neighbours", TEXT, bold=True) + 16
    for _k, label, rgb, dashed in NB_SERIES:
        cr.set_source_rgb(*rgb)
        cr.set_line_width(2)
        cr.set_dash([4, 3] if dashed else [])
        cr.move_to(lx, top - 10)
        cr.line_to(lx + 16, top - 10)
        cr.stroke()
        cr.set_dash([])
        lx += 22 + _text(cr, lx + 22, top - 6, label, TEXT) + 14
    last = points[-1]
    _text(
        cr,
        PAD_L + plot_w,
        top - 6,
        f"latest: {last.get('h1', 0)} in 1 h \u00b7 {last.get('h24', 0)} in 24 h \u00b7 "
        f"{last.get('total', 0)} in table",
        MUTED,
        align="right",
    )

    v = 0.0
    cr.set_line_width(1)
    while v <= hi + step / 1000:
        y = round(Y(v)) + 0.5
        cr.set_source_rgb(*GRID)
        cr.move_to(PAD_L, y)
        cr.line_to(PAD_L + plot_w, y)
        cr.stroke()
        _text(cr, PAD_L - 6, y + 4, f"{v:g}", MUTED, align="right")
        v += step

    few = len(points) <= MAX_MARKERS
    cr.save()
    cr.rectangle(PAD_L, top - 2, plot_w, bottom - top + 4)
    cr.clip()
    for key, _label, rgb, dashed in NB_SERIES:
        pts = [(p["t"], p[key]) for p in points if p.get(key) is not None]
        cr.set_source_rgb(*rgb)
        cr.set_line_width(2)
        for seg in _segments(pts, data["gap"]):
            if len(seg) == 1 or (few and not dashed):
                for t, val in seg:
                    cr.arc(X(t), Y(val), 3.5, 0, 2 * math.pi)
                    cr.fill()
            if len(seg) > 1:  # steps: a count holds until the next poll
                cr.set_dash([4, 3] if dashed else [])
                cr.move_to(X(seg[0][0]), Y(seg[0][1]))
                for (_ta, va), (tb, vb) in zip(seg, seg[1:]):
                    cr.line_to(X(tb), Y(va))
                    cr.line_to(X(tb), Y(vb))
                cr.stroke()
                cr.set_dash([])
    cr.restore()

    tstep = _time_step(span)
    tz_off = datetime.fromtimestamp(t0).astimezone().utcoffset().total_seconds()
    t = math.ceil((t0 + tz_off) / tstep) * tstep - tz_off
    while t <= t1:
        _text(cr, X(t), bottom + 16, _fmt_time(t, span), MUTED, align="center")
        t += tstep

    if hover_x is not None and PAD_L <= hover_x <= PAD_L + plot_w:
        hover_t = t0 + (hover_x - PAD_L) / plot_w * span
        best = min(points, key=lambda r: abs(r["t"] - hover_t))
        readout = [((), "no poll here")]
        if abs(X(best["t"]) - hover_x) <= 24:
            readout = [(rgb, f"{label}: {best.get(k, 'n/a')}") for k, label, rgb, _d in NB_SERIES]
            if best.get("snr_h24") is not None:
                readout.append((MUTED, f"Mean SNR (24 h): {best['snr_h24']:+.1f} dB"))
            for k, _l, rgb, _d in NB_SERIES[:2]:
                if best.get(k) is not None:
                    cr.set_source_rgb(*rgb)
                    cr.arc(X(best["t"]), Y(best[k]), 5, 0, 2 * math.pi)
                    cr.fill()
        _crosshair(cr, width, hover_x, hover_t, top, bottom, readout)


class RepeaterGraphPage(Gtk.Box):
    """Range selector + the two charts for the selected repeater."""

    def __init__(self, service) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self._service = service
        self._pubkey: str | None = None
        self._shown_path: list | None = (
            None  # [trace-path patch] path the Trace graph was drawn for
        )
        self._range_s = 86400  # default 24 h
        self._hover_x: float | None = None
        self._data: dict | None = None
        self._mode = "signal"  # or "trace" [trace-log patch]

        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        bar.set_margin_start(8)
        bar.set_margin_end(8)
        bar.set_margin_top(8)
        mode_group: Gtk.ToggleButton | None = None
        for label, mode, tip in (
            ("Signal", "signal", "RSSI and SNR heard by this radio and reported by the repeater"),
            ("Trace", "trace", "SNR of every link from logged round-trip traces"),
            ("Packets", "packets", "RX/TX packets and errors per hour, from polled repeater stats"),
            ("Battery", "battery", "Battery voltage from polled repeater stats"),
            (
                "Neighbours",
                "neighbours",
                "Neighbours heard by the repeater, counted with each poll",
            ),
        ):
            btn = Gtk.ToggleButton(label=label)
            btn.add_css_class("quick-cmd-btn")
            btn.set_tooltip_text(tip)
            if mode_group is not None:
                btn.set_group(mode_group)
            mode_group = mode_group or btn
            btn.set_active(mode == self._mode)
            btn.connect("toggled", self._on_mode, mode)
            bar.append(btn)
        sep = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        sep.set_margin_start(6)
        sep.set_margin_end(6)
        bar.append(sep)
        group: Gtk.ToggleButton | None = None
        for label, seconds in RANGES:
            btn = Gtk.ToggleButton(label=label)
            btn.add_css_class("quick-cmd-btn")
            if group is not None:
                btn.set_group(group)
            group = group or btn
            btn.set_active(seconds == self._range_s)
            btn.connect("toggled", self._on_range, seconds)
            bar.append(btn)
        self._info = Gtk.Label(label="")
        self._info.add_css_class("panel-muted")
        self._info.set_hexpand(True)
        self._info.set_halign(Gtk.Align.END)
        bar.append(self._info)
        self.append(bar)

        self._area = Gtk.DrawingArea()
        self._area.set_vexpand(True)
        self._area.set_hexpand(True)
        self._area.set_margin_start(4)
        self._area.set_margin_end(4)
        self._area.set_margin_bottom(6)
        self._area.set_draw_func(self._draw)
        self._hover_y: float | None = None
        motion = Gtk.EventControllerMotion.new()
        motion.connect("motion", self._on_motion)
        motion.connect("leave", self._on_leave)
        self._area.add_controller(motion)
        self._scroll = Gtk.ScrolledWindow()  # [trace-scroll patch] long routes scroll
        self._scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._scroll.set_vexpand(True)
        self._scroll.set_child(self._area)
        self.append(self._scroll)

        GLib.timeout_add_seconds(60, self._tick)

    def set_repeater(self, pubkey: str | None) -> None:
        self._pubkey = pubkey.lower() if pubkey else None
        self.refresh()

    def path_changed(self) -> None:  # [trace-path patch]
        """The repeater's network path may have changed: redraw Trace if it did."""
        if self._mode == "trace" and self._pubkey and self._current_path() != self._shown_path:
            self.refresh()

    def _current_path(self) -> list | None:  # [trace-path patch]
        """Current network path to the repeater as "me", hops..., pubkey (None if unknown)."""
        try:
            peers = self._service.list_peers()
        except Exception:  # noqa: BLE001
            return None
        for p in peers:
            if (p.public_key or p.peer_id or "").lower() == self._pubkey:
                return ["me", *(p.last_path or []), p.public_key or self._pubkey]
        return None

    def refresh(self) -> None:
        self._data = self._load() if self._pubkey else None
        need = 0  # [trace-scroll patch] trace: grow with the number of links, then scroll
        if self._data is not None and self._data.get("mode") == "trace" and self._data.get("links"):
            need = trace_graph_height(len(self._data["links"]))
        self._area.set_content_height(need)
        self._area.queue_draw()

    def _load(self) -> dict:
        if self._mode == "trace":
            return self._load_traces()
        if self._mode == "packets":
            return self._load_packets()
        if self._mode == "battery":
            return self._load_battery()
        if self._mode == "neighbours":
            return self._load_neighbours()
        store = self._service.get_repeater_samples()
        t1 = time.time()
        t0 = t1 - self._range_s
        bucket = self._range_s / 240
        heard = store.series(self._pubkey, "heard", t0, bucket)
        stats = store.series(self._pubkey, "stats", t0, max(1.0, bucket))
        n = sum(r["n"] for r in heard)
        polls = sum(r["n"] for r in stats)
        self._info.set_text(f"{n} heard · {polls} polls")
        return {
            "t0": t0,
            "t1": t1,
            "heard": heard,
            "stats": stats,
            "gap": {"heard": max(4 * bucket, 1200.0), "stats": max(4 * bucket, 2.5 * 900)},
        }

    def _load_traces(self) -> dict:  # [trace-log patch]
        from meshcore_console.ui_gtk.widgets.node_badge import find_peer_for_hop

        store = self._service.get_repeater_samples()
        t1 = time.time()
        t0 = t1 - self._range_s
        traces = store.trace_series(self._pubkey, t0) if hasattr(store, "trace_series") else []
        fails = [tr["t"] for tr in traces if not tr["ok"]]
        ok = [tr for tr in traces if tr["ok"]]
        try:
            sf = int(self._service.get_settings().spreading_factor)
        except Exception:  # noqa: BLE001
            sf = 7
        if ok and ok[-1].get("sf"):
            sf = int(ok[-1]["sf"])
        try:
            peers = self._service.list_peers()
        except Exception:  # noqa: BLE001
            peers = []
        try:
            me = self._service.get_status().node_id or "This radio"
        except Exception:  # noqa: BLE001
            me = "This radio"

        def names(hop) -> str:
            if hop in (None, "me"):
                return me
            peer = find_peer_for_hop(peers, str(hop))
            return f"{peer.display_name} ({str(hop)[:4]})" if peer else str(hop)

        # typical spacing between traces -> gap that breaks the line
        spacing = (ok[-1]["t"] - ok[0]["t"]) / (len(ok) - 1) if len(ok) > 1 else self._range_s
        self._info.set_text(f"{len(traces)} traces · {len(fails)} failed")
        path = self._shown_path = self._current_path()  # [trace-path patch]
        return {
            "mode": "trace",
            "t0": t0,
            "t1": t1,
            "links": trace_links(traces, span=self._range_s, path=path),
            "fails": fails,
            "sf": sf,
            "limit": SNR_LIMIT_DB.get(sf, -7.5),
            "names": names,
            "gap": max(3 * spacing, self._range_s / 80),
        }

    def _load_packets(self) -> dict:  # [packet-graph patch]
        from meshcore_console.meshcore.repeater_stats import POLL_INTERVAL_S

        store = self._service.get_repeater_samples()
        t1 = time.time()
        t0 = t1 - self._range_s
        # one poll before the window so the first interval in it has a rate
        rows = store.stats_rows(self._pubkey, t0 - 3 * 3600) if hasattr(store, "stats_rows") else []
        if len(rows) > 2:
            gaps = sorted(b["t"] - a["t"] for a, b in zip(rows, rows[1:]))
            interval = gaps[len(gaps) // 2]
        else:
            interval = POLL_INTERVAL_S
        gap = max(3.0 * interval, 2.5 * POLL_INTERVAL_S)
        bucket = max(interval, self._range_s / 240)
        rates = packet_rates(rows, bucket, gap)
        rates["points"] = [p for p in rates["points"] if p["t"] >= t0]
        rates["reboots"] = [t for t in rates["reboots"] if t >= t0]
        polls = sum(1 for r in rows if r["t"] >= t0)
        boots = len(rates["reboots"])
        self._info.set_text(
            f"{polls} polls" + (f" · {boots} reboot{'s' if boots != 1 else ''}" if boots else "")
        )
        return {
            "mode": "packets",
            "t0": t0,
            "t1": t1,
            "points": rates["points"],
            "reboots": rates["reboots"],
            "latest": rows[-1] if rows else None,
            "gap": max(gap, 2.5 * bucket),
            "interval": interval,
        }

    def _load_neighbours(self) -> dict:  # [neighbour-graph patch]
        from meshcore_console.meshcore.repeater_stats import POLL_INTERVAL_S

        store = self._service.get_repeater_samples()
        t1 = time.time()
        t0 = t1 - self._range_s
        points = (
            store.neighbour_series(self._pubkey, t0) if hasattr(store, "neighbour_series") else []
        )
        self._info.set_text(
            f"{len(points)} poll{'s' if len(points) != 1 else ''}" if points else ""
        )
        gaps = sorted(b["t"] - a["t"] for a, b in zip(points, points[1:]))
        interval = gaps[len(gaps) // 2] if gaps else POLL_INTERVAL_S
        return {
            "mode": "neighbours",
            "t0": t0,
            "t1": t1,
            "points": points,
            "gap": max(3.0 * interval, 2.5 * POLL_INTERVAL_S),
            "interval": interval,
        }

    def _load_battery(self) -> dict:  # [battery-graph patch]
        from meshcore_console.meshcore.repeater_stats import POLL_INTERVAL_S

        store = self._service.get_repeater_samples()
        t1 = time.time()
        t0 = t1 - self._range_s
        bucket = max(float(POLL_INTERVAL_S), self._range_s / 240)
        points = (
            store.battery_series(self._pubkey, t0, bucket)
            if hasattr(store, "battery_series")
            else []
        )
        latest = store.latest(self._pubkey, "stats") if hasattr(store, "latest") else None
        polls = sum(p["n"] for p in points)
        self._info.set_text(f"{polls} poll{'s' if polls != 1 else ''}" if polls else "")
        power = store.power_series(self._pubkey, t0) if hasattr(store, "power_series") else []
        # boards that cannot detect their source answer n/a: no strip for those
        power = [p for p in power if p["src"] != "unknown"]
        return {
            "power": power,  # [power-source patch]
            "power_gap": max(3.0 * POLL_INTERVAL_S, 2.5 * bucket),
            "mode": "battery",
            "t0": t0,
            "t1": t1,
            "points": points,
            "latest_mv": (latest or {}).get("batt_mv") or None,
            "gap": max(2.5 * POLL_INTERVAL_S, 2.5 * bucket),
            "interval": POLL_INTERVAL_S,
        }

    def _on_mode(self, btn: Gtk.ToggleButton, mode: str) -> None:
        if btn.get_active():
            self._mode = mode
            self.refresh()

    def _tick(self) -> bool:
        if self._pubkey and self.get_mapped():
            self.refresh()
        return True

    def _on_range(self, btn: Gtk.ToggleButton, seconds: int) -> None:
        if btn.get_active():
            self._range_s = seconds
            self.refresh()

    def _on_motion(self, _ctrl, x: float, y: float) -> None:
        self._hover_x = x
        self._hover_y = y
        self._area.queue_draw()

    def _on_leave(self, _ctrl) -> None:
        self._hover_x = None
        self._area.queue_draw()

    def _draw(self, _area, cr, width: int, height: int) -> None:
        if self._data is None:
            _text(cr, width / 2, height / 2, "Select a repeater", MUTED, align="center")
            return
        if self._data.get("mode") == "trace":
            draw_trace_graph(cr, width, height, self._data, self._hover_x, self._hover_y)
            return
        if self._data.get("mode") == "packets":
            draw_packet_graph(cr, width, height, self._data, self._hover_x)
            return
        if self._data.get("mode") == "battery":
            draw_battery_graph(cr, width, height, self._data, self._hover_x)
            return
        if self._data.get("mode") == "neighbours":
            draw_neighbour_graph(cr, width, height, self._data, self._hover_x)
            return
        draw_graph(cr, width, height, self._data, self._hover_x)
