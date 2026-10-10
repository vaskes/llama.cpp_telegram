import os
import json
import time
import base64
import copy
import signal
import tempfile
import urllib.parse
import socket
import asyncio
import httpx
from typing import Optional
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

# === Force IPv4: docker container has no IPv6 routing, but Telegram DNS returns AAAA first ===
_orig_getaddrinfo = socket.getaddrinfo
def _ipv4_only_getaddrinfo(host, *args, **kwargs):
    """Filter out IPv6 results — Telegram bot lib otherwise tries IPv6 and gets Network unreachable."""
    results = _orig_getaddrinfo(host, *args, **kwargs)
    if not results:
        return results
    # If any family explicitly requested, pass through; else filter to IPv4
    family = kwargs.get('family', socket.AF_UNSPEC)
    if family == socket.AF_UNSPEC:
        return [r for r in results if r[0] == socket.AF_INET] or results
    return results
socket.getaddrinfo = _ipv4_only_getaddrinfo

# Конфигурация.
#   BOT_TOKEN   — required, no default (без токена бот не работает).
#   LLAMA_URL   — default http://localhost:8080/v1 (стандартный порт llama-server).
#   WHISPER_URL — default http://localhost:8000 (стандартный порт whisper-api).
#   API_KEY     — default sk-no-key (open-source конвенция, не токен;
#                 llama-server без --api-key принимает любой непустой bearer).
#   MODEL       — если пусто, бот спросит /v1/models у llama-server при старте
#                 и возьмёт первое доступное имя. Никаких захардкоженных
#                 имён конкретных моделей в коде.
BOT_TOKEN = os.environ.get('BOT_TOKEN')
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN env var is required but not set")
LLAMA_URL = os.environ.get('LLAMA_URL', 'http://localhost:8080/v1')
WHISPER_URL = os.environ.get('WHISPER_URL', 'http://localhost:8000')
API_KEY = os.environ.get('API_KEY', 'sk-no-key')
MODEL = os.environ.get('MODEL', '').strip()

# Если MODEL не задан — спросим у llama-server, что реально загружено.
# Это автоматически подстраивается под любую конфигурацию и не
# привязывает репо к конкретной модели.
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



# === Security: whitelist ===
# ALLOWED_USER_IDS — comma-separated numeric Telegram user IDs, e.g. "123456789,987654321"
# ALLOWED_USERNAMES — comma-separated Telegram usernames (without @), case-insensitive
# Both empty = LOCKDOWN (bot rejects everyone). Set at least one to allow access.
ALLOWED_USER_IDS_RAW = os.environ.get('ALLOWED_USER_IDS', '').strip()
ALLOWED_USERNAMES_RAW = os.environ.get('ALLOWED_USERNAMES', '').strip()
ALLOWED_USER_IDS = {int(x) for x in ALLOWED_USER_IDS_RAW.split(',') if x.strip().isdigit()}
ALLOWED_USERNAMES = {x.lstrip('@').lower() for x in ALLOWED_USERNAMES_RAW.split(',') if x.strip()}
if not ALLOWED_USER_IDS and not ALLOWED_USERNAMES:
    LOCKDOWN = True
    print('[SECURITY] ALLOWED_USER_IDS and ALLOWED_USERNAMES both empty -> LOCKDOWN (reject all).', flush=True)
else:
    LOCKDOWN = False
    print(f'[SECURITY] whitelist: {len(ALLOWED_USER_IDS)} ids, {len(ALLOWED_USERNAMES)} usernames', flush=True)
    # Username-based access is fragile: users can change their @username
    # at any time and silently lose access. Prefer IDs. Warn if usernames
    # are configured without any IDs to anchor on.
    if ALLOWED_USERNAMES and not ALLOWED_USER_IDS:
        print('[SECURITY] WARNING: ALLOWED_USERNAMES is set but ALLOWED_USER_IDS is empty. '
              'Username-based access can break if a user changes their @username. '
              'Prefer numeric IDs (find yours via @userinfobot or by reading '
              '"rejected id=..." in the logs).', flush=True)


# === Size limits ===
# Without these, a single user sending a 50-MP photo or a 500-MB PDF
# could OOM the bot process. We reject upfront, before downloading.
MAX_PHOTO_BYTES = int(os.environ.get('MAX_PHOTO_BYTES', '10000000'))   # 10 MB
MAX_DOC_BYTES = int(os.environ.get('MAX_DOC_BYTES', '5000000'))       # 5 MB
MAX_VOICE_BYTES = int(os.environ.get('MAX_VOICE_BYTES', '20000000'))  # 20 MB
MAX_VIDEO_NOTE_BYTES = int(os.environ.get('MAX_VIDEO_NOTE_BYTES', '50000000'))  # 50 MB
# Voice messages are typically <1 MB and capped client-side at 20 MB.
# Video notes (round video) go through the same handler via filters.AUDIO
# and can be 5-50 MB; a separate, higher cap keeps them working without
# opening the door to a 1 GB upload.


def is_authorized(update: Update) -> bool:
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

# Conversation history lives in SQLite (see `store` below). The
# previous in-memory `conversations = {}` dict was removed when
# sub-talks were introduced — see docs/CALL_LLAMA.md §7 for the
# rationale.

# Tools the bot does NOT execute (security or not implemented)
DISABLED_TOOLS = {
    'read_file', 'write_file', 'edit_file', 'exec_shell_command',
    'file_glob_search', 'grep_search', 'get_info',
    'playwright_browser_close', 'playwright_browser_resize',
    'playwright_browser_console_messages', 'playwright_browser_handle_dialog',
    'playwright_browser_evaluate', 'playwright_browser_file_upload',
    'playwright_browser_drop', 'playwright_browser_find',
    'playwright_browser_fill_form', 'playwright_browser_press_key',
    'playwright_browser_type', 'playwright_browser_navigate',
    'playwright_browser_navigate_back', 'playwright_browser_network_requests',
    'playwright_browser_network_request', 'playwright_browser_run_code_unsafe',
    'playwright_browser_take_screenshot', 'playwright_browser_snapshot',
    'playwright_browser_click', 'playwright_browser_drag',
    'playwright_browser_hover', 'playwright_browser_select_option',
    'playwright_browser_tabs', 'playwright_browser_wait_for',
}

# Кеш tools (загружаются один раз)
_TOOLS_CACHE = None
# Module-level shutdown signal. Set by main._run() when SIGINT/SIGTERM fires.
# call_llama() reads it between iterations to bail out fast (faster than
# docker stop's 10s SIGKILL grace). Kept as a module global because the
# polling loop does not have a way to pass per-update context into the
# PTB handlers; threading it through handler signatures would touch
# every dispatcher, which is out of scope for the current review wave.
SHUTDOWN_EVENT: Optional[asyncio.Event] = None

# Persistent conversation store. SQLite at $CONVERSATIONS_DB (default
# /app/data/conversations.db, bind-mounted from ./data on the host
# via docker-compose.yml). All sub-talk and message I/O goes through
# this single object. Sync API; handlers wrap calls in
# asyncio.to_thread() so the DB never blocks the event loop.
import storage  # local module; safe because storage has no top-level
                 # I/O at import time
DB_PATH = os.environ.get('CONVERSATIONS_DB', '/app/data/conversations.db')
CONTEXT_MESSAGES = int(os.environ.get('CONTEXT_MESSAGES', '20'))
store = storage.Storage(DB_PATH)
print(f"[storage] SQLite at {DB_PATH} (context window: {CONTEXT_MESSAGES} msgs)", flush=True)


async def fetch_tools_from_llama():
    """Получить список tools с llama-server и отфильтровать доступные.

    Cold-start retry: llama-server may take a few seconds after `docker
    compose up` to bind the /tools endpoint. Three attempts with 1s/2s/4s
    backoff before giving up. Caches the result so we only fetch once.
    """
    global _TOOLS_CACHE
    if _TOOLS_CACHE is not None:
        return _TOOLS_CACHE
    last_err = None
    for attempt, backoff in enumerate((1.0, 2.0, 4.0), start=1):
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                r = await client.get(f"{LLAMA_URL.replace('/v1', '')}/tools")
                r.raise_for_status()
                all_tools = r.json()
            openai_tools = []
            for t in all_tools:
                name = t.get('tool', '')
                if name in DISABLED_TOOLS:
                    continue
                defn = t.get('definition', {}).get('function', {})
                if not defn:
                    continue
                openai_tools.append({
                    'type': 'function',
                    'function': {
                        'name': defn.get('name', name),
                        'description': defn.get('description', '')[:1500],
                        'parameters': defn.get('parameters', {'type': 'object', 'properties': {}}),
                    }
                })
            _TOOLS_CACHE = openai_tools
            print(f"[tools] loaded {len(openai_tools)} enabled tools (from {len(all_tools)} total) on attempt {attempt}", flush=True)
            return openai_tools
        except Exception as e:
            last_err = e
            print(f"[tools] attempt {attempt}/3 failed: {type(e).__name__}: {e}", flush=True)
            if attempt < 3:
                await asyncio.sleep(backoff)
    print(f"[tools] giving up after 3 attempts, last error: {last_err}", flush=True)
    return []


# Keywords (RU + EN) that signal the user actually wants a tool call.
# If the user message contains any of these, we expose tools; otherwise
# we send a plain system prompt and skip the tool definitions so Ornith
# just answers in a single turn (verified: 1 turn no tools = 17s and a
# clean response; 3 turns with tools = 30s+ stuck in tool_calls loop).
# === Tool availability ===
#
# History: the bot used to gate tool availability on a keyword
# filter (`_TOOL_KEYWORDS` / `_detect_tool_intent`) so that simple
# "hi" / "thanks" messages did not pay the cost of having the
# model see the full tool definitions. In practice the keyword
# list was too narrow: a user asking "what is the current price
# of Bitcoin?" or "who won the match last night?" would get
# hallucinated answers because the model had no tools and no
# honest way to say "I don't have fresh data".
#
# New policy: tools are ALWAYS passed to the model, except when
# the bot is in rating mode (RATING_MODE=1 + group + not muted).
# In rating mode the LLM is acting as a classifier and must
# output a structured `[[TYPE:...]]` response; tool calls
# would be inappropriate noise. In all other modes, the LLM
# decides for itself whether to call a tool. A greeting or
# a chat question will simply not trigger any tool call; that
# is the model's correct behaviour, not a bug.
#
# The cost of always-on tools is a small constant token overhead
# per request (the tool definitions are ~500 tokens). We accept
# that cost in exchange for correct behaviour on arbitrary
# user queries.
#
# _detect_tool_intent is kept as a no-op for callers that may
# still reference it (it's harmless and removed next refactor).
def _detect_tool_intent(text: str) -> bool:  # pragma: no cover
    """Legacy: keyword-based tool gate. Always True now; tools
    are passed unconditionally in non-rating mode. See comment
    above for rationale.
    """
    return True


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


CUSTOM_TOOLS = {
    'get_weather': get_weather,
}

# === Donsetch-http MCP client ===
# Donsetch replaces the old SearXNG-based stack (which is CAPTCHA-blocked on cloud IPs).
# It exposes 4 tools via MCP/JSON-RPC on http://localhost:8765/mcp. llama-server prefixes
# them with `donsetch_` (e.g. `donsetch_web_search`); we strip that prefix when calling.

DONSETCH_URL = os.environ.get('DONSETCH_URL', 'http://localhost:8765/mcp')
_donsetch_session_id = None
_donsetch_session_lock = asyncio.Lock()


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


async def call_llama(messages, max_tokens=65536, user_text='', thinking_msg=None, use_stream=True, shutdown_event=None, rating_active=False, abort_event=None):
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
    ]
    all_tools = tools + bot_side_tool_defs

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
    # one. The LLM sees its first system message; both are honored.
    if rating_active:
        messages = [
            {"role": "system", "content": RATING_RULES}
        ] + messages
    if use_tools:
        sys_prompt = {
            'role': 'system',
            'content': (
                'You are a helpful assistant with access to tools. '
                'Use them whenever the user asks about something '
                'requiring fresh data, real-world facts you cannot '
                'be sure about, weather, news, prices, sports results, '
                'or any web content. '
                'For weather: use get_weather (wttr.in, always works). '
                'For web: use donsetch_web_search / donsetch_web_fetch / '
                'donsetch_web_crawl / donsetch_web_screenshot. '
                'If a tool returns no useful data, say so honestly and '
                'suggest where the user can find the info themselves. '
                'Do NOT keep retrying the same query with variations. '
                'After getting a tool result, give a clear, concise '
                'answer in the user\'s language. '
                'If no tool is needed (greetings, opinions, math, code '
                'review, chitchat), just answer directly — do not call '
                'a tool unnecessarily.'
            )
        }
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
            return '__ABORTED__'
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
                # On first iter, also print the first user message structure
                for m in req_body.get("messages", []):
                    if m.get("role") == "user":
                        c = m.get("content")
                        if isinstance(c, list):
                            print(f"[LLAMA]   first user msg content types: {[item.get('type') for item in c if isinstance(item, dict)]}", flush=True)
                        else:
                            print(f"[LLAMA]   first user msg content head: {str(c)[:150]!r}", flush=True)
                        break
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
                    print(f"[LLAMA] iter={iteration} non-stream finish={data['choices'][0].get('finish_reason')} content_chars={len(content)} reasoning_chars={len(reasoning)} tool_calls={len(msg0.get('tool_calls') or [])}", flush=True)
                    if reasoning and thinking_msg is not None:
                        await push_thinking(accumulated_reasoning + reasoning, force=True)
                    return content
                except Exception as e:
                    # r is bound to the httpx Response if we got past
                    # the .post() call; otherwise it's still None
                    # (e.g. JSONDecodeError, ConnectionError before
                    # the response object is constructed).
                    body = r.text[:500] if r is not None else ''
                    print(f"[non-stream err iter={iteration}] {type(e).__name__}: {e}; body={body!r}", flush=True)
                    return f'[llama-server request failed: {e}]'
            try:
                async with client.stream("POST", f"{LLAMA_URL}/chat/completions",
                                          headers={"Authorization": f"Bearer {API_KEY}",
                                                   "Content-Type": "application/json"},
                                          json=req_body) as r:
                    r.raise_for_status()
                    # Manual line iteration with abort-aware waits.
                    # The default `async for line in r.aiter_lines()`
                    # blocks inside __anext__() until a chunk arrives,
                    # which means an abort event set BEFORE the first
                    # chunk is never seen — the user clicks Stop but
                    # the bot keeps waiting on the first line.
                    #
                    # We wrap each __anext__() in asyncio.wait_for with
                    # a short timeout (300ms). If the timeout fires,
                    # we check the abort event: if set, return the
                    # sentinel; otherwise loop and try again. This
                    # gives a worst-case abort latency of 300ms
                    # between the click and the response, even when
                    # the LLM is slow to produce the first chunk.
                    line_iter = r.aiter_lines()
                    while True:
                        try:
                            line = await asyncio.wait_for(
                                line_iter.__anext__(),
                                timeout=0.3,
                            )
                        except asyncio.TimeoutError:
                            if abort_event is not None and abort_event.is_set():
                                print(
                                    f"[call_llama] abort_event set while "
                                    f"waiting for first/next chunk "
                                    f"iter={iteration}",
                                    flush=True,
                                )
                                return '__ABORTED__'
                            continue
                        except StopAsyncIteration:
                            break
                        # Got a line. Honour the abort check between
                        # chunks too (in case a fast LLM produces many
                        # chunks between 300ms ticks).
                        if abort_event is not None and abort_event.is_set():
                            print(
                                f"[call_llama] abort_event set between "
                                f"chunks iter={iteration}",
                                flush=True,
                            )
                            return '__ABORTED__'
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
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except Exception:
                args = {}
            tc_id = tc.get('id', '')
            current_calls.append((fn_name, json.dumps(args, sort_keys=True) if isinstance(args, dict) else str(args)))
            print(f"[tool] iter={iteration} call {fn_name}({args})", flush=True)
            if fn_name in CUSTOM_TOOLS:
                result = await CUSTOM_TOOLS[fn_name](args)
            elif fn_name in DONSETCH_TOOLS:
                result = await DONSETCH_TOOLS[fn_name](args)
            else:
                result = f'[tool {fn_name} unavailable in this mode. Bot supports: get_weather, donsetch_web_search, donsetch_web_fetch, donsetch_web_crawl, donsetch_web_screenshot.]'
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


EMPTY_RESPONSE_FALLBACK = (
    '[model returned an empty response. This usually means the LLM '
    'spent all its tokens on internal reasoning without writing a reply. '
    'Try /reset to clear context, or switch to a less reasoning-heavy model.]'
)


