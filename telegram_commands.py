#!/usr/bin/env python3
"""Handle a restricted set of local UZ monitor commands from Telegram."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import subprocess
import time
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path
from typing import Any

import uz_monitor
from verify_and_scan import choose_date, choose_route


PROJECT_DIR = Path(__file__).parent
OFFSET_FILE = PROJECT_DIR / "data/telegram-command-offset.json"
LOCK_FILE = PROJECT_DIR / "data/telegram-command.lock"
BOT_COMMANDS = [
    {"command": "status", "description": "Show current availability"},
    {"command": "session", "description": "Open the monitor's UZ session"},
    {"command": "close", "description": "Close the monitor's UZ session"},
    {"command": "scan", "description": "Run an exact-seat scan"},
    {"command": "help", "description": "Show commands and buttons"},
]
MENU_KEYBOARD = {
    "keyboard": [
        [{"text": "/status"}, {"text": "/session"}],
        [{"text": "/close"}, {"text": "/scan"}],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
    "input_field_placeholder": "Choose a UZ monitor action",
}


def telegram_api(
    method: str, params: dict[str, Any], request_timeout: int = 20
) -> dict[str, Any]:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    query = urllib.parse.urlencode(params)
    with urllib.request.urlopen(
        f"https://api.telegram.org/bot{token}/{method}?{query}",
        timeout=request_timeout,
    ) as response:
        payload = json.load(response)
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram {method} failed: {payload}")
    return payload


def read_offset() -> int:
    if not OFFSET_FILE.exists():
        return 0
    with OFFSET_FILE.open(encoding="utf-8") as handle:
        return int(json.load(handle).get("offset", 0))


def write_offset(offset: int) -> None:
    OFFSET_FILE.parent.mkdir(parents=True, exist_ok=True)
    with OFFSET_FILE.open("w", encoding="utf-8") as handle:
        json.dump({"offset": offset}, handle)
        handle.write("\n")


def parse_command(text: str) -> tuple[str, list[str]]:
    parts = text.strip().split()
    if not parts:
        return "", []
    command = parts[0].split("@", 1)[0].lower()
    return command, parts[1:]


def profile_path(config: dict[str, Any]) -> Path:
    profile = Path(config.get("playwright_user_data_dir", "data/chrome-profile"))
    return profile if profile.is_absolute() else PROJECT_DIR / profile


def latest_snapshot(config: dict[str, Any], route: dict[str, Any]) -> Path | None:
    settings = uz_monitor.route_config(config, route)
    directory = Path(settings["snapshot_dir"])
    if not directory.is_absolute():
        directory = PROJECT_DIR / directory
    snapshots = sorted(directory.glob("snapshot-*.csv"))
    return snapshots[-1] if snapshots else None


def send_status(config: dict[str, Any]) -> None:
    for route in [r for r in config.get("routes", []) if r.get("enabled", True)]:
        snapshot = latest_snapshot(config, route)
        lines = (
            uz_monitor.available_snapshot_lines(snapshot)
            if snapshot
            else ["No snapshot collected yet."]
        )
        name = route.get("name", route["id"])
        uz_monitor.telegram(f"UZ current availability ({name}):\n" + "\n".join(lines))


def send_menu() -> None:
    telegram_api(
        "sendMessage",
        {
            "chat_id": os.environ["TELEGRAM_CHAT_ID"],
            "text": (
                "UZ monitor commands:\n"
                "/status — show all current availability\n"
                "/session — open the monitor's UZ browser session\n"
                "/close — close the monitor browser without scanning\n"
                "/scan — close the monitor browser and refresh exact seats"
            ),
            "reply_markup": json.dumps(MENU_KEYBOARD),
        },
    )


def install_menu() -> None:
    telegram_api("setMyCommands", {"commands": json.dumps(BOT_COMMANDS)})
    send_menu()


def open_verification(
    config: dict[str, Any], route_id: str | None, requested_date: str | None
) -> None:
    if uz_monitor.playwright_profile_in_use(config):
        uz_monitor.telegram("UZ browser session is already open.")
        return
    route = choose_route(config, route_id)
    travel_date = choose_date(config, route, requested_date)
    date.fromisoformat(travel_date)
    url = (
        f"https://booking.uz.gov.ua/search-trips/{route['from_station_id']}/"
        f"{route['to_station_id']}/list?startDate={travel_date}"
    )
    subprocess.run(
        [
            "open",
            "-na",
            "Google Chrome",
            "--args",
            f"--user-data-dir={profile_path(config)}",
            url,
        ],
        check=True,
    )
    uz_monitor.telegram(
        f"UZ browser session opened for {route.get('name', route['id'])} on "
        f"{travel_date}. Browse or complete verification, then send /scan "
        "to refresh exact seats."
    )


def stop_monitor_browser(profile: Path) -> None:
    marker = f"--user-data-dir={profile}"
    def matching_pids() -> list[int]:
        output = subprocess.run(
            ["ps", "-axo", "pid=,command="],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        return [
            int(line.strip().partition(" ")[0])
            for line in output.splitlines()
            if marker in line and "telegram_commands.py" not in line
        ]

    pids = matching_pids()
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    for _ in range(20):
        if not matching_pids():
            break
        time.sleep(0.25)
    for pid in matching_pids():
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    for _ in range(20):
        if not matching_pids():
            break
        time.sleep(0.25)
    if matching_pids():
        raise RuntimeError("Could not stop the monitor browser")
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        (profile / name).unlink(missing_ok=True)


def force_scan(config: dict[str, Any]) -> None:
    if not config.get("exact_seat_checks_enabled", True):
        uz_monitor.telegram(
            "UZ exact-seat checks are disabled in the local configuration."
        )
        return
    stop_monitor_browser(profile_path(config))
    config["force_seat_check"] = True
    uz_monitor.telegram("UZ exact-seat scan started.")
    result = uz_monitor.run_once(config)
    send_status(config)
    if result != 0:
        uz_monitor.telegram(f"UZ exact-seat scan failed with exit code {result}.")


def close_session(config: dict[str, Any]) -> None:
    stop_monitor_browser(profile_path(config))
    uz_monitor.telegram(
        "UZ browser session closed. Scheduled monitoring can use the profile again."
    )


def handle_command(config: dict[str, Any], text: str) -> None:
    command, _ = parse_command(text)
    if command == "/status":
        send_status(config)
    elif command == "/session":
        open_verification(config, None, None)
    elif command == "/close":
        close_session(config)
    elif command == "/scan":
        force_scan(config)
    elif command in {"/help", "/start", "/menu"}:
        send_menu()


def poll(config: dict[str, Any], timeout: int = 0) -> None:
    authorized_chat = str(os.environ["TELEGRAM_CHAT_ID"])
    offset = read_offset()
    updates = telegram_api(
        "getUpdates",
        {"offset": offset, "timeout": timeout},
        request_timeout=timeout + 10,
    )["result"]
    for update in updates:
        message = update.get("message") or {}
        chat_id = str((message.get("chat") or {}).get("id", ""))
        text = str(message.get("text") or "")
        if chat_id == authorized_chat:
            try:
                handle_command(config, text)
            except Exception as exc:
                uz_monitor.telegram(f"UZ command failed: {exc}")
        offset = max(offset, int(update["update_id"]) + 1)
        write_offset(offset)


def listen(config: dict[str, Any]) -> None:
    while True:
        try:
            poll(config, timeout=50)
        except Exception as exc:
            print(f"Telegram listener error: {exc}", flush=True)
            time.sleep(5)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--command", help="run one command locally instead of polling")
    parser.add_argument(
        "--once", action="store_true", help="poll once instead of listening continuously"
    )
    parser.add_argument(
        "--install-menu", action="store_true", help="install Telegram command menus"
    )
    args = parser.parse_args()
    uz_monitor.load_env()
    config = uz_monitor.load_config(uz_monitor.DEFAULT_CONFIG)
    if args.install_menu:
        install_menu()
        return 0
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_FILE.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        if args.command:
            handle_command(config, args.command)
        elif args.once:
            poll(config)
        else:
            listen(config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
