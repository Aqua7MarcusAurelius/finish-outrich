# CRM API Token Authentication — Полное руководство

## Обзор

CRM поддерживает аутентификацию через **Bearer API Tokens** для внешних сервисов.
Это позволяет AI-платформам (tg-autocom и др.) работать как полноценный L1 оператор
через HTTP API и WebSocket, **без cookies и браузера**.

## Архитектура

```
┌─────────────────────────────────────────────────────────────────┐
│  ВНЕШНИЙ СЕРВИС (tg-autocom / AI L1 бот / любой хостинг)       │
│                                                                 │
│  Authorization: Bearer crm_a1b2c3d4e5f6g7h8i9j0...             │
└───────────────┬──────────────────────┬──────────────────────────┘
                │ HTTP REST            │ WebSocket
                ▼                      ▼
┌───────────────────────────────────────────────────────────────────┐
│  CRM (Render)                                                     │
│                                                                   │
│  apiTokenAuth middleware                                          │
│    ↓ SHA-256(token) → lookup api_tokens table                    │
│    ↓ valid? → set req.user (as if logged in via browser)         │
│    ↓ → roleGuard('l1') passes ✓                                 │
│    ↓ → all L1 endpoints work normally                            │
│                                                                   │
│  WebSocket: authenticateBearerUpgrade                            │
│    ↓ Authorization: Bearer / ?token= query param                 │
│    ↓ → real-time events: new_message, typing, user_status        │
└───────────────────────────────────────────────────────────────────┘
```

## Безопасность

- **Raw-токен НЕ хранится в БД** — только SHA-256 хэш
- Токен показывается **один раз** при создании
- Формат: `crm_<40 hex chars>` (44 символа, легко распознать в логах)
- Поддержка scopes (опционально): `['l1']`, `['l1:read']`, `['*']`
- Можно задать TTL (expires_in_days) или создать бессрочный
- Мгновенный отзыв через DELETE (кэш инвалидируется)
- Аудит: last_used_at, last_used_ip, api_token_log

---

## Шаг 1: Создание пользователя для AI

В Admin-панели CRM создать пользователя:

```
Login:    ai-l1-bot
Password: (любой сложный, для API не используется)
Role:     l1
Name:     AI L1 Operator
```

Или через API (если есть admin-сессия):

```bash
curl -X POST https://crm.example.com/api/admin/users \
  -H "Cookie: crm_session=..." \
  -H "Content-Type: application/json" \
  -d '{
    "login": "ai-l1-bot",
    "password": "random_strong_password",
    "role": "l1",
    "name": "AI L1 Operator"
  }'
```

## Шаг 2: Создание API токена

```bash
curl -X POST https://crm.example.com/api/admin/api-tokens \
  -H "Cookie: crm_session=<admin_session>" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": 42,
    "label": "tg-autocom production",
    "scopes": ["l1"],
    "expires_in_days": 365
  }'
```

**Ответ (СОХРАНИ ТОКЕН — он показывается ОДИН раз!):**

```json
{
  "token": {
    "id": 1,
    "token_prefix": "crm_a1b2c3d4",
    "user_id": 42,
    "label": "tg-autocom production",
    "scopes": ["l1"],
    "is_active": true,
    "expires_at": "2027-02-23T00:00:00.000Z",
    "created_at": "2026-02-23T12:00:00.000Z",
    "user_login": "ai-l1-bot",
    "user_role": "l1"
  },
  "raw_token": "crm_a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0",
  "warning": "Save this token now! It will not be shown again."
}
```

## Шаг 3: Управление токенами

```bash
# Список всех токенов
curl https://crm.example.com/api/admin/api-tokens \
  -H "Cookie: crm_session=<admin>"

# Отозвать токен (мгновенно прекращает доступ)
curl -X DELETE https://crm.example.com/api/admin/api-tokens/1 \
  -H "Cookie: crm_session=<admin>"

# Проверить валидность токена
curl -X POST https://crm.example.com/api/admin/api-tokens/verify \
  -H "Cookie: crm_session=<admin>" \
  -H "Content-Type: application/json" \
  -d '{"raw_token": "crm_a1b2c3d4..."}'

# Логи использования конкретного токена
curl https://crm.example.com/api/admin/api-tokens/1/logs \
  -H "Cookie: crm_session=<admin>"
```

---

## Использование токена — HTTP API

Все запросы к `/api/l1/*` работают с Bearer-токеном точно так же, как через браузер.

### Базовый шаблон (curl)

