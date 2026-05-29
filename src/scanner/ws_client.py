import asyncio
import os

import orjson
import redis.asyncio as redis
import websockets

WS_URL = "wss://api.hyperliquid.xyz/ws"
DEFAULT_COINS = ["BTC", "ETH", "SOL"]
DEFAULT_VALKEY_SOCKET = "/run/valkey/valkey.sock"


def _load_coins() -> list[str]:
    raw = os.getenv("HL_COINS", "")
    if not raw.strip():
        return DEFAULT_COINS
    coins = [c.strip().upper() for c in raw.split(",") if c.strip()]
    return coins or DEFAULT_COINS


def _valkey_client() -> redis.Redis:
    valkey_url = os.getenv("VALKEY_URL", "").strip()
    if valkey_url:
        return redis.from_url(valkey_url, decode_responses=False)
    socket_path = os.getenv("VALKEY_SOCKET_PATH", DEFAULT_VALKEY_SOCKET)
    return redis.Redis(unix_socket_path=socket_path, decode_responses=False)


VALKEY = _valkey_client()


async def run(coins: list[str]) -> None:
    backoff = 1
    while True:
        try:
            async with websockets.connect(WS_URL, ping_interval=None, max_size=2**22) as ws:
                for coin in coins:
                    for sub_type in ("trades", "l2Book", "activeAssetCtx"):
                        await ws.send(
                            orjson.dumps(
                                {
                                    "method": "subscribe",
                                    "subscription": {"type": sub_type, "coin": coin},
                                }
                            ).decode()
                        )
                asyncio.create_task(_pinger(ws))
                backoff = 1
                async for raw in ws:
                    msg = orjson.loads(raw)
                    channel = msg.get("channel")
                    if channel in ("trades", "l2Book", "activeAssetCtx"):
                        await VALKEY.xadd(
                            f"hl:{channel}",
                            {"d": raw},
                            maxlen=200000,
                            approximate=True,
                        )
        except Exception:
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)


async def _pinger(ws: websockets.WebSocketClientProtocol) -> None:
    while True:
        await asyncio.sleep(25)
        try:
            await ws.send('{"method":"ping"}')
        except Exception:
            return


async def main() -> None:
    await run(_load_coins())


if __name__ == "__main__":
    asyncio.run(main())
