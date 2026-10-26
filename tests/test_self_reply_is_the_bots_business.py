"""A reply to your own message is addressed to the bot, not to a stranger.

The group policy (T65.1) says: when one human replies to another human's
message, the bot keeps quiet, because the group is talking to itself and
a visible answer from the bot is noise.

That rule was written for replies addressed to SOMEONE ELSE. A reply to
the user's OWN message addresses nobody else -- and it is how the user
points the bot at a message ("delete this one", "what did I mean by
that").

The operator's live test of 2026-10-26 (19:40, LlmChatPlace) failed
exactly here. He replied to his own message to highlight it, and the bot
answered with:

    [dispatch] reply-to-other-user: absorbing silently (reply_to=id=286293081)

so the model never saw the id of the message he had highlighted, and the
delete request died with it.

These tests hold the boundary: a self-reply is the bot's business, a
reply to somebody else still is not.
"""
import types

from access import _is_reply_to_other_user


def _user(uid, username=None):
    return types.SimpleNamespace(id=uid, username=username, first_name="u")


def _message(*, reply_from, sender=1055898374, thread_id=25):
    replied = types.SimpleNamespace(
        message_id=3307,
        chat=types.SimpleNamespace(id=-1004461679108),
        from_user=_user(reply_from),
    )
    return types.SimpleNamespace(
        message_id=3313,
        message_thread_id=thread_id,
        reply_to_message=replied,
        from_user=_user(sender),
        text="подсветил сообщение которое надо удалить",
        caption=None,
        chat=types.SimpleNamespace(id=-1004461679108, type="supergroup",
                                    is_forum=True),
    )


def _update(message):
    return types.SimpleNamespace(
        message=message,
        effective_chat=message.chat,
        effective_user=message.from_user,
    )


BOT_ID = 8954505611
OPERATOR_ID = 1055898374


def test_a_reply_to_your_own_message_is_not_a_reply_to_another_human():
    """The gesture that points the bot at a message must reach the bot."""
    msg = _message(reply_from=OPERATOR_ID)
    assert _is_reply_to_other_user(_update(msg), BOT_ID) is False


def test_a_reply_to_another_human_is_still_swallowed():
    """The rule T65.1 exists for is not weakened by the fix above."""
    msg = _message(reply_from=4242)
    assert _is_reply_to_other_user(_update(msg), BOT_ID) is True


def test_a_reply_to_the_bot_is_still_answered():
    """The oldest case, and the one the bot exists for."""
    msg = _message(reply_from=BOT_ID)
    assert _is_reply_to_other_user(_update(msg), BOT_ID) is False


def test_a_command_in_a_reply_to_another_human_is_answered():
    """An explicit command wins over the group policy."""
    msg = _message(reply_from=4242)
    msg.text = "/stats"
    assert _is_reply_to_other_user(_update(msg), BOT_ID) is False
