"""The `delete_messages` tool: take a message back out of the chat.

The operator's request (2026-10-26), in his words: "бот сейчас в канале
админ, можно сделать ему инструмент, чтобы он мог удалять сообщения
в ТЕЛЕГРАММЕ ... чтобы он мог чистить историю сообщений". The bot is
an administrator in the admin channel, and the channel had no way to be
cleaned: the bot could post, it could react, and it could not take
anything back. A wrong answer, a duplicated answer, a wall of console
output -- all of it stayed, and only a human with a phone could remove
it.

The tool removes messages from the chat the model is talking in. That is
the whole of its reach, and the shape of the tool says so: there is no
`chat_id` parameter. The model's reach is the conversation it is in, so
a prompt injection hidden in a document cannot aim the tool at another
chat. The same reasoning the sandbox's session_id follows (T57 #S16):
one input, and it is not the model's to choose.

WHAT IT DOES NOT DO
It does not touch the bot's own database. The operator's rule, the same
evening: "sqlite наша священная корова -- источник истины и всего
того, что происходило в канале. Базу не трогаем." A deleted message
stays in the history the bot keeps, because that history is the record
of what happened; a tool that erased the record would leave the bot
believing something the chat no longer shows. So this module does not
import storage at all, and `tests/test_delete_messages_tool.py` checks
that it does not.

THE RIGHTS ARE TELEGRAM'S, NOT OURS
A bot may delete its own messages anywhere. Other people's messages only
where the bot is an administrator, and only for 48 hours -- Telegram
refuses the rest. The tool checks the administrator's right BEFORE it
deletes anything, and it reports Telegram's own answer when Telegram
refuses. It never says "deleted" about a message that is still there.

HOW "MY LAST ANSWER" IS FOUND
The Bot API cannot list a chat's messages, so the only way to know what
the bot sent is to remember it. `remember_answer` is called by the
answer path (`handlers._send_answer_text`) and keeps the last 50 sent
answers per chat, in memory, in this process -- and, since
2026-10-26, on disk as well, in a small log of ids.

THE LOG IS NOT THE DATABASE
The log holds ids and nothing else: which messages the bot sent, so
that the tool can take them back after a restart. The operator's rule
about the database stands unchanged -- the database is the record of
what happened in the chat, and this module does not import it. The log
is the bot's own memory of its own actions, and it lives next to the
database file, not inside it.

Without the log a restart empties the registry, and the tool says it
does not know the ids -- the truth, but useless to the operator who
wants the last four messages gone. The operator hit exactly that on
2026-10-26: the bot was redeployed, the channel had four bot messages
in it, and the tool could not name a single one of them.
"""
from __future__ import annotations

import os
import time
from collections import defaultdict, deque
from typing import Any, Optional

__all__ = [
    "delete_messages_tool_def",
    "execute_delete_messages",
    "remember_answer",
    "new_answer_group",
    "bot_may_delete",
    "recent_ids",
]


#: Where the bot's own sends are remembered across a restart. NOT the
#: database: the database is the record of what happened in the chat,
#: and the operator's rule is that nothing writes to it. This file
#: holds no content -- only the ids of the messages the bot sent.
SENT_LOG_PATH = "/app/data/sent_messages.jsonl"


#: How many of the bot's own sent messages are remembered per chat. A
#: mistake that needs undoing is recent; a hundred messages back is not.
_MAX_KEPT_PER_CHAT = 50

#: The most the tool will delete in one call. Telegram's own
#: `deleteMessages` takes 100, but a tool the model can call should not
#: be able to empty a channel in one word.
_MAX_DELETE_COUNT = 20

#: How long the administrator's rights are believed. They cost an API
#: call, and an administrator does not usually change between turns.
_ADMIN_TTL_SECONDS = 300.0


# === The registry of what the bot sent ===

#: chat_id -> deque of (message_id, group). The group is the answer the
#: message belongs to: one answer can be several messages, and they go
#: together.
_SENT: dict[int, deque[tuple[int, int]]] = defaultdict(deque)

_GROUP_SEQ = 0

#: Whether the log has been read into the registry already. One read
#: per process: the log is a startup thing, not a per-call thing.
_LOG_LOADED = False


# === The log of what the bot sent ===


def _log_path() -> str:
    return SENT_LOG_PATH


