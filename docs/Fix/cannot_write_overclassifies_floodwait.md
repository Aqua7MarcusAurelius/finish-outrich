# Bug: FloodWait классифицируется как `CANNOT_WRITE` → лид уходит в CRM как `invalid`

**Статус**: ✅ **FIXED** — см. `docs/dev_diary/10.17_floodwait_classification.md`.
Применён Вариант 1: гранулярная классификация в autochat
(`FloodWait` + `SendFailedTransient` отдельно от `CannotWrite`),
плюс в качестве default'а для непонятного выбран `SendFailedTransient`
(транзиент) — безопаснее чем ошибочный `invalid` у партнёра.

**Ветка**: `main` @ `f0743c7`
**Окружение**: prod-like контейнерное (docker compose), миграции `0012 (head)`
**Дата обнаружения**: 2026-05-22
**Связано с**: фикс 10.16 «не теряем лидов при сбое autochat» — задумка верная, но классификация ошибок в нижестоящем модуле его подрывает.

---

## Краткая суть

10.16 разделил ошибки `autochat.create_session` на две категории:

- **A** (лид физически невалиден) — `USERNAME_NOT_FOUND`, `USERNAME_UNAVAILABLE`,
  `CANNOT_WRITE` → push в CRM `lead_status=invalid`, в пул не возвращаем.
- **B** (транзиент) — всё остальное → reinsert в пул, останавливаем воркер.

Замысел правильный. Но в `autochat/service.py` есть catch-all блок,
который заворачивает **любое** исключение из `wrapper.send_message`
(включая `FloodWaitError`) в `CannotWrite`. В итоге FloodWait —
транзиентная ошибка нашей стороны — едет по ветке A: партнёру в CRM
улетает `invalid`, лид теряется со стороны учёта.

---

## Конкретный кейс

После включения Play на 19 воркерах (22.05 09:57 UTC), за первые
минуты три лида получили FloodWait на отправке стартового сообщения.

| Время (UTC) | account_id | worker | username | error_code | error |
|---|---|---|---|---|---|
| `09:57:27.88` | 5 | SAD_06_RU | `@stasharz` | **`CANNOT_WRITE`** | Too many requests (caused by SendMessageRequest) |
| `09:57:28.45` | 11 | JOKER_02_RU | `@eva_strong` | **`CANNOT_WRITE`** | Too many requests (caused by SendMessageRequest) |
| `09:57:30.09` | 17 | KALAW_02_US | `@regina_krm` | **`CANNOT_WRITE`** | Too many requests (caused by SendMessageRequest) |

В пределах 200-500 мс после каждого `auto_runner.lead_skipped` шёл
`partner_crm.status_pushed` с `status: invalid` для того же username:

```
09:57:27.88  auto_runner.lead_skipped   stasharz   CANNOT_WRITE
09:57:28.34  partner_crm.status_pushed  stasharz   status=invalid
09:57:28.45  auto_runner.lead_skipped   eva_strong CANNOT_WRITE
09:57:28.61  partner_crm.status_pushed  eva_strong status=invalid
09:57:30.09  auto_runner.lead_skipped   regina_krm CANNOT_WRITE
09:57:30.25  partner_crm.status_pushed  regina_krm status=invalid
```

То есть три валидные собачки помечены у партнёра как непригодные —
из-за нашего же троттлинга в TG.

---

## Корневая причина

### Точка ошибки

