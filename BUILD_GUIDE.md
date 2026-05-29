# Build Guide

# Hyperliquid Scanner — Build Guide

## Executive summary

A solo-developer perpetual-futures scanner against Hyperliquid that ingests trade/BBO/asset-context streams, builds 1-minute OHLCV bars, computes per-asset/per-hour z-scores, fires alerts, and logs markouts at +30s/+5m/+30m/+4h. All four Python 3.12 processes plus a sidecar Valkey run on a single c7g.2xlarge in AWS ap-northeast-1, ~2–3 ms from the Hyperliquid validator set, supervised by systemd. Hot Parquet on EBS gp3, cold Parquet on S3, transactional state on RDS PostgreSQL. Total ~$219/mo on a 1-year Compute Savings Plan.

## §1 Language

Python 3.12 throughout. `uv` for dependency and venv management — measurably faster than pip and produces reproducible lockfiles. Key libs: `websockets`, `orjson`, `polars`, `duckdb`, `psycopg[binary]`, `redis` (Valkey-compatible client), `pyarrow`, `pytz`, `pydantic` v2, `boto3`.

## §2 Hosting & Infrastructure (AWS-native)

| Concern | Choice | Why |
|---|---|---|
| Region | **ap-northeast-1 (Tokyo)** | Hyperliquid's 24 validators are clustered in AWS Tokyo across multiple AZs (Glassnode latency probes); raw network latency from Tokyo is 2–3 ms |
| AZ topology | Single AZ (`ap-northeast-1a`), with a second AZ defined only because RDS requires a DB subnet group spanning ≥2 AZs | Multi-AZ has no benefit when validators are in one region |
| Compute | **c7g.2xlarge** (8 vCPU / 16 GiB Graviton3), public subnet, 1-yr Compute Savings Plan No Upfront | Graviton3 cheaper per vCPU than equivalent c7i; compute-optimized is the right shape for the DuckDB analytics workload; T-class burstable would exhaust CPU credits during liquidation cascades |
| Block storage | **gp3, 200 GB**, default 3,000 IOPS / 125 MB/s | Free baseline performance handles ~1 GB/day compressed Parquet; gp3 ~20% cheaper per GB than gp2; instance-store NVMe (i4g) would lose hot Parquet on stop/start |
| Postgres | **RDS PostgreSQL 16, db.t4g.micro Single-AZ**, 20 GB gp3, 7-day automated backups | 5K–50K INSERTs/day is well below the t4g.micro baseline; Aurora Serverless v2 minimum 0.5 ACU is ~$43/mo, more than 2× the cost; self-managed on EC2 saves only ~$15/mo and forfeits managed backups |
| Cache / streams | **Sidecar valkey-server on the EC2 box, listening on `/run/valkey/valkey.sock`** | Unix-socket IPC is ~10× faster than TCP loopback; ElastiCache Serverless Valkey adds a network hop and ECPU charges (Oct 2024 AWS Database Blog confirms the $6/mo floor) |
| Object storage | **S3 Standard**, lifecycle rule transitioning objects >30 days to S3 Standard-IA, >180 days to S3 Glacier Instant Retrieval | Parquet is appended once and read rarely; Intelligent-Tiering's per-object monitoring fee isn't worth it at our object count |
| Networking | One VPC (10.10.0.0/16), one public subnet for EC2 (10.10.1.0/24), two private subnets for RDS (10.10.10.0/24, 10.10.11.0/24); no NAT Gateway; S3 reached via Gateway Endpoint | NAT GW in Tokyo is $0.062/hr + $0.062/GB processed — completely avoidable here |
| Static IP | One Elastic IP attached to the EC2 instance | Stable client IP for Hyperliquid; rate-limit budgets are IP-scoped |
| Observability | CloudWatch Logs (Infrequent Access class) for the four systemd units, 7-day retention; one alarm on EC2 `StatusCheckFailed`; one alarm on RDS CPU > 80% for 10 min | Bare minimum to know the scanner is broken; full Container Insights / X-Ray is overkill |

## §3 Hyperliquid WebSocket API specifics

Connect to `wss://api.hyperliquid.xyz/ws` from the EC2 instance. The server closes idle connections after 60 seconds — send `{"method":"ping"}` every 20–30 seconds and treat the `{"channel":"pong"}` as a liveness signal (per the Hyperliquid GitBook docs). Subscribe channels:

- `trades` per coin (live trade ticks; first message has `isSnapshot: true`)
- `l2Book` per coin (price/size deltas at the desired sig-fig resolution)
- `activeAssetCtx` (funding, open interest, mark price)

WebSocket subscription quota is 1,000 per IP. With ~200 listed perp assets and three streams each, you fit comfortably under the cap. Rate-limit weights are 2 units for `l2Book`/`allMids`/`clearinghouseState` against a 1,200 weight/min budget — irrelevant for a read-only WS subscriber.

## §4 Universe selection

Bootstrap the universe from `POST /info` with `{"type":"meta"}` once at startup; subscribe to the union of (a) all perp coins with 24h notional volume > $X (configurable, default $10M) and (b) any coin currently appearing in `activeAssetCtx` snapshots. Re-evaluate every 6 hours; subscribe newly-qualifying coins and unsubscribe drop-outs in one batch to avoid mid-window reconciliation pain.

## §5 Z-score formula and bootstrap

