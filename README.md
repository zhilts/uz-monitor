# UZ ticket availability monitor

Read-only monitor for direct Ukrainian Railways trains. Playwright runs Chrome
headlessly, opens the official UZ search page, and captures its JSON responses.
The monitor never purchases or reserves tickets.

## Features

- monitors one or more routes
- checks direct trains only
- tracks coupe availability for two passengers
- retrieves exact car and seat numbers when at least two seats are available
- detects whether two seats are in the same compartment
- stores full snapshots only when availability changes
- sends Telegram notifications with both the diff and current availability
- keeps a persistent Chrome profile in `data/chrome-profile`

By default, each route checks the next 14 days plus one boundary date to detect
changes to the sales window. Set `travel_dates` on a route to monitor only
specific dates while debugging.

## Configuration

Copy the example configuration and edit the local copy:

```bash
cp config.example.json config.json
```

`config.json` is intentionally ignored by Git. Routes are configured in the
`routes` array. Set `enabled` to temporarily enable or disable a route.

Each route gets separate state files automatically:

```text
data/<route-id>/uz-monitor.sqlite3
data/<route-id>/changes.csv
data/<route-id>/snapshots/snapshot-<UTC timestamp>.csv
```

The snapshot columns `free_seats_detail`, `same_compartment`, and
`same_compartment_detail` contain the full seat-level result. Every Telegram
change notification also lists all current rows with at least one available
seat.

## Running

```bash
./run-once.sh
python3 uz_monitor.py report
```

An unchanged candidate snapshot is deleted immediately. The first snapshot for
each route becomes its baseline and does not trigger a Telegram notification.

When UZ account verification expires, aggregate counts remain available, but
exact cars and seats cannot be refreshed. Run the interactive verification flow
to force an exact-seat scan across all enabled routes:

```bash
./verify-and-scan.sh
./verify-and-scan.sh --route dnipro-przemysl --date 2026-08-12
```

Complete verification in the opened Chrome window, return to the terminal, and
press Enter. The script closes the browser profile, bypasses the seat cache for
one scan, saves updated snapshots, and sends Telegram updates.

## Scheduled runs

A five-minute interval is suitable for initial data collection. In n8n, use a
Schedule Trigger and Execute Command:

Run `./run-once.sh` from the project directory.

Do not start a second run while the previous one is still active.

## Telegram

Create a local `.env` file:

```bash
TELEGRAM_BOT_TOKEN='...'
TELEGRAM_CHAT_ID='...'
```

The `.env` file is ignored by Git.

The optional Telegram command listener accepts messages only from
`TELEGRAM_CHAT_ID`:

```text
/status
/session
/scan
```

Install Telegram's command menu and persistent action buttons once:

```bash
python3 telegram_commands.py --install-menu
```

`/session` chooses an available date automatically and opens the monitor's Chrome
profile on the selected UZ search. Browse normally or complete
the account verification in Chrome, then send `/scan`. The scan closes that
Chrome instance, refreshes exact seats without using the cache, and sends the
full current availability to Telegram.

The Telegram listener uses long polling, so commands normally arrive immediately.
Run `./install-telegram-service.sh` to create a user-specific LaunchAgent locally,
start it at login, and restart it if it exits. The generated plist is never
stored in Git. For a foreground run:

```bash
./telegram-commands.sh
```

## reCAPTCHA

UZ may protect seat-level requests with reCAPTCHA. The monitor does not bypass
it. A persistent Chrome profile preserves a manually verified session for later
headless runs. Failures are recorded explicitly instead of being reported as
an absence of matching seats.

## Verification

```bash
python3 -m unittest -v
python3 -m py_compile uz_monitor.py
node --check playwright_fetch.mjs
```
