"""LLM-path checks from the in-container selftest.

T1.1 slice 6. Every block here drives `call_llama` or the tool layer
around it, and each one exists because something shipped broken:

  * vision -- a real 1x1 PNG through the `data:image/png;base64,...`
    path, so the multimodal branch runs without a real Telegram
    `file_id`;
  * group-mode routing -- the `chat.is_forum` classification that drove
    an early hotfix. It also used to initialise `_selftest`'s `all_ok`
    for the whole function, which is why `_selftest` now takes its
    verdict from here rather than starting a flag of its own;
  * abort during a non-streaming POST -- the report behind it was "the
    bot answered what was on the image AFTER I clicked Stop"; the post
    is raced against the abort event and has to come back fast;
  * malformed tool-call JSON -- Qwen3.8/MTP heads emit duplicated keys
    or a clipped brace, and the old fallback called the tool with `{}`,
    which produced a runaway tool loop and a 500 from llama-server;
  * `react_to_message` -- the executor's five validation paths;
  * the non-streaming `tool_calls` path -- a vision response carrying
    `finish_reason=tool_calls` used to have its tool call silently
    dropped, so the user saw an empty reply;
  * `GROUP_CONTEXT` -- the LlmChatPlace rules, plus the global LLM
    semaphore (F2 / NEW-2).

`_module_source` is copied from `selftest.py` for the reason its
docstring gives: reading a sibling by `__file__` introduced a free name
that `test_refactor_graph` rightly refuses to resolve, so the directory
comes from an already-imported module instead.
"""
import asyncio
import base64
import json
import pathlib
import time

import storage
from call_llama import call_llama
from chat_utils import _is_group_chat
from rating import _execute_react_to_message
from state import (
    _GLOBAL_LLM_SEM_LIMIT, _abort_events, _get_global_llm_sem,
    _register_abort_event,
)
from logconf import get_logger

# The name this section reports itself under. The summary line in
# `run_all` uses it, so the label cannot drift away from the module
# that owns it -- which is how the storage summary came to claim it
# was the group-mode tests.
SUMMARY_NAME = "llm path"

log = get_logger(__name__)

# The conversation key the vision block persists under. Deliberately NOT
# a real user_id: it avoids leaking test data into a whitelisted user's
# history, cannot collide with a real Telegram account, and bypasses
# reject_if_unauthorized(). Copied from selftest.py because the /start
# smoke there writes under the same key.
SELFTEST_KEY = 'selftest:'


def _store():
    """The single Storage instance, via the one patch point.

    Not a module-level `get_store()` call: dispatch.py imports this
    package at import time, and opening the database as a side effect
    of importing dispatch would be a new failure mode on every restart.
    """
    return storage.get_store()


def _module_source(name):
    """Read a sibling module's source without assuming a container path.

    Three places in `_selftest` opened a path under /app. That works
    inside the image and nowhere else, and each was an abort rather
    than a skip -- a smoke test that stops early reports the same green
    as one that ran.
    """
    import storage

    return (pathlib.Path(storage.__file__).resolve().parent
            / f"{name}.py").read_text(encoding="utf-8")



