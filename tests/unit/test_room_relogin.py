"""Room servers get logged in to again after the session drops."""

from __future__ import annotations

import time

from meshcore_console.core.models import Peer
from meshcore_console.meshcore.db import open_db


def _client(tmp_path, monkeypatch):
    from meshcore_console.meshcore import client as client_mod
    from meshcore_console.meshcore.client import MeshcoreClient
    from meshcore_console.meshcore.config import runtime_config_from_settings
    from meshcore_console.meshcore.settings import MeshcoreSettings
    from meshcore_console.mock import MockOpenHopCoreSession

    db_conn = open_db(str(tmp_path / "rooms.db"))
    monkeypatch.setattr(client_mod, "open_db", lambda *a, **k: db_conn)
    client = MeshcoreClient(
        session=MockOpenHopCoreSession(runtime_config_from_settings(MeshcoreSettings())),
        require_openhop=False,
    )
    client._peers["Room"] = Peer(
        peer_id="r", display_name="Room", node_type=3, public_key="ab" * 32
    )
    client._peers["Other"] = Peer(peer_id="o", display_name="Other", node_type=3)
    client._connected = True
    logins: list[str] = []

    def fake_login(name, password, *, save_password=False):
        logins.append(name)
        client._room_sessions[name] = {"time": time.time(), "perms": 2, "role": "read/write"}
        return {"success": True}

    monkeypatch.setattr(client, "login_to_room", fake_login)
    return client, logins


def test_relogin_skips_rooms_without_password_or_with_fresh_session(tmp_path, monkeypatch):
    client, logins = _client(tmp_path, monkeypatch)
    client._repeater_password_store.save_password("Room", "pw")
    client.relogin_rooms()
    assert logins == ["Room"]  # "Other" has no saved password
    client.relogin_rooms(max_age=3600)
    assert logins == ["Room"]  # session is fresh


def test_relogin_refreshes_stale_session(tmp_path, monkeypatch):
    client, logins = _client(tmp_path, monkeypatch)
    client._repeater_password_store.save_password("Room", "pw")
    client._room_sessions["Room"] = {"time": time.time() - 7200, "perms": 2, "role": "x"}
    client.relogin_rooms()
    assert logins == []  # without max_age, any session counts
    client.relogin_rooms(max_age=3600)
    assert logins == ["Room"]


def test_disconnect_clears_room_sessions(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    client._room_sessions["Room"] = {"time": time.time(), "perms": 2, "role": "x"}
    client.disconnect()
    assert client.get_room_login_state("Room") is None


def test_failed_post_logs_in_again_and_retries(tmp_path, monkeypatch):
    client, logins = _client(tmp_path, monkeypatch)
    client._repeater_password_store.save_password("Room", "pw")
    attempts: list[str] = []

    def fake_post(name, text):
        attempts.append(text)
        return (len(attempts) > 1, "" if len(attempts) > 1 else "No ACK")

    monkeypatch.setattr(client, "_post_to_room", fake_post)
    result = client.send_room_post("Room", "hello")
    assert result["success"]
    assert logins == ["Room"]
    assert attempts == ["hello", "hello"]


def test_failed_post_without_password_does_not_retry(tmp_path, monkeypatch):
    client, logins = _client(tmp_path, monkeypatch)
    monkeypatch.setattr(client, "_post_to_room", lambda n, t: (False, "No ACK"))
    result = client.send_room_post("Room", "hello")
    assert not result["success"]
    assert logins == []


def test_reply_during_ack_wait_sorts_after_our_post(tmp_path, monkeypatch) -> None:
    client, _ = _client(tmp_path, monkeypatch)
    room = client._peers["Room"]

    def slow_post(name, text):
        # The room answers before its ACK reaches us; its clock runs 30 s slow.
        time.sleep(0.05)
        client._store_room_post(room, {"timestamp": int(time.time()) - 30}, "Commands…")
        return True, ""

    monkeypatch.setattr(client, "_post_to_room", slow_post)
    assert client.send_room_post("Room", "/help")["success"]
    posts = sorted(client.list_room_posts("Room"), key=lambda m: m.created_at)
    assert [m.body for m in posts] == ["/help", "Commands…"]
