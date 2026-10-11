# Webhook vs Polling — How the bot receives updates from Telegram

**Status (2026-10-11):** v0.9.0 ships webhook mode as the default for
production. Polling mode is preserved as a fallback.

This document explains:
1. The two transports the bot supports.
2. Why we chose webhook for production.
3. Step-by-step setup for both modes.
4. Security, fallback, and troubleshooting.

---

## TL;DR

| Mode | Use when | Latency on user action | Setup |
|---|---|---|---|
| **Polling** (default) | You don't have a public domain or HTTPS endpoint, or you're behind a strict NAT/CGNAT, or you're doing development | Up to **25s** (one getUpdates cycle) | Just `WEBHOOK_MODE=0` and run the bot |
| **Webhook** | You have a domain, a wildcard LE cert, and a way to expose port 443 on the public internet | **<1s** (Telegram pushes directly) | DNS A record + DNAT + nginx reverse-proxy + `WEBHOOK_MODE=1` + `setWebhook()` |

If you don't have the infrastructure for webhook, **polling is fine**.
It works, it's stable, and it has been the only mode until v0.9.0.

---

## Why webhook is better

Polling has one fundamental limitation: the bot only learns about a
new message when it next asks Telegram. With `getUpdates` long-polling
at `timeout=25`, the worst-case latency is 25 seconds. The user sends
"hello", waits up to 25 seconds, and sees a reply.

For most conversations this is invisible. But for a few specific
operations, polling is **broken by design**:

- **Edit-during-LLM**: if a user sends a message, the bot starts
  generating, the user edits the message — the bot does NOT see the
  edit until the next getUpdates cycle. By then it's already answered
  the original. With webhook, Telegram pushes the `edited_message`
  event within ~1 second, and the bot can replace its stale answer.
- **Stop button on a long LLM call**: works in both modes (per-handler
  abort_event fires locally), but webhook makes the UX tighter.
- **Future background tasks**: anything that completes asynchronously
  and needs to deliver a follow-up message needs push, not poll.

For everything else, polling is fine. Don't migrate to webhook if
you don't have the infrastructure.

---

## Architecture

### Polling mode (default, WEBHOOK_MODE=0)

```
Telegram servers  ←  long-poll (25s timeout)  ←  bot's httpx client
                          ↓
                  Telegram sends updates
                          ↓
                  bot.dispatch._run() in dispatch.py
                          ↓
                  asyncio.create_task(_dispatch_update)
                          ↓
                  handler.handle_text / handle_photo / ...
```

The bot opens a single `httpx.AsyncClient` to `api.telegram.org/bot<token>/getUpdates`.
Telegram holds the connection open for up to 25 seconds. When a message
arrives, Telegram sends it in the response. The bot dispatches it as
an asyncio task. The connection is then re-opened.

A single connection is intentional: opening multiple connections triggers
Telegram's "duplicate long-poll" detection and 409s.

### Webhook mode (WEBHOOK_MODE=1)

```
Telegram servers  →  POST https://bot.kuduza.com/tg-webhook/<secret>
                          ↓
                  91.210.150.42:443 (your public IP)
                          ↓
                  MikroTik DNAT → 192.168.10.7:443
                          ↓
                  nginx-proxy (TLS termination, reverse-proxy)
                          ↓
                  127.0.0.1:8443 (bot's aiohttp server)
                          ↓
                  webhook._handle() — verify secret, parse update
                          ↓
                  asyncio.create_task(_dispatch_update)
                          ↓
                  handler.handle_text / handle_photo / ...
                          ↓
                  bot's httpx client → api.telegram.org (for LLM, not for updates)
```

