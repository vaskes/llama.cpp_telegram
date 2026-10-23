"""The polling delivery path: signals, runtime prep, the loop itself.

TECH-DEBT #10 block D. The ticket asked for `dispatch.py` at 400 lines;
after block C it was 974, and `_poll_loop` at 164 was the largest thing
left in it.

What lives here and why:

- `_install_signal_handlers` / `_prepare_runtime` / `_poll_loop` -- the
  startup sequence, in the order `test_startup_order.py` pins;
- `_spawn_dispatch` -- how an update becomes a task, and the strong
  reference set that keeps it from being garbage collected;
- `_dispatcher` / `_dispatch_tasks` -- the Application registry and the
  task set, which moved with the code that writes them.

**This module imports nothing from `dispatch`, and that is deliberate.**
The first version reached back with `from dispatch import ...` inside
function bodies, the way `webhook.py` reaches for `_dispatch_update`.
It worked, and `test_import_graph.py` correctly reported that a cycle
had grown from six modules to seven -- a lazy import inside a function is
still an edge, and adding this module to the `dispatch` component is a
regression dressed as a refactor.

So the three things this path needs from `dispatch` -- `_register_bot_menu`,
`_dispatch_update` and `_selftest` -- arrive as **keyword arguments**,
passed by `main`. Nothing is resolved through `sys.modules` at call time,
the graph has no new edge, and the dependencies show up in the signature
instead of hiding in a function body.
"""

import asyncio
import signal

import httpx

from config import TELEGRAM_API
from logconf import get_logger

log = get_logger(__name__)


# The live PTB Application, or None before startup finishes.
_dispatcher = None


def get_dispatcher():
    return _dispatcher


def set_dispatcher(app) -> None:
    """Publish the assembled Application. Read by `_poll_loop`, and by the
    two modules that build it."""
    global _dispatcher
    _dispatcher = app


_dispatch_tasks: set[asyncio.Task] = set()


def _spawn_dispatch(upd_dict: dict, *, dispatch_update) -> asyncio.Task:
    """Create the dispatch task and keep a strong reference until it finishes."""
    task = asyncio.get_running_loop().create_task(dispatch_update(upd_dict))

    def _done(t: asyncio.Task) -> None:
        _dispatch_tasks.discard(t)
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            log.warning(f"[dispatch] update_id={upd_dict.get('update_id')} FAILED "
                f"unhandled: {type(exc).__name__}: {exc}")

    task.add_done_callback(_done)
    _dispatch_tasks.add(task)
    return task

async def _install_signal_handlers(shutdown_event: asyncio.Event) -> None:
    """SIGINT/SIGTERM set the shutdown event.

    `shutdown_event` is `config.SHUTDOWN_EVENT` -- `main` passes that one
    object in -- because `call_llama` reads it between iterations and gets
    no context argument from this hand-rolled loop. It arrives as a
    parameter and is not imported here, so nothing in this module can
    rebind or set the name by accident.

    The handler used to be a closure inside `main`. It belongs here: the
    signals and the loop they interrupt are the same concern, and a
    closure named `_on_signal` was one rename away from being invisible.
    """
    def _on_signal(signame: str) -> None:
        log.info(f"[main] received {signame}, initiating shutdown...")
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _on_signal, sig.name)
        except (NotImplementedError, RuntimeError):
            # Windows or a non-main thread: fall back to the default
            # behaviour. KeyboardInterrupt still aborts, just less
            # gracefully -- the in-flight httpx call dies mid-stream
            # instead of unwinding through shutdown_event.
            pass
    # No `global SHUTDOWN_EVENT`, and nothing else here either.
    #
    # It used to be `global SHUTDOWN_EVENT; SHUTDOWN_EVENT = shutdown_event`,
    # which rebound *this module's* alias and left every other reader on
    # the stale None. Fixing that by putting a bare `SHUTDOWN_EVENT.set()`
    # at the end of this function was worse: the event was then set the
    # moment the handlers were installed, before any signal arrived, and
    # `llm_iteration`'s abort guard -- whose fallback reads exactly this
    # object -- aborted every tool loop at iteration zero. The bot
    # answered `[bot: shutdown requested...]` to everything.
    #
    # `shutdown_event` IS `config.SHUTDOWN_EVENT`: `main` passes it in.
    # `_on_signal` above sets it, on a signal, which is the only thing
    # that should ever set it.
