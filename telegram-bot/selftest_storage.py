"""Storage checks from the in-container selftest.

The first slice of T1.1. `_selftest` was 1940 lines in one function with
no runtime coverage; `tests/test_selftest_runtime.py` now runs it and
pins the marker sequence, so this block was cut with a signal that can
tell whether it still ran.

Each function returns its own verdict instead of mutating an `all_ok`
threaded through 1900 lines. That seam is worth naming: the storage
summary used to read the aggregate of everything before it and was
labelled `[selftest] group-mode tests`, which was simply false. The
label now names what it reports.

`storage.get_store()` is called inside each block, never at import
time, for the reason `_store()`'s docstring gives -- dispatch.py imports
the selftest module, and opening the database as an import side effect
would be a new failure mode on every restart.
"""
import asyncio

import storage
from logconf import get_logger

log = get_logger(__name__)


def _store():
    """The single Storage instance, via the one patch point."""
    return storage.get_store()


async def check_known_topics() -> bool:
    # === Storage: known_topics round-trip ===    # === Storage: known_topics round-trip ===
    # Add a topic, list it, find it by name, remove it. Catches
    # typos in the SQL and the new known_topics methods (commit 8).
    log.info('[selftest] running known_topics round-trip...')
    all_ok = True
    SELFTEST_CHAT = -1001
    SELFTEST_TOPIC_ID = 999999
    try:  # noqa
        await asyncio.to_thread(
            _store().add_known_topic, SELFTEST_CHAT, SELFTEST_TOPIC_ID, 'selftest-topic'
        )
        topics = await asyncio.to_thread(_store().list_known_topics, SELFTEST_CHAT)
        names = sorted(t["name"] for t in topics)
        if 'selftest-topic' not in names:
            log.warning(f"  [FAIL] add_known_topic didn't persist (got {names})")
            all_ok = False
        else:
            log.info(f"  [OK] add_known_topic persisted")
        found = await asyncio.to_thread(
            _store().find_known_topic_by_name, SELFTEST_CHAT, 'selftest-topic'
        )
        if found is None or found["message_thread_id"] != SELFTEST_TOPIC_ID:
            log.warning(f"  [FAIL] find_known_topic_by_name returned {found}")
            all_ok = False
        else:
            log.info(f"  [OK] find_known_topic_by_name returned id={found['message_thread_id']}")
        await asyncio.to_thread(
            _store().remove_known_topic, SELFTEST_CHAT, SELFTEST_TOPIC_ID
        )
        topics_after = await asyncio.to_thread(_store().list_known_topics, SELFTEST_CHAT)
        if any(t["name"] == 'selftest-topic' for t in topics_after):
            log.warning(f"  [FAIL] remove_known_topic didn't remove (got {[t['name'] for t in topics_after]})")
            all_ok = False
        else:
            log.info(f"  [OK] remove_known_topic worked")
        # find_known_topic_by_id: re-add, then look up by id (not name).
        await asyncio.to_thread(
            _store().add_known_topic, SELFTEST_CHAT, SELFTEST_TOPIC_ID, 'selftest-topic'
        )
        by_id = await asyncio.to_thread(
            _store().find_known_topic_by_id, SELFTEST_CHAT, SELFTEST_TOPIC_ID
        )
        if by_id is None or by_id["name"] != 'selftest-topic':
            log.warning(f"  [FAIL] find_known_topic_by_id returned {by_id}")
            all_ok = False
        else:
            log.info(f"  [OK] find_known_topic_by_id resolved to '{by_id['name']}'")
        # Negative case: a non-existent id.
        missing = await asyncio.to_thread(
            _store().find_known_topic_by_id, SELFTEST_CHAT, 999999999
        )
        if missing is not None:
            log.warning(f"  [FAIL] find_known_topic_by_id returned non-None for missing id: {missing}")
            all_ok = False
        else:
            log.info(f"  [OK] find_known_topic_by_id returns None for missing id")
        # Cleanup
        await asyncio.to_thread(
            _store().remove_known_topic, SELFTEST_CHAT, SELFTEST_TOPIC_ID
        )
    except Exception as e:
        log.warning(f"  [FAIL] known_topics round-trip crashed: {type(e).__name__}: {e}")
        all_ok = False
    return all_ok


