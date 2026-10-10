FROM python:3.11-slim

WORKDIR /app

# Unbuffered stdout/stderr so `docker logs` shows prints in real time even
# if the operator runs the container directly without docker-compose.
ENV PYTHONUNBUFFERED=1

# Copy requirements.txt first for better Docker layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Create an unprivileged user for the bot process. Combined with
# `network_mode: host` in docker-compose.yml, a RCE in python-telegram-bot
# or httpx would otherwise drop the attacker to root on the host.
#
# /app/data is the bind-mount point for the SQLite conversation store
# (see docker-compose.yml). bot.py creates the file on first run
# with default mode 0644 owned by `bot` via umask; the bind-mount
# gives the operator a stable path on the host.
RUN useradd --create-home --uid 1000 --shell /bin/false bot \
    && mkdir -p /app/files /app/data \
    && chown -R bot:bot /app

# Then copy ALL bot modules (F3 module split, v0.6.0+).
# Without this wildcard, `docker compose build` produces
# a container missing config.py / state.py / dispatch.py /
# handlers.py / call_llama.py / etc., and bot.py fails to
# import at startup. The COPY *.py glob is evaluated by the
# Docker builder at build time, so the image is reproducible.
COPY *.py ./

# Drop privileges. Anything that needs to write here must be writable
# by uid 1000, which is fine because we only write to /app/files
# and /app/data.
USER bot

CMD ["python", "bot.py"]
