"""Модерация успешных диалогов кнопками в Telegram-группе.

Заменяет `report_delivery`: тот сразу пушил успех в партнёрскую CRM,
здесь решение принимает человек, а пуш откладывается на 4 часа.
"""
from .service import ReportBotService

__all__ = ["ReportBotService"]
