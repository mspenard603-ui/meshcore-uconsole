"""Repeaters > Log: the activity log, its trace lines, and what survives a restart."""

from __future__ import annotations

import sqlite3
import threading

import pytest

from meshcore_console.meshcore import activity_log
from meshcore_console.meshcore.activity_log import ActivityLog, log_trace_activity
from meshcore_console.meshcore.repeater_stats import RepeaterSampleStore


@pytest.fixture
def log(monkeypatch: pytest.MonkeyPatch) -> ActivityLog:
    fresh = ActivityLog(max_entries=5)
    monkeypatch.setattr(activity_log, "ACTIVITY", fresh)
    return fresh


def _store(path) -> RepeaterSampleStore:
    return RepeaterSampleStore(sqlite3.connect(str(path), check_same_thread=False))


def test_filter_by_repeater_key(log: ActivityLog) -> None:
    log.add("aa", "Hill", "Poll", "OK", ok=True)
    log.add("bb", "Ridge", "Poll", "Failed", ok=False)
    log.add("aa", "Hill (renamed)", "Setting", "Auto-poll off")
    assert [e.text for e in log.entries("aa")] == ["OK", "Auto-poll off"]
    assert [e.key for e in log.entries()] == ["aa", "bb", "aa"]
    assert log.entries("nobody") == []


def test_oldest_entries_drop_past_the_cap(log: ActivityLog) -> None:
    for i in range(8):
        log.add("aa", "Hill", "Poll", str(i))
    assert [e.text for e in log.entries()] == ["3", "4", "5", "6", "7"]


def test_listeners_see_entries_from_any_thread(log: ActivityLog) -> None:
    seen = []
    log.subscribe(seen.append)
    t = threading.Thread(target=log.add, args=("aa", "Hill", "Auto-login", "OK", True))
    t.start()
    t.join()
    assert [(e.kind, e.ok) for e in seen] == [("Auto-login", True)]


def test_log_survives_a_restart(tmp_path) -> None:
    db = tmp_path / "meshcore.db"
    first = ActivityLog()
    first.attach_store(_store(db))
    first.add("aa", "Hill", "Auto-login", "OK (guest)", ok=True)
    first.add("aa", "Hill", "Poll", "Failed: timeout", ok=False)
    first.add("bb", "Ridge", "Setting", "Auto-trace every 1 h")

    restarted = ActivityLog()
    restarted.attach_store(_store(db))
    got = [(e.repeater, e.kind, e.text, e.ok) for e in restarted.entries("aa")]
    assert got == [
        ("Hill", "Auto-login", "OK (guest)", True),
        ("Hill", "Poll", "Failed: timeout", False),
    ]
    assert restarted.entries("bb")[0].ok is None
    later = restarted.add("aa", "Hill", "Poll", "OK (300 ms)", ok=True)
    assert later.seq > restarted.entries("aa")[-2].seq  # numbering continues


def test_console_rows_and_state_survive_a_restart(tmp_path) -> None:
    db = tmp_path / "meshcore.db"
    store = _store(db)
    store.add_console("aa", "command", "ver")
    store.add_console("aa", "response", "v1.18.0")
    store.add_console("aa", "trace", "", {"name": "Hill", "result": {"success": True}})
    store.add_console("bb", "error", "timeout")
    store.set_state("aa", "cmd_history", ["ver", "neighbors"])
    store.set_state("aa", "trace_retry", [3, 2])

    again = _store(db)
    rows = again.console_rows("aa", 2000)
    assert [(r["kind"], r["text"]) for r in rows] == [
        ("command", "ver"),
        ("response", "v1.18.0"),
        ("trace", ""),
    ]
    assert rows[2]["data"] == {"name": "Hill", "result": {"success": True}}
    assert [r["text"] for r in again.console_rows("aa", 1)] == [""]  # newest only
    assert again.get_state("aa", "cmd_history") == ["ver", "neighbors"]
    assert again.get_state("aa", "trace_retry") == [3, 2]
    assert again.get_state("bb", "cmd_history") is None


def test_trace_success_names_source_and_weakest_link(log: ActivityLog) -> None:
    result = {
        "success": True,
        "route": ["AB"],
        "links": [{"snr": 4.25}, {"snr": -1.5}],
        "rtt_ms": 800,
    }
    log_trace_activity("aa", "Hill", result, "auto")
    (entry,) = log.entries("aa")
    assert entry.kind == "Auto-trace"
    assert entry.ok is True
    assert entry.text == "OK: 2 links, weakest SNR -1.50 dB, 800 ms"


def test_trace_timeout_is_a_failure_refusal_is_neutral(log: ActivityLog) -> None:
    log_trace_activity("aa", "Hill", {"success": False, "reason": "timeout", "route": []}, "map")
    log_trace_activity("aa", "Hill", {"success": False, "reason": "Radio not connected"}, None)
    failed, refused = log.entries("aa")
    assert (failed.kind, failed.ok, failed.text) == ("Trace (Map)", False, "Failed: timeout")
    assert (refused.kind, refused.ok) == ("Trace", None)
    assert refused.text == "Not sent: Radio not connected"


def test_mock_trace_repeater_logs_with_its_source(log: ActivityLog) -> None:
    from meshcore_console.meshcore.trace import trace_source
    from meshcore_console.mock.client import MockMeshcoreClient

    client = MockMeshcoreClient()
    peer = next(p for p in client.list_peers() if p.is_repeater)
    with trace_source("peers"):
        client.trace_repeater(peer.display_name)
    (entry,) = log.entries(peer.public_key or peer.peer_id)
    assert entry.kind == "Trace (Peers)"
    assert entry.repeater == peer.display_name
    assert entry.ok is True
