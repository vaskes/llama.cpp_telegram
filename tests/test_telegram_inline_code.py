"""The inline code the model wrote reaches the chat as native code.

The operator's words (2026-10-30): "хочу вот такое же форматирование в
телеге" -- the answer should look the way his own client renders it,
where a word or a path in single backticks is monospace. His example
was his own review of a harness:

    Харнес `evals` у него написан: P@K, R@K, MRR, nDCG@K, 79
    размеченных запросов в фикстурах. А `evals/baselines/` -- **пусто**,
    один `.gitkeep`.

The answer goes out as HTML, so a span of single backticks becomes a
``<code>`` element, which Telegram draws monospace.

What is pinned here:

  * a span of single backticks becomes a ``<code>`` element, and the
    prose around it survives as the model wrote it;
  * a run of **two or more** backticks is not a delimiter -- it is a
    fence, or the way to write a backtick inside a span;
  * an **unmatched** backtick stays literal, and so does a pair that
    runs over a line break: a ``<code>`` without its closing tag is a
    message Telegram rejects, and a rejected message means the user
    gets no answer at all;
  * the code inside a fenced block is not converted again -- the block
    is already code;
  * the splitter never cuts a ``<code>`` in half;
  * **the reasoning message is not formatted at all.** The operator's
    rule, categorical: the thinking bubble is the model's scratchpad,
    not the answer. The notices and the errors are the third artifact:
    text written for a person, with no markup to render.
"""
import ast
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("BOT_TOKEN", "123456:offline-replay")
os.environ.setdefault("ALLOWED_USER_IDS", "111,222")
if not os.environ.get("CONVERSATIONS_DB"):
    _DB_DIR = tempfile.mkdtemp(prefix="telegram-inline-code-")
    os.environ["CONVERSATIONS_DB"] = os.path.join(_DB_DIR, "conversations.db")

BOT = Path(__file__).resolve().parent.parent / "telegram-bot"
if str(BOT) not in sys.path:
    sys.path.insert(0, str(BOT))

import chat_utils  # noqa: E402
import handlers  # noqa: E402
import llm_thinking  # noqa: E402
import telegram_format  # noqa: E402

#: The operator's own sentence, with the spans he wants monospace.
OPERATOR = (
    "Теперь почему я бы это не поставила прод. Харнес `evals` у него "
    "написан: P@K, R@K, MRR, nDCG@K, 79 размеченных запросов в фикстурах. "
    "А `evals/baselines/` -- **пусто**, один `.gitkeep`."
)


# --- the rendering --------------------------------------------------------

def test_an_inline_code_span_becomes_a_code_element():
    html = telegram_format.render_html("Харнес `evals` у него написан.")
    assert "<code>evals</code>" in html, (
        "the span has to become a code element -- the user asked for the "
        "answer to look the way his own client renders it")
    assert "`evals`" not in html, "the backticks are markup, not text"


def test_the_operator_own_sentence_keeps_its_prose_and_marks_its_spans():
    html = telegram_format.render_html(OPERATOR)
    for span in ("evals", "evals/baselines/", ".gitkeep"):
        assert f"<code>{span}</code>" in html
    assert "79 размеченных запросов в фикстурах" in html, (
        "the prose between the spans has to survive as it was written")
    assert "**пусто**" in html, (
        "the model's own markdown is not the bot's business -- HTML, not "
        "MarkdownV2, is what keeps it readable")


def test_the_code_inside_the_span_is_escaped():
    """Unescaped, it is a message Telegram cannot parse."""
    html = telegram_format.render_html("the `a && b < c` case")
    assert "<code>a &amp;&amp; b &lt; c</code>" in html


def test_a_run_of_two_backticks_is_not_a_delimiter():
    """Two backticks are the way to write a backtick inside a span, and
    three are a fence. Neither is a delimiter here."""
    html = telegram_format.render_html("a ``b`` c")
    assert "<code>" not in html
    assert "``b``" in html


def test_a_run_of_three_backticks_stays_literal():
    """An unclosed fence: the splitter cut the block. The run is not a
    delimiter, or the block would be turned into a span."""
    html = telegram_format.render_html("```bash\n$ ls -l /workspace")
    assert "<code>" not in html
    assert "```" in html


def test_an_unmatched_backtick_stays_literal():
    """A `<code>` without its closing tag is a message Telegram rejects."""
    html = telegram_format.render_html("the ` symbol is a backtick")
    assert "<code>" not in html
    assert "` symbol" in html


def test_a_pair_that_runs_over_a_line_break_stays_literal():
    html = telegram_format.render_html("`a\nb`")
    assert "<code>" not in html
    assert "`a\nb`" in html


def test_the_code_inside_a_fenced_block_is_not_converted_again():
    """The block is already code. Its backticks are part of the code."""
    html = telegram_format.render_html("```bash\nls `x`\n```")
    assert "<pre>" in html
    assert "<code>" not in html
    assert "ls `x`" in html


def test_an_empty_answer_is_empty():
    assert telegram_format.render_html("") == ""


# --- the split ------------------------------------------------------------

