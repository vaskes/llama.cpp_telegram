"""A long answer must not die at the Telegram size limit.

T-FREEZE-CHUNK-1 (2026-10-24): Telegram rejects a text message over
4096 characters with `Message_too_long`, and the user sees the API
error instead of the answer. The text and voice handlers split their
answer; the photo and document paths did not, so a long answer to a
photo came back as an error.

Three things are checked here:

  * the split itself -- `chat_utils.split_text`;
  * the two paths that lacked it, driven through the real handlers with
    the engine replaced (the same seam the offline replay uses, and the
    same reason: the production host cannot take the model load);
  * the property that keeps it that way: every handler sends its
    answer through one helper, so the next path cannot forget the
    split. That last one is structural, and it is the one that matters
    -- a handler that called `send_reply` directly would pass every
    other test in this file.
"""
import ast
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

# The suite gets these from tests/conftest.py. Set them here too, so the
# module can be imported outside pytest as well.
os.environ.setdefault("BOT_TOKEN", "123456:offline-replay")
os.environ.setdefault("ALLOWED_USER_IDS", "111,222")
if not os.environ.get("CONVERSATIONS_DB"):
    _DB_DIR = tempfile.mkdtemp(prefix="answer-chunking-")
    os.environ["CONVERSATIONS_DB"] = os.path.join(_DB_DIR, "conversations.db")

BOT = Path(__file__).resolve().parent.parent / "telegram-bot"
if str(BOT) not in sys.path:
    sys.path.insert(0, str(BOT))

import access  # noqa: E402
import chat_utils  # noqa: E402
import handlers  # noqa: E402
import state  # noqa: E402

#: Telegram's own limit. A message over this comes back as
#: `Message_too_long`.
TELEGRAM_LIMIT = 4096

#: The chunk size the bot sends with.
CHUNK = chat_utils.TELEGRAM_TEXT_CHUNK

#: A prose answer long enough to need three messages: 900 lines of
#: "строка N " is about 9000 characters.
LONG_ANSWER = "".join(f"строка {i} " for i in range(900)).strip()


# --- the split ------------------------------------------------------------

def test_a_short_answer_is_one_piece():
    assert chat_utils.split_text("привет") == ["привет"]


def test_an_empty_answer_is_no_pieces():
    """Nothing to send. An empty message is rejected by Telegram."""
    assert chat_utils.split_text("") == []
    assert chat_utils.split_text("   \n  ") == []


def test_an_answer_at_the_limit_is_still_one_piece():
    text = "x" * CHUNK
    assert chat_utils.split_text(text) == [text]


def test_a_long_answer_is_split_into_pieces_that_fit():
    pieces = chat_utils.split_text(LONG_ANSWER)
    assert len(pieces) > 1
    for piece in pieces:
        assert len(piece) <= CHUNK
        assert len(piece) <= TELEGRAM_LIMIT


def test_the_split_loses_no_content():
    """The user has to read the whole answer, not the first 4000
    characters of it."""
    assert " ".join("".join(chat_utils.split_text(LONG_ANSWER)).split()) == \
        " ".join(LONG_ANSWER.split())


def test_a_whitespace_only_piece_is_dropped():
    """A chunk that says nothing carries nothing. On the path that falls
    back to a friendly message it would put that message in the middle
    of an answer."""
    text = "a" * (CHUNK - 1) + " " + "b" * 10
    pieces = chat_utils.split_text(text)
    assert len(pieces) == 2
    assert all(piece.strip() for piece in pieces)


# --- the two paths that lacked the split ---------------------------------

class _Sent:
    """What a sent message looks like to the handler."""

    def __init__(self, message_id: int):
        self.message_id = message_id
        self.deleted = False

    async def delete(self):
        self.deleted = True


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


class _FakeFile:
    def __init__(self, content: bytes):
        self.file_size = len(content)
        self._content = content

    async def download_as_bytearray(self):
        return bytearray(self._content)


