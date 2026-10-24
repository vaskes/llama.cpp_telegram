"""Which parts of a model answer are files, and what they are called.

The operator's request (2026-10-24): when the model answers with a
formatted document -- html, json, sql, code, txt -- the file should
arrive as a Telegram document instead of as a wall of text in the chat.
One answer may carry several files.

This module is a pure function of the answer text: no Telegram, no
client, no clock, no await.  That is what makes the shapes testable,
and the shapes are the whole risk here -- a model answer is free text,
and every rule below was written against a form that was actually
observed rather than against the form one would expect.

The observed forms (production, 2026-10-24, engine
Qwen3.8-27B-Ultra-Heretic-MTP-256k, measured on the live engine):

  * ``[llm] `` followed by a ```json fence -- a traits dump, a few
    hundred characters, with no name anywhere;
  * prose, a ```html fence, and a tail "Сохрани как `index.html`";
  * prose, a ```js fence whose first line is ``// greet.js``, and a
    tail "Запуск: `node greet.js`";
  * prose, a ```sql fence whose first line is ``-- cradle.sql``, and a
    tail "Выполнить: `sqlite3 cradle.db < cradle.sql`";
  * three blocks in one answer, each preceded by a heading
    ("## index.html", "## style.css", "## app.js");
  * a raw HTML page with no fence at all (425 lines).

Three things that look like files and are not:

  * the bot's own executed-tool trace -- a ```bash fence whose lines
    all start with "$ ".  It is a log of what the model ran, not a
    deliverable;
  * a diagnostic -- "the model spent all its tokens on reasoning" --
    which carries the model's reasoning tail.  The reasoning is not a
    file, and the *type* of the result is what says so: a diagnostic
    is a plain string, an answer is a dict.  See `llm_result`;
  * an unterminated fence: the answer was cut off mid-block, so what
    is left is a fragment, not a file.  It stays in the text.

The name of a file is looked for in three places, in this order:

  1. the line right above the block, when that line *is* a name
     ("## index.html", "index.html:", "**index.html**");
  2. a comment on the block's first line ("// greet.js",
     "-- cradle.sql", "# config.yaml", "<!-- index.html -->");
  3. a name mentioned in the prose that follows the block
     ("Сохрани как `index.html`").

When none of them names the file, it is called `answer.<ext>` --
`answer-2.<ext>` for the second file with the same extension.

Why the extension comes from the fence tag: the tag is what the model
wrote, so the table is the model's vocabulary rather than ours.  A tag
that is not in the table is a `.txt` file -- the content is still worth
delivering, and guessing an extension would put a `.py` name on
something that is not Python.
"""
from __future__ import annotations

import json
import re
from typing import NamedTuple

__all__ = ["Artifact", "extract_files", "LANGUAGE_EXTENSIONS"]


class Artifact(NamedTuple):
    """One file an answer carries.

    `filename` is what the document will be called, `content` is what
    goes into it, `language` is the fence tag it came from -- empty
    when the answer had no tag.
    """
    filename: str
    content: str
    language: str


#: Fence tag -> file extension.
LANGUAGE_EXTENSIONS = {
    "html": ".html", "htm": ".html", "xhtml": ".html",
    "xml": ".xml", "svg": ".svg",
    "json": ".json", "jsonc": ".json", "json5": ".json",
    "yaml": ".yml", "yml": ".yml",
    "toml": ".toml", "ini": ".ini", "cfg": ".cfg", "conf": ".cfg",
    "csv": ".csv", "tsv": ".tsv",
    "sql": ".sql",
    "python": ".py", "py": ".py", "python3": ".py",
    "javascript": ".js", "js": ".js", "node": ".js",
    "mjs": ".mjs", "cjs": ".cjs",
    "typescript": ".ts", "ts": ".ts", "jsx": ".jsx", "tsx": ".tsx",
    "css": ".css", "scss": ".scss", "sass": ".sass", "less": ".less",
    "sh": ".sh", "bash": ".sh", "shell": ".sh", "zsh": ".sh",
    "console": ".sh",
    "markdown": ".md", "md": ".md",
    "diff": ".diff", "patch": ".patch",
    "c": ".c", "h": ".h", "cpp": ".cpp", "c++": ".cpp", "hpp": ".hpp",
    "java": ".java", "kotlin": ".kt", "kt": ".kt",
    "go": ".go", "rust": ".rs", "rs": ".rs", "php": ".php",
    "ruby": ".rb", "rb": ".rb", "swift": ".swift", "scala": ".scala",
    "lua": ".lua", "perl": ".pl", "pl": ".pl", "r": ".r",
    "julia": ".jl", "jl": ".jl", "dart": ".dart", "elixir": ".ex",
    "ex": ".ex", "haskell": ".hs", "hs": ".hs", "clojure": ".clj",
    "clj": ".clj", "graphql": ".graphql", "gql": ".graphql",
    "proto": ".proto", "prisma": ".prisma", "dockerfile": ".dockerfile",
    "makefile": ".makefile", "cmake": ".cmake", "terraform": ".tf",
    "tf": ".tf", "hcl": ".hcl",
    "text": ".txt", "txt": ".txt", "plain": ".txt", "plaintext": ".txt",
}

