#!/usr/bin/env bash
# update.sh — pull the latest from this repo and redeploy both services.
# Run from inside a clone of llama.cpp_telegram.
set -euo pipefail

BOT_DIR="${BOT_DIR:-/opt/telegram-bot}"
WHISPER_DIR="${WHISPER_DIR:-/opt/whisper-api}"

echo ">>> git pull"
git pull --rebase --autostash

echo ">>> update submodules (sandbox)"
# The bash-sandbox submodule may have been bumped. Update it
# to the recorded SHA if the operator wants the new code.
# Use --init to handle the case where the submodule was never
# initialised (e.g. operator started on a pre-submodule tag).
git submodule update --init --recursive
echo "  [OK] submodules at:"
git submodule status

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

echo ">>> migrate data/ -> named volume bot_data (T52 #R2)"
# After the R2 change, /app/data inside the container is backed
# by a named Docker volume (bot_data), not a host bind-mount.
# On first deploy with the new compose, the named volume is
# empty and the bot would start with no chat history.
# Seed the volume from the existing bind-mount contents at
# /opt/telegram-bot/data/ so the conversation history survives
# the migration.
if sudo test -d "$BOT_DIR/data" && sudo test -f "$BOT_DIR/data/conversations.db"; then
  # Create the volume (idempotent: no-op if it already exists).
  sudo docker volume inspect bot_data >/dev/null 2>&1 || \
      sudo docker volume create bot_data >/dev/null
  # Check whether the volume is already seeded (has a
  # conversations.db inside). If yes, skip migration.
  if ! sudo docker run --rm -v bot_data:/check alpine:latest \
      test -f /check/conversations.db 2>/dev/null; then
    echo "  [MIGRATE] seeding bot_data volume from $BOT_DIR/data/"
    sudo docker run --rm \
        -v bot_data:/target \
        -v "$BOT_DIR/data":/source:ro \
        alpine:latest \
        cp -a /source/. /target/
    echo "  [MIGRATE] done. Old bind-mount at $BOT_DIR/data is left in place as a safety net."
  else
    echo "  [MIGRATE] bot_data already seeded, skipping"
  fi
fi

echo ">>> rebuild & restart telegram-bot"
cd "$BOT_DIR"
sudo docker compose build telegram-bot
sudo docker compose up -d --no-deps --force-recreate telegram-bot

echo ">>> verify the bot came up clean"
sleep 3
if sudo docker logs telegram-bot --tail 20 2>&1 | grep -iE "TypeError|SyntaxError|Traceback" >/dev/null; then
  echo "  [WARN] bot log contains errors. Run:"
  echo "    sudo docker logs -f telegram-bot"
fi

echo ">>> restart whisper-api (it doesn't need a rebuild unless the image changed)"
cd "$WHISPER_DIR"
sudo docker compose up -d --no-deps --force-recreate whisper-api

echo "  [OK] done. Tail logs with:"
echo "    sudo docker logs -f telegram-bot"
echo "    sudo docker logs -f whisper-api"
