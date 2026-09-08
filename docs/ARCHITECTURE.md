# Architecture

## bot.py — что внутри

### Tool-calling loop

Бот **сам** делает loop вызовов tool'ов (до 5 итераций). Это потому что
llama-server с `--mcp-servers-config` подгружает **только список tools**,
но **исполняет** их не он, а клиент (мы).

Цикл:

```
1. Получить tools с GET /tools  (один раз, кешируется)
2. POST /v1/chat/completions с messages + tools
3. Если в ответе finish_reason == "tool_calls":
     a. Для каждого tool_call:
        - распарсить name + arguments
        - исполнить (HTTP к SearXNG, wttr.in, и т.д.)
        - добавить в messages: {role: "tool", tool_call_id, content}
     b. goto 2
4. Вернуть content как ответ
```

### Какие tools умеет бот исполнять

| Tool | Реализация | Когда доступен |
|---|---|---|
| `get_weather` | HTTP GET `https://wttr.in/{location}?format=j1&lang=ru` | Всегда (custom, добавлен в код) |
| `searxng_search` | HTTP GET `http://SEARXNG_URL/search?q=...&format=json` | Если поднят SearXNG |
| `searxng_fetch_url` | HTTP GET к URL + HTML strip | Всегда (если интернет есть) |
| `searxng_engines` | HTTP GET `http://SEARXNG_URL/engines` | Если поднят SearXNG |
| `read_file`, `write_file`, `edit_file`, `exec_shell_command` | — | **ОТКЛЮЧЕНЫ** в `DISABLED_TOOLS` |
| `playwright_browser_*` | — | **ОТКЛЮЧЕНЫ** в `DISABLED_TOOLS` (нужен Playwright MCP) |
| `file_glob_search`, `grep_search`, `get_info` | — | **ОТКЛЮЧЕНЫ** (security + не реализовано) |

### IPv4 monkey-patch

```python
socket.getaddrinfo = _ipv4_only_getaddrinfo
```

**Зачем:** docker-контейнер без IPv6 маршрутизации, а `api.telegram.org`
резолвится в IPv6 (AAAA) первым. `httpx`-based клиенты (Telegram Bot API,
SearXNG) падают с `Network is unreachable` на исходящих. Monkey-patch
фильтрует IPv6 из результатов `getaddrinfo`, оставляя только IPv4.

**Когда убирать:** как только хост получит IPv6 маршрутизацию, или
когда upstream починит A-record для `api.telegram.org`.

### Whitelist

```python
ALLOWED_USER_IDS = {int(x) for x in ENV.split(',') if x.strip().isdigit()}
ALLOWED_USERNAMES = {x.lstrip('@').lower() for x in ENV.split(',') if x.strip()}

if not ALLOWED_USER_IDS and not ALLOWED_USERNAMES:
    LOCKDOWN = True
```

Каждый handler начинается с:
```python
if await reject_if_unauthorized(update, context):
    return
```

`reject_if_unauthorized`:
- Если `LOCKDOWN` → отказ
- Если `user_id` в `ALLOWED_USER_IDS` → пропуск
- Если `username.lower()` в `ALLOWED_USERNAMES` → пропуск
- Иначе → лог `[SECURITY] rejected id=...` + `return True` (handler выходит молча)

**Молчаливый отказ** (без `reply_text`) — чтобы посторонний не узнал,
что бот вообще существует. Если бот ответит «access denied», это подтвердит
жизнь бота.

## whisper-api

Использует образ `fedirz/faster-whisper-server:latest-cpu` — это обёртка
вокруг `faster-whisper` от SYSTRAN, OpenAI-совместимый API.

Дефолтная модель `Systran/faster-distil-whisper-large-v3` (1.5 GB) —
хороший баланс скорость/качество для русского. Модель скачивается при
первом запуске в `/opt/whisper-api/cache/`.

Если нужна лучшая точность — замените `WHISPER_MODEL` в `.env` на
`Systran/faster-whisper-large-v3` (~3 GB, медленнее).

## Сетевая модель

`network_mode: "host"` в `telegram-bot/docker-compose.yml`.

**Плюсы:**
- Бот видит `localhost:8080` (llama), `localhost:8000` (whisper), `localhost:8888` (searxng)
  без дополнительных DNS и link-ов
- Проще отлаживать (`netstat`, `ss` с хоста = то, что видит бот)

**Минусы:**
- Контейнер не изолирован от сети хоста
- Порт, который бот слушает (если бы слушал), занял бы хост-порт
- Нельзя запустить два экземпляра бота с одним `BOT_TOKEN`

Для production-ready сетапа стоит перейти на bridge-сеть + явные
internal DNS-имена (`http://llama:8080/v1`). Сейчас это overkill для
standalone-машины.
