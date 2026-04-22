# Структура базы данных

> Все таблицы PostgreSQL в одном месте. Схема управляется через Alembic-миграции.
> SQL пишем руками через asyncpg — ORM не используем.

---

## Обзор таблиц

| Таблица | Назначение | Владеющий модуль |
|---|---|---|
| `accounts` | Наши Telegram-аккаунты, сессии, прокси | Авторизация (создание), история (чтение) |
| `dialogs` | Пары "мы ↔ собеседник" с данными о собеседнике | История |
| `messages` | Сообщения | История |
| `media` | Вложения (файл в MinIO, транскрипция, описание) | История |
| `reactions` | Реакции на сообщения | История |
| `message_edits` | Предыдущие версии текста отредактированных сообщений | История |
| `settings` | Настройки поведения модулей (меняются без перезапуска) | API |
| `events_archive` | Архив всех событий шины для фильтрации и экспорта | API |
| `autochat_sessions` | Инициированные нами автодиалоги через Opus 4.7 | AutoChat |

Принцип: **в каждую таблицу пишет только один модуль — тот что ей владеет.** Остальные модули либо читают, либо получают данные через шину.

---

## Связи таблиц

```
accounts
    │
    ▼
dialogs
    │
    ▼
messages
    │   │
    │   ├──▶ media           ← вложения (storage_key ведёт в MinIO)
    │   ├──▶ reactions       ← реакции
    │   └──▶ message_edits   ← предыдущие версии текста

settings           ← независимая, без связей
events_archive     ← независимая, без связей
autochat_sessions  ← account_id → accounts, dialog_id → dialogs
```

---

## Таблица `accounts` — наши Telegram-аккаунты

Одна строка = один наш Telegram-аккаунт. Создаётся модулем авторизации.

| Поле | Тип | Что тут лежит |
|---|---|---|
| `id` | int | PK |
| `name` | string | удобное название для различения — например "Аккаунт для прогрева" |
| `phone` | string | номер телефона |
| `session_data` | bytea | содержимое сессии Telethon в байтах |
| `proxy_primary` | string | основной прокси в формате `socks5://user:pass@host:port` |
| `proxy_fallback` | string | запасной прокси в том же формате |
| `is_active` | bool | активен ли аккаунт |
| `created_at` | timestamp | когда добавили |
| `updated_at` | timestamp | когда обновляли |

> Статус воркера (running / stopped / crashed) — только в Redis, не здесь. Это оперативные данные.

---

## Таблица `dialogs` — с кем мы общаемся

Одна строка = один собеседник одного нашего аккаунта.

| Поле | Тип | Что тут лежит |
|---|---|---|
| `id` | int | PK |
| `account_id` | int | FK → accounts |
| `telegram_user_id` | bigint | tg id собеседника |
| `type` | string | всегда `private` — так называется личный диалог в Telegram. Поле оставлено на будущее |
| `username` | string | @handle собеседника (nullable) |
| `first_name` | string | имя из публичного профиля |
| `last_name` | string | фамилия из публичного профиля (nullable) |
| `phone` | string | номер телефона (nullable) |
| `birthday` | date | день рождения (nullable) |
| `bio` | text | "о себе" из профиля (nullable) |
| `is_contact` | bool | добавлен ли в наши контакты |
| `contact_first_name` | string | как сохранили у себя в контактах |
| `contact_last_name` | string | фамилия из нашей адресной книги |
| `is_bot` | bool | бот или человек |
| `created_at` | timestamp | когда завели запись |
| `updated_at` | timestamp | когда последний раз обновляли |

**Уникальность:** `(account_id, telegram_user_id)`.

---

## Таблица `messages` — сообщения

Одно сообщение Telegram = одна строка.