class _FakeBot:
    def __init__(self, content: bytes):
        self._file = _FakeFile(content)

    async def get_file(self, file_id):
        return self._file


class _FakeMessage:
    """Records everything the bot sends, so the test can read the
    transcript the user would have seen."""

    def __init__(self, chat, user, *, caption="", photo=None, document=None):
        self.text = ""
        self.caption = caption
        self.chat = chat
        self.from_user = user
        self.message_id = 100
        self.message_thread_id = None
        self.photo = photo or []
        self.document = document
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

    @property
    def texts(self):
        """The chat messages, minus the bot's own 'thinking' placeholder.

        That one carries the Stop button and is deleted again at the end
        of the turn; it is not something the user reads.
        """
        return [m["text"] for m in self.sent
                if m["kind"] == "text" and "reply_markup" not in m["kwargs"]]


@pytest.fixture(autouse=True)
def _clean_process_state():
    """The handler keeps process-global registries: the abort events, the
    burst detector, the per-chat rate limiter. A test that leaves them
    dirty changes the next test's behaviour, so they are wiped on both
    sides."""
    def wipe():
        state._abort_events.clear()
        state._user_recent_msg.clear()
        chat_utils._send_window.clear()

    wipe()
    yield
    wipe()


async def _replay(handler, *, user_id, answer, photo=None, document=None,
                  caption=""):
    """The real handler, with the engine replaced by `answer`.

    Everything else is production code: the routing, the download, the
    persist, the storage, the sender. Only `call_llama` is replaced --
    that is the boundary between the bot and the engine, and the engine
    is the one thing the production host cannot be asked for right now.

    `user_id` is also the database key in a private chat, so every test
    gets its own. The allowlist is patched for the same reason: the
    test user is not on the production allowlist, and a rejected update
    would test nothing.
    """
    calls = []

    async def fake_call_llama(*args, **kwargs):
        calls.append(kwargs)
        return answer

    chat = _FakeChat(user_id)
    user = _FakeUser(user_id)
    message = _FakeMessage(chat, user, caption=caption, photo=photo,
                           document=document)
    update = SimpleNamespace(message=message, effective_chat=chat,
                             effective_user=user)
    # The bytes have to match the file the test says it is: a document
    # named notes.txt and typed text/plain carries text, and a binary is
    # read as a binary (the bot puts those in the sandbox).
    if document is not None and (document.mime_type or "").startswith("text/"):
        payload = b"the text of the notes: " + b"x" * 64
    else:
        payload = b"\x89PNG\r\n\x1a\n" + b"x" * 64
    context = SimpleNamespace(bot=_FakeBot(payload))
    with patch.object(handlers, "call_llama", fake_call_llama), \
            patch.object(access, "ALLOWED_USER_IDS", {user_id}):
        await handler(update, context)
    return message


async def test_a_long_answer_to_a_photo_arrives_in_several_messages():
    message = await _replay(
        handlers.handle_photo,
        user_id=9301,
        answer={"text": LONG_ANSWER},
        photo=[SimpleNamespace(file_size=100, file_id="photo-1")],
        caption="что это?")

    texts = message.texts
    assert len(texts) > 1, (
        f"the answer went out in one piece of {len(texts[0])} characters -- "
        f"Telegram rejects that with Message_too_long")
    for text in texts:
        assert len(text) <= TELEGRAM_LIMIT
    # Exactly, not "the words are all there": a chunk trimmed at the
    # boundary loses the space between the last word of one chunk and
    # the first word of the next, and the user reads "строка 372строка
    # 373" instead of two lines of text.
    assert "".join(texts) == LONG_ANSWER


