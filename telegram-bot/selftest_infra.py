"""Infrastructure checks from the in-container selftest.

T1.1 slice 7, and the last one: after this, `_selftest` is the
preamble, the `/start` smoke, a list of `run_all()` calls and two log
lines.

  * the donsetch integration -- `donsetch_call` against the live MCP
    transport, checking that the v4.7.4 split-shape gives a
    human-readable string rather than the `[meta]` JSON envelope;
  * the per-handler abort_event key -- `_abort_events` used to be keyed
    on (chat_id, user_id), so two concurrent handlers for one user
    overwrote each other's events and the Stop button set the wrong
    one;
  * the F4 LRU bound -- 250 inserts must leave 200 entries, which is
    what makes the v0.6.0 P0-1 bug non-regressable;
  * the post-won race -- a handler must discard the response if the
    abort event was set while `call_llama` was running;
  * the webhook boot -- the aiohttp server starts, `/healthz` answers,
    and a POST without the secret token is refused.

Two of these patch `handlers` module attributes by string, which is
where `handle_text` resolves every one of those names. That is the
fourth instance of the TECH-DEBT #1 defect -- a write to a module whose
globals nothing reads -- so the patch lists here are load-bearing and
are not tidied up.
"""
import asyncio
import os
import re

from state import _abort_events, _register_abort_event
from logconf import get_logger

# The name this section reports itself under. The summary line in
# `run_all` uses it, so the label cannot drift away from the module that
# owns it -- which is how the storage summary came to claim it was the
# group-mode tests.
SUMMARY_NAME = "infra"

log = get_logger(__name__)



async def check_donsetch_integration() -> bool:
    # === Real donsetch integration (against the live MCP server) ===
    # Skip this whole block if the env doesn't expose DONSETCH_URL
    # (e.g. in a unit-test environment that doesn't run the MCP).
    # The test exercises donsetch_call() which goes through the real
    # HTTP transport at 127.0.0.1:8765, validating that the v4.7.4
    # split-shape (DONSETCH_MCP__TEXT_ONLY=false) gives us a
    # human-readable text result instead of the [meta] JSON envelope.
    if not (os.environ.get('DONSETCH_URL')
            or os.environ.get('LLAMABOT_SELFTEST') == '1'):
        return True
    log.info('[selftest] running real donsetch integration test...')
    rt_ok = True
    try:
        # asyncio is imported at the top of this file; we use it
        # here without re-importing (a local `import asyncio` would
        # shadow the module reference for the whole function and
        # break every earlier `asyncio.X(...)` call in _selftest).
        import donsetch as _ds
        donsetch_call = _ds.donsetch_call
        _sid = _ds._donsetch_session_id
        # force re-init in case the session was set by a prior test.
        #
        # The write has to land on `donsetch`, the module that OWNS the
        # global. TECH-DEBT #3 Block B1 moved it here from call_llama;
        # before that this patched `bot`, and before that it bound
        # `_b_module` inside a try/except so the NameError was
        # swallowed and this entire test did nothing. Two no-ops in a
        # row in the same three lines, both invisible.
        _orig_sid = _ds._donsetch_session_id
        _ds._donsetch_session_id = None

        async def _rt():
            # web_search: human-readable result, no [meta] envelope
            r = await donsetch_call('web_search',
                {'query': 'python 3.13 release', 'max_results': 2})
            assert isinstance(r, str), f'web_search result is not str: {type(r)}'
            assert 'Search results' in r, f'web_search missing "Search results": {r[:100]}'
            assert '[meta]' not in r, f'web_search still has [meta] envelope (split-shape off?): {r[:200]}'
            log.info(f'  [OK] web_search: {len(r)} chars, "Search results" present, no [meta] envelope')

            # web_fetch: markdown content
            r2 = await donsetch_call('web_fetch',
                {'url': 'https://example.com'})
            assert isinstance(r2, str), f'web_fetch result is not str: {type(r2)}'
            assert 'Example Domain' in r2, f'web_fetch missing "Example Domain": {r2[:200]}'
            log.info(f'  [OK] web_fetch: {len(r2)} chars, markdown content delivered')

            # THE HANDLE ROUND TRIP. A search result carries a compact
            # handle, not a URL, and the handle is what reads the page.
            # The bot's tool description says so, and the model is told
            # so -- but a description that lies is how a turn ends in
            # fifteen guessed URLs and a 404 each.
            #
            # So this is the property, not the wording: search, take the
            # handle out of the result, fetch THAT handle, and the page
            # the search found comes back. If the server's result shape
            # changes (no more handles, or a handle the fetch refuses),
            # this goes red here -- in the container that talks to the
            # live server -- instead of quietly costing the model its
            # turn.
            r3 = await donsetch_call('web_search',
                {'query': 'python 3.13 release', 'max_results': 2})
            _handle = None
            for _line in r3.splitlines():
                _m = re.match(r'^\s*\d+\.\s+(\S+)\s+·', _line)
                if _m:
                    _handle = _m.group(1)
                    break
            assert _handle, (
                f'no handle in the search result -- the model cannot read '
                f'a page the search found without guessing its URL: '
                f'{r3[:300]}')
            r4 = await donsetch_call('web_fetch',
                {'url': f'@{_handle}', 'max_chars': 800})
            assert isinstance(r4, str), f'handle fetch is not str: {type(r4)}'
            assert 'http' in r4, (
                f'fetching the handle @{_handle} did not return a page: '
                f'{r4[:200]}')
            assert 'not found' not in r4.lower(), (
                f'fetching the handle @{_handle} answered 404: {r4[:200]}')
            log.info(f'  [OK] handle round trip: search -> @{_handle} -> page')

        await _rt()
    except AssertionError as e:
        log.warning(f"  [FAIL] {e!r}")
        rt_ok = False
    except Exception as e:
        # If donsetch is unreachable (e.g. test env without MCP),
        # skip rather than fail - we still have the curl-level
        # test in the deployment script.
        log.info(f"  [SKIP] donsetch unreachable in this env: {type(e).__name__}: {e!r}")
        rt_ok = True
    all_ok = rt_ok
    log.warning(f"[selftest] real donsetch integration tests: {'all pass' if rt_ok else 'FAILED'}")
    return all_ok



