# API системы

> Единая точка управления всей системой и источник данных для веб-интерфейса.
> Реализуется на FastAPI. Документация генерируется автоматически —
> открывается в браузере на `localhost:8000/docs`.

---

## Общий принцип

Веб-интерфейс (дашборд) — основной клиент этого API. Система устроена как:

```
[Веб-дашборд в браузере]
       │
       │ HTTP / SSE (один общий URL)
       ▼
[FastAPI]
  ├── Справочные endpoints — отдают данные при загрузке страниц
  │    └── /auth, /accounts, /workers, /dialogs, /messages, /media
  │
  ├── Реалтайм-стримы (SSE) — обновления без перезагрузки страницы
  │    ├── /workers/stream      — статусы воркеров (дашборд)
  │    ├── /dialogs/{id}/stream — новые сообщения (чат)
  │    └── /events/stream       — бегущий лог
  │
  └── Команды — действия пользователя
       └── создать аккаунт, запустить воркер, отправить сообщение
       │
       ▼
[PostgreSQL / Redis / MinIO / Менеджер воркеров / Модуль авторизации]
```

Прямые запросы к API без веб-интерфейса — для тестов и разработки.

---

## Группы endpoints

| Группа | Префикс | Назначение |
|---|---|---|
| Авторизация | `/auth` | Многошаговая авторизация и переавторизация |
| Аккаунты | `/accounts` | CRUD аккаунтов, прокси, настройки |
| Воркеры | `/workers` | Запуск / остановка + стрим статусов |
| Диалоги | `/dialogs` | Просмотр диалогов, сообщения, стрим нового |
| Медиа | `/media` | Файлы, транскрипты, описания |
| События | `/events` | Бегущий лог, фильтры, экспорт |
| Поиск | `/search` | Глобальный поиск по сообщениям |
| AutoChat | `/autochat` | Автодиалоги через Opus 4.7 |
| Система | `/system` | Health, stats, proxy-check, dashboard |

---

## Авторизация аккаунтов

Многошаговый процесс. Состояние между шагами живёт **в памяти процесса модуля авторизации** + ключ в Redis как адресная книга.

### Состояние сессии авторизации

На каждый запуск создаётся **session_id** (UUID). Клиент получает его в ответ на `POST /auth/start` и передаёт в каждом следующем запросе.

```
Redis ключ: auth_session:{session_id}       TTL 15 минут
Содержимое:
  phase          — code_sent | 2fa_required | done | failed
  phone          — +7999...
  name           — имя аккаунта
  proxy_primary  — socks5://...
  proxy_fallback — socks5://...
  created_at     — время создания
  error          — текст последней ошибки
```

TTL 15 минут: Telegram аннулирует код примерно через 10 минут, плюс запас на 2FA.

Объект `TelegramClient` с открытым подключением живёт в памяти процесса модуля авторизации, индексируется по `session_id`. Если процесс перезапустился — активные авторизации теряются, клиент начинает сначала.

### Endpoints

| Метод | Путь | Что делает |
|---|---|---|
| POST | `/auth/start` | Начать авторизацию нового аккаунта |
| POST | `/auth/code` | Передать код из Telegram |
| POST | `/auth/2fa` | Передать пароль 2FA |
| GET  | `/auth/status/{session_id}` | Текущая фаза авторизации |
| DELETE | `/auth/{session_id}` | Отменить активную сессию |
| POST | `/auth/reauth` | Переавторизовать существующий аккаунт |

**POST /auth/start**

Request:
```
{
  "phone": "+79991234567",
  "name": "Аккаунт для прогрева",
  "proxy_primary":  "socks5://user:pass@host:port",
  "proxy_fallback": "socks5://user:pass@host:port"
}
```

Ответы:
```
200 { "session_id": "uuid", "status": "code_sent" }
400 { "error": { "code": "PROXY_CHECK_FAILED", "message": "Основной прокси недоступен" } }
400 { "error": { "code": "PHONE_INVALID" } }
```

> Совет для UI: перед `/auth/start` вызвать `POST /system/proxy-check` — чтобы форма подсветила некорректный прокси сразу, а не после нажатия "Далее".

