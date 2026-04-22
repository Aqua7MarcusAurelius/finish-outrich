# Модуль автообщения (AutoChat)

> Инициирует переписку с указанным `@username` через выбранный воркер,
> ведёт её по заданному промту (Opus 4.7 через OpenRouter), имитирует
> человеческое поведение: «онлайн/офлайн», тайпинг собеседника, паузы,
> отправка ответа сегментами.

---

## Область применения

Модуль управляет **только теми диалогами, которые инициировали мы сами** через `POST /autochat/start`. Всё остальное — вне его ответственности:

- Холодные входящие (когда нам написали первым) — не обрабатывает, это ручной флоу.
- Медиа в исходящих ответах — не отправляет, только текст.
- Группы / каналы — не поддерживаются (как и весь проект).
- Стратегия диалога («остановиться», «перевести на X», «попросить контакт») — полностью внутри промта. Модуль не знает что такое «цель разговора», он просто отвечает.

Модуль — первая реализация отложенного симулятора человечности для конкретного use-case. Остальные части симулятора (задержки чтения в нагоне, FloodWait-защита и т.п.) — отдельная задача на будущее.

---

## Общая картина

```
POST /autochat/start
    │
    ├── resolve @username → telegram_user_id
    ├── LLM: генерация первого сообщения по initial_prompt
    ├── wrapper.send_message(user_id, text)
    ├── INSERT autochat_sessions  (status=active, in_chat=false)
    ├── publish autochat.started + autochat.initial_sent
    └── запустить per-session task

[per-session task — крутится пока status=active]
    │
    ▼
 слушает события от AutoChatService (из шины):
   - message.saved для нашего dialog_id           (inbound / наш outgoing)
   - message.updated                              (транскрипция/описание готовы)
   - dialog.typing_observed                       (собеседник печатает)
    │
    ▼
 прогоняет state machine (см. ниже)
    │
    ▼
 по необходимости: вызывает OpenRouter, кладёт сегменты в очередь отправки
    │
    ▼
 отдельный send-loop: тайпинг → пауза → send → пауза → следующий сегмент
```

---

## State machine сессии

Состояние сессии определяется двумя вещами: `status` (active / paused / failed / stopped) и `in_chat` (true / false). Пока `status != active` — таск ничего не делает.

```
STATUS: active  (всё ниже — в этом статусе)

╔═══════════════════════════════════════════════════════════════╗
║  InChat=0 (мы не в сети)                                       ║
║                                                                ║
║  ├── нет событий                 → сидим, ничего не делаем    ║
║  ├── наше исходящее сообщение    → обновляем last_our_activity ║
║  └── inbound от собеседника      → смотрим возраст             ║
║       age = now - last_any_message_at (любого сообщения)       ║
║         0–5 мин   → sleep 15s                                  ║
║         5–10 мин  → sleep 60s                                  ║
║         ≥10 мин   → sleep 120s                                 ║
║       после sleep → перейти в InChat=1                         ║
║                                                                ║
║  Важно: enter_timer запускается РАЗ, на первом inbound-е       ║
║  после входа в InChat=0. Дальнейшие сообщения собеседника,     ║
║  пока таймер идёт — не перезапускают его. Они накапливаются    ║
║  в dialog'е и будут обработаны уже в InChat=1.                 ║
╚═══════════════════════════════════════════════════════════════╝
                            │
                  [таймер истёк]
                            ▼
╔═══════════════════════════════════════════════════════════════╗
║  InChat=1 (мы в сети)                                          ║
║                                                                ║
║  Подсостояния:                                                 ║
║                                                                ║
║  (a) waiting_media — в dialog'е есть неответенные inbound      ║
║      с неготовым медиа (transcription/description=pending).    ║
║      Ждём message.updated, пока все pending→done|failed.       ║
║      Потом → (b).                                              ║
║                                                                ║
║  (b) reply_timer — 30 сек после последнего значимого события:  ║
║      - новое inbound-сообщение              → reset timer      ║
║      - dialog.typing_observed               → reset timer      ║
║      - message.updated (у inbound с media)  → reset timer      ║
║      по истечении → (c).                                       ║
║                                                                ║
║  (c) generating — делаем chat_completion в OpenRouter.         ║
║      В это время новые inbound/typing уже запускают НОВЫЙ      ║
║      reply_timer в фоне (но не прерывают текущую генерацию).   ║
║      Когда ответ готов → парсим на сегменты → кладём в         ║
║      send_queue. Возвращаемся в (b).                           ║
║                                                                ║
║  Идле-детектор: если в dialog'е 3 минуты нет НИКАКИХ сообщений ║
║  (ни наших, ни собеседника) → in_chat = false.                 ║
╚═══════════════════════════════════════════════════════════════╝
```

