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


def test_every_call_of_a_keyword_only_callee_passes_its_required_arguments():
    """Structural half, so a future edit cannot reintroduce C1.

    A keyword-only parameter with no default is a required contract. Wave
    5 found `_spawn_dispatch(upd_dict)` missing its `dispatch_update`, and
    before that, `register_bot_menu=None` where the callable was meant to
    go -- which survives the whole suite because `main()` is not called
    end-to-end and `test_startup_order` checks the order of names, not
    the arguments.

    Scoped to `dispatch_polling.py` first, and that scope was itself the
    bug: the second mutation lives in `dispatch.py`. The rule is
    package-wide, so this walks every module. Signature information is
    built from the whole package's AST, because a callee's contract is
    not knowable from the caller's file alone.
    """
    signatures = {}
    for path in sorted(BOT.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            required = [a.arg for a, d
                        in zip(node.args.kwonlyargs, node.args.kw_defaults)
                        if d is None]
            if required:
                signatures.setdefault(node.name, set()).update(required)

    assert "_spawn_dispatch" in signatures, (
        "_spawn_dispatch lost its required dispatch_update parameter; if "
        "that was deliberate, this test is the thing to update")

    offenders = []
    for path in sorted(BOT.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None)
            if name not in signatures:
                continue
            if any(kw.arg is None for kw in node.keywords):
                # `**kwargs` can carry the required name; a static check
                # cannot prove otherwise, and the two compat shims in
                # this package are exactly that shape.
                continue
            passed = {kw.arg for kw in node.keywords if kw.arg}
            missing = signatures[name] - passed
            if missing:
                offenders.append(
                    f"{path.name}:{node.lineno} {name}() omits "
                    f"{sorted(missing)}")

    assert not offenders, (
        f"call sites omit a required keyword-only argument: {offenders}. "
        "Each is a runtime TypeError at the point of use, swallowed by "
        "the caller's own except.")


def test_installing_the_handlers_does_not_set_the_event():
    """The one wave 6 was about.

    Fixing C2 by replacing `global SHUTDOWN_EVENT` with a bare
    `SHUTDOWN_EVENT.set()` at the end of `_install_signal_handlers` set
    the event the moment the handlers were installed -- before any signal
    arrived. `llm_iteration`'s abort guard falls back to exactly that
    object, so in polling mode every tool loop aborted at iteration zero
    and the bot answered `[bot: shutdown requested...]` to everything.

    The test that should have caught it asserted the opposite, and named
    the wrong state `precondition`:

        assert ev.is_set(), "precondition: the signal handler set the event"

    Installing handlers and receiving a signal are different events.
    Only the second one may set the event.
    """
    import config as cfg
    import dispatch_polling

    async def go():
        cfg.SHUTDOWN_EVENT.clear()
        before = cfg.SHUTDOWN_EVENT.is_set()
        await dispatch_polling._install_signal_handlers(cfg.SHUTDOWN_EVENT)
        return before, cfg.SHUTDOWN_EVENT.is_set()

    before, after = asyncio.new_event_loop().run_until_complete(go())
    try:
        assert not before, "precondition: the event starts clear"
        assert not after, (
            "SHUTDOWN_EVENT is set merely by installing the signal "
            "handlers, before any signal was delivered. The ToolLoop "
            "abort guard reads this object as its fallback, so the whole "
            "polling transport answers "
            "'[bot: shutdown requested, aborting tool loop]' instead of "
            "replying. Setting it belongs in `_on_signal`.")
    finally:
        cfg.SHUTDOWN_EVENT.clear()


def test_a_real_signal_sets_the_event_every_reader_can_see():
    """C2, in the direction that was missing: a delivered signal.

    `_install_signal_handlers` used to do `global SHUTDOWN_EVENT`, which
    rebound `dispatch_polling`'s alias of a name imported from `config`
    and left every other holder on the stale None. `llm_iteration` holds
    its own alias and that is the one the abort guard reads.

    This sends SIGTERM to this process and waits for the loop to deliver
    it. The previous version of this test never sent a signal at all: it
    read the event *after* installing the handlers, which is also set on
    the broken tree, so it could not tell the two bugs apart.
    """
    import os
    import signal

    import config as cfg
    import dispatch_polling
    import llm_iteration

    async def go():
        cfg.SHUTDOWN_EVENT.clear()
        await dispatch_polling._install_signal_handlers(cfg.SHUTDOWN_EVENT)
        os.kill(os.getpid(), signal.SIGTERM)
        for _ in range(100):
            await asyncio.sleep(0.005)
            if cfg.SHUTDOWN_EVENT.is_set():
                break
        return cfg.SHUTDOWN_EVENT

    ev = asyncio.new_event_loop().run_until_complete(go())
    try:
        assert ev.is_set(), (
            "SIGTERM was delivered to a handler that did not set the "
            "event. `docker stop` then leaves the in-flight inference to "
            "be SIGKILLed ten seconds later.")
        readers = {
            "config": getattr(cfg, "SHUTDOWN_EVENT", "<missing>"),
            "llm_iteration": getattr(llm_iteration, "SHUTDOWN_EVENT",
                                     "<missing>"),
        }
        stale = [n for n, v in readers.items() if v is not ev]
        assert not stale, (
            f"{stale} do not see the event that SIGTERM set "
            f"(config={readers['config']!r}, "
            f"llm_iteration={readers['llm_iteration']!r}). Rebinding a "
            "`from X import Y` alias writes one module's binding; the "
            "abort guard in llm_iteration reads its own.")
    finally:
        cfg.SHUTDOWN_EVENT.clear()


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


def test_startup_wiring_passes_the_callables_and_not_placeholders():
    """The coverage hole wave 4 reported, closed.

    Three mutations survive the whole suite when these three arguments are
    replaced by `None`:

        register_bot_menu=None -> TypeError inside _prepare_runtime
        dispatch_update=None   -> TypeError inside _spawn_dispatch
        run_selftest=None      -> TypeError on the boot self-test

    None of them raise at the call site, because passing None IS passing
    the argument -- a checker that asks "was the keyword supplied" is
    satisfied by all three. The defect is that the *value* is a
    placeholder where a callable belongs, and the bot dies on boot
    instead of answering.

    `main()` is not called end-to-end by anything, and `test_startup_order`
    checks the order of call names rather than their arguments, which is
    why `register_bot_menu=None` has been live this long.

    Mutation: any of the three to None. All three are red.
    """
    dispatch = ast.parse((BOT / "dispatch.py").read_text(encoding="utf-8"))
    main_fn = next(n for n in ast.walk(dispatch)
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and n.name == "main")
    wiring = {}
    for node in ast.walk(main_fn):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) \
                == "_poll_loop":
            for kw in node.keywords:
                if kw.arg:
                    wiring[kw.arg] = ast.unparse(kw.value)

    assert wiring, "main no longer calls _poll_loop; this test is stale"

    expected = {"register_bot_menu": "_register_bot_menu",
                "dispatch_update": "_dispatch_update",
                "run_selftest": "_selftest"}
    wrong = {k: (wiring.get(k), v) for k, v in expected.items()
             if wiring.get(k) != v}
    assert not wrong, (
        f"main passes the wrong value for {sorted(wrong)}: "
        f"{ {k: got for k, (got, _) in wrong.items()} }, expected the "
        "callables themselves. A placeholder here raises TypeError at "
        "boot, inside a hand-rolled startup path that no test calls.")


