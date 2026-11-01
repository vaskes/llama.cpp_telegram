"""Which tool reads a file, and what the model is told about it.

THE MEASUREMENT (production, 2026-11-01, 19:20-19:26 MSK)

The operator sent a 603 675-byte JSON file, had it put in the sandbox,
and asked for it to be read into the model's context WHOLE. Two things
happened, and neither of them was the file door:

  * the model did not try. It read a window size out of a screenshot the
    operator had posted earlier in the chat, did the arithmetic on the
    file's size against that number, and answered that reading the file
    whole is "physically impossible" -- without calling one tool;
  * when it did try, in the next turn, it called `bash_command` with
    `cat`, and the console cut the file TWICE: the side-car at 524 288
    bytes and the bot itself at 8 000 characters. The model saw 1 742
    characters of a 474 071-character file, and then reported on the
    fragment as if it were the file.

The door was there the whole time. `sandbox_read_file` was in the tool
list, the side-car answered 200 with the whole file, and the side-car's
own read budget (1 MiB) was four times the size of the file. Measured
the same day, through the bot's own transport: the file read through
the door grew the prompt from 7 077 to 130 478 tokens -- it arrives
whole, and the engine's window (460 776 tokens) takes it.

So the file was never lost on the way to the model. It was lost on the
way to the TOOL. This module is the bot's answer to that, and it is
one section in the system prompt:

  * it names the door THIS chat has -- and nothing at all when the chat
    has no door, because a name the model cannot call is a name it
    invents;
  * it says the door delivers a file whole and the console cannot;
  * it says the door is the arbiter of whether a file fits, so the
    model does not decide that a file is too big from a number quoted
    in the chat -- the server answers that, by name, with the size and
    the limit.

WHY THE SYSTEM PROMPT AND NOT THE TOOL DESCRIPTION

The description of an imported tool is the SERVER's words, plus one line
saying where the tool came from (mcp_registry._tool_def). The rule that
keeps the imported half honest is that the bot does not put its own
words in a foreign tool's mouth. The system prompt is the bot's own
surface: its rules live there, and they are the rules the model is
supposed to fall back on when the chat says something else.

And the chat DOES say something else. The operator's own message --
"загрузи файл целиком в контекст" -- arrived right after the model had
computed that the file could not fit. A rule the model only meets in
the chat is a rule it can talk itself out of; a rule in the system
prompt is the thing it is supposed to weigh the chat against.

WHY THE TEXT IS NOT IN prompt_text

`prompt_text` holds prose, and prose there is a CONSTANT: the package
has no functions (tests/test_prompts.py asserts it), and every
constant in it is pinned by a hash. This text is not a constant -- it
names the door this chat has, and a chat without a door must not be
told about one. So the text lives here, next to the code that knows
which door there is.
"""
from __future__ import annotations

from personal_tools.sandbox_contract import MCP_MAX_READ_BYTES

__all__ = ["instruction_for", "console_name_for", "CONSOLE_TOOL_NAME"]

#: The section's title, as the model reads it. A rule with a name is a
#: rule the model can point at.
SECTION_TITLE = "Reading a file whole"

#: The bot's own console tool. The door and the console are two ways
#: into the same sandbox, and only one of them delivers a file whole.
CONSOLE_TOOL_NAME = "bash_command"


def console_name_for(tool_defs: list[dict]) -> str | None:
    """The console tool's name, or None when this chat has no console.

    The console is a personal-mode tool, so a chat without it has
    nothing to be told about it.
    """
    names = {t.get("function", {}).get("name") for t in tool_defs}
    return CONSOLE_TOOL_NAME if CONSOLE_TOOL_NAME in names else None


def instruction_for(door_names: tuple[str, ...],
                    console_name: str | None = None) -> str:
    """The section the model reads when this chat has a file door.

    EMPTY when the chat has no door. That is the whole point of the
    parameter: a chat in the public profile has no sandbox and no door,
    and a sentence about a tool it does not have is an invitation to
    call it and be refused.

    `console_name` is the name of the console tool, or None when this
    chat has no console. The console is named only when it is there:
    naming a tool the model cannot call is the same mistake as naming
    a door it does not have.
    """
    if not door_names:
        return ""
    doors = ", ".join(f"`{name}`" for name in sorted(door_names))
    console = (f"The console (`{console_name}`) is not a file door: its "
               "output is capped and the middle is dropped, so a file "
               "printed with `cat` reaches you as a fragment and you "
               "cannot tell what you are missing. "
               if console_name else
               "A command's output is not a file either: it is capped, "
               "and the middle is dropped, so a file printed by a "
               "command reaches you as a fragment and you cannot tell "
               "what you are missing. ")
    return (
        f"\n\n## {SECTION_TITLE}\n\n"
        f"{doors} reads a file out of the sandbox workspace and puts it "
        "into your context WHOLE -- from the first byte to the last -- "
        "up to the server's own read limit, which is "
        f"{MCP_MAX_READ_BYTES} bytes (1 MiB). A file under that limit "
        "arrives whole; a file over it is refused BY NAME, and the "
        "refusal tells you the size and the limit. Do not decide in "
        "advance that a file will not fit: a window size, a limit or an "
        "estimate quoted in the chat is not the server's answer, and "
        "the only way to know is to call the door. Compare the file's "
        "size with the limit above -- the size is usually in the "
        "conversation -- and call the door.\n\n"
        + console
        + "When the user asks you to read a file, to load a file into "
        "your context, or to say what is inside a file, call "
        f"{doors} with that file's path -- not `cat`, not `head`, not "
        "`sed`, and not a command that prints the file. Call it: a "
        "command written out in your answer was never run, so what you "
        "say about the file afterwards is a guess and not a reading.\n"
    )
