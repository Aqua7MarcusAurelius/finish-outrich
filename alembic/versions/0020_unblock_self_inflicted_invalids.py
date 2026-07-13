"""unblock_self_inflicted_invalids — вернуть в оборот лидов, забракованных
НАШИМИ сбоями (не самим лидом).

Контекст (2026-07-13). До этого фикса reinsert_into_pool на 3-й НАШ сбой
(PEER_FLOOD от общего api_id, недоступность OpenRouter — всё это наши причины,
а не свойство лида) заносил лида в `invalid_leads` с reason
`max_attempts_exhausted` и пушил invalid партнёру. Итог: валидные лиды, которым
НИКТО не писал, оказались заблокированы навсегда. После «сброса в 0» это одна из
причин, почему пул пуст и прописи стоят.

Что делаем: убираем из ЛОКАЛЬНОГО чёрного списка `invalid_leads` записи с
reason `max_attempts_exhausted`, которых НЕТ в `contacted_leads` (т.е. которым мы
точно НЕ писали). `contacted_leads` НЕ трогаем — «не написать тем, кому уже
писали» превыше всего (решение владельца).

ВАЖНО (партнёрская сторона). Эта миграция чинит только НАШ локальный блок. Тем
же лидам мог быть отправлен `invalid` в партнёрскую CRM — там они тоже помечены
и не придут обратно как `new`, пока партнёр их не переназначит. Восстановление
на стороне партнёра — отдельным шагом (координация с партнёром / отдельный push),
намеренно НЕ делается автоматически.

Идемпотентна: повторный прогон просто удалит 0 строк.

Revision ID: 0020
Revises: 0019
Create Date: 2026-07-13
"""
from __future__ import annotations

from alembic import op


revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DELETE FROM invalid_leads
        WHERE reason = 'max_attempts_exhausted'
          AND lower(username) NOT IN (
                SELECT lower(username) FROM contacted_leads
          );
        """
    )


def downgrade() -> None:
    # Необратимо: удалённые записи были ошибочными блокировками, восстанавливать
    # их не нужно (и нечем — attempts_seen/время утеряны).
    pass
