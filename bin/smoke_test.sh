#!/usr/bin/env bash
set -euo pipefail

APP=/opt/scanner
ENV_FILE="$APP/.env.production"
VALKEY_SOCK=/run/valkey/valkey.sock

if [[ -f "$ENV_FILE" ]]; then
  # shellcheck disable=SC1090
  source "$ENV_FILE"
fi

required_units=(
  scanner-valkey.service
  scanner-ingestor.service
  scanner-feature-worker.service
  scanner-alerter.service
  scanner-markouts.service
)

for unit in "${required_units[@]}"; do
  systemctl is-active --quiet "$unit"
done
echo "✓ systemd units active"

XLEN=$(valkey-cli -s "$VALKEY_SOCK" XLEN hl:trades)
if [[ "$XLEN" -le 0 ]]; then
  echo "hl:trades has no entries" >&2
  exit 1
fi
echo "✓ hl:trades XLEN=$XLEN"

psql "$POSTGRES_URL" -c "SELECT count(*) FROM alerts WHERE ts > now() - interval '1 hour';" >/dev/null
echo "✓ alerts query succeeded"

if [[ -z "${S3_BUCKET:-}" ]]; then
  echo "S3_BUCKET is not set" >&2
  exit 1
fi

FIRST_OBJECT=$(
  aws s3 ls "s3://$S3_BUCKET/cold/" --recursive 2>/dev/null | awk 'NR==1 {print; exit}' || true
)
if [[ -n "$FIRST_OBJECT" ]]; then
  echo "✓ first archived object: $FIRST_OBJECT"
else
  echo "ℹ no archived objects yet"
fi
