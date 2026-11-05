"""GTK widget utility functions."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk


def clear_children(container: Gtk.Widget) -> None:
    """Remove all children from a container widget (Gtk.Box, etc.)."""
    while True:
        child = container.get_first_child()
        if child is None:
            break
        container.remove(child)


def clear_listbox(listbox: Gtk.ListBox) -> None:
    """Remove all rows from a ListBox."""
    while True:
        row = listbox.get_row_at_index(0)
        if row is None:
            break
        listbox.remove(row)


def capitalize_first(text: str) -> str:  # [capitalize patch]
    """Upper-case the first letter of a message, after any leading @[mention]s;
    a message starting with a link is left as it is."""
    i = 0
    while text.startswith("@[", i):
        j = text.find("]", i)
        if j < 0:
            break
        i = j + 1
        while i < len(text) and text[i] == " ":
            i += 1
    if i < len(text) and text[i].islower() and not text.startswith(("http://", "https://", "www."), i):
        return text[:i] + text[i].upper() + text[i + 1:]
    return text


def navigate(widget: Gtk.Widget, page: str, then: tuple[str, str] | None = None) -> bool:
    """Navigate to *page* via the MainWindow, optionally calling a method on the target.

    *then* is a ``(method_name, arg)`` tuple — e.g. ``("select_peer", peer_id)``.
    Returns True if navigation succeeded (useful for activate-link handlers).
    """
    root = widget.get_root()
    if root is None:
        return False
    nav = getattr(root, "navigate_to", None)
    if nav is None:
        return False
    nav(page, then)
    return True
