

class _FakeUser:
    """Minimal stand-in for telegram.User used in handler tests.
    Has real string/None attributes (not MagicMock) so that
    _sender_display_name() returns a clean string and SQLite
    binding works."""
    def __init__(self, id=1, first_name="t", last_name=None, username=None):
        self.id = id
        self.first_name = first_name
        self.last_name = last_name
        self.username = username
"""Tests for the rollback pattern in all 4 handlers.

The v0.9.2 hotfix added the rollback pattern to handle_voice,
handle_document, and handle_text (matching the v0.9.1 fix for
handle_photo). The pattern is:

1. persisted = False at the start of the try block
2. Send thinking message + register abort_event
3. Then _persist_message(...) and persisted = True
4. Rollback in the abort branch (Stop click) -> delete_last_user_message
5. Rollback in the except branch -> delete_last_user_message
6. Rollback in the retry-abort branch (for retryable handlers)

These tests mock the LLM call to return None (simulating Stop)
or raise (simulating exception), and verify that the rollback
actually happens.
"""
import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# === handle_text rollback ===



@pytest.mark.asyncio
async def test_handle_text_rollback_on_abort(monkeypatch, tmp_path):
    """v0.9.2: Stop click during text LLM call should roll back the persisted user message."""
    # Set up a fresh DB
    db = str(tmp_path / "test.db")
    from storage import Storage
    import config as _cfg
    monkeypatch.setattr(_cfg, "DB_PATH", db)
    import storage as _st
    # Reset the module-level singleton so get_store() builds a fresh
    # Storage from the patched DB_PATH, not the previously-cached
    # instance (which would point at the default /app/data path).
    monkeypatch.setattr(_st, "_store_instance", None)
    import persistence as _pers
    store = Storage(db)
    store.create_thread(1, "main")
    monkeypatch.setattr(_st, "get_store", lambda: store)
    monkeypatch.setattr(_pers, "get_store", lambda: store)
    monkeypatch.setattr("handlers.get_store", lambda: store)
    # turn.py owns its own `from storage import get_store` binding, so it
    # needs patching explicitly. Without this the suite only passed because
    # `import bot` inside a test ran bot.py's module-level
    # `store = get_store()` against an already-patched storage module.
    monkeypatch.setattr("turn.get_store", lambda: store)
    
    # Mock _route_to_thread
    from handlers import _sender_display_name
    async def fake_route(update, context):
        return (1, "main", False)
    monkeypatch.setattr("handlers._route_to_thread", fake_route)
    # Mock _check_user_slot
    sem = asyncio.Semaphore()
    async def fake_slot(chat_id, user_id, update):
        return sem
    monkeypatch.setattr("dispatch._check_user_slot", fake_slot)
    # Mock _is_rating_active
    monkeypatch.setattr("dispatch._is_rating_active", lambda u: False)
    # Mock _stop_button_markup
    monkeypatch.setattr("dispatch._stop_button_markup", lambda: None)
    # Mock _reply
    thinking = MagicMock()
    thinking.message_id = 999
    thinking.delete = AsyncMock()
    thinking.edit_text = AsyncMock()
    monkeypatch.setattr("handlers._reply", AsyncMock(return_value=thinking))
    # Mock _get_global_llm_sem
    class FakeSem:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
    monkeypatch.setattr("handlers._get_global_llm_sem", lambda: FakeSem())
    # Mock call_llama to return None (simulating abort)
    async def fake_call(*a, **kw):
        return None
    monkeypatch.setattr("handlers.call_llama", fake_call)
    # Mock send_action
    chat = MagicMock()
    chat.send_action = AsyncMock()
    monkeypatch.setattr("handlers.Update", MagicMock())
    
    # Build the update
    update = MagicMock()
    update.effective_user.id = 1
    update.effective_chat.type = "private"
    update.effective_chat = chat
    update.effective_chat.send_action = AsyncMock()
    update.message.text = "hello world"
    update.message.from_user = _FakeUser()
    update.message.message_thread_id = None
    update.message.chat = chat
    
    # Track abort_event being set
    registered = []
    monkeypatch.setattr("handlers._register_abort_event", lambda cid, mid, ev: registered.append((cid, mid, ev)))
    # Simulate abort being set
    async def fake_route_and_abort(*a, **kw):
        # Set abort_event so the post-won race branch is taken
        if registered:
            registered[0][2].set()
        return None
    monkeypatch.setattr("handlers.call_llama", fake_route_and_abort)
    
    # Run handle_text
    from handlers import handle_text
    context = MagicMock()
    try:
        await asyncio.wait_for(handle_text(update, context), timeout=5.0)
    except asyncio.TimeoutError:
        pytest.fail("handle_text timed out")
    
    # Verify: persisted then rolled back
    rows = store.get_messages(1, "main", limit=10)
    # The user message "hello world" was persisted, then rolled back
    user_msgs = [r for r in rows if r["role"] == "user"]
    assert user_msgs == [], f"rollback failed; user rows remain: {user_msgs}"


