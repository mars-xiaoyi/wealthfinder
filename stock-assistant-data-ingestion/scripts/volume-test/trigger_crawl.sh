#!/bin/bash
# Trigger one SADI crawl via POST /v1/crawl and log the immediate response.
#
# Usage: trigger_crawl.sh <SOURCE_NAME>
#   SOURCE_NAME: HKEX | MINGPAO | AASTOCKS | YAHOO_HK  (per docs/api.md §2.3)
#
# Note: the VM's system timezone must be set to Asia/Hong_Kong (see Setup
# checklist in volume-test-plan.md) so `date +%F` here lines up with HKEX's
# HKT-local filing day — otherwise a crawl firing near midnight could pull
# the wrong day's batch.

set -euo pipefail

SOURCE_NAME="${1:?Usage: trigger_crawl.sh <SOURCE_NAME>}"
LOGDIR="/opt/sadi/logs"
mkdir -p "$LOGDIR"

# Kernel-provided UUID source — avoids depending on the `uuid-runtime`
# package (uuidgen), which isn't guaranteed present on a minimal cloud image.
EXECUTION_ID="$(cat /proc/sys/kernel/random/uuid)"
TODAY="$(date +%F)"
LOGFILE="$LOGDIR/trigger_${SOURCE_NAME}_${TODAY}.log"

echo "$(date -u +%FT%TZ) triggering source=${SOURCE_NAME} execution_id=${EXECUTION_ID} date=${TODAY}" | tee -a "$LOGFILE"

HTTP_CODE=$(curl -s -o "$LOGDIR/response_${SOURCE_NAME}_${TODAY}.json" -w "%{http_code}" \
  -X POST "http://localhost:8000/v1/crawl" \
  -H "Content-Type: application/json" \
  -d "{\"execution_id\": \"${EXECUTION_ID}\", \"source_name\": \"${SOURCE_NAME}\", \"date\": \"${TODAY}\"}")

echo "$(date -u +%FT%TZ) source=${SOURCE_NAME} http_status=${HTTP_CODE}" | tee -a "$LOGFILE"

if [ "$HTTP_CODE" != "202" ]; then
  echo "$(date -u +%FT%TZ) WARNING: unexpected status ${HTTP_CODE} for ${SOURCE_NAME} — see response_${SOURCE_NAME}_${TODAY}.json" | tee -a "$LOGFILE"
fi