Key differences:
- **Telegram initiates** the connection. The bot is purely a server.
- **TLS terminates at your edge** (nginx). Telegram sees a cert signed
  by a trusted CA (Let's Encrypt, in our case).
- **aiohttp on 127.0.0.1:8443** only — the bot is never directly
  exposed. nginx is the only process listening on the public port.
- **Shared secret in URL + header**: `X-Telegram-Bot-Api-Secret-Token`
  header is verified on every request. URL leak doesn't grant access.

---

## Setup: polling mode

If you're starting from scratch or just want the bot to work without
any external infrastructure, polling is the answer.

1. Get a `BOT_TOKEN` from `@BotFather` on Telegram.
2. Configure the bot's environment (`.env` or `docker-compose.yml`):
   ```yaml
   - BOT_TOKEN=...
   - LLAMA_URL=http://<llama-server>:8080/v1
   - MODEL=...
   - ALLOWED_USER_IDS=286293081,117382588
   # WEBHOOK_MODE is omitted or set to 0 (default)
   ```
3. Run the bot:
   ```bash
   docker compose up -d
   ```
4. Send `/start` to the bot. After up to 25 seconds, the bot
   acknowledges you.

That's it. No DNS, no SSL, no nginx.

---

## Setup: webhook mode

You need four things:
1. **A domain** that you control (we use `kuduza.com`).
2. **A wildcard or single-host LE certificate** for that domain.
3. **A public IP with port 443 reachable** (MikroTik or any edge
   router that can DNAT to a host where the bot runs).
4. **nginx** (or any TLS terminator) on the host, configured to
   reverse-proxy to the bot's aiohttp port.

### 4.1 DNS

Add an A record for the subdomain the bot will live at:

```
bot.kuduza.com.   IN   A   <your-public-IP>
```

If you have a `*.kuduza.com` wildcard LE cert, the subdomain is
arbitrary — `bot`, `tg`, `llm`, whatever you like. Pick something
descriptive.

### 4.2 Edge / MikroTik DNAT

Expose TCP 443 on your public IP and forward to the host where
nginx-proxy will run:

```
/ip firewall nat add chain=dstnat dst-address=<public-IP> dst-port=443     protocol=tcp action=dst-nat to-addresses=<nginx-host-internal-IP>     to-ports=443 comment="telegram-bot webhook"
```

If your edge is on a different network than the bot host, the
address `to-addresses` is the internal IP of the bot host (e.g.
`192.168.10.7`).

The same DNAT rule covers both 80 and 443 if you also want a
plain-HTTP-301-to-HTTPS redirect. Otherwise, just 443.

### 4.3 nginx reverse-proxy

The bot's aiohttp server listens on `127.0.0.1:8443` only (inside
the bot's container, but `network_mode: host` in docker-compose.yml
means it's actually the host's 127.0.0.1:8443). nginx terminates
TLS and forwards to it.

`/etc/nginx/conf.d/bot-webhook.conf` (or wherever your nginx reads
its config from):

```nginx
server {
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name bot.kuduza.com;

    # Wildcard cert (or single-host cert covering bot.kuduza.com).
    ssl_certificate     /etc/letsencrypt/live/kuduza.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/kuduza.com/privkey.pem;

    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_prefer_server_ciphers on;
    ssl_ciphers ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384;
    ssl_session_cache shared:SSL:10m;
    ssl_session_timeout 10m;

    # Telegram webhooks send a single POST per update. 100M is
    # overkill (Telegram caps update size at 25 MB) but cheap
    # to allow.
    client_max_body_size 100M;

    location / {
        proxy_pass http://127.0.0.1:8443;

        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        proxy_http_version 1.1;
        proxy_set_header Connection "";

        proxy_read_timeout 60s;
        proxy_send_timeout 60s;
        proxy_connect_timeout 10s;
    }
}
```

Reload nginx: `nginx -s reload` or `docker exec nginx-proxy nginx -s reload`.

### 4.4 Bot configuration

Add two env vars to the bot's `docker-compose.yml`:

```yaml
- WEBHOOK_MODE=1
- WEBHOOK_SECRET_TOKEN=<32-hex-chars>
```

Generate the secret with `openssl rand -hex 16` (32 hex chars). Keep
it secret — anyone with this token can forge updates.

### 4.5 Register the webhook with Telegram

```bash
BOT_TOKEN="..."
SECRET="..."

curl -X POST "https://api.telegram.org/bot${BOT_TOKEN}/setWebhook" \
  -H "Content-Type: application/json" \
  -d "{
    "url": "https://bot.kuduza.com/tg-webhook/${SECRET}",
    "secret_token": "${SECRET}",
    "allowed_updates": ["message", "edited_message", "callback_query", "chat_member"]
  }"
```

The `secret_token` here must match `WEBHOOK_SECRET_TOKEN` in the
bot's env. The URL path can be any random string — the bot's
aiohttp server accepts any path under `/tg-webhook/*`.

### 4.6 Restart and verify

```bash
docker compose down --remove-orphans
docker compose up -d
docker logs --tail 20 telegram-bot
# expect: "[main] webhook mode: aiohttp on http://127.0.0.1:8443/tg-webhook/"
```

Then:
```bash
curl "https://api.telegram.org/bot${BOT_TOKEN}/getWebhookInfo"
# expect: url=bot.kuduza.com, pending_update_count=0, ip_address=<your-public-IP>
```

Send a message to the bot. It should respond in **<1 second**.

---

## Security

Webhook mode has one public endpoint, `POST /tg-webhook/<secret>`, behind
nginx. Threat model and mitigations:

| Threat | Mitigation |
|---|---|
| URL leak → attacker forges updates | `X-Telegram-Bot-Api-Secret-Token` header required on every request. URL alone is useless. |
| L7 DDoS | nginx `client_max_body_size 100M` + timeouts. Add `limit_req_zone` if you see floods. |
| Network scanner probing | Only `/tg-webhook/*` and `/healthz` are exposed. Other paths get 404 from nginx. |
| Compromise of nginx container | nginx has no business logic. Bot's aiohttp server still validates the secret. Defense in depth. |
| Home network exposure | Only port 443 is open. Bot binds 127.0.0.1 only. No path from nginx to other local services. |
| TLS downgrade | `ssl_protocols TLSv1.2 TLSv1.3; ssl_prefer_server_ciphers on;` — only modern protocols. |

