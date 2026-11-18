"""The open card names its own parts c5-something, so the page's other styles never reach into it.

The user, 2026-11-15: the message a task came from showed in bigger text than the rest of the
thread. The card marked it "src", a name the page already styles at 15px for a row's source line.
"""

import re
from pathlib import Path

import pytest

ASSETS = Path(__file__).parent / "src" / "today" / "assets"
# Names the card shares with the page on purpose: the page's own buttons and its error line.
SHARED = {"btn", "primary", "quiet", "link", "err", "on"}


def _card_class_names(card: str) -> set[str]:
    names: set[str] = set()
    for m in re.finditer(r'class="([^"]*)"', card):
        for name in re.sub(r"\$\{[^}]*\}", " ", m.group(1)).split():
            if re.fullmatch(r"[a-z][a-z0-9-]*", name):
                names.add(name)
    # Names added inside a class attribute by a condition: ${cond ? " name" : ""}
    names.update(re.findall(r'\?\s*"\s([a-z][a-z0-9-]*)"', card))
    return names


@pytest.mark.frontend
def test_card_parts_do_not_share_names_the_page_styles() -> None:
    card = (ASSETS / "card.js").read_text()
    page = (ASSETS / "app.html").read_text() + (ASSETS / "today_list.js").read_text()
    page_styled = set(re.findall(r"\.([a-z][a-z0-9-]*)", page))
    clashing = sorted(n for n in _card_class_names(card) if not n.startswith("c5") and n in page_styled and n not in SHARED)
    assert clashing == [], f"card parts named like page styles: {clashing}"


@pytest.mark.frontend
def test_the_message_a_task_came_from_has_a_card_name() -> None:
    card = (ASSETS / "card.js").read_text()
    assert '" c5src"' in card and ".c5msg.c5src{" in card and '" src"' not in card
