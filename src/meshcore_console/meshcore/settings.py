from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta


@dataclass(slots=True)
class MeshcoreSettings:
    # Public Info
    node_name: str = "uconsole-node"
    latitude: float = 0.0
    longitude: float = 0.0
    share_position: bool = False  # Share GPS position in adverts
    allow_telemetry: bool = True  # Allow telemetry requests from other nodes
    telemetry_favorites_only: bool = False
    autoconnect: bool = False  # Automatically connect to radio on app startup
    gps_device: str = ""  # GPS serial port path; empty = auto-detect (#82)
    suppress_service_dialog: bool = False  # Don't prompt to stop conflicting services
    log_level: str = "INFO"  # stderr log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
    show_radio_error_toasts: bool = False  # Show radio warnings as in-app toast alerts
    # Repeaters with no advert for this long are removed unless favourited (REPEATER_EXPIRY key)
    repeater_expiry: str = "never"

    # Radio
    radio_preset: str = "meshcore-us"
    frequency: int = 910_525_000
    bandwidth: int = 62_500
    spreading_factor: int = 7
    coding_rate: int = 5
    tx_power: int = 22
    preamble_length: int = 17
    path_hash_mode: int = 0
    flood_region: str = ""  # Default region scope for flood packets; empty = unscoped (#90)

    # Hardware (SPI/GPIO)
    hardware_preset: str = "uconsole"
    bus_id: int = 1
    cs_id: int = 0
    cs_pin: int = -1
    reset_pin: int = 25
    busy_pin: int = 24
    irq_pin: int = 26
    txen_pin: int = -1
    rxen_pin: int = -1
    is_waveshare: bool = False
    use_dio2_rf: bool = True
    use_dio3_tcxo: bool = True
    # Which /dev/gpiochipN carries the 40-pin header.  0 on CM4, but 15 on CM5
    # and Pi 5 kernels where the header hangs off the RP1 (#85).
    gpio_chip: int = 0
    # Selects openhop_core's "gpiod" GPIO backend.  Despite the name this does
    # not switch to libgpiod while python-periphery is installed (the library
    # only swaps in its libgpiod wrapper when periphery is *absent*); what it
    # actually changes is edge detection, from kernel edge interrupts to a
    # polling thread.  That is the useful part: some kernels reject the edge
    # request outright, leaving the radio connected but deaf (#85).
    use_gpiod_backend: bool = False
    # Comma-separated GPIO pins driven HIGH at init to power the radio, for
    # boards with a LoRa power-enable line (e.g. HG AIOv2).  Empty = none (#85).
    en_pins: str = ""

    def clone(self) -> "MeshcoreSettings":
        return replace(self)


# Settings > Repeaters > Remove after: key -> (label, seconds; None = never)
REPEATER_EXPIRY: dict[str, tuple[str, int | None]] = {
    "1d": ("1 day", 86_400),
    "1w": ("1 week", 7 * 86_400),
    "1m": ("1 month", 30 * 86_400),
    "6m": ("6 months", 182 * 86_400),
    "1y": ("1 year", 365 * 86_400),
    "never": ("Never", None),
}


def expired_repeaters(peers: Iterable, expiry: str, now: datetime) -> list:
    """Repeaters whose last advert is older than *expiry*, favourites excepted.

    Only adverts set ``last_advert_time``. A repeater with no advert on record
    is kept: there is nothing to age.
    """
    seconds = REPEATER_EXPIRY.get(expiry, ("", None))[1]
    if seconds is None:
        return []
    cutoff = now - timedelta(seconds=seconds)

    def heard(p) -> datetime:  # an old naive time is UTC
        t = p.last_advert_time
        return t if t.tzinfo is not None else t.replace(tzinfo=UTC)

    return [
        p
        for p in peers
        if p.is_repeater
        and not p.is_favorite
        and p.last_advert_time is not None
        and heard(p) < cutoff
    ]


RADIO_PRESETS: dict[str, dict[str, int]] = {
    "meshcore-us": {
        "frequency": 910_525_000,
        "bandwidth": 62_500,
        "spreading_factor": 7,
        "coding_rate": 5,
        "preamble_length": 17,
        "tx_power": 22,
    },
    "meshcore-eu": {
        "frequency": 869_525_000,
        "bandwidth": 250_000,
        "spreading_factor": 11,
        "coding_rate": 5,
        "preamble_length": 17,
        "tx_power": 22,
    },
}


def apply_preset(settings: MeshcoreSettings, preset: str) -> MeshcoreSettings:
    values = RADIO_PRESETS.get(preset)
    updated = settings.clone()
    updated.radio_preset = preset
    if values is None:
        return updated
    for key, value in values.items():
        setattr(updated, key, value)
    return updated


HARDWARE_PRESETS: dict[str, dict[str, int | bool | str]] = {
    "uconsole": {
        "bus_id": 1,
        "cs_id": 0,
        "cs_pin": -1,
        "reset_pin": 25,
        "busy_pin": 24,
        "irq_pin": 26,
        "txen_pin": -1,
        "rxen_pin": -1,
        "is_waveshare": False,
        "use_dio2_rf": True,
        "use_dio3_tcxo": True,
        "en_pins": "",
    },
    # Same wiring as the plain uConsole board, plus the LoRa power-enable pin
    # that the HackerGadgets AIOv2 puts on GPIO 27 (#85).
    "hg-aiov2": {
        "bus_id": 1,
        "cs_id": 0,
        "cs_pin": -1,
        "reset_pin": 25,
        "busy_pin": 24,
        "irq_pin": 26,
        "txen_pin": -1,
        "rxen_pin": -1,
        "is_waveshare": False,
        "use_dio2_rf": True,
        "use_dio3_tcxo": True,
        "en_pins": "27",
    },
    "waveshare": {
        "bus_id": 0,
        "cs_id": 0,
        "cs_pin": 21,
        "reset_pin": 18,
        "busy_pin": 20,
        "irq_pin": 16,
        "txen_pin": 6,
        "rxen_pin": -1,
        "is_waveshare": True,
        "use_dio2_rf": False,
        "use_dio3_tcxo": False,
        "en_pins": "",
    },
    "meshadv-mini": {
        "bus_id": 0,
        "cs_id": 0,
        "cs_pin": 8,
        "reset_pin": 24,
        "busy_pin": 20,
        "irq_pin": 16,
        "txen_pin": -1,
        "rxen_pin": 12,
        "is_waveshare": False,
        "use_dio2_rf": False,
        "use_dio3_tcxo": False,
        "en_pins": "",
    },
}


def apply_hardware_preset(settings: MeshcoreSettings, preset: str) -> MeshcoreSettings:
    values = HARDWARE_PRESETS.get(preset)
    updated = settings.clone()
    updated.hardware_preset = preset
    if values is None:
        return updated
    for key, value in values.items():
        setattr(updated, key, value)
    return updated
