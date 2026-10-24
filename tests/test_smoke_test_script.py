"""The smoke test's own logic, without a bot.

`scripts/smoke_test.py` is the operator's replacement for the manual
checklist: it asks a real bot over the real Telegram API whether the
bot is alive and whether it answered. That is the whole point of it,
and it is also why it cannot be tested by asking a real bot -- the
production host is not always there, and a test that needs one is a
test that only runs on the operator's machine.

So the script is tested from the other side: the Telegram API is
replaced by a fake `urlopen`, and the checks the script makes are
checked here. What is being tested is the *script*, not the bot.

The rules the script itself has to obey, and what each one is worth:

  * a token that does not work is a FAIL, and nothing else is checked;
  * a webhook that points somewhere else is a FAIL -- the bot is
    answering, just not where the operator thinks;
  * a webhook with a last error is a FAIL, even if the URL is right;
  * `edited_message` still in the subscription is a WARN: the feature
    was removed on 2026-10-24, the bot ignores edits anyway, and the
    subscription survives a restart;
  * an answer that is the empty-response diagnostic is a FAIL, not an
    answer;
  * a database with an empty assistant turn is a FAIL;
  * a log with a fatal line is a FAIL.
"""
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "smoke_test.py"


def _load():
    """The script as a module, so its functions can be called."""
    spec = importlib.util.spec_from_file_location("smoke_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["smoke_test"] = module
    spec.loader.exec_module(module)
    return module


smoke = _load()


class _Response:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _api_returns(monkeypatch, results):
    """Every API call returns the next entry of `results`."""
    calls = []

    def fake_urlopen(request, timeout=None):
        calls.append(request.full_url)
        payload = results.pop(0)
        if isinstance(payload, Exception):
            raise payload
        return _Response(payload)

    monkeypatch.setattr(smoke.urllib.request, "urlopen", fake_urlopen)
    return calls


def _ok(result):
    return {"ok": True, "result": result}


# --- the token -------------------------------------------------------------

def test_a_working_token_passes(monkeypatch, capsys):
    _api_returns(monkeypatch, [_ok({"id": 42, "username": "testbot"})])
    report = smoke.Report()
    assert smoke.check_token(report, "token") == 42
    assert report.rows[0][1] == smoke.PASS


def test_a_broken_token_fails(monkeypatch):
    _api_returns(monkeypatch, [{"ok": False, "description": "Unauthorized"}])
    report = smoke.Report()
    assert smoke.check_token(report, "token") is None
    assert report.rows[0][1] == smoke.FAIL
    assert "Unauthorized" in report.rows[0][2]


def test_a_network_error_fails(monkeypatch):
    _api_returns(monkeypatch, [OSError("connection refused")])
    report = smoke.Report()
    assert smoke.check_token(report, "token") is None
    assert report.rows[0][1] == smoke.FAIL


# --- the webhook -----------------------------------------------------------

def _webhook_report(monkeypatch, info, expect_url=None):
    _api_returns(monkeypatch, [_ok(info)])
    report = smoke.Report()
    smoke.check_webhook(report, "token", expect_url)
    return {name: (status, detail) for name, status, detail in report.rows}


def test_a_webhook_that_points_somewhere_else_fails(monkeypatch):
    rows = _webhook_report(monkeypatch, {
        "url": "https://other.example/tg-webhook/",
        "last_error_message": "",
        "pending_update_count": 0,
        "allowed_updates": ["message"],
    }, expect_url="https://bot.example/tg-webhook/")
    assert rows["webhook"][0] == smoke.FAIL
    assert "bot.example" in rows["webhook"][1]


def test_a_webhook_error_fails_even_when_the_url_is_right(monkeypatch):
    rows = _webhook_report(monkeypatch, {
        "url": "https://bot.example/tg-webhook/",
        "last_error_message": "502 Bad Gateway",
        "pending_update_count": 0,
        "allowed_updates": ["message"],
    }, expect_url="https://bot.example/tg-webhook/")
    assert rows["webhook"][0] == smoke.PASS
    assert rows["webhook errors"][0] == smoke.FAIL


def test_a_pending_queue_is_a_warning_not_a_failure(monkeypatch):
    rows = _webhook_report(monkeypatch, {
        "url": "https://bot.example/tg-webhook/",
        "last_error_message": "",
        "pending_update_count": 7,
        "allowed_updates": ["message"],
    })
    assert rows["pending updates"][0] == smoke.WARN
    assert "7" in rows["pending updates"][1]


def test_a_stale_edited_message_subscription_is_a_warning(monkeypatch):
    """The feature is gone, the bot ignores edits anyway, and the
    subscription survives a restart -- so it is a warning, not a
    failure."""
    rows = _webhook_report(monkeypatch, {
        "url": "https://bot.example/tg-webhook/",
        "last_error_message": "",
        "pending_update_count": 0,
        "allowed_updates": ["message", "edited_message"],
    })
    assert rows["allowed_updates"][0] == smoke.WARN
    assert "edited_message" in rows["allowed_updates"][1]


def test_a_polling_bot_is_a_warning_not_a_failure(monkeypatch):
    rows = _webhook_report(monkeypatch, {
        "url": "",
        "last_error_message": "",
        "pending_update_count": 0,
        "allowed_updates": ["message"],
    })
    assert rows["webhook"][0] == smoke.WARN
    assert "polling" in rows["webhook"][1]


# --- the round trip --------------------------------------------------------

def test_a_diagnostic_is_not_an_answer(monkeypatch):
    """The empty-response diagnostic starts with a known prefix. An
    answer that starts with it is a failed turn, and the script has to
    say so instead of reporting a round trip that worked."""
    answer = smoke.EMPTY_RESPONSE_PREFIX + " reasoning=... finish_reason=length"
    _api_returns(monkeypatch, [
        _ok({"message_id": 5}),                       # sendMessage
        _ok([{"message": {"message_id": 6, "from": {"id": 42},
                           "chat": {"id": -100}, "text": answer}}]),
    ])
    report = smoke.Report()
    smoke.check_roundtrip(report, "tester", "-100", 42, "hi", timeout=5)
    assert report.rows[0][1] == smoke.FAIL
    assert "diagnostic" in report.rows[0][2]


def test_a_real_answer_passes(monkeypatch):
    _api_returns(monkeypatch, [
        _ok({"message_id": 5}),
        _ok([{"message": {"message_id": 6, "from": {"id": 42},
                           "chat": {"id": -100}, "text": "привет"}}]),
    ])
    report = smoke.Report()
    smoke.check_roundtrip(report, "tester", "-100", 42, "hi", timeout=5)
    assert report.rows[0][1] == smoke.PASS
    assert "привет" in report.rows[0][2]


def test_a_tester_that_cannot_send_fails(monkeypatch):
    """The usual reason: the tester bot is not in the chat."""
    _api_returns(monkeypatch, [
        {"ok": False, "description": "Forbidden: bot is not a member"},
    ])
    report = smoke.Report()
    smoke.check_roundtrip(report, "tester", "-100", 42, "hi", timeout=5)
    assert report.rows[0][1] == smoke.FAIL
    assert "not a member" in report.rows[0][2]


def test_no_answer_in_time_fails(monkeypatch):
    _api_returns(monkeypatch, [
        _ok({"message_id": 5}),
        _ok([]),
    ])
    report = smoke.Report()
    smoke.check_roundtrip(report, "tester", "-100", 42, "hi", timeout=1)
    assert report.rows[0][1] == smoke.FAIL


def test_getupdates_on_a_bot_with_a_webhook_fails(monkeypatch):
    """A bot with a webhook cannot use getUpdates. The script has to say
    that instead of timing out."""
    _api_returns(monkeypatch, [
        _ok({"message_id": 5}),
        {"ok": False, "description": "can't use getUpdates while webhook "
                                     "is active"},
    ])
    report = smoke.Report()
    smoke.check_roundtrip(report, "tester", "-100", 42, "hi", timeout=5)
    assert report.rows[0][1] == smoke.FAIL
    assert "getUpdates" in report.rows[0][2]


# --- the database ----------------------------------------------------------

def _db(tmp_path, content):
    path = tmp_path / "conversations.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, "
                 "chat_id INTEGER, thread_id TEXT, role TEXT, "
                 "content TEXT, created_at REAL)")
    conn.execute("INSERT INTO messages (chat_id, thread_id, role, content, "
                 "created_at) VALUES (?, ?, 'assistant', ?, 0)",
                 (-100, "main", json.dumps({"role": "assistant",
                                            "content": content})))
    conn.commit()
    conn.close()
    return str(path)


