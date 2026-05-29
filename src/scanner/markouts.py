import asyncio
import os
import time

import psycopg
import requests

HORIZONS = {"30s": 30, "5m": 300, "30m": 1800, "4h": 14400}


async def schedule(alert_id: int, ts: int, coin: str):
    for h, dt_s in HORIZONS.items():
        target = ts + dt_s
        await asyncio.sleep(max(0, target - time.time()))
        try:
            r = requests.post(
                "https://api.hyperliquid.xyz/info",
                json={"type": "l2Book", "coin": coin},
                timeout=5,
            ).json()
            mid = (float(r["levels"][0][0]["px"]) + float(r["levels"][1][0]["px"])) / 2
            with psycopg.connect(os.environ["POSTGRES_URL"]) as c, c.cursor() as cur:
                cur.execute(
                    """INSERT INTO markouts(alert_id,horizon,mid_at_horizon,recorded_at)
                               VALUES(%s,%s,%s,to_timestamp(%s))
                               ON CONFLICT DO NOTHING""",
                    (alert_id, h, mid, time.time()),
                )
                c.commit()
        except Exception as e:
            print(f"markout {alert_id}/{h} failed: {e}", flush=True)


async def main():
    last_id = 0
    while True:
        await asyncio.sleep(2)
        with psycopg.connect(os.environ["POSTGRES_URL"]) as c, c.cursor() as cur:
            cur.execute("SELECT id,extract(epoch FROM ts)::int,coin FROM alerts WHERE id>%s", (last_id,))
            for aid, ts, coin in cur.fetchall():
                last_id = max(last_id, aid)
                asyncio.create_task(schedule(aid, ts, coin))


if __name__ == "__main__":
    asyncio.run(main())
