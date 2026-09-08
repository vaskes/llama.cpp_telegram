# Troubleshooting

## Бот молчит / отвечает только на `/start`

### 1. Проверьте whitelist
```bash
sudo docker logs telegram-bot --tail 200 | grep "whitelist\|LOCKDOWN\|rejected"
```

Если `LOCKDOWN` — у вас пустые `ALLOWED_USER_IDS` и `ALLOWED_USERNAMES`.
Откройте `/opt/telegram-bot/.env` и добавьте свой user_id.

### 2. Проверьте llama-server
```bash
curl -s -m 5 http://localhost:8080/health
# → {"status":"ok"}
```

Если не отвечает — бот **не виноват**. Запускайте llama-server (см.
[vaskes/llama.cpp-rocm-780m](https://github.com/vaskes/llama.cpp-rocm-780m)).

## `httpx.ConnectError` в логах

Типовые причины:

### a) llama-server не запущен
См. выше.

### b) IPv6 vs IPv4 (редко, но бывает в docker)
В `bot.py` есть monkey-patch, фильтрующий IPv6. Если вы видите эту
ошибку после правки — проверьте, что monkey-patch **не** удалён:

```python
import socket
_orig_getaddrinfo = socket.getaddrinfo
def _ipv4_only_getaddrinfo(host, *args, **kwargs):
    results = _orig_getaddrinfo(host, *args, **kwargs)
    family = kwargs.get('family', socket.AF_UNSPEC)
    if family == socket.AF_UNSPEC:
        return [r for r in results if r[0] == socket.AF_INET] or results
    return results
socket.getaddrinfo = _ipv4_only_getaddrinfo
```

### c) Bot token отозван / невалидный
`@BotFather` → проверьте токен.

## Бот пишет `❌ Ошибка: ...`

```bash
sudo docker logs telegram-bot --tail 50
```

Типичные ошибки:

- `Connection refused` к `localhost:8080` — llama-server не на том порту
  или вообще не запущен
- `name resolution failed` — DNS в контейнере, пропишите `network_mode: host`
  (по дефолту так) или добавьте `dns: [8.8.8.8]` в compose
- `Model not found` — модель в `MODEL` env не совпадает с `--alias` на сервере

## SearXNG возвращает 0 результатов

Это **известная проблема** на cloud IP — `DuckDuckGo`, `Brave`, `Startpage`
отдают CAPTCHA. На localhost (домашний IP) работает.

Workaround:
- Используйте только `get_weather` для вопросов про погоду
- Используйте `searxng_engines` чтобы увидеть список доступных
- Для русского поиска попробуйте `searxng_fetch_url` к конкретному сайту
  (например, к `https://www.google.com/search?q=...`)

## Whisper не распознаёт голосовое

```bash
sudo docker logs whisper-api --tail 20
```

- Модель ещё скачивается — первый запуск 1.5 GB, ~5-10 мин
- Голосовое слишком тихое / шумное — faster-whisper справляется плохо
- Язык — если `language: 'ru'` в `bot.py` стоит, а голосовое на английском,
  распознавание будет кривое. Поменяйте на `language: 'auto'` (если
  upstream поддерживает) или уберите `language` совсем

## `docker compose build` падает с ошибкой pip

```bash
# Очистить кеш
sudo docker builder prune

# Или явно подтянуть свежий python:3.11-slim
sudo docker pull python:3.11-slim
```

## Контейнер не стартует / уходит в ребут

```bash
sudo docker logs telegram-bot --tail 100
```

Если видите `Restarting` loop — скорее всего, невалидный `BOT_TOKEN`
или llama-server недоступен **во время инициализации** (бот делает
`get_me()` при старте).

## Сеть между контейнерами не работает

Убедитесь, что `network_mode: "host"` в `docker-compose.yml`. Без него
контейнер изолирован, и `localhost:8080` ≠ хостовый `localhost:8080`.

## Системные лимиты

Если бот перестаёт отвечать после N сообщений:

```bash
ulimit -n          # fd лимит
df -h /opt         # диск
free -h            # память
```

llama-server с большой моделью и 8B контекстом может съесть всю RAM.
Следите за `MEM%` в `docker stats`.