```bash
TOKEN="crm_a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0"
BASE="https://crm.example.com"

curl "$BASE/api/l1/leads" \
  -H "Authorization: Bearer $TOKEN"
```

### Python (requests)

```python
import requests

TOKEN = "crm_a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0"
BASE = "https://crm.example.com"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}

# ── Получить список лидов ──
leads = requests.get(f"{BASE}/api/l1/leads", headers=HEADERS).json()
print(f"Лидов: {len(leads['leads'])}")

# ── Забрать новых лидов из пула ──
new_leads = requests.post(f"{BASE}/api/l1/leads/request-new",
    headers=HEADERS,
    json={"count": 5}
).json()
print(f"Назначено: {new_leads['count']}")

# ── Начать чат с лидом ──
start = requests.post(f"{BASE}/api/l1/leads/petrov_ivan/start",
    headers=HEADERS
).json()
print(f"Чат: {start['status']}")

# ── Получить историю сообщений ──
messages = requests.get(f"{BASE}/api/l1/chats/petrov_ivan/messages",
    headers=HEADERS
).json()
for msg in messages['messages']:
    author = "LEAD" if msg['author_type'] == 'lead' else "L1"
    text = msg['payload'].get('text', '')
    print(f"  [{author}] {text}")

# ── Отправить сообщение ──
send = requests.post(f"{BASE}/api/l1/chats/petrov_ivan/messages",
    headers=HEADERS,
    json={"message": "Привет! Чем могу помочь?"}
).json()
print(f"Статус: {send['status']}")  # "queued"

# ── Обновить статус лида ──
status = requests.post(f"{BASE}/api/l1/leads/petrov_ivan/status",
    headers=HEADERS,
    json={"status": "ready_for_transfer"}
).json()

# ── Пометить сообщения прочитанными ──
requests.post(f"{BASE}/api/l1/chats/petrov_ivan/mark-read",
    headers=HEADERS)

# ── Синхронизация чата (подтянуть новые TG сообщения) ──
sync = requests.post(f"{BASE}/api/l1/chats/petrov_ivan/sync",
    headers=HEADERS
).json()
print(f"Непрочитанных: {sync['unread_count']}")
```

### Python (aiohttp — async)

```python
import aiohttp

TOKEN = "crm_a1b2c3d4..."
BASE = "https://crm.example.com"

async def crm_request(session, method, path, **kwargs):
    headers = {"Authorization": f"Bearer {TOKEN}"}
    async with session.request(method, f"{BASE}{path}", headers=headers, **kwargs) as resp:
        return await resp.json()

async def ai_l1_loop():
    async with aiohttp.ClientSession() as session:
        # Забрать лидов
        result = await crm_request(session, "POST", "/api/l1/leads/request-new",
            json={"count": 3})

        # Для каждого нового лида — начать чат
        for lead in result.get("assigned", []):
            username = lead["username"]
            await crm_request(session, "POST", f"/api/l1/leads/{username}/start")

        # Получить чаты с непрочитанными
        chats = await crm_request(session, "GET", "/api/l1/chats")
        for chat in chats.get("chats", []):
            if chat["unread_count"] > 0:
                messages = await crm_request(session, "GET",
                    f"/api/l1/chats/{chat['username']}/messages")
                # ... AI обрабатывает сообщения ...
                # ... генерирует ответ ...
                await crm_request(session, "POST",
                    f"/api/l1/chats/{chat['username']}/messages",
                    json={"message": ai_response})
```

---

## Использование токена — WebSocket (реалтайм)

WebSocket даёт push-уведомления о новых сообщениях, typing-индикаторах, 
статусах пользователей — **без polling**.

### Подключение

Два способа передать токен:

