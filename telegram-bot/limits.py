# limits.py
# Tunable per-deployment limits for the LLM gateway.
#
# This module encodes the bot's policy on:
#   - what the LLM is allowed to spend per call (max_tokens, timeouts)
#   - how long the bot is allowed to wait for an answer
#   - how much context history each user gets
#
# ============================================================================
# DEPLOYMENT PROFILES — READ THIS BEFORE TUNING
# ============================================================================
#
# The bot has *two* canonical deployment profiles. They differ
# dramatically in defaults, and the right one depends on the
# operator's threat model, not on the underlying llama-server.
#
# Public / multi-user group chat (DEFAULT — what the code ships with):
#   The bot sits in a forum supergroup with many users. Some are
#   trusted, some are strangers, some might be adversarial. The
#   bot's response is broadcast to everyone. A runaway generation
#   burns GPU time that other users' messages are waiting on; a
#   too-clever response is a foot-gun if the bot has tools.
#
#   Defaults in this profile are CONSERVATIVE: short token caps,
#   short timeouts, capped history. If the llama-server can do
#   more, the bot deliberately doesn't ask it to. The user can
#   always override per-deployment via env vars (see below).
#
# Personal / single-user private assistant (opt-in):
#   The bot is a private assistant for one operator. Root shell,
#   full file access, sensitive data. The operator WANTS the bot
#   to push the model to its limits because there is no other
#   user being starved. The shell-tools feature (Track 3) is
#   only ever enabled in this profile.
#
#   This profile is NOT the default. To opt in, set
#   LLAMABOT_DEPLOYMENT_PROFILE=personal. The env vars below
#   then resolve to the personal defaults. Per-env overrides
#   still win.
#
# ============================================================================
# WHY NOT "MAX BY DEFAULT"
# ============================================================================
#
# Not every operator has an RTX 5090 + 128 GB of RAM. Even if
# the operator does, max-by-default is a foot-gun in a public
# chat: a malicious or sloppy user can trigger 8-minute LLM
# calls, and other users' messages pile up behind the semaphore.
# In a personal-assistant deployment, that risk is the
# operator's to take; in a public chat, it is the operator's
# users' to suffer. So the bot's job is to be SAFE out of the
# box, not FAST out of the box.
#
# ============================================================================
# WHY EVERY VALUE IS ENV-OVERRIDEABLE
# ============================================================================
#
# Operator feedback 2026-10-13: "Ограничением для бота должны
# быть параметры llama, если лама не смогла бот - не смог.
# Сам бот не должен ограничивать модель и обрывать
# выполнение запросов." — translated: the bot must not have
# hardcoded caps that hide what llama-server could do. The
# operator is allowed to tune. env vars are the way.
#
# Defaults are *conservative* (safe for public chat), NOT max.
# The operator who wants max sets the env vars.
#
# ============================================================================
# HOW TO TUNE — operators
# ============================================================================
#
# 1) Identify your profile.
#    - LLAMABOT_DEPLOYMENT_PROFILE=public  (default; conservative)
#    - LLAMABOT_DEPLOYMENT_PROFILE=personal (aggressive)
#
# 2) If public, the defaults are tuned for ~5-10 concurrent
#    users in a forum group. If you have more or fewer, scale
#    the per-handler max_tokens and the loop budget up or down.
#    The single biggest lever is MAX_TOKENS_DEFAULT — start
#    there.
#
# 3) If personal, defaults already let the model run long.
#    If even those are not enough (e.g. 4K-char prompts
#    routinely need 60K+ tokens of output), bump
#    LLAMABOT_MAX_TOKENS up first, then LLAMABOT_LOOP_BUDGET_SEC
#    to match.
#
# 4) The reasoning budget cap (REASONING_RATIO_HARD_LIMIT) is
#    the most dangerous one to tune. Qwen 3.8-27B-Ultra-Heretic
#    MTP can spend its ENTIRE token budget on the
#    reasoning_content field (observed 2026-10-13: 94K reasoning
#    chars, 0 content, finish_reason=length on a 4K-char prompt).
#    If you raise this, you are trusting the model to leave room
#    for the actual answer. Don't raise it past 0.7 without
#    a stress test on real prompts.
#
# ============================================================================
# HOW TO TUNE — future maintainers
# ============================================================================
#
# When the operator adds Track 3 (shell-tools), the personal
# profile is the only place where that feature is reachable.
# The public profile rejects shell-tool session creation at
# the handler level. (That check lives in handlers.py /
# dispatch.py, not here — limits.py is for *numeric* caps only,
# not feature gates.)
#
# When llama-server is upgraded (new model, new --ctx-size),
# this file is the single place to revisit. The defaults
# below track the current production llama-server:
#   - model: Qwen3.8-27B-Ultra-Heretic-MTP
#   - --ctx-size: 262144
#   - VRAM: production llama-server is on a single GPU; the
#     operator has not yet shared the model card with this code,
#     so "track 2 stress tests" are still pending.

