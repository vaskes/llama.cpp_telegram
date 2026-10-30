"""The platform's file ceiling is chosen by the PARSED host.

GitHub's code scanning filed a ticket on 2026-10-29 (alert #5): CodeQL's
``py/incomplete-url-substring-sanitization`` on ``config.py`` -- the
check that decides whether the bot is talking to Telegram's public API
was a substring check on an unparsed URL:

    if TELEGRAM_API.startswith("https://api.telegram.org"):

That string is a HOST, and the check could not tell Telegram from a
host that merely starts with the same characters:
``https://api.telegram.org.evil.example/bot`` passes it. The endpoint
the bot actually talks to has no platform ceiling of its own, so the
wrong answer is the wrong limit on a document -- Telegram's 20 MB one
handed to a server that has none.

The host is parsed now. These tests pin the PROPERTY, not the spelling:
each of them is red on the old check and green on the new one, and a
check that reads the string instead of the host fails two of them.
"""
import config


def test_the_public_api_carries_the_platforms_own_ceiling(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_API", "https://api.telegram.org/bot")
    assert config.telegram_api_host() == "api.telegram.org"
    assert config.telegram_download_limit_bytes() == 20 * 1024 * 1024
    assert config.document_limit_bytes() == 20 * 1024 * 1024


def test_a_host_that_only_starts_like_telegram_is_not_telegram(monkeypatch):
    """This one passed the old check."""
    monkeypatch.setattr(config, "TELEGRAM_API",
                        "https://api.telegram.org.evil.example/bot")
    assert config.telegram_api_host() == "api.telegram.org.evil.example"
    assert config.telegram_download_limit_bytes() is None, (
        "a lookalike host is not Telegram, and it has no platform ceiling "
        "of its own -- handing it Telegram's 20 MB is the wrong limit")
    assert config.document_limit_bytes() == config.MAX_DOC_BYTES


def test_a_path_that_mentions_telegram_is_not_telegram(monkeypatch):
    """The host of this URL is evil.example; api.telegram.org is in the path."""
    monkeypatch.setattr(config, "TELEGRAM_API",
                        "https://evil.example/https://api.telegram.org/bot")
    assert config.telegram_api_host() == "evil.example"
    assert config.telegram_download_limit_bytes() is None


def test_a_local_bot_api_server_has_no_platform_ceiling(monkeypatch):
    """The local server is the whole reason the ceiling is conditional."""
    monkeypatch.setattr(config, "TELEGRAM_API", "http://127.0.0.1:8091/bot")
    assert config.telegram_api_host() == "127.0.0.1"
    assert config.telegram_download_limit_bytes() is None
    assert config.document_limit_bytes() == config.MAX_DOC_BYTES


def test_the_smoke_tests_mock_server_has_no_platform_ceiling(monkeypatch):
    """A mock in a smoke test must not be handed a platform limit."""
    monkeypatch.setattr(config, "TELEGRAM_API", "http://127.0.0.1:18090/bot")
    assert config.telegram_download_limit_bytes() is None


def test_no_api_at_all_has_no_platform_ceiling(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_API", "")
    assert config.telegram_api_host() == ""
    assert config.telegram_download_limit_bytes() is None


def test_the_host_is_read_case_insensitively(monkeypatch):
    """A host is case-insensitive; the URL's spelling is not."""
    monkeypatch.setattr(config, "TELEGRAM_API", "HTTPS://API.TELEGRAM.ORG/bot")
    assert config.telegram_api_host() == "api.telegram.org"
    assert config.telegram_download_limit_bytes() == 20 * 1024 * 1024


def test_a_url_that_cannot_be_parsed_has_no_host(monkeypatch):
    """A malformed URL must not take the bot down asking for the ceiling."""
    monkeypatch.setattr(config, "TELEGRAM_API", "http://[::1/bot")
    assert config.telegram_api_host() == ""
    assert config.telegram_download_limit_bytes() is None
