#!/usr/bin/env python3
"""Open UZ verification and force an exact-seat scan."""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
from datetime import date, timedelta
from pathlib import Path

import uz_monitor


PROJECT_DIR = Path(__file__).parent


def enabled_routes(config: dict) -> list[dict]:
    return [route for route in config.get("routes", []) if route.get("enabled", True)]


def choose_route(config: dict, route_id: str | None) -> dict:
    routes = enabled_routes(config)
    if route_id:
        routes = [route for route in routes if route.get("id") == route_id]
    if not routes:
        raise SystemExit("No matching enabled route found")
    return routes[0]


def choose_date(config: dict, route: dict, requested: str | None) -> str:
    if requested:
        return requested
    route_settings = uz_monitor.route_config(config, route)
    snapshot_dir = PROJECT_DIR / route_settings["snapshot_dir"]
    snapshots = sorted(snapshot_dir.glob("snapshot-*.csv"))
    if snapshots:
        candidates = [
            row["travel_date"]
            for row in uz_monitor.read_snapshot(snapshots[-1])
            if row.get("free_seats")
            and int(row["free_seats"]) >= int(config.get("passengers", 2))
        ]
        if candidates:
            return sorted(candidates)[0]
    horizon = int(config.get("sale_horizon_days", 14))
    return (date.today() + timedelta(days=horizon - 1)).isoformat()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--route", help="route id; defaults to the first enabled route")
    parser.add_argument("--date", help="verification date in YYYY-MM-DD format")
    args = parser.parse_args()

    uz_monitor.load_env()
    config = uz_monitor.load_config(uz_monitor.DEFAULT_CONFIG)
    route = choose_route(config, args.route)
    travel_date = choose_date(config, route, args.date)
    profile = Path(config.get("playwright_user_data_dir", "data/chrome-profile"))
    if not profile.is_absolute():
        profile = PROJECT_DIR / profile
    url = (
        f"https://booking.uz.gov.ua/search-trips/{route['from_station_id']}/"
        f"{route['to_station_id']}/list?startDate={travel_date}"
    )
    payload = base64.urlsafe_b64encode(
        json.dumps({"url": url, "user_data_dir": str(profile)}).encode()
    ).decode()
    subprocess.run(
        [uz_monitor.node_binary(), str(PROJECT_DIR / "playwright_verify.mjs"), payload],
        cwd=PROJECT_DIR,
        check=True,
    )
    config["force_seat_check"] = True
    return uz_monitor.run_once(config)


if __name__ == "__main__":
    raise SystemExit(main())