def _load_log() -> None:
    """Read the log into the registry, once per process.

    A restart empties the memory, and the operator's channel does not
    stop talking while the bot is down. The log is what the bot sent
    before the restart, so the tool can still name those messages.
    """
    global _LOG_LOADED, _GROUP_SEQ
    if _LOG_LOADED:
        return
    _LOG_LOADED = True
    try:
        with open(_log_path(), "r", encoding="utf-8") as fh:
            for line in fh:
                parts = line.rstrip("\n").split("\t")
                if len(parts) != 3:
                    continue
                try:
                    chat = int(parts[0])
                    message = int(parts[1])
                    group = int(parts[2])
                except ValueError:
                    continue
                queue = _SENT[chat]
                queue.append((message, group))
                while len(queue) > _MAX_KEPT_PER_CHAT:
                    queue.popleft()
                if group > _GROUP_SEQ:
                    # The group counter must not reuse a group id from
                    # the log: two different answers sharing a group
                    # would be deleted as one.
                    _GROUP_SEQ = group
    except OSError:
        # No log yet, or it cannot be read. The tool then knows only
        # what it sends from here on -- which it says out loud.
        return
    _rewrite_log()


def _append_log(chat_id: int, message_id: int, group: int) -> None:
    """Write one sent message to the log."""
    try:
        with open(_log_path(), "a", encoding="utf-8") as fh:
            fh.write(f"{chat_id}\t{message_id}\t{group}\n")
    except OSError:
        # The log is a convenience, not the chat's record. If it cannot
        # be written, the tool still works for this process.
        return


def _rewrite_log() -> None:
    """Write the registry back to the log, dropping what was forgotten.

    The log grows with every answer, so it is rewritten when the
    registry is loaded and when something is deleted. The write goes
    through a temporary file, so a crash mid-write cannot leave a
    half-written log behind.
    """
    path = _log_path()
    tmp = f"{path}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            for chat, queue in _SENT.items():
                for message, group in queue:
                    fh.write(f"{chat}\t{message}\t{group}\n")
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return


def new_answer_group() -> int:
    """Start a new answer group.

    The answer path sends one answer as one or more messages (Telegram
    caps a message at 4096 characters). The group ties those messages
    together, so "delete my last answer" removes the whole answer and
    not a fragment of it.
    """
    global _GROUP_SEQ
    _load_log()
    _GROUP_SEQ += 1
    return _GROUP_SEQ


def remember_answer(chat_id: Any, message_id: Any, group: int) -> None:
    """Remember one message of an answer the bot just sent."""
    if chat_id is None or message_id is None:
        return
    try:
        key = int(chat_id)
        msg = int(message_id)
    except (TypeError, ValueError):
        return
    _load_log()
    queue = _SENT[key]
    queue.append((msg, int(group)))
    while len(queue) > _MAX_KEPT_PER_CHAT:
        queue.popleft()
    _append_log(key, msg, int(group))


def recent_ids(chat_id: Any, limit: int = 10) -> list[int]:
    """The ids of the bot's own recent messages in this chat, newest first.

    The model is shown these, because a model that wants to delete a
    message and has no id will invent one. Invented ids are not a
    small mistake: the bot is an administrator, so it would really try
    to delete a stranger's message.
    """
    if chat_id is None:
        return []
    try:
        key = int(chat_id)
    except (TypeError, ValueError):
        return []
    _load_log()
    queue = _SENT.get(key)
    if not queue:
        return []
    return [message for message, _ in list(reversed(queue))[:limit]]


def forget_messages(chat_id: Any, message_ids: list[int]) -> None:
    """Drop messages that are gone from the chat, so the next round does
    not try to delete them again."""
    try:
        key = int(chat_id)
    except (TypeError, ValueError):
        return
    queue = _SENT.get(key)
    if not queue:
        return
    dead = {int(m) for m in message_ids}
    _SENT[key] = deque(entry for entry in queue if entry[0] not in dead)
    _rewrite_log()


def _answer_groups(chat_id: Any, count: int) -> list[list[int]]:
    """The last `count` answers, newest first, each as its message ids."""
    try:
        key = int(chat_id)
    except (TypeError, ValueError):
        return []
    _load_log()
    queue = _SENT.get(key)
    if not queue:
        return []
    order: list[int] = []
    by_group: dict[int, list[int]] = {}
    current: Optional[int] = None
    for message_id, group in reversed(queue):
        # The messages of one answer are sent one after another, so the
        # group changes once and then the older answer begins. Stop
        # there -- the last group must be collected WHOLE, or "delete my
        # last answer" would leave half of it in the chat.
        if current is not None and group != current and len(order) >= count:
            break
        if group not in by_group:
            by_group[group] = []
            order.append(group)
        by_group[group].append(message_id)
        current = group
    return [by_group[group] for group in order]