async def _prepare_runtime(*, register_bot_menu, run_selftest) -> None:
    """Once, before the first poll: register the menu, then self-test.

    Menu registration is non-fatal on purpose. If the network is briefly
    down at boot the user can still type `/command` by hand, and Telegram
    caches `setMyCommands` across restarts, so re-registering is cheap.

    The NB comment below is a correction of CALL_LLAMA.md section 4, not
    a leftover: we never poll with offset=-1. In LOCKDOWN the handler
    rejects everything anyway; outside it, dropping pending messages means
    the user sent something while the bot was down and gets no answer.
    """
    # Register the bot's command menu with Telegram. Without this,
    # the chat input / menu shows whatever was last setMyCommands'd
    # for this bot — usually nothing useful. setMyCommands is the
    # native way to make commands discoverable in the GUI: they
    # appear in the "Menu" button on the chat input bar with their
    # descriptions, and typing / shows the list.
    try:
        await register_bot_menu()
    except Exception as e:
        log.info(f"[main] setMyCommands failed (non-fatal): {e!r}")
    # NB: we intentionally do NOT call getUpdates with offset=-1 to
    # drop pending updates. Reasoning:
    #   - in LOCKDOWN, the handler rejects everything anyway, so
    #     backlog gets eaten harmlessly;
    #   - in non-LOCKDOWN, dropping pending messages is a UX bug
    #     (user sent a message while bot was down, expects to see
    #     a reply when bot comes back).
    # The CALL_LLAMA.md §4 paragraph that claimed otherwise is
    # corrected by this comment.

    # Self-test: if LLAMABOT_SELFTEST=1, push a fake Update through
    # the dispatcher so we can verify handlers actually fire even when
    # we can't easily test with a real user.
    await run_selftest()

