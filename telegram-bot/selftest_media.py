"""Media and attribution checks from the in-container selftest.

T1.1 slice 5. Three blocks about files coming in and about who sent
them:

  * the `_bot_replies` map -- (chat, user_msg) -> bot_msg, which is how
    a later poll knows which of its own messages to edit;
  * `_download_with_limit` -- the size cap, with the two distinct
    rejection paths. PTB 21.0 removed `File.download_as_chunks`, and any
    call to it surfaced to the user as "Failed to download photo/voice",
    so this block exists to keep `download_as_bytearray` in place;
  * the sender-name flow -- display-name resolution, the
    persist/`_load_history` round-trip of `sender_name`, and the
    `From: <name>: ` tag for text and multimodal content.

`_store` is copied from `selftest.py` rather than imported, for the
reason that function's docstring gives: `storage.get_store()` must not
run at import time, because dispatch.py imports this package and opening
the database as a side effect of that is a new failure mode on every
restart.
"""
import asyncio

import storage
from handlers import _download_with_limit, _sender_display_name
from persistence import load_history as _load_history
from persistence import persist as _persist_message
from state import _bot_replies
from logconf import get_logger

# The name this section reports itself under. The summary line
# in `run_all` uses it, so the label cannot drift away from the
# module that owns it -- which is how the storage summary came
# to claim it was the group-mode tests.
SUMMARY_NAME = "media and attribution"

log = get_logger(__name__)


def _store():
    """The single Storage instance, via the one patch point.

    Not a module-level `get_store()` call: dispatch.py imports this
    module at import time, and opening the database as a side effect of
    importing dispatch would be a new failure mode on every restart.
    """
    return storage.get_store()


async def check_edit_replace() -> bool:
    # === Edit-replace test ===
    # Verifies the _bot_replies tracking and the new edit-handling
    # logic in the polling loop. We don't run the full polling loop
    # (it requires a live Telegram server), but we exercise the
    # data structure: record a (chat, user_msg) → bot_msg mapping,
    # then verify it can be retrieved and popped.
    log.info('[selftest] running edit-replace test...')
    er_ok = True
    try:  # noqa
        # Save the existing map and clear it for the test
        saved = dict(_bot_replies)
        _bot_replies.clear()
        # Record a mapping
        _bot_replies[(-1001234567890, 100)] = 200
        _bot_replies[(-1001234567890, 101)] = 201
        # Lookup works
        if _bot_replies.get((-1001234567890, 100)) != 200:
            log.warning("  [FAIL] _bot_replies.get() returned wrong bot_msg_id")
            er_ok = False
        else:
            log.info("  [OK] _bot_replies stores (chat, user_msg) → bot_msg mapping")
        # Pop removes the entry
        popped = _bot_replies.pop((-1001234567890, 100), None)
        if popped != 200:
            log.warning(f"  [FAIL] pop returned {popped}, expected 200")
            er_ok = False
        elif (-1001234567890, 100) in _bot_replies:
            log.warning("  [FAIL] entry not actually removed by pop")
            er_ok = False
        else:
            log.info("  [OK] pop removes entry correctly")
        # Pop on missing key returns None (no exception)
        if _bot_replies.pop((-1001234567890, 99999), None) is not None:
            log.warning("  [FAIL] pop on missing key should return None")
            er_ok = False
        else:
            log.info("  [OK] pop on missing key returns None")
        # Restore the saved state
        _bot_replies.clear()
        _bot_replies.update(saved)
    except Exception as e:
        log.warning(f"  [FAIL] edit-replace test raised: {type(e).__name__}: {e!r}")
        er_ok = False
    all_ok = er_ok
    log.warning(f"[selftest] edit-replace tests: {'all pass' if er_ok else 'FAILED'}")
    return all_ok


