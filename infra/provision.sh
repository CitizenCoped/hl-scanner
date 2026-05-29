#!/usr/bin/env bash
# infra/provision.sh — one-command interactive provisioning for the HL scanner.
set -euo pipefail

REGION="ap-northeast-1"
ALLOWED_REGIONS=("ap-northeast-1")

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
red() { printf '\033[31m%s\033[0m\n' "$*"; }
grn() { printf '\033[32m%s\033[0m\n' "$*"; }

bold "=== HL Scanner provisioning (AWS ap-northeast-1) ==="
echo
echo "Will provision: VPC, subnets, security groups, EC2+EIP+200GB EBS,"
echo "RDS PostgreSQL, S3 bucket, IAM role+instance profile, SSH key."
echo "Estimated bill: ~\$219/month."
echo

# 1. Interactively collect credentials (never silently read env vars)
read -r -p "AWS Access Key ID: " AWS_ACCESS_KEY_ID
read -r -s -p "AWS Secret Access Key: " AWS_SECRET_ACCESS_KEY
echo
read -r -p "AWS Session Token (blank if long-lived key): " AWS_SESSION_TOKEN || true
export AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
export AWS_DEFAULT_REGION="$REGION"
export AWS_STS_REGIONAL_ENDPOINTS="regional"

# 2. Validate credentials BEFORE spending money — using the regional STS endpoint
bold "Validating credentials against sts.${REGION}.amazonaws.com ..."
CALLER=$(aws sts get-caller-identity --endpoint-url "https://sts.${REGION}.amazonaws.com" --output json)
ACCOUNT=$(echo "$CALLER" | python3 -c 'import sys,json;print(json.load(sys.stdin)["Account"])')
ARN=$(echo "$CALLER" | python3 -c 'import sys,json;print(json.load(sys.stdin)["Arn"])')
grn "✓ Authenticated as: $ARN  (account $ACCOUNT)"

# 3. Region drift guard
CONFIGURED_REGION=$(aws configure get region 2>/dev/null || true)
if [[ -n "$CONFIGURED_REGION" && "$CONFIGURED_REGION" != "$REGION" ]]; then
  red "REFUSING: ~/.aws/config region=$CONFIGURED_REGION but we require $REGION."
  red "Edit ~/.aws/config or unset it, then re-run."
  exit 1
fi
ok=0
for r in "${ALLOWED_REGIONS[@]}"; do
  [[ "$r" == "$REGION" ]] && ok=1
done
[[ $ok -eq 1 ]] || { red "REFUSING: $REGION not in whitelist."; exit 1; }
grn "✓ Region pinned to $REGION"

# 4. Ensure SSH key exists locally
KEY="$HOME/.ssh/hl_scanner_ed25519"
[[ -f "$KEY" ]] || ssh-keygen -t ed25519 -N '' -f "$KEY"
PUBKEY=$(cat "${KEY}.pub")

# 5. Plan summary + confirmation
bold "=== Plan summary ==="
cat <<EOF
  Region:          $REGION
  EC2:             c7g.2xlarge, 200 GB gp3, Elastic IP, public subnet
  RDS:             PostgreSQL 16, db.t4g.micro, Single-AZ, 20 GB gp3
  S3:              hl-scanner-${ACCOUNT}-${REGION} (STD→IA@30d→GIR@180d)
  VPC:             10.10.0.0/16, one public, two private subnets (RDS)
  NAT Gateway:     none (intentional)
  Estimated cost:  ~\$219/month (1-yr Compute Savings Plan applied separately)
EOF
read -r -p "Proceed? [y/N]: " yn
[[ "$yn" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 0; }

# 6. Run Terraform
cd "$(dirname "$0")"
terraform init -input=false
terraform apply -input=false -auto-approve \
  -var "region=$REGION" \
  -var "account_id=$ACCOUNT" \
  -var "ssh_public_key=$PUBKEY" \
  -var "my_ip=$(curl -s https://checkip.amazonaws.com)/32"

# 7. Emit .env.production
EIP=$(terraform output -raw eip)
RDS_HOST=$(terraform output -raw rds_endpoint)
RDS_PW=$(terraform output -raw rds_password)
S3_BUCKET=$(terraform output -raw s3_bucket)
cat > ../.env.production <<EOF
AWS_REGION=$REGION
EC2_HOST=$EIP
POSTGRES_URL=postgresql://scanner:${RDS_PW}@${RDS_HOST}:5432/scanner?sslmode=require
S3_BUCKET=$S3_BUCKET
VALKEY_URL=unix:///run/valkey/valkey.sock
AWS_STS_REGIONAL_ENDPOINTS=regional
AWS_DEFAULT_REGION=$REGION
EOF
chmod 600 ../.env.production
grn "✓ Wrote ../.env.production"

bold "=== Next steps ==="
cat <<EOF
  1. ssh -i $KEY ubuntu@$EIP
  2. git clone <your-repo> /opt/scanner/app && cd /opt/scanner/app
  3. sudo bash bin/do_bootstrap.sh
  4. scp ../.env.production ubuntu@$EIP:/opt/scanner/.env.production
  5. sudo systemctl restart 'scanner-*'
  6. bash bin/smoke_test.sh
EOF