**POST /auth/code**

Request: `{ "session_id": "uuid", "code": "12345" }`

Ответы:
```
200 { "status": "2fa_required" }
200 { "status": "success", "account_id": 1 }
400 { "error": { "code": "CODE_INVALID" } }
400 { "error": { "code": "CODE_EXPIRED" } }
410 { "error": { "code": "SESSION_EXPIRED" } }
```

**POST /auth/2fa**

Request: `{ "session_id": "uuid", "password": "..." }`

```
200 { "status": "success", "account_id": 1 }
400 { "error": { "code": "PASSWORD_INVALID" } }
410 { "error": { "code": "SESSION_EXPIRED" } }
```

**GET /auth/status/{session_id}**

```
200 {
  "session_id": "uuid",
  "phase": "code_sent | 2fa_required | done | failed",
  "phone": "+79991234567",
  "created_at": "...",
  "error": null
}
404 если сессии нет
```

**DELETE /auth/{session_id}** — `204 No Content`.

**POST /auth/reauth**

Request: `{ "account_id": 1 }`

Берёт номер и оба прокси из БД, запускает флоу как при первой авторизации.

```
200 { "session_id": "uuid", "status": "code_sent" }
404 { "error": { "code": "ACCOUNT_NOT_FOUND" } }
400 { "error": { "code": "PROXY_CHECK_FAILED" } }
```

---

## Аккаунты

| Метод | Путь | Что делает |
|---|---|---|
| GET | `/accounts` | Список всех аккаунтов |
| GET | `/accounts/{id}` | Карточка аккаунта |
| PATCH | `/accounts/{id}` | Изменить имя и/или прокси |
| DELETE | `/accounts/{id}` | Полное удаление (аккаунт + история + файлы) |

**GET /accounts**

Query: `limit`, `cursor`, `is_active` (фильтр).

```
{
  "accounts": [
    {
      "id": 1,
      "name": "Аккаунт для прогрева",
      "phone": "+79991234567",
      "is_active": true,
      "proxy_primary":  "socks5://***@host:port",     ← логин/пароль маскируются
      "proxy_fallback": "socks5://***@host:port",
      "created_at": "2024-01-15T10:12:30Z",
      "worker_status": "running",                        ← из Redis, для удобства
      "unread_messages": 12                              ← сумма по всем диалогам
    }
  ],
  "next_cursor": "..."
}
```

**GET /accounts/{id}**

Карточка + агрегаты:
```
{
  "id": 1,
  "name": "...",
  ...,
  "stats": {
    "dialogs_count": 26,
    "messages_count": 8420,
    "unread_messages": 12,
    "media_count": 317,
    "media_pending_transcription": 2,
    "media_pending_description": 1
  }
}
```

**PATCH /accounts/{id}**

Request (любое сочетание):
```
{
  "name": "Новое имя",
  "proxy_primary":  "socks5://...",
  "proxy_fallback": "socks5://..."
}
```

При смене прокси — проверяем оба. Если воркер запущен, новые прокси применяются при следующем переключении.

```
200 { обновлённая карточка }
400 { "error": { "code": "PROXY_CHECK_FAILED" } }
404 { "error": { "code": "ACCOUNT_NOT_FOUND" } }
```

**DELETE /accounts/{id}** — полное необратимое удаление

Требует заголовок `X-Confirm-Delete: yes`.

Что делает:
1. Останавливает воркер если запущен
2. Удаляет все файлы из MinIO
3. Каскадно удаляет из БД
4. Удаляет статус из Redis
5. Публикует `account.deleted` на шину

```
200 { "deleted": true, "stats": { "deleted_messages": 1842, "deleted_files": 317 } }
404 { "error": { "code": "ACCOUNT_NOT_FOUND" } }
428 { "error": { "code": "CONFIRMATION_REQUIRED" } }
```

---

## Воркеры

| Метод | Путь | Что делает |
|---|---|---|
| GET | `/workers` | Список всех аккаунтов со статусами воркеров |
| POST | `/workers/{id}/start` | Запустить воркер |
| POST | `/workers/{id}/stop` | Аккуратно остановить |
| GET | `/workers/stream` | **SSE: изменения статусов в реальном времени** |