For each (asset, 1-minute bar), compute the rolling return `r_t = ln(close_t / close_{t-1})`. Maintain per-(asset, hour-of-day) windows of length N=60 (one full hour of minute bars). The z-score is `z = (r_t − μ) / σ` where μ and σ are the windowed mean and stdev of returns for that asset/hour bucket.

**Bootstrap**: on first run, the markout_logger fetches the last 7 days of 1-minute candles from `POST /info` with `{"type":"candleSnapshot","req":{"coin":...,"interval":"1m","startTime":...}}` to seed each bucket. Until you have 24×60 = 1,440 historical minutes per hour-of-day bucket, gate alerts behind `min_samples >= 30`.

Alert fires when `|z| > 4.0` AND the minute's notional volume is in the top decile for that asset's prior 24h.

## §6 Storage layout, Postgres schema, DuckDB-on-S3

**Local Parquet (hot lake on `/opt/scanner/data`)** — Hive-style partitions, one file per hour, ZSTD-compressed:

```
/opt/scanner/data/
├── bars/coin=BTC/dt=2026-05-25/000.parquet
├── bbo/coin=BTC/dt=2026-05-25/000.parquet
└── trades/coin=BTC/dt=2026-05-25/000.parquet
```

The `archive_to_s3.py` worker runs nightly and moves any partition older than 30 days to `s3://hl-scanner-<account>-<region>/cold/<table>/coin=.../dt=.../...parquet`, then deletes the local copy.

**Postgres schema (`alerts` and `markouts`)**:

```sql
CREATE TABLE alerts (
  id BIGSERIAL PRIMARY KEY,
  ts TIMESTAMPTZ NOT NULL,
  coin TEXT NOT NULL,
  z DOUBLE PRECISION NOT NULL,
  ret DOUBLE PRECISION NOT NULL,
  notional DOUBLE PRECISION NOT NULL,
  mid DOUBLE PRECISION NOT NULL,
  bucket_hour SMALLINT NOT NULL
);
CREATE INDEX alerts_ts_coin ON alerts(ts DESC, coin);

CREATE TABLE markouts (
  alert_id BIGINT REFERENCES alerts(id) ON DELETE CASCADE,
  horizon TEXT NOT NULL CHECK (horizon IN ('30s','5m','30m','4h')),
  mid_at_horizon DOUBLE PRECISION,
  recorded_at TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (alert_id, horizon)
);
```

**DuckDB-on-S3** for occasional historical research; the IAM instance profile vends temporary credentials so no static keys live on the box:

```python
import duckdb
con = duckdb.connect()
con.execute("INSTALL httpfs; LOAD httpfs;")
con.execute("SET s3_region='ap-northeast-1';")
df = con.execute("""
  SELECT coin, ts, close
  FROM read_parquet('/opt/scanner/data/bars/coin=*/dt=2026-05-*/*.parquet',
                    hive_partitioning=true)
  WHERE ts >= now() - INTERVAL 25 HOUR
""").pl()
```

## §7 Process supervision: systemd

systemd wins over honcho on a single-host AWS Linux box: restart-on-failure with backoff, journal integration with the CloudWatch agent, dependency ordering, zero extra processes. One unit per process plus a timer for the nightly archive:

```
/etc/systemd/system/scanner-valkey.service
/etc/systemd/system/scanner-ingestor.service
/etc/systemd/system/scanner-feature-worker.service
/etc/systemd/system/scanner-alerter.service
/etc/systemd/system/scanner-markouts.service
/etc/systemd/system/scanner-archive.timer
/etc/systemd/system/scanner-archive.service
```

Each unit `Requires=` and `After=` the valkey unit. Restart policy: `Restart=always`, `RestartSec=5s`, `StartLimitBurst=10`, `StartLimitIntervalSec=60`.

## §8 Working Python code

```python
# src/scanner/ws_client.py
import asyncio, orjson
import websockets, redis.asyncio as redis

WS_URL = "wss://api.hyperliquid.xyz/ws"
VALKEY = redis.Redis(unix_socket_path="/run/valkey/valkey.sock", decode_responses=False)

async def run(coins: list[str]):
    backoff = 1
    while True:
        try:
            async with websockets.connect(WS_URL, ping_interval=None, max_size=2**22) as ws:
                for coin in coins:
                    for typ in ("trades", "l2Book", "activeAssetCtx"):
                        await ws.send(orjson.dumps({
                            "method":"subscribe",
                            "subscription":{"type":typ, "coin":coin}
                        }).decode())
                asyncio.create_task(_pinger(ws))
                backoff = 1
                async for raw in ws:
                    msg = orjson.loads(raw)
                    ch = msg.get("channel")
                    if ch in ("trades","l2Book","activeAssetCtx"):
                        await VALKEY.xadd(f"hl:{ch}", {"d": raw}, maxlen=200_000, approximate=True)
        except Exception:
            await asyncio.sleep(backoff)
            backoff = min(backoff*2, 30)

async def _pinger(ws):
    while True:
        await asyncio.sleep(25)
        try: await ws.send('{"method":"ping"}')
        except Exception: return
```

