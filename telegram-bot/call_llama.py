# call_llama.py
# The LLM-call layer: tool definitions, donsetch MCP client,
# sender-name tagging, the big call_llama() coroutine, and
# voice transcription.
#
# This is the heart of the bot - everything that talks to
# llama-server or donsetch-http lives here. handlers.py calls
# call_llama() and persists results through persistence.persist().

import os
import asyncio
import base64
import copy
import json
import time
import urllib.parse
from typing import Any

import httpx

# KNOWN_TOOL_NAMES is used bare on the unknown-tool paths below. It was
# reached through a string import on one path and a bare name on two
# others, and only the import of its two siblings existed -- so the
# branches meant to name the known tools raised NameError instead of
# producing the message. One import, one name, three call sites.
from tool_isolation import (KNOWN_TOOL_NAMES, UnknownToolNameError,
                               validate_tool_calls)

from logconf import get_logger
log = get_logger(__name__)
# === T39: bash_command availability helper ===
# Lazy import + read of the personal_tools.bash_command module so
# we don't pay the cost (or take the token-check trip) on cold
# import if the bot never actually goes into a tool loop.


import limits

from personality import system_message as personality_system_message
from config import (
    API_KEY, DONSETCH_SESSION_ID, DONSETCH_URL, LLAMA_URL, MODEL, SHUTDOWN_EVENT,
)
# DISABLED_TOOLS went to llm_tools with fetch_tools_from_llama;
# WHISPER_LANGUAGE / WHISPER_URL went to http_clients with
# transcribe_voice; _TOOLS_CACHE went with the cache it belongs to and was
# never read from here.
from context_budget import OVERFLOW_MESSAGE, is_context_overflow

# --- extracted leaves (TECH-DEBT #3, Block B2) ---
# Re-exported, not reimplemented. dispatch.py imports
# fetch_tools_from_llama from this module, handlers.py imports
# _tag_sender and transcribe_voice, and tests call _abortable_post and
# _bash_command_tool_enabled through it. Removing these is the same trade
# as the bot.py facade: forks break, runtime gains nothing.
from prompts import GROUP_CONTEXT, RATING_RULES
from rating import _execute_react_to_message
from http_clients import _abortable_post, transcribe_voice  # noqa: F401
from llm_messages import _tag_sender  # noqa: F401
from llm_nonstream import non_stream_iteration
from llm_sse import StreamAccumulator
from llm_loop_guard import Abort, LoopGuard
from llm_tools import _bash_command_tool_enabled, fetch_tools_from_llama  # noqa: F401


# === Default model discovery ===
async def _discover_default_model(base_url: str, api_key: str) -> str:
    """Query /v1/models and return the first model id."""
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    async with httpx.AsyncClient(timeout=limits.LLAMA_MODELS_TIMEOUT_SEC) as client:
        r = await client.get(f"{base_url}/models", headers=headers)
        r.raise_for_status()
        data = r.json()
        models = data.get("data", [])
        if not models:
            raise RuntimeError(f"No models available at {base_url}/models")
        return models[0]["id"]


# --- donsetch MCP client (TECH-DEBT #3, Block B1) ---
# Moved verbatim into donsetch.py. These four names are re-exported here
# because call_llama references DONSETCH_TOOLS below and selftest.py
# reaches for donsetch_call -- and because a 200-fork bot is allowed to
# have imported them from this module since v0.9.
from donsetch import (  # noqa: F401
    DONSETCH_TOOLS,
    donsetch_call,
    execute_donsetch_web_crawl,
    execute_donsetch_web_fetch,
    execute_donsetch_web_screenshot,
    execute_donsetch_web_search,
)






async def _ensure_model() -> str:
    """Lazy MODEL discovery. Called at the top of call_llama().

    Why lazy: the v0.6.0 implementation ran this at import time,
    which failed the whole bot if llama-server was unreachable
    at boot. The v0.6.3 fix defers it to first LLM call, so
    the bot starts cleanly and the user sees a clear error
    when their first message tries to reach the model.

    If MODEL is set in env (e.g. via docker-compose), this is a
    no-op - we just return it.
    """
    global MODEL
    if not MODEL:
        MODEL = await _discover_default_model(LLAMA_URL, API_KEY)
        log.info(f"[config] MODEL auto-discovered: {MODEL}")
    return MODEL






