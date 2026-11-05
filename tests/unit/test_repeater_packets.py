"""Repeaters > Packets: which packets involve a repeater, and how."""

from __future__ import annotations

from meshcore_console.meshcore.repeater_packets import packet_relation, shared_prefixes

KEY = "fb92683c853b532d2d4d95fd444ccb71b863d06ff61039f4ae8633f38fc8dbd0"
OTHER = "2a77" + "0" * 60


def rel(**kw):
    return packet_relation(KEY, **kw)


def test_own_advert_is_from() -> None:
    assert rel(sender_pubkey=KEY.upper(), payload_type=4, path_hops=["2A"]) == "from"


def test_addressed_packets_use_source_and_destination_hash() -> None:
    assert rel(payload_type=1, payload_hex="6bfb0011", route_type="DIRECT") == "from"
    assert rel(payload_type=0, payload_hex="fb6b0011", route_type="DIRECT") == "to"
    assert rel(payload_type=7, payload_hex="fb" + "6b" * 32, route_type="FLOOD") == "to"
    assert rel(payload_type=2, payload_hex="6b2a0011", route_type="DIRECT") is None


def test_flood_last_hop_is_heard_earlier_hop_is_via() -> None:
    assert rel(payload_type=5, route_type="FLOOD", path_hops=["2A", "FB"]) == "heard"
    assert rel(payload_type=5, route_type="FLOOD", path_hops=["FB", "2A"]) == "via"
    assert rel(payload_type=5, route_type="TRANSPORT_FLOOD", path_hops=["fb92"]) == "heard"
    # a direct packet's path is the route still ahead: never "heard"
    assert rel(payload_type=2, route_type="DIRECT", path_hops=["FB"], payload_hex="") == "via"


def test_unrelated_and_trace_packets_are_left_out() -> None:
    assert rel(payload_type=5, route_type="FLOOD", path_hops=["2A", "C3"]) is None
    assert rel(payload_type=9, route_type="DIRECT", path_hops=["FB"]) is None  # SNR bytes
    assert packet_relation("", payload_type=5, route_type="FLOOD", path_hops=["FB"]) is None


def test_shared_one_byte_hash_is_marked() -> None:
    shared = shared_prefixes([KEY, "fb00" + "1" * 60, OTHER])
    assert shared == {"fb"}
    assert rel(payload_type=5, route_type="FLOOD", path_hops=["FB"], shared=shared) == "heard?"
    # a 2-byte hop tells the two apart
    assert rel(payload_type=5, route_type="FLOOD", path_hops=["FB92"], shared=shared) == "heard"