# === handle_voice rollback (uses transcribe_voice) ===

@pytest.mark.asyncio
async def test_handle_voice_rollback_on_abort(monkeypatch, tmp_path):
    """v0.9.2: Stop click during voice LLM call should roll back the persisted transcript."""
    db = str(tmp_path / "test.db")
    from storage import Storage
    import config as _cfg
    monkeypatch.setattr(_cfg, "DB_PATH", db)
    import storage as _st
    # Reset the module-level singleton so get_store() builds a fresh
    # Storage from the patched DB_PATH, not the previously-cached
    # instance (which would point at the default /app/data path).
    monkeypatch.setattr(_st, "_store_instance", None)
    import persistence as _pers
    store = Storage(db)
    store.create_thread(1, "main")
    monkeypatch.setattr(_st, "get_store", lambda: store)
    monkeypatch.setattr(_pers, "get_store", lambda: store)
    monkeypatch.setattr("handlers.get_store", lambda: store)
    # turn.py owns its own `from storage import get_store` binding, so it
    # needs patching explicitly. Without this the suite only passed because
    # `import bot` inside a test ran bot.py's module-level
    # `store = get_store()` against an already-patched storage module.
    monkeypatch.setattr("turn.get_store", lambda: store)
    
    async def fake_route(update, context):
        return (1, "main", False)
    monkeypatch.setattr("handlers._route_to_thread", fake_route)
    sem = asyncio.Semaphore()
    async def fake_slot(chat_id, user_id, update):
        return sem
    monkeypatch.setattr("dispatch._check_user_slot", fake_slot)
    monkeypatch.setattr("dispatch._is_rating_active", lambda u: False)
    monkeypatch.setattr("dispatch._stop_button_markup", lambda: None)
    
    # Mock context.bot.get_file to return a fake file
    fake_file = MagicMock()
    fake_file.download_to_memory = AsyncMock()
    context = MagicMock()
    context.bot.get_file = AsyncMock(return_value=fake_file)
    
    # Mock _download_with_limit to return some bytes
    async def fake_download(file, max_bytes, kind, update):
        return b"fake-ogg-bytes"
    monkeypatch.setattr("handlers._download_with_limit", fake_download)
    
    # Mock transcribe_voice
    async def fake_transcribe(voice_bytes):
        return "transcribed user speech"
    monkeypatch.setattr("handlers.transcribe_voice", fake_transcribe)
    
    thinking = MagicMock()
    thinking.message_id = 999
    thinking.delete = AsyncMock()
    thinking.edit_text = AsyncMock()
    # _reply is called multiple times; the handler will await each return.
    # Use a function side_effect that returns a fresh MagicMock each time,
    # but the SECOND call must return our `thinking` (because the handler
    # binds the second return to the `thinking` local).
    call_count = [0]
    async def fake_reply(*a, **kw):
        call_count[0] += 1
        if call_count[0] == 2:
            return thinking
        return MagicMock()
    monkeypatch.setattr("handlers._reply", fake_reply)
    
    class FakeSem:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
    monkeypatch.setattr("handlers._get_global_llm_sem", lambda: FakeSem())
    
    registered = []
    monkeypatch.setattr("handlers._register_abort_event", lambda cid, mid, ev: registered.append((cid, mid, ev)))
    
    async def fake_call(*a, **kw):
        # Set the abort_event (simulating user clicking Stop)
        if registered:
            registered[-1][2].set()
        return None
    monkeypatch.setattr("handlers.call_llama", fake_call)
    
    chat = MagicMock()
    chat.send_action = AsyncMock()
    msg = MagicMock()
    msg.voice = MagicMock()
    msg.voice.file_id = "abc"
    msg.voice.file_size = 100
    msg.video_note = None
    update = MagicMock()
    update.effective_user.id = 1
    update.message = msg
    update.message.chat = chat
    update.message.from_user = _FakeUser()
    update.message.message_thread_id = None
    update.message.reply_to_message = None
    
    from handlers import handle_voice
    try:
        await asyncio.wait_for(handle_voice(update, context), timeout=5.0)
    except asyncio.TimeoutError:
        pytest.fail("handle_voice timed out")
    
    rows = store.get_messages(1, "main", limit=10)
    user_msgs = [r for r in rows if r["role"] == "user"]
    assert user_msgs == [], f"rollback failed; user rows remain: {user_msgs}"


