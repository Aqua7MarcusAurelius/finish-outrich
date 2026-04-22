# Docker

> Всё окружение поднимается одной командой: `docker-compose up -d`.
> Все данные сохраняются между перезапусками через volumes.

---

## Сервисы

| Сервис | Что это | Порты наружу |
|---|---|---|
| `app` | Наше приложение (FastAPI + все модули) | `8000` (API + документация `/docs`) |
| `postgres` | База данных | — (только внутри Docker сети) |
| `redis` | Шина событий + статусы | — (только внутри Docker сети) |
| `minio` | Хранилище медиафайлов + веб-консоль | `9001` (веб-консоль для просмотра файлов) |

PostgreSQL и Redis не светятся наружу — доступны только внутри Docker сети между сервисами.

MinIO — один сервис с двумя портами: `9000` используется приложением для S3 API и не выставляется наружу, `9001` — веб-консоль для ручного просмотра bucket'ов.

---

## docker-compose.yml

```yaml
version: '3.9'

services:

  app:
    build: .
    ports:
      - "8000:8000"
    env_file:
      - .env
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy
      minio:
        condition: service_healthy
    volumes:
      - ./:/app
    restart: unless-stopped

  postgres:
    image: postgres:16
    environment:
      POSTGRES_DB: ${POSTGRES_DB}
      POSTGRES_USER: ${POSTGRES_USER}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
    volumes:
      - postgres_data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${POSTGRES_USER}"]
      interval: 5s
      timeout: 5s
      retries: 5
    restart: unless-stopped

  redis:
    image: redis:7
    volumes:
      - redis_data:/data
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 5s
      retries: 5
    restart: unless-stopped

  minio:
    image: minio/minio
    command: server /data --console-address ":9001"
    environment:
      MINIO_ROOT_USER: ${MINIO_ROOT_USER}
      MINIO_ROOT_PASSWORD: ${MINIO_ROOT_PASSWORD}
    ports:
      - "9001:9001"
    volumes:
      - minio_data:/data
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:9000/minio/health/live"]
      interval: 5s
      timeout: 5s
      retries: 5
    restart: unless-stopped

volumes:
  postgres_data:
  redis_data:
  minio_data:
```

---

## Как пользоваться

**Запустить всё:**
```bash
docker-compose up -d
```

**Остановить:**
```bash
docker-compose down
```

**Посмотреть логи приложения:**
```bash
docker-compose logs -f app
```

> Обычные события и бизнес-ошибки приложения идут через шину и доступны в `/events/stream`. Docker-логи — только для инфраструктурных ошибок старта.

**Пересобрать приложение после изменений в коде:**
```bash
docker-compose up -d --build app
```

---

## Что доступно после запуска

| Что | Адрес |
|---|---|
| API + документация | `http://localhost:8000/docs` |
| Веб-консоль MinIO | `http://localhost:9001` |

---

## Принципы которые мы зафиксировали

1. Все данные хранятся в Docker volumes — не теряются при перезапуске
2. Приложение стартует только после того как база, Redis и MinIO готовы (healthcheck)
3. PostgreSQL и Redis не светятся наружу — только внутри Docker сети
4. MinIO S3 API (порт 9000) тоже не светится наружу — только внутри сети
5. MinIO веб-консоль (порт 9001) доступна снаружи для ручного просмотра файлов
6. `restart: unless-stopped` — сервисы поднимаются автоматически если упали