**GET /workers**

```
[
  { "account_id": 1, "name": "Аккаунт для прогрева", "status": "running", "uptime_seconds": 43201, "last_error": null },
  ...
]
```

Возможные `status` (из Redis): `running`, `starting`, `stopping`, `stopped`, `crashed`, `session_expired`.

**POST /workers/{id}/start**

```
200 { "account_id": 1, "status": "starting" }
404 ACCOUNT_NOT_FOUND
409 ALREADY_RUNNING | ACCOUNT_INACTIVE
```

**POST /workers/{id}/stop**

```
200 { "account_id": 1, "status": "stopping" }
404 ACCOUNT_NOT_FOUND
409 NOT_RUNNING
```

**GET /workers/stream** — SSE

Отправляет событие при любом изменении статуса любого воркера (или конкретного, если фильтр). Формат:

```
event: worker.update
data: {
  "account_id": 1,
  "status": "running",
  "previous_status": "starting",
  "uptime_seconds": 2,
  "last_error": null,
  "updated_at": "2024-01-15T10:15:02.488Z"
}
```

Query: `account_id` (фильтр по одному), `heartbeat_interval` (сек, по умолчанию 30).

Keep-alive: `: heartbeat` раз в 30 секунд.

Для дашборда: открываешь один стрим, обновляешь карточки воркеров без polling.

---

## Диалоги и сообщения

| Метод | Путь | Что делает |
|---|---|---|
| GET | `/accounts/{id}/dialogs` | Список диалогов аккаунта |
| GET | `/dialogs/{id}` | Карточка диалога (собеседник) |
| GET | `/dialogs/{id}/messages` | История сообщений |
| GET | `/dialogs/{id}/stream` | **SSE: новые сообщения и обновления** |
| GET | `/messages/{id}` | Конкретное сообщение со всеми медиа |
| POST | `/accounts/{id}/messages` | Отправить сообщение через враппер |
| POST | `/messages/{id}/read` | Отметить прочитанным |
| POST | `/dialogs/{id}/read` | Отметить весь диалог прочитанным |

**GET /accounts/{id}/dialogs**

Query: `limit` (до 200), `cursor`, `search` (по имени/username собеседника), `sort` (`last_message_desc` по умолчанию, `name_asc`).

```
{
  "dialogs": [
    {
      "id": 7,
      "telegram_user_id": 555111222,
      "username": "@oleg",
      "first_name": "Олег",
      "last_name": "Петров",
      "is_contact": true,
      "contact_first_name": "Олег работа",
      "messages_count": 842,
      "unread_count": 3,
      "last_message": {
        "id": 42,
        "date": "2024-01-15T14:23:01Z",
        "is_outgoing": false,
        "preview": "голосовое 12с: «привет как дела у тебя всё...»"
      }
    }
  ],
  "next_cursor": "..."
}
```

**GET /dialogs/{id}**

Полная строка из `dialogs` + агрегаты (`messages_count`, `unread_count`, `media_count`, `first_message_date`, `last_message_date`).

**GET /dialogs/{id}/messages**

| Параметр | Описание |
|---|---|
| `limit` | До 200 |
| `cursor` | Курсор пагинации |
| `direction` | `backward` (новые→старые, по умолчанию), `forward` |
| `from`, `to` | Диапазон дат |
| `is_outgoing` | Только наши / только собеседника |
| `has_media` | Только с медиа |
| `search` | Поиск по тексту + транскриптам + описаниям в рамках диалога |

```
{
  "messages": [
    {
      "id": 42,
      "telegram_message_id": 10005,
      "dialog_id": 7,
      "is_outgoing": false,
      "is_read": true,
      "date": "2024-01-15T14:23:01Z",
      "text": null,
      "reply_to_message_id": null,
      "forward_from": null,
      "edited_at": null,
      "deleted_at": null,
      "media": [
        {
          "id": 15,
          "type": "voice",
          "mime_type": "audio/ogg",
          "duration": 12,
          "file_available": true,
          "transcription": "привет как дела у тебя всё нормально",
          "transcription_status": "done",
          "description": null,
          "description_status": "none"
        }
      ],
      "reactions": []
    }
  ],
  "next_cursor": "..."
}
```