# === handle_document rollback ===

@pytest.mark.asyncio
async def test_handle_document_rollback_on_abort(monkeypatch, tmp_path):
    """v0.9.2: Stop click during document LLM call should roll back the persisted document content."""
    db = str(tmp_path / "test.db")
    from storage import Storage
    import config as _cfg
    monkeypatch.setattr(_cfg, "DB_PATH", db)
    import storage as _st
    # Reset the module-level singleton so get_store() builds a fresh
    # Storage from the patched DB_PATH, not the previously-cached
    # instance (which would point at the default /app/data path).
    monkeypatch.setattr(_st, "_store_instance", None)
    import persistence as _pers
    store = Storage(db)
    store.create_thread(1, "main")
    monkeypatch.setattr(_st, "get_store", lambda: store)
    monkeypatch.setattr(_pers, "get_store", lambda: store)
    monkeypatch.setattr("handlers.get_store", lambda: store)
    # turn.py owns its own `from storage import get_store` binding, so it
    # needs patching explicitly. Without this the suite only passed because
    # `import bot` inside a test ran bot.py's module-level
    # `store = get_store()` against an already-patched storage module.
    monkeypatch.setattr("turn.get_store", lambda: store)
    
    async def fake_route(update, context):
        return (1, "main", False)
    monkeypatch.setattr("handlers._route_to_thread", fake_route)
    sem = asyncio.Semaphore()
    async def fake_slot(chat_id, user_id, update):
        return sem
    monkeypatch.setattr("dispatch._check_user_slot", fake_slot)
    monkeypatch.setattr("dispatch._is_rating_active", lambda u: False)
    monkeypatch.setattr("dispatch._stop_button_markup", lambda: None)
    
    fake_file = MagicMock()
    context = MagicMock()
    context.bot.get_file = AsyncMock(return_value=fake_file)
    
    async def fake_download(file, max_bytes, kind, update):
        return b"document content bytes"
    monkeypatch.setattr("handlers._download_with_limit", fake_download)
    
    thinking = MagicMock()
    thinking.message_id = 999
    thinking.delete = AsyncMock()
    thinking.edit_text = AsyncMock()
    async def fake_reply(*a, **kw):
        return thinking
    monkeypatch.setattr("handlers._reply", fake_reply)
    
    class FakeSem:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
    monkeypatch.setattr("handlers._get_global_llm_sem", lambda: FakeSem())
    
    registered = []
    monkeypatch.setattr("handlers._register_abort_event", lambda cid, mid, ev: registered.append((cid, mid, ev)))
    
    async def fake_call(*a, **kw):
        if registered:
            registered[-1][2].set()
        return None
    monkeypatch.setattr("handlers.call_llama", fake_call)
    
    doc = MagicMock()
    doc.file_size = 100
    doc.file_name = "test.txt"
    chat = MagicMock()
    chat.send_action = AsyncMock()
    msg = MagicMock()
    msg.document = doc
    msg.caption = "Read this"
    update = MagicMock()
    update.effective_user.id = 1
    update.message = msg
    update.message.chat = chat
    update.message.from_user = _FakeUser()
    update.message.message_thread_id = None
    
    from handlers import handle_document
    try:
        await asyncio.wait_for(handle_document(update, context), timeout=5.0)
    except asyncio.TimeoutError:
        pytest.fail("handle_document timed out")
    
    rows = store.get_messages(1, "main", limit=10)
    user_msgs = [r for r in rows if r["role"] == "user"]
    assert user_msgs == [], f"rollback failed; user rows remain: {user_msgs}"