**Три параллельных «контура» внутри одной сессии:**

1. **State-loop** — основной цикл, читает события из in-memory очереди, двигает стейт-машину.
2. **Reply-planner** — запускается когда входим в `reply_timer`; если планнер уже работает, просто ресетит таймер. Когда таймер догорает — вызывает OpenRouter и кладёт сегменты в `send_queue`. Может крутиться параллельно с send-loop'ом: пока один ответ отправляется, следующий уже готовится.
3. **Send-loop** — читает сегменты из `send_queue`, сериализованно: typing → пауза → send → пауза → следующий. Обеспечивает что два ответа не пойдут одновременно.

Разделение планнера и сендера — критично для пункта 3 из обсуждения: собеседник может написать пока мы отправляем прошлый ответ, а мы уже готовим следующий. Для пользователя это выглядит естественно: два ответа подряд, второй после короткой паузы.

---

## Вход / Выход (шина)

### Слушает

| Событие | Зачем |
|---|---|
| `message.saved` | Поймать ответ собеседника (фильтр: `dialog_id` ∈ активных сессий). Обновить `last_their_message_at`, `last_any_message_at`. Для наших `is_outgoing=true` — обновить `last_our_activity_at`. |
| `message.updated` | Дождаться готовности `transcription` / `description` у inbound сообщений. Пока поле не готово — не стартуем `reply_timer`. |
| `dialog.typing_observed` | Перезапустить `reply_timer`, если сейчас он идёт. Если мы в `InChat=0` — игнорируем (по условию, тайпинг не запускает вход в чат). |

### Публикует

| Событие | Когда |
|---|---|
| `autochat.started` | Сессия создана |
| `autochat.initial_sent` | Первое сообщение отправлено |
| `autochat.entered_chat` | Перешли в `InChat=1` (после enter-таймера) |
| `autochat.left_chat` | Вышли из чата по idle (`InChat=1 → 0`) |
| `autochat.generation_requested` | Ушёл запрос в OpenRouter |
| `autochat.generation_done` | Получен ответ, распарсены сегменты |
| `autochat.segment_sent` | Каждый отправленный сегмент |
| `autochat.session_stopped` | Сессия остановлена (вручную через API или автоматически) |
| `autochat.session_error` | Ошибка, которая завалила сессию (блок, невозможно писать, LLM постоянно падает и т.п.) |
| `dialog.typing_observed` | Публикуется **враппером** (см. раздел «Изменения в враппере»), слушается — этим же модулем |

---

## Таблица `autochat_sessions`

Новая таблица, владелец — модуль `autochat`.

| Поле | Тип | Что |
|---|---|---|
| `id` | int | PK |
| `account_id` | int | FK → `accounts` (воркер, от которого пишем) |
| `dialog_id` | int | FK → `dialogs` (nullable — до первого `message.saved` dialog ещё может не существовать) |
| `telegram_user_id` | bigint | целевой tg id (кэш после `resolve_username`) |
| `target_username` | string | @username как был задан |
| `system_prompt` | text | системный промт (персонаж, правила сегментации) |
| `initial_prompt` | text | промт для генерации ПЕРВОГО сообщения |
| `initial_sent_text` | text | что реально отправили |
| `status` | string | `starting` / `active` / `paused` / `failed` / `stopped` |
| `in_chat` | bool | текущее InChat (дублируется в Redis для быстрого чтения, в БД — для восстановления) |
| `last_our_activity_at` | timestamp | время последнего нашего сообщения |
| `last_their_message_at` | timestamp | время последнего сообщения собеседника |
| `last_any_message_at` | timestamp | max(above) — для расчёта enter-timer |
| `last_error` | text | последняя ошибка (nullable, для вывода в UI) |
| `created_at` | timestamp | |
| `updated_at` | timestamp | |

**Уникальность:** `(account_id, telegram_user_id)` где `status IN (active, paused)` — не даём завести две активные сессии на одну и ту же пару. Реализуется через partial unique index.

