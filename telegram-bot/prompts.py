"""Prompt text and the regexes that parse what comes back.

TECH-DEBT #12 moved the prose out. This file used to be 422 lines of
module constants -- zero functions, all of it words the bot says -- with
the four regexes buried between them, so reviewing a wording change meant
scrolling past unrelated code and diffing text against text that had
nothing to do with the sentence being changed.

The text now lives in `prompt_text/`, one file per prompt. What stays
here is code: the four regexes, which compile and are consumed by other
modules. Everything imported below is re-exported so that the eight
modules doing `from prompts import GROUP_CONTEXT` keep working, and so
that there is exactly one place the bot's wording lives.

`_RATING_PREFIX_RE` is unchanged, and still accepts `[[RATE:11]]`; the
tech-debt note pins that as current behaviour. Tightening the alternation
changes what counts as a rating in production, which is a product
decision about malformed model output, not a refactoring one.
"""

import os
import re

# Rating mode is read from the environment, not from a prompt file:
# it is a switch, not a sentence.
RATING_MODE: bool = os.environ.get("RATING_MODE", "0") == "1"

from prompt_text import (
    BLOAT_EMOJI,
    BOT_COMMANDS,
    GROUP_CONTEXT,
    RATING_EMOJI,
    RATING_RULES,
    WELCOME_TEXT,
)

__all__ = [
    "BLOAT_EMOJI",
    "BOT_COMMANDS",
    "GROUP_CONTEXT",
    "RATING_EMOJI",
    "RATING_RULES",
    "RATING_MODE",
    "WELCOME_TEXT",
    "_EMOJI_CHAR_RE",
    "_LLM_TOKEN_RE",
    "_RATING_PREFIX_RE",
    "_SUBTALK_NAME_RE",
    "_TOPIC_NAME_RE",
]

# === Regexes. These are code, they compile, and other modules use them,
# so they stayed here rather than moving next to the prose they parse.
_RATING_PREFIX_RE = re.compile(
    r"^\s*\[\[TYPE:(?P<type>question|request|confirmation|info|statement|bloat)\]\]"
    r"(?:\s*\[\[RATE:(?P<rate>[1-9]|10)\]\])?"
    r"(?:\s*(?P<rest>.*))?$",
    re.DOTALL,
)

# Single-emoji-char regex for extracting the emoji from a bloat
# rest like "🤔" or "🤔 some text". Used only when TYPE is bloat.
_EMOJI_CHAR_RE = re.compile(
    r"^(\S{1,4})\b"
)

# === Command-name regexes (compiled once for hot path) ===
import re as _llm_re

_LLM_TOKEN_RE = _llm_re.compile(
    r"^\s*\[llm\](?=$|[\s.,!?;:])",
    _llm_re.IGNORECASE,
)
_SUBTALK_NAME_RE = _llm_re.compile(r'^[^\s]{1,32}$')
_TOPIC_NAME_RE = _llm_re.compile(r'^[^\s]{1,128}$')