# === The rights to delete ===


_admin_cache: dict[int, tuple[float, bool, str]] = {}


async def _bot_user(bot: Any):
    """The bot's own user object, without a second API call if PTB has
    it cached (it does, after `initialize()`)."""
    try:
        return bot.bot
    except Exception:
        return await bot.get_me()


async def bot_may_delete(bot: Any, chat_id: Any,
                         chat_type: str) -> tuple[bool, str]:
    """Whether the bot may delete messages in this chat, and why not.

    In a private chat the bot owns the conversation and may delete its
    own messages there. In a group, a supergroup or a channel it may
    only delete other people's messages if it is an administrator with
    the right to delete them.
    """
    if chat_type in ("private", "sender"):
        return True, ""
    try:
        key = int(chat_id)
    except (TypeError, ValueError):
        return False, "the chat id is not a number, so the rights are unknown"
    now = time.time()
    cached = _admin_cache.get(key)
    if cached and cached[0] > now:
        return cached[1], cached[2]
    allowed, reason = await _check_admin(bot, key)
    _admin_cache[key] = (now + _ADMIN_TTL_SECONDS, allowed, reason)
    return allowed, reason


async def _check_admin(bot: Any, chat_id: int) -> tuple[bool, str]:
    try:
        me = await _bot_user(bot)
        member = await bot.get_chat_member(chat_id, me.id)
    except Exception as e:
        # The check is INCONCLUSIVE, not negative. The bot is in this
        # chat by construction -- it just received an update from it --
        # so a failure to read the rights is a quirk of the chat (a
        # channel can answer differently from a group), not a verdict.
        # Hiding the tool because of a quirk would leave the operator
        # with a tool that does not exist and no explanation. Telegram
        # has the last word on what may be deleted anyway.
        return True, (f"the bot could not read its own rights in this "
                      f"chat ({type(e).__name__}: {e}) -- Telegram will "
                      f"have the last word")
    status = getattr(member, "status", "") or ""
    if status == "creator":
        return True, ""
    if status == "administrator":
        # `can_delete_messages` is reported for supergroups and
        # channels. A basic group does not have the field at all, and
        # there an administrator may delete any message.
        #
        # An administrator WITHOUT the right is still an administrator:
        # it may take back its own messages, and that is half the tool.
        # The right decides the other half -- other people's messages.
        # So the tool stays available, and the operator is told which
        # half they have.
        right = getattr(member, "can_delete_messages", None)
        if right is None or right:
            return True, ""
        return True, ("the bot is an administrator here, but without the "
                      "\"delete messages\" right: it can take back its own "
                      "messages, not other people's")
    return False, (f"the bot is not an administrator in this chat (status: "
                   f"{status or 'unknown'}), so it cannot delete other "
                   f"people's messages here")


# === The tool ===


def delete_messages_tool_def(*, chat_id: Any = None,
                              reply_to_message_id: Any = None) -> dict:
    """The definition the model is shown for `delete_messages`.

    The description carries the ids the bot actually knows: the ids of
    its own last messages in this chat, and the id of the message the
    user replied to. A model that has an id will use it; a model
    without one will invent one, and the bot is an administrator, so an
    invented id is a real attempt to delete a stranger's message.
    """
    known = recent_ids(chat_id)
    described = (
        "Delete messages from this Telegram chat. Use it when the "
        "user asks you to take something back: \"delete your last "
        "answer\", \"remove that message\", \"clean up the channel\". By "
        "default it removes YOUR own last answer in this chat (the "
        "whole answer, even if it was sent as several messages); "
        "`count` removes that many of your answers, newest first. "
        "`message_ids` removes exactly the messages with those "
        "Telegram ids -- use it when the user names an id. The bot "
        "can only delete other people's messages where it is an "
        "administrator, and Telegram only lets it delete messages "
        "less than 48 hours old, so a refusal is Telegram's answer and "
        "not yours. The bot's own record of the chat is NOT touched: "
        "what happened stays in its history."
    )
    if known:
        described += (
            " The ids of YOUR last messages in this chat are: "
            + ", ".join(str(m) for m in known)
            + ". Use those ids, or use `count`."
        )
    if reply_to_message_id is not None:
        described += (
            f" The user replied to the message with id "
            f"{reply_to_message_id} -- that is the message they mean "
            f"when they say \"this one\" or \"that message\"."
        )
    described += (
        " NEVER invent a message id: use only the ids you were given "
        "here or the ones the user named. If you have no id, ask for it."
    )
    return {
        "type": "function",
        "function": {
            "name": "delete_messages",
            "description": described,
            "parameters": {
                "type": "object",
                "properties": {
                    "count": {
                        "type": "integer",
                        "description": (
                            "How many of your own last answers to delete in "
                            f"this chat (1..{_MAX_DELETE_COUNT}, default 1). "
                            "One answer is one reply, even when it was sent "
                            "as several messages."
                        ),
                    },
                    "message_ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": (
                            "Telegram message ids to delete exactly. Use it "
                            "when the user names an id. It wins over "
                            "`count`."
                        ),
                    },
                },
            },
        },
    }


