"""The Stop button's inline keyboard.

TECH-DEBT #8 block 3, fallout. One function, extracted so that nothing
has to reach across the dispatcher for it.

Why it moved

`llm_thinking.push_thinking` needed this markup on every edit, and got
it with a lazy `from dispatch import _stop_button_markup`. dispatch
imports call_llama, call_llama imports llm_thinking, so the lazy import
was holding a seven-module runtime cycle open. The comment there said
as much -- and named dispatch as the owner, which was wrong: the
function has lived in `access.py` since TECH-DEBT #2, and
`llm_thinking` was reaching it through a re-export.

Adding `llm_stream` to T3.2 made this visible. That module imports
llm_thinking, so it inherited the cycle, and the honest reading of the
failure was "I made one more module cyclic" rather than "the cycle was
already there and my change made it bigger". The ledger now lists one
fewer module in it.

Kept verbatim, including the lazy `telegram` import. That import is
load-bearing for `llm_thinking`, which has no telegram dependency of
its own and is imported by `call_llama` at module scope.
"""

__all__ = ["_stop_button_markup"]


def _stop_button_markup():
    """Inline keyboard markup for the "💭 думаю…" thinking message.

    One button: "⏹ Stop". When the user clicks it, the callback
    handler looks up the per-(chat, user) abort event and sets
    it; call_llama() then bails out at the next iteration of
    the tool loop. The handler edits the thinking message to
    "⏹ Остановлено" and moves to the next update.

    We import InlineKeyboardButton lazily so this module is
    still importable in environments where the full PTB extras
    are not installed (selftest imports, for example).
    """
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⏹ Stop", callback_data="stop:thinking")],
    ])