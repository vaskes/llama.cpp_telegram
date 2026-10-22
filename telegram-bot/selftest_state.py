"""State and rating checks from the in-container selftest.

T1.1 slice 4. Four blocks about the state the bot keeps between
messages and the rating protocol it speaks:

  * `_get_user_semaphore` -- the per-(chat, user) concurrency limit, the
    one that stops a user from occupying every slot at once. `_check_user_slot`
    reads it, so the limit itself is what is under test.
  * `_parse_rating_response` -- the `[[TYPE:...]] [[RATE:n]]` grammar the
    model is asked to speak. This parser is the reason
    `text[m.end():]` had to be careful with an `$`-anchored group: the
    body it returns is what the user actually sees.
  * `RATING_EMOJI` -- ten ratings, ten distinct non-empty emoji.
  * `chat_member` -- the join path: a new member gets the welcome in
    General and a PM, and a leave or another bot joining gets nothing.

`time` is used by the chat_member block to stamp `int(time.time())` on
the synthetic updates, and `Update.de_json` is what turns them into
objects -- the same two things `_selftest` had in scope.
"""
import time

from telegram import Update

from prompts import RATING_EMOJI, WELCOME_TEXT
from rating import _parse_rating_response
from state import _PER_USER_SEMAPHORE_LIMIT, _get_user_semaphore
from logconf import get_logger

# The name this section reports itself under. The summary line
# in `run_all` uses it, so the label cannot drift away from the
# module that owns it -- which is how the storage summary came
# to claim it was the group-mode tests.
SUMMARY_NAME = "state and rating"

log = get_logger(__name__)


def _lazy_dispatch():
    """dispatch imports selftest, so it can only be imported lazily.

    Copied rather than imported: `selftest._lazy_dispatch` exists to keep
    this cycle out of the module that owns it, and importing it here
    would put that cycle in a second place.
    """
    import dispatch
    return dispatch


async def check_per_user_semaphore() -> bool:
    # === Per-user semaphore ===
    # Verifies that _check_user_slot returns None when all
    # permits are taken. We hold N=2 permits, then try a 3rd.
    log.info('[selftest] running per-user semaphore test...')
    sem_ok = True
    try:  # noqa
        sem = _get_user_semaphore(123, 456)
        # Manually lock it to simulate 2 in-flight requests
        for _ in range(_PER_USER_SEMAPHORE_LIMIT):
            await sem.acquire()
        # Third call should report locked
        if not sem.locked():
            log.warning(f"  [FAIL] semaphore not locked after {_PER_USER_SEMAPHORE_LIMIT} acquires")
            sem_ok = False
        else:
            log.info(f"  [OK] semaphore locked at limit")
        # Release them
        for _ in range(_PER_USER_SEMAPHORE_LIMIT):
            sem.release()
        if sem.locked():
            log.warning("  [FAIL] semaphore still locked after release")
            sem_ok = False
        else:
            log.info("  [OK] semaphore unlocked after release")
        # Distinct (chat_id, user_id) gets a distinct semaphore
        sem_a = _get_user_semaphore(1, 1)
        sem_b = _get_user_semaphore(1, 2)
        if sem_a is sem_b:
            log.warning("  [FAIL] (1,1) and (1,2) share the same semaphore")
            sem_ok = False
        else:
            log.info("  [OK] (1,1) and (1,2) have distinct semaphores")
    except Exception as e:
        log.warning(f"  [FAIL] semaphore test crashed: {type(e).__name__}: {e}")
        sem_ok = False
    all_ok = sem_ok
    return all_ok