[`modules/autochat/service.py:420-424`](../../modules/autochat/service.py#L420-L424)

```python
try:
    sent = await wrapper.send_message(...)
except Exception as e:
    await self._mark_failed(session_id, f"send_failed: {e}")
    raise CannotWrite(str(e)) from e
```

Catch-all `except Exception` ловит **всё**, в том числе:

- `telethon.errors.FloodWaitError` (TG throttling — **transient**)
- `RPCError 403 PRIVACY_PREMIUM_REQUIRED` (лид принимает DM только от Premium — invalid)
- `RPCError 406 ALLOW_PAYMENT_REQUIRED` (лид требует Stars — invalid)
- `RPCError ChatWriteForbidden` (заблокировал нас — invalid)
- Прочее → ?

И всё это в одну корзину `CannotWrite`.

### Классификатор в auto_runner

[`modules/worker_auto_runner/service.py` (около строки 524)](../../modules/worker_auto_runner/service.py)

```python
_CRM_INVALID_ERROR_CODES: frozenset[str] = frozenset({
    "USERNAME_NOT_FOUND",
    "USERNAME_UNAVAILABLE",
    "CANNOT_WRITE",
})
```

Получив `err_code = "CANNOT_WRITE"`, классификатор отправляет в
ветку A → push `invalid`. Контекста о реальном типе ошибки (FloodWait
vs PRIVACY_PREMIUM) у него нет.

### Описание `CannotWrite` это подтверждает

[`modules/autochat/errors.py:50-53`](../../modules/autochat/errors.py#L50-L53)

```python
class CannotWrite(AutoChatError):
    code = "CANNOT_WRITE"
    message = "Не удалось отправить сообщение (privacy/blocked/прочее)"
```

«прочее» — это и есть проблема: класс семантически разноуровневый.

---

## Влияние

1. **Прямые потери лидов в учёте**: FloodWait у нашей стороны → партнёру
   уходит `invalid`. Партнёр их у себя закрывает, нам обратно не отдаст.
   Если воркер словил серию FloodWait — теряем серию лидов.
2. **Ложные метрики на стороне партнёра**: они видят повышенный процент
   `invalid` от нас, который на самом деле наша вина, а не качество их лидов.
3. **Воркер не останавливается** при FloodWait. По задумке 10.16
   транзиентная ошибка → воркер должен сам встать (Категория B), оператор
   видит, чинит, запускает. С `CANNOT_WRITE` воркер продолжает крутиться
   и палить следующих лидов под тем же FloodWait — то самое поведение,
   от которого 10.16 хотел уйти.

В нашем кейсе после Play на 19 воркеров за пару минут это уже всплыло:
KALAW_02_US (под трёхдневным FloodWait) сразу же выдал `invalid` на
`@regina_krm` вместо того чтобы остановиться и попросить помощи.

---

## Воспроизведение

1. Включить auto-runner на воркере с активным TG FloodWait
   (легко получить, отправив много сообщений с молодого номера).
2. Дождаться `auto_runner.lead_skipped`.
3. Проверить:
   - `data.error` содержит `Too many requests` или `FloodWaitError`.
   - `data.error_code` = `CANNOT_WRITE` ← **тут баг**.
   - Через секунду в `events_archive` появится
     `partner_crm.status_pushed` со `status: invalid` для того же username.
   - В `crm_lead_pool` лида нет (не возвращён).
   - Воркер `is_running=true`, не остановился.

SQL для проверки на любом таком инциденте:

```sql
SELECT time, type, data
FROM events_archive
WHERE time >= NOW() - INTERVAL '5 minutes'
  AND (
    (type = 'auto_runner.lead_skipped' AND data->>'error' ILIKE '%Too many requests%')
    OR (type = 'partner_crm.status_pushed' AND data->>'status' = 'invalid')
  )
ORDER BY time;
```

---

## Предлагаемые фиксы

### Вариант 1 — гранулярная классификация в autochat (предпочтительно)

[`modules/autochat/errors.py`](../../modules/autochat/errors.py) — добавить:

```python
class FloodWait(AutoChatError):
    """Telegram дросселит исходящие сообщения. Транзиент — не путать с CANNOT_WRITE."""
    code = "FLOOD_WAIT"
    message = "TG ограничил исходящие — нужно подождать"
    status_code = 429

    def __init__(self, seconds: int | None = None, message: str | None = None):
        self.seconds = seconds
        super().__init__(message)


class SendFailedTransient(AutoChatError):
    """Любая прочая транзиентная сетевая/RPC ошибка при send_message."""
    code = "SEND_FAILED_TRANSIENT"
    message = "Не удалось отправить (сеть/RPC) — повторим"
    status_code = 502
```

[`modules/autochat/service.py:420-424`](../../modules/autochat/service.py#L420-L424) — разобрать на типы:

```python
from telethon.errors import FloodWaitError, RPCError
# конкретные подклассы из telethon.errors.rpcerrorlist:
from telethon.errors.rpcerrorlist import (
    UserPrivacyRestrictedError,
    UserIsBlockedError,
    ChatWriteForbiddenError,
    PrivacyPremiumRequiredError,
    AllowPaymentRequiredError,
)

try:
    sent = await wrapper.send_message(...)
except FloodWaitError as e:
    await self._mark_failed(session_id, f"flood_wait: {e}")
    raise FloodWait(seconds=getattr(e, "seconds", None), message=str(e)) from e
except (
    UserPrivacyRestrictedError, UserIsBlockedError, ChatWriteForbiddenError,
    PrivacyPremiumRequiredError, AllowPaymentRequiredError,
) as e:
    await self._mark_failed(session_id, f"cannot_write: {e}")
    raise CannotWrite(str(e)) from e
except Exception as e:
    # Всё прочее — лучше считать транзиентом по умолчанию (безопаснее, чем invalid).
    await self._mark_failed(session_id, f"send_failed: {e}")
    raise SendFailedTransient(str(e)) from e
```

### Вариант 2 — добавить `FLOOD_WAIT` в Категорию Б в auto_runner

Без правки autochat, только список:

[`modules/worker_auto_runner/service.py`](../../modules/worker_auto_runner/service.py)

```python
# Оставляем CANNOT_WRITE в invalid (privacy/blocked), но autochat
# должен перестать оборачивать FloodWait в CannotWrite.
_CRM_INVALID_ERROR_CODES: frozenset[str] = frozenset({
    "USERNAME_NOT_FOUND",
    "USERNAME_UNAVAILABLE",
    "CANNOT_WRITE",
})
```

Этот вариант работает только если **сначала** сделан вариант 1, иначе
все FloodWait продолжают приезжать как `CANNOT_WRITE`. Нет смысла без
правки autochat.

### Рекомендация

Делать **вариант 1**. Дополнительно:

- В `FloodWait.seconds` пробросить значение секунд из Telethon — оператор
  в UI / в `auto_runner.stopped` событии увидит «остановлен на 3600s
  FloodWait», понятнее, чем абстрактное «transient».
- В `_CRM_INVALID_ERROR_CODES` оставить только то, что **100% невалидно
  на стороне лида** (privacy/blocked/username). Всё «прочее» лучше
  пускать через транзиент — потерянная пауза на воркере дешевле, чем
  ошибочный `invalid` у партнёра.

---

## Расхлёбывание уже потерянных лидов

В период до фикса в CRM партнёра уже могли уйти ложные `invalid`.
По диапазону доступных событий (с 19.05 11:25 UTC) на момент фикса
такие случаи можно собрать:

```sql
SELECT s.time, s.account_id, s.data->>'username' AS username
FROM events_archive s
WHERE s.type = 'auto_runner.lead_skipped'
  AND s.data->>'error_code' = 'CANNOT_WRITE'
  AND (s.data->>'error' ILIKE '%Too many requests%'
       OR s.data->>'error' ILIKE '%FloodWait%')
ORDER BY s.time;
```

Эти собачки можно (а) попросить у партнёра переоткрыть, (б) локально
вернуть в `crm_lead_pool` через `partner_crm.reinsert_into_pool`.

---

## Verify после фикса

1. На воркере под FloodWait включить auto-runner.
2. После следующего `auto_runner.lead_skipped`:
   - `data.error_code` = **`FLOOD_WAIT`** (не `CANNOT_WRITE`).
   - В `events_archive` **нет** соответствующего `partner_crm.status_pushed status=invalid`.
   - В `crm_lead_pool` username **снова есть** (reinsert сработал).
   - Воркер ушёл в `auto_runner.stopped` с `reason: transient_error:FLOOD_WAIT`.
3. На «нормальном» воркере без FloodWait тот же лид при следующей попытке
   уезжает в `auto_runner.lead_sent`.
4. На воркере, где лид реально невалиден (PRIVACY_PREMIUM_REQUIRED) —
   по-прежнему `CANNOT_WRITE` → `invalid` в CRM. Эта семантика не ломается.
