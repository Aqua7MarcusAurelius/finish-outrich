# CRM API Token — v2 расширение (лид-менеджмент для AI L1)

> Дополнение к базовому руководству `08_CRM_API_TOKEN_GUIDE.md`.
> Формат: **old / new** — что было и что добавилось. ТЗ для интеграторов бота.
> Дата: 2026-07-18 · обновлено 2026-07-21 · Статус: **реализовано** (v2 в проде, под текущими токенами).

---

## 0. Главное для интеграции

- **Токены менять не нужно.** Работают текущие бот-токены (роль `l1`, scope `["l1"]`).
- После обновления CRM **новые эндпоинты просто появляются** под тем же `Authorization: Bearer crm_...`.
- Всё, что описано в базовом руководстве (`/api/l1/*`, WebSocket, переписка, sync), **работает как раньше** — v2 только добавляет возможности, ничего не убирает и не ломает.
- Действия бота логируются от имени его пользователя (`ai-l1-bot`) в аудите — как и прежде.

---

## 1. Что меняется (old → new)

| # | Возможность | OLD (сейчас) | NEW (v2) |
|---|-------------|--------------|----------|
| 1 | Видеть доступные лиды | только `request-new` — берёт «вслепую», без предпросмотра | **`GET /api/l1/available-leads`** — счётчик + список доступных до взятия |
| 2 | Статусы лида | `in_progress`, `invalid`, `ready_for_transfer`, `no_response` | **+ `approved`** через `/status`; `transferred` — через `assign-l2` |
| 3 | Передача на L2 | нет | **`GET /api/l1/l2-managers`** (список L2) + **`POST /api/l1/leads/:username/assign-l2`** — назначить по логину |
| 4 | Правка карточки лида | только заметки (`/note`) | **`PATCH /api/l1/leads/:username/card`** — поля карточки (пол, гео, источник, ссылка, комментарий, имя, телефоны) |

Идентичность/аудит: без изменений — бот уже отдельный пользователь `ai-l1-bot`, действия атрибутируются ему.

---

## 2. NEW — Reference

Все эндпоинты — по `/api/l1/*`, с `Authorization: Bearer <token>`, как в базовом руководстве.

### 2.1 Доступные лиды (предпросмотр пула)

| Метод | Путь | Описание |
|-------|------|----------|
| `GET` | `/api/l1/available-leads` | Сколько лидов доступно к взятию + предпросмотр. Не назначает. |

**Query:** `?gender=male|female` · `?geo=cis|europe|asia|other` · `?limit=50` (по умолчанию — фильтры из настроек бота, как в `request-new`).

**Ответ:**
```json
{
  "available_count": 137,
  "returned": 50,
  "leads": [
    { "username": "petrov_ivan", "gender": "male", "geo": "cis", "platform": "telegram" }
  ]
}
```
- `available_count` — всего свободных лидов под фильтр (весь пул).
- `leads` — предпросмотр (до `limit`), без назначения. Чтобы взять — `request-new` как раньше.

### 2.2 Смена статуса (+ approved)

| Метод | Путь | Описание |
|-------|------|----------|
| `POST` | `/api/l1/leads/:username/status` | Сменить статус. **Body:** `{"status": "approved"}` |

**Допустимые статусы (v2):** `in_progress`, `invalid`, `ready_for_transfer`, `no_response`, **`approved`**.
- `approved` — ранее был только у супервайзера; теперь доступен боту (финализация/засчёт).
- **`transferred` ставится не здесь, а через `assign-l2`** (§2.3) — вместе с назначением владельца-L2.
- Нельзя менять статус лида, уже находящегося в финале (`approved`/`transferred`/`no_response`).

### 2.3 Список L2 и передача лида на L2

Бот сначала узнаёт доступных L2, затем назначает лида **по логину**.

| Метод | Путь | Описание |
|-------|------|----------|
| `GET` | `/api/l1/l2-managers` | Список активных L2 (логин + имя) — цели для назначения |
| `POST` | `/api/l1/leads/:username/assign-l2` | Передать лида L2. **Body:** `{"l2_login": "l2-07"}` |

**`GET /api/l1/l2-managers` — ответ:**
```json
{ "managers": [
  { "login": "l2-07", "name": "Ivan" },
  { "login": "l2-08", "name": "Petro" }
] }
```

