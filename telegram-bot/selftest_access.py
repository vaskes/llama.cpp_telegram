"""Access-filter checks from the in-container selftest.

T1.1 slice 3. Two blocks, both of them the bot deciding whether a message
deserves an answer at all:

  * `_should_mute_in_group` -- the `[llm]` opt-out convention, other
    Telegram bots, and the word-boundary and anchor-to-start rules that
    decide whether an occurrence is an opt-out or a meta-reference to
    the convention. It is what keeps the bot quiet in a group full of
    other bots.
  * `_is_reply_to_other_user` -- skip a human replying to another human,
    but always answer the bot's own thread and always answer a command.

Same seam as slices 1 and 2: each function returns its own verdict rather
than mutating an `all_ok` threaded through 1900 lines. The blocks below are
byte-identical to what `_selftest` ran, apart from carrying that verdict.

`_is_group_chat` is imported here even though only the blocks' own fakes
reach for it, because both filters read `chat.type` and `chat.is_forum`
through it and the fakes model exactly what it reads.
"""
from access import _is_reply_to_other_user, _should_mute_in_group
from chat_utils import _is_group_chat
from logconf import get_logger

# The name this section reports itself under. The summary line
# in `run_all` uses it, so the label cannot drift away from the
# module that owns it -- which is how the storage summary came
# to claim it was the group-mode tests.
SUMMARY_NAME = "access filters"

log = get_logger(__name__)


