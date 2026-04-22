# Отчёт по Этапу 4

> Проект: `finish-outrich` — Telegram Automation Framework
> Репо: https://github.com/Aqua7MarcusAurelius/finish-outrich
> Сессия: 22 апреля 2026, продолжение после Этапа 3

---

## Цель этапа

Поднять слой истории — всё что связано с записью сообщений в БД, их
хранением, отдачей клиентам и восстановлением при простоях:

1. Воркер качает медиа в MinIO и публикует обогащённый `message.received`
2. Модуль `history` пишет всё это в БД, эмитит `message.saved` для
   медиа-модулей (транскрипция/описание — следующие этапы)
3. Чистильщик удаляет файлы старше 3 дней, метаданные и тексты остаются
4. REST-endpoints для диалогов, сообщений и отправки + SSE реального времени
5. Модуль нагона догружает пропущенные сообщения при старте воркера

**Цель достигнута полностью.** Разбили на 6 коммитов, каждый
самостоятельный и проверенный.

---

## Структура коммитов

| Коммит | Что |
|---|---|
| 4.1 (+4.1.1) | wrapper + worker: медиа в MinIO, peer_profile через Telethon entity cache |
| 4.2.a | Обобщение `core/bus.py` — generic consumer group API |
| 4.2.b | Модуль `history` — consumer шины, writer в БД, publish `message.saved` |
| 4.3 | Чистильщик MinIO |
| 4.4 | Endpoints истории (diaolgs/messages/send/SSE) |
| 4.5 | Нагон истории при старте воркера |

---

## Этап 4.1 — Wrapper и Worker пишут медиа в MinIO

**Цель:** при получении сообщения воркер скачивает файл в MinIO и
публикует `message.received` с готовым `storage_key`, а не просто
минимальный snapshot как это было в 3.3.

### Файлы

- **`core/minio.py`** — добавлены:
  - `put_object(storage_key, data, content_type=None)` — загрузка байтов
    через `asyncio.to_thread(client.put_object, ...)` с `io.BytesIO`
  - `get_object(storage_key)` — скачивание в байты. Не нужен в Этапе 4,
    но заложен заранее — будет использоваться модулями транскрипции и
    описания в Этапах 5–6

- **`modules/worker/wrapper.py`** — серьёзное расширение:
  - `detect_media_info(m)` — разбор `MessageMediaPhoto` / `MessageMediaDocument`
    с проходом по `DocumentAttribute*`. Порядок проверок важен: кружок
    это тоже `DocumentAttributeVideo`, голосовое — тоже
    `DocumentAttributeAudio`. Поэтому сначала проверяется `round_message`
    и `voice` флаги, потом общие категории. Все поля таблицы `media`
    (кроме storage_key/transcription/description) возвращаются в dict
    плюс служебное `ext`
  - `build_storage_key(account_id, telegram_user_id, telegram_message_id, ext)` —
    формат `account_{id}/{tg_user_id}/{tg_msg_id}.{ext}`. Ключевое
    архитектурное решение: не используем внутренний `dialog_id` из БД
    (враппер его не знает и не должен), а `telegram_user_id` однозначно
    сопоставляется с `dialog_id` в БД по уникальному индексу
    `(account_id, telegram_user_id)`
  - Метод `TelegramWrapper.download_media_bytes(msg)` — обёртка над
    `client.download_media(msg, file=bytes)` через `_guard` (если
    сессия протухла — публикуется `account.session_expired` и
    пробрасывается `SessionExpired`)
  - `serialize_message` расширен:
    - `forward_from` теперь вложенный объект `{user_id, chat_id, name, date}`
      (раньше были плоские поля). Это соответствует `docs/event_bus.md`
    - `reply_to_msg_id` переименован в `reply_to_telegram_message_id`
      (тоже по доке)
  - `get_history(dialog, limit, offset_id)` — **breaking change**: теперь
    возвращает **сырые Telethon Message-объекты**, а не snapshots.
    Сделано ради модуля нагона (4.5), которому нужно через те же объекты
    скачивать медиа. Callers которым нужен snapshot могут сами вызвать
    `serialize_message(m)`. На момент изменения потребителей не было

