"""What happens when the user presses Stop.

TECH-DEBT #13, second block. The tail of an aborted turn was written out
seven times: four primary paths and three empty-response retries, each
one logging, rolling back the transcript, editing the thinking message to
"stopped" and returning.

Of those seven lines the one that matters is `turn.rollback('aborted')`.
A transcript that was persisted for a turn the user cancelled is the
reason the next message gets answered about something that no longer
happened. Whether a given handler rolls back is not a stylistic choice
that should be re-made at each site: image turns have nothing in the
transcript to undo, so photo passes no `turn`, and the rollback for the
other three is not optional.

The log text differs between the two attempts -- "ABORTED on retry" so an
operator can tell a cancelled first POST from a cancelled retry -- so
`attempt` is a parameter rather than two functions. It reproduces both
existing messages exactly; a message only exists to be grepped, and
rewriting one to unify it would break the runbook for no gain.

The caller still returns; this cannot return for it.
"""
from __future__ import annotations

from logconf import get_logger

log = get_logger(__name__)

__all__ = ["finalize_aborted_turn"]


async def finalize_aborted_turn(*, label, response, abort_event, thinking,
                                turn=None, attempt="primary"):
    """End an aborted turn: log it, undo its transcript, show "stopped".

    `label` is the handler name, and it is part of every log line, so the
    runbook can attribute an abort to a handler. `attempt` is
    "primary" or "retry". `turn=None` means there is no persisted turn to
    roll back -- photo turns never get one, the image was never written.
    """
    if attempt not in ("primary", "retry"):
        raise ValueError(f"attempt must be 'primary' or 'retry', got {attempt!r}")
    is_retry = attempt == "retry"

    if response is not None and abort_event.is_set():
        # The post won the race: call_llama returned a full answer, and
        # Stop was pressed while it was in flight. It still gets dropped --
        # the user asked to cancel.
        log.info(f"[{label}] ABORTED {'on retry ' if is_retry else ''}via Stop; "
                 f"discarding {len(response)}-char response"
                 f"{'' if is_retry else ' (post-won race)'}")
    else:
        log.info(f"[{label}] ABORTED "
                 f"{'on retry,' if is_retry else 'by user via Stop button,'} "
                 f"moving to next")

    if turn is not None:
        await turn.rollback('aborted')

    try:
        await thinking.edit_text('⏹ Stopped')
    except Exception:
        pass
