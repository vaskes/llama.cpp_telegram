"""The document handler must say which file arrived.

2026-10-23: the first document this bot ever received in production
raised a question the log could not answer. The shared
_acquire_turn_slot logs the route ('[handle_document] chat_id=...'), but
nothing said WHICH file arrived, so a document that never showed up
looked exactly like a photo run. The user's file had in fact failed to
upload on the client, and the only reason we could tell was that no
getFile call followed.

This pins the entry log: the file name, its size and the caption have
to be visible BEFORE the handler does any work, so a document that is
dropped (no slot, wrong thread) is still visible in the log.

The mutation that must fail this test: deleting the ENTRY log line.
"""
import logging
import sys
from pathlib import Path

import pytest

BOT = Path(__file__).resolve().parent.parent / "telegram-bot"
if str(BOT) not in sys.path:
    sys.path.insert(0, str(BOT))

import handlers  # noqa: E402


class _Doc:
    def __init__(self, file_name, file_size, mime_type="text/markdown"):
        self.file_name = file_name
        self.file_size = file_size
        self.mime_type = mime_type


class _Message:
    def __init__(self, doc):
        self.document = doc
        self.caption = "выполни задание"
        self.message_thread_id = 25


class _Chat:
    id = -1004461679108


class _Update:
    def __init__(self, doc):
        self.message = _Message(doc)
        self.effective_chat = _Chat()


@pytest.mark.asyncio
async def test_the_document_entry_names_the_file(monkeypatch, caplog):
    """The file name has to be in the log even when nothing else happens.

    The slot is stubbed out to None, which is the handler's own drop
    path: the entry log has to be written before that, or a document
    the bot refused is a document nobody can find in the log.
    """
    async def no_slot(update, context, *, label):
        return None

    monkeypatch.setattr(handlers, "_acquire_turn_slot", no_slot)
    u = _Update(_Doc("newton.md", 8192))

    with caplog.at_level(logging.INFO):
        await handlers.handle_document(u, None)

    assert "newton.md" in caplog.text, (
        f"the entry log does not name the file: {caplog.text!r}")
    assert "8192" in caplog.text, (
        f"the entry log does not carry the file size: {caplog.text!r}")
    assert "выполни задание" in caplog.text, (
        f"the entry log does not carry the caption: {caplog.text!r}")


@pytest.mark.asyncio
async def test_a_document_without_a_file_still_logs(monkeypatch, caplog):
    """A handler invoked without a document must not raise.

    The log line reads the document defensively on purpose: the same
    handler is reached by tests and by a mis-dispatched update, and a
    logging line that raises replaces the real error with an
    AttributeError about 'document'.
    """
    async def no_slot(update, context, *, label):
        return None

    monkeypatch.setattr(handlers, "_acquire_turn_slot", no_slot)

    class _Bare:
        caption = None
        message_thread_id = None
        document = None

    class _BareUpdate:
        message = _Bare()
        effective_chat = _Chat()

    with caplog.at_level(logging.INFO):
        await handlers.handle_document(_BareUpdate(), None)

    assert "[handle_document] ENTRY" in caplog.text, (
        f"no entry log at all: {caplog.text!r}")
