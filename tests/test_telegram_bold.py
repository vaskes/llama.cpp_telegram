"""The bold the model wrote reaches the chat as bold, not as asterisks.

The operator's words (2026-10-30): "надо еще с текстом в ** **
сделать, можно как то цветом выделить другим? причем этот момент
надо учесть если конструкция ** `` ** вот такая".

His screenshot is the state before this: the answer came out of the
chat with the asterisks still in it -- "**python3-venv — 4 пакета**" --
while the inline code the model wrote in the same answer was already
tinted by the client.

Telegram has no colour for text. The coloured things are the code
(monospace on a tinted background), a link, and a quote. So the bold is
the bold: the asterisks stop showing, and the phrase stands out the
way every other client shows it.

What is pinned here:

  * a span of double asterisks becomes a ``<b>`` element, and the
    prose around it survives as the model wrote it;
  * a run of **three or more** asterisks is not a delimiter -- in
    markdown that is bold *and* italic, and this renderer knows one of
    the two;
  * an **unmatched** ``**`` stays literal;
  * the pair may cross a **line break** (a model writes a bold phrase
    over two lines all the time) but not an **empty line**;
  * the code span inside a bold span keeps both, and they do not nest:
    ``<b>before</b><code>x</code><b>after</b>``. That is the case the
    operator named -- "** `` **";
  * a ``**`` inside a code span, and the bold inside a fenced block,
    are not converted: the code wins;
  * the splitter never cuts a ``<b>`` in half;
  * **the reasoning message is not formatted at all.** The operator's
    rule, categorical: the thinking bubble is the model's scratchpad,
    not the answer.
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
    _DB_DIR = tempfile.mkdtemp(prefix="telegram-bold-")
    os.environ["CONVERSATIONS_DB"] = os.path.join(_DB_DIR, "conversations.db")

BOT = Path(__file__).resolve().parent.parent / "telegram-bot"
if str(BOT) not in sys.path:
    sys.path.insert(0, str(BOT))

import chat_utils  # noqa: E402
import handlers  # noqa: E402
import llm_thinking  # noqa: E402
import telegram_format  # noqa: E402

#: The operator's own sentence, with the bold phrase he wants marked.
OPERATOR = (
    "Соседа проверила из любопытства: **python3-venv — 4 пакета** "
    "(python3-pip-whl, python3-setuptools-whl, python3.14-venv, "
    "python3-venv), и это ровно тот отсутствующий ensurepip."
)

#: The case the operator named: a bold phrase that holds a code span.
NESTED = (
    "Потому что **noexec визит не только на `/workspace`, но и на "
    "`/tmp`** — проверила запуском скрипта из обоих."
)


# --- the rendering --------------------------------------------------------

def test_a_bold_span_becomes_a_bold_element():
    html = telegram_format.render_html("Песочнице нужен **один** путь.")
    assert "<b>один</b>" in html, (
        "the phrase has to become a bold element -- the user does not "
        "want to read the model's asterisks")
    assert "**один**" not in html, "the asterisks are markup, not text"


def test_the_operator_own_sentence_keeps_its_prose_and_marks_its_bold():
    html = telegram_format.render_html(OPERATOR)
    assert "<b>python3-venv — 4 пакета</b>" in html
    assert "Соседа проверила из любопытства:" in html, (
        "the prose around the phrase has to survive as it was written")
    assert "**" not in html, "the asterisks are markup, not text"


def test_the_bold_inside_the_phrase_is_escaped():
    html = telegram_format.render_html("**a < b & c**")
    assert "<b>a &lt; b &amp; c</b>" in html, (
        "the body of the element is escaped -- Telegram rejects a "
        "message it cannot parse")


def test_a_run_of_three_asterisks_stays_literal():
    html = telegram_format.render_html("***both***")
    assert "<b>" not in html, (
        "a run of three asterisks is bold *and* italic in markdown; this "
        "renderer knows one of the two, so it leaves the run alone")
    assert "***both***" in html


def test_an_unmatched_pair_stays_literal():
    html = telegram_format.render_html("2 ** 3 = 8")
    assert "<b>" not in html, (
        "an unmatched delimiter is not a delimiter; the text stays the "
        "way it was written")
    assert "2 ** 3 = 8" in html


def test_a_pair_that_crosses_a_line_break_is_converted():
    html = telegram_format.render_html("**bold over\ntwo lines**")
    assert "<b>bold over\ntwo lines</b>" in html, (
        "a model writes a bold phrase over two lines all the time")


def test_a_pair_that_crosses_an_empty_line_stays_literal():
    html = telegram_format.render_html("**bold over\n\na blank line**")
    assert "<b>" not in html, (
        "a paragraph break between the two means the asterisks belong to "
        "something else")


def test_the_bold_inside_a_fenced_block_is_not_converted_again():
    html = telegram_format.render_html("```\n**inside a fence**\n```")
    assert "<pre>" in html
    assert "<b>" not in html, (
        "the block is already code; the bold inside it is text")


def test_the_bold_inside_an_inline_code_span_is_not_converted():
    html = telegram_format.render_html("`**not bold**`")
    assert "<code>**not bold**</code>" in html, (
        "the code wins: the asterisks inside a code span are text")


def test_a_delimiter_inside_a_code_span_does_not_open_a_bold_span():
    """A `**` inside a code span is not a delimiter.

    Otherwise the pair would reach out of the span and take the prose
    with it, and the code span would end up inside a bold phrase it was
    never part of.
    """
    html = telegram_format.render_html("`a ** b` and **bold**")
    assert "<code>a ** b</code>" in html, (
        "the code span keeps its own asterisks")
    assert "<b>bold</b>" in html
    assert html.count("<b>") == 1, (
        "the asterisks inside the code span must not open a span of "
        "their own")


def test_a_code_span_inside_a_bold_span_keeps_both():
    """The case the operator named: the construct ** `` **."""
    html = telegram_format.render_html(NESTED)
    assert "<code>/workspace</code>" in html, (
        "the code span inside the bold phrase stays a code element")
    assert "<b>" in html, "the prose of the phrase stays bold"
    assert "**" not in html, "the asterisks are markup, not text"


def test_the_bold_and_the_code_do_not_nest():
    html = telegram_format.render_html("**a `code` b**")
    assert html == "<b>a </b><code>code</code><b> b</b>", (
        "the elements do not nest: the splitter has to keep every "
        "element whole, and it can only do that if they do not nest")


def test_a_bold_span_that_does_not_fit_stays_literal():
    limit = 200
    html = telegram_format.render_html("**" + "x" * 400 + "**", limit=limit)
    assert "<b>" not in html, (
        "a span that does not fit in a message stays literal, the same "
        "way a code span that does not fit does")
    assert "**" in html


def test_an_empty_answer_is_empty():
    assert telegram_format.render_html("") == ""


# --- the split ------------------------------------------------------------

def test_the_split_never_cuts_a_bold_in_half():
    """A cut inside a `<b>` is a message Telegram rejects.

    The span has to straddle the limit -- a span that ends before it
    would leave the cut after the element and exercise nothing.
    """
    prose = "x" * (chat_utils.TELEGRAM_TEXT_CHUNK - 10)
    html = telegram_format.render_html(prose + "**abcdefghij**" + "tail " * 500)
    assert "<b>abcdefghij</b>" in html, (
        "the span was not converted -- the test would pass without "
        "checking anything")
    pieces = telegram_format.split_html(html, chat_utils.TELEGRAM_TEXT_CHUNK)
    assert len(pieces) > 1, "the case needs a cut inside the span"
    for piece in pieces:
        assert piece.count("<b>") == piece.count("</b>"), (
            f"a chunk has an unbalanced <b>: {piece[-80:]!r}")
        assert len(piece) <= chat_utils.TELEGRAM_TEXT_CHUNK, (
            f"a chunk of {len(piece)} characters does not fit Telegram")
    whole = [p for p in pieces if "<b>" in p]
    assert len(whole) == 1, "the span has to be delivered whole"
    assert whole[0].startswith("<b>"), (
        "the span opens the message that carries it")


def test_every_piece_fits_telegram_and_holds_whole_spans():
    """The property, over the shapes an answer can have."""
    shapes = [
        "prose " * 3000,
        "prose " * 500 + "**a phrase**" + "tail " * 2000,
        "**a phrase**" + "tail " * 3000,
        "prose " * 900 + "**a phrase**" + "tail " * 900,
        "```bash\n$ echo hi\n```" + "**a phrase**" + "tail " * 3000,
        "**a phrase**" * 500,
        "**a phrase with `code` inside**" * 400,
    ]
    for shape in shapes:
        html = telegram_format.render_html(shape)
        pieces = telegram_format.split_html(html, chat_utils.TELEGRAM_TEXT_CHUNK)
        for piece in pieces:
            assert len(piece) <= chat_utils.TELEGRAM_TEXT_CHUNK, (
                f"a piece of {len(piece)} characters does not fit Telegram")
            assert piece.count("<b>") == piece.count("</b>"), (
                f"a piece has an unbalanced <b>: {piece[-80:]!r}")
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


async def test_the_answer_goes_out_with_the_bold_element(update):
    await handlers._send_answer_text(update, OPERATOR)
    text = update.message.sent[0]["text"]
    assert "<b>python3-venv — 4 пакета</b>" in text
    assert update.message.sent[0]["kwargs"].get("parse_mode") == "HTML"


# --- the reasoning is not formatted ---------------------------------------

async def test_the_reasoning_keeps_its_asterisks(update):
    """The reasoning goes out as the model wrote it.

    The asterisks stay asterisks, and no ``<b>`` element appears. The
    thinking bubble is the model's scratchpad, edited in place -- the
    operator's rule about it is categorical.
    """
    fake = _FakeMessage(update.effective_chat)
    pushed = await llm_thinking.push_thinking(
        "the **evals** harness is empty",
        thinking_msg=fake,
        silent=False,
        throttle=llm_thinking.ThinkingThrottle(),
        force=True,
    )
    assert pushed is True
    assert len(fake.edits) == 1
    edit = fake.edits[0]
    assert edit["kwargs"].get("parse_mode") is None
    assert "**evals**" in edit["text"], (
        "the reasoning keeps the model's own markup -- it is not the "
        "answer, and it is not formatted")
    assert "<b>" not in edit["text"]