async def send_reply(update: Update, text: str):
    """Reply to a Telegram message, falling back to a helpful message if the model returned nothing.

    Telegram rejects empty messages with `Message text is empty`. Some models
    (Ornith-Uncensored in particular) spend the whole token budget on reasoning
    and return content='' for short prompts. We don't want the user to see a
    cryptic API error in that case.

    Also records the (chat_id, user_message_id) → bot_message_id mapping
    so a later user edit can trigger delete+reprocess.
    """
    text = (text or '').strip()
    if not text:
        text = EMPTY_RESPONSE_FALLBACK
    sent = await _reply(update, text)
    # Record the bot reply so an edit on the user message can replace it.
    # update.message.message_id is the user's message; sent.message_id is
    # the bot's reply. The mapping lets us delete the bot reply and
    # process the edited user message as a fresh request.
    try:
        user_msg = update.message
        if user_msg is not None and sent is not None:
            chat_id = update.effective_chat.id if update.effective_chat else None
            if chat_id is not None:
                _bot_replies[(chat_id, user_msg.message_id)] = sent.message_id
    except Exception as e:
        # Never let tracking break a reply.
        print(f"[send_reply] tracking failed: {type(e).__name__}: {e!r}", flush=True)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await reject_if_unauthorized(update, context):
        return
    await _reply(update, 
        '🤖 **LlamaBot v2 запущен!**\n\n'
        'Я могу:\n'
        '• Отвечать на вопросы (с tool-calling)\n'
        '• Искать в интернете 🌐\n'
        '• Узнавать погоду (wttr.in) ☀️\n'
        '• Анализировать изображения (отправьте фото)\n'
        '• Расшифровывать голосовые 🎤\n'
        '• Читать документы (TXT, PDF)\n\n'
        'Команды: /reset, /stats'
    )


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Clear the messages in the current sub-talk / topic."""
    # === Group mode: clear history in the current topic ===
    if _is_group_chat(update):
        chat_id = update.effective_chat.id
        # Use "general" sentinel for the General topic so the DB
        # key is consistent with _route_to_thread.
        msg_thread_id = update.message.message_thread_id
        thread_id = str(msg_thread_id) if msg_thread_id is not None else _general_thread_id()
        n = await asyncio.to_thread(
            store.delete_thread, chat_id, thread_id
        )
        # Look up the topic name for a friendlier message
        if msg_thread_id is not None:
            entry = await asyncio.to_thread(
                store.find_known_topic_by_id, chat_id, msg_thread_id
            )
            topic_label = f"#{msg_thread_id} — {entry['name']}" if entry else f"#{msg_thread_id}"
        else:
            topic_label = "General"
        await _reply(update,
            f'🗑 Cleared {n} message(s) in {topic_label}.\n'
            f'The Telegram topic itself is unchanged — only the '
            f"bot's memory of past messages in it is wiped.\n"
            f'In private mode the active sub-talk is re-created; in '
            f'group mode the topic is owned by Telegram and just '
            f'loses its conversation history.'
        )
        return
    if await reject_if_unauthorized(update, context):
        return
    user_id = update.effective_user.id
    thread_id = await _resolve_active(user_id)
    n = await asyncio.to_thread(store.delete_thread, user_id, thread_id)
    # delete_thread removes the thread row too, so re-create it
    # (empty) and keep it active. The user can still /subs to see it.
    await asyncio.to_thread(store.create_thread, user_id, thread_id)
    await asyncio.to_thread(store.set_active_thread, user_id, thread_id)
    await _reply(update,
        f'🔄 Cleared {n} message(s) in sub-talk "{thread_id}".\n'
        f'Sub-talk is preserved. Use /delsub to remove the whole thread.'
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the full command reference. Same content as the bot menu
    but with examples and the sub-talk workflow explained in prose."""
    if await reject_if_unauthorized(update, context):
        return
    # Build a ReplyKeyboardMarkup with the most common actions as
    # one-tap buttons. This is a *custom keyboard* (replaces the
    # input area) — it stays in the chat until the user dismisses
    # it. Useful for first-time users; they tap to discover.
    from telegram import KeyboardButton, ReplyKeyboardMarkup
    rows = [
        [KeyboardButton("/here"), KeyboardButton("/subs")],
        [KeyboardButton("/newsub research"), KeyboardButton("/sub research")],
        [KeyboardButton("/reset"), KeyboardButton("/stats")],
    ]
    await _reply(update, 
        '🤖 **LlamaBot — help**\n\n'
        '**Sub-talks** — named conversation threads. The bot keeps a '
        'separate history for each one and only the active thread is in '
        'the model\'s context window.\n\n'
        '**Workflow:**\n'
        '1. /newsub <name>  → create a new sub-talk and switch to it\n'
        '2. Send messages — they go into the active sub-talk\n'
        '3. /sub <name>     → switch to a different sub-talk\n'
        '4. /here           → see what\'s in the current sub-talk\n'
        '5. /subs           → list all sub-talks (with tap-to-switch buttons)\n'
        '6. /delsub <name>  → remove a sub-talk and its history\n\n'
        '**Other commands:**\n'
        '• /start  — welcome + feature list\n'
        '• /reset  — clear messages in the current sub-talk (keeps the thread)\n'
        '• /stats  — model + tool count + sub-talk list\n'
        '• /help   — this message\n\n'
        'Send any photo / voice / document and it lands in the active '
        'sub-talk. Use /sub to switch.',
        reply_markup=ReplyKeyboardMarkup(
            keyboard=rows, resize_keyboard=True, one_time_keyboard=False,
        ),
    )


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # === Group mode: show topic count + total stored messages ===
    if _is_group_chat(update):
        chat_id = update.effective_chat.id
        thread_id = str(update.message.message_thread_id)
        topics = await asyncio.to_thread(
            store.list_known_topics, chat_id
        )
        # Per-topic message count from local DB
        all_subs = await asyncio.to_thread(store.list_threads, chat_id)
        msg_total = sum(s["msg_count"] for s in all_subs)
        cur_count = next(
            (s["msg_count"] for s in all_subs if s["thread_id"] == thread_id),
            0
        )
        await _reply(update,
            f'📊 Group stats\n'
            f' • Topics (bot-known): {len(topics)}\n'
            f' • Current topic: #{update.message.message_thread_id} '
            f'({cur_count} stored message(s))\n'
            f' • Total stored messages across topics: {msg_total}'
        )
        return
    if await reject_if_unauthorized(update, context):
        return
    user_id = update.effective_user.id
    subs = await asyncio.to_thread(store.list_threads, user_id)
    thread_id = await _resolve_active(user_id)
    current_count = next((s["msg_count"] for s in subs if s["thread_id"] == thread_id), 0)
    tools = await fetch_tools_from_llama()
    sub_lines = "\n".join(
        f'  → "{s["thread_id"]}" ({s["msg_count"]} msgs)' if s["thread_id"] == thread_id
        else f'    "{s["thread_id"]}" ({s["msg_count"]} msgs)'
        for s in subs
    ) or "    (no sub-talks yet)"
    await _reply(update, 
        f'📊 **Статистика:**\n'
        f'Активный sub-talk: "{thread_id}" — {current_count} сообщений\n'
        f'Всего sub-talks: {len(subs)}\n'
        f'{sub_lines}\n'
        f'Модель: {MODEL}\n'
        f'Tools: {len(tools)} from llama-server + 1 weather (custom)'
    )


# === Sub-talk commands ===
# A sub-talk is a named conversation thread. Each user has their own
# set of sub-talks (no sharing across users). The "active" sub-talk
# is per-user; new text/photo/voice/document messages go into the
# active one. See docs/CALL_LLAMA.md and docs/ARCHITECTURE.md for the
# design rationale.

import re as _re

# Sub-talk names: 1-32 chars, must start with a letter or digit,
# may contain letters, digits, dash, underscore, dot. Spaces, slashes,
# and other shell-meta characters are not allowed so the name is safe
# to log and never collides with command arguments.
# Sub-talk names: 1-32 chars, no whitespace. We previously used
# the ASCII-only regex ^[A-Za-z0-9][A-Za-z0-9_.\-]{0,31}$ but
# Russian users want to call their sub-talks "Болталка", so we
# accept any non-whitespace string instead. The limit is 32 chars
# to keep storage keys short and inline-keyboard callback_data
# under 64 bytes.
_SUBTALK_NAME_RE = _re.compile(r'^[^\s]{1,32}$')

# Forum topic names: 1-128 chars (Telegram's limit), no whitespace.
# Forum topic names are also used as the display name in the topic
# header, so we don't enforce a stricter charset here.
_TOPIC_NAME_RE = _re.compile(r'^[^\s]{1,128}$')


def _parse_subtalk_arg(text: str) -> str | None:
    """Extract a single sub-talk name from a /command arg string.

    Returns the first whitespace-separated token, or None if absent
    or invalid. For sub-talks we use the strict 32-char limit; for
    forum-topic names (group mode) see _parse_topic_arg() which
    accepts up to 128 chars.
    """
    parts = text.strip().split(None, 1)
    if len(parts) < 2:
        return None
    name = parts[1].strip()
    if not _SUBTALK_NAME_RE.match(name):
        return None
    return name


def _parse_topic_arg(text: str) -> str | None:
    """Like _parse_subtalk_arg but for Telegram forum topic names.

    Telegram's createForumTopic accepts topic names up to 128 chars,
    any non-empty non-whitespace string. Used in group mode by
    /newsub, /delsub.
    """
    parts = text.strip().split(None, 1)
    if len(parts) < 2:
        return None
    name = parts[1].strip()
    if not _TOPIC_NAME_RE.match(name):
        return None
    return name


async def _reply_active(update: Update, user_id: int) -> str:
    """Returns the user's active sub-talk name (resolving to 'main' default)."""
    return await asyncio.to_thread(store.get_active_thread, user_id) or 'main'


async def cmd_newsub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # === Group mode: create a forum topic via createForumTopic ===
    if _is_group_chat(update):
        topic_name = _parse_topic_arg(update.message.text)
        if not topic_name:
            await _reply(update,
                '❌ Usage: /newsub <name>\n'
                'Name: 1-128 chars, no whitespace. Any UTF-8 ok.\n'
                'Example: /newsub research'
            )
            return
        try:
            topic = await context.bot.create_forum_topic(
                chat_id=update.effective_chat.id,
                name=topic_name,
            )
        except Exception as e:
            print(f"[ERR newsub-group] {type(e).__name__}: {e}", flush=True)
            await _reply(update, f'❌ createForumTopic failed: {e}')
            return
        # Record the topic in our local index so /subs and /delsub
        # can find it later. Telegram Bot API does NOT expose a
        # "list topics" method to bots, so the bot's local DB is
        # the only way to enumerate topics we know about.
        await asyncio.to_thread(
            store.add_known_topic,
            update.effective_chat.id,
            topic.message_thread_id,
            topic_name,
        )
        await _reply(update,
            f'✅ Created topic "{topic_name}" (id={topic.message_thread_id}).\n'
            f'Switch to it in the sidebar to start the conversation.'
        )
        return
    # === Private mode: sub-talk in DB ===
    thread_id = _parse_subtalk_arg(update.message.text)
    if not thread_id:
        await _reply(update,
            '❌ Usage: /newsub <name>\n'
            'Name: 1-32 chars, no whitespace. Any UTF-8 ok.\n'
            'Example: /newsub research'
        )
        return
    if await reject_if_unauthorized(update, context):
        return
    user_id = update.effective_user.id
    created = await asyncio.to_thread(store.create_thread, user_id, thread_id)
    if not created:
        # Already exists — just switch to it.
        await asyncio.to_thread(store.set_active_thread, user_id, thread_id)
        await _reply(update,
            f'ℹ Sub-talk "{thread_id}" already existed. Now active.'
        )
        return
    await asyncio.to_thread(store.set_active_thread, user_id, thread_id)
    await _reply(update,
        f'✅ Created sub-talk "{thread_id}" — now active.\n'
        f'Send any message to add to this thread. '
        f'Use /sub to switch, /subs to list, /delsub to remove.'
    )


async def cmd_sub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # === Group mode: navigate-by-name is not really a thing in
    # Telegram forum topics (the user just taps the topic in the
    # sidebar). Best we can do is show the current topic. ===
    if _is_group_chat(update):
        await _reply(update,
            f'📍 You are in topic #{update.message.message_thread_id}.\n\n'
            f'In group mode, just tap a topic in the sidebar to switch —\n'
            f'there is no command to "navigate" between topics programmatically.'
        )
        return
    if await reject_if_unauthorized(update, context):
        return
    user_id = update.effective_user.id
    thread_id = _parse_subtalk_arg(update.message.text)
    if not thread_id:
        # /sub with no arg: show current
        current = await _reply_active(update, user_id)
        await _reply(update,
            f'You are in sub-talk "{current}".\n'
            f'Use /sub <thread_id> to switch, /subs to list all.'
        )
        return
    existing = await asyncio.to_thread(store.get_thread, user_id, thread_id)
    if existing is None:
        # Auto-create on /sub to a non-existent thread_id — friendlier than
        # asking the user to /newsub first.
        await asyncio.to_thread(store.create_thread, user_id, thread_id)
    await asyncio.to_thread(store.set_active_thread, user_id, thread_id)
    await _reply(update, f'✅ Switched to sub-talk "{thread_id}".')


