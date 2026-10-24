#!/usr/bin/env python3
"""A real round trip against a real bot -- no mocks, no fixtures.

What this replaces: the manual checklist. "Is the bot alive?" used to
be a phone, a photo, a long question and a Stop button, and it cost a
coffee break per check. This script asks the same questions over the
real Telegram API and prints a checklist.

What it can and cannot do, stated up front -- because a smoke test that
quietly skips half of what it claims to check is worse than none:

  * **Telegram does not let a bot act as a user.** A bot's own messages
    never come back as updates, so a round trip needs a SECOND bot:
    the tester sends the probe into the chat, the bot under test
    answers, and the tester reads the answer with `getUpdates`. Both
    bots have to be in the same chat -- a group, because a bot cannot
    open a private chat with another bot. Without the tester token the
    script still runs every check that does not need a user.
  * **A bot cannot press an inline button**, so the Stop button stays a
    manual click. The script says what to look for in the log and
    checks the log afterwards.
  * **Everything else is checked for real**: the token, the webhook,
    the pending queue, the answer, the document that came with it, what
    the bot remembered, and the log.

Usage:

    # the checks that need nothing but the token
    python3 scripts/smoke_test.py --token $BOT_TOKEN

    # the full round trip (needs a second bot in the same group)
    python3 scripts/smoke_test.py --token $BOT_TOKEN \
        --tester-token $TESTER_BOT_TOKEN --chat -1001234567890

    # on the host, with the bot's own database and log
    python3 scripts/smoke_test.py --token $BOT_TOKEN \
        --db /opt/telegrambot/bot/app/data/conversations.db \
        --log /opt/telegrambot/bot/app/logs/bot.log

Exit code: 0 when every check passed, 1 when one failed, 2 when the
script could not run at all (no token).

Standard library only, on purpose. This script runs on the operator's
host, where `pip install` is not an option (see the system hygiene
rule in the project memory), so it cannot depend on httpx.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request

API = "https://api.telegram.org"

#: The prefix of the diagnostic the bot sends when the model spent its
#: whole budget on reasoning. An answer that starts with it is a
#: failure of the turn, not an answer -- and it is the exact string
#: T-FREEZE-EMPTY-1 is about.
EMPTY_RESPONSE_PREFIX = "[model returned an empty response."

#: Log lines that mean something went wrong. The bot logs real failures
#: at ERROR with the `[ERR ...]` prefix (wave 5 moved them off INFO),
#: so the prefix is the cheap signal; the traceback lines catch the
#: ones that escape the handler.
FATAL_PATTERNS = (
    re.compile(r"\[ERR (photo|doc|text|voice)\]"),
    re.compile(r"Traceback \(most recent call last\)"),
    re.compile(r"Message_too_long"),
    re.compile(r"Message to send is empty"),
    re.compile(r"\[rate_limit\].*window full"),
)

#: The banner every process start writes (dispatch.py). The log file is a
#: bind mount that survives container restarts, so it accumulates the whole
#: history of the box. A smoke test that scans the whole history is a smoke
#: test that is permanently red after the first incident -- and the operator
#: cannot tell "the bot is broken now" from "the bot was broken once". So
#: the log check looks at the CURRENT process only: everything after the
#: last banner. The old lines are not hidden, they are just not this
#: deployment's news.
START_MARKER = "LlamaBot v2 started"

PASS, FAIL, WARN, SKIP = "PASS", "FAIL", "WARN", "SKIP"


class Report:
    """The checklist, and the exit code that comes out of it."""

    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    def add(self, name: str, status: str, detail: str = "") -> None:
        self.rows.append((name, status, detail))
        mark = {PASS: "✓", FAIL: "✗", WARN: "!", SKIP: "-"}[status]
        print(f"{mark} {name}" + (f": {detail}" if detail else ""))

    @property
    def failed(self) -> int:
        return sum(1 for _, status, _ in self.rows if status == FAIL)

    @property
    def warned(self) -> int:
        return sum(1 for _, status, _ in self.rows if status == WARN)


def api(token: str, method: str, payload: dict | None = None,
        timeout: float = 20.0):
    """One Telegram Bot API call. Returns (ok, result_or_error)."""
    url = f"{API}/bot{token}/{method}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            body = json.loads(raw)
        except ValueError:
            body = {}
        return False, body.get("description") or f"HTTP {exc.code}"
    except Exception as exc:  # network, DNS, TLS -- all the same here
        return False, f"{type(exc).__name__}: {exc}"
    if not body.get("ok"):
        return False, body.get("description") or "unknown API error"
    return True, body.get("result")


def check_token(report: Report, token: str) -> int | None:
    """getMe. The bot's own id, which the round trip needs later."""
    ok, result = api(token, "getMe")
    if not ok:
        report.add("token", FAIL, str(result))
        return None
    report.add("token", PASS,
               f"@{result.get('username')} id={result.get('id')}")
    return result.get("id")


