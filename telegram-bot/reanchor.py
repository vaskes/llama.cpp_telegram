"""T68 — daily re-anchoring of the memory window. See ADR-0003.

Why this exists, in one paragraph: the context window is anchored at bot
start, so it must be re-anchored or it never moves. Historically that meant
"restart the bot at 05:00". But restarting the bot and restarting the engine
are two different operations, and neither is required for the context window
to be correct — the window is a timestamp the bot computes locally. Restarting
the engine is VRAM hygiene. This module therefore supports three modes, and
the only one that needs any privilege at all is `soft`.

`soft` mode re-anchors in-process. It exists because of the 200+ forks of
this repo: an operator whose engine runs on a machine they do not control must
still be able to re-anchor, and `process`/`process+engine` would fail for them
at exactly the moment they need it.
"""

from __future__ import annotations

import asyncio
import subprocess
import time

from context_budget import (
    ENGINE_RESET_CMD,
    REANCHOR_AT,
    REANCHOR_MODE,
    SOFT_REANCHOR_ENABLED,
    get_budget,
    reanchor,
)

from logconf import get_logger
log = get_logger(__name__)


async def summarise_after_reanchor() -> dict:
    """T70.3 — rebuild the out-of-window summaries for every thread.

    Runs on EVERY re-anchor mode, not just `soft`. That is deliberate: if it
    were tied to process start, `soft` and `process` would differ in memory
    quality while looking identical in config (ADR-0003).

    A failure here is non-fatal by construction — summarise_all() catches per
    thread and falls back to the plain window.
    """
    if get_budget().strategy != "summarize":
        return {"ok": True, "skipped": True, "reason": "strategy != summarize"}
    try:
        import config
        import storage
        import summarize
        return await summarize.summarise_all(
            budget=get_budget(),
            store=storage.get_store(),
            llama_url=config.LLAMA_URL,
            model=config.MODEL,
        )
    except Exception as exc:
        log.info(f"[reanchor] summarisation failed (window strategy still "
              f"works): {type(exc).__name__}: {exc}")
        return {"ok": False, "error": str(exc)}


def parse_hhmm(value: str) -> tuple[int, int]:
    """"05:00" -> (5, 0). Raises ValueError on anything else."""
    hh, _, mm = value.partition(":")
    h, m = int(hh), int(mm)
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(f"out of range: {value!r}")
    return h, m


# --- timezone (2026-10-22) -----------------------------------------------
#
# BOT_REANCHOR_AT has always been read as "local time wherever the process
# runs". That is the container's timezone, which is not the operator's:
# TZ is unset here, so the container is UTC and 05:00 fires at 08:00 for
# the person who set it. The arithmetic was self-consistent, so nothing
# failed -- the anchor just arrived three hours late and the owner had no
# way to see why.
#
# Asked, not guessed. Reading the host's zone would be wrong too: it
# moves with whoever last logged in, and the bot's owner is not
# necessarily the machine's owner.
_REANCHOR_TZ = None      # cached zone
_REANCHOR_TZ_NAME = None  # the setting that produced it


def _anchor_timezone():
    """The zone BOT_REANCHOR_AT is interpreted in.

    Cached against the *setting*, not for the lifetime of the process.
    Keying it to the process was a real bug the first time round: the
    first zone resolved stayed in force for every later call, so the DST
    test ran its Europe/Berlin case in America/New_York and the anchor
    fired an hour late. If BOT_REANCHOR_TZ changes, this follows.
    """
    global _REANCHOR_TZ, _REANCHOR_TZ_NAME
    import datetime as _dt
    import os
    import zoneinfo

    name = os.environ.get("BOT_REANCHOR_TZ") or os.environ.get("TZ") or "UTC"
    if _REANCHOR_TZ is not None and _REANCHOR_TZ_NAME == name:
        return _REANCHOR_TZ

    resolved = name
    try:
        tz = (_dt.timezone.utc if name == "UTC"
              else zoneinfo.ZoneInfo(name))
    except Exception as e:
        log.warning(f"[reanchor] BOT_REANCHOR_TZ={name!r} unusable ({e}); "
                    f"falling back to UTC. The anchor will fire at "
                    f"{REANCHOR_AT!r} UTC, not your local time.")
        tz = _dt.timezone.utc
        resolved = "UTC"

    _REANCHOR_TZ, _REANCHOR_TZ_NAME = tz, resolved
    # Report what was resolved, not what was asked for. Naming
    # 'Mars/Olympus' on the line after saying it was unusable costs an
    # hour of debugging every time it happens.
    log.info(f"[reanchor] BOT_REANCHOR_AT={REANCHOR_AT!r} is read as "
             f"{resolved!r} local time (set BOT_REANCHOR_TZ to change it)")
    return tz

def seconds_until_next(hour: int, minute: int, now: float | None = None) -> float:
    """Seconds from `now` until the next occurrence of hour:minute local time.

    Computed in absolute terms from the epoch rather than by sleeping until
    a wall-clock moment: the loop must survive a suspended host, a DST shift
    and a clock adjustment without ending up waiting 24 hours.
    """
    now = time.time() if now is None else now
    # Calendar arithmetic on local dates, NOT epoch arithmetic with a flat
    # 86400. A DST day is 23 or 25 hours long, so "midnight + 24h + 5h" does
    # not land on 05:00 the next day. Measured in America/New_York: on the
    # morning after a spring-forward probe the old code fired at 06:00 and
    # then skipped the entire following day, because each re-evaluation found
    # its target in the past and pushed it another 86400 out.
    #
    # datetime.combine(...).timestamp() resolves the UTC offset for that
    # specific date, which is the whole point.
    import datetime as _dt
    tz = _anchor_timezone()
    today = _dt.datetime.fromtimestamp(now, tz).date()
    target = _dt.datetime.combine(today, _dt.time(hour, minute), tzinfo=tz)
    if target.timestamp() <= now:
        target = _dt.datetime.combine(
            today + _dt.timedelta(days=1), _dt.time(hour, minute), tzinfo=tz
        )
    return target.timestamp() - now


