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
    reanchor,
)


def parse_hhmm(value: str) -> tuple[int, int]:
    """"05:00" -> (5, 0). Raises ValueError on anything else."""
    hh, _, mm = value.partition(":")
    h, m = int(hh), int(mm)
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(f"out of range: {value!r}")
    return h, m


def seconds_until_next(hour: int, minute: int, now: float | None = None) -> float:
    """Seconds from `now` until the next occurrence of hour:minute local time.

    Computed in absolute terms from the epoch rather than by sleeping until
    a wall-clock moment: the loop must survive a suspended host, a DST shift
    and a clock adjustment without ending up waiting 24 hours.
    """
    now = time.time() if now is None else now
    lt = time.localtime(now)
    today_midnight = now - (lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec)
    target = today_midnight + hour * 3600 + minute * 60
    if target <= now:
        target += 86400.0
    return target - now


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
        print(f"[reanchor] engine reset timed out after {timeout}s: {cmd}",
              flush=True)
        return {"ok": False, "error": "timeout"}
    except Exception as exc:
        print(f"[reanchor] engine reset failed to start: {exc}", flush=True)
        return {"ok": False, "error": str(exc)}
    if proc.returncode != 0:
        # Log the tail: an operator debugging this needs to see WHY, and
        # these are their own command's output, not model or user data.
        tail = (proc.stderr or proc.stdout or "").strip()[-500:]
        print(f"[reanchor] engine reset rc={proc.returncode}: {tail}", flush=True)
        return {"ok": False, "returncode": proc.returncode, "output": tail}
    print(f"[reanchor] engine reset ok: {cmd}", flush=True)
    return {"ok": True, "returncode": 0}


async def reanchor_loop(stop: asyncio.Event) -> None:
    """Daily re-anchor scheduler. Only ever started in `soft` mode.

    Sleeps in chunks rather than one long sleep so that a clock jump, a DST
    change or a suspend/resume cannot leave it waiting until tomorrow. Each
    wake re-evaluates the target from the current clock.
    """
    hour, minute = parse_hhmm(REANCHOR_AT)
    print(
        f"[reanchor] scheduler armed: mode={REANCHOR_MODE} at={REANCHOR_AT} "
        f"soft={SOFT_REANCHOR_ENABLED}",
        flush=True,
    )
    while not stop.is_set():
        try:
            delay = seconds_until_next(hour, minute)
        except Exception as exc:
            print(f"[reanchor] bad BOT_REANCHOR_AT {REANCHOR_AT!r}: {exc}",
                  flush=True)
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
        print(
            f"[reanchor] mode={REANCHOR_MODE}: no in-process scheduler; the "
            f"anchor is re-derived when the process starts",
            flush=True,
        )
        return
    stop = asyncio.Event()
    task = asyncio.get_running_loop().create_task(
        reanchor_loop(stop), name="reanchor_loop"
    )
    _HANDLES.append((stop, task))
    print("[reanchor] scheduler started", flush=True)


#: Keeps stop/task alive so they are not garbage collected mid-sleep. The
#: loop is process-lifetime anyway; cancellation is best-effort cleanup.
_HANDLES: list[tuple[asyncio.Event, asyncio.Task]] = []