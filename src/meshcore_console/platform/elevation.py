"""Offline ground elevation from SRTM ``.hgt`` tiles.

Each tile covers 1x1 degree and is named for its south-west corner
(``N43W072.hgt``). It is a square grid of big-endian int16 metres above sea
level: 3601x3601 samples (1 arc-second, ~30 m) or 1201x1201 (3 arc-second),
row 0 at the north edge, column 0 at the west edge; -32768 marks a void.

Tiles live in ``<data dir>/elevation``. A lookup that misses a tile starts a
one-off background download from the public AWS Terrain Tiles bucket (the
"skadi" set) and returns None; once the tile is on disk every lookup is
offline. Readers use mmap, so a 25 MB tile costs nothing until it is touched.
"""

from __future__ import annotations

import gzip
import logging
import math
import mmap
import os
import shutil
import struct
import threading
import urllib.request
from collections.abc import Callable
from pathlib import Path

logger = logging.getLogger(__name__)

TILE_URL = "https://s3.amazonaws.com/elevation-tiles-prod/skadi/{ns}/{name}.hgt.gz"
VOID = -32768
_SIZES = {1201 * 1201 * 2: 1201, 3601 * 3601 * 2: 3601}


def tile_name(lat: float, lon: float) -> str:
    """SRTM tile holding (lat, lon), e.g. 43.2, -71.5 -> 'N43W072'."""
    la, lo = math.floor(lat), math.floor(lon)
    return f"{'N' if la >= 0 else 'S'}{abs(la):02d}{'E' if lo >= 0 else 'W'}{abs(lo):03d}"


def default_dir() -> Path:
    from meshcore_console.meshcore.paths import data_dir

    return data_dir() / "elevation"


class ElevationStore:
    """Ground elevation lookups over a directory of SRTM tiles."""

    def __init__(self, directory: Path | None = None, *, download: bool = True) -> None:
        self._dir = Path(directory) if directory is not None else default_dir()
        self._download = download
        self._open: dict[str, tuple[mmap.mmap, int] | None] = {}
        self._pending: set[str] = set()
        self._failed: set[str] = set()
        self._lock = threading.Lock()
        self._listeners: list[Callable[[str], None]] = []

    def add_listener(self, callback: Callable[[str], None]) -> None:
        """Call *callback(tile_name)* (from a worker thread) when a tile arrives."""
        self._listeners.append(callback)

    def elevation(self, lat: float, lon: float) -> float | None:
        """Metres above sea level at (lat, lon), or None if unknown (yet)."""
        if not (-90 <= lat < 90 and -180 <= lon < 180):
            return None
        name = tile_name(lat, lon)
        tile = self._tile(name)
        if tile is None:
            return None
        data, n = tile
        la0, lo0 = math.floor(lat), math.floor(lon)
        r = (la0 + 1 - lat) * (n - 1)
        c = (lon - lo0) * (n - 1)
        r0, c0 = min(int(r), n - 2), min(int(c), n - 2)
        fr, fc = r - r0, c - c0

        def at(row: int, col: int) -> int:
            return struct.unpack_from(">h", data, (row * n + col) * 2)[0]

        corners = (
            (at(r0, c0), (1 - fr) * (1 - fc)),
            (at(r0, c0 + 1), (1 - fr) * fc),
            (at(r0 + 1, c0), fr * (1 - fc)),
            (at(r0 + 1, c0 + 1), fr * fc),
        )
        good = [(v, w) for v, w in corners if v != VOID]
        weight = sum(w for _, w in good)
        if not good or weight <= 0:
            return None
        return sum(v * w for v, w in good) / weight

    # -- tiles -----------------------------------------------------------------
    def _tile(self, name: str) -> tuple[mmap.mmap, int] | None:
        with self._lock:
            if name in self._open and self._open[name] is not None:
                return self._open[name]
        path = self._dir / f"{name}.hgt"
        if not path.exists():
            self._request(name)
            return None
        size = path.stat().st_size
        n = _SIZES.get(size)
        if n is None:
            logger.warning("Elevation tile %s has unexpected size %d; ignoring", path, size)
            return None
        with open(path, "rb") as fh:
            data = mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)
        with self._lock:
            self._open[name] = (data, n)
        return data, n

    def _request(self, name: str) -> None:
        if not self._download:
            return
        with self._lock:
            if name in self._pending or name in self._failed:
                return
            self._pending.add(name)
        threading.Thread(target=self._fetch, args=(name,), daemon=True).start()

    def _fetch(self, name: str) -> None:
        url = TILE_URL.format(ns=name[:3], name=name)
        dest = self._dir / f"{name}.hgt"
        part = dest.with_suffix(".hgt.part")
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            logger.info("Elevation: downloading %s", url)
            with urllib.request.urlopen(url, timeout=60) as resp:  # noqa: S310 (fixed https URL)
                with gzip.GzipFile(fileobj=resp) as gz, open(part, "wb") as out:
                    shutil.copyfileobj(gz, out)
            if part.stat().st_size not in _SIZES:
                raise ValueError(f"unexpected tile size {part.stat().st_size}")
            os.replace(part, dest)
            logger.info("Elevation: saved %s", dest)
        except Exception as exc:  # noqa: BLE001
            # Offline, or no tile (open ocean has none): don't retry this session.
            logger.info("Elevation: no tile %s (%s)", name, exc)
            part.unlink(missing_ok=True)
            with self._lock:
                self._failed.add(name)
                self._pending.discard(name)
            return
        with self._lock:
            self._pending.discard(name)
        for callback in list(self._listeners):
            try:
                callback(name)
            except Exception as exc:  # noqa: BLE001
                logger.debug("elevation listener failed: %s", exc)


_STORE: ElevationStore | None = None


def get_store() -> ElevationStore:
    """The app-wide store (tiles in the user data dir, downloads on)."""
    global _STORE
    if _STORE is None:
        _STORE = ElevationStore()
    return _STORE
