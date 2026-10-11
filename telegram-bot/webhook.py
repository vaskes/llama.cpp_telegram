# webhook.py
# T25: aiohttp server that receives Telegram webhook POSTs and
# hands them to the same _dispatch_update() the polling loop
# uses. Listening on WEBHOOK_HOST:WEBHOOK_PORT (default 0.0.0.0:8443)
# — when the bot is in lan_macvlan, the nginx-proxy reverse-proxies
# to its macvlan IP. When the bot is on host network, the reverse
# proxy forwards to 127.0.0.1:8443.
#
# Authentication: every request from Telegram carries the
# X-Telegram-Bot-Api-Secret-Token header set at setWebhook() time.
# We compare it constant-time. A missing/mismatched header
# returns 403 immediately, before any work.
#
# Concurrency: aiohttp spawns a task per request. The handler
# awaits _dispatch_update() which itself calls asyncio.create_task
# internally (in dispatch.py:_run). So we have layered
# concurrency: one request -> one dispatch task. This is
# what we want for webhooks (each POST = one update).
import asyncio
import hmac
import json
import logging

import aiohttp
from aiohttp import web

from telegram import Update

import config


log = logging.getLogger(__name__)


# Cap incoming bodies. Telegram updates are < 100 KB; we allow 1 MB
# as a generous margin but no more — without a cap, a malicious
# request that bypasses the secret check (or a future bug) could
# OOM the bot by streaming a huge body.
_MAX_BODY_BYTES = 1 * 1024 * 1024

# v0.10.1 (Qwen wave-2 review): backpressure cap on pending dispatch
# tasks. _GLOBAL_LLM_SEM_LIMIT=4 and per-user sem=2 cap CONCURRENCY
# (how many LLM calls run at once), not QUEUE DEPTH (how many tasks
# wait in memory for a slot). Without this cap, a flood that
# produces 1000 messages in 10s would spawn 1000 asyncio.create_task
# objects, each holding the Update + history + closures. The semaphores
# would still cap in-flight LLM calls to 4-6, but the other ~994
# tasks would sit in memory waiting their turn.
#
# MAX_PENDING_DISPATCH caps the queue. 50 is generous (≈10s of
# burst at Telegram's normal rate) but bounded. Beyond that we
# reject with 503 and let Telegram retry the update (it will, with
# backoff). 503 is the right status — it tells Telegram the bot is
# alive but busy, so Telegram doesn't mark the webhook as broken.
_MAX_PENDING_DISPATCH = 50
_pending_dispatch_tasks: "set[asyncio.Task]" = set()


def _register_pending(task: "asyncio.Task") -> None:
    """Add a dispatch task to the pending set with auto-cleanup on completion."""
    _pending_dispatch_tasks.add(task)
    task.add_done_callback(_pending_dispatch_tasks.discard)


def _check_secret(request: web.Request) -> bool:
    """Constant-time compare of X-Telegram-Bot-Api-Secret-Token
    against config.WEBHOOK_SECRET_TOKEN. Returns True if the
    header matches, False otherwise (or if not configured).
    """
    expected = (config.WEBHOOK_SECRET_TOKEN or "").encode("utf-8")
    if not expected:
        # Misconfigured: refuse everything. The operator must
        # set WEBHOOK_SECRET_TOKEN in the env.
        return False
    got = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "").encode("utf-8")
    return hmac.compare_digest(expected, got)


