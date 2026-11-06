"""Settings > Repeaters > Remove after: which repeaters expire, and the setting persists."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from meshcore_console.core.models import Peer
from meshcore_console.meshcore.db import open_db
from meshcore_console.meshcore.settings import (
    REPEATER_EXPIRY,
    MeshcoreSettings,
    expired_repeaters,
)
from meshcore_console.meshcore.settings_store import SettingsStore

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)


def _peer(name: str, age: timedelta | None, *, repeater=True, favorite=False) -> Peer:
    return Peer(
        peer_id=name,
        display_name=name,
        is_repeater=repeater,
        is_favorite=favorite,
        last_advert_time=None if age is None else NOW - age,
    )


def _names(peers) -> list[str]:
    return sorted(p.display_name for p in peers)


def test_week_expiry_removes_only_old_unfavourited_repeaters() -> None:
    peers = [
        _peer("fresh", timedelta(days=2)),
        _peer("stale", timedelta(days=8)),
        _peer("stale-fav", timedelta(days=30), favorite=True),
        _peer("stale-contact", timedelta(days=30), repeater=False),
        _peer("never-heard", None),
    ]
    assert _names(expired_repeaters(peers, "1w", NOW)) == ["stale"]


def test_never_keeps_everything() -> None:
    peers = [_peer("ancient", timedelta(days=5000))]
    assert expired_repeaters(peers, "never", NOW) == []


def test_unknown_key_is_treated_as_never() -> None:
    peers = [_peer("ancient", timedelta(days=5000))]
    assert expired_repeaters(peers, "fortnight", NOW) == []


@pytest.mark.parametrize(
    ("key", "days"), [("1d", 1), ("1w", 7), ("1m", 30), ("6m", 182), ("1y", 365)]
)
def test_each_period_boundary(key: str, days: int) -> None:
    inside = _peer("inside", timedelta(days=days) - timedelta(minutes=1))
    outside = _peer("outside", timedelta(days=days) + timedelta(minutes=1))
    assert _names(expired_repeaters([inside, outside], key, NOW)) == ["outside"]


def test_naive_advert_time_is_read_as_utc() -> None:
    old = Peer(
        peer_id="old",
        display_name="old",
        is_repeater=True,
        last_advert_time=(NOW - timedelta(days=3)).replace(tzinfo=None),
    )
    assert _names(expired_repeaters([old], "1d", NOW)) == ["old"]


def test_options_match_the_settings_menu() -> None:
    labels = [label for label, _ in REPEATER_EXPIRY.values()]
    assert labels == ["1 day", "1 week", "1 month", "6 months", "1 year", "Never"]
    assert MeshcoreSettings().repeater_expiry == "never"


def test_expiry_setting_round_trip(tmp_path) -> None:
    conn = open_db(str(tmp_path / "test.db"))
    store = SettingsStore(conn)
    store.save(MeshcoreSettings(repeater_expiry="6m"))
    assert store.load().repeater_expiry == "6m"
    conn.close()


def test_mock_client_expires_repeaters() -> None:
    from meshcore_console.mock import MockMeshcoreClient

    client = MockMeshcoreClient()
    settings = client.get_settings()
    settings.repeater_expiry = "1d"
    client.update_settings(settings)
    client._peers["gone"] = _peer("gone", timedelta(days=3))
    client._peers["kept"] = _peer("kept", timedelta(days=3), favorite=True)

    assert client.expire_repeaters(now=NOW) >= 1
    names = {p.display_name for p in client.list_peers()}
    assert "gone" not in names
    assert "kept" in names


def _real_client(tmp_path, monkeypatch):
    from meshcore_console.meshcore import client as client_mod
    from meshcore_console.meshcore.client import MeshcoreClient
    from meshcore_console.meshcore.config import runtime_config_from_settings
    from meshcore_console.mock import MockOpenHopCoreSession

    db_conn = open_db(str(tmp_path / "real.db"))
    monkeypatch.setattr(client_mod, "open_db", lambda *a, **k: db_conn)
    client = MeshcoreClient(
        session=MockOpenHopCoreSession(runtime_config_from_settings(MeshcoreSettings())),
        require_openhop=False,
    )
    return client, db_conn


def test_real_client_expires_on_setting_change_and_persists(tmp_path, monkeypatch) -> None:
    from meshcore_console.meshcore.state_store import PeerStore

    client, db_conn = _real_client(tmp_path, monkeypatch)
    old = datetime.now(UTC) - timedelta(days=10)
    for name, fav in (("stale", False), ("stale-fav", True)):
        peer = Peer(
            peer_id=name, display_name=name, is_repeater=True, is_favorite=fav, last_advert_time=old
        )
        client._peers[name] = peer
        client._peer_store.add_or_update(peer)

    settings = client.get_settings()
    settings.repeater_expiry = "1w"
    client.update_settings(settings)  # a new period applies at once

    names = {p.display_name for p in client.list_peers()}
    assert "stale" not in names
    assert "stale-fav" in names
    stored = set(PeerStore(db_conn).get_all())
    assert "stale" not in stored  # gone from the database too, not only the list
    assert "stale-fav" in stored
