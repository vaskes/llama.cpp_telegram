"""T70.3 / T70.5 — summarise out-of-window history, in chunks, once per chunk.

Operator's design, verbatim (2026-10-21, extended 2026-10-24):

> Модель пусть пересказывает та же самая, на которой бот, не надо выдумывать
> и усложнять. При старте бота надо «пересказывать» то, что не влезло в 7d
> окно, потом делать context reset и загружать туда пересказанное + 7d окно.

and the extension that closed T70.5 (2026-10-24):

> сводки надо делать чанками, и помечать. т.е. если ОБЪЕМ данных для сводки
> > 200к надо сделав по ней сводку проставить в базе поле СВЕДЕНО, ссылку на
> сводку которая сделана и по этому чанку больше сводки не делать. Если объем
> данных для сводки <200к не проставлять поле сведено и не сводить, а грузить
> как есть в контекст. Ну и при загрузке соответсвенно грузить только готовые
> сводики и только сообщение которые не сведены.

(200k was corrected to 64k -- see context_budget.py for the arithmetic.)

So: once per re-anchor, not per request. Same model as the bot. The old part
of the history is compressed into CHUNKS, each chunk summarised on its own
and marked in the database, and the context is assembled from the finished
summaries plus the messages that were never summarised.

Design constraints, all of which are easy to break:

1. **The LLM call does NOT live in ContextBudget.apply_summaries().** That
   method is pure -- no I/O, no network, no clock. A dozen tests depend on
   that. So the summaries are built out here, ahead of time, and the
   assembler only splices in text it was given.

2. **The trigger is reanchor(), not process start.** If it were process start,
   `soft` and `process` modes would silently differ in memory quality while
   looking identical in config. See ADR-0003.

3. **SQLite stays the source of truth.** The messages are never deleted, and
   the summary is a DERIVED artefact -- but a derived artefact WITH A LINK:
   `messages.summary_id` says which summary stands for which messages, so
   the two can be checked against each other instead of merely believed.
   That link is what makes persisting the summary safe, and it is what makes
   "summarise each chunk exactly once" possible at all.

4. **Any failure falls back to the raw tail.** A summarisation outage must
   degrade to "remembers less", never to "bot is down" and never to "silent
   data loss". A chunk whose summary failed stays unmarked, so it is loaded
   raw and retried at the next re-anchor.
"""

from __future__ import annotations

import asyncio
import json
from typing import Sequence

import httpx

from context_budget import estimate_message_tokens
from logconf import get_logger
log = get_logger(__name__)

#: Below this many old messages, summarising costs more than it saves: the
#: model call plus prompt tokens outweigh the handful of messages dropped.
#: The volume trigger (context_budget.SUMMARY_TAIL_TOKENS) is the real
#: condition; this one is a cheap floor under it.
MIN_MESSAGES_TO_SUMMARIZE = 8

#: How many old messages one summarisation pass may pull from the DB. The
#: history is unbounded in principle; this is the ceiling on one pass, and it
#: keeps the OLDEST pending messages (so a pass that hits the ceiling makes
#: progress instead of stalling on the same newest chunk forever).
SUMMARY_FETCH_LIMIT = 20000

#: How many chunks one re-anchor may summarise. Each chunk is one model
#: call, and the box running the bot also serves the live bot -- a run that
#: summarises thirty chunks would keep the GPU busy for minutes. Whatever is
#: left over is summarised at the next re-anchor, which is why the order is
#: oldest-first: progress, not starvation.
MAX_CHUNKS_PER_RUN = 8

#: Backstop on the messages in one summarisation call. The chunking is done
#: by token volume; this is the row-count ceiling that catches a chat full of
#: one-word messages.
MAX_MESSAGES_PER_CALL = 2000

#: Output cap of one summarisation call. A chunk is 64k tokens of history;
#: its summary is a few hundred words.
SUMMARY_MAX_TOKENS = 1024

