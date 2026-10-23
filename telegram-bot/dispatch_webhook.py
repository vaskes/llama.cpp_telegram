"""Telegram-side glue: webhook registration, Application assembly, the
command menu, and chat-member bookkeeping.

TECH-DEBT #10 block E. The ticket asked for `dispatch.py` at 400 lines;
after block D it was 733, and what remained was not "routing" at all
but five functions that are PTB surface rather than decisions:
`_webhook_auto_register_sync` speaks raw HTTP to the Bot API,
`_build_application` + `_attach_handlers` are the assembly,
`_register_bot_menu` is a startup side effect, and
`_handle_chat_member_update` is join/leave bookkeeping.

`dispatch.py` keeps what should be kept: which update goes to which
handler, and the per-user/global concurrency caps. Those are the parts
with tests that assert behaviour. Everything here is either construction
or bookkeeping -- which is the property that made it safe to move, since
none of these functions can pick a handler.

Reads the Application from `dispatch_polling`'s registry, in one
direction only. Nothing in the polling path imports from here.
"""

from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

import dispatch_polling
from commands import (
    cmd_callback,
    cmd_delsub,
    cmd_help,
    cmd_here,
    cmd_newsub,
    cmd_sub,
    cmd_subs,
    reset,
    start,
    stats,
)
from config import ALLOWED_UPDATES_LIST, BOT_TOKEN, TELEGRAM_API
from handlers import (
    _safe_sender_label,
    handle_document,
    handle_photo,
    handle_text,
    handle_voice,
)
from logconf import get_logger
from prompts import BOT_COMMANDS, WELCOME_TEXT
from reanchor import start_reanchor_scheduler, start_summarise_on_boot

log = get_logger(__name__)


