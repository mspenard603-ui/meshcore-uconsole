"""Airtime rules for timed requests (auto-poll, auto-login), after the 2026-11-10 flooding report."""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from meshcore_console.meshcore import airtime_policy as ap
from meshcore_console.meshcore import schedule_store


@pytest.fixture(autouse=True)
def _fresh(tmp_path, monkeypatch):
    from meshcore_console.meshcore import paths

    monkeypatch.setattr(paths, "config_dir", lambda: tmp_path)
    schedule_store.reset_cache()
    ap.ROUTE_MISSES._misses.clear()
    yield
    schedule_store.reset_cache()


# --- intervals ----------------------------------------------------------------
def test_flood_only_repeater_polled_at_most_hourly():
    assert ap.effective_poll_interval(60, True, 0) == 60  # antenna alignment, with a route
    assert ap.effective_poll_interval(60, False, 0) == ap.FLOOD_MIN_INTERVAL_S
    assert ap.effective_poll_interval(21600, False, 0) == 21600


def test_failures_back_off_and_cap():
    assert ap.effective_poll_interval(300, True, 1) == 600
    assert ap.effective_poll_interval(300, True, 3) == 2400
    assert ap.effective_poll_interval(3600, True, 10) == ap.MAX_BACKOFF_S


# --- routes -------------------------------------------------------------------
def _contact(hops_byte):
    return SimpleNamespace(out_path=[1, 2], out_path_len=hops_byte)


def test_route_forgotten_only_after_two_misses_in_a_row():
    c = _contact(0x42)
    assert not ap.forget_route_after_misses(c, "R")  # one slow reply: keep the route
    assert c.out_path_len == 0x42
    assert ap.forget_route_after_misses(c, "R")  # second in a row: forget
    assert (c.out_path_len, c.out_path) == (-1, [])


def test_a_reply_resets_the_count():
    c = _contact(0x41)
    ap.forget_route_after_misses(c, "R")
    ap.ROUTE_MISSES.ok("R")
    assert not ap.forget_route_after_misses(c, "R")
    assert c.out_path_len == 0x41


def test_zero_hop_route_is_forgotten_too():
    """A far repeater heard directly gets a zero-hop route from its advert; if it doesn't answer,
    that route goes too (before, only multi-hop routes were forgotten)."""
    c = _contact(0)
    ap.forget_route_after_misses(c, "Far")
    assert ap.forget_route_after_misses(c, "Far") and c.out_path_len == -1


def test_no_route_nothing_to_forget():
    c = _contact(-1)
    assert not ap.forget_route_after_misses(c, "R") and not ap.forget_route_after_misses(c, "R")


def test_request_status_timeout_uses_the_miss_counter():
    from meshcore_console.meshcore import repeater_stats

    c = _contact(0x41)
    node = SimpleNamespace(contacts=SimpleNamespace(get_by_name=lambda n: c))
    repeater_stats._forget_route(node, "R")
    assert c.out_path_len == 0x41
    repeater_stats._forget_route(node, "R")
    assert c.out_path_len == -1


# --- schedules ----------------------------------------------------------------
def test_poll_off_unless_chosen_and_pause_all():
    assert schedule_store.poll_interval("k1") == 0
    schedule_store.set_poll_interval("k1", 300)
    schedule_store.set_poll_interval("k2", 3600)
    schedule_store.set_poll_interval("k3", 0)
    assert sorted(schedule_store.pause_all_polls()) == ["k1", "k2"]
    assert schedule_store.paused_polls() == {"k1", "k2"}
    assert schedule_store.poll_interval("k1") == 300  # kept
    schedule_store.set_poll_interval("k1", 900)  # choosing one resumes
    assert "k1" not in schedule_store.paused_polls()
    schedule_store.reset_cache()  # persisted
    assert schedule_store.paused_polls() == {"k2"} and schedule_store.poll_interval("k1") == 900


def test_clear_traces():
    schedule_store.set_trace_interval("k1", 60)
    schedule_store.set_trace_interval("k2", 300)
    assert sorted(schedule_store.clear_traces()) == ["k1", "k2"]
    schedule_store.reset_cache()
    assert schedule_store.trace_intervals() == {}


# --- Admin: poll loop and auto-login gating (methods run on a stand-in self) ----
def _admin(direct: dict[str, bool], logged_in=("R",)):
    from meshcore_console.ui_gtk.views.admin import AdminView

    polled: list[str] = []
    logins: list[str] = []
    service = SimpleNamespace(
        list_logged_in_repeaters=lambda: list(logged_in),
        has_direct_route=lambda n: direct.get(n, False),
        get_status=lambda: SimpleNamespace(connected=True),
        list_peers=lambda: [
            SimpleNamespace(display_name=n, is_repeater=True, public_key=n, peer_id=n)
            for n in direct
        ],
    )
    a = SimpleNamespace(
        _busy=False,
        _cmd_queue=[],
        _service=service,
        _last_poll={},
        _poll_fails={},
        _route_recovery=set(),
        _autologin_suppressed=set(),
        _relogin_needed=set(),
        _autologin_next={},
        _auto_trace_due=lambda now: False,
        _repeater_key=lambda n: n,
        _autologin_enabled=lambda n: True,
        _poll_repeater=lambda n: polled.append(n),
        _auto_login=lambda n, relogin=False: logins.append(n),
    )
    a._poll_interval_for = lambda n: AdminView._poll_interval_for(a, n)
    a._auto_login_due = lambda now: AdminView._auto_login_due(a, now)
    a.poll = lambda now: AdminView._poll_due_repeaters(a)
    a.login = lambda now: AdminView._auto_login_due(a, now)
    return a, polled, logins


def test_not_polled_without_an_interval():
    a, polled, _ = _admin({"R": True})
    a.poll(time.time())
    assert polled == []


def test_one_minute_poll_only_with_a_route():
    schedule_store.set_poll_interval("R", 60)
    a, polled, _ = _admin({"R": True})
    a._last_poll["R"] = time.time() - 70
    a.poll(time.time())
    assert polled == ["R"]
    a, polled, _ = _admin({"R": False})  # no route: the 1 h flood minimum
    a._last_poll["R"] = time.time() - 70
    a.poll(time.time())
    assert polled == []


def test_paused_poll_not_sent():
    schedule_store.set_poll_interval("R", 300)
    schedule_store.pause_poll("R")
    a, polled, _ = _admin({"R": True})
    a.poll(time.time())
    assert polled == []


def test_auto_login_only_over_a_route_or_once_after_it_was_forgotten():
    a, _, logins = _admin({"R": False}, logged_in=())
    a.login(time.time())
    assert logins == []  # would flood: held off
    a._route_recovery.add("R")
    a.login(time.time())
    assert logins == ["R"] and "R" not in a._route_recovery  # the one recovery round
    a, _, logins = _admin({"R": True}, logged_in=())
    a.login(time.time())
    assert logins == ["R"]