async def cmd_here(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # === Group mode: show the current topic's name + last snippet ===
    if _is_group_chat(update):
        chat_id = update.effective_chat.id
        msg_thread_id = update.message.message_thread_id
        # In the General topic message_thread_id is None — use the
        # "general" sentinel so the DB key is consistent.
        thread_id = str(msg_thread_id) if msg_thread_id is not None else _general_thread_id()
        # Look up the topic name. In the General topic (or any
        # topic the bot did not create), the name is unknown —
        # show the id alone.
        if msg_thread_id is not None:
            entry = await asyncio.to_thread(
                store.find_known_topic_by_id, chat_id, msg_thread_id
            )
            if entry:
                topic_label = f"#{msg_thread_id} — {entry['name']}"
            else:
                topic_label = f"#{msg_thread_id} (not in known_topics)"
        else:
            topic_label = "General"
        last = await asyncio.to_thread(store.get_last_message, chat_id, thread_id)
        if last is None:
            snippet = '(empty)'
        else:
            try:
                content = json.loads(last["content"])
                text = content.get("text", "")
                if len(text) > 200:
                    text = text[:200] + "…"
                role_emoji = "🧑" if last["role"] == "user" else "🤖"
                snippet = f'{role_emoji} {text}' if text else '(non-text message)'
            except Exception:
                snippet = '(unreadable)'
        await _reply(update,
            f'📍 Current topic: {topic_label}\n'
            f'Last message in this topic: {snippet}'
        )
        return
    if await reject_if_unauthorized(update, context):
        return
    user_id = update.effective_user.id
    current = await _reply_active(update, user_id)
    # Also show the thread_id's last message snippet so the user
    # remembers what they were doing here.
    last = await asyncio.to_thread(store.get_last_message, user_id, current)
    if last is None:
        snippet = '(empty)'
    else:
        try:
            content = json.loads(last["content"])
            text = content.get("text", "")
            if len(text) > 200:
                text = text[:200] + "…"
            role_emoji = "🧑" if last["role"] == "user" else "🤖"
            snippet = f'{role_emoji} {text}' if text else '(non-text message)'
        except Exception:
            snippet = "(unreadable)"
    await _reply(update,
        f'📍 Current sub-talk: "{current}"\n'
        f'Last message: {snippet}\n\n'
        f'Use /subs to list, /sub <name> to switch, /newsub <name> to create.'
    )


async def cmd_subs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # === Group mode: list known topics from local DB ===
    # Telegram Bot API does NOT expose a "list all forum topics"
    # method to bots, so we list from our local known_topics table
    # (populated by /newsub). Topics created outside the bot
    # (manually in Telegram UI, or by another bot) won't appear
    # here. The Telegram sidebar is the authoritative list.
    if _is_group_chat(update):
        topics = await asyncio.to_thread(
            store.list_known_topics, update.effective_chat.id
        )
        if not topics:
            await _reply(update,
                'No topics yet. Use /newsub <name> to create one.\n'
                '\n'
                'Note: topics you created via /newsub appear here.\n'
                'Topics created directly in Telegram UI are not\n'
                'enumerated by the bot (Telegram does not expose a\n'
                '"list topics" API to bots).'
            )
            return
        cur = update.message.message_thread_id
        lines = ['📚 **Topics in this group** (active marked ✅):\n']
        for t in topics:
            mark = '✅ ' if t["message_thread_id"] == cur else '  '
            lines.append(
                f'{mark}#{t["message_thread_id"]} — {t["name"]}'
            )
        await _reply(update, '\n'.join(lines))
        return
    if await reject_if_unauthorized(update, context):
        return
    user_id = update.effective_user.id
    subs = await asyncio.to_thread(store.list_threads, user_id)
    active = await _reply_active(update, user_id)
    if not subs:
        await _reply(update,
            'You have no sub-talks yet. Send any message to start '
            'the default "main" sub-talk, or /newsub <name> to create one.'
        )
        return
    # Build inline-keyboard rows. One button per sub-talk — tap to
    # switch. Active sub-talk gets a checkmark prefix. Buttons send
    # the /sub <name> command as a callback so the user does not
    # need to type it.
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    buttons = []
    for s in subs:
        label = f'{"✅ " if s["thread_id"] == active else "↔️ "}{s["thread_id"]}'
        # callback_data must be <=64 bytes; thread_id names already
        # match /^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$/ so this is safe.
        buttons.append(
            [InlineKeyboardButton(label, callback_data=f"sub:{s['thread_id']}")]
        )
    # New thread / cancel row
    # No "New sub-talk" button: the previous one triggered a
    # ForceReply hint that the user had to then type the name
    # into, but the bot would just process it as a regular
    # question. Better to just use /newsub directly. See
    # cmd_callback's "newsub:prompt" branch for the redirect
    # message.
    await _reply(update,
        f'📚 **Your sub-talks** (active marked ✅)\n\n'
        f'Tap a button to switch. Use /delsub <name> to remove one.',
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def cmd_delsub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # === Group mode: look up the topic in known_topics, then deleteForumTopic ===
    # Telegram Bot API does NOT expose a "list all topics" method to
    # bots, so we look up the topic by name (or numeric id) in our
    # local known_topics index. The index is populated by /newsub.
    if _is_group_chat(update):
        arg = _parse_topic_arg(update.message.text)
        if not arg:
            await _reply(update,
                '❌ Usage: /delsub <name>   (or /delsub <id>)\n'
                'Removes the topic AND all its messages. Cannot be undone.\n'
                'Names match the /subs list; numeric ids also work.'
            )
            return
        target_id = None
        target_name = None
        # Numeric id? Direct SQL lookup (no Python-side scan).
        if arg.isdigit():
            tid = int(arg)
            entry = await asyncio.to_thread(
                store.find_known_topic_by_id,
                update.effective_chat.id,
                tid,
            )
            if entry is None:
                await _reply(update, f'❌ No topic with id={arg} in this group.')
                return
            target_id = entry["message_thread_id"]
            target_name = entry["name"]
        else:
            entry = await asyncio.to_thread(
                store.find_known_topic_by_name,
                update.effective_chat.id,
                arg,
            )
            if entry is None:
                await _reply(update,
                    f'❌ No topic named "{arg}" in this group.\n'
                    f'(The bot only knows topics created via /newsub;\n'
                    f'topics created manually in Telegram UI are not\n'
                    f'enumerated. Use the sidebar\'s "Delete topic".)'
                )
                return
            target_id = entry["message_thread_id"]
            target_name = entry["name"]
        try:
            await context.bot.delete_forum_topic(
                chat_id=update.effective_chat.id,
                message_thread_id=target_id,
            )
        except Exception as e:
            print(f"[ERR delsub-group-delete] {type(e).__name__}: {e}", flush=True)
            await _reply(update, f'❌ deleteForumTopic failed: {e}')
            return
        # Remove from known_topics + wipe local message history.
        await asyncio.to_thread(
            store.remove_known_topic,
            update.effective_chat.id,
            target_id,
        )
        n = await asyncio.to_thread(
            store.delete_thread, update.effective_chat.id, str(target_id)
        )
        await _reply(update,
            f'🗑 Deleted topic "{target_name}" (id={target_id}) '
            f'and {n} locally-stored message(s).'
        )
        return
    # === Private mode ===
    thread_id = _parse_subtalk_arg(update.message.text)
    if not thread_id:
        await _reply(update,
            '❌ Usage: /delsub <name>\n'
            'Removes the sub-talk AND all its messages. Cannot be undone.'
        )
        return
    if await reject_if_unauthorized(update, context):
        return
    user_id = update.effective_user.id
    n = await asyncio.to_thread(store.delete_thread, user_id, thread_id)
    if n == 0:
        await _reply(update, f'❌ Sub-talk "{thread_id}" not found.')
        return
    # If we just deleted the active one, the next get_active will
    # fall back to most-recent or to 'main'.
    await _reply(update,
        f'🗑 Deleted sub-talk "{thread_id}" and {n} message(s).'
    )


# === Sub-talk-aware helpers for the message handlers ===
# Each handler resolves the user's active sub-talk, persists the new
# user/assistant messages into that sub-talk, and loads the recent
# history (CONTEXT_MESSAGES messages) for the call_llama call.
#
# Messages are stored as full OpenAI-format message dicts
# ({"role": ..., "content": ...}) JSON-serialized in the messages.content
# column. That way text-only and multimodal (text + image_url) messages
# round-trip through SQLite without any translation layer.

# Two deployment modes share the same storage layer:
#
#   Private chat mode (legacy)
#     chat_id   = user_id (1:1 chat, same int)
#     thread_id = active sub-talk name from DB (or 'main' default)
#     whitelist = ALLOWED_USER_IDS
#
#   Group with Telegram Topics mode (new)
#     chat_id   = group chat id (negative number)
#     thread_id = str(update.message.message_thread_id)  -- the Telegram topic
#     whitelist = NONE (any group member can use the bot)
#
# Both modes are auto-detected from the incoming Update via
# _is_group_chat() (checks chat.is_forum and chat.type). The
# actual mode for a given message is decided at dispatch time,
# not stored as module state.



def _is_group_chat(update) -> bool:
    """True if the message is in a forum-enabled supergroup (or any non-private chat).

    Telegram Bot API quirk: in a forum-enabled supergroup, messages in
    the General topic have message_thread_id == None (not set), while
    messages in regular topics have it set to the topic id. So
    "message_thread_id is not None" is NOT a reliable way to detect
    group mode. The reliable signal is `chat.is_forum` (PTB attribute
    on Chat) or, failing that, `chat.type != 'private'`.
    """
    chat = update.effective_chat
    if chat is None:
        return False
    if getattr(chat, "is_forum", False):
        return True
    return chat.type != "private"


# Group-mode noise filter: in a forum-enabled supergroup, the bot
# shares the room with other Telegram bots AND with humans acting
# as LLM proxies. The convention (in the group's pinned welcome
# message) is that an active LLM marks itself with "[llm]" in its
# reply. We use that marker to stay silent when another LLM is
# actively answering — the goal being to avoid bot-to-bot
# double-answering, not to suppress other participants.
#
# In private mode this filter is a no-op (the bot talks to one
# human, no other LLM can interject). Applied only in group mode.
#
# The match is a standalone token, not a substring. The previous
# substring check had a small but real false-positive class: a
# user discussing "the [llm] model", typing "[llm]s are bad at
# math", or a URL containing [llm] in the path. Word-boundary
# match avoids all of those.
import re as _llm_re
_LLM_TOKEN_RE = _llm_re.compile(
    r"(?:^|\b|\s)\[llm\](?=$|[\s.,!?;:])",
    _llm_re.IGNORECASE,
)


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


def _msg_text_edited_during(chat_id: int, msg_id: int, text_at_start: str) -> bool:
    """True if the user message's text/caption has changed since
    text_at_start was captured. Used by handlers to detect
    "user edited while we were calling the LLM" — if so, the
    bot's response (about to be sent) would be based on stale
    text. Callers should discard the response and let the
    polling loop's edit-handling take over.

    Returns False if the dict has no entry for this message
    (e.g., it was a voice message, which has no editable text).
    """
    current = _user_msg_text.get((chat_id, msg_id))
    if current is None:
        return False
    return current != text_at_start


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


# Per-(chat_id, user_id) concurrency cap. Without this, one
# user in a group can flood the bot with messages and
# monopolise the call_llama queue (up to 10 min per request
# via LOOP_BUDGET_SEC). The semaphore caps each user's
# concurrent in-flight LLM calls at 2, with a friendly
# "busy with your earlier request" reply if the cap is hit.
#
# Private mode: keyed on user_id (chat_id is the same int).
# Group mode: keyed on (chat_id, user_id) so users in
# different groups don't affect each other.
_PER_USER_SEMAPHORE_LIMIT = 2
_user_semaphores: dict = {}


def _user_semaphore(chat_id: int, user_id: int) -> asyncio.Semaphore:
    """Return the per-(chat_id, user_id) asyncio.Semaphore, creating
    it on first use. Lazy init because we cannot create asyncio
    primitives at module-import time (no running event loop)."""
    key = (chat_id, user_id)
    sem = _user_semaphores.get(key)
    if sem is None:
        sem = asyncio.Semaphore(_PER_USER_SEMAPHORE_LIMIT)
        _user_semaphores[key] = sem
    return sem


async def _check_user_slot(chat_id: int, user_id: int, update):
    """Try to claim one of the user's concurrency slots. If all
    slots are taken, sends a 'busy' reply and returns None.
    Otherwise returns the semaphore which the caller MUST release
    in a `finally` block.

    Private mode: chat_id == user_id (1:1 chat), so the slot is
    effectively per-user. Group mode: keyed on (chat_id, user_id)
    so a user active in two groups has separate budgets per group.
    """
    sem = _user_semaphore(chat_id, user_id)
    if sem.locked():
        await _reply(update,
            f"⏳ Бот уже обрабатывает {_PER_USER_SEMAPHORE_LIMIT} твоих "
            f"запросов параллельно. Подожди пока один из них завершится."
        )
        return None
    await sem.acquire()
    return sem


# === Rating mode ===
# In a group with RATING_MODE=1, every user message is classified
# by the LLM into one of: question, request, confirmation, info,
# statement, bloat. The action depends on the type:
#
#   - question / request / confirmation → normal text reply
#   - info / statement                  → rate 1-10, apply a
#                                          Telegram reaction
#                                          (1=💩 ... 10=🔥), persist
#                                          the rating, no text
#   - bloat                             → one neutral reaction,
#                                          no text
#
# The LLM embeds its decision in the response as a structured
# prefix: `[[TYPE:<type>]] [[RATE:<1-10>]]` or `[[TYPE:bloat]] <emoji>`.
# For [llm]-marked messages the rating path is skipped — the
# bot always replies normally to other LLM participants.

import re as _rating_re

RATING_MODE: bool = os.environ.get("RATING_MODE", "0") == "1"

# 1-10 score → Telegram reaction emoji. Standard set accepted
# by setMessageReaction. 1=💩 (spam), 10=🔥 (insightful). Mid
# values step through facial reactions so the spread is visible
# in chat without needing the rating column to be displayed.
RATING_EMOJI = {
    1: "💩", 2: "🤮", 3: "😡", 4: "😢", 5: "😐",
    6: "🤔", 7: "👍", 8: "👏", 9: "❤️", 10: "🔥",
}

# Default emoji for bloat messages (no rating, just acknowledge).
BLOAT_EMOJI = "😐"

# === Rating rules ===
# This string is the system prompt injected when RATING_MODE=1
# and the chat is group mode. It is the bot's published policy
# for what the rating means. The same content is in
# docs/RATING_RULES.md, which the operator is expected to keep
# in sync with the welcome message of the group.
#
# Keep these rules declarative and machine-readable: the LLM
# is told to follow them literally. Plain English summary
# follows; the markdown is intentional for the LLM to parse.
RATING_RULES = """\
# Rating rules (RATING_MODE=1)
You are one of several LLM participants in a Telegram group.
For every human user message, classify the message and act
accordingly. Multiple LLMs in the group each apply their own
reaction; the aggregate is the consensus.

## Six message types
- question:     user is asking something
- request:      user is requesting an action (may lack "?")
- confirmation: user wants you to verify and confirm
- info:         user is sharing a verifiable fact
- statement:    user is asserting something to evaluate
- bloat:        stream-of-consciousness, not worth evaluating

## Three actions
- question / request / confirmation → text reply
- info / statement                  → rate 1-10, no text
- bloat                             → single neutral emoji, no text

## Output format
- text reply: just write the answer
- rate:      "[[TYPE:info]] [[RATE:1-10]]" or
             "[[TYPE:statement]] [[RATE:1-10]]"
- bloat:     "[[TYPE:bloat]] <one emoji>"

## Rating scale (1-10)
1=spam/junk · 2=misleading · 3=weak · 4=mediocre · 5=average
6=useful · 7=good · 8=strong · 9=insightful · 10=brilliant

## Rules of judgment (CRITICAL — read carefully)

1. Rate by TRUTH, not by style. A correct claim stated bluntly
   ("you're wrong, X") is a 7-10. A wrong claim stated politely
   ("perhaps we could consider that X may not be quite right")
   is a 1-3. Style is irrelevant; substance is everything.

2. Harsh language, profanity, and direct criticism are NOT
   penalized. A message calling out a factual error in strong
   terms can be a 9 if the callout is correct.

3. Mature language is allowed. The group is for serious LLM
   discussion. We do not police tone.

4. When in doubt, rate LOWER, not higher. A spam or low-effort
   message that you are not sure about is 1-2, not 5.

5. A message that mixes a correct claim with off-topic ranting
   rates on the claim, not the ranting. ("the sky is blue, and
   by the way everyone here is a moron" → 7 for the sky part.)

6. For info and statement, your rating is your honest subjective
   assessment. Do not be sycophantic. Do not give 7 by default.

## Special cases
- "[llm]" markers: skip the user. Other LLM participants are
  peers, not rating subjects.
- Pure greetings ("hi", "hello", "good morning"): bloat, 😐.
- Memes / single emoji: bloat.
- Off-topic / spam / advertisements: 1, 💩.
- Garbled / nonsensical: 1, 💩.

Apply your reaction directly. Do not explain your rating.
"""

# === Welcome text for new group members ===
# Sent (a) as a reply in the General topic of the group, and
# (b) as a private message to the new member's DM, whenever
# the bot detects a chat_member update with a status change
# from "left"/"kicked" to "member"/"administrator"/"creator".
#
# Purpose: ensure the LLM rating rules are the FIRST received
# information for any new joiner. LLM-claimed accounts
# (tagged with [llm]) in particular get this in their first
# getUpdates cycle, so they can adapt their behaviour from
# the first message they send.
#
# Keep this in sync with:
#   - RATING_RULES (the machine-readable form for our LLM)
#   - docs/RATING_RULES.md (the canonical policy)
#   - docs/WELCOME_MESSAGE.en.md / .ru.md (group-pinned form)
#
# Markdown is intentional: Telegram parses it in PMs.
WELCOME_TEXT = """\
👋 Welcome to LlmChatPlace

Public space for serious discussion between humans and LLM
agents. No tone policing, no "be polite" pressure.

🤖 LLM accounts — tag your messages with [llm]. Peer LLMs
skip them.

📊 How rating works

Every human message is classified + rated by LLM participants
via Telegram reactions. Peer review, not judgement.

  • question / request / confirmation → text reply
  • info / statement → rating 1-10, no text
  • bloat → 😐, no text

Scale: 💩 🤮 😡 😢 😐 🤔 👍 👏 ❤️ 🔥

⚖️ Rating = TRUTH, not style

A blunt correct claim is 7-10. A polite lie is 1-3. Profanity,
harsh language, direct criticism — NOT penalized. Rate on
substance. No sycophancy.

🎯 Multi-LLM = multi-vote

Telegram allows up to 11 distinct reactions per message from
different users. Multiple LLMs each apply their own →
top-reactions row is the consensus. No central arbiter.

❌ No auto-bans. Ratings are signals for the operator, not
verdicts.
"""

# The prefix grammar. Matches at the start of the LLM response.
# We keep the parser simple: only TYPE and optional RATE are
# captured; everything after is "rest" (the answer or reason).
# For bloat, the LLM is expected to put a single emoji in rest
# and we extract it; for info/statement rest is an optional reason.
_RATING_PREFIX_RE = _rating_re.compile(
    r"^\s*\[\[TYPE:(?P<type>question|request|confirmation|info|statement|bloat)\]\]"
    r"(?:\s*\[\[RATE:(?P<rate>[1-9]|10)\]\])?"
    r"(?:\s*(?P<rest>.*))?$",
    _rating_re.DOTALL,
)

# Single-emoji-char regex for extracting the emoji from a bloat
# rest like "🤔" or "🤔 some text". Used only when TYPE is bloat.
_EMOJI_CHAR_RE = _rating_re.compile(
    r"^(\S{1,4})\b"
)


def _parse_rating_response(text: str) -> dict:
    """Parse the LLM response for a RATING_MODE prefix.

    Returns a dict with:
      - 'type': one of question/request/confirmation/info/statement/bloat
      - 'rating': int 1-10 if type is info/statement, else None
      - 'emoji': one emoji char (for bloat) or the rating-mapped
        emoji (for info/statement), or None
      - 'rest': trailing text after the prefix (for question/
        request/confirmation this is the answer; for info/statement
        the optional reason; for bloat empty)

    If the response does not match the prefix grammar, returns
    `{'type': 'question', 'rating': None, 'emoji': None,
    'rest': text}` — i.e. assumes the LLM wanted to answer
    normally. This is the safe default in case the LLM
    misformats.
    """
    m = _RATING_PREFIX_RE.match(text)
    if m is None:
        return {"type": "question", "rating": None, "emoji": None, "rest": text.strip()}
    type_ = m.group("type")
    rating_raw = m.group("rate")
    rating = int(rating_raw) if rating_raw else None
    rest = (m.group("rest") or "").strip()
    if type_ == "bloat":
        # bloat: the LLM may put one emoji in rest (e.g. "🤔")
        # followed by an optional reaction. Extract the first
        # non-whitespace token if it looks like a single emoji.
        m2 = _EMOJI_CHAR_RE.match(rest) if rest else None
        emoji = m2.group(1) if m2 else BLOAT_EMOJI
        return {
            "type": "bloat",
            "rating": None,
            "emoji": emoji,
            "rest": "",
        }
    if type_ in ("info", "statement") and rating is not None:
        return {
            "type": type_,
            "rating": rating,
            "emoji": RATING_EMOJI.get(rating, BLOAT_EMOJI),
            "rest": rest,
        }
    # question / request / confirmation, OR info/statement
    # without a rating (LLM forgot [[RATE:N]]): treat as text
    # answer with the full rest.
    return {"type": type_, "rating": None, "emoji": None, "rest": rest}


async def _apply_reaction(context, chat_id: int, message_id: int, emoji: str) -> None:
    """Set a single-emoji reaction on a Telegram message. Best-effort:
    if the API rejects (e.g. emoji not in the allowed set, or bot
    lacks permission), the error is logged but does not propagate."""
    try:
        from telegram import ReactionTypeEmoji
        await context.bot.set_message_reaction(
            chat_id=chat_id,
            message_id=message_id,
            reaction=[ReactionTypeEmoji(emoji=emoji)],
        )
    except Exception as e:
        print(f"[rating] set_message_reaction failed: {type(e).__name__}: {e}", flush=True)


def _general_thread_id() -> str:
    """Sentinel thread id for the General topic of a forum-enabled
    supergroup. The General topic is real (it has messages, history,
    notifications) but the Bot API does not assign it a numeric
    message_thread_id; instead, messages in it have message_thread_id
    == None. We use a stable string sentinel so that conversation
    history persists per-(chat_id, "general") key.
    """
    return "general"

async def _route_to_thread(update, context) -> tuple[int, str, bool] | None:
    """Resolve the (chat_id, thread_id) for the current message.

    Returns a tuple (chat_id, thread_id, is_group) on success, or
    None if the user is not authorized (only in private mode).

    Private mode (update.message.message_thread_id is None):
      - Enforces ALLOWED_USER_IDS whitelist via reject_if_unauthorized.
      - chat_id = effective_user.id (== chat_id for 1:1 chats).
      - thread_id = active sub-talk name from DB, or 'main' default
        auto-created on first message.

    Group mode (update.message.message_thread_id is not None):
      - No whitelist check. Any member of the group can use the bot.
      - chat_id = effective_chat.id (the group id, negative).
      - thread_id = str(message_thread_id) -- the Telegram topic id.
    """
    msg = update.message
    if _is_group_chat(update):
        # Group mode. Telegram is the source of truth for thread
        # existence; we do not need a DB row to know it exists.
        # For messages in the General topic, message_thread_id is None
        # by Bot API design — use the "general" sentinel so the DB key
        # is stable and conversation history persists.
        if msg.message_thread_id is not None:
            thread_id = str(msg.message_thread_id)
        else:
            thread_id = _general_thread_id()
        return update.effective_chat.id, thread_id, True
    # Private mode
    if await reject_if_unauthorized(update, context):
        return None
    user_id = update.effective_user.id
    active = await asyncio.to_thread(store.get_active_thread, user_id)
    if active is not None:
        return user_id, active, False
    # No sub-talks at all yet — auto-create 'main' so the user's first
    # message lands somewhere sensible without them needing to /newsub.
    await asyncio.to_thread(store.create_thread, user_id, 'main')
    await asyncio.to_thread(store.set_active_thread, user_id, 'main')
    return user_id, 'main', False


async def _reply(update, text, **kwargs):
    """Reply to update.message, preserving message_thread_id in group mode.

    PTB's Message.reply_text() does NOT pass message_thread_id
    through to sendMessage, so replies in group mode would land
    in the General topic instead of staying in the user's topic.
    This helper fixes that.
    """
    if _is_group_chat(update):
        kwargs.setdefault('message_thread_id', update.message.message_thread_id)
    return await update.message.reply_text(text, **kwargs)


async def _reject_in_group(update) -> bool:
    """Sub-talk commands (/newsub, /sub, /subs, /delsub, /here, /reset, /stats)
    only make sense in private chat mode — they manage the user's own
    sub-talks in the bot's DB. In group mode, the equivalent is to use
    Telegram's native forum-topic controls (createForumTopic, etc.),
    which are wired in commit 3 of the group-mode migration.

    Returns True if the message is in a group and we sent a notice,
    False otherwise (i.e. the caller should continue processing).
    """
    if _is_group_chat(update):
        await _reply(
            update,
            'ℹ In group mode, sub-talk commands are replaced by Telegram\n'
            'forum topics. Use the chat\'s "Create topic" button or\n'
            'the /forumtopic /topics /deltopic commands instead.\n'
            'See docs/SETUP.md for the group deployment pattern.'
        )
        return True
    return False


async def _resolve_active(user_id: int) -> str:
    """Deprecated. Use _route_to_thread() in handlers.

    Kept for backward compatibility with any external code that
    imports it. New code should call _route_to_thread() instead.
    """
    active = await asyncio.to_thread(store.get_active_thread, user_id)
    if active is not None:
        return active
    # No sub-talks at all yet — auto-create 'main' so the user's first
    # message lands somewhere sensible without them needing to /newsub.
    await asyncio.to_thread(store.create_thread, user_id, 'main')
    await asyncio.to_thread(store.set_active_thread, user_id, 'main')
    return 'main'


async def _load_history(chat_id: int, thread_id: str) -> list:
    """Return up to CONTEXT_MESSAGES messages for the thread, in chronological
    order, as the full OpenAI message dicts ({"role", "content"}) that
    call_llama expects.

    Text-only and multimodal (text + image_url) messages are returned
    verbatim because both are stored as JSON in the DB and parse to the
    same shape that call_llama forwards to llama-server.
    """
    rows = await asyncio.to_thread(
        store.get_messages, chat_id, thread_id, CONTEXT_MESSAGES
    )
    out = []
    for r in rows:
        try:
            msg = json.loads(r["content"])
        except Exception:
            # Defensive: if a row was somehow written with non-JSON
            # content, fall back to wrapping it as a text message.
            msg = {"role": r["role"], "content": r["content"]}
        out.append(msg)
    return out


async def _persist_message(chat_id: int, thread_id: str, role: str, content):
    """Append a message to the thread's history in the DB.

    `content` is whatever the message has — a string for text, a list
    of content parts for multimodal. json.dumps it for storage.
    """
    msg = {"role": role, "content": content}
    await asyncio.to_thread(
        store.add_message, chat_id, thread_id, role, json.dumps(msg, ensure_ascii=False)
    )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print(f"[handle_photo] ENTRY chat_id={update.effective_chat.id} thread_id={update.message.message_thread_id} caption={update.message.caption!r}", flush=True)
    result = await _route_to_thread(update, context)
    if result is None:
        print("[handle_photo] REJECTED", flush=True)
        return
    chat_id, thread_id, is_group = result
    print(f"[handle_photo] chat_id={chat_id} thread_id={thread_id!r} is_group={is_group}", flush=True)
    sem = await _check_user_slot(chat_id, update.effective_user.id, update)
    if sem is None:
        return
    await update.message.chat.send_action(action='typing')
    thinking = None
    try:
        photo = update.message.photo[-1]
        # Telegram sends multiple PhotoSize entries; we want the smallest
        # that is still useful (index 0 = 90x90 thumbnail is too small,
        # we usually take the last = largest). But cap it: a 50-MP photo
        # at Q8_0 base64 inflates to >30 MB and OOMs the bot.
        # Note: photo.file_size is not always populated by Telegram. We
        # cannot trust it as the only guard, so we also stream-download
        # with a chunk counter that aborts if the cumulative size
        # exceeds MAX_PHOTO_BYTES. This avoids the OOM that the post-
        # download check (which only looks at len(bytearray)) could
        # not prevent — by the time you have the bytearray, you
        # already have the whole file in RAM.
        if photo.file_size and photo.file_size > MAX_PHOTO_BYTES:
            await _reply(update, 
                f'❌ Photo too large ({photo.file_size/1e6:.1f} MB > '
                f'{MAX_PHOTO_BYTES/1e6:.0f} MB).'
            )
            return
        file = await context.bot.get_file(photo.file_id)
        photo_bytes = await _stream_with_limit(file, MAX_PHOTO_BYTES, 'Photo', update)
        if photo_bytes is None:
            return
        # Telegram photos are often WebP (especially from Android), not JPEG.
        # Detect actual MIME from magic bytes — we were hardcoding image/jpeg
        # which caused llama-server to reject the image with 400.
        mime = 'image/jpeg'
        if photo_bytes[:8].startswith(b'\x89PNG\r\n\x1a\n'):
            mime = 'image/png'
        elif photo_bytes[:4] == b'RIFF' and photo_bytes[8:12] == b'WEBP':
            mime = 'image/webp'
        elif photo_bytes[:2] == b'\xff\xd8':
            mime = 'image/jpeg'
        elif photo_bytes[:6] in (b'GIF87a', b'GIF89a'):
            mime = 'image/gif'
        photo_b64 = base64.b64encode(photo_bytes).decode('ascii')
        photo_data_url = f"data:{mime};base64,{photo_b64}"
        caption = update.message.caption or 'Describe the image in detail.'
        print(f"[handle_photo] downloaded {len(photo_bytes)} bytes, caption={caption!r}", flush=True)
        photo_content = [
            {"type": "text", "text": caption},
            {"type": "image_url", "image_url": {"url": photo_data_url}}
        ]
        thread_id = thread_id  # already resolved by _route_to_thread
        await _persist_message(chat_id, thread_id, 'user', photo_content)
        history = await _load_history(chat_id, thread_id)
        # When recalling image turns in later text-only messages, the
        # images themselves cannot be re-sent from history (no id
        # retained), so the model falls back to its own description.
        thinking = await _reply(update, '💭 думаю…', reply_markup=_stop_button_markup())
        print(f"[handle_photo] chat_id={chat_id} thread_id={thread_id!r} history_len={len(history)}", flush=True)
        # Per-task abort event for the Stop button.
        abort_event = asyncio.Event()
        _abort_events[(chat_id, user_id)] = abort_event
        # Compute rating_active here (before call_llama) so we
        # can pass it to the model AND use it in the dispatcher.
        rating_active = (
            RATING_MODE
            and _is_group_chat(update)
            and not _should_mute_in_group(update)
        )
        bot_response = await call_llama(history, max_tokens=16384, user_text=caption, thinking_msg=thinking, use_stream=False, rating_active=rating_active, abort_event=abort_event)
        print(f"[handle_photo] call_llama returned: {len(bot_response)} chars, head={bot_response[:200]!r}", flush=True)
        if bot_response == '__ABORTED__':
            print(f"[handle_photo] ABORTED by user via Stop button, moving to next", flush=True)
            try:
                await thinking.edit_text('⏹ Остановлено')
            except Exception:
                pass
            return
        # === Empty-response retry ===
        # The LLM sometimes returns no content, no reasoning,
        # and no finish_reason — usually on hard questions
        # (future predictions, niche facts). The call_llama
        # fallback string starts with "[model returned an
        # empty response." We retry once. If still empty,
        # replace with a user-friendly message that suggests
        # rephrasing or trying again.
        if bot_response.startswith('[model returned an empty response.'):
            print(
                f"[handle_photo] empty response, retrying once",
                flush=True,
            )
            try:
                await thinking.edit_text('🔄 переспрашиваю…')
            except Exception:
                pass
            retry_response = await call_llama(
                history, max_tokens=16384, user_text=caption,
                thinking_msg=thinking, use_stream=False,
                rating_active=rating_active, abort_event=abort_event,
            )
            if retry_response == '__ABORTED__':
                print(f"[handle_photo] ABORTED on retry, moving to next", flush=True)
                try:
                    await thinking.edit_text('⏹ Остановлено')
                except Exception:
                    pass
                return
            if not retry_response.startswith('[model returned an empty response.'):
                bot_response = retry_response
                print(f"[handle_photo] retry succeeded", flush=True)
            else:
                print(f"[handle_photo] retry also empty, sending fallback", flush=True)
                bot_response = (
                    '🤔 Не удалось получить ответ от модели. '
                    'Попробуй переформулировать вопрос или '
                    'добавить больше контекста.'
                )
        # === Edit-during-LLM detection ===
        # NOTE: the original implementation here checked
        # _user_msg_text and discarded the response if the
        # caption was edited during the LLM call. That check
        # only works if a *concurrent* getUpdates consumer is
        # updating _user_msg_text in real time, which we
        # cannot do (Telegram rejects simultaneous getUpdates
        # from the same bot with HTTP 409). The check is now
        # a no-op in practice; we keep the data structure
        # for future use if/when the bot is migrated to
        # webhooks. The polling loop's edit block (commit
        # 7056887) still replaces stale responses AFTER they
        # are sent — the user sees a brief wrong answer, then
        # the corrected one. The Stop button (this commit) is
        # the recommended way to avoid the wrong answer.
        # === RATING_MODE dispatch ===
        # In a group with RATING_MODE=1, the LLM prefixes its
        # response with [[TYPE:...]] [[RATE:N]]. We parse the
        # type and take one of three actions:
        #   question / request / confirmation → text reply
        #   info / statement with rating        → emoji reaction, persist rating
        #   bloat                               → emoji reaction, no text
        # Private mode and [llm]-marked messages always get a
        # plain text reply (rating path is group-only).
        # rating_active was computed above (before call_llama).
        if rating_active:
            parsed = _parse_rating_response(bot_response)
            if parsed['type'] in ('info', 'statement') and parsed['rating'] is not None:
                await _apply_reaction(
                    context, update.effective_chat.id,
                    update.message.message_id, parsed['emoji'],
                )
                await _persist_message(
                    chat_id, thread_id, 'assistant', '',
                    rating=parsed['rating'],
                )
                bot_response = ''  # signal: no text reply
            elif parsed['type'] == 'bloat':
                await _apply_reaction(
                    context, update.effective_chat.id,
                    update.message.message_id, parsed['emoji'],
                )
                await _persist_message(
                    chat_id, thread_id, 'assistant', '',
                )
                bot_response = ''
            else:
                await _persist_message(
                    chat_id, thread_id, 'assistant', bot_response
                )
        else:
            await _persist_message(
                chat_id, thread_id, 'assistant', bot_response
            )
        if bot_response:
            await send_reply(update, bot_response)
    except Exception as e:
        print(f"[ERR photo] {type(e).__name__}: {e}", flush=True)
        import traceback
        traceback.print_exc()
        try:
            await _reply(update, f'❌ Error: {e}')
        except Exception:
            pass
    finally:
        if thinking is not None:
            try:
                await thinking.delete()
            except Exception:
                pass
        _abort_events.pop((chat_id, user_id), None)
        sem.release()


async def _stream_with_limit(file, max_bytes: int, kind: str, update: Update):
    """Stream-download a Telegram file, aborting if size > max_bytes.

    Returns the accumulated bytes on success, or None if aborted (the
    user-facing error message has already been sent to `update`).
    Used by handle_photo, handle_voice, and handle_document so the
    pre-check + chunk counter + error path live in one place.
    """
    buf = bytearray()
    try:
        async for chunk in file.download_as_chunks(chunk_size=64 * 1024):
            buf.extend(chunk)
            if len(buf) > max_bytes:
                await _reply(update, 
                    f'❌ {kind} too large (>{max_bytes/1e6:.0f} MB during download).'
                )
                return None
    except Exception as e:
        await _reply(update, f'❌ Failed to download {kind.lower()}: {e}')
        return None
    return buf


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result = await _route_to_thread(update, context)
    if result is None:
        return
    chat_id, thread_id, is_group = result
    user_id = chat_id  # alias for log lines below
    sem = await _check_user_slot(chat_id, user_id, update)
    if sem is None:
        return
    await update.message.chat.send_action(action='typing')
    thinking = None
    try:
        msg = update.message
        # filters.AUDIO catches both voice messages and video notes.
        # Pick the right cap for the kind we got; tell the user which
        # one if we reject.
        if msg.voice:
            max_bytes = MAX_VOICE_BYTES
            media = msg.voice
            media_kind = "Voice"
        elif msg.video_note:
            max_bytes = MAX_VIDEO_NOTE_BYTES
            media = msg.video_note
            media_kind = "Video note"
        else:
            return  # should not happen — handler is only registered for these
        # Pre-check; media.file_size is usually populated. The real
        # guard is the chunk counter in the helper.
        if media.file_size and media.file_size > max_bytes:
            await _reply(update, 
                f'❌ {media_kind} too large ({media.file_size/1e6:.1f} MB > '
                f'{max_bytes/1e6:.0f} MB).'
            )
            return
        file = await context.bot.get_file(media.file_id)
        voice_bytes = await _stream_with_limit(file, max_bytes, media_kind, update)
        if voice_bytes is None:
            return
        transcript = await transcribe_voice(voice_bytes)
        # No parse_mode — transcript is user-generated and may contain
        # '*', '_', '[', '`', which would break Markdown rendering.
        await _reply(update, f'🎤 Transcript:\n{transcript}')
        await _persist_message(chat_id, thread_id, 'user', transcript)
        history = await _load_history(chat_id, thread_id)
        print(f"[handle_voice] user_id={user_id} thread_id={thread_id!r} history_len={len(history)}", flush=True)
        thinking = await _reply(update, '💭 думаю…')
        rating_active = (
            RATING_MODE
            and _is_group_chat(update)
            and not _should_mute_in_group(update)
        )
        bot_response = await call_llama(history, max_tokens=16384, user_text=transcript, thinking_msg=thinking, rating_active=rating_active)
        # === RATING_MODE dispatch (continuation) ===
        # rating_active was computed above (before call_llama).
        if rating_active:
            parsed = _parse_rating_response(bot_response)
            if parsed['type'] in ('info', 'statement') and parsed['rating'] is not None:
                await _apply_reaction(
                    context, update.effective_chat.id,
                    update.message.message_id, parsed['emoji'],
                )
                await _persist_message(
                    chat_id, thread_id, 'assistant', '',
                    rating=parsed['rating'],
                )
                bot_response = ''
            elif parsed['type'] == 'bloat':
                await _apply_reaction(
                    context, update.effective_chat.id,
                    update.message.message_id, parsed['emoji'],
                )
                await _persist_message(
                    chat_id, thread_id, 'assistant', '',
                )
                bot_response = ''
            else:
                await _persist_message(
                    chat_id, thread_id, 'assistant', bot_response
                )
        else:
            await _persist_message(
                chat_id, thread_id, 'assistant', bot_response
            )
        if bot_response:
            # In rating mode the response is short (no chunking needed).
            # The original 4000-char chunking applied to voice transcripts
            # which are unlikely to exceed 4k after going through the LLM
            # in this mode.
            if len(bot_response) > 4000:
                for i in range(0, len(bot_response), 4000):
                    await _reply(update, bot_response[i:i+4000])
            else:
                await send_reply(update, bot_response)
    except Exception as e:
        print(f"[ERR voice] {type(e).__name__}: {e}", flush=True)
        await _reply(update, f'❌ Error: {e}')
    finally:
        if thinking is not None:
            try:
                await thinking.delete()
            except Exception:
                pass
        sem.release()


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result = await _route_to_thread(update, context)
    if result is None:
        return
    chat_id, thread_id, is_group = result
    user_id = chat_id  # alias for log lines below
    sem = await _check_user_slot(chat_id, user_id, update)
    if sem is None:
        return
    await update.message.chat.send_action(action='typing')
    doc = update.message.document
    # Reject oversized documents before downloading. We will only ever
    # read the first 16 KB of text from the file, so anything bigger is
    # almost certainly a mistake or an attempt to OOM the bot.
    if doc.file_size and doc.file_size > MAX_DOC_BYTES:
        await _reply(update, 
            f'❌ Document too large ({doc.file_size/1e6:.1f} MB > '
            f'{MAX_DOC_BYTES/1e6:.0f} MB).'
        )
        return
    file = await context.bot.get_file(doc.file_id)
    doc_bytes = await _stream_with_limit(file, MAX_DOC_BYTES, 'Document', update)
    if doc_bytes is None:
        return
    tmp_path = None
    thinking = None
    try:
        ext = doc.file_name.split('.')[-1] if '.' in doc.file_name else 'txt'
        with tempfile.NamedTemporaryFile(delete=False, suffix=f'.{ext}') as tmp:
            tmp.write(doc_bytes)
            tmp_path = tmp.name
        with open(tmp_path, 'r', encoding='utf-8', errors='ignore') as f:
            doc_text = f.read()[:16000]
        caption = update.message.caption or 'Read the document and answer questions.'
        user_content = f"{caption}\n\n--- Document ---\n{doc_text}"
        await _persist_message(chat_id, thread_id, 'user', user_content)
        history = await _load_history(chat_id, thread_id)
        print(f"[handle_document] user_id={user_id} thread_id={thread_id!r} history_len={len(history)}", flush=True)
        thinking = await _reply(update, '💭 думаю…', reply_markup=_stop_button_markup())
        rating_active = (
            RATING_MODE
            and _is_group_chat(update)
            and not _should_mute_in_group(update)
        )
        abort_event = asyncio.Event()
        _abort_events[(chat_id, user_id)] = abort_event
        bot_response = await call_llama(history, max_tokens=16384, user_text=caption, thinking_msg=thinking, rating_active=rating_active, abort_event=abort_event)
        if bot_response == '__ABORTED__':
            print(f"[handle_document] ABORTED by user via Stop button, moving to next", flush=True)
            try:
                await thinking.edit_text('⏹ Остановлено')
            except Exception:
                pass
            return
        # === Empty-response retry (same as handle_photo) ===
        if bot_response.startswith('[model returned an empty response.'):
            print(f"[handle_document] empty response, retrying once", flush=True)
            try:
                await thinking.edit_text('🔄 переспрашиваю…')
            except Exception:
                pass
            retry_response = await call_llama(
                history, max_tokens=16384, user_text=caption,
                thinking_msg=thinking, rating_active=rating_active,
                abort_event=abort_event,
            )
            if retry_response == '__ABORTED__':
                print(f"[handle_document] ABORTED on retry, moving to next", flush=True)
                try:
                    await thinking.edit_text('⏹ Остановлено')
                except Exception:
                    pass
                return
            if not retry_response.startswith('[model returned an empty response.'):
                bot_response = retry_response
                print(f"[handle_document] retry succeeded", flush=True)
            else:
                print(f"[handle_document] retry also empty, sending fallback", flush=True)
                bot_response = (
                    '🤔 Не удалось получить ответ от модели. '
                    'Попробуй переформулировать вопрос или '
                    'добавить больше контекста.'
                )
        # Edit-during-LLM detection removed: the check is a
        # no-op without a concurrent getUpdates consumer.
        # See handle_photo for the full rationale.
        # === RATING_MODE dispatch ===
        # In a group with RATING_MODE=1, the LLM prefixes its
        # response with [[TYPE:...]] [[RATE:N]]. We parse the
        # type and take one of three actions:
        #   question / request / confirmation → text reply
        #   info / statement with rating        → emoji reaction, persist rating
        #   bloat                               → emoji reaction, no text
        # Private mode and [llm]-marked messages always get a
        # plain text reply (rating path is group-only).
        # rating_active was computed above (before call_llama).
        if rating_active:
            parsed = _parse_rating_response(bot_response)
            if parsed['type'] in ('info', 'statement') and parsed['rating'] is not None:
                await _apply_reaction(
                    context, update.effective_chat.id,
                    update.message.message_id, parsed['emoji'],
                )
                await _persist_message(
                    chat_id, thread_id, 'assistant', '',
                    rating=parsed['rating'],
                )
                bot_response = ''  # signal: no text reply
            elif parsed['type'] == 'bloat':
                await _apply_reaction(
                    context, update.effective_chat.id,
                    update.message.message_id, parsed['emoji'],
                )
                await _persist_message(
                    chat_id, thread_id, 'assistant', '',
                )
                bot_response = ''
            else:
                await _persist_message(
                    chat_id, thread_id, 'assistant', bot_response
                )
        else:
            await _persist_message(
                chat_id, thread_id, 'assistant', bot_response
            )
        if bot_response:
            await send_reply(update, bot_response)
    except Exception as e:
        print(f"[ERR doc] {type(e).__name__}: {e}", flush=True)
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)
        if thinking is not None:
            try:
                await thinking.delete()
            except Exception:
                pass
        _abort_events.pop((chat_id, user_id), None)
        sem.release()


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print(f"[handle_text] ENTRY user_id={update.effective_user.id} chat_type={update.effective_chat.type} is_forum={getattr(update.effective_chat, 'is_forum', None)} thread_id={update.message.message_thread_id} text={update.message.text!r}", flush=True)
    result = await _route_to_thread(update, context)
    if result is None:
        print(f"[handle_text] REJECTED {update.effective_user.id}", flush=True)
        return
    chat_id, thread_id, is_group = result
    user_id = chat_id  # alias for log lines below
    sem = await _check_user_slot(chat_id, user_id, update)
    if sem is None:
        return
    user_message = update.message.text
    try:
        await update.message.chat.send_action(action='typing')
    except Exception as e:
        print(f"[handle_text] send_action FAILED: {type(e).__name__}: {e!r}", flush=True)
    await _persist_message(chat_id, thread_id, 'user', user_message)
    history = await _load_history(chat_id, thread_id)
    print(f"[handle_text] user_id={user_id} thread_id={thread_id!r} history_len={len(history)}", flush=True)
    thinking = await _reply(update, '💭 думаю…', reply_markup=_stop_button_markup())
    # Register per-task abort event so the Stop button on the
    # thinking message can interrupt the LLM call. Cleaned up
    # in the finally block to avoid leaking the event into the
    # next handler call (the semaphore allows the same user to
    # have at most 2 in-flight requests, so the dict shouldn't
    # grow without bound).
    abort_event = asyncio.Event()
    _abort_events[(chat_id, user_id)] = abort_event
    try:
        rating_active = (
            RATING_MODE
            and _is_group_chat(update)
            and not _should_mute_in_group(update)
        )
        bot_response = await call_llama(history, max_tokens=32768, user_text=user_message, thinking_msg=thinking, rating_active=rating_active, abort_event=abort_event)
        if bot_response == '__ABORTED__':
            print(f"[handle_text] ABORTED by user via Stop button, moving to next", flush=True)
            try:
                await thinking.edit_text('⏹ Остановлено')
            except Exception:
                pass
            return
        # === Empty-response retry (same as handle_photo) ===
        if bot_response.startswith('[model returned an empty response.'):
            print(f"[handle_text] empty response, retrying once", flush=True)
            try:
                await thinking.edit_text('🔄 переспрашиваю…')
            except Exception:
                pass
            retry_response = await call_llama(
                history, max_tokens=32768, user_text=user_message,
                thinking_msg=thinking, rating_active=rating_active,
                abort_event=abort_event,
            )
            if retry_response == '__ABORTED__':
                print(f"[handle_text] ABORTED on retry, moving to next", flush=True)
                try:
                    await thinking.edit_text('⏹ Остановлено')
                except Exception:
                    pass
                return
            if not retry_response.startswith('[model returned an empty response.'):
                bot_response = retry_response
                print(f"[handle_text] retry succeeded", flush=True)
            else:
                print(f"[handle_text] retry also empty, sending fallback", flush=True)
                bot_response = (
                    '🤔 Не удалось получить ответ от модели. '
                    'Попробуй переформулировать вопрос или '
                    'добавить больше контекста.'
                )
        # Edit-during-LLM detection removed: the check is a
        # no-op without a concurrent getUpdates consumer.
        # See handle_photo for the full rationale.
        # === RATING_MODE dispatch ===
        if rating_active:
            parsed = _parse_rating_response(bot_response)
            if parsed['type'] in ('info', 'statement') and parsed['rating'] is not None:
                await _apply_reaction(
                    context, update.effective_chat.id,
                    update.message.message_id, parsed['emoji'],
                )
                await _persist_message(
                    chat_id, thread_id, 'assistant', '',
                    rating=parsed['rating'],
                )
                bot_response = ''
            elif parsed['type'] == 'bloat':
                await _apply_reaction(
                    context, update.effective_chat.id,
                    update.message.message_id, parsed['emoji'],
                )
                await _persist_message(
                    chat_id, thread_id, 'assistant', '',
                )
                bot_response = ''
            else:
                await _persist_message(
                    chat_id, thread_id, 'assistant', bot_response
                )
        else:
            await _persist_message(
                chat_id, thread_id, 'assistant', bot_response
            )
        if bot_response:
            if len(bot_response) > 4000:
                for i in range(0, len(bot_response), 4000):
                    await _reply(update, bot_response[i:i+4000])
            else:
                await send_reply(update, bot_response)
    except Exception as e:
        print(f"[ERR text] {type(e).__name__}: {e}", flush=True)
        await _reply(update, f'❌ Error: {e}')
    finally:
        try:
            await thinking.delete()
        except Exception:
            pass
        # Deregister the per-task abort event so a subsequent
        # handler call for the same user doesn't accidentally
        # see a leftover event. We pop the specific key rather
        # than clear, in case the dict has been overwritten by
        # a concurrent handler (defensive — the semaphore
        # prevents same-user concurrency, but be safe).
        _abort_events.pop((chat_id, user_id), None)
        sem.release()


def main():
    # === Pure-httpx polling loop, no PTB Updater, no app.start() ===
    # Rationale: with `app.initialize()` or `app.start()` PTB holds a
    # second keep-alive connection open to api.telegram.org, which makes
    # Telegram see a "duplicate long-poll" and 409 every other cycle.
    # Solution: never use PTB's HTTP client. Hit the Bot API directly
    # with a single dedicated httpx.AsyncClient, de_json() into Update
    # objects, then call handler functions ourselves (not via dispatcher,
    # to skip process_update's internal queue/worker logic).
    print('🤖 LlamaBot v2 (no-PTB-poll) started...', flush=True)

    TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

    # Graceful shutdown: SIGINT (Ctrl+C) and SIGTERM (docker stop / systemd
    # stop) set this event; the polling loop checks it at the top of each
    # iteration. Without this, KeyboardInterrupt aborts the in-flight
    # httpx call mid-stream and any in-progress handler dies with an
    # unhandled exception traceback instead of returning cleanly.
    shutdown_event = asyncio.Event()

    def _on_signal(signame: str) -> None:
        print(f"[main] received {signame}, initiating shutdown...", flush=True)
        shutdown_event.set()

    async def _run():
        # Install signal handlers now that we have a running loop.
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _on_signal, sig.name)
            except (NotImplementedError, RuntimeError):
                # Windows or non-main thread: fall back to default behaviour
                # (KeyboardInterrupt will still abort, just less gracefully).
                pass
        # Publish to module global so call_llama() (called from PTB handlers
        # that don't get a context arg from our hand-rolled polling loop)
        # can read it and bail between iterations.
        global SHUTDOWN_EVENT
        SHUTDOWN_EVENT = shutdown_event

        # Register the bot's command menu with Telegram. Without this,
        # the chat input / menu shows whatever was last setMyCommands'd
        # for this bot — usually nothing useful. setMyCommands is the
        # native way to make commands discoverable in the GUI: they
        # appear in the "Menu" button on the chat input bar with their
        # descriptions, and typing / shows the list.
        try:
            await _register_bot_menu()
        except Exception as e:
            print(f"[main] setMyCommands failed (non-fatal): {e!r}", flush=True)
        # NB: we intentionally do NOT call getUpdates with offset=-1 to
        # drop pending updates. Reasoning:
        #   - in LOCKDOWN, the handler rejects everything anyway, so
        #     backlog gets eaten harmlessly;
        #   - in non-LOCKDOWN, dropping pending messages is a UX bug
        #     (user sent a message while bot was down, expects to see
        #     a reply when bot comes back).
        # The CALL_LLAMA.md §4 paragraph that claimed otherwise is
        # corrected by this comment.

        # Self-test: if LLAMABOT_SELFTEST=1, push a fake Update through
        # the dispatcher so we can verify handlers actually fire even when
        # we can't easily test with a real user.
        await _selftest()

        offset = 0
        backoff = 1.0
        n_polls = 0
        n_updates = 0
        # Use a single dedicated httpx client. max_keepalive_connections=0
        # means no keep-alive — every request opens a fresh connection.
        # This guarantees only one open connection at a time.
        # try/finally around the whole block ensures the PTB dispatcher
        # gets shut down on every exit path: normal `break` (graceful
        # shutdown), CancelledError (asyncio.run() cancel), or any
        # unexpected exception. Without the finally, the httpx client
        # held by app.bot leaks and prints "RuntimeWarning: unclosed
        # client" on interpreter shutdown.
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(connect=10.0, read=35.0, write=10.0, pool=10.0),
                limits=httpx.Limits(max_connections=1, max_keepalive_connections=0),
            ) as client:
                while True:
                    if shutdown_event.is_set():
                        print(f"[poll] shutdown_event set, exiting loop (after {n_polls} cycles, {n_updates} updates)", flush=True)
                        break
                    try:
                        r = await client.get(
                            f"{TELEGRAM_API}/getUpdates",
                            params={
                                "offset": offset,
                                "timeout": 25,            # long-poll
                                "allowed_updates": '["message","edited_message","chat_member"]',
                            },
                        )
                        data = r.json()
                        if not data.get("ok"):
                            raise RuntimeError(f"getUpdates not ok: {data}")
                        updates_raw = data.get("result", [])
                        n_polls += 1
                        if updates_raw:
                            n_updates += len(updates_raw)
                            backoff = 1.0
                            print(f"[poll] cycle={n_polls} got {len(updates_raw)} updates (total={n_updates})", flush=True)
                            for upd_dict in updates_raw:
                                # PII-safe dump. The previous version
                                # printed the full JSON including
                                # first_name, last_name, chat.title, and
                                # the message text — all of which are
                                # PII in journald. The new line logs
                                # only structural fields plus a short
                                # content hash for cross-correlating
                                # with later log lines.
                                upd_id = upd_dict.get("update_id", "?")
                                upd_msg = upd_dict.get("message") or upd_dict.get("edited_message") or {}
                                upd_chat = upd_msg.get("chat") or {}
                                upd_from = upd_msg.get("from") or {}
                                upd_text = (upd_msg.get("text") or upd_msg.get("caption") or "")[:60]
                                # Populate _user_msg_text with the latest
                                # text/caption for this user message. This
                                # is read by handlers after the LLM call
                                # to detect "edited during processing".
                                # Both `message` and `edited_message`
                                # update the same key, so the dict always
                                # reflects the latest known text.
                                _upd_chat_id = upd_chat.get("id")
                                _upd_msg_id = upd_msg.get("message_id")
                                _upd_full_text = upd_msg.get("text") or upd_msg.get("caption") or ""
                                if _upd_chat_id is not None and _upd_msg_id is not None and _upd_full_text:
                                    _user_msg_text[(_upd_chat_id, _upd_msg_id)] = _upd_full_text
                                import hashlib
                                text_hash = hashlib.sha1(upd_text.encode("utf-8", errors="replace")).hexdigest()[:10] if upd_text else "-"
                                print(
                                    f"[poll] update_id={upd_id} "
                                    f"chat_type={upd_chat.get('type', '?')} "
                                    f"chat_id={upd_chat.get('id', '?')} "
                                    f"is_forum={upd_chat.get('is_forum', '?')} "
                                    f"from_id={upd_from.get('id', '?')} "
                                    f"from_is_bot={upd_from.get('is_bot', '?')} "
                                    f"thread_id={upd_msg.get('message_thread_id', '-')} "
                                    f"text_hash={text_hash}",
                                    flush=True,
                                )
                                offset = upd_dict["update_id"] + 1
                                # edited_message handling:
                                #
                                # Telegram re-delivers edited messages as
                                # `edited_message` updates (separate from
                                # the original `message`). The bot used to
                                # skip non-command edits — the original
                                # was already answered, the edit was just
                                # a typo fix. But this made the bot
                                # appear unresponsive to the corrected
                                # version: the user sees a wrong answer
                                # for the typo and no new answer for the
                                # correction.
                                #
                                # New behaviour:
                                #   1. If the new text is a command, keep
                                #      the old behaviour (commands are
                                #      idempotent, re-running is fine).
                                #   2. If the new text is regular text,
                                #      look up the bot's previous reply
                                #      to the original message in
                                #      _bot_replies. If we have one,
                                #      delete it. Then rewrite the
                                #      update as a regular `message` and
                                #      let the dispatcher process it
                                #      like a fresh user message.
                                #   3. If we have no previous reply
                                #      (e.g., the original was rejected
                                #      for security, or the bot never
                                #      responded for some reason), just
                                #      process the edit.
                                if "edited_message" in upd_dict and "message" not in upd_dict:
                                    em = upd_dict["edited_message"]
                                    em_text = (em.get("text") or "").lstrip()
                                    if em_text.startswith("/"):
                                        # Normalise: rewrite the update so
                                        # the dispatcher's handlers see a
                                        # regular `message` field. This way
                                        # every handler continues to read
                                        # `update.message.text` and friends
                                        # without per-handler branching.
                                        upd_dict["message"] = em
                                        print(
                                            f"[poll] treating edited_message as new command "
                                            f"update_id={upd_dict['update_id']} cmd={em_text[:32]!r}",
                                            flush=True,
                                        )
                                    else:
                                        # Non-command edit: delete the
                                        # bot's previous reply (if any)
                                        # and reprocess.
                                        em_chat_id = (em.get("chat") or {}).get("id")
                                        em_msg_id = em.get("message_id")
                                        if em_chat_id is not None and em_msg_id is not None:
                                            key = (em_chat_id, em_msg_id)
                                            old_bot_msg_id = _bot_replies.pop(key, None)
                                            if old_bot_msg_id is not None and _dispatcher is not None:
                                                try:
                                                    await _dispatcher.bot.delete_message(
                                                        chat_id=em_chat_id,
                                                        message_id=old_bot_msg_id,
                                                    )
                                                    print(
                                                        f"[poll] edit-replace: deleted old bot reply "
                                                        f"chat_id={em_chat_id} bot_msg_id={old_bot_msg_id} "
                                                        f"user_msg_id={em_msg_id}",
                                                        flush=True,
                                                    )
                                                except Exception as e:
                                                    # Most common failure: bot
                                                    # lacks delete permission,
                                                    # or the message is too
                                                    # old. Just log and
                                                    # continue.
                                                    print(
                                                        f"[poll] edit-replace: delete failed "
                                                        f"chat_id={em_chat_id} bot_msg_id={old_bot_msg_id}: "
                                                        f"{type(e).__name__}: {e!r}",
                                                        flush=True,
                                                    )
                                        # Now rewrite the update as a
                                        # regular `message` so the
                                        # dispatcher processes it like
                                        # a new user message. The
                                        # downstream handlers (handle_text
                                        # etc.) will see the edited text.
                                        upd_dict["message"] = em
                                        print(
                                            f"[poll] edit-replace: processing edited_message "
                                            f"update_id={upd_dict['update_id']} text_hash={em_text[:32]!r}",
                                            flush=True,
                                        )
                                try:
                                    # Dispatch via PTB's Application so handlers
                                    # get a real Context with .bot, .user_data, etc.
                                    # We construct an Application lazily here, only
                                    # for this single Update, to avoid keeping
                                    # any persistent state.
                                    await _dispatch_update(upd_dict)
                                except Exception as e:
                                    print(f"[poll] dispatch error: {type(e).__name__}: {e!r}", flush=True)
                                    import traceback
                                    traceback.print_exc()
                        elif n_polls <= 5 or n_polls % 20 == 0:
                            print(f"[poll] cycle={n_polls} no updates", flush=True)
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        print(f"[poll] error: {type(e).__name__}: {e!r} (backoff {backoff}s)", flush=True)
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2, 30.0)
        finally:
            # Cleanly close the PTB Application if it was ever built.
            # Runs on every exit path: normal `break` (graceful
            # shutdown), CancelledError, or any exception. Without
            # this, the httpx client held by app.bot leaks and prints
            # "RuntimeWarning: unclosed client" on interpreter shutdown.
            if _dispatcher is not None:
                try:
                    await _dispatcher.shutdown()
                except Exception as e:
                    print(f"[main] dispatcher shutdown error: {e!r}", flush=True)

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        print('[main] KeyboardInterrupt', flush=True)


