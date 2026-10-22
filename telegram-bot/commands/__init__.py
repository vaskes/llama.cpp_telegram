"""Command handlers.

TECH-DEBT #10 moved these out of dispatch.py, which was 1672 lines of
orchestration and routing with ten command handlers wedged between the
two. Re-exported here under their old names, so `dispatch` and the
existing tests reach them the same way.

This package imports nothing from `dispatch`. That is the property that
makes the move possible and the one to check before adding an import:
a command that needs the dispatcher has taken on a dependency it did
not have, and the answer is to pass what it needs in.
"""
from commands.callback import cmd_callback
from commands.start_cmd import cmd_help, reset, start
from commands.stats_cmd import stats
from commands.subtalking import cmd_delsub, cmd_here, cmd_newsub, cmd_sub, cmd_subs

__all__ = [
    "cmd_callback",
    "cmd_delsub",
    "cmd_help",
    "cmd_here",
    "cmd_newsub",
    "cmd_sub",
    "cmd_subs",
    "reset",
    "start",
    "stats",
]