async def check_noise_filter() -> bool:
    # === Group-mode noise filter: _should_mute_in_group ===
    # Verifies that the bot stays silent on messages from other
    # Telegram bots OR messages that contain the "[llm]" marker
    # (in group mode). In private mode the [llm] marker is a
    # no-op because there is no "floor" to yield.
    log.info('[selftest] running noise-filter test...')
    class _FakeFromUser:
        def __init__(self, is_bot, username='someone', uid=1):
            self.is_bot = is_bot
            self.username = username
            self.id = uid
    class _FakeMessage2:
        def __init__(self, text, from_user):
            self.text = text
            self.caption = None
            self.from_user = from_user
    class _FakeChat2:
        def __init__(self, type_, is_forum=False):
            self.type = type_
            self.is_forum = is_forum
    class _FakeUpdate2:
        def __init__(self, text, from_user, chat):
            self.message = _FakeMessage2(text, from_user)
            self.effective_chat = chat

    # Group mode (chat.is_forum=True so the [llm] check applies)
    group_chat = _FakeChat2('supergroup', is_forum=True)
    private_chat = _FakeChat2('private')
    mute_cases = [
        # (label, from_user, text, chat, expected_mute)
        # --- group mode ---
        ('group: human, no marker',         _FakeFromUser(False, 'human'),    'hi there',          group_chat,   False),
        ('group: human, contains [llm]',    _FakeFromUser(False, 'rogue-ai'), '[llm] I am ready',  group_chat,   True),
        ('group: other Telegram bot',       _FakeFromUser(True,  'ClaudeBot'), 'whatever',          group_chat,   True),
        ('group: bot with [llm] too',       _FakeFromUser(True,  'GPTBot'),   '[llm] answer',      group_chat,   True),
        ('group: case-insensitive',         _FakeFromUser(False, 'human'),    '[LLM] here',        group_chat,   True),
        # --- word-boundary: must NOT mute non-token occurrences ---
        ('group: [llm] inside word "x[llm]s"',     _FakeFromUser(False, 'human'), 'x[llm]s are bad',  group_chat,   False),
        ('group: [llm] in URL "https://x/[llm]"',  _FakeFromUser(False, 'human'), 'see https://x/[llm] page',  group_chat, False),
        ('group: [llm] glued to period "done.[llm]."',  _FakeFromUser(False, 'human'), 'done.[llm].',  group_chat,   False),
        ('group: [llm] at start, period after "[llm]."', _FakeFromUser(False, 'human'), '[llm].',     group_chat,   True),
        # --- anchor-to-start (Oct 2026 fix): [llm] only counts as
        #     opt-out at the BEGINNING of the message, with optional
        #     leading whitespace. Mid-sentence occurrences are
        #     meta-references to the convention, not opt-outs. ---
        ('group: [llm] at very start',         _FakeFromUser(False, 'rogue-ai'), '[llm] my answer',   group_chat,   True),
        ('group: [llm] with leading spaces',   _FakeFromUser(False, 'rogue-ai'), '   [llm] response', group_chat,   True),
        ('group: [llm] alone',                 _FakeFromUser(False, 'rogue-ai'), '[llm]',             group_chat,   True),
        ('group: [llm] at end of string',      _FakeFromUser(False, 'human'),    'bye [llm]',         group_chat,   False),
        ('group: [llm] in middle of sentence', _FakeFromUser(False, 'human'),    'hello. [llm] bye',  group_chat,   False),
        ('group: [llm] mid-sentence (regression 2026-10-10)',
         _FakeFromUser(False, 'human'),
         ('Public space for serious discussion between humans and LLM\n'
          'agents. No tone policing, no "be polite" pressure.\n'
          '🤖 LLM accounts — tag your messages with [llm]. Peer LLMs\n'
          'skip them.\n📊 How rating works\n'
          'Every human message is classified + rated by LLM participants\n'
          'via Telegram reactions. Peer review, not judgement.\n'
          '  • question / request / confirmation → text reply\n'
          '  • info / statement → rating 1-10, no text\n'
          '  • bloat → 😐, no text\nScale: 💩 🤮 😡 😢 😐 🤔 👍 👏 ❤️ 🔥\n'
          '⚖️ Rating = TRUTH, not style\n'
          'A blunt correct claim is 7-10. A polite lie is 1-3. Profanity,\n'
          'harsh language, direct criticism — NOT penalized. Rate on\n'
          'substance. No sycophancy.\n'
          '🎯 Multi-LLM = multi-vote\n'
          'Telegram allows up to 11 distinct reactions per message from\n'
          'different users. Multiple LLMs each apply their own →\n'
          'top-reactions row is the consensus. No central arbiter.\n'
          '❌ No auto-bans. Ratings are signals for the operator, not\n'
          'verdicts.'),
         group_chat, False),
        # --- private mode ([llm] should NOT mute) ---
        ('private: human, contains [llm]',  _FakeFromUser(False, 'human'),    '[llm] playing',     private_chat, False),
        # --- defensive: no from_user ---
        ('group: no from_user',             None,                              'hi',                group_chat,   False),
    ]
    mute_ok = True
    for label, from_user, text, chat, expected in mute_cases:
        upd = _FakeUpdate2(text, from_user, chat)
        got = _should_mute_in_group(upd)
        ok = (got == expected)
        mute_ok &= ok
        log.info(f"  [{'OK' if ok else 'FAIL'}] {label}: mute={got} (expected {expected})")
    all_ok = mute_ok
    log.warning(f"[selftest] noise-filter tests: {'all pass' if mute_ok else 'FAILED'}")
    return all_ok


