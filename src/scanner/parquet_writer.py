import datetime as dt
import os

import pyarrow as pa
import pyarrow.parquet as pq

ROOT = os.getenv("SCANNER_BARS_ROOT", "/opt/scanner/data/bars")


async def write_bar(coin: str, minute_ts: int, b: dict[str, float | int | None]) -> None:
    # SE-approved deviation from BUILD_GUIDE §6 ("one file per hour"): write one
    # immutable Parquet file per minute flush via pq.write_table (which writes a
    # complete footer and closes immediately). A long-open per-hour ParquetWriter
    # leaves files without footer "magic bytes" until close(), so any reader (the
    # alerter's DuckDB query in features.py and the dashboard's stats exporter)
    # fails on an in-progress or restart-abandoned file. Per-minute files are
    # always complete and readable.
    d = dt.datetime.fromtimestamp(minute_ts, tz=dt.timezone.utc)
    out_dir = f"{ROOT}/coin={coin}/dt={d:%Y-%m-%d}"
    os.makedirs(out_dir, exist_ok=True)
    path = f"{out_dir}/{minute_ts}.parquet"

    table = pa.table(
        {
            "ts": [minute_ts],
            "o": [b["o"]],
            "h": [b["h"]],
            "l": [b["l"]],
            "c": [b["c"]],
            "v": [b["v"]],
            "n": [b["n"]],
        }
    )
    pq.write_table(table, path, compression="zstd", compression_level=3)
