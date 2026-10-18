"""
One-shot script for GitHub Actions.
Checks Garmin for new runs → sends to Telegram → saves state.

State (sent activity IDs) is kept in state.json (committed to the repo).
Garmin tokens come from the GARMINTOKENS env var (GitHub secret).
"""

import json
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

import archive
import cardiac_drift
import weather
from garmin_sync import fetch_activities, is_run, login, parse_activity
from telegram import format_run, tg_send

load_dotenv()

STATE_FILE  = Path(__file__).parent / "state.json"

ARCHIVE_DELAY = timedelta(hours=3)

# Wide enough that runs missed during an outage (e.g. expired tokens) are sent
# once sync recovers; state.json prevents duplicates.
LOOKBACK_DAYS = 10


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"sent_ids": []}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(
        json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def archive_runs(client, runs: list, archived: set) -> list:
    """Writes runs to the Google Sheet archive once observed weather covers them.

    AEMET publishes hourly observations ~2 h late and keeps only 24 h, so a run
    is archived on the first sync ARCHIVE_DELAY after its end (older runs at
    once, with whatever station data is left — Garmin's airport observation).
    """
    now = datetime.now(timezone.utc)
    rows = []
    for act in runs:
        aid = str(act["activityId"])
        start = datetime.fromisoformat(act["startTimeGMT"]).replace(tzinfo=timezone.utc)
        if aid in archived or now - start - timedelta(seconds=act.get("elapsedDuration") or 0) < ARCHIVE_DELAY:
            continue
        km = (act.get("distance") or 0) / 1000
        drift = cardiac_drift.for_activity(client, aid, act.get("activityName") or "", km)
        rows.append(archive.run_row(act, drift, weather.for_activity(client, act)))
    if rows and archive.upsert(rows):
        archived |= {r["ID"] for r in rows}
        print(f"Archived {len(rows)} run(s).")
    return sorted(archived, key=int)[-300:]


def main() -> None:
    state    = load_state()
    sent_ids = set(state.get("sent_ids", []))

    # tokenstore=None → login() reads GARMINTOKENS env var automatically
    client = login()

    end   = date.today()
    start = end - timedelta(days=LOOKBACK_DAYS)
    activities = fetch_activities(client, start, end)
    runs = [a for a in activities if is_run(a)]

    print(f"Found {len(runs)} run(s) in the last {LOOKBACK_DAYS} days.")

    new_count = 0
    failed = 0
    for act in runs:
        row = parse_activity(act)
        if row["activity_id"] in sent_ids:
            print(f"  Skip (already sent): {row['start_time'][:10]}  {row['name']}")
            continue

        print(f"  New run: {row['start_time'][:10]}  {(row['distance_m'] or 0)/1000:.2f} km  {row['name']}")
        # Optional: a run is still reported if its detail data is unavailable
        drift = cardiac_drift.for_activity(client, row["activity_id"], row["name"] or "", (row["distance_m"] or 0) / 1000)
        w = weather.for_activity(client, act)
        text = format_run(row, cardiac_drift.format_lines(drift, w.line() if w else None) if drift else None)
        if not tg_send(text):
            failed += 1  # not marked as sent → retried next run
            continue
        sent_ids.add(row["activity_id"])
        new_count += 1
        time.sleep(1)

    print(f"Sent {new_count} new run(s) to Telegram.")

    if archive.enabled():
        state["archived_ids"] = archive_runs(client, runs, set(state.get("archived_ids", [])))

    # Garmin IDs grow over time: sorting keeps the file stable between runs
    # (no noise commits) and the cap drops the oldest IDs, not random ones.
    state["sent_ids"] = sorted(sent_ids, key=int)[-200:]
    save_state(state)

    if failed:
        sys.exit(f"{failed} run(s) failed to send to Telegram")


if __name__ == "__main__":
    main()
