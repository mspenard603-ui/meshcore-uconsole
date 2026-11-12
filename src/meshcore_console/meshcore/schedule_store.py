"""Per-repeater auto-poll and auto-trace schedules, shared by Admin and Settings.

Files in the config dir (keys: repeater public key, or its name if none):
  repeater_poll.json         {key: seconds}   0 or absent = off
  repeater_poll_paused.json  [key, ...]       paused (interval kept); setting an interval resumes
  repeater_trace.json        {key: seconds}
Loaded once per process; every change is written at once.
"""

from __future__ import annotations

import json
import logging
import threading

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_cache: dict[str, object] = {}


def _path(name: str):
    from meshcore_console.meshcore.paths import config_dir

    return config_dir() / name


def _load(name: str, default):
    with _lock:
        if name not in _cache:
            try:
                data = json.loads(_path(name).read_text())
            except (OSError, ValueError):
                data = default
            if isinstance(default, dict):
                data = {
                    str(k): int(v)
                    for k, v in (data.items() if isinstance(data, dict) else ())
                    if isinstance(v, (int, float)) and v >= 0
                }
            else:
                data = {str(k) for k in data} if isinstance(data, list) else set()
            _cache[name] = data
        return _cache[name]


def _save(name: str) -> None:
    data = _cache.get(name)
    payload = sorted(data) if isinstance(data, set) else data
    try:
        path = _path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=1))
    except OSError as e:
        logger.warning("Could not save %s: %s", name, e)


def reset_cache() -> None:
    """Forget what was loaded (tests)."""
    with _lock:
        _cache.clear()


# --- auto-poll ------------------------------------------------------------
def poll_intervals() -> dict[str, int]:
    return _load("repeater_poll.json", {})


def paused_polls() -> set[str]:
    return _load("repeater_poll_paused.json", [])


def poll_interval(key: str) -> int:
    """The chosen interval; 0 = off (a repeater is polled only when given one)."""
    return int(poll_intervals().get(key, 0))


def set_poll_interval(key: str, seconds: int) -> None:
    """Choose an interval (0 = off). Also resumes a paused auto-poll."""
    poll_intervals()[key] = int(seconds)
    paused_polls().discard(key)
    _save("repeater_poll.json")
    _save("repeater_poll_paused.json")


def pause_poll(key: str) -> None:
    paused_polls().add(key)
    _save("repeater_poll_paused.json")


def pause_all_polls() -> list[str]:
    """Settings: pause every auto-poll that has an interval. Returns the keys paused."""
    keys = [k for k, s in poll_intervals().items() if s and k not in paused_polls()]
    paused_polls().update(keys)
    _save("repeater_poll_paused.json")
    return keys


# --- auto-trace -----------------------------------------------------------
def trace_intervals() -> dict[str, int]:
    return _load("repeater_trace.json", {})


def set_trace_interval(key: str, seconds: int) -> None:
    if seconds:
        trace_intervals()[key] = int(seconds)
    else:
        trace_intervals().pop(key, None)
    _save("repeater_trace.json")


def clear_traces() -> list[str]:
    """Settings: turn every auto-trace off. Returns the keys that had one."""
    keys = list(trace_intervals())
    trace_intervals().clear()
    _save("repeater_trace.json")
    return keys