async def check_download_with_limit() -> bool:
    # === _download_with_limit (PTB 21 download_as_chunks regression guard) ===
    # Bug: PTB 21.0 removed File.download_as_chunks. Any code path
    # still calling it raised "File object has no attribute
    # download_as_chunks", which the bot surfaced as
    # "Failed to download photo/voice" to the user. We now use
    # File.download_as_bytearray() inside _download_with_limit.
    # These 4 cases verify the helper behaves correctly with a
    # mock File object, without needing a real Telegram file.
    log.info('[selftest] running _download_with_limit test...')
    dl_ok = True
    try:  # noqa
        # The F3 module split made _download_with_limit's internal
        # `await _reply(update, ...)` resolve to handlers._reply
        # (local name in handlers.py), not bot._reply. So patching
        # _reply = _fake_reply doesn't work. Instead, use a
        # _FakeUpdate that satisfies what the real _reply() reads:
        # update.effective_chat (via _is_group_chat) and
        # update.message (for the actual sendMessage call, which
        # we never let reach the network because _is_group_chat
        # returns False for chat=None).
        captured_replies = []
        async def _fake_send_message(**kwargs):
            captured_replies.append(kwargs.get('text', ''))
            class _Sent:
                message_id = 999
            return _Sent()

        class _FakeMessage:
            async def reply_text(self, text, **kwargs):
                return await _fake_send_message(text=text, **kwargs)

        class _FakeChat:
            id = 12345

        class _FakeUpdate:
            effective_chat = None  # private mode -> _is_group_chat returns False
            message = _FakeMessage()

        # Case 1: file_size set + over max_bytes -> reject without download.
        class _BigFile:
            file_size = 999_999_999
            async def download_as_bytearray(self):
                raise AssertionError("download should not be called when file_size is over the cap")
        r = await _download_with_limit(_BigFile(), 100, "Photo", _FakeUpdate())
        if r is not None or not captured_replies or "too large" not in captured_replies[-1]:
            log.warning("  [FAIL] oversized file: expected None + 'too large' reply")
            dl_ok = False
        else:
            log.info("  [OK] oversized file rejected via file_size pre-check (no download)")

        # Case 2: file_size missing + small download -> success.
        captured_replies.clear()
        class _GoodFile:
            file_size = None
            async def download_as_bytearray(self):
                return bytearray(b"hello" * 50)  # 250 bytes
        r = await _download_with_limit(_GoodFile(), 100_000, "Photo", _FakeUpdate())
        if r is None or len(r) != 250 or captured_replies:
            log.warning(f"  [FAIL] small file: expected 250 bytes, got {len(r) if r else None}, replies={captured_replies}")
            dl_ok = False
        else:
            log.info("  [OK] small file downloaded (250 bytes, no error reply)")

        # Case 3: download raises -> friendly error, no download_as_chunks mention.
        captured_replies.clear()
        class _FailingFile:
            file_size = None
            async def download_as_bytearray(self):
                raise RuntimeError("network error")
        r = await _download_with_limit(_FailingFile(), 100_000, "Photo", _FakeUpdate())
        if r is not None or not captured_replies or "Failed to download" not in captured_replies[-1]:
            log.warning(f"  [FAIL] failing file: expected None + 'Failed to download' reply, got {r!r} {captured_replies!r}")
            dl_ok = False
        elif "download_as_chunks" in captured_replies[-1]:
            log.warning(f"  [FAIL] download_as_chunks still mentioned in error: {captured_replies[-1]!r}")
            dl_ok = False
        else:
            log.info("  [OK] download exception -> 'Failed to download' reply (no download_as_chunks mention)")

        # Case 4: file_size missing + download returns oversized buf -> post-check rejects.
        captured_replies.clear()
        class _BigBufFile:
            file_size = None  # PTB didn't populate
            async def download_as_bytearray(self):
                return bytearray(b"x" * 200_000)  # 200 KB, > 100 KB cap
        r = await _download_with_limit(_BigBufFile(), 100_000, "Photo", _FakeUpdate())
        if r is not None or not captured_replies or "too large" not in captured_replies[-1]:
            log.warning(f"  [FAIL] big buf: expected None + 'too large' reply, got {r!r} {captured_replies!r}")
            dl_ok = False
        else:
            log.info("  [OK] post-check on len(buf) catches oversized file when file_size is missing")
    except Exception as e:
        log.warning(f"  [FAIL] _download_with_limit test raised: {type(e).__name__}: {e!r}")
        dl_ok = False
    all_ok = dl_ok
    log.warning(f"[selftest] _download_with_limit tests: {'all pass' if dl_ok else 'FAILED'}")
    return all_ok


