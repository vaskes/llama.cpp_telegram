#!/usr/bin/env bash
# update.sh — pull the latest from this repo and redeploy both services.
# Run from inside a clone of llama.cpp_telegram.
set -euo pipefail

BOT_DIR="${BOT_DIR:-/opt/telegram-bot}"
WHISPER_DIR="${WHISPER_DIR:-/opt/whisper-api}"

echo ">>> git pull"
git pull --rebase --autostash

echo ">>> copy updated files into $BOT_DIR"
sudo cp telegram-bot/bot.py "$BOT_DIR/bot.py"
sudo cp telegram-bot/Dockerfile "$BOT_DIR/Dockerfile"
sudo cp telegram-bot/requirements.txt "$BOT_DIR/requirements.txt"
sudo cp telegram-bot/docker-compose.yml "$BOT_DIR/docker-compose.yml"
# DO NOT touch .env — it has your credentials.

# .env.example may have new keys; merge them in if missing.
if ! sudo diff -q "$BOT_DIR/.env" "$BOT_DIR/.env.example" >/dev/null; then
  echo "  [INFO] .env differs from .env.example — diff:"
  sudo diff "$BOT_DIR/.env" "$BOT_DIR/.env.example" || true
fi

echo ">>> copy updated files into $WHISPER_DIR"
sudo cp whisper-api/docker-compose.yml "$WHISPER_DIR/docker-compose.yml"
# We do NOT touch whisper-api/.env either; the same reasoning.

echo ">>> rebuild & restart telegram-bot"
cd "$BOT_DIR"
sudo docker compose build telegram-bot
sudo docker compose up -d --no-deps --force-recreate telegram-bot

echo ">>> restart whisper-api (it doesn't need a rebuild unless the image changed)"
cd "$WHISPER_DIR"
sudo docker compose up -d --no-deps --force-recreate whisper-api

echo "  [OK] done. Tail logs with:"
echo "    sudo docker logs -f telegram-bot"
echo "    sudo docker logs -f whisper-api"
