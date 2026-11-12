"""Capitalize as you type in the chat compose boxes, like a phone keyboard.

A lowercase letter typed at the start of a message (after any ``@[Name]``
reply mentions) or after ``. ``, ``! `` or ``? `` becomes uppercase. Only
single typed characters are changed, so a paste is left alone and a letter
you lowercase again afterwards stays lowercase.
"""

from __future__ import annotations

import re

from gi.repository import Gtk

_LEADING_MENTIONS = re.compile(r"^\s*(?:@\[[^\]]*\]\s*)*")
_SENTENCE_END = re.compile(r"[.!?]\s+$")


def starts_sentence(before: str) -> bool:
    """True when a letter typed after *before* begins a sentence."""
    rest = before[_LEADING_MENTIONS.match(before).end() :]
    if not rest.strip():
        return True
    return bool(_SENTENCE_END.search(rest))


def capitalize_insert(old: str, new: str) -> tuple[str, int] | None:
    """If *new* is *old* with one lowercase letter typed where a sentence starts,
    return (*new* with that letter uppercased, its index). Otherwise None."""
    if len(new) != len(old) + 1:
        return None
    i = 0
    while i < len(old) and old[i] == new[i]:
        i += 1
    if new[i + 1 :] != old[i:]:
        return None
    ch = new[i]
    if not ch.islower() or ch.upper() == ch or not starts_sentence(new[:i]):
        return None
    return new[:i] + ch.upper() + new[i + 1 :], i


def attach_auto_capitalize(entry: Gtk.Entry) -> None:
    """Capitalize sentence starts in *entry* as they are typed."""
    state = {"text": entry.get_text(), "busy": False}

    def on_changed(editable: Gtk.Editable) -> None:
        if state["busy"]:
            return
        new = editable.get_text()
        fixed = capitalize_insert(state["text"], new)
        if fixed is not None:
            text, i = fixed
            state["busy"] = True
            try:
                editable.delete_text(i, i + 1)
                editable.insert_text(text[i], i)
                editable.set_position(i + 1)
            finally:
                state["busy"] = False
            new = editable.get_text()
        state["text"] = new

    entry.connect("changed", on_changed)