def run_engine_reset(cmd: str = ENGINE_RESET_CMD, timeout: int = 120) -> dict:
    """Run BOT_ENGINE_RESET_CMD. Never raises.

    An empty command means "do not touch the engine" — that is a valid and
    common configuration, not an error.

    The command is operator-supplied from their own .env and runs as the bot
    user, which is the same trust boundary as any other systemd unit ExecStart
    the operator wrote. It is not sandboxed, and must not pretend to be.
    """
    if not cmd:
        return {"ok": True, "skipped": True, "reason": "no BOT_ENGINE_RESET_CMD"}
    try:
        proc = subprocess.run(
            cmd, shell=True, timeout=timeout,
            capture_output=True, text=True,
        )
    except subprocess.TimeoutExpired:
        log.info(f"[reanchor] engine reset timed out after {timeout}s: {cmd}")
        return {"ok": False, "error": "timeout"}
    except Exception as exc:
        log.info(f"[reanchor] engine reset failed to start: {exc}")
        return {"ok": False, "error": str(exc)}
    if proc.returncode != 0:
        # Log the tail: an operator debugging this needs to see WHY, and
        # these are their own command's output, not model or user data.
        tail = (proc.stderr or proc.stdout or "").strip()[-500:]
        log.info(f"[reanchor] engine reset rc={proc.returncode}: {tail}")
        return {"ok": False, "returncode": proc.returncode, "output": tail}
    log.info(f"[reanchor] engine reset ok: {cmd}")
    return {"ok": True, "returncode": 0}


async def reanchor_loop(stop: asyncio.Event) -> None:
    """Daily re-anchor scheduler. Only ever started in `soft` mode.

    Sleeps in chunks rather than one long sleep so that a clock jump, a DST
    change or a suspend/resume cannot leave it waiting until tomorrow. Each
    wake re-evaluates the target from the current clock.
    """
    hour, minute = parse_hhmm(REANCHOR_AT)
    log.info(f"[reanchor] scheduler armed: mode={REANCHOR_MODE} at={REANCHOR_AT} "
        f"soft={SOFT_REANCHOR_ENABLED}")
    while not stop.is_set():
        try:
            delay = seconds_until_next(hour, minute)
        except Exception as exc:
            log.info(f"[reanchor] bad BOT_REANCHOR_AT {REANCHOR_AT!r}: {exc}")
            return
        # Wake up at least every 10 minutes so a clock jump is noticed.
        remaining = delay
        while remaining > 0 and not stop.is_set():
            chunk = min(remaining, 600.0)
            try:
                await asyncio.wait_for(stop.wait(), timeout=chunk)
            except asyncio.TimeoutError:
                pass
            remaining = seconds_until_next(hour, minute)
        if stop.is_set():
            return
        info = await asyncio.to_thread(reanchor)
        # T70.3: summarisation is keyed to the re-anchor, in every mode.
        await summarise_after_reanchor()
        if info.get("ok") and REANCHOR_MODE == "process+engine":
            await asyncio.to_thread(run_engine_reset)
        # Re-anchor every day even though in soft mode the window already
        # slid forward — that is what moves the messages summary-able later
        # (T70.3) and keeps /stats honest about the anchor generation.
        await asyncio.sleep(1.0)


def start_reanchor_scheduler() -> None:
    """Attach the scheduler to the running loop, if the mode calls for it.

    Returns immediately in `process`/`process+engine` modes: there, the
    re-anchor is a consequence of the process starting, and running the
    in-process scheduler too would move the anchor twice per restart cycle.

    Uses asyncio.create_task, not Application.create_task: this bot never
    calls app.start() (see the hand-rolled polling loop in dispatch.py), so
    PTB's own task bookkeeping is not set up. Must be called from inside the
    running loop, after initialize().
    """
    if not SOFT_REANCHOR_ENABLED:
        log.info(f"[reanchor] mode={REANCHOR_MODE}: no in-process scheduler; the "
            f"anchor is re-derived when the process starts")
        return
    stop = asyncio.Event()
    task = asyncio.get_running_loop().create_task(
        reanchor_loop(stop), name="reanchor_loop"
    )
    _HANDLES.append((stop, task))
    log.info("[reanchor] scheduler started")


#: Keeps stop/task alive so they are not garbage collected mid-sleep. The
#: loop is process-lifetime anyway; cancellation is best-effort cleanup.
_HANDLES: list[tuple[asyncio.Event, asyncio.Task]] = []

def start_summarise_on_boot() -> None:
    """T70.3 — in `process`/`process+engine` there is no in-process scheduler,
    so nothing would ever build the summaries. Fire once at boot.

    Background task, never awaited on the startup path: summarisation can take
    minutes across many threads, and the bot must accept Telegram updates
    immediately regardless. A failure here is logged and the bot continues with
    the plain window.
    """
    try:
        import asyncio as _a
        if get_budget().strategy != "summarize":
            return
        task = _a.get_running_loop().create_task(
            summarise_after_reanchor(), name="summarise_on_boot"
        )
        _HANDLES.append((None, task))
        log.info("[summarize] boot summarisation scheduled")
    except Exception as exc:
        log.info(f"[summarize] boot hook failed: {exc!r}")