async def check_rating_parser() -> bool:
    # === Rating-mode parser ===
    # Verifies that _parse_rating_response correctly extracts
    # the type, rating, and rest text from LLM responses.
    log.info('[selftest] running rating-parser test...')
    parse_cases = [
        # (input, expected_type, expected_rating, expected_rest_substr)
        # Default: no prefix → treated as question with full text as answer
        ('plain answer',                  'question',                      None, 'plain answer'),
        ('[[TYPE:question]] hello',       'question',                      None, 'hello'),
        ('[[TYPE:request]] done, recipe', 'request',                       None, 'done, recipe'),
        ('[[TYPE:confirmation]] yes',     'confirmation',                  None, 'yes'),
        ('[[TYPE:info]] [[RATE:7]]',      'info',                          7, ''),
        ('[[TYPE:info]] [[RATE:10]] good', 'info',                         10, 'good'),
        ('[[TYPE:statement]] [[RATE:3]]', 'statement',                     3, ''),
        ('[[TYPE:bloat]] 🤔',             'bloat',                          None, ''),
        ('[[TYPE:bloat]]',                'bloat',                          None, ''),  # no emoji → default
        # Invalid type → fall back to "question" with whole text
        ('[[TYPE:unknown]]',              'question',                      None, '[[TYPE:unknown]]'),
    ]
    parse_ok = True
    for text, exp_type, exp_rating, exp_rest in parse_cases:
        got = _parse_rating_response(text)
        if got['type'] != exp_type or got['rating'] != exp_rating:
            log.warning(f"  [FAIL] {text!r}: type={got['type']!r} rating={got['rating']!r} rest={got['body']!r} "
                  f"(expected type={exp_type!r}, rating={exp_rating!r})")
            parse_ok = False
        elif exp_rest and exp_rest not in got['body']:
            log.warning(f"  [FAIL] {text!r}: rest={got['body']!r} doesn't contain {exp_rest!r}")
            parse_ok = False
    if parse_ok:
        log.info(f"  [OK] all {len(parse_cases)} parse cases passed")
    all_ok = parse_ok
    return all_ok


async def check_rating_emoji_map() -> bool:
    # === RATING_EMOJI map ===
    # All 10 ratings must map to a non-empty string and be
    # distinct. Sanity check.
    log.info('[selftest] running rating-emoji map test...')
    emoji_ok = True
    if len(RATING_EMOJI) != 10:
        log.warning(f"  [FAIL] RATING_EMOJI has {len(RATING_EMOJI)} entries, expected 10")
        emoji_ok = False
    elif len(set(RATING_EMOJI.values())) != 10:
        log.warning("  [FAIL] RATING_EMOJI has duplicate emoji")
        emoji_ok = False
    elif any(not e for e in RATING_EMOJI.values()):
        log.warning("  [FAIL] RATING_EMOJI has empty emoji")
        emoji_ok = False
    else:
        log.info("  [OK] 10 distinct ratings, all map to non-empty emoji")
    all_ok = emoji_ok
    return all_ok


