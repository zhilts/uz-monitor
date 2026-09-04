#!/usr/bin/env python3
"""Read-only availability monitor for Ukrainian Railways."""

from __future__ import annotations

import argparse
import base64
import csv
import json
import os
import random
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any


API_BASE = "https://app.uz.gov.ua/api"
DEFAULT_CONFIG = Path(__file__).with_name("config.json")
DEFAULT_ENV = Path(__file__).with_name(".env")
CHANGE_CSV_FIELDS = [
    "detected_at",
    "travel_date",
    "train_number",
    "depart_at",
    "arrive_at",
    "wagon_class",
    "wagon_name",
    "old_free_seats",
    "new_free_seats",
    "delta",
    "enough_for_2",
    "price",
    "kind",
]
SNAPSHOT_FIELDS = [
    "travel_date",
    "status",
    "train_number",
    "depart_at",
    "arrive_at",
    "wagon_class",
    "wagon_name",
    "free_seats",
    "enough_for_2",
    "free_seats_detail",
    "same_compartment",
    "same_compartment_detail",
    "price",
]


class ApiError(RuntimeError):
    pass


def node_binary() -> str:
    configured = os.environ.get("NODE_BINARY")
    candidates = [
        configured,
        shutil.which("node"),
        "/opt/homebrew/bin/node",
        "/usr/local/bin/node",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    raise ApiError("Node.js not found; install Node.js or set NODE_BINARY")


def playwright_profile_path(config: dict[str, Any]) -> Path:
    profile = Path(config.get("playwright_user_data_dir", "data/chrome-profile"))
    return profile if profile.is_absolute() else Path(__file__).parent / profile


def playwright_profile_in_use(config: dict[str, Any]) -> bool:
    lock = playwright_profile_path(config) / "SingletonLock"
    if not lock.is_symlink():
        return lock.exists()
    try:
        pid = int(os.readlink(lock).rsplit("-", 1)[1])
    except (OSError, ValueError, IndexError):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class RecaptchaRequired(ApiError):
    def __init__(self, url: str):
        super().__init__("UZ requires reCAPTCHA")
        self.url = url


@dataclass(frozen=True)
class Availability:
    travel_date: str
    train_number: str
    depart_at: str | None
    arrive_at: str | None
    wagon_class: str
    wagon_name: str
    free_seats: int
    price: int | None
    free_seats_detail: str = ""
    same_compartment: bool | None = None
    same_compartment_detail: str = ""

    @property
    def enough_for_group(self) -> bool:
        return self.free_seats >= 2


class UzClient:
    def __init__(self, session_id: str, timeout: int = 20) -> None:
        self.timeout = timeout
        self.session_id = session_id

    def _get(self, endpoint: str, params: dict[str, Any]) -> Any:
        query = urllib.parse.urlencode(params)
        request = urllib.request.Request(
            f"{API_BASE}/{endpoint}?{query}",
            headers={
                "Accept": "application/json",
                "Accept-Language": "uk",
                "Origin": "https://booking.uz.gov.ua",
                "Referer": "https://booking.uz.gov.ua/",
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 Chrome/138 Safari/537.36",
                "X-Client-Locale": "uk",
                "X-Session-Id": self.session_id,
                "X-User-Agent": "UZ/2 Web/1 User/guest",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            try:
                error_payload = json.loads(body)
            except json.JSONDecodeError:
                error_payload = None
            if isinstance(error_payload, dict) and error_payload.get("recaptcha_link"):
                raise RecaptchaRequired(error_payload["recaptcha_link"]) from exc
            raise ApiError(f"HTTP {exc.code}: {body[:300]}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise ApiError(str(exc)) from exc

        if isinstance(payload, dict) and payload.get("recaptcha_link"):
            raise RecaptchaRequired(payload["recaptcha_link"])
        return payload

    def departure_dates(self, from_id: int, to_id: int) -> list[str]:
        payload = self._get(
            "trips/departure-dates",
            {"station_from_id": from_id, "station_to_id": to_id},
        )
        return payload if isinstance(payload, list) else []

    def trips(self, from_id: int, to_id: int, travel_date: str) -> dict[str, Any]:
        payload = self._get(
            "v3/trips",
            {
                "station_from_id": from_id,
                "station_to_id": to_id,
                "with_transfers": 0,
                "date": travel_date,
            },
        )
        if not isinstance(payload, dict):
            raise ApiError(f"Unexpected trips response: {type(payload).__name__}")
        return payload


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_config(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def load_env(path: Path = DEFAULT_ENV) -> None:
    if not path.exists():
        return
    with path.open(encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            os.environ.setdefault(key, value)


def persistent_session_id(config: dict[str, Any]) -> str:
    session_path = Path(config.get("session_file", "data/session.json"))
    if not session_path.is_absolute():
        session_path = Path(__file__).parent / session_path
    if session_path.exists():
        with session_path.open(encoding="utf-8") as handle:
            value = json.load(handle).get("session_id")
        if value:
            return str(value)
    session_path.parent.mkdir(parents=True, exist_ok=True)
    value = str(uuid.uuid4())
    with session_path.open("w", encoding="utf-8") as handle:
        json.dump({"session_id": value}, handle)
        handle.write("\n")
    return value


def connect_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY,
            checked_at TEXT NOT NULL,
            status TEXT NOT NULL,
            detail TEXT
        );
        CREATE TABLE IF NOT EXISTS observations (
            id INTEGER PRIMARY KEY,
            checked_at TEXT NOT NULL,
            travel_date TEXT NOT NULL,
            train_number TEXT NOT NULL,
            depart_at TEXT,
            arrive_at TEXT,
            wagon_class TEXT NOT NULL,
            wagon_name TEXT,
            free_seats INTEGER NOT NULL,
            price INTEGER,
            UNIQUE(checked_at, travel_date, train_number, wagon_class)
        );
        CREATE TABLE IF NOT EXISTS changes (
            id INTEGER PRIMARY KEY,
            detected_at TEXT NOT NULL,
            travel_date TEXT NOT NULL,
            train_number TEXT NOT NULL,
            wagon_class TEXT NOT NULL,
            old_free_seats INTEGER,
            new_free_seats INTEGER NOT NULL,
            delta INTEGER,
            kind TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS date_status (
            checked_at TEXT NOT NULL,
            travel_date TEXT NOT NULL,
            status TEXT NOT NULL,
            detail TEXT
        );
        """
    )
    return db


def normalize_class(value: str) -> str:
    return value.strip().lower().translate(str.maketrans({"\u043a": "k"}))


def parse_availability(
    payload: dict[str, Any],
    travel_date: str,
    allowed_classes: set[str],
    coupe_wagons_by_trip: dict[str, Any] | None = None,
    passengers: int = 2,
    seat_check_errors_by_trip: dict[str, str] | None = None,
) -> list[Availability]:
    coupe_wagons_by_trip = coupe_wagons_by_trip or {}
    seat_check_errors_by_trip = seat_check_errors_by_trip or {}
    found: list[Availability] = []
    for trip in payload.get("direct", []):
        trip_id = str(trip.get("id") or "")
        seat_details, same_compartment, compartment_details = analyze_coupe_wagons(
            coupe_wagons_by_trip.get(trip_id), passengers
        )
        seat_check_error = seat_check_errors_by_trip.get(trip_id, "")
        if seat_check_error:
            if "account verification expired" in seat_check_error:
                compartment_details = seat_check_error
            else:
                compartment_details = f"seat check failed: {seat_check_error[:160]}"
        train = trip.get("train") or {}
        number = str(train.get("number") or trip.get("train_number") or "?")
        for wagon in train.get("wagon_classes", []):
            class_id = str(wagon.get("id") or "")
            class_name = str(wagon.get("name") or "")
            if not ({normalize_class(class_id), normalize_class(class_name)} & allowed_classes):
                continue
            found.append(
                Availability(
                    travel_date=travel_date,
                    train_number=number,
                    depart_at=trip.get("depart_at"),
                    arrive_at=trip.get("arrive_at"),
                    wagon_class=class_id or class_name,
                    wagon_name=class_name,
                    free_seats=int(wagon.get("free_seats") or 0),
                    price=wagon.get("price"),
                    free_seats_detail=seat_details,
                    same_compartment=same_compartment,
                    same_compartment_detail=compartment_details,
                )
            )
    return found


def analyze_coupe_wagons(
    response: list[dict[str, Any]] | dict[str, Any] | None,
    passengers: int = 2,
) -> tuple[str, bool | None, str]:
    if response is None:
        return "", None, ""
    wagons = response.get("wagons", []) if isinstance(response, dict) else response

    all_seats: list[str] = []
    matching_compartments: list[str] = []
    for wagon in wagons:
        wagon_number = str(wagon.get("number") or "?")
        seats = sorted({int(seat) for seat in wagon.get("seats", [])})
        mockup_name = str(wagon.get("mockup_name") or "").lower()
        international_numbering = (
            any(seat > 36 for seat in seats)
            or "international" in mockup_name
            or "\u043c\u0456\u0436\u043d\u0430\u0440\u043e\u0434" in mockup_name
        )
        if seats:
            all_seats.append(f"car {wagon_number}: {', '.join(map(str, seats))}")
        compartments: dict[int, list[int]] = {}
        for seat in seats:
            if international_numbering:
                compartment = seat // 10
            else:
                compartment = (seat - 1) // 4 + 1
            compartments.setdefault(compartment, []).append(seat)
        for compartment, compartment_seats in compartments.items():
            if len(compartment_seats) >= passengers:
                labeled_seats = [
                    format_seat(seat, international_numbering)
                    for seat in compartment_seats
                ]
                matching_compartments.append(
                    f"car {wagon_number}, compartment {compartment}: "
                    + ", ".join(labeled_seats)
                )

    return (
        "; ".join(all_seats),
        bool(matching_compartments),
        "; ".join(matching_compartments),
    )


def last_seat_count(
    db: sqlite3.Connection, item: Availability
) -> int | None:
    row = db.execute(
        """
        SELECT free_seats FROM observations
        WHERE travel_date=? AND train_number=? AND wagon_class=?
        ORDER BY id DESC LIMIT 1
        """,
        (item.travel_date, item.train_number, item.wagon_class),
    ).fetchone()
    return None if row is None else int(row[0])


def change_kind(previous: int | None, current: int) -> str:
    if previous is None:
        return "first_seen"
    if current > previous:
        return "appeared"
    return "disappeared"


def save_observation(
    db: sqlite3.Connection, checked_at: str, item: Availability
) -> tuple[int | None, int]:
    previous = last_seat_count(db, item)
    db.execute(
        """
        INSERT INTO observations
        (checked_at, travel_date, train_number, depart_at, arrive_at,
         wagon_class, wagon_name, free_seats, price)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            checked_at,
            item.travel_date,
            item.train_number,
            item.depart_at,
            item.arrive_at,
            item.wagon_class,
            item.wagon_name,
            item.free_seats,
            item.price,
        ),
    )
    if previous != item.free_seats:
        delta = None if previous is None else item.free_seats - previous
        db.execute(
            """
            INSERT INTO changes
            (detected_at, travel_date, train_number, wagon_class,
             old_free_seats, new_free_seats, delta, kind)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                checked_at,
                item.travel_date,
                item.train_number,
                item.wagon_class,
                previous,
                item.free_seats,
                delta,
                change_kind(previous, item.free_seats),
            ),
        )
    return previous, item.free_seats


def append_change_csv(
    path: Path,
    detected_at: str,
    item: Availability,
    previous: int | None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not path.exists()
    delta = None if previous is None else item.free_seats - previous
    with path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=CHANGE_CSV_FIELDS,
        )
        if is_new:
            writer.writeheader()
        writer.writerow(
            {
                "detected_at": detected_at,
                "travel_date": item.travel_date,
                "train_number": item.train_number,
                "depart_at": item.depart_at,
                "arrive_at": item.arrive_at,
                "wagon_class": item.wagon_class,
                "wagon_name": item.wagon_name,
                "old_free_seats": "" if previous is None else previous,
                "new_free_seats": item.free_seats,
                "delta": "" if delta is None else delta,
                "enough_for_2": item.free_seats >= 2,
                "price": "" if item.price is None else item.price,
                "kind": change_kind(previous, item.free_seats),
            }
        )


def ensure_change_csv(path: Path) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        csv.DictWriter(handle, fieldnames=CHANGE_CSV_FIELDS).writeheader()


def persist_snapshot(
    directory: Path, checked_at: str, rows: list[dict[str, Any]]
) -> Path | None:
    directory.mkdir(parents=True, exist_ok=True)
    candidate = directory / ".pending.csv"
    with candidate.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=SNAPSHOT_FIELDS)
        writer.writeheader()
        writer.writerows(sorted(rows, key=lambda row: (
            row["travel_date"], row["train_number"], row["wagon_class"]
        )))

    previous_files = sorted(directory.glob("snapshot-*.csv"))
    if previous_files:
        previous = previous_files[-1]
        if candidate.read_bytes() == previous.read_bytes():
            candidate.unlink()
            return None
        with previous.open(encoding="utf-8") as handle:
            previous_rows = list(csv.DictReader(handle))
        previous_trains = sum(bool(row["train_number"]) for row in previous_rows)
        current_trains = sum(bool(row["train_number"]) for row in rows)
        if previous_trains >= 3 and current_trains * 2 < previous_trains:
            candidate.unlink()
            raise ApiError(
                "Snapshot rejected: train coverage collapsed "
                f"from {previous_trains} to {current_trains}"
            )

    timestamp = datetime.fromisoformat(checked_at).strftime("%Y%m%dT%H%M%SZ")
    destination = directory / f"snapshot-{timestamp}.csv"
    candidate.replace(destination)
    return destination


def snapshot_rows_for_date(
    travel_date: str, status: str, items: list[Availability]
) -> list[dict[str, Any]]:
    if not items:
        return [{
            "travel_date": travel_date,
            "status": status,
            "train_number": "",
            "depart_at": "",
            "arrive_at": "",
            "wagon_class": "",
            "wagon_name": "",
            "free_seats": "",
            "enough_for_2": "",
            "free_seats_detail": "",
            "same_compartment": "",
            "same_compartment_detail": "",
            "price": "",
        }]
    return [{
        "travel_date": travel_date,
        "status": status,
        "train_number": item.train_number,
        "depart_at": item.depart_at or "",
        "arrive_at": item.arrive_at or "",
        "wagon_class": item.wagon_class,
        "wagon_name": item.wagon_name,
        "free_seats": item.free_seats,
        "enough_for_2": item.free_seats >= 2,
        "free_seats_detail": item.free_seats_detail,
        "same_compartment": (
            "" if item.same_compartment is None else item.same_compartment
        ),
        "same_compartment_detail": item.same_compartment_detail,
        "price": "" if item.price is None else item.price,
    } for item in items]


def read_snapshot(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def load_cached_availability(directory: Path) -> dict[tuple[str, str, str], dict[str, str]]:
    cached: dict[tuple[str, str, str], dict[str, str]] = {}
    for path in reversed(sorted(directory.glob("snapshot-*.csv"))):
        for row in read_snapshot(path):
            key = (row["travel_date"], row["train_number"], row["wagon_class"])
            if key not in cached and row.get("free_seats_detail"):
                cached[key] = row
    return cached


def parse_cached_wagons(details: str) -> list[dict[str, Any]]:
    wagons = []
    for segment in details.split(";"):
        car, separator, seat_values = segment.strip().partition(":")
        if not separator or not car.startswith("car "):
            continue
        seats = [int(value.strip()) for value in seat_values.split(",")]
        wagons.append({"number": car.removeprefix("car "), "seats": seats})
    return wagons


def apply_cached_seat_details(
    items: list[Availability],
    cached: dict[tuple[str, str, str], dict[str, str]],
    passengers: int,
) -> list[Availability]:
    updated = []
    for item in items:
        key = (item.travel_date, item.train_number, item.wagon_class)
        previous = cached.get(key)
        if item.free_seats_detail or not previous:
            updated.append(item)
            continue
        if str(item.free_seats) != previous.get("free_seats"):
            updated.append(item)
            continue
        seat_details, same_compartment, compartment_details = analyze_coupe_wagons(
            parse_cached_wagons(previous["free_seats_detail"]), passengers
        )
        updated.append(
            replace(
                item,
                free_seats_detail=seat_details,
                same_compartment=same_compartment,
                same_compartment_detail=compartment_details,
            )
        )
    return updated


def format_seat(seat: int, international_numbering: bool) -> str:
    if international_numbering:
        berth = "lower" if seat % 10 in {1, 2} else "upper"
    else:
        berth = "lower" if seat % 2 else "upper"
    return f"{seat} ({berth})"


def summarize_compartments(details: str) -> str:
    formatted = []
    for value in details.split(";"):
        compartment, separator, seats = value.strip().partition(":")
        if not separator:
            continue
        seat_values = [seat_value.strip() for seat_value in seats.split(",")]
        if all("(" in seat_value for seat_value in seat_values):
            formatted.append(f"{compartment}: {', '.join(seat_values)}")
            continue
        seat_numbers = [int(seat_value) for seat_value in seat_values]
        international_numbering = any(seat > 36 for seat in seat_numbers)
        labeled_seats = []
        for seat in seat_numbers:
            labeled_seats.append(format_seat(seat, international_numbering))
        formatted.append(f"{compartment}: {', '.join(labeled_seats)}")
    return "; ".join(formatted)


def snapshot_differences(previous: Path, current: Path) -> list[str]:
    def indexed(path: Path) -> dict[tuple[str, str, str], dict[str, str]]:
        return {
            (row["travel_date"], row["train_number"], row["wagon_class"]): row
            for row in read_snapshot(path)
        }

    old_rows = indexed(previous)
    new_rows = indexed(current)
    differences = []
    verification_required = False
    for key in sorted(old_rows.keys() | new_rows.keys()):
        old = old_rows.get(key)
        new = new_rows.get(key)
        state_fields = (
            "status",
            "free_seats",
            "same_compartment",
            "same_compartment_detail",
        )
        old_state = None if old is None else tuple(
            old.get(field, "") for field in state_fields
        )
        new_state = None if new is None else tuple(
            new.get(field, "") for field in state_fields
        )
        if old_state == new_state:
            continue
        travel_date, train_number, wagon_class = key
        old_seats = (
            "no data" if old is None or old["free_seats"] == "" else old["free_seats"]
        )
        new_seats = (
            "no data" if new is None or new["free_seats"] == "" else new["free_seats"]
        )
        train = train_number or "no direct train"
        wagon = f", {wagon_class}" if wagon_class else ""
        message = f"{travel_date}: {train}{wagon}: {old_seats} → {new_seats}"
        if new and new.get("same_compartment") == "True":
            details = summarize_compartments(new["same_compartment_detail"])
            message += f"; together: {details}"
        elif new and new.get("same_compartment") == "False":
            message += "; no two seats in one compartment"
        elif new and new.get("same_compartment_detail", "").startswith(
            "UZ account verification"
        ):
            verification_required = True
        elif new and new.get("same_compartment_detail", "").startswith("seat check"):
            message += "; seat check failed"
        differences.append(message)
    if verification_required:
        differences.append("Exact-seat scan requires UZ account verification.")
    return differences


def available_snapshot_lines(path: Path) -> list[str]:
    available = []
    for row in read_snapshot(path):
        value = row.get("free_seats", "")
        if not value or int(value) <= 0:
            continue
        line = (
            f"{row['travel_date']}: {row['train_number']}, "
            f"{row['wagon_class']} — {value} seats"
        )
        if row.get("same_compartment") == "True":
            details = summarize_compartments(row["same_compartment_detail"])
            line += f"; together: {details}"
        elif row.get("same_compartment") == "False":
            line += "; no two seats in one compartment"
        elif int(value) >= 2:
            line += "; exact seats unknown"
        available.append(line)
    return available or ["No available coupe seats."]


def telegram(text: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    chunks: list[str] = []
    current = ""
    for line in text.splitlines():
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > 4000 and current:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    for chunk in chunks:
        body = urllib.parse.urlencode({"chat_id": chat_id, "text": chunk}).encode()
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", data=body
        )
        with urllib.request.urlopen(request, timeout=15):
            pass


def dates_to_check(config: dict[str, Any], advertised: list[str]) -> list[str]:
    today = date.today()
    horizon = today + timedelta(days=int(config["sale_horizon_days"]) - 1)
    dates = [value for value in advertised if today <= date.fromisoformat(value) <= horizon]

    # One low-cost boundary probe lets the data prove when the sales horizon changes.
    boundary = today + timedelta(days=int(config.get("boundary_probe_days", 15)))
    boundary_value = boundary.isoformat()
    if boundary_value in advertised and boundary_value not in dates:
        dates.append(boundary_value)
    return dates


def configured_dates(config: dict[str, Any]) -> list[str]:
    if config.get("travel_dates"):
        return [str(value) for value in config["travel_dates"]]
    today = date.today()
    horizon = int(config["sale_horizon_days"])
    dates = [(today + timedelta(days=offset)).isoformat() for offset in range(horizon)]
    boundary = int(config.get("boundary_probe_days", horizon))
    boundary_date = (today + timedelta(days=boundary)).isoformat()
    if boundary_date not in dates:
        dates.append(boundary_date)
    return dates


def request_timing_ms(config: dict[str, Any]) -> tuple[int, int]:
    delay_ms = int(max(float(config.get("request_delay_seconds", 12)), 10) * 1000)
    jitter_ms = int(max(float(config.get("request_jitter_seconds", 4)), 0) * 1000)
    return delay_ms, jitter_ms


def playwright_trips(
    config: dict[str, Any],
    travel_dates: list[str],
    cached: dict[tuple[str, str, str], dict[str, str]],
) -> list[dict[str, Any]]:
    project_dir = Path(__file__).parent
    storage_state = Path(config["playwright_storage_state"])
    if not storage_state.is_absolute():
        storage_state = project_dir / storage_state
    user_data_dir = playwright_profile_path(config)
    request_delay_ms, request_jitter_ms = request_timing_ms(config)
    payload = {
        "dates": travel_dates,
        "from_station_id": config["from_station_id"],
        "to_station_id": config["to_station_id"],
        "storage_state": str(storage_state),
        "user_data_dir": str(user_data_dir),
        "request_delay_ms": request_delay_ms,
        "request_jitter_ms": request_jitter_ms,
        "passengers": int(config.get("passengers", 2)),
        "exact_seat_checks_enabled": bool(
            config.get("exact_seat_checks_enabled", True)
        ),
        "cached_availability": {
            f"{travel_date}|{train_number}": int(row["free_seats"])
            for (travel_date, train_number, _), row in cached.items()
        },
        "force_seat_check": bool(config.get("force_seat_check", False)),
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, ensure_ascii=False).encode("utf-8")
    ).decode("ascii")
    result = subprocess.run(
        [node_binary(), str(project_dir / "playwright_fetch.mjs"), encoded],
        cwd=project_dir,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    if result.returncode != 0:
        raise ApiError(f"Playwright failed: {result.stderr.strip()[-1000:]}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ApiError(f"Invalid Playwright output: {result.stdout[-500:]}") from exc


def route_config(base: dict[str, Any], route: dict[str, Any]) -> dict[str, Any]:
    merged = {key: value for key, value in base.items() if key != "routes"}
    merged.update(route)
    route_id = str(route["id"])
    defaults = {
        "database": f"data/{route_id}/uz-monitor.sqlite3",
        "changes_csv": f"data/{route_id}/changes.csv",
        "snapshot_dir": f"data/{route_id}/snapshots",
    }
    for key, value in defaults.items():
        if key not in route:
            merged[key] = value
    return merged


def run_once(config: dict[str, Any]) -> int:
    routes = config.get("routes")
    if routes:
        enabled = [route for route in routes if route.get("enabled", True)]
        if not enabled:
            raise ApiError("No enabled routes configured")
        return max(run_route_once(route_config(config, route)) for route in enabled)
    return run_route_once(config)


def run_route_once(config: dict[str, Any]) -> int:
    transport = config.get("transport", "api")
    if transport == "playwright" and playwright_profile_in_use(config):
        route_name = config.get("name", "route")
        telegram(
            f"UZ scan skipped ({route_name}): the interactive browser session "
            "is open. Use /close to resume scheduled monitoring or /scan to "
            "close it and scan now."
        )
        print(
            f"{utc_now()}: {route_name}: "
            "skipped; interactive browser session is open"
        )
        return 0
    db_path = Path(config["database"])
    if not db_path.is_absolute():
        db_path = Path(__file__).parent / db_path
    db = connect_db(db_path)
    csv_path = Path(config.get("changes_csv", "data/changes.csv"))
    if not csv_path.is_absolute():
        csv_path = Path(__file__).parent / csv_path
    ensure_change_csv(csv_path)
    snapshot_dir = Path(config.get("snapshot_dir", "data/snapshots"))
    if not snapshot_dir.is_absolute():
        snapshot_dir = Path(__file__).parent / snapshot_dir
    checked_at = utc_now()
    allowed = {normalize_class(value) for value in config["wagon_class_ids"]}
    snapshot_rows: list[dict[str, Any]] = []
    cached_availability = load_cached_availability(snapshot_dir)

    try:
        if transport == "playwright":
            travel_dates = configured_dates(config)
            trip_results = playwright_trips(config, travel_dates, cached_availability)
        else:
            client = UzClient(persistent_session_id(config))
            advertised = client.departure_dates(
                config["from_station_id"], config["to_station_id"]
            )
            travel_dates = dates_to_check(config, advertised)
            trip_results = []
        if not travel_dates:
            raise ApiError("API returned no dates in the configured sales window")

        for index, travel_date in enumerate(travel_dates):
            try:
                coupe_wagons_by_trip = {}
                seat_check_errors_by_trip = {}
                if transport == "playwright":
                    result = trip_results[index]
                    if result.get("error"):
                        raise ApiError(result["error"])
                    payload = result["payload"]
                    coupe_wagons_by_trip = result.get("coupe_wagons_by_trip", {})
                    seat_check_errors_by_trip = result.get(
                        "seat_check_errors_by_trip", {}
                    )
                    if isinstance(payload, dict) and payload.get("recaptcha_link"):
                        raise RecaptchaRequired(payload["recaptcha_link"])
                else:
                    payload = client.trips(
                        config["from_station_id"], config["to_station_id"], travel_date
                    )
                items = parse_availability(
                    payload,
                    travel_date,
                    allowed,
                    coupe_wagons_by_trip,
                    int(config.get("passengers", 2)),
                    seat_check_errors_by_trip,
                )
                if config.get("exact_seat_checks_enabled", True):
                    items = apply_cached_seat_details(
                        items,
                        cached_availability,
                        int(config.get("passengers", 2)),
                    )
                status = "available" if any(item.free_seats for item in items) else "no_coupe"
                if payload.get("not_on_sale"):
                    status = "not_on_sale"
                elif not payload.get("direct"):
                    status = "no_direct_or_not_on_sale"
                db.execute(
                    "INSERT INTO date_status VALUES (?, ?, ?, ?)",
                    (checked_at, travel_date, status, None),
                )
                snapshot_rows.extend(snapshot_rows_for_date(travel_date, status, items))
                for item in items:
                    previous, current = save_observation(db, checked_at, item)
                    if previous is not None and previous != current:
                        append_change_csv(csv_path, checked_at, item, previous)
            except RecaptchaRequired:
                raise
            except ApiError as exc:
                db.execute(
                    "INSERT INTO date_status VALUES (?, ?, ?, ?)",
                    (checked_at, travel_date, "error", str(exc)),
                )
                snapshot_rows.extend(snapshot_rows_for_date(travel_date, "error", []))
            if transport != "playwright" and index + 1 < len(travel_dates):
                base_delay = float(config.get("request_delay_seconds", 1.5))
                time.sleep(base_delay + random.uniform(0, 0.5))

        previous_snapshots = sorted(snapshot_dir.glob("snapshot-*.csv"))
        previous_snapshot = previous_snapshots[-1] if previous_snapshots else None
        saved_snapshot = persist_snapshot(snapshot_dir, checked_at, snapshot_rows)
        if saved_snapshot and previous_snapshot:
            differences = snapshot_differences(previous_snapshot, saved_snapshot)
            if differences:
                route_name = config.get("name") or (
                    f"{config.get('from_station_name', '?')} → "
                    f"{config.get('to_station_name', '?')}"
                )
                telegram(
                    f"UZ snapshot changed ({route_name}):\n\nChanges:\n"
                    + "\n".join(differences)
                    + "\n\nAvailable now:\n"
                    + "\n".join(available_snapshot_lines(saved_snapshot))
                )
        db.execute("INSERT INTO runs (checked_at, status) VALUES (?, 'ok')", (checked_at,))
        db.commit()
        snapshot_result = str(saved_snapshot) if saved_snapshot else "unchanged (deleted)"
        print(
            f"{checked_at}: {config.get('name', 'route')}: "
            f"checked {len(travel_dates)} dates; "
            f"snapshot={snapshot_result}"
        )
        return 0
    except RecaptchaRequired as exc:
        db.execute(
            "INSERT INTO runs (checked_at, status, detail) VALUES (?, 'recaptcha', ?)",
            (checked_at, exc.url),
        )
        db.commit()
        print(f"reCAPTCHA required: {exc.url}", file=sys.stderr)
        return 2
    except ApiError as exc:
        db.execute(
            "INSERT INTO runs (checked_at, status, detail) VALUES (?, 'error', ?)",
            (checked_at, str(exc)),
        )
        db.commit()
        print(f"API error: {exc}", file=sys.stderr)
        return 1
    finally:
        db.close()


def print_report(config: dict[str, Any], limit: int) -> int:
    db_path = Path(config["database"])
    if not db_path.is_absolute():
        db_path = Path(__file__).parent / db_path
    db = connect_db(db_path)
    rows = db.execute(
        """
        SELECT detected_at, travel_date, train_number, old_free_seats,
               new_free_seats, delta, kind
        FROM changes ORDER BY id DESC LIMIT ?
        """,
        (limit,),
    ).fetchall()
    if not rows:
        print("No availability changes recorded yet.")
    for row in rows:
        print(" | ".join("-" if value is None else str(value) for value in row))
    db.close()
    return 0


def main() -> int:
    load_env()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    subparsers = parser.add_subparsers(dest="command", required=True)
    once = subparsers.add_parser("once", help="perform one monitoring pass")
    once.add_argument(
        "--force-seat-check",
        action="store_true",
        help="ignore cached seat details after manual UZ verification",
    )
    report = subparsers.add_parser("report", help="show recent seat-count changes")
    report.add_argument("--limit", type=int, default=50)
    args = parser.parse_args()
    config = load_config(args.config)
    if args.command == "once":
        config["force_seat_check"] = args.force_seat_check
        return run_once(config)
    return print_report(config, args.limit)


if __name__ == "__main__":
    raise SystemExit(main())
