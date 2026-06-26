# Bug: транзиентные ошибки не останавливают воркер + Privacy/Payment ошибки утекают как транзиент

**Ветка**: `main` @ `d47cf31`
**Окружение**: prod-like контейнерное (docker compose), миграции `0012 (head)`
**Дата обнаружения**: 2026-05-22 (сразу после деплоя 10.17)
**Связано с**: 10.16 «не теряем лидов при сбое autochat» и 10.17
«FloodWait != CannotWrite». Оба фикса концептуально верные, но
реализация не до конца перекрывает изначально заявленное поведение.

---

## Краткая суть

Поверх 10.17 (FloodWait отделён от CannotWrite) обнаружены два бага:

1. **Self-stop воркера при транзиенте не срабатывает** —
   `worker_auto_runner._try_start_dialog` дёргает
   `asyncio.create_task(self.stop(...))`, но воркер остаётся
   `is_running=true`, событие `auto_runner.stopped` с
   `reason: transient_error:*` не публикуется. Воркер продолжает
   крутить попытки, лиды бумерангом возвращаются в пул через reinsert.

2. **`PRIVACY_PREMIUM_REQUIRED` и `ALLOW_PAYMENT_REQUIRED` утекают
   через generic `except Exception` в `SendFailedTransient`** — это
   реально невалидные лиды (privacy-only / Stars-only), должны идти
   в CRM как `invalid`. Сейчас они бесконечно бумерангом возвращаются
   в пул, никогда не финализируются у партнёра.

---

## Конкретный кейс

После rebuild на `d47cf31` и enable auto-runner на 19 воркерах
(22.05 ~10:20 UTC) одной волной 10:22:24 шесть лидов получили
`auto_runner.lead_skipped`. Разбор по `error_code` и реальному `error`:

| account | username | error_code | реальный error | классификация |
|---|---|---|---|---|
| 5 SAD_06_RU | `@tatimmy` | `SEND_FAILED_TRANSIENT` | `Too many requests` (FloodWait) | ✅ корректно (FloodWait) |
| 9 SAD_10_RU | `@annet_ladykey` | `SEND_FAILED_TRANSIENT` | `RPCError 403: PRIVACY_PREMIUM_REQUIRED` | ❌ должно быть `CANNOT_WRITE` |
| 11 JOKER_02_RU | `@elena0240` | `SEND_FAILED_TRANSIENT` | `Too many requests` (FloodWait) | ✅ корректно |
| 17 KALAW_02_US | `@afro_atenea` | `SEND_FAILED_TRANSIENT` | `Too many requests` (FloodWait) | ✅ корректно |
| 18 KALAW_03_US | `@dariasa_vd` | `SEND_FAILED_TRANSIENT` | `RPCError 403: PRIVACY_PREMIUM_REQUIRED` | ❌ должно быть `CANNOT_WRITE` |
| 19 AFERIST_01_RU | `@vorontsovaes` | `SEND_FAILED_TRANSIENT` | `RPCError 403: PRIVACY_PREMIUM_REQUIRED` | ❌ должно быть `CANNOT_WRITE` |
| 20 AFERIST_02_RU | `@alin148bambola` | `SESSION_EXPIRED` | `RPCError 406: ALLOW_PAYMENT_REQUIRED` | ❌ должно быть `CANNOT_WRITE` |

После каждого `lead_skipped`:
- В `crm_lead_pool` появилась запись с этим username (reinsert работает ✅)
- `auto_runner.stopped` с `transient_error:*` **не появился ни для одного из 7 воркеров** ❌
- Воркеры `5, 9, 11, 17, 18, 19, 20` остались `is_running=true`
- `partner_crm.status_pushed` не пушнулся для 4-х реально невалидных лидов
  (`@annet_ladykey`, `@dariasa_vd`, `@vorontsovaes`, `@alin148bambola`) ❌

Подтверждено SQL:

```sql
SELECT COUNT(*) FILTER (WHERE is_running) AS running, COUNT(*) AS total
FROM worker_auto_runners;
-- running: 19, total: 19  ← все остались бегать

SELECT * FROM events_archive
WHERE type='auto_runner.stopped'
  AND data->>'reason' LIKE 'transient_error:%'
  AND time >= NOW() - INTERVAL '30 minutes';
-- 0 rows
```

