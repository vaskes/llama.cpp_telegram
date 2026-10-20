#!/usr/bin/env bash
# update.sh — pull the latest from this repo and redeploy the bot.
# Run from inside a clone of llama.cpp_telegram.
#
# T64: this script used to deploy a FRANKENSTEIN build. It copied only
# bot.py + Dockerfile + requirements.txt + docker-compose.yml out of
# 13 python modules, so `dispatch.py` / `handlers.py` / `call_llama.py`
# / `personal_tools/` stayed at whatever version was previously in
# $BOT_DIR. Given T53/T60 made `chat_type` a required keyword-only
# argument and T62 made dispatch.py import `from handlers import (...)`,
# a mixed build dies with ImportError/TypeError on the first message.
# docs/DEPLOY-NOTES.md even documented a manual 10-file `cp` as the
# workaround, which is the tell: the script was known-broken and
# nobody fixed the script.
#
# It then compounded that by hardcoding the container name `telegram-bot`
# while production (docker-compose.production.yml) names it `tb-bot`, and
# by copying the GENERIC compose over the top of the production one.
#
# Design rules now enforced by tests/test_deploy_artifacts.py:
#   1. no host-specific literals (container names, volume names, IPs)
#   2. no `up -d --force-recreate` without `--build` before it
#   3. every compose invocation passes an explicit -f
#   4. the entire telegram-bot/ tree is synced, not a hand-listed subset
set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration — everything overridable, nothing host-specific baked in.
# ---------------------------------------------------------------------------
BOT_DIR="${BOT_DIR:-/opt/telegram-bot}"
WHISPER_DIR="${WHISPER_DIR:-/opt/whisper-api}"
SERVICE="${SERVICE:-telegram-bot}"

# Which compose file to drive. Empty = auto-detect, see resolve_compose().
COMPOSE_FILE="${COMPOSE_FILE:-}"

# SKIP_WHISPER=1 for deployments where whisper runs elsewhere (or the
# bot is deployed on its own, as on llmhost1 where whisper is a sibling
# stack under /opt/telegrambot/).
SKIP_WHISPER="${SKIP_WHISPER:-0}"

# DRY_RUN=1 prints every mutating command and exits without doing it.
DRY_RUN="${DRY_RUN:-0}"

# The whisper service name, also overridable for parity with SERVICE.
WHISPER_SERVICE="${WHISPER_SERVICE:-whisper-api}"

log()  { printf '>>> %s\n' "$*"; }
info() { printf '  [INFO] %s\n' "$*"; }
warn() { printf '  [WARN] %s\n' "$*"; }
die()  { printf '  [FAIL] %s\n' "$*" >&2; exit 1; }

run() {
    if [[ "$DRY_RUN" == "1" ]]; then
        printf '  [DRY-RUN] %s\n' "$*"
    else
        "$@"
    fi
}

# ---------------------------------------------------------------------------
# Compose resolution — never let docker compose guess.
#
# `docker compose` with no -f picks docker-compose.yml, which is the
# GENERIC/dev layout: container_name telegram-bot, volume bot_data,
# /var/log/telegram-bot bind. Production uses tb-bot + tb_bot_data.
# Guessing wrong orphans the SQLite conversation history. So: pick
# explicitly, every time.
# ---------------------------------------------------------------------------
resolve_compose() {
    local chosen

    if [[ -n "$COMPOSE_FILE" ]]; then
        [[ -f "$COMPOSE_FILE" ]] || die "COMPOSE_FILE=$COMPOSE_FILE does not exist"
        chosen="$COMPOSE_FILE"
        info "compose file: $chosen (explicit)"
    elif [[ -f "$BOT_DIR/docker-compose.production.yml" ]]; then
        chosen="$BOT_DIR/docker-compose.production.yml"
        info "compose file: $chosen (auto-detected production layout)"
    else
        chosen="$BOT_DIR/docker-compose.yml"
        [[ -f "$chosen" ]] || die "no compose file in $BOT_DIR — is BOT_DIR correct?"
        info "compose file: $chosen (auto-detected generic layout)"
    fi

    COMPOSE_FILE="$chosen"
    DOCKER_COMPOSE=(docker compose -f "$COMPOSE_FILE")

    # `docker compose config` must succeed, otherwise every later compose
    # call silently no-ops. This exact bug shipped once already: the
    # migration check ran before COMPOSE_FILE was assigned and reported
    # "no named volume" for a compose that declares one.
    if ! docker compose -f "$COMPOSE_FILE" config >/dev/null 2>&1; then
        die "cannot parse $COMPOSE_FILE — refusing to deploy against a compose that does not load"
    fi
}

# Read container_name straight out of the compose file we are going to
# drive. Never hardcode it: the generic and production layouts disagree,
# and a stale literal is how you end up tailing the wrong container's
# logs while the real one crash-loops.
resolve_container_name() {
    CONTAINER_NAME="$(docker compose -f "$COMPOSE_FILE" config \
        --format json 2>/dev/null \
        | python3 -c "import json,sys; print(json.load(sys.stdin)['services']['$SERVICE'].get('container_name','$SERVICE'))" \
        2>/dev/null || true)"
    [[ -n "$CONTAINER_NAME" ]] || CONTAINER_NAME="$SERVICE"
    info "container: $CONTAINER_NAME (from $COMPOSE_FILE)"
}

# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------
log "preflight"
[[ -d "$BOT_DIR" ]] || die "BOT_DIR=$BOT_DIR does not exist — nothing to update"
[[ -d telegram-bot ]] || die "run this from inside the llama.cpp_telegram clone"
git rev-parse --git-dir >/dev/null 2>&1 || die "not a git clone"

PREV_HEAD="$(git rev-parse --short HEAD)"

# ---------------------------------------------------------------------------
# Sync the source tree
# ---------------------------------------------------------------------------
log "sync telegram-bot/ -> $BOT_DIR"

# rsync if available (--delete keeps stale modules from lingering), else cp.
# Exclusions are load-bearing:
#   .env   — operator's credentials, never overwrite
#   data/  — legacy bind-mount contents, migration reads it but never writes
#   logs/  — live log output owned by the running container
#   files/ — user uploads, persisted outside the image
if command -v rsync >/dev/null 2>&1; then
    RSYNC_ARGS=(-a --delete
        --exclude '.env'
        --exclude 'data/'
        --exclude 'logs/'
        --exclude 'files/'
        --exclude '__pycache__/'
        --exclude '*.pyc')
    if [[ "$DRY_RUN" == "1" ]]; then
        printf '  [DRY-RUN] rsync %s telegram-bot/ %s/\n' "${RSYNC_ARGS[*]}" "$BOT_DIR"
    else
        sudo rsync "${RSYNC_ARGS[@]}" telegram-bot/ "$BOT_DIR/"
    fi
    info "rsync: whole tree, .env/data/logs/files preserved"
