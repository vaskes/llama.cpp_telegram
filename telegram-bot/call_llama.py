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
from llm_nonstream import (classify_error, non_stream_iteration,
                           non_stream_step)
from llm_sse import StreamAccumulator
from llm_markup import _stop_button_markup  # noqa: F401  (re-export)
from llm_iteration import ToolLoop
from llm_loop_guard import Abort, LoopGuard, Nudge
# Nudge was missing from this import between a079e3c and now. It is
# used on the branch that follows `isinstance(outcome, Abort)`, so a
# Nudge -- which happens when the empty-call budget reaches 2, i.e.
# whenever a tool returns "[unavailable]" twice -- raised NameError
# and took the whole turn down. py_compile cannot see it, and 925
# tests were green because none of them drives the loop to a nudge:
# touches the caller.
from llm_nonstream import tools_less_fallback
from llm_toolcalls import normalize_tool_calls, reject_unknown_names
from llm_dispatch import (DispatchDeps, MAX_TOOL_RESULT_CHARS,
                         execute_tool)
from llm_finish import (finish_fallback_empty,
                       finish_without_tool_calls, refuse_hallucinated_tools)
from llm_toolargs import ArgsRejected, parse_tool_args
from llm_json import dumps_body
from llm_request import (
    build_request_body, log_request_shape,
    new_stream_accumulators,
)
from llm_thinking import ThinkingThrottle, push_thinking
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
        # send_file reads from the same sandbox, so it is advertised
        # under the same condition: no sandbox, no way to send a file
        # out of it. (2026-10-24: the operator asked for a binary file
        # and the bytes had no way to travel -- the model's text was
        # the only channel.)
        from llm_sendfile import send_file_tool_def
        bot_side_tool_defs.append(send_file_tool_def())
    # delete_messages removes messages from the chat itself, so it does
    # not need the sandbox. It is advertised only where the bot may
    # actually delete something: in a private chat it owns the
    # conversation, and in a group or a channel Telegram only lets an
    # administrator delete other people's messages. The check is
    # Telegram's and it costs an API call, so it is cached for five
    # minutes (llm_delete.bot_may_delete).
    if bot is not None and chat_id is not None:
        from llm_delete import bot_may_delete, delete_messages_tool_def
        may_delete, _why_not = await bot_may_delete(bot, chat_id, chat_type)
        if may_delete:
            bot_side_tool_defs.append(delete_messages_tool_def())
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
                + 'react_to_message (set a Telegram reaction), '
                + 'and send_file (send a file from the sandbox to the '
                + 'chat as a document). '
                + 'When the user asks for a FILE -- an archive, an '
                + 'image, a generated script, a binary blob -- create '
                + 'it with bash_command and then call send_file with '
                + 'its path. Do not paste the contents into the chat '
                + 'and do not send base64: the file itself is what the '
                + 'user asked for, and it does not have to fit in a '
                + 'message. The user sees what your commands printed, '
                + 'so do not repeat the console output either. '
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
    loop_start = time.monotonic()
    LOOP_BUDGET_SEC = limits.LOOP_BUDGET_SEC
    guard = LoopGuard(LOOP_BUDGET_SEC)  # configurable via LLAMABOT_LOOP_BUDGET_SEC; default 600 (10 min)

    # Per-turn throttle. Was two one-element lists closed over by
    # push_thinking; now explicit, and testable without call_llama.
    _throttle = ThinkingThrottle()

    # Empty-response detection used to be a local here, called
    # EMPTY_FALLBACK_PREFIX, with a comment saying it kept "the
    # marker and the recovery logic in one place". Nothing read
    # it: the marker is written by llm_finish.finish_fallback_empty
    # and config.EMPTY_RESPONSE_FALLBACK, and screens for in six
    # places in handlers.py. All of them now share one constant,
    # config.EMPTY_RESPONSE_PREFIX, because this one was a comment
    # with a name and the name was the part that looked load-bearing.


    return await ToolLoop(
        msgs,
        max_tokens=max_tokens, req_tools=req_tools, bot=bot, chat_id=chat_id,
        current_message_id=current_message_id, chat_type=chat_type,
        shutdown_event=shutdown_event, abort_event=abort_event,
        thinking_msg=thinking_msg, silent=silent, use_stream=use_stream,
        throttle=_throttle, guard=guard, loop_start=loop_start,
        custom_tools=CUSTOM_TOOLS,
    ).run()





CUSTOM_TOOLS = {
}
# === Backward-compat aliases (used by tests and old call sites) ===
async def call_llama_compat(*args, **kwargs):
    return await call_llama(*args, **kwargs)

