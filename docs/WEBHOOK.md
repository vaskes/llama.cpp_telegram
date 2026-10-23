# Webhook vs Polling — How the bot receives updates from Telegram

The bot supports two transports for receiving Telegram updates:
**long-polling** (default, no infrastructure required) and
**webhook** (sub-second latency, requires a public endpoint with
TLS). This document explains when to use each, how to set them up,
security trade-offs, and fallback.

---

## TL;DR

| Mode | Use when | Latency on user action | Setup |
|---|---|---|---|
| **Polling** (default) | You don't have a public domain, no HTTPS endpoint, behind a strict NAT/CGNAT, or doing development | Up to **25s** (one `getUpdates` cycle) | Just set `WEBHOOK_MODE=0` and run the bot |
| **Webhook** | You have a domain, a TLS certificate, and a way to expose port 443 on the public internet | **<1s** (Telegram pushes directly) | DNS A record + reverse proxy + `WEBHOOK_MODE=1` + `setWebhook()` |

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
  edit until the next `getUpdates` cycle. By then it's already answered
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

### Polling mode (default, `WEBHOOK_MODE=0`)

```
Telegram servers  ←  long-poll (25s timeout)  ←  bot's httpx client
                          ↓
                  Telegram sends updates
                          ↓
                  bot's dispatch loop
                          ↓
                  asyncio.create_task(_dispatch_update)
                          ↓
                  handler.handle_text / handle_photo / ...
```

The bot opens a single `httpx.AsyncClient` to
`api.telegram.org/bot<token>/getUpdates`. Telegram holds the connection
open for up to 25 seconds. When a message arrives, Telegram sends it in
the response. The bot dispatches it as an asyncio task. The connection
is then re-opened.

A single connection is intentional: opening multiple connections
triggers Telegram's "duplicate long-poll" detection and 409s.

### Webhook mode (`WEBHOOK_MODE=1`)

```
Telegram servers  →  POST https://<your-domain>/tg-webhook/
                     (secret in the X-Telegram-Bot-Api-Secret-Token header)
                          ↓
                  <your-public-IP>:443 (edge router)
                          ↓
                  reverse proxy (TLS termination)
                          ↓
                  127.0.0.1:<webhook-port> (bot's aiohttp server)
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
- **TLS terminates at your edge** (nginx, Caddy, Traefik, etc.). Telegram
  sees a cert signed by a trusted CA (Let's Encrypt is the standard).
- **aiohttp on WEBHOOK_HOST:WEBHOOK_PORT** (default `0.0.0.0:8443`) —
  the bot binds to the loopback when the reverse proxy is on the
  same host, or to the host LAN IP when the proxy is on a different
  host. With `network_mode: host` (the current setup since
  2026-10-13), the bot has no container IP; it just listens on the
  host's interfaces. The reverse proxy is the only process that
  should listen on the public port. Set `WEBHOOK_HOST=127.0.0.1`
  for the single-host dev case, or the host LAN IP for the
  cross-host case.
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
   - ALLOWED_USER_IDS=...
   # WEBHOOK_MODE is omitted or set to 0 (default)
   ```
3. Run the bot:
   ```bash
   docker compose up -d
   ```
4. Send `/start` to the bot. After up to 25 seconds, the bot
   acknowledges you.

That's it. No DNS, no TLS, no reverse proxy.

---

## Setup: webhook mode

You need four things:
1. **A domain** that you control.
2. **A TLS certificate** for that domain (single-host or wildcard).
3. **A public IP with port 443 reachable** (an edge router, cloud VM,
   or a tunnel like Cloudflare Tunnel).
4. **A reverse proxy** on the host where the bot runs, configured to
   forward to the bot's aiohttp port.

### 4.1 DNS

Add an A record for the subdomain the bot will live at:

```
<your-subdomain>.<your-domain>.   IN   A   <your-public-IP>
```

If you have a wildcard cert (e.g. `*.<your-domain>`), the subdomain is
arbitrary. Pick something descriptive.

### 4.2 Edge / router DNAT

Expose TCP 443 on your public IP and forward to the host where the
reverse proxy will run. The exact command depends on your router.

Example for a generic edge router:
```
# dst-NAT: public-IP:443 -> reverse-proxy-host:443
```

If your edge is on a different network than the bot host, use the
internal IP of the bot host as the DNAT target.

The same DNAT rule covers both 80 and 443 if you also want a
plain-HTTP-301-to-HTTPS redirect. Otherwise, just 443.

