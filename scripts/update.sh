#!/usr/bin/env bash
# update.sh — pull the latest from this repo and redeploy the bot.
# Run from inside a clone of llama.cpp_telegram.
set -euo pipefail

BOT_DIR="${BOT_DIR:-/opt/telegram-bot}"

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

echo ">>> rebuild & restart"
cd "$BOT_DIR"
sudo docker compose build telegram-bot
sudo docker compose up -d --no-deps --force-recreate telegram-bot
echo "  [OK] done. Tail logs with: sudo docker logs -f telegram-bot"
