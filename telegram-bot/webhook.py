# webhook.py
# T25: aiohttp server that receives Telegram webhook POSTs and
# hands them to the same _dispatch_update() the polling loop
# uses. Listening on 127.0.0.1:WEBHOOK_PORT only — nginx-proxy
# in front of us terminates TLS and reverse-proxies here.
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
import logging
from typing import Optional

import aiohttp
from aiohttp import web

from telegram import Update

import config


log = logging.getLogger(__name__)


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
    try:
        body = await request.read()
        upd = Update.de_json(__import__("json").loads(body), None)
    except Exception as e:
        log.warning("[webhook] de_json failed: %s: %r", type(e).__name__, e)
        return web.Response(status=400, text="bad update")
    if upd is None:
        return web.Response(status=400, text="null update")
    # Late import: dispatch.py imports bot.py which would
    # circularly import webhook.py if we did this at module load.
    from dispatch import _dispatch_update
    # Spawn the update on the same event loop the polling loop
    # would use. We don't await — Telegram expects 200 within
    # ~5s and LLM calls can take 30s+; fire-and-forget is the
    # only sane pattern.
    asyncio.create_task(_dispatch_update(upd.to_dict()))
    return web.Response(status=200, text="ok")


async def _healthz(request: web.Request) -> web.Response:
    """Health check for the operator. Returns 200 with the
    version string. Nginx doesn't need to proxy this; the
    operator can hit it via 127.0.0.1:8443 from the host.
    """
    return web.Response(text=f"webhook-ok mode=webhook")


def make_app() -> web.Application:
    app = web.Application()
    # Match the path prefix WEBHOOK_PATH so any secret token
    # suffix is accepted (Telegram appends random bytes).
    app.router.add_post(config.WEBHOOK_PATH + r"/{tail:.*}", _handle)
    app.router.add_get("/healthz", _healthz)
    return app


async def run() -> None:
    """Start the aiohttp server. Returns when the server stops."""
    app = make_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, config.WEBHOOK_HOST, config.WEBHOOK_PORT)
    await site.start()
    log.info(
        "[webhook] listening on http://%s:%d%s/  (secret-token enforced)",
        config.WEBHOOK_HOST, config.WEBHOOK_PORT, config.WEBHOOK_PATH,
    )
    # Block forever. The runner keeps the loop alive.
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
