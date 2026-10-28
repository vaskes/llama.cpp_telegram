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
# The bot has *three* canonical deployment profiles. They differ
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
# Dev / single-operator box, single-user group (opt-in):
#   The operator's own machine, one chat, one GPU, and the
#   llama-server next door running with --reasoning-budget 65536.
#   The thinking cap lives THERE, not here: llama-server cuts the
#   reasoning block and the model moves on to the answer. This
#   profile therefore has no bot-side thinking cap at all (T70.6:
#   the knob was removed, because a limit nobody enforces is a
#   trap) and gives every path enough token room to finish an
#   answer AFTER llama has spent up to 64K on thinking.
#
#   Before 2026-10-23 this profile was a PHANTOM: the name was
#   accepted by the validation below, but the selection fell
#   through to the public defaults, so "dev" silently meant
#   "conservative public". The operator thought he was running the
#   aggressive numbers and was getting the public ones. That is
#   what the _DEV_DEFAULTS table fixes.
#
# ============================================================================
# THE INVARIANT: every per-path cap must exceed llama's reasoning budget
# ============================================================================
#
# llama-server's --reasoning-budget N caps the THINKING block at N
# tokens. The answer is generated after that, and it draws from the
# SAME max_tokens. So if a path's max_tokens is not comfortably
# above the thinking budget, the thinking can eat the entire cap and
# the answer is lost -- observed 2026-10-23 20:45 on the document
# path: reasoning 77977 chars, content 0, finish_reason=length.
#
# The invariant is per DEPLOYMENT, not per file: it compares a path's
# cap against the thinking budget of the llama-server that profile
# runs against. The dev table below is sized for the dev box, whose
# llama-server runs --reasoning-budget 65536, so every cap in that
# table is above 65536. The public table is sized for a llama-server
# with a much smaller thinking budget and is deliberately strict --
# if you point a profile at a llama-server with a 64K thinking
# budget, raise that profile's caps with it.
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
# 4) The THINKING budget is not a bot-side knob -- it is
#    llama-server's --reasoning-budget (65536 on the dev box; see
#    llama-qwen5090-256k.service). Qwen 3.8-27B-Ultra-Heretic
#    MTP can spend its ENTIRE token budget on the
#    reasoning_content field (observed 2026-10-13: 94K reasoning
#    chars, 0 content, finish_reason=length on a 4K-char prompt).
#    The bot's job is to leave room for the answer that comes
#    AFTER that cap: every per-path max_tokens here must exceed
#    the engine's thinking budget, or the answer has nowhere to go
#    (observed 2026-10-23 20:45 on the document path: reasoning
#    77977 chars, content 0). If you raise the engine's budget,
#    raise these with it.
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
    "LOOP_BUDGET_SEC":        7200,   # 2 h. Raised 2026-10-27 from 900 s:
                                       #   the sandbox command limit is an
                                       #   hour (AGENT_EXEC_TIMEOUT=3600 in
                                       #   the production deployment), and a
                                       #   turn whose budget is shorter than
                                       #   the command it runs throws the
                                       #   answer away.
    "LLAMA_HTTP_TIMEOUT_SEC": 7920.0, # LOOP_BUDGET_SEC * 1.1
    "LLAMA_MODELS_TIMEOUT_SEC": 10.0, # unchanged
    "TRANSCRIBE_TIMEOUT_SEC":  120.0, # unchanged
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
    "LOOP_BUDGET_SEC":        7200,   # 2 h. Raised 2026-10-27 from 1200 s:
                                       #   the sandbox command limit is an
                                       #   hour (AGENT_EXEC_TIMEOUT=3600 in
                                       #   the production deployment), and a
                                       #   turn whose budget is shorter than
                                       #   the command it runs throws the
                                       #   answer away.
    "LLAMA_HTTP_TIMEOUT_SEC": 7920.0, # LOOP_BUDGET_SEC * 1.1
    "LLAMA_MODELS_TIMEOUT_SEC": 30.0, # unchanged
    "TRANSCRIBE_TIMEOUT_SEC":  300.0, # unchanged
}

_DEV_DEFAULTS = {
    # The operator's own box (2026-10-23). Single user, single GPU,
    # llama-server on the same host running:
    #   --ctx-size 262144 --reasoning-budget 65536
    # so the thinking block is capped at 65536 tokens BY LLAMA, and
    # the answer is generated after that from the same max_tokens.
    # That is why every cap here sits above 64K: the cap has to
    # cover the thinking budget AND the answer that follows it.
    #
    # Measured 2026-10-23 on the 4K-char Newton's-cradle prompt:
    # the model reached ~57-58K reasoning tokens, wrote the result to
    # a file and continued with the agentic tool loop. At 121 tok/s
    # a 128K budget is ~18 min of generation, hence the 60 min loop
    # budget.
    "MAX_TOKENS_DEFAULT":    131072,  # 128K: 64K thinking + 64K answer
    "MAX_TOKENS_CAPTION":     98304,  # 64K thinking + 32K answer. The
                                       #   photo/voice/document paths are
                                       #   one-shot, so their answers are
                                       #   short -- but they still have to
                                       #   clear the thinking budget, or
                                       #   the model returns content=0.
    "LOOP_BUDGET_SEC":        7200,  # 2 h. Raised 2026-10-27 from 3600 s:
                                    #   the sandbox command limit is an hour
                                    #   (AGENT_EXEC_TIMEOUT=3600 in the
                                    #   production deployment), and a turn
                                    #   whose budget is not longer than the
                                    #   command it runs loses the answer:
                                    #   the guard aborts the turn the moment
                                    #   the command returns, and the user
                                    #   gets "time budget exceeded" instead
                                    #   of the answer.
    "LLAMA_HTTP_TIMEOUT_SEC": 7920.0, # LOOP_BUDGET_SEC * 1.1
    "LLAMA_MODELS_TIMEOUT_SEC": 30.0,
    "TRANSCRIBE_TIMEOUT_SEC":  300.0,
}


