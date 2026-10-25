"""The `send_file` tool: a file from the sandbox workspace to the chat.

The operator's request (2026-10-24), after the answer-files feature
was already live: "сделай произвольный бинарный файл скажем в 10кб и
пришли мне". The bot made the file -- the model ran `dd if=/dev/urandom
of=random10k.bin` in the sandbox and the file was there, 10240 bytes --
and the user got nothing usable: the model's answer pasted 180 lines of
base64 into the chat and the file feature turned the console output
into `.txt` documents.

The reason is structural, not a bug in one place: the only channel from
the bot to the chat was the model's TEXT. A code block becomes a
document (the answer-files feature) and a binary has no text form, so
the bytes could not travel at all.

This tool is the other channel. The model creates the file in the
sandbox, calls `send_file` with its path, and the bot fetches the bytes
through the same side-car the model writes into and sends them as a
Telegram document. The reach is exactly the model's own reach: the
sandbox user can already read whatever the model can read, so this
hands the model nothing it did not have.

The bytes come through the sandbox's file door (`GET /files?path=`),
which is a stream of raw bytes, not text. The previous route -- base64
through the terminal pane -- was bounded by the pane's scrollback, and
when the pane overflowed it dropped the head silently, so the payload
decoded into garbage. The file the model produced has to arrive as the
file the model produced, and a binary has no text form to fall back on.

The definition lives here, apart from the dispatcher, for the same
reason `personal_tools/bash_command.py` holds its own definition: one
place decides what the model is TOLD it can call, and one place
implements it.
"""
from __future__ import annotations

__all__ = ["send_file_tool_def"]


def send_file_tool_def() -> dict:
    """The definition the model is shown for `send_file`.

    A function and not a bare constant because the architecture map
    checks the public surface against what the module actually
    defines, and a constant is not a name a reader can follow.
    """
    return {
        "type": "function",
        "function": {
            "name": "send_file",
            "description": (
                "Send a file from the sandbox workspace to the chat as a "
                "Telegram document. Use it when the user asks for a FILE: "
                "an archive, an image, a generated script, a binary blob, "
                "anything you have created with bash_command. Create the "
                "file first, then call this with its path. Do NOT paste the "
                "contents into the chat and do NOT send base64 -- the user "
                "gets the file itself, and the bytes do not have to fit in "
                "a message. The file has to be finished and on disk: the "
                "sandbox serves files that exist, not downloads in flight. "
                "If the file is over the sandbox's own size limit the call "
                "comes back with its real size, so read the answer before "
                "retrying."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "Absolute path of the file in the sandbox "
                            "workspace, e.g. /workspace/report.zip."
                        ),
                    },
                },
                "required": ["path"],
            },
        },
    }