- **`modules/worker/worker.py`** — переписана логика `_on_new_message`:
  - Фильтр `event.is_private` — группы и каналы полностью игнорируются
  - Отдельная проверка `telegram_user_id == 777000` — системный чат
    Telegram мимо (из `docs/history.md`)
  - Метод `_handle_message` отделён от `_on_new_message` для чистоты
  - Качаем медиа через `wrapper.download_media_bytes` → пишем в MinIO
    через `minio_mod.put_object` **до** публикации события
  - Если медиа упало по инфре (MinIO недоступен) — публикуем `system.error`,
    но **само сообщение всё равно публикуется** без media. Осознанное
    решение: лучше потерять файл чем потерять факт получения сообщения —
    метаданные и текст остаются
  - Добавлен `_pending_exception: Exception | None` и
    логика в `run()`: если handler поймал `SessionExpired`,
    запоминаем её, ставим `_stop_event` и после завершения цикла
    пробрасываем наружу. До этого session expired из handler гасла
    внутри и воркер продолжал крутиться впустую. В 3.3 это не
    стреляло потому что handler был тривиальным, теперь там есть
    сетевые операции (download_media)

### Проверки

Отправили 7 типов медиа: voice, photo, video (как Gallery), video_note,
document (.txt с кириллицей), sticker (.tgs), gif. Все корректно
распознаны (правильный `type`, `duration`/`width`/`height`/`mime_type`).
В MinIO файлы сложились под ключами `account_1/7875919809/NNNN.{ext}`.

Нюанс: один и тот же mp4-файл отправленный как **Video** (Gallery) →
`type: video`, а отправленный как **File** (скрепка-файл) → `type: document`.
Это поведение Telegram — он по-разному выставляет атрибуты, и у нас
это честно отражается.

---

## Этап 4.1.1 — peer_profile в payload

**Контекст:** изначально предлагали создавать диалог с пустым профилем и
заполнять потом через нагон. Пользователь обоснованно зарубил — нагон
это аварийный механизм, а профили собеседника нужны в штатном потоке.

### Решение

Использовать `event.get_chat()` в Telethon. В подавляющем большинстве
случаев это **не сетевой запрос**, а чтение entity из внутреннего кэша
сессии — Telethon сам копит entities при любом взаимодействии.
Реальный запрос идёт только при "первой встрече" нового человека.

### Файлы

- **`modules/worker/wrapper.py`**:
  - `extract_user_profile(entity)` — снимает 7 полей с User entity:
    `telegram_user_id`, `username`, `first_name`, `last_name`, `phone`,
    `is_bot`, `is_contact`
  - Недоступны без отдельного `GetFullUserRequest`: `birthday`, `bio`,
    `contact_first_name`, `contact_last_name`. Эти поля в `dialogs`
    остаются nullable, заполнять их будем лениво когда реально понадобятся
    (например, при открытии карточки `/dialogs/{id}` в UI)
  - Метод `TelegramWrapper.resolve_event_peer(event)` — обёртка над
    `event.get_chat()` через `_guard`. SessionExpired пробрасывает,
    прочие ошибки глушит в None (профиль не критичен, сообщение всё
    равно запишется)

- **`modules/worker/worker.py`**:
  - В `_handle_message` первым делом: `peer_entity = await wrapper.resolve_event_peer(event)` → `peer_profile = extract_user_profile(peer_entity)`
  - В payload `message.received` добавлено поле `peer_profile`

### Проверка

В events после рестарта увидели `peer_profile` со всеми 7 полями.
`username: "shuyartyom"`, `first_name: "Artyom"`, остальное null потому
что тестовый аккаунт не в контактах аккаунта-воркера.

---

## Этап 4.2.a — Обобщение `core/bus.py`

**Цель:** подготовить шину для второй consumer group (`history-writer`).
До этого вся логика работы с группами была захардкожена под `archive-writer`.

### Файлы

