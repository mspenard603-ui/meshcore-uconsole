"""Airtime rules for requests sent on a timer (auto-poll, auto-login).

A neighbouring mesh reported (2026-11-10) one station flooding half their request traffic:
repeater polls and logins sent by flood every few minutes, with flood retries. Every flood is
retransmitted by every repeater in reach, so a background request must count its worst case.
These are the rules MeshCore Commsol's poller follows, measured on air there:

* Without a known route a request floods: a repeater with no direct route is polled at most
  every FLOOD_MIN_INTERVAL_S, whatever its interval.
* Consecutive failures back off exponentially (capped at MAX_BACKOFF_S); after
  MAX_POLL_FAILURES the auto-poll pauses until its interval is set again.
* A route is forgotten only after ROUTE_MISSES_FORGET unanswered requests in a row (one slow
  or lost reply is not a dead route); the next request then floods once and its reply teaches
  a route again.
* Auto-login is sent only over a known route, or once right after a route was forgotten (the
  recovery round), and stops after MAX_AUTOLOGIN_FAILURES failures in a row.
* Extra requests after a poll (neighbour table, power) only go over a known route.
"""

from __future__ import annotations

import threading

FLOOD_MIN_INTERVAL_S = 3600
MAX_BACKOFF_S = 24 * 3600
MAX_POLL_FAILURES = 5
MAX_AUTOLOGIN_FAILURES = 5
ROUTE_MISSES_FORGET = 2


def effective_poll_interval(base_s: float, direct: bool, failures: int) -> float:
    """Seconds between polls: the chosen interval, at least FLOOD_MIN_INTERVAL_S without a
    route, doubled per consecutive failure (capped at MAX_BACKOFF_S, or the interval if longer)."""
    interval = float(base_s)
    if not direct:
        interval = max(interval, FLOOD_MIN_INTERVAL_S)
    if failures > 0:
        interval = min(interval * (2**failures), max(MAX_BACKOFF_S, interval))
    return interval


class RouteMisses:
    """Unanswered requests in a row, per contact name (any reply resets the count)."""

    def __init__(self, threshold: int = ROUTE_MISSES_FORGET):
        self.threshold = threshold
        self._misses: dict[str, int] = {}
        self._lock = threading.Lock()

    def miss(self, name: str) -> bool:
        """Count a timeout; True when the route should be forgotten (and the count restarts)."""
        with self._lock:
            n = self._misses.get(name, 0) + 1
            if n >= self.threshold:
                self._misses.pop(name, None)
                return True
            self._misses[name] = n
            return False

    def ok(self, name: str) -> None:
        with self._lock:
            self._misses.pop(name, None)

    def count(self, name: str) -> int:
        with self._lock:
            return self._misses.get(name, 0)


ROUTE_MISSES = RouteMisses()


def has_route(contact: object) -> bool:
    """A contact with a stored route (zero-hop or multi-hop) is sent to directly."""
    return contact is not None and getattr(contact, "out_path_len", -1) >= 0


def forget_route_after_misses(contact: object, name: str) -> bool:
    """Count one unanswered request to *name*; clear its route on the second in a row.

    Returns True if the route was cleared (the next request floods)."""
    if not has_route(contact) or not ROUTE_MISSES.miss(name):
        return False
    contact.out_path = []
    contact.out_path_len = -1
    return True
