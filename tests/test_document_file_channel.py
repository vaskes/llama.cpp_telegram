"""A document that arrives in the chat is a file, not a wall of text.

2026-11-01: the operator sent a JSON file and said, twice, that he
wanted it in the workspace as a binary. The bot read the first 16 000
characters of it and pasted them into the prompt instead. The rest of
the file was gone, and nothing in the prompt said that the text in
front of the model was a piece of a file rather than the file. The
model answered from that piece, re-typed the piece into the sandbox,
and reported -- with a byte count and a sha256 -- a file that was not
the file the operator had sent.

So the file channel is the thing under test here, and the assertions
are made where the model reads them:

  * a text document lands in the sandbox, like a binary one does, and
    the bytes that land are the bytes that arrived;
  * the turn carries a POINTER to that file, and says which part of it
    is quoted -- the model is never left guessing whether the text in
    front of it is the file;
  * when the file cannot be stored, the turn says THAT, with a reason.
    There is no silent cut left anywhere.

Every test here drives the real handler (`handle_document`), with only
the file door and the LLM call faked. The unit tests of the file
channel would pass on a tree where the handler never called it, which
is the seam this file exists to close.
"""
import asyncio
import hashlib
import re
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "telegram-bot"))

import handlers  # noqa: E402

#: A document big enough to be cut. The tail is a string that appears
#: NOWHERE else, so "the turn does not contain it" is a statement about
#: the file and not about the vocabulary.
#:
#: The length matters as much as the tail: the old code cut at 16 000
#: CHARACTERS, so a document shorter than that would survive a byte-level
#: corruption untouched -- the tripwire on the bytes would have been
#: green on a tree that mangled every file under 16 000 characters. This
#: one is longer than the cut on purpose.
DOC_BODY = ("{\n  \"scene_id\": 1,\n  \"text\": \"" + ("я" * 20000) +
            "\"\n}\n")
DOC_TAIL = "THE-VERY-LAST-CHARACTERS-OF-THE-FILE"
DOC_BYTES = (DOC_BODY + DOC_TAIL).encode("utf-8")

#: The cut the old code made. A document that fits under it cannot catch
#: a re-typed file, so the document above must be longer than this.
OLD_CUT_CHARS = 16000
assert len(DOC_BODY) > OLD_CUT_CHARS, (
    "the document under test must be longer than the cut the old code "
    f"made ({OLD_CUT_CHARS} characters), or it cannot catch it")

SHORT_TEXT = "заметка на две строки, целиком"
SHORT_BYTES = SHORT_TEXT.encode("utf-8")

BINARY_BYTES = b"\x7fELF\x02\x01\x01\x00" + bytes(range(256)) * 4

#: "Only the first N characters" -- the shape of the sentence that says
#: "this is a piece", with the number left to the knob.
_PIECE = re.compile(r"Only the first \d+ characters")
_PIECE_HEADER = re.compile(r"--- First \d+ characters ---")


# --------------------------------------------------------------- harness --


class _Thinking:
    """The 'thinking' message the handler opens its turn with."""

    message_id = 999

    async def edit_text(self, text, **kw):
        pass

    async def delete(self):
        pass


def _update(doc_bytes, doc_name="characters.json"):
    u = MagicMock()
    u.effective_user.id = 1
    chat = MagicMock()
    chat.id = 1
    chat.type = "private"
    chat.send_action = AsyncMock()
    u.effective_chat = chat
    u.message.chat = chat
    u.message.message_thread_id = None
    u.message.message_id = 500
    u.message.text = None
    u.message.caption = "положи файл в сендбокс как бинарник"
    u.message.from_user = MagicMock(id=1, first_name="v", last_name=None,
                                    username=None)
    doc = MagicMock()
    doc.file_name = doc_name
    doc.file_size = len(doc_bytes)
    doc.file_id = "f1"
    u.message.document = doc
    u.message.voice = None
    u.message.photo = None
    return u


def _context():
    c = MagicMock()
    c.bot = MagicMock()
    remote = MagicMock()
    remote.file_size = 100
    c.bot.get_file = AsyncMock(return_value=remote)
    return c