---

## API

Префикс `/autochat`. Все endpoints возвращают объект сессии с актуальными полями из БД + `worker_running` (флаг что воркер этого аккаунта запущен).

| Метод | Путь | Что делает |
|---|---|---|
| POST | `/autochat/start` | Создать и запустить сессию |
| GET | `/autochat/sessions` | Список сессий с фильтром по `account_id`, `status` |
| GET | `/autochat/sessions/{id}` | Одна сессия |
| POST | `/autochat/sessions/{id}/stop` | Остановить сессию (идемпотентно) |

### POST /autochat/start

Request:
```
{
  "account_id": 1,
  "username": "durov",
  "system_prompt": "Ты энтузиаст крипто-стартапов, пишешь коротко, ...",
  "initial_prompt": "Напиши дружелюбное первое сообщение на тему ..."
}
```

Что делает синхронно (блокирует до ответа):
1. Проверяет что воркер для `account_id` запущен. Если нет → `409 WORKER_NOT_RUNNING`.
2. Проверяет что нет активной сессии на эту пару → `409 SESSION_ALREADY_ACTIVE`.
3. Через враппер: `resolve_username(username)`. Ошибки:
   - Пользователь не найден → `404 USERNAME_NOT_FOUND`.
   - Privacy / нельзя резолвить → `400 USERNAME_UNAVAILABLE`.
4. LLM: `chat_completion` с `system_prompt` + `initial_prompt`, ограниченный ответ (одно короткое сообщение, без `<msg>`-сегментов на старте — уточнение ниже).
5. `wrapper.send_message(telegram_user_id, text)`. Ошибки:
   - Юзер заблокировал нас / privacy → `409 CANNOT_WRITE`.
6. INSERT в `autochat_sessions` (`status=active`, `in_chat=false`).
7. `publish autochat.started` + `autochat.initial_sent`.
8. Запуск per-session task в worker'e.
9. Возврат сессии.

```
200 { "session": { id, account_id, dialog_id, telegram_user_id, ..., initial_sent_text } }
409 WORKER_NOT_RUNNING
409 SESSION_ALREADY_ACTIVE
409 CANNOT_WRITE
404 USERNAME_NOT_FOUND
400 USERNAME_UNAVAILABLE
```

### GET /autochat/sessions

Query: `account_id`, `status`, `limit`, `cursor`. Без фильтров — возвращает всё.

### GET /autochat/sessions/{id}

Полный объект сессии.

### POST /autochat/sessions/{id}/stop

- Ставит `status=stopped` в БД.
- Отменяет per-session task (ждёт завершения текущей отправки, не обрывает её посреди сегмента).
- Публикует `autochat.session_stopped`.
- Идемпотентно: если уже `stopped` / `failed` — `200` с тем же состоянием.

---

## Изменения в враппере

Три добавления в `modules/worker/wrapper.py`:

### 1. `resolve_username(username)`

```
async def resolve_username(self, username: str) -> dict | None:
    # ResolveUsernameRequest через self._client
    # вернёт { telegram_user_id, first_name, last_name, is_bot, ... }
    # или None если пользователь не найден
```

Нужен для `POST /autochat/start`. Также заполняет Telethon entity cache — дальше `send_message(user_id)` будет работать без лишних запросов.

### 2. `set_typing(user_id)` / `cancel_typing(user_id)`

```
async def set_typing(self, user_id: int) -> None:
    # SetTypingRequest, action=SendMessageTypingAction
    # Telegram сам гасит через ~6 сек если не продлевать.
    # Для длинной печати — вызываем несколько раз с интервалом ~4 сек.
```

`cancel_typing` шлёт `SendMessageCancelAction` — мгновенно убирает индикатор (нужно перед `send_message`, чтобы Telegram не показал «печатает» на 0.5 сек после отправки).

### 3. Handler `events.ChatAction` → `dialog.typing_observed`

```
async def _on_chat_action(self, event):
    if event.typing and event.user_id:
        bus.publish(
            type="dialog.typing_observed",
            account_id=self.account_id,
            data={ telegram_user_id: event.user_id, at: now }
        )
```

Регистрируется при старте воркера рядом с `_on_new_message`. Шина — единственный канал, модуль autochat слушает события как любой другой consumer.

---

## Генерация ответа (OpenRouter)

