# modules/ui_web — Web UI

Веб-интерфейс системы (read-only панель наблюдения) поверх существующего FastAPI.

- Stack: **Vite + React 19 + TypeScript + Tailwind CSS + shadcn/ui** (new-york style, lucide icons).
- Data: **@tanstack/react-query** + native `EventSource` (SSE) для живого потока.
- Routing: **react-router-dom v7**.
- Тема: тёмная по умолчанию (переключается из шапки). Цвета модулей/статусов — CSS-переменные, смотри `src/index.css`.

Модуль изолирован от Python-кода — тут отдельный `package.json`, собственный Dockerfile и собственный сервис в `docker-compose.yml`. Падение UI не влияет на ядро, ровно как и описано в `docs/ui/web_ui_overview_v1.md`.

---

## Структура

```
modules/ui_web/
├── Dockerfile              ← node:22-alpine, npm run dev на :3000
├── package.json
├── vite.config.ts          ← прокси /accounts /dialogs /messages /events /media /system → FastAPI
├── tailwind.config.ts      ← палитра модулей/статусов
├── tsconfig.json / tsconfig.node.json
├── postcss.config.js
├── components.json         ← конфиг shadcn/ui CLI
├── index.html
├── .env.example            ← VITE_API_URL
└── src/
    ├── main.tsx
    ├── App.tsx             ← маршруты /dialogs, /events
    ├── index.css           ← дизайн-токены (CSS vars)
    ├── lib/
    │   ├── api.ts          ← типизированный клиент (см. web_ui_api_contract_v1)
    │   ├── tokens.ts       ← мэппинг module/status → tailwind токены
    │   └── utils.ts        ← cn() для shadcn
    ├── types/api.ts        ← Account, DialogSummary, Message, BusEvent, ...
    ├── components/
    │   ├── ui/             ← shadcn-примитивы: button, card, badge, input,
    │   │                     scroll-area, select, dialog
    │   ├── common/         ← StatusDot, ModulePill, ErrorBox
    │   ├── layout/         ← AppShell
    │   ├── dialogs/        ← AccountCard, DialogListItem, MessageBubble,
    │   │                     MediaTag, AsyncBlock (transcription/description)
    │   └── events/         ← EventRow, EventFilters, MetricsBar, EventDetailDialog
    ├── hooks/
    │   ├── useAccounts.ts  ← accounts / dialogs / messages / dialog profile
    │   └── useEvents.ts    ← архив + SSE-подписка
    └── pages/
        ├── DialogsPage.tsx
        └── EventLogPage.tsx
```

---

## Запуск на localhost

### Вариант A — через Docker (рекомендуется, ничего ставить локально не надо)

Сначала убедись что есть основной backend:

```powershell
docker compose up -d app postgres redis minio
```

Потом поднимаем UI-сервис (добавлен в корневой `docker-compose.yml`):

```powershell
docker compose up -d ui
```

Открывай http://localhost:3000 — дев-сервер Vite с HMR. Он проксирует все API-запросы на `http://app:8000` внутри docker-сети.

### Вариант B — локально (если есть Node 20+)

```powershell
cd modules/ui_web
copy .env.example .env
# при необходимости поправь VITE_API_URL (по умолчанию http://localhost:8000)
npm install
npm run dev
```

Откроется http://localhost:3000.

Бэкенд в `.env` корня проекта уже содержит `CORS_ORIGINS=http://localhost:3000`, так что запросы из браузера работать будут и без прокси — но Vite всё равно проксирует, чтобы в коде UI не было хардкода хоста API.

---

## Контракт с backend

Все ручки — строго по `docs/ui/web_ui_api_contract_v1.md`. Ничего в БД/Redis/MinIO напрямую — только через API.

| UI использует | Backend endpoint |
|---|---|
| AccountCard | `GET /accounts` |
| Список диалогов | `GET /accounts/{id}/dialogs` |
| Профиль собеседника | `GET /dialogs/{id}` |
| Сообщения | `GET /dialogs/{id}/messages` |
| Превью медиа | `GET /media/{id}/preview` |
| История правок | `GET /messages/{id}/edits` |
| Архив событий | `GET /events?...` |
| Метрики | `GET /events/stats?...` |
| Живой поток | `GET /events/stream?...` (SSE) |
| Детали + цепочка | `GET /events/{id}/chain` |
| Экспорт | `GET /events/export?format=csv|json` |

Пока backend не реализовал эти ручки — UI будет показывать `ErrorBox` в соответствующих секциях. Дизайн не ломается, остальные части работают.

---

## Добавить shadcn-компонент (когда понадобится)

```powershell
cd modules/ui_web
npx shadcn@latest add dropdown-menu
```

CLI берёт настройки из `components.json` (уже настроен: style new-york, tailwind v3, alias `@/components/ui`).

---

## Как это вписывается в остальную систему

Архитектура модулей этого проекта — каждый модуль автономен и общается с остальными только через шину событий/API. `ui_web` следует тому же принципу:

- **не знает** о схеме БД, Redis-ключах, MinIO-бакетах
- **читает** только публичный API (с проксированием media)
- **не пишет** ничего кроме команд start/stop воркеру
- **падение** UI не трогает воркеры и шину — они в отдельных процессах/контейнерах
- **падение** API → UI показывает ErrorBox и не валится

Пункты зафиксированы в `docs/ui/web_ui_overview_v1.md` раздел «Принципы».