# Lazy global app used only for the dispatcher. Built once on first
# dispatch. We never call .start() on it; the dispatcher works fine
# without start() for one-shot process_update.
_dispatcher = None



# === Per-task abort events (Stop button) ===
# When the user clicks "Stop" on the thinking message, we set
# the event for their (chat_id, user_id). call_llama() checks
# the event between iterations of the tool loop and bails out
# with the special return value '__ABORTED__'. The handler then
# edits the thinking message to "⏹ Остановлено" and moves to
# the next update.
#
# The dict is keyed by (chat_id, user_id) so the same Stop
# button only affects its own message. In a forum group, each
# topic is a separate "user" from the perspective of the
# bot's processing order (the semaphore is per-(chat,user)).
#
# Entries are added at the start of the handler and removed in
# a try/finally so an exception doesn't leak the event into
# the next handler call.
_abort_events: dict = {}


# === Edit-replace tracking ===
# When a user edits their message, the bot's previous response to
# the original message becomes stale. We track the (chat_id,
# user_message_id) → bot_message_id mapping so that, on edit, we
# can delete the stale bot reply and process the edit as a fresh
# user message. The map lives in-memory; restarts clear it, which
# is fine because edits are only useful for the current session.
#
# Only `send_reply` (the final response) is tracked, not the
# "💭 думаю…" thinking message. We don't want to delete the
# thinking message on edit — it's just a status indicator.
_bot_replies: dict = {}