SUMMARY_SYSTEM_PROMPT = (
    "Ты сжимаешь историю группового чата, чтобы продолжить разговор без "
    "потери смысла. Тебе дана старая часть переписки, которая уже не помещается "
    "в контекст.\n\n"
    "Пиши ТОЛЬКО факты: о чём говорили, какие решения приняли, что важно знать "
    "для продолжения. Выбрось болтовню, приветствия и повторы.\n\n"
    "Не обращайся к собеседнику, не задавай вопросов, не пиши вступлений. "
    "Только сжатая сводка, до 400 слов."
)


def _decode(rows) -> list[dict]:
    """Turn raw DB rows into the dict shape the chunker works on.

    Carries `_id` (the row id -- the chunk boundaries and the summary
    placement are both id-based) and `_created_at`.
    """
    out = []
    for r in rows:
        d = dict(r)
        raw = d.get("content")
        try:
            content = json.loads(raw) if isinstance(raw, str) else raw
        except (json.JSONDecodeError, TypeError):
            content = raw
        if isinstance(content, dict):
            item = dict(content)
        elif isinstance(content, list):
            item = {"role": d.get("role", "user"), "content": content}
        else:
            item = {"role": d.get("role", "user"), "content": content}
        item["_created_at"] = d.get("created_at")
        item["_id"] = d.get("id")
        if d.get("telegram_message_id") is not None:
            item["_telegram_message_id"] = d["telegram_message_id"]
        if d.get("sender_name"):
            item["_sender_name"] = d["sender_name"]
        out.append(item)
    return out


def chunk_messages(messages: Sequence[dict],
                   chunk_tokens: int) -> list[list[dict]]:
    """Split old history into contiguous chunks of ~`chunk_tokens` each.

    Contiguous, because a chunk is a slice of the timeline: the summary
    stands for a stretch of history that really was consecutive, and the
    marker points at a range of message ids.

    A single message larger than the chunk budget is its own chunk -- it
    cannot be split, and refusing to summarise it would leave it pending
    forever.
    """
    chunks: list[list[dict]] = []
    current: list[dict] = []
    current_tokens = 0
    for m in messages:
        tokens = estimate_message_tokens(m)
        if current and current_tokens + tokens > chunk_tokens:
            chunks.append(current)
            current = []
            current_tokens = 0
        current.append(m)
        current_tokens += tokens
    if current:
        chunks.append(current)
    return chunks


def _render(messages: Sequence[dict]) -> str:
    """Old history as plain text for the summarisation prompt."""
    lines = []
    for m in messages[:MAX_MESSAGES_PER_CALL]:
        who = m.get("_sender_name") or m.get("role", "user")
        text = m.get("content")
        if isinstance(text, list):
            # Multimodal parts: keep the text ones, drop images (they are not
            # remembered anyway since T67.2).
            text = " ".join(
                p.get("text", "") for p in text
                if isinstance(p, dict) and p.get("type") == "text"
            )
        if not text:
            continue
        lines.append(f"{who}: {text}")
    return "\n".join(lines)


