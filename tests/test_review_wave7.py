"""Review 2026-10-22 (second wave): persona meta-directive, anchor timezone.

Two fixes, two tripwires. Both are written so that the mutation which
reverts the fix makes them red, which is the only thing that distinguishes
a tripwire from a decoration -- see the four survivors recorded in
docs/archive/2026-10/REVIEW-BRIEF-2026-10-22.md.
"""
import ast
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "telegram-bot"))


# ---------------------------------------------------------------- persona --

def _render(persona):
    import personality
    return personality.render_persona(persona)


def test_rendered_persona_tells_the_model_not_to_invent():
    """The whole point: the rule was a docstring, the model never read it."""
    out = _render({"enabled": True, "name": "Гадя", "profile": "врач"})
    assert out, "a configured persona must render"
    low = out.lower()
    assert "неизвестно" in low, (
        "the persona block must tell the model that anything not stated "
        "above is unknown to it, otherwise it fills the gap from the user's "
        "tone after three or four turns"
    )


def test_directive_names_the_class_not_an_enumeration():
    """An enumeration is a list someone forgets to extend."""
    directive = [ln for ln in _render({"enabled": True, "name": "X",
                                       "profile": "p"}).splitlines()
                 if "неизвестно" in ln.lower()]
    assert len(directive) == 1, f"expected exactly one directive line, got {directive}"
    text = directive[0].lower()
    # age/sex are the current examples, but the sentence must not consist
    # of only them -- otherwise adding a third field silently drops it.
    assert not any(w in text for w in ("только", "только возраст")), (
        "the directive must generalise past the fields it was written for"
    )


def test_directive_absent_when_persona_disabled():
    assert _render({"enabled": False, "name": "X", "profile": "p"}) == ""
    assert _render({}) == ""


def test_directive_is_absent_when_only_a_bare_name():
    """A bare name used to return '' deliberately; keep that."""
    assert _render({"enabled": True, "name": "Гадя"}) == ""


def test_directive_does_not_change_the_described_facts():
    """It appends a line; it must not rewrite the biography."""
    p = {"enabled": True, "name": "Гадя", "age": 36, "sex": "female",
         "profile": "врач", "traits": ["злая"]}
    out = _render(p)
    body = [ln for ln in out.splitlines() if "неизвестно" not in ln.lower()]
    assert body == ["Ты — Гадя.", "Ты женщина, 36 лет.", "Врач.",
                    "Черты характера: злая."]


def test_directive_does_not_reach_the_prompt_twice():
    out = _render({"enabled": True, "name": "X", "profile": "p"})
    assert out.lower().count("неизвестно") == 1


# -------------------------------------------------------------- timezone --