**GET /dialogs/{id}/stream** — SSE для страницы чата

Отправляет три типа событий:

```
event: message.new
data: { ...полная структура сообщения, такая же как в /dialogs/{id}/messages... }

event: message.updated
data: {
  "message_id": 42,
  "updated_fields": ["media.transcription"],
  "media": [{ "id": 15, "transcription": "...", "transcription_status": "done" }]
}

event: message.reacted
data: { "message_id": 42, "reactions": [...новый список реакций...] }
```

Keep-alive: `: heartbeat` раз в 30 сек.

Для UI-чата: открываешь стрим, полностью готовые данные приходят в карточках сообщений, никаких дополнительных fetch.

**GET /messages/{id}** — одно сообщение по id.

**POST /accounts/{id}/messages** — отправить

Request:
```
{
  "dialog_id": 7,
  "text": "привет",
  "reply_to_message_id": 42     ← опционально
}
```

Воркер должен быть запущен. API синхронно дожидается записи и возвращает готовый объект:
```
200 { "message": { id: 43, telegram_message_id: 10043, ... } }
409 WORKER_NOT_RUNNING
404 DIALOG_NOT_FOUND
504 SEND_TIMEOUT (если ответа от Telegram нет в 30с)
```

> Отправка с медиа — отдельный endpoint с `multipart/form-data`, добавим по мере надобности.

**POST /messages/{id}/read** — `204 No Content`.

**POST /dialogs/{id}/read** — отметить весь диалог прочитанным. `204 No Content`.

---

## Медиа

| Метод | Путь | Что делает |
|---|---|---|
| GET | `/media/{id}` | Инфо о медиа-объекте |
| GET | `/media/{id}/file` | Скачать файл из MinIO |
| POST | `/media/{id}/retranscribe` | Повторно запустить транскрибацию |
| POST | `/media/{id}/redescribe` | Повторно запустить описание |

**GET /media/{id}**

Строка из таблицы `media` + флаг `file_available`:
```
{
  "id": 15,
  "message_id": 42,
  "type": "voice",
  "storage_key": "account_1/dialog_7/10005.ogg",
  "file_available": true,
  "mime_type": "audio/ogg",
  "file_size": 48120,
  "duration": 12,
  "transcription": "привет как дела у тебя всё нормально",
  "transcription_status": "done",
  "description": null,
  "description_status": "none",
  "downloaded_at": "2024-01-15T14:23:01Z",
  "file_deleted_at": null
}
```

**GET /media/{id}/file**

Стримит байты из MinIO с правильным `Content-Type` и `Content-Disposition`. Если уже удалён:
```
410 { "error": { "code": "FILE_CLEANED", "message": "Файл удалён, метаданные и транскрипт остались" } }
```

**POST /media/{id}/retranscribe**

Сбрасывает `transcription_status` в `pending`, публикует событие `media.reprocess.requested` на шину, транскрибация подхватывает.

```
200 { "media_id": 15, "status": "pending" }
404 MEDIA_NOT_FOUND
410 FILE_CLEANED
409 WRONG_MEDIA_TYPE
```

**POST /media/{id}/redescribe** — симметрично.

---

## События (шина)

### Формат события при отдаче через API

В БД лежит сырой payload. При отдаче через API добавляется вычисляемое поле **`message`** — готовая фраза на русском для отображения в логе:

```
{
  "id": "0101",
  "parent_id": "0100",
  "time": "2024-01-15T14:23:01.187Z",
  "account": "account_1",
  "module": "history",
  "type": "message.saved",
  "status": "success",
  "data": { ...сырой payload... },
  "message": "Записал сообщение из события #0100 — msg #42 в диалоге #7 (голос 12с)"
}
```

Поле `message` вычисляется на лету при отдаче из справочника шаблонов. Фронт просто рендерит строку. В БД поле `message` не хранится — можно менять формулировки без миграций.

### Endpoints

