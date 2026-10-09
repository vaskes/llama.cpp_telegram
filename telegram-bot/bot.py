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
_TOOL_KEYWORDS = (
    # RU
    'погод', 'температур', 'осадк', 'дожд', 'снег', 'ветер',
    'новост', 'что слышно', 'что нового', 'что в мире', 'свеж',
    'найди в интернет', 'найди в сети', 'поищи в интернет', 'поищи в сети',
    'погугли', 'загугли', 'поиск в гугл', 'web search',
    'открой сайт', 'перейди на сайт', 'скачай страниц',
    'скриншот', 'сделай скрин', 'сфоткай сайт',
    'fetch the page', 'crawl the site',
    # EN
    'weather forecast', 'current weather', 'temperature in',
    'news about', 'latest news', 'breaking news',
    'search the web', 'google this', 'web search',
    'open this url', 'read this url', 'fetch the page', 'crawl the site',
    'screenshot', 'capture the page',
)


def _detect_tool_intent(text: str) -> bool:
    """True if the user message looks like it actually needs a tool."""
    if not text:
        return False
    t = text.lower()
    return any(kw in t for kw in _TOOL_KEYWORDS)


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


async def call_llama(messages, max_tokens=65536, user_text='', thinking_msg=None, use_stream=True, shutdown_event=None):
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
    # search/fetch/crawl in any language we support, treat it as a tool
    # turn. Otherwise omit tools entirely so the model just answers.
    tool_intent = _detect_tool_intent(user_text)
    if not tool_intent:
        # No tool intent: hand the model a plain system prompt and skip
        # the tool definitions. Reasoning and content come back clean.
        sys_prompt = {
            'role': 'system',
            'content': (
                'You are a helpful assistant. '
                'Answer in the language of the user. '
                'Be direct and concise.'
            )
        }
        msgs = [sys_prompt] + messages
        req_tools = None
    else:
        sys_prompt = {
            'role': 'system',
            'content': (
                'You are a helpful assistant with access to tools. '
                'When the user asks about weather, news, current events, '
                'or anything requiring fresh data — call the relevant tool. '
                'For weather: use get_weather (wttr.in, always works). '
                'For web: use donsetch_web_search / donsetch_web_fetch / '
                'donsetch_web_crawl / donsetch_web_screenshot. '
                'If a tool returns no useful data, say so honestly and '
                'suggest where the user can find the info themselves. '
                'Do NOT keep retrying the same query with variations. '
                'After getting a tool result, give a clear, concise '
                'answer in the user\'s language.'
            )
        }
        msgs = [sys_prompt] + messages
        req_tools = all_tools
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
            await thinking_msg.edit_text(f"{prefix}{truncated_marker}{body}")
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
                    async for line in r.aiter_lines():
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
    """
    text = (text or '').strip()
    if not text:
        text = EMPTY_RESPONSE_FALLBACK
    await _reply(update, text)


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
        thread_id = str(update.message.message_thread_id)
        n = await asyncio.to_thread(
            store.delete_thread, chat_id, thread_id
        )
        await _reply(update,
            f'🗑 Cleared {n} message(s) in this topic.\n'
            f'Note: the topic itself is unchanged — only the LLM\'s '
            f'memory of past messages in it is wiped.'
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
        thread_id = str(update.message.message_thread_id)
        # We don't have a name lookup helper for forum topics here, but
        # getForumTopics is async. For a quick "where am I" hint, just
        # show the numeric id; the user sees the topic name in the UI.
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
            f'📍 Current topic: #{update.message.message_thread_id}\n'
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
    buttons.append([
        InlineKeyboardButton("➕ New sub-talk", callback_data="newsub:prompt"),
    ])
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
        # Numeric id?
        if arg.isdigit():
            tid = int(arg)
            for t in await asyncio.to_thread(
                store.list_known_topics, update.effective_chat.id
            ):
                if t["message_thread_id"] == tid:
                    target_id = tid
                    target_name = t["name"]
                    break
            if target_id is None:
                await _reply(update, f'❌ No topic with id={arg} in this group.')
                return
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
# Both modes are auto-detected from the incoming Update: if
# message_thread_id is not None, it's a group; otherwise it's a
# private chat. The dispatching helper below applies the right
# rules for each.

_group_mode: bool = False  # module-level flag, set on first group message



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
_LLM_MARK = "[llm]"


def _should_mute_in_group(update) -> bool:
    """True if the bot should stay silent for this message in group mode.

    Returns True when the message is from:
      - another Telegram bot (is_bot=True; Telegram doesn't deliver
        these by default via getUpdates, but defend in depth in case
        a future API change or webhook setup changes that)
      - a human/user that self-marked with the "[llm]" convention
        (an LLM proxy or a non-Telegram LLM that joined via a
        user account)
    The marker is case-insensitive and substring-matched; false
    positives are possible ("the [llm] model is great") but rare
    and harmless (the bot just doesn't reply to that one message).
    """
    msg = update.message
    if msg is None or msg.from_user is None:
        return False
    if getattr(msg.from_user, "is_bot", False):
        return True
    text = (msg.text or msg.caption or "").lower()
    if _LLM_MARK in text:
        return True
    return False


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
    global _group_mode
    msg = update.message
    if _is_group_chat(update):
        # Group mode. Telegram is the source of truth for thread
        # existence; we do not need a DB row to know it exists.
        # For messages in the General topic, message_thread_id is None
        # by Bot API design — use the "general" sentinel so the DB key
        # is stable and conversation history persists.
        _group_mode = True
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
        thinking = await _reply(update, '💭 думаю…')
        print(f"[handle_photo] chat_id={chat_id} thread_id={thread_id!r} history_len={len(history)}", flush=True)
        bot_response = await call_llama(history, max_tokens=16384, user_text=caption, thinking_msg=thinking, use_stream=False)
        print(f"[handle_photo] call_llama returned: {len(bot_response)} chars, head={bot_response[:200]!r}", flush=True)
        await _persist_message(chat_id, thread_id, 'assistant', bot_response)
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
        bot_response = await call_llama(history, max_tokens=16384, user_text=transcript, thinking_msg=thinking)
        await _persist_message(chat_id, thread_id, 'assistant', bot_response)
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


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result = await _route_to_thread(update, context)
    if result is None:
        return
    chat_id, thread_id, is_group = result
    user_id = chat_id  # alias for log lines below
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
        thinking = await _reply(update, '💭 думаю…')
        bot_response = await call_llama(history, max_tokens=16384, user_text=caption, thinking_msg=thinking)
        await _persist_message(chat_id, thread_id, 'assistant', bot_response)
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


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print(f"[handle_text] ENTRY user_id={update.effective_user.id} chat_type={update.effective_chat.type} is_forum={getattr(update.effective_chat, 'is_forum', None)} thread_id={update.message.message_thread_id} text={update.message.text!r}", flush=True)
    result = await _route_to_thread(update, context)
    if result is None:
        print(f"[handle_text] REJECTED {update.effective_user.id}", flush=True)
        return
    chat_id, thread_id, is_group = result
    user_id = chat_id  # alias for log lines below
    user_message = update.message.text
    try:
        await update.message.chat.send_action(action='typing')
    except Exception as e:
        print(f"[handle_text] send_action FAILED: {type(e).__name__}: {e!r}", flush=True)
    await _persist_message(chat_id, thread_id, 'user', user_message)
    history = await _load_history(chat_id, thread_id)
    print(f"[handle_text] user_id={user_id} thread_id={thread_id!r} history_len={len(history)}", flush=True)
    thinking = await _reply(update, '💭 думаю…')
    try:
        bot_response = await call_llama(history, max_tokens=32768, user_text=user_message, thinking_msg=thinking)
        await _persist_message(chat_id, thread_id, 'assistant', bot_response)
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
                                "allowed_updates": '["message","edited_message"]',
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
                                # FULL dump so we can see exactly what Telegram sent.
                                print(f"[poll] RAW update: {json.dumps(upd_dict, ensure_ascii=False)[:600]}", flush=True)
                                offset = upd_dict["update_id"] + 1
                                # edited_message handling:
                                #
                                # Telegram re-delivers edited messages as
                                # `edited_message` updates (separate from
                                # the original `message`). The bot used to
                                # skip them entirely, but the UX was bad:
                                # users press up-arrow + Send in the
                                # Telegram input, and that produces an
                                # edited_message (not a fresh `message`).
                                # The bot's silence looked like a bug.
                                #
                                # Compromise: only process edited_message
                                # if the new text is a command (starts with
                                # `/`). Edits of regular text are dropped
                                # (we already answered the original; a
                                # follow-up would be confusing or duplicate
                                # work). Commands, by contrast, are
                                # idempotent enough that re-running them
                                # is fine.
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
                                        # Edit of a non-command message —
                                        # already handled when first sent.
                                        print(
                                            f"[poll] skipping edited non-command "
                                            f"update_id={upd_dict['update_id']}",
                                            flush=True,
                                        )
                                        continue
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
    """Handle inline-keyboard button presses from /subs and /here.

    callback_data format:
      sub:<name>     — switch to sub-talk <name>
      newsub:prompt  — show a one-time reply keyboard asking for the new name
      delsub:<name>  — confirm-then-delete (we delete immediately; no confirm step)

    Group mode: callback_data is only emitted by the private-mode
    /subs inline keyboard, so any callback here is a stale/private
    state leak. Just answer and ignore.
    """
    await update.callback_query.answer()
    # Group mode: no inline buttons exist there.
    if _is_group_chat(update):
        return
    if await reject_if_unauthorized(update, context):
        return
    user_id = update.effective_user.id
    data = update.callback_query.data or ""
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
        from telegram import ForceReply
        await update.effective_message.reply_text(
            'Send the new sub-talk name (letters, digits, _-. only, '
            'max 32 chars).',
            reply_markup=ForceReply(selective=True),
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
    except Exception as e:
        print(f"  [FAIL] known_topics round-trip crashed: {type(e).__name__}: {e}", flush=True)
        all_ok = False

    print(f"[selftest] group-mode tests: {'all pass' if all_ok else 'FAILED'}", flush=True)

    # === Group-mode noise filter: _should_mute_in_group ===
    # Verifies that the bot stays silent on messages from other
    # Telegram bots OR messages that contain the "[llm]" marker.
    # This is the group-mode etiquette: when another LLM is
    # actively answering in the room, our bot doesn't double up.
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
    class _FakeUpdate2:
        def __init__(self, text, from_user):
            self.message = _FakeMessage2(text, from_user)
            self.effective_chat = None

    mute_cases = [
        # (label, from_user, text, expected_mute)
        ('human, no marker',         _FakeFromUser(False, 'human'),    'hi there',          False),
        ('human, contains [llm]',    _FakeFromUser(False, 'rogue-ai'), 'I am [llm] ready',  True),
        ('other Telegram bot',       _FakeFromUser(True,  'ClaudeBot'), 'whatever',          True),
        ('bot with [llm] too',       _FakeFromUser(True,  'GPTBot'),   '[llm] answer',      True),
        ('case-insensitive',         _FakeFromUser(False, 'human'),    'this is [LLM] here',True),
        ('text with no from_user',   None,                              'hi',                False),
    ]
    mute_ok = True
    for label, from_user, text, expected in mute_cases:
        upd = _FakeUpdate2(text, from_user)
        got = _b._should_mute_in_group(upd)
        ok = (got == expected)
        mute_ok &= ok
        print(f"  [{'OK' if ok else 'FAIL'}] {label}: mute={got} (expected {expected})", flush=True)
    all_ok &= mute_ok
    print(f"[selftest] noise-filter tests: {'all pass' if mute_ok else 'FAILED'}", flush=True)
    print('[selftest] done', flush=True)
    print('[selftest] done', flush=True)


if __name__ == '__main__':
    main()
