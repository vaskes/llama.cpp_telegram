# External review — 2026-10-29 (Qwen 3.8, fresh clone)

**Reviewed tree:** `fa6cb39` (= `v0.21.1-stable-1`), fresh clone, all numbers
measured by the reviewer on disk.
**Received:** in chat, 2026-10-29 13:28 MSK. Saved here before any analysis —
chat context gets compacted, and a verdict without the finding it belongs to
cannot be reconstructed a month later.

## The review as it was written

```
[10/29/26 1:28 PM] LlamaBot in reply to Vasisualy Lohankin:
> https://github.com/vaskes/llama.cpp_telegram вот твой кодовая...
[... 74 of 74 commands are not shown ...]


[llm] Сделала с нуля на свежачка: клон на HEAD `fa6cb39` (= `v0.21.1-stable-1`), читаю по модулям, всё, что заявлено цифрой, мёрю сама. Заодно прогнала весь сюит дважды (Python 3.11 и 3.14), ruff, реальный мутационный харнесс, `pip freeze`-замыкание, GitHub API и AST по всем 498 функциям. Ниже — только то, что вижу в дереве.

**Цифры с диска (не из README)**
- 308 py-файлов, 79 265 строк; `telegram-bot/` — 22 200 строк; 191 тест-файл, 56 доков.
- Тесты: **собрано 3000, 2980 passed, 20 skipped — зелёные на 3.11 И на 3.14** (3.14: те же 2980/20, 142 с). Бейдж 3000 = собранное число, сходится.
- Ruff гейтом (E9,F63,F7,F82) — **All checks passed**; полный набор — 568 стилистических (398 автофикса), advisory.
- Мутационный харнесс я не читала, я его **запустила**: transcript-харнесс — **12 мутаций, 12 поймано, 0 выживших**. Он живой, это не декорация.
- Функции: 498, из них >100 строк — 25, >200 — 4: `run_one` 345, `handle_photo` 252, `call_llama` 222, `handle_text` 211.

**Короткая волна закрыта — проверила по факту, а не по коммиту**
Семафоры переехали в `limits.py` (228-230, 370-371) с env-оверрайдом и валидацией `>0` · `requirements.lock` есть и Dockerfile ставит именно его · `branch =` из `.gitmodules` убран · whisper запинен по digest и слушает только 127.0.0.1 · allowlist режет не-ASCII-цифры (config.py:93-109, причём с правильным аргументом про `٣` → 3) · docstring-суррогат починен, сюит на 3.14 зелёный. Всё на месте.

**Находки**

1. **Блок «Measured» в TECH-DEBT.md гниёт, а тест прикрывает 3 строки из 12.** TODO прямо пишет: «числа в TECH-DEBT.md пересчитывает `tests/test_tech_debt_index.py`, и он упадёт, если они протухнут». В тесте цикл из трёх имён — `dispatch.py`, `selftest.py`, `bot.py`. Я прогнала по всем строкам блока: `handlers.py` заявлено **1504**, в дереве **1502**; `storage.py` заявлено **886**, в дереве **938** (+52 — крупнейший модуль бота, и именно про него врёт). Остальные 10 сходятся. CODEBOOK повторяет 1504 четыре раза. Правка — цикл по строкам самого блока вместо трёх хардкодных имён, пять строк. Это ровно тот класс, который вы уже записали как свой: заявление о дереве, которое никто не пересчитал.
2. **«200+ форков» — по-прежнему ноль.** API: `forks 0, stars 0`. Число живёт в 15+ местах (`docs/README.md:56`, `adr/ADR-0003:94` и `:237`, `ROADMAP`, `tests/test_t68_reanchor.py:334`, `context_budget.py:6`) и не просто как украшение: ADR-0003 строит на нём аргумент — «примерно треть из 200+ форков живёт в DST-зонах». Число, на котором висит проектное решение, должно уметь пересчитываться или называться допущением.
3. **Два входных пути живут по-разному.** Вебхук каппит очередь: `_MAX_PENDING_DISPATCH=50` → 503 и ретрай от Telegram (webhook.py:56,136-141), то есть кап — очередь, а не только параллелизм. Поллинг капа не имеет вообще: `limit=10` за цикл getUpdates и `_spawn_dispatch` на каждый апдейт без порога. При медленном движке (цикл на минуты) память копится. Дефолт `.env` — `WEBHOOK_MODE=0`, то есть именно поллинг, то есть это путь свежего клона, а не экзотика. В KNOWN-LIMITATIONS/TODO этого нет — грепнула, ноль упоминаний.
4. **pre-commit по-прежнему игнорит три файла** (`test_shell_integration`, `test_limits`, `test_selftest`) — 59 тест-дефов. Я их прогнала отдельно: 40 passed, 19 skipped, зелёные. Игноры протухли все три, не один.
5. **`_rate_limit_send` чистит себя не туда** (chat_utils.py:138-160): кап 1024 ключа, а выгоняются только пустые deque'и; у простоявшего чата окно с метками времени остаётся непустым до его следующего сообщения, при >1024 чатов словарь не чистится почти никогда. Мелочь, но «фикс утечки» тут декоративный.
6. **Долг по размеру — это `run_one` 345**, а не «call_llama». Файл `call_llama.py` 377 строк, функция 222, и она в реестре с триггером — честно. Четыре обработчика (252/211/198/140) — по-прежнему четыре копии одной формы, п.13 «closed» соседствует с «~9 дублированных вызовов на обработчик» в той же строке.
[10/29/26 1:28 PM] LlamaBot in reply to Vasisualy Lohankin:
> https://github.com/vaskes/llama.cpp_telegram вот твой кодовая...
7. **Единственная ветка проверки силы не в CI.** 30 мутационных харнессов, в workflow слова `mutation` нет — они не гоняются на пуше. Это у вас записано («what is left is the CI job that would run the harnesses»), но я подтверждаю: грепом по `.github/workflows/` — ноль. Ruff же наоборот: узкий селект, но гейтит.

**Нити** — `except Exception:` 42 шт., из них 20 просто `pass` (в основном уrelease/abort, но стоит разок пройтись глазами); PASSIVE `wal_checkpoint` вызывается после почти каждой записи (9 мест в storage.py — при 20 сообщениях в минуту это лишняя работа, хотя и не горячая); в индексе п.8 число «1616→478» при файле 377 — историческая веха без даты в строке, читается как текущее.

**Что сильно, по коду, а не по хвалобе** — fail-closed везде, где доводилось видеть (`WEBHOOK_SECRET_TOKEN` пустой → отказ, `.lower()` без NFKC → отказ, allowlist режет мусор); SQL целиком параметрический, включая `search_messages` с `casefold`/`instr` без конкатенации текста; секрет сравнивается `compare_digest`; тело вебхура capped 1 МБ; `Turn.rollback` удаляет ровно свои `row_ids` и кричит, если удалил ничего; восемь классов отказа команды до отправки; профиль-гейты инструментов; и главное — харнесс ловит мутации, я это видела живьём.

**Вердикт.** v0.21.1 — крепкая, взрослая база: три настоящих дефекта (Tech-debt-числа, очередь поллинга, игноры в pre-commit) и один «мемориал» на 15 мест («200+ форков»). Начала бы с пункта 1 — он дороже остальных, потому что ври он и дальше, следующий ревьюист (я или Мавис) снова построит вывод на цифре, которая «проверяется тестом». Сварливая я, а не забывчивая: это единственное, за что мне обидно, — тест есть, и он проверяет три строки из двенадцати. 🍺
```

