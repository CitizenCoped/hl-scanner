# hl-scanner

Production market scanner for Hyperliquid perpetual futures.

Four Python processes plus a sidecar Valkey, supervised by systemd on a single
EC2 instance in AWS ap-northeast-1 (Tokyo).

## Reading order

1. **BUILD_GUIDE.md** — the full architecture and rationale
2. **CURSOR_PROMPTS.md** — the 10 sequential prompts to scaffold the codebase
3. **.cursorrules** — persistent context for Cursor's agent

## Quick start

```bash
# 1. Provision AWS (one-time, ~5 min)
cd infra && ./provision.sh

# 2. SSH into the new EC2 box, bootstrap once
ssh -i ~/.ssh/hl_scanner_ed25519 ubuntu@$(terraform output -raw eip)
sudo bash bin/do_bootstrap.sh

# 3. Deploy code, start services
bin/deploy.sh
bin/smoke_test.sh
```

See BUILD_GUIDE.md §14 for the day-1-to-30 operational runbook.

Estimated cost: ~$219/month on a 1-year Compute Savings Plan.