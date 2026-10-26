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
from typing import Any, NamedTuple, Optional

from logconf import get_logger

log = get_logger(__name__)

__all__ = [
    "delete_messages_tool_def",
    "execute_delete_messages",
    "remember_answer",
    "new_answer_group",
    "bot_may_delete",
    "recent_ids",
    "own_ids",
    "authorized_ids",
    "telegram_refusal",
    "Rights",
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
    except FileNotFoundError:
        # There is no log. On a fresh install that is normal; on a
        # deployed bot it is the signature of the failure of
        # 2026-10-26 -- the tool knew no ids, and the model invented
        # some. So it is a line in the log, not a debug note: it is
        # read right after a deploy, and it is the first question to
        # ask when "delete this message" does not work.
        log.info("[delete_messages] no id log at %s yet; the tool knows "
                 "only what it sends from here on", _log_path())
        return
    except OSError as e:
        # The log exists but cannot be read. That is not the normal
        # first run, and it costs the tool its memory across a deploy
        # -- so it is a warning, not a debug line.
        log.warning("[delete_messages] cannot read the id log %s: %s; the "
                    "tool knows only what it sends from here on",
                    _log_path(), e)
        return
    log.info("[delete_messages] id log %s loaded: %d id(s) in %d chat(s)",
             _log_path(), sum(len(q) for q in _SENT.values()), len(_SENT))
    _rewrite_log()


def _append_log(chat_id: int, message_id: int, group: int) -> None:
    """Write one sent message to the log."""
    try:
        with open(_log_path(), "a", encoding="utf-8") as fh:
            fh.write(f"{chat_id}\t{message_id}\t{group}\n")
    except OSError as e:
        # The log is a convenience, not the chat's record, so the tool
        # still works for this process without it. But it is NOT
        # allowed to fail quietly: on 2026-10-26 the log of ids was
        # empty for a whole deployment window, the tool description
        # carried no ids at all because of it, and the model answered
        # "delete this message" with numbers it had made up. A silent
        # failure here is a silent failure of the user's request.
        log.warning("[delete_messages] could not write the id log %s: %s",
                    _log_path(), e)
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
    except OSError as e:
        log.warning("[delete_messages] could not rewrite the id log %s: %s",
                    path, e)
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


def own_ids(chat_id: Any) -> list[int]:
    """Every id of the bot's own messages the registry knows in this chat.

    `recent_ids` is what the model is shown (the newest ten). This is
    the whole set, and it is what the tool is allowed to delete: the
    bot may take back anything it sent here.
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
    return [message for message, _ in queue]


def authorized_ids(chat_id: Any,
                   reply_to_message_id: Any = None,
                   current_message_id: Any = None) -> set[int]:
    """The ids this tool is allowed to delete in this chat.

    Two sources, and both of them are verifiable:

    - the ids of the bot's own messages (the registry);
    - the id of the message the user replied to (the turn's context).

    Everything else is a number the model produced on its own. The bot
    is an administrator in the chat where this tool lives, so a number
    the model made up is a real attempt to delete a stranger's message.
    The tool therefore refuses such an id itself, without asking
    Telegram to delete anything: the damage of an invented id must not
    depend on Telegram's willingness to carry it out.

    The live test of 2026-10-26 is the reason. The model answered
    "delete this message" with ids 706 and 707 -- numbers it had made
    up -- and Telegram's refusal ("Message can't be deleted", the
    48-hour window) came back to the user as a story about the bot
    not being an administrator. It IS one. The refusal never named the
    real cause, so the model invented one.
    """
    allowed = set(own_ids(chat_id))
    for extra in (reply_to_message_id, current_message_id):
        if extra is None:
            continue
        try:
            allowed.add(int(extra))
        except (TypeError, ValueError):
            pass
    return allowed


def telegram_refusal(exc: Exception, *,
                     may_delete: Optional[bool]) -> str:
    """What Telegram's refusal means, in one line.

    The model must not have to guess the cause: on 2026-10-26 it
    explained "Message can't be deleted" as "the bot is not an
    administrator here", and the bot WAS an administrator with the
    delete right. The bot's own rights are known here, so the two
    refusals can be told apart instead of being guessed at.

    `may_delete` says whether the bot may delete OTHER PEOPLE's
    messages here: True when it is an administrator with the delete
    right (or owns a private chat), False when it is an administrator
    without that right, None when the rights could not be read.
    """
    raw = f"{type(exc).__name__}: {exc}"
    low = str(exc).lower()
    if "message to delete not found" in low:
        return (f"{raw} -- the message is gone already (it was deleted, or "
                f"the id belongs to another chat)")
    if "can't be deleted" in low or "cannot be deleted" in low:
        if may_delete is True:
            return (f"{raw} -- Telegram refused to delete it and did not "
                    f"say why. The bot holds the delete right in this chat, "
                    f"so the usual reason is the 48-hour window: a bot may "
                    f"only delete messages sent in the last 48 hours. "
                    f"Telegram refuses ids that are not messages of this "
                    f"chat as well, so if the message is fresh the id is "
                    f"probably not the one the user meant: reply to it "
                    f"again, so the bot learns the real id")
        if may_delete is False:
            return (f"{raw} -- the bot is not allowed to delete other "
                    f"people's messages here")
        return (f"{raw} -- the bot could not read its own rights in this "
                f"chat, so Telegram's answer stands as it is")
    return f"{raw} -- Telegram's answer, reported as it is"


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


class Rights(NamedTuple):
    """What the bot may delete in this chat.

    Two rights, not one. The bot may always take back its OWN
    messages. Other people's messages need the administrator right,
    and that is a separate fact: an administrator without the "delete
    messages" right has half the tool, not none of it.

    `others` is None when the rights could not be read at all. That is
    not the same as False: the check failed, it did not answer.
    """

    allowed: bool
    others: Optional[bool]
    reason: str


_admin_cache: dict[int, tuple[float, Rights]] = {}


async def _bot_user(bot: Any):
    """The bot's own user object, without a second API call if PTB has
    it cached (it does, after `initialize()`)."""
    try:
        return bot.bot
    except Exception:
        return await bot.get_me()


async def bot_may_delete(bot: Any, chat_id: Any,
                         chat_type: str) -> Rights:
    """What the bot may delete in this chat, and what is missing.

    In a private chat the bot owns the conversation and may delete
    messages there. In a group, a supergroup or a channel it may
    only delete other people's messages if it is an administrator with
    the right to delete them.
    """
    if chat_type in ("private", "sender"):
        return Rights(True, True, "")
    try:
        key = int(chat_id)
    except (TypeError, ValueError):
        return Rights(False, False,
                      "the chat id is not a number, so the rights are unknown")
    now = time.time()
    cached = _admin_cache.get(key)
    if cached and cached[0] > now:
        return cached[1]
    rights = await _check_admin(bot, key)
    _admin_cache[key] = (now + _ADMIN_TTL_SECONDS, rights)
    return rights


async def _check_admin(bot: Any, chat_id: int) -> Rights:
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
        return Rights(True, None,
                      (f"the bot could not read its own rights in this "
                       f"chat ({type(e).__name__}: {e}) -- Telegram will "
                       f"have the last word"))
    status = getattr(member, "status", "") or ""
    if status == "creator":
        return Rights(True, True, "")
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
            return Rights(True, True, "")
        return Rights(True, False,
                      ("the bot is an administrator here, but without the "
                       "\"delete messages\" right: it can take back its own "
                       "messages, not other people's"))
    return Rights(False, False,
                  (f"the bot is not an administrator in this chat (status: "
                   f"{status or 'unknown'}), so it cannot delete other "
                   f"people's messages here"))


# === The tool ===


def delete_messages_tool_def(*, chat_id: Any = None,
                              reply_to_message_id: Any = None,
                              current_message_id: Any = None) -> dict:
    """The definition the model is shown for `delete_messages`.

    The description carries the ids the bot actually knows: the ids of
    its own last messages in this chat, and the id of the message the
    user replied to. A model that has an id will use it; a model
    without one will invent one, and the bot is an administrator, so an
    invented id is a real attempt to delete a stranger's message.

    The WORDING matters as much as the ids, and the live test of
    2026-10-26 (14:01 MSK) is what showed it. That description already
    carried the reply-to id, and the model answered with ids 706 and
    707 -- numbers it had made up, hundreds of messages too old for
    Telegram's 48-hour window. Two things were wrong with it: the ids
    came after a paragraph of prose, and "YOUR" was ambiguous -- read
    from the user's side, "the ids of YOUR last messages" is the
    user's messages, which is exactly how the model took it. So the ids
    now come FIRST, each labelled with whose message it is, and the
    rule is stated as a prohibition, not as advice.
    """
    known = recent_ids(chat_id)
    described = (
        "Delete messages from this Telegram chat. Use it when the "
        "user asks you to take something back: \"delete your last "
        "answer\", \"remove that message\", \"clean up the channel\".\n\n"
        "WHICH MESSAGE TO DELETE -- read this first:\n"
    )
    if current_message_id is not None:
        described += (
            f"- The message the user JUST WROTE has id "
            f"{current_message_id}. That is the one they mean when they "
            f"say \"this one\", \"the message I just wrote\", \"my "
            f"message\" or \"the last thing I wrote\": delete it with "
            f"message_ids=[{current_message_id}].\n"
        )
    if reply_to_message_id is not None:
        described += (
            f"- The message the user REPLIED TO has id "
            f"{reply_to_message_id}. That is the one they mean when they "
            f"say \"the one you answered\" or \"the message above that "
            f"I answered\": delete it with "
            f"message_ids=[{reply_to_message_id}].\n"
        )
    if known:
        described += (
            "- The bot's own messages in this chat, newest first: "
            + ", ".join(str(m) for m in known)
            + ". These are the BOT's messages, not the user's. `count` "
            "takes back that many of the bot's own last answers (one "
            "answer is one reply, even when it was sent as several "
            "messages); `message_ids` deletes exactly the ids you "
            "pass.\n"
        )
    else:
        described += (
            "- The bot's own messages in this chat: the bot does not "
            "know their ids right now, so `count` cannot take them "
            "back. The ids listed above are the ones it can delete.\n"
        )
    described += (
        "\nNEVER invent a message id. Only pass an id that is listed "
        "above. The bot is an "
        "administrator here, so a made-up id is a real attempt to "
        "delete somebody else's message, and an id the bot does not "
        "know is refused without touching anything. Telegram also "
        "refuses messages older than 48 hours, and refuses other "
        "people's messages where the bot is not an administrator; when "
        "Telegram refuses, that is Telegram's answer and the bot "
        "reports it as it is -- do not explain it your own way.\n\n"
        "The bot's own record of the chat is NOT touched by a "
        "deletion: what happened stays in its history."
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
                            "only for the ids listed in this description: "
                            "the message the user just wrote, the message "
                            "the user replied to, or the bot's own "
                            "messages. An id that is not listed is "
                            "refused without touching anything. It wins "
                            "over `count`."
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
    reply_to_message_id: Any = None,
    current_message_id: Any = None,
) -> tuple[str, Optional[dict]]:
    """Delete messages from the current chat.

    Returns the text the model sees and the extra fields for the turn's
    ledger. The ledger is the record of what the tool did; it is not
    the bot's history, and nothing here writes to the database.

    An id the bot cannot vouch for is refused HERE, before Telegram is
    asked to do anything: the bot's own messages and the message the
    user replied to are the only ids in reach. The bot is an
    administrator in the chat where this tool lives, so an id the model
    made up would be a real attempt to delete a stranger's message.
    """
    args = args or {}
    count, count_problem = _parse_count(args.get("count"))
    if count_problem:
        return f"[delete_messages: {count_problem}]", None
    ids, ids_problem = _parse_ids(args.get("message_ids"))
    if ids_problem:
        return f"[delete_messages: {ids_problem}]", None

    allowed = authorized_ids(chat_id, reply_to_message_id, current_message_id)
    unknown: list[int] = []

    if ids:
        unknown = [i for i in ids if i not in allowed]
        targets = [i for i in ids if i in allowed]
        scope = "the requested message ids"
        if unknown and not targets:
            return (_unknown_ids_text(unknown, chat_id, reply_to_message_id,
                                      current_message_id, allowed), None)
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

    rights = await bot_may_delete(bot, chat_id, chat_type)
    if not rights.allowed:
        return (f"[delete_messages: refused -- {rights.reason}. Only "
                f"messages the bot may delete are in reach.]"), None

    deleted: list[int] = []
    failed: list[tuple[int, str]] = []
    for message_id in targets:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
            deleted.append(message_id)
        except Exception as e:
            why = telegram_refusal(e, may_delete=rights.others)
            failed.append((message_id, why))

    if deleted:
        forget_messages(chat_id, deleted)

    if not failed:
        text = (f"[delete_messages: deleted {len(deleted)} message(s) from "
                f"{scope} (ids {', '.join(str(m) for m in deleted)})]")
    elif not deleted:
        detail = "; ".join(f"{mid}: {why}" for mid, why in failed)
        text = (f"[delete_messages: nothing was deleted ({detail}). "
                f"Telegram's rules, not mine.]")
        if rights.reason:
            text = text[:-1] + f" {rights.reason}.]"
    else:
        detail = "; ".join(f"{mid}: {why}" for mid, why in failed)
        text = (f"[delete_messages: deleted {len(deleted)} of "
                f"{len(targets)} message(s) from {scope}; Telegram refused "
                f"the rest -- {detail}]")
        if rights.reason:
            text = text[:-1] + f" ({rights.reason}).]"

    if unknown:
        text = (text[:-1] + " Refused without touching anything: id(s) "
                + ", ".join(str(u) for u in unknown)
                + " -- the bot does not know them, and it is an "
                "administrator here, so a made-up id could delete "
                "somebody else's message.]")

    extras = {"deleted": deleted, "failed": [mid for mid, _ in failed],
              "refused": unknown}
    return text, extras


def _unknown_ids_text(unknown: list[int], chat_id: Any,
                       reply_to_message_id: Any,
                       current_message_id: Any,
                       allowed: set[int]) -> str:
    """The refusal for ids the bot cannot vouch for.

    It names the way out, because a refusal that only says "no" is a
    dead end for the model and for the user behind it: the message the
    user replied to IS in reach, and its id is right there.
    """
    own = own_ids(chat_id)
    known = ", ".join(str(m) for m in own) if own else "none"
    reply = (str(reply_to_message_id)
             if reply_to_message_id is not None else "none")
    current = (str(current_message_id)
               if current_message_id is not None else "none")
    return (
        "[delete_messages: refused -- nothing was deleted. The id(s) "
        + ", ".join(str(u) for u in unknown)
        + " are not messages the bot knows in this chat. It knows its "
        "own messages (ids " + known + "), the message the user just "
        "wrote (id " + current + ") and the message the user replied "
        "to (id " + reply + "). A made-up id would be a real attempt to "
        "delete somebody else's message -- the bot is an administrator "
        "here -- so it was not passed to Telegram. Reply to the message "
        "you mean, so the bot learns its id, or use `count` to take "
        "back the bot's own last answer.]"
    )
