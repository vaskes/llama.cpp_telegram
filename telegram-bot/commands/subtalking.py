"""Sub-talk and forum-topic commands: /sub /subs /newsub /delsub /here.

TECH-DEBT #10: command handlers moved out of dispatch.py.
Everything that decides what the bot *says* in response to a command
now lives here; dispatch.py keeps the routing that finds it.

Nothing in this package imports `dispatch`. The command handlers
reached nothing from it -- `_dispatcher` was only used by
`_register_bot_menu`, which now takes a bot it is handed.
"""

import asyncio
import json

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from access import reject_if_unauthorized
import storage
from handlers import _general_thread_id, _reply, _reply_active
from chat_utils import _is_group_chat, _parse_subtalk_arg, \
    _parse_topic_arg, _SUBTALK_NAME_RE
from logconf import get_logger

log = get_logger(__name__)




# === cmd_newsub ===
async def cmd_newsub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # 2026-10-22: slash commands are for trusted ids only -- in a group
    # as well as in private. The group branch below returned before the
    # authorization check, so anyone who could write could clear history,
    # create topics, or delete them outright (cmd_delsub calls
    # deleteForumTopic, which is not a local record).
    if await reject_if_unauthorized(update, context):
        return
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
            log.info(f"[ERR newsub-group] {type(e).__name__}: {e}")
            await _reply(update, f'❌ createForumTopic failed: {e}')
            return
        # Record the topic in our local index so /subs and /delsub
        # can find it later. Telegram Bot API does NOT expose a
        # "list topics" method to bots, so the bot's local DB is
        # the only way to enumerate topics we know about.
        await asyncio.to_thread(
            storage.get_store().add_known_topic,
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
    created = await asyncio.to_thread(storage.get_store().create_thread, user_id, thread_id)
    if not created:
        # Already exists — just switch to it.
        await asyncio.to_thread(storage.get_store().set_active_thread, user_id, thread_id)
        await _reply(update,
            f'ℹ Sub-talk "{thread_id}" already existed. Now active.'
        )
        return
    await asyncio.to_thread(storage.get_store().set_active_thread, user_id, thread_id)
    await _reply(update,
        f'✅ Created sub-talk "{thread_id}" — now active.\n'
        f'Send any message to add to this thread. '
        f'Use /sub to switch, /subs to list, /delsub to remove.'
    )


# === cmd_sub ===
async def cmd_sub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # 2026-10-22: slash commands are for trusted ids only -- in a group
    # as well as in private. The group branch below returned before the
    # authorization check, so anyone who could write could clear history,
    # create topics, or delete them outright (cmd_delsub calls
    # deleteForumTopic, which is not a local record).
    if await reject_if_unauthorized(update, context):
        return
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
    existing = await asyncio.to_thread(storage.get_store().get_thread, user_id, thread_id)
    if existing is None:
        # Auto-create on /sub to a non-existent thread_id — friendlier than
        # asking the user to /newsub first.
        await asyncio.to_thread(storage.get_store().create_thread, user_id, thread_id)
    await asyncio.to_thread(storage.get_store().set_active_thread, user_id, thread_id)
    await _reply(update, f'✅ Switched to sub-talk "{thread_id}".')


# === cmd_here ===
async def cmd_here(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # 2026-10-22: slash commands are for trusted ids only -- in a group
    # as well as in private. The group branch below returned before the
    # authorization check, so anyone who could write could clear history,
    # create topics, or delete them outright (cmd_delsub calls
    # deleteForumTopic, which is not a local record).
    if await reject_if_unauthorized(update, context):
        return
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
                storage.get_store().find_known_topic_by_id, chat_id, msg_thread_id
            )
            if entry:
                topic_label = f"#{msg_thread_id} — {entry['name']}"
            else:
                topic_label = f"#{msg_thread_id} (not in known_topics)"
        else:
            topic_label = "General"
        last = await asyncio.to_thread(storage.get_store().get_last_message, chat_id, thread_id)
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
    last = await asyncio.to_thread(storage.get_store().get_last_message, user_id, current)
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
    # 2026-10-22: slash commands are for trusted ids only -- in a group
    # as well as in private. The group branch below returned before the
    # authorization check, so anyone who could write could clear history,
    # create topics, or delete them outright (cmd_delsub calls
    # deleteForumTopic, which is not a local record).
    if await reject_if_unauthorized(update, context):
        return
    # === Group mode: list known topics from local DB ===
    # Telegram Bot API does NOT expose a "list all forum topics"
    # method to bots, so we list from our local known_topics table
    # (populated by /newsub). Topics created outside the bot
    # (manually in Telegram UI, or by another bot) won't appear
    # here. The Telegram sidebar is the authoritative list.
    if _is_group_chat(update):
        topics = await asyncio.to_thread(
            storage.get_store().list_known_topics, update.effective_chat.id
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
    subs = await asyncio.to_thread(storage.get_store().list_threads, user_id)
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
    # 2026-10-22: slash commands are for trusted ids only -- in a group
    # as well as in private. The group branch below returned before the
    # authorization check, so anyone who could write could clear history,
    # create topics, or delete them outright (cmd_delsub calls
    # deleteForumTopic, which is not a local record).
    if await reject_if_unauthorized(update, context):
        return
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
                storage.get_store().find_known_topic_by_id,
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
                storage.get_store().find_known_topic_by_name,
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
            log.info(f"[ERR delsub-group-delete] {type(e).__name__}: {e}")
            await _reply(update, f'❌ deleteForumTopic failed: {e}')
            return
        # Remove from known_topics + wipe local message history.
        await asyncio.to_thread(
            storage.get_store().remove_known_topic,
            update.effective_chat.id,
            target_id,
        )
        n = await asyncio.to_thread(
            storage.get_store().delete_thread, update.effective_chat.id, str(target_id)
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
    n = await asyncio.to_thread(storage.get_store().delete_thread, user_id, thread_id)
    if n == 0:
        await _reply(update, f'❌ Sub-talk "{thread_id}" not found.')
        return
    # If we just deleted the active one, the next get_active will
    # fall back to most-recent or to 'main'.
    await _reply(update,
        f'🗑 Deleted sub-talk "{thread_id}" and {n} message(s).'
    )
