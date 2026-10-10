# dispatch.py
# The orchestration layer: bot lifecycle (main), polling loop,
# update dispatch, command handlers, auth, group routing,
# concurrency control. This is the wiring of the bot - all the
# message handlers and tool calls happen in other modules; this
# module routes the right update to the right handler and manages
# the per-user / global concurrency caps.

import asyncio
import json
import os
import time
from typing import Optional

import httpx
from telegram import Update, Bot
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    filters, ContextTypes,
)

import call_llama
import handlers
import persistence
import prompts
import rating
from config import (
    ALLOWED_USER_IDS, ALLOWED_USERNAMES, API_KEY, BOT_TOKEN,
    BOT_USERNAME, DB_PATH, DISABLED_TOOLS, DONSETCH_URL,
    LLAMA_URL, MAX_PHOTO_BYTES, MAX_DOC_BYTES, MAX_VOICE_BYTES,
    MAX_VIDEO_NOTE_BYTES, MODEL, SHUTDOWN_EVENT, TELEGRAM_API,
    WHISPER_URL, _TOOLS_CACHE, LLAMABOT_SELFTEST,
    _GLOBAL_LLM_SEM_LIMIT, _PER_USER_SEMAPHORE_LIMIT,
)
from prompts import (
    BLOAT_EMOJI, GROUP_CONTEXT, RATING_EMOJI, RATING_MODE,
    RATING_RULES, WELCOME_TEXT, _EMOJI_CHAR_RE, _RATING_PREFIX_RE,
)
from persistence import persist as _persist_message, load_history as _load_history
from rating import (
    _apply_rating_and_persist, _apply_reaction, _execute_react_to_message,
    _is_rating_active, _parse_rating_response,
)
from handlers import (
    handle_document, handle_photo, handle_text, handle_voice,
    send_reply, _download_with_limit, _general_thread_id, _reply,
    _reply_active, _resolve_active, _route_to_thread, _sender_display_name,
    _reject_in_group,
)

# _abort_events and _bot_replies still live in bot.py (cross-module state
# used by both handlers and the polling loop's cmd_callback). They will
# move to a state module in Stage 6.

# === is_authorized ===
def is_authorized(update: Update) -> bool:
    if LOCKDOWN:
        return False
    user = update.effective_user
    if not user:
        return False
    if user.id in ALLOWED_USER_IDS:
        return True
    if user.username and user.username.lower() in ALLOWED_USERNAMES:
        return True
    return False