- **`core/bus.py`**:
  - Удалены: `ensure_consumer_group()`, `_read_for_archive(count, block_ms)`,
    `_ack_archive(stream_ids)` — все были заточены под `ARCHIVE_GROUP`
  - Добавлены обобщённые:
    - `ensure_group(group, start_id="0")` — идемпотентное создание группы.
      Параметр `start_id` оставлен на будущее: "0" значит "читать с
      начала потока" (наше текущее поведение для archive), "$" — "только
      новые события после создания группы"
    - `read_group(group, consumer, count, block_ms)` — батчевое чтение.
      Некорректные события (не парсится JSON) автоматически ack'аются,
      чтобы не висели в pending
    - `ack_group(group, stream_ids)` — подтверждение батча
  - `archive_writer_loop` переписан на эти функции, поведение идентично

Важное: это чистый рефакторинг без изменения поведения — archive-writer
продолжает работать как и работал.

### Проверка

После рестарта проверили что события продолжают доезжать в
`events_archive` и отдаваться через `/events`. Свежие события
появились (`message.received` про "Спиноза", `worker.started`) —
архивный писатель работает.

---

## Этап 4.2.b — Модуль истории

**Цель:** consumer-loop который пишет `message.received` в БД и
публикует `message.saved` — ключевое событие для всех медиа-модулей.

### Файлы

- **`modules/history/__init__.py`** — пустой пакетный init

- **`modules/history/service.py`** — класс `HistoryService`:
  - Consumer group `history-writer`, consumer `history-writer-1`
  - Dispatcher по типу события:
    - `message.received` → upsert dialogs + insert messages + insert media + publish `message.saved`
    - `transcription.done` → update media.transcription + publish `message.updated`
    - `description.done` → update media.description + publish `message.updated`
    - остальные ack'аются и игнорируются
  - Если handler упал — event **не ack'аем**, в следующем прогоне
    переобработаем. Для `message.received` защита от задваивания —
    unique-индекс `(dialog_id, telegram_message_id)` в БД
  - Вся запись в БД — в одной транзакции на событие. `publish message.saved`
    делается **после** commit, чтобы к моменту прилёта события медиа-модулям
    запись гарантированно была в БД (иначе была бы гонка с
    `message.saved → качать файл из media`)

### Ключевые решения

**Upsert dialogs.** Если `peer_profile` есть — обновляем профильные поля
через `COALESCE(EXCLUDED.field, dialogs.field)` — не перетираем
существующие данные NULL-ом если вдруг в одном из сообщений
`resolve_event_peer` отдал None. `is_bot` / `is_contact` — обновляем
жёстко, это актуальное состояние. Если `peer_profile=null` вообще —
просто освежаем `updated_at`.

**Начальные статусы media** (маппинг по типу):

| тип | `transcription_status` | `description_status` |
|---|---|---|
| voice, audio | `pending` | `none` |
| video, video_note | `pending` | `pending` |
| photo, sticker, gif, document | `none` | `pending` |

Кружок (video_note) попадает и в транскрипцию (аудиодорожка), и в
описание (визуальные кадры) — это прямо из `docs/media_description.md`.

**Флаги `has_*` в `message.saved`:**
- `has_text` = `bool(text)`
- `has_audio` = `voice / audio / video_note`
- `has_image` = `photo / sticker / gif`
- `has_video` = `video / video_note` (кружок одновременно в `has_audio` и `has_video`)
- `has_document` = `document`

**Reply-to.** В `message.received` приходит `reply_to_telegram_message_id`
(id сообщения в Telegram). Перед insert резолвим в внутренний
`messages.id` через SELECT. Если референс не нашёлся (reply на
сообщение которого у нас нет в БД) — пишем NULL.

### Интеграция

- **`api/main.py`**: `HistoryService` в lifespan как `asyncio.create_task(service.run())`.
  При shutdown — `service.stop()` + `task.cancel()`. Версия приложения
  поднята до `0.4.0`.

### Проверка

Отправили 5 сообщений (текст, текст, фото, кружок, ещё). В БД:

```
dialogs:  1 строка с первым peer_profile
messages: 5 строк
media:    2 строки (photo и video_note) с правильными статусами
```

В событиях появились 5 `message.saved` со структурой:

```
{
  message_id: 16, dialog_id: 1, telegram_message_id: 20728,
  is_outgoing: false,
  has_text: false, has_audio: true, has_image: false, has_video: true, has_document: false,
  media: [{ media_id: 9, type: "video_note", storage_key: "account_1/7875919809/20728.mp4", ... }]
}
```

