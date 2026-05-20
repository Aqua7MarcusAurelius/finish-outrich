"""Модуль оценщика лидов.

Зона ответственности:
- При закрытии диалога (autochat.finished_by_llm или ручной запрос
  оператора) подать переписку и профиль лида в LLM, получить вердикт
  (`success` / `archive`) и извлечённые поля (имя, возраст, город,
  работа, интересы), записать в `lead_evaluations`.
- На основании вердикта проставить терминальный пузырь на диалоге
  через `dialog_statuses.set_dialog_status`. Дальше `partner_crm`
  подхватит и запушит финальный статус в CRM (модуль про CRM не
  знает напрямую).

Принципы:
- Owns: таблица `lead_evaluations` (CRUD только отсюда).
- Не знает про partner_crm и autochat — общается через шину.
- Промт живёт в `prompts/lead_evaluator_system.md`, читается на каждом
  вызове, плейсхолдеры подставляются переиспользованным механизмом
  из autochat/generation.

Триггеры запуска:
1. `autochat.finished_by_llm` — LLM решила что закончила (после
   отправки последнего сегмента).
2. POST /lead-evaluator/dialogs/{id}/evaluate — ручной от оператора.

Если LLM-ответ непарсится (нет `<verdict>`) — ОДИН ретрай. Если
после ретрая опять без `<verdict>` — записываем `parse_succeeded=false`,
вердикт=`archive` (защитно), отчёт пойдёт в папку `archive/` (10.7).

См. docs/dev_diary/10.0_crm_integration_design.md и
docs/dev_diary/10.5_lead_evaluator.md.
"""