else
    warn "rsync not found — falling back to cp; stale files will NOT be removed"
    for item in telegram-bot/*; do
        base="$(basename "$item")"
        [[ "$base" == "data" || "$base" == "logs" || "$base" == "files" ]] && continue
        run sudo cp -r "$item" "$BOT_DIR/"
    done
    run sudo cp telegram-bot/.env.example "$BOT_DIR/.env.example"
    info "cp fallback: tree copied, no --delete equivalent"
fi

# Sanity check: every module the image needs must now exist in $BOT_DIR.
# This is the check that would have caught the 4-of-17 copy bug on the
# very first run, and it is cheap.
MISSING=0
for mod in telegram-bot/*.py; do
    base="$(basename "$mod")"
    if [[ ! -f "$BOT_DIR/$base" ]]; then
        warn "missing after sync: $base"
        MISSING=$((MISSING + 1))
    fi
done
if [[ ! -d "$BOT_DIR/personal_tools" ]]; then
    warn "missing after sync: personal_tools/"
    MISSING=$((MISSING + 1))
fi
[[ "$MISSING" -eq 0 ]] || die "$MISSING file(s) missing after sync — refusing to deploy a partial build"
info "all $(ls telegram-bot/*.py | wc -l | tr -d ' ') modules + personal_tools/ present"

# .env must survive. If it vanished, we are about to start a container
# with no BOT_TOKEN.
[[ -f "$BOT_DIR/.env" ]] || die "$BOT_DIR/.env is missing — refusing to deploy without credentials"
info ".env present (not touched)"

resolve_compose
resolve_container_name

# Report .env.example keys that are missing from .env — by NAME only.
# Never diff the files directly: `diff .env .env.example` prints BOT_TOKEN
# and WEBHOOK_SECRET_TOKEN into the terminal and into any CI log that
# captures stdout. The old script did exactly that.
mapfile -t ENV_KEYS < <(grep -oE '^[A-Z_][A-Z0-9_]*=' telegram-bot/.env.example | tr -d '=' | sort -u)
MISSING_ENV=()
for key in "${ENV_KEYS[@]}"; do
    grep -qE "^${key}=" "$BOT_DIR/.env" 2>/dev/null || MISSING_ENV+=("$key")
done
if [[ "${#MISSING_ENV[@]}" -gt 0 ]]; then
    info ".env is missing ${#MISSING_ENV[@]} key(s) present in .env.example:"
    printf '    %s\n' "${MISSING_ENV[@]}"
    info "(values not shown; add them to $BOT_DIR/.env if the feature is needed)"
else
    info ".env has every key from .env.example"
fi

# ---------------------------------------------------------------------------
# Pull latest (skipped under DRY_RUN so a rehearsal mutates nothing)
# ---------------------------------------------------------------------------
if [[ "$DRY_RUN" == "1" ]]; then
    log "git pull (skipped: DRY_RUN)"
else
    log "git pull"
    git pull --rebase --autostash

    log "update submodules"
    git submodule update --init --recursive
    git submodule status
fi

# ---------------------------------------------------------------------------
# Legacy bind-mount -> named volume migration (T52 #R2), read from compose
# ---------------------------------------------------------------------------
# The old layout put the SQLite in $BOT_DIR/data via bind-mount. The named
# volume in the compose we are driving is what the container will mount.
# Seed it from the bind-mount once, if the source exists.
log "check data migration (T52 #R2)"
# `config --volumes` prints the volume key(s); with an explicit project
# `name:` the real on-disk volume is "<project>_<key>". Read the key, then
# ask compose itself for the resolved name so the prefix is never guessed.
DATA_VOLUME_KEY="$(docker compose -f "$COMPOSE_FILE" config --volumes 2>/dev/null | head -1 || true)"
if [[ -z "$DATA_VOLUME_KEY" ]]; then
    info "compose declares no named volume — nothing to migrate"
elif [[ ! -f "$BOT_DIR/data/conversations.db" ]]; then
    info "no legacy bind-mount at $BOT_DIR/data — nothing to migrate"
else
    DATA_VOLUME="$(docker compose -f "$COMPOSE_FILE" config 2>/dev/null \
        | python3 -c "import json,sys;d=json.load(sys.stdin);print(list(d.get('volumes',{}))[0])" 2>/dev/null || echo "$DATA_VOLUME_KEY")"
    info "legacy bind-mount present at $BOT_DIR/data — volume key '$DATA_VOLUME_KEY'"
    if [[ "$DRY_RUN" == "1" ]]; then
        printf '  [DRY-RUN] would check/seed volume for key %s (not touching it)\n' "$DATA_VOLUME_KEY"
    else
        SEEDED="$(docker run --rm -v "$DATA_VOLUME:/check" alpine:latest \
            test -f /check/conversations.db 2>/dev/null && echo yes || echo no)"
        if [[ "$SEEDED" == "yes" ]]; then
            info "volume already seeded — skipping"
        else
            warn "seeding volume from $BOT_DIR/data (first run after migration)"
            sudo docker run --rm \
                -v "$DATA_VOLUME:/target" \
                -v "$BOT_DIR/data:/source:ro" \
                alpine:latest cp -a /source/. /target/
        fi
    fi
fi

# ---------------------------------------------------------------------------
# Build + up — always --build, never bare --force-recreate
# ---------------------------------------------------------------------------
# COMPOSE_FILE / CONTAINER_NAME were already resolved above; resolving
# again here is harmless (idempotent) and documents the invariant, but the
# guarantee is that resolve_compose() ran BEFORE anything read COMPOSE_FILE.

log "build + up (compose: $COMPOSE_FILE)"
# --build first: `--force-recreate` alone recreates from the STALE image,
# which is the exact failure mode that made an update look like a no-op.
run sudo "${DOCKER_COMPOSE[@]}" build "$SERVICE"
run sudo "${DOCKER_COMPOSE[@]}" up -d --no-deps "$SERVICE"

# ---------------------------------------------------------------------------
# Verify against the REAL container name
# ---------------------------------------------------------------------------
log "verify $CONTAINER_NAME came up clean"
sleep 3

if [[ "$DRY_RUN" == "1" ]]; then
    printf '  [DRY-RUN] docker logs %s --tail 20\n' "$CONTAINER_NAME"
else
    if sudo docker logs "$CONTAINER_NAME" --tail 20 2>&1 \
        | grep -iE "TypeError|SyntaxError|Traceback|ImportError|ModuleNotFoundError"; then
        warn "bot log contains errors. Inspect with:"
        echo "    sudo docker logs -f $CONTAINER_NAME"
        echo "  Then roll back:"
        echo "    cd $(git rev-parse --show-toplevel) && git checkout $PREV_HEAD && bash scripts/update.sh"
        exit 1
    fi
    info "no import/syntax errors in the last 20 log lines"
fi

# ---------------------------------------------------------------------------
# Whisper — separate stack, no local build, so no --build needed
# ---------------------------------------------------------------------------
if [[ "$SKIP_WHISPER" == "1" ]]; then
    info "SKIP_WHISPER=1 — leaving whisper alone"
elif [[ ! -d "$WHISPER_DIR" ]]; then
    info "WHISPER_DIR=$WHISPER_DIR not present — skipping whisper (set SKIP_WHISPER=1 to silence this)"
else
    log "restart whisper"
    if [[ -f "$WHISPER_DIR/docker-compose.yml" ]]; then
        # Public image (faster-whisper-server), nothing local to build.
        run sudo docker compose -f "$WHISPER_DIR/docker-compose.yml" \
            up -d --no-deps "$WHISPER_SERVICE"
    else
        warn "no compose file in $WHISPER_DIR — skipping"
    fi
fi

NEW_HEAD="$(git rev-parse --short HEAD 2>/dev/null || echo "$PREV_HEAD")"
if [[ "$DRY_RUN" == "1" ]]; then
    log "done (dry run — nothing was changed)"
else
    log "done: $PREV_HEAD -> $NEW_HEAD"
    echo "    sudo docker logs -f $CONTAINER_NAME"
fi