import sys
import os

import sys as _sys
from logconf import get_logger
log = get_logger(__name__)
log_stderr = get_logger(__name__ + '.stderr', stream=_sys.stderr)


# === Deployment profile selector ===
# public    — conservative; default; safe for multi-user groups.
# personal   — aggressive; opt-in; for single-operator private assistant.
# dev        (T58) — same caps as personal, but bash_command is
#                available in the chat_ids listed in
#                LLAMABOT_DEV_ALLOWED_CHAT_IDS, even when the
#                chat is a supergroup or channel. Operator
#                opt-in. See BASH-SANDBOX.md "Dev mode" for
#                the full contract.
_DEPLOYMENT_PROFILE = os.environ.get("LLAMABOT_DEPLOYMENT_PROFILE", "public").lower()
# !! dev mode (T58) is operator opt-in. See bash_command.py for
# the full list of safe guards that REMAIN in place when dev mode
# is on. The ONLY thing dev mode changes is the chat_type='private'
# gate; everything else (sandbox containment, rate limit, validators,
# tool isolation) is still active. !!
if _DEPLOYMENT_PROFILE not in ("public", "personal", "dev"):
    # Fail loudly rather than silently defaulting to one or the other.
    raise ValueError(
        f"LLAMABOT_DEPLOYMENT_PROFILE={_DEPLOYMENT_PROFILE!r} is not a recognised profile. "
        f"Use 'public' (default, conservative) or 'personal' (aggressive, opt-in)."
    )


# === Profile-default tables ===
# Each profile declares its own defaults. Per-env overrides
# (set after this block) win on top of the chosen profile's
# defaults. The two tables are intentionally separated so that
# the *meaning* of a default is obvious in context, not buried
# in a chain of ternaries.
_PUBLIC_DEFAULTS = {
    # Battle-tested 2026-10-13 against RTX 5090 32GB /
    # Qwen3.8-27B-Ultra-Heretic-MTP / llama-server --ctx-size 262144,
    # with a 4K-char HTML Newton's-cradle prompt through the bot
    # (tools=yes). Real consumption on that prompt:
    #   reasoning  ~86K chars (first iter alone; iter=0 is mostly
    #              thinking before any tool call or answer)
    #   content    ~17K chars (final HTML across 4 Telegram messages)
    #   wall clock ~5-10 min
    #   finish     tool_calls (write_file; one REJECTED, then re-emit)
    # Sizing rule: keep ~1.5-2x headroom over observed max, not 4x.
    "MAX_TOKENS_DEFAULT":    65536,   # 2x over 4K-prompt peak (~100K total
                                       #   output: reasoning + content).
                                       #   Below 32K the 4K-prompt case
                                       #   truncates with content_chars=0.
    "MAX_TOKENS_CAPTION":    16384,   # unchanged: photos / voice transcripts
    "LOOP_BUDGET_SEC":         900,   # 15 min - 1.5x over the 5-10 min the
                                       #   4K-prompt case actually took
    "LLAMA_HTTP_TIMEOUT_SEC": 990.0,  # LOOP_BUDGET_SEC * 1.1
    "LLAMA_MODELS_TIMEOUT_SEC": 10.0, # unchanged
    "TRANSCRIBE_TIMEOUT_SEC":  120.0, # unchanged
    "REASONING_RATIO_HARD_LIMIT": 0.95, # 5% content floor. Qwen 3.8 MTP
                                       #   routinely hits 0.94+ reasoning
                                       #   on long tool-using prompts;
                                       #   0.5 was too aggressive (would
                                       #   abort valid runs that hadn't
                                       #   reached the answer phase yet)
}