| Поле | Тип | Что тут лежит |
|---|---|---|
| `id` | int | PK |
| `dialog_id` | int | FK → dialogs |
| `telegram_message_id` | bigint | ID сообщения в Telegram |
| `is_outgoing` | bool | **true = мы отправили, false = собеседник** |
| `type` | string | `regular` / `service` |
| `date` | timestamp | когда отправлено в Telegram |
| `text` | text | текст сообщения (nullable) |
| `reply_to_message_id` | int | FK → messages (nullable) |
| `forward_from_user_id` | bigint | tg id пересыльщика (не FK) |
| `forward_from_username` | string | @handle пересыльщика на момент пересылки |
| `forward_from_name` | string | имя пересыльщика на момент пересылки |
| `forward_from_chat_id` | bigint | tg id канала если пересылка из канала |
| `forward_date` | timestamp | когда исходное сообщение было отправлено |
| `media_group_id` | bigint | если сообщение часть альбома |
| `edited_at` | timestamp | когда отредактировано (nullable) |
| `deleted_at` | timestamp | soft delete (nullable) |

**Уникальность:** `(dialog_id, telegram_message_id)`.

Данные пересыльщика хранятся денормализованно — прямо в `messages` как снимок на момент получения. Это осознанный компромисс: не нужна отдельная таблица для людей которых мы видели только в пересылках, но данные не обновятся если автор потом сменит имя. Для архива это как раз и нужно.

---

## Таблица `media` — вложения

| Поле | Тип | Что тут лежит |
|---|---|---|
| `id` | int | PK |
| `message_id` | int | FK → messages |
| `type` | string | photo / video / audio / voice / video_note / sticker / gif / document |
| `file_name` | string | оригинальное имя файла (nullable) |
| `telegram_file_id` | string | ссылка внутри Telegram |
| `storage_key` | string | ключ файла в MinIO (nullable после удаления) |
| `mime_type` | string | тип содержимого |
| `file_size` | int | размер в байтах |
| `duration` | int | для аудио/видео, в секундах |
| `width` | int | для фото/видео |
| `height` | int | для фото/видео |
| `transcription` | text | текст из аудио (храним как есть, не обрезаем) |
| `transcription_status` | string | none / pending / done / failed |
| `description` | text | описание изображения/документа (храним как есть, не обрезаем) |
| `description_status` | string | none / pending / done / failed |
| `downloaded_at` | timestamp | когда файл появился в MinIO |
| `file_deleted_at` | timestamp | когда файл удалён из MinIO (nullable) |

Файл в MinIO живёт 3 дня, метаданные и тексты (transcription/description) — навсегда.

---

## Таблица `reactions` — реакции

| Поле | Тип | Что тут лежит |
|---|---|---|
| `id` | int | PK |
| `message_id` | int | FK → messages |
| `is_outgoing` | bool | true = наша реакция, false = собеседника |
| `emoji` | string | стандартный эмодзи (nullable) |
| `custom_emoji_id` | string | ID кастомного эмодзи (nullable) |
| `created_at` | timestamp | когда поставили |
| `removed_at` | timestamp | когда сняли (nullable) |

Ровно одно из `emoji` / `custom_emoji_id` заполнено.

---

## Таблица `message_edits` — история правок

| Поле | Тип | Что тут лежит |
|---|---|---|
| `id` | int | PK |
| `message_id` | int | FK → messages |
| `old_text` | text | предыдущая версия текста |
| `edited_at` | timestamp | когда эта версия была заменена |

При каждом редактировании — новый текст в `messages.text`, старый — сюда.

---

## Таблица `settings` — настройки поведения

Настройки которые можно менять без перезапуска системы. Значения по умолчанию создаются первой миграцией.

| Поле | Тип | Что тут лежит |
|---|---|---|
| `key` | string | уникальный ключ настройки |
| `value` | string | значение |
| `description` | text | что это за настройка |
| `updated_at` | timestamp | когда последний раз меняли |

Дефолтные значения и семантика ключей — в `configuration.md`.

---

## Таблица `events_archive` — архив событий шины

Все события шины сохраняются сюда для фильтрации, экспорта и расследований. Живые события при этом продолжают летать через Redis Streams — архив пишется параллельно.

