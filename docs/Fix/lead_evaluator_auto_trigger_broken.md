# Bug: автозапуск `lead_evaluator` после `<finishdialog/>` не работает

**Статус**: ✅ **FIXED** — см. `docs/dev_diary/10.14_fix_finished_by_llm_dispatch.md` и
коммит на ветке `beta`. Применены оба варианта (publisher payload + defensive
resolve в consumer + log.warning вместо silent return).

**Ветка**: `beta` @ `9037552`
**Окружение**: prod-like контейнерное (docker compose), миграции до `0012 (head)`
**Дата обнаружения**: 2026-05-19

---

## Краткая суть

После того как LLM-собеседник эмитит маркер `<finishdialog/>`, событие
`autochat.finished_by_llm` публикуется в шину штатно — но `lead_evaluator`
его **молча игнорирует**, потому что в payload отсутствует `dialog_id`,
который consumer считает обязательным.

В результате весь дальнейший каскад («оценщик → терминальный пузырь →
отчёт → push в партнёрский CRM») не запускается **до тех пор, пока
оператор не нажмёт кнопку «Оценить» вручную**.

---

## Конкретный кейс (репро)

Диалог `id=35976` с `@vaylerii` (account_id=4 / `SAD_05_RU`),
session_id=14. CRM-источник, target_gender=female.

### Что отрабатывает штатно

| Время (UTC) | Событие | Payload |
|---|---|---|
| `11:30:02` | `partner_crm.lead_started` | `{"origin":"assigned","username":"vaylerii"}` |
| `11:49:31` | `partner_crm.lead_taken` | `{"gender":"female","username":"vaylerii"}` |
| `11:49:35` | `autochat.started` | `{"session_id":14,"username":"vaylerii","telegram_user_id":521063643}` |
| `11:49:35` | `dialog.status_changed` → `status_id=2` (Первый контакт) | |
| `11:50:13` | первый ответ лида → `dialog.status_changed` → `status_id=3` (Общение) | |
| `11:50-11:56` | 21 сообщение, нормальная переписка | |
| `11:56:16` | `autochat.segment_sent` segment 3/3 (последний исходящий, `tg_msg_id=6149`) | |
| `11:56:16` | **`autochat.finished_by_llm`** | **`{"session_id":14}`** ← здесь начинается проблема |
| `11:56:16` | `autochat.session_stopped` | `{"session_id":14}` |

### Что должно было случиться, но не случилось

