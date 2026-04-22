# Отчёт по Этапу 5

> Проект: `finish-outrich` — Telegram Automation Framework
> Репо: https://github.com/Aqua7MarcusAurelius/finish-outrich
> Сессия: 22 апреля 2026, продолжение после Этапа 4

---

## Цель этапа

Модуль транскрибации — превращение голосовых/аудио/видео-сообщений и
кружков в текст, запись результата в `media.transcription`.

1. Consumer шины подхватывает `message.saved` с аудио/видео
2. Скачивает файл из MinIO, гонит через ffmpeg в wav PCM 16kHz mono
3. Отправляет в OpenRouter (мультимодальный gpt-4o-audio-preview, т.к.
   у OpenRouter нет нативного Whisper STT-endpoint'а)
4. Публикует `transcription.started` → `transcription.done`
5. Модуль истории (с 4.2.b) подхватывает `transcription.done` и
   обновляет `media.transcription` + `transcription_status`

**Цель достигнута.** Голосовые и кружки с голосом транскрибируются
полностью; немые кружки проходят как `done` с пустым текстом.

---

## Структура коммитов

| Коммит | Что |
|---|---|
| 5.1 | Конфиг + HTTP-клиент OpenRouter (`core/openrouter.py`, `httpx`, `.env.example`) |
| 5.2 | ffmpeg-конвертер (`modules/transcription/ffmpeg.py`) |
| 5.3 | `TranscriptionService` — consumer шины |
| 5.4 | Интеграция в `api/main.py` lifespan |
| 5.fix-1 | mp3 → wav (OpenAI капризил к mp3) |
| 5.fix-2 | pipe → временный файл (mp4 с moov в конце не читался через stdin) |
| 5.fix-3 | `NoAudioError` → немая дорожка трактуется как валидный пустой результат |

---

## Этап 5.1 — HTTP-клиент OpenRouter

**Цель:** тонкая async-обёртка над OpenRouter API для транскрипции.

### Ключевое открытие

У OpenRouter **нет** endpoint'а `/v1/audio/transcriptions` (Whisper STT).
Доступ к аудио только через `/api/v1/chat/completions` с полем
`input_audio` (base64 + format) на мультимодальные модели.

Выбор модели: `openai/gpt-4o-audio-preview`. Альтернатива — выход
напрямую на OpenAI (Whisper), но это добавляло бы второй ключ и
рушило архитектурный принцип «только через OpenRouter». Решили
остаться с OpenRouter.

### Файлы

- **`requirements.txt`** — добавлен `httpx==0.27.2`.

- **`.env.example`** — `OPENROUTER_MODEL_TRANSCRIPTION` поменян с
  несуществующего `openai/whisper` на рабочее
  `openai/gpt-4o-audio-preview`, с комментарием почему.

- **`core/openrouter.py`** — новый файл:
  - `transcribe_audio(audio_bytes, *, audio_format="wav", model=None)`
  - Формирует payload с `content=[text-prompt, input_audio]`, base64'ит
    байты, шлёт POST на `/api/v1/chat/completions`
  - Промпт: «Transcribe verbatim, return only spoken text, return
    empty string if silent/unintelligible»
  - `temperature=0` — детерминированный ответ
  - Нормализует content: может прийти строкой или списком частей
    `[{type: text, text: ...}]` — оба случая обрабатываются
  - `OpenRouterError` — единое исключение для сетевых/HTTP/shape-ошибок
  - Таймауты: connect=10s, read=180s — Whisper-подобные модели на
    длинных файлах могут думать долго

---

## Этап 5.2 — ffmpeg-конвертер

**Цель:** унифицировать вход «что угодно → wav PCM 16kHz mono».
На любой другой формат у OpenAI-провайдера OpenRouter натыкались
на капризы.

### Файлы

- **`modules/transcription/ffmpeg.py`** — `to_wav(input_bytes) -> bytes`:
  - Пишет вход во временный файл через `tempfile.mkstemp` +
    `asyncio.to_thread` (чтобы не блокировать loop)
  - Запускает `ffmpeg -i <path> -vn -ac 1 -ar 16000 -c:a pcm_s16le -f wav pipe:1`
    через `asyncio.create_subprocess_exec`
  - Читает stdout в память, stderr оставляет на случай ошибки
  - В `finally` удаляет временный файл
  - Жёсткий таймаут 120s на конвертацию
  - Два отдельных класса исключений:
    - `FfmpegError` — реальная ошибка (битый файл, exit!=0, таймаут)
    - `NoAudioError` — wav на выходе ≤100 байт (только header),
      то есть в исходнике нет аудиодорожки

- **`modules/transcription/__init__.py`** — пустой пакетный init.

### Почему временный файл, а не stdin pipe

Первоначально делали через `-i pipe:0` + `proc.communicate(input=...)`.
На голосовых (`.ogg/opus`) работало, на кружках (`mp4 video_note`)
ffmpeg выдавал только WAV-header (44 байта) — **аудиодорожки не находил**.

Причина: Telegram mp4 хранит moov atom в конце файла. ffmpeg при
чтении из stdin **не умеет seek** и до метаданных не доходит. С
файлом на диске проблема исчезает мгновенно.

Доки (`docs/transcription.md`) писали «временных локальных файлов не
создаём — байты живут в памяти на время запроса». Это концептуальное
пожелание, которое упёрлось в реальность. Оверхед от файла —
миллисекунды, зато работает для любого формата.

---

## Этап 5.3 — TranscriptionService

**Цель:** consumer-loop модуля, аналогичный `HistoryService`.

### Файлы

- **`modules/transcription/service.py`** — класс `TranscriptionService`:
  - Consumer group `transcription-worker`, consumer `transcription-worker-1`
  - Dispatcher по типу события:
    - `message.saved` — основной путь. Быстрый фильтр по флагам
      `has_audio | has_video` — если ни одного, выходим без похода
      в БД. Потом по списку media из payload фильтруем по типу
      (voice/audio/video/video_note) и каждое обрабатываем
    - `media.reprocess.requested` с `field=transcription` — подтянуть
      `type` и `storage_key` из БД и прогнать заново. На
      этап 5 endpoint `/media/{id}/retranscribe` ещё не написан,
      но слушатель заложен
    - остальные события молча ack'аются
  - Если handler упал на любом шаге — event **не ack'аем**, переобработаем
    в следующем прогоне. Дубль-транскрипции при двойной обработке не
    критичны: `history` просто перезаписывает тот же результат
  - Read batch 20, block_ms=5000

### Основной флоу одного media (`_process_media`)

```
 storage_key отсутствует  →  transcription.done [failed, file_not_available]
         │
         ▼
 publish transcription.started [in_progress]
         │
 minio.get_object(storage_key) → bytes
         │ ошибка MinIO → system.error + transcription.done [failed]
         ▼
 ffmpeg.to_wav(bytes) → wav
         ├── NoAudioError  →  transcription.done [success, text=""]  (выход)
         └── FfmpegError   →  transcription.done [failed, error=ffmpeg: …]  (выход)
         │
         ▼
 openrouter.transcribe_audio(wav) с ретраями из settings.transcription.retries
         │
         ▼
 publish transcription.done [success|error, text, status=done|failed]
         │
         ▼
 (history слушает → UPDATE media.transcription, publish message.updated)
```

### Логика ретраев (по `docs/transcription.md`)

- При ошибке OpenRouter — до `settings.transcription.retries` (дефолт 1)
  повторных попыток. Если после всех всё равно ошибка → `failed` +
  `error` с текстом последней ошибки
- При **пустом** ответе — ровно одна повторная попытка. Если снова
  пусто — `done` с пустой строкой (пустота — валидный результат
  тишины/невнятной речи). Если на ретрае ошибка — фиксируем первый
  пустой как `done`

### Все ошибки — в шину

- Инфраструктурные (MinIO недоступен) — `system.error` + финальный
  `transcription.done [failed]`
- Бизнес (OpenRouter 4xx, таймаут, ffmpeg exit!=0) — только
  `transcription.done [failed]`
- Соблюдается принцип «в каждую таблицу пишет только один модуль»:
  сам сервис в БД не лезет, только публикует в шину

---

## Этап 5.4 — Интеграция в lifespan

**Цель:** запустить `TranscriptionService` фоновой задачей при старте
приложения.

### Файлы

- **`api/main.py`**:
  - Добавлен импорт `TranscriptionService`
  - Startup: `transcription_service = TranscriptionService()`,
    `asyncio.create_task(transcription_service.run())`
  - Shutdown: `stop()` + `task.cancel()` в обратном порядке (первым
    после worker_manager/auth_service, до cleaner и history)
  - Версия приложения поднята до `0.5.0`

---

## Фиксы по ходу

Этап не зашёл с первой попытки — пришлось три раза переделать по мере
понимания как реально себя ведёт провайдер OpenAI через OpenRouter.

### Fix 1: mp3 → wav

**Симптом:** все запросы валились с
`"The data provided for 'input_audio' is not of valid mp3 format"`.

**Причина:** ffmpeg писал валидный mp3, но OpenAI в OpenRouter
капризит к VBR/Xing-заголовкам и отказывается его читать.

**Фикс:** перегнали всё в wav PCM 16kHz mono. Формально модель
поддерживает и mp3, и wav, но wav гарантированно читается.
Изменения: `ffmpeg.py` (args: `-c:a pcm_s16le -f wav`), `openrouter.py`
(дефолт `audio_format="wav"`), `service.py` (переименован `to_mp3 → to_wav`).

### Fix 2: pipe → временный файл

**Симптом:** голосовые (.ogg/opus) работают, кружки (.mp4 video_note)
падают с `"Input audio is too short. Provide at least 0.1 seconds."`

**Причина:** ffmpeg через `-i pipe:0` не умеет seek, а Telegram mp4
хранит moov atom в конце файла. ffmpeg не находит аудиодорожку и
выдаёт пустой wav (только header, 44 байта). base64 от header OpenAI
честно называет «too short».

**Диагностика:** добавили временный лог размеров
`raw → wav`. Увидели `wav=78 bytes` на кружке — подтверждение.

**Фикс:** пишем вход во временный файл, ffmpeg читает с диска с
нормальным seek'ом. С файлом на диске кружки работают для любых
форматов. Реализовано в `ffmpeg.py` через `tempfile.mkstemp` +
`asyncio.to_thread`.

### Fix 3: немые кружки

**Симптом:** кружок с выключенным микрофоном падал в `failed`
с `ffmpeg: no audio track`. По смыслу это не ошибка — так же как
пустой ответ Whisper на тишину.

**Фикс:** отдельный класс `NoAudioError` (только когда wav ≤100 байт
на выходе). В сервисе `except NoAudioError` публикует
`transcription.done [success, text=""]`. Формально `transcription_status`
в таблице станет `done`, `transcription = ""` — UI сможет отличить
«обработано, говорить никто не говорил» от «не смогли обработать».

---

## Что НЕ сделано (известные нюансы)

1. **Галлюцинации у gpt-4o-audio-preview.** Модель иногда выдумывает
   текст при неразборчивом аудио (фоновый шум без речи → на тесте
   вернулось `"The quick brown fox jumps over the lazy dog."` —
   классический пример из обучающих данных). Это фундаментальное
   свойство мультимодальных LLM, не нашего кода. Whisper такого не
   делает. Решения на будущее — либо прямой вызов OpenAI Whisper,
   либо переключение на Gemini через OpenRouter (по слухам
   галлюцинирует меньше на аудио). Оставлено как есть.

2. **endpoint `/media/{id}/retranscribe` не написан.** Модуль его
   слушает (на `media.reprocess.requested`), но сам endpoint — в
   Этапе 6 или позже, вместе с `/media/*` endpoint'ами в целом.

---

## Проверка

Тесты на живом аккаунте (account_id=1):

| Что | Результат |
|---|---|
| Голосовое «Раз, два, три, четыре, ау, ау, ау!» (6с .ogg) | ✅ точный текст |
| Голосовое «Брусик-трусик.» (2с .ogg) | ✅ точный текст |
| Кружок с речью «Поросячий виск 777.» (3с .mp4) | ✅ точный текст |
| Кружок с речью «И я Пантелеймон Кулич. Кав-кав-кав.» (9с .mp4) | ✅ точный текст |
| Немой/шумный кружок (5с .mp4) | ⚠ галлюцинация «The quick brown fox…» (ограничение модели) |

Цепочка событий по `/events?limit=10` на каждом тесте:

```
message.received → message.saved → transcription.started → transcription.done → message.updated
```

`parent_id` связи корректные, `media.transcription` и
`transcription_status=done` обновляются в БД.

---

## Что в итоге на GitHub

Новые файлы:
```
core/
  openrouter.py                         — клиент OpenRouter
modules/
  transcription/
    __init__.py
    ffmpeg.py                           — to_wav + FfmpegError + NoAudioError
    service.py                          — TranscriptionService
```

Изменённые файлы:
```
requirements.txt                        — +httpx
.env.example                            — OPENROUTER_MODEL_TRANSCRIPTION
api/main.py                             — TranscriptionService в lifespan, v0.5.0
```

**Версия приложения:** `0.5.0`
**Endpoint'ов работает:** 24 (без изменений; Этап 5 — чистый consumer шины)
**Endpoint'ов от финального MVP:** ~73% (24 из 33)

---

## Что дальше — План на Этап 6

**Цель:** модуль описания медиа (GPT-4o через OpenRouter) +
endpoint'ы `/media/*`.

### Модуль описания медиа

`modules/media_description/service.py` — симметричный
`TranscriptionService`:
- Consumer group `description-worker`, слушает `message.saved`
  с `has_image | has_video | has_document`
- Фильтр по типу media:
  - `photo / sticker / document` → байты → GPT-4o vision
  - `gif / video / video_note` → FFmpeg нарезает
    `settings.description.frames_count` (дефолт 5) кадров в памяти →
    GPT-4o vision с массивом изображений
- Промпт — общее описание содержимого
- Retry policy ровно как у транскрипции: `settings.description.retries`
  (дефолт 1) на ошибку, 1 на пустой результат → `done` с пустым текстом
- Публикует `description.started` → `description.done`; history уже
  умеет (с 4.2.b) обновлять `media.description` и статус

FFmpeg для нарезки кадров — через тот же приём с временным файлом
что и в транскрипции (на mp4/moov это опять же критично). Скорее всего
вынесем общий helper в `core/ffmpeg.py` или оставим копию в
`modules/media_description/ffmpeg.py` — посмотрим по месту.

Кружок обрабатывается и здесь, и в транскрипции — два независимых
поля (`transcription` и `description`). Это уже работает по history:
начальные статусы для `video_note` — `(pending, pending)`.

### Endpoint'ы `/media/*`

`api/routes/media.py`:
- `GET /media/{id}` — метаданные + `file_available`
- `GET /media/{id}/file` — стрим из MinIO с правильным Content-Type
- `POST /media/{id}/retranscribe` — сбрасывает статус + публикует
  `media.reprocess.requested` (с `field=transcription`). Слушатель в
  транскрипции уже есть
- `POST /media/{id}/redescribe` — симметрично

**Оценка:** 2–3 часа. Шаблон обработки ровно тот же, что в Этапе 5 —
разница в модели и промпте.

---

## Формат старта Этапа 6

Открыть новый чат, прикрепить документацию:

минимум: `architecture.md`, `api.md`, `event_bus.md`, `database_schema.md`,
`media_description.md`, `configuration.md`, `STAGE_4_REPORT.md`,
`STAGE_5_REPORT.md` (этот файл).

Начать примерно так:

> Продолжаем проект `finish-outrich`. Этапы 1-5 закрыты — инфра, шина,
> авторизация, воркеры, история + endpoints + нагон, транскрибация
> голосовых/кружков/видео через OpenRouter. Репо
> https://github.com/Aqua7MarcusAurelius/finish-outrich. Отчёты по
> Этапам 4 и 5 лежат в файлах проекта. Поехали к Этапу 6 — модуль
> описания медиа (GPT-4o) + endpoints /media/*. Аккаунт и прокси те
> же. Работаем так же — по шагам с проверкой, файлы в чат, я сам
> коммичу.

---

*Сгенерировано в конце сессии 22.04.2026.*
