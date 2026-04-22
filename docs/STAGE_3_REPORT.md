# Отчёт по Этапу 3

> Проект: `finish-outrich` — Telegram Automation Framework
> Репо: https://github.com/Aqua7MarcusAurelius/finish-outrich
> Финальные коммиты сессии:
>   - `890b8ac` — Stage 3.2: auth + /system/proxy-check
>   - <hash 3.3>  — Stage 3.3: worker manager + worker lifecycle
> Сессия: ~3 часа, 21 апреля 2026

---

## Цель этапа

Подключить живой Telegram-аккаунт, поднять воркер, убедиться что
при получении сообщения в шине проезжает `message.received`.

**Цель достигнута полностью.**

---

## Что сделали

Этап разбит на три шага, каждый коммитился отдельно после sanity-проверок.

### Этап 3.1 — Враппер Telegram

**Цель:** единая точка общения с Telegram через Telethon.

**Написаны:**

- `modules/worker/__init__.py` + `modules/worker/wrapper.py` — класс `TelegramWrapper`:
  - Команды: `send_message`, `read_message`, `get_dialogs`, `get_history`, `on_new_message`
  - Управление прокси: primary → fallback → `ProxyUnavailable` + публикация `system.error`
  - Определение протухшей сессии (`AuthKeyError`, `SessionExpiredError`, `SessionRevokedError`, `UserDeactivatedError`, `UserDeactivatedBanError`, `AuthKeyUnregisteredError`) → публикация `account.session_expired` + `SessionExpired`
  - Сессия через `StringSession`, сериализация в/из `bytea` поля `accounts.session_data`
  - Подписка на входящие/исходящие через `add_event_handler(NewMessage)`
  - Хелпер `serialize_message(m)` — минимальный снимок сообщения для публикации в шину
- `core/config.py` — +2 поля: `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`
- `requirements.txt` — +`telethon==1.36.0`, +`pysocks==1.7.1`
- `.env.example` — +2 ключа под Telegram API

**Проверки:**
- `docker compose up -d --build` — контейнеры поднялись, `tgf_app` в `Up`
- `/system/health` всё ещё `ok` по всем компонентам
- `from modules.worker.wrapper import TelegramWrapper, SessionExpired, ProxyUnavailable` — импорт без ошибок

---

### Этап 3.2 — Модуль авторизации + `/system/proxy-check`

**Цель:** многошаговая авторизация нового аккаунта через API.

**Написаны:**

- `core/proxy.py` — общий модуль работы с SOCKS5:
  - `parse_socks5(url)` — в кортеж для Telethon
  - `mask(url)` — маскировка user:pass для логов
  - `check_socks5(url)` — асинхронная TCP-проверка через `asyncio.to_thread`, цель — `1.1.1.1:443`
- `modules/auth/service.py` — класс `AuthService`:
  - `start` / `submit_code` / `submit_password` / `get_status` / `cancel` / `start_reauth`
  - State в Redis (`auth_session:{id}`, TTL 15 мин), `TelegramClient` и `phone_code_hash` — в памяти процесса
  - Предварительная проверка обоих прокси до подключения
  - Иерархия исключений `AuthError` → `PhoneInvalid` / `CodeInvalid` / `PasswordInvalid` / `ProxyCheckFailed` / `SessionExpired` и ещё 7 — маппятся в `{"error": {"code","message"}}` в роутере
  - Финализация единая для code и 2fa: `INSERT`/`UPDATE` в `accounts`, публикация `account.created` / `account.reauthorized`, очистка state
- `modules/auth/routes.py` — 6 endpoint'ов: POST `/auth/start`, POST `/auth/code`, POST `/auth/2fa`, GET `/auth/status/{id}`, DELETE `/auth/{id}`, POST `/auth/reauth`
- `api/routes/system.py` — **новый файл**, вынесены `/system/*` из `api/main.py`. Добавлен POST `/system/proxy-check` с форматом `{"proxy": "..."}` или `{"proxies": [...]}`
- `api/main.py` — `AuthService` инстанциируется в lifespan (`app.state.auth_service`), регистрация роутеров `system`/`auth`, версия `0.3.0`
- `modules/worker/wrapper.py` — перевод на `core/proxy` вместо локальных `_parse_socks5`/`mask_proxy`

**Проверки:**
- `/system/health` ок, `auth_module: ok`
- Импорт `AuthService`, `auth routes`, `check_socks5` — без ошибок
- `/system/proxy-check` на битом `socks5://127.0.0.1:1` → `ok: False`, `error: Connection refused`

---

### Этап 3.3 — Менеджер воркеров + жизненный цикл воркера

**Цель:** запускать/останавливать/удалять воркеры через API, слушать входящие сообщения.

**Написаны:**

