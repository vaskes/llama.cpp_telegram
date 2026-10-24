"""In-container smoke test: push a fake Update through the real dispatcher.

Extracted verbatim from bot.py (TECH-DEBT #1). It stays in the shipped image
rather than the test suite because it has to exercise the real import graph
and the real /app paths inside the running container — `pytest` proves the
logic, this proves the deployment.

Runs only when LLAMABOT_SELFTEST=1; it is a no-op otherwise, and production
runs with the flag off.

The conversation key is deliberately not a real user_id: it avoids leaking
test data into a whitelisted user's history and cannot collide with a real
Telegram account. The fake user id is 0, which Telegram reserves.

What this module now depends on is one-directional: selftest -> bot, never
bot -> selftest. That is what lets dispatch.py import it at module level
instead of late, which was the only reason bot.py had to import dispatch.
#
# selftest no longer imports bot at all: the names below come from the
# modules that define them.
"""

from pathlib import Path
import os

# TECH-DEBT #1 extracted this module from bot.py, and the extraction cut it
# loose from bot.py's namespace. Six names used to resolve through that
# facade and now raise NameError -- but only when LLAMABOT_SELFTEST=1, which
# production never sets, so the shipped smoke test went unrunnable and the
# test that claims to pin it kept passing.
#
# The dependency stays one-directional: selftest -> dispatch/state, never
# the reverse.
#
# `Update.de_json`, `_parse_rating_response`, `RATING_EMOJI`,
# `WELCOME_TEXT` and the per-user semaphore names used to be imported
# here. T1.1 moved the blocks that used them into leaf modules, and they
# are imported there now -- keeping them here would have left the
# impression that this file still depends on them, which is exactly the
# claim `test_drift_*` tripwires exist to keep honest.

# === Owning modules (TECH-DEBT #1) ===
#
# Everything below used to be reached through `import bot as _b`. That was
# not a style problem, it was a correctness one: `bot` is a compatibility
# surface, and a patch written against it lands on an attribute nobody
# reads. The names are imported from the module that defines them.
#
# `dispatch` is deliberately NOT imported here. dispatch.py imports this
# module at module level (so the dispatcher can run the smoke test), so a
# module-level import of dispatch back would close a cycle. It stays a
# lazy import inside the places that need it.
import storage
# T1.1: the storage blocks, cut out of `_selftest` and driven from
# `run_all()`. Imported at module level, not lazily, because it has no
# back-edge to this module -- `dispatch.py` imports *this* module, so
# anything importing selftest back would close the cycle.
import selftest_access
import selftest_handlers
import selftest_infra
import selftest_llm
import selftest_media
import selftest_state
import selftest_storage
from logconf import get_logger
log = get_logger(__name__)


def _lazy_dispatch():
    """dispatch imports this module, so it can only be imported lazily.

    Three call sites need dispatch; all three are inside _selftest().
    """
    import dispatch
    return dispatch


def _store():
    """The single Storage instance, via the one patch point.

    Not a module-level `get_store()` call: dispatch.py imports this module
    at import time, and opening the database as a side effect of importing
    dispatch would be a new failure mode on every restart.
    """
    return storage.get_store()


# state is safe at module level: dispatch imports it, it imports nothing back.
# The global LLM semaphore names used to be imported here for the F2/NEW-2
# block, which T1.1 moved to selftest_llm.py alongside the rest of the LLM
# path. The infra blocks patch those names as strings on `handlers`, which
# is where handle_text resolves them, so no name is needed here.

# _dispatch_update MUST stay a lazy import. dispatch.py imports this module at
# module level (so the dispatcher can run the smoke test), so importing it
# back here closes the exact cycle the import-graph tripwire forbids --
# pytest reports it as 24 unrelated ImportError failures, not as a cycle,
# because the cycle surfaces in whichever module happens to be imported first.

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

def _module_source(name):
    """Read a sibling module's source without assuming a container path.

    Three places here opened a path under /app. That works inside the image
    and nowhere else, and each was an abort rather than a skip -- a smoke
    test that stops early reports the same green as one that ran.

    The directory comes from an already-imported sibling rather than from
    __file__: selftest.py and storage.py ship in the same place, and using
    __file__ introduced a free name that
    tests/test_refactor_graph.py rightly refuses to resolve.
    """
    import storage

    return (Path(storage.__file__).resolve().parent / f"{name}.py").read_text(
        encoding="utf-8")


