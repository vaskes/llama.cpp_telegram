# call_llama.py
# The LLM-call layer: tool definitions, donsetch MCP client,
# sender-name tagging, the big call_llama() coroutine, and
# voice transcription.
#
# This is the heart of the bot - everything that talks to
# llama-server or donsetch-http lives here. handlers.py calls
# call_llama() and persists results through persistence.persist().

import asyncio
import base64
import copy
import json
import urllib.parse
from typing import Any

import httpx

from config import (
    API_KEY, DISABLED_TOOLS, DONSETCH_SESSION_ID, DONSETCH_URL,
    LLAMA_URL, MODEL, WHISPER_URL, _TOOLS_CACHE,
)
from prompts import GROUP_CONTEXT, RATING_RULES


# === Default model discovery ===
async def _discover_default_model(base_url: str, api_key: str) -> str:
    """Query /v1/models and return the first model id."""
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(f"{base_url}/models", headers=headers)
        r.raise_for_status()
        data = r.json()
        models = data.get("data", [])
        if not models:
            raise RuntimeError(f"No models available at {base_url}/models")
        return models[0]["id"]


# === Custom tools ===
def _discover_default_model(base_url: str, api_key: str) -> str:
    """Hit /v1/models, return the first model id. Used only if MODEL is unset."""
    import urllib.request
    import json as _json
    base = base_url.rstrip('/').removesuffix('/v1')
    req = urllib.request.Request(
        f"{base}/v1/models",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = _json.loads(resp.read())
    models = data.get("data") or data.get("models") or []
    if not models:
        raise RuntimeError(f"llama-server at {base}/v1/models returned no models")
    first = models[0]
    return first.get("id") or first.get("name") or ""

if not MODEL:
    MODEL = _discover_default_model(LLAMA_URL, API_KEY)
    print(f"[config] MODEL not set, auto-discovered from llama-server: {MODEL}", flush=True)



async def get_weather(args):
    """Custom tool: wttr.in для погоды. Always works, no CAPTCHA."""
    location = args.get('location', '') or args.get('city', '')
    if not location:
        return '[weather error: empty location]'
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(f'https://wttr.in/{urllib.parse.quote(location)}?format=j1&lang=ru')
            r.raise_for_status()
            data = r.json()
        cur = data.get('current_condition', [{}])[0]
        lang_ru = cur.get('lang_ru')
        if isinstance(lang_ru, list) and lang_ru:
            desc = lang_ru[0].get('value', '')
        else:
            wd = cur.get('weatherDesc', [{}])
            desc = wd[0].get('value', '') if isinstance(wd, list) and wd else ''
        temp = cur.get('temp_C', '?')
        feels = cur.get('FeelsLikeC', '?')
        humidity = cur.get('humidity', '?')
        wind = cur.get('windspeedKmph', '?')
        return f'Weather in {location}: {desc}, {temp}C (feels {feels}C), humidity {humidity}%, wind {wind} km/h'
    except Exception as e:
        return f'[wttr error: {e}]'


async def _donsetch_init():
    """Initialize MCP session with donsetch-http. Returns session id."""
    global _donsetch_session_id
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(
            DONSETCH_URL,
            headers={"Content-Type": "application/json",
                     "Accept": "application/json, text/event-stream"},
            json={
                "jsonrpc": "2.0",
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "llama.cpp_telegram_bot", "version": "1.0"},
                },
                "id": "init",
            },
        )
        r.raise_for_status()
        sid = r.headers.get("mcp-session-id")
        if not sid:
            raise RuntimeError(f"donsetch initialize did not return session id (status {r.status_code}, body {r.text[:200]})")
        _donsetch_session_id = sid
        # Send the initialized notification as required by MCP spec
        await client.post(
            DONSETCH_URL,
            headers={"Content-Type": "application/json",
                     "Accept": "application/json, text/event-stream",
                     "Mcp-Session-Id": sid},
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        return sid


async def donsetch_call(tool_name: str, arguments: dict) -> str:
    """Call a tool on donsetch-http via MCP JSON-RPC.

    tool_name: the bare name (e.g. 'web_search') or the prefixed name
               ('donsetch_web_search'). Prefix is stripped automatically.
    Returns the tool output as a string (may be markdown).
    """
    global _donsetch_session_id
    bare = tool_name
    if bare.startswith("donsetch_"):
        bare = bare[len("donsetch_"):]
    async with _donsetch_session_lock:
        if _donsetch_session_id is None:
            try:
                await _donsetch_init()
            except Exception as e:
                return f'[donsetch init error: {e}]'
        for attempt in range(2):
            async with httpx.AsyncClient(timeout=120.0) as client:
                try:
                    r = await client.post(
                        DONSETCH_URL,
                        headers={"Content-Type": "application/json",
                                 "Accept": "application/json, text/event-stream",
                                 "Mcp-Session-Id": _donsetch_session_id},
                        json={
                            "jsonrpc": "2.0",
                            "method": "tools/call",
                            "params": {"name": bare, "arguments": arguments},
                            "id": "call",
                        },
                    )
                    if r.status_code == 404 or (
                        'session' in (r.text or '').lower()
                        and 'unknown' in (r.text or '').lower()
                    ):
                        # session expired — re-init and retry once
                        _donsetch_session_id = None
                        await _donsetch_init()
                        continue
                    r.raise_for_status()
                    data = r.json()
                    if 'error' in data:
                        return f'[donsetch error {data["error"].get("code","?")}: {data["error"].get("message","?")}]'
                    result = data.get('result', {})
                    # MCP tool result: {"content": [{"type":"text","text":"..."}, ...], "isError": false}
                    content = result.get('content', [])
                    parts = []
                    for block in content:
                        if isinstance(block, dict):
                            if block.get('type') == 'text':
                                parts.append(block.get('text', ''))
                            elif block.get('type') == 'image':
                                parts.append(f'[image: {block.get("mimeType","?")}, {len(block.get("data",""))} bytes]')
                            else:
                                parts.append(str(block))
                        else:
                            parts.append(str(block))
                    if not parts:
                        return result.get('text') or '[donsetch: empty result]'
                    text = '\n'.join(parts)
                    if result.get('isError'):
                        return f'[donsetch tool error: {text}]'
                    return text[:24000]
                except Exception as e:
                    if attempt == 1:
                        return f'[donsetch call error: {e}]'
                    _donsetch_session_id = None
        return '[donsetch: failed after retry]'


async def execute_donsetch_web_search(args):
    """Tool: donsetch_web_search — search the web."""
    return await donsetch_call('web_search', args)


async def execute_donsetch_web_fetch(args):
    """Tool: donsetch_web_fetch — fetch a URL as markdown."""
    return await donsetch_call('web_fetch', args)


async def execute_donsetch_web_crawl(args):
    """Tool: donsetch_web_crawl — crawl a site."""
    return await donsetch_call('web_crawl', args)


async def execute_donsetch_web_screenshot(args):
    """Tool: donsetch_web_screenshot — capture URL as PNG."""
    return await donsetch_call('web_screenshot', args)


DONSETCH_TOOLS = {
    'donsetch_web_search': execute_donsetch_web_search,
    'donsetch_web_fetch': execute_donsetch_web_fetch,
    'donsetch_web_crawl': execute_donsetch_web_crawl,
    'donsetch_web_screenshot': execute_donsetch_web_screenshot,
}


def _tag_sender(m):
    """T6 (P1-4): prepend "From: <name>: " to user content so the
    LLM can tell Vasisualy from Dimon in a multi-human thread.
    Sender name comes from the _sender_name field that
    _load_history attaches (NULL for pre-v4 rows -> "user"
    prefix). Returns the message dict unchanged if it's not a
    user message, or if content is in an unsupported shape.

    This used to be defined twice (once inside the tools-mode
    branch, once inside the rating-mode branch) as closures
    that were byte-for-byte identical. The duplication was a
    hazard: any change to one (e.g. the text-prefix format)
    had to be mirrored to the other, and one of the two was
    always slightly out of date. Now defined once at module
    level.
    """
    if not isinstance(m, dict) or m.get("role") != "user":
        return m
    name = m.get("_sender_name")
    tag = (name.strip() if isinstance(name, str) and name.strip() else "user")
    content = m.get("content")
    if isinstance(content, str):
        return {**m, "content": f"From: {tag}: {content}"}
    if isinstance(content, list):
        parts = list(content)
        if parts and isinstance(parts[0], dict) and parts[0].get("type") == "text":
            parts[0] = {**parts[0], "text": f"From: {tag}: " + parts[0].get("text", "")}
            return {**m, "content": parts}
    return m


async def call_llama(messages, max_tokens=65536, user_text='', thinking_msg=None, use_stream=True, shutdown_event=None, rating_active=False, abort_event=None, bot=None, chat_id=None, current_message_id=None):
    """Call llama.cpp with a tool-calling loop and live reasoning stream.

    thinking_msg: optional Telegram Message to update with reasoning text as it streams
                  (throttled internally). Pass None to skip the live reasoning feed.
    use_stream:    if False, do a plain non-streaming POST (better for vision tasks
                  and other cases where streaming may truncate reasoning).
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
    a captured llama-server response. See also docs/REVIEW-MINIMAX.md §P3-4.
    """
    tools = await fetch_tools_from_llama()
    # custom_weather_tool is now in bot_side_tool_defs below; see "Add our
    # bot-side tool implementations" comment. Keeping the legacy variable
    # name as alias so other call sites still work.
    custom_weather_tool = None  # placeholder, see bot_side_tool_defs
    # Add our bot-side tool implementations (get_weather, donsetch_web_*).
    # get_weather is a local call; donsetch_web_* talk directly to
    # donsetch-http at 127.0.0.1:8765/mcp. We do NOT rely on llama.cpp's
    # own MCP integration — that path has been broken (playwright works
    # but donsetch doesn't load, see llama.cpp #13845 or similar). Adding
    # the tools here means the model can call them via tool_calls and we
    # dispatch in this Python process.
    bot_side_tool_defs = [
        {
            'type': 'function',
            'function': {
                'name': 'get_weather',
                'description': 'Get current weather in a given city. Uses wttr.in, always works, no CAPTCHA. Use this for any questions about current weather, temperature, precipitation, wind.',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'location': {'type': 'string', 'description': 'City name (e.g. "Yalta", "Moscow")'}
                    },
                    'required': ['location']
                }
            }
        },
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
                GROUP_CONTEXT
                + '\n\n## Tool use (tools mode)\n'
                + 'You have access to: get_weather (wttr.in, always works), '
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
                'You are a message classifier. Classify the user message '
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
    last_empty = 0
    final_fallback = None
    prev_calls = []  # detect identical-call loops
    loop_start = time.monotonic()
    LOOP_BUDGET_SEC = 600  # 10 min total wall-clock, then bail regardless of iter count

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
                print(f"[thinking edit] flood; backing off edits for 30s", flush=True)
            else:
                print(f"[thinking edit err] {type(e).__name__}: {e}", flush=True)

    for iteration in range(max_iter):
        # Honour graceful shutdown between iterations. Without this, a SIGTERM
        # mid-call_llama is ignored until the current llama-server request
        # finishes (could be 5-7 min for a hard problem). docker stop
        # SIGKILLs after 10s, so we MUST bail faster than that.
        _sd = shutdown_event if shutdown_event is not None else SHUTDOWN_EVENT
        if _sd is not None and _sd.is_set():
            print(f"[call_llama] shutdown_event set, aborting tool loop at iteration {iteration}", flush=True)
            return '[bot: shutdown requested, aborting tool loop]'
        # Honour per-task abort (Stop button on the thinking message).
        # Distinct from shutdown_event: shutdown is global ("kill the
        # process"), abort is per-message ("skip this one, move on").
        if abort_event is not None and abort_event.is_set():
            print(f"[call_llama] abort_event set, aborting tool loop at iteration {iteration}", flush=True)
            return None
        # --- request body ---
        async with httpx.AsyncClient(timeout=600.0) as client:
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
                    raw = json.dumps(req_body, ensure_ascii=False).encode('utf-8')
                    print(f"[LLAMA] iter=0 payload_bytes={len(raw)} (image bytes masked)", flush=True)
                except Exception as e:
                    print(f"[LLAMA] iter=0 cannot size payload: {e}", flush=True)
            # Log what we're about to send — without the image bytes
            # CRITICAL: build a deep-copy for the log so we don't mutate
            # req_body (the bug we just hit: this replaced real image_url
            # with a truncated fake and llama-server 400'd on it).
            safe_body = copy.deepcopy(req_body)
            if "messages" in safe_body:
                for m in safe_body["messages"]:
                    if isinstance(m.get("content"), list):
                        for item in m["content"]:
                            if isinstance(item, dict) and "image_url" in item:
                                url = item["image_url"].get("url", "")
                                item["image_url"] = {"url": f"data:image/...,[{len(url)} chars]"}
            print(f"[LLAMA] iter={iteration} req_body_keys={list(req_body.keys())} tools={'yes ('+str(len(req_tools))+' defs)' if req_tools else 'no'} msgs_count={len(req_body.get('messages',[]))}", flush=True)
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
                        print(f"[LLAMA]   first user msg content types: {[item.get('type') for item in c0 if isinstance(item, dict)]}", flush=True)
                    else:
                        print(f"[LLAMA]   first user msg content head: {str(c0)[:150]!r}", flush=True)
                    ml = user_msgs[-1]
                    cl = ml.get("content")
                    if isinstance(cl, list):
                        first_part = cl[0] if cl else {}
                        first_text = first_part.get("text", "") if isinstance(first_part, dict) else ""
                        print(f"[LLAMA]   last user msg first text part head: {first_text[:150]!r}", flush=True)
                    else:
                        print(f"[LLAMA]   last user msg content head: {str(cl)[:150]!r}", flush=True)
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
                    payload_bytes = json.dumps(req_body, ensure_ascii=False).encode('utf-8')
                    async with httpx.AsyncClient(
                        timeout=600.0,
                        limits=httpx.Limits(max_keepalive_connections=0, max_connections=1),
                    ) as vclient:
                        r = await vclient.post(
                            f"{LLAMA_URL}/chat/completions",
                            headers={"Authorization": f"Bearer {API_KEY}",
                                     "Content-Type": "application/json"},
                            content=payload_bytes,
                        )
                        r.raise_for_status()
                        data = r.json()
                    msg0 = data["choices"][0]["message"]
                    content = msg0.get("content") or ""
                    reasoning = msg0.get("reasoning_content") or ""
                    non_stream_tool_calls = msg0.get("tool_calls") or []
                    print(f"[LLAMA] iter={iteration} non-stream finish={data['choices'][0].get('finish_reason')} content_chars={len(content)} reasoning_chars={len(reasoning)} tool_calls={len(non_stream_tool_calls)}", flush=True)
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
                    finish_reason = data['choices'][0].get('finish_reason') or 'tool_calls'
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
                        print(f"[LLAMA]   call: {fn.get('name','')}({(fn.get('arguments') or '')[:200]})", flush=True)
                except Exception as e:
                    # r is bound to the httpx Response if we got past
                    # the .post() call; otherwise it's still None
                    # (e.g. JSONDecodeError, ConnectionError before
                    # the response object is constructed).
                    body = r.text[:500] if r is not None else ''
                    print(f"[non-stream err iter={iteration}] {type(e).__name__}: {e}; body={body!r}", flush=True)
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
                            print(
                                f"[call_llama] abort_event set pre-stream "
                                f"iter={iteration}",
                                flush=True,
                            )
                            return None
                        async for line in r.aiter_lines():
                            # Between-chunk abort check. Fires as soon
                            # as a chunk arrives if the user clicked
                            # Stop during the previous chunk's
                            # processing.
                            if abort_event is not None and abort_event.is_set():
                                print(
                                    f"[call_llama] abort_event set between "
                                    f"chunks iter={iteration}",
                                    flush=True,
                                )
                                return None
                            if not line or not line.startswith("data: "):
                                continue
                            payload = line[6:]
                            if payload.strip() == "[DONE]":
                                break
                            try:
                                chunk = json.loads(payload)
                            except json.JSONDecodeError:
                                continue
                            for choice in chunk.get("choices", []):
                                delta = choice.get("delta", {})
                                rc = delta.get("reasoning_content")
                                if rc:
                                    reasoning_buf += rc
                                    await push_thinking(accumulated_reasoning + reasoning_buf)
                                cc = delta.get("content")
                                if cc:
                                    content_buf += cc
                                for tc_delta in delta.get("tool_calls") or []:
                                    idx = tc_delta.get("index", 0)
                                    if idx not in tool_calls_buf:
                                        tool_calls_buf[idx] = {"id": "", "name": "", "arguments": ""}
                                    if tc_delta.get("id"):
                                        tool_calls_buf[idx]["id"] = tc_delta["id"]
                                    fn = tc_delta.get("function") or {}
                                    if fn.get("name"):
                                        tool_calls_buf[idx]["name"] += fn["name"]
                                    if fn.get("arguments"):
                                        tool_calls_buf[idx]["arguments"] += fn["arguments"]
                                if choice.get("finish_reason"):
                                    finish_reason = choice["finish_reason"]
                        # Final flush: ensure the latest reasoning text is on Telegram
                        if reasoning_buf:
                            await push_thinking(accumulated_reasoning + reasoning_buf, force=True)
                except httpx.HTTPError as e:
                    print(f"[stream err iter={iteration}] {type(e).__name__}: {e}; falling back to non-streaming", flush=True)
                    # Fall back to non-streaming request
                    try:
                        async with httpx.AsyncClient(timeout=600.0) as client2:
                            r2 = await client2.post(
                                f"{LLAMA_URL}/chat/completions",
                                headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
                                json={**req_body, "stream": False},
                            )
                            r2.raise_for_status()
                            data = r2.json()
                        msg = data["choices"][0]["message"]
                        tool_calls = msg.get("tool_calls") or []
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
                        print(f"[fallback err iter={iteration}] {type(e2).__name__}: {e2}", flush=True)
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
        print(f"[LLAMA] iter={iteration} finish={finish_reason} content_chars={len(content_buf)} reasoning_chars={len(reasoning_buf)} tool_calls={len(tool_calls)} req_tools={'yes' if req_tools else 'no'}", flush=True)
        for tc in tool_calls:
            print(f"[LLAMA]   call: {tc.get('function',{}).get('name','')}({tc.get('function',{}).get('arguments','')[:200]})", flush=True)

        if not tool_calls:
            if content_buf:
                return content_buf
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
            print(f"[LLAMA] iter={iteration} WARNING: model hallucinated tool_calls={len(tool_calls)} with req_tools=None; aborting tool loop", flush=True)
            if content_buf:
                return content_buf
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
        print(f"[loop] iter={iteration} tool_calls={len(tool_calls)} finish={finish_reason} content_chars={len(content_buf)} reasoning_chars={len(reasoning_buf)}", flush=True)
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
                    print(f"[tool] iter={iteration} REJECTED {fn_name} args too repetitive or too long ({len(raw_args)} chars); asking model to retry", flush=True)
                    msgs.append({
                        'role': 'tool',
                        'tool_call_id': tc.get('id', ''),
                        'content': f'[bot: your tool call for {fn_name} had malformed args (length={len(raw_args)}, possibly duplicate keys or clipped). Please re-emit the tool call with a single, well-formed JSON object.]',
                    })
                    last_empty += 1
                    continue
                try:
                    args = json.loads(raw_args)
                except Exception as e:
                    print(f"[tool] iter={iteration} REJECTED {fn_name} args unparseable: {e!r} (head={raw_args[:120]!r})", flush=True)
                    msgs.append({
                        'role': 'tool',
                        'tool_call_id': tc.get('id', ''),
                        'content': f'[bot: your tool call for {fn_name} had invalid JSON ({e!r}). Please re-emit with a single well-formed JSON object.]',
                    })
                    last_empty += 1
                    continue
                # If parsing yielded {} for a non-empty raw string,
                # the model produced only structural noise (e.g.
                # "{{" or "null" or duplicated empty key). Reject.
                if not args and raw_args.strip() not in ('{}', 'null', '[]'):
                    print(f"[tool] iter={iteration} REJECTED {fn_name} args parsed to empty for raw={raw_args[:120]!r}", flush=True)
                    msgs.append({
                        'role': 'tool',
                        'tool_call_id': tc.get('id', ''),
                        'content': f'[bot: your tool call for {fn_name} parsed to an empty object. Please re-emit with actual parameters.]',
                    })
                    last_empty += 1
                    continue
            else:
                args = {}
            tc_id = tc.get('id', '')
            current_calls.append((fn_name, json.dumps(args, sort_keys=True) if isinstance(args, dict) else str(args)))
            print(f"[tool] iter={iteration} call {fn_name}({args})", flush=True)
            if fn_name == 'react_to_message':
                result = await _execute_react_to_message(
                    args,
                    bot=bot,
                    chat_id=chat_id,
                    default_message_id=current_message_id,
                )
            elif fn_name in CUSTOM_TOOLS:
                result = await CUSTOM_TOOLS[fn_name](args)
            elif fn_name in DONSETCH_TOOLS:
                result = await DONSETCH_TOOLS[fn_name](args)
            else:
                result = f'[tool {fn_name} unavailable in this mode. Bot supports: get_weather, donsetch_web_search, donsetch_web_fetch, donsetch_web_crawl, donsetch_web_screenshot, react_to_message.]'
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

        # If the model asked for a tool we don't have, stop right away — it
        # will keep asking forever otherwise (each iteration costs ~2 min of
        # reasoning and the bot will burn 30+ min before max_iter triggers).
        if saw_unavailable_tool:
            print(f"[loop] iter={iteration} unhandled tool called; aborting", flush=True)
            return (
                f'[bot: the model requested a tool ({tool_calls[0]["function"].get("name","?")}) '
                f'that this bot mode does not support. The tool is not installed in this '
                f'llama-server configuration. Try a different question, /reset, or restart '
                f'the bot if this is unexpected.]'
            )

        # If the model is asking for the same tool+args as in the previous
        # iteration, it has looped — return whatever we have and stop.
        if current_calls == prev_calls:
            print(f"[loop] iter={iteration} same tool calls as previous iter; aborting", flush=True)
            return (
                f'[bot: the model asked for the same tool call twice in a row '
                f'({current_calls[0][0]}). It is stuck in a loop. '
                f'Final answer so far: {final_fallback or "(none)"}]'
            )
        prev_calls = current_calls

        # Hard wall-clock budget so the bot cannot burn 30+ minutes in
        # slow-reasoning loops even when every iteration produces different
        # tool calls (which defeats the identical-call detector above).
        elapsed = time.monotonic() - loop_start
        if elapsed > LOOP_BUDGET_SEC:
            print(f"[loop] iter={iteration} time budget exceeded ({elapsed:.0f}s > {LOOP_BUDGET_SEC}s); aborting", flush=True)
            return (
                f'[bot: time budget exceeded after {int(elapsed/60)} min and {iteration+1} iterations. '
                f'The model is reasoning too slowly for this question. '
                f'Try a simpler question, /reset, or a different model.]'
            )

        if iter_had_real_result:
            last_empty = 0
        else:
            last_empty += 1
            # Hard abort: 3 iterations in a row with no real tool result
            # (either no tools called, or every tool returned a placeholder
            # like "[unavailable]" or "0 results"). Bail with what we have.
            if last_empty >= 3:
                tail = (accumulated_reasoning or reasoning_buf)[-3500:]
                print(f"[loop] iter={iteration} 3 empty iters in a row; aborting", flush=True)
                return (
                    f'_(модель 3 итерации подряд не получила полезных данных от инструментов; '
                    f'останавливаю, чтобы не сжигать токены. '
                    f'finish_reason={finish_reason}, reasoning_chars={len(accumulated_reasoning + reasoning_buf)})\n\n'
                    f'{tail}'
                )
            if last_empty >= 2:
                msgs.append({
                    'role': 'user',
                    'content': (
                        'The previous tool calls returned no useful data. '
                        'Please stop searching and give your final answer now — '
                        'if you cannot find the requested information, say so honestly '
                        'and suggest where the user can find it themselves.'
                    ),
                })
                # Final non-streamed summarize call (no tools — we want a plain answer)
                async with httpx.AsyncClient(timeout=600.0) as client3:
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
                msg3 = data3['choices'][0]['message']
                return msg3.get('content') or (
                    '[bot: search returned no useful data. '
                    'For live data (flight prices, news, stocks), please use a direct service '
                    'like Aviasales, Google Flights, or your browser.]'
                )

    if final_fallback:
        return final_fallback
    return ('[bot: exceeded tool-calling iteration limit. '
            'Please try a simpler question, /reset, or a different model.]')


async def transcribe_voice(voice_bytes):
    async with httpx.AsyncClient(timeout=120.0) as client:
        files = {'file': ('voice.ogg', bytes(voice_bytes), 'audio/ogg')}
        data = {'language': 'ru'}
        response = await client.post(
            f"{WHISPER_URL}/v1/audio/transcriptions",
            files=files, data=data
        )
        response.raise_for_status()
        return response.json()['text']



# === Backward-compat aliases (used by tests and old call sites) ===
async def call_llama_compat(*args, **kwargs):
    return await call_llama(*args, **kwargs)
