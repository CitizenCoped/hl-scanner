import asyncio
import os
import time
from collections import defaultdict

import orjson
import redis.asyncio as redis

from scanner.parquet_writer import write_bar

DEFAULT_VALKEY_SOCKET = "/run/valkey/valkey.sock"


def _valkey_client() -> redis.Redis:
    valkey_url = os.getenv("VALKEY_URL", "").strip()
    if valkey_url:
        return redis.from_url(valkey_url, decode_responses=False)
    socket_path = os.getenv("VALKEY_SOCKET_PATH", DEFAULT_VALKEY_SOCKET)
    return redis.Redis(unix_socket_path=socket_path, decode_responses=False)


VALKEY = _valkey_client()


async def consume() -> None:
    last_id = "$"
    bars = defaultdict(lambda: {"o": None, "h": -1e30, "l": 1e30, "c": None, "v": 0.0, "n": 0})

    async def flush(minute_ts: int) -> None:
        for coin, bar in list(bars.items()):
            if bar["o"] is not None:
                await write_bar(coin, minute_ts, bar)
        bars.clear()

    current_minute = int(time.time() // 60) * 60
    while True:
        resp = await VALKEY.xread({"hl:trades": last_id}, block=1000, count=1000)
        now_minute = int(time.time() // 60) * 60
        if now_minute > current_minute:
            await flush(current_minute)
            current_minute = now_minute
        if not resp:
            continue
        for _, entries in resp:
            for eid, fields in entries:
                last_id = eid
                msg = orjson.loads(fields[b"d"])
                for t in msg.get("data", []):
                    coin = t["coin"]
                    try:
                        px = float(t["px"])
                        sz = float(t["sz"])
                    except (TypeError, ValueError):
                        continue
                    bar = bars[coin]
                    if bar["o"] is None:
                        bar["o"] = px
                    bar["h"] = max(bar["h"], px)
                    bar["l"] = min(bar["l"], px)
                    bar["c"] = px
                    bar["v"] += sz
                    bar["n"] += 1


if __name__ == "__main__":
    asyncio.run(consume())
