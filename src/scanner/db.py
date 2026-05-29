import os
from contextlib import contextmanager

import psycopg

DSN = os.environ["POSTGRES_URL"]


@contextmanager
def conn():
    with psycopg.connect(DSN, autocommit=False) as c:
        yield c


def insert_alert(c, row: dict) -> int:
    with c.cursor() as cur:
        cur.execute(
            """
          INSERT INTO alerts(ts,coin,z,ret,notional,mid,bucket_hour)
          VALUES(to_timestamp(%s),%s,%s,%s,%s,%s,%s) RETURNING id
        """,
            (
                row["ts"],
                row["coin"],
                row["z"],
                row["ret"],
                row["notional"],
                row.get("mid", 0.0),
                row["h"],
            ),
        )
        return cur.fetchone()[0]
