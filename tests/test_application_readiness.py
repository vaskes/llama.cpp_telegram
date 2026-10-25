"""A bot that cannot answer must not accept traffic.

The 2026-10-25 production incident, in the shape it had:

  * the host rebooted, and the bot container came up before the host's
    DNS did;
  * the bot's `Application.initialize()` failed on `getMe`;
  * `dispatch_webhook._register_bot_menu` had already published the
    half-built Application to the registry, and swallowed the failure
    as "setMyCommands failed (non-fatal)";
  * every update after that read the zombie from the registry, skipped
    the build, and died on `app.bot.id` with "ExtBot is not properly
    initialized";
  * the webhook answered Telegram with 200 all the same, so Telegram
    did not retry -- the messages were dropped, not delayed;
  * and nothing brought the bot back. It stayed deaf until a human
    restarted the container.

Each of those steps is a separate wrong, and the tripwires below pin
each one, because a test that only says "the registry is empty after a
failed initialize" would have stayed green through the whole incident:
the lazy dispatch path did publish nothing, and the tripwire
`tests/test_c3_initialize_failure.py` was watching that path. The path
production boots through was the one that got it wrong.

The properties:

  1. a failed initialization publishes nothing -- in the boot path too;
  2. the bot retries a transient failure instead of dying on the
     first one, because a host that has just rebooted usually has its
     DNS a second or two later;
  3. a bot that cannot reach Telegram does not come up as a server:
     the readiness failure is fatal, and the container's restart
     policy brings it back -- visibly, instead of as a green zombie;
  4. an Application in the registry that has not finished initializing
     is repaired, not trusted;
  5. and the criterion is the real one: an Application that has not
     finished initializing cannot read `app.bot.id`, which is what the
     bot reads on every single update.
"""
import asyncio
import sys
from pathlib import Path

import pytest

BOT = Path(__file__).resolve().parent.parent / "telegram-bot"
if str(BOT) not in sys.path:
    sys.path.insert(0, str(BOT))


class _FakeBot:
    def __init__(self):
        self.commands = []

    async def set_my_commands(self, commands, scope=None,
                              language_code=None):
        self.commands = commands


class _FakeApp:
    """An Application that can fail its initialization N times first.

    PTB sets `_initialized` last, so a raise leaves it False and a
    retry re-runs from the top -- that is the property the retry in
    `ensure_application_ready` relies on, and this fake models it.
    """

    def __init__(self, *, failures=0, error=None):
        self.bot = _FakeBot()
        self.failures = failures
        self.error = error or RuntimeError("getMe failed: name resolution")
        self.init_calls = 0
        self._initialized = False
        self.processed = []

    async def initialize(self):
        self.init_calls += 1
        if self.init_calls <= self.failures:
            raise self.error
        self._initialized = True

    async def process_update(self, update):
        self.processed.append(update)


def _update(n=1):
    return {
        "update_id": n,
        "message": {
            "message_id": 7, "date": 1,
            "chat": {"id": 1000, "type": "private"},
            "from": {"id": 42, "is_bot": False, "first_name": "t"},
            "text": "hi",
        },
    }


@pytest.fixture
def registry(monkeypatch):
    """The real registry, so a test that leaves something in it is
    visible -- and so a leak cannot make the next test green."""
    import dispatch_polling

    state = {"app": None}
    monkeypatch.setattr(dispatch_polling, "set_dispatcher",
                        lambda app: state.__setitem__("app", app))
    monkeypatch.setattr(dispatch_polling, "get_dispatcher",
                        lambda: state.get("app"))
    return state


@pytest.fixture
def no_boot_jobs(monkeypatch):
    """The boot jobs walk every thread on a single-GPU box; they do not
    belong in a unit test."""
    import dispatch_webhook

    monkeypatch.setattr(dispatch_webhook, "start_reanchor_scheduler",
                        lambda: None)
    monkeypatch.setattr(dispatch_webhook, "start_summarise_on_boot",
                        lambda: None)


@pytest.mark.asyncio
async def test_a_failed_boot_publishes_nothing(monkeypatch, registry,
                                                no_boot_jobs):
    """The boot path, the one production uses, keeps the invariant.

    `tests/test_c3_initialize_failure.py` states this property for the
    lazy dispatch path. The boot path had the opposite order -- publish,
    then initialize -- and that is the one that killed the bot.
    """
    import dispatch_webhook

    app = _FakeApp(failures=99)
    monkeypatch.setattr(dispatch_webhook, "_build_application",
                        lambda: app)

    with pytest.raises(dispatch_webhook.ApplicationNotReady):
        await dispatch_webhook._register_bot_menu()

    assert registry.get("app") is None, (
        "a failed initialization was published to the registry; every "
        "later update reads it, skips the build, and dies on app.bot.id "
        "-- and the webhook answers Telegram with 200, so the message is "
        "lost rather than retried")