### 4.3 Reverse proxy

The bot's aiohttp server listens on `WEBHOOK_HOST:WEBHOOK_PORT`
(default `0.0.0.0:8443`). For single-host dev, set
`WEBHOOK_HOST=127.0.0.1` and have the proxy talk to
`http://127.0.0.1:8443`. For macvlan (production), the bot
gets a stable container IP and the proxy talks to that.

Example nginx config (replace `<your-domain>` and adjust paths):

```nginx
# 1. Drop catch-all for unknown hosts (close the door on cert probing)
server {
    listen 80 default_server;
    server_name _;
    return 444;
}
server {
    listen 443 ssl http2 default_server;
    server_name _;
    ssl_certificate     /etc/letsencrypt/live/<your-domain>/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/<your-domain>/privkey.pem;
    return 444;
}

# 2. <your-subdomain>.<your-domain> — Telegram webhook endpoint
server {
    listen 443 ssl http2;
    server_name <your-subdomain>.<your-domain>;

    ssl_certificate     /etc/letsencrypt/live/<your-domain>/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/<your-domain>/privkey.pem;

    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_prefer_server_ciphers on;
    ssl_ciphers ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384;
    ssl_session_cache shared:SSL:10m;
    ssl_session_timeout 10m;

    # Webhook payload can be 25MB+ (vision messages). Match the bot's
    # download limit so nginx doesn't truncate.
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

Reload nginx: `nginx -s reload` or `docker exec <nginx> nginx -s reload`.

### 4.3.1 Network layout (since 2026-10-13)

**Current layout:** the bot and whisper-api both run with
`network_mode: host`. No Docker bridge network, no static
container IPs, no macvlan. The bot binds to `0.0.0.0:8443`
on the host. The reverse proxy (nginx, also on host) proxies
HTTPS 443 -> aiohttp 8443 via `127.0.0.1` (or via the
host's LAN IP if the proxy is on a different host).

**Why we dropped macvlan:** macvlan L2 isolation made the
bot unable to reach services on the same host (whisper, the
sandbox side-car, and even llama-server when bound to the
host main IP) without elaborate PBR/DNAT routing on the
router. The macvlan sub-interface cannot ARP the parent
host, so any service on the host main IP was unreachable
from the bot container. The fix was either:
  - run every service in its own macvlan container with
    a static IP, OR
  - switch to `network_mode: host`.

We picked host networking because:
  1. The bot already needs to be reachable from the reverse
     proxy, and putting it on the host network is the
     simplest way to expose it.
  2. The bot is a single-tenant tool; the network isolation
     that macvlan would give us is not worth the
     configuration complexity.
  3. All the dependent services (llama, whisper, donsetch,
     bash-sandbox) are already on the host LAN and reachable
     from any process bound to 0.0.0.0.

**For your reverse proxy:** point it at `127.0.0.1:8443`
if it runs on the same host, or at `<host-lan-ip>:8443` if
it runs on a different host on the same LAN. Both work
without any special routing.

**Historical (pre-2026-10-13):** before the host-network
switch, the bot was on a `lan_macvlan` Docker network with
a static IP (192.168.10.5). The reverse proxy reached it at
that IP. This worked when all other services were also on
the same macvlan, but broke the moment any service moved
to the host. See git history if you need to roll back; do
not start fresh with macvlan.

### 4.4 Bot configuration

Add three env vars to the bot's `.env` (see `telegram-bot/.env.example`):

```env
WEBHOOK_MODE=1
WEBHOOK_SECRET_TOKEN=<32+ random hex chars>
WEBHOOK_URL=https://<your-subdomain>.<your-domain>/tg-webhook
```

`WEBHOOK_URL` (v0.9.2) is the public URL Telegram should POST to.
If set, the bot auto-registers the webhook with Telegram on
startup via `bot.setWebhook(...)`. If empty, register manually
in step 4.5 below.

Generate a secret token with: `openssl rand -hex 16`

Generate the secret with `openssl rand -hex 16` (32 hex chars). Keep
it secret — anyone with this token can forge updates.

### Rotating the bot token kills the webhook until it is re-registered

The webhook is registered **against a token**. When the token is
revoked (`@BotFather /revoke`, which is what a leak response looks
like), Telegram drops that webhook: `getWebhookInfo` answers with an
empty `url`, `pending_update_count` stays 0, and the bot sits there
answering `/healthz` 200 while no update ever arrives.

Nothing in the bot's logs says so -- the process starts, binds the port,
and looks healthy. The check that catches it is one call:

```bash
curl -s "https://api.telegram.org/bot<new-token>/getWebhookInfo" | jq .result.url
```

An empty string means the webhook is gone.

Two ways to not be caught by it:

- **set `WEBHOOK_URL`** in `.env` (with `WEBHOOK_MODE=1` and
  `WEBHOOK_SECRET_TOKEN`). The bot then registers the webhook itself on
  every start, so a rotation is followed by a restart and nothing else;
- **re-register by hand** after the rotation, with the `setWebhook`
  call above.

`WEBHOOK_URL` is the one that survives the next incident, so it is the
one to set.

### 4.5 Register the webhook with Telegram

```bash
curl -s -X POST \
  "https://api.telegram.org/bot<bot-token>/setWebhook" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://<your-subdomain>.<your-domain>/tg-webhook/",
    "secret_token": "<your-webhook-secret-token>",
    "allowed_updates": ["message", "edited_message", "callback_query", "chat_member"],
    "drop_pending_updates": false
  }' | jq
