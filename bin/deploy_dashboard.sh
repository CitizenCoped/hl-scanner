#!/usr/bin/env bash
# bin/deploy_dashboard.sh — upload the static dashboard SPA to the dashboard
# S3 bucket and invalidate the CloudFront cache for index.html.
set -euo pipefail

REGION="ap-northeast-1"
HERE="$(cd "$(dirname "$0")/.." && pwd)"

BUCKET="${DASHBOARD_BUCKET:-}"
if [[ -z "$BUCKET" ]]; then
  BUCKET=$(terraform -chdir="$HERE/infra" output -raw dashboard_bucket)
fi

aws s3 cp "$HERE/dashboard/index.html" "s3://$BUCKET/index.html" \
  --region "$REGION" \
  --content-type "text/html" \
  --cache-control "public, max-age=60"

DIST_ID=$(aws cloudfront list-distributions \
  --query "DistributionList.Items[?Comment=='hl-scanner dashboard'].Id | [0]" \
  --output text --region "$REGION" 2>/dev/null || true)
if [[ -n "$DIST_ID" && "$DIST_ID" != "None" ]]; then
  aws cloudfront create-invalidation --distribution-id "$DIST_ID" --paths "/index.html" >/dev/null
  echo "Invalidated /index.html on $DIST_ID"
fi

echo "Dashboard deployed to s3://$BUCKET/index.html"