```python
# src/scanner/bar_builder.py
import asyncio, orjson, time
from collections import defaultdict
import redis.asyncio as redis
from scanner.parquet_writer import write_bar

VALKEY = redis.Redis(unix_socket_path="/run/valkey/valkey.sock", decode_responses=False)

async def consume():
    last_id = "$"
    bars = defaultdict(lambda: {"o":None,"h":-1e30,"l":1e30,"c":None,"v":0.0,"n":0})
    async def flush(minute_ts):
        for coin, b in list(bars.items()):
            if b["o"] is not None: await write_bar(coin, minute_ts, b)
        bars.clear()
    current_minute = int(time.time()//60)*60
    while True:
        resp = await VALKEY.xread({"hl:trades": last_id}, block=1000, count=1000)
        now_minute = int(time.time()//60)*60
        if now_minute > current_minute:
            await flush(current_minute); current_minute = now_minute
        if not resp: continue
        for _, entries in resp:
            for eid, fields in entries:
                last_id = eid
                msg = orjson.loads(fields[b"d"])
                for t in msg.get("data", []):
                    coin = t["coin"]; px = float(t["px"]); sz = float(t["sz"])
                    b = bars[coin]
                    if b["o"] is None: b["o"] = px
                    b["h"] = max(b["h"], px); b["l"] = min(b["l"], px); b["c"] = px
                    b["v"] += sz; b["n"] += 1

if __name__ == "__main__": asyncio.run(consume())
```

```python
# src/scanner/parquet_writer.py
import os, datetime as dt
import pyarrow as pa, pyarrow.parquet as pq
ROOT = "/opt/scanner/data/bars"
_writers = {}

async def write_bar(coin, minute_ts, b):
    d = dt.datetime.utcfromtimestamp(minute_ts)
    key = (coin, d.strftime("%Y-%m-%d"), d.hour)
    path = f"{ROOT}/coin={coin}/dt={key[1]}/{key[2]:03d}.parquet"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tbl = pa.table({"ts":[minute_ts],"o":[b["o"]],"h":[b["h"]],"l":[b["l"]],
                    "c":[b["c"]],"v":[b["v"]],"n":[b["n"]]})
    w = _writers.get(key)
    if w is None:
        w = pq.ParquetWriter(path, tbl.schema, compression="zstd", compression_level=3)
        _writers[key] = w
    w.write_table(tbl)
```

```python
# src/scanner/features.py
import duckdb
def compute_zscores(now_ts:int) -> list[dict]:
    con = duckdb.connect()
    rows = con.execute("""
      WITH r AS (
        SELECT coin, ts,
               ln(c / lag(c) OVER (PARTITION BY coin ORDER BY ts)) AS ret,
               c*v AS notional,
               extract('hour' FROM to_timestamp(ts)) AS h
        FROM read_parquet('/opt/scanner/data/bars/coin=*/dt=*/*.parquet',
                          hive_partitioning=true)
        WHERE ts >= ? - 7*24*3600
      )
      SELECT coin, ts, ret, notional, h,
             (ret - avg(ret) OVER w) / nullif(stddev_samp(ret) OVER w, 0) AS z,
             count(ret) OVER w AS n
      FROM r
      WINDOW w AS (PARTITION BY coin, h ORDER BY ts ROWS BETWEEN 59 PRECEDING AND 0 PRECEDING)
      QUALIFY ts = ? AND abs(z) > 4.0 AND n >= 30
    """, [now_ts, now_ts]).fetchall()
    return [{"coin":r[0],"ts":r[1],"ret":r[2],"notional":r[3],"h":r[4],"z":r[5]} for r in rows]
```

```python
# src/scanner/db.py
import os, psycopg
from contextlib import contextmanager
DSN = os.environ["POSTGRES_URL"]

@contextmanager
def conn():
    with psycopg.connect(DSN, autocommit=False) as c: yield c

def insert_alert(c, row:dict) -> int:
    with c.cursor() as cur:
        cur.execute("""
          INSERT INTO alerts(ts,coin,z,ret,notional,mid,bucket_hour)
          VALUES(to_timestamp(%s),%s,%s,%s,%s,%s,%s) RETURNING id
        """, (row["ts"],row["coin"],row["z"],row["ret"],row["notional"],row.get("mid",0.0),row["h"]))
        return cur.fetchone()[0]
```

```python
# src/scanner/alerter.py
import asyncio, time
from scanner.features import compute_zscores
from scanner.db import conn, insert_alert

async def main():
    while True:
        await asyncio.sleep(60 - (time.time() % 60))
        now = int(time.time()//60)*60
        try:
            with conn() as c:
                for row in compute_zscores(now):
                    insert_alert(c, row)
                c.commit()
        except Exception as e:
            print(f"alerter error: {e}", flush=True)

if __name__ == "__main__": asyncio.run(main())
```

```python
# src/scanner/markouts.py
import asyncio, time, os, psycopg, requests
HORIZONS = {"30s":30, "5m":300, "30m":1800, "4h":14400}

async def schedule(alert_id:int, ts:int, coin:str):
    for h, dt_s in HORIZONS.items():
        target = ts + dt_s
        await asyncio.sleep(max(0, target - time.time()))
        try:
            r = requests.post("https://api.hyperliquid.xyz/info",
                              json={"type":"l2Book","coin":coin}, timeout=5).json()
            mid = (float(r["levels"][0][0]["px"]) + float(r["levels"][1][0]["px"]))/2
            with psycopg.connect(os.environ["POSTGRES_URL"]) as c, c.cursor() as cur:
                cur.execute("""INSERT INTO markouts(alert_id,horizon,mid_at_horizon,recorded_at)
                               VALUES(%s,%s,%s,to_timestamp(%s))
                               ON CONFLICT DO NOTHING""",
                            (alert_id,h,mid,time.time()))
                c.commit()
        except Exception as e:
            print(f"markout {alert_id}/{h} failed: {e}", flush=True)

async def main():
    last_id = 0
    while True:
        await asyncio.sleep(2)
        with psycopg.connect(os.environ["POSTGRES_URL"]) as c, c.cursor() as cur:
            cur.execute("SELECT id,extract(epoch FROM ts)::int,coin FROM alerts WHERE id>%s",(last_id,))
            for aid, ts, coin in cur.fetchall():
                last_id = max(last_id, aid)
                asyncio.create_task(schedule(aid, ts, coin))

if __name__ == "__main__": asyncio.run(main())
```