---

## Баг #1 — Self-stop не работает

### Где код

[`modules/worker_auto_runner/service.py:619-637`](../../modules/worker_auto_runner/service.py#L619-L637)

```python
# Категория Б: транзиентная ошибка → возвращаем лида в пул,
# останавливаем воркер. Оператор смотрит лог, фиксит причину,
# запускает заново.
try:
    await self._partner_crm.reinsert_into_pool(
        username=username, gender=gender,
    )
except Exception as r_err:
    log.warning(
        "auto_runner[%s]: reinsert failed for %s: %s",
        account_id, username, r_err,
    )

# Стопаем фоном — текущий task будет cancel'ed, на нашей же
# стороне выйдем через CancelledError в _account_loop.
asyncio.create_task(
    self.stop(account_id, reason=f"transient_error:{err_code}"),
    name=f"auto-runner-self-stop-{account_id}",
)
```

### Что наблюдается

- `reinsert_into_pool` — работает (видно по DB).
- `asyncio.create_task(self.stop(...))` — задача создаётся.
- Но `self.stop(...)` либо не доходит до `_set_running(..., False)`,
  либо не публикует `AUTO_RUNNER_STOPPED`, либо отменяется до конца.

В логе после волны 10:22:24 **нет строк** с `"auto_runner[...]: cancelled"`
из `_account_loop` exception-блока, нет `"WorkerAutoRunnerService loop cancelled"`,
нет следов завершения `stop()`. Видимо stop_task завершается тихо.

### Гипотезы

1. **Race с самим _account_loop**. После `create_task(stop())` управление
   возвращается в `_try_start_dialog`, потом в `_account_loop`, который
   идёт в `await _select_one(...)` → `await _chunked_sleep(...)`. Между
   этими await'ами stop_task получает CPU, делает
   `self._tasks.pop(account_id)` → но к этому моменту loop_task мог
   уже завершить итерацию и **сам** перезаписать `_tasks[account_id]`
   в start() (если был перезапуск рядом). Или, наоборот, успешно
   попал в следующую итерацию и продолжил.
2. **`async with self._lock` в start() и stop() конкурирует** —
   если start() (от user-action / restart-on-worker.started) удерживает
   lock, stop_task ждёт; затем start() кладёт новый task в `_tasks`;
   stop_task его удаляет и не находит «своего».
3. **Stop корректно отменяет старый task**, но `start()` где-то ещё
   стартует новый сразу же. В частности, `WorkerAutoRunnerService`
   слушает шину `worker.started` для авто-старта runner'ов:
   возможно после рестарта `app` все runner'ы с
   `is_running=true` в DB перезапускаются (это в принципе ожидаемо
   на старте), но если такой механизм работает и в runtime — то наш
   stop сразу же откатывается.

Точная причина требует тёплого дебага, но факт остаётся: **stop
задумывался, не материализуется**.

### Влияние

- Поломанные воркеры (FloodWait, потёкшая сессия, и т.д.) продолжают
  пытаться каждые ~12 мин (`interval_sec`).
- Лиды через reinsert возвращаются в пул, но тут же могут быть взяты
  тем же воркером (если он в очереди первый). Получаем «бумеранг» —
  лид крутится между пулом и одним и тем же сломанным воркером.
- Оператор не получает сигнал «вот этот воркер сейчас сломан, посмотри».
  Сейчас приходится самому заметить серию `SEND_FAILED_TRANSIENT`.

### Фикс — варианты

**Вариант A — синхронно дождаться stop перед выходом из _try_start_dialog.**
Вместо fire-and-forget:

```python
# Стопаем синхронно — наш собственный loop сейчас всё равно вернётся
# в _account_loop и await'нёт на sleep, так что cancel пройдёт чисто.
# create_task создаёт race с потенциальным новым start().
asyncio.shield(asyncio.create_task(
    self.stop(account_id, reason=f"transient_error:{err_code}"),
    name=f"auto-runner-self-stop-{account_id}",
))
```

`shield` не даёт отменить stop_task если кто-то отменяет родительский.

Или ещё проще — поднять флаг «должен остановиться» и обработать его в
самом `_account_loop`:

```python
# _try_start_dialog:
if from_crm and not is_invalid:
    self._self_stop_requested.add(account_id)
    return

# _account_loop, после _try_start_dialog:
if account_id in self._self_stop_requested:
    self._self_stop_requested.discard(account_id)
    await _set_running(account_id, False, last_status=f"stopped (transient_error:{err_code})")
    await bus.publish(EventType.AUTO_RUNNER_STOPPED, ..., data={"reason": "transient_error:..."})
    return  # выходим из loop, task завершается естественно
```

Это снимает race с start()/cancel целиком — loop_task сам решает «всё,
выхожу» без cancel-машинерии.

**Вариант B — добавить логи в stop() и run() / lifespan** — пока
никаких следов того, выполнился stop_task или нет. Нужен минимум
`log.info("auto_runner[%s]: self-stop start reason=%s")` в начале
stop() и аналог в конце, плюс `log.warning` если stop_task упал.

**Рекомендация**: Вариант A через флаг + дополнительное логирование.
Self-stop через `create_task` в asyncio-context'е, где исходный task
сам же должен умереть — это естественный источник race conditions.

---

## Баг #2 — `PRIVACY_PREMIUM_REQUIRED` и `ALLOW_PAYMENT_REQUIRED` уходят в транзиент

### Где код

[`modules/autochat/service.py:33-58`](../../modules/autochat/service.py#L33-L58)

```python
from telethon.errors import FloodWaitError
from telethon.errors.rpcerrorlist import (
    ChatWriteForbiddenError,
    PremiumAccountRequiredError,
    UserBlockedError,
    UserIsBlockedError,
    UserNotMutualContactError,
    UserPrivacyRestrictedError,
)
...
_TELETHON_CANNOT_WRITE_ERRORS: tuple[type[Exception], ...] = (
    ChatWriteForbiddenError,
    PremiumAccountRequiredError,    # ← это НЕ то же, что PRIVACY_PREMIUM_REQUIRED!
    UserBlockedError,
    UserIsBlockedError,
    UserNotMutualContactError,
    UserPrivacyRestrictedError,
)
```

И [`modules/autochat/service.py:463-468`](../../modules/autochat/service.py#L463-L468):

```python
except _TELETHON_CANNOT_WRITE_ERRORS as e:
    await self._mark_failed(session_id, f"cannot_write: {e}")
    raise CannotWrite(str(e)) from e
except Exception as e:
    await self._mark_failed(session_id, f"send_failed: {e}")
    raise SendFailedTransient(str(e)) from e
```

### Что не так

В установленной версии Telethon **отсутствуют** классы:

```
>>> from telethon.errors.rpcerrorlist import PrivacyPremiumRequiredError
ImportError: cannot import name 'PrivacyPremiumRequiredError'
>>> from telethon.errors.rpcerrorlist import AllowPaymentRequiredError
ImportError: cannot import name 'AllowPaymentRequiredError'
```

Telethon отдаёт эти ошибки как generic `RPCError` со строкой
`PRIVACY_PREMIUM_REQUIRED` / `ALLOW_PAYMENT_REQUIRED` в `.message`.
Они не матчатся ни на `PremiumAccountRequiredError` (это другой код —
`PREMIUM_ACCOUNT_REQUIRED`, не `PRIVACY_PREMIUM_REQUIRED`), ни на
`UserPrivacyRestrictedError` (тоже отдельный код — `USER_PRIVACY_RESTRICTED`).

В итоге catch уходит в `except Exception` → `SendFailedTransient` →
Категория Б в `worker_auto_runner` → reinsert в пул.

### Влияние

- Реально невалидные лиды (privacy-only, Stars-only) бесконечно
  бумерангом ходят между пулом и воркерами. Никогда не финализируются.
- Партнёрский CRM по этим лидам так и видит `in_progress`. Через какое-то
  время может закрыть по своему таймауту — но в нашей семантике мы не
  присылаем `invalid`, хотя должны.
- Бессмысленная нагрузка на TG-API на каждой попытке.

### Фикс

Telethon стабильно вкладывает имя ошибки в сообщение исключения.
Можно ловить generic `RPCError` и матчить по тексту:

[`modules/autochat/service.py`](../../modules/autochat/service.py)

```python
from telethon.errors import FloodWaitError, RPCError

_RPC_CANNOT_WRITE_MARKERS = (
    "PRIVACY_PREMIUM_REQUIRED",
    "ALLOW_PAYMENT_REQUIRED",
    "USER_BANNED_IN_CHANNEL",      # на всякий — тоже permanent
    "CHAT_RESTRICTED",
)

try:
    sent = await wrapper.send_message(telegram_user_id, initial_text)
except WrapperSessionExpired as e:
    ...
except FloodWaitError as e:
    ...
except _TELETHON_CANNOT_WRITE_ERRORS as e:
    raise CannotWrite(str(e)) from e
except RPCError as e:
    msg = str(e)
    if any(m in msg for m in _RPC_CANNOT_WRITE_MARKERS):
        await self._mark_failed(session_id, f"cannot_write: {e}")
        raise CannotWrite(str(e)) from e
    # Прочие RPCError — транзиент по умолчанию
    await self._mark_failed(session_id, f"send_failed: {e}")
    raise SendFailedTransient(str(e)) from e
except Exception as e:
    await self._mark_failed(session_id, f"send_failed: {e}")
    raise SendFailedTransient(str(e)) from e
```

Альтернатива (если хочется без строкового матчинга) — обновить Telethon
до версии, где есть `PrivacyPremiumRequiredError`/`AllowPaymentRequiredError`,
и добавить их в `_TELETHON_CANNOT_WRITE_ERRORS`. Но это бамп зависимости.

### Также

В кейсе выше один лид (`@alin148bambola`) поехал как `SESSION_EXPIRED`
с реальной ошибкой `RPCError 406: ALLOW_PAYMENT_REQUIRED`. Это странно —
`SESSION_EXPIRED` должно ловиться только когда Telethon бросает
`AuthKeyError`/`UserDeactivatedError`/подобное, не на per-message
`ALLOW_PAYMENT_REQUIRED`. Стоит посмотреть `WrapperSessionExpired`
ветку в [`modules/worker/wrapper.py`](../../modules/worker/wrapper.py) —
возможно, она тоже ловит лишнее.

---

## Расхлёбывание текущей пачки

После фикса вернуть в пул и/или закрыть как invalid в CRM:

```sql
-- Лиды, которые сейчас бумерангом возвращаются — реально невалидные,
-- но висят как transient.
SELECT DISTINCT data->>'username' AS username
FROM events_archive
WHERE type = 'auto_runner.lead_skipped'
  AND data->>'error_code' IN ('SEND_FAILED_TRANSIENT','SESSION_EXPIRED')
  AND (data->>'error' ILIKE '%PRIVACY_PREMIUM_REQUIRED%'
       OR data->>'error' ILIKE '%ALLOW_PAYMENT_REQUIRED%'
       OR data->>'error' ILIKE '%USER_BANNED_IN_CHANNEL%');
```

Эти 4+ собачек нужно прогнать через `partner_crm.push_lead_finalized
(terminal_status="invalid")` руками и удалить из пула.

---

## Verify после фикса

### Self-stop (баг #1)

1. Спровоцировать `SEND_FAILED_TRANSIENT` (или подождать) на воркере с активным FloodWait.
2. Проверить:
   - `auto_runner.stopped` с `reason: transient_error:SEND_FAILED_TRANSIENT` появилось в `events_archive`
   - `worker_auto_runners.is_running = false` для этого account_id
   - В логе строка `auto_runner[N]: self-stop triggered reason=...`

### Privacy/Payment (баг #2)

1. Спровоцировать `PRIVACY_PREMIUM_REQUIRED` (взять лида с Premium-only privacy в TG).
2. Проверить:
   - `auto_runner.lead_skipped` с `error_code: CANNOT_WRITE` (не `SEND_FAILED_TRANSIENT`)
   - `partner_crm.status_pushed` со `status: invalid` для того же username
   - В `crm_lead_pool` username **отсутствует** (не reinsert'нулся)

### Также

После фикса бага #1 серия из 6-7 транзиентов одной волной должна
останавливать только тех воркеров, у которых реальная проблема —
а не размазываться по всему пулу из-за бумеранга.