**`POST …/assign-l2` — ответ:**
```json
{ "status": "transferred", "username": "petrov_ivan", "l2_login": "l2-07" }
```
- Бот берёт логин из `GET /api/l1/l2-managers` и передаёт в назначении.
- Сервер: назначает лида на этого L2 + переводит в `transferred` (уходит в воронку L2).

### 2.4 Правка карточки лида

| Метод | Путь | Описание |
|-------|------|----------|
| `PATCH` | `/api/l1/leads/:username/card` | Обновить поля карточки. Присылать только меняемые. |

**Body (все поля опциональны):**
```json
{
  "gender": "female",
  "geo": "europe",
  "platform": "telegram",
  "source_url": "https://...",
  "comment": "заметка",
  "first_name": "Ivan",
  "last_name": "Petrov",
  "phones": ["+79180407634"]
}
```
- Полный доступ к полям карточки (при необходимости часть отсечём позже).
- Валидация значений — как в CRM (гео/платформа из допустимых наборов, телефоны нормализуются).

---

## 3. Обновлённый рабочий цикл AI L1

```
0. GET  /api/l1/available-leads        → NEW: посмотреть, сколько доступно (счётчик)
1. POST /api/l1/leads/request-new      → забрать N лидов из пула (как раньше)
2. POST /api/l1/leads/:user/start      → начать чат (in_progress)
3. WebSocket new_message               → вести переписку (как раньше)
4. PATCH /api/l1/leads/:user/card      → NEW: уточнить карточку (гео/пол/коммент…)
5. POST /api/l1/leads/:user/status     → довести статус (в т.ч. NEW: approved)
6. GET  /api/l1/l2-managers            → NEW: узнать доступных L2
7. POST /api/l1/leads/:user/assign-l2  → NEW: передать по логину
```

Шаги 1–3 — без изменений из базового руководства; **0, 4, 5(+), 6, 7 — новое в v2**.

---

## 4. Python — примеры (v2)

```python
import requests

TOKEN = "crm_..."            # существующий бот-токен, менять не нужно
BASE  = "https://crm.example.com"
H     = {"Authorization": f"Bearer {TOKEN}"}

# ── NEW: сколько доступно к взятию ──
avail = requests.get(f"{BASE}/api/l1/available-leads",
    headers=H, params={"limit": 50}).json()
print(f"Доступно: {avail['available_count']}")

# ── NEW: полный статус ──
requests.post(f"{BASE}/api/l1/leads/petrov_ivan/status",
    headers=H, json={"status": "approved"})

# ── NEW: узнать доступных L2 и назначить по логину ──
managers = requests.get(f"{BASE}/api/l1/l2-managers", headers=H).json()["managers"]
requests.post(f"{BASE}/api/l1/leads/petrov_ivan/assign-l2",
    headers=H, json={"l2_login": "l2-07"})

# ── NEW: правка карточки ──
requests.patch(f"{BASE}/api/l1/leads/petrov_ivan/card",
    headers=H, json={"geo": "europe", "comment": "перспективный"})
```

---

## 5. Ошибки (дополнение к базовым)

| HTTP | error | Описание |
|------|-------|----------|
| 400 | `invalid_lead_status` | Статус вне допустимого набора |
| 400 | `l2_login_required` | Не передан `l2_login` в `assign-l2` |
| 400 | `l2_not_found` | Указанный L2 (логин) не найден / не активен |
| 400 | `invalid_geo` / `invalid_platform` / `invalid_gender` | Значение поля карточки вне допустимых |
| 400 | `invalid_phones` | `phones` не является массивом |
| 400 | `no_fields` | В `PATCH /card` не передано ни одного поля |
| 403 | `forbidden` | Возможность недоступна для этого типа запроса (нужен бот-токен) |
| 404 | `lead_not_found` | Лид не найден / не принадлежит боту |

---

## 6. Заметки

- **Аудит:** каждое действие пишется от `ai-l1-bot` (уже так). Расширенные операции (approved/transferred, assign-l2, правка карточки) отдельно фиксируются в логе токена.
- **Guard-rails:** жёстких лимитов на API-токен нет (как и раньше). Рекомендация — не превышать разумный темп; это часть нашего софта, агрессивно не зажимаем.
- **Совместимость:** старый поток бота продолжает работать без правок; v2 включается автоматически после обновления CRM.

---

*Решения зафиксированы:* `assign-l2` — по `l2_login` (+ `GET /api/l1/l2-managers` для получения логинов); карточка — **полный доступ** к полям.
