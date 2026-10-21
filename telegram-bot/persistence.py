# persistence.py
# Async wrappers around the storage layer (storage.get_store()).
# Both functions are called from the bot's event loop but
# dispatch the actual SQLite work to a worker thread
# (asyncio.to_thread), so the loop stays responsive during
# DB writes.
#
# Reads return up to CONTEXT_MESSAGES messages for a thread;
# writes are append-only and never replace existing rows.

import asyncio
import json
from typing import TYPE_CHECKING

from context_budget import FETCH_LIMIT, get_budget
import storage

if TYPE_CHECKING:
    # Declared so static analysers can see that persistence reaches summarize
    # at all. The real import stays lazy, inside the function: summarisation
    # pulls in the LLM call path, and a bot running CONTEXT_STRATEGY=window
    # should not pay that at import time.
    #
    # summarize.py imports nothing local, so promoting this edge to module
    # level cannot close a cycle -- verified by test_import_graph rather than
    # by reading this comment.
    import summarize


async def persist(chat_id: int, thread_id: str, role: str, content,
                  sender_name: str | None = None):
    """Append a message to the thread's history in the DB.

    `content` is whatever the message has - a string for text,
    a list of content parts for multimodal. json.dumps it for
    storage.

    `sender_name` (optional) is the Telegram display name of the
    human who sent the message. Persisted alongside the row so
    the LLM can render "From: {name}: ..." in subsequent turns
    (and so /stats / analysis can attribute messages). Only
    meaningful for role='user'; pass None for assistant / system
    rows.
    """
    msg = {"role": role, "content": content}
    # Returns the new row's id (added 2026-10-22). Turn needs it: a
    # rollback that deletes "the last user message in this thread"
    # deletes the wrong one whenever two people share a topic.
    return await asyncio.to_thread(
        storage.get_store().add_message, chat_id, thread_id, role,
        json.dumps(msg, ensure_ascii=False), None, sender_name,
    )


def _decode(rows: list[dict]) -> list[dict]:
    """Turn raw DB rows into the dict shape call_llama expects.

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
      - "_created_at": float | None. Unix timestamp of the row, used by
        context_budget to decide what falls inside the memory window.
        Carried through as an out-of-band field so it never reaches the
        engine as a message field.
    """
    out = []
    for r in rows:
        try:
            msg = json.loads(r["content"])
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(msg, dict) or "role" not in msg or "content" not in msg:
            continue
        # Attach sender_name as an out-of-band field (not inside
        # the JSON envelope) so the LLM doesn't see it in its own
        # assistant turns. _tag_sender strips it before sending.
        if r.get("sender_name"):
            msg["_sender_name"] = r["sender_name"]
        msg["_created_at"] = r.get("created_at")
        out.append(msg)
    return out


async def load_history(chat_id: int, thread_id: str) -> list:
    """Return the messages to send for this turn, in chronological order.

    T70: this is the single place where context policy lives. Storage hands
    over everything inside the fetch ceiling; ContextBudget decides what
    actually goes to the engine. Before T70 the LIMIT was applied in SQL and
    the caller got a fixed count with no notion of age.

    Text-only and multimodal (text + image_url) messages pass through
    verbatim because both are stored as JSON in the DB and parse to the
    same shape that call_llama forwards to the engine.

    The returned dicts still carry the out-of-band `_sender_name` /
    `_created_at` fields; call_llama strips them before the HTTP request.
    """
    rows = await asyncio.to_thread(
        storage.get_store().get_messages, chat_id, thread_id, FETCH_LIMIT
    )
    budget = get_budget()
    messages = _decode(rows)
    if budget.strategy == "summarize":
        # T70.3: the summary itself was built ahead of time by summarize.py at
        # the last re-anchor. Here we only splice in whatever text it holds.
        # No model call on the request path — that is the whole point of the
        # "once per re-anchor" design.
        from summarize import get_summary
        return budget.apply_summarize(messages, get_summary(chat_id, thread_id))
    return budget.apply(messages)
