"""What the user is told when the model comes back with nothing.

Measured on production, 2026-10-24. A user asked the bot for a zip file
with some context inside. The model wrote a long bash command to build
it; the bot refused the call; the tool loop ended; the final plain call
came back with empty content. And the message the user got was:

    [bot: search returned no useful data. For live data (flight prices,
    news, stocks), please use a direct service like Aviasales, Google
    Flights, or your browser.]

Nothing in that turn had anything to do with a search. The text is a
leftover from the search-tool era, and it is worse than useless: it
sends the user looking for a search that never happened, and it hides
the real reason the request failed.

This file pins the fallback's honesty, not its tone. Whether the bot
should answer an empty turn with a friendly line or with a diagnostic
is T-FREEZE-EMPTY-1 and the operator's call; what is not the operator's
call is a message that describes a different situation.
"""
import asyncio
import sys
from pathlib import Path

import httpx
import pytest

BOT_DIR = Path(__file__).resolve().parent.parent / "telegram-bot"
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

from llm_iteration import ToolLoop  # noqa: E402
from llm_loop_guard import LoopGuard  # noqa: E402


def _loop() -> ToolLoop:
    return ToolLoop(
        msgs=[{"role": "user", "content": "дай zip файл"}],
        max_tokens=256,
        req_tools=[],
        bot=None,
        chat_id=1,
        current_message_id=2,
        chat_type="private",
        shutdown_event=None,
        abort_event=None,
        thinking_msg=None,
        silent=True,
        use_stream=False,
        throttle=None,
        guard=LoopGuard(600),
        loop_start=0.0,
        custom_tools={},
    )


class _Response:
    status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        # A 200 with choices, but no content: the case that used to
        # produce the search diagnostic.
        return {"choices": [{"message": {"content": ""}}]}


async def test_the_empty_answer_fallback_does_not_invent_a_search(monkeypatch):
    async def fake_post(self, *args, **kwargs):
        return _Response()

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    out = await _loop()._summarize()
    assert isinstance(out, str)
    for wrong in ("Aviasales", "flight prices", "search returned",
                  "Google Flights"):
        assert wrong not in out, (
            f"the fallback still describes a search: {out!r}")
    assert "empty" in out.lower(), (
        f"the fallback has to say the answer was empty: {out!r}")


async def test_a_real_answer_is_returned_untouched(monkeypatch):
    class _WithContent(_Response):
        def json(self):
            return {"choices": [{"message": {"content": "Вот архив."}}]}

    async def fake_post(self, *args, **kwargs):
        return _WithContent()

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    out = await _loop()._summarize()
    assert out == "Вот архив."


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
