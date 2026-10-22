"""Handler-regression checks from the in-container selftest.

T1.1 slice 2. Three handlers, each of which shipped a NameError once:

  * `handle_photo` lost `is_group` in the v0.6.3 T11 cleanup, on the
    false grounds that it was unused -- it uses it on the next line.
    Every photo since crashed at the first print, so the bot never
    replied to an image.
  * `handle_voice` and `handle_document` lost `_stop_button_markup`
    in the v0.7.4 late-import trim. That is the Stop button, which is
    how a user cancels a long turn.

These are regression blocks in the literal sense -- each exists because
a specific shipped version broke -- so they live together and the module
docstring says which bug each one pins.

Same seam as slice 1: each function returns its own verdict rather than
mutating an `all_ok` threaded through 1900 lines.
"""
import asyncio

import handlers
from handlers import handle_document, handle_photo, handle_voice
from logconf import get_logger

# The name this section reports itself under. The summary line
# in `run_all` uses it, so the label cannot drift away from the
# module that owns it -- which is how the storage summary came
# to claim it was the group-mode tests.
SUMMARY_NAME = "handler regression"

log = get_logger(__name__)


async def check_handle_photo_regression() -> bool:
    # === handle_photo NameError regression test (v0.6.3 T11 bug) ===
    # The v0.6.3 T11 cleanup dropped is_group from all 4 message
    # handlers with the comment "is_group unused". But handle_photo
    # uses is_group on the next line (a print statement). Every
    # photo message since v0.6.3 crashed at the first print, so
    # the bot never replied to images. This test calls handle_photo
    # with a fake Update and asserts no NameError on is_group.
    # We patch _route_to_thread to return a 3-tuple so the
    # destructure works, and patch _check_user_slot to return
    # None so the handler short-circuits before LLM call.
    log.info('[selftest] running handle_photo NameError regression test...')
    all_ok = True
    hp_ok = True
    try:  # noqa
        class _FakePhoto:
            file_id = 'fake'
            file_size = 12345
            async def download_as_bytearray(self):
                return bytearray(b'fake-photo-bytes')
        class _FakeMessage:
            photo = [_FakePhoto()]
            caption = 'что на фото?'
            message_thread_id = 19
            chat = type('C', (), {'id': -1001, 'send_action': lambda self, **kw: asyncio.sleep(0)})()
        class _FakeUser:
            id = 286293081
            is_bot = False
            username = 'V_bot'
            first_name = 'Vasisualy'
            last_name = 'Lohankin'
        class _FakeChat:
            id = -1001
            type = 'supergroup'
        class _FakeUpdate:
            effective_chat = _FakeChat()
            effective_user = _FakeUser()
            message = _FakeMessage()
        class _FakeContext:
            bot = type('B', (), {'id': 1})()
        async def _fake_route(_u, _c):
            return (-1001, '19', True)  # is_group=True
        async def _fake_slot(*args, **kwargs):
            return None  # short-circuit before LLM
        # TECH-DEBT #1: these two are looked up in handlers' globals,
        # so the patch has to land there. Written against `bot` it was
        # a no-op and the handler ran for real.
        _orig_route = handlers._route_to_thread
        _orig_slot = handlers._check_user_slot
        handlers._route_to_thread = _fake_route
        handlers._check_user_slot = _fake_slot
        await handle_photo(_FakeUpdate(), _FakeContext())
        log.info('  [OK] handle_photo does not NameError on is_group')
    except NameError as e:
        if 'is_group' in str(e):
            log.warning(f"  [FAIL] handle_photo regressed: NameError on is_group: {e!r}")
            hp_ok = False
        else:
            log.warning(f"  [FAIL] handle_photo NameError (not is_group): {e!r}")
            hp_ok = False
    except Exception as e:
        # Other exceptions are OK (e.g. _check_user_slot returning None
        # short-circuits, or asyncio.CancelledError from send_action)
        log.info(f"  [OK] handle_photo reached past is_group (other exception: {type(e).__name__})")
    all_ok &= hp_ok
    log.warning(f"[selftest] handle_photo NameError regression: {'all pass' if hp_ok else 'FAILED'}")
    return all_ok


