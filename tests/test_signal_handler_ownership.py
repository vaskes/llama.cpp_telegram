"""Which transport installs signal handlers, and how many times.

Kven's H4: `webhook.run()` and `dispatch_polling._install_signal_handlers`
both register SIGINT/SIGTERM, "so in hybrid mode (webhook + polling
fallback) this is a double registration". He proposes either skipping
`_install_signal_handlers` in webhook mode, or switching to
`signal.signal()` because it "replaces, rather than adds".

Checked against the tree at `84de47a`, and there is no hybrid mode.
`_poll_loop` has exactly one caller -- `dispatch.py:302`, the polling
branch of `main` -- and the webhook branch returns at line 271, thirty
lines earlier. `_install_signal_handlers` is therefore unreachable when
`WEBHOOK_MODE` is set, and the two handlers never coexist.

The proposed remedy is built on the same false premise:
`loop.add_signal_handler` replaces, it does not stack. Registering twice
would leave one live handler, not two, so even the double registration
he describes would not be a defect.

None of which means there is nothing here. `main()` still defines an
`_on_signal` closure that nothing calls -- the real handler moved into
`dispatch_polling._install_signal_handlers` when that function was
extracted, and the copy stayed behind. That is the same shape as every
other stale claim in this repo: a comment, or a definition, describing a
place the code used to be.

So this file pins the property that actually holds -- one owner per
mode, and the modes are exclusive -- by running `main()` for real with
both transports faked, rather than by reading the `if`.
"""
import asyncio
import sys
from pathlib import Path

import pytest

BOT = Path(__file__).resolve().parent.parent / "telegram-bot"
if str(BOT) not in sys.path:
    sys.path.insert(0, str(BOT))


def test_webhook_mode_never_reaches_the_polling_signal_handlers(monkeypatch):
    """Run `main()` in webhook mode and watch who installs what.

    This is the direction that can fail. An AST check that reads `if
    _config.WEBHOOK_MODE:` cannot tell an early `return` from a missing
    one; running `main` can.
    """
    import config
    import dispatch
    import dispatch_polling
    import webhook

    installed = []
    polled = []

    async def _fake_webhook_run(**kwargs):
        # The registrar arrives as a keyword argument (the webhook
        # server registers the command menu at startup); the fake only
        # records that the webhook path was taken.
        polled.append("webhook.run")

    async def _fake_install(shutdown_event):
        installed.append("dispatch_polling._install_signal_handlers")

    async def _fake_poll_loop(*args, **kwargs):
        polled.append("dispatch_polling._poll_loop")

    monkeypatch.setattr(config, "WEBHOOK_MODE", True)
    monkeypatch.setattr(config, "WEBHOOK_URL", "")
    monkeypatch.setattr(config, "WEBHOOK_SECRET_TOKEN", "")
    monkeypatch.setattr(webhook, "run", _fake_webhook_run)
    monkeypatch.setattr(dispatch_polling, "_install_signal_handlers",
                        _fake_install)
    monkeypatch.setattr(dispatch_polling, "_poll_loop", _fake_poll_loop)

    dispatch.main()

    assert polled == ["webhook.run"], (
        f"webhook mode ran {polled}; expected exactly webhook.run and "
        "nothing else")
    assert installed == [], (
        f"webhook mode installed polling's signal handlers: {installed}. "
        "The two modes are exclusive -- the polling loop's handlers write "
        "the same shutdown event webhook.run() waits on, so this would "
        "mean two owners for one signal.")


def test_polling_mode_reaches_the_poll_loop_without_touching_signals(
        monkeypatch):
    """The other half, and the half where `main` was quietly wrong.

    `dispatch.py` does `from dispatch_polling import _poll_loop`, so the
    name that runs is `dispatch._poll_loop`. Patching
    `dispatch_polling._poll_loop` patches something nobody calls -- and
    the real loop then long-polls api.telegram.org for the rest of the
    run. That is not a hypothetical: an earlier draft of this test did
    exactly that and hung on the network.

    `main` installs no handler of its own; `_install_signal_handlers`,
    which lives in the polling module and is called from inside
    `_poll_loop`, owns that. So the property is: main reaches the loop,
    passes real callables, and never registers a signal itself.
    """
    import asyncio

    import config
    import dispatch
    import dispatch_polling

    ran = []
    registered = []

    async def _fake_poll_loop(shutdown_event, *, register_bot_menu,
                              dispatch_update, run_selftest):
        ran.append({"register_bot_menu": register_bot_menu,
                    "dispatch_update": dispatch_update,
                    "run_selftest": run_selftest})

    def _record_signal(sig, cb, *a):
        registered.append(sig)
        return None

    monkeypatch.setattr(config, "WEBHOOK_MODE", False)
    monkeypatch.setattr(dispatch, "_poll_loop", _fake_poll_loop)
    monkeypatch.setattr(asyncio.AbstractEventLoop, "add_signal_handler",
                        _record_signal)

    dispatch.main()

    assert len(ran) == 1, f"_poll_loop ran {len(ran)} times: {ran}"
    assert registered == [], (
        f"main() registered {registered} itself. The signal handler belongs "
        "to dispatch_polling._install_signal_handlers; a second "
        "registration here would be a second owner of shutdown, and the "
        "one in main has had no callers since that function was extracted.")
    assert all(ran[0].values()), (
        f"main passed placeholders to _poll_loop: {ran[0]}")


def test_main_does_not_define_a_second_signal_handler():
    """The dead closure, pinned by AST so it cannot come back.

    `main` must not define a nested `_on_signal`. If someone re-adds
    one, this fails with the reason rather than leaving two definitions
    of the same policy, one of them inert.
    """
    import ast

    src = (BOT / "dispatch.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    main_fn = next(n for n in tree.body
                   if isinstance(n, ast.FunctionDef) and n.name == "main")
    nested = [n.name for n in ast.walk(main_fn)
              if isinstance(n, ast.FunctionDef) and n is not main_fn]
    assert "_on_signal" not in nested, (
        f"main() defines {nested} again. The signal handler belongs to "
        "dispatch_polling._install_signal_handlers; a second definition "
        "here has no callers and only misleads the next reader about who "
        "owns shutdown.")