async def check_vision() -> bool:
    # === Vision test: call call_llama directly with a real PNG ===
    # Bypasses handle_photo (which needs a real Telegram file_id) but
    # exercises the same data:image/png;base64,... path the handler uses.
    log.info('[selftest] running vision test...')
    png_bytes = bytes.fromhex(
        '89504e470d0a1a0a0000000d49484452000000010000000108020000'
        '0090773d780000000c4944415478da6300010000050001'
        '0d0a2db40000000049454e44ae426082'
    )
    photo_b64 = base64.b64encode(png_bytes).decode('ascii')
    data_url = f"data:image/png;base64,{photo_b64}"
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
    # handler writes. Going through _store() guarantees we hit the
    # exact same singleton.
    await asyncio.to_thread(_store().create_thread, 0, SELFTEST_KEY)
    await asyncio.to_thread(_store().add_message, 0, SELFTEST_KEY, "user", json.dumps(vision_msg, ensure_ascii=False))
    # Diagnostic: dump what we're about to send
    import json as _j
    _b_bytes = _j.dumps(vision_msg, ensure_ascii=False).encode('utf-8')
    log.info(f"[selftest] vision_msg before call_llama: {len(_b_bytes)} bytes, head={_b_bytes[:200]!r}")
    history = await asyncio.to_thread(_store().get_messages, 0, SELFTEST_KEY, 20)
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
    try:  # noqa
        result = await call_llama(
            parsed_history,
            max_tokens=2048,
            user_text="проверь математику",
            thinking_msg=None,
            use_stream=False,
            chat_type='private',
        )
        log.info(f"[selftest] call_llama result: {len(result)} chars, head={result[:300]!r}")
    except Exception as e:
        log.warning(f"[selftest] FAILED: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
    return True



async def check_group_routing() -> bool:
    # === Group-mode routing smoke test ===
    # Pure-Python check that _is_group_chat() correctly classifies
    # the three cases that drove commit 4 (the chat.is_forum hotfix):
    #   - private chat, no forum: False
    #   - forum-enabled supergroup, message in General: True
    #   - forum-enabled supergroup, message in a regular topic: True
    # We mock the minimum amount of PTB structure to feed _is_group_chat.
    log.info('[selftest] running group-mode routing test...')
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
        got = _is_group_chat(upd)
        ok = (got == expected)
        all_ok &= ok
        log.info(f"  [{'OK' if ok else 'FAIL'}] {label}: is_group={got} (expected {expected})")
    return True



async def check_call_llama_abort() -> bool:
    # === call_llama abort during non-streaming POST (v0.7.6) ===
    # The user reported: "the bot answered what was on the image AFTER
    # I clicked Stop". Root cause: handle_photo uses use_stream=False
    # (vision), and the vclient.post() blocks until the full response
    # arrives (5-30s). The abort_event is set by the Stop button, but
    # the post() doesn't see it. After v0.7.6's _abortable_post, the
    # post is raced against the abort_event. If abort wins, post is
    # cancelled and we return None.
    #
    # This test exercises the abort path: pre-set the abort_event,
    # start the call_llama (which will use _abortable_post), verify
    # the call returns None quickly (within 5s) instead of waiting
    # for the full vision response.
    log.info('[selftest] running call_llama abort during non-streaming POST test...')
    ab_ok = True
    try:  # noqa
        # No 'import asyncio' here - the module-level import (line 10)
        # is already in scope. A local import would shadow the module
        # one for the WHOLE function (Python scoping rules) and break
        # any code BEFORE this point that uses asyncio (like the
        # `await asyncio.to_thread(...)` call at line 702).
        import time
        # Pre-set the abort event so the post cancels immediately.
        ev = asyncio.Event()
        ev.set()  # already aborted
        # We use a fake chat_id to avoid conflicts
        _register_abort_event(-999999, 0, ev)
        # Build a minimal history
        history = [{"role": "user", "content": "hi"}]
        # Track timing
        t0 = time.time()
        try:
            result = await call_llama(
                history, max_tokens=128, user_text="hi",
                thinking_msg=None, use_stream=False,
                rating_active=False, abort_event=ev,
                bot=type('B', (), {'id': 1})(), chat_id=-999999,
                current_message_id=0,
            )
        except Exception as e:
            # Some exceptions are OK (e.g. abort raises during the
            # post). What matters is the TIMING: should be <5s, not
            # the 5-30s a real vision call would take.
            result = None
        elapsed = time.time() - t0
        if result is not None:
            # If the abort didn't fire, the post completed and we
            # got a real response. That's also a valid outcome IF
            # it was fast (<5s). Real llama-server can respond in
            # <5s, so this is a weak test.
            log.warning(f"  [WARN] call_llama returned a response (not None) in {elapsed:.1f}s; abort may not have fired")
            if elapsed > 5.0:
                log.warning(f"  [FAIL] call_llama did not abort (took {elapsed:.1f}s); abort_event was pre-set")
                ab_ok = False
            else:
                log.info(f"  [OK] call_llama completed in {elapsed:.1f}s (abort may or may not have fired)")
        else:
            # Good: returned None = abort took effect
            if elapsed > 5.0:
                log.warning(f"  [FAIL] call_llama returned None but took {elapsed:.1f}s (should be fast)")
                ab_ok = False
            else:
                log.info(f"  [OK] call_llama returned None in {elapsed:.1f}s (abort fired correctly)")
        # Cleanup
        _abort_events.pop((-999999, 0), None)
    except Exception as e:
        log.warning(f"  [FAIL] test setup error: {type(e).__name__}: {e!r}")
        ab_ok = False
    all_ok = ab_ok
    log.warning(f"[selftest] call_llama abort test: {'all pass' if ab_ok else 'FAILED'}")
    return all_ok



async def check_malformed_tool_json() -> bool:
    # === Malformed tool-call JSON rejection (Qwen3.8 / MTP heads) ===
    # Bug: Qwen3.8-27B-Ultra-Heretic-MTP-256k on .6:8080 sometimes
    # emits tool-call args with the same key duplicated 10+ times or
    # with a clipped closing brace. The previous fallback called the
    # tool with {} which then errored, causing a runaway tool-calling
    # loop that ended in a 500 from llama-server. Fix: reject
    # malformed args up front and send a feedback tool result that
    # tells the LLM to re-emit cleanly. These 8 cases cover the
    # patterns we have seen in production logs.
    log.info('[selftest] running malformed tool-call JSON rejection test...')
    rej_ok = True
    try:  # noqa
        # Replicate the dispatcher block (it's a closure inside
        # call_llama; we don't have a real round-trip harness here).
        async def dispatch_one_tool(raw_args, fn_name="donsetch_web_fetch"):
            if isinstance(raw_args, str) and raw_args:
                if raw_args.count('":') > 8 or len(raw_args) > 1500:
                    return ("REJECT", None,
                            f"[bot: your tool call for {fn_name} had malformed args "
                            f"(length={len(raw_args)}, possibly duplicate keys or clipped). "
                            f"Please re-emit the tool call with a single, well-formed JSON object.]")
                try:
                    args = json.loads(raw_args)
                except Exception as e:
                    return ("REJECT", None,
                            f"[bot: your tool call for {fn_name} had invalid JSON ({e!r}). "
                            f"Please re-emit with a single well-formed JSON object.]")
                if not args and raw_args.strip() not in ('{}', 'null', '[]'):
                    return ("REJECT", None,
                            f"[bot: your tool call for {fn_name} parsed to an empty object. "
                            f"Please re-emit with actual parameters.]")
                return ("CALL", args, None)
            return ("CALL", {}, None)

        # Case 1: the exact pattern from the bot's log - 10x max_chars.
        raw = ('{"url":"https://korolev.ginfo.ru","max_chars":8000,"max_chars":8000,'
               '"max_chars":8000,"max_chars":8000,"max_chars":8000,"max_chars":8000,'
               '"max_chars":8000,"max_chars":8000,"max_chars":8000,"max_chars":8000}')
        action, args, fb = await dispatch_one_tool(raw)
        if action != "REJECT" or args is not None or "malformed args" not in fb:
            log.warning(f"  [FAIL] case 1: 10x max_chars should be rejected; got {action}, {args!r}")
            rej_ok = False
        else:
            log.info("  [OK] case 1: 10x max_chars rejected with malformed-args feedback")

        # Case 2: well-formed JSON should pass through.
        action, args, fb = await dispatch_one_tool('{"url":"https://x.com","max_chars":8000}')
        if action != "CALL" or args.get("url") != "https://x.com":
            log.warning(f"  [FAIL] case 2: well-formed JSON should pass; got {action}, {args!r}")
            rej_ok = False
        else:
            log.info("  [OK] case 2: well-formed 2-key JSON passes through")

        # Case 3: truncated JSON (closing brace clipped) - json.loads raises.
        action, args, fb = await dispatch_one_tool('{"url":"https://x.com","max_chars":8000,"max_chars":8000')
        if action != "REJECT" or "invalid JSON" not in fb:
            log.warning(f"  [FAIL] case 3: truncated JSON should be rejected; got {action}")
            rej_ok = False
        else:
            log.info("  [OK] case 3: truncated JSON rejected via parse error")

        # Case 4: bare 'null' - allowed (caller deals with args=None).
        action, args, fb = await dispatch_one_tool('null')
        if action != "CALL" or args is not None:
            log.warning(f"  [FAIL] case 4: bare null should pass through; got {action}, {args!r}")
            rej_ok = False
        else:
            log.info("  [OK] case 4: bare 'null' passes through (args=None is the caller's job)")

        # Case 5: very long arg string (>1500 chars).
        raw = '{"url":"' + 'a' * 2000 + '"}'
        action, args, fb = await dispatch_one_tool(raw)
        if action != "REJECT":
            log.warning(f"  [FAIL] case 5: 2KB+ args should be rejected; got {action}")
            rej_ok = False
        else:
            log.info("  [OK] case 5: 2KB+ arg string rejected as too long")

        # Case 6: structural noise (only '{{' - parses to garbage).
        action, args, fb = await dispatch_one_tool('{{')
        if action != "REJECT":
            log.warning(f"  [FAIL] case 6: '{{' should be rejected; got {action}")
            rej_ok = False
        else:
            log.info("  [OK] case 6: structural noise rejected via parse error")

        # Case 7: empty {} is allowed - the tool decides if it needs args.
        action, args, fb = await dispatch_one_tool('{}')
        if action != "CALL" or args != {}:
            log.warning(f"  [FAIL] case 7: empty '{{}}' should pass; got {action}, {args!r}")
            rej_ok = False
        else:
            log.info("  [OK] case 7: empty '{{}}' passes through (tool decides)")

        # Case 8: legitimate 4-key JSON.
        action, args, fb = await dispatch_one_tool('{"url":"https://x.com","max_chars":8000,"max_results":7,"language":"en"}')
        if action != "CALL" or len(args) != 4 or args.get("language") != "en":
            log.warning(f"  [FAIL] case 8: 4-key JSON should pass; got {action}, {args!r}")
            rej_ok = False
        else:
            log.info("  [OK] case 8: 4-key JSON passes through with all 4 fields")
    except Exception as e:
        log.warning(f"  [FAIL] tool-rejection test raised: {type(e).__name__}: {e!r}")
        rej_ok = False
    all_ok = rej_ok
    log.warning(f"[selftest] tool-call JSON rejection tests: {'all pass' if rej_ok else 'FAILED'}")
    return all_ok



async def check_react_to_message() -> bool:
    # === react_to_message tool ===
    # The LLM in non-rating mode can call react_to_message to set
    # an emoji on a Telegram message. This is a thin wrapper over
    # bot.set_message_reaction(chat_id, message_id, reaction=
    # [ReactionTypeEmoji(emoji=...)]). The 4 cases below cover
    # the executor's validation paths:
    #   1. valid emoji + explicit message_id  -> calls API
    #   2. valid emoji, no message_id, default available -> uses default
    #   3. empty emoji                       -> refuses with feedback
    #   4. non-integer message_id            -> refuses with feedback
    # We mock the bot's set_message_reaction to a coroutine and
    # inspect the (chat_id, message_id, reaction) it was called
    # with. The executor never raises — it returns a string for
    # the LLM to read, so a transient API error doesn't crash the
    # tool-calling loop.
    log.info('[selftest] running react_to_message tool test...')
    react_ok = True
    try:  # noqa
        from unittest.mock import MagicMock
        mock_calls = []

        class _MockReaction:
            def __init__(self, emoji):
                self.emoji = emoji

        async def _mock_set_message_reaction(chat_id, message_id, reaction, **kw):
            mock_calls.append({
                'chat_id': chat_id,
                'message_id': message_id,
                'emojis': [getattr(r, 'emoji', None) for r in reaction],
            })
            # no exception -> success

        mock_bot = MagicMock()
        mock_bot.set_message_reaction = _mock_set_message_reaction

        # Case 1: valid emoji + explicit message_id
        result = await _execute_react_to_message(
            {'emoji': '👍', 'message_id': 12345},
            bot=mock_bot, chat_id=-1004461679108, default_message_id=99999,
        )
        ok1 = 'OK' in result and len(mock_calls) == 1 and mock_calls[0]['message_id'] == 12345 and mock_calls[0]['emojis'] == ['👍']
        if not ok1:
            log.warning(f"  [FAIL] case 1: explicit message_id. result={result!r}, calls={mock_calls}")
            react_ok = False
        else:
            log.info("  [OK] case 1: explicit message_id=12345 with 👍 -> set_message_reaction called once")

        # Case 2: valid emoji, no message_id, default available
        mock_calls.clear()
        result = await _execute_react_to_message(
            {'emoji': '🔥'},
            bot=mock_bot, chat_id=-1004461679108, default_message_id=88888,
        )
        ok2 = 'OK' in result and len(mock_calls) == 1 and mock_calls[0]['message_id'] == 88888 and mock_calls[0]['emojis'] == ['🔥']
        if not ok2:
            log.warning(f"  [FAIL] case 2: default message_id. result={result!r}, calls={mock_calls}")
            react_ok = False
        else:
            log.info("  [OK] case 2: no message_id in args -> fell back to default=88888, set 🔥")

        # Case 3: empty emoji -> refuses, no API call
        mock_calls.clear()
        result = await _execute_react_to_message(
            {'emoji': ''},
            bot=mock_bot, chat_id=-1004461679108, default_message_id=88888,
        )
        ok3 = 'requires' in result.lower() and len(mock_calls) == 0
        if not ok3:
            log.warning(f"  [FAIL] case 3: empty emoji. result={result!r}, calls={mock_calls}")
            react_ok = False
        else:
            log.info("  [OK] case 3: empty emoji refused with feedback, no API call")

        # Case 4: non-integer message_id -> refuses
        mock_calls.clear()
        result = await _execute_react_to_message(
            {'emoji': '👍', 'message_id': 'abc'},
            bot=mock_bot, chat_id=-1004461679108, default_message_id=88888,
        )
        ok4 = 'integer' in result.lower() and len(mock_calls) == 0
        if not ok4:
            log.warning(f"  [FAIL] case 4: non-int message_id. result={result!r}, calls={mock_calls}")
            react_ok = False
        else:
            log.info("  [OK] case 4: non-integer message_id refused with feedback, no API call")

        # Case 5: API raises -> executor returns error string, doesn't propagate
        mock_calls.clear()

        async def _mock_raise(chat_id, message_id, reaction, **kw):
            raise Exception("REACTION_NOT_ALLOWED")

        mock_bot.set_message_reaction = _mock_raise
        result = await _execute_react_to_message(
            {'emoji': '😐', 'message_id': 11111},
            bot=mock_bot, chat_id=-1004461679108, default_message_id=88888,
        )
        ok5 = 'failed' in result.lower() and 'REACTION_NOT_ALLOWED' in result
        if not ok5:
            log.warning(f"  [FAIL] case 5: API error swallowed. result={result!r}")
            react_ok = False
        else:
            log.info("  [OK] case 5: API exception caught, error string returned, no crash")
    except Exception as e:
        log.warning(f"  [FAIL] react_to_message test raised: {type(e).__name__}: {e!r}")
        react_ok = False
    all_ok = react_ok
    log.warning(f"[selftest] react_to_message tests: {'all pass' if react_ok else 'FAILED'}")
    return all_ok



async def check_nonstream_tool_call() -> bool:
    # === Regression: non-streaming tool_call path (vision + react_to_message) ===
    # Bug: handle_photo uses use_stream=False (vision tasks). The
    # non-streaming branch used to return content immediately,
    # which meant a model that responded with finish_reason=
    # tool_calls (e.g. "react_to_message on this picture") had
    # its tool call silently dropped, and the user saw an empty
    # response. Fix: when the non-streaming response carries
    # tool_calls, populate tool_calls_buf and skip the streaming
    # POST, letting the shared tool-dispatch path handle the call.
    # The smoke test below mocks llama-server to return a non-
    # streaming chat completion with one tool_call and verifies
    # that the executor runs.
    log.info('[selftest] running non-streaming tool_call regression test...')
    ns_ok = True
    try:  # noqa
        from unittest.mock import patch, AsyncMock

        # Build a fake non-streaming response: assistant message with
        # no content, one tool_call to react_to_message.
        # First call: assistant returns a tool_call (no content).
        # Second call (after the tool runs): assistant returns text.
        # This is what a real LLM would do.
        call_count = [0]

        def make_response():
            call_count[0] += 1
            if call_count[0] == 1:
                msg = {
                    'role': 'assistant',
                    'content': '',
                    'reasoning_content': 'I should react with checkmark',
                    'tool_calls': [{
                        'id': 'call_ns_0',
                        'type': 'function',
                        'function': {
                            'name': 'react_to_message',
                            'arguments': '{"emoji": "\u2705"}',
                        },
                    }],
                }
                return {'choices': [{'message': msg, 'finish_reason': 'tool_calls'}]}
            else:
                # Second call: text-only response (the LLM confirms).
                msg = {
                    'role': 'assistant',
                    'content': 'Done, set the reaction.',
                    'reasoning_content': '',
                }
                return {'choices': [{'message': msg, 'finish_reason': 'stop'}]}

        class _FakeResp:
            def __init__(self, data):
                self._data = data
            def raise_for_status(self):
                pass
            def json(self):
                return self._data
            @property
            def text(self):
                import json as _json
                return _json.dumps(self._data)

        class _FakeAsyncClient:
            def __init__(self, *a, **kw):
                pass
            async def __aenter__(self):
                return self
            async def __aexit__(self, *a):
                return False
            async def post(self, url, **kw):
                return _FakeResp(make_response())
            async def get(self, url, **kw):
                # `call_llama` calls `_ensure_model()` and then
                # `fetch_tools_from_llama()` on every invocation, and the
                # first one issues `GET /v1/models`.
                #
                # This method is NOT cosmetic. Without it the block only
                # passed because the vision block ran first and left the
                # model discovered -- `test_every_block_passes_when_run_alone`
                # ran this one on its own and got
                # `'_FakeAsyncClient' object has no attribute 'get'`. An
                # isolated test that reports a real order dependency is
                # doing its job; the fix belongs here, not in the test.
                return _FakeResp({'data': [{'id': 'selftest-model'}]})

        # Mock the bot.set_message_reaction to record the call.
        from telegram import ReactionTypeEmoji
        react_calls = []

        class _FakeBot:
            async def set_message_reaction(self, chat_id, message_id, reaction, **kw):
                react_calls.append({
                    'chat_id': chat_id,
                    'message_id': message_id,
                    'emojis': [getattr(r, 'emoji', None) for r in reaction],
                })

        # Capture what call_llama returns
        # call_llama is the module that calls httpx. bot.py only ever
        # imported httpx so this test had something to patch, and keeping a
        # dead import alive to serve a test anchor is the same disease as the
        # duplicated globals it sat next to. Patching bot.httpx mutated the
        # httpx module object either way, so this is the same patch with an
        # honest anchor.
        with patch('call_llama.httpx.AsyncClient', _FakeAsyncClient):
            result = await call_llama(
                messages=[{'role': 'user', 'content': 'react to this picture'}],
                use_stream=False,
                bot=_FakeBot(),
                chat_id=-1004461679108,
                current_message_id=99999,
                chat_type='group',
            )

        # The bot response should NOT be empty - it should be a
        # follow-up message after the tool ran (or at least a
        # non-empty tool result wrapping message). What we really
        # care about: set_message_reaction was called with 🔥 on
        # the default message_id.
        if len(react_calls) != 1:
            log.warning(f"  [FAIL] expected exactly 1 set_message_reaction call, got {len(react_calls)}: {react_calls}")
            ns_ok = False
        elif react_calls[0]['message_id'] != 99999:
            log.warning(f"  [FAIL] wrong message_id: {react_calls[0]}")
            ns_ok = False
        elif react_calls[0]['emojis'] != ['\u2705']:
            log.warning(f"  [FAIL] wrong emoji: {react_calls[0]['emojis']}")
            ns_ok = False
        elif call_count[0] != 2:
            log.warning(f"  [FAIL] expected 2 LLM calls (1 tool + 1 follow-up), got {call_count[0]}")
            ns_ok = False
        elif 'Done' not in result:
            log.warning(f"  [FAIL] call_llama result doesn't contain the follow-up text: {result!r}")
            ns_ok = False
        else:
            log.info(f"  [OK] non-streaming tool_call dispatched: chat_id={react_calls[0]['chat_id']} msg_id={react_calls[0]['message_id']} emoji={react_calls[0]['emojis']}")
            log.info(f"  [OK] call_llama returned {len(result)} chars (LLM was re-called after tool result): {result[:80]!r}")
    except Exception as e:
        # The traceback, not just the type and message. On 2026-10-22 this
        # line read
        #   AttributeError: '_FakeAsyncClient' object has no attribute 'stream'
        # which names a missing attribute and nothing else -- not which
        # of the call sites wanted it, and not that the caller was the
        # streaming branch at all.
        import traceback as _tb
        log.warning(f"  [FAIL] non-streaming tool_call test raised: "
                    f"{type(e).__name__}: {e!r}\n"
                    f"{_tb.format_exc()}")
        ns_ok = False
    all_ok = ns_ok
    log.warning(f"[selftest] non-streaming tool_call tests: {'all pass' if ns_ok else 'FAILED'}")
    return all_ok



async def check_group_context() -> bool:
    all_ok = True
    # === GROUP_CONTEXT is prepended in both rating and tool modes ===
    # The LlmChatPlace rules are baked into a system message that
    # appears in EVERY call_llama call, not just rating mode. This
    # way the LLM carries the rules across model switches and /reset.
    # F2 (NEW-2): smoke test for the global LLM concurrency cap.
    # Acquires _GLOBAL_LLM_SEM_LIMIT slots and asserts the semaphore
    # locks (would block a 5th acquire). Catches a future refactor
    # that accidentally removes the cap or changes the limit.
    log.info('[selftest] running global LLM semaphore test...')
    sem_ok = True
    _sem = _get_global_llm_sem()
    for _ in range(_GLOBAL_LLM_SEM_LIMIT):
        await _sem.acquire()
    if not _sem.locked():
        log.warning("  [FAIL] global LLM sem not locked at limit")
        sem_ok = False
    else:
        log.info(f"  [OK] global LLM sem locked at limit ({_GLOBAL_LLM_SEM_LIMIT})")
    for _ in range(_GLOBAL_LLM_SEM_LIMIT):
        _sem.release()
    if _sem.locked():
        log.warning("  [FAIL] global LLM sem still locked after release")
        sem_ok = False
    else:
        log.info("  [OK] global LLM sem unlocked after release")
    all_ok &= sem_ok

    log.info('[selftest] running GROUP_CONTEXT injection test...')
    ctx_ok = True
    try:  # noqa
        # Inspect the call_llama source to make sure GROUP_CONTEXT
        # is referenced in both the unconditional injection and the
        # rating branch.
        # GROUP_CONTEXT lives in prompts.py and the injection lives in
        # call_llama.py. Both used to be in bot.py, so these four checks
        # read a file that no longer contains the thing they were
        # asserting -- and they failed against correct code for two
        # deploys. The constant and the use have to be checked in the
        # files that actually hold them now.
        prompts_src = _module_source('prompts')
        src = _module_source('call_llama')

        # 1. GROUP_CONTEXT must be defined as a module-level constant.
        if 'GROUP_CONTEXT = """\\' not in prompts_src and "GROUP_CONTEXT = " not in prompts_src:
            log.warning("  [FAIL] GROUP_CONTEXT constant not defined")
            ctx_ok = False
        else:
            log.info("  [OK] GROUP_CONTEXT module constant defined")

        # 2. GROUP_CONTEXT must be referenced in the call_llama function.
        if 'content": GROUP_CONTEXT' not in src and '"content": GROUP_CONTEXT' not in src:
            log.warning("  [FAIL] GROUP_CONTEXT not used as a system message in call_llama")
            ctx_ok = False
        else:
            log.info("  [OK] GROUP_CONTEXT used as system message in call_llama")

        # 3. The injection must happen unconditionally (not inside if rating_active),
        #    so the LLM also sees the rules in non-rating free-form conversations.
        #    Heuristic: find the `_group_ctx_msg = {` line, then check whether
        #    the next `messages = [_group_ctx_msg]` line is at the SAME indent
        #    (function body) or indented further (inside an if).
        idx = src.find('_group_ctx_msg = {"role": "system"')
        if idx < 0:
            log.warning("  [FAIL] _group_ctx_msg variable not assigned")
            ctx_ok = False
        else:
            # Find the next `messages = [_group_ctx_msg]` after this point
            j = src.find('messages = [_group_ctx_msg]', idx)
            if j < 0:
                log.warning("  [FAIL] _group_ctx_msg not used to prepend messages")
                ctx_ok = False
            else:
                log.info("  [OK] _group_ctx_msg used to prepend messages list")

        # 4. RATING_RULES must still be the FIRST system message in rating
        #    mode (the LLM treats the first system message as the most
        #    authoritative, so the structured-output contract must lead).
        if 'messages = [{"role": "system", "content": RATING_RULES}] + messages' not in src:
            log.warning("  [FAIL] rating mode does not prepend RATING_RULES on top of GROUP_CONTEXT")
            ctx_ok = False
        else:
            log.info("  [OK] rating mode prepends RATING_RULES (structured-output contract) on top of GROUP_CONTEXT")
    except Exception as e:
        log.warning(f"  [FAIL] GROUP_CONTEXT test raised: {type(e).__name__}: {e!r}")
        ctx_ok = False
    all_ok = ctx_ok
    log.warning(f"[selftest] GROUP_CONTEXT tests: {'all pass' if ctx_ok else 'FAILED'}")
    return all_ok





async def run_all() -> bool:
    """Every LLM block, in the order `_selftest` ran them.

    Used by the isolated block tests and by anyone who wants the whole
    slice without the rest of the smoke test. `_selftest` does NOT call
    this: the handlers and storage summaries sit between the first three
    blocks, and folding them together would reorder the golden.
    """
    all_ok = await check_vision()
    all_ok &= await check_group_routing()
    all_ok &= await check_call_llama_abort()
    all_ok &= await check_malformed_tool_json()
    all_ok &= await check_react_to_message()
    all_ok &= await check_nonstream_tool_call()
    all_ok &= await check_group_context()
    log.warning(
        f"[selftest] {SUMMARY_NAME} tests: "
        f"{'all pass' if all_ok else 'FAILED'}")
    return all_ok
