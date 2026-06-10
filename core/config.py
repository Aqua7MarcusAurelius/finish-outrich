"""
Загрузка переменных окружения.

Всё читается из .env один раз при старте приложения.
Настройки поведения модулей (которые меняются без перезапуска) живут
не здесь, а в таблице `settings` в БД — см. database_schema.md.
"""
import json
from typing import List

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Приложение
    APP_ENV: str = "development"
    APP_PORT: int = 8000
    API_TOKEN: str = ""
    DOCS_PUBLIC: bool = True

    # CORS — список через запятую
    CORS_ORIGINS: str = "http://localhost:3000"

    # PostgreSQL
    POSTGRES_HOST: str = "postgres"
    POSTGRES_PORT: int = 5432
    POSTGRES_DB: str = "tgframework"
    POSTGRES_USER: str = "tgframework"
    POSTGRES_PASSWORD: str = ""

    # Redis
    REDIS_HOST: str = "redis"
    REDIS_PORT: int = 6379

    # MinIO
    MINIO_HOST: str = "minio"
    MINIO_PORT: int = 9000
    MINIO_ROOT_USER: str = ""
    MINIO_ROOT_PASSWORD: str = ""
    MINIO_BUCKET: str = "tgframework"
    # Отдельный бакет для HTML-отчётов автодиалогов, завершённых LLM
    # маркером <finishdialog/>. Хранятся долго (в отличие от медиа в
    # MINIO_BUCKET, где TTL 3 дня) — отдельный бакет нужен чтобы у них
    # не пересекались политики lifecycle.
    MINIO_REPORTS_BUCKET: str = "autochat-reports"

    # Telegram API (общие креды для всех аккаунтов, берутся с my.telegram.org)
    TELEGRAM_API_ID: int = 0
    TELEGRAM_API_HASH: str = ""

    # OpenRouter
    OPENROUTER_API_KEY: str = ""
    OPENROUTER_MODEL_TRANSCRIPTION: str = "openai/whisper"
    OPENROUTER_MODEL_DESCRIPTION: str = "openai/gpt-4o"
    OPENROUTER_MODEL_DESCRIPTION_DOCUMENTS: str = "google/gemini-2.5-flash"
    # Модель для автодиалогов (chat_completion). Точное имя уточнить в
    # каталоге OpenRouter — на момент релиза Opus 4.7 ожидается как
    # "anthropic/claude-opus-4.7" (или похожее). При несовпадении —
    # поменять только в .env без правок кода.
    OPENROUTER_MODEL_AUTOCHAT: str = "anthropic/claude-opus-4.7"
    # Модель для оценщика лидов (модуль lead_evaluator). Дефолт — тот же
    # Opus 4.7. Меняется независимо от autochat для тюнинга качества.
    OPENROUTER_MODEL_EVALUATOR: str = "anthropic/claude-opus-4.7"

    # ── Партнёрская CRM (этап 10) ──────────────────────────────────────
    # Адрес и токен для модуля partner_crm. Полное описание API:
    # docs/INT_CRM_API_TOKEN_GUIDE.md.
    CRM_BASE_URL: str = ""
    CRM_API_TOKEN: str = ""

    # ── Доставка success-отчётов в TG-группы (этап 10.20) ──────────────
    # Сервисный Telegram-аккаунт, который состоит в группах выгрузки и
    # заливает туда HTML-отчёты по удачным диалогам. Заводится обычным
    # мастером «новый аккаунт»; здесь — только его имя (accounts.name),
    # чтобы модуль report_delivery нашёл строку и скрыл аккаунт из UI.
    SERVICE_ACCOUNT_NAME: str = "service"
    # Карта «семейство воркера → chat_id группы выгрузки», JSON-объект.
    # Ключ = префикс имени воркера до '_' (SAD_03_RU → SAD), регистр любой;
    # одна группа обслуживает всех воркеров семейства. Значение = числовой
    # id приватной группы (напр. -1001234567890). Пусто = доставка выключена.
    # Пример: REPORT_GROUP_MAP={"SAD": -1001234567890, "JOKER": -1009876543210}
    REPORT_GROUP_MAP: str = ""

    # ── Вычисляемые свойства ─────────────────────────────────────────

    @property
    def postgres_dsn(self) -> str:
        """Sync DSN — используется Alembic-ом."""
        return (
            f"postgresql+psycopg2://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )

    @property
    def postgres_dsn_async(self) -> str:
        """DSN для asyncpg — без префикса драйвера."""
        return (
            f"postgresql://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )

    @property
    def redis_url(self) -> str:
        return f"redis://{self.REDIS_HOST}:{self.REDIS_PORT}"

    @property
    def minio_endpoint(self) -> str:
        return f"{self.MINIO_HOST}:{self.MINIO_PORT}"

    @property
    def cors_origins_list(self) -> List[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    @property
    def report_group_map(self) -> dict[str, int]:
        """
        Распарсенная карта `семейство → chat_id` из REPORT_GROUP_MAP.

        Пустая строка / битый JSON / не-объект → пустой dict (доставка
        просто молчит, приложение не роняем — модуль report_delivery
        логирует факт пустой/битой карты на старте). Значения → int.
        """
        raw = (self.REPORT_GROUP_MAP or "").strip()
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except Exception:
            return {}
        if not isinstance(data, dict):
            return {}
        out: dict[str, int] = {}
        for key, value in data.items():
            try:
                out[str(key)] = int(value)
            except (TypeError, ValueError):
                continue
        return out


settings = Settings()