async def check_no_prune() -> bool:
    # === Storage: history is never pruned (T67) ===
    # The inverse of the old lazy-trim test. That one asserted that
    # crossing TRIM_HIGH rows left the table at TRIM_LOW — i.e. it
    # asserted that get_messages DELETES history, which is exactly what
    # broke LoRA training data. This one asserts nothing is deleted.
    log.info('[selftest] running no-prune test...')
    all_ok = True
    # storage.py sits next to bot.py in the container (/app/storage.py
    # per the Dockerfile `COPY bot.py storage.py ./`); module name
    # "storage" would clash with stdlib's `storage` if we ever need
    # to import it normally. Use spec_from_file_location to load it
    # under a private name without touching sys.modules globally.
    # W2-M1. This was spec_from_file_location("_selftest_storage",
    # "/app/storage.py"). Three container paths hardcoded inside a function
    # whose contract is that it never raises: anywhere but the image's /app
    # the first one threw FileNotFoundError and aborted the run with 85 of
    # 110 assertion sites never reached -- 22% of the smoke test.
    #
    # The stdlib clash the old comment warned about is not real. `storage` is
    # already imported at module level and this directory is on sys.path,
    # which is the only reason that import resolves. Use the module we have.
    import storage as _storage_mod
    NOPRUNE_ROWS = 700  # above the old TRIM_HIGH of 500
    NOPRUNE_CHAT = -1002
    NOPRUNE_THREAD = "noprun-test"
    try:  # noqa
        # Wipe any leftover rows
        await asyncio.to_thread(_store().delete_thread, NOPRUNE_CHAT, NOPRUNE_THREAD)
        for i in range(NOPRUNE_ROWS):
            await asyncio.to_thread(
                _store().add_message, NOPRUNE_CHAT, NOPRUNE_THREAD,
                "user", f'{{"role":"user","content":"msg {i}"}}'
            )
        # Read back with a ceiling above what we wrote
        rows = (
            await asyncio.to_thread(
                _store().get_messages, NOPRUNE_CHAT, NOPRUNE_THREAD, 100000
            )
        )
        if len(rows) != NOPRUNE_ROWS:
            log.warning(f"  [FAIL] history pruned: got {len(rows)} rows, expected {NOPRUNE_ROWS}")
            all_ok = False
        else:
            log.info(f"  [OK] all {NOPRUNE_ROWS} rows survived get_messages (no pruning)")
        # Cleanup
        await asyncio.to_thread(_store().delete_thread, NOPRUNE_CHAT, NOPRUNE_THREAD)
    except Exception as e:
        log.warning(f"  [FAIL] no-prune test crashed: {type(e).__name__}: {e}")
        all_ok = False
    return all_ok


async def check_rating_roundtrip() -> bool:
    # === Storage: rating column (v3 schema) ===
    # add_message with rating=N must persist N; get_messages
    # must return it. Round-trip.
    log.info('[selftest] running rating round-trip...')
    all_ok = True
    RATING_CHAT = -1003
    RATING_THREAD = "rating-test"
    try:  # noqa
        await asyncio.to_thread(_store().delete_thread, RATING_CHAT, RATING_THREAD)
        await asyncio.to_thread(
            _store().add_message, RATING_CHAT, RATING_THREAD,
            "user", '{"role":"user","content":"x"}',
        )
        await asyncio.to_thread(
            _store().add_message, RATING_CHAT, RATING_THREAD,
            "assistant", '{"role":"assistant","content":""}',
            rating=7,
        )
        await asyncio.to_thread(
            _store().add_message, RATING_CHAT, RATING_THREAD,
            "assistant", '{"role":"assistant","content":"hi"}',
            rating=None,  # no rating for normal text response
        )
        msgs = await asyncio.to_thread(
            _store().get_messages, RATING_CHAT, RATING_THREAD, 10
        )
        # ms2_msgs[0] is oldest, [1] is rated, [2] is unrated
        if len(msgs) != 3:
            log.warning(f"  [FAIL] expected 3 rows, got {len(msgs)}")
            all_ok = False
        elif msgs[1].get("rating") != 7:
            log.warning(f"  [FAIL] rated message has rating={msgs[1].get('rating')!r}, expected 7")
            all_ok = False
        elif msgs[2].get("rating") is not None:
            log.warning(f"  [FAIL] unrated message has rating={msgs[2].get('rating')!r}, expected None")
            all_ok = False
        else:
            log.info(f"  [OK] rating 7 persisted on row 1, None on row 2")
        # Cleanup
        await asyncio.to_thread(_store().delete_thread, RATING_CHAT, RATING_THREAD)
    except Exception as e:
        log.warning(f"  [FAIL] lazy-trim test crashed: {type(e).__name__}: {e}")
        all_ok = False

    log.warning(f"[selftest] group-mode tests: {'all pass' if all_ok else 'FAILED'}")
    return all_ok


async def run_all() -> bool:
    """Every storage block, in the order `_selftest` ran them."""
    all_ok = True
    all_ok &= await check_known_topics()
    all_ok &= await check_no_prune()
    all_ok &= await check_rating_roundtrip()
    log.warning(
        f"[selftest] storage tests: {'all pass' if all_ok else 'FAILED'}")
    return all_ok
