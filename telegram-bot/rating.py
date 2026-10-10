# rating.py
# Rating system: the LLM classifies each human message in a
# group (question / request / confirmation / info / statement /
# bloat) and applies a 1-10 emoji reaction. This module owns:
#   - the rating-mode gate (_is_rating_active)
#   - the response parser (_parse_rating_response)
#   - the dispatch helper (_apply_rating_and_persist)
#   - the Telegram reaction setter (_apply_reaction)
#   - the react_to_message tool executor (_execute_react_to_message)

import persistence
from prompts import (
    BLOAT_EMOJI, GROUP_CONTEXT, RATING_EMOJI, RATING_MODE,
    RATING_RULES, _EMOJI_CHAR_RE, _RATING_PREFIX_RE,
)


def is_rating_active(update) -> bool:
    """True iff the current update should be handled in rating mode.

    Rating mode is on when RATING_MODE=1 and the update came
    from a group. Private chats are never rated (the rating-
    mode structured output is meaningless in 1:1 context).

    Note: the dispatch filter already drops muted messages,
    so the `not _should_mute_in_group(update)` clause that
    used to live at the call sites is no longer needed; the
    helper is the single source of truth.
    """
    from bot import _is_group_chat  # late import: avoid circular at module load
    return bool(RATING_MODE) and _is_group_chat(update)


def parse_rating_response(text: str) -> dict:
    """Parse the LLM response for a RATING_MODE prefix.

    Returns a dict with:
      - type: "question" | "request" | "confirmation" | "info" |
              "statement" | "bloat" | "unknown"
      - rating: int (1..10) or None
      - emoji: str (matching RATING_EMOJI) or BLOAT_EMOJI
      - body: the original text with the prefix stripped

    The first line is checked for the [[TYPE:...]] [[RATE:N]]
    pattern. If no prefix is found, the whole text is treated
    as the body and type defaults to "unknown".
    """
    if not text:
        return {"type": "unknown", "rating": None, "emoji": None, "body": ""}
    m = _RATING_PREFIX_RE.match(text)
    if not m:
        return {"type": "unknown", "rating": None, "emoji": None, "body": text}
    type_ = m.group("type")
    rate_str = m.group("rate")
    body = text[m.end():].lstrip()
    rating = int(rate_str) if rate_str else None
    if type_ in ("info", "statement") and rating is not None:
        emoji = RATING_EMOJI[rating]
    elif type_ == "bloat":
        emoji = BLOAT_EMOJI
    else:
        emoji = None
    return {"type": type_, "rating": rating, "emoji": emoji, "body": body}


async def apply_rating_and_persist(
    context, update, chat_id: int, thread_id, bot_response: str, rating_active: bool,
) -> str:
    """Apply RATING_MODE dispatch logic and persist the assistant turn.

    Behaviour:
      - rating_active=False: persist bot_response as the assistant
        turn; return it unchanged for the caller to send as text.
      - rating_active=True + parseable [[TYPE:...]] [[RATE:N]]:
        parse the prefix, apply a Telegram reaction with the
        parsed emoji, persist an empty assistant turn with the
        numeric rating, return "" (caller skips text reply).
      - rating_active=True + bloat type: apply the bloat emoji
        (BLOAT_EMOJI), persist empty turn, return "".
      - rating_active=True + question/request/confirmation type:
        persist the full bot_response as the assistant turn
        (so the structured prefix goes into the history),
        return the original bot_response for the caller to
        send as text.

    Returns the (possibly empty) text to reply with to the user.
    """
    if not rating_active:
        await persistence.persist(chat_id, thread_id, "assistant", bot_response)
        return bot_response
    parsed = parse_rating_response(bot_response)
    if parsed["type"] in ("info", "statement") and parsed["rating"] is not None:
        await apply_reaction(
            context, update.effective_chat.id,
            update.message.message_id, parsed["emoji"],
        )
        await persistence.persist(
            chat_id, thread_id, "assistant", "",
            rating=parsed["rating"],
        )
        return ""  # signal: no text reply
    if parsed["type"] == "bloat":
        await apply_reaction(
            context, update.effective_chat.id,
            update.message.message_id, parsed["emoji"],
        )
        await persistence.persist(chat_id, thread_id, "assistant", "")
        return ""  # signal: no text reply
    # question / request / confirmation: persist full response
    # (with the structured prefix) and reply with it.
    await persistence.persist(chat_id, thread_id, "assistant", bot_response)
    return bot_response


