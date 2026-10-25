"""Tripwire: webhook mode registers the command menu at startup.

The command menu is user-facing text: `setMyCommands` is what puts
"reset", "stats" and the rest into the chat input, with their
descriptions. Telegram caches it across restarts, so the menu the user
sees is whatever the last run that registered one had set.

Polling mode registers it in `dispatch_polling._prepare_runtime`.
Webhook mode -- the mode production runs in -- returned from
`dispatch.main()` before that point, so the menu was never refreshed.
The English translation of `BOT_COMMANDS` was deployed, the menu in the
chat was still the Russian one, and every test was green: the content
pin in `tests/test_prompts.py` guards the *text*, and nothing guarded
the *registration*.

These tests close that gap. Two halves, because either half alone is
half a promise:

* the server calls the registrar it is handed;
* the wiring hands it the real registrar.

The registration is non-fatal -- a bot that cannot reach Telegram at
boot must still serve `/healthz` and the traffic that does arrive --
so that is asserted too: a registrar that raises does not take the
server down.
"""

import asyncio

import pytest


def _server_settings(monkeypatch):
    """Point the webhook server at a throwaway port and let it stop."""
    import config
    monkeypatch.setattr(config, "WEBHOOK_HOST", "127.0.0.1")
    monkeypatch.setattr(config, "WEBHOOK_PORT", 0)
    monkeypatch.setattr(config, "WEBHOOK_SECRET_TOKEN", "test-secret")


@pytest.mark.asyncio
async def test_the_webhook_server_registers_the_menu(monkeypatch):
    """The server calls the registrar it was given."""
    import config
    import webhook

    _server_settings(monkeypatch)
    called = []

    async def _fake_register():
        called.append("registered")

    # The server waits for the shutdown event; set it so the run
    # returns instead of hanging the test.
    config.SHUTDOWN_EVENT.set()
    try:
        await webhook.run(register_bot_menu=_fake_register)
    finally:
        config.SHUTDOWN_EVENT.clear()

    assert called == ["registered"], (
        "webhook.run() did not register the command menu -- the menu "
        "Telegram shows is whatever the last run that registered one "
        "had set, and in webhook mode that is nothing at all")


@pytest.mark.asyncio
async def test_a_failing_registration_does_not_take_the_server_down(
        monkeypatch):
    """The registration is non-fatal: the server still comes up."""
    import config
    import webhook

    _server_settings(monkeypatch)

    async def _boom():
        raise RuntimeError("Telegram is unreachable at boot")

    config.SHUTDOWN_EVENT.set()
    try:
        # Must not raise: a bot that cannot register its menu still has
        # to serve /healthz and the updates that do arrive.
        await webhook.run(register_bot_menu=_boom)
    finally:
        config.SHUTDOWN_EVENT.clear()


def test_webhook_mode_hands_the_server_a_registrar(monkeypatch):
    """The wiring: `dispatch.main()` passes the real registrar.

    Without this half, the server would register whatever the caller
    happened to pass -- including nothing.
    """
    import config
    import dispatch
    import dispatch_webhook
    import webhook

    captured = {}

    async def _fake_webhook_run(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(config, "WEBHOOK_MODE", True)
    monkeypatch.setattr(config, "WEBHOOK_URL", "")
    monkeypatch.setattr(config, "WEBHOOK_SECRET_TOKEN", "")
    monkeypatch.setattr(webhook, "run", _fake_webhook_run)

    dispatch.main()

    registrar = captured.get("register_bot_menu")
    assert registrar is not None, (
        "webhook mode called webhook.run() without a register_bot_menu "
        f"argument -- got {sorted(captured)}")
    assert registrar is dispatch_webhook._register_bot_menu, (
        "webhook mode must register the menu with the real registrar, "
        f"not {registrar!r}")


def test_the_menu_registrar_publishes_the_command_list():
    """The registrar sends the list the content pin guards.

    `tests/test_prompts.py` pins the *text* of BOT_COMMANDS. This
    asserts the text is the one that reaches Telegram -- the pin alone
    would stay green if the registration published an empty list.
    """
    import dispatch_webhook
    from prompts import BOT_COMMANDS

    assert dispatch_webhook.BOT_COMMANDS is BOT_COMMANDS, (
        "the webhook registrar and the prompts module disagree about "
        "which command list is the real one")

    published = {}

    class _FakeBot:
        async def set_my_commands(self, commands, scope=None,
                                  language_code=None):
            published["commands"] = commands

    class _FakeDispatcher:
        def __init__(self):
            self.bot = _FakeBot()
            self.init_calls = 0

        async def initialize(self):
            # The registrar no longer builds the Application itself: it
            # asks for one that is ready, and a ready one has finished
            # initializing. So the fake has to finish initializing too.
            self.init_calls += 1
            self._initialized = True

    dispatcher = _FakeDispatcher()

    class _FakePolling:
        @staticmethod
        def get_dispatcher():
            return dispatcher

    import dispatch_polling
    old = dispatch_polling.get_dispatcher
    dispatch_polling.get_dispatcher = staticmethod(lambda: dispatcher)
    try:
        asyncio.run(dispatch_webhook._register_bot_menu())
    finally:
        dispatch_polling.get_dispatcher = old

    sent = published.get("commands")
    assert sent, "the registrar published no commands to Telegram"
    assert [c.command for c in sent] == [c for c, _ in BOT_COMMANDS], (
        "the menu published to Telegram is not the pinned BOT_COMMANDS")
    assert all(c.description for c in sent), (
        "a command with an empty description is invisible in the menu")