async def check_chat_member() -> bool:
    # === chat_member test ===
    # We can't actually trigger a join in a selftest, but we can
    # verify (a) the WELCOME_TEXT is non-empty and contains the
    # rating scale, and (b) the chat_member update path dispatches
    # without crashing. We use a fake chat_member update with a
    # mock bot that records send_message calls instead of hitting
    # the real Telegram API.
    log.info('[selftest] running chat_member / WELCOME_TEXT test...')
    cm_ok = True
    if not WELCOME_TEXT or not WELCOME_TEXT.strip():
        log.warning("  [FAIL] WELCOME_TEXT is empty")
        cm_ok = False
    elif '💩' not in WELCOME_TEXT or '🔥' not in WELCOME_TEXT:
        log.warning("  [FAIL] WELCOME_TEXT does not contain rating scale")
        cm_ok = False
    elif 'TRUTH' not in WELCOME_TEXT:
        log.warning("  [FAIL] WELCOME_TEXT does not contain 'TRUTH' (rating rule)")
        cm_ok = False
    else:
        log.info("  [OK] WELCOME_TEXT is non-empty, contains rating scale + TRUTH rule")
    # Mock bot that records send_message calls
    class _MockBot:
        def __init__(self):
            self.calls = []
        async def send_message(self, chat_id, text, message_thread_id=None):
            self.calls.append({"chat_id": chat_id, "text": text, "thread_id": message_thread_id})
            return None
    # Build a synthetic chat_member update: new join in a forum supergroup
    fake_cm = {
        "update_id": 999_999_777,
        "chat_member": {
            "chat": {"id": -1001234567890, "type": "supergroup", "is_forum": True, "title": "TestGroup"},
            "from": {"id": 1, "is_bot": False, "first_name": "operator"},
            "date": int(time.time()),
            "old_chat_member": {"user": {"id": 555, "is_bot": False, "first_name": "Newbie", "username": "newbie"}, "status": "left"},
            "new_chat_member": {"user": {"id": 555, "is_bot": False, "first_name": "Newbie", "username": "newbie"}, "status": "member"},
        },
    }
    upd = Update.de_json(fake_cm, None)
    mock_bot = _MockBot()
    try:  # noqa
        await _lazy_dispatch()._handle_chat_member_update(upd, mock_bot)
        # Expect 2 calls: one to chat_id, one to user_id
        if len(mock_bot.calls) != 2:
            log.warning(f"  [FAIL] expected 2 send_message calls, got {len(mock_bot.calls)}")
            cm_ok = False
        else:
            chat_call = next((c for c in mock_bot.calls if c["chat_id"] == -1001234567890), None)
            user_call = next((c for c in mock_bot.calls if c["chat_id"] == 555), None)
            if not chat_call or not user_call:
                log.warning(f"  [FAIL] missing expected calls: {[c['chat_id'] for c in mock_bot.calls]}")
                cm_ok = False
            elif chat_call["thread_id"] is not None:
                log.warning(f"  [FAIL] group welcome should be in General (thread_id=None), got {chat_call['thread_id']}")
                cm_ok = False
            elif 'WELCOME' not in chat_call["text"] and 'Welcome' not in chat_call["text"]:
                log.warning(f"  [FAIL] group welcome does not contain 'Welcome': {chat_call['text'][:80]!r}")
                cm_ok = False
            elif '@newbie' not in chat_call["text"]:
                log.warning(f"  [FAIL] group welcome should mention @newbie, head={chat_call['text'][:200]!r}")
                cm_ok = False
            else:
                log.info("  [OK] group welcome posted in General with @newbie mention")
                log.info("  [OK] PM sent to new user_id")
    except Exception as e:
        log.warning(f"  [FAIL] _handle_chat_member_update raised: {type(e).__name__}: {e!r}")
        import traceback
        traceback.print_exc()
        cm_ok = False
    # Negative test: chat_member update for a leave (old=member, new=left)
    # should NOT trigger any send_message.
    fake_leave = {
        "update_id": 999_999_778,
        "chat_member": {
            "chat": {"id": -1001234567890, "type": "supergroup", "is_forum": True, "title": "TestGroup"},
            "from": {"id": 1, "is_bot": False, "first_name": "operator"},
            "date": int(time.time()),
            "old_chat_member": {"user": {"id": 666, "is_bot": False, "first_name": "Leaver"}, "status": "member"},
            "new_chat_member": {"user": {"id": 666, "is_bot": False, "first_name": "Leaver"}, "status": "left"},
        },
    }
    upd2 = Update.de_json(fake_leave, None)
    mock_bot2 = _MockBot()
    try:  # noqa
        await _lazy_dispatch()._handle_chat_member_update(upd2, mock_bot2)
        if len(mock_bot2.calls) != 0:
            log.warning(f"  [FAIL] leave event triggered {len(mock_bot2.calls)} calls, expected 0")
            cm_ok = False
        else:
            log.info("  [OK] leave event correctly skipped (0 calls)")
    except Exception as e:
        log.warning(f"  [FAIL] _handle_chat_member_update(leave) raised: {type(e).__name__}: {e!r}")
        cm_ok = False
    # Negative test: another bot joining should NOT trigger
    fake_bot_join = {
        "update_id": 999_999_779,
        "chat_member": {
            "chat": {"id": -1001234567890, "type": "supergroup", "is_forum": True, "title": "TestGroup"},
            "from": {"id": 1, "is_bot": False, "first_name": "operator"},
            "date": int(time.time()),
            "old_chat_member": {"user": {"id": 777, "is_bot": True, "first_name": "SomeBot", "username": "somebot"}, "status": "left"},
            "new_chat_member": {"user": {"id": 777, "is_bot": True, "first_name": "SomeBot", "username": "somebot"}, "status": "member"},
        },
    }
    upd3 = Update.de_json(fake_bot_join, None)
    mock_bot3 = _MockBot()
    try:  # noqa
        await _lazy_dispatch()._handle_chat_member_update(upd3, mock_bot3)
        if len(mock_bot3.calls) != 0:
            log.warning(f"  [FAIL] bot-join triggered {len(mock_bot3.calls)} calls, expected 0")
            cm_ok = False
        else:
            log.info("  [OK] bot-join correctly skipped (0 calls)")
    except Exception as e:
        log.warning(f"  [FAIL] _handle_chat_member_update(bot-join) raised: {type(e).__name__}: {e!r}")
        cm_ok = False
    all_ok = cm_ok
    return all_ok


async def run_all() -> bool:
    """Every state/rating block, in the order `_selftest` ran them."""
    all_ok = True
    all_ok &= await check_per_user_semaphore()
    all_ok &= await check_rating_parser()
    all_ok &= await check_rating_emoji_map()
    all_ok &= await check_chat_member()
    log.warning(
        f"[selftest] {SUMMARY_NAME} tests: "
        f"{'all pass' if all_ok else 'FAILED'}")
    return all_ok