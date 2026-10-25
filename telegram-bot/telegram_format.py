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

Everything else is escaped, which is what makes the parse mode safe:
the model writes whatever it writes, and the user sees it as it was
written.
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


def render_html(text: str, limit: int = TELEGRAM_TEXT_CHUNK) -> str:
    """The answer as a Telegram HTML message.

    The text is escaped, and every complete fenced code block becomes a
    ``<pre>`` element -- which Telegram renders as a native code block
    with its own copy button.

    A code block bigger than one message is left as literal text. A
    ``<pre>`` that does not fit is a message Telegram rejects, and the
    plain text it replaced would have been split and delivered.
    """
    if not text:
        return ""
    spans = _fenced_blocks(text)
    if not spans:
        return escape_html(text)
    out: list[str] = []
    cursor = 0
    for start, end in spans:
        out.append(escape_html(text[cursor:start]))
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
    out.append(escape_html(text[cursor:]))
    return "".join(out)


def split_html(html: str, limit: int) -> list[str]:
    """Split an HTML message so that a ``<pre>`` is never cut in half.

    The cut is on character boundaries, like the plain-text splitter. If
    it would land inside a ``<pre>``, the cut moves *before* that block:
    the block opens the next message, whole. A ``<pre>`` without its
    closing tag is a message Telegram rejects, and a message longer than
    Telegram's limit is a message Telegram rejects -- both lose the
    answer, so neither is allowed.

    The block always fits in a message on its own: ``render_html`` left
    a code block bigger than a message as literal text, so the block
    that reaches this function is at most a message long.

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
        opened = rest.rfind("<pre>", 0, cut)
        closed = rest.rfind("</pre>", 0, cut)
        if opened > closed:
            # The cut is inside a <pre>: the block opens the next piece.
            end = rest.find("</pre>", cut)
            if end >= 0:
                cut = opened
                if cut == 0:
                    # A block that starts the text: the only cut left is
                    # the limit itself.
                    cut = limit
        piece = rest[:cut]
        if piece.strip():
            pieces.append(piece)
        rest = rest[cut:]
    if rest.strip():
        pieces.append(rest)
    return pieces
