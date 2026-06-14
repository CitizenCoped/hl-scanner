"""Aggregate scanner health into stats.json and publish it to the dashboard
S3 bucket. Run on a 1-minute systemd timer. Each section is isolated so one
failing data source never blocks the rest of the export.
"""

import datetime as dt
import json
import os
import shutil
import time

import boto3
import duckdb
import redis
import requests

INFO_URL = "https://api.hyperliquid.xyz/info"
BARS_ROOT = os.getenv("SCANNER_BARS_ROOT", "/opt/scanner/data/bars")
DATA_ROOT = os.getenv("SCANNER_DATA_ROOT", "/opt/scanner/data")
DASHBOARD_BUCKET = os.getenv("DASHBOARD_BUCKET", "").strip()
MIN_24H_NOTIONAL = float(os.getenv("HL_MIN_24H_NOTIONAL", "10000000"))
MAX_COINS = int(os.getenv("HL_MAX_COINS", "180"))
DEFAULT_VALKEY_SOCKET = "/run/valkey/valkey.sock"
AWS_REGION = os.getenv("AWS_REGION", "ap-northeast-1")
ALERTER_HEARTBEAT = os.getenv("ALERTER_HEARTBEAT", "/opt/scanner/data/alerter.heartbeat")
METRIC_NAMESPACE = "HLScanner"
STALE_SENTINEL = 99999  # published when a freshness value can't be computed (so alarms fire)


def _publish_metrics(stats: dict) -> None:
    """Publish pipeline-liveness metrics so CloudWatch can alarm (once, with
    native de-dup) when the scanner is 'up but not working'."""
    now = int(time.time())
    bars = stats.get("bars", {})
    last_bars = [b["last_bar_ts"] for b in bars.values() if b.get("last_bar_ts")]
    bar_age = (now - max(last_bars)) if last_bars else STALE_SENTINEL
    try:
        hb_age = now - int(os.path.getmtime(ALERTER_HEARTBEAT))
    except OSError:
        hb_age = STALE_SENTINEL
    try:
        boto3.client("cloudwatch", region_name=AWS_REGION).put_metric_data(
            Namespace=METRIC_NAMESPACE,
            MetricData=[
                {"MetricName": "SecondsSinceLastBar", "Value": float(bar_age), "Unit": "Seconds"},
                {"MetricName": "SecondsSinceAlerterLoop", "Value": float(hb_age), "Unit": "Seconds"},
            ],
        )
    except Exception as e:  # noqa: BLE001 - never let metrics break the export
        print(f"metric publish failed: {e}", flush=True)


def _valkey_client() -> redis.Redis:
    valkey_url = os.getenv("VALKEY_URL", "").strip()
    if valkey_url:
        return redis.from_url(valkey_url, decode_responses=True)
    socket_path = os.getenv("VALKEY_SOCKET_PATH", DEFAULT_VALKEY_SOCKET)
    return redis.Redis(unix_socket_path=socket_path, decode_responses=True)


def _stream_lengths() -> dict:
    out = {}
    try:
        client = _valkey_client()
        for stream in ("hl:trades", "hl:l2Book", "hl:activeAssetCtx"):
            try:
                out[stream] = client.xlen(stream)
            except Exception:
                out[stream] = None
    except Exception:
        pass
    return out


def _universe() -> list[dict]:
    """Selected universe ranked by 24h notional volume."""
    try:
        resp = requests.post(INFO_URL, json={"type": "metaAndAssetCtxs"}, timeout=10)
        resp.raise_for_status()
        meta, ctxs = resp.json()
        ranked = []
        for asset, ctx in zip(meta.get("universe", []), ctxs):
            name = asset.get("name")
            if not name:
                continue
            try:
                vol = float(ctx.get("dayNtlVlm", 0.0))
            except (TypeError, ValueError):
                vol = 0.0
            ranked.append((name.upper(), vol))
        ranked.sort(key=lambda x: x[1], reverse=True)
        selected = [(n, v) for n, v in ranked if v > MIN_24H_NOTIONAL][:MAX_COINS]
        return [{"coin": n, "day_ntl_vlm": v, "subscribed": True} for n, v in selected]
    except Exception:
        return []


