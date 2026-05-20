"""Модуль интеграции с партнёрской CRM.

Зона ответственности:
- Тянуть лидов из CRM (POST /leads/request-new) в локальный пул
  `crm_lead_pool`.
- Сообщать CRM что лид взят в работу (POST /leads/:u/start) —
  когда autochat реально стартанул переписку.
- Сообщать CRM финальный статус (POST /leads/:u/status) — когда
  оператор / оценщик / таймер поставили терминальный пузырь.

Принципы:
- В этом модуле живёт **единственная** точка общения с партнёрской CRM.
- Модуль ничего не знает про autochat, lead_evaluator или UI —
  только шину и БД.
- Owns: таблица crm_lead_pool. Читает: accounts, dialog_statuses, dialogs.

См. docs/INT_CRM_API_TOKEN_GUIDE.md и docs/dev_diary/10.0_crm_integration_design.md.
"""
