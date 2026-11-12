"""A room's reply never sorts above the post it answers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from meshcore_console.meshcore.client import room_post_time

NOW = datetime(2026, 11, 7, 17, 47, 30, tzinfo=UTC)


def _ts(dt: datetime) -> int:
    return int(dt.timestamp())


def test_live_post_from_slow_room_clock_is_filed_now() -> None:
    assert room_post_time(_ts(NOW - timedelta(seconds=40)), NOW) == NOW


def test_post_from_fast_room_clock_is_filed_now() -> None:
    assert room_post_time(_ts(NOW + timedelta(minutes=3)), NOW) == NOW


def test_history_keeps_room_time() -> None:
    old = NOW - timedelta(hours=2)
    assert room_post_time(_ts(old), NOW) == old


def test_unset_room_clock_is_filed_now() -> None:
    assert room_post_time(0, NOW) == NOW