def _fresh_reanchor(monkeypatch, **env):
    """Reimport so the module-level cache does not leak between tests."""
    for k in ("BOT_REANCHOR_TZ", "TZ"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import reanchor
    import importlib
    return importlib.reload(reanchor)


def test_anchor_uses_the_configured_zone(monkeypatch):
    """The finding: TZ unset means UTC, so 05:00 fired three hours late."""
    m = _fresh_reanchor(monkeypatch, BOT_REANCHOR_TZ="Europe/Moscow")
    assert str(m._anchor_timezone()) == "Europe/Moscow"
    assert str(m._anchor_timezone()) == "Europe/Moscow"  # cached, still sane


def test_anchor_default_is_utc_and_says_so(monkeypatch):
    m = _fresh_reanchor(monkeypatch)
    tz = m._anchor_timezone()
    assert tz.utcoffset(None) is not None
    assert tz.utcoffset(__import__("datetime").datetime(2026, 7, 1)) \
        .total_seconds() == 0


def test_utc_and_moscow_disagree_by_three_hours(monkeypatch):
    """The actual regression, as a difference: same instant, same 05:00."""
    # `now` is pinned, not time.time().
    #
    # This test compares the two zones' NEXT 05:00 and expects a 3h
    # difference -- which only holds while the current instant sits
    # between 02:00 and 05:00 UTC. Outside that window one zone has
    # already passed 05:00 and rolls to tomorrow while the other has
    # not, and the difference becomes 21h instead of 3h. It went green
    # all evening and red at 02:06 UTC on 2026-10-22, with no code
    # change anywhere: a time-of-day-dependent assertion waiting for
    # the clock to reach it.
    #
    # Noon UTC is comfortably inside the window on every day of the
    # year, so the property under test -- same wall clock, two zones,
    # three hours apart -- is what runs.
    import datetime
    now = datetime.datetime(2026, 7, 1, 12, 0,
                             tzinfo=datetime.timezone.utc).timestamp()
    a = _fresh_reanchor(monkeypatch, BOT_REANCHOR_TZ="UTC")
    utc = a.seconds_until_next(5, 0, now)
    b = _fresh_reanchor(monkeypatch, BOT_REANCHOR_TZ="Europe/Moscow")
    msk = b.seconds_until_next(5, 0, now)
    assert abs((utc - msk) - 3 * 3600) < 60, (
        f"expected a 3h difference between 05:00 UTC and 05:00 Moscow, "
        f"got {abs(utc - msk) / 3600:.2f}h -- the zone is probably ignored"
    )


def test_tz_is_a_fallback_not_a_silent_override(monkeypatch):
    m = _fresh_reanchor(monkeypatch, TZ="Europe/Berlin")
    assert str(m._anchor_timezone()) == "Europe/Berlin"


def test_bogus_zone_degrades_to_utc_instead_of_crashing(monkeypatch):
    m = _fresh_reanchor(monkeypatch, BOT_REANCHOR_TZ="Mars/Olympus")
    import datetime
    assert m._anchor_timezone().utcoffset(
        datetime.datetime(2026, 7, 1)).total_seconds() == 0
    assert m.seconds_until_next(5, 0, 1_800_000_000.0) > 0


def test_no_naive_datetime_survives_in_the_anchor_path():
    """The property, not the spelling: every combine() there is tz-aware."""
    src = (ROOT / "telegram-bot" / "reanchor.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "seconds_until_next")
    bare = [n for n in ast.walk(fn)
            if isinstance(n, ast.Call)
            and getattr(n.func, "attr", "") == "combine"
            and not any(k.arg == "tzinfo" for k in n.keywords)]
    assert not bare, (
        f"seconds_until_next has {len(bare)} tz-naive datetime.combine call(s); "
        "each one reinterprets the operator's wall clock in the container's zone"
    )


def test_anchor_timezone_helper_is_not_cached_across_zone_changes(monkeypatch):
    """A stale cache would make the setting look broken after an edit."""
    import importlib
    m = _fresh_reanchor(monkeypatch, BOT_REANCHOR_TZ="UTC")
    assert str(m._anchor_timezone()) == "UTC"
    monkeypatch.setenv("BOT_REANCHOR_TZ", "Europe/Moscow")
    m2 = importlib.reload(m)
    assert str(m2._anchor_timezone()) == "Europe/Moscow"


def test_fallback_log_reports_the_resolved_zone_not_the_requested_one(
        monkeypatch, caplog):
    """The line right after 'unusable, falling back to UTC' must not
    still name the unusable zone. It cost an hour once already, and the
    first version of this fix reintroduced it."""
    import logging
    m = _fresh_reanchor(monkeypatch, BOT_REANCHOR_TZ="Mars/Olympus")
    with caplog.at_level(logging.INFO, logger="reanchor"):
        m._anchor_timezone()
    # force a fresh resolution in case another test warmed the cache
    m._REANCHOR_TZ, m._REANCHOR_TZ_NAME = None, None
    with caplog.at_level(logging.INFO, logger="reanchor"):
        m._anchor_timezone()
    resolved_lines = [r.getMessage() for r in caplog.records
                      if "is read as" in r.getMessage()]
    assert resolved_lines, "the resolved zone must be logged when it changes"
    for msg in resolved_lines:
        assert "Mars/Olympus" not in msg, (
            f"log names an unusable zone as the resolved one: {msg!r}"
        )
        assert "'UTC'" in msg, f"resolved zone not reported: {msg!r}"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))