```

The `allowed_updates` parameter is **mandatory**. Without it,
Telegram defaults to `["message", "edited_message"]` and
silently drops `callback_query` updates - which is what the
Stop button sends. With `allowed_updates` missing, the Stop
button appears in the UI but clicks are lost; the LLM runs to
completion ignoring the user intent.

If `WEBHOOK_URL` is set in your `.env`, the bot auto-registers
on startup. Skip this step. If you left it empty (e.g., for a
multi-bot shared webhook), register manually:

```bash
BOT_TOKEN="..."
SECRET="..."

curl -X POST "https://api.telegram.org/bot${BOT_TOKEN}/setWebhook" \
  -H "Content-Type: application/json" \
  -d "{
    \"url\": \"https://<your-subdomain>.<your-domain>/tg-webhook/\",
    \"secret_token\": \"${SECRET}\",
    \"allowed_updates\": [\"message\", \"edited_message\", \"callback_query\", \"chat_member\"]
  }"
```

The `secret_token` here must match `WEBHOOK_SECRET_TOKEN` in the
bot's env. The URL path can be any random string — the bot's aiohttp
server accepts any path under `/tg-webhook/*`.

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
# expect: url=<your-subdomain>.<your-domain>, pending_update_count=0,
#          ip_address=<your-public-IP>
```

Send a message to the bot. It should respond in **<1 second**.

---

## Security

Webhook mode has one public endpoint, `POST /tg-webhook/`,
behind a reverse proxy. Threat model and mitigations:

| Threat | Mitigation |
|---|---|
| URL leak → attacker forges updates | `X-Telegram-Bot-Api-Secret-Token` header required on every request. URL alone is useless. |
| L7 DDoS | Reverse proxy `client_max_body_size 100M` + timeouts. Add `limit_req_zone` if you see floods. |
| Network scanner probing | Only `/tg-webhook/` and `/healthz` are exposed. Other paths get 404 from the reverse proxy. |
| Compromise of reverse proxy container | The proxy has no business logic. Bot's aiohttp server still validates the secret. Defense in depth. |
| Home network exposure | Only port 443 is open. Bot binds 127.0.0.1 only. No path from reverse proxy to other local services. |
| TLS downgrade | `ssl_protocols TLSv1.2 TLSv1.3; ssl_prefer_server_ciphers on;` — only modern protocols. |

The cert is auto-renewed by `certbot` with `dns-<challenge>` or
`http-01` challenge. As long as the cert is valid, Telegram accepts
the webhook URL.

---

## Fallback to polling

If webhook mode breaks (cert expired, reverse proxy down, edge
router rebooted with wrong DNAT), you can switch back to polling in
30 seconds:

1. Edit `docker-compose.yml`:
   - Either remove `WEBHOOK_MODE` and `WEBHOOK_SECRET_TOKEN`, or
   - Set `WEBHOOK_MODE=0`
2. `docker compose up -d`
3. The bot starts the polling loop. Telegram will start delivering
   updates via `getUpdates` again automatically — no need to call
   `setWebhook` (the old webhook is forgotten as soon as the bot
   stops accepting POSTs).

The polling code is **unchanged** from the original. It's the same
loop that has been running for years, hardened with multiple rounds
of hotfixes.

---

## Troubleshooting

### "Webhook was set" but bot doesn't respond

Check:
1. `getWebhookInfo` → `last_error_message` and `last_error_date`.
   Telegram tells you why it gave up.
2. `docker logs telegram-bot` → look for
   `[webhook] rejected request: bad or missing X-Telegram-Bot-Api-Secret-Token`
   (means the secret in the header doesn't match the env) or
   `connection refused` (means the reverse proxy is down or DNAT is
   broken).
3. From the bot host, `curl -k https://127.0.0.1:8443/healthz` →
   should return `webhook-ok mode=webhook`. If not, the bot's
   aiohttp server didn't start.
4. `curl -I https://<your-subdomain>.<your-domain>/` from outside →
   should be 502 (bot listener has no /healthz) or 200 if you
   proxied it. If it's connection refused, DNAT is broken.

### Bot accepts POSTs but doesn't reply

1. Check `WEBHOOK_SECRET_TOKEN` matches what `setWebhook` registered.
2. Check the bot can reach `api.telegram.org` for the LLM call.
   Webhook mode still uses outbound HTTPS to llama-server.
3. Check Docker `network_mode: host` is set in `docker-compose.yml`.
   Without it, the bot's `127.0.0.1` is container-local, not
   host-local, and the reverse proxy can't reach the bot.

### Telegram says "Wrong response from the webhook"

Telegram does a one-time test POST when you call `setWebhook`. If
the bot doesn't return 200 within ~5s, Telegram rejects the webhook.

Common causes:
- Bot didn't start yet (Docker startup race).
- Reverse proxy is returning 502 because the bot isn't listening.
- A reverse proxy in front of nginx strips or rewrites the path.

### Cert renewal fails

If you're using `dns-01` (recommended for wildcard certs), cert
renewal is independent of the bot. Check your certbot logs.
If you're using `http-01`, ensure port 80 is reachable from the
internet during renewal (DNAT must allow it).

### Asymmetric routing (foreign ingress)

If your edge router sends foreign traffic (e.g. Telegram's servers
in Amsterdam) through a VPN tunnel, the response packets may leave
through a different interface than they came in on. This creates
asymmetric routing. TCP connections are matched by 4-tuple, so
they generally survive — but PBR (policy-based routing) on the
edge router may be required to ensure responses go back the same
way they came in.

If you see `Connection timed out` from Telegram's side, the issue
is likely at the network path before the bot, not in the bot
itself. Check your edge router's routing policy.

---

## Why we built it this way (architecture notes)

- **Reverse proxy in front of the bot, not aiohttp serving TLS
  directly.** aiohttp can do TLS, but nginx/Caddy/Traefik have
  better TLS config ergonomics and are the standard tools. Also,
  putting the reverse proxy in front means the bot never touches
  untrusted input over plain HTTP.
- **127.0.0.1 binding for the aiohttp server, not 0.0.0.0.** Defense
  in depth: even if the Docker network_mode changes or another
  container gets the same IP, the bot is unreachable except
  through the reverse proxy on localhost.
- **`secret_token` BOTH in URL and header.** Redundant, but
  Telegram's docs recommend it. The URL secret makes enumeration
  harder; the header secret makes URL leak harmless.
- **Webhook mode and polling mode share `_dispatch_update()`.** The
  transport is the only difference. All the handler logic, abort_event
  handling, semaphores, rate limiting, retry — all of it works
  identically in both modes.
- **`asyncio.create_task` in the webhook handler.** Telegram's
  webhook delivery expects a 200 within 5s. LLM calls take 30s+.
  Fire-and-forget on the dispatch task is the only sane pattern.

---

## Files

In the repository:
- `telegram-bot/webhook.py` — aiohttp server, secret check, handler.
- `telegram-bot/config.py` — `WEBHOOK_MODE` and friends (env vars).
- `telegram-bot/dispatch.py` — `main()` branches on `WEBHOOK_MODE`.
- `telegram-bot/bot.py` — `_selftest()` includes a `webhook boot`
  test (aiohttp TestServer + TestClient) that exercises the
  secret-token check, `GET /healthz`, and `POST /tg-webhook/`.

Outside the repo (operator-specific):
- Reverse proxy server block (e.g. `/etc/nginx/conf.d/`).
- TLS cert (Let's Encrypt or otherwise).
- Edge router DNAT rule.
- DNS A record.

---

## See also

- [docs/SETUP.md](SETUP.md) — installing on a fresh host
- [docs/SECURITY.md](SECURITY.md) — bot-level security (whitelist, etc.)
- [docs/TROUBLESHOOTING.md](TROUBLESHOOTING.md) — common issues
