"""Which parts of an answer are files, and what they are called.

The feature (2026-10-24): when the model answers with a document -- html,
json, sql, code, txt -- the document should arrive as a Telegram
document, not as a wall of text in the chat. One answer may carry
several files.

Every fixture below is a form that was actually observed, not a form
that would be convenient to test. The engine was Qwen3.8-27B-Ultra-Heretic-
MTP-256k, and the answers were taken from a live run on 2026-10-24:

  * a traits dump that the model prefixed with "[llm] " and put in a
    ```json fence, with no name anywhere;
  * prose, a ```html fence, and a tail "Сохрани как `index.html`";
  * prose, a ```js fence whose first line is ``// greet.js``;
  * prose, a ```sql fence whose first line is ``-- cradle.sql``;
  * three blocks in one answer, each under a heading;
  * a raw HTML page, 425 lines, with no fence at all.

The three things that look like files and are not are pinned here too,
because each of them is a way for the user to receive something they did
not ask for: the bot's own executed-tool trace, the reasoning tail of a
diagnostic, and a fence that was cut off mid-block.
"""
import sys
from pathlib import Path

BOT = Path(__file__).resolve().parent.parent / "telegram-bot"
if str(BOT) not in sys.path:
    sys.path.insert(0, str(BOT))

from llm_artifact import extract_files  # noqa: E402


# --- the observed forms ---------------------------------------------------

def test_a_json_dump_with_a_prefix_becomes_one_file():
    """The traits dump. The fence does not start a line -- the model put
    it right after "[llm] " -- and a fence that does not start a line is
    not a fence to a markdown parser, but it is one to a user who asked
    for a file."""
    answer = ('[llm] ```json\n{\n  "name": "Vaskes",\n'
              '  "traits": ["direct", "curious"]\n}\n```')
    prose, files = extract_files(answer)
    assert [f.filename for f in files] == ["answer.json"]
    assert files[0].language == "json"
    assert '"traits"' in files[0].content
    assert "```" not in prose
    assert "[llm]" in prose


def test_a_small_answer_still_becomes_a_file():
    """The operator asked for "json со своими traits" and got a few
    hundred characters. A size threshold would have thrown that away:
    the file is what was asked for, not a sign that the answer was
    long."""
    answer = '```json\n{"traits": ["a", "b"]}\n```'
    _, files = extract_files(answer)
    assert [f.filename for f in files] == ["answer.json"]
    assert files[0].content == '{"traits": ["a", "b"]}'


def test_an_html_block_is_named_by_the_prose_that_follows_it():
    answer = ('Готово, вот страница.\n\n```html\n'
              '<!doctype html>\n<html><body>hi</body></html>\n```\n\n'
              'Сохрани как `index.html` и открой в браузере.')
    prose, files = extract_files(answer)
    assert [f.filename for f in files] == ["index.html"]
    assert files[0].language == "html"
    assert "<!doctype html>" in files[0].content
    assert "Сохрани как" in prose
    assert "<html>" not in prose


def test_a_javascript_block_is_named_by_the_comment_on_its_first_line():
    answer = ('Вот скрипт:\n\n```js\n// greet.js\n'
              'console.log("hi");\n```\n\nЗапуск: `node greet.js`')
    _, files = extract_files(answer)
    assert [f.filename for f in files] == ["greet.js"]
    assert files[0].language == "js"
    assert files[0].content.startswith("// greet.js")


def test_an_sql_block_is_named_by_the_comment_on_its_first_line():
    answer = ('Схема:\n\n```sql\n-- cradle.sql\n'
              'CREATE TABLE t (id INT);\n```\n\n'
              'Выполнить: `sqlite3 cradle.db < cradle.sql`')
    _, files = extract_files(answer)
    assert [f.filename for f in files] == ["cradle.sql"]
    assert files[0].language == "sql"


def test_three_blocks_in_one_answer_are_three_files():
    """The site-in-three-files answer: a heading names each block."""
    answer = ('## index.html\n\n```html\n<html></html>\n```\n\n'
              '## style.css\n\n```css\nbody { color: red; }\n```\n\n'
              '## app.js\n\n```js\nconsole.log(1);\n```\n\n'
              'Как запустить: открой index.html в браузере.')
    prose, files = extract_files(answer)
    assert [f.filename for f in files] == ["index.html", "style.css", "app.js"]
    assert [f.language for f in files] == ["html", "css", "js"]
    assert "Как запустить" in prose
    assert "body { color: red; }" not in prose


