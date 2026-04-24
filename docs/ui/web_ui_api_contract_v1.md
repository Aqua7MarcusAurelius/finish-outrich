# Контракт API для веб-интерфейса

> Какие ручки нужно добавить в существующий FastAPI чтобы заработали страницы  
> `web_ui_dialogs_page_v1` и `web_ui_event_log_page_v1`.

---

## Что есть сейчас

В документации описаны ручки управления воркерами из `module_worker_manager_v1`:

| Ручка | Что делает |
|---|---|
| `worker.start` | запускает воркер для аккаунта |
| `worker.stop` | останавливает воркер |
| `worker.delete` | удаляет аккаунт полностью |
| `worker.list` | возвращает список аккаунтов со статусами |

Плюс ручки модуля авторизации (принять номер / код / 2FA).

Для веб-интерфейса этого не хватает — нужны ручки на чтение диалогов, сообщений и событий шины.

---

## Что нужно добавить

Все новые ручки — REST-стиль, JSON в теле, курсорная пагинация где это имеет смысл.

### Для страницы «Диалоги»

| Ручка | Метод | Что возвращает |
|---|---|---|
| `/accounts` | GET | список аккаунтов + статус воркера из Redis + счётчик диалогов. Это по сути `worker.list` с добавленным `dialogs_count`. |
| `/accounts/{id}/dialogs` | GET | список диалогов аккаунта с последним сообщением |
| `/dialogs/{id}` | GET | полный профиль собеседника (все поля `dialogs`) |
| `/dialogs/{id}/messages` | GET | сообщения диалога с подтянутыми `media`, `reactions`, `message_edits`, `forward_*` |
| `/messages/{id}/edits` | GET | история правок сообщения (таблица `message_edits`) |
| `/media/{id}/preview` | GET | прокся превью медиа из MinIO. Если файл уже удалён — 404. |

### Для страницы «Event log»

| Ручка | Метод | Что возвращает |
|---|---|---|
| `/events` | GET | архив событий из Postgres с фильтрами и курсорной пагинацией |
| `/events/stats` | GET | агрегаты для пяти карточек метрик с теми же фильтрами |
| `/events/stream` | GET (SSE) | живой поток событий из Redis Streams с теми же фильтрами |
| `/events/{id}` | GET | полное событие для детального просмотра |
| `/events/{id}/chain` | GET | цепочка предков по `parent_id` и прямых потомков |
| `/events/export` | GET | стриминговая выгрузка в CSV или JSON с теми же фильтрами |

---

## Параметры фильтрации событий

Одинаковые для `/events`, `/events/stats`, `/events/stream` и `/events/export`.

| Параметр | Формат | Значение по умолчанию |
|---|---|---|
| `account` | int (`accounts.id`) | все |
| `module` | string | все |
| `type` | string или префикс с `*` | все |
| `status` | `success` / `error` / `in_progress` | любой |
| `from` | ISO-8601 datetime | `now - 1h` |
| `to` | ISO-8601 datetime | `now` |
| `limit` | int, max 500 | 100 |
| `cursor` | opaque string для пагинации | — |

Для `/events/stream` параметры `from`, `to`, `limit`, `cursor` не используются.

---

## Пагинация

**Курсорная, не offset.** На больших таблицах (сообщения, события) offset становится медленнее с каждой страницей.

Курсор — непрозрачная строка в base64, которую сервер выдаёт в ответе. Клиент передаёт её обратно в параметре `cursor`. Внутри сервер декодирует её в пару `(date, id)` для `WHERE (date, id) < (cursor_date, cursor_id)`.

Формат ответа:

```json
{
  "items": [ /* ... */ ],
  "next_cursor": "eyJkYXRlIjoiMjAyNi0wNC0yMlQxNDoyMjo1MSIsImlkIjoxMjc3fQ==",
  "has_more": true
}
```

Когда `has_more = false` — `next_cursor` отсутствует.

---

## Живой поток событий

**SSE, не WebSocket.** Причины:
- Поток односторонний (сервер → клиент), дуплекс не нужен
- SSE работает через обычный HTTP, проще с прокси и балансировщиками
- Автоматический реконнект встроен в браузер

Эндпоинт `/events/stream` держит соединение открытым и шлёт события как они появляются в Redis Streams. Каждое SSE-сообщение — одно событие в JSON.

```
event: event
data: {"id": 1285, "parent_id": null, "time": "...", "module": "telegram", ...}

event: event
data: {"id": 1286, ...}
```

