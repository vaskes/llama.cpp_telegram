"""The implicit topic reply must not look like a message id.

Telegram's Bot API attaches an implicit reply to every message in a forum
topic that answers nothing else: the topic's creation message. A topic's
id EQUALS the id of the service message that created it, so that implicit
reply carries the TOPIC ID, which looks exactly like a message id.

Measured on the operator's chat (2026-10-26, topic 25): a plain message
arrived with reply_to_message.message_id == 25 == message_thread_id.
Two defects came out of it:

  * the delete tool advertised id 25 as "the message the user replied
    to", the model tried to delete it, and Telegram refused -- a service
    message is not deletable ("Message can't be deleted");
  * a plain message in a topic created by a HUMAN looked like a reply to
    that human, so the bot swallowed it (the reply-to-other-user path).

These tests hold the two properties that fix both: the implicit reply is
not a reply, and a real reply inside a topic keeps its chat-wide id.
"""
import types

from access import _is_reply_to_other_user, real_reply_to
from handlers import _reply_to_message_id


def _user(uid, username=None):
    return types.SimpleNamespace(id=uid, username=username, first_name="u")


def _message(*, message_id=3307, thread_id=25, reply=None, sender=None):
    """A stand-in for telegram.Message with real attributes.

    SimpleNamespace, not MagicMock: a MagicMock returns a truthy mock
    for every attribute, so a comparison against a number would be True
    for the wrong reason.
    """
    return types.SimpleNamespace(
        message_id=message_id,
        message_thread_id=thread_id,
        reply_to_message=reply,
        from_user=sender if sender is not None else _user(1055898374),
        text=None,
        caption=None,
        chat=types.SimpleNamespace(id=-1004461679108, type="supergroup",
                                    is_forum=True),
    )


def _replied(*, message_id, chat_id=-1004461679108, from_uid=1055898374):
    return types.SimpleNamespace(
        message_id=message_id,
        chat=types.SimpleNamespace(id=chat_id),
        from_user=_user(from_uid),
    )


def _update(message):
    return types.SimpleNamespace(
        message=message,
        effective_chat=message.chat,
        effective_user=message.from_user,
    )


# === the implicit topic reply ===

def test_the_implicit_topic_reply_is_not_a_reply():
    """A plain message in a topic carries the topic's creation message
    as its reply. It is not something the user pointed at."""
    msg = _message(message_id=3313, thread_id=25,
                   reply=_replied(message_id=25, from_uid=8954505611))
    assert real_reply_to(_update(msg)) is None


def test_a_plain_message_in_a_topic_offers_no_reply_id_to_the_delete_tool():
    """The id that reaches the model as 'the message the user replied to'
    must be None here -- id 25 is the topic, and the model deleted it."""
    msg = _message(message_id=3313, thread_id=25,
                   reply=_replied(message_id=25, from_uid=8954505611))
    assert _reply_to_message_id(_update(msg)) is None


def test_a_plain_message_in_a_topic_is_not_swallowed_as_a_reply_to_a_human():
    """The topic was created by a human, so the implicit reply carries
    that human's id. The bot must still answer the message."""
    msg = _message(message_id=3313, thread_id=25,
                   reply=_replied(message_id=25, from_uid=4242))
    assert _is_reply_to_other_user(_update(msg), 8954505611) is False


# === a real reply inside a topic ===

def test_a_real_reply_in_a_topic_keeps_its_chat_wide_id():
    """The reply the user actually made carries a chat-wide id, which is
    not the topic id, and that id is the one the delete tool needs."""
    msg = _message(message_id=3313, thread_id=25,
                   reply=_replied(message_id=3307, from_uid=1055898374))
    assert _reply_to_message_id(_update(msg)) == 3307


def test_a_real_reply_in_a_topic_is_still_a_reply_to_a_human():
    """The topic reply being filtered out must not cost the bot its
    judgement about replies to other people."""
    msg = _message(message_id=3313, thread_id=25,
                   reply=_replied(message_id=3307, from_uid=4242))
    assert _is_reply_to_other_user(_update(msg), 8954505611) is True


def test_a_reply_from_another_chat_is_dropped():
    """A forwarded message carries an id that means nothing in this chat."""
    msg = _message(message_id=3313, thread_id=25,
                   reply=_replied(message_id=3307, chat_id=-1009999999999))
    assert _reply_to_message_id(_update(msg)) is None


def test_a_message_outside_a_topic_keeps_its_reply():
    """No topic, no implicit reply: a plain reply is a plain reply."""
    msg = _message(message_id=3313, thread_id=None,
                   reply=_replied(message_id=3307))
    assert _reply_to_message_id(_update(msg)) == 3307