def test_a_persisted_answer_passes(tmp_path):
    path = _db(tmp_path, "the answer")
    report = smoke.Report()
    smoke.check_database(report, path, "-100")
    assert report.rows[0][1] == smoke.PASS
    assert "10 chars" in report.rows[0][2]


def test_an_empty_assistant_turn_fails(tmp_path):
    path = _db(tmp_path, "")
    report = smoke.Report()
    smoke.check_database(report, path, "-100")
    assert report.rows[0][1] == smoke.FAIL


def test_a_missing_database_fails(tmp_path):
    report = smoke.Report()
    smoke.check_database(report, str(tmp_path / "nope.db"), "-100")
    assert report.rows[0][1] == smoke.FAIL


# --- the log ---------------------------------------------------------------

def test_a_clean_log_passes(tmp_path):
    path = tmp_path / "bot.log"
    path.write_text("[handle_text] ok\n[dispatch] processed\n", encoding="utf-8")
    report = smoke.Report()
    smoke.check_log(report, str(path))
    assert report.rows[0][1] == smoke.PASS


def test_a_fatal_line_fails(tmp_path):
    path = tmp_path / "bot.log"
    path.write_text("[ERR photo] TimeoutError: too slow\n", encoding="utf-8")
    report = smoke.Report()
    smoke.check_log(report, str(path))
    assert report.rows[0][1] == smoke.FAIL
    assert "ERR photo" in report.rows[0][2]


def test_a_message_too_long_fails(tmp_path):
    """The exact symptom this whole change is about."""
    path = tmp_path / "bot.log"
    path.write_text("telegram.error.BadRequest: Message_too_long\n",
                    encoding="utf-8")
    report = smoke.Report()
    smoke.check_log(report, str(path))
    assert report.rows[0][1] == smoke.FAIL


# --- the exit code ---------------------------------------------------------

def test_the_script_exits_1_when_a_check_failed(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "argv", ["smoke_test.py", "--token", "t",
                                      "--log", str(tmp_path / "missing.log")])
    assert smoke.main([]) == 1


def test_the_script_exits_2_without_a_token(monkeypatch):
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    monkeypatch.setattr(sys, "argv", ["smoke_test.py"])
    assert smoke.main([]) == 2


def test_the_script_never_prints_the_token(monkeypatch, capsys):
    """A token in the output is a token in the operator's scrollback."""
    monkeypatch.setattr(sys, "argv", ["smoke_test.py", "--token", "SECRET"])
    monkeypatch.setattr(smoke, "api", lambda *a, **k: (False, "no network"))
    smoke.main([])
    assert "SECRET" not in capsys.readouterr().out