- `modules/worker/worker.py` — класс `Worker`:
  - `run()` — поднимает враппер, регистрирует `on_new_message`, публикует `worker.started`, блокируется на `_stop_event.wait()`
  - `stop()` — ставит `_stop_event`, дожидается inflight-хендлеров через `_inflight_zero` (таймаут 10 сек), делает `disconnect`
  - `_on_new_message` — `serialize_message` + добавляется `telegram_user_id` из `msg.peer_id.user_id`, публикует `message.received` на шину
- `modules/worker_manager/service.py` — класс `WorkerManager`:
  - `list_workers`, `start`, `stop`, `delete`, `shutdown`
  - Статусы в Redis (`worker:{id}:status`) — `starting`/`running`/`stopping`/`stopped`/`crashed`/`session_expired`
  - Redis pubsub-канал `worker_updates` — публикация при каждом изменении статуса, для SSE
  - Один авторестарт при падении (через `restart_count` в `_Slot`); повторное падение → `crashed`, больше не трогаем
  - `stop` синхронно ждёт завершения таска с таймаутом 30 сек, потом cancel
  - `delete` — порядок важен: `stop` → сбор `storage_key` из БД → удаление файлов из MinIO → `DELETE FROM accounts` (каскад) → удаление статуса из Redis → `account.deleted`
  - Иерархия исключений `ManagerError` → `AccountNotFound` / `AccountInactive` / `AlreadyRunning` / `NotRunning` / `ConfirmationRequired`
- `modules/worker_manager/routes.py` — 4 endpoint'а:
  - GET `/workers` — список из БД с обогащением статусами из Redis
  - POST `/workers/{id}/start` / POST `/workers/{id}/stop`
  - GET `/workers/stream` — SSE через Redis pubsub, в стиле `/events/stream` (heartbeat каждые 30 сек тишины, формат через `api.sse.sse_format`)
  - DELETE `/accounts/{id}` — требует заголовок `X-Confirm-Delete: yes`
- `core/minio.py` — +`remove_object(key)`, +`remove_objects(keys)` (пачкой через `DeleteObject`)
- `api/main.py` — `WorkerManager` в lifespan, регистрация `/workers/*`

**Проверки после 3.3:**
- Сборка/импорт — без ошибок
- `/workers` на пустой базе — пустой массив

---

## End-to-end проверка на живом Telegram

После раскладки всех трёх шагов прогнали полный флоу.

| Шаг | Запрос | Результат |
|---|---|---|
| 1 | POST `/system/proxy-check` с двумя прокси | оба `ok: true`, latency 306 / 369 мс |
| 2 | POST `/auth/start` (`+447350135778`, оба прокси) | `session_id`, `status: code_sent` |
| 3 | POST `/auth/code` (код из Telegram) | `status: 2fa_required` (2FA включён) |
| 4 | POST `/auth/2fa` (пароль) | `status: done, account_id: 1` |
| 5 | POST `/workers/1/start` | `status: starting` |
| 6 | GET `/workers` (через 3 сек) | `status: running, uptime_seconds: 19` |
| 7 | Отправка "привет" с личного ТГ на этот аккаунт | — |
| 8 | GET `/events?type=message.received&limit=5` | событие с `text: "привет"`, `is_outgoing: false`, `telegram_user_id: 7875919809`, `telegram_message_id: 20713` |
| 9 | POST `/workers/1/stop` | `status: stopping` → через 3 сек `stopped, uptime_seconds: 0` |
| 10 | POST `/workers/1/start` (повторно) | воркер поднялся с сохранённой сессии → `running` |

Шаг 10 — ключевой: подтвердил что `StringSession.save()` корректно сериализуется в `bytea` и обратно читается через `StringSession(session_data.decode("utf-8"))`, т.е. сессия действительно переживает остановку воркера.

---

## Нюансы и мелочи сессии

### PowerShell + JSON в теле POST

Бились минут десять с форматом тела. Попытка:

```
curl.exe -X POST http://localhost:8000/system/proxy-check \
  -H "Content-Type: application/json" -d "{\"proxy\":\"...\"}"
```

возвращала `{"detail":[{"type":"json_invalid","loc":["body",1],...}]}` — PowerShell раскрывает `\"` до попадания в curl и ломает JSON. Одинарные кавычки тоже не спасли: PowerShell их всё равно обрабатывает.

**Что сработало:**
- `Invoke-RestMethod -Method POST -ContentType 'application/json' -Body '{...}'` — родной способ PowerShell, он сам пакует тело в HTTP-запрос без промежуточного парсера.
- Либо через файл: `Set-Content body.json ... ; curl.exe ... --data "@body.json"`.

Весь дальнейший тест гоняли через `Invoke-RestMethod`.

### PowerShell 5.x и оператор `&&`

`docker compose down && docker compose up -d --build` в Windows PowerShell 5.x не работает — `&&` появился только в PowerShell 7. Лечится запуском команд через `;` или разбиением на две строки. На будущее можно поставить `pwsh`.

### UTF-8 в консоли PowerShell

