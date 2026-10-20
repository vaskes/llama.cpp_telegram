"""T68 — daily re-anchoring. See docs/adr/ADR-0003-context-reanchoring.md.

The reasoning behind the three modes lives in the ADR; these tests exist to
stop the reasoning from being quietly undone. Each one corresponds to a
specific way `soft` mode could rot into the sliding-window bug, or into a
mode that fails on a fork whose operator has no privileges.
"""
import ast
import importlib
import os
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BOT_DIR = REPO_ROOT / "telegram-bot"
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

DAY = 86400.0


def _reload(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    for mod in ("reanchor", "context_budget"):
        sys.modules.pop(mod, None)
    import context_budget
    importlib.import_module("context_budget")
    return importlib.import_module("reanchor")


# ---------------------------------------------------------------------------
# parse_hhmm / seconds_until_next — the arithmetic of "when"
# ---------------------------------------------------------------------------

def test_parse_hhmm():
    from reanchor import parse_hhmm
    assert parse_hhmm("05:00") == (5, 0)
    assert parse_hhmm("23:59") == (23, 59)
    assert parse_hhmm("0:00") == (0, 0)


@pytest.mark.parametrize("bad", ["5", "05-00", "aa:bb", "25:00", "05:61", ""])
def test_parse_hhmm_rejects_junk(bad):
    from reanchor import parse_hhmm
    with pytest.raises(ValueError):
        parse_hhmm(bad)


def test_seconds_until_next_is_in_the_future_always():
    """A scheduler that computes a past target sleeps until tomorrow — which
    means it silently runs every 24h instead of every 24h-from-now, or worse,
    fires immediately and every tick after. The contract is one-shot future.
    """
    from reanchor import seconds_until_next
    now = time.time()
    for hour, minute in ((5, 0), (0, 0), (23, 59), (12, 30)):
        delta = seconds_until_next(hour, minute, now=now)
        assert delta > 0, f"target in the past for {hour:02d}:{minute:02d}"
        assert delta <= DAY + 1, f"delta too large: {delta}"


def test_seconds_until_next_respects_exact_boundary():
    """At exactly 05:00:00 the next occurrence is tomorrow, not zero seconds
    away — otherwise the loop fires and immediately re-fires."""
    from reanchor import seconds_until_next
    lt = time.localtime()
    today_midnight = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 5, 0, 0, 0, 0, -1))
    delta = seconds_until_next(5, 0, now=today_midnight)
    assert delta > 23 * 3600, f"expected ~24h, got {delta}"


# ---------------------------------------------------------------------------
# engine reset — must never take the bot down
# ---------------------------------------------------------------------------

def test_engine_reset_noop_when_unset():
    from reanchor import run_engine_reset
    out = run_engine_reset("")
    assert out["ok"] and out["skipped"], (
        "an unset BOT_ENGINE_RESET_CMD is a valid configuration, not a failure"
    )


def test_engine_reset_runs_the_command():
    import tempfile
    from reanchor import run_engine_reset
    marker = Path(tempfile.mkdtemp()) / "touched"
    out = run_engine_reset(f"touch {marker}")
    assert out["ok"] and marker.exists(), "the operator's command must actually run"


def test_engine_reset_reports_failure_without_raising():
    from reanchor import run_engine_reset
    out = run_engine_reset("exit 3")
    assert out["ok"] is False and out["returncode"] == 3


def test_engine_reset_survives_a_broken_command():
    """shell=True means a missing binary is not an exception but a non-zero
    rc; a genuinely broken string must still not propagate."""
    from reanchor import run_engine_reset
    out = run_engine_reset("this-command-does-not-exist-xyz --nope")
    assert out["ok"] is False


def test_engine_reset_timeout_is_bounded():
    from reanchor import run_engine_reset
    out = run_engine_reset("sleep 5", timeout=1)
    assert out["ok"] is False and out["error"] == "timeout"


# ---------------------------------------------------------------------------
# mode selection
# ---------------------------------------------------------------------------

def test_soft_mode_enables_the_scheduler():
    mod = _reload(__import__("pytest").MonkeyPatch(),
                  BOT_REANCHOR_MODE="soft")
    assert mod.SOFT_REANCHOR_ENABLED is True


@pytest.mark.parametrize("mode", ["process", "process+engine"])
def test_process_modes_disable_the_in_process_scheduler(mode):
    """Both process modes re-derive the anchor when the process starts. If the
    in-process scheduler ALSO ran, the anchor would move twice per restart
    cycle — and in a self-restarting deployment that is a loop."""
    mod = _reload(__import__("pytest").MonkeyPatch(), BOT_REANCHOR_MODE=mode)
    assert mod.SOFT_REANCHOR_ENABLED is False


