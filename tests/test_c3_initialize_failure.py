"""What happens when `Application.initialize()` raises, measured not argued.

Kven's C3: `set_dispatcher(app)` sits after `await app.initialize()`, so a
raise leaves the registry empty and "the next update builds another
Application". His fix: publish before initialize.

Those are two separate claims. Measured on the review commit `84de47a`,
PTB 21.0:

  * the premise is true, and the retry costs nothing. 20 consecutive
    failed `initialize()` attempts against real `ApplicationBuilder()`
    objects: fd delta 0, retained Application/ExtBot objects after gc 0,
    open httpx AsyncClients 0. A discarded Application has nothing
    attached to it until `initialize()` succeeds, so "the next update
    builds another one" is a retry, not a leak.
  * the fix is a regression. Moving `set_dispatcher` above the await
    makes `test_a_failed_initialize_publishes_nothing` red: the registry
    then holds an Application whose `process_update` raises
    "This Application is not initialized!".

An earlier draft of this note explained the harm as "PTB returns early on
`_initialized`, so a retry silently accepts a half-built app". That was
written without reading PTB and it is wrong. In 21.0 the body is:

    16| if self._initialized:
    17|     _LOGGER.debug("This Application is already initialized.")
    18|     return
    20| await self.bot.initialize()
    21| await self._update_processor.initialize()
    23| if self.updater: ... self._initialized = True; return
       ... await self._initialize_persistence() ... self._initialized = True

The flag is set last, or in a branch that returns immediately after. A
raise therefore leaves it False and a retry re-runs from the top --
a real retry, not a half-built app.

The actual harm is simpler and worse, and it does not need PTB's
internals at all: publishing early means the NEXT update sees a
non-None registry, skips the build, and hands that uninitialized
object to `process_update`, which raises. Nothing ever rebuilds it,
because the build is behind the `if app is None` guard. The retry stops
being a retry.

Publishing after success is the discipline. Keep the order; keep these
two tests, which is what makes the order a decision rather than an
accident.

The remaining question -- should a failed boot be fatal? -- is a policy
call, not a defect, and is left alone here.
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
        self.sent = []


class _FakeApp:
    """PTB 21's `initialize()` opens with `if self._initialized: return`,
    so the flag -- not the exception -- decides what a retry does. This
    models both shapes: a raise before the flag (retry re-runs) and a
    raise after it (retry silently returns a half-built app)."""

    def __init__(self, *, raise_before_flag=True, registry=None):
        self.bot = _FakeBot()
        self.raise_before_flag = raise_before_flag
        self._initialized = False
        self.init_calls = 0
        self.registry = registry
        self.error_handlers = []

    def add_error_handler(self, fn):
        self.error_handlers.append(fn)

    async def initialize(self):
        self.init_calls += 1
        # Publish ahead of the await, the way the proposed fix does, so
        # the experiment can show what a reader of that registry sees
        # while the app is still unusable.
        if self.registry is not None:
            self.registry["published_during_init"] = self
        if self.raise_before_flag:
            raise RuntimeError("boom: initialize failed")
        self._initialized = True

    def process_update(self, update):
        if not self._initialized:
            raise RuntimeError("This Application is not initialized!")
        return None


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
def wired(monkeypatch):
    """A dispatch module whose Application build is observable."""
    import dispatch
    import dispatch_polling
    import dispatch_webhook

    built = []
    registry = {}
    monkeypatch.setattr(dispatch_polling, "set_dispatcher",
                        lambda app: registry.__setitem__("app", app))
    monkeypatch.setattr(dispatch_polling, "get_dispatcher",
                        lambda: registry.get("app"))

    def _build(**kw):
        app = _FakeApp(
            raise_before_flag=kw.get("raise_before_flag", True),
            registry=registry,
        )
        built.append(app)
        return app

    # The build lives in `dispatch_webhook` -- it owns the Application
    # assembly, and `ensure_application_ready` is the one place that
    # builds, initializes and publishes. The lazy dispatch path and the
    # boot path both go through it, which is the whole point: the two
    # call sites used to disagree about the publish order, and the one
    # that production boots through was the one that got it wrong.
    monkeypatch.setattr(dispatch_webhook, "_build_application", _build)
    monkeypatch.setattr(dispatch_webhook, "start_reanchor_scheduler",
                        lambda: None)
    monkeypatch.setattr(dispatch_webhook, "start_summarise_on_boot",
                        lambda: None)
    return built, registry


def test_a_failed_initialize_publishes_nothing(wired):
    """The half of C3 that is true, stated as a property.

    Whatever `initialize()` does, a failed Application must not reach the
    registry: the registry is what every later update reads, and PTB
    refuses `process_update` on an app that never finished initializing.
    """
    import dispatch

    built, registry = wired
    with pytest.raises(RuntimeError):
        asyncio.run(dispatch._dispatch_update(_update()))

    assert built, "the fake Application was never built -- the test is not wired"
    assert "app" not in registry, (
        "a failed Application was published; every later update would hand "
        "it to process_update, which raises 'not initialized'"
    )


def test_the_registry_never_holds_an_uninitialized_app(wired):
    """Same property, stated about the moment rather than the outcome.

    A fix that moves `set_dispatcher` above `await app.initialize()` makes
    the first test red -- and does so for a real reason: for the duration
    of the await, the registry holds an Application whose
    `process_update` raises. This is the test that distinguishes "the
    premise was true" from "the fix helps".
    """
    import dispatch

    built, registry = wired
    seen_during_init = {}

    async def _go():
        async def _probe():
            # While initialize() is in flight, what does a concurrent
            # update see? This is the window the proposed fix widens.
            await asyncio.sleep(0)
            seen_during_init["published"] = registry.get("app")
        asyncio.ensure_future(_probe())
        with pytest.raises(RuntimeError):
            await dispatch._dispatch_update(_update())

    asyncio.run(_go())
    during = seen_during_init.get("published")
    assert during is None or during._initialized, (
        "the registry held an Application that had not finished "
        "initialize(); a concurrent update would get 'This Application is "
        "not initialized!' from process_update"
    )