def check_webhook(report: Report, token: str, expect_url: str | None) -> None:
    """getWebhookInfo: the transport, the queue, and the subscription."""
    ok, info = api(token, "getWebhookInfo")
    if not ok:
        report.add("webhook", FAIL, str(info))
        return
    url = info.get("url") or ""
    if not url:
        report.add("webhook", WARN,
                   "no webhook registered -- the bot is in polling mode")
    elif expect_url and url.rstrip("/") != expect_url.rstrip("/"):
        report.add("webhook", FAIL,
                   f"registered {url!r}, expected {expect_url!r}")
    else:
        report.add("webhook", PASS, url)

    last_error = info.get("last_error_message")
    if last_error:
        report.add("webhook errors", FAIL, last_error)
    else:
        report.add("webhook errors", PASS, "none")

    pending = info.get("pending_update_count", 0)
    if pending:
        report.add("pending updates", WARN,
                   f"{pending} waiting -- the bot is behind")
    else:
        report.add("pending updates", PASS, "0")

    allowed = info.get("allowed_updates") or []
    if "edited_message" in allowed:
        # The edit-replace feature was removed on 2026-10-24. The bot
        # ignores edited messages anyway (an explicit guard in the
        # dispatcher), so this is not a defect -- but the subscription
        # is stale, and it survives a bot restart.
        report.add("allowed_updates", WARN,
                   "edited_message is still subscribed; re-register the "
                   "webhook to drop it")
    else:
        report.add("allowed_updates", PASS, ", ".join(allowed) or "default")


def check_roundtrip(report: Report, tester_token: str, chat_id: str,
                    bot_id: int | None, probe: str, timeout: float) -> None:
    """The round trip: probe in, answer out, both over the real API.

    The tester sends, the bot under test answers, and the tester reads
    the answer with getUpdates. Only the tester polls: the bot under
    test must not be polled by anybody else, or the script would steal
    its updates.
    """
    ok, sent = api(tester_token, "sendMessage",
                   {"chat_id": chat_id, "text": probe})
    if not ok:
        report.add("round trip", FAIL,
                   f"the tester bot could not send: {sent}. Is it in the "
                   f"chat, and can it talk there?")
        return
    probe_id = sent.get("message_id")

    deadline = time.monotonic() + timeout
    answer = None
    document = None
    while time.monotonic() < deadline:
        ok, updates = api(tester_token, "getUpdates",
                          {"timeout": 20, "allowed_updates": ["message"]})
        if not ok:
            report.add("round trip", FAIL,
                       f"getUpdates failed: {updates}. A bot with a "
                       f"webhook cannot use getUpdates.")
            return
        for update in updates or []:
            message = update.get("message") or {}
            if message.get("message_id") == probe_id:
                continue
            sender = message.get("from") or {}
            if bot_id is not None and sender.get("id") != bot_id:
                continue
            if str(message.get("chat", {}).get("id")) != str(chat_id):
                continue
            if "document" in message:
                document = message["document"]
            if "text" in message:
                answer = message["text"]
                break
        if answer is not None:
            break
    if answer is None:
        report.add("round trip", FAIL,
                   f"no answer within {timeout:.0f}s"
                   + (f" (a document arrived: {document.get('file_name')})"
                      if document else ""))
        return
    if answer.startswith(EMPTY_RESPONSE_PREFIX):
        report.add("round trip", FAIL,
                   "the answer is the empty-response diagnostic, not an "
                   "answer (T-FREEZE-EMPTY-1)")
        return
    report.add("round trip", PASS,
               f"{len(answer)} chars: {answer[:80]!r}")
    if document:
        report.add("answer file", PASS,
                   f"document {document.get('file_name')} "
                   f"({document.get('file_size')} bytes)")
    else:
        report.add("answer file", SKIP,
                   "no document in the answer -- expected only for a "
                   "probe that asks for code")


def check_database(report: Report, path: str, chat_id: str | None) -> None:
    """What the bot remembered, read from its own database.

    The chat gets the prose and the history keeps the whole answer --
    two different strings on purpose (2026-10-24). This checks that the
    assistant turn exists, is not empty, and that the code the model
    wrote is still in it.
    """
    if not os.path.exists(path):
        report.add("database", FAIL, f"{path} does not exist")
        return
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        report.add("database", FAIL, f"{type(exc).__name__}: {exc}")
        return
    try:
        if chat_id:
            rows = conn.execute(
                "SELECT id, role, content, created_at FROM messages "
                "WHERE chat_id = ? AND role = 'assistant' "
                "ORDER BY id DESC LIMIT 3", (chat_id,)).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, role, content, created_at FROM messages "
                "WHERE role = 'assistant' ORDER BY id DESC LIMIT 3"
            ).fetchall()
        if not rows:
            report.add("database", WARN,
                       "no assistant turns -- has the bot answered at all?")
            return
        last = rows[0]
        try:
            payload = json.loads(last["content"])
        except ValueError:
            payload = {}
        content = payload.get("content") or ""
        if not content.strip():
            report.add("database", FAIL,
                       f"assistant turn {last['id']} is empty")
            return
        has_code = "```" in content
        report.add("database", PASS,
                   f"last assistant turn {last['id']}: {len(content)} chars"
                   + (" (contains a code block)" if has_code else ""))
        if chat_id:
            report.add("database (chat)", PASS,
                       f"{len(rows)} assistant turn(s) in chat {chat_id}")
        try:
            summarised = conn.execute(
                "SELECT count(*) FROM summaries").fetchone()[0]
            report.add("summaries", PASS,
                       f"{summarised} chunk summarie(s) in the database")
        except sqlite3.Error:
            report.add("summaries", SKIP,
                       "no summaries table -- summarise strategy off")
    finally:
        conn.close()


