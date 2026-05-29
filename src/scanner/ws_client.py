import asyncio
import os

import orjson
import redis.asyncio as redis
import requests
import websockets

WS_URL = "wss://api.hyperliquid.xyz/ws"
INFO_URL = "https://api.hyperliquid.xyz/info"
SUB_TYPES = ("trades", "l2Book", "activeAssetCtx")
DEFAULT_COINS = ["BTC", "ETH", "SOL"]
DEFAULT_VALKEY_SOCKET = "/run/valkey/valkey.sock"

# Universe selection (BUILD_GUIDE §4). Defaults: perps with >$10M 24h notional,
# capped at 180 coins (180 * 3 streams = 540 subscriptions, under the 1,000/IP
# WS cap), re-evaluated every 6 hours.
MIN_24H_NOTIONAL = float(os.getenv("HL_MIN_24H_NOTIONAL", "10000000"))
MAX_COINS = int(os.getenv("HL_MAX_COINS", "180"))
UNIVERSE_REFRESH_SEC = int(os.getenv("HL_UNIVERSE_REFRESH_SEC", "21600"))


def _pinned_coins() -> list[str]:
    raw = os.getenv("HL_COINS", "")
    if not raw.strip():
        return []
    return [c.strip().upper() for c in raw.split(",") if c.strip()]


def _valkey_client() -> redis.Redis:
    valkey_url = os.getenv("VALKEY_URL", "").strip()
    if valkey_url:
        return redis.from_url(valkey_url, decode_responses=False)
    socket_path = os.getenv("VALKEY_SOCKET_PATH", DEFAULT_VALKEY_SOCKET)
    return redis.Redis(unix_socket_path=socket_path, decode_responses=False)


VALKEY = _valkey_client()


def _fetch_universe_sync() -> list[str]:
    """Return the ranked coin universe from Hyperliquid.

    Uses POST /info {"type":"metaAndAssetCtxs"}, which returns the perp meta
    (universe names) alongside per-asset contexts that include dayNtlVlm (24h
    notional volume). Pinned HL_COINS are always included.
    """
    resp = requests.post(INFO_URL, json={"type": "metaAndAssetCtxs"}, timeout=10)
    resp.raise_for_status()
    meta, ctxs = resp.json()
    universe = meta.get("universe", [])

    ranked: list[tuple[str, float]] = []
    for asset, ctx in zip(universe, ctxs):
        name = asset.get("name")
        if not name:
            continue
        try:
            vol = float(ctx.get("dayNtlVlm", 0.0))
        except (TypeError, ValueError):
            vol = 0.0
        ranked.append((name.upper(), vol))

    ranked.sort(key=lambda x: x[1], reverse=True)
    selected = [name for name, vol in ranked if vol > MIN_24H_NOTIONAL][:MAX_COINS]

    pinned = _pinned_coins()
    valid = {name for name, _ in ranked}
    out = list(selected)
    for coin in pinned:
        if coin in valid and coin not in out:
            out.append(coin)
    return out or DEFAULT_COINS


async def fetch_universe() -> list[str]:
    try:
        return await asyncio.to_thread(_fetch_universe_sync)
    except Exception:
        return DEFAULT_COINS


async def _subscribe(ws, coin: str, method: str = "subscribe") -> None:
    for sub_type in SUB_TYPES:
        await ws.send(
            orjson.dumps(
                {"method": method, "subscription": {"type": sub_type, "coin": coin}}
            ).decode()
        )


async def _universe_refresher(ws, subscribed: set[str]) -> None:
    while True:
        await asyncio.sleep(UNIVERSE_REFRESH_SEC)
        try:
            target = set(await fetch_universe())
            for coin in target - subscribed:
                await _subscribe(ws, coin, "subscribe")
                subscribed.add(coin)
            for coin in subscribed - target:
                await _subscribe(ws, coin, "unsubscribe")
                subscribed.discard(coin)
        except Exception:
            # Never let a refresh failure tear down the live connection.
            continue


async def run() -> None:
    backoff = 1
    while True:
        try:
            coins = await fetch_universe()
            async with websockets.connect(WS_URL, ping_interval=None, max_size=2**22) as ws:
                subscribed: set[str] = set()
                for coin in coins:
                    await _subscribe(ws, coin, "subscribe")
                    subscribed.add(coin)
                asyncio.create_task(_pinger(ws))
                asyncio.create_task(_universe_refresher(ws, subscribed))
                backoff = 1
                async for raw in ws:
                    msg = orjson.loads(raw)
                    channel = msg.get("channel")
                    if channel in SUB_TYPES:
                        await VALKEY.xadd(
                            f"hl:{channel}",
                            {"d": raw},
                            maxlen=200000,
                            approximate=True,
                        )
        except Exception:
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)


async def _pinger(ws) -> None:
    while True:
        await asyncio.sleep(25)
        try:
            await ws.send('{"method":"ping"}')
        except Exception:
            return


async def main() -> None:
    await run()


if __name__ == "__main__":
    asyncio.run(main())