#: The extension a file with no known tag gets.
DEFAULT_EXTENSION = ".txt"

#: Fence tags that carry a shell transcript rather than a file.
_SHELL_TAGS = frozenset({"bash", "sh", "shell", "zsh", "console"})

#: Three backticks and a language tag.
_FENCE_OPEN = re.compile(r"^```(?P<tag>[A-Za-z0-9_+#.\-]*)")

#: A filename: a stem and an extension.  The extension is at least two
#: characters so that "e.g." and "i.e." in prose are not filenames.
#: A path is allowed here -- it is reduced to its last segment later.
_FILENAME_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._\-/]*\.[A-Za-z0-9]{2,12}")

#: The same, for a name mentioned in running prose.  A path is not
#: matched, and the lookbehind keeps a name that is part of a URL
#: ("https://example.com") from being read as a file name.
_MENTION_RE = re.compile(r"(?<![\w./:-])[A-Za-z0-9_][A-Za-z0-9._\-]*\.[A-Za-z0-9]{2,12}")

#: A comment on the first line of a block that names the file.
_COMMENT_NAME_RE = re.compile(
    r"^\s*(?://|#|--|<!--|/\*|;)\s*"
    r"(?P<name>[A-Za-z0-9_][A-Za-z0-9._\-/]*\.[A-Za-z0-9]{2,12})"
    r"\s*(?:-->|\*/)?\s*$"
)

#: A code span, which is how a model names a file in prose.
_CODE_SPAN_RE = re.compile(r"`([^`\n]+)`")

#: Markdown heading, for the "the line above the block" rule.
_HEADING_RE = re.compile(r"^#{1,6}\s*")

#: A fence that does not start a line.  The model puts one right after
#: a prefix sometimes -- "[llm] ```json" -- and a fence that does not
#: start a line is not a fence to a markdown parser, but it is one to a
#: user who asked for a file.  Splitting the line keeps the prefix as
#: prose and the fence as a block.
_INLINE_FENCE_RE = re.compile(r"(?<=[^\n])```")

#: Longest file name we are willing to send.
_MAX_NAME = 64


class _Block(NamedTuple):
    """One fenced block of the answer."""
    tag: str
    body: list
    start: int      # index of the opening fence line
    end: int        # index of the closing fence line, inclusive


def extract_files(text, tool_outputs=()):
    """Split an answer into the prose and the files it carries.

    Returns `(prose, artifacts)`.  When the answer carries no file the
    prose is the answer, byte for byte -- the text a user sees must not
    change because a feature was switched on next to it.

    `text` is the model's own answer, not the text the bot sends: the
    executed-tool trace is added after this call, so a trace is never
    in here to be mistaken for a file.  A trace that the model itself
    wrote is still recognised, by its shape.

    `tool_outputs` is what the tools printed this turn.  The bot knows
    it -- it ran them.  A code block that repeats it is CONSOLE OUTPUT,
    not a deliverable: the operator's rule (2026-10-24) is that the
    console output goes into the chat, and a `.txt` document full of
    `xxd` output is neither a file the user asked for nor an answer.
    """
    if not isinstance(text, str) or not text.strip():
        return (text or "", [])

    stripped = text.strip()
    raw = _raw_document(stripped)
    if raw is not None:
        # The whole answer is the document: there is no prose to send
        # and no name to find, because the answer never had any.
        return ("", [raw])

    lines = _normalized_lines(text)
    blocks = _find_blocks(lines)
    if not blocks:
        return (text, [])

    artifacts = []
    counters = {}
    for index, block in enumerate(blocks):
        if _is_tool_trace(block):
            continue
        if _is_console_output(block, tool_outputs):
            continue
        before = lines[_previous_end(blocks, index):block.start]
        after = lines[block.end + 1:_next_start(blocks, index)]
        artifacts.append(_artifact_for(block, before, after, counters))

    if not artifacts:
        return (text, [])

    removed = set()
    for block in blocks:
        if _is_tool_trace(block):
            continue
        if _is_console_output(block, tool_outputs):
            continue
        removed.update(range(block.start, block.end + 1))
    prose = "\n".join(line for i, line in enumerate(lines) if i not in removed)
    prose = re.sub(r"\n{3,}", "\n\n", prose).strip()
    return (prose, artifacts)


