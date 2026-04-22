# Отчёт по Этапу 6

> Проект: `finish-outrich` — Telegram Automation Framework
> Репо: https://github.com/Aqua7MarcusAurelius/finish-outrich
> Сессия: 22 апреля 2026, продолжение после Этапа 5

---

## Цель этапа

Два независимых блока:

**A. Модуль описания медиа** — симметричный модулю транскрибации:
  1. Consumer шины подхватывает `message.saved` с визуальным медиа или
     документом
  2. Скачивает файл из MinIO
  3. Для картинок / кадров видео — GPT-4o (vision) через OpenRouter
  4. Для документов — Gemini с нативной поддержкой PDF
  5. Публикует `description.started` → `description.done`
  6. Модуль истории (с 4.2.b) обновляет `media.description` +
     `description_status`

**B. Endpoint'ы `/media/*`** — для чтения файлов/метаданных и
перезапуска обработки.

**Цель достигнута.** Все 6 типов медиа (photo, sticker, gif, video,
video_note, document) обрабатываются корректно; endpoint'ы отдают
метаданные и файлы, retranscribe/redescribe запускают полный цикл
через шину.

---

## Структура коммитов

| Коммит | Что |
|---|---|
| 6.1 | Клиент OpenRouter — `describe_images`, `describe_document` + хелперы |
| 6.2 | ffmpeg-нарезка кадров (`modules/media_description/ffmpeg.py`) |
| 6.3 | `DescriptionService` — consumer шины |
| 6.4 | Интеграция в `api/main.py` lifespan (v0.6.0) |
| 6.5-fix | MJPEG `-pix_fmt yuvj420p` (для GIF) + ранний skip TGS-стикеров |
| 6.6-6.7 | Роуты `api/routes/media.py` — GET, file, retranscribe, redescribe |

Параллельно заведён `docs/dev_diary/` — журнал решений по подэтапам
(README.md с форматом + по одному файлу на каждый подэтап).

---

## Этап 6.1 — расширение OpenRouter-клиента

**Цель:** две новые функции для модуля описания.

### Ключевые решения

**Две модели, не одна.** GPT-4o через OpenRouter отлично принимает
картинки через `image_url` (data-URL base64), но не принимает PDF
напрямую. Документы требуют модели с нативной поддержкой файлов — из
доступных в OpenRouter выбран `google/gemini-2.5-flash` (дешевле Claude,
PDF проходит нативно без плагина-парсера).

**plugin file-parser включён как страховка.** Для Gemini это no-op
(файл и так идёт нативно), но если в будущем модель сменится на
не-PDF-нативную — OpenRouter автоматически подключит парсер.

**Общие части клиента вынесены в хелперы** `_headers()` и `_post()` —
чтобы не дублировать обработку ошибок и парсинг `choices[0].message.content`
трижды (`transcribe_audio`, `describe_images`, `describe_document`).

### Файлы

- **`core/openrouter.py`** — добавлены `describe_images(images, prompt,
  model, image_format)`, `describe_document(bytes, filename, mime_type,
  prompt, model)`, приватные `_headers`, `_post`. Промпты
  `IMAGE_DESCRIPTION_PROMPT` и `DOCUMENT_DESCRIPTION_PROMPT` — на русском
  (вывод попадает в UI).
- **`core/config.py`** — `OPENROUTER_MODEL_DESCRIPTION_DOCUMENTS` по
  умолчанию `google/gemini-2.5-flash`.
- **`.env.example`** — та же переменная с комментарием.

---

## Этап 6.2 — ffmpeg: нарезка кадров

**Цель:** получать N JPEG-кадров, равномерно распределённых по
длительности видео/GIF/video_note.

### Ключевые решения

**Через временный файл, не stdin.** Та же причина что на Этапе 5 —
Telegram mp4/video_note хранит moov atom в конце файла, ffmpeg при
чтении из stdin не умеет seek и видеодорожку не находит. С файлом на
диске всё работает.

**ffprobe для duration + отдельные запуски ffmpeg на каждый кадр.**
Альтернатива через фильтр `fps=N/duration` хрупкая (зависит от знания
fps, для vfr-видео непредсказуема). Отдельные запуски с `-ss` и
`-frames:v 1` — тупо и контролируемо. 3-5 кадров стоят десятков
миллисекунд оверхеда на запуск процесса.

**JPEG, не PNG.** Vision-модели принимают и то, и другое, но JPEG в
base64 в 5-10 раз меньше по байтам → меньше токенов на входе у
провайдера. Для «опиши что изображено» качества с `-q:v 3` достаточно.