# === handle_photo rollback (regression test for v0.9.1) ===

@pytest.mark.asyncio
async def test_handle_photo_rollback_on_abort(monkeypatch, tmp_path):
    """v0.9.1: Stop click during photo LLM call should roll back the persisted image."""
    db = str(tmp_path / "test.db")
    from storage import Storage
    import config as _cfg
    monkeypatch.setattr(_cfg, "DB_PATH", db)
    import storage as _st
    # Reset the module-level singleton so get_store() builds a fresh
    # Storage from the patched DB_PATH, not the previously-cached
    # instance (which would point at the default /app/data path).
    monkeypatch.setattr(_st, "_store_instance", None)
    import persistence as _pers
    store = Storage(db)
    store.create_thread(1, "main")
    monkeypatch.setattr(_st, "get_store", lambda: store)
    monkeypatch.setattr(_pers, "get_store", lambda: store)
    monkeypatch.setattr("handlers.get_store", lambda: store)
    # turn.py owns its own `from storage import get_store` binding, so it
    # needs patching explicitly. Without this the suite only passed because
    # `import bot` inside a test ran bot.py's module-level
    # `store = get_store()` against an already-patched storage module.
    monkeypatch.setattr("turn.get_store", lambda: store)
    
    async def fake_route(update, context):
        return (1, "main", True)
    monkeypatch.setattr("handlers._route_to_thread", fake_route)
    sem = asyncio.Semaphore()
    async def fake_slot(chat_id, user_id, update):
        return sem
    monkeypatch.setattr("dispatch._check_user_slot", fake_slot)
    monkeypatch.setattr("dispatch._is_rating_active", lambda u: False)
    monkeypatch.setattr("dispatch._stop_button_markup", lambda: None)
    
    fake_file = MagicMock()
    context = MagicMock()
    context.bot.get_file = AsyncMock(return_value=fake_file)
    
    async def fake_download(file, max_bytes, kind, update):
        return b"fake-jpeg-bytes"
    monkeypatch.setattr("handlers._download_with_limit", fake_download)
    
    thinking = MagicMock()
    thinking.message_id = 999
    thinking.delete = AsyncMock()
    thinking.edit_text = AsyncMock()
    async def fake_reply(*a, **kw):
        return thinking
    monkeypatch.setattr("handlers._reply", fake_reply)
    
    class FakeSem:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
    monkeypatch.setattr("handlers._get_global_llm_sem", lambda: FakeSem())
    
    registered = []
    monkeypatch.setattr("handlers._register_abort_event", lambda cid, mid, ev: registered.append((cid, mid, ev)))
    
    async def fake_call(*a, **kw):
        if registered:
            registered[-1][2].set()
        return None
    monkeypatch.setattr("handlers.call_llama", fake_call)
    
    photo = MagicMock()
    photo.file_size = 100
    chat = MagicMock()
    chat.send_action = AsyncMock()
    msg = MagicMock()
    msg.photo = [photo, photo, photo]  # multiple sizes
    msg.caption = "what is this?"
    update = MagicMock()
    update.effective_user.id = 1
    update.message = msg
    update.message.chat = chat
    update.message.from_user = _FakeUser()
    update.message.message_thread_id = None
    
    from handlers import handle_photo
    try:
        await asyncio.wait_for(handle_photo(update, context), timeout=5.0)
    except asyncio.TimeoutError:
        pytest.fail("handle_photo timed out")
    
    rows = store.get_messages(1, "main", limit=10)
    user_msgs = [r for r in rows if r["role"] == "user"]
    assert user_msgs == [], f"rollback failed; user rows remain: {user_msgs}"


# === handle_voice successful flow (no abort) ===

