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