### Расширение клиента

В `core/openrouter.py` добавить общий метод:

```
async def chat_completion(
    messages: list[dict],      # [{role, content}, ...]
    model: str,
    temperature: float = 0.7,
    max_tokens: int | None = None,
) -> str
```

Возвращает `choices[0].message.content` как строку. Ошибки — `OpenRouterError` (как уже есть у transcribe/describe).

### Настройки

В `.env`:
```
OPENROUTER_MODEL_AUTOCHAT=anthropic/claude-opus-4-7
```
Точное имя модели — уточнить в каталоге OpenRouter перед первым запуском.

### Сборка контекста

Для запроса в LLM собираем:

```
messages = [
  { role: "system", content: system_prompt + "\n\nТекущее время: ...\n\n" + SEGMENTATION_INSTRUCTION },
  # история диалога, хронологически:
  { role: "user" or "assistant", content: ... },
  ...
]
```

Правила сборки истории:
- `SELECT m.*, media_json FROM messages m LEFT JOIN media ...` по `dialog_id`, `ORDER BY date ASC`, `WHERE deleted_at IS NULL`.
- `role = "assistant" if m.is_outgoing else "user"`.
- Тело сообщения:
  - Если есть `m.text` — берём его.
  - Для каждого media:
    - `voice` / `audio` / `video_note`: приложить `[голос, {duration}с: «{transcription}»]`.
    - `video`: `[видео: «{description}» / «{transcription}»]`.
    - `photo` / `sticker` / `gif`: `[изображение: «{description}»]`.
    - `document`: `[документ {mime}: «{description}»]`.
  - Если `transcription_status=failed` или `description_status=failed` — пишем `[голос: не удалось распознать]`, чтобы LLM это видел.
- Удалённые сообщения (`deleted_at IS NOT NULL`) — пропускаем из контекста. Они видны в UI, но для генерации не нужны (собеседник не хотел, чтобы мы их читали).

### Промт сегментации

Часть `SEGMENTATION_INSTRUCTION` в system-промте:

```
Отвечай короткими сообщениями, как в живом чате.
Разделяй ответ на 1–4 отдельных сообщения. Каждое сообщение оборачивай в
теги <msg>...</msg>. Пример:
<msg>привет</msg><msg>да, я как раз об этом думал</msg>

Правила:
- Не нумеруй сообщения.
- Не используй markdown, эмодзи — только если это уместно в живой переписке.
- Каждое сообщение — одна мысль или короткая фраза.
- Не обязательно всегда 4 — часто достаточно 1–2.
```

Парсер — регулярка `r"<msg>(.*?)</msg>"` с `re.DOTALL`. Если ни одного `<msg>` не найдено — фолбэк: весь ответ = один сегмент (обрезаем лишние пробелы).

### Первое сообщение (initial)

Для `/autochat/start` используем тот же `chat_completion`, но:
- `messages = [{role: system, content: system_prompt + initial_prompt}]`
- Сегментацию пропускаем: первое сообщение — одно, чтобы не грузить нового собеседника потоком с незнакомого номера.
- Из ответа убираем возможные `<msg>` теги если модель всё равно их вставила.

---

## Отправка сегментов

Send-loop берёт очередной сегмент из `send_queue` и:

```
for segment in segments_from_queue:
    await wrapper.set_typing(user_id)
    typing_duration = 0.04 * len(segment) + random.uniform(0.5, 1.5)
    # ~40 мс на символ + jitter 0.5–1.5 сек
    typing_duration = min(typing_duration, 8.0)  # не более 8с на сегмент
    await asyncio.sleep(typing_duration)
    await wrapper.cancel_typing(user_id)
    await wrapper.send_message(user_id, segment)
    publish autochat.segment_sent
    # пауза между сегментами
    await asyncio.sleep(random.uniform(1.0, 3.0))
```

Сериализация ключевая: **только один send-loop на сессию**, никогда не два. Планнер может генерировать пачки параллельно, но отправка — всегда в одну очередь.

---

## Отслеживание наших исходящих

Важная тонкость: `wrapper.send_message` **не** триггерит handler `NewMessage` на нашем же клиенте (известный эффект Telethon, см. STAGE_4_REPORT). Это значит, что для наших автоотправок `message.received` и далее `message.saved` надо эмитить вручную — точно так же, как это делает `history/routes.py::send_message` при ручной отправке через API.