async def _selftest():
    """Skip in production — only used via env LLAMABOT_SELFTEST=1."""
    if os.environ.get('LLAMABOT_SELFTEST') != '1':
        return
    import time
    # Lazy on purpose -- see the note at the top of this file.
    from dispatch import _dispatch_update
    log.info('[selftest] starting')
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
    # Cut to selftest_llm.py by T1.1. It has no verdict of its own --
    # it swallows every exception on purpose, so a smoke test that
    # takes the production process down is not a smoke test.
    await selftest_llm.check_vision()

    # === Group-mode routing smoke test ===
    await selftest_llm.check_group_routing()
    # === Handler regression checks ===
    # handle_photo's is_group, handle_voice's and handle_document's
    # _stop_button_markup -- three handlers that each shipped a
    # NameError. Cut to selftest_handlers.py by T1.1; the blocks are
    # byte-identical apart from carrying their own verdict.
    await selftest_handlers.run_all()

    # === call_llama abort during non-streaming POST ===
    # Cut to selftest_llm.py by T1.1.
    await selftest_llm.check_call_llama_abort()

    # === Storage checks ===
    # known_topics round-trip, the T67 no-prune guarantee, and the
    # v3 rating column. Cut to selftest_storage.py by T1.1; the
    # blocks are byte-identical apart from carrying their own verdict.
    await selftest_storage.run_all()

    # === Access filters: group-mode noise, reply-to-other-user ===
    # The two decisions that gate whether the bot answers a message at
    # all. Cut to selftest_access.py by T1.1; the blocks are
    # byte-identical apart from carrying their own verdict.
    await selftest_access.run_all()

    # === State and rating ===
    # Per-user semaphore, the rating grammar, the emoji map and the
    # chat_member join path. Cut to selftest_state.py by T1.1; the
    # blocks are byte-identical apart from carrying their own verdict.
    await selftest_state.run_all()

    # === Media and attribution ===
    # The download size cap and the sender-name flow.
    # Cut to selftest_media.py by T1.1; the blocks are byte-identical
    # apart from carrying their own verdict.
    await selftest_media.run_all()

    # === Malformed tool-call JSON rejection ===
    # Cut to selftest_llm.py by T1.1.
    await selftest_llm.check_malformed_tool_json()

    # === react_to_message tool ===
    # Cut to selftest_llm.py by T1.1.
    await selftest_llm.check_react_to_message()

    # === Non-streaming tool_call path ===
    # Cut to selftest_llm.py by T1.1.
    await selftest_llm.check_nonstream_tool_call()

    # === GROUP_CONTEXT and the global LLM semaphore ===
    # Cut to selftest_llm.py by T1.1.
    await selftest_llm.check_group_context()

    # === Infrastructure ===
    # donsetch integration, the per-handler abort_event key, the F4
    # LRU bound, the post-won race discard and the webhook boot.
    # Cut to selftest_infra.py by T1.1; this is the last block.
    await selftest_infra.run_all()

    # No aggregate verdict here, and that is deliberate.
    #
    # This function used to thread one `all_ok` flag through 1940 lines
    # and then never read it -- the smoke test has no exit code and
    # reported by logging, so the flag was write-only before T1.1 as
    # well. What made it dangerous was not that it was unread but that
    # it looked load-bearing: the summary sitting at the end of the
    # storage blocks read the aggregate of everything before it and was
    # labelled `[selftest] group-mode tests`, which was false.
    #
    # Now every leaf returns its own verdict AND logs its own summary
    # line, and `selftest_golden.log` pins those lines. The aggregate
    # would restate what the golden already holds, so it is gone rather
    # than given a reader.
    #
    # A mutation that drops one call site below is now caught by
    # `test_every_block_is_awaited_by_selftest`, which is the direction
    # that replaced the flag.
    log.info('[selftest] done')
    log.info('[selftest] done')
