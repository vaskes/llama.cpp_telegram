"""The seam, not the unit: does dispatch still ABSORB instead of ANSWER?

The T65 suite reads `dispatch_group._handle_reply_to_other_user` and
asserts what is inside it -- persist before the LLM call, no re-raise,
`silent=True`. Every one of those would stay green if `_dispatch_update`
stopped calling the function altogether, because a body nobody runs is
a body with no bugs.

That is the same hole the ToolLoop split had: the unit's own suite
drove the unit directly, so nothing could see the join. So this file
drives `_dispatch_update` with a real group reply and watches the two
things that must differ.

The property is a difference, which is why it needs both directions:
a human-to-human reply is absorbed (persisted, reacted to, never
answered), and a top-level message is answered normally. Assert only the
first and a branch that absorbed *everything* would pass.
"""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

BOT = Path(__file__).resolve().parent.parent / "telegram-bot"
if str(BOT) not in sys.path:
    sys.path.insert(0, str(BOT))

BOT_ID = 777


def _user(uid, name, username=None):
    return SimpleNamespace(id=uid, is_bot=False, first_name=name,
                           username=username)


def _group_reply(*, reply_to_bot=False):
    """A message in a group that replies to another human."""
    if reply_to_bot:
        parent = SimpleNamespace(from_user=_user(BOT_ID, "bot", "thebot"))
    else:
        parent = SimpleNamespace(from_user=_user(999, "alice", "alice"))
    return SimpleNamespace(
        update_id=4242,
        message=SimpleNamespace(
            message_id=7,
            text="hey alice, look at this",
            caption=None,
            from_user=_user(42, "bob", "bob"),
            message_thread_id=None,
            reply_to_message=parent,
        ),
        effective_chat=SimpleNamespace(id=-100123, type="supergroup"),
    )


def _top_level():
    return SimpleNamespace(
        update_id=4243,
        message=SimpleNamespace(
            message_id=8,
            text="@thebot what is up",
            caption=None,
            from_user=_user(42, "bob", "bob"),
            message_thread_id=None,
            reply_to_message=None,
        ),
        effective_chat=SimpleNamespace(id=-100123, type="supergroup"),
    )


class _Harness:
    """Drives `_dispatch_update` far enough to see which branch ran."""

    def __init__(self):
        self.absorbed = []
        self.answered = []

    async def run(self, upd, monkeypatch):
        import dispatch
        import dispatch_polling

        registry = {}
        harness = self

        async def _process(u):
            harness.answered.append(u)

        def _build(**kw):
            # NOT async: `_dispatch_update` calls this synchronously.
            # An `async def` here returns a coroutine, and the failure
            # surfaces as "coroutine has no attribute add_error_handler",
            # which reads like a defect in dispatch.py rather than in
            # the harness.
            return app

        async def _initialize():
            return None

        async def _absorb(u, bot):
            self.absorbed.append(u)

        app = SimpleNamespace(
            bot=SimpleNamespace(id=BOT_ID),
            process_update=_process,
            initialize=_initialize,
            add_error_handler=lambda fn: None,
        )
        monkeypatch.setattr(dispatch, "_build_application", _build)
        monkeypatch.setattr(dispatch, "_handle_reply_to_other_user", _absorb)
        monkeypatch.setattr(dispatch_polling, "set_dispatcher",
                            lambda a: registry.__setitem__("app", a))
        monkeypatch.setattr(dispatch_polling, "get_dispatcher",
                            lambda: registry.get("app"))
        # de_json has to return the update under test. A stub returning a
        # bare namespace with `message=None` makes every case look like a
        # non-message, and the absorb branch is then unreachable for a
        # reason that has nothing to do with dispatch.py.
        monkeypatch.setattr(dispatch.Update, "de_json",
                            lambda d, bot: upd, raising=False)
        await dispatch._dispatch_update({"update_id": upd.update_id})
        return self


@pytest.fixture(autouse=True)
def _no_boot_jobs(monkeypatch):
    """`_dispatch_update` starts the reanchor scheduler and the boot
    summariser on first build. Neither belongs in a unit test, and the
    summariser walks every thread on a single-GPU box."""
    import dispatch
    monkeypatch.setattr(dispatch, "_start_reanchor_scheduler", lambda: None)
    monkeypatch.setattr(dispatch, "_start_summarise_on_boot", lambda: None)

    async def _no_rewrite(upd_dict):
        return None

    # async, not a bare lambda: `_dispatch_update` awaits the result, and
    # a sync stub fails with "object NoneType can't be used in 'await'
    # expression" -- which points at dispatch.py, not at the stub.
    monkeypatch.setattr(dispatch, "_apply_edited_message", _no_rewrite)



def test_a_reply_to_another_human_is_absorbed_not_answered(monkeypatch):
    h = _Harness()
    asyncio.run(h.run(_group_reply(), monkeypatch))
    assert len(h.absorbed) == 1, (
        f"the human-to-human reply was not absorbed (absorbed={h.absorbed});"
        " this is the branch that keeps a group thread out of the chat")
    assert h.answered == [], (
        f"the bot answered a message addressed to someone else: {h.answered}")


def test_a_reply_to_the_bot_is_answered_normally(monkeypatch):
    """The other direction. A branch that absorbed everything passes the
    test above; only the pair pins the predicate."""
    h = _Harness()
    asyncio.run(h.run(_group_reply(reply_to_bot=True), monkeypatch))
    assert h.absorbed == [], (
        f"a direct reply to the bot was absorbed: {h.absorbed}. That is the "
        "bot refusing to answer a user who addressed it.")
    assert len(h.answered) == 1, (
        f"a direct reply to the bot was not answered (answered={h.answered})")


def test_a_top_level_group_message_is_answered_normally(monkeypatch):
    h = _Harness()
    asyncio.run(h.run(_top_level(), monkeypatch))
    assert h.absorbed == [], f"top-level message absorbed: {h.absorbed}"
    assert len(h.answered) == 1, (
        f"top-level message not answered (answered={h.answered})")


def test_dispatch_exports_the_handler_from_its_new_owner():
    """The move, pinned. `dispatch` re-exports the name it calls, so a
    test that patches `dispatch._handle_reply_to_other_user` reaches the
    one that actually runs -- the failure mode that made two earlier
    drafts of the T65 file monkeypatch the wrong module and go green
    while asserting nothing."""
    import dispatch
    import dispatch_group

    assert dispatch._handle_reply_to_other_user is (
        dispatch_group._handle_reply_to_other_user), (
        "dispatch._handle_reply_to_other_user is no longer the one in "
        "dispatch_group; patching it will not reach the branch that runs")
    assert dispatch_group.__file__.endswith("dispatch_group.py"), (
        f"unexpected owner module: {dispatch_group.__file__}")