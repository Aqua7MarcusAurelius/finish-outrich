"""
Загрузка переменных окружения.

Всё читается из .env один раз при старте приложения.
Настройки поведения модулей (которые меняются без перезапуска) живут
не здесь, а в таблице `settings` в БД — см. database_schema.md.
"""
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

    # OpenRouter
    OPENROUTER_API_KEY: str = ""
    OPENROUTER_MODEL_TRANSCRIPTION: str = "openai/whisper"
    OPENROUTER_MODEL_DESCRIPTION: str = "openai/gpt-4o"

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


settings = Settings()