def _normalized_lines(text):
    """The answer's lines, with every fence starting a line.

    The text the user ends up seeing is the original one: this is only
    how the answer is *read*.
    """
    return _INLINE_FENCE_RE.sub("\n```", text).splitlines()


def _raw_document(stripped):
    """The answer as one document with no fence around it.

    Measured: a 425-line HTML page, delivered as the answer itself.
    The JSON case is the same idea -- the model was asked for a traits
    dump and answered with the dump and nothing else.

    Only a dict or a list counts as JSON.  A bare number parses as
    JSON too, and "42" is an answer, not a file.
    """
    low = stripped.lower()
    if low.startswith("<!doctype html") or low.startswith("<html"):
        return Artifact("answer.html", stripped, "html")
    if stripped[0] in "{[":
        try:
            value = json.loads(stripped)
        except ValueError:
            return None
        if isinstance(value, (dict, list)):
            return Artifact("answer.json", stripped, "json")
    return None


def _find_blocks(lines):
    """Every closed fenced block, in order.

    An unterminated fence ends the scan: the rest of the answer is one
    truncated block, and a fragment is not a file.  Treating the lines
    after it as ordinary text is what keeps a cut-off answer readable.
    """
    blocks = []
    index = 0
    count = len(lines)
    while index < count:
        match = _FENCE_OPEN.match(lines[index].strip())
        if not match:
            index += 1
            continue
        tag = match.group("tag").lower()
        close = index + 1
        while close < count and lines[close].strip() != "```":
            close += 1
        if close >= count:
            break
        blocks.append(_Block(tag, lines[index + 1:close], index, close))
        index = close + 1
    return blocks


def _is_tool_trace(block):
    """True for the bot's own executed-tool trace.

    A shell fence that STARTS with a "$ " line is a transcript of a
    terminal session, not a deliverable.  The bot renders exactly this
    shape -- the commands, and since 2026-10-24 what they printed --
    and a model that echoes the transcript back should not turn it into
    a file either.

    The rule is "the first non-empty line starts with $ ", not "every
    line does": the trace carries the console output now, and the
    output does not start with "$ ".  A model-written shell script
    does not start with "$ " either, so it is still a file.
    """
    if block.tag not in _SHELL_TAGS:
        return False
    body = [line for line in block.body if line.strip()]
    if not body:
        return False
    return body[0].lstrip().startswith("$ ")


def _is_console_output(block, tool_outputs) -> bool:
    """True for a block that repeats what a tool printed.

    The bot ran the tool, so it knows the output.  The model often
    pastes that output into a code block -- the base64 of a file, the
    `xxd` dump of a binary, the listing of a directory -- and the file
    feature used to take the block out of the chat and deliver it as a
    `.txt` document.  The user then got the console output as a file
    and the chat without it.

    Two ways to match, because the model quotes in two ways:

      * the block IS the output (a part of it) -- normalised and
        compared as a substring;
      * the block is a QUOTE of the output -- the model pasted some
        lines of it, possibly with its own framing, so most of the
        block's lines have to be found in the output.

    The second rule has a cost, stated here because it is a policy and
    not an accident: a file the model wrote AND printed (cat'ed) is
    output too, and stays in the chat instead of arriving as a
    document.  The content is still visible to the user either way;
    what changes is whether there is a file to save.
    """
    if not tool_outputs:
        return False
    body = "\n".join(block.body).strip()
    if not body:
        return False
    body_norm = " ".join(body.split())
    body_lines = [line.strip() for line in body.splitlines() if line.strip()]
    for out in tool_outputs:
        if not out or not out.strip():
            continue
        out_norm = " ".join(out.split())
        if body_norm and body_norm in out_norm:
            return True
        out_lines = set(line.strip() for line in out.splitlines() if line.strip())
        if not out_lines or not body_lines:
            continue
        hits = sum(1 for line in body_lines if line in out_lines)
        if hits / len(body_lines) >= 0.6:
            return True
    return False