def _bar_coverage() -> dict:
    """Per-coin bar coverage for the last 24h from local Parquet."""
    cutoff = int(time.time()) - 86400
    # Prune by the `dt` hive partition (YYYY-MM-DD from the path) so DuckDB only
    # opens the last ~2 days of files. Filtering on `ts` alone forces a read of
    # the entire (unbounded) dataset, which OOM-kills the exporter once the
    # file count grows large.
    cutoff_date = dt.datetime.fromtimestamp(cutoff, dt.timezone.utc).strftime("%Y-%m-%d")
    try:
        con = duckdb.connect()
        rows = con.execute(
            """
            SELECT coin,
                   count(*) AS bars_24h,
                   max(ts) AS last_bar_ts,
                   sum(c * v) AS notional_24h
            FROM read_parquet(?, hive_partitioning=true)
            WHERE dt >= ? AND ts >= ?
            GROUP BY coin
            """,
            [f"{BARS_ROOT}/coin=*/dt=*/*.parquet", cutoff_date, cutoff],
        ).fetchall()
        return {
            r[0]: {
                "bars_24h": int(r[1]),
                "last_bar_ts": int(r[2]) if r[2] is not None else None,
                "notional_24h": float(r[3]) if r[3] is not None else 0.0,
            }
            for r in rows
        }
    except Exception:
        return {}


def _alert_stats() -> dict:
    out = {"total": 0, "last_24h": 0, "last_1h": 0, "per_coin": {}, "recent": []}
    try:
        from scanner.db import conn

        with conn() as c, c.cursor() as cur:
            cur.execute("SELECT count(*) FROM alerts")
            out["total"] = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM alerts WHERE ts > now() - interval '24 hours'")
            out["last_24h"] = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM alerts WHERE ts > now() - interval '1 hour'")
            out["last_1h"] = cur.fetchone()[0]
            cur.execute(
                "SELECT coin, count(*) FROM alerts WHERE ts > now() - interval '24 hours' GROUP BY coin"
            )
            out["per_coin"] = {row[0]: row[1] for row in cur.fetchall()}
            cur.execute(
                """SELECT coin, z, ret, notional, extract(epoch FROM ts)::int
                   FROM alerts ORDER BY id DESC LIMIT 20"""
            )
            out["recent"] = [
                {"coin": r[0], "z": r[1], "ret": r[2], "notional": r[3], "ts": r[4]}
                for r in cur.fetchall()
            ]
            cur.execute("SELECT count(*) FROM markouts")
            total_markouts = cur.fetchone()[0]
            cur.execute(
                "SELECT count(*) FROM (SELECT alert_id FROM markouts GROUP BY alert_id HAVING count(*) = 4) t"
            )
            fully_covered = cur.fetchone()[0]
            out["markouts"] = {"rows": total_markouts, "fully_covered_alerts": fully_covered}
    except Exception:
        pass
    return out


def _disk() -> dict:
    try:
        usage = shutil.disk_usage(DATA_ROOT)
        return {
            "total_gb": round(usage.total / 1e9, 2),
            "used_gb": round(usage.used / 1e9, 2),
            "free_gb": round(usage.free / 1e9, 2),
        }
    except Exception:
        return {}


def build_stats() -> dict:
    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "streams": _stream_lengths(),
        "universe": _universe(),
        "bars": _bar_coverage(),
        "alerts": _alert_stats(),
        "data": _disk(),
        "config": {"min_24h_notional": MIN_24H_NOTIONAL, "max_coins": MAX_COINS},
    }


def main() -> None:
    stats = build_stats()
    _publish_metrics(stats)
    payload = json.dumps(stats, separators=(",", ":")).encode()
    if not DASHBOARD_BUCKET:
        print(payload.decode())
        return
    s3 = boto3.client("s3", region_name="ap-northeast-1")
    s3.put_object(
        Bucket=DASHBOARD_BUCKET,
        Key="stats.json",
        Body=payload,
        ContentType="application/json",
        CacheControl="public, max-age=30",
    )


if __name__ == "__main__":
    main()