@pytest.mark.asyncio
async def test_the_boot_retries_a_transient_failure(monkeypatch, registry,
                                                    no_boot_jobs):
    """A host that has just rebooted usually has its DNS a second or
    two later. The bot must survive that window by itself.

    The number of attempts is the *default* on purpose: the default is
    what production uses, and a test that passes the number in itself
    would stay green with the default broken.
    """
    import dispatch_webhook

    app = _FakeApp(failures=2)
    monkeypatch.setattr(dispatch_webhook, "_build_application",
                        lambda: app)

    ready = await dispatch_webhook.ensure_application_ready(delay=0)

    assert ready is app
    assert app.init_calls == 3, (
        "the initialization was not retried; a bot that gives up on the "
        "first DNS failure needs an operator to notice and restart it")
    assert registry.get("app") is app, (
        "the Application was never published, so no update can use it")


@pytest.mark.asyncio
async def test_the_dispatch_path_repairs_a_zombie(monkeypatch, registry,
                                                  no_boot_jobs):
    """An Application in the registry that never finished initializing
    is repaired, not trusted.

    The zombie is what the incident left behind: the registry held an
    Application whose `initialize()` had failed, and every update read
    it and died. The dispatch path has to notice and fix it.
    """
    import dispatch
    import dispatch_webhook

    zombie = _FakeApp(failures=0)
    zombie._initialized = False  # published before initialize() -- the bug
    registry["app"] = zombie

    async def _fake_process(update, bot):
        zombie.processed.append(update)

    monkeypatch.setattr(dispatch, "_handle_reply_to_other_user",
                        _fake_process)
    monkeypatch.setattr(dispatch.Update, "de_json",
                        lambda d, bot: type("U", (), {"update_id": d["update_id"],
                                                      "message": None,
                                                      "edited_message": None,
                                                      "chat_member": None,
                                                      "callback_query": None})(),
                        raising=False)

    await dispatch._dispatch_update(_update())

    assert zombie.init_calls >= 1, (
        "the dispatch path trusted an uninitialized Application; "
        "app.bot.id raises 'ExtBot is not properly initialized' on it, "
        "and every message dies")
    assert zombie._initialized is True, (
        "the repair did not finish the initialization")


@pytest.mark.asyncio
async def test_a_bot_that_cannot_answer_does_not_come_up_as_a_server(
        monkeypatch):
    """The readiness failure is fatal at boot.

    The server must not come up as a bot that answers nothing: the
    webhook would return 200 for every update, Telegram would not
    retry, and the messages would be dropped. The process has to exit
    instead, so the container's restart policy brings it back -- and
    the operator sees a flapping container rather than a green one
    that eats traffic.
    """
    import config
    import dispatch_webhook
    import webhook

    monkeypatch.setattr(config, "WEBHOOK_HOST", "127.0.0.1")
    monkeypatch.setattr(config, "WEBHOOK_PORT", 0)
    monkeypatch.setattr(config, "WEBHOOK_SECRET_TOKEN", "test-secret")

    bound = []

    class _FakeSite:
        def __init__(self, runner, host, port):
            pass

        async def start(self):
            bound.append(True)

    monkeypatch.setattr(webhook.web, "TCPSite", _FakeSite)

    async def _not_ready():
        raise dispatch_webhook.ApplicationNotReady(
            "could not initialize the Telegram application")

    # The shutdown event is set, so that a regression which lets the
    # server come up returns instead of waiting for a signal that will
    # never come. A test that hangs is a worse failure than a test that
    # fails: it takes the whole suite down with it.
    config.SHUTDOWN_EVENT.set()
    try:
        with pytest.raises(dispatch_webhook.ApplicationNotReady):
            await webhook.run(register_bot_menu=_not_ready)
    finally:
        config.SHUTDOWN_EVENT.clear()

    assert not bound, (
        "the webhook socket was bound before the bot was ready; a bot "
        "that cannot answer must not accept traffic")


@pytest.mark.asyncio
async def test_a_menu_failure_is_still_not_fatal(monkeypatch):
    """The two failures keep their weights.

    The menu not registering is cosmetic -- Telegram caches the old
    menu, and the user can type the command by hand. The bot being
    unable to reach Telegram is not. Only the second one stops the
    server.
    """
    import config
    import webhook

    monkeypatch.setattr(config, "WEBHOOK_HOST", "127.0.0.1")
    monkeypatch.setattr(config, "WEBHOOK_PORT", 0)
    monkeypatch.setattr(config, "WEBHOOK_SECRET_TOKEN", "test-secret")

    config.SHUTDOWN_EVENT.set()
    try:
        async def _menu_only_failure():
            raise RuntimeError("setMyCommands failed")

        # Must not raise: the server still comes up.
        await webhook.run(register_bot_menu=_menu_only_failure)
    finally:
        config.SHUTDOWN_EVENT.clear()


def test_the_readiness_criterion_is_the_real_one():
    """The criterion is not a guess: an Application that has not
    finished initializing cannot read `app.bot.id`.

    This is the failure the production incident produced, verbatim:
    `RuntimeError: ExtBot is not properly initialized`. The bot reads
    that id on every update, to tell whether a reply is addressed to
    somebody else -- so "not initialized" is not a formality, it is the
    answer to every message.
    """
    from telegram.ext import Application

    app = Application.builder().token("123456:TESTTOKEN").build()

    assert app._initialized is False, (
        "PTB does not start out initialized; the readiness criterion "
        "would be vacuous if it did")
    with pytest.raises(RuntimeError):
        app.bot.id
