"""send_login / send_repeater_command against openhop_core's real handlers.

The mock client never reaches operations.py, so these tests are what catches
openhop_core changing its callback API (1.1.3 dropped set_login_callback and
set_command_response_callback for per-pubkey registration).

Packet building is stubbed out; the fake dispatcher plays the radio by
delivering the reply to whichever waiter the handler holds for the
repeater's pubkey -- the same lookup the handler does on a real reply.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from openhop_core.node.handlers.login_response import LoginResponseHandler
from openhop_core.node.handlers.text import TextMessageHandler
from openhop_core.protocol.packet_builder import PacketBuilder

from meshcore_console.meshcore import operations

PUBKEY = bytes(range(32))


class _Contacts:
    def __init__(self, contact: object) -> None:
        self._contact = contact

    def get_by_name(self, name: str) -> object | None:
        return self._contact if name == self._contact.name else None


@pytest.fixture(autouse=True)
def _stub_packet_builder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(PacketBuilder, "create_login_packet", staticmethod(lambda **_: object()))
    monkeypatch.setattr(
        PacketBuilder, "create_text_message", staticmethod(lambda **_: (object(), 0))
    )


def _make_node(reply):
    """Build a node whose send_packet calls reply(login_handler, text_handler, contact)."""
    contact = SimpleNamespace(name="rpt", public_key=PUBKEY.hex(), out_path=[], out_path_len=-1)
    contacts = _Contacts(contact)

    async def send_packet(_pkt, **_kwargs):
        if reply is not None:
            reply(login, text, contact)
        return True

    login = LoginResponseHandler(None, contacts, lambda *_: None)
    text = TextMessageHandler(None, contacts, lambda *_: None, send_packet)
    dispatcher = SimpleNamespace(
        login_response_handler=login, text_message_handler=text, send_packet=send_packet
    )
    node = SimpleNamespace(dispatcher=dispatcher, contacts=contacts, identity=None)
    return node, login, text


def test_login_registers_by_pubkey_and_cleans_up() -> None:
    def reply(login, _text, _contact):
        login._pending_logins[PUBKEY](True, {"is_admin": True, "keep_alive_interval": 5})

    node, login, _ = _make_node(reply)
    result = asyncio.run(operations.send_login(node=node, peer_name="rpt", password="pw"))

    assert result["success"] is True
    assert result["is_admin"] is True
    assert result["keep_alive_interval"] == 5
    assert login._pending_logins == {}


def test_login_timeout_removes_waiter() -> None:
    node, login, _ = _make_node(None)
    result = asyncio.run(
        operations.send_login(node=node, peer_name="rpt", password="pw", timeout=0.05)
    )

    assert result == {"success": False, "reason": "Login response timeout"}
    assert login._pending_logins == {}


def test_repeater_command_registers_by_pubkey_and_cleans_up() -> None:
    def reply(_login, text, contact):
        text._pending_command_responses.pop(PUBKEY)("v1.2.3", contact)

    node, _, text = _make_node(reply)
    result = asyncio.run(
        operations.send_repeater_command(node=node, peer_name="rpt", command="ver")
    )

    assert result["success"] is True
    assert result["response_text"] == "v1.2.3"
    assert text._pending_command_responses == {}


def test_repeater_command_timeout_removes_waiter() -> None:
    node, _, text = _make_node(None)
    result = asyncio.run(
        operations.send_repeater_command(node=node, peer_name="rpt", command="ver", timeout=0.05)
    )

    assert result["success"] is False
    assert text._pending_command_responses == {}
