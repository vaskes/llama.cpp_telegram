import os
import json
import re
import base64
import tempfile
import urllib.parse
import socket
import httpx
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

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

# Configuration
BOT_TOKEN = os.environ.get('BOT_TOKEN')
LLAMA_URL = os.environ.get('LLAMA_URL', 'http://192.168.10.7:8080/v1')
WHISPER_URL = os.environ.get('WHISPER_URL', 'http://192.168.10.7:8000')
SEARXNG_URL = os.environ.get('SEARXNG_URL', 'http://localhost:8888')
API_KEY = os.environ.get('API_KEY', 'sk-no-key')
MODEL = os.environ.get('MODEL', 'Qwen3.6-35B-A3B-Heretic')

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
    print('[SECURITY] ALLOWED_USER_IDS and ALLOWED_USERNAMES both empty -> LOCKDOWN (reject all).')
else:
    LOCKDOWN = False
    print(f'[SECURITY] whitelist: {len(ALLOWED_USER_IDS)} ids, {len(ALLOWED_USERNAMES)} usernames')


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
    print(f'[SECURITY] rejected id={uid} {uname} msg={snippet!r}')
    return True

# Conversation context storage
conversations = {}

# Tools the bot does NOT execute (security, or not implemented)
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

# Tools cache (loaded once)
_TOOLS_CACHE = None


async def fetch_tools_from_llama():
    """Fetch tools list from llama-server and filter to the ones we can handle."""
    global _TOOLS_CACHE
    if _TOOLS_CACHE is not None:
        return _TOOLS_CACHE
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
        print(f"[tools] loaded {len(openai_tools)} enabled tools (from {len(all_tools)} total)")
        return openai_tools
    except Exception as e:
        print(f"[tools] failed to fetch: {e}")
        return []


async def execute_searxng_search(args):
    """searxng_search: execute an HTTP request to SearXNG."""
    query = args.get('query', '')
    if not query:
        return '[tool error: empty query]'
    max_results = int(args.get('max_results', 8))
    engines = args.get('engines', '')
    params = {
        'q': query,
        'format': 'json',
        'language': 'ru',
    }
    if engines:
        params['engines'] = engines
    url = f"{SEARXNG_URL}/search?{urllib.parse.urlencode(params)}"
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.get(url)
            r.raise_for_status()
            data = r.json()
        results = data.get('results', [])
        if not results:
            return f'[searxng: 0 results for "{query}". SearXNG engines may be blocked (CAPTCHA) on this IP.]'
        lines = [f'Found {len(results)} results for "{query}":']
        for i, res in enumerate(results[:max_results], 1):
            title = res.get('title', '(no title)')[:120]
            snippet = (res.get('content') or res.get('snippet') or '')[:400]
            link = res.get('url', '')
            lines.append(f'\n{i}. {title}\n   {snippet}\n   {link}')
        if data.get('unresponsive_engines'):
            lines.append(f"\n[unavailable engines: {data['unresponsive_engines']}]")
        return '\n'.join(lines)
    except Exception as e:
        return f'[searxng error: {e}]'


async def execute_searxng_fetch_url(args):
    """searxng_fetch_url: download and return the text of a URL."""
    url = args.get('url', '')
    if not url:
        return '[tool error: empty url]'
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            r = await client.get(url, headers={'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36'})
            r.raise_for_status()
            text = r.text
            text = re.sub(r'<script[^>]*>.*?</script>', ' ', text, flags=re.DOTALL | re.IGNORECASE)
            text = re.sub(r'<style[^>]*>.*?</style>', ' ', text, flags=re.DOTALL | re.IGNORECASE)
            text = re.sub(r'<[^>]+>', ' ', text)
            text = re.sub(r'\s+', ' ', text).strip()
            return text[:8000]
    except Exception as e:
        return f'[fetch error: {e}]'


