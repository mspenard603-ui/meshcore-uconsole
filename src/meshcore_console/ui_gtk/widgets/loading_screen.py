from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk


# [splash-logo patch] the app icon the .deb installs, shown above the title
_LOGO_PATHS = (
    "/usr/share/icons/hicolor/256x256/apps/meshcore-console.png",
    "/usr/local/share/icons/hicolor/256x256/apps/meshcore-console.png",
)
LOGO_SIZE = 128


def _logo_image() -> "Gtk.Image | None":
    """The MeshCore Console logo, or None if it isn't installed (never fails)."""
    import os

    try:
        for path in _LOGO_PATHS:
            if os.path.isfile(path):
                image = Gtk.Image.new_from_file(path)
                break
        else:
            from gi.repository import Gdk

            display = Gdk.Display.get_default()
            if display is None or not Gtk.IconTheme.get_for_display(display).has_icon("meshcore-console"):
                return None
            image = Gtk.Image.new_from_icon_name("meshcore-console")
        image.set_pixel_size(LOGO_SIZE)
        image.add_css_class("loading-logo")
        return image
    except Exception:  # noqa: BLE001 - a missing logo must never block start-up
        return None


BUILD_SUFFIX = "MLS"  # [splash-version patch] marks this locally patched build


def _version_text() -> str:
    """'v1.12.0-MLS': the installed package version plus our build suffix."""
    import importlib.metadata

    try:
        version = importlib.metadata.version("meshcore-uconsole")
    except Exception:  # noqa: BLE001
        version = "dev"
    return f"v{version}-{BUILD_SUFFIX}" if BUILD_SUFFIX else f"v{version}"


class LoadingScreen(Gtk.Box):
    """Lightweight centered loading screen with spinner and status label."""

    def __init__(self, title: str = "MeshCore Console", status: str = "Loading...") -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        self.set_halign(Gtk.Align.CENTER)
        self.set_valign(Gtk.Align.CENTER)
        self.set_vexpand(True)
        self.set_hexpand(True)
        self.add_css_class("loading-screen")

        logo = _logo_image()  # [splash-logo patch]
        if logo is not None:
            self.append(logo)

        title_label = Gtk.Label(label=title)
        title_label.add_css_class("loading-title")
        self.append(title_label)
        version_label = Gtk.Label(label=_version_text())  # [splash-version patch]
        version_label.add_css_class("loading-status")
        version_label.set_margin_top(-10)  # sits just under the title
        self.append(version_label)

        spinner = Gtk.Spinner()
        spinner.set_size_request(48, 48)
        spinner.start()
        self.append(spinner)

        self._status_label = Gtk.Label(label=status)
        self._status_label.add_css_class("loading-status")
        self.append(self._status_label)

    def set_status(self, text: str) -> None:
        self._status_label.set_text(text)
