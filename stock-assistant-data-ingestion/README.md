# Stock Assistant Data Ingestion (SADI)

News ingestion and cleaning service for the HK Stock AI Research Assistant.

---

## Prerequisites

- [`uv`](https://github.com/astral-sh/uv) — `pip install uv`
- Docker and Docker Compose (for PostgreSQL + Redis) — Docker Desktop must be
  running before any `docker compose` command

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

## Running the service (first time)

```bash
# Start PostgreSQL and Redis
docker compose up -d postgres redis

# Load .env into the shell — nothing in the app auto-loads it; app/config.py
# and alembic/env.py both read os.environ directly
set -a && source .env && set +a

# Run database migrations
alembic upgrade head

# Start the service
uvicorn app.main:app --reload --port 8000
```

Verify it's up:

```bash
curl http://localhost:8000/v1/health
# {"status":"healthy", ...}
```

## Stopping and restarting

Stop everything — container data persists in the `pgdata` volume:

```bash
docker compose stop
# and Ctrl-C the running uvicorn process
```

Start again later — no need to repeat the one-time `Setup` steps, and no need
to re-run migrations unless the schema changed since you last stopped:

```bash
docker compose start postgres redis
source .venv/bin/activate
set -a && source .env && set +a
uvicorn app.main:app --reload --port 8000
```

To remove the containers entirely instead of just stopping them:

```bash
docker compose down        # keeps the pgdata volume — data survives
docker compose down -v     # also deletes pgdata — wipes the database
```

## Accessing Postgres and Redis

Run clients inside the running containers — no local `psql`/`redis-cli`
install needed:

```bash
# Postgres
docker compose exec postgres psql -U postgres -d sadi

# Redis
docker compose exec redis redis-cli
```

Useful once connected: `\dt` in `psql` lists `raw_news`, `cleaned_news`,
`crawl_error_log`; `KEYS stream:*` in `redis-cli` shows
`stream:raw_news_cleaned` / `stream:crawl_completed` once a crawl has run.

## Running tests

```bash
# With venv activated
pytest

# Or without activating the venv
uv run pytest
```
