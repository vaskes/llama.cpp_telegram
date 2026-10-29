"""How an answer becomes a Telegram message.

The bot's own console output is a code block -- the transcript of the
commands the model ran, with what they printed:

    ```bash
    $ ls -l /workspace
    -rw-r--r-- 1 agent agent 10240 random10k.bin
    ```

Sent as plain text, the user reads the backticks too. Telegram can
render that block natively -- a monospace, selectable, copyable code
block -- but only if the message carries a parse mode. So the answer
goes out as HTML, and the code block becomes ``<pre>``.

**Why HTML and not MarkdownV2.** MarkdownV2 needs eighteen characters
escaped (``_*[]()~`>#+-=|{}.!``), and escaping them destroys the model's
own markdown: ``**bold**`` becomes literal asterisks. HTML needs three
(``&``, ``<``, ``>``), so the prose the model wrote survives as it is,
and the block the bot owns becomes a real code block.

Two rules keep the message from breaking:

* only a **complete** fenced block is converted. A block the chunk
  splitter cut in half stays literal text -- a ``<pre>`` without its
  closing tag is a message Telegram rejects with "can't parse
  entities", and a rejected message means the user gets no answer at
  all;
* a block longer than one message is left literal as well. Telegram
  counts what it is sent, so the HTML has to fit the same budget the
  plain text did.

The same goes for the **inline** code. A word or a path the model
wrapped in single backticks -- ``evals/baselines/`` -- becomes a
``<code>`` element, and Telegram draws it monospace, the way the
operator's own client draws it. Three rules keep that honest:

* a delimiter is a run of **exactly one** backtick. A run of two or
  more is a fence (or the way to write a backtick inside a span), and
  it stays literal;
* the pair sits on **one line**. A span that runs over a line break is
  a model that lost track of its own markup, and converting it would
  hide the mistake instead of showing it;
* an **unmatched** backtick is not a delimiter. The text stays the way
  it was written, because a ``<code>`` without its closing tag is a
  message Telegram rejects, and a rejected message means the user gets
  no answer at all.

Everything else is escaped, which is what makes the parse mode safe:
the model writes whatever it writes, and the user sees it as it was
written.

**Only the answer is formatted.** The reasoning the user watches while
the model thinks is a different artifact -- it is edited in place, it
is the model's own scratchpad, and the operator's rule is categorical:
it is not formatted. The notices and the errors are a third artifact:
text written for a person, with no markup to render.
"""
from __future__ import annotations

from chat_utils import TELEGRAM_TEXT_CHUNK

__all__ = [
    "escape_html",
    "render_html",
    "split_html",
]


#: The fence that opens and closes a code block.
_FENCE = "```"

#: What a `<pre>` element costs around its body.
_TAG_OVERHEAD = len("<pre>") + len("</pre>")

#: What a `<code>` element costs around its body.
_CODE_TAG_OVERHEAD = len("<code>") + len("</code>")

#: The elements the renderer produces, and the tag that closes each.
#: The splitter has to keep both of them whole: a cut inside one of
#: them is a message Telegram rejects. They never nest -- the body of
#: a `<pre>` is escaped, so it cannot contain a `<code>`.
_ELEMENTS = (("<pre>", "</pre>"), ("<code>", "</code>"))