**Моменты — середины равных отрезков** `(i + 0.5) * duration / N`.
Кадр 0 часто чёрный/лого, конец тоже часто статичный. Середины дают
равномерное покрытие без краевых эффектов.

### Файлы

- **`modules/media_description/__init__.py`** — пустой init.
- **`modules/media_description/ffmpeg.py`** — `extract_frames`,
  `FfmpegError`, `NoFramesError`.

---

## Этап 6.3 — DescriptionService

**Цель:** consumer-loop модуля, зеркальный `TranscriptionService`.

### Три маршрута по типу media

```
photo, sticker         → describe_images([raw])          (одна картинка)
gif, video, video_note → extract_frames → describe_images(frames)
document               → describe_document(raw)
```

Множества вынесены в константы `STATIC_IMAGE_TYPES`, `FRAME_TYPES`,
`DOCUMENT_TYPES` — одинаково используются в фильтре входа и dispatcher.

### Отличия от TranscriptionService

1. **Флаги** — `has_image or has_video or has_document` (video_note
   имеет и `has_audio` и `has_video` → попадает в оба модуля — by design).
2. **Политика ретраев вынесена в `_retry_policy(call, retries)`** —
   принимает callable, потому что для изображений и документов
   функции разные. Сама политика та же: `1 + retries` попыток на
   ошибку, один повтор на пустое.
3. **Две настройки из БД** (`description.retries` и
   `description.frames_count`) — добавлен хелпер `_get_int_setting(key,
   default)`.

### Filename для документов — из MIME

`saved_media` payload не содержит настоящего имени файла. Генерируем из
MIME: `application/pdf → document.pdf`, и т.п. Gemini смотрит на
MIME из data-URL, поле filename — только метка в логах провайдера.

### Файлы

- **`modules/media_description/service.py`** — полный сервис.

---

## Этап 6.4 — интеграция в lifespan

**Цель:** запустить `DescriptionService` фоновой задачей при старте.

### Файлы

- **`api/main.py`** — импорт, создание на startup после transcription,
  остановка на shutdown до него (зеркальный порядок). Версия → `0.6.0`.

---

## Фиксы по результатам живого теста (Этап 6.5)

Живой тест прогнан сразу после 6.4 на шести типах медиа. Первые четыре
прошли. Два фейла разобраны и исправлены.

### Результаты первого прогона

| Тип | Статус | Комментарий |
|---|---|---|
| Фото | ✅ done | — |
| Стикер TGS (`application/x-tgsticker`) | ❌ failed | HTTP 400 от провайдера |
| GIF | ❌ failed | `ffmpeg exit=234`, ff_frame_thread_encoder_init failed |
| Видео | ✅ done | — |
| Кружок | ✅ done | — |
| PDF | ✅ done — **Gemini native работает** | — |

### Fix 1: GIF и MJPEG

**Симптом:** ffmpeg exit=234, stderr:
```
[mjpeg] Non full-range YUV is non-standard
[mjpeg] ff_frame_thread_encoder_init failed
Error while opening encoder ... Invalid argument
```

**Причина:** Telegram отдаёт GIF как mp4 с `yuv420p` (limited-range YUV).
MJPEG требует full-range YUV (`yuvj420p`) — это стандарт для JPEG.

**Фикс:** добавлен `-pix_fmt yuvj420p` в `_grab_frame`. Также покрывает
видео с нестандартным цветовым пространством.

### Fix 2: TGS-стикер

**Симптом:** HTTP 400 от модели.

**Причина:** TGS — анимированный Lottie-JSON в gzip, не картинка.
Vision-модели его не читают — это не баг провайдера.

**Фикс:** ранний short-circuit в `_process_media`: для
`media_type == "sticker" and mime_type == "application/x-tgsticker"` сразу
публикуем `description.done [done, text=""]`, без скачивания из MinIO.

Конвертировать TGS в PNG через lottie сейчас не делаем — отдельная
зависимость, один статичный кадр плохо отражает анимированный стикер,
доля TGS невелика.

### Результаты после фиксов

| Тип | Статус |
|---|---|
| 🎬 GIF (после pix_fmt fix) | ✅ done («Мужчина сидит и сильно потеет...») |
| 🏷 TGS-стикер | ✅ done `""` (быстрый skip) |
| 🏷 webp-стикер | ✅ done («Серый котёнок держит в лапах нож.») |

---

## Этап 6.6-6.7 — Endpoint'ы `/media/*`

**Цель:** 4 endpoint'а группы медиа в одном файле.

### Ключевые решения

