# persistence.py
# Async wrappers around the storage layer (storage.store).
# Both functions are called from the bot's event loop but
# dispatch the actual SQLite work to a worker thread
# (asyncio.to_thread), so the loop stays responsive during
# DB writes.
#
# Reads return up to CONTEXT_MESSAGES messages for a thread;
# writes are append-only and never replace existing rows.

import asyncio
import json

from storage import get_store

# Use a local name to avoid the `store` global binding from bot.py
def _s():
    return get_store()
from config import CONTEXT_MESSAGES


async def persist(chat_id: int, thread_id: str, role: str, content,
                  sender_name: str | None = None):
    """Append a message to the thread's history in the DB.

    `content` is whatever the message has - a string for text,
    a list of content parts for multimodal. json.dumps it for
    storage.

    `sender_name` (optional) is the Telegram display name of the
    human who sent this message. Persisted alongside the row so
    the LLM can render "From: {name}: ..." in subsequent turns
    (and so /stats / analysis can attribute messages). Only
    meaningful for role='user'; pass None for assistant / system
    rows.
    """
    msg = {"role": role, "content": content}
    await asyncio.to_thread(
        _s().add_message, chat_id, thread_id, role,
        json.dumps(msg, ensure_ascii=False), None, sender_name,
    )


async def load_history(chat_id: int, thread_id: str) -> list:
    """Return up to CONTEXT_MESSAGES messages for the thread, in
    chronological order, as dicts that call_llama expects.

    Each returned dict has:
      - "role": "user" | "assistant" | "system"
      - "content": the raw OpenAI content (str for text, list of
        content parts for multimodal)
      - "_sender_name": str | None. The Telegram display name of
        the human who sent this user message (NULL for system /
        assistant rows, or for pre-sender-name DB rows from before
        the v3 -> v4 migration). call_llama uses this to render
        "From: {name}: <content>" so the model can tell speakers
        apart in a busy group.

    Text-only and multimodal (text + image_url) messages are returned
    verbatim because both are stored as JSON in the DB and parse
    to the same shape that call_llama forwards to llama-server.
    """
    rows = await asyncio.to_thread(
        _s().get_messages, chat_id, thread_id, CONTEXT_MESSAGES
    )
    out = []
    for r in rows:
        try:
            msg = json.loads(r["content_json"])
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(msg, dict) or "role" not in msg or "content" not in msg:
            continue
        # Attach sender_name as an out-of-band field (not inside
        # the JSON envelope) so the LLM doesn't see it in its own
        # assistant turns. _tag_sender strips it before sending.
        if r.get("sender_name"):
            msg["_sender_name"] = r["sender_name"]
        out.append(msg)
    return out
