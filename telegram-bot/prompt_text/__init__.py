"""Prompt text as files, not as a Python module.

TECH-DEBT #12. `prompts.py` was 422 lines of module constants --
zero functions, all prose -- which means a review of it is a review
of the words the bot says, and the words were impossible to diff
because any change mixed prose with the four regexes living between
them.

The regexes stay in `prompts.py`, which is now a facade: they are
code, they compile, and they belong with the code that applies them.
Everything here is text, and text belongs in its own file.

Every constant is byte-identical to what it was. The sha256 pins in
tests/test_prompts.py are the contract, and they are checked against
these files rather than against `prompts`.
"""
from prompt_text.commands import BOT_COMMANDS
from prompt_text.emoji import BLOAT_EMOJI, RATING_EMOJI
from prompt_text.group_context import GROUP_CONTEXT
from prompt_text.rating_rules import RATING_RULES
from prompt_text.welcome import WELCOME_TEXT

__all__ = ["BOT_COMMANDS", "BLOAT_EMOJI", "GROUP_CONTEXT",
           "RATING_EMOJI", "RATING_RULES", "WELCOME_TEXT"]
