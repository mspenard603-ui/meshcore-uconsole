"""Log of background repeater activity: logins, stats polls, traces, settings.

Shown under Repeaters > Log, one repeater at a time. Entries are added from worker
threads as well as the GTK main loop, so the log is lock-protected and listeners
must hop to the main loop themselves (``GLib.idle_add``).

Once a store is attached (the radio client does it at start-up) every entry is
written to the app database and the newest ones are loaded back, so the log
survives restarts; the store prunes rows older than its retention (30 days).
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

MAX_ENTRIES = 5000  # in memory, all repeaters together

# Who started a trace (meshcore.trace.trace_source) -> how the log names it.
TRACE_SOURCES = {
    "auto": "Auto-trace",
    "admin": "Trace (Repeaters)",
    "map": "Trace (Map)",
    "peers": "Trace (Peers)",
    "channels": "Trace (Channels)",
}


@dataclass(frozen=True)
class ActivityEntry:
    seq: int
    key: str  # repeater key: public key, else peer id
    repeater: str  # display name when logged
    kind: str  # short label shown in the log, e.g. "Auto-login", "Poll"
    text: str
    ok: bool | None = None  # None = neutral (a setting change, a note)
    ts: float = field(default_factory=time.time)


class ActivityLog:
    def __init__(self, max_entries: int = MAX_ENTRIES) -> None:
        self._entries: deque[ActivityEntry] = deque(maxlen=max_entries)
        self._lock = threading.Lock()
        self._seq = 0
        self._listeners: list[Callable[[ActivityEntry], None]] = []
        self._store: Any = None

    def attach_store(self, store: Any) -> None:
        """Persist to *store* (a RepeaterSampleStore) and load what it already holds."""
        try:
            rows = store.activity_rows(self._entries.maxlen or MAX_ENTRIES)
        except Exception:  # noqa: BLE001 - a broken log must not stop the radio
            logger.exception("could not load the repeater activity log")
            rows = []
        with self._lock:
            self._store = store
            loaded = [
                ActivityEntry(
                    r["id"], r["pubkey"], r["name"], r["kind"], r["text"], r["ok"], r["ts"]
                )
                for r in rows
            ]
            self._entries.extend(loaded)
            if loaded:
                self._seq = max(self._seq, loaded[-1].seq)

    def add(
        self, key: str, repeater: str, kind: str, text: str, ok: bool | None = None
    ) -> ActivityEntry:
        ts = time.time()
        with self._lock:
            store = self._store
        seq = None
        if store is not None:
            try:
                seq = store.add_activity(key, repeater, kind, text, ok, ts)
            except Exception:  # noqa: BLE001
                logger.exception("could not save repeater activity")
        with self._lock:
            self._seq = max(self._seq + 1, seq or 0)
            entry = ActivityEntry(self._seq, key, repeater, kind, text, ok, ts)
            self._entries.append(entry)
            listeners = list(self._listeners)
        for listener in listeners:
            listener(entry)
        return entry

    def entries(self, key: str | None = None) -> list[ActivityEntry]:
        """Oldest first; only that repeater's entries when *key* is given."""
        with self._lock:
            items = list(self._entries)
        if key is None:
            return items
        return [e for e in items if e.key == key]

    def subscribe(self, listener: Callable[[ActivityEntry], None]) -> None:
        """*listener* runs on the thread that added the entry."""
        with self._lock:
            self._listeners.append(listener)


ACTIVITY = ActivityLog()


def log_trace_activity(key: str, repeater: str, result: dict, source: str | None) -> None:
    """One log line for a finished trace, whoever started it."""
    kind = TRACE_SOURCES.get(source or "", "Trace")
    if result.get("success"):
        links = result.get("links") or []
        snrs = [lk["snr"] for lk in links if lk.get("snr") is not None]
        text = f"OK: {len(links)} link{'s' if len(links) != 1 else ''}"
        if snrs:
            text += f", weakest SNR {min(snrs):+.2f} dB"
        if result.get("rtt_ms"):
            text += f", {result['rtt_ms']} ms"
        ACTIVITY.add(key, repeater, kind, text, ok=True)
    elif result.get("busy") or "route" not in result:
        # Refused before going on air (another trace running, not connected, ...)
        ACTIVITY.add(key, repeater, kind, f"Not sent: {result.get('reason', 'refused')}", ok=None)
    else:
        ACTIVITY.add(
            key, repeater, kind, f"Failed: {result.get('reason', 'no response')}", ok=False
        )