def _wire(monkeypatch, tmp_path, *, doc_bytes, doc_name="characters.json",
          upload_error=None):
    """Everything handle_document needs -- and the knobs the test needs.

    Returns a dict with: `uploaded` (the bytes that reached the file
    door, in order), `turns` (the history the model was given) and
    `replies` (what the user was told).
    """
    import config as _cfg
    import storage as _st
    import turn as _turn
    from personal_tools import file_upload as _fu
    from storage import Storage

    # A real database, so the row the handler persists is the row the
    # model is about to read. Asserting on the mock instead would
    # prove that the mock was called, not that the model saw anything.
    monkeypatch.setattr(_cfg, "DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setattr(_st, "_store_instance", None)
    store = Storage(str(tmp_path / "t.db"))
    monkeypatch.setattr(_st, "get_store", lambda: store)
    monkeypatch.setattr(_turn, "storage", _st)
    store.create_thread(1, "main")

    uploaded = []
    turns = []
    replies = []

    async def fake_upload(content, name, **kw):
        if upload_error is not None:
            raise upload_error
        uploaded.append((content, name))
        return _fu.UploadedFile(
            name=name, path=f"/workspace/{name}", size=len(content),
            sha256=hashlib.sha256(content).hexdigest())

    monkeypatch.setattr(_fu, "upload_to_sandbox", fake_upload)
    monkeypatch.setattr(
        "personal_tools.bash_command.is_enabled_for_active_profile",
        lambda: True)

    async def fake_reply(update, text, **kw):
        replies.append(text)
        return _Thinking()

    monkeypatch.setattr(handlers, "_reply", fake_reply)

    async def fake_open_thinking(update, *, label):
        return _Thinking(), "Vasisualy"

    monkeypatch.setattr(handlers, "_open_thinking", fake_open_thinking)

    async def fake_download(file, max_bytes, kind, update):
        return doc_bytes

    monkeypatch.setattr(handlers, "_download_with_limit", fake_download)

    async def fake_slot(update, context, *, label):
        return handlers._TurnSlot(sem=asyncio.Semaphore(1), chat_id=1,
                                   thread_id="main", user_id=1)

    monkeypatch.setattr(handlers, "_acquire_turn_slot", fake_slot)
    monkeypatch.setattr(handlers, "_is_rating_active", lambda u: False)
    monkeypatch.setattr(handlers, "_register_abort_event",
                        lambda *a, **kw: None)
    monkeypatch.setattr(handlers, "_typing_indicator",
                        lambda *a, **kw: asyncio.sleep(0))

    class _Sem:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(handlers, "_get_global_llm_sem", lambda: _Sem())

    async def fake_call_llama(history, **kwargs):
        turns.append(history)
        return {"text": "принято", "executed_tools": [], "aborted": False}

    monkeypatch.setattr(handlers, "call_llama", fake_call_llama)

    async def fake_persist_for_reply(*a, **kw):
        return ""

    monkeypatch.setattr(handlers, "_persist_for_reply", fake_persist_for_reply)

    async def fake_send_files(*a, **kw):
        return None

    monkeypatch.setattr(handlers, "send_files", fake_send_files)

    async def fake_close_thinking(*a, **kw):
        return None

    monkeypatch.setattr(handlers, "close_thinking", fake_close_thinking)

    return {"uploaded": uploaded, "turns": turns, "replies": replies}


def _turn_text(turns):
    """The text of the last user turn the model was given."""
    assert turns, "the model was never called -- the handler stopped early"
    history = turns[-1]
    users = [m for m in history
             if isinstance(m, dict) and m.get("role") == "user"]
    assert users, f"no user turn in the history: {history!r}"
    return users[-1].get("content") or ""


# ------------------------------------------------------------------ tests --


@pytest.mark.asyncio
async def test_a_text_document_lands_in_the_sandbox(monkeypatch, tmp_path):
    """The file goes in through the door -- the bytes that arrived."""
    from personal_tools.file_upload import FileUploadError  # noqa: F401
    wired = _wire(monkeypatch, tmp_path, doc_bytes=DOC_BYTES)

    await handlers.handle_document(_update(DOC_BYTES), _context())

    assert wired["uploaded"], (
        "the document never reached the sandbox -- the chat still has no "
        "way in")
    content, name = wired["uploaded"][0]
    assert content == DOC_BYTES, (
        "the bytes that landed are not the bytes that arrived")
    assert name == "characters.json", (
        f"the file landed under an unexpected name: {name!r}")


@pytest.mark.asyncio
async def test_the_model_is_given_a_pointer_not_the_file(monkeypatch,
                                                          tmp_path):
    """The turn names the file and where it is; the file is not pasted."""
    wired = _wire(monkeypatch, tmp_path, doc_bytes=DOC_BYTES)

    await handlers.handle_document(_update(DOC_BYTES), _context())

    text = _turn_text(wired["turns"])
    assert "/workspace/characters.json" in text, (
        "the turn does not name the file in the sandbox -- the model has "
        f"nothing to read: {text[:400]!r}")
    assert hashlib.sha256(DOC_BYTES).hexdigest() in text, (
        "the turn does not carry the digest of the file that landed")
    assert DOC_TAIL not in text, (
        "the whole file is in the prompt again -- the turn is the file, "
        "not a pointer to it")


@pytest.mark.asyncio
async def test_a_piece_of_a_file_says_it_is_a_piece(monkeypatch, tmp_path):
    """The model is told that the quoted text is not the file."""
    wired = _wire(monkeypatch, tmp_path, doc_bytes=DOC_BYTES)

    await handlers.handle_document(_update(DOC_BYTES), _context())

    text = _turn_text(wired["turns"])
    # The NUMBER is the knob's business (there is a test for that); what
    # matters here is that the turn says the quoted text is a piece.
    assert _PIECE.search(text), (
        "the turn does not say that the quoted text is a piece of the "
        f"file: {text[:400]!r}")
    assert _PIECE_HEADER.search(text), (
        "the excerpt is not labelled as an excerpt")


@pytest.mark.asyncio
async def test_a_short_document_arrives_whole(monkeypatch, tmp_path):
    """A note that fits is quoted in full, and says that it is the whole."""
    wired = _wire(monkeypatch, tmp_path, doc_bytes=SHORT_BYTES,
                  doc_name="note.txt")

    await handlers.handle_document(_update(SHORT_BYTES, "note.txt"),
                                   _context())

    text = _turn_text(wired["turns"])
    assert SHORT_TEXT in text, (
        f"the whole note is not in the turn: {text[:400]!r}")
    assert "--- Contents ---" in text, (
        "the turn does not say that what follows is the whole file")
    assert "Only the first" not in text, (
        "a file that arrived whole is described as a piece")


@pytest.mark.asyncio
async def test_a_file_the_sandbox_will_not_take_is_named_as_lost(
        monkeypatch, tmp_path):
    """No silent cut: the refusal and the reason are in the turn."""
    from personal_tools.file_upload import FileUploadError
    wired = _wire(
        monkeypatch, tmp_path, doc_bytes=DOC_BYTES,
        upload_error=FileUploadError("the sandbox is out of room", status=507))

    await handlers.handle_document(_update(DOC_BYTES), _context())

    text = _turn_text(wired["turns"])
    assert "could not be put in the sandbox" in text, (
        "the turn does not say that the file was not stored")
    assert "out of room" in text, (
        "the turn does not carry the reason the sandbox gave")
    assert _PIECE.search(text), (
        "the turn quotes a piece of the file without saying so")


@pytest.mark.asyncio
async def test_a_binary_document_still_lands_in_the_sandbox(monkeypatch,
                                                             tmp_path):
    """The path that already worked keeps working."""
    wired = _wire(monkeypatch, tmp_path, doc_bytes=BINARY_BYTES,
                  doc_name="blob.bin")

    await handlers.handle_document(_update(BINARY_BYTES, "blob.bin"),
                                   _context())

    assert wired["uploaded"], "the binary never reached the sandbox"
    content, name = wired["uploaded"][0]
    assert content == BINARY_BYTES
    assert name == "blob.bin"
    text = _turn_text(wired["turns"])
    assert "/workspace/blob.bin" in text
    assert "is a binary file" in text, (
        "the turn does not say what kind of file it is -- a binary "
        "described as a text file is a file the model will try to read "
        f"as words: {text[:400]!r}")


def test_the_preview_decode_is_bounded():
    """A huge file must not become a huge string just to be measured.

    The window is four bytes per character -- the UTF-8 maximum -- so
    a 50 MB file is decoded in a 16 KB window and thrown away, not held.
    """
    big = ("я" * 5_000_000).encode("utf-8")
    preview, whole = handlers._document_preview(big, 4000)
    assert whole is False, "a file that does not fit was called whole"
    assert len(preview) == 4000, (
        f"the preview is {len(preview)} characters, not 4000")
    small, whole_small = handlers._document_preview(SHORT_BYTES, 4000)
    assert whole_small is True
    assert small == SHORT_TEXT