| Метод | Путь | Что делает |
|---|---|---|
| GET | `/events/stream` | SSE: бегущий лог |
| GET | `/events` | Архив с курсорной пагинацией |
| GET | `/events/{id}` | Одно событие по id (для раскрытия карточки) |
| GET | `/events/export` | JSONL / CSV выгрузка с фильтрами |

Фильтры (общие для всех трёх):
| Параметр | Описание |
|---|---|
| `account_id` | Только события аккаунта |
| `module` | `history`, `transcription`, ... |
| `type` | Тип события |
| `status` | `success`, `error`, `in_progress` |
| `from`, `to` | Диапазон времени |
| `parent_id` | Только прямые потомки |
| `root_id` | **Вся цепочка от корня рекурсивно** — для клика по ID в логе |
| `limit` | До 500 |
| `cursor` | Курсор |

Экспорт: без фильтров отказываем (`EXPORT_TOO_LARGE`), лимит 1 млн записей.

---

## Поиск

**GET /search**

Единый endpoint для глобального поиска — по тексту сообщений, транскриптам, описаниям медиа.

Query:
| Параметр | Описание |
|---|---|
| `q` | Поисковая строка (обязательный) |
| `account_id` | Ограничить аккаунтом |
| `dialog_id` | Ограничить диалогом |
| `scope` | `text`, `transcription`, `description`, `all` (по умолчанию) |
| `limit`, `cursor` | Пагинация |

Ответ:
```
{
  "results": [
    {
      "message_id": 42,
      "dialog_id": 7,
      "account_id": 1,
      "date": "2024-01-15T14:23:01Z",
      "match_type": "transcription",               ← где совпало
      "preview": "...привет как дела у тебя всё...",   ← с подсветкой совпадения
      "dialog_title": "Олег Петров"                     ← для контекста в UI
    }
  ],
  "next_cursor": "..."
}
```

По клику — открывается нужный диалог и скроллит к сообщению.

---

## AutoChat

Модуль автодиалогов. Инициирует переписку с `@username` через выбранный воркер, ведёт её по заданному промту с имитацией живого поведения. Подробно — в `autochat.md`.

| Метод | Путь | Что делает |
|---|---|---|
| POST | `/autochat/start` | Создать и запустить сессию |
| GET | `/autochat/sessions` | Список сессий (фильтры `account_id`, `status`) |
| GET | `/autochat/sessions/{id}` | Одна сессия |
| POST | `/autochat/sessions/{id}/stop` | Остановить (идемпотентно) |

**POST /autochat/start**

Request:
```
{
  "account_id": 1,
  "username": "durov",
  "system_prompt": "Ты энтузиаст крипто-стартапов, пишешь коротко, ...",
  "initial_prompt": "Напиши дружелюбное первое сообщение на тему ..."
}
```

Синхронная цепочка: резолв username через враппер → генерация первого сообщения в Opus 4.7 → отправка → INSERT в `autochat_sessions` → запуск per-session таска → возврат сессии клиенту.

Ответы:
```
200 { "session": { id, account_id, dialog_id, telegram_user_id, ..., initial_sent_text } }
409 WORKER_NOT_RUNNING
409 SESSION_ALREADY_ACTIVE
409 CANNOT_WRITE
404 USERNAME_NOT_FOUND
400 USERNAME_UNAVAILABLE
```

**GET /autochat/sessions** — список. Query: `account_id`, `status`, `limit`, `cursor`.

**GET /autochat/sessions/{id}** — полный объект сессии.

**POST /autochat/sessions/{id}/stop** — `status=stopped`, отмена per-session таска (текущая отправка сегментов допечатывается). Публикует `autochat.session_stopped`. Идемпотентно.

---

## Система

| Метод | Путь | Что делает |
|---|---|---|
| GET | `/system/health` | Живы ли компоненты |
| GET | `/system/stats` | Базовые цифры |
| POST | `/system/proxy-check` | Превалидация прокси для формы авторизации |
| GET | `/dashboard` | Всё для главной страницы одним запросом |

**GET /system/health**

```
{
  "status": "ok",
  "components": {
    "postgres": "ok",
    "redis": "ok",
    "minio": "ok",
    "auth_module": "ok"
  }
}
```

Если компонент лежит — `503 degraded`.