Кружок получил `has_audio=true` **и** `has_video=true` одновременно — ровно то что планировали.

---

## Этап 4.3 — Чистильщик MinIO

**Цель:** фоновая задача удаляет файлы старше `cleaner.file_ttl_days` дней,
не трогает те что ещё в `pending`-обработке, чистит батчами.

### Файлы

- **`modules/history/cleaner.py`** — класс `Cleaner`:
  - Фоновый цикл `run()`:
    - прогон сразу при старте (до первой паузы)
    - после прогона пауза на `cleaner.interval_hours * 3600` секунд, но
      не меньше `MIN_SLEEP_SECONDS = 60` (защита от случайного `0`)
    - ожидание реализовано через `asyncio.wait_for(self._stop_event.wait(), timeout=...)`.
      Если `stop_event` установили — мгновенный выход, иначе по таймауту
      и следующая итерация
  - SQL выборки:
    ```sql
    SELECT id, storage_key FROM media
    WHERE file_deleted_at IS NULL
      AND storage_key IS NOT NULL
      AND downloaded_at < $1                         -- cutoff
      AND transcription_status != 'pending'          -- ещё обрабатывается
      AND description_status != 'pending'
    ORDER BY downloaded_at
    LIMIT $2
    ```
    `cutoff` считаем в Python как `now() - timedelta(seconds=ttl_days*86400)` —
    чисто и без возни с `make_interval` для дробных дней
  - На каждом файле `remove_object(key)` — при ошибке пропускаем
    (в следующий прогон попробуем снова), успешные собираем в `deleted_ids`
  - UPDATE media SET storage_key=NULL, file_deleted_at=NOW() делаем
    только для `deleted_ids`
  - Особый случай: если `rows` был непустой но `deleted_ids` пуст (все
    попытки упали) — публикуется `system.error`. Это значит скорее всего
    MinIO лёг целиком

- **`api/main.py`**: `Cleaner` добавлен в lifespan после `HistoryService`

### Семантика статусов

- `transcription_status != 'pending'` — это `none` / `done` / `failed`.
  Файл с `failed` чистильщик **тоже** удалит через 3 дня — осознанно,
  текста всё равно нет, админ пересоздаст если надо (`POST /media/{id}/retranscribe`)

### Проверка

Для теста пришлось:
1. `UPDATE settings SET value='0' WHERE key='cleaner.file_ttl_days'` — все файлы считаются старыми сразу
2. `UPDATE media SET transcription_status='done' WHERE transcription_status='pending'` и то же для description — модулей транскрипции ещё нет, статусы намертво висят в `pending`

После `docker compose restart app` — чистильщик выполнился при старте,
9 файлов удалились из MinIO, в БД `storage_key=NULL` + `file_deleted_at`
проставлены, событие `file.cleaned` с `count=9` и полным списком `media_ids`.

После теста вернули `cleaner.file_ttl_days` обратно в `3`.

---

## Этап 4.4 — Endpoints истории

**Цель:** 7 endpoints для UI — просмотр диалогов, сообщений, отправка,
SSE реального времени.

### Файлы

- **`modules/history/routes.py`** — новый большой роутер:
  - `GET /accounts/{id}/dialogs` — список диалогов, `LEFT JOIN LATERAL`
    для последнего сообщения, сортировка по `last_message_date DESC NULLS LAST`
  - `GET /dialogs/{id}` — карточка + `stats.messages_count`, `stats.media_count`
    через подзапросы
  - `GET /dialogs/{id}/messages` — курсорная пагинация по `(date, id)`,
    `direction=backward|forward`. Курсор — base64 JSON `{"d": iso, "i": int}`.
    `LIMIT limit+1` чтобы знать `has_more`. Для батча сообщений отдельным
    запросом выгребаются media по `message_id = ANY(...)` и собираются в
    dict `{msg_id → [media]}`. Reply-to telegram id тоже выгребается
    отдельным SELECT'ом по internal ids
  - `GET /dialogs/{id}/stream` — SSE, слушаем `bus.read_live`, фильтруем
    по `event.type in {message.saved, message.updated}` **и**
    `event.data.dialog_id == target_dialog_id`. Heartbeat раз в 30 сек тишины,
    формат через `api/sse.py` как у `/events/stream`
  - `POST /dialogs/{id}/read` — через `wrapper.read_message(telegram_user_id)`.
    Endpoint готов но UI его **не дёргает** — прочтение делаем руками,
    чтобы не конфликтовало с будущим симулятором человечности (Этап 6).
    Возвращает `{"ok": true/false}`
  - `GET /messages/{id}` — одно сообщение со своими media и reply-to
  - `POST /accounts/{id}/messages` — синхронная отправка, см. ниже

