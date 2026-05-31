import asyncio
import time

from scanner.db import conn, insert_alert
from scanner.features import compute_zscores
from scanner.notifier import send_alert_notifications


async def main():
    while True:
        # Wake a few seconds past the minute boundary so the feature worker has
        # flushed the just-closed minute's bar to Parquet.
        await asyncio.sleep(60 - (time.time() % 60) + 5)
        # Evaluate the last fully-closed minute (t-1). Bars are labelled by their
        # start minute and only written once the minute closes, so the current
        # minute has no bar yet; using `now` would never match (and would be
        # look-ahead). See BUILD_GUIDE §5 / .cursorrules correctness rule #1.
        now = int(time.time() // 60) * 60 - 60
        try:
            with conn() as c:
                for row in compute_zscores(now):
                    insert_alert(c, row)
                    send_alert_notifications(row)
                c.commit()
        except Exception as e:
            print(f"alerter error: {e}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