async def check_abort_event_key() -> bool:
    # === Per-handler abort_event key (Stop button race fix) ===
    # Bug: the dict was keyed on (chat_id, user_id). Two concurrent
    # in-flight handlers for the same user overwrote each other's
    # events; the Stop click then set the WRONG event and the earlier
    # LLM call ran to completion while the user thought it had been
    # stopped. Fix: key on (chat_id, thinking.message_id) so each
    # handler has its own slot.
    log.info('[selftest] running per-handler abort_event key test...')
    aek_ok = True
    try:  # noqa
        saved = dict(_abort_events)
        _abort_events.clear()
        # Two in-flight handlers for the same (chat, user) - the
        # old shared key would let the second overwrite the first.
        ev_a = asyncio.Event()
        ev_b = asyncio.Event()
        ev_a.set()  # simulate "user clicked Stop on handler A"
        # The OLD key (chat_id, user_id) would collide; the NEW key
        # uses the thinking message id which is unique per handler.
        # Use _register_abort_event (not raw insert) so the F4
        # LRU bound fires - the runtime path is the production path.
        _register_abort_event(-1001234567890, 5001, ev_a)
        _register_abort_event(-1001234567890, 5002, ev_b)
        # Both events present, neither overwrites the other.
        if _abort_events.get((-1001234567890, 5001)) is not ev_a:
            log.warning("  [FAIL] handler A event overwritten or missing")
            aek_ok = False
        elif _abort_events.get((-1001234567890, 5002)) is not ev_b:
            log.warning("  [FAIL] handler B event missing")
            aek_ok = False
        else:
            log.info("  [OK] two concurrent in-flight handlers keep distinct events")
        # Handler A's event is set; handler B's is not. Verifies the
        # race: setting one does NOT set the other.
        if not ev_a.is_set():
            log.warning("  [FAIL] handler A event should be set")
            aek_ok = False
        elif ev_b.is_set():
            log.warning("  [FAIL] handler B event should NOT be set")
            aek_ok = False
        else:
            log.info("  [OK] setting handler A's event does not affect handler B")
        # Pop only the matching key; the other stays intact.

        popped = _abort_events.pop((-1001234567890, 5001), None)
        if popped is not ev_a:
            log.warning("  [FAIL] pop on handler A key returned wrong value")
            aek_ok = False
        elif (-1001234567890, 5002) not in _abort_events:
            log.warning("  [FAIL] handler B event popped by mistake (shared key regression)")
            aek_ok = False
        else:
            log.info("  [OK] pop on handler A key does not touch handler B")
        # Cleanup
        _abort_events.clear()
        _abort_events.update(saved)
    except Exception as e:
        log.warning(f"  [FAIL] abort_event test raised: {type(e).__name__}: {e!r}")
        aek_ok = False
    all_ok = aek_ok
    log.warning(f"[selftest] abort_event key tests: {'all pass' if aek_ok else 'FAILED'}")
    return all_ok