def _parse_count(raw: Any) -> tuple[int, Optional[str]]:
    """The requested count, and the reason it is not acceptable."""
    if raw is None:
        return 1, None
    try:
        count = int(raw)
    except (TypeError, ValueError):
        return 0, f"count {raw!r} is not a number"
    if count < 1:
        return 0, f"count {count} is not at least 1"
    if count > _MAX_DELETE_COUNT:
        return 0, (f"count {count} is above the limit of "
                   f"{_MAX_DELETE_COUNT} messages per call")
    return count, None


def _parse_ids(raw: Any) -> tuple[list[int], Optional[str]]:
    """The requested message ids, and the reason they are not usable."""
    if raw is None:
        return [], None
    if not isinstance(raw, (list, tuple)):
        return [], f"message_ids {raw!r} is not a list"
    ids: list[int] = []
    for item in raw:
        try:
            ids.append(int(item))
        except (TypeError, ValueError):
            return [], f"message id {item!r} is not a number"
    if not ids:
        return [], "message_ids is empty"
    if len(ids) > _MAX_DELETE_COUNT:
        return [], (f"{len(ids)} message ids is above the limit of "
                    f"{_MAX_DELETE_COUNT} per call")
    return ids, None


async def execute_delete_messages(
    args: dict,
    *,
    bot: Any,
    chat_id: Any,
    chat_type: str,
) -> tuple[str, Optional[dict]]:
    """Delete messages from the current chat.

    Returns the text the model sees and the extra fields for the turn's
    ledger. The ledger is the record of what the tool did; it is not
    the bot's history, and nothing here writes to the database.
    """
    args = args or {}
    count, count_problem = _parse_count(args.get("count"))
    if count_problem:
        return f"[delete_messages: {count_problem}]", None
    ids, ids_problem = _parse_ids(args.get("message_ids"))
    if ids_problem:
        return f"[delete_messages: {ids_problem}]", None

    if ids:
        targets = ids
        scope = "the requested message ids"
    else:
        groups = _answer_groups(chat_id, count)
        if not groups:
            return ("[delete_messages: I do not know which messages to "
                    "delete. I know the ids of the messages I sent "
                    "recently, and of nothing else. If the user replied "
                    "to a message, delete that one -- its id is in my "
                    "tool description. Otherwise ask the user for the "
                    "id, or use `count` to take back my last answer.]"), None
        targets = [msg for group in groups for msg in group]
        scope = f"my last {len(groups)} answer(s)"

    allowed, reason = await bot_may_delete(bot, chat_id, chat_type)
    if not allowed:
        return (f"[delete_messages: refused -- {reason}. Only messages the "
                f"bot may delete are in reach.]"), None

    deleted: list[int] = []
    failed: list[tuple[int, str]] = []
    for message_id in targets:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
            deleted.append(message_id)
        except Exception as e:
            failed.append((message_id, f"{type(e).__name__}: {e}"))

    if deleted:
        forget_messages(chat_id, deleted)

    if not failed:
        text = (f"[delete_messages: deleted {len(deleted)} message(s) from "
                f"{scope} (ids {', '.join(str(m) for m in deleted)})]")
    elif not deleted:
        detail = "; ".join(f"{mid}: {why}" for mid, why in failed)
        text = (f"[delete_messages: nothing was deleted ({detail}). "
                f"Telegram's rules, not mine.]")
        if reason:
            text = text[:-1] + f" {reason}.]"
    else:
        detail = "; ".join(f"{mid}: {why}" for mid, why in failed)
        text = (f"[delete_messages: deleted {len(deleted)} of "
                f"{len(targets)} message(s) from {scope}; Telegram refused "
                f"the rest -- {detail}]")
        if reason:
            text = text[:-1] + f" ({reason}).]"

    extras = {"deleted": deleted, "failed": [mid for mid, _ in failed]}
    return text, extras
