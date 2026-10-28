"""Files the model asked for, waiting for the answer to carry them.

The operator's rule (2026-10-28): a file made for the operator goes out
**with the answer to the user** -- never in the middle of the turn.

What happened before: the `send_file` tool delivered the document the
moment the model called it.  The model was still working at that point
-- its reasoning was still streaming into the chat -- so the file
appeared between two thoughts, in the middle of a sentence the user was
reading.  The file was wanted; the timing was the defect.

So the tool does the expensive part -- the bytes come out of the
sandbox over the file door, once, hashed on the way -- and *stages*
what it fetched.  The handler sends the staged files after the answer
text, and the temp copy goes away with the delivery.

The bucket is keyed by the turn: (chat id, the id of the user's
message).  Two turns in the same chat therefore cannot hand each other
a file, and a turn that was stopped drops its files instead of leaving
them for the next one.

The module holds no Telegram code and no I/O beyond removing a temp
file: it is a shelf, and the shelf is the whole point -- the delivery
belongs to the answer, not to the tool call.
"""
from __future__ import annotations

import os
import time
from typing import Any, NamedTuple

from logconf import get_logger

log = get_logger(__name__)

__all__ = ["StagedFile", "stage", "take", "discard", "staged_count",
           "clear_all"]


class StagedFile(NamedTuple):
    """One fetched file, waiting for the answer to carry it.

    `ledger` is the tool's ledger entry, kept by reference: the channel's
    own id for the file is only known after the delivery, and the ledger
    is where that id belongs.  Mutating the dict here updates the turn's
    ledger, which is the same object.
    """

    path: str          # the temp file with the bytes
    name: str          # what the chat should call it
    size: int
    sha256: str
    ledger: Any = None


#: (chat id, user message id) -> [StagedFile, ...]
_STAGED: dict[tuple, list[StagedFile]] = {}

#: (chat id, user message id) -> when the bucket was filled
_FILLED_AT: dict[tuple, float] = {}

#: A bucket nobody collected within this many seconds is litter: the
#: turn was stopped, or the bot was restarted between the staging and
#: the delivery.  An hour is far longer than any turn takes, and short
#: enough that the temp copy does not sit in the container forever.
_STALE_SECONDS = 3600.0


def stage(key, item: StagedFile) -> None:
    """Put a fetched file on the shelf, waiting for the answer."""
    key = tuple(key)
    _prune()
    _STAGED.setdefault(key, []).append(item)
    _FILLED_AT.setdefault(key, time.monotonic())
    log.info(f"[staged] {item.name} ({item.size} bytes) waits for the answer")


def take(key) -> list[StagedFile]:
    """The turn's files, and no longer the turn's files."""
    key = tuple(key)
    _FILLED_AT.pop(key, None)
    return _STAGED.pop(key, [])


def discard(key) -> None:
    """Drop the turn's files, and the temp copies behind them."""
    key = tuple(key)
    _FILLED_AT.pop(key, None)
    for item in _STAGED.pop(key, []):
        _unlink(item.path)


def _prune() -> None:
    """Empty the buckets nobody came back for."""
    now = time.monotonic()
    for key, filled in list(_FILLED_AT.items()):
        if now - filled > _STALE_SECONDS:
            log.info(f"[staged] {key} was never collected; the temp copy goes")
            discard(key)


def staged_count(key) -> int:
    """How many files the turn is holding.  For the tests."""
    return len(_STAGED.get(tuple(key), []))


def clear_all() -> None:
    """Empty every bucket.  For the tests: the shelf is process-wide, so a
    test that stages a file and never delivers it must not hand that
    file to the next test."""
    for key in list(_STAGED):
        discard(key)


def _unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass
