#!/usr/bin/env bash
# infra/destroy.sh — interactive, idempotent teardown
set -euo pipefail

REGION="ap-northeast-1"
ALLOWED_REGIONS=("ap-northeast-1")

red() { printf '\033[31m%s\033[0m\n' "$*"; }
bold() { printf '\033[1m%s\033[0m\n' "$*"; }

bold "=== HL Scanner DESTROY ==="
echo "This will permanently delete the EC2, EBS data volume, RDS database,"
echo "S3 bucket contents, IAM role, SSH key registration, and VPC in $REGION."
echo

read -r -p "AWS Access Key ID: " AWS_ACCESS_KEY_ID
read -r -s -p "AWS Secret Access Key: " AWS_SECRET_ACCESS_KEY
echo
read -r -p "AWS Session Token (blank if long-lived): " AWS_SESSION_TOKEN || true
export AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
export AWS_DEFAULT_REGION="$REGION"
export AWS_STS_REGIONAL_ENDPOINTS="regional"

aws sts get-caller-identity --endpoint-url "https://sts.${REGION}.amazonaws.com" >/dev/null

# Region guard
ok=0
for r in "${ALLOWED_REGIONS[@]}"; do
  [[ "$r" == "$REGION" ]] && ok=1
done
[[ $ok -eq 1 ]] || { red "REFUSING: $REGION not in whitelist."; exit 1; }

# Verify Terraform state region matches
cd "$(dirname "$0")"
TF_REGION=$(terraform output -raw region 2>/dev/null || true)
if [[ -n "$TF_REGION" && "$TF_REGION" != "$REGION" ]]; then
  red "REFUSING: terraform state shows region=$TF_REGION, expected $REGION."
  exit 1
fi

read -r -p "Type DESTROY to confirm: " confirm
[[ "$confirm" == "DESTROY" ]] || { echo "Aborted."; exit 0; }

# Drain the S3 bucket so terraform destroy can remove it
BUCKET=$(terraform output -raw s3_bucket 2>/dev/null || true)
if [[ -n "$BUCKET" ]]; then
  bold "Draining S3 bucket $BUCKET ..."
  aws s3 rm "s3://$BUCKET" --recursive || true
fi

ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
PUBKEY="$(cat ~/.ssh/hl_scanner_ed25519.pub 2>/dev/null || echo 'placeholder')"
MYIP="$(curl -s https://checkip.amazonaws.com)/32"

terraform destroy -auto-approve \
  -var "region=$REGION" \
  -var "account_id=$ACCOUNT" \
  -var "ssh_public_key=$PUBKEY" \
  -var "my_ip=$MYIP"

rm -f ../.env.production
echo "✓ Destroyed. Verify in the AWS console that no resources remain in $REGION."
