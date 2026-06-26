# Открытые проблемы — сводный отчёт

**Дата сборки**: 2026-06-26
**Что в этом файле**: всё что мы находили за последние ~6 недель и ещё не починили.
**Что НЕ в этом файле**: проблемы из старых репортов, которые уже закрыли фиксами 10.14 / 10.17 / 10.19.

История уже закрытого (для контекста — не делать ещё раз):
- `docs/Fix/lead_evaluator_auto_trigger_broken.md` → закрыто в 10.14 (lead_evaluator не запускался после `<finishdialog/>` — фикс через `dialog_id` в payload + log.warning вместо silent return)
- `docs/Fix/cannot_write_overclassifies_floodwait.md` → закрыто в 10.17 (FloodWait отделён от CannotWrite, не уходит в CRM как invalid)
- `docs/Fix/transient_self_stop_and_privacy_misclassification.md` → **частично** закрыто в 10.19:
  - ✅ Self-stop через `asyncio.create_task` → теперь через флаг в `_account_loop` (работает)
  - ✅ `PRIVACY_PREMIUM_REQUIRED` → ловится в autochat.service через `except RPCError` + текстовый матч → `CannotWrite` → invalid в CRM
  - ❌ **`ALLOW_PAYMENT_REQUIRED`** до сих пор уходит в `SESSION_EXPIRED` (см. ниже Issue #1) — wrapper ловит его раньше autochat

---

## 🔴 Критичные (бьют по основному пайплайну)

### Issue #1 — `wrapper.py` оборачивает `ALLOW_PAYMENT_REQUIRED` как `WrapperSessionExpired`

**Где**: [modules/worker/wrapper.py](modules/worker/wrapper.py) — кортеж `_SESSION_EXPIRED_ERRORS` слишком широкий, ловит RPCError 406 (`ALLOW_PAYMENT_REQUIRED` — лид принимает DM только за Stars).

**Эффект**: Stars-only лиды (`@alin148bambola`, `@fontilove`, `@ihann_a` и др.):
- Классифицируются wrapper'ом как «сессия Telegram умерла» → autochat бросает `AutoSessionExpired` → worker_auto_runner видит `SESSION_EXPIRED` → категория Б (транзиент) → **reinsert в пул + self-stop воркера**
- Эти лиды бумерангом ходят по очереди между воркерами — каждый раз новый воркер останавливается
- Реальный кейс: вечером 26.05 `@alin148bambola` за полчаса убил 15 из 19 воркеров через цепную реакцию

**Почему 10.19 не помог**: 10.19 добавил `except RPCError` с текстовым матчем в `autochat/service.py:466+`, но wrapper.py ловит `ALLOW_PAYMENT_REQUIRED` ВЫШЕ — до того как autochat что-то увидит.

**Фикс**:
1. Сузить `_SESSION_EXPIRED_ERRORS` в [`modules/worker/wrapper.py`](modules/worker/wrapper.py) — убрать оттуда то, что ловит RPCError 406/403
2. Добавить отдельный `except RPCError as e` в `_guard()` и в `send_message`-обёртке: матчить `ALLOW_PAYMENT_REQUIRED` и пробрасывать наверх как обычный `RPCError`, чтобы autochat.service уже сам классифицировал в `CannotWrite`

**Verify**: Положить `@alin148bambola` обратно в пул, включить auto-runner на воркере. Должно быть:
- `auto_runner.lead_skipped` с `error_code: CANNOT_WRITE` (не `SESSION_EXPIRED`)
- `partner_crm.status_pushed: invalid` для username
- Воркер **не** останавливается

---

### Issue #2 — Транскрипция голосовых сообщений не работает уже месяц

**Где**: `.env` → `OPENROUTER_MODEL_TRANSCRIPTION=openai/gpt-4o-audio-preview`. OpenRouter маршрутизирует на `gpt-4o-audio-preview-2025-06-03`, который **не существует / нет доступа** (HTTP 404 на каждом вызове).

**Эффект**:
- За 7 дней наблюдения **0 успешных** транскрипций
- 19 голосовых от собеседников ушли в `transcription_status = failed`
- LLM в контексте видит `Transcription: (failed)` вместо реального текста → отвечает абстрактно, не по сути голосового
- Бессмысленная нагрузка на OpenRouter, ретраи в логах каждый раз

**Фикс**:
1. Сменить модель в `.env` на актуальную. Варианты:
   - `openai/whisper-1` (через OpenRouter)
   - Другой провайдер транскрибации (например `google/gemini-pro`)
2. Прогнать `POST /media/{id}/retranscribe` на провалившихся (опционально, можно забить)

**Бонус-замечание**: `docs/transcription.md` говорит «OpenRouter → Whisper», а по факту шлётся в `gpt-4o-audio-preview`. Доки рассинхронизированы с реальностью.

**Verify**: Получить голосовое от собеседника → media.transcription_status = `done` → media.transcription содержит текст.

---

### Issue #3 — HTML-репорт не показывает текст голосовых, даже если транскрипция успешна

**Где**: [modules/autochat/report.py:148-170](modules/autochat/report.py#L148-L170), `_load_message_history` SELECT'ит только:
```sql
SELECT m.id, m.is_outgoing, m.type, m.date, m.text,
       EXISTS (SELECT 1 FROM media WHERE message_id = m.id) AS has_media,
       ...
```
**`media.transcription` не выбирается**. В рендере на [report.py:624-626](modules/autochat/report.py#L624-L626):
```python
elif has_media:
    body = escape(_media_placeholder(m.get("type") or ""))  # "[голосовое]"
```

**Эффект**: Партнёр в HTML-отчёте видит просто `[голосовое]` / `[аудио]` / `[видео]` вместо текста. Даже когда транскрипция сработала — её в отчёте нет.

**Фикс**:
1. В SELECT добавить LEFT JOIN на `media` с подтягиванием `transcription` и `description`
2. В рендере: если media-only сообщение и есть `transcription` или `description` — печатать текст в баббле, оставив placeholder только когда обработка реально failed

**Verify**: Сгенерить отчёт на диалог с голосовыми → в HTML видно расшифровку, а не `[голосовое]`.

---

## 🟡 Существенные (бьют по UX и эффективности)

### Issue #4 — Cache-Control не выставлен на GET `/workers/{id}/auto-runner`

**Где**: [modules/worker_auto_runner/routes.py:65-72](modules/worker_auto_runner/routes.py#L65-L72), endpoint `get_auto_runner`. Response не имеет заголовков `Cache-Control: no-store`.

**Эффект**: Браузер кеширует JSON-ответы → React Query запрашивает каждые 10 сек, но браузер отдаёт кешированный (помечено «кешировано» в DevTools). UI показывает старое состояние Play/Pause часами после Hard-Refresh. Реальный кейс — кнопка SAD_08_RU «Play» вместо «Пауза», когда `is_running=true` в БД.

**Фикс**: Добавить FastAPI response middleware или явно `Response(headers={"Cache-Control": "no-store"})` на эндпоинты `/workers/*` и `/accounts*`. 5-10 строк кода.

**Verify**: После рестарта в DevTools Network не должно быть «кешировано», а ответы должны идти с `Cache-Control: no-store`.

---

### Issue #5 — Poller не учитывает `target_gender` активных воркеров

**Где**: [modules/partner_crm/poller.py:105-123](modules/partner_crm/poller.py#L105-L123) — сравнивает **общий** размер пула против `pool_low_threshold`:
```python
pool_size = await self._service.pool_size()
if pool_size >= threshold: return
```

**Эффект**: Если в пуле сидят 23 male'я при `target_gender=female` на всех 19 воркерах:
- `pool_size=23 > threshold=10` → poller считает «всё ок, не дозаливаем»
- Эффективный пул для female-воркеров = **0**
- Ферма стоит, никто не работает, partner CRM не запрашивается на refill

Реальный кейс: 23 male'я сидели в пуле с 22.05 по 01.06 — 10 дней блокировки автозабора, пришлось их вручную удалять.

**Фикс**: Считать эффективный пул с фильтром gender:
```python
async def effective_pool_size(self) -> int:
    # Какие target_gender нужны активным воркерам сейчас?
    active_genders = await db.fetch("""
        SELECT DISTINCT target_gender FROM accounts a
        JOIN worker_auto_runners war ON war.account_id = a.id
        WHERE a.lead_source = 'partner_crm' AND war.is_running = TRUE
    """)
    # Считать только то, что воркеры реально могут забрать
    ...
```

**Verify**: Положить в пул 23 male, без female. Поставить worker.target_gender=female. Через 60 сек poller должен запросить refill (а не игнорить).

---

## 🟠 Архитектурные (про fingerprint и устойчивость к Telegram-бану)

### Issue #6 — Один `api_id` на всю ферму

**Где**: `.env` → `TELEGRAM_API_ID=21102480` используется для **всех** воркеров через `wrapper._make_client(api_id=self.api_id, ...)`.

**Эффект**: Telegram легко группирует наши номера в одну ферму по api_id. Реальные кейсы:
- 26.06 в 08:32 UTC — 7 воркеров получили FloodWait в одну секунду на разных лидах (batch-action от TG)
- Сегодня (26.06) — массовый session-kill для 13 из 18 воркеров. Аналогичный фингерпринт = аналогичная судьба
- В живых остались 2 из 18

**Фикс** (масштабный):
1. Миграция: `accounts ADD COLUMN api_id INTEGER, api_hash TEXT`
2. `wrapper.__init__` принимает api_id/api_hash из аккаунта, а не из глобального settings
3. UI-мастер добавления аккаунта требует `api_id`/`api_hash` (зарегистрированные на my.telegram.org под каждый номер)
4. Существующие 2 живых аккаунта мигрировать сразу же — выдать им индивидуальные creds

**Эффект на выживание фермы — критический.** Без этого новые аккаунты будут падать так же.

---

### Issue #7 — Дефолтный device fingerprint Telethon

**Где**: [modules/worker/wrapper.py:887-895](modules/worker/wrapper.py#L887-L895), `_make_client` не задаёт:
- `device_model` (по дефолту `"Linux <kernel>"`)
- `system_version` (Python version)
- `app_version` (Telethon version)
- `lang_code` / `system_lang_code` (по дефолту `'en'`)

**Эффект**: Все наши N аккаунтов шлют **идентичный device info** при connect — ещё один маркер автоматизации для антифрод-системы TG. В сочетании с одним api_id (Issue #6) дают почти стопроцентную идентификацию фермы.

**Фикс**:
1. Миграция: `accounts ADD COLUMN device_info JSONB` (хранит {device_model, system_version, app_version, lang_code})
2. В `wrapper._make_client` подставлять из `device_info` (или генерить детерминированно из `account_id` если не задано)
3. Опционально: дефолтные значения врать что это «Android 14, Telegram X 10.5.3, ru-RU» — выглядит как реальный пользователь

---

## Приоритизация

| # | Тяжесть | Сложность | Эффект |
|---|---|---|---|
| 1 | 1-2 часа | 🔴 высокий | Lazy bouncer'ы перестанут валить ферму |
| 2 | 15 минут (.env) | 🔴 высокий | Бот начнёт реально понимать голосовые |
| 3 | 1-2 часа (SQL+рендер) | 🔴 высокий | Партнёр будет получать реальный контент |
| 4 | 10 минут (header) | 🟡 средний | UI перестанет врать |
| 5 | 1-2 часа (SQL) | 🟡 средний | Поток лидов не будет застревать |
| 6 | 1-2 дня (миграция+UI) | 🟠 высокий long-term | Защита от массового бана |
| 7 | 1 день (миграция+wrapper) | 🟠 средний long-term | Дополнительная защита |

**Рекомендация**: брать в работу в порядке 2 → 4 → 1 → 5 → 3 → 6 → 7. Первые два — копеечные правки с большим эффектом. Дальше — серьёзная работа, но особо в #6 без неё ферма будет рассыпаться раз в 1-2 недели.