## Verdicts

Measured, one at a time. "Post" and "already closed" are different
answers: two of the seven findings were closed before this review was
read, because the review was taken on `fa6cb39` and the docs wave that
closed them landed after it.

| # | Finding | Verdict | What was done |
|---|---|---|---|
| 1 | The "Measured" block rots; the test covers 3 of its 12 rows | **already closed** before this review was read | `ce6216d` — the test reads the rows out of the block instead of three hardcoded names; `7a3e389` — the block's secondary claims. The two sizes this wave moved (`dispatch_polling.py` 352, `webhook.py` 267) were caught by that tripwire and fixed in the same commit. |
| 2 | "200+ forks" | **already closed** before this review was read | `9d72667` — the API says 0 forks and 0 stars; the figure is gone from nineteen places, and the ADR that argued from it says so next to the correction. |
| 3 | Two delivery paths answer the same question differently | **confirmed, closed** | `c284d93b` — one cap (`limits.MAX_PENDING_DISPATCH`), the polling path now refuses work above it and leaves the update unconfirmed, so Telegram redelivers it. Nothing is lost; the queue is bounded. |
| 4 | pre-commit ignores three files | **confirmed, closed** | `006a239` — the two stale ignores are gone; the reason for them ("pre-existing PYTHONPATH issues") was fixed in the same wave that removed the identical ignore from the CI job. `test_shell_integration.py` stays ignored: it needs a live side-car. |
| 5 | `_rate_limit_send` cleans itself nowhere | **confirmed, and worse than stated** | `ee4a517` — the eviction removed chats whose deque was EMPTY, and a deque here is never empty (every key is created by a send, every send appends a timestamp). So it never fired at all, and the cap bounded nothing. It now drops windows that have expired, where the budget is already spent and nothing is reset. The test that "proved" the leak fixed filled the dict with empty deques by hand — a state the code cannot produce. |
| 6 | The size debt is `run_one`, not `call_llama`; item 13's row says "closed" next to a cost | **confirmed, closed** | The index (TECH-DEBT.md) no longer restates sizes, leaf counts or mutation counts — it points at the item files, which carry a recomputed marker. The item 8 row named a `call_llama` size the function no longer had; the item 13 row sat "closed" next to a cost that was no longer being paid. A tripwire fails if a size claim comes back into the table. The size debt that remains really is `ToolLoop.run_one` — it is registered in `tests/test_size_budget.py` with a date, a reason and a trigger. |
| 7 | The harnesses do not run in CI | **confirmed, closed** | `6e3912ee` — `.github/workflows/mutation-nightly.yml`: it builds the bot image plus pytest and runs ONE harness per night, rotating (a full cycle is about a month; the umbrella harness gets its own night). The cheap half was already in the pytest job — that every anchor still points at the tree, and that a refused mutation fails the verdict. The one harness that needs the production host carries a `REQUIRES_HOST` marker, and a tripwire checks the marker against the harness's behaviour in both directions. |

