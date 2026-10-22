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
    && mkdir -p /app/files /app/data /app/personal_tools \
    && chown -R bot:bot /app

# Copy ALL top-level bot modules (F3 module split, v0.6.0+).
# Without this wildcard, `docker compose build` produces
# a container missing config.py / state.py / dispatch.py /
# handlers.py / call_llama.py / etc., and bot.py fails to
# import at startup. The COPY *.py glob is evaluated by the
# Docker builder at build time, so the image is reproducible.
COPY *.py ./

# Copy the personal_tools subpackage (T38: bash_command tool for
# LLAMABOT_DEPLOYMENT_PROFILE=personal). This is a SEPARATE COPY
# because the `COPY *.py ./` glob above only matches top-level
# files; without this line, `import personal_tools.bash_command`
# raises ModuleNotFoundError at startup. The bind-mount in
# docker-compose.yml is a redundant safety net (so editing the
# module on the host does not require an image rebuild) but the
# image must still self-contain the files for cold builds.
COPY personal_tools/ ./personal_tools/

# prompt_text holds the prompt prose (TECH-DEBT #12). Same reason, same
# failure: `COPY *.py ./` matches top-level files only, so moving the
# sentences into a package without this line produces an image whose
# bot.py dies on `from prompts import` with ModuleNotFoundError at
# startup. The suite did not catch it -- the suite runs against the host
# tree, and the host tree has the package. A package that is not in the
# image is not in the product.
COPY prompt_text/ ./prompt_text/

# Drop privileges. Anything that needs to write here must be writable
# by uid 1000, which is fine because we only write to /app/files
# and /app/data.
USER bot

CMD ["python", "bot.py"]
