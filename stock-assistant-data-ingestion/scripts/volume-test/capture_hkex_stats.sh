#!/bin/bash
# Sample `docker stats` in the background while the HKEX crawl runs, then
# trigger the crawl. Opportunistic sanity-check only (see Revision note in
# volume-test-plan.md) — not used to validate box sizing, just to catch a
# crash-loop or OOM under the lowered worker concurrency.
#
# Sampling window: 15 min ceiling (36 x 25s), generous vs. the ~94-150s HKEX
# took in earlier live tests (progress.md, 2026-04-15) — stops early if the
# crawl finishes first isn't detected here on purpose, to keep this script
# simple; a few extra idle samples at the tail are harmless.

set -euo pipefail

LOGDIR="/opt/sadi/logs"
mkdir -p "$LOGDIR"
TODAY="$(date +%F)"
STATS_LOG="$LOGDIR/docker_stats_${TODAY}.log"

sample_stats() {
  for _ in $(seq 1 36); do
    {
      date -u +%FT%TZ
      docker stats --no-stream --format "table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}"
      echo
    } >> "$STATS_LOG"
    sleep 25
  done
}

sample_stats &
STATS_PID=$!

"$(dirname "$0")/trigger_crawl.sh" HKEX

wait "$STATS_PID"
