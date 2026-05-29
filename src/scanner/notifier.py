import os
from typing import Any

import requests


def _format_alert_message(row: dict[str, Any]) -> str:
    coin = row.get("coin", "unknown")
    z = row.get("z", 0.0)
    ret = row.get("ret", 0.0)
    notional = row.get("notional", 0.0)
    return f"{coin} alert: z={z:.2f}, ret={ret:.6f}, notional={notional:.2f}"


def send_alert_notifications(row: dict[str, Any]) -> None:
    _send_pushover(row)
    _send_discord(row)


def _send_pushover(row: dict[str, Any]) -> None:
    token = os.getenv("PUSHOVER_APP_TOKEN", "").strip()
    user = os.getenv("PUSHOVER_USER_KEY", "").strip()
    if not token or not user:
        return

    try:
        requests.post(
            "https://api.pushover.net/1/messages.json",
            data={
                "token": token,
                "user": user,
                "title": "HL Scanner Alert",
                "message": _format_alert_message(row),
            },
            timeout=5,
        )
    except Exception:
        return


def _send_discord(row: dict[str, Any]) -> None:
    webhook_url = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook_url:
        return

    try:
        requests.post(
            webhook_url,
            json={"content": _format_alert_message(row)},
            timeout=5,
        )
    except Exception:
        return