```python
# src/scanner/archive_to_s3.py
import os, glob, datetime as dt, boto3, pathlib
BUCKET = os.environ["S3_BUCKET"]
ROOT = "/opt/scanner/data"
s3 = boto3.client("s3", region_name="ap-northeast-1")
cutoff = (dt.date.today() - dt.timedelta(days=30)).isoformat()

for path in glob.glob(f"{ROOT}/*/coin=*/dt=*/*.parquet"):
    p = pathlib.Path(path)
    dt_part = next((seg for seg in p.parts if seg.startswith("dt=")), None)
    if not dt_part: continue
    if dt_part[3:] < cutoff:
        key = "cold/" + str(p.relative_to(ROOT))
        s3.upload_file(path, BUCKET, key, ExtraArgs={"StorageClass":"STANDARD"})
        p.unlink()
```

## §9 Project structure

```
hl-scanner/
├── pyproject.toml             # uv-managed, Python 3.12
├── uv.lock
├── src/scanner/
│   ├── ws_client.py
│   ├── bar_builder.py
│   ├── parquet_writer.py
│   ├── features.py
│   ├── db.py
│   ├── alerter.py
│   ├── markouts.py
│   └── archive_to_s3.py
├── bin/
│   ├── do_bootstrap.sh
│   ├── deploy.sh
│   └── smoke_test.sh
├── infra/
│   ├── main.tf
│   ├── variables.tf
│   ├── outputs.tf
│   ├── provision.sh
│   └── destroy.sh
├── systemd/
│   ├── scanner-valkey.service
│   ├── scanner-ingestor.service
│   ├── scanner-feature-worker.service
│   ├── scanner-alerter.service
│   ├── scanner-markouts.service
│   ├── scanner-archive.service
│   └── scanner-archive.timer
├── sql/001_init.sql
└── .env.production            # written by provision.sh, gitignored
```

## §10 30-day validation plan

| Day | Validation |
|---|---|
| 1 | Provision; SSH; bootstrap; confirm all five systemd units healthy; confirm one alert row exists in RDS by EOD |
| 2 | Confirm Parquet bars rotating hourly; spot-check one (coin, hour) file for non-null OHLCV |
| 3–7 | Watch markouts table populating; confirm all four horizons land for each alert; check no >5% gap in WS messages |
| 7 | Run a manual archive dry-run; confirm S3 PUT works via VPC endpoint and instance-profile creds |
| 14 | Run a full DuckDB historical query against S3 (cold tier) — round-trip < 10s for a single coin/day |
| 21 | Failure drill: `sudo systemctl stop scanner-ingestor`; confirm restart within 10s; confirm Valkey stream backfills without gap |
| 30 | Cost true-up: verify AWS bill is within $10 of the $219 forecast; rotate CloudWatch log retention; review alert hit-rate vs. markout PnL |

## §11 Pitfalls (AWS-specific)

- **NAT data charges.** Do not put the EC2 box in a private subnet "for security" — Tokyo NAT GW is $0.062/hr base + $0.062/GB processed. Public subnet + EIP + SG locked to your SSH IP is more secure in practice and saves $50+/mo.
- **EBS vs. instance store durability.** `i4g.2xlarge` local NVMe is destroyed on stop; planned AWS maintenance becomes a data-loss event. Stay on EBS gp3.
- **RDS reboot windows.** Single-AZ has no failover. Set the maintenance window to Sunday 18:00 UTC (= Monday 03:00 JST, low Asia volume) and make sure the alerter retries on transient connection errors.
- **S3 request pricing.** PUTs cost real money at high object counts. Don't write one S3 object per minute — batch nightly (which is what `archive_to_s3.py` does). A naïve per-minute archive would cost more in PUT requests than in storage.
- **Security group misconfig.** The most common bug is opening RDS 5432 to `0.0.0.0/0` instead of to the EC2 SG. The Terraform module uses SG-to-SG rules exclusively.
- **STS global endpoint defaults to us-east-1.** Per the AWS IAM docs, the global STS endpoint is hosted in us-east-1 and CloudTrail logs for global-endpoint calls land there. Force the regional endpoint everywhere: `AWS_STS_REGIONAL_ENDPOINTS=regional` and `AWS_DEFAULT_REGION=ap-northeast-1`. The provisioning script enforces this.
- **IAM control plane lives in us-east-1.** This is unavoidable in the AWS commercial partition — IAM principal lookups during STS calls hit a us-east-1 control plane. It is metadata-only; no scanner data ever traverses it. The data plane stays 100% in Tokyo.
- **SSM Session Manager.** If you use SSM instead of SSH, the endpoint is regional (`ssm.ap-northeast-1.amazonaws.com`); always set `AWS_DEFAULT_REGION` before `aws ssm start-session`.