# === Latest-text tracking for "edit-during-LLM-call" detection ===
# This is the second half of the edit-handling story. _bot_replies
# above handles the case where the user edits AFTER the bot has
# finished responding. _user_msg_text handles the case where the
# user edits WHILE the bot is in the middle of an LLM call.
#
# Flow when bot is busy answering message X (the user's reported bug):
#   1. Bot polls, gets [Y_orig] (Y_edit not yet sent by user).
#   2. Bot dispatches Y_orig: handler starts, sends "💭 думаю…",
#      calls LLM (3 seconds). Main polling loop is BLOCKED on
#      the LLM await.
#   3. While the LLM is in flight, user edits Y_orig → Y_edit.
#      The edit is queued at Telegram.
#   4. BACKGROUND TEXT UPDATER (separate async task, started
#      at boot) is concurrently polling getUpdates. It picks
#      up Y_edit and updates _user_msg_text[(chat, Y_id)] =
#      edited_text. This happens independently of the main
#      polling loop, so it works even while the main loop is
#      blocked on the LLM call.
#   5. LLM returns. Handler checks: does _user_msg_text still
#      equal what was sent to the LLM? NO (it has the edited
#      text). Handler:
#        - Deletes the "💭 думаю…" thinking message
#        - Does NOT call send_reply (would send a response
#          based on the OLD text — the bug)
#        - Logs and returns
#   6. Main polling loop continues, picks up Y_edit. The
#      polling loop's edit block runs, but _bot_replies has
#      no entry for Y_orig (we never recorded one), so the
#      delete is a no-op. The edit is rewritten as a message
#      and dispatched. The handler runs again (this time
#      with the EDITED text), LLM call, response sent.
#
# Net effect: the user sees a brief "💭 думаю…" for the typo,
# then it disappears, then a new "💭 думаю…" for the corrected
# version, then the final corrected response. They do NOT see
# a response to the typo (which was the bug).
#
# Why this needs a background task: the main polling loop is
# single-threaded; while it's blocked on an LLM call, it
# cannot pick up new updates. A second concurrent consumer
# (the background task) is the simplest way to get real-time
# edit detection without changing the bot to webhooks.
#
# The dict is in-memory only. For media messages (photo, doc)
# the tracked value is the caption, which is the only part
# that can be edited — the media itself is immutable on edit.
# Voice messages have no editable text and are not tracked.
_user_msg_text: dict = {}