def _previous_end(blocks, index):
    """The line after the previous block, or the start of the answer."""
    if index == 0:
        return 0
    return blocks[index - 1].end + 1


def _next_start(blocks, index):
    """The line before the next block, or the end of the answer."""
    if index + 1 >= len(blocks):
        return 10 ** 9
    return blocks[index + 1].start


def _artifact_for(block, before, after, counters):
    """The file this block is, and what it is called."""
    name = _name_above(before)
    if name is None:
        name = _name_in_comment(block)
    if name is None:
        name = _name_after(after)
    if name is None:
        name = _fallback_name(_extension_for(block.tag), counters)
    elif "." not in name:
        name = name + _extension_for(block.tag)
    return Artifact(name, "\n".join(block.body), block.tag)


def _extension_for(tag):
    """The extension a fence tag implies."""
    return LANGUAGE_EXTENSIONS.get(tag, DEFAULT_EXTENSION)


def _fallback_name(extension, counters):
    """`answer.html`, then `answer-2.html` for the next one."""
    counters[extension] = counters.get(extension, 0) + 1
    count = counters[extension]
    suffix = "" if count == 1 else f"-{count}"
    return f"answer{suffix}{extension}"


def _name_above(before):
    """A name in the line right above the block.

    The line has to *be* a name ("## index.html", "index.html:",
    "**index.html**") or mention one in a code span ("Сохрани как
    `index.html`").  A bare filename in running prose is not enough:
    "in contrast to config.yaml" is not a name for this file.
    """
    line = _last_meaningful_line(before)
    if not line:
        return None
    bare = _bare_name(line)
    if bare is not None:
        return bare
    return _code_span_name(line)


def _name_in_comment(block):
    """A name in a comment on the block's first line."""
    if not block.body:
        return None
    match = _COMMENT_NAME_RE.match(block.body[0])
    if not match:
        return None
    return _safe_name(match.group("name"))


def _name_after(after):
    """A name in the prose that follows the block.

    Only the first line of it, and never a heading: a heading names
    the *next* block, and reading it as this block's name would give
    two files in one answer the same name.
    """
    for line in after:
        stripped = line.strip()
        if not stripped:
            continue
        if _HEADING_RE.match(stripped):
            return None
        spanned = _code_span_name(stripped)
        if spanned is not None:
            return spanned
        match = _MENTION_RE.search(stripped)
        if match:
            return _safe_name(match.group(0))
        return None
    return None


def _last_meaningful_line(lines):
    for line in reversed(lines):
        if line.strip():
            return line
    return ""


def _bare_name(line):
    """The line, stripped of markdown decoration, if it is a filename."""
    candidate = _HEADING_RE.sub("", line.strip()).strip()
    candidate = candidate.strip("*_`~").strip()
    candidate = candidate.rstrip(":").strip()
    if _FILENAME_RE.fullmatch(candidate):
        return _safe_name(candidate)
    return None


def _code_span_name(text):
    """A filename mentioned in a code span.

    The span may be a command rather than a name -- "node greet.js",
    "sqlite3 cradle.db < cradle.sql" -- so the last path-like token in
    it is the one that names the file.
    """
    for span in _CODE_SPAN_RE.findall(text):
        token = _last_path_token(span)
        if token is not None:
            return token
    return None


def _last_path_token(span):
    for token in reversed(re.split(r"[\s<>|]+", span)):
        token = token.rstrip("/")
        if token and _FILENAME_RE.fullmatch(token):
            return _safe_name(token)
    return None


def _safe_name(name):
    """A name Telegram will accept as a file name.

    A path is reduced to its last segment: the document arrives in a
    chat, not in a directory tree.
    """
    base = name.replace("\\", "/").split("/")[-1]
    base = re.sub(r"[^A-Za-z0-9._\- ]", "_", base).strip()
    if not base:
        return "answer"
    return base[:_MAX_NAME]