**Один файл на всю группу.** Все endpoint'ы работают с таблицей `media`,
три из них делят валидацию. Разбивать по файлам — преждевременно.

**StreamingResponse с одним чанком.** По доке — «стрим из MinIO».
Реально мы уже читаем объект целиком (`minio_mod.get_object` →
bytes). Для 20 МБ файлов Telegram этого достаточно. Сигнатура
endpoint'а сохраняет контракт на будущее переключение на честный
стрим без breaking change.

**Общая функция `_reprocess(field)` для двух endpoint'ов.** retranscribe
и redescribe отличаются только таблицей статуса и допустимым набором
типов. Код один.

**WRONG_MEDIA_TYPE — 409, не 400.** Media существует, просто не той
природы — это конфликт состояния.

### Файлы

- **`api/routes/media.py`** — новый роутер:
  - `GET /media/{id}` — метаданные + `file_available`
  - `GET /media/{id}/file` — стрим с правильным Content-Type, 410
    `FILE_CLEANED`
  - `POST /media/{id}/retranscribe` — сброс статуса +
    `media.reprocess.requested [field=transcription]`
  - `POST /media/{id}/redescribe` — то же для `field=description`
- **`api/main.py`** — импорт и регистрация роутера.

### Тестирование

Через `docker exec tgf_app curl ...` (локальный bash/curl на Windows
портит UTF-8 в отображении — артефакт cp1251 codepage, не API):

| Проверка | Результат |
|---|---|
| `GET /media/26` | 200, все поля, description на кириллице читаемый |
| `GET /media/9999` | 404 `MEDIA_NOT_FOUND` |
| `POST /media/26/redescribe` | 200, цепочка `media.reprocess.requested → description.started → description.done → message.updated`, media.description перезаписано новым текстом |

---

## Что в итоге на GitHub

Новые файлы:
```
core/
  — (изменён openrouter.py)
modules/
  media_description/
    __init__.py
    ffmpeg.py                              — extract_frames + FfmpegError + NoFramesError
    service.py                             — DescriptionService
api/
  routes/
    media.py                               — 4 endpoint'а /media/*
docs/
  STAGE_6_REPORT.md                        — этот отчёт
  dev_diary/
    README.md                              — формат журнала
    6.1_openrouter_client.md
    6.2_ffmpeg_frames.md
    6.3_description_service.md
    6.4_lifespan_integration.md
    6.5_live_test_and_fixes.md
    6.6_media_endpoints.md
```

Изменённые файлы:
```
core/openrouter.py                        — +describe_images, +describe_document, хелперы
core/config.py                            — +OPENROUTER_MODEL_DESCRIPTION_DOCUMENTS
.env.example                              — та же переменная
api/main.py                               — DescriptionService в lifespan, media_router, v0.6.0
```

**Версия приложения:** `0.6.0`
**Endpoint'ов работает:** 28 (+4 к Этапу 5: 24 + 4 /media/*)
**Endpoint'ов от финального MVP:** ~85% (28 из 33)

---

## Что НЕ сделано (известные нюансы)

1. **Анимированные TGS-стикеры не описываются.** Отдаются как пустой
   `done`. Конвертация через python-lottie — отдельная итерация, если
   понадобится.

2. **Некоторые форматы документов (xlsx, docx) могут упасть с HTTP 400**
   от Gemini. Формально OpenRouter обещает «any file», но для
   нестандартных форматов провайдер может отказать. Fail происходит
   корректно (→ `description.done [failed]` с сообщением), систему не
   ломает.

3. **GET /media/{id}/file — не честный стрим.** Файл читается в память
   целиком перед отдачей. Для 20 МБ Telegram-файлов нормально; для
   2 ГБ Premium-файлов надо переписать на chunked read из MinIO.

4. **Галлюцинации у gpt-4o** (упомянутое в Этапе 5 ограничение — сохраняется
   для видеокадров). Решения на будущее — либо Gemini через
   OpenRouter также для картинок, либо промпт-инжиниринг против
   галлюцинаций. Оставлено как есть.

---

## Что дальше — план на Этап 7

По состоянию на 28/33 endpoint'ов: осталось ~5 endpoint'ов + SSE-стримы
воркеров и диалога. Это следующий этап — **UI/SSE** и
оставшиеся endpoint'ы:

- `/workers/stream` SSE
- `/dialogs/{id}/stream` SSE (уже частично есть — проверить)
- `/accounts/*` CRUD
- `/search?q=...` — глобальный поиск
- `/dashboard`, `/system/stats`

После Этапа 7 — готовый бэкенд под веб-дашборд.

---

*Сгенерировано в конце сессии 22.04.2026.*