async def _call_model(llama_url: str, model: str, old: list[dict],
                      timeout: float) -> str | None:
    """One summarisation round-trip. Returns text or None on any failure.

    Deliberately a plain chat-completions call, no tools, no tool loop: a
    summariser that can call web_search is a summariser that can go off and
    do something else instead.
    """
    body = _render(old)
    if not body:
        return None
    payload = {
        "model": model,
        "max_tokens": SUMMARY_MAX_TOKENS,
        "stream": False,
        "messages": [
            {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
            {"role": "user", "content": body},
        ],
    }
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(f"{llama_url.rstrip('/')}/chat/completions",
                                  json=payload)
            r.raise_for_status()
            data = r.json()
    except Exception as exc:
        log.info(f"[summarize] model call failed: {type(exc).__name__}: {exc}")
        return None
    try:
        msg = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return None
    # A reasoning model may put everything in reasoning_content and leave
    # content empty. Take whichever is present.
    text = (msg.get("content") or "").strip()
    if not text:
        text = (msg.get("reasoning_content") or "").strip()
    return text or None


async def summarise_thread(*, chat_id: int, thread_id: str,
                           budget, store, llama_url: str, model: str,
                           timeout: float = 180.0) -> dict:
    """Summarise the pending old history of one thread, chunk by chunk.

    Never raises. The pending history is the old part of the thread that no
    summary stands for yet:

      * below the tail budget -> leave it raw. The operator's rule: "если
        объем < порога, не сводить, а грузить как есть";
      * above the tail budget -> split it into chunks, summarise each chunk
        on its own, store the summary and mark the chunk's messages so the
        same chunk is never summarised twice.

    A chunk whose model call failed stays unmarked: it is loaded raw and
    retried at the next re-anchor. Nothing is lost either way.
    """
    try:
        if budget.anchor <= 0.0:
            # No age filter: nothing is "old", so nothing to summarise.
            return {"ok": True, "skipped": True, "reason": "no age filter"}
        pending = _decode(store.get_pending_old(
            chat_id, thread_id, budget.anchor, SUMMARY_FETCH_LIMIT,
        ))
        if len(pending) < MIN_MESSAGES_TO_SUMMARIZE:
            return {"ok": True, "skipped": True, "reason": "too few messages",
                    "pending": len(pending)}
        pending_tokens = sum(estimate_message_tokens(m) for m in pending)
        if pending_tokens <= budget.summary_tail_tokens:
            # Small enough to load as is. No summary, no marker -- exactly
            # what the operator asked for in this case.
            return {"ok": True, "skipped": True,
                    "reason": "within the tail budget",
                    "pending": len(pending), "pending_tokens": pending_tokens}

        chunks = chunk_messages(pending, budget.summary_chunk_tokens)
        summarised = 0
        failed = 0
        for chunk in chunks[:MAX_CHUNKS_PER_RUN]:
            text = await _call_model(llama_url, model, chunk, timeout)
            if not text:
                # Leave the chunk unmarked: it stays raw in the context and
                # is retried at the next re-anchor.
                failed += 1
                continue
            first_id = chunk[0].get("_id")
            last_id = chunk[-1].get("_id")
            if first_id is None or last_id is None:
                failed += 1
                continue
            store.save_summary_chunk(
                chat_id, thread_id, text, int(first_id), int(last_id),
                len(chunk),
                float(chunk[0].get("_created_at") or 0.0),
                float(chunk[-1].get("_created_at") or 0.0),
            )
            summarised += 1
        log.info(f"[summarize] chat={chat_id} thread={thread_id}: "
                 f"{len(pending)} pending messages "
                 f"({pending_tokens} est. tokens) -> {summarised} chunks "
                 f"summarised, {failed} failed, {len(chunks)} chunks total")
        return {"ok": True, "pending": len(pending),
                "pending_tokens": pending_tokens,
                "chunks": len(chunks), "summarised": summarised,
                "failed": failed}
    except Exception as exc:
        log.info(f"[summarize] thread {chat_id}/{thread_id} failed: {exc!r}")
        return {"ok": False, "error": str(exc)}


async def summarise_all(*, budget, store, llama_url: str, model: str,
                        timeout: float = 180.0) -> dict:
    """Re-anchor every known thread. Called from reanchor(), once a day.

    Sequential on purpose: this runs on a laptop-grade box whose single GPU is
    also serving the live bot. Firing N concurrent summarisation calls would
    make the bot stutter for the duration.
    """
    try:
        chats = store.list_chats()
    except Exception as exc:
        log.info(f"[summarize] cannot list chats: {exc!r}")
        return {"ok": False, "error": str(exc)}
    total = {"threads": 0, "summarised": 0, "chunks": 0, "skipped": 0,
             "failed": 0}
    for chat_id, thread_id in chats:
        r = await summarise_thread(
            chat_id=chat_id, thread_id=thread_id, budget=budget,
            store=store, llama_url=llama_url, model=model, timeout=timeout,
        )
        total["threads"] += 1
        if not r.get("ok"):
            total["failed"] += 1
        elif r.get("summarised"):
            total["summarised"] += 1
            total["chunks"] += r.get("summarised", 0)
        else:
            total["skipped"] += 1
    log.info(f"[summarize] done: {total['summarised']} threads summarised "
             f"({total['chunks']} chunks), {total['skipped']} skipped, "
             f"{total['failed']} failed")
    return {"ok": total["failed"] == 0, **total}