Ожидаемая цепочка (см. [`modules/autochat/report_consumer.py:7-15`](../../modules/autochat/report_consumer.py#L7-L15)):

```
LLM ставит <finishdialog/>         → autochat.finished_by_llm
  ↓
lead_evaluator анализирует         → ставит терминальный пузырь
  ↓
event dialog.status_changed
  ↓
ReportConsumer ловит               → save_report_for_dialog
  ↓
partner_crm.finalizer пушит статус в CRM
```

Фактически между `11:56:16` и `11:59:25` (3 минуты тишины) — **ни одного
события** от `lead_evaluator`, `autochat.report_saved`,
`partner_crm.status_pushed` не появилось.

### Что разморозило процесс

В `11:59:25` оператор вручную нажал «Оценить» в UI:

| Время | Событие | Source |
|---|---|---|
| `11:59:25.852` | `lead_evaluator.started` | **`"source":"manual"`** |
| `11:59:29.838` | `dialog.status_changed` → `status_id=5` (Завершен успешно), `source: "event:manual"` | |
| `11:59:29.839` | `lead_evaluator.completed` | verdict=success, extracted={age:35, city:Самара, name:Валерия, workplace:эскорт} |
| `11:59:29.890` | `autochat.report_saved` | folder=`success`, key=`success/SAD_05_RU_@vaylerii_19-05-26_11-56.html` |
| `11:59:30.301` | `partner_crm.status_pushed` | status=`ready_for_transfer` |

Т.е. при ручном тычке весь хвост отработал безупречно за <500 мс.
Проблема **только** в авто-триггере.

---

## Корневая причина

### Publisher

[`modules/autochat/session.py:786-796`](../../modules/autochat/session.py#L786-L796)

```python
async def _publish_finished_by_llm(self, parent_id: str | None) -> None:
    """Публикует событие autochat.finished_by_llm. Сам stop делает callback."""
    try:
        await bus.publish(
            module=Module.AUTOCHAT,
            type=EventType.AUTOCHAT_FINISHED_BY_LLM,
            status=Status.SUCCESS,
            parent_id=parent_id,
            account_id=self.account_id,
            data={"session_id": self.id},   # ← dialog_id не передаётся
        )
```

В payload только `session_id`. `dialog_id` известен сессии
(`self.dialog_id`) — но не кладётся в data.

### Consumer

[`modules/lead_evaluator/service.py:163-180`](../../modules/lead_evaluator/service.py#L163-L180)

```python
async def _on_finished_by_llm(self, event: dict) -> None:
    data = event.get("data") or {}
    dialog_id = data.get("dialog_id")
    if dialog_id is None:
        # AutoChat не всегда передаёт dialog_id (если сессия упала
        # до создания диалога) — без него оценщик ничего не сделает.
        return
    try:
        await self.evaluate(int(dialog_id), source="autochat.finished_by_llm")
    except EvaluationAlreadyRunning:
        ...
```

Consumer ожидает `data.dialog_id`, и при его отсутствии **молча выходит
без логирования и без ошибки**. Поэтому в `events_archive` нет ни
`lead_evaluator.started`, ни какого-либо warn'а.

### Стык контрактов

Publisher и consumer не согласованы:
- publisher отдаёт `{session_id}`
- consumer требует `{dialog_id}`
- результат: silent drop, без следов в логах или events.

Комментарий в consumer'е («AutoChat не всегда передаёт dialog_id если
сессия упала до создания диалога») описывает граничный случай (early
fail), но текущий код применяет это ко **всем** сценариям, в т.ч. к
нормальному завершению уже созданного диалога.

---

## Влияние

Авто-flow CRM-интеграции (этап 10) **полностью неактивен** в части,
завязанной на `autochat.finished_by_llm`:

1. **`lead_evaluator`** не оценивает диалоги, которые LLM закрыла
   маркером `<finishdialog/>`. Записи в `lead_evaluations` появляются
   только при ручном нажатии или при срабатывании `silence_watcher`
   (а он — раз в 5 минут и по таймерам тишины, не сразу).
2. **Терминальный пузырь** не выставляется автоматически → диалог
   зависает на `status_id=3` (Общение) даже после `<finishdialog/>`.
3. **`ReportConsumer`** не строит HTML-отчёт (он ждёт
   `dialog.status_changed` с терминальным пузырём).
4. **`partner_crm.finalizer`** не пушит финальный статус в партнёрскую
   CRM (он тоже завязан на терминальный пузырь).

Внешний эффект для оператора: «диалог закончился сам собой, но в
системе ничего не произошло» — что и было замечено.

`silence_watcher` в итоге всё равно подберёт такие диалоги по таймауту
`silence_to_close_hours` (дефолт 48ч) и закроет как `no_response`, но
это не то поведение, которое задумано для `<finishdialog/>` сценария
(там должен быть `ready_for_transfer` / `success`).

---

## Воспроизведение

1. Поднять beta, миграции до `0012`, dialog_statuses пересеяны под
   6 канонических пузырей (см. ниже про реseed).
2. Завести диалог через `partner_crm` flow или вручную.
3. Дождаться, пока LLM эмитит `<finishdialog/>` (в нашем кейсе LLM
   решила завершить, предложив «давай я тебе напишу с личного
   аккаунта»).
4. Проверить `events_archive`:
   - `autochat.finished_by_llm` — есть, payload `{"session_id": N}`
   - `autochat.session_stopped` — есть
   - `lead_evaluator.started` — **нет** (только если оператор ткнёт
     вручную)
   - `dialog.status_changed` на терминальный пузырь — **нет**
   - `autochat.report_saved` — **нет**
   - `partner_crm.status_pushed` — **нет**

SQL для проверки на любом диалоге `D`:

```sql
SELECT time, module, type, status, data
FROM events_archive
WHERE (data->>'dialog_id')::int = D
   OR (module = 'autochat' AND (data->>'session_id')::int IN (
        SELECT id FROM autochat_sessions WHERE dialog_id = D
   ))
ORDER BY time;
```

---

## Предлагаемые фиксы

Любой из двух (или оба). Первый предпочтительнее — событие
самодостаточное.

### Вариант 1 — расширить publisher payload (минимальный диф)

[`modules/autochat/session.py:795`](../../modules/autochat/session.py#L795)

```python
data={
    "session_id": self.id,
    "dialog_id": self.dialog_id,   # ← добавить
},
```

`self.dialog_id` уже доступен на момент публикации (сессия не уходит
в `<finishdialog/>` до того как диалог создан — INSERT `dialogs` и
INSERT `autochat_sessions` идут в `create_session` одной транзакцией
до отправки первого сообщения).

### Вариант 2 — резолвить в consumer (defensive)

[`modules/lead_evaluator/service.py:163-169`](../../modules/lead_evaluator/service.py#L163-L169)

Если `data.dialog_id is None`, попробовать достать через `session_id`:

```python
dialog_id = data.get("dialog_id")
if dialog_id is None:
    session_id = data.get("session_id")
    if session_id is not None:
        pool = db.get_pool()
        async with pool.acquire() as conn:
            dialog_id = await conn.fetchval(
                "SELECT dialog_id FROM autochat_sessions WHERE id = $1",
                int(session_id),
            )
    if dialog_id is None:
        log.warning(
            "lead_evaluator: finished_by_llm event without resolvable "
            "dialog_id (session=%s) — skipping", data.get("session_id"),
        )
        return
```

Плюс: обратная совместимость со старыми событиями в стриме (если
дошлёт что-то старое). Минус: extra DB-roundtrip на каждое событие.

### Рекомендация

Сделать **оба**: вариант 1 как основной фикс контракта, вариант 2 как
страховка + добавить `log.warning` вместо silent `return` (чтобы
будущий рассинхрон контрактов не уходил в тишину).

---

## Дополнительные замечания (попутно)

1. **`dialog_statuses` после миграции 0012** — миграция переезжает на
   новые 6 пузырей только если каталог пустой или ровно 4-row default
   из 0009. В нашем случае оператор накастомил 2 пузыря — миграция
   решила не трогать, и `lead_evaluator` / `silence_watcher` падали с
   warn'ом «один из системных пузырей не найден». Пересеяли вручную
   на канонические 6, после этого процесс заработал (кроме описанной
   тут проблемы с авто-evaluator). Стоит подумать про более жёсткий
   path в миграции — например, обязательно добавлять недостающие
   системные labels поверх кастомных, либо явно ругаться в логах
   при старте, если `lead_evaluator` не нашёл нужные пузыри.

2. **Тишина при silent drop** — в `_on_finished_by_llm` нет логов
   вообще. Любой невидимый skip в bus consumer'е — это головная боль
   при диагностике. Минимум `log.debug` на каждом early return.

3. **`silence_watcher` интервал** — после reseed модуль молчит до
   первого тика (5 минут по дефолту `silence_check_interval_sec=300`).
   Если бы он был привязан ещё и к событию `autochat.finished_by_llm`,
   это могло бы стать workaround'ом до фикса. Сейчас он чисто
   таймерный.

---

## Verify после фикса

После применения варианта 1 повторить репро. В `events_archive`
должно появиться:

```
autochat.finished_by_llm          data={"session_id": N, "dialog_id": M}
lead_evaluator.started            source="autochat.finished_by_llm"
lead_evaluator.completed          verdict=success|archive
dialog.status_changed             status_id=терминальный
autochat.report_saved             folder=success|failed|archive
partner_crm.status_pushed         status=ready_for_transfer|no_response|invalid
```

Цепочка должна сложиться внутри одной секунды после `<finishdialog/>`,
без участия оператора.