def current_run_lines(lines: list[str]) -> list[str]:
    """Only the lines the CURRENT process wrote.

    The log file is a bind mount: it outlives the container. Everything
    before the last startup banner belongs to a previous life of the bot.
    Keeping that history in the window means one old traceback fails every
    future smoke test, which trains the operator to ignore the one check
    that is supposed to be loud.
    """
    for index in range(len(lines) - 1, -1, -1):
        if START_MARKER in lines[index]:
            return lines[index:]
    return lines


def check_log(report: Report, path: str) -> None:
    """The log: the failures that would not show up as a failed answer."""
    if not os.path.exists(path):
        report.add("log", FAIL, f"{path} does not exist")
        return
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        lines = current_run_lines(handle.readlines())[-2000:]
    hits = []
    for line in lines:
        for pattern in FATAL_PATTERNS:
            if pattern.search(line):
                hits.append(line.strip()[:160])
                break
    if hits:
        report.add("log", FAIL,
                   f"{len(hits)} suspicious line(s) since the last start; "
                   f"last: {hits[-1]}")
    else:
        report.add("log", PASS,
                   f"{len(lines)} lines since the last start, nothing suspicious")


def manual_items() -> None:
    """What a script cannot do, said out loud instead of skipped."""
    print()
    print("manual (a script cannot do these):")
    print("  - Stop button: click it on a running turn, then check the log:")
    print("      grep -E 'abort_event set|ABORTED by user via Stop|no abort_event' bot.log")
    print("      'abort_event set' = stopped; 'no abort_event' = the turn was already over")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Real Telegram round trip against a real bot.")
    parser.add_argument("--token", default=os.environ.get("BOT_TOKEN"),
                        help="the bot under test (default: $BOT_TOKEN)")
    parser.add_argument("--tester-token",
                        default=os.environ.get("SMOKE_TESTER_BOT_TOKEN"),
                        help="a second bot in the same chat, for the "
                             "round trip (default: $SMOKE_TESTER_BOT_TOKEN)")
    parser.add_argument("--chat", default=os.environ.get("SMOKE_TEST_ID"),
                        help="the chat id for the round trip "
                             "(default: $SMOKE_TEST_CHAT)")
    parser.add_argument("--probe",
                        default="Ответь одним предложением: что ты умеешь?",
                        help="the message the tester sends")
    parser.add_argument("--timeout", type=float, default=120.0,
                        help="how long to wait for the answer")
    parser.add_argument("--db", default=None,
                        help="the bot's conversations.db, for the memory check")
    parser.add_argument("--log", default=None,
                        help="the bot's log file, for the health check")
    parser.add_argument("--expect-webhook",
                        default=os.environ.get("WEBHOOK_URL"),
                        help="the webhook URL the bot is supposed to have "
                             "(default: $WEBHOOK_URL)")
    args = parser.parse_args(argv)

    print("=" * 68)
    print("llamabot smoke test -- the real API, no mocks")
    print("=" * 68)
    print()

    if not args.token:
        print("no token: pass --token or set BOT_TOKEN", file=sys.stderr)
        return 2

    report = Report()
    bot_id = check_token(report, args.token)
    if bot_id is None:
        print()
        print("the token does not work -- nothing else can be checked")
        return 1

    check_webhook(report, args.token, args.expect_webhook)

    if args.tester_token and args.chat:
        check_roundtrip(report, args.tester_token, args.chat, bot_id,
                        args.probe, args.timeout)
    else:
        missing = []
        if not args.tester_token:
            missing.append("--tester-token")
        if not args.chat:
            missing.append("--chat")
        report.add("round trip", SKIP,
                   "needs a second bot in the same chat: missing "
                   + ", ".join(missing))

    if args.db:
        check_database(report, args.db, args.chat)
    else:
        report.add("database", SKIP, "no --db given")

    if args.log:
        check_log(report, args.log)
    else:
        report.add("log", SKIP, "no --log given")

    manual_items()

    print()
    print(f"{len(report.rows)} checks: "
          f"{sum(1 for _, s, _ in report.rows if s == PASS)} passed, "
          f"{report.failed} failed, {report.warned} warnings")
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