async def _poll_loop(shutdown_event: asyncio.Event, *, register_bot_menu,
                   dispatch_update, run_selftest) -> None:
    """Start the runtime, then long-poll getUpdates until shutdown.

    This was `_run`, nested inside `main`, where it was 190 of main's 269
    lines and could not be exercised without starting a whole bot. It
    closed over exactly one thing -- the shutdown event -- so that is the
    only parameter.

    The two startup steps run here rather than in `main` so that there is
    one place where the ordering "handlers installed, then runtime
    prepared, then the first poll" is visible. `main` decides *which mode*
    runs; this decides what running it consists of.
    """
    await _install_signal_handlers(shutdown_event)
    await _prepare_runtime(register_bot_menu=register_bot_menu,
                         run_selftest=run_selftest)
    offset = 0
    backoff = 1.0
    n_polls = 0
    n_updates = 0
    # Use a single dedicated httpx client. max_keepalive_connections=0
    # means no keep-alive — every request opens a fresh connection.
    # This guarantees only one open connection at a time.
    # try/finally around the whole block ensures the PTB dispatcher
    # gets shut down on every exit path: normal `break` (graceful
    # shutdown), CancelledError (asyncio.run() cancel), or any
    # unexpected exception. Without the finally, the httpx client
    # held by app.bot leaks and prints "RuntimeWarning: unclosed
    # client" on interpreter shutdown.
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=35.0, write=10.0, pool=10.0),
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=0),
        ) as client:
            while True:
                if shutdown_event.is_set():
                    log.info(f"[poll] shutdown_event set, exiting loop (after {n_polls} cycles, {n_updates} updates)")
                    break
                try:
                    r = await client.get(
                        f"{TELEGRAM_API}/getUpdates",
                        params={
                            "offset": offset,
                            "timeout": 25,            # long-poll
                            # T1 (P0-1) defense-in-depth: cap the
                            # per-cycle update batch so a flood
                            # doesn't queue 100 dispatch tasks at
                            # once. With limit=10 and
                            # _GLOBAL_LLM_SEM_LIMIT=4, the worst
                            # case is 10 in-flight at any moment,
                            # only 4 inside the LLM call.
                            "limit": 10,
                            "allowed_updates": '["message","edited_message","chat_member","callback_query"]',
                        },
                    )
                    data = r.json()
                    if not data.get("ok"):
                        raise RuntimeError(f"getUpdates not ok: {data}")
                    updates_raw = data.get("result", [])
                    n_polls += 1
                    if updates_raw:
                        n_updates += len(updates_raw)
                        backoff = 1.0
                        log.info(f"[poll] cycle={n_polls} got {len(updates_raw)} updates (total={n_updates})")
                        for upd_dict in updates_raw:
                            # PII-safe dump. The previous version
                            # printed the full JSON including
                            # first_name, last_name, chat.title, and
                            # the message text — all of which are
                            # PII in journald. The new line logs
                            # only structural fields plus a short
                            # content hash for cross-correlating
                            # with later log lines.
                            upd_id = upd_dict.get("update_id", "?")
                            upd_msg = upd_dict.get("message") or upd_dict.get("edited_message") or {}
                            upd_chat = upd_msg.get("chat") or {}
                            upd_from = upd_msg.get("from") or {}
                            upd_text = (upd_msg.get("text") or upd_msg.get("caption") or "")[:60]
                            import hashlib
                            text_hash = hashlib.sha1(upd_text.encode("utf-8", errors="replace")).hexdigest()[:10] if upd_text else "-"
                            log.info(f"[poll] update_id={upd_id} "
                                f"chat_type={upd_chat.get('type', '?')} "
                                f"chat_id={upd_chat.get('id', '?')} "
                                f"is_forum={upd_chat.get('is_forum', '?')} "
                                f"from_id={upd_from.get('id', '?')} "
                                f"from_is_bot={upd_from.get('is_bot', '?')} "
                                f"thread_id={upd_msg.get('message_thread_id', '-')} "
                                f"text_hash={text_hash}")
                            # `offset` is advanced AFTER dispatch succeeds,
                            # not here. Advancing first means Telegram
                            # considers the update delivered even when
                            # dispatch raises, and the message is then
                            # gone -- not queued, not retried, not logged
                            # as lost. That is exactly what a
                            # keyword-argument slip in `_spawn_dispatch`
                            # did: TypeError on every update, and the
                            # default transport silently answering
                            # nothing at all.
                            next_offset = upd_dict["update_id"] + 1
                            # edited_message handling:
                            #
                            # Telegram re-delivers edited messages as
                            # `edited_message` updates (separate from
                            # the original `message`). The bot used to
                            # skip non-command edits — the original
                            # was already answered, the edit was just
                            # a typo fix. But this made the bot
                            # appear unresponsive to the corrected
                            # version: the user sees a wrong answer
                            # for the typo and no new answer for the
                            # correction.
                            #
                            # New behaviour:
                            #   1. If the new text is a command, keep
                            #      the old behaviour (commands are
                            #      idempotent, re-running is fine).
                            #   2. If the new text is regular text,
                            #      look up the bot's previous reply
                            #      to the original message in
                            #      _bot_replies. If we have one,
                            #      delete it. Then rewrite the
                            #      update as a regular `message` and
                            #      let the dispatcher process it
                            #      like a fresh user message.
                            #   3. If we have no previous reply
                            #      (e.g., the original was rejected
                            #      for security, or the bot never
                            #      responded for some reason), just
                            #      process the edit.
                            # (The block itself moved out; see
                            # _apply_edited_message below.)
                            # Dispatch as a TASK, not awaited. This is
                            # the critical change for the Stop button:
                            # previously, the polling loop was blocked
                            # on `await _dispatch_update(...)` for the
                            # entire duration of a handler's LLM call
                            # (3-30 seconds). Any callback_query
                            # updates (e.g., the Stop button click)
                            # would queue up and only be processed
                            # AFTER the handler finished — by which
                            # time the abort_event had been popped and
                            # the Stop click was useless.
                            #
                            # With task dispatch, the for loop returns
                            # immediately and the while loop is free to
                            # call getUpdates again. The handler runs
                            # concurrently in its own task. When the
                            # user clicks Stop, the callback reaches
                            # the polling loop, gets dispatched as
                            # another task, sets the abort_event, and
                            # the original handler's between-chunk
                            # check picks it up.
                            try:
                                _spawn_dispatch(
                                    upd_dict, dispatch_update=dispatch_update)
                            except Exception:
                                # `log.exception`, not `log.info`: this is a
                                # dispatch failure, and the whole point of
                                # the call is that the bot answers. At INFO
                                # `journalctl -p warning` is empty while
                                # messages go nowhere.
                                log.exception(
                                    "[poll] task-create failed for "
                                    f"update_id={upd_id}")
                            else:
                                # Dispatch handed off. Only now is this
                                # update safe to consider delivered.
                                offset = next_offset
                    elif n_polls <= 5 or n_polls % 20 == 0:
                        log.info(f"[poll] cycle={n_polls} no updates")
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.exception(
                        f"[poll] error: {type(e).__name__}: {e!r} "
                        f"(backoff {backoff}s)")
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30.0)
    finally:
        # Cleanly close the PTB Application if it was ever built.
        # Runs on every exit path: normal `break` (graceful
        # shutdown), CancelledError, or any exception. Without
        # this, the httpx client held by app.bot leaks and prints
        # "RuntimeWarning: unclosed client" on interpreter shutdown.
        if _dispatcher is not None:
            try:
                await _dispatcher.shutdown()
            except Exception as e:
                log.exception(f"[main] dispatcher shutdown error: {e!r}")
