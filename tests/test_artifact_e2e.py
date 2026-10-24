"""The answer-files feature, end to end, without a production host.

Why this file exists (2026-10-24): the production host runs on a
generator, and any request to the model overloads it -- the operator
measured that, so the live check cannot be done on the real bot right
now. "We will have to imagine it for you without prod."

So this file is that check, done offline. The real text handler, the
real parser, the real sender, the real database. The only thing
replaced is the engine: `call_llama` returns the answer shapes that
were MEASURED on the live engine on 2026-10-24, not shapes somebody
thought of afterwards.

What it covers that the unit tests cannot:

  * the seam. `test_llm_artifact.py` calls `extract_files` directly;
    `test_llm_files.py` calls `send_files` directly. Neither can see
    whether the handler wires the two together -- and the history of
    this repository is a list of features that were in the code and not
    in the path the production turn actually takes.
  * the two different strings. The chat gets the prose; the database
    keeps the whole answer, code blocks included, because the model
    wrote that code and the next question is about the file it just
    sent.
  * the rule that a file goes out even when there is no prose at all.
  * that the three things which look like files are not files: the
    bot's own tool trace, the reasoning tail of a diagnostic, and a
    fence that was cut off.

Every shape below came out of the live engine. A shape nobody measured
is a shape nobody should ship a test for.

Run it as a script to see the transcripts -- what the user would have
read, and what the bot would have kept:

    python tests/test_artifact_e2e.py
"""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

# The suite gets these from tests/conftest.py. Set them here too, so the
# module can be run as a script (the demo at the bottom) without pytest.
os.environ.setdefault("BOT_TOKEN", "123456:offline-replay")
os.environ.setdefault("ALLOWED_USER_IDS", "111,222")
if not os.environ.get("CONVERSATIONS_DB"):
    _OFFLINE_DB_DIR = tempfile.mkdtemp(prefix="offline-replay-")
    os.environ["CONVERSATIONS_DB"] = os.path.join(
        _OFFLINE_DB_DIR, "conversations.db")

BOT = Path(__file__).resolve().parent.parent / "telegram-bot"
if str(BOT) not in sys.path:
    sys.path.insert(0, str(BOT))

import access  # noqa: E402
import chat_utils  # noqa: E402
import handlers  # noqa: E402
import state  # noqa: E402
import storage  # noqa: E402


# --- the answers, as the live engine produced them (2026-10-24) -----------

#: The traits dump. The model prefixed it with "[llm] " and put it in a
#: ```json fence with no name anywhere.
TRAITS_JSON = ('[llm] ```json\n{\n  "name": "Vaskes",\n'
               '  "traits": ["direct", "curious"]\n}\n```')

#: The site in three files, each under a heading.
THREE_FILES = ('## index.html\n\n```html\n<html></html>\n```\n\n'
               '## style.css\n\n```css\nbody { color: red; }\n```\n\n'
               '## app.js\n\n```js\nconsole.log(1);\n```\n\n'
               'Как запустить: открой index.html в браузере.')

#: A raw HTML page -- 425 lines in the live run, six here. No fence.
RAW_HTML = ("<!doctype html>\n<html>\n<head><title>x</title></head>\n"
            "<body>hi</body>\n</html>")

#: Prose, an html fence, and the name in the tail.
NAMED_HTML = ('Готово, вот страница.\n\n```html\n<!doctype html>\n'
              '<html><body>hi</body></html>\n```\n\n'
              'Сохрани как `index.html` и открой в браузере.')

#: The bot's own executed-tool trace, as `unwrap_answer` puts it in
#: front of the answer: a ```bash fence whose lines all start with "$ ".
TOOL_TRACE_ANSWER = {
    "text": "Готово, всё посмотрел.",
    "executed_tools": [{"name": "bash_command", "args": {"command": "ls -la"}}],
}

#: The diagnostic the bot sends when the model spent its whole budget on
#: reasoning. `call_llama` returns a STRING for this one, and the string
#: carries the reasoning tail. It is not a deliverable.
REASONING_DIAGNOSTIC = (
    "[model returned an empty response. reasoning=the model kept thinking "
    "about the schema until the budget ran out and never wrote an answer] "
    "finish_reason=length"
)


# --- the fakes ------------------------------------------------------------

class _Sent:
    """What a sent message looks like to the handler: an id, and the two
    methods the handlers call on it."""

    def __init__(self, message_id: int):
        self.message_id = message_id
        self.deleted = False
        self.edited = None

    async def delete(self):
        self.deleted = True

    async def edit_text(self, text, **kwargs):
        self.edited = text


class _FakeChat:
    def __init__(self, chat_id, chat_type="private"):
        self.id = chat_id
        self.type = chat_type
        self.is_forum = chat_type == "supergroup"
        self.actions = []

    async def send_action(self, action=None, **kwargs):
        self.actions.append(action)