@pytest.mark.asyncio
async def test_handle_voice_successful_flow_keeps_transcript(monkeypatch, tmp_path):
    """Sanity: when voice message completes without abort, the transcript stays in history."""
    db = str(tmp_path / "test.db")
    from storage import Storage
    import config as _cfg
    monkeypatch.setattr(_cfg, "DB_PATH", db)
    import storage as _st
    # Reset the module-level singleton so get_store() builds a fresh
    # Storage from the patched DB_PATH, not the previously-cached
    # instance (which would point at the default /app/data path).
    monkeypatch.setattr(_st, "_store_instance", None)
    import persistence as _pers
    store = Storage(db)
    store.create_thread(1, "main")
    monkeypatch.setattr(_st, "get_store", lambda: store)
    monkeypatch.setattr(_pers, "get_store", lambda: store)
    monkeypatch.setattr("handlers.get_store", lambda: store)
    # turn.py owns its own `from storage import get_store` binding, so it
    # needs patching explicitly. Without this the suite only passed because
    # `import bot` inside a test ran bot.py's module-level
    # `store = get_store()` against an already-patched storage module.
    monkeypatch.setattr("turn.get_store", lambda: store)
    
    async def fake_route(update, context):
        return (1, "main", False)
    monkeypatch.setattr("handlers._route_to_thread", fake_route)
    sem = asyncio.Semaphore()
    async def fake_slot(chat_id, user_id, update):
        return sem
    monkeypatch.setattr("dispatch._check_user_slot", fake_slot)
    monkeypatch.setattr("dispatch._is_rating_active", lambda u: False)
    monkeypatch.setattr("dispatch._stop_button_markup", lambda: None)
    
    fake_file = MagicMock()
    context = MagicMock()
    context.bot.get_file = AsyncMock(return_value=fake_file)
    async def fake_download(file, max_bytes, kind, update):
        return b"fake-ogg-bytes"
    monkeypatch.setattr("handlers._download_with_limit", fake_download)
    async def fake_transcribe(voice_bytes):
        return "what is the weather"
    monkeypatch.setattr("handlers.transcribe_voice", fake_transcribe)
    
    thinking = MagicMock()
    thinking.message_id = 999
    thinking.delete = AsyncMock()
    thinking.edit_text = AsyncMock()
    call_count = [0]
    async def fake_reply(*a, **kw):
        call_count[0] += 1
        if call_count[0] == 2:
            return thinking
        return MagicMock()
    monkeypatch.setattr("handlers._reply", fake_reply)
    
    class FakeSem:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
    monkeypatch.setattr("handlers._get_global_llm_sem", lambda: FakeSem())
    monkeypatch.setattr("handlers._register_abort_event", lambda *a, **kw: None)
    
    async def fake_call(*a, **kw):
        return "It's sunny."
    monkeypatch.setattr("handlers.call_llama", fake_call)
    monkeypatch.setattr("handlers.send_reply", AsyncMock())
    monkeypatch.setattr("handlers._apply_rating_and_persist", AsyncMock(return_value="It's sunny."))
    
    chat = MagicMock()
    chat.send_action = AsyncMock()
    msg = MagicMock()
    msg.voice = MagicMock()
    msg.voice.file_id = "abc"
    msg.voice.file_size = 100
    msg.video_note = None
    update = MagicMock()
    update.effective_user.id = 1
    update.message = msg
    update.message.chat = chat
    update.message.from_user = _FakeUser()
    update.message.message_thread_id = None
    update.message.message_id = 100
    update.message.reply_to_message = None
    
    from handlers import handle_voice
    await asyncio.wait_for(handle_voice(update, context), timeout=5.0)
    
    rows = store.get_messages(1, "main", limit=10)
    user_msgs = [r for r in rows if r["role"] == "user"]
    assert len(user_msgs) == 1, f"expected 1 user msg, got {user_msgs}"
    import json as _json
    actual_content = _json.loads(user_msgs[0]["content"]).get("content", "")
    assert "what is the weather" in actual_content


# === stop button on text keeps abort_event registered correctly ===

