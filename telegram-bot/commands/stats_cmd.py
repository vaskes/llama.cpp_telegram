"""/stats.

TECH-DEBT #10: command handlers moved out of dispatch.py.
Everything that decides what the bot *says* in response to a command
now lives here; dispatch.py keeps the routing that finds it.

Nothing in this package imports `dispatch`. The command handlers
reached nothing from it -- `_dispatcher` was only used by
`_register_bot_menu`, which now takes a bot it is handed.
"""

import resource

from call_llama import fetch_tools_from_llama

import asyncio

from telegram import Update
from telegram.ext import ContextTypes

from access import reject_if_unauthorized
import storage
from config import ALLOWED_USER_IDS, ALLOWED_USERNAMES, MODEL
from context_budget import get_budget
from handlers import _reply, _resolve_active
from chat_utils import _is_group_chat
from logconf import get_logger

log = get_logger(__name__)



# === stats ===
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # 2026-10-22: slash commands are for trusted ids only -- in a group
    # as well as in private. The group branch below returned before the
    # authorization check, so anyone who could write could clear history,
    # create topics, or delete them outright (cmd_delsub calls
    # deleteForumTopic, which is not a local record).
    if await reject_if_unauthorized(update, context):
        return
    # T70 observability: the context policy is the one thing a user cannot
    # see from the outside. "Remembers 847 of 4231" is the question that
    # matters — and until /stats answered it, the only way to check was to
    # open SQLite by hand.
    _b = get_budget()
    policy = _b.describe()
    window_days = policy["remember_days"]

    async def _memory_lines(chat_id: int, thread_id: str, stored: int) -> str:
        """One block: what the model sees vs what the DB holds."""
        store = storage.get_store()
        anchor = _b.anchor
        # sqlite counts are blocking; run them off the event loop.
        loop = asyncio.get_running_loop()
        in_window = loop.run_in_executor(
            None, store.count_since, chat_id, thread_id, anchor
        )
        oldest = loop.run_in_executor(
            None, store.oldest_message_age, chat_id, thread_id
        )
        n_in = await in_window
        age = await oldest
        if window_days <= 0:
            window_text = "без ограничения по времени"
        else:
            window_text = f"{window_days} дн. от старта бота (якорь не скользит)"
        age_text = "—" if age is None else f"{int(age // 86400)} дн."
        old_total = max(0, stored - n_in)
        if policy["strategy"] == "summarize":
            # T70.5: the old part is no longer simply "not sent" -- part of it
            # is compressed into chunk summaries and part of it is loaded raw.
            # Saying "не отправляется" here would be a lie.
            n_summarised, n_sums = await loop.run_in_executor(
                None, store.count_summarised, chat_id, thread_id
            )
            old_line = (
                f' • Старше окна: {old_total} сообщ. '
                f'(сведено в сводки: {n_summarised} в {n_sums} сводках, '
                f'в контексте как есть: {max(0, old_total - n_summarised)})'
            )
        else:
            old_line = f' • Старше окна (не отправляется): {old_total} сообщ.'
        return (
            f' • В контексте сейчас: {n_in} сообщ.\n'
            f' • Всего в базе: {stored} сообщ.\n'
            f'{old_line}\n'
            f' • Окно памяти: {window_text}\n'
            f' • Старейшее сообщение в базе: {age_text}\n'
            f' • Стратегия: {policy["strategy"]}, потолок {policy["max_messages"]}'
        )

    # === Group mode: show topic count + total stored messages ===
    if _is_group_chat(update):
        chat_id = update.effective_chat.id
        thread_id = str(update.message.message_thread_id)
        topics = await asyncio.to_thread(
            storage.get_store().list_known_topics, chat_id
        )
        # Per-topic message count from local DB
        all_subs = await asyncio.to_thread(storage.get_store().list_threads, chat_id)
        msg_total = sum(s["msg_count"] for s in all_subs)
        cur_count = next(
            (s["msg_count"] for s in all_subs if s["thread_id"] == thread_id),
            0
        )
        memory = await _memory_lines(chat_id, thread_id, cur_count)
        await _reply(update,
            f'📊 Group stats\n'
            f' • Topics (bot-known): {len(topics)}\n'
            f' • Current topic: #{update.message.message_thread_id} '
            f'({cur_count} stored message(s))\n'
            f' • Total stored messages across topics: {msg_total}\n'
            f'\n🧠 Память (этот топик):\n{memory}'
        )
        return
    if await reject_if_unauthorized(update, context):
        return
    user_id = update.effective_user.id
    subs = await asyncio.to_thread(storage.get_store().list_threads, user_id)
    thread_id = await _resolve_active(user_id)
    current_count = next((s["msg_count"] for s in subs if s["thread_id"] == thread_id), 0)
    tools = await fetch_tools_from_llama()
    sub_lines = "\n".join(
        f'  → "{s["thread_id"]}" ({s["msg_count"]} msgs)' if s["thread_id"] == thread_id
        else f'    "{s["thread_id"]}" ({s["msg_count"]} msgs)'
        for s in subs
    ) or "    (no sub-talks yet)"
    memory = await _memory_lines(user_id, thread_id, current_count)
    await _reply(update,
        f'📊 **Статистика:**\n'
        f'Активный sub-talk: "{thread_id}" — {current_count} сообщений\n'
        f'Всего sub-talks: {len(subs)}\n'
        f'{sub_lines}\n'
        f'Модель: {MODEL}\n'
        f'Tools: {len(tools)} from llama-server + 1 weather (custom)\n'
        f'\n🧠 **Память:**\n{memory}\n'
        f'\n💡 История хранится целиком (в т.ч. для будущего обучения). '
        f'В контекст уходит только то, что попадает в окно выше.'
    )