async def call_llama(
    messages,
    max_tokens=limits.MAX_TOKENS_CALL_LLAMA_DEFAULT,
    user_text="",
    thinking_msg=None,
    use_stream=True,
    shutdown_event=None,
    rating_active=False,
    abort_event=None,
    bot=None,
    chat_id=None,
    current_message_id=None,
    *,
    chat_type: str,
    silent: bool = False,
):
    """Call llama.cpp with a tool-calling loop and live reasoning stream.

    thinking_msg: optional Telegram Message to update with reasoning text as it streams
                  (throttled internally). Pass None to skip the live reasoning feed.
    use_stream:    if False, do a plain non-streaming POST (better for vision tasks
                  and other cases where streaming may truncate reasoning).
    chat_type: required. The Telegram chat type ("private", "group",
             "supergroup", "channel"). Required - no default - so
             a new caller cannot accidentally enable bash_command
             in a context that should be locked down. See T52 #H1
             and T53 #R4 for the threat model.
    silent:      T65.1. The message was addressed to another human in a group.
                  The LLM still runs and still reasons -- that is how it decides
                  what reaction to leave -- but reasoning is NOT streamed to the
                  chat and the text answer is NOT sent. The only outward signal
                  is whatever the model does via tools (react_to_message).
                  Nothing about the engine changes; this is presentation only.
shutdown_event: optional asyncio.Event; checked at the top of every iteration
                  so SIGTERM (docker stop) can abort a long call before the 10s
                  SIGKILL grace runs out. Falls back to the module-level
                  SHUTDOWN_EVENT if None.
    Returns the final assistant content (or fallback message if exhausted).

    Implementation note: this function is intentionally long (the streaming
    branch alone is ~400 lines, cyclomatic ~100). The shape is the
    convergence of: streaming SSE parser, four independent abort guards
    (max_iter, identical-call, wall-clock, hallucination), tool-execution
    loop, and reasoning-stream push to Telegram. Splitting it prematurely
    loses the abort-ladder invariants. DO NOT REFACTOR without reading
    docs/CALL_LLAMA.md end-to-end and writing a regression test against
    a captured llama-server response. See also docs/archive/v0.0-v0.4/REVIEW-MINIMAX.md §P3-4 (superseded review; the v0.7+ refactors made most of its P3 findings obsolete — read for historical context only).
    """

    # T53 #R3: chat_type is required. A new caller that forgets
    # the kwarg would otherwise silently enable bash_command in
    # a context that should be locked down. The sentinel default
    # is None and we raise immediately. The T52 commit wired
    # chat_type through the 4 handlers but forgot to add it to
    # this signature, which broke the bot in prod.
    if chat_type is None:
        raise TypeError(
            "call_llama() missing required argument: 'chat_type'. "
            "Pass update.effective_chat.type from the handler. "
            "See T53 #R3 and T52 #H1."
        )


    await _ensure_model()  # lazy first-call discovery
    from dispatch import _stop_button_markup
    tools = await fetch_tools_from_llama()
    # custom_weather_tool is now in bot_side_tool_defs below; see "Add our
    # bot-side tool implementations" comment. Keeping the legacy variable
    # name as alias so other call sites still work.
    custom_weather_tool = None  # placeholder, see bot_side_tool_defs
    # Add our bot-side tool implementations (donsetch_web_*).
    # donsetch_web_* talk directly to
    # donsetch-http at 127.0.0.1:8765/mcp. We do NOT rely on llama.cpp's
    # own MCP integration — that path has been broken (playwright works
    # but donsetch doesn't load, see llama.cpp #13845 or similar). Adding
    # the tools here means the model can call them via tool_calls and we
    # dispatch in this Python process.
    bot_side_tool_defs = [
        {
            'type': 'function',
            'function': {
                'name': 'donsetch_web_search',
                'description': 'Web search: aggregated results from 10+ keyless engines (DuckDuckGo, Brave, Startpage, etc.), reranked. Use when the user asks about news, current events, prices, anything requiring fresh data. Returns titles + snippets + URLs. Follow up with donsetch_web_fetch to read a specific URL.',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'query': {'type': 'string', 'description': 'Search query, e.g. "bitcoin price today"'},
                        'max_results': {'type': 'integer', 'description': 'Max results to return (default 7, max 12). Use only when default is insufficient.'},
                    },
                    'required': ['query']
                }
            }
        },
        {
            'type': 'function',
            'function': {
                'name': 'donsetch_web_fetch',
                'description': 'Read one URL as clean markdown. Use after web_search to read a specific page. Returns title, body text, and source URL.',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'url': {'type': 'string', 'description': 'http(s) URL to read'},
                        'max_chars': {'type': 'integer', 'description': 'Max markdown chars (default 16000). Lower for previews.'},
                    },
                    'required': ['url']
                }
            }
        },
        {
            'type': 'function',
            'function': {
                'name': 'donsetch_web_crawl',
                'description': 'Read multiple pages from one site. Use when you need breadth (e.g. all docs, all products). Slower than fetch.',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'url': {'type': 'string', 'description': 'Seed URL to crawl from'},
                        'max_pages': {'type': 'integer', 'description': 'Max pages (default 10, cap 200)'},
                    },
                    'required': ['url']
                }
            }
        },
        {
            'type': 'function',
            'function': {
                'name': 'donsetch_web_screenshot',
                'description': 'Capture a URL as a rendered PNG (headless browser). Use when you need to see a page visually.',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'url': {'type': 'string', 'description': 'http(s) URL to capture'},
                    },
                    'required': ['url']
                }
            }
        },
        {
            'type': 'function',
            'function': {
                'name': 'react_to_message',
                'description': (
                    'Set a single-emoji Telegram reaction on a message in the current chat. '
                    'Use this when the user explicitly asks for a reaction ("поставь 👍 на моё сообщение", '
                    '"react to his question with 🤔"), or when you want to acknowledge a message non-verbally. '
                    'Standard Telegram reaction set (use these — other emojis may be rejected by the API): '
                    '👍 👏 ❤️ 🔥 (positive), 😐 🤔 (neutral), 😢 😡 🤮 💩 (negative). '
                    'If message_id is omitted, the bot reacts to the most recent human message in the active thread '
                    '(the one that triggered this turn), which is what you usually want. '
                    'Not available in rating mode (rating mode uses a hardcoded reaction path).'
                ),
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'emoji': {
                            'type': 'string',
                            'description': 'A single emoji. Standard set: 👍 👏 ❤️ 🔥 😐 🤔 😢 😡 🤮 💩.',
                        },
                        'message_id': {
                            'type': 'integer',
                            'description': 'Optional. Telegram message_id of the target message in the current chat. If omitted, reacts to the most recent human message in this thread (the default you usually want).',
                        },
                    },
                    'required': ['emoji']
                }
            }
        },
    ]
    # T39: append the bash_command tool def ONLY when personal profile
    # is active AND the sandbox side-car token is configured. In any
    # other case (public, or personal without token) the LLM never
    # sees the tool at all, so it cannot reach for it.
    if _bash_command_tool_enabled():
        # T57 #D7 (Mavis wave-5): the bash_command tool def
        # is now imported from personal_tools at dispatch time
        # (T52 #M5 was a "claimed" dedup that left two divergent
        # dicts; the actual import happens lazily here). The
        # dispatch function in call_llama never constructs the
        # def directly; the single source of truth is in
        # personal_tools/bash_command.py.
        from personal_tools.bash_command import BASH_COMMAND_TOOL_DEF
        bot_side_tool_defs.append(BASH_COMMAND_TOOL_DEF)
    all_tools = tools + bot_side_tool_defs

    # === Group context (always-injected system message) ===
    # The LlmChatPlace rules go into a system message that appears
    # in EVERY call_llama call - both rating and tool modes. The
    # pinned welcome message in the chat is for humans; the LLM
    # does not see it on its own, and after a model switch or
    # /reset it has no memory of the rules. Putting them here
    # means they survive every cold start. See also the
    # `_group_ctx_msg` references further down for the injection
    # points.
    _group_ctx_msg = {"role": "system", "content": GROUP_CONTEXT}
    # T69: the persona goes FIRST, before GROUP_CONTEXT. The group rules are
    # shared by every fork; the persona is one operator's. Putting the persona
    # in the authoritative first slot means "be Гадя" outranks the generic
    # assistant framing, and if a fork edits GROUP_CONTEXT they cannot demote
    # someone else's persona by accident.
    _persona_msg = personality_system_message()

    # Inject the group-context system message so the LLM has the
    # LlmChatPlace rules in every turn (not just rating mode). The
    # tool-mode branch below may add another system message for
    # the assistant-prompt; rating mode handles its own ordering.
    messages = [_group_ctx_msg] + messages
    # === Detect whether this turn actually needs tools ===
    # Reason: with tools always present, Ornith on a 2nd+ turn tends to
    # hallucinate a tool_call even for math/text tasks (we verified via
    # direct curl: 1 turn = 17s finish_reason=stop, no tool_calls; 3 turns
    # with tools = 30s+ finish_reason=tool_calls, 30 tool_call chunks, then
    # loop until max_iter). open-webui works for the same questions because
    # it doesn't pass tools by default.
    #
    # Heuristic: if the user's current message mentions web/weather/news/
    # Tool availability policy: tools are passed to the model in
    # every call EXCEPT when rating_active=True. In rating mode
    # the LLM is a classifier and must output a structured
    # `[[TYPE:...]]` response; tool calls would be inappropriate
    # noise. See the comment above _TOOL_KEYWORDS for the full
    # rationale on why we dropped the keyword gate.
    use_tools = not rating_active
    # If the caller asked for rating mode (group mode + RATING_MODE=1),
    # inject the rating rules as a system message BEFORE the existing
    # GROUP_CONTEXT message. The LLM sees its first system message;
    # both are honored (RATING_RULES first, GROUP_CONTEXT second).
    if rating_active:
        # Add the rating-mode-specific RATING_RULES on top of the
        # already-injected GROUP_CONTEXT. ORDER matters: the LLM
        # treats the first system message as the most authoritative,
        # so RATING_RULES goes FIRST (the structured output format
        # is the rating-mode contract) and GROUP_CONTEXT (the
        # general LlmChatPlace rules) comes second.
        messages = [{"role": "system", "content": RATING_RULES}] + messages
    if use_tools:
        # T2 (P0-2): unified persona in the most-authoritative slot.
        # Previously the order was [helpful_assistant_sys,
        # GROUP_CONTEXT, ...rest], which made Qwen treat the generic
        # assistant persona as the highest-priority system message
        # and demote the LlmChatPlace rules (including the [llm]
        # tag). Now the first system message leads with the
        # LlmChatPlace persona (GROUP_CONTEXT) and the tool guidance
        # comes second. The redundant _group_ctx_msg that we
        # inserted at the top of this function is dropped from
        # `messages` before the prepended sys_prompt so it doesn't
        # appear twice.
        sys_prompt = {
            'role': 'system',
            'content': (
                ((_persona_msg or {}).get("content", "") + "\n\n")
                + GROUP_CONTEXT
                + '\n\n## Tool use (tools mode)\n'
                + 'You have access to: '
                + 'donsetch_web_search / donsetch_web_fetch / '
                + 'donsetch_web_crawl / donsetch_web_screenshot, '
                + 'and react_to_message (set a Telegram reaction). '
                + 'Use a tool when the user asks for fresh data, '
                + 'real-world facts you cannot be sure about, weather, '
                + 'news, prices, sports results, or any web content. '
                + 'If a tool returns no useful data, say so honestly '
                + 'and suggest where the user can find the info '
                + 'themselves. Do NOT keep retrying the same query '
                + 'with variations. After getting a tool result, give '
                + 'a clear, concise answer in the user\'s language. '
                + 'If no tool is needed (greetings, opinions, math, '
                + 'code review, chitchat), just answer directly — do '
                + 'not call a tool unnecessarily.'
            )
        }
        # T2 (P0-2): drop the standalone _group_ctx_msg we added
        # at the function top — it's now inside sys_prompt. Without
        # this dedup the LlmChatPlace rules would appear twice and
        # the rating-vs-tools ordering would be inconsistent with
        # the rating branch (which keeps GROUP_CONTEXT as a
        # separate system message after RATING_RULES).
        messages = [m for m in messages if m is not _group_ctx_msg]
        # T6 (P1-4): _tag_sender is now module-level, used by both
        # the tools branch and the rating branch.
        messages = [_tag_sender(m) for m in messages]
        msgs = [sys_prompt] + messages
        req_tools = all_tools
    else:
        # Rating mode: classifier persona, no tools.
        sys_prompt = {
            'role': 'system',
            'content': (
                ((_persona_msg or {}).get("content", "") + "\n\n")
                + 'You are a message classifier. Classify the user message '
                'and emit the structured prefix. No tools. No chatter. '
                'See the rules above.'
            )
        }
        # T6 (P1-4): use the module-level _tag_sender (the
        # in-branch _tag_sender_rating closure was a byte-for-byte
        # duplicate of the tools-mode closure and is now removed).
        messages = [_tag_sender(m) for m in messages]
        msgs = [sys_prompt] + messages
        req_tools = None
    max_iter = 15
    final_fallback = None
    loop_start = time.monotonic()
    LOOP_BUDGET_SEC = limits.LOOP_BUDGET_SEC
    guard = LoopGuard(LOOP_BUDGET_SEC)  # configurable via LLAMABOT_LOOP_BUDGET_SEC; default 600 (10 min)

    # Accumulated reasoning across iterations (for the live feed)
    accumulated_reasoning = ""
    last_thinking_push = [0.0]  # mutable closure for throttling
    last_flood_at = [0.0]       # back off edit_text for a while after Telegram flood

    # Empty-response detection. The model sometimes returns no
    # content, no reasoning, and no finish_reason — particularly
    # on hard questions (future predictions, niche facts). The
    # caller checks for this sentinel string and either retries
    # or substitutes a user-friendly message. Defining it as a
    # constant here keeps the marker and the recovery logic in
    # one place.
    EMPTY_FALLBACK_PREFIX = '[model returned an empty response.'

    async def push_thinking(reasoning_text: str, force: bool = False):
        # T65.1: `silent` turns exist for messages addressed to another human
        # in a group. The LLM still runs and its reasoning still happens -- we
        # want it, that is the whole point of having the model classify the
        # message before reacting. But showing "💭 думаю…" for a message
        # nobody asked the bot about is noise: the group is talking to each
        # other, and the bot would be visibly thinking along. The reaction is
        # the only outward signal in that case.
        if silent:
            if reasoning_text:
                log.info(f"[LLAMA] silent reasoning ({len(reasoning_text)} chars, "
                    f"not shown to GUI)")
            return
        if thinking_msg is None or not reasoning_text:
            return
        now = time.monotonic()
        # If we hit a Telegram flood in the last 30s, lay off (skip edits).
        if now - last_flood_at[0] < 30.0 and not force:
            return
        if not force and now - last_thinking_push[0] < 5.0:
            return
        last_thinking_push[0] = now
        # Telegram message body limit is 4096 chars. Show the TAIL of the
        # reasoning so the user always sees what the model is currently
        # thinking. No leading "…" — just a one-line status + last 1800
        # chars. Reasoning from earlier iterations is preserved in the
        # final answer.
        prefix = "💭 думаю…\n"
        max_payload = 3800
        body = reasoning_text
        truncated_marker = ""
        if len(body) > max_payload:
            body = body[-max_payload:]
            truncated_marker = "\n[…earlier reasoning omitted…]\n"
        try:
            # IMPORTANT: editMessageText removes the inline keyboard
            # if reply_markup is not passed. To keep the ⏹ Stop
            # button visible while reasoning streams, we must
            # re-attach the markup on every edit.
            await thinking_msg.edit_text(
                f"{prefix}{truncated_marker}{body}",
                reply_markup=_stop_button_markup(),
            )
        except Exception as e:
            err = str(e).lower()
            if 'not modified' in err:
                pass  # no-op, the message is already up to date
            elif 'flood' in err or 'too many requests' in err:
                last_flood_at[0] = now
                log.info(f"[thinking edit] flood; backing off edits for 30s")
            else:
                log.info(f"[thinking edit err] {type(e).__name__}: {e}")

    for iteration in range(max_iter):
        # T44: per-turn accumulator for executed tools.
        _executed_tools_in_turn: list[dict] = []
        # Honour graceful shutdown between iterations. Without this, a SIGTERM
        # mid-call_llama is ignored until the current llama-server request
        # finishes (could be 5-7 min for a hard problem). docker stop
        # SIGKILLs after 10s, so we MUST bail faster than that.
        _sd = shutdown_event if shutdown_event is not None else SHUTDOWN_EVENT
        if _sd is not None and _sd.is_set():
            log.info(f"[call_llama] shutdown_event set, aborting tool loop at iteration {iteration}")
            return '[bot: shutdown requested, aborting tool loop]'
        # Honour per-task abort (Stop button on the thinking message).
        # Distinct from shutdown_event: shutdown is global ("kill the
        # process"), abort is per-message ("skip this one, move on").
        if abort_event is not None and abort_event.is_set():
            log.info(f"[call_llama] abort_event set, aborting tool loop at iteration {iteration}")
            return None
        # --- request body ---
        async with httpx.AsyncClient(timeout=limits.LLAMA_HTTP_TIMEOUT_SEC) as client:
            req_body = {
                "model": MODEL,
                "messages": msgs,
                "max_tokens": max_tokens,
                "stream": use_stream,
            }
            if req_tools is not None:
                req_body["tools"] = req_tools
                req_body["tool_choice"] = "auto"
                req_body["parallel_tool_calls"] = False
            # Diagnostic: log request structure only. NEVER dump raw bytes —
            # for vision requests that includes the base64 image, which is PII
            # and would land in journald / docker logs.
            if iteration == 0 and not use_stream:
                # Count the bytes that would be sent (without ever writing them).
                try:
                    # T54: sanitise lone surrogates before serialising.
                    # The model sometimes returns tool_call arguments
                    # with Python-style lone surrogates for non-BMP
                    # characters (e.g. emoji U+1F44D as \uD83D\uDC4D
                    # instead of UTF-8 bytes). ensure_ascii=False
                    # passes these through, producing JSON that
                    # llama-server's parser rejects with 500.
                    # Walk the dict once, replacing lone surrogates
                    # with U+FFFD (the Unicode replacement char).
                    def _scrub_surrogates(obj):
                        if isinstance(obj, str):
                            return obj.encode('utf-8', errors='surrogatepass').decode('utf-8', errors='replace')
                        if isinstance(obj, dict):
                            return {k: _scrub_surrogates(v) for k, v in obj.items()}
                        if isinstance(obj, list):
                            return [_scrub_surrogates(v) for v in obj]
                        return obj
                    req_body = _scrub_surrogates(req_body)
                    raw = json.dumps(req_body, ensure_ascii=False).encode('utf-8')
                    log.info(f"[LLAMA] iter={iteration} payload_bytes={len(raw)} (image bytes masked)")
                    if os.environ.get("LLAMABOT_DEBUG_PAYLOAD") == "1":
                        head = raw[:400].decode('utf-8', errors='replace')
                        # Find the column where the bad JSON starts by scanning
                        # for known key patterns. Most useful for diagnosing
                        # 500 from llama-server: this prints the head so we
                        # can see what we sent before the parser gave up.
                        log.info(f"[LLAMA-DEBUG] iter={iteration} first 400 bytes:\n{head}")
                except Exception as e:
                    log.info(f"[LLAMA] iter=0 cannot size payload: {e}")
            # W2.1: the previous version built a deepcopy of req_body
            # here (mutating it to mask image_url bytes for a log line)
            # but the actual prints read req_body directly, not the
            # copy. That meant a 256K-token req_body — possibly with
            # base64 image payloads — was being deep-copied on every
            # tool-loop iteration, then thrown away. On a 10-iter run
            # that is 10x the full request size, with no benefit.
            # Replaced with a small summary that doesn't need a copy.
            _msgs = req_body.get("messages", [])
            _imgs = sum(
                1 for m in _msgs
                if isinstance(m.get("content"), list)
                for it in m["content"]
                if isinstance(it, dict) and "image_url" in it
            )
            log.info(f"[LLAMA] iter={iteration} req_body_keys={list(req_body.keys())} "
                f"tools={'yes ('+str(len(req_tools))+' defs)' if req_tools else 'no'} "
                f"msgs_count={len(_msgs)} image_msgs={_imgs}")
            if iteration == 0:
                # On first iter, also print the first AND last user
                # message structure (the first to confirm history is
                # loaded; the last to confirm the current speaker
                # got tagged with "From: <name>: ").
                user_msgs = [m for m in req_body.get("messages", []) if m.get("role") == "user"]
                if user_msgs:
                    m0 = user_msgs[0]
                    c0 = m0.get("content")
                    if isinstance(c0, list):
                        log.info(f"[LLAMA]   first user msg content types: {[item.get('type') for item in c0 if isinstance(item, dict)]}")
                    else:
                        log.info(f"[LLAMA]   first user msg content head: {str(c0)[:150]!r}")
                    ml = user_msgs[-1]
                    cl = ml.get("content")
                    if isinstance(cl, list):
                        first_part = cl[0] if cl else {}
                        first_text = first_part.get("text", "") if isinstance(first_part, dict) else ""
                        log.info(f"[LLAMA]   last user msg first text part head: {first_text[:150]!r}")
                    else:
                        log.info(f"[LLAMA]   last user msg content head: {str(cl)[:150]!r}")
            reasoning_buf = ""
            content_buf = ""
            tool_calls_buf = {}
            finish_reason = None

            if not use_stream:
                # --- non-streaming POST (vision tasks, slow-reasoning models) ---
                # Use a fresh client with no keep-alive — somehow a keep-alive
                # connection from a prior streaming call to llama-server was
                # producing 400 for the next request even though the bytes
                # were identical. Closing the client per call avoids that.
                # Return content immediately — no tool-call loop in this path
                # (vision tasks don't need tools, and the multi-iter loop below
                # was raising somewhere we couldn't catch on the vision payload).
                r = None
                try:
                    result = await non_stream_iteration(
                        req_body, iteration=iteration,
                        abort_event=abort_event)
                    if result.outcome == "final":
                        return result.final_text
                    content = result.content
                    reasoning = result.reasoning
                    non_stream_tool_calls = result.tool_calls
                    finish_reason = result.finish_reason
                    if result.saw_unavailable_tool:
                        saw_unavailable_tool = True
                    log.info(f"[LLAMA] iter={iteration} non-stream finish={result.finish_reason} content_chars={len(content)} reasoning_chars={len(reasoning)} tool_calls={len(non_stream_tool_calls)}")
                    if reasoning and thinking_msg is not None:
                        await push_thinking(accumulated_reasoning + reasoning, force=True)
                    accumulated_reasoning += reasoning
                    # If the model returned tool_calls (e.g. react_to_message
                    # on a vision task), fall through to the unified tool
                    # loop below - DO NOT short-circuit with `return content`,
                    # otherwise the tool call is silently dropped and the
                    # user sees an empty response. The non-streaming path
                    # was originally vision-only with no tools, so the
                    # unconditional return was safe; once we added
                    # react_to_message (Oct 2026) it isn't.
                    if not non_stream_tool_calls:
                        return content
                    # Populate the streaming-side tool_calls_buf in the same
                    # shape the SSE parser produces ({idx: {id, name,
                    # arguments}}). The code that reconstructs the final
                    # `tool_calls` list (just below) iterates over this
                    # buffer, so by populating it here we let the same
                    # shared dispatch path handle the non-streaming case.
                    # Set a sentinel so the streaming block below is
                    # skipped for this iteration (we already have a
                    # response and we don't want a second POST).
                    _ns_handled = True
                    content_buf = content
                    reasoning_buf = accumulated_reasoning
                    tool_calls_buf = {}
                    for idx, tc in enumerate(non_stream_tool_calls):
                        fn = tc.get('function', {}) or {}
                        tool_calls_buf[idx] = {
                            'id': tc.get('id', f'call_ns_{idx}'),
                            'name': fn.get('name', ''),
                            'arguments': fn.get('arguments', '') or '',
                        }
                        log.info(f"[LLAMA]   call: {fn.get('name','')}({(fn.get('arguments') or '')[:200]})")
                except Exception as e:
                    # r is bound to the httpx Response if we got past
                    # the .post() call; otherwise it's still None
                    # (e.g. JSONDecodeError, ConnectionError before
                    # the response object is constructed).
                    body = r.text[:500] if r is not None else ''
                    log.info(f"[non-stream err iter={iteration}] {type(e).__name__}: {e}; body={body!r}")
                    # v0.8.1: 500 from llama-server is usually the PEG
                    # grammar parser rejecting the model tool-call
                    # output. Retry ONCE without tools.
                    if r is not None and getattr(r, 'status_code', None) == 500 and req_body.get('tools'):
                        log.info(f"[call_llama] 500 on tools-enabled request; retrying iter={iteration} without tools")
                        fallback_body = dict(req_body)
                        fallback_body.pop('tools', None)
                        fallback_body.pop('tool_choice', None)
                        try:
                            r2 = await _abortable_post(
                                vclient, f"{LLAMA_URL}/chat/completions",
                                abort_event=abort_event,
                                headers={"Authorization": f"Bearer {API_KEY}",
                                         "Content-Type": "application/json"},
                                json=fallback_body,
                            )
                            if r2 is None:
                                return None
                            r2.raise_for_status()
                            data2 = r2.json()
                            # T32 (2026-10-13): same guard as the primary
                            # and first-fallback paths.
                            _choices3 = data2.get("choices") or []
                            if not _choices3:
                                log.info(f"[call_llama] tools-less fallback: empty choices, returning None")
                                return None
                            content2 = _choices3[0].get("message", {}).get("content") or ''
                            log.info(f"[call_llama] tools-less fallback succeeded: {len(content2)} chars")
                            return content2
                        except Exception as e2:
                            log.info(f"[call_llama] tools-less fallback also failed: {type(e2).__name__}: {e2}")
                    return f'[llama-server request failed: {e}]'
            if not locals().get('_ns_handled'):
                try:
                    async with client.stream("POST", f"{LLAMA_URL}/chat/completions",
                                              headers={"Authorization": f"Bearer {API_KEY}",
                                                       "Content-Type": "application/json"},
                                              json=req_body) as r:
                        r.raise_for_status()
                        # Pre-check for abort before entering the line
                        # iteration. This catches the case where the
                        # user clicks Stop RIGHT after the bot sends
                        # the thinking message but before the LLM has
                        # produced any chunks.
                        #
                        # IMPORTANT: do NOT use `asyncio.wait_for` with
                        # a short timeout on `__anext__()`. Cancelling
                        # the read task mid-stream corrupts httpx's
                        # internal state and causes the LLM to return
                        # empty responses on subsequent calls. Plain
                        # `async for` is safe; the only downside is a
                        # small window (typically <1s) where an abort
                        # set after the LLM has started but before the
                        # first chunk is not seen until the first
                        # chunk arrives. The between-chunk check below
                        # handles that.
                        if abort_event is not None and abort_event.is_set():
                            log.info(f"[call_llama] abort_event set pre-stream "
                                f"iter={iteration}")
                            return None
                        acc = StreamAccumulator()
                        async for line in r.aiter_lines():
                            # Between-chunk abort check. Fires as soon
                            # as a chunk arrives if the user clicked
                            # Stop during the previous chunk's
                            # processing.
                            if abort_event is not None and abort_event.is_set():
                                log.info(f"[call_llama] abort_event set between "
                                    f"chunks iter={iteration}")
                                return None
                            # SSE framing stays here, NOT in feed():
                            # the accumulator parses JSON objects, and the
                            # line protocol is the transport's business.
                            # The first draft of this cut dropped all three
                            # guards below, which sent the "[DONE]" sentinel
                            # into json.loads on every single stream.
                            if not line or not line.startswith("data: "):
                                continue
                            payload = line[6:]
                            if payload.strip() == "[DONE]":
                                break
                            try:
                                chunk = json.loads(payload)
                            except json.JSONDecodeError:
                                continue
                            d = acc.feed(chunk)
                            if d.grew_reasoning:
                                await push_thinking(
                                    accumulated_reasoning + acc.reasoning)
                            reasoning_buf = acc.reasoning
                            content_buf = acc.content
                            tool_calls_buf = acc.tool_calls
                            if d.finish_reason:
                                finish_reason = d.finish_reason
                        # Final flush: ensure the latest reasoning text is on Telegram
                        if reasoning_buf:
                            await push_thinking(accumulated_reasoning + reasoning_buf, force=True)
                except httpx.HTTPError as e:
                    log.info(f"[stream err iter={iteration}] {type(e).__name__}: {e}; falling back to non-streaming")
                    # Fall back to non-streaming request
                    try:
                        async with httpx.AsyncClient(timeout=limits.LLAMA_HTTP_TIMEOUT_SEC) as client2:
                            r2 = await client2.post(
                                f"{LLAMA_URL}/chat/completions",
                                headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
                                json={**req_body, "stream": False},
                            )
                            r2.raise_for_status()
                            data = r2.json()
                        # T32 (2026-10-13): same guard as the primary
                        # non-streaming path above — 'choices' may be
                        # empty on rate-limit / context-overflow / filter
                        # responses. Without this, the user gets a
                        # 'list index out of range' on the fallback path
                        # too, even though we already guarded the
                        # primary path.
                        _choices2 = data.get("choices") or []
                        if not _choices2 and is_context_overflow(
                            json.dumps(data.get("error") or data)[:500]
                        ):
                            log.info("[call_llama] CONTEXT OVERFLOW on fallback")
                            return OVERFLOW_MESSAGE
                        if not _choices2:
                            return ("[model returned no choices on fallback — "
                                    "the prompt may have exceeded the context window, "
                                    "hit a rate limit, or been blocked by a content "
                                    "filter. Try a shorter prompt or try again in a minute.]")
                        msg = _choices2[0].get("message") or {}
                        tool_calls = msg.get("tool_calls") or []
                        # T38.5: tool isolation (see validate_tool_calls docstring).
                        try:
                            validate_tool_calls(tool_calls)
                        except UnknownToolNameError as _e:
                            # T43: soft-reject (same as site 1).
                            bad_names = sorted({(tc.get("function") or {}).get("name") or "" for tc in tool_calls})
                            log.info(f"[TOOL-ISOLATION] iter={iteration} soft-reject {bad_names!r}; "
                                f"injecting tool result feedback")
                            for tc in tool_calls:
                                fn = (tc.get("function") or {})
                                if (fn.get("name") or "") not in KNOWN_TOOL_NAMES:
                                    fn["name"] = "__rejected__"
                                    fn["arguments"] = "{}"
                            saw_unavailable_tool = True
                        if not tool_calls:
                            if msg.get("content"):
                                return msg["content"]
                            if msg.get("reasoning_content"):
                                tail = msg["reasoning_content"][-3500:]
                                return (
                                    f'_(fallback non-stream: content пустой, '
                                    f'reasoning_chars={len(msg["reasoning_content"])})_\n\n'
                                    f'{tail}'
                                )
                            return f'[fallback empty: finish_reason={msg.get("finish_reason")}]'
                        msgs.append(msg)
                        if msg.get("content"):
                            final_fallback = msg["content"]
                        # jump into tool execution below by reusing local var
                        reasoning_buf = msg.get("reasoning_content") or ""
                        content_buf = msg.get("content") or ""
                    except Exception as e2:
                        log.info(f"[fallback err iter={iteration}] {type(e2).__name__}: {e2}")
                        return f'[both streaming and non-streaming failed: stream_err={e!r}, fallback_err={e2!r}]'

            else:
                pass
        # commit accumulated reasoning for next-iteration display
        accumulated_reasoning += reasoning_buf

        # assemble final message
        msg = {
            "role": "assistant",
            "content": content_buf or None,
            "reasoning_content": reasoning_buf or None,
        }
        tool_calls = []
        for idx, tc in tool_calls_buf.items():
            if tc["name"]:
                # T38.5: streaming tool_calls_buf[name] comes from llama-server's
                # stream chunks. Reject any unknown name BEFORE building the
                # tool_calls list that we will execute or send back to the model.
                if tc["name"] not in KNOWN_TOOL_NAMES:
                    # T43: soft-reject (same as site 1, applied to the
                    # streaming chunk that just arrived). We skip this
                    # tool call but keep the iteration alive by
                    # appending a synthetic tool result that names the
                    # offending tool and the available set.
                    log.info(f"[TOOL-ISOLATION] iter={iteration} soft-reject streamed "
                        f"tool name {tc['name']!r}; injecting tool result feedback")
                    bad_name = tc["name"]
                    tc["name"] = "__rejected__"
                    saw_unavailable_tool = True
                    # We can't easily inject a tool-role message here
                    # because the streaming buffer is being filled in
                    # real time; the unhandled-tool branch further down
                    # the loop will add the role:tool message with the
                    # rejection text. Just continue the iteration.
                args_str = tc["arguments"]
                try:
                    args_obj = json.loads(args_str) if args_str else {}
                except Exception:
                    args_obj = {}
                tool_calls.append({
                    "id": tc["id"] or f"call_{idx}",
                    "type": "function",
                    "function": {"name": tc["name"],
                                  "arguments": json.dumps(args_obj) if args_obj else args_str},
                })
        if tool_calls:
            msg["tool_calls"] = tool_calls
        tool_calls = msg.get("tool_calls") or []
        log.info(f"[LLAMA] iter={iteration} finish={finish_reason} content_chars={len(content_buf)} reasoning_chars={len(reasoning_buf)} tool_calls={len(tool_calls)} req_tools={'yes' if req_tools else 'no'}")
        for tc in tool_calls:
            log.info(f"[LLAMA]   call: {tc.get('function',{}).get('name','')}({tc.get('function',{}).get('arguments','')[:200]})")

        if not tool_calls:
            if content_buf:
                return {
                    "text": content_buf,
                    "executed_tools": list(_executed_tools_in_turn),
                    "aborted": False,
                }
            # Empty content but reasoning exists (Ornith burned the whole token budget on
            # reasoning). Surface the reasoning as the answer rather than the generic
            # fallback message — it's still useful info, and tells the user why the bot
            # couldn't reply normally.
            if reasoning_buf:
                tail = reasoning_buf[-3500:]
                return (
                    f'_(модель потратила все токены на размышления, не оставив на сам ответ; '
                    f'finish_reason={finish_reason}; reasoning_chars={len(reasoning_buf)})_\n\n'
                    f'{tail}'
                )
            return (
                f'[model returned an empty response. finish_reason={finish_reason}, '
                f'reasoning_chars={len(reasoning_buf)}, content_chars=0]'
            )
        # If we asked for tools and the model produced some, run them.
        # If we did NOT ask for tools (vision/math turn) and the model still
        # hallucinated a tool_call, return whatever content we have rather
        # than enter a loop the user can't escape.
        if req_tools is None:
            log.warning(f"[LLAMA] iter={iteration} WARNING: model hallucinated tool_calls={len(tool_calls)} with req_tools=None; aborting tool loop")
            if content_buf:
                return {
                    "text": content_buf,
                    "executed_tools": list(_executed_tools_in_turn),
                    "aborted": False,
                }
            return (
                f'_(модель попыталась вызвать инструмент, хотя в этом turn\'е он не запрашивался; '
                f'finish_reason={finish_reason}, content_chars={len(content_buf)})_\n\n'
                f'{reasoning_buf[-3500:] if reasoning_buf else "(no reasoning)"}'
            )
        if content_buf:
            final_fallback = content_buf
        msgs.append(msg)

        # --- execute tool calls ---
        iter_had_real_result = False
        saw_unavailable_tool = False
        current_calls = []
        log.info(f"[loop] iter={iteration} tool_calls={len(tool_calls)} finish={finish_reason} content_chars={len(content_buf)} reasoning_chars={len(reasoning_buf)}")
        for tc in tool_calls:
            fn_name = tc.get('function', {}).get('name', '')
            raw_args = tc.get('function', {}).get('arguments', '{}')
            # --- Defensive parse of tool call args ---
            # Qwen3.8-27B-Ultra-Heretic (and a few other recent models
            # with speculative decoding / MTP heads) sometimes emit
            # malformed tool-call JSON during long chains — most
            # commonly the same key repeated many times, or the
            # closing brace clipped. json.loads then either accepts
            # only the last value of a duplicated key, or raises and
            # we silently fall back to {} which then errors at the
            # tool side. Both paths waste a turn. Detect up front:
            # - if the raw arg string is suspiciously long, or
            # - if it doesn't parse AND is non-empty (model produced
            #   garbage, not a real tool call), or
            # - if it parses to {} while the raw string is non-empty
            #   (parsing dropped all keys), or
            # - if the same top-level key appears > 3 times
            # then return a feedback message asking the model to
            # retry. This costs one iteration but prevents the
            # runaway loop that ends in a 500 from llama-server.
            args = None
            if isinstance(raw_args, str) and raw_args:
                # Cheap check: too many duplicate top-level keys.
                # E.g. "max_chars":8000,"max_chars":8000,... — we don't
                # need exact JSON parsing to count this.
                if raw_args.count('":') > 8 or len(raw_args) > 1500:
                    log.info(f"[tool] iter={iteration} REJECTED {fn_name} args too repetitive or too long ({len(raw_args)} chars); asking model to retry")
                    msgs.append({
                        'role': 'tool',
                        'tool_call_id': tc.get('id', ''),
                        'content': f'[bot: your tool call for {fn_name} had malformed args (length={len(raw_args)}, possibly duplicate keys or clipped). Please re-emit the tool call with a single, well-formed JSON object.]',
                    })
                    guard.note_rejected_call()
                    continue
                try:
                    args = json.loads(raw_args)
                except Exception as e:
                    log.info(f"[tool] iter={iteration} REJECTED {fn_name} args unparseable: {e!r} (head={raw_args[:120]!r})")
                    msgs.append({
                        'role': 'tool',
                        'tool_call_id': tc.get('id', ''),
                        'content': f'[bot: your tool call for {fn_name} had invalid JSON ({e!r}). Please re-emit with a single well-formed JSON object.]',
                    })
                    guard.note_rejected_call()
                    continue
                # If parsing yielded {} for a non-empty raw string,
                # the model produced only structural noise (e.g.
                # "{{" or "null" or duplicated empty key). Reject.
                if not args and raw_args.strip() not in ('{}', 'null', '[]'):
                    log.info(f"[tool] iter={iteration} REJECTED {fn_name} args parsed to empty for raw={raw_args[:120]!r}")
                    msgs.append({
                        'role': 'tool',
                        'tool_call_id': tc.get('id', ''),
                        'content': f'[bot: your tool call for {fn_name} parsed to an empty object. Please re-emit with actual parameters.]',
                    })
                    guard.note_rejected_call()
                    continue
            else:
                args = {}
            tc_id = tc.get('id', '')
            current_calls.append((fn_name, json.dumps(args, sort_keys=True) if isinstance(args, dict) else str(args)))
            log.info(f"[tool] iter={iteration} call {fn_name}({args})")
            if fn_name == 'react_to_message':
                result = await _execute_react_to_message(
                    args,
                    bot=bot,
                    chat_id=chat_id,
                    default_message_id=current_message_id,
                )
                _executed_tools_in_turn.append({
                    "name": "react_to_message",
                    "args": args if isinstance(args, dict) else {},
                    "iter": iteration,
                })
            elif fn_name == 'bash_command':
                # T39: forward to the vaskes/OpenWebUI-bash-sandbox
                # side-car. The tool_isolation guard earlier in this
                # iter (see T38.5) has already checked that
                # 'bash_command' is in KNOWN_TOOL_NAMES, and the
                # bot_side_tool_defs append above has already
                # confirmed personal+token. Defence in depth: also
                # check here in case the call path was reached via a
                # tools= param from the caller.
                try:
                    from personal_tools import bash_command as _bc
                except Exception as e:
                    result = f'[bash_command unavailable: {e!r}]'
                else:
                    if not _bc.is_enabled_for_active_profile():
                        result = (
                            '[bash_command unavailable: requires '
                            'LLAMABOT_DEPLOYMENT_PROFILE=personal '
                            'and AGENT_STACK_TOKEN. This chat is '
                            'not in personal mode.]'
                        )
                    else:
                        # T51 C5: session_id is intentionally
                        # chat-{chat_id} (deterministic, NOT random).
                        # Random per-call IDs would break the
                        # persistent-shell feature (cwd / env /
                        # history are tmux-bound in the sandbox,
                        # so they survive across calls only when
                        # the same session_id is reused). The

                        sid = f'chat-{chat_id}'  # T57 #S16: never trust the LLM's session_id
                        cmd = args.get('command', '')
                        if not cmd:
                            result = '[bash_command: empty command]'
                        else:
                            r = await _bc.call_bash_command(
                                cmd, session_id=sid, chat_type=chat_type,
                                chat_id=chat_id)
                            result = _bc.tool_result_to_text(r)
                            _executed_tools_in_turn.append({
                                "name": "bash_command",
                                "args": args if isinstance(args, dict) else {},
                                "iter": iteration,
                                "ok": r.get("ok", False),
                            })
            elif fn_name in CUSTOM_TOOLS:
                result = await CUSTOM_TOOLS[fn_name](args)
                _executed_tools_in_turn.append({
                    "name": fn_name,
                    "args": args if isinstance(args, dict) else {},
                    "iter": iteration,
                })
            elif fn_name in DONSETCH_TOOLS:
                result = await DONSETCH_TOOLS[fn_name](args)
                _executed_tools_in_turn.append({
                    "name": fn_name,
                    "args": args if isinstance(args, dict) else {},
                    "iter": iteration,
                })
            else:
                result = f'[tool {fn_name} unavailable in this mode. Bot supports: donsetch_web_search, donsetch_web_fetch, donsetch_web_crawl, donsetch_web_screenshot, react_to_message, bash_command (personal mode only).]'
                saw_unavailable_tool = True
            r_str = str(result)
            is_empty = (
                '0 results' in r_str.lower() or
                'engines unavailable' in r_str.lower() or
                r_str.startswith('[donsetch') or
                r_str.startswith('[fetch error') or
                r_str.startswith('[tool')
            )
            if not is_empty:
                iter_had_real_result = True
            msgs.append({
                'role': 'tool',
                'tool_call_id': tc_id,
                'content': r_str[:12000],
            })

        # T49: removed the early-return on saw_unavailable_tool.
        # The synthetic tool result is already in msgs (the
        # `else:` branch above appended it). We let the loop
        # continue so the model can see the feedback on the
        # next iteration and either retry with a whitelisted
        # tool or fall back to plain text. The T32c stuck-call
        # detector (same_calls == prev_calls) handles the case
        # where the model keeps asking for the same forbidden
        # tool - it returns after 2 iters with a clear message.
        # The max_iter / LOOP_BUDGET_SEC cap bounds the total
        # time. See the original T43 commit message for the
        # design intent.
        if saw_unavailable_tool:
            log.info(f"[loop] iter={iteration} unhandled tool(s) called; "
                f"continuing so model can see the synthetic tool result "
                f"on the next iter and retry with a whitelisted tool")

        # If the model is asking for the same tool+args as in the previous
        # iteration, it has looped — return whatever we have and stop.
        # T32c (2026-10-13): guard against current_calls being empty
        # (e.g. all tool calls in this iter were REJECTED for malformed
        # args, so current_calls was never appended to). An empty list
        # equals prev_calls=[] on the first iter, which would trigger
        # a spurious "stuck" detection and IndexError. Only compare
        # when there's actually a tool call to compare.
        elapsed = time.monotonic() - loop_start
        outcome = guard.tick(
            iteration=iteration,
            current_calls=current_calls,
            elapsed=elapsed,
            iter_had_real_result=iter_had_real_result,
            final_fallback=final_fallback,
            reasoning_tail=(accumulated_reasoning or reasoning_buf)[-3500:],
            finish_reason=finish_reason,
        )
        if isinstance(outcome, Abort):
            return outcome.text
        if outcome is not None:
            msgs.append({'role': 'user', 'content': outcome.text})

        if isinstance(outcome, Nudge):

            # Final non-streamed summarize call (no tools — we want a plain answer)
            async with httpx.AsyncClient(timeout=limits.LLAMA_HTTP_TIMEOUT_SEC) as client3:
                r3 = await client3.post(
                    f"{LLAMA_URL}/chat/completions",
                    headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
                    json={
                        "model": MODEL,
                        "messages": msgs,
                        "tool_choice": "none",
                        "max_tokens": max_tokens,
                        "stream": False,
                    }
                )
                r3.raise_for_status()
                data3 = r3.json()
            # T32b (2026-10-13): guard. Without this, a 200 OK
            # with choices:[] (rate limit / content filter /
            # context overflow on the summarize call) would
            # produce 'list index out of range' in the bot's
            # reply, the same regression T32 was supposed to
            # close. This is the 5th call site that needed
            # the guard.
            _choices3b = data3.get("choices") or []
            if not _choices3b:
                log.info(f"[call_llama] summarize fallback: empty choices, returning explanatory error")
                return ("[bot: the model returned no choices on the summarize call — "
                        "the prompt may have exceeded the context window, hit a rate limit, "
                        "or been blocked by a content filter. Try a shorter prompt or "
                        "try again in a minute.]")
            msg3 = _choices3b[0].get("message") or {}
            return msg3.get("content") or (
                '[bot: search returned no useful data. '
                'For live data (flight prices, news, stocks), please use a direct service '
                'like Aviasales, Google Flights, or your browser.]'
            )

    if final_fallback:
        return final_fallback
    return ('[bot: exceeded tool-calling iteration limit. '
            'Please try a simpler question, /reset, or a different model.]')





CUSTOM_TOOLS = {
}
# === Backward-compat aliases (used by tests and old call sites) ===
async def call_llama_compat(*args, **kwargs):
    return await call_llama(*args, **kwargs)

