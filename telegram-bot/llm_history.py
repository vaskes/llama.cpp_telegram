"""The bot's own record of a chat, and the tool that reads it.

The operator's words (2026-10-27): "дай боту инструмент чтения всей
истории из sqlite по неформальным параметрам (text, sender name и т.п.)
-- по сути select * из messages -- и находить нужные id".

What that is, in the bot's terms:

  * the record is the `messages` table -- what the bot was sent, what it
    absorbed from the group, and its own answers. It is the bot's own
    memory of the chat, and it is the thing the operator keeps for the
    training set;
  * the search is a *read*, and it is the first way the bot can reach a
    message it was sent an hour ago: the Bot API cannot list a chat's
    messages, so the record is the only place a message survives with
    its id attached;
  * the id the search returns is what `delete_messages` needs. Before
    this, the only ids in reach were the ones the live update carried
    (the message just written, the message replied to) and the bot's own
    messages. Now the bot can find the message somebody said three hours
    ago and take it back out of the chat.

Two things this module does NOT do, both on purpose:

  * it does not write. Every row it reads is the record, and the record
    is not a scratch pad;
  * it does not touch the delete tool's authorisation. That lives in
    `llm_delete`, which is not allowed to reach the database at all --
    the ids it may act on are handed to it as a parameter by the
    dispatcher, which asks this module what the record holds.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Optional

import storage
from logconf import get_logger

log = get_logger(__name__)

#: How many matches a search returns when the model does not say.
MAX_RESULTS = 20

#: The ceiling, however many the model asked for. A search that returns
#: the whole history is a search that fills the context window with the
#: history instead of answering the question.
MAX_LIMIT = 100

#: How much of a matched message the result carries. Enough to recognise
#: the message, not enough to paste the chat into the context.
EXCERPT_CHARS = 160


def search_messages_tool_def() -> dict:
    """The definition the model is shown for `search_messages`.

    The wording matters as much as the parameters, and the reason is the
    same as it was for the delete tool: a model that does not know what
    a tool is FOR will call it for the wrong thing, and this one reads
    the bot's own history -- which is not the same thing as the chat.
    """
    return {
        "type": "function",
        "function": {
            "name": "search_messages",
            "description": (
                "Search the bot's OWN RECORD of this chat: the messages it "
                "was sent, the ones it absorbed from the group, and its own "
                "answers. Use it when the user refers to something said "
                "earlier -- \"what did he say about X\", \"find my message "
                "about Y\", \"the message where he asked about Z\" -- and "
                "when the Telegram id of an earlier message is needed, for "
                "example to take it back out of the chat.\n\n"
                "The filters are informal and case-insensitive: `text` is a "
                "substring of the message body, `sender` a substring of the "
                "display name of the sender. With no filter it returns the "
                "newest messages of the chat.\n\n"
                "The result carries the Telegram id of each match, and that "
                "id can be passed to `delete_messages`. The record is the "
                "bot's own history, not Telegram: a message the bot never "
                "saw is not in it, and a message deleted from the chat "
                "stays in the record."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": (
                            "Case-insensitive substring of the body of the "
                            "message."
                        ),
                    },
                    "sender": {
                        "type": "string",
                        "description": (
                            "Case-insensitive substring of the display name "
                            "of the sender (\"dim\" finds \"Dmitriklim\")."
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "description": (
                            f"How many matches to return, newest first. "
                            f"Default {MAX_RESULTS}, at most {MAX_LIMIT}."
                        ),
                    },
                },
            },
        },
    }


def _text_of(raw: Any) -> str:
    """The text of a stored message, out of the JSON the row carries."""
    if not isinstance(raw, str):
        return ""
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return raw
    content = payload.get("content") if isinstance(payload, dict) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(str(part.get("text", "")) for part in content
                        if isinstance(part, dict))
    return ""


def _excerpt(text: str) -> str:
    """One line of the message, short enough to recognise it by."""
    flat = " ".join(text.split())
    if len(flat) > EXCERPT_CHARS:
        flat = flat[:EXCERPT_CHARS - 1].rstrip() + "…"
    return flat


def _ago(created_at: Any) -> str:
    """How long ago the message was, in words.

    The age is in the result because of Telegram's 48-hour window: a
    message that is three days old cannot be taken back out of the chat
    whatever the bot would like, and the model has to be able to say so
    before it tries.
    """
    try:
        delta = max(0.0, time.time() - float(created_at))
    except (TypeError, ValueError):
        return "unknown age"
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"


def _filter_text(raw: Any) -> Optional[str]:
    """The `text` filter, or None when there is none."""
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def _limit(raw: Any) -> int:
    """How many matches to return, inside the ceiling."""
    try:
        limit = int(raw)
    except (TypeError, ValueError):
        return MAX_RESULTS
    if limit < 1:
        return MAX_RESULTS
    return min(limit, MAX_LIMIT)


async def run_search(args: dict, *,
                     chat_id: Any) -> tuple[str, Optional[dict]]:
    """Search the chat's own record. Returns the text and the ledger.

    The text is what the model reads. The extras are what the turn's
    ledger keeps -- the count of the matches and the ids that were
    found, so that a turn can be read afterwards without re-running the
    search.
    """
    args = args or {}
    text = _filter_text(args.get("text"))
    sender = _filter_text(args.get("sender"))
    limit = _limit(args.get("limit"))
    try:
        rows = await asyncio.to_thread(
            storage.get_store().search_messages, chat_id,
            text=text, sender=sender, limit=limit)
    except Exception as exc:  # noqa: BLE001 - the tool must not raise
        log.info(f"[search_messages] the record of chat {chat_id} could "
                 f"not be read: {type(exc).__name__}: {exc}")
        return ("[search_messages: the bot's own record of this chat could "
                "not be read. Say that plainly; do not answer from "
                "memory.]"), {"matches": 0, "error": True}

    if not rows:
        return ("[search_messages: 0 results in the bot's own record of "
                "this chat. The bot only knows the messages it was sent, "
                "the ones it absorbed from the group, and its own "
                "answers.]"), {"matches": 0}

    lines = [f"[search_messages: {len(rows)} result(s) in the bot's own "
             f"record of this chat, newest first]"]
    found: list[int] = []
    for row in rows:
        tg_id = row.get("telegram_message_id")
        if tg_id is not None:
            found.append(int(tg_id))
        who = row.get("sender_name") or (
            "the bot" if row.get("role") == "assistant" else "unknown")
        where = (f"telegram id {tg_id}" if tg_id is not None
                 else "no telegram id")
        lines.append(f"- {where} | {who} | {_ago(row.get('created_at'))} | "
                     f"\"{_excerpt(_text_of(row.get('content')))}\"")
    if found:
        lines.append("A telegram id from this list can be passed to "
                     "delete_messages. The record is not touched by a "
                     "deletion: the message leaves the chat, the row "
                     "stays.")
    return "\n".join(lines), {"matches": len(rows), "ids": found}


async def known_ids(chat_id: Any, ids: list[int]) -> set[int]:
    """Which of these Telegram ids the bot's own record of this chat holds.

    This is the authorisation for a deletion. The bot can vouch for an
    id only if its own record holds it: the Bot API cannot list a chat's
    messages, so a number that is not in the record is a number the
    model produced on its own, and the bot is an administrator -- a made
    up id is a real attempt to delete somebody else's message.

    A failure to read the record is not an authorisation: it returns
    nothing, and the ids stay out of reach.
    """
    if not ids:
        return set()
    try:
        rows = await asyncio.to_thread(
            storage.get_store().search_messages, chat_id, ids=list(ids))
    except Exception as exc:  # noqa: BLE001 - the tool must not raise
        log.info(f"[search_messages] the record of chat {chat_id} could "
                 f"not be read, so no id is vouched for: "
                 f"{type(exc).__name__}: {exc}")
        return set()
    return {int(row["telegram_message_id"]) for row in rows
            if row.get("telegram_message_id") is not None}
