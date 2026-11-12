"""Messages that @-mention you are picked out (widgets/mention.py). [mention-glow patch]"""

from __future__ import annotations

import pytest

pytest.importorskip("gi")

from meshcore_console.ui_gtk.widgets.mention import mentions_name  # noqa: E402


@pytest.mark.parametrize(
    "body",
    [
        "@[mikes603] that would be a phenomenal location!",
        "@[MIKES603] hi",
        "thanks @[mikes603]",
        "@mikes603 that works",
        "ok @Mikes603, see you",
        "@[Oak Hill Mobile] @[mikes603] both of you",
    ],
)
def test_mentions_you(body: str) -> None:
    assert mentions_name(body, "mikes603")


@pytest.mark.parametrize(
    "body",
    [
        "Going to try and get access on the top of 1000ElmSt",
        "@[mikes6030] someone else",
        "@mikes6030 someone else",
        "@mikes603-2 a different node",
        "mikes603 without an at",
        "@[Oak Hill Mobile] hi",
    ],
)
def test_does_not_mention_you(body: str) -> None:
    assert not mentions_name(body, "mikes603")


def test_name_with_spaces_and_no_name() -> None:
    assert mentions_name("@[Oak Hill Mobile] hello", "Oak Hill Mobile")
    assert not mentions_name("@[x] hi", "")
    assert not mentions_name("@[x] hi", None)