@pytest.mark.asyncio
async def test_text_abort_registers_abort_event_with_thinking_message_id(monkeypatch, tmp_path):
    """v0.7.x: the abort_event must be keyed on (chat_id, thinking.message_id) so the
    callback handler can find it when the user clicks Stop on the thinking message."""
    db = str(tmp_path / "test.db")
    from storage import Storage
    import config as _cfg
    monkeypatch.setattr(_cfg, "DB_PATH", db)
    import storage as _st
    # Reset the module-level singleton so get_store() builds a fresh
    # Storage from the patched DB_PATH, not the previously-cached
    # instance (which would point at the default /app/data path).
    monkeypatch.setattr(_st, "_store_instance", None)
    import persistence as _pers
    store = Storage(db)
    store.create_thread(1, "main")
    monkeypatch.setattr(_st, "get_store", lambda: store)
    monkeypatch.setattr(_pers, "get_store", lambda: store)
    monkeypatch.setattr("handlers.get_store", lambda: store)
    # turn.py owns its own `from storage import get_store` binding, so it
    # needs patching explicitly. Without this the suite only passed because
    # `import bot` inside a test ran bot.py's module-level
    # `store = get_store()` against an already-patched storage module.
    monkeypatch.setattr("turn.get_store", lambda: store)
    
    async def fake_route(update, context):
        return (1, "main", False)
    monkeypatch.setattr("handlers._route_to_thread", fake_route)
    sem = asyncio.Semaphore()
    async def fake_slot(chat_id, user_id, update):
        return sem
    monkeypatch.setattr("dispatch._check_user_slot", fake_slot)
    monkeypatch.setattr("dispatch._is_rating_active", lambda u: False)
    monkeypatch.setattr("dispatch._stop_button_markup", lambda: None)
    
    thinking = MagicMock()
    thinking.message_id = 12345  # specific ID
    thinking.delete = AsyncMock()
    thinking.edit_text = AsyncMock()
    monkeypatch.setattr("handlers._reply", AsyncMock(return_value=thinking))
    
    class FakeSem:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
    monkeypatch.setattr("handlers._get_global_llm_sem", lambda: FakeSem())
    
    captured = []
    def fake_register(cid, mid, ev):
        captured.append((cid, mid))
    monkeypatch.setattr("handlers._register_abort_event", fake_register)
    
    async def fake_call(*a, **kw):
        return "ok"
    monkeypatch.setattr("handlers.call_llama", fake_call)
    monkeypatch.setattr("handlers.send_reply", AsyncMock())
    monkeypatch.setattr("handlers._apply_rating_and_persist", AsyncMock(return_value="ok"))
    
    chat = MagicMock()
    chat.send_action = AsyncMock()
    update = MagicMock()
    update.effective_user.id = 1
    update.effective_chat = chat
    update.message.text = "test"
    update.message.from_user = _FakeUser()
    update.message.message_thread_id = None
    update.message.chat = chat
    update.message.message_id = 1
    
    from handlers import handle_text
    await asyncio.wait_for(handle_text(update, MagicMock()), timeout=5.0)
    
    assert captured == [(1, 12345)], f"expected abort_event registered with (chat_id, thinking.message_id), got {captured}"


# === text handler with successful flow (no rollback) ===

