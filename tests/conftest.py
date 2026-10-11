# tests/conftest.py
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_TELEGRAM_BOT = os.path.join(_HERE, "..", "telegram-bot")
if _TELEGRAM_BOT not in sys.path:
    sys.path.insert(0, _TELEGRAM_BOT)

os.environ.setdefault("BOT_TOKEN", "test_bot_token_for_ci_only")
os.environ.setdefault("ALLOWED_USER_IDS", "111,222")
os.environ.setdefault("CONVERSATIONS_DB", "/tmp/pytest_conversations.db")
