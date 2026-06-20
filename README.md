# hl-scanner

Production market scanner for Hyperliquid perpetual futures.

Four Python processes plus a sidecar Valkey, supervised by systemd on a single
EC2 instance in AWS ap-northeast-1 (Tokyo).

## Reading order

1. **BUILD_GUIDE.md** — the full architecture and rationale
2. **CURSOR_PROMPTS.md** — the 10 sequential prompts to scaffold the codebase
3. **.cursorrules** — persistent context for Cursor's agent

This work is being applied to the repositiory below:
   Think about: https://github.com/google-research/timesfm
   What this is: https://x.com/antpalkin/status/2068083311667462618/video/1?s=46
   
Z-score the landscape of hyperliquid is a solid first and esential step -- makes sense to make the money while banging on the door of the bigest of opportunities and questions that will transend money itself -- this is data that is not currenetly published so I decided to make 'hlscanner' to collect it. 

Mental masturbation task: (for the few who will ever read this) 

IMPORTANT and WORTH THE USE OF MIND:

My goal is to drastically lower the compute and the expertise barrier required for world class forecasting. (incredible and bold goal for little me to work on - 'trust me I know' - i am shooting for the stars but if I miss I will land on the Moon - so look for me there when I fail and bring me back to earth to try again. 
  
BIG PICUTRE QUESTION/SENARIO:  

Are we are moving away from having a model per task to one model fits all for time sieres?

I keep coming back to synthetic training. The math curve:  20% of the training data was pure synthetic math teaching the model the fundamental grammar of reality. What happens when you start training these foundation models entirely on synthetic worlds?
  
If you are reading this I want you to ponder: Are we teaching AI to predict our human reality? OR.... Are we just teaching AI the mathematical rules that underpin a simulation?

This IS the deepest of rabbit holes: if a math curve predicts human behavior better than human history then maybe... (My understading is leaning to believe) that the math is the actual reality. <---- smoke this in your pipe tonight... 
  
A provocative thought with huge implifications if True - a real world plan is found here in 'hlscanner' to 'honor the dollar'.

let me know your thoughts. Special thank you to all of you who have helped me design hlscanner. 

  
  peace & love always

  
   

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
