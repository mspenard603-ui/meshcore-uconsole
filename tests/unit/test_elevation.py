"""Offline SRTM elevation lookups (platform/elevation.py)."""

from __future__ import annotations

import struct

import pytest

from meshcore_console.platform.elevation import VOID, ElevationStore, tile_name

N = 1201  # 3 arc-second tile


def _write_tile(directory, name: str, value_at) -> None:
    """Write an N x N tile whose sample (row, col) is value_at(row, col)."""
    rows = []
    for r in range(N):
        rows.append(struct.pack(f">{N}h", *(value_at(r, c) for c in range(N))))
    (directory / f"{name}.hgt").write_bytes(b"".join(rows))


@pytest.mark.parametrize(
    ("lat", "lon", "name"),
    [
        (43.2, -71.5, "N43W072"),
        (43.0, -71.0, "N43W071"),
        (-33.9, 151.2, "S34E151"),
        (0.5, 0.5, "N00E000"),
        (-0.5, -0.5, "S01W001"),
    ],
)
def test_tile_name(lat: float, lon: float, name: str) -> None:
    assert tile_name(lat, lon) == name


def test_corners_and_interpolation(tmp_path) -> None:
    # Height rises 1 m per row going south, 2 m per column going east.
    _write_tile(tmp_path, "N43W072", lambda r, c: r + 2 * c)
    store = ElevationStore(tmp_path, download=False)

    assert store.elevation(44.0 - 1e-9, -72.0) == pytest.approx(0.0, abs=0.01)  # NW corner
    assert store.elevation(43.0, -72.0) == pytest.approx(N - 1, abs=0.01)  # SW corner
    # Halfway between samples on both axes: the bilinear mean.
    lat = 44.0 - 10.5 / (N - 1)
    lon = -72.0 + 20.5 / (N - 1)
    assert store.elevation(lat, lon) == pytest.approx(10.5 + 2 * 20.5)


def test_void_samples_are_skipped(tmp_path) -> None:
    _write_tile(tmp_path, "N43W072", lambda r, c: VOID if (r, c) == (0, 0) else 100)
    store = ElevationStore(tmp_path, download=False)
    assert store.elevation(44.0 - 0.1 / (N - 1), -72.0 + 0.1 / (N - 1)) == pytest.approx(100)


def test_all_void_is_none(tmp_path) -> None:
    _write_tile(tmp_path, "N43W072", lambda r, c: VOID)
    store = ElevationStore(tmp_path, download=False)
    assert store.elevation(43.5, -71.5) is None


def test_missing_tile_is_none_and_requests_nothing_when_offline(tmp_path) -> None:
    store = ElevationStore(tmp_path, download=False)
    assert store.elevation(43.5, -71.5) is None
    assert not any(tmp_path.iterdir())


def test_bad_size_file_is_ignored(tmp_path) -> None:
    (tmp_path / "N43W072.hgt").write_bytes(b"\x00" * 100)
    store = ElevationStore(tmp_path, download=False)
    assert store.elevation(43.5, -71.5) is None
