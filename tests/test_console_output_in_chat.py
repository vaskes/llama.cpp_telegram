"""The console output goes into the chat. It is not a file.

Measured on production, 2026-10-24. The operator asked the bot for a
10 KB binary file. The model made the file in the sandbox and then
printed what it had: a listing, an md5, a base64 head, an `xxd` dump.
It pasted that output into code blocks, and the answer-files feature --
which takes code blocks out of the chat and delivers them as documents
-- delivered the CONSOLE OUTPUT as `answer.txt` and `answer-2.txt`.

So the user got the output as a file, the chat without it, and no binary
at all. The output was not a deliverable; it was output.

Three things are pinned here:

  * the chat shows what the commands printed, not only what they were;
  * a code block that repeats the console output stays in the chat
    instead of becoming a document;
  * the transcript itself (the bot's own ```bash block) is never a file,
    even now that it carries the output.
"""
import sys
from pathlib import Path

import pytest

BOT_DIR = Path(__file__).resolve().parent.parent / "telegram-bot"
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

import llm_dispatch  # noqa: E402
from llm_artifact import extract_files  # noqa: E402
from llm_dispatch import DispatchDeps, execute_tool  # noqa: E402
from llm_result import unwrap_answer  # noqa: E402


#: What the sandbox printed on 2026-10-24, in the shape it printed it.
STDOUT = (
    "-rw-r--r-- 1 agent agent 10240 Sep 24 20:14 random10k.bin\n"
    "e6e347b8b54cb9cedd747b5026d38ad0  random10k.bin\n"
    "---\n"
    "00000000: 9c8f 2b1a 7d3e 5501 2c9a 4f6b 88d1 e0a3  |.........?U.,.K..|\n"
    "00000010: 1b77 92e4 0d55 8a31 c6f0 44b2 7e9d 3a08  |.w...U.1...D~.:|\n"
)


def _answer_with_console_output() -> str:
    """The shape the model actually produced: the output, pasted."""
    return (
        "[llm] Вот, готово. Случайные 10 КБ из `/dev/urandom`.\n\n"
        "**Начало base64:**\n\n"
        "```text\n"
        "00000000: 9c8f 2b1a 7d3e 5501 2c9a 4f6b 88d1 e0a3  |.........?U.,.K..|\n"
        "00000010: 1b77 92e4 0d55 8a31 c6f0 44b2 7e9d 3a08  |.w...U.1...D~.:|\n"
        "```\n\n"
        "Полный base64 — 180 строк, в чат не влезу целиком.\n"
    )


def test_console_output_pasted_by_the_model_stays_in_the_chat():
    prose, files = extract_files(_answer_with_console_output(),
                                 tool_outputs=[STDOUT])
    assert files == [], (
        f"the console output became a file: {[f.filename for f in files]}")
    assert "00000000: 9c8f" in prose, (
        "the console output has to stay in the chat -- the operator's "
        "rule, 2026-10-24")


def test_console_output_without_a_tool_leaves_the_answer_alone():
    """No tool ran, so the bot has no output to compare against, and the
    block is whatever the model wrote -- a file, as before."""
    prose, files = extract_files(_answer_with_console_output())
    assert len(files) == 1
    assert files[0].filename.startswith("answer")


def test_a_real_file_is_still_a_file():
    """The rule is about console output, not about text blocks."""
    answer = (
        "Вот страница.\n\n"
        "```html\n"
        "<!doctype html>\n"
        "<html><body>hi</body></html>\n"
        "```\n"
    )
    prose, files = extract_files(answer, tool_outputs=[STDOUT])
    assert [f.filename for f in files] == ["answer.html"]
    assert "Вот страница." in prose


def test_a_command_the_model_wrote_is_still_a_file():
    """The command is not output: it is what the model wants to hand
    over. The output is what the command PRINTED."""
    answer = (
        "Скрипт:\n\n"
        "```sh\n"
        "mkdir -p /workspace/out\n"
        "dd if=/dev/urandom of=/workspace/out/blob.bin bs=1024 count=10\n"
        "```\n"
    )
    prose, files = extract_files(answer, tool_outputs=[STDOUT])
    assert [f.filename for f in files] == ["answer.sh"]


