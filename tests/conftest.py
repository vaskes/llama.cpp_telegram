# tests/conftest.py
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_TELEGRAM_BOT = os.path.join(_HERE, "..", "telegram-bot")
if _TELEGRAM_BOT not in sys.path:
    sys.path.insert(0, _TELEGRAM_BOT)

os.environ.setdefault("BOT_TOKEN", "test_bot_token_for_ci_only")
os.environ.setdefault("ALLOWED_USER_IDS", "111,222")
os.environ.setdefault("CONVERSATIONS_DB", "/tmp/pytest_conversations.db")


@pytest.fixture(autouse=True)
def _reset_module_state():
    """Reset bot.store and storage._store_instance before AND after each
    test in this directory. The bot module captures the Storage at
    import time; storage lazily creates one. If a previous test
    caused one of these to be set, subsequent tests see a stale
    instance and assert the wrong thing.

    This is autouse so all 36 tests in tests/ get the reset without
    having to declare the fixture.
    """
    import storage as _st
    import bot as _bot
    _st._store_instance = None
    if hasattr(_bot, "store"):
        _bot.store = _st.get_store()  # set before each test
    yield
    _st._store_instance = None
    if hasattr(_bot, "store"):
        _bot.store = None