**Фильтрация — на сервере.** Если пользователь в интерфейсе поставил `account = 1`, сервер присылает только события этого аккаунта. Интерфейс ничего не фильтрует у себя — доверяет потоку.

При смене фильтров в UI — интерфейс закрывает текущее SSE-соединение и открывает новое с обновлёнными параметрами.

---

## Проксирование медиа

Интерфейс не должен ходить в MinIO напрямую — это ломает инкапсуляцию, требует отдельных прав доступа и усложняет деплой.

Вместо этого — ручка `/media/{id}/preview`:
1. Смотрит `media` в БД, берёт `storage_key`
2. Проверяет что файл не удалён (`file_deleted_at IS NULL`)
3. Стримит байты из MinIO в ответ
4. Ставит правильный `Content-Type` из `media.mime_type`

Для превью фото и видео можно добавить трансформацию (уменьшение до 400×400) в будущем. В v1 — отдаём как есть.

---

## Ответ `/accounts` — пример

```json
[
  {
    "id": 1,
    "name": "Аккаунт для прогрева",
    "phone": "+7999...",
    "status": "running",
    "is_active": true,
    "dialogs_count": 12,
    "last_event_at": "2026-04-22T14:23:12Z"
  },
  {
    "id": 3,
    "name": "Резервный",
    "phone": "+7777...",
    "status": "crashed",
    "is_active": false,
    "dialogs_count": 4,
    "last_event_at": "2026-04-22T14:22:40Z"
  }
]
```

`status` и `last_event_at` — из Redis. `dialogs_count` — `count(*)` из БД. Остальное — из `accounts`.

---

## Ответ `/dialogs/{id}/messages` — пример

```json
{
  "items": [
    {
      "id": 42,
      "telegram_message_id": 10005,
      "is_outgoing": false,
      "date": "2026-04-22T11:22:00Z",
      "text": null,
      "type": "regular",
      "edited_at": null,
      "deleted_at": null,
      "reply_to": null,
      "forward": null,
      "media_group_id": null,
      "media": [
        {
          "id": 15,
          "type": "voice",
          "mime_type": "audio/ogg",
          "duration": 14,
          "storage_key": "account_1/dialog_7/10005.ogg",
          "preview_url": "/media/15/preview",
          "transcription": "Слушай, спасибо огромное...",
          "transcription_status": "done",
          "description": null,
          "description_status": "none"
        }
      ],
      "reactions": []
    }
  ],
  "next_cursor": "...",
  "has_more": true
}
```

Цель — свести количество запросов к минимуму. Клиент получает сообщение со всеми связанными данными одним запросом. Это денормализация в API-слое, а не в БД — в БД таблицы остаются нормальными.

---

## Ответ `/events/{id}/chain` — пример

```json
{
  "ancestors": [
    { "id": 1270, "module": "telegram",      "type": "message.received" },
    { "id": 1271, "module": "history",       "type": "message.saved" },
    { "id": 1272, "module": "transcription", "type": "transcription.started" }
  ],
  "event": {
    "id": 1277,
    "module": "transcription",
    "type": "transcription.done",
    "status": "error",
    "data": { "media_id": 13, "error": "OpenRouter 502", "retry_count": 1 }
  },
  "descendants": []
}
```

Цепочка строится рекурсивным CTE в PostgreSQL по `parent_id`. Глубина ограничена (например, 50 уровней) чтобы не уйти в бесконечность при какой-нибудь ошибке в данных.

---

## Авторизация запросов к API

В v1 — Basic auth на уровне reverse proxy перед FastAPI. Интерфейс шлёт запросы без собственной авторизации, браузер сам передаёт `Authorization` header из стандартного диалога.

Полноценная система ролей и токенов — отдельная задача на потом.

---

## Принципы которые мы зафиксировали

1. Интерфейс общается с бэкендом только через API, не напрямую в БД/Redis/MinIO
2. Курсорная пагинация, не offset — для больших таблиц это принципиально
3. Живой поток — SSE, не WebSocket: поток односторонний, с браузером проще
4. Фильтрация живого потока — на сервере, интерфейс ничего не фильтрует у себя
5. Медиа отдаются через прокси-ручку API, не прямой доступ к MinIO из браузера
6. Сообщение с прикреплёнными данными (медиа, реакции, правки) — один запрос, один ответ
7. Цепочка `parent_id` строится через рекурсивный CTE с ограничением глубины
8. Авторизация интерфейса в v1 — Basic auth на уровне reverse proxy, не в приложении