async def _handle(request: web.Request) -> web.Response:
    if not _check_secret(request):
        log.warning(
            "[webhook] rejected request: bad or missing "
            "X-Telegram-Bot-Api-Secret-Token from %s",
            request.remote,
        )
        return web.Response(status=403, text="forbidden")
    # Body size cap. aiohttp enforces client_max_size globally,
    # but we also re-check here so the JSON parse never sees a
    # pathologically large string.
    if request.content_length and request.content_length > _MAX_BODY_BYTES:
        log.warning(
            "[webhook] rejected request: body %d > %d from %s",
            request.content_length, _MAX_BODY_BYTES, request.remote,
        )
        return web.Response(status=413, text="payload too large")
    try:
        body = await request.read()
        if len(body) > _MAX_BODY_BYTES:
            log.warning(
                "[webhook] rejected request: body %d > %d from %s",
                len(body), _MAX_BODY_BYTES, request.remote,
            )
            return web.Response(status=413, text="payload too large")
        upd = Update.de_json(json.loads(body), None)
    except Exception as e:
        log.warning("[webhook] de_json failed: %s: %r", type(e).__name__, e)
        return web.Response(status=400, text="bad update")
    if upd is None:
        return web.Response(status=400, text="null update")
    # Late import: dispatch.py imports bot.py which would
    # circularly import webhook.py if we did this at module load.
    from dispatch import _dispatch_update

    # v0.9.2: wrap the dispatch task in a try/except so a future
    # bug in a handler doesn't get silently swallowed by the loop's
    # default exception handler. We still return 200 to Telegram
    # because the request itself was valid; the operator sees the
    # traceback in `docker logs`.
    async def _safe_dispatch():
        try:
            await _dispatch_update(upd.to_dict())
        except Exception:
            log.exception(
                "[webhook] dispatch failed: update_id=%s", upd.update_id
            )

    # Spawn the update on the same event loop the polling loop
    # would use. We don't await — Telegram expects 200 within
    # ~5s and LLM calls can take 30s+; fire-and-forget is the
    # only sane pattern.
    #
    # v0.10.1: cap pending tasks. If the queue is full, reject
    # with 503 — Telegram will retry with backoff. This is the
    # right HTTP status for "alive but overloaded".
    if len(_pending_dispatch_tasks) >= _MAX_PENDING_DISPATCH:
        log.warning(
            "[webhook] rejected: %d pending tasks >= %d cap",
            len(_pending_dispatch_tasks), _MAX_PENDING_DISPATCH,
        )
        return web.Response(status=503, text="bot busy, retry later")
    task = asyncio.create_task(_safe_dispatch())
    _register_pending(task)
    return web.Response(status=200, text="ok")


async def _healthz(request: web.Request) -> web.Response:
    """Health check for the operator. Returns 200 with the
    version string. Nginx doesn't need to proxy this; the
    operator can hit it via 127.0.0.1:8443 from the host, or
    via the macvlan IP from another LAN host.
    """
    return web.Response(text=f"webhook-ok mode=webhook")


def make_app() -> web.Application:
    # client_max_size caps body size at the aiohttp layer. We also
    # re-check in _handle() because client_max_size is enforced
    # when reading, and we want an explicit 413 response rather
    # than a generic "payload too large" exception.
    app = web.Application(client_max_size=_MAX_BODY_BYTES)
    # Match the path prefix WEBHOOK_PATH so any secret token
    # suffix is accepted (Telegram appends random bytes).
    app.router.add_post(config.WEBHOOK_PATH + r"/{tail:.*}", _handle)
    app.router.add_get("/healthz", _healthz)
    return app


async def run() -> None:
    """Start the aiohttp server. Returns when the server stops.

    v0.10.2 (Qwen wave-3): add signal handlers for graceful shutdown
    in webhook mode. Without this, `docker stop` (SIGTERM) kills the
    aiohttp server abruptly and in-flight requests are dropped
    mid-stream. With the shutdown_event, SIGTERM triggers
    runner.cleanup() so active dispatches can finish.
    """
    import signal
    app = make_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, config.WEBHOOK_HOST, config.WEBHOOK_PORT)
    await site.start()
    log.info(
        "[webhook] listening on http://%s:%d%s/  (secret-token enforced, "
        "body cap %d KB)",
        config.WEBHOOK_HOST, config.WEBHOOK_PORT, config.WEBHOOK_PATH,
        _MAX_BODY_BYTES // 1024,
    )
    # v0.10.2: shutdown event triggered by SIGTERM/SIGINT
    shutdown_event = asyncio.Event()
    def _on_signal(signame):
        log.info("[webhook] received %s, initiating shutdown", signame)
        shutdown_event.set()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _on_signal, sig.name)
        except (NotImplementedError, RuntimeError):
            pass  # Windows or non-main thread, fall through
    try:
        await shutdown_event.wait()
    finally:
        # Give in-flight tasks a chance to finish, then stop the runner
        log.info("[webhook] stopping runner, %d tasks still pending", len(_pending_dispatch_tasks))
        await runner.cleanup()
