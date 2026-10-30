"""The console output reaches the chat as a native Telegram code block.

The operator's words (2026-10-25): the bash output is now text in the
message, "which is exactly right, because when the bot sent bash as an
attached file it broke the eyes" -- and it would be good to have
Telegram's own formatting for code, so it reads properly.

The bot's console output is a fenced block:

    ```bash
    $ which python3
    /usr/bin/python3
    ```

Sent as plain text -- which is how the answer went out until now --
the user reads the backticks too. Telegram renders a code block natively
only when the message carries a parse mode, so the answer now goes out
as HTML and the block becomes ``<pre>``.

What is pinned here:

  * the console-output block becomes a ``<pre>``;
  * the prose the model wrote survives as it was written -- the reason
    for HTML and not MarkdownV2, which would need the model's own
    ``**bold**`` escaped into literal asterisks;
  * a block the splitter cut in half stays literal text, because a
    ``<pre>`` without its closing tag is a message Telegram rejects --
    and a rejected message means the user gets no answer at all;
  * the answer is sent with the parse mode, and the plain messages
    (the notices, the errors) are not.
"""
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("BOT_TOKEN", "123456:offline-replay")
os.environ.setdefault("ALLOWED_USER_IDS", "111,222")
if not os.environ.get("CONVERSATIONS_DB"):
    _DB_DIR = tempfile.mkdtemp(prefix="telegram-format-")
    os.environ["CONVERSATIONS_DB"] = os.path.join(_DB_DIR, "conversations.db")

BOT = Path(__file__).resolve().parent.parent / "telegram-bot"
if str(BOT) not in sys.path:
    sys.path.insert(0, str(BOT))

import chat_utils  # noqa: E402
import handlers  # noqa: E402
import telegram_format  # noqa: E402

#: The console output as the bot builds it: the command, then what it
#: printed. This is the shape the user saw in the chat on 2026-10-25.
CONSOLE = (
    "```bash\n"
    "$ which python3 2>&1; echo '---'; python3 --version 2>&1\n"
    "/usr/bin/python3\n"
    "---\n"
    "Python 3.12.3\n"
    "```\n\n"
    "Вот, что показывает песочница."
)


# --- the rendering --------------------------------------------------------

def test_the_console_block_becomes_a_pre():
    html = telegram_format.render_html(CONSOLE)
    assert "<pre>" in html
    assert "</pre>" in html
    assert "```" not in html, (
        "the fences must be gone -- otherwise the user reads them as text, "
        "which is the whole defect")


def test_the_code_inside_the_block_is_untouched():
    html = telegram_format.render_html(CONSOLE)
    assert "/usr/bin/python3" in html
    assert "Python 3.12.3" in html
    # The ampersands are escaped -- inside a <pre> they still are:
    # Telegram's HTML parser reads them as entities either way.
    assert "$ which python3 2&gt;&amp;1; echo '---'; python3 --version 2&gt;&amp;1" in html


def test_the_language_hint_is_not_part_of_the_code():
    """The fence said "bash". Telegram's highlighter does not need it,
    and the code itself does not contain it."""
    html = telegram_format.render_html(CONSOLE)
    assert "bash" not in html.split("<pre>")[1].split("</pre>")[0]


def test_the_prose_survives_as_it_was_written():
    """The reason for HTML: MarkdownV2 would eat the model's markdown."""
    html = telegram_format.render_html("The answer is **bold**, not *italic*.")
    assert "<b>bold</b>" in html, (
        "the bold the model marked is bold in the chat -- the asterisks "
        "are markup, not text")
    assert "*italic*" in html, (
        "a single asterisk is not a delimiter this renderer knows, so "
        "that text stays the way the model wrote it")


def test_the_special_characters_are_escaped():
    """Unescaped, they are a message Telegram cannot parse."""
    html = telegram_format.render_html("a < b && c > d")
    assert "&lt;" in html
    assert "&gt;" in html
    assert "&amp;" in html
    assert "a < b" not in html


def test_the_special_characters_inside_the_block_are_escaped_too():
    html = telegram_format.render_html("```\nif (a && b) { c < d }\n```")
    assert "&amp;&amp;" in html
    assert "&lt;" in html
    assert "{" in html, "braces are not special in Telegram's HTML"