@pytest.mark.asyncio
async def test_text_successful_flow_keeps_message(monkeypatch, tmp_path):
    """Sanity: when text handler completes without abort, the user message stays in history."""
    db = str(tmp_path / "test.db")
    from storage import Storage
    import config as _cfg
    monkeypatch.setattr(_cfg, "DB_PATH", db)
    import storage as _st
    # Reset the module-level singleton so get_store() builds a fresh
    # Storage from the patched DB_PATH, not the previously-cached
    # instance (which would point at the default /app/data path).
    monkeypatch.setattr(_st, "_store_instance", None)
    import persistence as _pers
    store = Storage(db)
    store.create_thread(1, "main")
    monkeypatch.setattr(_st, "get_store", lambda: store)
    monkeypatch.setattr(_pers, "get_store", lambda: store)
    monkeypatch.setattr("handlers.get_store", lambda: store)
    # turn.py owns its own `from storage import get_store` binding, so it
    # needs patching explicitly. Without this the suite only passed because
    # `import bot` inside a test ran bot.py's module-level
    # `store = get_store()` against an already-patched storage module.
    monkeypatch.setattr("turn.get_store", lambda: store)
    
    async def fake_route(update, context):
        return (1, "main", False)
    monkeypatch.setattr("handlers._route_to_thread", fake_route)
    sem = asyncio.Semaphore()
    async def fake_slot(chat_id, user_id, update):
        return sem
    monkeypatch.setattr("dispatch._check_user_slot", fake_slot)
    monkeypatch.setattr("dispatch._is_rating_active", lambda u: False)
    monkeypatch.setattr("dispatch._stop_button_markup", lambda: None)
    
    thinking = MagicMock()
    thinking.message_id = 999
    thinking.delete = AsyncMock()
    thinking.edit_text = AsyncMock()
    monkeypatch.setattr("handlers._reply", AsyncMock(return_value=thinking))
    
    class FakeSem:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
    monkeypatch.setattr("handlers._get_global_llm_sem", lambda: FakeSem())
    monkeypatch.setattr("handlers._register_abort_event", lambda *a, **kw: None)
    async def fake_call(*a, **kw):
        return "ok"
    monkeypatch.setattr("handlers.call_llama", fake_call)
    monkeypatch.setattr("handlers.send_reply", AsyncMock())
    monkeypatch.setattr("handlers._apply_rating_and_persist", AsyncMock(return_value="ok"))
    
    chat = MagicMock()
    chat.send_action = AsyncMock()
    update = MagicMock()
    update.effective_user.id = 1
    update.effective_chat = chat
    update.message.text = "important question"
    update.message.from_user = _FakeUser()
    update.message.message_thread_id = None
    update.message.chat = chat
    update.message.message_id = 1
    
    from handlers import handle_text
    await asyncio.wait_for(handle_text(update, MagicMock()), timeout=5.0)
    
    rows = store.get_messages(1, "main", limit=10)
    user_msgs = [r for r in rows if r["role"] == "user"]
    assert len(user_msgs) == 1
    # content is stored as JSON; parse it to get the actual text
    import json as _json
    actual_content = _json.loads(user_msgs[0]["content"]).get("content", "")
    assert actual_content == "important question"


def test_empty_response_fallback_is_imported_in_handlers(monkeypatch):
    """v0.10.2 (Qwen wave-3 regression): handlers.py uses
    EMPTY_RESPONSE_FALLBACK but previously didn't import it, causing
    NameError on empty LLM responses. This test verifies the import
    is present.
    """
    import handlers
    import config
    # Must be the SAME object (not a re-define with the same value)
    assert handlers.EMPTY_RESPONSE_FALLBACK is config.EMPTY_RESPONSE_FALLBACK, \
        "EMPTY_RESPONSE_FALLBACK must be the same object in handlers and config"
    # Must be non-empty and informative
    assert len(handlers.EMPTY_RESPONSE_FALLBACK) > 20
    assert "reset" in handlers.EMPTY_RESPONSE_FALLBACK.lower(), \
        "EMPTY_RESPONSE_FALLBACK should mention /reset to the user"


@pytest.mark.asyncio
async def test_send_reply_does_not_crash_on_empty_text(monkeypatch, tmp_path):
    """v0.10.2 (regression): send_reply with empty text must not crash
    with NameError. The fallback message is used instead."""
    import config as _cfg
    import handlers as _handlers
    from unittest.mock import AsyncMock, MagicMock
    monkeypatch.setattr(_cfg, "DB_PATH", str(tmp_path / "test.db"))
    import storage as _st
    monkeypatch.setattr(_st, "_store_instance", None)
    import persistence as _pers
    store = _st.Storage(str(tmp_path / "test.db"))
    monkeypatch.setattr(_st, "get_store", lambda: store)
    monkeypatch.setattr(_pers, "get_store", lambda: store)

    # _reply mock — must NOT raise
    mock_sent = MagicMock()
    mock_sent.message_id = 999
    monkeypatch.setattr(
        "handlers._reply",
        AsyncMock(return_value=mock_sent),
    )
    # Rate limit must not block
    async def no_op_rate_limit(chat_id):
        pass
    monkeypatch.setattr(
        "dispatch._rate_limit_send",
        no_op_rate_limit,
    )

    update = MagicMock()
    update.effective_chat.id = 1
    update.message.message_id = 42

    # This used to raise NameError before v0.10.2
    await _handlers.send_reply(update, "")
    # Verify _reply was called with the fallback
    _handlers._reply.assert_awaited_once()
    sent_text = _handlers._reply.await_args.args[1]
    assert sent_text == _handlers.EMPTY_RESPONSE_FALLBACK