class _FakeUser:
    def __init__(self, user_id, username="tester"):
        self.id = user_id
        self.username = username
        self.first_name = "Test"
        self.last_name = "User"


class _FakeMessage:
    """Records everything the bot sends, so the test can read the
    transcript the user would have seen."""

    def __init__(self, text, chat, user, message_id=100, thread_id=None):
        self.text = text
        self.chat = chat
        self.from_user = user
        self.message_id = message_id
        self.message_thread_id = thread_id
        self.sent = []
        self._next_id = 1000

    async def reply_text(self, text, **kwargs):
        self.sent.append({"kind": "text", "text": text, "kwargs": kwargs})
        sent = _Sent(self._next_id)
        self._next_id += 1
        return sent

    async def reply_document(self, document=None, caption=None, **kwargs):
        self.sent.append({"kind": "document", "document": document,
                          "caption": caption, "kwargs": kwargs})
        sent = _Sent(self._next_id)
        self._next_id += 1
        return sent


class _Transcript:
    """What the user would have seen, and what the bot would have kept."""

    def __init__(self, message, calls, chat_id, thread_id):
        self.message = message
        self.calls = calls
        self.chat_id = chat_id
        self.thread_id = thread_id

    @property
    def texts(self):
        """The chat messages, minus the bot's own 'thinking' placeholder.

        That one carries the Stop button and is deleted again at the end
        of the turn; it is not something the user reads.
        """
        return [m["text"] for m in self.message.sent
                if m["kind"] == "text" and "reply_markup" not in m["kwargs"]]

    @property
    def documents(self):
        """(filename, content, caption, kwargs) per document."""
        return [(m["document"].filename,
                 m["document"].input_file_content.decode("utf-8"),
                 m["caption"], m["kwargs"])
                for m in self.message.sent if m["kind"] == "document"]

    @property
    def assistant_answers(self):
        """What the bot remembered -- read from the database, not from
        the message it sent."""
        out = []
        for row in storage.get_store().get_messages(self.chat_id,
                                                     self.thread_id):
            payload = json.loads(row["content"])
            if payload.get("role") == "assistant":
                out.append(payload.get("content"))
        return out


@pytest.fixture(autouse=True)
def _clean_process_state():
    """The handler keeps process-global registries: the abort events, the
    burst detector, the per-chat rate limiter. A test that leaves them
    dirty changes the next test's behaviour, so they are wiped on both
    sides. The database is not wiped -- every test has its own user id,
    and a private chat's id IS the user's id, so the rows of one test
    cannot be mistaken for the rows of another."""
    def wipe():
        state._abort_events.clear()
        state._user_recent_msg.clear()
        chat_utils._send_window.clear()

    wipe()
    yield
    wipe()


async def _replay(answer, *, user_id=9111, chat_id=None,
                  user_text="сделай файл", thread_id=None,
                  chat_type="private"):
    """The real text handler, with the engine replaced by `answer`.

    Everything else is production code: the parser, the sender, the
    persist, the storage. Only `call_llama` is replaced -- that is the
    boundary between the bot and the engine, and the engine is the one
    thing the production host cannot be asked for right now.

    `user_id` is also the database key in a private chat, so every test
    gets its own. The allowlist is patched for the same reason: the
    test user is not on the production allowlist, and a rejected update
    would test nothing.
    """
    calls = []

    async def fake_call_llama(*args, **kwargs):
        calls.append(kwargs)
        return answer

    chat = _FakeChat(chat_id if chat_id is not None else user_id, chat_type)
    user = _FakeUser(user_id)
    message = _FakeMessage(user_text, chat, user, thread_id=thread_id)
    update = SimpleNamespace(message=message, effective_chat=chat,
                             effective_user=user)
    with patch.object(handlers, "call_llama", fake_call_llama), \
            patch.object(access, "ALLOWED_USER_IDS", {user_id}):
        await handlers.handle_text(update, SimpleNamespace(bot=None))
    if chat_type == "private":
        db_chat_id, db_thread = user_id, "main"
    else:
        db_chat_id = chat.id
        db_thread = str(thread_id) if thread_id is not None else "general"
    return _Transcript(message, calls, db_chat_id, db_thread)


# --- the feature, end to end ----------------------------------------------

async def test_a_json_answer_arrives_as_a_document_and_the_chat_gets_the_prose():
    """The traits dump: the fence does not start a line, because the
    model put it right after "[llm] "."""
    tr = await _replay({"text": TRAITS_JSON}, user_id=9101)

    assert [name for name, *_ in tr.documents] == ["answer.json"]
    assert '"traits"' in tr.documents[0][1]

    assert tr.texts, "the chat got nothing at all"
    for text in tr.texts:
        assert "```" not in text
        assert '"traits"' not in text


