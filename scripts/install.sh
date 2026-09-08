#!/usr/bin/env bash
# install.sh — bootstrap both services on a fresh host.
# Run as a regular user with sudo.
set -euo pipefail

BOT_DIR="${BOT_DIR:-/opt/telegram-bot}"
WHISPER_DIR="${WHISPER_DIR:-/opt/whisper-api}"

echo ">>> Installing to $BOT_DIR and $WHISPER_DIR"

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
  2) Start llama-server (this repo assumes you have one — see the llama.cpp_search
     repo for the SearXNG side, and llama.cpp-rocm-780m for the GPU build).
  3) Start the services:
       sudo systemctl start whisper-api-compose
       sudo systemctl start telegram-bot-compose
  4) Open Telegram, find your bot, send /start.
NEXT