```python
import asyncio
import websockets
import json

TOKEN = "crm_a1b2c3d4..."

# Способ 1: Authorization header (рекомендуемый)
async def ws_with_header():
    uri = "wss://crm.example.com"
    headers = {"Authorization": f"Bearer {TOKEN}"}
    async with websockets.connect(uri, additional_headers=headers) as ws:
        await handle_events(ws)

# Способ 2: Query parameter (если клиент не поддерживает headers)
async def ws_with_query():
    uri = f"wss://crm.example.com?token={TOKEN}"
    async with websockets.connect(uri) as ws:
        await handle_events(ws)

async def handle_events(ws):
    async for raw in ws:
        event = json.loads(raw)
        event_type = event.get("type")
        data = event.get("data", {})

        if event_type == "connected":
            print(f"WS подключён, userId={data['userId']}")

        elif event_type == "new_message":
            chat = data.get("chatUsername") or data.get("chat_username", "")
            text = data.get("text", "")
            print(f"[Новое сообщение] {chat}: {text}")
            # → AI обрабатывает и отвечает через HTTP API

        elif event_type == "message_edited":
            print(f"[Редакция] {data}")

        elif event_type == "message_deleted":
            print(f"[Удалено] message_ids={data.get('message_ids')}")

        elif event_type == "typing":
            print(f"[Печатает] {data.get('username')}")

        elif event_type == "user_status":
            print(f"[Статус] {data.get('username')}: {data.get('kind')}")

        elif event_type == "messages_read":
            print(f"[Прочитано] {data}")

        elif event_type == "pong":
            pass  # heartbeat response

# Отправить typing индикатор (client → server)
async def send_typing(ws, username):
    await ws.send(json.dumps({
        "type": "typing",
        "data": {"username": username, "cancel": False}
    }))
```

### Полный цикл AI L1 с WebSocket

```python
import asyncio
import aiohttp
import websockets
import json

TOKEN = "crm_a1b2c3d4..."
BASE = "https://crm.example.com"
WS_URL = "wss://crm.example.com"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}

async def ai_respond(session, username, lead_message):
    """AI генерирует ответ и отправляет через CRM API."""
    # 1. Получить историю для контекста
    resp = await session.get(f"{BASE}/api/l1/chats/{username}/messages", headers=HEADERS)
    history = await resp.json()

    # 2. AI модель генерирует ответ (ваша логика)
    ai_reply = await your_ai_model.generate(
        history=history["messages"],
        new_message=lead_message
    )

    # 3. Отправить через CRM (CRM доставит через Telegram)
    await session.post(f"{BASE}/api/l1/chats/{username}/messages",
        headers=HEADERS,
        json={"message": ai_reply})

    # 4. Пометить прочитанным
    await session.post(f"{BASE}/api/l1/chats/{username}/mark-read",
        headers=HEADERS)

async def main():
    async with aiohttp.ClientSession() as http:
        # Забрать лидов при старте
        await http.post(f"{BASE}/api/l1/leads/request-new",
            headers=HEADERS, json={"count": 10})

        # Подключиться к WebSocket для реалтайм-событий
        async with websockets.connect(WS_URL,
                additional_headers=HEADERS) as ws:
            async for raw in ws:
                event = json.loads(raw)
                if event["type"] == "new_message":
                    data = event["data"]
                    username = data.get("chat_username", "")
                    text = data.get("text", "")
                    if username and text:
                        asyncio.create_task(
                            ai_respond(http, username, text)
                        )

asyncio.run(main())
```

---

## Полный L1 API Reference

Все эндпоинты доступны по пути `/api/l1/*` с `Authorization: Bearer <token>`.

### Лиды

| Метод | Путь | Описание |
|-------|------|----------|
| `GET` | `/api/l1/leads` | Список лидов. Query: `?status=in_progress`, `?search=username` |
| `POST` | `/api/l1/leads/request-new` | Забрать лидов из пула. Body: `{"count": 5}` |
| `POST` | `/api/l1/leads/:username/start` | Начать чат (status→in_progress, создать lead_chat) |
| `POST` | `/api/l1/leads/:username/status` | Сменить статус. Body: `{"status": "ready_for_transfer"}` |

Допустимые статусы лидов: `in_progress`, `invalid`, `ready_for_transfer`, `no_response`

### Сообщения

| Метод | Путь | Описание |
|-------|------|----------|
| `GET` | `/api/l1/chats/:chatId/messages` | История сообщений. `?sync=full` для полной синхронизации |
| `POST` | `/api/l1/chats/:chatId/messages` | Отправить текст. Body: `{"message": "..."}` |
| `POST` | `/api/l1/chats/:chatId/messages/:id/edit` | Редактировать. Body: `{"text": "..."}` |
| `DELETE` | `/api/l1/chats/:chatId/messages/:id` | Удалить сообщение |
| `POST` | `/api/l1/chats/:chatId/media` | Отправить медиа (multipart/form-data, field `media`, kind: voice/video_note/photo) |

### Чаты

| Метод | Путь | Описание |
|-------|------|----------|
| `GET` | `/api/l1/chats` | Список чатов с unread_count |
| `POST` | `/api/l1/chats/:chatId/mark-read` | Пометить прочитанным |
| `POST` | `/api/l1/chats/:chatId/sync` | Синхронизировать с Telegram |
| `GET` | `/api/l1/chats/unread-count` | Общее кол-во непрочитанных |
| `GET` | `/api/l1/active-chats` | Активные чаты. `?sync=1` для синхронизации |

