# AGENTS.md

## Cursor Cloud specific instructions

### Architecture overview

This is a Python 3.12 backend service (no web framework) that scans Hyperliquid perpetual futures markets. It consists of four long-running processes plus a Valkey sidecar, coordinated via Redis-compatible streams and PostgreSQL. See `BUILD_GUIDE.md` for the full architecture and `.cursorrules` for coding conventions.

### Local dev environment

**Dependencies:** `uv sync` in the repo root installs everything into `.venv/`.

**Infrastructure services needed locally:**
- Redis (acting as Valkey): `sudo redis-server --daemonize yes --port 6379`
- PostgreSQL 16: `sudo pg_ctlcluster 16 main start`

**Environment variables** (export before running any scanner module):
```bash
export VALKEY_URL="redis://localhost:6379/0"
export POSTGRES_URL="postgresql://scanner:scanner@localhost:5432/scanner"
export SCANNER_BARS_ROOT="/opt/scanner/data/bars"
export SCANNER_DATA_ROOT="/opt/scanner/data"
```

**Database setup** (one-time after fresh PostgreSQL install):
```bash
sudo -u postgres psql -c "CREATE USER scanner WITH PASSWORD 'scanner';"
sudo -u postgres psql -c "CREATE DATABASE scanner OWNER scanner;"
psql "$POSTGRES_URL" -f sql/001_init.sql
```

**Data directory:**
```bash
sudo mkdir -p /opt/scanner/data/bars && sudo chown -R $(whoami):$(whoami) /opt/scanner
```

### Running services

Each module is run via `uv run python -m scanner.<module>`:
- Ingestor: `uv run python -m scanner.ws_client`
- Feature worker (bar builder): `uv run python -m scanner.bar_builder`
- Alerter: `uv run python -m scanner.alerter`
- Markouts: `uv run python -m scanner.markouts`
- Stats exporter (one-shot): `uv run python -m scanner.stats_exporter`

The ingestor subscribes to Hyperliquid's public WebSocket and writes ticks to Redis streams. The bar builder consumes from `hl:trades` and flushes 1-minute OHLCV bars as Parquet. The alerter uses DuckDB to query Parquet in-place and looks for |z| > 4.0 signals (requires ~7 days of bar data for meaningful results due to the 30-sample floor).

### Linting

No lint tool is configured in `pyproject.toml`. Use `ruff check src/` and `ruff format --check src/` via `uv tool run ruff` or install ruff globally.

### Gotchas

- The `POSTGRES_URL` env var is **required at import time** by `scanner.db` (it reads `os.environ["POSTGRES_URL"]` at module level). Always export it before importing any module that touches the DB (alerter, markouts, stats_exporter).
- The alerter's z-score query won't fire alerts until there are >=30 same-hour-of-day observations per coin (~7 days of continuous ingestor+bar_builder operation). This is by design (min_obs=30 floor).
- The bar builder flushes at each 1-minute wall-clock boundary. On first start, you must wait up to 60 seconds to see the first Parquet file appear.
- Stats exporter prints JSON to stdout if `DASHBOARD_BUCKET` is unset (useful for local testing without S3).
- There are currently no automated tests in the repo. Verification is done by running services and checking stream/file/query outputs.