def test_backoff_resets_after_a_recovery_even_when_the_chat_is_quiet():
    """M6: the poll backoff only ever grew.

    It was reset inside `if updates_raw`, so a quiet chat could never
    clear it. Two transient failures put it at 4; from there it doubled
    to the 30s cap and stayed -- on a long-poll with `timeout=25`, that
    is up to 55 seconds before each message is picked up, on a bot with
    nothing at all wrong with it.

    The script below fails twice (backoff 1, 2), recovers twice with an
    empty result, then fails once more. The last failure is the probe:
    after two successes the backoff must be back at 1, so that sleep is
    1.0. Without the reset it is 4.0 -- and two failures are not enough
    on their own, because the growth only becomes visible once a
    recovery has had a chance to clear it.
    """
    import dispatch_polling

    n = {"i": 0}
    sleeps = []
    # fail, fail, ok, ok, fail, then stop
    fail_on = {1, 2, 5}

    class _FlakyClient:
        def __init__(self, shutdown):
            self._shutdown = shutdown

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, params=None):
            n["i"] += 1
            if n["i"] in fail_on:
                raise RuntimeError("simulated transient failure")
            if n["i"] > 6:
                self._shutdown.set()
            return _FakeResponse({"ok": True, "result": []})

    real_sleep = asyncio.sleep

    async def counting_sleep(delay, *a, **k):
        if delay >= 0.5:          # the loop's backoff, not a poll delay
            sleeps.append(delay)
            await real_sleep(0)
            return
        return await real_sleep(delay, *a, **k)

    async def drive():
        shutdown = asyncio.Event()
        client = _FlakyClient(shutdown)
        original = dispatch_polling.httpx.AsyncClient
        dispatch_polling.httpx.AsyncClient = lambda *a, **k: client
        asyncio.sleep = counting_sleep
        try:
            await asyncio.wait_for(dispatch_polling._poll_loop(
                shutdown,
                register_bot_menu=_no_op_menu,
                dispatch_update=_no_op_menu,
                run_selftest=_no_op_selftest,
            ), 20)
        finally:
            dispatch_polling.httpx.AsyncClient = original
            asyncio.sleep = real_sleep

    try:
        asyncio.new_event_loop().run_until_complete(drive())
    finally:
        asyncio.sleep = real_sleep

    assert len(sleeps) >= 3, (
        f"precondition: expected at least three backoff sleeps, got "
        f"{sleeps}. The script has to reach the post-recovery failure "
        "for this test to mean anything.")
    assert sleeps[-1] == 1.0, (
        f"backoff sleeps were {sleeps}. The last one is the probe: two "
        "failures (1, 2), two successful-but-empty polls, then a failure "
        "again. It must sleep 1.0, because the reset happens on any "
        "successful getUpdates -- not only on one that returns updates. "
        "Otherwise a quiet chat pins the backoff at the 30s cap for the "
        "life of the process.")