async def execute_searxng_engines(args):
    """searxng_engines: return the list of available engines."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"{SEARXNG_URL}/engines")
            r.raise_for_status()
            data = r.json()
        names = sorted([e.get('name', '?') for e in data])
        return f'Available SearXNG engines ({len(names)}): ' + ', '.join(names)
    except Exception as e:
        return f'[engines error: {e}]'


async def get_weather(args):
    """Custom tool: wttr.in for weather. Always works, does not depend on SearXNG."""
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

SEARXNG_TOOLS = {
    'searxng_search': execute_searxng_search,
    'searxng_fetch_url': execute_searxng_fetch_url,
    'searxng_engines': execute_searxng_engines,
}


async def call_llama(messages, max_tokens=4096, user_text=''):
    """Call llama.cpp with a tool-calling loop (max 5 iterations)."""
    tools = await fetch_tools_from_llama()
    custom_weather_tool = {
        'type': 'function',
        'function': {
            'name': 'get_weather',
            'description': 'Get current weather in a given city. Uses wttr.in, always works, no CAPTCHA. Use this for any questions about current weather, temperature, precipitation, wind.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'location': {'type': 'string', 'description': 'City name (e.g. "Yalta", "Moscow", "Yalta")'}
                },
                'required': ['location']
            }
        }
    }
    all_tools = tools + [custom_weather_tool]
    sys_prompt = {
        'role': 'system',
        'content': (
            'You are a smart assistant with access to tools. '
            'When the user asks about weather, news, current events, or anything requiring fresh data — '
            'ALWAYS call the relevant tool, do not say "I have no access". '
            'For weather questions use get_weather (it always works via wttr.in). '
            'For web search — searxng_search. '
            'If SearXNG returns 0 results, try searxng_fetch_url to a specific site or tell the user search is currently unavailable. '
            'After getting tool result, give a clear, concise answer in natural language. '
            'Answer in the language of the user (Russian by default).'
        )
    }
    msgs = [sys_prompt] + messages
    max_iter = 5
    for iteration in range(max_iter):
        async with httpx.AsyncClient(timeout=180.0) as client:
            r = await client.post(
                f"{LLAMA_URL}/chat/completions",
                headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
                json={
                    "model": MODEL,
                    "messages": msgs,
                    "tools": all_tools,
                    "tool_choice": "auto",
                    "parallel_tool_calls": False,
                    "max_tokens": max_tokens,
                    "stream": False,
                }
            )
            r.raise_for_status()
            data = r.json()
        msg = data['choices'][0]['message']
        tool_calls = msg.get('tool_calls') or []
        if not tool_calls:
            return msg.get('content') or ''
        msgs.append(msg)
        for tc in tool_calls:
            fn_name = tc.get('function', {}).get('name', '')
            raw_args = tc.get('function', {}).get('arguments', '{}')
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except Exception:
                args = {}
            tc_id = tc.get('id', '')
            print(f"[tool] iter={iteration} call {fn_name}({args})")
            if fn_name in CUSTOM_TOOLS:
                result = await CUSTOM_TOOLS[fn_name](args)
            elif fn_name in SEARXNG_TOOLS:
                result = await SEARXNG_TOOLS[fn_name](args)
            else:
                result = f'[tool {fn_name} unavailable in this mode. Bot supports: get_weather, searxng_search, searxng_fetch_url, searxng_engines.]'
            msgs.append({
                'role': 'tool',
                'tool_call_id': tc_id,
                'content': str(result)[:12000],
            })
    return '[bot: exceeded tool-calling iteration limit]'


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


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await reject_if_unauthorized(update, context):
        return
    await update.message.reply_text(
        '🤖 **LlamaBot v2 started!**\n\n'
        'I can:\n'
        '• Answer questions (with tool-calling)\n'
        '• Search the web (SearXNG) 🌐\n'
        '• Get weather (wttr.in) ☀️\n'
        '• Analyze images (send a photo)\n'
        '• Transcribe voice messages 🎤\n'
        '• Read documents (TXT, PDF)\n\n'
        'Commands: /reset, /stats'
    )


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await reject_if_unauthorized(update, context):
        return
    user_id = update.effective_user.id
    if user_id in conversations:
        del conversations[user_id]
    await update.message.reply_text('🔄 Context cleared.')


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await reject_if_unauthorized(update, context):
        return
    user_id = update.effective_user.id
    msg_count = len(conversations.get(user_id, []))
    tools = await fetch_tools_from_llama()
    await update.message.reply_text(
        f'📊 **Stats:**\n'
        f'Messages: {msg_count}\n'
        f'Model: {MODEL}\n'
        f'Tools: {len(tools)} searxng + 1 weather'
    )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await reject_if_unauthorized(update, context):
        return
    user_id = update.effective_user.id
    await update.message.chat.send_action(action='typing')
    try:
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        photo_bytes = await file.download_as_bytearray()
        photo_base64 = base64.b64encode(photo_bytes).decode('utf-8')
        caption = update.message.caption or 'Describe the image in detail.'
        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": caption},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{photo_base64}"}}
            ]
        }]
        bot_response = await call_llama(messages, max_tokens=2048)
        await update.message.reply_text(bot_response)
    except Exception as e:
        await update.message.reply_text(f'❌ Error: {e}')


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await reject_if_unauthorized(update, context):
        return
    user_id = update.effective_user.id
    await update.message.chat.send_action(action='typing')
    try:
        voice = update.message.voice
        file = await context.bot.get_file(voice.file_id)
        voice_bytes = await file.download_as_bytearray()
        transcript = await transcribe_voice(voice_bytes)
        await update.message.reply_text(f'🎤 **Transcript:**\n_{transcript}_', parse_mode='Markdown')
        if user_id not in conversations:
            conversations[user_id] = []
        conversations[user_id].append({"role": "user", "content": transcript})
        if len(conversations[user_id]) > 20:
            conversations[user_id] = conversations[user_id][-20:]
        bot_response = await call_llama(conversations[user_id], max_tokens=4096, user_text=transcript)
        conversations[user_id].append({"role": "assistant", "content": bot_response})
        if len(bot_response) > 4000:
            for i in range(0, len(bot_response), 4000):
                await update.message.reply_text(bot_response[i:i+4000])
        else:
            await update.message.reply_text(bot_response)
    except Exception as e:
        await update.message.reply_text(f'❌ Error: {e}')


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await reject_if_unauthorized(update, context):
        return
    await update.message.chat.send_action(action='typing')
    doc = update.message.document
    file = await context.bot.get_file(doc.file_id)
    doc_bytes = await file.download_as_bytearray()
    tmp_path = None
    try:
        ext = doc.file_name.split('.')[-1] if '.' in doc.file_name else 'txt'
        with tempfile.NamedTemporaryFile(delete=False, suffix=f'.{ext}') as tmp:
            tmp.write(doc_bytes)
            tmp_path = tmp.name
        with open(tmp_path, 'r', encoding='utf-8', errors='ignore') as f:
            doc_text = f.read()[:16000]
        caption = update.message.caption or 'Read the document and answer questions.'
        messages = [{"role": "user", "content": f"{caption}\n\n--- Document ---\n{doc_text}"}]
        bot_response = await call_llama(messages, max_tokens=4096)
        await update.message.reply_text(bot_response)
    except Exception as e:
        await update.message.reply_text(f'❌ Error: {e}')
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await reject_if_unauthorized(update, context):
        return
    user_id = update.effective_user.id
    user_message = update.message.text
    await update.message.chat.send_action(action='typing')
    if user_id not in conversations:
        conversations[user_id] = []
    conversations[user_id].append({"role": "user", "content": user_message})
    if len(conversations[user_id]) > 20:
        conversations[user_id] = conversations[user_id][-20:]
    try:
        bot_response = await call_llama(conversations[user_id], max_tokens=4096, user_text=user_message)
        conversations[user_id].append({"role": "assistant", "content": bot_response})
        if len(bot_response) > 4000:
            for i in range(0, len(bot_response), 4000):
                await update.message.reply_text(bot_response[i:i+4000])
        else:
            await update.message.reply_text(bot_response)
    except Exception as e:
        await update.message.reply_text(f'❌ Error: {e}')
        if conversations[user_id]:
            conversations[user_id].pop()


def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    print('🤖 LlamaBot v2 (with tool-calling) started...')
    app.run_polling()


if __name__ == '__main__':
    main()