- **`modules/worker_manager/service.py`** — добавлен метод `get_wrapper(account_id)`:
  - Возвращает живой `TelegramWrapper` только если воркер запущен
    **и** `wrapper.is_connected()` вернул True. Иначе None → endpoint
    вернёт 409 `WORKER_NOT_RUNNING`

- **`modules/history/service.py`** — один фикс: в `_on_transcription_done`
  и `_on_description_done` теперь публикуется `message.updated` с
  `dialog_id`. Нужно для фильтра в `/dialogs/{id}/stream`. Резолв
  делается одним запросом через `WITH upd AS (UPDATE ... RETURNING message_id) SELECT u.message_id, m.dialog_id FROM upd u JOIN messages m ON m.id = u.message_id`

### Две болячки в `send_message` которые пришлось лечить

**Болячка 1:** `ValueError: Could not find the input entity for PeerUser(user_id=...)`.
Корень проблемы — `StringSession` в Telethon не сохраняет entity-кэш
(точнее, `access_hash`) между рестартами. После перезапуска воркера
`client.send_message(user_id)` не может разрезолвить entity и падает.

**Фикс:** в `worker.run()` после `connect()` делаем `await wrapper.get_dialogs(limit=None)`.
Telethon при этом вытягивает все диалоги с `access_hash`-ами и кладёт в
свой внутренний кэш. Дальше `send_message(user_id)` работает.
Этот же результат переиспользуется модулем нагона (4.5) — один запрос
вместо двух.

**Болячка 2:** после отправки сообщение не появлялось в БД в течение
timeout'а (endpoint возвращал 504 `SEND_TIMEOUT`), хотя в Telegram оно
приходило. Корень — Telethon **не** триггерит `NewMessage` handler для
сообщений отправленных **этим же** клиентом. Эхо приходит только с
других устройств/клиентов. Поэтому polling БД никогда ничего не находил.

**Фикс:** после успешного `wrapper.send_message()` endpoint **сам**
публикует `message.received` на шину с теми же полями что воркер:
`telegram_message_id`, `telegram_user_id`, `is_outgoing: true`, `text`,
`peer_profile: null` (не принципиально), `media: []`. Модуль истории
подхватывает обычным путём и записывает. Polling находит запись и
возвращает полный объект клиенту.

### Проверка

Поднят воркер, последовательно вызваны все endpoints — все ответили
корректно. `POST /accounts/1/messages` с body `{"dialog_id":1,"text":"..."}`
реально отправил сообщение в мой личный Telegram и вернул готовый
объект с `id=17, is_outgoing=true`.

Отдельный нюанс с PowerShell — он по умолчанию энкодит `-Body '...'` в
Windows-1251, русский текст до API долетает как `?????`. Обходится так:
```powershell
$bytes = [System.Text.Encoding]::UTF8.GetBytes('{"text":"..."}')
Invoke-RestMethod ... -Body $bytes
```
Или использовать `pwsh` (PowerShell 7+), там проблемы нет.

---

## Этап 4.5 — Нагон истории

**Цель:** при старте воркера догрузить всё что пропустили. Полноценно —
с медиа, через тот же путь обработки что основной поток.

### Файлы

- **`modules/history_sync/__init__.py`** — пустой