async def check_handle_voice_regression() -> bool:
    # === handle_voice _stop_button_markup regression (v0.7.4) ===
    # Same bug class as v0.6.3 T8: the late-import trim dropped
    # _stop_button_markup from handle_voice. handle_voice uses
    # _stop_button_markup() on the 'thinking' message (line ~568).
    # Without the import, every voice message since v0.6.3 crashed
    # with NameError before getting to the LLM call. The user
    # noticed because photo sends had the same error (handle_photo
    # is_group bug, fixed in v0.7.3). voice/document were still
    # broken.
    log.info('[selftest] running handle_voice _stop_button_markup regression test...')
    all_ok = True
    hv_ok = True
    try:  # noqa
        async def _fake_route_v(_u, _c):
            return (-1001, '19', True)
        async def _fake_slot_v(*args, **kwargs):
            return None
        # TECH-DEBT #1: these two are looked up in handlers' globals,
        # so the patch has to land there. Written against `bot` it was
        # a no-op and the handler ran for real.
        _orig_route = handlers._route_to_thread
        _orig_slot = handlers._check_user_slot
        handlers._route_to_thread = _fake_route_v
        handlers._check_user_slot = _fake_slot_v
        class _FakeVoice:
            file_id = 'fake'
            file_size = 12345
            duration = 5
            async def download_as_bytearray(self):
                return bytearray(b'fake-voice-bytes')
        class _FakeVoiceMessage:
            voice = _FakeVoice()
            caption = None
            message_thread_id = 19
            chat = type('C', (), {'id': -1001, 'send_action': lambda self, **kw: asyncio.sleep(0)})()
        class _FakeVoiceUpdate:
            effective_chat = type('C', (), {'id': -1001, 'type': 'supergroup'})()
            effective_user = type('U', (), {'id': 286293081, 'is_bot': False})()
            message = _FakeVoiceMessage()
        class _FakeVoiceContext:
            bot = type('B', (), {'id': 1})()
        await handle_voice(_FakeVoiceUpdate(), _FakeVoiceContext())
        log.info('  [OK] handle_voice does not NameError on _stop_button_markup')
    except NameError as e:
        if '_stop_button_markup' in str(e):
            log.warning(f"  [FAIL] handle_voice regressed: {e!r}")
            hv_ok = False
        else:
            log.warning(f"  [FAIL] handle_voice NameError: {e!r}")
            hv_ok = False
    except Exception as e:
        log.info(f"  [OK] handle_voice reached past _stop_button_markup ({type(e).__name__})")
    all_ok &= hv_ok
    log.warning(f"[selftest] handle_voice _stop_button_markup regression: {'all pass' if hv_ok else 'FAILED'}")
    return all_ok


async def check_handle_document_regression() -> bool:
    # === handle_document _stop_button_markup regression (v0.7.4) ===
    log.info('[selftest] running handle_document _stop_button_markup regression test...')
    all_ok = True
    hd_ok = True
    try:  # noqa
        async def _fake_route_d(_u, _c):
            return (-1001, '19', True)
        async def _fake_slot_d(*args, **kwargs):
            return None
        # TECH-DEBT #1: these two are looked up in handlers' globals,
        # so the patch has to land there. Written against `bot` it was
        # a no-op and the handler ran for real.
        _orig_route = handlers._route_to_thread
        _orig_slot = handlers._check_user_slot
        handlers._route_to_thread = _fake_route_d
        handlers._check_user_slot = _fake_slot_d
        class _FakeDoc:
            file_id = 'fake'
            file_size = 12345
            file_name = 'test.txt'
        class _FakeDocMessage:
            document = _FakeDoc()
            caption = None
            message_thread_id = 19
            chat = type('C', (), {'id': -1001, 'send_action': lambda self, **kw: asyncio.sleep(0)})()
        class _FakeDocUpdate:
            effective_chat = type('C', (), {'id': -1001, 'type': 'supergroup'})()
            effective_user = type('U', (), {'id': 286293081, 'is_bot': False})()
            message = _FakeDocMessage()
        class _FakeDocContext:
            bot = type('B', (), {'id': 1})()
        await handle_document(_FakeDocUpdate(), _FakeDocContext())
        log.info('  [OK] handle_document does not NameError on _stop_button_markup')
    except NameError as e:
        if '_stop_button_markup' in str(e):
            log.warning(f"  [FAIL] handle_document regressed: {e!r}")
            hd_ok = False
        else:
            log.warning(f"  [FAIL] handle_document NameError: {e!r}")
            hd_ok = False
    except Exception as e:
        log.info(f"  [OK] handle_document reached past _stop_button_markup ({type(e).__name__})")
    all_ok &= hd_ok
    log.warning(f"[selftest] handle_document _stop_button_markup regression: {'all pass' if hd_ok else 'FAILED'}")
    return all_ok


async def run_all() -> bool:
    """Every handler regression, in the order `_selftest` ran them."""
    all_ok = True
    all_ok &= await check_handle_photo_regression()
    all_ok &= await check_handle_voice_regression()
    all_ok &= await check_handle_document_regression()
    log.warning(
        f"[selftest] {SUMMARY_NAME} tests: "
        f"{'all pass' if all_ok else 'FAILED'}")
    return all_ok
