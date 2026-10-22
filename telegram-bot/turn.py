"""One LLM turn: persist, and undo the persist if the turn does not finish.

TECH-DEBT #4. The rollback path existed in all four handlers — handle_text,
handle_photo, handle_voice, handle_document — as a hand-copied block:

    except Exception:
        if persisted:
            try:
                await asyncio.to_thread(store.delete_last_user_message, ...)
            except Exception as rb:
                print(...)

Four copies of the one piece of handler code where a mistake loses a user
message. If a fix lands in three of them, the fourth leaves a message
persisted with no assistant reply — a conversation the user can see half of,
which is worse than no message at all.

So the ledger owns the flag and the undo. Callers do:

    async with llm_turn(chat_id, thread_id, "handle_voice") as turn:
        await turn.persist(transcript, sender_name=sender)
        response = await call_llama(...)
    # committed on clean exit, rolled back on any exception

The `finally` in every handler collapses into this context manager, which
means a new handler cannot forget the rollback: it is not written, so it
cannot be wrong.
"""

from __future__ import annotations

import asyncio

import storage

from logconf import get_logger
log = get_logger(__name__)


class Turn:
    """Persist/rollback bookkeeping for a single LLM turn."""

    def __init__(self, chat_id: int, thread_id: str, label: str,
                 store=None):
        """`store` is optional and defaults to the module-level singleton.

        The six modules that used to say `from storage import get_store` all
        bound the function object at import time, so patching the owner
        reached none of them and every test had to patch all six. Resolving
        through the module fixes the patch point; taking the store outright
        removes the need to patch anything for a caller that already has one.
        """
        self.chat_id = chat_id
        self.thread_id = thread_id
        self.label = label
        self._store = store
        self.persisted = False
        #: How many user rows this turn wrote. Not a bool, because handle_photo
        #: used to write more than one, and rolling back "the" message without
        #: saying which one is how orphan rows happen.
        self.rows = 0
        #: Row ids this turn actually wrote, so a rollback deletes its own
        #: rows rather than "whatever is newest in the thread". 2026-10-22.
        self._row_ids: list[int] = []

    async def persist(self, content, *, role: str = "user",
                      sender_name: str | None = None) -> None:
        """Write the user turn. Sets the flag BEFORE awaiting.

        The flag goes first on purpose: if persist raises halfway, `persisted`
        is already true and the rollback attempts a delete rather than
        assuming nothing was written. The safe direction is to attempt an undo
        that might be a no-op, never to skip an undo that was needed.
        """
        from persistence import persist as _persist
        self.persisted = True
        row_id = await _persist(self.chat_id, self.thread_id, role, content,
                                sender_name=sender_name)
        self.rows += 1
        if row_id is not None:
            self._row_ids.append(int(row_id))

    async def rollback(self, reason: str) -> bool:
        """Undo the persisted rows. Never raises.

        Returns True if a row was actually deleted. Safe to call twice: the
        second call is a no-op rather than deleting an unrelated message.
        """
        if not self.persisted or self.rows == 0:
            return False
        store = self._store or storage.get_store()
        try:
            if self._row_ids:
                # 2026-10-22: delete exactly what this turn wrote. See
                # Turn.persist. The fallback below is only for a persist
                # that never returned an id.
                deleted = await asyncio.to_thread(
                    store.delete_message_ids, self.chat_id,
                    self.thread_id, self._row_ids,
                )
            else:
                deleted = await asyncio.to_thread(
                    store.delete_last_user_message, self.chat_id,
                    self.thread_id,
                )
        except Exception as rb:
            # The DB refused. There is no second undo, so the only thing left
            # is to make the failure loud — a silent half-rollback is how you
            # end up debugging "why does the bot think I said that".
            log.warning(f"[{self.label}] ROLLBACK FAILED for "
                f"{self.chat_id}/{self.thread_id}: "
                f"{type(rb).__name__}: {rb}. The user message is still in the "
                f"history with no assistant reply.")
            return False
        if not deleted:
            # 2026-10-22: `deleted` was computed and never read, so a
            # rollback that removed nothing returned True, logged success
            # and cleared `persisted` -- a silent false success, in the
            # function whose own except branch says a silent half-rollback
            # is how you end up debugging "why does the bot think I said
            # that". Say nothing was deleted and leave the turn marked.
            log.warning(f"[{self.label}] rollback found nothing to delete "
                f"for {self.chat_id}/{self.thread_id} ({reason}); "
                f"rows={self.rows} ids={self._row_ids}")
            return False
        log.info(f"[{self.label}] rolled back persisted turn for "
            f"{self.chat_id}/{self.thread_id} ({reason})")
        # All-or-nothing. With id-based deletion the whole set goes, so a
        # counter that decrements by one would report a second successful
        # rollback for a turn that is already gone.
        self._row_ids.clear()
        self.rows = 0
        self.persisted = False
        return True


class _Ledger:
    __slots__ = ("turn",)

    def __init__(self, turn: Turn):
        self.turn = turn

    async def __aenter__(self) -> Turn:
        return self.turn

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        if exc_type is not None:
            # Rolls back and swallows. Handlers already report the error to
            # the user themselves; re-raising here would double-report and,
            # in the dispatch task, would be logged a second time as an
            # unhandled task failure.
            await self.turn.rollback(f"exception {exc_type.__name__}")
            return True
        return False


def llm_turn(chat_id: int, thread_id: str, label: str) -> _Ledger:
    """`async with llm_turn(...) as turn:` — commits on exit, rolls back on raise."""
    return _Ledger(Turn(chat_id, thread_id, label))