"""Wave 5 findings C1 and C2, pinned with runtime tests.

**C1** -- `_spawn_dispatch` gained a keyword-only `dispatch_update` when
the polling path was extracted (TECH-DEBT #10 block D) and its one call
site was never updated:

    _spawn_dispatch(upd_dict)      # TypeError, on every update

`offset` had already been advanced past the message, so Telegram counted
it delivered. The bot ran, logged at INFO, and answered nothing -- in the
default transport, the one README tells people to use.

Nothing caught it: `_bound_names` checks name binding, `compile()` checks
syntax, `test_startup_order` checks the order of call names. None of them
checks that a required keyword argument is passed. The unit tests call
`_spawn_dispatch` directly *with* the argument, so they are green on a
loop that could never work -- the seam was uncovered.

**C2** -- `_install_signal_handlers` did `global SHUTDOWN_EVENT` here,
which rebinds this module's alias of a `from config import` name.
`llm_iteration` holds its own alias, still None. Run it and the ToolLoop
abort guard sees `shutdown = False` after the event was set.
"""
import ast
import asyncio
import sys
from pathlib import Path

BOT = Path(__file__).resolve().parent.parent / "telegram-bot"
POLLING = BOT / "dispatch_polling.py"

if str(BOT) not in sys.path:
    sys.path.insert(0, str(BOT))


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200
        self.text = ""
        self.headers = {"content-type": "application/json"}
        self.content = b"[]"

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    @property
    def ok(self):
        return True


def _msg(update_id, text="hi"):
    return {"update_id": update_id,
            "message": {"chat": {"id": 1, "type": "private"},
                        "message_id": 5, "text": text}}


class _FakeClient:
    """The one `httpx.AsyncClient` the loop opens.

    When the scripted batches run out it sets the shutdown event and
    returns an empty result, so the loop exits through its own normal
    `shutdown_event.is_set()` branch rather than being killed. Nothing
    here decides what the loop does.
    """

    def __init__(self, batches, shutdown_event):
        self._batches = list(batches)
        self._shutdown = shutdown_event
        self.offsets = []
        self.allowed_updates = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, params=None):
        params = params or {}
        self.offsets.append(params.get("offset"))
        self.allowed_updates.append(params.get("allowed_updates"))
        if self._batches:
            return _FakeResponse({"ok": True, "result": self._batches.pop(0)})
        self._shutdown.set()
        return _FakeResponse({"ok": True, "result": []})


async def _no_op_menu():
    return None


async def _no_op_selftest():
    return None


async def _drive_loop(module, shutdown_event, client, dispatch_update):
    return await module._poll_loop(
        shutdown_event,
        register_bot_menu=_no_op_menu,
        dispatch_update=dispatch_update,
        run_selftest=_no_op_selftest,
    )


def _run_loop(batches, dispatch_update, monkeypatch, spawn=None):
    """Run `_poll_loop` to completion against a scripted client.

    Returns (client, seen). `dispatch_update` receives each update that
    the loop actually handed off.
    """
    dispatch, polling, _ = load_modules()
    shutdown_event = asyncio.Event()
    client = _FakeClient(batches, shutdown_event)

    def _client_factory(*a, **k):
        return client

    monkeypatch.setattr(polling.httpx, "AsyncClient", _client_factory)
    monkeypatch.setattr(dispatch, "_start_reanchor_scheduler", lambda: None)
    monkeypatch.setattr(dispatch, "_start_summarise_on_boot", lambda: None)
    if spawn is not None:
        monkeypatch.setattr(polling, "_spawn_dispatch", spawn)

    seen = []

    async def collect(upd):
        seen.append(upd)

    target = dispatch_update if dispatch_update is not None else collect

    asyncio.new_event_loop().run_until_complete(
        asyncio.wait_for(
            _drive_loop(polling, shutdown_event, client, target), 30))
    return client, seen, collect


def load_modules():
    import dispatch
    import dispatch_polling
    import llm_iteration
    return dispatch, dispatch_polling, llm_iteration


def test_every_update_reaches_the_dispatch_call(monkeypatch):
    """The seam: poll loop -> spawn -> routing.

    On 2cebf49 the loop raises `TypeError: _spawn_dispatch() missing 1
    required keyword-only argument: 'dispatch_update'`, catches it,
    logs at INFO and continues -- so `seen` comes out empty.
    """
    _client, seen, _ = _run_loop([[_msg(101)]], None, monkeypatch)
    assert [u["update_id"] for u in seen] == [101], (
        "the poll loop did not deliver update 101. It either did not "
        "hand the update off at all, or the hand-off raised and was "
        "swallowed. If the log says 'task-create failed', "
        "`_spawn_dispatch` is missing its `dispatch_update=` argument.")


def test_offset_advances_only_after_a_successful_hand_off(monkeypatch):
    """A failed hand-off must leave the update re-deliverable.

    Advancing `offset` first means Telegram drops the message for good:
    not queued, not retried, not reported. That is worse than answering
    twice -- the user wrote into nothing and the bot has no memory of
    it.
    """
    calls = []

    def exploding_spawn(upd_dict, **kwargs):
        calls.append(upd_dict["update_id"])
        raise TypeError("simulated hand-off failure")

    client, _seen, _ = _run_loop([[_msg(201)]], None, monkeypatch,
                                 spawn=exploding_spawn)

    assert calls == [201], "the hand-off was never attempted"
    assert client.offsets[0] == 0, (
        f"first request used offset={client.offsets[0]}, not 0")
    later = [o for o in client.offsets if o not in (0, None)]
    assert not later, (
        f"offset advanced to {later} even though dispatch failed; Telegram "
        "will never resend update 201, so the message is lost silently")


