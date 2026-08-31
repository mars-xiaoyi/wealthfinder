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

## Testing

### Unit tests

```bash
# With venv activated
pytest

# Or without activating the venv
uv run pytest
```

Live integration tests (real network, one per crawler) are opt-in and skipped
by default:

```bash
pytest -m live -v
```

### End-to-end local testing

`pytest` mocks the DB/Redis, so it doesn't exercise the real running stack —
API → crawler → DB → cleaning pipeline → Redis stream → retrieval API. For
that, use the phase scripts in [`scripts/local-test/`](scripts/local-test/)
against a running stack (see `Running the service` above). Each phase is
independent and can be re-run on its own; run them in order the first time,
since later phases assume data from earlier ones.

```bash
./scripts/local-test/00-automated.sh     # pytest (unit) + pytest -m live
./scripts/local-test/01-health.sh        # GET /v1/health
./scripts/local-test/02-crawl.sh         # POST /v1/crawl for all 4 sources + validation errors
./scripts/local-test/03-cleaning.sh      # cleaned_news populated, stream:raw_news_cleaned published
./scripts/local-test/04-retrieval.sh     # GET/POST /v1/cleaned_news, incl. 404 + validation error
./scripts/local-test/05-idempotency.sh   # re-crawl doesn't duplicate raw_news rows
./scripts/local-test/06-resilience.sh    # DB/Redis down -> 503s; disruptive but self-healing
./scripts/local-test/07-docker.sh        # docker build + docker compose up --build, the actual deploy artifact
```

`HKEX_DATE` (02-crawl.sh), a source name arg (05-idempotency.sh), and
`SADI_BASE_URL` (default `http://localhost:8000`, all scripts) can be
overridden — see each script's header comment. Phases 0-6 passing gives
functional confidence; phase 7 additionally confirms the actual Docker image
works, not just the host-run dev server.