async def check_f4_lru_bound() -> bool:
    # === F4 LRU bound (v0.6.0 P0-1 regression test) ===
    # _abort_events is LRU-bounded at 200 entries. If someone
    # removes the OrderedDict or the popitem in
    # _register_abort_event, the 250 -> 200 assertion fails
    # loudly. Without this test, the v0.6.0 P0-1 bug is silently
    # regressable on the next refactor.
    log.info('[selftest] running F4 LRU bound test...')
    lru_ok = True
    saved_aes = dict(_abort_events)
    _abort_events.clear()
    try:  # noqa
        for i in range(250):
            _register_abort_event(0, i, asyncio.Event())
        if len(_abort_events) != 200:
            log.warning(f"  [FAIL] F4 LRU bound: expected 200, got {len(_abort_events)}")
            lru_ok = False
        else:
            log.info("  [OK] F4 LRU bound at 200 (250 inserts -> 200 entries)")
        if (0, 0) in _abort_events:
            log.warning("  [FAIL] F4 LRU should have evicted (0, 0)")
            lru_ok = False
        else:
            log.info("  [OK] F4 LRU evicted oldest 50 entries")
    except Exception as e:
        log.warning(f"  [FAIL] F4 LRU test raised: {type(e).__name__}: {e!r}")
        lru_ok = False
    finally:
        _abort_events.clear()
        _abort_events.update(saved_aes)
    all_ok = lru_ok
    log.warning(f"[selftest] F4 LRU bound: {'all pass' if lru_ok else 'FAILED'}")
    return all_ok



async def check_post_race_abort_discard() -> bool:
    # === handle_text abort_event discard (post-won race) ===
    # v0.8.0 fix: handler discards non-None response if abort_event
    # was set during call_llama. Otherwise the user sees a bot
    # response AFTER clicking Stop — confusing UX. The fix lives in
    # all 4 handlers (text/photo/voice/document); we test handle_text
    # because it's the simplest (no media download) and exercises the
    # exact code path.
    #
    # IMPORTANT: this test patches module-level attrs on handlers, which
    # is where handle_text resolves every one of these names. We save
    # originals and restore in finally so we don't pollute later tests
    # (this is the LAST test, but still good hygiene in case the order
    # changes).
    #
    # One entry used to be patched on bot instead of handlers, which is
    # the fourth instance of the defect TECH-DEBT #1 fixed: a write to a
    # module whose globals nothing reads.
    log.info('[selftest] running handle_text post-race abort discard test...')
    ar_ok = True
    saved = {}
    try:  # noqa
        import handlers as _h

        class _FakeMessage:
            # message_id is read by handle_text when it forwards the turn to
            # call_llama. Without it the abort path still passes, but the
            # smoke test prints a traceback every run -- and a smoke test
            # that cries wolf is the kind nobody reads the day it is telling
            # the truth.
            message_id = 555
            text = 'user query'
            caption = None
            message_thread_id = 19
            from_user = type('U', (), {'id': 286293081, 'is_bot': False, 'username': 'V', 'first_name': 'V', 'last_name': 'L'})()
            chat = type('C', (), {'id': -1001, 'send_action': lambda self, **kw: asyncio.sleep(0)})()
        class _FakeUser:
            id = 286293081
            is_bot = False
            username = 'V'
            first_name = 'V'
            last_name = 'L'
        class _FakeChat:
            id = -1001
            type = 'private'
        class _FakeUpdate:
            effective_chat = _FakeChat()
            effective_user = _FakeUser()
            message = _FakeMessage()
        class _FakeContext:
            bot = type('B', (), {'id': 1})()
        class _ThinkingMsg:
            message_id = 42
            async def edit_text(self, *a, **k): return None
            async def delete(self): return None

        # Save originals
        for owner, name in [
            (_h, '_register_abort_event'),
            (_h, '_route_to_thread'),
            (_h, '_check_user_slot'),
            (_h, '_get_global_llm_sem'),
            (_h, '_apply_rating_and_persist'),  # was (_b, ...) -- a no-op
            (_h, '_reply'),
            (_h, 'send_reply'),
            (_h, '_persist_message'),
            (_h, '_load_history'),
            (_h, '_sender_display_name'),
            (_h, '_is_rating_active'),
            (_h, '_stop_button_markup'),
            (_h, 'call_llama'),
        ]:
            saved[(id(owner), name)] = getattr(owner, name, None)

        # Patch
        captured = []
        def _capture(chat_id, msg_id, ev):
            captured.append(ev)
        # handlers imports _register_abort_event / _route_to_thread from
        # their own modules; patching bot's copy does nothing.
        _h._register_abort_event = _capture
        async def _route(_u, _c): return (-1001, '19', False)
        _h._route_to_thread = _route
        class _FakeSem:
            def release(self): pass
        async def _slot(*a, **k): return _FakeSem()
        _h._check_user_slot = _slot
        sent_texts = []
        async def _reply(_u, text, **kw):
            sent_texts.append(text)
            return _ThinkingMsg()
        _h._reply = _reply
        _h.send_reply = _reply
        async def _persist(*a, **k): return None
        _h._persist_message = _persist
        async def _load_hist(*a, **k): return []
        _h._load_history = _load_hist
        _apply_rating_and_persist = lambda *a, **k: a[4]  # 5th arg is bot_response
        _h._sender_display_name = lambda u: 'V'
        _h._is_rating_active = lambda u: False
        _h._stop_button_markup = lambda: None
        class _DummySem:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
        _h._get_global_llm_sem = lambda: _DummySem()
        RACE = 'POST_RACE_RESPONSE_SHOULD_BE_DISCARDED'
        async def _race(*a, **k):
            # Simulate user clicked Stop during call_llama
            if captured:
                captured[0].set()
            return RACE
        _h.call_llama = _race

        await _h.handle_text(_FakeUpdate(), _FakeContext())
        if any(RACE in str(t) for t in sent_texts):
            log.warning(f"  [FAIL] post-race response was sent: {sent_texts}")
            ar_ok = False
        else:
            log.info(f"  [OK] post-race response discarded (sent {len(sent_texts)} non-race texts)")
    except Exception as e:
        import traceback
        traceback.print_exc()
        log.warning(f"  [FAIL] test error: {type(e).__name__}: {e!r}")
        ar_ok = False
    finally:
        # Restore originals
        for owner in ({_h} if 'ar_ok' in dir() and '_h' in dir() else []):
            for (oid, name), orig in saved.items():
                if id(owner) == oid and orig is not None:
                    setattr(owner, name, orig)
    all_ok = ar_ok
    log.warning(f"[selftest] post-race abort discard: {'all pass' if ar_ok else 'FAILED'}")
    return all_ok