**GET /system/stats**

```
{
  "accounts": { "total": 3, "active": 2, "inactive": 1 },
  "workers": { "running": 2, "stopped": 1, "crashed": 0 },
  "data": {
    "dialogs_total": 84,
    "messages_total": 23410,
    "media_total": 1203,
    "media_pending": { "transcription": 2, "description": 1 }
  },
  "events_last_hour": 4821
}
```

**POST /system/proxy-check** — превалидация

Request:
```
{ "proxy": "socks5://user:pass@host:port" }
```

Можно массив, чтобы проверить оба сразу:
```
{ "proxies": ["socks5://...", "socks5://..."] }
```

Ответ:
```
200 {
  "results": [
    { "proxy": "socks5://***@host1:1080", "ok": true,  "latency_ms": 142 },
    { "proxy": "socks5://***@host2:1080", "ok": false, "error": "connection refused" }
  ]
}
```

Нужен в основном для UI-формы авторизации — чтобы подсветить плохое поле ещё до нажатия "Далее".

**GET /dashboard** — всё для главной

Один запрос возвращает весь рендер главной страницы:
```
{
  "stats":     { ...как в /system/stats... },
  "workers":   [ ...как в /workers... ],
  "accounts_with_errors": [ { account_id: 2, last_error: "..." } ],
  "recent_events": [ ...последние 20 событий с message-полем... ]
}
```

Альтернатива — три параллельных запроса (`/system/stats`, `/workers`, `/events?limit=20`). `/dashboard` просто короче и атомарнее.

---

## Для веб-интерфейса

Типовые UI-сценарии и какие endpoints дёргать.

### Сценарий 1 — открытие главной страницы (дашборд)

```
1. GET /dashboard
   → отрисовать карточки воркеров, цифры, последние события

2. Подписка: GET /workers/stream
   → при изменении статуса воркера — обновить карточку без перезагрузки

3. Подписка: GET /events/stream?limit=20
   → живой лог в правой колонке / внизу
```

### Сценарий 2 — добавление нового аккаунта (форма авторизации)

```
Шаг 1: пользователь заполнил phone + name + два прокси
   └── POST /system/proxy-check { proxies: [...] }
        → если хоть один не ok — красная подсветка поля, кнопка Далее неактивна

Шаг 2: оба прокси ок, пользователь нажал "Далее"
   └── POST /auth/start { phone, name, proxy_primary, proxy_fallback }
        → вернулся session_id, фаза code_sent
        → UI показывает поле "Введите код"

Шаг 3: пользователь ввёл код из Telegram
   └── POST /auth/code { session_id, code }
        └── ответ 2fa_required → UI показывает поле пароля
        └── ответ success → закрыть модалку, перейти к сценарию 3
        └── ответ CODE_INVALID → показать ошибку, оставить поле для новой попытки

Шаг 4 (если 2fa_required): пользователь ввёл пароль
   └── POST /auth/2fa { session_id, password }
        → success / PASSWORD_INVALID

Отмена: DELETE /auth/{session_id}
```

### Сценарий 3 — запуск воркера после авторизации

```
1. POST /workers/{account_id}/start
   → в ответ: { status: "starting" }

2. (уже подписаны на /workers/stream с дашборда)
   → прилетит worker.update с status=running — обновить карточку

3. (если подписаны на /events/stream)
   → увидим цепочку: worker.started → sync.started → sync.dialog.done ... → sync.done
```

### Сценарий 4 — открытие диалога (чат)

```
1. GET /dialogs/{id}
   → заголовок: имя собеседника, аватар, unread_count

2. GET /dialogs/{id}/messages?limit=50
   → отрисовать последние 50 сообщений внизу

3. Подписка: GET /dialogs/{id}/stream
   └── message.new     → добавить сообщение снизу
   └── message.updated → обновить текст транскрипции/описания у уже отрисованного сообщения
   └── message.reacted → обновить реакции

4. Прокрутка вверх — подгрузка истории:
   GET /dialogs/{id}/messages?cursor=...&direction=backward

5. Пользователь прочитал — POST /dialogs/{id}/read
```

### Сценарий 5 — отправка сообщения из UI-чата

