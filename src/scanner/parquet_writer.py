import datetime as dt
import os

import pyarrow as pa
import pyarrow.parquet as pq

ROOT = os.getenv("SCANNER_BARS_ROOT", "/opt/scanner/data/bars")
_writers: dict[tuple[str, str, int], pq.ParquetWriter] = {}


async def write_bar(coin: str, minute_ts: int, b: dict[str, float | int | None]) -> None:
    d = dt.datetime.fromtimestamp(minute_ts, tz=dt.timezone.utc)
    key = (coin, d.strftime("%Y-%m-%d"), d.hour)
    path = f"{ROOT}/coin={coin}/dt={key[1]}/{key[2]:03d}.parquet"
    os.makedirs(os.path.dirname(path), exist_ok=True)

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
    writer = _writers.get(key)
    if writer is None:
        writer = pq.ParquetWriter(path, table.schema, compression="zstd", compression_level=3)
        _writers[key] = writer
    writer.write_table(table)
