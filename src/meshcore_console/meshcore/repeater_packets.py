"""How a received packet involves one repeater, for Repeaters > Packets.

    from   the repeater sent it: its own advert (full key), or a direct
           REQ / RESPONSE / TXT_MSG / PATH whose source hash is the repeater's
    to     addressed to the repeater: destination hash of those types, or an
           ANON_REQ (a login) sent to it
    heard  the repeater was the last hop of a flood path, i.e. our radio heard
           the repeater itself transmit it
    via    the repeater is elsewhere in the path

A short hash (1 byte: about 1 in 256 nodes share one) that other known nodes
share as well gets a trailing "?".
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable

# Payloads that start with [dest_hash(1)][src_hash(1)] (MeshCore v1 wire format)
_ADDRESSED = {0: "REQ", 1: "RESPONSE", 2: "TXT_MSG", 8: "PATH"}
_ANON_REQ = 7
_TRACE = 9


def shared_prefixes(keys: Iterable[str], width: int = 2) -> set[str]:
    """Hex prefixes of *width* chars that more than one of *keys* starts with."""
    counts = Counter(k[:width].lower() for k in keys if k and len(k) >= width)
    return {p for p, n in counts.items() if n > 1}


def packet_relation(
    repeater_key: str,
    *,
    sender_pubkey: str = "",
    payload_type: int | None = None,
    payload_hex: str = "",
    route_type: str = "",
    path_hops: list[str] | None = None,
    shared: set[str] | frozenset[str] = frozenset(),
) -> str | None:
    """'from', 'to', 'heard' or 'via' (with '?' when the hash is shared), or None."""
    key = (repeater_key or "").lower()
    if not key:
        return None

    def hit(prefix: str) -> str | None:
        prefix = prefix.lower()
        if not prefix or not key.startswith(prefix):
            return None
        return "?" if len(prefix) <= 2 and prefix in shared else ""

    if sender_pubkey and sender_pubkey.lower() == key:
        return "from"
    if payload_type in _ADDRESSED and len(payload_hex) >= 4:
        src = hit(payload_hex[2:4])
        if src is not None:
            return "from" + src
        dest = hit(payload_hex[0:2])
        if dest is not None:
            return "to" + dest
    if payload_type == _ANON_REQ and len(payload_hex) >= 2:
        dest = hit(payload_hex[0:2])
        if dest is not None:
            return "to" + dest
    hops = list(path_hops or [])
    if payload_type == _TRACE or not hops:
        return None  # TRACE paths hold SNR bytes, not node hashes
    if "FLOOD" in route_type.upper():
        last = hit(str(hops[-1]))
        if last is not None:
            return "heard" + last
    for hop in hops:
        mark = hit(str(hop))
        if mark is not None:
            return "via" + mark
    return None