_PERSONAL_DEFAULTS = {
    # Battle-tested 2026-10-13, see _PUBLIC_DEFAULTS comment for the
    # 4K-prompt measurement. Same data, but personal uses 2x more
    # headroom because the box is single-user.
    "MAX_TOKENS_DEFAULT":    131072,  # 2x over observed 4K-prompt peak
                                       #   (~100K total output). 128K
                                       #   still leaves room to grow for
                                       #   prompts that need more
                                       #   reasoning than the 4K
                                       #   Newton's-cradle test did.
    "MAX_TOKENS_CAPTION":    65536,   # unchanged from v3
    "LOOP_BUDGET_SEC":        1200,   # 20 min - 2x over the 5-10 min
                                       #   observed. Trimming v3's 30
                                       #   min because nobody waited that
                                       #   long; if a future test takes
                                       #   >20 min, raise this, not
                                       #   MAX_TOKENS_DEFAULT.
    "LLAMA_HTTP_TIMEOUT_SEC": 1320.0, # LOOP_BUDGET_SEC * 1.1
    "LLAMA_MODELS_TIMEOUT_SEC": 30.0, # unchanged
    "TRANSCRIBE_TIMEOUT_SEC":  300.0, # unchanged
    "REASONING_RATIO_HARD_LIMIT": 1.0, # still OFF. The 4K-prompt test
                                       #   had 100% reasoning on iter=0
                                       #   before the model emitted the
                                       #   first tool call; a cap of
                                       #   0.95 would have aborted the
                                       #   run before it could produce
                                       #   the answer. Cap stays off
                                       #   for personal; public gets
                                       #   0.95 (5% content floor).
}

_PROFILE_DEFAULTS = (
    _PERSONAL_DEFAULTS if _DEPLOYMENT_PROFILE == "personal"
    else _PUBLIC_DEFAULTS
)


def _env_or(env_name, profile_value):
    """Resolve a limit: env var (full name) wins, else profile default.

    Type coercion preserves the type of the profile default. We
    accept the source string as written (no float() of an int
    field) but tolerate a stray ".0" on what should be an int
    — operators copy from the example line in the comment and
    occasionally paste "900.0" into an int field. Reject other
    garbage (ValueError from the type constructor) by falling
    back to the profile default with a stderr warning, so a
    typo in .env doesn't take the whole bot down.
    """
    raw = os.environ.get(env_name)
    if raw is None:
        return profile_value
    if isinstance(profile_value, bool):
        return raw.lower() in ("1", "true", "yes", "on")
    if isinstance(profile_value, int):
        # Strip an optional trailing ".0" so "900.0" works.
        if raw.endswith(".0"):
            raw = raw[:-2]
        try:
            return int(raw)
        except ValueError:
            log_stderr.error(f"[limits] WARNING: {env_name}={raw!r} is not a valid int; "
                f"using profile default {profile_value!r}")
            return profile_value
    if isinstance(profile_value, float):
        try:
            return float(raw)
        except ValueError:
            log_stderr.error(f"[limits] WARNING: {env_name}={raw!r} is not a valid float; "
                f"using profile default {profile_value!r}")
            return profile_value
    return raw