def _webhook_auto_register_sync(url: str, secret_token: str) -> None:
    """Register the webhook with Telegram using synchronous HTTP calls.

    Two-step process (v0.9.4):

    1. Call `getWebhookInfo` to see the current webhook. If it
       matches the desired URL, skip setWebhook (idempotent).
       If different, print a clear WARNING before overwriting —
       the operator might have a different WEBHOOK_URL in env
       than what's actually serving production, and overwriting
       a working webhook is a destructive action.
    2. Call `setWebhook` with the desired URL, including
       `allowed_updates` (callback_query is the Stop button),
       `drop_pending_updates=False` (don't drop messages that
       arrived during the bot restart), and the secret_token.

    Both calls use a JSON body via Content-Type: application/json
    (v0.9.4 switched from form-encoded data). JSON is cleaner
    (drop_pending_updates is a real Bool, allowed_updates is a
    real list, not a stringified one) and Telegram parses
    response bodies the same way regardless of request body
    format.

    The Bot is lazily initialized inside the webhook event
    loop. If we used an async set_webhook here in a separate
    event loop, the Bot httpx client would be closed by the
    time the webhook loop started, and every bot.send_message()
    call would fail.
    """
    import urllib.request
    import json as _json
    bot_api = f"https://api.telegram.org/bot{BOT_TOKEN}"
    headers = {"Content-Type": "application/json"}

    # Step 1: check the current webhook
    current_url = None
    try:
        req = urllib.request.Request(f"{bot_api}/getWebhookInfo", method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            info = _json.loads(resp.read().decode("utf-8"))
            current_url = info.get("result", {}).get("url", "")
    except Exception as e:
        # If getWebhookInfo fails (network, transient Telegram error),
        # we proceed with setWebhook anyway. Better to risk a duplicate
        # register than to fail to start the bot.
        log.info(f"[main] getWebhookInfo failed (continuing): {type(e).__name__}: {e!r}")

    # Idempotent: same URL, nothing to do
    if current_url == url:
        log.info(f"[main] webhook already registered: {url}")
        return
    # Different URL: warn before overwriting
    if current_url:
        log.warning(f"[main] WARNING: overwriting existing webhook")
        log.info(f"[main]   current: {current_url}")
        log.info(f"[main]   new:     {url}")

    # Step 2: register
    payload = {
        "url": url,
        "secret_token": secret_token,
        "allowed_updates": ALLOWED_UPDATES_LIST,
        "drop_pending_updates": False,
    }
    data = _json.dumps(payload).encode("utf-8")
    try:
        req = urllib.request.Request(
            f"{bot_api}/setWebhook", data=data, method="POST", headers=headers,
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            body_text = resp.read().decode("utf-8", errors="replace")
            try:
                body = _json.loads(body_text)
                if body.get("ok"):
                    log.info(f"[main] webhook auto-registered: {url}")
                else:
                    err = body.get("description", "(no description)")
                    log.info(f"[main] webhook auto-register REJECTED: {err}")
            except _json.JSONDecodeError:
                log.info(f"[main] webhook auto-registered: {url} (status={resp.status}, body not JSON)")
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        log.warning(f"[main] webhook auto-register FAILED: HTTP {e.code} {body_text[:200]}")
    except Exception as e:
        log.warning(f"[main] webhook auto-register FAILED: {type(e).__name__}: {e!r}")

def _attach_handlers(app: Application) -> None:
    """Register every handler, in one place, once.

    This list existed twice -- here and in `main` -- and the two copies
    had drifted: one registered `/help` twice and put it before
    `/reset`, the other put it last. Neither copy was wrong on its own
    terms, which is what made it survive: reading one site tells you
    nothing about the other, and a command added to one of them is
    present in the source, absent from the running bot, and green in
    every test.

    Order is the PTB handler order: commands, then callbacks, then the
    media and text handlers, which must stay last so a caption arrives
    at `handle_photo` rather than `handle_text`.
    """
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

def _build_application() -> Application:
    """The Application every code path must end up with.

    `main` and `_register_bot_menu` each built one, and both had to
    remember `base_url(TELEGRAM_API)` -- W2-H2, without it the PTB Bot
    talks to api.telegram.org no matter what the env override says, so
    a smoke test against a mock server sends half its traffic to the
    real Telegram. One builder means the override cannot be forgotten.
    """
    app = Application.builder() \
           .token(BOT_TOKEN) \
           .base_url(TELEGRAM_API) \
           .build()
    _attach_handlers(app)
    return app

async def _register_bot_menu():
    # Late import: BOT_COMMANDS lives in bot.py; bot.py imports this
    # module, so we resolve at call time to break the cycle.
    """Register the bot's command menu with Telegram via setMyCommands.

    Called once at startup. Failures are non-fatal — if the network
    is briefly down at boot, the user can still type /command
    manually; the menu just won't show until next restart. Once
    set, the menu persists in Telegram's cache across restarts
    (re-registering is idempotent and cheap).
    """
    from telegram import BotCommand
    _dispatcher = dispatch_polling.get_dispatcher()
    if _dispatcher is None:
        # Build the dispatcher eagerly so we can call setMyCommands.
        # Same construction as _dispatch_update; both call sites
        # end up at the same Application instance.
        # W2-H2. Without base_url the PTB Bot talks to api.telegram.org
        # no matter what TELEGRAM_API says, so sendMessage, editMessageText,
        # getMe, setMyCommands, set_message_reaction and every file
        # download all bypass the override. The v0.10.2 env override only
        # ever covered the raw-httpx getUpdates loop. Without this line a
        # smoke test against a mock server sends half its traffic to the
        # real Telegram.
        app = _build_application()
        dispatch_polling.set_dispatcher(app)
        _dispatcher = app
        # PTB 21 requires Application.initialize() before process_update.
        # We do not run app.start() — the hand-rolled polling loop drives
        # everything — but initialize() sets up the application context.
        await _dispatcher.initialize()
        # T68: in `soft` mode the re-anchor scheduler lives in this process.
        # In the other modes this is a no-op log line. Started after
        # initialize() because it needs the running loop and a live app.
        start_reanchor_scheduler()
        start_summarise_on_boot()
    await _dispatcher.bot.set_my_commands(
        [BotCommand(c, d) for c, d in BOT_COMMANDS],
        scope=None,  # default scope covers all private chats
        language_code=None,
    )
    log.info(f"[main] setMyCommands: registered {len(BOT_COMMANDS)} commands")

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
    log.info(f"[chat_member] chat_id={chat.id} is_forum={is_forum} "
        f"user_id={user_id} username={username!r} is_bot={is_bot_user} "
        f"old_status={old_status!r} new_status={new_status!r}")
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
        log.info(f"[chat_member] skip bot user_id={user_id}")
        return
    # Build a mention. Telegram allows @username for users with a
    # public username; fall back to a first_name or just "you".
    #
    # T54 #D2: sanitise first_name before rendering. A user can
    # join with a crafted first_name containing newlines, RTL
    # overrides, or system-prompt-style text ("SYSTEM: ...")
    # which would be visible to every existing group member in
    # the welcome message. _safe_sender_label() (T53 S1) strips
    # Cc/Cf/Cs/Co categories and redacts LLM-control keywords.
    if username:
        mention = f"@{_safe_sender_label(username)}"
    elif first:
        sanitized = _safe_sender_label(first)
        mention = sanitized if sanitized else "there"
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
        log.info(f"[chat_member] posted welcome in chat_id={chat.id} "
            f"for user_id={user_id}")
    except Exception as e:
        log.warning(f"[chat_member] FAILED to post welcome in chat_id={chat.id}: "
            f"{type(e).__name__}: {e!r}")
    # 2) PM the new member. Telegram allows this because the
    # user is now in a common group with the bot. If their
    # privacy settings block PMs from non-contacts, this will
    # fail with Forbidden; we just log and continue.
    try:
        await bot.send_message(
            chat_id=user_id,
            text=WELCOME_TEXT,
        )
        log.info(f"[chat_member] PM'd welcome to user_id={user_id}")
    except Exception as e:
        log.warning(f"[chat_member] PM to user_id={user_id} FAILED: "
            f"{type(e).__name__}: {e!r}")
