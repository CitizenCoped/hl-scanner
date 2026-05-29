# Cursor Prompts — Hyperliquid Scanner

Paste these into Cursor's agent in order. Wait for each prompt's smoke test to pass before moving to the next. Reference: BUILD_GUIDE.md.

---

## Prompt 1 — Project skeleton

Read `BUILD_GUIDE.md`. Create the project structure des
cribed in §9 exactly: `pyproject.toml` targeting Python 3.12 with uv, empty module files in `src/scanner/`, and the `infra/`, `bin/`, `systemd/`, `sql/` directories. Initialize `pyproject.toml` with these deps: websockets, orjson, polars, duckdb, pyarrow, redis, psycopg[binary], pydantic, requests, boto3. Run `uv sync`.

**Smoke test:** `uv run python -c "import scanner; print('ok')"` succeeds.

---

## Prompt 2 — Provisioning (Terraform + interactive wrapper)

From `BUILD_GUIDE.md` §16, write the complete `infra/main.tf`, `infra/variables.tf`, `infra/outputs.tf`, and `infra/provision.sh`. The wrapper must (a) prompt interactively for AWS creds, (b) validate with `sts get-caller-identity` against `https://sts.ap-northeast-1.amazonaws.com`, (c) refuse any region other than `ap-northeast-1`, (d) show a plan summary and require y/N confirmation, (e) write `.env.production` to the repo root.

**Smoke test:** `cd infra && terraform fmt -check && terraform init && terraform validate` all pass.

---

## Prompt 3 — WebSocket ingestor

Implement `src/scanner/ws_client.py` exactly per §8. Subscribe to `trades`, `l2Book`, `activeAssetCtx` for the configured coin list (default `["BTC","ETH","SOL"]`); ping every 25 s; xadd raw payloads to `hl:trades`, `hl:l2Book`, `hl:activeAssetCtx` Valkey streams with `maxlen=200000 approximate=True`; reconnect with exponential backoff capped at 30 s.

**Smoke test:** against a local Valkey, run for 60 s and confirm `XLEN hl:trades > 100`.

---

## Prompt 4 — Feature worker (bar builder + Parquet writer)

Implement `src/scanner/bar_builder.py` and `src/scanner/parquet_writer.py` per §8. Bar builder reads `hl:trades` via XREAD with 1-second block, accumulates per-coin OHLCV into in-memory dicts, flushes on minute rollover. Parquet writer keeps one open `ParquetWriter` per (coin, date, hour), ZSTD level 3.

**Smoke test:** run for 3 minutes; verify at least one `.parquet` exists under `/opt/scanner/data/bars/coin=BTC/...` and DuckDB can read it: `duckdb -c "SELECT count(*) FROM read_parquet('/opt/scanner/data/bars/**/*.parquet', hive_partitioning=true)"`.

---

## Prompt 5 — Postgres schema + db.py

Write `sql/001_init.sql` exactly per §6 with the alerts and markouts tables and indexes. Implement `src/scanner/db.py` per §8. Add `bin/migrate.sh` running `psql $POSTGRES_URL -f sql/001_init.sql`.

**Smoke test:** against a local Postgres, INSERT one fake alert, SELECT it back, exit 0.

---

## Prompt 6 — Alerter (DuckDB z-score)

Implement `src/scanner/features.py` and `src/scanner/alerter.py` per §8. The DuckDB query already filters `n >= 30` for the min-samples gate. The alerter wakes at each minute boundary, calls `compute_zscores(now)`, inserts each returned row.

**Smoke test:** with synthetic Parquet data containing an obvious 6-sigma spike, the alerter inserts exactly one row.

---

## Prompt 7 — Markout logger

Implement `src/scanner/markouts.py` per §8. Worker polls the alerts table for `id > last_seen`, spawns an asyncio task per new alert that sleeps until each horizon, then fetches the current mid from `POST https://api.hyperliquid.xyz/info` with `{"type":"l2Book","coin":...}` and INSERTs into `markouts` with `ON CONFLICT DO NOTHING`.

**Smoke test:** seed an alert with `ts = now - 35s`, run for 60 s, confirm the `30s` markout row appears.

---

## Prompt 8 — S3 archive worker

Implement `src/scanner/archive_to_s3.py` per §8. Use `boto3.client('s3', region_name='ap-northeast-1')` relying on the IAM instance profile — do NOT accept access keys. Walk `/opt/scanner/data/{bars,bbo,trades}/coin=*/dt=*` and upload partitions whose `dt=` is older than 30 days to `s3://$S3_BUCKET/cold/...`, then unlink locally.

**Smoke test:** with `moto` mocking S3, seed a 35-day-old file, run the script, confirm the file was uploaded and removed locally.

---

## Prompt 9 — Bootstrap script + systemd units

Write `bin/do_bootstrap.sh` exactly per §18 plus the seven systemd unit files in `systemd/`.

**Smoke test:** shellcheck `bin/do_bootstrap.sh` zero warnings; `systemd-analyze verify systemd/*.service` reports no errors.

---

## Prompt 10 — Hardening + smoke test + destroy

Write `infra/destroy.sh` per §17. Write `bin/smoke_test.sh` that (a) confirms all five systemd units are active, (b) `XLEN hl:trades > 0`, (c) `SELECT count(*) FROM alerts WHERE ts > now() - interval '1 hour'` returns without error, (d) lists at least one object in the S3 bucket if any archive has run. Add a CloudWatch alarm in Terraform on `StatusCheckFailed_Instance` (already in §16; verify it's wired to an SNS email subscription via a `notification_email` Terraform variable).

**End-to-end smoke test:** provision → bootstrap → deploy → smoke test → destroy with zero residual resources in the AWS console.