def test_an_unclosed_fence_stays_literal_text():
    """The splitter cut the block. Converting half of it would produce a
    `<pre>` without its closing tag -- a message Telegram rejects."""
    cut = "```bash\n$ ls -l /workspace\n-rw-r--r-- 1 agent a"
    html = telegram_format.render_html(cut)
    assert "<pre>" not in html
    assert "```" in html


def test_a_code_block_bigger_than_a_message_stays_literal():
    """A `<pre>` that does not fit is a message Telegram rejects. The
    plain text it replaced would have been split and delivered, so it
    stays plain."""
    huge = "```bash\n" + ("$ echo hello\n" * 1000) + "```"
    html = telegram_format.render_html(huge)
    assert "<pre>" not in html
    assert "```" in html


def test_a_huge_code_block_still_arrives_in_pieces_that_fit():
    huge = "```bash\n" + ("$ echo hello\n" * 1000) + "```\n\n" + "prose"
    html = telegram_format.render_html(huge)
    pieces = telegram_format.split_html(html, chat_utils.TELEGRAM_TEXT_CHUNK)
    assert len(pieces) > 1
    for piece in pieces:
        assert len(piece) <= chat_utils.TELEGRAM_TEXT_CHUNK, (
            f"a piece of {len(piece)} characters does not fit")


def test_an_empty_answer_is_empty():
    assert telegram_format.render_html("") == ""


# --- the split ------------------------------------------------------------

def test_the_split_never_cuts_a_pre_in_half():
    """A cut inside a `<pre>` is a message Telegram rejects.

    The block has to be one that is actually converted -- a block bigger
    than a message stays literal, and a test on that block would pass
    with no `<pre>` in it at all: green, and proving nothing. And the
    prose has to push the block across the limit, or the cut lands
    after it and nothing is exercised either.
    """
    body = "$ echo hello\n" * 200          # about 2600 characters
    html = telegram_format.render_html(
        "prose " * 500 + "```bash\n" + body + "```\n\n" + "x" * 5000
    )
    assert "<pre>" in html, (
        "the block was not converted -- the test would pass without "
        "checking anything")
    pieces = telegram_format.split_html(html, chat_utils.TELEGRAM_TEXT_CHUNK)
    assert len(pieces) > 1
    for piece in pieces:
        assert piece.count("<pre>") == piece.count("</pre>"), (
            f"a chunk has an unbalanced <pre>: {piece[:80]!r}")
    assert "".join(pieces) == html, "the split must not lose or add text"


def test_a_cut_inside_a_pre_keeps_the_block_whole():
    """The concrete case: the prose pushes the block over the limit.

    The block is longer than what is left of the first message, so the
    cut goes *before* it -- the block opens the next message, whole."""
    body = "$ echo hello\n" * 200
    html = telegram_format.render_html(
        "prose " * 500 + "```bash\n" + body + "```"
    )
    pieces = telegram_format.split_html(html, chat_utils.TELEGRAM_TEXT_CHUNK)
    assert len(pieces) > 1, "the case needs a cut inside the block"
    for piece in pieces:
        assert piece.count("<pre>") == piece.count("</pre>"), (
            f"a chunk has an unbalanced <pre>: {piece[:80]!r}")
        assert len(piece) <= chat_utils.TELEGRAM_TEXT_CHUNK, (
            f"a chunk of {len(piece)} characters does not fit Telegram")
    whole = [p for p in pieces if "<pre>" in p]
    assert len(whole) == 1, "the block has to be delivered whole"
    assert whole[0].startswith("<pre>"), (
        "the block opens the message that carries it")


def test_every_piece_fits_telegram_and_holds_whole_blocks():
    """The property, over the shapes an answer can have.

    A piece longer than Telegram's limit is a rejected message, and a
    piece with half a `<pre>` is a rejected message. Both lose the
    answer, so both are checked on every shape."""
    block = "$ echo hello\n" * 200          # about 2600 characters
    shapes = [
        "prose " * 3000,
        "prose " * 500 + "```bash\n" + block + "```" + "tail " * 2000,
        "```bash\n" + block + "```" + "tail " * 2000,
        "prose " * 500 + "```bash\n" + block + "```",
        "```bash\n" + "$ echo hi\n" * 10 + "```" + "tail " * 3000,
    ]
    for shape in shapes:
        html = telegram_format.render_html(shape)
        pieces = telegram_format.split_html(html, chat_utils.TELEGRAM_TEXT_CHUNK)
        for piece in pieces:
            assert len(piece) <= chat_utils.TELEGRAM_TEXT_CHUNK, (
                f"a piece of {len(piece)} characters does not fit Telegram")
            assert piece.count("<pre>") == piece.count("</pre>"), (
                f"a piece has an unbalanced <pre>: {piece[:80]!r}")
        assert "".join(pieces) == html, "the split must not lose any text"


