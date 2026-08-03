#!/usr/bin/env python3
"""Validate Telegram credentials and send one diagnostic message."""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

import uz_monitor


def telegram_request(token: str, method: str, data: dict[str, str] | None = None) -> Any:
    url = f"https://api.telegram.org/bot{token}/{method}"
    encoded = None if data is None else urllib.parse.urlencode(data).encode("utf-8")
    request = urllib.request.Request(url, data=encoded)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(body).get("description", body)
        except json.JSONDecodeError:
            detail = body
        if exc.code in {401, 404}:
            raise RuntimeError(f"Bot token rejected by Telegram (HTTP {exc.code}): {detail}") from None
        raise RuntimeError(f"Telegram HTTP {exc.code}: {detail}") from None
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Cannot reach Telegram: {exc.reason}") from None


def main() -> int:
    uz_monitor.load_env()
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID in .env", file=sys.stderr)
        return 2

    try:
        bot = telegram_request(token, "getMe")["result"]
        print(f"Token OK: @{bot.get('username', 'unknown')} (id={bot['id']})")
        result = telegram_request(
            token,
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": "UZ monitor: test message delivered successfully.",
            },
        )["result"]
        print(f"Message sent: chat_id={result['chat']['id']}, message_id={result['message_id']}")
        return 0
    except (RuntimeError, KeyError) as exc:
        print(f"Telegram test failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