def escape_html(text: str) -> str:
    """Escape the three characters that are special in Telegram's HTML.

    Everything else is left alone: Telegram's HTML parser only knows
    these three, and the tag names it accepts are not among the
    characters a model prints.
    """
    if not text:
        return ""
    return (text.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;"))


def _fenced_blocks(text: str) -> list[tuple[int, int]]:
    """The spans of the complete fenced code blocks in `text`.

    A block is complete when it has an opening fence, a closing fence,
    and no fence in between. An unclosed fence means the chunk splitter
    cut the block, and that block is not converted.
    """
    spans: list[tuple[int, int]] = []
    pos = 0
    while True:
        start = text.find(_FENCE, pos)
        if start < 0:
            return spans
        end = text.find(_FENCE, start + len(_FENCE))
        if end < 0:
            # No closing fence: the block was cut, or there never was one.
            return spans
        spans.append((start, end + len(_FENCE)))
        pos = end + len(_FENCE)


def _inline_code_spans(text: str) -> list[tuple[int, int]]:
    """The spans of the inline code in `text`.

    An inline code span is a pair of single backticks with the text
    between them. The three rules are the ones the module docstring
    gives, and each of them is a shape that really happens:

    * a run of **two or more** backticks is not a delimiter -- it is the
      fence of a block that was cut in half, or the way to write a
      backtick inside a span. It stays literal;
    * the pair has to sit on **one line**. The closing backtick of a
      rejected pair is skipped as well: it cannot open a span either,
      or the text after it would be swallowed by a span the model never
      wrote;
    * an **unmatched** backtick is not a delimiter.

    The spans are `(start, end)` pairs over `text`, and `end` is one
    past the closing backtick.
    """
    spans: list[tuple[int, int]] = []
    pos = 0
    length = len(text)
    while pos < length:
        start = text.find("`", pos)
        if start < 0:
            break
        run_end = start
        while run_end < length and text[run_end] == "`":
            run_end += 1
        if run_end - start != 1:
            # A run of two or more: not a delimiter.
            pos = run_end
            continue
        close = text.find("`", run_end)
        if close < 0:
            # No closing backtick at all: the rest is literal text.
            break
        close_end = close
        while close_end < length and text[close_end] == "`":
            close_end += 1
        if close_end - close != 1:
            # The next run is longer than one: not a closing delimiter.
            pos = run_end
            continue
        if "\n" in text[run_end:close]:
            # The pair is not on one line. Skip the closing backtick as
            # well -- it cannot open a span either.
            pos = close + 1
            continue
        spans.append((start, close + 1))
        pos = close + 1
    return spans


def _render_prose(text: str, limit: int) -> str:
    """The text as HTML: escaped, with the inline code marked.

    A code span that does not fit in a message stays literal, the same
    way a fenced block that does not fit does. A ``<code>`` that
    Telegram cannot parse loses the answer, and an answer written as
    plain text is still an answer.
    """
    if not text:
        return ""
    spans = _inline_code_spans(text)
    if not spans:
        return escape_html(text)
    out: list[str] = []
    cursor = 0
    for start, end in spans:
        out.append(escape_html(text[cursor:start]))
        body = escape_html(text[start + 1:end - 1])
        if len(body) + _CODE_TAG_OVERHEAD > limit:
            # Too big for one message: leave it the way it came.
            out.append(escape_html(text[start:end]))
        else:
            out.append("<code>" + body + "</code>")
        cursor = end
    out.append(escape_html(text[cursor:]))
    return "".join(out)


def render_html(text: str, limit: int = TELEGRAM_TEXT_CHUNK) -> str:
    """The answer as a Telegram HTML message.

    The text is escaped, every complete fenced code block becomes a
    ``<pre>`` element -- which Telegram renders as a native code block
    with its own copy button -- and every inline code span becomes a
    ``<code>`` element, which Telegram draws monospace.

    A code block bigger than one message is left as literal text. A
    ``<pre>`` that does not fit is a message Telegram rejects, and the
    plain text it replaced would have been split and delivered.
    """
    if not text:
        return ""
    spans = _fenced_blocks(text)
    if not spans:
        return _render_prose(text, limit)
    out: list[str] = []
    cursor = 0
    for start, end in spans:
        out.append(_render_prose(text[cursor:start], limit))
        body = text[start + len(_FENCE):end - len(_FENCE)]
        # The text right after the opening fence is the info string --
        # the language hint ("```bash"). It is not part of the code, and
        # Telegram's highlighter does not know every shell, so it goes.
        newline = body.find("\n")
        if newline >= 0:
            body = body[newline + 1:]
        if len(body) + _TAG_OVERHEAD > limit:
            # Too big for one message: leave it the way it came, so the
            # splitter can cut it into pieces that fit.
            out.append(escape_html(text[start:end]))
        else:
            out.append("<pre>" + escape_html(body) + "</pre>")
        cursor = end
    out.append(_render_prose(text[cursor:], limit))
    return "".join(out)


def split_html(html: str, limit: int) -> list[str]:
    """Split an HTML message so that a ``<pre>`` is never cut in half.

    The cut is on character boundaries, like the plain-text splitter. If
    it would land inside a ``<pre>`` or a ``<code>``, the cut moves
    *before* that element: the element opens the next message, whole. A
    ``<pre>`` without its closing tag is a message Telegram rejects, and
    a message longer than Telegram's limit is a message Telegram
    rejects -- both lose the answer, so neither is allowed.

    The block always fits in a message on its own: ``render_html`` left
    a code block bigger than a message as literal text, so the block
    that reaches this function is at most a message long. The same goes
    for an inline code span, and for the same reason.

    A ``<pre>`` without a closing tag cannot come out of
    ``render_html``; if it does, the cut stays where it is. A short
    message beats a rejected one.
    """
    if not html:
        return []
    pieces: list[str] = []
    rest = html
    while len(rest) > limit:
        cut = limit
        # The element the cut would land inside, if any. The renderer
        # never nests them, so at most one of them is open here.
        for open_tag, close_tag in _ELEMENTS:
            opened = rest.rfind(open_tag, 0, cut)
            closed = rest.rfind(close_tag, 0, cut)
            if opened > closed:
                if rest.find(close_tag, cut) >= 0 and opened > 0:
                    # The cut moves *before* the element: it opens the
                    # next piece, whole. Moving it *past* the element
                    # would make the piece longer than the limit --
                    # the same rejection, wearing a different hat.
                    cut = opened
                break
        piece = rest[:cut]
        if piece.strip():
            pieces.append(piece)
        rest = rest[cut:]
    if rest.strip():
        pieces.append(rest)
    return pieces