Вариант: вынести эту публикацию в `wrapper.send_message` (пусть враппер сам эмитит `message.received` для собственных отправок) — тогда и AutoChat, и ручной API получат это бесплатно. Либо модуль AutoChat эмитит сам. Решим при реализации — аккуратнее оценив что ломается.

---

## Восстановление при рестарте

При старте воркера:
1. `SELECT * FROM autochat_sessions WHERE account_id=$1 AND status='active'`.
2. Для каждой — поднять per-session task.
3. Таск при старте:
   - Пересчитать `last_*` из `messages` (свежайший срез точнее того что в БД сессии).
   - Если `last_their_message_at > last_our_activity_at` и `last_any_message_at` свежий (< 3 мин) — возможно стоит оценить `in_chat`. На MVP: **всегда стартуем с `in_chat=false`**. Следующее inbound от собеседника нормально запустит enter-timer, поведение не ломается, только чуть медленнее.

Таймеры, зависшие в момент падения, не восстанавливаем — они мягкие, новая оценка пересчитается.

---

## Обработка ошибок

| Тип | Что делаем |
|---|---|
| Ошибка resolve (user не найден / privacy) | Синхронно — 4xx в ответ на `/autochat/start`. Сессию не создаём. |
| Ошибка первой отправки | Аналогично — 4xx, сессию не создаём. |
| Ошибка send в активной сессии (заблокировали и т.п.) | `publish autochat.session_error`, `status=failed`, таск завершается. |
| Ошибка OpenRouter | Пробуем `OPENROUTER_AUTOCHAT_RETRIES` раз (новая настройка в `settings`, дефолт 2), между попытками — exponential backoff. Если всё равно упало → `publish autochat.session_error` как обычное событие ошибки (status=error), **сессию не валим** — ждём следующего inbound и пробуем снова. |
| `SessionExpired` в воркере | Воркер падает по своей логике (session_expired), менеджер уведомляет. AutoChat-сессии остаются `status=active` в БД — когда воркер поднимется после переавторизации, они автоматически продолжатся. |

**Все ошибки автоматически идут и в `events_archive` через обычный архиватор** — это как у всех модулей.

---

## Edge cases

**Пользователь вручную пишет в тот же диалог через `POST /accounts/{id}/messages`.**
Нормально отрабатываем — увидим наше исходящее через `message.saved`, обновим `last_our_activity_at`, в контексте LLM оно будет как `assistant`.

**Собеседник удалил сообщение.**
Hist-модуль ставит `deleted_at`. При сборке контекста для LLM — пропускаем (см. правила сборки). Если это было свежее сообщение на которое мы как раз собирались отвечать — reply_timer продолжает идти, контекст LLM будет уже без этого сообщения. Нормально.

**Транскрипция/описание упали в `failed`.**
Считаем «резолвнутым» — вышли из `waiting_media`, в контекст LLM пишем пометку `[голос: не удалось распознать]`. LLM может ответить общими словами.

**Собеседник отредактировал сообщение.**
История апдейтит `messages.text`, эмитит `message.updated`. Мы это услышим — ресетим reply_timer (ровно как на новое сообщение). Контекст LLM при следующей генерации возьмёт новый текст.

**Несколько сообщений подряд во время enter-timer (InChat=0).**
По договорённости (пункт 1): enter-timer не перезапускается. Следующие inbound просто накапливаются в diaog. Как только вошли в InChat=1, отработает waiting_media → reply_timer → LLM увидит сразу все сообщения.

**Собеседник пишет пока мы отправляем сегменты предыдущего ответа.**
(пункт 3): reply-planner запускает новый reply_timer параллельно. Генерация и отправка — независимые потоки, соединены только через `send_queue`. Новый ответ придёт в очередь и после текущего send'а отправится.

**Собеседник прислал голосовое, пока мы отправляем сегменты.**
Всё то же, плюс в точке (c) generating ждём готовность транскрипции перед тем как делать chat_completion. То есть: новый reply_timer → таймер истёк → проверяем waiting_media → ждём → делаем LLM.

**Session-таск упал с необработанным исключением.**
Wrapper в самом цикле — `try / except BaseException` с публикацией `autochat.session_error`, `status=failed`, таск завершается. Автоперезапуск — нет; пользователь может создать новую сессию вручную. Стандартно для наших модулей (как в worker_manager: одна попытка перезапуска тоже не по нашему уровню).

