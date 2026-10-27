"""Tool registry for the LLM call. TECH-DEBT #3, Block B2.

fetch_tools_from_llama and _bash_command_tool_enabled, moved verbatim.

The cache moved with the function. _TOOLS_CACHE was imported from config
by call_llama and by dispatch, but neither ever read its own binding --
dispatch imports it on line 63 and never uses it. So rebinding it here
changes nothing observable; leaving a dead `from config import
_TOOLS_CACHE` behind would have been the confusing option.

Both names are re-exported by call_llama: dispatch.py imports
fetch_tools_from_llama from there, and tests/test_t39_integration.py
calls call_llama._bash_command_tool_enabled() directly.
"""
import asyncio
from urllib.parse import urlparse, urlunparse

import httpx

import mcp_registry
from config import DISABLED_TOOLS, LLAMA_URL
from logconf import get_logger

log = get_logger(__name__)

# The cache this module owns. Used to be config._TOOLS_CACHE, imported
# into call_llama and read there; nobody outside the function read it.
_TOOLS_CACHE = None


def _bash_command_tool_enabled() -> bool:
    """True iff the bash_command tool should be advertised to
    llama-server in this run. Gated by:
      - LLAMABOT_DEPLOYMENT_PROFILE=personal
      - AGENT_STACK_TOKEN is set (non-empty)
    See telegram-bot/personal_tools/bash_command.py for the
    single source of truth on this check; this wrapper is just
    a try/except so a missing module (very old deploys) doesn't
    take the whole bot down.
    """
    try:
        from personal_tools import bash_command as _bc
    except Exception:
        return False
    return _bc.is_enabled_for_active_profile()


def merge_tools(engine_tools: list, bot_tools: list) -> list:
    """The bot's list is the whole truth about what the model may call.

    llama-server has tools of its own: its built-ins (read_file,
    exec_shell_command, ...) and whatever MCP servers IT was configured
    with. Two of those facts are traps, and both were measured on the
    live engine on 2026-10-27:

      * a built-in the bot cannot execute is a tool the model may call
        and the bot must refuse. The engine advertised
        `exec_shell_command`; the bot soft-rejected it; the model had
        spent a turn for nothing, and a shell on the engine's host is
        not something to offer in the first place;
      * the same name from two sources is TWO definitions of one name.
        The engine's `donsetch_web_search` offered `intent` and
        `deadline_ms`; the bot's offered `query` and `max_results`.
        The model saw two schemas for one tool and could send
        arguments that one of them did not have.

    So an engine tool is kept only when the bot can execute it AND the
    bot does not define it itself.
    """
    from tool_isolation import known_tool_names
    known = known_tool_names()
    bot_names = {t.get("function", {}).get("name") for t in bot_tools}
    kept = [t for t in engine_tools
            if t.get("function", {}).get("name") in known
            and t.get("function", {}).get("name") not in bot_names]
    return kept + list(bot_tools)


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
                # v0.10.2 (Qwen wave-3): robust URL construction.
                # Old LLAMA_URL.replace('/v1', '') broke on trailing
                # slashes, multiple /v1, or non-/v1 paths. Use urlparse
                # to strip the last path component if it's /v1.
                from urllib.parse import urlparse, urlunparse
                parsed = urlparse(LLAMA_URL)
                path = parsed.path.rstrip("/")
                if path.endswith("/v1"):
                    path = path[:-3]
                base = urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))
                r = await client.get(f"{base}/tools")
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
            log.info(f"[tools] loaded {len(openai_tools)} enabled tools (from {len(all_tools)} total) on attempt {attempt}")
            return openai_tools
        except Exception as e:
            last_err = e
            log.info(f"[tools] attempt {attempt}/3 failed: {type(e).__name__}: {e}")
            if attempt < 3:
                await asyncio.sleep(backoff)
    log.info(f"[tools] giving up after 3 attempts, last error: {last_err}")
    return []


async def build_tool_defs(*, bot, chat_id, chat_type,
                          reply_to_message_id=None,
                          current_message_id=None) -> list[dict]:
    """The tools the model is shown this turn.

    Two classes of tools in one list. The NATIVE ones are the bot's
    own: Telegram, the shell, the bot's own record of the chat. The
    IMPORTED ones are whatever the MCP servers offer -- discovered,
    not declared, and named `<server>_<tool>`. The model calls both
    the same way; the bot does not treat them the same way, and the
    difference is in the names, the dispatch and the policy.

    This lives here, and not inside call_llama, because it is the one
    place that decides what the model may call -- and because a list
    that is built inline cannot be tested without driving a whole
    model call.
    """
    engine_tools = await fetch_tools_from_llama()
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
        rights = await bot_may_delete(bot, chat_id, chat_type)
        if rights.allowed:
            bot_side_tool_defs.append(delete_messages_tool_def(
                chat_id=chat_id,
                reply_to_message_id=reply_to_message_id,
                current_message_id=current_message_id))
    # search_messages reads the bot's own record of the chat, so it needs
    # neither the sandbox nor any Telegram rights: the record is there in
    # every chat, and it is the only place a message said hours ago
    # survives with its id attached (the Bot API cannot list a chat's
    # messages). It is advertised always -- a search that finds nothing
    # says so, and the model has to be able to look before it answers.
    from llm_history import search_messages_tool_def
    bot_side_tool_defs.append(search_messages_tool_def())
    # The imported half: the tools the MCP servers offer. Discovered,
    # not declared -- the registry asks the servers what they have,
    # so a server that is down contributes nothing instead of taking
    # the bot down with it.
    imported_defs = await mcp_registry.get_registry().ensure_ready(chat_id)
    bot_side_tool_defs.extend(imported_defs)
    return merge_tools(engine_tools, bot_side_tool_defs)