# === reject_if_unauthorized ===
async def reject_if_unauthorized(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Return True if access denied (caller must stop). Silent reject: no reply to strangers."""
    if is_authorized(update):
        return False
    user = update.effective_user
    uid = user.id if user else '?'
    uname = ('@' + user.username) if user and user.username else '(no username)'
    snippet = ''
    if update.message:
        if update.message.text:
            snippet = update.message.text[:80]
        elif update.message.caption:
            snippet = f'[cap] {update.message.caption[:60]}'
    print(f'[SECURITY] rejected id={uid} {uname} msg={snippet!r}', flush=True)
    return True

# === start ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await reject_if_unauthorized(update, context):
        return
    await _reply(update, 
        '🤖 **LlamaBot v2 запущен!**\n\n'
        'Я могу:\n'
        '• Отвечать на вопросы (с tool-calling)\n'
        '• Искать в интернете 🌐\n'
        '• Узнавать погоду (wttr.in) ☀️\n'
        '• Анализировать изображения (отправьте фото)\n'
        '• Расшифровывать голосовые 🎤\n'
        '• Читать документы (TXT, PDF)\n\n'
        'Команды: /reset, /stats'
    )

# === reset ===
async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Clear the messages in the current sub-talk / topic."""
    # === Group mode: clear history in the current topic ===
    if _is_group_chat(update):
        chat_id = update.effective_chat.id
        # Use "general" sentinel for the General topic so the DB
        # key is consistent with _route_to_thread.
        msg_thread_id = update.message.message_thread_id
        thread_id = str(msg_thread_id) if msg_thread_id is not None else _general_thread_id()
        n = await asyncio.to_thread(
            store.delete_thread, chat_id, thread_id
        )
        # Look up the topic name for a friendlier message
        if msg_thread_id is not None:
            entry = await asyncio.to_thread(
                store.find_known_topic_by_id, chat_id, msg_thread_id
            )
            topic_label = f"#{msg_thread_id} — {entry['name']}" if entry else f"#{msg_thread_id}"
        else:
            topic_label = "General"
        await _reply(update,
            f'🗑 Cleared {n} message(s) in {topic_label}.\n'
            f'The Telegram topic itself is unchanged — only the '
            f"bot's memory of past messages in it is wiped.\n"
            f'In private mode the active sub-talk is re-created; in '
            f'group mode the topic is owned by Telegram and just '
            f'loses its conversation history.'
        )
        return
    if await reject_if_unauthorized(update, context):
        return
    user_id = update.effective_user.id
    thread_id = await _resolve_active(user_id)
    n = await asyncio.to_thread(store.delete_thread, user_id, thread_id)
    # delete_thread removes the thread row too, so re-create it
    # (empty) and keep it active. The user can still /subs to see it.
    await asyncio.to_thread(store.create_thread, user_id, thread_id)
    await asyncio.to_thread(store.set_active_thread, user_id, thread_id)
    await _reply(update,
        f'🔄 Cleared {n} message(s) in sub-talk "{thread_id}".\n'
        f'Sub-talk is preserved. Use /delsub to remove the whole thread.'
    )

# === cmd_help ===
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the full command reference. Same content as the bot menu
    but with examples and the sub-talk workflow explained in prose."""
    if await reject_if_unauthorized(update, context):
        return
    # Build a ReplyKeyboardMarkup with the most common actions as
    # one-tap buttons. This is a *custom keyboard* (replaces the
    # input area) — it stays in the chat until the user dismisses
    # it. Useful for first-time users; they tap to discover.
    from telegram import KeyboardButton, ReplyKeyboardMarkup
    rows = [
        [KeyboardButton("/here"), KeyboardButton("/subs")],
        [KeyboardButton("/newsub research"), KeyboardButton("/sub research")],
        [KeyboardButton("/reset"), KeyboardButton("/stats")],
    ]
    await _reply(update, 
        '🤖 **LlamaBot — help**\n\n'
        '**Sub-talks** — named conversation threads. The bot keeps a '
        'separate history for each one and only the active thread is in '
        'the model\'s context window.\n\n'
        '**Workflow:**\n'
        '1. /newsub <name>  → create a new sub-talk and switch to it\n'
        '2. Send messages — they go into the active sub-talk\n'
        '3. /sub <name>     → switch to a different sub-talk\n'
        '4. /here           → see what\'s in the current sub-talk\n'
        '5. /subs           → list all sub-talks (with tap-to-switch buttons)\n'
        '6. /delsub <name>  → remove a sub-talk and its history\n\n'
        '**Other commands:**\n'
        '• /start  — welcome + feature list\n'
        '• /reset  — clear messages in the current sub-talk (keeps the thread)\n'
        '• /stats  — model + tool count + sub-talk list\n'
        '• /help   — this message\n\n'
        'Send any photo / voice / document and it lands in the active '
        'sub-talk. Use /sub to switch.',
        reply_markup=ReplyKeyboardMarkup(
            keyboard=rows, resize_keyboard=True, one_time_keyboard=False,
        ),
    )

# === stats ===
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # === Group mode: show topic count + total stored messages ===
    if _is_group_chat(update):
        chat_id = update.effective_chat.id
        thread_id = str(update.message.message_thread_id)
        topics = await asyncio.to_thread(
            store.list_known_topics, chat_id
        )
        # Per-topic message count from local DB
        all_subs = await asyncio.to_thread(store.list_threads, chat_id)
        msg_total = sum(s["msg_count"] for s in all_subs)
        cur_count = next(
            (s["msg_count"] for s in all_subs if s["thread_id"] == thread_id),
            0
        )
        await _reply(update,
            f'📊 Group stats\n'
            f' • Topics (bot-known): {len(topics)}\n'
            f' • Current topic: #{update.message.message_thread_id} '
            f'({cur_count} stored message(s))\n'
            f' • Total stored messages across topics: {msg_total}'
        )
        return
    if await reject_if_unauthorized(update, context):
        return
    user_id = update.effective_user.id
    subs = await asyncio.to_thread(store.list_threads, user_id)
    thread_id = await _resolve_active(user_id)
    current_count = next((s["msg_count"] for s in subs if s["thread_id"] == thread_id), 0)
    tools = await fetch_tools_from_llama()
    sub_lines = "\n".join(
        f'  → "{s["thread_id"]}" ({s["msg_count"]} msgs)' if s["thread_id"] == thread_id
        else f'    "{s["thread_id"]}" ({s["msg_count"]} msgs)'
        for s in subs
    ) or "    (no sub-talks yet)"
    await _reply(update, 
        f'📊 **Статистика:**\n'
        f'Активный sub-talk: "{thread_id}" — {current_count} сообщений\n'
        f'Всего sub-talks: {len(subs)}\n'
        f'{sub_lines}\n'
        f'Модель: {MODEL}\n'
        f'Tools: {len(tools)} from llama-server + 1 weather (custom)'
    )

# === _parse_subtalk_arg ===
def _parse_subtalk_arg(text: str) -> str | None:
    """Extract a single sub-talk name from a /command arg string.

    Returns the first whitespace-separated token, or None if absent
    or invalid. For sub-talks we use the strict 32-char limit; for
    forum-topic names (group mode) see _parse_topic_arg() which
    accepts up to 128 chars.
    """
    parts = text.strip().split(None, 1)
    if len(parts) < 2:
        return None
    name = parts[1].strip()
    if not _SUBTALK_NAME_RE.match(name):
        return None
    return name

# === _parse_topic_arg ===
def _parse_topic_arg(text: str) -> str | None:
    """Like _parse_subtalk_arg but for Telegram forum topic names.

    Telegram's createForumTopic accepts topic names up to 128 chars,
    any non-empty non-whitespace string. Used in group mode by
    /newsub, /delsub.
    """
    parts = text.strip().split(None, 1)
    if len(parts) < 2:
        return None
    name = parts[1].strip()
    if not _TOPIC_NAME_RE.match(name):
        return None
    return name

# === cmd_newsub ===
async def cmd_newsub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # === Group mode: create a forum topic via createForumTopic ===
    if _is_group_chat(update):
        topic_name = _parse_topic_arg(update.message.text)
        if not topic_name:
            await _reply(update,
                '❌ Usage: /newsub <name>\n'
                'Name: 1-128 chars, no whitespace. Any UTF-8 ok.\n'
                'Example: /newsub research'
            )
            return
        try:
            topic = await context.bot.create_forum_topic(
                chat_id=update.effective_chat.id,
                name=topic_name,
            )
        except Exception as e:
            print(f"[ERR newsub-group] {type(e).__name__}: {e}", flush=True)
            await _reply(update, f'❌ createForumTopic failed: {e}')
            return
        # Record the topic in our local index so /subs and /delsub
        # can find it later. Telegram Bot API does NOT expose a
        # "list topics" method to bots, so the bot's local DB is
        # the only way to enumerate topics we know about.
        await asyncio.to_thread(
            store.add_known_topic,
            update.effective_chat.id,
            topic.message_thread_id,
            topic_name,
        )
        await _reply(update,
            f'✅ Created topic "{topic_name}" (id={topic.message_thread_id}).\n'
            f'Switch to it in the sidebar to start the conversation.'
        )
        return
    # === Private mode: sub-talk in DB ===
    thread_id = _parse_subtalk_arg(update.message.text)
    if not thread_id:
        await _reply(update,
            '❌ Usage: /newsub <name>\n'
            'Name: 1-32 chars, no whitespace. Any UTF-8 ok.\n'
            'Example: /newsub research'
        )
        return
    if await reject_if_unauthorized(update, context):
        return
    user_id = update.effective_user.id
    created = await asyncio.to_thread(store.create_thread, user_id, thread_id)
    if not created:
        # Already exists — just switch to it.
        await asyncio.to_thread(store.set_active_thread, user_id, thread_id)
        await _reply(update,
            f'ℹ Sub-talk "{thread_id}" already existed. Now active.'
        )
        return
    await asyncio.to_thread(store.set_active_thread, user_id, thread_id)
    await _reply(update,
        f'✅ Created sub-talk "{thread_id}" — now active.\n'
        f'Send any message to add to this thread. '
        f'Use /sub to switch, /subs to list, /delsub to remove.'
    )

# === cmd_sub ===
async def cmd_sub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # === Group mode: navigate-by-name is not really a thing in
    # Telegram forum topics (the user just taps the topic in the
    # sidebar). Best we can do is show the current topic. ===
    if _is_group_chat(update):
        await _reply(update,
            f'📍 You are in topic #{update.message.message_thread_id}.\n\n'
            f'In group mode, just tap a topic in the sidebar to switch —\n'
            f'there is no command to "navigate" between topics programmatically.'
        )
        return
    if await reject_if_unauthorized(update, context):
        return
    user_id = update.effective_user.id
    thread_id = _parse_subtalk_arg(update.message.text)
    if not thread_id:
        # /sub with no arg: show current
        current = await _reply_active(update, user_id)
        await _reply(update,
            f'You are in sub-talk "{current}".\n'
            f'Use /sub <thread_id> to switch, /subs to list all.'
        )
        return
    existing = await asyncio.to_thread(store.get_thread, user_id, thread_id)
    if existing is None:
        # Auto-create on /sub to a non-existent thread_id — friendlier than
        # asking the user to /newsub first.
        await asyncio.to_thread(store.create_thread, user_id, thread_id)
    await asyncio.to_thread(store.set_active_thread, user_id, thread_id)
    await _reply(update, f'✅ Switched to sub-talk "{thread_id}".')

# === cmd_here ===
async def cmd_here(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # === Group mode: show the current topic's name + last snippet ===
    if _is_group_chat(update):
        chat_id = update.effective_chat.id
        msg_thread_id = update.message.message_thread_id
        # In the General topic message_thread_id is None — use the
        # "general" sentinel so the DB key is consistent.
        thread_id = str(msg_thread_id) if msg_thread_id is not None else _general_thread_id()
        # Look up the topic name. In the General topic (or any
        # topic the bot did not create), the name is unknown —
        # show the id alone.
        if msg_thread_id is not None:
            entry = await asyncio.to_thread(
                store.find_known_topic_by_id, chat_id, msg_thread_id
            )
            if entry:
                topic_label = f"#{msg_thread_id} — {entry['name']}"
            else:
                topic_label = f"#{msg_thread_id} (not in known_topics)"
        else:
            topic_label = "General"
        last = await asyncio.to_thread(store.get_last_message, chat_id, thread_id)
        if last is None:
            snippet = '(empty)'
        else:
            try:
                content = json.loads(last["content"])
                text = content.get("text", "")
                if len(text) > 200:
                    text = text[:200] + "…"
                role_emoji = "🧑" if last["role"] == "user" else "🤖"
                snippet = f'{role_emoji} {text}' if text else '(non-text message)'
            except Exception:
                snippet = '(unreadable)'
        await _reply(update,
            f'📍 Current topic: {topic_label}\n'
            f'Last message in this topic: {snippet}'
        )
        return
    if await reject_if_unauthorized(update, context):
        return
    user_id = update.effective_user.id
    current = await _reply_active(update, user_id)
    # Also show the thread_id's last message snippet so the user
    # remembers what they were doing here.
    last = await asyncio.to_thread(store.get_last_message, user_id, current)
    if last is None:
        snippet = '(empty)'
    else:
        try:
            content = json.loads(last["content"])
            text = content.get("text", "")
            if len(text) > 200:
                text = text[:200] + "…"
            role_emoji = "🧑" if last["role"] == "user" else "🤖"
            snippet = f'{role_emoji} {text}' if text else '(non-text message)'
        except Exception:
            snippet = "(unreadable)"
    await _reply(update,
        f'📍 Current sub-talk: "{current}"\n'
        f'Last message: {snippet}\n\n'
        f'Use /subs to list, /sub <name> to switch, /newsub <name> to create.'
    )

# === cmd_subs ===
async def cmd_subs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # === Group mode: list known topics from local DB ===
    # Telegram Bot API does NOT expose a "list all forum topics"
    # method to bots, so we list from our local known_topics table
    # (populated by /newsub). Topics created outside the bot
    # (manually in Telegram UI, or by another bot) won't appear
    # here. The Telegram sidebar is the authoritative list.
    if _is_group_chat(update):
        topics = await asyncio.to_thread(
            store.list_known_topics, update.effective_chat.id
        )
        if not topics:
            await _reply(update,
                'No topics yet. Use /newsub <name> to create one.\n'
                '\n'
                'Note: topics you created via /newsub appear here.\n'
                'Topics created directly in Telegram UI are not\n'
                'enumerated by the bot (Telegram does not expose a\n'
                '"list topics" API to bots).'
            )
            return
        cur = update.message.message_thread_id
        lines = ['📚 **Topics in this group** (active marked ✅):\n']
        for t in topics:
            mark = '✅ ' if t["message_thread_id"] == cur else '  '
            lines.append(
                f'{mark}#{t["message_thread_id"]} — {t["name"]}'
            )
        await _reply(update, '\n'.join(lines))
        return
    if await reject_if_unauthorized(update, context):
        return
    user_id = update.effective_user.id
    subs = await asyncio.to_thread(store.list_threads, user_id)
    active = await _reply_active(update, user_id)
    if not subs:
        await _reply(update,
            'You have no sub-talks yet. Send any message to start '
            'the default "main" sub-talk, or /newsub <name> to create one.'
        )
        return
    # Build inline-keyboard rows. One button per sub-talk — tap to
    # switch. Active sub-talk gets a checkmark prefix. Buttons send
    # the /sub <name> command as a callback so the user does not
    # need to type it.
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    buttons = []
    for s in subs:
        label = f'{"✅ " if s["thread_id"] == active else "↔️ "}{s["thread_id"]}'
        # callback_data must be <=64 bytes; thread_id names already
        # match /^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$/ so this is safe.
        buttons.append(
            [InlineKeyboardButton(label, callback_data=f"sub:{s['thread_id']}")]
        )
    # New thread / cancel row
    # No "New sub-talk" button: the previous one triggered a
    # ForceReply hint that the user had to then type the name
    # into, but the bot would just process it as a regular
    # question. Better to just use /newsub directly. See
    # cmd_callback's "newsub:prompt" branch for the redirect
    # message.
    await _reply(update,
        f'📚 **Your sub-talks** (active marked ✅)\n\n'
        f'Tap a button to switch. Use /delsub <name> to remove one.',
        reply_markup=InlineKeyboardMarkup(buttons),
    )

# === cmd_delsub ===
async def cmd_delsub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # === Group mode: look up the topic in known_topics, then deleteForumTopic ===
    # Telegram Bot API does NOT expose a "list all topics" method to
    # bots, so we look up the topic by name (or numeric id) in our
    # local known_topics index. The index is populated by /newsub.
    if _is_group_chat(update):
        arg = _parse_topic_arg(update.message.text)
        if not arg:
            await _reply(update,
                '❌ Usage: /delsub <name>   (or /delsub <id>)\n'
                'Removes the topic AND all its messages. Cannot be undone.\n'
                'Names match the /subs list; numeric ids also work.'
            )
            return
        target_id = None
        target_name = None
        # Numeric id? Direct SQL lookup (no Python-side scan).
        if arg.isdigit():
            tid = int(arg)
            entry = await asyncio.to_thread(
                store.find_known_topic_by_id,
                update.effective_chat.id,
                tid,
            )
            if entry is None:
                await _reply(update, f'❌ No topic with id={arg} in this group.')
                return
            target_id = entry["message_thread_id"]
            target_name = entry["name"]
        else:
            entry = await asyncio.to_thread(
                store.find_known_topic_by_name,
                update.effective_chat.id,
                arg,
            )
            if entry is None:
                await _reply(update,
                    f'❌ No topic named "{arg}" in this group.\n'
                    f'(The bot only knows topics created via /newsub;\n'
                    f'topics created manually in Telegram UI are not\n'
                    f'enumerated. Use the sidebar\'s "Delete topic".)'
                )
                return
            target_id = entry["message_thread_id"]
            target_name = entry["name"]
        try:
            await context.bot.delete_forum_topic(
                chat_id=update.effective_chat.id,
                message_thread_id=target_id,
            )
        except Exception as e:
            print(f"[ERR delsub-group-delete] {type(e).__name__}: {e}", flush=True)
            await _reply(update, f'❌ deleteForumTopic failed: {e}')
            return
        # Remove from known_topics + wipe local message history.
        await asyncio.to_thread(
            store.remove_known_topic,
            update.effective_chat.id,
            target_id,
        )
        n = await asyncio.to_thread(
            store.delete_thread, update.effective_chat.id, str(target_id)
        )
        await _reply(update,
            f'🗑 Deleted topic "{target_name}" (id={target_id}) '
            f'and {n} locally-stored message(s).'
        )
        return
    # === Private mode ===
    thread_id = _parse_subtalk_arg(update.message.text)
    if not thread_id:
        await _reply(update,
            '❌ Usage: /delsub <name>\n'
            'Removes the sub-talk AND all its messages. Cannot be undone.'
        )
        return
    if await reject_if_unauthorized(update, context):
        return
    user_id = update.effective_user.id
    n = await asyncio.to_thread(store.delete_thread, user_id, thread_id)
    if n == 0:
        await _reply(update, f'❌ Sub-talk "{thread_id}" not found.')
        return
    # If we just deleted the active one, the next get_active will
    # fall back to most-recent or to 'main'.
    await _reply(update,
        f'🗑 Deleted sub-talk "{thread_id}" and {n} message(s).'
    )

# === _is_group_chat ===
def _is_group_chat(update) -> bool:
    """True if the message is in a forum-enabled supergroup (or any non-private chat).

    Telegram Bot API quirk: in a forum-enabled supergroup, messages in
    the General topic have message_thread_id == None (not set), while
    messages in regular topics have it set to the topic id. So
    "message_thread_id is not None" is NOT a reliable way to detect
    group mode. The reliable signal is `chat.is_forum` (PTB attribute
    on Chat) or, failing that, `chat.type != 'private'`.
    """
    chat = update.effective_chat
    if chat is None:
        return False
    if getattr(chat, "is_forum", False):
        return True
    return chat.type != "private"

# === _should_mute_in_group ===
def _should_mute_in_group(update) -> bool:
    """True if the bot should stay silent for this message in group mode.

    Returns True when the message is from:
      - another Telegram bot (is_bot=True; Telegram doesn't deliver
        these by default via getUpdates, but defend in depth in case
        a future API change or webhook setup changes that)
      - a human/user that self-marked with the "[llm]" convention
        (an LLM proxy or a non-Telegram LLM that joined via a
        user account)

    Filter only applies in group mode. In private mode, the bot
    is talking to one human and the [llm] marker does not have
    the "yield the floor" meaning — a private user typing [llm]
    is being playful, not opting out, and should still be answered.
    The is_bot check is still applied (defensive — a real user
    cannot have is_bot=True, so this is a no-op in practice).

    The [llm] marker is case-insensitive and word-boundary matched
    (see _LLM_TOKEN_RE). Substring matches like "[llm]s" or
    "[llm] model" do not trigger.
    """
    msg = update.message
    if msg is None or msg.from_user is None:
        return False
    if getattr(msg.from_user, "is_bot", False):
        return True
    # [llm] marker only meaningful in group mode (yield the floor
    # to other LLM participants).
    if not _is_group_chat(update):
        return False
    text = msg.text or msg.caption or ""
    if _LLM_TOKEN_RE.search(text):
        return True
    return False

# === _stop_button_markup ===
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

# === _is_reply_to_other_user ===
def _is_reply_to_other_user(update, bot_id: int) -> bool:
    """True if the message is a reply to another human's message in
    a group, and should therefore be skipped.

    Why: in a multi-user group, when user A replies to user B's
    message, the bot inserting itself into that human-to-human
    thread creates noise. The bot should respond to top-level
    messages, and to direct replies to its OWN messages, but
    not to replies directed at other humans.

    Rules:
      - No `reply_to_message` → not a reply → False.
      - Private chat → False. In a 1:1, the only "other" is
        the bot, so all replies are to the bot. The Telegram
        client also doesn't really let you reply to yourself
        in private.
      - Command (`/something`) → False, even in reply. Commands
        are explicit user actions; the user means it.
      - Reply to bot's own message (rtm.from_user.id == bot_id)
        → False. The user is talking to the bot directly.
      - Otherwise (reply to another human in a group) → True.

    Returns False in non-group chats so that the 1:1 conversation
    is never broken by this filter.
    """
    msg = update.message
    if msg is None:
        return False
    rtm = msg.reply_to_message
    if rtm is None:
        return False
    if not _is_group_chat(update):
        return False
    # Commands bypass the filter — explicit intent wins.
    text = (msg.text or msg.caption or "").lstrip()
    if text.startswith("/"):
        return False
    # Reply to the bot's own message: user is talking to the bot.
    if rtm.from_user is not None and getattr(rtm.from_user, 'id', None) == bot_id:
        return False
    return True

# === _PER_USER_SEMAPHORE_LIMIT ===
_PER_USER_SEMAPHORE_LIMIT = 2

# === _GLOBAL_LLM_SEM_LIMIT ===
_GLOBAL_LLM_SEM_LIMIT = 4

# === _get_global_llm_sem ===
def _get_global_llm_sem():
    """Return the module-level semaphore, lazy-initialised because
    asyncio primitives cannot be created at module import time
    (no running event loop)."""
    global _global_llm_sem
    if _global_llm_sem is None:
        _global_llm_sem = asyncio.Semaphore(_GLOBAL_LLM_SEM_LIMIT)
    return _global_llm_sem

# === _user_semaphore ===
def _user_semaphore(chat_id: int, user_id: int) -> asyncio.Semaphore:
    """Return the per-(chat_id, user_id) asyncio.Semaphore, creating
    it on first use. Lazy init because we cannot create asyncio
    primitives at module-import time (no running event loop)."""
    key = (chat_id, user_id)
    sem = _user_semaphores.get(key)
    if sem is None:
        sem = asyncio.Semaphore(_PER_USER_SEMAPHORE_LIMIT)
        _user_semaphores[key] = sem
    return sem

# === _check_user_slot ===
async def _check_user_slot(chat_id: int, user_id: int, update):
    """Try to claim one of the user's concurrency slots. If all
    slots are taken, sends a 'busy' reply and returns None.
    Otherwise returns the semaphore which the caller MUST release
    in a `finally` block.

    Private mode: chat_id == user_id (1:1 chat), so the slot is
    effectively per-user. Group mode: keyed on (chat_id, user_id)
    so a user active in two groups has separate budgets per group.
    """
    sem = _user_semaphore(chat_id, user_id)
    if sem.locked():
        await _reply(update,
            f"⏳ Бот уже обрабатывает {_PER_USER_SEMAPHORE_LIMIT} твоих "
            f"запросов параллельно. Подожди пока один из них завершится."
        )
        return None
    await sem.acquire()
    return sem

# === main ===
def main():
    # === Pure-httpx polling loop, no PTB Updater, no app.start() ===
    # Rationale: with `app.initialize()` or `app.start()` PTB holds a
    # second keep-alive connection open to api.telegram.org, which makes
    # Telegram see a "duplicate long-poll" and 409 every other cycle.
    # Solution: never use PTB's HTTP client. Hit the Bot API directly
    # with a single dedicated httpx.AsyncClient, de_json() into Update
    # objects, then call handler functions ourselves (not via dispatcher,
    # to skip process_update's internal queue/worker logic).
    print('🤖 LlamaBot v2 (no-PTB-poll) started...', flush=True)

    TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

    # Graceful shutdown: SIGINT (Ctrl+C) and SIGTERM (docker stop / systemd
    # stop) set this event; the polling loop checks it at the top of each
    # iteration. Without this, KeyboardInterrupt aborts the in-flight
    # httpx call mid-stream and any in-progress handler dies with an
    # unhandled exception traceback instead of returning cleanly.
    shutdown_event = asyncio.Event()

    def _on_signal(signame: str) -> None:
        print(f"[main] received {signame}, initiating shutdown...", flush=True)
        shutdown_event.set()

    async def _run():
        # Install signal handlers now that we have a running loop.
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _on_signal, sig.name)
            except (NotImplementedError, RuntimeError):
                # Windows or non-main thread: fall back to default behaviour
                # (KeyboardInterrupt will still abort, just less gracefully).
                pass
        # Publish to module global so call_llama() (called from PTB handlers
        # that don't get a context arg from our hand-rolled polling loop)
        # can read it and bail between iterations.
        global SHUTDOWN_EVENT
        SHUTDOWN_EVENT = shutdown_event

        # Register the bot's command menu with Telegram. Without this,
        # the chat input / menu shows whatever was last setMyCommands'd
        # for this bot — usually nothing useful. setMyCommands is the
        # native way to make commands discoverable in the GUI: they
        # appear in the "Menu" button on the chat input bar with their
        # descriptions, and typing / shows the list.
        try:
            await _register_bot_menu()
        except Exception as e:
            print(f"[main] setMyCommands failed (non-fatal): {e!r}", flush=True)
        # NB: we intentionally do NOT call getUpdates with offset=-1 to
        # drop pending updates. Reasoning:
        #   - in LOCKDOWN, the handler rejects everything anyway, so
        #     backlog gets eaten harmlessly;
        #   - in non-LOCKDOWN, dropping pending messages is a UX bug
        #     (user sent a message while bot was down, expects to see
        #     a reply when bot comes back).
        # The CALL_LLAMA.md §4 paragraph that claimed otherwise is
        # corrected by this comment.

        # Self-test: if LLAMABOT_SELFTEST=1, push a fake Update through
        # the dispatcher so we can verify handlers actually fire even when
        # we can't easily test with a real user.
        await _selftest()

        offset = 0
        backoff = 1.0
        n_polls = 0
        n_updates = 0
        # Use a single dedicated httpx client. max_keepalive_connections=0
        # means no keep-alive — every request opens a fresh connection.
        # This guarantees only one open connection at a time.
        # try/finally around the whole block ensures the PTB dispatcher
        # gets shut down on every exit path: normal `break` (graceful
        # shutdown), CancelledError (asyncio.run() cancel), or any
        # unexpected exception. Without the finally, the httpx client
        # held by app.bot leaks and prints "RuntimeWarning: unclosed
        # client" on interpreter shutdown.
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(connect=10.0, read=35.0, write=10.0, pool=10.0),
                limits=httpx.Limits(max_connections=1, max_keepalive_connections=0),
            ) as client:
                while True:
                    if shutdown_event.is_set():
                        print(f"[poll] shutdown_event set, exiting loop (after {n_polls} cycles, {n_updates} updates)", flush=True)
                        break
                    try:
                        r = await client.get(
                            f"{TELEGRAM_API}/getUpdates",
                            params={
                                "offset": offset,
                                "timeout": 25,            # long-poll
                                # T1 (P0-1) defense-in-depth: cap the
                                # per-cycle update batch so a flood
                                # doesn't queue 100 dispatch tasks at
                                # once. With limit=10 and
                                # _GLOBAL_LLM_SEM_LIMIT=4, the worst
                                # case is 10 in-flight at any moment,
                                # only 4 inside the LLM call.
                                "limit": 10,
                                "allowed_updates": '["message","edited_message","chat_member","callback_query"]',
                            },
                        )
                        data = r.json()
                        if not data.get("ok"):
                            raise RuntimeError(f"getUpdates not ok: {data}")
                        updates_raw = data.get("result", [])
                        n_polls += 1
                        if updates_raw:
                            n_updates += len(updates_raw)
                            backoff = 1.0
                            print(f"[poll] cycle={n_polls} got {len(updates_raw)} updates (total={n_updates})", flush=True)
                            for upd_dict in updates_raw:
                                # PII-safe dump. The previous version
                                # printed the full JSON including
                                # first_name, last_name, chat.title, and
                                # the message text — all of which are
                                # PII in journald. The new line logs
                                # only structural fields plus a short
                                # content hash for cross-correlating
                                # with later log lines.
                                upd_id = upd_dict.get("update_id", "?")
                                upd_msg = upd_dict.get("message") or upd_dict.get("edited_message") or {}
                                upd_chat = upd_msg.get("chat") or {}
                                upd_from = upd_msg.get("from") or {}
                                upd_text = (upd_msg.get("text") or upd_msg.get("caption") or "")[:60]
                                import hashlib
                                text_hash = hashlib.sha1(upd_text.encode("utf-8", errors="replace")).hexdigest()[:10] if upd_text else "-"
                                print(
                                    f"[poll] update_id={upd_id} "
                                    f"chat_type={upd_chat.get('type', '?')} "
                                    f"chat_id={upd_chat.get('id', '?')} "
                                    f"is_forum={upd_chat.get('is_forum', '?')} "
                                    f"from_id={upd_from.get('id', '?')} "
                                    f"from_is_bot={upd_from.get('is_bot', '?')} "
                                    f"thread_id={upd_msg.get('message_thread_id', '-')} "
                                    f"text_hash={text_hash}",
                                    flush=True,
                                )
                                offset = upd_dict["update_id"] + 1
                                # edited_message handling:
                                #
                                # Telegram re-delivers edited messages as
                                # `edited_message` updates (separate from
                                # the original `message`). The bot used to
                                # skip non-command edits — the original
                                # was already answered, the edit was just
                                # a typo fix. But this made the bot
                                # appear unresponsive to the corrected
                                # version: the user sees a wrong answer
                                # for the typo and no new answer for the
                                # correction.
                                #
                                # New behaviour:
                                #   1. If the new text is a command, keep
                                #      the old behaviour (commands are
                                #      idempotent, re-running is fine).
                                #   2. If the new text is regular text,
                                #      look up the bot's previous reply
                                #      to the original message in
                                #      _bot_replies. If we have one,
                                #      delete it. Then rewrite the
                                #      update as a regular `message` and
                                #      let the dispatcher process it
                                #      like a fresh user message.
                                #   3. If we have no previous reply
                                #      (e.g., the original was rejected
                                #      for security, or the bot never
                                #      responded for some reason), just
                                #      process the edit.
                                if "edited_message" in upd_dict and "message" not in upd_dict:
                                    em = upd_dict["edited_message"]
                                    em_text = (em.get("text") or "").lstrip()
                                    if em_text.startswith("/"):
                                        # Normalise: rewrite the update so
                                        # the dispatcher's handlers see a
                                        # regular `message` field. This way
                                        # every handler continues to read
                                        # `update.message.text` and friends
                                        # without per-handler branching.
                                        upd_dict["message"] = em
                                        print(
                                            f"[poll] treating edited_message as new command "
                                            f"update_id={upd_dict['update_id']} cmd={em_text[:32]!r}",
                                            flush=True,
                                        )
                                    else:
                                        # Non-command edit: delete the
                                        # bot's previous reply (if any)
                                        # and reprocess.
                                        em_chat_id = (em.get("chat") or {}).get("id")
                                        em_msg_id = em.get("message_id")
                                        if em_chat_id is not None and em_msg_id is not None:
                                            key = (em_chat_id, em_msg_id)
                                            old_bot_msg_id = _bot_replies.pop(key, None)
                                            if old_bot_msg_id is not None and _dispatcher is not None:
                                                try:
                                                    await _dispatcher.bot.delete_message(
                                                        chat_id=em_chat_id,
                                                        message_id=old_bot_msg_id,
                                                    )
                                                    print(
                                                        f"[poll] edit-replace: deleted old bot reply "
                                                        f"chat_id={em_chat_id} bot_msg_id={old_bot_msg_id} "
                                                        f"user_msg_id={em_msg_id}",
                                                        flush=True,
                                                    )
                                                except Exception as e:
                                                    # Most common failure: bot
                                                    # lacks delete permission,
                                                    # or the message is too
                                                    # old. Just log and
                                                    # continue.
                                                    print(
                                                        f"[poll] edit-replace: delete failed "
                                                        f"chat_id={em_chat_id} bot_msg_id={old_bot_msg_id}: "
                                                        f"{type(e).__name__}: {e!r}",
                                                        flush=True,
                                                    )
                                        # Now rewrite the update as a
                                        # regular `message` so the
                                        # dispatcher processes it like
                                        # a new user message. The
                                        # downstream handlers (handle_text
                                        # etc.) will see the edited text.
                                        upd_dict["message"] = em
                                        print(
                                            f"[poll] edit-replace: processing edited_message "
                                            f"update_id={upd_dict['update_id']} text_hash={em_text[:32]!r}",
                                            flush=True,
                                        )
                                # Dispatch as a TASK, not awaited. This is
                                # the critical change for the Stop button:
                                # previously, the polling loop was blocked
                                # on `await _dispatch_update(...)` for the
                                # entire duration of a handler's LLM call
                                # (3-30 seconds). Any callback_query
                                # updates (e.g., the Stop button click)
                                # would queue up and only be processed
                                # AFTER the handler finished — by which
                                # time the abort_event had been popped and
                                # the Stop click was useless.
                                #
                                # With task dispatch, the for loop returns
                                # immediately and the while loop is free to
                                # call getUpdates again. The handler runs
                                # concurrently in its own task. When the
                                # user clicks Stop, the callback reaches
                                # the polling loop, gets dispatched as
                                # another task, sets the abort_event, and
                                # the original handler's between-chunk
                                # check picks it up.
                                try:
                                    asyncio.create_task(_dispatch_update(upd_dict))
                                except Exception as e:
                                    print(f"[poll] task-create error: {type(e).__name__}: {e!r}", flush=True)
                        elif n_polls <= 5 or n_polls % 20 == 0:
                            print(f"[poll] cycle={n_polls} no updates", flush=True)
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        print(f"[poll] error: {type(e).__name__}: {e!r} (backoff {backoff}s)", flush=True)
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2, 30.0)
        finally:
            # Cleanly close the PTB Application if it was ever built.
            # Runs on every exit path: normal `break` (graceful
            # shutdown), CancelledError, or any exception. Without
            # this, the httpx client held by app.bot leaks and prints
            # "RuntimeWarning: unclosed client" on interpreter shutdown.
            if _dispatcher is not None:
                try:
                    await _dispatcher.shutdown()
                except Exception as e:
                    print(f"[main] dispatcher shutdown error: {e!r}", flush=True)

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        print('[main] KeyboardInterrupt', flush=True)

# === _register_bot_menu ===
async def _register_bot_menu():
    """Register the bot's command menu with Telegram via setMyCommands.

    Called once at startup. Failures are non-fatal — if the network
    is briefly down at boot, the user can still type /command
    manually; the menu just won't show until next restart. Once
    set, the menu persists in Telegram's cache across restarts
    (re-registering is idempotent and cheap).
    """
    from telegram import BotCommand
    global _dispatcher
    if _dispatcher is None:
        # Build the dispatcher eagerly so we can call setMyCommands.
        # Same construction as _dispatch_update; both call sites
        # end up at the same Application instance.
        app = Application.builder().token(BOT_TOKEN).build()
        app.add_handler(CommandHandler("start", start))
        app.add_handler(CommandHandler("help", cmd_help))
        app.add_handler(CommandHandler("reset", reset))
        app.add_handler(CommandHandler("stats", stats))
        app.add_handler(CommandHandler("newsub", cmd_newsub))
        app.add_handler(CommandHandler("sub", cmd_sub))
        app.add_handler(CommandHandler("subs", cmd_subs))
        app.add_handler(CommandHandler("delsub", cmd_delsub))
        app.add_handler(CommandHandler("here", cmd_here))
        app.add_handler(CommandHandler("help", cmd_help))
        app.add_handler(CallbackQueryHandler(cmd_callback))
        app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
        app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
        app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
        _dispatcher = app
        # PTB 21 requires Application.initialize() before process_update.
        # We do not run app.start() — the hand-rolled polling loop drives
        # everything — but initialize() sets up the application context.
        await _dispatcher.initialize()
    await _dispatcher.bot.set_my_commands(
        [BotCommand(c, d) for c, d in BOT_COMMANDS],
        scope=None,  # default scope covers all private chats
        language_code=None,
    )
    print(f"[main] setMyCommands: registered {len(BOT_COMMANDS)} commands", flush=True)

# === cmd_callback ===
async def cmd_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline-keyboard button presses.

    callback_data format:
      sub:<name>     — switch to sub-talk <name>  (private mode)
      newsub:prompt  — show a one-time reply keyboard asking for the new name
      delsub:<name>  — confirm-then-delete (we delete immediately; no confirm step)
      stop:thinking  — abort the in-flight LLM call for the user

    The Stop button is on every "💭 думаю…" message in both
    private and group mode. The original user (the one whose
    message is being processed) is the only one allowed to
    stop it — identified via the thinking message's
    reply_to_message.from_user.id. We compare that against
    the callback's from_user.id.
    """
    # answer() acknowledges the callback (removes the loading
    # spinner in the Telegram client). It's best-effort: if
    # the callback is too old (Telegram's timeout is short,
    # typically 30s) the call raises BadRequest. The action
    # itself proceeds regardless. Wrapped in try/except so
    # the rest of the handler runs even if answer() fails.
    try:
        await update.callback_query.answer()
    except Exception as e:
        print(f"[cmd_callback] answer() failed (non-fatal): {type(e).__name__}: {e!r}", flush=True)
    data = update.callback_query.data or ""
    # === Stop button (works in private AND group) ===
    if data == "stop:thinking":
        # Identify the original user from the thinking message.
        # The thinking message was sent as a reply to the user's
        # original message, so reply_to_message is set.
        thinking_msg = update.callback_query.message
        original = (
            thinking_msg.reply_to_message
            if thinking_msg and thinking_msg.reply_to_message
            else None
        )
        if original is None or original.from_user is None:
            print("[callback stop] no original message in reply_to_message", flush=True)
            return
        original_user_id = original.from_user.id
        clicker_id = update.effective_user.id
        if clicker_id != original_user_id:
            # Don't reveal that the request was meaningful; just
            # answer with a notice and let the clicker move on.
            print(
                f"[callback stop] rejected: clicker={clicker_id} != "
                f"original={original_user_id}",
                flush=True,
            )
            await update.callback_query.answer(
                "Только автор сообщения может остановить обработку.",
                show_alert=True,
            )
            return
        chat_id = update.effective_chat.id
        # Key on the thinking message id, NOT (chat_id, user_id).
        # Two concurrent in-flight handlers for the same (chat, user)
        # would otherwise overwrite each other in the dict: the LATER
        # handler wins the dict slot, so the Stop click on the EARLIER
        # message sets the wrong event, leaving the earlier LLM call to
        # run to completion. The user then sees a "stopped" thinking
        # message followed by a full response from the still-running LLM.
        # Using the thinking message id makes each handler own its own
        # slot, and the Stop click on a specific thinking message always
        # targets the matching handler.
        key = (chat_id, thinking_msg.message_id)
        event = _abort_events.get(key)
        if event is None:
            # No in-flight request for this user. Either the
            # handler finished between when the user clicked
            # Stop and when the callback arrived, or this is a
            # stale button (rare — buttons are tied to a
            # specific thinking message which gets deleted on
            # completion).
            print(
                f"[callback stop] no abort_event for {key} "
                f"(handler may have already finished)",
                flush=True,
            )
            try:
                await update.callback_query.edit_message_text("⏹ Уже завершено")
            except Exception:
                pass
            return
        event.set()
        print(
            f"[callback stop] abort_event set for {key}",
            flush=True,
        )
        # Edit the thinking message to "⏹ Остановлено" immediately
        # so the user sees the click took effect. The handler will
        # also try to edit it (and may delete it); the second edit
        # is a no-op if Telegram returns "not modified" or the
        # message is already gone.
        try:
            await update.callback_query.edit_message_text("⏹ Остановлено…")
        except Exception as e:
            print(f"[callback stop] edit_text failed: {type(e).__name__}: {e!r}", flush=True)
        return

    # The remaining branches (sub:, newsub:prompt, delsub:) are
    # only emitted by the private-mode /subs inline keyboard.
    # Group mode: no inline buttons exist there.
    if _is_group_chat(update):
        return
    if await reject_if_unauthorized(update, context):
        return
    user_id = update.effective_user.id
    if data.startswith("sub:"):
        thread_id = data[4:]
        existing = await asyncio.to_thread(store.get_thread, user_id, thread_id)
        if existing is None:
            await asyncio.to_thread(store.create_thread, user_id, thread_id)
        await asyncio.to_thread(store.set_active_thread, user_id, thread_id)
        await update.callback_query.edit_message_text(
            f'✅ Switched to sub-talk "{thread_id}".'
        )
    elif data == "newsub:prompt":
        # The button used to fire a ForceReply that asked the user
        # to "send the new sub-talk name" — but the next text
        # message the user typed would be routed through
        # handle_text as a regular question, not a name. The
        # button was confusing (review P2-4). Drop it; the user
        # can just type /newsub <name> directly.
        await update.effective_message.reply_text(
            'Use /newsub <name> to create a new sub-talk.'
        )
    elif data.startswith("delsub:"):
        thread_id = data[7:]
        n = await asyncio.to_thread(store.delete_thread, user_id, thread_id)
        await update.callback_query.edit_message_text(
            f'🗑 Deleted "{thread_id}" and {n} message(s).'
        )
    else:
        await update.callback_query.edit_message_text(
            f'(unknown action: {data!r})'
        )

# === _handle_chat_member_update ===
async def _handle_chat_member_update(upd, bot):
    """Welcome a new group member.

    Triggered by a Telegram chat_member update. We detect a "new
    join" as: old_chat_member.status in {left, kicked} and
    new_chat_member.status in {member, administrator, creator}.

    Actions:
      1. Post WELCOME_TEXT in the General topic of the group,
         as a reply to the service "User X joined" message.
      2. PM the new member with the same text. PMs may fail
         if the user has not started a chat with the bot or
         has strict privacy; we log and continue.
      3. Also handles my_chat_member (the bot itself being
         added/removed) — we just log it for ops visibility.

    No persistence: welcome is one-shot. The user can re-read
    the pinned message in the group.
    """
    cm = upd.chat_member
    chat = cm.chat
    new_user = cm.new_chat_member.user if cm.new_chat_member else None
    old_status = cm.old_chat_member.status if cm.old_chat_member else None
    new_status = cm.new_chat_member.status if cm.new_chat_member else None
    user_id = new_user.id if new_user else None
    username = getattr(new_user, 'username', None) if new_user else None
    first = getattr(new_user, 'first_name', None) if new_user else None
    is_bot_user = getattr(new_user, 'is_bot', False) if new_user else False
    is_forum = getattr(chat, 'is_forum', False)
    print(
        f"[chat_member] chat_id={chat.id} is_forum={is_forum} "
        f"user_id={user_id} username={username!r} is_bot={is_bot_user} "
        f"old_status={old_status!r} new_status={new_status!r}",
        flush=True,
    )
    # Only act on "new join" (left/kicked → member/admin/creator).
    # Skip leaves, promotes, demotes, bans — those are operator
    # actions, not new users.
    is_new_join = (
        old_status in ("left", "kicked")
        and new_status in ("member", "administrator", "creator")
        and user_id is not None
    )
    if not is_new_join:
        return
    # Skip other bots joining. Their operator is responsible for
    # the bot's behaviour, and PMing another bot is wasted work.
    if is_bot_user:
        print(f"[chat_member] skip bot user_id={user_id}", flush=True)
        return
    # Build a mention. Telegram allows @username for users with a
    # public username; fall back to a first_name or just "you".
    if username:
        mention = f"@{username}"
    elif first:
        mention = first
    else:
        mention = "there"
    group_msg = f"👋 {mention}, welcome!\n\n{WELCOME_TEXT}"
    # 1) Post in the group. In a forum supergroup, the General
    # topic has message_thread_id=None. We do NOT use
    # _reply() here because that helper is wired to a specific
    # message_id in a specific topic; we want a fresh message
    # in General, not a reply in some specific topic.
    try:
        await bot.send_message(
            chat_id=chat.id,
            text=group_msg,
            message_thread_id=None,  # explicit: General topic
        )
        print(
            f"[chat_member] posted welcome in chat_id={chat.id} "
            f"for user_id={user_id}",
            flush=True,
        )
    except Exception as e:
        print(
            f"[chat_member] FAILED to post welcome in chat_id={chat.id}: "
            f"{type(e).__name__}: {e!r}",
            flush=True,
        )
    # 2) PM the new member. Telegram allows this because the
    # user is now in a common group with the bot. If their
    # privacy settings block PMs from non-contacts, this will
    # fail with Forbidden; we just log and continue.
    try:
        await bot.send_message(
            chat_id=user_id,
            text=WELCOME_TEXT,
        )
        print(
            f"[chat_member] PM'd welcome to user_id={user_id}",
            flush=True,
        )
    except Exception as e:
        print(
            f"[chat_member] PM to user_id={user_id} FAILED: "
            f"{type(e).__name__}: {e!r}",
            flush=True,
        )

# === _dispatch_update ===
async def _dispatch_update(upd_dict):
    global _dispatcher
    if _dispatcher is None:
        app = Application.builder().token(BOT_TOKEN).build()
        app.add_handler(CommandHandler("start", start))
        app.add_handler(CommandHandler("reset", reset))
        app.add_handler(CommandHandler("stats", stats))
        app.add_handler(CommandHandler("newsub", cmd_newsub))
        app.add_handler(CommandHandler("sub", cmd_sub))
        app.add_handler(CommandHandler("subs", cmd_subs))
        app.add_handler(CommandHandler("delsub", cmd_delsub))
        app.add_handler(CommandHandler("here", cmd_here))
        app.add_handler(CommandHandler("help", cmd_help))
        app.add_handler(CallbackQueryHandler(cmd_callback))
        app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
        app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
        app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

        async def log_error(update, context):
            err = context.error
            print(f"[handler error] {type(err).__name__}: {err!r}", flush=True)

        app.add_error_handler(log_error)
        await app.initialize()
        _dispatcher = app
        print("[dispatch] Application initialized (lazy)", flush=True)
        # PTB 21 requires Application.initialize() before process_update.
        # We do not call app.start() — the hand-rolled polling loop drives
        # dispatch. initialize() sets up the application context.
        await _dispatcher.initialize()

    upd = Update.de_json(upd_dict, _dispatcher.bot)
    if upd is None:
        print(f"[dispatch] de_json returned None for {upd_dict}", flush=True)
        return
    # === chat_member (new join) handler ===
    # We handle chat_member updates BEFORE PTB's process_update
    # because PTB may not have a handler registered for them, and
    # we want full control of the welcome flow. The bot detects
    # new members joining the group and sends the WELCOME_TEXT
    # (a) in the General topic, and (b) as a PM to the new
    # member. This ensures the rating rules are the first
    # information the new member (especially an LLM-claimed
    # account) receives after joining.
    if getattr(upd, 'chat_member', None) is not None:
        await _handle_chat_member_update(upd, _dispatcher.bot)
        return
    txt = upd.message.text if (upd.message and upd.message.text) else None
    print(f"[dispatch] update_id={upd.update_id} msg={txt!r}", flush=True)
    # Group-mode noise filter. Apply here (in the dispatch path) so
    # EVERY handler is covered uniformly: message handlers AND
    # command handlers. If a message is from another Telegram bot
    # or contains the "[llm]" convention marker, drop it before
    # any handler runs.
    if upd.message is not None and _should_mute_in_group(upd):
        who = upd.message.from_user
        tag = (
            f"bot @{who.username}" if getattr(who, 'is_bot', False)
            else f"[llm] @{who.username}" if who.username
            else f"id={who.id}"
        )
        print(f"[dispatch] muting group-mode message from {tag}", flush=True)
        return
    # Reply-chain filter: skip messages that are replies to other
    # humans in a group. The bot should not insert itself into
    # human-to-human conversation threads. Replies to the bot's
    # own messages and commands are exempt.
    if upd.message is not None and _is_reply_to_other_user(upd, _dispatcher.bot.id):
        rtm = upd.message.reply_to_message
        rtm_from = rtm.from_user if rtm and rtm.from_user else None
        tag = (
            f"@{rtm_from.username}" if rtm_from and rtm_from.username
            else f"id={rtm_from.id}" if rtm_from
            else "?"
        )
        print(
            f"[dispatch] skipping reply-to-other-user (rtm_from={tag}) "
            f"update_id={upd.update_id}",
            flush=True,
        )
        return
    await _dispatcher.process_update(upd)
    print(f"[dispatch] processed update_id={upd.update_id}", flush=True)



# === Module-level state that other modules need ===

def get_global_llm_sem_limit() -> int:
    return _GLOBAL_LLM_SEM_LIMIT