- **`modules/history_sync/service.py`** — класс `HistorySyncService`:
  - Принимает `account_id`, `wrapper`, опционально `dialogs_snapshot`
    (результат уже сделанного `get_dialogs` из worker warm)
  - Алгоритм:
    1. `publish sync.started` (in_progress), сохраняем `parent_id = event.id`
    2. Если `dialogs_snapshot` не передан — `wrapper.get_dialogs()`
    3. Для каждого диалога (с фильтром на `tg_user_id != 777000`):
       - `SELECT MAX(telegram_message_id)` по этой паре в нашей БД
       - Цикл `wrapper.get_history(tg_user_id, limit=chunk_size, offset_id=...)`:
         - `chunk_size` из `settings.history_sync.chunk_size` (дефолт 100)
         - `offset_id=0` на первой итерации (от самых новых), потом
           `min(id)` предыдущего батча → следующий батч старше
         - `new_messages = [m for m in batch if m.id > last_tg]`
         - если пусто — break
         - для каждого сообщения из `reversed(new_messages)` (от старых
           к новым для корректного резолва reply_to) публикуем
           `message.received` c `parent_id=sync.started`
         - `if len(batch) < chunk_size: break` — достигли самого старого
         - `await asyncio.sleep(PAUSE_BETWEEN_CHUNKS)` (0.5 сек)
       - `publish sync.dialog.done` с `telegram_user_id` и `new_messages` count
       - пауза 0.5 сек между диалогами
    4. `publish sync.done` с `dialogs_synced` и `messages_synced`

- Обработка `FloodWaitError` от Telegram:
  - `publish system.error` с `wait_seconds`
  - `await asyncio.sleep(wait+1)`
  - `continue` — повторная попытка того же батча

- Медиа качается **точно так же** как в воркере: `detect_media_info` →
  `build_storage_key` → `download_media_bytes` → `put_object`. Ошибки
  скачивания → `system.error`, но публикация `message.received` всё
  равно идёт (без media entry). Есть небольшая дубликация кода между
  `worker._handle_message` и `history_sync._publish_message` — сознательно
  пока не выносим в общий helper, чтобы не трогать протестированный
  worker. Если рефакторить — позже

- **`modules/worker/worker.py`** — интеграция нагона:
  - Единственный `get_dialogs()` — и для warm entity cache, и для нагона
  - Результат передаётся в `HistorySyncService(dialogs_snapshot=...)`
  - Нагон стартует как **отдельный asyncio.Task** — не блокирует основной
    цикл. Новые сообщения продолжают ловиться через `NewMessage` handler
    параллельно. Дубли между нагоном и handler'ом отсекает unique-индекс
  - `_run_sync_safe` — обёртка вокруг `sync_service.run()`:
    - ловит `SessionExpired` → `_pending_exception + stop_event` (воркер
      упадёт с правильным статусом через `WorkerManager`)
    - остальные ошибки **не валят воркер** — нагон уже опубликовал
      `system.error` сам
  - При `stop()` воркера — `sync_task.cancel()` + `await` до отмены,
    потом обычный путь (inflight, disconnect). Порядок важен: сначала
    отменяем нагон, потом дожидаемся inflight — чтобы нагон не успел
    напубликовать ещё событий пока мы закрываемся

- `parent_id` у всех событий нагона — id `sync.started`. Можно в любой
  момент кликнуть в логе и увидеть всю цепочку рекурсивно через
  `GET /events?root_id=<sync_started_id>`

### Проверка

Самый показательный тест:
```
1. TRUNCATE dialogs RESTART IDENTITY CASCADE   -- снесли всё
2. Рестарт app, POST /workers/1/start
3. Через 15 секунд:
   - sync.done: dialogs_synced: 1, messages_synced: 19
   - 19 событий message.received от history_sync с parent_id на sync.started
   - В БД: 1 dialog, 19 messages, 9 media
   - Все медиа (голосовое, фото, видео, кружок, документ, стикер, gif) восстановлены в MinIO
```

---

## End-to-end на живом Telegram — сводка