def test_the_polling_loop_subscribes_to_the_types_it_handles(monkeypatch):
    """Guard on the `allowed_updates` the loop actually sends."""
    client, seen, _ = _run_loop([[_msg(301)]], None, monkeypatch)
    assert seen, "precondition: an update was delivered"
    subscription = client.allowed_updates[0]
    for needed in ("message", "edited_message", "callback_query",
                   "chat_member"):
        assert needed in subscription, (
            f"{needed} is missing from allowed_updates: {subscription}")


def test_every_call_of_spawn_passes_the_dispatch_callable():
    """Structural half, so a future edit cannot reintroduce C1.

    A keyword-only parameter with no default is a required contract.
    Every call site of such a callee in this module must pass it. This
    is the check the unit tests could not give: they call the function
    directly, so they are green whether or not the loop can call it.
    """
    tree = ast.parse(POLLING.read_text(encoding="utf-8"))

    required = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # keyword-only defaults live in `kw_defaults`, aligned
            # with `kwonlyargs` -- `ast.arg` itself has no `.default`.
            kwonly = [a.arg for a, d
                      in zip(node.args.kwonlyargs, node.args.kw_defaults)
                      if d is None]
            if kwonly:
                required[node.name] = kwonly

    assert "_spawn_dispatch" in required, (
        "_spawn_dispatch lost its required dispatch_update parameter; if "
        "that was deliberate, this test is the thing to update")

    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "id", None)
        if name not in required:
            continue
        passed = {kw.arg for kw in node.keywords if kw.arg}
        for need in required[name]:
            if need not in passed:
                offenders.append(f"{name}() at line {node.lineno} omits "
                                 f"{need}=")
    assert not offenders, (
        f"call sites omit a required keyword-only argument: {offenders}. "
        "Each is a runtime TypeError on the update path, swallowed by the "
        "loop's own except.")


def test_the_shutdown_event_is_one_object_every_reader_can_see():
    """C2. A rebind of a `from X import Y` alias writes one module.

    `_install_signal_handlers` did `global SHUTDOWN_EVENT` in
    `dispatch_polling`, rebinding that module's alias of a name imported
    from `config`. `llm_iteration` holds its own alias, still None, and
    that is the one the ToolLoop abort guard reads -- so SIGTERM never
    reached the in-flight inference, which is the P1 this was supposed
    to fix.
    """
    import config
    import dispatch_polling
    import llm_iteration

    import config as cfg

    async def go():
        ev = cfg.SHUTDOWN_EVENT
        ev.clear()
        await dispatch_polling._install_signal_handlers(ev)
        return ev

    ev = asyncio.new_event_loop().run_until_complete(go())
    try:
        assert ev.is_set(), "precondition: the signal handler set the event"
        readers = {
            "config": getattr(config, "SHUTDOWN_EVENT", "<missing>"),
            "llm_iteration": getattr(llm_iteration, "SHUTDOWN_EVENT",
                                     "<missing>"),
            "dispatch_polling": getattr(dispatch_polling, "SHUTDOWN_EVENT",
                                        "<missing>"),
        }
        stale = [n for n, v in readers.items() if v is not ev]
        assert not stale, (
            f"{stale} do not see the shutdown event that was set "
            f"(config={readers['config']!r}, "
            f"llm_iteration={readers['llm_iteration']!r}). Rebinding a "
            "`from X import Y` alias writes one module's binding; the "
            "abort guard in llm_iteration reads its own, which is why the "
            "guard never fires.")
    finally:
        ev.clear()

def test_concurrent_updates_build_exactly_one_application(monkeypatch):
    """H1: the lazy init was a check-then-act across an `await`.

    On 2cebf49 ten gathered updates built ten Applications and ran the
    boot jobs twenty times. `_summarise_on_boot` walks every thread on a
    single-GPU box, so ten of those is a self-inflicted OOM, not just a
    leaked httpx pool.

    The fake Application yields inside `initialize()` on purpose: that
    suspension is the window the race needs, and a version that never
    yields cannot show the bug at all.
    """
    import dispatch
    import dispatch_polling

    built = []
    schedulers = []

    class _FakeApp:
        def __init__(self):
            built.append(self)
            self.bot = type("_B", (), {"id": 1, "token": "x"})()

        def add_error_handler(self, handler):
            pass

        async def initialize(self):
            await asyncio.sleep(0.01)

        async def process_update(self, update):
            pass

    async def go():
        dispatch_polling.set_dispatcher(None)
        # `return_exceptions=True`: this test is about how many
        # Applications get built, not about what happens to the fake one
        # afterwards. A raise past the registry read must not hide the
        # count.
        await asyncio.gather(*[
            dispatch._dispatch_update({"update_id": i}) for i in range(10)],
            return_exceptions=True)

    monkeypatch.setattr(dispatch, "_build_application", _FakeApp)
    monkeypatch.setattr(dispatch, "_start_reanchor_scheduler",
                        lambda: schedulers.append("reanchor"))
    monkeypatch.setattr(dispatch, "_start_summarise_on_boot",
                        lambda: schedulers.append("summarise"))

    asyncio.new_event_loop().run_until_complete(go())

    try:
        assert len(built) == 1, (
            f"{len(built)} Applications were built for 10 concurrent "
            "updates; the loser instances are never shut down and their "
            "boot jobs still run")
        assert len(schedulers) == 2, (
            f"boot jobs ran {len(schedulers)} times: {schedulers}. "
            "Summarisation walks every thread on one GPU, and the comment "
            "on that function says sequential on purpose.")
    finally:
        dispatch_polling.set_dispatcher(None)