`Invoke-RestMethod` корректно парсит UTF-8 ответ, но по умолчанию рендерит русские строки как Windows-1252 (видно крокозябрами: `ÐÐ¸Ð²ÐµÑ` вместо `Привет`). В БД данные хранятся корректно. Косметически лечится `chcp 65001` в сессии.

### Инфра, приехавшая с Этапа 1

- `docker compose down && up --build` после изменений в `requirements.txt` обязателен (новые зависимости не установятся при `restart app`). Для чисто кодовых правок достаточно `restart app`.
- Контейнер сервиса называется `tgf_app`, имя сервиса в compose — `app`. В документации Этапов 1–2 где-то проскочило `api` — в Этапе 3 явно используем `app`.

---

## Что в итоге на GitHub

Структура репо после Этапа 3 (добавленное относительно `a0aedca`):

```
finish-outrich/
├── core/
│   └── proxy.py                                 — parse/mask/check SOCKS5
├── api/
│   ├── main.py                                  — обновлён: AuthService + WorkerManager в lifespan
│   └── routes/
│       └── system.py                            — вынесен из main.py, +/system/proxy-check
├── modules/
│   ├── auth/
│   │   ├── __init__.py
│   │   ├── service.py                           — AuthService
│   │   └── routes.py                            — 6 endpoints /auth/*
│   ├── worker/
│   │   ├── __init__.py
│   │   ├── wrapper.py                           — TelegramWrapper
│   │   └── worker.py                            — Worker (жизненный цикл)
│   └── worker_manager/
│       ├── __init__.py
│       ├── service.py                           — WorkerManager
│       └── routes.py                            — 4 endpoints /workers/*
└── core/minio.py                                — +remove_object, +remove_objects
```

**Версия приложения:** `0.3.0`
**Endpoint'ов работает:** 17 (было 6):
- `/system/health`, `/system/stats`, `/system/proxy-check`, `/system/_debug/emit-event` (4)
- `/events`, `/events/{id}`, `/events/stream` (3)
- `/auth/start`, `/auth/code`, `/auth/2fa`, `/auth/status/{id}`, `/auth/{id}`, `/auth/reauth` (6)
- `/workers`, `/workers/{id}/start`, `/workers/{id}/stop`, `/workers/stream`, `/accounts/{id}` (DELETE) (4 новых + 1 удаление аккаунта)

**Endpoint'ов от финального MVP:** ~52% (17 из 33)

---

## Что дальше — План на Этап 4

**Цель:** модуль истории. Тот самый потребитель `message.received`, который мы сейчас лишь публикуем в шину но не обрабатываем. Плюс запись медиа в MinIO и чистильщик.

**Модули к написанию:**

1. `modules/history/service.py` — подписчик на `message.received`:
   - Создаёт/обновляет запись в `dialogs` (собеседник)
   - Пишет в `messages`, при наличии медиа — скачивает из Telegram и кладёт в MinIO, запись в `media`
   - Публикует `message.saved` с флагами `has_audio` / `has_image` / ... — на это событие потом подпишутся медиа-модули
2. `modules/history/cleaner.py` — раз в час, пачкой до 50, удаляет файлы старше 3 дней из MinIO, публикует `file.cleaned`. Записи в БД не трогает.
3. `modules/history/routes.py` — endpoints `/accounts/{id}/dialogs`, `/dialogs/{id}`, `/dialogs/{id}/messages`, `/dialogs/{id}/stream` (SSE), `/messages/{id}`, `/dialogs/{id}/read`, POST `/accounts/{id}/messages` (отправка через враппер).
4. `modules/history_sync/service.py` — нагон истории при старте воркера: `sync.started` → `sync.dialog.done` (много раз) → `sync.done`. Этот модуль пойдёт первой интеграцией с `Worker.run()` — воркер на старте должен его дёрнуть.

Будет одна миграция — скорее всего добавление статусов транскрипции/описания в `media` (если на Этапе 1 их не поставили).

**Что нужно для теста:** тот же аккаунт, отправить на него сообщения разных типов — текст, голосовое, фото, видео, документ, пересланное.

**Оценка времени:** 5-7 часов. Самый большой модуль по объёму кода за всю систему, но без новых инфраструктурных сложностей.

---

## Формат старта Этапа 4

Открыть новый чат (этот уже накопил много истории), прикрепить документацию:
- минимум: `architecture.md`, `api.md`, `event_bus.md`, `database_schema.md`, `history.md`, `history_sync.md`, `transcription.md`, `media_description.md`, а также `STAGES_1_2_REPORT.md` и `STAGE_3_REPORT.md`.

Начать примерно так:

> Продолжаем проект `finish-outrich`. Этапы 1-3 закрыты (инфра, шина, telethon-враппер, авторизация, менеджер воркеров). Репо https://github.com/Aqua7MarcusAurelius/finish-outrich. Поехали к Этапу 4 — модуль истории (запись сообщений, чистильщик, endpoints /dialogs/*) и модуль нагона. Аккаунт и прокси уже настроены, используем тот же.

---

*Сгенерировано в финале сессии 21.04.2026.*