def test_unknown_mode_falls_back_and_warns(capsys):
    """A typo in .env must not stop the bot from starting — same rule as
    CONTEXT_STRATEGY — but it must be loud, because silently running in a
    different mode than the operator asked for is its own kind of drift."""
    mod = _reload(__import__("pytest").MonkeyPatch(),
                  BOT_REANCHOR_MODE="proccess")
    assert mod.REANCHOR_MODE == "process+engine"
    assert "BOT_REANCHOR_MODE" in capsys.readouterr().out


def test_default_mode_is_what_the_operator_runs():
    """Changing the default silently would change behaviour for every fork
    that did not set the variable. Pin it."""
    mod = _reload(__import__("pytest").MonkeyPatch())
    assert mod.REANCHOR_MODE == "process+engine"


# ---------------------------------------------------------------------------
# reanchor() — the only sanctioned writer
# ---------------------------------------------------------------------------

def test_reanchor_moves_the_anchor_forward(monkeypatch):
    mod = _reload(monkeypatch, BOT_REANCHOR_MODE="soft")
    import context_budget as cb
    before = cb.CONTEXT_ANCHOR
    info = cb.reanchor()
    assert info["ok"] is True
    assert cb.CONTEXT_ANCHOR >= before, "the anchor must move forward in time"
    assert abs(cb.CONTEXT_ANCHOR - (time.time() - 7 * DAY)) < 5


def test_reanchor_bumps_generation(monkeypatch):
    mod = _reload(monkeypatch, BOT_REANCHOR_MODE="soft")
    import context_budget as cb
    g0 = cb.ANCHOR_GENERATION
    cb.reanchor()
    assert cb.ANCHOR_GENERATION == g0 + 1


def test_reanchor_failure_keeps_the_old_anchor(monkeypatch):
    """The failure direction matters: an OLDER anchor only widens the window,
    which is safe. A broken re-anchor that cleared the anchor or raised would
    turn a minor error into a bot with no memory at all."""
    mod = _reload(monkeypatch, BOT_REANCHOR_MODE="soft")
    import context_budget as cb
    before = cb.CONTEXT_ANCHOR
    monkeypatch.setattr(cb, "_anchor_for_days", lambda d: 1 / 0)
    out = cb.reanchor()
    assert out["ok"] is False
    assert cb.CONTEXT_ANCHOR == before, "a failed re-anchor must not move the anchor"


def test_zero_remember_days_disables_the_age_filter(monkeypatch):
    """BOT_REMEMBER_DAYS=0 means "no age filter" — the anchor is 0, so every
    message passes the age test."""
    mod = _reload(monkeypatch, BOT_REANCHOR_MODE="soft",
                  BOT_REMEMBER_DAYS="0")
    import context_budget as cb
    cb.reanchor()
    assert cb.CONTEXT_ANCHOR == 0.0


# ---------------------------------------------------------------------------
# wiring + AST tripwires
# ---------------------------------------------------------------------------

def test_dispatch_starts_the_scheduler():
    src = (BOT_DIR / "dispatch.py").read_text()
    assert "start_reanchor_scheduler" in src
    # Both entry points: polling and webhook. Starting it in only one would
    # mean the scheduler silently never runs in the other mode.
    assert src.count("_start_reanchor_scheduler()") >= 2, (
        "the scheduler must be started in both the polling and the webhook "
        "entry points"
    )


def test_scheduler_does_not_use_ptb_create_task():
    """This bot never calls app.start() — see the hand-rolled polling loop.
    PTB's Application.create_task depends on that being set up."""
    src = (BOT_DIR / "reanchor.py").read_text()
    assert "app.create_task" not in src, (
        "app.create_task requires app.start(); this bot does not run it, so "
        "the task would never be tracked (and in some PTB versions never run)"
    )


def test_only_reanchor_writes_the_anchor():
    tree = ast.parse((BOT_DIR / "context_budget.py").read_text())
    writers = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = n.targets if isinstance(n, ast.Assign) else [n]
            if any(getattr(t, "id", None) in
                   ("CONTEXT_ANCHOR", "ANCHOR_GENERATION")
                   for t in targets if isinstance(t, ast.Name)):
                fns = [f.name for f in ast.walk(tree)
                       if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))
                       and any(c is n for c in ast.walk(f))]
                writers.add(fns[0] if fns else "<module>")
    assert writers <= {"<module>", "reanchor"}, (
        f"anchor state written from {sorted(writers)}; only reanchor() may "
        f"move it once the module is loaded"
    )


def test_reanchor_loop_does_not_call_reanchor_per_tick():
    """The loop must sleep between calls. A regression here is the
    sliding-window bug wearing a different hat: reanchor() called every
    second is equivalent to `now - 7 days` per request."""
    tree = ast.parse((BOT_DIR / "reanchor.py").read_text())
    loop = next(f for f in ast.walk(tree)
                if isinstance(f, ast.AsyncFunctionDef) and f.name == "reanchor_loop")
    calls = [n for n in ast.walk(loop) if isinstance(n, ast.Call)]
    assert any(isinstance(c.func, ast.Attribute) and c.func.attr == "wait"
               for c in calls), "the loop must await between re-anchors"