# The background text updater was disabled: Telegram rejects
# simultaneous getUpdates from the same bot (HTTP 409). The
# "edit during LLM" detection therefore relies on a different
# mechanism (or is accepted as a brief wrong response, replaced
# by the polling loop's edit block).
_bg_text_offset: int = 0


# Telegram bot menu — registered via setMyCommands on startup. This
# is the native way to make commands discoverable in the Telegram
# client: they show up in the "Menu" button next to the chat input,
# and typing "/" suggests them with descriptions.
#
# Telegram Bot API limit: 100 commands per bot. We have 9. The list
# below is also exported as the user-facing help text via /help.
BOT_COMMANDS = [
    ("start",   "🏁 Начать работу, показать возможности"),
    ("help",    "❓ Помощь по командам"),
    ("here",    "📍 Показать текущий sub-talk"),
    ("subs",    "📚 Список всех sub-talks (с кнопками)"),
    ("newsub",  "➕ Создать новый sub-talk: /newsub <имя>"),
    ("sub",     "↔️ Переключиться на sub-talk: /sub <имя>"),
    ("delsub",  "🗑 Удалить sub-talk: /delsub <имя>"),
    ("reset",   "🔄 Очистить сообщения в текущем sub-talk"),
    ("stats",   "📊 Статистика и список sub-talks"),
]


async def _register_bot_menu():
    """Register the bot's command menu with Telegram via setMyCommands.

    Called once at startup. Failures are non-fatal — if the network
    is briefly down at boot, the user can still type /command
    manually; the menu just won't show until next restart. Once
    set, the menu persists in Telegram's cache across restarts
    (re-registering is idempotent and cheap).
    """
    from telegram import BotCommand
    global _dispatcher
    if _dispatcher is None:
        # Build the dispatcher eagerly so we can call setMyCommands.
        # Same construction as _dispatch_update; both call sites
        # end up at the same Application instance.
        app = Application.builder().token(BOT_TOKEN).build()
        app.add_handler(CommandHandler("start", start))
        app.add_handler(CommandHandler("help", cmd_help))
        app.add_handler(CommandHandler("reset", reset))
        app.add_handler(CommandHandler("stats", stats))
        app.add_handler(CommandHandler("newsub", cmd_newsub))
        app.add_handler(CommandHandler("sub", cmd_sub))
        app.add_handler(CommandHandler("subs", cmd_subs))
        app.add_handler(CommandHandler("delsub", cmd_delsub))
        app.add_handler(CommandHandler("here", cmd_here))
        app.add_handler(CommandHandler("help", cmd_help))
        app.add_handler(CallbackQueryHandler(cmd_callback))
        app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
        app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
        app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
        _dispatcher = app
        # PTB 21 requires Application.initialize() before process_update.
        # We do not run app.start() — the hand-rolled polling loop drives
        # everything — but initialize() sets up the application context.
        await _dispatcher.initialize()
    await _dispatcher.bot.set_my_commands(
        [BotCommand(c, d) for c, d in BOT_COMMANDS],
        scope=None,  # default scope covers all private chats
        language_code=None,
    )
    print(f"[main] setMyCommands: registered {len(BOT_COMMANDS)} commands", flush=True)