### The threads

* **`except Exception: pass`** — audited, and the numbers needed a
  third reading. The review's 42 is the number of **textual
  occurrences** of the string `except Exception:` in the bot package,
  and it is neither a file count nor a site count: two of the 42 are
  not handlers at all (a comment in `access.py`, an example inside a
  docstring in `turn.py`), so 40 are handler headers of that exact
  spelling; 40 files contain `except Exception` in any spelling, and 17
  of them with that exact one. By site the package has 125 handlers
  catching `Exception` — 40 of them without `as`, 85 binding the name
  — and 9 more catching `BaseException`: 134 handler sites in all. The
  review's "20 of them a bare `pass`" is 19 handlers with a bare `pass`
  body plus the comment that writes the pattern out; there are 18 of
  them now, because this wave fixed one. Read one by one, they are the
  deliberate pattern: the cleanup must not mask the error already being
  reported, and the decoration must not finish the turn. **One was not
  that**: the photo path swallowed the failure of the failure notice,
  while the voice and document handlers log it — so a user whose
  Telegram was down left nothing behind, neither for them nor for the
  operator. Fixed in `d47f025`, with an AST tripwire (a comment is not
  a log line, and a neighbouring handler's log does not cover this
  one).
* **PASSIVE `wal_checkpoint` "9 places in storage.py"** — measured: one
  function, nine call sites, and it is deliberate. The file has to be
  current, because the deploy's backup copies the file — a stale file is
  a 15-hour-old snapshot pretending to be a backup (measured on
  production, 2026-10-28). The cost is a PASSIVE fold that does
  nothing when there is nothing to fold. **Not a defect.**
* **"1616→478" in the index** — closed with finding 6: the index no
  longer restates sizes at all, and the historical milestones live in
  the item files where they are dated.

### What the review got right about the shape of the problem

It read the code and ran the harness instead of trusting the README,
which is the only way a review is worth anything. Its finding 5 was
even sharper than it claimed: the cap it called decorative was not
decorative, it was unreachable.
