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

# Install dependencies — from the lockfile for a reproducible install
# (exact transitive versions + hashes; use requirements-test.lock instead to
# also get pytest/pytest-asyncio). requirements.txt/-test.txt stay the source
# of direct dependencies — add new ones there first, per CLAUDE.md.
uv pip sync requirements.lock

# Install playwright browsers (required for HKEX + MingPao crawlers)
playwright install chromium
```

After adding or changing a dependency in `requirements.txt`/`requirements-test.txt`,
regenerate the corresponding lockfile:

```bash
uv pip compile requirements.txt -o requirements.lock --generate-hashes
uv pip compile requirements-test.txt -o requirements-test.lock --generate-hashes
```

Copy `.env.example` to `.env` (the latter is gitignored — never commit it) and
adjust as needed:

```bash
cp .env.example .env
```

See `.env.example` for the full list of variables and their defaults — it's the
single source of truth; this file doesn't duplicate it so the two can't drift.

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
