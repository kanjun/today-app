"""Login codes never connect a conversation to a task (the user, 2026-11-15: "they just add noise")."""

import pytest
from today.conversation_matching import is_code


@pytest.mark.parametrize("text", ["217903", "Lmk if code received!", "(Text w code)",
                                  "Your bank verification code is 448812", "G-123456 is your Google verification code"])
def test_a_login_code_or_passing_one_along_is_a_code(text: str) -> None:
    assert is_code(text)


@pytest.mark.parametrize("text", ["12/2 at 1p works! Please send invoice", "Here is the code for the demo repo, 2026 version",
                                  "Can you review my code on the onboarding PR?", "Call me at 4155551234"])
def test_ordinary_messages_are_not_codes(text: str) -> None:
    assert not is_code(text)
