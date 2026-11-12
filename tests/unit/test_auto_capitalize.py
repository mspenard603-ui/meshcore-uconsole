"""Compose boxes capitalize sentence starts as you type (widgets/auto_capitalize.py)."""

from __future__ import annotations

import pytest

pytest.importorskip("gi")

from meshcore_console.ui_gtk.widgets.auto_capitalize import (  # noqa: E402
    capitalize_insert,
    starts_sentence,
)


def _type(text: str) -> str:
    """Type *text* one character at a time, as the entry would see it."""
    out = ""
    for ch in text:
        fixed = capitalize_insert(out, out + ch)
        out = fixed[0] if fixed else out + ch
    return out


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("hello there", "Hello there"),
        ("ok. see you. bye", "Ok. See you. Bye"),
        ("really? yes! cool", "Really? Yes! Cool"),
        ("@[Oak Hill] thanks", "@[Oak Hill] Thanks"),
        ("@[a] @[b] both", "@[a] @[b] Both"),
        ("  leading space", "  Leading space"),
        ("see example.com now", "See example.com now"),
        ("123 go", "123 go"),
        ("ok... fine", "Ok... Fine"),
    ],
)
def test_typing(typed: str, expected: str) -> None:
    assert _type(typed) == expected


def test_reply_prefill_then_typing() -> None:
    # The reply arrow sets "@[Bob] " in one go; the first typed letter is capitalized.
    assert capitalize_insert("@[Bob] ", "@[Bob] h") == ("@[Bob] H", 7)


def test_paste_and_edits_are_left_alone() -> None:
    assert capitalize_insert("", "hello") is None  # paste
    assert capitalize_insert("Hello", "hello"[:0] + "ello") is None  # deletion
    # Lowercasing the first letter by hand: delete "H", type "h" -> capitalized again
    # only because it is typed; replacing via a multi-char change is not touched.
    assert capitalize_insert("ello", "hello") == ("Hello", 0)


def test_insert_in_middle() -> None:
    assert capitalize_insert("Hi. there", "Hi. xthere") == ("Hi. Xthere", 4)
    assert capitalize_insert("Hi there", "Hi xthere") is None


def test_starts_sentence() -> None:
    assert starts_sentence("")
    assert starts_sentence("Done. ")
    assert not starts_sentence("Done.")
    assert not starts_sentence("word ")