def test_a_short_message_is_one_piece():
    html = telegram_format.render_html("просто текст")
    assert telegram_format.split_html(html, 4000) == ["просто текст"]


def test_the_console_block_survives_the_split_in_one_piece():
    """The real trace: the bot's own block, capped at 2000 characters of
    output, plus the fences. It has to fit in one message -- the code
    block is at the front of the answer, and a `<pre>` cut in half is a
    rejected message."""
    from llm_result import MAX_TRANSCRIPT_OUTPUT_CHARS
    trace = (
        "```bash\n"
        + "$ echo " + "x" * 50 + "\n"
        + "y" * MAX_TRANSCRIPT_OUTPUT_CHARS
        + "\n```"
    )
    html = telegram_format.render_html(trace + "\n\n" + "prose")
    pieces = telegram_format.split_html(html, chat_utils.TELEGRAM_TEXT_CHUNK)
    assert len(pieces) == 1, (
        "the console block must fit in one message; a split `<pre>` is a "
        "message Telegram rejects")
    assert "<pre>" in pieces[0] and "</pre>" in pieces[0]


# --- the send path --------------------------------------------------------

class _FakeChat:
    def __init__(self, chat_id=42):
        self.id = chat_id
        self.type = "private"
        self.is_forum = False
        self.actions = []

    async def send_action(self, action=None, **kwargs):
        self.actions.append(action)


class _FakeMessage:
    def __init__(self, chat):
        self.chat = chat
        self.message_id = 100
        self.message_thread_id = None
        self.sent = []

    async def reply_text(self, text, **kwargs):
        self.sent.append({"text": text, "kwargs": kwargs})
        return SimpleNamespace(message_id=1000)


@pytest.fixture
def update():
    chat = _FakeChat()
    message = _FakeMessage(chat)
    return SimpleNamespace(message=message, effective_chat=chat,
                           effective_user=SimpleNamespace(id=42))


async def test_the_answer_goes_out_with_the_parse_mode(update):
    await handlers._send_answer_text(update, CONSOLE)
    sent = update.message.sent
    assert len(sent) == 1
    assert sent[0]["kwargs"].get("parse_mode") == "HTML", (
        "without a parse mode Telegram shows the code block as plain text")


async def test_the_code_block_is_a_pre_in_the_message_the_user_gets(update):
    await handlers._send_answer_text(update, CONSOLE)
    text = update.message.sent[0]["text"]
    assert "<pre>" in text
    assert "```" not in text


async def test_the_prose_in_the_message_is_the_models_own(update):
    await handlers._send_answer_text(update, CONSOLE)
    text = update.message.sent[0]["text"]
    assert "Вот, что показывает песочница." in text


async def test_a_long_answer_is_still_split(update):
    long_answer = "```bash\n$ echo hi\nhi\n```\n\n" + "строка " * 2000
    await handlers._send_answer_text(update, long_answer)
    sent = update.message.sent
    assert len(sent) > 1, "a long answer must still be split"
    for message in sent:
        assert message["text"].count("<pre>") == message["text"].count("</pre>")
        assert len(message["text"]) <= chat_utils.TELEGRAM_TEXT_CHUNK + 32, (
            "a chunk must fit Telegram's limit even after the HTML")


async def test_the_plain_messages_keep_no_parse_mode(update):
    """The notices and the errors are written for a person: they have no
    markup to render, and a parse mode would only give them a way to
    fail."""
    await handlers.send_reply(update, "The bot is overloaded, try again later.")
    sent = update.message.sent
    assert len(sent) == 1
    assert sent[0]["kwargs"].get("parse_mode") is None