def test_the_transcript_shows_the_output():
    """The chat gets the commands AND what they printed."""
    result = {
        "text": "Готово, файл 10 КБ.",
        "executed_tools": [
            {"name": "bash_command",
             "args": {"command": "dd if=/dev/urandom of=random10k.bin"},
             "iter": 0,
             "stdout": STDOUT},
        ],
    }
    answer = unwrap_answer(result)
    assert "dd if=/dev/urandom of=random10k.bin" in answer.text
    assert "e6e347b8b54cb9cedd747b5026d38ad0" in answer.text, (
        "the console output has to be in the chat, not only the command")
    assert answer.files == []


def test_the_transcript_is_not_a_file():
    """The bot's own ```bash block is a log, never a deliverable -- and
    it now carries the output, so the shape of the check changed."""
    result = {
        "text": "Готово.",
        "executed_tools": [
            {"name": "bash_command",
             "args": {"command": "uname -a"},
             "iter": 0,
             "stdout": "Linux sandbox 6.8.0 x86_64\n"},
        ],
    }
    answer = unwrap_answer(result)
    assert answer.files == [], (
        f"the transcript became a file: "
        f"{[f.filename for f in answer.files]}")
    assert "```bash" in answer.text


def test_a_model_echo_of_the_transcript_is_not_a_file():
    """A model that repeats the transcript back should not turn it into
    a document either."""
    answer = (
        "Вот что я сделал:\n\n"
        "```bash\n"
        "$ uname -a\n"
        "Linux sandbox 6.8.0 x86_64\n"
        "```\n"
    )
    prose, files = extract_files(answer)
    assert files == []
    assert "uname -a" in prose


async def test_the_ledger_does_not_carry_the_whole_output(monkeypatch):
    """The ledger is written to the database and to the log.

    The sandbox's stdout is one command's output, and on production one
    call measured 118 KB of it. A ledger that carries all of it turns
    every command into a hundred kilobytes of rows and log lines -- and
    the log is a bind-mount that outlives the container. So the ledger
    keeps a bounded piece of it, and says what it dropped.

    Which piece is a decision, and it changed on 2026-10-25 with the
    sandbox's v2.7.0. The stdout used to be a whole terminal pane, so
    the ledger kept the tail: the end was the command that had just
    run. Now the stdout is that command's output and nothing else, and
    the beginning is the part that carries the sandbox's own marker
    ("the beginning of this output no longer fits in the session
    scrollback"). A ledger that keeps the tail throws that marker away
    and shows a fragment as if it were the whole thing.
    """
    output = ("HEAD_MARKER the very first line of the output\n"
              + "filler line\n" * 5000
              + "TAIL_MARKER the command that just ran printed this\n")

    async def fake_call(command, *, session_id=None, timeout=70.0,
                        chat_type="private", chat_id=None):
        return {"ok": True, "stdout": output, "stderr": "",
                "exit_code": 0, "duration_ms": 1}

    import personal_tools.bash_command as bc
    monkeypatch.setattr(bc, "call_bash_command", fake_call)
    monkeypatch.setattr(bc, "is_enabled_for_active_profile", lambda: True)

    deps = DispatchDeps(
        bot=None,
        chat_id=-1004461679108,
        current_message_id=1,
        chat_type="supergroup",
        iteration=0,
        custom_tools={},
        donsetch_tools={},
    )
    outcome = await execute_tool("bash_command", {"command": "echo hi"}, deps)

    stored = outcome.ledger["stdout"]
    assert len(stored) <= llm_dispatch.MAX_LEDGER_STDOUT_CHARS, (
        f"the ledger kept {len(stored)} characters of a "
        f"{len(output)}-character output")
    assert "TAIL_MARKER" in stored, (
        "the ledger must keep the end of the output -- that is what the "
        "command printed last, and what the model quotes back")
    assert "HEAD_MARKER" in stored, (
        "the ledger must keep the head of the output -- that is where the "
        "sandbox's own marker about a lost beginning lives")
    assert "characters were dropped from the middle" in stored, (
        "the ledger shows a cut as if it were the whole output")


