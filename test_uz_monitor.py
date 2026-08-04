import csv
import os
import tempfile
import unittest
import urllib.error
from datetime import date, timedelta
from io import BytesIO
from pathlib import Path

import uz_monitor
import telegram_commands


class MonitorTests(unittest.TestCase):
    def test_detects_live_playwright_profile_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory)
            (profile / "SingletonLock").symlink_to(f"test-host-{os.getpid()}")
            self.assertTrue(
                uz_monitor.playwright_profile_in_use(
                    {"playwright_user_data_dir": str(profile)}
                )
            )

    def test_ignores_stale_playwright_profile_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory)
            (profile / "SingletonLock").symlink_to("test-host-999999999")
            self.assertFalse(
                uz_monitor.playwright_profile_in_use(
                    {"playwright_user_data_dir": str(profile)}
                )
            )

    def test_parses_telegram_commands_and_optional_bot_name(self):
        self.assertEqual(telegram_commands.parse_command("/status@uz_bot"), ("/status", []))
        self.assertEqual(telegram_commands.parse_command("/session"), ("/session", []))
        self.assertEqual(telegram_commands.parse_command("/close"), ("/close", []))

    def test_http_441_recaptcha_is_recognized(self):
        client = uz_monitor.UzClient("test-session")
        response = urllib.error.HTTPError(
            "https://example.test", 441, "captcha", {}, BytesIO(
                b'{"message":"Error","recaptcha_link":"https://captcha.test/c"}'
            )
        )
        original = urllib.request.urlopen
        urllib.request.urlopen = lambda *args, **kwargs: (_ for _ in ()).throw(response)
        try:
            with self.assertRaises(uz_monitor.RecaptchaRequired):
                client.trips(1, 2, "2026-08-01")
        finally:
            urllib.request.urlopen = original

    def test_parse_only_coupe(self):
        payload = {
            "direct": [{
                "depart_at": "2026-08-01T10:00:00",
                "train": {
                    "number": "032P",
                    "wagon_classes": [
                        {"id": "K", "name": "Coupe", "free_seats": 7, "price": 100},
                        {"id": "P", "name": "Berth", "free_seats": 20},
                    ],
                },
            }]
        }
        result = uz_monitor.parse_availability(payload, "2026-08-01", {"k", "coupe"})
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].free_seats, 7)

    def test_finds_two_seats_in_same_compartment(self):
        seats, together, details = uz_monitor.analyze_coupe_wagons([
            {"number": "5", "seats": [1, 4, 6, 9, 10]},
            {"number": "7", "seats": [20]},
        ])
        self.assertEqual(seats, "car 5: 1, 4, 6, 9, 10; car 7: 20")
        self.assertTrue(together)
        self.assertIn("car 5, compartment 1: 1, 4", details)
        self.assertIn("car 5, compartment 3: 9, 10", details)

    def test_does_not_mix_seats_from_different_compartments(self):
        _, together, details = uz_monitor.analyze_coupe_wagons([
            {"number": "5", "seats": [4, 5]},
        ])
        self.assertFalse(together)
        self.assertEqual(details, "")

    def test_groups_international_uic_seat_numbers(self):
        seats, together, details = uz_monitor.analyze_coupe_wagons([
            {"number": "28", "seats": [82, 86]},
            {"number": "26", "seats": [56, 75]},
        ])
        self.assertEqual(seats, "car 28: 82, 86; car 26: 56, 75")
        self.assertTrue(together)
        self.assertEqual(details, "car 28, compartment 8: 82, 86")

    def test_uses_international_mockup_for_low_uic_numbers(self):
        _, together, details = uz_monitor.analyze_coupe_wagons([
            {
                "number": "1",
                "mockup_name": "International sleeping car",
                "seats": [21, 26],
            },
        ])
        self.assertTrue(together)
        self.assertEqual(details, "car 1, compartment 2: 21, 26")

    def test_reuses_and_recalculates_cached_seats_when_count_is_unchanged(self):
        item = uz_monitor.Availability(
            "2026-08-09",
            "032P",
            None,
            None,
            "K",
            "Coupe",
            5,
            None,
            same_compartment_detail="seat check failed: HTTP 443",
        )
        cached = {
            ("2026-08-09", "032P", "K"): {
                "free_seats": "5",
                "free_seats_detail": "car 26: 56, 75; car 28: 82, 86; car 29: 35",
            },
        }
        result = uz_monitor.apply_cached_seat_details([item], cached, 2)
        self.assertTrue(result[0].same_compartment)
        self.assertEqual(
            result[0].same_compartment_detail,
            "car 28, compartment 8: 82, 86",
        )

    def test_does_not_reuse_cached_seats_when_count_changed(self):
        item = uz_monitor.Availability(
            "2026-08-09", "032P", None, None, "K", "Coupe", 4, None
        )
        cached = {
            ("2026-08-09", "032P", "K"): {
                "free_seats": "5",
                "free_seats_detail": "car 28: 82, 86",
            },
        }
        self.assertEqual(
            uz_monitor.apply_cached_seat_details([item], cached, 2),
            [item],
        )

    def test_reports_expired_verification_without_claiming_no_compartment(self):
        payload = {"direct": [{
            "id": 101,
            "train": {"number": "032P", "wagon_classes": [
                {"id": "K", "name": "Coupe", "free_seats": 3},
            ]},
        }]}
        result = uz_monitor.parse_availability(
            payload,
            "2026-08-09",
            {"k"},
            seat_check_errors_by_trip={
                "101": "UZ account verification expired; exact seats unknown",
            },
        )
        self.assertIsNone(result[0].same_compartment)
        self.assertEqual(
            result[0].same_compartment_detail,
            "UZ account verification expired; exact seats unknown",
        )

    def test_available_snapshot_lists_only_positive_counts(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.csv"
            rows = []
            for travel_date, seats in (("2026-08-11", 0), ("2026-08-12", 10)):
                row = {field: "" for field in uz_monitor.SNAPSHOT_FIELDS}
                row.update({
                    "travel_date": travel_date,
                    "train_number": "032P",
                    "wagon_class": "K",
                    "free_seats": seats,
                })
                rows.append(row)
            with path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=uz_monitor.SNAPSHOT_FIELDS)
                writer.writeheader()
                writer.writerows(rows)
            lines = uz_monitor.available_snapshot_lines(path)
            self.assertEqual(len(lines), 1)
            self.assertIn("2026-08-12", lines[0])
            self.assertIn("10 seats", lines[0])

    def test_verification_warning_appears_once_for_multiple_dates(self):
        with tempfile.TemporaryDirectory() as directory:
            old_path = Path(directory) / "old.csv"
            new_path = Path(directory) / "new.csv"
            old_rows = []
            new_rows = []
            for travel_date in ("2026-08-11", "2026-08-12"):
                base = {field: "" for field in uz_monitor.SNAPSHOT_FIELDS}
                base.update({
                    "travel_date": travel_date,
                    "train_number": "032P",
                    "wagon_class": "K",
                    "free_seats": 10,
                })
                old_rows.append(base)
                new_rows.append(dict(
                    base,
                    free_seats=11,
                    same_compartment_detail=(
                        "UZ account verification expired; exact seats unknown"
                    ),
                ))
            for path, rows in ((old_path, old_rows), (new_path, new_rows)):
                with path.open("w", encoding="utf-8", newline="") as handle:
                    writer = csv.DictWriter(
                        handle, fieldnames=uz_monitor.SNAPSHOT_FIELDS
                    )
                    writer.writeheader()
                    writer.writerows(rows)
            differences = uz_monitor.snapshot_differences(old_path, new_path)
            self.assertEqual(
                differences.count("Exact-seat scan requires UZ account verification."),
                1,
            )

    def test_unknown_seats_are_not_reported_as_unavailable(self):
        self.assertEqual(uz_monitor.analyze_coupe_wagons(None), ("", None, ""))

    def test_parses_current_wrapped_wagon_response(self):
        seats, together, details = uz_monitor.analyze_coupe_wagons({
            "wagons": [{"number": "4", "seats": [6, 7, 8]}],
        })
        self.assertEqual(seats, "car 4: 6, 7, 8")
        self.assertTrue(together)
        self.assertEqual(details, "car 4, compartment 2: 6, 7, 8")

    def test_assigns_seats_to_the_matching_train(self):
        payload = {"direct": [
            {
                "id": 101,
                "train": {"number": "101K", "wagon_classes": [
                    {"id": "K", "name": "Coupe", "free_seats": 2},
                ]},
            },
            {
                "id": 202,
                "train": {"number": "202K", "wagon_classes": [
                    {"id": "K", "name": "Coupe", "free_seats": 2},
                ]},
            },
        ]}
        result = uz_monitor.parse_availability(
            payload,
            "2026-08-08",
            {"k"},
            {
                "101": {"wagons": [{"number": "1", "seats": [1, 2]}]},
                "202": {"wagons": [{"number": "2", "seats": [5, 6]}]},
            },
        )
        self.assertEqual(result[0].free_seats_detail, "car 1: 1, 2")
        self.assertEqual(result[1].free_seats_detail, "car 2: 5, 6")

    def test_route_config_uses_separate_state_directories(self):
        config = uz_monitor.route_config(
            {"database": "data/legacy.sqlite3"},
            {"id": "kyiv-dnipro", "from_station_id": 1, "to_station_id": 2},
        )
        self.assertEqual(config["database"], "data/kyiv-dnipro/uz-monitor.sqlite3")
        self.assertEqual(config["snapshot_dir"], "data/kyiv-dnipro/snapshots")

    def test_telegram_compartment_summary_is_bounded(self):
        details = "; ".join(
            f"car 1, compartment {number}: 1, 2" for number in range(1, 6)
        )
        summary = uz_monitor.summarize_compartments(details)
        self.assertIn("compartment 3", summary)
        self.assertNotIn("compartment 4", summary)
        self.assertTrue(summary.endswith("2 more options"))

    def test_dates_include_one_boundary_probe(self):
        today = date.today()
        advertised = [(today + timedelta(days=i)).isoformat() for i in range(60)]
        config = {"sale_horizon_days": 14, "boundary_probe_days": 15}
        result = uz_monitor.dates_to_check(config, advertised)
        self.assertEqual(len(result), 15)
        self.assertEqual(result[-1], (today + timedelta(days=15)).isoformat())

    def test_configured_dates_include_one_boundary_probe(self):
        result = uz_monitor.configured_dates(
            {"sale_horizon_days": 14, "boundary_probe_days": 15}
        )
        self.assertEqual(len(result), 15)
        self.assertEqual(result[-1], (date.today() + timedelta(days=15)).isoformat())

    def test_changes_are_recorded(self):
        with tempfile.TemporaryDirectory() as directory:
            db = uz_monitor.connect_db(Path(directory) / "test.sqlite3")
            item = uz_monitor.Availability(
                "2026-08-01", "032P", None, None, "K", "Coupe", 3, None
            )
            uz_monitor.save_observation(db, "2026-07-28T08:00:00+00:00", item)
            item = uz_monitor.Availability(
                "2026-08-01", "032P", None, None, "K", "Coupe", 6, None
            )
            uz_monitor.save_observation(db, "2026-07-28T08:05:00+00:00", item)
            delta = db.execute("SELECT delta FROM changes ORDER BY id DESC LIMIT 1").fetchone()
            self.assertEqual(delta[0], 3)

    def test_session_id_is_persistent(self):
        with tempfile.TemporaryDirectory() as directory:
            config = {"session_file": str(Path(directory) / "session.json")}
            first = uz_monitor.persistent_session_id(config)
            second = uz_monitor.persistent_session_id(config)
            self.assertEqual(first, second)

    def test_load_env_does_not_override_existing_value(self):
        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / ".env"
            env_path.write_text("UZ_MONITOR_TEST=from-file\nQUOTED='hello world'\n")
            original = os.environ.get("UZ_MONITOR_TEST")
            os.environ["UZ_MONITOR_TEST"] = "existing"
            try:
                uz_monitor.load_env(env_path)
                self.assertEqual(os.environ["UZ_MONITOR_TEST"], "existing")
                self.assertEqual(os.environ["QUOTED"], "hello world")
            finally:
                if original is None:
                    os.environ.pop("UZ_MONITOR_TEST", None)
                else:
                    os.environ["UZ_MONITOR_TEST"] = original
                os.environ.pop("QUOTED", None)

    def test_csv_contains_only_explicit_change_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            csv_path = Path(directory) / "changes.csv"
            item = uz_monitor.Availability(
                "2026-08-01", "032P", None, None, "K", "Coupe", 6, 100
            )
            uz_monitor.append_change_csv(csv_path, "2026-07-28T09:00:00+00:00", item, 3)
            with csv_path.open(encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["delta"], "3")
            self.assertEqual(rows[0]["kind"], "appeared")

    def test_empty_change_csv_has_header_only(self):
        with tempfile.TemporaryDirectory() as directory:
            csv_path = Path(directory) / "changes.csv"
            uz_monitor.ensure_change_csv(csv_path)
            with csv_path.open(encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows, [])
            self.assertEqual(csv_path.read_text().count("\n"), 1)

    def test_unchanged_snapshot_is_deleted(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot_dir = Path(directory)
            rows = [{field: "" for field in uz_monitor.SNAPSHOT_FIELDS}]
            rows[0].update({"travel_date": "2026-08-01", "status": "no_coupe"})
            first = uz_monitor.persist_snapshot(
                snapshot_dir, "2026-07-28T10:00:00+00:00", rows
            )
            second = uz_monitor.persist_snapshot(
                snapshot_dir, "2026-07-28T10:05:00+00:00", rows
            )
            self.assertIsNotNone(first)
            self.assertIsNone(second)
            self.assertEqual(len(list(snapshot_dir.glob("snapshot-*.csv"))), 1)
            self.assertFalse((snapshot_dir / ".pending.csv").exists())

    def test_snapshot_with_collapsed_train_coverage_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot_dir = Path(directory)
            baseline = []
            for day in range(4):
                row = {field: "" for field in uz_monitor.SNAPSHOT_FIELDS}
                row.update({
                    "travel_date": f"2026-08-0{day + 1}",
                    "status": "no_coupe",
                    "train_number": "032P",
                })
                baseline.append(row)
            uz_monitor.persist_snapshot(
                snapshot_dir, "2026-07-28T10:00:00+00:00", baseline
            )
            collapsed = [dict(row, train_number="") for row in baseline]
            with self.assertRaises(uz_monitor.ApiError):
                uz_monitor.persist_snapshot(
                    snapshot_dir, "2026-07-28T10:05:00+00:00", collapsed
                )
            self.assertEqual(len(list(snapshot_dir.glob("snapshot-*.csv"))), 1)

    def test_snapshot_differences_include_increase_and_disappearance(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot_dir = Path(directory)
            old_rows = []
            for travel_date, seats in (("2026-08-01", 0), ("2026-08-02", 3)):
                row = {field: "" for field in uz_monitor.SNAPSHOT_FIELDS}
                row.update({
                    "travel_date": travel_date,
                    "status": "no_coupe" if seats == 0 else "available",
                    "train_number": "032P",
                    "wagon_class": "K",
                    "free_seats": seats,
                })
                old_rows.append(row)
            new_rows = [dict(old_rows[0], status="available", free_seats=4)]
            old_path = uz_monitor.persist_snapshot(
                snapshot_dir, "2026-07-28T10:00:00+00:00", old_rows
            )
            new_path = uz_monitor.persist_snapshot(
                snapshot_dir, "2026-07-28T10:05:00+00:00", new_rows
            )
            differences = uz_monitor.snapshot_differences(old_path, new_path)
            self.assertIn("2026-08-01: 032P, K: 0 → 4", differences)
            self.assertIn("2026-08-02: 032P, K: 3 → no data", differences)


if __name__ == "__main__":
    unittest.main()