---

## Структура файлов

```
modules/autochat/
├── __init__.py
├── service.py            — AutoChatService: consumer шины, роутит события по sessions
├── session.py            — AutoChatSession: state-loop + reply-planner + send-loop (три таска)
├── generation.py         — сборка контекста, парсинг <msg>-сегментов, вызов OpenRouter
├── routes.py             — /autochat/* endpoints
└── errors.py             — иерархия AutoChatError → UsernameNotFound / CannotWrite / ...
```

В `core/openrouter.py` — новый метод `chat_completion()`.

В `modules/worker/wrapper.py` — `resolve_username`, `set_typing`, `cancel_typing`, handler `_on_chat_action`.

В `alembic/versions/` — новая миграция: таблица `autochat_sessions` + partial unique index + три настройки в `settings` (retries, enter-таймеры — все параметры задержек в БД, можно тюнить без рестарта).

---

## Настройки в таблице `settings`

Дефолты — первой миграцией этапа AutoChat:

| Ключ | По умолчанию | Описание |
|---|---|---|
| `autochat.enter_delay_short_sec` | `15` | Задержка входа при age сообщения 0–5 мин |
| `autochat.enter_delay_mid_sec` | `60` | 5–10 мин |
| `autochat.enter_delay_long_sec` | `120` | ≥10 мин |
| `autochat.idle_leave_sec` | `180` | Через сколько тишины уходим из чата (3 мин) |
| `autochat.reply_timer_sec` | `30` | Базовый reply-timer |
| `autochat.openrouter_retries` | `2` | Ретраи при ошибке OpenRouter |
| `autochat.typing_ms_per_char` | `40` | Имитация печати — мс на символ |

---

## MVP (первый заход) vs потом

**Включено в первый заход:**
- Таблица + миграция + настройки.
- `chat_completion` в `core/openrouter.py`.
- `resolve_username`, `set_typing`, `cancel_typing`, handler typing в wrapper.
- `POST /autochat/start` — синхронная часть (resolve, первая генерация, send, создание сессии, запуск таска).
- Per-session task: все три контура (state / planner / sender).
- State machine в полном объёме: InChat=0/1, enter-timer, reply_timer, waiting_media, idle-exit.
- Сегментация через `<msg>`, fallback на один сегмент.
- Ошибки в шину, `status=failed` при фатальных.
- `GET /autochat/sessions`, `GET /autochat/sessions/{id}`, `POST /autochat/sessions/{id}/stop`.

**Отложено (для второго захода, если захочется допилить):**
- Pause/resume (сейчас только stop).
- Восстановление сессий после рестарта приложения — пока при падении воркера сессии остаются в БД с `status=active`, но не поднимаются автоматически. Первой итерации этого достаточно — сессий 1–2, если упало, перезапустим вручную.
- Более тонкая имитация: перечитать все входящие в InChat=1 единой пачкой, с паузой между «чтением» каждого (сейчас — читаем всё мгновенно).
- SSE для UI — отдельный стрим `/autochat/sessions/{id}/stream` с событиями жизни сессии.
- Retry с exponential backoff для OpenRouter — сейчас между попытками будет фиксированная короткая пауза.

---

## Принципы

1. Модуль управляет **только** диалогами, начатыми через `/autochat/start`. Холодные входящие — не его дело.
2. Любая коммуникация с Telegram — через враппер (resolve, send, typing).
3. Все значимые переходы и ошибки публикуются на шину, как у всех модулей.
4. В БД пишет только этот модуль (в `autochat_sessions`). Остальные таблицы читает.
5. Состояние сессии — в БД (переживает рестарты) + оперативные таймеры в памяти процесса (пересчитываются при пересоздании таска).
6. Reply-planner и send-loop независимы — можно думать над следующим ответом пока отправляется текущий.
7. Ожидание готовности медиа (`message.updated`) — обязательно перед reply_timer. LLM не должна думать без полного контекста.
8. Сегментация — через теги `<msg>...</msg>` в промте. Фолбэк — один сегмент.
9. Тайминги задержек — в `settings`, меняются без рестарта.
10. Ошибка отправки (заблокировали, privacy) — `status=failed`, сессия останавливается. Ошибка OpenRouter — не фатальна, ждём следующий тригер.
