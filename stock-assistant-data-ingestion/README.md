# Stock Assistant Data Ingestion (SADI)

News ingestion and cleaning service for the HK Stock AI Research Assistant.

---

## Prerequisites

- [`uv`](https://github.com/astral-sh/uv) — `pip install uv`
- Docker and Docker Compose (for PostgreSQL + Redis)

## Setup

```bash
# Create and activate virtual environment
uv venv --python 3.12
source .venv/bin/activate

# Install dependencies
uv pip install -r requirements.txt

# Install playwright browsers (required for HKEX + MingPao crawlers)
playwright install chromium
```

Create a `.env` file in the project root (do not commit this file):

```env
DATABASE_URL=postgresql://postgres:password@localhost:5432/sadi
DB_POOL_SIZE=10
DB_MAX_RETRY=3
DB_RETRY_BASE_WAIT_MS=100
REDIS_URL=redis://localhost:6379

CRAWL_MAX_RETRY=3
CRAWL_RETRY_BASE_WAIT_MS=500
CRAWL_REQUEST_TIMEOUT_S=10

CLEAN_WORKER_CONCURRENCY=5
CLEAN_BODY_MIN_LENGTH=50
STREAM_CLAIM_TIMEOUT_MS=30000
```

## Running the service

```bash
# Start PostgreSQL and Redis
docker compose up -d postgres redis

# Run database migrations
alembic upgrade head

# Start the service
uvicorn app.main:app --reload --port 8000
```

## Running tests

```bash
# With venv activated
pytest

# Or without activating the venv
uv run pytest
```
