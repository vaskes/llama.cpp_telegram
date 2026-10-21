"""HTTP helpers that are not the LLM call. TECH-DEBT #3, Block B2.

_abortable_post -- a POST cancellable mid-flight by an asyncio.Event.
Used by both branches of call_llama.

transcribe_voice -- Whisper. Not an LLM call at all; it lived in
call_llama.py because that used to be the only module doing HTTP.
handlers.py imports it and monkeypatches handlers.transcribe_voice, which
keeps working because handlers binds the name at import.

Both names are re-exported by call_llama for fork compatibility.
"""
import asyncio

import httpx

from config import WHISPER_LANGUAGE, WHISPER_URL


async def _abortable_post(client, url, abort_event=None, **kwargs):
    """httpx POST that can be cancelled by an asyncio.Event.

    The plain client.post() blocks until the full response arrives
    (5-30s for vision). The Stop button sets the abort_event, but
    the post() doesn't see it. This helper races the post against
    the abort_event:

    - If abort_event fires first, cancel the post task and
      return None (handler treats None as "aborted, don't send").
    - If the post completes first, return the response.

    Why not asyncio.wait_for? Because abort_event doesn't fire a
    timeout; it's a manual trigger. We need to actually race it.
    """
    if abort_event is None:
        return await client.post(url, **kwargs)

    post_task = asyncio.create_task(client.post(url, **kwargs))
    abort_task = asyncio.create_task(abort_event.wait())
    try:
        done, pending = await asyncio.wait(
            {post_task, abort_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if abort_task in done:
            # User clicked Stop. Cancel the post and return None.
            post_task.cancel()
            try:
                await post_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
            return None
        else:
            abort_task.cancel()
            try:
                await abort_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
            return post_task.result()
    except Exception:
        for t in (post_task, abort_task):
            if not t.done():
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass
        raise


async def transcribe_voice(voice_bytes):
    async with httpx.AsyncClient(timeout=120.0) as client:
        files = {'file': ('voice.ogg', bytes(voice_bytes), 'audio/ogg')}
        # v0.10.2: WHISPER_LANGUAGE env var, empty = autodetect. Default 'ru' for backwards compat with the LlmChatPlace group.
        data = {'language': WHISPER_LANGUAGE} if WHISPER_LANGUAGE else {}
        response = await client.post(
            f"{WHISPER_URL}/v1/audio/transcriptions",
            files=files, data=data
        )
        response.raise_for_status()
        return response.json()['text']
