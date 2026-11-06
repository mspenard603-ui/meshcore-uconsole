"""Repeaters/Contacts search matching (views/peers.py). [peer-search patch]"""

from __future__ import annotations

import pytest

pytest.importorskip("gi")

from meshcore_console.core.models import Peer  # noqa: E402
from meshcore_console.ui_gtk.views.peers import PeersView  # noqa: E402

PEER = Peer(
    peer_id="p1", display_name="FrancestownNH DodgeHill", public_key="59b9c0ffee" + "0" * 54
)


@pytest.mark.parametrize("query", ["", "dodge", "francestownnh d", "59", "59b9c0"])
def test_matches_name_substring_or_key_prefix(query: str) -> None:
    assert PeersView.peer_matches(PEER, query)


@pytest.mark.parametrize("query", ["zzz", "b9c0", "hill dodge"])
def test_no_match(query: str) -> None:
    assert not PeersView.peer_matches(PEER, query)


def test_peer_without_key_matches_on_name_only() -> None:
    peer = Peer(peer_id="p2", display_name="Alice")
    assert PeersView.peer_matches(peer, "ali")
    assert not PeersView.peer_matches(peer, "59")