```
1. Пользователь вводит текст, жмёт Enter
   └── POST /accounts/{id}/messages { dialog_id, text }
        → API ждёт пока враппер отправит и история запишет
        → возвращает готовый объект сообщения

2. UI добавляет сообщение в чат
   (можно сразу из ответа, без ожидания стрима — проще логика)

   Параллельно стрим /dialogs/{id}/stream тоже пришлёт message.new —
   UI должен дедуплицировать по message_id
```

### Сценарий 6 — просмотр лога

```
1. GET /events?limit=100
   → последние 100 событий с вычисленным message-полем
   → отрисовать таблицу

2. Подписка: GET /events/stream
   → новые события добавляются сверху

3. Клик по ID события:
   GET /events?root_id={id}
   → показать всю цепочку от корня рекурсивно

4. Экспорт:
   GET /events/export?from=...&to=...&format=jsonl
   → скачать файл
```

### Сценарий 7 — поиск сообщений

```
1. Пользователь вводит запрос в глобальный поиск
   └── GET /search?q=...&limit=20
        → список результатов с превью и названием диалога

2. Клик по результату:
   → роутинг на /dialogs/{dialog_id}
   → внутри чата: GET /dialogs/{id}/messages?around_message_id={mid}
        (endpoint для открытия диалога на конкретном сообщении — 🟡 добавить если понадобится)
```

### Потребление SSE в браузере

JS-клиент использует стандартный `EventSource`:
```javascript
const es = new EventSource('/workers/stream');
es.addEventListener('worker.update', e => {
  const data = JSON.parse(e.data);
  updateWorkerCard(data);
});
```

Браузер сам переподключается при разрыве. При переподключении API принимает `Last-Event-ID` и досылает пропущенные события.

---

## CORS

Если веб-интерфейс — отдельное приложение на другом порту/домене (например, dev-сервер фронта на `localhost:3000`, API на `localhost:8000`), браузер заблокирует запросы без CORS-заголовков.

Настройка в FastAPI:
```
разрешённые origins: перечень через .env (CORS_ORIGINS=http://localhost:3000,https://app.example.com)
разрешённые методы: GET, POST, PATCH, DELETE, OPTIONS
разрешённые заголовки: Content-Type, Authorization, X-Confirm-Delete
allow_credentials: true                ← если авторизация cookie-based в будущем
```

Для SSE специально ничего не нужно — это обычный GET.

---

## Аутентификация API

Bearer token из `.env`:
```
Authorization: Bearer <API_TOKEN>
```

Токен в `.env` под ключом `API_TOKEN`, не коммитится. Меняется перезапуском контейнера.

`/docs` и `/openapi.json` — открыты в dev, закрыты в prod через `DOCS_PUBLIC=false`.

> Для веб-интерфейса на первых порах можно обойтись без аутентификации API, если интерфейс крутится на той же машине что и бэк (localhost only) и не светится наружу. Когда выставляем наружу — включаем токен.

---

## Формат ошибок

```
{
  "error": {
    "code": "SHORT_UPPERCASE_CODE",
    "message": "Человекочитаемое сообщение на русском",
    "details": { ... }
  }
}
```

HTTP-коды:
| Код | Когда |
|---|---|
| 400 | Невалидные данные |
| 401 | Нет/неверный `Authorization` |
| 404 | Сущность не найдена |
| 409 | Конфликт состояния (`ALREADY_RUNNING`, `WORKER_NOT_RUNNING`) |
| 410 | Ресурс был, но протух (`SESSION_EXPIRED`, `FILE_CLEANED`) |
| 422 | Валидация Pydantic |
| 428 | Требуется подтверждение |
| 500 | Внутренняя ошибка |
| 503 | Сервис degraded |
| 504 | Таймаут (`SEND_TIMEOUT`) |

---

## Сводная таблица — все endpoints