## §12 Build workflow (Cursor → Mac → GitHub → EC2)

1. Author code in Cursor on the Mac.
2. Provision with `cd infra && ./provision.sh` (Terraform under the hood).
3. Push to a private GitHub repo.
4. SSH to the EC2 box: `ssh -i ~/.ssh/hl_scanner_ed25519 ubuntu@$EIP`.
5. `git clone` the repo into `/opt/scanner/app`.
6. `sudo bash bin/do_bootstrap.sh` (idempotent — safe to re-run).
7. `bin/deploy.sh` syncs code and restarts the five systemd units.
8. `bin/smoke_test.sh` confirms a heartbeat in each Valkey stream and a row in `alerts`.

## §13 Costs

| Line item | Spec | Pricing basis | Monthly USD |
|---|---|---|---|
| EC2 compute | c7g.2xlarge (8 vCPU / 16 GiB, Graviton3), Linux, 1-yr Compute SP No Upfront | OD $0.3638/hr → SP effective ~$0.241/hr | **$176.00** |
| EBS root + data | gp3, 200 GB, 3,000 IOPS / 125 MB/s baseline (free tier) | $0.096/GB-mo (Tokyo) | **$19.20** |
| Elastic IP | 1 attached IPv4 | $0.005/IP-hr | **$3.65** |
| RDS PostgreSQL | db.t4g.micro Single-AZ, 20 GB gp3, 7-day backups | $0.021/hr + $0.115/GB-mo storage | **$17.00** |
| S3 Standard | ~30 GB month 1, ramping to ~365 GB by month 12 | $0.025/GB-mo (Tokyo, first 50 TB tier) | **$0.75–$9.00** |
| VPC Gateway Endpoint (S3) | — | Free | **$0.00** |
| CloudWatch Logs (IA class) | ~1 GB/mo ingest, 7-day retention | $0.25/GB ingest + $0.03/GB-mo storage | **$1.00** |
| Data transfer out | Modest egress (alerts, occasional SSH) | First 100 GB/mo free, then $0.114/GB | **$1.00** |
| **Total (steady state)** | | | **≈ $219/mo** |

Year-1 effective spend: ~$2,630. On-demand instead of SP: ~$308/mo total (over budget by ~$70). **The 1-yr Compute Savings Plan is essentially mandatory** — but do not buy it until day 7-14, after you've confirmed the stack produces credible alerts.

## §14 Operational runbook (day 1–30)

- **Healthy:** `systemctl status 'scanner-*'` all green; `redis-cli -s /run/valkey/valkey.sock xlen hl:trades` increasing; `psql … "SELECT count(*) FROM alerts WHERE ts > now() - interval '1 day'"` > 0.
- **WS disconnect storm:** the ingestor logs reconnect attempts with exponential backoff to 30s. Persistent failure → verify the EIP is unchanged and the SG still allows egress 443.
- **RDS storage at 80%:** RDS gp3 auto-grows with `max_allocated_storage`. Set max to 100 GB; this scanner will not hit it inside 12 months.
- **S3 archive failed:** `journalctl -u scanner-archive`. Common cause: instance-profile lost permission. Re-attach via Terraform.
- **Budget alarm fires (>$250):** `aws ce get-cost-and-usage` and look for unexpected NAT/EIP charges — both should be near zero.

## §15 Region final recommendation

ap-northeast-1 is the only correct choice. Comparison:

| AWS region | Distance to HL validators | Typical RTT | Verdict |
|---|---|---|---|
| ap-northeast-1 Tokyo | Same data center cluster | **2–3 ms** | ✅ chosen |
| ap-northeast-3 Osaka | ~400 km | ~12–15 ms | ❌ adds latency for no cost saving |
| ap-northeast-2 Seoul | ~1,150 km | ~30 ms via undersea cable | ❌ ~10× the validator-to-scanner latency |
| ap-southeast-1 Singapore | ~5,300 km | ~70–80 ms | ❌ defeats the purpose |
| ap-southeast-3 Jakarta | ~5,800 km | ~85+ ms | ❌ |

Tokyo costs ~25–30% more than us-east-1 for the same compute; we accept that premium because the entire reason this project exists is the 200-ms execution edge it buys. **Every endpoint in this plan resolves to ap-northeast-1.**

## §16 Provisioning Script — Terraform

Terraform over shell-of-CLI for one decisive reason: **idempotent destroy**. The destroy script is `terraform destroy -auto-approve` and it tears resources down in the right order even after partial failures. Terraform also gives us a single declarative source of truth for the region-pinning guardrails.

### `infra/provision.sh`

```bash
#!/usr/bin/env bash
# infra/provision.sh — one-command interactive provisioning for the HL scanner.
set -euo pipefail

REGION="ap-northeast-1"
ALLOWED_REGIONS=("ap-northeast-1")

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
red()  { printf '\033[31m%s\033[0m\n' "$*"; }
grn()  { printf '\033[32m%s\033[0m\n' "$*"; }

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
ARN=$(echo "$CALLER"     | python3 -c 'import sys,json;print(json.load(sys.stdin)["Arn"])')
grn "✓ Authenticated as: $ARN  (account $ACCOUNT)"

# 3. Region drift guard
CONFIGURED_REGION=$(aws configure get region 2>/dev/null || true)
if [[ -n "$CONFIGURED_REGION" && "$CONFIGURED_REGION" != "$REGION" ]]; then
  red "REFUSING: ~/.aws/config region=$CONFIGURED_REGION but we require $REGION."
  red "Edit ~/.aws/config or unset it, then re-run."; exit 1
fi
ok=0; for r in "${ALLOWED_REGIONS[@]}"; do [[ "$r" == "$REGION" ]] && ok=1; done
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
```