### Заметки по лиду

| Метод | Путь | Описание |
|-------|------|----------|
| `GET` | `/api/l1/chats/:chatId/note` | Получить заметку |
| `POST` | `/api/l1/chats/:chatId/note` | Сохранить заметку. Body: `{"shortNote": "...", "longNote": "..."}` |

### TG-аккаунты

| Метод | Путь | Описание |
|-------|------|----------|
| `GET` | `/api/l1/assigned-account` | Назначенные TG-аккаунты |
| `POST` | `/api/l1/set-primary-account` | Установить основной аккаунт |
| `POST` | `/api/l1/chats/:chatId/switch-account` | Переключить чат на другой TG-аккаунт |
| `POST` | `/api/l1/check-spam` | Проверка спам-блока через @SpamBot |
| `POST` | `/api/l1/chats/:chatId/check-block` | Проверка блокировки от лида |
| `POST` | `/api/l1/chats/:chatId/unblock` | Разблокировать чат |

### Прочее

| Метод | Путь | Описание |
|-------|------|----------|
| `POST` | `/api/l1/chats/:chatId/suggest` | AI-подсказка ответа (OpenAI) |
| `GET` | `/api/l1/chats/:chatId/export` | Экспорт истории (`?format=txt` или `json`) |
| `GET` | `/api/l1/stats` | Статистика L1 (`?period=today` / `?from=&to=`) |
| `GET` | `/api/l1/preferences` | Настройки (gender_filter, max_concurrent_leads) |
| `POST` | `/api/l1/preferences` | Обновить настройки |
| `GET` | `/api/l1/my-persona` | Персона L1 (bio, tone, goals) |
| `GET` | `/api/l1/notification-details` | Данные для нотификаций |
| `GET` | `/api/l1/account-logs` | Логи событий TG-аккаунта |

---

## Типичный рабочий цикл AI L1

```
1. POST /api/l1/leads/request-new     → забрать N лидов из пула
2. POST /api/l1/leads/:user/start     → начать чат (status → in_progress)
3. Подключиться к WebSocket           → слушать new_message
4. На каждое new_message:
   a. GET  /api/l1/chats/:user/messages → получить контекст
   b. AI модель генерирует ответ
   c. POST /api/l1/chats/:user/messages → отправить ответ
   d. POST /api/l1/chats/:user/mark-read → пометить прочитанным
5. Когда лид готов:
   POST /api/l1/leads/:user/status     → ready_for_transfer
6. Когда лид не отвечает:
   POST /api/l1/leads/:user/status     → no_response
```

---

## Ошибки

| HTTP Code | error | Описание |
|-----------|-------|----------|
| 401 | `invalid_token` | Токен невалидный, истёк или отозван |
| 401 | `auth_required` | Нет токена и нет сессии |
| 403 | `forbidden` | Роль пользователя не совпадает с требуемой |
| 400 | `invalid_lead_status` | Недопустимый статус лида |
| 400 | `lead_status_locked` | Лид в финальном статусе (approved/transferred) |
| 429 | `too_many_requests` | Rate limit (только для /auth/login) |
| 500 | `internal_error` | Внутренняя ошибка сервера |

---

## FAQ

### Как проверить что токен работает?
```bash
curl -s https://crm.example.com/api/l1/chats \
  -H "Authorization: Bearer crm_a1b2c3d4..." | jq .
```
Если вернётся `{"chats": [...]}` — токен валиден.

### Сколько запросов в минуту можно делать?
Нет rate limit на API-token запросы (rate limit стоит только на `/auth/login`).
Рекомендуемый максимум: 60 req/min на polling, без ограничений на WS-push.

### Можно ли создать несколько токенов для одного юзера?
Да. Каждый токен отзывается независимо.

### Что если токен скомпрометирован?
```bash
curl -X DELETE https://crm.example.com/api/admin/api-tokens/1 \
  -H "Cookie: crm_session=<admin>"
```
Токен перестаёт работать мгновенно (кэш инвалидируется).

### Работает ли отправка сообщений через API?
Да. `POST /api/l1/chats/:chatId/messages` создаёт задачу в очереди,
Telethon-демон CRM отправляет через Telegram. Всё как в браузере.

### Может ли один AI управлять несколькими L1 аккаунтами?
Да — создать несколько пользователей L1, каждому свой токен.
Или создать одного L1 с несколькими TG-аккаунтами.
