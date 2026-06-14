import datetime as _dt

import duckdb


def compute_zscores(now_ts: int) -> list[dict]:
    # Prune by the `dt` hive partition so DuckDB only opens the last ~8 days of
    # Parquet (covering the 7-day z-score window) instead of scanning the whole
    # dataset, which grows unbounded and OOM-kills the alerter under its cgroup
    # memory cap. `dt` is the path partition value (YYYY-MM-DD).
    cutoff_date = _dt.datetime.fromtimestamp(
        now_ts - 8 * 24 * 3600, _dt.timezone.utc
    ).strftime("%Y-%m-%d")
    con = duckdb.connect()
    rows = con.execute(
        """
      WITH r AS (
        SELECT coin, ts,
               ln(c / lag(c) OVER (PARTITION BY coin ORDER BY ts)) AS ret,
               c*v AS notional,
               extract('hour' FROM to_timestamp(ts)) AS h
        FROM read_parquet('/opt/scanner/data/bars/coin=*/dt=*/*.parquet',
                          hive_partitioning=true)
        WHERE dt >= ? AND ts >= ? - 7*24*3600
      )
      SELECT coin, ts, ret, notional, h,
             (ret - avg(ret) OVER w) / nullif(stddev_samp(ret) OVER w, 0) AS z,
             count(ret) OVER w AS n
      FROM r
      WINDOW w AS (PARTITION BY coin, h ORDER BY ts ROWS BETWEEN 60 PRECEDING AND 1 PRECEDING)
      QUALIFY ts = ? AND abs(z) > 4.0 AND n >= 30
    """,
        [cutoff_date, now_ts, now_ts],
    ).fetchall()
    return [{"coin": r[0], "ts": r[1], "ret": r[2], "notional": r[3], "h": r[4], "z": r[5]} for r in rows]