_PROFILE_DEFAULTS = {
    "public": _PUBLIC_DEFAULTS,
    "personal": _PERSONAL_DEFAULTS,
    "dev": _DEV_DEFAULTS,
}[_DEPLOYMENT_PROFILE]


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
# T65.1 — silent reaction turn. A reply addressed to another human produces a
# reaction and nothing else: no answer, no visible reasoning. A reaction is one
# emoji plus a short reasoning trace, so this is far below the normal budget.
# Deliberately its own constant rather than a fraction of MAX_TOKENS_DEFAULT:
# a runaway turn here is pure waste, and the number should be obviously small
# to whoever reads the log next.
MAX_TOKENS_REACT_ONLY      = _env_or("LLAMABOT_MAX_TOKENS_REACT_ONLY", 2048)


# === The tool loop's iteration cap ===
#
# How many times one turn may ask the engine and act on what it said.
#
# An ITERATION is one round of the loop: the engine is asked, the tools
# it asked for run, their results go back into the context, and the
# loop asks again. Fifteen iterations is a turn that can read a page,
# look at the answer, and correct itself a few times over.
#
# WHY THIS IS A SETTING AND NOT A CONSTANT. It was a constant -- 15,
# in llm_iteration.py -- until 2026-10-28, and that was wrong: a cap
# this important has to be the operator's, because the two deployments
# want opposite things. A public bot wants a SMALL cap: every
# iteration of a loop that will not finish is GPU time that other
# users' messages are waiting on. A single-operator box wants a LARGE
# one: the work there is long (a crawl, a build, a research task) and
# the operator is the only one paying for the GPU.
#
# WHAT RAISING IT COSTS, measured rather than assumed:
#
#  * the context grows by two messages per iteration (the assistant
#    turn and the tool result), and the request is rebuilt from it
#    every time -- so the tokens and the latency of every later
#    iteration grow with it;
#  * a model that is STUCK spends the whole cap. Measured on
#    production, 2026-10-28: a turn that asked for a web search
#    burned all fifteen iterations GUESSING URLs that answered 404 --
#    one guess per iteration -- and answered nothing at all. A
#    hundred iterations would have been a hundred guesses.
#
# So a large cap is not a free win: it is the room a long task needs
# AND the room a runaway loop gets. Raise it when the task is long
# and the model is known to make progress; keep it small when the
# model might not be.
#
# The value belongs to the deployment: the repository ships 15, and a
# deployment that needs more sets LLAMABOT_MAX_ITER in its own
# environment.
MAX_ITER = _env_or("LLAMABOT_MAX_ITER", 15)


# === The tool trace the CHAT gets ===
#
# When the model runs shell commands, the bot puts a transcript of them
# in front of the answer: what it ran, and what it printed. The
# operator asked for that on 2026-10-24 ("the console output belongs in
# the chat") because without it the user sees the model's paraphrase and
# nothing else -- and because a transcript without its output is half a
# transcript.
#
# WHAT IT COST, measured rather than assumed. The transcript had a cap
# per command (2000 characters of output) and NO cap on the whole thing,
# so its size grew with the number of commands the model ran. Measured
# on production, 2026-10-28: a review task ran 44 commands, the
# transcript came to 77 044 characters, the answer to 81 154 -- and the
# chat got 22 messages of console noise in front of the 4 110
# characters of actual review. The flood then hit Telegram's rate
# limit: 8 of those 22 messages were delivered TWICE, because a send
# that timed out was retried with the same text (see handlers._reply).
# Forty-four commands of noise is not a transcript, it is a wall.
#
# So the transcript is bounded as a whole: the head and the tail of the
# command list are kept, the middle is dropped, and the drop is marked
# with the number of commands that are not shown. The head carries what
# the turn started with; the tail carries what it ended with, and the
# tail is the part the model quotes in its answer.
#
# The bot's MEMORY keeps the whole transcript (`raw` in
# llm_result.unwrap_answer): a follow-up question about what a command
# printed still has the answer. Delivery and memory are two different
# artefacts, and only the chat's copy is bounded.
#
# 0 means: the chat gets no transcript at all. The model still ran the
# commands, the memory still has them, and the answer still arrives.
#
# The value belongs to the deployment: the repository ships 4000 (one
# Telegram message), and a deployment that wants a clean chat sets it
# to 0.
MAX_TRANSCRIPT_CHARS = _env_or("LLAMABOT_MAX_TRANSCRIPT_CHARS", 4000)


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
if MAX_ITER <= 0:
    raise ValueError(
        f"MAX_ITER must be positive (got {MAX_ITER}). A turn that may "
        "not iterate is a turn that may not use a tool at all."
    )
if MAX_TRANSCRIPT_CHARS < 0:
    raise ValueError(
        f"MAX_TRANSCRIPT_CHARS must not be negative (got "
        f"{MAX_TRANSCRIPT_CHARS}). 0 is a valid budget -- it means the "
        "chat gets no transcript -- but a negative one is a typo."
    )
if LLAMA_HTTP_TIMEOUT_SEC <= LOOP_BUDGET_SEC:
    # The httpx timeout must be greater than the loop budget
    # so the client doesn't fire its own timeout mid-loop.
    raise ValueError(
        f"LLAMA_HTTP_TIMEOUT_SEC ({LLAMA_HTTP_TIMEOUT_SEC}) must be "
        f"strictly greater than LOOP_BUDGET_SEC ({LOOP_BUDGET_SEC})"
    )