async def cmd_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline-keyboard button presses.

    callback_data format:
      sub:<name>     — switch to sub-talk <name>  (private mode)
      newsub:prompt  — show a one-time reply keyboard asking for the new name
      delsub:<name>  — confirm-then-delete (we delete immediately; no confirm step)
      stop:thinking  — abort the in-flight LLM call for the user

    The Stop button is on every "💭 думаю…" message in both
    private and group mode. The original user (the one whose
    message is being processed) is the only one allowed to
    stop it — identified via the thinking message's
    reply_to_message.from_user.id. We compare that against
    the callback's from_user.id.
    """
    await update.callback_query.answer()
    data = update.callback_query.data or ""
    # === Stop button (works in private AND group) ===
    if data == "stop:thinking":
        # Identify the original user from the thinking message.
        # The thinking message was sent as a reply to the user's
        # original message, so reply_to_message is set.
        thinking_msg = update.callback_query.message
        original = (
            thinking_msg.reply_to_message
            if thinking_msg and thinking_msg.reply_to_message
            else None
        )
        if original is None or original.from_user is None:
            print("[callback stop] no original message in reply_to_message", flush=True)
            return
        original_user_id = original.from_user.id
        clicker_id = update.effective_user.id
        if clicker_id != original_user_id:
            # Don't reveal that the request was meaningful; just
            # answer with a notice and let the clicker move on.
            print(
                f"[callback stop] rejected: clicker={clicker_id} != "
                f"original={original_user_id}",
                flush=True,
            )
            await update.callback_query.answer(
                "Только автор сообщения может остановить обработку.",
                show_alert=True,
            )
            return
        chat_id = update.effective_chat.id
        key = (chat_id, original_user_id)
        event = _abort_events.get(key)
        if event is None:
            # No in-flight request for this user. Either the
            # handler finished between when the user clicked
            # Stop and when the callback arrived, or this is a
            # stale button (rare — buttons are tied to a
            # specific thinking message which gets deleted on
            # completion).
            print(
                f"[callback stop] no abort_event for {key} "
                f"(handler may have already finished)",
                flush=True,
            )
            try:
                await update.callback_query.edit_message_text("⏹ Уже завершено")
            except Exception:
                pass
            return
        event.set()
        print(
            f"[callback stop] abort_event set for {key}",
            flush=True,
        )
        # Edit the thinking message to "⏹ Остановлено" immediately
        # so the user sees the click took effect. The handler will
        # also try to edit it (and may delete it); the second edit
        # is a no-op if Telegram returns "not modified" or the
        # message is already gone.
        try:
            await update.callback_query.edit_message_text("⏹ Остановлено…")
        except Exception as e:
            print(f"[callback stop] edit_text failed: {type(e).__name__}: {e!r}", flush=True)
        return

    # The remaining branches (sub:, newsub:prompt, delsub:) are
    # only emitted by the private-mode /subs inline keyboard.
    # Group mode: no inline buttons exist there.
    if _is_group_chat(update):
        return
    if await reject_if_unauthorized(update, context):
        return
    user_id = update.effective_user.id
    if data.startswith("sub:"):
        thread_id = data[4:]
        existing = await asyncio.to_thread(store.get_thread, user_id, thread_id)
        if existing is None:
            await asyncio.to_thread(store.create_thread, user_id, thread_id)
        await asyncio.to_thread(store.set_active_thread, user_id, thread_id)
        await update.callback_query.edit_message_text(
            f'✅ Switched to sub-talk "{thread_id}".'
        )
    elif data == "newsub:prompt":
        # The button used to fire a ForceReply that asked the user
        # to "send the new sub-talk name" — but the next text
        # message the user typed would be routed through
        # handle_text as a regular question, not a name. The
        # button was confusing (review P2-4). Drop it; the user
        # can just type /newsub <name> directly.
        await update.effective_message.reply_text(
            'Use /newsub <name> to create a new sub-talk.'
        )
    elif data.startswith("delsub:"):
        thread_id = data[7:]
        n = await asyncio.to_thread(store.delete_thread, user_id, thread_id)
        await update.callback_query.edit_message_text(
            f'🗑 Deleted "{thread_id}" and {n} message(s).'
        )
    else:
        await update.callback_query.edit_message_text(
            f'(unknown action: {data!r})'
        )


async def _handle_chat_member_update(upd, bot):
    """Welcome a new group member.

    Triggered by a Telegram chat_member update. We detect a "new
    join" as: old_chat_member.status in {left, kicked} and
    new_chat_member.status in {member, administrator, creator}.

    Actions:
      1. Post WELCOME_TEXT in the General topic of the group,
         as a reply to the service "User X joined" message.
      2. PM the new member with the same text. PMs may fail
         if the user has not started a chat with the bot or
         has strict privacy; we log and continue.
      3. Also handles my_chat_member (the bot itself being
         added/removed) — we just log it for ops visibility.

    No persistence: welcome is one-shot. The user can re-read
    the pinned message in the group.
    """
    cm = upd.chat_member
    chat = cm.chat
    new_user = cm.new_chat_member.user if cm.new_chat_member else None
    old_status = cm.old_chat_member.status if cm.old_chat_member else None
    new_status = cm.new_chat_member.status if cm.new_chat_member else None
    user_id = new_user.id if new_user else None
    username = getattr(new_user, 'username', None) if new_user else None
    first = getattr(new_user, 'first_name', None) if new_user else None
    is_bot_user = getattr(new_user, 'is_bot', False) if new_user else False
    is_forum = getattr(chat, 'is_forum', False)
    print(
        f"[chat_member] chat_id={chat.id} is_forum={is_forum} "
        f"user_id={user_id} username={username!r} is_bot={is_bot_user} "
        f"old_status={old_status!r} new_status={new_status!r}",
        flush=True,
    )
    # Only act on "new join" (left/kicked → member/admin/creator).
    # Skip leaves, promotes, demotes, bans — those are operator
    # actions, not new users.
    is_new_join = (
        old_status in ("left", "kicked")
        and new_status in ("member", "administrator", "creator")
        and user_id is not None
    )
    if not is_new_join:
        return
    # Skip other bots joining. Their operator is responsible for
    # the bot's behaviour, and PMing another bot is wasted work.
    if is_bot_user:
        print(f"[chat_member] skip bot user_id={user_id}", flush=True)
        return
    # Build a mention. Telegram allows @username for users with a
    # public username; fall back to a first_name or just "you".
    if username:
        mention = f"@{username}"
    elif first:
        mention = first
    else:
        mention = "there"
    group_msg = f"👋 {mention}, welcome!\n\n{WELCOME_TEXT}"
    # 1) Post in the group. In a forum supergroup, the General
    # topic has message_thread_id=None. We do NOT use
    # _reply() here because that helper is wired to a specific
    # message_id in a specific topic; we want a fresh message
    # in General, not a reply in some specific topic.
    try:
        await bot.send_message(
            chat_id=chat.id,
            text=group_msg,
            message_thread_id=None,  # explicit: General topic
        )
        print(
            f"[chat_member] posted welcome in chat_id={chat.id} "
            f"for user_id={user_id}",
            flush=True,
        )
    except Exception as e:
        print(
            f"[chat_member] FAILED to post welcome in chat_id={chat.id}: "
            f"{type(e).__name__}: {e!r}",
            flush=True,
        )
    # 2) PM the new member. Telegram allows this because the
    # user is now in a common group with the bot. If their
    # privacy settings block PMs from non-contacts, this will
    # fail with Forbidden; we just log and continue.
    try:
        await bot.send_message(
            chat_id=user_id,
            text=WELCOME_TEXT,
        )
        print(
            f"[chat_member] PM'd welcome to user_id={user_id}",
            flush=True,
        )
    except Exception as e:
        print(
            f"[chat_member] PM to user_id={user_id} FAILED: "
            f"{type(e).__name__}: {e!r}",
            flush=True,
        )


async def _dispatch_update(upd_dict):
    global _dispatcher
    if _dispatcher is None:
        app = Application.builder().token(BOT_TOKEN).build()
        app.add_handler(CommandHandler("start", start))
        app.add_handler(CommandHandler("reset", reset))
        app.add_handler(CommandHandler("stats", stats))
        app.add_handler(CommandHandler("newsub", cmd_newsub))
        app.add_handler(CommandHandler("sub", cmd_sub))
        app.add_handler(CommandHandler("subs", cmd_subs))
        app.add_handler(CommandHandler("delsub", cmd_delsub))
        app.add_handler(CommandHandler("here", cmd_here))
        app.add_handler(CommandHandler("help", cmd_help))
        app.add_handler(CallbackQueryHandler(cmd_callback))
        app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
        app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
        app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

        async def log_error(update, context):
            err = context.error
            print(f"[handler error] {type(err).__name__}: {err!r}", flush=True)

        app.add_error_handler(log_error)
        await app.initialize()
        _dispatcher = app
        print("[dispatch] Application initialized (lazy)", flush=True)
        # PTB 21 requires Application.initialize() before process_update.
        # We do not call app.start() — the hand-rolled polling loop drives
        # dispatch. initialize() sets up the application context.
        await _dispatcher.initialize()

    upd = Update.de_json(upd_dict, _dispatcher.bot)
    if upd is None:
        print(f"[dispatch] de_json returned None for {upd_dict}", flush=True)
        return
    # === chat_member (new join) handler ===
    # We handle chat_member updates BEFORE PTB's process_update
    # because PTB may not have a handler registered for them, and
    # we want full control of the welcome flow. The bot detects
    # new members joining the group and sends the WELCOME_TEXT
    # (a) in the General topic, and (b) as a PM to the new
    # member. This ensures the rating rules are the first
    # information the new member (especially an LLM-claimed
    # account) receives after joining.
    if getattr(upd, 'chat_member', None) is not None:
        await _handle_chat_member_update(upd, _dispatcher.bot)
        return
    txt = upd.message.text if (upd.message and upd.message.text) else None
    print(f"[dispatch] update_id={upd.update_id} msg={txt!r}", flush=True)
    # Group-mode noise filter. Apply here (in the dispatch path) so
    # EVERY handler is covered uniformly: message handlers AND
    # command handlers. If a message is from another Telegram bot
    # or contains the "[llm]" convention marker, drop it before
    # any handler runs.
    if upd.message is not None and _should_mute_in_group(upd):
        who = upd.message.from_user
        tag = (
            f"bot @{who.username}" if getattr(who, 'is_bot', False)
            else f"[llm] @{who.username}" if who.username
            else f"id={who.id}"
        )
        print(f"[dispatch] muting group-mode message from {tag}", flush=True)
        return
    # Reply-chain filter: skip messages that are replies to other
    # humans in a group. The bot should not insert itself into
    # human-to-human conversation threads. Replies to the bot's
    # own messages and commands are exempt.
    if upd.message is not None and _is_reply_to_other_user(upd, _dispatcher.bot.id):
        rtm = upd.message.reply_to_message
        rtm_from = rtm.from_user if rtm and rtm.from_user else None
        tag = (
            f"@{rtm_from.username}" if rtm_from and rtm_from.username
            else f"id={rtm_from.id}" if rtm_from
            else "?"
        )
        print(
            f"[dispatch] skipping reply-to-other-user (rtm_from={tag}) "
            f"update_id={upd.update_id}",
            flush=True,
        )
        return
    await _dispatcher.process_update(upd)
    print(f"[dispatch] processed update_id={upd.update_id}", flush=True)


# === Self-test: inject a fake Update to verify handlers actually fire ===
# Conversation key for the selftest. Deliberately NOT a real user_id:
#   - avoids leaking test data into a whitelisted user's history
#   - avoids matching any real Telegram account that might exist
#   - bypasses reject_if_unauthorized() (the smoke test is about
#     infrastructure, not authorization)
SELFTEST_KEY = 'selftest:'
# Fake Telegram user_id for the synthetic /start. id=0 is reserved by
# Telegram and not a real account.
SELFTEST_USER_ID = 0


async def _selftest():
    """Skip in production — only used via env LLAMABOT_SELFTEST=1."""
    if os.environ.get('LLAMABOT_SELFTEST') != '1':
        return
    import time
    print('[selftest] starting', flush=True)
    fake = {
        "update_id": 999_999_001,
        "message": {
            "message_id": 1,
            "date": int(time.time()),
            "chat": {"id": SELFTEST_USER_ID, "type": "private"},
            "from": {"id": SELFTEST_USER_ID, "is_bot": False, "first_name": "selftest"},
            "text": "/start",
        },
    }
    await _dispatch_update(fake)
    # Note: in LOCKDOWN (no ALLOWED_USER_IDS) or when SELFTEST_USER_ID
    # is not whitelisted, reject_if_unauthorized() silently no-ops the
    # /start. That is the expected behaviour for a non-LOCKDOWN
    # deploy with the bot operator's id not in the whitelist; the
    # /start smoke is informational only. The vision test below
    # exercises the LLM path independently of the whitelist.

    # === Vision test: call call_llama directly with a real PNG ===
    # Bypasses handle_photo (which needs a real Telegram file_id) but
    # exercises the same data:image/png;base64,... path the handler uses.
    print('[selftest] running vision test...', flush=True)
    png_bytes = bytes.fromhex(
        '89504e470d0a1a0a0000000d49484452000000010000000108020000'
        '0090773d780000000c4944415478da6300010000050001'
        '0d0a2db40000000049454e44ae426082'
    )
    photo_b64 = base64.b64encode(png_bytes).decode('ascii')
    data_url = f"data:image/png;base64,{photo_b64}"
    import bot as _b
    # Inject the photo message directly into the SELFTEST conversation,
    # NOT into a real user's history.
    vision_msg = {
        "role": "user",
        "content": [
            {"type": "text", "text": "проверь математику"},
            {"type": "image_url", "image_url": {"url": data_url}}
        ]
    }
    # Persist into the SQLite-backed store under SELFTEST_KEY so the
    # selftest follows the same code path as a real user. The
    # self-import dance is intentional — _selftest runs in the
    # module's own async context, so going through `store` (the
    # module-level Storage instance) would race with concurrent
    # handler writes. Accessing _b.store guarantees we hit the
    # exact same singleton.
    await asyncio.to_thread(_b.store.create_thread, 0, SELFTEST_KEY)
    await asyncio.to_thread(_b.store.add_message, 0, SELFTEST_KEY, "user", json.dumps(vision_msg, ensure_ascii=False))
    # Diagnostic: dump what we're about to send
    import json as _j
    _b_bytes = _j.dumps(vision_msg, ensure_ascii=False).encode('utf-8')
    print(f"[selftest] vision_msg before call_llama: {len(_b_bytes)} bytes, head={_b_bytes[:200]!r}", flush=True)
    history = await asyncio.to_thread(_b.store.get_messages, 0, SELFTEST_KEY, 20)
    parsed_history = []
    for r in history:
        try:
            parsed_history.append(json.loads(r["content"]))
        except Exception:
            parsed_history.append({"role": r["role"], "content": r["content"]})
    # Best-effort: a smoke test that takes the production process
    # down with it is not a smoke test. Catch every exception, log
    # it, keep going. The production bot should be able to start
    # even if llama-server is unreachable at boot.
    try:
        result = await _b.call_llama(
            parsed_history,
            max_tokens=2048,
            user_text="проверь математику",
            thinking_msg=None,
            use_stream=False,
        )
        print(f"[selftest] call_llama result: {len(result)} chars, head={result[:300]!r}", flush=True)
    except Exception as e:
        print(f"[selftest] FAILED: {type(e).__name__}: {e}", flush=True)
        import traceback
        traceback.print_exc()

    # === Group-mode routing smoke test ===
    # Pure-Python check that _is_group_chat() correctly classifies
    # the three cases that drove commit 4 (the chat.is_forum hotfix):
    #   - private chat, no forum: False
    #   - forum-enabled supergroup, message in General: True
    #   - forum-enabled supergroup, message in a regular topic: True
    # We mock the minimum amount of PTB structure to feed _is_group_chat.
    print('[selftest] running group-mode routing test...', flush=True)
    class _FakeChat:
        def __init__(self, type_, is_forum=None, chat_id=0):
            self.type = type_
            self.id = chat_id
            if is_forum is not None:
                self.is_forum = is_forum
    class _FakeMessage:
        def __init__(self, chat, message_thread_id=None):
            self.chat = chat
            self.message_thread_id = message_thread_id
    class _FakeUpdate:
        def __init__(self, chat, message):
            self.effective_chat = chat
            self.message = message

    cases = [
        # (label, chat_type, is_forum, message_thread_id, expected_is_group)
        ('private 1:1',             'private',    None, None, False),
        ('private with message_id', 'private',    None, None, False),
        ('group (legacy, no forum)', 'group',      None, None, True),
        ('supergroup, no forum',    'supergroup', False, None, True),
        ('supergroup + forum + General',  'supergroup', True, None, True),
        ('supergroup + forum + topic 42', 'supergroup', True, 42,   True),
    ]
    all_ok = True
    for label, chat_type, is_forum, thread_id, expected in cases:
        chat = _FakeChat(chat_type, is_forum, chat_id=-1001 if chat_type != 'private' else 1)
        msg = _FakeMessage(chat, thread_id)
        upd = _FakeUpdate(chat, msg)
        got = _b._is_group_chat(upd)
        ok = (got == expected)
        all_ok &= ok
        print(f"  [{'OK' if ok else 'FAIL'}] {label}: is_group={got} (expected {expected})", flush=True)

    # === Storage: known_topics round-trip ===
    # Add a topic, list it, find it by name, remove it. Catches
    # typos in the SQL and the new known_topics methods (commit 8).
    print('[selftest] running known_topics round-trip...', flush=True)
    SELFTEST_CHAT = -1001
    SELFTEST_TOPIC_ID = 999999
    try:
        await asyncio.to_thread(
            _b.store.add_known_topic, SELFTEST_CHAT, SELFTEST_TOPIC_ID, 'selftest-topic'
        )
        topics = await asyncio.to_thread(_b.store.list_known_topics, SELFTEST_CHAT)
        names = sorted(t["name"] for t in topics)
        if 'selftest-topic' not in names:
            print(f"  [FAIL] add_known_topic didn't persist (got {names})", flush=True)
            all_ok = False
        else:
            print(f"  [OK] add_known_topic persisted", flush=True)
        found = await asyncio.to_thread(
            _b.store.find_known_topic_by_name, SELFTEST_CHAT, 'selftest-topic'
        )
        if found is None or found["message_thread_id"] != SELFTEST_TOPIC_ID:
            print(f"  [FAIL] find_known_topic_by_name returned {found}", flush=True)
            all_ok = False
        else:
            print(f"  [OK] find_known_topic_by_name returned id={found['message_thread_id']}", flush=True)
        await asyncio.to_thread(
            _b.store.remove_known_topic, SELFTEST_CHAT, SELFTEST_TOPIC_ID
        )
        topics_after = await asyncio.to_thread(_b.store.list_known_topics, SELFTEST_CHAT)
        if any(t["name"] == 'selftest-topic' for t in topics_after):
            print(f"  [FAIL] remove_known_topic didn't remove (got {[t['name'] for t in topics_after]})", flush=True)
            all_ok = False
        else:
            print(f"  [OK] remove_known_topic worked", flush=True)
        # find_known_topic_by_id: re-add, then look up by id (not name).
        await asyncio.to_thread(
            _b.store.add_known_topic, SELFTEST_CHAT, SELFTEST_TOPIC_ID, 'selftest-topic'
        )
        by_id = await asyncio.to_thread(
            _b.store.find_known_topic_by_id, SELFTEST_CHAT, SELFTEST_TOPIC_ID
        )
        if by_id is None or by_id["name"] != 'selftest-topic':
            print(f"  [FAIL] find_known_topic_by_id returned {by_id}", flush=True)
            all_ok = False
        else:
            print(f"  [OK] find_known_topic_by_id resolved to '{by_id['name']}'", flush=True)
        # Negative case: a non-existent id.
        missing = await asyncio.to_thread(
            _b.store.find_known_topic_by_id, SELFTEST_CHAT, 999999999
        )
        if missing is not None:
            print(f"  [FAIL] find_known_topic_by_id returned non-None for missing id: {missing}", flush=True)
            all_ok = False
        else:
            print(f"  [OK] find_known_topic_by_id returns None for missing id", flush=True)
        # Cleanup
        await asyncio.to_thread(
            _b.store.remove_known_topic, SELFTEST_CHAT, SELFTEST_TOPIC_ID
        )
    except Exception as e:
        print(f"  [FAIL] known_topics round-trip crashed: {type(e).__name__}: {e}", flush=True)
        all_ok = False

    # === Storage: lazy trim in get_messages ===
    # Verifies that the row count per (chat_id, thread_id) is
    # bounded — adding > TRIM_HIGH messages and then reading
    # should leave the table at TRIM_LOW, not unbounded.
    print('[selftest] running lazy-trim test...', flush=True)
    # storage.py sits next to bot.py in the container (/app/storage.py
    # per the Dockerfile `COPY bot.py storage.py ./`); module name
    # "storage" would clash with stdlib's `storage` if we ever need
    # to import it normally. Use spec_from_file_location to load it
    # under a private name without touching sys.modules globally.
    import importlib.util
    _spec = importlib.util.spec_from_file_location(
        "_selftest_storage", "/app/storage.py"
    )
    _storage_mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_storage_mod)
    TRIM_HIGH = _storage_mod.TRIM_HIGH
    TRIM_LOW = _storage_mod.TRIM_LOW
    LAZY_CHAT = -1002
    LAZY_THREAD = "lazy-test"
    try:
        # Wipe any leftover rows
        await asyncio.to_thread(_b.store.delete_thread, LAZY_CHAT, LAZY_THREAD)
        # Insert TRIM_HIGH + 50 rows
        for i in range(TRIM_HIGH + 50):
            await asyncio.to_thread(
                _b.store.add_message, LAZY_CHAT, LAZY_THREAD,
                "user", f'{{"role":"user","content":"msg {i}"}}'
            )
        # Count before read
        n_before = (
            await asyncio.to_thread(_b.store.get_messages, LAZY_CHAT, LAZY_THREAD, 100000)
        )
        if len(n_before) != TRIM_LOW:
            print(f"  [FAIL] after add: row count is {len(n_before)}, expected TRIM_LOW={TRIM_LOW}", flush=True)
            all_ok = False
        else:
            print(f"  [OK] lazy trim kept {len(n_before)} rows (== TRIM_LOW)", flush=True)
        # Cleanup
        await asyncio.to_thread(_b.store.delete_thread, LAZY_CHAT, LAZY_THREAD)
    except Exception as e:
        print(f"  [FAIL] lazy-trim test crashed: {type(e).__name__}: {e}", flush=True)
        all_ok = False

    # === Storage: rating column (v3 schema) ===
    # add_message with rating=N must persist N; get_messages
    # must return it. Round-trip.
    print('[selftest] running rating round-trip...', flush=True)
    RATING_CHAT = -1003
    RATING_THREAD = "rating-test"
    try:
        await asyncio.to_thread(_b.store.delete_thread, RATING_CHAT, RATING_THREAD)
        await asyncio.to_thread(
            _b.store.add_message, RATING_CHAT, RATING_THREAD,
            "user", '{"role":"user","content":"x"}',
        )
        await asyncio.to_thread(
            _b.store.add_message, RATING_CHAT, RATING_THREAD,
            "assistant", '{"role":"assistant","content":""}',
            rating=7,
        )
        await asyncio.to_thread(
            _b.store.add_message, RATING_CHAT, RATING_THREAD,
            "assistant", '{"role":"assistant","content":"hi"}',
            rating=None,  # no rating for normal text response
        )
        msgs = await asyncio.to_thread(
            _b.store.get_messages, RATING_CHAT, RATING_THREAD, 10
        )
        # ms2_msgs[0] is oldest, [1] is rated, [2] is unrated
        if len(msgs) != 3:
            print(f"  [FAIL] expected 3 rows, got {len(msgs)}", flush=True)
            all_ok = False
        elif msgs[1].get("rating") != 7:
            print(f"  [FAIL] rated message has rating={msgs[1].get('rating')!r}, expected 7", flush=True)
            all_ok = False
        elif msgs[2].get("rating") is not None:
            print(f"  [FAIL] unrated message has rating={msgs[2].get('rating')!r}, expected None", flush=True)
            all_ok = False
        else:
            print(f"  [OK] rating 7 persisted on row 1, None on row 2", flush=True)
        # Cleanup
        await asyncio.to_thread(_b.store.delete_thread, RATING_CHAT, RATING_THREAD)
    except Exception as e:
        print(f"  [FAIL] lazy-trim test crashed: {type(e).__name__}: {e}", flush=True)
        all_ok = False

    print(f"[selftest] group-mode tests: {'all pass' if all_ok else 'FAILED'}", flush=True)

    # === Group-mode noise filter: _should_mute_in_group ===
    # Verifies that the bot stays silent on messages from other
    # Telegram bots OR messages that contain the "[llm]" marker
    # (in group mode). In private mode the [llm] marker is a
    # no-op because there is no "floor" to yield.
    print('[selftest] running noise-filter test...', flush=True)
    class _FakeFromUser:
        def __init__(self, is_bot, username='someone', uid=1):
            self.is_bot = is_bot
            self.username = username
            self.id = uid
    class _FakeMessage2:
        def __init__(self, text, from_user):
            self.text = text
            self.caption = None
            self.from_user = from_user
    class _FakeChat2:
        def __init__(self, type_, is_forum=False):
            self.type = type_
            self.is_forum = is_forum
    class _FakeUpdate2:
        def __init__(self, text, from_user, chat):
            self.message = _FakeMessage2(text, from_user)
            self.effective_chat = chat

    # Group mode (chat.is_forum=True so the [llm] check applies)
    group_chat = _FakeChat2('supergroup', is_forum=True)
    private_chat = _FakeChat2('private')
    mute_cases = [
        # (label, from_user, text, chat, expected_mute)
        # --- group mode ---
        ('group: human, no marker',         _FakeFromUser(False, 'human'),    'hi there',          group_chat,   False),
        ('group: human, contains [llm]',    _FakeFromUser(False, 'rogue-ai'), 'I am [llm] ready',  group_chat,   True),
        ('group: other Telegram bot',       _FakeFromUser(True,  'ClaudeBot'), 'whatever',          group_chat,   True),
        ('group: bot with [llm] too',       _FakeFromUser(True,  'GPTBot'),   '[llm] answer',      group_chat,   True),
        ('group: case-insensitive',         _FakeFromUser(False, 'human'),    'this is [LLM] here',group_chat,   True),
        # --- word-boundary: must NOT mute non-token occurrences ---
        ('group: [llm] inside word "x[llm]s"',     _FakeFromUser(False, 'human'), 'x[llm]s are bad',  group_chat,   False),
        ('group: [llm] in URL "https://x/[llm]"',  _FakeFromUser(False, 'human'), 'see https://x/[llm] page',  group_chat, False),
        ('group: [llm] glued to period "done.[llm]."',  _FakeFromUser(False, 'human'), 'done.[llm].',  group_chat,   False),
        ('group: [llm] at start, period after "[llm]."', _FakeFromUser(False, 'human'), '[llm].',     group_chat,   True),
        ('group: [llm] with spaces',                _FakeFromUser(False, 'human'), 'hello. [llm] bye', group_chat, True),
        ('group: [llm] at end of string',           _FakeFromUser(False, 'human'), 'bye [llm]',     group_chat,   True),
        # --- private mode ([llm] should NOT mute) ---
        ('private: human, contains [llm]',  _FakeFromUser(False, 'human'),    '[llm] playing',     private_chat, False),
        # --- defensive: no from_user ---
        ('group: no from_user',             None,                              'hi',                group_chat,   False),
    ]
    mute_ok = True
    for label, from_user, text, chat, expected in mute_cases:
        upd = _FakeUpdate2(text, from_user, chat)
        got = _b._should_mute_in_group(upd)
        ok = (got == expected)
        mute_ok &= ok
        print(f"  [{'OK' if ok else 'FAIL'}] {label}: mute={got} (expected {expected})", flush=True)
    all_ok &= mute_ok
    print(f"[selftest] noise-filter tests: {'all pass' if mute_ok else 'FAILED'}", flush=True)

    # === Reply-to-other-user filter ===
    # Verifies that _is_reply_to_other_user() correctly classifies
    # the four cases:
    #   - top-level message → False (bot responds)
    #   - reply to bot's own message → False (bot responds)
    #   - reply to another human in a group → True (bot skips)
    #   - command in reply context → False (commands always work)
    # Plus a private-chat baseline: reply to anyone → False (1:1).
    print('[selftest] running reply-to-other-user filter test...', flush=True)
    rpl_ok = True
    # Build minimal Update-like objects with just .message, .chat, .from_user,
    # .reply_to_message. _is_group_chat reads chat.type and chat.is_forum.
    class _RU:
        """Minimal Update-like object for _is_reply_to_other_user tests."""
        def __init__(self, chat_type, is_forum, from_id, rtm_from_id=None, text="hi"):
            self.message = _RM(chat_type, is_forum, from_id, rtm_from_id, text) if from_id is not None else None
            # _is_group_chat reads update.effective_chat, so expose it
            self.effective_chat = self.message.chat if self.message else None
    class _RC:
        def __init__(self, chat_type, is_forum):
            self.type = chat_type
            self.is_forum = is_forum
    class _RM:
        def __init__(self, chat_type, is_forum, from_id, rtm_from_id, text):
            self.chat = _RC(chat_type, is_forum)
            self.from_user = _User(from_id) if from_id is not None else None
            if rtm_from_id is not None:
                self.reply_to_message = _RM_dummy(rtm_from_id)
            else:
                self.reply_to_message = None
            self.text = text
            self.caption = None
    class _User:
        def __init__(self, uid):
            self.id = uid
            self.is_bot = False
            self.username = None
            self.first_name = "x"
    class _RM_dummy:
        def __init__(self, from_id):
            self.from_user = _User(from_id)
            self.text = "orig"
    BOT_ID = 999_999_001
    cases = [
        # (label, group_type, is_forum, from_id, rtm_from_id, text, expected)
        ("top-level group: human, no reply", "supergroup", True, 10, None, "hello", False),
        ("reply to bot in group: human", "supergroup", True, 10, BOT_ID, "ok", False),
        ("reply to another human in group", "supergroup", True, 10, 20, "agreed", True),
        ("command in reply to other human", "supergroup", True, 10, 20, "/reset", False),
        ("command in reply to bot", "supergroup", True, 10, BOT_ID, "/newsub foo", False),
        ("top-level private: human, no reply", "private", False, 10, None, "hello", False),
        ("reply in private: to bot", "private", False, 10, BOT_ID, "hi", False),
        ("forum General: top-level", "supergroup", True, 10, None, "ok", False),
        ("forum General: reply to human", "supergroup", True, 10, 20, "right", True),
    ]
    for label, gtype, iforum, fid, rtm_id, txt, expected in cases:
        upd = _RU(gtype, iforum, fid, rtm_id, txt)
        got = _b._is_reply_to_other_user(upd, BOT_ID)
        if got != expected:
            print(f"  [FAIL] {label}: got={got} expected={expected}", flush=True)
            rpl_ok = False
        else:
            print(f"  [OK] {label}: skip={got}", flush=True)
    all_ok &= rpl_ok
    print(f"[selftest] reply-to-other-user tests: {'all pass' if rpl_ok else 'FAILED'}", flush=True)

    # === Per-user semaphore ===
    # Verifies that _check_user_slot returns None when all
    # permits are taken. We hold N=2 permits, then try a 3rd.
    print('[selftest] running per-user semaphore test...', flush=True)
    sem_ok = True
    try:
        sem = _b._user_semaphore(123, 456)
        # Manually lock it to simulate 2 in-flight requests
        for _ in range(_b._PER_USER_SEMAPHORE_LIMIT):
            await sem.acquire()
        # Third call should report locked
        if not sem.locked():
            print(f"  [FAIL] semaphore not locked after {_b._PER_USER_SEMAPHORE_LIMIT} acquires", flush=True)
            sem_ok = False
        else:
            print(f"  [OK] semaphore locked at limit", flush=True)
        # Release them
        for _ in range(_b._PER_USER_SEMAPHORE_LIMIT):
            sem.release()
        if sem.locked():
            print(f"  [FAIL] semaphore still locked after release", flush=True)
            sem_ok = False
        else:
            print(f"  [OK] semaphore unlocked after release", flush=True)
        # Distinct (chat_id, user_id) gets a distinct semaphore
        sem_a = _b._user_semaphore(1, 1)
        sem_b = _b._user_semaphore(1, 2)
        if sem_a is sem_b:
            print(f"  [FAIL] (1,1) and (1,2) share the same semaphore", flush=True)
            sem_ok = False
        else:
            print(f"  [OK] (1,1) and (1,2) have distinct semaphores", flush=True)
    except Exception as e:
        print(f"  [FAIL] semaphore test crashed: {type(e).__name__}: {e}", flush=True)
        sem_ok = False
    all_ok &= sem_ok

    # === Rating-mode parser ===
    # Verifies that _parse_rating_response correctly extracts
    # the type, rating, and rest text from LLM responses.
    print('[selftest] running rating-parser test...', flush=True)
    parse_cases = [
        # (input, expected_type, expected_rating, expected_rest_substr)
        # Default: no prefix → treated as question with full text as answer
        ('plain answer',                  'question',                      None, 'plain answer'),
        ('[[TYPE:question]] hello',       'question',                      None, 'hello'),
        ('[[TYPE:request]] done, recipe', 'request',                       None, 'done, recipe'),
        ('[[TYPE:confirmation]] yes',     'confirmation',                  None, 'yes'),
        ('[[TYPE:info]] [[RATE:7]]',      'info',                          7, ''),
        ('[[TYPE:info]] [[RATE:10]] good', 'info',                         10, 'good'),
        ('[[TYPE:statement]] [[RATE:3]]', 'statement',                     3, ''),
        ('[[TYPE:bloat]] 🤔',             'bloat',                          None, ''),
        ('[[TYPE:bloat]]',                'bloat',                          None, ''),  # no emoji → default
        # Invalid type → fall back to "question" with whole text
        ('[[TYPE:unknown]]',              'question',                      None, '[[TYPE:unknown]]'),
    ]
    parse_ok = True
    for text, exp_type, exp_rating, exp_rest in parse_cases:
        got = _b._parse_rating_response(text)
        if got['type'] != exp_type or got['rating'] != exp_rating:
            print(f"  [FAIL] {text!r}: type={got['type']!r} rating={got['rating']!r} rest={got['rest']!r} "
                  f"(expected type={exp_type!r}, rating={exp_rating!r})", flush=True)
            parse_ok = False
        elif exp_rest and exp_rest not in got['rest']:
            print(f"  [FAIL] {text!r}: rest={got['rest']!r} doesn't contain {exp_rest!r}", flush=True)
            parse_ok = False
    if parse_ok:
        print(f"  [OK] all {len(parse_cases)} parse cases passed", flush=True)
    all_ok &= parse_ok

    # === RATING_EMOJI map ===
    # All 10 ratings must map to a non-empty string and be
    # distinct. Sanity check.
    print('[selftest] running rating-emoji map test...', flush=True)
    emoji_ok = True
    if len(_b.RATING_EMOJI) != 10:
        print(f"  [FAIL] RATING_EMOJI has {len(_b.RATING_EMOJI)} entries, expected 10", flush=True)
        emoji_ok = False
    elif len(set(_b.RATING_EMOJI.values())) != 10:
        print(f"  [FAIL] RATING_EMOJI has duplicate emoji", flush=True)
        emoji_ok = False
    elif any(not e for e in _b.RATING_EMOJI.values()):
        print(f"  [FAIL] RATING_EMOJI has empty emoji", flush=True)
        emoji_ok = False
    else:
        print(f"  [OK] 10 distinct ratings, all map to non-empty emoji", flush=True)
    all_ok &= emoji_ok

    # === chat_member test ===
    # We can't actually trigger a join in a selftest, but we can
    # verify (a) the WELCOME_TEXT is non-empty and contains the
    # rating scale, and (b) the chat_member update path dispatches
    # without crashing. We use a fake chat_member update with a
    # mock bot that records send_message calls instead of hitting
    # the real Telegram API.
    print('[selftest] running chat_member / WELCOME_TEXT test...', flush=True)
    cm_ok = True
    if not _b.WELCOME_TEXT or not _b.WELCOME_TEXT.strip():
        print("  [FAIL] WELCOME_TEXT is empty", flush=True)
        cm_ok = False
    elif '💩' not in _b.WELCOME_TEXT or '🔥' not in _b.WELCOME_TEXT:
        print("  [FAIL] WELCOME_TEXT does not contain rating scale", flush=True)
        cm_ok = False
    elif 'TRUTH' not in _b.WELCOME_TEXT:
        print("  [FAIL] WELCOME_TEXT does not contain 'TRUTH' (rating rule)", flush=True)
        cm_ok = False
    else:
        print("  [OK] WELCOME_TEXT is non-empty, contains rating scale + TRUTH rule", flush=True)
    # Mock bot that records send_message calls
    class _MockBot:
        def __init__(self):
            self.calls = []
        async def send_message(self, chat_id, text, message_thread_id=None):
            self.calls.append({"chat_id": chat_id, "text": text, "thread_id": message_thread_id})
            return None
    # Build a synthetic chat_member update: new join in a forum supergroup
    fake_cm = {
        "update_id": 999_999_777,
        "chat_member": {
            "chat": {"id": -1001234567890, "type": "supergroup", "is_forum": True, "title": "TestGroup"},
            "from": {"id": 1, "is_bot": False, "first_name": "operator"},
            "date": int(time.time()),
            "old_chat_member": {"user": {"id": 555, "is_bot": False, "first_name": "Newbie", "username": "newbie"}, "status": "left"},
            "new_chat_member": {"user": {"id": 555, "is_bot": False, "first_name": "Newbie", "username": "newbie"}, "status": "member"},
        },
    }
    upd = Update.de_json(fake_cm, None)
    mock_bot = _MockBot()
    try:
        await _b._handle_chat_member_update(upd, mock_bot)
        # Expect 2 calls: one to chat_id, one to user_id
        if len(mock_bot.calls) != 2:
            print(f"  [FAIL] expected 2 send_message calls, got {len(mock_bot.calls)}", flush=True)
            cm_ok = False
        else:
            chat_call = next((c for c in mock_bot.calls if c["chat_id"] == -1001234567890), None)
            user_call = next((c for c in mock_bot.calls if c["chat_id"] == 555), None)
            if not chat_call or not user_call:
                print(f"  [FAIL] missing expected calls: {[c['chat_id'] for c in mock_bot.calls]}", flush=True)
                cm_ok = False
            elif chat_call["thread_id"] is not None:
                print(f"  [FAIL] group welcome should be in General (thread_id=None), got {chat_call['thread_id']}", flush=True)
                cm_ok = False
            elif 'WELCOME' not in chat_call["text"] and 'Welcome' not in chat_call["text"]:
                print(f"  [FAIL] group welcome does not contain 'Welcome': {chat_call['text'][:80]!r}", flush=True)
                cm_ok = False
            elif '@newbie' not in chat_call["text"]:
                print(f"  [FAIL] group welcome should mention @newbie, head={chat_call['text'][:200]!r}", flush=True)
                cm_ok = False
            else:
                print("  [OK] group welcome posted in General with @newbie mention", flush=True)
                print("  [OK] PM sent to new user_id", flush=True)
    except Exception as e:
        print(f"  [FAIL] _handle_chat_member_update raised: {type(e).__name__}: {e!r}", flush=True)
        import traceback
        traceback.print_exc()
        cm_ok = False
    # Negative test: chat_member update for a leave (old=member, new=left)
    # should NOT trigger any send_message.
    fake_leave = {
        "update_id": 999_999_778,
        "chat_member": {
            "chat": {"id": -1001234567890, "type": "supergroup", "is_forum": True, "title": "TestGroup"},
            "from": {"id": 1, "is_bot": False, "first_name": "operator"},
            "date": int(time.time()),
            "old_chat_member": {"user": {"id": 666, "is_bot": False, "first_name": "Leaver"}, "status": "member"},
            "new_chat_member": {"user": {"id": 666, "is_bot": False, "first_name": "Leaver"}, "status": "left"},
        },
    }
    upd2 = Update.de_json(fake_leave, None)
    mock_bot2 = _MockBot()
    try:
        await _b._handle_chat_member_update(upd2, mock_bot2)
        if len(mock_bot2.calls) != 0:
            print(f"  [FAIL] leave event triggered {len(mock_bot2.calls)} calls, expected 0", flush=True)
            cm_ok = False
        else:
            print("  [OK] leave event correctly skipped (0 calls)", flush=True)
    except Exception as e:
        print(f"  [FAIL] _handle_chat_member_update(leave) raised: {type(e).__name__}: {e!r}", flush=True)
        cm_ok = False
    # Negative test: another bot joining should NOT trigger
    fake_bot_join = {
        "update_id": 999_999_779,
        "chat_member": {
            "chat": {"id": -1001234567890, "type": "supergroup", "is_forum": True, "title": "TestGroup"},
            "from": {"id": 1, "is_bot": False, "first_name": "operator"},
            "date": int(time.time()),
            "old_chat_member": {"user": {"id": 777, "is_bot": True, "first_name": "SomeBot", "username": "somebot"}, "status": "left"},
            "new_chat_member": {"user": {"id": 777, "is_bot": True, "first_name": "SomeBot", "username": "somebot"}, "status": "member"},
        },
    }
    upd3 = Update.de_json(fake_bot_join, None)
    mock_bot3 = _MockBot()
    try:
        await _b._handle_chat_member_update(upd3, mock_bot3)
        if len(mock_bot3.calls) != 0:
            print(f"  [FAIL] bot-join triggered {len(mock_bot3.calls)} calls, expected 0", flush=True)
            cm_ok = False
        else:
            print("  [OK] bot-join correctly skipped (0 calls)", flush=True)
    except Exception as e:
        print(f"  [FAIL] _handle_chat_member_update(bot-join) raised: {type(e).__name__}: {e!r}", flush=True)
        cm_ok = False
    all_ok &= cm_ok

    # === Edit-replace test ===
    # Verifies the _bot_replies tracking and the new edit-handling
    # logic in the polling loop. We don't run the full polling loop
    # (it requires a live Telegram server), but we exercise the
    # data structure: record a (chat, user_msg) → bot_msg mapping,
    # then verify it can be retrieved and popped.
    print('[selftest] running edit-replace test...', flush=True)
    er_ok = True
    try:
        # Save the existing map and clear it for the test
        import bot as _b
        saved = dict(_b._bot_replies)
        _b._bot_replies.clear()
        # Record a mapping
        _b._bot_replies[(-1001234567890, 100)] = 200
        _b._bot_replies[(-1001234567890, 101)] = 201
        # Lookup works
        if _b._bot_replies.get((-1001234567890, 100)) != 200:
            print("  [FAIL] _bot_replies.get() returned wrong bot_msg_id", flush=True)
            er_ok = False
        else:
            print("  [OK] _bot_replies stores (chat, user_msg) → bot_msg mapping", flush=True)
        # Pop removes the entry
        popped = _b._bot_replies.pop((-1001234567890, 100), None)
        if popped != 200:
            print(f"  [FAIL] pop returned {popped}, expected 200", flush=True)
            er_ok = False
        elif (-1001234567890, 100) in _b._bot_replies:
            print("  [FAIL] entry not actually removed by pop", flush=True)
            er_ok = False
        else:
            print("  [OK] pop removes entry correctly", flush=True)
        # Pop on missing key returns None (no exception)
        if _b._bot_replies.pop((-1001234567890, 99999), None) is not None:
            print("  [FAIL] pop on missing key should return None", flush=True)
            er_ok = False
        else:
            print("  [OK] pop on missing key returns None", flush=True)
        # Restore the saved state
        _b._bot_replies.clear()
        _b._bot_replies.update(saved)
    except Exception as e:
        print(f"  [FAIL] edit-replace test raised: {type(e).__name__}: {e!r}", flush=True)
        er_ok = False
    all_ok &= er_ok
    print(f"[selftest] edit-replace tests: {'all pass' if er_ok else 'FAILED'}", flush=True)

    print('[selftest] done', flush=True)
    print('[selftest] done', flush=True)


if __name__ == '__main__':
    main()