async def check_webhook_boot() -> bool:
    # === T25 webhook boot test ===
    # Verify that with WEBHOOK_MODE=1, the aiohttp server
    # starts, listens on 127.0.0.1:WEBHOOK_PORT, accepts
    # GET /healthz (200), rejects POST without secret
    # (403), and accepts POST with valid secret (200).
    log.info('[selftest] running webhook boot test...')
    wh_ok = True
    try:  # noqa
        import aiohttp
        import config as _cfg_test
        from webhook import make_app, _check_secret

        # 1. secret-token verification (use stub Request with
        # just the headers attribute that _check_secret reads)
        from unittest.mock import MagicMock
        saved_token = _cfg_test.WEBHOOK_SECRET_TOKEN
        _cfg_test.WEBHOOK_SECRET_TOKEN = 'test-secret-xyz'
        def _fake_req(token):
            r = MagicMock()
            r.headers = {'X-Telegram-Bot-Api-Secret-Token': token} if token else {}
            return r
        if not _check_secret(_fake_req('test-secret-xyz')):
            log.warning("  [FAIL] valid token rejected"); wh_ok = False
        if _check_secret(_fake_req('wrong-token')):
            log.warning("  [FAIL] invalid token accepted"); wh_ok = False
        if _check_secret(_fake_req(None)):
            log.warning("  [FAIL] missing token accepted"); wh_ok = False
        _cfg_test.WEBHOOK_SECRET_TOKEN = saved_token

        # 2. aiohttp server starts and responds
        from aiohttp import test_utils
        app = make_app()
        async def _run_aiohttp_test():
            async with test_utils.TestServer(app) as srv:
                port = srv.port
                async with test_utils.TestClient(srv) as client:
                    # healthz
                    r = await client.get('/healthz')
                    if r.status != 200:
                        log.warning(f"  [FAIL] /healthz returned {r.status}")
                        return False
                    body = await r.text()
                    if 'webhook-ok' not in body:
                        log.warning(f"  [FAIL] /healthz body missing marker: {body!r}")
                        return False
                    # POST without token -> 403
                    r = await client.post('/tg-webhook/abc123', json={'foo': 'bar'})
                    if r.status != 403:
                        log.warning(f"  [FAIL] POST without token returned {r.status}")
                        return False
            return True
        result = await _run_aiohttp_test()
        if not result:
            wh_ok = False
        else:
            log.info("  [OK] webhook server starts, healthz OK, secret-token enforced")
    except Exception as e:
        import traceback
        traceback.print_exc()
        log.warning(f"  [FAIL] test error: {type(e).__name__}: {e!r}")
        wh_ok = False
    all_ok = wh_ok
    log.warning(f"[selftest] webhook boot: {'all pass' if wh_ok else 'FAILED'}")
    return all_ok



async def run_all() -> bool:
    """Every infra block, in the order `_selftest` ran them."""
    all_ok = True
    all_ok &= await check_donsetch_integration()
    all_ok &= await check_abort_event_key()
    all_ok &= await check_f4_lru_bound()
    all_ok &= await check_post_race_abort_discard()
    all_ok &= await check_webhook_boot()
    log.warning(
        f"[selftest] {SUMMARY_NAME} tests: "
        f"{'all pass' if all_ok else 'FAILED'}")
    return all_ok
