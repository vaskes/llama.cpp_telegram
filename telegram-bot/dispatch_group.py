"""A human replying to another human in a group.

TECH-DEBT #10, closing block. `dispatch.py` was 424 lines against a
ticket that asked for 400, and this function was the last thing in it
that was not routing, lifecycle, or the lazy Application build.

It is a leaf: it reads the update, persists it, asks the model to react
silently, and returns. Nothing here decides which handler runs, so
nothing here needs to know how handlers are found.

Extraction note, since it has bitten this repo repeatedly -- a moved
function carries its statements and none of its imports. The five free
names below were measured with an AST pass over the body, not read off
the top of the old module:

    _persist_message  _load_history  call_llama  limits  log

`dispatch.py` bound the first two from `persistence`, so this module
binds them the same way and would have raised NameError on the first
group reply otherwise.
"""

import call_llama
import limits
from logconf import get_logger
from persistence import (
    persist as _persist_message,
    load_history as _load_history,
)

log = get_logger(__name__)


def _name_of(user) -> str:
    """How a person is named in the record.

    Never a bare number. On 2026-10-26 the model read the digits out of
    "[reply to id=286293081]" -- a USER id -- and handed them to the
    delete tool as a MESSAGE id. A user id is not a message id, and a
    record that carries one in the shape the other one has is a trap
    with the operator's own messages in it.
    """
    if user is None:
        return "unknown"
    username = getattr(user, "username", None)
    if username:
        return f"@{username}"
    return getattr(user, "first_name", None) or "unknown"


async def _handle_reply_to_other_user(upd, bot) -> None:
    """T65.1 — a human replied to another human in a group.

    The operator's design, verbatim from the ticket: the message must still be
    *seen*. The model reads it, thinks about it, and leaves a reaction via the
    react_to_message tool. What it must NOT do is answer, and the reasoning
    must not appear in the chat — the group is talking to itself, and a visible
    "💭 thinking…" from the bot on someone else's message is noise.

    So: real LLM call, `silent=True`. Not a skip.

    What was here before: an unconditional `return` before process_update,
    which meant the message was never written to the DB at all. That is the
    silent data loss this ticket exists to fix — the operator keeps this
    history for a LoRA dataset, and an entire class of group conversation was
    silently dropped.
    """
    msg = upd.message
    rtm = msg.reply_to_message
    rtm_from = rtm.from_user if rtm and rtm.from_user else None
    tag = _name_of(rtm_from)
    chat_id = upd.effective_chat.id
    thread_id = str(getattr(msg, "message_thread_id", None) or "main")
    who = msg.from_user
    sender = _name_of(who)
    log.info(f"[dispatch] reply-to-other-user: absorbing silently "
        f"(reply_to={tag}, chat={chat_id}, thread={thread_id}) "
        f"update_id={upd.update_id}")

    # 1. Persist. ALWAYS, and before anything that can fail — this is the part
    #    the ticket is really about.
    #
    #    1a. The message that was replied TO. It is a message of this chat as
    #        well, and until now the record carried only the reply, with the
    #        target named in a prefix. That is enough for the model to read
    #        and not enough to find: "the message where he said X" was not
    #        in the record, so the bot could not take it back out of the
    #        chat. The target is written as its own row, with its own id --
    #        and first, because it is the older of the two.
    if rtm is not None and rtm.message_id is not None:
        target_body = rtm.text or rtm.caption or ""
        if target_body:
            try:
                await _persist_message(
                    chat_id, thread_id, "user", target_body,
                    sender_name=_name_of(rtm.from_user),
                    telegram_message_id=rtm.message_id,
                )
            except Exception as exc:
                log.info(f"[dispatch] persist of the replied-to message "
                    f"failed: {exc!r}")
    #    1b. The reply itself.
    body = msg.text or msg.caption or ""
    content = f"[reply to {tag}] {body}" if tag else body
    try:
        await _persist_message(
            chat_id, thread_id, "user", content, sender_name=sender,
            telegram_message_id=msg.message_id,
        )
    except Exception as exc:
        log.info(f"[dispatch] persist failed for reply-to-other-user: {exc!r}")

    # 2. Build context exactly as a normal turn would, then ask the model to
    #    react without answering. `silent` suppresses the reasoning stream.
    try:
        history = await _load_history(chat_id, thread_id)
        silent_prompt = (
            "This message was sent by one human in the group to ANOTHER human, "
            "not to you. Do not reply and do not say anything. Look at it, then "
            "express your reaction ONLY by calling react_to_message with a "
            "single emoji. Your reasoning is private and will not be shown."
        )
        await call_llama.call_llama(
            history,
            max_tokens=limits.MAX_TOKENS_REACT_ONLY,
            user_text=silent_prompt,
            thinking_msg=None,          # nothing to update: no chat message
            use_stream=False,
            bot=bot,
            chat_id=chat_id,
            current_message_id=msg.message_id,
            chat_type=upd.effective_chat.type,
            silent=True,                # reasoning stays out of the GUI
        )
        log.info(f"[dispatch] absorbed reply-to-other-user "
            f"update_id={upd.update_id}")
    except Exception as exc:
        # A failed reaction must never lose the persisted message, and must
        # never surface as an error to the group.
        log.info(f"[dispatch] silent-absorb failed (message already persisted) "
            f"update_id={upd.update_id}: {type(exc).__name__}: {exc}")