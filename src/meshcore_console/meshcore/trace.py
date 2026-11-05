"""Round-trip TRACE along a repeater path, giving SNR per link. [trace patch]

MeshCore TRACE (firmware Mesh.cpp): the payload lists the hop hashes to visit;
each listed repeater that receives the trace appends the SNR it received it at
(int8, SNR x 4) to the packet's path field and forwards it to the next hash.
When the list is exhausted, the next receiver (here: this radio) gets the
completed packet with one SNR reading per forwarded leg; this radio's own
receive SNR measures the final leg.

The route is out to the target repeater and back along the same hops, so every
link is measured in both directions.
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import Any

# TRACE flags (lower 2 bits) encode the hash width as 1 << n bytes.
_WIDTH_FLAG = {1: 0, 2: 1, 4: 2}

# Only one trace in flight: run_trace() chains the handler's completion hook,
# and overlapping chains could restore it out of order. [trace-log patch]
_IN_FLIGHT = {"busy": False}

# LoRa demodulator SNR limit by spreading factor (Semtech SX1261/2 datasheet).
SNR_LIMIT_DB = {5: -2.5, 6: -5.0, 7: -7.5, 8: -10.0, 9: -12.5, 10: -15.0, 11: -17.5, 12: -20.0}


def trace_route(hops: list[str], target_key: str) -> tuple[list[str], int]:
    """Hop hashes (hex) out to the target and back, and the hash width in bytes.

    *hops* is the stored path in outbound order (your side first). Widths that
    TRACE cannot encode (3 bytes) are cut to 2; hash prefixes still match.
    """
    widths = {len(h) // 2 for h in hops if h}
    width = max(widths) if widths else 2
    width = 4 if width >= 4 else (2 if width >= 2 else 1)
    cut = [h[: width * 2].upper() for h in hops]
    target = target_key[: width * 2].upper()
    return cut + [target] + list(reversed(cut)), width


def trace_route_hops(hops: list[str]) -> tuple[list[str], int]:
    """Route out along *hops* (our side first) to the last one and back. [channel-trace patch]

    For paths taken from a received message, where the last hop is a hash, not
    a full key. The width is the shortest hash every hop has (3-byte cut to 2,
    which TRACE cannot encode), so every hop is encoded at the same width.
    """
    hops = [str(h).upper() for h in hops if h]
    if not hops:
        raise ValueError("no hops to trace")
    width = min(len(h) // 2 for h in hops)
    width = 4 if width >= 4 else (2 if width >= 2 else 1)
    cut = [h[: width * 2] for h in hops]
    return cut + list(reversed(cut[:-1])), width


async def run_trace(*, node: Any, route: list[str], width: int, timeout: float = 20.0) -> dict:
    """Send the trace and wait for it to come back. Returns per-link SNR.

    If another trace is still waiting, returns at once with ``busy: True`` and
    sends nothing.
    """
    if _IN_FLIGHT["busy"]:
        return {"success": False, "busy": True, "reason": "Another trace is still in progress"}
    _IN_FLIGHT["busy"] = True
    try:
        return await _run_trace(node=node, route=route, width=width, timeout=timeout)
    finally:
        _IN_FLIGHT["busy"] = False


# Who started the trace being logged on this thread ("auto", "admin", "map",
# "peers"); callers wrap service.trace_repeater() in trace_source(). [trace-source patch]
import threading as _threading
from contextlib import contextmanager as _contextmanager

_LOCAL = _threading.local()


@_contextmanager
def trace_source(name: str):
    previous = getattr(_LOCAL, "source", None)
    _LOCAL.source = name
    try:
        yield
    finally:
        _LOCAL.source = previous


def current_trace_source() -> str | None:
    """Who started the trace running on this thread (see trace_source)."""
    return getattr(_LOCAL, "source", None)


def trace_in_progress() -> bool:
    return _IN_FLIGHT["busy"]


def log_trace(store: Any, pubkey: str | None, result: dict, sf: int | None) -> None:
    """Record a trace that went on air (success or timeout) in the sample store.

    Refusals (busy, not connected, unknown repeater) never reached the radio
    and are not logged.
    """
    if store is None or not pubkey or result.get("busy") or "route" not in result:
        return
    try:
        try:
            store.add_trace(pubkey, result, sf=sf, source=getattr(_LOCAL, "source", None))
        except TypeError:  # an older store without the source argument
            store.add_trace(pubkey, result, sf=sf)
    except Exception:  # noqa: BLE001 - logging must never break the trace itself
        import logging

        logging.getLogger(__name__).exception("could not log trace")


async def _run_trace(*, node: Any, route: list[str], width: int, timeout: float) -> dict:
    from openhop_core.protocol.packet_builder import PacketBuilder

    tag = random.getrandbits(32)
    path_bytes = bytes.fromhex("".join(route))
    pkt = PacketBuilder.create_trace(tag, 0, _WIDTH_FLAG[width], list(path_bytes))

    handler = node.dispatcher.trace_handler
    loop = asyncio.get_running_loop()
    done: asyncio.Future = loop.create_future()
    previous = handler.on_trace_complete

    async def _on_complete(packet: Any, parsed: dict) -> None:
        if parsed.get("tag") == tag and not done.done():
            done.set_result((bytes(packet.path), parsed))
        if previous is not None:
            await previous(packet, parsed)

    handler.on_trace_complete = _on_complete
    t0 = time.monotonic()
    try:
        await node.dispatcher.send_packet(pkt, wait_for_ack=False)
        snr_bytes, parsed = await asyncio.wait_for(done, timeout=timeout)
    except asyncio.TimeoutError:
        return {
            "success": False,
            "reason": f"No trace came back within {timeout:.0f}s (a hop may be down or out of range)",
            "route": route,
        }
    finally:
        if handler.on_trace_complete is _on_complete:
            handler.on_trace_complete = previous

    snrs = [(b - 256 if b > 127 else b) / 4.0 for b in snr_bytes]
    nodes = ["me", *route, "me"]
    links = [{"from": nodes[i], "to": nodes[i + 1], "snr": snr} for i, snr in enumerate(snrs)]
    own = parsed.get("snr")
    links.append({"from": route[-1] if route else "me", "to": "me", "snr": own})
    return {
        "success": True,
        "route": route,
        "links": links,
        "rtt_ms": round((time.monotonic() - t0) * 1000),
    }
