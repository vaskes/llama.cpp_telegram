"""The transcript must not deliver a file the chat already has.

Measured on production, 2026-10-25.  The operator asked for an HTML
file ("создай простой html файл с простым содержимым и пришли мне").
The model wrote it in the sandbox with a heredoc, called `send_file`,
and the document arrived -- 595 bytes, the right bytes.  Then the chat
got a second message: the transcript of the heredoc, which is the same
HTML again, once inside the command and once inside the output.

Two copies of the file, one of them 595 bytes of noise in a code
block, for a user who asked for a file.  The transcript exists because
the operator asked for the console output in the chat (2026-10-24),
and that rule is about the case where the output is the ONLY place the
user can see what the model did.  It is not a rule about repeating a
deliverable that is already in the chat as a document.

The shapes below are measured, not invented:

  * the command -- from the production log of that turn, a heredoc
    that writes the file and then lists it;
  * the output -- from the live sandbox, through the same proxy the
    bot talks to.  The sandbox answers with the pane, and the pane
    shows what was TYPED (with the shell's own "> " continuation
    marker), so the file is in the output twice over;
  * the tool result -- the ledger the `send_file` executor writes.

The negative half matters as much as the positive one: the transcript
stays in the chat when no file was delivered, it stays when the
delivery failed, and it stays in what the bot REMEMBERS even when it
is kept out of the chat.
"""
import sys
from pathlib import Path

BOT = Path(__file__).resolve().parent.parent / "telegram-bot"
if str(BOT) not in sys.path:
    sys.path.insert(0, str(BOT))

from llm_result import unwrap_answer  # noqa: E402


# --- the measured shapes --------------------------------------------------

#: The command the model ran, from the production log of 2026-10-25.
HEREDOC_COMMAND = (
    "cat > /workspace/hello.html <<'EOF'\n"
    "<!DOCTYPE html>\n"
    '<html lang="ru">\n'
    "<head>\n"
    '  <meta charset="UTF-8">\n'
    "  <title>Привет от Гадьи Петровичны</title>\n"
    "</head>\n"
    "<body>\n"
    "  <h1>Привет, Васюальы!</h1>\n"
    "</body>\n"
    "</html>\n"
    "EOF\n"
    "ls -l /workspace/hello.html"
)

#: What the sandbox answered, through the proxy the bot uses.  The pane
#: shows the typed command, the heredoc lines with the shell's "> "
#: marker, and the listing.
HEREDOC_STDOUT = (
    "ubuntu@da68f168b2ac:/workspace$ cat > /workspace/hello.html <<'EOF'\n"
    "> <!DOCTYPE html>\n"
    '> <html lang="ru">\n'
    "> <head>\n"
    '>   <meta charset="UTF-8">\n'
    ">   <title>Привет от Гадьи Петровичны</title>\n"
    "> </head>\n"
    "> <body>\n"
    ">   <h1>Привет, Васюальы!</h1>\n"
    "> </body>\n"
    "> </html>\n"
    "> EOF\n"
    "ubuntu@da68f168b2ac:/workspace$ ls -l /workspace/hello.html\n"
    "\n"
    "-rw-r--r-- 1 ubuntu ubuntu 595 Oct 25 19:19 "
    "/workspace/hello.html"
)

#: What the model answered in the end.
MODEL_ANSWER = (
    "Готово! Создал файл hello.html в песочнице и отправил его в чат."
)

#: The ledger the `send_file` executor writes on a delivered file.
DELIVERED_LEDGER = {
    "name": "send_file",
    "args": {"path": "/workspace/hello.html"},
    "iter": 1,
    "size": 595,
    "sha256": "9f2c" + "0" * 60,
    "file_id": "BQACAgIAA",
}

#: The ledger of a bash command.
BASH_LEDGER = {
    "name": "bash_command",
    "args": {"command": HEREDOC_COMMAND},
    "iter": 0,
    "stdout": HEREDOC_STDOUT,
}


def _turn(executed):
    """A finished turn: the model's text and what the tools did."""
    return {"text": MODEL_ANSWER, "executed_tools": executed}


# --- the rule -------------------------------------------------------------

def test_the_transcript_is_kept_out_of_the_chat_when_a_file_was_delivered():
    """The chat gets the model's answer, not a second copy of the file."""
    answer = unwrap_answer(_turn([BASH_LEDGER, DELIVERED_LEDGER]))

    assert "<!DOCTYPE html>" not in answer.text
    assert "cat > /workspace/hello.html" not in answer.text
    assert MODEL_ANSWER in answer.text


def test_the_transcript_stays_in_the_chat_when_no_file_was_delivered():
    """The operator's rule of 2026-10-24: the console output is in the chat."""
    answer = unwrap_answer(_turn([BASH_LEDGER]))

    assert "$ cat > /workspace/hello.html" in answer.text
    assert "595" in answer.text


def test_a_delivery_that_failed_keeps_the_transcript():
    """A file that did not arrive leaves the output as the only witness.

    A failed `send_file` writes no ledger at all (the executor returns
    the failure as its result text), and a ledger without a hash is not
    a delivery either.  Neither may switch the transcript off.
    """
    failed = {"name": "send_file", "args": {"path": "/workspace/hello.html"},
              "iter": 1, "text": "[send_file: sending failed]"}
    answer = unwrap_answer(_turn([BASH_LEDGER, failed]))

    assert "$ cat > /workspace/hello.html" in answer.text


def test_the_bot_still_remembers_the_transcript():
    """Delivery and memory are two artefacts: the chat hides it, the history keeps it.

    The next turn has to be able to say what the model did, and the
    answer the bot stores is `raw`.
    """
    answer = unwrap_answer(_turn([BASH_LEDGER, DELIVERED_LEDGER]))

    assert "$ cat > /workspace/hello.html" in answer.raw
    assert "<!DOCTYPE html>" in answer.raw
    assert "<!DOCTYPE html>" not in answer.text


def test_files_out_of_the_answer_are_not_a_delivered_file():
    """The files the answer carries come from the model's own text.

    They are a different channel: the transcript cannot be showing
    them a second time, so it stays in the chat.
    """
    text = ("Вот файл:\n\n```html\n<!doctype html>\n<html></html>\n```\n")
    result = {"text": text,
              "executed_tools": [BASH_LEDGER]}
    answer = unwrap_answer(result)

    assert [f.filename for f in answer.files] == ["answer.html"]
    assert "$ cat > /workspace/hello.html" in answer.text


def test_the_measured_turn_delivers_the_file_once():
    """The whole measured turn: one file, one answer, no copy of the file.

    This is the operator's case, end to end through the answer the
    chat would show.  The file itself went out as a document from the
    `send_file` tool; what is checked here is the text that came after
    it.
    """
    answer = unwrap_answer(_turn([BASH_LEDGER, DELIVERED_LEDGER]))

    text_messages = [answer.text]
    for message in text_messages:
        assert "<!DOCTYPE html>" not in message
        assert "html lang=" not in message
    assert answer.text.strip() == MODEL_ANSWER