def test_the_split_never_cuts_a_code_in_half():
    """A cut inside a `<code>` is a message Telegram rejects.

    The span has to straddle the limit -- a span that ends before it
    would leave the cut after the element and exercise nothing.
    """
    prose = "x" * (chat_utils.TELEGRAM_TEXT_CHUNK - 10)
    html = telegram_format.render_html(prose + "`abcdefghij`" + "tail " * 500)
    assert "<code>" in html, (
        "the span was not converted -- the test would pass without "
        "checking anything")
    pieces = telegram_format.split_html(html, chat_utils.TELEGRAM_TEXT_CHUNK)
    assert len(pieces) > 1, "the case needs a cut inside the span"
    for piece in pieces:
        assert piece.count("<code>") == piece.count("</code>"), (
            f"a chunk has an unbalanced <code>: {piece[-80:]!r}")
        assert len(piece) <= chat_utils.TELEGRAM_TEXT_CHUNK, (
            f"a chunk of {len(piece)} characters does not fit Telegram")
    whole = [p for p in pieces if "<code>" in p]
    assert len(whole) == 1, "the span has to be delivered whole"
    assert whole[0].startswith("<code>"), (
        "the span opens the message that carries it")


def test_every_piece_fits_telegram_and_holds_whole_spans():
    """The property, over the shapes an answer can have."""
    shapes = [
        "prose " * 3000,
        "prose " * 500 + "`a path.py`" + "tail " * 2000,
        "`a path.py`" + "tail " * 3000,
        "prose " * 900 + "`a path.py`" + "tail " * 900,
        "```bash\n$ echo hi\n```" + "`a path.py`" + "tail " * 3000,
        "`a path.py`" * 500,
    ]
    for shape in shapes:
        html = telegram_format.render_html(shape)
        pieces = telegram_format.split_html(html, chat_utils.TELEGRAM_TEXT_CHUNK)
        for piece in pieces:
            assert len(piece) <= chat_utils.TELEGRAM_TEXT_CHUNK, (
                f"a piece of {len(piece)} characters does not fit Telegram")
            assert piece.count("<code>") == piece.count("</code>"), (
                f"a piece has an unbalanced <code>: {piece[-80:]!r}")
            assert piece.count("<pre>") == piece.count("</pre>"), (
                f"a piece has an unbalanced <pre>: {piece[-80:]!r}")
        assert "".join(pieces) == html, "the split must not lose any text"


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
        self.edits = []
        self.from_user = SimpleNamespace(id=42, first_name="Vasisualy",
                                         last_name="Lohankin", username=None)

    async def reply_text(self, text, **kwargs):
        self.sent.append({"text": text, "kwargs": kwargs})
        return SimpleNamespace(message_id=1000)

    async def edit_text(self, text, **kwargs):
        self.edits.append({"text": text, "kwargs": kwargs})
        return SimpleNamespace(message_id=1000)


@pytest.fixture
def update():
    chat = _FakeChat()
    message = _FakeMessage(chat)
    return SimpleNamespace(message=message, effective_chat=chat,
                           effective_user=SimpleNamespace(id=42))


async def test_the_answer_goes_out_with_the_code_element(update):
    await handlers._send_answer_text(update, OPERATOR)
    text = update.message.sent[0]["text"]
    assert "<code>evals</code>" in text
    assert "<code>evals/baselines/</code>" in text
    assert update.message.sent[0]["kwargs"].get("parse_mode") == "HTML"


# --- the reasoning is not formatted ---------------------------------------

async def test_the_thinking_bubble_carries_no_parse_mode(update):
    """The operator's rule, categorical: the reasoning message is not
    formatted. It is the model's scratchpad, edited in place -- and a
    parse mode on it would render whatever the model wrote inside it
    as markup."""
    await handlers._open_thinking(update, label="test")
    sent = update.message.sent
    assert len(sent) == 1
    assert sent[0]["kwargs"].get("parse_mode") is None, (
        "the thinking bubble must not carry a parse mode")


async def test_the_reasoning_keeps_its_backticks(update):
    """The reasoning goes out as the model wrote it: the backticks stay
    backticks, and no `<code>` element appears."""
    fake = _FakeMessage(update.effective_chat)
    pushed = await llm_thinking.push_thinking(
        "the `evals` harness is empty",
        thinking_msg=fake,
        silent=False,
        throttle=llm_thinking.ThinkingThrottle(),
        force=True,
    )
    assert pushed is True
    assert len(fake.edits) == 1
    edit = fake.edits[0]
    assert edit["kwargs"].get("parse_mode") is None
    assert "`evals`" in edit["text"], (
        "the reasoning keeps the model's own markup -- it is not the "
        "answer, and it is not formatted")
    assert "<code>" not in edit["text"]


def test_the_reasoning_module_does_not_use_the_formatter():
    """The structural half of the same rule.

    The behavioural tests above say that the reasoning the user sees is
    not formatted. This one says why it cannot start being formatted:
    the module that owns the reasoning does not import the formatter at
    all.
    """
    source = (BOT / "llm_thinking.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = set()
    called = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            called.add(node.func.id)
    assert "telegram_format" not in imported, (
        "the reasoning module must not import the answer formatter")
    assert not called & {"render_html", "split_html", "escape_html"}, (
        "the reasoning module must not render HTML")