| Поле | Тип | Что тут лежит |
|---|---|---|
| `id` | string | уникальный ID события (из шины) |
| `parent_id` | string | родительское событие (nullable) |
| `time` | timestamp | когда событие опубликовано |
| `account_id` | int | FK → accounts (nullable для системных) |
| `module` | string | кто опубликовал — history / transcription / worker / ... |
| `type` | string | тип события — message.received / transcription.done / system.error / ... |
| `status` | string | success / error / in_progress |
| `data` | jsonb | payload события как есть |

Индексы для типовых запросов:

| Запрос | Индекс |
|---|---|
| Лента событий по времени | `(time DESC)` |
| События аккаунта | `(account_id, time DESC)` |
| События модуля | `(module, time DESC)` |
| События определённого типа | `(type, time DESC)` |
| Цепочка событий | `(parent_id)` |
| Только ошибки | `(status, time DESC)` где `status='error'` |

Поле `message` (готовая фраза для отображения в UI) здесь **не хранится** — вычисляется в API при отдаче из справочника шаблонов. Это позволяет менять формулировки без миграций.

---

## Таблица `autochat_sessions` — автодиалоги

Инициированные нами переписки через модуль AutoChat. Одна строка = одна сессия (один воркер ↔ один собеседник). Подробнее — в `autochat.md`.

| Поле | Тип | Что тут лежит |
|---|---|---|
| `id` | int | PK |
| `account_id` | int | FK → accounts (воркер, от которого пишем) |
| `dialog_id` | int | FK → dialogs (nullable — до первого `message.saved` может отсутствовать) |
| `telegram_user_id` | bigint | целевой tg id (кэш после `resolve_username`) |
| `target_username` | string | @username как был задан в запросе |
| `system_prompt` | text | системный промт (персонаж, правила сегментации) |
| `initial_prompt` | text | промт для генерации первого сообщения |
| `initial_sent_text` | text | что реально отправили первым |
| `status` | string | `starting` / `active` / `paused` / `failed` / `stopped` |
| `in_chat` | bool | текущее состояние InChat (дубликат для восстановления при рестарте) |
| `last_our_activity_at` | timestamp | время последнего нашего сообщения |
| `last_their_message_at` | timestamp | время последнего сообщения собеседника |
| `last_any_message_at` | timestamp | max двух выше — для расчёта enter-timer |
| `last_error` | text | последняя ошибка (nullable) |
| `created_at` | timestamp | |
| `updated_at` | timestamp | |

**Уникальность:** partial unique index по `(account_id, telegram_user_id)` при `status IN ('active','paused')` — не даём завести две активные сессии на одну и ту же пару.

---

## Базовые индексы

| Запрос | Индекс |
|---|---|
| Все сообщения диалога по порядку | `(dialog_id, date)` на `messages` |
| Защита от дублей | уникальный `(dialog_id, telegram_message_id)` на `messages` |
| Чистильщик ищет старые файлы | `(downloaded_at)` на `media` где `file_deleted_at IS NULL` |
| Поиск media по storage_key | `(storage_key)` на `media` |
| Поиск диалога по собеседнику | уникальный `(account_id, telegram_user_id)` на `dialogs` |
| Реакции сообщения | `(message_id)` на `reactions` |

---

## Миграции

Схема управляется через Alembic. Папка миграций — `alembic/versions/`. Детали — в `startup_sequence.md`.

Первая миграция (`0001_initial.py`) создаёт все таблицы и заполняет `settings` значениями по умолчанию.

На каждое изменение схемы — новая миграция:
```bash
alembic revision --autogenerate -m "что меняем"
alembic upgrade head
```

---

## Принципы которые мы зафиксировали

1. В каждую таблицу пишет только один модуль — владелец
2. ORM не используем — SQL руками через asyncpg
3. Миграции через Alembic, автоматически при старте приложения
4. Первая миграция создаёт все таблицы и заполняет `settings` дефолтами
5. Денормализация там где это упрощает модель (пересыльщики в `messages`)
6. Soft delete через `deleted_at` — физически не удаляем
7. События шины архивируются в `events_archive` параллельно с Redis Streams
8. Поле `message` в событиях не хранится в БД — вычисляется API на лету