async def test_a_long_answer_to_a_document_arrives_in_several_messages():
    message = await _replay(
        handlers.handle_document,
        user_id=9302,
        answer={"text": LONG_ANSWER},
        document=SimpleNamespace(file_name="notes.txt", file_size=64,
                                 file_id="doc-1", mime_type="text/plain"),
        caption="прочитай")

    texts = message.texts
    assert len(texts) > 1, (
        f"the answer went out in one piece of {len(texts[0])} characters -- "
        f"Telegram rejects that with Message_too_long")
    for text in texts:
        assert len(text) <= TELEGRAM_LIMIT
    # Exactly, not "the words are all there": a chunk trimmed at the
    # boundary loses the space between the last word of one chunk and
    # the first word of the next, and the user reads "строка 372строка
    # 373" instead of two lines of text.
    assert "".join(texts) == LONG_ANSWER


async def test_a_short_answer_to_a_photo_is_still_one_message():
    """The split must not turn every answer into a conversation."""
    message = await _replay(
        handlers.handle_photo,
        user_id=9303,
        answer={"text": "короткий ответ"},
        photo=[SimpleNamespace(file_size=100, file_id="photo-1")],
        caption="что это?")

    assert message.texts == ["короткий ответ"]


# --- the property that keeps the four paths in step ----------------------

HANDLERS = ["handle_text", "handle_photo", "handle_voice", "handle_document"]


def _handlers_tree():
    return ast.parse((BOT / "handlers.py").read_text(encoding="utf-8"))


def _function(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == name:
            return node
    raise AssertionError(f"{name} not found in handlers.py")


def _calls(fn, name):
    """Line numbers of the calls to `name` inside `fn`."""
    out = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        called = func.attr if isinstance(func, ast.Attribute) \
            else getattr(func, "id", None)
        if called == name:
            out.append(node.lineno)
    return out


def test_no_handler_sends_the_answer_on_its_own():
    """`send_reply` puts ONE message in the chat.

    A handler that calls it directly sends its whole answer in one
    message, and is one long answer away from `Message_too_long`. So
    the only place allowed to call it is the helper that splits.
    """
    tree = _handlers_tree()
    offenders = []
    for name in HANDLERS:
        fn = _function(tree, name)
        if _calls(fn, "send_reply"):
            offenders.append(f"{name} calls send_reply directly")
    assert not offenders, (
        "\n".join(offenders)
        + "\n\nThe answer goes out through _send_answer_text, which splits "
        "it. A handler that sends it on its own is one long answer away "
        "from Message_too_long.")


def test_every_handler_sends_its_answer_through_the_helper():
    """The other direction: the split has to be on every path, not just
    on the ones that happen to be long today."""
    tree = _handlers_tree()
    missing = [name for name in HANDLERS
               if not _calls(_function(tree, name), "_send_answer_text")]
    assert not missing, (
        f"{', '.join(missing)}: no _send_answer_text call -- the answer "
        f"goes out unsplit, and a long one comes back as "
        f"Message_too_long")


def test_the_helper_is_where_the_split_happens():
    """The helper must actually split. A helper that sends the answer in
    one piece would pass the two tests above.

    The split is `split_html`, not `split_text`: the answer goes out as
    HTML, so the piece has to be cut where a `<pre>` cannot be cut in
    half. The rendering is checked here too -- a helper that splits the
    plain text and then sends it as HTML would cut a code block in
    half."""
    tree = _handlers_tree()
    fn = _function(tree, "_send_answer_text")
    assert _calls(fn, "render_html"), (
        "_send_answer_text does not render the answer as HTML -- the code "
        "block would reach the chat as literal backticks")
    assert _calls(fn, "split_html"), (
        "_send_answer_text does not split the answer -- it sends it in "
        "one message")
    assert _calls(fn, "send_reply"), (
        "_send_answer_text does not send anything")


def test_the_scan_found_the_handlers():
    """A guard on the guard: if the walk stopped matching, every check
    above would pass by finding nothing."""
    tree = _handlers_tree()
    for name in HANDLERS:
        _function(tree, name)
    assert len(_calls(_function(tree, "_send_answer_text"), "split_html")) == 1