| Шаг | Что проверили | Результат |
|---|---|---|
| 1 | Разбор 7 типов медиа | все корректно (`type`, `duration`, `width`, `height`, `mime_type`) |
| 2 | Файлы в MinIO | ключи `account_1/7875919809/NNNN.{ext}`, превью открывается |
| 3 | `peer_profile` в `message.received` | 7 полей, username/first_name/is_bot/is_contact заполнены |
| 4 | `message.saved` после записи в БД | все флаги `has_*` корректные, включая кружок с двумя `has_audio`+`has_video` |
| 5 | Чистильщик с ttl=0 | 9 файлов удалены, `file_deleted_at` проставлены, `file.cleaned` опубликован |
| 6 | Endpoints диалогов/сообщений | все 6 GET ответили структурированным JSON'ом |
| 7 | `POST /accounts/1/messages` | сообщение ушло в Telegram, endpoint вернул `id=17, is_outgoing=true` |
| 8 | `TRUNCATE CASCADE` → рестарт воркера | нагон восстановил 19 сообщений + 9 медиа из Telegram |

---

## Нюансы сессии

### PowerShell 5.x + UTF-8 в POST body

PowerShell 5 по умолчанию энкодит `-Body '{"text":"привет"}'` в
Windows-1251, API получает `?????`. В БД сохраняется так же. Лечится:

```powershell
$bytes = [System.Text.Encoding]::UTF8.GetBytes('{"dialog_id":1,"text":"привет"}')
Invoke-RestMethod -Method POST ... -ContentType 'application/json; charset=utf-8' -Body $bytes
```

Или перейти на PowerShell 7 (`pwsh`) — там нормально из коробки.

### SSE в браузере и почему "там только heartbeat"

Если открыть `/events/stream` просто как URL в браузере — страница
вечно "грузится", видно только `: heartbeat` каждые 30 сек. Это
нормально: SSE по природе показывает **только то что прилетело после
подключения**. Если в этот момент ничего не происходит — и показывать
нечего.

Корректный способ посмотреть в разработке:
- **curl.exe** `--no-buffer http://localhost:8000/events/stream` +
  параллельно отправить что-нибудь через API
- **DevTools** (F12) → Network → клик на запрос `stream` → вкладка
  **EventStream** (Chrome) — там в удобной таблице видно приходящие
  события

Для "посмотреть что было" — `/events?limit=100`, это отдельный endpoint.

### Watchfiles и висящие SSE

`uvicorn --reload` при изменении файла пытается перезагрузить процесс.
Если в этот момент есть живое SSE-соединение — старый процесс не может
закрыться, висит на `Waiting for connections to close. (CTRL+C to force quit)`.
Новый параллельно уже поднимается, часть запросов уходит в мёртвый процесс.

Симптомы: `/workers/1/stop` висит без ответа, curl-ы тоже висят.

Лечится: **`docker compose restart app`** (это не reload, а полный
kill + start). В логах должно быть ровно одно финальное
`Application startup complete` без `Waiting for connections` / `Reloading`
в хвосте.

На будущее — в prod мы `--reload` отключим; в dev можно пока жить,
но после подозрительных правок сразу делать `restart app`.

### Telethon StringSession и entity-кэш

`StringSession` сохраняет только sessionID + auth_key, а **не** кэш
entities (user_id → access_hash). После рестарта контейнера кэш
пустой, и `send_message(user_id)` / `get_history(user_id)` падают с
`Could not find the input entity`.

Наше решение: в `worker.run()` первым делом после `connect()` дёргаем
`wrapper.get_dialogs(limit=None)`. Telethon в процессе протаскивает
все entities и кладёт их в кэш сессии. Дальше всё работает.

Побочный эффект: получили готовый snapshot диалогов для нагона
бесплатно — прокинули его в `HistorySyncService`, избежали второго
запроса.

### Telethon NewMessage handler и self-echo

Handler `NewMessage(incoming=True, outgoing=True)` ловит:
- все входящие сообщения — OK
- исходящие с **других** устройств/клиентов — OK
- исходящие **с этого же клиента** (когда мы сами вызвали `send_message`) — **НЕ ловит**

Это недокументированная особенность Telethon. Значит для отправок
через наш API мы сами должны публиковать `message.received` — что мы
и делаем в `modules/history/routes.py::send_message`.

---

## Что в итоге на GitHub

Добавлено относительно конца Этапа 3:

