"""Repeater signal history and polled repeater stats. [repeater-stats patch]

Two kinds of sample are stored per repeater (keyed by full public key):

* ``heard`` - RSSI/SNR of a packet this radio received directly from the
  repeater. Taken passively from flood packets: the last hash in a flood path is
  the node that transmitted the copy we heard (or the sender itself when the
  path is empty). Packets whose last hop matches more than one known repeater
  are skipped rather than guessed.
* ``stats`` - the repeater's own RepeaterStats (REQ_TYPE_GET_STATUS): noise
  floor, RSSI/SNR of the last packet *it* heard, battery, counters. Needs a
  login (guest works).

Round-trip TRACE results go in their own table, one row per trace with the
per-link SNRs as JSON (``repeater_traces``). [trace-log patch]
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

RETENTION_DAYS = 30
POLL_INTERVAL_S = 15 * 60

_SCHEMA = """
CREATE TABLE IF NOT EXISTS repeater_samples (
    id INTEGER PRIMARY KEY,
    pubkey TEXT NOT NULL,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    rssi REAL,
    snr REAL,
    noise_floor REAL,
    batt_mv INTEGER,
    data TEXT
);
CREATE INDEX IF NOT EXISTS idx_repeater_samples ON repeater_samples (pubkey, kind, ts);
CREATE TABLE IF NOT EXISTS repeater_traces (
    id INTEGER PRIMARY KEY,
    pubkey TEXT NOT NULL,
    ts REAL NOT NULL,
    ok INTEGER NOT NULL,
    sf INTEGER,
    rtt_ms INTEGER,
    links TEXT,
    reason TEXT,
    source TEXT
);
CREATE INDEX IF NOT EXISTS idx_repeater_traces ON repeater_traces (pubkey, ts);
CREATE TABLE IF NOT EXISTS room_sync (
    pubkey TEXT PRIMARY KEY,
    sync_since INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS peer_types (
    pubkey TEXT PRIMARY KEY,
    node_type INTEGER NOT NULL,
    updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS repeater_activity (
    id INTEGER PRIMARY KEY,
    pubkey TEXT NOT NULL,
    name TEXT NOT NULL,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    text TEXT NOT NULL,
    ok INTEGER
);
CREATE INDEX IF NOT EXISTS idx_repeater_activity ON repeater_activity (ts);
CREATE TABLE IF NOT EXISTS repeater_console (
    id INTEGER PRIMARY KEY,
    pubkey TEXT NOT NULL,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    text TEXT NOT NULL,
    data TEXT
);
CREATE INDEX IF NOT EXISTS idx_repeater_console ON repeater_console (pubkey, id);
CREATE TABLE IF NOT EXISTS repeater_state (
    pubkey TEXT NOT NULL,
    name TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (pubkey, name)
);
CREATE TABLE IF NOT EXISTS repeater_info (
    pubkey TEXT NOT NULL,
    kind TEXT NOT NULL,
    value TEXT NOT NULL,
    first_ts REAL NOT NULL,
    last_ts REAL NOT NULL,
    n INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (pubkey, kind, value)
);
"""


def open_sample_store() -> "RepeaterSampleStore":
    """Store on its own connection to the app database, so writes from the poll
    thread never interleave with the app's own transactions (WAL handles the
    concurrency between connections)."""
    from meshcore_console.meshcore.paths import db_path

    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False, timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL")
    return RepeaterSampleStore(conn)


class RepeaterSampleStore:
    """SQLite-backed sample history (its own table; the app schema is untouched)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._lock = threading.Lock()
        self._last_prune = 0.0
        with self._lock:
            self._conn.executescript(_SCHEMA)
            try:  # tables created before the source column existed [trace-source patch]
                self._conn.execute("ALTER TABLE repeater_traces ADD COLUMN source TEXT")
            except sqlite3.OperationalError:
                pass  # already there
            self._conn.commit()

    def add(
        self,
        pubkey: str,
        kind: str,
        *,
        ts: float | None = None,
        rssi: float | None = None,
        snr: float | None = None,
        noise_floor: float | None = None,
        batt_mv: int | None = None,
        data: dict | None = None,
    ) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO repeater_samples (pubkey, ts, kind, rssi, snr, noise_floor, batt_mv, data) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    pubkey.lower(),
                    ts if ts is not None else now,
                    kind,
                    rssi,
                    snr,
                    noise_floor,
                    batt_mv,
                    json.dumps(data) if data else None,
                ),
            )
            if now - self._last_prune > 3600:
                self._conn.execute(
                    "DELETE FROM repeater_samples WHERE ts < ?", (now - RETENTION_DAYS * 86400,)
                )
                self._last_prune = now
            self._conn.commit()

    def series(self, pubkey: str, kind: str, since: float, bucket_s: float) -> list[dict]:
        """Samples since *since*, averaged into buckets of *bucket_s* seconds."""
        bucket_s = max(1.0, float(bucket_s))
        with self._lock:
            rows = self._conn.execute(
                "SELECT AVG(ts), AVG(rssi), MIN(rssi), MAX(rssi), AVG(snr), MIN(snr), MAX(snr), "
                "AVG(noise_floor), COUNT(*) FROM repeater_samples "
                "WHERE pubkey = ? AND kind = ? AND ts >= ? "
                "GROUP BY CAST(ts / ? AS INTEGER) ORDER BY 1",
                (pubkey.lower(), kind, since, bucket_s),
            ).fetchall()
        keys = (
            "t",
            "rssi",
            "rssi_min",
            "rssi_max",
            "snr",
            "snr_min",
            "snr_max",
            "noise_floor",
            "n",
        )
        return [dict(zip(keys, r)) for r in rows]

    def latest(self, pubkey: str, kind: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT ts, rssi, snr, noise_floor, batt_mv, data FROM repeater_samples "
                "WHERE pubkey = ? AND kind = ? ORDER BY ts DESC LIMIT 1",
                (pubkey.lower(), kind),
            ).fetchone()
        if row is None:
            return None
        out = {
            "ts": row[0],
            "rssi": row[1],
            "snr": row[2],
            "noise_floor": row[3],
            "batt_mv": row[4],
        }
        if row[5]:
            try:
                out.update(json.loads(row[5]))
            except ValueError:
                pass
        return out

    def stats_rows(self, pubkey: str, since: float) -> list[dict]:
        """Raw polled stats since *since*, oldest first: {"t", <counter>: value...}. [packet-graph patch]"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, data FROM repeater_samples WHERE pubkey = ? AND kind = 'stats' AND ts >= ? "
                "ORDER BY ts",
                (pubkey.lower(), since),
            ).fetchall()
        out = []
        for ts, data in rows:
            row = {"t": ts}
            if data:
                try:
                    row.update(json.loads(data))
                except ValueError:
                    pass
            out.append(row)
        return out

    def battery_series(self, pubkey: str, since: float, bucket_s: float) -> list[dict]:
        """Polled battery voltage since *since*, in buckets: {"t", "mv", "mv_min", "mv_max", "n"}. [battery-graph patch]"""
        bucket_s = max(1.0, float(bucket_s))
        with self._lock:
            rows = self._conn.execute(
                "SELECT AVG(ts), AVG(batt_mv), MIN(batt_mv), MAX(batt_mv), COUNT(*) FROM repeater_samples "
                "WHERE pubkey = ? AND kind = 'stats' AND ts >= ? AND batt_mv > 0 "
                "GROUP BY CAST(ts / ? AS INTEGER) ORDER BY 1",
                (pubkey.lower(), since, bucket_s),
            ).fetchall()
        return [dict(zip(("t", "mv", "mv_min", "mv_max", "n"), r)) for r in rows]

    def power_series(self, pubkey: str, since: float) -> list[dict]:
        """Power-source readings since *since* (one row before it too, for the state at
        the left edge), oldest first: {"t", "src", "batt_mv"}. [power-source patch]"""
        with self._lock:
            before = self._conn.execute(
                "SELECT ts, data, batt_mv FROM repeater_samples WHERE pubkey = ? AND kind = 'power' AND ts < ? "
                "ORDER BY ts DESC LIMIT 1",
                (pubkey.lower(), since),
            ).fetchall()
            rows = self._conn.execute(
                "SELECT ts, data, batt_mv FROM repeater_samples WHERE pubkey = ? AND kind = 'power' AND ts >= ? "
                "ORDER BY ts",
                (pubkey.lower(), since),
            ).fetchall()
        out = []
        for ts, data, mv in before + rows:
            try:
                src = (json.loads(data) if data else {}).get("src", "unknown")
            except ValueError:
                src = "unknown"
            out.append({"t": ts, "src": src, "batt_mv": mv})
        return out

    def neighbour_series(self, pubkey: str, since: float) -> list[dict]:
        """Polled neighbour counts since *since*, oldest first: {"t", "total", "h1", "h24", ...}. [neighbour-graph patch]"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, data FROM repeater_samples WHERE pubkey = ? AND kind = 'neighbours' AND ts >= ? "
                "ORDER BY ts",
                (pubkey.lower(), since),
            ).fetchall()
        out = []
        for ts, data in rows:
            try:
                d = json.loads(data) if data else {}
            except ValueError:
                continue
            d["t"] = ts
            out.append(d)
        return out

    # -- advert node types [rooms patch] -----------------------------------------
    # 1 companion, 2 repeater, 3 room server, 4 sensor. The app's peers table has
    # only is_repeater, so room servers and sensors are told apart from here.
    def set_node_type(self, pubkey: str, node_type: int) -> None:
        if not pubkey:
            return
        with self._lock:
            self._conn.execute(
                "INSERT INTO peer_types (pubkey, node_type, updated) VALUES (?, ?, ?) "
                "ON CONFLICT (pubkey) DO UPDATE SET node_type = excluded.node_type, updated = excluded.updated",
                (pubkey.lower(), int(node_type), time.time()),
            )
            self._conn.commit()

    def node_types(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute("SELECT pubkey, node_type FROM peer_types").fetchall()
        return {k: t for k, t in rows}

    # -- room servers [room-chat patch] --------------------------------------------
    # The newest post timestamp received from each room: sent as 'sync since' on
    # login, so the room only pushes posts we don't have yet.
    def room_sync(self, pubkey: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT sync_since FROM room_sync WHERE pubkey = ?", (pubkey.lower(),)
            ).fetchone()
        return int(row[0]) if row else 0

    def set_room_sync(self, pubkey: str, ts: int) -> None:
        if not pubkey or not ts:
            return
        with self._lock:
            self._conn.execute(
                "INSERT INTO room_sync (pubkey, sync_since) VALUES (?, ?) "
                "ON CONFLICT (pubkey) DO UPDATE SET sync_since = MAX(sync_since, excluded.sync_since)",
                (pubkey.lower(), int(ts)),
            )
            self._conn.commit()

    # -- reported hardware / firmware [board-history patch] ------------------
    def note_info(self, pubkey: str, kind: str, value: str, ts: float | None = None) -> None:
        """Record a reported value ('board', 'firmware'): first/last seen and count per value. Never pruned."""
        value = (value or "").strip()
        if not pubkey or not value:
            return
        ts = time.time() if ts is None else ts
        with self._lock:
            self._conn.execute(
                "INSERT INTO repeater_info (pubkey, kind, value, first_ts, last_ts, n) VALUES (?, ?, ?, ?, ?, 1) "
                "ON CONFLICT (pubkey, kind, value) DO UPDATE SET last_ts = MAX(last_ts, excluded.last_ts), n = n + 1",
                (pubkey.lower(), kind, value[:120], ts, ts),
            )
            self._conn.commit()

    def info_history(self, pubkey: str, kind: str) -> list[dict]:
        """Every value reported for *kind*, most recently seen first: {"value","first","last","n"}."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT value, first_ts, last_ts, n FROM repeater_info WHERE pubkey = ? AND kind = ? "
                "ORDER BY last_ts DESC",
                (pubkey.lower(), kind),
            ).fetchall()
        return [{"value": v, "first": f, "last": la, "n": n} for v, f, la, n in rows]

    # -- round-trip traces [trace-log patch] ---------------------------------
    def add_trace(
        self,
        pubkey: str,
        result: dict,
        *,
        sf: int | None = None,
        ts: float | None = None,
        source: str | None = None,
    ) -> None:
        """Log one trace. *result* is run_trace()'s dict (success, links, rtt_ms, reason)."""
        now = time.time()
        ok = bool(result.get("success"))
        links = [[lk.get("from"), lk.get("to"), lk.get("snr")] for lk in result.get("links") or []]
        with self._lock:
            self._conn.execute(
                "INSERT INTO repeater_traces (pubkey, ts, ok, sf, rtt_ms, links, reason, source) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    pubkey.lower(),
                    ts if ts is not None else now,
                    1 if ok else 0,
                    sf,
                    result.get("rtt_ms"),
                    json.dumps(links) if ok else None,
                    None if ok else str(result.get("reason") or "")[:200],
                    source,
                ),
            )
            self._conn.execute(
                "DELETE FROM repeater_traces WHERE ts < ?", (now - RETENTION_DAYS * 86400,)
            )
            self._conn.commit()

    # --- [persist patch] Repeaters > Log, console history, small per-repeater state.
    # Keyed by the app's repeater key (public key, else peer id) exactly as given.

    def add_activity(
        self, pubkey: str, name: str, kind: str, text: str, ok: bool | None, ts: float
    ) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO repeater_activity (pubkey, name, ts, kind, text, ok) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (pubkey, name, ts, kind, text, None if ok is None else int(ok)),
            )
            self._prune_persisted(ts)
            self._conn.commit()
            return int(cur.lastrowid)

    def activity_rows(self, limit: int) -> list[dict]:
        """The newest *limit* activity entries, oldest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, pubkey, name, ts, kind, text, ok FROM repeater_activity "
                "ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {
                "id": r[0],
                "pubkey": r[1],
                "name": r[2],
                "ts": r[3],
                "kind": r[4],
                "text": r[5],
                "ok": None if r[6] is None else bool(r[6]),
            }
            for r in reversed(rows)
        ]

    def add_console(self, pubkey: str, kind: str, text: str, data: dict | None = None) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO repeater_console (pubkey, ts, kind, text, data) VALUES (?, ?, ?, ?, ?)",
                (
                    pubkey,
                    now,
                    kind,
                    text,
                    json.dumps(data, default=str) if data is not None else None,
                ),
            )
            self._prune_persisted(now)
            self._conn.commit()

    def console_rows(self, pubkey: str, limit: int) -> list[dict]:
        """The newest *limit* console rows of one repeater, oldest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, kind, text, data FROM repeater_console WHERE pubkey = ? "
                "ORDER BY id DESC LIMIT ?",
                (pubkey, limit),
            ).fetchall()
        return [
            {"ts": r[0], "kind": r[1], "text": r[2], "data": json.loads(r[3]) if r[3] else None}
            for r in reversed(rows)
        ]

    def get_state(self, pubkey: str, name: str) -> Any:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM repeater_state WHERE pubkey = ? AND name = ?", (pubkey, name)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def set_state(self, pubkey: str, name: str, value: Any) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO repeater_state (pubkey, name, value) VALUES (?, ?, ?)",
                (pubkey, name, json.dumps(value)),
            )
            self._conn.commit()

    def _prune_persisted(self, now: float) -> None:
        """Hourly, under the lock: activity and console rows older than the retention."""
        if now - getattr(self, "_last_persist_prune", 0.0) < 3600:
            return
        cutoff = now - RETENTION_DAYS * 86400
        self._conn.execute("DELETE FROM repeater_activity WHERE ts < ?", (cutoff,))
        self._conn.execute("DELETE FROM repeater_console WHERE ts < ?", (cutoff,))
        self._last_persist_prune = now

    def trace_series(self, pubkey: str, since: float) -> list[dict]:
        """Traces since *since*, oldest first: {t, ok, sf, rtt_ms, links:[[from,to,snr]], reason}."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, ok, sf, rtt_ms, links, reason, source FROM repeater_traces "
                "WHERE pubkey = ? AND ts >= ? ORDER BY ts",
                (pubkey.lower(), since),
            ).fetchall()
        out = []
        for ts, ok, sf, rtt, links, reason, source in rows:
            try:
                parsed = json.loads(links) if links else []
            except ValueError:
                parsed = []
            out.append(
                {
                    "t": ts,
                    "ok": bool(ok),
                    "sf": sf,
                    "rtt_ms": rtt,
                    "links": parsed,
                    "reason": reason,
                    "source": source,
                }
            )
        return out

    def summary(self, pubkey: str, kind: str, since: float) -> dict:
        with self._lock:
            n, rssi, snr, last = self._conn.execute(
                "SELECT COUNT(*), AVG(rssi), AVG(snr), MAX(ts) FROM repeater_samples "
                "WHERE pubkey = ? AND kind = ? AND ts >= ?",
                (pubkey.lower(), kind, since),
            ).fetchone()
        return {"n": n or 0, "rssi": rssi, "snr": snr, "last": last}


# -- Passive capture ------------------------------------------------------------


def heard_transmitter(data: dict) -> str | None:
    """Hash (hex, lower) of the node whose transmission we received, or None.

    Only flood packets qualify: in a flood path each relay appends its hash, so
    the last entry is the node we heard. Direct packets carry the *remaining*
    route instead, and TRACE paths hold SNR bytes, so both are skipped.
    """
    if str(data.get("payload_type_name") or "").upper() == "TRACE":
        return None
    if "FLOOD" not in str(data.get("route_type_name") or "").upper():
        return None
    hops = data.get("path_hops") or []
    if hops:
        return str(hops[-1]).lower()
    sender = data.get("sender_pubkey")
    return str(sender).lower() if sender else None


def match_repeater(hop: str, repeaters: list[Any]) -> str | None:
    """Public key of the only repeater whose key starts with *hop*; None if 0 or >1."""
    hop = hop.lower()
    found = {
        p.public_key.lower()
        for p in repeaters
        if p.public_key and p.public_key.lower().startswith(hop)
    }
    return found.pop() if len(found) == 1 else None


# -- Active polling ---------------------------------------------------------------


def _response_key(handler: Any, contact: Any, tag: int) -> tuple:
    """Waiter key for protocol_response_handler, for either library API.

    Newer openhop_core: set_response_callback(pubkey, request_tag, callback),
    matched on the full public key plus the tag the firmware echoes back.
    Older builds: set_response_callback(first_pubkey_byte, callback).
    """
    import inspect

    pubkey = bytes.fromhex(contact.public_key)
    try:
        n = len(inspect.signature(handler.set_response_callback).parameters)
    except (TypeError, ValueError):
        n = 3
    return (pubkey, tag & 0xFFFFFFFF) if n >= 3 else (pubkey[0],)


def _forget_route(node: Any, contact_name: str) -> None:
    """After a timeout over a multi-hop route, clear it so the next request floods."""
    contacts = getattr(node, "contacts", None)
    contact = contacts.get_by_name(contact_name) if contacts is not None else None
    if (
        contact is not None
        and getattr(contact, "out_path_len", -1) > 0
        and (contact.out_path_len & 0x3F)
    ):
        contact.out_path = []
        contact.out_path_len = -1


async def request_status(*, node: Any, contact_name: str, timeout: float = 10.0) -> dict:
    """Send REQ_TYPE_GET_STATUS to a repeater and wait for its RepeaterStats."""
    from openhop_core.protocol.constants import REQ_TYPE_GET_STATUS
    from openhop_core.protocol.packet_builder import PacketBuilder

    from meshcore_console.meshcore.operations import _resolve_contact

    contact = _resolve_contact(node, contact_name)
    pkt, tag = PacketBuilder.create_protocol_request(contact, node.identity, REQ_TYPE_GET_STATUS)
    handler = node.dispatcher.protocol_response_handler
    event = asyncio.Event()
    result: dict = {}

    def _on_response(success: bool, text: str, parsed: dict) -> None:
        result.update(success=success, text=text, parsed=parsed)
        event.set()

    key = _response_key(handler, contact, tag)
    handler.set_response_callback(*key, _on_response)
    t0 = time.monotonic()
    try:
        await node.dispatcher.send_packet(pkt, wait_for_ack=False)
        await asyncio.wait_for(event.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        _forget_route(node, contact_name)
        return {"success": False, "reason": f"No response after {timeout:.0f}s"}
    finally:
        handler.clear_response_callback(*key)

    parsed = result.get("parsed") or {}
    if not result.get("success") or parsed.get("type") != "stats":
        return {"success": False, "reason": f"Unexpected reply: {result.get('text', '')[:60]}"}
    return {"success": True, "stats": parsed, "rtt_ms": round((time.monotonic() - t0) * 1000)}


_STAT_FIELDS = (
    "curr_tx_queue_len",
    "n_packets_recv",
    "n_packets_sent",
    "total_air_time_secs",
    "total_up_time_secs",
    "n_sent_flood",
    "n_sent_direct",
    "n_recv_flood",
    "n_recv_direct",
    "err_events",
    "n_direct_dups",
    "n_flood_dups",
    "total_rx_air_time_secs",
    "n_recv_errors",
)


def stats_sample(stats: dict) -> dict:
    """Map a parsed RepeaterStats dict to RepeaterSampleStore.add() keyword args."""
    return {
        "rssi": stats.get("last_rssi"),
        "snr": stats.get("last_snr"),
        "noise_floor": stats.get("noise_floor"),
        "batt_mv": stats.get("batt_milli_volts"),
        "data": {k: stats[k] for k in _STAT_FIELDS if k in stats},
    }


# -- Power source from the 'power' CLI command [power-source patch] ---------------
# Firmware v1.18.0+ (admin only):
#   batt:4123 mV usb:yes|no|n/a solar_chg:yes|no|n/a ext:yes|no charger:yes|no
# n/a = the board cannot detect it. ext defaults to 'no' on boards that cannot
# detect external power, so 'no' everywhere only means 'battery' when the board
# detects at least USB or solar.
POWER_SOURCES = {
    "usb": "USB power",
    "dc": "External DC power",
    "solar": "Solar charging",
    "battery": "On battery",
    "unknown": "Power source not detectable on this board",
}
_POWER_RE = re.compile(
    r"batt:\s*(\d+)\s*mV\s+usb:(\S+)\s+solar_chg:(\S+)\s+ext:(\S+)\s+charger:(\S+)", re.I
)


def parse_power(text: str) -> dict | None:
    """Parse a 'power' reply: {"batt_mv", "usb", "solar", "ext", "charger", "src"}, or None."""
    m = _POWER_RE.search(text or "")
    if m is None:
        return None
    mv, usb, solar, ext, charger = (m.group(1),) + tuple(g.lower() for g in m.groups()[1:])
    if usb == "yes":
        src = "usb"
    elif solar == "yes":
        src = "solar"
    elif ext == "yes" or charger == "yes":
        src = "dc"
    elif usb == "no" or solar == "no":
        src = "battery"
    else:
        src = "unknown"
    return {
        "batt_mv": int(mv),
        "usb": usb,
        "solar": solar,
        "ext": ext,
        "charger": charger,
        "src": src,
    }


# -- Neighbour list by request [neighbours-request patch] -------------------------
# REQ_TYPE_GET_NEIGHBOURS (simple_repeater MyMesh.cpp). Unlike the 'neighbors'
# CLI command, which the firmware only accepts from admin logins, this request
# is answered for guest logins too. Request payload after the type byte:
#   version(1)=0, count(1), offset(uint16 LE), order_by(1), pubkey_prefix_len(1),
#   random(4)
# Reply after the 4-byte tag:
#   total(int16 LE), returned(int16 LE), then per neighbour:
#   pubkey_prefix(prefix_len), heard_seconds_ago(uint32 LE), snr(int8, SNR x 4)
# A reply holds at most 130 bytes of entries (14 at a 4-byte prefix), so longer
# lists are fetched in pages by offset.
REQ_TYPE_GET_NEIGHBOURS = 0x06
NEIGHBOURS_NEWEST_FIRST = 0


def parse_neighbours(data: bytes, prefix_len: int) -> tuple[int, list[tuple[str, int, int]]]:
    """(total, [(hex_prefix, seconds_ago, snr_x4)]) from a tag-prefixed reply."""
    if len(data) < 8:
        raise ValueError(f"neighbours reply too short ({len(data)} bytes)")
    total = int.from_bytes(data[4:6], "little", signed=True)
    count = int.from_bytes(data[6:8], "little", signed=True)
    size = prefix_len + 5
    rows: list[tuple[str, int, int]] = []
    off = 8
    for _ in range(max(0, count)):
        if off + size > len(data):
            break
        key = data[off : off + prefix_len].hex().upper()
        secs = int.from_bytes(data[off + prefix_len : off + prefix_len + 4], "little")
        snr = data[off + prefix_len + 4]
        rows.append((key, secs, snr - 256 if snr > 127 else snr))
        off += size
    return max(0, total), rows


async def _request_raw(node: Any, contact: Any, pkt: Any, tag: int, timeout: float) -> bytes | None:
    """Send *pkt* and return the raw tag-prefixed reply, or None on timeout.

    openhop_core has no parser for this reply and could mistake it for telemetry
    or stats, so the handler's parser is wrapped for this one tag only.
    """
    handler = node.dispatcher.protocol_response_handler
    tag &= 0xFFFFFFFF
    previous = handler.__dict__.get("_parse_protocol_response")
    inner = handler._parse_protocol_response

    def _parse(data: bytes):
        if len(data) >= 8 and int.from_bytes(bytes(data[:4]), "little") == tag:
            return True, "neighbours", {"type": "neighbours", "raw": bytes(data)}
        return inner(data)

    handler._parse_protocol_response = _parse
    event = asyncio.Event()
    got: dict = {}

    def _on_response(success: bool, text: str, parsed: dict) -> None:
        got.update(parsed or {})
        event.set()

    key = _response_key(handler, contact, tag)
    handler.set_response_callback(*key, _on_response)
    try:
        await node.dispatcher.send_packet(pkt, wait_for_ack=False)
        await asyncio.wait_for(event.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        return None
    finally:
        handler.clear_response_callback(*key)
        if handler.__dict__.get("_parse_protocol_response") is _parse:
            if previous is None:
                del handler._parse_protocol_response
            else:
                handler._parse_protocol_response = previous
    return got.get("raw")


async def request_neighbours(
    *, node: Any, contact_name: str, timeout: float = 10.0, prefix_len: int = 4, max_pages: int = 4
) -> dict:
    """Fetch a repeater's neighbour list with REQ_TYPE_GET_NEIGHBOURS (guest login is enough).

    Returns {"success", "rows": [(hex, secs_ago, snr_x4)], "total", "text", "rtt_ms"}, where
    "text" is in the CLI 'neighbors' format (AABBCCDD:secs:snr_x4 per line, or '-none-').
    """
    import os

    from openhop_core.protocol.packet_builder import PacketBuilder

    from meshcore_console.meshcore.operations import _resolve_contact

    contact = _resolve_contact(node, contact_name)
    rows: list[tuple[str, int, int]] = []
    total = 0
    t0 = time.monotonic()
    for page in range(max_pages):
        data = bytes(
            [0, 255, len(rows) & 0xFF, len(rows) >> 8, NEIGHBOURS_NEWEST_FIRST, prefix_len]
        )
        pkt, tag = PacketBuilder.create_protocol_request(
            contact, node.identity, REQ_TYPE_GET_NEIGHBOURS, data + os.urandom(4)
        )
        raw = await _request_raw(node, contact, pkt, tag, timeout)
        if raw is None:
            if page == 0:
                _forget_route(node, contact_name)
                return {"success": False, "reason": f"No response after {timeout:.0f}s"}
            break  # keep the pages we have
        try:
            total, entries = parse_neighbours(raw, prefix_len)
        except ValueError as exc:
            return {"success": False, "reason": str(exc)}
        rows += entries
        if not entries or len(rows) >= total:
            break
    text = "\n".join(f"{k}:{s}:{n}" for k, s, n in rows) if rows else "-none-"
    return {
        "success": True,
        "rows": rows,
        "total": total,
        "text": text,
        "rtt_ms": round((time.monotonic() - t0) * 1000),
    }


def neighbour_counts(rows: list[tuple[str, int, int]], total: int) -> dict:
    """Summary of a neighbour list for the graph. [neighbour-graph patch]

    The firmware keeps up to MAX_NEIGHBOURS (50 on most builds) entries and never
    expires them, so *total* only grows (or stays full); how many were heard
    recently is what shows the neighbourhood changing.
    """
    h1 = [snr for _k, secs, snr in rows if 0 <= secs <= 3600]
    h24 = [snr for _k, secs, snr in rows if 0 <= secs <= 86400]
    return {
        "total": int(total),
        "listed": len(rows),
        "h1": len(h1),
        "h24": len(h24),
        "snr_h24": round(sum(h24) / len(h24) / 4, 2) if h24 else None,
    }


# -- Owner info by request [owner-info patch] -------------------------------------
# REQ_TYPE_GET_OWNER_INFO (firmware level >= 2): any login, guest included.
# Reply after the 4-byte tag: "firmware_version\nnode_name\nowner_info" where
# owner_info may itself hold newlines (set with 'set owner.info a|b|c').
REQ_TYPE_GET_OWNER_INFO = 0x07


def parse_owner_info(data: bytes) -> dict:
    text = bytes(data[4:]).rstrip(b"\x00").decode("utf-8", errors="replace")
    parts = text.split("\n", 2)
    while len(parts) < 3:
        parts.append("")
    return {"firmware": parts[0].strip(), "name": parts[1].strip(), "owner": parts[2].strip()}


async def request_owner_info(*, node: Any, contact_name: str, timeout: float = 10.0) -> dict:
    """{"success", "firmware", "name", "owner", "rtt_ms"} via REQ_TYPE_GET_OWNER_INFO."""
    import os

    from openhop_core.protocol.packet_builder import PacketBuilder

    from meshcore_console.meshcore.operations import _resolve_contact

    contact = _resolve_contact(node, contact_name)
    pkt, tag = PacketBuilder.create_protocol_request(
        contact, node.identity, REQ_TYPE_GET_OWNER_INFO, os.urandom(4)
    )
    t0 = time.monotonic()
    raw = await _request_raw(node, contact, pkt, tag, timeout)
    if raw is None:
        _forget_route(node, contact_name)
        return {"success": False, "reason": f"No response after {timeout:.0f}s"}
    info = parse_owner_info(raw)
    info.update(success=True, rtt_ms=round((time.monotonic() - t0) * 1000))
    return info


def format_owner_info(info: dict) -> str:
    owner = info.get("owner") or "(not set)"
    lines = owner.splitlines() or [owner]
    out = [
        f"Name      {info.get('name') or '?'}",
        f"Firmware  {info.get('firmware') or '?'}",
        f"Owner     {lines[0]}",
    ]
    out += [f"          {line}" for line in lines[1:]]
    return "\n".join(out)
