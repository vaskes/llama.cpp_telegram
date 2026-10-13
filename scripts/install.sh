#!/usr/bin/env bash
# install.sh — bootstrap both services on a fresh host.
# Run as a regular user with sudo.
set -euo pipefail

BOT_DIR="${BOT_DIR:-/opt/telegram-bot}"
WHISPER_DIR="${WHISPER_DIR:-/opt/whisper-api}"

echo ">>> Installing to $BOT_DIR and $WHISPER_DIR"

# If the operator has uncommitted local edits in the working copy,
# the `cp -r` below will silently overwrite them. Warn and give a
# short window to abort.
if ! git diff --quiet HEAD 2>/dev/null || ! git diff --cached --quiet 2>/dev/null; then
    echo "  [WARN] Repo has uncommitted changes. install.sh will copy current"
    echo "         files into $BOT_DIR and $WHISPER_DIR, potentially overwriting"
    echo "         your edits. Press Ctrl+C within 5 seconds to abort."
    sleep 5
fi

# 1) telegram-bot
sudo mkdir -p "$BOT_DIR/files"
sudo cp -r telegram-bot/* "$BOT_DIR/"
sudo cp telegram-bot/.env.example "$BOT_DIR/.env.example"
[[ -f "$BOT_DIR/.env" ]] || sudo cp "$BOT_DIR/.env.example" "$BOT_DIR/.env"
sudo chown -R "$USER":"$USER" "$BOT_DIR"
echo "  [OK] telegram-bot files in $BOT_DIR — edit $BOT_DIR/.env with your BOT_TOKEN and ALLOWED_USER_IDS"

# 2) whisper-api
sudo mkdir -p "$WHISPER_DIR/cache"
sudo cp whisper-api/docker-compose.yml "$WHISPER_DIR/"
sudo cp whisper-api/.env.example "$WHISPER_DIR/.env.example"
[[ -f "$WHISPER_DIR/.env" ]] || sudo cp "$WHISPER_DIR/.env.example" "$WHISPER_DIR/.env"
sudo chown -R "$USER":"$USER" "$WHISPER_DIR"
echo "  [OK] whisper-api files in $WHISPER_DIR — edit $WHISPER_DIR/.env if you want a different model"

# 2.5) bash-sandbox (optional, personal mode only)
# The killer feature bash_command requires the vaskes/OpenWebUI-bash-sandbox
# side-car. Skip this step if you do not want bash_command.
if [[ "${INSTALL_BASH_SANDBOX:-ask}" == "ask" ]]; then
    read -r -p "Install bash-sandbox side-car for bash_command (personal mode)? [y/N] " ans
    if [[ "$ans" =~ ^[Yy]$ ]]; then
        INSTALL_BASH_SANDBOX=1
    else
        INSTALL_BASH_SANDBOX=0
    fi
fi
if [[ "${INSTALL_BASH_SANDBOX:-0}" == "1" ]]; then
    if [[ ! -d .git ]] || ! git submodule status sandbox 2>/dev/null | grep -q '^[ -]'; then
        echo "  [INFO] initialising git submodule for sandbox..."
        git submodule update --init --recursive
    fi
    SANDBOX_DIR="${SANDBOX_DIR:-/opt/bash-sandbox}"
    sudo mkdir -p "$SANDBOX_DIR"
    sudo cp -r sandbox/* "$SANDBOX_DIR/"
    sudo chown -R "$USER":"$USER" "$SANDBOX_DIR"
    echo "  [OK] bash-sandbox files in $SANDBOX_DIR — follow the upstream README"
    echo "         to deploy the openapi-proxy. Then set AGENT_STACK_URL and"
    echo "         AGENT_STACK_TOKEN in $BOT_DIR/.env and switch the bot's"
    echo "         profile to personal: LLAMABOT_DEPLOYMENT_PROFILE=personal"
    echo "         See docs/BASH-SANDBOX.md for the full setup."
fi

# 3) systemd units
for unit in systemd/telegram-bot-compose.service systemd/whisper-api-compose.service; do
  base="$(basename "$unit")"
  sudo cp "$unit" "/etc/systemd/system/$base"
  sudo systemctl daemon-reload
  sudo systemctl enable "$base"
  echo "  [OK] enabled $base"
done

# 4) Reminder
cat <<'NEXT'

NEXT STEPS:
  1) Edit $BOT_DIR/.env — at minimum set BOT_TOKEN, ALLOWED_USER_IDS, and LLAMA_URL.
     Find your Telegram user_id by messaging @userinfobot.
  2) Start llama-server (any OpenAI-compatible endpoint; the bot does
     not include llama.cpp itself). See docs/SETUP.md for the
     recommended build.
  3) Start the services:
       sudo systemctl start whisper-api-compose
       sudo systemctl start telegram-bot-compose
  4) (personal mode only) If you opted into bash-sandbox above,
     deploy the side-car at $SANDBOX_DIR per its README, then
     set AGENT_STACK_URL and AGENT_STACK_TOKEN in $BOT_DIR/.env.
  5) Open Telegram, find your bot, send /start. If you enabled
     bash-sandbox, you should see the bash_command line in the
     /start reply; if not, /start shows only web tools + react.
NEXT