```
finish-outrich/
├── core/
│   ├── bus.py                         — рефакторинг: generic consumer group API
│   └── minio.py                       — +put_object, +get_object
├── api/
│   └── main.py                        — +HistoryService, +Cleaner, +history_router, v0.4.0
├── modules/
│   ├── history/
│   │   ├── __init__.py
│   │   ├── service.py                 — HistoryService (consumer группы history-writer)
│   │   ├── cleaner.py                 — Cleaner (фоновая задача)
│   │   └── routes.py                  — 7 endpoints: dialogs/messages/send/SSE/read
│   ├── history_sync/
│   │   ├── __init__.py
│   │   └── service.py                 — HistorySyncService (нагон)
│   ├── worker/
│   │   ├── wrapper.py                 — +detect_media_info, +build_storage_key,
│   │   │                                +download_media_bytes, +extract_user_profile,
│   │   │                                +resolve_event_peer, расширен serialize_message,
│   │   │                                get_history теперь возвращает raw Messages
│   │   └── worker.py                  — скачивает медиа в MinIO, peer_profile в payload,
│   │                                    стартует sync как параллельный task,
│   │                                    _pending_exception для SessionExpired из handler
│   └── worker_manager/
│       └── service.py                 — +get_wrapper(account_id)
```

**Версия приложения:** `0.4.0`
**Endpoints'ов работает:** **24** (было 17). Из них SSE — 3 (`/events/stream`,
`/workers/stream`, `/dialogs/{id}/stream`).

Новые endpoints:
- `/accounts/{id}/dialogs` (GET)
- `/dialogs/{id}` (GET)
- `/dialogs/{id}/messages` (GET)
- `/dialogs/{id}/stream` (GET SSE)
- `/dialogs/{id}/read` (POST)
- `/messages/{id}` (GET)
- `/accounts/{id}/messages` (POST)

**Endpoints от финального MVP:** ~73% (24 из 33).

---

## Что дальше — План на Этап 5

**Цель:** модуль транскрипции голосовых и видео.

**Модуль к написанию:**

`modules/transcription/service.py` — consumer шины:
- Слушает `message.saved`, фильтрует по `has_audio or has_video`
- Для каждого media с типом `voice/audio/video/video_note`:
  - `minio.get_object(storage_key)` → байты
  - отправка в OpenRouter (Whisper через их API)
  - publish `transcription.started` (in_progress, parent_id на message.saved)
  - publish `transcription.done` с `{media_id, text, status}`
- `history` уже умеет обрабатывать `transcription.done` (заглушка с 4.2.b
  готова) — допишет `media.transcription` и статус
- Ретраи: 1 повторная попытка при ошибке (`settings.transcription.retries`),
  1 при пустом результате. При финальном фейле — `status=failed`, текст
  пустой

**Что нужно для теста:** уже есть `OPENROUTER_API_KEY` в `.env` (из
Этапа 1), отправить голосовое на аккаунт, через ~секунды увидеть
запись в `media.transcription`.

**Оценка:** 2–3 часа. Основные риски — API OpenRouter (формат запроса,
передача аудио), остальное инфраструктурно готово.

После транскрипции — Этап 6 (описание фото/видео/документов через
GPT-4o с FFmpeg для видео). Он даже короче, потому что шаблон обработки
будет уже отработан.

---

## Формат старта Этапа 5

Открыть новый чат, прикрепить документацию:

минимум: `architecture.md`, `api.md`, `event_bus.md`, `database_schema.md`,
`transcription.md`, `configuration.md`, `STAGE_3_REPORT.md`, `STAGE_4_REPORT.md`
(этот файл).

Начать примерно так:

> Продолжаем проект `finish-outrich`. Этапы 1-4 закрыты — инфра, шина,
> авторизация, воркеры, история + endpoints + нагон. Репо
> https://github.com/Aqua7MarcusAurelius/finish-outrich. Отчёты по
> Этапам 3 и 4 лежат в файлах проекта. Поехали к Этапу 5 — модуль
> транскрипции голосовых/видео через OpenRouter/Whisper. Аккаунт и
> прокси те же. Работаем так же — по шагам с проверкой, файлы в чат,
> я сам коммичу.

---

*Сгенерировано в конце сессии 22.04.2026. Сессия ~4 часа.*
