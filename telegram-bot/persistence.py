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
import functools
import json
from typing import TYPE_CHECKING

from context_budget import FETCH_LIMIT, get_budget
import storage

#: How many old, never-summarised messages the context assembler may pull
#: for the raw tail. The tail is capped by volume (context_budget's
#: SUMMARY_TAIL_TOKENS), so this is a row-count backstop: a chat of
#: one-word messages would hit the token budget in a few thousand rows, and
#: a chat of huge messages would hit it in a few hundred. Either way the
#: volume cap decides, this only keeps the query bounded.
RAW_TAIL_FETCH_LIMIT = 5000

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
                  sender_name: str | None = None,
                  telegram_message_id: int | None = None):
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

    `telegram_message_id` (optional) is the id of the Telegram
    message this row came from. It is what lets the bot find that
    message again -- by its text, by who wrote it -- and take it back
    out of the chat later, while this row stays where it is: the
    record of what happened is not the same thing as the chat.
    """
    msg = {"role": role, "content": content}
    # Returns the new row's id (added 2026-10-22). Turn needs it: a
    # rollback that deletes "the last user message in this thread"
    # deletes the wrong one whenever two people share a topic.
    return await asyncio.to_thread(
        storage.get_store().add_message, chat_id, thread_id, role,
        json.dumps(msg, ensure_ascii=False), None, sender_name,
        telegram_message_id,
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
      - "_id": int | None. The row id. T70.5 uses it to place a chunk
        summary on the timeline (at the id of the first message of the
        chunk it stands for) and to slice chunks out of the old history.
      - "_summary_id": int | None. T70.5's "СВЕДЕНО" marker: the id of
        the summary that stands for this message, or None if it was never
        summarised.
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
        if r.get("id") is not None:
            msg["_id"] = r["id"]
        if r.get("summary_id") is not None:
            msg["_summary_id"] = r["summary_id"]
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
    budget = get_budget()
    store = storage.get_store()
    if budget.strategy == "summarize":
        return await _load_summarize_context(chat_id, thread_id, budget, store)
    rows = await asyncio.to_thread(
        store.get_messages, chat_id, thread_id, FETCH_LIMIT
    )
    return budget.apply(_decode(rows))


async def _load_summarize_context(chat_id: int, thread_id: str, budget,
                                   store) -> list:
    """T70.5: assemble the context from chunk summaries + the raw tail.

    The operator's rule: "при загрузке соответсвенно грузить только готовые
    сводики и только сообщения которые не сведены". So three reads:

      1. the fresh part -- the newest messages inside the window;
      2. the summaries -- one row per summarised chunk;
      3. the raw tail -- the old messages no summary stands for.

    The summarised messages themselves are NOT loaded: their summary stands
    for them, in their place on the timeline. The messages are still in the
    database -- nothing is deleted -- but the engine gets the compressed
    form.

    No model call here. This is on the request path, and the whole point of
    the design is that summarisation happens at the re-anchor, not per
    request.
    """
    loop = asyncio.get_running_loop()
    # run_in_executor takes positional arguments only, and get_pending_old
    # takes newest_first as a keyword -- so it goes through a partial.
    raw_tail = functools.partial(
        store.get_pending_old, chat_id, thread_id, budget.anchor,
        RAW_TAIL_FETCH_LIMIT, newest_first=True,
    )
    fresh_rows, raw_rows, summaries = await asyncio.gather(
        loop.run_in_executor(
            None, store.get_messages_since, chat_id, thread_id,
            budget.anchor, budget.max_messages or FETCH_LIMIT,
        ),
        loop.run_in_executor(None, raw_tail),
        loop.run_in_executor(
            None, store.get_summaries, chat_id, thread_id,
        ),
    )
    messages = _decode(list(fresh_rows) + list(raw_rows))
    return budget.apply_summaries(messages, summaries)
