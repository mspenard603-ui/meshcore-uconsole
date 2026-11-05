"""Which favourite repeaters hear each other directly, from their neighbour tables.

A repeater's neighbour table (REQ_TYPE_GET_NEIGHBOURS) lists the repeaters whose
zero-hop adverts it has heard: key prefix, seconds since last heard, SNR x 4. Two
favourites are linked on the Map when *each* lists the other, recently enough.

Tables are saved per repeater in the sample store's repeater_state under
NEIGHBOUR_TABLE: {"ts": when fetched, "rows": [[hex_prefix, secs_ago, snr_x4], ...]}.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

NEIGHBOUR_TABLE = "neighbours"
# The firmware never drops a table entry, so a pair that last heard each other
# long ago would otherwise be drawn forever.
MAX_LINK_AGE_S = 72 * 3600


@dataclass(frozen=True)
class FavoriteLink:
    a: Any  # Peer
    b: Any  # Peer
    snr_ab: float  # dB, A as heard by B
    snr_ba: float  # dB, B as heard by A
    age_s: int  # seconds since the staler direction was last heard


@dataclass
class FavoriteMesh:
    links: list[FavoriteLink] = field(default_factory=list)
    one_way: list[tuple[Any, Any]] = field(default_factory=list)  # (heard, hearer)
    stale: int = 0  # mutual pairs not heard within MAX_LINK_AGE_S
    no_table: list[Any] = field(default_factory=list)  # favourites without a saved table
    no_location: list[Any] = field(default_factory=list)


def save_neighbour_table(
    store: Any, key: str, rows: list[tuple[str, int, int]], ts: float | None = None
) -> None:
    store.set_state(
        key,
        NEIGHBOUR_TABLE,
        {"ts": ts if ts is not None else time.time(), "rows": [list(r) for r in rows]},
    )


def _heard(table: dict | None, target_key: str, now: float) -> tuple[float, int] | None:
    """(SNR dB, seconds since heard) for *target_key* in a saved table, or None."""
    if not table or not target_key:
        return None
    key = target_key.upper()
    for prefix, secs, snr_x4 in table.get("rows", []):
        if prefix and key.startswith(str(prefix).upper()):
            return snr_x4 / 4.0, int(secs + max(0.0, now - table.get("ts", now)))
    return None


def favorite_mesh(
    peers: list[Any], load_table, now: float | None = None, max_age_s: int = MAX_LINK_AGE_S
) -> FavoriteMesh:
    """Links between located favourite repeaters that list each other.

    *load_table(key)* returns a repeater's saved table (or None); keys are the
    public key, else the peer id, as everywhere else in the app.
    """
    now = time.time() if now is None else now
    mesh = FavoriteMesh()
    favs = []
    for p in peers:
        if not (p.is_repeater and p.is_favorite):
            continue
        if p.latitude is None or p.longitude is None:
            mesh.no_location.append(p)
            continue
        favs.append(p)
    tables = {}
    for p in favs:
        table = load_table(p.public_key or p.peer_id)
        if table:
            tables[p.peer_id] = table
        else:
            mesh.no_table.append(p)
    for i, a in enumerate(favs):
        for b in favs[i + 1 :]:
            a_hears_b = _heard(tables.get(a.peer_id), b.public_key or "", now)
            b_hears_a = _heard(tables.get(b.peer_id), a.public_key or "", now)
            if a_hears_b and b_hears_a:
                age = max(a_hears_b[1], b_hears_a[1])
                if age > max_age_s:
                    mesh.stale += 1
                    continue
                mesh.links.append(FavoriteLink(a, b, b_hears_a[0], a_hears_b[0], age))
            elif a_hears_b and b.peer_id in tables:
                mesh.one_way.append((b, a))  # A hears B, B's table doesn't list A
            elif b_hears_a and a.peer_id in tables:
                mesh.one_way.append((a, b))
    return mesh
