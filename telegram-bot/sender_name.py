"""One person, one name in the record.

TWO writers, ONE fact. The text handler and the group-absorb path both
write a row for the same person, and until 2026-10-27 they named that
person differently: the text handler stored the display name
("Vasisualy Lohankin"), the group-absorb path stored the short one
("Vasisualy") or the handle ("@V_bot"). The record is the source of
truth -- the operator keeps this history for a LoRA dataset -- and a
record that calls one person two names is a record that cannot be
searched by name, and a model that reads both rows sees two people where
there is one.

The operator's decision (2026-10-27): the full Telegram nickname is the
canonical name. Anything shorter is an artefact of a second resolver,
not a second fact.

So the policy lives here, once, and both writers call it. Whether a name
is SAFE to put in a prompt line is a different question with a different
module: `prompt_guard._safe_sender_label` strips the control characters
Telegram lets a user type into their own name. This module decides
*which* name, not whether it is safe.
"""

from prompt_guard import _safe_sender_label


def display_name(user) -> str | None:
    """The name the record carries for one Telegram user.

    Resolution order:
      1. first_name + " " + last_name  (e.g. "Vasisualy Lohankin")
      2. first_name only
      3. "@" + username
      4. "user_" + str(id)              (last-resort numeric)

    Returns None for non-User inputs (system messages, channel posts)
    so the caller can decide to skip persisting the name.

    The user-controlled first_name / last_name / username are passed
    through _safe_sender_label() to strip newlines, RTL overrides,
    and other characters that would let the name break out of the
    From: line in the LLM prompt. See prompt_guard for the threat model
    and the stripped categories.
    """
    if user is None:
        return None
    first_raw = getattr(user, "first_name", None) or ""
    last_raw = getattr(user, "last_name", None) or ""
    full = _safe_sender_label((first_raw + " " + last_raw).strip())
    if full:
        return full
    username_raw = getattr(user, "username", None) or ""
    # Usernames are restricted to [A-Za-z0-9_] by Telegram but we
    # sanitise anyway for symmetry and future-proofing.
    username_safe = _safe_sender_label(username_raw)
    if username_safe:
        return "@" + username_safe
    if getattr(user, "id", None) is not None:
        return f"user_{user.id}"
    return None
