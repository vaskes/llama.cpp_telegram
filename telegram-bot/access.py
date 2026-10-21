"""Authorization, group-policy and slot helpers. TECH-DEBT #2.

These lived in dispatch.py and were imported by handlers.py *inside function
bodies* to dodge an import cycle. With the four helpers in chat_utils.py they
are the entire reason handlers still needed a late import.

None of them touch the dispatcher: they answer a question about an update,
claim a concurrency slot, or build a keyboard. That makes them leaf code —
moving them here lets handlers.py import dispatch at module level like any
other module, which is what makes the remaining graph readable.

Kept verbatim. `is_authorized` in particular carries a security note about
Unicode normalisation that must not be "fixed" casually; read it first.
"""

import asyncio

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from chat_utils import _is_group_chat
from config import ALLOWED_USER_IDS, ALLOWED_USERNAMES, LOCKDOWN
from prompts import _LLM_TOKEN_RE
from state import _get_user_semaphore


def is_authorized(update) -> bool:
    """Return True iff the user is on the allowlist.

    Allowlist matching:
      * user.id in ALLOWED_USER_IDS (numeric Telegram user_id)
      * user.username.lower() in ALLOWED_USERNAMES (case-insensitive)

    T56 #S13 (Mavis wave-4, by-design):
    Username comparison uses .lower() without NFKC Unicode
    normalization. This is the correct fail-closed trade-off:

      * Telegram usernames per the Bot API spec are
        [A-Za-z0-9_]{5,32}, so .lower() is sufficient for the
        spec-compliant case.
      * For non-spec / legacy usernames that Unicode-normalise
        differently, the bot REJECTS the user (fail-closed)
        rather than granting access. The wrong direction for
        a security check is to "fix" a non-match by adding
        unicodedata.normalize() — that would let an attacker
        bypass the allowlist with a username that normalises
        to an allowed value.

    Do NOT "improve" this with unicodedata.normalize without
    a security review. The lack of normalization is the
    defence, not a bug.
    """
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


# --- moved verbatim from dispatch.py (TECH-DEBT #2) ---

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


async def _check_user_slot(chat_id: int, user_id: int, update):
    """Claim one of the user's concurrency slots, queuing if all
    slots are taken.

    Returns the semaphore which the caller MUST release in a
    `finally` block, OR None if the wait timed out.

    v0.7.8: the previous version rejected the message with a
    "Бот уже обрабатывает..." reply when the semaphore was
    locked. That was a fairness bug: the 3rd+ message from a
    user was dropped even though the bot was going to be free
    in a few seconds. The user had to manually re-send.

    Now: if the semaphore is locked, await sem.acquire() with
    a timeout. The handler is already running as an asyncio
    task (dispatch creates it via asyncio.create_task), so
    waiting in the task doesn't block the polling loop or
    other users' messages. After MAX_QUEUE_WAIT_SEC the wait
    gives up and we drop with a log line, so a flood of
    messages can't OOM the bot.

    Private mode: chat_id == user_id (1:1 chat), so the slot is
    effectively per-user. Group mode: keyed on (chat_id, user_id)
    so a user active in two groups has separate budgets per group.
    """
    import asyncio
    MAX_QUEUE_WAIT_SEC = 300  # 5 minutes — LLM calls are usually <60s
    sem = _get_user_semaphore(chat_id, user_id)
    if not sem.locked():
        # Fast path: free slot available
        await sem.acquire()
        return sem
    # Slow path: all slots taken, queue this message
    try:
        user_label = (update.effective_user.username or update.effective_user.id) if update.effective_user else "?"
    except Exception:
        user_label = "?"
    print(
        f"[check_user_slot] queueing message for user={user_label} chat={chat_id} "
        f"(slots all in use); will process when a slot frees (timeout {MAX_QUEUE_WAIT_SEC}s)",
        flush=True,
    )
    try:
        await asyncio.wait_for(sem.acquire(), timeout=MAX_QUEUE_WAIT_SEC)
        return sem
    except asyncio.TimeoutError:
        print(
            f"[check_user_slot] queue timeout for user={user_label} chat={chat_id} "
            f"after {MAX_QUEUE_WAIT_SEC}s; dropping message",
            flush=True,
        )
        # v0.10.2 (Qwen wave-3): tell the user the bot is overloaded
        # instead of silently dropping. The user would otherwise see
        # "💭 думаю…" and then nothing for 5 minutes.
        try:
            await _reply(update, "⚠️ Бот перегружен, попробуйте позже.")
        except Exception:
            pass
        return None


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
