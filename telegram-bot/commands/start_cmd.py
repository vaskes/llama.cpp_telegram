"""/start, /reset and /help.

TECH-DEBT #10: command handlers moved out of dispatch.py.
Everything that decides what the bot *says* in response to a command
now lives here; dispatch.py keeps the routing that finds it.

Nothing in this package imports `dispatch`. The command handlers
reached nothing from it -- `_dispatcher` was only used by
`_register_bot_menu`, which now takes a bot it is handed.
"""

import asyncio

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, \
    KeyboardButton, ReplyKeyboardMarkup, Update
from telegram.ext import ContextTypes

from access import reject_if_unauthorized, is_authorized
import storage
from handlers import _general_thread_id, _reply, _resolve_active
from chat_utils import _is_group_chat, _SUBTALK_NAME_RE
from config import ALLOWED_USER_IDS, ALLOWED_USERNAMES
from logconf import get_logger

log = get_logger(__name__)




# === start ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await reject_if_unauthorized(update, context):
        return
    # T39: show the shell tools bullet ONLY in personal profile.
    # The public profile MUST NEVER see the "available" copy - it
    # is the single bit of user-facing text that signals the bot
    # can execute shell commands. The actual enforcement is in
    # personal_tools.bash_command (profile gate) and tool_isolation
    # (whitelist); this line is just user-facing copy.
    try:
        from personal_tools import bash_command as _bc
        _shell_enabled = _bc.is_enabled_for_active_profile()
    except Exception:
        _shell_enabled = False
    if _shell_enabled:
        if _is_group_chat(update):
            # T52 #M4: group chats get a clear "unavailable" line
            # instead of the misleading "only for you" copy. The
            # real per-chat guard is in call_bash_command (T52 #H1);
            # this is just honest user-facing copy.
            shell_line = (
                chr(8226) + ' 🚫 **Shell-команды (bash) недоступны '
                'в этом чате** — песочница включается только в '
                'личке с ботом (by design, см. docs/BASH-SANDBOX.md).'
                + chr(10)
            )
        else:
            shell_line = (
                chr(8226) + ' 🐚 **Запускать shell-команды в '
                'изолированной песочнице** (bash_command — личный '
                'режим, доступен ТОЛЬКО здесь, в личке с ботом)'
                + chr(10)
            )
    else:
        shell_line = (
            chr(8226) + ' 🚫 **Shell-команды (bash) — недоступны в '
            'этом режиме**. Песочница включается только в personal '
            '(LLAMABOT_DEPLOYMENT_PROFILE=personal) и недоступна в '
            'публичных чатах. Это by design.' + chr(10)
        )
    # T58: dev mode is invisible in the user-facing chat by
    # operator design (2026-10-14 11:19 MSK). No badge, no prefix,
    # no warning in /start. The scary "!! WARNING" comment lives
    # in the code (see personal_tools/bash_command.py + limits.py),
    # not in the chat. See BASH-SANDBOX.md "Dev mode" section
    # for the operator's exact requirements.
    await _reply(update,
        chr(129302) + ' **LlamaBot v2 запущен!**' + chr(10) + chr(10) +
        'Я могу:' + chr(10) +
        chr(8226) + ' Отвечать на вопросы (с tool-calling)' + chr(10) +
        chr(8226) + ' Искать в интернете ' + chr(127760) + chr(10) +
        chr(8226) + ' Анализировать изображения (отправьте фото)' + chr(10) +
        chr(8226) + ' Расшифровывать голосовые ' + chr(127908) + chr(10) +
        chr(8226) + ' Читать документы (TXT, PDF)' + chr(10) +
        shell_line + chr(10) +
        'Команды: /reset, /stats, /help'
    )


# === reset ===
async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # 2026-10-22: slash commands are for trusted ids only -- in a group
    # as well as in private. The group branch below returned before the
    # authorization check, so anyone who could write could clear history,
    # create topics, or delete them outright (cmd_delsub calls
    # deleteForumTopic, which is not a local record).
    if await reject_if_unauthorized(update, context):
        return
    """Clear the messages in the current sub-talk / topic."""
    # === Group mode: clear history in the current topic ===
    if _is_group_chat(update):
        chat_id = update.effective_chat.id
        # Use "general" sentinel for the General topic so the DB
        # key is consistent with _route_to_thread.
        msg_thread_id = update.message.message_thread_id
        thread_id = str(msg_thread_id) if msg_thread_id is not None else _general_thread_id()
        n = await asyncio.to_thread(
            storage.get_store().delete_thread, chat_id, thread_id
        )
        # Look up the topic name for a friendlier message
        if msg_thread_id is not None:
            entry = await asyncio.to_thread(
                storage.get_store().find_known_topic_by_id, chat_id, msg_thread_id
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
    n = await asyncio.to_thread(storage.get_store().delete_thread, user_id, thread_id)
    # delete_thread removes the thread row too, so re-create it
    # (empty) and keep it active. The user can still /subs to see it.
    await asyncio.to_thread(storage.get_store().create_thread, user_id, thread_id)
    await asyncio.to_thread(storage.get_store().set_active_thread, user_id, thread_id)
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
