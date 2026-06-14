"""Compact per-minute bar Parquet files into one file per coin per day.

The feature worker writes one immutable Parquet file per minute
(`coin=X/dt=Y/<minute_ts>.parquet`) — an SE-approved deviation from
BUILD_GUIDE §6 that avoids footerless files but, over time, creates hundreds of
thousands of tiny files. That makes the alerter (`features.py`) and dashboard
(`stats_exporter.py`) DuckDB scans slow and memory-hungry (the cause of the
2026-06 OOM incidents).

This nightly job merges each CLOSED day's per-minute files into a single
`coin=X/dt=Y/day.parquet`, then removes the per-minute files. Readers are
unaffected — they glob `coin=*/dt=*/*.parquet`, which matches one file or many.

Safety:
- Never touches the current (still-being-written) UTC day.
- Idempotent: already-compacted partitions are skipped, and the merge
  de-duplicates by `ts`, so an interrupted run self-heals on the next pass.
- The merged file is written to a `.tmp` (not matched by `*.parquet`), verified
  non-empty, then atomically renamed into place before the per-minute files are
  deleted — so a crash never loses data.
"""

import datetime as dt
import os
import socket
from pathlib import Path

import boto3
import duckdb

ROOT = Path(os.getenv("SCANNER_BARS_ROOT", "/opt/scanner/data/bars"))
COMPACTED = "day.parquet"
SNS_TOPIC_ARN = os.getenv("ALERT_SNS_TOPIC_ARN", "").strip()
AWS_REGION = os.getenv("AWS_REGION", "ap-northeast-1")


def _notify(ok: bool, summary: str) -> None:
    """Publish exactly one status message per run to SNS (email). Successful
    runs are a heartbeat; failures are prefixed with ALERT for visibility."""
    if not SNS_TOPIC_ARN:
        return
    status = "success" if ok else "fail"
    subject = f"compact.py {status}" if ok else f"ALERT compact.py {status}"
    prefix = "" if ok else "ALERT "
    message = (
        f"{prefix}compact.py {status}\n\n"
        f"{summary}\n\n"
        f"host: {socket.gethostname()}\n"
        f"time: {dt.datetime.now(dt.timezone.utc).isoformat()}"
    )
    try:
        boto3.client("sns", region_name=AWS_REGION).publish(
            TopicArn=SNS_TOPIC_ARN, Subject=subject[:100], Message=message
        )
    except Exception as e:  # noqa: BLE001 - notification must never crash the job
        print(f"sns notify failed: {e}", flush=True)


def _sql_str(p: Path) -> str:
    # Single-quote a path for inline SQL (DuckDB COPY ... TO does not accept a
    # bind parameter for the output path). Paths are internal (coin/date), but
    # escape quotes defensively anyway.
    return "'" + str(p).replace("'", "''") + "'"


def _compact_partition(part: Path) -> bool:
    minute_files = [f for f in part.glob("*.parquet") if f.name != COMPACTED]
    if not minute_files:
        return False  # already compacted or empty
    tmp = part / "day.parquet.tmp"
    tmp.unlink(missing_ok=True)  # clear any leftover from an aborted run
    glob_in = _sql_str(part / "*.parquet")  # all complete files, incl. prior day.parquet
    # A fresh connection per partition keeps memory flat regardless of how many
    # partitions a run processes (a shared connection accumulates GBs).
    con = duckdb.connect()
    try:
        con.execute("PRAGMA threads=2")
        con.execute(
            f"""
            COPY (
              SELECT ts,
                     any_value(o) AS o, any_value(h) AS h, any_value(l) AS l,
                     any_value(c) AS c, any_value(v) AS v, any_value(n) AS n
              FROM read_parquet({glob_in})
              GROUP BY ts ORDER BY ts
            ) TO {_sql_str(tmp)} (FORMAT PARQUET, COMPRESSION ZSTD)
            """
        )
        rows = con.execute(
            f"SELECT count(*) FROM read_parquet({_sql_str(tmp)})"
        ).fetchone()[0]
    finally:
        con.close()
    if not rows:
        tmp.unlink(missing_ok=True)
        raise RuntimeError("compacted file is empty")
    os.replace(tmp, part / COMPACTED)  # atomic swap; merged file is now live
    for f in minute_files:
        f.unlink(missing_ok=True)
    return True


def _run() -> tuple[int, int, int, str | None]:
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    parts = removed = failures = 0
    first_error: str | None = None
    if not ROOT.exists():
        return parts, removed, failures, first_error
    for coin_dir in sorted(ROOT.glob("coin=*")):
        for part in sorted(coin_dir.glob("dt=*")):
            if part.name[3:] >= today:
                continue  # skip the current (live) day — still being written
            n = len([f for f in part.glob("*.parquet") if f.name != COMPACTED])
            if n == 0:
                continue
            try:
                if _compact_partition(part):
                    parts += 1
                    removed += n - 1  # n per-minute files -> 1 day file
            except Exception as e:  # noqa: BLE001 - never abort the whole run
                failures += 1
                if first_error is None:
                    first_error = f"{coin_dir.name}/{part.name}: {e}"
                print(f"compact failed for {part}: {e}", flush=True)
    return parts, removed, failures, first_error


def main() -> None:
    # Exactly one SNS status message per run (a nightly heartbeat on success,
    # an ALERT on any failure) — never one per partition.
    try:
        parts, removed, failures, first_error = _run()
    except Exception as e:  # noqa: BLE001 - report unexpected crashes too
        _notify(ok=False, summary=f"compaction run crashed: {e}")
        raise
    summary = f"compacted {parts} partitions, removed {removed} files"
    print(summary, flush=True)
    if failures:
        _notify(ok=False, summary=f"{failures} partition(s) failed; first: {first_error}. {summary}")
    else:
        _notify(ok=True, summary=summary)


if __name__ == "__main__":
    main()
