#!/bin/bash
# One-shot setup script for the volume-test VPS — takes a fresh GCP VM from
# bare OS to fully running + cron-scheduled. Run once via SSH after the VM
# is created and the repo is present. Safe to re-run (each step is a no-op
# or idempotent if already done).
#
# Usage: cd /opt/sadi && ./scripts/volume-test/deploy.sh
#
# Does NOT create the VM/firewall rule or get the repo onto it — those are
# still manual (gcloud/console + git clone or scp), see Setup checklist in
# docs/volume-test-plan.md.

set -euo pipefail

REPO_DIR="/opt/sadi"
COMPOSE_FILE="$REPO_DIR/docker-compose-dev.yml"
cd "$REPO_DIR"

echo "=== 1/7: Timezone -> Asia/Hong_Kong ==="
sudo timedatectl set-timezone Asia/Hong_Kong

echo "=== 2/7: Docker install + group membership ==="
if ! command -v docker &>/dev/null; then
  curl -fsSL https://get.docker.com | sudo sh
fi
sudo usermod -aG docker "$USER"
# `sg docker` activates the new group membership for the rest of *this*
# script's docker commands without needing a fresh login. Cron jobs started
# later read group membership fresh at launch time, so they need no
# equivalent — this is only needed here, in the current shell.
dc() { sg docker -c "docker compose -f $COMPOSE_FILE $*"; }

echo "=== 3/7: .env (CLEAN_WORKER_CONCURRENCY=1, per Revision note) ==="
[ -f .env ] || cp .env.example .env
if grep -q '^CLEAN_WORKER_CONCURRENCY=' .env; then
  sed -i 's/^CLEAN_WORKER_CONCURRENCY=.*/CLEAN_WORKER_CONCURRENCY=1/' .env
else
  echo 'CLEAN_WORKER_CONCURRENCY=1' >> .env
fi

echo "=== 4/7: Starting the stack (builds sadi image, runs playwright install chromium) ==="
dc "up -d --build"
sleep 5   # let postgres/redis healthchecks + sadi's own startup settle

echo "=== 5/7: Database migrations ==="
dc "exec -T sadi alembic upgrade head"

echo "=== 6/7: Smoke test — confirms migrations + crawl + Playwright/Chromium all actually work ==="
curl -sf http://localhost:8000/v1/health >/dev/null && echo "  /v1/health OK" \
  || { echo "  FAILED: SADI not healthy — stop and investigate before continuing"; exit 1; }

echo "  triggering one manual HKEX crawl..."
mkdir -p "$REPO_DIR/logs"
"$REPO_DIR/scripts/volume-test/trigger_crawl.sh" HKEX
echo "  waiting 60s for it to complete..."
sleep 60
ROW_COUNT=$(dc "exec -T postgres psql -U postgres -d sadi -tAc 'SELECT count(*) FROM raw_news;'")
echo "  raw_news row count: $ROW_COUNT"
if [ "$ROW_COUNT" -eq 0 ]; then
  echo "  WARNING: 0 rows after the smoke-test crawl — check logs/trigger_HKEX_*.log and"
  echo "  'docker compose -f $COMPOSE_FILE logs sadi' before trusting the unattended run."
fi

echo "=== 7/7: Installing cron schedule ==="
crontab "$REPO_DIR/scripts/volume-test/crontab.txt"
echo "Installed crontab:"
crontab -l

echo
echo "Done. Review the smoke-test output above before walking away from this box."
