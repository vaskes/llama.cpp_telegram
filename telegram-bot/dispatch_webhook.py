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

import asyncio

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
from config import (
    ALLOWED_UPDATES_LIST,
    BOT_TOKEN,
    TELEGRAM_API,
    telegram_file_base_url,
)
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

    The file base rides along for the same reason and one step further:
    PTB downloads files from `base_file_url`, which is a DIFFERENT
    constant from `base_url` and defaults to the public API. A bot
    pointed at a local Bot API server that still downloads its files
    from api.telegram.org takes the public API's 20 MB ceiling back
    at the last step -- so both bases are derived from TELEGRAM_API in
    config.py and both are passed here.
    """
    app = Application.builder() \
           .token(BOT_TOKEN) \
           .base_url(TELEGRAM_API) \
           .base_file_url(telegram_file_base_url()) \
           .build()
    _attach_handlers(app)

    async def _log_error(update, context):
        err = context.error
        log.exception(f"[handler error] {type(err).__name__}: {err!r}")

    # The error handler belongs to the assembly, not to the call site:
    # `dispatch._dispatch_update` used to attach it while it built the
    # Application, and the boot path never had one -- so a handler that
    # raised was logged in one mode and swallowed in the other.
    app.add_error_handler(_log_error)
    return app

class ApplicationNotReady(RuntimeError):
    """The bot could not reach Telegram, so it cannot answer anybody.

    A separate class, because the two failures it used to be confused
    with are not the same kind of failure:

    * the command menu did not register -- Telegram keeps the old menu
      and the user can still type the commands by hand. Non-fatal.
    * the Application never finished initializing -- the bot cannot
      read its own id, cannot send a message, cannot answer. Fatal:
      a bot that cannot answer must not sit there accepting traffic.

    On 2026-10-25 the host rebooted and came up before its DNS did. The
    bot's `initialize()` failed on `getMe`, the failure was logged as
    "setMyCommands failed (non-fatal)", and the bot went on serving
    traffic it could not answer. Telegram got a 200 for every message
    and did not retry, so the messages were lost -- and nothing brought
    the bot back except a human restarting the container.
    """


#: How hard to try to reach Telegram at boot. A host that has just
#: rebooted usually has its DNS a second or two later; a bot that gives
#: up in that window needs an operator to notice and restart it.
_INIT_ATTEMPTS = 5
_INIT_DELAY_SECONDS = 2.0


def _is_initialized(app) -> bool:
    """True when the Application can actually talk to Telegram.

    PTB 21's `Application.initialize()` awaits `bot.initialize()` FIRST
    and sets `self._initialized` LAST (verified against the installed
    PTB, and quoted in `tests/test_c3_initialize_failure.py`). So the
    flag is the honest question to ask: a True flag means `getMe`
    succeeded, which is what makes `app.bot.id` readable -- and the bot
    reads that id on every single update, to tell whether a reply is
    addressed to somebody else.

    An Application in the registry that has not finished initializing is
    a zombie: it looks ready and answers nothing. The check fails
    closed: an object without the flag is not ready.
    """
    return bool(getattr(app, "_initialized", False))


async def ensure_application_ready(
    *,
    attempts: int = _INIT_ATTEMPTS,
    delay: float = _INIT_DELAY_SECONDS,
) -> Application:
    """Return an initialized Application, or raise `ApplicationNotReady`.

    The invariant this function exists to keep: **the registry holds
    only initialized Applications**. `tests/test_c3_initialize_failure.py`
    states that property for the lazy dispatch path; this is the same
    property for the path production boots through, which is where it
    was broken.

    So: initialize first, publish second. A failed initialization leaves
    nothing in the registry, the next update builds (or repairs) and
    initializes again, and the retry is a real retry -- PTB sets
    `_initialized` last, so a raise leaves it False.

    And if Telegram cannot be reached at all, do not publish a bot that
    cannot answer: raise. The caller decides what to do with that; at
    boot it is fatal, because a bot that cannot answer must not keep
    the webhook port open looking healthy.
    """
    last_error: Exception | None = None
    app = dispatch_polling.get_dispatcher()
    built = app is None
    if built:
        app = _build_application()
    for attempt in range(1, attempts + 1):
        if _is_initialized(app):
            dispatch_polling.set_dispatcher(app)
            return app
        try:
            await app.initialize()
        except Exception as exc:  # noqa: BLE001 - the point is to retry
            last_error = exc
            log.warning(
                "[telegram] initialize failed (attempt %d/%d): %r",
                attempt, attempts, exc)
            if attempt < attempts:
                await asyncio.sleep(delay)
            continue
        dispatch_polling.set_dispatcher(app)
        if built:
            # Started after the initialization, because they need the
            # running loop and a live app. The Application is built
            # exactly once per process, so these start exactly once.
            start_reanchor_scheduler()
            start_summarise_on_boot()
        log.info("[telegram] application initialized")
        return app
    raise ApplicationNotReady(
        f"could not initialize the Telegram application in {attempts} "
        f"attempts: {last_error!r}"
    ) from last_error


async def _register_bot_menu():
    # Late import: BOT_COMMANDS lives in bot.py; bot.py imports this
    # module, so we resolve at call time to break the cycle.
    """Register the bot's command menu with Telegram via setMyCommands.

    Called once at startup. Two failures, two different weights:

    * the Application could not be initialized -- the bot cannot answer
      anybody. That raises `ApplicationNotReady`, and the caller treats
      it as fatal.
    * the menu itself did not register -- Telegram keeps the old menu
      and the user can still type `/command` by hand, so it is
      non-fatal. Once set, the menu persists in Telegram's cache across
      restarts (re-registering is idempotent and cheap).

    The Application is published to the registry only after it is
    initialized. Publishing first was the 2026-10-25 production defect:
    the half-built Application sat in the registry, every update then
    read it instead of building its own, and every update died on
    `app.bot.id` with "ExtBot is not properly initialized" -- while the
    webhook answered Telegram with 200, so the messages were dropped
    rather than retried.
    """
    from telegram import BotCommand
    _dispatcher = await ensure_application_ready()
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
