# hl-scanner

Production market Zscore scanner and research tool for Hyperliquid perpetual futures. 

Four Python processes plus a sidecar Valkey, supervised by systemd on a single
EC2 instance in AWS ap-northeast-1 (Tokyo). Hyperliquid's validators are bassed in Tokyo - lowest latency

## Reading order

1. **BUILD_GUIDE.md** — the full architecture and rationale
2. **CURSOR_PROMPTS.md** — the 10 sequential prompts to scaffold the codebase
3. **.cursorrules** — persistent context for Cursor's agent

   
I think it will be vauable to Z-score the entire landscape of hyperliquid. All the assets. Compared to themselves and their own behavior.  - this is data that is not currenetly published so I decided to make 'hlscanner' to collect it. bars/chart/Standard deviation - it's a Dex that moves  fast like a centralized exchange + LEVERAGE. this is the future of trading. 


Will publish results of this experiment - unless the alpha is absolutely ridiculous and I'm gonna keep it to myself and take all of your money I'm just kidding. I'll publish it. I'll let you guys know how I did with it. 


  

  peace & love 

  
   

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
