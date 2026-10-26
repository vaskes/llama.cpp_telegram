"""Chunked summaries of the old history -- the T70.5 half of the store.

Split out of `storage.py` on 2026-10-27, and the reason is a size, not a
taste. `storage.py` was 940 lines against a 1000-line module limit
(CODEBOOK S-6), and the feature that followed -- recording the Telegram id
of every message the bot stores, so a message can be found again and taken
back out of the chat -- needed ninety more lines than that. A module over
the limit is debt that has to be registered, and the register is not the
place a new feature goes.

So the summaries moved, and this is the concept that moved with them: the
old part of the history is stored twice over, as messages (the source of
truth, never deleted) and as chunk summaries (the compressed form the
model actually reads). The link between the two is messages.summary_id.

`SummaryStore` is a mixin, not a separate object, and that is deliberate:
every call site in the tree says `store.get_summaries(chat_id, thread_id)`
and stays exactly as it was. A move that rewrites the call sites is a
refactor of the callers, and this one is a move.

The methods need two things from the store they are mixed into: the
connection (`self._conn()`) and the clock (`self._sql_now`). Both are set
by `storage.Storage.__init__`.
"""

class SummaryStore:
    """The summary half of the store, mixed into `storage.Storage`.

    The methods below are the ones that write and read the chunk
    summaries of T70.5. They are here because they are one concept with
    one owner, and because `storage.py` had no room left for them.
    """

    # === T70.5: chunked summaries =========================================
    #
    # The old part of the history is stored twice over: as messages (the
    # source of truth, never deleted) and as chunk summaries (the compressed
    # form the model actually reads). The link between the two is
    # messages.summary_id, and it is written in the SAME transaction as the
    # summary row -- a marker without a summary is a hole in the memory, and
    # a summary without a marker means the same history gets summarised
    # again at the next re-anchor.

    def save_summary_chunk(self, chat_id: int, thread_id: str, text: str,
                           first_msg_id: int, last_msg_id: int,
                           n_messages: int,
                           first_created_at: float = 0.0,
                           last_created_at: float = 0.0) -> int:
        """Store one chunk summary and mark its messages in one transaction.

        Returns the new summary id. The messages in [first_msg_id,
        last_msg_id] get `summary_id` pointing at it, which is the operator's
        "СВЕДЕНО" field with the link to the summary that was made.

        The range is a contiguous id range, which is what a chunk is: a
        contiguous slice of the pending history in chronological order.

        The timestamps say when the chunk was. The context assembler uses
        them to decide whether the chunk is still outside the window -- the
        anchor only ever moves forward, so it always is, but the check is
        what keeps a summary from being loaded next to the very messages it
        replaces if that ever changes.
        """
        c = self._conn()
        try:
            c.execute("BEGIN")
            cur = c.execute(
                "INSERT INTO summaries (chat_id, thread_id, text, n_messages, "
                "first_msg_id, last_msg_id, first_created_at, "
                "last_created_at, created_at) "
                f"VALUES (?, ?, ?, ?, ?, ?, ?, ?, {self._sql_now})",
                (chat_id, thread_id, text, n_messages, first_msg_id,
                 last_msg_id, first_created_at, last_created_at),
            )
            summary_id = int(cur.lastrowid)
            c.execute(
                "UPDATE messages SET summary_id = ? "
                "WHERE chat_id = ? AND thread_id = ? "
                "AND id >= ? AND id <= ? AND summary_id IS NULL",
                (summary_id, chat_id, thread_id, first_msg_id, last_msg_id),
            )
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise
        return summary_id

    def get_summaries(self, chat_id: int, thread_id: str) -> list[dict]:
        """Every chunk summary of this thread, oldest chunk first.

        Ordered by first_msg_id, not by created_at: the ORDER of the
        summaries IS the order of the history, and the clock is not a
        reliable witness of it (a re-anchor can summarise an old chunk
        after a new one).
        """
        c = self._conn()
        rows = c.execute(
            "SELECT id, text, n_messages, first_msg_id, last_msg_id, "
            "first_created_at, last_created_at, created_at FROM summaries "
            "WHERE chat_id = ? AND thread_id = ? ORDER BY first_msg_id ASC",
            (chat_id, thread_id),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_messages_since(self, chat_id: int, thread_id: str,
                           since: float, limit: int) -> list[dict]:
        """The newest `limit` messages newer than `since`, chronological.

        The fresh half of the context. It is fetched by age rather than by
        "the last N rows" because the two are different questions: the
        message cap and the age window are independent policies, and the
        fresh part of the context is "the newest messages that are also
        inside the window".
        """
        c = self._conn()
        rows = c.execute(
            """
            SELECT id, role, content, sender_name, rating, created_at
            FROM (
                SELECT id, role, content, sender_name, rating, created_at
                FROM messages
                WHERE chat_id = ? AND thread_id = ? AND created_at >= ?
                ORDER BY id DESC
                LIMIT ?
            ) ORDER BY id ASC
            """,
            (chat_id, thread_id, since, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_pending_old(self, chat_id: int, thread_id: str, before: float,
                        limit: int, *, newest_first: bool = False) -> list[dict]:
        """Old messages that no valid summary stands for, chronological.

        "Old" = created before `before` (the window anchor). "No valid
        summary stands for it" = its `summary_id` is NULL, or points at a
        summary row that no longer exists -- an orphaned marker must not
        make the message disappear from the context, so those come back as
        raw and get summarised again at the next re-anchor.

        This is the "load only the messages that are not summarised" half of
        the operator's rule; the other half is get_summaries().

        `newest_first` decides WHICH END the limit cuts, and the two callers
        need opposite ends:

          * the summariser takes the OLDEST pending, so that a pass which
            hits the ceiling makes progress instead of re-summarising the
            same newest chunk forever;
          * the context assembler takes the NEWEST pending, because the raw
            tail is the recent past and the recent past is what survives a
            budget cut.

        Either way the result is chronological.
        """
        c = self._conn()
        order = "DESC" if newest_first else "ASC"
        rows = c.execute(
            f"""
            SELECT id, role, content, sender_name, rating, created_at,
                   summary_id
            FROM (
                SELECT id, role, content, sender_name, rating, created_at,
                       summary_id
                FROM messages
                WHERE chat_id = ? AND thread_id = ? AND created_at < ?
                  AND (summary_id IS NULL OR summary_id NOT IN (
                        SELECT id FROM summaries
                        WHERE chat_id = ? AND thread_id = ?
                          AND last_created_at < ?))
                ORDER BY id {order}
                LIMIT ?
            ) ORDER BY id ASC
            """,
            (chat_id, thread_id, before, chat_id, thread_id, before, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def count_summarised(self, chat_id: int, thread_id: str) -> tuple[int, int]:
        """(messages covered by a summary, number of summaries).

        For /stats: how much of the old history is compressed and how many
        chunks it is spread over.
        """
        c = self._conn()
        row = c.execute(
            "SELECT COUNT(*) FROM messages m "
            "WHERE m.chat_id = ? AND m.thread_id = ? AND m.summary_id IN "
            "(SELECT id FROM summaries WHERE chat_id = ? AND thread_id = ?)",
            (chat_id, thread_id, chat_id, thread_id),
        ).fetchone()
        n_msgs = int(row[0]) if row else 0
        row = c.execute(
            "SELECT COUNT(*) FROM summaries WHERE chat_id = ? AND thread_id = ?",
            (chat_id, thread_id),
        ).fetchone()
        n_sums = int(row[0]) if row else 0
        return n_msgs, n_sums