def test_the_transcript_shows_both_ends_of_the_output():
    """Same reason as the ledger: the chat shows what the command
    printed, and it has to say when what it shows is not all of it."""
    output = ("HEAD_MARKER the very first line of the output\n"
              + "filler line\n" * 5000
              + "TAIL_MARKER the command that just ran printed this\n")
    result = {
        "text": "Готово.",
        "executed_tools": [
            {"name": "bash_command",
             "args": {"command": "echo hi"},
             "iter": 0,
             "stdout": output},
        ],
    }
    answer = unwrap_answer(result)
    assert "TAIL_MARKER" in answer.text
    assert "HEAD_MARKER" in answer.text, (
        "the transcript dropped the head of the output -- that is where "
        "the sandbox's marker about a lost beginning lives")
    assert "characters were dropped from the middle" in answer.text


async def test_a_quote_from_the_middle_of_a_long_output_stays_in_the_chat(monkeypatch):
    """The production case, 2026-10-30.

    The model's answer quoted the middle of a long command's output.
    The file feature took the quote out of the chat and delivered it as
    `answer.txt` -- the execution artifacts, in a document nobody asked
    for. The model had called no file tool at all: the bot's own
    feature did it, and the model said -- truthfully -- that it had
    not.

    The quote sat in the window the MODEL saw (8000 characters, head
    and tail) and outside the window the ledger kept (2000 characters,
    head and tail). The comparison that tells a quote from a
    deliverable ran against the narrower window, so it could not see
    the quote it was supposed to see.
    """
    filler = "filler line that says nothing in particular\n"
    output = ("HEAD_MARKER the very first line of the output\n"
              + filler * 50
              + "MIDDLE_MARKER the line the model quoted back\n"
              + filler * 50
              + "TAIL_MARKER the command that just ran printed this\n")

    async def fake_call(command, *, session_id=None, timeout=70.0,
                        chat_type="private", chat_id=None):
        return {"ok": True, "stdout": output, "stderr": "",
                "exit_code": 0, "duration_ms": 1}

    import personal_tools.bash_command as bc
    monkeypatch.setattr(bc, "call_bash_command", fake_call)
    monkeypatch.setattr(bc, "is_enabled_for_active_profile", lambda: True)

    deps = DispatchDeps(
        bot=None,
        chat_id=-1004461679108,
        current_message_id=1,
        chat_type="supergroup",
        iteration=0,
        custom_tools={},
        donsetch_tools={},
    )
    outcome = await execute_tool("bash_command", {"command": "echo hi"}, deps)

    # The ledger keeps a bounded piece of the output for the database.
    assert "MIDDLE_MARKER" not in outcome.ledger["stdout"], (
        "this test is about the window the ledger keeps -- the quote has "
        "to sit outside it, or it proves nothing")
    # What the model saw is what the ledger carries for the comparison.
    assert "MIDDLE_MARKER" in outcome.ledger["output"], (
        "the ledger must carry what the model saw, or a quote from the "
        "middle of a long output is invisible to the comparison")

    answer_text = ("Проверила, вот строка из вывода:\n\n"
                   "```\n"
                   "MIDDLE_MARKER the line the model quoted back\n"
                   "```\n")
    answer = unwrap_answer({"text": answer_text,
                             "executed_tools": [outcome.ledger]})
    assert answer.files == [], (
        "the quote is output, not a deliverable: it must stay in the chat "
        "instead of arriving as a document nobody asked for")
    assert "MIDDLE_MARKER" in answer.text, (
        "and the quote stays where it was -- in the chat, with the "
        "model's own words around it")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
