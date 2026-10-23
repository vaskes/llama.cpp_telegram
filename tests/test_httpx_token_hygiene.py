"""The bot token must not reach the operator log.

httpx logs every Telegram API call at INFO, and the URL it logs carries
the bot token in the path: 'HTTP Request: POST
https://api.telegram.org/bot<TOKEN>/sendMessage'. With the root logger
at INFO (logconf.configure), that line went to stdout, which the
container tees into /var/log/bot/bot.log -- 694 occurrences of the
token in one operator-readable file. Anyone who can read that file can
act as the bot.

httpx itself only logs at DEBUG and INFO (checked in the 0.27 source:
_client.py logs the request line at INFO, _config.py at DEBUG, and
httpcore only at DEBUG), so raising its threshold to WARNING costs no
diagnostic that existed in the first place.

The mutation that must fail this test: dropping the setLevel line.
"""
import logging
import sys
from pathlib import Path

BOT = Path(__file__).resolve().parent.parent / "telegram-bot"
if str(BOT) not in sys.path:
    sys.path.insert(0, str(BOT))

import logconf  # noqa: E402


def test_httpx_info_records_carry_no_token(caplog, capsys):
    logconf.get_logger("probe")
    logger = logging.getLogger("httpx")

    assert logger.level > logging.INFO, (
        f"the httpx logger is still at level {logger.level}, so its INFO "
        f"lines -- which carry the bot token in the URL -- reach the log"
    )

    logger.info(
        "HTTP Request: POST "
        "https://api.telegram.org/bot123456:SECRET-TOKEN/sendMessage"
    )
    out = capsys.readouterr()
    assert "SECRET-TOKEN" not in out.out, (
        f"the token reached stdout: {out.out!r}")
    assert "SECRET-TOKEN" not in caplog.text, (
        f"the token reached the log: {caplog.text!r}")


def test_httpx_warnings_still_reach_the_log(caplog):
    """Raising the threshold must not silence httpx entirely."""
    logconf.get_logger("probe")
    logging.getLogger("httpx").warning("something went wrong")
    assert "something went wrong" in caplog.text, (
        f"httpx warnings no longer reach the log: {caplog.text!r}")