async def check_sender_name_flow() -> bool:
    # === Sender-name flow (group-mode speaker attribution) ===
    # Bug: the bot could not distinguish between two humans writing
    # in the same group topic. The LLM only saw "user: ok" twice and
    # had no way to tell Vasisualy from Dimon apart. Fix: each user
    # message is persisted with sender_name (resolved from
    # update.message.from_user), and call_llama's _tag_sender
    # closure prepends "From: <name>: " to every user content
    # (text and multimodal alike) so the model can read who said
    # what. Old rows from before the v3 -> v4 migration have
    # sender_name=NULL and fall back to a generic "user" prefix.
    log.info('[selftest] running sender-name flow test...')
    sn_ok = True
    try:  # noqa
        from telegram import User as _U

        # Case 1: display-name resolution.
        n_vasya = _sender_display_name(_U(id=111, first_name='Vasisualy', last_name='Lohankin', username='V_bot', is_bot=False))
        n_dima = _sender_display_name(_U(id=222, first_name='Dima', last_name=None, username='KlimDimm', is_bot=False))
        n_anon = _sender_display_name(_U(id=333, first_name=None, last_name=None, username=None, is_bot=False))
        n_none = _sender_display_name(None)
        if n_vasya != 'Vasisualy Lohankin' or n_dima != 'Dima' or n_anon != 'user_333' or n_none is not None:
            log.warning(f"  [FAIL] name resolution: {n_vasya!r} {n_dima!r} {n_anon!r} {n_none!r}")
            sn_ok = False
        else:
            log.info('  [OK] _sender_display_name resolves first+last / first / @user / user_id / None')

        # Case 2: persist + load round-trip preserves sender_name.
        # We use the real global store but a fake chat_id so the
        # selftest rows are easy to identify and clean up.
        SN_CHAT = -1001234567890
        SN_THREAD = 'selftest-sender-name'
        # Clean any leftover rows from a previous run of this test.
        def _pre_cleanup():
            import sqlite3 as _sq
            cc = _sq.connect(_store().path)
            cc.execute('DELETE FROM messages WHERE chat_id = ? AND thread_id = ?',
                       (SN_CHAT, SN_THREAD))
            cc.commit()
            cc.close()
        await asyncio.to_thread(_pre_cleanup)
        await _persist_message(SN_CHAT, SN_THREAD, 'user', 'hi from vasya', sender_name=n_vasya)
        await _persist_message(SN_CHAT, SN_THREAD, 'user', 'hi from dima', sender_name=n_dima)
        await _persist_message(SN_CHAT, SN_THREAD, 'assistant', 'hello both', None)
        history = await _load_history(SN_CHAT, SN_THREAD)
        # Use .get() (not []) so a missing key is a no-op (test continues
        # with sn_ok=False) instead of crashing the whole selftest with
        # a KeyError. Defensive against future schema changes.
        if (len(history) != 3
                or history[0].get('_sender_name') != n_vasya
                or history[1].get('_sender_name') != n_dima
                or history[2].get('_sender_name') is not None):
            log.warning(f"  [FAIL] persist+load: {[h.get('_sender_name') for h in history]!r}")
            sn_ok = False
        else:
            log.info('  [OK] persist + _load_history round-trip preserves sender_name (3 rows)')

        # Case 3: _tag_sender formatting on text content.
        def _tag(m):
            if not isinstance(m, dict) or m.get('role') != 'user':
                return m
            name = m.get('_sender_name')
            tag = (name.strip() if isinstance(name, str) and name.strip() else 'user')
            content = m.get('content')
            if isinstance(content, str):
                return {**m, 'content': f'From: {tag}: {content}'}
            if isinstance(content, list):
                parts = list(content)
                if parts and isinstance(parts[0], dict) and parts[0].get('type') == 'text':
                    parts[0] = {**parts[0], 'text': f'From: {tag}: ' + parts[0].get('text', '')}
                    return {**m, 'content': parts}
            return m
        tagged = [_tag(m) for m in history]
        if 'From: Vasisualy Lohankin: hi from vasya' not in tagged[0]['content']:
            log.warning(f"  [FAIL] text tag: {tagged[0]['content']!r}")
            sn_ok = False
        elif 'From: Dima: hi from dima' not in tagged[1]['content']:
            log.warning(f"  [FAIL] dima tag: {tagged[1]['content']!r}")
            sn_ok = False
        elif tagged[2]['content'] != 'hello both':
            log.warning(f"  [FAIL] assistant should be untouched, got {tagged[2]['content']!r}")
            sn_ok = False
        else:
            log.info('  [OK] _tag_sender prepends From: <name>: to text content; assistant unchanged')

        # Case 4: multimodal content (list of parts) - sender tag
        # goes into the first text part, image_url untouched.
        mm = {
            'role': 'user',
            '_sender_name': 'Vasisualy Lohankin',
            'content': [
                {'type': 'text', 'text': 'Что на картинке?'},
                {'type': 'image_url', 'image_url': {'url': 'data:...'}},
            ],
        }
        tagged_mm = _tag(mm)
        if (isinstance(tagged_mm['content'], list)
                and tagged_mm['content'][0]['text'] == 'From: Vasisualy Lohankin: Что на картинке?'
                and tagged_mm['content'][1] == {'type': 'image_url', 'image_url': {'url': 'data:...'}}):
            log.info('  [OK] multimodal: sender tag in first text part, image preserved')
        else:
            log.warning(f"  [FAIL] multimodal tag: {tagged_mm!r}")
            sn_ok = False

        # Case 5: NULL sender_name (old pre-migration row) falls back
        # to a generic 'user' prefix so the LLM still knows it's a
        # person, just unnamed.
        legacy = {'role': 'user', '_sender_name': None, 'content': 'old message'}
        if _tag(legacy)['content'] != 'From: user: old message':
            log.warning(f"  [FAIL] NULL fallback: {_tag(legacy)!r}")
            sn_ok = False
        else:
            log.info('  [OK] NULL sender_name falls back to From: user: (no name but still tagged)')

        # Cleanup: remove the 3 test rows so re-running the selftest
        # is idempotent and the real DB doesn't accumulate junk. We
        # open a private sqlite3 connection to the same DB path so we
        # don't fight the bot's threading.local() connection. The
        # WAL mode means concurrent readers + the one writer won't
        # block each other; cleanup is short-lived and best-effort.
        def _cleanup():
            import sqlite3 as _sq
            cc = _sq.connect(_store().path)
            cc.execute('DELETE FROM messages WHERE chat_id = ? AND thread_id = ?',
                       (SN_CHAT, SN_THREAD))
            cc.commit()
            cc.close()
        await asyncio.to_thread(_cleanup)
    except Exception as e:
        log.warning(f"  [FAIL] sender-name test raised: {type(e).__name__}: {e!r}")
        sn_ok = False
    all_ok = sn_ok
    log.warning(f"[selftest] sender-name tests: {'all pass' if sn_ok else 'FAILED'}")
    return all_ok


async def run_all() -> bool:
    """Every media/attribution block, in the order `_selftest` ran them."""
    all_ok = True
    all_ok &= await check_edit_replace()
    all_ok &= await check_download_with_limit()
    all_ok &= await check_sender_name_flow()
    log.warning(
        f"[selftest] {SUMMARY_NAME} tests: "
        f"{'all pass' if all_ok else 'FAILED'}")
    return all_ok