MAX_TOKENS_DEFAULT         = _env_or("LLAMABOT_MAX_TOKENS",          _PROFILE_DEFAULTS["MAX_TOKENS_DEFAULT"])
MAX_TOKENS_CAPTION         = _env_or("LLAMABOT_MAX_TOKENS_CAPTION",  _PROFILE_DEFAULTS["MAX_TOKENS_CAPTION"])
LOOP_BUDGET_SEC            = _env_or("LLAMABOT_LOOP_BUDGET_SEC",    _PROFILE_DEFAULTS["LOOP_BUDGET_SEC"])
LLAMA_HTTP_TIMEOUT_SEC     = _env_or("LLAMABOT_LLAMA_HTTP_TIMEOUT",  _PROFILE_DEFAULTS["LLAMA_HTTP_TIMEOUT_SEC"])
LLAMA_MODELS_TIMEOUT_SEC   = _env_or("LLAMABOT_LLAMA_MODELS_TIMEOUT", _PROFILE_DEFAULTS["LLAMA_MODELS_TIMEOUT_SEC"])
TRANSCRIBE_TIMEOUT_SEC     = _env_or("LLAMABOT_TRANSCRIBE_TIMEOUT", _PROFILE_DEFAULTS["TRANSCRIBE_TIMEOUT_SEC"])
REASONING_RATIO_HARD_LIMIT = _env_or("LLAMABOT_REASONING_RATIO_LIMIT", _PROFILE_DEFAULTS["REASONING_RATIO_HARD_LIMIT"])

# T65.1 — silent reaction turn. A reply addressed to another human produces a
# reaction and nothing else: no answer, no visible reasoning. A reaction is one
# emoji plus a short reasoning trace, so this is far below the normal budget.
# Deliberately its own constant rather than a fraction of MAX_TOKENS_DEFAULT:
# a runaway turn here is pure waste, and the number should be obviously small
# to whoever reads the log next.
MAX_TOKENS_REACT_ONLY      = _env_or("LLAMABOT_MAX_TOKENS_REACT_ONLY", 2048)


# === call_llama signature default ===
# The default in call_llama()'s signature is what a future
# caller who doesn't specify max_tokens will get. It tracks
# MAX_TOKENS_DEFAULT so the profile selection propagates to
# the signature too.
MAX_TOKENS_CALL_LLAMA_DEFAULT = MAX_TOKENS_DEFAULT


# === Self-test overrides ===
# The in-tree selftest uses smaller caps to keep the smoke
# test fast. NOT for production tuning. These are constants
# on purpose, not env-driven, because the selftest must be
# deterministic across deployments.
SELFTEST_MAX_TOKENS = 1024
SELFTEST_LOOP_BUDGET_SEC = 60


# === Self-check at import time ===
# Catch misconfiguration early instead of at first call.
if MAX_TOKENS_DEFAULT <= 0:
    raise ValueError("MAX_TOKENS_DEFAULT must be positive")
if MAX_TOKENS_CAPTION <= 0:
    raise ValueError("MAX_TOKENS_CAPTION must be positive")
if LOOP_BUDGET_SEC <= 0:
    raise ValueError("LOOP_BUDGET_SEC must be positive")
if LLAMA_HTTP_TIMEOUT_SEC <= LOOP_BUDGET_SEC:
    # The httpx timeout must be greater than the loop budget
    # so the client doesn't fire its own timeout mid-loop.
    raise ValueError(
        f"LLAMA_HTTP_TIMEOUT_SEC ({LLAMA_HTTP_TIMEOUT_SEC}) must be "
        f"strictly greater than LOOP_BUDGET_SEC ({LOOP_BUDGET_SEC})"
    )
if not 0 <= REASONING_RATIO_HARD_LIMIT <= 1:
    raise ValueError(
        f"REASONING_RATIO_HARD_LIMIT ({REASONING_RATIO_HARD_LIMIT}) "
        f"must be in the closed interval [0, 1]; 0 = no answer at all, "
        f"1 = no cap (model can spend the whole budget on reasoning)"
    )