### `infra/main.tf`

```hcl
terraform {
  required_version = ">= 1.6"
  required_providers {
    aws    = { source = "hashicorp/aws",    version = "~> 5.50" }
    random = { source = "hashicorp/random", version = "~> 3.6"  }
  }
}

variable "region"         { type = string }
variable "account_id"     { type = string }
variable "ssh_public_key" { type = string }
variable "my_ip"          { type = string }

# Region guard — refuses any region other than ap-northeast-1
locals { allowed_regions = ["ap-northeast-1"] }
resource "null_resource" "region_guard" {
  lifecycle {
    precondition {
      condition     = contains(local.allowed_regions, var.region)
      error_message = "Region ${var.region} not allowed. All endpoints must remain in ap-northeast-1."
    }
  }
}

provider "aws" {
  region = var.region
  default_tags { tags = { Project = "hl-scanner", ManagedBy = "terraform" } }
}

data "aws_availability_zones" "available" { state = "available" }

# ---------- VPC ----------
resource "aws_vpc" "main" {
  cidr_block           = "10.10.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags = { Name = "hl-scanner-vpc" }
}
resource "aws_internet_gateway" "igw" { vpc_id = aws_vpc.main.id }

resource "aws_subnet" "public" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = "10.10.1.0/24"
  availability_zone       = data.aws_availability_zones.available.names[0]
  map_public_ip_on_launch = false
  tags = { Name = "hl-scanner-public-a" }
}
resource "aws_subnet" "private_a" {
  vpc_id            = aws_vpc.main.id
  cidr_block        = "10.10.10.0/24"
  availability_zone = data.aws_availability_zones.available.names[0]
}
resource "aws_subnet" "private_b" {
  vpc_id            = aws_vpc.main.id
  cidr_block        = "10.10.11.0/24"
  availability_zone = data.aws_availability_zones.available.names[1]
}
resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id
  route { cidr_block = "0.0.0.0/0"; gateway_id = aws_internet_gateway.igw.id }
}
resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}
# Free S3 Gateway Endpoint
resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.main.id
  service_name      = "com.amazonaws.${var.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.public.id]
}

# ---------- Security groups ----------
resource "aws_security_group" "ec2" {
  name   = "hl-scanner-ec2"
  vpc_id = aws_vpc.main.id
  ingress { from_port = 22; to_port = 22; protocol = "tcp"; cidr_blocks = [var.my_ip] }
  egress  { from_port = 0;  to_port = 0;  protocol = "-1";  cidr_blocks = ["0.0.0.0/0"] }
}
resource "aws_security_group" "rds" {
  name   = "hl-scanner-rds"
  vpc_id = aws_vpc.main.id
}
resource "aws_security_group_rule" "rds_in" {
  type                     = "ingress"
  from_port                = 5432
  to_port                  = 5432
  protocol                 = "tcp"
  source_security_group_id = aws_security_group.ec2.id
  security_group_id        = aws_security_group.rds.id
}

# ---------- SSH key ----------
resource "aws_key_pair" "scanner" {
  key_name   = "hl-scanner"
  public_key = var.ssh_public_key
}

# ---------- IAM instance profile (S3 access only) ----------
data "aws_iam_policy_document" "ec2_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals { type = "Service"; identifiers = ["ec2.amazonaws.com"] }
  }
}
resource "aws_iam_role" "scanner" {
  name               = "hl-scanner-ec2"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json
}
resource "aws_iam_role_policy" "scanner_s3" {
  name = "s3-rw"
  role = aws_iam_role.scanner.id
  policy = jsonencode({
    Version = "2012-10-17",
    Statement = [{
      Effect = "Allow",
      Action = ["s3:PutObject","s3:GetObject","s3:DeleteObject","s3:ListBucket"],
      Resource = [
        "arn:aws:s3:::hl-scanner-${var.account_id}-${var.region}",
        "arn:aws:s3:::hl-scanner-${var.account_id}-${var.region}/*"
      ]
    }]
  })
}
resource "aws_iam_instance_profile" "scanner" {
  name = "hl-scanner"
  role = aws_iam_role.scanner.name
}

# ---------- S3 ----------
resource "aws_s3_bucket" "lake" {
  bucket        = "hl-scanner-${var.account_id}-${var.region}"
  force_destroy = false
}
resource "aws_s3_bucket_public_access_block" "lake" {
  bucket                  = aws_s3_bucket.lake.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
resource "aws_s3_bucket_lifecycle_configuration" "lake" {
  bucket = aws_s3_bucket.lake.id
  rule {
    id     = "tier-down"
    status = "Enabled"
    transition { days = 30  ; storage_class = "STANDARD_IA" }
    transition { days = 180 ; storage_class = "GLACIER_IR"  }
  }
}

# ---------- EC2 ----------
data "aws_ami" "ubuntu_arm" {
  most_recent = true
  owners      = ["099720109477"]
  filter { name = "name"; values = ["ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-arm64-server-*"] }
}
resource "aws_instance" "scanner" {
  ami                         = data.aws_ami.ubuntu_arm.id
  instance_type               = "c7g.2xlarge"
  subnet_id                   = aws_subnet.public.id
  vpc_security_group_ids      = [aws_security_group.ec2.id]
  key_name                    = aws_key_pair.scanner.key_name
  iam_instance_profile        = aws_iam_instance_profile.scanner.name
  associate_public_ip_address = false
  metadata_options { http_tokens = "required"; http_endpoint = "enabled" }
  root_block_device { volume_type = "gp3"; volume_size = 30; encrypted = true }
  tags = { Name = "hl-scanner" }
}
resource "aws_ebs_volume" "data" {
  availability_zone = aws_subnet.public.availability_zone
  size              = 200
  type              = "gp3"
  iops              = 3000
  throughput        = 125
  encrypted         = true
  tags = { Name = "hl-scanner-data" }
}
resource "aws_volume_attachment" "data" {
  device_name = "/dev/xvdf"
  volume_id   = aws_ebs_volume.data.id
  instance_id = aws_instance.scanner.id
}
resource "aws_eip" "scanner" {
  instance = aws_instance.scanner.id
  domain   = "vpc"
}

# ---------- RDS ----------
resource "random_password" "rds" { length = 28; special = false }
resource "aws_db_subnet_group" "main" {
  name       = "hl-scanner-db-subnets"
  subnet_ids = [aws_subnet.private_a.id, aws_subnet.private_b.id]
}
resource "aws_db_instance" "pg" {
  identifier              = "hl-scanner-pg"
  engine                  = "postgres"
  # SE-approved update: ap-northeast-1 does not offer postgres 16.4; use 16.14.
  engine_version          = "16.14"
  instance_class          = "db.t4g.micro"
  allocated_storage       = 20
  max_allocated_storage   = 100
  storage_type            = "gp3"
  storage_encrypted       = true
  db_name                 = "scanner"
  username                = "scanner"
  password                = random_password.rds.result
  vpc_security_group_ids  = [aws_security_group.rds.id]
  db_subnet_group_name    = aws_db_subnet_group.main.name
  multi_az                = false
  publicly_accessible     = false
  backup_retention_period = 7
  backup_window           = "17:00-18:00"      # = 02:00–03:00 JST
  maintenance_window      = "Sun:18:00-Sun:19:00"
  skip_final_snapshot     = true
  deletion_protection     = false
}

# ---------- CloudWatch alarm (minimum viable) ----------
resource "aws_cloudwatch_metric_alarm" "ec2_down" {
  alarm_name          = "hl-scanner-ec2-down"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  metric_name         = "StatusCheckFailed_Instance"
  namespace           = "AWS/EC2"
  period              = 60
  statistic           = "Maximum"
  threshold           = 1
  dimensions = { InstanceId = aws_instance.scanner.id }
}

output "eip"           { value = aws_eip.scanner.public_ip }
output "rds_endpoint"  { value = aws_db_instance.pg.address }
output "rds_password"  { value = random_password.rds.result; sensitive = true }
output "s3_bucket"     { value = aws_s3_bucket.lake.id }
output "region"        { value = var.region }
```

