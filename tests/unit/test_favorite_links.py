"""Map > favourite links: which favourite repeaters hear each other."""

from __future__ import annotations

from meshcore_console.meshcore.favorite_links import NEIGHBOUR_TABLE, favorite_mesh
from meshcore_console.mock.client import MockMeshcoreClient


def _mesh():
    client = MockMeshcoreClient()
    store = client.get_repeater_samples()
    peers = client.list_peers()
    return client, favorite_mesh(peers, lambda key: store.get_state(key, NEIGHBOUR_TABLE))


def _ids(link) -> set[str]:
    return {link.a.peer_id, link.b.peer_id}


def test_mock_has_ten_repeaters_with_keys() -> None:
    reps = [p for p in MockMeshcoreClient().list_peers() if p.is_repeater]
    assert len(reps) == 10
    assert all(p.public_key and len(p.public_key) == 64 for p in reps)
    assert len({p.public_key[:8] for p in reps}) == 10  # table prefixes are unambiguous


def test_only_mutual_fresh_links_between_favourites() -> None:
    _client, mesh = _mesh()
    assert sorted(sorted(_ids(link)) for link in mesh.links) == [
        ["relay-001", "relay-004"],
        ["relay-001", "relay-005"],
        ["relay-002", "relay-005"],
        ["relay-002", "relay-006"],
        ["relay-004", "relay-008"],
    ]
    # A-C and D-G hear each other but C and G are not favourites
    assert not any({"relay-003", "relay-007"} & _ids(link) for link in mesh.links)


def test_edge_cases_are_reported_not_drawn() -> None:
    _client, mesh = _mesh()
    assert [(heard.peer_id, hearer.peer_id) for heard, hearer in mesh.one_way] == [
        ("relay-001", "relay-008")
    ]
    assert mesh.stale == 1  # F-I, last heard 5 days ago
    assert [p.peer_id for p in mesh.no_table] == ["relay-010"]


def test_snr_direction_is_as_heard_by_the_receiver() -> None:
    _client, mesh = _mesh()
    link = next(lk for lk in mesh.links if _ids(lk) == {"relay-002", "relay-006"})
    by_id = {link.a.peer_id: "a", link.b.peer_id: "b"}
    # B (relay-002) hears F at -8.5 dB; F (relay-006) hears B at -3.0 dB
    b_hears_f = link.snr_ba if by_id["relay-002"] == "a" else link.snr_ab
    f_hears_b = link.snr_ab if by_id["relay-002"] == "a" else link.snr_ba
    assert (b_hears_f, f_hears_b) == (-8.5, -3.0)


def test_unfavouriting_removes_its_links() -> None:
    client, _ = _mesh()
    client.set_favorite("relay-005", False)
    store = client.get_repeater_samples()
    mesh = favorite_mesh(client.list_peers(), lambda key: store.get_state(key, NEIGHBOUR_TABLE))
    assert not any("relay-005" in _ids(link) for link in mesh.links)


def test_neighbour_request_refreshes_the_saved_table() -> None:
    client = MockMeshcoreClient()
    peer = next(p for p in client.list_peers() if p.peer_id == "relay-001")
    store = client.get_repeater_samples()
    before = store.get_state(peer.public_key, NEIGHBOUR_TABLE)["ts"]
    result = client.request_repeater_neighbours(peer.display_name)
    assert result["success"] and result["rows"]
    assert store.get_state(peer.public_key, NEIGHBOUR_TABLE)["ts"] > before
    assert not client.request_repeater_neighbours("\U0001f4e1 Alameda J")["success"]