async def test_three_files_in_one_answer_arrive_as_three_documents():
    tr = await _replay({"text": THREE_FILES}, user_id=9102)

    assert [name for name, *_ in tr.documents] == ["index.html",
                                                   "style.css",
                                                   "app.js"]
    assert "body { color: red; }" in tr.documents[1][1]

    assert tr.texts, "the chat got nothing at all"
    for text in tr.texts:
        assert "body { color: red; }" not in text
        assert "console.log(1);" not in text
    assert "Как запустить" in tr.texts[0]


async def test_a_raw_document_with_no_prose_still_arrives_as_a_file():
    """The answer is nothing but a document. There is no prose to send,
    and the file still has to go out -- otherwise the user gets nothing
    at all."""
    tr = await _replay({"text": RAW_HTML}, user_id=9103)

    assert [name for name, *_ in tr.documents] == ["answer.html"]
    assert tr.documents[0][1].startswith("<!doctype html>")
    assert tr.texts == [], "the raw document was sent as text as well"


async def test_the_bot_remembers_the_code_it_just_sent():
    """Two different strings since 2026-10-24.

    The chat gets the prose. The database keeps the whole answer, code
    blocks included: the model wrote that code, and the next question is
    about the file it just sent. Persisting only the prose would make
    the model forget what it just delivered -- with every test on the
    delivery path still green.
    """
    tr = await _replay({"text": THREE_FILES}, user_id=9104)

    remembered = tr.assistant_answers
    assert remembered, "the assistant turn was not persisted at all"
    assert "```html" in remembered[0]
    assert "console.log(1);" in remembered[0]

    for text in tr.texts:
        assert "console.log(1);" not in text


async def test_the_diagnostic_with_a_reasoning_tail_carries_no_file():
    """The model spent its budget on reasoning and wrote no answer. The
    reasoning tail is the model thinking out loud about a failure; it is
    not a deliverable, and it must not become a document.

    What the chat gets is the diagnostic itself, not
    EMPTY_RESPONSE_USER_FALLBACK: the retry branch assigns the fallback
    to `bot_response`, and then `_persist_for_reply` returns the
    answer's prose over it. That is a separate defect (T-FREEZE-EMPTY-1
    in docs/TODO.md), and it is recorded rather than quietly fixed here
    -- the fallback is what the user should see, or the diagnostic is,
    and that is the operator's call.
    """
    tr = await _replay(REASONING_DIAGNOSTIC, user_id=9105)

    assert tr.documents == [], "the reasoning tail became a file"
    assert tr.texts == [REASONING_DIAGNOSTIC]
    assert tr.assistant_answers == [REASONING_DIAGNOSTIC]


async def test_the_bots_own_tool_trace_is_not_a_file():
    """The trace of what the model ran is a log, not a deliverable."""
    tr = await _replay(TOOL_TRACE_ANSWER, user_id=9106)

    assert tr.documents == []
    assert "$ ls -la" in tr.texts[0]


async def test_the_document_carries_the_topic_id():
    """A document sent from a forum topic has to land in that topic.
    PTB's reply_document does not carry the topic id through on its own,
    which is why the sender builds the kwargs itself."""
    tr = await _replay({"text": NAMED_HTML}, user_id=9107, chat_id=-9007,
                       thread_id=42, chat_type="supergroup")

    assert [name for name, *_ in tr.documents] == ["index.html"]
    assert tr.documents[0][3].get("message_thread_id") == 42


async def test_a_plain_answer_is_untouched():
    """A user who asked a question must not get a different answer
    because a feature was switched on next to it."""
    tr = await _replay({"text": "Просто текст. И `code.span` тоже не файл."},
                       user_id=9108)

    assert tr.documents == []
    assert tr.texts == ["Просто текст. И `code.span` тоже не файл."]


# --- the demo --------------------------------------------------------------

_SHAPES = [
    ("the traits dump", {"text": TRAITS_JSON}),
    ("three files in one answer", {"text": THREE_FILES}),
    ("a raw document, no prose", {"text": RAW_HTML}),
    ("the bot's own tool trace", TOOL_TRACE_ANSWER),
    ("the reasoning diagnostic", REASONING_DIAGNOSTIC),
]


def _demo():
    """Print the transcript of every measured shape.

    This is the "imagine it without prod" part: the real handler runs,
    the real parser runs, and what would have been sent to the user is
    printed.
    """
    user_id = 9200
    for name, answer in _SHAPES:
        user_id += 1
        tr = asyncio.run(_replay(answer, user_id=user_id))
        print(f"\n=== {name} ===")
        print("chat:")
        for text in tr.texts:
            print(f"  {text!r}")
        print("documents:")
        for filename, content, caption, _ in tr.documents:
            print(f"  {filename} (caption {caption!r})")
            print(f"    {content[:120]!r}")
        remembered = tr.assistant_answers
        print(f"remembered: {remembered[0][:120]!r}" if remembered
              else "remembered: (nothing)")


if __name__ == "__main__":
    _demo()