def test_a_raw_document_with_no_fence_is_one_file():
    """A 425-line HTML page, delivered as the answer itself."""
    answer = "<!doctype html>\n<html>\n<head><title>x</title></head>\n<body>hi</body>\n</html>"
    prose, files = extract_files(answer)
    assert [f.filename for f in files] == ["answer.html"]
    assert files[0].content.startswith("<!doctype html>")
    assert prose == ""


def test_a_raw_json_document_with_no_fence_is_one_file():
    answer = '{"traits": ["direct", "curious"], "name": "Vaskes"}'
    prose, files = extract_files(answer)
    assert [f.filename for f in files] == ["answer.json"]
    assert prose == ""


# --- the things that are not files ----------------------------------------

def test_the_executed_tool_trace_is_not_a_file():
    """A transcript of what the model ran is a log, not a deliverable."""
    answer = "```bash\n$ ls -la\n$ cat notes.txt\n```\n\nГотово, всё посмотрел."
    prose, files = extract_files(answer)
    assert files == []
    assert prose == answer


def test_a_shell_script_is_still_a_file():
    """The trace is recognised by its shape, not by its tag alone: a
    script has no "$ " in front of its lines."""
    answer = "```bash\n#!/bin/sh\necho hi\n```"
    _, files = extract_files(answer)
    assert [f.filename for f in files] == ["answer.sh"]


def test_an_unterminated_block_stays_in_the_text():
    """The answer was cut off mid-block. What is left is a fragment, and
    a fragment is not a file."""
    answer = "Вот код:\n\n```python\nprint(1)\nprint(2)"
    prose, files = extract_files(answer)
    assert files == []
    assert prose == answer


def test_prose_without_a_block_is_untouched():
    """A user who asked a question must not see a different answer
    because a feature was switched on next to it."""
    answer = "Просто текст. И `code.span` тоже не файл."
    prose, files = extract_files(answer)
    assert files == []
    assert prose == answer


def test_an_empty_answer_is_an_empty_answer():
    assert extract_files("") == ("", [])
    assert extract_files(None) == ("", [])


# --- the names ------------------------------------------------------------

def test_a_heading_names_the_block_under_it():
    answer = "### config.yaml\n\n```yaml\nkey: value\n```"
    _, files = extract_files(answer)
    assert [f.filename for f in files] == ["config.yaml"]


def test_a_name_with_its_own_extension_keeps_it():
    """The name the model wrote is more specific than the tag."""
    answer = "```python\n# deploy.sh\nprint(1)\n```"
    _, files = extract_files(answer)
    assert [f.filename for f in files] == ["deploy.sh"]


def test_an_unknown_tag_becomes_a_text_file():
    """The tag is the model's vocabulary. Guessing an extension would
    put a .py name on something that is not Python."""
    answer = "```brainfuck\n+++.\n```"
    _, files = extract_files(answer)
    assert [f.filename for f in files] == ["answer.txt"]


def test_two_files_with_the_same_extension_are_told_apart():
    answer = '```json\n{"a": 1}\n```\n\n```json\n{"b": 2}\n```'
    _, files = extract_files(answer)
    assert [f.filename for f in files] == ["answer.json", "answer-2.json"]


def test_a_path_in_a_name_is_reduced_to_its_last_segment():
    """A document arrives in a chat, not in a directory tree."""
    answer = "```python\n# src/app/main.py\nprint(1)\n```"
    _, files = extract_files(answer)
    assert [f.filename for f in files] == ["main.py"]


def test_a_heading_below_a_block_is_not_the_name_of_the_block_above():
    """The heading names the *next* block. Reading it as this block's
    name would give two files in one answer the same name."""
    answer = ('## index.html\n\n```html\n<html></html>\n```\n\n'
              '## style.css\n\n```css\nbody {}\n```')
    _, files = extract_files(answer)
    assert [f.filename for f in files] == ["index.html", "style.css"]
