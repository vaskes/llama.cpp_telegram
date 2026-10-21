"""T70.3 — summarise out-of-window history, once per re-anchor.

Operator's design, verbatim:

> Модель пусть пересказывает та же самая, на которой бот, не надо выдумывать
> и усложнять. При старте бота надо «пересказывать» то, что не влезло в 7d
> окно, потом делать context reset и загружать туда пересказанное + 7d окно.

So: once per re-anchor, not per request. Same model as the bot. Context becomes
{system} + {summary} + {window}. The summary is reused until the next re-anchor.

Design constraints, all of which are easy to break:

1. **The LLM call does NOT live in ContextBudget.apply().** apply() is pure —
   no I/O, no network, no clock. Twelve tests depend on that. So the summary is
   built out here, ahead of time, and apply() only splices in text it was given.

2. **The trigger is reanchor(), not process start.** If it were process start,
   `soft` and `process` modes would silently differ in memory quality while
   looking identical in config. See ADR-0003.

3. **SQLite stays the source of truth.** The summary is derived and rebuildable;
   deleting it loses nothing. Losing it only means the next re-anchor is a bit
   more expensive.

4. **Any failure falls back to `window`.** A summarisation outage must degrade
   to "remembers less", never to "bot is down" and never to "silent data loss".
"""

from __future__ import annotations

import asyncio
import json
import time

import httpx

from logconf import get_logger
log = get_logger(__name__)

#: (chat_id, thread_id) -> {"text": str, "n_messages": int, "at": float}
#: In-memory on purpose. The summary is a derived artefact; persisting it would
#: create a second source of truth that could disagree with the first. Losing
#: it on restart costs one LLM call.
_SUMMARIES: dict[tuple[int, str], dict] = {}

#: Below this many old messages, summarising costs more than it saves: the
#: model call plus prompt tokens outweigh the handful of messages dropped.
MIN_MESSAGES_TO_SUMMARIZE = 8

#: Cap on how much old history goes into one summarisation call. The engine
#: has a 256k window and the prompt has to leave room for the window itself.
MAX_OLD_MESSAGES_PER_CALL = 400

SUMMARY_SYSTEM_PROMPT = (
    "Ты сжимаешь историю группового чата, чтобы продолжить разговор без "
    "потери смысла. Тебе дана старая часть переписки, которая уже не помещается "
    "в контекст.\n\n"
    "Пиши ТОЛЬКО факты: о чём говорили, какие решения приняли, что важно знать "
    "для продолжения. Выбрось болтовню, приветствия и повторы.\n\n"
    "Не обращайся к собеседнику, не задавай вопросов, не пиши вступлений. "
    "Только сжатая сводка, до 400 слов."
)


def get_summary(chat_id: int, thread_id: str) -> str | None:
    """Return the cached summary text, or None."""
    entry = _SUMMARIES.get((chat_id, thread_id))
    return entry["text"] if entry else None


def stats() -> dict:
    """For /stats and tests."""
    return {
        "threads": len(_SUMMARIES),
        "messages_summarised": sum(
            e["n_messages"] for e in _SUMMARIES.values()
        ),
        "newest": max(
            (e["at"] for e in _SUMMARIES.values()), default=0.0
        ),
    }


def clear() -> None:
    _SUMMARIES.clear()


def _decode(rows) -> list[dict]:
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
        if d.get("sender_name"):
            item["_sender_name"] = d["sender_name"]
        out.append(item)
    return out


async def _call_model(llama_url: str, model: str, old: list[dict],
                      timeout: float) -> str | None:
    """One summarisation round-trip. Returns text or None on any failure.

    Deliberately a plain chat-completions call, no tools, no tool loop: a
    summariser that can call web_search is a summariser that can go off and do
    something else instead.
    """
    lines = []
    for m in old[-MAX_OLD_MESSAGES_PER_CALL:]:
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
    if not lines:
        return None
    payload = {
        "model": model,
        "max_tokens": 1024,
        "stream": False,
        "messages": [
            {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
            {"role": "user", "content": "\n".join(lines)},
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
    """Build (or skip) the summary for one thread. Never raises."""
    try:
        rows = store.get_messages(chat_id, thread_id, 5000)
        inside, older = budget.split_for_summary(_decode(rows))
        if len(older) < MIN_MESSAGES_TO_SUMMARIZE:
            _SUMMARIES.pop((chat_id, thread_id), None)
            return {"ok": True, "skipped": True,
                    "old_messages": len(older)}
        text = await _call_model(llama_url, model, older, timeout)
        if not text:
            # Fall back to window. Losing old context is survivable; a failed
            # summarisation must never be visible as a broken bot.
            return {"ok": False, "skipped": True, "old_messages": len(older)}
        _SUMMARIES[(chat_id, thread_id)] = {
            "text": text,
            "n_messages": len(older),
            "at": time.time(),
        }
        log.info(f"[summarize] chat={chat_id} thread={thread_id}: "
            f"{len(older)} old messages -> {len(text)} chars "
            f"(window keeps {len(inside)})")
        return {"ok": True, "old_messages": len(older),
                "summary_chars": len(text), "in_window": len(inside)}
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
    total = {"threads": 0, "summarised": 0, "skipped": 0, "failed": 0}
    for chat_id, thread_id in chats:
        r = await summarise_thread(
            chat_id=chat_id, thread_id=thread_id, budget=budget,
            store=store, llama_url=llama_url, model=model, timeout=timeout,
        )
        total["threads"] += 1
        if r.get("ok") and r.get("summary_chars"):
            total["summarised"] += 1
        elif r.get("ok"):
            total["skipped"] += 1
        else:
            total["failed"] += 1
    log.info(f"[summarize] done: {total['summarised']} summarised, "
        f"{total['skipped']} skipped, {total['failed']} failed")
    return {"ok": total["failed"] == 0, **total}