The cert is auto-renewed by `certbot` with `dns-rfc2136` (DNS-01
challenge). No port-80 dependency for renewal. As long as the cert
is valid, Telegram accepts the webhook URL.

---

## Fallback to polling

If webhook mode breaks (cert expired, nginx down, edge router
rebooted with wrong DNAT), you can switch back to polling in 30 seconds:

1. Edit `docker-compose.yml`:
   - Either remove `WEBHOOK_MODE` and `WEBHOOK_SECRET_TOKEN`, or
   - Set `WEBHOOK_MODE=0`
2. `docker compose up -d`
3. The bot starts the polling loop. Telegram will start delivering
   updates via `getUpdates` again automatically — no need to call
   `setWebhook` (the old webhook is forgotten as soon as the bot
   stops accepting POSTs).

The polling code is **unchanged** in v0.9.0. It's the same loop that
has been running since v0.5.0, and it has been hardened with 11
hotfixes through v0.7.x and v0.8.x.

---

## Troubleshooting

### "Webhook was set" but bot doesn't respond

Check:
1. `getWebhookInfo` → `last_error_message` and `last_error_date`.
   Telegram tells you why it gave up.
2. `docker logs telegram-bot` → look for
   `[webhook] rejected request: bad or missing X-Telegram-Bot-Api-Secret-Token`
   (means the secret in the header doesn't match the env) or
   `connection refused` (means nginx is down or DNAT is broken).
3. From the bot host, `curl -k https://127.0.0.1:8443/healthz` →
   should return `webhook-ok mode=webhook`. If not, the bot's
   aiohttp server didn't start.
4. `curl -I https://bot.kuduza.com/` from outside → should be
   502 (bot listener has no /healthz) or 200 if you proxied it.
   If it's connection refused, DNAT is broken.

### Bot accepts POSTs but doesn't reply

1. Check `WEBHOOK_SECRET_TOKEN` matches what `setWebhook` registered.
2. Check the bot can reach `api.telegram.org` for the LLM call.
   Webhook mode still uses outbound HTTPS to llama-server.
3. Check Docker `network_mode: host` is set in `docker-compose.yml`.
   Without it, the bot's `127.0.0.1` is container-local, not
   host-local, and nginx can't reach the bot.

### Telegram says "Wrong response from the webhook"

Telegram does a one-time test POST when you call `setWebhook`. If
the bot doesn't return 200 within ~5s, Telegram rejects the webhook.

Common causes:
- Bot didn't start yet (Docker startup race).
- Nginx is returning 502 because the bot isn't listening.
- A reverse proxy in front of nginx strips or rewrites the path.

### Cert renewal fails

If you're using `dns-rfc2136` (DNS-01 challenge), cert renewal is
independent of the bot. Check `/var/log/letsencrypt/letsencrypt.log`.
If you're using HTTP-01, ensure port 80 is reachable from the
internet during renewal (DNAT must allow it).

---

## Why we built it this way (architecture notes)

- **nginx in front of the bot, not aiohttp serving TLS directly.**
  aiohttp can do TLS, but nginx has better TLS config ergonomics
  and is the standard tool. Also, putting nginx in front means the
  bot never touches untrusted input over plain HTTP.
- **127.0.0.1 binding for the aiohttp server, not 0.0.0.0.**
  Defense in depth: even if the Docker network_mode changes or
  another container gets the same IP, the bot is unreachable
  except through nginx on localhost.
- **`secret_token` BOTH in URL and header.** Redundant, but
  Telegram's docs recommend it. The URL secret makes enumeration
  harder; the header secret makes URL leak harmless.
- **Webhook mode and polling mode share `_dispatch_update()`.**
  The transport is the only difference. All the handler logic,
  abort_event handling, semaphores, rate limiting, retry — all of
  it works identically in both modes.
- **`asyncio.create_task` in the webhook handler.** Telegram's
  webhook delivery expects a 200 within 5s. LLM calls take 30s+.
  Fire-and-forget on the dispatch task is the only sane pattern.

---

## Files

- `telegram-bot/webhook.py` — aiohttp server, secret check, handler.
- `telegram-bot/config.py` — `WEBHOOK_MODE` and friends (env vars).
- `telegram-bot/dispatch.py` — `main()` branches on `WEBHOOK_MODE`.
- `telegram-bot/bot.py` — `_selftest()` includes a `webhook boot`
  test (aiohttp TestServer + TestClient) that exercises the
  secret-token check, `GET /healthz`, and `POST /tg-webhook/<x>`.

Production config lives outside the repo:
- `/opt/nginx-proxy/conf.d/bot-webhook.conf` — nginx server block.
- `/etc/letsencrypt/live/kuduza.com/` — wildcard LE cert.
- `/opt/telegram-bot/docker-compose.yml` — `WEBHOOK_MODE=1` and
  `WEBHOOK_SECRET_TOKEN=...` in `environment:`.
- MikroTik `/ip firewall nat` — DNAT rule 10: 91.210.150.42:443
  → 192.168.10.7:443.
- ns1.kuduza.com DNS A record: `bot.kuduza.com → 91.210.150.42`.