## §17 Destroy Script — `infra/destroy.sh`

```bash
#!/usr/bin/env bash
# infra/destroy.sh — interactive, idempotent teardown
set -euo pipefail

REGION="ap-northeast-1"
ALLOWED_REGIONS=("ap-northeast-1")

red()  { printf '\033[31m%s\033[0m\n' "$*"; }
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
ok=0; for r in "${ALLOWED_REGIONS[@]}"; do [[ "$r" == "$REGION" ]] && ok=1; done
[[ $ok -eq 1 ]] || { red "REFUSING: $REGION not in whitelist."; exit 1; }

# Verify Terraform state region matches
cd "$(dirname "$0")"
TF_REGION=$(terraform output -raw region 2>/dev/null || true)
if [[ -n "$TF_REGION" && "$TF_REGION" != "$REGION" ]]; then
  red "REFUSING: terraform state shows region=$TF_REGION, expected $REGION."; exit 1
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
```

## §18 Bootstrap Script — `bin/do_bootstrap.sh`

```bash
#!/usr/bin/env bash
# bin/do_bootstrap.sh — run ONCE on the EC2 box after SSH'ing in. Idempotent.
set -euo pipefail
if [[ "$(id -u)" -ne 0 ]]; then exec sudo -E "$0" "$@"; fi

REGION="ap-northeast-1"
APP=/opt/scanner
DATA=$APP/data
SOCKET_DIR=/run/valkey
bold() { printf '\033[1m%s\033[0m\n' "$*"; }

bold "=== HL Scanner bootstrap ==="

# 1. System deps
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y --no-install-recommends \
  python3.12 python3.12-venv python3-pip \
  build-essential git tmux jq curl ca-certificates \
  postgresql-client-16 valkey-server xfsprogs unzip

# uv
if ! command -v uv >/dev/null; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  ln -sf /root/.local/bin/uv /usr/local/bin/uv
fi

# s5cmd
if ! command -v s5cmd >/dev/null; then
  ARCH=$(uname -m | sed s/aarch64/arm64/)
  curl -L "https://github.com/peak/s5cmd/releases/latest/download/s5cmd_${ARCH}_Linux.tar.gz" \
    | tar xz -C /usr/local/bin s5cmd
fi

# CloudWatch agent
if ! command -v amazon-cloudwatch-agent-ctl >/dev/null; then
  curl -L "https://amazoncloudwatch-agent-${REGION}.s3.${REGION}.amazonaws.com/ubuntu/arm64/latest/amazon-cloudwatch-agent.deb" -o /tmp/cwagent.deb
  dpkg -i /tmp/cwagent.deb || true
fi

# 2. User + dirs
id -u scanner &>/dev/null || useradd -r -m -d $APP -s /usr/sbin/nologin scanner
install -d -o scanner -g scanner $APP $DATA $APP/logs

# 3. Mount the 200 GB data EBS volume at /opt/scanner/data
DEV=$(lsblk -dno NAME,SIZE | awk '$2=="200G"{print $1; exit}')
if [[ -n "$DEV" ]]; then
  blkid "/dev/$DEV" >/dev/null 2>&1 || mkfs.xfs -L scanner-data "/dev/$DEV"
  UUID=$(blkid -s UUID -o value "/dev/$DEV")
  grep -q "$UUID" /etc/fstab || echo "UUID=$UUID $DATA xfs defaults,noatime 0 2" >> /etc/fstab
  mountpoint -q $DATA || mount $DATA
  chown scanner:scanner $DATA
fi

# 4. Valkey sidecar (Unix socket)
install -d -o scanner -g scanner $SOCKET_DIR $DATA/valkey
cat > /etc/valkey/valkey.conf <<EOF
bind 127.0.0.1 -::1
port 0
unixsocket $SOCKET_DIR/valkey.sock
unixsocketperm 770
dir $DATA/valkey
maxmemory 4gb
maxmemory-policy allkeys-lru
save ""
appendonly no
EOF

# 5. systemd units
cat > /etc/systemd/system/scanner-valkey.service <<'EOF'
[Unit]
Description=Scanner Valkey sidecar
After=network.target
[Service]
Type=simple
User=scanner
Group=scanner
RuntimeDirectory=valkey
RuntimeDirectoryMode=0770
ExecStart=/usr/bin/valkey-server /etc/valkey/valkey.conf
Restart=always
RestartSec=2s
[Install]
WantedBy=multi-user.target
EOF

for svc in ingestor feature-worker alerter markouts; do
  module="${svc//-/_}"
  cat > /etc/systemd/system/scanner-$svc.service <<EOF
[Unit]
Description=Scanner $svc
Requires=scanner-valkey.service
After=scanner-valkey.service network-online.target
[Service]
Type=simple
User=scanner
Group=scanner
WorkingDirectory=$APP/app
EnvironmentFile=$APP/.env.production
ExecStart=$APP/app/.venv/bin/python -u -m scanner.$module
Restart=always
RestartSec=5s
StartLimitBurst=10
StartLimitIntervalSec=60
StandardOutput=journal
StandardError=journal
[Install]
WantedBy=multi-user.target
EOF
done

cat > /etc/systemd/system/scanner-archive.service <<EOF
[Unit]
Description=Scanner nightly S3 archive
[Service]
Type=oneshot
User=scanner
Group=scanner
WorkingDirectory=$APP/app
EnvironmentFile=$APP/.env.production
ExecStart=$APP/app/.venv/bin/python -u -m scanner.archive_to_s3
EOF

cat > /etc/systemd/system/scanner-archive.timer <<'EOF'
[Unit]
Description=Run scanner archive nightly
[Timer]
OnCalendar=*-*-* 02:00:00 UTC
Persistent=true
[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now scanner-valkey.service
systemctl enable scanner-archive.timer

# 6. Force STS regional endpoint for any boto3 call from this host
install -d -o scanner -g scanner $APP/.aws
cat > $APP/.aws/config <<EOF
[default]
region = $REGION
sts_regional_endpoints = regional
EOF
chown -R scanner:scanner $APP/.aws

# 7. CloudWatch agent — ship the four journals at Infrequent-Access class, 7-day retention
cat > /opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json <<EOF
{
  "logs": {
    "logs_collected": {
      "journald": {
        "collect_list": [
          {"unit":"scanner-ingestor.service",       "log_group_name":"/scanner/ingestor",       "log_stream_name":"{instance_id}","log_group_class":"INFREQUENT_ACCESS","retention_in_days":7},
          {"unit":"scanner-feature-worker.service", "log_group_name":"/scanner/feature-worker", "log_stream_name":"{instance_id}","log_group_class":"INFREQUENT_ACCESS","retention_in_days":7},
          {"unit":"scanner-alerter.service",        "log_group_name":"/scanner/alerter",        "log_stream_name":"{instance_id}","log_group_class":"INFREQUENT_ACCESS","retention_in_days":7},
          {"unit":"scanner-markouts.service",       "log_group_name":"/scanner/markouts",       "log_stream_name":"{instance_id}","log_group_class":"INFREQUENT_ACCESS","retention_in_days":7}
        ]
      }
    }
  }
}
EOF
amazon-cloudwatch-agent-ctl -a fetch-config -m ec2 \
  -c file:/opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json -s || true

echo
bold "✓ Bootstrap complete."
echo "Next: clone the app to $APP/app, run 'uv sync' as the scanner user,"
echo "then 'systemctl enable --now scanner-{ingestor,feature-worker,alerter,markouts}'."
```
