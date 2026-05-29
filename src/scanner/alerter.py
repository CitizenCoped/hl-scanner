import asyncio
import time

from scanner.db import conn, insert_alert
from scanner.features import compute_zscores
from scanner.notifier import send_alert_notifications


async def main():
    while True:
        await asyncio.sleep(60 - (time.time() % 60))
        now = int(time.time() // 60) * 60
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
