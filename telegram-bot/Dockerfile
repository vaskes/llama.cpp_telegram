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
RUN useradd --create-home --uid 1000 --shell /bin/false bot \
    && mkdir -p /app/files \
    && chown -R bot:bot /app

# Then copy bot.py
COPY bot.py .

# Drop privileges. Anything that needs to write here must be writable
# by uid 1000, which is fine because we only write to /app/files.
USER bot

CMD ["python", "bot.py"]