| Группа | Метод | Путь |
|---|---|---|
| Авторизация | POST | `/auth/start` |
| Авторизация | POST | `/auth/code` |
| Авторизация | POST | `/auth/2fa` |
| Авторизация | GET | `/auth/status/{session_id}` |
| Авторизация | DELETE | `/auth/{session_id}` |
| Авторизация | POST | `/auth/reauth` |
| Аккаунты | GET | `/accounts` |
| Аккаунты | GET | `/accounts/{id}` |
| Аккаунты | PATCH | `/accounts/{id}` |
| Аккаунты | DELETE | `/accounts/{id}` |
| Воркеры | GET | `/workers` |
| Воркеры | POST | `/workers/{id}/start` |
| Воркеры | POST | `/workers/{id}/stop` |
| Воркеры | GET | `/workers/stream` 🟢 SSE |
| Диалоги | GET | `/accounts/{id}/dialogs` |
| Диалоги | GET | `/dialogs/{id}` |
| Диалоги | GET | `/dialogs/{id}/messages` |
| Диалоги | GET | `/dialogs/{id}/stream` 🟢 SSE |
| Диалоги | POST | `/dialogs/{id}/read` |
| Сообщения | GET | `/messages/{id}` |
| Сообщения | POST | `/accounts/{id}/messages` |
| Сообщения | POST | `/messages/{id}/read` |
| Медиа | GET | `/media/{id}` |
| Медиа | GET | `/media/{id}/file` |
| Медиа | POST | `/media/{id}/retranscribe` |
| Медиа | POST | `/media/{id}/redescribe` |
| События | GET | `/events/stream` 🟢 SSE |
| События | GET | `/events` |
| События | GET | `/events/{id}` |
| События | GET | `/events/export` |
| Поиск | GET | `/search` |
| AutoChat | POST | `/autochat/start` |
| AutoChat | GET | `/autochat/sessions` |
| AutoChat | GET | `/autochat/sessions/{id}` |
| AutoChat | POST | `/autochat/sessions/{id}/stop` |
| Система | GET | `/system/health` |
| Система | GET | `/system/stats` |
| Система | POST | `/system/proxy-check` |
| Система | GET | `/dashboard` |

Всего: **37 endpoint'ов**. Из них 3 SSE-стрима.

---

## Управление модулями

На старте не делаем. Все модули работают для всех аккаунтов. Выключение модуля целиком — через `.env`.

Открытые варианты на будущее (если понадобится per-account управление):
- **Б** — per-аккаунт: `GET /accounts/{id}/modules`, `PATCH /accounts/{id}/modules`. Модули проверяют флаг при обработке
- **В** — глобально: модули вкл/выкл целиком без разреза по аккаунтам

---

## Принципы которые мы зафиксировали

1. Всё управление и доступ к данным — через API, веб-интерфейс — основной клиент
2. Документация `/docs` генерируется автоматически, каждый endpoint типизирован через Pydantic
3. Авторизация многошаговая, состояние в Redis с TTL 15 минут
4. Удаление аккаунта — `DELETE /accounts/{id}` с заголовком подтверждения
5. Жизненный цикл процесса воркера отделён от управления аккаунтом
6. Для UI-реалтайма — три SSE-канала: воркеры, диалог, шина событий
7. События при отдаче через API получают вычисляемое поле `message` — готовую фразу
8. Отправка сообщений — синхронно, API ждёт `message.sent` и возвращает объект
9. Файлы из MinIO стримятся, если удалены — `FILE_CLEANED`
10. Повторная обработка медиа — через событие `media.reprocess.requested` на шину
11. Бегущий лог — SSE, не WebSocket
12. Архив и поиск — курсорная пагинация, не offset
13. Экспорт — JSONL, без фильтров отказываем
14. `POST /system/proxy-check` — для превалидации в UI-форме до `/auth/start`
15. CORS конфигурируется через `.env`
16. Формулировки фраз в логе живут в коде API (справочник шаблонов), не в БД, не на фронте
17. Дашборд слушает шину через SSE — база не опрашивается постоянно. При первой загрузке — REST, дальше — события
18. API не общается с модулями напрямую — читает из Redis, команды передаёт менеджеру воркеров
19. ORM не используем — SQL руками через asyncpg
20. SSE переподключение — принимаем Last-Event-ID и досылаем пропущенные события
21. Управление модулями на старте не делаем — все модули работают для всех аккаунтов