async def apply_reaction(context, chat_id: int, message_id: int, emoji: str) -> None:
    """Set a single-emoji Telegram reaction on a message.

    Best-effort: if the bot lacks permission, the message is gone,
    or the emoji is not in the allowed set, we log and move on.
    The bot's primary contract is "do the right thing silently";
    a missing reaction should never break the handler.
    """
    from telegram import ReactionTypeEmoji
    try:
        await context.bot.set_message_reaction(
            chat_id=chat_id, message_id=message_id,
            reaction=[ReactionTypeEmoji(emoji=emoji)],
        )
        print(
            f"[rating] set {emoji!r} on chat_id={chat_id} message_id={message_id}",
            flush=True,
        )
    except Exception as e:
        # REACTION_NOT_ALLOWED, MESSAGE_ID_INVALID, etc. are
        # all expected in some channels. Log and move on.
        print(
            f"[rating] set_message_reaction FAILED: {type(e).__name__}: {e!r}",
            flush=True,
        )


async def execute_react_to_message(args, *, bot, chat_id, default_message_id):
    """Executor for the LLM-callable `react_to_message` tool.

    Lets the LLM apply an emoji reaction to a message in the
    current chat. Used for ad-hoc reactions outside the
    structured rating-mode path (e.g. "👍" on a fact the LLM
    agrees with mid-conversation).

    Args (from the LLM's tool call):
      - emoji (str, required): the emoji to apply
      - message_id (int, optional): the message to react to.
        If omitted, falls back to the bot's current message
        (the user's message that triggered the turn).

    Returns a short status string for the LLM to read.
    """
    from telegram import ReactionTypeEmoji
    emoji = (args.get("emoji") or "").strip()
    if not emoji:
        return "[bot: react_to_message requires a non-empty 'emoji' parameter. Use a single emoji like 👍 or 😐.]"
    msg_id = args.get("message_id")
    try:
        msg_id = int(msg_id) if msg_id is not None else None
    except (TypeError, ValueError):
        return f"[bot: react_to_message: message_id must be an integer, got {msg_id!r}]"
    if msg_id is None:
        msg_id = default_message_id
    if msg_id is None:
        return "[bot: react_to_message: no target message_id available - pass one in args, or call from a handler that provides a default.]"
    try:
        await bot.set_message_reaction(
            chat_id=chat_id, message_id=msg_id,
            reaction=[ReactionTypeEmoji(emoji=emoji)],
        )
        print(f"[react_tool] set {emoji!r} on chat_id={chat_id} message_id={msg_id}", flush=True)
        return f"[bot: OK - set {emoji} reaction on message_id={msg_id} in chat_id={chat_id}]"
    except Exception as e:
        print(f"[react_tool] set_message_reaction FAILED: {type(e).__name__}: {e!r}", flush=True)
        return f"[bot: react_to_message failed: {type(e).__name__}: {e!r}]"


# === Backward-compat shims (re-exports matching the old
# private names in bot.py so existing call sites still work
# during the staged refactor). ===
def _is_rating_active(update) -> bool:
    return is_rating_active(update)


def _parse_rating_response(text: str) -> dict:
    return parse_rating_response(text)


async def _apply_rating_and_persist(*args, **kwargs):
    return await apply_rating_and_persist(*args, **kwargs)


async def _apply_reaction(*args, **kwargs):
    return await apply_reaction(*args, **kwargs)


async def _execute_react_to_message(*args, **kwargs):
    return await execute_react_to_message(*args, **kwargs)