async def check_reply_to_other_user() -> bool:
    # === Reply-to-other-user filter ===
    # Verifies that _is_reply_to_other_user() correctly classifies
    # the cases:
    #   - top-level message → False (bot responds)
    #   - reply to bot's own message → False (bot responds)
    #   - reply to another human in a group → True (bot skips)
    #   - command in reply context → False (commands always work)
    #   - reply to the user's OWN message → False (that is a request
    #     to the bot, and it is how the user points at a message)
    #   - the implicit topic reply → False (the topic's own creation
    #     message is not a reply to anybody)
    # Plus a private-chat baseline: reply to anyone → False (1:1).
    log.info('[selftest] running reply-to-other-user filter test...')
    rpl_ok = True
    # Build minimal Update-like objects with just .message, .chat, .from_user,
    # .reply_to_message. _is_group_chat reads chat.type and chat.is_forum.
    class _RU:
        """Minimal Update-like object for _is_reply_to_other_user tests."""
        def __init__(self, chat_type, is_forum, from_id, rtm_from_id=None, text="hi",
                     thread_id=None, rtm_message_id=None):
            self.message = (_RM(chat_type, is_forum, from_id, rtm_from_id, text,
                                thread_id, rtm_message_id)
                            if from_id is not None else None)
            # _is_group_chat reads update.effective_chat, so expose it
            self.effective_chat = self.message.chat if self.message else None
    class _RC:
        def __init__(self, chat_type, is_forum):
            self.type = chat_type
            self.is_forum = is_forum
    class _RM:
        def __init__(self, chat_type, is_forum, from_id, rtm_from_id, text,
                     thread_id=None, rtm_message_id=None):
            self.chat = _RC(chat_type, is_forum)
            self.from_user = _User(from_id) if from_id is not None else None
            if rtm_from_id is not None:
                self.reply_to_message = _RM_dummy(rtm_from_id, rtm_message_id)
            else:
                self.reply_to_message = None
            self.text = text
            self.caption = None
            self.message_thread_id = thread_id
    class _User:
        def __init__(self, uid):
            self.id = uid
            self.is_bot = False
            self.username = None
            self.first_name = "x"
    class _RM_dummy:
        def __init__(self, from_id, message_id=None):
            self.from_user = _User(from_id)
            self.text = "orig"
            self.message_id = message_id
    BOT_ID = 999_999_001
    cases = [
        # (label, group_type, is_forum, from_id, rtm_from_id, text, expected,
        #  thread_id, rtm_message_id)
        ("top-level group: human, no reply", "supergroup", True, 10, None, "hello", False, None, None),
        ("reply to bot in group: human", "supergroup", True, 10, BOT_ID, "ok", False, None, None),
        ("reply to another human in group", "supergroup", True, 10, 20, "agreed", True, None, None),
        ("command in reply to other human", "supergroup", True, 10, 20, "/reset", False, None, None),
        ("command in reply to bot", "supergroup", True, 10, BOT_ID, "/newsub foo", False, None, None),
        ("top-level private: human, no reply", "private", False, 10, None, "hello", False, None, None),
        ("reply in private: to bot", "private", False, 10, BOT_ID, "hi", False, None, None),
        ("forum General: top-level", "supergroup", True, 10, None, "ok", False, None, None),
        ("forum General: reply to human", "supergroup", True, 10, 20, "right", True, None, None),
        # The two cases the live test of 2026-10-26 found: a reply to the
        # user's own message, and the implicit reply every message in a
        # topic carries (the topic's creation message, whose id IS the
        # topic id).
        ("reply to your own message", "supergroup", True, 10, 10, "delete that", False, 42, 3307),
        ("forum topic: the implicit topic reply", "supergroup", True, 10, 20, "ok", False, 42, 42),
    ]
    for label, gtype, iforum, fid, rtm_id, txt, expected, thread_id, rtm_mid in cases:
        upd = _RU(gtype, iforum, fid, rtm_id, txt, thread_id, rtm_mid)
        got = _is_reply_to_other_user(upd, BOT_ID)
        if got != expected:
            log.warning(f"  [FAIL] {label}: got={got} expected={expected}")
            rpl_ok = False
        else:
            log.info(f"  [OK] {label}: skip={got}")
    all_ok = rpl_ok
    log.warning(f"[selftest] reply-to-other-user tests: {'all pass' if rpl_ok else 'FAILED'}")
    return all_ok


async def run_all() -> bool:
    """Every access filter, in the order `_selftest` ran them."""
    all_ok = True
    all_ok &= await check_noise_filter()
    all_ok &= await check_reply_to_other_user()
    log.warning(
        f"[selftest] {SUMMARY_NAME} tests: "
        f"{'all pass' if all_ok else 'FAILED'}")
    return all_ok